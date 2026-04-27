"""Repository for ``forget_events`` (T3-01).

Thin data-access layer for forget/tombstone events. Sprint 3 (#96) adds the cascade
worker that drives status transitions; this repo exposes only the primitives needed
by that worker and by the forget commands (Sprints 2 / 4 — #95 / #105).

Status lifecycle: pending → processing → completed | failed.
Allowed transitions:
  - pending → processing
  - processing → completed
  - processing → failed

Any other transition raises ``ValueError`` immediately (before any DB call).
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ForgetEvent

# Valid transitions: key = current status, value = set of allowed next statuses.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing"}),
    "processing": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


class ForgetEventRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        target_type: str,
        target_id: str | None,
        actor_user_id: int | None,
        authorized_by: str,
        tombstone_key: str,
        reason: str | None = None,
        policy: str = "forgotten",
    ) -> ForgetEvent:
        """Insert a new ForgetEvent; return existing row if tombstone_key already taken.

        Idempotent on ``tombstone_key``: re-issuing a forget for the same target
        returns the existing row without raising or creating a duplicate.

        Flushes; does not commit. Caller controls the transaction lifecycle.

        Implementation: ``INSERT ... ON CONFLICT (tombstone_key) DO NOTHING RETURNING *``.
        On conflict the RETURNING result is empty; we then SELECT the existing row.
        Both operations live in the caller's transaction (no commit, no rollback here).
        This is race-safe: two concurrent callers both pass no application-level check;
        one wins the insert, the other hits the conflict path and fetches the winner's row.
        """
        stmt = (
            pg_insert(ForgetEvent)
            .values(
                target_type=target_type,
                target_id=target_id,
                actor_user_id=actor_user_id,
                authorized_by=authorized_by,
                tombstone_key=tombstone_key,
                reason=reason,
                policy=policy,
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=["tombstone_key"])
            .returning(ForgetEvent)
        )
        result = await session.execute(stmt)
        inserted = result.scalars().first()
        if inserted is not None:
            await session.flush()
            return inserted

        # Conflict path: the row already exists.
        existing = await session.execute(
            select(ForgetEvent).where(ForgetEvent.tombstone_key == tombstone_key)
        )
        return existing.scalars().one()

    @staticmethod
    async def get_by_tombstone_key(
        session: AsyncSession,
        tombstone_key: str,
    ) -> ForgetEvent | None:
        """Return the row matching ``tombstone_key``, or ``None`` if not found."""
        result = await session.execute(
            select(ForgetEvent).where(ForgetEvent.tombstone_key == tombstone_key)
        )
        return result.scalars().first()

    @staticmethod
    async def list_pending(
        session: AsyncSession,
        limit: int = 100,
    ) -> list[ForgetEvent]:
        """Return up to ``limit`` pending rows ordered by ``created_at`` ASC.

        Used by the cascade worker (Sprint 3) to fetch the next batch to process.
        """
        result = await session.execute(
            select(ForgetEvent)
            .where(ForgetEvent.status == "pending")
            .order_by(ForgetEvent.created_at.asc(), ForgetEvent.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_status(
        session: AsyncSession,
        forget_event_id: int,
        *,
        status: str,
        cascade_status: dict | None = None,
    ) -> ForgetEvent:
        """Update status (and optionally cascade_status) of an existing ForgetEvent.

        Enforces the lifecycle state machine:
          - pending → processing
          - processing → completed
          - processing → failed

        Raises ``ValueError`` immediately (before any DB call) if the transition is
        invalid. This includes unknown statuses and backward / skip transitions such
        as ``completed → processing``.

        Flushes; does not commit. Caller controls the transaction lifecycle.
        """
        if status not in _ALLOWED_TRANSITIONS:
            raise ValueError(
                f"Unknown forget_event status {status!r}. "
                f"Must be one of: {sorted(_ALLOWED_TRANSITIONS)}"
            )

        # Compute which current statuses may transition to the requested status.
        allowed_old = [
            old for old, nexts in _ALLOWED_TRANSITIONS.items() if status in nexts
        ]

        values: dict = {"status": status, "updated_at": func.now()}
        if cascade_status is not None:
            values["cascade_status"] = cascade_status

        stmt = (
            update(ForgetEvent)
            .where(ForgetEvent.id == forget_event_id)
            .where(ForgetEvent.status.in_(allowed_old))
            .values(**values)
            .returning(ForgetEvent)
        )
        result = await session.execute(stmt)
        row = result.scalars().first()
        if row is not None:
            await session.flush()
            return row

        # Either the id doesn't exist or the current status disallows the transition.
        # Re-fetch with populate_existing=True so identity-map cache (expire_on_commit=False
        # in bot/db/engine.py) does not shadow the actual current DB status in the error
        # message. Without this flag a stale cached row would mislead the caller.
        actual = await session.get(
            ForgetEvent, forget_event_id, populate_existing=True
        )
        if actual is None:
            raise ValueError(f"ForgetEvent(id={forget_event_id}) not found")
        allowed_next = _ALLOWED_TRANSITIONS.get(actual.status, frozenset())
        raise ValueError(
            f"Invalid status transition for ForgetEvent(id={forget_event_id}): "
            f"{actual.status!r} → {status!r}. "
            f"Allowed from {actual.status!r}: {sorted(allowed_next) or '[]'}"
        )

    @staticmethod
    async def update_cascade_status(
        session: AsyncSession,
        forget_event_id: int,
        *,
        cascade_status: dict,
    ) -> ForgetEvent:
        """Checkpoint cascade progress on a row in ``status='processing'``.

        Sprint 3 (#96) cascade primitive. Separate from ``mark_status`` so the cascade
        worker can write per-layer progress mid-cascade without transitioning state.
        ``mark_status`` rejects ``processing → processing`` (not in the state machine);
        without this method, a worker crash mid-cascade would lose all per-layer progress.

        Atomic ``UPDATE ... WHERE id = ? AND status = 'processing' RETURNING``. Rejects
        any other current status (including ``pending`` — the row must be claimed first
        via ``mark_status(status='processing')`` — and the terminal states).

        Flushes; does not commit. Caller controls the transaction lifecycle.

        Raises ``ValueError`` if the row does not exist or is not in ``processing``.
        """
        stmt = (
            update(ForgetEvent)
            .where(ForgetEvent.id == forget_event_id)
            .where(ForgetEvent.status == "processing")
            .values(cascade_status=cascade_status, updated_at=func.now())
            .returning(ForgetEvent)
        )
        result = await session.execute(stmt)
        row = result.scalars().first()
        if row is not None:
            await session.flush()
            return row

        # Either the id doesn't exist or the row is not in 'processing'.
        actual = await session.get(
            ForgetEvent, forget_event_id, populate_existing=True
        )
        if actual is None:
            raise ValueError(f"ForgetEvent(id={forget_event_id}) not found")
        raise ValueError(
            f"update_cascade_status requires status='processing'; "
            f"ForgetEvent(id={forget_event_id}) is in {actual.status!r}."
        )
