"""Tests for bot/services/digests.py + synthesize_digest gateway extension.

T7-02 / Phase 7 Wave 1. Covers:
- load_digest_config env-var parsing
- citation token parsing (drop card, drop hallucinated)
- bullet-level citation invariant
- run_digest idempotency
- run_digest empty-window skip
- run_digest cost ceiling (separate Phase 7 bucket)
- run_digest happy path
- run_digest rejects weekly
- synthesize_digest EMPTY_WINDOW handling
- synthesize_digest hallucinated drop + bullet invariant raise
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=8_300_000_000)
_msg_counter = itertools.count(start=830_000)
_chat_counter = itertools.count(start=8300)


def _next_uid() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


# ── helpers ──────────────────────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_uid()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="T",
        last_name=None,
    )
    return uid


async def _make_msg_and_version(
    db_session, *, chat_id: int, ts: datetime, text: str = "hello",
    memory_policy: str = "normal", is_redacted: bool = False
) -> tuple[int, int]:
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
        content_hash=f"hash-{msg_id}",
        is_redacted=is_redacted,
    )
    db_session.add(mv)
    await db_session.flush()
    msg.current_version_id = mv.id
    await db_session.flush()
    return msg.id, mv.id


async def _make_approved_card(db_session, *, mv_ids: list[int]) -> uuid.UUID:
    from bot.db.models import KnowledgeCard, CardSource

    uid = await _make_user(db_session)
    card = KnowledgeCard(
        title="Test Card",
        body_markdown="card body",
        card_status="approved",
        approved_by_user_id=uid,
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    await db_session.flush()
    for mv_id in mv_ids:
        cs = CardSource(card_id=card.id, message_version_id=mv_id)
        db_session.add(cs)
    await db_session.flush()
    return card.id


def _make_gateway_config() -> "LLMGatewayConfig":  # noqa: F821
    from bot.services.llm_gateway import LLMGatewayConfig

    return LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=Decimal("10.00"),
        monthly_ceiling_usd=Decimal("100.00"),
        prompt_template_version="digest-v0.1.0",
    )


def _make_digest_config(**overrides) -> "DigestConfig":  # noqa: F821
    from bot.services.digests import DigestConfig

    defaults = {
        "daily_cost_ceiling_usd": Decimal("1.00"),
        "monthly_cost_ceiling_usd": Decimal("10.00"),
        "source_chat_id": 0,
        "destination_chat_id": None,
        "hour_msk": 9,
        "min_cards_threshold": 3,
        "raw_message_top_n": 15,
        "token_budget_input": 8000,
    }
    defaults.update(overrides)
    return DigestConfig(**defaults)


class _StubProvider:
    """Test provider — returns canned answer_text."""

    def __init__(self, *, answer_text: str, tokens_in: int = 100, tokens_out: int = 50,
                 raise_on_call: Exception | None = None):
        self.answer_text = answer_text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.raise_on_call = raise_on_call
        self.calls = 0

    async def call(self, *, prompt: str, model: str):
        from bot.services.llm_providers import ProviderResult

        self.calls += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return ProviderResult(
            answer_text=self.answer_text,
            citation_ids=(),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id=f"req-{self.calls}",
            raw_latency_ms=10,
        )


# ── unit tests ───────────────────────────────────────────────────────────────


def test_load_digest_config_reads_env_vars(monkeypatch):
    monkeypatch.setenv("DIGEST_DAILY_USD_CEILING", "2.50")
    monkeypatch.setenv("DIGEST_MONTHLY_USD_CEILING", "25.00")
    monkeypatch.setenv("DIGEST_SOURCE_CHAT_ID", "-12345")
    monkeypatch.setenv("DIGEST_DESTINATION_CHAT_ID", "-67890")
    monkeypatch.setenv("DIGEST_HOUR_MSK", "11")
    monkeypatch.setenv("DIGEST_MIN_CARDS_THRESHOLD", "5")
    monkeypatch.setenv("DIGEST_RAW_MESSAGE_TOP_N", "20")
    monkeypatch.setenv("DIGEST_TOKEN_BUDGET_INPUT", "4000")

    from bot.services.digests import load_digest_config

    cfg = load_digest_config()
    assert cfg.daily_cost_ceiling_usd == Decimal("2.50")
    assert cfg.monthly_cost_ceiling_usd == Decimal("25.00")
    assert cfg.source_chat_id == -12345
    assert cfg.destination_chat_id == -67890
    assert cfg.hour_msk == 11
    assert cfg.min_cards_threshold == 5
    assert cfg.raw_message_top_n == 20
    assert cfg.token_budget_input == 4000


def test_load_digest_config_raises_when_src_equals_dst(monkeypatch):
    """F5: src==dst is an echo-loop misconfiguration and must raise ConfigurationError."""
    monkeypatch.setenv("DIGEST_SOURCE_CHAT_ID", "-99999")
    monkeypatch.setenv("DIGEST_DESTINATION_CHAT_ID", "-99999")

    from bot.services.digests import ConfigurationError, load_digest_config

    with pytest.raises(ConfigurationError, match="echo loop"):
        load_digest_config()


def test_parse_citations_drops_card_kind_token():
    from bot.services.llm_gateway import _parse_digest_citations

    body = "TL;DR\n\n- Topic [[card:abc-uuid]] body"
    citations, dropped = _parse_digest_citations(
        body, valid_card_source_ids=frozenset(), valid_mv_ids=frozenset()
    )
    assert citations == []
    assert "[[card:abc-uuid]]" in dropped


def test_parse_citations_drops_hallucinated_mv():
    from bot.services.llm_gateway import _parse_digest_citations

    body = "- Topic [[mv:99999]] something"
    citations, dropped = _parse_digest_citations(
        body, valid_card_source_ids=frozenset(), valid_mv_ids=frozenset({123})
    )
    assert citations == []
    assert "[[mv:99999]]" in dropped


def test_parse_citations_keeps_valid_mv():
    from bot.services.llm_gateway import _parse_digest_citations

    body = "- Topic [[mv:123]]"
    citations, _dropped = _parse_digest_citations(
        body, valid_card_source_ids=frozenset(), valid_mv_ids=frozenset({123})
    )
    assert len(citations) == 1
    assert citations[0]["kind"] == "message_version"
    assert citations[0]["id"] == 123


def test_parse_citations_preserves_duplicate_positions():
    """F3: same source cited in two bullets must produce two citation entries with
    distinct positions — dedup by (kind, id) is a privacy gap for the redactor."""
    from bot.services.llm_gateway import _parse_digest_citations

    # Same mv:123 cited in two different bullets (positions 0 and 1)
    body = (
        "TL;DR text.\n"
        "\n"
        "- Bullet A [[mv:123]] first mention\n"
        "- Bullet B [[mv:123]] second mention\n"
    )
    citations, dropped = _parse_digest_citations(
        body, valid_card_source_ids=frozenset(), valid_mv_ids=frozenset({123})
    )
    assert dropped == [], f"unexpected drops: {dropped}"
    assert len(citations) == 2, (
        f"Expected 2 citations (one per bullet), got {len(citations)}: {citations}"
    )
    positions = {c["position"] for c in citations}
    assert len(positions) == 2, f"Both citations must have distinct positions: {citations}"


def test_validate_every_bullet_has_citation_raises_on_empty():
    from bot.services.llm_gateway import (
        DigestCitationValidationError,
        _validate_every_bullet_has_citation,
    )

    body = "TL;DR\n\n- Bullet without citation"
    with pytest.raises(DigestCitationValidationError):
        _validate_every_bullet_has_citation(body, valid_citation_tokens=set())


def test_validate_every_bullet_has_citation_passes_when_all_covered():
    from bot.services.llm_gateway import _validate_every_bullet_has_citation

    body = "TL;DR\n\n- One [[mv:1]] a\n- Two [[mv:2]] b"
    _validate_every_bullet_has_citation(
        body, valid_citation_tokens={"[[mv:1]]", "[[mv:2]]"}
    )  # no raise


# ── DB-dependent tests ───────────────────────────────────────────────────────


async def test_run_digest_rejects_unknown_type(db_session):
    """T8-02 widening: run_digest now accepts both 'daily' and 'weekly'.
    Other type values are still rejected with a clear ValueError."""
    from bot.services.digests import run_digest

    cfg = _make_gateway_config()
    dcfg = _make_digest_config()
    with pytest.raises(ValueError, match="unsupported digest type"):
        await run_digest(
            db_session,
            type="monthly",  # type: ignore[arg-type]
            window_start=datetime.now(timezone.utc) - timedelta(days=1),
            window_end=datetime.now(timezone.utc),
            ledger_repo=None,
            provider=None,
            config=cfg,
            digest_config=dcfg,
        )


async def test_run_digest_empty_window_skipped(db_session):
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    dcfg = _make_digest_config(source_chat_id=chat_id)
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)
    digest = await run_digest(
        db_session,
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        ledger_repo=None,  # not reached
        provider=None,  # not reached
        config=cfg,
        digest_config=dcfg,
    )
    assert digest.status == "skipped"


async def test_run_digest_idempotency(db_session):
    """Two invocations with same window return the same digest row."""
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    dcfg = _make_digest_config(source_chat_id=chat_id)
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=1)
    we = now

    d1 = await run_digest(
        db_session, type="daily", window_start=ws, window_end=we,
        ledger_repo=None, provider=None, config=cfg, digest_config=dcfg,
    )
    await db_session.flush()
    d2 = await run_digest(
        db_session, type="daily", window_start=ws, window_end=we,
        ledger_repo=None, provider=None, config=cfg, digest_config=dcfg,
    )
    assert d1.id == d2.id
    assert d2.status == "skipped"  # both empty-window


async def test_run_digest_cost_ceiling_separate_bucket(db_session):
    """Phase 7 separate bucket: ledger rows linked to digests must trip the ceiling
    independently of Phase 5 shared bucket."""
    from bot.db.models import Digest, LlmUsageLedger
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    # Pre-insert a digest + a ledger row whose cost == ceiling
    ledger = LlmUsageLedger(
        provider="anthropic",
        model="haiku",
        prompt_hash="x" * 64,
        response_hash="y" * 64,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("1.50"),  # > 1.00 ceiling
        latency_ms=10,
        request_id="r1",
        cache_hit=False,
        error=None,
    )
    db_session.add(ledger)
    await db_session.flush()
    prior_digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=2),
        window_end=datetime.now(timezone.utc) - timedelta(days=1),
        body_markdown="prior",
        citations=[],
        status="draft",
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(prior_digest)
    await db_session.flush()

    dcfg = _make_digest_config(source_chat_id=chat_id, daily_cost_ceiling_usd=Decimal("1.00"))
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)
    digest = await run_digest(
        db_session,
        type="daily",
        window_start=now - timedelta(hours=12),
        window_end=now,
        ledger_repo=None,  # not reached — ceiling pre-check fires first
        provider=None,
        config=cfg,
        digest_config=dcfg,
    )
    assert digest.status == "cost_exceeded"
    assert digest.error_text == "daily digest budget exceeded"


async def test_run_digest_happy_path(db_session):
    """Insert cards, mock provider, assert status='draft' with valid citations."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=1)
    we = now
    # Insert one message + approved card
    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=now - timedelta(hours=6), text="meaningful content"
    )
    card_id = await _make_approved_card(db_session, mv_ids=[mv_id])
    await db_session.flush()
    # Fetch card_source.id for use in canned response
    from sqlalchemy import text as _t
    cs_row = (await db_session.execute(
        _t("SELECT id FROM card_sources WHERE card_id = :cid LIMIT 1"),
        {"cid": str(card_id)},
    )).scalar_one()
    cs_id_str = str(cs_row)

    body = (
        "TL;DR прозой первая фраза. Вторая. Третья.\n"
        "\n"
        f"- Topic about something [[cs:{cs_id_str}]] summary line\n"
    )
    provider = _StubProvider(answer_text=body)

    dcfg = _make_digest_config(
        source_chat_id=chat_id,
        min_cards_threshold=1,  # 1 card is enough — fall back to raw only when 0
        daily_cost_ceiling_usd=Decimal("1.00"),
    )
    cfg = _make_gateway_config()
    digest = await run_digest(
        db_session,
        type="daily",
        window_start=ws,
        window_end=we,
        ledger_repo=LedgerRepo(),
        provider=provider,
        config=cfg,
        digest_config=dcfg,
    )
    assert digest.status == "draft", f"got {digest.status} error={digest.error_text}"
    assert digest.body_markdown == body
    assert len(digest.citations) == 1
    assert digest.citations[0]["kind"] == "card_source"
    assert digest.citations[0]["id"] == cs_id_str
    assert digest.llm_usage_ledger_id is not None
    assert provider.calls == 1


async def test_synthesize_digest_empty_window_with_nonempty_input_fails(db_session):
    """Provider returns EMPTY_WINDOW sentinel against non-empty input → DigestProviderError."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import (
        DigestProviderError,
        synthesize_digest,
    )

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=datetime.now(timezone.utc) - timedelta(hours=6)
    )
    await db_session.flush()
    now = datetime.now(timezone.utc)
    ctx = DigestContext(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv_id,
                chat_message_id=_cm_id,
                author_display="Test",
                text="hello",
                ts=now - timedelta(hours=6),
            )
        ],
    )
    provider = _StubProvider(answer_text="EMPTY_WINDOW")
    cfg = _make_gateway_config()

    with pytest.raises(DigestProviderError):
        await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=provider,
        )


async def test_synthesize_digest_raises_on_bullet_without_citations(db_session):
    """Provider returns body with bullet that has only a hallucinated id →
    after drop, bullet has 0 citations → DigestCitationValidationError."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import (
        DigestCitationValidationError,
        synthesize_digest,
    )

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session, chat_id=chat_id, ts=datetime.now(timezone.utc) - timedelta(hours=6)
    )
    await db_session.flush()
    now = datetime.now(timezone.utc)
    ctx = DigestContext(
        type="daily",
        window_start=now - timedelta(days=1),
        window_end=now,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv_id,
                chat_message_id=_cm_id,
                author_display="Test",
                text="hello",
                ts=now - timedelta(hours=6),
            )
        ],
    )
    # Bullet cites a hallucinated id (99999 is not in input).
    body = (
        "TL;DR text one. Two. Three.\n"
        "\n"
        "- Topic [[mv:99999]] hallucinated\n"
    )
    provider = _StubProvider(answer_text=body)
    cfg = _make_gateway_config()

    with pytest.raises(DigestCitationValidationError):
        await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=provider,
        )
