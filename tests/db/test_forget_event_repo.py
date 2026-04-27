"""T3-01 acceptance tests — forget_events table + ForgetEventRepo.

Outer-tx isolation. Tests do NOT call ``session.commit()``.

Coverage:
- insert happy path for all four target_type variants
- idempotent tombstone_key (re-create returns existing row, no duplicate)
- status transitions: valid pending→processing→completed; pending→processing→failed
- reject invalid transition: completed→processing raises ValueError
- list_pending ordering + limit
- cascade_status JSON round-trip
- model registered in metadata smoke (no DB needed)
"""

from __future__ import annotations

import itertools

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=9_300_000_000)
_key_counter = itertools.count(start=1)


def _next_user_id() -> int:
    return next(_user_counter)


def _unique_key(prefix: str = "message") -> str:
    n = next(_key_counter)
    return f"{prefix}:test:{n}"


async def _make_user(db_session) -> int:
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


# ──────────────────────────────────────────────────────────────────────────────
# Happy-path inserts — all four target_type variants
# ──────────────────────────────────────────────────────────────────────────────


async def test_create_message_target_type(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    actor = await _make_user(db_session)
    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id="chat:42:msg:100",
        actor_user_id=actor,
        authorized_by="admin",
        tombstone_key=f"message:-42:{next(_key_counter)}",
        reason="test",
        policy="forgotten",
    )

    assert ev.id is not None
    assert ev.target_type == "message"
    assert ev.authorized_by == "admin"
    assert ev.policy == "forgotten"
    assert ev.status == "pending"
    assert ev.cascade_status is None


async def test_create_message_hash_target_type(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message_hash",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message_hash:{'a' * 64}_{next(_key_counter)}",
    )

    assert ev.id is not None
    assert ev.target_type == "message_hash"
    assert ev.target_id is None
    assert ev.actor_user_id is None


async def test_create_user_target_type(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    uid = await _make_user(db_session)
    ev = await ForgetEventRepo.create(
        db_session,
        target_type="user",
        target_id=str(uid),
        actor_user_id=uid,
        authorized_by="self",
        tombstone_key=f"user:{uid}_{next(_key_counter)}",
        reason="forget me",
    )

    assert ev.target_type == "user"
    assert ev.target_id == str(uid)
    assert ev.reason == "forget me"


async def test_create_export_target_type(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="export",
        target_id="src:export_9999",
        actor_user_id=None,
        authorized_by="gdpr_request",
        tombstone_key=f"export:desktop:{next(_key_counter)}",
    )

    assert ev.target_type == "export"
    assert ev.authorized_by == "gdpr_request"
    assert ev.status == "pending"


# ──────────────────────────────────────────────────────────────────────────────
# Idempotency: re-create with same tombstone_key returns existing row
# ──────────────────────────────────────────────────────────────────────────────


async def test_create_idempotent_on_tombstone_key(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    key = f"message:-100:{next(_key_counter)}"
    actor = await _make_user(db_session)

    first = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id="x",
        actor_user_id=actor,
        authorized_by="admin",
        tombstone_key=key,
    )
    second = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id="y",  # different, but key is the same
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=key,
    )

    assert first.id == second.id
    assert second.target_id == "x"  # original value preserved


# ──────────────────────────────────────────────────────────────────────────────
# get_by_tombstone_key
# ──────────────────────────────────────────────────────────────────────────────


async def test_get_by_tombstone_key_returns_none_for_missing(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    result = await ForgetEventRepo.get_by_tombstone_key(db_session, "message:missing:0")
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Status transitions
# ──────────────────────────────────────────────────────────────────────────────


async def test_valid_transition_pending_to_processing_to_completed(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    actor = await _make_user(db_session)
    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=actor,
        authorized_by="admin",
        tombstone_key=f"message:-1:{next(_key_counter)}",
    )
    assert ev.status == "pending"

    processing = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="processing"
    )
    assert processing.status == "processing"
    assert processing.id == ev.id

    completed = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="completed"
    )
    assert completed.status == "completed"


async def test_valid_transition_pending_to_processing_to_failed(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    actor = await _make_user(db_session)
    ev = await ForgetEventRepo.create(
        db_session,
        target_type="user",
        target_id="99",
        actor_user_id=actor,
        authorized_by="self",
        tombstone_key=f"user:99_{next(_key_counter)}",
    )

    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")
    failed = await ForgetEventRepo.mark_status(db_session, ev.id, status="failed")
    assert failed.status == "failed"


async def test_invalid_transition_completed_to_processing_raises(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message_hash",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message_hash:bad_trans_{next(_key_counter)}",
    )

    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")
    await ForgetEventRepo.mark_status(db_session, ev.id, status="completed")

    with pytest.raises(ValueError, match="completed"):
        await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")


async def test_invalid_transition_pending_to_completed_raises(db_session) -> None:
    """Skipping processing is not allowed."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="export",
        target_id="src:1",
        actor_user_id=None,
        authorized_by="gdpr_request",
        tombstone_key=f"export:src:{next(_key_counter)}",
    )

    with pytest.raises(ValueError, match="pending"):
        await ForgetEventRepo.mark_status(db_session, ev.id, status="completed")


# ──────────────────────────────────────────────────────────────────────────────
# list_pending — ordering + limit
# ──────────────────────────────────────────────────────────────────────────────


async def test_list_pending_returns_oldest_first_and_respects_limit(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    # Insert 4 rows. By inserting in forward order, IDs are ascending naturally.
    # We then insert a 5th row with a key that sorts BEFORE the first 4 alphabetically
    # but gets a HIGHER id — this exercises the id ASC tie-breaker when created_at
    # timestamps are equal (common in fast sequential inserts within a transaction).
    keys = [f"message:-100:{next(_key_counter)}" for _ in range(4)]
    created_ids = []
    for key in keys:
        ev = await ForgetEventRepo.create(
            db_session,
            target_type="message",
            target_id=None,
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=key,
        )
        created_ids.append(ev.id)

    # Advance the first one to processing (no longer pending).
    await ForgetEventRepo.mark_status(db_session, created_ids[0], status="processing")

    pending = await ForgetEventRepo.list_pending(db_session, limit=2)
    pending_ids = [ev.id for ev in pending]

    # First item should not be in the result (it's processing now).
    assert created_ids[0] not in pending_ids
    # Limit of 2 is respected.
    assert len(pending_ids) == 2
    # Results are ordered by (created_at ASC, id ASC): ids should be ascending.
    assert pending_ids[0] < pending_ids[1]
    # The two oldest remaining pending rows are created_ids[1] and created_ids[2].
    assert pending_ids == [created_ids[1], created_ids[2]]


# ──────────────────────────────────────────────────────────────────────────────
# cascade_status JSON round-trip
# ──────────────────────────────────────────────────────────────────────────────


async def test_cascade_status_json_round_trip(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"message:-9:{next(_key_counter)}",
    )

    # Use a realistic per-layer nested dict to validate JSONB nested-dict round-trip.
    cascade_payload = {
        "chat_messages": {"status": "completed", "rows_affected": 1, "completed_at": "2026-04-27T12:00:00Z"},
        "message_versions": {"status": "pending", "rows_affected": 0},
        "offrecord_marks": {"status": "completed", "rows_affected": 1},
    }

    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")
    updated = await ForgetEventRepo.mark_status(
        db_session,
        ev.id,
        status="completed",
        cascade_status=cascade_payload,
    )

    assert updated.cascade_status == cascade_payload

    # Re-fetch to verify postgres stored it correctly.
    fetched = await ForgetEventRepo.get_by_tombstone_key(
        db_session, ev.tombstone_key
    )
    assert fetched is not None
    assert fetched.cascade_status == cascade_payload


# ──────────────────────────────────────────────────────────────────────────────
# Metadata smoke (no DB needed — runs with app_env only)
# ──────────────────────────────────────────────────────────────────────────────


def test_forget_event_model_registered_in_metadata(app_env) -> None:
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert hasattr(models, "ForgetEvent")
    assert "forget_events" in models.Base.metadata.tables

    table = models.Base.metadata.tables["forget_events"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "target_type",
        "target_id",
        "actor_user_id",
        "authorized_by",
        "tombstone_key",
        "reason",
        "policy",
        "status",
        "cascade_status",
        "created_at",
        "updated_at",
    } == cols

    constraint_names = {c.name for c in table.constraints if c.name}
    assert "ck_forget_events_target_type" in constraint_names
    assert "ck_forget_events_authorized_by" in constraint_names
    assert "ck_forget_events_policy" in constraint_names
    assert "ck_forget_events_status" in constraint_names
    assert "uq_forget_events_tombstone_key" in constraint_names

    fk_names = {fk.name for fk in table.foreign_keys if fk.name}
    assert "fk_forget_events_actor_user_id" in fk_names

    # Fix 3: cascade_status must render as JSONB on postgres dialect.
    from sqlalchemy.dialects import postgresql as pg_dialect

    cascade_col = table.c["cascade_status"]
    compiled_type = cascade_col.type.compile(dialect=pg_dialect.dialect())
    assert compiled_type == "JSONB", (
        f"cascade_status should compile to JSONB on postgres, got {compiled_type!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Terminal state lockout
# ──────────────────────────────────────────────────────────────────────────────


async def test_failed_state_is_terminal(db_session) -> None:
    """Verify that failed is a terminal state: no transition out of it is allowed."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:-terminal:{next(_key_counter)}",
    )

    # Transition pending → processing → failed.
    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")
    failed = await ForgetEventRepo.mark_status(db_session, ev.id, status="failed")
    assert failed.status == "failed"

    # Attempt to re-enter processing from failed must raise ValueError.
    with pytest.raises(ValueError, match="failed"):
        await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")


# ──────────────────────────────────────────────────────────────────────────────
# update_cascade_status — cascade checkpoint primitive (Sprint 3 / #96)
# ──────────────────────────────────────────────────────────────────────────────
#
# update_cascade_status is the cascade worker's checkpoint primitive. It writes a
# new ``cascade_status`` value WHILE the row stays in ``status='processing'``. This
# is intentionally separate from ``mark_status``: the cascade worker may need to
# checkpoint per-layer progress mid-cascade WITHOUT transitioning state, which
# ``mark_status`` would refuse (``processing → processing`` is not a valid state-
# machine edge). Without this separation, mid-cascade restart-safety is impossible.


async def test_update_cascade_status_succeeds_in_processing(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:-checkpoint:{next(_key_counter)}",
    )
    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")

    payload = {"chat_messages": {"status": "completed", "rows": 1}}
    updated = await ForgetEventRepo.update_cascade_status(
        db_session, ev.id, cascade_status=payload
    )

    assert updated.id == ev.id
    assert updated.status == "processing"
    assert updated.cascade_status == payload


async def test_update_cascade_status_rejected_in_pending(db_session) -> None:
    """Cannot checkpoint a row that hasn't been claimed yet."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:-pending-cp:{next(_key_counter)}",
    )
    assert ev.status == "pending"

    with pytest.raises(ValueError, match="pending"):
        await ForgetEventRepo.update_cascade_status(
            db_session, ev.id, cascade_status={"x": 1}
        )


async def test_update_cascade_status_rejected_in_completed(db_session) -> None:
    """Cannot rewrite cascade_status on a finished row."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:-done-cp:{next(_key_counter)}",
    )
    await ForgetEventRepo.mark_status(db_session, ev.id, status="processing")
    await ForgetEventRepo.mark_status(db_session, ev.id, status="completed")

    with pytest.raises(ValueError, match="completed"):
        await ForgetEventRepo.update_cascade_status(
            db_session, ev.id, cascade_status={"x": 1}
        )


async def test_update_cascade_status_rejected_on_missing_id(db_session) -> None:
    """A non-existent id must raise (not silently no-op)."""
    from bot.db.repos.forget_event import ForgetEventRepo

    with pytest.raises(ValueError, match="not found"):
        await ForgetEventRepo.update_cascade_status(
            db_session, 999_999_999, cascade_status={"x": 1}
        )
