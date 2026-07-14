"""Migration 086 acceptance tests for durable image-description claims."""

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


def _base_url() -> URL:
    return make_url(
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )


def _kwargs(url: URL, *, database: str | None = None) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database or url.database,
    }


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _alembic(url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
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
    base = _base_url()
    name = f"shkoder_vision_claim_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(**_kwargs(base, database="postgres"))
    try:
        await admin.execute(f"CREATE DATABASE {_quote(name)}")
    except Exception as exc:  # pragma: no cover - environment guard
        await admin.close()
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")
    await admin.close()
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        admin = await asyncpg.connect(**_kwargs(base, database="postgres"))
        try:
            await admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = $1 AND pid <> pg_backend_pid()
                """,
                name,
            )
            await admin.execute(f"DROP DATABASE IF EXISTS {_quote(name)}")
        finally:
            await admin.close()


async def _seed_media(connection: asyncpg.Connection) -> int:
    await connection.execute(
        """
        INSERT INTO users (id, username, first_name)
        VALUES (986000000001, 'migration_086', 'Migration 086')
        """
    )
    chat_message_id = await connection.fetchval(
        """
        INSERT INTO chat_messages (message_id, chat_id, user_id, text, date)
        VALUES (986001, -100986001, 986000000001, 'photo', now())
        RETURNING id
        """
    )
    return int(
        await connection.fetchval(
            """
            INSERT INTO message_media (
                chat_message_id, media_kind, source_message_url,
                description_status
            )
            VALUES ($1, 'photo', 'https://t.me/c/986001/1', 'pending')
            RETURNING id
            """,
            chat_message_id,
        )
    )


async def test_086_processing_requires_a_complete_claim_and_downgrade_is_fail_closed(
    temp_database_url: str,
) -> None:
    _alembic(temp_database_url, "upgrade", "086")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        columns = {
            row["column_name"]
            for row in await connection.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='message_media'
                """
            )
        }
        assert {"description_claim_token", "description_claimed_at"} <= columns
        media_id = await _seed_media(connection)
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE message_media SET description_status='processing' WHERE id=$1",
                media_id,
            )
        await connection.execute(
            """
            UPDATE message_media
            SET description_status='processing',
                description_claim_token=$2,
                description_claimed_at=now()
            WHERE id=$1
            """,
            media_id,
            str(uuid.uuid4()),
        )
    finally:
        await connection.close()

    failed = _alembic(temp_database_url, "downgrade", "085", check=False)
    assert failed.returncode != 0
    assert "durable image claims are present" in (failed.stdout + failed.stderr)

    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        await connection.execute(
            """
            UPDATE message_media
            SET description_status='pending',
                description_claim_token=NULL,
                description_claimed_at=NULL
            """
        )
    finally:
        await connection.close()
    _alembic(temp_database_url, "downgrade", "085")
