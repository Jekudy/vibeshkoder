from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

logger = logging.getLogger(__name__)


async def create_invite(bot: Bot, chat_id: int, app_id: int, user_id: int) -> str:
    """Create a one-time invite link for the community chat."""
    link = await bot.create_chat_invite_link(
        chat_id=chat_id,
        member_limit=1,
        creates_join_request=False,
        name=f"app-{app_id}",
    )
    logger.info("Created invite for app %s bound to user %s", app_id, user_id)
    return link.invite_link


async def try_send_invite(
    bot: Bot, chat_id: int, user_id: int, app_id: int
) -> tuple[bool, str | None]:
    """Try to create an invite and send it to the user.

    Returns (success, invite_link_or_None).
    If sending the DM fails due to privacy, returns (False, None).
    """
    try:
        invite_link = await create_invite(bot, chat_id, app_id, user_id)
    except Exception:
        logger.exception("Failed to create invite link for app %s", app_id)
        return False, None

    try:
        from bot.texts import INVITE_LINK_MSG

        await bot.send_message(
            chat_id=user_id,
            text=INVITE_LINK_MSG.format(link=invite_link),
        )
        return True, invite_link
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning(
            "Cannot DM user %s for app %s: %s", user_id, app_id, exc
        )
        return False, None
