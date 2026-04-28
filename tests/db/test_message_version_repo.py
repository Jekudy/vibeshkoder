"""T1-06 acceptance tests — message_versions table + MessageVersionRepo.

Outer-tx isolation. Tests do NOT call ``session.commit()``.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=8_800_000_000)
_msg_counter = itertools.count(start=950_000)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _random_chat_id() -> int:
    return -1_000_000_000_000 - (next(_msg_counter) % 1_000_000)


async def _make_chat_message(db_session, *, text: str = "hello") -> int:
    """Create a chat_messages row and return its id."""
    from bot.db.models import ChatMessage
    from bot.db.repos.user import UserRepo

    user_id = _next_user()
    chat_id = _random_chat_id()
    when = datetime.now(timezone.utc)

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username="u",
        first_name="U",
        last_name=None,
    )

    msg = ChatMessage(
        message_id=_next_msg_id(),
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=when,
    )
    db_session.add(msg)
    await db_session.flush()
    return msg.id


# ─── insert_version: new ───────────────────────────────────────────────────────────────────


async def test_insert_first_version_creates_v1(db_session) -> None:
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session, text="hello")

    v = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="hash-v1",
        text="hello",
        normalized_text="hello",
    )

    assert v.id is not None
    assert v.chat_message_id == msg_id
    assert v.version_seq == 1
    assert v.content_hash == "hash-v1"
    assert v.text == "hello"
    assert v.is_redacted is False
    assert v.captured_at is not None


async def test_insert_second_version_with_different_hash_increments_seq(db_session) -> None:
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)

    v1 = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="hash-1",
        text="first",
    )
    v2 = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="hash-2",
        text="second",
    )

    assert v1.version_seq == 1
    assert v2.version_seq == 2
    assert v1.id != v2.id


# ─── insert_version: idempotent on duplicate hash ─────────────────────────────────────────


async def test_insert_duplicate_hash_returns_existing_no_new_version(db_session) -> None:
    from bot.db.models import MessageVersion
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)

    first = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="same-hash",
        text="x",
    )
    second = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="same-hash",
        text="x (would be dup)",
    )

    assert second.id == first.id
    rows = await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == msg_id)
    )
    assert len(rows.scalars().all()) == 1


# ─── get_max_version_seq ───────────────────────────────────────────────────────────────────


async def test_get_max_version_seq_zero_for_no_versions(db_session) -> None:
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)
    assert await MessageVersionRepo.get_max_version_seq(db_session, msg_id) == 0


async def test_get_max_version_seq_after_inserts(db_session) -> None:
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)
    await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="h1",
    )
    await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="h2",
    )
    assert await MessageVersionRepo.get_max_version_seq(db_session, msg_id) == 2


# ─── get_by_hash ───────────────────────────────────────────────────────────────────────────


async def test_get_by_hash_returns_none_for_missing(db_session) -> None:
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)
    assert (await MessageVersionRepo.get_by_hash(db_session, msg_id, "nope")) is None


# ─── current_version_id FK closure (T1-05 forward-ref → real FK) ──────────────────────────


async def test_chat_messages_current_version_id_fk_links_to_message_versions(db_session) -> None:
    from bot.db.models import ChatMessage
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session, text="hi")
    v1 = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="h",
        text="hi",
    )

    msg = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == msg_id))
    ).scalar_one()
    msg.current_version_id = v1.id
    await db_session.flush()

    fetched = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.id == msg_id))
    ).scalar_one()
    assert fetched.current_version_id == v1.id


# ─── unique (chat_message_id, version_seq) ───────────────────────────────────────────────


async def test_duplicate_version_seq_rejected_by_unique_constraint(db_session) -> None:
    """Direct ORM insert bypassing the repo: two rows with the same
    (chat_message_id, version_seq) must violate the unique constraint."""
    from sqlalchemy.exc import IntegrityError

    from bot.db.models import MessageVersion

    msg_id = await _make_chat_message(db_session)

    db_session.add(
        MessageVersion(
            chat_message_id=msg_id,
            version_seq=1,
            content_hash="a",
        )
    )
    await db_session.flush()

    db_session.add(
        MessageVersion(
            chat_message_id=msg_id,
            version_seq=1,
            content_hash="b",  # same seq, diff hash
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ─── cascade behaviour ─────────────────────────────────────────────────────────────────────


async def test_versions_deleted_when_chat_message_deleted(db_session) -> None:
    """ON DELETE CASCADE from chat_messages.id wipes child versions."""
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)
    await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="h",
    )

    await db_session.execute(ChatMessage.__table__.delete().where(ChatMessage.id == msg_id))
    await db_session.flush()

    rows = await db_session.execute(
        select(MessageVersion).where(MessageVersion.chat_message_id == msg_id)
    )
    assert rows.scalars().all() == []


# ─── metadata smoke ────────────────────────────────────────────────────────────────────────


def test_message_version_metadata_smoke(app_env) -> None:
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert "message_versions" in models.Base.metadata.tables

    table = models.Base.metadata.tables["message_versions"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "chat_message_id",
        "version_seq",
        "text",
        "caption",
        "normalized_text",
        "entities_json",
        "edit_date",
        "captured_at",
        "content_hash",
        "raw_update_id",
        "is_redacted",
        "imported_final",
    } == cols

    constraint_names = {c.name for c in table.constraints if c.name}
    assert "uq_message_versions_chat_message_seq" in constraint_names

    fk_names = {fk.name for fk in table.foreign_keys if fk.name}
    assert "fk_message_versions_chat_message_id" in fk_names
    assert "fk_message_versions_raw_update_id" in fk_names

    index_names = {ix.name for ix in table.indexes}
    assert "ix_message_versions_content_hash" in index_names
    assert "ix_message_versions_captured_at" in index_names
    assert "ix_message_versions_chat_message_id" in index_names

    # T1-05's forward-ref is now a real FK on chat_messages.current_version_id
    cm_table = models.Base.metadata.tables["chat_messages"]
    cm_fk_names = {fk.name for fk in cm_table.foreign_keys if fk.name}
    assert "fk_chat_messages_current_version_id" in cm_fk_names

    # T2-81: UNIQUE (chat_message_id, content_hash) must exist as a DB-level constraint
    assert "uq_message_versions_chat_message_content_hash" in constraint_names


# ─── T2-81: concurrent-insert idempotency ─────────────────────────────────────────────────


async def test_insert_version_concurrent_same_hash_returns_existing(db_session) -> None:
    """insert_version called twice with the same hash must return the same row (get_by_hash
    short-circuit path).  No IntegrityError must surface."""
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)

    v1 = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="dup-hash",
        text="original",
    )
    # Second call — same content_hash — must return v1, not create v2
    v2 = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="dup-hash",
        text="would be dup",
    )

    assert v2.id == v1.id
    assert v2.version_seq == 1


async def test_insert_version_integrity_error_path_reselects_existing(db_session) -> None:
    """Simulate the race-condition IntegrityError path: insert a row directly (bypassing the
    get_by_hash short-circuit), then call insert_version — it must catch the IntegrityError
    from the savepoint, reselect, and return the pre-existing row."""
    from bot.db.models import MessageVersion
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)

    # Insert directly via ORM to bypass insert_version's get_by_hash guard
    existing_row = MessageVersion(
        chat_message_id=msg_id,
        version_seq=1,
        content_hash="race-hash",
        text="winner",
    )
    db_session.add(existing_row)
    await db_session.flush()

    # Now call insert_version — get_by_hash will find the row via the first SELECT,
    # but if somehow it didn't (race), the savepoint must catch IntegrityError.
    # To truly exercise the IntegrityError path, we'd need two async tasks; instead
    # we verify that insert_version is idempotent regardless of insertion order.
    result = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="race-hash",
        text="racer",
    )

    assert result.id == existing_row.id
    assert result.version_seq == 1


async def test_insert_version_savepoint_branch_reselects_on_integrity_error(
    db_session, monkeypatch
) -> None:
    """Force the savepoint+reselect branch by bypassing get_by_hash on first call.

    Setup: insert v1 via insert_version (real path). Then monkeypatch
    MessageVersionRepo.get_by_hash to return None ONCE (simulating a stale read /
    TOCTOU window), then defer to the real impl on subsequent calls. Calling
    insert_version with the same content_hash MUST:
      - Enter begin_nested() (because get_by_hash returned None).
      - Hit IntegrityError on the duplicate INSERT (UNIQUE constraint).
      - Reselect via the unmocked get_by_hash and return the existing row.
      - NOT propagate IntegrityError.
    """
    from bot.db.repos.message_version import MessageVersionRepo

    msg_id = await _make_chat_message(db_session)

    # Insert v1 through the real path so the UNIQUE constraint row exists in the DB.
    v1 = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="toctou-hash",
        text="first writer wins",
    )

    # Patch get_by_hash to lie ONCE — return None regardless of DB state.
    real_get_by_hash = MessageVersionRepo.get_by_hash
    call_count = {"n": 0}

    async def fake_get_by_hash(session, chat_message_id, content_hash):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # lie — simulate stale read / TOCTOU window
        return await real_get_by_hash(session, chat_message_id, content_hash)

    monkeypatch.setattr(MessageVersionRepo, "get_by_hash", staticmethod(fake_get_by_hash))

    # insert_version sees get_by_hash → None (lie), enters begin_nested, attempts INSERT,
    # hits IntegrityError from uq_message_versions_chat_message_content_hash,
    # rolls back the savepoint, then calls real get_by_hash → returns v1.
    result = await MessageVersionRepo.insert_version(
        db_session,
        chat_message_id=msg_id,
        content_hash="toctou-hash",
        text="late writer loses",
    )

    assert result.id == v1.id
    assert result.version_seq == v1.version_seq
    assert call_count["n"] == 2  # one lie + one real reselect inside except branch
