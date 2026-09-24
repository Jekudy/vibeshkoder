"""Telegram digest publisher — T7-05 / Phase 7.

Commits the ``posting`` state BEFORE ``bot.send_message``.  Telegram has no
idempotency key, so this at-most-once boundary prevents an automatic retry
from duplicating a digest after the send succeeded but the process crashed
before recording the returned message id.  A stale ``posting`` row is treated
as delivery-uncertain and requires explicit operator reconciliation.

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
import re
from collections import Counter
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Digest, DigestRun
from bot.services.control_messages import control_message_excludes_sql_fragment
from bot.services.digest_admin_notify import notify_admins_digest_failure
from bot.services.digest_renderer import render_digest_html
from bot.services.digests import DigestConfig
from bot.services.image_memory import telegram_source_message_url

logger = logging.getLogger(__name__)

_CONTROL_EXCLUDES = control_message_excludes_sql_fragment()

_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
_MESSAGE_CITATION_RE = re.compile(r"\[\[mv:([1-9]\d*)\]\]")


def _is_lock_not_available(exc: DBAPIError) -> bool:
    """Return whether a DBAPI failure is PostgreSQL ``lock_not_available``.

    SQLAlchemy's asyncpg adapter exposes the server code on ``exc.orig``;
    psycopg-compatible adapters may expose the same value as ``pgcode``.
    Matching the SQLSTATE keeps unrelated connection/query failures fail-fast.
    """
    return (
        getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE_SQLSTATE
        or getattr(exc.orig, "pgcode", None) == _LOCK_NOT_AVAILABLE_SQLSTATE
    )


class DigestPublisherInvalidState(Exception):
    """Publisher trigger guard fired, OR the guarded UPDATE returned
    rowcount=0 (concurrent race).

    Handlers branch on ``current_status`` to
    render context-aware replies:

      ``digest_id``: int
      ``current_status``: str | None  — ``None`` ⇒ row was DELETEd
                                        concurrently (e.g. by
                                        ``--regenerate`` racing a stale
                                        approve).
                                        ``str`` ⇒ row exists in this status
                                        (e.g. ``'redacted'`` after cascade
                                        won the race).
      ``reason``: str                 — free-text explanation.
    """

    def __init__(
        self,
        digest_id: int | None = None,
        current_status: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.digest_id = digest_id
        self.current_status = current_status
        self.reason = reason or ""
        super().__init__(
            f"DigestPublisherInvalidState(digest_id={digest_id}, "
            f"current_status={current_status!r}, reason={self.reason!r})"
        )


async def resolve_digest_source_links(
    session: AsyncSession, *, citations: list[dict], source_chat_id: int
) -> dict[str, str]:
    """Resolve every internal message token to an application-built URL."""
    citation_keys: set[tuple[str, str, int]] = set()
    for citation in citations:
        if (
            citation.get("kind") != "message_version"
            or not isinstance(citation.get("position"), int)
            or isinstance(citation["position"], bool)
            or citation["position"] < 0
        ):
            raise ValueError("digest citations are malformed")
        key = (citation["kind"], str(citation.get("id")), citation["position"])
        if key in citation_keys:
            raise ValueError("digest citations contain a duplicate")
        citation_keys.add(key)
    try:
        mv_ids = [int(c["id"]) for c in citations if c["kind"] == "message_version"]
    except (TypeError, ValueError) as exc:
        raise ValueError("digest citations are malformed") from exc
    by_key: dict[tuple[str, str], str] = {}
    if mv_ids:
        sql = (
            f"SELECT mv.id::text, cm.chat_id, cm.message_id FROM message_versions mv "
            "JOIN chat_messages cm ON cm.id=mv.chat_message_id "
            "WHERE mv.id=ANY(:ids) AND cm.chat_id=:chat_id "
            "AND cm.current_version_id=mv.id AND cm.memory_policy='normal' "
            f"AND cm.is_redacted=FALSE AND mv.is_redacted=FALSE AND {_CONTROL_EXCLUDES}"
        )
        for source_id, chat_id, message_id in (
            await session.execute(
                text(sql),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- static in-code SQL/fragments; all runtime values are bound parameters.
                {"ids": mv_ids, "chat_id": source_chat_id},
            )
        ).all():
            by_key[("message_version", source_id)] = telegram_source_message_url(
                int(chat_id), int(message_id), username=None
            )

    links: dict[str, str] = {}
    for citation in citations:
        url = by_key.get((str(citation.get("kind")), str(citation.get("id"))))
        if url is None:
            raise ValueError("digest citation has no safe source URL")
        links[f"[[mv:{int(citation['id'])}]]"] = url
    return links


async def _digest_revalidate_citations(session: AsyncSession, *, digest: Digest) -> bool:
    """Defense-in-depth: re-check every citation source id is still visible.

    Returns True if all cited sources are clean, False if any failed
    revalidation (caller should mark digest 'failed' and skip publish).
    """
    citations = digest.citations or []
    mv_ids = [int(c["id"]) for c in citations if c.get("kind") == "message_version"]
    cs_ids = [str(c["id"]) for c in citations if c.get("kind") == "card_source"]

    if mv_ids:
        result = await session.execute(
            text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- static in-code SQL/fragments; all runtime values are bound parameters.
                f"SELECT mv.id FROM message_versions mv "
                "JOIN chat_messages cm ON cm.id = mv.chat_message_id "
                "WHERE mv.id = ANY(:mv_ids) "
                "  AND cm.memory_policy = 'normal' "
                "  AND cm.is_redacted = FALSE "
                "  AND mv.is_redacted = FALSE "
                f"  AND {_CONTROL_EXCLUDES} "
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
            text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- static in-code SQL/fragments; all runtime values are bound parameters.
                f"SELECT cs.id::text FROM card_sources cs "
                "JOIN knowledge_cards kc ON kc.id = cs.card_id "
                "JOIN message_versions mv ON mv.id = cs.message_version_id "
                "JOIN chat_messages cm ON cm.id = mv.chat_message_id "
                "WHERE cs.id::text = ANY(:cs_ids) "
                "  AND kc.card_status = 'approved' "
                "  AND cm.memory_policy = 'normal' "
                "  AND cm.is_redacted = FALSE "
                "  AND mv.is_redacted = FALSE "
                f"  AND {_CONTROL_EXCLUDES} "
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


async def _digest_delivery_lock_scope(session: AsyncSession, *, digest: Digest):
    """Return the established provenance lock scope for a digest send."""
    from bot.services.advisory_locks import (
        governed_message_lock_keys,
        hold_session_advisory_locks,
    )
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    provenance_ids = tuple(
        int(citation["id"])
        for citation in (digest.citations or [])
        if citation.get("kind") == "message_version"
    )
    lock_keys = await governed_message_lock_keys(session, provenance_ids)
    return hold_session_advisory_locks(
        session,
        (_p6_mvid_advisory_lock_id(value) for value in provenance_ids),
        lock_keys=lock_keys,
    )


async def publish_digest(
    session: AsyncSession,
    *,
    bot: Bot,
    digest: Digest,
    digest_config: DigestConfig,
) -> Digest:
    """Publish while holding provenance locks before any digest-row lock."""
    async with await _digest_delivery_lock_scope(session, digest=digest):
        return await _publish_digest_after_provenance_lock(
            session, bot=bot, digest=digest, digest_config=digest_config
        )


async def _publish_digest_after_provenance_lock(
    session: AsyncSession,
    *,
    bot: Bot,
    digest: Digest,
    digest_config: DigestConfig,
) -> Digest:
    """Publish a digest to its destination chat.

    The function owns two durable transaction boundaries: ``posting`` before
    outbound send, and ``posted``/``failed`` after the Telegram result.
    """
    if digest.status != "draft":
        raise DigestPublisherInvalidState(
            digest_id=digest.id,
            current_status=digest.status,
            reason=(f"publisher trigger requires status 'draft', found {digest.status!r}"),
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
    await session.execute(text("SELECT set_config('statement_timeout', '30s', true)"))

    # Try FOR UPDATE NOWAIT with up to 3 backoff retries.
    locked = False
    last_exc: DBAPIError | None = None
    for attempt in range(3):
        try:
            # PostgreSQL aborts the current transaction after a 55P03.  Keep
            # each NOWAIT attempt inside a SAVEPOINT so the expected failure
            # rolls back locally and the publisher's outer transaction stays
            # usable for the next retry and the failure audit below.
            async with session.begin_nested():
                await session.execute(
                    text("SELECT id FROM digests WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": digest.id},
                )
            locked = True
            break
        except DBAPIError as exc:
            if not _is_lock_not_available(exc):
                raise
            last_exc = exc
            await asyncio.sleep(0.1 * (2**attempt))
    if not locked:
        logger.warning(
            "publish_digest: lock acquisition exhausted (3 retries) digest_id=%s",
            digest.id,
        )
        # The lock owner may have committed ``posting``/``posted`` while this
        # publisher was backing off.  Transition to failure only if the row is
        # still publishable; an unconditional ORM flush here would overwrite
        # the concurrent winner after waiting for its row lock.
        failure_result = await session.execute(
            text(
                "UPDATE digests "
                "SET status='failed', error_text='publish_lock_timeout', updated_at=now() "
                "WHERE id=:id AND status='draft' "
                "RETURNING id"
            ),
            {"id": digest.id},
        )
        if failure_result.scalar_one_or_none() is None:
            current = (
                await session.execute(
                    text("SELECT status FROM digests WHERE id=:id"),
                    {"id": digest.id},
                )
            ).scalar_one_or_none()
            if current is None:
                raise DigestPublisherInvalidState(
                    digest_id=digest.id,
                    current_status=None,
                    reason="row_deleted_during_lock_timeout_transition",
                )
            raise DigestPublisherInvalidState(
                digest_id=digest.id,
                current_status=current,
                reason=(
                    f"publish_lock_timeout transition requires status 'draft', found {current!r}"
                ),
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

    # Guarded transition to ``posting``. Rowcount=0 means a concurrent
    # transition won and is classified by re-reading the row.
    posting_result = await session.execute(
        text(
            "UPDATE digests "
            "SET status='posting', "
            "    posting_started_at=now(), "
            "    updated_at=now() "
            "WHERE id=:id AND status='draft' "
            "RETURNING id"
        ),
        {"id": digest.id},
    )
    if posting_result.rowcount == 0:
        # Race-won concurrent transition (cascade redacted, --regenerate
        # DELETEd, etc.). Re-read, classify, raise.
        current = (
            await session.execute(
                text("SELECT status FROM digests WHERE id=:id"),
                {"id": digest.id},
            )
        ).scalar_one_or_none()
        if current is None:
            raise DigestPublisherInvalidState(
                digest_id=digest.id,
                current_status=None,
                reason="row_deleted_during_transition",
            )
        raise DigestPublisherInvalidState(
            digest_id=digest.id,
            current_status=current,
            reason=(f"expected status 'draft', found {current!r}"),
        )

    # Refresh ORM state to reflect the guarded UPDATE so subsequent code
    # (revalidation, render, send) sees the new status / posting_started_at.
    digest.status = "posting"
    digest.posting_started_at = datetime.now(timezone.utc)
    digest.updated_at = datetime.now(timezone.utc)
    await session.flush()

    return await _publish_posting_digest(session, bot=bot, digest=digest, digest_config=digest_config)


async def _publish_posting_digest(
    session: AsyncSession, *, bot: Bot, digest: Digest, digest_config: DigestConfig
) -> Digest:
    """Revalidate and deliver while the provenance locks are held."""
    if not await _digest_revalidate_citations(session, digest=digest):
        digest.status = "failed"
        digest.error_text = "citations_stale_at_publish"
        digest.posting_started_at = None
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="failed",
                error_text=digest.error_text,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        await notify_admins_digest_failure(
            bot, digest_id=digest.id, status="failed", error_text=digest.error_text
        )
        return digest
    try:
        body_tokens = Counter(
            match.group(0) for match in _MESSAGE_CITATION_RE.finditer(digest.body_markdown or "")
        )
        citation_tokens = Counter(
            f"[[mv:{int(citation['id'])}]]" for citation in (digest.citations or [])
        )
        if body_tokens != citation_tokens:
            raise ValueError("digest body citations do not match stored provenance")
        body_html = render_digest_html(
            digest.body_markdown or "",
            window_start_utc=digest.window_start,
            source_links_by_citation=await resolve_digest_source_links(
                session,
                citations=digest.citations or [],
                source_chat_id=digest_config.source_chat_id,
            ),
            digest_type=digest.type,
            window_end_utc=digest.window_end if digest.type == "weekly" else None,
        )
    except (TypeError, ValueError):
        digest.status = "failed"
        digest.error_text = "source_link_validation_failed"
        digest.posting_started_at = None
        session.add(
            DigestRun(
                digest_id=digest.id,
                status="failed",
                error_text=digest.error_text,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        await notify_admins_digest_failure(
            bot, digest_id=digest.id, status="failed", error_text=digest.error_text
        )
        return digest
    # At-most-once delivery boundary. If Telegram accepts the message, posting
    # remains durable and no cron path sends this digest again.
    await session.commit()
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
        await session.commit()
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
        await session.commit()
        await notify_admins_digest_failure(
            bot,
            digest_id=digest.id,
            status="failed",
            error_text="bot_not_in_destination",
        )
        return digest
    update_result = await session.execute(
        text(
            "UPDATE digests "
            "SET status='posted', posted_chat_id=:cid, posted_message_id=:mid, "
            "    posted_at=now(), posting_started_at=NULL, updated_at=now() "
            "WHERE id = :id AND status='posting' "
            "RETURNING id"
        ),
        {"id": digest.id, "cid": digest_config.destination_chat_id, "mid": sent.message_id},
    )
    if update_result.rowcount == 0:
        logger.warning("publish_digest: posted-transition rowcount=0 digest_id=%s", digest.id)
        await notify_admins_digest_failure(
            bot,
            digest_id=digest.id,
            status="failed",
            error_text="posted_transition_rowcount_zero_after_send",
        )
        return digest
    session.add(DigestRun(digest_id=digest.id, status="finished", finished_at=datetime.now(timezone.utc)))
    await session.flush()
    await session.refresh(digest)
    await session.commit()
    return digest


__all__ = ["publish_digest", "resolve_digest_source_links", "DigestPublisherInvalidState"]
