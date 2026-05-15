"""Admin notification helper for Phase 7 / T7-05.

Sends a DM to the first admin in ``settings.ADMIN_IDS`` on actionable
failure modes. NOT triggered on transient / expected failure modes
(empty window, unset destination) — those are operator-routine.

Per PHASE7_PLAN.md §5.J: admin-notify fires on
- ``cost_exceeded``
- ``redacted_edit_failed``
- markdown render errors / TelegramBadRequest
- ``publish_lock_timeout``
- ``bot_kicked_from_posted_chat_id`` (privacy gap)

Silent on:
- ``skipped`` (empty window)
- ``skipped_no_destination``
- transient LLM errors that get retried (Phase 7 does no auto-retry; but
  the notify-on-failure path covers actionable subset only).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.config import settings
from bot.html_escape import html_escape

logger = logging.getLogger(__name__)


async def notify_admins_digest_failure(
    bot: Bot,
    *,
    digest_id: int | None,
    status: str,
    error_text: str,
) -> None:
    """Send DM to first admin in ``settings.ADMIN_IDS``.

    Silent on send failures (admin DM is informational; no retry).
    """
    admins = list(settings.ADMIN_IDS) if settings.ADMIN_IDS else []
    if not admins:
        logger.warning("notify_admins_digest_failure: no ADMIN_IDS configured")
        return
    target = admins[0]
    body = (
        "<b>⚠️ Phase 7 digest alert</b>\n\n"
        f"digest_id: <code>{html_escape(str(digest_id))}</code>\n"
        f"status: <code>{html_escape(status)}</code>\n"
        f"error_text: <code>{html_escape(error_text[:500])}</code>\n\n"
        "<i>Check /digest_history for the full audit trail.</i>"
    )
    try:
        await bot.send_message(chat_id=target, text=body, parse_mode="HTML")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.error(
            "notify_admins_digest_failure: failed to DM admin %s: %s",
            target,
            exc,
        )


__all__ = ["notify_admins_digest_failure"]
