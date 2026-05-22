"""Integration tests for bot/db/repos/graph_provenance.py (T10-02 / Phase 10).

Uses temp_database_url pattern (same as test_graph_projection_run_repo.py):
creates an isolated temp Postgres DB, runs alembic upgrade head (includes 061),
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
async def provenance_db_url() -> AsyncIterator[str]:
    """Temp DB with alembic upgrade head (includes migrations 060, 061, 062)."""
    base_url = _base_test_url()
    database_name = f"shkoder_provenance_{uuid.uuid4().hex[:12]}"
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
async def prov_session(provenance_db_url: str) -> AsyncIterator:
    """AsyncSession on the migrated temp DB; each test fully isolated via rollback."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(provenance_db_url, echo=False)
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
    """Insert a graph_projection_runs row and return its id."""
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(session, mode="incremental", started_by="test")
    await session.flush()
    return run.id


async def _make_message_version(session) -> int:
    """Insert minimal message_versions row and return its id.

    Users.id IS the telegram user id (BigInteger PK). ChatMessages requires
    message_id, chat_id, user_id, date. MessageVersions requires
    chat_message_id, version_seq.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text

    user_id = abs(hash(uuid.uuid4().hex)) % (10**9)
    # users.id is the PK = telegram_id; no separate telegram_id column
    await session.execute(
        text(
            "INSERT INTO users (id, first_name) VALUES (:id, :fname) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": user_id, "fname": "Test"},
    )

    msg_telegram_id = abs(hash(uuid.uuid4().hex)) % (10**9)
    result = await session.execute(
        text(
            "INSERT INTO chat_messages (message_id, chat_id, user_id, date) "
            "VALUES (:mid, :cid, :uid, :d) RETURNING id"
        ),
        {
            "mid": msg_telegram_id,
            "cid": -1001234567890,
            "uid": user_id,
            "d": datetime.now(tz=timezone.utc),
        },
    )
    chat_msg_id = result.scalar_one()

    result = await session.execute(
        text(
            "INSERT INTO message_versions (chat_message_id, version_seq, text, content_hash) "
            "VALUES (:mid, 1, 'hello', :ch) RETURNING id"
        ),
        {"mid": chat_msg_id, "ch": uuid.uuid4().hex},
    )
    return result.scalar_one()


async def _make_knowledge_card(session) -> uuid.UUID:
    """Insert minimal knowledge_cards row and return its id.

    knowledge_cards requires title + body_markdown (body_tsv is Computed).
    card_status defaults to 'draft'.
    """
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


# ─── create_provenance ────────────────────────────────────────────────────────


async def test_create_provenance_with_card_source(prov_session) -> None:
    """create_provenance inserts a row with source_card_id path."""
    from bot.db.repos.graph_provenance import create_provenance

    run_id = await _make_run(prov_session)
    card_id = await _make_knowledge_card(prov_session)

    row = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="knowledge_cards",
        source_pk=str(card_id),
        source_card_id=card_id,
        triple_hash=123456789,
    )

    assert row.id is not None
    assert row.projection_run_id == run_id
    assert row.source_table == "knowledge_cards"
    assert row.source_pk == str(card_id)
    assert row.source_card_id == card_id
    assert row.source_message_version_id is None
    assert row.triple_hash == 123456789
    assert row.purged_at is None
    assert row.graph_store == "neo4j"


async def test_create_provenance_with_message_version_source(prov_session) -> None:
    """create_provenance inserts a row with source_message_version_id path."""
    from bot.db.repos.graph_provenance import create_provenance

    run_id = await _make_run(prov_session)
    mv_id = await _make_message_version(prov_session)

    row = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
        triple_hash=456789012,
    )

    assert row.id is not None
    assert row.source_message_version_id == mv_id
    assert row.source_card_id is None
    assert row.source_table == "message_versions"


async def test_create_provenance_rejects_source_table_fk_mismatch(prov_session) -> None:
    """create_provenance raises ValueError when source_table='message_versions' but
    source_card_id is populated instead of source_message_version_id."""
    from bot.db.repos.graph_provenance import create_provenance

    run_id = await _make_run(prov_session)
    card_id = await _make_knowledge_card(prov_session)

    with pytest.raises(ValueError, match="source_table='message_versions' requires source_message_version_id only"):
        await create_provenance(
            prov_session,
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk=str(card_id),
            source_card_id=card_id,
            source_message_version_id=None,
        )


async def test_create_provenance_rejects_source_table_missing_fk(prov_session) -> None:
    """create_provenance raises ValueError when source_table='message_versions' but
    source_message_version_id is None (missing required FK)."""
    from bot.db.repos.graph_provenance import create_provenance

    run_id = await _make_run(prov_session)

    with pytest.raises(ValueError, match="source_table='message_versions' requires source_message_version_id to be set"):
        await create_provenance(
            prov_session,
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk="99",
            source_card_id=None,
            source_message_version_id=None,
        )


async def test_create_provenance_rejects_duplicate_active_triple(prov_session) -> None:
    """Two active rows with the same (source_table, source_pk, triple_hash) violate
    the unique partial index uq_graph_provenance_triple (WHERE purged_at IS NULL)."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.repos.graph_provenance import create_provenance

    run_id = await _make_run(prov_session)
    mv_id = await _make_message_version(prov_session)

    await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
        triple_hash=987654321,
    )

    with pytest.raises(IntegrityError):
        await create_provenance(
            prov_session,
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            triple_hash=987654321,
        )


async def test_create_provenance_rejects_dangling_projection_run_id(prov_session) -> None:
    """create_provenance with a non-existent projection_run_id raises IntegrityError
    (FK constraint on graph_provenance.projection_run_id)."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.repos.graph_provenance import create_provenance

    mv_id = await _make_message_version(prov_session)

    with pytest.raises(IntegrityError):
        await create_provenance(
            prov_session,
            projection_run_id=99999999,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
        )


async def test_create_provenance_rejects_no_source(prov_session) -> None:
    """create_provenance raises ValueError when both source FKs are NULL.

    With source_table='message_versions' and both FKs None, the source_table-
    specific check fires first ('requires source_message_version_id to be set').
    """
    from bot.db.repos.graph_provenance import create_provenance

    run_id = await _make_run(prov_session)

    with pytest.raises(ValueError, match="source_message_version_id"):
        await create_provenance(
            prov_session,
            projection_run_id=run_id,
            source_table="message_versions",
            source_pk="42",
            source_card_id=None,
            source_message_version_id=None,
        )


# ─── mark_inactive ────────────────────────────────────────────────────────────


async def test_mark_inactive_soft_deletes(prov_session) -> None:
    """mark_inactive sets purged_at to a non-NULL timestamp."""
    from bot.db.repos.graph_provenance import create_provenance, mark_inactive

    run_id = await _make_run(prov_session)
    mv_id = await _make_message_version(prov_session)

    row = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
    )
    assert row.purged_at is None

    await mark_inactive(prov_session, row.id)
    await prov_session.flush()
    await prov_session.refresh(row)

    assert row.purged_at is not None


# ─── find_by_source ───────────────────────────────────────────────────────────


async def test_find_by_source_returns_matching(prov_session) -> None:
    """find_by_source returns rows matching both card and message_version sources."""
    from bot.db.repos.graph_provenance import create_provenance, find_by_source

    run_id = await _make_run(prov_session)
    mv_id = await _make_message_version(prov_session)
    card_id = await _make_knowledge_card(prov_session)

    row_mv = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
    )
    row_card = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="knowledge_cards",
        source_pk=str(card_id),
        source_card_id=card_id,
    )

    rows_mv = await find_by_source(
        prov_session, source_table="message_versions", source_pk=str(mv_id)
    )
    assert len(rows_mv) == 1
    assert rows_mv[0].id == row_mv.id

    rows_card = await find_by_source(
        prov_session, source_table="knowledge_cards", source_pk=str(card_id)
    )
    assert len(rows_card) == 1
    assert rows_card[0].id == row_card.id


# ─── find_active ──────────────────────────────────────────────────────────────


async def test_find_active_filters_inactive(prov_session) -> None:
    """find_active excludes rows with purged_at set."""
    from bot.db.repos.graph_provenance import (
        create_provenance,
        find_active,
        mark_inactive,
    )

    run_id = await _make_run(prov_session)
    mv_id1 = await _make_message_version(prov_session)
    mv_id2 = await _make_message_version(prov_session)

    row_active = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id1),
        source_message_version_id=mv_id1,
    )
    row_inactive = await create_provenance(
        prov_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id2),
        source_message_version_id=mv_id2,
    )

    await mark_inactive(prov_session, row_inactive.id)
    await prov_session.flush()

    active_rows = await find_active(prov_session)
    active_ids = {r.id for r in active_rows}
    assert row_active.id in active_ids
    assert row_inactive.id not in active_ids
