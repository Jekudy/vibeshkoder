"""Repository for ``graph_projection_runs`` (Phase 10 / W0-A).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.

Stats update pattern: uses SQLAlchemy UPDATE statements to avoid stale
read-modify-write cycles. The deep-merge for JSONB-style stats is done via
column-level UPDATE (individual columns, not a JSONB column), so each call
replaces only the explicitly supplied keys. Unknown keys are rejected with
ValueError (not silently ignored) — this is intentional to surface stale
callers early.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GraphProjectionRun
from bot.services.graph_common import GraphProjectionMode, GraphProjectionRunStatus

_log = logging.getLogger(__name__)

# Terminal statuses per migration 060 CHECK constraint on status column.
# Non-terminal is only 'running'. A run transitions running → terminal exactly once.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "cost_exceeded", "dry_run_complete"}
)
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"running"})

# Columns that update_run_stats is allowed to set. Enumerated from migration 060.
# Semantics: SET-replace (not increment). Unknown keys are rejected. Empty patch is no-op.
_UPDATABLE_STATS_COLS: frozenset[str] = frozenset({
    "source_card_count",
    "source_message_version_count",
    "projected_node_count",
    "projected_edge_count",
    "skipped_policy_count",
    "skipped_budget_count",
    "llm_prompt_tokens",
    "llm_completion_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "source_cutoff_at",
    "error_code",
    "error_context",
})


async def create_run(
    session: AsyncSession,
    *,
    mode: GraphProjectionMode,
    started_by: str | None = None,
) -> GraphProjectionRun:
    """Insert a new graph_projection_runs row with status='running'.

    mode: one of 'dry_run', 'incremental', 'full_rebuild', 'repair'.
    started_by: free-text label for who triggered this run (e.g. 'scheduler',
        'admin:149820031'). None for system-initiated runs.

    Flushes; caller commits. NEVER commits internally.
    """
    run = GraphProjectionRun(
        mode=mode,
        status="running",
        started_by=started_by,
    )
    session.add(run)
    await session.flush()
    _log.debug("graph_projection_runs: inserted run id=%s mode=%s", run.id, mode)
    return run


async def update_run_stats(
    session: AsyncSession,
    run_id: int,
    *,
    stats_patch: dict,
) -> None:
    """Update individual stat columns on a graph_projection_runs row.

    Semantics:
    - SET-replace (NOT increment): each supplied key overwrites the column value.
    - Unknown keys rejected with ValueError (not silently ignored).
    - Empty patch is a no-op (returns immediately without touching the row).

    stats_patch keys must be a subset of the allowed columns defined by
    _UPDATABLE_STATS_COLS (enumerated from migration 060).

    Flushes; caller commits. NEVER commits internally.
    """
    unknown = set(stats_patch) - _UPDATABLE_STATS_COLS
    if unknown:
        raise ValueError(
            f"update_run_stats received unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(_UPDATABLE_STATS_COLS)}"
        )
    if not stats_patch:
        return

    stmt = (
        update(GraphProjectionRun)
        .where(GraphProjectionRun.id == run_id)
        .values(**stats_patch)
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise LookupError(
            f"GraphProjectionRun(id={run_id}) not found — cannot update stats"
        )
    await session.flush()
    _log.debug(
        "graph_projection_runs: updated stats run_id=%s keys=%s",
        run_id, list(stats_patch.keys())
    )


async def finalize_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: GraphProjectionRunStatus,
    cost_usd: Decimal | None = None,
) -> None:
    """Set terminal status and finished_at on a graph_projection_runs row.

    State-machine contract:
    - status must be a terminal value (one of TERMINAL_STATUSES).
    - Transitions only from 'running' → terminal (WHERE status='running').
    - Idempotent for same terminal status: calling twice with the same terminal
      status is a no-op (e.g., double-finalize on retry).
    - Raises ValueError if attempting to transition to a DIFFERENT terminal status
      (e.g., succeeded → failed).

    cost_usd: if provided, updates actual_cost_usd.

    Flushes; caller commits. NEVER commits internally.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(
            f"finalize_run requires a terminal status; got {status!r}. "
            f"Terminal statuses: {sorted(TERMINAL_STATUSES)}"
        )

    values: dict = {
        "status": status,
        "finished_at": datetime.now(tz=timezone.utc),
    }
    if cost_usd is not None:
        values["actual_cost_usd"] = cost_usd

    stmt = (
        update(GraphProjectionRun)
        .where(GraphProjectionRun.id == run_id)
        .where(GraphProjectionRun.status == "running")
        .values(**values)
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        # Row may be already finalized — check for idempotency vs wrong transition
        row_result = await session.execute(
            select(GraphProjectionRun.status).where(GraphProjectionRun.id == run_id)
        )
        existing_status = row_result.scalar_one_or_none()
        if existing_status is None:
            raise LookupError(
                f"GraphProjectionRun(id={run_id}) not found — cannot finalize"
            )
        if existing_status == status:
            # Idempotent: same terminal status, no-op
            _log.debug(
                "graph_projection_runs: finalize_run id=%s already at status=%s (idempotent noop)",
                run_id, status
            )
            return
        raise ValueError(
            f"Cannot finalize run {run_id}: already {existing_status!r}, "
            f"requested {status!r}"
        )
    await session.flush()
    _log.debug(
        "graph_projection_runs: finalized run_id=%s status=%s", run_id, status
    )


async def list_recent_runs(
    session: AsyncSession,
    *,
    limit: int = 20,
) -> Sequence[GraphProjectionRun]:
    """Return the most recent graph_projection_runs rows, descending by started_at.

    limit: max rows to return (default 20).
    """
    stmt = (
        select(GraphProjectionRun)
        .order_by(GraphProjectionRun.started_at.desc(), GraphProjectionRun.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_active_run(session: AsyncSession) -> GraphProjectionRun | None:
    """Return the currently running GraphProjectionRun (status='running'), or None.

    If multiple 'running' rows exist (shouldn't happen — caller is responsible for
    not starting overlapping runs), returns the most recently started one.
    """
    stmt = (
        select(GraphProjectionRun)
        .where(GraphProjectionRun.status == "running")
        .order_by(GraphProjectionRun.started_at.desc(), GraphProjectionRun.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
