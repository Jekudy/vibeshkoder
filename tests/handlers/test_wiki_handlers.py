"""T9-06 — admin wiki handler tests.

10 scenarios covering /wiki_publish, /wiki_unpublish, /wiki_robots.

All tests use db_session (rollback fixture) for DB isolation — no commits.
Handler functions are imported via import_module to honour the app_env reset
pattern used by the rest of the handler test suite.

Admin user ID: 149820031 (matches app_env ADMIN_IDS setting).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

# Admin ID must match app_env fixture ADMIN_IDS = "[149820031]".
_ADMIN_ID = 149820031
_NON_ADMIN_ID = 9_999_999


# ── helpers ───────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _message(*, user_id: int = _ADMIN_ID, text: str = "/wiki_publish test-slug") -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _command(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


async def _make_user(session, *, user_id: int | None = None) -> int:
    from sqlalchemy import text

    uid = user_id if user_id is not None else int(uuid.uuid4().int & 0x7FFFFFFF)
    # Use ON CONFLICT DO NOTHING so calling this multiple times for the same user is safe.
    await session.execute(
        text(
            "INSERT INTO users (id, username, first_name, is_member, is_admin, created_at, updated_at) "
            "VALUES (:id, :u, 'Test', true, false, now(), now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": uid, "u": f"u{uid}"},
    )
    return uid


async def _ensure_admin_user(session) -> None:
    """Insert the admin user (id=149820031) into users so FK constraints pass."""
    await _make_user(session, user_id=_ADMIN_ID)


async def _make_wiki_page(
    session,
    *,
    slug: str | None = None,
    page_status: str = "draft",
    public_enabled: bool = False,
    robots_policy: str = "noindex",
) -> tuple[uuid.UUID, str]:
    from sqlalchemy import text

    page_id = uuid.uuid4()
    slug = slug or f"test-{uuid.uuid4().hex[:8]}"
    created_by = await _make_user(session)

    await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(:id, :slug, :title, '', :ps, :pe, :rp, :cb, now(), now())"
        ),
        {
            "id": str(page_id),
            "slug": slug,
            "title": "Test Page",
            "ps": page_status,
            "pe": public_enabled,
            "rp": robots_policy,
            "cb": created_by,
        },
    )
    await session.flush()
    return page_id, slug


async def _make_chat_message(session, *, user_id: int) -> object:
    from bot.db.models import ChatMessage

    cm = ChatMessage(
        message_id=int(uuid.uuid4().int & 0x7FFFFFFF),
        chat_id=-1001234567890,
        user_id=user_id,
        text="test",
        date=_now(),
        raw_json={"text": "test"},
        memory_policy="normal",
        is_redacted=False,
    )
    session.add(cm)
    await session.flush()
    return cm


async def _make_message_version(session, *, chat_message_id: int) -> int:
    from bot.db.models import MessageVersion

    mv = MessageVersion(
        chat_message_id=chat_message_id,
        version_seq=1,
        text="test",
        normalized_text="test",
        entities_json={},
        content_hash=f"h-{uuid.uuid4().hex[:16]}",
        is_redacted=False,
    )
    session.add(mv)
    await session.flush()
    return mv.id


async def _make_knowledge_card(session, *, admin_uid: int) -> uuid.UUID:
    from bot.db.models import KnowledgeCard

    card = KnowledgeCard(
        title="Card",
        body_markdown="body",
        card_status="approved",
        approved_by_user_id=admin_uid,
        approved_at=_now(),
    )
    session.add(card)
    await session.flush()
    return card.id


async def _link_card(session, *, page_id: uuid.UUID, card_id: uuid.UUID) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
            "VALUES (:pid, :cid, 0)"
        ),
        {"pid": str(page_id), "cid": str(card_id)},
    )


async def _link_mv(session, *, page_id: uuid.UUID, mv_id: int) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position) "
            "VALUES (:pid, :mvid, 0)"
        ),
        {"pid": str(page_id), "mvid": mv_id},
    )


async def _pub_log_count(session, *, page_id: uuid.UUID) -> int:
    from sqlalchemy import text

    row = (
        await session.execute(
            text("SELECT count(*) FROM wiki_publication_log WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
    ).scalar()
    return int(row or 0)


async def _get_page(session, *, page_id: uuid.UUID) -> object:
    from sqlalchemy import text

    return (
        await session.execute(
            text("SELECT public_enabled, robots_policy, page_status FROM wiki_pages WHERE id = :pid"),
            {"pid": str(page_id)},
        )
    ).mappings().one()


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_non_admin_publish_refused(db_session) -> None:
    """R6.a — non-admin /wiki_publish returns refusal with no DB write."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")

    msg = _message(user_id=_NON_ADMIN_ID, text=f"/wiki_publish {slug}")
    cmd = _command(slug)

    await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert "администратор" in replied.lower() or "admin" in replied.lower()

    # No DB write
    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


async def test_publish_draft_returns_review_message(db_session) -> None:
    """R6.b — admin cannot publish a page with page_status='draft'."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="draft")

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_publish {slug}")
    cmd = _command(slug)

    # Mock feature flag ON
    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert "ревью" in replied.lower() or "review" in replied.lower()

    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


async def test_publish_empty_sources_returns_no_sources(db_session) -> None:
    """R6.c precondition — page has zero sources → 'Нет источников.' response."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")
    # No card sources or message sources linked — zero sources

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_publish {slug}")
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert "источник" in replied.lower() or "source" in replied.lower()

    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


async def test_publish_failed_governance_returns_summary(db_session) -> None:
    """R6.c — page with an offrecord source fails governance → summary reply, no write."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")

    # Create a message version with offrecord memory_policy
    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    # Patch memory_policy to offrecord directly via SQL
    from sqlalchemy import text as _text
    await db_session.execute(
        _text("UPDATE chat_messages SET memory_policy = 'offrecord' WHERE id = :cid"),
        {"cid": cm.id},
    )
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    await _link_mv(db_session, page_id=page_id, mv_id=mv_id)

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_publish {slug}")
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    # Should mention invalid source(s) in some form
    assert replied  # non-empty response

    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


async def test_publish_success_inserts_log_and_flips_pe(db_session) -> None:
    """Positive publish path: public_enabled=true + exactly 1 wiki_publication_log row."""
    handler = import_module("bot.handlers.wiki")

    await _ensure_admin_user(db_session)
    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")

    # Add a clean source (normal memory_policy, not redacted)
    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    await _link_mv(db_session, page_id=page_id, mv_id=mv_id)

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_publish {slug}")
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_publish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    # Should confirm publication
    assert slug in replied or "публик" in replied.lower()

    assert await _pub_log_count(db_session, page_id=page_id) == 1
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is True


async def test_publish_pe_log_atomicity(db_session) -> None:
    """UPDATE and INSERT must be in one transaction — simulate INSERT failure rollback."""
    handler = import_module("bot.handlers.wiki")

    await _ensure_admin_user(db_session)
    page_id, slug = await _make_wiki_page(db_session, page_status="reviewed")
    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    await _link_mv(db_session, page_id=page_id, mv_id=mv_id)

    # Patch session.execute so the INSERT into wiki_publication_log raises an error
    # We wrap: first call (SELECT FOR UPDATE) succeeds, UPDATE succeeds,
    # INSERT fails → everything rolls back.
    original_execute = db_session.execute

    async def _patched_execute(stmt, params=None, **kwargs):
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        # Raise on the INSERT INTO wiki_publication_log call
        if "wiki_publication_log" in stmt_str and "INSERT" in stmt_str.upper():
            raise RuntimeError("simulated INSERT failure")
        if params is not None:
            return await original_execute(stmt, params, **kwargs)
        return await original_execute(stmt, **kwargs)

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_publish {slug}")
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        with patch.object(db_session, "execute", side_effect=_patched_execute):
            await handler.cmd_wiki_publish(msg, db_session, cmd)

    # After rollback: public_enabled must still be False and no log row
    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False


async def test_unpublish_sets_noindex_and_logs_unpublish(db_session) -> None:
    """Unpublish sets public_enabled=false, robots_policy=noindex, action='unpublish'."""
    handler = import_module("bot.handlers.wiki")

    await _ensure_admin_user(db_session)
    # Start as a published page with robots_policy='index'
    page_id, slug = await _make_wiki_page(
        db_session, page_status="reviewed", public_enabled=True, robots_policy="index"
    )

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_unpublish {slug}")
    cmd = _command(slug)

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_unpublish(msg, db_session, cmd)

    msg.answer.assert_awaited_once()

    page = await _get_page(db_session, page_id=page_id)
    assert page["public_enabled"] is False
    assert page["robots_policy"] == "noindex"

    assert await _pub_log_count(db_session, page_id=page_id) == 1

    from sqlalchemy import text as _text
    log = (
        await db_session.execute(
            _text("SELECT action FROM wiki_publication_log WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
    ).mappings().one()
    assert log["action"] == "unpublish"


async def test_wiki_robots_index_refused_when_not_public(db_session) -> None:
    """R6.d — cannot set robots_policy='index' when public_enabled=false."""
    handler = import_module("bot.handlers.wiki")

    page_id, slug = await _make_wiki_page(
        db_session, page_status="reviewed", public_enabled=False, robots_policy="noindex"
    )

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_robots {slug} index")
    cmd = _command(f"{slug} index")

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_robots(msg, db_session, cmd)

    msg.answer.assert_awaited_once()
    replied = msg.answer.call_args[0][0]
    assert "непубли" in replied.lower() or "public" in replied.lower() or "нельзя" in replied.lower()

    assert await _pub_log_count(db_session, page_id=page_id) == 0
    page = await _get_page(db_session, page_id=page_id)
    assert page["robots_policy"] == "noindex"


async def test_wiki_robots_index_success_inserts_log(db_session) -> None:
    """Positive path: /wiki_robots <slug> index on a public page sets robots_policy='index'."""
    handler = import_module("bot.handlers.wiki")

    await _ensure_admin_user(db_session)
    page_id, slug = await _make_wiki_page(
        db_session, page_status="reviewed", public_enabled=True, robots_policy="noindex"
    )

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_robots {slug} index")
    cmd = _command(f"{slug} index")

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_robots(msg, db_session, cmd)

    msg.answer.assert_awaited_once()

    assert await _pub_log_count(db_session, page_id=page_id) == 1
    page = await _get_page(db_session, page_id=page_id)
    assert page["robots_policy"] == "index"

    from sqlalchemy import text as _text
    log = (
        await db_session.execute(
            _text("SELECT action FROM wiki_publication_log WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
    ).mappings().one()
    assert log["action"] == "robots_index"


async def test_wiki_robots_noindex_always_succeeds(db_session) -> None:
    """robots_policy='noindex' always succeeds regardless of public_enabled."""
    handler = import_module("bot.handlers.wiki")

    await _ensure_admin_user(db_session)
    page_id, slug = await _make_wiki_page(
        db_session, page_status="draft", public_enabled=False, robots_policy="noindex"
    )

    msg = _message(user_id=_ADMIN_ID, text=f"/wiki_robots {slug} noindex")
    cmd = _command(f"{slug} noindex")

    with patch.object(handler.FeatureFlagRepo, "get", AsyncMock(return_value=True)):
        await handler.cmd_wiki_robots(msg, db_session, cmd)

    msg.answer.assert_awaited_once()

    assert await _pub_log_count(db_session, page_id=page_id) == 1
    page = await _get_page(db_session, page_id=page_id)
    assert page["robots_policy"] == "noindex"

    from sqlalchemy import text as _text
    log = (
        await db_session.execute(
            _text("SELECT action FROM wiki_publication_log WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
    ).mappings().one()
    assert log["action"] == "robots_noindex"
