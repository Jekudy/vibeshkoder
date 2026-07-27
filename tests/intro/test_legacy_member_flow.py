from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, User

from tests.conftest import import_module


def _message(user_id: int = 111) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            username="alice",
            first_name="Alice",
            last_name=None,
        ),
        answer=AsyncMock(),
    )


def _user(*, is_member: bool) -> SimpleNamespace:
    return SimpleNamespace(id=111, is_member=is_member)


def _application(*, app_id: int = 222, status: str, flow_kind: str) -> SimpleNamespace:
    return SimpleNamespace(id=app_id, status=status, flow_kind=flow_kind)


def test_start_confirmed_admission_acknowledges_queue_without_new_lookups(
    app_env, monkeypatch
) -> None:
    handler = import_module("bot.handlers.start")
    texts = import_module("bot.texts")
    message = _message()
    state = AsyncMock()
    session = AsyncMock()
    upsert = AsyncMock(return_value=_user(is_member=False))
    get_active = AsyncMock(return_value=_application(status="confirmed", flow_kind="admission"))
    get_intro = AsyncMock()
    create = AsyncMock()
    next_missing = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", upsert)
    monkeypatch.setattr(handler.ApplicationRepo, "get_active", get_active)
    monkeypatch.setattr(handler.IntroRepo, "get", get_intro)
    monkeypatch.setattr(handler.ApplicationRepo, "create", create)
    monkeypatch.setattr(handler, "next_field_id", next_missing)

    asyncio.run(handler.cmd_start(message, state, session))

    message.answer.assert_awaited_once_with(texts.QUESTIONNAIRE_POSTED)
    get_intro.assert_not_awaited()
    create.assert_not_awaited()
    next_missing.assert_not_awaited()


def test_refresh_confirmed_application_acknowledges_queue_without_resuming_questions(
    app_env, monkeypatch
) -> None:
    handler = import_module("bot.handlers.start")
    texts = import_module("bot.texts")
    message = _message()
    state = AsyncMock()
    session = AsyncMock()
    start_refresh = AsyncMock(return_value=_application(status="confirmed", flow_kind="refresh"))
    next_missing = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock(return_value=_user(is_member=True)))
    monkeypatch.setattr(handler, "start_or_resume_refresh", start_refresh)
    monkeypatch.setattr(handler, "next_field_id", next_missing)

    asyncio.run(handler.cmd_refresh(message, state, session))

    message.answer.assert_awaited_once_with(texts.REFRESH_SAVED)
    next_missing.assert_not_awaited()
    assert texts.REFRESH_NOT_MEMBER not in [call.args[0] for call in message.answer.await_args_list]


def test_member_without_intro_confirmed_refresh_start_acknowledges_queue(
    app_env, monkeypatch
) -> None:
    handler = import_module("bot.handlers.start")
    texts = import_module("bot.texts")
    message = _message()
    state = AsyncMock()
    session = AsyncMock()
    start_refresh = AsyncMock(return_value=_application(status="confirmed", flow_kind="refresh"))
    next_missing = AsyncMock()
    monkeypatch.setattr(handler.UserRepo, "upsert", AsyncMock(return_value=_user(is_member=True)))
    monkeypatch.setattr(handler.ApplicationRepo, "get_active", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.IntroRepo, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(handler, "start_or_resume_refresh", start_refresh)
    monkeypatch.setattr(handler, "next_field_id", next_missing)

    asyncio.run(handler.cmd_start(message, state, session))

    message.answer.assert_awaited_once_with(texts.REFRESH_SAVED)
    next_missing.assert_not_awaited()


@pytest.mark.parametrize("legacy", ["confirm:yes", "confirm:redo"])
def test_legacy_confirm_callback_ends_spinner_and_offers_restart_without_fsm_binding(
    app_env, legacy: str
) -> None:
    questionnaire = import_module("bot.handlers.questionnaire")
    telegram_callback = CallbackQuery(
        id="legacy-confirm",
        from_user=User(id=111, is_bot=False, first_name="Alice"),
        chat_instance="chat-instance",
        data=legacy,
    )
    handler = next(
        registered
        for registered in questionnaire.router.callback_query.handlers
        if registered.callback is questionnaire.handle_legacy_confirm
    )
    matched, _ = asyncio.run(handler.check(telegram_callback))
    assert matched

    callback = SimpleNamespace(
        data=legacy,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    asyncio.run(questionnaire.handle_legacy_confirm(callback))

    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.await_args
    response = args[0] if args else kwargs["text"]
    assert "/start" in response or "/refresh" in response
    assert kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_awaited()
