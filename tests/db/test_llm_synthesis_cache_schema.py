"""T5-02 acceptance tests — llm_synthesis_cache schema.

Isolated temporary database pattern (same as test_fts_schema.py and
test_llm_usage_ledger_schema.py).
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
    database_name = f"shkoder_cache_schema_{uuid.uuid4().hex[:12]}"
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


# ─── Test 1: Table exists with all 8 columns + correct types/lengths ────────


async def test_llm_synthesis_cache_all_columns_exist(migrated_database_url: str) -> None:
    """All 8 columns exist in llm_synthesis_cache with correct types and lengths.

    Tuple: (data_type, character_maximum_length, numeric_precision, numeric_scale).
    None means 'not applicable / not reported by information_schema' for that slot.
    """
    expected_columns: dict[str, tuple[str, int | None, int | None, int | None]] = {
        "id":           ("bigint",                   None, 64,   0),
        "input_hash":   ("character",                64,   None, None),
        "answer_text":  ("text",                     None, None, None),
        "citation_ids": ("jsonb",                    None, None, None),
        "model":        ("character varying",        128,  None, None),
        "created_at":   ("timestamp with time zone", None, None, None),
        "last_hit_at":  ("timestamp with time zone", None, None, None),
        "hit_count":    ("integer",                  None, 32,   0),
    }

    for col_name, (exp_type, exp_char_len, exp_num_prec, exp_num_scale) in expected_columns.items():
        row = await _fetch_row(
            migrated_database_url,
            """
            SELECT data_type, character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'llm_synthesis_cache'
              AND column_name = $1
            """,
            col_name,
        )
        assert row is not None, f"Column '{col_name}' not found in llm_synthesis_cache"
        assert row["data_type"] == exp_type, (
            f"Column '{col_name}': expected data_type '{exp_type}', got '{row['data_type']}'"
        )
        assert row["character_maximum_length"] == exp_char_len, (
            f"Column '{col_name}': expected character_maximum_length {exp_char_len!r},"
            f" got {row['character_maximum_length']!r}"
        )
        assert row["numeric_precision"] == exp_num_prec, (
            f"Column '{col_name}': expected numeric_precision {exp_num_prec!r},"
            f" got {row['numeric_precision']!r}"
        )
        assert row["numeric_scale"] == exp_num_scale, (
            f"Column '{col_name}': expected numeric_scale {exp_num_scale!r},"
            f" got {row['numeric_scale']!r}"
        )


# ─── Test 2: UNIQUE on input_hash enforced ──────────────────────────────────


async def test_llm_synthesis_cache_unique_input_hash_enforced(migrated_database_url: str) -> None:
    """Duplicate INSERT on input_hash raises a unique violation."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        input_hash = "d" * 64

        await conn.execute(
            """
            INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
            VALUES ($1, 'first answer', '[1, 2]', 'claude-haiku')
            """,
            input_hash,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
                VALUES ($1, 'second answer', '[3]', 'claude-haiku')
                """,
                input_hash,
            )
    finally:
        await conn.close()


# ─── Test 3: citation_ids JSONB round-trips arrays ──────────────────────────


async def test_llm_synthesis_cache_citation_ids_jsonb_roundtrip(migrated_database_url: str) -> None:
    """citation_ids [1, 2, 3] → inserted → queried back as [1, 2, 3]."""
    import json

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        input_hash = "e" * 64
        citation_ids_in = [1, 2, 3]

        cache_id = await conn.fetchval(
            """
            INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
            VALUES ($1, 'some answer', $2::jsonb, 'claude-haiku')
            RETURNING id
            """,
            input_hash,
            json.dumps(citation_ids_in),
        )

        row = await conn.fetchrow(
            "SELECT citation_ids FROM llm_synthesis_cache WHERE id = $1",
            cache_id,
        )
        assert row is not None
        # asyncpg returns JSONB as Python objects
        citation_ids_out = row["citation_ids"]
        if isinstance(citation_ids_out, str):
            citation_ids_out = json.loads(citation_ids_out)
        assert citation_ids_out == [1, 2, 3]
    finally:
        await conn.close()


# ─── Test 4: JSONB containment query ────────────────────────────────────────


async def test_llm_synthesis_cache_jsonb_containment_query(migrated_database_url: str) -> None:
    """WHERE citation_ids @> '[1]'::jsonb finds row with [1, 2, 3]."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        input_hash = "f" * 64

        inserted_id = await conn.fetchval(
            """
            INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
            VALUES ($1, 'containment answer', '[1, 2, 3]'::jsonb, 'claude-haiku')
            RETURNING id
            """,
            input_hash,
        )

        found_id = await conn.fetchval(
            """
            SELECT id FROM llm_synthesis_cache
            WHERE citation_ids @> '[1]'::jsonb
              AND id = $1
            """,
            inserted_id,
        )
        assert found_id == inserted_id
    finally:
        await conn.close()


# ─── Test 5: Server defaults populate on minimal INSERT ─────────────────────


async def test_llm_synthesis_cache_server_defaults(migrated_database_url: str) -> None:
    """created_at, last_hit_at, hit_count populate from server defaults on minimal INSERT."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
            VALUES ($1, 'defaults test', '[]'::jsonb, 'claude-haiku')
            RETURNING created_at, last_hit_at, hit_count
            """,
            "g" * 64,
        )
    finally:
        await conn.close()

    assert row is not None
    assert row["created_at"] is not None
    assert row["last_hit_at"] is not None
    assert row["hit_count"] == 1


# ─── Test 6: ORM round-trip + metadata smoke ────────────────────────────────


async def test_llm_synthesis_cache_orm_round_trip(migrated_database_url: str) -> None:
    """ORM insert + select verifies all fields, plus tablename registered in metadata."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(migrated_database_url, echo=False)
    try:
        Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            from bot.db.models import LlmSynthesisCache

            cache = LlmSynthesisCache(
                input_hash="h" * 64,
                answer_text="The answer is 42.",
                citation_ids=[10, 20, 30],
                model="claude-haiku",
            )
            session.add(cache)
            await session.flush()

            result = await session.execute(
                select(LlmSynthesisCache).where(LlmSynthesisCache.id == cache.id)
            )
            fetched = result.scalar_one()

            assert fetched.input_hash == "h" * 64
            assert fetched.answer_text == "The answer is 42."
            assert fetched.citation_ids == [10, 20, 30]
            assert fetched.model == "claude-haiku"
            assert fetched.created_at is not None
            assert fetched.last_hit_at is not None
            assert fetched.hit_count == 1
    finally:
        await engine.dispose()


def test_llm_synthesis_cache_tablename_registered(app_env) -> None:
    """LlmSynthesisCache.__tablename__ is 'llm_synthesis_cache' and in Base.metadata."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert models.LlmSynthesisCache.__tablename__ == "llm_synthesis_cache"
    assert "llm_synthesis_cache" in models.Base.metadata.tables


# ─── Test 8: CHAR(64) boundary on input_hash ────────────────────────────────


async def test_llm_synthesis_cache_input_hash_char_64_boundary(migrated_database_url: str) -> None:
    """input_hash CHAR(64): exactly 64 chars succeeds; 65 chars raises DataError."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        # Exactly 64 chars — must succeed
        await conn.execute(
            """
            INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
            VALUES ($1, 'boundary test', '[]'::jsonb, 'claude-haiku')
            """,
            "i" * 64,
        )

        # 65 chars — must fail with a truncation error
        with pytest.raises(asyncpg.exceptions.StringDataRightTruncationError):
            await conn.execute(
                """
                INSERT INTO llm_synthesis_cache (input_hash, answer_text, citation_ids, model)
                VALUES ($1, 'over boundary', '[]'::jsonb, 'claude-haiku')
                """,
                "j" * 65,
            )
    finally:
        await conn.close()
