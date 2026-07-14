"""Catch-all memory handler persists humans and ignores bot-authored output."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import import_module


pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


def _message(
    *,
    is_bot: bool,
    text: str = "сообщение",
    with_photo: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=701,
        chat=SimpleNamespace(id=COMMUNITY_CHAT_ID, type="supergroup"),
        from_user=SimpleNamespace(
            id=777,
            username="shkoder" if is_bot else "member",
            first_name="Shkoder" if is_bot else "Member",
            last_name=None,
            is_bot=is_bot,
        ),
        text=text,
        caption=None,
        date=datetime.now(timezone.utc),
        photo=(
            [
                SimpleNamespace(
                    file_id="photo-large",
                    file_unique_id="photo-unique",
                    width=1200,
                    height=900,
                    file_size=100_000,
                )
            ]
            if with_photo
            else None
        ),
    )


async def test_catch_all_persists_human_message(monkeypatch) -> None:
    handler = import_module("bot.handlers.chat_messages")
    upsert = AsyncMock()
    persist = AsyncMock(
        return_value=SimpleNamespace(
            policy="normal",
            is_offrecord_mark_created=False,
            chat_message=SimpleNamespace(id=801),
        )
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", upsert)
    monkeypatch.setattr(handler, "persist_message_with_policy", persist)
    monkeypatch.setattr(handler, "enqueue_photo_memory", enqueue)

    message = _message(is_bot=False, text="@VibeShkoderBot что решили?")
    await handler.save_chat_message(message, AsyncMock())

    upsert.assert_awaited_once()
    persist.assert_awaited_once()
    enqueue.assert_not_awaited()


async def test_catch_all_skips_bot_authored_message_before_any_normalized_write(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.chat_messages")
    upsert = AsyncMock()
    persist = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", upsert)
    monkeypatch.setattr(handler, "persist_message_with_policy", persist)
    monkeypatch.setattr(handler, "enqueue_photo_memory", enqueue)

    await handler.save_chat_message(
        _message(is_bot=True, text="Дневной дайджест от Шкодера"),
        AsyncMock(),
    )

    upsert.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_catch_all_queues_human_photo_after_normalized_persistence(
    monkeypatch,
) -> None:
    handler = import_module("bot.handlers.chat_messages")
    persist = AsyncMock(
        return_value=SimpleNamespace(
            policy="normal",
            is_offrecord_mark_created=False,
            chat_message=SimpleNamespace(id=802),
        )
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock())
    monkeypatch.setattr(handler, "persist_message_with_policy", persist)
    monkeypatch.setattr(handler, "enqueue_photo_memory", enqueue)
    message = _message(is_bot=False, text="", with_photo=True)
    session = AsyncMock()

    await handler.save_chat_message(message, session)

    enqueue.assert_awaited_once_with(
        session,
        message=message,
        chat_message_id=802,
    )


async def test_qa_router_persists_human_question_before_consuming_it(monkeypatch) -> None:
    """QA is above catch-all, so its human trigger must write the source question."""
    handler = import_module("bot.handlers.qa")
    trigger = import_module("bot.services.qa_trigger")
    upsert = AsyncMock()
    persist = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", upsert)
    monkeypatch.setattr(handler, "persist_message_with_policy", persist)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=False))

    message = _message(is_bot=False, text="@VibeShkoderBot что решили?")
    question = trigger.TriggeredQuestion(
        query="что решили?",
        via_mention=True,
        via_reply=False,
        was_truncated=False,
    )
    await handler.mention_question_handler(message, question, AsyncMock())

    upsert.assert_awaited_once()
    persist.assert_awaited_once()
