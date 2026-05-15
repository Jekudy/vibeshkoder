"""Forget-cascade digest redactor — T7-05 / Phase 7.

Called from ``forget_cascade._cascade_digests`` (extension landed in
this same PR). Masks affected bullets, persists DB redaction unconditionally,
then attempts ``bot.edit_message_text`` for posted rows. Erratum follow-up
on TelegramBadRequest; admin-notify on TelegramForbiddenError (no erratum
because bot can't post — privacy stop signal per PHASE7_PLAN.md §8).

Bullet identification uses the ``citations[i].position`` bullet index that
``_parse_digest_citations`` now writes (Phase 7.5 fix #295).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.digest_admin_notify import notify_admins_digest_failure
from bot.services.digest_renderer import render_digest_html

logger = logging.getLogger(__name__)

REDACTED_BULLET_TEMPLATE = "- [REDACTED — забыто]"


def _mask_bullets_in_body(body_markdown: str, *, bullet_indices: set[int]) -> str:
    """Replace bullets at the given 0-based indices with REDACTED template.

    A bullet starts at a line beginning with ``- `` or ``• `` and extends
    until the next bullet or end of body. The TL;DR header (text before
    the first bullet) is preserved verbatim.
    """
    if not bullet_indices:
        return body_markdown
    lines = body_markdown.splitlines()
    out: list[str] = []
    current_bullet_idx = -1
    skip_until_next_bullet = False
    for line in lines:
        is_bullet_start = line.startswith("- ") or line.startswith("• ")
        if is_bullet_start:
            current_bullet_idx += 1
            if current_bullet_idx in bullet_indices:
                out.append(REDACTED_BULLET_TEMPLATE)
                skip_until_next_bullet = True
                continue
            else:
                skip_until_next_bullet = False
        if skip_until_next_bullet:
            continue
        out.append(line)
    return "\n".join(out)


async def redact_digest_for_forget(
    session: AsyncSession,
    *,
    digest_id: int,
    affected_mvids: set[int],
    affected_card_source_ids: set[str],
    bot: Bot | None,
) -> None:
    """Mask affected bullets in a digest. Always persists DB redaction; the
    Telegram side-effect is best-effort.

    Steps:
    1. SELECT FOR UPDATE with statement_timeout 5s.
    2. Resolve bullet indices from citations matching affected ids.
    3. Mask bullets + filter citations.
    4. UPDATE digests SET body / citations / status='redacted'.
    5. If posted_message_id and bot is not None, try edit_message_text
       unconditionally (bot-posted messages have no time limit).
    6. On TelegramBadRequest → post erratum follow-up. Status →
       redacted_edit_failed.
    7. On TelegramForbiddenError → admin notify, no erratum.
    """
    await session.execute(
        text("SELECT set_config('statement_timeout', '5s', true)")
    )

    try:
        digest_row = (
            await session.execute(
                text("SELECT * FROM digests WHERE id = :id FOR UPDATE"),
                {"id": digest_id},
            )
        ).mappings().one_or_none()
    except Exception:
        # statement_timeout fires here on stuck `posting` row. Per Codex
        # round-3 fix: log + skip without raise (per-event isolation).
        logger.warning(
            "redact_digest_for_forget: FOR UPDATE timed out digest_id=%s",
            digest_id,
        )
        return

    if digest_row is None:
        return

    current_status = digest_row["status"]
    if current_status not in ("draft", "posted", "redacted", "redacted_edit_failed"):
        # Terminal states (skipped, failed, cost_exceeded, etc.) — nothing to redact.
        return

    citations = digest_row["citations"] or []
    bullet_indices_to_mask: set[int] = set()
    surviving_citations: list[dict] = []
    for cit in citations:
        kind = cit.get("kind")
        cid = cit.get("id")
        position = cit.get("position", -1)
        affected = False
        if kind == "message_version":
            try:
                affected = int(cid) in affected_mvids
            except (TypeError, ValueError):
                affected = False
        elif kind == "card_source":
            affected = str(cid) in affected_card_source_ids
        if affected:
            if isinstance(position, int) and position >= 0:
                bullet_indices_to_mask.add(position)
        else:
            surviving_citations.append(cit)

    if not bullet_indices_to_mask:
        # Edge case: cited but position == -1 (TL;DR), or no citations matched.
        # Still mark redacted to record event.
        await session.execute(
            text(
                "UPDATE digests SET status='redacted', updated_at=now() "
                "WHERE id = :id AND status IN "
                "('draft','posted','redacted','redacted_edit_failed')"
            ),
            {"id": digest_id},
        )
        return

    masked = _mask_bullets_in_body(
        digest_row["body_markdown"] or "",
        bullet_indices=bullet_indices_to_mask,
    )

    await session.execute(
        text(
            "UPDATE digests "
            "SET body_markdown = :body, "
            "    citations = CAST(:cits AS jsonb), "
            "    status = 'redacted', "
            "    updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": digest_id,
            "body": masked,
            "cits": __import__("json").dumps(surviving_citations),
        },
    )

    # Telegram side-effect — best effort.
    posted_message_id = digest_row.get("posted_message_id")
    posted_chat_id = digest_row.get("posted_chat_id")
    if posted_message_id and bot is not None:
        body_html = render_digest_html(
            masked, window_start_utc=digest_row["window_start"]
        )
        try:
            await bot.edit_message_text(
                chat_id=posted_chat_id,
                message_id=posted_message_id,
                text=body_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            # Edit refused (bot still in chat) — post erratum.
            await session.execute(
                text(
                    "UPDATE digests SET status='redacted_edit_failed' "
                    "WHERE id = :id"
                ),
                {"id": digest_id},
            )
            erratum = (
                f"Дайджест за {digest_row['window_start'].strftime('%d.%m.%Y')} "
                "обновлён: цитата по запросу автора удалена. "
                "Полный текст в /digest_history."
            )
            try:
                await bot.send_message(
                    chat_id=posted_chat_id,
                    text=erratum,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception(
                    "digest_redactor: erratum follow-up failed digest_id=%s",
                    digest_id,
                )
            await notify_admins_digest_failure(
                bot,
                digest_id=digest_id,
                status="redacted_edit_failed",
                error_text=f"telegram_bad_request:{exc}",
            )
        except TelegramForbiddenError:
            # Bot kicked — no erratum possible. Privacy stop signal.
            await session.execute(
                text(
                    "UPDATE digests SET status='redacted_edit_failed' "
                    "WHERE id = :id"
                ),
                {"id": digest_id},
            )
            await notify_admins_digest_failure(
                bot,
                digest_id=digest_id,
                status="redacted_edit_failed",
                error_text="bot_kicked_from_posted_chat_id",
            )


__all__ = ["redact_digest_for_forget", "_mask_bullets_in_body"]
