from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.message import MessageRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter
from bot.services.normalization import extract_normalized_fields

logger = logging.getLogger(__name__)

router = Router(name="chat_messages")


@router.message(GroupChatFilter())
async def save_chat_message(
    message: Message,
    session: AsyncSession,
) -> None:
    """Save every message in the community group chat."""
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        return

    if message.from_user is None:
        return

    # Keep sender profile fresh for message attribution and admin lookups.
    await UserRepo.upsert(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    # T1-09/10/11: extract normalized fields (reply / thread / caption / kind) so
    # captions and media-only messages are first-class content in the archive. Falls
    # back to None for fields the message doesn't carry, preserving the legacy shape
    # for plain-text messages.
    normalized = extract_normalized_fields(message)

    # MessageRepo.save is idempotent on (chat_id, message_id) per T0-03 — duplicates
    # return the existing row instead of raising. No need for a try/except + rollback
    # that would also discard the UserRepo.upsert and set_member work above.
    #
    # raw_json continues to be persisted ONLY when text is present (preserving the
    # gatekeeper-era behaviour). Caption-only media messages get their content captured
    # via the dedicated `caption` column (T1-05/T1-11) — that column is the
    # authoritative store for caption content going forward. raw_json is intentionally
    # NOT extended to caption-only paths in this PR because:
    #   - chat_messages.raw_json sits OUTSIDE the #offrecord ordering rule (which
    #     governs telegram_updates via bot/services/ingestion.py + governance stub)
    #   - T1-12 (deterministic detector) is the right place to wire detect_policy
    #     into the chat_messages path; widening raw_json here would extend the
    #     governance gap to caption-bearing media messages with no compensating
    #     redaction
    # See AUTHORIZED_SCOPE.md §`#offrecord` ordering rule for the full constraint.
    await MessageRepo.save(
        session,
        message_id=message.message_id,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=message.text,
        date=message.date,
        raw_json=message.model_dump(mode="json", exclude_none=True)
        if message.text
        else None,
        **normalized,
    )
