"""Repository for ``extraction_candidates`` (T6-04 / PHASE6_PLAN.md §5.C).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (handler) owns the transaction lifecycle (matches the qa_trace +
forget_event repo pattern).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ExtractionCandidate


class ExtractionCandidateRepo:
    @staticmethod
    async def list_pending(
        session: AsyncSession,
        *,
        limit: int,
        offset: int,
    ) -> list[ExtractionCandidate]:
        """Return a page of pending candidates ordered newest-first.

        Order: ``created_at DESC, id DESC`` per T6-04 design §4. The id tie
        break keeps pagination deterministic across pages.
        """
        stmt = (
            select(ExtractionCandidate)
            .where(ExtractionCandidate.status == "pending")
            .order_by(
                ExtractionCandidate.created_at.desc(),
                ExtractionCandidate.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id_for_update(
        session: AsyncSession,
        candidate_id: uuid.UUID,
    ) -> ExtractionCandidate | None:
        """Lock the candidate row for the lifetime of the current transaction.

        Step 1 of the §5.C 8-step protocol — ``SELECT ... FOR UPDATE`` prevents
        a concurrent admin from racing the same ``/approve`` or ``/reject``.

        Returns ``None`` if the candidate does not exist (caller decides
        whether to raise or render a user-facing error).
        """
        stmt = (
            select(ExtractionCandidate)
            .where(ExtractionCandidate.id == candidate_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def mark_status(
        session: AsyncSession,
        *,
        candidate_id: uuid.UUID,
        status: str,
        reviewed_by: int,
    ) -> None:
        """Flip status + populate reviewer audit columns atomically.

        The DB ``ck_extraction_candidates_reviewer_consistency`` check requires
        both ``reviewed_by`` AND ``reviewed_at`` to be set when the status is
        any of ``approved`` / ``rejected`` / ``superseded``.
        """
        stmt = (
            update(ExtractionCandidate)
            .where(ExtractionCandidate.id == candidate_id)
            .values(
                status=status,
                reviewed_by=reviewed_by,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        await session.execute(stmt)
        await session.flush()
