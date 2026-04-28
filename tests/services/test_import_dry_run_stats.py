"""T2-02 acceptance tests — import_dry_run_stats (dry-run duplicate / policy / broken-reply stats).

Tests cover:
1. New db_* fields default to 0/[] when parse_export() called without DB context (backward compat).
2. DB duplicate detection: export ids already in chat_messages → db_duplicate_count/ids.
3. DB broken reply chain: reply target missing from DB → db_broken_reply_count.
4. CLI --with-db prints operator summary line.

DB tests are SKIPPED if no postgres is reachable (see conftest.postgres_engine).

Note on imports: all bot.* imports are done INSIDE test functions (not at module level) to
avoid SQLAlchemy mapper initialization issues caused by conftest's ``_clear_modules`` running
between test collection and test execution. This matches the pattern used in existing DB tests.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"

pytestmark = pytest.mark.usefixtures("app_env")


# ---------------------------------------------------------------------------
# Helpers for building minimal export fixtures
# ---------------------------------------------------------------------------

def _make_export(chat_id: int, messages: list[dict]) -> dict:
    return {
        "name": "Test Chat",
        "type": "private_supergroup",
        "id": chat_id,
        "messages": messages,
    }


def _make_message(msg_id: int, from_id: str = "user1000001", reply_to: int | None = None) -> dict:
    msg: dict = {
        "id": msg_id,
        "type": "message",
        "date": "2024-01-15T10:00:00",
        "date_unixtime": "1705312800",
        "from": "Test User",
        "from_id": from_id,
        "text": "test message",
    }
    if reply_to is not None:
        msg["reply_to_message_id"] = reply_to
    return msg


def _rand_chat_id() -> int:
    """Negative chat id in the Telegram group range."""
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


async def _create_chat_message(
    session,
    chat_id: int,
    message_id: int,
    user_id: int,
) -> object:
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


# ---------------------------------------------------------------------------
# 1. Backward compat: parse_export (sync, no DB) leaves db_* fields at defaults
# ---------------------------------------------------------------------------

def test_parse_export_db_fields_default_to_zero(tmp_path: Path) -> None:
    """parse_export() (no DB) must leave db_duplicate_count=0, db_duplicate_export_msg_ids=[],
    db_broken_reply_count=0. This verifies backward compatibility with T2-01 callers."""
    from bot.services.import_parser import parse_export

    export = _make_export(-100111, [_make_message(1001), _make_message(1002)])
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = parse_export(f)

    assert report.db_duplicate_count == 0
    assert report.db_duplicate_export_msg_ids == []
    assert report.db_broken_reply_count == 0


# ---------------------------------------------------------------------------
# 2. DB duplicate detection
# ---------------------------------------------------------------------------

async def test_db_duplicate_count_matches_colliding_ids(db_session, tmp_path: Path) -> None:
    """Pre-populate chat_messages with N rows; export contains M of those ids.
    db_duplicate_count == N ∩ M (count of export ids that already exist in DB)."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    # Pre-populate 3 existing messages
    existing_ids = [_rand_msg_id(), _rand_msg_id(), _rand_msg_id()]
    for mid in existing_ids:
        await _create_chat_message(db_session, chat_id, mid, user_id)

    # Export contains 2 of those + 1 new
    new_id = _rand_msg_id()
    export_ids = [existing_ids[0], existing_ids[1], new_id]
    messages = [_make_message(mid) for mid in export_ids]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.db_duplicate_count == 2
    assert sorted(report.db_duplicate_export_msg_ids) == sorted(existing_ids[:2])


async def test_db_duplicate_count_zero_when_no_overlap(db_session, tmp_path: Path) -> None:
    """Export with message ids that don't exist in DB → db_duplicate_count == 0."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    export_ids = [_rand_msg_id(), _rand_msg_id()]
    messages = [_make_message(mid) for mid in export_ids]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.db_duplicate_count == 0
    assert report.db_duplicate_export_msg_ids == []


async def test_db_duplicate_does_not_cross_chat_boundary(db_session, tmp_path: Path) -> None:
    """A message id in chat A must NOT be counted as duplicate when querying chat B."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_a = _rand_chat_id()
    chat_b = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    shared_msg_id = _rand_msg_id()
    # Insert the message in chat A
    await _create_chat_message(db_session, chat_a, shared_msg_id, user_id)

    # Export for chat B contains the same message id
    export = _make_export(chat_b, [_make_message(shared_msg_id)])
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_b)

    assert report.db_duplicate_count == 0


# ---------------------------------------------------------------------------
# 3. DB broken reply chain
# ---------------------------------------------------------------------------

async def test_db_broken_reply_count_when_target_missing(db_session, tmp_path: Path) -> None:
    """Export has a reply whose target is not in DB → db_broken_reply_count == 1."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    source_msg_id = _rand_msg_id()
    missing_target_id = _rand_msg_id()  # NOT in DB

    messages = [
        _make_message(source_msg_id, reply_to=missing_target_id),
    ]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.db_broken_reply_count == 1


async def test_db_broken_reply_count_zero_when_target_exists(db_session, tmp_path: Path) -> None:
    """Export has a reply whose target IS in DB (live row) → db_broken_reply_count == 0."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    target_id = _rand_msg_id()
    source_id = _rand_msg_id()

    # Pre-populate the reply target in the DB (as a legacy/live row with raw_update_id=None)
    await _create_chat_message(db_session, chat_id, target_id, user_id)

    messages = [
        _make_message(target_id),
        _make_message(source_id, reply_to=target_id),
    ]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.db_broken_reply_count == 0


async def test_db_broken_reply_count_no_replies(db_session, tmp_path: Path) -> None:
    """Export with no replies → db_broken_reply_count == 0."""
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()
    messages = [_make_message(_rand_msg_id()), _make_message(_rand_msg_id())]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    assert report.db_broken_reply_count == 0


# ---------------------------------------------------------------------------
# 4. CLI --with-db prints operator summary
# ---------------------------------------------------------------------------

async def test_cli_with_db_prints_operator_summary(db_session, tmp_path: Path) -> None:
    """import_dry_run --with-db <path> prints the EXACT operator summary line.

    Verifies the issue #99 spec verbatim:
    "N duplicates would be skipped, M offrecord messages, K nomem, J broken reply chains."

    The DB session is mocked so no real connection is needed — we patch
    bot.db.engine.async_session to yield the test db_session.
    """
    import io
    import sys
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from bot.cli import main

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()
    await _ensure_user(db_session, user_id)

    # 1 existing message (1 duplicate)
    existing_id = _rand_msg_id()
    await _create_chat_message(db_session, chat_id, existing_id, user_id)

    # Export: existing_id (duplicate) + new_id (no duplicate) + reply to missing
    # missing_target is NOT in the export, so it IS a broken reply chain.
    new_id = _rand_msg_id()
    missing_target = _rand_msg_id()
    reply_id = _rand_msg_id()

    export = _make_export(chat_id, [
        _make_message(existing_id),
        _make_message(new_id),
        _make_message(reply_id, reply_to=missing_target),
    ])
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    @asynccontextmanager
    async def _fake_session_ctx():
        yield db_session

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        with patch("bot.db.engine.async_session", return_value=_fake_session_ctx()):
            rc = main(["import_dry_run", "--with-db", str(f)])
    finally:
        sys.stdout = old_stdout

    output = buf.getvalue().strip()
    assert rc == 0, f"Expected exit 0, got {rc}. Output: {output!r}"
    # Exact format per issue #99 spec:
    # "N duplicates would be skipped, M offrecord messages, K nomem, J broken reply chains."
    expected = "1 duplicates would be skipped, 0 offrecord messages, 0 nomem, 1 broken reply chains."
    assert output == expected, f"Expected exact summary line:\n  {expected!r}\nGot:\n  {output!r}"


# ---------------------------------------------------------------------------
# Fix 1: intra-export reply targets are NOT counted as broken
# ---------------------------------------------------------------------------

async def test_db_broken_reply_count_excludes_in_export_targets(db_session, tmp_path: Path) -> None:
    """Reply target present in same export but absent from DB is NOT counted as broken.

    When apply runs, msg-B (the target) will be created first, so the chain is
    not truly broken. Only targets that are truly missing from both the export
    and the DB count as broken.
    """
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()

    # msg-A replies to msg-B; msg-B is also in the export. Neither in DB.
    msg_a_id = _rand_msg_id()
    msg_b_id = _rand_msg_id()

    messages = [
        _make_message(msg_b_id),                       # target — in export, NOT in DB
        _make_message(msg_a_id, reply_to=msg_b_id),    # source — reply to msg_b
    ]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    # msg_b is in the export → apply will create it → NOT a broken reply chain.
    assert report.db_broken_reply_count == 0, (
        f"Expected 0 broken replies (target in export), got {report.db_broken_reply_count}"
    )


# ---------------------------------------------------------------------------
# Fix 2: multiplicity — count MESSAGES (not unique target ids)
# ---------------------------------------------------------------------------

async def test_db_broken_reply_count_counts_messages_not_unique_targets(db_session, tmp_path: Path) -> None:
    """Two messages that both reply to the same missing target → db_broken_reply_count == 2.

    The resolver deduplicates by target_id internally, but the count must be per-message
    (option A semantics per issue #99 spec). Two broken messages → 2.
    """
    from bot.services.import_dry_run import parse_export_with_db

    chat_id = _rand_chat_id()

    # One missing target (NOT in DB, NOT in export), two messages reply to it.
    missing_target_id = _rand_msg_id()
    msg_x_id = _rand_msg_id()
    msg_y_id = _rand_msg_id()

    messages = [
        _make_message(msg_x_id, reply_to=missing_target_id),
        _make_message(msg_y_id, reply_to=missing_target_id),
    ]
    export = _make_export(chat_id, messages)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(export), encoding="utf-8")

    report = await parse_export_with_db(f, db_session, chat_id)

    # Two messages have broken replies (same missing target but two messages).
    assert report.db_broken_reply_count == 2, (
        f"Expected 2 broken replies (two messages, same missing target), got {report.db_broken_reply_count}"
    )
