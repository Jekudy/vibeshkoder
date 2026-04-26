"""T1-03 acceptance tests — telegram_updates table + TelegramUpdateRepo."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")


def _random_update_id() -> int:
    return random.randint(1_000_000_000, 9_999_999_999)


def _random_chat_id() -> int:
    return -1_000_000_000_000 - random.randint(0, 999_999)


# ─── insert: live updates (with update_id) ─────────────────────────────────────────────────

async def test_insert_live_update_persists_row(db_session) -> None:
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    update_id = _random_update_id()
    chat_id = _random_chat_id()
    row = await TelegramUpdateRepo.insert(
        db_session,
        update_id=update_id,
        update_type="message",
        raw_json={"message": {"text": "hi"}},
        raw_hash="abc123",
        chat_id=chat_id,
        message_id=42,
    )

    assert row.id is not None
    assert row.update_id == update_id
    assert row.update_type == "message"
    assert row.raw_json == {"message": {"text": "hi"}}
    assert row.is_redacted is False

    fetched = await TelegramUpdateRepo.get_by_update_id(db_session, update_id)
    assert fetched is not None
    assert fetched.id == row.id


async def test_insert_duplicate_update_id_returns_existing_no_duplicate(db_session) -> None:
    from bot.db.models import TelegramUpdate
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    update_id = _random_update_id()
    first = await TelegramUpdateRepo.insert(
        db_session,
        update_id=update_id,
        update_type="message",
        raw_json={"v": 1},
    )
    second = await TelegramUpdateRepo.insert(
        db_session,
        update_id=update_id,
        update_type="message",
        raw_json={"v": 2},  # different payload — should NOT overwrite
    )

    assert second.id == first.id

    rows = await db_session.execute(
        select(TelegramUpdate).where(TelegramUpdate.update_id == update_id)
    )
    persisted = rows.scalars().all()
    assert len(persisted) == 1
    assert persisted[0].raw_json == {"v": 1}


# ─── insert: synthetic import updates (no update_id) ───────────────────────────────────────

async def test_insert_without_update_id_creates_independent_rows(db_session) -> None:
    """Synthetic import updates (NULL update_id) bypass the partial unique index. Two
    inserts produce two rows; the importer is responsible for its own dedup via raw_hash
    + ingestion_run_id."""
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    chat_id = _random_chat_id()
    a = await TelegramUpdateRepo.insert(
        db_session,
        update_type="import_message",
        raw_hash="hash-a",
        chat_id=chat_id,
        message_id=1,
    )
    b = await TelegramUpdateRepo.insert(
        db_session,
        update_type="import_message",
        raw_hash="hash-b",
        chat_id=chat_id,
        message_id=2,
    )

    assert a.id != b.id
    assert a.update_id is None
    assert b.update_id is None


# ─── ingestion_run_id FK behaviour ─────────────────────────────────────────────────────────

async def test_insert_with_ingestion_run_id(db_session) -> None:
    from bot.db.repos.ingestion_run import IngestionRunRepo
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    run = await IngestionRunRepo.create(db_session, run_type="live")
    row = await TelegramUpdateRepo.insert(
        db_session,
        update_id=_random_update_id(),
        update_type="message",
        ingestion_run_id=run.id,
    )

    assert row.ingestion_run_id == run.id


# ─── redaction columns ────────────────────────────────────────────────────────────────────

async def test_insert_with_redaction_marker(db_session) -> None:
    """Smoke that the redaction columns persist as set. Real T1-12 detector logic lives
    in a later ticket; this only verifies the schema accepts the marker."""
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    row = await TelegramUpdateRepo.insert(
        db_session,
        update_id=_random_update_id(),
        update_type="message",
        raw_json=None,  # already redacted
        raw_hash="hash-of-redacted",
        is_redacted=True,
        redaction_reason="offrecord",
    )

    assert row.is_redacted is True
    assert row.redaction_reason == "offrecord"
    assert row.raw_json is None


# ─── get_by_update_id ─────────────────────────────────────────────────────────────────────

async def test_get_by_update_id_returns_none_for_missing(db_session) -> None:
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    assert await TelegramUpdateRepo.get_by_update_id(db_session, 99_999_999_999) is None


# ─── metadata smoke ───────────────────────────────────────────────────────────────────────

def test_telegram_update_model_registered(app_env) -> None:
    """Offline smoke: model + columns + indexes registered (incl. partial unique)."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert "telegram_updates" in models.Base.metadata.tables
    table = models.Base.metadata.tables["telegram_updates"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "update_id",
        "update_type",
        "raw_json",
        "raw_hash",
        "received_at",
        "chat_id",
        "message_id",
        "ingestion_run_id",
        "is_redacted",
        "redaction_reason",
        "created_at",
    } == cols

    index_names = {ix.name for ix in table.indexes}
    assert "ix_telegram_updates_update_id" in index_names
    assert "ix_telegram_updates_update_type_received_at" in index_names
    assert "ix_telegram_updates_chat_id_message_id" in index_names

    # Partial unique on update_id
    update_id_index = next(
        ix for ix in table.indexes if ix.name == "ix_telegram_updates_update_id"
    )
    assert update_id_index.unique is True
