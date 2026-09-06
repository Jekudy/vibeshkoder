from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Application, QuestionnaireAnswer
from bot.db.repos.application import ApplicationRepo
from bot.db.repos.questionnaire import QuestionnaireRepo
from bot.filters.chat_type import PrivateChatFilter
from bot.keyboards.inline import (
    ConfirmCallback,
    confirm_keyboard,
)
from bot.services.intro_contract import get_intro_catalog, intro_digest, render_intro_html
from bot.services.intro_workflow import (
    IntroWorkflowError,
    InvalidReferralAnswer,
    confirm_application,
    cancel_refresh,
    reset_draft,
    verify_refresh_preview,
    write_answer,
)
from bot.states.questionnaire import STATES_LIST, QuestionnaireForm
from bot.texts import (
    CONFIRM_PROMPT,
    INVALID_REFERRAL_USERNAME,
    NEXT_QUESTION,
    NOT_TEXT_ERROR,
    QUESTIONS,
    QUESTIONNAIRE_POSTED,
    REFRESH_CANCELLED,
    REFRESH_SAVED,
)

logger = logging.getLogger(__name__)

router = Router(name="questionnaire")
FIELD_IDS = tuple(field.field_id for field in get_intro_catalog("intro-v2"))


def build_intro_preview(answers: list[QuestionnaireAnswer]) -> str:
    """Build formatted intro text from questionnaire answers."""
    return render_intro_html(
        [(answer.field_id, answer.answer_text) for answer in answers],
        catalog_version="intro-v2",
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

    field_id = FIELD_IDS[idx]
    try:
        next_field_id = await write_answer(
            session,
            user_id=message.from_user.id,
            application_id=application_id,
            field_id=field_id,
            answer_text=message.text,
        )
    except InvalidReferralAnswer:
        await message.answer(INVALID_REFERRAL_USERNAME)
        return
    except IntroWorkflowError:
        await message.answer("Анкета устарела. Запусти /start ещё раз.")
        return

    # Advance to next state or confirm
    application = await session.get(Application, application_id)
    if next_field_id is not None:
        if application is not None and application.flow_kind == "refresh":
            from bot.handlers.intro_refresh import show_refresh_step

            await show_refresh_step(
                message,
                state,
                session,
                user_id=message.from_user.id,
                application_id=application_id,
                field_id=next_field_id,
                edit=False,
            )
            return
        next_idx = FIELD_IDS.index(next_field_id)
        await state.set_state(STATES_LIST[next_idx])
        await message.answer(NEXT_QUESTION.format(question=QUESTIONS[next_idx]))
    else:
        await show_confirm(
            message,
            state,
            session,
            message.from_user.id,
            application_id,
            edit=False,
        )


async def show_confirm(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user_id: int,
    application_id: int,
    *,
    edit: bool,
) -> None:
    application = await ApplicationRepo.get(session, application_id)
    if application is None or application.user_id != user_id:
        raise IntroWorkflowError("Questionnaire is not owned by this user")
    answers = await QuestionnaireRepo.get_by_application(session, application_id=application_id)
    intro_text = build_intro_preview(answers)
    await state.set_state(QuestionnaireForm.confirm)
    markup = confirm_keyboard(
        application_id,
        intro_digest(intro_text),
        redo_text=(
            "Изменить выбор блоков" if application.flow_kind == "refresh" else "Заполнить заново 🔄"
        ),
        cancel_text="Отменить обновление" if application.flow_kind == "refresh" else None,
    )
    text = CONFIRM_PROMPT.format(intro_text=intro_text)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


# ── Non-text message error for question states ──────────────────────


@router.message(
    QuestionnaireForm.q1_name,
    PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q2_location,
    PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q3_source,
    PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q4_experience,
    PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q5_projects,
    PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q6_hardest,
    PrivateChatFilter(),
)
@router.message(
    QuestionnaireForm.q7_goals,
    PrivateChatFilter(),
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

    if callback_data.action == "cancel":
        try:
            application = await verify_refresh_preview(
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
                digest=callback_data.digest,
            )
            await cancel_refresh(
                session,
                user_id=callback.from_user.id,
                application_id=application.id,
            )
        except IntroWorkflowError:
            await callback.answer("Эта анкета устарела. Запусти /refresh ещё раз.")
            return
        await state.clear()
        await callback.message.edit_text(REFRESH_CANCELLED)
        await callback.answer()
        return

    if callback_data.action == "redo":
        application = await ApplicationRepo.get(session, callback_data.application_id)
        if application is not None and application.flow_kind == "refresh":
            try:
                await verify_refresh_preview(
                    session,
                    user_id=callback.from_user.id,
                    application_id=callback_data.application_id,
                    digest=callback_data.digest,
                )
                await state.clear()
                from bot.handlers.intro_refresh import show_refresh_selection

                await show_refresh_selection(
                    callback.message,
                    session,
                    callback.from_user.id,
                    edit=True,
                    context=(f"a{callback_data.application_id}.{callback_data.digest}"),
                )
            except IntroWorkflowError:
                await callback.answer("Эта анкета устарела. Запусти /refresh ещё раз.")
                return
            await callback.answer()
            return
        try:
            next_field_id = await reset_draft(
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
                digest=callback_data.digest,
            )
        except IntroWorkflowError:
            await callback.answer("Эта анкета устарела. Запусти /start ещё раз.")
            return
        if next_field_id is None:
            await callback.answer("Анкета уже заполнена.")
            return
        next_idx = FIELD_IDS.index(next_field_id)
        await state.update_data(application_id=callback_data.application_id)
        await state.set_state(STATES_LIST[next_idx])
        await callback.message.edit_text(NEXT_QUESTION.format(question=QUESTIONS[next_idx]))
        await callback.answer()
        return

    if callback_data.action == "yes":
        try:
            application = await confirm_application(
                session,
                user_id=callback.from_user.id,
                application_id=callback_data.application_id,
                digest=callback_data.digest,
            )
        except IntroWorkflowError:
            await callback.answer("Предпросмотр устарел. Заполни анкету заново.")
            return

        await state.clear()
        await callback.message.edit_text(
            REFRESH_SAVED if application.flow_kind == "refresh" else QUESTIONNAIRE_POSTED
        )

        await callback.answer()
        return

    await callback.answer()


@router.callback_query(F.data.in_({"confirm:yes", "confirm:redo"}))
async def handle_legacy_confirm(callback: CallbackQuery) -> None:
    await callback.answer(
        "Эта кнопка устарела. Запусти /start или /refresh ещё раз.", show_alert=True
    )
