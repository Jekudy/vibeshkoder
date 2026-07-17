"""Durable two-per-Moscow-day semantic Q&A admission state machine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import SemanticQaAttempt, SemanticRetrievalTrace
from bot.services.qa_guardrails import DAILY_LLM_QUESTION_LIMIT, MOSCOW_TZ, moscow_day_bounds_utc


AttemptOutcome = Literal["answered", "abstained", "technical_failure"]


@dataclass(frozen=True, slots=True)
class SemanticQuotaDecision:
    allowed: bool
    attempt_id: int
    used: int
    limit: int
    resets_at: datetime
    replayed: bool = False
    status: str | None = None
    outcome: str | None = None
    qa_trace_id: int | None = None


def _user_lock_id(user_tg_id: int) -> int:
    payload = f"semantic_qa_user:{user_tg_id}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


class SemanticQuotaRepo:
    STALE_RESERVATION_AFTER = timedelta(minutes=15)

    @staticmethod
    async def reserve(
        session: AsyncSession,
        *,
        idempotency_key: str,
        user_tg_id: int,
        chat_id: int,
        source_chat_message_id: int | None,
        now: datetime | None = None,
        limit: int = DAILY_LLM_QUESTION_LIMIT,
    ) -> SemanticQuotaDecision:
        """Reserve one slot atomically; caller must commit before providers."""

        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("semantic quota idempotency_key must contain 1..128 characters")
        if limit != DAILY_LLM_QUESTION_LIMIT:
            raise ValueError("semantic Q&A daily limit is fixed at two")
        effective_now = now or datetime.now(timezone.utc)
        start_utc, end_utc = moscow_day_bounds_utc(effective_now)
        local_day = start_utc.astimezone(MOSCOW_TZ).date()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _user_lock_id(user_tg_id)},
        )
        await SemanticQuotaRepo.release_stale_reserved(
            session,
            user_tg_id=user_tg_id,
            through_local_day=local_day,
            stale_before=effective_now - SemanticQuotaRepo.STALE_RESERVATION_AFTER,
            finalized_at=effective_now,
        )

        existing_result = await session.execute(
            select(SemanticQaAttempt).where(SemanticQaAttempt.idempotency_key == idempotency_key)
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            return SemanticQuotaDecision(
                # Telegram retries reuse the same source message. They must
                # never dispatch a second paid provider call, including while
                # the original attempt is still reserved.
                allowed=False,
                attempt_id=existing.id,
                used=await SemanticQuotaRepo._active_count(
                    session,
                    user_tg_id=user_tg_id,
                    local_day=local_day,
                ),
                limit=limit,
                resets_at=end_utc,
                replayed=True,
                status=existing.status,
                outcome=existing.outcome,
                qa_trace_id=existing.qa_trace_id,
            )

        active_result = await session.execute(
            select(SemanticQaAttempt.slot_number)
            .where(
                SemanticQaAttempt.user_tg_id == user_tg_id,
                SemanticQaAttempt.local_day == local_day,
                SemanticQaAttempt.status.in_(("reserved", "consumed")),
            )
            .order_by(SemanticQaAttempt.slot_number.asc())
        )
        used_slots = {int(slot) for slot in active_result.scalars() if slot is not None}
        available_slot = next((slot for slot in (1, 2) if slot not in used_slots), None)
        if available_slot is None:
            statement = (
                pg_insert(SemanticQaAttempt)
                .values(
                    idempotency_key=idempotency_key,
                    user_tg_id=user_tg_id,
                    chat_id=chat_id,
                    source_chat_message_id=source_chat_message_id,
                    local_day=local_day,
                    slot_number=None,
                    status="denied",
                    outcome="quota_denied",
                )
                .returning(SemanticQaAttempt.id)
            )
            denied_result = await session.execute(statement)
            attempt_id = int(denied_result.scalar_one())
            return SemanticQuotaDecision(
                allowed=False,
                attempt_id=attempt_id,
                used=len(used_slots),
                limit=limit,
                resets_at=end_utc,
                status="denied",
                outcome="quota_denied",
            )

        statement = (
            pg_insert(SemanticQaAttempt)
            .values(
                idempotency_key=idempotency_key,
                user_tg_id=user_tg_id,
                chat_id=chat_id,
                source_chat_message_id=source_chat_message_id,
                local_day=local_day,
                slot_number=available_slot,
                status="reserved",
                outcome=None,
            )
            .returning(SemanticQaAttempt.id)
        )
        reserved_result = await session.execute(statement)
        attempt_id = int(reserved_result.scalar_one())
        return SemanticQuotaDecision(
            allowed=True,
            attempt_id=attempt_id,
            used=len(used_slots),
            limit=limit,
            resets_at=end_utc,
            status="reserved",
        )

    @staticmethod
    async def release_stale_reserved(
        session: AsyncSession,
        *,
        user_tg_id: int,
        through_local_day: date,
        stale_before: datetime,
        finalized_at: datetime | None = None,
    ) -> int:
        """Reconcile crashed reservations through the current Moscow day.

        A durable delivery intent is conservatively consumed: Telegram may
        have accepted the message even if the process died before the final DB
        commit. Reservations that never reached delivery are released. The
        user-scoped admission lock makes reconciliation atomic across the day
        boundary and prevents an old idempotent replay from remaining reserved.
        """

        effective_finalized_at = finalized_at or datetime.now(timezone.utc)
        delivered_result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.user_tg_id == user_tg_id,
                SemanticQaAttempt.local_day <= through_local_day,
                SemanticQaAttempt.status == "reserved",
                SemanticQaAttempt.progress_at < stale_before,
                SemanticQaAttempt.delivery_started_at.is_not(None),
            )
            .values(
                status="consumed",
                finalized_at=effective_finalized_at,
            )
        )
        released_result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.user_tg_id == user_tg_id,
                SemanticQaAttempt.local_day <= through_local_day,
                SemanticQaAttempt.status == "reserved",
                SemanticQaAttempt.progress_at < stale_before,
                SemanticQaAttempt.delivery_started_at.is_(None),
            )
            .values(
                status="released",
                outcome="technical_failure",
                finalized_at=effective_finalized_at,
            )
        )
        await session.flush()
        return int(getattr(delivered_result, "rowcount", 0) or 0) + int(
            getattr(released_result, "rowcount", 0) or 0
        )

    @staticmethod
    async def mark_delivery_started(
        session: AsyncSession,
        *,
        attempt_id: int,
        outcome: Literal["answered", "abstained"],
        qa_trace_id: int,
        embedding_llm_call_id: int,
        synthesis_llm_call_id: int | None,
    ) -> None:
        """Persist an irreversible delivery intent before the Telegram call."""

        if outcome not in ("answered", "abstained"):
            raise ValueError("delivery intent must consume semantic quota")
        result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.id == attempt_id,
                SemanticQaAttempt.status == "reserved",
                SemanticQaAttempt.delivery_started_at.is_(None),
            )
            .values(
                outcome=outcome,
                qa_trace_id=qa_trace_id,
                embedding_llm_call_id=embedding_llm_call_id,
                synthesis_llm_call_id=synthesis_llm_call_id,
                progress_at=datetime.now(timezone.utc),
                delivery_started_at=datetime.now(timezone.utc),
            )
        )
        if getattr(result, "rowcount", None) != 1:
            raise LookupError("semantic Q&A delivery intent is missing or already started")
        await session.flush()

    @staticmethod
    async def attach_trace(
        session: AsyncSession,
        *,
        attempt_id: int,
        qa_trace_id: int,
    ) -> None:
        result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.id == attempt_id,
                SemanticQaAttempt.status == "reserved",
            )
            .values(qa_trace_id=qa_trace_id, progress_at=datetime.now(timezone.utc))
        )
        if getattr(result, "rowcount", None) != 1:
            raise LookupError("semantic Q&A reservation is not active")
        await session.flush()

    @staticmethod
    async def attach_embedding_call(
        session: AsyncSession,
        *,
        attempt_id: int,
        embedding_llm_call_id: int,
    ) -> None:
        result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.id == attempt_id,
                SemanticQaAttempt.status == "reserved",
            )
            .values(
                embedding_llm_call_id=embedding_llm_call_id,
                progress_at=datetime.now(timezone.utc),
            )
        )
        if getattr(result, "rowcount", None) != 1:
            raise LookupError("semantic Q&A reservation is not active")
        await session.flush()

    @staticmethod
    async def finalize(
        session: AsyncSession,
        *,
        attempt_id: int,
        outcome: AttemptOutcome,
        qa_trace_id: int | None = None,
        embedding_llm_call_id: int | None = None,
        synthesis_llm_call_id: int | None = None,
    ) -> None:
        if outcome not in ("answered", "abstained", "technical_failure"):
            raise ValueError("unsupported semantic Q&A outcome")
        status = "released" if outcome == "technical_failure" else "consumed"
        result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.id == attempt_id,
                SemanticQaAttempt.status == "reserved",
            )
            .values(
                status=status,
                outcome=outcome,
                qa_trace_id=qa_trace_id,
                embedding_llm_call_id=embedding_llm_call_id,
                synthesis_llm_call_id=synthesis_llm_call_id,
                progress_at=datetime.now(timezone.utc),
                finalized_at=datetime.now(MOSCOW_TZ),
            )
        )
        if getattr(result, "rowcount", None) != 1:
            existing = (
                await session.execute(
                    select(SemanticQaAttempt.status, SemanticQaAttempt.outcome).where(
                        SemanticQaAttempt.id == attempt_id
                    )
                )
            ).one_or_none()
            if existing is not None and existing.status == status and existing.outcome == outcome:
                return
            raise LookupError("semantic Q&A attempt is missing or finalized differently")
        if qa_trace_id is not None:
            await session.execute(
                update(SemanticRetrievalTrace)
                .where(SemanticRetrievalTrace.attempt_id == attempt_id)
                .values(qa_trace_id=qa_trace_id)
            )
        await session.flush()

    @staticmethod
    async def touch(
        session: AsyncSession,
        *,
        attempt_id: int,
    ) -> None:
        """Renew a live reservation before a potentially blocking phase."""

        result = await session.execute(
            update(SemanticQaAttempt)
            .where(
                SemanticQaAttempt.id == attempt_id,
                SemanticQaAttempt.status == "reserved",
                SemanticQaAttempt.delivery_started_at.is_(None),
            )
            .values(progress_at=datetime.now(timezone.utc))
        )
        if getattr(result, "rowcount", None) != 1:
            raise LookupError("semantic Q&A reservation lease is no longer active")
        await session.flush()

    @staticmethod
    async def _active_count(
        session: AsyncSession,
        *,
        user_tg_id: int,
        local_day: date,
    ) -> int:
        result = await session.execute(
            select(SemanticQaAttempt.id).where(
                SemanticQaAttempt.user_tg_id == user_tg_id,
                SemanticQaAttempt.local_day == local_day,
                SemanticQaAttempt.status.in_(("reserved", "consumed")),
            )
        )
        return len(result.scalars().all())


__all__ = ["SemanticQuotaDecision", "SemanticQuotaRepo"]
