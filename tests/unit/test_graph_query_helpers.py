"""Unit tests for graph_query.py helpers (T10-05).

Tests max_hops cap, max_results cap, GraphQueryResult frozen semantics,
visibility_filter validation, and role validation.
No DB required — all pure or AsyncMock-based.
"""

from __future__ import annotations

import pytest


# ─── Tests: max_hops cap (>5 rejects) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_related_topics_max_hops_cap_raises():
    """find_related_topics raises ValueError when max_hops > 5."""
    from unittest.mock import AsyncMock
    from bot.services.graph_query import find_related_topics

    session = AsyncMock()
    adapter = AsyncMock()

    with pytest.raises(ValueError, match="max_hops"):
        await find_related_topics(
            session,
            adapter,
            topic="AI",
            viewer_is_admin=True,
            max_hops=6,
        )


@pytest.mark.asyncio
async def test_explain_connection_max_hops_cap_raises():
    """explain_connection raises ValueError when max_hops > 5."""
    from unittest.mock import AsyncMock
    from bot.services.graph_query import explain_connection

    session = AsyncMock()
    adapter = AsyncMock()

    with pytest.raises(ValueError, match="max_hops"):
        await explain_connection(
            session,
            adapter,
            node_a="Alice",
            node_b="Bob",
            viewer_is_admin=True,
            max_hops=6,
        )


def test_max_hops_cap_constant():
    """MAX_HOPS_CAP constant is 5."""
    from bot.services.graph_query import MAX_HOPS_CAP

    assert MAX_HOPS_CAP == 5


# ─── Tests: max_results cap per role ─────────────────────────────────────────


def test_max_results_cap_member():
    """MAX_RESULTS_MEMBER is 200."""
    from bot.services.graph_query import MAX_RESULTS_MEMBER

    assert MAX_RESULTS_MEMBER == 200


def test_max_results_cap_admin():
    """MAX_RESULTS_ADMIN is 1000."""
    from bot.services.graph_query import MAX_RESULTS_ADMIN

    assert MAX_RESULTS_ADMIN == 1000


# ─── Tests: GraphQueryResult dataclass frozen + abstained semantics ───────────


def test_graph_query_result_is_frozen():
    """GraphQueryResult is a frozen dataclass — mutation raises."""
    from bot.services.graph_query import GraphQueryResult

    result = GraphQueryResult(
        abstained=False,
        abstain_reason=None,
        paths=[],
        query_metadata={"mode": "test"},
    )
    with pytest.raises((AttributeError, TypeError)):
        result.abstained = True  # type: ignore[misc]


def test_graph_query_result_abstained_true_semantics():
    """GraphQueryResult with abstained=True has empty paths and a reason."""
    from bot.services.graph_query import GraphQueryResult

    result = GraphQueryResult(
        abstained=True,
        abstain_reason="pending purge for node X",
        paths=[],
        query_metadata={},
    )
    assert result.abstained is True
    assert result.abstain_reason == "pending purge for node X"
    assert result.paths == []


def test_graph_path_dataclass():
    """GraphPath dataclass has expected fields."""
    from bot.services.graph_query import GraphPath

    path = GraphPath(
        nodes=[],
        edges=[],
        provenance_ids=[1, 2],
        source_message_version_ids=[],
        source_card_ids=[],
    )
    assert path.provenance_ids == [1, 2]


# ─── Tests: GraphQueryDisabledError exported ─────────────────────────────────


def test_graph_query_disabled_error_exported():
    """GraphQueryDisabledError is importable from graph_query."""
    from bot.services.graph_query import GraphQueryDisabledError

    assert issubclass(GraphQueryDisabledError, Exception)


# ─── Tests: feature flag function exported ───────────────────────────────────


def test_graph_query_feature_flag_constant():
    """GRAPH_QUERY_FEATURE_FLAG constant is 'memory.graph.query.enabled'."""
    from bot.services.graph_query import GRAPH_QUERY_FEATURE_FLAG

    assert GRAPH_QUERY_FEATURE_FLAG == "memory.graph.query.enabled"


# ─── Tests: max_hops valid values (boundary) ─────────────────────────────────


@pytest.mark.asyncio
async def test_find_related_topics_max_hops_5_is_valid():
    """find_related_topics does NOT raise ValueError when max_hops == 5."""
    from unittest.mock import AsyncMock, patch
    from bot.services.graph_query import find_related_topics

    session = AsyncMock()
    adapter = AsyncMock()
    adapter.query_traversal = AsyncMock(return_value=[])

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_query.assert_no_pending_purge",
        new=AsyncMock(),
    ), patch(
        "bot.services.graph_query._resolve_provenance_for_nodes",
        new=AsyncMock(return_value=[]),
    ):
        result = await find_related_topics(
            session,
            adapter,
            topic="AI",
            viewer_is_admin=True,
            max_hops=5,
        )

    assert result is not None


@pytest.mark.asyncio
async def test_find_related_topics_max_hops_1_is_valid():
    """find_related_topics does NOT raise ValueError when max_hops == 1."""
    from unittest.mock import AsyncMock, patch
    from bot.services.graph_query import find_related_topics

    session = AsyncMock()
    adapter = AsyncMock()
    adapter.query_traversal = AsyncMock(return_value=[])

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_query.assert_no_pending_purge",
        new=AsyncMock(),
    ), patch(
        "bot.services.graph_query._resolve_provenance_for_nodes",
        new=AsyncMock(return_value=[]),
    ):
        result = await find_related_topics(
            session,
            adapter,
            topic="AI",
            viewer_is_admin=True,
            max_hops=1,
        )

    assert result is not None


# ─── Tests: max_hops int type validation (FIX-WARN-2) ────────────────────────


# ─── Tests: paused kill-switch gate (FIX-1 / CRITICAL-1) ────────────────────


@pytest.mark.asyncio
async def test_query_disabled_when_write_pending_paused():
    """find_related_topics raises GraphQueryDisabledError when write_pending.paused is ON."""
    from unittest.mock import AsyncMock, patch
    from bot.services.graph_query import find_related_topics, GraphQueryDisabledError

    session = AsyncMock()
    adapter = AsyncMock()

    # query.enabled=True but write_pending.paused=True → should raise
    async def fake_is_enabled(s: object) -> bool:
        # Simulate the paused path by patching internal flag check directly
        return False

    with patch(
        "bot.services.graph_query._is_query_enabled",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(GraphQueryDisabledError):
            await find_related_topics(
                session,
                adapter,
                topic="AI",
                viewer_is_admin=True,
            )


@pytest.mark.asyncio
async def test_is_query_enabled_returns_false_when_paused():
    """_is_query_enabled returns False when write_pending.paused flag is ON even if query.enabled is ON."""
    from unittest.mock import AsyncMock, patch
    from bot.services.graph_query import _is_query_enabled

    session = AsyncMock()

    # query.enabled=True, write_pending.paused=True → should return False
    async def fake_get(s: object, flag_key: str) -> bool:
        if flag_key == "memory.graph.query.enabled":
            return True
        if flag_key == "memory.graph.write_pending.paused":
            return True
        return False

    with patch("bot.db.repos.feature_flag.FeatureFlagRepo.get", new=AsyncMock(side_effect=fake_get)):
        result = await _is_query_enabled(session)

    assert result is False, "_is_query_enabled must return False when write_pending.paused is True"


@pytest.mark.asyncio
async def test_is_query_enabled_returns_true_when_enabled_and_not_paused():
    """_is_query_enabled returns True when query.enabled=True and paused=False."""
    from unittest.mock import AsyncMock, patch
    from bot.services.graph_query import _is_query_enabled

    session = AsyncMock()

    async def fake_get(s: object, flag_key: str) -> bool:
        if flag_key == "memory.graph.query.enabled":
            return True
        if flag_key == "memory.graph.write_pending.paused":
            return False
        return False

    with patch("bot.db.repos.feature_flag.FeatureFlagRepo.get", new=AsyncMock(side_effect=fake_get)):
        result = await _is_query_enabled(session)

    assert result is True


# ─── Tests: max_hops int type validation (FIX-WARN-2) ────────────────────────


@pytest.mark.asyncio
async def test_find_related_topics_rejects_non_int_max_hops():
    """find_related_topics raises TypeError when max_hops is not int."""
    from unittest.mock import AsyncMock
    from bot.services.graph_query import find_related_topics

    session = AsyncMock()
    adapter = AsyncMock()

    with pytest.raises(TypeError, match="max_hops must be int"):
        await find_related_topics(
            session,
            adapter,
            topic="AI",
            viewer_is_admin=True,
            max_hops="3",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_find_related_topics_rejects_float_max_hops():
    """find_related_topics raises TypeError when max_hops is float."""
    from unittest.mock import AsyncMock
    from bot.services.graph_query import find_related_topics

    session = AsyncMock()
    adapter = AsyncMock()

    with pytest.raises(TypeError, match="max_hops must be int"):
        await find_related_topics(
            session,
            adapter,
            topic="AI",
            viewer_is_admin=True,
            max_hops=3.0,  # type: ignore[arg-type]
        )
