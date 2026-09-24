from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.dml import Insert

from tests.conftest import import_module


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, claim_ids: list[int | None], *, failed_claims: int = 0):
        self.claim_ids = iter(claim_ids)
        self.failed_claims = failed_claims
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        if isinstance(statement, Insert):
            if self.failed_claims:
                self.failed_claims -= 1
                raise SQLAlchemyError("claim failed")
            return _ScalarResult(next(self.claim_ids))
        return _ScalarResult(None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def test_successful_wave_sends_once_and_duplicate_claim_does_not_retry(
    app_env, monkeypatch
) -> None:
    scheduler = import_module("bot.services.scheduler")
    session = _Session([42, None])
    intro = SimpleNamespace(user_id=111, intro_text="👤 Имя: Сергей")
    monkeypatch.setattr(scheduler, "async_session", lambda: session)
    monkeypatch.setattr(
        scheduler.IntroRepo,
        "get_refresh_wave_candidates",
        AsyncMock(return_value=[intro]),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    now = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)

    assert asyncio.run(scheduler.check_intro_refresh(bot, now=now)) == 1
    assert asyncio.run(scheduler.check_intro_refresh(bot, now=now)) == 0

    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "Хочешь обновить интро?" in sent_text
    assert "<blockquote expandable>👤 Имя: Сергей</blockquote>" in sent_text


def test_failed_delivery_is_not_retried_in_the_same_wave(app_env, monkeypatch) -> None:
    scheduler = import_module("bot.services.scheduler")
    session = _Session([42, None])
    intro = SimpleNamespace(user_id=111, intro_text="intro")
    monkeypatch.setattr(scheduler, "async_session", lambda: session)
    monkeypatch.setattr(
        scheduler.IntroRepo,
        "get_refresh_wave_candidates",
        AsyncMock(return_value=[intro]),
    )
    error = TelegramBadRequest(method=SendMessage(chat_id=111, text="test"), message="blocked")
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    now = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)

    assert asyncio.run(scheduler.check_intro_refresh(bot, now=now)) == 0
    assert asyncio.run(scheduler.check_intro_refresh(bot, now=now)) == 0
    bot.send_message.assert_awaited_once()


def test_wave_job_is_inactive_outside_shared_dates(app_env) -> None:
    scheduler = import_module("bot.services.scheduler")
    bot = SimpleNamespace(send_message=AsyncMock())

    assert (
        asyncio.run(
            scheduler.check_intro_refresh(bot, now=datetime(2026, 8, 31, 7, tzinfo=timezone.utc))
        )
        is None
    )
    bot.send_message.assert_not_awaited()


def test_database_failure_for_one_candidate_does_not_abort_the_wave(app_env, monkeypatch) -> None:
    scheduler = import_module("bot.services.scheduler")
    session = _Session([42], failed_claims=1)
    intros = [
        SimpleNamespace(user_id=111, intro_text="first"),
        SimpleNamespace(user_id=222, intro_text="second"),
    ]
    monkeypatch.setattr(scheduler, "async_session", lambda: session)
    monkeypatch.setattr(
        scheduler.IntroRepo,
        "get_refresh_wave_candidates",
        AsyncMock(return_value=intros),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    assert (
        asyncio.run(
            scheduler.check_intro_refresh(bot, now=datetime(2026, 9, 1, 7, tzinfo=timezone.utc))
        )
        == 1
    )
    assert session.rollbacks == 1
    assert bot.send_message.await_args.kwargs["chat_id"] == 222


def test_long_legacy_intro_is_split_before_delivery(app_env, monkeypatch) -> None:
    scheduler = import_module("bot.services.scheduler")
    session = _Session([42])
    intro = SimpleNamespace(user_id=111, intro_text=("Строка " + "x" * 200 + "\n") * 30)
    monkeypatch.setattr(scheduler, "async_session", lambda: session)
    monkeypatch.setattr(
        scheduler.IntroRepo,
        "get_refresh_wave_candidates",
        AsyncMock(return_value=[intro]),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    assert (
        asyncio.run(
            scheduler.check_intro_refresh(bot, now=datetime(2026, 9, 1, 7, tzinfo=timezone.utc))
        )
        == 1
    )
    assert bot.send_message.await_count > 1
    calls = bot.send_message.await_args_list
    assert all(len(call.kwargs["text"]) <= 4096 for call in calls)
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] is not None
