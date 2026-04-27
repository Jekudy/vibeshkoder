"""T3-02 acceptance tests for the forget_reply handler.

Test isolation strategy:
- Tests 1–5: mock-based (no DB required). Fast and hermetic.
- Tests 6–7: mock-based branch coverage (Fix 3).
- DB-backed tests (8–10): use the ``db_session`` fixture with real postgres.
  Skip cleanly if postgres is unreachable.

Coverage:
- test_forget_own_message_creates_event: author /forget → event created
- test_forget_admin_forgets_any_creates_event: admin (non-author) /forget → event created
- test_forget_other_member_denied_silently_no_event: other member → silent denial
- test_forget_unknown_user_denied_silently_no_event: unknown user → silent denial
- test_forget_idempotent_re_issue_returns_same_event: re-issue returns same event, no duplicate
- test_forget_no_reply_returns_usage_hint: no reply_to_message → usage hint, no event
- test_forget_replied_to_unknown_message_silent: replied-to msg not in DB → silent, no event
- test_forget_db_own_message_creates_real_event_row: DB-backed, author creates event
- test_forget_db_admin_creates_real_event_row: DB-backed, admin creates event
- test_forget_db_idempotent_no_duplicate_row: DB-backed, second /forget returns same row
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


# ─── Test 6: no reply_to_message → usage hint ─────────────────────────────────


def test_forget_no_reply_returns_usage_hint(app_env, monkeypatch) -> None:
    """/forget sent WITHOUT a reply_to_message → usage hint reply, no event created."""
    handler = import_module("bot.handlers.forget_reply")

    # Message with NO reply_to_message
    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        user_id=_random_user_id(),
        reply_to_message_id=None,  # explicit: no reply
    )
    mock_create_event = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget(message, session))

    # Must respond with a usage hint
    message.answer.assert_awaited_once()
    reply_text = message.answer.call_args[0][0]
    assert "as a reply" in reply_text.lower(), (
        f"Expected usage hint containing 'as a reply', got: {reply_text!r}"
    )
    # Must NOT create a forget event
    mock_create_event.assert_not_awaited()


# ─── Test 7: replied-to message not in DB → silent return ─────────────────────


def test_forget_replied_to_unknown_message_silent(app_env, monkeypatch, caplog) -> None:
    """/forget on a message not in chat_messages → no event, no reply, warn log emitted."""
    import logging

    handler = import_module("bot.handlers.forget_reply")

    user_id = _random_user_id()
    replied_msg_id = _random_message_id()

    message = _make_command_message(
        chat_id=COMMUNITY_CHAT_ID,
        user_id=user_id,
        reply_to_message_id=replied_msg_id,
    )

    # Known requester (registered user, is author if message existed)
    requester_user = _make_user_row(tg_id=user_id, is_admin=False, is_member=True)
    # _find_chat_message returns None → message not found in DB
    mock_find_chat_msg = AsyncMock(return_value=None)
    mock_create_event = AsyncMock()
    session = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=requester_user))
    monkeypatch.setattr(handler, "_find_chat_message", mock_find_chat_msg)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    with caplog.at_level(logging.WARNING, logger="bot.handlers.forget_reply"):
        asyncio.run(handler.handle_forget(message, session))

    # Silent: no reply to the user
    message.answer.assert_not_awaited()
    # No event created
    mock_create_event.assert_not_awaited()
    # Warning must be logged
    assert any("chat_messages" in r.message or "no chat_messages" in r.message or "skipping" in r.message
               for r in caplog.records), (
        f"Expected a warning log mentioning missing chat_messages row, got: {[r.message for r in caplog.records]}"
    )


# ─── DB-backed tests ──────────────────────────────────────────────────────────

import itertools
import random as _random

_user_counter_fr = itertools.count(start=9_200_000_000)


def _next_user_id_fr() -> int:
    return next(_user_counter_fr)


async def _make_db_user_fr(db_session, *, is_admin: bool = False) -> int:
    """Insert a user row and return its telegram_id (== users.id)."""
    from bot.db.repos.user import UserRepo

    uid = _next_user_id_fr()
    user = await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    if is_admin:
        from sqlalchemy import update as sa_update
        from bot.db.models import User
        await db_session.execute(
            sa_update(User).where(User.id == uid).values(is_admin=True)
        )
        await db_session.flush()
    return uid


async def _make_db_chat_message(db_session, *, user_id: int, chat_id: int) -> "ChatMessage":
    """Insert a minimal chat_messages row and return the ORM object."""
    from datetime import datetime, timezone
    from bot.db.models import ChatMessage

    msg = ChatMessage(
        message_id=_random.randint(10_000_000, 99_999_999),
        chat_id=chat_id,
        user_id=user_id,
        text="hello world",
        date=datetime.now(timezone.utc),
    )
    db_session.add(msg)
    await db_session.flush()
    return msg


async def test_forget_db_own_message_creates_real_event_row(db_session) -> None:
    """DB-backed: author calls /forget → forget_events row has correct fields."""
    from sqlalchemy import func, select
    from bot.db.models import ForgetEvent
    from bot.config import settings

    handler = import_module("bot.handlers.forget_reply")

    chat_id = settings.COMMUNITY_CHAT_ID
    user_tg_id = await _make_db_user_fr(db_session)
    chat_msg = await _make_db_chat_message(db_session, user_id=user_tg_id, chat_id=chat_id)

    # Build a minimal message simulating /forget as reply to chat_msg
    message = _make_command_message(
        chat_id=chat_id,
        user_id=user_tg_id,
        reply_to_message_id=chat_msg.message_id,
    )

    await handler.handle_forget(message, db_session)

    tombstone_key = f"message:{chat_id}:{chat_msg.message_id}"
    count = await db_session.scalar(
        select(func.count(ForgetEvent.id)).where(
            ForgetEvent.tombstone_key == tombstone_key
        )
    )
    assert count == 1, f"Expected 1 forget_events row, got {count}"

    ev = await db_session.scalar(
        select(ForgetEvent).where(ForgetEvent.tombstone_key == tombstone_key)
    )
    assert ev.authorized_by == "self"
    assert ev.target_type == "message"
    assert ev.target_id == str(chat_msg.id)


async def test_forget_db_admin_creates_real_event_row(db_session) -> None:
    """DB-backed: admin /forget on another user's message → event has authorized_by='admin'."""
    from sqlalchemy import func, select
    from bot.db.models import ForgetEvent
    from bot.config import settings

    handler = import_module("bot.handlers.forget_reply")

    chat_id = settings.COMMUNITY_CHAT_ID
    author_tg_id = await _make_db_user_fr(db_session)
    admin_tg_id = await _make_db_user_fr(db_session, is_admin=True)
    chat_msg = await _make_db_chat_message(db_session, user_id=author_tg_id, chat_id=chat_id)

    # Admin issues /forget on author's message
    message = _make_command_message(
        chat_id=chat_id,
        user_id=admin_tg_id,
        reply_to_message_id=chat_msg.message_id,
    )

    await handler.handle_forget(message, db_session)

    tombstone_key = f"message:{chat_id}:{chat_msg.message_id}"
    count = await db_session.scalar(
        select(func.count(ForgetEvent.id)).where(
            ForgetEvent.tombstone_key == tombstone_key
        )
    )
    assert count == 1, f"Expected 1 forget_events row, got {count}"

    ev = await db_session.scalar(
        select(ForgetEvent).where(ForgetEvent.tombstone_key == tombstone_key)
    )
    assert ev.authorized_by == "admin"
    assert ev.actor_user_id == admin_tg_id
    assert ev.target_id == str(chat_msg.id)


async def test_forget_db_idempotent_no_duplicate_row(db_session) -> None:
    """DB-backed: two /forget calls → COUNT(forget_events WHERE tombstone_key=...) == 1."""
    from sqlalchemy import func, select
    from bot.db.models import ForgetEvent
    from bot.config import settings

    handler = import_module("bot.handlers.forget_reply")

    chat_id = settings.COMMUNITY_CHAT_ID
    user_tg_id = await _make_db_user_fr(db_session)
    chat_msg = await _make_db_chat_message(db_session, user_id=user_tg_id, chat_id=chat_id)

    message = _make_command_message(
        chat_id=chat_id,
        user_id=user_tg_id,
        reply_to_message_id=chat_msg.message_id,
    )

    await handler.handle_forget(message, db_session)
    await handler.handle_forget(message, db_session)

    tombstone_key = f"message:{chat_id}:{chat_msg.message_id}"
    count = await db_session.scalar(
        select(func.count(ForgetEvent.id)).where(
            ForgetEvent.tombstone_key == tombstone_key
        )
    )
    assert count == 1, f"Expected 1 forget_events row after two calls, got {count}"

    # Both calls return the same event id (idempotency)
    ev = await db_session.scalar(
        select(ForgetEvent).where(ForgetEvent.tombstone_key == tombstone_key)
    )
    assert ev is not None
