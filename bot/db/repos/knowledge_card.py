"""Repository for ``knowledge_cards`` (T6-04 / T6-05).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller (handler) owns the transaction lifecycle.
"""

from __future__ import annotations

import uuid

from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import String

from bot.db.models import KnowledgeCard


class KnowledgeCardRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        title: str,
        body_markdown: str,
        approved_by_user_id: int,
        topic_slug: str | None = None,
    ) -> KnowledgeCard:
        """Insert an approved card with audit columns populated.

        Step 5 of the §5.C ``/approve`` 8-step protocol. The DB
        ``ck_knowledge_cards_approved_attribution`` check requires both
        ``approved_by_user_id`` AND ``approved_at`` set when
        ``card_status='approved'`` — both are written here.
        """
        card = KnowledgeCard(
            topic_slug=topic_slug,
            title=title,
            body_markdown=body_markdown,
            card_status="approved",
            approved_by_user_id=approved_by_user_id,
            approved_at=func.now(),
        )
        session.add(card)
        await session.flush()
        await session.refresh(card)
        return card

    @staticmethod
    async def list_approved(
        session: AsyncSession,
        *,
        limit: int,
        offset: int,
    ) -> list[KnowledgeCard]:
        """Page of approved cards ordered newest-approval first.

        Filter ``card_status='approved'`` — draft/archived hidden from default
        browse (T6-05 design §2).
        """
        stmt = (
            select(KnowledgeCard)
            .where(KnowledgeCard.card_status == "approved")
            .order_by(
                KnowledgeCard.approved_at.desc(),
                KnowledgeCard.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        card_id: uuid.UUID,
    ) -> KnowledgeCard | None:
        """Exact-id lookup. Approved-only — draft/archived hidden.

        Privacy: filter at SQL layer so no draft/archived metadata leaks
        even if the caller forgets to post-check. Codex round 2 MED #1.
        """
        stmt = (
            select(KnowledgeCard)
            .where(KnowledgeCard.id == card_id)
            .where(KnowledgeCard.card_status == "approved")
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id_prefix(
        session: AsyncSession,
        prefix: str,
    ) -> list[KnowledgeCard]:
        """Prefix lookup for short-UUID resolution. Approved-only.

        Returns up to 2 approved rows so the caller can detect ambiguity
        without scanning the full table. Empty/very-short prefix returns
        the first 2 matches in arbitrary order — admins should use longer
        prefixes in practice.

        Privacy: SQL-layer filter on ``card_status='approved'``. Codex
        round 2 MED #1.
        """
        stmt = (
            select(KnowledgeCard)
            .where(cast(KnowledgeCard.id, String).like(f"{prefix}%"))
            .where(KnowledgeCard.card_status == "approved")
            .limit(2)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
