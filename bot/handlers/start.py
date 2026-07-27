from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.services.intro_contract import get_intro_catalog, intro_digest
from bot.services.intro_workflow import (
    IntroWorkflowError,
    next_field_id,
    start_or_resume_refresh,
)
from bot.states.questionnaire import STATES_LIST, QuestionnaireForm
from bot.texts import (
    ALREADY_HAS_INTRO,
    APPLICATION_PENDING,
    PRIVACY_BLOCK_MSG,
    QUESTIONNAIRE_POSTED,
    REFRESH_NOT_MEMBER,
    REFRESH_SAVED,
    REFRESH_START,
    RESUME_QUESTIONNAIRE,
    VOUCHED_PENDING,
    WELCOME_EXISTING_MEMBER,
    WELCOME_NEW,
    QUESTIONS,
)
from bot.keyboards.inline import ready_keyboard

logger = logging.getLogger(__name__)

router = Router(name="start")
FIELD_IDS = tuple(field.field_id for field in get_intro_catalog("intro-v2"))


@router.message(CommandStart(), PrivateChatFilter())
async def cmd_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return

    # Upsert user
    user = await UserRepo.upsert(
        session,
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        is_bot=getattr(tg_user, "is_bot", None),
    )

    # Check for active application (filling / pending / privacy_block)
    active_app = await ApplicationRepo.get_active(session, tg_user.id)

    if active_app is not None:
        if active_app.status == "pending":
            await message.answer(APPLICATION_PENDING)
            return

        if active_app.status == "privacy_block":
            await message.answer(
                PRIVACY_BLOCK_MSG,
                reply_markup=ready_keyboard(active_app.id),
            )
            return

        if active_app.status == "confirmed":
            await message.answer(QUESTIONNAIRE_POSTED)
            return

        if active_app.status == "filling":
            try:
                next_field = await next_field_id(
                    session, user_id=tg_user.id, application_id=active_app.id
                )
            except IntroWorkflowError:
                await message.answer("Анкета устарела. Запусти /start ещё раз.")
                return
            if next_field is not None:
                next_idx = FIELD_IDS.index(next_field)
                await state.update_data(application_id=active_app.id)
                await state.set_state(STATES_LIST[next_idx])
                await message.answer(RESUME_QUESTIONNAIRE.format(question=QUESTIONS[next_idx]))
            else:
                await state.update_data(application_id=active_app.id)
                await _show_confirm(message, state, session, tg_user.id, active_app.id)
            return

        if active_app.status == "vouched":
            await message.answer(VOUCHED_PENDING)
            return

    # Check if member without intro → existing member flow
    intro = await IntroRepo.get(session, tg_user.id)

    if user.is_member and intro is None:
        app = await start_or_resume_refresh(session, user_id=tg_user.id)
        if app.status == "confirmed":
            await message.answer(REFRESH_SAVED)
            return
        first_field = await next_field_id(session, user_id=tg_user.id, application_id=app.id)
        if first_field is None:
            await _show_confirm(message, state, session, tg_user.id, app.id)
            return
        first_idx = FIELD_IDS.index(first_field)
        await state.update_data(application_id=app.id)
        await state.set_state(STATES_LIST[first_idx])
        await message.answer(WELCOME_EXISTING_MEMBER.format(question=QUESTIONS[first_idx]))
        return

    if user.is_member and intro is not None:
        await message.answer(ALREADY_HAS_INTRO)
        return

    # Check previously rejected → allow new application
    # (get_active already returned None, so no active app)
    # New applicant
    app = await ApplicationRepo.create(
        session,
        user_id=tg_user.id,
        flow_kind="admission",
        base_application_id=None,
        catalog_version="intro-v2",
    )
    await state.update_data(application_id=app.id)
    await state.set_state(QuestionnaireForm.q1_name)
    await message.answer(WELCOME_NEW.format(question=QUESTIONS[0]))


@router.message(Command("refresh"), PrivateChatFilter())
async def cmd_refresh(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return

    user = await UserRepo.upsert(
        session,
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        is_bot=getattr(tg_user, "is_bot", None),
    )

    if not user.is_member:
        await message.answer(REFRESH_NOT_MEMBER)
        return

    try:
        app = await start_or_resume_refresh(session, user_id=tg_user.id)
        if app.status == "confirmed":
            await message.answer(REFRESH_SAVED)
            return
        first_field = await next_field_id(session, user_id=tg_user.id, application_id=app.id)
    except IntroWorkflowError:
        await message.answer(REFRESH_NOT_MEMBER)
        return
    if first_field is None:
        await _show_confirm(message, state, session, tg_user.id, app.id)
        return
    first_idx = FIELD_IDS.index(first_field)
    await state.clear()
    await state.update_data(application_id=app.id)
    await state.set_state(STATES_LIST[first_idx])
    await message.answer(REFRESH_START.format(question=QUESTIONS[first_idx]))


async def _show_confirm(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user_id: int,
    application_id: int,
) -> None:
    from bot.handlers.questionnaire import build_intro_preview

    from bot.db.repos.questionnaire import QuestionnaireRepo

    answers = await QuestionnaireRepo.get_by_application(session, application_id=application_id)
    intro_text = build_intro_preview(answers)
    from bot.texts import CONFIRM_PROMPT
    from bot.keyboards.inline import confirm_keyboard

    await state.set_state(QuestionnaireForm.confirm)
    await message.answer(
        CONFIRM_PROMPT.format(intro_text=intro_text),
        reply_markup=confirm_keyboard(application_id, intro_digest(intro_text)),
    )
