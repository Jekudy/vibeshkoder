"""Migration 084 acceptance tests for durable static deployment audit rows."""

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


async def _create_database(admin_url: URL, database_name: str) -> None:
    connection = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await connection.execute(f"CREATE DATABASE {_quote_identifier(database_name)}")
    finally:
        await connection.close()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    connection = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await connection.execute(
            """
            SELECT pg_terminate_backend(pid)
              FROM pg_stat_activity
             WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database_name,
        )
        await connection.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
    finally:
        await connection.close()


def _run_alembic(
    database_url: str,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=check,
    )


@pytest_asyncio.fixture()
async def temp_database_url() -> AsyncIterator[str]:
    base_url = _base_test_url()
    database_name = f"shkoder_wiki_deploy_{uuid.uuid4().hex[:10]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    try:
        yield base_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        await _drop_database(base_url, database_name)


async def test_084_schema_constraints_and_repeat_success_audit(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "084")
    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        columns = await connection.fetch(
            """
            SELECT column_name, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'wiki_static_deployments'
            """
        )
        nullable = {row["column_name"]: row["is_nullable"] for row in columns}
        assert nullable == {
            "id": "NO",
            "manifest_sha256": "NO",
            "project": "NO",
            "branch": "NO",
            "status": "NO",
            "deployment_url": "YES",
            "error_code": "YES",
            "error_class": "YES",
            "started_at": "NO",
            "finished_at": "YES",
            "created_at": "NO",
            "updated_at": "NO",
        }

        constraint_names = {
            row["conname"]
            for row in await connection.fetch(
                """
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'wiki_static_deployments'::regclass
                """
            )
        }
        assert {
            "ck_wiki_static_deployments_status",
            "ck_wiki_static_deployments_manifest_sha256",
            "ck_wiki_static_deployments_terminal_state",
        } <= constraint_names

        index_definition = await connection.fetchval(
            """
            SELECT indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'wiki_static_deployments'
               AND indexname = 'ix_wiki_static_deployments_success_lookup'
            """
        )
        assert index_definition is not None
        assert "CREATE INDEX" in index_definition
        assert "CREATE UNIQUE INDEX" not in index_definition
        assert "manifest_sha256, project, branch" in index_definition
        assert "WHERE ((status)::text = 'succeeded'::text)" in index_definition

        manifest = "a" * 64
        await connection.execute(
            """
            INSERT INTO wiki_static_deployments (
                manifest_sha256, project, branch, status,
                deployment_url, finished_at
            )
            VALUES ($1, 'wiki', 'main', 'succeeded', 'https://wiki.example.test', now())
            """,
            manifest,
        )
        await connection.execute(
            """
            INSERT INTO wiki_static_deployments (
                manifest_sha256, project, branch, status,
                deployment_url, finished_at
            )
            VALUES ($1, 'wiki', 'main', 'succeeded', 'https://wiki.example.test', now())
            """,
            manifest,
        )
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                  FROM wiki_static_deployments
                 WHERE manifest_sha256 = $1
                   AND project = 'wiki'
                   AND branch = 'main'
                   AND status = 'succeeded'
                """,
                manifest,
            )
            == 2
        )

        await connection.execute(
            """
            INSERT INTO wiki_static_deployments (
                manifest_sha256, project, branch, status,
                error_code, finished_at
            )
            VALUES
                ($1, 'wiki', 'main', 'failed', 'smoke_failed', now()),
                ($1, 'wiki', 'main', 'failed', 'smoke_failed', now())
            """,
            manifest,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO wiki_static_deployments (
                    manifest_sha256, project, branch, status, finished_at
                )
                VALUES ($1, 'wiki', 'preview', 'failed', now())
                """,
                manifest,
            )
    finally:
        await connection.close()


async def test_084_downgrade_removes_only_static_deployment_table(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "084")
    _run_alembic(temp_database_url, "downgrade", "083")

    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "083"
        assert (
            await connection.fetchval(
                "SELECT to_regclass('public.wiki_static_deployments') IS NULL"
            )
            is True
        )
    finally:
        await connection.close()


async def test_084_downgrade_fails_closed_when_audit_rows_exist(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "084")
    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await connection.execute(
            """
            INSERT INTO wiki_static_deployments (
                manifest_sha256, project, branch, status,
                deployment_url, finished_at
            ) VALUES (
                $1, 'wiki', 'main', 'succeeded',
                'https://wiki.example.test', now()
            )
            """,
            "f" * 64,
        )
    finally:
        await connection.close()

    result = _run_alembic(
        temp_database_url,
        "downgrade",
        "083",
        check=False,
    )
    assert result.returncode != 0
    assert "Cannot downgrade 084: wiki_static_deployments contains audit rows" in (
        result.stdout + result.stderr
    )

    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "084"
        assert await connection.fetchval("SELECT count(*) FROM wiki_static_deployments") == 1
    finally:
        await connection.close()
