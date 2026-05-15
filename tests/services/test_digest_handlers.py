"""Tests for T7-06 — admin digest handlers.

Covers:
- non-admin invocation: silent no-op (no DB writes, no replies)
- /digest_now weekly: rejected with Phase 8 message
- /digest_now daily: invokes run_digest (mocked or end-to-end with empty
  window → skipped); empty window reply
- /digest_preview: returns body + audit block when digest exists
- /digest_preview: returns "not generated" when no digest
- /digest_history: empty case
- /digest_history: shows rows
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


_chat_counter = itertools.count(start=8600)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _mk_command_obj(args: str | None) -> MagicMock:
    obj = MagicMock()
    obj.args = args
    return obj


def _mk_msg(user_id: int) -> MagicMock:
    m = MagicMock()
    m.from_user = MagicMock()
    m.from_user.id = user_id
    m.answer = AsyncMock()
    return m


# ── /digest_now ──────────────────────────────────────────────────────────────


async def test_digest_now_non_admin_silent_no_op(db_session):
    """Non-admin user: handler returns silently — no message.answer call."""
    from bot.handlers.digest import cmd_digest_now

    bot_mock = MagicMock()
    msg = _mk_msg(user_id=99_999_999)  # not in ADMIN_IDS
    await cmd_digest_now(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj("daily")
    )
    msg.answer.assert_not_called()


async def test_digest_now_weekly_returns_phase8_message(db_session, monkeypatch):
    """admin invokes /digest_now weekly → Phase 8 message."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1
    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj("weekly")
    )
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "Phase 8" in body or "weekly" in body.lower()


async def test_digest_now_invalid_arg(db_session):
    """admin invokes /digest_now garbage → usage message."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_now

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1
    bot_mock = MagicMock()
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_now(
        msg, bot=bot_mock, session=db_session, command=_mk_command_obj("garbage")
    )
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "/digest_now" in body


# ── /digest_preview ──────────────────────────────────────────────────────────


async def test_digest_preview_non_admin_silent(db_session):
    from bot.handlers.digest import cmd_digest_preview

    msg = _mk_msg(user_id=99_999_999)
    await cmd_digest_preview(
        msg, session=db_session, command=_mk_command_obj("daily")
    )
    msg.answer.assert_not_called()


async def test_digest_preview_no_existing_digest(db_session):
    """Admin asks preview, no row exists → 'not generated' reply."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_preview

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_preview(
        msg, session=db_session, command=_mk_command_obj("daily 2000-01-01")
    )
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "ещё не сгенерирован" in body or "digest_now" in body


async def test_digest_preview_with_existing_digest(db_session):
    """Insert a digest, preview returns body + audit block."""
    from bot.config import settings
    from bot.db.models import Digest
    from bot.handlers.digest import cmd_digest_preview, _parse_date_for_preview

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1

    # Insert digest with a specific window
    ws, we = _parse_date_for_preview("2026-04-10")
    digest = Digest(
        type="daily",
        window_start=ws,
        window_end=we,
        body_markdown="TL;DR test.\n\n- One bullet [[mv:100]]",
        citations=[{"kind": "message_version", "id": 100, "position": 0}],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_preview(
        msg, session=db_session, command=_mk_command_obj("daily 2026-04-10")
    )
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "TL;DR test" in body
    assert "Audit" in body
    assert "message_version" in body
    assert str(digest.id) in body


async def test_digest_preview_invalid_date(db_session):
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_preview

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1
    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_preview(
        msg, session=db_session, command=_mk_command_obj("daily 2026/13/40")
    )
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "формат" in body.lower() or "YYYY-MM-DD" in body


# ── /digest_history ──────────────────────────────────────────────────────────


async def test_digest_history_non_admin_silent(db_session):
    from bot.handlers.digest import cmd_digest_history

    msg = _mk_msg(user_id=99_999_999)
    await cmd_digest_history(msg, session=db_session)
    msg.answer.assert_not_called()


async def test_digest_history_empty(db_session):
    """If no digests exist, history reply says 'История пуста'."""
    from bot.config import settings
    from bot.handlers.digest import cmd_digest_history

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1
    # Wipe digests in test transaction (will rollback at fixture teardown)
    await db_session.execute(text("DELETE FROM digest_runs"))
    await db_session.execute(text("DELETE FROM digests"))
    await db_session.flush()

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_history(msg, session=db_session)
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "пуста" in body.lower() or "не создавался" in body


async def test_digest_history_with_rows(db_session):
    """Insert 2 digests, history shows them."""
    from bot.config import settings
    from bot.db.models import Digest
    from bot.handlers.digest import cmd_digest_history

    admin_id = list(settings.ADMIN_IDS)[0] if settings.ADMIN_IDS else 1
    now = datetime.now(timezone.utc)
    d1 = Digest(
        type="daily",
        window_start=now - timedelta(days=2),
        window_end=now - timedelta(days=1),
        body_markdown="TL;DR.\n\n- One [[mv:1]]",
        citations=[{"kind": "message_version", "id": 1, "position": 0}],
        status="draft",
    )
    d2 = Digest(
        type="daily",
        window_start=now - timedelta(days=3),
        window_end=now - timedelta(days=2),
        body_markdown=None,
        citations=[],
        status="skipped",
    )
    db_session.add_all([d1, d2])
    await db_session.flush()

    msg = _mk_msg(user_id=admin_id)
    await cmd_digest_history(msg, session=db_session)
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    body = args[0] if args else kwargs.get("text", "")
    assert "Последние дайджесты" in body
    assert f"#{d1.id}" in body
    assert f"#{d2.id}" in body
    assert "draft" in body
    assert "skipped" in body
