"""Tests for bot/services/digest_context.py — T7-03.

Governance filter checks: card status, window bounds, is_redacted, memory_policy,
chat_id isolation, threshold fallback logic.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=7_300_000_000)
_msg_counter = itertools.count(start=730_000)
_chat_counter = itertools.count(start=7300)


def _next_uid() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


# ── helpers ──────────────────────────────────────────────────────────────────


async def _make_user(db_session, *, display_name: str = "Test User") -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_uid()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name=display_name,
        last_name=None,
    )
    return uid


async def _make_msg_and_version(
    db_session,
    *,
    chat_id: int,
    ts: datetime,
    text: str = "hello",
    memory_policy: str = "normal",
    is_redacted: bool = False,
) -> tuple[int, int]:
    """Create ChatMessage + MessageVersion. Returns (chat_message_id, message_version_id).

    Window filtering uses chat_messages.date (the original Telegram timestamp).
    MessageVersion has no 'ts' column; captured_at is a server default.
    """
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    msg_id = _next_msg_id()

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text=text,
        date=ts,
        raw_json={"text": text},
        memory_policy=memory_policy,
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    mv = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={"entities": []},
        content_hash=f"h-t7-{msg_id}",
        is_redacted=is_redacted,
    )
    db_session.add(mv)
    await db_session.flush()

    # link current_version_id
    msg.current_version_id = mv.id
    await db_session.flush()

    return msg.id, mv.id


async def _make_approved_card(
    db_session,
    *,
    mv_ids: list[int],
    title: str = "Test Card",
    body: str = "Some body text",
    approved_by_uid: int | None = None,
    approved_at: datetime | None = None,
) -> "uuid.UUID":
    """Create KnowledgeCard (approved) + CardSource rows. Returns card UUID."""
    import uuid
    from bot.db.models import KnowledgeCard, CardSource

    if approved_by_uid is None:
        approved_by_uid = await _make_user(db_session)
    if approved_at is None:
        approved_at = datetime.now(timezone.utc)

    card = KnowledgeCard(
        title=title,
        body_markdown=body,
        card_status="approved",
        approved_by_user_id=approved_by_uid,
        approved_at=approved_at,
    )
    db_session.add(card)
    await db_session.flush()

    for mv_id in mv_ids:
        cs = CardSource(card_id=card.id, message_version_id=mv_id)
        db_session.add(cs)
    await db_session.flush()

    return card.id


# ── test cases ────────────────────────────────────────────────────────────────


async def test_build_context_returns_empty_when_no_data(db_session) -> None:
    """Empty DB → empty cards + empty messages."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.cards == []
    assert ctx.messages == []
    assert ctx.source_chat_id == chat_id
    assert ctx.type == "daily"


async def test_build_context_includes_approved_card_in_window(db_session) -> None:
    """Approved card with source in window → returned in cards list."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts_in_window = now - timedelta(hours=12)

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts_in_window
    )
    card_id = await _make_approved_card(db_session, mv_ids=[mv_id], title="My Card")

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert len(ctx.cards) == 1
    assert ctx.cards[0].card_id == card_id
    assert ctx.cards[0].title == "My Card"
    assert ctx.cards[0].source_count == 1


async def test_build_context_excludes_draft_card(db_session) -> None:
    """Draft card is excluded from results."""
    from bot.db.models import KnowledgeCard, CardSource
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    _cm_id, mv_id = await _make_msg_and_version(db_session, chat_id=chat_id, ts=ts)

    # Create a draft card (no approved_by_user_id needed for draft)
    card = KnowledgeCard(
        title="Draft Card",
        body_markdown="draft body",
        card_status="draft",
        approved_by_user_id=None,
        approved_at=None,
    )
    db_session.add(card)
    await db_session.flush()
    cs = CardSource(card_id=card.id, message_version_id=mv_id)
    db_session.add(cs)
    await db_session.flush()

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.cards == []


async def test_build_context_excludes_card_outside_window(db_session) -> None:
    """Card with source ts before window_start → excluded."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts_outside = now - timedelta(hours=36)  # before window_start

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts_outside
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.cards == []


async def test_build_context_excludes_redacted_message_version(db_session) -> None:
    """Card whose only source has is_redacted=TRUE is excluded."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, is_redacted=True
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    # Card with only a redacted source should not appear
    assert ctx.cards == []


async def test_build_context_excludes_offrecord_message(db_session) -> None:
    """Message with memory_policy='offrecord' excluded from both cards and raw fallback."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, memory_policy="offrecord"
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(min_cards_threshold=0),  # force raw fallback attempt
    )

    assert ctx.cards == []
    assert ctx.messages == []


async def test_build_context_excludes_nomem_message(db_session) -> None:
    """Message with memory_policy='nomem' excluded from both cards and raw fallback."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, memory_policy="nomem"
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(min_cards_threshold=0),
    )

    assert ctx.cards == []
    assert ctx.messages == []


async def test_build_context_falls_back_to_raw_messages_when_few_cards(db_session) -> None:
    """Fewer than min_cards_threshold cards → raw messages included."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    _cm_id, _mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, text="raw fallback message"
    )

    # No cards at all → 0 < min_cards_threshold (default 3) → raw fallback
    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.cards == []
    assert len(ctx.messages) >= 1
    assert ctx.messages[0].text == "raw fallback message"


async def test_build_context_skips_raw_fallback_when_enough_cards(db_session) -> None:
    """When enough approved cards exist, messages list stays empty."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    # Create min_cards_threshold cards (default=3)
    threshold = 3
    for i in range(threshold):
        _cm_id, mv_id = await _make_msg_and_version(
            db_session, chat_id=chat_id, ts=ts, text=f"card source {i}"
        )
        await _make_approved_card(
            db_session, mv_ids=[mv_id], title=f"Card {i}", body=f"body {i}"
        )

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(min_cards_threshold=threshold),
    )

    assert len(ctx.cards) == threshold
    assert ctx.messages == []


async def test_build_context_excludes_different_chat_id(db_session) -> None:
    """Message and card from a different chat_id are excluded."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    target_chat_id = _next_chat_id()
    other_chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    # Insert data in OTHER chat
    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=other_chat_id, ts=ts, text="other chat message"
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    # Query against TARGET chat
    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=target_chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.cards == []
    assert ctx.messages == []
