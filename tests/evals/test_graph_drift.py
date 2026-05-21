"""G2 binding test — drift detection (T10-08 scaffold, finalized in T10-09).

Spec: PHASE10_PLAN.md §5.I + G2 acceptance criterion at line ~1301.

G2: Insert a Neo4j node directly (no Postgres provenance row) → call
    reconcile_counts() → assert drift_detected=True AND
    postgres_active_hash != neo4j_edge_hash.

This file is SCAFFOLDED in T10-08 and FINALIZED in T10-09 (when the real Neo4j
test container integration is available via pytest-testcontainers).

Tests currently marked @pytest.mark.neo4j are skipped unless NEO4J_BOLT_URI
is set and points to a live Neo4j instance. All unit-level drift tests live in
tests/services/test_graph_query_drift.py.
"""

from __future__ import annotations

import pytest

# Mark all tests in this file as neo4j integration tests.
# They are skipped in standard CI and run only in the neo4j CI job.
pytestmark = pytest.mark.neo4j


@pytest.mark.asyncio
async def test_reconcile_counts_detects_orphan_neo4j_node():
    """G2: Direct Cypher insert with no Postgres provenance → drift_detected=True.

    This is the canonical G2 binding test. It requires a live Neo4j instance.

    Steps:
    1. Start from a clean state (empty Postgres graph tables, empty Neo4j).
    2. Insert a node into Neo4j directly via Cypher MERGE — no Postgres provenance row.
    3. Call reconcile_counts().
    4. Assert drift_detected=True.
    5. Assert postgres_active_hash != neo4j_edge_hash (or one is None while other is non-zero).

    Finalized in T10-09 with pytest-testcontainers Neo4j fixture.
    """
    pytest.skip(
        "G2: Requires live Neo4j instance via pytest-testcontainers. "
        "Finalized in T10-09. Scaffold committed in T10-08."
    )


@pytest.mark.asyncio
async def test_reconcile_counts_clean_state_no_drift():
    """G2 baseline: clean state → drift_detected=False.

    Steps:
    1. Start from a clean state.
    2. Project a set of triples via graph_projector (Postgres + Neo4j in sync).
    3. Call reconcile_counts().
    4. Assert drift_detected=False.
    5. Assert postgres_active_hash == neo4j_edge_hash.

    Finalized in T10-09.
    """
    pytest.skip(
        "G2 baseline: Requires live Neo4j instance. "
        "Finalized in T10-09. Scaffold committed in T10-08."
    )
