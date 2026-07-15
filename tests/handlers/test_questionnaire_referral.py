from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import import_module


def _message(text: str, user_id: int = 111) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _q3_state(questionnaire) -> AsyncMock:
    state = AsyncMock()
    state.get_state.return_value = questionnaire.QuestionnaireForm.q3_source.state
    state.get_data.return_value = {"application_id": 222}
    return state


def test_q3_question_explicitly_requests_telegram_id(app_env) -> None:
    texts = import_module("bot.texts")

    assert len(texts.QUESTIONS) == 7
    assert texts.QUESTIONS[2] == "От кого узнал — укажи Telegram ID"


def test_q3_saves_normalized_username_and_advances(app_env, monkeypatch) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    message = _message("https://t.me/Nick_Name/?source=invite")
    state = _q3_state(questionnaire)
    session = AsyncMock()
    save_answer = AsyncMock()
    monkeypatch.setattr(questionnaire.QuestionnaireRepo, "save_answer", save_answer)

    asyncio.run(questionnaire.handle_answer(message, state, session))

    save_answer.assert_awaited_once_with(
        session,
        user_id=111,
        application_id=222,
        question_index=2,
        question_text="От кого узнал — укажи Telegram ID",
        answer_text="@nick_name",
    )
    state.set_state.assert_awaited_once_with(questionnaire.QuestionnaireForm.q4_experience)
    message.answer.assert_awaited_once_with(
        questionnaire.NEXT_QUESTION.format(question=questionnaire.QUESTIONS[3])
    )


def test_referral_input_is_normalized_and_published_in_intro(app_env, monkeypatch) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    session = AsyncMock()
    persisted_answers: list[SimpleNamespace] = []

    async def save_answer(_session, **kwargs) -> None:
        persisted_answers.append(
            SimpleNamespace(
                question_index=kwargs["question_index"],
                answer_text=kwargs["answer_text"],
            )
        )

    save_answer_mock = AsyncMock(side_effect=save_answer)
    get_answers_mock = AsyncMock(side_effect=lambda *_args, **_kwargs: persisted_answers)
    monkeypatch.setattr(questionnaire.QuestionnaireRepo, "save_answer", save_answer_mock)
    monkeypatch.setattr(questionnaire.QuestionnaireRepo, "get_answers", get_answers_mock)
    monkeypatch.setattr(questionnaire.UserRepo, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(questionnaire.ApplicationRepo, "update_status", AsyncMock())

    state = _q3_state(questionnaire)
    asyncio.run(questionnaire.handle_answer(_message("t.me/Nick_Name"), state, session))

    save_answer_mock.assert_awaited_once()
    assert [answer.answer_text for answer in persisted_answers] == ["@nick_name"]

    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=111,
            first_name="Applicant",
            username="applicant",
        ),
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=333))),
        answer=AsyncMock(),
    )
    callback_data = SimpleNamespace(action="yes")

    asyncio.run(
        questionnaire.handle_confirm(
            callback,
            callback_data,
            state,
            session,
        )
    )

    get_answers_mock.assert_awaited_once_with(session, 111, application_id=222)
    callback.bot.send_message.assert_awaited_once()
    published_text = callback.bot.send_message.await_args.kwargs["text"]

    assert "🔗 Откуда узнал: @nick_name" in published_text.splitlines()


def test_invalid_q3_answer_fails_fast_without_advancing(app_env, monkeypatch) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    texts = import_module("bot.texts")
    message = _message("узнал от @nickname")
    state = _q3_state(questionnaire)
    session = AsyncMock()
    save_answer = AsyncMock()
    monkeypatch.setattr(questionnaire.QuestionnaireRepo, "save_answer", save_answer)

    asyncio.run(questionnaire.handle_answer(message, state, session))

    save_answer.assert_not_awaited()
    state.set_state.assert_not_awaited()
    message.answer.assert_awaited_once_with(texts.INVALID_REFERRAL_USERNAME)
