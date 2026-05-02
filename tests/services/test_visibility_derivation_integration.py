"""Integration test for bot.services.visibility_derivation.

Uses the real postgres fixture (db_session from conftest). Inserts actual
chat_messages + message_versions + forget_events rows, then asserts that
derive_card_visibility returns the correct result end-to-end.

If postgres is unreachable, the test is SKIPPED (not failed), consistent with
the project's conftest.py pattern.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_uid_counter = itertools.count(start=9_200_000_000)
_msg_counter = itertools.count(start=920_000)
_chat_counter = itertools.count(start=200)


def _next_uid() -> int:
    return next(_uid_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -2_000_000_000_000 - next(_chat_counter)


async def _create_version(
    db_session,
    *,
    memory_policy: str = "normal",
    is_redacted: bool = False,
    content_hash: str | None = None,
) -> tuple[int, int]:
    """Insert real ChatMessage + MessageVersion rows. Returns (chat_message_id, version_id)."""
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    uid = _next_uid()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"integ-user-{uid}",
        first_name="Integration",
        last_name=None,
    )

    chat_id = _next_chat_id()
    msg_id = _next_msg_id()
    when = datetime.now(UTC)
    hash_val = content_hash or f"integ-hash-{uuid4().hex[:12]}"

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text=None if is_redacted else "integration test message",
        date=when,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
        content_hash=hash_val,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=None if is_redacted else "integration test message",
        content_hash=hash_val,
        is_redacted=is_redacted,
    )
    db_session.add(ver)
    await db_session.flush()

    return msg.id, ver.id


async def _create_tombstone(db_session, *, content_hash: str) -> int:
    """Insert a real forget_events row with a message_hash tombstone. Returns its id."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message_hash",
        target_id=None,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"message_hash:{content_hash}",
    )
    return ev.id


# ─── Integration test ─────────────────────────────────────────────────────────


async def test_integration_mixed_sources(db_session) -> None:
    """End-to-end integration test with real postgres.

    Scenario:
    - 3 sources: normal, nomem, and offrecord
    - Expected: REDACTED (highest precedence)
    - Also verifies that blocking_source_ids lists only the non-visible versions.

    This is the core invariant: no matter how many visible sources exist,
    a single offrecord/redacted source must block the entire artifact.
    """
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    _, v_normal = await _create_version(db_session, memory_policy="normal")
    _, v_nomem = await _create_version(db_session, memory_policy="nomem")
    _, v_offrecord = await _create_version(db_session, memory_policy="offrecord")

    result = await derive_card_visibility(db_session, [v_normal, v_nomem, v_offrecord])

    # REDACTED wins over NOMEM and VISIBLE
    assert result.visibility == CardVisibility.REDACTED, (
        f"Expected REDACTED, got {result.visibility}. "
        "An offrecord source must block the artifact regardless of other sources."
    )

    # The blocking ids must include v_offrecord and v_nomem (both non-visible)
    assert v_offrecord in result.blocking_source_ids, (
        "offrecord version must be in blocking_source_ids"
    )
    assert v_nomem in result.blocking_source_ids, (
        "nomem version must be in blocking_source_ids"
    )
    assert v_normal not in result.blocking_source_ids, (
        "normal (visible) version must NOT be in blocking_source_ids"
    )

    # Reason must be non-empty and mention the blocking state
    assert result.reason, "reason must be non-empty"
    assert "offrecord" in result.reason.lower() or "redact" in result.reason.lower(), (
        f"reason must mention offrecord/redact, got: {result.reason!r}"
    )


async def test_integration_tombstone_blocks_artifact(db_session) -> None:
    """Integration: a forget_events tombstone blocks a cited version → FORGOTTEN.

    Verifies the full DB path: real forget_events row with tombstone_key='message_hash:<hash>'
    correctly blocks the matching message_version.
    """
    from bot.services.visibility_derivation import CardVisibility, derive_card_visibility

    specific_hash = f"integ-tombstone-{uuid4().hex[:10]}"
    _, v_forgotten = await _create_version(db_session, content_hash=specific_hash)
    _, v_clean = await _create_version(db_session, memory_policy="normal")

    await _create_tombstone(db_session, content_hash=specific_hash)

    # v_forgotten alone → FORGOTTEN
    result_single = await derive_card_visibility(db_session, [v_forgotten])
    assert result_single.visibility == CardVisibility.FORGOTTEN, (
        f"Single forgotten version should yield FORGOTTEN, got {result_single.visibility}"
    )
    assert v_forgotten in result_single.blocking_source_ids

    # v_clean alone → VISIBLE
    result_clean = await derive_card_visibility(db_session, [v_clean])
    assert result_clean.visibility == CardVisibility.VISIBLE, (
        f"Clean version (no tombstone) should yield VISIBLE, got {result_clean.visibility}"
    )

    # Combined: FORGOTTEN wins over VISIBLE
    result_combined = await derive_card_visibility(db_session, [v_clean, v_forgotten])
    assert result_combined.visibility == CardVisibility.FORGOTTEN, (
        f"forgotten+visible combined should yield FORGOTTEN, got {result_combined.visibility}"
    )
