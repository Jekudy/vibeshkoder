from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter, PrivateChatFilter

logger = logging.getLogger(__name__)

router = Router(name="admin")


@router.message(Command("chatid"), GroupChatFilter())
async def cmd_chatid(message: Message) -> None:
    """Reply with the chat ID (group only)."""
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>", parse_mode="HTML")


@router.message(Command("stats"), PrivateChatFilter())
async def cmd_stats(
    message: Message,
    session: AsyncSession,
) -> None:
    """Show funnel stats (private, admin only)."""
    if message.from_user is None:
        return

    if message.from_user.id not in settings.ADMIN_IDS:
        return

    stats = await ApplicationRepo.get_funnel_stats(session)
    members = await UserRepo.get_members(session)
    all_intros = await IntroRepo.get_all(session)
    members_without_intro = await IntroRepo.get_members_without_intro(session)

    lines = [
        "📊 <b>Статистика</b>\n",
        f"Участников в чате: {len(members)}",
        f"Всего интро: {len(all_intros)}",
        f"Участников без интро: {len(members_without_intro)}",
        "",
        "<b>Заявки по статусам:</b>",
    ]

    for status, count in sorted(stats.items()):
        lines.append(f"  {status}: {count}")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("force_refresh"), PrivateChatFilter())
async def cmd_force_refresh(
    message: Message,
    session: AsyncSession,
) -> None:
    """Trigger intro refresh cycle for all stale intros (admin only)."""
    if message.from_user is None:
        return

    if message.from_user.id not in settings.ADMIN_IDS:
        return

    from bot.services.scheduler import check_intro_refresh

    await check_intro_refresh(message.bot)
    await message.answer("Цикл обновления интро запущен.")
