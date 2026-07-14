"""T6-04 + T6-05 acceptance tests — knowledge_cards repo.

PHASE6_PLAN.md §5.A + §5.C step 5: repo backs the ``/approve`` INSERT step
plus ``/cards`` / ``/card <id>`` browse queries.

Methods covered:

* ``create(session, title, body_markdown, approved_by_user_id)`` — insert an
  approved card; populates ``card_status='approved'``, ``approved_at=now()``,
  required for the ``ck_knowledge_cards_approved_attribution`` CHECK.
* ``list_approved(session, limit, offset)`` — paginated browse of approved
  cards only.
* ``get_by_id_prefix(session, prefix)`` — resolve a card by short UUID prefix;
  returns up to 2 rows so caller can detect ambiguity.
* ``get_by_id(session, card_id)`` — exact lookup (returns row or None).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_990_000_000)


def _next_user_id() -> int:
    return next(_user_counter)


async def _make_admin(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"admin_{uid}",
        first_name="Admin",
        last_name=None,
    )
    return uid


# ─── create ──────────────────────────────────────────────────────────────────


async def test_create_inserts_approved_card(db_session) -> None:
    """``create`` writes a row with card_status='approved' and audit columns."""
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    admin = await _make_admin(db_session)
    card = await KnowledgeCardRepo.create(
        db_session,
        title="Title",
        body_markdown="Body",
        approved_by_user_id=admin,
        topic_slug="test-topic",
    )
    assert card.id is not None
    assert card.title == "Title"
    assert card.topic_slug == "test-topic"
    assert card.body_markdown == "Body"
    assert card.card_status == "approved"
    assert card.approved_by_user_id == admin
    assert card.approved_at is not None


# ─── list_approved ───────────────────────────────────────────────────────────


async def test_list_approved_returns_only_approved(db_session) -> None:
    """Filter ``card_status='approved'`` — draft/archived hidden by default."""
    from bot.db.models import KnowledgeCard
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    admin = await _make_admin(db_session)
    approved = await KnowledgeCardRepo.create(
        db_session,
        title="approved",
        body_markdown="body",
        approved_by_user_id=admin,
    )

    # Insert a draft and archived card directly (not via repo create — they
    # represent pre/post lifecycle states).
    draft_card = KnowledgeCard(
        title="draft",
        body_markdown="d",
        card_status="draft",
    )
    db_session.add(draft_card)
    await db_session.flush()
    archived_card = KnowledgeCard(
        title="archived",
        body_markdown="a",
        card_status="archived",
        archived_reason="for test",
    )
    db_session.add(archived_card)
    await db_session.flush()

    rows = await KnowledgeCardRepo.list_approved(db_session, limit=10, offset=0)
    ids = [r.id for r in rows]
    assert approved.id in ids
    assert draft_card.id not in ids
    assert archived_card.id not in ids


async def test_list_approved_orders_by_approved_at_desc(db_session) -> None:
    """Newest approvals first (design §2 query)."""
    from bot.db.models import KnowledgeCard
    from bot.db.repos.knowledge_card import KnowledgeCardRepo
    from sqlalchemy import update

    admin = await _make_admin(db_session)
    older = await KnowledgeCardRepo.create(
        db_session,
        title="older",
        body_markdown="b",
        approved_by_user_id=admin,
    )
    newer = await KnowledgeCardRepo.create(
        db_session,
        title="newer",
        body_markdown="b",
        approved_by_user_id=admin,
    )
    base = datetime.now(timezone.utc)
    await db_session.execute(
        update(KnowledgeCard)
        .where(KnowledgeCard.id == older.id)
        .values(approved_at=base - timedelta(minutes=10))
    )
    await db_session.execute(
        update(KnowledgeCard).where(KnowledgeCard.id == newer.id).values(approved_at=base)
    )
    await db_session.flush()

    rows = await KnowledgeCardRepo.list_approved(db_session, limit=10, offset=0)
    ids = [r.id for r in rows]
    assert ids.index(newer.id) < ids.index(older.id)


async def test_list_approved_pagination(db_session) -> None:
    """LIMIT/OFFSET drive pagination."""
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    admin = await _make_admin(db_session)
    for i in range(4):
        await KnowledgeCardRepo.create(
            db_session,
            title=f"t{i}",
            body_markdown="b",
            approved_by_user_id=admin,
        )

    page1 = await KnowledgeCardRepo.list_approved(db_session, limit=2, offset=0)
    page2 = await KnowledgeCardRepo.list_approved(db_session, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    p1_ids = {r.id for r in page1}
    p2_ids = {r.id for r in page2}
    assert p1_ids.isdisjoint(p2_ids)


# ─── get_by_id ───────────────────────────────────────────────────────────────


async def test_get_by_id_returns_row(db_session) -> None:
    """Returns the exact row (no status filter)."""
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    admin = await _make_admin(db_session)
    card = await KnowledgeCardRepo.create(
        db_session,
        title="t",
        body_markdown="b",
        approved_by_user_id=admin,
    )
    row = await KnowledgeCardRepo.get_by_id(db_session, card.id)
    assert row is not None
    assert row.id == card.id


async def test_get_by_id_returns_none_when_missing(db_session) -> None:
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    row = await KnowledgeCardRepo.get_by_id(db_session, uuid.uuid4())
    assert row is None


# ─── get_by_id_prefix ────────────────────────────────────────────────────────


async def test_get_by_id_prefix_returns_match(db_session) -> None:
    """Short prefix resolves to a single card when exactly one matches."""
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    admin = await _make_admin(db_session)
    card = await KnowledgeCardRepo.create(
        db_session,
        title="t",
        body_markdown="b",
        approved_by_user_id=admin,
    )
    prefix = str(card.id)[:8]
    rows = await KnowledgeCardRepo.get_by_id_prefix(db_session, prefix)
    assert len(rows) >= 1
    assert any(r.id == card.id for r in rows)


async def test_get_by_id_prefix_limits_to_two(db_session) -> None:
    """LIMIT 2 so the handler can detect ambiguity without scanning all rows."""
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    rows = await KnowledgeCardRepo.get_by_id_prefix(db_session, "")
    # empty prefix matches every row — LIMIT 2 enforces no full scan output.
    assert len(rows) <= 2
