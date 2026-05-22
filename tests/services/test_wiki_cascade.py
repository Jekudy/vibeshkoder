"""T9-07 — wiki cascade layer tests.

17 scenarios mapped to acceptance criteria from PHASE9_PLAN.md §T9-07.

Isolation: every test uses db_session (rollback fixture) — no commits.
All table inserts use raw SQL because no ORM classes exist for wiki tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ── helpers ────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(session) -> int:
    from bot.db.models import User

    uid = int(uuid.uuid4().int & 0x7FFFFFFF)  # positive int
    user = User(
        id=uid,
        username=f"u{uid}",
        first_name="Test",
        is_member=True,
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return uid


async def _make_chat_message(session, *, user_id: int, memory_policy: str = "normal"):
    from bot.db.models import ChatMessage

    cm = ChatMessage(
        message_id=int(uuid.uuid4().int & 0x7FFFFFFF),
        chat_id=-1001234567890,
        user_id=user_id,
        text="test content",
        date=_now(),
        raw_json={"text": "test content"},
        memory_policy=memory_policy,
        is_redacted=False,
    )
    session.add(cm)
    await session.flush()
    return cm


async def _make_message_version(
    session,
    *,
    chat_message_id: int,
    content_hash: str | None = None,
    is_redacted: bool = False,
) -> int:
    from bot.db.models import MessageVersion
    from sqlalchemy import text

    if content_hash is None:
        content_hash = f"h-{uuid.uuid4().hex[:16]}"

    mv = MessageVersion(
        chat_message_id=chat_message_id,
        version_seq=1,
        text="test content",
        normalized_text="test content",
        entities_json={},
        content_hash=content_hash,
        is_redacted=is_redacted,
    )
    session.add(mv)
    await session.flush()

    # Set current_version_id on the parent ChatMessage so _resolve_affected_mvids
    # can resolve this mvid for target_type='message' forgets.
    await session.execute(
        text("UPDATE chat_messages SET current_version_id = :mvid WHERE id = :cmid"),
        {"mvid": mv.id, "cmid": chat_message_id},
    )

    return mv.id


async def _make_knowledge_card(session, *, admin_user_id: int) -> uuid.UUID:
    from bot.db.models import KnowledgeCard

    card = KnowledgeCard(
        title="Test Card",
        body_markdown="test body",
        card_status="approved",
        approved_by_user_id=admin_user_id,
        approved_at=_now(),
    )
    session.add(card)
    await session.flush()
    return card.id


async def _make_card_source(session, *, card_id: uuid.UUID, message_version_id: int) -> None:
    from bot.db.models import CardSource

    cs = CardSource(
        card_id=card_id,
        message_version_id=message_version_id,
        position=0,
    )
    session.add(cs)
    await session.flush()


async def _make_wiki_page(
    session,
    *,
    created_by_user_id: int | None = None,
    page_status: str = "reviewed",
    slug: str | None = None,
) -> uuid.UUID:
    """Insert a minimal wiki_pages row and return its UUID id."""
    from sqlalchemy import text

    if created_by_user_id is None:
        created_by_user_id = await _make_user(session)
    if slug is None:
        slug = f"test-page-{uuid.uuid4().hex[:8]}"

    page_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(:id, :slug, :title, :body, :page_status, false, 'noindex', "
            " :created_by, now(), now())"
        ),
        {
            "id": str(page_id),
            "slug": slug,
            "title": "Test Page",
            "body": "body content",
            "page_status": page_status,
            "created_by": created_by_user_id,
        },
    )
    return page_id


async def _link_mv(
    session, *, page_id: uuid.UUID, message_version_id: int, position: int = 0
) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position) "
            "VALUES (:page_id, :mvid, :pos)"
        ),
        {"page_id": str(page_id), "mvid": message_version_id, "pos": position},
    )


async def _link_card(session, *, page_id: uuid.UUID, card_id: uuid.UUID, position: int = 0) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
            "VALUES (:page_id, :card_id, :pos)"
        ),
        {"page_id": str(page_id), "card_id": str(card_id), "pos": position},
    )


async def _make_forget_event_row(
    session,
    *,
    tombstone_key: str,
    target_type: str = "message",
    target_id: str | None = None,
    status: str = "pending",
) -> int:
    """Insert a forget_event row and return its id."""
    from sqlalchemy import text

    result = await session.execute(
        text(
            "INSERT INTO forget_events "
            "(target_type, target_id, authorized_by, tombstone_key, status, policy, created_at, updated_at) "
            "VALUES (:tt, :tid, 'admin', :tk, :st, 'forgotten', now(), now()) "
            "RETURNING id"
        ),
        {
            "tt": target_type,
            "tid": target_id,
            "tk": tombstone_key,
            "st": status,
        },
    )
    return result.scalar_one()


async def _get_page_status(session, page_id: uuid.UUID) -> dict:
    from sqlalchemy import text

    row = (
        await session.execute(
            text("SELECT page_status, public_enabled FROM wiki_pages WHERE id = :id"),
            {"id": str(page_id)},
        )
    ).mappings().one()
    return dict(row)


async def _get_revision_count(session, page_id: uuid.UUID) -> int:
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT count(*) FROM wiki_revisions WHERE wiki_page_id = :id"),
        {"id": str(page_id)},
    )
    return result.scalar_one()


async def _get_revisions(session, page_id: uuid.UUID) -> list[dict]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT body_markdown, revision_status, edit_reason, "
                "redacted_by_forget_event_id, redacted_at, revision_sources_resolved_at "
                "FROM wiki_revisions WHERE wiki_page_id = :id ORDER BY created_at"
            ),
            {"id": str(page_id)},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


class _FakeEvent:
    """Minimal fake forget_event object for unit testing cascade functions."""

    def __init__(
        self,
        *,
        id: int,
        target_type: str,
        target_id: str | None,
        tombstone_key: str,
    ):
        self.id = id
        self.target_type = target_type
        self.target_id = target_id
        self.tombstone_key = tombstone_key


# ── AC#1: _cascade_wiki_pages present in _LAYER_FUNCS ─────────────────────────


def test_cascade_wiki_pages_in_layer_funcs() -> None:
    """AC#1: _cascade_wiki_pages is present in _LAYER_FUNCS dict."""
    from bot.services.forget_cascade import _LAYER_FUNCS

    assert "wiki_pages" in _LAYER_FUNCS


# ── AC#2: _cascade_wiki_revisions present in _LAYER_FUNCS ────────────────────


def test_cascade_wiki_revisions_in_layer_funcs() -> None:
    """AC#2: _cascade_wiki_revisions is present in _LAYER_FUNCS dict."""
    from bot.services.forget_cascade import _LAYER_FUNCS

    assert "wiki_revisions" in _LAYER_FUNCS


# ── AC#3: wiki_pages ORDER: after digests, before wiki_revisions ─────────────


def test_cascade_layer_order_wiki_pages_position() -> None:
    """AC#3: wiki_pages appears in CASCADE_LAYER_ORDER after digests and before wiki_revisions."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    order = list(CASCADE_LAYER_ORDER)
    assert "wiki_pages" in order
    assert order.index("wiki_pages") > order.index("digests")
    assert order.index("wiki_pages") < order.index("wiki_revisions")


# ── AC#4: wiki_revisions ORDER: after wiki_pages, before card_sources ─────────


def test_cascade_layer_order_wiki_revisions_position() -> None:
    """AC#4: wiki_revisions appears after wiki_pages and before card_sources."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    order = list(CASCADE_LAYER_ORDER)
    assert "wiki_revisions" in order
    assert order.index("wiki_revisions") > order.index("wiki_pages")
    assert order.index("wiki_revisions") < order.index("card_sources")


# ── AC#5: forget on mv → page transitions to stale + public_enabled=false ────


async def test_cascade_wiki_pages_stale_partial_forget(db_session) -> None:
    """AC#5: forget event on an mv with remaining valid sources → page_status='stale', public_enabled=false."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv1_id = await _make_message_version(db_session, chat_message_id=cm.id)

    # Create a second mv for the same page so there are remaining valid sources
    cm2 = await _make_chat_message(db_session, user_id=uid)
    mv2_id = await _make_message_version(db_session, chat_message_id=cm2.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv1_id, position=0)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv2_id, position=1)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count == 1
    row = await _get_page_status(db_session, page_id)
    assert row["page_status"] == "stale"
    assert row["public_enabled"] is False


# ── AC#6: all sources forgotten → page archived + public_enabled=false ────────


async def test_cascade_wiki_pages_archived_all_sources_forgotten(db_session) -> None:
    """AC#6: all mvid sources forgotten → page_status='archived', public_enabled=false."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id, is_redacted=True)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id, position=0)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count == 1
    row = await _get_page_status(db_session, page_id)
    assert row["page_status"] == "archived"
    assert row["public_enabled"] is False


# ── AC#7: card_id in wiki_page_card_sources triggers cascade (I7b) ─────────


async def test_cascade_wiki_pages_card_source_triggers_cascade(db_session) -> None:
    """AC#7: forget on mv that flows through card_sources → wiki page transitions."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid)
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count >= 1
    row = await _get_page_status(db_session, page_id)
    # page should be stale or archived — either way public_enabled=false
    assert row["public_enabled"] is False
    assert row["page_status"] in ("stale", "archived")


# ── AC#8: L9d — message_hash tombstone triggers cascade ──────────────────────


async def test_cascade_wiki_pages_message_hash_tombstone(db_session) -> None:
    """AC#8/L9d: message_hash tombstone matching cited mvid triggers wiki page cascade."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    content_hash = f"hash-{uuid.uuid4().hex[:8]}"
    mv_id = await _make_message_version(
        db_session, chat_message_id=cm.id, content_hash=content_hash
    )

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message_hash:{content_hash}",
        target_type="message_hash",
        target_id=content_hash,
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message_hash",
        target_id=content_hash,
        tombstone_key=f"message_hash:{content_hash}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count >= 1
    row = await _get_page_status(db_session, page_id)
    assert row["public_enabled"] is False


# ── AC#9: L9e — user tombstone triggers cascade ────────────────────────────────


async def test_cascade_wiki_pages_user_tombstone(db_session) -> None:
    """AC#9/L9e: user tombstone matching cited mvid's author triggers wiki page cascade."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"user:{uid}",
        target_type="user",
        target_id=str(uid),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="user",
        target_id=str(uid),
        tombstone_key=f"user:{uid}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count >= 1
    row = await _get_page_status(db_session, page_id)
    assert row["public_enabled"] is False


# ── AC#10: _cascade_wiki_pages writes wiki_revisions with edit_reason='forget_cascade' ──


async def test_cascade_wiki_pages_writes_revision_row(db_session) -> None:
    """AC#10: _cascade_wiki_pages writes a wiki_revisions row with edit_reason='forget_cascade'."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count == 1
    revisions = await _get_revisions(db_session, page_id)
    assert len(revisions) == 1
    assert revisions[0]["edit_reason"] == "forget_cascade"


# ── AC#10b: cascade audit revision is pre-masked at INSERT (Codex CRITICAL #1) ──


async def test_cascade_wiki_pages_audit_revision_pre_masked(db_session) -> None:
    """AC#10b (Codex CRITICAL #1): the audit wiki_revisions row inserted by
    _cascade_wiki_pages must already have:
    - body_markdown == '[CONTENT_REDACTED: forget_event_id={n}]'
    - revision_status == 'forgotten_redacted'
    - redacted_at IS NOT NULL
    - redacted_by_forget_event_id == event.id

    This prevents future queries of the audit log from exposing the forgotten text
    via the cascade-created row, which has empty source snapshots and is never
    touched by _cascade_wiki_revisions's overlap filter.
    """
    from bot.services.forget_cascade import _cascade_wiki_pages
    from sqlalchemy import text

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count == 1

    # The cascade-created revision must already be masked — NOT 'active' with original body.
    row = (
        await db_session.execute(
            text(
                "SELECT body_markdown, revision_status, redacted_at, "
                "redacted_by_forget_event_id "
                "FROM wiki_revisions "
                "WHERE wiki_page_id = CAST(:pid AS uuid) "
                "  AND edit_reason = 'forget_cascade'"
            ),
            {"pid": str(page_id)},
        )
    ).mappings().one()

    expected_mask = f"[CONTENT_REDACTED: forget_event_id={event_id}]"
    assert row["body_markdown"] == expected_mask, (
        f"Expected masked body, got: {row['body_markdown']!r}"
    )
    assert row["revision_status"] == "forgotten_redacted", (
        f"Expected 'forgotten_redacted', got: {row['revision_status']!r}"
    )
    assert row["redacted_at"] is not None, "redacted_at must be set at INSERT"
    assert row["redacted_by_forget_event_id"] == event_id, (
        f"Expected event_id={event_id}, got: {row['redacted_by_forget_event_id']!r}"
    )


# ── AC#11: _cascade_wiki_revisions mask format ────────────────────────────────


async def test_cascade_wiki_revisions_mask_format(db_session) -> None:
    """AC#11/I7e sub-AC A: _cascade_wiki_revisions masks body_markdown correctly."""
    from bot.services.forget_cascade import _cascade_wiki_revisions

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    # Insert a wiki_revisions row with the mv in snapshot
    from sqlalchemy import text

    rev_id = uuid.uuid4()
    await session_execute_insert_revision(
        db_session,
        rev_id=rev_id,
        page_id=page_id,
        mv_ids=[mv_id],
    )

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_revisions(db_session, event)

    assert count == 1
    # Check mask format
    row = (
        await db_session.execute(
            text("SELECT body_markdown FROM wiki_revisions WHERE id = :id"),
            {"id": str(rev_id)},
        )
    ).scalar_one()
    assert row == f"[CONTENT_REDACTED: forget_event_id={event_id}]"


# ── AC#12: _cascade_wiki_revisions metadata fields ────────────────────────────


async def test_cascade_wiki_revisions_metadata_fields(db_session) -> None:
    """AC#12/I7e sub-AC B: _cascade_wiki_revisions sets all metadata fields correctly."""
    from bot.services.forget_cascade import _cascade_wiki_revisions
    from sqlalchemy import text

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    rev_id = uuid.uuid4()
    await session_execute_insert_revision(
        db_session,
        rev_id=rev_id,
        page_id=page_id,
        mv_ids=[mv_id],
    )

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    await _cascade_wiki_revisions(db_session, event)

    row = (
        await db_session.execute(
            text(
                "SELECT revision_status, redacted_by_forget_event_id, "
                "redacted_at, revision_sources_resolved_at "
                "FROM wiki_revisions WHERE id = :id"
            ),
            {"id": str(rev_id)},
        )
    ).mappings().one()

    assert row["revision_status"] == "forgotten_redacted"
    assert row["redacted_by_forget_event_id"] == event_id
    assert row["redacted_at"] is not None
    assert row["revision_sources_resolved_at"] is not None


# ── 9.5-C: _cascade_wiki_revisions idempotency guard ─────────────────────────


async def test_cascade_wiki_revisions_idempotent_second_run_is_noop(db_session) -> None:
    """9.5-C: Running _cascade_wiki_revisions twice for the same forget_event
    on an already-redacted revision must be a no-op on the second run.

    Contract:
    - First run: body_markdown is masked, revision_status='forgotten_redacted'.
    - Second run (same forget_event): row is SKIPPED (already_redacted_skip),
      body_markdown and redacted_by_forget_event_id are NOT overwritten.
    - Return value of second run: 0 rows modified.
    """
    from bot.services.forget_cascade import _cascade_wiki_revisions
    from sqlalchemy import text

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    rev_id = uuid.uuid4()
    await session_execute_insert_revision(
        db_session,
        rev_id=rev_id,
        page_id=page_id,
        mv_ids=[mv_id],
        body_markdown="original body before forget",
    )

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    # First run — should redact
    count1 = await _cascade_wiki_revisions(db_session, event)
    assert count1 == 1

    row_after_first = (
        await db_session.execute(
            text(
                "SELECT body_markdown, revision_status, redacted_by_forget_event_id "
                "FROM wiki_revisions WHERE id = :id"
            ),
            {"id": str(rev_id)},
        )
    ).mappings().one()
    assert row_after_first["revision_status"] == "forgotten_redacted"
    first_body = row_after_first["body_markdown"]
    assert first_body == f"[CONTENT_REDACTED: forget_event_id={event_id}]"

    # Second run — must be no-op (idempotency guard)
    count2 = await _cascade_wiki_revisions(db_session, event)
    assert count2 == 0, (
        f"Second cascade run returned {count2} rows modified; "
        "expected 0 — already-redacted revisions must be skipped"
    )

    row_after_second = (
        await db_session.execute(
            text(
                "SELECT body_markdown, revision_status, redacted_by_forget_event_id "
                "FROM wiki_revisions WHERE id = :id"
            ),
            {"id": str(rev_id)},
        )
    ).mappings().one()
    # Provenance must be unchanged
    assert row_after_second["revision_status"] == "forgotten_redacted"
    assert row_after_second["body_markdown"] == first_body
    assert row_after_second["redacted_by_forget_event_id"] == event_id


# ── AC#13: cascade order index assertions ─────────────────────────────────────


def test_cascade_layer_order_index_assertions() -> None:
    """AC#13: index() assertions per PHASE9_PLAN spec."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    order = list(CASCADE_LAYER_ORDER)
    assert order.index("wiki_pages") > order.index("digests")
    assert order.index("wiki_pages") < order.index("wiki_revisions")
    assert order.index("wiki_revisions") < order.index("card_sources")


# ── AC#14: _LAYER_APPLICABLE_TARGET_TYPES wiki_pages ─────────────────────────


def test_layer_applicable_target_types_wiki_pages() -> None:
    """AC#14: wiki_pages target_types == frozenset({'message','message_hash','user'})."""
    from bot.services.forget_cascade import _LAYER_APPLICABLE_TARGET_TYPES

    assert _LAYER_APPLICABLE_TARGET_TYPES["wiki_pages"] == frozenset(
        {"message", "message_hash", "user"}
    )


# ── AC#15: _LAYER_APPLICABLE_TARGET_TYPES wiki_revisions ─────────────────────


def test_layer_applicable_target_types_wiki_revisions() -> None:
    """AC#15: wiki_revisions target_types == frozenset({'message','message_hash','user'})."""
    from bot.services.forget_cascade import _LAYER_APPLICABLE_TARGET_TYPES

    assert _LAYER_APPLICABLE_TARGET_TYPES["wiki_revisions"] == frozenset(
        {"message", "message_hash", "user"}
    )


# ── AC#16: /wiki_publish advisory lock + re-check ─────────────────────────────


async def test_wiki_publish_advisory_lock_recheck(db_session) -> None:
    """AC#16: /wiki_publish acquires advisory lock and re-runs validate_sources in lock window.

    This test verifies the contract by checking that the handler correctly fails
    validation when sources become stale between initial check and the lock window.
    We test the advisory-lock path indirectly: if lock is acquired, validate_sources
    is re-run, and a stale source causes refusal.
    """
    # Import the function we want to test the contract of
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    # First validation: clean sources
    result1 = await validate_sources(db_session, page_id=page_id)
    assert result1.valid is True

    # Now mark mv as redacted (simulates forget completing between validation and publish)
    from sqlalchemy import text

    await db_session.execute(
        text("UPDATE message_versions SET is_redacted = true WHERE id = :id"),
        {"id": mv_id},
    )

    # Second validation (would be inside advisory lock window): stale sources
    result2 = await validate_sources(db_session, page_id=page_id)
    assert result2.valid is False

    # The advisory lock function itself is tested separately; here we verify the
    # re-check is meaningful and would catch the stale source.
    # Verify the handler imports the advisory lock helper (structural contract)
    import inspect
    import bot.handlers.wiki as wiki_module
    src = inspect.getsource(wiki_module)
    assert "acquire_advisory_lock" in src or "_p6_mvid_advisory_lock_id" in src or "pg_advisory" in src


# ── AC#17: bulk cascade — 50 pages, single event, count=50 ───────────────────


async def test_cascade_wiki_pages_bulk_50_pages(db_session) -> None:
    """AC#17/H3/I7f: single forget event invalidating 50 pages sharing the same mvid → count=50."""
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    # Seed 50 wiki pages all citing the same mvid
    page_ids = []
    for i in range(50):
        page_id = await _make_wiki_page(
            db_session,
            page_status="reviewed",
            slug=f"bulk-test-page-{i:03d}-{uuid.uuid4().hex[:6]}",
        )
        await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)
        page_ids.append(page_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count == 50

    # All pages must be stale/archived with public_enabled=false
    from sqlalchemy import text
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY as _PG_ARRAY
    from sqlalchemy.types import String as _StringType

    rows = (
        await db_session.execute(
            text(
                "SELECT page_status, public_enabled FROM wiki_pages "
                "WHERE id::text = ANY(:ids)"
            ).bindparams(
                bindparam("ids", type_=_PG_ARRAY(_StringType))
            ),
            {"ids": [str(p) for p in page_ids]},
        )
    ).mappings().all()

    assert len(rows) == 50
    for row in rows:
        assert row["public_enabled"] is False
        assert row["page_status"] in ("stale", "archived")


# ── helper for revision insertion ─────────────────────────────────────────────


async def session_execute_insert_revision(
    session,
    *,
    rev_id: uuid.UUID,
    page_id: uuid.UUID,
    mv_ids: list[int],
    body_markdown: str = "original body",
    revision_seq: int | None = None,
) -> None:
    """Insert a wiki_revisions row with the given mvid snapshot."""
    import json
    from sqlalchemy import text

    if revision_seq is None:
        # Auto-increment: count existing revisions for this page
        count_result = await session.execute(
            text("SELECT count(*) FROM wiki_revisions WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
        revision_seq = count_result.scalar_one() + 1

    await session.execute(
        text(
            "INSERT INTO wiki_revisions "
            "(id, wiki_page_id, revision_seq, body_markdown, revision_status, "
            " source_message_version_ids_snapshot, source_card_ids_snapshot, "
            " edited_at, created_at) "
            "VALUES "
            "(:id, :page_id, :seq, :body, 'active', "
            " CAST(:mv_ids AS jsonb), '[]'::jsonb, "
            " now(), now())"
        ),
        {
            "id": str(rev_id),
            "page_id": str(page_id),
            "seq": revision_seq,
            "body": body_markdown,
            "mv_ids": json.dumps(mv_ids),
        },
    )
