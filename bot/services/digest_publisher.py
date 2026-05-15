"""Telegram digest publisher — T7-05 / Phase 7.

Holds the digest row lock ACROSS ``bot.send_message`` in a single
transaction (PHASE7_PLAN.md §5.F). This eliminates the race window where
a forget cascade could see the row unlocked while Telegram is in-flight.

Status state machine:
    draft → posting → posted     (success)
    draft → posting → failed     (TelegramBadRequest / format error)
    draft → posting → failed     (TelegramForbiddenError / bot kicked)
    draft → skipped_no_destination  (destination not configured)
    draft → failed (publish_lock_timeout)  (3 NOWAIT retries exhausted)

The publisher never re-publishes a row that's already terminal. The admin
``/digest_now`` handler may invoke this on an existing draft (orphan
recovery path, see PHASE7_PLAN.md §5.I).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Digest, DigestRun
from bot.services.digest_admin_notify import notify_admins_digest_failure
from bot.services.digest_renderer import render_digest_html
from bot.services.digests import DigestConfig

logger = logging.getLogger(__name__)


class DigestPublisherInvalidState(Exception):
    """Row was not in ``draft`` state when the publisher acquired the lock."""


async def _digest_revalidate_citations(
    session: AsyncSession, *, digest: Digest
) -> bool:
    """Defense-in-depth: re-check every citation source id is still visible.

    Returns True if all cited sources are clean, False if any failed
    revalidation (caller should mark digest 'failed' and skip publish).
    """
    citations = digest.citations or []
    mv_ids = [int(c["id"]) for c in citations if c.get("kind") == "message_version"]
    cs_ids = [str(c["id"]) for c in citations if c.get("kind") == "card_source"]

    if mv_ids:
        result = await session.execute(
            text(
                "SELECT mv.id FROM message_versions mv "
                "JOIN chat_messages cm ON cm.id = mv.chat_message_id "
                "WHERE mv.id = ANY(:mv_ids) "
                "  AND cm.memory_policy = 'normal' "
                "  AND mv.is_redacted = FALSE "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM forget_events fe "
                "      WHERE fe.status IN ('pending','processing','completed') "
                "        AND ( "
                "            (fe.target_type = 'message' AND fe.target_id = cm.id::text) "
                "            OR (fe.target_type = 'user' AND fe.target_id = cm.user_id::text) "
                "            OR (fe.target_type = 'message_hash' AND fe.target_id = mv.content_hash) "
                "        ) "
                "  )"
            ),
            {"mv_ids": mv_ids},
        )
        visible = {r[0] for r in result.all()}
        if set(mv_ids) - visible:
            return False
    if cs_ids:
        result = await session.execute(
            text(
                "SELECT cs.id::text FROM card_sources cs "
                "JOIN knowledge_cards kc ON kc.id = cs.card_id "
                "JOIN message_versions mv ON mv.id = cs.message_version_id "
                "JOIN chat_messages cm ON cm.id = mv.chat_message_id "
                "WHERE cs.id::text = ANY(:cs_ids) "
                "  AND kc.card_status = 'approved' "
                "  AND cm.memory_policy = 'normal' "
                "  AND mv.is_redacted = FALSE "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM forget_events fe "
                "      WHERE fe.status IN ('pending','processing','completed') "
                "        AND ( "
                "            (fe.target_type = 'message' AND fe.target_id = cm.id::text) "
                "            OR (fe.target_type = 'user' AND fe.target_id = cm.user_id::text) "
                "            OR (fe.target_type = 'message_hash' AND fe.target_id = mv.content_hash) "
                "        ) "
                "  )"
            ),
            {"cs_ids": cs_ids},
        )
        visible = {r[0] for r in result.all()}
        if set(cs_ids) - visible:
            return False
    return True


async def publish_digest(
    session: AsyncSession,
    *,
    bot: Bot,
    digest: Digest,
    digest_config: DigestConfig,
) -> Digest:
    """Publish a digest to its destination chat.

    Single long-lived transaction holding the row lock across send_message.
    Caller MUST commit the session after this returns OR roll back on raise.
    """
    if digest.status != "draft":
        raise DigestPublisherInvalidState(
            f"expected status='draft', got {digest.status!r}"
        )

    # If no destination, skip publication cleanly.
    if digest_config.destination_chat_id is None:
        digest.status = "skipped_no_destination"
        digest.updated_at = datetime.now(timezone.utc)
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="skipped_no_destination",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        return digest

    # Set transaction-local timeout (covers 5s lock wait + ~20s Telegram + buffer).
    await session.execute(
        text("SELECT set_config('statement_timeout', '30s', true)")
    )

    # Try FOR UPDATE NOWAIT with up to 3 backoff retries.
    locked = False
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await session.execute(
                text("SELECT id FROM digests WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": digest.id},
            )
            locked = True
            break
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.1 * (2**attempt))
    if not locked:
        # Fresh transaction guard: only update if still 'draft' (race-safe).
        # Note: we're still inside the same session — for true fresh-tx,
        # caller would need a separate session. For this implementation we
        # update + log + raise; the scheduler wrapper rolls back outer tx.
        logger.warning(
            "publish_digest: lock acquisition exhausted (3 retries) digest_id=%s",
            digest.id,
        )
        digest.status = "failed"
        digest.error_text = "publish_lock_timeout"
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="failed",
                error_text="publish_lock_timeout",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        await notify_admins_digest_failure(
            bot,
            digest_id=digest.id,
            status="failed",
            error_text=f"publish_lock_timeout (last={last_exc!r})",
        )
        return digest

    # Transition draft → posting (in same transaction). Row remains locked.
    digest.status = "posting"
    digest.posting_started_at = datetime.now(timezone.utc)
    digest.updated_at = datetime.now(timezone.utc)
    await session.flush()

    # Defense-in-depth revalidation — block forgotten-source publish even
    # if it slipped past gateway-side check.
    if not await _digest_revalidate_citations(session, digest=digest):
        digest.status = "failed"
        digest.error_text = "citations_stale_at_publish"
        digest.posting_started_at = None
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="failed",
                error_text="citations_stale_at_publish",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        await notify_admins_digest_failure(
            bot,
            digest_id=digest.id,
            status="failed",
            error_text="citations_stale_at_publish",
        )
        return digest

    # Render + send.
    body_html = render_digest_html(
        digest.body_markdown or "",
        window_start_utc=digest.window_start,
    )
    try:
        sent = await bot.send_message(
            chat_id=digest_config.destination_chat_id,
            text=body_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        digest.status = "failed"
        digest.error_text = str(exc)[:500]
        digest.posting_started_at = None
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="failed",
                error_text=str(exc)[:2000],
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        await notify_admins_digest_failure(
            bot, digest_id=digest.id, status="failed", error_text=str(exc)
        )
        return digest
    except TelegramForbiddenError:
        digest.status = "failed"
        digest.error_text = "bot_not_in_destination"
        digest.posting_started_at = None
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="failed",
                error_text="bot_not_in_destination",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        await notify_admins_digest_failure(
            bot,
            digest_id=digest.id,
            status="failed",
            error_text="bot_not_in_destination",
        )
        return digest

    # Success: posting → posted (guarded by status='posting' to prevent
    # racing with reaper).
    update_result = await session.execute(
        text(
            "UPDATE digests "
            "SET status='posted', posted_chat_id=:cid, posted_message_id=:mid, "
            "    posted_at=now(), posting_started_at=NULL, updated_at=now() "
            "WHERE id = :id AND status='posting' "
            "RETURNING id"
        ),
        {
            "id": digest.id,
            "cid": digest_config.destination_chat_id,
            "mid": sent.message_id,
        },
    )
    if update_result.rowcount == 0:
        logger.warning(
            "publish_digest: posted-transition rowcount=0 digest_id=%s "
            "(reaper or another worker moved the row)",
            digest.id,
        )
        # The Telegram message is posted but DB rejected — admin must investigate.
        await notify_admins_digest_failure(
            bot,
            digest_id=digest.id,
            status="failed",
            error_text="posted_transition_rowcount_zero_after_send",
        )
        return digest
    session.add(
        DigestRun(
            digest_id=digest.id,
            status="finished",
            finished_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()
    await session.refresh(digest)
    return digest


__all__ = ["publish_digest", "DigestPublisherInvalidState"]
