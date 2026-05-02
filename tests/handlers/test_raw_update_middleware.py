"""§3.8 tests — RawUpdatePersistenceMiddleware live_ingestion_run_id threading."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def _make_session_mock():
    """Build an AsyncSession mock with a working begin_nested() context manager."""
    session = AsyncMock()

    @asynccontextmanager
    async def _begin_nested():
        yield

    session.begin_nested = _begin_nested
    return session


def test_middleware_passes_live_run_id_to_record_update(app_env, monkeypatch) -> None:
    """§3.8: middleware reads live_ingestion_run_id from data and passes it to record_update."""
    from tests.conftest import import_module

    middleware_module = import_module("bot.middlewares.raw_update_persistence")

    live_run_id = 42
    session = _make_session_mock()
    fake_raw_row = SimpleNamespace(id=99)

    captured_ingestion_run_id = []

    async def _fake_record_update(sess, event, ingestion_run_id=None):
        captured_ingestion_run_id.append(ingestion_run_id)
        return fake_raw_row

    monkeypatch.setattr(middleware_module, "record_update", _fake_record_update)

    # Build a minimal real aiogram Update so isinstance(event, Update) passes.
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, Update, User

    when = datetime.now(timezone.utc)
    chat = Chat(id=-1001234567890, type="supergroup", title="test")
    user = User(id=12345, is_bot=False, first_name="Test")
    msg = Message(message_id=1, date=when, chat=chat, from_user=user, text="hello")
    update = Update(update_id=1, message=msg)

    data = {"session": session, "live_ingestion_run_id": live_run_id}

    async def _handler(event, data_inner):
        return None

    middleware = middleware_module.RawUpdatePersistenceMiddleware()
    asyncio.run(middleware(handler=_handler, event=update, data=data))

    assert len(captured_ingestion_run_id) == 1
    assert captured_ingestion_run_id[0] == live_run_id
    # Verify data["raw_update"] was set from the fake row
    assert data.get("raw_update") is fake_raw_row


def test_middleware_no_live_run_id_passes_none(app_env, monkeypatch) -> None:
    """§3.8: when live_ingestion_run_id is absent from data, record_update gets None."""
    from tests.conftest import import_module

    middleware_module = import_module("bot.middlewares.raw_update_persistence")

    session = _make_session_mock()
    fake_raw_row = SimpleNamespace(id=88)

    captured_ingestion_run_id = []

    async def _fake_record_update(sess, event, ingestion_run_id=None):
        captured_ingestion_run_id.append(ingestion_run_id)
        return fake_raw_row

    monkeypatch.setattr(middleware_module, "record_update", _fake_record_update)

    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, Update, User

    when = datetime.now(timezone.utc)
    chat = Chat(id=-1001234567890, type="supergroup", title="test")
    user = User(id=12345, is_bot=False, first_name="Test")
    msg = Message(message_id=2, date=when, chat=chat, from_user=user, text="hi")
    update = Update(update_id=2, message=msg)

    # No live_ingestion_run_id key in data.
    data = {"session": session}

    async def _handler(event, data_inner):
        return None

    middleware = middleware_module.RawUpdatePersistenceMiddleware()
    asyncio.run(middleware(handler=_handler, event=update, data=data))

    assert captured_ingestion_run_id[0] is None
