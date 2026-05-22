"""Tests for forget_cascade._cascade_graph_provenance (T10-06 / Phase 10).

Verifies:
1. forget_event → _cascade_graph_provenance → marks graph_provenance rows inactive
   AND enqueues graph_purge_pending rows in the same transaction.
2. graph_nodes layer appears in CASCADE_LAYER_ORDER at the correct position.
3. Full cascade with graph_nodes layer completes (status='completed' with graph_nodes).
4. Layer records skipped when no graph_provenance rows exist for target.
5. graph_nodes layer appears after card_sources in cascade_status on completion.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=9_000_000)


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


async def _make_message_with_version(db_session, *, text: str = "test") -> tuple[int, int, int, int]:
    """Create a ChatMessage + v1 MessageVersion. Returns (cm_id, mv_id, chat_id, msg_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text=text,
        date=when,
        raw_json={"text": text},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()

    msg.current_version_id = ver.id
    await db_session.flush()
    return msg.id, ver.id, chat_id, msg_id


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
    source_pk: str | None = None,
    source_message_version_id: int | None = None,
    source_card_id=None,
    graph_node_key: str | None = None,
    triple_hash: int | None = None,
) -> int:
    """Insert a graph_provenance row. Returns its id."""
    from bot.db.repos.graph_provenance import create_provenance

    pk = source_pk or str(_next_id())
    prov = await create_provenance(
        db_session,
        projection_run_id=run_id,
        source_table=source_table,
        source_pk=pk,
        source_message_version_id=source_message_version_id if source_table == "message_versions" else None,
        source_card_id=source_card_id,
        graph_node_key=graph_node_key or f"node:test:{_next_id()}",
        triple_hash=triple_hash,
    )
    return prov.id


async def _make_forget_event(db_session, *, target_type: str, target_id: str) -> int:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"{target_type}:{target_id}:test:{_next_id()}",
    )
    return ev.id


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_nodes_in_cascade_layer_order(db_session):
    """graph_nodes appears in CASCADE_LAYER_ORDER after card_sources."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    layers = list(CASCADE_LAYER_ORDER)
    assert "graph_nodes" in layers
    card_sources_idx = layers.index("card_sources")
    graph_nodes_idx = layers.index("graph_nodes")
    assert graph_nodes_idx > card_sources_idx, (
        "graph_nodes must appear after card_sources in CASCADE_LAYER_ORDER"
    )


@pytest.mark.asyncio
async def test_cascade_graph_provenance_marks_inactive_and_enqueues(db_session):
    """forget_event causes graph_provenance to be soft-deleted + purge_pending enqueued."""
    from bot.services.forget_cascade import run_cascade_worker_once
    from bot.db.models import GraphProvenance, GraphPurgePending
    from sqlalchemy import select

    cm_id, mv_id, chat_id, msg_id = await _make_message_with_version(db_session)
    run_id = await _make_projection_run(db_session)
    prov_id = await _make_provenance(
        db_session,
        run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
        graph_node_key=f"node:mv:{mv_id}",
    )

    await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
    )

    await run_cascade_worker_once(db_session)

    # graph_provenance row must be soft-deleted (purged_at IS NOT NULL)
    prov = await db_session.scalar(
        select(GraphProvenance).where(GraphProvenance.id == prov_id)
    )
    assert prov is not None
    assert prov.purged_at is not None, "graph_provenance.purged_at must be set"

    # graph_purge_pending row must be enqueued (purged_at IS NULL = waiting for worker)
    purge_row = await db_session.scalar(
        select(GraphPurgePending).where(
            GraphPurgePending.source_table == "message_versions",
            GraphPurgePending.source_pk == str(mv_id),
        )
    )
    assert purge_row is not None, "graph_purge_pending row must be enqueued"
    assert purge_row.purged_at is None  # not yet processed by graph_purge_worker


@pytest.mark.asyncio
async def test_cascade_graph_nodes_skipped_when_no_provenance(db_session):
    """forget_event where target has no graph_provenance: layer records rows=0."""
    from bot.services.forget_cascade import run_cascade_worker_once

    cm_id, mv_id, chat_id, msg_id = await _make_message_with_version(db_session)
    # No graph_provenance row created for this mv_id

    event_id = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
    )

    await run_cascade_worker_once(db_session)

    from sqlalchemy import select
    from bot.db.models import ForgetEvent
    ev = await db_session.scalar(
        select(ForgetEvent).where(ForgetEvent.id == event_id)
    )
    assert ev.status == "completed"
    cascade = ev.cascade_status
    assert "graph_nodes" in cascade
    assert cascade["graph_nodes"]["status"] == "completed"
    assert cascade["graph_nodes"]["rows"] == 0


@pytest.mark.asyncio
async def test_full_cascade_includes_graph_nodes_completed(db_session):
    """Full cascade run marks graph_nodes layer as completed in cascade_status."""
    from bot.services.forget_cascade import run_cascade_worker_once

    cm_id, mv_id, chat_id, msg_id = await _make_message_with_version(db_session)
    run_id = await _make_projection_run(db_session)
    await _make_provenance(
        db_session,
        run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
    )

    event_id = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    from sqlalchemy import select
    from bot.db.models import ForgetEvent
    ev = await db_session.scalar(
        select(ForgetEvent).where(ForgetEvent.id == event_id)
    )
    assert ev.status == "completed"
    cascade = ev.cascade_status
    assert cascade["graph_nodes"]["status"] == "completed"


# ─── CRITICAL-1: multi-provenance row enqueue ────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_enqueues_purge_for_all_provenance_rows_of_source(db_session):
    """CRITICAL-1: 3 graph_provenance rows for same source → 3 purge_pending rows."""
    from bot.services.forget_cascade import run_cascade_worker_once
    from bot.db.models import GraphPurgePending
    from sqlalchemy import select, func

    cm_id, mv_id, chat_id, msg_id = await _make_message_with_version(db_session)
    run_id = await _make_projection_run(db_session)
    source_pk = str(mv_id)

    # Three graph_provenance rows for same (source_table, source_pk) — different triple_hash
    for i in range(3):
        await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=source_pk,
            source_message_version_id=mv_id,
            graph_node_key=f"node:mv:{mv_id}:triple{i}",
            triple_hash=mv_id * 100 + i,
        )

    await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
    )

    await run_cascade_worker_once(db_session)

    # ALL 3 provenance rows must have a purge_pending row, not just 1
    count = await db_session.scalar(
        select(func.count()).select_from(GraphPurgePending).where(
            GraphPurgePending.source_table == "message_versions",
            GraphPurgePending.source_pk == source_pk,
        )
    )
    assert count == 3, (
        f"Expected 3 purge_pending rows for 3 provenance rows, got {count}"
    )


# ─── CRITICAL-2: knowledge_cards source path ─────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_enqueues_purge_for_archived_card_provenance(db_session):
    """CRITICAL-2: card whose ALL sources are in affected_mvids gets knowledge_cards purge_pending."""
    import uuid as _uuid
    from bot.services.forget_cascade import run_cascade_worker_once
    from bot.db.models import GraphPurgePending, KnowledgeCard, CardSource
    from sqlalchemy import select

    # One message with one version — will be the only source for the card
    cm_id, mv_id, chat_id, msg_id = await _make_message_with_version(db_session, text="src msg")
    run_id = await _make_projection_run(db_session)

    # A knowledge card sourced from ONLY that one message version
    card_id = _uuid.uuid4()
    card = KnowledgeCard(
        id=card_id,
        title="Test Card",
        body_markdown="body text",
        card_status="draft",
    )
    db_session.add(card)
    await db_session.flush()

    src = CardSource(card_id=card_id, message_version_id=mv_id, position=0)
    db_session.add(src)
    await db_session.flush()

    # graph_provenance for the knowledge_card itself
    from bot.db.repos.graph_provenance import create_provenance
    await create_provenance(
        db_session,
        projection_run_id=run_id,
        source_table="knowledge_cards",
        source_pk=str(card_id),
        source_card_id=card_id,
        graph_node_key=f"node:card:{card_id}",
    )

    # graph_provenance for the message_version
    await _make_provenance(
        db_session, run_id=run_id, source_table="message_versions",
        source_pk=str(mv_id), source_message_version_id=mv_id,
    )

    # Forget the message — its mv_id is the ONLY source for the card,
    # so all card sources are in affected_mvids.
    await _make_forget_event(
        db_session, target_type="message", target_id=str(cm_id)
    )

    await run_cascade_worker_once(db_session)

    # Check that a purge_pending row was enqueued for the knowledge_cards provenance
    card_purge = await db_session.scalar(
        select(GraphPurgePending).where(
            GraphPurgePending.source_table == "knowledge_cards",
            GraphPurgePending.source_pk == str(card_id),
        )
    )
    assert card_purge is not None, (
        "purge_pending row must be enqueued for knowledge_cards provenance "
        "when all card sources are in affected_mvids"
    )


# ─── HIGH-4: graph_edges.purged_at soft-delete ───────────────────────────────


@pytest.mark.asyncio
async def test_cascade_soft_deletes_graph_edges_in_same_transaction(db_session):
    """HIGH-4: cascade must set purged_at on graph_edges rows for provenance."""
    from bot.services.forget_cascade import run_cascade_worker_once
    from bot.db.models import GraphEdge
    from sqlalchemy import select

    cm_id, mv_id, chat_id, msg_id = await _make_message_with_version(db_session)
    run_id = await _make_projection_run(db_session)

    # Create provenance
    from bot.db.repos.graph_provenance import create_provenance
    prov = await create_provenance(
        db_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
        graph_node_key=f"node:mv:{mv_id}",
    )

    # Create a graph_edge referencing this provenance
    edge = GraphEdge(
        graph_provenance_id=prov.id,
        subject_node_key=f"node:mv:{mv_id}",
        predicate="MENTIONS",
        object_node_key="node:entity:test",
        edge_key=f"edge:{mv_id}:MENTIONS:test",
    )
    db_session.add(edge)
    await db_session.flush()

    await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
    )

    await run_cascade_worker_once(db_session)

    # graph_edges.purged_at must be set
    refreshed_edge = await db_session.scalar(
        select(GraphEdge).where(GraphEdge.id == edge.id)
    )
    assert refreshed_edge is not None
    assert refreshed_edge.purged_at is not None, (
        "graph_edges.purged_at must be set by cascade (spec §5.F step 3)"
    )
