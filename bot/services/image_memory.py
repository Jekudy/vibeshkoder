"""Queue and process Telegram photo descriptions without blocking ingestion."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import ChatMessage, MessageMedia
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.services.llm_gateway import (
    ImageDescriptionAmbiguousError,
    ImageDescriptionBudgetExceeded,
    ImageDescriptionOutcomeRef,
    ImageDescriptionResult,
    describe_image,
    load_vision_gateway_config,
)
from bot.services.llm_providers import (
    ProviderStructuralError,
    ProviderTransientError,
)

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
DescribeImageCallable = Callable[..., Awaitable[ImageDescriptionResult]]
SessionFactory = Callable[[], Any]
MAX_DESCRIPTION_ATTEMPTS = 3
_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
)


@dataclass(frozen=True)
class _PhotoClaim:
    media_id: int
    claim_token: str
    telegram_file_id: str | None
    caption: str | None
    attempts: int


def telegram_source_message_url(
    chat_id: int,
    message_id: int,
    *,
    username: str | None,
) -> str:
    """Return a non-tokenized Telegram permalink for one source message."""

    if message_id <= 0:
        raise ValueError("message_id must be positive")
    if username and _USERNAME_RE.fullmatch(username):
        return f"https://t.me/{username}/{message_id}"
    chat_text = str(chat_id)
    if not chat_text.startswith("-100") or len(chat_text) <= 4:
        raise ValueError("private Telegram source links require a supergroup chat id")
    return f"https://t.me/c/{chat_text[4:]}/{message_id}"


def _largest_photo(message: Any) -> Any | None:
    photos = getattr(message, "photo", None)
    if not photos:
        return None
    return max(
        photos,
        key=lambda photo: (
            int(getattr(photo, "width", 0)) * int(getattr(photo, "height", 0)),
            int(getattr(photo, "file_size", 0) or 0),
        ),
    )


async def enqueue_photo_memory(
    session: AsyncSession,
    *,
    message: Any,
    chat_message_id: int,
) -> MessageMedia | None:
    """Idempotently queue a photo; voice/audio messages are ignored."""

    photo = _largest_photo(message)
    if photo is None:
        return None
    source_url = telegram_source_message_url(
        int(message.chat.id),
        int(message.message_id),
        username=getattr(message.chat, "username", None),
    )
    statement = (
        pg_insert(MessageMedia)
        .values(
            chat_message_id=chat_message_id,
            media_kind="photo",
            telegram_file_id=str(photo.file_id),
            telegram_file_unique_id=(
                str(photo.file_unique_id) if getattr(photo, "file_unique_id", None) else None
            ),
            source_message_url=source_url,
            description_status="pending",
        )
        .on_conflict_do_nothing(index_elements=["chat_message_id"])
        .returning(MessageMedia)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    if created is not None:
        await session.flush()
        return created
    return (
        await session.execute(
            select(MessageMedia).where(MessageMedia.chat_message_id == chat_message_id)
        )
    ).scalar_one()


async def record_missing_import_photo(
    session: AsyncSession,
    *,
    chat_message_id: int,
    chat_id: int,
    message_id: int,
    username: str | None = None,
) -> MessageMedia:
    """Record a historical photo whose Telegram export omitted the image file."""
    source_url = telegram_source_message_url(chat_id, message_id, username=username)
    statement = (
        pg_insert(MessageMedia)
        .values(
            chat_message_id=chat_message_id,
            media_kind="photo",
            telegram_file_id=None,
            telegram_file_unique_id=None,
            source_message_url=source_url,
            description=None,
            description_status="missing_source",
            last_error_code="historical_export_no_file",
        )
        .on_conflict_do_nothing(index_elements=["chat_message_id"])
        .returning(MessageMedia)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    if created is not None:
        await session.flush()
        return created
    return (
        await session.execute(
            select(MessageMedia).where(MessageMedia.chat_message_id == chat_message_id)
        )
    ).scalar_one()


def _mime_type(file_path: str) -> str:
    suffix = PurePosixPath(file_path).suffix.casefold()
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
    }.get(suffix, "image/jpeg")


async def _default_describe_image(session: AsyncSession, **kwargs: Any) -> ImageDescriptionResult:
    return await describe_image(
        session,
        config=load_vision_gateway_config(),
        ledger_repo=LedgerRepo(),
        **kwargs,
    )


def _durable_session_factory(session: AsyncSession) -> SessionFactory:
    """Create independent sessions, preserving the real-Postgres test connection."""

    bind = session.bind
    if bind is None:
        raise RuntimeError("image worker session is not bound")
    return async_sessionmaker(bind, class_=AsyncSession, expire_on_commit=False)


async def _claim_next_photo(
    session_factory: SessionFactory,
    *,
    now: datetime,
) -> _PhotoClaim | None:
    """Commit a single processing claim before any Telegram/provider I/O."""

    async with session_factory() as claim_session:
        result = await claim_session.execute(
            select(MessageMedia, ChatMessage.caption)
            .join(ChatMessage, ChatMessage.id == MessageMedia.chat_message_id)
            .where(MessageMedia.description_status == "pending")
            .where((MessageMedia.next_attempt_at.is_(None)) | (MessageMedia.next_attempt_at <= now))
            .order_by(MessageMedia.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        row = result.one_or_none()
        if row is None:
            await claim_session.rollback()
            return None
        media: MessageMedia = row[0]
        token = str(uuid.uuid4())
        media.description_status = "processing"
        media.description_claim_token = token
        media.description_claimed_at = now
        media.description_attempts += 1
        media.description = None
        media.description_model = None
        media.llm_usage_ledger_id = None
        media.next_attempt_at = None
        media.last_error_code = "reserved_in_flight"
        claim = _PhotoClaim(
            media_id=media.id,
            claim_token=token,
            telegram_file_id=media.telegram_file_id,
            caption=row[1],
            attempts=media.description_attempts,
        )
        await claim_session.commit()
        return claim


def _safe_error_subtype(exc: BaseException) -> str:
    subtype = getattr(exc, "subtype", "unknown")
    if isinstance(subtype, str) and re.fullmatch(r"[a-z0-9_]+", subtype):
        return subtype
    return "unknown"


async def _finalize_failure(
    session_factory: SessionFactory,
    *,
    claim: _PhotoClaim,
    status: str,
    next_attempt_at: datetime | None,
    last_error_code: str,
    restore_attempt: bool = False,
) -> MessageMedia:
    """Durably leave processing only when the exact claim still owns the row."""

    values: dict[str, Any] = {
        "description": None,
        "description_status": status,
        "next_attempt_at": next_attempt_at,
        "last_error_code": last_error_code[:64],
        "description_claim_token": None,
        "description_claimed_at": None,
    }
    if restore_attempt:
        values["description_attempts"] = max(0, claim.attempts - 1)
    async with session_factory() as terminal_session:
        updated = await terminal_session.execute(
            sa_update(MessageMedia)
            .where(
                MessageMedia.id == claim.media_id,
                MessageMedia.description_status == "processing",
                MessageMedia.description_claim_token == claim.claim_token,
            )
            .values(**values)
        )
        if updated.rowcount != 1:
            await terminal_session.rollback()
            raise RuntimeError("image description claim changed during failure handling")
        await terminal_session.commit()
    return await _load_media(session_factory, claim.media_id)


async def _finalize_success_fallback(
    session_factory: SessionFactory,
    *,
    claim: _PhotoClaim,
    result: ImageDescriptionResult,
) -> MessageMedia:
    """Support injected gateways; the real gateway already commits atomically."""

    async with session_factory() as terminal_session:
        media = await terminal_session.get(MessageMedia, claim.media_id)
        if media is None:
            raise RuntimeError("claimed image row disappeared")
        if media.description_status == "ready":
            if (
                media.llm_usage_ledger_id != result.llm_usage_ledger_id
                or media.description != result.description
                or media.description_model != result.model
            ):
                raise RuntimeError("durable image outcome differs from gateway result")
            await terminal_session.rollback()
            return await _load_media(session_factory, claim.media_id)
        if (
            media.description_status != "processing"
            or media.description_claim_token != claim.claim_token
        ):
            raise RuntimeError("image description claim changed after gateway success")
        media.description = result.description
        media.description_status = "ready"
        media.description_model = result.model
        media.llm_usage_ledger_id = result.llm_usage_ledger_id
        media.next_attempt_at = None
        media.last_error_code = None
        media.description_claim_token = None
        media.description_claimed_at = None
        await terminal_session.commit()
    return await _load_media(session_factory, claim.media_id)


async def _load_media(session_factory: SessionFactory, media_id: int) -> MessageMedia:
    async with session_factory() as read_session:
        media = await read_session.get(MessageMedia, media_id)
        if media is None:
            raise RuntimeError("image media row disappeared")
        read_session.expunge(media)
        return media


async def process_next_pending_photo(
    session: AsyncSession,
    *,
    bot: Any,
    describe_image_fn: DescribeImageCallable = _default_describe_image,
    session_factory: SessionFactory | None = None,
) -> MessageMedia | None:
    """Process one due photo with durable claims and terminal outcomes.

    The caller's session is used only to derive the database binding.  Every
    state transition commits through a separate short-lived session, so the
    caller can roll back after this function without erasing a paid result.
    """

    now = datetime.now(timezone.utc)
    durable_sessions = session_factory or _durable_session_factory(session)
    claim = await _claim_next_photo(durable_sessions, now=now)
    if claim is None:
        return None
    try:
        if not claim.telegram_file_id:
            raise ValueError("pending photo has no Telegram file id")
        telegram_file = await bot.get_file(claim.telegram_file_id)
        file_path = getattr(telegram_file, "file_path", None)
        if not isinstance(file_path, str) or not file_path:
            raise ValueError("Telegram returned no file path")
        downloaded = await bot.download_file(file_path)
        if isinstance(downloaded, bytes):
            image_bytes = downloaded
        else:
            read = getattr(downloaded, "read", None)
            if read is None:
                raise ValueError("Telegram download result is not readable")
            image_bytes = read()
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ValueError("Telegram downloaded an empty image")

        async with durable_sessions() as gateway_session:
            description = await describe_image_fn(
                gateway_session,
                image_bytes=image_bytes,
                mime_type=_mime_type(file_path),
                caption=claim.caption,
                ledger_session_factory=durable_sessions,
                outcome_ref=ImageDescriptionOutcomeRef(
                    message_media_id=claim.media_id,
                    claim_token=claim.claim_token,
                ),
            )
        return await _finalize_success_fallback(
            durable_sessions,
            claim=claim,
            result=description,
        )
    except ImageDescriptionBudgetExceeded as exc:
        # No provider call was made, so budget deferral does not consume one
        # of the three delivery/vision attempts.
        media = await _finalize_failure(
            durable_sessions,
            claim=claim,
            status="pending",
            next_attempt_at=now + timedelta(hours=1),
            last_error_code="budget_exceeded",
            restore_attempt=True,
        )
        logger.warning(
            "photo_description_deferred",
            extra={
                "message_media_id": media.id,
                "error_class": type(exc).__name__,
                "next_attempt_at": media.next_attempt_at.isoformat(),
            },
        )
        return media
    except ProviderTransientError as exc:
        subtype = _safe_error_subtype(exc)
        if subtype != "rate_limit":
            # This guard also covers injected gateways which bypass the real
            # gateway's classification.  Only an explicit 429/rate_limit is
            # proven rejected before a paid completion could be produced.
            logger.error(
                "photo_description_ambiguous",
                extra={
                    "message_media_id": claim.media_id,
                    "error_class": type(exc).__name__,
                    "provider_error_subtype": subtype,
                },
            )
            return await _load_media(durable_sessions, claim.media_id)
        if claim.attempts < MAX_DESCRIPTION_ATTEMPTS:
            delay = _RETRY_DELAYS[claim.attempts - 1]
            status = "pending"
            next_attempt_at = now + delay
            log_method = logger.warning
            event = "photo_description_retry_scheduled"
        else:
            status = "failed"
            next_attempt_at = None
            log_method = logger.error
            event = "photo_description_failed"
        media = await _finalize_failure(
            durable_sessions,
            claim=claim,
            status=status,
            next_attempt_at=next_attempt_at,
            last_error_code=f"provider_{subtype}",
        )
        log_method(
            event,
            extra={
                "message_media_id": media.id,
                "attempts": media.description_attempts,
                "error_class": type(exc).__name__,
            },
        )
        return media
    except (TelegramAPIError, OSError) as exc:
        # Telegram download failures happen before provider dispatch and are
        # therefore safe for the same bounded delivery retry policy.
        subtype = _safe_error_subtype(exc)
        if claim.attempts < MAX_DESCRIPTION_ATTEMPTS:
            delay = _RETRY_DELAYS[claim.attempts - 1]
            status = "pending"
            next_attempt_at = now + delay
            log_method = logger.warning
            event = "photo_description_retry_scheduled"
        else:
            status = "failed"
            next_attempt_at = None
            log_method = logger.error
            event = "photo_description_failed"
        media = await _finalize_failure(
            durable_sessions,
            claim=claim,
            status=status,
            next_attempt_at=next_attempt_at,
            last_error_code=f"telegram_{subtype}",
        )
        log_method(
            event,
            extra={
                "message_media_id": media.id,
                "attempts": media.description_attempts,
                "error_class": type(exc).__name__,
            },
        )
        return media
    except (ProviderStructuralError, ValueError) as exc:
        media = await _finalize_failure(
            durable_sessions,
            claim=claim,
            status="failed",
            next_attempt_at=None,
            last_error_code=f"provider_{_safe_error_subtype(exc)}",
        )
        logger.error(
            "photo_description_failed",
            extra={"message_media_id": media.id, "error_class": type(exc).__name__},
        )
        return media
    except ImageDescriptionAmbiguousError as exc:
        # The durable claim + reservation intentionally remain in processing.
        # Retrying could duplicate a provider charge because the process died or
        # failed after dispatch but before terminal persistence.
        logger.error(
            "photo_description_ambiguous",
            extra={"message_media_id": claim.media_id, "error_class": type(exc).__name__},
        )
        return await _load_media(durable_sessions, claim.media_id)


__all__ = [
    "enqueue_photo_memory",
    "process_next_pending_photo",
    "record_missing_import_photo",
    "telegram_source_message_url",
]
