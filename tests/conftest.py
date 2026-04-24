from __future__ import annotations

# NOTE: pytest-asyncio is NOT in the declared dev deps, so we cannot use
# @pytest.mark.asyncio or async fixtures. All async code in tests is run via
# asyncio.run() inside sync test functions. The session_factory_sqlite fixture
# returns a callable factory; tests call asyncio.run(factory()) themselves.

import asyncio
import importlib
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _clear_modules() -> None:
    for name in list(sys.modules):
        if name == "bot" or name.startswith("bot.") or name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)


@pytest.fixture()
def app_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://vibe:changeme@db:5432/vibe_gatekeeper")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDS_FILE", "")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("WEB_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("WEB_BOT_USERNAME", "vibeshkoder_dev_bot")
    monkeypatch.setenv("DB_PASSWORD", "changeme")
    monkeypatch.setenv("WEB_PASSWORD", "test-pass")
    monkeypatch.setenv("DEV_MODE", "true")
    _clear_modules()
    yield
    _clear_modules()


def import_module(name: str):
    return importlib.import_module(name)


# ── New safety-net fixtures ──────────────────────────────────────────────────


@pytest.fixture()
def aiogram_bot_mock() -> AsyncMock:
    """Return an AsyncMock with the shape of aiogram.Bot."""
    import aiogram

    bot = AsyncMock(spec=aiogram.Bot)
    # Ensure sub-methods are also AsyncMock (spec sets them, but be explicit)
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    bot.edit_message_text = AsyncMock(return_value=MagicMock())
    bot.delete_message = AsyncMock(return_value=True)
    bot.ban_chat_member = AsyncMock(return_value=True)
    bot.unban_chat_member = AsyncMock(return_value=True)
    bot.get_chat = AsyncMock(return_value=MagicMock())
    return bot


@pytest.fixture()
def chat_member_factory():
    """Factory that produces minimal ChatMemberUpdated-like MagicMocks.

    Since constructing real aiogram ChatMemberUpdated objects requires heavy
    telegram-API data validation, we use MagicMock with the attribute shape
    expected by our handler code (_is_join, _is_leave, handle_chat_member).
    """

    def _make(
        old_status: str,
        new_status: str,
        user_id: int = 12345,
        chat_id: int = -1001234567890,
        username: str | None = "testuser",
        first_name: str = "Test",
    ) -> Any:
        event = MagicMock()
        event.chat.id = chat_id

        old_member = MagicMock()
        old_member.status = old_status

        new_member = MagicMock()
        new_member.status = new_status
        new_member.user.id = user_id
        new_member.user.username = username
        new_member.user.first_name = first_name
        new_member.user.last_name = None

        event.old_chat_member = old_member
        event.new_chat_member = new_member
        event.bot = AsyncMock()
        event.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        event.bot.ban_chat_member = AsyncMock(return_value=True)
        event.bot.unban_chat_member = AsyncMock(return_value=True)
        return event

    return _make


@pytest.fixture()
def message_factory(aiogram_bot_mock):
    """Factory that produces minimal Message-like MagicMocks."""

    def _make(
        text: str = "Hello",
        user_id: int = 12345,
        chat_id: int = 12345,
        message_id: int = 1,
        username: str | None = "testuser",
        first_name: str = "Test",
    ) -> Any:
        msg = MagicMock()
        msg.text = text
        msg.message_id = message_id
        msg.chat.id = chat_id
        msg.chat.type = "private"
        msg.from_user.id = user_id
        msg.from_user.username = username
        msg.from_user.first_name = first_name
        msg.from_user.last_name = None
        msg.content_type = "text"
        msg.answer = AsyncMock()
        msg.delete = AsyncMock()
        msg.bot = aiogram_bot_mock
        return msg

    return _make


@pytest.fixture()
def callback_query_factory(aiogram_bot_mock):
    """Factory that produces minimal CallbackQuery-like MagicMocks."""

    def _make(
        data: str = "vouch:1",
        user_id: int = 99999,
        message_id: int = 500,
        chat_id: int = -1001234567890,
        username: str | None = "voucher_user",
        first_name: str = "Voucher",
    ) -> Any:
        cq = MagicMock()
        cq.data = data
        cq.from_user.id = user_id
        cq.from_user.username = username
        cq.from_user.first_name = first_name
        cq.message.message_id = message_id
        cq.message.chat.id = chat_id
        cq.message.edit_text = AsyncMock()
        cq.answer = AsyncMock()
        cq.bot = aiogram_bot_mock
        return cq

    return _make


@pytest.fixture()
def session_factory_sqlite(app_env):
    """Sync fixture that provides an async SQLite session factory.

    Usage in tests:
        def test_something(session_factory_sqlite):
            async def _run():
                async with session_factory_sqlite() as session:
                    ...
            asyncio.run(_run())

    Teardown drops all tables after each test for isolation.
    """
    from bot.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Create schema synchronously by running an event loop
    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())

    yield factory

    # Teardown
    async def _teardown():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    asyncio.run(_teardown())
