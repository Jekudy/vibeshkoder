from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bot.services.eval_seeds import (
    QueryRow,
    canonical_jsonl_bytes,
    compute_seed_hash,
    load_seed_spec,
    resolve_expected_ids,
)


FIXTURE_DIR = Path("tests/fixtures/golden_recall/seed_v1")


def _base_messages() -> list[dict[str, Any]]:
    return [
        {
            "seed_local_id": "msg_03",
            "user_id_local": 703,
            "text": "Postgres FTS workshop",
            "ts": "2026-04-01T10:02:00+03:00",
            "message_kind": "text",
        },
        {
            "seed_local_id": "msg_07",
            "user_id_local": 706,
            "text": "Saturday breakfast",
            "ts": "2026-04-03T08:42:00+03:00",
            "message_kind": "text",
            "caption": None,
        },
    ]


def _base_queries() -> list[dict[str, Any]]:
    return [
        {
            "query_id": "q_01",
            "query": "When is the workshop?",
            "expected_message_version_ids": ["msg_03"],
            "expected_abstain": False,
        },
        {
            "query_id": "q_02",
            "query": "What was for breakfast?",
            "expected_message_version_ids": ["msg_07"],
            "expected_abstain": False,
        },
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


def _write_seed(
    tmp_path: Path,
    messages: list[dict[str, Any]],
    queries: list[dict[str, Any]],
) -> Path:
    seed_dir = tmp_path / "seed_v1"
    seed_dir.mkdir()
    _write_jsonl(seed_dir / "chat_history.jsonl", messages)
    _write_jsonl(seed_dir / "queries.jsonl", queries)
    return seed_dir


def _load_raw_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_load_seed_spec_happy_path_loads_real_fixture() -> None:
    spec = load_seed_spec(FIXTURE_DIR, seed_id="golden_recall", version=1)
    raw_messages = _load_raw_jsonl(FIXTURE_DIR / "chat_history.jsonl")

    assert spec.seed_id == "golden_recall"
    assert spec.version == 1
    assert len(spec.messages) == 24
    assert len(spec.queries) == 9
    assert spec.seed_hash == compute_seed_hash(canonical_jsonl_bytes(raw_messages))
    assert sum(query.expected_abstain for query in spec.queries) == 1


def test_seed_hash_determinism_for_fixture_and_key_order() -> None:
    first = load_seed_spec(FIXTURE_DIR, seed_id="golden_recall", version=1)
    second = load_seed_spec(FIXTURE_DIR, seed_id="golden_recall", version=1)
    raw_messages = _load_raw_jsonl(FIXTURE_DIR / "chat_history.jsonl")
    reordered_messages = [
        {key: row[key] for key in reversed(tuple(row.keys()))}
        for row in raw_messages
    ]

    assert first.seed_hash == second.seed_hash
    assert canonical_jsonl_bytes(raw_messages) == canonical_jsonl_bytes(reordered_messages)
    assert compute_seed_hash(canonical_jsonl_bytes(raw_messages)) == first.seed_hash


def test_canonical_jsonl_bytes_sorts_keys() -> None:
    assert canonical_jsonl_bytes([{"b": 1, "a": 2}]) == canonical_jsonl_bytes([{"a": 2, "b": 1}])


def test_resolve_expected_ids_preserves_order() -> None:
    query = QueryRow(
        query_id="q",
        query="question",
        expected_message_version_ids=("msg_03", "msg_07"),
        expected_abstain=False,
    )

    assert resolve_expected_ids(query, {"msg_03": 303, "msg_07": 707}) == [303, 707]


def test_resolve_expected_ids_raises_key_error_on_missing_id() -> None:
    query = QueryRow(
        query_id="q",
        query="question",
        expected_message_version_ids=("msg_03", "msg_missing"),
        expected_abstain=False,
    )

    with pytest.raises(KeyError):
        resolve_expected_ids(query, {"msg_03": 303})


def test_load_seed_spec_missing_seed_dir_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="seed directory not found"):
        load_seed_spec(tmp_path / "missing", seed_id="seed", version=1)


def test_load_seed_spec_missing_chat_history_raises_file_not_found(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed_v1"
    seed_dir.mkdir()
    _write_jsonl(seed_dir / "queries.jsonl", _base_queries())

    with pytest.raises(FileNotFoundError, match="chat_history\\.jsonl"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_missing_queries_raises_file_not_found(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed_v1"
    seed_dir.mkdir()
    _write_jsonl(seed_dir / "chat_history.jsonl", _base_messages())

    with pytest.raises(FileNotFoundError, match="queries\\.jsonl"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_duplicate_seed_local_id_raises_value_error(tmp_path: Path) -> None:
    messages = _base_messages()
    messages[1]["seed_local_id"] = "msg_03"
    seed_dir = _write_seed(tmp_path, messages, _base_queries())

    with pytest.raises(ValueError, match="duplicate seed_local_id"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_duplicate_query_id_raises_value_error(tmp_path: Path) -> None:
    queries = _base_queries()
    queries[1]["query_id"] = "q_01"
    seed_dir = _write_seed(tmp_path, _base_messages(), queries)

    with pytest.raises(ValueError, match="duplicate query_id"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_query_references_unknown_seed_local_id_raises_value_error(
    tmp_path: Path,
) -> None:
    queries = _base_queries()
    queries[0]["expected_message_version_ids"] = ["msg_missing"]
    seed_dir = _write_seed(tmp_path, _base_messages(), queries)

    with pytest.raises(ValueError, match="references unknown"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_abstain_with_expected_ids_raises_value_error(tmp_path: Path) -> None:
    queries = _base_queries()
    queries[0]["expected_abstain"] = True
    seed_dir = _write_seed(tmp_path, _base_messages(), queries)

    with pytest.raises(ValueError, match="expected_abstain=true"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_non_abstain_without_expected_ids_raises_value_error(
    tmp_path: Path,
) -> None:
    queries = _base_queries()
    queries[0]["expected_message_version_ids"] = []
    seed_dir = _write_seed(tmp_path, _base_messages(), queries)

    with pytest.raises(ValueError, match="expected_abstain=false"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_malformed_jsonl_raises_value_error(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed_v1"
    seed_dir.mkdir()
    (seed_dir / "chat_history.jsonl").write_text("{not-json}\n", encoding="utf-8")
    _write_jsonl(seed_dir / "queries.jsonl", _base_queries())

    with pytest.raises(ValueError, match="invalid JSON"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_jsonl_row_not_object_raises_value_error(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed_v1"
    seed_dir.mkdir()
    (seed_dir / "chat_history.jsonl").write_text("[]\n", encoding="utf-8")
    _write_jsonl(seed_dir / "queries.jsonl", _base_queries())

    with pytest.raises(ValueError, match="expected JSON object"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_version_zero_raises_value_error(tmp_path: Path) -> None:
    seed_dir = _write_seed(tmp_path, _base_messages(), _base_queries())

    with pytest.raises(ValueError, match="version must be >= 1"):
        load_seed_spec(seed_dir, seed_id="seed", version=0)


def test_load_seed_spec_empty_seed_id_raises_value_error(tmp_path: Path) -> None:
    seed_dir = _write_seed(tmp_path, _base_messages(), _base_queries())

    with pytest.raises(ValueError, match="seed_id must be non-empty"):
        load_seed_spec(seed_dir, seed_id="", version=1)


def test_load_seed_spec_missing_required_field_mentions_field_name(tmp_path: Path) -> None:
    messages = _base_messages()
    del messages[0]["text"]
    seed_dir = _write_seed(tmp_path, messages, _base_queries())

    with pytest.raises(ValueError, match="text"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_invalid_ts_format_mentions_ts(tmp_path: Path) -> None:
    messages = _base_messages()
    messages[0]["ts"] = "not-a-timestamp"
    seed_dir = _write_seed(tmp_path, messages, _base_queries())

    with pytest.raises(ValueError, match="ts"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_caption_non_null_non_string_raises_value_error(tmp_path: Path) -> None:
    messages = _base_messages()
    messages[0]["caption"] = 123
    seed_dir = _write_seed(tmp_path, messages, _base_queries())

    with pytest.raises(ValueError, match="caption"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_expected_message_version_ids_must_be_list_of_strings(
    tmp_path: Path,
) -> None:
    queries = _base_queries()
    queries[0]["expected_message_version_ids"] = ["msg_03", 7]
    seed_dir = _write_seed(tmp_path, _base_messages(), queries)

    with pytest.raises(ValueError, match="expected_message_version_ids"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)


def test_load_seed_spec_expected_abstain_must_be_bool(tmp_path: Path) -> None:
    queries = _base_queries()
    queries[0]["expected_abstain"] = "false"
    seed_dir = _write_seed(tmp_path, _base_messages(), queries)

    with pytest.raises(ValueError, match="expected_abstain"):
        load_seed_spec(seed_dir, seed_id="seed", version=1)
