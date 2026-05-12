"""T6-06: card-extension tests for ``search_messages``.

These tests exercise the ``include_cards=True`` path of the search service:
- UNION ALL with the Phase 4 message branch.
- ``card_status='approved'`` gate.
- Defense-in-depth source-state subqueries (forget tombstones / redaction /
  memory_policy mismatch must exclude the card even if it remains
  ``approved``).
- Card rank boost above equivalent message hits.
- ``SearchHit`` discriminator fields populated for card hits and at defaults
  for message hits.

All tests require Postgres because the card branch uses Russian-language
``to_tsvector`` / ``ts_rank_cd`` / ``ts_headline``. SQLite tests cover the
dialect-guard separately (see ``test_search_cards_sqlite_safe_noop``).
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

# Counters offset from test_search.py ranges to avoid PK collisions when
# both test files run in the same session.
_user_counter = itertools.count(start=10_600_000_000)
_message_counter = itertools.count(start=1_060_000)
_hash_counter = itertools.count(start=1)


@dataclass(frozen=True)
class CreatedMessage:
    chat_message_id: int
    version_id: int
    message_id: int
    chat_id: int
    user_id: int
    content_hash: str


@dataclass(frozen=True)
class CreatedCard:
    card_id: uuid.UUID
    source_version_ids: tuple[int, ...]


async def _create_versioned_message(
    db_session,
    *,
    chat_id: int,
    text: str = "питон любит память",
    memory_policy: str = "normal",
    chat_is_redacted: bool = False,
    version_is_redacted: bool = False,
    message_id: int | None = None,
) -> CreatedMessage:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    user_id = next(_user_counter)
    tg_message_id = message_id if message_id is not None else next(_message_counter)
    content_hash = f"card-hash-{next(_hash_counter)}"

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"u{user_id}",
        first_name="Card",
        last_name=None,
    )

    chat_message = ChatMessage(
        message_id=tg_message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        caption=None,
        date=datetime.now(timezone.utc),
        memory_policy=memory_policy,
        is_redacted=chat_is_redacted,
        content_hash=content_hash,
    )
    db_session.add(chat_message)
    await db_session.flush()

    version = MessageVersion(
        chat_message_id=chat_message.id,
        version_seq=1,
        text=text,
        caption=None,
        normalized_text=text,
        content_hash=content_hash,
        is_redacted=version_is_redacted,
    )
    db_session.add(version)
    await db_session.flush()

    chat_message.current_version_id = version.id
    await db_session.flush()

    return CreatedMessage(
        chat_message_id=chat_message.id,
        version_id=version.id,
        message_id=tg_message_id,
        chat_id=chat_id,
        user_id=user_id,
        content_hash=content_hash,
    )


async def _create_approved_card(
    db_session,
    *,
    body_markdown: str,
    source_version_ids: tuple[int, ...],
    title: str = "Карточка",
    card_status: str = "approved",
    archived_reason: str | None = None,
) -> CreatedCard:
    """Insert one knowledge_cards row + card_sources rows in arrival order."""
    from bot.db.models import CardSource, KnowledgeCard
    from bot.db.repos.user import UserRepo

    admin_id = next(_user_counter)
    await UserRepo.upsert(
        db_session,
        telegram_id=admin_id,
        username=f"admin{admin_id}",
        first_name="Admin",
        last_name=None,
    )

    approved_at = datetime.now(timezone.utc) if card_status == "approved" else None
    approved_by_user_id = admin_id if card_status == "approved" else None

    card = KnowledgeCard(
        title=title,
        body_markdown=body_markdown,
        card_status=card_status,
        archived_reason=archived_reason,
        approved_by_user_id=approved_by_user_id,
        approved_at=approved_at,
    )
    db_session.add(card)
    await db_session.flush()

    for position, mvid in enumerate(source_version_ids):
        db_session.add(
            CardSource(
                card_id=card.id,
                message_version_id=mvid,
                position=position,
            )
        )
    await db_session.flush()

    return CreatedCard(
        card_id=card.id,
        source_version_ids=source_version_ids,
    )


async def _create_forget_event(
    db_session,
    *,
    tombstone_key: str,
    target_type: str = "message",
    target_id: str | None = None,
    status: str = "completed",
) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    event = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    if status == "pending":
        return
    await ForgetEventRepo.mark_status(db_session, event.id, status="processing")
    if status == "completed":
        await ForgetEventRepo.mark_status(db_session, event.id, status="completed")


# ─── Acceptance: include_cards default False vs True semantics ───────────────


async def test_include_cards_false_omits_card_hits(db_session) -> None:
    """Phase 4 semantics preserved when caller opts out."""
    from bot.services.search import search_messages

    chat_id = -100_601
    msg = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="питон обсуждение",
    )
    await _create_approved_card(
        db_session,
        body_markdown="питон карточка тут",
        source_version_ids=(msg.version_id,),
    )

    hits = await search_messages(
        db_session, "питон", chat_id=chat_id, include_cards=False
    )

    assert [h.source_type for h in hits] == ["message"]
    assert all(h.card_id is None for h in hits)


async def test_include_cards_true_returns_card_when_only_card_matches(db_session) -> None:
    """If no messages match but a card does, card surfaces in results."""
    from bot.services.search import search_messages

    chat_id = -100_602
    msg = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="нерелевантное обсуждение",
    )
    card = await _create_approved_card(
        db_session,
        body_markdown="карточка про специфическое слово диковина",
        source_version_ids=(msg.version_id,),
    )

    hits = await search_messages(
        db_session, "диковина", chat_id=chat_id, include_cards=True
    )

    assert len(hits) == 1
    assert hits[0].source_type == "card"
    assert hits[0].card_id == card.card_id
    assert hits[0].card_source_message_version_ids == (msg.version_id,)


async def test_include_cards_true_merges_message_and_card_hits(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_603
    msg = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="питон сообщение",
    )
    msg2 = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="питон карточка-источник",
    )
    await _create_approved_card(
        db_session,
        body_markdown="питон карточка тут",
        source_version_ids=(msg2.version_id,),
    )

    hits = await search_messages(
        db_session, "питон", chat_id=chat_id, include_cards=True, limit=10
    )

    types = sorted(h.source_type for h in hits)
    assert "card" in types
    assert "message" in types
    # at least the original first message is present
    assert any(h.message_version_id == msg.version_id for h in hits)


# ─── card_status gating ──────────────────────────────────────────────────────


async def test_draft_card_is_not_returned(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_604
    msg = await _create_versioned_message(
        db_session, chat_id=chat_id, text="ничего не найдём"
    )
    await _create_approved_card(
        db_session,
        body_markdown="драфт-карточка уникальное диковина2",
        source_version_ids=(msg.version_id,),
        card_status="draft",
    )

    hits = await search_messages(
        db_session, "диковина2", chat_id=chat_id, include_cards=True
    )

    assert [h.source_type for h in hits if h.source_type == "card"] == []


async def test_archived_card_is_not_returned(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_605
    msg = await _create_versioned_message(
        db_session, chat_id=chat_id, text="ничего не найдём"
    )
    await _create_approved_card(
        db_session,
        body_markdown="archived карточка диковина3",
        source_version_ids=(msg.version_id,),
        card_status="archived",
        archived_reason="cascade",
    )

    hits = await search_messages(
        db_session, "диковина3", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


# ─── Defense-in-depth source-state subqueries ────────────────────────────────


async def test_card_with_offrecord_source_excluded(db_session) -> None:
    """If ANY card source's chat_message has memory_policy != 'normal', exclude."""
    from bot.services.search import search_messages

    chat_id = -100_606
    msg = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="источник offrecord",
        memory_policy="offrecord",
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка с offrecord источником диковина4",
        source_version_ids=(msg.version_id,),
    )

    hits = await search_messages(
        db_session, "диковина4", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


async def test_card_with_redacted_chat_message_source_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_607
    msg = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="источник редактированный",
        chat_is_redacted=True,
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка с redacted источником диковина5",
        source_version_ids=(msg.version_id,),
    )

    hits = await search_messages(
        db_session, "диковина5", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


async def test_card_with_redacted_message_version_source_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_608
    msg = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="версия редактирована",
        version_is_redacted=True,
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка mv-redacted диковина6",
        source_version_ids=(msg.version_id,),
    )

    hits = await search_messages(
        db_session, "диковина6", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


async def test_card_with_completed_forget_event_on_one_source_excluded(db_session) -> None:
    """L6a equivalent: forget one of three sources → card excluded even if
    cascade keeps card_status='approved' because remaining_count > 0."""
    from bot.services.search import search_messages

    chat_id = -100_609
    msgs = [
        await _create_versioned_message(
            db_session, chat_id=chat_id, text=f"источник {i}"
        )
        for i in range(3)
    ]
    await _create_approved_card(
        db_session,
        body_markdown="карточка 3-source диковина7",
        source_version_ids=tuple(m.version_id for m in msgs),
    )
    # Forget the first source.
    await _create_forget_event(
        db_session,
        tombstone_key=f"message:{msgs[0].chat_id}:{msgs[0].message_id}",
        target_id=str(msgs[0].chat_message_id),
        status="completed",
    )

    hits = await search_messages(
        db_session, "диковина7", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


async def test_card_with_pending_forget_event_on_one_source_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_610
    msg = await _create_versioned_message(
        db_session, chat_id=chat_id, text="pending-forget source"
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка pending диковина8",
        source_version_ids=(msg.version_id,),
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"message:{msg.chat_id}:{msg.message_id}",
        target_id=str(msg.chat_message_id),
        status="pending",
    )

    hits = await search_messages(
        db_session, "диковина8", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


async def test_card_with_user_tombstone_excluded(db_session) -> None:
    """L3c-style: user-level forget cascades to any card whose source author
    is tombstoned."""
    from bot.services.search import search_messages

    chat_id = -100_611
    msg = await _create_versioned_message(
        db_session, chat_id=chat_id, text="user-tombstone source"
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка user-tombstone диковина9",
        source_version_ids=(msg.version_id,),
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"user:{msg.user_id}",
        target_type="user",
        target_id=str(msg.user_id),
        status="completed",
    )

    hits = await search_messages(
        db_session, "диковина9", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


async def test_card_with_content_hash_tombstone_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_612
    msg = await _create_versioned_message(
        db_session, chat_id=chat_id, text="hash-tombstone source"
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка hash диковина10",
        source_version_ids=(msg.version_id,),
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"message_hash:{msg.content_hash}",
        target_type="message_hash",
        target_id=msg.content_hash,
        status="completed",
    )

    hits = await search_messages(
        db_session, "диковина10", chat_id=chat_id, include_cards=True
    )

    assert [h for h in hits if h.source_type == "card"] == []


# ─── Card rank boost ─────────────────────────────────────────────────────────


async def test_card_hit_ranks_above_equivalent_message_hit(db_session) -> None:
    """A card and a message with similar match → card boosted above."""
    from bot.services.search import search_messages

    chat_id = -100_613
    # Message with similar body — same query keyword present.
    await _create_versioned_message(
        db_session, chat_id=chat_id, text="одинаковая диковина11 здесь"
    )
    src = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="источник карточки",
    )
    await _create_approved_card(
        db_session,
        body_markdown="одинаковая диковина11 здесь",
        source_version_ids=(src.version_id,),
    )

    hits = await search_messages(
        db_session, "диковина11", chat_id=chat_id, include_cards=True, limit=10
    )

    types_in_order = [h.source_type for h in hits]
    # First hit must be card thanks to rank boost.
    assert types_in_order[0] == "card"


# ─── SearchHit shape ─────────────────────────────────────────────────────────


async def test_message_hit_has_default_card_fields(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_614
    await _create_versioned_message(
        db_session, chat_id=chat_id, text="чистое сообщение"
    )
    hits = await search_messages(db_session, "чистое", chat_id=chat_id, include_cards=True)

    assert len(hits) == 1
    h = hits[0]
    assert h.source_type == "message"
    assert h.card_id is None
    assert h.card_source_message_version_ids == ()


async def test_card_hit_carries_source_mvid_tuple(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_615
    sources = [
        await _create_versioned_message(
            db_session, chat_id=chat_id, text=f"источник {i}"
        )
        for i in range(3)
    ]
    expected_mvids = tuple(s.version_id for s in sources)
    await _create_approved_card(
        db_session,
        body_markdown="карточка multi-source диковина12",
        source_version_ids=expected_mvids,
    )

    hits = await search_messages(
        db_session, "диковина12", chat_id=chat_id, include_cards=True
    )

    card_hits = [h for h in hits if h.source_type == "card"]
    assert len(card_hits) == 1
    assert card_hits[0].card_source_message_version_ids == expected_mvids
    # Anchor source is position-0 mvid.
    assert card_hits[0].message_version_id == expected_mvids[0]


# ─── Limit + ordering ───────────────────────────────────────────────────────


async def test_limit_clamps_merged_result(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_616
    sources = []
    for i in range(3):
        m = await _create_versioned_message(
            db_session, chat_id=chat_id, text=f"sourcemsg {i}"
        )
        sources.append(m)
        await _create_approved_card(
            db_session,
            body_markdown=f"карточка-{i} диковина13",
            source_version_ids=(m.version_id,),
        )
    await _create_versioned_message(
        db_session, chat_id=chat_id, text="ещё диковина13 сообщение"
    )

    hits = await search_messages(
        db_session, "диковина13", chat_id=chat_id, include_cards=True, limit=2
    )

    assert len(hits) == 2


# ─── Default value contract ─────────────────────────────────────────────────


async def test_include_cards_default_is_true_per_spec(db_session) -> None:
    """Per PHASE6_PLAN.md §5.D the default is True; absent kwarg surfaces cards."""
    from bot.services.search import search_messages

    chat_id = -100_617
    msg = await _create_versioned_message(
        db_session, chat_id=chat_id, text="нерелевантно"
    )
    await _create_approved_card(
        db_session,
        body_markdown="карточка default диковина14",
        source_version_ids=(msg.version_id,),
    )

    # No include_cards kwarg.
    hits = await search_messages(db_session, "диковина14", chat_id=chat_id)

    assert any(h.source_type == "card" for h in hits)
