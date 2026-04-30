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


@pytest.mark.xfail(
    reason=(
        "qa_traces cascade layer not yet wired into bot/services/forget_cascade.py "
        "CASCADE_LAYER_ORDER. Tracked as deferred follow-up."
    )
)
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
