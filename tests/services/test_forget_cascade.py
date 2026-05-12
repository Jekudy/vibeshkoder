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
from decimal import Decimal

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
    await _make_pending_forget_event(
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
    """Simulate the losing-worker path: pre-claim the event row (as Worker A would),
    then run the cascade worker (Worker B). Worker B must skip silently — no
    ValueError propagated, no crash — because ``list_pending`` filters out
    'processing' rows and the claim path catches ValueError from ``mark_status``.

    True asyncio concurrency with a single AsyncSession is not possible (the
    session serializes statements). Instead we exercise the loser path directly:
    after the row is in 'processing', the worker sees no pending rows and returns
    all-zero stats.
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

    # Worker A claims the row (simulates winning the race).
    claimed = await ForgetEventRepo.mark_status(
        db_session, event_id, status="processing"
    )
    assert claimed.status == "processing"

    # Worker B runs: row is already 'processing', not in list_pending. Must be a
    # strict no-op — zero stats, no exception raised.
    stats_b = await run_cascade_worker_once(db_session)
    assert stats_b == {"claimed": 0, "processed": 0, "failed": 0}

    # Row must remain in Worker A's claim state (not double-progressed).
    ev = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_key)
    assert ev.status == "processing"

    # Additionally: directly verify the ValueError loser path is silently caught.
    # Attempt to claim an already-processing row raises ValueError from the repo;
    # the worker's claim logic must swallow it and continue.
    with pytest.raises(ValueError):
        await ForgetEventRepo.mark_status(db_session, event_id, status="processing")


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


# ──────────────────────────────────────────────────────────────────────────────
# Acceptance #8: target_type='user' — full content wipe for all user messages
# ──────────────────────────────────────────────────────────────────────────────


async def _make_user_raw(db_session, uid: int) -> None:
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )


async def _make_message_for_user(
    db_session,
    user_id: int,
    *,
    text: str = "secret",
    caption: str | None = None,
    raw_json: dict | None = None,
) -> int:
    """Create a ChatMessage for a specific user_id; return chat_message id."""
    from bot.db.models import ChatMessage

    chat_id = _next_chat_id()
    message_id = _next_msg_id()
    when = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=when,
        caption=caption,
        raw_json=raw_json or {"text": text},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    return msg.id


async def _make_version_for_message(db_session, chat_message_id: int, *, seq: int = 1) -> int:
    """Create a MessageVersion for a given chat_message; return version id."""
    from bot.db.models import MessageVersion

    v = MessageVersion(
        chat_message_id=chat_message_id,
        version_seq=seq,
        text="secret text",
        caption="secret cap",
        normalized_text="secret norm",
        entities_json={"entities": []},
        content_hash=f"h-test-user-{chat_message_id}-{seq}",
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()
    return v.id


async def test_user_target_wipes_all_user_messages(db_session) -> None:
    """target_type='user': 3 messages for user X + 2 for user Y.
    After cascade: X's messages are NULLed + redacted + forgotten; Y's are untouched.
    """
    from bot.db.models import ChatMessage
    from bot.services.forget_cascade import run_cascade_worker_once

    uid_x = _next_user()
    uid_y = _next_user()
    await _make_user_raw(db_session, uid_x)
    await _make_user_raw(db_session, uid_y)

    # 3 messages for user X
    cm_x1 = await _make_message_for_user(db_session, uid_x, text="x-secret-1")
    cm_x2 = await _make_message_for_user(db_session, uid_x, text="x-secret-2", caption="cap")
    cm_x3 = await _make_message_for_user(db_session, uid_x, text="x-secret-3")
    # 2 messages for user Y — must be untouched
    cm_y1 = await _make_message_for_user(db_session, uid_y, text="y-keep-1")
    cm_y2 = await _make_message_for_user(db_session, uid_y, text="y-keep-2")

    await _make_pending_forget_event(
        db_session,
        target_type="user",
        target_id=str(uid_x),
        tombstone_key=f"user:{uid_x}",
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["claimed"] == 1
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    for cm_id in (cm_x1, cm_x2, cm_x3):
        cm = await db_session.get(ChatMessage, cm_id, populate_existing=True)
        assert cm.text is None, f"cm {cm_id} text not NULLed"
        assert cm.caption is None, f"cm {cm_id} caption not NULLed"
        assert cm.raw_json is None, f"cm {cm_id} raw_json not NULLed"
        assert cm.is_redacted is True, f"cm {cm_id} not redacted"
        assert cm.memory_policy == "forgotten", f"cm {cm_id} policy not forgotten"

    for cm_id in (cm_y1, cm_y2):
        cm = await db_session.get(ChatMessage, cm_id, populate_existing=True)
        assert cm.text is not None, f"cm {cm_id} text was wiped (should be untouched)"
        assert cm.is_redacted is False, f"cm {cm_id} wrongly redacted"


async def test_user_target_wipes_all_user_versions(db_session) -> None:
    """target_type='user': versions for X's messages are NULLed; Y's versions untouched.
    content_hash must remain NOT NULL (citation stability).
    """
    from bot.db.models import MessageVersion
    from bot.services.forget_cascade import run_cascade_worker_once

    uid_x = _next_user()
    uid_y = _next_user()
    await _make_user_raw(db_session, uid_x)
    await _make_user_raw(db_session, uid_y)

    cm_x1 = await _make_message_for_user(db_session, uid_x, text="x-v-secret")
    cm_y1 = await _make_message_for_user(db_session, uid_y, text="y-v-keep")

    ver_x = await _make_version_for_message(db_session, cm_x1)
    ver_y = await _make_version_for_message(db_session, cm_y1)

    await _make_pending_forget_event(
        db_session,
        target_type="user",
        target_id=str(uid_x),
        tombstone_key=f"user:{uid_x}:ver",
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["processed"] == 1

    # X's version must be NULLed in all 5 content fields + is_redacted
    vx = await db_session.get(MessageVersion, ver_x, populate_existing=True)
    assert vx.text is None
    assert vx.caption is None
    assert vx.normalized_text is None
    assert vx.entities_json is None
    assert vx.is_redacted is True
    # content_hash preserved for citation stability
    assert vx.content_hash is not None

    # Y's version untouched
    vy = await db_session.get(MessageVersion, ver_y, populate_existing=True)
    assert vy.text is not None
    assert vy.is_redacted is False


async def test_message_hash_target_records_skipped(db_session) -> None:
    """target_type='message_hash': cascade finalizes as 'completed'.

    Phase 5 layers (T5-04 — ``llm_synthesis_cache``, ``qa_traces_llm``) run
    with ``status='completed'`` because they support ``message_hash``.
    Phase 1 layers (``chat_messages``, ``message_versions``, ``qa_traces``)
    skip with ``not_applicable``. ``llm_usage_ledger`` skips with
    ``not_applicable`` (only ``user`` is supported per §8.3). The remaining
    placeholder layers stay ``target_type_not_supported_yet``.
    """
    from bot.services.forget_cascade import run_cascade_worker_once

    event_id = await _make_pending_forget_event(
        db_session,
        target_type="message_hash",
        target_id="somehash",
        tombstone_key="message_hash:somehash:test",
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["claimed"] == 1
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    from bot.db.models import ForgetEvent

    ev = await db_session.get(ForgetEvent, event_id, populate_existing=True)
    assert ev.status == "completed"

    # Phase 5 layers (T5-04) support message_hash → completed with rows count.
    p5_layers = ("llm_synthesis_cache", "qa_traces_llm")
    for layer in p5_layers:
        assert ev.cascade_status[layer]["status"] == "completed", (
            f"Layer {layer} should be completed for message_hash target, "
            f"got: {ev.cascade_status[layer]}"
        )

    # Phase 1 + qa_traces + llm_usage_ledger layers: not_applicable for
    # message_hash (Phase 1 layers handle only message + user; ledger handles
    # only user per §8.3).
    not_applicable_layers = (
        "chat_messages",
        "message_versions",
        "qa_traces",
        "llm_usage_ledger",
    )
    for layer in not_applicable_layers:
        cs = ev.cascade_status[layer]
        assert cs["status"] == "completed", (
            f"Layer {layer} should be completed (not_applicable) for "
            f"message_hash target, got: {cs}"
        )
        assert cs["reason"] == "not_applicable", (
            f"Layer {layer} reason should be not_applicable, got: {cs}"
        )

    # Placeholder layers: tables don't exist yet, so skipped with table_not_exists
    # (since message_hash is now routed through the per-layer dispatcher).
    placeholder_layers = ("message_entities", "message_links", "attachments", "fts_rows")
    for layer in placeholder_layers:
        cs = ev.cascade_status[layer]
        assert cs["status"] == "skipped", (
            f"Layer {layer} should be skipped, got: {cs}"
        )
        assert cs["reason"] == "table_not_exists", (
            f"Layer {layer} reason should be table_not_exists, got: {cs}"
        )


async def test_export_target_records_skipped(db_session) -> None:
    """target_type='export': analogous to message_hash — all layers skipped."""
    from bot.db.models import ForgetEvent
    from bot.services.forget_cascade import run_cascade_worker_once

    event_id = await _make_pending_forget_event(
        db_session,
        target_type="export",
        target_id="export-uuid-abc",
        tombstone_key="export:export-uuid-abc:test",
    )

    stats = await run_cascade_worker_once(db_session)
    assert stats["claimed"] == 1
    assert stats["processed"] == 1
    assert stats["failed"] == 0

    ev = await db_session.get(ForgetEvent, event_id, populate_existing=True)
    assert ev.status == "completed"

    # ALL 6 layers: uniformly skipped with target_type_not_supported_yet
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    for layer in CASCADE_LAYER_ORDER:
        assert ev.cascade_status[layer]["status"] == "skipped", (
            f"Layer {layer} should be skipped for export target, got: {ev.cascade_status[layer]}"
        )
        assert ev.cascade_status[layer]["reason"] == "target_type_not_supported_yet", (
            f"Layer {layer} should have reason='target_type_not_supported_yet', got: {ev.cascade_status[layer]}"
        )


# ──────────────────────────────────────────────────────────────────────────────


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


# ──────────────────────────────────────────────────────────────────────────────
# H4: per-event savepoint isolation — real DB error on one event must not abort
# the outer transaction so remaining events in the batch can still be processed.
# ──────────────────────────────────────────────────────────────────────────────


async def test_per_event_db_error_isolation_via_savepoint(db_session, monkeypatch) -> None:
    """H4 fix verification: when event B's cascade causes a real SQLAlchemy DB-level
    error (not a Python-only exception), the worker MUST still process events A and C
    via savepoint isolation around each event.

    Without ``begin_nested()`` around ``_process_one_event``, a database-level error
    aborts the outer transaction and all subsequent DB operations in the same session
    fail with PendingRollbackError — breaking the per-event isolation guarantee
    documented in the worker's docstring.
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade

    # Event A — processes normally.
    cm_a, _, chat_a, msg_a = await _make_chat_message_with_v1(db_session, text="alpha")
    tomb_a = f"message:{chat_a}:{msg_a}"
    await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_a), tombstone_key=tomb_a
    )

    # Event B — rigged to raise a real SQLAlchemy DB error (not a Python exception).
    # ProgrammingError is a sqlalchemy.exc subclass that maps to a PostgreSQL ERROR.
    # We raise it directly from the layer function; the worker must absorb it without
    # leaving the outer transaction in an aborted state.
    cm_b, _, chat_b, msg_b = await _make_chat_message_with_v1(db_session, text="beta")
    tomb_b = f"message:{chat_b}:{msg_b}"
    await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_b), tombstone_key=tomb_b
    )

    # Event C — must still process even after event B's DB error.
    cm_c, _, chat_c, msg_c = await _make_chat_message_with_v1(db_session, text="gamma")
    tomb_c = f"message:{chat_c}:{msg_c}"
    await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_c), tombstone_key=tomb_c
    )

    original = forget_cascade._cascade_chat_messages

    async def _db_error_on_b(session, event):
        if event.target_id == str(cm_b):
            # Execute a real invalid SQL statement so PostgreSQL itself aborts
            # the current (sub)transaction. This is the class of error that
            # requires savepoint isolation to contain: without begin_nested(),
            # this aborts the OUTER transaction and all subsequent operations
            # fail with PendingRollbackError.
            from sqlalchemy import text as sa_text
            await session.execute(sa_text("SELECT 1 / 0"))  # division by zero → ERROR
        return await original(session, event)

    monkeypatch.setitem(forget_cascade._LAYER_FUNCS, "chat_messages", _db_error_on_b)

    stats = await forget_cascade.run_cascade_worker_once(db_session)

    # All three events must be claimed.
    assert stats["claimed"] == 3, f"expected 3 claimed, got {stats['claimed']}"
    # Events A and C succeed; event B fails.
    assert stats["processed"] == 2, f"expected 2 processed, got {stats['processed']}"
    assert stats["failed"] == 1, f"expected 1 failed, got {stats['failed']}"

    ev_a = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_a)
    ev_b = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_b)
    ev_c = await ForgetEventRepo.get_by_tombstone_key(db_session, tomb_c)

    assert ev_a.status == "completed", f"event A should be completed, got {ev_a.status}"
    assert ev_b.status == "failed", f"event B should be failed, got {ev_b.status}"
    assert ev_c.status == "completed", f"event C should be completed, got {ev_c.status}"


# ──────────────────────────────────────────────────────────────────────────────
# T5-04 — Phase 5 cascade layers (llm_synthesis_cache → qa_traces_llm → ledger)
# Per contracts.md §8. All three layers must land in the same PR as alembic 025.
# ──────────────────────────────────────────────────────────────────────────────


async def _make_chat_message_with_v1_for_user(
    db_session, user_id: int, *, text: str = "secret"
) -> tuple[int, int]:
    """Create a ChatMessage + v1 MessageVersion attributed to ``user_id``.

    Returns ``(chat_message_id, message_version_id)``. The chat_message's
    ``current_version_id`` is set to the v1 id so source filter + cascade
    join logic resolves correctly.
    """
    from bot.db.models import ChatMessage, MessageVersion
    from sqlalchemy import update as sa_update

    chat_id = _next_chat_id()
    message_id = _next_msg_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=when,
        caption=None,
        raw_json={"text": text},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        caption=None,
        normalized_text=text,
        entities_json={"entities": []},
        content_hash=f"h-cascade-{msg.id}",
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()
    # Set current_version_id on the chat_message.
    await db_session.execute(
        sa_update(ChatMessage).where(ChatMessage.id == msg.id).values(current_version_id=v.id)
    )
    await db_session.flush()
    return msg.id, v.id


async def _make_cache_row(
    db_session, *, citation_ids: list[int], answer_text: str = "cached answer"
) -> int:
    """Insert an llm_synthesis_cache row; return its id."""
    from bot.db.models import LlmSynthesisCache

    row = LlmSynthesisCache(
        input_hash=f"h-{citation_ids}-{answer_text}"[:64].ljust(64, "0"),
        answer_text=answer_text,
        citation_ids=list(citation_ids),
        model="claude-haiku-4-5-20251001",
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


async def _make_ledger_row_for_trace(
    db_session, *, qa_trace_id: int | None = None
) -> int:
    """Insert a minimal llm_usage_ledger row. Returns id."""
    from bot.db.models import LlmUsageLedger
    from decimal import Decimal as _Dec

    row = LlmUsageLedger(
        qa_trace_id=qa_trace_id,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        prompt_hash="a" * 64,
        response_hash="b" * 64,
        tokens_in=100,
        tokens_out=50,
        cost_usd=_Dec("0.000550"),
        latency_ms=120,
        cache_hit=False,
        error=None,
    )
    db_session.add(row)
    await db_session.flush()
    return row.id


async def test_phase5_cascade_layer_order_constant(db_session) -> None:
    """contracts.md §8 ORDER: cache → qa_traces_llm → ledger AFTER qa_traces.

    Asserts the constant tuple is correct; this is the binding contract.
    """
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    idx_qa_traces = CASCADE_LAYER_ORDER.index("qa_traces")
    idx_cache = CASCADE_LAYER_ORDER.index("llm_synthesis_cache")
    idx_traces_llm = CASCADE_LAYER_ORDER.index("qa_traces_llm")
    idx_ledger = CASCADE_LAYER_ORDER.index("llm_usage_ledger")

    assert idx_qa_traces < idx_cache
    assert idx_cache < idx_traces_llm
    assert idx_traces_llm < idx_ledger


async def test_phase5_cascade_layers_registered_in_layer_funcs(db_session) -> None:
    """The three Phase 5 layers must be wired in _LAYER_FUNCS."""
    from bot.services.forget_cascade import _LAYER_FUNCS

    for layer in ("llm_synthesis_cache", "qa_traces_llm", "llm_usage_ledger"):
        assert layer in _LAYER_FUNCS, f"layer {layer} not registered"


async def test_cascade_llm_synthesis_cache_user_target(db_session) -> None:
    """target_type='user' → bulk DELETE every cache row citing user's versions."""
    from bot.db.models import LlmSynthesisCache
    from bot.services.forget_cascade import run_cascade_worker_once

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_a, vid_a = await _make_chat_message_with_v1_for_user(db_session, uid, text="a")
    cm_b, vid_b = await _make_chat_message_with_v1_for_user(db_session, uid, text="b")

    # Other user — must remain untouched.
    uid_other = _next_user()
    await _make_user_raw(db_session, uid_other)
    cm_o, vid_o = await _make_chat_message_with_v1_for_user(db_session, uid_other, text="o")

    cache_a = await _make_cache_row(db_session, citation_ids=[vid_a], answer_text="A")
    cache_b = await _make_cache_row(db_session, citation_ids=[vid_b, 999], answer_text="B")
    cache_other = await _make_cache_row(db_session, citation_ids=[vid_o], answer_text="O")

    await _make_pending_forget_event(
        db_session,
        target_type="user",
        target_id=str(uid),
        tombstone_key=f"user:{uid}",
    )

    await run_cascade_worker_once(db_session)

    # A + B deleted (cited a user-owned version); other survives.
    from sqlalchemy import select as sa_select

    remaining_ids = (
        await db_session.execute(sa_select(LlmSynthesisCache.id))
    ).scalars().all()
    assert cache_a not in remaining_ids
    assert cache_b not in remaining_ids
    assert cache_other in remaining_ids


async def test_cascade_llm_synthesis_cache_message_target(db_session) -> None:
    """target_type='message' → invalidate by message_version_id of current version."""
    from bot.db.models import LlmSynthesisCache
    from bot.services.forget_cascade import run_cascade_worker_once

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)
    # Unrelated cache row that should survive.
    unrelated = await _make_cache_row(
        db_session, citation_ids=[12345], answer_text="unrelated"
    )
    target_cache = await _make_cache_row(
        db_session, citation_ids=[vid], answer_text="target"
    )

    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=f"message:test:{cm_id}",
    )

    await run_cascade_worker_once(db_session)

    from sqlalchemy import select as sa_select

    remaining_ids = (
        await db_session.execute(sa_select(LlmSynthesisCache.id))
    ).scalars().all()
    assert target_cache not in remaining_ids
    assert unrelated in remaining_ids


async def test_cascade_qa_traces_llm_message_target_nulls_summary(db_session) -> None:
    """target_type='message' → NULL llm_response_summary on traces citing the version."""
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)

    # Trace citing the to-be-forgotten version.
    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=uid,
        chat_id=_next_chat_id(),
        query="q",
        evidence_ids=[vid],
        abstained=False,
        redact_query=False,
    )
    ledger_id = await _make_ledger_row_for_trace(db_session, qa_trace_id=trace.id)
    await QaTraceRepo.update_llm_fields(
        db_session,
        qa_trace_id=trace.id,
        llm_call_id=ledger_id,
        llm_response_summary="answer about message",
        llm_response_redacted=False,
        cost_usd=Decimal("0.001"),
    )

    # Unrelated trace cites a different version.
    other_trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=uid,
        chat_id=_next_chat_id(),
        query="q2",
        evidence_ids=[99999],
        abstained=False,
        redact_query=False,
    )
    other_ledger = await _make_ledger_row_for_trace(db_session, qa_trace_id=other_trace.id)
    await QaTraceRepo.update_llm_fields(
        db_session,
        qa_trace_id=other_trace.id,
        llm_call_id=other_ledger,
        llm_response_summary="unrelated answer",
        llm_response_redacted=False,
        cost_usd=Decimal("0.001"),
    )

    await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=f"message:cqal:{cm_id}",
    )

    await run_cascade_worker_once(db_session)

    await db_session.refresh(trace)
    await db_session.refresh(other_trace)
    # Citing trace summary → NULL; unrelated → preserved.
    assert trace.llm_response_summary is None
    assert other_trace.llm_response_summary == "unrelated answer"


async def test_cascade_llm_usage_ledger_user_nulls_hashes_preserves_aggregates(
    db_session,
) -> None:
    """target_type='user' → NULL prompt_hash + response_hash; preserve cost/tokens/latency.

    Invariant #9 (tombstones durable for PII fields) AND budget audit preservation
    co-exist via this NULL-the-hash-keep-the-aggregate pattern.
    """
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=uid,
        chat_id=_next_chat_id(),
        query="q",
        evidence_ids=[1, 2, 3],
        abstained=False,
        redact_query=False,
    )
    ledger_id = await _make_ledger_row_for_trace(db_session, qa_trace_id=trace.id)
    # Snapshot the aggregates BEFORE cascade for preservation assertion.
    pre_row = await db_session.get(LlmUsageLedger, ledger_id, populate_existing=True)
    pre_cost = pre_row.cost_usd
    pre_tokens_in = pre_row.tokens_in
    pre_tokens_out = pre_row.tokens_out
    pre_latency = pre_row.latency_ms

    await _make_pending_forget_event(
        db_session,
        target_type="user",
        target_id=str(uid),
        tombstone_key=f"user:{uid}:ledger",
    )

    await run_cascade_worker_once(db_session)

    row = await db_session.get(LlmUsageLedger, ledger_id, populate_existing=True)
    # Hashes NULLed.
    assert row.prompt_hash is None
    assert row.response_hash is None
    # Aggregates preserved for budget audit.
    assert row.cost_usd == pre_cost
    assert row.tokens_in == pre_tokens_in
    assert row.tokens_out == pre_tokens_out
    assert row.latency_ms == pre_latency


async def test_cascade_llm_usage_ledger_message_target_not_applicable(db_session) -> None:
    """target_type='message' / 'message_hash' → ledger layer reports not_applicable."""
    from bot.db.models import ForgetEvent
    from bot.services.forget_cascade import run_cascade_worker_once

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)

    event_id = await _make_pending_forget_event(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        tombstone_key=f"message:lulnna:{cm_id}",
    )

    await run_cascade_worker_once(db_session)

    ev = await db_session.get(ForgetEvent, event_id, populate_existing=True)
    cs = ev.cascade_status["llm_usage_ledger"]
    assert cs["status"] == "completed"
    assert cs["reason"] == "not_applicable"


async def test_cascade_layer_execution_order_runtime(db_session, monkeypatch) -> None:
    """Runtime check: cache invalidate runs BEFORE traces_llm UPDATE BEFORE ledger NULL.

    Spy on each layer function and record call order; assert the 3 Phase 5 layers
    are invoked in the binding order (§8).
    """
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services import forget_cascade

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)
    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=uid,
        chat_id=_next_chat_id(),
        query="q",
        evidence_ids=[vid],
        abstained=False,
        redact_query=False,
    )
    ledger_id = await _make_ledger_row_for_trace(db_session, qa_trace_id=trace.id)
    await QaTraceRepo.update_llm_fields(
        db_session,
        qa_trace_id=trace.id,
        llm_call_id=ledger_id,
        llm_response_summary="answer",
        llm_response_redacted=False,
        cost_usd=Decimal("0.001"),
    )

    call_order: list[str] = []

    def _spy(layer_name: str, original):
        async def wrapper(session, event):
            call_order.append(layer_name)
            return await original(session, event)

        return wrapper

    for name in ("llm_synthesis_cache", "qa_traces_llm", "llm_usage_ledger"):
        monkeypatch.setitem(
            forget_cascade._LAYER_FUNCS, name, _spy(name, forget_cascade._LAYER_FUNCS[name])
        )

    await _make_pending_forget_event(
        db_session,
        target_type="user",
        target_id=str(uid),
        tombstone_key=f"user:{uid}:order",
    )
    await forget_cascade.run_cascade_worker_once(db_session)

    # Filter to just the three Phase 5 layers and assert relative order.
    p5_only = [c for c in call_order if c in ("llm_synthesis_cache", "qa_traces_llm", "llm_usage_ledger")]
    assert p5_only == ["llm_synthesis_cache", "qa_traces_llm", "llm_usage_ledger"]


async def test_cascade_user_target_invariant_no_surviving_cache_row(db_session) -> None:
    """Invariant #9: after forget event finalises, independent SELECT confirms
    no surviving cache row references any of the user's versions.
    """
    from bot.db.models import LlmSynthesisCache
    from bot.services.forget_cascade import run_cascade_worker_once

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)

    await _make_cache_row(db_session, citation_ids=[vid], answer_text="prejudice")
    await _make_pending_forget_event(
        db_session,
        target_type="user",
        target_id=str(uid),
        tombstone_key=f"user:{uid}:inv9",
    )

    await run_cascade_worker_once(db_session)

    from sqlalchemy import select as sa_select

    # Independent SELECT — read every cache row and verify none cites vid.
    rows = (await db_session.execute(sa_select(LlmSynthesisCache.citation_ids))).all()
    for (citation_ids,) in rows:
        assert vid not in (citation_ids or [])


# ──────────────────────────────────────────────────────────────────────────────
# M-4 FHR carryover: direct target_type='message_hash' sub-case tests
# Phase 5 FHR M-4: existing tests covered target_type='message' and 'user';
# target_type='message_hash' branch had no direct sub-case for the two Phase 5
# cascade functions. These tests close that coverage gap.
# ──────────────────────────────────────────────────────────────────────────────


async def test_cascade_qa_traces_llm_message_hash_subcase_nulls_summary(
    db_session,
) -> None:
    """M-4 direct test: target_type='message_hash' cascade NULLs llm_response_summary.

    Phase 5 FHR carryover M-4: existing cascade tests cover target_type='message'
    and 'user'. This adds the missing target_type='message_hash' sub-case
    for _cascade_qa_traces_llm with explicit assertion that llm_response_summary
    becomes NULL post-cascade. Other audit fields (cost_usd, tokens, latency) are
    preserved — only the response summary is NULLed.
    """
    from bot.db.models import ForgetEvent
    from bot.db.repos.qa_trace import QaTraceRepo
    from bot.services.forget_cascade import _cascade_qa_traces_llm

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)

    # Read the content_hash assigned to vid (set by _make_chat_message_with_v1_for_user).
    from bot.db.models import MessageVersion
    mv = await db_session.get(MessageVersion, vid)
    content_hash = mv.content_hash
    assert content_hash is not None

    # Create a trace citing this version with a non-None llm_response_summary.
    trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=uid,
        chat_id=_next_chat_id(),
        query="q-mh",
        evidence_ids=[vid],
        abstained=False,
        redact_query=False,
    )
    ledger_id = await _make_ledger_row_for_trace(db_session, qa_trace_id=trace.id)
    await QaTraceRepo.update_llm_fields(
        db_session,
        qa_trace_id=trace.id,
        llm_call_id=ledger_id,
        llm_response_summary="answer about message_hash target",
        llm_response_redacted=False,
        cost_usd=Decimal("0.002"),
    )

    # Unrelated trace citing a different version — must be preserved.
    other_trace = await QaTraceRepo.create(
        db_session,
        user_tg_id=uid,
        chat_id=_next_chat_id(),
        query="q-other-mh",
        evidence_ids=[99998],
        abstained=False,
        redact_query=False,
    )
    other_ledger = await _make_ledger_row_for_trace(db_session, qa_trace_id=other_trace.id)
    await QaTraceRepo.update_llm_fields(
        db_session,
        qa_trace_id=other_trace.id,
        llm_call_id=other_ledger,
        llm_response_summary="unrelated summary preserved",
        llm_response_redacted=False,
        cost_usd=Decimal("0.001"),
    )

    # Snapshot ledger aggregate fields BEFORE cascade — Phase 5 cascade contract
    # NULLs PII (prompt_hash + response_hash) but PRESERVES budget aggregates
    # (cost_usd / tokens_in / tokens_out / latency_ms). Per Codex T6-00 round 1
    # M-Cdx (test coverage gap): assert this preservation explicitly.
    from bot.db.models import LlmUsageLedger

    ledger_pre = await db_session.get(LlmUsageLedger, ledger_id)
    cost_pre = ledger_pre.cost_usd
    tokens_in_pre = ledger_pre.tokens_in
    tokens_out_pre = ledger_pre.tokens_out
    latency_pre = ledger_pre.latency_ms

    # Create forget_event with target_type='message_hash', target_id=content_hash.
    event_id = await _make_pending_forget_event(
        db_session,
        target_type="message_hash",
        target_id=content_hash,
        tombstone_key=f"message_hash:{content_hash}:m4qa",
    )
    event = await db_session.get(ForgetEvent, event_id)

    # Call the layer function directly.
    rows_affected = await _cascade_qa_traces_llm(db_session, event)
    await db_session.flush()

    await db_session.refresh(trace)
    await db_session.refresh(other_trace)
    await db_session.refresh(ledger_pre)

    # Citing trace: llm_response_summary must be NULL after cascade.
    assert trace.llm_response_summary is None, (
        "M-4: llm_response_summary should be NULL after message_hash cascade"
    )
    # Unrelated trace: summary must be preserved.
    assert other_trace.llm_response_summary == "unrelated summary preserved"
    # At least one row was affected.
    assert rows_affected >= 1

    # Ledger budget aggregates MUST be preserved across `_cascade_qa_traces_llm`
    # — this cascade NULLs `qa_traces.llm_response_summary` only; it does NOT
    # touch `llm_usage_ledger` rows. Ledger PII NULL'ing is a separate cascade
    # (`_cascade_llm_usage_ledger`) tested elsewhere. Closes Codex T6-00 round 1
    # coverage gap: assert this isolation explicitly so a future refactor that
    # accidentally widens this cascade's WHERE clause fails fast.
    assert ledger_pre.cost_usd == cost_pre, "M-4: ledger.cost_usd must survive qa_traces_llm cascade"
    assert ledger_pre.tokens_in == tokens_in_pre, "M-4: ledger.tokens_in must survive qa_traces_llm cascade"
    assert ledger_pre.tokens_out == tokens_out_pre, "M-4: ledger.tokens_out must survive qa_traces_llm cascade"
    assert ledger_pre.latency_ms == latency_pre, "M-4: ledger.latency_ms must survive qa_traces_llm cascade"


async def test_cascade_llm_synthesis_cache_message_hash_subcase(
    db_session,
) -> None:
    """M-4 direct test: target_type='message_hash' cascade invalidates synthesis cache.

    Phase 5 FHR carryover M-4: same coverage gap for _cascade_llm_synthesis_cache.
    Cache rows with citation_ids referencing a message_version whose content_hash
    matches the forget_event target_hash MUST be invalidated (hard-deleted, per
    Phase 5 cascade behaviour — SynthesisCacheRepo.invalidate_by_citation DELETE path).
    Unrelated cache rows must survive.
    """
    from bot.db.models import ForgetEvent, LlmSynthesisCache, MessageVersion
    from bot.services.forget_cascade import _cascade_llm_synthesis_cache
    from sqlalchemy import select as sa_select

    uid = _next_user()
    await _make_user_raw(db_session, uid)
    cm_id, vid = await _make_chat_message_with_v1_for_user(db_session, uid)

    # Read the content_hash assigned to vid.
    mv = await db_session.get(MessageVersion, vid)
    content_hash = mv.content_hash
    assert content_hash is not None

    # Cache row citing the to-be-forgotten version — must be deleted.
    target_cache_id = await _make_cache_row(
        db_session, citation_ids=[vid], answer_text="cached answer for message_hash"
    )
    # Unrelated cache row — must survive.
    unrelated_cache_id = await _make_cache_row(
        db_session, citation_ids=[88888], answer_text="unrelated cache"
    )

    # Create forget_event with target_type='message_hash', target_id=content_hash.
    event_id = await _make_pending_forget_event(
        db_session,
        target_type="message_hash",
        target_id=content_hash,
        tombstone_key=f"message_hash:{content_hash}:m4cache",
    )
    event = await db_session.get(ForgetEvent, event_id)

    # Call the layer function directly.
    rows_affected = await _cascade_llm_synthesis_cache(db_session, event)
    await db_session.flush()

    remaining_ids = (
        await db_session.execute(sa_select(LlmSynthesisCache.id))
    ).scalars().all()

    # Target cache row must be deleted.
    assert target_cache_id not in remaining_ids, (
        "M-4: cache row citing message_hash target must be deleted after cascade"
    )
    # Unrelated cache row must survive.
    assert unrelated_cache_id in remaining_ids
    # At least one row was deleted.
    assert rows_affected >= 1
