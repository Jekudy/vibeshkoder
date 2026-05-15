"""Phase 8 / T8-04 — weekly digest review state machine.

Implements the admin-review gate that sits between ``run_digest`` (which
now produces ``status='draft'`` for weekly rows) and ``publish_digest``
(which transitions ``approved_for_publish → posting → posted``).

Canonical state transitions (PHASE8_PLAN.md §5.E):

    draft
      └─> awaiting_review        (transition_to_awaiting_review)
            ├─> approved_for_publish → posting → posted   (approve_digest)
            ├─> rejected_by_admin                          (reject_digest)
            └─> failed                                     (citations stale)

All transitions use the H2 guarded-UPDATE pattern: ``WHERE id=:id AND
status=<expected>`` with ``RETURNING id``. ``rowcount=0`` means a racing
transition won — caller routes through
``_raise_invalid_state_after_guard_miss`` which re-reads the row,
distinguishes "deleted concurrently" (current_status=None) from
"wrong state" (str), and raises ``DigestReviewInvalidState`` with
structured fields.

Transaction discipline mirrors Phase 7 ``digest_publisher.py``: this
module FLUSHES, callers COMMIT. The scheduler/handler wraps each
service call in its own ``session.commit()`` boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import Bot
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Digest, DigestRun
from bot.services.digests import DigestConfig
from bot.services.llm_gateway import (
    DigestContextStaleError,
    _digest_context_is_clean,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApproveResult:
    """Returned by ``approve_digest`` on commit-path success.

    ``error_text`` is populated if approval transitioned (status moved to
    ``approved_for_publish``) but the subsequent publisher dispatch failed.
    """

    digest_id: int
    posted_chat_id: int | None
    posted_message_id: int | None
    error_text: str | None


class DigestReviewInvalidState(Exception):
    """Approval/reject attempted from a non-``awaiting_review`` state, OR a
    guarded UPDATE returned rowcount=0 because the row was deleted or its
    status moved out from under us between revalidation and commit.

    Structured fields (NOT positional Exception args) — handlers render
    context-aware admin replies by branching on ``current_status``:

      ``digest_id``: int                — always populated.
      ``current_status``: str | None    — ``None`` ⇒ row was DELETEd
                                          concurrently (e.g. by
                                          ``/digest_now --regenerate`` or
                                          operator manual cleanup).
                                          ``str`` ⇒ row exists in this
                                          status (e.g. ``'redacted'``,
                                          ``'rejected_by_admin'``,
                                          ``'posted'``).
      ``reason``: str                   — free-text explanation for logs
                                          + admin reply.
    """

    def __init__(
        self,
        *,
        digest_id: int,
        current_status: str | None,
        reason: str,
    ) -> None:
        self.digest_id = digest_id
        self.current_status = current_status
        self.reason = reason
        super().__init__(
            f"DigestReviewInvalidState(digest_id={digest_id}, "
            f"current_status={current_status!r}, reason={reason!r})"
        )


class DigestReviewNotFound(Exception):
    """Digest id was never found by the pre-flight SELECT inside
    ``approve_digest`` / ``reject_digest``.

    Distinct from ``DigestReviewInvalidState(current_status=None)``, which
    fires only AFTER a guarded UPDATE rowcount=0 race confirms the row has
    since been DELETEd.
    """


async def _raise_invalid_state_after_guard_miss(
    session: AsyncSession,
    *,
    digest_id: int,
    expected_status: str | tuple[str, ...],
) -> None:
    """Canonical rowcount=0 classifier.

    Call ONLY after a guarded UPDATE returned rowcount=0. Re-reads the
    current status (or detects DELETE) and raises
    ``DigestReviewInvalidState`` with structured fields.
    """
    current = (
        await session.execute(
            select(Digest.status).where(Digest.id == digest_id)
        )
    ).scalar_one_or_none()
    if current is None:
        raise DigestReviewInvalidState(
            digest_id=digest_id,
            current_status=None,
            reason="row_deleted_during_transition",
        )
    raise DigestReviewInvalidState(
        digest_id=digest_id,
        current_status=current,
        reason=f"expected status={expected_status!r}, found {current!r}",
    )


async def transition_to_awaiting_review(
    session: AsyncSession,
    *,
    digest_id: int,
) -> None:
    """Guarded transition ``draft → awaiting_review`` for a weekly digest.

    Called by ``digest_weekly_job`` after ``run_digest`` returns
    ``status='draft'``. The caller DMs admins after this commits.

    Inserts a ``digest_runs(status='awaiting_review')`` audit row. Caller
    commits.
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        text(
            "UPDATE digests "
            "SET status='awaiting_review', "
            "    awaiting_review_at=now(), "
            "    updated_at=now() "
            "WHERE id=:id AND status='draft' AND type='weekly' "
            "RETURNING id"
        ),
        {"id": digest_id},
    )
    if result.rowcount == 0:
        await _raise_invalid_state_after_guard_miss(
            session, digest_id=digest_id, expected_status="draft"
        )

    session.add(
        DigestRun(
            digest_id=digest_id,
            status="awaiting_review",
            started_at=now,
        )
    )
    await session.flush()


async def approve_digest(
    session: AsyncSession,
    *,
    bot: Bot | None,
    digest_id: int,
    admin_id: int,
    digest_config: DigestConfig,
    _publisher_dispatch: Callable[..., Awaitable[Any]] | None = None,
) -> ApproveResult:
    """Single-admin approval → triggers publisher.

    Flow (PHASE8_PLAN.md §5.E):
      1. SELECT digest by id; not found → raise ``DigestReviewNotFound``.
      2. Defense-in-depth: revalidate every cited mvid/cs against current
         governance state via ``_digest_context_is_clean``. On stale,
         guarded UPDATE → 'failed' / ``error_text='citations_stale_at_approval'``,
         then raise ``DigestReviewInvalidState(current_status='failed', ...)``.
      3. Guarded UPDATE ``WHERE status='awaiting_review'`` → 'approved_for_publish',
         set ``published_by_admin_id`` + ``approved_at``. rowcount=0 → classifier.
         INSERT digest_runs(status='approved_for_publish'). Caller commits.
      4. Dispatch publisher (``publish_digest``). Publisher's own guarded UPDATE
         accepts ``status IN ('draft','approved_for_publish')`` per §5.L; on
         success returns digest with ``status='posted'``.

    Returns ``ApproveResult`` with the publisher outcome. The
    ``_publisher_dispatch`` kwarg is a test-only override; default routes
    to ``bot.services.digest_publisher.publish_digest``.
    """
    # Step 1: pre-flight SELECT.
    row = (
        await session.execute(
            select(Digest).where(Digest.id == digest_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise DigestReviewNotFound(f"digest_id={digest_id} not found")

    if row.status != "awaiting_review":
        raise DigestReviewInvalidState(
            digest_id=digest_id,
            current_status=row.status,
            reason=(
                f"expected status='awaiting_review', found {row.status!r}"
            ),
        )
    if row.type != "weekly":
        raise DigestReviewInvalidState(
            digest_id=digest_id,
            current_status=row.status,
            reason=f"expected type='weekly', found {row.type!r}",
        )

    # Step 2: defense-in-depth citation revalidation. _digest_context_is_clean
    # operates on (cards, messages) lists with .message_version_id /
    # .card_source_ids attributes, so build the minimal shape here from
    # the JSONB citations.
    citations = row.citations or []
    mv_ids = [
        int(c["id"]) for c in citations if c.get("kind") == "message_version"
    ]
    cs_ids = [
        str(c["id"]) for c in citations if c.get("kind") == "card_source"
    ]

    @dataclass
    class _MsgStub:
        message_version_id: int

    @dataclass
    class _CardStub:
        card_source_ids: list[str]

    messages_stub = [_MsgStub(message_version_id=i) for i in mv_ids]
    cards_stub = [_CardStub(card_source_ids=cs_ids)] if cs_ids else []

    revalidation_failed = False
    try:
        await _digest_context_is_clean(
            session, cards=cards_stub, messages=messages_stub
        )
    except DigestContextStaleError:
        revalidation_failed = True

    if revalidation_failed:
        # Mark failed via guarded UPDATE; classifier on rowcount=0.
        stale_result = await session.execute(
            text(
                "UPDATE digests "
                "SET status='failed', "
                "    error_text='citations_stale_at_approval', "
                "    updated_at=now() "
                "WHERE id=:id AND status='awaiting_review' "
                "RETURNING id"
            ),
            {"id": digest_id},
        )
        if stale_result.rowcount == 0:
            await _raise_invalid_state_after_guard_miss(
                session,
                digest_id=digest_id,
                expected_status="awaiting_review",
            )
        session.add(
            DigestRun(
                digest_id=digest_id,
                status="failed",
                error_text="citations_stale_at_approval",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        raise DigestReviewInvalidState(
            digest_id=digest_id,
            current_status="failed",
            reason="citations_stale_at_approval",
        )

    # Step 3: guarded approval transition.
    approve_result = await session.execute(
        text(
            "UPDATE digests "
            "SET status='approved_for_publish', "
            "    published_by_admin_id=:admin_id, "
            "    approved_at=now(), "
            "    updated_at=now() "
            "WHERE id=:id AND status='awaiting_review' "
            "RETURNING id"
        ),
        {"id": digest_id, "admin_id": admin_id},
    )
    if approve_result.rowcount == 0:
        await _raise_invalid_state_after_guard_miss(
            session, digest_id=digest_id, expected_status="awaiting_review"
        )

    session.add(
        DigestRun(
            digest_id=digest_id,
            status="approved_for_publish",
            started_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()

    # Refresh the ORM row so subsequent operations see the new status.
    await session.refresh(row)

    # Step 4: dispatch publisher.
    if _publisher_dispatch is None:
        from bot.services.digest_publisher import publish_digest as _publish

        _publisher_dispatch = _publish

    published = await _publisher_dispatch(
        session, bot=bot, digest=row, digest_config=digest_config
    )

    return ApproveResult(
        digest_id=digest_id,
        posted_chat_id=getattr(published, "posted_chat_id", None),
        posted_message_id=getattr(published, "posted_message_id", None),
        error_text=getattr(published, "error_text", None),
    )


async def reject_digest(
    session: AsyncSession,
    *,
    digest_id: int,
    admin_id: int,
    reason: str | None = None,
) -> None:
    """Guarded transition ``awaiting_review → rejected_by_admin``.

    L4 service-layer normalization: ``review_notes`` is truncated to 1000
    chars. ``review_notes`` column itself is unbounded ``Text``; truncation
    happens here, not in the schema.
    """
    notes = (reason or "no reason given")[:1000]

    result = await session.execute(
        text(
            "UPDATE digests "
            "SET status='rejected_by_admin', "
            "    published_by_admin_id=:admin_id, "
            "    review_notes=:notes, "
            "    updated_at=now() "
            "WHERE id=:id AND status='awaiting_review' "
            "RETURNING id"
        ),
        {"id": digest_id, "admin_id": admin_id, "notes": notes},
    )
    if result.rowcount == 0:
        await _raise_invalid_state_after_guard_miss(
            session, digest_id=digest_id, expected_status="awaiting_review"
        )

    now = datetime.now(timezone.utc)
    session.add(
        DigestRun(
            digest_id=digest_id,
            status="rejected_by_admin",
            error_text=notes,
            started_at=now,
            finished_at=now,
        )
    )
    await session.flush()


__all__ = [
    "ApproveResult",
    "DigestReviewInvalidState",
    "DigestReviewNotFound",
    "approve_digest",
    "reject_digest",
    "transition_to_awaiting_review",
]
