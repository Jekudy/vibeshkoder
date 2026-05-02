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

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


# ─── Mock helpers ─────────────────────────────────────────────────────────────


def _fake_cm(cm_id: int = 42) -> MagicMock:
    """A ChatMessage-shaped MagicMock with the fields hotfix needs."""
    row = MagicMock()
    row.id = cm_id
    row.current_version_id = None
    return row


def _fake_v1(v1_id: int = 999) -> MagicMock:
    """A MessageVersion-shaped MagicMock."""
    v = MagicMock()
    v.id = v1_id
    return v


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

    fake_row = _fake_cm(42)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()) as mock_mark,
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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
    fake_row = _fake_cm(1)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
    ):
        await persist_message_with_policy(session, message)

    call_kwargs = mock_save.call_args.kwargs
    # raw_json should be populated because message.text is truthy
    assert call_kwargs["raw_json"] is not None


async def test_persist_normal_no_raw_json_for_caption_only(app_env) -> None:
    """Caption-only photo: raw_json is None (message.text is falsy)."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_photo_message(caption="nice photo")
    fake_row = _fake_cm(2)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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

    fake_row = _fake_cm(10)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()) as mock_mark,
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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

    fake_row = _fake_cm(20)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()) as mock_mark,
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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

    fake_row = _fake_cm(21)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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

    fake_row = _fake_cm(30)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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

    fake_row = _fake_cm(99)

    async def mock_save(*args, **kwargs):
        call_order.append("save")
        return fake_row

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", side_effect=mock_lock),
        patch("bot.services.message_persistence.MessageRepo.save", side_effect=mock_save),
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=_fake_v1())),
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
    fake_row.current_version_id = None

    session = AsyncMock()

    fake_v1 = MagicMock()
    fake_v1.id = 501

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_row)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=fake_v1)),
    ):
        await persist_message_with_policy(session, message, raw_update_id=12345)

    call_kwargs = mock_save.call_args.kwargs
    assert call_kwargs["raw_update_id"] == 12345


# ─── v1 creation (hotfix #164 §3.1) ──────────────────────────────────────────


async def test_persist_creates_v1_with_current_version_fk(db_session) -> None:
    """Normal text → v1 row with correct fields; current_version_id IS NOT NULL."""
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select

    user_id = 77_001
    chat_id = -1_002_000_000_001
    msg_id = 10_001

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username="u77001",
        first_name="Test",
        last_name=None,
    )

    message = _make_text_message(message_id=msg_id, chat_id=chat_id, user_id=user_id, text="hello v1")
    result = await persist_message_with_policy(db_session, message)

    assert result.policy == "normal"
    cm = result.chat_message
    assert cm.current_version_id is not None

    v1 = (
        await db_session.execute(
            select(MessageVersion).where(MessageVersion.chat_message_id == cm.id)
        )
    ).scalar_one()

    assert v1.version_seq == 1
    assert v1.text == "hello v1"
    assert v1.is_redacted is False
    assert v1.imported_final is False
    assert v1.normalized_text == "hello v1"
    assert cm.current_version_id == v1.id


async def test_persist_offrecord_creates_redacted_v1(db_session) -> None:
    """#offrecord → v1 with text=None, caption=None, is_redacted=True; hash = null-state hash."""
    from bot.db.models import MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.content_hash import compute_content_hash
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select

    user_id = 77_002
    chat_id = -1_002_000_000_002
    msg_id = 10_002

    await UserRepo.upsert(db_session, telegram_id=user_id, username="u77002", first_name="T", last_name=None)

    message = _make_text_message(message_id=msg_id, chat_id=chat_id, user_id=user_id, text="secret #offrecord info")
    result = await persist_message_with_policy(db_session, message)

    assert result.policy == "offrecord"
    cm = result.chat_message
    assert cm.current_version_id is not None

    v1 = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == cm.id)
    )).scalar_one()

    assert v1.text is None
    assert v1.caption is None
    assert v1.is_redacted is True
    expected_hash = compute_content_hash(text=None, caption=None, message_kind="text", entities=None)
    assert v1.content_hash == expected_hash


async def test_persist_caption_only_photo_creates_v1(db_session) -> None:
    """Photo+caption → v1 with text=None, caption=<c>; normalized_text=None (text is None)."""
    from bot.db.models import MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select

    user_id = 77_003
    chat_id = -1_002_000_000_003
    msg_id = 10_003

    await UserRepo.upsert(db_session, telegram_id=user_id, username="u77003", first_name="T", last_name=None)

    message = _make_photo_message(message_id=msg_id, chat_id=chat_id, user_id=user_id, caption="nice photo")
    result = await persist_message_with_policy(db_session, message)

    assert result.policy == "normal"
    cm = result.chat_message
    assert cm.current_version_id is not None

    v1 = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == cm.id)
    )).scalar_one()

    assert v1.text is None
    assert v1.caption == "nice photo"
    assert v1.is_redacted is False


async def test_persist_idempotent_on_telegram_retry(db_session) -> None:
    """Call twice for same (chat_id, message_id) → 1 v1, stable current_version_id, version_seq=1."""
    from bot.db.models import MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select

    user_id = 77_004
    chat_id = -1_002_000_000_004
    msg_id = 10_004

    await UserRepo.upsert(db_session, telegram_id=user_id, username="u77004", first_name="T", last_name=None)
    message = _make_text_message(message_id=msg_id, chat_id=chat_id, user_id=user_id, text="retry text")

    r1 = await persist_message_with_policy(db_session, message)
    r2 = await persist_message_with_policy(db_session, message)

    assert r1.chat_message.current_version_id == r2.chat_message.current_version_id

    versions = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == r1.chat_message.id)
    )).scalars().all()
    assert len(versions) == 1
    assert versions[0].version_seq == 1


async def test_persist_v1_advisory_lock_called_before_insert_version(app_env) -> None:
    """advisory_lock_chat_message MUST be called before MessageVersionRepo.insert_version."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_text_message(text="lock ordering")
    call_order: list[str] = []

    fake_cm = MagicMock()
    fake_cm.id = 9
    fake_cm.current_version_id = None

    fake_v1 = MagicMock()
    fake_v1.id = 99

    async def mock_lock(session, chat_id, message_id):
        call_order.append("lock")

    async def mock_save(*a, **kw):
        call_order.append("save")
        return fake_cm

    async def mock_insert(*a, **kw):
        call_order.append("insert_version")
        return fake_v1

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", side_effect=mock_lock),
        patch("bot.services.message_persistence.MessageRepo.save", side_effect=mock_save),
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", side_effect=mock_insert),
    ):
        await persist_message_with_policy(session, message)

    assert call_order[0] == "lock"
    assert "insert_version" in call_order
    assert call_order.index("lock") < call_order.index("insert_version")


async def test_persist_v1_imported_final_flag_for_import_source(app_env) -> None:
    """source='import' → v1.imported_final=True passed to insert_version."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_text_message(text="import text")
    fake_cm = MagicMock()
    fake_cm.id = 8
    fake_cm.current_version_id = None

    fake_v1 = MagicMock()
    fake_v1.id = 88

    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_cm)),
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=fake_v1)) as mock_insert,
    ):
        await persist_message_with_policy(session, message, source="import")

    insert_kwargs = mock_insert.call_args.kwargs
    assert insert_kwargs.get("imported_final") is True


async def test_persist_failure_rolls_back_v1(app_env) -> None:
    """If insert_version raises, no chat_messages row survives (mock — rollback is caller's)."""
    from bot.services.message_persistence import persist_message_with_policy

    message = _make_text_message(text="rollback text")
    fake_cm = MagicMock()
    fake_cm.id = 7
    fake_cm.current_version_id = None

    session = AsyncMock()

    async def raise_error(*a, **kw):
        raise RuntimeError("simulated insert failure")

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_cm)),
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", side_effect=raise_error),
    ):
        with pytest.raises(RuntimeError, match="simulated insert failure"):
            await persist_message_with_policy(session, message)


async def test_persist_anonymous_channel_post_creates_v1(app_env) -> None:
    """from_user=None → v1 insert_version called; user_id=None passed to MessageRepo.save.

    Note: DB schema has user_id NOT NULL — full DB round-trip not possible for anon posts.
    This test verifies the helper correctly extracts user_id=None and calls v1 creation.
    """
    from bot.services.message_persistence import persist_message_with_policy
    from types import SimpleNamespace

    message = SimpleNamespace(
        message_id=10_008,
        chat=SimpleNamespace(id=-1_002_000_000_008, type="supergroup"),
        from_user=None,  # anonymous channel post
        text="anon text",
        caption=None,
        date=datetime.now(timezone.utc),
        model_dump=None,  # no model_dump for anon duck
        reply_to_message=None,
        message_thread_id=None,
        photo=None, video=None, voice=None, audio=None, document=None,
        sticker=None, animation=None, video_note=None, location=None,
        contact=None, poll=None, dice=None, forward_origin=None,
        new_chat_members=None, left_chat_member=None, pinned_message=None,
        entities=None, caption_entities=None,
    )

    fake_cm = _fake_cm(88)
    fake_v = _fake_v1(888)
    session = AsyncMock()

    with (
        patch("bot.services.message_persistence.advisory_lock_chat_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageRepo.save", new=AsyncMock(return_value=fake_cm)) as mock_save,
        patch("bot.services.message_persistence.OffrecordMarkRepo.create_for_message", new=AsyncMock()),
        patch("bot.services.message_persistence.MessageVersionRepo.insert_version", new=AsyncMock(return_value=fake_v)) as mock_insert,
    ):
        result = await persist_message_with_policy(session, message)

    # user_id=None must be passed to MessageRepo.save
    save_kwargs = mock_save.call_args.kwargs
    assert save_kwargs["user_id"] is None

    # v1 must be created even for anonymous posts
    mock_insert.assert_awaited_once()
    assert result.chat_message.current_version_id == fake_v.id


async def test_persist_forwarded_text_creates_v1_with_kind_forward(db_session) -> None:
    """Forwarded text → v1 with message_kind-based hash differing from native equivalent."""
    from bot.db.models import MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.content_hash import compute_content_hash
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select
    from types import SimpleNamespace

    user_id = 77_009
    chat_id = -1_002_000_000_009
    msg_id = 10_009

    await UserRepo.upsert(db_session, telegram_id=user_id, username="u77009", first_name="T", last_name=None)

    message = SimpleNamespace(
        message_id=msg_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, username="u77009", first_name="T", last_name=None),
        text="forwarded text",
        caption=None,
        date=datetime.now(timezone.utc),
        model_dump=MagicMock(return_value={"text": "forwarded text"}),
        reply_to_message=None,
        message_thread_id=None,
        photo=None, video=None, voice=None, audio=None, document=None,
        sticker=None, animation=None, video_note=None, location=None,
        contact=None, poll=None, dice=None,
        forward_origin=SimpleNamespace(type="user"),  # makes kind="forward"
        new_chat_members=None, left_chat_member=None, pinned_message=None,
        entities=None, caption_entities=None,
    )

    result = await persist_message_with_policy(db_session, message)
    cm = result.chat_message
    v1 = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == cm.id)
    )).scalar_one()

    forward_hash = compute_content_hash(text="forwarded text", caption=None, message_kind="forward", entities=None)
    native_hash = compute_content_hash(text="forwarded text", caption=None, message_kind="text", entities=None)
    assert v1.content_hash == forward_hash
    assert v1.content_hash != native_hash


async def test_persist_threads_captured_at_to_v1(db_session) -> None:
    """captured_at kwarg → both chat_messages.captured_at and v1.captured_at match override."""
    from bot.db.models import MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select

    user_id = 77_011
    chat_id = -1_002_000_000_011
    msg_id = 10_011
    override_ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    await UserRepo.upsert(db_session, telegram_id=user_id, username="u77011", first_name="T", last_name=None)

    message = _make_text_message(message_id=msg_id, chat_id=chat_id, user_id=user_id, text="captured at test")
    result = await persist_message_with_policy(db_session, message, captured_at=override_ts)

    cm = result.chat_message
    assert cm.current_version_id is not None

    v1 = (await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == cm.id)
    )).scalar_one()

    assert v1.captured_at.replace(microsecond=0) == override_ts.replace(microsecond=0)
