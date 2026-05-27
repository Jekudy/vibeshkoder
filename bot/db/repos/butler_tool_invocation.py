"""Repository for ``butler_tool_invocations`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.
"""

from __future__ import annotations

import logging

from datetime import datetime

from sqlalchemy import select, update

from bot.db.models import ButlerToolInvocation
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class ButlerToolInvocationRepo:
    """Data-access layer for ``butler_tool_invocations``."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        action_id: int,
        tool_name: str,
        idempotency_key: str,
        request_payload: dict,
        request_payload_hash: str,
        status: str,
        invocation_seq: int = 1,
    ) -> ButlerToolInvocation:
        """Insert a new butler_tool_invocations row. Flushes; caller commits."""
        row = ButlerToolInvocation(
            action_id=action_id,
            tool_name=tool_name,
            invocation_seq=invocation_seq,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            request_payload_hash=request_payload_hash,
            status=status,
        )
        session.add(row)
        await session.flush()
        _log.debug("butler_tool_invocations: inserted row id=%s action_id=%s", row.id, action_id)
        return row

    @staticmethod
    async def list_for_action(
        session: AsyncSession,
        action_id: int,
    ) -> list[ButlerToolInvocation]:
        """Return all invocations for a given action (oldest first)."""
        result = await session.execute(
            select(ButlerToolInvocation)
            .where(ButlerToolInvocation.action_id == action_id)
            .order_by(ButlerToolInvocation.started_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def find_by_posted_message_id(
        session: AsyncSession,
        posted_message_id: int,
    ) -> "ButlerToolInvocation | None":
        """Return the invocation row that posted this Telegram message_id.

        Used by update_intro to verify Butler ownership before attempting an edit.
        Returns None if no invocation has posted this message_id (not Butler's).

        The partial index on (posted_message_id) WHERE posted_message_id IS NOT NULL
        (migration 075) ensures this lookup is fast even on large invocation tables.
        """
        result = await session.execute(
            select(ButlerToolInvocation).where(
                ButlerToolInvocation.posted_message_id == posted_message_id
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def update_invocation(
        session: AsyncSession,
        invocation_id: int,
        *,
        status: str,
        response_payload: dict | None = None,
        response_payload_hash: str | None = None,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        error_context: dict | None = None,
        posted_message_id: int | None = None,
        inverse_op_payload: dict | None = None,
    ) -> int:
        """Update an invocation row mid/post-execute.

        status must be one of the CHECK enum values
        ('pending','running','succeeded','failed','rolled_back').

        Used by ButlerService.execute_action after each tool's
        validate_policy / execute call.
        Returns rowcount (should be 1). Raises LookupError if row absent.
        Flushes; caller commits.
        """
        values: dict = {"status": status}
        if response_payload is not None:
            values["response_payload"] = response_payload
        if response_payload_hash is not None:
            values["response_payload_hash"] = response_payload_hash
        if finished_at is not None:
            values["finished_at"] = finished_at
        if error_code is not None:
            values["error_code"] = error_code
        if error_context is not None:
            values["error_context"] = error_context
        if posted_message_id is not None:
            values["posted_message_id"] = posted_message_id
        if inverse_op_payload is not None:
            values["inverse_op_payload"] = inverse_op_payload

        stmt = (
            update(ButlerToolInvocation)
            .where(ButlerToolInvocation.id == invocation_id)
            .values(**values)
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"ButlerToolInvocation(id={invocation_id}) not found — row missing"
            )
        await session.flush()
        _log.debug(
            "butler_tool_invocations: updated id=%s status=%s", invocation_id, status
        )
        return rowcount
