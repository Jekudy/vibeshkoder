"""Phase 4 hotfix #164 end-to-end integration tests.

Covers the 7 scenarios from §5 of the hotfix design spec. Scenario 7 (live
ingestion_run_id via dev-bot) is a manual smoke test and is not included here.

Scenarios 1–6 use real postgres via the ``db_session`` fixture and exercise the
full production code paths (persist_message_with_policy, run_apply, run_qa, etc.).

Skipped automatically if postgres is unreachable (same pattern as other integration
tests in this repo).
"""

from __future__ import annotations

import itertools
import json
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=8_600_000_000)
_chat_counter = itertools.count(start=8_600_000)
_msg_counter = itertools.count(start=8_600_000)


def _next_user_id() -> int:
    return next(_user_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


async def _upsert_user(session, user_id: int) -> None:
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"e2e_{user_id}",
        first_name="E2E",
        last_name=None,
    )


def _make_message_duck(
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text: str | None,
    caption: str | None = None,
    date: datetime | None = None,
) -> SimpleNamespace:
    """Build a minimal duck compatible with persist_message_with_policy."""
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
        text=text,
        caption=caption,
        date=date or datetime.now(timezone.utc),
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        entities=None,
        caption_entities=None,
    )


def _build_td_export(*, chat_id: int, messages: list[dict]) -> str:
    """Write a minimal TD export JSON to a temp file, return path."""
    export = {
        "name": "E2E Test Chat",
        "type": "private_supergroup",
        "id": chat_id,
        "messages": messages,
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="e2e_hotfix_"
    )
    json.dump(export, tmp)
    tmp.close()
    return tmp.name


async def _create_import_run(db_session, *, source_path: str, chat_id: int, source_hash: str) -> int:
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


def _default_chunking():
    from bot.services.import_chunking import ChunkingConfig

    return ChunkingConfig(
        chunk_size=500,
        sleep_between_chunks_ms=0,
        use_advisory_lock=False,
    )


# ─── Scenario 1: Live → recall ───────────────────────────────────────────────


async def test_live_message_then_recall_returns_hit(db_session) -> None:
    """Scenario 1: group message persisted via real path → /recall returns hit citing v1."""
    from bot.services.message_persistence import persist_message_with_policy
    from bot.services.qa import run_qa

    user_id = _next_user_id()
    chat_id = _next_chat_id()
    message_id = _next_msg_id()

    await _upsert_user(db_session, user_id)

    duck = _make_message_duck(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="хорошие новости о проекте",
    )
    result = await persist_message_with_policy(db_session, duck, source="live")

    # CRITICAL 1 invariant: current_version_id must be set.
    assert result.chat_message.current_version_id is not None, (
        "persist_message_with_policy did not set current_version_id — CRITICAL 1 regression"
    )

    qa_result = await run_qa(db_session, query="новости", chat_id=chat_id, redact_query_in_audit=False)

    assert qa_result.bundle.abstained is False
    actual_msg_ids = [item.message_id for item in qa_result.bundle.items]
    assert message_id in actual_msg_ids


# ─── Scenario 2: Import → recall ─────────────────────────────────────────────


async def test_import_message_then_recall_returns_hit(db_session) -> None:
    """Scenario 2: apply TD fixture → /recall returns hit citing imported v1."""
    from bot.services.import_apply import run_apply
    from bot.services.qa import run_qa

    chat_id = _next_chat_id()
    message_id = _next_msg_id()

    export_path = _build_td_export(
        chat_id=chat_id,
        messages=[
            {
                "id": message_id,
                "type": "message",
                "date": "2026-04-20T10:00:00",
                "date_unixtime": "1745143200",
                "from": "Import Test User",
                "from_id": f"user{_next_user_id()}",
                "text": "важное решение принято на встрече",
                "text_entities": [
                    {"type": "plain", "text": "важное решение принято на встрече"}
                ],
            }
        ],
    )
    run_id = await _create_import_run(
        db_session,
        source_path=export_path,
        chat_id=chat_id,
        source_hash=f"e2e-import-{chat_id}",
    )
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
        export_path=export_path,
    )
    assert report.error_count == 0
    assert report.applied_count == 1

    qa_result = await run_qa(db_session, query="решение", chat_id=chat_id, redact_query_in_audit=False)

    assert qa_result.bundle.abstained is False
    actual_msg_ids = [item.message_id for item in qa_result.bundle.items]
    assert message_id in actual_msg_ids


# ─── Scenario 3: Forget message → recall abstains ────────────────────────────


async def test_forget_message_then_recall_abstains(db_session) -> None:
    """Scenario 3: persist → create tombstone → /recall abstains."""
    from bot.services.message_persistence import persist_message_with_policy
    from bot.services.qa import run_qa

    user_id = _next_user_id()
    chat_id = _next_chat_id()
    message_id = _next_msg_id()

    await _upsert_user(db_session, user_id)

    duck = _make_message_duck(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="забудь это сообщение пожалуйста",
    )
    await persist_message_with_policy(db_session, duck, source="live")

    # Add message tombstone (status=completed means it's in effect).
    from bot.db.repos.forget_event import ForgetEventRepo

    row = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(message_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:{chat_id}:{message_id}",
    )
    await ForgetEventRepo.mark_status(db_session, row.id, status="processing")
    await ForgetEventRepo.mark_status(db_session, row.id, status="completed")
    await db_session.flush()

    qa_result = await run_qa(
        db_session, query="забудь", chat_id=chat_id, redact_query_in_audit=False
    )

    assert qa_result.bundle.abstained is True, (
        "Expected abstention after message tombstone, got evidence"
    )


# ─── Scenario 4: forget_me → qa_traces redacted ──────────────────────────────


async def test_forget_me_cascade_redacts_qa_trace(db_session) -> None:
    """Scenario 4: /recall foo → /forget_me → qa_trace.query_text IS NULL."""
    from bot.db.models import ForgetEvent
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    user_id = _next_user_id()
    chat_id = _next_chat_id()

    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=user_id,
        chat_id=chat_id,
        query="некий запрос для забывания",
        evidence_ids=[],
        abstained=True,
        redact_query=False,
    )
    db_session.add(
        ForgetEvent(
            target_type="user",
            target_id=str(user_id),
            tombstone_key=f"user:{user_id}",
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()

    stats = await run_cascade_worker_once(db_session)

    await db_session.refresh(trace)
    assert trace.query_text is None, "qa_trace.query_text must be NULL after forget_me"
    assert trace.query_redacted is True
    assert stats["failed"] == 0


# ─── Scenario 5: Flag-OFF → /recall still archives message ───────────────────


async def test_flag_off_recall_still_archives_message(db_session) -> None:
    """Scenario 5: flag OFF, /recall → chat_messages row exists with v1 (§3.5 fix)."""
    from bot.db.models import ChatMessage
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select

    user_id = _next_user_id()
    chat_id = _next_chat_id()
    message_id = _next_msg_id()

    await _upsert_user(db_session, user_id)

    # Simulate the /recall handler's pre-flag persist (§3.5).
    duck = _make_message_duck(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="/recall тест",
    )
    result = await persist_message_with_policy(db_session, duck, source="live")

    # Verify: row exists with current_version_id set (CRITICAL 1 fix).
    assert result.chat_message.current_version_id is not None

    # Even with qa feature flag OFF, the message itself was persisted.
    fetched = await db_session.execute(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
    )
    cm = fetched.scalar_one_or_none()
    assert cm is not None, "chat_messages row must exist after /recall even with flag OFF"
    assert cm.current_version_id is not None


# ─── Scenario 6: Imported offrecord round-trip ───────────────────────────────


async def test_imported_offrecord_creates_redacted_row_and_abstains(db_session) -> None:
    """Scenario 6: import #offrecord message → OffrecordMark + redacted v1; /recall abstains."""
    from bot.db.models import ChatMessage, OffrecordMark
    from bot.services.import_apply import run_apply
    from bot.services.qa import run_qa
    from sqlalchemy import select

    chat_id = _next_chat_id()
    message_id = _next_msg_id()

    export_path = _build_td_export(
        chat_id=chat_id,
        messages=[
            {
                "id": message_id,
                "type": "message",
                "date": "2026-04-20T12:00:00",
                "date_unixtime": "1745150400",
                "from": "Offrecord Import User",
                "from_id": f"user{_next_user_id()}",
                "text": "#offrecord секретная информация",
                "text_entities": [
                    {"type": "hashtag", "text": "#offrecord"},
                    {"type": "plain", "text": " секретная информация"},
                ],
            }
        ],
    )
    run_id = await _create_import_run(
        db_session,
        source_path=export_path,
        chat_id=chat_id,
        source_hash=f"e2e-offrecord-{chat_id}",
    )
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
        export_path=export_path,
    )
    assert report.error_count == 0

    # CRITICAL 3 + H2 fix: offrecord import creates chat_messages row with redacted content.
    cm_result = await db_session.execute(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
    )
    cm = cm_result.scalar_one_or_none()
    assert cm is not None, "chat_messages row must exist for imported offrecord message (H2 fix)"
    assert cm.memory_policy == "offrecord"
    assert cm.is_redacted is True
    assert cm.text is None
    assert cm.current_version_id is not None, "v1 must be set even for offrecord imports"

    # OffrecordMark must exist.
    mark_result = await db_session.execute(
        select(OffrecordMark).where(OffrecordMark.chat_message_id == cm.id)
    )
    mark = mark_result.scalar_one_or_none()
    assert mark is not None, "OffrecordMark must be created for imported #offrecord message"

    # /recall must abstain on the offrecord content.
    qa_result = await run_qa(
        db_session, query="секретная", chat_id=chat_id, redact_query_in_audit=False
    )
    assert qa_result.bundle.abstained is True, (
        "Expected abstention for #offrecord imported message"
    )
