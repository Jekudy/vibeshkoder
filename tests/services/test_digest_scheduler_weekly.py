"""Tests for T8-05 — Phase 8 weekly-digest scheduler jobs.

Covers:
- ``digest_weekly_job``: flag-OFF strict no-op
- ``digest_weekly_job``: seven daily windows (last Mon 05:00 MSK → this Mon
  05:00 MSK, stored as UTC)
- ``digest_weekly_job``: draft → automatic publication, without review
- ``digest_weekly_job``: idempotency-return non-draft statuses do NOT publish
- ``digest_weekly_job``: ``failed``/``cost_exceeded`` paths fire admin DM
- ``digest_weekly_job``: registered as Mon 09:00 MSK cron

PHASE8_PLAN.md §13 AC1.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    """window_start = last Mon 05:00 MSK; window_end = this Mon 05:00 MSK.

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

    expected_start = datetime(2026, 5, 11, 2, 0, 0, tzinfo=timezone.utc)
    expected_end = datetime(2026, 5, 18, 2, 0, 0, tzinfo=timezone.utc)
    assert captured["type"] == "weekly"
    assert captured["window_start"] == expected_start, captured
    assert captured["window_end"] == expected_end, captured
    # 7-day span:
    assert (captured["window_end"] - captured["window_start"]).total_seconds() == 7 * 86400


# ── digest_weekly_job: draft → automatic publish ──────────────────────────


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
    ["posting", "posted"],
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
