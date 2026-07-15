"""Durably persist every human community message before router dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from bot.config import settings
from bot.db.repos.user import UserRepo
from bot.services.image_memory import enqueue_photo_memory
from bot.services.message_persistence import persist_message_with_policy


class NormalizedMemoryPersistenceMiddleware(BaseMiddleware):
    """Archive human messages even when a higher-priority router consumes them.

    The archive transaction commits before the product handler runs.  A later
    command failure can therefore roll back its own side effects without
    silently losing the source conversation.  Bot-authored output is excluded
    from normalized/derived memory to prevent feedback loops; raw update
    persistence remains the audit boundary for those updates.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or event.message is None:
            return await handler(event, data)

        message = event.message
        sender = message.from_user
        if sender is None or sender.is_bot or message.chat.id != settings.COMMUNITY_CHAT_ID:
            return await handler(event, data)

        session = data.get("session")
        if session is None:
            raise RuntimeError("normalized memory middleware requires a DB session")

        await UserRepo.upsert(
            session,
            telegram_id=sender.id,
            username=sender.username,
            first_name=sender.first_name,
            last_name=sender.last_name,
        )
        raw_update = data.get("raw_update")
        result = await persist_message_with_policy(
            session,
            message,
            raw_update_id=raw_update.id if raw_update is not None else None,
            source="live",
        )
        if message.photo:
            await enqueue_photo_memory(
                session,
                message=message,
                chat_message_id=result.chat_message.id,
            )
        await session.commit()
        data["normalized_memory_persisted"] = result
        return await handler(event, data)


__all__ = ["NormalizedMemoryPersistenceMiddleware"]
