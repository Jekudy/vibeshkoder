"""Repository for ``butler_tool_invocations`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

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
