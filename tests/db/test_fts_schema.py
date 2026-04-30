"""Phase 4 FTS schema acceptance tests.

These tests create an isolated temporary database, run Alembic against it, and
drop the database afterwards. They do not mutate the shared pytest database.
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


@pytest_asyncio.fixture()
async def temp_database_url() -> AsyncIterator[str]:
    base_url = _base_test_url()
    database_name = f"shkoder_fts_schema_{uuid.uuid4().hex[:12]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    try:
        yield base_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        await _drop_database(base_url, database_name)


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


async def _fetch_one(database_url: str, query: str, *args: object) -> asyncpg.Record | None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def _fetch_value(database_url: str, query: str, *args: object) -> object:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _insert_message_version(
    database_url: str,
    *,
    text_value: str | None,
    caption_value: str | None,
    normalized_text: str | None,
    content_hash: str,
) -> int:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (id, username, first_name)
                VALUES (910000000001, 'fts_schema', 'FTS')
                ON CONFLICT (id) DO NOTHING
                """
            )
            chat_message_id = await conn.fetchval(
                """
                INSERT INTO chat_messages (message_id, chat_id, user_id, text, date)
                VALUES (
                    (SELECT 42001 + count(*) FROM chat_messages),
                    -10042001,
                    910000000001,
                    $1,
                    now()
                )
                RETURNING id
                """,
                text_value,
            )
            return await conn.fetchval(
                """
                INSERT INTO message_versions (
                    chat_message_id,
                    version_seq,
                    text,
                    caption,
                    normalized_text,
                    content_hash
                )
                VALUES ($1, 1, $2, $3, $4, $5)
                RETURNING id
                """,
                chat_message_id,
                text_value,
                caption_value,
                normalized_text,
                content_hash,
            )
    finally:
        await conn.close()


@pytest_asyncio.fixture()
async def migrated_database_url(temp_database_url: str) -> AsyncIterator[str]:
    _run_alembic(temp_database_url, "upgrade", "head")
    yield temp_database_url


async def test_alembic_upgrade_head_on_clean_db_green(migrated_database_url: str) -> None:
    current = await _fetch_value(migrated_database_url, "SELECT version_num FROM alembic_version")

    assert current == "021"


async def test_insert_message_versions_generates_search_tsv_from_normalized_text(
    migrated_database_url: str,
) -> None:
    version_id = await _insert_message_version(
        migrated_database_url,
        text_value="ORIGINAL TEXT SHOULD NOT DRIVE SEARCH",
        caption_value=None,
        normalized_text="тестовое сообщение",
        content_hash="fts-schema-normalized",
    )

    row = await _fetch_one(
        migrated_database_url,
        """
        SELECT
            search_tsv::text AS search_tsv_text,
            search_tsv @@ plainto_tsquery('russian', 'сообщение') AS matches_query,
            search_tsv @@ plainto_tsquery('russian', 'original') AS text_matches
        FROM message_versions
        WHERE id = $1
        """,
        version_id,
    )

    assert row is not None
    assert row["search_tsv_text"]
    assert row["matches_query"] is True
    assert row["text_matches"] is False


async def test_insert_message_versions_generates_search_tsv_from_caption(
    migrated_database_url: str,
) -> None:
    version_id = await _insert_message_version(
        migrated_database_url,
        text_value=None,
        caption_value="русская подпись",
        normalized_text=None,
        content_hash="fts-schema-caption",
    )

    row = await _fetch_one(
        migrated_database_url,
        """
        SELECT search_tsv @@ plainto_tsquery('russian', 'подпись') AS matches_query
        FROM message_versions
        WHERE id = $1
        """,
        version_id,
    )

    assert row is not None
    assert row["matches_query"] is True


async def test_insert_message_versions_null_content_generates_empty_search_tsv(
    migrated_database_url: str,
) -> None:
    version_id = await _insert_message_version(
        migrated_database_url,
        text_value=None,
        caption_value=None,
        normalized_text=None,
        content_hash="fts-schema-empty",
    )

    row = await _fetch_one(
        migrated_database_url,
        """
        SELECT
            search_tsv::text AS search_tsv_text,
            search_tsv @@ plainto_tsquery('russian', 'anything') AS matches_query
        FROM message_versions
        WHERE id = $1
        """,
        version_id,
    )

    assert row is not None
    assert row["search_tsv_text"] == ""
    assert row["matches_query"] is False


async def test_pg_indexes_contains_named_gin_index(migrated_database_url: str) -> None:
    indexdef = await _fetch_value(
        migrated_database_url,
        """
        SELECT indexdef
        FROM pg_indexes
            WHERE schemaname = 'public'
                AND tablename = 'message_versions'
                AND indexname = 'ix_message_versions_search_tsv'
            """,
    )

    assert indexdef is not None
    lowered = str(indexdef).lower()
    assert "using gin" in lowered
    assert "(search_tsv)" in lowered
    assert "where" not in lowered


def test_message_version_metadata_includes_search_tsv(app_env) -> None:
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    table = models.Base.metadata.tables["message_versions"]

    assert "search_tsv" in table.columns
    assert "ix_message_versions_search_tsv" in {ix.name for ix in table.indexes}


async def test_alembic_downgrade_minus_one_drops_search_tsv(temp_database_url: str) -> None:
    _run_alembic(temp_database_url, "upgrade", "head")
    _run_alembic(temp_database_url, "downgrade", "-1")

    column_exists = await _fetch_value(
        temp_database_url,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
                WHERE table_schema = 'public'
                    AND table_name = 'message_versions'
                    AND column_name = 'search_tsv'
            )
            """,
    )
    index_exists = await _fetch_value(
        temp_database_url,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_indexes
                WHERE schemaname = 'public'
                    AND tablename = 'message_versions'
                    AND indexname = 'ix_message_versions_search_tsv'
            )
            """,
    )
    current = await _fetch_value(temp_database_url, "SELECT version_num FROM alembic_version")

    assert column_exists is False
    assert index_exists is False
    assert current == "020"
