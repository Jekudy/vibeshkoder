from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.conftest import import_module


pytestmark = pytest.mark.usefixtures("app_env")


def _session_context(session):
    @asynccontextmanager
    async def context():
        yield session

    return context


async def test_semantic_index_tick_is_strict_noop_when_flag_off(monkeypatch) -> None:
    scheduler = import_module("bot.services.scheduler")
    feature_flags = import_module("bot.db.repos.feature_flag")
    semantic_index = import_module("bot.services.semantic_index")
    session = AsyncMock()
    backfill = AsyncMock()

    monkeypatch.setattr(scheduler, "async_session", _session_context(session))
    monkeypatch.setattr(feature_flags.FeatureFlagRepo, "get", AsyncMock(return_value=False))
    monkeypatch.setattr(semantic_index, "backfill_semantic_index", backfill)

    await scheduler.run_semantic_index_tick()

    backfill.assert_not_awaited()


async def test_semantic_index_tick_runs_idempotent_reconciliation(monkeypatch) -> None:
    scheduler = import_module("bot.services.scheduler")
    feature_flags = import_module("bot.db.repos.feature_flag")
    gateway = import_module("bot.services.llm_gateway")
    semantic_index = import_module("bot.services.semantic_index")
    session = AsyncMock()
    config = SimpleNamespace(model="text-embedding-3-small", dimensions=1536)
    report = SimpleNamespace(run_id=9, eligible=7, indexed=2, skipped=5, failed=0)
    backfill = AsyncMock(return_value=report)

    monkeypatch.setattr(scheduler, "async_session", _session_context(session))
    monkeypatch.setattr(feature_flags.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(gateway, "load_embedding_gateway_config", Mock(return_value=config))
    monkeypatch.setattr(semantic_index, "backfill_semantic_index", backfill)

    await scheduler.run_semantic_index_tick()

    backfill.assert_awaited_once_with(
        session,
        config=config,
        chat_id=-1001234567890,
    )


async def test_semantic_index_tick_reports_unresolved_claim(monkeypatch, caplog) -> None:
    scheduler = import_module("bot.services.scheduler")
    feature_flags = import_module("bot.db.repos.feature_flag")
    gateway = import_module("bot.services.llm_gateway")
    semantic_index = import_module("bot.services.semantic_index")
    session = AsyncMock()
    backfill = AsyncMock(
        side_effect=semantic_index.EmbeddingClaimUnresolved(unit_id=404, status="reserved")
    )

    monkeypatch.setattr(scheduler, "async_session", _session_context(session))
    monkeypatch.setattr(feature_flags.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    monkeypatch.setattr(
        gateway,
        "load_embedding_gateway_config",
        Mock(return_value=SimpleNamespace(model="text-embedding-3-small", dimensions=1536)),
    )
    monkeypatch.setattr(semantic_index, "backfill_semantic_index", backfill)

    await scheduler.run_semantic_index_tick()

    session.rollback.assert_awaited_once()
    assert "semantic_index_tick_failed" in caplog.text


def test_start_scheduler_registers_semantic_index_tick(monkeypatch) -> None:
    scheduler = import_module("bot.services.scheduler")
    jobs: list[tuple[object, str | None, dict[str, object]]] = []

    monkeypatch.setattr(
        scheduler.scheduler,
        "add_job",
        lambda function, trigger, **kwargs: jobs.append(
            (function, kwargs.get("id"), {"trigger": trigger, **kwargs})
        ),
    )
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)

    scheduler.start_scheduler(object())

    matches = [job for job in jobs if job[1] == "semantic_index_tick"]
    assert len(matches) == 1
    function, _, kwargs = matches[0]
    assert function is scheduler.run_semantic_index_tick
    assert kwargs["trigger"] == "interval"
    assert kwargs["minutes"] == 15
    assert kwargs["max_instances"] == 1
