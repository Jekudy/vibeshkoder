"""Migration 085 acceptance tests on an isolated PostgreSQL database."""

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
    return make_url(
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )


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
async def temp_database_url() -> AsyncIterator[str]:
    base_url = _base_test_url()
    database_name = f"shkoder_extract_once_{uuid.uuid4().hex[:10]}"
    try:
        admin = await asyncpg.connect(**_asyncpg_kwargs(base_url, database="postgres"))
        try:
            await admin.execute(f"CREATE DATABASE {_quote_identifier(database_name)}")
        finally:
            await admin.close()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    try:
        yield database_url
    finally:
        admin = await asyncpg.connect(**_asyncpg_kwargs(base_url, database="postgres"))
        try:
            await admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                  FROM pg_stat_activity
                 WHERE datname = $1 AND pid <> pg_backend_pid()
                """,
                database_name,
            )
            await admin.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
        finally:
            await admin.close()


async def test_085_schema_enforces_semantic_identity_and_cursor_contract(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "085")
    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "085"
        assert (
            await connection.fetchval("SELECT to_regclass('public.extraction_cursors') IS NOT NULL")
            is True
        )
        extraction_run_columns = {
            row["column_name"]
            for row in await connection.fetch(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'extraction_runs'
                """
            )
        }
        assert {
            "semantic_key",
            "source_snapshot_hash",
            "prompt_template_version",
            "provider",
            "model",
            "selection_mode",
            "cursor_start_message_version_id",
            "cursor_end_message_version_id",
        } <= extraction_run_columns

        key = "a" * 64
        snapshot = "b" * 64
        insert_sql = """
            INSERT INTO extraction_runs (
                ingestion_window_start, ingestion_window_end,
                run_status, candidate_count, semantic_key,
                source_snapshot_hash, prompt_template_version,
                provider, model, selection_mode
            ) VALUES (
                now() - interval '1 hour', now(), 'completed', 0, $1,
                $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time'
            )
        """
        await connection.execute(insert_sql, key, snapshot)
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(insert_sql, key, snapshot)
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO extraction_runs (
                    run_status, candidate_count, semantic_key
                ) VALUES ('running', 0, $1)
                """,
                "c" * 64,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO extraction_cursors (
                    source_chat_id, last_message_version_id
                ) VALUES (-1, -1)
                """
            )
    finally:
        await connection.close()


async def test_085_empty_downgrade_is_allowed(temp_database_url: str) -> None:
    _run_alembic(temp_database_url, "upgrade", "085")
    _run_alembic(temp_database_url, "downgrade", "084")
    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "084"
    finally:
        await connection.close()


@pytest.mark.parametrize("rollout_data", ["semantic_run", "cursor", "candidate"])
async def test_085_downgrade_fails_closed_with_rollout_data(
    temp_database_url: str,
    rollout_data: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "085")
    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        if rollout_data == "semantic_run":
            await connection.execute(
                """
                INSERT INTO extraction_runs (
                    ingestion_window_start, ingestion_window_end,
                    run_status, candidate_count, semantic_key,
                    source_snapshot_hash, prompt_template_version,
                    provider, model, selection_mode
                ) VALUES (
                    now() - interval '1 hour', now(), 'completed', 0,
                    $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time'
                )
                """,
                "d" * 64,
                "e" * 64,
            )
        elif rollout_data == "cursor":
            await connection.execute(
                """
                INSERT INTO extraction_cursors (
                    source_chat_id, last_message_version_id
                ) VALUES (-1001, 42)
                """
            )
        else:
            await connection.execute(
                """
                INSERT INTO extraction_candidates (
                    candidate_json, source_message_version_ids,
                    status, payload_schema_version
                ) VALUES ('{}'::jsonb, '[]'::jsonb, 'pending', 'karpathy-wiki-v1')
                """
            )
    finally:
        await connection.close()

    with pytest.raises(subprocess.CalledProcessError):
        _run_alembic(temp_database_url, "downgrade", "084")
    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "085"
    finally:
        await connection.close()
