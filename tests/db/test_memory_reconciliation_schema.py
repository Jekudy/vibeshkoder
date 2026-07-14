"""Migration 087 acceptance tests for explicit paid-call reconciliation."""

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
    name = f"shkoder_reconcile_{uuid.uuid4().hex[:10]}"
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


async def test_087_backfills_dispatch_state_and_enforces_attempt_identity(
    temp_database_url: str,
) -> None:
    _alembic(temp_database_url, "upgrade", "086")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        completed_id = await connection.fetchval(
            """
            INSERT INTO extraction_runs (
                ingestion_window_start, ingestion_window_end,
                run_status, candidate_count, source_chat_id,
                semantic_key, source_snapshot_hash, prompt_template_version,
                provider, model, selection_mode
            ) VALUES (
                now() - interval '1 hour', now(), 'completed', 0, -10087001,
                $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time'
            ) RETURNING id
            """,
            "a" * 64,
            "b" * 64,
        )
        running_id = await connection.fetchval(
            """
            INSERT INTO extraction_runs (
                ingestion_window_start, ingestion_window_end,
                run_status, candidate_count, source_chat_id,
                semantic_key, source_snapshot_hash, prompt_template_version,
                provider, model, selection_mode,
                cursor_start_message_version_id, cursor_end_message_version_id
            ) VALUES (
                now() - interval '1 hour', now(), 'running', 0, -10087001,
                $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'version_cursor', 10, 20
            ) RETURNING id
            """,
            "c" * 64,
            "d" * 64,
        )
        failed_legacy_id = await connection.fetchval(
            """
            INSERT INTO extraction_runs (
                ingestion_window_start, ingestion_window_end,
                run_status, candidate_count
            ) VALUES (now() - interval '1 hour', now(), 'failed', 0)
            RETURNING id
            """
        )
    finally:
        await connection.close()

    _alembic(temp_database_url, "upgrade", "087")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "087"
        rows = {
            row["id"]: (row["attempt_no"], row["dispatch_state"])
            for row in await connection.fetch(
                """
                SELECT id, attempt_no, dispatch_state
                FROM extraction_runs
                WHERE id = ANY($1::uuid[])
                """,
                [completed_id, running_id, failed_legacy_id],
            )
        }
        assert rows[completed_id] == (1, "response_received")
        assert rows[running_id] == (1, "unknown")
        assert rows[failed_legacy_id] == (1, "not_dispatched")

        default_dispatch_state = await connection.fetchval(
            """
            INSERT INTO extraction_runs (run_status, candidate_count)
            VALUES ('running', 0)
            RETURNING dispatch_state
            """
        )
        assert default_dispatch_state == "not_dispatched"

        await connection.execute(
            """
            INSERT INTO extraction_runs (
                ingestion_window_start, ingestion_window_end,
                run_status, candidate_count, source_chat_id,
                semantic_key, source_snapshot_hash, prompt_template_version,
                provider, model, selection_mode, attempt_no, retry_of_run_id,
                dispatch_state
            ) VALUES (
                now() - interval '1 hour', now(), 'running', 0, -10087001,
                $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time', 2, $3,
                'not_dispatched'
            )
            """,
            "a" * 64,
            "b" * 64,
            completed_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                """
                INSERT INTO extraction_runs (
                    ingestion_window_start, ingestion_window_end,
                    run_status, candidate_count, source_chat_id,
                    semantic_key, source_snapshot_hash, prompt_template_version,
                    provider, model, selection_mode, attempt_no, retry_of_run_id,
                    dispatch_state
                ) VALUES (
                    now() - interval '1 hour', now(), 'running', 0, -10087001,
                    $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time', 2, $3,
                    'not_dispatched'
                )
                """,
                "a" * 64,
                "b" * 64,
                completed_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO extraction_runs (
                    ingestion_window_start, ingestion_window_end,
                    run_status, candidate_count,
                    semantic_key, source_snapshot_hash, prompt_template_version,
                    provider, model, selection_mode,
                    cursor_start_message_version_id, cursor_end_message_version_id,
                    dispatch_state
                ) VALUES (
                    now() - interval '1 hour', now(), 'running', 0,
                    $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'version_cursor',
                    0, 1, 'not_dispatched'
                )
                """,
                "e" * 64,
                "f" * 64,
            )
    finally:
        await connection.close()


async def test_087_resolution_audits_are_constrained_and_downgrade_fails_closed(
    temp_database_url: str,
) -> None:
    _alembic(temp_database_url, "upgrade", "087")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        await connection.execute(
            """
            INSERT INTO users (id, username, first_name)
            VALUES (870000001, 'operator_087', 'Operator')
            """
        )
        run_id = await connection.fetchval(
            """
            INSERT INTO extraction_runs (
                ingestion_window_start, ingestion_window_end,
                run_status, candidate_count, source_chat_id,
                semantic_key, source_snapshot_hash, prompt_template_version,
                provider, model, selection_mode, dispatch_state
            ) VALUES (
                now() - interval '1 hour', now(), 'running', 0, -10087001,
                $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time', 'unknown'
            ) RETURNING id
            """,
            "1" * 64,
            "2" * 64,
        )
        await connection.execute(
            """
            INSERT INTO extraction_run_resolutions (
                run_id, action, actor_user_id, reason,
                evidence_hash, accept_memory_gap
            ) VALUES ($1, 'risk_accepted_retry', 870000001, 'Provider audit inconclusive', $2, false)
            """,
            run_id,
            "3" * 64,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                """
                INSERT INTO extraction_run_resolutions (
                    run_id, action, actor_user_id, reason, accept_memory_gap
                ) VALUES ($1, 'abandon', 870000001, 'Duplicate resolution', true)
                """,
                run_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO image_description_resolutions (
                    message_media_id, attempt_no, action, actor_user_id, reason,
                    evidence_hash, accept_memory_gap
                ) VALUES (1, 1, 'abandon', 870000001, '', 'not-a-sha', false)
                """
            )
    finally:
        await connection.close()

    failed = _alembic(temp_database_url, "downgrade", "086", check=False)
    assert failed.returncode != 0
    assert "Cannot downgrade 087" in (failed.stdout + failed.stderr)


async def test_087_empty_roundtrip_is_allowed(temp_database_url: str) -> None:
    _alembic(temp_database_url, "upgrade", "087")
    _alembic(temp_database_url, "downgrade", "086")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "086"
    finally:
        await connection.close()


async def test_087_image_resolutions_are_exactly_once_per_provider_attempt(
    temp_database_url: str,
) -> None:
    _alembic(temp_database_url, "upgrade", "087")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        await connection.execute(
            """
            INSERT INTO users (id, username, first_name)
            VALUES (870000003, 'operator_087_attempts', 'Operator')
            """
        )
        chat_message_id = await connection.fetchval(
            """
            INSERT INTO chat_messages (message_id, chat_id, user_id, text, date)
            VALUES (870003, -100870003, 870000003, 'photo', now())
            RETURNING id
            """
        )
        media_id = await connection.fetchval(
            """
            INSERT INTO message_media (
                chat_message_id, media_kind, source_message_url,
                description_status, description_attempts
            ) VALUES ($1, 'photo', 'https://t.me/c/870003/1', 'failed', 2)
            RETURNING id
            """,
            chat_message_id,
        )
        await connection.execute(
            """
            INSERT INTO image_description_resolutions (
                message_media_id, attempt_no, action, actor_user_id, reason,
                accept_memory_gap
            ) VALUES
                ($1, 1, 'risk_accepted_retry', 870000003, 'First claim unknown', false),
                ($1, 2, 'abandon', 870000003, 'Second claim unknown', true)
            """,
            media_id,
        )
        assert (
            await connection.fetchval(
                """
            SELECT count(*)
            FROM image_description_resolutions
            WHERE message_media_id = $1
            """,
                media_id,
            )
            == 2
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                """
                INSERT INTO image_description_resolutions (
                    message_media_id, attempt_no, action, actor_user_id, reason,
                    accept_memory_gap
                ) VALUES ($1, 2, 'abandon', 870000003, 'Duplicate claim decision', true)
                """,
                media_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO image_description_resolutions (
                    message_media_id, attempt_no, action, actor_user_id, reason,
                    accept_memory_gap
                ) VALUES ($1, 0, 'abandon', 870000003, 'Invalid claim identity', true)
                """,
                media_id,
            )
    finally:
        await connection.close()


@pytest.mark.parametrize("rollout_kind", ["image_resolution", "retry_attempt"])
async def test_087_downgrade_fails_closed_for_each_rollout_audit_surface(
    temp_database_url: str,
    rollout_kind: str,
) -> None:
    _alembic(temp_database_url, "upgrade", "087")
    connection = await asyncpg.connect(**_kwargs(make_url(temp_database_url)))
    try:
        await connection.execute(
            """
            INSERT INTO users (id, username, first_name)
            VALUES (870000002, 'operator_087_guard', 'Operator')
            """
        )
        if rollout_kind == "image_resolution":
            chat_message_id = await connection.fetchval(
                """
                INSERT INTO chat_messages (message_id, chat_id, user_id, text, date)
                VALUES (870002, -100870002, 870000002, 'photo', now())
                RETURNING id
                """
            )
            media_id = await connection.fetchval(
                """
                INSERT INTO message_media (
                    chat_message_id, media_kind, source_message_url,
                    description_status
                ) VALUES ($1, 'photo', 'https://t.me/c/870002/1', 'failed')
                RETURNING id
                """,
                chat_message_id,
            )
            await connection.execute(
                """
                INSERT INTO image_description_resolutions (
                    message_media_id, attempt_no, action, actor_user_id, reason,
                    accept_memory_gap
                ) VALUES ($1, 1, 'abandon', 870000002, 'Provider outcome unknown', true)
                """,
                media_id,
            )
        else:
            first_id = await connection.fetchval(
                """
                INSERT INTO extraction_runs (
                    ingestion_window_start, ingestion_window_end,
                    run_status, candidate_count, source_chat_id,
                    semantic_key, source_snapshot_hash, prompt_template_version,
                    provider, model, selection_mode, dispatch_state
                ) VALUES (
                    now() - interval '1 hour', now(), 'failed', 0, -100870002,
                    $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time',
                    'rejected_pre_accept'
                ) RETURNING id
                """,
                "4" * 64,
                "5" * 64,
            )
            await connection.execute(
                """
                INSERT INTO extraction_runs (
                    ingestion_window_start, ingestion_window_end,
                    run_status, candidate_count, source_chat_id,
                    semantic_key, source_snapshot_hash, prompt_template_version,
                    provider, model, selection_mode, attempt_no, retry_of_run_id,
                    dispatch_state
                ) VALUES (
                    now() - interval '1 hour', now(), 'running', 0, -100870002,
                    $1, $2, 'v0.1.0', 'deepseek', 'deepseek-chat', 'event_time', 2, $3,
                    'not_dispatched'
                )
                """,
                "4" * 64,
                "5" * 64,
                first_id,
            )
    finally:
        await connection.close()

    failed = _alembic(temp_database_url, "downgrade", "086", check=False)
    assert failed.returncode != 0
    assert "Cannot downgrade 087" in (failed.stdout + failed.stderr)
