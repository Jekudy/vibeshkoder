"""T1-13 acceptance tests — offrecord_marks table + OffrecordMarkRepo.

Outer-tx isolation. Tests do NOT call ``session.commit()``.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=9_100_000_000)
_msg_counter = itertools.count(start=980_000)


def _next_user() -> int:
    return next(_user_counter)


def _random_chat_id() -> int:
    return -1_000_000_000_000 - (next(_msg_counter) % 1_000_000)


async def _make_chat_message(db_session) -> int:
    from bot.db.models import ChatMessage
    from bot.db.repos.user import UserRepo

    user_id = _next_user()
    chat_id = _random_chat_id()
    message_id = next(_msg_counter)
    when = datetime.now(timezone.utc)

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"u{user_id}",
        first_name="T",
        last_name=None,
    )
    msg = ChatMessage(
        message_id=message_id, chat_id=chat_id, user_id=user_id, text="hi", date=when
    )
    db_session.add(msg)
    await db_session.flush()
    return msg.id


async def test_create_for_message_inserts_active_row(db_session) -> None:
    from bot.db.repos.offrecord_mark import OffrecordMarkRepo

    msg_id = await _make_chat_message(db_session)

    mark = await OffrecordMarkRepo.create_for_message(
        db_session,
        chat_message_id=msg_id,
        mark_type="offrecord",
        detected_by="deterministic_token_match_v1",
        set_by_user_id=42,
    )

    assert mark.id is not None
    assert mark.mark_type == "offrecord"
    assert mark.scope_type == "message"
    assert mark.scope_id == str(msg_id)
    assert mark.chat_message_id == msg_id
    assert mark.set_by_user_id == 42
    assert mark.detected_by == "deterministic_token_match_v1"
    assert mark.status == "active"
    assert mark.detected_at is not None


async def test_create_for_message_with_thread_id(db_session) -> None:
    from bot.db.repos.offrecord_mark import OffrecordMarkRepo

    msg_id = await _make_chat_message(db_session)
    mark = await OffrecordMarkRepo.create_for_message(
        db_session,
        chat_message_id=msg_id,
        mark_type="nomem",
        detected_by="deterministic_token_match_v1",
        thread_id=99,
    )
    assert mark.thread_id == 99


async def test_invalid_mark_type_rejected_by_check(db_session) -> None:
    from sqlalchemy.exc import IntegrityError

    from bot.db.models import OffrecordMark

    msg_id = await _make_chat_message(db_session)
    bogus = OffrecordMark(
        mark_type="bogus_type",
        scope_type="message",
        scope_id=str(msg_id),
        chat_message_id=msg_id,
        detected_by="x",
    )
    db_session.add(bogus)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_cascade_delete_when_message_deleted(db_session) -> None:
    from bot.db.models import ChatMessage, OffrecordMark
    from bot.db.repos.offrecord_mark import OffrecordMarkRepo

    msg_id = await _make_chat_message(db_session)
    await OffrecordMarkRepo.create_for_message(
        db_session,
        chat_message_id=msg_id,
        mark_type="offrecord",
        detected_by="x",
    )

    await db_session.execute(
        ChatMessage.__table__.delete().where(ChatMessage.id == msg_id)
    )
    await db_session.flush()

    rows = await db_session.execute(
        select(OffrecordMark).where(OffrecordMark.chat_message_id == msg_id)
    )
    assert rows.scalars().all() == []


def test_offrecord_mark_metadata_smoke(app_env) -> None:
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert "offrecord_marks" in models.Base.metadata.tables
    table = models.Base.metadata.tables["offrecord_marks"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "mark_type",
        "scope_type",
        "scope_id",
        "chat_message_id",
        "thread_id",
        "set_by_user_id",
        "detected_by",
        "detected_at",
        "expires_at",
        "status",
    } == cols

    constraint_names = {c.name for c in table.constraints if c.name}
    assert "ck_offrecord_marks_mark_type" in constraint_names
    assert "ck_offrecord_marks_scope_type" in constraint_names
    assert "ck_offrecord_marks_status" in constraint_names

    fk_names = {fk.name for fk in table.foreign_keys if fk.name}
    assert "fk_offrecord_marks_chat_message_id" in fk_names
    assert "fk_offrecord_marks_set_by_user_id" in fk_names
