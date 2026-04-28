"""T3-05 acceptance tests — reimport tombstone prevention service.

Tests for bot.services.import_tombstone:
  - check_tombstone: returns first active tombstone matching any of three keys
  - record_tombstone_skip: appends skip entry to stats_json dict

Isolation: all DB tests use db_session fixture (outer-tx rollback).
Tests MUST NOT call session.commit().

Privacy hardening: status='failed' STILL blocks import (see test #6).
"""

from __future__ import annotations

import itertools

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_key_counter = itertools.count(start=9_700_000)


def _unique_key(prefix: str) -> str:
    n = next(_key_counter)
    return f"{prefix}:{n}"


async def _create_tombstone(db_session, *, tombstone_key: str) -> object:
    """Helper: create a ForgetEvent with the given tombstone_key, minimal required fields."""
    from bot.db.repos.forget_event import ForgetEventRepo

    return await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: no tombstones → returns None
# ──────────────────────────────────────────────────────────────────────────────


async def test_check_no_tombstone_returns_none(db_session) -> None:
    """Clean state (no forget_events). check_tombstone for any args → None."""
    from bot.services.import_tombstone import check_tombstone

    result = await check_tombstone(
        db_session,
        chat_id=-100,
        message_id=9_999_999,
        content_hash=None,
        user_tg_id=None,
    )
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: message:{chat_id}:{message_id} key hits
# ──────────────────────────────────────────────────────────────────────────────


async def test_check_message_key_hits(db_session) -> None:
    """Create tombstone with message:-100:42 key. check_tombstone → returns event."""
    from bot.services.import_tombstone import check_tombstone

    ev = await _create_tombstone(db_session, tombstone_key="message:-100:42")

    result = await check_tombstone(
        db_session,
        chat_id=-100,
        message_id=42,
        content_hash=None,
        user_tg_id=None,
    )

    assert result is not None
    assert result.id == ev.id
    assert result.tombstone_key == "message:-100:42"


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: message_hash:{content_hash} key hits
# ──────────────────────────────────────────────────────────────────────────────


async def test_check_message_hash_key_hits(db_session) -> None:
    """Compute hash via compute_content_hash. Create tombstone with message_hash:<hash>.
    check_tombstone with mismatched chat_id/message_id but matching content_hash → returns event.
    """
    from bot.services.content_hash import compute_content_hash
    from bot.services.import_tombstone import check_tombstone

    hash_value = compute_content_hash("hello world", None, "text")
    tombstone_key = f"message_hash:{hash_value}"

    ev = await _create_tombstone(db_session, tombstone_key=tombstone_key)

    # message: key won't hit (different chat_id/message_id combination)
    result = await check_tombstone(
        db_session,
        chat_id=-100,
        message_id=999,
        content_hash=hash_value,
        user_tg_id=None,
    )

    assert result is not None
    assert result.id == ev.id
    assert result.tombstone_key == tombstone_key


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: user:{user_tg_id} key hits
# ──────────────────────────────────────────────────────────────────────────────


async def test_check_user_key_hits(db_session) -> None:
    """Create tombstone with user:12345 key. check_tombstone with user_tg_id=12345 → returns event."""
    from bot.services.import_tombstone import check_tombstone

    ev = await _create_tombstone(db_session, tombstone_key="user:12345")

    result = await check_tombstone(
        db_session,
        chat_id=-100,
        message_id=999,
        content_hash=None,
        user_tg_id=12345,
    )

    assert result is not None
    assert result.id == ev.id
    assert result.tombstone_key == "user:12345"


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: message key takes precedence over user key
# ──────────────────────────────────────────────────────────────────────────────


async def test_message_key_takes_precedence_over_user(db_session) -> None:
    """Create BOTH message:-100:42 AND user:12345 tombstones.
    check_tombstone with both keys present → returned event matches message:-100:42.
    Verifies priority order: message > message_hash > user.
    """
    from bot.services.import_tombstone import check_tombstone

    msg_ev = await _create_tombstone(db_session, tombstone_key="message:-100:42")
    _user_ev = await _create_tombstone(db_session, tombstone_key="user:12345")

    result = await check_tombstone(
        db_session,
        chat_id=-100,
        message_id=42,
        content_hash=None,
        user_tg_id=12345,
    )

    assert result is not None
    assert result.id == msg_ev.id
    assert result.tombstone_key == "message:-100:42"


# ──────────────────────────────────────────────────────────────────────────────
# Test 6: failed status STILL blocks import (privacy hardening)
# ──────────────────────────────────────────────────────────────────────────────


async def test_failed_status_still_blocks(db_session) -> None:
    """A tombstone in status='failed' STILL blocks re-import.

    Privacy doctrine: a failed cascade means content may still be live —
    re-importing is even MORE dangerous than a clean tombstone. So: if
    get_by_tombstone_key returns a row, that row blocks the import regardless
    of status (no status filter in check_tombstone).

    Lifecycle: pending → processing → failed.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.import_tombstone import check_tombstone

    ev = await _create_tombstone(db_session, tombstone_key="message:-200:77")

    # Advance to failed via the state machine
    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")
    await ForgetEventRepo.mark_status(db_session, ev.id, status="failed")

    # Confirm the row is now in 'failed'
    failed_ev = await ForgetEventRepo.get_by_tombstone_key(db_session, "message:-200:77")
    assert failed_ev is not None
    assert failed_ev.status == "failed"

    # Despite failed status, check_tombstone MUST still return the event.
    result = await check_tombstone(
        db_session,
        chat_id=-200,
        message_id=77,
        content_hash=None,
        user_tg_id=None,
    )

    assert result is not None, (
        "A 'failed' tombstone must still block re-import. "
        "check_tombstone must NOT filter by status."
    )
    assert result.id == ev.id
    assert result.status == "failed"


# ──────────────────────────────────────────────────────────────────────────────
# Test 7: record_tombstone_skip appends entries correctly
# ──────────────────────────────────────────────────────────────────────────────


def test_record_tombstone_skip_appends_entry() -> None:
    """record_tombstone_skip builds stats_json['skipped_tombstones'] list.

    Start with None → first call returns dict with 1 entry.
    Second call on the result → dict with 2 entries.
    All entry fields are populated correctly.
    Immutability: first result is NOT mutated by the second call.
    """
    from bot.services.import_tombstone import record_tombstone_skip

    # First call: stats_json=None
    result1 = record_tombstone_skip(
        None,
        matched_key="message:-100:42",
        matched_status="completed",
        forget_event_id=101,
        export_message_id=42,
        chat_id=-100,
    )

    assert "skipped_tombstones" in result1
    assert len(result1["skipped_tombstones"]) == 1

    entry1 = result1["skipped_tombstones"][0]
    assert entry1["matched_key"] == "message:-100:42"
    assert entry1["matched_status"] == "completed"
    assert entry1["forget_event_id"] == 101
    assert entry1["export_message_id"] == 42
    assert entry1["chat_id"] == -100

    # Second call: accumulates
    result2 = record_tombstone_skip(
        result1,
        matched_key="user:12345",
        matched_status="failed",
        forget_event_id=202,
        export_message_id=99,
        chat_id=-100,
    )

    assert len(result2["skipped_tombstones"]) == 2

    entry2 = result2["skipped_tombstones"][1]
    assert entry2["matched_key"] == "user:12345"
    assert entry2["matched_status"] == "failed"
    assert entry2["forget_event_id"] == 202
    assert entry2["export_message_id"] == 99
    assert entry2["chat_id"] == -100

    # Verify immutability: result1 was NOT mutated by the second call
    assert len(result1["skipped_tombstones"]) == 1
