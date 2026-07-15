"""Tests for T8-05 — Phase 8 weekly-digest scheduler jobs.

Covers:
- ``digest_weekly_job``: flag-OFF strict no-op
- ``digest_weekly_job``: ISO week window calc (last Mon 00:00 MSK → this Mon
  00:00 MSK, stored as UTC)
- ``digest_weekly_job``: draft → automatic publication, without review
- ``digest_weekly_job``: idempotency-return non-draft statuses do NOT publish
- ``digest_weekly_job``: ``failed``/``cost_exceeded`` paths fire admin DM
- ``digest_weekly_job``: registered as Mon 09:00 MSK cron
- ``digest_stale_review_reaper_job``: 48h DM marker append (M4 guarded UPDATE)
- ``digest_stale_review_reaper_job``: 48h DM is idempotent (marker present →
  no-op)
- ``digest_stale_review_reaper_job``: 7d auto-reject → ``rejected_by_reaper``
  terminal + audit insert
- ``digest_stale_review_reaper_job``: guarded UPDATE no-ops when an admin
  approves between the SELECT and the UPDATE
- ``digest_stale_review_reaper_job``: registered as 30-min interval

PHASE8_PLAN.md §5.G + §13 AC1 + §13 AC7.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


# ── helpers ─────────────────────────────────────────────────────────────────


async def _set_flag(db_session, key: str, enabled: bool) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo

    await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=enabled)


def _fake_session_ctx(db_session):
    """Build a context manager that yields the test ``db_session``.

    The job body opens its own ``async_session()``; we patch the symbol so the
    job operates on the test transaction (which gets rolled back at fixture
    teardown).
    """

    @asynccontextmanager
    async def _ctx():
        yield db_session

    return _ctx


# ── digest_weekly_job: flag-OFF strict no-op ────────────────────────────────


async def test_digest_weekly_job_skipped_when_flag_off(db_session, monkeypatch):
    """AC1: flag default OFF → ``run_digest`` must NOT be invoked.

    The job body's first action is the feature-flag check; if the flag is
    False the function returns immediately. We assert by mocking
    ``run_digest`` and verifying zero calls.
    """
    await _set_flag(db_session, "memory.digests.weekly.enabled", False)
    await db_session.flush()

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    run_digest_mock = AsyncMock()
    monkeypatch.setattr("bot.services.digests.run_digest", run_digest_mock)

    fake_bot = MagicMock()
    await scheduler_mod.digest_weekly_job(fake_bot)

    assert run_digest_mock.await_count == 0, (
        "run_digest must not be invoked when memory.digests.weekly.enabled is OFF"
    )


# ── digest_weekly_job: ISO week window ──────────────────────────────────────


async def test_digest_weekly_job_window_is_iso_mon_to_mon_msk(db_session, monkeypatch):
    """AC1 / §5.G: window_start = last Mon 00:00 MSK; window_end = this Mon
    00:00 MSK. Both must be passed to ``run_digest`` in UTC.

    We pin ``datetime.now`` via a stub on ``bot.services.scheduler.datetime``
    to a known Mon 09:15 MSK so the math is deterministic.
    """
    await _set_flag(db_session, "memory.digests.weekly.enabled", True)
    await db_session.flush()

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    # Pin the scheduler's datetime.now() — Mon 2026-05-18 09:15 MSK.
    from zoneinfo import ZoneInfo

    msk = ZoneInfo("Europe/Moscow")
    pinned_now_msk = datetime(2026, 5, 18, 9, 15, 0, tzinfo=msk)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return pinned_now_msk.astimezone(timezone.utc).replace(tzinfo=None)
            return pinned_now_msk.astimezone(tz)

    monkeypatch.setattr(scheduler_mod, "datetime", _FakeDatetime)

    captured = {}

    async def _fake_run_digest(session, **kwargs):
        captured["window_start"] = kwargs["window_start"]
        captured["window_end"] = kwargs["window_end"]
        captured["type"] = kwargs["type"]
        # Return a stub with a status that bypasses the rest of the job body.
        digest = MagicMock()
        digest.id = 999
        digest.status = "skipped"
        return digest

    monkeypatch.setattr("bot.services.digests.run_digest", _fake_run_digest)

    fake_bot = MagicMock()
    await scheduler_mod.digest_weekly_job(fake_bot)

    # Expected: this Mon 00:00 MSK = 2026-05-18 00:00 MSK = 2026-05-17 21:00 UTC
    # Last Mon 00:00 MSK = 2026-05-11 00:00 MSK = 2026-05-10 21:00 UTC
    expected_start = datetime(2026, 5, 10, 21, 0, 0, tzinfo=timezone.utc)
    expected_end = datetime(2026, 5, 17, 21, 0, 0, tzinfo=timezone.utc)
    assert captured["type"] == "weekly"
    assert captured["window_start"] == expected_start, captured
    assert captured["window_end"] == expected_end, captured
    # 7-day span:
    assert (captured["window_end"] - captured["window_start"]).total_seconds() == 7 * 86400


# ── digest_weekly_job: draft → transition_to_awaiting_review ───────────────


async def test_digest_weekly_job_publishes_draft_without_review(db_session, monkeypatch):
    """A fresh weekly draft is posted automatically to the source chat."""
    monkeypatch.setenv("DIGEST_SOURCE_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("DIGEST_DESTINATION_CHAT_ID", "-1001234567890")
    await _set_flag(db_session, "memory.digests.weekly.enabled", True)
    await db_session.flush()

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    digest = MagicMock()
    digest.id = 4242
    digest.status = "draft"
    digest.error_text = None

    monkeypatch.setattr("bot.services.digests.run_digest", AsyncMock(return_value=digest))

    publish_mock = AsyncMock()

    monkeypatch.setattr(
        "bot.services.digest_publisher.publish_digest",
        publish_mock,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_weekly_job(fake_bot)

    publish_mock.assert_awaited_once()
    call_kwargs = publish_mock.await_args.kwargs
    assert call_kwargs["bot"] is fake_bot
    assert call_kwargs["digest"] is digest
    assert call_kwargs["digest_config"].source_chat_id == (
        call_kwargs["digest_config"].destination_chat_id
    )


# ── digest_weekly_job: idempotency-return non-draft statuses ───────────────


@pytest.mark.parametrize(
    "existing_status",
    ["awaiting_review", "approved_for_publish", "posting", "posted"],
)
async def test_digest_weekly_job_skips_publish_on_non_draft_status(
    db_session, monkeypatch, existing_status
):
    """H4 cron status-aware match block: when ``run_digest`` returns an
    EXISTING row in an advanced status, it is not posted a second time.
    """
    await _set_flag(db_session, "memory.digests.weekly.enabled", True)
    await db_session.flush()

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    digest = MagicMock()
    digest.id = 555
    digest.status = existing_status

    monkeypatch.setattr("bot.services.digests.run_digest", AsyncMock(return_value=digest))

    publish_mock = AsyncMock()
    monkeypatch.setattr("bot.services.digest_publisher.publish_digest", publish_mock)

    fake_bot = MagicMock()
    await scheduler_mod.digest_weekly_job(fake_bot)

    assert publish_mock.await_count == 0, (
        f"publish_digest must NOT be called for status={existing_status!r}"
    )


# ── digest_weekly_job: failed / cost_exceeded → admin DM ─────────────────


@pytest.mark.parametrize("error_status", ["failed", "cost_exceeded"])
async def test_digest_weekly_job_admin_dm_on_error_status(db_session, monkeypatch, error_status):
    """H4: ``failed`` / ``cost_exceeded`` paths route to
    ``notify_admins_digest_failure`` so the admin sees the error explicitly.
    """
    await _set_flag(db_session, "memory.digests.weekly.enabled", True)
    await db_session.flush()

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    digest = MagicMock()
    digest.id = 777
    digest.status = error_status
    digest.error_text = "weekly_test_error"

    monkeypatch.setattr("bot.services.digests.run_digest", AsyncMock(return_value=digest))

    notify_mock = AsyncMock()
    monkeypatch.setattr(
        "bot.services.digest_admin_notify.notify_admins_digest_failure",
        notify_mock,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_weekly_job(fake_bot)

    assert notify_mock.await_count == 1, (
        f"admin DM must fire for status={error_status!r}; got {notify_mock.await_count} calls"
    )
    call_kwargs = notify_mock.await_args.kwargs
    assert call_kwargs["digest_id"] == 777
    assert call_kwargs["status"] == error_status


# ── digest_weekly_job: APScheduler cron registration ────────────────────────


async def test_digest_weekly_job_registered_at_mon_09_00_msk():
    """Weekly cron is registered at Monday 09:00 MSK.

    Inspects the APScheduler registration via ``get_job``. The trigger is
    a ``CronTrigger`` with fields ``day_of_week='mon'``, ``hour=9``,
    ``minute=0``, ``timezone=Europe/Moscow``.
    """
    from bot.services import scheduler as scheduler_mod

    # Snapshot original start() so the test never actually starts the loop.
    started = []
    real_start = scheduler_mod.scheduler.start
    scheduler_mod.scheduler.start = lambda: started.append(True)  # type: ignore[assignment]
    try:

        class _FakeBot:
            pass

        scheduler_mod.start_scheduler(_FakeBot())  # type: ignore[arg-type]
        job = scheduler_mod.scheduler.get_job("digest_weekly")
        assert job is not None, "digest_weekly must be registered by start_scheduler"

        trigger = job.trigger
        # CronTrigger.fields is a list of Field instances with .name + .expressions.
        fields_by_name = {f.name: f for f in trigger.fields}
        assert "Moscow" in str(trigger.timezone), (
            f"expected Europe/Moscow timezone, got {trigger.timezone!r}"
        )

        # day_of_week='mon' is rendered by apscheduler as the field having
        # 'mon' in its expression list. Compare via str(expressions[0]).
        dow = fields_by_name["day_of_week"]
        assert "mon" in str(dow).lower(), f"day_of_week field={dow!r}"
        hour = fields_by_name["hour"]
        assert "9" in str(hour), f"hour field={hour!r}"
        minute = fields_by_name["minute"]
        assert "0" in str(minute), f"minute field={minute!r}"
    finally:
        scheduler_mod.scheduler.start = real_start  # type: ignore[assignment]
        for jid in (
            "digest_weekly",
            "digest_stale_review_reaper",
            "digest_daily",
            "digest_stale_posting_reaper",
            "process_invite_outbox",
            "check_vouch_deadlines",
            "check_intro_refresh",
            "sync_google_sheets",
            "forget_cascade_worker",
            "extraction_scheduler_tick",
        ):
            try:
                scheduler_mod.scheduler.remove_job(jid)
            except Exception:
                pass


# ── digest_stale_review_reaper_job: 48h DM + marker append ─────────────────


async def test_digest_stale_review_reaper_48h_dm_and_marker(db_session, monkeypatch):
    """AC7 first pass: row in ``awaiting_review`` with
    ``awaiting_review_at`` 49h ago and no ``[48h_notified]`` marker →
    reaper appends marker and dispatches an admin DM.
    """
    from bot.db.models import Digest

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="weekly",
        window_start=now - timedelta(days=14),
        window_end=now - timedelta(days=7),
        body_markdown="weekly draft",
        citations=[],
        status="awaiting_review",
        awaiting_review_at=now - timedelta(hours=49),
        review_notes=None,
    )
    db_session.add(digest)
    await db_session.flush()
    digest_id = digest.id

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    notify_mock = AsyncMock()
    monkeypatch.setattr(
        "bot.services.digest_admin_notify.notify_admins_digest_failure",
        notify_mock,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_stale_review_reaper_job(fake_bot)

    # DM fired with the right error_text:
    assert notify_mock.await_count == 1, "48h DM must fire exactly once for this row"
    kwargs = notify_mock.await_args.kwargs
    assert kwargs["digest_id"] == digest_id
    assert kwargs["error_text"] == "review_48h_reminder"

    # Marker appended in review_notes:
    refreshed = await db_session.execute(
        text("SELECT review_notes, status FROM digests WHERE id = :id"),
        {"id": digest_id},
    )
    row = refreshed.mappings().one()
    assert row["status"] == "awaiting_review", (
        "48h pass must NOT terminate the row — only the 7d pass does"
    )
    assert "[48h_notified]" in (row["review_notes"] or ""), (
        f"expected marker in review_notes; got {row['review_notes']!r}"
    )


async def test_digest_stale_review_reaper_48h_skips_if_already_notified(db_session, monkeypatch):
    """AC7 idempotency: a row already marked ``[48h_notified]`` is NOT
    re-DM'd on the next reaper tick.
    """
    from bot.db.models import Digest

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="weekly",
        window_start=now - timedelta(days=14),
        window_end=now - timedelta(days=7),
        body_markdown="weekly draft",
        citations=[],
        status="awaiting_review",
        awaiting_review_at=now - timedelta(hours=60),
        # Per PHASE8_PLAN.md §5.G, the marker is the literal token
        # ``[48h_notified]`` (no timestamp) — that's what the reaper's LIKE
        # filter ``review_notes NOT LIKE '%[48h_notified]%'`` matches.
        review_notes="[48h_notified]",
    )
    db_session.add(digest)
    await db_session.flush()

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    notify_mock = AsyncMock()
    monkeypatch.setattr(
        "bot.services.digest_admin_notify.notify_admins_digest_failure",
        notify_mock,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_stale_review_reaper_job(fake_bot)

    # No 48h DM (row already marked) and no 7d auto-reject (not yet 7d).
    assert notify_mock.await_count == 0, (
        f"reaper must be a no-op for already-marked row; got {notify_mock.await_count} DM calls"
    )


# ── digest_stale_review_reaper_job: 7d auto-reject ─────────────────────────


async def test_digest_stale_review_reaper_7d_auto_reject(db_session, monkeypatch):
    """AC7 second pass: row in ``awaiting_review`` with
    ``awaiting_review_at`` 8d ago → transition to ``rejected_by_reaper``
    + ``digest_runs`` audit row + admin DM with ``review_7d_auto_rejected``
    error_text.
    """
    from bot.db.models import Digest

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="weekly",
        window_start=now - timedelta(days=15),
        window_end=now - timedelta(days=8),
        body_markdown="weekly draft body",
        citations=[],
        status="awaiting_review",
        awaiting_review_at=now - timedelta(days=8),
        review_notes="[48h_notified]",
    )
    db_session.add(digest)
    await db_session.flush()
    digest_id = digest.id

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    notify_mock = AsyncMock()
    monkeypatch.setattr(
        "bot.services.digest_admin_notify.notify_admins_digest_failure",
        notify_mock,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_stale_review_reaper_job(fake_bot)

    # Row terminated as rejected_by_reaper.
    refreshed = await db_session.execute(
        text("SELECT status, review_notes FROM digests WHERE id = :id"),
        {"id": digest_id},
    )
    row = refreshed.mappings().one()
    assert row["status"] == "rejected_by_reaper", (
        f"7d auto-reject must transition status to rejected_by_reaper; got {row['status']!r}"
    )
    assert "[stale_7d]" in (row["review_notes"] or "")

    # digest_runs audit row written.
    audit = await db_session.execute(
        text(
            "SELECT status, error_text FROM digest_runs "
            "WHERE digest_id = :id ORDER BY id DESC LIMIT 1"
        ),
        {"id": digest_id},
    )
    audit_row = audit.mappings().one()
    assert audit_row["status"] == "rejected_by_reaper"
    assert audit_row["error_text"] == "review_deadline_exceeded"

    # Admin DM fired with review_7d_auto_rejected tag.
    assert notify_mock.await_count == 1
    kwargs = notify_mock.await_args.kwargs
    assert kwargs["digest_id"] == digest_id
    assert kwargs["error_text"] == "review_7d_auto_rejected"


async def test_digest_stale_review_reaper_guarded_update_no_op_after_admin_approval(
    db_session, monkeypatch
):
    """M4 guarded UPDATE: if a row was at 49h awaiting_review but an admin
    APPROVED it between the reaper's SELECT and the marker UPDATE, the
    guarded UPDATE rowcount=0 — no DM fires, no marker appended.

    We simulate this race by pre-advancing the row to
    ``approved_for_publish`` BEFORE the reaper runs. Since the reaper's
    SELECT filter is ``status='awaiting_review'``, the row is NOT selected
    and no DM fires. This is the same observable outcome as the race —
    rowcount=0 protection.
    """
    from bot.db.models import Digest

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="weekly",
        window_start=now - timedelta(days=14),
        window_end=now - timedelta(days=7),
        body_markdown="weekly draft",
        citations=[],
        # Pre-advanced — admin won the race.
        status="approved_for_publish",
        awaiting_review_at=now - timedelta(hours=49),
        # ck_digests_approved_audit: status='approved_for_publish' rows must
        # have BOTH published_by_admin_id AND approved_at set.
        approved_at=now - timedelta(minutes=1),
        published_by_admin_id=149820031,
        review_notes=None,
    )
    db_session.add(digest)
    await db_session.flush()
    digest_id = digest.id

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    notify_mock = AsyncMock()
    monkeypatch.setattr(
        "bot.services.digest_admin_notify.notify_admins_digest_failure",
        notify_mock,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_stale_review_reaper_job(fake_bot)

    # No DM, no marker, status unchanged.
    assert notify_mock.await_count == 0, (
        "reaper must NOT DM a row that was advanced by admin between cycles"
    )
    refreshed = await db_session.execute(
        text("SELECT status, review_notes FROM digests WHERE id = :id"),
        {"id": digest_id},
    )
    row = refreshed.mappings().one()
    assert row["status"] == "approved_for_publish"
    assert row["review_notes"] is None or "[48h_notified]" not in row["review_notes"]


# ── digest_stale_review_reaper: APScheduler interval registration ──────────


async def test_digest_stale_review_reaper_registered_30min_interval():
    """AC7: reaper registered as 30-min interval job."""
    from bot.services import scheduler as scheduler_mod

    started = []
    real_start = scheduler_mod.scheduler.start
    scheduler_mod.scheduler.start = lambda: started.append(True)  # type: ignore[assignment]
    try:

        class _FakeBot:
            pass

        scheduler_mod.start_scheduler(_FakeBot())  # type: ignore[arg-type]
        job = scheduler_mod.scheduler.get_job("digest_stale_review_reaper")
        assert job is not None, "digest_stale_review_reaper must be registered"
        trigger = job.trigger
        # IntervalTrigger has .interval (timedelta).
        interval = getattr(trigger, "interval", None)
        assert interval is not None, f"expected IntervalTrigger; got {trigger!r}"
        assert interval.total_seconds() == 30 * 60, (
            f"expected 30-min interval (1800s); got {interval.total_seconds()}s"
        )
    finally:
        scheduler_mod.scheduler.start = real_start  # type: ignore[assignment]
        for jid in (
            "digest_weekly",
            "digest_stale_review_reaper",
            "digest_daily",
            "digest_stale_posting_reaper",
            "process_invite_outbox",
            "check_vouch_deadlines",
            "check_intro_refresh",
            "sync_google_sheets",
            "forget_cascade_worker",
            "extraction_scheduler_tick",
        ):
            try:
                scheduler_mod.scheduler.remove_job(jid)
            except Exception:
                pass
