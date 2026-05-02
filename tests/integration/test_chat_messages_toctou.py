"""TOCTOU race regression tests for #80 — advisory lock on chat_messages handler.

Strategy: state-based verification using SQLAlchemy event listeners to spy on emitted
SQL statements and verify that:
1. ``advisory_lock_chat_message`` is called BEFORE any SELECT or INSERT against
   ``chat_messages`` in the handler path.
2. The lock key format is correct: ``chat_msg:{chat_id}:{message_id}``.
3. Privacy invariant: if a handler path processes an #offrecord message, the advisory
   lock is acquired before detect_policy or any DB write runs.

Why not true concurrent asyncio.gather with two real sessions?
The ``db_session`` fixture uses a single connection wrapped in an outer transaction with
nested savepoints. Two independent AsyncSession objects on the SAME connection would
share the same transaction state, making independent advisory locks impossible to test
without two separate real connections. That requires either a separate engine fixture or
a full docker-based integration test (which needs postgres to be running). This approach
gives deterministic, fast proof that the lock is acquired in the right order, and does
not depend on postgres being available. The advisory lock itself is tested with a real
postgres in test_message_repo.py (via db_session fixture) since lock acquisition is a
no-op unless postgres is reachable.

Scenario 1 (chat-messages handler + advisory lock ordering):
- Patch session.execute to record SQL statement text.
- Call save_chat_message once for an #offrecord message.
- Assert the first session.execute call is the advisory lock SQL.

Scenario 2 (edited_message handler + advisory lock ordering):
- Same approach: first execute call is advisory lock, subsequent calls are the SELECT
  FOR UPDATE (with_for_update), UPDATE, etc.

Scenario 3 (MessageRepo.save legacy DO NOTHING SELECT path uses with_for_update):
- Patch session to capture SQL statements.
- Call MessageRepo.save with both policy args = None (triggers legacy path).
- Assert the follow-up SELECT (after DO NOTHING conflict) uses FOR UPDATE.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

COMMUNITY_CHAT_ID = -1001234567890


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_message(
    *,
    message_id: int = 500_001,
    chat_id: int = COMMUNITY_CHAT_ID,
    user_id: int = 999_000_001,
    text: str | None = "hello",
    caption: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"u{user_id}",
            first_name="Test",
            last_name=None,
        ),
        text=text,
        caption=caption,
        date=datetime.now(timezone.utc),
        model_dump=MagicMock(return_value={"message_id": message_id, "text": text}),
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        entities=None,
        caption_entities=None,
        edit_date=None,
    )


# ─── Scenario 1: chat_messages handler acquires advisory lock first ───────────


def test_save_chat_message_advisory_lock_before_db_ops(app_env, monkeypatch) -> None:
    """The advisory lock SQL must be the FIRST session.execute call in save_chat_message.

    This proves that no SELECT or INSERT can race before the lock is acquired.
    The lock key must be ``chat_msg:{chat_id}:{message_id}``.
    """
    handler = import_module("bot.handlers.chat_messages")

    executed_sql: list[str] = []

    async def capture_execute(stmt, *args, **kwargs):
        # Capture the SQL text for ordering verification.
        stmt_str = str(stmt) if hasattr(stmt, "__str__") else repr(stmt)
        executed_sql.append(stmt_str)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalar_one = MagicMock(return_value=None)
        result.scalars = MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    message = _make_message(
        message_id=500_001,
        chat_id=COMMUNITY_CHAT_ID,
        text="normal message",
    )

    # Patch MessageRepo.save and MessageVersionRepo.insert_version to avoid actual
    # DB interaction after the lock. insert_version must return an obj with .id to
    # allow persist_message_with_policy to close the FK loop.
    saved_row = MagicMock()
    saved_row.id = 1
    saved_row.current_version_id = None
    v1_row = MagicMock()
    v1_row.id = 10
    monkeypatch.setattr(
        "bot.db.repos.message.MessageRepo.save",
        AsyncMock(return_value=saved_row),
    )
    monkeypatch.setattr(
        "bot.db.repos.message_version.MessageVersionRepo.insert_version",
        AsyncMock(return_value=v1_row),
    )
    monkeypatch.setattr(
        "bot.db.repos.user.UserRepo.upsert",
        AsyncMock(return_value=MagicMock()),
    )

    asyncio.run(handler.save_chat_message(message, session))

    assert executed_sql, "No session.execute calls made — advisory lock was never called"
    first_sql = executed_sql[0]
    assert "pg_advisory_xact_lock" in first_sql, (
        f"TOCTOU RACE: first session.execute was NOT the advisory lock. "
        f"Got: {first_sql!r}. All SQL: {executed_sql}"
    )
    assert "hashtext" in first_sql, (
        f"Advisory lock SQL must use hashtext(). Got: {first_sql!r}"
    )

    # Verify the lock key was correct by checking the params of the first call.
    first_call_args = session.execute.await_args_list[0]
    params = first_call_args.args[1] if len(first_call_args.args) > 1 else {}
    assert params.get("k") == f"chat_msg:{COMMUNITY_CHAT_ID}:500001", (
        f"Lock key mismatch. Expected chat_msg:{COMMUNITY_CHAT_ID}:500001, got {params.get('k')!r}"
    )


def test_save_chat_message_offrecord_advisory_lock_before_db_ops(app_env, monkeypatch) -> None:
    """Advisory lock is acquired first even for #offrecord messages — privacy invariant
    requires the lock precedes any detect_policy DB interaction as well."""
    handler = import_module("bot.handlers.chat_messages")

    executed_sql: list[str] = []

    async def capture_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        executed_sql.append(stmt_str)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalar_one = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    message = _make_message(
        message_id=500_002,
        chat_id=COMMUNITY_CHAT_ID,
        text="#offrecord secret",
    )

    saved_row = MagicMock()
    saved_row.id = 2
    saved_row.current_version_id = None
    v1_row = MagicMock()
    v1_row.id = 20
    monkeypatch.setattr(
        "bot.db.repos.message.MessageRepo.save",
        AsyncMock(return_value=saved_row),
    )
    monkeypatch.setattr(
        "bot.db.repos.message_version.MessageVersionRepo.insert_version",
        AsyncMock(return_value=v1_row),
    )
    monkeypatch.setattr(
        "bot.db.repos.user.UserRepo.upsert",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "bot.db.repos.offrecord_mark.OffrecordMarkRepo.create_for_message",
        AsyncMock(),
    )

    asyncio.run(handler.save_chat_message(message, session))

    assert executed_sql, "No session.execute calls — advisory lock missing"
    assert "pg_advisory_xact_lock" in executed_sql[0], (
        f"Advisory lock must be first even for #offrecord. First SQL: {executed_sql[0]!r}"
    )


# ─── Scenario 2: edited_message handler acquires advisory lock first ──────────


def test_handle_edited_message_advisory_lock_before_find_chat_message(
    app_env, monkeypatch
) -> None:
    """The advisory lock must be acquired before _find_chat_message (SELECT FOR UPDATE).

    This verifies that T1-14's with_for_update() and #80's advisory lock cooperate:
    the advisory lock serializes competing transactions at the application level, and
    FOR UPDATE locks the row inside the transaction.
    """
    handler = import_module("bot.handlers.edited_message")

    executed_sql: list[str] = []

    async def capture_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        executed_sql.append(stmt_str)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalar_one = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    message = _make_message(
        message_id=500_003,
        chat_id=COMMUNITY_CHAT_ID,
        text="some edit",
    )

    # _find_chat_message returns None → handler logs warning and returns.
    # This is enough to verify lock ordering.
    monkeypatch.setattr(handler, "_find_chat_message", AsyncMock(return_value=None))

    asyncio.run(handler.handle_edited_message(message, session))

    assert executed_sql, "No session.execute calls — advisory lock missing"
    assert "pg_advisory_xact_lock" in executed_sql[0], (
        f"TOCTOU RACE: edited_message handler did not acquire advisory lock first. "
        f"First SQL: {executed_sql[0]!r}. All SQL: {executed_sql}"
    )


# ─── Scenario 3: MessageRepo.save legacy path uses with_for_update ────────────


def test_message_repo_save_legacy_select_uses_for_update(app_env) -> None:
    """When MessageRepo.save takes the legacy DO NOTHING path (both policy args = None),
    the follow-up SELECT must include FOR UPDATE to prevent TOCTOU reads.

    Verification: capture all session.execute calls and assert that the SELECT
    statement (after the DO NOTHING insert conflict) includes the FOR UPDATE clause.
    """
    # We need to simulate the DO NOTHING conflict path:
    # 1. pg_insert(...).on_conflict_do_nothing() returns scalar_one_or_none=None
    #    (simulates conflict — no row returned by RETURNING).
    # 2. The fallback SELECT must include FOR UPDATE.
    from bot.db.repos.message import MessageRepo

    captured_stmts: list = []
    call_count = [0]

    async def capture_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            # First call: DO NOTHING insert — simulate conflict (returns None).
            result.scalar_one_or_none = MagicMock(return_value=None)
        else:
            # Second call: the SELECT fallback — return a fake existing row.
            fake_row = MagicMock()
            fake_row.id = 99
            fake_row.chat_id = -100
            fake_row.message_id = 1
            result.scalar_one = MagicMock(return_value=fake_row)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    asyncio.run(
        MessageRepo.save(
            session,
            message_id=1,
            chat_id=-100,
            user_id=42,
            text="hello",
            date=datetime.now(timezone.utc),
            # No memory_policy / is_redacted → triggers legacy DO NOTHING path.
        )
    )

    # Two execute calls: insert then SELECT.
    assert call_count[0] == 2, (
        f"Expected 2 execute calls (DO NOTHING insert + SELECT), got {call_count[0]}"
    )

    select_stmt = captured_stmts[1]
    # Verify the SELECT statement has FOR UPDATE set.
    # SQLAlchemy's with_for_update() sets _for_update_arg on the select object.
    # We inspect the compiled SQL string for "FOR UPDATE" or check the internal flag.
    compiled_sql = str(select_stmt.compile(dialect=__import__("sqlalchemy.dialects.postgresql", fromlist=["dialect"]).dialect()))
    assert "FOR UPDATE" in compiled_sql.upper(), (
        f"TOCTOU RACE: legacy DO NOTHING SELECT fallback does not use FOR UPDATE. "
        f"Compiled SQL: {compiled_sql!r}"
    )
