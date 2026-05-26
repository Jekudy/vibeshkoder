"""Repository for ``butler_actions`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (handler / service) owns the transaction lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from bot.db.models import ButlerAction
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class ButlerActionRepo:
    """Data-access layer for ``butler_actions``.

    All methods are ``@staticmethod`` and flush-only.
    """

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        requester_tg_id: int,
        chat_id: int,
        action_type: str,
        status: str,
        tool_name: str,
        tool_manifest_version: str,
        governance_filter_version: str,
        evidence_context_hash: str,
        plan_summary: str,
        action_args: dict,
        action_args_hash: str,
        rollback_kind: str,
        risk_level: str,
        evidence_ids: list | None = None,
        approved_card_source_ids: list | None = None,
        requires_confirmation: bool = True,
        confirmation_policy: str = "per_action",
        expires_at: datetime | None = None,
        llm_usage_ledger_id: int | None = None,
    ) -> ButlerAction:
        """Insert a new butler_actions row. Flushes; caller commits."""
        row = ButlerAction(
            requester_tg_id=requester_tg_id,
            chat_id=chat_id,
            action_type=action_type,
            status=status,
            tool_name=tool_name,
            tool_manifest_version=tool_manifest_version,
            governance_filter_version=governance_filter_version,
            evidence_context_hash=evidence_context_hash,
            evidence_ids=evidence_ids if evidence_ids is not None else [],
            approved_card_source_ids=(
                approved_card_source_ids if approved_card_source_ids is not None else []
            ),
            plan_summary=plan_summary,
            action_args=action_args,
            action_args_hash=action_args_hash,
            rollback_kind=rollback_kind,
            risk_level=risk_level,
            requires_confirmation=requires_confirmation,
            confirmation_policy=confirmation_policy,
            expires_at=expires_at,
            llm_usage_ledger_id=llm_usage_ledger_id,
        )
        session.add(row)
        await session.flush()
        _log.debug("butler_actions: inserted row id=%s", row.id)
        return row

    @staticmethod
    async def get(session: AsyncSession, action_id: int) -> ButlerAction | None:
        """Fetch a ButlerAction by PK. Returns None if not found."""
        result = await session.execute(
            select(ButlerAction).where(ButlerAction.id == action_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_for_update(session: AsyncSession, action_id: int) -> ButlerAction | None:
        """SELECT FOR UPDATE on butler_actions row. Returns None if row absent.

        Used by ButlerService.confirm_action / execute_action / cancel_action /
        expire_action to coordinate with ``_cascade_butler_actions`` per §3.6 step 5
        (cascade holds an advisory lock; this matches via SELECT FOR UPDATE row lock).
        """
        result = await session.execute(
            select(ButlerAction)
            .where(ButlerAction.id == action_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def update_status(
        session: AsyncSession,
        action_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        error_code: str | None = None,
        error_context: dict | None = None,
        result_payload: dict | None = None,
        result_payload_hash: str | None = None,
        inverse_op_payload: dict | None = None,
        confirmed_at: datetime | None = None,
        executed_at: datetime | None = None,
        undone_at: datetime | None = None,
        llm_usage_ledger_id: int | None = None,
    ) -> int:
        """UPDATE status + optional fields. Returns rowcount (should be 1).

        Flushes; caller commits. Raises LookupError if row not found.
        """
        values: dict = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if rejection_reason is not None:
            values["rejection_reason"] = rejection_reason
        if error_code is not None:
            values["error_code"] = error_code
        if error_context is not None:
            values["error_context"] = error_context
        if result_payload is not None:
            values["result_payload"] = result_payload
        if result_payload_hash is not None:
            values["result_payload_hash"] = result_payload_hash
        if inverse_op_payload is not None:
            values["inverse_op_payload"] = inverse_op_payload
        if confirmed_at is not None:
            values["confirmed_at"] = confirmed_at
        if executed_at is not None:
            values["executed_at"] = executed_at
        if undone_at is not None:
            values["undone_at"] = undone_at
        if llm_usage_ledger_id is not None:
            values["llm_usage_ledger_id"] = llm_usage_ledger_id

        stmt = (
            update(ButlerAction)
            .where(ButlerAction.id == action_id)
            .values(**values)
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"ButlerAction(id={action_id}) not found — row missing"
            )
        await session.flush()
        return rowcount

    @staticmethod
    async def list_by_requester(
        session: AsyncSession,
        requester_tg_id: int,
        *,
        limit: int = 20,
    ) -> list[ButlerAction]:
        """Return the most recent N actions for a requester (newest first)."""
        result = await session.execute(
            select(ButlerAction)
            .where(ButlerAction.requester_tg_id == requester_tg_id)
            .order_by(ButlerAction.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_pending_for_chat(
        session: AsyncSession,
        chat_id: int,
    ) -> list[ButlerAction]:
        """Return all pending_confirmation actions for a chat (oldest first)."""
        result = await session.execute(
            select(ButlerAction)
            .where(
                ButlerAction.chat_id == chat_id,
                ButlerAction.status == "pending_confirmation",
            )
            .order_by(ButlerAction.created_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_expired_past_ttl(
        session: AsyncSession,
        now: datetime | None = None,
    ) -> int:
        """Expire all pending_confirmation/planned rows past their expires_at.

        Returns count of rows expired. Flushes; caller commits.
        """
        cutoff = now if now is not None else datetime.now(timezone.utc)
        stmt = (
            update(ButlerAction)
            .where(
                ButlerAction.status.in_(["pending_confirmation", "planned"]),
                ButlerAction.expires_at <= cutoff,
            )
            .values(
                status="expired",
                rejection_reason="ttl_expired",
                updated_at=datetime.now(timezone.utc),
            )
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount:
            await session.flush()
        _log.debug("butler_actions: expired %d rows past TTL", rowcount)
        return rowcount
