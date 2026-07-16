"""PostgreSQL acceptance tests for the durable semantic-Q&A quota."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import SemanticQaAttempt
from bot.db.repos.semantic_quota import AttemptOutcome, SemanticQuotaRepo


async def _persist_delivery_intent(
    session: AsyncSession,
    *,
    attempt_id: int,
    outcome: AttemptOutcome,
    now: datetime,
) -> None:
    """Seed the pre-Telegram state when a repo test exercises finalize directly."""

    await session.execute(
        update(SemanticQaAttempt)
        .where(SemanticQaAttempt.id == attempt_id)
        .values(outcome=outcome, delivery_started_at=now)
    )
    await session.flush()


@pytest_asyncio.fixture()
async def concurrent_quota_sessions(postgres_engine) -> AsyncIterator[tuple[object, str]]:
    """Use unique committed rows for the real multi-connection race, then scrub only them.

    A PostgreSQL xact advisory-lock race cannot be exercised inside the single outer
    transaction used by ``db_session``: the lock is released only by commit/rollback.
    This fixture therefore commits unique test-owned rows across independent connections
    and removes exactly that prefix at teardown; it never truncates or resets shared data.
    """

    prefix = f"test-semantic-concurrency-{uuid.uuid4().hex}"
    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory, prefix
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                delete(SemanticQaAttempt).where(
                    SemanticQaAttempt.idempotency_key.like(f"{prefix}:%")
                )
            )
            await cleanup.commit()


@pytest.mark.asyncio
async def test_three_concurrent_reservations_admit_exactly_two(
    concurrent_quota_sessions,
) -> None:
    factory, prefix = concurrent_quota_sessions
    ready = asyncio.Event()
    waiting = 0
    waiting_lock = asyncio.Lock()
    user_tg_id = 8_040_400_001
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)

    async def reserve(index: int):
        nonlocal waiting
        async with waiting_lock:
            waiting += 1
            if waiting == 3:
                ready.set()
        await ready.wait()
        async with factory() as session:
            decision = await SemanticQuotaRepo.reserve(
                session,
                idempotency_key=f"{prefix}:{index}",
                user_tg_id=user_tg_id,
                chat_id=-100404,
                source_chat_message_id=None,
                now=now,
            )
            await session.commit()
            return decision

    decisions = await asyncio.gather(*(reserve(index) for index in range(3)))

    assert sum(decision.allowed for decision in decisions) == 2
    assert sorted(decision.used for decision in decisions) == [0, 1, 2]
    async with factory() as verification:
        rows = (
            (
                await verification.execute(
                    select(SemanticQaAttempt).where(
                        SemanticQaAttempt.idempotency_key.like(f"{prefix}:%")
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(row.status for row in rows) == ["denied", "reserved", "reserved"]
    assert sorted(row.slot_number for row in rows if row.slot_number is not None) == [1, 2]


@pytest.mark.asyncio
async def test_technical_failure_releases_slot_for_reuse(db_session) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    first = await SemanticQuotaRepo.reserve(
        db_session,
        idempotency_key="technical-release:first",
        user_tg_id=8_040_400_002,
        chat_id=-100404,
        source_chat_message_id=None,
        now=now,
    )
    await SemanticQuotaRepo.finalize(
        db_session,
        attempt_id=first.attempt_id,
        outcome="technical_failure",
    )
    replacement = await SemanticQuotaRepo.reserve(
        db_session,
        idempotency_key="technical-release:replacement",
        user_tg_id=8_040_400_002,
        chat_id=-100404,
        source_chat_message_id=None,
        now=now,
    )

    assert first.allowed is True
    assert replacement.allowed is True
    assert replacement.used == 0
    assert (
        await db_session.scalar(
            select(SemanticQaAttempt.status).where(SemanticQaAttempt.id == first.attempt_id)
        )
        == "released"
    )


@pytest.mark.asyncio
async def test_answered_and_abstained_both_consume_quota(db_session) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    outcomes: tuple[AttemptOutcome, ...] = ("answered", "abstained")
    for index, outcome in enumerate(outcomes, start=1):
        decision = await SemanticQuotaRepo.reserve(
            db_session,
            idempotency_key=f"consumed:{index}",
            user_tg_id=8_040_400_003,
            chat_id=-100404,
            source_chat_message_id=None,
            now=now,
        )
        assert decision.allowed is True
        await _persist_delivery_intent(
            db_session,
            attempt_id=decision.attempt_id,
            outcome=outcome,
            now=now,
        )
        await SemanticQuotaRepo.finalize(
            db_session,
            attempt_id=decision.attempt_id,
            outcome=outcome,
        )

    denied = await SemanticQuotaRepo.reserve(
        db_session,
        idempotency_key="consumed:third",
        user_tg_id=8_040_400_003,
        chat_id=-100404,
        source_chat_message_id=None,
        now=now,
    )

    assert denied.allowed is False
    assert denied.used == 2
    statuses = (
        await db_session.execute(
            select(SemanticQaAttempt.status, SemanticQaAttempt.outcome)
            .where(SemanticQaAttempt.user_tg_id == 8_040_400_003)
            .order_by(SemanticQaAttempt.id)
        )
    ).all()
    assert statuses == [
        ("consumed", "answered"),
        ("consumed", "abstained"),
        ("denied", "quota_denied"),
    ]


@pytest.mark.asyncio
async def test_reservation_idempotency_never_allocates_a_second_slot(db_session) -> None:
    async def reserve():
        return await SemanticQuotaRepo.reserve(
            db_session,
            idempotency_key="idempotent:one",
            user_tg_id=8_040_400_004,
            chat_id=-100404,
            source_chat_message_id=None,
            now=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
        )

    first = await reserve()
    repeated = await reserve()

    assert repeated.allowed is False
    assert repeated.replayed is True
    assert repeated.status == "reserved"
    assert repeated.attempt_id == first.attempt_id
    assert first.used == 0
    assert repeated.used == 1
    assert repeated.limit == first.limit
    assert repeated.resets_at == first.resets_at
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SemanticQaAttempt)
            .where(SemanticQaAttempt.idempotency_key == "idempotent:one")
        )
        == 1
    )

    await _persist_delivery_intent(
        db_session,
        attempt_id=first.attempt_id,
        outcome="answered",
        now=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
    )
    await SemanticQuotaRepo.finalize(
        db_session,
        attempt_id=first.attempt_id,
        outcome="answered",
    )
    finalized_replay = await reserve()
    assert finalized_replay.allowed is False
    assert finalized_replay.replayed is True
    assert finalized_replay.status == "consumed"
    assert finalized_replay.outcome == "answered"
    assert finalized_replay.attempt_id == first.attempt_id
    assert finalized_replay.used == 1


@pytest.mark.asyncio
async def test_next_admission_releases_stale_crashed_reservation(db_session) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    stale = SemanticQaAttempt(
        idempotency_key="stale:crashed",
        user_tg_id=8_040_400_005,
        chat_id=-100404,
        source_chat_message_id=None,
        local_day=now.date(),
        slot_number=1,
        status="reserved",
        outcome=None,
        reserved_at=now - timedelta(minutes=16),
    )
    db_session.add(stale)
    await db_session.flush()

    replacement = await SemanticQuotaRepo.reserve(
        db_session,
        idempotency_key="stale:replacement",
        user_tg_id=8_040_400_005,
        chat_id=-100404,
        source_chat_message_id=None,
        now=now,
    )

    assert replacement.allowed is True
    assert replacement.used == 0
    await db_session.refresh(stale)
    assert stale.status == "released"
    assert stale.outcome == "technical_failure"


@pytest.mark.asyncio
async def test_stale_delivery_intent_is_consumed_not_released(db_session) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    stale = SemanticQaAttempt(
        idempotency_key="stale:delivery-started",
        user_tg_id=8_040_400_006,
        chat_id=-100404,
        source_chat_message_id=None,
        local_day=now.date(),
        slot_number=1,
        status="reserved",
        outcome="answered",
        reserved_at=now - timedelta(minutes=16),
        delivery_started_at=now - timedelta(minutes=15, seconds=30),
    )
    db_session.add(stale)
    await db_session.flush()

    replacement = await SemanticQuotaRepo.reserve(
        db_session,
        idempotency_key="stale:delivery-replacement",
        user_tg_id=8_040_400_006,
        chat_id=-100404,
        source_chat_message_id=None,
        now=now,
    )

    await db_session.refresh(stale)
    assert stale.status == "consumed"
    assert stale.outcome == "answered"
    assert stale.finalized_at == now
    assert replacement.allowed is True
    assert replacement.used == 1
