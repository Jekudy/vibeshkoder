"""Unit tests for the no-content semantic rollout CLI boundary."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import backfill_semantic_index as cli


def test_parser_requires_explicit_backfill_scope() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["backfill"])

    args = parser.parse_args(["backfill", "--all-chats"])
    assert args.all_chats is True
    assert args.chat_id is None
    assert args.batch_size == 64


def test_top_level_help_renders_without_argparse_percent_expansion(capsys) -> None:
    parser = cli.build_parser()

    parser.print_help()

    assert "backfill" in capsys.readouterr().out


def test_load_shadow_cases_accepts_only_bounded_opaque_metadata(tmp_path) -> None:
    raw_question = "Кто обсуждал распределённые транзакции?"
    source = tmp_path / "private.jsonl"
    source.write_text(
        json.dumps(
            {
                "question_id": "semantic-001",
                "chat_id": -100123,
                "query": raw_question,
                "exclude_chat_message_id": 42,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    cases = cli._load_shadow_cases(source, max_queries=1)

    assert cases == (
        cli.ShadowCase(
            question_id="semantic-001",
            chat_id=-100123,
            query=raw_question,
            exclude_chat_message_id=42,
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"question_id": "raw question with spaces", "chat_id": 1, "query": "x"},
        {"question_id": "q1", "chat_id": True, "query": "x"},
        {"question_id": "q1", "chat_id": 1, "query": "   "},
        {"question_id": "q1", "chat_id": 1, "query": "x", "snippet": "forbidden"},
    ],
)
def test_shadow_case_validation_fails_closed(payload) -> None:
    with pytest.raises(ValueError):
        cli._parse_shadow_case(payload, line_number=7)


def test_shadow_file_is_validated_before_any_provider_work(tmp_path) -> None:
    source = tmp_path / "private.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"question_id": "q1", "chat_id": 1, "query": "first"}),
                json.dumps({"question_id": "q2", "chat_id": 1, "query": "second"}),
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no provider calls were made"):
        cli._load_shadow_cases(source, max_queries=1)


def test_private_report_is_mode_0600_and_exclusive_by_default(tmp_path) -> None:
    report = tmp_path / "report.json"
    payload = {"status": "pass", "query_sha256": "a" * 64}

    cli._write_private_report(report, payload, overwrite=False)

    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert json.loads(report.read_text(encoding="utf-8")) == payload
    with pytest.raises(FileExistsError):
        cli._write_private_report(report, payload, overwrite=False)

    report.chmod(0o644)
    cli._write_private_report(report, payload, overwrite=True)
    assert stat.S_IMODE(report.stat().st_mode) == 0o600


def test_emit_does_not_print_success_when_report_write_fails(tmp_path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text("already exists", encoding="utf-8")

    with pytest.raises(FileExistsError):
        cli._emit_report(
            {"status": "pass"},
            report_path=str(report),
            overwrite=False,
        )

    assert capsys.readouterr().out == ""


def test_ranked_keys_are_branch_specific_and_bounded() -> None:
    ranks = {
        "message:3": {"fts": 2, "vector": 1},
        "message:1": {"fts": 1},
        "card:abc": {"vector": 2},
    }

    assert cli._ranked_keys(ranks, branch="fts", limit=2) == ["message:1", "message:3"]
    assert cli._ranked_keys(ranks, branch="vector", limit=1) == ["message:3"]


def test_shadow_failure_payload_contains_only_bounded_error_class() -> None:
    secret = "provider response must not leak"

    payload = cli._shadow_failure_payload(processed=3, exc=RuntimeError(secret))

    rendered = json.dumps(payload)
    assert payload["reason_counts"] == {"failed:RuntimeError": 1}
    assert secret not in rendered
    assert payload["contains_raw_text"] is False
    assert payload["synthesis_called"] is False


@pytest.mark.parametrize("chat_id", [None, -1001234567890])
async def test_coverage_audit_fails_closed_when_scope_has_no_eligible_identities(
    monkeypatch,
    chat_id,
) -> None:
    monkeypatch.setattr(cli, "_eligible_identities", AsyncMock(return_value={}))
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))
        ),
    )

    report = await cli._coverage_report(
        session,
        chat_id=chat_id,
        model="text-embedding-3-small",
        batch_size=64,
    )

    assert report["status"] == "fail"
    assert report["eligible"] == 0
    assert report["coverage_percent"] == 0.0
    assert report["reason_counts"]["failed:no_eligible_identities"] == 1


@pytest.mark.parametrize("actual_sources", [(), (11,), (12, 11)])
async def test_coverage_audit_rejects_missing_partial_or_reordered_provenance(
    monkeypatch,
    actual_sources,
) -> None:
    identity = ("card", "card-id", "revision", 0, 1, -100404, "a" * 64, "model")
    monkeypatch.setattr(
        cli,
        "_eligible_identities",
        AsyncMock(return_value={identity: (11, 12)}),
    )
    row = {
        "source_type": "card",
        "source_id": "card-id",
        "source_revision": "revision",
        "chunk_index": 0,
        "chunk_count": 1,
        "chat_id": -100404,
        "content_hash": "a" * 64,
        "embedding_model": "model",
        "invalidated_at": None,
        "message_version_ids": actual_sources,
    }
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [row]))
        )
    )

    report = await cli._coverage_report(
        session,
        chat_id=-100404,
        model="model",
        batch_size=64,
    )

    assert report["status"] == "fail"
    assert report["indexed"] == 0
    assert report["reason_counts"]["failed:provenance_mismatch"] == 1


def test_main_redacts_validation_error_details(capsys, tmp_path) -> None:
    raw_question = "секретный вопрос нельзя печатать"
    source = tmp_path / "private.jsonl"
    source.write_text(
        json.dumps({"question_id": raw_question, "chat_id": 1, "query": raw_question}),
        encoding="utf-8",
    )

    result = cli.main(
        [
            "shadow",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "result.jsonl"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert raw_question not in captured.out
    assert raw_question not in captured.err
    assert json.loads(captured.err)["error_class"] == "ValueError"


@pytest.mark.asyncio
async def test_backfill_cli_emits_service_reason_counts_without_relabeling(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("DEV_MODE", "true")
    import bot.db.engine
    import bot.services.llm_gateway
    import bot.services.semantic_index

    session = SimpleNamespace()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    reasons = {
        "indexed:new_embedding": 2,
        "indexed:reused_embedding": 1,
        "skipped:unchanged": 3,
        "skipped:governance_race": 1,
        "invalidated:ineligible": 2,
    }
    monkeypatch.setattr(bot.db.engine, "async_session", SessionContext)
    monkeypatch.setattr(
        bot.services.llm_gateway,
        "load_embedding_gateway_config",
        lambda: SimpleNamespace(model="text-embedding-3-small", dimensions=1536),
    )
    monkeypatch.setattr(
        bot.services.semantic_index,
        "backfill_semantic_index",
        AsyncMock(
            return_value=SimpleNamespace(
                run_id=404,
                eligible=7,
                indexed=3,
                skipped=4,
                failed=0,
                reason_counts=reasons,
            )
        ),
    )
    monkeypatch.setattr(
        cli,
        "_coverage_report",
        AsyncMock(
            return_value={
                "status": "pass",
                "eligible": 7,
                "indexed": 7,
                "coverage_percent": 100.0,
                "missing": 0,
                "unexpected_active": 0,
                "reason_counts": {"indexed:active_identity": 7},
            }
        ),
    )

    exit_code = await cli._run_backfill(
        SimpleNamespace(
            chat_id=-100404,
            all_chats=False,
            batch_size=64,
            report=None,
            overwrite=False,
        )
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["reason_counts"] == reasons
