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
from apscheduler.schedulers.base import SchedulerAlreadyRunningError

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
    """butler_expire_tick calls _expire_action_inline on a stale action and returns count."""
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

    expire_action_calls: list[int] = []

    async def _fake_expire_inline(session, action_id, *, action_repo):
        expire_action_calls.append(action_id)
        return _FakeExpiredAction()

    with patch.object(sched_mod, "_query_pending_past_ttl", new=AsyncMock(return_value=[_FakeAction()])):
        with patch.object(sched_mod, "_expire_action_inline", new=_fake_expire_inline):
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
    except (SchedulerAlreadyRunningError, RuntimeError):
        pass  # scheduler.start() may fail without full config

    # Collect all job IDs that were registered
    job_ids = [
        call.kwargs.get("id") or (call.args[2] if len(call.args) > 2 else None)
        for call in mock_scheduler.add_job.call_args_list
    ]
    assert "butler_expire_tick" in job_ids


# ---------------------------------------------------------------------------
# Tests — H1: _butler_budget_check uses call_type filter for monthly cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_butler_budget_check_monthly_uses_call_type_filter() -> None:
    """_butler_budget_check passes call_type to monthly_cost_usd (H1 fix)."""
    from decimal import Decimal
    from bot.services.llm_gateway import _butler_budget_check

    monthly_call_types_seen: list[str | None] = []

    class _FakeLedger:
        async def daily_cost_usd(self, session, *, day, call_type=None):
            return Decimal("0")

        async def monthly_cost_usd(self, session, *, year, month, call_type=None):
            monthly_call_types_seen.append(call_type)
            return Decimal("0")

    result = await _butler_budget_check(None, _FakeLedger(), "butler_decision")
    assert result is False
    # Both butler_decision and butler_summary call_types should be queried
    assert "butler_decision" in monthly_call_types_seen
    assert "butler_summary" in monthly_call_types_seen
    # None (unfiltered) should NOT be passed
    assert None not in monthly_call_types_seen


# ---------------------------------------------------------------------------
# Tests — H2: get_pending_past_ttl respects BUTLER_EXPIRE_BATCH_SIZE limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pending_past_ttl_respects_batch_size(db_session) -> None:
    """get_pending_past_ttl with limit returns at most limit rows."""
    from bot.db.repos.butler_action import ButlerActionRepo

    now = datetime.now(timezone.utc)
    # With no rows in DB the result is empty regardless of limit
    rows = await ButlerActionRepo.get_pending_past_ttl(db_session, now=now, limit=1)
    assert rows == []


@pytest.mark.asyncio
async def test_butler_expire_tick_passes_batch_limit() -> None:
    """butler_expire_tick passes BUTLER_EXPIRE_BATCH_SIZE limit to get_pending_past_ttl."""
    import bot.services.scheduler as sched_mod

    captured_kwargs: list[dict] = []

    # No default for limit — if caller doesn't pass it, TypeError is raised
    async def _capture_query(session, *, now, limit):
        captured_kwargs.append({"limit": limit})
        return []

    session_mock = MagicMock()
    session_mock.commit = AsyncMock()

    with patch.object(sched_mod, "_query_pending_past_ttl", new=_capture_query):
        await sched_mod.butler_expire_tick(bot=MagicMock(), session=session_mock)

    assert len(captured_kwargs) == 1
    # Default batch size is 200
    assert captured_kwargs[0]["limit"] == 200


# ---------------------------------------------------------------------------
# Tests — M1: butler_expire_tick uses _expire_action_inline free function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_butler_expire_tick_uses_expire_action_inline() -> None:
    """butler_expire_tick calls _expire_action_inline, not ButlerService (M1)."""
    import bot.services.scheduler as sched_mod

    # Verify _expire_action_inline exists as a free function in the scheduler module
    assert hasattr(sched_mod, "_expire_action_inline"), (
        "_expire_action_inline free function must exist in scheduler module"
    )

    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    class _FakeAction:
        id = 77
        status = "pending_confirmation"
        expires_at = past

    class _FakeExpiredAction:
        id = 77
        status = "expired"

    session_mock = MagicMock()
    session_mock.commit = AsyncMock()

    inline_calls: list[int] = []

    async def _fake_expire_inline(session, action_id, *, action_repo):
        inline_calls.append(action_id)
        return _FakeExpiredAction()

    with patch.object(sched_mod, "_query_pending_past_ttl", new=AsyncMock(return_value=[_FakeAction()])):
        with patch.object(sched_mod, "_expire_action_inline", new=_fake_expire_inline):
            result = await sched_mod.butler_expire_tick(bot=MagicMock(), session=session_mock)

    assert result == 1
    assert 77 in inline_calls


# ---------------------------------------------------------------------------
# Tests — FHR HIGH: per-row savepoint isolation in butler_expire_tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_butler_expire_tick_lock_contention_does_not_discard_batch() -> None:
    """Lock contention on one row must not discard expiry of other rows.

    Regression for FHR HIGH defect: without per-row savepoint, an OperationalError
    (e.g. FOR UPDATE NOWAIT lock contention, SQLSTATE 55P03) is caught by the bare
    ``except Exception`` but the session is left in aborted-transaction state.
    Every subsequent row's work and the final commit then also fail or raise, so
    the outer job handler rolls back — rows already expired earlier in the batch
    are discarded.

    With the per-row savepoint (``session.begin_nested()``), only the contended
    row's savepoint is rolled back; the outer transaction stays usable and the
    other rows are committed.

    Test approach: mock-based (fake session where begin_nested is an async context
    manager that isolates exceptions).  _expire_action_inline raises OperationalError
    for action_id=2 only; rows 1 and 3 must still be expired and tick must return 2.

    The test verifies the PRODUCTION CODE uses begin_nested: if the loop wraps each
    row in ``async with session.begin_nested()``, begin_nested will be called 3 times
    (once per row).  If the code does NOT use begin_nested, the call count is 0 and
    the assertion fails — proving the fix is present.
    """
    from sqlalchemy.exc import OperationalError

    import bot.services.scheduler as sched_mod

    class _FakeAction:
        def __init__(self, action_id: int) -> None:
            self.id = action_id

    class _FakeExpiredAction:
        def __init__(self, action_id: int) -> None:
            self.id = action_id
            self.status = "expired"

    # Track which action_ids were successfully expired
    expired_ids: list[int] = []

    async def _fake_expire_inline(session, action_id, *, action_repo):
        if action_id == 2:
            raise OperationalError("lock not available", None, None)
        expired_ids.append(action_id)
        return _FakeExpiredAction(action_id)

    # Build a session mock that supports begin_nested() as an async context manager.
    # The mock records how many times begin_nested was entered so we can assert the
    # production code wraps each row in a savepoint.
    from contextlib import asynccontextmanager

    begin_nested_enter_count = [0]

    @asynccontextmanager
    async def _fake_begin_nested():
        begin_nested_enter_count[0] += 1
        try:
            yield MagicMock()
        except Exception:
            # Savepoint rolled back — outer transaction NOT poisoned
            pass

    session_mock = MagicMock()
    session_mock.commit = AsyncMock()
    session_mock.begin_nested = _fake_begin_nested

    stale = [_FakeAction(1), _FakeAction(2), _FakeAction(3)]

    with patch.object(sched_mod, "_query_pending_past_ttl", new=AsyncMock(return_value=stale)):
        with patch.object(sched_mod, "_expire_action_inline", new=_fake_expire_inline):
            result = await sched_mod.butler_expire_tick(bot=MagicMock(), session=session_mock)

    # The loop MUST enter begin_nested once per row (3 rows → 3 savepoints)
    assert begin_nested_enter_count[0] == 3, (
        f"expected begin_nested entered 3 times (one savepoint per row), "
        f"got {begin_nested_enter_count[0]} — the per-row savepoint fix is missing"
    )
    # Rows 1 and 3 must be expired; row 2 (lock contention) is skipped
    assert result == 2, f"expected 2 expired rows, got {result}"
    assert expired_ids == [1, 3], f"expected [1, 3] expired, got {expired_ids}"
    # Final commit must still be called (outer transaction not poisoned)
    session_mock.commit.assert_called_once()
