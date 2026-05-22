"""Unit tests for bot/services/graph_common.py (W0-A / Phase 10).

Tests verify:
- ALLOWED_NODE_TYPES, ALLOWED_PREDICATES, RESERVED_LEDGER_CALL_TYPES constants
- GraphNodeRef validates node_type membership
- GraphEdgeRef validates predicate membership
- RefusalError hierarchy (GraphProjectionPolicyError, GraphProjectionBudgetError)
- RepairModeContract Protocol exists
"""

from __future__ import annotations

import pytest


# ─── Constants ───────────────────────────────────────────────────────────────


def test_graph_common_constants_present() -> None:
    from bot.services.graph_common import (
        ALLOWED_NODE_TYPES,
        ALLOWED_PREDICATES,
        RESERVED_LEDGER_CALL_TYPES,
        GraphProjectionMode,
        GraphProjectionRunStatus,
        RepairModeContract,
    )

    # ALLOWED_NODE_TYPES: 9 types per §5.B prompt template
    assert len(ALLOWED_NODE_TYPES) == 9
    assert "Person" in ALLOWED_NODE_TYPES
    assert "KnowledgeCard" in ALLOWED_NODE_TYPES
    assert "Source" in ALLOWED_NODE_TYPES

    # ALLOWED_PREDICATES: 12 predicates per §5.B
    assert len(ALLOWED_PREDICATES) == 12
    assert "MENTIONS" in ALLOWED_PREDICATES
    assert "SUPERSEDES" in ALLOWED_PREDICATES

    # RESERVED_LEDGER_CALL_TYPES: graph_projection (PHASE10_PLAN §5.B step 4)
    # + extract_candidates (Task 10.5-6 bucket rename from 'unknown')
    assert "graph_projection" in RESERVED_LEDGER_CALL_TYPES
    assert "extract_candidates" in RESERVED_LEDGER_CALL_TYPES

    # GraphProjectionMode: exactly the 4 CHECK constraint values from migration 060
    assert GraphProjectionMode.__args__ == ("dry_run", "incremental", "full_rebuild", "repair")

    # GraphProjectionRunStatus: exactly the 6 CHECK constraint values from migration 060
    assert GraphProjectionRunStatus.__args__ == (
        "running", "completed", "failed", "cancelled", "cost_exceeded", "dry_run_complete"
    )

    # RepairModeContract is importable
    assert RepairModeContract is not None


# ─── GraphNodeRef validation ──────────────────────────────────────────────────


def test_graph_node_ref_validates_node_type() -> None:
    from bot.services.graph_common import GraphNodeRef

    # Valid construction
    ref = GraphNodeRef(node_key="key:abc", node_type="Person")
    assert ref.node_key == "key:abc"
    assert ref.node_type == "Person"

    # Invalid node_type raises ValueError
    with pytest.raises(ValueError, match="node_type"):
        GraphNodeRef(node_key="key:abc", node_type="InvalidType")


def test_graph_node_ref_is_frozen() -> None:
    from bot.services.graph_common import GraphNodeRef

    ref = GraphNodeRef(node_key="key:abc", node_type="Topic")
    with pytest.raises(Exception):
        ref.node_key = "different"  # type: ignore[misc]


# ─── GraphEdgeRef validation ──────────────────────────────────────────────────


def test_graph_edge_ref_validates_predicate() -> None:
    from bot.services.graph_common import GraphEdgeRef

    # Valid construction
    edge = GraphEdgeRef(
        edge_key="edge:abc",
        source_key="src:1",
        target_key="tgt:2",
        predicate="MENTIONS",
    )
    assert edge.predicate == "MENTIONS"

    # Invalid predicate raises ValueError
    with pytest.raises(ValueError, match="predicate"):
        GraphEdgeRef(
            edge_key="edge:abc",
            source_key="src:1",
            target_key="tgt:2",
            predicate="INVALID_PREDICATE",
        )


def test_graph_edge_ref_is_frozen() -> None:
    from bot.services.graph_common import GraphEdgeRef

    edge = GraphEdgeRef(
        edge_key="edge:abc",
        source_key="src:1",
        target_key="tgt:2",
        predicate="RELATED_TO",
    )
    with pytest.raises(Exception):
        edge.predicate = "MENTIONS"  # type: ignore[misc]


# ─── Error hierarchy ──────────────────────────────────────────────────────────


def test_refusal_error_hierarchy() -> None:
    from bot.services.graph_common import (
        GraphProjectionBudgetError,
        GraphProjectionPolicyError,
        RefusalError,
    )

    # GraphProjectionPolicyError is-a RefusalError
    policy_err = GraphProjectionPolicyError("governance_policy != normal")
    assert isinstance(policy_err, RefusalError)
    assert isinstance(policy_err, Exception)

    # GraphProjectionBudgetError is-a RefusalError
    budget_err = GraphProjectionBudgetError("cost ceiling exceeded")
    assert isinstance(budget_err, RefusalError)
    assert isinstance(budget_err, Exception)

    # They are distinct types
    assert not isinstance(policy_err, GraphProjectionBudgetError)
    assert not isinstance(budget_err, GraphProjectionPolicyError)


# ─── RepairModeContract Protocol ─────────────────────────────────────────────


def test_repair_mode_contract_protocol_shape() -> None:
    """RepairModeContract declares the repair_mode API surface."""
    import inspect

    from bot.services.graph_common import RepairModeContract

    # Protocol should have request_repair as an abstract method
    assert hasattr(RepairModeContract, "request_repair")
    method = RepairModeContract.request_repair
    sig = inspect.signature(method)
    params = list(sig.parameters.keys())
    # Should have self + keyword params
    assert "self" in params
    assert "source_table" in params
    assert "source_pk" in params
    assert "reason" in params
