from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

EVAL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "qa_eval_cases.json"


def _parse_dt(value: str | None) -> datetime:
    if value is None:
        return datetime.fromisoformat("2026-04-30T12:00:00+00:00")
    return datetime.fromisoformat(value)


async def _create_message(db_session, case_id: str, message: dict) -> None:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    user_id = int(message["user_id"])
    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"qa_eval_{user_id}",
        first_name=f"Eval {user_id}",
        last_name=None,
    )

    captured_at = _parse_dt(message.get("captured_at"))
    text = message.get("text", message.get("normalized_text"))
    caption = message.get("caption")
    content_hash = message.get("content_hash") or f"{case_id}-{message['message_id']}"
    is_redacted = bool(message.get("is_redacted", False))

    chat_message = ChatMessage(
        message_id=int(message["message_id"]),
        chat_id=int(message["chat_id"]),
        user_id=user_id,
        text=text,
        caption=caption,
        date=captured_at,
        memory_policy=message.get("memory_policy", "normal"),
        is_redacted=is_redacted,
        content_hash=content_hash,
    )
    db_session.add(chat_message)
    await db_session.flush()

    version = MessageVersion(
        chat_message_id=chat_message.id,
        version_seq=1,
        text=text,
        caption=caption,
        normalized_text=message.get("normalized_text"),
        content_hash=content_hash,
        is_redacted=is_redacted,
        captured_at=captured_at,
    )
    db_session.add(version)
    await db_session.flush()

    chat_message.current_version_id = version.id
    await db_session.flush()


async def _create_forget_event(db_session, event: dict) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    row = await ForgetEventRepo.create(
        db_session,
        target_type=event["target_type"],
        target_id=event.get("target_id"),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=event["tombstone_key"],
    )
    status = event.get("status", "pending")
    if status == "pending":
        return

    await ForgetEventRepo.mark_status(db_session, row.id, status="processing")
    if status == "completed":
        await ForgetEventRepo.mark_status(db_session, row.id, status="completed")


def _build_synthetic_td_export(case: dict) -> str:
    """Write a minimal Telegram Desktop export JSON to a temp file and return its path.

    Builds the envelope expected by the import parser from case["messages"] (which are
    already in TD export format) and case["chat_id"].
    """
    export = {
        "name": f"Eval Chat {case['id']}",
        "type": "private_supergroup",
        "id": case["chat_id"],
        "messages": case["messages"],
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix=f"eval_{case['id']}_"
    )
    json.dump(export, tmp)
    tmp.close()
    return tmp.name


async def _create_import_run(db_session, *, source_path: str, chat_id: int, source_hash: str) -> int:
    """Insert an ingestion_runs row for the import-path eval seed (mirrors test_import_apply.py)."""
    from sqlalchemy import text as sa_text

    result = await db_session.execute(
        sa_text(
            """
            INSERT INTO ingestion_runs (run_type, source_name, source_hash, status, config_json)
            VALUES ('import', :source_name, :source_hash, 'running', CAST(:cfg AS JSON))
            RETURNING id
            """
        ),
        {
            "source_name": source_path,
            "source_hash": source_hash,
            "cfg": json.dumps({"chat_id": chat_id}),
        },
    )
    run_id = result.scalar_one()
    await db_session.flush()
    return run_id


async def _seed_case_via_import_path(db_session, case: dict) -> None:
    """Drive the full import_apply pipeline for an imp_* case.

    Builds a synthetic Telegram Desktop export from case["messages"] and runs
    the full run_apply path. Asserts that non-offrecord messages have
    current_version_id set (CRITICAL 2 inverse-test).
    """
    from bot.services.import_apply import run_apply
    from bot.services.import_chunking import ChunkingConfig

    export_path = _build_synthetic_td_export(case)
    run_id = await _create_import_run(
        db_session,
        source_path=export_path,
        chat_id=case["chat_id"],
        source_hash=f"eval-{case['id']}",
    )
    chunking = ChunkingConfig(
        chunk_size=500,
        sleep_between_chunks_ms=0,
        use_advisory_lock=False,
    )
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=chunking,
        export_path=export_path,
    )
    assert report.error_count == 0, f"{case['id']}: import apply had errors: {report}"

    # Verify non-offrecord messages have current_version_id set (CRITICAL 2 inverse-test).
    from bot.db.models import ChatMessage
    from sqlalchemy import select

    for msg in case["messages"]:
        if msg.get("type") != "message":
            continue
        # Determine expected policy from text (detect_policy token match).
        text = msg.get("text", "")
        if isinstance(text, list):
            text = " ".join(
                part if isinstance(part, str) else part.get("text", "") for part in text
            )
        is_offrecord = "#offrecord" in text.lower()
        if is_offrecord:
            continue
        result = await db_session.execute(
            select(ChatMessage).where(
                ChatMessage.chat_id == case["chat_id"],
                ChatMessage.message_id == msg["id"],
            )
        )
        cm = result.scalar_one_or_none()
        assert cm is not None, (
            f"{case['id']}: message_id={msg['id']} not found after import"
        )
        assert cm.current_version_id is not None, (
            f"{case['id']}: message_id={msg['id']} current_version_id IS NULL — CRITICAL 2 regression"
        )


async def _populate_case(db_session, case: dict) -> int:
    case_id = case["id"]

    if case_id.startswith("imp_"):
        # Import-path cases: seed via run_apply, return chat_id from case.
        await _seed_case_via_import_path(db_session, case)
        for event in case.get("fixture_forget_events", []):
            await _create_forget_event(db_session, event)
        return int(case["chat_id"])

    # Legacy rec_* cases: seed via manual ORM rows.
    for message in case["fixture_messages"]:
        await _create_message(db_session, case_id, message)
    for event in case.get("fixture_forget_events", []):
        await _create_forget_event(db_session, event)
    return int(case["fixture_messages"][0]["chat_id"])


CASES = json.loads(EVAL_FIXTURE.read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["id"])
async def test_eval_case(case: dict, db_session) -> None:
    from bot.services.qa import run_qa

    chat_id = await _populate_case(db_session, case)
    result = await run_qa(
        db_session,
        query=case["query"],
        chat_id=chat_id,
        redact_query_in_audit=False,
    )

    expected_ids = case["expected_chat_message_ids"]
    if not case["expected_evidence_present"]:
        assert result.bundle.abstained is True, f"{case['id']}: expected abstention"
        assert result.bundle.items == ()
        return

    actual_ids = [item.message_id for item in result.bundle.items]
    assert actual_ids[: len(expected_ids)] == expected_ids, (
        f"{case['id']}: expected leading message_ids {expected_ids}, got {actual_ids}"
    )
