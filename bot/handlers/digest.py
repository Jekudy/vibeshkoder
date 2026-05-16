"""Phase 7 + Phase 8 — admin Telegram handlers for digests.

Phase 7 (daily — preserved byte-for-byte):
- ``/digest_now [daily]`` — manual trigger. Bypasses
  ``memory.digests.daily.enabled`` feature flag (always runs when admin
  invokes). Still respects cost ceiling (separate Phase 7 bucket).
  Calls ``run_digest`` then ``publish_digest`` if a destination chat is
  configured. Idempotent on (type, window_start, window_end).
- ``/digest_preview <type> [date]`` — render the digest body + citation
  audit to the admin's DM. Does NOT post to the destination chat.
- ``/digest_history`` — list last 14 digest rows with status, citation
  count, posted message link if available.

Phase 8 (weekly — T8-06, PHASE8_PLAN.md §5.H):
- ``/digest_now weekly`` — run_digest(type='weekly') → on fresh draft
  transitions to ``awaiting_review`` (NOT publish). Status-aware match
  block for the existing-row branches (idempotency lock returns).
- ``/digest_now weekly --regenerate`` — atomic lock+audit+delete+re-run
  flow for ``rejected_by_admin`` / ``rejected_by_reaper`` only (H3
  single-transaction).
- ``/digest_review`` — list awaiting_review weekly digests (ORDER BY
  awaiting_review_at ASC, LIMIT 20). Each row shows id + window + body
  length + citations count + hours waiting + truncated body preview.
- ``/digest_approve <id>`` — dispatches ``digest_review.approve_digest``;
  renders context-aware replies for ``DigestReviewInvalidState`` /
  ``DigestReviewNotFound``.
- ``/digest_reject <id> [reason]`` — dispatches
  ``digest_review.reject_digest`` (service truncates to 1000 chars).

Non-admin calls: silent no-op (matches ``/stats`` / ``/admin_extract``
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
from bot.services.digest_review import (
    DigestReviewInvalidState,
    DigestReviewNotFound,
    approve_digest,
    reject_digest,
    transition_to_awaiting_review,
)
from bot.services.digests import (
    _acquire_idempotency_lock,
    load_digest_config,
    run_digest,
)
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


def _weekly_window_for_now_msk() -> tuple[datetime, datetime]:
    """Return the most recently completed ISO week (last Mon 00:00 MSK
    → this Mon 00:00 MSK), stored as UTC. Matches digest_weekly_job."""
    msk = ZoneInfo("Europe/Moscow")
    now_msk = datetime.now(tz=msk)
    today_msk_midnight = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_monday = today_msk_midnight.isoweekday() - 1
    this_monday_msk = today_msk_midnight - timedelta(days=days_since_monday)
    last_monday_msk = this_monday_msk - timedelta(days=7)
    return (
        last_monday_msk.astimezone(timezone.utc),
        this_monday_msk.astimezone(timezone.utc),
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


def _format_posted_link(chat_id: int | None, message_id: int | None) -> str:
    """Render a ``t.me/c/<tail>/<message_id>`` link or empty if not posted."""
    if not chat_id or not message_id:
        return ""
    chat_id_abs = abs(chat_id)
    if str(chat_id_abs).startswith("100"):
        tail = str(chat_id_abs)[3:]
        return f"https://t.me/c/{tail}/{message_id}"
    return ""


async def _handle_daily_digest_now(
    message: Message,
    bot: Bot,
    session: AsyncSession,
) -> None:
    """Phase 7 daily path. Preserved byte-for-byte from the Phase 7 baseline."""
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
        link = _format_posted_link(digest.posted_chat_id, digest.posted_message_id)
        link_block = f"\n{link}" if link else ""
        await message.answer(
            f"✅ Posted. digest_id=<code>{digest.id}</code>{link_block}",
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


async def _reply_weekly_status(
    message: Message,
    digest,
    window_start: datetime,
) -> None:
    """Render the §5.H status-aware reply for the various existing-row
    statuses returned by run_digest (idempotency hits) or after a fresh
    draft transition.

    Caller MUST handle the ``draft`` happy path separately (this function
    assumes the draft has already been transitioned to ``awaiting_review``
    and renders the awaiting-review reply for that case).
    """
    status = digest.status
    if status == "awaiting_review":
        await message.answer(
            f"📝 Weekly digest #<code>{digest.id}</code> ожидает одобрения админом. "
            f"Используйте <code>/digest_approve {digest.id}</code> или "
            f"<code>/digest_reject {digest.id} [причина]</code>.",
            parse_mode="HTML",
        )
    elif status == "approved_for_publish":
        admin_label = (
            html_escape(str(digest.published_by_admin_id))
            if digest.published_by_admin_id
            else "?"
        )
        await message.answer(
            f"⏳ Digest #<code>{digest.id}</code> одобрен админом "
            f"#<code>{admin_label}</code>, ожидает публикации. "
            f"Подождите или проверьте <code>/digest_history</code>.",
            parse_mode="HTML",
        )
    elif status == "posting":
        await message.answer(
            f"⏳ Weekly digest #<code>{digest.id}</code> уже публикуется, "
            "попробуйте через минуту.",
            parse_mode="HTML",
        )
    elif status == "posted":
        link = _format_posted_link(digest.posted_chat_id, digest.posted_message_id)
        link_block = f"\n{link}" if link else ""
        await message.answer(
            f"✅ Weekly digest #<code>{digest.id}</code> уже опубликован.{link_block}",
            parse_mode="HTML",
        )
    elif status in ("rejected_by_admin", "rejected_by_reaper"):
        await message.answer(
            f"🚫 Weekly digest #<code>{digest.id}</code> отклонён "
            f"(<code>{status}</code>). Используйте "
            "<code>/digest_now weekly --regenerate</code>, чтобы пересоздать.",
            parse_mode="HTML",
        )
    elif status == "skipped":
        await message.answer(
            f"⏭️ Skipped — недельное окно за {window_start.strftime('%Y-%m-%d')} пустое.",
            parse_mode="HTML",
        )
    elif status == "skipped_no_destination":
        await message.answer(
            f"⚠️ Draft создан (id=<code>{digest.id}</code>), но "
            "<code>DIGEST_DESTINATION_CHAT_ID</code> не настроен.",
            parse_mode="HTML",
        )
    elif status == "cost_exceeded":
        await message.answer(
            f"💰 Cost ceiling exceeded — <code>{html_escape(digest.error_text or '')}</code>",
            parse_mode="HTML",
        )
    elif status in ("redacted", "redacted_edit_failed"):
        await message.answer(
            f"⚠️ Weekly digest #<code>{digest.id}</code> был отредактирован "
            f"после forget-события (<code>{status}</code>). "
            "Use <code>/digest_history</code> for audit.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"❌ status=<code>{html_escape(status)}</code> "
            f"error=<code>{html_escape(digest.error_text or '')}</code>",
            parse_mode="HTML",
        )


async def _handle_weekly_digest_now(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    regenerate: bool,
) -> None:
    """Phase 8 weekly path (PHASE8_PLAN.md §5.H)."""
    window_start, window_end = _weekly_window_for_now_msk()
    digest_config = load_digest_config()
    gateway_config = load_gateway_config()
    admin_user_id = message.from_user.id if message.from_user else 0

    if regenerate:
        # H3 — single-transaction lock+audit+delete+rerun.
        #
        # FHR HIGH-3: the lock MUST be acquired BEFORE reading the row state.
        # Without the lock-first ordering, two parallel ``--regenerate`` calls
        # can both pass the pre-check (both see the row in ``rejected_*``)
        # then race on the lock; the second caller would audit-insert + DELETE
        # on a row that no longer matches the expected status, OR FK-violate
        # if the prior caller already DELETEd it.
        #
        # Post-fix flow:
        #   1. Acquire ``_acquire_idempotency_lock`` — blocks until exclusive.
        #   2. Re-read the row UNDER lock — state is now authoritative.
        #   3. If status NOT in ``rejected_*`` (or row missing): refuse and
        #      surface to the admin. No audit / no DELETE / no rerun.
        #   4. Insert audit + DELETE + ``run_digest`` (advisory lock is
        #      re-entrant within the same session, so ``run_digest``'s own
        #      call to ``_acquire_idempotency_lock`` is a no-op).
        try:
            await _acquire_idempotency_lock(
                session, type="weekly", window_start=window_start, window_end=window_end
            )
            # Re-read state UNDER lock — this is the authoritative view.
            existing_id_status = (
                await session.execute(
                    text(
                        "SELECT id, status FROM digests "
                        "WHERE type='weekly' AND window_start=:ws AND window_end=:we "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"ws": window_start, "we": window_end},
                )
            ).mappings().one_or_none()
            if existing_id_status is None:
                await message.answer(
                    "Нет существующего weekly-дайджеста для этого окна — "
                    "не нужен <code>--regenerate</code>; запустите без флага.",
                    parse_mode="HTML",
                )
                return
            if existing_id_status["status"] not in (
                "rejected_by_admin",
                "rejected_by_reaper",
            ):
                await message.answer(
                    f"⚠️ <code>--regenerate</code> доступен только для "
                    "<code>rejected_by_admin</code> / "
                    "<code>rejected_by_reaper</code>. "
                    f"Текущий статус digest "
                    f"#<code>{existing_id_status['id']}</code>: "
                    f"<code>{html_escape(existing_id_status['status'])}</code>.",
                    parse_mode="HTML",
                )
                return

            old_id = int(existing_id_status["id"])
            old_status = existing_id_status["status"]
            now = datetime.now(timezone.utc)
            await session.execute(
                text(
                    "INSERT INTO digest_runs ("
                    "  digest_id, status, started_at, finished_at, error_text"
                    ") VALUES ("
                    "  :did, 'regenerated_by_admin', :ts, :ts, :err"
                    ")"
                ),
                {
                    "did": old_id,
                    "ts": now,
                    "err": f"regenerated from {old_status} by admin {admin_user_id}",
                },
            )
            await session.execute(
                text(
                    "DELETE FROM digests "
                    "WHERE id=:id AND status IN ('rejected_by_admin','rejected_by_reaper')"
                ),
                {"id": old_id},
            )
            await session.flush()
            new_digest = await run_digest(
                session,
                type="weekly",
                window_start=window_start,
                window_end=window_end,
                ledger_repo=LedgerRepo(),
                provider=resolve_provider(gateway_config.provider),
                config=gateway_config,
                digest_config=digest_config,
            )
        except Exception as exc:
            logger.exception("cmd_digest_now weekly --regenerate crashed")
            await message.answer(
                f"❌ regenerate crashed: <code>{html_escape(str(exc)[:300])}</code>",
                parse_mode="HTML",
            )
            return

        # Transition fresh draft → awaiting_review (separate from regenerate
        # transaction per §5.H pseudocode).
        if new_digest.status == "draft":
            try:
                await transition_to_awaiting_review(session, digest_id=new_digest.id)
            except DigestReviewInvalidState:
                logger.exception(
                    "cmd_digest_now weekly --regenerate: transition_to_awaiting_review failed"
                )
            await session.refresh(new_digest)
            await message.answer(
                f"♻️ Regenerated. Weekly digest #<code>{new_digest.id}</code> "
                "ожидает одобрения админом. Используйте "
                f"<code>/digest_approve {new_digest.id}</code> или "
                f"<code>/digest_reject {new_digest.id} [причина]</code>.",
                parse_mode="HTML",
            )
            return
        # If for some reason the new run did not produce a draft (skipped /
        # cost_exceeded / etc.), surface its status via the standard reply.
        await _reply_weekly_status(message, new_digest, window_start)
        return

    # No --regenerate — standard /digest_now weekly path.
    try:
        digest = await run_digest(
            session,
            type="weekly",
            window_start=window_start,
            window_end=window_end,
            ledger_repo=LedgerRepo(),
            provider=resolve_provider(gateway_config.provider),
            config=gateway_config,
            digest_config=digest_config,
        )
    except Exception as exc:
        logger.exception("cmd_digest_now weekly: run_digest crashed")
        await message.answer(
            f"❌ <code>run_digest</code> crashed: <code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    if digest.status == "draft":
        try:
            await transition_to_awaiting_review(session, digest_id=digest.id)
        except DigestReviewInvalidState as exc:
            logger.warning(
                "cmd_digest_now weekly: transition_to_awaiting_review raced "
                "(digest_id=%s, current_status=%s)",
                digest.id,
                exc.current_status,
            )
            await session.refresh(digest)
            await _reply_weekly_status(message, digest, window_start)
            return
        await session.refresh(digest)
        await message.answer(
            f"📝 Weekly digest #<code>{digest.id}</code> создан и ожидает одобрения "
            f"админом — <code>/digest_approve {digest.id}</code> или "
            f"<code>/digest_reject {digest.id} [причина]</code>.",
            parse_mode="HTML",
        )
        return

    # Existing-row branches: dispatch to the status-aware reply matrix.
    await _reply_weekly_status(message, digest, window_start)


@router.message(Command("digest_now"), PrivateChatFilter())
async def cmd_digest_now(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Manual digest trigger (admin-only). Supports daily + weekly."""
    if not _is_admin(message):
        return

    raw = (command.args or "").strip()
    tokens = raw.split() if raw else []
    type_arg = tokens[0].strip().lower() if tokens else "daily"
    regenerate = any(
        tok.lower() in ("--regenerate", "regenerate") for tok in tokens[1:]
    )

    if type_arg == "weekly":
        await _handle_weekly_digest_now(message, bot, session, regenerate=regenerate)
        return

    if type_arg != "daily":
        await message.answer(
            "Использование: <code>/digest_now [daily|weekly] [--regenerate]</code>",
            parse_mode="HTML",
        )
        return

    if regenerate:
        await message.answer(
            "<code>--regenerate</code> поддерживается только для "
            "<code>/digest_now weekly</code>.",
            parse_mode="HTML",
        )
        return

    await _handle_daily_digest_now(message, bot, session)


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


@router.message(Command("digest_review"), PrivateChatFilter())
async def cmd_digest_review(
    message: Message,
    session: AsyncSession,
) -> None:
    """List weekly digests in ``awaiting_review`` (PHASE8_PLAN.md §5.H).

    SELECT id, window_start, body_markdown, jsonb_array_length(citations),
           awaiting_review_at FROM digests
    WHERE type='weekly' AND status='awaiting_review'
    ORDER BY awaiting_review_at ASC LIMIT 20.
    """
    if not _is_admin(message):
        return

    rows = (
        await session.execute(
            text(
                "SELECT id, window_start, window_end, body_markdown, "
                "       jsonb_array_length(citations) AS citation_count, "
                "       awaiting_review_at, "
                "       EXTRACT(EPOCH FROM (now() - awaiting_review_at))/3600.0 AS hours_waiting "
                "FROM digests "
                "WHERE type='weekly' AND status='awaiting_review' "
                "ORDER BY awaiting_review_at ASC NULLS LAST "
                "LIMIT 20"
            )
        )
    ).mappings().all()

    if not rows:
        await message.answer(
            "Нет дайджестов на ревью — список пуст.", parse_mode="HTML"
        )
        return

    msk = ZoneInfo("Europe/Moscow")
    lines = ["<b>Weekly digests на ревью</b>\n"]
    for r in rows:
        ws_msk = r["window_start"].astimezone(msk) if r["window_start"] else None
        date_label = ws_msk.strftime("%d.%m.%Y") if ws_msk else "?"
        body_len = len(r["body_markdown"] or "")
        hours = float(r["hours_waiting"]) if r["hours_waiting"] is not None else 0.0
        # Truncated body preview — first 300 chars of raw markdown.
        preview = (r["body_markdown"] or "")[:300]
        if r["body_markdown"] and len(r["body_markdown"]) > 300:
            preview += "…"
        line = (
            f"<code>#{r['id']}</code> {date_label} "
            f"body=<code>{body_len}</code> "
            f"cites=<code>{r['citation_count']}</code> "
            f"waiting=<code>{hours:.1f}h</code>\n"
            f"<i>{html_escape(preview)}</i>"
        )
        lines.append(line)
    body = "\n\n".join(lines)
    if len(body) > 4000:
        body = body[:3997] + "..."
    await message.answer(body, parse_mode="HTML", disable_web_page_preview=True)


def _render_invalid_state_reply(exc: DigestReviewInvalidState) -> str:
    """Format a Russian context-aware reply for the various current_status
    cases of ``DigestReviewInvalidState``."""
    did = exc.digest_id
    cs = exc.current_status
    if cs is None:
        return (
            f"⚠️ Digest #<code>{did}</code> был удалён или больше не существует."
        )
    if cs == "rejected_by_admin":
        return (
            f"🚫 Digest #<code>{did}</code> уже отклонён "
            "(<code>rejected_by_admin</code>). Используйте "
            f"<code>/digest_now weekly --regenerate</code>, чтобы пересоздать."
        )
    if cs == "rejected_by_reaper":
        return (
            f"🚫 Digest #<code>{did}</code> авто-отклонён ревизором "
            "(<code>rejected_by_reaper</code>). Используйте "
            f"<code>/digest_now weekly --regenerate</code>, чтобы пересоздать."
        )
    if cs == "posted":
        return (
            f"✅ Digest #<code>{did}</code> уже опубликован "
            "(<code>posted</code>). Действие неприменимо."
        )
    if cs == "approved_for_publish":
        return (
            f"⏳ Digest #<code>{did}</code> уже одобрен и ожидает публикации "
            "(<code>approved_for_publish</code>)."
        )
    if cs == "posting":
        return (
            f"⏳ Digest #<code>{did}</code> сейчас публикуется "
            "(<code>posting</code>) — попробуйте через минуту."
        )
    if cs == "failed":
        return (
            f"❌ Digest #<code>{did}</code> в статусе <code>failed</code> "
            f"(<code>{html_escape(exc.reason[:200])}</code>)."
        )
    return (
        f"⚠️ Digest #<code>{did}</code> в статусе "
        f"<code>{html_escape(cs)}</code> — действие неприменимо."
    )


@router.message(Command("digest_approve"), PrivateChatFilter())
async def cmd_digest_approve(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Approve a weekly digest → triggers publish (PHASE8_PLAN.md §5.H).

    Usage: ``/digest_approve <digest_id>``
    """
    if not _is_admin(message):
        return

    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Использование: <code>/digest_approve &lt;digest_id&gt;</code>",
            parse_mode="HTML",
        )
        return
    try:
        digest_id = int(raw.split()[0])
    except (ValueError, IndexError):
        await message.answer(
            f"Неверный <code>digest_id</code>: <code>{html_escape(raw[:50])}</code>.",
            parse_mode="HTML",
        )
        return

    admin_id = message.from_user.id if message.from_user else 0
    digest_config = load_digest_config()

    try:
        result = await approve_digest(
            session,
            bot=bot,
            digest_id=digest_id,
            admin_id=admin_id,
            digest_config=digest_config,
        )
    except DigestReviewNotFound:
        await message.answer(
            f"⚠️ Digest #<code>{digest_id}</code> не найден.",
            parse_mode="HTML",
        )
        return
    except DigestReviewInvalidState as exc:
        await message.answer(
            _render_invalid_state_reply(exc), parse_mode="HTML"
        )
        return
    except Exception as exc:
        logger.exception("cmd_digest_approve: approve_digest crashed")
        await message.answer(
            f"❌ <code>approve_digest</code> crashed: "
            f"<code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    link = _format_posted_link(result.posted_chat_id, result.posted_message_id)
    link_block = f"\n{link}" if link else ""
    extra = (
        f"\nerror: <code>{html_escape(result.error_text[:200])}</code>"
        if result.error_text
        else ""
    )
    await message.answer(
        f"✅ Digest #<code>{result.digest_id}</code> опубликован.{link_block}{extra}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.message(Command("digest_reject"), PrivateChatFilter())
async def cmd_digest_reject(
    message: Message,
    session: AsyncSession,
    command: CommandObject,
) -> None:
    """Reject a weekly digest → terminal ``rejected_by_admin``
    (PHASE8_PLAN.md §5.H).

    Usage: ``/digest_reject <digest_id> [reason]``
    Reason is optional free text (service truncates to 1000 chars).
    """
    if not _is_admin(message):
        return

    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Использование: <code>/digest_reject &lt;digest_id&gt; [причина]</code>",
            parse_mode="HTML",
        )
        return
    parts = raw.split(maxsplit=1)
    try:
        digest_id = int(parts[0])
    except ValueError:
        await message.answer(
            f"Неверный <code>digest_id</code>: <code>{html_escape(parts[0][:50])}</code>.",
            parse_mode="HTML",
        )
        return
    reason: str | None = parts[1].strip() if len(parts) > 1 else None
    if reason == "":
        reason = None

    admin_id = message.from_user.id if message.from_user else 0

    try:
        await reject_digest(
            session,
            digest_id=digest_id,
            admin_id=admin_id,
            reason=reason,
        )
    except DigestReviewNotFound:
        await message.answer(
            f"⚠️ Digest #<code>{digest_id}</code> не найден.",
            parse_mode="HTML",
        )
        return
    except DigestReviewInvalidState as exc:
        await message.answer(
            _render_invalid_state_reply(exc), parse_mode="HTML"
        )
        return
    except Exception as exc:
        logger.exception("cmd_digest_reject: reject_digest crashed")
        await message.answer(
            f"❌ <code>reject_digest</code> crashed: "
            f"<code>{html_escape(str(exc)[:300])}</code>",
            parse_mode="HTML",
        )
        return

    reason_echo = (
        f" Причина: <code>{html_escape(reason[:200])}</code>." if reason else ""
    )
    await message.answer(
        f"❌ Digest #<code>{digest_id}</code> отклонён.{reason_echo} "
        f"<code>/digest_now weekly --regenerate</code> — пересоздать.",
        parse_mode="HTML",
    )


__all__ = ["router"]
