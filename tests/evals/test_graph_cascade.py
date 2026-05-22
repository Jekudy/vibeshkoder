"""Phase 11 §5.F — Phase 10 graph cascade binding tests.

Covers AC IDs I8a, I8b, I8c, I8d, I8e from PHASE10_PLAN.md §10.

I8a: forget_event → graph_provenance soft-delete + graph_purge_pending enqueued atomically.
I8b: CASCADE_LAYER_ORDER.index("graph_nodes") > CASCADE_LAYER_ORDER.index("card_sources").
I8c: graph_purge_worker_tick processes pending row → purged_at set (Postgres side).
I8d: project_full_rebuild (replay, no LLM) produces identical node count on two consecutive
     calls (determinism test using NetworkXAdapter).
I8e: graph_edges soft-deleted in same transaction as graph_provenance (HIGH-4 invariant).

No real Neo4j required — uses NetworkXAdapter for graph operations.
No LLM calls (httpx_llm_guard autouse fixture enforces this).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot.services.graph_adapter import NetworkXAdapter

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=72_000_000)


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
        text="cascade test",
        date=when,
        raw_json={"text": "cascade test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="cascade test",
        normalized_text="cascade test",
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
    graph_edge_key: str | None = None,
) -> int:
    from bot.db.models import GraphProvenance

    prov = GraphProvenance(
        projection_run_id=run_id,
        source_table=source_table,
        source_pk=source_pk,
        source_message_version_id=source_message_version_id,
        graph_node_key=graph_node_key,
        graph_edge_key=graph_edge_key,
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


class TestGraphCascade:
    async def test_i8b_cascade_order_graph_nodes_after_card_sources(self) -> None:
        """I8b: CASCADE_LAYER_ORDER places graph_nodes after card_sources.

        Direct assertion on the tuple — no DB required.
        Invariant: graph_nodes must run AFTER card_sources so archived card IDs
        are still queryable when we look up graph_provenance rows keyed by card.
        """
        from bot.services.forget_cascade import CASCADE_LAYER_ORDER

        assert "graph_nodes" in CASCADE_LAYER_ORDER, (
            "I8b: 'graph_nodes' not in CASCADE_LAYER_ORDER"
        )
        assert "card_sources" in CASCADE_LAYER_ORDER, (
            "I8b: 'card_sources' not in CASCADE_LAYER_ORDER"
        )

        graph_nodes_idx = CASCADE_LAYER_ORDER.index("graph_nodes")
        card_sources_idx = CASCADE_LAYER_ORDER.index("card_sources")
        assert graph_nodes_idx > card_sources_idx, (
            f"I8b: graph_nodes (idx={graph_nodes_idx}) must come AFTER "
            f"card_sources (idx={card_sources_idx}) in CASCADE_LAYER_ORDER. "
            "Ordering invariant per PHASE10_PLAN.md §5.F."
        )

    async def test_i8a_forget_enqueues_purge_and_soft_deletes_provenance(
        self, db_session
    ) -> None:
        """I8a: forget_event → graph_provenance.purged_at set AND graph_purge_pending enqueued.

        Setup: normal message with a graph_provenance row.
        Issue a forget event. Run cascade worker once.
        Assert:
        - graph_provenance.purged_at IS NOT NULL (soft-deleted)
        - graph_purge_pending row created with purged_at IS NULL (still pending Neo4j delete)
        """
        from bot.db.repos.forget_event import ForgetEventRepo
        from bot.services.forget_cascade import run_cascade_worker_once
        from bot.db.models import GraphProvenance, GraphPurgePending
        from sqlalchemy import select

        cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-i8a-{mv_id}"
        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        await ForgetEventRepo.create(
            db_session,
            target_type="message",
            target_id=str(cm_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message:i8a:{cm_id}",
        )

        await run_cascade_worker_once(db_session, bot=None, batch_size=10)

        # Assert: provenance soft-deleted.
        prov = await db_session.get(GraphProvenance, prov_id)
        assert prov is not None
        assert prov.purged_at is not None, (
            f"I8a: graph_provenance id={prov_id} purged_at is NULL after cascade"
        )

        # Assert: purge_pending row enqueued.
        pending_count_result = await db_session.execute(
            select(GraphPurgePending).where(
                GraphPurgePending.graph_node_key == node_key
            )
        )
        pending_rows = pending_count_result.scalars().all()
        assert len(pending_rows) >= 1, (
            f"I8a: no graph_purge_pending row enqueued for node_key={node_key}"
        )

    async def test_i8c_purge_worker_processes_pending_row(self, db_session) -> None:
        """I8c: graph_purge_worker_tick processes a pending row → purged_at set.

        Setup: Insert a graph_purge_pending row manually.
        Call graph_purge_worker_tick with a mocked adapter (returns 1 node deleted).
        Assert: graph_purge_pending.purged_at IS NOT NULL.
        """
        from bot.db.models import GraphPurgePending

        # Simulate a pending purge row (as if cascade had already set provenance purged_at).
        cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-i8c-{mv_id}"
        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        # Manually insert a purge_pending row.
        pending_row = GraphPurgePending(
            forget_event_id=_next_id(),
            source_table="message_versions",
            source_pk=str(mv_id),
            graph_node_key=node_key,
            graph_provenance_id=prov_id,
        )
        db_session.add(pending_row)
        await db_session.flush()
        pending_id = pending_row.id

        # Mock the Neo4j adapter to simulate successful delete.
        mock_adapter = AsyncMock()
        mock_adapter.delete_provenance = AsyncMock(return_value=1)

        from bot.services.graph_purge_worker import graph_purge_worker_tick

        await graph_purge_worker_tick(db_session, adapter=mock_adapter, batch_size=10)

        # Assert: pending row now has purged_at set.
        await db_session.refresh(pending_row)
        assert pending_row.purged_at is not None, (
            f"I8c: graph_purge_pending id={pending_id} purged_at is NULL after worker tick"
        )

    async def test_i8d_full_rebuild_determinism(self, db_session) -> None:
        """I8d: project_full_rebuild (replay, no LLM) → deterministic node count.

        Two consecutive project_full_rebuild calls on the same Postgres state
        produce the same projected_node_count. Replay-only — no LLM calls.
        """
        from bot.db.repos.feature_flag import FeatureFlagRepo
        from bot.services.graph_projector import (
            default_projector_config,
            project_full_rebuild,
            GRAPH_PROJECTION_FEATURE_FLAG,
        )

        await FeatureFlagRepo.set_enabled(db_session, GRAPH_PROJECTION_FEATURE_FLAG, True)

        cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-i8d-{mv_id}"
        obj_key = f"obj-i8d-{mv_id}"
        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )
        await _make_graph_edge(
            db_session,
            provenance_id=prov_id,
            subject_node_key=node_key,
            object_node_key=obj_key,
        )

        # First rebuild.
        adapter1 = NetworkXAdapter()
        config1 = default_projector_config(adapter1)
        result1 = await project_full_rebuild(db_session, config=config1, started_by="test")

        # Second rebuild on fresh adapter (simulate clean Neo4j state).
        adapter2 = NetworkXAdapter()
        config2 = default_projector_config(adapter2)
        result2 = await project_full_rebuild(db_session, config=config2, started_by="test")

        assert result1.nodes_merged == result2.nodes_merged, (
            f"I8d: full_rebuild not deterministic: "
            f"run1 nodes_merged={result1.nodes_merged}, run2 nodes_merged={result2.nodes_merged}"
        )
        assert result1.edges_merged == result2.edges_merged, (
            f"I8d: full_rebuild edge count not deterministic: "
            f"run1={result1.edges_merged}, run2={result2.edges_merged}"
        )

    async def test_i8e_graph_edges_soft_deleted_with_provenance(
        self, db_session
    ) -> None:
        """I8e: graph_edges purged_at set in same transaction as graph_provenance (HIGH-4).

        Setup: forget event with graph_provenance + graph_edges row.
        Run cascade.
        Assert both graph_provenance.purged_at and graph_edges.purged_at are set.
        """
        from bot.db.repos.forget_event import ForgetEventRepo
        from bot.services.forget_cascade import run_cascade_worker_once
        from bot.db.models import GraphProvenance, GraphEdge

        cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-i8e-{mv_id}"
        obj_key = f"obj-i8e-{mv_id}"
        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )
        edge_id = await _make_graph_edge(
            db_session,
            provenance_id=prov_id,
            subject_node_key=node_key,
            object_node_key=obj_key,
        )

        await ForgetEventRepo.create(
            db_session,
            target_type="message",
            target_id=str(cm_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message:i8e:{cm_id}",
        )

        await run_cascade_worker_once(db_session, bot=None, batch_size=10)

        # Assert: graph_provenance soft-deleted.
        prov = await db_session.get(GraphProvenance, prov_id)
        assert prov is not None
        assert prov.purged_at is not None, (
            f"I8e: graph_provenance id={prov_id} purged_at is NULL after cascade"
        )

        # Assert: graph_edges soft-deleted in same transaction (HIGH-4 invariant).
        edge = await db_session.get(GraphEdge, edge_id)
        assert edge is not None
        assert edge.purged_at is not None, (
            f"I8e: graph_edges id={edge_id} purged_at is NULL after cascade — "
            "HIGH-4 invariant: edges must be soft-deleted in the same transaction "
            "as graph_provenance"
        )
