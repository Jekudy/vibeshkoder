"""Offrecord irreversibility integration tests — Codex CRITICAL (Sprint #80 fixup).

These tests reproduce the exact privacy-invariant downgrade scenario from the Codex review:
a stale duplicate ORIGINAL delivery must NOT overwrite an offrecord-flipped row's policy
fields back to 'normal' / is_redacted=False.

Strategy: real DB-backed assertions using the same ``db_session`` fixture as
``test_message_repo.py``. Tests are SKIPPED if postgres is not reachable. CI always runs
against a real postgres service container.

Scenario (from Codex CRITICAL finding):
  1. save_chat_message(M, text="A")           → memory_policy='normal', is_redacted=False
  2. handle_edited_message(M, text="A #offrecord") → flip to offrecord, text=NULL
  3. save_chat_message(M, text="A") AGAIN     → stale duplicate (polling glitch / restart)
  4. Assert: row STILL has memory_policy='offrecord', is_redacted=True, text=NULL.
  5. (Optional deeper) A subsequent normal edit must also not restore content.

The tests exercise the sticky CASE logic in MessageRepo.save without depending on handler
internals — we call MessageRepo.save directly and simulate the offrecord state as set by
the edited_message handler.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

pytestmark = pytest.mark.usefixtures("app_env")


def _random_user_id() -> int:
    return random.randint(800_000_000, 899_999_999)


def _random_chat_id() -> int:
    return -1_000_000_000_000 - random.randint(0, 999_999)


def _random_message_id() -> int:
    return random.randint(200_000, 299_999)


async def _create_user(session, telegram_id: int) -> None:
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        session,
        telegram_id=telegram_id,
        username=f"irrev{telegram_id}",
        first_name="Test",
        last_name=None,
    )


async def test_offrecord_then_stale_duplicate_then_normal_edit_does_not_restore_content(
    db_session,
) -> None:
    """End-to-end Codex CRITICAL scenario.

    1. Initial message saved with normal policy.
    2. State manually set to offrecord (simulating edited_message handler flip).
    3. Stale duplicate original delivery with normal policy arrives via MessageRepo.save.
    4. Assert row stays offrecord, is_redacted=True, text=NULL.
    5. (Deeper) Another normal save also cannot restore text.
    """
    from bot.db.models import ChatMessage
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Step 1: original message arrives, saved as normal.
    original = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="A — the original content",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )
    assert original.memory_policy == "normal"
    assert original.is_redacted is False
    assert original.text == "A — the original content"

    # Step 2: simulate edited_message handler flip — manually update as the handler would.
    # (In production this goes through _apply_offrecord_flip; here we set state directly
    # to isolate the MessageRepo.save sticky logic from handler dependencies.)
    await db_session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == original.id)
        .values(
            text=None,
            caption=None,
            raw_json=None,
            is_redacted=True,
            memory_policy="offrecord",
        )
    )
    await db_session.flush()

    # Verify the flip took effect.
    flipped_result = await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == original.id)
    )
    flipped = flipped_result.scalar_one()
    assert flipped.memory_policy == "offrecord"
    assert flipped.is_redacted is True
    assert flipped.text is None

    # Step 3: stale duplicate original delivery via MessageRepo.save — THE BUG PATH.
    stale = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="A — the original content",  # same text as original
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )

    # Step 4: CRITICAL assertions — sticky policy must hold.
    assert stale.memory_policy == "offrecord", (
        "PRIVACY VIOLATION: stale duplicate delivery downgraded memory_policy from "
        f"'offrecord' to '{stale.memory_policy}'"
    )
    assert stale.is_redacted is True, (
        "PRIVACY VIOLATION: stale duplicate delivery flipped is_redacted back to False"
    )
    # text/caption must remain NULL — content fields are immutable on conflict.
    assert stale.text is None, (
        f"PRIVACY VIOLATION: stale duplicate delivery restored text. Got: {stale.text!r}"
    )

    # Step 5: a second normal save also cannot restore content.
    stale2 = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="A — second attempt",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )
    assert stale2.memory_policy == "offrecord", (
        "PRIVACY VIOLATION: second stale duplicate delivery downgraded memory_policy"
    )
    assert stale2.is_redacted is True, (
        "PRIVACY VIOLATION: second stale duplicate delivery unset is_redacted"
    )
    assert stale2.text is None, (
        "PRIVACY VIOLATION: second stale duplicate delivery restored text"
    )


async def test_offrecord_row_accepts_repeated_offrecord_saves_idempotently(
    db_session,
) -> None:
    """An already-offrecord row must accept further saves with offrecord policy without error.

    The sticky CASE must not block legitimate policy-consistent re-saves. This verifies
    that the CASE logic correctly passes through offrecord→offrecord (same policy, same
    redaction) without violating idempotency.
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Initial offrecord save.
    first = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=None,
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )
    assert first.memory_policy == "offrecord"

    # Second offrecord save — must be idempotent.
    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=None,
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )
    assert second.memory_policy == "offrecord"
    assert second.is_redacted is True
    assert second.id == first.id


async def test_normal_to_offrecord_upgrade_via_save_is_allowed(
    db_session,
) -> None:
    """Upgrading from 'normal' to 'offrecord' via MessageRepo.save must succeed.

    The sticky CASE only blocks downgrades. An upgrade (normal→offrecord) must still
    be applied — this tests that the CASE else-branch correctly picks up EXCLUDED.memory_policy
    when the stored value is NOT 'offrecord'.
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # First save as normal.
    first = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )
    assert first.memory_policy == "normal"

    # Upgrade to offrecord — must succeed (sticky allows upgrades).
    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )
    assert second.memory_policy == "offrecord", (
        f"Upgrade normal→offrecord must be applied. Got: '{second.memory_policy}'"
    )
    assert second.is_redacted is True
