from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.locks import advisory_lock_chat_message
from bot.db.repos.message import MessageRepo
from bot.db.repos.offrecord_mark import OffrecordMarkRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter
from bot.services.governance import detect_policy
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

    # #80: Take advisory lock for this (chat_id, message_id) pair before any read or
    # write. Serializes concurrent transactions (e.g. duplicate delivery, simultaneous
    # edit) at the application level. Releases automatically at transaction end.
    await advisory_lock_chat_message(session, message.chat.id, message.message_id)

    # Keep sender profile fresh for message attribution and admin lookups.
    await UserRepo.upsert(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    # T1-09/10/11: extract normalized fields (reply / thread / caption / kind).
    normalized = extract_normalized_fields(message)

    # T1-12: detect #nomem / #offrecord BEFORE we persist content. The detector runs
    # over text + caption, so caption-only media messages with #offrecord are caught
    # along with text-bearing ones. This closes the AUTHORIZED_SCOPE.md "Known gap"
    # for the chat_messages path that T1-09/10/11 deferred.
    # ``getattr`` handles aiogram Message AND test-shaped SimpleNamespace alike.
    policy, mark_payload = detect_policy(
        getattr(message, "text", None), getattr(message, "caption", None)
    )

    # Build the persisted values per policy. For 'offrecord', content fields are nulled
    # (we keep the row + ids + timestamps + memory_policy marker). For 'nomem',
    # content stays but the policy column lets downstream filters exclude it.
    if policy == "offrecord":
        persist_text: str | None = None
        persist_caption: str | None = None
        persist_raw_json: dict | None = None
        is_redacted_flag = True
    else:
        persist_text = message.text
        persist_caption = normalized["caption"]
        # raw_json continues to track text presence (gatekeeper-era behaviour). T1-12
        # closes the caption raw_json gap by routing through detect_policy first; we
        # still don't write raw_json for caption-only media (no need — caption column
        # is the authoritative store).
        persist_raw_json = (
            message.model_dump(mode="json", exclude_none=True) if message.text else None
        )
        is_redacted_flag = False

    saved = await MessageRepo.save(
        session,
        message_id=message.message_id,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=persist_text,
        date=message.date,
        raw_json=persist_raw_json,
        reply_to_message_id=normalized["reply_to_message_id"],
        message_thread_id=normalized["message_thread_id"],
        caption=persist_caption,
        message_kind=normalized["message_kind"],
        memory_policy=policy,
        is_redacted=is_redacted_flag,
    )

    # T1-13: persist the audit mark for any non-normal policy. The same transaction
    # carries the message + mark — DbSessionMiddleware commits both atomically.
    if policy != "normal" and mark_payload is not None:
        # detect_policy guarantees ``detected_by`` is present in mark_payload for any
        # non-"normal" outcome — direct dict access is safe.
        await OffrecordMarkRepo.create_for_message(
            session,
            chat_message_id=saved.id,
            mark_type=policy,
            detected_by=mark_payload["detected_by"],
            set_by_user_id=message.from_user.id,
            thread_id=normalized["message_thread_id"],
        )
