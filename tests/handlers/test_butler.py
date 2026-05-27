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
from unittest.mock import AsyncMock, MagicMock, patch

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
    """Non-member → polite refusal message, no ButlerService interaction."""
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
    message.reply.assert_awaited_once()
    call_args = message.reply.call_args
    assert "участникам" in call_args[0][0] or "участник" in call_args[0][0].lower()


# ---------------------------------------------------------------------------
# Test 3: Non-member rejection — UserRepo returns None
# ---------------------------------------------------------------------------


def test_non_member_rejection_user_none(app_env, monkeypatch) -> None:
    """UserRepo.get returns None → polite refusal."""
    handler = import_module("bot.handlers.butler")

    message, command = _make_message()
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=None))

    asyncio.run(handler.handle_butler(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "участник" in reply_text.lower() or "доступ" in reply_text.lower()


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

    uid = _random_tg_id()
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
    """Affected user rejects → cancel_action called."""
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
    mock_butler.cancel_action = AsyncMock(return_value=MagicMock(status="cancelled"))
    monkeypatch.setattr(handler, "_build_butler_service", lambda s: mock_butler)

    token = "affected-token-xyz"
    callback = _make_callback(
        user_id=affected_uid,
        data=f"butler_affected_reject:{action_id}:{token}",
    )
    session = _make_session()

    asyncio.run(handler.handle_butler_affected_reject(callback, session))

    mock_butler.cancel_action.assert_awaited_once()
    call_kwargs = mock_butler.cancel_action.call_args.kwargs
    assert call_kwargs["action_id"] == action_id
    assert call_kwargs["cancelling_user_id"] == affected_uid

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
    """/butler_undo → stub message (T12-07 scope)."""
    handler = import_module("bot.handlers.butler")

    uid = _random_tg_id()
    message, command = _make_message(user_id=uid)
    command.args = "1"
    session = _make_session()

    monkeypatch.setattr(
        handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)
    )
    member = _make_user(uid, is_member=True)
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=member))

    asyncio.run(handler.handle_butler_undo(message, command, session))

    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert "обновлен" in reply_text.lower() or "undo" in reply_text.lower() or "отмен" in reply_text.lower()


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
