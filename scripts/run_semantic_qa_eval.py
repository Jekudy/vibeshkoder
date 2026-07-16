"""Run the private frozen semantic Q&A set through the production provider path.

The input and review output contain private query/evidence text and therefore
never leave the operator host.  Stdout/stderr contain only aggregate metadata
and exception taxonomy.  The runner deliberately bypasses product quota rows,
but it uses the real embedding/synthesis gateways so every provider call is
recorded in ``llm_usage_ledger``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

from sqlalchemy.exc import SQLAlchemyError

from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.services.evidence import EvidenceBundle, EvidenceItem
from bot.services.llm_gateway import (
    Abstention,
    AnswerWithCitations,
    EmbeddingBudgetExceeded,
    filter_surviving_evidence,
    load_embedding_gateway_config,
    load_gateway_config,
    resolve_provider,
    synthesize_answer,
)
from bot.services.llm_providers import ProviderStructuralError, ProviderTransientError
from bot.services.qa_guardrails import build_guarded_llm_query, contains_secret_like_data
from bot.services.qa_trigger import MAX_USER_QUERY_CHARS
from bot.services.semantic_eval import SemanticEvalCase
from bot.services.semantic_index import HybridSearchResult, hybrid_search


MIN_FROZEN_CASES = 50
PRIVATE_RUN_SCHEMA_VERSION = 2
SEMANTIC_EVIDENCE_LIMIT = 5
SEMANTIC_PROVIDER = "deepseek"
SEMANTIC_MODEL = "deepseek-v4-flash"

_CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_INPUT_FIELDS = {
    "case_id",
    "category",
    "chat_id",
    "query",
    "expected_source_ids",
    "forbidden_source_ids",
    "expected_abstain",
    "exclude_chat_message_id",
}


@dataclass(frozen=True, slots=True)
class PrivateEvalCase:
    label: SemanticEvalCase
    chat_id: int
    query: str
    exclude_chat_message_id: int | None


def _guard_eval_query(query: str) -> str:
    """Apply the production query boundary before any eval provider call."""

    if len(query) > MAX_USER_QUERY_CHARS:
        raise ValueError("private evaluation query exceeds the production query limit")
    return build_guarded_llm_query(query)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"input line {line_number} is not valid JSON") from exc
            if not isinstance(row, dict) or set(row) != _INPUT_FIELDS:
                raise ValueError(f"input line {line_number} has an invalid schema")
            rows.append(row)
    if not rows:
        raise ValueError("private evaluation input is empty")
    return rows


def load_private_cases(path: Path, *, smoke: bool) -> tuple[PrivateEvalCase, ...]:
    """Validate the complete private input before any provider dispatch."""

    cases: list[PrivateEvalCase] = []
    for row in _load_jsonl(path):
        case_id = row["case_id"]
        if not isinstance(case_id, str) or _CASE_ID_RE.fullmatch(case_id) is None:
            raise ValueError("case_id must be an opaque safe identifier")
        expected_ids = row["expected_source_ids"]
        forbidden_ids = row["forbidden_source_ids"]
        if not isinstance(expected_ids, list) or not isinstance(forbidden_ids, list):
            raise ValueError("expected and forbidden source ids must be JSON arrays")
        label = SemanticEvalCase(
            case_id=case_id,
            category=row["category"],
            expected_source_ids=tuple(expected_ids),
            forbidden_source_ids=tuple(forbidden_ids),
            expected_abstain=row["expected_abstain"],
        )
        chat_id = row["chat_id"]
        if type(chat_id) is not int or chat_id == 0:
            raise ValueError("chat_id must be a non-zero integer")
        query = row["query"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or query != query.strip()
            or len(query) > 256
        ):
            raise ValueError("query must be a non-empty trimmed string of at most 256 chars")
        _guard_eval_query(query)
        exclude_id = row["exclude_chat_message_id"]
        if exclude_id is not None and (type(exclude_id) is not int or exclude_id < 1):
            raise ValueError("exclude_chat_message_id must be null or a positive integer")
        cases.append(
            PrivateEvalCase(
                label=label,
                chat_id=chat_id,
                query=query,
                exclude_chat_message_id=exclude_id,
            )
        )
    if len({case.label.case_id for case in cases}) != len(cases):
        raise ValueError("private evaluation case ids must be unique")
    if smoke:
        if len(cases) != 1:
            raise ValueError("provider smoke mode requires exactly one case")
    else:
        if len(cases) < MIN_FROZEN_CASES:
            raise ValueError("frozen private evaluation requires at least 50 cases")
        categories = {case.label.category for case in cases}
        if categories != {
            "semantic",
            "exact",
            "multi_source",
            "no_answer",
            "privacy_governance",
        }:
            raise ValueError("frozen private evaluation requires all five categories")
    return tuple(cases)


def _open_private_text(path: Path, *, overwrite: bool) -> TextIO:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _write_jsonl(target: TextIO, payload: dict[str, Any]) -> None:
    target.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    target.flush()


def _source_key(item: EvidenceItem) -> str:
    if item.source_type == "card":
        if item.card_id is None:
            raise ValueError("card evidence is missing its canonical card id")
        return f"card:{item.card_id}"
    return f"message:{item.message_version_id}"


def _ranked_branch_keys(candidate_ranks: dict[str, dict[str, int]], branch: str) -> list[str]:
    ranked = [
        (ranks[branch], source_key)
        for source_key, ranks in candidate_ranks.items()
        if branch in ranks
    ]
    return [source_key for _, source_key in sorted(ranked)]


def _telegram_link(item: EvidenceItem) -> str:
    short_chat_id = str(item.chat_id).removeprefix("-100")
    return f"https://t.me/c/{short_chat_id}/{item.message_id}"


def _sanitized_case_row(case: PrivateEvalCase) -> dict[str, Any]:
    return {
        "case_id": case.label.case_id,
        "category": case.label.category,
        "expected_source_ids": list(case.label.expected_source_ids),
        "forbidden_source_ids": list(case.label.forbidden_source_ids),
        "expected_abstain": case.label.expected_abstain,
    }


def _sanitized_observation_row(review_row: dict[str, Any]) -> dict[str, Any]:
    reviewed_result = review_row["reviewed_result"]
    return {
        "record_type": "case_observation",
        "case_id": review_row["case_id"],
        "fts_source_ids": review_row["fts_source_ids"],
        "hybrid_source_ids": review_row["hybrid_source_ids"],
        "abstained": reviewed_result["abstained"],
        "leakage_count": review_row["objective_leakage_count"],
        "retrieval_latency_seconds": reviewed_result["retrieval_latency_seconds"],
        "full_latency_seconds": reviewed_result["full_latency_seconds"],
    }


async def _run_case(case: PrivateEvalCase) -> dict[str, Any]:
    from bot.db.engine import async_session

    guarded_query = _guard_eval_query(case.query)
    started = time.monotonic()
    embedding_config = load_embedding_gateway_config()
    synthesis_config = load_gateway_config()
    if synthesis_config.provider != SEMANTIC_PROVIDER or synthesis_config.model != SEMANTIC_MODEL:
        raise ValueError("semantic evaluation requires DeepSeek V4 Flash configuration")

    async with async_session() as session:
        from bot.services.llm_gateway import embed_texts

        embedding = await embed_texts(
            session,
            inputs=[case.query],
            config=embedding_config,
            ledger_repo=LedgerRepo(),
            qa_trace_id=None,
        )
        retrieval: HybridSearchResult = await hybrid_search(
            session,
            query=case.query,
            query_embedding=embedding.vectors[0],
            chat_id=case.chat_id,
            embedding_model=embedding_config.model,
            exclude_chat_message_id=case.exclude_chat_message_id,
        )
        retrieval_finished = time.monotonic()
        bundle = EvidenceBundle.from_hits(case.query, case.chat_id, list(retrieval.hits))
        synth_result: AnswerWithCitations | Abstention | None = None
        rendered_bundle = bundle
        abstention_reason: str | None = None
        if bundle.abstained or not bundle.evidence_ids:
            abstention_reason = "empty_bundle"
        elif any(contains_secret_like_data(item.snippet) for item in bundle.items):
            abstention_reason = "sensitive_input"
            rendered_bundle = EvidenceBundle.from_hits(case.query, case.chat_id, [])
        else:
            synth_result = await synthesize_answer(
                session,
                bundle=bundle,
                query=guarded_query,
                config=synthesis_config,
                qa_trace_id=None,
                ledger_repo=LedgerRepo(),
                cache_repo=SynthesisCacheRepo(),
                provider=resolve_provider(synthesis_config.provider),
                max_evidence_items=SEMANTIC_EVIDENCE_LIMIT,
                durable_placeholder=True,
                revalidate_after_provider=True,
            )
            if isinstance(synth_result, AnswerWithCitations):
                rendered_bundle = await filter_surviving_evidence(
                    session,
                    bundle,
                    max_evidence_items=SEMANTIC_EVIDENCE_LIMIT,
                )
                expected_ids = synth_result.surviving_evidence_ids
                if tuple(rendered_bundle.evidence_ids) != expected_ids or not set(
                    synth_result.citation_ids
                ).issubset(expected_ids):
                    abstention_reason = "citation_rejected"
            else:
                abstention_reason = synth_result.reason
            await session.commit()

    hybrid_ids = [_source_key(item) for item in retrieval.hits]
    forbidden = set(case.label.forbidden_source_ids)
    leakage_count = len(set(hybrid_ids) & forbidden)
    answer = (
        synth_result.answer_text
        if isinstance(synth_result, AnswerWithCitations) and abstention_reason is None
        else None
    )
    cited_source_ids: list[str] = []
    if isinstance(synth_result, AnswerWithCitations) and abstention_reason is None:
        by_anchor = {item.message_version_id: _source_key(item) for item in rendered_bundle.items}
        cited_source_ids = [by_anchor[value] for value in synth_result.citation_ids]
    full_finished = time.monotonic()
    return {
        "record_type": "case_review",
        "case_id": case.label.case_id,
        "category": case.label.category,
        "query": case.query,
        "answer": answer,
        "abstention_reason": abstention_reason,
        "fts_source_ids": _ranked_branch_keys(retrieval.candidate_ranks, "fts"),
        "hybrid_source_ids": hybrid_ids,
        "cited_source_ids": cited_source_ids,
        "evidence": [
            {
                "source_id": _source_key(item),
                "message_version_id": item.message_version_id,
                "snippet": item.snippet,
                "source_link": _telegram_link(item),
            }
            for item in rendered_bundle.items
        ],
        "embedding_llm_call_id": embedding.llm_usage_ledger_id,
        "synthesis_llm_call_id": synth_result.llm_call_id if synth_result else None,
        "cache_hit": synth_result.cache_hit
        if isinstance(synth_result, AnswerWithCitations)
        else False,
        "objective_leakage_count": leakage_count,
        "reviewed_result": {
            "case_id": case.label.case_id,
            "fts_source_ids": _ranked_branch_keys(retrieval.candidate_ranks, "fts"),
            "hybrid_source_ids": hybrid_ids,
            "abstained": answer is None,
            "leakage_count": leakage_count,
            "valid_source_links": None,
            "invalid_source_links": None,
            "unsupported_claims": None,
            "total_claims": None,
            "retrieval_latency_seconds": retrieval_finished - started,
            "full_latency_seconds": full_finished - started,
        },
    }


async def run_private_evaluation(args: argparse.Namespace) -> int:
    release_sha = args.release_sha
    if _RELEASE_SHA_RE.fullmatch(release_sha) is None:
        raise ValueError("release SHA must be exactly 40 lowercase hexadecimal characters")
    input_path = Path(args.input).expanduser()
    cases = load_private_cases(input_path, smoke=args.smoke)
    dataset_hash = _sha256_file(input_path)
    cases_path = Path(args.cases_output).expanduser()
    observations_path = Path(args.observations_output).expanduser()
    review_path = Path(args.review_output).expanduser()
    with (
        _open_private_text(cases_path, overwrite=args.overwrite) as cases_target,
        _open_private_text(observations_path, overwrite=args.overwrite) as observations_target,
        _open_private_text(review_path, overwrite=args.overwrite) as review_target,
    ):
        for case in cases:
            _write_jsonl(cases_target, _sanitized_case_row(case))
        _write_jsonl(
            observations_target,
            {
                "record_type": "header",
                "schema_version": PRIVATE_RUN_SCHEMA_VERSION,
                "contains_raw_text": False,
                "release_sha": release_sha,
                "dataset_sha256": dataset_hash,
                "case_count": len(cases),
            },
        )
        _write_jsonl(
            review_target,
            {
                "record_type": "header",
                "schema_version": PRIVATE_RUN_SCHEMA_VERSION,
                "contains_raw_text": True,
                "private_review_required": True,
                "release_sha": release_sha,
                "dataset_sha256": dataset_hash,
                "case_count": len(cases),
            },
        )
        for case in cases:
            row = await _run_case(case)
            row["release_sha"] = release_sha
            row["dataset_sha256"] = dataset_hash
            _write_jsonl(observations_target, _sanitized_observation_row(row))
            _write_jsonl(review_target, row)
    observations_hash = _sha256_file(observations_path)
    print(
        json.dumps(
            {
                "schema_version": PRIVATE_RUN_SCHEMA_VERSION,
                "status": "review_required",
                "contains_raw_text": False,
                "release_sha": release_sha,
                "dataset_sha256": dataset_hash,
                "observations_sha256": observations_hash,
                "case_count": len(cases),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.run_semantic_qa_eval")
    parser.add_argument("--input", required=True)
    parser.add_argument("--cases-output", required=True)
    parser.add_argument("--observations-output", required=True)
    parser.add_argument("--review-output", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_private_evaluation(args))
    except (
        FileExistsError,
        FileNotFoundError,
        PermissionError,
        EmbeddingBudgetExceeded,
        ProviderStructuralError,
        ProviderTransientError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema_version": PRIVATE_RUN_SCHEMA_VERSION,
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "contains_raw_text": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
