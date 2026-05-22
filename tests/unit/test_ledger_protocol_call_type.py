"""Unit tests for Task 10.5-1 — LedgerProtocol typed call_type contract.

Verifies:
1. LedgerRepoProtocol.daily_cost_usd has a call_type parameter (str | None = None)
2. LedgerRepo.daily_cost_usd accepts call_type kwarg without TypeError
3. graph_projector._check_graph_budget does NOT use inspect.signature duck-typing
   (the call_type kwarg should be passed directly, not conditionally)

These are static structural tests — no DB required.
"""
from __future__ import annotations

import inspect


def test_ledger_repo_protocol_daily_cost_usd_has_call_type() -> None:
    """LedgerRepoProtocol.daily_cost_usd must declare call_type parameter."""
    from bot.services.llm_gateway import LedgerRepoProtocol

    sig = inspect.signature(LedgerRepoProtocol.daily_cost_usd)
    assert "call_type" in sig.parameters, (
        "LedgerRepoProtocol.daily_cost_usd must declare 'call_type: str | None = None' "
        "so graph_projector can pass call_type='graph_projection' without duck-typing. "
        "Task 10.5-1."
    )
    param = sig.parameters["call_type"]
    # default must be None (not 'unknown' — for daily_cost_usd None means all call types)
    assert param.default is None, (
        f"LedgerRepoProtocol.daily_cost_usd call_type default must be None, "
        f"got {param.default!r}"
    )


def test_ledger_repo_daily_cost_usd_accepts_call_type_kwarg() -> None:
    """LedgerRepo.daily_cost_usd signature must include call_type kwarg."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    sig = inspect.signature(LedgerRepo.daily_cost_usd)
    assert "call_type" in sig.parameters, (
        "LedgerRepo.daily_cost_usd must accept call_type kwarg to match "
        "LedgerRepoProtocol. Task 10.5-1."
    )
    param = sig.parameters["call_type"]
    assert param.default is None, (
        f"LedgerRepo.daily_cost_usd call_type default must be None, got {param.default!r}"
    )


def test_graph_projector_does_not_use_inspect_duck_typing() -> None:
    """graph_projector._check_graph_budget must NOT use inspect.signature fallback.

    After Task 10.5-1, LedgerRepoProtocol guarantees call_type is available —
    the conditional duck-typing workaround is no longer needed.
    """
    import ast
    from pathlib import Path

    graph_projector_path = (
        Path(__file__).resolve().parents[2]
        / "bot" / "services" / "graph_projector.py"
    )
    source = graph_projector_path.read_text(encoding="utf-8")

    # The function _check_graph_budget should NOT contain 'inspect.signature'
    # After the fix, it calls ledger_repo.daily_cost_usd(..., call_type=...) directly.
    tree = ast.parse(source, filename=str(graph_projector_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_check_graph_budget":
            func_source = ast.get_source_segment(source, node) or ""
            assert "inspect.signature" not in func_source, (
                "_check_graph_budget must not use inspect.signature duck-typing. "
                "LedgerRepoProtocol now guarantees call_type kwarg exists. "
                "Replace the conditional with a direct call. Task 10.5-1."
            )
            return

    # If we reach here, _check_graph_budget was not found — fail explicitly
    raise AssertionError(
        "_check_graph_budget not found in graph_projector.py — "
        "check if the function was renamed or removed."
    )
