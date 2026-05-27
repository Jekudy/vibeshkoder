"""Repository for ``butler_card_suggestions`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.
"""

from __future__ import annotations

import logging
import uuid

from datetime import datetime, timezone

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

    @staticmethod
    async def dismiss_by_undo(
        session: AsyncSession,
        butler_action_id: int,
        *,
        reviewer_user_id: int,
    ) -> int:
        """Dismiss a pending card suggestion by /butler_undo (T12-07).

        Rejects the associated extraction_candidate (transitions from 'pending' to
        'rejected') for the suggestion linked to butler_action_id, marking it as
        withdrawn by the undo flow. This is the 'cancel_pending' rollback_kind path.

        C5 fix: sets reviewed_by + reviewed_at to satisfy
        ck_extraction_candidates_reviewer_consistency CHECK constraint.
        Mirrors ExtractionCandidateRepo.mark_status pattern.

        Looks up the butler_card_suggestions row, then rejects the linked
        extraction_candidate if one exists and is still pending.

        Returns count of extraction_candidates updated (0 if none found or already terminal).
        Flushes; caller commits.
        """
        from sqlalchemy import update as sa_update

        from bot.db.models import ExtractionCandidate

        # Find the suggestion
        result = await session.execute(
            select(ButlerCardSuggestion).where(
                ButlerCardSuggestion.butler_action_id == butler_action_id
            )
        )
        suggestion = result.scalar_one_or_none()
        if suggestion is None or suggestion.extraction_candidate_id is None:
            _log.debug(
                "butler_card_suggestions: dismiss_by_undo — no suggestion or no candidate "
                "for action_id=%s",
                butler_action_id,
            )
            return 0

        # Reject the linked extraction_candidate if still pending.
        # Must set reviewed_by + reviewed_at to satisfy ck_extraction_candidates_reviewer_consistency.
        stmt = (
            sa_update(ExtractionCandidate)
            .where(
                ExtractionCandidate.id == suggestion.extraction_candidate_id,
                ExtractionCandidate.status == "pending",
            )
            .values(
                status="rejected",
                reviewed_by=reviewer_user_id,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        update_result = await session.execute(stmt)
        rowcount: int = update_result.rowcount
        if rowcount > 0:
            await session.flush()
            _log.debug(
                "butler_card_suggestions: dismiss_by_undo — rejected candidate %s "
                "for action_id=%s by reviewer_user_id=%s",
                suggestion.extraction_candidate_id,
                butler_action_id,
                reviewer_user_id,
            )
        return rowcount
