from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import TelegramUpdate
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter
from bot.services.message_persistence import persist_message_with_policy

logger = logging.getLogger(__name__)

router = Router(name="chat_messages")


@router.message(GroupChatFilter())
async def save_chat_message(
    message: Message,
    session: AsyncSession,
    raw_update: TelegramUpdate | None = None,
) -> None:
    """Save every message in the community group chat."""
    # Handler-only guards (community chat filter, group filter, anonymous sender).
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

    # Persist with policy detection (advisory lock + governance + mark creation).
    # The helper mirrors the former inline logic from this handler body so DB state
    # is byte-identical regardless of call site (live handler or importer).
    result = await persist_message_with_policy(
        session,
        message,
        raw_update_id=raw_update.id if raw_update is not None else None,
    )

    logger.debug(
        "chat_message saved: chat_id=%s message_id=%s policy=%s mark_created=%s",
        message.chat.id,
        message.message_id,
        result.policy,
        result.is_offrecord_mark_created,
    )
