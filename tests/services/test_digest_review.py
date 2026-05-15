"""Tests for T8-04 / Phase 8 — bot/services/digest_review.py.

Covers PHASE8_PLAN.md §5.E review state machine:
- `transition_to_awaiting_review` — draft → awaiting_review (guarded UPDATE).
- `approve_digest` — awaiting_review → approved_for_publish → publisher.
- `reject_digest` — awaiting_review → rejected_by_admin.
- `_raise_invalid_state_after_guard_miss` canonical rowcount=0 classifier
  distinguishing "row deleted" (current_status=None) from "wrong state"
  (current_status=str).

Tests follow the Phase 7 service-fixture pattern: outer-transaction
isolation; the service flushes, tests roll back. No `session.commit()`
inside test bodies (would break fixture isolation).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")

_chat_counter = itertools.count(start=9100)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


async def _make_weekly_digest(
    db_session,
    *,
    status: str = "draft",
    citations: list | None = None,
    body: str = "TL;DR.\n\n- One bullet.",
):
    """Insert a weekly digest in the given status. Returns row."""
    from bot.db.models import Digest

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="weekly",
        window_start=now - timedelta(days=7),
        window_end=now,
        body_markdown=body,
        citations=citations if citations is not None else [],
        status=status,
    )
    db_session.add(digest)
    await db_session.flush()
    return digest


# ── transition_to_awaiting_review ─────────────────────────────────────────────


async def test_transition_to_awaiting_review_draft_to_awaiting(db_session):
    """draft → awaiting_review: status flips, awaiting_review_at set,
    digest_runs(status='awaiting_review') audit row inserted."""
    from bot.services.digest_review import transition_to_awaiting_review

    digest = await _make_weekly_digest(db_session, status="draft")
    did = digest.id

    await transition_to_awaiting_review(db_session, digest_id=did)

    row = (await db_session.execute(
        text(
            "SELECT status, awaiting_review_at FROM digests WHERE id=:id"
        ),
        {"id": did},
    )).mappings().one()
    assert row["status"] == "awaiting_review"
    assert row["awaiting_review_at"] is not None

    audit_count = (await db_session.execute(
        text(
            "SELECT COUNT(*) FROM digest_runs "
            "WHERE digest_id=:id AND status='awaiting_review'"
        ),
        {"id": did},
    )).scalar_one()
    assert audit_count == 1


async def test_transition_to_awaiting_review_wrong_state_raises(db_session):
    """Calling on a status='posted' row → DigestReviewInvalidState with
    current_status='posted'."""
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        transition_to_awaiting_review,
    )

    digest = await _make_weekly_digest(db_session, status="draft")
    did = digest.id
    # Move to posted manually (bypass the SM since we're testing guard).
    # ck_digests_approved_audit requires published_by_admin_id + approved_at
    # for weekly posted rows.
    await db_session.execute(
        text(
            "UPDATE digests SET status='posted', posted_chat_id=-42, "
            "posted_message_id=999, posted_at=now(), "
            "published_by_admin_id=42, approved_at=now() WHERE id=:id"
        ),
        {"id": did},
    )
    await db_session.flush()

    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await transition_to_awaiting_review(db_session, digest_id=did)
    assert exc_info.value.digest_id == did
    assert exc_info.value.current_status == "posted"
    assert "draft" in exc_info.value.reason


async def test_transition_to_awaiting_review_row_deleted_raises_none_status(db_session):
    """Row deleted between caller and guarded UPDATE →
    DigestReviewInvalidState with current_status=None."""
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        transition_to_awaiting_review,
    )

    digest = await _make_weekly_digest(db_session, status="draft")
    did = digest.id
    await db_session.execute(text("DELETE FROM digests WHERE id=:id"), {"id": did})
    await db_session.flush()

    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await transition_to_awaiting_review(db_session, digest_id=did)
    assert exc_info.value.digest_id == did
    assert exc_info.value.current_status is None
    assert "row_deleted_during_transition" in exc_info.value.reason


# ── approve_digest ────────────────────────────────────────────────────────────


async def test_approve_digest_awaiting_to_approved_for_publish(db_session):
    """Clean citations + awaiting_review → approved_for_publish → publisher
    dispatched. With a stub publisher, verify state machine transitions and
    audit rows."""
    from bot.services.digests import DigestConfig
    from bot.services.digest_review import approve_digest

    digest = await _make_weekly_digest(
        db_session, status="awaiting_review", citations=[]
    )
    did = digest.id

    publisher_called = []

    async def _stub_publisher(session, *, bot, digest, digest_config):
        publisher_called.append(digest.id)
        digest.status = "posted"
        digest.posted_chat_id = -42
        digest.posted_message_id = 555
        digest.posted_at = datetime.now(timezone.utc)
        await session.flush()
        return digest

    cfg = DigestConfig(destination_chat_id=-42)
    result = await approve_digest(
        db_session,
        bot=None,  # _stub_publisher ignores bot
        digest_id=did,
        admin_id=149820031,
        digest_config=cfg,
        _publisher_dispatch=_stub_publisher,
    )

    assert publisher_called == [did]
    assert result.digest_id == did
    assert result.posted_message_id == 555

    row = (await db_session.execute(
        text(
            "SELECT status, published_by_admin_id, approved_at "
            "FROM digests WHERE id=:id"
        ),
        {"id": did},
    )).mappings().one()
    # Publisher stub moved to 'posted' so we verify approve_digest set
    # published_by_admin_id/approved_at BEFORE the publisher call.
    assert row["status"] == "posted"
    assert row["published_by_admin_id"] == 149820031
    assert row["approved_at"] is not None

    # Audit: at least one 'approved_for_publish' DigestRun row exists.
    audit_count = (await db_session.execute(
        text(
            "SELECT COUNT(*) FROM digest_runs "
            "WHERE digest_id=:id AND status='approved_for_publish'"
        ),
        {"id": did},
    )).scalar_one()
    assert audit_count == 1


async def test_approve_digest_wrong_state_raises(db_session):
    """Approving a status='rejected_by_admin' row → DigestReviewInvalidState
    with current_status='rejected_by_admin'."""
    from bot.services.digests import DigestConfig
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        approve_digest,
    )

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id
    # Manually move to rejected_by_admin to simulate a race-won state.
    await db_session.execute(
        text(
            "UPDATE digests SET status='rejected_by_admin', "
            "published_by_admin_id=42, review_notes='nope' WHERE id=:id"
        ),
        {"id": did},
    )
    await db_session.flush()

    cfg = DigestConfig(destination_chat_id=-42)
    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await approve_digest(
            db_session,
            bot=None,
            digest_id=did,
            admin_id=149820031,
            digest_config=cfg,
        )
    assert exc_info.value.current_status == "rejected_by_admin"


async def test_approve_digest_citations_stale_marks_failed(db_session):
    """If citation revalidation fails at approval, digest goes to 'failed'
    with error_text='citations_stale_at_approval' and DigestReviewInvalidState
    is raised."""
    from bot.services.digests import DigestConfig
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        approve_digest,
    )

    # Cite a non-existent mv_id — revalidation must fail.
    digest = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        citations=[{"kind": "message_version", "id": 999_999_999, "position": 0}],
    )
    did = digest.id

    publisher_called = []

    async def _stub_publisher(session, *, bot, digest, digest_config):
        publisher_called.append(digest.id)
        return digest

    cfg = DigestConfig(destination_chat_id=-42)
    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await approve_digest(
            db_session,
            bot=None,
            digest_id=did,
            admin_id=149820031,
            digest_config=cfg,
            _publisher_dispatch=_stub_publisher,
        )
    assert exc_info.value.current_status == "failed"
    assert "citations_stale_at_approval" in exc_info.value.reason
    # Publisher MUST NOT be called.
    assert publisher_called == []

    row = (await db_session.execute(
        text("SELECT status, error_text FROM digests WHERE id=:id"),
        {"id": did},
    )).mappings().one()
    assert row["status"] == "failed"
    assert row["error_text"] == "citations_stale_at_approval"


async def test_approve_digest_not_found_raises(db_session):
    """A digest_id that does not exist → DigestReviewNotFound (distinct from
    DigestReviewInvalidState)."""
    from bot.services.digests import DigestConfig
    from bot.services.digest_review import (
        DigestReviewNotFound,
        approve_digest,
    )

    cfg = DigestConfig(destination_chat_id=-42)
    with pytest.raises(DigestReviewNotFound):
        await approve_digest(
            db_session,
            bot=None,
            digest_id=9_999_999,
            admin_id=149820031,
            digest_config=cfg,
        )


# ── reject_digest ─────────────────────────────────────────────────────────────


async def test_reject_digest_awaiting_to_rejected_by_admin(db_session):
    """awaiting_review → rejected_by_admin, review_notes set, audit row
    inserted."""
    from bot.services.digest_review import reject_digest

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    await reject_digest(
        db_session,
        digest_id=did,
        admin_id=149820031,
        reason="off-topic content",
    )

    row = (await db_session.execute(
        text(
            "SELECT status, review_notes, published_by_admin_id "
            "FROM digests WHERE id=:id"
        ),
        {"id": did},
    )).mappings().one()
    assert row["status"] == "rejected_by_admin"
    assert row["review_notes"] == "off-topic content"
    assert row["published_by_admin_id"] == 149820031

    audit_count = (await db_session.execute(
        text(
            "SELECT COUNT(*) FROM digest_runs "
            "WHERE digest_id=:id AND status='rejected_by_admin'"
        ),
        {"id": did},
    )).scalar_one()
    assert audit_count == 1


async def test_reject_digest_truncates_reason_to_1000_chars(db_session):
    """review_notes must be truncated to <=1000 chars (L4 service-layer
    normalization)."""
    from bot.services.digest_review import reject_digest

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    long_reason = "x" * 2000
    await reject_digest(
        db_session, digest_id=did, admin_id=42, reason=long_reason
    )

    row = (await db_session.execute(
        text("SELECT review_notes FROM digests WHERE id=:id"), {"id": did}
    )).mappings().one()
    assert row["review_notes"] is not None
    assert len(row["review_notes"]) == 1000


async def test_reject_digest_none_reason_default_message(db_session):
    """reason=None → review_notes='no reason given'."""
    from bot.services.digest_review import reject_digest

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    await reject_digest(db_session, digest_id=did, admin_id=42, reason=None)

    row = (await db_session.execute(
        text("SELECT review_notes FROM digests WHERE id=:id"), {"id": did}
    )).mappings().one()
    assert row["review_notes"] == "no reason given"


async def test_reject_digest_wrong_state_raises(db_session):
    """Rejecting a posted row → DigestReviewInvalidState."""
    from bot.services.digest_review import (
        DigestReviewInvalidState,
        reject_digest,
    )

    digest = await _make_weekly_digest(db_session, status="draft")
    did = digest.id
    await db_session.execute(
        text(
            "UPDATE digests SET status='posted', posted_chat_id=-42, "
            "posted_message_id=999, posted_at=now(), "
            "published_by_admin_id=42, approved_at=now() WHERE id=:id"
        ),
        {"id": did},
    )
    await db_session.flush()

    with pytest.raises(DigestReviewInvalidState) as exc_info:
        await reject_digest(
            db_session, digest_id=did, admin_id=42, reason="anything"
        )
    assert exc_info.value.current_status == "posted"
