from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def _message(user_id: int) -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=user_id)
    message.answer = AsyncMock()
    return message


async def test_digest_now_is_silent_for_non_admin(db_session) -> None:
    from bot.handlers.digest import cmd_digest_now

    message = _message(9_999_999)
    command = MagicMock(args="daily")
    await cmd_digest_now(message, MagicMock(), db_session, command)

    message.answer.assert_not_awaited()


async def test_digest_now_rejects_unknown_mode_without_provider_call(db_session) -> None:
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = next(iter(settings.ADMIN_IDS), 1)
    message = _message(admin_id)
    command = MagicMock(args="monthly")
    await cmd_digest_now(message, MagicMock(), db_session, command)

    message.answer.assert_awaited_once()
    assert "Использование" in message.answer.await_args.args[0]


@pytest.mark.parametrize("mode", ["daily", "weekly"])
async def test_digest_now_dispatches_supported_mode(db_session, monkeypatch, mode: str) -> None:
    from bot.config import settings
    import bot.handlers.digest as handler

    calls: list[str] = []

    async def _run(*args, **kwargs):
        calls.append(kwargs["digest_type"])

    monkeypatch.setattr(handler, "_run_and_publish", _run)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True))
    message = _message(next(iter(settings.ADMIN_IDS), 1))
    await handler.cmd_digest_now(message, MagicMock(), db_session, MagicMock(args=mode))

    assert calls == [mode]


async def test_digest_now_daily_is_paused_by_feature_flag(db_session, monkeypatch) -> None:
    from bot.config import settings
    import bot.handlers.digest as handler

    run = AsyncMock()
    flag = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_run_and_publish", run)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", flag)
    message = _message(next(iter(settings.ADMIN_IDS), 1))

    await handler.cmd_digest_now(message, MagicMock(), db_session, MagicMock(args="daily"))

    flag.assert_awaited_once_with(db_session, "memory.digests.daily.enabled")
    run.assert_not_awaited()
    assert "Ежедневный дайджест выключен" in message.answer.await_args.args[0]


async def test_digest_now_defaults_to_weekly_even_when_daily_is_paused(
    db_session, monkeypatch
) -> None:
    from bot.config import settings
    import bot.handlers.digest as handler

    run = AsyncMock()
    flag = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_run_and_publish", run)
    monkeypatch.setattr(handler.FeatureFlagRepo, "get", flag)
    message = _message(next(iter(settings.ADMIN_IDS), 1))

    await handler.cmd_digest_now(message, MagicMock(), db_session, MagicMock(args=None))

    flag.assert_not_awaited()
    run.assert_awaited_once()
    assert run.await_args.kwargs["digest_type"] == "weekly"
