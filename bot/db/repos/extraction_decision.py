"""Repository for ``extraction_decisions`` (T6-04 / PHASE6_PLAN §5.C step 8).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (handler) owns the transaction lifecycle.

The DB ``UNIQUE(candidate_id)`` ensures exactly one terminal decision per
candidate. R3-block aborts in ``/approve`` MUST NOT write a row here — they
are precondition failures, not decisions (PHASE6_PLAN §5.C / §8).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ExtractionDecision


class ExtractionDecisionRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        candidate_id: uuid.UUID,
        action: str,
        decided_by: int | None,
        decided_by_username: str,
        reason: str | None,
    ) -> ExtractionDecision:
        """Insert an audit row for an admin terminal decision.

        ``decided_by_username`` is a NOT-NULL audit shadow — it survives
        ``decided_by`` going NULL on user soft-delete (FK SET NULL). Handler
        must always supply a non-empty string; ``f"tg{user.id}"`` is the
        agreed fallback when ``users.username`` is NULL (T6-04 design §2
        step 8).
        """
        row = ExtractionDecision(
            candidate_id=candidate_id,
            action=action,
            decided_by=decided_by,
            decided_by_username=decided_by_username,
            reason=reason,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row
