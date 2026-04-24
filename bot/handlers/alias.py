from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.repos.member_tag import MemberTagRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter

logger = logging.getLogger(__name__)

router = Router(name="alias")

ALIAS_COOLDOWN = timedelta(hours=1)
MAX_TAG_LENGTH = 16
MISSING_TAG_RIGHTS_TEXT = "Нужны права админа с управлением тегами."


def normalize_member_tag(raw: str | None) -> str | None:
    if raw is None:
        return None

    cleaned_chars: list[str] = []
    for char in raw.strip():
        if _is_disallowed_tag_char(char):
            continue
        cleaned_chars.append(char)

    cleaned = "".join(cleaned_chars).strip()
    if not cleaned or cleaned == "-":
        return None

    return cleaned[:MAX_TAG_LENGTH]


def _is_disallowed_tag_char(char: str) -> bool:
    codepoint = ord(char)
    if codepoint in (0x200D, 0xFE0E, 0xFE0F, 0x20E3):
        return True

    if unicodedata.category(char) == "So" and codepoint >= 0x2600:
        return True

    emoji_ranges = (
        (0x1F000, 0x1FAFF),
        (0x2600, 0x27BF),
    )
    return any(start <= codepoint <= end for start, end in emoji_ranges)


async def _bot_can_manage_tags(message: Message) -> bool:
    bot_user = await message.bot.me()
    bot_member = await message.bot.get_chat_member(message.chat.id, bot_user.id)
    return bool(getattr(bot_member, "can_manage_tags", False))


def _uses_admin_title(chat_type: str, member_status: str) -> bool:
    return chat_type == "supergroup" and member_status in {"administrator", "creator"}


@router.message(Command("alias"), GroupChatFilter())
async def cmd_alias(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    if message.from_user is None:
        return

    await UserRepo.upsert(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    if not await _bot_can_manage_tags(message):
        await message.reply(MISSING_TAG_RIGHTS_TEXT)
        return

    now = datetime.now(timezone.utc)
    cooldown = await MemberTagRepo.get(session, message.chat.id, message.from_user.id)
    if cooldown is not None and now - cooldown.changed_at < ALIAS_COOLDOWN:
        return

    tag = normalize_member_tag(command.args)
    sender_member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    use_admin_title = _uses_admin_title(message.chat.type, sender_member.status)

    try:
        if use_admin_title:
            await message.bot.set_chat_administrator_custom_title(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                custom_title=tag or "",
            )
        else:
            await message.bot.set_chat_member_tag(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                tag=tag,
            )
    except TelegramBadRequest:
        logger.info(
            "Failed to set alias in chat %s for user %s",
            message.chat.id,
            message.from_user.id,
            exc_info=True,
        )
        return

    await MemberTagRepo.set(
        session,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        tag=tag,
        changed_at=now,
    )
