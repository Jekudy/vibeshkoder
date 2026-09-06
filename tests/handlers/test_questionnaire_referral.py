from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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


def test_q3_prompt_and_public_label_are_intro_v2_catalog_contract(app_env) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    texts = import_module("bot.texts")
    contract = import_module("bot.services.intro_contract")

    referral = contract.get_intro_catalog("intro-v2")[2]

    assert referral.field_id == "referral"
    assert referral.question == (
        "От кого ты узнал о чате? Укажи @username или ссылку t.me/username."
    )
    assert referral.public_label == "🔗 От кого узнал о чате"
    assert texts.QUESTIONS[2] == referral.question
    assert questionnaire.QUESTIONS[2] == referral.question


def test_q3_delegates_raw_referral_to_workflow_and_advances_by_returned_field(
    app_env, monkeypatch
) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    contract = import_module("bot.services.intro_contract")
    message = _message("https://t.me/Nick_Name/?source=invite")
    state = _q3_state(questionnaire)
    session = AsyncMock()
    write_answer = AsyncMock(return_value="experience")
    monkeypatch.setattr(questionnaire, "write_answer", write_answer, raising=False)
    monkeypatch.setattr(questionnaire.QuestionnaireRepo, "save_answer", AsyncMock())

    asyncio.run(questionnaire.handle_answer(message, state, session))

    write_answer.assert_awaited_once_with(
        session,
        user_id=111,
        application_id=222,
        field_id="referral",
        answer_text="https://t.me/Nick_Name/?source=invite",
    )
    state.set_state.assert_awaited_once_with(questionnaire.QuestionnaireForm.q4_experience)
    experience = contract.get_intro_catalog("intro-v2")[3]
    message.answer.assert_awaited_once_with(
        questionnaire.NEXT_QUESTION.format(question=experience.question)
    )


def test_success_confirm_binds_callback_identity_and_only_queues_publication(
    app_env, monkeypatch
) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    session = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"application_id": 222}
    confirm_application = AsyncMock(return_value=SimpleNamespace(flow_kind="admission"))
    monkeypatch.setattr(
        questionnaire,
        "confirm_application",
        confirm_application,
        raising=False,
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=111, first_name="Applicant", username="applicant"),
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=444))),
        answer=AsyncMock(),
    )
    callback_data = SimpleNamespace(
        action="yes",
        application_id=333,
        digest="mrpfJjmr1jpKAq5Aueol1w",
    )

    asyncio.run(questionnaire.handle_confirm(callback, callback_data, state, session))

    confirm_application.assert_awaited_once_with(
        session,
        user_id=111,
        application_id=333,
        digest="mrpfJjmr1jpKAq5Aueol1w",
    )
    callback.bot.send_message.assert_not_awaited()


def test_invalid_q3_answer_fails_fast_without_advancing(app_env, monkeypatch) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    texts = import_module("bot.texts")
    workflow = import_module("bot.services.intro_workflow")
    message = _message("узнал от @nickname")
    state = _q3_state(questionnaire)
    session = AsyncMock()

    write_answer = AsyncMock(side_effect=workflow.InvalidReferralAnswer("invalid referral"))
    monkeypatch.setattr(questionnaire, "write_answer", write_answer, raising=False)

    asyncio.run(questionnaire.handle_answer(message, state, session))

    write_answer.assert_awaited_once_with(
        session,
        user_id=111,
        application_id=222,
        field_id="referral",
        answer_text="узнал от @nickname",
    )
    state.set_state.assert_not_awaited()
    message.answer.assert_awaited_once_with(texts.INVALID_REFERRAL_USERNAME)


@pytest.mark.parametrize("reason", ["stale", "wrong owner"])
def test_q3_stale_or_wrong_owner_restarts_instead_of_claiming_bad_username(
    app_env, monkeypatch, reason: str
) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    texts = import_module("bot.texts")
    message = _message("@valid_name")
    state = _q3_state(questionnaire)
    session = AsyncMock()
    write_answer = AsyncMock(side_effect=questionnaire.IntroWorkflowError(reason))
    monkeypatch.setattr(questionnaire, "write_answer", write_answer, raising=False)

    asyncio.run(questionnaire.handle_answer(message, state, session))

    state.set_state.assert_not_awaited()
    message.answer.assert_awaited_once()
    response = message.answer.await_args.args[0]
    assert response != texts.INVALID_REFERRAL_USERNAME
    assert "/start" in response or "/refresh" in response
