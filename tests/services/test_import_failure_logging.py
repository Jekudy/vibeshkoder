"""Secret-safe diagnostics for import failure recovery paths."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from bot import cli

pytestmark = pytest.mark.usefixtures("app_env")


async def test_finalize_failed_apply_logs_classes_without_exception_payloads(
    monkeypatch,
    caplog,
) -> None:
    """All best-effort finalize failures are logged without exception messages."""
    sentinels = {
        "sentinel-secret-primary-rollback",
        "sentinel-secret-partial-stats",
        "sentinel-secret-stats-rollback",
        "sentinel-secret-finalize",
    }
    primary_session = SimpleNamespace(
        rollback=AsyncMock(side_effect=SQLAlchemyError("sentinel-secret-primary-rollback"))
    )
    fresh_session = SimpleNamespace(
        rollback=AsyncMock(side_effect=SQLAlchemyError("sentinel-secret-stats-rollback")),
        commit=AsyncMock(),
    )

    class _SessionContext:
        async def __aenter__(self):
            return fresh_session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    engine_module = SimpleNamespace(async_session=lambda: _SessionContext())
    original_exc = RuntimeError("sentinel-secret-original")
    original_exc.import_apply_report = SimpleNamespace()
    monkeypatch.setattr(
        cli,
        "_save_apply_final_stats",
        AsyncMock(side_effect=SQLAlchemyError("sentinel-secret-partial-stats")),
    )
    finalize_run = AsyncMock(side_effect=SQLAlchemyError("sentinel-secret-finalize"))

    with caplog.at_level(logging.WARNING, logger="bot.cli"):
        await cli._finalize_failed_apply(
            engine_module,
            session=primary_session,
            finalize_run=finalize_run,
            ingestion_run_id=9002,
            original_exc=original_exc,
        )

    for sentinel in sentinels | {"sentinel-secret-original"}:
        assert sentinel not in caplog.text
    records = [
        record
        for record in caplog.records
        if getattr(record, "error_taxonomy", "").startswith("import_apply_")
    ]
    assert {record.error_taxonomy for record in records} == {
        "import_apply_primary_rollback_failed",
        "import_apply_partial_stats_persist_failed",
        "import_apply_partial_stats_rollback_failed",
        "import_apply_finalize_failed",
    }
    assert all(record.error_class == "SQLAlchemyError" for record in records)
    assert all(record.exc_info is None for record in records)
