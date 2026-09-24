"""Phase 7 binding tests — L7a, L7b, C6, I5a, I5b, I5c.

T7-07 / PHASE7_PLAN.md §7. Extends the Phase 11 binding suite (28/28 →
34/34 with these 6 cases).

Cases:
- L7a: forgotten message_version (memory_policy='forgotten') excluded
  from build_digest_context output.
- L7b: forgotten card_source (parent kc archived) excluded.
- C6: synthesize_digest produces citations that all resolve in input.
  Bullets without citation tokens → DigestCitationValidationError.
- I5a: active forget_event in 'pending' state for cited mvid excludes
  it from the context (defense-in-depth even before cascade runs).
- I5b: cascade worker tick on forget_event targeting a cited
  message_version triggers digest redaction.
- I5c: publish-vs-redact race — the `posting` status interlock and
  publisher's step-3 revalidation prevent forgotten content from
  reaching Telegram.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


_chat_counter = itertools.count(start=9700)
_msg_counter = itertools.count(start=970_000)
_user_counter = itertools.count(start=9_700_000_000)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = next(_user_counter)
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_msg(
    db_session,
    *,
    chat_id: int,
    ts: datetime,
    text_value: str = "content",
    memory_policy: str = "normal",
    is_redacted: bool = False,
) -> tuple[int, int]:
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    msg = ChatMessage(
        message_id=next(_msg_counter),
        chat_id=chat_id,
        user_id=uid,
        text=text_value,
        date=ts,
        raw_json={"text": text_value},
        memory_policy=memory_policy,
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    mv = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text_value,
        normalized_text=text_value,
        entities_json={"entities": []},
        content_hash=f"h-{msg.id}",
        is_redacted=is_redacted,
    )
    db_session.add(mv)
    await db_session.flush()
    msg.current_version_id = mv.id
    await db_session.flush()
    return msg.id, mv.id


async def _make_card(db_session, *, mv_ids: list[int], card_status: str = "approved") -> uuid.UUID:
    from bot.db.models import CardSource, KnowledgeCard

    uid = await _make_user(db_session) if card_status == "approved" else None
    card = KnowledgeCard(
        title="binding-test-card",
        body_markdown="card body",
        card_status=card_status,
        approved_by_user_id=uid,
        approved_at=datetime.now(timezone.utc) if card_status == "approved" else None,
    )
    db_session.add(card)
    await db_session.flush()
    for mv_id in mv_ids:
        cs = CardSource(card_id=card.id, message_version_id=mv_id)
        db_session.add(cs)
    await db_session.flush()
    return card.id


# ── L7a: forgotten message_version excluded ──────────────────────────────────


async def test_L7a_forgotten_message_version_excluded_from_digest_context(db_session):
    """Phase 7 binding L7a: a message_version whose chat_message has
    memory_policy='forgotten' (cascade-completed) MUST NOT appear in
    build_digest_context output."""
    from bot.services.digest_context import build_digest_context

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=1)
    we = now
    # Forgotten message
    _cm, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(hours=6),
        text_value="leaked secret",
        memory_policy="forgotten",
    )
    await _make_card(db_session, mv_ids=[mv])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
    )
    assert ctx.cards == []
    assert ctx.messages == []
    # Secret string MUST NOT appear in any DigestContext serialization.
    for c in ctx.cards:
        assert "leaked secret" not in c.title
        assert "leaked secret" not in c.body_markdown
    for m in ctx.messages:
        assert "leaked secret" not in m.text


# ── L7b: forgotten card_source excluded ──────────────────────────────────────


async def test_L7b_card_with_redacted_source_excluded(db_session):
    """Phase 7 binding L7b: when a card_source's linked message_version is
    redacted (is_redacted=TRUE — cascade has nulled the content) the card
    MUST NOT appear in build_digest_context output."""
    from bot.services.digest_context import build_digest_context

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=1)
    we = now
    _cm, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(hours=6),
        text_value="secret card body",
        is_redacted=True,  # cascade-redacted
    )
    await _make_card(db_session, mv_ids=[mv])

    ctx = await build_digest_context(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        source_chat_id=chat_id,
    )
    assert ctx.cards == []


# ── C6: digest citation invariant ────────────────────────────────────────────


async def test_C6_citation_invariant_bullet_without_citation_fails(db_session):
    """Phase 7 binding C6: bullets must each have ≥1 valid citation token.
    Provider misbehavior (bullet without a citation) → DigestCitationValidationError."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import (
        DigestContext,
        DigestContextMessage,
    )
    from bot.services.llm_gateway import (
        DigestCitationValidationError,
        LLMGatewayConfig,
        synthesize_digest,
    )
    from bot.services.llm_providers import ProviderResult
    from decimal import Decimal as _D

    chat_id = _next_chat_id()
    _cm, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    await db_session.flush()
    ctx = DigestContext(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=1),
        window_end=datetime.now(timezone.utc),
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv,
                chat_message_id=_cm,
                author_display="T",
                text="hello",
                ts=datetime.now(timezone.utc),
            )
        ],
    )

    class _Provider:
        async def call_structured(self, **_kwargs):
            # The second item has no citations → strict contract must reject it.
            return ProviderResult(
                answer_text=(
                    '{"publish":true,"layout":"flat","sections":[{"heading":"",'
                    '"items":[{"text":"Обсудили тему",'
                    f'"citations":["[[mv:{mv}]]"]}},'
                    '{"text":"Обсудили другую тему","citations":[]}]}],'
                    '"closing":{"text":"Итог",'
                    f'"citations":["[[mv:{mv}]]"]}}}}'
                ),
                citation_ids=(),
                tokens_in=10,
                tokens_out=10,
                request_id="r",
                raw_latency_ms=1,
            )

    cfg = LLMGatewayConfig(
        provider="openai",
        model="gpt-5.6-sol",
        daily_ceiling_usd=_D("10"),
        monthly_ceiling_usd=_D("100"),
        prompt_template_version="digest-v0.3.0",
    )
    with pytest.raises(DigestCitationValidationError):
        await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=_Provider(),
        )


# ── I5a: pending forget_event excluded ───────────────────────────────────────


async def test_I5a_pending_forget_event_excludes_cited_mvid(db_session):
    """Phase 7 binding I5a: a forget_event in 'pending' state for a cited
    message_version MUST cause build_digest_context to exclude it, even
    when memory_policy is still 'normal' and is_redacted is still FALSE
    (defense-in-depth — cascade hasn't run yet)."""
    from bot.db.models import ForgetEvent
    from bot.services.digest_context import build_digest_context

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(hours=6),
        memory_policy="normal",
        is_redacted=False,
    )
    await _make_card(db_session, mv_ids=[mv])

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
        window_start=now - timedelta(days=1),
        window_end=now,
        source_chat_id=chat_id,
    )
    assert ctx.cards == []
    assert ctx.messages == []


# ── I5b: cascade worker redacts digest with mv citation ──────────────────────


async def test_I5b_cascade_worker_redacts_digest_citing_mvid(db_session):
    """Phase 7 binding I5b: full e2e — insert digest citing a message_version,
    fire forget_event, run cascade worker → digest transitions to
    'redacted' with REDACTED placeholder in body."""
    from bot.db.models import Digest, ForgetEvent
    from bot.services.forget_cascade import run_cascade_worker_once

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(
        db_session,
        chat_id=chat_id,
        ts=now - timedelta(hours=6),
        text_value="cited content",
    )
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown=f"TL;DR.\n\n- One bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()
    did = digest.id

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

    await run_cascade_worker_once(db_session, batch_size=10)

    row = (
        (
            await db_session.execute(
                text("SELECT status, body_markdown FROM digests WHERE id = :id"),
                {"id": did},
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] in ("redacted", "redacted_edit_failed")
    assert "[REDACTED — забыто]" in row["body_markdown"]


# ── I5c: publish revalidation blocks forgotten content ──────────────────────


async def test_I5c_publish_revalidation_blocks_stale_citation(db_session):
    """Phase 7 binding I5c: if a citation source becomes invalid between
    digest synthesis (status='draft') and publish, the publisher's
    step-3 revalidation MUST fail the publish (status→'failed') and NOT
    send the Telegram message."""
    from bot.db.models import Digest, ForgetEvent
    from bot.services.digest_publisher import publish_digest
    from bot.services.digests import DigestConfig

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    cm_id, mv = await _make_msg(db_session, chat_id=chat_id, ts=now - timedelta(hours=6))
    digest = Digest(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        body_markdown=f"TL;DR.\n\n- One bullet [[mv:{mv}]]",
        citations=[{"kind": "message_version", "id": mv, "position": 0}],
        status="draft",
    )
    db_session.add(digest)
    await db_session.flush()

    # Simulate forget happening between synthesis and publish — insert a
    # pending forget_event.
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

    cfg = DigestConfig(destination_chat_id=-1001234567890)
    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    result = await publish_digest(db_session, bot=bot_mock, digest=digest, digest_config=cfg)
    # Publisher revalidation MUST have fired.
    assert result.status == "failed"
    assert (
        "stale" in (result.error_text or "").lower()
        or "citations" in (result.error_text or "").lower()
    )
    # The DIGEST publish to destination MUST NOT have happened. Admin-notify
    # DMs (different chat_id) are allowed. Verify the destination chat was
    # never targeted.
    for call in bot_mock.send_message.call_args_list:
        kwargs = call.kwargs if hasattr(call, "kwargs") else call[1]
        called_chat_id = kwargs.get("chat_id") if kwargs else None
        assert called_chat_id != cfg.destination_chat_id, (
            "publisher must NOT call send_message on destination_chat_id after revalidation failure"
        )
