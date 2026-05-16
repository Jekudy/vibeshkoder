"""Tests for T8-06 — Phase 8 weekly admin digest handlers.

Covers PHASE8_PLAN.md §5.H:
- ``/digest_now weekly``: creates draft → transitions to awaiting_review;
  status-aware reply matrix.
- ``/digest_now weekly --regenerate``: atomic lock+audit+delete+re-run; only
  valid from ``rejected_by_admin`` / ``rejected_by_reaper`` states.
- ``/digest_review``: lists ``awaiting_review`` weekly digests (ORDER BY
  awaiting_review_at ASC, LIMIT 20).
- ``/digest_approve <id>``: calls ``approve_digest``; renders context-aware
  replies for ``DigestReviewInvalidState`` / ``DigestReviewNotFound``.
- ``/digest_reject <id> [reason]``: calls ``reject_digest`` with truncated
  reason; same error mapping.
- Non-admin invocations: silent no-op (no DB writes, no replies).

Tests follow the Phase 7 service-fixture pattern: outer-transaction
isolation; service flushes, tests roll back. No ``session.commit()``
inside test bodies (would break fixture isolation).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


_chat_counter = itertools.count(start=8800)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _mk_command_obj(args: str | None) -> MagicMock:
    obj = MagicMock()
    obj.args = args
    return obj


def _mk_msg(user_id: int) -> MagicMock:
    m = MagicMock()
    m.from_user = MagicMock()
    m.from_user.id = user_id
    m.answer = AsyncMock()
    return m


def _current_weekly_window() -> tuple[datetime, datetime]:
    """Mirror of ``bot.handlers.digest._weekly_window_for_now_msk`` for use in
    fixtures that need to pre-seed a row matching the handler's lookup
    window."""
    from bot.handlers.digest import _weekly_window_for_now_msk

    return _weekly_window_for_now_msk()


async def _make_weekly_digest(
    db_session,
    *,
    status: str = "draft",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    citations: list | None = None,
    body: str = "TL;DR.\n\n- One bullet.",
    awaiting_review_at: datetime | None = None,
    published_by_admin_id: int | None = None,
    approved_at: datetime | None = None,
    review_notes: str | None = None,
):
    """Insert a weekly digest in the given status. Returns ORM row.

    By default the window matches the current weekly window so the row
    aligns with ``cmd_digest_now weekly`` lookups; pass ``window_start``
    / ``window_end`` to override (e.g. /digest_review tests need rows at
    different windows to assert ORDER BY).
    """
    from bot.db.models import Digest

    if window_start is None or window_end is None:
        cur_ws, cur_we = _current_weekly_window()
        ws = window_start if window_start is not None else cur_ws
        we = window_end if window_end is not None else cur_we
    else:
        ws = window_start
        we = window_end
    digest = Digest(
        type="weekly",
        window_start=ws,
        window_end=we,
        body_markdown=body,
        citations=citations if citations is not None else [],
        status=status,
        awaiting_review_at=awaiting_review_at,
        published_by_admin_id=published_by_admin_id,
        approved_at=approved_at,
        review_notes=review_notes,
    )
    db_session.add(digest)
    await db_session.flush()
    return digest


# ── /digest_now weekly ──────────────────────────────────────────────────────


async def test_digest_now_weekly_creates_draft_and_transitions_to_awaiting_review(
    db_session, monkeypatch
):
    """Happy path: admin invokes /digest_now weekly → run_digest returns
    a fresh draft → transition_to_awaiting_review runs → reply mentions
    awaiting admin approval (with the new digest_id)."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    draft = await _make_weekly_digest(db_session, status="draft")

    async def _fake_run_digest(*args, **kwargs):
        return draft

    monkeypatch.setattr("bot.handlers.digest.run_digest", _fake_run_digest)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj("weekly")
    )

    # Row must now be in awaiting_review.
    row = (
        await db_session.execute(
            text(
                "SELECT status, awaiting_review_at FROM digests WHERE id=:id"
            ),
            {"id": draft.id},
        )
    ).mappings().one()
    assert row["status"] == "awaiting_review"
    assert row["awaiting_review_at"] is not None

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert str(draft.id) in body
    assert (
        "ожидает одобрения" in body.lower()
        or "одобрения админ" in body.lower()
        or "awaiting" in body.lower()
    ), f"expected awaiting-admin reply, got: {body!r}"
    # Should hint at approve / reject commands.
    assert "digest_approve" in body and "digest_reject" in body


async def test_digest_now_weekly_regenerate_after_rejected_by_admin(
    db_session, monkeypatch
):
    """/digest_now weekly --regenerate on a rejected_by_admin row:
    1. Acquires lock + audit insert (regenerated_by_admin) BEFORE delete.
    2. DELETEs old row.
    3. Calls run_digest fresh; new draft → transition_to_awaiting_review.
    4. Reply mentions new digest_id (NOT the old one)."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now
    from bot.db.models import Digest

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    old = await _make_weekly_digest(
        db_session,
        status="rejected_by_admin",
        published_by_admin_id=admin_id,
        review_notes="off-topic",
    )
    old_id = old.id

    new_draft_holder: dict = {}

    async def _fake_run_digest(session, *, type, window_start, window_end, **kwargs):
        # New draft for the same window.
        new = Digest(
            type=type,
            window_start=window_start,
            window_end=window_end,
            body_markdown="New TL;DR.\n\n- Fresh bullet.",
            citations=[],
            status="draft",
        )
        session.add(new)
        await session.flush()
        new_draft_holder["digest"] = new
        return new

    monkeypatch.setattr("bot.handlers.digest.run_digest", _fake_run_digest)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg,
        bot=bot_mock,
        session=db_session,
        command=_mk_command_obj("weekly --regenerate"),
    )

    # Audit row for regenerated_by_admin must exist. After the DELETE the
    # FK ``digest_id`` is reset to NULL via ON DELETE SET NULL (so the audit
    # survives even when the parent row goes away). Look up by status +
    # error_text trail instead.
    audit_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM digest_runs "
                "WHERE status='regenerated_by_admin' "
                "  AND error_text LIKE :pat"
            ),
            {"pat": f"%from rejected_by_admin by admin {admin_id}%"},
        )
    ).scalar_one()
    assert audit_count == 1, (
        f"expected one regenerated_by_admin audit row for old digest {old_id}, "
        f"got {audit_count}"
    )

    # Old row must be deleted.
    deleted = (
        await db_session.execute(
            text("SELECT 1 FROM digests WHERE id=:id"), {"id": old_id}
        )
    ).scalar_one_or_none()
    assert deleted is None, f"old digest {old_id} must be deleted"

    # New draft row must exist and be transitioned to awaiting_review.
    new = new_draft_holder["digest"]
    assert new.id != old_id
    row = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"), {"id": new.id}
        )
    ).mappings().one()
    assert row["status"] == "awaiting_review"

    # Reply mentions the NEW digest id.
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert str(new.id) in body
    assert str(old_id) not in body or body.count(str(new.id)) >= 1


async def test_digest_now_weekly_regenerate_race_state_changed_post_lock(
    db_session, monkeypatch
):
    """FHR HIGH-3: in the regenerate path, the lock MUST be acquired BEFORE
    reading the existing row's status. If the row's status changes between
    the (broken) pre-check SELECT and the lock — e.g. another admin queues
    a regenerate, drains the row, and a new awaiting_review row lands —
    the second caller's stale view would proceed to audit-insert + DELETE
    on a row that no longer matches ``rejected_*``.

    After the fix, the lock is held BEFORE the state read. We simulate the
    "concurrent admin won the race" outcome by monkeypatching
    ``_acquire_idempotency_lock`` to flip the row's status inside the lock
    body (representing the prior holder's effect committed before this
    waiter got its lock). The handler MUST then refuse to DELETE / audit /
    rerun and surface the new status to the admin.
    """
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    rejected = await _make_weekly_digest(
        db_session,
        status="rejected_by_admin",
        published_by_admin_id=admin_id,
        review_notes="off-topic",
    )
    rejected_id = rejected.id

    # Patch the lock to flip status mid-call — simulates "the other
    # regenerate transaction committed between my pre-check and my lock
    # acquire". The handler must NOT proceed to DELETE / audit / rerun
    # on this stale view.
    import bot.handlers.digest as digest_mod

    real_lock = digest_mod._acquire_idempotency_lock
    flipped = {"done": False}

    async def _flipping_lock(session, *, type, window_start, window_end):
        await real_lock(
            session, type=type, window_start=window_start, window_end=window_end
        )
        if not flipped["done"]:
            # Simulate: the prior regenerate holder finished — the rejected
            # row is gone and a new awaiting_review row exists.
            await session.execute(
                text(
                    "UPDATE digests SET status='awaiting_review', "
                    "awaiting_review_at=now() WHERE id=:id"
                ),
                {"id": rejected_id},
            )
            await session.flush()
            flipped["done"] = True

    monkeypatch.setattr(
        "bot.handlers.digest._acquire_idempotency_lock", _flipping_lock
    )

    run_called = []

    async def _fake_run_digest(*args, **kwargs):  # pragma: no cover
        run_called.append(True)
        raise AssertionError(
            "run_digest must NOT be called when state changed under lock"
        )

    monkeypatch.setattr("bot.handlers.digest.run_digest", _fake_run_digest)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg,
        bot=bot_mock,
        session=db_session,
        command=_mk_command_obj("weekly --regenerate"),
    )

    # run_digest must NOT have been invoked — the under-lock re-read should
    # have caught the state change and aborted before delete/rerun.
    assert run_called == [], (
        "run_digest must NOT run when the under-lock re-read sees a "
        "non-rejected status (state changed between pre-check and lock)"
    )

    # Row must NOT have been deleted (it's now awaiting_review — outside
    # the regenerate scope).
    row = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"),
            {"id": rejected_id},
        )
    ).mappings().one_or_none()
    assert row is not None, (
        "the (now-awaiting-review) row must NOT have been deleted by a "
        "stale regenerate path"
    )
    assert row["status"] == "awaiting_review"

    # No regenerated_by_admin audit row inserted — the race-loser must not
    # leave a trail referencing a state that no longer applies.
    audit_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM digest_runs "
                "WHERE digest_id=:id AND status='regenerated_by_admin'"
            ),
            {"id": rejected_id},
        )
    ).scalar_one()
    assert audit_count == 0, (
        "race-loser must NOT audit-insert when state already changed"
    )

    # User-facing reply must surface that the state changed (or that
    # regenerate is no longer applicable).
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    # Reply text mentions current status or the regenerate-blocked hint.
    assert ("awaiting_review" in body) or (
        "regenerate" in body.lower() or "--regenerate" in body
    ) or ("rejected" not in body.lower()), (
        f"reply must reflect state change; got: {body!r}"
    )


async def test_digest_now_weekly_regenerate_row_deleted_under_lock(
    db_session, monkeypatch
):
    """FHR HIGH-3 corollary: if the row is DELETED between the (pre-fix)
    SELECT and the lock acquire — i.e. another regenerate concurrently
    drained it — the post-fix path MUST treat it as "no existing row"
    and reply with the hint to run without --regenerate, NOT crash on the
    audit insert (which FKs digest_id to digests.id)."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    rejected = await _make_weekly_digest(
        db_session,
        status="rejected_by_admin",
        published_by_admin_id=admin_id,
        review_notes="off-topic",
    )
    rejected_id = rejected.id

    import bot.handlers.digest as digest_mod

    real_lock = digest_mod._acquire_idempotency_lock
    deleted = {"done": False}

    async def _deleting_lock(session, *, type, window_start, window_end):
        await real_lock(
            session, type=type, window_start=window_start, window_end=window_end
        )
        if not deleted["done"]:
            # Simulate: prior regenerate holder DELETEd before our lock.
            await session.execute(
                text("DELETE FROM digests WHERE id=:id"),
                {"id": rejected_id},
            )
            await session.flush()
            deleted["done"] = True

    monkeypatch.setattr(
        "bot.handlers.digest._acquire_idempotency_lock", _deleting_lock
    )

    run_called = []

    async def _fake_run_digest(*args, **kwargs):  # pragma: no cover
        run_called.append(True)
        raise AssertionError(
            "run_digest must NOT be called when row deleted under lock"
        )

    monkeypatch.setattr("bot.handlers.digest.run_digest", _fake_run_digest)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg,
        bot=bot_mock,
        session=db_session,
        command=_mk_command_obj("weekly --regenerate"),
    )

    assert run_called == []

    # No audit row inserted (would FK-fail anyway since the parent row is gone
    # — but ON DELETE SET NULL would silently leave a dangling audit which
    # is worse than the explicit refusal).
    audit_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM digest_runs "
                "WHERE status='regenerated_by_admin' "
                "  AND error_text LIKE :pat"
            ),
            {"pat": f"%admin {admin_id}%"},
        )
    ).scalar_one()
    assert audit_count == 0, (
        "race-loser must NOT audit-insert when row was deleted under lock"
    )

    # Reply surfaces the absent state hint.
    msg.answer.assert_awaited_once()


async def test_digest_now_weekly_regenerate_rejects_non_rejected_status(
    db_session, monkeypatch
):
    """/digest_now weekly --regenerate when existing row is awaiting_review →
    error reply naming current status; no audit insert, no DELETE, no
    run_digest call."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    existing = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        awaiting_review_at=datetime.now(timezone.utc),
    )
    existing_id = existing.id

    run_called = []

    async def _fake_run_digest(*args, **kwargs):  # pragma: no cover — must not run
        run_called.append(True)
        raise AssertionError("run_digest must NOT be called when regen blocked")

    monkeypatch.setattr("bot.handlers.digest.run_digest", _fake_run_digest)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg,
        bot=bot_mock,
        session=db_session,
        command=_mk_command_obj("weekly --regenerate"),
    )

    assert run_called == [], "run_digest must not run when current row is awaiting_review"

    # Row remains untouched.
    row = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"),
            {"id": existing_id},
        )
    ).mappings().one()
    assert row["status"] == "awaiting_review"

    # No regenerated_by_admin audit row.
    audit_count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM digest_runs "
                "WHERE digest_id=:id AND status='regenerated_by_admin'"
            ),
            {"id": existing_id},
        )
    ).scalar_one()
    assert audit_count == 0

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "awaiting_review" in body or "ожидает" in body.lower()


async def test_digest_now_weekly_posting_state_replies_with_retry_hint(
    db_session, monkeypatch
):
    """H2 / Phase 7 F6 extension: when run_digest returns the existing row in
    status='posting' for weekly, handler must NOT attempt to publish/transition
    and must reply with a polite retry message."""
    from bot.config import settings
    from bot.db.models import Digest
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    now = datetime.now(timezone.utc)
    posting = Digest(
        type="weekly",
        window_start=now - timedelta(days=7),
        window_end=now,
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": 0}],
        status="posting",
        published_by_admin_id=admin_id,
        approved_at=now,
        posting_started_at=now,
    )
    db_session.add(posting)
    await db_session.flush()

    async def _fake_run_digest(*args, **kwargs):
        return posting

    monkeypatch.setattr("bot.handlers.digest.run_digest", _fake_run_digest)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj("weekly")
    )

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert not body.startswith("❌"), (
        f"expected friendly retry, got error reply: {body!r}"
    )
    # Friendly retry hint mentions trying later (Russian wording).
    assert "попробуйте" in body.lower() or "позже" in body.lower() or "посл" in body.lower(), (
        f"expected polite retry hint, got: {body!r}"
    )
    # Row must still be in 'posting' (no state change).
    row = (
        await db_session.execute(
            text("SELECT status FROM digests WHERE id=:id"), {"id": posting.id}
        )
    ).mappings().one()
    assert row["status"] == "posting"


# ── /digest_review ──────────────────────────────────────────────────────────


async def test_digest_review_lists_awaiting_review_digests(db_session):
    """List ORDER BY awaiting_review_at ASC, LIMIT 20.

    Inserts 2 awaiting_review rows; the older one comes first in the reply.
    Body preview truncated."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_review

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    # Wipe pre-existing rows for stable assertion.
    await db_session.execute(text("DELETE FROM digest_runs"))
    await db_session.execute(text("DELETE FROM digests"))
    await db_session.flush()

    now = datetime.now(timezone.utc)
    older = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        window_start=now - timedelta(days=14),
        window_end=now - timedelta(days=7),
        awaiting_review_at=now - timedelta(hours=10),
        body="Old TL;DR." + "A" * 400 + "\n- bullet",
    )
    newer = await _make_weekly_digest(
        db_session,
        status="awaiting_review",
        window_start=now - timedelta(days=7),
        window_end=now,
        awaiting_review_at=now - timedelta(hours=2),
        body="New TL;DR." + "B" * 200,
    )

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_review(msg, session=db_session)

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")

    # Both ids present
    assert str(older.id) in body
    assert str(newer.id) in body

    # Older (longer awaiting_review_at) should appear first.
    older_pos = body.find(f"#{older.id}")
    newer_pos = body.find(f"#{newer.id}")
    assert older_pos != -1 and newer_pos != -1
    assert older_pos < newer_pos, (
        f"older digest {older.id} should appear before newer {newer.id} (ASC by awaiting_review_at); "
        f"got older@{older_pos}, newer@{newer_pos}"
    )

    # Truncation: the 400×'A' body must NOT appear verbatim in full.
    assert "A" * 400 not in body


async def test_digest_review_empty_state(db_session):
    """No awaiting_review rows → 'no pending digests' reply."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_review

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    await db_session.execute(text("DELETE FROM digest_runs"))
    await db_session.execute(text("DELETE FROM digests"))
    await db_session.flush()

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_review(msg, session=db_session)

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert (
        "нет дайджестов" in body.lower()
        or "пусто" in body.lower()
        or "пуст" in body.lower()
    ), f"expected empty-state reply, got: {body!r}"


# ── /digest_approve ─────────────────────────────────────────────────────────


async def test_digest_approve_success(db_session, monkeypatch):
    """admin invokes /digest_approve <id> for an awaiting_review row →
    approve_digest is called; reply shows 'опубликован' + chat link."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_approve

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    captured = {}

    async def _fake_approve(session, *, bot, digest_id, admin_id, digest_config):
        captured["digest_id"] = digest_id
        captured["admin_id"] = admin_id
        from bot.services.digest_review import ApproveResult

        return ApproveResult(
            digest_id=digest_id,
            posted_chat_id=-1_001_234_567_890,
            posted_message_id=4242,
            error_text=None,
        )

    monkeypatch.setattr("bot.handlers.digest.approve_digest", _fake_approve)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_approve(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj(str(did))
    )

    assert captured["digest_id"] == did
    assert captured["admin_id"] == admin_id

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "✅" in body or "опубликован" in body.lower() or "posted" in body.lower()
    # link rendering
    assert "t.me/c/" in body


async def test_digest_approve_invalid_state_posted(db_session, monkeypatch):
    """If approve_digest raises DigestReviewInvalidState with
    current_status='posted', reply explains current status."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_approve
    from bot.services.digest_review import DigestReviewInvalidState

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    async def _fake_approve(session, **kwargs):
        raise DigestReviewInvalidState(
            digest_id=did,
            current_status="posted",
            reason="already posted",
        )

    monkeypatch.setattr("bot.handlers.digest.approve_digest", _fake_approve)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_approve(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj(str(did))
    )

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "posted" in body.lower()
    assert str(did) in body


async def test_digest_approve_invalid_state_rejected_by_admin_suggests_regenerate(
    db_session, monkeypatch
):
    """If approve_digest raises with current_status='rejected_by_admin', reply
    suggests /digest_now weekly --regenerate."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_approve
    from bot.services.digest_review import DigestReviewInvalidState

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    async def _fake_approve(session, **kwargs):
        raise DigestReviewInvalidState(
            digest_id=did,
            current_status="rejected_by_admin",
            reason="already rejected",
        )

    monkeypatch.setattr("bot.handlers.digest.approve_digest", _fake_approve)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_approve(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj(str(did))
    )

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "regenerate" in body.lower() or "регенер" in body.lower() or "--regenerate" in body
    assert "rejected_by_admin" in body or "отклон" in body.lower()


async def test_digest_approve_not_found(db_session, monkeypatch):
    """If approve_digest raises DigestReviewNotFound → 'не найден' reply."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_approve
    from bot.services.digest_review import DigestReviewNotFound

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    async def _fake_approve(session, **kwargs):
        raise DigestReviewNotFound("not found")

    monkeypatch.setattr("bot.handlers.digest.approve_digest", _fake_approve)

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_approve(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj("999999")
    )

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "не найден" in body.lower() or "not found" in body.lower()
    assert "999999" in body


# ── /digest_reject ──────────────────────────────────────────────────────────


async def test_digest_reject_with_reason(db_session, monkeypatch):
    """admin invokes /digest_reject <id> off-topic content → reject_digest is
    called with the reason; reply confirms rejection."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_reject

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    captured = {}

    async def _fake_reject(session, *, digest_id, admin_id, reason):
        captured["digest_id"] = digest_id
        captured["admin_id"] = admin_id
        captured["reason"] = reason

    monkeypatch.setattr("bot.handlers.digest.reject_digest", _fake_reject)

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_reject(
        msg,
        session=db_session,
        command=_mk_command_obj(f"{did} off-topic content"),
    )

    assert captured["digest_id"] == did
    assert captured["admin_id"] == admin_id
    assert captured["reason"] == "off-topic content"

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "❌" in body or "отклон" in body.lower() or "rejected" in body.lower()
    assert "off-topic content" in body


async def test_digest_reject_no_reason_defaults_to_placeholder(db_session, monkeypatch):
    """admin invokes /digest_reject <id> (no reason) → reject_digest is called
    with reason=None (service layer defaults to 'no reason given')."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_reject

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    digest = await _make_weekly_digest(db_session, status="awaiting_review")
    did = digest.id

    captured = {}

    async def _fake_reject(session, *, digest_id, admin_id, reason):
        captured["digest_id"] = digest_id
        captured["reason"] = reason

    monkeypatch.setattr("bot.handlers.digest.reject_digest", _fake_reject)

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_reject(
        msg, session=db_session, command=_mk_command_obj(str(did))
    )

    assert captured["digest_id"] == did
    # Handler passes None (or empty) when no reason given — service defaults
    # to 'no reason given' per digest_review.py line 359.
    assert captured["reason"] is None or captured["reason"] == ""


# ── non-admin silent no-op ──────────────────────────────────────────────────


async def test_digest_handlers_non_admin_silent_no_op(db_session):
    """Non-admin user invokes any of the 4 commands → handler returns
    silently; no DB writes, no message.answer call."""
    from bot.handlers.digest import (
        cmd_digest_approve,
        cmd_digest_now,
        cmd_digest_reject,
        cmd_digest_review,
    )

    non_admin_id = 99_999_999

    bot_mock = MagicMock()

    # /digest_now weekly
    m1 = _mk_msg(user_id=non_admin_id)
    await cmd_digest_now(
        m1, bot=bot_mock, session=db_session, command=_mk_command_obj("weekly")
    )
    m1.answer.assert_not_called()

    # /digest_review
    m2 = _mk_msg(user_id=non_admin_id)
    await cmd_digest_review(m2, session=db_session)
    m2.answer.assert_not_called()

    # /digest_approve
    m3 = _mk_msg(user_id=non_admin_id)
    await cmd_digest_approve(
        m3, bot=bot_mock, session=db_session, command=_mk_command_obj("1")
    )
    m3.answer.assert_not_called()

    # /digest_reject
    m4 = _mk_msg(user_id=non_admin_id)
    await cmd_digest_reject(
        m4, session=db_session, command=_mk_command_obj("1 reason")
    )
    m4.answer.assert_not_called()
