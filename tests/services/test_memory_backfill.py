"""Bounded historical extraction backfill on real PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("app_env")


@dataclass
class _FakeGateway:
    provider_name: str
    fail_on_call: int | None = None
    emit_candidate: bool = False
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    @property
    def extraction_provider(self) -> str:
        return self.provider_name

    @property
    def extraction_model(self) -> str:
        return "memory-backfill-fake"

    async def extract_candidates(
        self,
        session: AsyncSession,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        del prompt_template_version
        self.calls.append(list(source_versions))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("synthetic provider failure")

        from bot.db.models import LlmUsageLedger

        ledger = LlmUsageLedger(
            provider=self.provider_name,
            model="memory-backfill-fake",
            tokens_in=0,
            tokens_out=0,
        )
        session.add(ledger)
        await session.flush()
        candidates: list[dict[str, Any]] = []
        if self.emit_candidate:
            source_id = int(source_versions[0]["message_version_id"])
            candidates.append(
                {
                    "candidate_json": {
                        "topic_slug": f"backfill-{source_id}",
                        "title": "Historical card",
                        "body_markdown": "Historical fact",
                        "tags": ["history"],
                    },
                    "source_message_version_ids": [source_id],
                }
            )
        return {"candidates": candidates, "llm_usage_ledger_id": ledger.id}


@dataclass
class _PromotionSpy:
    calls: list[tuple[uuid.UUID, int]] = field(default_factory=list)

    async def __call__(
        self,
        session: AsyncSession,
        *,
        extraction_run_id: uuid.UUID,
        actor_user_id: int,
    ) -> list[Any]:
        # Exercise the same transaction instead of being a pure in-memory mock.
        from bot.db.models import ExtractionRun

        assert await session.get(ExtractionRun, extraction_run_id) is not None
        self.calls.append((extraction_run_id, actor_user_id))
        return []


@dataclass
class _SameTransactionGatewayError:
    """Mirror the live gateway: flush the ledger row, then return an error."""

    provider_name: str
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    @property
    def extraction_provider(self) -> str:
        return self.provider_name

    @property
    def extraction_model(self) -> str:
        return "memory-backfill-failing-fake"

    async def extract_candidates(
        self,
        session: AsyncSession,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        del prompt_template_version
        from bot.db.models import LlmUsageLedger

        self.calls.append(list(source_versions))
        ledger = LlmUsageLedger(
            provider=self.provider_name,
            model="memory-backfill-failing-fake",
            tokens_in=0,
            tokens_out=0,
            error="provider_transient:timeout",
        )
        session.add(ledger)
        await session.flush()
        return {
            "candidates": [],
            "llm_usage_ledger_id": ledger.id,
            "gateway_error": "synthetic provider timeout",
        }


@dataclass
class _CrashingPromotion:
    calls: list[uuid.UUID] = field(default_factory=list)

    async def __call__(
        self,
        session: AsyncSession,
        *,
        extraction_run_id: uuid.UUID,
        actor_user_id: int,
    ) -> list[Any]:
        del session, actor_user_id
        self.calls.append(extraction_run_id)
        raise RuntimeError("synthetic crash before promotion")


@dataclass
class _ControllableGateway:
    provider_name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    @property
    def extraction_provider(self) -> str:
        return self.provider_name

    @property
    def extraction_model(self) -> str:
        return "controllable-extraction-model"

    async def extract_candidates(
        self,
        session: AsyncSession,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        del prompt_template_version
        self.calls.append(list(source_versions))
        self.entered.set()
        await self.release.wait()

        from bot.db.models import LlmUsageLedger

        ledger = LlmUsageLedger(
            provider=self.provider_name,
            model=self.extraction_model,
            tokens_in=0,
            tokens_out=0,
        )
        session.add(ledger)
        await session.flush()
        return {"candidates": [], "llm_usage_ledger_id": ledger.id}


def _unique_ids() -> tuple[int, int, int]:
    suffix = uuid.uuid4().int % 100_000_000
    return (
        8_000_000_000 + suffix,
        -(8_000_000_000_000 + suffix),
        -(8_100_000_000_000 + suffix),
    )


async def _seed_message(
    session: AsyncSession,
    *,
    actor_id: int,
    chat_id: int,
    message_id: int,
    event_time: datetime,
) -> int:
    from bot.db.models import ChatMessage, MessageVersion

    message = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=actor_id,
        text=f"source-{message_id}",
        date=event_time,
        # A deliberately different ingestion time pins event-time windowing.
        created_at=datetime.now(timezone.utc),
        memory_policy="normal",
        is_redacted=False,
    )
    session.add(message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text=message.text,
        normalized_text=message.text,
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
    return message.id


async def _seed_actor_and_messages(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: int,
    messages: list[tuple[int, int, datetime]],
) -> None:
    from bot.db.models import User

    async with factory() as session, session.begin():
        session.add(
            User(
                id=actor_id,
                username=f"memory_actor_{actor_id}",
                first_name="Memory",
                is_member=True,
            )
        )
        await session.flush()
        for chat_id, message_id, event_time in messages:
            await _seed_message(
                session,
                actor_id=actor_id,
                chat_id=chat_id,
                message_id=message_id,
                event_time=event_time,
            )


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: int,
    chat_ids: tuple[int, ...],
    provider_name: str,
) -> None:
    from bot.db.models import (
        ChatMessage,
        ExtractionCandidate,
        ExtractionCursor,
        ExtractionRun,
        KnowledgeCard,
        LlmUsageLedger,
        MessageVersion,
        User,
    )

    async with factory() as session, session.begin():
        await session.execute(
            delete(KnowledgeCard).where(KnowledgeCard.approved_by_user_id == actor_id)
        )
        run_ids = select(ExtractionRun.id).where(
            ExtractionRun.source_chat_id.in_(chat_ids),
        )
        await session.execute(
            delete(ExtractionCandidate).where(ExtractionCandidate.extraction_run_id.in_(run_ids))
        )
        await session.execute(
            delete(ExtractionRun).where(
                ExtractionRun.source_chat_id.in_(chat_ids),
            )
        )
        await session.execute(
            delete(ExtractionCursor).where(ExtractionCursor.source_chat_id.in_(chat_ids))
        )
        chat_message_ids = select(ChatMessage.id).where(ChatMessage.chat_id.in_(chat_ids))
        await session.execute(
            delete(MessageVersion).where(MessageVersion.chat_message_id.in_(chat_message_ids))
        )
        await session.execute(delete(ChatMessage).where(ChatMessage.chat_id.in_(chat_ids)))
        await session.execute(
            delete(LlmUsageLedger).where(LlmUsageLedger.provider == provider_name)
        )
        await session.execute(delete(User).where(User.id == actor_id))


async def test_semantic_extraction_survives_caller_rollback_and_actor_change(
    postgres_engine,
) -> None:
    from bot.db.models import ExtractionCandidate, ExtractionRun, LlmUsageLedger
    from bot.services.extractor import run_extraction_pass

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    other_actor_id = actor_id + 1
    start = datetime(2010, 1, 2, tzinfo=timezone.utc)
    provider_name = f"semantic-rollback-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name, emit_candidate=True)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 501, start + timedelta(minutes=1))],
    )

    try:
        async with factory() as caller:
            await caller.begin()
            first = await run_extraction_pass(
                caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )
            await caller.rollback()

        async with factory() as second_caller:
            second = await run_extraction_pass(
                second_caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=other_actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )

        assert first.run_status == "completed"
        assert second.run_status == "completed"
        assert second.resumed is True
        assert second.extraction_run_id == first.extraction_run_id
        assert len(gateway.calls) == 1

        async with factory() as verify:
            run = await verify.get(ExtractionRun, first.extraction_run_id)
            assert run is not None and run.run_status == "completed"
            assert run.operator_user_id == actor_id
            assert run.semantic_key is not None
            assert run.llm_usage_ledger_id is not None
            assert await verify.get(LlmUsageLedger, run.llm_usage_ledger_id) is not None
            candidate = await verify.scalar(
                select(ExtractionCandidate).where(ExtractionCandidate.extraction_run_id == run.id)
            )
            assert candidate is not None
            assert candidate.payload_schema_version == "karpathy-wiki-v1"
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_extraction_parser_version_bump_replays_once_then_stays_idempotent(
    postgres_engine,
) -> None:
    """A completed v0.1.0 run cannot suppress v0.1.1, but v0.1.1 resumes itself."""
    from bot.db.models import ExtractionRun
    from bot.services.extractor import run_extraction_pass

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2010, 2, 3, tzinfo=timezone.utc)
    provider_name = f"semantic-parser-version-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 502, start + timedelta(minutes=1))],
    )

    try:
        async with factory() as first_caller:
            old = await run_extraction_pass(
                first_caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                prompt_template_version="v0.1.0",
                durable_session_factory=factory,
            )

        async with factory() as second_caller:
            upgraded = await run_extraction_pass(
                second_caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )

        async with factory() as third_caller:
            repeated = await run_extraction_pass(
                third_caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )

        assert old.run_status == upgraded.run_status == repeated.run_status == "completed"
        assert upgraded.resumed is False
        assert upgraded.extraction_run_id != old.extraction_run_id
        assert repeated.resumed is True
        assert repeated.extraction_run_id == upgraded.extraction_run_id
        assert len(gateway.calls) == 2

        async with factory() as verify:
            runs = (
                (
                    await verify.execute(
                        select(ExtractionRun)
                        .where(ExtractionRun.source_chat_id == chat_id)
                        .order_by(ExtractionRun.prompt_template_version)
                    )
                )
                .scalars()
                .all()
            )
            assert [run.prompt_template_version for run in runs] == ["v0.1.0", "v0.1.1"]
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_semantic_extraction_concurrency_dispatches_provider_once(
    postgres_engine,
) -> None:
    from bot.services.extractor import run_extraction_pass

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2011, 2, 3, tzinfo=timezone.utc)
    provider_name = f"semantic-concurrent-{uuid.uuid4()}"
    gateway = _ControllableGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 502, start + timedelta(minutes=1))],
    )

    async def _worker(operator_user_id: int):
        async with factory() as caller:
            return await run_extraction_pass(
                caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=operator_user_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )

    try:
        first_task = asyncio.create_task(_worker(actor_id))
        await asyncio.wait_for(gateway.entered.wait(), timeout=3)
        second_task = asyncio.create_task(_worker(actor_id + 1))
        await asyncio.sleep(0.1)
        assert len(gateway.calls) == 1
        gateway.release.set()
        first, second = await asyncio.wait_for(
            asyncio.gather(first_task, second_task),
            timeout=5,
        )

        assert first.extraction_run_id == second.extraction_run_id
        assert {first.resumed, second.resumed} == {False, True}
        assert len(gateway.calls) == 1
    finally:
        gateway.release.set()
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_ambiguous_running_reservation_fails_closed_without_repeat_spend(
    postgres_engine,
) -> None:
    from bot.db.models import ExtractionRun
    from bot.services.extractor import (
        AmbiguousExtractionRunError,
        run_extraction_pass,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2012, 3, 4, tzinfo=timezone.utc)
    provider_name = f"semantic-ambiguous-{uuid.uuid4()}"
    gateway = _ControllableGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 503, start + timedelta(minutes=1))],
    )

    async def _dispatch():
        async with factory() as caller:
            return await run_extraction_pass(
                caller,
                window_start=start,
                window_end=start + timedelta(hours=1),
                gateway=gateway,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )

    task = asyncio.create_task(_dispatch())
    try:
        await asyncio.wait_for(gateway.entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with factory() as verify:
            running = await verify.scalar(
                select(ExtractionRun).where(
                    ExtractionRun.source_chat_id == chat_id,
                    ExtractionRun.run_status == "running",
                )
            )
            assert running is not None and running.semantic_key is not None

        with pytest.raises(AmbiguousExtractionRunError) as caught:
            await _dispatch()
        assert caught.value.extraction_run_id == running.id
        assert len(gateway.calls) == 1
    finally:
        gateway.release.set()
        if not task.done():
            task.cancel()
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_live_cursor_processes_late_insert_and_current_edit_once(
    postgres_engine,
) -> None:
    from bot.db.models import ChatMessage, FeatureFlag, MessageVersion
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.extractor import (
        MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
        extraction_scheduler_tick,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    enabled_at = datetime(2013, 1, 1, tzinfo=timezone.utc)
    provider_name = f"cursor-incremental-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 601, enabled_at + timedelta(minutes=1))],
    )

    async with factory() as setup, setup.begin():
        await FeatureFlagRepo.set_enabled(
            setup,
            MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
            True,
        )
        await setup.execute(
            update(FeatureFlag)
            .where(FeatureFlag.flag_key == MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG)
            .values(updated_at=enabled_at)
        )
        first_message = await setup.scalar(
            select(ChatMessage).where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == 601,
            )
        )
        assert first_message is not None and first_message.current_version_id is not None
        first_mvid = first_message.current_version_id

    async def _tick(now: datetime):
        async with factory() as caller:
            result = await extraction_scheduler_tick(
                caller,
                gateway=gateway,
                now=now,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )
            await caller.commit()
            return result

    try:
        first = await _tick(enabled_at + timedelta(hours=1))
        assert first.skipped is False
        assert [[row["message_version_id"] for row in call] for call in gateway.calls] == [
            [first_mvid]
        ]

        async with factory() as late_session, late_session.begin():
            late_chat_message_id = await _seed_message(
                late_session,
                actor_id=actor_id,
                chat_id=chat_id,
                message_id=602,
                event_time=enabled_at - timedelta(days=90),
            )
            late_message = await late_session.get(ChatMessage, late_chat_message_id)
            assert late_message is not None and late_message.current_version_id is not None
            late_mvid = late_message.current_version_id

        second = await _tick(enabled_at + timedelta(hours=2))
        assert second.skipped is False
        assert [row["message_version_id"] for row in gateway.calls[1]] == [late_mvid]
        third = await _tick(enabled_at + timedelta(hours=3))
        assert third.skipped is True and third.reason == "up_to_date"
        assert len(gateway.calls) == 2

        async with factory() as edit_session, edit_session.begin():
            original = await edit_session.scalar(
                select(ChatMessage).where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.message_id == 601,
                )
            )
            assert original is not None
            edited = MessageVersion(
                chat_message_id=original.id,
                version_seq=2,
                text="edited current content",
                normalized_text="edited current content",
                entities_json={},
                content_hash=uuid.uuid4().hex,
                is_redacted=False,
            )
            edit_session.add(edited)
            await edit_session.flush()
            await edit_session.execute(
                update(ChatMessage)
                .where(ChatMessage.id == original.id)
                .values(current_version_id=edited.id)
            )
            edited_mvid = edited.id

        fourth = await _tick(enabled_at + timedelta(hours=4))
        assert fourth.skipped is False
        assert [row["message_version_id"] for row in gateway.calls[2]] == [edited_mvid]
        fifth = await _tick(enabled_at + timedelta(hours=5))
        assert fifth.skipped is True and fifth.reason == "up_to_date"
        assert len(gateway.calls) == 3
    finally:
        async with factory() as cleanup, cleanup.begin():
            await cleanup.execute(
                delete(FeatureFlag).where(
                    FeatureFlag.flag_key == MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG
                )
            )
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_splits_event_time_into_bounded_windows(postgres_engine) -> None:
    from bot.services.memory_backfill import run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2004, 1, 1, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-test-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    promotion = _PromotionSpy()
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[
            (chat_id, 1, start + timedelta(minutes=10)),
            (chat_id, 2, start + timedelta(hours=1, minutes=10)),
            (chat_id, 3, start + timedelta(hours=2, minutes=10)),
        ],
    )

    try:
        report = await run_memory_backfill(
            session_factory=factory,
            gateway=gateway,
            source_chat_id=chat_id,
            actor_user_id=actor_id,
            start=start,
            end=start + timedelta(hours=3),
            window_hours=1,
            max_windows=3,
            promote_run_fn=promotion,
        )

        assert report.completed_window_count == 3
        assert report.resumed_window_count == 0
        assert len(gateway.calls) == 3
        assert [len(call) for call in gateway.calls] == [1, 1, 1]
        assert len(promotion.calls) == 3
        assert [window.window_start for window in report.windows] == [
            start,
            start + timedelta(hours=1),
            start + timedelta(hours=2),
        ]
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_promotes_and_commits_candidates(postgres_engine) -> None:
    from bot.db.models import CardSource, KnowledgeCard
    from bot.services.memory_backfill import run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2004, 6, 1, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-test-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name, emit_candidate=True)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 5, start + timedelta(minutes=10))],
    )

    try:
        report = await run_memory_backfill(
            session_factory=factory,
            gateway=gateway,
            source_chat_id=chat_id,
            actor_user_id=actor_id,
            start=start,
            end=start + timedelta(hours=1),
            window_hours=1,
            max_windows=1,
        )

        assert report.candidate_count == 1
        assert report.promoted_count == 1
        async with factory() as verify:
            card = await verify.scalar(
                select(KnowledgeCard).where(KnowledgeCard.approved_by_user_id == actor_id)
            )
            assert card is not None and card.card_status == "approved"
            assert (
                await verify.scalar(
                    select(func.count(CardSource.id)).where(CardSource.card_id == card.id)
                )
                == 1
            )
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_isolates_source_chat_and_db_bounds(postgres_engine) -> None:
    from bot.services.memory_backfill import run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    target_time = datetime(2005, 2, 3, 4, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-test-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[
            (chat_id, 10, target_time),
            (other_chat_id, 11, target_time - timedelta(days=30)),
            (other_chat_id, 12, target_time + timedelta(days=30)),
        ],
    )

    try:
        report = await run_memory_backfill(
            session_factory=factory,
            gateway=gateway,
            source_chat_id=chat_id,
            actor_user_id=actor_id,
            start=None,
            end=None,
            window_hours=24,
            max_windows=1,
            promote_run_fn=_PromotionSpy(),
        )

        assert len(gateway.calls) == 1
        assert {item["chat_id"] for item in gateway.calls[0]} == {chat_id}
        assert report.range_start == target_time
        assert report.range_end == target_time + timedelta(microseconds=1)
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_resume_skips_provider_but_recovers_pending_promotion(
    postgres_engine,
) -> None:
    from bot.services.extractor import run_extraction_pass
    from bot.services.memory_backfill import run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2006, 3, 4, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-test-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    promotion = _PromotionSpy()
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 20, start + timedelta(minutes=1))],
    )
    async with factory() as session:
        existing = await run_extraction_pass(
            session,
            window_start=start,
            window_end=start + timedelta(hours=1),
            gateway=gateway,
            operator_user_id=actor_id,
            source_chat_id=chat_id,
            durable_session_factory=factory,
        )
        existing_run_id = existing.extraction_run_id
    assert len(gateway.calls) == 1
    gateway.calls.clear()

    try:
        report = await run_memory_backfill(
            session_factory=factory,
            gateway=gateway,
            source_chat_id=chat_id,
            actor_user_id=actor_id,
            start=start,
            end=start + timedelta(hours=1),
            window_hours=1,
            max_windows=1,
            promote_run_fn=promotion,
        )

        assert gateway.calls == []
        assert report.completed_window_count == 1
        assert report.resumed_window_count == 1
        assert promotion.calls == [(existing_run_id, actor_id)]
        assert report.windows[0].extraction_run_id == existing_run_id
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_gateway_error_commits_failed_run_without_self_deadlock(
    postgres_engine,
) -> None:
    """The failed run may link only to a ledger committed in the same caller tx."""
    from bot.db.models import ExtractionRun, LlmUsageLedger
    from bot.services.memory_backfill import MemoryBackfillWindowError, run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2006, 8, 9, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-error-{uuid.uuid4()}"
    gateway = _SameTransactionGatewayError(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 21, start + timedelta(minutes=1))],
    )

    try:
        with pytest.raises(MemoryBackfillWindowError) as caught:
            await asyncio.wait_for(
                run_memory_backfill(
                    session_factory=factory,
                    gateway=gateway,
                    source_chat_id=chat_id,
                    actor_user_id=actor_id,
                    start=start,
                    end=start + timedelta(hours=1),
                    window_hours=1,
                    max_windows=1,
                    promote_run_fn=_PromotionSpy(),
                ),
                timeout=3,
            )

        assert caught.value.error_class is None
        assert len(gateway.calls) == 1
        async with factory() as verify:
            run = await verify.scalar(
                select(ExtractionRun).where(
                    ExtractionRun.source_chat_id == chat_id,
                    ExtractionRun.operator_user_id == actor_id,
                    ExtractionRun.ingestion_window_start == start,
                    ExtractionRun.ingestion_window_end == start + timedelta(hours=1),
                )
            )
            assert run is not None
            assert run.run_status == "failed"
            assert run.gateway_error == "synthetic provider timeout"
            assert run.llm_usage_ledger_id is not None
            assert await verify.get(LlmUsageLedger, run.llm_usage_ledger_id) is not None
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_crash_before_promotion_resumes_without_second_provider_call(
    postgres_engine,
) -> None:
    """Extraction is durable before promotion, so retry spends no second LLM call."""
    from bot.db.models import ExtractionRun, KnowledgeCard
    from bot.services.memory_backfill import MemoryBackfillWindowError, run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2006, 9, 10, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-resume-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name, emit_candidate=True)
    crashing_promotion = _CrashingPromotion()
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 22, start + timedelta(minutes=1))],
    )

    try:
        with pytest.raises(MemoryBackfillWindowError, match="RuntimeError"):
            await run_memory_backfill(
                session_factory=factory,
                gateway=gateway,
                source_chat_id=chat_id,
                actor_user_id=actor_id,
                start=start,
                end=start + timedelta(hours=1),
                window_hours=1,
                max_windows=1,
                promote_run_fn=crashing_promotion,
            )

        async with factory() as verify:
            completed_run = await verify.scalar(
                select(ExtractionRun).where(
                    ExtractionRun.source_chat_id == chat_id,
                    ExtractionRun.operator_user_id == actor_id,
                    ExtractionRun.ingestion_window_start == start,
                    ExtractionRun.ingestion_window_end == start + timedelta(hours=1),
                    ExtractionRun.run_status == "completed",
                )
            )
            assert completed_run is not None

        report = await run_memory_backfill(
            session_factory=factory,
            gateway=gateway,
            source_chat_id=chat_id,
            actor_user_id=actor_id,
            start=start,
            end=start + timedelta(hours=1),
            window_hours=1,
            max_windows=1,
        )

        assert len(gateway.calls) == 1
        assert report.resumed_window_count == 1
        assert report.promoted_count == 1
        async with factory() as verify:
            assert (
                await verify.scalar(
                    select(func.count(KnowledgeCard.id)).where(
                        KnowledgeCard.approved_by_user_id == actor_id
                    )
                )
                == 1
            )
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_stops_after_first_failed_window(postgres_engine) -> None:
    from bot.services.memory_backfill import MemoryBackfillWindowError, run_memory_backfill

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2007, 4, 5, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-test-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name, fail_on_call=1)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[
            (chat_id, 30, start + timedelta(minutes=1)),
            (chat_id, 31, start + timedelta(hours=1, minutes=1)),
        ],
    )

    try:
        with pytest.raises(MemoryBackfillWindowError) as caught:
            await run_memory_backfill(
                session_factory=factory,
                gateway=gateway,
                source_chat_id=chat_id,
                actor_user_id=actor_id,
                start=start,
                end=start + timedelta(hours=2),
                window_hours=1,
                max_windows=2,
                promote_run_fn=_PromotionSpy(),
            )

        assert len(gateway.calls) == 1
        assert caught.value.window_start == start
        assert caught.value.error_class == "RuntimeError"
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_backfill_rejects_unbounded_work_and_missing_actor(postgres_engine) -> None:
    from bot.services.memory_backfill import (
        MemoryBackfillConfigurationError,
        run_memory_backfill,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    start = datetime(2008, 5, 6, tzinfo=timezone.utc)
    provider_name = f"memory-backfill-test-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)

    with pytest.raises(MemoryBackfillConfigurationError, match="existing user"):
        await run_memory_backfill(
            session_factory=factory,
            gateway=gateway,
            source_chat_id=chat_id,
            actor_user_id=actor_id,
            start=start,
            end=start + timedelta(hours=1),
        )

    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 40, start + timedelta(minutes=1))],
    )
    try:
        with pytest.raises(MemoryBackfillConfigurationError, match="max_windows"):
            await run_memory_backfill(
                session_factory=factory,
                gateway=gateway,
                source_chat_id=chat_id,
                actor_user_id=actor_id,
                start=start,
                end=start + timedelta(hours=401),
                window_hours=1,
                max_windows=400,
            )
        assert gateway.calls == []
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


def test_memory_backfill_cli_requires_chat_and_accepts_bounds(monkeypatch) -> None:
    from bot import cli

    captured: dict[str, Any] = {}

    def fake_command(args: Any) -> int:
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(cli, "_cmd_memory_backfill", fake_command)
    rc = cli.main(
        [
            "memory_backfill",
            "--chat-id",
            "-100123",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-02T00:00:00Z",
            "--window-hours",
            "6",
            "--max-windows",
            "10",
            "--actor-user-id",
            "149820031",
        ]
    )

    assert rc == 0
    assert captured["chat_id"] == -100123
    assert captured["start"] == "2026-01-01T00:00:00Z"
    assert captured["end"] == "2026-01-02T00:00:00Z"
    assert captured["window_hours"] == 6
    assert captured["max_windows"] == 10
    assert captured["actor_user_id"] == 149820031


def test_backfill_actor_is_explicit_or_required_env(monkeypatch) -> None:
    from bot.services.memory_backfill import (
        MEMORY_AUTOMATION_ACTOR_ENV,
        MemoryBackfillConfigurationError,
        resolve_automation_actor_user_id,
    )

    monkeypatch.delenv(MEMORY_AUTOMATION_ACTOR_ENV, raising=False)
    with pytest.raises(MemoryBackfillConfigurationError, match=MEMORY_AUTOMATION_ACTOR_ENV):
        resolve_automation_actor_user_id(None)

    monkeypatch.setenv(MEMORY_AUTOMATION_ACTOR_ENV, "149820031")
    assert resolve_automation_actor_user_id(None) == 149820031
    assert resolve_automation_actor_user_id(42) == 42


def test_backfill_cli_bounds_must_be_utc() -> None:
    from bot.cli import _parse_backfill_utc

    assert _parse_backfill_utc("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="UTC"):
        _parse_backfill_utc("2026-01-01T03:00:00+03:00")
    with pytest.raises(ValueError, match="UTC"):
        _parse_backfill_utc("2026-01-01T00:00:00")
