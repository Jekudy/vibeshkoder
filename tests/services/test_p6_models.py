"""T6-01 Phase 6 ORM models smoke tests.

Validates that the 5 new SQLAlchemy models bind correctly to the tables
created by migrations 030-034. Each test inserts a representative row via
the ORM and asserts the columns map to the expected DB fields with the
correct nullability and types.

This is the application-layer counterpart of ``test_p6_schema_constraints``
(which uses raw SQL): together they pin both the schema AND the ORM mapping.
A drift in either layer surfaces here.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_600_000_000)


def _next_user() -> int:
    return next(_user_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_message_version(db_session) -> int:
    """Return a valid message_version.id (used as card_sources FK target)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    msg = ChatMessage(
        message_id=int(uuid.uuid4().int % 1_000_000),
        chat_id=-100,
        user_id=uid,
        text="x",
        date=datetime.now(timezone.utc),
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    v = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="x",
        normalized_text="x",
        entities_json={},
        content_hash=f"h{uuid.uuid4().hex[:16]}",
        is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()
    return v.id


# ─── ExtractionRun ───────────────────────────────────────────────────────────


async def test_extraction_run_model_roundtrip(db_session) -> None:
    """ExtractionRun inserts and reads back with the expected field set."""
    from bot.db.models import ExtractionRun

    now = datetime.now(timezone.utc)
    run = ExtractionRun(
        ingestion_window_start=now,
        ingestion_window_end=now,
        candidate_count=3,
        run_status="completed",
        llm_usage_ledger_id=None,
    )
    db_session.add(run)
    await db_session.flush()

    assert isinstance(run.id, uuid.UUID)
    assert run.candidate_count == 3
    assert run.run_status == "completed"
    assert run.created_at is not None


async def test_extraction_run_tablename(db_session) -> None:
    from bot.db.models import ExtractionRun

    assert ExtractionRun.__tablename__ == "extraction_runs"


# ─── ExtractionCandidate ─────────────────────────────────────────────────────


async def test_extraction_candidate_model_roundtrip(db_session) -> None:
    """ExtractionCandidate inserts a pending row + jsonb fields."""
    from bot.db.models import ExtractionCandidate

    candidate = ExtractionCandidate(
        candidate_json={"fact": "Some fact"},
        source_message_version_ids=[1, 2],
        status="pending",
    )
    db_session.add(candidate)
    await db_session.flush()

    assert isinstance(candidate.id, uuid.UUID)
    assert candidate.status == "pending"
    assert candidate.reviewed_by is None
    assert candidate.reviewed_at is None
    assert candidate.candidate_json == {"fact": "Some fact"}
    assert candidate.source_message_version_ids == [1, 2]


async def test_extraction_candidate_tablename(db_session) -> None:
    from bot.db.models import ExtractionCandidate

    assert ExtractionCandidate.__tablename__ == "extraction_candidates"


# ─── KnowledgeCard ───────────────────────────────────────────────────────────


async def test_knowledge_card_draft_roundtrip(db_session) -> None:
    """KnowledgeCard inserts a draft row with no approval metadata."""
    from bot.db.models import KnowledgeCard

    card = KnowledgeCard(
        title="Hello",
        body_markdown="*bold* body",
        card_status="draft",
    )
    db_session.add(card)
    await db_session.flush()

    assert isinstance(card.id, uuid.UUID)
    assert card.card_status == "draft"
    assert card.approved_by_user_id is None
    assert card.approved_at is None
    assert card.archived_reason is None


async def test_knowledge_card_approved_with_attribution(db_session) -> None:
    """Approved row needs both approver columns set."""
    from bot.db.models import KnowledgeCard

    uid = await _make_user(db_session)
    when = datetime.now(timezone.utc)
    card = KnowledgeCard(
        title="Approved",
        body_markdown="body",
        card_status="approved",
        approved_by_user_id=uid,
        approved_at=when,
    )
    db_session.add(card)
    await db_session.flush()

    assert card.approved_by_user_id == uid
    assert card.approved_at is not None


async def test_knowledge_card_tablename(db_session) -> None:
    from bot.db.models import KnowledgeCard

    assert KnowledgeCard.__tablename__ == "knowledge_cards"


# ─── CardSource ──────────────────────────────────────────────────────────────


async def test_card_source_model_roundtrip(db_session) -> None:
    """CardSource links a card to a message_version row."""
    from bot.db.models import CardSource, KnowledgeCard

    card = KnowledgeCard(
        title="t", body_markdown="b", card_status="draft",
    )
    db_session.add(card)
    await db_session.flush()
    mv_id = await _make_message_version(db_session)

    src = CardSource(
        card_id=card.id,
        message_version_id=mv_id,
        position=0,
    )
    db_session.add(src)
    await db_session.flush()

    assert isinstance(src.id, uuid.UUID)
    assert src.card_id == card.id
    assert src.message_version_id == mv_id
    assert src.position == 0


async def test_card_source_tablename(db_session) -> None:
    from bot.db.models import CardSource

    assert CardSource.__tablename__ == "card_sources"


# ─── ExtractionDecision ──────────────────────────────────────────────────────


async def test_extraction_decision_model_roundtrip(db_session) -> None:
    """ExtractionDecision records an admin terminal action with audit shadow."""
    from bot.db.models import ExtractionCandidate, ExtractionDecision

    uid = await _make_user(db_session)
    cand = ExtractionCandidate(
        candidate_json={"x": 1}, status="pending",
    )
    db_session.add(cand)
    await db_session.flush()

    decision = ExtractionDecision(
        candidate_id=cand.id,
        action="rejected",
        reason="bad fact",
        decided_by=uid,
        decided_by_username="admin1",
    )
    db_session.add(decision)
    await db_session.flush()

    assert isinstance(decision.id, uuid.UUID)
    assert decision.action == "rejected"
    assert decision.decided_by_username == "admin1"
    assert decision.decided_at is not None


async def test_extraction_decision_tablename(db_session) -> None:
    from bot.db.models import ExtractionDecision

    assert ExtractionDecision.__tablename__ == "extraction_decisions"


# ─── Cross-model: knowledge_cards.body_tsv is populated by the DB ────────────


async def test_knowledge_cards_body_tsv_generated(db_session) -> None:
    """Inserting a card populates body_tsv server-side (GENERATED ALWAYS).

    The Phase 4 baseline uses to_tsvector('russian', ...). This test
    asserts the column gets non-null content immediately after insert,
    matching the search.py path (T6-06).
    """
    from bot.db.models import KnowledgeCard

    card = KnowledgeCard(
        title="t", body_markdown="привет мир", card_status="draft",
    )
    db_session.add(card)
    await db_session.flush()

    tsv = (
        await db_session.execute(
            text("SELECT body_tsv::text FROM knowledge_cards WHERE id = :id"),
            {"id": str(card.id)},
        )
    ).scalar()
    assert tsv is not None
    assert len(tsv) > 0
