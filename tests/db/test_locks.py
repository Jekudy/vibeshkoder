"""Unit tests for bot.db.locks — advisory lock helper (#80).

Strategy: offline (mock-based). The helper executes a raw SQL statement against the
session; we verify the correct SQL string and key format are emitted without needing a
real postgres connection.

F.1 behaviors verified:
1. advisory_lock_chat_message executes pg_advisory_xact_lock(hashtext(:k)) with the
   correct key format `chat_msg:{chat_id}:{message_id}`.
2. Two sequential calls with the same (chat_id, message_id) in the same function both
   call session.execute (advisory locks are reentrant on the same session — the second
   call is a no-op in postgres but the helper always issues it; we only verify it is
   called, not that postgres skips it, since that is a postgres implementation detail).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def test_advisory_lock_chat_message_emits_correct_sql(app_env) -> None:
    """advisory_lock_chat_message issues pg_advisory_xact_lock(hashtext(:k)) with the
    correct key `chat_msg:{chat_id}:{message_id}`."""
    from bot.db.locks import advisory_lock_chat_message

    session = AsyncMock()
    session.execute = AsyncMock()

    asyncio.run(advisory_lock_chat_message(session, chat_id=-1001234567890, message_id=42))

    session.execute.assert_awaited_once()
    args, kwargs = session.execute.await_args
    stmt = args[0]
    # The text() clause renders the lock SQL.
    stmt_str = str(stmt)
    assert "pg_advisory_xact_lock" in stmt_str
    assert "hashtext" in stmt_str
    # The bind parameter uses key "k".
    params = args[1] if len(args) > 1 else kwargs.get("params", {})
    assert params == {"k": "chat_msg:-1001234567890:42"}


def test_advisory_lock_chat_message_key_format(app_env) -> None:
    """Key format is exactly `chat_msg:{chat_id}:{message_id}` — all handlers must use
    this format for the advisory lock to be cross-handler cooperative."""
    from bot.db.locks import advisory_lock_chat_message

    session = AsyncMock()
    session.execute = AsyncMock()

    asyncio.run(advisory_lock_chat_message(session, chat_id=-9999, message_id=1))

    _args, _kwargs = session.execute.await_args
    params = _args[1]
    assert params["k"] == "chat_msg:-9999:1"


def test_advisory_lock_two_sequential_calls_both_execute(app_env) -> None:
    """Two sequential calls with the same (chat_id, message_id) within one coroutine
    both issue session.execute. Advisory locks are reentrant in postgres (second call is
    a no-op at the DB level), but the helper does not deduplicate — correct behavior
    because the helper is a thin wrapper and deduplication is postgres's job."""
    from bot.db.locks import advisory_lock_chat_message

    session = AsyncMock()
    session.execute = AsyncMock()

    async def _two_calls():
        await advisory_lock_chat_message(session, chat_id=-100, message_id=10)
        await advisory_lock_chat_message(session, chat_id=-100, message_id=10)

    asyncio.run(_two_calls())

    assert session.execute.await_count == 2


def test_advisory_lock_different_messages_use_different_keys(app_env) -> None:
    """Two different (chat_id, message_id) pairs produce different lock keys."""
    from bot.db.locks import advisory_lock_chat_message

    calls_params: list[dict] = []

    async def capture_execute(stmt, params):
        calls_params.append(dict(params))

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)

    async def _two_pairs():
        await advisory_lock_chat_message(session, chat_id=-100, message_id=1)
        await advisory_lock_chat_message(session, chat_id=-100, message_id=2)

    asyncio.run(_two_pairs())

    assert calls_params[0]["k"] == "chat_msg:-100:1"
    assert calls_params[1]["k"] == "chat_msg:-100:2"
    assert calls_params[0]["k"] != calls_params[1]["k"]
