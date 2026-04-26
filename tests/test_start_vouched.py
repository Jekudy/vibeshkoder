from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def _user(user_id: int = 111, *, is_member: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, is_member=is_member)


def _application(app_id: int = 222, *, status: str = "vouched") -> SimpleNamespace:
    return SimpleNamespace(id=app_id, status=status)


def test_start_vouched_status_sends_pending_message(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.start")
    texts = import_module("bot.texts")
    session = AsyncMock()
    state = AsyncMock()
    message = _message()

    user_upsert = AsyncMock(return_value=_user(is_member=False))
    application_get_active = AsyncMock(return_value=_application(status="vouched"))
    application_create = AsyncMock()
    intro_get = AsyncMock()

    monkeypatch.setattr(handler.UserRepo, "upsert", user_upsert)
    monkeypatch.setattr(handler.ApplicationRepo, "get_active", application_get_active)
    monkeypatch.setattr(handler.ApplicationRepo, "create", application_create)
    monkeypatch.setattr(handler.IntroRepo, "get", intro_get)

    asyncio.run(handler.cmd_start(message, state, session))

    user_upsert.assert_awaited_once_with(
        session,
        telegram_id=111,
        username="alice",
        first_name="Alice",
        last_name=None,
    )
    application_get_active.assert_awaited_once_with(session, 111)
    message.answer.assert_awaited_once_with(texts.VOUCHED_PENDING)
    application_create.assert_not_called()
    intro_get.assert_not_called()
    state.update_data.assert_not_called()
    state.set_state.assert_not_called()
