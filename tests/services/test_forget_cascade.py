"""T3-04 — cascade worker skeleton (issue #96).

Outer-tx isolation. Tests do NOT call ``session.commit()``.

The worker drives ``forget_events`` rows through the cascade defined in HANDOFF.md §10.
Phase 3 skeleton scope: only ``chat_messages`` and ``message_versions`` exist as Phase 1
tables; the remaining cascade layers (``message_entities``, ``message_links``,
``attachments``, ``fts_rows``) MUST be recorded as ``skipped`` so the cascade is forward-
compatible without doing any work that would require non-existent tables.

Critical durability invariants (from issue body):
  A. Idempotent claim: pending → processing must be atomic; double-claim impossible.
  B. Restart-safe: after crash mid-cascade, next run resumes from the last completed layer.
  C. Per-event isolation: a failure in one event's cascade must NOT halt other events.
  D. No cascade duplication: rerunning the worker over a completed event is a no-op.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=9_400_000_000)
_msg_counter = itertools.count(start=970_000)
_chat_counter = itertools.count(start=1)
_key_counter = itertools.count(start=1)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_key(prefix: str = "message") -> str:
    return f"{prefix}:test:{next(_key_counter)}"


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_chat_message_with_v1(
    db_session,
    *,
    text: str = "secret content",
    caption: str | None = None,
    raw_json: dict | None = None,
) -> tuple[int, int, int, int]:
    """Create a ChatMessage with a v1 MessageVersion. Returns (chat_message_id,
    message_version_id, chat_id, message_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = _next_chat_id()
    message_id = _next_msg_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=uid,
        text=text,
        date=when,
        caption=caption,
        raw_json=raw_json or {"text": text},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        caption=caption,
        normalized_text=text,
        entities_json={"entities": []},
        content_hash="h-test-v1",
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()

    return msg.id, v.id, chat_id, message_id


async def _make_pending_forget_event(
    db_session,
    *,
    target_type: str = "message",
    target_id: str | None = None,
    tombstone_key: str | None = None,
) -> int:
    """Create a pending forget_event row and return its id."""
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=tombstone_key or _next_key(target_type),
    )
    return ev.id


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #1: pending event progresses to completed; content nulled.
# ──────────────────────────────────────────────────────────────────────────────


async def test_pending_event_progresses_to_completed(db_session) -> None:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    cm_id, ver_id, chat_id, msg_id = await _make_chat_message_with_v1(
        db_session, text="erase me", caption="and this caption"
    )
    event_id = await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=f"message:{chat_id}:{msg_id}",
    )

    stats = await run_cascade_worker_once(db_session)

    assert stats["claimed"] == 1
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    ev = await ForgetEventRepo.get_by_tombstone_key(
        db_session, f"message:{chat_id}:{msg_id}"
    )
    assert ev is not None
    assert ev.status == "completed"
    assert ev.cascade_status is not None
    assert ev.cascade_status["chat_messages"]["status"] == "completed"
    assert ev.cascade_status["message_versions"]["status"] == "completed"

    cm = await db_session.get(ChatMessage, cm_id)
    assert cm.text is None
    assert cm.caption is None
    assert cm.raw_json is None
    assert cm.is_redacted is True
    assert cm.memory_policy == "forgotten"

    ver = await db_session.get(MessageVersion, ver_id)
    assert ver.text is None
    assert ver.caption is None
    assert ver.normalized_text is None
    assert ver.entities_json is None
    assert ver.is_redacted is True


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #2: partial-cascade restartable (durability invariant B)
# ──────────────────────────────────────────────────────────────────────────────


async def test_partial_cascade_restartable(db_session, monkeypatch) -> None:
    """Simulate a worker crash AFTER chat_messages succeeds but BEFORE
    message_versions completes. The next worker run must resume — skipping
    chat_messages (already done) and completing message_versions.

    Restart-safety is the whole point of the cascade_status checkpoint primitive.
    Without it, a crash mid-cascade would either lose progress (cascade redoes
    the chat_messages NULL) or stall (state stuck in 'processing' indefinitely).
    """
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade

    cm_id, ver_id, chat_id, msg_id = await _make_chat_message_with_v1(
        db_session, text="resumable", caption="caption"
    )
    tomb_key = f"message:{chat_id}:{msg_id}"
    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=tomb_key,
    )

    # First run: monkeypatch _cascade_message_versions to raise. This fires AFTER
    # chat_messages has already nulled content + been checkpointed.
    crash_count = {"n": 0}

    async def _crash_message_versions(session, event):
        crash_count["n"] += 1
        raise RuntimeError("simulated mid-cascade crash")

    monkeypatch.setitem(
        forget_cascade._LAYER_FUNCS, "message_versions", _crash_message_versions
    )

    stats1 = await forget_cascade.run_cascade_worker_once(db_session)
    assert stats1["claimed"] == 1
    assert stats1["failed"] == 1
    assert stats1["processed"] == 0
    assert crash_count["n"] == 1

    # After crash: chat_messages is already NULLed and checkpointed.
    cm_after_crash = await db_session.get(ChatMessage, cm_id)
    assert cm_after_crash.text is None
    assert cm_after_crash.is_redacted is True

    ev_after_crash = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev_after_crash.status == "failed"
    assert ev_after_crash.cascade_status["chat_messages"]["status"] == "completed"

    # Recovery scenario: an operator (or a future re-issuer) flips the row back
    # to pending so the worker can resume. We simulate this with a direct
    # status update (not exposed by the repo since failed→pending is not in the
    # state machine — the recovery path is intentionally ops-only and would be
    # gated by an admin action in production).
    from sqlalchemy import update as sa_update

    from bot.db.models import ForgetEvent

    await db_session.execute(
        sa_update(ForgetEvent)
        .where(ForgetEvent.id == ev_after_crash.id)
        .values(status="pending")
    )
    await db_session.flush()

    # Second run: undo the monkeypatch so message_versions completes.
    monkeypatch.setitem(
        forget_cascade._LAYER_FUNCS,
        "message_versions",
        forget_cascade._cascade_message_versions,
    )

    # Track that chat_messages cascade is NOT re-invoked: wrap the real func.
    chat_messages_calls = {"n": 0}
    original_chat_messages = forget_cascade._cascade_chat_messages

    async def _counting_chat_messages(session, event):
        chat_messages_calls["n"] += 1
        return await original_chat_messages(session, event)

    monkeypatch.setitem(
        forget_cascade._LAYER_FUNCS, "chat_messages", _counting_chat_messages
    )

    stats2 = await forget_cascade.run_cascade_worker_once(db_session)
    assert stats2["claimed"] == 1
    assert stats2["processed"] == 1
    assert stats2["failed"] == 0
    # Restart invariant: chat_messages cascade must NOT re-run for an already-
    # completed layer. Re-running would still be safe (the UPDATE is idempotent),
    # but skipping is the contract — Phase 4+ layers may have non-idempotent
    # work (vector deletions, FTS rebuilds) where re-running would matter.
    assert chat_messages_calls["n"] == 0

    ev_done = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev_done.status == "completed"
    assert ev_done.cascade_status["chat_messages"]["status"] == "completed"
    assert ev_done.cascade_status["message_versions"]["status"] == "completed"

    ver_done = await db_session.get(MessageVersion, ver_id)
    assert ver_done.text is None
    assert ver_done.is_redacted is True


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #3: idempotent rerun on already-completed event (invariant D)
# ──────────────────────────────────────────────────────────────────────────────


async def test_idempotent_rerun_already_completed_noops(db_session) -> None:
    """A completed forget_event must NOT be re-claimed or re-cascaded.

    ``list_pending`` filters by ``status='pending'``, so a completed row is
    invisible to the worker — confirming this is the cheapest "no-op" guarantee
    we can offer (no scan, no UPDATE attempt). Sprint 4+ may add
    re-import / re-trigger flows that DO touch completed rows; those will
    require their own tests.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    cm_id, _ver_id, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    tomb_key = f"message:{chat_id}:{msg_id}"
    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=tomb_key,
    )

    # First run: take the event to completed.
    stats1 = await run_cascade_worker_once(db_session)
    assert stats1["claimed"] == 1
    assert stats1["processed"] == 1

    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev.status == "completed"
    snapshot_cascade = ev.cascade_status
    snapshot_updated_at = ev.updated_at

    # Second run: nothing pending — worker must do nothing.
    stats2 = await run_cascade_worker_once(db_session)
    assert stats2 == {"claimed": 0, "processed": 0, "failed": 0}

    # Row must be byte-identical (same cascade_status, same updated_at).
    ev_again = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev_again.status == "completed"
    assert ev_again.cascade_status == snapshot_cascade
    assert ev_again.updated_at == snapshot_updated_at


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #4: no double-claim under concurrency (invariant A)
# ──────────────────────────────────────────────────────────────────────────────


async def test_concurrent_workers_no_double_claim(db_session) -> None:
    """Simulate two workers running back-to-back on the same DB session: the
    first claim must win, the second must skip the row silently.

    True asyncio concurrency in a single AsyncSession isn't possible (a session
    serializes its own statements). We model concurrency at the API surface:
    after the first ``mark_status('processing')`` succeeds, calling it again
    raises ``ValueError`` — exactly what the worker's claim path catches and
    treats as "another worker got there first".
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    cm_id, _ver_id, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    tomb_key = f"message:{chat_id}:{msg_id}"
    event_id = await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=tomb_key,
    )

    # Worker A claims first via the same atomic UPDATE the worker uses.
    claimed = await ForgetEventRepo.mark_status(
        db_session, event_id, status="processing"
    )
    assert claimed.status == "processing"

    # Worker B comes along: it sees no pending rows (the row is already in
    # 'processing'), so its run is a no-op.
    stats_b = await run_cascade_worker_once(db_session)
    assert stats_b == {"claimed": 0, "processed": 0, "failed": 0}

    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev.status == "processing"  # still A's claim, not double-progressed


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #5: per-event isolation (invariant C)
# ──────────────────────────────────────────────────────────────────────────────


async def test_per_event_isolation_failure_doesnt_halt_batch(
    db_session, monkeypatch
) -> None:
    """Three pending events in one batch. The middle one's cascade is rigged to
    fail. The other two must complete normally — the worker MUST NOT abort the
    batch on a single failure.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade

    # Event A — completes normally.
    cm_a, _, chat_a, msg_a = await _make_chat_message_with_v1(db_session, text="a")
    tomb_a = f"message:{chat_a}:{msg_a}"
    await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_a), tombstone_key=tomb_a
    )

    # Event B — rigged to fail (specific cm_id matched in monkeypatch below).
    cm_b, _, chat_b, msg_b = await _make_chat_message_with_v1(db_session, text="b")
    tomb_b = f"message:{chat_b}:{msg_b}"
    await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_b), tombstone_key=tomb_b
    )

    # Event C — completes normally.
    cm_c, _, chat_c, msg_c = await _make_chat_message_with_v1(db_session, text="c")
    tomb_c = f"message:{chat_c}:{msg_c}"
    await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_c), tombstone_key=tomb_c
    )

    # Patch chat_messages cascade to raise specifically when called for event B.
    original = forget_cascade._cascade_chat_messages

    async def _selective_fail(session, event):
        if event.target_id == str(cm_b):
            raise RuntimeError("boom on event B")
        return await original(session, event)

    monkeypatch.setitem(
        forget_cascade._LAYER_FUNCS, "chat_messages", _selective_fail
    )

    stats = await forget_cascade.run_cascade_worker_once(db_session)
    assert stats["claimed"] == 3
    assert stats["processed"] == 2
    assert stats["failed"] == 1

    ev_a = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_a)
    ev_b = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_b)
    ev_c = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_c)

    assert ev_a.status == "completed"
    assert ev_b.status == "failed"
    assert ev_b.cascade_status is not None
    assert "error" in ev_b.cascade_status
    assert ev_c.status == "completed"


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #6: skipped layers recorded for forward-compatibility
# ──────────────────────────────────────────────────────────────────────────────


async def test_skipped_layers_recorded_in_cascade_status(db_session) -> None:
    """The cascade order in HANDOFF.md §10 includes layers whose tables don't
    yet exist in this codebase (Phase 4+ derived layers). The worker MUST
    record them as ``{"status": "skipped", "reason": "table_not_exists"}``
    so the cascade is forward-compatible: a later phase that adds the table
    replaces the per-layer function and re-running the cascade picks up
    where it left off.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import (
        CASCADE_LAYER_ORDER,
        run_cascade_worker_once,
    )

    cm_id, _, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    tomb_key = f"message:{chat_id}:{msg_id}"
    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=tomb_key,
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["processed"] == 1

    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev.status == "completed"

    # Phase 1 layers: completed.
    assert ev.cascade_status["chat_messages"]["status"] == "completed"
    assert ev.cascade_status["message_versions"]["status"] == "completed"

    # Phase 4+ layers: skipped with the canonical reason.
    for layer in ("message_entities", "message_links", "attachments", "fts_rows"):
        assert ev.cascade_status[layer] == {
            "status": "skipped",
            "reason": "table_not_exists",
        }, f"Layer {layer} not recorded as skipped"

    # All layers from CASCADE_LAYER_ORDER are present in cascade_status — none
    # silently dropped.
    for layer in CASCADE_LAYER_ORDER:
        assert layer in ev.cascade_status


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #7: scheduler tick is gated by feature flag (production safety)
# ──────────────────────────────────────────────────────────────────────────────


async def test_scheduler_tick_no_op_when_flag_off(db_session) -> None:
    """``cascade_worker_tick`` is the scheduler entry point. When the feature
    flag ``memory.forget.cascade_worker.enabled`` is OFF (default), the tick
    must NOT process any events — it is a strict no-op.

    This mirrors the AUTHORIZED_SCOPE pattern for new ingestion-style paths
    (cf. ``memory.ingestion.raw_updates.enabled``): code lands first, the flag
    stays OFF in production until the implementation is verified.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import cascade_worker_tick

    cm_id, _, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    tomb_key = f"message:{chat_id}:{msg_id}"
    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=tomb_key,
    )
    # Flag is not set — defaults to False per FeatureFlagRepo.get contract.

    # Tick uses its own session via async_session() — pass our test session
    # explicitly so the outer-tx isolation is preserved (no real commit).
    await cascade_worker_tick(session=db_session)

    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev.status == "pending"  # untouched
    assert ev.cascade_status is None


async def test_scheduler_tick_processes_events_when_flag_on(db_session) -> None:
    """When ``memory.forget.cascade_worker.enabled`` is ON, the tick claims
    and processes pending events exactly like ``run_cascade_worker_once``."""
    from bot.db.repos.feature_flag import FeatureFlagRepo
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import CASCADE_WORKER_FLAG, cascade_worker_tick

    await FeatureFlagRepo.set_enabled(db_session, CASCADE_WORKER_FLAG, enabled=True)

    cm_id, _, chat_id, msg_id = await _make_chat_message_with_v1(db_session)
    tomb_key = f"message:{chat_id}:{msg_id}"
    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=tomb_key,
    )

    await cascade_worker_tick(session=db_session)

    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev.status == "completed"
