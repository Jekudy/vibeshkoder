from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, call

from tests.conftest import import_module


COMMUNITY_CHAT_ID = -1001234567890


def _user(user_id: int = 111) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        first_name=f"User {user_id}",
        last_name=None,
    )


def _db_user(*, left_at=None) -> SimpleNamespace:
    return SimpleNamespace(left_at=left_at)


def _application(
    *,
    app_id: int = 555,
    status: str,
    invite_user_id: int | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=app_id,
        status=status,
        invite_user_id=invite_user_id,
        vouched_by=999,
    )


def _join_event() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=COMMUNITY_CHAT_ID),
        bot=SimpleNamespace(
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


def _patch_join_dependencies(
    handler,
    monkeypatch,
    *,
    active_app: SimpleNamespace | None,
) -> tuple[AsyncMock, AsyncMock]:
    user_upsert = AsyncMock(return_value=_db_user())
    user_set_member = AsyncMock()
    user_get = AsyncMock(return_value=_db_user())

    monkeypatch.setattr(handler.UserRepo, "upsert", user_upsert)
    monkeypatch.setattr(handler.UserRepo, "set_member", user_set_member)
    monkeypatch.setattr(handler.UserRepo, "get", user_get)
    monkeypatch.setattr(handler.IntroRepo, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handler.ApplicationRepo,
        "get_active",
        AsyncMock(return_value=active_app),
    )
    monkeypatch.setattr(handler.ApplicationRepo, "update_status", AsyncMock())
    monkeypatch.setattr(handler.QuestionnaireRepo, "get_answers", AsyncMock(return_value=[]))
    monkeypatch.setattr(handler.IntroRepo, "upsert", AsyncMock())

    return user_set_member, handler.ApplicationRepo.update_status


def test_filling_user_using_forwarded_invite_rejected(
    app_env, monkeypatch, caplog
) -> None:
    handler = import_module("bot.handlers.chat_events")
    session = AsyncMock()
    event = _join_event()
    tg_user = _user(222)
    user_set_member, update_status = _patch_join_dependencies(
        handler,
        monkeypatch,
        active_app=_application(status="filling", invite_user_id=None),
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(handler._handle_join(event, session, tg_user))

    event.bot.ban_chat_member.assert_awaited_once_with(COMMUNITY_CHAT_ID, 222)
    event.bot.unban_chat_member.assert_awaited_once_with(COMMUNITY_CHAT_ID, 222)
    update_status.assert_not_called()
    user_set_member.assert_has_awaits(
        [
            call(session, 222, is_member=True, joined_at=ANY),
            call(session, 222, is_member=False, left_at=ANY),
        ]
    )
    assert "status='filling'" in caplog.text


def test_pending_user_join_rejected(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.chat_events")
    session = AsyncMock()
    event = _join_event()
    tg_user = _user(222)
    _user_set_member, update_status = _patch_join_dependencies(
        handler,
        monkeypatch,
        active_app=_application(status="pending", invite_user_id=None),
    )

    asyncio.run(handler._handle_join(event, session, tg_user))

    event.bot.ban_chat_member.assert_awaited_once_with(COMMUNITY_CHAT_ID, 222)
    event.bot.unban_chat_member.assert_awaited_once_with(COMMUNITY_CHAT_ID, 222)
    update_status.assert_not_called()


def test_vouched_user_join_accepted(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.chat_events")
    session = AsyncMock()
    event = _join_event()
    tg_user = _user(111)
    _user_set_member, update_status = _patch_join_dependencies(
        handler,
        monkeypatch,
        active_app=_application(status="vouched", invite_user_id=111),
    )

    asyncio.run(handler._handle_join(event, session, tg_user))

    event.bot.ban_chat_member.assert_not_called()
    event.bot.unban_chat_member.assert_not_called()
    update_status.assert_awaited_once()
    args = update_status.await_args.args
    kwargs = update_status.await_args.kwargs
    assert args == (session, 555, "added")
    assert "added_at" in kwargs


def test_invite_bound_to_user_id(app_env, monkeypatch, caplog) -> None:
    handler = import_module("bot.handlers.chat_events")
    invite_service = import_module("bot.services.invite")
    session = AsyncMock()
    event = _join_event()
    tg_user = _user(222)
    _user_set_member, update_status = _patch_join_dependencies(
        handler,
        monkeypatch,
        active_app=_application(status="vouched", invite_user_id=111),
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(handler._handle_join(event, session, tg_user))

    event.bot.ban_chat_member.assert_awaited_once_with(COMMUNITY_CHAT_ID, 222)
    update_status.assert_not_called()
    assert "invite_user_id=111" in caplog.text

    bot = AsyncMock()
    bot.create_chat_invite_link.return_value = SimpleNamespace(
        invite_link="https://t.me/+bound"
    )

    invite_link = asyncio.run(
        invite_service.create_invite(
            bot,
            chat_id=COMMUNITY_CHAT_ID,
            app_id=555,
            user_id=111,
        )
    )

    assert invite_link == "https://t.me/+bound"
    bot.create_chat_invite_link.assert_awaited_once_with(
        chat_id=COMMUNITY_CHAT_ID,
        member_limit=1,
        creates_join_request=False,
        name="app-555",
    )
