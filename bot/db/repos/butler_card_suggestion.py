"""Repository for ``butler_card_suggestions`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update

from bot.db.models import ButlerCardSuggestion
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


class ButlerCardSuggestionRepo:
    """Data-access layer for ``butler_card_suggestions``."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        butler_action_id: int,
        suggested_card_payload: dict,
        created_by_user_id: int,
        extraction_candidate_id: uuid.UUID | None = None,
    ) -> ButlerCardSuggestion:
        """Insert a new butler_card_suggestions row. Flushes; caller commits."""
        row = ButlerCardSuggestion(
            butler_action_id=butler_action_id,
            suggested_card_payload=suggested_card_payload,
            created_by_user_id=created_by_user_id,
            extraction_candidate_id=extraction_candidate_id,
        )
        session.add(row)
        await session.flush()
        _log.debug(
            "butler_card_suggestions: inserted row id=%s butler_action_id=%s",
            row.id,
            butler_action_id,
        )
        return row

    @staticmethod
    async def link_to_extraction_candidate(
        session: AsyncSession,
        suggestion_id: int,
        extraction_candidate_id: uuid.UUID,
    ) -> int:
        """Set extraction_candidate_id on an existing suggestion row.

        Returns rowcount (should be 1). Raises LookupError if not found.
        Flushes; caller commits.
        """
        stmt = (
            update(ButlerCardSuggestion)
            .where(ButlerCardSuggestion.id == suggestion_id)
            .values(extraction_candidate_id=extraction_candidate_id)
        )
        result = await session.execute(stmt)
        rowcount: int = result.rowcount
        if rowcount == 0:
            raise LookupError(
                f"ButlerCardSuggestion(id={suggestion_id}) not found"
            )
        await session.flush()
        return rowcount

    @staticmethod
    async def get_for_action(
        session: AsyncSession,
        butler_action_id: int,
    ) -> ButlerCardSuggestion | None:
        """Fetch the suggestion for a given butler_action_id. Returns None if not found."""
        result = await session.execute(
            select(ButlerCardSuggestion).where(
                ButlerCardSuggestion.butler_action_id == butler_action_id
            )
        )
        return result.scalar_one_or_none()
