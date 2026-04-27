"""Handler for ``/forget_me`` Telegram command (T3-03).

Implements the GDPR-style member right to request erasure of their own data.
No admin approval is needed — any registered user can submit this request.

Authorization: self-only (``authorized_by='self'``).

What this handler does:
1. Look up the Telegram user in the ``users`` table. If not found → silent return
   (or brief informational reply). No event created.
2. Count ``chat_messages`` rows for this user BEFORE any cascade (info string only,
   not a guarantee of rows erased).
3. Create a ``forget_events`` row (idempotent on ``tombstone_key``):
   - target_type='user'
   - target_id=str(user.id)
   - tombstone_key='user:{tg_id}'
   - actor_user_id=user.id
   - authorized_by='self'
   - policy='forgotten'
4. Reply with confirmation + estimated message count.

What this handler does NOT do:
- Does NOT cascade / wipe ``chat_messages`` or ``message_versions`` rows. That is
  the responsibility of the Sprint 03 (#96) cascade worker which polls ``forget_events``
  with ``status='pending'``.
- Does NOT require LLM calls.

Transaction model: all DB work runs inside the ``DbSessionMiddleware`` session.
``ForgetEventRepo.create`` calls ``session.flush()`` internally. The middleware
commits at handler exit. No explicit commit here.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ChatMessage
from bot.db.repos.forget_event import ForgetEventRepo
from bot.db.repos.user import UserRepo

logger = logging.getLogger(__name__)

router = Router(name="forget_me")


@router.message(Command("forget_me"))
async def handle_forget_me(
    message: Message,
    session: AsyncSession,
) -> None:
    """Handle ``/forget_me`` command from DM or in-chat.

    Any registered user can request erasure of their own data (GDPR-style member
    right). No admin approval required. This handler enqueues a ``forget_events``
    row with ``status='pending'``; the Sprint 03 cascade worker (#96) drives the
    actual wipe.
    """
    tg_id = message.from_user.id

    # Step 1: look up user in the database.
    user = await UserRepo.get(session, tg_id)
    if user is None:
        logger.info("forget_me: tg_id=%s not in users table — ignoring request", tg_id)
        # Do not create an event for unregistered users.
        return

    # Step 2: count chat_messages BEFORE cascade (info only, not a guarantee).
    msg_count = await session.scalar(
        select(func.count(ChatMessage.id)).where(ChatMessage.user_id == user.id)
    )
    if msg_count is None:
        msg_count = 0

    # Step 3: create forget_events row (idempotent on tombstone_key).
    event = await ForgetEventRepo.create(
        session,
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        authorized_by="self",
        tombstone_key=f"user:{tg_id}",
        reason=f"/forget_me by user {tg_id}",
        policy="forgotten",
    )

    # Step 4: reply with confirmation + estimated count.
    await message.reply(
        f"OK — your data has been queued for removal (event #{event.id}). "
        f"Approximately {msg_count} messages will be redacted."
    )
