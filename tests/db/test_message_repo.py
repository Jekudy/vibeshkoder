"""T0-03 acceptance tests — MessageRepo.save is idempotent on (chat_id, message_id).

Test isolation: each test runs inside the ``db_session`` fixture's outer transaction which
is rolled back at fixture teardown. Tests do NOT call ``session.commit()`` — they call
``MessageRepo.save()`` (which flushes internally on the first insert) and verify state with
``session.execute(select(...))``.

Tests use random telegram ids and random message ids (high range, randomized per test) so
any leaked rows from a prior failed run cannot collide and concurrent test runs cannot
interfere.

Tests are SKIPPED if no postgres is reachable (see ``conftest.postgres_engine``).
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")


def _random_user_id() -> int:
    return random.randint(900_000_000, 999_999_999)


def _random_chat_id() -> int:
    # Telegram supergroup ids are negative bigints starting with -100.
    return -1_000_000_000_000 - random.randint(0, 999_999)


def _random_message_id() -> int:
    return random.randint(100_000, 999_999)


async def _create_user(session, telegram_id: int) -> None:
    """Insert a minimal User row so chat_messages FK is satisfied."""
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        session,
        telegram_id=telegram_id,
        username=f"u{telegram_id}",
        first_name="Test",
        last_name=None,
    )


async def _count_messages(session, chat_id: int, message_id: int) -> int:
    from bot.db.models import ChatMessage

    result = await session.execute(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
    )
    return len(result.scalars().all())


async def test_save_new_message_inserts_row(db_session) -> None:
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    saved = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        raw_json={"k": "v"},
    )

    assert saved.id is not None
    assert saved.chat_id == chat_id
    assert saved.message_id == message_id
    assert saved.user_id == user_id
    assert saved.text == "hello"
    assert await _count_messages(db_session, chat_id, message_id) == 1


async def test_save_duplicate_returns_existing_no_error(db_session) -> None:
    """Repeat save with same (chat_id, message_id) must NOT raise and must return the
    existing row's id."""
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    first = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        raw_json=None,
    )

    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello (would be duplicate)",
        date=when,
        raw_json=None,
    )

    assert second.id == first.id
    assert second.chat_id == first.chat_id
    assert second.message_id == first.message_id


async def test_save_duplicate_does_not_create_duplicate_row(db_session) -> None:
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="first",
        date=when,
        raw_json=None,
    )
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="second",
        date=when,
        raw_json=None,
    )
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="third",
        date=when,
        raw_json=None,
    )

    assert await _count_messages(db_session, chat_id, message_id) == 1


async def test_save_duplicate_preserves_first_inserted_text(db_session) -> None:
    """The architect's contract is "duplicate-safe save". Existing text is NOT overwritten
    on duplicate (Phase 1 message_versions will handle edits properly). The returned row's
    text equals the FIRST insert's text."""
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="original",
        date=when,
        raw_json=None,
    )

    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="changed text — must NOT overwrite",
        date=when,
        raw_json=None,
    )

    assert second.text == "original"


async def test_save_different_messages_in_same_chat_both_persist(db_session) -> None:
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    msg_id_a = _random_message_id()
    msg_id_b = _random_message_id()
    while msg_id_b == msg_id_a:
        msg_id_b = _random_message_id()

    await MessageRepo.save(
        db_session,
        message_id=msg_id_a,
        chat_id=chat_id,
        user_id=user_id,
        text="A",
        date=when,
        raw_json=None,
    )
    await MessageRepo.save(
        db_session,
        message_id=msg_id_b,
        chat_id=chat_id,
        user_id=user_id,
        text="B",
        date=when,
        raw_json=None,
    )

    assert await _count_messages(db_session, chat_id, msg_id_a) == 1
    assert await _count_messages(db_session, chat_id, msg_id_b) == 1


# ─── Issue #67 tests ─────────────────────────────────────────────────────────


async def test_save_duplicate_with_new_policy_refreshes_policy_fields(db_session) -> None:
    """AC2: duplicate delivery with explicit policy args must refresh memory_policy /
    is_redacted on the existing row."""
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )

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

    assert second.memory_policy == "offrecord"
    assert second.is_redacted is True


async def test_save_duplicate_with_both_none_preserves_existing_policy(db_session) -> None:
    """AC3: legacy callers that pass neither policy arg must NOT clobber existing policy
    fields with None/NULL."""
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )

    # Legacy call — neither memory_policy nor is_redacted passed.
    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
    )

    assert second.memory_policy == "offrecord"
    assert second.is_redacted is True


async def test_save_duplicate_with_only_policy_does_not_clobber_is_redacted(db_session) -> None:
    """AC2 selectivity: updating only memory_policy must leave is_redacted unchanged."""
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )

    # Duplicate with only memory_policy — no is_redacted kwarg.
    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        memory_policy="offrecord",
    )

    assert second.memory_policy == "offrecord"
    assert second.is_redacted is False


async def test_save_duplicate_does_not_overwrite_text_when_refreshing_policy(db_session) -> None:
    """AC4 irreversibility doctrine: text, caption, and raw_json must stay immutable
    even when policy fields are being refreshed on a duplicate delivery."""
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="original",
        date=when,
        caption="cap-A",
        raw_json={"k": "a"},
    )

    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="attacker",
        date=when,
        caption="cap-B",
        raw_json={"k": "b"},
        memory_policy="offrecord",
        is_redacted=True,
    )

    assert second.text == "original"
    assert second.memory_policy == "offrecord"
    # caption must not be overwritten on conflict — immutable alongside text.
    assert second.caption == "cap-A"
    # raw_json must not be overwritten on conflict — immutable alongside text.
    assert second.raw_json == {"k": "a"}


async def test_save_duplicate_with_only_policy_normal_does_not_unflip_redacted(
    db_session,
) -> None:
    """Asymmetric privacy invariant under sticky semantics (Sprint #80 fixup):

    Two stickiness rules apply to a duplicate save against an existing offrecord +
    is_redacted=True row:

    1. ``memory_policy``: sticky — once 'offrecord', cannot be downgraded to 'normal'
       by a re-save, even if the caller explicitly passes ``memory_policy='normal'``.
       The CASE WHEN expression in MessageRepo.save short-circuits to keep 'offrecord'.
    2. ``is_redacted``: sticky AND set-clause-selective — once True, cannot be flipped
       back to False. Additionally, if the caller does NOT pass ``is_redacted`` at all,
       the field is excluded from the SET clause entirely (#67 selectivity).

    Scenario: a message was first saved as offrecord (is_redacted=True). A duplicate
    delivery arrives with ``memory_policy='normal'`` only — no ``is_redacted`` kwarg.
    Both fields must remain at their more-restrictive values:
    - ``memory_policy`` stays 'offrecord' (sticky CASE blocks the downgrade).
    - ``is_redacted`` stays True (caller did not declare it AND sticky would block False).
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Insert with memory_policy='offrecord', is_redacted=True.
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="secret",
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )

    # Duplicate with memory_policy='normal' only — no is_redacted kwarg.
    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="secret",
        date=when,
        memory_policy="normal",
    )

    # memory_policy stays 'offrecord' — sticky CASE blocks the downgrade even though
    # the caller explicitly declared 'normal'. This is the post-Sprint-#80 contract.
    assert second.memory_policy == "offrecord"
    # is_redacted must remain True — caller did not declare it (selectivity preserved
    # the field), and even if they had, the sticky CASE would still block False.
    assert second.is_redacted is True


# ─── Codex CRITICAL: sticky policy regression tests (Sprint #80 fixup) ────────


async def test_save_duplicate_with_normal_policy_does_not_downgrade_offrecord(
    db_session,
) -> None:
    """Privacy invariant: once a row is 'offrecord' + is_redacted=True, a stale duplicate
    original delivery (memory_policy='normal', is_redacted=False) MUST NOT downgrade either
    field.

    Reproduces the exact Codex CRITICAL race:
    1. Row flipped to offrecord via edited_message handler.
    2. Telegram polling glitch re-delivers original M with normal policy.
    3. MessageRepo.save must NOT overwrite offrecord state with normal.
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Step 1: insert as offrecord (simulates edited_message flip already happened).
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=None,  # already redacted
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )

    # Step 2: stale duplicate original delivery with normal policy and is_redacted=False.
    result = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="original text before offrecord",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )

    # Both fields must stay at their more-restrictive values.
    assert result.memory_policy == "offrecord", (
        f"PRIVACY VIOLATION: memory_policy downgraded from 'offrecord' to "
        f"'{result.memory_policy}' by stale duplicate delivery"
    )
    assert result.is_redacted is True, (
        "PRIVACY VIOLATION: is_redacted flipped back to False by stale duplicate delivery"
    )


async def test_save_duplicate_with_only_normal_policy_does_not_downgrade_offrecord(
    db_session,
) -> None:
    """Variant: only memory_policy='normal' passed (no is_redacted kwarg).

    Same stale-duplicate race but caller omits is_redacted entirely. The sticky CASE
    expression for memory_policy must still keep 'offrecord'.
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Insert as offrecord.
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=None,
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )

    # Stale duplicate with only memory_policy='normal' — no is_redacted kwarg.
    result = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="stale text",
        date=when,
        memory_policy="normal",
    )

    assert result.memory_policy == "offrecord", (
        f"PRIVACY VIOLATION: memory_policy='normal' stale duplicate downgraded offrecord row. "
        f"Got memory_policy='{result.memory_policy}'"
    )


async def test_save_duplicate_only_is_redacted_false_does_not_unflag_redacted(
    db_session,
) -> None:
    """Variant: only is_redacted=False passed (no memory_policy kwarg).

    A caller passing only is_redacted=False must not be able to unflag a row that has
    is_redacted=True. The sticky OR-semantics for is_redacted must prevent this.
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Insert as is_redacted=True (any policy).
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=None,
        date=when,
        memory_policy="offrecord",
        is_redacted=True,
    )

    # Duplicate with only is_redacted=False — no memory_policy kwarg.
    result = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="stale text",
        date=when,
        is_redacted=False,
    )

    assert result.is_redacted is True, (
        f"PRIVACY VIOLATION: is_redacted=False stale duplicate unset the redaction flag. "
        f"Got is_redacted={result.is_redacted!r}"
    )


# ─── C1: forgotten policy is sticky (invariant 9 — tombstones are durable) ───


async def test_save_duplicate_with_normal_does_not_downgrade_forgotten(
    db_session,
) -> None:
    """Invariant 9 (HANDOFF.md §1): tombstones are durable and not casually rolled back.

    Scenario: cascade worker sets memory_policy='forgotten', is_redacted=True on a row.
    A subsequent stale Telegram redelivery calls MessageRepo.save with
    memory_policy='normal' and is_redacted=False. The row MUST stay 'forgotten' and
    is_redacted MUST stay True.

    This is the 'forgotten' analogue of the offrecord stickiness tests above.
    """
    from bot.db.repos.message import MessageRepo

    user_id = _random_user_id()
    chat_id = _random_chat_id()
    message_id = _random_message_id()
    when = datetime.now(timezone.utc)

    await _create_user(db_session, user_id)

    # Step 1: cascade worker applies forget → sets forgotten + redacted.
    await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=None,  # already redacted by cascade
        date=when,
        memory_policy="forgotten",
        is_redacted=True,
    )

    # Step 2: stale redelivery arrives with memory_policy='normal', is_redacted=False.
    result = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="stale original text",
        date=when,
        memory_policy="normal",
        is_redacted=False,
    )

    assert result.memory_policy == "forgotten", (
        f"INVARIANT 9 VIOLATION: memory_policy='normal' stale duplicate downgraded 'forgotten' row. "
        f"Got memory_policy='{result.memory_policy}'"
    )
    assert result.is_redacted is True, (
        f"INVARIANT 9 VIOLATION: is_redacted=False stale duplicate unset redaction on 'forgotten' row. "
        f"Got is_redacted={result.is_redacted!r}"
    )
