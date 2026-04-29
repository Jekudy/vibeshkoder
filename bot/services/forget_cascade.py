"""Forget cascade worker (T3-04, issue #96).

Background worker that drives ``forget_events`` rows through the cascade defined in
HANDOFF.md §10. Phase 3 skeleton: only Phase 1 layers (``chat_messages`` and
``message_versions``) execute; layers whose tables do not yet exist (``message_entities``,
``message_links``, ``attachments``, ``fts_rows``) are recorded as ``skipped`` so the
cascade is forward-compatible — when those tables land in later phases, the cascade
order is already wired and only the per-layer functions need filling in.

Durability invariants (binding for every code path here):

* **Idempotent claim.** ``ForgetEventRepo.mark_status(status='processing')`` is an atomic
  ``UPDATE ... WHERE status='pending' RETURNING``; double-claim is impossible. A
  losing claimant gets ``ValueError`` and skips the row.
* **Restart-safe.** Per-layer progress is checkpointed via
  ``ForgetEventRepo.update_cascade_status``. After a crash mid-cascade, the next
  worker run reads ``cascade_status`` and skips already-completed layers.
* **Per-event isolation.** Each event's cascade is wrapped in its own try/except.
  A failure in one event marks that row ``failed`` but does NOT halt other events
  in the batch.
* **Irreversibility doctrine** (HANDOFF.md §10, ADR-0003). The cascade for
  ``chat_messages`` NULLs ``text``, ``caption``, ``raw_json`` and sets
  ``is_redacted=True``, ``memory_policy='forgotten'``. The cascade for
  ``message_versions`` NULLs ``text``, ``caption``, ``normalized_text``,
  ``entities_json`` and sets ``is_redacted=True``. ``content_hash`` is intentionally
  preserved so prior citations resolve; the redacted flag tells consumers to skip
  the body.

Production wiring is gated by feature flag ``memory.forget.cascade_worker.enabled``
(default OFF) — the scheduler reads the flag every tick and no-ops when off. This
mirrors the AUTHORIZED_SCOPE pattern for new ingestion-style paths.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.engine import async_session
from bot.db.models import ChatMessage, MessageVersion
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.forget_event import ForgetEventRepo

logger = logging.getLogger(__name__)

# Feature flag key — read by the scheduler tick to decide whether to run the worker.
CASCADE_WORKER_FLAG = "memory.forget.cascade_worker.enabled"

# Cascade order per HANDOFF §10. The first two layers are Phase 1 tables; the rest
# are Phase 4+ derived layers whose tables do not yet exist. Skipped layers are
# recorded with ``{"status": "skipped", "reason": "table_not_exists"}`` so the
# cascade_status JSON is forward-compatible: a later phase that adds the table
# replaces the per-layer function and existing rows are reprocessed naturally.
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "message_entities",
    "message_links",
    "attachments",
    "fts_rows",
)


async def _cascade_chat_messages(session: AsyncSession, event) -> int:
    """NULL content fields on ``chat_messages`` rows targeted by this forget_event.

    Per HANDOFF.md §10 irreversibility doctrine, the cascade overwrites:
    - ``text``, ``caption``, ``raw_json`` → ``NULL``
    - ``is_redacted`` → ``True``
    - ``memory_policy`` → ``'forgotten'``

    Returns the number of rows affected.

    Supported target_types:
    - ``message``: single row by ``ChatMessage.id == int(target_id)``.
    - ``user``: all rows by ``ChatMessage.user_id == CAST(target_id AS BIGINT)``
      (User.id == telegram_id per codebase invariant).

    Other target types (``message_hash``, ``export``) are reserved for future
    streams (#97, #105); the caller (``_process_one_event``) must NOT invoke
    this function for them — it skips them before reaching ``_LAYER_FUNCS``.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires a non-None target_id"
        )

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(ChatMessage)
            .where(ChatMessage.id == cm_id)
            .values(
                text=None,
                caption=None,
                raw_json=None,
                is_redacted=True,
                memory_policy="forgotten",
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(ChatMessage)
            .where(ChatMessage.user_id == telegram_id)
            .values(
                text=None,
                caption=None,
                raw_json=None,
                is_redacted=True,
                memory_policy="forgotten",
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    # Should not reach here: _process_one_event guards unsupported target_types
    # before calling _LAYER_FUNCS.  Raise so a regression surfaces immediately.
    raise ValueError(
        f"_cascade_chat_messages: unsupported target_type={event.target_type!r}"
    )


async def _cascade_message_versions(session: AsyncSession, event) -> int:
    """NULL content fields on ``message_versions`` rows whose ``chat_message_id``
    matches this forget_event's target.

    Per HANDOFF.md §10 irreversibility doctrine, the cascade overwrites:
    - ``text``, ``caption``, ``normalized_text``, ``entities_json`` → ``NULL``
    - ``is_redacted`` → ``True``

    ``content_hash`` is intentionally PRESERVED so prior citations remain
    resolvable; the redacted flag tells consumers to skip the body. Matches the
    T1-14 hotfix invariant (closes Codex Phase 1 final-review CRITICAL
    PRIVACY_LEAK_CLASS_4).

    Returns the number of rows affected.

    Supported target_types:
    - ``message``: versions for the single chat_messages row.
    - ``user``: versions for ALL chat_messages rows owned by the user.
      Because ``_cascade_chat_messages`` NULLs text/caption/raw_json but does
      NOT touch ``user_id``, the subquery ``WHERE user_id = telegram_id``
      still resolves correctly.
    """
    if event.target_id is None:
        raise ValueError(
            f"forget_event target_type={event.target_type!r} requires a non-None target_id"
        )

    if event.target_type == "message":
        try:
            cm_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='message' requires integer target_id; "
                f"got {event.target_id!r}"
            )
        stmt = (
            update(MessageVersion)
            .where(MessageVersion.chat_message_id == cm_id)
            .values(
                text=None,
                caption=None,
                normalized_text=None,
                entities_json=None,
                is_redacted=True,
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    if event.target_type == "user":
        try:
            telegram_id = int(event.target_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"forget_event target_type='user' requires integer target_id (telegram_id); "
                f"got {event.target_id!r}"
            )
        # Select version ids via subquery on chat_messages.user_id. The previous
        # layer NULLed text/caption/raw_json but user_id column is untouched, so
        # this subquery resolves correctly regardless of layer order.
        from sqlalchemy import select as sa_select

        stmt = (
            update(MessageVersion)
            .where(
                MessageVersion.chat_message_id.in_(
                    sa_select(ChatMessage.id).where(ChatMessage.user_id == telegram_id)
                )
            )
            .values(
                text=None,
                caption=None,
                normalized_text=None,
                entities_json=None,
                is_redacted=True,
            )
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount or 0

    # Should not reach here: _process_one_event guards unsupported target_types.
    raise ValueError(
        f"_cascade_message_versions: unsupported target_type={event.target_type!r}"
    )


# Map layer name → cascade function. Only Phase 1 layers have functions here;
# the rest are recorded as skipped. When a future phase adds a layer's table,
# add its function to this map and remove it from the implicit "skipped" set.
_LAYER_FUNCS: dict[str, Any] = {
    "chat_messages": _cascade_chat_messages,
    "message_versions": _cascade_message_versions,
}


async def _process_one_event(session: AsyncSession, event) -> None:
    """Run the full cascade for a single (already-claimed) forget_event row.

    Resumes from ``cascade_status``: layers already marked ``completed`` are
    skipped. Each layer's outcome is checkpointed via ``update_cascade_status``
    BEFORE moving on, so a crash between layers leaves the worker in a state
    the next run can resume from.

    On success, transitions the row to ``status='completed'`` with the final
    cascade_status. On exception, transitions to ``status='failed'`` with the
    exception captured under ``cascade_status['error']``.

    H4 fix (p2-hotfix): the entire cascade is wrapped in ``begin_nested()``
    (SAVEPOINT). This ensures that a real PostgreSQL-level DB error in any layer
    function aborts only this event's sub-transaction, leaving the outer
    transaction valid so ``run_cascade_worker_once`` can continue with the next
    event. Without the savepoint, a DB error would abort the outer transaction and
    subsequent events would fail with InFailedSQLTransactionError.
    """
    # Snapshot current per-layer progress so we can resume.
    cascade_state: dict[str, Any] = dict(event.cascade_status or {})

    # target_types whose cascade is not yet implemented. The event still finalises
    # as 'completed' (all layers explicitly accounted for), but each layer records
    # status='skipped' so the audit trail shows no work was done.
    # Stream Delta #97 (message_hash) and Bravo importer (#105) will fill these in.
    _SKIP_TARGET_TYPES = frozenset({"message_hash", "export"})

    try:
        for layer in CASCADE_LAYER_ORDER:
            existing = cascade_state.get(layer)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                # Already done in a previous run — skip.
                continue

            if event.target_type in _SKIP_TARGET_TYPES:
                # Uniform reason: target_type_not_supported_yet (regardless of whether
                # the layer's table exists yet — the dominant reason is the outer
                # unsupported target_type). Phase-1 layers include rows=0 for consistency
                # with the supported-target_type completion shape.
                if layer in _LAYER_FUNCS:
                    cascade_state[layer] = {
                        "status": "skipped",
                        "reason": "target_type_not_supported_yet",
                        "rows": 0,
                    }
                else:
                    cascade_state[layer] = {
                        "status": "skipped",
                        "reason": "target_type_not_supported_yet",
                    }
            elif layer in _LAYER_FUNCS:
                # H4 fix (p2-hotfix): wrap each active layer call in a SAVEPOINT so
                # that a real PostgreSQL-level DB error in one layer aborts only that
                # layer's sub-transaction, leaving the outer transaction valid. Without
                # per-layer savepoints, a DB error would abort the outer transaction and
                # subsequent events in the batch would fail with InFailedSQLTransactionError,
                # breaking the per-event isolation guarantee.
                async with session.begin_nested():
                    rows = await _LAYER_FUNCS[layer](session, event)
                cascade_state[layer] = {"status": "completed", "rows": rows}
            else:
                # Phase 4+ layers — table not yet present in this codebase.
                cascade_state[layer] = {
                    "status": "skipped",
                    "reason": "table_not_exists",
                }

            # Checkpoint after every layer so a crash mid-cascade is recoverable.
            await ForgetEventRepo.update_cascade_status(
                session, event.id, cascade_status=cascade_state
            )

        await ForgetEventRepo.mark_status(
            session, event.id, status="completed", cascade_status=cascade_state
        )
    except Exception as exc:
        cascade_state["error"] = repr(exc)
        # Try to record the failure; if THAT itself fails (e.g. terminal state
        # already set), let the outer exception propagate to the batch loop.
        # The per-layer begin_nested() savepoint was rolled back on exception exit,
        # so the outer transaction is still valid here.
        await ForgetEventRepo.mark_status(
            session, event.id, status="failed", cascade_status=cascade_state
        )
        raise


async def run_cascade_worker_once(
    session: AsyncSession,
    *,
    batch_size: int = 10,
) -> dict[str, int]:
    """Process up to ``batch_size`` pending forget_events.

    Returns a stats dict ``{claimed, processed, failed}``:
    - ``claimed`` — events successfully transitioned ``pending → processing``
    - ``processed`` — events that completed the cascade (status='completed')
    - ``failed`` — events whose cascade raised (status='failed')

    The function does NOT commit. Caller controls the transaction lifecycle.
    Per-event isolation: a failure in one event's cascade marks ONLY that event
    as ``failed`` and continues with the rest of the batch. The function returns
    normally even if some events failed.
    """
    pending = await ForgetEventRepo.list_pending(session, limit=batch_size)
    stats = {"claimed": 0, "processed": 0, "failed": 0}

    for event in pending:
        # Atomic claim: pending → processing. Race-safe via the repo's
        # WHERE-status filter; if another worker already claimed this row, the
        # repo raises ValueError and we skip silently.
        try:
            claimed = await ForgetEventRepo.mark_status(
                session, event.id, status="processing"
            )
        except ValueError:
            logger.debug(
                "cascade_worker: skipping already-claimed forget_event id=%s",
                event.id,
            )
            continue
        stats["claimed"] += 1

        try:
            await _process_one_event(session, claimed)
            stats["processed"] += 1
        except Exception:
            logger.exception(
                "cascade_worker: cascade failed for forget_event id=%s "
                "(other events in batch unaffected)",
                claimed.id,
            )
            stats["failed"] += 1

    return stats


async def cascade_worker_tick(
    session: AsyncSession | None = None,
    *,
    batch_size: int = 10,
) -> dict[str, int]:
    """Scheduler entry point for the cascade worker.

    Reads the ``memory.forget.cascade_worker.enabled`` feature flag (default
    OFF) and either runs one batch of ``run_cascade_worker_once`` or returns
    immediately. Mirrors the AUTHORIZED_SCOPE pattern for new ingestion-style
    paths: code lands first, the flag stays OFF in production until the
    implementation is verified end-to-end.

    Two callers:
    - Production: APScheduler tick. ``session`` is None — the function opens
      its own session via ``async_session()`` and commits at the end (same
      pattern as ``process_invite_outbox`` and ``check_intro_refresh``).
    - Tests: an explicit ``session`` is passed; the function uses it directly
      WITHOUT committing, so outer-tx isolation is preserved.

    Returns the same stats dict as ``run_cascade_worker_once`` (with all-zero
    counts when the flag is off).
    """
    if session is not None:
        if not await FeatureFlagRepo.get(session, CASCADE_WORKER_FLAG):
            return {"claimed": 0, "processed": 0, "failed": 0}
        return await run_cascade_worker_once(session, batch_size=batch_size)

    # Production path: own session + commit on success.
    async with async_session() as own_session:
        if not await FeatureFlagRepo.get(own_session, CASCADE_WORKER_FLAG):
            return {"claimed": 0, "processed": 0, "failed": 0}
        try:
            stats = await run_cascade_worker_once(own_session, batch_size=batch_size)
            await own_session.commit()
            return stats
        except Exception:
            await own_session.rollback()
            raise
