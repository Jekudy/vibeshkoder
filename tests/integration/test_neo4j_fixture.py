"""Smoke tests for the neo4j_session fixture (W0-D).

Tests are marked @pytest.mark.graph_integration and are SKIPPED when
NEO4J_BOLT_URI is not set (local dev without Neo4j). In CI the service is
present and NEO4J_BOLT_URI is set, so tests RUN.

Two behavioral tests:
  1. Fixture connects and returns data — MERGE + READ a node.
  2. Fixture cleans between tests — second test sees an empty DB.
"""

from __future__ import annotations

import pytest


@pytest.mark.graph_integration
@pytest.mark.asyncio
async def test_neo4j_fixture_connects_and_returns_data(neo4j_session) -> None:
    """Fixture connects to Neo4j, runs MERGE, and reads the node back."""
    await neo4j_session.run(
        "MERGE (n:TestNode {key: $key}) SET n.value = $value",
        key="smoke-1",
        value="hello",
    )
    result = await neo4j_session.run(
        "MATCH (n:TestNode {key: $key}) RETURN n.value AS v",
        key="smoke-1",
    )
    record = await result.single()
    assert record is not None
    assert record["v"] == "hello"


@pytest.mark.graph_integration
@pytest.mark.asyncio
async def test_neo4j_fixture_cleans_between_tests(neo4j_session) -> None:
    """DB is empty at start of each test — node from previous test must not exist."""
    result = await neo4j_session.run("MATCH (n) RETURN count(n) AS cnt")
    record = await result.single()
    assert record is not None
    # DB should be empty — cleanup ran before this test
    assert record["cnt"] == 0
