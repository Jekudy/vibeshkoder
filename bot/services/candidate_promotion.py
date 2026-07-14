"""Atomic, idempotent extraction-candidate promotion."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ExtractionCandidate, User
from bot.db.repos.card_source import CardSourceRepo
from bot.db.repos.extraction_candidate import ExtractionCandidateRepo
from bot.db.repos.extraction_decision import ExtractionDecisionRepo
from bot.db.repos.knowledge_card import KnowledgeCardRepo
from bot.services.extraction_schema import (
    CandidateValidationError,
    EXTRACTION_CANDIDATE_SCHEMA_VERSION,
    ValidatedExtractionCandidate,
    validate_candidate_envelope,
)
from bot.services.forget_cascade import _p6_mvid_advisory_lock_id
from bot.services.governance_revalidation import revalidate_sources

logger = logging.getLogger(__name__)

MAX_PENDING_PROMOTION_BATCH = 100
PromotionStatus = Literal["promoted", "already_promoted", "blocked"]
_PROMOTION_BATCH_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"p13:candidate_promotion_batch").digest()[:8],
    "big",
    signed=True,
)


class ActorUserNotFoundError(ValueError):
    """The explicitly configured automatic actor does not exist in users."""


class LegacyPendingCandidatesError(RuntimeError):
    """Automatic promotion found unversioned payloads it cannot classify."""

    def __init__(self, count: int) -> None:
        super().__init__(f"automatic promotion blocked by {count} legacy pending candidate(s)")
        self.count = count


@dataclass(frozen=True)
class CandidatePromotionResult:
    candidate_id: uuid.UUID
    status: PromotionStatus
    card_id: uuid.UUID | None = None
    reason: str | None = None


async def _require_actor(session: AsyncSession, actor_user_id: int) -> User:
    if type(actor_user_id) is not int or actor_user_id <= 0:
        raise ActorUserNotFoundError("actor_user_id must identify an existing user")
    actor = await session.get(User, actor_user_id)
    if actor is None:
        raise ActorUserNotFoundError(f"automatic promotion actor {actor_user_id} does not exist")
    return actor


def _validate_candidate(candidate: ExtractionCandidate) -> ValidatedExtractionCandidate:
    raw_source_ids = candidate.source_message_version_ids
    allowed_source_ids = (
        [source_id for source_id in raw_source_ids if type(source_id) is int]
        if isinstance(raw_source_ids, list)
        else []
    )
    return validate_candidate_envelope(
        {
            "candidate_json": candidate.candidate_json,
            "source_message_version_ids": raw_source_ids,
        },
        allowed_source_message_version_ids=allowed_source_ids,
    )


async def _acquire_source_locks(
    session: AsyncSession,
    source_message_version_ids: tuple[int, ...],
) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    lock_ids = sorted(_p6_mvid_advisory_lock_id(mvid) for mvid in source_message_version_ids)
    for lock_id in lock_ids:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )


async def _acquire_batch_lock(session: AsyncSession) -> None:
    """Serialize multi-candidate batches whose xact locks accumulate."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _PROMOTION_BATCH_LOCK_ID},
    )


async def _supersede_pending_candidate(
    session: AsyncSession,
    *,
    candidate: ExtractionCandidate,
    actor_user_id: int,
) -> None:
    """Terminalize a deterministic non-promotable candidate without a decision."""
    if candidate.status != "pending":
        return
    await ExtractionCandidateRepo.mark_status(
        session,
        candidate_id=candidate.id,
        status="superseded",
        reviewed_by=actor_user_id,
    )


async def _require_no_legacy_pending_candidates(
    session: AsyncSession,
    *,
    extraction_run_id: uuid.UUID | None = None,
) -> None:
    statement = select(func.count(ExtractionCandidate.id)).where(
        ExtractionCandidate.status == "pending",
        ExtractionCandidate.payload_schema_version.is_(None),
    )
    if extraction_run_id is not None:
        statement = statement.where(ExtractionCandidate.extraction_run_id == extraction_run_id)
    count = int(await session.scalar(statement) or 0)
    if count:
        raise LegacyPendingCandidatesError(count)


async def promote_candidate(
    session: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    actor_user_id: int,
) -> CandidatePromotionResult:
    """Promote one candidate exactly once inside the caller's transaction.

    Lock order matches the forget cascade and the legacy manual handler:
    source advisory locks first, then the candidate row lock, then source
    governance ``FOR SHARE`` reads. The function is flush-only; its caller owns
    commit/rollback.
    """
    actor = await _require_actor(session, actor_user_id)
    initial = await ExtractionCandidateRepo.get_by_id(session, candidate_id)
    if initial is None:
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="candidate_missing",
        )
    if initial.status != "pending":
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status=("already_promoted" if initial.status == "approved" else "blocked"),
            reason=(None if initial.status == "approved" else f"already_{initial.status}"),
        )
    if initial.payload_schema_version is None:
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="legacy_candidate_requires_reextract",
        )
    if initial.payload_schema_version != EXTRACTION_CANDIDATE_SCHEMA_VERSION:
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="unsupported_candidate_schema",
        )

    try:
        initial_validated = _validate_candidate(initial)
    except CandidateValidationError:
        # There may be no trustworthy source set from which to derive advisory
        # lock keys. Row-lock and terminalize the malformed staging record so
        # bounded recovery sweeps cannot starve behind it forever.
        malformed = await ExtractionCandidateRepo.get_by_id_for_update(session, candidate_id)
        if malformed is not None and malformed.status == "pending":
            try:
                _validate_candidate(malformed)
            except CandidateValidationError:
                await _supersede_pending_candidate(
                    session,
                    candidate=malformed,
                    actor_user_id=actor_user_id,
                )
            else:
                return CandidatePromotionResult(
                    candidate_id=candidate_id,
                    status="blocked",
                    reason="candidate_changed_during_promotion",
                )
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="invalid_candidate_json",
        )

    await _acquire_source_locks(
        session,
        initial_validated.source_message_version_ids,
    )
    candidate = await ExtractionCandidateRepo.get_by_id_for_update(session, candidate_id)
    if candidate is None:
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="candidate_missing",
        )
    if candidate.status != "pending":
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status=("already_promoted" if candidate.status == "approved" else "blocked"),
            reason=(None if candidate.status == "approved" else f"already_{candidate.status}"),
        )
    if candidate.payload_schema_version != EXTRACTION_CANDIDATE_SCHEMA_VERSION:
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="candidate_schema_changed_during_promotion",
        )

    try:
        validated = _validate_candidate(candidate)
    except CandidateValidationError:
        await _supersede_pending_candidate(
            session,
            candidate=candidate,
            actor_user_id=actor_user_id,
        )
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="invalid_candidate_json",
        )
    if validated.source_message_version_ids != initial_validated.source_message_version_ids:
        # Candidate source sets are immutable after extraction. Failing closed
        # avoids acquiring new locks out of the global sorted order.
        await _supersede_pending_candidate(
            session,
            candidate=candidate,
            actor_user_id=actor_user_id,
        )
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason="candidate_changed_during_promotion",
        )

    source_ids = list(validated.source_message_version_ids)
    governance_status, governance_payload = await revalidate_sources(session, source_ids)
    if governance_status == "blocked":
        reason = str((governance_payload or {}).get("failure_reason") or "source_blocked")
        logger.warning(
            "automatic_candidate_promotion_blocked",
            extra={
                "candidate_id": str(candidate_id),
                "actor_user_id": actor_user_id,
                "reason": reason,
            },
        )
        await _supersede_pending_candidate(
            session,
            candidate=candidate,
            actor_user_id=actor_user_id,
        )
        return CandidatePromotionResult(
            candidate_id=candidate_id,
            status="blocked",
            reason=reason,
        )

    card = await KnowledgeCardRepo.create(
        session,
        topic_slug=validated.topic_slug,
        title=validated.title,
        body_markdown=validated.body_markdown,
        approved_by_user_id=actor_user_id,
    )
    await CardSourceRepo.bulk_create(
        session,
        card_id=card.id,
        message_version_ids=source_ids,
    )
    await ExtractionCandidateRepo.mark_status(
        session,
        candidate_id=candidate_id,
        status="approved",
        reviewed_by=actor_user_id,
    )
    decided_by_username = actor.username or f"tg{actor_user_id}"
    await ExtractionDecisionRepo.create(
        session,
        candidate_id=candidate_id,
        action="approved",
        decided_by=actor_user_id,
        decided_by_username=decided_by_username,
        reason=None,
    )
    return CandidatePromotionResult(
        candidate_id=candidate_id,
        status="promoted",
        card_id=card.id,
    )


async def promote_run_candidates(
    session: AsyncSession,
    *,
    extraction_run_id: uuid.UUID,
    actor_user_id: int,
) -> list[CandidatePromotionResult]:
    """Promote all still-pending candidates emitted by one extraction run."""
    await _require_actor(session, actor_user_id)
    await _acquire_batch_lock(session)
    await _require_no_legacy_pending_candidates(
        session,
        extraction_run_id=extraction_run_id,
    )
    candidate_ids = list(
        (
            await session.scalars(
                select(ExtractionCandidate.id)
                .where(
                    ExtractionCandidate.extraction_run_id == extraction_run_id,
                    ExtractionCandidate.status == "pending",
                )
                .order_by(
                    ExtractionCandidate.created_at.asc(),
                    ExtractionCandidate.id.asc(),
                )
            )
        ).all()
    )
    results: list[CandidatePromotionResult] = []
    for candidate_id in candidate_ids:
        results.append(
            await promote_candidate(
                session,
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
        )
    return results


async def promote_pending_candidates(
    session: AsyncSession,
    *,
    actor_user_id: int,
    limit: int,
) -> list[CandidatePromotionResult]:
    """Bounded crash-recovery sweep over pending candidates.

    Candidate ids are selected without a batch row lock, then handed to the
    exactly-once primitive. This preserves the source-advisory-lock → candidate
    row-lock order and remains safe with multiple concurrent recovery workers.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PENDING_PROMOTION_BATCH:
        raise ValueError(f"limit must be an integer from 1 to {MAX_PENDING_PROMOTION_BATCH}")
    await _require_actor(session, actor_user_id)
    await _acquire_batch_lock(session)
    await _require_no_legacy_pending_candidates(session)
    candidate_ids = list(
        (
            await session.scalars(
                select(ExtractionCandidate.id)
                .where(ExtractionCandidate.status == "pending")
                .order_by(
                    ExtractionCandidate.created_at.asc(),
                    ExtractionCandidate.id.asc(),
                )
                .limit(limit)
            )
        ).all()
    )
    results: list[CandidatePromotionResult] = []
    for candidate_id in candidate_ids:
        results.append(
            await promote_candidate(
                session,
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
            )
        )
    return results


__all__ = [
    "ActorUserNotFoundError",
    "CandidatePromotionResult",
    "LegacyPendingCandidatesError",
    "MAX_PENDING_PROMOTION_BATCH",
    "promote_candidate",
    "promote_pending_candidates",
    "promote_run_candidates",
]
