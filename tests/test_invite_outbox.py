from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

_user_id_counter = itertools.count(start=9_100_000_000)


def _next_user_id() -> int:
    return next(_user_id_counter)


class _ExecuteResult:
    def __init__(self, value: int | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> int | None:
        return self._value


class _Scalars:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _RowsResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class _SessionContext:
    def __init__(self, session: SimpleNamespace) -> None:
        self.session = session

    async def __aenter__(self) -> SimpleNamespace:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _callback() -> SimpleNamespace:
    bot = SimpleNamespace(
        delete_message=AsyncMock(),
        send_message=AsyncMock(),
        create_chat_invite_link=AsyncMock(),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=2001),
        message=SimpleNamespace(message_id=3001),
        bot=bot,
        answer=AsyncMock(),
    )


def test_vouch_inserts_outbox_row_no_invite_sent(monkeypatch) -> None:
    handler = import_module("bot.handlers.vouch")
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_ExecuteResult(101)),
        flush=AsyncMock(),
    )
    callback = _callback()
    callback_data = SimpleNamespace(application_id=101)
    app = SimpleNamespace(id=101, user_id=3002)
    voucher = SimpleNamespace(id=2001, is_member=True, username="bob", first_name="Bob")

    monkeypatch.setattr(handler.ApplicationRepo, "get", AsyncMock(return_value=app))
    monkeypatch.setattr(handler.UserRepo, "get", AsyncMock(return_value=voucher))
    monkeypatch.setattr(handler.VouchRepo, "create", AsyncMock())
    monkeypatch.setattr(handler.InviteOutboxRepo, "create_pending", AsyncMock())

    asyncio.run(handler.handle_vouch(callback, callback_data, session))

    handler.InviteOutboxRepo.create_pending.assert_awaited_once_with(
        session,
        application_id=101,
        user_id=3002,
        chat_id=handler.settings.COMMUNITY_CHAT_ID,
    )
    callback.bot.create_chat_invite_link.assert_not_called()
    callback.answer.assert_awaited_once_with("Готово! Спасибо за ручательство.")


def test_ready_inserts_outbox_row_no_invite_sent(monkeypatch) -> None:
    handler = import_module("bot.handlers.vouch")
    session = SimpleNamespace()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=3002),
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(create_chat_invite_link=AsyncMock()),
        answer=AsyncMock(),
    )
    callback_data = SimpleNamespace(application_id=101)
    app = SimpleNamespace(id=101, user_id=3002, status="privacy_block")

    monkeypatch.setattr(handler.ApplicationRepo, "get", AsyncMock(return_value=app))
    monkeypatch.setattr(handler.ApplicationRepo, "update_status", AsyncMock())
    monkeypatch.setattr(handler.InviteOutboxRepo, "create_pending", AsyncMock())

    asyncio.run(handler.handle_ready(callback, callback_data, session))

    handler.ApplicationRepo.update_status.assert_awaited_once_with(
        session, 101, "vouched", invite_user_id=3002
    )
    handler.InviteOutboxRepo.create_pending.assert_awaited_once_with(
        session,
        application_id=101,
        user_id=3002,
        chat_id=handler.settings.COMMUNITY_CHAT_ID,
    )
    callback.bot.create_chat_invite_link.assert_not_called()
    callback.message.edit_text.assert_awaited_once_with(
        "Запрос принят. Инвайт скоро придёт в личные сообщения."
    )
    callback.answer.assert_awaited_once_with()


def test_outbox_worker_sends_pending(monkeypatch) -> None:
    worker = import_module("bot.services.invite_worker")
    row = SimpleNamespace(
        id=1,
        application_id=101,
        user_id=3002,
        chat_id=-100123,
        status="pending",
        invite_link=None,
        attempt_count=0,
        last_error=None,
        sent_at=None,
    )
    session = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    monkeypatch.setattr(worker, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(worker.InviteOutboxRepo, "get_pending", AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        worker.invite_service, "create_invite", AsyncMock(return_value="https://t.me/+ok")
    )

    asyncio.run(worker.process_invite_outbox(bot))

    worker.invite_service.create_invite.assert_awaited_once_with(bot, -100123, 101, 3002)
    bot.send_message.assert_awaited_once_with(
        chat_id=3002,
        text=worker.INVITE_LINK_MSG.format(link="https://t.me/+ok"),
    )
    assert row.status == "sent"
    assert row.invite_link == "https://t.me/+ok"
    assert row.sent_at is not None
    session.commit.assert_awaited_once()


def test_outbox_worker_retries_on_failure(monkeypatch) -> None:
    worker = import_module("bot.services.invite_worker")
    row = SimpleNamespace(
        id=1,
        application_id=101,
        user_id=3002,
        chat_id=-100123,
        status="pending",
        invite_link=None,
        attempt_count=0,
        last_error=None,
        sent_at=None,
    )
    session = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    monkeypatch.setattr(worker, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(worker.InviteOutboxRepo, "get_pending", AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        worker.invite_service,
        "create_invite",
        AsyncMock(side_effect=RuntimeError("telegram unavailable")),
    )

    asyncio.run(worker.process_invite_outbox(bot))

    assert row.status == "pending"
    assert row.attempt_count == 1
    assert row.last_error == "telegram unavailable"
    assert row.sent_at is None
    bot.send_message.assert_not_called()
    session.commit.assert_awaited_once()


def test_invite_worker_exhausted_pending_app_demoted(monkeypatch, caplog) -> None:
    worker = import_module("bot.services.invite_worker")
    row = SimpleNamespace(
        id=1,
        application_id=101,
        user_id=3002,
        chat_id=-100123,
        status="pending",
        invite_link=None,
        attempt_count=4,
        last_error=None,
        sent_at=None,
    )
    app = SimpleNamespace(id=101, status="pending")
    session = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    async def cas_update(session_arg, app_id, expected_from, new_status, **extra_fields):
        assert session_arg is session
        assert app_id == app.id
        assert expected_from == "pending"
        assert extra_fields == {}
        if app.status != expected_from:
            return False
        app.status = new_status
        return True

    monkeypatch.setattr(worker, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(worker.InviteOutboxRepo, "get_pending", AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        worker.invite_service,
        "create_invite",
        AsyncMock(side_effect=RuntimeError("privacy blocked")),
    )
    update_status_if_mock = AsyncMock(side_effect=cas_update)
    get_mock = AsyncMock(return_value=app)
    monkeypatch.setattr(worker.ApplicationRepo, "update_status_if", update_status_if_mock)
    monkeypatch.setattr(worker.ApplicationRepo, "get", get_mock)
    caplog.set_level("WARNING", logger=worker.logger.name)

    asyncio.run(worker.process_invite_outbox(bot))

    assert row.status == "failed"
    assert row.attempt_count == 5
    assert row.last_error == "privacy blocked"
    assert app.status == "privacy_block"
    update_status_if_mock.assert_awaited_once_with(
        session,
        app_id=101,
        expected_from="pending",
        new_status="privacy_block",
    )
    get_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert any("failed attempt 5/5" in record.message for record in caplog.records)


def test_invite_worker_exhausted_vouched_app_preserved(monkeypatch, caplog) -> None:
    worker = import_module("bot.services.invite_worker")
    row = SimpleNamespace(
        id=1,
        application_id=101,
        user_id=3002,
        chat_id=-100123,
        status="pending",
        invite_link=None,
        attempt_count=4,
        last_error=None,
        sent_at=None,
    )
    app = SimpleNamespace(id=101, status="vouched")
    session = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    async def cas_update(session_arg, app_id, expected_from, new_status, **extra_fields):
        assert session_arg is session
        assert app_id == app.id
        assert expected_from == "pending"
        assert extra_fields == {}
        if app.status != expected_from:
            return False
        app.status = new_status
        return True

    monkeypatch.setattr(worker, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(worker.InviteOutboxRepo, "get_pending", AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        worker.invite_service,
        "create_invite",
        AsyncMock(side_effect=RuntimeError("privacy blocked")),
    )
    update_status_if_mock = AsyncMock(side_effect=cas_update)
    get_mock = AsyncMock(return_value=app)
    monkeypatch.setattr(worker.ApplicationRepo, "update_status_if", update_status_if_mock)
    monkeypatch.setattr(worker.ApplicationRepo, "get", get_mock)
    caplog.set_level("WARNING", logger=worker.logger.name)

    asyncio.run(worker.process_invite_outbox(bot))

    assert row.status == "failed"
    assert row.attempt_count == 5
    assert row.last_error == "privacy blocked"
    assert app.status == "vouched"
    update_status_if_mock.assert_awaited_once_with(
        session,
        app_id=101,
        expected_from="pending",
        new_status="privacy_block",
    )
    get_mock.assert_awaited_once_with(session, 101)
    bot.send_message.assert_not_called()
    session.commit.assert_awaited_once()
    records = [
        record
        for record in caplog.records
        if record.message == "invite_worker.privacy_block_skipped"
    ]
    assert len(records) == 1
    assert records[0].app_id == 101
    assert records[0].observed_status == "vouched"


def test_invite_worker_exhausted_added_app_preserved(monkeypatch, caplog) -> None:
    worker = import_module("bot.services.invite_worker")
    row = SimpleNamespace(
        id=1,
        application_id=101,
        user_id=3002,
        chat_id=-100123,
        status="pending",
        invite_link=None,
        attempt_count=4,
        last_error=None,
        sent_at=None,
    )
    app = SimpleNamespace(id=101, status="added")
    session = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    async def cas_update(session_arg, app_id, expected_from, new_status, **extra_fields):
        assert session_arg is session
        assert app_id == app.id
        assert expected_from == "pending"
        assert extra_fields == {}
        if app.status != expected_from:
            return False
        app.status = new_status
        return True

    monkeypatch.setattr(worker, "async_session", lambda: _SessionContext(session))
    monkeypatch.setattr(worker.InviteOutboxRepo, "get_pending", AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        worker.invite_service,
        "create_invite",
        AsyncMock(side_effect=RuntimeError("privacy blocked")),
    )
    update_status_if_mock = AsyncMock(side_effect=cas_update)
    get_mock = AsyncMock(return_value=app)
    monkeypatch.setattr(worker.ApplicationRepo, "update_status_if", update_status_if_mock)
    monkeypatch.setattr(worker.ApplicationRepo, "get", get_mock)
    caplog.set_level("WARNING", logger=worker.logger.name)

    asyncio.run(worker.process_invite_outbox(bot))

    assert row.status == "failed"
    assert row.attempt_count == 5
    assert row.last_error == "privacy blocked"
    assert app.status == "added"
    update_status_if_mock.assert_awaited_once_with(
        session,
        app_id=101,
        expected_from="pending",
        new_status="privacy_block",
    )
    get_mock.assert_awaited_once_with(session, 101)
    bot.send_message.assert_not_called()
    session.commit.assert_awaited_once()
    records = [
        record
        for record in caplog.records
        if record.message == "invite_worker.privacy_block_skipped"
    ]
    assert len(records) == 1
    assert records[0].app_id == 101
    assert records[0].observed_status == "added"


async def test_invite_outbox_unique_pending_per_application(db_session) -> None:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from bot.db.models import InviteOutbox
    from bot.db.repos.application import ApplicationRepo
    from bot.db.repos.user import UserRepo

    connection = await db_session.connection()
    if connection.dialect.name != "postgresql":
        pytest.skip("invite_outbox pending partial unique index is postgres-only")

    user_id = _next_user_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"u{user_id}",
        first_name="T",
        last_name=None,
    )
    app = await ApplicationRepo.create(db_session, user_id)
    await ApplicationRepo.update_status(db_session, app.id, "pending")

    first = InviteOutbox(
        application_id=app.id,
        user_id=user_id,
        chat_id=-100123,
        status="pending",
    )
    db_session.add(first)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                InviteOutbox(
                    application_id=app.id,
                    user_id=user_id,
                    chat_id=-100123,
                    status="pending",
                )
            )
            await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(InviteOutbox).where(
                    InviteOutbox.application_id == app.id,
                    InviteOutbox.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in rows] == [first.id]


def test_invite_outbox_model_registered() -> None:
    models = import_module("bot.db.models")

    assert hasattr(models, "InviteOutbox")
    assert "invite_outbox" in models.Base.metadata.tables
    table = models.Base.metadata.tables["invite_outbox"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "application_id",
        "user_id",
        "chat_id",
        "status",
        "invite_link",
        "attempt_count",
        "last_error",
        "created_at",
        "sent_at",
    } == cols
    indexes = {ix.name: ix for ix in table.indexes}
    assert "ix_invite_outbox_status" in indexes
    assert "ix_invite_outbox_pending_unique" in indexes
    pending_unique = indexes["ix_invite_outbox_pending_unique"]
    assert pending_unique.unique is True
    assert str(pending_unique.dialect_options["postgresql"]["where"]) == ("status = 'pending'")
