"""TDD tests for update_intro tool (T12-06).

Spec: PHASE12_PLAN_REFRESH.md §10 T12-06
- Edits ONLY Butler-owned intro messages (verified via butler_actions.message_id lookup)
- If unable to edit → posts a follow-up reply instead of failing
- inverse_op_payload: rollback_kind='edit_message' with prior_text, OR
  'followup_correction' if edit not available
- validate_policy: message_id > 0 and new_intro_text non-blank
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import ButlerPlanError, UpdateIntroArgs, ToolResult
from bot.services.butler_tools.update_intro import UpdateIntroTool
from bot.services.evidence import EvidenceBundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_ID = -100_999_004
REQUESTER_UID = 42

_TOOL = UpdateIntroTool()


def _make_ctx() -> ButlerEvidenceContext:
    bundle = EvidenceBundle(
        query="update intro",
        chat_id=CHAT_ID,
        items=(),
        abstained=True,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )
    ctx_hash = butler_context_hash(bundle, "member", "v1")
    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope="member",
        context_hash=ctx_hash,
        governance_filter_version="v1",
        requester_user_id=REQUESTER_UID,
        chat_id=CHAT_ID,
        query="update intro",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


def _make_bot_edit_ok(prior_text: str = "Old intro text") -> tuple[MagicMock, MagicMock]:
    """Bot that succeeds on edit_message_text, returns msg with prior text."""
    bot = MagicMock()
    edited_msg = MagicMock()
    edited_msg.message_id = 100
    bot.edit_message_text = AsyncMock(return_value=edited_msg)
    bot.send_message = AsyncMock()

    # Repo that confirms Butler ownership
    repo = MagicMock()
    action_row = MagicMock()
    action_row.message_id = 100
    repo.find_by_message_id = AsyncMock(return_value=action_row)

    return bot, repo


def _make_bot_edit_fail() -> tuple[MagicMock, MagicMock]:
    """Bot that fails on edit (timeout / not Butler's message), send_message ok."""
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=Exception("message can't be edited"))
    followup_msg = MagicMock()
    followup_msg.message_id = 200
    bot.send_message = AsyncMock(return_value=followup_msg)

    repo = MagicMock()
    repo.find_by_message_id = AsyncMock(return_value=None)  # not Butler's

    return bot, repo


class _FakeSession:
    pass


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_tool_name():
    from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS
    assert _TOOL.name == "update_intro"
    assert _TOOL.name in ALLOWED_BUTLER_TOOLS


def test_schema_version():
    assert _TOOL.schema_version == "v1.0.0"


def test_args_model():
    assert _TOOL.args_model is UpdateIntroArgs


# ---------------------------------------------------------------------------
# validate_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_policy_passes_valid_args():
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=100, new_intro_text="New intro text")
    await _TOOL.validate_policy(ctx, args)  # should not raise


@pytest.mark.asyncio
async def test_validate_policy_rejects_blank_new_intro_text():
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=100, new_intro_text="   ")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


@pytest.mark.asyncio
async def test_validate_policy_rejects_zero_message_id():
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=0, new_intro_text="New text")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


# ---------------------------------------------------------------------------
# execute — happy path: edit succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_edit_success():
    """When edit_message_text succeeds, result is success with edit outcome."""
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=100, new_intro_text="Updated intro")
    bot, repo = _make_bot_edit_ok()

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)

    assert result.success is True
    assert result.payload["outcome"] == "edited"
    assert result.payload["message_id"] == 100


@pytest.mark.asyncio
async def test_execute_edit_success_does_not_raise():
    """Not-Butler's-message scenario → fallback to follow-up reply (no exception)."""
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=999, new_intro_text="Updated intro")
    bot, repo = _make_bot_edit_fail()

    # Should NOT raise — falls back to followup reply
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)
    assert result.success is True
    assert result.payload["outcome"] == "followup_reply"


@pytest.mark.asyncio
async def test_execute_followup_reply_posts_to_chat():
    """When edit fails, a followup message is sent to the chat."""
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=999, new_intro_text="Updated intro")
    bot, repo = _make_bot_edit_fail()

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)

    bot.send_message.assert_called_once()
    assert result.payload["outcome"] == "followup_reply"
    assert "followup_message_id" in result.payload


# ---------------------------------------------------------------------------
# build_inverse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_inverse_edit_outcome_is_edit_message():
    """When outcome=edited, inverse is edit_message with prior_text slot."""
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=100, new_intro_text="Updated intro")
    bot, repo = _make_bot_edit_ok()
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["rollback_kind"] == "edit_message"
    assert inverse["message_id"] == 100


@pytest.mark.asyncio
async def test_build_inverse_followup_outcome_is_followup_correction():
    """When outcome=followup_reply, inverse is followup_correction."""
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=999, new_intro_text="Updated intro")
    bot, repo = _make_bot_edit_fail()
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["rollback_kind"] == "followup_correction"


@pytest.mark.asyncio
async def test_build_inverse_deterministic():
    ctx = _make_ctx()
    args = UpdateIntroArgs(message_id=100, new_intro_text="New text")
    bot, repo = _make_bot_edit_ok()
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)

    inv1 = await _TOOL.build_inverse(result)
    inv2 = await _TOOL.build_inverse(result)
    assert json.dumps(inv1, sort_keys=True) == json.dumps(inv2, sort_keys=True)


# ---------------------------------------------------------------------------
# Privacy: new_intro_text not in payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_payload_no_intro_text():
    ctx = _make_ctx()
    intro_text = "SECRET_NEW_INTRO_DO_NOT_LOG"
    args = UpdateIntroArgs(message_id=100, new_intro_text=intro_text)
    bot, repo = _make_bot_edit_ok()

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot, action_repo=repo)

    payload_str = json.dumps(result.payload)
    assert intro_text not in payload_str
