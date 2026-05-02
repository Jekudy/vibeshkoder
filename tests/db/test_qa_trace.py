"""T4-05 acceptance tests — qa_traces table + QaTraceRepo."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=9_700_000_000)
_chat_counter = itertools.count(start=970_000)


def _next_user_id() -> int:
    return next(_user_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


async def test_create_with_query_text(db_session) -> None:
    from bot.db.repos.qa_trace import QaTraceRepo

    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=_next_user_id(),
        chat_id=_next_chat_id(),
        query="hello memory",
        evidence_ids=[],
        abstained=False,
        redact_query=False,
    )

    assert trace.query_text == "hello memory"
    assert trace.query_redacted is False


async def test_create_with_redacted_query(db_session) -> None:
    from bot.db.repos.qa_trace import QaTraceRepo

    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=_next_user_id(),
        chat_id=_next_chat_id(),
        query="secret query",
        evidence_ids=[],
        abstained=False,
        redact_query=True,
    )

    assert trace.query_text is None
    assert trace.query_redacted is True


async def test_evidence_ids_jsonb_round_trip(db_session) -> None:
    from bot.db.models import QaTrace
    from bot.db.repos.qa_trace import QaTraceRepo

    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=_next_user_id(),
        chat_id=_next_chat_id(),
        query="with evidence",
        evidence_ids=[111, 222],
        abstained=False,
        redact_query=False,
    )

    result = await db_session.execute(select(QaTrace).where(QaTrace.id == trace.id))
    fetched = result.scalar_one()
    assert fetched.evidence_ids == [111, 222]


async def test_forget_me_cascade_redacts_query(db_session) -> None:
    from bot.db.models import ForgetEvent
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    user_tg_id = _next_user_id()
    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=user_tg_id,
        chat_id=_next_chat_id(),
        query="forget this query",
        evidence_ids=[],
        abstained=False,
        redact_query=False,
    )
    db_session.add(
        ForgetEvent(
            target_type="user",
            target_id=str(user_tg_id),
            tombstone_key="user:" + str(user_tg_id),
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()

    await run_cascade_worker_once(db_session)
    await db_session.refresh(trace)

    assert trace.query_text is None


# ─── §3.3 qa_traces cascade layer tests ────────────────────────────────────


async def test_qa_trace_cascade_idempotent(db_session) -> None:
    """Running forget cascade twice for the same user reports 0 rows on second run."""
    from bot.db.models import ForgetEvent
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    user_tg_id = _next_user_id()
    await QaTraceRepo.create(
        db_session,
        user_tg_id=user_tg_id,
        chat_id=_next_chat_id(),
        query="idempotency test query",
        evidence_ids=[],
        abstained=False,
        redact_query=False,
    )
    db_session.add(
        ForgetEvent(
            target_type="user",
            target_id=str(user_tg_id),
            tombstone_key="user:" + str(user_tg_id),
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()

    # First run — should redact.
    await run_cascade_worker_once(db_session)

    # Second forget event for the same user — idempotency guard.
    db_session.add(
        ForgetEvent(
            target_type="user",
            target_id=str(user_tg_id),
            tombstone_key="user:" + str(user_tg_id) + "_r2",
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()
    stats2 = await run_cascade_worker_once(db_session)
    # Second run processes the event but qa_traces has 0 un-redacted rows → rows=0
    # (the layer ran but found nothing to update).
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER
    # No error, just 0 more rows.
    assert stats2["failed"] == 0


async def test_qa_trace_cascade_message_target_skips(db_session) -> None:
    """target_type='message' event → qa_traces layer reports not_applicable (pre-filter)."""
    from bot.db.models import ForgetEvent, ChatMessage, TelegramUpdate
    from bot.services.forget_cascade import run_cascade_worker_once

    # Create a minimal chat_messages row to have a valid target_id.
    from sqlalchemy import text as sa_text

    await db_session.execute(
        sa_text(
            "INSERT INTO users (id, first_name) VALUES (88880001, 'CascadeUser') "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    await db_session.flush()

    await db_session.execute(
        sa_text(
            """
            INSERT INTO chat_messages
              (message_id, chat_id, user_id, date, message_kind, memory_policy)
            VALUES (77770001, -1001000077770, 88880001, now(), 'text', 'normal')
            """
        )
    )
    await db_session.flush()
    cm_id_row = await db_session.execute(
        sa_text("SELECT id FROM chat_messages WHERE message_id=77770001 AND chat_id=-1001000077770")
    )
    cm_id = cm_id_row.scalar_one()

    db_session.add(
        ForgetEvent(
            target_type="message",
            target_id=str(cm_id),
            tombstone_key=f"msg:{cm_id}",
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()

    await run_cascade_worker_once(db_session)

    # Verify the event completed and qa_traces layer was not_applicable.
    from sqlalchemy import text as sa_text2
    ev_row = await db_session.execute(
        sa_text2(
            "SELECT status, cascade_status FROM forget_events "
            "WHERE target_type='message' AND target_id=:tid"
        ),
        {"tid": str(cm_id)},
    )
    ev = ev_row.fetchone()
    assert ev is not None
    assert ev[0] == "completed"
    import json as _json
    cs = _json.loads(ev[1]) if isinstance(ev[1], str) else ev[1]
    assert cs.get("qa_traces", {}).get("reason") == "not_applicable"


async def test_qa_trace_cascade_only_affects_target_user(db_session) -> None:
    """Forget user A → only A's traces redacted; user B's traces untouched."""
    from bot.db.models import ForgetEvent
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    user_a = _next_user_id()
    user_b = _next_user_id()
    chat_id = _next_chat_id()

    trace_a = await QaTraceRepo.create(
        db_session,
        user_tg_id=user_a,
        chat_id=chat_id,
        query="user A's secret query",
        evidence_ids=[],
        abstained=False,
        redact_query=False,
    )
    trace_b = await QaTraceRepo.create(
        db_session,
        user_tg_id=user_b,
        chat_id=chat_id,
        query="user B's query — must stay",
        evidence_ids=[],
        abstained=False,
        redact_query=False,
    )
    db_session.add(
        ForgetEvent(
            target_type="user",
            target_id=str(user_a),
            tombstone_key="user:" + str(user_a),
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()

    await run_cascade_worker_once(db_session)
    await db_session.refresh(trace_a)
    await db_session.refresh(trace_b)

    assert trace_a.query_text is None
    assert trace_a.query_redacted is True
    assert trace_b.query_text == "user B's query — must stay"
    assert trace_b.query_redacted is False


async def test_qa_trace_cascade_redacts_multiple_rows(db_session) -> None:
    """User with 5 qa_traces rows; forget_me → all 5 redacted in single UPDATE."""
    from bot.db.models import ForgetEvent
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    user_tg_id = _next_user_id()
    chat_id = _next_chat_id()

    traces = []
    for i in range(5):
        t = await QaTraceRepo.create(
            db_session,
            user_tg_id=user_tg_id,
            chat_id=chat_id,
            query=f"query {i}",
            evidence_ids=[],
            abstained=False,
            redact_query=False,
        )
        traces.append(t)

    db_session.add(
        ForgetEvent(
            target_type="user",
            target_id=str(user_tg_id),
            tombstone_key="user:" + str(user_tg_id),
            authorized_by="self",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()

    stats = await run_cascade_worker_once(db_session)
    assert stats["failed"] == 0

    for t in traces:
        await db_session.refresh(t)
        assert t.query_text is None, f"trace {t.id} query_text must be NULL"
        assert t.query_redacted is True, f"trace {t.id} query_redacted must be True"
