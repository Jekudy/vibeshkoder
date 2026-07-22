"""Admin-only immediate trigger for automatic daily and weekly digests."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.html_escape import html_escape
from bot.services.digest_publisher import publish_digest
from bot.services.digest_windows import completed_daily_window, completed_weekly_window
from bot.services.digests import load_digest_config, run_digest
from bot.services.llm_gateway import load_digest_gateway_config, resolve_digest_provider

logger = logging.getLogger(__name__)
router = Router(name="digest_admin")


def _is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in settings.ADMIN_IDS


async def _run_and_publish(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    digest_type: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    digest_config = load_digest_config()
    gateway_config = load_digest_gateway_config(digest_type=digest_type)  # type: ignore[arg-type]
    try:
        digest = await run_digest(
            session,
            type=digest_type,
            window_start=window_start,
            window_end=window_end,
            ledger_repo=LedgerRepo(),
            provider=resolve_digest_provider(),
            config=gateway_config,
            digest_config=digest_config,
        )
    except Exception as exc:
        logger.exception("cmd_digest_now: run_digest crashed")
        await message.answer(f"❌ <code>{html_escape(str(exc)[:300])}</code>", parse_mode="HTML")
        return

    if digest.status == "posting":
        await asyncio.sleep(1)
        await session.refresh(digest)
    if digest.status == "draft":
        try:
            digest = await publish_digest(
                session, bot=bot, digest=digest, digest_config=digest_config
            )
        except Exception as exc:
            logger.exception("cmd_digest_now: publish_digest crashed")
            await message.answer(
                f"❌ <code>{html_escape(str(exc)[:300])}</code>", parse_mode="HTML"
            )
            return
    if digest.status == "posted":
        await message.answer(f"✅ Published. digest_id=<code>{digest.id}</code>", parse_mode="HTML")
    elif digest.status == "skipped":
        await message.answer("⏭️ Окно пустое.", parse_mode="HTML")
    elif digest.status == "posting":
        await message.answer(
            "⏳ Дайджест уже публикуется, попробуйте через минуту.", parse_mode="HTML"
        )
    else:
        await message.answer(
            f"❌ status=<code>{html_escape(digest.status)}</code> "
            f"error=<code>{html_escape(digest.error_text or '')}</code>",
            parse_mode="HTML",
        )


@router.message(Command("digest_now"), PrivateChatFilter())
async def cmd_digest_now(
    message: Message, bot: Bot, session: AsyncSession, command: CommandObject
) -> None:
    """Generate and immediately publish a daily or weekly digest."""
    if not _is_admin(message):
        return
    tokens = (command.args or "").split()
    digest_type = tokens[0].lower() if tokens else "weekly"
    if digest_type not in {"daily", "weekly"} or len(tokens) > 1:
        await message.answer(
            "Использование: <code>/digest_now [daily|weekly]</code>", parse_mode="HTML"
        )
        return
    if digest_type == "daily" and not await FeatureFlagRepo.get(
        session, "memory.digests.daily.enabled"
    ):
        await message.answer(
            "⏸️ Ежедневный дайджест выключен: "
            "<code>memory.digests.daily.enabled=false</code>.",
            parse_mode="HTML",
        )
        return
    now = datetime.now(timezone.utc)
    window_start, window_end = (
        completed_daily_window(now) if digest_type == "daily" else completed_weekly_window(now)
    )
    await _run_and_publish(
        message,
        bot,
        session,
        digest_type=digest_type,
        window_start=window_start,
        window_end=window_end,
    )
