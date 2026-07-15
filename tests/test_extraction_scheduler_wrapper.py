"""T6-03 — apscheduler wrapper for the Phase 6 extraction scheduler tick.

T6-03 design §3 + §4: a thin ``run_extraction_scheduler_tick`` wrapper in
``bot/services/scheduler.py`` opens a fresh session, builds the live gateway
locally, calls ``extraction_scheduler_tick`` from ``bot.services.extractor``,
and commits — or logs + ignores on exception so the scheduler keeps running.

Coverage:

* Wrapper exists and is callable.
* Wrapper builds ``LiveExtractCandidatesGateway`` via local DI.
* Wrapper handles exceptions without raising (scheduler isolation).
* ``start_scheduler`` registers the wrapper as an apscheduler job.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_extraction_scheduler_tick_wrapper_exists() -> None:
    """The wrapper coroutine must be importable from bot.services.scheduler."""
    from bot.services import scheduler

    assert hasattr(scheduler, "run_extraction_scheduler_tick"), (
        "T6-03: ``run_extraction_scheduler_tick`` MUST be defined in bot/services/scheduler.py"
    )
    fn = scheduler.run_extraction_scheduler_tick
    assert inspect.iscoroutinefunction(fn), (
        "T6-03: ``run_extraction_scheduler_tick`` must be ``async def``"
    )


@pytest.mark.asyncio
async def test_run_extraction_scheduler_tick_constructs_live_gateway(monkeypatch) -> None:
    """The wrapper must build LiveExtractCandidatesGateway and pass it to
    ``extraction_scheduler_tick``.
    """
    from bot.services import scheduler as scheduler_module
    from bot.services.extractor import SchedulerTickResult
    from bot.services.llm_gateway import LiveExtractCandidatesGateway

    captured: dict = {}

    class _FakeSession:
        async def commit(self) -> None: ...
        async def rollback(self) -> None: ...

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(scheduler_module, "async_session", lambda: _FakeSessionCtx())
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(side_effect=[True, False]),
    )

    async def fake_tick(session, *, gateway, **kwargs):
        captured["gateway"] = gateway
        captured["source_chat_id"] = kwargs["source_chat_id"]
        return SchedulerTickResult(skipped=True, reason="flag_disabled")

    monkeypatch.setattr(scheduler_module, "extraction_scheduler_tick", fake_tick)

    await scheduler_module.run_extraction_scheduler_tick()

    assert "gateway" in captured
    assert isinstance(captured["gateway"], LiveExtractCandidatesGateway)
    assert captured["source_chat_id"] == scheduler_module.settings.COMMUNITY_CHAT_ID


@pytest.mark.asyncio
async def test_run_extraction_scheduler_tick_swallows_exceptions(monkeypatch) -> None:
    """Tick crashes must NOT propagate — scheduler keeps running."""
    from bot.services import scheduler as scheduler_module

    class _FakeSession:
        async def commit(self) -> None: ...
        async def rollback(self) -> None: ...

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(scheduler_module, "async_session", lambda: _FakeSessionCtx())
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(side_effect=[True, False]),
    )

    async def fake_tick(session, *, gateway, **kwargs):
        raise RuntimeError("simulated tick crash")

    monkeypatch.setattr(scheduler_module, "extraction_scheduler_tick", fake_tick)

    # Should not raise.
    await scheduler_module.run_extraction_scheduler_tick()


@pytest.mark.asyncio
async def test_promotion_failure_cannot_rollback_completed_extraction(monkeypatch) -> None:
    from bot.services import scheduler as scheduler_module
    from bot.services.extractor import ExtractionResult, SchedulerTickResult

    sessions = [AsyncMock(), AsyncMock()]
    context_index = 0

    class _Context:
        async def __aenter__(self):
            nonlocal context_index
            session = sessions[context_index]
            context_index += 1
            return session

        async def __aexit__(self, *args):
            return None

    run_id = uuid.uuid4()
    tick_result = SchedulerTickResult(
        skipped=False,
        extraction_result=ExtractionResult(
            extraction_run_id=run_id,
            run_status="completed",
            candidate_count=1,
            llm_usage_ledger_id=77,
        ),
    )
    monkeypatch.setattr(scheduler_module, "async_session", lambda: _Context())
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(side_effect=[True, True]),
    )
    monkeypatch.setattr(scheduler_module, "_load_automation_actor_user_id", lambda: 42)
    monkeypatch.setattr(scheduler_module, "_require_automation_actor", AsyncMock())
    monkeypatch.setattr(
        scheduler_module,
        "extraction_scheduler_tick",
        AsyncMock(return_value=tick_result),
    )
    promote = AsyncMock(side_effect=RuntimeError("promotion crashed"))
    monkeypatch.setattr(
        "bot.services.candidate_promotion.promote_run_candidates",
        promote,
    )

    await scheduler_module.run_extraction_scheduler_tick()

    sessions[0].commit.assert_awaited_once()
    sessions[0].rollback.assert_not_awaited()
    promote.assert_awaited_once_with(
        sessions[1],
        extraction_run_id=run_id,
        actor_user_id=42,
    )
    sessions[1].rollback.assert_awaited_once()


def test_extraction_scheduler_tick_registered_in_start_scheduler() -> None:
    """``start_scheduler`` must register the extraction tick as an apscheduler job.

    AST-level check (the live scheduler.add_job is a complex apscheduler obj
    not trivial to introspect at runtime).
    """
    src = (REPO_ROOT / "bot" / "services" / "scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_call = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_job":
            continue
        # First positional arg must be ``run_extraction_scheduler_tick``.
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id == "run_extraction_scheduler_tick":
            found_call = True
            break
    assert found_call, (
        "T6-03: ``scheduler.add_job(run_extraction_scheduler_tick, ...)`` "
        "must appear inside ``start_scheduler`` in bot/services/scheduler.py"
    )
