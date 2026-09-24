from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Application, Intro, IntroRefreshTracking, User
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.questionnaire import QuestionnaireRepo
from bot.keyboards.inline import (
    IntroRefreshEditCallback,
    IntroRefreshOfferCallback,
    IntroRefreshSelectCallback,
    intro_refresh_cancel_keyboard,
    intro_refresh_edit_keyboard,
    intro_refresh_legacy_keyboard,
    intro_refresh_selection_keyboard,
)
from bot.services.intro_contract import get_intro_catalog
from bot.services.intro_refresh_wave import (
    split_expandable_template,
    wave_from_token,
    wave_token,
)
from bot.services.intro_workflow import (
    IntroWorkflowError,
    cancel_refresh,
    keep_base_answer,
    next_field_id,
    refresh_base_answer,
    restart_refresh_selection,
    start_or_resume_refresh,
    verify_refresh_preview,
)
from bot.services.referral_username import is_concrete_referral
from bot.states.questionnaire import STATES_LIST
from bot.texts import (
    REFRESH_CANCELLED,
    REFRESH_CANCEL_CONFIRM,
    REFRESH_DECLINED,
    REFRESH_LEGACY,
    REFRESH_RESELECT_WARNING,
    REFRESH_SELECTION,
    REFRESH_START,
)


router = Router(name="intro_refresh")
CATALOG = get_intro_catalog("intro-v2")
FIELD_IDS = tuple(field.field_id for field in CATALOG)


async def pending_refresh_selection_context(session: AsyncSession, user_id: int) -> str | None:
    result = await session.execute(
        select(IntroRefreshTracking.cycle_started_at)
        .where(
            IntroRefreshTracking.user_id == user_id,
            IntroRefreshTracking.phase == "accepted",
        )
        .order_by(IntroRefreshTracking.cycle_started_at.desc())
        .limit(1)
    )
    wave = result.scalar_one_or_none()
    return f"w{wave_token(wave)}" if wave is not None else None


async def _source_values(
    session: AsyncSession, user_id: int
) -> tuple[Intro, dict[str, str] | None]:
    user = await session.get(User, user_id)
    if user is None or not user.is_member:
        raise IntroWorkflowError("Refresh requires a current member")
    intro = await IntroRepo.get(session, user_id)
    if intro is None:
        raise IntroWorkflowError("Member has no intro")
    if intro.application_id is None:
        return intro, None
    answers = await QuestionnaireRepo.get_by_application(
        session, application_id=intro.application_id
    )
    values = {
        answer.field_id: answer.answer_text for answer in answers if answer.field_id is not None
    }
    if set(values) != set(FIELD_IDS):
        return intro, None
    return intro, values


def _selectable_fields(values: dict[str, str]) -> list[tuple[int, str]]:
    return [
        (index, field.public_label)
        for index, field in enumerate(CATALOG)
        if field.field_id != "referral" or not is_concrete_referral(values["referral"])
    ]


async def show_refresh_selection(
    message: Message,
    session: AsyncSession,
    user_id: int,
    *,
    edit: bool,
    mask: int = 0,
    context: str = "manual",
) -> None:
    intro, values = await _source_values(session, user_id)
    source_application_id = intro.application_id or 0
    close_text = "Вернуться к предпросмотру" if context.startswith("a") else "Отмена"
    if values is None:
        template = REFRESH_LEGACY.replace("{count}", str(len(CATALOG)))
        markup = intro_refresh_legacy_keyboard(
            source_application_id,
            context,
            close_text=close_text,
        )
    else:
        template = REFRESH_SELECTION
        markup = intro_refresh_selection_keyboard(
            _selectable_fields(values),
            mask,
            source_application_id,
            context,
            close_text=close_text,
        )
    if context.startswith("a"):
        template += REFRESH_RESELECT_WARNING
    parts = split_expandable_template(template, intro.intro_text)
    if edit:
        await message.edit_text(parts[0], reply_markup=markup if len(parts) == 1 else None)
        parts = parts[1:]
    for index, part in enumerate(parts):
        await message.answer(
            part,
            reply_markup=markup if index == len(parts) - 1 else None,
        )


async def show_refresh_question(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    field_id: str,
    edit: bool,
) -> None:
    index = FIELD_IDS.index(field_id)
    old_text = await refresh_base_answer(
        session,
        user_id=user_id,
        application_id=application_id,
        field_id=field_id,
    )
    if old_text is None:
        text = REFRESH_START.format(question=CATALOG[index].question)
    else:
        copy_hint = (
            "Нажми «Скопировать текст», вставь его ответом и отредактируй."
            if len(old_text) <= 256
            else "Зажми текст, скопируй его, вставь ответом и отредактируй."
        )
        text = (
            f"❓ <b>{CATALOG[index].question}</b>\n\n"
            f"Текущий текст:\n<pre>{escape(old_text, quote=True)}</pre>\n\n{copy_hint}"
        )
    await state.update_data(application_id=application_id)
    await state.set_state(STATES_LIST[index])
    markup = intro_refresh_edit_keyboard(application_id, field_id, old_text)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def show_refresh_step(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    user_id: int,
    application_id: int,
    field_id: str | None,
    edit: bool,
) -> None:
    if field_id is not None:
        await show_refresh_question(
            message,
            state,
            session,
            user_id=user_id,
            application_id=application_id,
            field_id=field_id,
            edit=edit,
        )
        return
    from bot.handlers.questionnaire import show_confirm

    await show_confirm(
        message,
        state,
        session,
        user_id,
        application_id,
        edit=edit,
    )


@router.callback_query(IntroRefreshOfferCallback.filter())
async def handle_refresh_offer(
    callback: CallbackQuery,
    callback_data: IntroRefreshOfferCallback,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    try:
        wave = wave_from_token(callback_data.wave)
    except ValueError:
        await callback.answer("Эта волна обновления устарела.", show_alert=True)
        return
    result = await session.execute(
        select(IntroRefreshTracking)
        .where(
            IntroRefreshTracking.user_id == callback.from_user.id,
            IntroRefreshTracking.cycle_started_at == wave,
        )
        .with_for_update()
    )
    tracking = result.scalar_one_or_none()
    if tracking is None:
        await callback.answer("Эта волна обновления устарела.", show_alert=True)
        return

    if tracking.phase == "claimed":
        tracking.reminders_sent = 1
        tracking.last_reminder_at = datetime.now(timezone.utc)
        tracking.phase = "offer_sent"
        tracking.completed = True

    if callback_data.action == "no" and tracking.phase in {"offer_sent", "declined"}:
        tracking.phase = "declined"
        await callback.message.edit_text(REFRESH_DECLINED)
        await callback.answer()
        return
    if callback_data.action == "yes" and tracking.phase in {"offer_sent", "accepted"}:
        try:
            await show_refresh_selection(
                callback.message,
                session,
                callback.from_user.id,
                edit=True,
                context=f"w{callback_data.wave}",
            )
        except IntroWorkflowError:
            await callback.answer("Интро больше недоступно.", show_alert=True)
            return
        tracking.phase = "accepted"
        await callback.answer()
        return
    await callback.answer("Ответ уже сохранён.", show_alert=True)


@router.callback_query(IntroRefreshSelectCallback.filter())
async def handle_refresh_selection(
    callback: CallbackQuery,
    callback_data: IntroRefreshSelectCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    try:
        intro, values = await _source_values(session, callback.from_user.id)
    except IntroWorkflowError:
        await callback.answer("Интро больше недоступно.", show_alert=True)
        return
    if callback_data.source_application_id != (intro.application_id or 0):
        await callback.answer("Этот выбор устарел. Запусти /refresh ещё раз.", show_alert=True)
        return

    active_application: Application | None = None
    active_digest: str | None = None
    tracking: IntroRefreshTracking | None = None
    context = callback_data.context
    if context == "manual":
        if await ApplicationRepo.get_active_refresh(session, callback.from_user.id) is not None:
            await callback.answer("Обновление уже начато. Продолжи через /start.", show_alert=True)
            return
    elif context.startswith("w"):
        try:
            wave = wave_from_token(context[1:])
        except ValueError:
            await callback.answer("Эта волна обновления устарела.", show_alert=True)
            return
        result = await session.execute(
            select(IntroRefreshTracking)
            .where(
                IntroRefreshTracking.user_id == callback.from_user.id,
                IntroRefreshTracking.cycle_started_at == wave,
            )
            .with_for_update()
        )
        tracking = result.scalar_one_or_none()
        if tracking is None or tracking.phase not in {"claimed", "offer_sent", "accepted"}:
            await callback.answer("Эта волна обновления устарела.", show_alert=True)
            return
        if await ApplicationRepo.get_active_refresh(session, callback.from_user.id) is not None:
            await callback.answer("Обновление уже начато. Продолжи через /start.", show_alert=True)
            return
        if tracking.phase == "claimed":
            tracking.reminders_sent = 1
            tracking.last_reminder_at = datetime.now(timezone.utc)
            tracking.completed = True
        tracking.phase = "accepted"
    elif context.startswith("a"):
        try:
            application_id, active_digest = context[1:].split(".", 1)
            active_application = await verify_refresh_preview(
                session,
                user_id=callback.from_user.id,
                application_id=int(application_id),
                digest=active_digest,
            )
        except (IntroWorkflowError, TypeError, ValueError):
            await callback.answer("Этот выбор устарел. Запусти /refresh ещё раз.", show_alert=True)
            return
        if (active_application.base_application_id or 0) != callback_data.source_application_id:
            await callback.answer("Этот выбор устарел. Запусти /refresh ещё раз.", show_alert=True)
            return
    else:
        await callback.answer("Этот выбор устарел. Запусти /refresh ещё раз.", show_alert=True)
        return

    if callback_data.action == "close":
        if active_application is not None:
            await show_refresh_step(
                callback.message,
                state,
                session,
                user_id=callback.from_user.id,
                application_id=active_application.id,
                field_id=None,
                edit=True,
            )
        else:
            if tracking is not None:
                tracking.phase = "cancelled"
            await callback.message.edit_text(REFRESH_CANCELLED)
        await callback.answer()
        return

    if callback_data.action == "legacy":
        if values is not None:
            await callback.answer("Это интро уже разбито на блоки.", show_alert=True)
            return
        try:
            if active_application is not None:
                field_id = await restart_refresh_selection(
                    session,
                    user_id=callback.from_user.id,
                    application_id=active_application.id,
                    digest=active_digest or "",
                    editable_field_ids=None,
                )
                application = active_application
            else:
                application = await start_or_resume_refresh(session, user_id=callback.from_user.id)
                field_id = await next_field_id(
                    session,
                    user_id=callback.from_user.id,
                    application_id=application.id,
                )
            if tracking is not None:
                tracking.phase = "started"
            await state.clear()
            await show_refresh_step(
                callback.message,
                state,
                session,
                user_id=callback.from_user.id,
                application_id=application.id,
                field_id=field_id,
                edit=True,
            )
        except IntroWorkflowError:
            await callback.answer("Не удалось начать обновление.", show_alert=True)
            return
        await callback.answer()
        return

    if values is None:
        await callback.answer("Это интро обновляется целиком.", show_alert=True)
        return
    selectable = _selectable_fields(values)
    selectable_indexes = {index for index, _ in selectable}
    if callback_data.action == "toggle":
        if callback_data.field_index not in selectable_indexes:
            await callback.answer("Этот блок недоступен.", show_alert=True)
            return
        mask = callback_data.mask ^ (1 << callback_data.field_index)
        await callback.message.edit_reply_markup(
            reply_markup=intro_refresh_selection_keyboard(
                selectable,
                mask,
                callback_data.source_application_id,
                context,
                close_text=(
                    "Вернуться к предпросмотру" if active_application is not None else "Отмена"
                ),
            )
        )
        await callback.answer()
        return
    if callback_data.action != "continue":
        await callback.answer()
        return
    selected = {
        FIELD_IDS[index] for index in selectable_indexes if callback_data.mask & (1 << index)
    }
    if not selected:
        await callback.answer("Выбери хотя бы один блок.", show_alert=True)
        return
    try:
        if active_application is not None:
            field_id = await restart_refresh_selection(
                session,
                user_id=callback.from_user.id,
                application_id=active_application.id,
                digest=active_digest or "",
                editable_field_ids=selected,
            )
            application = active_application
        else:
            application = await start_or_resume_refresh(
                session,
                user_id=callback.from_user.id,
                editable_field_ids=selected,
            )
            field_id = await next_field_id(
                session,
                user_id=callback.from_user.id,
                application_id=application.id,
            )
        if tracking is not None:
            tracking.phase = "started"
        await state.clear()
        await show_refresh_step(
            callback.message,
            state,
            session,
            user_id=callback.from_user.id,
            application_id=application.id,
            field_id=field_id,
            edit=True,
        )
    except IntroWorkflowError:
        await callback.answer("Выбор устарел. Запусти /refresh ещё раз.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(IntroRefreshEditCallback.filter())
async def handle_refresh_edit(
    callback: CallbackQuery,
    callback_data: IntroRefreshEditCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    try:
        field_id = await next_field_id(
            session,
            user_id=callback.from_user.id,
            application_id=callback_data.application_id,
        )
        if field_id != callback_data.field_id:
            await callback.answer("Этот блок уже обработан.", show_alert=True)
            return
        if callback_data.action == "cancel":
            await callback.message.edit_text(
                REFRESH_CANCEL_CONFIRM,
                reply_markup=intro_refresh_cancel_keyboard(
                    callback_data.application_id, callback_data.field_id
                ),
            )
            await callback.answer()
            return
        if callback_data.action == "confirm_cancel":
            await cancel_refresh(
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
            )
            await state.clear()
            await callback.message.edit_text(REFRESH_CANCELLED)
            await callback.answer()
            return
        if callback_data.action == "keep_editing":
            await show_refresh_step(
                callback.message,
                state,
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
                field_id=field_id,
                edit=True,
            )
            await callback.answer()
            return
        if callback_data.action == "skip" and field_id is not None:
            field_id = await keep_base_answer(
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
                field_id=field_id,
            )
            await show_refresh_step(
                callback.message,
                state,
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
                field_id=field_id,
                edit=True,
            )
            await callback.answer()
            return
    except IntroWorkflowError:
        await callback.answer("Обновление устарело. Запусти /refresh ещё раз.", show_alert=True)
        return
    await callback.answer()
