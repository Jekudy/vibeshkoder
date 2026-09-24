from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro_effect_outbox import IntroEffectOutboxRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.user import UserRepo
from bot.html_escape import html_escape

logger = logging.getLogger(__name__)

router = Router(name="chat_events")


def _is_join(update: ChatMemberUpdated) -> bool:
    """Check if this update represents a user joining the chat."""
    old = update.old_chat_member
    new = update.new_chat_member
    return old.status in ("left", "kicked", "restricted") and new.status in (
        "member",
        "administrator",
        "creator",
    )


def _is_leave(update: ChatMemberUpdated) -> bool:
    """Check if this update represents a user leaving the chat."""
    old = update.old_chat_member
    new = update.new_chat_member
    return old.status in ("member", "administrator", "creator") and new.status in ("left", "kicked")


def _admission_rejection_reason(active_app, user_id: int) -> str | None:
    if active_app is None:
        return "no active application"
    if active_app.status not in {"vouched", "added"}:
        return f"application {active_app.id} status={active_app.status!r}"
    if active_app.invite_user_id != user_id:
        return f"application {active_app.id} invite_user_id={active_app.invite_user_id!r}"
    return None


@router.chat_member()
async def handle_chat_member(
    event: ChatMemberUpdated,
    session: AsyncSession,
) -> None:
    if event.chat.id != settings.COMMUNITY_CHAT_ID:
        return

    tg_user = event.new_chat_member.user
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    logger.info(
        "ChatMemberUpdated: user %s (%s) %s -> %s",
        tg_user.id,
        tg_user.first_name,
        old_status,
        new_status,
    )

    if _is_join(event):
        logger.info("Detected JOIN for user %s", tg_user.id)
        await _handle_join(event, session, tg_user)
    elif _is_leave(event):
        logger.info("Detected LEAVE for user %s", tg_user.id)
        await _handle_leave(session, tg_user)


async def _reject_join(
    event: ChatMemberUpdated,
    session: AsyncSession,
    tg_user,
    now: datetime,
    reason: str,
) -> None:
    logger.warning(
        "Rejecting join for user %s (%s): %s",
        tg_user.id,
        tg_user.username or tg_user.first_name,
        reason,
    )
    chat_id = event.chat.id

    # Check if this is the first kick (user not previously kicked)
    user = await UserRepo.get(session, tg_user.id)
    first_kick = user is None or user.left_at is None

    try:
        await event.bot.ban_chat_member(chat_id, tg_user.id)
        await event.bot.unban_chat_member(chat_id, tg_user.id)
    except Exception:
        logger.exception("Failed to kick user %s", tg_user.id)

    # Post message in chat only on first kick
    if first_kick:
        mention = f"@{tg_user.username}" if tg_user.username else tg_user.first_name
        mention = html_escape(mention)
        try:
            await event.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Мимо меня хотел(-а) пройти {mention}. "
                    f"Если хотите помочь человеку попасть сюда, "
                    f"скиньте ему мой юзернейм @vibeshkoder_bot"
                ),
            )
        except Exception:
            logger.exception("Failed to post kick message for user %s", tg_user.id)

    # Try to DM the user
    try:
        await event.bot.send_message(
            chat_id=tg_user.id,
            text=(
                "Привет! Чтобы попасть в чат вайб-шкодеров, нужно заполнить анкету. "
                "Напиши мне /start"
            ),
        )
    except Exception:
        logger.debug("Cannot DM kicked user %s (hasn't started bot)", tg_user.id)

    # Mark user as not member with left_at (so next kick is not "first")
    await UserRepo.set_member(session, tg_user.id, is_member=False, left_at=now)


async def _handle_join(
    event: ChatMemberUpdated,
    session: AsyncSession,
    tg_user,
) -> None:
    now = datetime.now(timezone.utc)

    # Capture membership before upsert so an old added admission cannot admit a former member.
    existing_user = await UserRepo.get(session, tg_user.id)
    was_member = bool(existing_user is not None and getattr(existing_user, "is_member", False))
    await UserRepo.upsert(
        session,
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        is_bot=getattr(tg_user, "is_bot", None),
    )

    is_admin = tg_user.id in settings.ADMIN_IDS
    active_app = None

    if is_admin:
        logger.info("Admin user %s joined without gatekeeper admission", tg_user.id)
    else:
        # The added state is an idempotent duplicate join for this exact admission.
        active_app = await ApplicationRepo.get_active(session, tg_user.id, include_added=was_member)
        rejection_reason = _admission_rejection_reason(active_app, tg_user.id)

        if rejection_reason is not None:
            await _reject_join(event, session, tg_user, now, rejection_reason)
            return

    if is_admin:
        await UserRepo.set_member(session, tg_user.id, is_member=True, joined_at=now)
        return

    enqueue_intro = False
    if active_app.status == "vouched":
        application_id = active_app.id
        added = await ApplicationRepo.update_status_if(
            session,
            application_id,
            expected_from="vouched",
            new_status="added",
            added_at=now,
        )
        if not added:
            active_app = await ApplicationRepo.get(session, application_id)
            if (
                active_app is None
                or active_app.id != application_id
                or active_app.status != "added"
                or _admission_rejection_reason(active_app, tg_user.id) is not None
            ):
                await _reject_join(event, session, tg_user, now, "admission CAS lost")
                return
        else:
            enqueue_intro = True

    await UserRepo.set_member(session, tg_user.id, is_member=True, joined_at=now)
    existing_intro = await IntroRepo.get(session, tg_user.id)
    if existing_intro is not None:
        logger.info("User %s already has intro, skipping intro creation on join", tg_user.id)
        return
    if enqueue_intro:
        await IntroEffectOutboxRepo.enqueue_once(
            session,
            application_id=active_app.id,
            effect_kind="admission_intro",
        )


@router.message(F.new_chat_members)
async def delete_join_service_message(message: Message) -> None:
    """Delete 'X joined the group' service messages to keep chat clean."""
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete join service message %s", message.message_id)


@router.message(F.left_chat_member)
async def delete_leave_service_message(message: Message) -> None:
    """Delete 'X left the group' service messages to keep chat clean."""
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        return
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete leave service message %s", message.message_id)


async def _handle_leave(
    session: AsyncSession,
    tg_user,
) -> None:
    now = datetime.now(timezone.utc)

    await UserRepo.set_member(session, tg_user.id, is_member=False, left_at=now)
