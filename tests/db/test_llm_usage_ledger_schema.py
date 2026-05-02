"""T5-02 acceptance tests — llm_usage_ledger schema.

These tests use the same pattern as test_fts_schema.py: a temporary isolated
database is created, Alembic upgrade head is run, then schema-shape assertions
are executed via asyncpg. The temporary database is dropped after each test.
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
    database_name = f"shkoder_ledger_schema_{uuid.uuid4().hex[:12]}"
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


# ─── Test 1: Table exists with all 14 columns + correct types ───────────────


async def test_llm_usage_ledger_all_columns_exist(migrated_database_url: str) -> None:
    """All 14 columns exist in llm_usage_ledger with correct data types."""
    expected_columns = {
        "id": "bigint",
        "qa_trace_id": "integer",
        "provider": "character varying",
        "model": "character varying",
        "prompt_hash": "character",
        "response_hash": "character",
        "tokens_in": "integer",
        "tokens_out": "integer",
        "cost_usd": "numeric",
        "latency_ms": "integer",
        "request_id": "character varying",
        "cache_hit": "boolean",
        "error": "character varying",
        "created_at": "timestamp with time zone",
    }

    for col_name, expected_type in expected_columns.items():
        actual_type = await _fetch_value(
            migrated_database_url,
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'llm_usage_ledger'
              AND column_name = $1
            """,
            col_name,
        )
        assert actual_type == expected_type, (
            f"Column '{col_name}': expected type '{expected_type}', got '{actual_type}'"
        )


# ─── Test 2: FK qa_trace_id → qa_traces.id ON DELETE SET NULL ───────────────


async def test_llm_usage_ledger_fk_qa_trace_id_set_null(migrated_database_url: str) -> None:
    """FK constraint qa_trace_id → qa_traces.id with ON DELETE SET NULL."""
    row = await _fetch_row(
        migrated_database_url,
        """
        SELECT
            ccu.table_name AS ref_table,
            rc.delete_rule
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema = kcu.table_schema
        JOIN information_schema.referential_constraints rc
          ON tc.constraint_name = rc.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON rc.unique_constraint_name = ccu.constraint_name
          AND rc.unique_constraint_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = 'llm_usage_ledger'
          AND kcu.column_name = 'qa_trace_id'
        """,
    )
    assert row is not None, "FK constraint on qa_trace_id not found"
    assert row["ref_table"] == "qa_traces"
    assert row["delete_rule"] == "SET NULL"


# ─── Test 3: Three indexes present ──────────────────────────────────────────


async def test_llm_usage_ledger_indexes_exist(migrated_database_url: str) -> None:
    """All three expected indexes exist on llm_usage_ledger."""
    expected_indexes = [
        "ix_llm_usage_ledger_qa_trace_id",
        "ix_llm_usage_ledger_model_created_at",
        "ix_llm_usage_ledger_created_at",
    ]
    for index_name in expected_indexes:
        exists = await _fetch_value(
            migrated_database_url,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'llm_usage_ledger'
                  AND indexname = $1
            )
            """,
            index_name,
        )
        assert exists is True, f"Index '{index_name}' not found on llm_usage_ledger"


# ─── Test 4: Server defaults populate on minimal INSERT ─────────────────────


async def test_llm_usage_ledger_server_defaults(migrated_database_url: str) -> None:
    """Server defaults populate correctly when only required columns are provided."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO llm_usage_ledger (provider, model, prompt_hash)
            VALUES ('anthropic', 'claude-haiku', $1)
            RETURNING
                tokens_in, tokens_out, cost_usd, latency_ms,
                cache_hit, created_at, qa_trace_id, response_hash, request_id, error
            """,
            "a" * 64,
        )
    finally:
        await conn.close()

    assert row is not None
    assert row["tokens_in"] == 0
    assert row["tokens_out"] == 0
    assert float(row["cost_usd"]) == 0.0
    assert row["latency_ms"] == 0
    assert row["cache_hit"] is False
    assert row["created_at"] is not None
    assert row["qa_trace_id"] is None
    assert row["response_hash"] is None
    assert row["request_id"] is None
    assert row["error"] is None


# ─── Test 5: ORM round-trip ──────────────────────────────────────────────────


async def test_llm_usage_ledger_orm_round_trip(migrated_database_url: str) -> None:
    """ORM insert + select round-trip verifies all fields persist correctly."""
    from decimal import Decimal

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(migrated_database_url, echo=False)
    try:
        Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            from bot.db.models import LlmUsageLedger

            ledger = LlmUsageLedger(
                provider="openai",
                model="gpt-4o",
                prompt_hash="b" * 64,
                response_hash="c" * 64,
                tokens_in=100,
                tokens_out=50,
                cost_usd=Decimal("0.000123"),
                latency_ms=250,
                request_id="req-123",
                cache_hit=True,
                error=None,
            )
            session.add(ledger)
            await session.flush()

            result = await session.execute(
                select(LlmUsageLedger).where(LlmUsageLedger.id == ledger.id)
            )
            fetched = result.scalar_one()

            assert fetched.provider == "openai"
            assert fetched.model == "gpt-4o"
            assert fetched.prompt_hash == "b" * 64
            assert fetched.response_hash == "c" * 64
            assert fetched.tokens_in == 100
            assert fetched.tokens_out == 50
            assert fetched.cost_usd == Decimal("0.000123")
            assert fetched.latency_ms == 250
            assert fetched.request_id == "req-123"
            assert fetched.cache_hit is True
            assert fetched.error is None
    finally:
        await engine.dispose()


# ─── Test 6: ORM metadata smoke ─────────────────────────────────────────────


def test_llm_usage_ledger_tablename_registered(app_env) -> None:
    """LlmUsageLedger.__tablename__ is 'llm_usage_ledger' and in Base.metadata."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert models.LlmUsageLedger.__tablename__ == "llm_usage_ledger"
    assert "llm_usage_ledger" in models.Base.metadata.tables


# ─── Test 7: downgrade drops tables cleanly ─────────────────────────────────


async def test_llm_usage_ledger_downgrade_drops_table(temp_database_url: str) -> None:
    """alembic downgrade -1 from 024 drops llm_usage_ledger."""
    _run_alembic(temp_database_url, "upgrade", "head")

    # Verify table exists
    exists_before = await _fetch_value(
        temp_database_url,
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'llm_usage_ledger'
        )
        """,
    )
    assert exists_before is True

    _run_alembic(temp_database_url, "downgrade", "-1")

    exists_after = await _fetch_value(
        temp_database_url,
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'llm_usage_ledger'
        )
        """,
    )
    assert exists_after is False
