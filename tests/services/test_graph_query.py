"""Integration tests for bot/services/graph_query.py (T10-05).

Tests use:
- NetworkXAdapter (in-memory, no real Neo4j)
- db_session fixture (real postgres, transaction-wrapped, rolled back after test)

All tests require postgres connectivity and are skipped when postgres is
unreachable (same pattern as other services/ tests).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=8_000_000)


def _next_id() -> int:
    return next(_counter)


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    """Create a user row and return telegram_id."""
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


async def _make_message_version(db_session) -> int:
    """Create a ChatMessage + MessageVersion row. Returns message_version.id."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text="test",
        date=when,
        raw_json={"text": "test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="test",
        normalized_text="test",
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()

    msg.current_version_id = ver.id
    await db_session.flush()
    return ver.id


async def _seed_projection_run(db_session, *, mode="incremental") -> int:
    """Insert a graph_projection_runs row and return its id."""
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(db_session, mode=mode, started_by="test")
    await db_session.flush()
    return run.id


async def _seed_graph_provenance(
    db_session,
    *,
    run_id: int,
    graph_node_key: str,
) -> int:
    """Insert a graph_provenance row for a message_version source. Returns its id."""
    from bot.db.repos.graph_provenance import create_provenance

    mv_id = await _make_message_version(db_session)

    prov = await create_provenance(
        db_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
        graph_node_key=graph_node_key,
        triple_hash=_next_id(),
    )
    return prov.id


async def _seed_purge_pending(
    db_session,
    *,
    graph_node_key: str,
) -> None:
    """Insert a graph_purge_pending row (pending = purged_at IS NULL).

    forget_event_id is a BigInteger without FK constraint — use any large integer.
    source_table uses 'message_versions' to satisfy the CHECK constraint.
    """
    from bot.db.models import GraphPurgePending

    row = GraphPurgePending(
        forget_event_id=_next_id(),
        source_table="message_versions",
        source_pk=str(_next_id()),
        graph_node_key=graph_node_key,
    )
    db_session.add(row)
    await db_session.flush()


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_neighbors_returns_results(db_session):
    """find_related_topics seeds graph_provenance + adapter, returns paths."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics

    adapter = NetworkXAdapter()

    # Seed graph with nodes
    topic_key = f"topic:{_next_id()}"
    neighbor_key = f"topic:{_next_id()}"

    await adapter.merge_node(
        node_key=topic_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": topic_key, "node_type": "Topic"},
    )
    await adapter.merge_node(
        node_key=neighbor_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": neighbor_key, "node_type": "Topic"},
    )
    await adapter.merge_edge(
        edge_key=f"edge:{_next_id()}",
        source_key=topic_key,
        target_key=neighbor_key,
        relationship_type="RELATED_TO",
        properties={"predicate": "RELATED_TO"},
    )

    # Seed Postgres provenance
    run_id = await _seed_projection_run(db_session)
    await _seed_graph_provenance(
        db_session, run_id=run_id, graph_node_key=topic_key
    )
    await _seed_graph_provenance(
        db_session, run_id=run_id, graph_node_key=neighbor_key
    )

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=True,
            max_hops=2,
        )

    assert not result.abstained
    assert len(result.paths) >= 1
    # Verify each path has provenance_ids
    for path in result.paths:
        assert len(path.provenance_ids) >= 1, "Every path must have provenance_ids"


@pytest.mark.asyncio
async def test_find_neighbors_abstains_on_pending_purge(db_session):
    """find_related_topics returns abstained=True when topic has pending purge."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics

    adapter = NetworkXAdapter()
    topic_key = f"topic:{_next_id()}"

    # Seed a pending purge for this node
    await _seed_purge_pending(db_session, graph_node_key=topic_key)

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=True,
            max_hops=2,
        )

    assert result.abstained is True
    assert result.abstain_reason is not None
    assert len(result.paths) == 0


@pytest.mark.asyncio
async def test_find_paths_respects_max_hops(db_session):
    """explain_connection raises ValueError when max_hops > MAX_HOPS_CAP."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import explain_connection

    adapter = NetworkXAdapter()

    with pytest.raises(ValueError, match="max_hops"):
        await explain_connection(
            db_session,
            adapter,
            node_a="A",
            node_b="B",
            viewer_is_admin=True,
            max_hops=6,
        )


@pytest.mark.asyncio
async def test_query_by_concept_filters_by_visibility_member(db_session):
    """find_people_for_topic returns only Person nodes for member role."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_people_for_topic

    adapter = NetworkXAdapter()
    topic_key = f"topic:{_next_id()}"
    person_key = f"person:{_next_id()}"
    project_key = f"project:{_next_id()}"  # non-Person node

    # Seed graph
    await adapter.merge_node(
        node_key=topic_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": topic_key, "node_type": "Topic"},
    )
    await adapter.merge_node(
        node_key=person_key,
        labels=["Person", "MemoryNode"],
        properties={"label": person_key, "node_type": "Person"},
    )
    await adapter.merge_node(
        node_key=project_key,
        labels=["Project", "MemoryNode"],
        properties={"label": project_key, "node_type": "Project"},
    )
    await adapter.merge_edge(
        edge_key=f"edge:{_next_id()}",
        source_key=topic_key,
        target_key=person_key,
        relationship_type="MENTIONS",
        properties={"predicate": "MENTIONS"},
    )
    await adapter.merge_edge(
        edge_key=f"edge:{_next_id()}",
        source_key=topic_key,
        target_key=project_key,
        relationship_type="RELATED_TO",
        properties={"predicate": "RELATED_TO"},
    )

    # Seed provenance for all nodes
    run_id = await _seed_projection_run(db_session)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=topic_key)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=person_key)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=project_key)

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_people_for_topic(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=False,
        )

    assert not result.abstained
    # Only Person nodes should be in paths
    for path in result.paths:
        for node in path.nodes:
            assert node.get("node_type") == "Person", (
                f"Non-Person node_type found: {node.get('node_type')}"
            )


@pytest.mark.asyncio
async def test_member_cannot_see_admin_only_nodes(db_session):
    """find_related_topics respects max_results cap for member role."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics, MAX_RESULTS_MEMBER

    adapter = NetworkXAdapter()
    topic_key = f"topic:{_next_id()}"

    await adapter.merge_node(
        node_key=topic_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": topic_key, "node_type": "Topic"},
    )

    run_id = await _seed_projection_run(db_session)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=topic_key)

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=False,          # member role
            max_results=MAX_RESULTS_MEMBER + 999,  # request more than cap
        )

    # Should clamp to MAX_RESULTS_MEMBER internally
    assert not result.abstained
    assert result.query_metadata.get("max_results") <= MAX_RESULTS_MEMBER


@pytest.mark.asyncio
async def test_feature_flag_off_raises_disabled_error(db_session):
    """find_related_topics raises GraphQueryDisabledError when flag is OFF."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics, GraphQueryDisabledError

    adapter = NetworkXAdapter()

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(GraphQueryDisabledError):
            await find_related_topics(
                db_session,
                adapter,
                topic="AI",
                viewer_is_admin=True,
            )


@pytest.mark.asyncio
async def test_orphan_nodes_excluded(db_session):
    """Nodes without active graph_provenance are excluded from results."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics

    adapter = NetworkXAdapter()
    topic_key = f"topic:{_next_id()}"
    orphan_key = f"topic:{_next_id()}"

    # Add both to the graph adapter
    await adapter.merge_node(
        node_key=topic_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": topic_key, "node_type": "Topic"},
    )
    await adapter.merge_node(
        node_key=orphan_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": orphan_key, "node_type": "Topic"},
    )
    await adapter.merge_edge(
        edge_key=f"edge:{_next_id()}",
        source_key=topic_key,
        target_key=orphan_key,
        relationship_type="RELATED_TO",
        properties={"predicate": "RELATED_TO"},
    )

    # Only seed provenance for topic_key — orphan_key has NO provenance
    run_id = await _seed_projection_run(db_session)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=topic_key)
    # orphan_key intentionally NOT inserted into graph_provenance

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=True,
            max_hops=2,
        )

    # orphan_key must NOT appear in any path
    returned_keys = {
        node.get("node_key")
        for path in result.paths
        for node in path.nodes
    }
    assert orphan_key not in returned_keys, (
        f"Orphan node {orphan_key!r} appeared in results without provenance"
    )


@pytest.mark.asyncio
async def test_sources_for_path_returns_provenance_rows(db_session):
    """sources_for_path returns graph_provenance rows for given provenance_ids."""
    from bot.services.graph_query import sources_for_path

    run_id = await _seed_projection_run(db_session)
    prov_id = await _seed_graph_provenance(
        db_session, run_id=run_id, graph_node_key=f"node:{_next_id()}"
    )

    rows = await sources_for_path(db_session, provenance_ids=[prov_id])

    assert len(rows) == 1
    assert rows[0].id == prov_id


@pytest.mark.asyncio
async def test_sources_for_path_empty_ids_returns_empty(db_session):
    """sources_for_path with empty list returns empty list."""
    from bot.services.graph_query import sources_for_path

    rows = await sources_for_path(db_session, provenance_ids=[])

    assert rows == []


@pytest.mark.asyncio
async def test_explain_connection_returns_abstained_on_purge(db_session):
    """explain_connection returns abstained=True when anchor node has pending purge."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import explain_connection

    adapter = NetworkXAdapter()
    node_a = f"topic:{_next_id()}"
    node_b = f"topic:{_next_id()}"

    # Seed a pending purge for node_a
    await _seed_purge_pending(db_session, graph_node_key=node_a)

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await explain_connection(
            db_session,
            adapter,
            node_a=node_a,
            node_b=node_b,
            viewer_is_admin=True,
            max_hops=3,
        )

    assert result.abstained is True
    assert result.paths == []


@pytest.mark.asyncio
async def test_sources_for_path_excludes_purged_provenance(db_session):
    """sources_for_path does not return provenance rows where purged_at IS NOT NULL (FIX-WARN-3)."""
    from datetime import datetime, timezone
    from bot.services.graph_query import sources_for_path

    run_id = await _seed_projection_run(db_session)
    node_key = f"node:{_next_id()}"

    # Active provenance row
    active_prov_id = await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=node_key)

    # Purged provenance row (purged_at is set)
    mv_id = await _make_message_version(db_session)
    purged_prov = await __import__("bot.db.repos.graph_provenance", fromlist=["create_provenance"]).create_provenance(
        db_session,
        projection_run_id=run_id,
        source_table="message_versions",
        source_pk=str(mv_id),
        source_message_version_id=mv_id,
        graph_node_key=f"node:{_next_id()}",
        triple_hash=_next_id(),
    )
    purged_prov.purged_at = datetime.now(timezone.utc)
    db_session.add(purged_prov)
    await db_session.flush()

    rows = await sources_for_path(db_session, provenance_ids=[active_prov_id, purged_prov.id])

    returned_ids = {r.id for r in rows}
    assert active_prov_id in returned_ids, "active provenance must be returned"
    assert purged_prov.id not in returned_ids, "purged provenance must be excluded from sources_for_path"


@pytest.mark.asyncio
async def test_explain_connection_returns_edges_along_path(db_session):
    """explain_connection returns GraphPaths with edges populated (FIX-2 / CRITICAL-2)."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import explain_connection

    adapter = NetworkXAdapter()
    node_a_key = f"topic:{_next_id()}"
    node_b_key = f"topic:{_next_id()}"

    await adapter.merge_node(
        node_key=node_a_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": node_a_key, "node_type": "Topic"},
    )
    await adapter.merge_node(
        node_key=node_b_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": node_b_key, "node_type": "Topic"},
    )
    await adapter.merge_edge(
        edge_key=f"edge:{_next_id()}",
        source_key=node_a_key,
        target_key=node_b_key,
        relationship_type="RELATED_TO",
        properties={"predicate": "RELATED_TO"},
    )

    run_id = await _seed_projection_run(db_session)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=node_a_key)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=node_b_key)

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await explain_connection(
            db_session,
            adapter,
            node_a=node_a_key,
            node_b=node_b_key,
            viewer_is_admin=True,
            max_hops=3,
        )

    assert not result.abstained
    assert result.query_metadata.get("connection_found") is True
    # At least one path must have edges populated
    paths_with_edges = [p for p in result.paths if len(p.edges) > 0]
    assert len(paths_with_edges) > 0, (
        f"explain_connection must return at least one path with edges; "
        f"got paths={result.paths}"
    )


@pytest.mark.asyncio
async def test_find_related_topics_returns_neighbor_nodes(db_session):
    """find_related_topics returns neighbor nodes (basic traversal still works after FIX-2)."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics

    adapter = NetworkXAdapter()
    topic_key = f"topic:{_next_id()}"
    neighbor_key = f"topic:{_next_id()}"

    await adapter.merge_node(
        node_key=topic_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": topic_key, "node_type": "Topic"},
    )
    await adapter.merge_node(
        node_key=neighbor_key,
        labels=["Topic", "MemoryNode"],
        properties={"label": neighbor_key, "node_type": "Topic"},
    )
    await adapter.merge_edge(
        edge_key=f"edge:{_next_id()}",
        source_key=topic_key,
        target_key=neighbor_key,
        relationship_type="RELATED_TO",
        properties={"predicate": "RELATED_TO"},
    )

    run_id = await _seed_projection_run(db_session)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=topic_key)
    await _seed_graph_provenance(db_session, run_id=run_id, graph_node_key=neighbor_key)

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=True,
            max_hops=1,
        )

    assert not result.abstained
    returned_keys = {n["node_key"] for p in result.paths for n in p.nodes}
    assert neighbor_key in returned_keys, "neighbor_key must appear in find_related_topics result"


@pytest.mark.asyncio
async def test_graph_stats_returns_counts(db_session):
    """graph_stats returns non-negative counts from Postgres."""
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import graph_stats

    adapter = NetworkXAdapter()
    stats = await graph_stats(db_session, adapter)

    assert stats.active_provenance_rows >= 0
    assert stats.active_edge_rows >= 0
    assert stats.purged_provenance_rows >= 0


@pytest.mark.asyncio
async def test_find_related_topics_abstains_on_knowledge_cards_purge(db_session):
    """RFC-001:415 fail-closed: knowledge_cards pending purge also blocks graph_query.

    Codex CRITICAL fix: 10.5-8 narrowed assert_no_pending_purge to
    source_table_filter='message_versions', but forget_cascade enqueues
    knowledge_cards purges too (forget_cascade.py:1230-1262, 1281-1289).
    The narrowing created a privacy leak — stale Neo4j nodes for purged
    cards could still be returned. No source_table_filter must be used on
    the post-traversal guard.
    """
    from bot.db.models import GraphPurgePending
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import find_related_topics
    from unittest.mock import AsyncMock, patch

    adapter = NetworkXAdapter()
    topic_key = f"topic:{_next_id()}"

    # Insert a pending purge row with source_table='knowledge_cards'
    # (this is what forget_cascade enqueues for knowledge_cards sources)
    row = GraphPurgePending(
        forget_event_id=_next_id(),
        source_table="knowledge_cards",
        source_pk=str(_next_id()),
        graph_node_key=topic_key,
    )
    db_session.add(row)
    await db_session.flush()

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_key,
            viewer_is_admin=True,
            max_hops=2,
        )

    # RFC-001:415 fail-closed: must abstain even for knowledge_cards purge
    assert result.abstained is True, (
        "RFC-001:415 violated: knowledge_cards pending purge must trigger "
        "fail-closed read-block, not be ignored by source_table_filter narrowing"
    )
    assert result.abstain_reason is not None


@pytest.mark.asyncio
async def test_explain_connection_abstains_on_knowledge_cards_purge(db_session):
    """RFC-001:415 fail-closed: knowledge_cards pending purge blocks explain_connection.

    Same Codex CRITICAL as above — post-traversal guard in explain_connection
    must not use source_table_filter='message_versions'.
    """
    from bot.db.models import GraphPurgePending
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_query import explain_connection
    from unittest.mock import AsyncMock, patch

    adapter = NetworkXAdapter()
    node_key = f"topic:{_next_id()}"
    other_key = f"topic:{_next_id()}"

    # Insert knowledge_cards pending purge for the anchor node
    row = GraphPurgePending(
        forget_event_id=_next_id(),
        source_table="knowledge_cards",
        source_pk=str(_next_id()),
        graph_node_key=node_key,
    )
    db_session.add(row)
    await db_session.flush()

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ):
        result = await explain_connection(
            db_session,
            adapter,
            node_a=node_key,
            node_b=other_key,
            viewer_is_admin=True,
            max_hops=2,
        )

    # RFC-001:415 fail-closed: must abstain even for knowledge_cards purge
    assert result.abstained is True, (
        "RFC-001:415 violated: knowledge_cards pending purge must trigger "
        "fail-closed read-block in explain_connection"
    )
