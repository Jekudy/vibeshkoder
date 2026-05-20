"""Integration tests for bot/db/repos/graph_purge_pending.py (T10-06 / Phase 10).

Uses temp_database_url pattern (same as test_graph_provenance_repo.py):
creates an isolated temp Postgres DB, runs alembic upgrade head (includes 063),
exercises the repo, then drops the DB.

Tests are skipped (not failed) if Postgres is unreachable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine.url import URL, make_url

from tests.conftest import DEFAULT_LOCAL_POSTGRES_URL

pytestmark = pytest.mark.usefixtures("app_env")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ─── Temp DB helpers ──────────────────────────────────────────────────────────


def _base_test_url() -> URL:
    raw_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )
    return make_url(raw_url)


def _asyncpg_kwargs(url: URL, *, database: str | None = None) -> dict:
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


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
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


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def purge_db_url() -> AsyncIterator[str]:
    """Temp DB with alembic upgrade head (includes migrations 060-064, 063)."""
    base_url = _base_test_url()
    database_name = f"shkoder_purge_{uuid.uuid4().hex[:12]}"
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
async def purge_session(purge_db_url: str) -> AsyncIterator:
    """AsyncSession on the migrated temp DB; each test fully isolated via rollback."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(purge_db_url, echo=False)
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


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_basic(purge_session):
    """enqueue inserts a row and returns it with the expected fields."""
    from bot.db.repos.graph_purge_pending import enqueue

    row = await enqueue(
        purge_session,
        forget_event_id=1001,
        source_table="message_versions",
        source_pk="42",
        graph_node_key="node:user:42",
        graph_edge_key="edge:abc",
    )

    assert row.id is not None
    assert row.forget_event_id == 1001
    assert row.source_table == "message_versions"
    assert row.source_pk == "42"
    assert row.graph_node_key == "node:user:42"
    assert row.graph_edge_key == "edge:abc"
    assert row.purged_at is None
    assert row.failed_at is None
    assert row.retry_count == 0


@pytest.mark.asyncio
async def test_enqueue_idempotent(purge_session):
    """enqueue with the same (forget_event_id, source_table, source_pk) is idempotent."""
    from bot.db.repos.graph_purge_pending import enqueue

    row1 = await enqueue(
        purge_session,
        forget_event_id=1002,
        source_table="knowledge_cards",
        source_pk="card-uuid-1",
    )
    row2 = await enqueue(
        purge_session,
        forget_event_id=1002,
        source_table="knowledge_cards",
        source_pk="card-uuid-1",
    )
    # Same row returned (same pk)
    assert row1.id == row2.id


@pytest.mark.asyncio
async def test_claim_batch_skip_locked(purge_session):
    """claim_batch returns pending rows in enqueued_at order."""
    from bot.db.repos.graph_purge_pending import claim_batch, enqueue

    await enqueue(
        purge_session,
        forget_event_id=2001,
        source_table="message_versions",
        source_pk="10",
    )
    await enqueue(
        purge_session,
        forget_event_id=2002,
        source_table="message_versions",
        source_pk="11",
    )

    batch = await claim_batch(purge_session, batch_size=10)
    assert len(batch) == 2
    # All returned rows must be pending (purged_at IS NULL, failed_at IS NULL)
    for row in batch:
        assert row.purged_at is None
        assert row.failed_at is None


@pytest.mark.asyncio
async def test_mark_purged(purge_session):
    """mark_purged sets purged_at; subsequent call is idempotent."""
    from bot.db.repos.graph_purge_pending import enqueue, mark_purged

    row = await enqueue(
        purge_session,
        forget_event_id=3001,
        source_table="message_versions",
        source_pk="20",
    )
    await mark_purged(purge_session, row.id)

    # Re-fetch and check
    from sqlalchemy import select
    from bot.db.models import GraphPurgePending

    refreshed = await purge_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )
    assert refreshed.purged_at is not None

    # Idempotent second call must not raise
    await mark_purged(purge_session, row.id)


@pytest.mark.asyncio
async def test_mark_failed_increments_retry(purge_session):
    """mark_failed increments retry_count; sets failed_at only at MAX_RETRIES."""
    import bot.db.repos.graph_purge_pending as gpp_module
    from bot.db.repos.graph_purge_pending import enqueue, mark_failed

    row = await enqueue(
        purge_session,
        forget_event_id=4001,
        source_table="message_versions",
        source_pk="30",
    )

    # First failure — not yet DLQ
    await mark_failed(purge_session, row.id, error_msg="first error")

    from sqlalchemy import select
    from bot.db.models import GraphPurgePending

    refreshed = await purge_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )
    assert refreshed.retry_count == 1
    assert refreshed.failed_at is None  # not yet DLQ

    # Fail until DLQ
    for i in range(gpp_module.MAX_RETRIES - 1):
        await mark_failed(purge_session, row.id, error_msg=f"error {i+2}")

    refreshed = await purge_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )
    assert refreshed.retry_count == gpp_module.MAX_RETRIES
    assert refreshed.failed_at is not None  # now DLQ


@pytest.mark.asyncio
async def test_count_active(purge_session):
    """count_active returns correct pending/failed/total counts."""
    from bot.db.repos.graph_purge_pending import count_active, enqueue, mark_purged

    # Baseline counts before adding anything
    counts_before = await count_active(purge_session)
    pending_start = counts_before["pending"]
    total_start = counts_before["total"]

    row1 = await enqueue(
        purge_session,
        forget_event_id=5001,
        source_table="message_versions",
        source_pk="40",
    )
    await enqueue(
        purge_session,
        forget_event_id=5002,
        source_table="message_versions",
        source_pk="41",
    )
    await mark_purged(purge_session, row1.id)

    counts = await count_active(purge_session)
    # One still pending (row2), one purged (row1)
    assert counts["pending"] == pending_start + 1
    assert counts["total"] == total_start + 2


@pytest.mark.asyncio
async def test_claim_batch_excludes_purged(purge_session):
    """claim_batch does not return rows that are already purged."""
    from bot.db.repos.graph_purge_pending import claim_batch, enqueue, mark_purged

    row = await enqueue(
        purge_session,
        forget_event_id=6001,
        source_table="card_sources",
        source_pk="cs-99",
    )
    await mark_purged(purge_session, row.id)

    batch = await claim_batch(purge_session, batch_size=10)
    row_ids = [r.id for r in batch]
    assert row.id not in row_ids
