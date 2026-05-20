"""Repository for ``graph_provenance`` (Phase 10 / T10-02).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.

source_table / source_pk are logical refs (not typed FKs) queried by the forget
cascade. source_table discriminates the source type; the matching FK column must
be set and the OTHER FK column must be None. Code-level XOR enforcement here
(DB CHECK is OR per §5.A spec). This ensures unambiguous forget cascade
(queries by source_table+source_pk) and raises a clear ValueError before flush.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GraphProvenance

_log = logging.getLogger(__name__)


async def create_provenance(
    session: AsyncSession,
    *,
    projection_run_id: int,
    source_table: str,
    source_pk: str,
    source_card_id: uuid.UUID | None = None,
    source_message_version_id: int | None = None,
    triple_hash: str | None = None,
    graph_store: str = "neo4j",
    graph_node_key: str | None = None,
    graph_edge_key: str | None = None,
    source_content_hash: str | None = None,
    governance_policy: str = "normal",
) -> GraphProvenance:
    """Insert a new graph_provenance row.

    source_table discriminates the source type; the matching FK column must be
    set, and the OTHER FK column must be None. Code-level XOR enforcement
    (DB CHECK is OR per §5.A spec). Raises ValueError immediately (before flush)
    on violation so the caller sees a clear error.

    source_table must be one of 'message_versions' or 'knowledge_cards'.

    Flushes; caller commits. NEVER commits internally.
    """
    _ALLOWED_SOURCE_TABLES = frozenset({"message_versions", "knowledge_cards"})
    if source_table not in _ALLOWED_SOURCE_TABLES:
        raise ValueError(
            f"source_table {source_table!r} is not allowed. "
            f"Must be one of: {sorted(_ALLOWED_SOURCE_TABLES)}"
        )

    # source_table discriminates the source type; the matching FK column must be
    # set and the OTHER FK column must be None. Code-level XOR enforcement
    # (DB CHECK is OR per §5.A spec). This prevents ambiguous rows that would
    # break forget cascade (queries by source_table+source_pk).
    if source_table == "message_versions" and source_card_id is not None:
        raise ValueError(
            "source_table='message_versions' requires source_message_version_id only; "
            f"got source_card_id={source_card_id}"
        )
    if source_table == "knowledge_cards" and source_message_version_id is not None:
        raise ValueError(
            "source_table='knowledge_cards' requires source_card_id only; "
            f"got source_message_version_id={source_message_version_id}"
        )
    if source_table == "message_versions" and source_message_version_id is None:
        raise ValueError(
            "source_table='message_versions' requires source_message_version_id to be set"
        )
    if source_table == "knowledge_cards" and source_card_id is None:
        raise ValueError(
            "source_table='knowledge_cards' requires source_card_id to be set"
        )

    # Defensive fallback: should not reach here after source_table-specific checks,
    # but guard against future source_table values or refactoring gaps.
    if source_card_id is None and source_message_version_id is None:
        raise ValueError(
            "create_provenance requires exactly one of source_card_id or "
            "source_message_version_id to be non-NULL; both are None"
        )

    row = GraphProvenance(
        projection_run_id=projection_run_id,
        source_table=source_table,
        source_pk=source_pk,
        source_card_id=source_card_id,
        source_message_version_id=source_message_version_id,
        source_content_hash=source_content_hash,
        graph_store=graph_store,
        graph_node_key=graph_node_key,
        graph_edge_key=graph_edge_key,
        triple_hash=triple_hash,
        governance_policy=governance_policy,
    )
    session.add(row)
    await session.flush()
    _log.debug(
        "graph_provenance: inserted id=%s source_table=%s source_pk=%s",
        row.id,
        source_table,
        source_pk,
    )
    return row


async def mark_inactive(
    session: AsyncSession,
    provenance_id: int,
    *,
    purge_reason: str = "forget_cascade",
) -> None:
    """Soft-delete a graph_provenance row by setting purged_at and purge_reason.

    Spec §5.F step 2: SET purged_at = now(), purge_reason = 'forget_cascade'.

    Idempotent: if already purged, silently leaves the value as-is.
    Raises LookupError if the row does not exist.

    Flushes; caller commits. NEVER commits internally.
    """
    from sqlalchemy import update

    stmt = (
        update(GraphProvenance)
        .where(GraphProvenance.id == provenance_id)
        .where(GraphProvenance.purged_at.is_(None))
        .values(
            purged_at=datetime.now(tz=timezone.utc),
            purge_reason=purge_reason,
        )
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        # Check if row exists at all (already purged = idempotent, missing = error)
        exists_result = await session.execute(
            select(GraphProvenance.id).where(GraphProvenance.id == provenance_id)
        )
        if exists_result.scalar_one_or_none() is None:
            raise LookupError(
                f"GraphProvenance(id={provenance_id}) not found — cannot mark inactive"
            )
        # Row exists but purged_at is already set: idempotent no-op
        _log.debug(
            "graph_provenance: mark_inactive id=%s already purged (idempotent noop)",
            provenance_id,
        )
        return
    await session.flush()
    _log.debug(
        "graph_provenance: marked inactive id=%s purge_reason=%s",
        provenance_id,
        purge_reason,
    )


async def find_by_source(
    session: AsyncSession,
    *,
    source_table: str,
    source_pk: str,
) -> Sequence[GraphProvenance]:
    """Return all graph_provenance rows for the given source (active + purged).

    Used by forget cascade to find all provenance rows to soft-delete before
    enqueueing graph_purge_pending. Returns both active and already-purged rows
    so the caller can decide.

    Ordered by id ASC for deterministic enumeration.
    """
    stmt = (
        select(GraphProvenance)
        .where(GraphProvenance.source_table == source_table)
        .where(GraphProvenance.source_pk == source_pk)
        .order_by(GraphProvenance.id.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def find_active(
    session: AsyncSession,
    *,
    projection_run_id: int | None = None,
) -> Sequence[GraphProvenance]:
    """Return active (non-purged) graph_provenance rows.

    projection_run_id: if provided, filter to rows from that run only.

    Ordered by id ASC for deterministic enumeration.
    """
    stmt = select(GraphProvenance).where(GraphProvenance.purged_at.is_(None))
    if projection_run_id is not None:
        stmt = stmt.where(GraphProvenance.projection_run_id == projection_run_id)
    stmt = stmt.order_by(GraphProvenance.id.asc())
    result = await session.execute(stmt)
    return result.scalars().all()
