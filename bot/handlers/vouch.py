from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Application
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.user import UserRepo
from bot.db.repos.vouch import VouchRepo
from bot.html_escape import html_escape
from bot.keyboards.inline import ReadyCallback, VouchCallback, ready_keyboard
from bot.services.invite import try_send_invite
from bot.texts import (
    ALREADY_PROCESSED,
    CANT_VOUCH_SELF,
    ONLY_MEMBERS_VOUCH,
    PRIVACY_BLOCK_MSG,
    VOUCHED_NOTIFICATION,
)

logger = logging.getLogger(__name__)

router = Router(name="vouch")


@router.callback_query(VouchCallback.filter())
async def handle_vouch(
    callback: CallbackQuery,
    callback_data: VouchCallback,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return

    voucher_id = callback.from_user.id
    app_id = callback_data.application_id

    # Load application
    app = await ApplicationRepo.get(session, app_id)
    if app is None:
        await callback.answer(ALREADY_PROCESSED, show_alert=True)
        return

    # Verify clicker is member
    voucher = await UserRepo.get(session, voucher_id)
    if voucher is None or not voucher.is_member:
        await callback.answer(ONLY_MEMBERS_VOUCH, show_alert=True)
        return

    # Cannot vouch for self
    if voucher_id == app.user_id:
        await callback.answer(CANT_VOUCH_SELF, show_alert=True)
        return

    # Optimistic lock: UPDATE WHERE status='pending' RETURNING id
    result = await session.execute(
        update(Application)
        .where(Application.id == app_id, Application.status == "pending")
        .values(
            status="vouched",
            vouched_by=voucher_id,
            vouched_at=datetime.now(timezone.utc),
            invite_user_id=app.user_id,
        )
        .returning(Application.id)
    )
    updated_id = result.scalar_one_or_none()
    await session.flush()

    if updated_id is None:
        await callback.answer(ALREADY_PROCESSED, show_alert=True)
        return

    # Insert vouch log
    await VouchRepo.create(
        session,
        voucher_id=voucher_id,
        vouchee_id=app.user_id,
        application_id=app_id,
    )

    # Delete questionnaire message from chat
    if callback.message is not None:
        try:
            await callback.bot.delete_message(
                chat_id=settings.COMMUNITY_CHAT_ID,
                message_id=callback.message.message_id,
            )
        except Exception:
            logger.warning(
                "Failed to delete questionnaire message %s",
                callback.message.message_id,
            )

    # Notify applicant and send invite
    voucher_name = f"@{voucher.username}" if voucher.username else voucher.first_name
    try:
        await callback.bot.send_message(
            chat_id=app.user_id,
            text=VOUCHED_NOTIFICATION.format(name=html_escape(voucher_name)),
        )
    except Exception:
        logger.warning("Failed to notify user %s about vouch", app.user_id)

    # Try to send invite link
    success, _link = await try_send_invite(
        callback.bot, settings.COMMUNITY_CHAT_ID, app.user_id, app_id
    )

    if not success:
        # Privacy block
        await ApplicationRepo.update_status(session, app_id, "privacy_block")
        try:
            await callback.bot.send_message(
                chat_id=app.user_id,
                text=PRIVACY_BLOCK_MSG,
                reply_markup=ready_keyboard(app_id),
            )
        except Exception:
            logger.warning(
                "Failed to send privacy block message to user %s", app.user_id
            )

    await callback.answer("Готово! Спасибо за ручательство.")


@router.callback_query(ReadyCallback.filter())
async def handle_ready(
    callback: CallbackQuery,
    callback_data: ReadyCallback,
    session: AsyncSession,
) -> None:
    """Handle "Я готов" button for privacy_block retry."""
    if callback.from_user is None:
        return

    app_id = callback_data.application_id

    app = await ApplicationRepo.get(session, app_id)
    if app is None or app.status != "privacy_block":
        await callback.answer(ALREADY_PROCESSED, show_alert=True)
        return

    if callback.from_user.id != app.user_id:
        await callback.answer("Эта кнопка не для тебя.", show_alert=True)
        return

    # Retry sending invite
    success, _link = await try_send_invite(
        callback.bot, settings.COMMUNITY_CHAT_ID, app.user_id, app_id
    )

    if success:
        await ApplicationRepo.update_status(
            session, app_id, "vouched", invite_user_id=app.user_id
        )
        if callback.message is not None:
            await callback.message.edit_text(
                "Инвайт отправлен! Проверь личные сообщения."
            )
        await callback.answer()
    else:
        await callback.answer(
            "Всё ещё не удаётся отправить инвайт. "
            "Проверь настройки приватности и попробуй снова.",
            show_alert=True,
        )
