"""Live update ingestion (T1-04).

Persists every aiogram ``Update`` into the raw archive (``telegram_updates``, T1-03)
inside the same DB transaction that the handler will commit. Honors the ``#offrecord``
ordering rule from ``docs/memory-system/AUTHORIZED_SCOPE.md``:

    1. ``detect_policy()`` (stub today, real in T1-12) runs BEFORE commit.
    2. If policy == ``'offrecord'``, ``redact_raw_for_offrecord()`` strips content fields
       from the persisted ``raw_json`` BEFORE commit.
    3. Hash, ids, timestamps, and the redaction marker are always retained.

Behavior is gated by feature flag ``memory.ingestion.raw_updates.enabled``. The flag
defaults OFF (T1-01 migration seeds nothing); operators flip it on AFTER T1-12 + T1-13
land and the ordering rule is verifiable end-to-end.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import IngestionRun, TelegramUpdate
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.db.repos.ingestion_run import IngestionRunRepo
from bot.db.repos.telegram_update import TelegramUpdateRepo
from bot.services.governance import detect_policy, redact_raw_for_offrecord

logger = logging.getLogger(__name__)

RAW_ARCHIVE_FLAG = "memory.ingestion.raw_updates.enabled"

# Update event fields aiogram exposes on Update. Order is the priority used by
# ``_classify_update_type`` — first non-None wins.
_UPDATE_EVENT_FIELDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "callback_query",
    "chat_member",
    "my_chat_member",
    "message_reaction",
    "message_reaction_count",
)


def _compute_raw_hash(raw_dict: dict[str, Any]) -> str:
    """Deterministic SHA-256 over a stable JSON serialization of the update payload."""
    canonical = json.dumps(raw_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _classify_update_type(update: Update) -> str:
    for field in _UPDATE_EVENT_FIELDS:
        if getattr(update, field, None) is not None:
            return field
    return "unknown"


def _extract_chat_and_message_ids(update: Update) -> tuple[int | None, int | None]:
    """Best-effort extraction of (chat_id, message_id) from common update shapes."""
    msg = update.message or update.edited_message
    if msg is not None:
        chat_id = msg.chat.id if msg.chat is not None else None
        return chat_id, msg.message_id
    cb = update.callback_query
    if cb is not None and cb.message is not None:
        chat_id = cb.message.chat.id if cb.message.chat is not None else None
        return chat_id, cb.message.message_id
    cm = update.chat_member or update.my_chat_member
    if cm is not None:
        return cm.chat.id, None
    return None, None


def _extract_text_and_caption(update: Update) -> tuple[str | None, str | None]:
    msg = update.message or update.edited_message
    if msg is None:
        return None, None
    return msg.text, msg.caption


async def is_raw_archive_enabled(session: AsyncSession) -> bool:
    """Read the global ``memory.ingestion.raw_updates.enabled`` flag."""
    return await FeatureFlagRepo.get(session, RAW_ARCHIVE_FLAG)


async def get_or_create_live_run(session: AsyncSession) -> IngestionRun:
    """Return the active live ingestion run, opening one if none exists.

    HANDOFF.md §7 names this method on the ingestion service, not on the repo. The
    repo only does the read half (``get_active_live``); the write half (create on
    miss) lives here so the repo stays a thin data-access layer.
    """
    existing = await IngestionRunRepo.get_active_live(session)
    if existing is not None:
        return existing
    return await IngestionRunRepo.create(
        session, run_type="live", source_name="bot/__main__.py"
    )


async def record_update(
    session: AsyncSession,
    update: Update,
    ingestion_run_id: int | None = None,
) -> TelegramUpdate | None:
    """Persist one Telegram update into the raw archive.

    Returns:
        - The persisted ``TelegramUpdate`` row when the raw-archive flag is ON.
        - ``None`` when the flag is OFF (no row written, no behavior change).

    The function does NOT commit. Caller controls the transaction lifecycle (typically
    ``DbSessionMiddleware`` commits on handler success / rolls back on exception).

    Idempotency: live updates carry a non-null ``update_id``; repo's partial unique
    index makes ON CONFLICT DO NOTHING safe so retries (network replays, polling
    overlap on bot restart) collapse to a single row.

    Ordering rule (T1-04 ↔ T1-12 cross-cutting):
        - ``detect_policy`` runs BEFORE the row is persisted
        - If ``'offrecord'`` (only possible after T1-12), ``raw_json`` is redacted
          before insert and the row carries ``is_redacted=True``
        - This keeps the ``#offrecord`` invariant intact even when both tickets are
          in flight independently
    """
    if not await is_raw_archive_enabled(session):
        return None

    raw_dict = update.model_dump(mode="json", exclude_none=True)
    raw_hash = _compute_raw_hash(raw_dict)
    update_type = _classify_update_type(update)
    chat_id, message_id = _extract_chat_and_message_ids(update)
    text, caption = _extract_text_and_caption(update)

    # Detect policy in the same transaction as the raw insert (no commit yet).
    policy, _mark_payload = detect_policy(text, caption)
    is_redacted = policy == "offrecord"
    redaction_reason = "offrecord" if is_redacted else None
    persisted_raw = redact_raw_for_offrecord(raw_dict) if is_redacted else raw_dict

    return await TelegramUpdateRepo.insert(
        session,
        update_type=update_type,
        update_id=update.update_id,
        raw_json=persisted_raw,
        raw_hash=raw_hash,
        chat_id=chat_id,
        message_id=message_id,
        ingestion_run_id=ingestion_run_id,
        is_redacted=is_redacted,
        redaction_reason=redaction_reason,
    )
