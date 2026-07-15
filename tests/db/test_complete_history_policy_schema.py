"""Migration 082 acceptance tests for the complete-history policy."""

from __future__ import annotations

import json
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
    database_name = f"shkoder_complete_history_{uuid.uuid4().hex[:10]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    try:
        yield base_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        await _drop_database(base_url, database_name)


async def _seed_forget_policy_rows(database_url: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        await conn.executemany(
            """
            INSERT INTO forget_events (
                target_type,
                target_id,
                authorized_by,
                tombstone_key,
                status,
                cascade_status
            )
            VALUES ('message', $1, 'system', $2, $3, $4::jsonb)
            """,
            [
                ("pending", "migration-082:pending", "pending", '{"digests":"completed"}'),
                ("processing", "migration-082:processing", "processing", None),
                ("completed", "migration-082:completed", "completed", "{}"),
                ("failed", "migration-082:failed", "failed", '{"error":"legacy"}'),
            ],
        )
        await conn.executemany(
            """
            INSERT INTO offrecord_marks (
                mark_type,
                scope_type,
                scope_id,
                detected_by,
                status
            )
            VALUES ('offrecord', 'chat', $1, 'migration-082-test', $2)
            """,
            [("active", "active"), ("expired", "expired"), ("revoked", "revoked")],
        )
    finally:
        await conn.close()


async def _seed_message_version(
    database_url: str,
    *,
    version_seq: int,
    content_hash: str,
    is_redacted: bool,
) -> int:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO users (id, username, first_name)
            VALUES (982000000001, 'migration_082', 'Migration 082')
            ON CONFLICT (id) DO NOTHING
            """
        )
        chat_message_id = await conn.fetchval(
            """
            INSERT INTO chat_messages (message_id, chat_id, user_id, text, date)
            VALUES (982001, -100982001, 982000000001, 'complete history', now())
            ON CONFLICT (chat_id, message_id)
            DO UPDATE SET text = EXCLUDED.text
            RETURNING id
            """
        )
        await conn.execute(
            """
            INSERT INTO message_versions (
                chat_message_id,
                version_seq,
                text,
                normalized_text,
                content_hash,
                is_redacted
            )
            VALUES ($1, $2, 'complete history', 'complete history', $3, $4)
            """,
            chat_message_id,
            version_seq,
            content_hash,
            is_redacted,
        )
        return int(chat_message_id)
    finally:
        await conn.close()


async def test_082_upgrade_supersedes_live_events_and_revokes_active_marks(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "081")
    await _seed_forget_policy_rows(temp_database_url)

    _run_alembic(temp_database_url, "upgrade", "082")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        event_rows = await conn.fetch(
            """
            SELECT target_id, status, cascade_status
            FROM forget_events
            ORDER BY target_id
            """
        )
        events = {row["target_id"]: row for row in event_rows}
        for original_status in ("pending", "processing", "completed"):
            row = events[original_status]
            assert row["status"] == "superseded"
            cascade_status = json.loads(row["cascade_status"])
            assert cascade_status["phase13"] == "superseded_complete_history"

        assert json.loads(events["pending"]["cascade_status"])["digests"] == "completed"
        assert events["failed"]["status"] == "failed"
        assert json.loads(events["failed"]["cascade_status"]) == {"error": "legacy"}

        mark_rows = await conn.fetch(
            "SELECT scope_id, status FROM offrecord_marks ORDER BY scope_id"
        )
        assert {row["scope_id"]: row["status"] for row in mark_rows} == {
            "active": "revoked",
            "expired": "expired",
            "revoked": "revoked",
        }

        await conn.execute(
            """
            INSERT INTO forget_events (
                target_type, target_id, authorized_by, tombstone_key, status
            )
            VALUES ('message', 'new', 'system', 'migration-082:new', 'superseded')
            """
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO forget_events (
                    target_type, target_id, authorized_by, tombstone_key, status
                )
                VALUES ('message', 'invalid', 'system', 'migration-082:invalid', 'invalid')
                """
            )
    finally:
        await conn.close()


async def test_082_partial_index_allows_audit_copy_but_only_one_active_version(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "081")
    chat_message_id = await _seed_message_version(
        temp_database_url,
        version_seq=1,
        content_hash="same-canonical-content",
        is_redacted=False,
    )

    _run_alembic(temp_database_url, "upgrade", "082")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        index_definition = await conn.fetchval(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'message_versions'
              AND indexname = 'uq_message_versions_chat_message_content_hash_active'
            """
        )
        assert index_definition is not None
        assert "CREATE UNIQUE INDEX" in index_definition
        assert "WHERE (is_redacted = false)" in index_definition

        await conn.execute(
            "UPDATE message_versions SET is_redacted = true WHERE chat_message_id = $1",
            chat_message_id,
        )
        await conn.execute(
            """
            INSERT INTO message_versions (
                chat_message_id, version_seq, content_hash, is_redacted
            )
            VALUES ($1, 2, 'same-canonical-content', false)
            """,
            chat_message_id,
        )
        await conn.execute(
            """
            INSERT INTO message_versions (
                chat_message_id, version_seq, content_hash, is_redacted
            )
            VALUES ($1, 3, 'same-canonical-content', true)
            """,
            chat_message_id,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO message_versions (
                    chat_message_id, version_seq, content_hash, is_redacted
                )
                VALUES ($1, 4, 'same-canonical-content', false)
                """,
                chat_message_id,
            )
    finally:
        await conn.close()


async def test_082_downgrade_restores_081_constraints_when_hashes_are_unique(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "081")
    chat_message_id = await _seed_message_version(
        temp_database_url,
        version_seq=1,
        content_hash="downgrade-unique",
        is_redacted=False,
    )
    _run_alembic(temp_database_url, "upgrade", "082")

    _run_alembic(temp_database_url, "downgrade", "081")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "081"
        old_constraint_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'uq_message_versions_chat_message_content_hash'
                  AND contype = 'u'
            )
            """
        )
        assert old_constraint_exists is True
        partial_index_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = 'uq_message_versions_chat_message_content_hash_active'
            )
            """
        )
        assert partial_index_exists is False

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO forget_events (
                    target_type, target_id, authorized_by, tombstone_key, status
                )
                VALUES ('message', 'new', 'system', 'migration-082:downgrade', 'superseded')
                """
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO message_versions (
                    chat_message_id, version_seq, content_hash, is_redacted
                )
                VALUES ($1, 2, 'downgrade-unique', true)
                """,
                chat_message_id,
            )
    finally:
        await conn.close()


async def test_082_downgrade_preserves_superseded_forget_provenance(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "081")
    await _seed_forget_policy_rows(temp_database_url)
    _run_alembic(temp_database_url, "upgrade", "082")

    result = _run_alembic(temp_database_url, "downgrade", "081", check=False)
    assert result.returncode != 0
    assert (
        "Cannot downgrade 082: superseded forget-event provenance cannot be restored"
        in result.stdout + result.stderr
    )

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "082"
        rows = await conn.fetch(
            """
            SELECT target_id, status, cascade_status
            FROM forget_events
            WHERE status = 'superseded'
            ORDER BY target_id
            """
        )
        assert [row["target_id"] for row in rows] == [
            "completed",
            "pending",
            "processing",
        ]
        assert all(
            json.loads(row["cascade_status"])["phase13"] == "superseded_complete_history"
            for row in rows
        )
    finally:
        await conn.close()


async def test_082_downgrade_conservatively_preserves_revoked_offrecord_marks(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "081")
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO offrecord_marks (
                mark_type, scope_type, scope_id, detected_by, status
            ) VALUES (
                'offrecord', 'chat', 'phase13-active', 'migration-082-test', 'active'
            )
            """
        )
    finally:
        await conn.close()
    _run_alembic(temp_database_url, "upgrade", "082")

    result = _run_alembic(temp_database_url, "downgrade", "081", check=False)
    assert result.returncode != 0
    assert "Cannot downgrade 082: revoked offrecord marks may contain phase13 state" in (
        result.stdout + result.stderr
    )

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "082"
        assert (
            await conn.fetchval(
                "SELECT status FROM offrecord_marks WHERE scope_id = 'phase13-active'"
            )
            == "revoked"
        )
    finally:
        await conn.close()


async def test_082_downgrade_fails_closed_when_restored_and_audit_hashes_overlap(
    temp_database_url: str,
) -> None:
    _run_alembic(temp_database_url, "upgrade", "082")
    chat_message_id = await _seed_message_version(
        temp_database_url,
        version_seq=1,
        content_hash="downgrade-overlap",
        is_redacted=True,
    )

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO message_versions (
                chat_message_id, version_seq, content_hash, is_redacted
            )
            VALUES ($1, 2, 'downgrade-overlap', false)
            """,
            chat_message_id,
        )
    finally:
        await conn.close()

    result = _run_alembic(temp_database_url, "downgrade", "081", check=False)
    assert result.returncode != 0
    assert "Cannot downgrade 082" in result.stdout + result.stderr

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "082"
        assert (
            await conn.fetchval(
                """
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = 'uq_message_versions_chat_message_content_hash_active'
            )
            """
            )
            is True
        )
    finally:
        await conn.close()
