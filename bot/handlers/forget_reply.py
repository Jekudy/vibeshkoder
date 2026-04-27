"""Handler for the ``/forget`` command (T3-02).

Allows a community member to request erasure of a specific chat message by
replying to it with ``/forget``. The handler creates a ``forget_events`` row
(tombstone) via ``ForgetEventRepo.create`` — the cascade worker (Sprint 3 / #96)
picks it up asynchronously and performs the actual deletion.

Authorization per HANDOFF §10:
- Author (requester_user.id == chat_message.user_id) → authorized_by='self'
- Admin (requester_user.is_admin) → authorized_by='admin'
- Any other member or unknown user → silent denial (logged server-side, no reply)

This handler does NOT perform cascade execution — it only records the intent.

Registration: must be included in ``bot/__main__.py`` BEFORE the ``chat_messages``
catch-all router so the Command filter matches first.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import ChatMessage
from bot.db.repos.forget_event import ForgetEventRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import GroupChatFilter

logger = logging.getLogger(__name__)

router = Router(name="forget_reply")


async def _find_chat_message(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
) -> ChatMessage | None:
    """Return the ``chat_messages`` row for ``(chat_id, message_id)``, or None.

    Uses ``SELECT ... FOR UPDATE`` to acquire a row-level lock for the duration of the
    transaction. This closes the authz→tombstone TOCTOU window: without the lock, two
    concurrent /forget handlers could both read the authz check, then one writes the
    tombstone *after* the other has completed — the lock ensures the authz read and the
    ForgetEventRepo.create write are serialized within the same transaction.
    (Note: the earlier "race-safe via ON CONFLICT" rationale addressed the duplicate-key
    insert race inside the repo, NOT the authz read race at the handler level.)
    """
    result = await session.execute(
        select(ChatMessage)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


@router.message(Command("forget"), GroupChatFilter())
async def handle_forget(
    message: Message,
    session: AsyncSession,
) -> None:
    """Handle ``/forget`` command invoked as a reply to a message.

    Guards:
    1. Must be sent in the community chat.
    2. Must be a reply to a message (otherwise prompt usage).
    3. Requester must be a known user in DB.
    4. Requester must be author or admin (otherwise silent denial).
    """
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        return

    # Guard: must be a reply to a message.
    if message.reply_to_message is None:
        await message.answer(
            "Use /forget as a reply to the message you want to forget."
        )
        return

    replied_message_id = message.reply_to_message.message_id
    chat_id = message.chat.id
    requester_tg_id = message.from_user.id if message.from_user else None

    if requester_tg_id is None:
        logger.warning(
            "forget_reply: no from_user in /forget message — denying silently"
        )
        return

    # Resolve the requester user from DB.
    requester_user = await UserRepo.get(session, requester_tg_id)
    if requester_user is None:
        logger.warning(
            "forget_reply: unknown user tg_id=%s attempted /forget — denying silently",
            requester_tg_id,
        )
        return

    # Resolve the target chat_message row.
    chat_message = await _find_chat_message(session, chat_id, replied_message_id)
    if chat_message is None:
        logger.warning(
            "forget_reply: no chat_messages row for chat_id=%s message_id=%s — skipping",
            chat_id,
            replied_message_id,
        )
        return

    # Authorization check.
    is_author = chat_message.user_id == requester_user.id
    is_admin = requester_user.is_admin

    if not is_author and not is_admin:
        logger.info(
            "forget_reply: requester tg_id=%s (user.id=%s) denied for chat_id=%s message_id=%s — "
            "neither author (message.user_id=%s) nor admin",
            requester_tg_id,
            requester_user.id,
            chat_id,
            replied_message_id,
            chat_message.user_id,
        )
        return

    authorized_by = "self" if is_author else "admin"
    tombstone_key = f"message:{chat_id}:{replied_message_id}"
    reason = f"/forget by {'author' if is_author else 'admin'} {requester_tg_id}"

    forget_event = await ForgetEventRepo.create(
        session,
        target_type="message",
        target_id=str(chat_message.id),
        actor_user_id=requester_user.id,
        authorized_by=authorized_by,
        tombstone_key=tombstone_key,
        reason=reason,
        policy="forgotten",
    )

    await message.answer(
        f"OK, this message has been queued for forgetting (event #{forget_event.id})."
    )
