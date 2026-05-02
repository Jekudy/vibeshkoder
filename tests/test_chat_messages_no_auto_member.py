from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.conftest import import_module


def test_save_chat_message_does_not_auto_mark_member(app_env, monkeypatch) -> None:
    handler = import_module("bot.handlers.chat_messages")
    # After Sprint #89 refactor, MessageRepo.save is called inside
    # persist_message_with_policy (bot.services.message_persistence), not directly
    # in the handler. Patch through the service module instead.
    persistence_module = import_module("bot.services.message_persistence")
    session = AsyncMock()
    raw_json = {"message_id": 101, "text": "hello from chat"}
    message_date = datetime.now(timezone.utc)
    message = SimpleNamespace(
        message_id=101,
        chat=SimpleNamespace(id=-1001234567890),
        from_user=SimpleNamespace(
            id=222,
            username="not_member_yet",
            first_name="Alice",
            last_name="Example",
        ),
        text="hello from chat",
        date=message_date,
        model_dump=Mock(return_value=raw_json),
        # Fields required by extract_normalized_fields / classify_message_kind:
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        caption=None,
        entities=None,
        caption_entities=None,
    )

    user_upsert = AsyncMock()
    user_set_member = AsyncMock()
    message_save = AsyncMock()
    fake_row = Mock()
    fake_row.id = 1
    fake_row.current_version_id = None
    message_save.return_value = fake_row

    fake_v1 = Mock()
    fake_v1.id = 10
    version_insert = AsyncMock(return_value=fake_v1)

    monkeypatch.setattr(handler.UserRepo, "upsert", user_upsert)
    monkeypatch.setattr(handler.UserRepo, "set_member", user_set_member)
    monkeypatch.setattr(persistence_module.MessageRepo, "save", message_save)
    monkeypatch.setattr(persistence_module.MessageVersionRepo, "insert_version", version_insert)
    # Also patch advisory_lock so no real postgres is needed.
    monkeypatch.setattr(persistence_module, "advisory_lock_chat_message", AsyncMock())

    asyncio.run(handler.save_chat_message(message, session))

    user_upsert.assert_awaited_once_with(
        session,
        telegram_id=222,
        username="not_member_yet",
        first_name="Alice",
        last_name="Example",
    )
    user_set_member.assert_not_called()
    # T1-09/10/11 added normalized kwargs to MessageRepo.save. T1-12 added
    # memory_policy + is_redacted (the handler now calls detect_policy on every
    # message; for plain text 'hello from chat' the policy is 'normal',
    # is_redacted=False). SimpleNamespace without media/reply attrs resolves
    # extras to None / 'text'. Sprint #89: the call now goes through
    # persist_message_with_policy but the kwargs contract is unchanged.
    message_save.assert_awaited_once_with(
        session,
        message_id=101,
        chat_id=-1001234567890,
        user_id=222,
        text="hello from chat",
        date=message_date,
        raw_json=raw_json,
        reply_to_message_id=None,
        message_thread_id=None,
        caption=None,
        message_kind="text",
        raw_update_id=None,
        memory_policy="normal",
        is_redacted=False,
    )


def test_save_chat_message_threads_raw_update_id(app_env, monkeypatch) -> None:
    """§3.10: save_chat_message passes raw_update_id from raw_update param to persist_message_with_policy."""
    handler = import_module("bot.handlers.chat_messages")
    persistence_module = import_module("bot.services.message_persistence")

    session = AsyncMock()
    raw_json = {"message_id": 202, "text": "raw update threading"}
    message_date = datetime.now(timezone.utc)
    message = SimpleNamespace(
        message_id=202,
        chat=SimpleNamespace(id=-1001234567890),
        from_user=SimpleNamespace(
            id=333,
            username="testuser",
            first_name="Bob",
            last_name=None,
        ),
        text="raw update threading",
        date=message_date,
        model_dump=Mock(return_value=raw_json),
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        caption=None,
        entities=None,
        caption_entities=None,
    )

    # Simulate a raw_update row surfaced by RawUpdatePersistenceMiddleware.
    raw_update = SimpleNamespace(id=999)

    user_upsert = AsyncMock()
    message_save = AsyncMock()
    fake_row = Mock()
    fake_row.id = 2
    fake_row.current_version_id = None
    message_save.return_value = fake_row

    fake_v1 = Mock()
    fake_v1.id = 20
    version_insert = AsyncMock(return_value=fake_v1)

    monkeypatch.setattr(handler.UserRepo, "upsert", user_upsert)
    monkeypatch.setattr(persistence_module.MessageRepo, "save", message_save)
    monkeypatch.setattr(persistence_module.MessageVersionRepo, "insert_version", version_insert)
    monkeypatch.setattr(persistence_module, "advisory_lock_chat_message", AsyncMock())

    asyncio.run(handler.save_chat_message(message, session, raw_update=raw_update))

    # The key assertion: raw_update_id must be 999, not None.
    message_save.assert_awaited_once_with(
        session,
        message_id=202,
        chat_id=-1001234567890,
        user_id=333,
        text="raw update threading",
        date=message_date,
        raw_json=raw_json,
        reply_to_message_id=None,
        message_thread_id=None,
        caption=None,
        message_kind="text",
        raw_update_id=999,
        memory_policy="normal",
        is_redacted=False,
    )
