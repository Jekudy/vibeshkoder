"""T3-02 acceptance tests for the forget_reply handler.

Test isolation strategy:
- All 5 tests are offline (mock-based, no DB). Fast, hermetic.
- Use SimpleNamespace for aiogram Message and mock repos directly,
  mirroring the pattern in test_edited_message.py.

Coverage:
- test_forget_own_message_creates_event: author /forget → event created
- test_forget_admin_forgets_any_creates_event: admin (non-author) /forget → event created
- test_forget_other_member_denied_silently_no_event: other member → silent denial
- test_forget_unknown_user_denied_silently_no_event: unknown user → silent denial
- test_forget_idempotent_re_issue_returns_same_event: re-issue returns same event, no duplicate
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

# ─── Helpers ─────────────────────────────────────────────────────────────────

COMMUNITY_CHAT_ID = -1001234567890


def _random_user_id() -> int:
    return random.randint(900_000_000, 999_999_999)


def _random_message_id() -> int:
    return random.randint(100_000, 999_999)


def _make_command_message(
    *,
    message_id: int | None = None,
    chat_id: int = COMMUNITY_CHAT_ID,
    user_id: int | None = None,
    reply_to_message_id: int | None = None,
) -> SimpleNamespace:
    """Build a minimal SimpleNamespace that mimics an aiogram /forget command message."""
    uid = user_id or _random_user_id()
    return SimpleNamespace(
        message_id=message_id or _random_message_id(),
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=uid),
        text="/forget",
        # Simulate reply_to_message: if reply_to_message_id given, provide a nested msg
        reply_to_message=(
            SimpleNamespace(message_id=reply_to_message_id)
            if reply_to_message_id is not None
            else None
        ),
        answer=AsyncMock(),
    )


def _make_user_row(
    *,
    tg_id: int | None = None,
    is_admin: bool = False,
    is_member: bool = True,
) -> MagicMock:
    """Return a MagicMock shaped like a User ORM row."""
    row = MagicMock()
    row.id = tg_id or _random_user_id()
    row.is_admin = is_admin
    row.is_member = is_member
    return row


def _make_chat_message_row(
    *,
    id: int | None = None,
    message_id: int | None = None,
    chat_id: int = COMMUNITY_CHAT_ID,
    user_id: int | None = None,
) -> MagicMock:
    """Return a MagicMock shaped like a ChatMessage ORM row."""
    row = MagicMock()
    row.id = id or random.randint(1, 100_000)
    row.message_id = message_id or _random_message_id()
    row.chat_id = chat_id
    row.user_id = user_id or _random_user_id()
    return row


def _make_forget_event_row(
    *,
    id: int | None = None,
    tombstone_key: str = "message:-1001234567890:123",
) -> MagicMock:
    """Return a MagicMock shaped like a ForgetEvent ORM row."""
    row = MagicMock()
    row.id = id or random.randint(1, 100_000)
    row.tombstone_key = tombstone_key
    row.status = "pending"
    return row


# ─── Test 1: author /forget creates event ────────────────────────────────────


def test_forget_own_message_creates_event(app_env, monkeypatch) -> None:
    """Author issuing /forget on their own message → forget_event created with authorized_by='self'."""
    handler = import_module("bot.handlers.forget_reply")

    user_id = _random_user_id()
    replied_msg_id = _random_message_id()

    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        user_id=user_id,
        reply_to_message_id=replied_msg_id,
    )

    # Requester user row: is_member, not admin
    requester_user = _make_user_row(tg_id=user_id, is_admin=False, is_member=True)
    # ChatMessage row owned by the SAME user
    chat_msg_row = _make_chat_message_row(
        chat_id=COMMUNITY_CHAT_ID,
        message_id=replied_msg_id,
        user_id=user_id,  # same user → is_author
    )
    forget_event = _make_forget_event_row(id=42)

    mock_get_user = AsyncMock(return_value=requester_user)
    mock_find_chat_msg = AsyncMock(return_value=chat_msg_row)
    mock_create_event = AsyncMock(return_value=forget_event)
    session = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler, "_find_chat_message", mock_find_chat_msg)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget(message, session))

    mock_create_event.assert_awaited_once()
    call_kwargs = mock_create_event.call_args.kwargs
    assert call_kwargs["target_type"] == "message"
    assert call_kwargs["target_id"] == str(chat_msg_row.id)
    assert call_kwargs["authorized_by"] == "self"
    assert call_kwargs["actor_user_id"] == user_id
    tombstone = f"message:{COMMUNITY_CHAT_ID}:{replied_msg_id}"
    assert call_kwargs["tombstone_key"] == tombstone

    # Confirmation reply sent
    message.answer.assert_awaited_once()
    reply_text = message.answer.call_args[0][0]
    assert "42" in reply_text  # event id in reply


# ─── Test 2: admin forgets any message ────────────────────────────────────────


def test_forget_admin_forgets_any_creates_event(app_env, monkeypatch) -> None:
    """Admin issuing /forget on another user's message → event created with authorized_by='admin'."""
    handler = import_module("bot.handlers.forget_reply")

    admin_id = _random_user_id()
    author_id = _random_user_id()
    replied_msg_id = _random_message_id()

    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        user_id=admin_id,
        reply_to_message_id=replied_msg_id,
    )

    # Requester is admin, NOT the message author
    requester_user = _make_user_row(tg_id=admin_id, is_admin=True, is_member=True)
    # ChatMessage row owned by a DIFFERENT user
    chat_msg_row = _make_chat_message_row(
        chat_id=COMMUNITY_CHAT_ID,
        message_id=replied_msg_id,
        user_id=author_id,
    )
    forget_event = _make_forget_event_row(id=99)

    mock_get_user = AsyncMock(return_value=requester_user)
    mock_find_chat_msg = AsyncMock(return_value=chat_msg_row)
    mock_create_event = AsyncMock(return_value=forget_event)
    session = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler, "_find_chat_message", mock_find_chat_msg)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget(message, session))

    mock_create_event.assert_awaited_once()
    call_kwargs = mock_create_event.call_args.kwargs
    assert call_kwargs["authorized_by"] == "admin"
    assert call_kwargs["actor_user_id"] == admin_id

    message.answer.assert_awaited_once()


# ─── Test 3: other member denied silently ─────────────────────────────────────


def test_forget_other_member_denied_silently_no_event(app_env, monkeypatch) -> None:
    """Regular member (not author, not admin) → silent denial, no event created."""
    handler = import_module("bot.handlers.forget_reply")

    requester_id = _random_user_id()
    author_id = _random_user_id()
    replied_msg_id = _random_message_id()

    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        user_id=requester_id,
        reply_to_message_id=replied_msg_id,
    )

    # Requester: regular member, not admin
    requester_user = _make_user_row(tg_id=requester_id, is_admin=False, is_member=True)
    # ChatMessage owned by a DIFFERENT user
    chat_msg_row = _make_chat_message_row(
        chat_id=COMMUNITY_CHAT_ID,
        message_id=replied_msg_id,
        user_id=author_id,  # different user → not author
    )

    mock_get_user = AsyncMock(return_value=requester_user)
    mock_find_chat_msg = AsyncMock(return_value=chat_msg_row)
    mock_create_event = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler, "_find_chat_message", mock_find_chat_msg)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget(message, session))

    mock_create_event.assert_not_awaited()
    # Silent: no reply to the user
    message.answer.assert_not_awaited()


# ─── Test 4: unknown user denied silently ─────────────────────────────────────


def test_forget_unknown_user_denied_silently_no_event(app_env, monkeypatch) -> None:
    """User not found in DB → silent denial, no event created."""
    handler = import_module("bot.handlers.forget_reply")

    replied_msg_id = _random_message_id()
    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        reply_to_message_id=replied_msg_id,
    )

    # UserRepo.get returns None → unknown user
    mock_get_user = AsyncMock(return_value=None)
    mock_find_chat_msg = AsyncMock()
    mock_create_event = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler, "_find_chat_message", mock_find_chat_msg)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget(message, session))

    mock_find_chat_msg.assert_not_awaited()
    mock_create_event.assert_not_awaited()
    message.answer.assert_not_awaited()


# ─── Test 5: idempotent re-issue returns same event ───────────────────────────


def test_forget_idempotent_re_issue_returns_same_event(app_env, monkeypatch) -> None:
    """Re-issuing /forget on already-forgotten message returns same event (no duplicate row)."""
    handler = import_module("bot.handlers.forget_reply")

    user_id = _random_user_id()
    replied_msg_id = _random_message_id()

    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        user_id=user_id,
        reply_to_message_id=replied_msg_id,
    )

    requester_user = _make_user_row(tg_id=user_id, is_admin=False, is_member=True)
    chat_msg_row = _make_chat_message_row(
        chat_id=COMMUNITY_CHAT_ID,
        message_id=replied_msg_id,
        user_id=user_id,  # same user → author
    )
    # ForgetEventRepo.create is idempotent: returns the SAME row on conflict
    existing_event = _make_forget_event_row(id=77)
    mock_create_event = AsyncMock(return_value=existing_event)

    mock_get_user = AsyncMock(return_value=requester_user)
    mock_find_chat_msg = AsyncMock(return_value=chat_msg_row)
    session = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler, "_find_chat_message", mock_find_chat_msg)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    # First call
    asyncio.run(handler.handle_forget(message, session))
    # Second call (simulates re-issue — same mocks, same row returned)
    asyncio.run(handler.handle_forget(message, session))

    # ForgetEventRepo.create called twice (the idempotency is inside the repo, not handler)
    assert mock_create_event.await_count == 2
    # Both calls used the same tombstone_key
    for call in mock_create_event.call_args_list:
        assert call.kwargs["tombstone_key"] == f"message:{COMMUNITY_CHAT_ID}:{replied_msg_id}"

    # Confirmation reply sent both times (re-issuing shows the existing event id)
    assert message.answer.await_count == 2
    # Both replies reference the same event id
    for call in message.answer.call_args_list:
        assert "77" in call[0][0]
