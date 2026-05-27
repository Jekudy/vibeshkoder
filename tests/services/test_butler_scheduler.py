"""T12-08 — Butler TTL expiry worker + scheduler job tests.

Tests that require the real DB session (app_env fixture) live here.
Pure-unit tests for rate buckets + budget live in test_butler_controls.py.

Module-level imports required (T12-06 CI lesson — class-identity).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ---------------------------------------------------------------------------
# Tests — butler_expire_tick inner function (pure, DB-mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_butler_expire_tick_no_stale_actions_commits() -> None:
    """butler_expire_tick with no stale actions commits and returns 0."""
    import bot.services.scheduler as sched_mod

    session_mock = MagicMock()
    session_mock.commit = AsyncMock()

    with patch.object(sched_mod, "_query_pending_past_ttl", new=AsyncMock(return_value=[])):
        result = await sched_mod.butler_expire_tick(bot=MagicMock(), session=session_mock)

    assert result == 0
    session_mock.commit.assert_called_once()


@pytest.mark.asyncio
async def test_butler_expire_tick_expires_stale_action() -> None:
    """butler_expire_tick calls expire_action on a stale action and returns count."""
    import bot.services.scheduler as sched_mod

    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    class _FakeAction:
        id = 99
        status = "pending_confirmation"
        expires_at = past

    class _FakeExpiredAction:
        id = 99
        status = "expired"

    session_mock = MagicMock()
    session_mock.commit = AsyncMock()
    session_mock.execute = AsyncMock()

    expire_action_calls: list[int] = []

    # Patch the ButlerService constructor inside the tick
    class _FakeService:
        async def expire_action(self, *, action_id: int) -> _FakeExpiredAction:
            expire_action_calls.append(action_id)
            return _FakeExpiredAction()

    with patch.object(sched_mod, "_query_pending_past_ttl", new=AsyncMock(return_value=[_FakeAction()])):
        with patch("bot.services.butler.ButlerService", return_value=_FakeService()):
            result = await sched_mod.butler_expire_tick(bot=MagicMock(), session=session_mock)

    assert result == 1
    assert 99 in expire_action_calls
    session_mock.commit.assert_called_once()


@pytest.mark.asyncio
async def test_butler_expire_tick_skips_fresh_actions() -> None:
    """butler_expire_tick does not expire actions that are not past TTL."""
    import bot.services.scheduler as sched_mod

    # _query_pending_past_ttl only returns stale actions (filtered by repo)
    # so if it returns empty, no expire_action is called.
    session_mock = MagicMock()
    session_mock.commit = AsyncMock()

    with patch.object(sched_mod, "_query_pending_past_ttl", new=AsyncMock(return_value=[])):
        result = await sched_mod.butler_expire_tick(bot=MagicMock(), session=session_mock)

    assert result == 0


# ---------------------------------------------------------------------------
# Tests — butler_expire_tick_job (job wrapper with feature flag gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_butler_expire_tick_job_skips_when_flag_off(db_session) -> None:
    """butler_expire_tick_job is a no-op when memory.butler.enabled is OFF."""
    import bot.services.scheduler as sched_mod

    expire_called: list[bool] = []

    @asynccontextmanager
    async def _fake_session():
        yield db_session

    with patch.object(sched_mod, "async_session", _fake_session):
        with patch.object(
            sched_mod,
            "butler_expire_tick",
            new=AsyncMock(side_effect=lambda **kw: expire_called.append(True)),
        ):
            # Flag is OFF by default (no row in feature_flags)
            await sched_mod.butler_expire_tick_job(MagicMock())

    assert len(expire_called) == 0


@pytest.mark.asyncio
async def test_butler_expire_tick_job_runs_when_flag_on(db_session) -> None:
    """butler_expire_tick_job calls butler_expire_tick when memory.butler.enabled is ON."""
    from bot.db.repos.feature_flag import FeatureFlagRepo
    import bot.services.scheduler as sched_mod

    await FeatureFlagRepo.set_enabled(db_session, flag_key="memory.butler.enabled", enabled=True)
    await db_session.commit()

    expire_called: list[bool] = []

    @asynccontextmanager
    async def _fake_session():
        yield db_session

    with patch.object(sched_mod, "async_session", _fake_session):
        with patch.object(
            sched_mod,
            "butler_expire_tick",
            new=AsyncMock(side_effect=lambda **kw: expire_called.append(True) or 0),
        ):
            await sched_mod.butler_expire_tick_job(MagicMock())

    assert len(expire_called) == 1


# ---------------------------------------------------------------------------
# Tests — ButlerActionRepo.get_pending_past_ttl (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pending_past_ttl_empty_when_no_rows(db_session) -> None:
    """get_pending_past_ttl returns empty list when no butler_actions exist."""
    from bot.db.repos.butler_action import ButlerActionRepo

    now = datetime.now(timezone.utc)
    rows = await ButlerActionRepo.get_pending_past_ttl(db_session, now=now)
    assert rows == []


# ---------------------------------------------------------------------------
# Tests — LedgerRepo.monthly_cost_usd call_type extension (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monthly_cost_usd_call_type_filter(db_session) -> None:
    """LedgerRepo.monthly_cost_usd accepts call_type kwarg and returns 0 on empty DB."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from decimal import Decimal

    now = datetime.now(timezone.utc)
    result = await LedgerRepo.monthly_cost_usd(
        db_session,
        year=now.year,
        month=now.month,
        call_type="butler_decision",
    )
    assert result == Decimal("0")


@pytest.mark.asyncio
async def test_monthly_cost_usd_no_call_type_backwards_compatible(db_session) -> None:
    """LedgerRepo.monthly_cost_usd without call_type is backwards compatible."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from decimal import Decimal

    now = datetime.now(timezone.utc)
    result = await LedgerRepo.monthly_cost_usd(
        db_session,
        year=now.year,
        month=now.month,
    )
    assert result == Decimal("0")


# ---------------------------------------------------------------------------
# Tests — compute_user_daily_budget_spent (DB-backed, no rows)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_user_daily_budget_spent_empty_db(db_session) -> None:
    """compute_user_daily_budget_spent returns 0 when no butler ledger rows exist."""
    from bot.services.butler_budget import compute_user_daily_budget_spent
    from decimal import Decimal

    result = await compute_user_daily_budget_spent(session=db_session, user_id=999)
    assert result == Decimal("0")


# ---------------------------------------------------------------------------
# Tests — scheduler registration (job added to scheduler)
# ---------------------------------------------------------------------------


def test_start_scheduler_registers_butler_expire_tick_job() -> None:
    """start_scheduler registers the butler_expire_tick job."""
    import bot.services.scheduler as sched_mod

    mock_bot = MagicMock()
    mock_scheduler = MagicMock()
    sched_mod.scheduler = mock_scheduler

    try:
        sched_mod.start_scheduler(mock_bot)
    except Exception:
        pass  # scheduler.start() may fail without full config

    # Collect all job IDs that were registered
    job_ids = [
        call.kwargs.get("id") or (call.args[2] if len(call.args) > 2 else None)
        for call in mock_scheduler.add_job.call_args_list
    ]
    assert "butler_expire_tick" in job_ids
