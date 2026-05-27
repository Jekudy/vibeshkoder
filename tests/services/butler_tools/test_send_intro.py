"""TDD tests for send_intro tool (T12-06).

Spec: PHASE12_PLAN_REFRESH.md §10 T12-06
- Sends CONFIRMED intro text (from args, NOT re-fetched from DB)
- Target: target_user_id from args
- Cross-user consent must be in args/confirmations — validate_policy raises if missing
- Inverse op: delete_message (posted message_id)
- Privacy: no intro_text logged unmasked
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import ButlerPlanError, SendIntroArgs, ToolResult
from bot.services.butler_tools.send_intro import SendIntroTool
from bot.services.evidence import EvidenceBundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_ID = -100_999_003
REQUESTER_UID = 42
TARGET_UID = 77

_TOOL = SendIntroTool()


def _make_ctx() -> ButlerEvidenceContext:
    bundle = EvidenceBundle(
        query="intro",
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
        query="intro",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


def _make_mock_bot(message_id: int = 101) -> MagicMock:
    bot = MagicMock()
    msg = MagicMock()
    msg.message_id = message_id
    bot.send_message = AsyncMock(return_value=msg)
    return bot


class _FakeSession:
    pass


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_tool_name():
    from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS
    assert _TOOL.name == "send_intro"
    assert _TOOL.name in ALLOWED_BUTLER_TOOLS


def test_schema_version():
    assert _TOOL.schema_version == "v1.0.0"


def test_args_model():
    assert _TOOL.args_model is SendIntroArgs


# ---------------------------------------------------------------------------
# validate_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_policy_passes_valid_args():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="Hello, Alice!")
    await _TOOL.validate_policy(ctx, args)  # should not raise


@pytest.mark.asyncio
async def test_validate_policy_rejects_blank_intro_text():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="   ")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


@pytest.mark.asyncio
async def test_validate_policy_rejects_zero_target_user_id():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=0, intro_text="Hello")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


# ---------------------------------------------------------------------------
# execute — uses confirmation-bound text, does NOT re-fetch from DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_sends_confirmed_intro_text():
    """execute sends the intro_text from args (not re-fetched from DB)."""
    ctx = _make_ctx()
    intro_text = "Hello, this is your confirmed introduction!"
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text=intro_text)
    bot = _make_mock_bot(message_id=202)

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    assert result.success is True
    bot.send_message.assert_called_once()
    # The text sent must include the confirmed intro_text
    call_kwargs = bot.send_message.call_args
    sent_text = call_kwargs.kwargs.get("text") or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else "")
    assert intro_text in sent_text


@pytest.mark.asyncio
async def test_execute_returns_message_id_in_payload():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="Hello!")
    bot = _make_mock_bot(message_id=303)

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    assert result.payload["message_id"] == 303
    assert result.payload["target_user_id"] == TARGET_UID


@pytest.mark.asyncio
async def test_execute_no_db_reads():
    """execute does NOT call session.execute — text comes from args."""
    class _SpySession:
        calls = 0
        async def execute(self, *a, **kw):
            self.calls += 1
            return None

    spy = _SpySession()
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="Hello")
    bot = _make_mock_bot()
    await _TOOL.execute(ctx, args, session=spy, bot=bot)
    assert spy.calls == 0, "send_intro must NOT re-fetch intro_text from DB"


# ---------------------------------------------------------------------------
# build_inverse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_inverse_has_delete_message_kind():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="Hello")
    bot = _make_mock_bot(message_id=404)
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["rollback_kind"] == "delete_message"


@pytest.mark.asyncio
async def test_build_inverse_contains_message_id():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="Hello")
    bot = _make_mock_bot(message_id=505)
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["message_id"] == 505
    assert inverse["target_user_id"] == TARGET_UID


@pytest.mark.asyncio
async def test_build_inverse_deterministic():
    ctx = _make_ctx()
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text="Hello")
    bot = _make_mock_bot(message_id=606)
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    inv1 = await _TOOL.build_inverse(result)
    inv2 = await _TOOL.build_inverse(result)
    assert json.dumps(inv1, sort_keys=True) == json.dumps(inv2, sort_keys=True)


# ---------------------------------------------------------------------------
# Privacy: intro_text must NOT appear in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_payload_no_intro_text():
    """The result payload must NOT contain the raw intro_text (privacy)."""
    ctx = _make_ctx()
    intro_text = "SECRET_INTRO_TEXT_DO_NOT_LOG"
    args = SendIntroArgs(target_user_id=TARGET_UID, intro_text=intro_text)
    bot = _make_mock_bot(message_id=707)

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    payload_str = json.dumps(result.payload)
    assert intro_text not in payload_str
