"""Phase 7 / T7-06 — admin Telegram handlers for daily digests.

Three admin-only commands:
- ``/digest_now [daily]`` — manual trigger. Bypasses
  ``memory.digests.daily.enabled`` feature flag (always runs when admin
  invokes). Still respects cost ceiling (separate Phase 7 bucket).
  Calls ``run_digest`` then ``publish_digest`` if a destination chat is
  configured. Idempotent on (type, window_start, window_end).
- ``/digest_preview <type> [date]`` — render the digest body + citation
  audit to the admin's DM. Does NOT post to the destination chat.
- ``/digest_history`` — list last 14 digest rows with status, citation
  count, posted message link if available.

Non-admin calls: silent no-op (matches \\``/stats`` / ``/admin_extract``
precedent — no leak of digest content or even acknowledgement that
digests exist).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.html_escape import html_escape
from bot.services.digest_publisher import publish_digest
from bot.services.digest_renderer import render_digest_html
from bot.services.digests import load_digest_config, run_digest
from bot.services.llm_gateway import load_gateway_config, resolve_provider

logger = logging.getLogger(__name__)

router = Router(name="digest_admin")


def _is_admin(message: Message) -> bool:
    if message.from_user is None:
        return False
    return message.from_user.id in settings.ADMIN_IDS


def _daily_window_for_today_msk() -> tuple[datetime, datetime]:
    """Return the standard daily window: yesterday 00:00 MSK..today 00:00 MSK
    (stored as UTC). Matches scheduler.digest_daily_job."""
    msk = ZoneInfo("Europe/Moscow")
    now_msk = datetime.now(tz=msk)
    today_msk_midnight = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_msk_midnight = today_msk_midnight - timedelta(days=1)
    return (
        yesterday_msk_midnight.astimezone(timezone.utc),
        today_msk_midnight.astimezone(timezone.utc),
    )


def _parse_date_for_preview(date_str: str) -> tuple[datetime, datetime]:
    """Parse YYYY-MM-DD as a MSK day. Returns (window_start_utc, window_end_utc)."""
    msk = ZoneInfo("Europe/Moscow")
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=msk)
    day_start_msk = d.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_msk = day_start_msk + timedelta(days=1)
    return (
        day_start_msk.astimezone(timezone.utc),
        day_end_msk.astimezone(timezone.utc),
    )


@router.message(Command("digest_now"), PrivateChatFilter())
async def cmd_digest_now(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Manual digest trigger (admin-only)."""
    if not _is_admin(message):
        return

    arg = (command.args or "").strip().lower()
    if not arg:
        arg = "daily"
    if arg == "weekly":
        await message.answer(
            "Weekly дайджест появится в Phase 8.", parse_mode="HTML"
        )
        return
    if arg != "daily":
        await message.answer(
            "Использование: <code>/digest_now [daily]</code>", parse_mode="HTML"
        )
        return

    window_start, window_end = _daily_window_for_today_msk()
    digest_config = load_digest_config()
    gateway_config = load_gateway_config()
    try:
        digest = await run_digest(
            session,
            type="daily",
            window_start=window_start,
            window_end=window_end,
            ledger_repo=LedgerRepo(),
            provider=resolve_provider(gateway_config.provider),
            config=gateway_config,
            digest_config=digest_config,
        )
    except Exception as exc:
        logger.exception("cmd_digest_now: run_digest crashed")
        await message.answer(
            f"❌ <code>run_digest</code> crashed: <code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    status = digest.status

    # F6: handle concurrent publish in-flight — reaper will clean up stale
    # 'posting' rows; admin should retry in a moment rather than re-publishing.
    if status == "posting":
        import asyncio

        await asyncio.sleep(1)
        await session.refresh(digest)
        status = digest.status
        if status == "posting":
            await message.answer(
                "Дайджест уже публикуется, попробуйте через минуту.",
                parse_mode="HTML",
            )
            return
        # fall through with refreshed status

    if status == "draft":
        try:
            digest = await publish_digest(
                session, bot=bot, digest=digest, digest_config=digest_config
            )
        except Exception as exc:
            logger.exception("cmd_digest_now: publish_digest crashed")
            await message.answer(
                f"❌ <code>publish_digest</code> crashed: <code>{html_escape(str(exc)[:300])}</code>",
                parse_mode="HTML",
            )
            return
        status = digest.status

    if status == "posted":
        link = ""
        if digest.posted_chat_id and digest.posted_message_id:
            # Convert -100xxxxx → public-link form for supergroups.
            chat_id_abs = abs(digest.posted_chat_id)
            if str(chat_id_abs).startswith("100"):
                tail = str(chat_id_abs)[3:]
                link = f"\nhttps://t.me/c/{tail}/{digest.posted_message_id}"
        await message.answer(
            f"✅ Posted. digest_id=<code>{digest.id}</code>{link}",
            parse_mode="HTML",
        )
    elif status == "skipped":
        await message.answer(
            f"⏭️ Skipped — окно за {window_start.strftime('%Y-%m-%d')} пустое.",
            parse_mode="HTML",
        )
    elif status == "skipped_no_destination":
        await message.answer(
            f"⚠️ Draft создан (id=<code>{digest.id}</code>), но "
            "<code>DIGEST_DESTINATION_CHAT_ID</code> не настроен — не запостил.",
            parse_mode="HTML",
        )
    elif status == "cost_exceeded":
        await message.answer(
            f"💰 Cost ceiling exceeded — <code>{html_escape(digest.error_text or '')}</code>",
            parse_mode="HTML",
        )
    elif status in ("redacted", "redacted_edit_failed"):
        await message.answer(
            f"⚠️ Existing digest for this window was redacted after a forget event "
            f"(id=<code>{digest.id}</code>, status=<code>{status}</code>). "
            "Use <code>/digest_history</code> for audit.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"❌ status=<code>{html_escape(status)}</code> "
            f"error=<code>{html_escape(digest.error_text or '')}</code>",
            parse_mode="HTML",
        )


@router.message(Command("digest_preview"), PrivateChatFilter())
async def cmd_digest_preview(
    message: Message,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Render a digest body + citation audit to admin DM. NO post."""
    if not _is_admin(message):
        return

    parts = (command.args or "").split()
    digest_type = parts[0].strip().lower() if parts else "daily"
    if digest_type == "weekly":
        await message.answer(
            "Weekly дайджест появится в Phase 8.", parse_mode="HTML"
        )
        return
    if digest_type != "daily":
        await message.answer(
            "Использование: <code>/digest_preview daily [YYYY-MM-DD]</code>",
            parse_mode="HTML",
        )
        return

    if len(parts) >= 2:
        try:
            window_start, window_end = _parse_date_for_preview(parts[1])
        except ValueError:
            await message.answer(
                "Неверный формат даты. Используйте YYYY-MM-DD.",
                parse_mode="HTML",
            )
            return
    else:
        window_start, window_end = _daily_window_for_today_msk()

    # Find existing digest (no synthesis).
    row = (
        await session.execute(
            text(
                "SELECT id, body_markdown, citations, status, posted_message_id, "
                "       posted_chat_id, error_text "
                "FROM digests "
                "WHERE type = :t AND window_start = :ws AND window_end = :we "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"t": digest_type, "ws": window_start, "we": window_end},
        )
    ).mappings().one_or_none()

    if row is None:
        await message.answer(
            "Дайджест для этого окна ещё не сгенерирован. "
            "Используйте <code>/digest_now daily</code>.",
            parse_mode="HTML",
        )
        return

    if row["body_markdown"]:
        rendered = render_digest_html(
            row["body_markdown"], window_start_utc=window_start
        )
    else:
        rendered = (
            f"<i>(no body — status=<code>{html_escape(row['status'])}</code>)</i>"
        )
    citations = row["citations"] or []
    cite_lines = [
        f"  - kind=<code>{html_escape(str(c.get('kind')))}</code> "
        f"id=<code>{html_escape(str(c.get('id')))}</code> "
        f"position=<code>{c.get('position')}</code>"
        for c in citations
    ]
    cite_block = "\n".join(cite_lines) if cite_lines else "  (no citations)"
    audit = (
        f"\n\n<b>Audit</b>\n"
        f"digest_id: <code>{row['id']}</code>\n"
        f"status: <code>{html_escape(row['status'])}</code>\n"
        f"citations ({len(citations)}):\n{cite_block}"
    )
    body = rendered + audit
    if len(body) > 4000:
        body = body[:3997] + "..."
    await message.answer(body, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("digest_history"), PrivateChatFilter())
async def cmd_digest_history(
    message: Message,
    session: AsyncSession,
) -> None:
    """List last 14 digests with status + citation count."""
    if not _is_admin(message):
        return

    rows = (
        await session.execute(
            text(
                "SELECT id, type, window_start, window_end, status, "
                "       jsonb_array_length(citations) AS citation_count, "
                "       posted_chat_id, posted_message_id, error_text "
                "FROM digests "
                "ORDER BY id DESC "
                "LIMIT 14"
            )
        )
    ).mappings().all()

    if not rows:
        await message.answer(
            "История пуста — ни один дайджест не создавался.",
            parse_mode="HTML",
        )
        return

    msk = ZoneInfo("Europe/Moscow")
    lines = ["<b>Последние дайджесты</b>\n"]
    for r in rows:
        ws_msk = r["window_start"].astimezone(msk)
        date_label = ws_msk.strftime("%d.%m.%Y")
        line = (
            f"<code>#{r['id']}</code> {date_label} "
            f"[{html_escape(r['type'])}] "
            f"<b>{html_escape(r['status'])}</b> "
            f"cites=<code>{r['citation_count']}</code>"
        )
        if r["posted_chat_id"] and r["posted_message_id"]:
            chat_id_abs = abs(r["posted_chat_id"])
            if str(chat_id_abs).startswith("100"):
                tail = str(chat_id_abs)[3:]
                line += f" <a href='https://t.me/c/{tail}/{r['posted_message_id']}'>link</a>"
        if r["error_text"]:
            line += f" err=<code>{html_escape(r['error_text'][:80])}</code>"
        lines.append(line)
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3997] + "..."
    await message.answer(body, parse_mode="HTML", disable_web_page_preview=True)


__all__ = ["router"]
