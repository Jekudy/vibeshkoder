"""Bounded, resumable one-off extraction backfill for one Telegram chat."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import ChatMessage, ExtractionRun, User
from bot.services.extractor import ExtractCandidatesGateway, run_extraction_pass

logger = logging.getLogger(__name__)

MEMORY_AUTOMATION_ACTOR_ENV = "MEMORY_AUTOMATION_ACTOR_USER_ID"
DEFAULT_WINDOW_HOURS = 24
MIN_WINDOW_HOURS = 1
MAX_WINDOW_HOURS = 168
DEFAULT_MAX_WINDOWS = 400
MAX_BACKFILL_WINDOWS = 400


class MemoryBackfillConfigurationError(ValueError):
    """The operator request is unsafe, incomplete, or not deployable."""


class MemoryBackfillWindowError(RuntimeError):
    """One window failed; subsequent windows were not attempted."""

    def __init__(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        extraction_run_id: uuid.UUID | None,
        error_class: str | None,
    ) -> None:
        super().__init__(
            "memory backfill window failed "
            f"start={window_start.isoformat()} end={window_end.isoformat()} "
            f"run_id={extraction_run_id or 'none'} "
            f"error_class={error_class or 'none'}"
        )
        self.window_start = window_start
        self.window_end = window_end
        self.extraction_run_id = extraction_run_id
        self.error_class = error_class


@dataclass(frozen=True)
class MemoryBackfillWindowResult:
    window_number: int
    window_count: int
    window_start: datetime
    window_end: datetime
    extraction_run_id: uuid.UUID | None
    run_status: str
    candidate_count: int
    promotion_count: int
    promoted_count: int
    resumed: bool


@dataclass(frozen=True)
class MemoryBackfillReport:
    source_chat_id: int
    actor_user_id: int
    range_start: datetime
    range_end: datetime
    window_hours: int
    windows: tuple[MemoryBackfillWindowResult, ...]

    @property
    def completed_window_count(self) -> int:
        return sum(window.run_status == "completed" for window in self.windows)

    @property
    def resumed_window_count(self) -> int:
        return sum(window.resumed for window in self.windows)

    @property
    def candidate_count(self) -> int:
        return sum(window.candidate_count for window in self.windows)

    @property
    def promoted_count(self) -> int:
        return sum(window.promoted_count for window in self.windows)


class _PromotionResult(Protocol):
    status: str


PromotionFunction = Callable[
    ...,
    Awaitable[list[_PromotionResult]],
]
ProgressFunction = Callable[[MemoryBackfillWindowResult], None]


def resolve_automation_actor_user_id(explicit_actor_user_id: int | None) -> int:
    """Resolve the required automatic actor without inventing a fallback."""
    raw_value: object = explicit_actor_user_id
    if raw_value is None:
        raw_value = os.environ.get(MEMORY_AUTOMATION_ACTOR_ENV)
        if raw_value is None or raw_value == "":
            raise MemoryBackfillConfigurationError(
                f"{MEMORY_AUTOMATION_ACTOR_ENV} or --actor-user-id is required"
            )
    try:
        actor_user_id = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise MemoryBackfillConfigurationError(
            f"{MEMORY_AUTOMATION_ACTOR_ENV} must be a positive integer"
        ) from exc
    if actor_user_id <= 0:
        raise MemoryBackfillConfigurationError(
            f"{MEMORY_AUTOMATION_ACTOR_ENV} must be a positive integer"
        )
    return actor_user_id


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MemoryBackfillConfigurationError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _validate_limits(*, window_hours: int, max_windows: int) -> None:
    if type(window_hours) is not int or not MIN_WINDOW_HOURS <= window_hours <= MAX_WINDOW_HOURS:
        raise MemoryBackfillConfigurationError(
            f"window_hours must be between {MIN_WINDOW_HOURS} and {MAX_WINDOW_HOURS}"
        )
    if type(max_windows) is not int or not 1 <= max_windows <= MAX_BACKFILL_WINDOWS:
        raise MemoryBackfillConfigurationError(
            f"max_windows must be between 1 and {MAX_BACKFILL_WINDOWS}"
        )


async def _require_source_chat_marker(session: AsyncSession) -> None:
    marker_exists = await session.scalar(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'extraction_runs'
                  AND column_name = 'source_chat_id'
            )
            """
        )
    )
    if not marker_exists:
        raise MemoryBackfillConfigurationError(
            "extraction_runs.source_chat_id is required for safe resume; "
            "apply the migration that adds the BIGINT column and its window index"
        )


async def _require_actor(session: AsyncSession, actor_user_id: int) -> None:
    if await session.get(User, actor_user_id) is None:
        raise MemoryBackfillConfigurationError("automation actor must identify an existing user")


async def _resolve_range(
    session: AsyncSession,
    *,
    source_chat_id: int,
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime]:
    if (start is None) != (end is None):
        raise MemoryBackfillConfigurationError(
            "start and end must be supplied together, or both omitted for DB bounds"
        )
    if start is not None and end is not None:
        resolved_start = _as_utc(start, field_name="start")
        resolved_end = _as_utc(end, field_name="end")
    else:
        row = (
            await session.execute(
                select(func.min(ChatMessage.date), func.max(ChatMessage.date)).where(
                    ChatMessage.chat_id == source_chat_id
                )
            )
        ).one()
        minimum, maximum = row
        if minimum is None or maximum is None:
            raise MemoryBackfillConfigurationError(
                "source chat has no messages from which to derive DB bounds"
            )
        resolved_start = _as_utc(minimum, field_name="DB minimum date")
        try:
            resolved_end = _as_utc(maximum, field_name="DB maximum date") + timedelta(
                microseconds=1
            )
        except OverflowError as exc:
            raise MemoryBackfillConfigurationError(
                "DB maximum message date cannot be represented as an exclusive bound"
            ) from exc

    if resolved_end <= resolved_start:
        raise MemoryBackfillConfigurationError("end must be later than start")
    return resolved_start, resolved_end


def _window_count(start: datetime, end: datetime, width: timedelta) -> int:
    quotient, remainder = divmod(end - start, width)
    return quotient + int(remainder > timedelta(0))


async def _find_failed_run_id(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_chat_id: int,
    window_start: datetime,
    window_end: datetime,
) -> uuid.UUID | None:
    async with session_factory() as session:
        return await session.scalar(
            select(ExtractionRun.id)
            .where(
                ExtractionRun.source_chat_id == source_chat_id,
                ExtractionRun.ingestion_window_start == window_start,
                ExtractionRun.ingestion_window_end == window_end,
                ExtractionRun.run_status == "failed",
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
            .limit(1)
        )


async def run_memory_backfill(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: ExtractCandidatesGateway,
    source_chat_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    actor_user_id: int | None = None,
    promote_run_fn: PromotionFunction | None = None,
    progress: ProgressFunction | None = None,
) -> MemoryBackfillReport:
    """Extract and promote history one committed, bounded event-time window at a time.

    An exact completed run with the same chat, window, and operator is resumed by
    promotion only. It never invokes the provider a second time. The first failed
    window stops the pass; retries are deliberately left to a new operator command.
    """
    if type(source_chat_id) is not int or source_chat_id == 0:
        raise MemoryBackfillConfigurationError("source_chat_id must be a non-zero integer")
    _validate_limits(window_hours=window_hours, max_windows=max_windows)
    resolved_actor_id = resolve_automation_actor_user_id(actor_user_id)

    async with session_factory() as validation_session:
        await _require_source_chat_marker(validation_session)
        await _require_actor(validation_session, resolved_actor_id)
        range_start, range_end = await _resolve_range(
            validation_session,
            source_chat_id=source_chat_id,
            start=start,
            end=end,
        )

    width = timedelta(hours=window_hours)
    total_windows = _window_count(range_start, range_end, width)
    if total_windows > max_windows:
        raise MemoryBackfillConfigurationError(
            f"range requires {total_windows} windows, exceeding max_windows={max_windows}"
        )

    if promote_run_fn is None:
        from bot.services.candidate_promotion import promote_run_candidates

        promote_run_fn = promote_run_candidates

    windows: list[MemoryBackfillWindowResult] = []
    for index in range(total_windows):
        window_start = range_start + width * index
        window_end = min(window_start + width, range_end)
        extraction_run_id: uuid.UUID | None = None
        try:
            # The extractor commits the ledger, terminal run, and candidates in
            # its own durable transaction before returning. Promotion remains a
            # separate transaction, so a promotion crash can resume without a
            # second provider call. Semantic identity intentionally excludes the
            # actor: an operator change cannot create a second spend.
            async with session_factory() as session, session.begin():
                extraction_result = await run_extraction_pass(
                    session,
                    window_start=window_start,
                    window_end=window_end,
                    gateway=gateway,
                    operator_user_id=resolved_actor_id,
                    source_chat_id=source_chat_id,
                    durable_session_factory=session_factory,
                )
                extraction_run_id = extraction_result.extraction_run_id
                run_status = extraction_result.run_status
                candidate_count = extraction_result.candidate_count
                resumed = extraction_result.resumed

            promotion_results: list[_PromotionResult] = []
            if run_status == "completed":
                if extraction_run_id is None:
                    raise RuntimeError("completed extraction window is missing its run id")
                async with session_factory() as promotion_session, promotion_session.begin():
                    promotion_results = await promote_run_fn(
                        promotion_session,
                        extraction_run_id=extraction_run_id,
                        actor_user_id=resolved_actor_id,
                    )

            window_result = MemoryBackfillWindowResult(
                window_number=index + 1,
                window_count=total_windows,
                window_start=window_start,
                window_end=window_end,
                extraction_run_id=extraction_run_id,
                run_status=run_status,
                candidate_count=candidate_count,
                promotion_count=len(promotion_results),
                promoted_count=sum(result.status == "promoted" for result in promotion_results),
                resumed=resumed,
            )
        except Exception as exc:
            failed_run_id = await _find_failed_run_id(
                session_factory,
                source_chat_id=source_chat_id,
                window_start=window_start,
                window_end=window_end,
            )
            durable_run_id = failed_run_id or extraction_run_id
            # Deliberately do not attach ``exc_info`` or ``str(exc)``: provider
            # failures can contain response fragments or credentials.
            logger.error(
                "memory_backfill_window_crashed",
                extra={
                    "source_chat_id": source_chat_id,
                    "actor_user_id": resolved_actor_id,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "extraction_run_id": str(durable_run_id) if durable_run_id else None,
                    "error_class": type(exc).__name__,
                },
            )
            raise MemoryBackfillWindowError(
                window_start=window_start,
                window_end=window_end,
                extraction_run_id=durable_run_id,
                error_class=type(exc).__name__,
            ) from exc

        windows.append(window_result)
        if progress is not None:
            progress(window_result)
        if window_result.run_status != "completed":
            raise MemoryBackfillWindowError(
                window_start=window_start,
                window_end=window_end,
                extraction_run_id=window_result.extraction_run_id,
                error_class=None,
            )

    return MemoryBackfillReport(
        source_chat_id=source_chat_id,
        actor_user_id=resolved_actor_id,
        range_start=range_start,
        range_end=range_end,
        window_hours=window_hours,
        windows=tuple(windows),
    )


__all__ = [
    "DEFAULT_MAX_WINDOWS",
    "DEFAULT_WINDOW_HOURS",
    "MAX_BACKFILL_WINDOWS",
    "MAX_WINDOW_HOURS",
    "MEMORY_AUTOMATION_ACTOR_ENV",
    "MemoryBackfillConfigurationError",
    "MemoryBackfillReport",
    "MemoryBackfillWindowError",
    "MemoryBackfillWindowResult",
    "resolve_automation_actor_user_id",
    "run_memory_backfill",
]
