"""T3-03 acceptance tests for the /forget_me handler.

Test strategy:
- Tests 1–5: mock-based (no DB required). Fast and hermetic.
- DB-backed variants use the ``db_session`` fixture with real postgres.

Skip cleanly if postgres is unreachable.

Tests:
1. Known user → creates forget_events row with correct fields.
2. Unknown user (not in users table) → silent return, no forget_events row.
3. Idempotent: second /forget_me returns same event id, no duplicate row.
4. Reply text includes the integer message count.
5. Cascade NOT run: chat_messages.text is still intact after /forget_me.
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


def _random_tg_id() -> int:
    return random.randint(900_000_000, 999_999_999)


def _make_message(
    *,
    tg_id: int | None = None,
    chat_id: int = 123456789,
    chat_type: str = "private",
) -> SimpleNamespace:
    """Build a minimal SimpleNamespace mimicking an aiogram Message for /forget_me."""
    uid = tg_id or _random_tg_id()
    return SimpleNamespace(
        message_id=random.randint(1, 100_000),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=uid),
        text="/forget_me",
        reply=AsyncMock(),
    )


def _make_user_row(*, tg_id: int) -> MagicMock:
    """Return a MagicMock shaped like a User ORM row (User.id == tg_id)."""
    row = MagicMock()
    row.id = tg_id
    return row


def _make_forget_event_row(*, event_id: int, user_id: int, tg_id: int) -> MagicMock:
    """Return a MagicMock shaped like a ForgetEvent ORM row."""
    row = MagicMock()
    row.id = event_id
    row.target_type = "user"
    row.target_id = str(user_id)
    row.actor_user_id = user_id
    row.authorized_by = "self"
    row.tombstone_key = f"user:{tg_id}"
    row.policy = "forgotten"
    return row


# ─── Test 1: known user creates event ────────────────────────────────────────


def test_forget_me_creates_user_event_for_known_user(app_env, monkeypatch) -> None:
    """Known user: forget_events row created with target_type='user',
    tombstone_key='user:{tg_id}', authorized_by='self'."""
    handler = import_module("bot.handlers.forget_me")

    tg_id = _random_tg_id()
    message = _make_message(tg_id=tg_id)
    user_row = _make_user_row(tg_id=tg_id)
    event_row = _make_forget_event_row(event_id=42, user_id=tg_id, tg_id=tg_id)

    # session.scalar returns msg_count
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=5)

    mock_get_user = AsyncMock(return_value=user_row)
    mock_create_event = AsyncMock(return_value=event_row)

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget_me(message, session))

    mock_create_event.assert_awaited_once()
    call_kwargs = mock_create_event.call_args.kwargs
    assert call_kwargs["target_type"] == "user"
    assert call_kwargs["target_id"] == str(tg_id)
    assert call_kwargs["authorized_by"] == "self"
    assert call_kwargs["tombstone_key"] == f"user:{tg_id}"
    assert call_kwargs["policy"] == "forgotten"
    assert call_kwargs["actor_user_id"] == tg_id


# ─── Test 2: unknown user → no event created ─────────────────────────────────


def test_forget_me_unknown_user_no_event(app_env, monkeypatch) -> None:
    """Telegram user not in users table: silent return (or 'not registered' reply),
    no forget_events row created."""
    handler = import_module("bot.handlers.forget_me")

    message = _make_message()
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)

    mock_get_user = AsyncMock(return_value=None)  # user not found
    mock_create_event = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget_me(message, session))

    # No event must be created for unregistered users
    mock_create_event.assert_not_awaited()


# ─── Test 3: idempotent — second call returns same event id ──────────────────


def test_forget_me_idempotent_returns_same_event(app_env, monkeypatch) -> None:
    """Call /forget_me twice: second call returns same forget_events.id, no duplicate."""
    handler = import_module("bot.handlers.forget_me")

    tg_id = _random_tg_id()
    message = _make_message(tg_id=tg_id)
    user_row = _make_user_row(tg_id=tg_id)
    event_row = _make_forget_event_row(event_id=99, user_id=tg_id, tg_id=tg_id)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=3)

    mock_get_user = AsyncMock(return_value=user_row)
    # ForgetEventRepo.create is idempotent — returns same row both times
    mock_create_event = AsyncMock(return_value=event_row)

    monkeypatch.setattr(handler.UserRepo, "get", mock_get_user)
    monkeypatch.setattr(handler.ForgetEventRepo, "create", mock_create_event)

    asyncio.run(handler.handle_forget_me(message, session))
    asyncio.run(handler.handle_forget_me(message, session))

    # create called twice (idempotency is inside the repo's ON CONFLICT logic)
    assert mock_create_event.await_count == 2
    # Both calls used same tombstone_key → repo returns same row → same event id
    for call in mock_create_event.call_args_list:
        assert call.kwargs["tombstone_key"] == f"user:{tg_id}"


# ─── Test 4: reply includes message count ────────────────────────────────────


def test_forget_me_reply_includes_message_count(app_env, monkeypatch) -> None:
    """Reply text must contain the integer message count queried before cascade."""
    handler = import_module("bot.handlers.forget_me")

    tg_id = _random_tg_id()
    message = _make_message(tg_id=tg_id)
    user_row = _make_user_row(tg_id=tg_id)
    event_row = _make_forget_event_row(event_id=7, user_id=tg_id, tg_id=tg_id)

    msg_count = 42
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=msg_count)

    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=user_row))
    monkeypatch.setattr(handler.ForgetEventRepo, "create", AsyncMock(return_value=event_row))

    asyncio.run(handler.handle_forget_me(message, session))

    # reply() must have been called with text containing the count
    message.reply.assert_awaited_once()
    reply_text = message.reply.call_args[0][0]
    assert str(msg_count) in reply_text


# ─── Test 5: cascade NOT run — chat_messages.text still intact ───────────────


def test_forget_me_does_not_run_cascade(app_env, monkeypatch) -> None:
    """After /forget_me, chat_messages.text is still intact.

    /forget_me only enqueues a forget_events row. The actual cascade (wiping message
    text) is performed asynchronously by the Sprint 03 worker (#96). This test verifies
    that the handler does NOT mutate chat_messages rows synchronously.
    """
    handler = import_module("bot.handlers.forget_me")

    tg_id = _random_tg_id()
    message = _make_message(tg_id=tg_id)
    user_row = _make_user_row(tg_id=tg_id)
    event_row = _make_forget_event_row(event_id=11, user_id=tg_id, tg_id=tg_id)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=10)

    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=user_row))
    monkeypatch.setattr(handler.ForgetEventRepo, "create", AsyncMock(return_value=event_row))

    asyncio.run(handler.handle_forget_me(message, session))

    # The handler must NOT issue any UPDATE on chat_messages (cascade is async, not here)
    # We verify by checking that session.execute was not called with an UPDATE statement
    # targeting chat_messages. The only session call should be session.scalar (count query).
    for call in session.execute.call_args_list:
        stmt = call.args[0] if call.args else None
        if stmt is None:
            continue
        # If there's an UPDATE statement, check it's not targeting chat_messages
        if hasattr(stmt, "table"):
            assert stmt.table.name != "chat_messages", (
                "VIOLATION: /forget_me issued a synchronous UPDATE on chat_messages — "
                "cascade must be deferred to sprint 03 worker (#96)"
            )


# ─── DB-backed tests ──────────────────────────────────────────────────────────

import itertools

_user_counter = itertools.count(start=8_200_000_000)


def _next_user_id() -> int:
    return next(_user_counter)


async def _make_db_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def test_forget_me_db_creates_event_known_user(db_session) -> None:
    """DB-backed: known user → forget_events row with correct fields, status=pending."""
    handler = import_module("bot.handlers.forget_me")
    tg_id = await _make_db_user(db_session)
    message = _make_message(tg_id=tg_id)

    await handler.handle_forget_me(message, db_session)

    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, f"user:{tg_id}")
    assert ev is not None
    assert ev.target_type == "user"
    assert ev.target_id == str(tg_id)
    assert ev.authorized_by == "self"
    assert ev.tombstone_key == f"user:{tg_id}"
    assert ev.policy == "forgotten"
    assert ev.actor_user_id == tg_id
    assert ev.status == "pending"


async def test_forget_me_db_idempotent(db_session) -> None:
    """DB-backed: two /forget_me calls produce exactly one forget_events row."""
    handler = import_module("bot.handlers.forget_me")
    tg_id = await _make_db_user(db_session)
    message = _make_message(tg_id=tg_id)

    await handler.handle_forget_me(message, db_session)
    await handler.handle_forget_me(message, db_session)

    from sqlalchemy import func, select
    from bot.db.models import ForgetEvent

    count = await db_session.scalar(
        select(func.count(ForgetEvent.id)).where(
            ForgetEvent.tombstone_key == f"user:{tg_id}"
        )
    )
    assert count == 1


async def test_forget_me_db_does_not_cascade(db_session) -> None:
    """DB-backed: chat_messages rows are untouched after /forget_me (cascade is async)."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from bot.db.models import ChatMessage
    from bot.db.repos.user import UserRepo

    handler = import_module("bot.handlers.forget_me")
    tg_id = await _make_db_user(db_session)

    # Insert a chat_message for this user
    msg = ChatMessage(
        message_id=random.randint(1_000_000, 9_999_999),
        chat_id=-1001234567890,
        user_id=tg_id,
        text="still here after forget_me",
        date=datetime.now(timezone.utc),
    )
    db_session.add(msg)
    await db_session.flush()
    msg_id = msg.id

    message = _make_message(tg_id=tg_id)
    await handler.handle_forget_me(message, db_session)

    # Re-fetch the chat_message — text must be unchanged
    result = await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == msg_id)
    )
    row = result.scalar_one()
    assert row.text == "still here after forget_me", (
        "cascade must not run synchronously in /forget_me"
    )
