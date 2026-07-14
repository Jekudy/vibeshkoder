from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError


pytestmark = pytest.mark.usefixtures("app_env")


def _session_context(session):
    @asynccontextmanager
    async def context():
        yield session

    return context


async def test_photo_description_worker_processes_one_row_and_commits(monkeypatch):
    from bot.services import scheduler as scheduler_mod

    session = AsyncMock()
    bot = object()
    process = AsyncMock(return_value=object())
    monkeypatch.setattr(scheduler_mod, "async_session", _session_context(session))
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(scheduler_mod, "process_next_pending_photo", process)

    await scheduler_mod.photo_description_worker_job(bot)

    process.assert_awaited_once_with(session, bot=bot)
    session.commit.assert_awaited_once()


async def test_photo_description_worker_is_strict_noop_when_flag_off(monkeypatch):
    from bot.services import scheduler as scheduler_mod

    session = AsyncMock()
    process = AsyncMock()
    monkeypatch.setattr(scheduler_mod, "async_session", _session_context(session))
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(scheduler_mod, "process_next_pending_photo", process)

    await scheduler_mod.photo_description_worker_job(object())

    process.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_photo_description_worker_database_log_excludes_exception_payload(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from bot.services import scheduler as scheduler_mod

    secret = "api_key=vision-scheduler-secret-sentinel"
    session = AsyncMock()
    process = AsyncMock(
        side_effect=SQLAlchemyError(f"UPDATE params include {secret} image-payload-sentinel")
    )
    monkeypatch.setattr(scheduler_mod, "async_session", _session_context(session))
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(scheduler_mod, "process_next_pending_photo", process)
    caplog.set_level(logging.ERROR, logger=scheduler_mod.__name__)

    await scheduler_mod.photo_description_worker_job(object())

    records = [record for record in caplog.records if record.name == scheduler_mod.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == "photo_description_worker_database_failed"
    assert record.error_class == "SQLAlchemyError"
    assert record.exc_info is None
    rendered = repr(record.__dict__)
    assert secret not in rendered
    assert "image-payload-sentinel" not in rendered
    assert "UPDATE params" not in rendered
    session.commit.assert_not_awaited()


def test_start_scheduler_registers_photo_description_worker(monkeypatch):
    from bot.services import scheduler as scheduler_mod

    calls: list[tuple[object, str, dict[str, object]]] = []
    monkeypatch.setattr(
        scheduler_mod.scheduler,
        "add_job",
        lambda func, trigger, **kwargs: calls.append((func, trigger, kwargs)),
    )
    monkeypatch.setattr(scheduler_mod.scheduler, "start", lambda: None)

    scheduler_mod.start_scheduler(object())

    photo_jobs = [call for call in calls if call[2].get("id") == "photo_description_worker"]
    assert len(photo_jobs) == 1
    func, trigger, kwargs = photo_jobs[0]
    assert func is scheduler_mod.photo_description_worker_job
    assert trigger == "interval"
    assert kwargs["seconds"] == 30
    assert kwargs["max_instances"] == 1
