"""Integration tests for bot/db/repos/graph_projection_run.py (W0-A / Phase 10).

Uses the temp_database_url pattern (like test_fts_schema.py): creates an isolated
temporary Postgres database, runs alembic upgrade head (which includes migration 060),
tests the repo CRUD, then drops the database.

Tests are skipped (not failed) if postgres is unreachable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine.url import URL, make_url

from tests.conftest import DEFAULT_LOCAL_POSTGRES_URL

pytestmark = pytest.mark.usefixtures("app_env")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ─── Temp DB helpers (same pattern as test_fts_schema.py) ─────────────────────


def _base_test_url() -> URL:
    raw_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )
    return make_url(raw_url)


def _asyncpg_kwargs(url: URL, *, database: str | None = None) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database or url.database,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _create_database(admin_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await conn.execute(f"CREATE DATABASE {_quote_identifier(database_name)}")
    finally:
        await conn.close()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database_name,
        )
        await conn.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
    finally:
        await conn.close()


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=True,
    )


@pytest_asyncio.fixture()
async def graph_run_db_url() -> AsyncIterator[str]:
    """Temp DB with alembic upgrade head (includes migration 060)."""
    base_url = _base_test_url()
    database_name = f"shkoder_graph_run_{uuid.uuid4().hex[:12]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    db_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    try:
        _run_alembic(db_url, "upgrade", "head")
    except subprocess.CalledProcessError as exc:
        await _drop_database(base_url, database_name)
        pytest.skip(f"alembic upgrade head failed: {exc.stderr}")

    try:
        yield db_url
    finally:
        await _drop_database(base_url, database_name)


@pytest_asyncio.fixture()
async def graph_session(graph_run_db_url: str) -> AsyncIterator:
    """AsyncSession connected to the migrated temp DB; each test fully isolated."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(graph_run_db_url, echo=False)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            Session = async_sessionmaker(
                bind=conn, class_=AsyncSession, expire_on_commit=False
            )
            async with Session() as session:
                try:
                    yield session
                finally:
                    if outer.is_active:
                        await outer.rollback()
    finally:
        await engine.dispose()


# ─── create_run ───────────────────────────────────────────────────────────────


async def test_create_run_returns_row_with_started_at(graph_session) -> None:
    from bot.db.models import GraphProjectionRun
    from bot.db.repos.graph_projection_run import create_run
    from sqlalchemy import select

    run = await create_run(graph_session, mode="dry_run", started_by="scheduler")

    assert run.id is not None
    assert run.mode == "dry_run"
    assert run.status == "running"
    assert run.started_at is not None
    assert run.started_by == "scheduler"
    assert run.finished_at is None

    # Verify persisted in DB
    fetched = await graph_session.execute(
        select(GraphProjectionRun).where(GraphProjectionRun.id == run.id)
    )
    row = fetched.scalar_one()
    assert row.mode == "dry_run"
    assert row.status == "running"


async def test_create_run_accepts_none_started_by(graph_session) -> None:
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(graph_session, mode="incremental", started_by=None)

    assert run.started_by is None
    assert run.mode == "incremental"


# ─── update_run_stats ─────────────────────────────────────────────────────────


async def test_update_run_stats_deep_merges_jsonb(graph_session) -> None:
    """update_run_stats preserves existing column values when patching a subset."""
    from bot.db.repos.graph_projection_run import create_run, update_run_stats

    run = await create_run(graph_session, mode="incremental", started_by="admin:1")

    # First patch: set initial counts
    await update_run_stats(
        graph_session, run.id, stats_patch={"projected_node_count": 5, "llm_prompt_tokens": 100}
    )
    await graph_session.flush()

    # Fetch and verify first patch applied
    await graph_session.refresh(run)
    assert run.projected_node_count == 5
    assert run.llm_prompt_tokens == 100

    # Second patch: update nodes without touching tokens
    await update_run_stats(
        graph_session, run.id,
        stats_patch={"projected_node_count": 10, "projected_edge_count": 3}
    )
    await graph_session.flush()

    await graph_session.refresh(run)
    assert run.projected_node_count == 10
    assert run.projected_edge_count == 3
    # Deep-merge semantics: llm_prompt_tokens not in second patch → stays at 100
    assert run.llm_prompt_tokens == 100


# ─── finalize_run ─────────────────────────────────────────────────────────────


async def test_finalize_run_sets_status_and_completed_at(graph_session) -> None:
    from bot.db.repos.graph_projection_run import create_run, finalize_run

    run = await create_run(graph_session, mode="full_rebuild", started_by=None)
    assert run.finished_at is None
    assert run.status == "running"

    await finalize_run(graph_session, run.id, status="completed", cost_usd=Decimal("0.042000"))
    await graph_session.flush()

    await graph_session.refresh(run)
    assert run.status == "completed"
    assert run.finished_at is not None
    assert run.actual_cost_usd == Decimal("0.042000")
    # finished_at should be close to now
    delta = (datetime.now(tz=timezone.utc) - run.finished_at).total_seconds()
    assert abs(delta) < 60


async def test_finalize_run_without_cost_leaves_cost_at_zero(graph_session) -> None:
    from bot.db.repos.graph_projection_run import create_run, finalize_run

    run = await create_run(graph_session, mode="dry_run", started_by=None)
    await finalize_run(graph_session, run.id, status="dry_run_complete")
    await graph_session.flush()

    await graph_session.refresh(run)
    assert run.status == "dry_run_complete"
    assert run.actual_cost_usd == Decimal("0")


# ─── get_active_run ───────────────────────────────────────────────────────────


async def test_get_active_run_returns_only_running(graph_session) -> None:
    from bot.db.repos.graph_projection_run import create_run, finalize_run, get_active_run

    # No running run initially
    active = await get_active_run(graph_session)
    assert active is None

    # Create a running run
    run = await create_run(graph_session, mode="incremental", started_by=None)
    await graph_session.flush()

    active = await get_active_run(graph_session)
    assert active is not None
    assert active.id == run.id
    assert active.status == "running"

    # Finalize it — should no longer be active
    await finalize_run(graph_session, run.id, status="completed")
    await graph_session.flush()

    active = await get_active_run(graph_session)
    assert active is None


# ─── list_recent_runs ─────────────────────────────────────────────────────────


async def test_list_recent_runs_ordered_desc_by_started_at(graph_session) -> None:
    from bot.db.repos.graph_projection_run import create_run, list_recent_runs

    # Create 3 runs in order
    run1 = await create_run(graph_session, mode="dry_run", started_by=None)
    run2 = await create_run(graph_session, mode="incremental", started_by=None)
    run3 = await create_run(graph_session, mode="full_rebuild", started_by=None)
    await graph_session.flush()

    runs = await list_recent_runs(graph_session, limit=10)

    # Should return at least our 3 runs
    ids_returned = [r.id for r in runs]
    assert run1.id in ids_returned
    assert run2.id in ids_returned
    assert run3.id in ids_returned

    # Most recent first (DESC order by started_at then id)
    our_runs = [r for r in runs if r.id in {run1.id, run2.id, run3.id}]
    our_ids = [r.id for r in our_runs]
    # run3 was created last → higher id → should appear before run2 and run1
    assert our_ids.index(run3.id) < our_ids.index(run2.id)
    assert our_ids.index(run2.id) < our_ids.index(run1.id)


async def test_list_recent_runs_respects_limit(graph_session) -> None:
    from bot.db.repos.graph_projection_run import create_run, list_recent_runs

    for _ in range(5):
        await create_run(graph_session, mode="dry_run", started_by=None)
    await graph_session.flush()

    runs = await list_recent_runs(graph_session, limit=3)
    assert len(runs) <= 3


# ─── HIGH-3: finalize_run state-machine guard ─────────────────────────────────


async def test_finalize_run_rejects_non_terminal_status(graph_session) -> None:
    """finalize_run raises ValueError when called with a non-terminal status."""
    from bot.db.repos.graph_projection_run import create_run, finalize_run

    run = await create_run(graph_session, mode="dry_run", started_by=None)
    await graph_session.flush()

    with pytest.raises(ValueError, match="requires a terminal status"):
        await finalize_run(graph_session, run.id, status="running")  # type: ignore[arg-type]


async def test_finalize_run_idempotent_for_same_terminal(graph_session) -> None:
    """finalize_run is a no-op when called twice with the same terminal status."""
    from bot.db.repos.graph_projection_run import create_run, finalize_run

    run = await create_run(graph_session, mode="incremental", started_by=None)
    await graph_session.flush()

    await finalize_run(graph_session, run.id, status="completed")
    await graph_session.flush()

    # Second call with same status must not raise
    await finalize_run(graph_session, run.id, status="completed")


async def test_finalize_run_rejects_terminal_to_different_terminal(graph_session) -> None:
    """finalize_run raises ValueError when attempting succeeded → failed transition."""
    from bot.db.repos.graph_projection_run import create_run, finalize_run

    run = await create_run(graph_session, mode="full_rebuild", started_by=None)
    await graph_session.flush()

    await finalize_run(graph_session, run.id, status="completed")
    await graph_session.flush()

    with pytest.raises(ValueError, match="already"):
        await finalize_run(graph_session, run.id, status="failed")


# ─── HIGH-4: update_run_stats rejects unknown keys ────────────────────────────


async def test_update_run_stats_rejects_unknown_keys(graph_session) -> None:
    """update_run_stats raises ValueError for unknown column names."""
    from bot.db.repos.graph_projection_run import create_run, update_run_stats

    run = await create_run(graph_session, mode="dry_run", started_by=None)
    await graph_session.flush()

    with pytest.raises(ValueError, match="unknown keys"):
        await update_run_stats(graph_session, run.id, stats_patch={"bogus_col": 1})


async def test_update_run_stats_empty_patch_is_noop(graph_session) -> None:
    """update_run_stats with empty dict does not touch the row."""
    from bot.db.repos.graph_projection_run import create_run, update_run_stats

    run = await create_run(graph_session, mode="dry_run", started_by=None)
    await graph_session.flush()

    # Empty patch must not raise and must not modify the row
    await update_run_stats(graph_session, run.id, stats_patch={})
    await graph_session.refresh(run)
    assert run.projected_node_count == 0  # untouched


# ─── MEDIUM-3: get_active_run deterministic ordering ─────────────────────────


async def test_get_active_run_deterministic_on_tied_started_at(graph_session) -> None:
    """When two running rows share started_at, get_active_run returns the higher id."""
    from sqlalchemy import update as sa_update

    from bot.db.models import GraphProjectionRun
    from bot.db.repos.graph_projection_run import create_run, get_active_run

    run1 = await create_run(graph_session, mode="dry_run", started_by=None)
    run2 = await create_run(graph_session, mode="incremental", started_by=None)
    await graph_session.flush()

    # Force both rows to the same started_at to simulate a tied timestamp
    tied_ts = run1.started_at
    await graph_session.execute(
        sa_update(GraphProjectionRun)
        .where(GraphProjectionRun.id.in_([run1.id, run2.id]))
        .values(started_at=tied_ts)
    )
    await graph_session.flush()

    active = await get_active_run(graph_session)
    assert active is not None
    # Higher id should win on tied started_at (ORDER BY started_at DESC, id DESC)
    assert active.id == max(run1.id, run2.id)
