"""Live extraction waits for non-terminal image descriptions at its cursor."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.services.test_memory_backfill import (
    _FakeGateway,
    _cleanup,
    _seed_actor_and_messages,
    _unique_ids,
)


pytestmark = pytest.mark.usefixtures("app_env")


async def _enable_scheduler(
    factory: async_sessionmaker[AsyncSession],
    *,
    enabled_at: datetime,
) -> None:
    from bot.db.models import FeatureFlag
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.services.extractor import MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG

    async with factory() as session, session.begin():
        await FeatureFlagRepo.set_enabled(
            session,
            MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
            True,
        )
        await session.execute(
            update(FeatureFlag)
            .where(FeatureFlag.flag_key == MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG)
            .values(updated_at=enabled_at)
        )


async def _disable_scheduler(factory: async_sessionmaker[AsyncSession]) -> None:
    from bot.db.models import FeatureFlag
    from bot.services.extractor import MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG

    async with factory() as session, session.begin():
        await session.execute(
            delete(FeatureFlag).where(
                FeatureFlag.flag_key == MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG
            )
        )


async def _tick(factory, *, gateway, now, chat_id):
    from bot.services.extractor import extraction_scheduler_tick

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


async def test_pending_photo_blocks_cursor_then_ready_description_is_extracted_once(
    postgres_engine,
) -> None:
    from bot.db.models import ChatMessage, ExtractionCursor, MessageMedia

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    enabled_at = datetime(2022, 1, 1, tzinfo=timezone.utc)
    provider_name = f"late-image-ready-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 8701, enabled_at + timedelta(minutes=1))],
    )
    await _enable_scheduler(factory, enabled_at=enabled_at)
    try:
        async with factory() as setup, setup.begin():
            message = await setup.scalar(
                select(ChatMessage).where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.message_id == 8701,
                )
            )
            assert message is not None and message.current_version_id is not None
            message_version_id = message.current_version_id
            media = MessageMedia(
                chat_message_id=message.id,
                media_kind="photo",
                telegram_file_id="late-image-8701",
                telegram_file_unique_id="late-image-unique-8701",
                source_message_url="https://t.me/c/8701/1",
                description_status="pending",
            )
            setup.add(media)
            await setup.flush()
            media_id = media.id

        waiting = await _tick(
            factory,
            gateway=gateway,
            now=enabled_at + timedelta(hours=1),
            chat_id=chat_id,
        )
        assert waiting.skipped is True
        assert waiting.reason == "waiting_for_image_description"
        assert gateway.calls == []
        async with factory() as verify:
            cursor = await verify.get(ExtractionCursor, chat_id)
            assert cursor is None or cursor.last_message_version_id < message_version_id

        async with factory() as ready, ready.begin():
            await ready.execute(
                update(MessageMedia)
                .where(MessageMedia.id == media_id)
                .values(
                    description_status="ready",
                    description="На фото доска с планом запуска.",
                    description_model="gpt-5-nano",
                )
            )

        processed = await _tick(
            factory,
            gateway=gateway,
            now=enabled_at + timedelta(hours=2),
            chat_id=chat_id,
        )
        assert processed.skipped is False
        assert len(gateway.calls) == 1
        payload = gateway.calls[0]
        assert [item["message_version_id"] for item in payload] == [message_version_id]
        normalized = payload[0]["normalized_text"]
        assert normalized.count("На фото доска с планом запуска.") == 1
        assert normalized.count("https://t.me/c/8701/1") == 1

        done = await _tick(
            factory,
            gateway=gateway,
            now=enabled_at + timedelta(hours=3),
            chat_id=chat_id,
        )
        assert done.skipped is True and done.reason == "up_to_date"
        assert len(gateway.calls) == 1
    finally:
        await _disable_scheduler(factory)
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )


async def test_ambiguous_processing_photo_blocks_until_explicit_abandon(postgres_engine) -> None:
    from bot.db.models import (
        ChatMessage,
        ImageDescriptionResolution,
        MessageMedia,
    )
    from bot.services.memory_reconciliation import reconcile_image_description

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id, chat_id, other_chat_id = _unique_ids()
    enabled_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    provider_name = f"late-image-ambiguous-{uuid.uuid4()}"
    gateway = _FakeGateway(provider_name)
    await _seed_actor_and_messages(
        factory,
        actor_id=actor_id,
        messages=[(chat_id, 8702, enabled_at + timedelta(minutes=1))],
    )
    await _enable_scheduler(factory, enabled_at=enabled_at)
    try:
        async with factory() as setup, setup.begin():
            message = await setup.scalar(
                select(ChatMessage).where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.message_id == 8702,
                )
            )
            assert message is not None
            media = MessageMedia(
                chat_message_id=message.id,
                media_kind="photo",
                telegram_file_id="late-image-8702",
                telegram_file_unique_id="late-image-unique-8702",
                source_message_url="https://t.me/c/8702/1",
                description_status="processing",
                description_attempts=1,
                description_claim_token=str(uuid.uuid4()),
                description_claimed_at=datetime.now(timezone.utc),
                last_error_code="reserved_in_flight",
            )
            setup.add(media)
            await setup.flush()
            media_id = media.id

        waiting = await _tick(
            factory,
            gateway=gateway,
            now=enabled_at + timedelta(hours=1),
            chat_id=chat_id,
        )
        assert waiting.skipped is True
        assert waiting.reason == "waiting_for_image_description"
        assert gateway.calls == []

        await reconcile_image_description(
            session_factory=factory,
            message_media_id=media_id,
            action="abandon",
            actor_user_id=actor_id,
            reason="Provider outcome cannot be proven",
            accept_memory_gap=True,
        )
        async with factory() as verify:
            media = await verify.get(MessageMedia, media_id)
            resolution = await verify.scalar(
                select(ImageDescriptionResolution).where(
                    ImageDescriptionResolution.message_media_id == media_id
                )
            )
            assert media is not None and media.description_status == "failed"
            assert media.description_claim_token is None
            assert resolution is not None and resolution.action == "abandon"
        processed = await _tick(
            factory,
            gateway=gateway,
            now=enabled_at + timedelta(hours=2),
            chat_id=chat_id,
        )
        assert processed.skipped is False
        assert len(gateway.calls) == 1
        assert "[Описание изображения]" not in gateway.calls[0][0]["normalized_text"]
    finally:
        await _disable_scheduler(factory)
        async with factory() as cleanup, cleanup.begin():
            await cleanup.execute(
                delete(ImageDescriptionResolution).where(
                    ImageDescriptionResolution.message_media_id == media_id
                )
            )
        await _cleanup(
            factory,
            actor_id=actor_id,
            chat_ids=(chat_id, other_chat_id),
            provider_name=provider_name,
        )
