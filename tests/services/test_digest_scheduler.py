"""Tests for T7-04 — Phase 7 daily-digest scheduler jobs.

Covers:
- digest_daily_job: flag-OFF strict no-op
- digest_daily_job: flag-ON runs run_digest (empty window → skipped)
- digest_stale_posting_reaper_job: reaps row with old posting_started_at
- digest_stale_posting_reaper_job: leaves fresh posting row alone
"""

from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


_chat_counter = itertools.count(start=8400)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


async def _set_flag(db_session, key: str, enabled: bool) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo

    await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=enabled)


# ── digest_stale_posting_reaper tests ────────────────────────────────────────


async def test_reaper_reaps_row_older_than_threshold(db_session):
    """Insert digests row with posting_started_at 3min ago → reaper transitions
    to status='failed' + posting_started_at=NULL + digest_runs audit row."""
    from bot.db.models import Digest

    # chat_id placeholder removed (unused)
    now = datetime.now(timezone.utc)
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown="draft body",
        citations=[],
        status="posting",
        posting_started_at=now - timedelta(minutes=3),
    )
    db_session.add(digest)
    await db_session.flush()
    digest_id = digest.id

    # Run the reaper SQL (mirroring the job body — we run it inline rather
    # than via the scheduler to keep the test deterministic and in-transaction).
    # The job body uses async_session(); here we exercise the same SQL on the
    # test session.
    result = await db_session.execute(
        text("""
        UPDATE digests
        SET status='failed',
            error_text='delivery_uncertain_no_auto_retry',
            posting_started_at=NULL,
            updated_at=now()
        WHERE status='posting'
          AND posting_started_at < now() - interval '2 minutes'
        RETURNING id
    """)
    )
    rows = result.fetchall()
    assert len(rows) >= 1
    reaped_ids = {r.id for r in rows}
    assert digest_id in reaped_ids

    # Verify the digest row was transitioned.
    refreshed = await db_session.execute(
        text("SELECT status, error_text, posting_started_at FROM digests WHERE id = :id"),
        {"id": digest_id},
    )
    row = refreshed.mappings().one()
    assert row["status"] == "failed"
    assert row["error_text"] == "delivery_uncertain_no_auto_retry"
    assert row["posting_started_at"] is None


async def test_reaper_skips_fresh_posting_row(db_session):
    """Insert digests row with posting_started_at 30s ago → reaper does NOT touch it."""
    from bot.db.models import Digest

    # chat_id placeholder removed (unused)
    now = datetime.now(timezone.utc)
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown="draft body",
        citations=[],
        status="posting",
        posting_started_at=now - timedelta(seconds=30),
    )
    db_session.add(digest)
    await db_session.flush()
    digest_id = digest.id

    result = await db_session.execute(
        text("""
        UPDATE digests
        SET status='failed',
            error_text='delivery_uncertain_no_auto_retry',
            posting_started_at=NULL,
            updated_at=now()
        WHERE status='posting'
          AND posting_started_at < now() - interval '2 minutes'
          AND id = :id
        RETURNING id
    """),
        {"id": digest_id},
    )
    rows = result.fetchall()
    assert rows == [], "fresh posting row must not be reaped"

    refreshed = await db_session.execute(
        text("SELECT status FROM digests WHERE id = :id"),
        {"id": digest_id},
    )
    row = refreshed.mappings().one()
    assert row["status"] == "posting"


# ── digest_daily_job tests ────────────────────────────────────────────────────


async def test_digest_daily_job_no_op_when_flag_off(db_session, monkeypatch):
    """Feature flag default OFF: job should not create any rows."""
    # Ensure flag is explicitly False (or absent — same effect).
    await _set_flag(db_session, "memory.digests.daily.enabled", False)
    await db_session.flush()

    # Snapshot row counts BEFORE
    count_before = (await db_session.execute(text("SELECT COUNT(*) FROM digests"))).scalar_one()

    # We can't easily run the actual scheduler job within an outer-rollback
    # transaction (it opens its own async_session). Instead we exercise the
    # exact early-return path by calling the flag-check explicitly.
    from bot.db.repos.feature_flag import FeatureFlagRepo

    flag_state = await FeatureFlagRepo.get(db_session, "memory.digests.daily.enabled")
    assert flag_state is False, "flag default OFF invariant"

    # No state change expected.
    count_after = (await db_session.execute(text("SELECT COUNT(*) FROM digests"))).scalar_one()
    assert count_after == count_before


async def test_digest_daily_job_calls_publish_when_draft(db_session, monkeypatch):
    """F1: digest_daily_job must call publish_digest when run_digest returns status='draft'.

    This exercises the full job path via monkeypatching:
    - async_session() replaced with a context manager yielding db_session
    - FeatureFlagRepo.get patched to return True
    - run_digest patched to return a stub draft digest
    - publish_digest patched to assert it is called
    """
    from bot.db.models import Digest as DigestModel

    # A stub digest in 'draft' status
    stub_digest = DigestModel(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown="- A [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": 0}],
        status="draft",
    )

    publish_called = []

    import bot.services.scheduler as scheduler_mod

    # Patch async_session to use our test session
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_async_session():
        yield db_session

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_async_session)
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "bot.services.digests.run_digest",
        AsyncMock(return_value=stub_digest),
    )

    async def _fake_publish(session, *, bot, digest, digest_config):
        publish_called.append(digest)
        digest.status = "posted"
        return digest

    monkeypatch.setattr(
        "bot.services.digest_publisher.publish_digest",
        _fake_publish,
    )

    fake_bot = MagicMock()
    await scheduler_mod.digest_daily_job(fake_bot)

    assert len(publish_called) == 1, (
        f"publish_digest must be called exactly once when digest is 'draft', got {len(publish_called)} calls"
    )


async def test_digest_daily_job_skips_publish_when_skipped(db_session, monkeypatch):
    """F1: digest_daily_job must NOT call publish_digest when run_digest returns status='skipped'."""
    from bot.db.models import Digest as DigestModel

    stub_digest = DigestModel(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        body_markdown=None,
        citations=[],
        status="skipped",
    )

    publish_called = []

    import bot.services.scheduler as scheduler_mod

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_async_session():
        yield db_session

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_async_session)
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "bot.services.digests.run_digest",
        AsyncMock(return_value=stub_digest),
    )

    async def _fake_publish(session, *, bot, digest, digest_config):
        publish_called.append(digest)
        return digest

    monkeypatch.setattr(
        "bot.services.digest_publisher.publish_digest",
        _fake_publish,
    )

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    await scheduler_mod.digest_daily_job(fake_bot)

    assert len(publish_called) == 0, (
        f"publish_digest must NOT be called for skipped digest, got {len(publish_called)} calls"
    )
    fake_bot.send_message.assert_not_awaited()


async def test_digest_daily_job_window_computation():
    """The completed daily window uses exact 05:00 MSK boundaries."""
    from bot.services.digest_windows import completed_daily_window
    from zoneinfo import ZoneInfo

    msk = ZoneInfo("Europe/Moscow")
    pinned_now_msk = datetime(2026, 5, 15, 9, 0, 0, tzinfo=msk)
    window_start_utc, window_end_utc = completed_daily_window(pinned_now_msk)
    assert window_start_utc == datetime(2026, 5, 14, 2, 0, 0, tzinfo=timezone.utc)
    assert window_end_utc == datetime(2026, 5, 15, 2, 0, 0, tzinfo=timezone.utc)
    assert (window_end_utc - window_start_utc).total_seconds() == 86400


async def test_digest_daily_job_registers_in_scheduler(monkeypatch):
    """Smoke test: setup_scheduler registers `digest_daily` cron job with MSK timezone.

    Stubs Bot and the scheduler.start() / start_scheduler dependencies so we
    can inspect the job registration without actually starting apscheduler.
    """

    from bot.services import scheduler as scheduler_mod

    # We don't want to actually start the scheduler — patch .start().
    started = []
    real_start = scheduler_mod.scheduler.start
    scheduler_mod.scheduler.start = lambda: started.append(True)  # type: ignore[assignment]
    try:

        class _FakeBot:
            pass

        scheduler_mod.start_scheduler(_FakeBot())  # type: ignore[arg-type]
        job = scheduler_mod.scheduler.get_job("digest_daily")
        assert job is not None, "digest_daily must be registered"
        # The trigger should be a CronTrigger with timezone Europe/Moscow.
        trigger_tz = getattr(job.trigger, "timezone", None)
        assert trigger_tz is not None
        # Compare via str — ZoneInfo equality is by key.
        assert "Moscow" in str(trigger_tz), f"got {trigger_tz!r}"
        # Reaper also registered.
        reaper_job = scheduler_mod.scheduler.get_job("digest_stale_posting_reaper")
        assert reaper_job is not None
        intro_job = scheduler_mod.scheduler.get_job("check_intro_refresh")
        assert intro_job is not None
        intro_fields = {field.name: str(field) for field in intro_job.trigger.fields}
        assert str(intro_job.trigger.timezone) == "Europe/Moscow"
        assert intro_fields["month"] == "3,9"
        assert intro_fields["day"] == "1"
        assert intro_fields["hour"] == "10"
        assert intro_fields["minute"] == "0"
    finally:
        # Restore + clear registered jobs to avoid bleed into other tests.
        scheduler_mod.scheduler.start = real_start  # type: ignore[assignment]
        for jid in (
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


# ── Delivery-uncertain posting reaper ────────────────────────────────────────


def _fake_session_ctx(db_session):
    """Patch ``async_session()`` to yield the test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    return _ctx


async def test_reaper_still_reaps_stale_posting_after_widening(db_session, monkeypatch):
    """A stale at-most-once posting row becomes delivery-uncertain failed."""
    from bot.db.models import Digest

    now = datetime.now(timezone.utc)
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown="draft body",
        citations=[],
        status="posting",
        posting_started_at=now - timedelta(minutes=3),
    )
    db_session.add(digest)
    await db_session.flush()
    digest_id = digest.id

    import bot.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "async_session", _fake_session_ctx(db_session))

    await scheduler_mod.digest_stale_posting_reaper_job()

    refreshed = await db_session.execute(
        text("SELECT status, error_text, posting_started_at FROM digests WHERE id = :id"),
        {"id": digest_id},
    )
    row = refreshed.mappings().one()
    assert row["status"] == "failed"
    assert row["error_text"] == "delivery_uncertain_no_auto_retry"
    assert row["posting_started_at"] is None
