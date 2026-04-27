from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.message import MessageRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter
from bot.services.normalization import extract_normalized_fields

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

    # Keep sender profile fresh for message attribution and admin lookups.
    await UserRepo.upsert(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    # T1-09/10/11: extract normalized fields (reply / thread / caption / kind) so
    # captions and media-only messages are first-class content in the archive. Falls
    # back to None for fields the message doesn't carry, preserving the legacy shape
    # for plain-text messages.
    normalized = extract_normalized_fields(message)

    # MessageRepo.save is idempotent on (chat_id, message_id) per T0-03 — duplicates
    # return the existing row instead of raising. No need for a try/except + rollback
    # that would also discard the UserRepo.upsert and set_member work above.
    # T1-11 caption is now first-class — persist raw_json whenever there is content
    # (text OR caption), not only when text is set.
    await MessageRepo.save(
        session,
        message_id=message.message_id,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=message.text,
        date=message.date,
        raw_json=message.model_dump(mode="json", exclude_none=True)
        if (message.text or message.caption)
        else None,
        **normalized,
    )
