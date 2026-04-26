"""T1-02 acceptance tests — ingestion_runs table + IngestionRunRepo.

Outer-tx isolation via ``db_session``. Tests do NOT call ``session.commit()``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")


# ─── create ────────────────────────────────────────────────────────────────────────────────

async def test_create_inserts_running_run(db_session) -> None:
    from bot.db.models import IngestionRun
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(
        db_session, run_type="live", source_name="bot/__main__.py"
    )

    assert run.id is not None
    assert run.run_type == "live"
    assert run.status == "running"
    assert run.started_at is not None
    assert run.finished_at is None

    rows = await db_session.execute(
        select(IngestionRun).where(IngestionRun.id == run.id)
    )
    assert rows.scalar_one().run_type == "live"


async def test_create_rejects_invalid_run_type(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    with pytest.raises(ValueError, match="unsupported run_type"):
        await IngestionRunRepo.create(db_session, run_type="bogus")


# ─── update_status ─────────────────────────────────────────────────────────────────────────

async def test_update_status_to_completed_sets_finished_at(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(db_session, run_type="import", source_name="export.json")
    assert run.finished_at is None

    updated = await IngestionRunRepo.update_status(
        db_session, run, status="completed", stats_json={"messages": 42}
    )

    assert updated.status == "completed"
    assert updated.stats_json == {"messages": 42}
    assert updated.finished_at is not None
    assert updated.finished_at.tzinfo is not None
    # finished_at should be within a reasonable window of "now"
    assert (datetime.now(tz=timezone.utc) - updated.finished_at).total_seconds() < 60


async def test_update_status_to_failed_records_error(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(db_session, run_type="dry_run", source_name="export.json")
    updated = await IngestionRunRepo.update_status(
        db_session,
        run,
        status="failed",
        error_json={"error_type": "ParseError", "where": "line 42"},
    )

    assert updated.status == "failed"
    assert updated.error_json["error_type"] == "ParseError"
    assert updated.finished_at is not None


async def test_update_status_running_does_not_set_finished_at(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(db_session, run_type="live")
    updated = await IngestionRunRepo.update_status(db_session, run, status="running")

    assert updated.status == "running"
    assert updated.finished_at is None


async def test_update_status_rejects_invalid_status(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(db_session, run_type="live")
    with pytest.raises(ValueError, match="unsupported status"):
        await IngestionRunRepo.update_status(db_session, run, status="bogus")


# ─── get_active_live ───────────────────────────────────────────────────────────────────────

async def test_get_active_live_returns_none_when_no_run(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    assert await IngestionRunRepo.get_active_live(db_session) is None


async def test_get_active_live_returns_none_when_only_run_is_completed(db_session) -> None:
    """The previous bot process exited cleanly and closed its live run. The next startup
    must see no active live and create a new one."""
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(db_session, run_type="live")
    await IngestionRunRepo.update_status(db_session, run, status="completed")

    assert await IngestionRunRepo.get_active_live(db_session) is None


async def test_get_active_live_returns_most_recent_running_live_run(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    older = await IngestionRunRepo.create(db_session, run_type="live")
    await IngestionRunRepo.update_status(db_session, older, status="completed")

    newer = await IngestionRunRepo.create(db_session, run_type="live")

    found = await IngestionRunRepo.get_active_live(db_session)
    assert found is not None
    assert found.id == newer.id


async def test_get_active_live_ignores_non_live_runs(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    await IngestionRunRepo.create(db_session, run_type="import")
    await IngestionRunRepo.create(db_session, run_type="dry_run")

    assert await IngestionRunRepo.get_active_live(db_session) is None


# ─── secret rejection (Codex MEDIUM enforcement) ──────────────────────────────────────────

async def test_create_rejects_secret_shaped_config_keys(db_session) -> None:
    """The table is dumped in admin views; refuse payloads with token / secret / password
    / api_key shaped top-level keys before they reach the DB."""
    from bot.db.repos.ingestion_run import IngestionRunRepo

    for offending in (
        {"bot_token": "x"},
        {"DB_PASSWORD": "x"},
        {"api_key": "x"},
        {"sentry_secret": "x"},
        {"refresh_token": "x"},
    ):
        with pytest.raises(ValueError, match="must not contain secret-shaped keys"):
            await IngestionRunRepo.create(
                db_session, run_type="live", config_json=offending
            )

    # Sanity: harmless keys are accepted.
    run = await IngestionRunRepo.create(
        db_session, run_type="live", config_json={"rolloutPct": 10}
    )
    assert run.config_json == {"rolloutPct": 10}


async def test_update_status_rejects_secret_shaped_stats_keys(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo

    run = await IngestionRunRepo.create(db_session, run_type="live")
    with pytest.raises(ValueError, match="must not contain secret-shaped keys"):
        await IngestionRunRepo.update_status(
            db_session, run, status="completed", stats_json={"auth_token": "x"}
        )


# ─── metadata smoke ────────────────────────────────────────────────────────────────────────

def test_ingestion_run_model_registered(app_env) -> None:
    """Offline smoke: model + columns + check constraints + indexes registered."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert "ingestion_runs" in models.Base.metadata.tables
    table = models.Base.metadata.tables["ingestion_runs"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "run_type",
        "source_name",
        "started_at",
        "finished_at",
        "status",
        "stats_json",
        "config_json",
        "error_json",
    } == cols
    constraint_names = {c.name for c in table.constraints if c.name}
    assert "ck_ingestion_runs_run_type" in constraint_names
    assert "ck_ingestion_runs_status" in constraint_names
