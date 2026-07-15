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
