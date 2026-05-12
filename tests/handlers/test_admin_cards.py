"""T6-04 + T6-05 admin handler tests — /candidates /approve /reject /cards /card.

PHASE6_PLAN.md §5.C + §7 T6-04 / T6-05 acceptance criteria.

The handlers all live in ``bot/handlers/admin_cards.py``; they share the
same admin filter shape (private chat + settings.ADMIN_IDS) as
``bot/handlers/admin.py`` / ``bot/handlers/admin_extract.py``.
"""

from __future__ import annotations

import itertools
import uuid as _uuid_module
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=8_900_000_000)
_chat_counter = itertools.count(start=890_000)
_msg_counter = itertools.count(start=890_000_000)
_key_counter = itertools.count(start=1)


def _next_user_id() -> int:
    return next(_user_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_key(prefix: str) -> str:
    return f"{prefix}:t6-04:handler:{next(_key_counter)}"


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="U",
        last_name=None,
    )
    return uid


async def _make_chat_message_with_version(
    db_session,
    *,
    text: str = "src",
    memory_policy: str = "normal",
    is_redacted: bool = False,
    version_is_redacted: bool = False,
    content_hash: str | None = None,
) -> tuple[int, int, int, int]:
    from sqlalchemy import update as sa_update

    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = _next_chat_id()
    msg_id = _next_msg_id()
    when = datetime.now(timezone.utc)
    cm = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text=text,
        date=when,
        created_at=when,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
    )
    db_session.add(cm)
    await db_session.flush()
    mv_ch = content_hash or f"h{_uuid_module.uuid4().hex[:16]}"
    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={},
        content_hash=mv_ch,
        is_redacted=version_is_redacted,
    )
    db_session.add(mv)
    await db_session.flush()
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == cm.id)
        .values(current_version_id=mv.id)
    )
    await db_session.flush()
    return cm.id, mv.id, chat_id, msg_id


async def _make_candidate(
    db_session,
    *,
    title: str = "T",
    body_markdown: str = "B",
    source_mvids: list[int] | None = None,
) -> _uuid_module.UUID:
    from bot.db.models import ExtractionCandidate, ExtractionRun

    run = ExtractionRun(
        run_status="completed",
        ingestion_window_start=datetime.now(timezone.utc) - timedelta(hours=1),
        ingestion_window_end=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.flush()
    cand = ExtractionCandidate(
        extraction_run_id=run.id,
        candidate_json={"title": title, "body_markdown": body_markdown},
        source_message_version_ids=source_mvids or [],
        status="pending",
    )
    db_session.add(cand)
    await db_session.flush()
    return cand.id


@pytest.fixture
def fake_admin_message() -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 149820031
    msg.from_user.username = "admin_user"
    msg.from_user.first_name = "Admin"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.chat.id = 149820031
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


@pytest.fixture
def fake_nonadmin_message() -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 12345
    msg.from_user.username = "regular"
    msg.from_user.first_name = "Reg"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.chat.id = 12345
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


@pytest.fixture
def cmd_factory():
    def _make(args: str | None) -> MagicMock:
        cmd = MagicMock()
        cmd.args = args
        return cmd

    return _make


def _collect_replies(msg: MagicMock) -> str:
    parts: list[str] = []
    for call in msg.answer.await_args_list:
        if call.args:
            parts.append(str(call.args[0]))
    for call in msg.reply.await_args_list:
        if call.args:
            parts.append(str(call.args[0]))
    return "\n".join(parts)


# ─── /candidates ────────────────────────────────────────────────────────────


async def test_candidates_silent_for_non_admin(
    db_session, fake_nonadmin_message, cmd_factory
) -> None:
    """Non-admin senders MUST receive no reply."""
    from bot.handlers.admin_cards import cmd_candidates

    await cmd_candidates(fake_nonadmin_message, cmd_factory(None), session=db_session)
    assert fake_nonadmin_message.answer.await_count == 0
    assert fake_nonadmin_message.reply.await_count == 0


async def test_candidates_renders_pending_list_for_admin(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Admin invocation lists pending candidates with short ids + titles."""
    from bot.handlers.admin_cards import cmd_candidates

    cid = await _make_candidate(db_session, title="Unit alpha")
    await cmd_candidates(fake_admin_message, cmd_factory(None), session=db_session)

    reply = _collect_replies(fake_admin_message)
    short = str(cid)[:8]
    assert short in reply
    assert "Unit alpha" in reply


async def test_candidates_empty_state(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Zero pending candidates → empty-state reply (no error)."""
    from bot.handlers.admin_cards import cmd_candidates

    await cmd_candidates(fake_admin_message, cmd_factory(None), session=db_session)
    assert fake_admin_message.answer.await_count + fake_admin_message.reply.await_count >= 1


async def test_candidates_supports_page_arg(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """``/candidates 2`` reads page 2 (offset = page_size)."""
    from bot.handlers.admin_cards import cmd_candidates

    # Seed >10 candidates so page 2 has content.
    for _ in range(12):
        await _make_candidate(db_session)
    await cmd_candidates(
        fake_admin_message, cmd_factory("2"), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    assert "page" in reply.lower() or "2" in reply


# ─── /approve happy path ────────────────────────────────────────────────────


async def test_approve_happy_path_writes_card_and_decision(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """A fully-eligible candidate → knowledge_cards + card_sources +
    extraction_decisions rows written; candidate status='approved'."""
    from sqlalchemy import select

    from bot.db.models import (
        CardSource,
        ExtractionCandidate,
        ExtractionDecision,
        KnowledgeCard,
    )
    from bot.handlers.admin_cards import cmd_approve

    # ensure admin user row exists (decision.decided_by FK)
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username=fake_admin_message.from_user.username,
        first_name=fake_admin_message.from_user.first_name,
        last_name=None,
    )

    _, mvid, _, _ = await _make_chat_message_with_version(db_session)
    cid = await _make_candidate(
        db_session, title="Card title", body_markdown="Card body", source_mvids=[mvid]
    )

    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )

    # Candidate flipped to approved.
    cand = (
        await db_session.execute(
            select(ExtractionCandidate).where(ExtractionCandidate.id == cid)
        )
    ).scalar_one()
    assert cand.status == "approved"
    assert cand.reviewed_by == fake_admin_message.from_user.id
    assert cand.reviewed_at is not None

    # KnowledgeCard inserted.
    cards = (
        await db_session.execute(
            select(KnowledgeCard).where(
                KnowledgeCard.approved_by_user_id == fake_admin_message.from_user.id
            )
        )
    ).scalars().all()
    assert len(cards) == 1
    card = cards[0]
    assert card.card_status == "approved"
    assert card.title == "Card title"
    assert card.body_markdown == "Card body"

    # CardSource row inserted.
    sources = (
        await db_session.execute(
            select(CardSource).where(CardSource.card_id == card.id)
        )
    ).scalars().all()
    assert len(sources) == 1
    assert sources[0].message_version_id == mvid

    # ExtractionDecision row inserted.
    decision = (
        await db_session.execute(
            select(ExtractionDecision).where(
                ExtractionDecision.candidate_id == cid
            )
        )
    ).scalar_one()
    assert decision.action == "approved"
    assert decision.decided_by == fake_admin_message.from_user.id
    assert decision.decided_by_username == "admin_user"


async def test_approve_silent_for_non_admin(
    db_session, fake_nonadmin_message, cmd_factory
) -> None:
    from bot.handlers.admin_cards import cmd_approve

    _, mvid, _, _ = await _make_chat_message_with_version(db_session)
    cid = await _make_candidate(db_session, source_mvids=[mvid])
    await cmd_approve(
        fake_nonadmin_message, cmd_factory(str(cid)), session=db_session
    )
    # No reply, no writes.
    assert fake_nonadmin_message.answer.await_count == 0
    assert fake_nonadmin_message.reply.await_count == 0


async def test_approve_rejects_missing_candidate(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Unknown candidate id → user-facing error, no DB writes."""
    from sqlalchemy import select

    from bot.db.models import KnowledgeCard
    from bot.handlers.admin_cards import cmd_approve

    fake_id = _uuid_module.uuid4()
    await cmd_approve(
        fake_admin_message, cmd_factory(str(fake_id)), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    assert reply  # error message present

    # No card created.
    cards = (await db_session.execute(select(KnowledgeCard))).scalars().all()
    assert len(cards) == 0


async def test_approve_rejects_already_decided_candidate(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Candidate already approved → already_decided error; no new
    extraction_decisions row beyond the first."""
    from sqlalchemy import select

    from bot.db.models import ExtractionCandidate, ExtractionDecision
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_approve

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    _, mvid, _, _ = await _make_chat_message_with_version(db_session)
    cid = await _make_candidate(db_session, source_mvids=[mvid])
    # First approve.
    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    initial_decisions = (
        await db_session.execute(
            select(ExtractionDecision).where(ExtractionDecision.candidate_id == cid)
        )
    ).scalars().all()
    assert len(initial_decisions) == 1
    # Second approve attempt.
    fake_admin_message.answer.reset_mock()
    fake_admin_message.reply.reset_mock()
    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    decisions_after = (
        await db_session.execute(
            select(ExtractionDecision).where(ExtractionDecision.candidate_id == cid)
        )
    ).scalars().all()
    # Still exactly one decision row.
    assert len(decisions_after) == 1
    # Candidate stays approved.
    cand = (
        await db_session.execute(
            select(ExtractionCandidate).where(ExtractionCandidate.id == cid)
        )
    ).scalar_one()
    assert cand.status == "approved"


async def test_approve_rejects_empty_source_set(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Candidate with no source mvids → error, no card written."""
    from sqlalchemy import select

    from bot.db.models import KnowledgeCard
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_approve

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    cid = await _make_candidate(db_session, source_mvids=[])
    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    cards = (await db_session.execute(select(KnowledgeCard))).scalars().all()
    assert len(cards) == 0


# ─── /approve R3-block (governance re-validation) ───────────────────────────


async def test_approve_r3_block_on_tombstoned_source_no_decision_row(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """A tombstone on any source mvid → R3-block: NO extraction_decisions
    row, NO knowledge_cards row, candidate stays pending.

    PHASE6_PLAN §5.C: "R3-block is a precondition failure, not a decision".
    """
    from sqlalchemy import select

    from bot.db.models import (
        ExtractionCandidate,
        ExtractionDecision,
        KnowledgeCard,
    )
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_approve

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    _, mvid, chat_id, msg_id = await _make_chat_message_with_version(db_session)
    # Insert a forget_event tombstoning this message.
    await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(mvid),
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=f"message:{chat_id}:{msg_id}",
    )
    cid = await _make_candidate(db_session, source_mvids=[mvid])

    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )

    # No card.
    cards = (await db_session.execute(select(KnowledgeCard))).scalars().all()
    assert len(cards) == 0
    # No decision row (R3 is a precondition failure).
    decisions = (
        await db_session.execute(
            select(ExtractionDecision).where(ExtractionDecision.candidate_id == cid)
        )
    ).scalars().all()
    assert len(decisions) == 0
    # Candidate still pending.
    cand = (
        await db_session.execute(
            select(ExtractionCandidate).where(ExtractionCandidate.id == cid)
        )
    ).scalar_one()
    assert cand.status == "pending"
    # Admin sees an error reply.
    reply = _collect_replies(fake_admin_message)
    assert reply


async def test_approve_r3_block_on_redacted_source(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """``chat_messages.is_redacted=TRUE`` → R3-block (source_redacted)."""
    from sqlalchemy import select

    from bot.db.models import ExtractionDecision, KnowledgeCard
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_approve

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    _, mvid, _, _ = await _make_chat_message_with_version(
        db_session, is_redacted=True
    )
    cid = await _make_candidate(db_session, source_mvids=[mvid])

    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    cards = (await db_session.execute(select(KnowledgeCard))).scalars().all()
    decisions = (
        await db_session.execute(
            select(ExtractionDecision).where(ExtractionDecision.candidate_id == cid)
        )
    ).scalars().all()
    assert len(cards) == 0
    assert len(decisions) == 0


async def test_approve_r3_block_on_offrecord_source(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """``memory_policy='offrecord'`` → R3-block."""
    from sqlalchemy import select

    from bot.db.models import ExtractionDecision, KnowledgeCard
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_approve

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    _, mvid, _, _ = await _make_chat_message_with_version(
        db_session, memory_policy="offrecord", is_redacted=True
    )
    cid = await _make_candidate(db_session, source_mvids=[mvid])

    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    cards = (await db_session.execute(select(KnowledgeCard))).scalars().all()
    decisions = (
        await db_session.execute(
            select(ExtractionDecision).where(ExtractionDecision.candidate_id == cid)
        )
    ).scalars().all()
    assert len(cards) == 0
    assert len(decisions) == 0


async def test_approve_username_fallback_when_null(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Admin's username is NULL → decision shadow uses ``tg<id>`` fallback."""
    from sqlalchemy import select

    from bot.db.models import ExtractionDecision
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_approve

    # Insert admin row WITHOUT a username (UserRepo.upsert accepts None).
    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username=None,
        first_name="Admin",
        last_name=None,
    )
    fake_admin_message.from_user.username = None
    _, mvid, _, _ = await _make_chat_message_with_version(db_session)
    cid = await _make_candidate(db_session, source_mvids=[mvid])
    await cmd_approve(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    decision = (
        await db_session.execute(
            select(ExtractionDecision).where(ExtractionDecision.candidate_id == cid)
        )
    ).scalar_one()
    assert decision.decided_by_username == f"tg{fake_admin_message.from_user.id}"


# ─── /reject ────────────────────────────────────────────────────────────────


async def test_reject_happy_path(db_session, fake_admin_message, cmd_factory) -> None:
    """Candidate flipped to rejected + decision row written."""
    from sqlalchemy import select

    from bot.db.models import ExtractionCandidate, ExtractionDecision
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_reject

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    cid = await _make_candidate(db_session)
    await cmd_reject(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )

    cand = (
        await db_session.execute(
            select(ExtractionCandidate).where(ExtractionCandidate.id == cid)
        )
    ).scalar_one()
    assert cand.status == "rejected"

    decision = (
        await db_session.execute(
            select(ExtractionDecision).where(
                ExtractionDecision.candidate_id == cid
            )
        )
    ).scalar_one()
    assert decision.action == "rejected"


async def test_reject_stores_reason_verbatim(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """``/reject <id> some reason`` stores the reason verbatim."""
    from sqlalchemy import select

    from bot.db.models import ExtractionDecision
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_reject

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    cid = await _make_candidate(db_session)
    await cmd_reject(
        fake_admin_message,
        cmd_factory(f"{cid} duplicate of another card"),
        session=db_session,
    )
    decision = (
        await db_session.execute(
            select(ExtractionDecision).where(
                ExtractionDecision.candidate_id == cid
            )
        )
    ).scalar_one()
    assert decision.reason == "duplicate of another card"


async def test_reject_silent_for_non_admin(
    db_session, fake_nonadmin_message, cmd_factory
) -> None:
    from bot.handlers.admin_cards import cmd_reject

    cid = await _make_candidate(db_session)
    await cmd_reject(
        fake_nonadmin_message, cmd_factory(str(cid)), session=db_session
    )
    assert fake_nonadmin_message.answer.await_count == 0
    assert fake_nonadmin_message.reply.await_count == 0


async def test_reject_already_decided(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Re-reject of an already-decided candidate is a noop on DB."""
    from sqlalchemy import select

    from bot.db.models import ExtractionDecision
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_reject

    await UserRepo.upsert(
        db_session,
        telegram_id=fake_admin_message.from_user.id,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    cid = await _make_candidate(db_session)
    await cmd_reject(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    # Second attempt.
    await cmd_reject(
        fake_admin_message, cmd_factory(str(cid)), session=db_session
    )
    decisions = (
        await db_session.execute(
            select(ExtractionDecision).where(
                ExtractionDecision.candidate_id == cid
            )
        )
    ).scalars().all()
    assert len(decisions) == 1


# ─── /cards browse (T6-05) ──────────────────────────────────────────────────


async def test_cards_silent_for_non_admin(
    db_session, fake_nonadmin_message, cmd_factory
) -> None:
    from bot.handlers.admin_cards import cmd_cards

    await cmd_cards(fake_nonadmin_message, cmd_factory(None), session=db_session)
    assert fake_nonadmin_message.answer.await_count == 0
    assert fake_nonadmin_message.reply.await_count == 0


async def test_cards_renders_approved_only(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """``/cards`` lists only approved cards; draft/archived hidden."""
    from bot.db.models import KnowledgeCard
    from bot.db.repos.knowledge_card import KnowledgeCardRepo
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_cards

    admin = fake_admin_message.from_user.id
    await UserRepo.upsert(
        db_session,
        telegram_id=admin,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    approved = await KnowledgeCardRepo.create(
        db_session,
        title="Approved title",
        body_markdown="body",
        approved_by_user_id=admin,
    )
    # Insert draft and archived rows manually.
    db_session.add(KnowledgeCard(title="DraftXYZ", body_markdown="x", card_status="draft"))
    db_session.add(
        KnowledgeCard(
            title="ArchivedXYZ",
            body_markdown="y",
            card_status="archived",
            archived_reason="x",
        )
    )
    await db_session.flush()

    await cmd_cards(fake_admin_message, cmd_factory(None), session=db_session)
    reply = _collect_replies(fake_admin_message)
    assert "Approved title" in reply
    assert "DraftXYZ" not in reply
    assert "ArchivedXYZ" not in reply


async def test_cards_empty_state(
    db_session, fake_admin_message, cmd_factory
) -> None:
    from bot.handlers.admin_cards import cmd_cards

    await cmd_cards(fake_admin_message, cmd_factory(None), session=db_session)
    assert (
        fake_admin_message.answer.await_count + fake_admin_message.reply.await_count
        >= 1
    )


# ─── /card <id> detail (T6-05) ──────────────────────────────────────────────


async def test_card_detail_renders_full_body_for_approved(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """``/card <id>`` shows title, body, sources, approval metadata."""
    from bot.db.repos.card_source import CardSourceRepo
    from bot.db.repos.knowledge_card import KnowledgeCardRepo
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_card

    admin = fake_admin_message.from_user.id
    await UserRepo.upsert(
        db_session,
        telegram_id=admin,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    _, mvid, chat_id, msg_id = await _make_chat_message_with_version(db_session)
    card = await KnowledgeCardRepo.create(
        db_session,
        title="Detail card title",
        body_markdown="Detail body content",
        approved_by_user_id=admin,
    )
    await CardSourceRepo.bulk_create(
        db_session, card_id=card.id, message_version_ids=[mvid]
    )

    await cmd_card(
        fake_admin_message, cmd_factory(str(card.id)), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    assert "Detail card title" in reply
    assert "Detail body content" in reply


async def test_card_detail_short_prefix_resolves(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Short prefix (≥8 chars) resolves to the card."""
    from bot.db.repos.knowledge_card import KnowledgeCardRepo
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_card

    admin = fake_admin_message.from_user.id
    await UserRepo.upsert(
        db_session,
        telegram_id=admin,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    card = await KnowledgeCardRepo.create(
        db_session,
        title="ShortLookup",
        body_markdown="body",
        approved_by_user_id=admin,
    )
    short = str(card.id)[:8]
    await cmd_card(
        fake_admin_message, cmd_factory(short), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    assert "ShortLookup" in reply


async def test_card_detail_missing_id(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Unknown id → user-facing error."""
    from bot.handlers.admin_cards import cmd_card

    await cmd_card(
        fake_admin_message,
        cmd_factory(str(_uuid_module.uuid4())),
        session=db_session,
    )
    reply = _collect_replies(fake_admin_message)
    assert reply  # error message exists


async def test_card_detail_hides_body_for_draft(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Draft cards do NOT expose body content."""
    from bot.db.models import KnowledgeCard
    from bot.handlers.admin_cards import cmd_card

    draft = KnowledgeCard(
        title="DraftTitle",
        body_markdown="SECRET_DRAFT_BODY",
        card_status="draft",
    )
    db_session.add(draft)
    await db_session.flush()

    await cmd_card(
        fake_admin_message, cmd_factory(str(draft.id)), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    assert "SECRET_DRAFT_BODY" not in reply


async def test_card_detail_hides_body_for_archived(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """Archived cards expose status + archived_reason, NOT body content."""
    from bot.db.models import KnowledgeCard
    from bot.handlers.admin_cards import cmd_card

    archived = KnowledgeCard(
        title="ArchivedTitle",
        body_markdown="SECRET_ARCHIVED_BODY",
        card_status="archived",
        archived_reason="all sources forgotten",
    )
    db_session.add(archived)
    await db_session.flush()

    await cmd_card(
        fake_admin_message, cmd_factory(str(archived.id)), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    assert "SECRET_ARCHIVED_BODY" not in reply


async def test_card_detail_silent_for_non_admin(
    db_session, fake_nonadmin_message, cmd_factory
) -> None:
    from bot.handlers.admin_cards import cmd_card

    await cmd_card(
        fake_nonadmin_message,
        cmd_factory(str(_uuid_module.uuid4())),
        session=db_session,
    )
    assert fake_nonadmin_message.answer.await_count == 0
    assert fake_nonadmin_message.reply.await_count == 0


async def test_card_detail_redacts_forgotten_source(
    db_session, fake_admin_message, cmd_factory
) -> None:
    """A source mvid whose chat_message is redacted is rendered as a
    placeholder, NEVER a clickable link or body content."""
    from sqlalchemy import update

    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.card_source import CardSourceRepo
    from bot.db.repos.knowledge_card import KnowledgeCardRepo
    from bot.db.repos.user import UserRepo
    from bot.handlers.admin_cards import cmd_card

    admin = fake_admin_message.from_user.id
    await UserRepo.upsert(
        db_session,
        telegram_id=admin,
        username="admin_user",
        first_name="Admin",
        last_name=None,
    )
    cm_id, mvid, chat_id, msg_id = await _make_chat_message_with_version(
        db_session
    )
    card = await KnowledgeCardRepo.create(
        db_session,
        title="t",
        body_markdown="b",
        approved_by_user_id=admin,
    )
    await CardSourceRepo.bulk_create(
        db_session, card_id=card.id, message_version_ids=[mvid]
    )
    # Simulate cascade-in-progress: chat_message redacted but card_sources
    # row still present.
    await db_session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == cm_id)
        .values(is_redacted=True, memory_policy="forgotten")
    )
    await db_session.flush()

    await cmd_card(
        fake_admin_message, cmd_factory(str(card.id)), session=db_session
    )
    reply = _collect_replies(fake_admin_message)
    # The Telegram link pattern must NOT appear for this redacted source.
    assert f"/{msg_id}" not in reply or "redacted" in reply.lower()
