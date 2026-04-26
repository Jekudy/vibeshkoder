from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.questionnaire import QuestionnaireRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.states.questionnaire import STATES_LIST, QuestionnaireForm
from bot.texts import (
    ALREADY_HAS_INTRO,
    APPLICATION_PENDING,
    PRIVACY_BLOCK_MSG,
    REFRESH_NOT_MEMBER,
    REFRESH_NO_INTRO,
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

        if active_app.status == "filling":
            # Resume from last answered question
            last_idx = await QuestionnaireRepo.get_last_answered_index(
                session, tg_user.id, active_app.id
            )
            if last_idx is not None and last_idx < len(QUESTIONS) - 1:
                next_idx = last_idx + 1
                await state.update_data(application_id=active_app.id)
                await state.set_state(STATES_LIST[next_idx])
                await message.answer(
                    RESUME_QUESTIONNAIRE.format(question=QUESTIONS[next_idx])
                )
            elif last_idx is not None and last_idx == len(QUESTIONS) - 1:
                # All questions answered, go to confirm
                await state.update_data(application_id=active_app.id)
                await _show_confirm(message, state, session, tg_user.id, active_app.id)
            else:
                # No answers yet, start from beginning
                await state.update_data(application_id=active_app.id)
                await state.set_state(QuestionnaireForm.q1_name)
                await message.answer(
                    RESUME_QUESTIONNAIRE.format(question=QUESTIONS[0])
                )
            return

        if active_app.status == "vouched":
            await message.answer(VOUCHED_PENDING)
            return

    # Check if member without intro → existing member flow
    intro = await IntroRepo.get(session, tg_user.id)

    if user.is_member and intro is None:
        app = await ApplicationRepo.create(session, tg_user.id)
        await state.update_data(application_id=app.id, is_existing_member=True)
        await state.set_state(QuestionnaireForm.q1_name)
        await message.answer(
            WELCOME_EXISTING_MEMBER.format(question=QUESTIONS[0])
        )
        return

    if user.is_member and intro is not None:
        await message.answer(ALREADY_HAS_INTRO)
        return

    # Check previously rejected → allow new application
    # (get_active already returned None, so no active app)
    # New applicant
    app = await ApplicationRepo.create(session, tg_user.id)
    await state.update_data(application_id=app.id, is_existing_member=False)
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
    )

    if not user.is_member:
        await message.answer(REFRESH_NOT_MEMBER)
        return

    intro = await IntroRepo.get(session, tg_user.id)
    if intro is None:
        await message.answer(REFRESH_NO_INTRO)
        return

    # Mark old questionnaire answers as not current
    await QuestionnaireRepo.mark_not_current(session, tg_user.id)

    # Create a new application for the refresh flow
    app = await ApplicationRepo.create(session, tg_user.id)
    await state.clear()
    await state.update_data(
        application_id=app.id,
        is_existing_member=True,
        is_refresh=True,
    )
    await state.set_state(QuestionnaireForm.q1_name)
    await message.answer(REFRESH_START.format(question=QUESTIONS[0]))


async def _show_confirm(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user_id: int,
    application_id: int,
) -> None:
    from bot.handlers.questionnaire import build_intro_preview

    answers = await QuestionnaireRepo.get_answers(
        session, user_id, application_id=application_id
    )
    intro_text = build_intro_preview(answers)
    from bot.texts import CONFIRM_PROMPT
    from bot.keyboards.inline import confirm_keyboard

    await state.set_state(QuestionnaireForm.confirm)
    await message.answer(
        CONFIRM_PROMPT.format(intro_text=intro_text),
        reply_markup=confirm_keyboard(),
    )
