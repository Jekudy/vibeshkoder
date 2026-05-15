"""Tests for bot/services/digest_context.py — T7-03.

Governance filter checks: card status, window bounds, is_redacted, memory_policy,
chat_id isolation, forget_events exclusion, token-budget trim, threshold
fallback logic.
"""

from __future__ import annotations

import itertools
import uuid
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
) -> uuid.UUID:
    """Create KnowledgeCard (approved) + CardSource rows. Returns card UUID."""
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
        digest_config=DigestConfig(min_cards_threshold=10),  # force raw fallback attempt
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
        digest_config=DigestConfig(min_cards_threshold=10),
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


async def test_build_context_excludes_forgotten_policy(db_session) -> None:
    """memory_policy='forgotten' (cascade-completed) excluded from both paths."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, memory_policy="forgotten"
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(min_cards_threshold=10),
    )

    assert ctx.cards == []
    assert ctx.messages == []


async def test_build_context_excludes_active_forget_event(db_session) -> None:
    """forget_events row in 'pending' state for a message excludes it from
    both cards and raw fallback, even when memory_policy is still 'normal'
    and is_redacted is FALSE (defense-in-depth — cascade hasn't run yet)."""
    from bot.db.models import ForgetEvent
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now
    ts = now - timedelta(hours=12)

    # Normal message with approved card
    cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, memory_policy="normal", is_redacted=False
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    # Insert forget_event in 'pending' state targeting this chat_message
    fe = ForgetEvent(
        target_type="message",
        target_id=str(cm_id),
        actor_user_id=None,
        authorized_by="self",
        tombstone_key=f"message:{chat_id}:{cm_id}",
        policy="forgotten",
        status="pending",
    )
    db_session.add(fe)
    await db_session.flush()

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(min_cards_threshold=10),
    )

    assert ctx.cards == [], "active forget_event must exclude card"
    assert ctx.messages == [], "active forget_event must exclude raw message"


async def test_build_context_token_budget_drops_tail(db_session) -> None:
    """When raw messages exceed token_budget_input, tail is dropped."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now

    # Each message ~1000 chars → ~285 tokens (1000/3.5).
    # With token_budget_input=2000, headroom=1000, we fit ~3 messages.
    big_text = "x" * 1000
    for i in range(10):
        ts = ws + timedelta(hours=i)
        await _make_msg_and_version(
            db_session, chat_id=chat_id, ts=ts, text=big_text
        )

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(
            min_cards_threshold=10,
            raw_message_top_n=10,
            token_budget_input=2000,
        ),
    )

    assert len(ctx.messages) > 0, "at least one message should fit"
    assert len(ctx.messages) < 10, "tail messages must be dropped under budget"
    # Verify chronological — earliest first.
    timestamps = [m.ts for m in ctx.messages]
    assert timestamps == sorted(timestamps), "messages must be chronological"


async def test_build_context_rejects_unknown_type(db_session) -> None:
    """type not in {'daily','weekly'} raises ValueError (Phase 8 widens daily → daily+weekly)."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(hours=24)
    we = now

    with pytest.raises(ValueError, match="daily.*weekly|weekly.*daily|unsupported"):
        await build_digest_context(
            db_session,
            type="monthly",  # type: ignore[arg-type]
            window_start=ws,
            window_end=we,
            source_chat_id=chat_id,
            digest_config=DigestConfig(),
        )


# ── Phase 8 / T8-03 weekly tests ─────────────────────────────────────────────


async def test_build_digest_context_weekly_window_is_7_days(db_session) -> None:
    """type='weekly' preserves caller-supplied 7-day window bounds on the returned context."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now

    ctx = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.type == "weekly"
    assert ctx.window_start == ws
    assert ctx.window_end == we
    assert (ctx.window_end - ctx.window_start) == timedelta(days=7)


async def test_build_digest_context_weekly_larger_token_budget(db_session) -> None:
    """type='weekly' reads weekly_token_budget_input (not the daily one).

    Same raw-message corpus, two calls with the same daily_token=2000 but
    weekly_token=8000: the weekly path fits strictly more messages.
    """
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now

    # 10 messages × ~1000 chars ≈ ~285 tokens each.
    # With token_budget_input=2000 (headroom 1000), fits ~3.
    # With weekly_token_budget_input=8000 (headroom 7000), fits ~10.
    big_text = "x" * 1000
    for i in range(10):
        ts = ws + timedelta(hours=i * 12)
        await _make_msg_and_version(
            db_session, chat_id=chat_id, ts=ts, text=big_text
        )

    cfg = DigestConfig(
        min_cards_threshold=10,  # daily threshold (forces raw fallback)
        raw_message_top_n=10,
        token_budget_input=2000,
        weekly_min_cards_threshold=10,  # forces raw fallback for weekly path
        weekly_raw_message_top_n=10,
        weekly_token_budget_input=8000,
    )

    ctx_weekly = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=cfg,
    )

    ctx_daily = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=cfg,
    )

    assert len(ctx_weekly.messages) > len(ctx_daily.messages), (
        f"weekly budget should fit more messages: "
        f"weekly={len(ctx_weekly.messages)} daily={len(ctx_daily.messages)}"
    )


async def test_build_digest_context_weekly_min_cards_threshold_default_8(
    db_session,
) -> None:
    """When fewer than weekly_min_cards_threshold (default 8) cards exist in the
    weekly window, raw fallback kicks in for type='weekly'."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now
    ts = now - timedelta(days=1)

    # Create 7 approved cards (< default weekly threshold of 8). And one
    # additional raw message so the fallback has something to surface.
    for i in range(7):
        _cm_id, mv_id = await _make_msg_and_version(
            db_session, chat_id=chat_id, ts=ts, text=f"card source {i}"
        )
        await _make_approved_card(
            db_session, mv_ids=[mv_id], title=f"Card {i}", body=f"body {i}"
        )
    await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, text="raw fallback message"
    )

    ctx = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),  # weekly_min_cards_threshold default 8
    )

    assert len(ctx.cards) == 7
    # 7 < 8, fallback fires → raw message included.
    assert any(m.text == "raw fallback message" for m in ctx.messages)


async def test_build_digest_context_weekly_governance_filter_forget_event(
    db_session,
) -> None:
    """Weekly path: active forget_event excludes target from both cards and raw."""
    from bot.db.models import ForgetEvent
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now
    ts = now - timedelta(days=2)

    cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, memory_policy="normal", is_redacted=False
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    fe = ForgetEvent(
        target_type="message",
        target_id=str(cm_id),
        actor_user_id=None,
        authorized_by="self",
        tombstone_key=f"message:{chat_id}:{cm_id}",
        policy="forgotten",
        status="pending",
    )
    db_session.add(fe)
    await db_session.flush()

    ctx = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(weekly_min_cards_threshold=10),
    )

    assert ctx.cards == [], "weekly: active forget_event must exclude card"
    assert ctx.messages == [], "weekly: active forget_event must exclude raw message"


async def test_build_digest_context_weekly_excludes_redacted(db_session) -> None:
    """Weekly path: message_version.is_redacted=TRUE → excluded from cards and raw."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now
    ts = now - timedelta(days=2)

    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=ts, is_redacted=True
    )
    await _make_approved_card(db_session, mv_ids=[mv_id])

    ctx = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(weekly_min_cards_threshold=10),
    )

    assert ctx.cards == []
    assert ctx.messages == []


async def test_build_digest_context_weekly_excludes_non_normal_memory_policy(
    db_session,
) -> None:
    """Weekly path: chat_messages.memory_policy != 'normal' rows excluded.

    Covers offrecord, nomem, forgotten — all should be filtered out of both
    the cards source-join and the raw fallback.
    """
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now
    ts = now - timedelta(days=2)

    for policy in ("offrecord", "nomem", "forgotten"):
        _cm_id, mv_id = await _make_msg_and_version(
            db_session,
            chat_id=chat_id,
            ts=ts,
            text=f"policy={policy}",
            memory_policy=policy,
        )
        await _make_approved_card(
            db_session, mv_ids=[mv_id], title=f"Card-{policy}", body=f"body-{policy}"
        )

    ctx = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(weekly_min_cards_threshold=10),
    )

    assert ctx.cards == []
    assert ctx.messages == []


async def test_build_digest_context_weekly_cards_limit_100(db_session) -> None:
    """Weekly cards SQL uses LIMIT 100 (vs daily 30).

    Create 31 approved cards in the weekly window; daily would cap at 30 by
    the SQL LIMIT, weekly is bounded by 100 so all 31 should return.
    """
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now
    ts = now - timedelta(days=2)

    n = 31
    for i in range(n):
        _cm_id, mv_id = await _make_msg_and_version(
            db_session, chat_id=chat_id, ts=ts, text=f"card src {i}"
        )
        # Stagger approved_at so ORDER BY approved_at DESC is deterministic.
        await _make_approved_card(
            db_session,
            mv_ids=[mv_id],
            title=f"Card {i}",
            body=f"body {i}",
            approved_at=now - timedelta(seconds=i),
        )

    ctx_weekly = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    ctx_daily = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert len(ctx_weekly.cards) == n, f"weekly LIMIT 100 should return all {n} cards"
    assert len(ctx_daily.cards) == 30, "daily LIMIT 30 caps at 30"


async def test_build_digest_context_weekly_empty_window(db_session) -> None:
    """Weekly path with zero candidates → empty cards + empty messages."""
    from bot.services.digest_context import build_digest_context, DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now

    ctx = await build_digest_context(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
        digest_config=DigestConfig(),
    )

    assert ctx.cards == []
    assert ctx.messages == []
    assert ctx.type == "weekly"
    assert ctx.source_chat_id == chat_id
