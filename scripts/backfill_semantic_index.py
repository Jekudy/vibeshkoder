"""Bounded semantic-index backfill, coverage audit, and retrieval shadow CLI.

Run from the repository root:

    python -m scripts.backfill_semantic_index <backfill|audit|shadow> ...

The CLI never prints source or query text.  Reports contain only counters,
content/query hashes, source identifiers, ranks, latency, and ledger IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO, cast

from sqlalchemy.exc import SQLAlchemyError

from bot.services.llm_providers import ProviderError


REPORT_SCHEMA_VERSION = 1
MAX_SHADOW_CASES = 1_000
MAX_QUESTION_ID_LENGTH = 128
MAX_SHADOW_QUERY_CHARS = 8_000
_QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ShadowCase:
    question_id: str
    chat_id: int
    query: str
    exclude_chat_message_id: int | None


def _scope_payload(chat_id: int | None) -> dict[str, int | bool]:
    if chat_id is None:
        return {"all_chats": True}
    return {"chat_id": chat_id}


def _resolve_chat_id(args: argparse.Namespace) -> int | None:
    chat_id = cast(int | None, getattr(args, "chat_id", None))
    all_chats = bool(getattr(args, "all_chats", False))
    if (chat_id is None) == (not all_chats):
        raise ValueError("choose exactly one of --chat-id or --all-chats")
    return chat_id


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")


def _write_private_report(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | (os.O_TRUNC if overwrite else os.O_EXCL)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, _json_bytes(payload))
    finally:
        os.close(descriptor)


def _emit_report(
    payload: dict[str, Any],
    *,
    report_path: str | None,
    overwrite: bool,
) -> None:
    if report_path is not None:
        _write_private_report(Path(report_path).expanduser(), payload, overwrite=overwrite)
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _parse_shadow_case(raw: Any, *, line_number: int) -> ShadowCase:
    if not isinstance(raw, dict):
        raise ValueError(f"shadow input line {line_number} must be a JSON object")
    allowed_keys = {"question_id", "chat_id", "query", "exclude_chat_message_id"}
    unknown_keys = sorted(set(raw) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"shadow input line {line_number} has unknown fields")

    question_id = raw.get("question_id")
    if not isinstance(question_id, str) or not _QUESTION_ID_RE.fullmatch(question_id):
        raise ValueError(
            f"shadow input line {line_number} question_id must be a safe opaque identifier"
        )
    if len(question_id) > MAX_QUESTION_ID_LENGTH:
        raise ValueError(f"shadow input line {line_number} question_id is too long")

    chat_id = raw.get("chat_id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise ValueError(f"shadow input line {line_number} chat_id must be an integer")

    query = raw.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"shadow input line {line_number} query must be non-empty text")
    if len(query) > MAX_SHADOW_QUERY_CHARS:
        raise ValueError(f"shadow input line {line_number} query is too long")

    exclude_id = raw.get("exclude_chat_message_id")
    if exclude_id is not None and (
        isinstance(exclude_id, bool) or not isinstance(exclude_id, int) or exclude_id < 1
    ):
        raise ValueError(
            f"shadow input line {line_number} exclude_chat_message_id must be positive"
        )
    return ShadowCase(
        question_id=question_id,
        chat_id=chat_id,
        query=query.strip(),
        exclude_chat_message_id=exclude_id,
    )


def _load_shadow_cases(path: Path, *, max_queries: int) -> tuple[ShadowCase, ...]:
    if max_queries < 1 or max_queries > MAX_SHADOW_CASES:
        raise ValueError(f"--max-queries must be between 1 and {MAX_SHADOW_CASES}")
    cases: list[ShadowCase] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            if len(cases) == max_queries:
                raise ValueError("shadow input exceeds --max-queries; no provider calls were made")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"shadow input line {line_number} is not valid JSON") from exc
            case = _parse_shadow_case(raw, line_number=line_number)
            if case.question_id in seen_ids:
                raise ValueError(f"shadow input line {line_number} repeats question_id")
            seen_ids.add(case.question_id)
            cases.append(case)
    if not cases:
        raise ValueError("shadow input contains no cases")
    return tuple(cases)


def _document_identity(
    document: Any,
    *,
    model: str,
) -> tuple[str, str, str, int, int, int, str, str]:
    return (
        str(document.source_type),
        str(document.source_id),
        str(document.source_revision),
        int(document.chunk_index),
        int(document.chunk_count),
        int(document.chat_id),
        str(document.content_hash),
        model,
    )


async def _eligible_identities(
    session: Any,
    *,
    chat_id: int | None,
    batch_size: int,
    model: str,
) -> dict[tuple[str, str, str, int, int, int, str, str], tuple[int, ...]]:
    from bot.services.semantic_index import (
        list_eligible_card_documents,
        list_eligible_message_documents,
    )

    identities: dict[tuple[str, str, str, int, int, int, str, str], tuple[int, ...]] = {}
    message_cursor = 0
    while True:
        batch = await list_eligible_message_documents(
            session,
            after_id=message_cursor,
            limit=batch_size,
            chat_id=chat_id,
        )
        if not batch:
            break
        identities.update(
            {
                _document_identity(document, model=model): tuple(document.message_version_ids)
                for document in batch
            }
        )
        message_next_cursor = int(batch[-1].source_id)
        if message_next_cursor <= message_cursor:
            raise RuntimeError("semantic message audit cursor did not advance")
        message_cursor = message_next_cursor

    card_cursor = ""
    while True:
        batch = await list_eligible_card_documents(
            session,
            after_id=card_cursor,
            limit=batch_size,
            chat_id=chat_id,
        )
        if not batch:
            break
        identities.update(
            {
                _document_identity(document, model=model): tuple(document.message_version_ids)
                for document in batch
            }
        )
        card_next_cursor = str(batch[-1].source_id)
        if card_next_cursor <= card_cursor:
            raise RuntimeError("semantic card audit cursor did not advance")
        card_cursor = card_next_cursor
    return identities


async def _coverage_report(
    session: Any,
    *,
    chat_id: int | None,
    model: str,
    batch_size: int,
) -> dict[str, Any]:
    from sqlalchemy import text

    expected = await _eligible_identities(
        session,
        chat_id=chat_id,
        batch_size=batch_size,
        model=model,
    )

    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT unit.source_type,
                       unit.source_id,
                       unit.source_revision,
                       unit.chunk_index,
                       unit.chunk_count,
                       unit.chat_id,
                       unit.content_hash,
                       unit.embedding_model,
                       unit.invalidated_at,
                       COALESCE(
                           ARRAY_AGG(source.message_version_id ORDER BY source.position)
                               FILTER (WHERE source.message_version_id IS NOT NULL),
                           ARRAY[]::INTEGER[]
                       ) AS message_version_ids
                FROM semantic_retrieval_units unit
                LEFT JOIN semantic_retrieval_unit_sources source ON source.unit_id = unit.id
                WHERE unit.embedding_model = :model
                  AND (CAST(:chat_id AS BIGINT) IS NULL
                       OR unit.chat_id = CAST(:chat_id AS BIGINT))
                GROUP BY unit.id
                """
                ),
                {"model": model, "chat_id": chat_id},
            )
        )
        .mappings()
        .all()
    )

    active: dict[tuple[str, str, str, int, int, int, str, str], tuple[int, ...]] = {}
    invalidated: dict[tuple[str, str, str, int, int, int, str, str], tuple[int, ...]] = {}
    for row in rows:
        key = (
            str(row["source_type"]),
            str(row["source_id"]),
            str(row["source_revision"]),
            int(row["chunk_index"]),
            int(row["chunk_count"]),
            int(row["chat_id"]),
            str(row["content_hash"]),
            str(row["embedding_model"]),
        )
        target = active if row["invalidated_at"] is None else invalidated
        target[key] = tuple(int(value) for value in row["message_version_ids"])

    expected_keys = set(expected)
    active_keys = set(active)
    invalidated_keys = set(invalidated)
    covered = {key for key in expected_keys & active_keys if active[key] == expected[key]}
    provenance_mismatch = {
        key for key in expected_keys & active_keys if active[key] != expected[key]
    }
    missing_identity = expected_keys - active_keys
    missing = missing_identity | provenance_mismatch
    invalidated_expected = missing_identity & invalidated_keys
    unexpected_active = active_keys - expected_keys
    eligible = len(expected)
    no_eligible_identities = eligible == 0
    coverage_percent = 0.0 if no_eligible_identities else round((len(covered) / eligible) * 100, 2)
    status = (
        "pass" if not no_eligible_identities and not missing and not unexpected_active else "fail"
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "audit",
        "status": status,
        "scope": _scope_payload(chat_id),
        "embedding_model": model,
        "eligible": eligible,
        "indexed": len(covered),
        "coverage_percent": coverage_percent,
        "missing": len(missing),
        "unexpected_active": len(unexpected_active),
        "reason_counts": {
            "failed:no_eligible_identities": int(no_eligible_identities),
            "indexed:active_identity": len(covered),
            "failed:missing_identity": len(missing - invalidated_expected),
            "failed:expected_identity_invalidated": len(invalidated_expected),
            "failed:provenance_mismatch": len(provenance_mismatch),
            "failed:unexpected_active_identity": len(unexpected_active),
        },
    }


async def _run_audit(args: argparse.Namespace) -> int:
    from bot.db.engine import async_session
    from bot.services.llm_gateway import load_embedding_gateway_config

    chat_id = _resolve_chat_id(args)
    config = load_embedding_gateway_config()
    try:
        async with async_session() as session:
            report = await _coverage_report(
                session,
                chat_id=chat_id,
                model=config.model,
                batch_size=args.batch_size,
            )
    except (RuntimeError, SQLAlchemyError) as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "mode": "audit",
            "status": "failed",
            "scope": _scope_payload(chat_id),
            "embedding_model": config.model,
            "failed": 1,
            "reason_counts": {f"failed:{type(exc).__name__}": 1},
        }
        _emit_report(report, report_path=args.report, overwrite=args.overwrite)
        return 1
    _emit_report(report, report_path=args.report, overwrite=args.overwrite)
    return 0 if report["status"] == "pass" else 1


async def _run_backfill(args: argparse.Namespace) -> int:
    from bot.db.engine import async_session
    from bot.services.llm_gateway import load_embedding_gateway_config
    from bot.services.semantic_index import backfill_semantic_index

    chat_id = _resolve_chat_id(args)
    config = load_embedding_gateway_config()
    try:
        async with async_session() as session:
            result = await backfill_semantic_index(
                session,
                config=config,
                batch_size=args.batch_size,
                chat_id=chat_id,
            )
            coverage = await _coverage_report(
                session,
                chat_id=chat_id,
                model=config.model,
                batch_size=args.batch_size,
            )
    except (ProviderError, RuntimeError, SQLAlchemyError) as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "mode": "backfill",
            "status": "failed",
            "scope": _scope_payload(chat_id),
            "embedding_model": config.model,
            "failed": 1,
            "reason_counts": {f"failed:{type(exc).__name__}": 1},
        }
        _emit_report(report, report_path=args.report, overwrite=args.overwrite)
        return 1
    status = "pass" if result.failed == 0 and coverage["status"] == "pass" else "fail"
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "backfill",
        "status": status,
        "scope": _scope_payload(chat_id),
        "embedding_model": config.model,
        "embedding_dimensions": config.dimensions,
        "run_id": result.run_id,
        "eligible": result.eligible,
        "indexed": result.indexed,
        "skipped": result.skipped,
        "failed": result.failed,
        "reason_counts": result.reason_counts,
        "coverage": {
            key: value
            for key, value in coverage.items()
            if key
            in {
                "status",
                "eligible",
                "indexed",
                "coverage_percent",
                "missing",
                "unexpected_active",
                "reason_counts",
            }
        },
    }
    _emit_report(report, report_path=args.report, overwrite=args.overwrite)
    return 0 if status == "pass" else 1


def _source_key(hit: Any) -> str:
    if hit.source_type == "card":
        if hit.card_id is None:
            raise ValueError("card shadow hit is missing card_id")
        return f"card:{hit.card_id}"
    return f"message:{hit.message_version_id}"


def _ranked_keys(
    candidate_ranks: dict[str, dict[str, int]],
    *,
    branch: str,
    limit: int,
) -> list[str]:
    ranked = ((ranks[branch], key) for key, ranks in candidate_ranks.items() if branch in ranks)
    return [key for _, key in sorted(ranked)[:limit]]


def _open_private_jsonl(path: Path, *, overwrite: bool) -> TextIO:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | (os.O_TRUNC if overwrite else os.O_EXCL)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _shadow_failure_payload(*, processed: int, exc: BaseException) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "shadow_summary",
        "status": "failed",
        "processed": processed,
        "failed": 1,
        "reason_counts": {f"failed:{type(exc).__name__}": 1},
        "contains_raw_text": False,
        "synthesis_called": False,
    }


async def _run_shadow(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if input_path == output_path:
        raise ValueError("shadow input and output paths must differ")
    cases = _load_shadow_cases(input_path, max_queries=args.max_queries)

    from bot.db.engine import async_session
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import (
        EmbeddingBudgetExceeded,
        embed_texts,
        load_embedding_gateway_config,
    )
    from bot.services.semantic_index import hybrid_search

    config = load_embedding_gateway_config()

    processed = 0
    with _open_private_jsonl(output_path, overwrite=args.overwrite) as destination:
        destination.write(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "mode": "shadow",
                    "status": "running",
                    "case_count": len(cases),
                    "embedding_model": config.model,
                    "embedding_dimensions": config.dimensions,
                    "contains_raw_text": False,
                    "synthesis_called": False,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n"
        )
        async with async_session() as session:
            for case in cases:
                try:
                    embedding = await embed_texts(
                        session,
                        inputs=[case.query],
                        config=config,
                        ledger_repo=LedgerRepo(),
                    )
                except (EmbeddingBudgetExceeded, ProviderError) as exc:
                    # The gateway wrote a bounded failure row. Persist that audit
                    # before stopping this no-retry shadow run.
                    await session.commit()
                    failure = _shadow_failure_payload(processed=processed, exc=exc)
                    destination.write(json.dumps(failure, sort_keys=True) + "\n")
                    destination.flush()
                    print(json.dumps(failure, sort_keys=True))
                    return 1
                # Preserve the provider usage/cost audit even if retrieval later fails.
                await session.commit()
                try:
                    result = await hybrid_search(
                        session,
                        query=case.query,
                        query_embedding=embedding.vectors[0],
                        chat_id=case.chat_id,
                        embedding_model=config.model,
                        exclude_chat_message_id=case.exclude_chat_message_id,
                        candidate_limit=args.candidate_limit,
                        limit=args.limit,
                    )
                except (LookupError, RuntimeError, SQLAlchemyError, ValueError) as exc:
                    await session.rollback()
                    failure = _shadow_failure_payload(processed=processed, exc=exc)
                    destination.write(json.dumps(failure, sort_keys=True) + "\n")
                    destination.flush()
                    print(json.dumps(failure, sort_keys=True))
                    return 1
                hybrid_keys = [_source_key(hit) for hit in result.hits]
                payload = {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "mode": "shadow_case",
                    "question_id": case.question_id,
                    "query_sha256": hashlib.sha256(case.query.encode("utf-8")).hexdigest(),
                    "chat_id": case.chat_id,
                    "embedding_llm_call_id": embedding.llm_usage_ledger_id,
                    "fts_top": _ranked_keys(
                        result.candidate_ranks,
                        branch="fts",
                        limit=args.limit,
                    ),
                    "vector_top": _ranked_keys(
                        result.candidate_ranks,
                        branch="vector",
                        limit=args.limit,
                    ),
                    "hybrid_top": hybrid_keys,
                    "candidate_ranks": result.candidate_ranks,
                    "latency_ms": {
                        "fts": result.fts_latency_ms,
                        "vector": result.vector_latency_ms,
                        "fusion": result.fusion_latency_ms,
                        "total": result.total_latency_ms,
                    },
                }
                destination.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
                destination.flush()
                processed += 1
        destination.write(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "mode": "shadow_summary",
                    "status": "completed",
                    "processed": processed,
                    "failed": 0,
                    "contains_raw_text": False,
                    "synthesis_called": False,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n"
        )
    print(
        json.dumps(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "mode": "shadow",
                "status": "completed",
                "processed": processed,
                "failed": 0,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


def _bounded_batch_size(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 100:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 100")
    return parsed


def _bounded_candidate_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 5 or parsed > 100:
        raise argparse.ArgumentTypeError("candidate limit must be between 5 and 100")
    return parsed


def _bounded_result_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 8:
        raise argparse.ArgumentTypeError("result limit must be between 1 and 8")
    return parsed


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--chat-id", type=int, default=None)
    scope.add_argument(
        "--all-chats",
        action="store_true",
        help="Explicitly acknowledge that every eligible chat will be processed.",
    )


def _add_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report",
        default=None,
        help="Optional private JSON report path; stdout always receives the same safe payload.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly allow replacing an existing report file.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.backfill_semantic_index")
    subcommands = parser.add_subparsers(dest="command", required=True)

    backfill = subcommands.add_parser(
        "backfill",
        help="Idempotently index eligible retrieval units and verify complete coverage.",
    )
    _add_scope_arguments(backfill)
    _add_report_arguments(backfill)
    backfill.add_argument("--batch-size", type=_bounded_batch_size, default=64)
    backfill.set_defaults(async_func=_run_backfill)

    audit = subcommands.add_parser(
        "audit",
        help="Read-only complete-coverage and unexpected-active-unit audit; no provider calls.",
    )
    _add_scope_arguments(audit)
    _add_report_arguments(audit)
    audit.add_argument("--batch-size", type=_bounded_batch_size, default=64)
    audit.set_defaults(async_func=_run_audit)

    shadow = subcommands.add_parser(
        "shadow",
        help="Replay private questions through embeddings + hybrid retrieval only.",
    )
    shadow.add_argument("--input", required=True, help="Private JSONL question file.")
    shadow.add_argument("--output", required=True, help="Private no-content JSONL audit path.")
    shadow.add_argument("--max-queries", type=int, default=50)
    shadow.add_argument("--candidate-limit", type=_bounded_candidate_limit, default=20)
    shadow.add_argument("--limit", type=_bounded_result_limit, default=5)
    shadow.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly allow replacing an existing shadow report.",
    )
    shadow.set_defaults(async_func=_run_shadow)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.async_func(args))
    except (FileExistsError, FileNotFoundError, PermissionError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "status": "error",
                    "error_class": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (LookupError, ProviderError, RuntimeError, SQLAlchemyError) as exc:
        # Provider/DB exception messages may contain URLs, credentials, or payload data.
        print(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
