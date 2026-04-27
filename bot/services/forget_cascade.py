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

    Currently supports ``target_type='message'`` only — the row is selected by
    ``ChatMessage.id == int(target_id)``. Other target types (``user``,
    ``message_hash``, ``export``) are reserved for Sprint 4 / #105 (/forget_me)
    and re-import prevention (#97); calling them here returns 0 rows.
    """
    if event.target_type != "message" or event.target_id is None:
        return 0

    try:
        cm_id = int(event.target_id)
    except (TypeError, ValueError):
        # target_id for ``message`` MUST be an integer chat_messages.id; reject
        # rather than silently no-op so a malformed event surfaces as failed.
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
    """
    if event.target_type != "message" or event.target_id is None:
        return 0

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
    """
    # Snapshot current per-layer progress so we can resume.
    cascade_state: dict[str, Any] = dict(event.cascade_status or {})

    try:
        for layer in CASCADE_LAYER_ORDER:
            existing = cascade_state.get(layer)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                # Already done in a previous run — skip.
                continue

            if layer in _LAYER_FUNCS:
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
