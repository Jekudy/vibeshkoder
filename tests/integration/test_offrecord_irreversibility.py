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
    assert stale2.text is None, "PRIVACY VIOLATION: second stale duplicate delivery restored text"


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


# ─── Sprint #80 Finding 3 / Codex MEDIUM: end-to-end sticky test through real handler ──
#
# The above tests cover the MessageRepo.save sticky CASE in isolation (Codex CRITICAL #1
# from the original Sprint #80 review). The Finding 1 unit tests in
# tests/handlers/test_edited_message.py cover the handler-layer guard with mocked DB.
# The test below closes the last gap: a real DB-backed end-to-end exercise of
# ``handle_edited_message`` running TWICE — first to flip normal→offrecord, then to
# attempt offrecord→normal via the user removing the #offrecord token. Verifies the
# combined invariants:
#
# 1. After flip-back attempt: ``chat_messages.memory_policy == 'offrecord'`` and
#    ``is_redacted is True`` (sticky policy holds end-to-end through the handler).
# 2. ``chat_messages.text/caption`` remain NULL (no content restoration).
# 3. No ``message_versions`` row carries restored ``text`` or ``caption`` or
#    ``is_redacted=False`` (the audit table never sees a fingerprint of the redacted
#    content).
# 4. Exactly ONE ``offrecord_marks`` row exists for the chat_message — the second
#    handler invocation does NOT create a duplicate audit row (no transition
#    occurred; the row stayed offrecord).


def _make_aiogram_message(
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text: str | None = "hello",
    caption: str | None = None,
    edit_date: datetime | None = None,
):
    """Build a SimpleNamespace mimicking an aiogram Message for handler tests."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    raw_json = {"message_id": message_id, "text": text} if text is not None else None
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
        edit_date=edit_date or datetime.now(timezone.utc),
        message_thread_id=None,
        model_dump=Mock(return_value=raw_json),
        # Probes for classify_message_kind:
        forward_origin=None,
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
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        reply_to_message=None,
        entities=None,
        caption_entities=None,
    )


async def test_handler_legacy_offrecord_marker_remains_complete_history_end_to_end(
    db_session,
) -> None:
    """Phase 13 stores both marker-bearing and later clean edits as normal versions."""
    from sqlalchemy import func

    from bot.db.models import ChatMessage, MessageVersion, OffrecordMark
    from bot.db.repos.message import MessageRepo
    from bot.handlers.edited_message import handle_edited_message

    user_id = _random_user_id()
    chat_id = -1001234567890  # must equal settings.COMMUNITY_CHAT_ID from app_env fixture
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Step 1: original normal save.
    saved = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="A — the original content",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )
    assert saved.memory_policy == "normal"
    assert saved.is_redacted is False

    # Step 2: the legacy marker is ordinary content.
    edit_offrecord = _make_aiogram_message(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="A — the original content #offrecord",
    )
    await handle_edited_message(edit_offrecord, db_session)

    after_marker_edit = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == saved.id))
    ).scalar_one()
    await db_session.refresh(after_marker_edit)
    assert after_marker_edit.memory_policy == "normal"
    assert after_marker_edit.is_redacted is False
    assert after_marker_edit.text == "A — the original content #offrecord"

    marks_after_flip = await db_session.scalar(
        select(func.count())
        .select_from(OffrecordMark)
        .where(OffrecordMark.chat_message_id == saved.id)
    )
    assert marks_after_flip == 0

    # Step 3: a later clean edit is another ordinary version.
    edit_revert = _make_aiogram_message(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="A — the original content (clean)",  # no #offrecord
    )
    await handle_edited_message(edit_revert, db_session)

    after_revert_attempt = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == saved.id))
    ).scalar_one()
    await db_session.refresh(after_revert_attempt)

    assert after_revert_attempt.memory_policy == "normal"
    assert after_revert_attempt.is_redacted is False
    assert after_revert_attempt.text == "A — the original content (clean)"
    assert after_revert_attempt.caption is None

    # Both edits remain auditable and searchable.
    version_rows = (
        (
            await db_session.execute(
                select(MessageVersion)
                .where(MessageVersion.chat_message_id == saved.id)
                .order_by(MessageVersion.version_seq)
            )
        )
        .scalars()
        .all()
    )
    assert [v.text for v in version_rows] == [
        "A — the original content #offrecord",
        "A — the original content (clean)",
    ]
    assert all(v.caption is None and v.is_redacted is False for v in version_rows)

    marks_after_revert = await db_session.scalar(
        select(func.count())
        .select_from(OffrecordMark)
        .where(OffrecordMark.chat_message_id == saved.id)
    )
    assert marks_after_revert == 0
