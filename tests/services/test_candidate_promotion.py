"""Automatic extraction-candidate promotion on real PostgreSQL."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("app_env")

_ids = itertools.count(9_830_000_000)


def _next_id() -> int:
    return next(_ids)


async def _seed_candidate(
    session: AsyncSession,
    *,
    candidate_json: dict | None = None,
    payload_schema_version: str | None = "karpathy-wiki-v1",
) -> tuple[int, uuid.UUID, uuid.UUID]:
    from bot.db.models import (
        ChatMessage,
        ExtractionCandidate,
        ExtractionRun,
        MessageVersion,
    )
    from bot.db.repos.user import UserRepo

    actor_id = _next_id()
    await UserRepo.upsert(
        session,
        telegram_id=actor_id,
        username=f"auto_actor_{actor_id}",
        first_name="Auto",
        last_name=None,
    )
    now = datetime.now(timezone.utc)
    message = ChatMessage(
        message_id=_next_id(),
        chat_id=-_next_id(),
        user_id=actor_id,
        text="canonical source",
        date=now,
        created_at=now,
        memory_policy="normal",
        is_redacted=False,
    )
    session.add(message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text="canonical source",
        normalized_text="canonical source",
        entities_json={},
        content_hash=uuid.uuid4().hex,
        is_redacted=False,
    )
    session.add(version)
    await session.flush()
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == message.id)
        .values(current_version_id=version.id)
    )

    run = ExtractionRun(
        ingestion_window_start=now - timedelta(minutes=5),
        ingestion_window_end=now,
        candidate_count=1,
        run_status="completed",
    )
    session.add(run)
    await session.flush()
    candidate = ExtractionCandidate(
        extraction_run_id=run.id,
        candidate_json=candidate_json
        or {
            "topic_slug": f"topic-{actor_id}",
            "title": f"Card {actor_id}",
            "body_markdown": "Canonical body",
            "tags": ["canonical"],
        },
        source_message_version_ids=[version.id],
        status="pending",
        payload_schema_version=payload_schema_version,
    )
    session.add(candidate)
    await session.flush()
    return actor_id, run.id, candidate.id


async def test_promote_candidate_writes_card_sources_and_decision_atomically(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import (
        CardSource,
        ExtractionCandidate,
        ExtractionDecision,
        KnowledgeCard,
    )
    from bot.services.candidate_promotion import promote_candidate

    actor_id, _, candidate_id = await _seed_candidate(db_session)

    result = await promote_candidate(
        db_session,
        candidate_id=candidate_id,
        actor_user_id=actor_id,
    )

    assert result.status == "promoted"
    assert result.card_id is not None
    candidate = await db_session.get(ExtractionCandidate, candidate_id)
    card = await db_session.get(KnowledgeCard, result.card_id)
    assert candidate is not None and candidate.status == "approved"
    assert card is not None
    assert card.topic_slug == f"topic-{actor_id}"
    assert card.card_status == "approved"
    assert card.approved_by_user_id == actor_id
    assert (
        await db_session.scalar(
            select(func.count(CardSource.id)).where(CardSource.card_id == card.id)
        )
        == 1
    )
    decision = await db_session.scalar(
        select(ExtractionDecision).where(ExtractionDecision.candidate_id == candidate_id)
    )
    assert decision is not None
    assert decision.action == "approved"
    assert decision.decided_by == actor_id
    assert decision.decided_by_username == f"auto_actor_{actor_id}"


async def test_promotion_rejects_missing_actor_and_preserves_legacy_candidate(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import ExtractionCandidate, KnowledgeCard
    from bot.services.candidate_promotion import (
        ActorUserNotFoundError,
        promote_candidate,
    )

    actor_id, _, candidate_id = await _seed_candidate(
        db_session,
        candidate_json={
            "title": "Legacy summary",
            "summary": "This old payload is not canonical",
            "tags": [],
        },
        payload_schema_version=None,
    )

    with pytest.raises(ActorUserNotFoundError):
        await promote_candidate(
            db_session,
            candidate_id=candidate_id,
            actor_user_id=actor_id + 999_999,
        )

    result = await promote_candidate(
        db_session,
        candidate_id=candidate_id,
        actor_user_id=actor_id,
    )
    assert result.status == "blocked"
    assert result.reason == "legacy_candidate_requires_reextract"
    candidate = await db_session.get(ExtractionCandidate, candidate_id)
    assert candidate is not None and candidate.status == "pending"
    assert candidate.reviewed_by is None
    assert candidate.reviewed_at is None
    assert (
        await db_session.scalar(
            select(func.count(KnowledgeCard.id)).where(
                KnowledgeCard.approved_by_user_id == actor_id
            )
        )
        == 0
    )


async def test_governance_block_is_terminal_superseded(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import ExtractionCandidate, MessageVersion
    from bot.services.candidate_promotion import promote_candidate

    actor_id, _, candidate_id = await _seed_candidate(db_session)
    candidate = await db_session.get(ExtractionCandidate, candidate_id)
    assert candidate is not None
    source_mvid = int(candidate.source_message_version_ids[0])
    await db_session.execute(
        update(MessageVersion).where(MessageVersion.id == source_mvid).values(is_redacted=True)
    )

    result = await promote_candidate(
        db_session,
        candidate_id=candidate_id,
        actor_user_id=actor_id,
    )

    assert result.status == "blocked"
    assert result.reason == "source_redacted"
    await db_session.refresh(candidate)
    assert candidate.status == "superseded"


async def test_promotion_supersedes_candidate_after_source_edit(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import ChatMessage, ExtractionCandidate, MessageVersion
    from bot.services.candidate_promotion import promote_candidate

    actor_id, _, candidate_id = await _seed_candidate(db_session)
    candidate = await db_session.get(ExtractionCandidate, candidate_id)
    assert candidate is not None
    stale_mvid = int(candidate.source_message_version_ids[0])
    stale = await db_session.get(MessageVersion, stale_mvid)
    assert stale is not None
    replacement = MessageVersion(
        chat_message_id=stale.chat_message_id,
        version_seq=stale.version_seq + 1,
        text="edited canonical source",
        normalized_text="edited canonical source",
        entities_json={},
        content_hash=uuid.uuid4().hex,
        is_redacted=False,
    )
    db_session.add(replacement)
    await db_session.flush()
    await db_session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == stale.chat_message_id)
        .values(current_version_id=replacement.id)
    )

    result = await promote_candidate(
        db_session,
        candidate_id=candidate_id,
        actor_user_id=actor_id,
    )

    assert result.status == "blocked"
    assert result.reason == "source_not_current"
    await db_session.refresh(candidate)
    assert candidate.status == "superseded"


async def test_promote_run_candidates_exposes_fully_automatic_batch(
    db_session: AsyncSession,
) -> None:
    from bot.services.candidate_promotion import promote_run_candidates

    actor_id, run_id, _ = await _seed_candidate(db_session)

    results = await promote_run_candidates(
        db_session,
        extraction_run_id=run_id,
        actor_user_id=actor_id,
    )

    assert len(results) == 1
    assert results[0].status == "promoted"


async def test_promote_pending_candidates_is_bounded_recovery_api(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import ExtractionCandidate
    from bot.services.candidate_promotion import promote_pending_candidates

    actor_id, _, candidate_id = await _seed_candidate(db_session)
    await db_session.execute(
        update(ExtractionCandidate)
        .where(ExtractionCandidate.id == candidate_id)
        .values(created_at=datetime(1900, 1, 1, tzinfo=timezone.utc))
    )
    with pytest.raises(ValueError, match="limit"):
        await promote_pending_candidates(
            db_session,
            actor_user_id=actor_id,
            limit=0,
        )
    with pytest.raises(ValueError, match="limit"):
        await promote_pending_candidates(
            db_session,
            actor_user_id=actor_id,
            limit=True,
        )

    results = await promote_pending_candidates(
        db_session,
        actor_user_id=actor_id,
        limit=1,
    )
    assert len(results) == 1
    assert results[0].status == "promoted"


async def test_recovery_terminalizes_malformed_oldest_then_promotes_next(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import ExtractionCandidate
    from bot.services.candidate_promotion import promote_pending_candidates

    actor_id, _, malformed_id = await _seed_candidate(
        db_session,
        candidate_json={"title": "Old", "summary": "Legacy", "tags": []},
    )
    _, _, valid_id = await _seed_candidate(db_session)
    await db_session.execute(
        update(ExtractionCandidate)
        .where(ExtractionCandidate.id == malformed_id)
        .values(created_at=datetime(1800, 1, 1, tzinfo=timezone.utc))
    )
    await db_session.execute(
        update(ExtractionCandidate)
        .where(ExtractionCandidate.id == valid_id)
        .values(created_at=datetime(1800, 1, 2, tzinfo=timezone.utc))
    )

    first = await promote_pending_candidates(
        db_session,
        actor_user_id=actor_id,
        limit=1,
    )
    second = await promote_pending_candidates(
        db_session,
        actor_user_id=actor_id,
        limit=1,
    )

    assert [(item.candidate_id, item.reason) for item in first] == [
        (malformed_id, "invalid_candidate_json")
    ]
    assert len(second) == 1
    assert second[0].candidate_id == valid_id
    assert second[0].status == "promoted"
    malformed = await db_session.get(ExtractionCandidate, malformed_id)
    assert malformed is not None and malformed.status == "superseded"


async def test_recovery_preflight_blocks_all_work_when_legacy_pending_exists(
    db_session: AsyncSession,
) -> None:
    from bot.db.models import ExtractionCandidate
    from bot.services.candidate_promotion import (
        LegacyPendingCandidatesError,
        promote_pending_candidates,
    )

    actor_id, _, legacy_id = await _seed_candidate(
        db_session,
        candidate_json={"title": "Legacy", "summary": "Old", "tags": []},
        payload_schema_version=None,
    )
    _, _, current_id = await _seed_candidate(db_session)

    with pytest.raises(LegacyPendingCandidatesError) as caught:
        await promote_pending_candidates(
            db_session,
            actor_user_id=actor_id,
            limit=100,
        )

    assert caught.value.count == 1
    legacy = await db_session.get(ExtractionCandidate, legacy_id)
    current = await db_session.get(ExtractionCandidate, current_id)
    assert legacy is not None and legacy.status == "pending"
    assert current is not None and current.status == "pending"


async def test_concurrent_promotion_creates_exactly_one_card_and_decision(
    postgres_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent transactions serialize and only one performs writes."""
    from bot.db.models import (
        CardSource,
        ExtractionCandidate,
        ExtractionDecision,
        KnowledgeCard,
        MessageVersion,
    )
    from bot.services import candidate_promotion

    promote_candidate = candidate_promotion.promote_candidate

    # Make the stale-identity-map race deterministic: both sessions must load
    # the pending candidate before either one acquires the source advisory
    # lock. The loser then waits for the winner's commit before its locking
    # SELECT reads the approved row from PostgreSQL.
    original_acquire_source_locks = candidate_promotion._acquire_source_locks
    both_initial_reads_done = asyncio.Event()
    arrival_guard = asyncio.Lock()
    arrival_count = 0

    async def _acquire_source_locks_after_both_reads(session, source_ids):
        nonlocal arrival_count
        async with arrival_guard:
            arrival_count += 1
            if arrival_count == 2:
                both_initial_reads_done.set()
        await asyncio.wait_for(both_initial_reads_done.wait(), timeout=5)
        await original_acquire_source_locks(session, source_ids)

    monkeypatch.setattr(
        candidate_promotion,
        "_acquire_source_locks",
        _acquire_source_locks_after_both_reads,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as setup:
        async with setup.begin():
            actor_id, run_id, candidate_id = await _seed_candidate(setup)
            candidate = await setup.get(ExtractionCandidate, candidate_id)
            assert candidate is not None
            source_mvid = int(candidate.source_message_version_ids[0])
            source_version = await setup.get(MessageVersion, source_mvid)
            assert source_version is not None
            chat_message_id = source_version.chat_message_id

    async def _worker():
        async with factory() as session:
            async with session.begin():
                return await promote_candidate(
                    session,
                    candidate_id=candidate_id,
                    actor_user_id=actor_id,
                )

    try:
        first, second = await asyncio.gather(_worker(), _worker())
        assert {first.status, second.status} == {"promoted", "already_promoted"}
        async with factory() as verify:
            assert (
                await verify.scalar(
                    select(func.count(ExtractionDecision.id)).where(
                        ExtractionDecision.candidate_id == candidate_id
                    )
                )
                == 1
            )
            card_ids = list(
                (
                    await verify.scalars(
                        select(CardSource.card_id).where(
                            CardSource.message_version_id == source_mvid
                        )
                    )
                ).all()
            )
            assert len(card_ids) == 1
            assert await verify.get(KnowledgeCard, card_ids[0]) is not None
    finally:
        async with factory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    text(
                        "DELETE FROM knowledge_cards WHERE id IN "
                        "(SELECT card_id FROM card_sources WHERE message_version_id=:mvid)"
                    ),
                    {"mvid": source_mvid},
                )
                await cleanup.execute(
                    text("DELETE FROM extraction_candidates WHERE id=:candidate_id"),
                    {"candidate_id": candidate_id},
                )
                await cleanup.execute(
                    text("DELETE FROM extraction_runs WHERE id=:run_id"),
                    {"run_id": run_id},
                )
                await cleanup.execute(
                    text("DELETE FROM message_versions WHERE id=:mvid"),
                    {"mvid": source_mvid},
                )
                await cleanup.execute(
                    text("DELETE FROM chat_messages WHERE id=:cmid"),
                    {"cmid": chat_message_id},
                )
                await cleanup.execute(
                    text("DELETE FROM users WHERE id=:actor_id"),
                    {"actor_id": actor_id},
                )
