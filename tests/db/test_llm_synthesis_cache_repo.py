"""T5-03 acceptance tests — SynthesisCacheRepo (llm_synthesis_cache table).

Uses the ``db_session`` fixture from ``tests/conftest.py`` which connects to the
real postgres and wraps each test in a rolled-back outer transaction for isolation.
Tests are skipped automatically if postgres is unreachable.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=7_700_000_000)


def _input_hash(n: int | None = None) -> str:
    """Return a unique 64-char hex-like string for input_hash."""
    suffix = str(next(_counter) if n is None else n)
    return ("d" * (64 - len(suffix)) + suffix)[:64]


# ─── Test 1: get_or_none returns None when no row matches ────────────────────


async def test_get_or_none_returns_none_when_absent(db_session) -> None:
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    result = await SynthesisCacheRepo.get_or_none(db_session, input_hash=_input_hash())

    assert result is None


# ─── Test 2: get_or_none returns the row when match exists ───────────────────


async def test_get_or_none_returns_row_when_present(db_session) -> None:
    from bot.db.models import LlmSynthesisCache
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    h = _input_hash()
    inserted = await SynthesisCacheRepo.store(
        db_session,
        input_hash=h,
        answer_text="some answer",
        citation_ids=[1, 2, 3],
        model="claude-haiku",
    )

    fetched = await SynthesisCacheRepo.get_or_none(db_session, input_hash=h)

    assert fetched is not None
    assert isinstance(fetched, LlmSynthesisCache)
    assert fetched.id == inserted.id
    assert fetched.input_hash == h
    assert fetched.answer_text == "some answer"
    assert fetched.citation_ids == [1, 2, 3]


# ─── Test 3: store inserts row with expected defaults ────────────────────────


async def test_store_inserts_row_with_defaults(db_session) -> None:
    from bot.db.models import LlmSynthesisCache
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    h = _input_hash()
    row = await SynthesisCacheRepo.store(
        db_session,
        input_hash=h,
        answer_text="cached answer",
        citation_ids=[10, 20],
        model="gpt-4o",
    )

    assert isinstance(row, LlmSynthesisCache)
    assert row.id is not None
    assert row.id > 0
    assert row.input_hash == h
    assert row.answer_text == "cached answer"
    assert row.citation_ids == [10, 20]
    assert row.model == "gpt-4o"
    # Server defaults
    assert row.created_at is not None
    assert row.last_hit_at is not None
    assert row.hit_count == 1


# ─── Test 4: store raises IntegrityError on duplicate input_hash ──────────────


async def test_store_raises_integrity_error_on_duplicate_input_hash(db_session) -> None:
    """UNIQUE constraint on input_hash raises IntegrityError on second insert."""
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    h = _input_hash()
    await SynthesisCacheRepo.store(
        db_session,
        input_hash=h,
        answer_text="first answer",
        citation_ids=[],
        model="claude-haiku",
    )

    with pytest.raises(IntegrityError):
        await SynthesisCacheRepo.store(
            db_session,
            input_hash=h,
            answer_text="second answer — should fail",
            citation_ids=[],
            model="claude-haiku",
        )


# ─── Test 5: bump_hit increments hit_count and updates last_hit_at ───────────


async def test_bump_hit_increments_hit_count(db_session) -> None:
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    row = await SynthesisCacheRepo.store(
        db_session,
        input_hash=_input_hash(),
        answer_text="bump test",
        citation_ids=[5],
        model="claude-haiku",
    )
    initial_hit_count = row.hit_count  # server_default = 1

    # Backdate last_hit_at by 1 hour to make the advance observable.
    old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    await db_session.execute(
        text("UPDATE llm_synthesis_cache SET last_hit_at = :old WHERE id = :id"),
        {"old": old_ts, "id": row.id},
    )
    await db_session.flush()

    await SynthesisCacheRepo.bump_hit(db_session, cache_id=row.id)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.hit_count == initial_hit_count + 1
    assert row.last_hit_at > old_ts


# ─── Test 6: invalidate_by_citation returns 0 when no rows reference the id ──


async def test_invalidate_by_citation_returns_zero_when_no_match(db_session) -> None:
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    # Insert a cache row that does NOT cite message_version_id=999.
    await SynthesisCacheRepo.store(
        db_session,
        input_hash=_input_hash(),
        answer_text="unrelated answer",
        citation_ids=[100, 200],
        model="claude-haiku",
    )

    rowcount = await SynthesisCacheRepo.invalidate_by_citation(
        db_session, message_version_id=999
    )

    assert rowcount == 0


# ─── Test 7: invalidate_by_citation returns N>0 for matching rows ─────────────


async def test_invalidate_by_citation_deletes_matching_rows(db_session) -> None:
    """Two rows cite message_version_id=42; one does not. Delete returns 2."""
    from bot.db.models import LlmSynthesisCache
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    target_mv_id = 42

    row_a = await SynthesisCacheRepo.store(
        db_session,
        input_hash=_input_hash(),
        answer_text="answer A cites 42",
        citation_ids=[target_mv_id, 100],
        model="claude-haiku",
    )
    row_b = await SynthesisCacheRepo.store(
        db_session,
        input_hash=_input_hash(),
        answer_text="answer B cites 42",
        citation_ids=[50, target_mv_id],
        model="claude-haiku",
    )
    row_c = await SynthesisCacheRepo.store(
        db_session,
        input_hash=_input_hash(),
        answer_text="answer C does NOT cite 42",
        citation_ids=[7, 8, 9],
        model="claude-haiku",
    )

    rowcount = await SynthesisCacheRepo.invalidate_by_citation(
        db_session, message_version_id=target_mv_id
    )

    assert rowcount == 2

    # row_a and row_b must be gone; row_c must survive.
    result_a = await db_session.execute(
        select(LlmSynthesisCache).where(LlmSynthesisCache.id == row_a.id)
    )
    assert result_a.scalar_one_or_none() is None, "row_a should have been deleted"

    result_b = await db_session.execute(
        select(LlmSynthesisCache).where(LlmSynthesisCache.id == row_b.id)
    )
    assert result_b.scalar_one_or_none() is None, "row_b should have been deleted"

    result_c = await db_session.execute(
        select(LlmSynthesisCache).where(LlmSynthesisCache.id == row_c.id)
    )
    assert result_c.scalar_one_or_none() is not None, "row_c must survive"


# ─── Bonus: get_or_none is idempotent (multiple calls same hash) ──────────────


async def test_get_or_none_idempotent_multiple_calls(db_session) -> None:
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    h = _input_hash()
    await SynthesisCacheRepo.store(
        db_session,
        input_hash=h,
        answer_text="idempotent check",
        citation_ids=[],
        model="claude-haiku",
    )

    r1 = await SynthesisCacheRepo.get_or_none(db_session, input_hash=h)
    r2 = await SynthesisCacheRepo.get_or_none(db_session, input_hash=h)
    assert r1 is not None
    assert r2 is not None
    assert r1.id == r2.id
