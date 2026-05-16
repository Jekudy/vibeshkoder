"""Unit tests for GraphAdapter Protocol + NetworkXAdapter (T10-01).

These tests use NetworkXAdapter (in-memory fake) only — no real Neo4j required.
Mark: graph_unit.

Real Neo4j integration tests are deferred to T10-09.
"""

from __future__ import annotations

import pytest

from bot.services.graph_adapter import GraphAdapter, NetworkXAdapter


pytestmark = pytest.mark.graph_unit


@pytest.fixture()
def adapter() -> NetworkXAdapter:
    return NetworkXAdapter()


# ─── Protocol conformance ─────────────────────────────────────────────────────


def test_networkx_adapter_is_graph_adapter(adapter: NetworkXAdapter) -> None:
    """NetworkXAdapter must satisfy the GraphAdapter runtime-checkable Protocol."""
    assert isinstance(adapter, GraphAdapter)


# ─── merge_node + query_traversal ────────────────────────────────────────────


async def test_merge_node_then_query(adapter: NetworkXAdapter) -> None:
    """After merging a node, query_traversal must return it when topic matches."""
    await adapter.merge_node(
        node_key="node:test:1",
        labels=["MemoryNode"],
        properties={"label": "Архитектура", "node_type": "Topic"},
    )

    results = await adapter.query_traversal(topic="Архитектура", max_hops=2, max_results=10)

    assert len(results) >= 1
    labels = [r.get("label") for r in results]
    assert "Архитектура" in labels


# ─── merge_edge round-trip ────────────────────────────────────────────────────


async def test_merge_edge_round_trip(adapter: NetworkXAdapter) -> None:
    """After merging two nodes and an edge, query_traversal from source must reach target."""
    await adapter.merge_node(
        node_key="node:person:1",
        labels=["MemoryNode"],
        properties={"label": "Вася", "node_type": "Person"},
    )
    await adapter.merge_node(
        node_key="node:topic:1",
        labels=["MemoryNode"],
        properties={"label": "Python", "node_type": "Topic"},
    )
    await adapter.merge_edge(
        edge_key="edge:1",
        source_key="node:person:1",
        target_key="node:topic:1",
        relationship_type="KNOWS_ABOUT",
        properties={"provenance_id": "42"},
    )

    results = await adapter.query_traversal(topic="Вася", max_hops=1, max_results=10)

    node_labels = [r.get("label") for r in results]
    assert "Python" in node_labels


# ─── delete_provenance removes edges ─────────────────────────────────────────


async def test_delete_provenance_removes_edges(adapter: NetworkXAdapter) -> None:
    """delete_provenance must remove edges tagged with the given provenance_id."""
    await adapter.merge_node(
        node_key="node:a",
        labels=["MemoryNode"],
        properties={"label": "A", "node_type": "Topic"},
    )
    await adapter.merge_node(
        node_key="node:b",
        labels=["MemoryNode"],
        properties={"label": "B", "node_type": "Topic"},
    )
    await adapter.merge_edge(
        edge_key="edge:prov:1",
        source_key="node:a",
        target_key="node:b",
        relationship_type="RELATED_TO",
        properties={"provenance_id": "prov-123"},
    )

    purged = await adapter.delete_provenance("prov-123")

    assert purged >= 1
    # After purge, traversal from A should not find B
    results = await adapter.query_traversal(topic="A", max_hops=1, max_results=10)
    connected_labels = [r.get("label") for r in results if r.get("label") != "A"]
    assert "B" not in connected_labels
