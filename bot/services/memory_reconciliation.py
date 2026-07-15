"""Explicit, append-only reconciliation for ambiguous paid memory calls."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import (
    ExtractionRun,
    ExtractionRunResolution,
    ImageDescriptionResolution,
    MessageMedia,
    User,
)
from bot.services.extractor import (
    _advance_extraction_cursor,
    _factory_engine,
    _semantic_lock_id,
)


logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EXTRACTION_ACTIONS = frozenset({"safe_retry", "risk_accepted_retry", "abandon"})
_IMAGE_ACTIONS = frozenset({"risk_accepted_retry", "abandon"})
_SAFE_EXTRACTION_STATES = frozenset({"not_dispatched", "rejected_pre_accept"})
_AMBIGUOUS_EXTRACTION_STATES = frozenset({"unknown", "response_received"})


class MemoryReconciliationError(RuntimeError):
    """The requested reconciliation is invalid, unsafe, or already decided."""


@dataclass(frozen=True)
class MemoryReconciliationResult:
    target_type: str
    target_id: str
    action: str


def _validate_common(
    *,
    actor_user_id: int,
    reason: str,
    evidence_hash: str | None,
) -> tuple[str, str | None]:
    if type(actor_user_id) is not int or actor_user_id <= 0:
        raise MemoryReconciliationError("actor_user_id must be a positive existing user id")
    if not isinstance(reason, str):
        raise MemoryReconciliationError("reason must contain 1 to 500 characters")
    normalized_reason = reason.strip()
    if not 1 <= len(normalized_reason) <= 500:
        raise MemoryReconciliationError("reason must contain 1 to 500 characters")
    if evidence_hash is not None:
        if not isinstance(evidence_hash, str) or _SHA256_RE.fullmatch(evidence_hash) is None:
            raise MemoryReconciliationError("evidence_hash must be a lowercase SHA-256 digest")
    return normalized_reason, evidence_hash


async def _require_actor(session: AsyncSession, actor_user_id: int) -> None:
    if await session.get(User, actor_user_id) is None:
        raise MemoryReconciliationError("actor_user_id must identify an existing user")


async def reconcile_extraction_run(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    action: str,
    actor_user_id: int,
    reason: str,
    evidence_hash: str | None = None,
    accept_possible_duplicate_cost: bool = False,
    accept_memory_gap: bool = False,
) -> MemoryReconciliationResult:
    """Record exactly one decision for a non-completed semantic extraction run.

    Retry decisions do not mutate the old run or ledger. The next identical
    scheduler/backfill call creates the linked attempt under the same semantic
    advisory lock. Cursor abandonment advances the durable watermark in this
    same transaction, while preserving the original run status for audit.
    """

    if not isinstance(run_id, uuid.UUID):
        raise MemoryReconciliationError("run_id must be a UUID")
    if action not in _EXTRACTION_ACTIONS:
        raise MemoryReconciliationError("unsupported extraction reconciliation action")
    normalized_reason, normalized_evidence = _validate_common(
        actor_user_id=actor_user_id,
        reason=reason,
        evidence_hash=evidence_hash,
    )

    async with session_factory() as lookup_session:
        semantic_key = await lookup_session.scalar(
            select(ExtractionRun.semantic_key).where(ExtractionRun.id == run_id)
        )
    if semantic_key is None:
        raise MemoryReconciliationError("run is missing or has no semantic identity")

    engine = _factory_engine(session_factory)
    async with engine.connect() as lock_connection:
        async with lock_connection.begin():
            await lock_connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _semantic_lock_id(semantic_key)},
            )
            async with session_factory() as session, session.begin():
                run = await session.scalar(
                    select(ExtractionRun).where(ExtractionRun.id == run_id).with_for_update()
                )
                if run is None or run.semantic_key != semantic_key:
                    raise MemoryReconciliationError("extraction run changed or disappeared")
                if run.run_status == "completed":
                    raise MemoryReconciliationError("completed extraction runs cannot reconcile")
                existing = await session.scalar(
                    select(ExtractionRunResolution).where(ExtractionRunResolution.run_id == run_id)
                )
                if existing is not None:
                    raise MemoryReconciliationError("extraction run is already reconciled")
                await _require_actor(session, actor_user_id)

                if action == "safe_retry":
                    if run.dispatch_state not in _SAFE_EXTRACTION_STATES:
                        raise MemoryReconciliationError(
                            "safe_retry requires not_dispatched or rejected_pre_accept"
                        )
                    if accept_possible_duplicate_cost or accept_memory_gap:
                        raise MemoryReconciliationError(
                            "safe_retry does not accept duplicate-cost or memory-gap flags"
                        )
                elif action == "risk_accepted_retry":
                    if run.dispatch_state not in _AMBIGUOUS_EXTRACTION_STATES:
                        raise MemoryReconciliationError(
                            "risk_accepted_retry requires unknown or response_received"
                        )
                    if not accept_possible_duplicate_cost:
                        raise MemoryReconciliationError(
                            "risk_accepted_retry requires --accept-possible-duplicate-cost"
                        )
                    if accept_memory_gap:
                        raise MemoryReconciliationError(
                            "risk_accepted_retry does not accept --accept-memory-gap"
                        )
                else:
                    if not accept_memory_gap:
                        raise MemoryReconciliationError("abandon requires --accept-memory-gap")
                    if accept_possible_duplicate_cost:
                        raise MemoryReconciliationError(
                            "abandon does not accept --accept-possible-duplicate-cost"
                        )

                session.add(
                    ExtractionRunResolution(
                        run_id=run.id,
                        action=action,
                        actor_user_id=actor_user_id,
                        reason=normalized_reason,
                        evidence_hash=normalized_evidence,
                        accept_memory_gap=accept_memory_gap,
                    )
                )
                if action == "abandon" and run.selection_mode == "version_cursor":
                    if run.source_chat_id is None or run.cursor_end_message_version_id is None:
                        raise MemoryReconciliationError(
                            "cursor extraction run is missing its durable cursor bounds"
                        )
                    await _advance_extraction_cursor(
                        session,
                        source_chat_id=run.source_chat_id,
                        through_message_version_id=run.cursor_end_message_version_id,
                    )

    logger.info(
        "extraction_run_reconciled",
        extra={
            "extraction_run_id": str(run_id),
            "actor_user_id": actor_user_id,
            "action": action,
        },
    )
    return MemoryReconciliationResult(
        target_type="extraction_run",
        target_id=str(run_id),
        action=action,
    )


async def reconcile_image_description(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    message_media_id: int,
    action: str,
    actor_user_id: int,
    reason: str,
    evidence_hash: str | None = None,
    accept_possible_duplicate_cost: bool = False,
    accept_memory_gap: bool = False,
) -> MemoryReconciliationResult:
    """Resolve the current ambiguous claim once without changing its old ledger."""

    if type(message_media_id) is not int or message_media_id <= 0:
        raise MemoryReconciliationError("message_media_id must be a positive integer")
    if action not in _IMAGE_ACTIONS:
        raise MemoryReconciliationError("unsupported image reconciliation action")
    normalized_reason, normalized_evidence = _validate_common(
        actor_user_id=actor_user_id,
        reason=reason,
        evidence_hash=evidence_hash,
    )

    async with session_factory() as session, session.begin():
        media = await session.scalar(
            select(MessageMedia).where(MessageMedia.id == message_media_id).with_for_update()
        )
        if media is None:
            raise MemoryReconciliationError("message media row does not exist")
        if media.description_status != "processing":
            raise MemoryReconciliationError(
                "only an ambiguous processing image description can reconcile"
            )
        attempt_no = media.description_attempts
        if attempt_no < 1:
            raise MemoryReconciliationError("processing image claim has no attempt identity")
        existing = await session.scalar(
            select(ImageDescriptionResolution).where(
                ImageDescriptionResolution.message_media_id == message_media_id,
                ImageDescriptionResolution.attempt_no == attempt_no,
            )
        )
        if existing is not None:
            raise MemoryReconciliationError(
                "current image description attempt is already reconciled"
            )
        await _require_actor(session, actor_user_id)

        if action == "risk_accepted_retry":
            if not accept_possible_duplicate_cost:
                raise MemoryReconciliationError(
                    "risk_accepted_retry requires --accept-possible-duplicate-cost"
                )
            if accept_memory_gap:
                raise MemoryReconciliationError(
                    "risk_accepted_retry does not accept --accept-memory-gap"
                )
            media.description_status = "pending"
            media.next_attempt_at = None
            media.last_error_code = "operator_risk_accepted_retry"
        else:
            if not accept_memory_gap:
                raise MemoryReconciliationError("abandon requires --accept-memory-gap")
            if accept_possible_duplicate_cost:
                raise MemoryReconciliationError(
                    "abandon does not accept --accept-possible-duplicate-cost"
                )
            media.description = None
            media.description_status = "failed"
            media.next_attempt_at = None
            media.last_error_code = "operator_abandoned_ambiguous"

        media.description_claim_token = None
        media.description_claimed_at = None
        session.add(
            ImageDescriptionResolution(
                message_media_id=media.id,
                attempt_no=attempt_no,
                action=action,
                actor_user_id=actor_user_id,
                reason=normalized_reason,
                evidence_hash=normalized_evidence,
                accept_memory_gap=accept_memory_gap,
            )
        )

    logger.info(
        "image_description_reconciled",
        extra={
            "message_media_id": message_media_id,
            "actor_user_id": actor_user_id,
            "action": action,
        },
    )
    return MemoryReconciliationResult(
        target_type="message_media",
        target_id=str(message_media_id),
        action=action,
    )


__all__ = [
    "MemoryReconciliationError",
    "MemoryReconciliationResult",
    "reconcile_extraction_run",
    "reconcile_image_description",
]
