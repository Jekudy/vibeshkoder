from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, Update, User
from sqlalchemy.exc import SQLAlchemyError


pytestmark = pytest.mark.usefixtures("app_env")


def _update() -> Update:
    return Update(
        update_id=991,
        message=Message(
            message_id=17,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-1001234567890, type="supergroup", title="test"),
            from_user=User(id=42, is_bot=False, first_name="Member"),
            text="raw-payload-sentinel",
        ),
    )


def _session() -> AsyncMock:
    session = AsyncMock()

    @asynccontextmanager
    async def begin_nested():
        yield

    session.begin_nested = begin_nested
    return session


async def test_sqlalchemy_failure_aborts_before_normalized_path(monkeypatch) -> None:
    from bot.middlewares import raw_update_persistence as module

    sensitive_marker = "raw-sensitive-sentinel"

    async def fail(*args, **kwargs):
        raise SQLAlchemyError(f"INSERT params include {sensitive_marker} raw-payload-sentinel")

    monkeypatch.setattr(module, "record_update", fail)
    normalized_path = AsyncMock()

    with pytest.raises(SQLAlchemyError):
        await module.RawUpdatePersistenceMiddleware()(
            normalized_path,
            _update(),
            {"session": _session()},
        )

    normalized_path.assert_not_awaited()


async def test_failure_log_is_structured_and_contains_no_exception_or_payload(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from bot.middlewares import raw_update_persistence as module

    sensitive_marker = "raw-sensitive-sentinel"

    async def fail(*args, **kwargs):
        raise SQLAlchemyError(f"SQL params {sensitive_marker} raw-payload-sentinel")

    monkeypatch.setattr(module, "record_update", fail)
    caplog.set_level(logging.ERROR, logger=module.__name__)

    with pytest.raises(SQLAlchemyError):
        await module.RawUpdatePersistenceMiddleware()(
            AsyncMock(),
            _update(),
            {"session": _session()},
        )

    records = [record for record in caplog.records if record.name == module.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == "raw_update_persistence_failed"
    assert record.update_id == 991
    assert record.error_class == "SQLAlchemyError"
    assert record.exc_info is None
    rendered = repr(record.__dict__)
    assert sensitive_marker not in rendered
    assert "raw-payload-sentinel" not in rendered
    assert "INSERT params" not in rendered


async def test_commit_failure_aborts_before_downstream_without_payload_leak(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from bot.middlewares import raw_update_persistence as module

    secret = "raw-commit-secret-sentinel"
    session = _session()
    session.commit.side_effect = SQLAlchemyError(secret)
    monkeypatch.setattr(
        module,
        "record_update",
        AsyncMock(return_value=SimpleNamespace(id=71)),
    )
    downstream = AsyncMock()
    caplog.set_level(logging.ERROR, logger=module.__name__)

    with pytest.raises(SQLAlchemyError):
        await module.RawUpdatePersistenceMiddleware()(
            downstream,
            _update(),
            {"session": session},
        )

    downstream.assert_not_awaited()
    records = [record for record in caplog.records if record.name == module.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == "raw_update_persistence_failed"
    assert records[0].error_class == "SQLAlchemyError"
    assert records[0].exc_info is None
    assert secret not in repr(records[0].__dict__)
