"""T7-01 acceptance tests — digests + digest_runs schema (migration 037).

These tests use the same pattern as test_fts_schema.py and
test_llm_usage_ledger_schema.py: a temporary isolated database is created,
Alembic upgrade head is run, then schema-shape and constraint assertions are
executed via asyncpg. The temporary database is dropped after each test.
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


async def _fetch_value(database_url: str, query: str, *args: object) -> object:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _fetch_row(database_url: str, query: str, *args: object) -> asyncpg.Record | None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


@pytest_asyncio.fixture()
async def temp_database_url() -> AsyncIterator[str]:
    base_url = _base_test_url()
    database_name = f"shkoder_digests_schema_{uuid.uuid4().hex[:12]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    try:
        yield base_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        await _drop_database(base_url, database_name)


@pytest_asyncio.fixture()
async def migrated_database_url(temp_database_url: str) -> AsyncIterator[str]:
    _run_alembic(temp_database_url, "upgrade", "head")
    yield temp_database_url


# ─── Test 1: head is now 037 ─────────────────────────────────────────────────


async def test_alembic_head_is_037(migrated_database_url: str) -> None:
    """After upgrade head, alembic_version reports 037."""
    current = await _fetch_value(
        migrated_database_url, "SELECT version_num FROM alembic_version"
    )
    assert current == "037"


# ─── Test 2: unique constraint on (type, window_start, window_end) ────────────


async def test_digests_unique_type_window_start_window_end(migrated_database_url: str) -> None:
    """INSERT same (type, window_start, window_end) twice → unique violation."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations)
            VALUES (
                'daily',
                '2026-05-13 00:00:00+00',
                '2026-05-14 00:00:00+00',
                'running',
                '[]'::jsonb
            )
            """
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO digests (type, window_start, window_end, status, citations)
                VALUES (
                    'daily',
                    '2026-05-13 00:00:00+00',
                    '2026-05-14 00:00:00+00',
                    'running',
                    '[]'::jsonb
                )
                """
            )
    finally:
        await conn.close()


# ─── Test 3: body_markdown NOT NULL when status='draft' ─────────────────────


async def test_digests_status_draft_requires_body_markdown(migrated_database_url: str) -> None:
    """INSERT status='draft' with body_markdown=NULL → check constraint violation."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (type, window_start, window_end, status, citations, body_markdown)
                VALUES (
                    'daily',
                    '2026-05-13 01:00:00+00',
                    '2026-05-14 01:00:00+00',
                    'draft',
                    '[]'::jsonb,
                    NULL
                )
                """
            )
    finally:
        await conn.close()


# ─── Test 4: status='running' with body_markdown=NULL succeeds ───────────────


async def test_digests_status_running_body_markdown_null_ok(migrated_database_url: str) -> None:
    """INSERT status='running' with body_markdown=NULL → succeeds."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        row_id = await conn.fetchval(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations, body_markdown)
            VALUES (
                'daily',
                '2026-05-13 02:00:00+00',
                '2026-05-14 02:00:00+00',
                'running',
                '[]'::jsonb,
                NULL
            )
            RETURNING id
            """
        )
        assert row_id is not None
    finally:
        await conn.close()


# ─── Test 5: status='posted' requires posted_message_id NOT NULL ─────────────


async def test_digests_status_posted_requires_posted_fields(migrated_database_url: str) -> None:
    """INSERT status='posted' with posted_message_id=NULL → check constraint violation."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations,
                    body_markdown, posted_chat_id, posted_message_id, posted_at
                )
                VALUES (
                    'daily',
                    '2026-05-13 03:00:00+00',
                    '2026-05-14 03:00:00+00',
                    'posted',
                    '[]'::jsonb,
                    'body',
                    -1001234567890,
                    NULL,
                    now()
                )
                """
            )
    finally:
        await conn.close()


# ─── Test 6: citations default is '[]'::jsonb ─────────────────────────────────


async def test_digests_citations_default_empty_array(migrated_database_url: str) -> None:
    """Omitting citations on INSERT → row has '[]'::jsonb default."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        row_id = await conn.fetchval(
            """
            INSERT INTO digests (type, window_start, window_end, status)
            VALUES (
                'daily',
                '2026-05-13 04:00:00+00',
                '2026-05-14 04:00:00+00',
                'running'
            )
            RETURNING id
            """
        )
        assert row_id is not None

        citations = await conn.fetchval(
            "SELECT citations FROM digests WHERE id = $1",
            row_id,
        )
        # asyncpg returns JSONB as a Python list; normalise both representations.
        import json as _json

        if isinstance(citations, str):
            citations = _json.loads(citations)
        assert citations == []
    finally:
        await conn.close()


# ─── Test 7: partial index ix_digests_status_draft exists ────────────────────


async def test_ix_digests_status_draft_partial_index_exists(migrated_database_url: str) -> None:
    """ix_digests_status_draft partial index exists on digests table."""
    indexdef = await _fetch_value(
        migrated_database_url,
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'digests'
          AND indexname = 'ix_digests_status_draft'
        """,
    )
    assert indexdef is not None, "ix_digests_status_draft not found"
    lowered = str(indexdef).lower()
    assert "where" in lowered
    assert "draft" in lowered


# ─── Test 8: GIN index ix_digests_citations_gin exists ───────────────────────


async def test_ix_digests_citations_gin_exists(migrated_database_url: str) -> None:
    """ix_digests_citations_gin GIN index exists on digests.citations."""
    indexdef = await _fetch_value(
        migrated_database_url,
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'digests'
          AND indexname = 'ix_digests_citations_gin'
        """,
    )
    assert indexdef is not None, "ix_digests_citations_gin not found"
    lowered = str(indexdef).lower()
    assert "using gin" in lowered


# ─── Test 9: partial index ix_digests_posting_started_at exists ──────────────


async def test_ix_digests_posting_started_at_partial_index_exists(
    migrated_database_url: str,
) -> None:
    """ix_digests_posting_started_at partial index exists on digests table."""
    indexdef = await _fetch_value(
        migrated_database_url,
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'digests'
          AND indexname = 'ix_digests_posting_started_at'
        """,
    )
    assert indexdef is not None, "ix_digests_posting_started_at not found"
    lowered = str(indexdef).lower()
    assert "where" in lowered
    assert "posting" in lowered


# ─── Test 10: downgrade -1 drops digest_runs then digests ────────────────────


async def test_alembic_downgrade_drops_digest_tables(temp_database_url: str) -> None:
    """alembic downgrade -1 from 037 drops digest_runs and digests, returns to 036."""
    _run_alembic(temp_database_url, "upgrade", "head")

    # Verify both tables exist before downgrade
    for table_name in ("digests", "digest_runs"):
        exists = await _fetch_value(
            temp_database_url,
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = $1
            )
            """,
            table_name,
        )
        assert exists is True, f"Expected '{table_name}' to exist before downgrade"

    _run_alembic(temp_database_url, "downgrade", "-1")

    # Both tables must be gone
    for table_name in ("digests", "digest_runs"):
        exists = await _fetch_value(
            temp_database_url,
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = $1
            )
            """,
            table_name,
        )
        assert exists is False, f"Expected '{table_name}' to be absent after downgrade"

    current = await _fetch_value(temp_database_url, "SELECT version_num FROM alembic_version")
    assert current == "036"


# ─── Test 11: upgrade → downgrade → upgrade roundtrip ────────────────────────


async def test_alembic_037_upgrade_downgrade_upgrade_roundtrip(temp_database_url: str) -> None:
    """Full roundtrip: upgrade head → downgrade -1 → upgrade head."""
    _run_alembic(temp_database_url, "upgrade", "head")
    _run_alembic(temp_database_url, "downgrade", "-1")
    _run_alembic(temp_database_url, "upgrade", "head")

    current = await _fetch_value(temp_database_url, "SELECT version_num FROM alembic_version")
    assert current == "037"

    # Verify tables re-appeared
    for table_name in ("digests", "digest_runs"):
        exists = await _fetch_value(
            temp_database_url,
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = $1
            )
            """,
            table_name,
        )
        assert exists is True, f"Expected '{table_name}' to exist after re-upgrade"


# ─── Test 12: ORM metadata smoke ─────────────────────────────────────────────


def test_digest_orm_classes_registered(app_env) -> None:
    """Digest and DigestRun ORM classes are registered in Base.metadata."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert models.Digest.__tablename__ == "digests"
    assert models.DigestRun.__tablename__ == "digest_runs"
    assert "digests" in models.Base.metadata.tables
    assert "digest_runs" in models.Base.metadata.tables


# ─── Test 13: digest_runs FK ON DELETE SET NULL ───────────────────────────────


async def test_digest_runs_fk_digest_id_set_null_on_digest_delete(
    migrated_database_url: str,
) -> None:
    """DELETE digests row → digest_runs.digest_id becomes NULL (ON DELETE SET NULL)."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        digest_id = await conn.fetchval(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations)
            VALUES ('daily', '2026-05-13 05:00:00+00', '2026-05-14 05:00:00+00', 'running', '[]')
            RETURNING id
            """
        )
        run_id = await conn.fetchval(
            """
            INSERT INTO digest_runs (digest_id, status)
            VALUES ($1, 'running')
            RETURNING id
            """,
            digest_id,
        )
        await conn.execute("DELETE FROM digests WHERE id = $1", digest_id)
        row = await conn.fetchrow(
            "SELECT digest_id FROM digest_runs WHERE id = $1", run_id
        )
        assert row is not None
        assert row["digest_id"] is None
    finally:
        await conn.close()
