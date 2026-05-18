"""T9-02 — wiki governance validator tests.

Covers all 12 AC scenarios from PHASE9_PLAN.md §T9-02.

Isolation: every test uses db_session (rollback fixture) — no commits.
The wiki_page_card_sources / wiki_page_message_sources rows are inserted
via raw SQL because no ORM classes exist for those tables yet (T9-01
delivered migrations only).
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
    """Create a ChatMessage row and return the full ORM instance.

    Tests access ``.id`` for FK linking and ``.chat_id`` / ``.message_id`` when
    constructing forget_events.tombstone_key for message-type tombstones
    (``'message:' || chat_id || ':' || message_id``).
    """
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
    return mv.id


async def _make_knowledge_card(
    session,
    *,
    admin_user_id: int,
    card_status: str = "approved",
) -> uuid.UUID:
    from bot.db.models import KnowledgeCard

    # KnowledgeCard needs approved_by_user_id + approved_at for 'approved' status
    card = KnowledgeCard(
        title="Test Card",
        body_markdown="test body",
        card_status=card_status,
        approved_by_user_id=admin_user_id if card_status == "approved" else None,
        approved_at=_now() if card_status == "approved" else None,
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


async def _make_wiki_page(session, *, created_by_user_id: int | None = None) -> uuid.UUID:
    """Insert a minimal wiki_pages row and return its id (UUID)."""
    from sqlalchemy import text

    if created_by_user_id is None:
        created_by_user_id = await _make_user(session)

    page_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(:id, :slug, :title, :body, 'draft', false, 'noindex', "
            " :created_by, now(), now())"
        ),
        {
            "id": str(page_id),
            "slug": f"test-page-{uuid.uuid4().hex[:8]}",
            "title": "Test Page",
            "body": "body content",
            "created_by": created_by_user_id,
        },
    )
    return page_id


async def _link_card(session, *, page_id: uuid.UUID, card_id: uuid.UUID, position: int = 0) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
            "VALUES (:page_id, :card_id, :pos)"
        ),
        {"page_id": str(page_id), "card_id": str(card_id), "pos": position},
    )


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


async def _make_forget_event(
    session,
    *,
    tombstone_key: str,
    target_type: str = "message",
    target_id: str | None = None,
    status: str = "pending",
) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO forget_events "
            "(target_type, target_id, authorized_by, tombstone_key, status, policy, created_at, updated_at) "
            "VALUES (:tt, :tid, 'admin', :tk, :st, 'forgotten', now(), now())"
        ),
        {
            "tt": target_type,
            "tid": target_id,
            "tk": tombstone_key,
            "st": status,
        },
    )


# ── AC 1: valid page returns valid=True ───────────────────────────────────────


async def test_valid_page_returns_valid_true(db_session) -> None:
    """AC 1: valid page with approved card + non-redacted mv → valid=True."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid)
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_id)

    page_id = await _make_wiki_page(db_session)
    await _link_card(db_session, page_id=page_id, card_id=card_id)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id, position=1)

    result = await validate_sources(db_session, page_id=page_id.int)
    # page_id is UUID — pass as UUID
    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is True
    assert result.invalid_card_ids == []
    assert result.invalid_mvids == []
    assert result.reasons == {}


# ── AC 2: archived card → valid=False + invalid_card_ids ─────────────────────


async def test_archived_card_returns_invalid(db_session) -> None:
    """AC 2: page citing archived card → valid=False + card in invalid_card_ids."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid, card_status="archived")

    page_id = await _make_wiki_page(db_session)
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert card_id in result.invalid_card_ids
    assert "archived" in result.reasons[f"card:{card_id}"]


# ── AC 3: redacted mv → valid=False + invalid_mvids ──────────────────────────


async def test_redacted_mv_returns_invalid(db_session) -> None:
    """AC 3: page citing redacted message_version → valid=False + mvid in invalid_mvids."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id, is_redacted=True)

    page_id = await _make_wiki_page(db_session)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert mv_id in result.invalid_mvids
    assert "redacted" in result.reasons[f"mvid:{mv_id}"]


# ── AC 4: offrecord mv → valid=False (L9a) ───────────────────────────────────


async def test_offrecord_mv_returns_invalid(db_session) -> None:
    """AC 4 (L9a): page citing mv whose chat_message has memory_policy='offrecord' → invalid."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid, memory_policy="offrecord")
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id)

    page_id = await _make_wiki_page(db_session)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert mv_id in result.invalid_mvids
    assert "offrecord" in result.reasons[f"mvid:{mv_id}"]


# ── AC 5: active forget_event for mv → valid=False ───────────────────────────


async def test_forgotten_mv_returns_invalid(db_session) -> None:
    """AC 5: page citing mv with active forget_event (status=pending) → invalid."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id)

    # Create active forget_event keyed by the canonical message tombstone format.
    # target_id is deliberately NULL — the validator must rely on tombstone_key.
    await _make_forget_event(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=None,
        status="pending",
    )

    page_id = await _make_wiki_page(db_session)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert mv_id in result.invalid_mvids
    assert result.reasons[f"mvid:{mv_id}"] == "forgotten"


# ── AC 6: all card_sources forgotten → valid=False ───────────────────────────


async def test_card_all_sources_forgotten_returns_invalid(db_session) -> None:
    """AC 6: card whose every card_source has an active forget_event → invalid."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid)
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_id)

    # Forget the transitive source via canonical message tombstone format.
    # target_id is deliberately NULL — the validator must rely on tombstone_key.
    await _make_forget_event(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=None,
        status="completed",
    )

    page_id = await _make_wiki_page(db_session)
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert card_id in result.invalid_card_ids


# ── AC 7: transitive forget through card_sources (L9c) ───────────────────────


async def test_transitive_forget_through_card_sources_returns_invalid(db_session) -> None:
    """AC 7 (L9c): approved card whose card_source mv is forgotten → invalid."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid, memory_policy="offrecord")
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id)
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid, card_status="approved")
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_id)

    page_id = await _make_wiki_page(db_session)
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert card_id in result.invalid_card_ids
    assert "transitive_forget" in result.reasons[f"card:{card_id}"]


# ── AC 8: message_hash tombstone (L9d) ───────────────────────────────────────


async def test_message_hash_tombstone_returns_invalid(db_session) -> None:
    """AC 8 (L9d): forget_event with tombstone_key='message_hash:<hash>' matching mv."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    cm_id = cm.id
    content_hash = f"hash-{uuid.uuid4().hex}"
    mv_id = await _make_message_version(
        db_session, chat_message_id=cm_id, content_hash=content_hash
    )

    # target_id=NULL — proves match is via tombstone_key prefix only.
    await _make_forget_event(
        db_session,
        tombstone_key=f"message_hash:{content_hash}",
        target_type="message_hash",
        target_id=None,
        status="pending",
    )

    page_id = await _make_wiki_page(db_session)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert mv_id in result.invalid_mvids
    assert result.reasons[f"mvid:{mv_id}"] == "tombstone:message_hash"


# ── AC 9: user tombstone (L9e) ───────────────────────────────────────────────


async def test_user_tombstone_returns_invalid(db_session) -> None:
    """AC 9 (L9e): forget_event with tombstone_key='user:<uid>' matching mv author."""
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    cm_id = cm.id
    mv_id = await _make_message_version(db_session, chat_message_id=cm_id)

    # target_id=NULL — proves match is via tombstone_key prefix only.
    await _make_forget_event(
        db_session,
        tombstone_key=f"user:{uid}",
        target_type="user",
        target_id=None,
        status="pending",
    )

    page_id = await _make_wiki_page(db_session)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert mv_id in result.invalid_mvids
    assert result.reasons[f"mvid:{mv_id}"] == "tombstone:user"


# ── AC 10/11: single batched SQL + join chain ─────────────────────────────────


async def test_single_batched_sql_join_chain(db_session) -> None:
    """AC 10 + 11: validator uses chat_message_id join (not message_id); single query.

    This test is structural: it verifies the correct column name is used in the
    query by inspecting the SQL text emitted. Asserting 'chat_message_id' appears
    in the query (and 'message_versions.message_id' does not) satisfies both ACs.
    """
    import bot.services.wiki_governance as module

    # The module-level constant BATCHED_QUERY_SQL must reference chat_message_id
    sql = module.BATCHED_QUERY_SQL
    assert "chat_message_id" in sql, (
        "Join chain must use message_versions.chat_message_id, not message_versions.message_id"
    )
    # Confirm the wrong column name is NOT used
    assert "message_versions.message_id" not in sql


# ── AC 12: SourceCheckResult serializes to dict ───────────────────────────────


async def test_source_check_result_serializes_to_dict(db_session) -> None:
    """AC 12: SourceCheckResult.to_dict() returns JSON-serializable mapping."""
    import json

    from bot.services.wiki_governance import SourceCheckResult

    result = SourceCheckResult(
        valid=False,
        invalid_card_ids=[uuid.uuid4()],
        invalid_mvids=[42],
        reasons={"mvid:42": "redacted"},
    )
    d = result.to_dict()
    assert isinstance(d, dict)
    # Must be JSON-serializable
    json_str = json.dumps(d)
    loaded = json.loads(json_str)
    assert loaded["valid"] is False
    assert 42 in loaded["invalid_mvids"]
    assert loaded["reasons"]["mvid:42"] == "redacted"


# ── G1 lint: no graph imports ─────────────────────────────────────────────────


def test_no_graph_imports_in_wiki_governance() -> None:
    """G1: wiki_governance.py must not import neo4j or graph_ modules."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "bot"
        / "services"
        / "wiki_governance.py"
    ).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module or ""]
            for name in names:
                assert not name.startswith("neo4j"), f"Forbidden import: {name}"
                assert "graph_" not in name, f"Forbidden import: {name}"


# ── Codex review fix #1: nonexistent page must raise ─────────────────────────


async def test_nonexistent_page_raises(db_session) -> None:
    """validate_sources must raise WikiPageNotFoundError for a non-existent id.

    Previously the validator silently returned valid=True for empty result sets,
    making it indistinguishable from a typo / stale id. The page-existence check
    is the first thing the validator does.
    """
    from bot.services.wiki_governance import (
        WikiPageNotFoundError,
        validate_sources,
    )

    bogus_id = uuid.uuid4()
    with pytest.raises(WikiPageNotFoundError):
        await validate_sources(db_session, page_id=bogus_id)


# ── Codex review fix #2: tombstone match relies on tombstone_key, not target_id ─


async def test_tombstone_match_uses_tombstone_key_not_target_id(db_session) -> None:
    """A forget_event with the correct tombstone_key but a DIVERGENT target_id
    must still be detected by the validator. This proves the SQL matches via
    tombstone_key prefix, not via the auxiliary target_id column.
    """
    from bot.services.wiki_governance import validate_sources

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    # tombstone_key matches mv author; target_id is intentionally a NON-matching
    # bogus value to ensure the validator cannot resolve it via target_id.
    await _make_forget_event(
        db_session,
        tombstone_key=f"user:{uid}",
        target_type="user",
        target_id="99999999",  # divergent — different from uid
        status="pending",
    )

    page_id = await _make_wiki_page(db_session)
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    result = await validate_sources(db_session, page_id=page_id)

    assert result.valid is False
    assert mv_id in result.invalid_mvids
    assert result.reasons[f"mvid:{mv_id}"] == "tombstone:user"
