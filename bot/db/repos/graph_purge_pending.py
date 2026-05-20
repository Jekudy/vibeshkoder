"""Repository for ``graph_purge_pending`` (Phase 10 / T10-06).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.

Implements the async purge queue for Neo4j bolt DELETE rows per RFC-001:415.
claim_batch uses SELECT ... FOR UPDATE SKIP LOCKED to prevent multi-worker
double-claim.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GraphPurgePending

_log = logging.getLogger(__name__)

# Max retries before a row is moved to the DLQ (failed_at IS NOT NULL).
MAX_RETRIES = 5

# Stale claim timeout: rows stuck in the queue longer than this (purged_at IS NULL,
# failed_at IS NULL, enqueued_at < now() - stale_minutes) are reset to retry on
# the next claim_batch call. In practice these appear when a worker crashes mid-flight.
STALE_CLAIM_MINUTES = 5


async def enqueue(
    session: AsyncSession,
    *,
    forget_event_id: int,
    source_table: str,
    source_pk: str,
    graph_node_key: str | None = None,
    graph_edge_key: str | None = None,
    graph_provenance_id: int | None = None,
) -> GraphPurgePending:
    """Insert a new graph_purge_pending row.

    Idempotent via ON CONFLICT DO NOTHING on
    (forget_event_id, source_table, source_pk, graph_provenance_id).

    CRITICAL-1 (T10-06): the unique key includes graph_provenance_id so that
    multiple graph_provenance rows for the same (source_table, source_pk) each
    receive their own purge_pending row. The old constraint
    (forget_event_id, source_table, source_pk) collapsed all but the first row,
    leaving the other Neo4j nodes alive after cascade.

    Returns the row just inserted (or the existing row on conflict).

    Flushes; caller commits.
    """
    _ALLOWED_SOURCE_TABLES = frozenset(
        {"message_versions", "knowledge_cards", "card_sources"}
    )
    if source_table not in _ALLOWED_SOURCE_TABLES:
        raise ValueError(
            f"source_table {source_table!r} not allowed; "
            f"must be one of {sorted(_ALLOWED_SOURCE_TABLES)}"
        )

    # Use PostgreSQL INSERT ... ON CONFLICT DO NOTHING for idempotency.
    # On SQLite (tests), fall back to a plain INSERT (no ON CONFLICT syntax).
    dialect_name = session.bind.dialect.name if session.bind is not None else "sqlite"

    if dialect_name == "postgresql":
        stmt = (
            pg_insert(GraphPurgePending)
            .values(
                forget_event_id=forget_event_id,
                source_table=source_table,
                source_pk=source_pk,
                graph_node_key=graph_node_key,
                graph_edge_key=graph_edge_key,
                graph_provenance_id=graph_provenance_id,
            )
            .on_conflict_do_nothing(
                # CRITICAL-1 fix: include graph_provenance_id so each provenance row
                # gets its own purge_pending entry (migration 065 constraint name).
                constraint="uq_graph_purge_pending_event_source_prov"
            )
        )
        await session.execute(stmt)
        await session.flush()
        # Re-fetch the row (either just inserted or existing on conflict).
        row = await session.scalar(
            select(GraphPurgePending).where(
                GraphPurgePending.forget_event_id == forget_event_id,
                GraphPurgePending.source_table == source_table,
                GraphPurgePending.source_pk == source_pk,
                GraphPurgePending.graph_provenance_id == graph_provenance_id
                if graph_provenance_id is not None
                else GraphPurgePending.graph_provenance_id.is_(None),
            )
        )
        if row is None:
            raise RuntimeError(
                f"enqueue: row not found after INSERT ON CONFLICT DO NOTHING "
                f"for forget_event_id={forget_event_id} source_table={source_table} "
                f"source_pk={source_pk} graph_provenance_id={graph_provenance_id}"
            )
    else:
        # SQLite path: plain insert.
        row = GraphPurgePending(
            forget_event_id=forget_event_id,
            source_table=source_table,
            source_pk=source_pk,
            graph_node_key=graph_node_key,
            graph_edge_key=graph_edge_key,
            graph_provenance_id=graph_provenance_id,
        )
        session.add(row)
        await session.flush()

    _log.debug(
        "graph_purge_pending: enqueued id=%s forget_event_id=%s source_table=%s source_pk=%s prov_id=%s",
        row.id,
        forget_event_id,
        source_table,
        source_pk,
        graph_provenance_id,
    )
    return row


async def claim_batch(
    session: AsyncSession,
    *,
    batch_size: int = 20,
) -> Sequence[GraphPurgePending]:
    """Claim up to batch_size pending rows for processing.

    Uses SELECT ... FOR UPDATE SKIP LOCKED on PostgreSQL to prevent
    multi-worker double-claim. Returns pending rows (purged_at IS NULL,
    failed_at IS NULL, ordered by enqueued_at ASC).

    SQLite fallback (tests): plain SELECT without FOR UPDATE SKIP LOCKED.

    Note on stale-row recovery: there is no separate ``claimed_at`` column in
    this table (migration 063). FOR UPDATE SKIP LOCKED implicitly handles
    worker crashes — locks are released on connection drop, so in-flight rows
    become available to other workers on reconnect. No explicit stale-reset
    UPDATE is needed.
    """
    dialect_name = session.bind.dialect.name if session.bind is not None else "sqlite"

    stmt = (
        select(GraphPurgePending)
        .where(
            GraphPurgePending.purged_at.is_(None),
            GraphPurgePending.failed_at.is_(None),
        )
        .order_by(GraphPurgePending.enqueued_at.asc())
        .limit(batch_size)
    )
    if dialect_name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    _log.debug("graph_purge_pending: claimed %d rows", len(rows))
    return rows


def sa_interval(minutes: int):
    """Return a SQLAlchemy interval expression for PostgreSQL."""
    from sqlalchemy import text

    return text(f"INTERVAL '{minutes} minutes'")


async def mark_purged(
    session: AsyncSession,
    row_id: int,
) -> None:
    """Mark a graph_purge_pending row as successfully purged.

    Idempotent: if already purged, silently no-ops.
    Raises LookupError if the row does not exist.

    Flushes; caller commits.
    """
    stmt = (
        update(GraphPurgePending)
        .where(GraphPurgePending.id == row_id)
        .where(GraphPurgePending.purged_at.is_(None))
        .values(purged_at=datetime.now(tz=timezone.utc))
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        exists = await session.scalar(
            select(GraphPurgePending.id).where(GraphPurgePending.id == row_id)
        )
        if exists is None:
            raise LookupError(
                f"GraphPurgePending(id={row_id}) not found — cannot mark purged"
            )
        # Already purged — idempotent no-op.
        _log.debug("graph_purge_pending: mark_purged id=%s already purged (noop)", row_id)
        return
    await session.flush()
    _log.debug("graph_purge_pending: marked purged id=%s", row_id)


async def mark_failed(
    session: AsyncSession,
    row_id: int,
    *,
    error_msg: str,
) -> None:
    """Mark a graph_purge_pending row as failed.

    Increments retry_count. Sets failed_at only when retry_count reaches MAX_RETRIES.
    Before MAX_RETRIES, the row stays in the pending queue for retry.

    Raises LookupError if the row does not exist.

    Flushes; caller commits.
    """
    row = await session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row_id)
    )
    if row is None:
        raise LookupError(
            f"GraphPurgePending(id={row_id}) not found — cannot mark failed"
        )

    row.retry_count = (row.retry_count or 0) + 1
    row.error = error_msg[:500]  # cap error message length

    if row.retry_count >= MAX_RETRIES:
        row.failed_at = datetime.now(tz=timezone.utc)
        _log.error(
            "graph_purge_pending: DLQ id=%s after %d retries error=%s",
            row_id,
            row.retry_count,
            error_msg[:200],
            extra={"graph_purge_dlq": True},
        )
    else:
        _log.warning(
            "graph_purge_pending: retry %d/%d id=%s error=%s",
            row.retry_count,
            MAX_RETRIES,
            row_id,
            error_msg[:200],
        )

    await session.flush()


async def count_active(session: AsyncSession) -> dict[str, int]:
    """Return counts of pending, failed (DLQ), and total rows.

    Used by admin stats and health checks.
    """
    from sqlalchemy import func

    pending_count = await session.scalar(
        select(func.count()).select_from(GraphPurgePending).where(
            GraphPurgePending.purged_at.is_(None),
            GraphPurgePending.failed_at.is_(None),
        )
    )
    failed_count = await session.scalar(
        select(func.count()).select_from(GraphPurgePending).where(
            GraphPurgePending.failed_at.is_not(None),
        )
    )
    total_count = await session.scalar(
        select(func.count()).select_from(GraphPurgePending)
    )
    return {
        "pending": pending_count or 0,
        "failed_dlq": failed_count or 0,
        "total": total_count or 0,
    }
