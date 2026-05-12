"""T6-04 carryover: forget-cascade orchestrator advisory-lock wiring.

PHASE6_PLAN.md §5.A.5 step 1 + T6-04 acceptance bullet 5:

    The forget-cascade orchestrator (`_process_one_event` in
    `bot/services/forget_cascade.py` — the de-facto `apply_forget_event`
    per §5.A.5) MUST acquire `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))`
    for the affected `message_version_id` as the FIRST step of each event apply.

Closes H-Cdx-2 race window between `/approve` and forget cascade.

These tests cover:

* The orchestrator emits the ``SELECT pg_advisory_xact_lock(:lock_id)`` SQL
  for every affected mvid BEFORE any cascade layer fires.
* For ``target_type='message'``: lock id derived from the message's
  current_version_id.
* For ``target_type='message_hash'``: lock ids derived from every mvid sharing
  the content_hash.
* For ``target_type='user'``: lock ids derived from every mvid the user
  authored.
* For ``target_type='export'``: no lock taken (cascade is skipped).
* Sorted lock acquisition order (deadlock prevention with /approve).

The tests use a Postgres-only fixture (db_session); pytest skips when PG
is unreachable.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=7_200_000_000)
_msg_counter = itertools.count(start=720_000)
_chat_counter = itertools.count(start=1)
_key_counter = itertools.count(start=1)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_key(prefix: str = "message") -> str:
    return f"{prefix}:t6-04-lock:{next(_key_counter)}"


async def _make_user(db_session, *, telegram_id: int | None = None) -> int:
    from bot.db.repos.user import UserRepo

    uid = telegram_id if telegram_id is not None else _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="U",
        last_name=None,
    )
    return uid


async def _make_chat_message_with_v1(
    db_session,
    *,
    user_id: int | None = None,
    content_hash: str | None = None,
) -> tuple[int, int]:
    """Returns (chat_message_id, message_version_id)."""
    import uuid as _uuid_module

    from sqlalchemy import update as sa_update

    from bot.db.models import ChatMessage, MessageVersion

    if user_id is None:
        user_id = await _make_user(db_session)
    chat_id = _next_chat_id()
    message_id = _next_msg_id()
    when = datetime.now(timezone.utc)

    cm = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="src",
        date=when,
        created_at=when,
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(cm)
    await db_session.flush()

    mv_ch = content_hash or f"h{_uuid_module.uuid4().hex[:16]}"
    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text="src",
        normalized_text="src",
        entities_json={},
        content_hash=mv_ch,
        is_redacted=False,
    )
    db_session.add(mv)
    await db_session.flush()
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == cm.id)
        .values(current_version_id=mv.id)
    )
    await db_session.flush()
    return cm.id, mv.id


async def _make_pending_forget_event(
    db_session,
    *,
    target_type: str,
    target_id: str | None,
    tombstone_key: str | None = None,
):
    from bot.db.repos.forget_event import ForgetEventRepo

    ev = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=tombstone_key or _next_key(target_type),
    )
    return ev


# ─── _resolve_affected_mvids — pure resolver ────────────────────────────────


async def test_resolve_affected_mvids_message_returns_current_version(
    db_session,
) -> None:
    """``target_type='message'`` resolves to ``[current_version_id]``."""
    from bot.services.forget_cascade import _resolve_affected_mvids

    cm_id, mv_id = await _make_chat_message_with_v1(db_session)
    ev = await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_id)
    )
    mvids = await _resolve_affected_mvids(db_session, ev)
    assert mvids == [mv_id]


async def test_resolve_affected_mvids_message_hash_returns_all_matching(
    db_session,
) -> None:
    """``target_type='message_hash'`` resolves every mvid sharing the hash."""
    from bot.services.forget_cascade import _resolve_affected_mvids

    shared = "deadbeefcafe1234"
    _, mv1 = await _make_chat_message_with_v1(db_session, content_hash=shared)
    _, mv2 = await _make_chat_message_with_v1(db_session, content_hash=shared)
    # Unrelated mvid with different hash.
    await _make_chat_message_with_v1(db_session)
    ev = await _make_pending_forget_event(
        db_session, target_type="message_hash", target_id=shared
    )
    mvids = await _resolve_affected_mvids(db_session, ev)
    assert set(mvids) == {mv1, mv2}


async def test_resolve_affected_mvids_user_returns_all_user_mvids(
    db_session,
) -> None:
    """``target_type='user'`` resolves every mvid the user authored."""
    from bot.services.forget_cascade import _resolve_affected_mvids

    user = await _make_user(db_session)
    _, mv1 = await _make_chat_message_with_v1(db_session, user_id=user)
    _, mv2 = await _make_chat_message_with_v1(db_session, user_id=user)
    # Unrelated user's mvid.
    other = await _make_user(db_session)
    await _make_chat_message_with_v1(db_session, user_id=other)
    ev = await _make_pending_forget_event(
        db_session, target_type="user", target_id=str(user)
    )
    mvids = await _resolve_affected_mvids(db_session, ev)
    assert set(mvids) == {mv1, mv2}


async def test_resolve_affected_mvids_export_returns_empty(db_session) -> None:
    """``target_type='export'`` is skipped — cascade is not implemented and
    no mvids are locked."""
    from bot.services.forget_cascade import _resolve_affected_mvids

    ev = await _make_pending_forget_event(
        db_session, target_type="export", target_id="42"
    )
    mvids = await _resolve_affected_mvids(db_session, ev)
    assert mvids == []


# ─── orchestrator emits lock SQL BEFORE cascade ─────────────────────────────


async def test_process_one_event_acquires_advisory_lock_for_message_target(
    db_session, monkeypatch
) -> None:
    """``_process_one_event`` MUST emit ``pg_advisory_xact_lock`` SQL with the
    P6 lock id for every affected mvid, BEFORE any cascade layer fires."""
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    cm_id, mv_id = await _make_chat_message_with_v1(db_session)
    ev = await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_id)
    )
    # Claim the row first (mirrors what run_cascade_worker_once does).
    claimed = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="processing"
    )

    # Spy on session.execute so we capture every SQL emitted.
    captured: list[dict[str, Any]] = []
    original_execute = db_session.execute

    async def spy_execute(stmt, *args, **kwargs):
        # Render the SQL string for inspection.
        try:
            sql_text = str(stmt)
        except Exception:
            sql_text = repr(stmt)
        params = args[0] if args else kwargs
        captured.append({"sql": sql_text, "params": params})
        return await original_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spy_execute)

    await forget_cascade._process_one_event(db_session, claimed)

    # Find every advisory_xact_lock invocation.
    lock_calls = [
        c for c in captured if "pg_advisory_xact_lock" in c["sql"]
    ]
    assert len(lock_calls) >= 1, (
        "_process_one_event must acquire pg_advisory_xact_lock at least once "
        "for a message target_type"
    )
    expected_lock_id = _p6_mvid_advisory_lock_id(mv_id)
    seen_lock_ids = [
        c["params"].get("lock_id") if isinstance(c["params"], dict) else None
        for c in lock_calls
    ]
    assert expected_lock_id in seen_lock_ids


async def test_process_one_event_locks_sorted_order(
    db_session, monkeypatch
) -> None:
    """Sorted lock acquisition order — deadlock avoidance with /approve."""
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    user = await _make_user(db_session)
    mvids: list[int] = []
    for _ in range(4):
        _, m = await _make_chat_message_with_v1(db_session, user_id=user)
        mvids.append(m)
    ev = await _make_pending_forget_event(
        db_session, target_type="user", target_id=str(user)
    )
    claimed = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="processing"
    )

    captured_lock_ids: list[int] = []
    original_execute = db_session.execute

    async def spy_execute(stmt, *args, **kwargs):
        try:
            sql_text = str(stmt)
        except Exception:
            sql_text = repr(stmt)
        if "pg_advisory_xact_lock" in sql_text:
            params = args[0] if args else kwargs
            if isinstance(params, dict) and "lock_id" in params:
                captured_lock_ids.append(params["lock_id"])
        return await original_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spy_execute)
    await forget_cascade._process_one_event(db_session, claimed)

    # Lock ids appear sorted (smallest first).
    p6_locks = [
        lid
        for lid in captured_lock_ids
        if lid in {_p6_mvid_advisory_lock_id(m) for m in mvids}
    ]
    assert p6_locks == sorted(p6_locks), (
        f"expected sorted lock acquisition order; got {p6_locks}"
    )


async def test_process_one_event_no_lock_for_export(
    db_session, monkeypatch
) -> None:
    """No advisory locks for ``target_type='export'`` (cascade skipped)."""
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade

    ev = await _make_pending_forget_event(
        db_session, target_type="export", target_id="123"
    )
    claimed = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="processing"
    )

    captured_lock_calls: list[dict[str, Any]] = []
    original_execute = db_session.execute

    async def spy_execute(stmt, *args, **kwargs):
        try:
            sql_text = str(stmt)
        except Exception:
            sql_text = repr(stmt)
        if "pg_advisory_xact_lock" in sql_text:
            captured_lock_calls.append({"sql": sql_text})
        return await original_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spy_execute)
    await forget_cascade._process_one_event(db_session, claimed)
    assert captured_lock_calls == [], (
        "no P6 advisory lock should be acquired for target_type='export'"
    )


# ─── existing cascade behaviour preserved ────────────────────────────────────


async def test_cascade_completes_after_lock_acquisition(db_session) -> None:
    """The cascade still NULLs content + flips is_redacted after the lock is
    acquired — i.e., the lock wiring does not break the existing pipeline."""
    from bot.db.models import ChatMessage
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.forget_cascade import _process_one_event

    cm_id, _ = await _make_chat_message_with_v1(db_session)
    ev = await _make_pending_forget_event(
        db_session, target_type="message", target_id=str(cm_id)
    )
    claimed = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="processing"
    )
    await _process_one_event(db_session, claimed)

    cm = await db_session.get(ChatMessage, cm_id)
    assert cm.is_redacted is True
    assert cm.text is None


# ─── Codex round 2 MED #2 regression: lock-order before mvid resolution ─────


async def test_process_one_event_locks_before_resolving_chat_messages(
    db_session, monkeypatch
) -> None:
    """``_process_one_event`` MUST acquire its first advisory lock BEFORE any
    SELECT against ``chat_messages`` or ``message_versions`` for mvid
    resolution.

    Codex round 2 MED #2 (regression for the CRITICAL #2 fix): the previous
    implementation called ``_resolve_affected_mvids`` (which reads
    chat_messages/message_versions) BEFORE acquiring ``pg_advisory_xact_lock``.
    That violated the "lock before any read that informs cascade work"
    discipline and opened a residual race window where ``/approve`` could
    acquire locks for newly-created mvids the cascade had not yet seen,
    write ``card_sources``, and commit — leaving the cascade with a stale
    mvid list.

    The CRITICAL #2 fix takes an event-level coarse advisory lock as the
    FIRST DB action (before any cascade-related read), then resolves mvids
    inside the locked region. This regression test pins that ordering via
    mock interception of ``session.execute`` so any future refactor that
    re-introduces the read-before-lock pattern fires the assertion.

    Test approach: spy on ``session.execute`` for the entire run of
    ``_process_one_event`` for a ``target_type='user'`` event, then verify
    the timestamp ordering — first ``pg_advisory_xact_lock`` precedes the
    first user-resolution SELECT (chat_messages JOIN message_versions on
    user_id).
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services import forget_cascade

    user = await _make_user(db_session)
    for _ in range(2):
        await _make_chat_message_with_v1(db_session, user_id=user)
    ev = await _make_pending_forget_event(
        db_session, target_type="user", target_id=str(user)
    )
    claimed = await ForgetEventRepo.mark_status(
        db_session, ev.id, status="processing"
    )

    captured_sql: list[str] = []
    original_execute = db_session.execute

    async def spy_execute(stmt, *args, **kwargs):
        try:
            sql_text = str(stmt)
        except Exception:
            sql_text = repr(stmt)
        captured_sql.append(sql_text)
        return await original_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spy_execute)
    await forget_cascade._process_one_event(db_session, claimed)

    # First advisory lock index.
    lock_idx = next(
        (i for i, s in enumerate(captured_sql) if "pg_advisory_xact_lock" in s),
        None,
    )
    assert lock_idx is not None, "expected at least one pg_advisory_xact_lock"

    # First SELECT that joins chat_messages with message_versions (this is the
    # signature of _resolve_affected_mvids for user-target). MUST come AFTER
    # the lock.
    resolve_idx = next(
        (
            i
            for i, s in enumerate(captured_sql)
            if (
                "FROM message_versions" in s
                and "chat_messages" in s
                and "user_id" in s
            )
        ),
        None,
    )
    assert resolve_idx is not None, (
        "expected mvid-resolution SELECT against chat_messages JOIN "
        "message_versions for user-target event"
    )
    assert lock_idx < resolve_idx, (
        f"advisory lock at idx {lock_idx} MUST precede mvid-resolution SELECT "
        f"at idx {resolve_idx}; SQL captured: {captured_sql}"
    )
