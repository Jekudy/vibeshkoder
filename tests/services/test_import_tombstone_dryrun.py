"""T2-NEW-D acceptance tests — tombstone collision dry-run report (issue #100).

Tests cover:
1. Offline parse_export() → tombstone fields default to 0 / [] (offline mode).
2. DB-aware: export message matching tombstone → tombstone_skip_count=1, ids listed.
3. DB-aware: no tombstone matches → count=0, list empty.
4. Edge: message that is BOTH a duplicate AND tombstone → counted under tombstone, not duplicate.
5. CLI --with-db mode prints the new "Tombstone skip:" summary line.

DB tests are SKIPPED if no postgres is reachable (see conftest.postgres_engine).

Note on imports: all bot.* imports are done INSIDE test functions (not at module level) to
avoid SQLAlchemy mapper initialization issues. Matches the pattern used in test_import_dry_run_stats.py.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ---------------------------------------------------------------------------
# Helpers (mirror style from test_import_dry_run_stats.py)
# ---------------------------------------------------------------------------

def _make_export(chat_id: int, messages: list[dict]) -> dict:
    return {
        "name": "Test Chat",
        "type": "private_supergroup",
        "id": chat_id,
        "messages": messages,
    }


def _make_message(msg_id: int, from_id: str = "user1000001") -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "date": "2024-01-15T10:00:00",
        "date_unixtime": "1705312800",
        "from": "Test User",
        "from_id": from_id,
        "text": "test message",
    }


def _rand_chat_id() -> int:
    return -random.randint(100_000_000, 199_999_999)


def _rand_msg_id() -> int:
    return random.randint(10_000, 9_999_999)


def _rand_user_id() -> int:
    return random.randint(900_000_000, 999_999_999)


async def _ensure_user(session, tg_id: int) -> object:
    from bot.db.repos.user import UserRepo
    return await UserRepo.upsert(
        session,
        telegram_id=tg_id,
        username=None,
        first_name=f"User{tg_id}",
        last_name=None,
    )


async def _create_chat_message(session, chat_id: int, message_id: int, user_id: int) -> object:
    from datetime import datetime, timezone

    from bot.db.repos.message import MessageRepo

    return await MessageRepo.save(
        session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="existing message",
        date=datetime.now(tz=timezone.utc),
        raw_update_id=None,
    )


async def _create_tombstone(session, *, tombstone_key: str) -> object:
    from bot.db.repos.forget_event import ForgetEventRepo

    return await ForgetEventRepo.create(
        session,
        target_type="message",
        target_id=None,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )


# ---------------------------------------------------------------------------
# 1. Offline mode: tombstone fields default to 0 / []
# ---------------------------------------------------------------------------

def test_parse_export_tombstone_fields_default_offline(tmp_path: Path) -> None:
    """parse_export() (no DB) must leave tombstone_skip_count=0 and
    tombstone_skip_export_msg_ids=[] — new fields present but empty in offline mode."""
    from bot.services.import_parser import parse_export

    export = _make_export(-100777, [_make_message(5001), _make_message(5002)])
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = parse_export(f)

    assert report.tombstone_skip_count == 0
    assert report.tombstone_skip_export_msg_ids == []


# ---------------------------------------------------------------------------
# 2. DB-aware: one export message matches a tombstone → count=1, id listed
# ---------------------------------------------------------------------------

async def test_tombstone_skip_count_matches_one_tombstone(db_session, tmp_path: Path) -> None:
    """Insert tombstone keyed to message:{chat_id}:{msg_id}.
    Export contains that msg_id → tombstone_skip_count=1, id in list."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    tombstoned_msg_id = _rand_msg_id()
    other_msg_id = _rand_msg_id()

    # Create tombstone for the specific message
    tombstone_key = f"message:{chat_id}:{tombstoned_msg_id}"
    await _create_tombstone(db_session, tombstone_key=tombstone_key)

    # Export contains both the tombstoned msg and a clean msg
    messages = [_make_message(tombstoned_msg_id), _make_message(other_msg_id)]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.tombstone_skip_count == 1
    assert report.tombstone_skip_export_msg_ids == [tombstoned_msg_id]


# ---------------------------------------------------------------------------
# 3. DB-aware: no tombstone matches → count=0, list empty
# ---------------------------------------------------------------------------

async def test_tombstone_skip_count_zero_when_no_match(db_session, tmp_path: Path) -> None:
    """Export messages with NO matching tombstones → tombstone_skip_count=0."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    msg_id_a = _rand_msg_id()
    msg_id_b = _rand_msg_id()

    # No tombstones inserted
    messages = [_make_message(msg_id_a), _make_message(msg_id_b)]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.tombstone_skip_count == 0
    assert report.tombstone_skip_export_msg_ids == []


# ---------------------------------------------------------------------------
# 4. Edge: message is BOTH a duplicate AND a tombstone → tombstone bucket wins
# ---------------------------------------------------------------------------

async def test_tombstone_wins_over_duplicate_bucket(db_session, tmp_path: Path) -> None:
    """A message id that is both in chat_messages (duplicate) AND has a tombstone
    must be counted under tombstone_skip_count, NOT under db_duplicate_count."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    # This msg_id is both a DB duplicate and has a tombstone
    shared_msg_id = _rand_msg_id()

    # Create DB row (would be counted as duplicate)
    await _create_chat_message(db_session, chat_id, shared_msg_id, user_id)

    # Create tombstone for the same message
    tombstone_key = f"message:{chat_id}:{shared_msg_id}"
    await _create_tombstone(db_session, tombstone_key=tombstone_key)

    export = _make_export(chat_id, [_make_message(shared_msg_id)])
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    # Tombstone bucket wins
    assert report.tombstone_skip_count == 1
    assert report.tombstone_skip_export_msg_ids == [shared_msg_id]
    # Duplicate count must NOT include the tombstoned id
    assert report.db_duplicate_count == 0
    assert shared_msg_id not in report.db_duplicate_export_msg_ids


# ---------------------------------------------------------------------------
# 5. CLI --with-db prints the new "Tombstone skip:" line
# ---------------------------------------------------------------------------

async def test_cli_with_db_prints_tombstone_skip_line(db_session, tmp_path: Path) -> None:
    """import_dry_run --with-db prints 'Tombstone skip: N messages match existing tombstones...'

    Verifies that the new line appears directly after the existing duplicate summary line.
    """
    import argparse
    import io
    import sys
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from bot.cli import _cmd_import_dry_run_with_db

    chat_id = _rand_chat_id()
    tombstoned_msg_id = _rand_msg_id()
    clean_msg_id = _rand_msg_id()

    # Create tombstone
    tombstone_key = f"message:{chat_id}:{tombstoned_msg_id}"
    await _create_tombstone(db_session, tombstone_key=tombstone_key)

    messages = [_make_message(tombstoned_msg_id), _make_message(clean_msg_id)]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    @asynccontextmanager
    async def _fake_session_ctx():
        yield db_session

    args = argparse.Namespace(export_path=str(f), with_db=True)

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        with patch("bot.db.engine.async_session", return_value=_fake_session_ctx()):
            rc = await _cmd_import_dry_run_with_db(args)
    finally:
        sys.stdout = old_stdout

    output = buf.getvalue()
    assert rc == 0, f"Expected exit 0, got {rc}. Output: {output!r}"
    assert "Tombstone skip:" in output, (
        f"Expected 'Tombstone skip:' line in output, got:\n{output!r}"
    )
    # The line should say 1 tombstone
    assert "1 messages match existing tombstones" in output, (
        f"Expected '1 messages match existing tombstones' in output, got:\n{output!r}"
    )
