"""Tests for persist_message_with_policy helper (Sprint #89, Commit 1).

Coverage:
- normal policy: text-only message
- nomem policy: text-only message
- offrecord policy: text-only message (content nulled, is_redacted=True)
- caption-only photo (offrecord via caption)
- SimpleNamespace duck (importer-shaped)
- PersistResult fields populated correctly
- advisory lock is called before MessageRepo.save
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_text_message(
    *,
    message_id: int = 100,
    chat_id: int = -1001234567890,
    user_id: int = 999,
    text: str | None = "hello world",
    caption: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"u{user_id}",
            first_name="Test",
            last_name=None,
        ),
        text=text,
        caption=caption,
        date=datetime.now(timezone.utc),
        model_dump=MagicMock(return_value={"message_id": message_id, "text": text}),
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
        entities=None,
        caption_entities=None,
    )


def _make_photo_message(
    *,
    message_id: int = 200,
    chat_id: int = -1001234567890,
    user_id: int = 998,
    caption: str | None = "nice photo",
) -> SimpleNamespace:
    """Caption-only photo message (no text field)."""
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"u{user_id}",
            first_name="Photo",
            last_name=None,
        ),
        text=None,
        caption=caption,
        date=datetime.now(timezone.utc),
        model_dump=MagicMock(return_value={"message_id": message_id}),
        reply_to_message=None,
        message_thread_id=None,
        photo=[SimpleNamespace(file_id="abc", file_size=100, width=100, height=100)],
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
        entities=None,
        caption_entities=None,
    )


def _make_duck_message(
    *,
    message_id: int = 300,
    chat_id: int = -1001234567890,
    user_id: int = 997,
    text: str | None = "importer text",
) -> SimpleNamespace:
    """Importer-shaped SimpleNamespace duck (no model_dump, no aiogram attrs)."""
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=None,
            first_name="Import",
            last_name=None,
        ),
        text=text,
        caption=None,
        date=datetime.now(timezone.utc),
        # NOTE: no model_dump — duck doesn't have this
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
        entities=None,
        caption_entities=None,
    )


# ─── PersistResult import ─────────────────────────────────────────────────────


def test_persist_result_is_importable(app_env) -> None:
    """PersistResult must be importable from bot.services.message_persistence."""
    from bot.services.message_persistence import PersistResult  # noqa: F401


def test_persist_message_with_policy_is_importable(app_env) -> None:
    from bot.services.message_persistence import persist_message_with_policy  # noqa: F401


# ─── Normal policy ────────────────────────────────────────────────────────────


async def test_persist_normal_text_message_returns_result(app_env) -> None:
    """Normal text message: policy='normal', mark not created, content preserved."""
    from bot.services.message_persistence import PersistResult, persist_message_with_policy

    message = _make_text_message(text="hello world")

    fake_row = MagicMock()
    fake_row.id = 42

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()) as mock_lock,
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()) as mock_mark,
    ):
        result = await persist_message_with_policy(session, message)

    assert isinstance(result, PersistResult)
    assert result.policy == "normal"
    assert result.chat_message is fake_row
    assert result.is_offrecord_mark_created is False

    # OffrecordMarkRepo should NOT be called for normal policy
    mock_mark.assert_not_called()

    # MessageRepo.save MUST be called with text preserved
    mock_save.assert_awaited_once()
    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["text"] == "hello world"
    assert call_kwargs["memory_policy"] == "normal"
    assert call_kwargs["is_redacted"] is False


async def test_persist_normal_sets_raw_json_for_text_message(app_env) -> None:
    """Normal text message: raw_json is set because message.text is truthy."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_text_message(text="some text")
    fake_row = MagicMock()
    fake_row.id = 1

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
    ):
        await persist_message_with_policy(session, message)

    call_kwargs = mock_save.call_args.kwargs
    # raw_json should be populated because message.text is truthy
    assert call_kwargs["raw_json"] is not None


async def test_persist_normal_no_raw_json_for_caption_only(app_env) -> None:
    """Caption-only photo: raw_json is None (message.text is falsy)."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_photo_message(caption="nice photo")
    fake_row = MagicMock()
    fake_row.id = 2

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
    ):
        await persist_message_with_policy(session, message)

    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["raw_json"] is None
    assert call_kwargs["caption"] == "nice photo"


# ─── Nomem policy ─────────────────────────────────────────────────────────────


async def test_persist_nomem_text_message(app_env) -> None:
    """#nomem text: policy='nomem', mark created, content NOT nulled."""
    from bot.services.message_persistence import PersistResult, persist_message_with_policy

    message = _make_text_message(text="important #nomem note")

    fake_row = MagicMock()
    fake_row.id = 10

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()) as mock_mark,
    ):
        result = await persist_message_with_policy(session, message)

    assert isinstance(result, PersistResult)
    assert result.policy == "nomem"
    assert result.is_offrecord_mark_created is True

    # Content must be preserved for nomem
    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["text"] == "important #nomem note"
    assert call_kwargs["is_redacted"] is False
    assert call_kwargs["memory_policy"] == "nomem"

    # OffrecordMarkRepo must be called
    mock_mark.assert_awaited_once()
    mark_kwargs = mock_mark.call_args.kwargs
    assert mark_kwargs["mark_type"] == "nomem"
    assert mark_kwargs["chat_message_id"] == 10


# ─── Offrecord policy ─────────────────────────────────────────────────────────


async def test_persist_offrecord_text_message_nulls_content(app_env) -> None:
    """#offrecord text: content fields nulled, is_redacted=True, mark created."""
    from bot.services.message_persistence import PersistResult, persist_message_with_policy

    message = _make_text_message(text="secret #offrecord info")

    fake_row = MagicMock()
    fake_row.id = 20

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()) as mock_mark,
    ):
        result = await persist_message_with_policy(session, message)

    assert isinstance(result, PersistResult)
    assert result.policy == "offrecord"
    assert result.is_offrecord_mark_created is True

    # Content MUST be nulled for offrecord
    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["text"] is None
    assert call_kwargs["caption"] is None
    assert call_kwargs["raw_json"] is None
    assert call_kwargs["is_redacted"] is True
    assert call_kwargs["memory_policy"] == "offrecord"

    # OffrecordMarkRepo must be called with offrecord mark type
    mock_mark.assert_awaited_once()
    mark_kwargs = mock_mark.call_args.kwargs
    assert mark_kwargs["mark_type"] == "offrecord"
    assert mark_kwargs["chat_message_id"] == 20


async def test_persist_offrecord_caption_only_photo(app_env) -> None:
    """#offrecord in caption of photo: content nulled, is_redacted=True."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_photo_message(caption="photo #offrecord secret")

    fake_row = MagicMock()
    fake_row.id = 21

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
    ):
        result = await persist_message_with_policy(session, message)

    assert result.policy == "offrecord"
    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["text"] is None
    assert call_kwargs["caption"] is None
    assert call_kwargs["raw_json"] is None
    assert call_kwargs["is_redacted"] is True


# ─── Duck (importer-shaped) ───────────────────────────────────────────────────


async def test_persist_duck_message_normal(app_env) -> None:
    """SimpleNamespace duck (no model_dump): persists without error."""
    from bot.services.message_persistence import PersistResult, persist_message_with_policy

    message = _make_duck_message(text="importer text")

    fake_row = MagicMock()
    fake_row.id = 30

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
    ):
        result = await persist_message_with_policy(session, message, source="import")

    assert isinstance(result, PersistResult)
    assert result.policy == "normal"
    # raw_json must be None (duck has text but no model_dump → getattr returns None)
    call_kwargs = mock_save.call_args.kwargs
    # The duck has text truthy, but no model_dump — helper uses getattr with None fallback
    # so raw_json must be None
    assert call_kwargs["raw_json"] is None


# ─── Advisory lock ordering ───────────────────────────────────────────────────


async def test_persist_advisory_lock_called_before_save(app_env) -> None:
    """Advisory lock MUST be called before MessageRepo.save — Sprint #80 invariant."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_text_message(text="hello")
    call_order: list[str] = []

    async def mock_lock(session, chat_id, message_id):
        call_order.append("lock")

    fake_row = MagicMock()
    fake_row.id = 99

    async def mock_save(*args, **kwargs):
        call_order.append("save")
        return fake_row

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", side_effect=mock_lock),
        patch("bot.services.message_persistence.MessageRepo.save", side_effect=mock_save),
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
    ):
        await persist_message_with_policy(session, message)

    assert call_order[0] == "lock", f"Expected lock first, got: {call_order}"
    assert "save" in call_order


# ─── raw_update_id passthrough ────────────────────────────────────────────────


async def test_persist_passes_raw_update_id(app_env) -> None:
    """raw_update_id kwarg is passed to MessageRepo.save."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_text_message(text="hello")
    fake_row = MagicMock()
    fake_row.id = 50

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
    ):
        await persist_message_with_policy(session, message, raw_update_id=12345)

    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["raw_update_id"] == 12345
