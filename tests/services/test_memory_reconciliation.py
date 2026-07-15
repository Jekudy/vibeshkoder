"""Operational reconciliation for ambiguous extraction and image calls."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.usefixtures("app_env")


@dataclass
class _Gateway:
    provider_name: str
    response_error: bool = False
    entered: asyncio.Event | None = None
    release: asyncio.Event | None = None
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    @property
    def extraction_provider(self) -> str:
        return self.provider_name

    @property
    def extraction_model(self) -> str:
        return "reconciliation-test-model"

    async def extract_candidates(
        self,
        session: AsyncSession,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        del prompt_template_version
        self.calls.append(list(source_versions))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        from bot.db.models import LlmUsageLedger

        ledger = LlmUsageLedger(
            provider=self.provider_name,
            model=self.extraction_model,
            tokens_in=0,
            tokens_out=0,
            error="provider_transient:timeout" if self.response_error else None,
            call_type="extract_candidates",
        )
        session.add(ledger)
        await session.flush()
        result: dict[str, Any] = {
            "candidates": [],
            "llm_usage_ledger_id": ledger.id,
        }
        if self.response_error:
            result["gateway_error"] = "provider_transient:timeout"
        return result


@dataclass
class _RejectedGateway(_Gateway):
    async def extract_candidates(
        self,
        session: AsyncSession,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        del prompt_template_version
        self.calls.append(list(source_versions))
        from bot.db.models import LlmUsageLedger

        ledger = LlmUsageLedger(
            provider=self.provider_name,
            model=self.extraction_model,
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            error="provider_transient:rate_limit",
            call_type="extract_candidates",
        )
        session.add(ledger)
        await session.flush()
        return {
            "candidates": [],
            "llm_usage_ledger_id": ledger.id,
            "gateway_error": "provider_transient:rate_limit",
        }


def _ids() -> tuple[int, int]:
    suffix = uuid.uuid4().int % 100_000_000
    return 8_700_000_000 + suffix, -(8_700_000_000_000 + suffix)


async def _seed_message(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: int,
    chat_id: int,
    event_at: datetime,
) -> tuple[int, int]:
    from bot.db.models import ChatMessage, MessageVersion, User

    async with factory() as session, session.begin():
        session.add(
            User(
                id=actor_id,
                username=f"reconcile_{actor_id}",
                first_name="Reconcile",
                is_member=True,
            )
        )
        await session.flush()
        message = ChatMessage(
            message_id=actor_id,
            chat_id=chat_id,
            user_id=actor_id,
            text="reconciliation source",
            date=event_at,
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
        return message.id, version.id


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: int,
    chat_id: int,
    provider_name: str,
) -> None:
    from bot.db.models import (
        ChatMessage,
        ExtractionCandidate,
        ExtractionCursor,
        ExtractionRun,
        ExtractionRunResolution,
        ImageDescriptionResolution,
        LlmUsageLedger,
        MessageMedia,
        MessageVersion,
        User,
    )

    async with factory() as session, session.begin():
        run_ids = select(ExtractionRun.id).where(ExtractionRun.source_chat_id == chat_id)
        media_ids = (
            select(MessageMedia.id)
            .join(ChatMessage, ChatMessage.id == MessageMedia.chat_message_id)
            .where(ChatMessage.chat_id == chat_id)
        )
        await session.execute(
            delete(ExtractionRunResolution).where(ExtractionRunResolution.run_id.in_(run_ids))
        )
        await session.execute(
            delete(ExtractionCandidate).where(ExtractionCandidate.extraction_run_id.in_(run_ids))
        )
        await session.execute(
            delete(ImageDescriptionResolution).where(
                ImageDescriptionResolution.message_media_id.in_(media_ids)
            )
        )
        # Delete retry children before their self-referenced parents.
        attempts = (
            (
                await session.execute(
                    select(ExtractionRun)
                    .where(ExtractionRun.source_chat_id == chat_id)
                    .order_by(ExtractionRun.attempt_no.desc())
                )
            )
            .scalars()
            .all()
        )
        for run in attempts:
            await session.delete(run)
            await session.flush()
        await session.execute(
            delete(ExtractionCursor).where(ExtractionCursor.source_chat_id == chat_id)
        )
        message_ids = select(ChatMessage.id).where(ChatMessage.chat_id == chat_id)
        await session.execute(
            delete(MessageVersion).where(MessageVersion.chat_message_id.in_(message_ids))
        )
        await session.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
        await session.execute(
            delete(LlmUsageLedger).where(LlmUsageLedger.provider == provider_name)
        )
        await session.execute(delete(User).where(User.id == actor_id))


async def test_event_window_abandon_is_audited_and_resumes_without_provider(
    postgres_engine,
) -> None:
    from bot.db.models import ExtractionRun, ExtractionRunResolution
    from bot.services.extractor import run_extraction_pass
    from bot.services.memory_reconciliation import (
        MemoryReconciliationError,
        reconcile_extraction_run,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id = _ids()
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    provider_name = f"reconcile-abandon-{uuid.uuid4()}"
    failing = _Gateway(provider_name, response_error=True)
    retry = _Gateway(provider_name)
    await _seed_message(factory, actor_id=actor_id, chat_id=chat_id, event_at=start)
    try:
        async with factory() as caller:
            first = await run_extraction_pass(
                caller,
                window_start=start - timedelta(minutes=1),
                window_end=start + timedelta(minutes=1),
                gateway=failing,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )
        assert first.run_status == "failed"
        async with factory() as verify:
            run = await verify.get(ExtractionRun, first.extraction_run_id)
            assert run is not None and run.dispatch_state == "response_received"

        with pytest.raises(MemoryReconciliationError, match="accept-memory-gap"):
            await reconcile_extraction_run(
                session_factory=factory,
                run_id=first.extraction_run_id,
                action="abandon",
                actor_user_id=actor_id,
                reason="Provider response was unusable",
            )
        await reconcile_extraction_run(
            session_factory=factory,
            run_id=first.extraction_run_id,
            action="abandon",
            actor_user_id=actor_id,
            reason="Provider response was unusable",
            accept_memory_gap=True,
        )

        async with factory() as caller:
            resumed = await run_extraction_pass(
                caller,
                window_start=start - timedelta(minutes=1),
                window_end=start + timedelta(minutes=1),
                gateway=retry,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )
        assert resumed.run_status == "completed"
        assert resumed.resumed is True
        assert retry.calls == []
        async with factory() as verify:
            original = await verify.get(ExtractionRun, first.extraction_run_id)
            resolution = await verify.scalar(
                select(ExtractionRunResolution).where(
                    ExtractionRunResolution.run_id == first.extraction_run_id
                )
            )
            assert original is not None and original.run_status == "failed"
            assert resolution is not None and resolution.action == "abandon"
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_id=chat_id,
            provider_name=provider_name,
        )


async def test_risk_accepted_retry_creates_one_linked_attempt_and_one_dispatch(
    postgres_engine,
) -> None:
    from bot.db.models import ExtractionRun, ExtractionRunResolution
    from bot.services.extractor import run_extraction_pass
    from bot.services.memory_reconciliation import (
        MemoryReconciliationError,
        reconcile_extraction_run,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id = _ids()
    start = datetime(2019, 1, 1, tzinfo=timezone.utc)
    provider_name = f"reconcile-retry-{uuid.uuid4()}"
    first_gateway = _Gateway(
        provider_name,
        entered=asyncio.Event(),
        release=asyncio.Event(),
    )
    await _seed_message(factory, actor_id=actor_id, chat_id=chat_id, event_at=start)

    async def _run(gateway: _Gateway):
        async with factory() as caller:
            return await run_extraction_pass(
                caller,
                window_start=start - timedelta(minutes=1),
                window_end=start + timedelta(minutes=1),
                gateway=gateway,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )

    first_task = asyncio.create_task(_run(first_gateway))
    try:
        assert first_gateway.entered is not None
        await asyncio.wait_for(first_gateway.entered.wait(), timeout=5)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        async with factory() as verify:
            original = await verify.scalar(
                select(ExtractionRun).where(ExtractionRun.source_chat_id == chat_id)
            )
            assert original is not None
            assert original.run_status == "running"
            assert original.dispatch_state == "unknown"

        with pytest.raises(MemoryReconciliationError, match="safe_retry"):
            await reconcile_extraction_run(
                session_factory=factory,
                run_id=original.id,
                action="safe_retry",
                actor_user_id=actor_id,
                reason="No response recorded",
            )
        with pytest.raises(MemoryReconciliationError, match="duplicate-cost"):
            await reconcile_extraction_run(
                session_factory=factory,
                run_id=original.id,
                action="risk_accepted_retry",
                actor_user_id=actor_id,
                reason="Provider audit is inconclusive",
            )

        results = await asyncio.gather(
            reconcile_extraction_run(
                session_factory=factory,
                run_id=original.id,
                action="risk_accepted_retry",
                actor_user_id=actor_id,
                reason="Provider audit is inconclusive",
                accept_possible_duplicate_cost=True,
            ),
            reconcile_extraction_run(
                session_factory=factory,
                run_id=original.id,
                action="risk_accepted_retry",
                actor_user_id=actor_id,
                reason="Provider audit is inconclusive",
                accept_possible_duplicate_cost=True,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert sum(isinstance(result, MemoryReconciliationError) for result in results) == 1

        retry_gateway = _Gateway(
            provider_name,
            entered=asyncio.Event(),
            release=asyncio.Event(),
        )
        retry_one = asyncio.create_task(_run(retry_gateway))
        assert retry_gateway.entered is not None
        await asyncio.wait_for(retry_gateway.entered.wait(), timeout=5)
        retry_two = asyncio.create_task(_run(retry_gateway))
        await asyncio.sleep(0.1)
        assert len(retry_gateway.calls) == 1
        assert retry_gateway.release is not None
        retry_gateway.release.set()
        one, two = await asyncio.wait_for(asyncio.gather(retry_one, retry_two), timeout=5)
        assert one.extraction_run_id == two.extraction_run_id
        assert len(retry_gateway.calls) == 1

        async with factory() as verify:
            attempts = (
                (
                    await verify.execute(
                        select(ExtractionRun)
                        .where(ExtractionRun.semantic_key == original.semantic_key)
                        .order_by(ExtractionRun.attempt_no)
                    )
                )
                .scalars()
                .all()
            )
            assert [attempt.attempt_no for attempt in attempts] == [1, 2]
            assert attempts[1].retry_of_run_id == original.id
            assert attempts[1].dispatch_state == "response_received"
            assert (
                await verify.scalar(
                    select(ExtractionRunResolution).where(
                        ExtractionRunResolution.run_id == original.id
                    )
                )
            ) is not None
    finally:
        if first_gateway.release is not None:
            first_gateway.release.set()
        if not first_task.done():
            first_task.cancel()
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_id=chat_id,
            provider_name=provider_name,
        )


async def test_rejected_pre_accept_allows_safe_retry_without_risk_flag(
    postgres_engine,
) -> None:
    from bot.db.models import ExtractionRun, LlmUsageLedger
    from bot.services.extractor import run_extraction_pass
    from bot.services.memory_reconciliation import (
        MemoryReconciliationError,
        reconcile_extraction_run,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id = _ids()
    start = datetime(2019, 6, 1, tzinfo=timezone.utc)
    provider_name = f"reconcile-safe-{uuid.uuid4()}"
    rejected = _RejectedGateway(provider_name)
    success = _Gateway(provider_name)
    await _seed_message(factory, actor_id=actor_id, chat_id=chat_id, event_at=start)
    try:
        async with factory() as caller:
            first = await run_extraction_pass(
                caller,
                window_start=start - timedelta(minutes=1),
                window_end=start + timedelta(minutes=1),
                gateway=rejected,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )
        async with factory() as verify:
            original = await verify.get(ExtractionRun, first.extraction_run_id)
            assert original is not None
            assert original.dispatch_state == "rejected_pre_accept"
            assert original.llm_usage_ledger_id is not None
            old_ledger = await verify.get(LlmUsageLedger, original.llm_usage_ledger_id)
            assert old_ledger is not None
            old_ledger_snapshot = (old_ledger.error, old_ledger.cost_usd, old_ledger.request_id)

        await reconcile_extraction_run(
            session_factory=factory,
            run_id=first.extraction_run_id,
            action="safe_retry",
            actor_user_id=actor_id,
            reason="Provider explicitly rejected before acceptance",
        )
        async with factory() as caller:
            second = await run_extraction_pass(
                caller,
                window_start=start - timedelta(minutes=1),
                window_end=start + timedelta(minutes=1),
                gateway=success,
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                durable_session_factory=factory,
            )
        assert second.run_status == "completed"
        async with factory() as verify:
            retry = await verify.get(ExtractionRun, second.extraction_run_id)
            assert retry is not None and retry.attempt_no == 2
            assert retry.retry_of_run_id == first.extraction_run_id
            preserved = await verify.get(LlmUsageLedger, original.llm_usage_ledger_id)
            assert preserved is not None
            assert (
                preserved.error,
                preserved.cost_usd,
                preserved.request_id,
            ) == old_ledger_snapshot
        with pytest.raises(MemoryReconciliationError, match="completed"):
            await reconcile_extraction_run(
                session_factory=factory,
                run_id=second.extraction_run_id,
                action="abandon",
                actor_user_id=actor_id,
                reason="Completed runs are immutable",
                accept_memory_gap=True,
            )
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_id=chat_id,
            provider_name=provider_name,
        )


async def test_cursor_abandon_advances_watermark_atomically(postgres_engine) -> None:
    from bot.db.models import ExtractionCursor, ExtractionRun
    from bot.services.memory_reconciliation import reconcile_extraction_run

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id = _ids()
    provider_name = f"reconcile-cursor-{uuid.uuid4()}"
    await _seed_message(
        factory,
        actor_id=actor_id,
        chat_id=chat_id,
        event_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    try:
        async with factory() as setup, setup.begin():
            run = ExtractionRun(
                ingestion_window_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
                ingestion_window_end=datetime(2020, 1, 2, tzinfo=timezone.utc),
                candidate_count=0,
                run_status="running",
                operator_user_id=actor_id,
                source_chat_id=chat_id,
                semantic_key="7" * 64,
                source_snapshot_hash="8" * 64,
                prompt_template_version="v0.1.0",
                provider=provider_name,
                model="reconciliation-test-model",
                selection_mode="version_cursor",
                cursor_start_message_version_id=10,
                cursor_end_message_version_id=20,
                dispatch_state="unknown",
            )
            setup.add(run)
            await setup.flush()
            run_id = run.id

        await reconcile_extraction_run(
            session_factory=factory,
            run_id=run_id,
            action="abandon",
            actor_user_id=actor_id,
            reason="Cannot prove provider outcome",
            accept_memory_gap=True,
        )
        async with factory() as verify:
            cursor = await verify.get(ExtractionCursor, chat_id)
            run = await verify.get(ExtractionRun, run_id)
            assert cursor is not None and cursor.last_message_version_id == 20
            assert run is not None and run.run_status == "running"
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_id=chat_id,
            provider_name=provider_name,
        )


async def test_cursor_retry_replays_resolved_window_before_new_versions(
    postgres_engine,
) -> None:
    from bot.db.models import (
        ChatMessage,
        ExtractionCursor,
        ExtractionRun,
        FeatureFlag,
        MessageVersion,
    )
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.extractor import (
        MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
        extraction_scheduler_tick,
    )
    from bot.services.memory_reconciliation import reconcile_extraction_run

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id = _ids()
    enabled_at = datetime(2020, 6, 1, tzinfo=timezone.utc)
    provider_name = f"reconcile-cursor-retry-{uuid.uuid4()}"
    first_gateway = _Gateway(
        provider_name,
        entered=asyncio.Event(),
        release=asyncio.Event(),
    )
    _, first_version_id = await _seed_message(
        factory,
        actor_id=actor_id,
        chat_id=chat_id,
        event_at=enabled_at + timedelta(minutes=1),
    )

    async def _tick(gateway: _Gateway, *, now: datetime):
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

    first_task = asyncio.create_task(_tick(first_gateway, now=enabled_at + timedelta(hours=1)))
    try:
        assert first_gateway.entered is not None
        await asyncio.wait_for(first_gateway.entered.wait(), timeout=5)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        async with factory() as verify:
            original = await verify.scalar(
                select(ExtractionRun).where(ExtractionRun.source_chat_id == chat_id)
            )
            assert original is not None
            assert original.cursor_end_message_version_id == first_version_id
            assert original.dispatch_state == "unknown"

        await reconcile_extraction_run(
            session_factory=factory,
            run_id=original.id,
            action="risk_accepted_retry",
            actor_user_id=actor_id,
            reason="Provider audit is inconclusive",
            accept_possible_duplicate_cost=True,
        )

        # This version arrives after reconciliation but before the next tick.
        # It must not be folded into the paid retry's semantic identity.
        async with factory() as append, append.begin():
            message = ChatMessage(
                message_id=actor_id + 1,
                chat_id=chat_id,
                user_id=actor_id,
                text="newer cursor source",
                date=enabled_at + timedelta(minutes=2),
                memory_policy="normal",
                is_redacted=False,
            )
            append.add(message)
            await append.flush()
            version = MessageVersion(
                chat_message_id=message.id,
                version_seq=1,
                text=message.text,
                normalized_text=message.text,
                entities_json={},
                content_hash=uuid.uuid4().hex,
                is_redacted=False,
            )
            append.add(version)
            await append.flush()
            await append.execute(
                update(ChatMessage)
                .where(ChatMessage.id == message.id)
                .values(current_version_id=version.id)
            )
            second_version_id = version.id
        assert second_version_id > first_version_id

        retry_gateway = _Gateway(provider_name)
        retried = await _tick(retry_gateway, now=enabled_at + timedelta(hours=2))
        assert retried.skipped is False
        assert [[item["message_version_id"] for item in call] for call in retry_gateway.calls] == [
            [first_version_id]
        ]
        async with factory() as verify:
            attempts = (
                (
                    await verify.execute(
                        select(ExtractionRun)
                        .where(ExtractionRun.semantic_key == original.semantic_key)
                        .order_by(ExtractionRun.attempt_no)
                    )
                )
                .scalars()
                .all()
            )
            cursor = await verify.get(ExtractionCursor, chat_id)
            assert [attempt.attempt_no for attempt in attempts] == [1, 2]
            assert attempts[1].retry_of_run_id == original.id
            assert cursor is not None and cursor.last_message_version_id == first_version_id

        later_gateway = _Gateway(provider_name)
        later = await _tick(later_gateway, now=enabled_at + timedelta(hours=3))
        assert later.skipped is False
        assert [[item["message_version_id"] for item in call] for call in later_gateway.calls] == [
            [second_version_id]
        ]
    finally:
        if first_gateway.release is not None:
            first_gateway.release.set()
        if not first_task.done():
            first_task.cancel()
        async with factory() as cleanup, cleanup.begin():
            await cleanup.execute(
                delete(FeatureFlag).where(
                    FeatureFlag.flag_key == MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG
                )
            )
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_id=chat_id,
            provider_name=provider_name,
        )


async def test_image_reconciliation_is_explicit_exactly_once_and_keeps_old_ledger(
    postgres_engine,
) -> None:
    from bot.db.models import (
        ImageDescriptionResolution,
        LlmUsageLedger,
        MessageMedia,
    )
    from bot.services.memory_reconciliation import (
        MemoryReconciliationError,
        reconcile_image_description,
    )

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id = _ids()
    provider_name = f"reconcile-image-{uuid.uuid4()}"
    message_id, _ = await _seed_message(
        factory,
        actor_id=actor_id,
        chat_id=chat_id,
        event_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
    )
    try:
        async with factory() as setup, setup.begin():
            ledger = LlmUsageLedger(
                provider=provider_name,
                model="gpt-5-nano",
                prompt_hash="9" * 64,
                tokens_in=0,
                tokens_out=0,
                cost_usd=Decimal("0.001"),
                error="reserved_in_flight",
                call_type="image_description",
            )
            setup.add(ledger)
            await setup.flush()
            media = MessageMedia(
                chat_message_id=message_id,
                media_kind="photo",
                telegram_file_id="file-087",
                telegram_file_unique_id="unique-087",
                source_message_url="https://t.me/c/87001/1",
                description_status="processing",
                description_attempts=1,
                description_claim_token=str(uuid.uuid4()),
                description_claimed_at=datetime.now(timezone.utc),
                llm_usage_ledger_id=ledger.id,
                last_error_code="reserved_in_flight",
            )
            setup.add(media)
            await setup.flush()
            media_id = media.id
            ledger_id = ledger.id

        with pytest.raises(MemoryReconciliationError, match="duplicate-cost"):
            await reconcile_image_description(
                session_factory=factory,
                message_media_id=media_id,
                action="risk_accepted_retry",
                actor_user_id=actor_id,
                reason="Provider audit is inconclusive",
            )
        outcomes = await asyncio.gather(
            reconcile_image_description(
                session_factory=factory,
                message_media_id=media_id,
                action="risk_accepted_retry",
                actor_user_id=actor_id,
                reason="Provider audit is inconclusive",
                accept_possible_duplicate_cost=True,
            ),
            reconcile_image_description(
                session_factory=factory,
                message_media_id=media_id,
                action="risk_accepted_retry",
                actor_user_id=actor_id,
                reason="Provider audit is inconclusive",
                accept_possible_duplicate_cost=True,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        assert sum(isinstance(item, MemoryReconciliationError) for item in outcomes) == 1

        async with factory() as verify:
            media = await verify.get(MessageMedia, media_id)
            ledger = await verify.get(LlmUsageLedger, ledger_id)
            assert media is not None and media.description_status == "pending"
            assert media.description_claim_token is None
            assert media.llm_usage_ledger_id == ledger_id
            assert ledger is not None
            assert ledger.error == "reserved_in_flight"
            assert ledger.cost_usd == Decimal("0.001000")

        # A retry creates a new durable provider claim. If that second claim is
        # ambiguous too, it needs its own exactly-once operator decision rather
        # than being blocked by the audit row for attempt 1.
        async with factory() as second_claim, second_claim.begin():
            await second_claim.execute(
                update(MessageMedia)
                .where(MessageMedia.id == media_id)
                .values(
                    description_status="processing",
                    description_attempts=2,
                    description_claim_token=str(uuid.uuid4()),
                    description_claimed_at=datetime.now(timezone.utc),
                    last_error_code="reserved_in_flight",
                )
            )
        await reconcile_image_description(
            session_factory=factory,
            message_media_id=media_id,
            action="abandon",
            actor_user_id=actor_id,
            reason="Second provider outcome is also inconclusive",
            accept_memory_gap=True,
        )

        async with factory() as verify:
            resolutions = (
                (
                    await verify.execute(
                        select(ImageDescriptionResolution)
                        .where(ImageDescriptionResolution.message_media_id == media_id)
                        .order_by(ImageDescriptionResolution.attempt_no)
                    )
                )
                .scalars()
                .all()
            )
            media = await verify.get(MessageMedia, media_id)
            assert [resolution.attempt_no for resolution in resolutions] == [1, 2]
            assert [resolution.action for resolution in resolutions] == [
                "risk_accepted_retry",
                "abandon",
            ]
            assert media is not None and media.description_status == "failed"
    finally:
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_id=chat_id,
            provider_name=provider_name,
        )
