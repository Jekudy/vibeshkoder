"""Butler per-user daily budget guard — T12-08.

``ButlerBudgetChecker`` enforces a per-user-day USD ceiling on Butler LLM
spend, using a caller-supplied ``spend_fn`` so the checker is DB-agnostic
and trivially testable.

``compute_user_daily_budget_spent`` is the concrete DB helper that queries
the ``llm_usage_ledger ↔ butler_actions`` join for a single user's spend
on the current MSK calendar day.

Design rationale
----------------
* The global daily ceiling ``_butler_budget_check`` in llm_gateway.py covers
  aggregate spend across all users. This module adds the *per-user* layer
  so a single user cannot exhaust the entire daily pool.
* MSK calendar day boundaries align with all other Butler/digest time windows
  (Phase 7/8 precedent).
* No new migration: the join via ``butler_actions.llm_usage_ledger_id`` links
  each ledger row to its requester without a DB column change.
* ``compute_user_daily_budget_spent`` delegates to ``_query_butler_daily_spend``
  which can be monkeypatched in tests to skip the real DB query.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Per-user-day default ceiling.  Operator may raise via env var.
_PER_USER_DAILY_USD_CEILING: Decimal = Decimal(
    os.environ.get("BUTLER_PER_USER_DAILY_USD_CEILING", "0.20")
)


class ButlerBudgetChecker:
    """Check whether a user has exceeded their per-day Butler spend ceiling.

    ``per_user_daily_ceiling`` is injected so tests can override without
    touching environment variables.
    """

    def __init__(
        self,
        per_user_daily_ceiling: Decimal = _PER_USER_DAILY_USD_CEILING,
    ) -> None:
        self._ceiling = per_user_daily_ceiling

    async def is_user_daily_exceeded(
        self,
        session: Any,
        *,
        user_id: int,
        spend_fn: Callable[[Any, int], Awaitable[Decimal]],
    ) -> bool:
        """Return True iff user's today-MSK Butler spend >= ceiling."""
        spent = await spend_fn(session, user_id)
        exceeded = spent >= self._ceiling
        if exceeded:
            logger.info(
                "butler_budget: per-user daily ceiling reached",
                extra={"user_id": user_id, "spent": str(spent), "ceiling": str(self._ceiling)},
            )
        return exceeded


async def _query_butler_daily_spend(session: Any, user_id: int) -> Decimal:  # positional to match spend_fn protocol
    """Execute the DB query for today's butler LLM spend for ``user_id``.

    Testable via ``monkeypatch.setattr(butler_budget, "_query_butler_daily_spend", ...)``
    without patching SQLAlchemy internals.

    Queries:
      SELECT SUM(l.cost_usd)
      FROM llm_usage_ledger l
      JOIN butler_actions ba ON ba.llm_usage_ledger_id = l.id
      WHERE ba.requester_tg_id = :user_id
        AND l.call_type IN ('butler_decision', 'butler_summary')
        AND l.created_at >= :day_start_utc
        AND l.created_at < :day_end_utc
    """
    from sqlalchemy import func, select

    from bot.db.models import LlmUsageLedger, ButlerAction

    now_utc = datetime.now(timezone.utc)
    # MSK = UTC+3; calendar day boundary aligned with Phase 7/8 convention
    msk_dt = now_utc + timedelta(hours=3)
    msk_start = msk_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    msk_end = msk_start + timedelta(days=1)
    day_start_utc = msk_start - timedelta(hours=3)
    day_end_utc = msk_end - timedelta(hours=3)

    stmt = (
        select(func.sum(LlmUsageLedger.cost_usd))
        .join(ButlerAction, ButlerAction.llm_usage_ledger_id == LlmUsageLedger.id)
        .where(
            ButlerAction.requester_tg_id == user_id,
            LlmUsageLedger.call_type.in_(["butler_decision", "butler_summary"]),
            LlmUsageLedger.created_at >= day_start_utc,
            LlmUsageLedger.created_at < day_end_utc,
        )
    )
    result = await session.execute(stmt)
    total = result.scalar_one_or_none()
    return Decimal(str(total)) if total is not None else Decimal("0")


async def compute_user_daily_budget_spent(session: Any, user_id: int) -> Decimal:
    """Return SUM of butler LLM spend for ``user_id`` on the current MSK calendar day.

    Signature matches the ``spend_fn: Callable[[Any, int], Awaitable[Decimal]]``
    protocol expected by ``ButlerBudgetChecker.is_user_daily_exceeded``.

    Delegates to ``_query_butler_daily_spend`` which can be patched in tests via
    ``monkeypatch.setattr(butler_budget, "_query_butler_daily_spend", ...)``.
    """
    return await _query_butler_daily_spend(session, user_id)
