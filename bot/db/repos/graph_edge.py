"""Repository for ``graph_edges`` (Phase 10 / T10-02).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.

Predicate vocabulary is enforced here (ValueError before flush) and at the DB
layer (CHECK constraint). Confidence range [0.00, 1.00] is similarly dual-enforced.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GraphEdge
from bot.services.graph_common import ALLOWED_PREDICATES

_log = logging.getLogger(__name__)


async def create_edge(
    session: AsyncSession,
    *,
    graph_provenance_id: int,
    subject_node_key: str,
    predicate: str,
    object_node_key: str,
    edge_key: str,
    confidence_score: Decimal = Decimal("0.50"),
) -> GraphEdge:
    """Insert a new graph_edges row.

    predicate must be in ALLOWED_PREDICATES (from graph_common.py).
    confidence_score must be in [0.00, 1.00].
    Both constraints are checked before flush (ValueError) and enforced at DB level.

    Flushes; caller commits. NEVER commits internally.
    """
    if predicate not in ALLOWED_PREDICATES:
        raise ValueError(
            f"predicate {predicate!r} is not in ALLOWED_PREDICATES: {ALLOWED_PREDICATES}"
        )
    if not (Decimal("0.00") <= confidence_score <= Decimal("1.00")):
        raise ValueError(
            f"confidence_score {confidence_score!r} is out of range [0.00, 1.00]"
        )

    row = GraphEdge(
        graph_provenance_id=graph_provenance_id,
        subject_node_key=subject_node_key,
        predicate=predicate,
        object_node_key=object_node_key,
        edge_key=edge_key,
        confidence_score=confidence_score,
    )
    session.add(row)
    await session.flush()
    _log.debug(
        "graph_edges: inserted id=%s provenance_id=%s predicate=%s edge_key=%s",
        row.id,
        graph_provenance_id,
        predicate,
        edge_key,
    )
    return row


async def find_by_provenance(
    session: AsyncSession,
    provenance_id: int,
) -> Sequence[GraphEdge]:
    """Return all graph_edges rows for the given graph_provenance_id.

    Returns both active and purged rows. Ordered by id ASC for deterministic
    enumeration.
    """
    stmt = (
        select(GraphEdge)
        .where(GraphEdge.graph_provenance_id == provenance_id)
        .order_by(GraphEdge.id.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def count_for_drift_check(session: AsyncSession) -> int:
    """Return the count of active (non-purged) graph_edges rows.

    Used by T10-08 drift reconcile to compare Postgres edge count vs Neo4j edge count.
    A mismatch indicates either a failed projection or a failed purge.
    """
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(GraphEdge)
        .where(GraphEdge.purged_at.is_(None))
    )
    result = await session.execute(stmt)
    count = result.scalar_one()
    return int(count)
