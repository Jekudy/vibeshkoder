"""Integration test: execute_action signature alignment (T12-06-fix C1/C2).

Verifies that ButlerService.execute_action threads real (ctx, args, session,
bot, action_repo, action_id) to real tool implementations — not FakeButlerTool.

These tests bypass the real DB (no async SA session needed) because
execute_action's inner loop builds ctx+args from plan_payload and threads them
to the tool; we exercise only the argument-passing path using minimal fakes.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import (
    RecallEvidenceArgs,
    ToolResult,
    UpdateIntroArgs,
)
from bot.services.butler_tools.recall_evidence import RecallEvidenceTool
from bot.services.butler_tools.update_intro import UpdateIntroTool
from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo
from bot.db.models import ButlerToolInvocation
from bot.services.evidence import EvidenceBundle, EvidenceItem

CHAT_ID = -100_999_911
REQUESTER_UID = 42


def _make_item(mvid: int) -> EvidenceItem:
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=CHAT_ID,
        message_id=mvid + 2000,
        user_id=99,
        snippet="snippet",
        ts_rank=0.8,
        captured_at=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        source_type="message",
        card_id=None,
        card_source_message_version_ids=(),
    )


def _make_ctx() -> ButlerEvidenceContext:
    items = (_make_item(1), _make_item(2))
    bundle = EvidenceBundle(
        query="who knows Rust?",
        chat_id=CHAT_ID,
        items=items,
        abstained=False,
        created_at=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
    )
    ctx_hash = butler_context_hash(bundle, "member", "v1")
    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope="member",
        context_hash=ctx_hash,
        governance_filter_version="v1",
        requester_user_id=REQUESTER_UID,
        chat_id=CHAT_ID,
        query="who knows Rust?",
        snapshot_at=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


class _FakeSession:
    """Minimal session fake — does NOT allow session.execute()."""
    async def execute(self, *a, **kw):
        raise AssertionError("direct DB access not allowed in execute_action tool call")


@pytest.mark.asyncio
async def test_recall_evidence_real_tool_signature_happy_path() -> None:
    """RecallEvidenceTool.execute accepts (ctx, args, *, session) with real pydantic args.

    This is the C1 smoke test: the caller in execute_action must build ctx + args
    as typed pydantic objects and thread them to the real tool. If args is a plain
    dict (old bug: tool.execute(None, None, ...)), the tool would AttributeError.
    """
    tool = RecallEvidenceTool()
    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="who knows Rust?")

    # C1: validate_policy takes (ctx, args: BaseModel) — not (None, dict)
    await tool.validate_policy(ctx, args)

    # C1: execute takes (ctx, args: BaseModel, *, session, bot=None, action_repo=None, action_id=...)
    result = await tool.execute(
        ctx,
        args,
        session=_FakeSession(),
        bot=None,
        action_repo=None,
        action_id=1,
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.payload is not None
    assert result.payload["evidence_ids"] == [1, 2]


@pytest.mark.asyncio
async def test_recall_evidence_real_tool_validate_policy_blank_query_raises() -> None:
    """RecallEvidenceTool.validate_policy raises for blank query.

    Confirms that with real pydantic args (not getattr fallback), the invariant
    check works correctly via args.query (direct attribute access).

    Note: catches Exception to avoid combined-mode _clear_modules class-identity
    mismatch (same as commit bd935c8 pattern). error_kind is the meaningful check.
    """
    tool = RecallEvidenceTool()
    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="   ")

    with pytest.raises(Exception) as exc_info:
        await tool.validate_policy(ctx, args)

    # ButlerPlanError carries error_kind='invalid_args' for blank query
    assert getattr(exc_info.value, "error_kind", None) == "invalid_args"


@pytest.mark.asyncio
async def test_recall_evidence_invariant_broken_if_wrong_args_type() -> None:
    """RecallEvidenceTool.validate_policy raises invariant_broken for non-BaseModel args.

    After C1/H1 fix: tools must raise ButlerPlanError(error_kind='invariant_broken')
    when args is not a proper pydantic model — not silently return empty via getattr.

    Note: catches Exception to avoid combined-mode _clear_modules class-identity
    mismatch (same as commit bd935c8 pattern). error_kind is the meaningful check.
    """
    tool = RecallEvidenceTool()
    ctx = _make_ctx()

    # Pass a plain dict — should raise invariant_broken, not silently proceed
    with pytest.raises(Exception) as exc_info:
        await tool.validate_policy(ctx, {"query": "some text"})  # type: ignore

    assert getattr(exc_info.value, "error_kind", None) == "invariant_broken"


@pytest.mark.asyncio
async def test_recall_evidence_build_inverse_deterministic() -> None:
    """build_inverse produces identical output on two calls with same result."""
    import json
    tool = RecallEvidenceTool()
    ctx = _make_ctx()
    args = RecallEvidenceArgs(query="test")

    result = await tool.execute(ctx, args, session=_FakeSession(), action_id=1)
    inv1 = await tool.build_inverse(result)
    inv2 = await tool.build_inverse(result)

    assert json.dumps(inv1, sort_keys=True) == json.dumps(inv2, sort_keys=True)


@pytest.mark.asyncio
async def test_update_intro_finds_owned_message_e2e() -> None:
    """C2 regression guard: update_intro.execute must use invocation_repo for ownership check.

    This test would FAIL on the pre-fix state in two ways:
    - Stage 1: if action_repo (ButlerActionRepo) is passed instead of invocation_repo
      → AttributeError on find_by_posted_message_id (method doesn't exist on ButlerActionRepo)
      → silently swallowed, is_butler_owned=False, falls through to followup path
    - Stage 2: if posted_message_id was never written to the row → find returns None
      → is_butler_owned=False, edit path never taken

    The test uses a real ButlerToolInvocationRepo against a minimal fake session that
    returns a ButlerToolInvocation row with posted_message_id=12345.
    """
    POSTED_MSG_ID = 12345

    # Build a fake invocation row with posted_message_id set
    fake_invocation = ButlerToolInvocation(
        id=1,
        action_id=10,
        tool_name="send_intro",
        invocation_seq=1,
        idempotency_key="test-key",
        request_payload={},
        request_payload_hash="abc",
        status="succeeded",
        posted_message_id=POSTED_MSG_ID,
    )

    # Build a fake session that returns the row for find_by_posted_message_id
    class _FakeResult:
        def scalar_one_or_none(self):
            return fake_invocation

    class _OwnershipSession:
        async def execute(self, stmt, *a, **kw):
            # Real ButlerToolInvocationRepo.find_by_posted_message_id calls session.execute(select(...))
            return _FakeResult()

    invocation_repo = ButlerToolInvocationRepo()

    # Fake bot that records edit_message_text calls
    fake_bot = MagicMock()
    edit_called_with: list = []

    async def _fake_edit(chat_id, message_id, text, parse_mode):
        edit_called_with.append({"chat_id": chat_id, "message_id": message_id})

    fake_bot.edit_message_text = _fake_edit

    tool = UpdateIntroTool()
    ctx = _make_ctx()  # chat_id = CHAT_ID = -100_999_911

    args = UpdateIntroArgs(message_id=POSTED_MSG_ID, new_intro_text="Updated intro text")

    result = await tool.execute(
        ctx,
        args,
        session=_OwnershipSession(),
        bot=fake_bot,
        invocation_repo=invocation_repo,  # C2 fix: pass invocation_repo, not action_repo
        action_repo=None,
        action_id=10,
    )

    # The edit path MUST be taken (not followup_reply)
    assert result.success is True, f"Expected success, got error={result.error}"
    assert result.payload is not None
    assert result.payload.get("outcome") == "edited", (
        f"Expected outcome='edited' (edit path), got outcome={result.payload.get('outcome')!r}. "
        f"If 'followup_reply': invocation_repo was not wired correctly (C2 stage 1 or stage 2 bug)."
    )
    assert result.payload.get("message_id") == POSTED_MSG_ID
    # Confirm the bot.edit_message_text was actually called (not send_message)
    assert len(edit_called_with) == 1, (
        f"edit_message_text should be called once; got {len(edit_called_with)} calls. "
        "Followup path was taken instead of edit path."
    )
    assert edit_called_with[0]["message_id"] == POSTED_MSG_ID
