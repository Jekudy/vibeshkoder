"""Repository for ``butler_action_confirmations`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from bot.db.models import ButlerActionConfirmation
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class ButlerActionConfirmationRepo:
    """Data-access layer for ``butler_action_confirmations``."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        action_id: int,
        confirmer_tg_id: int,
        confirmation_role: str,
        status: str,
        preview_payload_hash: str,
        expires_at: datetime,
        confirmation_message_chat_id: int | None = None,
        confirmation_message_id: int | None = None,
    ) -> ButlerActionConfirmation:
        """Insert a new butler_action_confirmations row. Flushes; caller commits."""
        row = ButlerActionConfirmation(
            action_id=action_id,
            confirmer_tg_id=confirmer_tg_id,
            confirmation_role=confirmation_role,
            status=status,
            preview_payload_hash=preview_payload_hash,
            expires_at=expires_at,
            confirmation_message_chat_id=confirmation_message_chat_id,
            confirmation_message_id=confirmation_message_id,
        )
        session.add(row)
        await session.flush()
        _log.debug(
            "butler_action_confirmations: inserted row id=%s action_id=%s",
            row.id,
            action_id,
        )
        return row

    @staticmethod
    async def mark_resolved(
        session: AsyncSession,
        confirmation_id: int,
        *,
        status: str,
        resolved_at: datetime | None = None,
    ) -> int:
        """Mark a confirmation row as confirmed, rejected, expired, or cancelled.

        Returns rowcount (should be 1). Raises LookupError if not found.
        """
        ts = resolved_at if resolved_at is not None else datetime.now(timezone.utc)
        values: dict = {"status": status}
        if status == "confirmed":
            values["confirmed_at"] = ts
        elif status in ("rejected",):
            values["rejected_at"] = ts

        stmt = (
            update(ButlerActionConfirmation)
            .where(ButlerActionConfirmation.id == confirmation_id)
            .values(**values)
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"ButlerActionConfirmation(id={confirmation_id}) not found"
            )
        await session.flush()
        return rowcount

    @staticmethod
    async def list_pending_for_user(
        session: AsyncSession,
        confirmer_tg_id: int,
    ) -> list[ButlerActionConfirmation]:
        """Return all pending confirmations for a user (oldest first)."""
        result = await session.execute(
            select(ButlerActionConfirmation)
            .where(
                ButlerActionConfirmation.confirmer_tg_id == confirmer_tg_id,
                ButlerActionConfirmation.status == "pending",
            )
            .order_by(ButlerActionConfirmation.created_at.asc())
        )
        return list(result.scalars().all())
