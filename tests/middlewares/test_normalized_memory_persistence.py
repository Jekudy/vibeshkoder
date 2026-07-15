from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, PhotoSize, Update, User


pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


def _update(*, is_bot: bool = False, photo: bool = False) -> Update:
    return Update(
        update_id=9001,
        message=Message(
            message_id=7001,
            date=datetime.now(timezone.utc),
            chat=Chat(id=COMMUNITY_CHAT_ID, type="supergroup"),
            from_user=User(
                id=701,
                is_bot=is_bot,
                first_name="Shkoder" if is_bot else "Member",
                username="shkoder_bot" if is_bot else "member",
            ),
            text=None if photo else "/chatid",
            photo=(
                [
                    PhotoSize(
                        file_id="photo-file",
                        file_unique_id="photo-unique",
                        width=1200,
                        height=900,
                    )
                ]
                if photo
                else None
            ),
        ),
    )


async def test_persists_command_before_specific_handler_and_commits(monkeypatch):
    from bot.middlewares import normalized_memory_persistence as module

    order: list[str] = []
    session = AsyncMock()

    async def commit() -> None:
        order.append("commit")

    async def downstream(event, data):
        order.append("handler")

    session.commit.side_effect = commit
    monkeypatch.setattr(module.UserRepo, "upsert", AsyncMock())
    persist = AsyncMock(return_value=SimpleNamespace(chat_message=SimpleNamespace(id=991)))
    monkeypatch.setattr(module, "persist_message_with_policy", persist)
    monkeypatch.setattr(module, "enqueue_photo_memory", AsyncMock())
    middleware = module.NormalizedMemoryPersistenceMiddleware()

    await middleware(
        downstream,
        _update(),
        {"session": session, "raw_update": SimpleNamespace(id=88)},
    )

    persist.assert_awaited_once()
    assert persist.call_args.kwargs["raw_update_id"] == 88
    assert order == ["commit", "handler"]


async def test_queues_photo_even_when_later_handler_consumes_message(monkeypatch):
    from bot.middlewares import normalized_memory_persistence as module

    session = AsyncMock()
    update = _update(photo=True)
    monkeypatch.setattr(module.UserRepo, "upsert", AsyncMock())
    monkeypatch.setattr(
        module,
        "persist_message_with_policy",
        AsyncMock(return_value=SimpleNamespace(chat_message=SimpleNamespace(id=992))),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(module, "enqueue_photo_memory", enqueue)

    await module.NormalizedMemoryPersistenceMiddleware()(
        AsyncMock(),
        update,
        {"session": session},
    )

    enqueue.assert_awaited_once_with(
        session,
        message=update.message,
        chat_message_id=992,
    )
    session.commit.assert_awaited_once()


async def test_bot_output_is_not_written_to_normalized_memory(monkeypatch):
    from bot.middlewares import normalized_memory_persistence as module

    upsert = AsyncMock()
    persist = AsyncMock()
    enqueue = AsyncMock()
    downstream = AsyncMock()
    session = AsyncMock()
    monkeypatch.setattr(module.UserRepo, "upsert", upsert)
    monkeypatch.setattr(module, "persist_message_with_policy", persist)
    monkeypatch.setattr(module, "enqueue_photo_memory", enqueue)

    await module.NormalizedMemoryPersistenceMiddleware()(
        downstream,
        _update(is_bot=True),
        {"session": session},
    )

    upsert.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()
    session.commit.assert_not_awaited()
    downstream.assert_awaited_once()
