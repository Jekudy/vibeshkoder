from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.intro import IntroRepo
from bot.db.repos.message import MessageRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.html_escape import html_escape
from bot.texts import (
    FORWARD_INTRO_RESULT,
    FORWARD_NO_INTRO,
    FORWARD_NO_TEXT,
    FORWARD_NOT_FOUND,
)

logger = logging.getLogger(__name__)

router = Router(name="forward_lookup")


@router.message(PrivateChatFilter(), F.forward_origin)
async def handle_forwarded_message(
    message: Message,
    session: AsyncSession,
) -> None:
    """Handle forwarded messages in private chat — look up author's intro."""

    if message.from_user is None:
        return

    requester = await UserRepo.get(session, message.from_user.id)
    if requester is None:
        logger.info(
            "forward_lookup denied for non-member user_id=%s",
            message.from_user.id,
        )
        return

    is_admin = requester.id in settings.ADMIN_IDS or requester.is_admin is True
    if requester.is_member is not True and not is_admin:
        logger.info(
            "forward_lookup denied for non-member user_id=%s",
            message.from_user.id,
        )
        return

    text = message.text
    if not text:
        await message.answer(FORWARD_NO_TEXT)
        return

    # Search by exact text match
    chat_msg = await MessageRepo.find_by_exact_text(session, text)
    if chat_msg is None:
        await message.answer(FORWARD_NOT_FOUND)
        return

    # Get user info
    user = await UserRepo.get(session, chat_msg.user_id)
    if user is None:
        await message.answer(FORWARD_NOT_FOUND)
        return

    name = user.first_name
    username = user.username or "no_username"

    # Get intro
    intro = await IntroRepo.get(session, user.id)
    if intro is not None:
        await message.answer(
            FORWARD_INTRO_RESULT.format(
                name=html_escape(name),
                username=html_escape(username),
                intro_text=intro.intro_text,
            )
        )
    else:
        await message.answer(
            FORWARD_NO_INTRO.format(
                name=html_escape(name),
                username=html_escape(username),
            )
        )
