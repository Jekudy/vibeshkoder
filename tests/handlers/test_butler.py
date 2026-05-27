"""T12-05 acceptance tests for bot/handlers/butler.py.

TDD — tests written and run (red) before/alongside handler implementation.

Test inventory
--------------
1.  feature_flag_off_no_service_call
    Flag OFF → handler returns immediately, no ButlerService interaction.

2.  non_member_rejection
    Non-member invokes /butler → polite refusal, no service call.

3.  non_member_rejection_user_none
    UserRepo returns None → polite refusal, no service call.

4.  dm_only_not_enforced_by_route
    /butler in group chat still processes if filter passes
    (PrivateChatFilter is tested separately via integration; here we test
    the handler's internal flag+auth checks work correctly for DM).

5.  empty_query_rejection
    /butler (no args) → usage hint.

6.  command_happy_path
    /butler <query> → plan_action called with correct args, preview sent,
    keyboard has Confirm/Cancel buttons.

7.  callback_expired_token_rejection
    Confirmation callback with wrong token → "Confirmation expired" alert.

8.  callback_expired_ttl
    Confirmation callback on expired action → alert.

9.  cross_user_consent_flow_approve
    plan_action returns action with affected_user → consent DM sent,
    affected user approves → confirm_action called.

10. cross_user_consent_flow_reject
    Affected user rejects → cancel_action called.

11. butler_cancel_command_happy
    /butler_cancel <id> → cancel_action called, success reply.

12. butler_cancel_forbidden
    Non-owner /butler_cancel → forbidden message.

13. butler_status_happy
    /butler_status <id> → status reply.

14. butler_undo_stub
    /butler_undo → stub message.

15. rate_limit_exceeded_path (§5.B path 2)
    plan_action raises rate_limit_exceeded → polite user message.

16. plan_error_path (§5.B path 8)
    plan_action raises generic ButlerActionError → plan error message.

17. cascade_in_flight_path
    confirm_action raises CascadeInFlightError → polite system-busy message.

18. evidence_stale_path (§5.B stale evidence)
    confirm_action raises EvidenceStaleError → stale data message.

Combined mode: these tests can run alongside tests/services/test_butler_state_machine.py
without class identity failures (module-level imports in handler + test).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")


# ---------------------------------------------------------------------------
# Helpers: fake ORM rows
# ---------------------------------------------------------------------------


def _random_tg_id() -> int:
    return random.randint(900_000_000, 999_999_999)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_user(tg_id: int, *, is_member: bool = True, is_admin: bool = False) -> MagicMock:
    user = MagicMock()
    user.id = tg_id
    user.is_member = is_member
    user.is_admin = is_admin
    return user


@dataclass
class FakeButlerAction:
    id: int
    requester_tg_id: int
    chat_id: int = 0
    action_type: str = "recall"
    status: str = "pending_confirmation"
    tool_name: str = "recall_evidence"
    tool_manifest_version: str = "v1.0.0"
    governance_filter_version: str = "test-v1"
    evidence_context_hash: str = "abc123"
    evidence_ids: list = field(default_factory=list)
    plan_summary: str = "Recall members who know Rust"
    action_args: dict = field(default_factory=dict)
    action_args_hash: str = ""
    rollback_kind: str = "not_reversible"
    risk_level: str = "low"
    requires_confirmation: bool = True
    confirmation_policy: str = "per_action"
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None
    llm_usage_ledger_id: int | None = None
    rejection_reason: str | None = None
    error_code: str | None = None
    query: str = "who knows Rust?"
    visibility_scope: str = "member"
    plan_payload: dict = field(default_factory=dict)


@dataclass
class FakeConfirmation:
    id: int
    action_id: int
    confirmer_tg_id: int
    confirmation_role: str
    status: str
    preview_payload_hash: str
    expires_at: datetime
    confirmation_token: str = "valid-token-abc123"
    confirmed_at: datetime | None = None
    rejected_at: datetime | None = None
    confirmation_message_chat_id: int | None = None
    confirmation_message_id: int | None = None
    created_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Helpers: fake aiogram objects
# ---------------------------------------------------------------------------


def _make_message(
    *,
    user_id: int | None = None,
    text: str = "/butler who knows Rust?",
    chat_type: str = "private",
    chat_id: int = 123456789,
    args: str | None = None,
    bot: Any = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    """Return (message, command) matching aiogram handler signatures."""
    uid = user_id or _random_tg_id()
    msg = SimpleNamespace(
        message_id=random.randint(1, 100_000),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=uid),
        text=text,
        reply=AsyncMock(),
        bot=bot,
    )
    cmd = SimpleNamespace(args=args or "who knows Rust?")
    return msg, cmd


def _make_callback(
    *,
    user_id: int | None = None,
    data: str = "butler_confirm:1:valid-token-abc123",
    message_text: str = "preview text",
) -> SimpleNamespace:
    uid = user_id or _random_tg_id()
    msg = SimpleNamespace(
        text=message_text,
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
        reply=AsyncMock(),
    )
    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        data=data,
        message=msg,
        answer=AsyncMock(),
    )
    return cb


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Test 1: Feature flag OFF → no service call
# ---------------------------------------------------------------------------


def test_feature_flag_off_no_service_call(app_env, monkeypatch) -> None:
    """When memory.butler.enabled=False, handler returns immediately — no service call."""
    handler = import_module("bot.handlers.butler")

    message, command = _make_message()
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=False)
    )
    mock_user_get = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "get", mock_user_get)

    asyncio.run(handler.handle_butler(message, command, session))

    mock_user_get.assert_not_awaited()
    message.reply.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 2: Non-member rejection (user.is_member=False, is_admin=False)
# ---------------------------------------------------------------------------


def test_non_member_rejection(app_env, monkeypatch) -> None:
    """Non-member → SILENT rejection (H3: no confirmation bot exists)."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    non_member = _make_user(uid, is_member=False, is_admin=False)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=non_member))

    mock_build = MagicMock()
    monkeypatch.setattr(handler, "_build_butler_service", mock_build)

    asyncio.run(handler.handle_butler(message, command, session))

    mock_build.assert_not_called()
    # H3: non-members are silently rejected — no reply
    message.reply.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 3: Non-member rejection — UserRepo returns None
# ---------------------------------------------------------------------------


def test_non_member_rejection_user_none(app_env, monkeypatch) -> None:
    """UserRepo.get returns None → SILENT rejection (H3: no confirmation bot exists)."""
    handler = import_module("bot.handlers.butler")

    message, command = _make_message()
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=None))

    asyncio.run(handler.handle_butler(message, command, session))

    # H3: non-members are silently rejected — no reply
    message.reply.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 4: Empty query → usage hint
# ---------------------------------------------------------------------------


def test_empty_query_usage_hint(app_env, monkeypatch) -> None:
    """No args → usage hint message."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid, args="")
    command.args = ""
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    asyncio.run(handler.handle_butler(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "/butler" in reply_text


# ---------------------------------------------------------------------------
# Test 5: Command happy path
# ---------------------------------------------------------------------------


def test_command_happy_path(app_env, monkeypatch) -> None:
    """plan_action called with correct args → preview sent with Confirm/Cancel keyboard."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    action_id = 42
    message, command = _make_message(user_id=uid)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    requester_conf = FakeConfirmation(
        id=1,
        action_id=action_id,
        confirmer_tg_id=uid,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="hash1",
        expires_at=_now() + timedelta(minutes=5),
        confirmation_token="requester-token-xyz",
    )

    mock_butler = MagicMock()
    mock_butler.plan_action = AsyncMock(return_value=fake_action)
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    monkeypatch.setattr(
        handler.ButlerActionConfirmationRepo,
        "list_for_action",
        AsyncMock(return_value=[requester_conf]),
    )

    asyncio.run(handler.handle_butler(message, command, session))

    mock_butler.plan_action.assert_awaited_once()
    call_kwargs = mock_butler.plan_action.call_args.kwargs
    assert call_kwargs["requester_user_id"] == uid
    assert call_kwargs["query"] == "who knows Rust?"
    assert call_kwargs["visibility_scope"] == "member"

    # Preview message sent
    message.reply.assert_awaited_once()
    reply_kwargs = message.reply.call_args
    # Keyboard should have confirm/cancel buttons
    markup = reply_kwargs.kwargs.get("reply_markup")
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    row = markup.inline_keyboard[0]
    assert any("butler_confirm" in btn.callback_data for btn in row)
    assert any("butler_cancel_cb" in btn.callback_data for btn in row)

    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 6: Callback expired token rejection
# ---------------------------------------------------------------------------


def test_callback_expired_token_rejection(app_env, monkeypatch) -> None:
    """Wrong token on confirmation callback → bad_token alert."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionError

    uid = _random_tg_id()
    action_id = 99
    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)

    monkeypatch.setattr(
        handler.ButlerActionRepo,
        "get",
        AsyncMock(return_value=fake_action),
    )

    mock_butler = MagicMock()
    mock_butler.confirm_action = AsyncMock(
        side_effect=ButlerActionError(
            "confirmation_token mismatch",
            error_kind="bad_token",
            action_id=action_id,
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    callback = _make_callback(
        user_id=uid,
        data=f"butler_confirm:{action_id}:wrong-token",
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_confirm_callback(callback, session))

    callback.answer.assert_awaited()
    # Check that some answer was called with a meaningful message
    calls = callback.answer.call_args_list
    # Last call should be the alert with bad token message
    alert_calls = [c for c in calls if c.kwargs.get("show_alert")]
    assert len(alert_calls) >= 1
    alert_text = alert_calls[-1].args[0]
    assert "недействит" in alert_text.lower() or "token" in alert_text.lower() or "ссылка" in alert_text.lower()


# ---------------------------------------------------------------------------
# Test 7: Callback on expired action (TTL exceeded)
# ---------------------------------------------------------------------------


def test_callback_expired_ttl(app_env, monkeypatch) -> None:
    """ButlerActionExpiredError on confirm_action → expired alert, keyboard removed."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionExpiredError

    uid = _random_tg_id()
    action_id = 77

    fake_action = FakeButlerAction(
        id=action_id,
        requester_tg_id=uid,
        status="expired",
        expires_at=_now() - timedelta(minutes=10),
    )

    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )
    mock_butler = MagicMock()
    mock_butler.confirm_action = AsyncMock(
        side_effect=ButlerActionExpiredError(
            "TTL expired", error_kind="expired", action_id=action_id
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    callback = _make_callback(
        user_id=uid, data=f"butler_confirm:{action_id}:any-token"
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_confirm_callback(callback, session))

    alert_calls = [
        c for c in callback.answer.call_args_list if c.kwargs.get("show_alert")
    ]
    assert len(alert_calls) >= 1
    msg = alert_calls[-1].args[0]
    assert "истёк" in msg.lower() or "истек" in msg.lower() or "повторите" in msg.lower()


# ---------------------------------------------------------------------------
# Test 8: Cross-user consent flow — approve
# ---------------------------------------------------------------------------


def test_cross_user_consent_flow_approve(app_env, monkeypatch) -> None:
    """Affected user approves → confirm_action called with affected user's token."""
    handler = import_module("bot.handlers.butler")

    affected_uid = _random_tg_id()
    action_id = 55

    mock_butler = MagicMock()
    mock_butler.confirm_action = AsyncMock(return_value=MagicMock(status="confirmed"))
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    affected_token = "affected-user-token-xyz"
    callback = _make_callback(
        user_id=affected_uid,
        data=f"butler_affected_approve:{action_id}:{affected_token}",
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_affected_approve(callback, session))

    mock_butler.confirm_action.assert_awaited_once_with(
        action_id=action_id,
        confirming_user_id=affected_uid,
        confirmation_token=affected_token,
    )
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 9: Cross-user consent flow — reject
# ---------------------------------------------------------------------------


def test_cross_user_consent_flow_reject(app_env, monkeypatch) -> None:
    """Affected user rejects → revoke_affected_user_consent called (C1 fix)."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    affected_uid = _random_tg_id()
    action_id = 66

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )

    affected_conf = FakeConfirmation(
        id=10,
        action_id=action_id,
        confirmer_tg_id=affected_uid,
        confirmation_role="affected_user",
        status="pending",
        preview_payload_hash="h",
        expires_at=_now() + timedelta(minutes=5),
    )
    monkeypatch.setattr(
        handler.ButlerActionConfirmationRepo,
        "list_for_action",
        AsyncMock(return_value=[affected_conf]),
    )

    mock_butler = MagicMock()
    # C1 fix: handler now calls revoke_affected_user_consent (not cancel_action)
    mock_butler.revoke_affected_user_consent = AsyncMock(
        return_value=MagicMock(status="cancelled")
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    token = "affected-token-xyz"
    callback = _make_callback(
        user_id=affected_uid,
        data=f"butler_affected_reject:{action_id}:{token}",
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_affected_reject(callback, session))

    mock_butler.revoke_affected_user_consent.assert_awaited_once()
    call_kwargs = mock_butler.revoke_affected_user_consent.call_args.kwargs
    assert call_kwargs["action_id"] == action_id
    assert call_kwargs["affected_user_id"] == affected_uid

    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 10: /butler_cancel command happy path
# ---------------------------------------------------------------------------


def test_butler_cancel_command_happy(app_env, monkeypatch) -> None:
    """Requester cancels own action → cancel_action called, success reply."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    action_id = 33
    message, command = _make_message(user_id=uid)
    command.args = str(action_id)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.cancel_action = AsyncMock(return_value=MagicMock(status="cancelled"))
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler_cancel(message, command, session))

    mock_butler.cancel_action.assert_awaited_once()
    call_kwargs = mock_butler.cancel_action.call_args.kwargs
    assert call_kwargs["action_id"] == action_id
    assert call_kwargs["cancelling_user_id"] == uid

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "отмен" in reply_text.lower()
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 11: /butler_cancel forbidden for non-owner
# ---------------------------------------------------------------------------


def test_butler_cancel_forbidden(app_env, monkeypatch) -> None:
    """Non-owner, non-admin /butler_cancel → forbidden message."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionRejectedError

    uid = _random_tg_id()
    action_id = 44
    message, command = _make_message(user_id=uid)
    command.args = str(action_id)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.cancel_action = AsyncMock(
        side_effect=ButlerActionRejectedError(
            "forbidden", error_kind="forbidden", action_id=action_id
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler_cancel(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "прав" in reply_text.lower() or "запрещ" in reply_text.lower()


# ---------------------------------------------------------------------------
# Test 12: /butler_status happy path
# ---------------------------------------------------------------------------


def test_butler_status_happy(app_env, monkeypatch) -> None:
    """/butler_status <id> → status reply containing plan_summary and status."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    action_id = 88
    message, command = _make_message(user_id=uid)
    command.args = str(action_id)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    fake_action = FakeButlerAction(
        id=action_id, requester_tg_id=uid, status="succeeded"
    )
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )

    asyncio.run(handler.handle_butler_status(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "Recall members who know Rust" in reply_text
    assert "успешно" in reply_text.lower() or "succeeded" in reply_text.lower()


# ---------------------------------------------------------------------------
# Test 13: /butler_undo → stub message
# ---------------------------------------------------------------------------


def test_butler_undo_stub(app_env, monkeypatch) -> None:
    """/butler_undo with undo flag OFF → stub message for members (T12-07)."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    command.args = "1"
    session = _make_session()

    # Master flag ON, undo sub-flag OFF — returns stub message
    def _flag_side_effect(session, flag_name: str):
        if flag_name == handler.BUTLER_UNDO_FLAG:
            async def _false():
                return False
            return _false()
        async def _true():
            return True
        return _true()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", _flag_side_effect
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    asyncio.run(handler.handle_butler_undo(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "обновлен" in reply_text.lower() or "undo" in reply_text.lower() or "отмен" in reply_text.lower()


def test_butler_undo_happy_path(app_env, monkeypatch) -> None:
    """/butler_undo with undo flag ON + execute_undo success → undo summary rendered."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    command.args = "42"
    session = _make_session()

    # Both flags ON
    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    # Stub execute_undo → returns a success summary
    mock_summary = {"action_id": 42, "status": "undone", "steps": []}
    mock_butler = MagicMock()
    mock_butler.execute_undo = AsyncMock(return_value=mock_summary)
    monkeypatch.setattr(handler, "_build_undo_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler_undo(message, command, session))

    mock_butler.execute_undo.assert_awaited_once()
    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    # Should contain undo summary content
    assert "откат" in reply_text.lower() or "undo" in reply_text.lower() or "✅" in reply_text or "🔄" in reply_text


def test_butler_undo_forbidden(app_env, monkeypatch) -> None:
    """/butler_undo with wrong user → forbidden message."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionRejectedError

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    command.args = "42"
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.execute_undo = AsyncMock(
        side_effect=ButlerActionRejectedError("forbidden", error_kind="forbidden", action_id=42)
    )
    monkeypatch.setattr(handler, "_build_undo_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler_undo(message, command, session))

    session.rollback.assert_awaited()
    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "прав" in reply_text.lower() or "forbidden" in reply_text.lower() or "нет" in reply_text.lower()


def test_butler_undo_ttl_expired(app_env, monkeypatch) -> None:
    """/butler_undo after TTL → ttl_expired message."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionError

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    command.args = "42"
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.execute_undo = AsyncMock(
        side_effect=ButlerActionError("ttl expired", error_kind="ttl_expired", action_id=42)
    )
    monkeypatch.setattr(handler, "_build_undo_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler_undo(message, command, session))

    session.rollback.assert_awaited()
    message.reply.assert_awaited_once()


def test_butler_undo_cascade_in_flight(app_env, monkeypatch) -> None:
    """/butler_undo when cascade running → system-busy message."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import CascadeInFlightError

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    command.args = "42"
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.execute_undo = AsyncMock(
        side_effect=CascadeInFlightError("locked", error_kind="cascade_in_flight", action_id=42)
    )
    monkeypatch.setattr(handler, "_build_undo_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler_undo(message, command, session))

    session.rollback.assert_awaited()
    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "занята" in reply_text.lower() or "секунд" in reply_text.lower()


# ---------------------------------------------------------------------------
# Test 14: rate_limit_exceeded plan rejection path (§5.B path 2)
# ---------------------------------------------------------------------------


def test_rate_limit_exceeded_path(app_env, monkeypatch) -> None:
    """plan_action raises rate_limit_exceeded → polite limit message."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionError

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.plan_action = AsyncMock(
        side_effect=ButlerActionError(
            "rate limit", error_kind="rate_limit_exceeded", action_id=1
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "лимит" in reply_text.lower() or "позже" in reply_text.lower()


# ---------------------------------------------------------------------------
# Test 15: Generic plan error path (§5.B path 8)
# ---------------------------------------------------------------------------


def test_plan_error_path(app_env, monkeypatch) -> None:
    """Generic ButlerActionError from plan_action → plan_error user message."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionError

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.plan_action = AsyncMock(
        side_effect=ButlerActionError(
            "some plan error", error_kind="plan_error", action_id=2
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    asyncio.run(handler.handle_butler(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "запрос" in reply_text.lower() or "другой" in reply_text.lower() or "планирова" in reply_text.lower()


# ---------------------------------------------------------------------------
# Test 16: CascadeInFlight on confirm callback
# ---------------------------------------------------------------------------


def test_cascade_in_flight_path(app_env, monkeypatch) -> None:
    """CascadeInFlightError on confirm_action → system-busy alert."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import CascadeInFlightError

    uid = _random_tg_id()
    action_id = 11

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )

    mock_butler = MagicMock()
    mock_butler.confirm_action = AsyncMock(
        side_effect=CascadeInFlightError(
            "locked", error_kind="cascade_in_flight", action_id=action_id
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    callback = _make_callback(user_id=uid, data=f"butler_confirm:{action_id}:token")
    session = _make_session()

    asyncio.run(handler.handle_butler_confirm_callback(callback, session))

    alert_calls = [
        c for c in callback.answer.call_args_list if c.kwargs.get("show_alert")
    ]
    assert len(alert_calls) >= 1
    msg = alert_calls[-1].args[0]
    assert "занята" in msg.lower() or "секунд" in msg.lower() or "попробуйте" in msg.lower()


# ---------------------------------------------------------------------------
# Test 17: EvidenceStale on confirm callback
# ---------------------------------------------------------------------------


def test_evidence_stale_path(app_env, monkeypatch) -> None:
    """EvidenceStaleError on confirm_action → data-changed alert."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import EvidenceStaleError

    uid = _random_tg_id()
    action_id = 22

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )

    mock_butler = MagicMock()
    mock_butler.confirm_action = AsyncMock(
        side_effect=EvidenceStaleError(
            "hash mismatch", error_kind="evidence_stale", action_id=action_id
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    callback = _make_callback(user_id=uid, data=f"butler_confirm:{action_id}:token")
    session = _make_session()

    asyncio.run(handler.handle_butler_confirm_callback(callback, session))

    alert_calls = [
        c for c in callback.answer.call_args_list if c.kwargs.get("show_alert")
    ]
    assert len(alert_calls) >= 1
    msg = alert_calls[-1].args[0]
    assert "измен" in msg.lower() or "заново" in msg.lower()


# ---------------------------------------------------------------------------
# Test 18: Feature flag gate — all three commands no-op when flag is OFF
# ---------------------------------------------------------------------------


def test_all_commands_no_op_when_flag_off(app_env, monkeypatch) -> None:
    """All /butler* commands are silent no-ops when memory.butler.enabled=False."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=False)
    )
    mock_user_get = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "get", mock_user_get)

    # /butler
    msg, cmd = _make_message(user_id=uid)
    asyncio.run(handler.handle_butler(msg, cmd, session))
    msg.reply.assert_not_awaited()

    # /butler_status
    msg2, cmd2 = _make_message(user_id=uid)
    cmd2.args = "1"
    asyncio.run(handler.handle_butler_status(msg2, cmd2, session))
    msg2.reply.assert_not_awaited()

    # /butler_cancel
    msg3, cmd3 = _make_message(user_id=uid)
    cmd3.args = "1"
    asyncio.run(handler.handle_butler_cancel(msg3, cmd3, session))
    msg3.reply.assert_not_awaited()

    # /butler_undo
    msg4, cmd4 = _make_message(user_id=uid)
    asyncio.run(handler.handle_butler_undo(msg4, cmd4, session))
    msg4.reply.assert_not_awaited()

    mock_user_get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 19: Affected user not found in confirmations → forbidden
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 20: Distinct error messages — cancel callback H1 fix
# ---------------------------------------------------------------------------


def test_cancel_callback_forbidden_error_distinct(app_env, monkeypatch) -> None:
    """Cancel callback: ButlerActionRejectedError(forbidden) → forbidden message (not system-busy)."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionRejectedError

    uid = _random_tg_id()
    action_id = 55

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )

    mock_butler = MagicMock()
    mock_butler.cancel_action = AsyncMock(
        side_effect=ButlerActionRejectedError(
            "forbidden", error_kind="forbidden", action_id=action_id
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    callback = _make_callback(
        user_id=uid, data=f"butler_cancel_cb:{action_id}"
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_cancel_callback(callback, session))

    alert_calls = [c for c in callback.answer.call_args_list if c.kwargs.get("show_alert")]
    assert len(alert_calls) >= 1
    msg = alert_calls[-1].args[0]
    # Must NOT be "system busy" — must be "forbidden" message
    assert "занята" not in msg.lower()
    assert "прав" in msg.lower() or "запрещ" in msg.lower()


def test_cancel_callback_wrong_status_distinct(app_env, monkeypatch) -> None:
    """Cancel callback: ButlerActionError(wrong_status) → 'cannot cancel' (not system-busy)."""
    handler = import_module("bot.handlers.butler")

    from bot.services.butler import ButlerActionError

    uid = _random_tg_id()
    action_id = 56

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )

    mock_butler = MagicMock()
    mock_butler.cancel_action = AsyncMock(
        side_effect=ButlerActionError(
            "wrong_status", error_kind="wrong_status", action_id=action_id
        )
    )
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    callback = _make_callback(
        user_id=uid, data=f"butler_cancel_cb:{action_id}"
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_cancel_callback(callback, session))

    alert_calls = [c for c in callback.answer.call_args_list if c.kwargs.get("show_alert")]
    assert len(alert_calls) >= 1
    msg = alert_calls[-1].args[0]
    assert "занята" not in msg.lower()
    assert "нельзя отмен" in msg.lower() or "already" in msg.lower()


# ---------------------------------------------------------------------------
# Test 21: H2 — AffectedUserUnreachableError on DM send failure
# ---------------------------------------------------------------------------


def test_affected_user_dm_failure_raises_unreachable(app_env, monkeypatch) -> None:
    """DM send to affected user fails → handler replies with unreachable message, no commit."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    affected_uid = _random_tg_id()
    action_id = 77

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)

    requester_conf = FakeConfirmation(
        id=1, action_id=action_id, confirmer_tg_id=uid,
        confirmation_role="requester", status="pending",
        preview_payload_hash="h1",
        expires_at=_now() + timedelta(minutes=5),
        confirmation_token="requester-tok",
    )
    affected_conf = FakeConfirmation(
        id=2, action_id=action_id, confirmer_tg_id=affected_uid,
        confirmation_role="affected_user", status="pending",
        preview_payload_hash="h2",
        expires_at=_now() + timedelta(minutes=5),
        confirmation_token="affected-tok",
    )

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.plan_action = AsyncMock(return_value=fake_action)
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    monkeypatch.setattr(
        handler.ButlerActionConfirmationRepo,
        "list_for_action",
        AsyncMock(return_value=[requester_conf, affected_conf]),
    )

    # Bot that fails to send to the affected user
    failing_bot = AsyncMock()
    failing_bot.send_message = AsyncMock(side_effect=Exception("User blocked bot"))

    msg, cmd = _make_message(user_id=uid, bot=failing_bot)
    session = _make_session()

    asyncio.run(handler.handle_butler(msg, cmd, session))

    # Handler should have replied with unreachable message
    msg.reply.assert_awaited()
    all_replies = msg.reply.call_args_list
    texts = [c[0][0] for c in all_replies if c[0]]
    assert any("участник" in t.lower() or "отправ" in t.lower() or "действие" in t.lower() for t in texts)

    # Commit must NOT have been called (action rolled back logically)
    session.commit.assert_not_awaited()
    # Handler must have called rollback explicitly before return to defeat
    # DbSessionMiddleware's unconditional commit on normal return.
    session.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 22: M2 — per-tool flag enforcement
# ---------------------------------------------------------------------------


def test_per_tool_flag_disabled_rejects_action(app_env, monkeypatch) -> None:
    """Tool-specific flag OFF → polite message, no preview sent."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    action_id = 88

    # Plan returns a tool that has a per-tool flag
    fake_action = FakeButlerAction(
        id=action_id, requester_tg_id=uid, tool_name="schedule_meeting"
    )
    requester_conf = FakeConfirmation(
        id=1, action_id=action_id, confirmer_tg_id=uid,
        confirmation_role="requester", status="pending",
        preview_payload_hash="h",
        expires_at=_now() + timedelta(minutes=5),
        confirmation_token="tok",
    )

    # Feature flag: master=ON, tool flag=OFF
    async def _flag_get(session, key):
        if key == "memory.butler.enabled":
            return True
        if key == "memory.butler.schedule_meeting.enabled":
            return False
        return True

    monkeypatch.setattr(handler.FeatureFlagRepo, "get", _flag_get)
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    mock_butler = MagicMock()
    mock_butler.plan_action = AsyncMock(return_value=fake_action)
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    monkeypatch.setattr(
        handler.ButlerActionConfirmationRepo,
        "list_for_action",
        AsyncMock(return_value=[requester_conf]),
    )

    msg, cmd = _make_message(user_id=uid)
    session = _make_session()

    asyncio.run(handler.handle_butler(msg, cmd, session))

    msg.reply.assert_awaited()
    texts = [c[0][0] for c in msg.reply.call_args_list if c[0]]
    # Should have replied with tool-disabled message
    assert any("недоступ" in t.lower() or "инструмент" in t.lower() for t in texts)
    # Commit must NOT have been called
    session.commit.assert_not_awaited()
    # Handler must have called rollback explicitly before return to defeat
    # DbSessionMiddleware's unconditional commit on normal return.
    session.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 23: M7 — html.escape in _render_preview
# ---------------------------------------------------------------------------


def test_render_preview_html_escapes_fields(app_env) -> None:
    """_render_preview escapes HTML special chars in LLM-generated fields."""
    handler = import_module("bot.handlers.butler")

    # Action with HTML-dangerous characters in LLM-generated summary
    action = FakeButlerAction(
        id=1,
        requester_tg_id=42,
        plan_summary="<script>alert('xss')</script> & some text",
        tool_name="recall_evidence",
        risk_level="low",
        visibility_scope="member",
    )

    result = handler._render_preview(action)

    # Dangerous chars must be escaped
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert "&amp;" in result


# ---------------------------------------------------------------------------
# Test 24: M3 / C1 — real service path for affected-user reject (non-admin)
# ---------------------------------------------------------------------------


def test_affected_user_reject_real_service_path(app_env, monkeypatch) -> None:
    """C1/M3: Affected user (non-admin) reject succeeds via revoke_affected_user_consent.

    Uses a real ButlerService with patched user_repo so that the non-admin
    affected user does NOT trigger forbidden in cancel_action.
    This verifies the production wiring is correct — not masked by mock.
    """
    from bot.services.butler import ButlerService
    from dataclasses import dataclass, field as dc_field
    from datetime import datetime, timedelta, timezone

    uid = _random_tg_id()
    affected_uid = _random_tg_id()
    action_id = 200

    # --- Minimal fake repos that support revoke_affected_user_consent ---

    @dataclass
    class _FakeAction:
        id: int
        requester_tg_id: int
        status: str = "pending_confirmation"
        rejection_reason: str | None = None
        expires_at: datetime = dc_field(
            default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        plan_payload: dict = dc_field(default_factory=dict)
        query: str = "test"
        visibility_scope: str = "member"
        tool_name: str = "recall_evidence"

    @dataclass
    class _FakeConf:
        id: int
        action_id: int
        confirmer_tg_id: int
        confirmation_role: str
        status: str = "pending"
        confirmation_token: str = "tok"

    _action_row = _FakeAction(id=action_id, requester_tg_id=uid)
    _conf_affected = _FakeConf(
        id=2, action_id=action_id,
        confirmer_tg_id=affected_uid,
        confirmation_role="affected_user",
    )

    class _FakeActionRepo:
        async def get(self, session, action_id):
            return _action_row if _action_row.id == action_id else None

        async def get_for_update(self, session, action_id):
            return _action_row if _action_row.id == action_id else None

        async def update_status(self, session, action_id, *, status, rejection_reason=None, **kwargs):
            _action_row.status = status
            if rejection_reason is not None:
                _action_row.rejection_reason = rejection_reason
            return 1

    class _FakeConfRepo:
        async def list_for_action(self, session, action_id):
            return [_conf_affected] if _conf_affected.action_id == action_id else []

        async def mark_resolved(self, session, conf_id, *, status, resolved_at=None):
            if _conf_affected.id == conf_id:
                _conf_affected.status = status
            return 1

    class _FakeUserRepo:
        async def get(self, session, user_id):
            # affected user is NOT admin
            u = MagicMock()
            u.is_admin = False
            u.is_member = True
            return u

    class _StubSettings:
        butler_plan_ttl_seconds = 900
        butler_confirmation_ttl_seconds = 300
        user_plans_day_ceiling = 10
        user_execs_day_ceiling = 5
        chat_actions_day_ceiling = 50
        tool_hour_ceiling = 20

    class _NoopRepo:
        async def try_increment(self, *a, **kw): return True
        async def decrement(self, *a, **kw): pass

    svc = ButlerService(
        session=AsyncMock(),
        ledger_repo=None,
        butler_action_repo=_FakeActionRepo(),
        butler_action_confirmation_repo=_FakeConfRepo(),
        butler_tool_invocation_repo=AsyncMock(),
        butler_rate_bucket_repo=_NoopRepo(),
        user_repo=_FakeUserRepo(),
        llm_gateway=AsyncMock(),
        evidence_builder=AsyncMock(),
        settings=_StubSettings(),
    )

    # Affected user (non-admin) calls revoke_affected_user_consent — must NOT raise forbidden
    result = asyncio.run(svc.revoke_affected_user_consent(
        action_id=action_id,
        affected_user_id=affected_uid,
    ))

    assert result.status == "cancelled"
    assert _conf_affected.status == "revoked"


def test_affected_user_not_in_confirmations_forbidden(app_env, monkeypatch) -> None:
    """Affected user reject when not in confirmations → forbidden alert."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    impostor_uid = _random_tg_id()
    action_id = 99

    fake_action = FakeButlerAction(id=action_id, requester_tg_id=uid)
    monkeypatch.setattr(
        handler.ButlerActionRepo, "get", AsyncMock(return_value=fake_action)
    )
    # Confirmation belongs to uid, NOT impostor_uid
    conf = FakeConfirmation(
        id=10,
        action_id=action_id,
        confirmer_tg_id=uid,  # not the impostor
        confirmation_role="affected_user",
        status="pending",
        preview_payload_hash="h",
        expires_at=_now() + timedelta(minutes=5),
    )
    monkeypatch.setattr(
        handler.ButlerActionConfirmationRepo,
        "list_for_action",
        AsyncMock(return_value=[conf]),
    )

    callback = _make_callback(
        user_id=impostor_uid,
        data=f"butler_affected_reject:{action_id}:token",
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_affected_reject(callback, session))

    alert_calls = [
        c for c in callback.answer.call_args_list if c.kwargs.get("show_alert")
    ]
    assert len(alert_calls) >= 1
    msg = alert_calls[-1].args[0]
    assert "прав" in msg.lower() or "запрещ" in msg.lower()
