"""T1-05 acceptance tests — extended chat_messages columns.

Verifies that:
- legacy rows (created with only the original 8 columns) survive after migration; new
  columns get sensible defaults
- new rows can be saved with ALL the new fields
- check constraints reject invalid memory_policy / visibility values
- model + migration shapes match (offline metadata smoke)
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

_user_id_counter = itertools.count(start=8_700_000_000)
_message_id_counter = itertools.count(start=900_000)


def _next_user_id() -> int:
    return next(_user_id_counter)


def _next_message_id() -> int:
    return next(_message_id_counter)


def _random_chat_id() -> int:
    return -1_000_000_000_000 - (next(_message_id_counter) % 1_000_000)


async def _ensure_user(session, telegram_id: int) -> None:
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        session,
        telegram_id=telegram_id,
        username=f"u{telegram_id}",
        first_name="T",
        last_name=None,
    )


# ─── existing-row survival ────────────────────────────────────────────────────────────────

async def test_legacy_row_shape_persists_with_defaults(db_session) -> None:
    """Inserting a row with ONLY the original 8 columns (the shape gatekeeper bot wrote
    for years before T1-05) must succeed — server defaults populate memory_policy and
    visibility."""
    from bot.db.models import ChatMessage

    user_id = _next_user_id()
    chat_id = _random_chat_id()
    message_id = _next_message_id()
    when = datetime.now(timezone.utc)

    await _ensure_user(db_session, user_id)

    legacy = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="legacy hello",
        date=when,
        raw_json=None,
    )
    db_session.add(legacy)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(ChatMessage).where(ChatMessage.id == legacy.id)
        )
    ).scalar_one()
    await db_session.refresh(fetched)

    # Server defaults populated:
    assert fetched.memory_policy == "normal"
    assert fetched.visibility == "member"
    assert fetched.is_redacted is False
    # Optional fields default to None:
    assert fetched.reply_to_message_id is None
    assert fetched.message_thread_id is None
    assert fetched.caption is None
    assert fetched.message_kind is None
    assert fetched.current_version_id is None
    assert fetched.content_hash is None
    assert fetched.updated_at is None
    assert fetched.raw_update_id is None


# ─── new fields persist ────────────────────────────────────────────────────────────────────

async def test_new_fields_persist(db_session) -> None:
    from bot.db.models import ChatMessage

    user_id = _next_user_id()
    chat_id = _random_chat_id()
    message_id = _next_message_id()
    when = datetime.now(timezone.utc)
    updated = datetime.now(timezone.utc)

    await _ensure_user(db_session, user_id)

    row = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hello",
        date=when,
        reply_to_message_id=message_id - 1,
        message_thread_id=42,
        caption="caption text",
        message_kind="text",
        memory_policy="normal",
        visibility="member",
        is_redacted=True,
        content_hash="abc123",
        updated_at=updated,
    )
    db_session.add(row)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == row.id))
    ).scalar_one()

    assert fetched.reply_to_message_id == message_id - 1
    assert fetched.message_thread_id == 42
    assert fetched.caption == "caption text"
    assert fetched.message_kind == "text"
    assert fetched.content_hash == "abc123"
    assert fetched.is_redacted is True
    assert fetched.updated_at is not None


# ─── check constraints ─────────────────────────────────────────────────────────────────────

async def test_invalid_memory_policy_rejected(db_session) -> None:
    """memory_policy must be one of normal/nomem/offrecord/forgotten."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.models import ChatMessage

    user_id = _next_user_id()
    chat_id = _random_chat_id()
    when = datetime.now(timezone.utc)
    await _ensure_user(db_session, user_id)

    bogus = ChatMessage(
        message_id=_next_message_id(),
        chat_id=chat_id,
        user_id=user_id,
        text="x",
        date=when,
        memory_policy="totally_not_valid",
    )
    db_session.add(bogus)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_invalid_visibility_rejected(db_session) -> None:
    from sqlalchemy.exc import IntegrityError

    from bot.db.models import ChatMessage

    user_id = _next_user_id()
    chat_id = _random_chat_id()
    when = datetime.now(timezone.utc)
    await _ensure_user(db_session, user_id)

    bogus = ChatMessage(
        message_id=_next_message_id(),
        chat_id=chat_id,
        user_id=user_id,
        text="x",
        date=when,
        visibility="cosmic",
    )
    db_session.add(bogus)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ─── valid policy values ───────────────────────────────────────────────────────────────────

async def test_all_valid_memory_policy_values_accepted(db_session) -> None:
    from bot.db.models import ChatMessage

    when = datetime.now(timezone.utc)
    for policy in ("normal", "nomem", "offrecord", "forgotten"):
        user_id = _next_user_id()
        chat_id = _random_chat_id()
        await _ensure_user(db_session, user_id)
        row = ChatMessage(
            message_id=_next_message_id(),
            chat_id=chat_id,
            user_id=user_id,
            text="x",
            date=when,
            memory_policy=policy,
        )
        db_session.add(row)
        await db_session.flush()
        assert row.memory_policy == policy


# ─── messageRepo.save still works (regression) ─────────────────────────────────────────────

async def test_message_repo_save_still_works_with_extended_columns(db_session) -> None:
    """T0-03 idempotent save must keep working after the column extension."""
    from bot.db.repos.message import MessageRepo
    from bot.db.repos.user import UserRepo

    user_id = _next_user_id()
    chat_id = _random_chat_id()
    message_id = _next_message_id()
    when = datetime.now(timezone.utc)

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username="u",
        first_name="U",
        last_name=None,
    )

    first = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hi",
        date=when,
        raw_json=None,
    )
    second = await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="hi (dup)",
        date=when,
        raw_json=None,
    )

    assert second.id == first.id
    # Server defaults applied to the existing-style insert
    assert first.memory_policy == "normal"
    assert first.visibility == "member"


# ─── metadata smoke ────────────────────────────────────────────────────────────────────────

def test_chat_messages_metadata_includes_new_columns_and_indexes(app_env) -> None:
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    table = models.Base.metadata.tables["chat_messages"]
    cols = {c.name for c in table.columns}
    new_cols = {
        "raw_update_id",
        "reply_to_message_id",
        "message_thread_id",
        "caption",
        "message_kind",
        "current_version_id",
        "memory_policy",
        "visibility",
        "is_redacted",
        "content_hash",
        "updated_at",
    }
    assert new_cols.issubset(cols)
    index_names = {ix.name for ix in table.indexes}
    assert {
        "ix_chat_messages_chat_msg",
        "ix_chat_messages_chat_id_date",
        "ix_chat_messages_reply_to_message_id",
        "ix_chat_messages_message_thread_id",
        "ix_chat_messages_memory_policy",
        "ix_chat_messages_content_hash",
    }.issubset(index_names)
    constraint_names = {c.name for c in table.constraints if c.name}
    assert "ck_chat_messages_memory_policy" in constraint_names
    assert "ck_chat_messages_visibility" in constraint_names

    # FK to telegram_updates.id must be present and named (Codex/Claude both flagged
    # FK-name divergence as a real source of create_all / alembic drift).
    fk_names = {fk.name for fk in table.foreign_keys if fk.name}
    assert "fk_chat_messages_raw_update_id" in fk_names
