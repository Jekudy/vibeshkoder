"""T5-03 acceptance tests — LedgerRepo (llm_usage_ledger table).

Uses the ``db_session`` fixture from ``tests/conftest.py`` which connects to the
real postgres and wraps each test in a rolled-back outer transaction for isolation.
Tests are skipped automatically if postgres is unreachable.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=8_800_000_000)


def _prompt_hash(n: int | None = None) -> str:
    """Return a unique 64-char hex-like string for prompt_hash."""
    suffix = str(next(_counter) if n is None else n)
    return ("a" * (64 - len(suffix)) + suffix)[:64]


# ─── Test 1: record inserts row + returns LlmUsageLedger with assigned id ────


async def test_record_inserts_and_returns_row(db_session) -> None:
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    row = await LedgerRepo.record(
        db_session,
        qa_trace_id=None,
        provider="anthropic",
        model="claude-haiku",
        prompt_hash=_prompt_hash(),
        response_hash=None,
        tokens_in=10,
        tokens_out=5,
        cost_usd=Decimal("0.000012"),
        latency_ms=123,
        request_id="req-001",
        cache_hit=False,
        error=None,
    )

    assert isinstance(row, LlmUsageLedger)
    assert row.id is not None
    assert row.id > 0
    assert row.provider == "anthropic"
    assert row.model == "claude-haiku"
    assert row.tokens_in == 10
    assert row.tokens_out == 5
    assert row.cost_usd == Decimal("0.000012")
    assert row.latency_ms == 123
    assert row.request_id == "req-001"
    assert row.cache_hit is False
    assert row.error is None


# ─── Test 2: record flushes but does NOT commit ───────────────────────────────


async def test_record_flushes_but_does_not_commit(db_session) -> None:
    """After record(), the session is still in transaction (outer tx not committed).

    We verify by rolling back and asserting the row is absent in a fresh SELECT.
    The outer transaction in db_session fixture rolls back at teardown; here we
    verify the state is consistent with still being inside the transaction.
    """
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    row = await LedgerRepo.record(
        db_session,
        qa_trace_id=None,
        provider="openai",
        model="gpt-4o",
        prompt_hash=_prompt_hash(),
        response_hash="b" * 64,
        tokens_in=50,
        tokens_out=25,
        cost_usd=Decimal("0.001"),
        latency_ms=200,
        request_id=None,
        cache_hit=False,
        error=None,
    )
    inserted_id = row.id

    # Session is still in transaction (flush does not commit).
    assert db_session.in_transaction()

    # The row IS visible inside the same session (flush made it visible to this conn).
    from bot.db.models import LlmUsageLedger

    result = await db_session.execute(
        select(LlmUsageLedger).where(LlmUsageLedger.id == inserted_id)
    )
    assert result.scalar_one_or_none() is not None


# ─── Test 3: daily_cost_usd returns Decimal("0") when zero rows ──────────────


async def test_daily_cost_usd_returns_zero_when_no_rows(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    result = await LedgerRepo.daily_cost_usd(db_session, day=date(2099, 12, 31))

    assert result == Decimal("0")
    assert isinstance(result, Decimal)


# ─── Test 4: daily_cost_usd sums correctly across UTC day boundary ────────────


async def test_daily_cost_usd_respects_utc_day_boundary(db_session) -> None:
    """Row at 23:59:59 UTC of day-1 excluded; row at 00:00:01 UTC of day included."""
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    target_day = date(2030, 6, 15)

    # Row at 23:59:59 UTC of the day BEFORE target_day — must NOT be counted.
    prev_day_ts = datetime(2030, 6, 14, 23, 59, 59, tzinfo=timezone.utc)
    prev_row = LlmUsageLedger(
        provider="anthropic",
        model="claude-haiku",
        prompt_hash=_prompt_hash(),
        tokens_in=1,
        tokens_out=1,
        cost_usd=Decimal("0.100000"),
        latency_ms=10,
        cache_hit=False,
    )
    db_session.add(prev_row)
    await db_session.flush()
    # Override created_at via raw SQL (ORM uses server_default which we can't set easily)
    from sqlalchemy import text

    await db_session.execute(
        text("UPDATE llm_usage_ledger SET created_at = :ts WHERE id = :id"),
        {"ts": prev_day_ts, "id": prev_row.id},
    )

    # Row at 00:00:01 UTC of target_day — must be counted.
    target_day_ts = datetime(2030, 6, 15, 0, 0, 1, tzinfo=timezone.utc)
    target_row = LlmUsageLedger(
        provider="anthropic",
        model="claude-haiku",
        prompt_hash=_prompt_hash(),
        tokens_in=1,
        tokens_out=1,
        cost_usd=Decimal("0.050000"),
        latency_ms=10,
        cache_hit=False,
    )
    db_session.add(target_row)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE llm_usage_ledger SET created_at = :ts WHERE id = :id"),
        {"ts": target_day_ts, "id": target_row.id},
    )
    await db_session.flush()

    total = await LedgerRepo.daily_cost_usd(db_session, day=target_day)

    assert total == Decimal("0.050000"), f"expected 0.050000, got {total}"


# ─── Test 5: monthly_cost_usd returns Decimal("0") when zero rows ────────────


async def test_monthly_cost_usd_returns_zero_when_no_rows(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    result = await LedgerRepo.monthly_cost_usd(db_session, year=2099, month=11)

    assert result == Decimal("0")
    assert isinstance(result, Decimal)


# ─── Test 6: monthly_cost_usd sums within month and excludes prev/next ────────


async def test_monthly_cost_usd_respects_calendar_month(db_session) -> None:
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from sqlalchemy import text

    target_year, target_month = 2031, 3  # March 2031

    # Row in previous month (Feb 28 23:59:59 UTC) — must NOT be counted.
    prev_ts = datetime(2031, 2, 28, 23, 59, 59, tzinfo=timezone.utc)
    prev_row = LlmUsageLedger(
        provider="openai",
        model="gpt-4o",
        prompt_hash=_prompt_hash(),
        cost_usd=Decimal("1.000000"),
        latency_ms=10,
        cache_hit=False,
    )
    db_session.add(prev_row)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE llm_usage_ledger SET created_at = :ts WHERE id = :id"),
        {"ts": prev_ts, "id": prev_row.id},
    )

    # Row at start of target month (Mar 1 00:00:01 UTC) — must be counted.
    mid1_ts = datetime(2031, 3, 1, 0, 0, 1, tzinfo=timezone.utc)
    mid1_row = LlmUsageLedger(
        provider="openai",
        model="gpt-4o",
        prompt_hash=_prompt_hash(),
        cost_usd=Decimal("0.300000"),
        latency_ms=10,
        cache_hit=False,
    )
    db_session.add(mid1_row)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE llm_usage_ledger SET created_at = :ts WHERE id = :id"),
        {"ts": mid1_ts, "id": mid1_row.id},
    )

    # Row at end of target month (Mar 31 23:59:59 UTC) — must be counted.
    mid2_ts = datetime(2031, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
    mid2_row = LlmUsageLedger(
        provider="openai",
        model="gpt-4o",
        prompt_hash=_prompt_hash(),
        cost_usd=Decimal("0.200000"),
        latency_ms=10,
        cache_hit=False,
    )
    db_session.add(mid2_row)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE llm_usage_ledger SET created_at = :ts WHERE id = :id"),
        {"ts": mid2_ts, "id": mid2_row.id},
    )

    # Row in next month (Apr 1 00:00:01 UTC) — must NOT be counted.
    next_ts = datetime(2031, 4, 1, 0, 0, 1, tzinfo=timezone.utc)
    next_row = LlmUsageLedger(
        provider="openai",
        model="gpt-4o",
        prompt_hash=_prompt_hash(),
        cost_usd=Decimal("2.000000"),
        latency_ms=10,
        cache_hit=False,
    )
    db_session.add(next_row)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE llm_usage_ledger SET created_at = :ts WHERE id = :id"),
        {"ts": next_ts, "id": next_row.id},
    )
    await db_session.flush()

    total = await LedgerRepo.monthly_cost_usd(
        db_session, year=target_year, month=target_month
    )

    # Only the two March rows should sum: 0.300000 + 0.200000 = 0.500000
    assert total == Decimal("0.500000"), f"expected 0.500000, got {total}"


# ─── Test 7: update_placeholder updates existing row and returns rowcount 1 ───


async def test_update_placeholder_updates_row_and_returns_one(db_session) -> None:
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    # Insert a placeholder row (minimal — simulates STEP_BUDGET_GUARD_ATOMIC insert).
    placeholder = LlmUsageLedger(
        provider="anthropic",
        model="claude-haiku",
        prompt_hash=_prompt_hash(),
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal("0"),
        latency_ms=0,
        cache_hit=False,
        error=None,
    )
    db_session.add(placeholder)
    await db_session.flush()
    placeholder_id = placeholder.id

    rowcount = await LedgerRepo.update_placeholder(
        db_session,
        llm_call_id=placeholder_id,
        tokens_in=150,
        tokens_out=75,
        cost_usd=Decimal("0.000456"),
        latency_ms=350,
        request_id="req-xyz",
        response_hash="c" * 64,
        error=None,
    )

    assert rowcount == 1

    # Verify the row was actually updated.
    result = await db_session.execute(
        select(LlmUsageLedger).where(LlmUsageLedger.id == placeholder_id)
    )
    updated = result.scalar_one()
    assert updated.tokens_in == 150
    assert updated.tokens_out == 75
    assert updated.cost_usd == Decimal("0.000456")
    assert updated.latency_ms == 350
    assert updated.request_id == "req-xyz"
    assert updated.response_hash == "c" * 64
    assert updated.error is None


# ─── Test 8: update_placeholder raises LookupError when id not found ─────────


async def test_update_placeholder_raises_lookup_error_when_not_found(db_session) -> None:
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    nonexistent_id = 999_999_999

    with pytest.raises(LookupError):
        await LedgerRepo.update_placeholder(
            db_session,
            llm_call_id=nonexistent_id,
            tokens_in=10,
            tokens_out=5,
            cost_usd=Decimal("0.001"),
            latency_ms=100,
            request_id=None,
            response_hash=None,
            error=None,
        )


# ─── Bonus: record with null qa_trace_id and error field ─────────────────────


async def test_record_with_null_qa_trace_id_and_error(db_session) -> None:
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    row = await LedgerRepo.record(
        db_session,
        qa_trace_id=None,
        provider="anthropic",
        model="claude-opus",
        prompt_hash=_prompt_hash(),
        response_hash=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal("0"),
        latency_ms=0,
        request_id=None,
        cache_hit=False,
        error="provider_transient:timeout",
    )

    assert isinstance(row, LlmUsageLedger)
    assert row.qa_trace_id is None
    assert row.error == "provider_transient:timeout"
    assert row.cache_hit is False
