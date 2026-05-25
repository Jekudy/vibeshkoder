"""Repository for ``llm_usage_ledger`` (T5-03).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (gateway / handler) owns the transaction lifecycle.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import func, select, update

from bot.db.models import LlmUsageLedger
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class LedgerRepo:
    """Data-access layer for ``llm_usage_ledger``.

    All methods are ``@staticmethod`` and flush-only. They mirror the
    ``LedgerRepoProtocol`` defined in ``bot/services/llm_gateway.py`` so that
    the gateway can accept either the real repo or a Protocol-satisfying test fake.
    """

    @staticmethod
    async def record(
        session: AsyncSession,
        *,
        qa_trace_id: int | None,
        provider: str,
        model: str,
        prompt_hash: str,
        response_hash: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: str | None,
        cache_hit: bool,
        error: str | None,
        call_type: str = "unknown",
    ) -> LlmUsageLedger:
        """Insert a ledger row. Flushes; caller commits. NEVER commits internally.

        Used by the cache-hit path where all fields are known up-front.

        ``call_type`` added in migration 064 (T10-03). Canonical 8-value allow-list
        (migration 071 CHECK): 'unknown', 'qa_synthesis', 'digest_daily', 'digest_weekly',
        'graph_projection', 'extract_candidates', 'butler_decision', 'butler_summary'.
        Caller SHOULD pass explicitly; 'unknown' is the fallback only for legacy rows.
        """
        row = LlmUsageLedger(
            qa_trace_id=qa_trace_id,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            request_id=request_id,
            cache_hit=cache_hit,
            error=error,
            call_type=call_type,
        )
        session.add(row)
        await session.flush()
        _log.debug("llm_usage_ledger: inserted row id=%s", row.id)
        return row

    @staticmethod
    async def daily_cost_usd(
        session: AsyncSession,
        *,
        day: date,
        call_type: str | None = None,
    ) -> Decimal:
        """SUM(cost_usd) WHERE created_at >= day_utc_start AND < day_utc_end.

        UTC-bounded; ``day`` is interpreted as a UTC calendar date. Zero rows =>
        ``Decimal("0")`` (NEVER ``None``).

        ``call_type=None`` sums across all call types (backwards-compatible default).
        Pass ``call_type='graph_projection'`` to isolate graph projection costs from
        QA/digest costs, per the Phase 10 cost-bucket contract (Task 10.5-1 / #291).
        """
        day_start = datetime.combine(day, time(0), tzinfo=timezone.utc)
        next_day = date.fromordinal(day.toordinal() + 1)
        day_end = datetime.combine(next_day, time(0), tzinfo=timezone.utc)

        filters = [
            LlmUsageLedger.created_at >= day_start,
            LlmUsageLedger.created_at < day_end,
        ]
        if call_type is not None:
            filters.append(LlmUsageLedger.call_type == call_type)

        result = await session.execute(
            select(func.sum(LlmUsageLedger.cost_usd)).where(*filters)
        )
        total = result.scalar_one_or_none()
        return Decimal(str(total)) if total is not None else Decimal("0")

    @staticmethod
    async def monthly_cost_usd(
        session: AsyncSession,
        *,
        year: int,
        month: int,
    ) -> Decimal:
        """SUM(cost_usd) WHERE created_at within (year, month) UTC.

        UTC-bounded. Zero rows => ``Decimal("0")`` (NEVER ``None``).
        """
        month_start = datetime.combine(date(year, month, 1), time(0), tzinfo=timezone.utc)
        _, days_in_month = monthrange(year, month)
        month_end_date = date(year, month, days_in_month)
        next_month_date = date.fromordinal(month_end_date.toordinal() + 1)
        month_end = datetime.combine(next_month_date, time(0), tzinfo=timezone.utc)

        result = await session.execute(
            select(func.sum(LlmUsageLedger.cost_usd)).where(
                LlmUsageLedger.created_at >= month_start,
                LlmUsageLedger.created_at < month_end,
            )
        )
        total = result.scalar_one_or_none()
        return Decimal(str(total)) if total is not None else Decimal("0")

    @staticmethod
    async def update_placeholder(
        session: AsyncSession,
        *,
        llm_call_id: int,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: str | None,
        response_hash: str | None,
        error: str | None,
    ) -> int:
        """UPDATE the placeholder row written under STEP_BUDGET_GUARD_ATOMIC.

        Returns rowcount (must be 1). Flushes; caller commits. NEVER commits internally.

        Raises ``LookupError`` if ``llm_call_id`` is not found (placeholder row missing).
        This guards against process-crash scenarios where the placeholder was never written.

        Note: kwarg name is ``llm_call_id`` (not ``ledger_id``) — this matches the Protocol
        declared in ``bot/services/llm_gateway.py`` line ~150 which T5-01 PR #209 shipped.
        The contracts.md §5.1 used ``ledger_id``; the code on main wins per task brief.
        """
        stmt = (
            update(LlmUsageLedger)
            .where(LlmUsageLedger.id == llm_call_id)
            .values(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                request_id=request_id,
                response_hash=response_hash,
                error=error,
            )
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"LlmUsageLedger(id={llm_call_id}) not found — placeholder row missing"
            )
        await session.flush()
        _log.debug("llm_usage_ledger: updated placeholder id=%s rowcount=%s", llm_call_id, rowcount)
        return rowcount
