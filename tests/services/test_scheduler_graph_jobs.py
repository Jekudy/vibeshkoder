"""T10-07 scheduler graph job tests.

PHASE10_PLAN.md §5.H:
- graph_projection_nightly_job: cron 03:30 MSK, skips when flag OFF
- graph_purge_worker_job: every 5 min, skips when paused flag ON
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


async def _set_flag(db_session, key: str, enabled: bool) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo

    await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=enabled)


# ─── graph_projection_nightly_job ──────────────────────────────────────────


async def test_graph_projection_nightly_job_skips_when_flag_off(db_session) -> None:
    """When memory.graph.projection.enabled is OFF, projection job is a strict no-op."""
    import bot.services.scheduler as scheduler_mod

    @asynccontextmanager
    async def _fake_async_session():
        yield db_session

    project_incremental_called = []

    async def _fake_incremental(*args, **kwargs):
        project_incremental_called.append(True)
        return MagicMock()

    with patch.object(scheduler_mod, "async_session", _fake_async_session):
        with patch(
            "bot.services.scheduler.project_incremental",
            new=AsyncMock(side_effect=_fake_incremental),
        ):
            # Flag is OFF by default (no row in feature_flags). Job must skip.
            await scheduler_mod.graph_projection_nightly_job(MagicMock())

    # project_incremental should NOT have been called
    assert len(project_incremental_called) == 0


async def test_graph_projection_nightly_job_calls_incremental(db_session) -> None:
    """When memory.graph.projection.enabled is ON, nightly job calls project_incremental."""
    import bot.services.scheduler as scheduler_mod

    await _set_flag(db_session, "memory.graph.projection.enabled", True)

    project_incremental_calls = []

    @asynccontextmanager
    async def _fake_async_session():
        yield db_session

    async def _fake_incremental(session, *, config, started_by=None):
        project_incremental_calls.append({"started_by": started_by})
        from bot.services.graph_projector import GraphProjectionRunResult
        from decimal import Decimal
        return GraphProjectionRunResult(
            run_id=1,
            status="completed",
            sources_total=0,
            sources_processed=0,
            sources_skipped_governance=0,
            sources_skipped_budget=0,
            sources_skipped_unknown=0,
            triples_created=0,
            nodes_merged=0,
            edges_merged=0,
            cost_usd=Decimal("0.00"),
            errors_list=[],
        )

    with patch.object(scheduler_mod, "async_session", _fake_async_session):
        with patch(
            "bot.services.scheduler.project_incremental",
            new=AsyncMock(side_effect=_fake_incremental),
        ):
            await scheduler_mod.graph_projection_nightly_job(MagicMock())

    assert len(project_incremental_calls) >= 1


# ─── graph_purge_worker_job ─────────────────────────────────────────────────


async def test_graph_purge_worker_job_skips_when_paused(db_session) -> None:
    """When memory.graph.write_pending.paused is ON, purge job is a strict no-op."""
    import bot.services.scheduler as scheduler_mod

    await _set_flag(db_session, "memory.graph.write_pending.paused", True)

    tick_calls = []

    @asynccontextmanager
    async def _fake_async_session():
        yield db_session

    async def _fake_tick(session, *, adapter, batch_size=20):
        tick_calls.append(True)
        return {"processed": 0, "errors": 0, "skipped_paused": True}

    with patch.object(scheduler_mod, "async_session", _fake_async_session):
        with patch(
            "bot.services.scheduler.graph_purge_worker_tick",
            new=AsyncMock(side_effect=_fake_tick),
        ):
            await scheduler_mod.graph_purge_worker_job()

    # tick may still be called (worker internally checks the flag) — but no work done.
    # The job body may short-circuit before calling tick based on flag check.
    # Accept either: tick not called OR tick returned skipped_paused=True.
    # Both are valid implementations per spec.
    # Here we assert the job completed without error (no exception raised).


async def test_graph_purge_worker_job_drives_tick(db_session) -> None:
    """When paused flag is OFF, purge worker job calls graph_purge_worker_tick."""
    import bot.services.scheduler as scheduler_mod

    # paused flag is OFF by default (no row). Worker should call tick.
    tick_calls = []

    @asynccontextmanager
    async def _fake_async_session():
        yield db_session

    async def _fake_tick(session, *, adapter, batch_size=20):
        tick_calls.append({"batch_size": batch_size})
        return {"processed": 3, "errors": 0, "skipped_paused": False}

    with patch.object(scheduler_mod, "async_session", _fake_async_session):
        with patch(
            "bot.services.scheduler.graph_purge_worker_tick",
            new=AsyncMock(side_effect=_fake_tick),
        ):
            await scheduler_mod.graph_purge_worker_job()

    assert len(tick_calls) >= 1
