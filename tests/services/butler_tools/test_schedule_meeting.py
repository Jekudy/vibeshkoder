"""TDD tests for schedule_meeting tool (T12-06).

Spec: PHASE12_PLAN_REFRESH.md §10 T12-06
- Posts a Telegram-native text-only proposal to the originating chat
- NO calendar API calls
- Returns posted message_id in inverse_op_payload
- inverse_op_payload: rollback_kind='delete_message', chat_id + message_id
- idempotency: re-running on succeeded invocation returns same payload
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash
from bot.services.butler_tools import ButlerPlanError, ScheduleMeetingArgs, ToolResult
from bot.services.butler_tools.schedule_meeting import ScheduleMeetingTool
from bot.services.evidence import EvidenceBundle, EvidenceItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_ID = -100_999_002
REQUESTER_UID = 42

_TOOL = ScheduleMeetingTool()


def _make_ctx() -> ButlerEvidenceContext:
    bundle = EvidenceBundle(
        query="meeting about Rust",
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
        query="meeting about Rust",
        snapshot_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


def _make_mock_bot(message_id: int = 777) -> MagicMock:
    """Return a mock aiogram Bot that returns message_id when send_message called."""
    bot = MagicMock()
    msg = MagicMock()
    msg.message_id = message_id
    bot.send_message = AsyncMock(return_value=msg)
    return bot


class _FakeSession:
    pass


# ---------------------------------------------------------------------------
# Registry checks
# ---------------------------------------------------------------------------


def test_tool_name():
    from bot.services.butler_tools import ALLOWED_BUTLER_TOOLS
    assert _TOOL.name == "schedule_meeting"
    assert _TOOL.name in ALLOWED_BUTLER_TOOLS


def test_tool_schema_version():
    assert _TOOL.schema_version == "v1.0.0"


def test_tool_args_model():
    assert _TOOL.args_model is ScheduleMeetingArgs


# ---------------------------------------------------------------------------
# validate_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_policy_passes_valid_args():
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="Rust architecture")
    await _TOOL.validate_policy(ctx, args)  # should not raise


@pytest.mark.asyncio
async def test_validate_policy_rejects_blank_topic():
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="   ")
    with pytest.raises(ButlerPlanError):
        await _TOOL.validate_policy(ctx, args)


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_posts_telegram_message():
    """execute calls bot.send_message and returns success with message_id."""
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="Rust architecture", proposed_time_text="Friday 15:00")
    bot = _make_mock_bot(message_id=888)

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    assert result.success is True
    bot.send_message.assert_called_once()
    call_kwargs = bot.send_message.call_args
    # Should post to originating chat
    assert call_kwargs.kwargs.get("chat_id") == CHAT_ID or call_kwargs.args[0] == CHAT_ID


@pytest.mark.asyncio
async def test_execute_returns_message_id_in_payload():
    """execute returns the posted Telegram message_id in payload."""
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="Rust architecture")
    bot = _make_mock_bot(message_id=999)

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    assert result.payload["message_id"] == 999
    assert result.payload["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_execute_no_calendar_api_calls():
    """execute must NOT call any external calendar API."""
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="Meeting test")
    bot = _make_mock_bot(message_id=111)

    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    # Only send_message should be called on the bot
    assert bot.send_message.call_count == 1
    # No calendar-specific method was called (check the mock call list)
    called_methods = [call[0] for call in bot.method_calls]
    assert "create_calendar_event" not in called_methods
    assert "create_event" not in called_methods


# ---------------------------------------------------------------------------
# build_inverse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_inverse_has_delete_message_kind():
    """build_inverse returns rollback_kind='delete_message'."""
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="test")
    bot = _make_mock_bot(message_id=555)
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["rollback_kind"] == "delete_message"


@pytest.mark.asyncio
async def test_build_inverse_contains_message_id_and_chat_id():
    """inverse_op_payload contains posted message_id and chat_id."""
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="test")
    bot = _make_mock_bot(message_id=444)
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    inverse = await _TOOL.build_inverse(result)
    assert inverse["message_id"] == 444
    assert inverse["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_build_inverse_is_deterministic():
    """Same result produces identical inverse payload bytes."""
    ctx = _make_ctx()
    args = ScheduleMeetingArgs(topic="test")
    bot = _make_mock_bot(message_id=222)
    result = await _TOOL.execute(ctx, args, session=_FakeSession(), bot=bot)

    inv1 = await _TOOL.build_inverse(result)
    inv2 = await _TOOL.build_inverse(result)
    assert json.dumps(inv1, sort_keys=True) == json.dumps(inv2, sort_keys=True)


# ---------------------------------------------------------------------------
# Schema validation (pydantic rejects bad args)
# ---------------------------------------------------------------------------


def test_args_model_rejects_missing_topic():
    with pytest.raises(Exception):
        ScheduleMeetingArgs()  # type: ignore[call-arg] — topic required
