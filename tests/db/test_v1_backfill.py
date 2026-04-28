"""T1-07 acceptance tests — v1 backfill via ``bot/services/backfill.py``.

The alembic migration 008 delegates to ``backfill_v1_message_versions`` so testing
the function directly proves the migration's behavior end-to-end (subject to the
async engine glue in the migration itself, which is hard to unit-test without a
fresh alembic environment).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=8_900_000_000)
_msg_counter = itertools.count(start=970_000)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _random_chat_id() -> int:
    return -1_000_000_000_000 - (next(_msg_counter) % 1_000_000)


async def _make_legacy_chat_message(
    db_session, *, text: str | None = "legacy text", caption: str | None = None
) -> int:
    """Insert a chat_messages row simulating the gatekeeper-era shape (no
    current_version_id, no message_kind)."""
    from bot.db.models import ChatMessage
    from bot.db.repos.user import UserRepo

    user_id = _next_user()
    chat_id = _random_chat_id()
    when = datetime.now(timezone.utc)

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username="u",
        first_name="U",
        last_name=None,
    )

    msg = ChatMessage(
        message_id=_next_msg_id(),
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        caption=caption,
        date=when,
    )
    db_session.add(msg)
    await db_session.flush()
    return msg.id


# ─── happy path ────────────────────────────────────────────────────────────────────────────


async def test_backfill_creates_v1_for_each_legacy_row(db_session) -> None:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.services.backfill import backfill_v1_message_versions

    msg_ids = [await _make_legacy_chat_message(db_session, text=f"row {i}") for i in range(5)]

    count = await backfill_v1_message_versions(db_session, batch_size=10)
    assert count == 5

    for msg_id in msg_ids:
        msg = (
            await db_session.execute(select(ChatMessage).where(ChatMessage.id == msg_id))
        ).scalar_one()
        await db_session.refresh(msg)
        assert msg.current_version_id is not None

        v = (
            await db_session.execute(
                select(MessageVersion).where(MessageVersion.chat_message_id == msg_id)
            )
        ).scalar_one()
        assert v.version_seq == 1
        assert v.content_hash is not None
        assert len(v.content_hash) == 64  # sha256 hex
        # captured_at must be pinned to msg.date (HANDOFF.md §6 #5 + issue #31).
        assert v.captured_at == msg.date


# ─── idempotency ───────────────────────────────────────────────────────────────────────────


async def test_backfill_is_idempotent_on_rerun(db_session) -> None:
    from bot.services.backfill import backfill_v1_message_versions

    for i in range(3):
        await _make_legacy_chat_message(db_session, text=f"x{i}")

    first = await backfill_v1_message_versions(db_session, batch_size=10)
    second = await backfill_v1_message_versions(db_session, batch_size=10)

    assert first == 3
    assert second == 0  # nothing left to backfill


# ─── chunked / batched processing ─────────────────────────────────────────────────────────


async def test_backfill_chunks_correctly_when_more_rows_than_batch_size(db_session) -> None:
    from bot.services.backfill import backfill_v1_message_versions

    for i in range(7):
        await _make_legacy_chat_message(db_session, text=f"chunk{i}")

    count = await backfill_v1_message_versions(db_session, batch_size=2)
    assert count == 7


# ─── NULL text handled ─────────────────────────────────────────────────────────────────────


async def test_backfill_handles_null_text_rows(db_session) -> None:
    """Legacy rows where text is NULL (e.g. media messages) must still get a v1
    row with a stable hash."""
    from bot.db.models import MessageVersion
    from bot.services.backfill import backfill_v1_message_versions

    msg_id = await _make_legacy_chat_message(db_session, text=None, caption=None)

    count = await backfill_v1_message_versions(db_session)
    assert count == 1

    v = (
        await db_session.execute(
            select(MessageVersion).where(MessageVersion.chat_message_id == msg_id)
        )
    ).scalar_one()
    assert v.content_hash is not None
    assert len(v.content_hash) == 64


# ─── pre-existing v1 rows are not duplicated ───────────────────────────────────────────────


async def test_backfill_skips_messages_with_existing_current_version_id(db_session) -> None:
    """If a message already has current_version_id set (e.g. from live ingestion),
    the backfill must not touch it."""
    from bot.db.models import ChatMessage
    from bot.db.repos.message_version import MessageVersionRepo
    from bot.services.backfill import backfill_v1_message_versions

    pre_msg_id = await _make_legacy_chat_message(db_session, text="already wired")
    v = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=pre_msg_id,
        content_hash="manual-hash",
        text="already wired",
    )
    msg = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == pre_msg_id))
    ).scalar_one()
    msg.current_version_id = v.id
    await db_session.flush()

    other_id = await _make_legacy_chat_message(db_session, text="needs backfill")

    count = await backfill_v1_message_versions(db_session)
    assert count == 1  # only the not-yet-wired message

    other_msg = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == other_id))
    ).scalar_one()
    await db_session.refresh(other_msg)
    assert other_msg.current_version_id is not None


# ─── content_hash deterministic ────────────────────────────────────────────────────────────


def test_compute_content_hash_deterministic(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="hello", caption=None, message_kind=None)
    b = compute_content_hash(text="hello", caption=None, message_kind=None)
    c = compute_content_hash(text="hello", caption=None, message_kind="text")
    d = compute_content_hash(text="HELLO", caption=None, message_kind=None)

    assert a == b  # same inputs → same hash
    assert a == c  # message_kind=None defaults to 'text', so same as explicit 'text'
    assert a != d  # case-sensitive


def test_compute_content_hash_handles_none(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    h = compute_content_hash(text=None, caption=None, message_kind=None)
    assert len(h) == 64  # sha256 hex


def test_compute_content_hash_includes_caption(app_env) -> None:
    from bot.services.content_hash import compute_content_hash

    a = compute_content_hash(text="hi", caption=None, message_kind=None)
    b = compute_content_hash(text="hi", caption="cap", message_kind=None)
    assert a != b


# ─── Finding 2 (Codex Sprint #80): backfill TOCTOU race against live edits ───
#
# The pre-fix backfill SELECTed a batch of rows then iterated them in Python without
# any per-row lock. A concurrent edited_message handler could offrecord-flip a row
# between the SELECT and the version INSERT, producing a stale UNREDACTED version row
# carrying the original text — privacy bypass. Tests below are state-based: they spy
# on session.execute calls (no real concurrency needed) and assert the backfill takes
# the per-row advisory lock, re-reads with FOR UPDATE, and uses the post-lock fresh
# row state (not the cached pre-lock batch state).


def test_backfill_acquires_advisory_lock_per_row(app_env) -> None:
    """Backfill must call ``pg_advisory_xact_lock`` keyed by ``chat_msg:{chat_id}:{message_id}``
    for each row before reading or inserting anything for that row. Mirrors the live
    handlers in chat_messages.py and edited_message.py.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import import_module

    backfill_mod = import_module("bot.services.backfill")

    # Build two MagicMock rows to simulate a 2-row batch from the SELECT.
    row1 = MagicMock()
    row1.id = 101
    row1.chat_id = -1001234567890
    row1.message_id = 555_001
    row1.text = "row1 text"
    row1.caption = None
    row1.message_kind = "text"
    row1.is_redacted = False
    row1.raw_update_id = None
    row1.date = datetime.now(timezone.utc)
    row1.current_version_id = None

    row2 = MagicMock()
    row2.id = 102
    row2.chat_id = -1001234567890
    row2.message_id = 555_002
    row2.text = "row2 text"
    row2.caption = None
    row2.message_kind = "text"
    row2.is_redacted = False
    row2.raw_update_id = None
    row2.date = datetime.now(timezone.utc)
    row2.current_version_id = None

    advisory_lock_calls: list[dict] = []
    select_call_count = {"n": 0}

    async def capture_execute(stmt, params=None, *args, **kwargs):
        # Detect advisory lock SQL.
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        if "pg_advisory_xact_lock" in stmt_str:
            advisory_lock_calls.append({"params": params})
            return MagicMock()

        # First SELECT returns the batch; subsequent SELECTs (per-row re-read) return
        # the same row by id. UPDATEs are no-ops for capture purposes.
        result = MagicMock()
        if "SELECT" in stmt_str.upper():
            select_call_count["n"] += 1
            if select_call_count["n"] == 1:
                # Initial batch SELECT.
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[row1, row2]))
                )
            else:
                # Per-row re-read after lock — return whichever row was just locked.
                # We cheat by alternating: the test only checks lock CALLS, not order.
                if select_call_count["n"] == 2:
                    result.scalar_one = MagicMock(return_value=row1)
                else:
                    result.scalar_one = MagicMock(return_value=row2)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()
    session.add = MagicMock()

    asyncio.run(backfill_mod.backfill_v1_message_versions(session, batch_size=10))

    # Two rows in the batch → two advisory lock calls, with the correct key per row.
    assert len(advisory_lock_calls) == 2, (
        f"Expected 2 advisory lock calls (one per row), got {len(advisory_lock_calls)}: "
        f"{advisory_lock_calls}"
    )

    # Each lock call must carry the key 'chat_msg:{chat_id}:{message_id}'.
    expected_keys = {
        f"chat_msg:{row1.chat_id}:{row1.message_id}",
        f"chat_msg:{row2.chat_id}:{row2.message_id}",
    }
    actual_keys = {call["params"].get("k") for call in advisory_lock_calls if call["params"]}
    assert actual_keys == expected_keys, (
        f"Advisory lock keys mismatch. Expected {expected_keys}, got {actual_keys}"
    )


def test_backfill_skips_row_concurrently_filled_during_lock_wait(app_env) -> None:
    """If a concurrent live-edit handler set ``current_version_id`` after our SELECT
    but before our advisory lock acquired, the post-lock re-read returns a row with
    ``current_version_id IS NOT NULL`` — backfill MUST skip it (do not insert a stale
    duplicate v1).
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import import_module

    backfill_mod = import_module("bot.services.backfill")

    # Pre-lock batch row: current_version_id was None at SELECT time.
    pre_lock_row = MagicMock()
    pre_lock_row.id = 200
    pre_lock_row.chat_id = -1001234567890
    pre_lock_row.message_id = 666_001
    pre_lock_row.text = "stale — concurrently flipped"
    pre_lock_row.caption = None
    pre_lock_row.message_kind = "text"
    pre_lock_row.is_redacted = False
    pre_lock_row.raw_update_id = None
    pre_lock_row.date = datetime.now(timezone.utc)
    pre_lock_row.current_version_id = None  # was None when we read the batch

    # Post-lock fresh row: a concurrent live handler already wrote v1.
    post_lock_row = MagicMock()
    post_lock_row.id = 200
    post_lock_row.chat_id = pre_lock_row.chat_id
    post_lock_row.message_id = pre_lock_row.message_id
    post_lock_row.text = None  # also offrecord-flipped
    post_lock_row.caption = None
    post_lock_row.message_kind = "text"
    post_lock_row.is_redacted = True
    post_lock_row.raw_update_id = None
    post_lock_row.date = pre_lock_row.date
    post_lock_row.current_version_id = 9_999  # populated by concurrent edit handler

    select_call_count = {"n": 0}
    insert_seen = {"called": False}

    async def capture_execute(stmt, params=None, *args, **kwargs):
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        if "pg_advisory_xact_lock" in stmt_str:
            return MagicMock()
        result = MagicMock()
        if "SELECT" in stmt_str.upper():
            select_call_count["n"] += 1
            if select_call_count["n"] == 1:
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[pre_lock_row]))
                )
            else:
                # Post-lock fresh re-read: returns the concurrently-filled row.
                result.scalar_one = MagicMock(return_value=post_lock_row)
        elif "UPDATE" in stmt_str.upper():
            # ChatMessage UPDATE setting current_version_id — must NOT be issued.
            insert_seen["called"] = True
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()

    add_seen = {"called": False}

    def add_capture(_obj):
        # session.add of a MessageVersion would be the leaky write — track it.
        add_seen["called"] = True

    session.add = MagicMock(side_effect=add_capture)

    count = asyncio.run(backfill_mod.backfill_v1_message_versions(session, batch_size=10))

    # No new MessageVersion was added (the row was already wired by the concurrent edit).
    assert add_seen["called"] is False, (
        "PRIVACY VIOLATION: backfill inserted a stale MessageVersion for a row that "
        "was concurrently filled by the live edit handler. The post-lock re-read should "
        "have detected current_version_id IS NOT NULL and skipped."
    )
    # Backfill must not bump the count when it skips a concurrently-filled row.
    assert count == 0, (
        f"Backfill returned count={count} for a row it should have skipped (concurrently "
        f"filled). Expected 0."
    )


def test_backfill_uses_post_lock_fresh_row_for_hash_and_version(app_env) -> None:
    """The hash + version content come from the POST-LOCK row state, not the pre-lock
    batch snapshot. If a concurrent edit offrecord-flipped the row to ``text=None,
    is_redacted=True`` between SELECT and lock acquisition, the backfill must use the
    redacted state for both ``content_hash`` and the inserted ``message_versions`` row.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import import_module

    backfill_mod = import_module("bot.services.backfill")

    pre_lock_row = MagicMock()
    pre_lock_row.id = 300
    pre_lock_row.chat_id = -1001234567890
    pre_lock_row.message_id = 777_001
    pre_lock_row.text = "raw secret content"  # SELECT-time stale value
    pre_lock_row.caption = None
    pre_lock_row.message_kind = "text"
    pre_lock_row.is_redacted = False
    pre_lock_row.raw_update_id = None
    pre_lock_row.date = datetime.now(timezone.utc)
    pre_lock_row.current_version_id = None

    # Concurrent edit flipped the row to offrecord. current_version_id still None
    # because the edit didn't yet insert a v1 (e.g. it was a flip of an unwired row).
    post_lock_row = MagicMock()
    post_lock_row.id = 300
    post_lock_row.chat_id = pre_lock_row.chat_id
    post_lock_row.message_id = pre_lock_row.message_id
    post_lock_row.text = None
    post_lock_row.caption = None
    post_lock_row.message_kind = "text"
    post_lock_row.is_redacted = True  # concurrently flipped
    post_lock_row.raw_update_id = None
    post_lock_row.date = pre_lock_row.date
    post_lock_row.current_version_id = None

    select_call_count = {"n": 0}
    added_versions: list = []

    async def capture_execute(stmt, params=None, *args, **kwargs):
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        if "pg_advisory_xact_lock" in stmt_str:
            return MagicMock()
        result = MagicMock()
        if "SELECT" in stmt_str.upper():
            select_call_count["n"] += 1
            if select_call_count["n"] == 1:
                result.scalars = MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[pre_lock_row]))
                )
            else:
                result.scalar_one = MagicMock(return_value=post_lock_row)
        return result

    def add_capture(obj):
        added_versions.append(obj)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=capture_execute)
    session.flush = AsyncMock()
    session.add = MagicMock(side_effect=add_capture)

    asyncio.run(backfill_mod.backfill_v1_message_versions(session, batch_size=10))

    assert len(added_versions) == 1, (
        f"Expected 1 MessageVersion added, got {len(added_versions)}"
    )
    v = added_versions[0]

    # PRIVACY: text/caption come from post_lock_row (None), NOT from pre_lock_row
    # ("raw secret content"). is_redacted is True.
    assert v.text is None, (
        f"PRIVACY VIOLATION: backfill used pre-lock stale text {v.text!r}. The post-lock "
        f"fresh row had text=None (offrecord-flipped concurrently)."
    )
    assert v.caption is None
    assert v.is_redacted is True, (
        "PRIVACY VIOLATION: backfill wrote is_redacted=False even though the post-lock "
        "fresh row was offrecord-flipped to is_redacted=True."
    )

    # Sanity: content_hash must be the redacted-state hash, not chv1 of "raw secret content".
    from bot.services.content_hash import compute_content_hash

    redacted_hash = compute_content_hash(
        text=None, caption=None, message_kind="text", entities=None
    )
    raw_hash = compute_content_hash(
        text="raw secret content", caption=None, message_kind="text", entities=None
    )
    assert v.content_hash == redacted_hash, (
        f"PRIVACY VIOLATION: content_hash leaks raw content fingerprint. "
        f"Got {v.content_hash[:16]}…, expected redacted-state hash {redacted_hash[:16]}…, "
        f"raw-state hash would be {raw_hash[:16]}…"
    )
