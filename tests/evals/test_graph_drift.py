"""G2 binding test — drift detection (T10-08 scaffold, finalized in T10-09).

Spec: PHASE10_PLAN.md §5.I + G2 acceptance criterion at line ~1301.

G2a: Normal state (all provenance matched) → drift_detected=False.
     After project with matching Postgres+Neo4j state, reconcile_counts reports no drift.
G2b: Drift simulation — manually insert orphan NetworkX node (no Postgres provenance)
     → reconcile_counts detects drift_detected=True AND drift_orphan_node_count >= 1.

The original T10-08 scaffold tests (both previously skipped) are finalized here
using NetworkXAdapter — no live Neo4j required for unit-level binding.

For real Neo4j integration tests: mark with @pytest.mark.graph_integration and
set NEO4J_BOLT_URI in the CI environment.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from bot.services.graph_adapter import NetworkXAdapter

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=74_000_000)


def _next_id() -> int:
    return next(_counter)


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_message_version(db_session) -> tuple[int, int]:
    """Create a ChatMessage + MessageVersion. Returns (cm_id, mv_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text="drift test",
        date=when,
        raw_json={"text": "drift test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="drift test",
        normalized_text="drift test",
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()

    msg.current_version_id = ver.id
    await db_session.flush()
    return msg.id, ver.id


async def _make_projection_run(db_session) -> int:
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(db_session, mode="incremental", started_by="test")
    await db_session.flush()
    return run.id


async def _make_provenance(
    db_session,
    *,
    run_id: int,
    source_table: str = "message_versions",
    source_pk: str,
    source_message_version_id: int | None = None,
    graph_node_key: str,
) -> int:
    from bot.db.models import GraphProvenance

    prov = GraphProvenance(
        projection_run_id=run_id,
        source_table=source_table,
        source_pk=source_pk,
        source_message_version_id=source_message_version_id,
        graph_node_key=graph_node_key,
        triple_hash=_next_id(),
        governance_policy="normal",
    )
    db_session.add(prov)
    await db_session.flush()
    return prov.id


async def _make_graph_edge(
    db_session,
    *,
    provenance_id: int,
    subject_node_key: str,
    object_node_key: str,
    predicate: str = "RELATED_TO",
) -> int:
    from bot.db.models import GraphEdge

    edge = GraphEdge(
        graph_provenance_id=provenance_id,
        subject_node_key=subject_node_key,
        object_node_key=object_node_key,
        predicate=predicate,
        edge_key=f"ek-{_next_id()}",
        confidence_score=0.9,
    )
    db_session.add(edge)
    await db_session.flush()
    return edge.id


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestGraphDrift:
    async def test_reconcile_counts_clean_state_no_drift(self, db_session) -> None:
        """G2a baseline: matching Postgres + NetworkX state → drift_detected=False.

        Setup:
        1. Seed N graph_provenance rows in Postgres.
        2. Seed N nodes + M edges in NetworkXAdapter to match.
        3. Call reconcile_counts().
        4. Assert drift_detected=False.

        reconcile_counts compares (postgres_provenance, postgres_edges) vs
        (neo4j_nodes, neo4j_edges). We seed 1 provenance + 0 edges in Postgres
        and 1 node + 0 edges in adapter so counts match exactly.
        """
        from bot.services.graph_query import reconcile_counts

        _cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-g2a-{mv_id}"

        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        # Seed adapter with exactly 1 node, 0 edges — matches 1 provenance, 0 edges in Postgres.
        adapter = NetworkXAdapter()
        await adapter.merge_node(
            node_key=node_key,
            labels=["MemoryNode"],
            properties={"label": node_key, "provenance_id": str(prov_id)},
        )

        report = await reconcile_counts(db_session, adapter)

        assert report.drift_detected is False, (
            f"G2a: drift_detected=True on matching state. "
            f"postgres_provenance={report.postgres_active_provenance}, "
            f"neo4j_nodes={report.neo4j_node_count}, "
            f"postgres_edges={report.postgres_active_edges}, "
            f"neo4j_edges={report.neo4j_edge_count}"
        )

    async def test_reconcile_counts_detects_orphan_neo4j_node(self, db_session) -> None:
        """G2b: Direct insert of orphan NetworkX node with no Postgres provenance → drift_detected=True.

        Steps:
        1. Start from a clean test state (new adapter, Postgres state from fixture).
        2. Insert a node into NetworkXAdapter directly — no Postgres provenance row.
        3. Call reconcile_counts().
        4. Assert drift_detected=True.
        5. Assert drift_orphan_node_count >= 1 (neo4j_nodes > postgres_provenance).
        """
        from bot.services.graph_query import reconcile_counts

        adapter = NetworkXAdapter()

        # Insert an orphan node directly — no Postgres provenance row for it.
        orphan_key = f"orphan-g2b-{_next_id()}"
        await adapter.merge_node(
            node_key=orphan_key,
            labels=["MemoryNode"],
            properties={"label": orphan_key, "node_type": "Topic"},
        )

        # Postgres has some provenance rows from the outer transaction but zero
        # for this orphan. The adapter has 1 node. Drift = neo4j_nodes > postgres_prov.
        # reconcile_counts compares the totals so we need adapter nodes > pg provenance.
        # Given test isolation, db_session starts clean (outer transaction rollback).
        # So postgres_active_provenance == 0, neo4j_node_count == 1.
        report = await reconcile_counts(db_session, adapter)

        assert report.drift_detected is True, (
            f"G2b: drift NOT detected after orphan node insertion. "
            f"postgres_provenance={report.postgres_active_provenance}, "
            f"neo4j_nodes={report.neo4j_node_count}. "
            "Expected drift_detected=True since neo4j_nodes > postgres_provenance."
        )
        assert report.drift_orphan_node_count >= 1, (
            f"G2b: drift_orphan_node_count={report.drift_orphan_node_count} expected >= 1. "
            "An orphan node exists in NetworkX with no Postgres provenance counterpart."
        )
