"""Pending-purge read-block helpers for Phase 10 (T10-06).

Implements the RFC-001:415 fail-closed read-block pattern for graph queries.
Before any Neo4j traversal, callers MUST invoke these helpers to assert no
pending purge rows exist for the candidate node keys.

If any pending rows exist (purged_at IS NULL), raises RefusalError — the caller
MUST return abstained=True rather than executing a Cypher query.

This module is owned by Stream Privacy (T10-06).
T10-05 (graph_query.py — future) will import assert_no_pending_purge.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GraphPurgePending
from bot.services.graph_common import RefusalError

_log = logging.getLogger(__name__)


async def assert_no_pending_purge(
    session: AsyncSession,
    *,
    node_keys: list[str],
) -> None:
    """Assert no pending purge rows exist for any of the given node_keys.

    Queries graph_purge_pending for any row where graph_node_key is in the
    given list AND purged_at IS NULL (pending or failed but not yet purged).

    Raises RefusalError if any match — fails CLOSED per RFC-001:415.
    Callers must return abstained=True when this raises.

    node_keys: list of graph_node_key values from the candidate traversal result.

    Invariant #9 binding: this check enforces the fail-closed window while the
    async Neo4j purge is in progress. Even after failed_at is set on a row, the
    row's purged_at is still NULL — so the read-block continues until the admin
    manually clears the DLQ or a successful retry sets purged_at.
    """
    if not node_keys:
        return

    row = await session.scalar(
        select(GraphPurgePending.id)
        .where(
            GraphPurgePending.graph_node_key.in_(node_keys),
            GraphPurgePending.purged_at.is_(None),
        )
        .limit(1)
    )
    if row is not None:
        _log.warning(
            "graph_purge_readblock: pending purge row id=%s found for node_keys=%s — "
            "returning abstained (RFC-001:415 fail-closed)",
            row,
            node_keys[:3],  # log first 3 only to avoid log flooding
        )
        raise RefusalError(
            f"Pending graph purge exists for node_keys (first match id={row}). "
            "Returning abstained=True per RFC-001:415 fail-closed contract."
        )


async def assert_no_pending_purge_for_source(
    session: AsyncSession,
    *,
    source_table: str,
    source_pk: str,
) -> None:
    """Assert no pending purge rows exist for the given (source_table, source_pk).

    Sister helper for assert_no_pending_purge — queries by source identity
    rather than graph_node_key. Useful when the caller knows the source row
    but hasn't yet resolved its node_keys.

    Raises RefusalError if any pending row matches — fails CLOSED per RFC-001:415.
    """
    row = await session.scalar(
        select(GraphPurgePending.id)
        .where(
            GraphPurgePending.source_table == source_table,
            GraphPurgePending.source_pk == source_pk,
            GraphPurgePending.purged_at.is_(None),
        )
        .limit(1)
    )
    if row is not None:
        _log.warning(
            "graph_purge_readblock: pending purge row id=%s found for "
            "source_table=%s source_pk=%s — returning abstained (RFC-001:415)",
            row,
            source_table,
            source_pk,
        )
        raise RefusalError(
            f"Pending graph purge exists for source_table={source_table!r} "
            f"source_pk={source_pk!r} (first match id={row}). "
            "Returning abstained=True per RFC-001:415 fail-closed contract."
        )
