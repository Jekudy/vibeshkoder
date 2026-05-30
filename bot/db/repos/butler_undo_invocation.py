"""Repository for ``butler_undo_invocations`` (T12-07).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from bot.db.models import ButlerUndoInvocation
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class ButlerUndoInvocationRepo:
    """Data-access layer for ``butler_undo_invocations``."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        butler_action_id: int,
        butler_tool_invocation_id: int,
        requester_user_id: int,
        rollback_kind: str,
        status: str = "pending",
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> ButlerUndoInvocation:
        """Insert a new butler_undo_invocations row. Flushes; caller commits."""
        row = ButlerUndoInvocation(
            butler_action_id=butler_action_id,
            butler_tool_invocation_id=butler_tool_invocation_id,
            requester_user_id=requester_user_id,
            rollback_kind=rollback_kind,
            status=status,
            error_kind=error_kind,
            error_message=error_message,
        )
        session.add(row)
        await session.flush()
        _log.debug(
            "butler_undo_invocations: inserted row id=%s action_id=%s invocation_id=%s",
            row.id,
            butler_action_id,
            butler_tool_invocation_id,
        )
        return row

    @staticmethod
    async def find_by_action_and_invocation(
        session: AsyncSession,
        butler_action_id: int,
        butler_tool_invocation_id: int,
    ) -> ButlerUndoInvocation | None:
        """Return the undo audit row for a specific (action, invocation) pair.

        Returns None if not found (idempotency check: has undo already been run?).
        """
        result = await session.execute(
            select(ButlerUndoInvocation).where(
                ButlerUndoInvocation.butler_action_id == butler_action_id,
                ButlerUndoInvocation.butler_tool_invocation_id == butler_tool_invocation_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_action(
        session: AsyncSession,
        butler_action_id: int,
    ) -> list[ButlerUndoInvocation]:
        """Return all undo audit rows for a given action (oldest first)."""
        result = await session.execute(
            select(ButlerUndoInvocation)
            .where(ButlerUndoInvocation.butler_action_id == butler_action_id)
            .order_by(ButlerUndoInvocation.created_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_status(
        session: AsyncSession,
        undo_invocation_id: int,
        *,
        status: str,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> int:
        """Update an undo invocation row status.

        status must be one of the CHECK enum values:
        ('pending','succeeded','failed','skipped_not_reversible').

        Returns rowcount (should be 1). Raises LookupError if row absent.
        Flushes; caller commits.
        """
        # Always set status. Always set error_kind/error_message — passing None
        # explicitly nulls them, which clears stale error info on a successful retry.
        values: dict = {
            "status": status,
            "error_kind": error_kind,
            "error_message": error_message,
        }

        stmt = (
            update(ButlerUndoInvocation)
            .where(ButlerUndoInvocation.id == undo_invocation_id)
            .values(**values)
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"ButlerUndoInvocation(id={undo_invocation_id}) not found — row missing"
            )
        await session.flush()
        _log.debug(
            "butler_undo_invocations: updated id=%s status=%s",
            undo_invocation_id,
            status,
        )
        return rowcount
