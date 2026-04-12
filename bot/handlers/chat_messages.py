from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.message import MessageRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter

logger = logging.getLogger(__name__)

router = Router(name="chat_messages")


@router.message(GroupChatFilter())
async def save_chat_message(
    message: Message,
    session: AsyncSession,
) -> None:
    """Save every message in the community group chat."""
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        return

    if message.from_user is None:
        return

    # Upsert user and mark as member (if they're writing in the chat, they're a member)
    await UserRepo.upsert(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    await UserRepo.set_member(session, message.from_user.id, is_member=True)
    logger.info("Marked user %s as member from chat message", message.from_user.id)

    try:
        await MessageRepo.save(
            session,
            message_id=message.message_id,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=message.text,
            date=message.date,
            raw_json=message.model_dump(mode="json", exclude_none=True)
            if message.text
            else None,
        )
    except Exception:
        await session.rollback()
        logger.debug(
            "Failed to save message %s in chat %s, skipping",
            message.message_id,
            message.chat.id,
        )
