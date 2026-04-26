from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import QuestionnaireAnswer
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.intro import IntroRepo
from bot.db.repos.questionnaire import QuestionnaireRepo
from bot.db.repos.user import UserRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.keyboards.inline import (
    ConfirmCallback,
    confirm_keyboard,
    vouch_keyboard,
)
from bot.states.questionnaire import STATES_LIST, QuestionnaireForm
from bot.texts import (
    CONFIRM_PROMPT,
    INTRO_TEMPLATE,
    NEXT_QUESTION,
    NOT_TEXT_ERROR,
    QUESTIONS,
    QUESTIONNAIRE_POSTED,
)

logger = logging.getLogger(__name__)

router = Router(name="questionnaire")


def build_intro_preview(answers: list[QuestionnaireAnswer]) -> str:
    """Build formatted intro text from questionnaire answers."""
    answers_by_idx = {a.question_index: a.answer_text for a in answers}
    return INTRO_TEMPLATE.format(
        name=answers_by_idx.get(0, "—"),
        location=answers_by_idx.get(1, "—"),
        source=answers_by_idx.get(2, "—"),
        experience=answers_by_idx.get(3, "—"),
        projects=answers_by_idx.get(4, "—"),
        hardest=answers_by_idx.get(5, "—"),
        goals=answers_by_idx.get(6, "—"),
    )


def _get_current_index(state_name: str) -> int | None:
    """Return question index (0-6) for the given FSM state, or None."""
    for i, s in enumerate(STATES_LIST):
        if s.state == state_name:
            return i
    return None


# ── Answer handler for all 7 question states ────────────────────────

@router.message(
    QuestionnaireForm.q1_name,
    PrivateChatFilter(),
    F.content_type == "text",
)
@router.message(
    QuestionnaireForm.q2_location,
    PrivateChatFilter(),
    F.content_type == "text",
)
@router.message(
    QuestionnaireForm.q3_source,
    PrivateChatFilter(),
    F.content_type == "text",
)
@router.message(
    QuestionnaireForm.q4_experience,
    PrivateChatFilter(),
    F.content_type == "text",
)
@router.message(
    QuestionnaireForm.q5_projects,
    PrivateChatFilter(),
    F.content_type == "text",
)
@router.message(
    QuestionnaireForm.q6_hardest,
    PrivateChatFilter(),
    F.content_type == "text",
)
@router.message(
    QuestionnaireForm.q7_goals,
    PrivateChatFilter(),
    F.content_type == "text",
)
async def handle_answer(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if message.from_user is None or message.text is None:
        return

    current_state = await state.get_state()
    if current_state is None:
        return

    idx = _get_current_index(current_state)
    if idx is None:
        return

    data = await state.get_data()
    application_id = data.get("application_id")
    if application_id is None:
        return

    # Save answer
    await QuestionnaireRepo.save_answer(
        session,
        user_id=message.from_user.id,
        application_id=application_id,
        question_index=idx,
        question_text=QUESTIONS[idx],
        answer_text=message.text,
    )

    # Advance to next state or confirm
    if idx < len(QUESTIONS) - 1:
        next_idx = idx + 1
        await state.set_state(STATES_LIST[next_idx])
        await message.answer(NEXT_QUESTION.format(question=QUESTIONS[next_idx]))
    else:
        # All questions answered → show confirmation
        answers = await QuestionnaireRepo.get_answers(
            session,
            message.from_user.id,
            application_id=application_id,
        )
        intro_text = build_intro_preview(answers)
        await state.set_state(QuestionnaireForm.confirm)
        await message.answer(
            CONFIRM_PROMPT.format(intro_text=intro_text),
            reply_markup=confirm_keyboard(),
        )


# ── Non-text message error for question states ──────────────────────

@router.message(
    QuestionnaireForm.q1_name, PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q2_location, PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q3_source, PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q4_experience, PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q5_projects, PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q6_hardest, PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q7_goals, PrivateChatFilter(),
)
async def handle_non_text(message: Message) -> None:
    await message.answer(NOT_TEXT_ERROR)


# ── Confirm callback ────────────────────────────────────────────────

@router.callback_query(ConfirmCallback.filter())
async def handle_confirm(
    callback: CallbackQuery,
    callback_data: ConfirmCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None or callback.message is None:
        return

    data = await state.get_data()
    application_id = data.get("application_id")
    is_existing_member = data.get("is_existing_member", False)

    if callback_data.action == "redo":
        # Delete answers and restart
        if application_id is not None:
            await QuestionnaireRepo.delete_answers(
                session,
                callback.from_user.id,
                application_id=application_id,
            )
        await state.set_state(QuestionnaireForm.q1_name)
        await callback.message.edit_text(
            NEXT_QUESTION.format(question=QUESTIONS[0])
        )
        await callback.answer()
        return

    if callback_data.action == "yes":
        if application_id is None:
            await callback.answer("Ошибка: заявка не найдена.")
            return

        answers = await QuestionnaireRepo.get_answers(
            session,
            callback.from_user.id,
            application_id=application_id,
        )
        intro_text = build_intro_preview(answers)

        user = await UserRepo.get(session, callback.from_user.id)
        user_display = user.first_name if user else callback.from_user.first_name
        username = user.username if user else callback.from_user.username

        if is_existing_member:
            is_refresh = data.get("is_refresh", False)

            if is_refresh:
                # Refresh: preserve vouched_by_name from existing intro
                existing_intro = await IntroRepo.get(
                    session, callback.from_user.id
                )
                vouched_by_name = (
                    existing_intro.vouched_by_name
                    if existing_intro
                    else "времена до бота"
                )
            else:
                vouched_by_name = "времена до бота"

            await IntroRepo.upsert(
                session,
                user_id=callback.from_user.id,
                intro_text=intro_text,
                vouched_by_name=vouched_by_name,
            )
            await ApplicationRepo.update_status(
                session, application_id, "added"
            )
            await state.clear()

            # Post intro in community chat (without vouch button)
            header = f"📋 Интро: {user_display}"
            if username:
                header += f" (@{username})"
            header += "\n\n"
            try:
                await callback.bot.send_message(
                    chat_id=settings.COMMUNITY_CHAT_ID,
                    text=header + intro_text,
                )
            except Exception:
                pass

            if is_refresh:
                from bot.texts import REFRESH_SAVED

                await callback.message.edit_text(REFRESH_SAVED)
            else:
                await callback.message.edit_text(
                    "Интро сохранено! Спасибо."
                )
        else:
            # New applicant: post to community chat
            header = f"📋 Новая анкета от {user_display}"
            if username:
                header += f" (@{username})"
            header += "\n\n"

            msg = await callback.bot.send_message(
                chat_id=settings.COMMUNITY_CHAT_ID,
                text=header + intro_text,
                reply_markup=vouch_keyboard(application_id),
            )
            await ApplicationRepo.update_status(
                session,
                application_id,
                "pending",
                questionnaire_message_id=msg.message_id,
                submitted_at=datetime.now(timezone.utc),
            )
            await state.clear()
            await callback.message.edit_text(QUESTIONNAIRE_POSTED)

        await callback.answer()
