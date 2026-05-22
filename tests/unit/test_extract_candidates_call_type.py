"""Unit tests for Task 10.5-6 — extract_candidates call_type bucket rename.

Verifies that extract_candidates uses call_type='extract_candidates' not 'unknown'.

Phase 5 cost-bucket policy: every gateway call site must use a named bucket.
'unknown' is a legacy default; explicit named buckets allow per-feature cost reporting.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LLM_GATEWAY_PATH = REPO_ROOT / "bot" / "services" / "llm_gateway.py"


def _get_extract_candidates_body() -> str:
    """Return the source of the extract_candidates function."""
    source = LLM_GATEWAY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(LLM_GATEWAY_PATH))

    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if node.name == "extract_candidates":
                body = ast.get_source_segment(source, node)
                if body:
                    return body
    raise AssertionError(
        "extract_candidates function not found in llm_gateway.py — "
        "check if it was renamed."
    )


def test_extract_candidates_does_not_use_unknown_call_type() -> None:
    """extract_candidates must NOT pass call_type='unknown' to ledger.record().

    Phase 5 cost-bucket policy requires every gateway call site to use a named
    bucket. 'unknown' is only acceptable as a fallback default; extraction calls
    must identify themselves as 'extract_candidates'.

    Task 10.5-6.
    """
    body = _get_extract_candidates_body()
    assert "call_type=\"unknown\"" not in body and "call_type='unknown'" not in body, (
        "extract_candidates uses call_type='unknown' — rename to "
        "call_type='extract_candidates' to comply with Phase 5 cost-bucket policy. "
        "Task 10.5-6."
    )


def test_extract_candidates_uses_named_call_type_bucket() -> None:
    """extract_candidates must use call_type='extract_candidates' bucket."""
    body = _get_extract_candidates_body()
    assert (
        "call_type=\"extract_candidates\"" in body
        or "call_type='extract_candidates'" in body
    ), (
        "extract_candidates must set call_type='extract_candidates' in ledger.record(). "
        "This enables per-feature cost reporting in the llm_usage_ledger. Task 10.5-6."
    )
