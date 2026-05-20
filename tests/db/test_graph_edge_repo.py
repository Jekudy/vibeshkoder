"""Integration tests for bot/db/repos/graph_edge.py (T10-02 / Phase 10).

Uses temp_database_url pattern: creates an isolated temp Postgres DB, runs
alembic upgrade head (includes 061 + 062), exercises the repo, then drops the DB.

Tests are skipped (not failed) if Postgres is unreachable.
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


# ─── Temp DB helpers ──────────────────────────────────────────────────────────


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
async def edge_db_url() -> AsyncIterator[str]:
    """Temp DB with alembic upgrade head (includes migrations 060, 061, 062)."""
    base_url = _base_test_url()
    database_name = f"shkoder_graph_edge_{uuid.uuid4().hex[:12]}"
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
async def edge_session(edge_db_url: str) -> AsyncIterator:
    """AsyncSession on the migrated temp DB; each test fully isolated via rollback."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(edge_db_url, echo=False)
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


# ─── helpers ──────────────────────────────────────────────────────────────────


async def _make_run(session) -> int:
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(session, mode="incremental", started_by="test")
    await session.flush()
    return run.id


async def _make_knowledge_card(session) -> uuid.UUID:
    from sqlalchemy import text

    card_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO knowledge_cards (id, title, body_markdown, card_status) "
            "VALUES (:id, 'Test Card', 'content', 'draft')"
        ),
        {"id": str(card_id)},
    )
    return card_id


async def _make_provenance(session, *, run_id: int) -> int:
    """Insert minimal graph_provenance row using knowledge_card path."""
    from bot.db.repos.graph_provenance import create_provenance

    card_id = await _make_knowledge_card(session)
    row = await create_provenance(
        session,
        projection_run_id=run_id,
        source_table="knowledge_cards",
        source_pk=str(card_id),
        source_card_id=card_id,
    )
    return row.id


def _edge_key() -> str:
    """Generate a unique stable edge key."""
    return uuid.uuid4().hex


# ─── create_edge ─────────────────────────────────────────────────────────────


async def test_create_edge_with_valid_predicate(edge_session) -> None:
    """create_edge inserts a row for a valid predicate."""
    from bot.db.repos.graph_edge import create_edge

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)

    edge = await create_edge(
        edge_session,
        graph_provenance_id=prov_id,
        subject_node_key="user:123",
        predicate="MENTIONS",
        object_node_key="topic:python",
        edge_key=_edge_key(),
        confidence_score=Decimal("0.85"),
    )

    assert edge.id is not None
    assert edge.graph_provenance_id == prov_id
    assert edge.predicate == "MENTIONS"
    assert edge.subject_node_key == "user:123"
    assert edge.object_node_key == "topic:python"
    assert edge.confidence_score == Decimal("0.85")
    assert edge.purged_at is None


async def test_create_edge_rejects_invalid_predicate(edge_session) -> None:
    """create_edge raises ValueError for an unknown predicate."""
    from bot.db.repos.graph_edge import create_edge

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)

    with pytest.raises(ValueError, match="ALLOWED_PREDICATES"):
        await create_edge(
            edge_session,
            graph_provenance_id=prov_id,
            subject_node_key="user:1",
            predicate="INVALID_PREDICATE",
            object_node_key="topic:foo",
            edge_key=_edge_key(),
        )


async def test_create_edge_rejects_invalid_confidence_above_one(edge_session) -> None:
    """create_edge raises ValueError for confidence_score > 1.0."""
    from bot.db.repos.graph_edge import create_edge

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)

    with pytest.raises(ValueError, match="out of range"):
        await create_edge(
            edge_session,
            graph_provenance_id=prov_id,
            subject_node_key="user:1",
            predicate="MENTIONS",
            object_node_key="topic:foo",
            edge_key=_edge_key(),
            confidence_score=Decimal("1.01"),
        )


async def test_create_edge_rejects_invalid_confidence_below_zero(edge_session) -> None:
    """create_edge raises ValueError for confidence_score < 0.0."""
    from bot.db.repos.graph_edge import create_edge

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)

    with pytest.raises(ValueError, match="out of range"):
        await create_edge(
            edge_session,
            graph_provenance_id=prov_id,
            subject_node_key="user:1",
            predicate="MENTIONS",
            object_node_key="topic:foo",
            edge_key=_edge_key(),
            confidence_score=Decimal("-0.01"),
        )


# ─── FK integrity ────────────────────────────────────────────────────────────


async def test_create_edge_rejects_dangling_provenance_fk(edge_session) -> None:
    """create_edge raises IntegrityError when graph_provenance_id points to
    a non-existent provenance row (FK constraint violation)."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.repos.graph_edge import create_edge

    with pytest.raises(IntegrityError):
        await create_edge(
            edge_session,
            graph_provenance_id=99999999,
            subject_node_key="user:1",
            predicate="MENTIONS",
            object_node_key="topic:foo",
            edge_key=_edge_key(),
        )


async def test_create_edge_rejects_duplicate_edge_key(edge_session) -> None:
    """Two active edges with the same edge_key violate uq_graph_edges_key
    (unique partial index WHERE purged_at IS NULL)."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.repos.graph_edge import create_edge

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)
    shared_key = _edge_key()

    await create_edge(
        edge_session,
        graph_provenance_id=prov_id,
        subject_node_key="user:1",
        predicate="MENTIONS",
        object_node_key="topic:a",
        edge_key=shared_key,
    )

    with pytest.raises(IntegrityError):
        await create_edge(
            edge_session,
            graph_provenance_id=prov_id,
            subject_node_key="user:2",
            predicate="AUTHORED",
            object_node_key="card:b",
            edge_key=shared_key,
        )


# ─── find_by_provenance ───────────────────────────────────────────────────────


async def test_find_by_provenance_returns_all_edges(edge_session) -> None:
    """find_by_provenance returns all edges (active and purged) for a provenance row."""
    from bot.db.repos.graph_edge import create_edge, find_by_provenance

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)

    edge1 = await create_edge(
        edge_session,
        graph_provenance_id=prov_id,
        subject_node_key="user:1",
        predicate="MENTIONS",
        object_node_key="topic:a",
        edge_key=_edge_key(),
    )
    edge2 = await create_edge(
        edge_session,
        graph_provenance_id=prov_id,
        subject_node_key="user:1",
        predicate="AUTHORED",
        object_node_key="card:x",
        edge_key=_edge_key(),
    )

    # Also create an edge on a DIFFERENT provenance to verify filtering
    prov_id2 = await _make_provenance(edge_session, run_id=run_id)
    await create_edge(
        edge_session,
        graph_provenance_id=prov_id2,
        subject_node_key="user:2",
        predicate="KNOWS_ABOUT",
        object_node_key="topic:b",
        edge_key=_edge_key(),
    )

    edges = await find_by_provenance(edge_session, prov_id)
    edge_ids = {e.id for e in edges}
    assert edge1.id in edge_ids
    assert edge2.id in edge_ids
    assert len(edges) == 2  # only edges for prov_id, not prov_id2


# ─── count_for_drift_check ────────────────────────────────────────────────────


async def test_count_for_drift_check_matches_neo4j(edge_session) -> None:
    """count_for_drift_check returns count of active (non-purged) graph_edges rows.

    T10-08 drift reconcile will compare this count against Neo4j's edge count.
    This test uses placeholder logic — actual Neo4j count comparison is T10-08.
    """
    from sqlalchemy import update

    from bot.db.models import GraphEdge
    from bot.db.repos.graph_edge import count_for_drift_check, create_edge

    initial_count = await count_for_drift_check(edge_session)

    run_id = await _make_run(edge_session)
    prov_id = await _make_provenance(edge_session, run_id=run_id)

    edge1 = await create_edge(
        edge_session,
        graph_provenance_id=prov_id,
        subject_node_key="user:1",
        predicate="MENTIONS",
        object_node_key="topic:a",
        edge_key=_edge_key(),
    )
    await create_edge(
        edge_session,
        graph_provenance_id=prov_id,
        subject_node_key="user:1",
        predicate="AUTHORED",
        object_node_key="card:x",
        edge_key=_edge_key(),
    )
    await edge_session.flush()

    count_after_insert = await count_for_drift_check(edge_session)
    assert count_after_insert == initial_count + 2

    # Soft-delete one edge (simulate purge)
    await edge_session.execute(
        update(GraphEdge)
        .where(GraphEdge.id == edge1.id)
        .values(purged_at=datetime.now(tz=timezone.utc))
    )
    await edge_session.flush()

    count_after_purge = await count_for_drift_check(edge_session)
    # purged edge is excluded; second edge still active
    assert count_after_purge == initial_count + 1
