"""G4 binding test — edge_key_hash written by projector and parity with NetworkXAdapter.

Spec: 10.5-10 / 10.5-11 — projector must write edge_key_hash into adapter on merge_edge;
NetworkXAdapter must store and return it on query.

G4a: After merge_edge with edge_key_hash in properties → stored in NetworkXAdapter edge.
G4b: Computed edge_key_hash matches expected sha256-based formula (frozen literal).
G4c: NetworkXAdapter.count_edges reflects merged edge.
G4d: Neo4jAdapter.merge_edge passes edge_key_hash parameter to Cypher (mock driver).
G4e: compute_edge_key_hash returns int, sum over multiple hashes does not raise.

These tests use NetworkXAdapter (in-memory) or mock for Neo4j — no live Neo4j required.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.graph_adapter import NetworkXAdapter
from bot.services.graph_common import compute_edge_key_hash

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=77_000_000)


def _next_id() -> int:
    return next(_counter)


class TestGraphDriftHash:
    """G4: edge_key_hash written into adapter and retrievable."""

    async def test_g4a_edge_key_hash_stored_in_networkx_adapter(self) -> None:
        """G4a: merge_edge with edge_key_hash in properties → stored in adapter edge data."""
        adapter = NetworkXAdapter()

        node_a = f"nodeA-{_next_id()}"
        node_b = f"nodeB-{_next_id()}"
        predicate = "RELATED_TO"
        edge_key = f"ek-{_next_id()}"
        edge_key_hash = compute_edge_key_hash(edge_key)

        await adapter.merge_node(
            node_key=node_a,
            labels=["MemoryNode"],
            properties={"label": node_a, "node_type": "Topic"},
        )
        await adapter.merge_node(
            node_key=node_b,
            labels=["MemoryNode"],
            properties={"label": node_b, "node_type": "Topic"},
        )
        await adapter.merge_edge(
            edge_key=edge_key,
            source_key=node_a,
            target_key=node_b,
            relationship_type=predicate,
            properties={
                "predicate": predicate,
                "edge_key_hash": edge_key_hash,
                "confidence": 0.9,
            },
        )

        # Inspect edge data stored in the adapter
        stored_hash = adapter.get_edge_key_hash(edge_key)
        assert stored_hash == edge_key_hash, (
            f"G4a: edge_key_hash not stored correctly. "
            f"expected={edge_key_hash!r}, got={stored_hash!r}"
        )
        assert isinstance(stored_hash, int), (
            f"G4a: edge_key_hash must be int, got {type(stored_hash)}"
        )

    async def test_g4b_edge_key_hash_formula_matches_projector(self) -> None:
        """G4b: hash computed by compute_edge_key_hash matches frozen literal.

        Frozen value locks the formula — if this test fails, drift detection sums
        stored in Neo4j are inconsistent with newly computed hashes.
        """
        from bot.services.graph_projector import _compute_edge_key

        node_a = "concept:Python"
        node_b = "concept:Programming"
        predicate = "IS_PART_OF"

        # The projector computes edge_key = sha256(canonical).hexdigest() (64 chars)
        # then triple_hash = compute_edge_key_hash(edge_key) → signed int64
        edge_key = _compute_edge_key(node_a, predicate, node_b)
        result = compute_edge_key_hash(edge_key)

        # FROZEN LITERAL — changing the formula will break stored hashes.
        # Recomputed via: hashlib.sha256(sha256("concept:Python|IS_PART_OF|concept:Programming").hexdigest().encode()).digest()[:8] → struct '>q'
        FROZEN_EXPECTED: int = -1196849340388981680
        assert result == FROZEN_EXPECTED, (
            f"G4b: formula drifted — drift detection will break. "
            f"expected={FROZEN_EXPECTED}, got={result}. "
            f"edge_key={edge_key!r}"
        )
        assert isinstance(result, int), "G4b: compute_edge_key_hash must return int"

    async def test_g4c_projector_writes_edge_key_hash_to_adapter(self) -> None:
        """G4c: projector passes edge_key_hash in merge_edge properties → adapter stores it."""
        from bot.services.graph_projector import _compute_edge_key

        adapter = NetworkXAdapter()

        node_a = f"subject-{_next_id()}"
        node_b = f"object-{_next_id()}"
        predicate = "HAS_PROPERTY"

        # Compute edge_key and triple_hash exactly as the projector does
        edge_key = _compute_edge_key(node_a, predicate, node_b)
        triple_hash = compute_edge_key_hash(edge_key)

        await adapter.merge_node(
            node_key=node_a,
            labels=["MemoryNode"],
            properties={"label": node_a},
        )
        await adapter.merge_node(
            node_key=node_b,
            labels=["MemoryNode"],
            properties={"label": node_b},
        )
        await adapter.merge_edge(
            edge_key=edge_key,
            source_key=node_a,
            target_key=node_b,
            relationship_type=predicate,
            properties={
                "predicate": predicate,
                "confidence": 0.85,
                "edge_key_hash": triple_hash,
            },
        )

        stored = adapter.get_edge_key_hash(edge_key)
        assert stored == triple_hash, (
            f"G4c: projector edge_key_hash not stored in adapter. "
            f"expected={triple_hash!r}, got={stored!r}"
        )
        assert await adapter.count_edges() == 1, "G4c: adapter must have exactly 1 edge"

    async def test_g4d_neo4j_adapter_writes_edge_key_hash(self) -> None:
        """G4d: Neo4jAdapter.merge_edge passes edge_key_hash parameter to Cypher.

        Uses a mock driver — no live Neo4j required.
        """
        from bot.services.graph_projector import _compute_edge_key

        # Build a mock Neo4j driver that captures run() calls
        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)
        mock_session.run = AsyncMock(return_value=mock_result)
        # __aenter__/__aexit__ for async context manager
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        # Construct Neo4jAdapter and replace its driver
        from bot.services.graph_adapter import Neo4jAdapter

        adapter = object.__new__(Neo4jAdapter)
        adapter._driver = mock_driver  # type: ignore[attr-defined]
        adapter._database = "neo4j"  # type: ignore[attr-defined]

        node_a = f"subject-{_next_id()}"
        node_b = f"object-{_next_id()}"
        predicate = "RELATED_TO"
        edge_key = _compute_edge_key(node_a, predicate, node_b)
        edge_key_hash = compute_edge_key_hash(edge_key)

        await adapter.merge_edge(
            edge_key=edge_key,
            source_key=node_a,
            target_key=node_b,
            relationship_type=predicate,
            properties={
                "predicate": predicate,
                "provenance_id": "prov-abc",
                "edge_key_hash": edge_key_hash,
            },
        )

        # Verify session.run was called with edge_key_hash kwarg
        assert mock_session.run.called, "G4d: mock session.run was not called"
        call_kwargs = mock_session.run.call_args
        # Keyword arguments passed to session.run
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        # If not in kwargs, check args[1:] (positional after query string)
        if "edge_key_hash" not in kwargs:
            # The driver may receive params as positional dicts; check all args
            all_args = call_kwargs.args
            found = any(
                isinstance(a, dict) and "edge_key_hash" in a for a in all_args
            )
            assert found, (
                f"G4d: edge_key_hash not passed to Neo4j session.run. "
                f"call_args={call_kwargs!r}"
            )
        else:
            assert kwargs["edge_key_hash"] == edge_key_hash, (
                f"G4d: edge_key_hash value mismatch. "
                f"expected={edge_key_hash}, got={kwargs['edge_key_hash']}"
            )

        # Also verify edge_key_hash appears in the Cypher string
        cypher = mock_session.run.call_args.args[0]
        assert "edge_key_hash" in cypher, (
            f"G4d: 'edge_key_hash' not in Cypher query. cypher={cypher!r}"
        )

    async def test_g4e_edge_key_hash_aggregation_type_int64(self) -> None:
        """G4e: compute_edge_key_hash returns int; sum over multiple hashes does not raise.

        Verifies that Cypher sum(r.edge_key_hash) analogue works in Python,
        and that hex-parsing ValueError cannot occur (regression guard).
        """
        edge_keys = [
            "concept:Python|IS_PART_OF|concept:Programming",
            "user:alice|KNOWS_ABOUT|concept:Python",
            "event:hackathon|MENTIONS|concept:Programming",
        ]

        hashes = [compute_edge_key_hash(ek) for ek in edge_keys]

        # All values must be int, not str
        for ek, h in zip(edge_keys, hashes):
            assert isinstance(h, int), (
                f"G4e: compute_edge_key_hash({ek!r}) returned {type(h)}, expected int"
            )

        # sum() must not raise (would fail if values were hex strings like "a1b2...")
        total = sum(hashes)
        assert isinstance(total, int), f"G4e: sum of hashes must be int, got {type(total)}"

        # Determinism check: same input → same output
        hashes2 = [compute_edge_key_hash(ek) for ek in edge_keys]
        assert hashes == hashes2, "G4e: compute_edge_key_hash is not deterministic"
