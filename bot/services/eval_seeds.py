"""Loader for golden-recall seed fixtures used by the offline eval harness.

The seed directory layout (canonical for ``seed_v1``):

    seed_v1/
      chat_history.jsonl    — one message per line; rows feed the production
                              ingestion path during the eval fixture
      queries.jsonl         — one (query, expected_local_ids, expected_abstain)
                              triple per line
      seed_meta.yaml        — human-readable manifest (NOT parsed by this loader;
                              ``seed_id`` / ``version`` are passed explicitly so
                              the project does not pull in a YAML runtime dep)

``seed_hash`` is sha256 over the canonical JSONL bytes of the message list and
serves as the deterministic content fingerprint that the harness writes into
``eval_results.jsonl`` (see PHASE11_PLAN.md §8.1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MessageRow:
    seed_local_id: str
    user_id_local: int
    text: str
    ts: datetime
    message_kind: str
    caption: str | None


@dataclass(frozen=True, slots=True)
class QueryRow:
    query_id: str
    query: str
    expected_message_version_ids: tuple[str, ...]
    expected_abstain: bool


@dataclass(frozen=True, slots=True)
class SeedSpec:
    seed_id: str
    version: int
    seed_hash: str
    messages: tuple[MessageRow, ...]
    queries: tuple[QueryRow, ...]


CHAT_HISTORY_FILENAME = "chat_history.jsonl"
QUERIES_FILENAME = "queries.jsonl"


def compute_seed_hash(messages_jsonl_bytes: bytes) -> str:
    return hashlib.sha256(messages_jsonl_bytes).hexdigest()


def canonical_jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    body = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    return f"{body}\n".encode("utf-8")


def load_seed_spec(seed_dir: Path, *, seed_id: str, version: int) -> SeedSpec:
    if not seed_dir.is_dir():
        raise FileNotFoundError(f"seed directory not found: {seed_dir}")
    if version < 1:
        raise ValueError(f"version must be >= 1, got {version}")
    if not seed_id:
        raise ValueError("seed_id must be non-empty")

    history_path = seed_dir / CHAT_HISTORY_FILENAME
    queries_path = seed_dir / QUERIES_FILENAME

    raw_messages = _load_jsonl(history_path)
    raw_queries = _load_jsonl(queries_path)

    seed_hash = compute_seed_hash(canonical_jsonl_bytes(raw_messages))

    messages = tuple(
        _message_from_row(row, history_path, line_no)
        for line_no, row in enumerate(raw_messages, start=1)
    )
    queries = tuple(
        _query_from_row(row, queries_path, line_no)
        for line_no, row in enumerate(raw_queries, start=1)
    )

    seen_message_ids = {m.seed_local_id for m in messages}
    if len(seen_message_ids) != len(messages):
        raise ValueError(f"{history_path}: duplicate seed_local_id detected")

    seen_query_ids = {q.query_id for q in queries}
    if len(seen_query_ids) != len(queries):
        raise ValueError(f"{queries_path}: duplicate query_id detected")

    for query in queries:
        unknown = [mv for mv in query.expected_message_version_ids if mv not in seen_message_ids]
        if unknown:
            raise ValueError(
                f"{queries_path}: query {query.query_id!r} references unknown seed_local_ids {unknown}"
            )

    return SeedSpec(
        seed_id=seed_id,
        version=version,
        seed_hash=seed_hash,
        messages=messages,
        queries=queries,
    )


def resolve_expected_ids(
    query: QueryRow,
    seed_local_id_map: dict[str, int],
) -> list[int]:
    return [seed_local_id_map[seed_local_id] for seed_local_id in query.expected_message_version_ids]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"seed file missing: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object, got {type(row).__name__}")
            rows.append(row)
    return rows


def _message_from_row(row: dict[str, Any], path: Path, line_no: int) -> MessageRow:
    location = f"{path}:{line_no}"
    seed_local_id = _required_nonempty_str(row, "seed_local_id", location)
    user_id_local = _required_int(row, "user_id_local", location)
    text = _required_str_field(row, "text", location)
    ts_raw = _required_nonempty_str(row, "ts", location)
    message_kind = _required_nonempty_str(row, "message_kind", location)
    caption_raw = row.get("caption")
    caption: str | None
    if caption_raw is None:
        caption = None
    elif isinstance(caption_raw, str):
        caption = caption_raw
    else:
        raise ValueError(f"{location}: 'caption' must be string or null, got {type(caption_raw).__name__}")
    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError as exc:
        raise ValueError(f"{location}: invalid 'ts' iso format: {exc!s}") from exc
    return MessageRow(
        seed_local_id=seed_local_id,
        user_id_local=user_id_local,
        text=text,
        ts=ts,
        message_kind=message_kind,
        caption=caption,
    )


def _query_from_row(row: dict[str, Any], path: Path, line_no: int) -> QueryRow:
    location = f"{path}:{line_no}"
    query_id = _required_nonempty_str(row, "query_id", location)
    query = _required_nonempty_str(row, "query", location)
    if "expected_message_version_ids" not in row:
        raise ValueError(f"{location}: missing required field 'expected_message_version_ids'")
    expected = row["expected_message_version_ids"]
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError(f"{location}: 'expected_message_version_ids' must be list[str]")
    if "expected_abstain" not in row:
        raise ValueError(f"{location}: missing required field 'expected_abstain'")
    expected_abstain = row["expected_abstain"]
    if not isinstance(expected_abstain, bool):
        raise ValueError(f"{location}: 'expected_abstain' must be bool")
    if expected_abstain and expected:
        raise ValueError(
            f"{location}: query {query_id!r} has expected_abstain=true but non-empty expected_message_version_ids"
        )
    if not expected_abstain and not expected:
        raise ValueError(
            f"{location}: query {query_id!r} has expected_abstain=false but empty expected_message_version_ids"
        )
    return QueryRow(
        query_id=query_id,
        query=query,
        expected_message_version_ids=tuple(expected),
        expected_abstain=expected_abstain,
    )


def _required_nonempty_str(source: dict[str, Any], key: str, location: Path | str) -> str:
    if key not in source:
        raise ValueError(f"{location}: missing required field '{key}'")
    value = source[key]
    if not isinstance(value, str):
        raise ValueError(f"{location}: '{key}' must be string, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{location}: '{key}' must be non-empty")
    return value


def _required_str_field(source: dict[str, Any], key: str, location: Path | str) -> str:
    if key not in source:
        raise ValueError(f"{location}: missing required field '{key}'")
    value = source[key]
    if not isinstance(value, str):
        raise ValueError(f"{location}: '{key}' must be string, got {type(value).__name__}")
    return value


def _required_int(source: dict[str, Any], key: str, location: Path | str) -> int:
    if key not in source:
        raise ValueError(f"{location}: missing required field '{key}'")
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{location}: '{key}' must be int, got {type(value).__name__}")
    return value
