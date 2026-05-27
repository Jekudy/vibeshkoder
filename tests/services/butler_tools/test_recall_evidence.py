"""TDD tests for recall_evidence tool (T12-06).

Spec: PHASE12_PLAN_REFRESH.md §10 T12-06
- Delegates to ButlerEvidenceContext only (no direct DB reads)
- Returns the sealed EvidenceBundle in payload
- inverse_op_payload is 'not_reversible' (no Telegram side effect to undo)
- idempotency: re-running on a succeeded action returns prior result
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import RecallEvidenceArgs, ToolResult
from bot.services.butler_tools.recall_evidence import RecallEvidenceTool
from bot.services.evidence import EvidenceBundle, EvidenceItem


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

CHAT_ID = -100_999_001

_TOOL = RecallEvidenceTool()


def _make_item(mvid: int) -> EvidenceItem:
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=CHAT_ID,
        message_id=mvid + 2000,
        user_id=99,
        snippet="snippet content",
        ts_rank=0.8,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        source_type="message",
        card_id=None,
        card_source_message_version_ids=(),
    )


def _make_ctx(mvids: tuple[int, ...] = (1, 2)) -> ButlerEvidenceContext:
    items = tuple(_make_item(m) for m in mvids)
    bundle = EvidenceBundle(
        query="who knows Python?",
        chat_id=CHAT_ID,
        items=items,
        abstained=False,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    ctx_hash = butler_context_hash(bundle, "member", "v1")
    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope="member",
        context_hash=ctx_hash,
        governance_filter_version="v1",
        requester_user_id=42,
        chat_id=CHAT_ID,
        query="who knows Python?",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


class _FakeSession:
    pass


# ---------------------------------------------------------------------------
# Registry checks
# ---------------------------------------------------------------------------


def test_tool_name_in_allowed_butler_tools():
    """RecallEvidenceTool.name is 'recall_evidence' and in ALLOWED_BUTLER_TOOLS."""
    from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS

    assert _TOOL.name == "recall_evidence"
    assert _TOOL.name in ALLOWED_BUTLER_TOOLS


def test_tool_schema_version():
    assert _TOOL.schema_version == "v1.0.0"


def test_tool_args_model():
    assert _TOOL.args_model is RecallEvidenceArgs


# ---------------------------------------------------------------------------
# validate_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_policy_passes_with_valid_args():
    """validate_policy does not raise for valid args."""
    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="who knows Python?")
    # should not raise
    await _TOOL.validate_policy(ctx, args)


@pytest.mark.asyncio
async def test_validate_policy_rejects_empty_query():
    """validate_policy raises ButlerPlanError for empty query."""
    from bot.services.butler_tools import ButlerPlanError

    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="   ")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_success_with_evidence_ids():
    """execute returns ToolResult with success=True and evidence_ids in payload."""
    ctx = _make_ctx((10, 20))
    args = RecallEvidenceArgs(query="who knows Python?")
    result = await _TOOL.execute(ctx, args, session=_FakeSession())

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.payload is not None
    assert result.payload["evidence_ids"] == [10, 20]


@pytest.mark.asyncio
async def test_execute_payload_has_context_hash():
    """execute payload includes context_hash from the sealed ctx."""
    ctx = _make_ctx((5,))
    args = RecallEvidenceArgs(query="something")
    result = await _TOOL.execute(ctx, args, session=_FakeSession())

    assert result.payload["context_hash"] == ctx.context_hash


@pytest.mark.asyncio
async def test_execute_no_direct_db_calls():
    """execute does NOT call session.execute — all reads go through ctx."""
    class _SpySession:
        calls = 0

        async def execute(self, *a, **kw):
            self.calls += 1
            return None

    spy = _SpySession()
    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="test")
    await _TOOL.execute(ctx, args, session=spy)
    assert spy.calls == 0, "recall_evidence must not issue direct DB queries"


# ---------------------------------------------------------------------------
# build_inverse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_inverse_is_not_reversible():
    """build_inverse returns rollback_kind='not_reversible' (no Telegram effect)."""
    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="test")
    result = await _TOOL.execute(ctx, args, session=_FakeSession())
    inverse = await _TOOL.build_inverse(result)

    assert inverse["rollback_kind"] == "not_reversible"


@pytest.mark.asyncio
async def test_build_inverse_is_deterministic():
    """Same ToolResult always produces identical inverse_op_payload bytes."""
    ctx = _make_ctx((7, 8))
    args = RecallEvidenceArgs(query="test")
    result = await _TOOL.execute(ctx, args, session=_FakeSession())

    inv1 = await _TOOL.build_inverse(result)
    inv2 = await _TOOL.build_inverse(result)

    assert json.dumps(inv1, sort_keys=True) == json.dumps(inv2, sort_keys=True)


# ---------------------------------------------------------------------------
# Privacy: no user content in result payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_payload_no_snippet_text():
    """execute payload does NOT include raw snippet text (privacy rule)."""
    ctx = _make_ctx((3,))
    args = RecallEvidenceArgs(query="who knows Python?")
    result = await _TOOL.execute(ctx, args, session=_FakeSession())

    payload_str = json.dumps(result.payload)
    assert "snippet content" not in payload_str
