from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


pytestmark = pytest.mark.usefixtures("app_env")


async def _ready_image_memory(db_session, *, chat_id: int):
    from bot.db.models import ChatMessage, MessageMedia, MessageVersion
    from bot.db.repos.user import UserRepo

    now = datetime.now(timezone.utc)
    user = await UserRepo.upsert(
        db_session,
        telegram_id=9_919_001,
        username="image_member",
        first_name="Image",
        last_name=None,
    )
    message = ChatMessage(
        message_id=919_001,
        chat_id=chat_id,
        user_id=user.id,
        text=None,
        caption=None,
        date=now,
        message_kind="photo",
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(message)
    await db_session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text=None,
        caption=None,
        normalized_text=None,
        content_hash=f"image-consumer-{message.id}",
        is_redacted=False,
    )
    db_session.add(version)
    await db_session.flush()
    message.current_version_id = version.id
    media = MessageMedia(
        chat_message_id=message.id,
        media_kind="photo",
        telegram_file_id="photo-file",
        telegram_file_unique_id="photo-unique",
        source_message_url="https://t.me/c/919001/919001",
        description="На фотографии флипчарт со схемой системы памяти.",
        description_status="ready",
        description_model="gpt-5-nano",
    )
    db_session.add(media)
    await db_session.flush()
    return message, version


async def test_ready_image_description_is_searchable_without_llm(db_session):
    from bot.services.search import search_messages

    chat_id = -100919001
    message, version = await _ready_image_memory(db_session, chat_id=chat_id)

    hits = await search_messages(
        db_session,
        "флипчарт",
        chat_id=chat_id,
        include_cards=False,
    )

    assert len(hits) == 1
    assert hits[0].chat_message_id == message.id
    assert hits[0].message_version_id == version.id
    assert "флипчарт" in hits[0].snippet.casefold()


async def test_ready_image_description_enters_digest_context(db_session):
    from bot.services.digest_context import build_digest_context

    chat_id = -100919002
    await _ready_image_memory(db_session, chat_id=chat_id)
    now = datetime.now(timezone.utc)

    context = await build_digest_context(
        db_session,
        type="daily",
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(hours=1),
        source_chat_id=chat_id,
    )

    assert len(context.messages) == 1
    assert "флипчарт" in context.messages[0].media_description.casefold()


async def test_ready_image_description_enters_extraction_source(db_session):
    from bot.services.extractor import _select_eligible_sources

    chat_id = -100919003
    await _ready_image_memory(db_session, chat_id=chat_id)
    now = datetime.now(timezone.utc)

    rows = await _select_eligible_sources(
        db_session,
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(hours=1),
        source_chat_id=chat_id,
        force_include_chat_message_ids=None,
    )

    image_rows = [row for row in rows if row.chat_id == chat_id]
    assert len(image_rows) == 1
    assert "[Описание изображения]" in (image_rows[0].normalized_text or "")
    assert "флипчарт" in (image_rows[0].normalized_text or "").casefold()
