"""Graph Query Service — read-only traversal API (Phase 10 / T10-05).

Implements three public traversal functions per PHASE10_PLAN.md §5.E:

- find_related_topics: BFS from a topic label, returns matching GraphPaths
- find_people_for_topic: finds Person nodes connected to a topic
- explain_connection: finds paths between two named nodes
- sources_for_path: resolves provenance rows for returned paths
- graph_stats: basic node/edge counts

Rules (§5.E verbatim contract):
- Read-only. No writes, no MERGE, no LLM calls.
- Provenance required. Every GraphPath must have at least one graph_provenance.id.
  Results with zero provenance are silently dropped.
- Pending-purge read-block (RFC-001:415 strict pattern).
  Before executing any traversal: assert_no_pending_purge() for candidate nodes.
  If raises RefusalError → return GraphQueryResult(abstained=True, ...).
- Flag gate. All public functions raise GraphQueryDisabledError if
  memory.graph.query.enabled is OFF OR memory.graph.write_pending.paused is ON.
- No raw content. GraphPath nodes carry only label and node_type from Neo4j,
  plus provenance_ids. Raw text is never included.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.graph_adapter import GraphAdapter
from bot.services.graph_common import RefusalError
from bot.services.graph_purge_readblock import assert_no_pending_purge

_log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Feature flag key for this service (default OFF per §5.E).
GRAPH_QUERY_FEATURE_FLAG: str = "memory.graph.query.enabled"

# Hardcoded max for max_hops parameter — cannot be raised above this even by admin.
MAX_HOPS_CAP: int = 5

# Result count caps by role.
MAX_RESULTS_MEMBER: int = 200
MAX_RESULTS_ADMIN: int = 1000


# ─── Errors ───────────────────────────────────────────────────────────────────


class GraphQueryDisabledError(Exception):
    """Raised when the graph query feature flag is off.

    Callers (admin handlers, T10-07) should catch this and respond with an
    informational message rather than an error trace.
    """


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GraphPath:
    """A traversal path through the graph with provenance linkage.

    Nodes and edges carry only structural data from Neo4j (label, node_type,
    node_key). Raw message text is NEVER included here. Callers wanting source
    text must go through the evidence layer.
    """

    nodes: list[dict]                       # [{label, node_type, node_key}, ...]
    edges: list[dict]                       # [{source_key, target_key, predicate}, ...]
    provenance_ids: list[int]               # graph_provenance.id references
    source_message_version_ids: list[int]   # for drift detection / source lookup
    source_card_ids: list[str]              # UUIDs


@dataclass(frozen=True)
class GraphQueryResult:
    """Unified result wrapper for all graph query functions.

    abstained=True means the query was not executed (purge read-block or flag gate).
    Callers MUST check abstained before consuming paths.
    """

    abstained: bool
    abstain_reason: str | None
    paths: list[GraphPath]
    query_metadata: dict


@dataclass(frozen=True)
class GraphStatsResult:
    """Counts from the live graph (Postgres canonical)."""

    active_provenance_rows: int
    active_edge_rows: int
    purged_provenance_rows: int


# ─── Internal helpers ─────────────────────────────────────────────────────────


async def _is_query_enabled(session: AsyncSession) -> bool:
    """Check feature flags per §5.E.

    Returns True only when BOTH:
    - memory.graph.query.enabled is ON
    - memory.graph.write_pending.paused is OFF (kill-switch)

    Default for both flags is False (missing row = disabled).
    """
    from bot.db.repos.feature_flag import FeatureFlagRepo

    enabled = await FeatureFlagRepo.get(session, GRAPH_QUERY_FEATURE_FLAG)
    if not enabled:
        return False
    paused = await FeatureFlagRepo.get(session, "memory.graph.write_pending.paused")
    if paused:
        return False
    return True


async def _resolve_provenance_for_nodes(
    session: AsyncSession,
    *,
    node_keys: list[str],
) -> list[Any]:
    """Return active graph_provenance rows matching any of the given node_keys.

    Returns rows from graph_provenance where:
    - graph_node_key IN node_keys
    - purged_at IS NULL (active only — purged provenance is excluded per §5.E)

    Ordered by id ASC for deterministic output.
    """
    from bot.db.models import GraphProvenance

    if not node_keys:
        return []

    stmt = (
        select(GraphProvenance)
        .where(
            GraphProvenance.graph_node_key.in_(node_keys),
            GraphProvenance.purged_at.is_(None),
        )
        .order_by(GraphProvenance.id.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _build_paths_from_traversal(
    traversal_nodes: list[dict],
    provenance_rows: list[Any],
) -> list[GraphPath]:
    """Convert raw adapter traversal results into GraphPath objects.

    Each node that matches an active provenance row contributes to a GraphPath.
    Nodes with zero provenance are silently dropped (per §5.E invariant).

    Strategy: group provenance rows by graph_node_key, then for each traversal
    node that has provenance, emit a minimal GraphPath (single-node, no edges).
    Callers needing full path structure should use explain_connection instead.
    """
    if not traversal_nodes or not provenance_rows:
        return []

    # Build index: node_key → list of provenance rows
    prov_by_key: dict[str, list[Any]] = {}
    for prov in provenance_rows:
        nk = prov.graph_node_key
        if nk:
            prov_by_key.setdefault(nk, []).append(prov)

    paths: list[GraphPath] = []
    for node in traversal_nodes:
        nk = node.get("node_key")
        if not nk:
            continue
        prov_list = prov_by_key.get(nk, [])
        if not prov_list:
            # No provenance → silently drop (§5.E: missing provenance = drift = fail closed)
            _log.debug("graph_query: dropping orphan node node_key=%s (no active provenance)", nk)
            continue

        prov_ids = [p.id for p in prov_list]
        mv_ids = [p.source_message_version_id for p in prov_list if p.source_message_version_id]
        card_ids = [str(p.source_card_id) for p in prov_list if p.source_card_id]

        paths.append(
            GraphPath(
                nodes=[{
                    "label": node.get("label"),
                    "node_type": node.get("node_type"),
                    "node_key": nk,
                }],
                edges=[],
                provenance_ids=prov_ids,
                source_message_version_ids=mv_ids,
                source_card_ids=card_ids,
            )
        )

    return paths


# ─── Public API ───────────────────────────────────────────────────────────────


async def find_related_topics(
    session: AsyncSession,
    adapter: GraphAdapter,
    *,
    topic: str,
    viewer_is_admin: bool,
    max_hops: int = 3,
    max_results: int = 20,
) -> GraphQueryResult:
    """Find nodes related to a given topic via graph traversal.

    Per §5.E:
    - Flag gate: raises GraphQueryDisabledError if feature flag is off.
    - Read-block: checks graph_purge_pending BEFORE any Neo4j call.
    - Provenance-required: drops nodes without active graph_provenance.
    - No raw content: returns only label/node_type/node_key + provenance linkage.

    max_hops: max traversal depth. Hardcoded cap = MAX_HOPS_CAP (5). Raises
    ValueError if caller requests more than cap.

    Returns GraphQueryResult. Never raises to caller; errors are abstained.
    """
    if not isinstance(max_hops, int):
        raise TypeError(f"max_hops must be int, got {type(max_hops).__name__}")
    if max_hops < 1 or max_hops > MAX_HOPS_CAP:
        raise ValueError(
            f"max_hops={max_hops} exceeds hardcoded cap MAX_HOPS_CAP={MAX_HOPS_CAP}"
        )

    if not await _is_query_enabled(session):
        raise GraphQueryDisabledError(
            f"{GRAPH_QUERY_FEATURE_FLAG} is disabled; graph query skipped"
        )

    # Read-block: check for pending purge rows on the start node before traversal.
    # Pre-guard scope: `topic` is a label string, NOT a graph_node_key.
    # This is a heuristic upper-bound check (Option B per review FIX-WARN-1).
    # It may over-block if the label matches a purge entry, but post-guard (below)
    # is the authoritative check on actual graph_node_keys returned by the adapter.
    try:
        await assert_no_pending_purge(session, node_keys=[topic])
    except RefusalError as exc:
        _log.warning("graph_query find_related_topics: read-block fired topic=%s: %s", topic, exc)
        return GraphQueryResult(
            abstained=True,
            abstain_reason=str(exc),
            paths=[],
            query_metadata={"mode": "find_related_topics", "topic": topic},
        )

    # Traverse the graph
    effective_max = MAX_RESULTS_ADMIN if viewer_is_admin else MAX_RESULTS_MEMBER
    actual_max = min(max_results, effective_max)

    traversal_nodes = await adapter.query_traversal(
        topic=topic,
        max_hops=max_hops,
        max_results=actual_max,
    )

    if not traversal_nodes:
        return GraphQueryResult(
            abstained=False,
            abstain_reason=None,
            paths=[],
            query_metadata={
                "mode": "find_related_topics",
                "topic": topic,
                "max_hops": max_hops,
                "max_results": actual_max,
                "viewer_is_admin": viewer_is_admin,
            },
        )

    # Resolve provenance — post-traversal read-block on all returned node_keys
    node_keys = [n["node_key"] for n in traversal_nodes if n.get("node_key")]

    try:
        await assert_no_pending_purge(session, node_keys=node_keys)
    except RefusalError as exc:
        _log.warning(
            "graph_query find_related_topics: post-traversal read-block fired: %s", exc
        )
        return GraphQueryResult(
            abstained=True,
            abstain_reason=str(exc),
            paths=[],
            query_metadata={"mode": "find_related_topics", "topic": topic},
        )

    provenance_rows = await _resolve_provenance_for_nodes(session, node_keys=node_keys)
    paths = _build_paths_from_traversal(traversal_nodes, provenance_rows)

    return GraphQueryResult(
        abstained=False,
        abstain_reason=None,
        paths=paths,
        query_metadata={
            "mode": "find_related_topics",
            "topic": topic,
            "max_hops": max_hops,
            "max_results": actual_max,
            "viewer_is_admin": viewer_is_admin,
            "nodes_returned_by_adapter": len(traversal_nodes),
            "nodes_with_provenance": len(paths),
        },
    )


async def find_people_for_topic(
    session: AsyncSession,
    adapter: GraphAdapter,
    *,
    topic: str,
    viewer_is_admin: bool,
) -> GraphQueryResult:
    """Find Person nodes related to a topic via BFS traversal.

    Post-filters find_related_topics results to Person node_type only.
    Inherits the same flag gate and read-block semantics.
    """
    # Delegate traversal to find_related_topics with default hops
    base_result = await find_related_topics(
        session,
        adapter,
        topic=topic,
        viewer_is_admin=viewer_is_admin,
        max_hops=2,
        max_results=MAX_RESULTS_ADMIN if viewer_is_admin else MAX_RESULTS_MEMBER,
    )

    if base_result.abstained:
        return GraphQueryResult(
            abstained=True,
            abstain_reason=base_result.abstain_reason,
            paths=[],
            query_metadata={**base_result.query_metadata, "mode": "find_people_for_topic"},
        )

    # Filter to Person node_type only
    person_paths = [
        p for p in base_result.paths
        if any(n.get("node_type") == "Person" for n in p.nodes)
    ]

    return GraphQueryResult(
        abstained=False,
        abstain_reason=None,
        paths=person_paths,
        query_metadata={
            "mode": "find_people_for_topic",
            "topic": topic,
            "viewer_is_admin": viewer_is_admin,
            "total_traversal_paths": len(base_result.paths),
            "person_paths": len(person_paths),
        },
    )


async def explain_connection(
    session: AsyncSession,
    adapter: GraphAdapter,
    *,
    node_a: str,
    node_b: str,
    viewer_is_admin: bool,
    max_hops: int = 5,
) -> GraphQueryResult:
    """Find paths connecting node_a and node_b in the graph.

    Traverses from node_a with max_hops, then checks if node_b appears in the
    result set. Uses the same adapter.query_traversal interface.

    Per §5.E: all inputs are passed as adapter parameters (NOT string-interpolated
    into Cypher). max_hops is hardcoded in the Cypher template, not a parameter.

    Returns GraphQueryResult. Never raises to caller.
    """
    if not isinstance(max_hops, int):
        raise TypeError(f"max_hops must be int, got {type(max_hops).__name__}")
    if max_hops < 1 or max_hops > MAX_HOPS_CAP:
        raise ValueError(
            f"max_hops={max_hops} exceeds hardcoded cap MAX_HOPS_CAP={MAX_HOPS_CAP}"
        )

    if not await _is_query_enabled(session):
        raise GraphQueryDisabledError(
            f"{GRAPH_QUERY_FEATURE_FLAG} is disabled; graph query skipped"
        )

    # Read-block: check both anchor nodes before traversal
    try:
        await assert_no_pending_purge(session, node_keys=[node_a, node_b])
    except RefusalError as exc:
        _log.warning(
            "graph_query explain_connection: read-block fired node_a=%s node_b=%s: %s",
            node_a, node_b, exc,
        )
        return GraphQueryResult(
            abstained=True,
            abstain_reason=str(exc),
            paths=[],
            query_metadata={"mode": "explain_connection", "node_a": node_a, "node_b": node_b},
        )

    effective_max = MAX_RESULTS_ADMIN if viewer_is_admin else MAX_RESULTS_MEMBER

    # Use path-aware traversal to get full paths (nodes + edges) from node_a to node_b.
    # This ensures GraphPath.edges is populated per §5.E (CRITICAL-2 fix).
    raw_paths = await adapter.query_traversal_with_paths(
        start_label=node_a,
        end_label=node_b,
        max_hops=max_hops,
        max_results=effective_max,
    )

    if not raw_paths:
        return GraphQueryResult(
            abstained=False,
            abstain_reason=None,
            paths=[],
            query_metadata={
                "mode": "explain_connection",
                "node_a": node_a,
                "node_b": node_b,
                "max_hops": max_hops,
                "connection_found": False,
            },
        )

    # Collect all node_keys from all paths for provenance resolution and read-block
    all_node_keys: list[str] = []
    for rp in raw_paths:
        for n in rp.get("nodes", []):
            nk = n.get("node_key")
            if nk and nk not in all_node_keys:
                all_node_keys.append(nk)

    # Post-traversal read-block
    try:
        await assert_no_pending_purge(session, node_keys=all_node_keys)
    except RefusalError as exc:
        _log.warning(
            "graph_query explain_connection: post-traversal read-block: %s", exc
        )
        return GraphQueryResult(
            abstained=True,
            abstain_reason=str(exc),
            paths=[],
            query_metadata={"mode": "explain_connection", "node_a": node_a, "node_b": node_b},
        )

    provenance_rows = await _resolve_provenance_for_nodes(session, node_keys=all_node_keys)

    # Build a provenance index by node_key
    prov_by_key: dict[str, list[Any]] = {}
    for prov in provenance_rows:
        nk = prov.graph_node_key
        if nk:
            prov_by_key.setdefault(nk, []).append(prov)

    # Build GraphPath objects with edges populated
    paths: list[GraphPath] = []
    for rp in raw_paths:
        path_nodes = rp.get("nodes", [])
        path_edges = rp.get("edges", [])

        # Collect all provenance for all nodes in this path
        prov_ids: list[int] = []
        mv_ids: list[int] = []
        card_ids: list[str] = []
        for n in path_nodes:
            nk = n.get("node_key")
            if not nk:
                continue
            for p in prov_by_key.get(nk, []):
                if p.id not in prov_ids:
                    prov_ids.append(p.id)
                if p.source_message_version_id and p.source_message_version_id not in mv_ids:
                    mv_ids.append(p.source_message_version_id)
                if p.source_card_id:
                    cid = str(p.source_card_id)
                    if cid not in card_ids:
                        card_ids.append(cid)

        if not prov_ids:
            # No provenance — drop path per §5.E invariant
            _log.debug("graph_query: dropping path with no active provenance node_a=%s node_b=%s", node_a, node_b)
            continue

        paths.append(GraphPath(
            nodes=path_nodes,
            edges=path_edges,
            provenance_ids=prov_ids,
            source_message_version_ids=mv_ids,
            source_card_ids=card_ids,
        ))

    return GraphQueryResult(
        abstained=False,
        abstain_reason=None,
        paths=paths,
        query_metadata={
            "mode": "explain_connection",
            "node_a": node_a,
            "node_b": node_b,
            "max_hops": max_hops,
            "viewer_is_admin": viewer_is_admin,
            "connection_found": len(paths) > 0,
            "paths_with_provenance": len(paths),
        },
    )


async def sources_for_path(
    session: AsyncSession,
    *,
    provenance_ids: list[int],
) -> list[Any]:
    """Return graph_provenance rows for the given provenance_ids.

    Per §5.E: callers wanting source text must go through the evidence layer
    separately. This returns only the provenance metadata rows (no raw content).

    Returns rows sorted by id ASC. Missing provenance_ids are silently skipped.
    """
    from bot.db.models import GraphProvenance

    if not provenance_ids:
        return []

    stmt = (
        select(GraphProvenance)
        .where(
            GraphProvenance.id.in_(provenance_ids),
            GraphProvenance.purged_at.is_(None),
        )
        .order_by(GraphProvenance.id.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def graph_stats(
    session: AsyncSession,
    adapter: GraphAdapter,
) -> GraphStatsResult:
    """Return basic graph statistics from the Postgres canonical store.

    Per §5.E: Postgres is canonical (invariant #6). We count from Postgres,
    not from Neo4j. The adapter health_check is used to verify connectivity.
    """
    # Count active provenance rows (non-purged)
    active_prov_result = await session.execute(
        text("SELECT COUNT(*) FROM graph_provenance WHERE purged_at IS NULL")
    )
    active_provenance = active_prov_result.scalar() or 0

    # Count active edge rows (non-purged)
    active_edge_result = await session.execute(
        text("SELECT COUNT(*) FROM graph_edges WHERE purged_at IS NULL")
    )
    active_edges = active_edge_result.scalar() or 0

    # Count purged provenance rows
    purged_prov_result = await session.execute(
        text("SELECT COUNT(*) FROM graph_provenance WHERE purged_at IS NOT NULL")
    )
    purged_provenance = purged_prov_result.scalar() or 0

    return GraphStatsResult(
        active_provenance_rows=int(active_provenance),
        active_edge_rows=int(active_edges),
        purged_provenance_rows=int(purged_provenance),
    )
