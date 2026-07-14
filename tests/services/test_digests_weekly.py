"""Tests for Phase 8 / T8-02 — weekly digest path.

Covers:
- `bot/services/llm_prompts/digest_weekly_v0_1_0.py` — prompt module shape
  (PROMPT_VERSION, SYSTEM_PROMPT, SECTION_NAME_ALLOWLIST, build_user_prompt).
- `bot/services/digests.py`:
  - `DigestConfig` weekly fields (defaults per §5.B).
  - `load_digest_config()` reads weekly env vars.
  - `_cost_ceiling_breached(type='weekly')` SQL filter independence.
  - `run_digest(type='weekly', ...)` accepts weekly, ends at `status='draft'`,
    weekly cost-ceiling pre-check fires, idempotency.
- `bot/services/llm_gateway.py`:
  - `synthesize_digest(type='weekly', ...)` imports weekly prompt module.
  - `_extract_sections` parses `## Раздел: <name>` headers + bullets.
  - Section header tolerance: bullet-index parser skips headers (no false bullet).
  - Section title allowlist soft warning (M1).
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=8_400_000_000)
_msg_counter = itertools.count(start=840_000)
_chat_counter = itertools.count(start=8400)


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
    db_session,
    *,
    chat_id: int,
    ts: datetime,
    text: str = "hello",
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
        memory_policy="normal",
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
        is_redacted=False,
    )
    db_session.add(mv)
    await db_session.flush()
    msg.current_version_id = mv.id
    await db_session.flush()
    return msg.id, mv.id


def _make_gateway_config():
    from bot.services.llm_gateway import LLMGatewayConfig

    return LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=Decimal("10.00"),
        monthly_ceiling_usd=Decimal("100.00"),
        prompt_template_version="digest-weekly-v0.1.0",
    )


def _make_digest_config(**overrides):
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
        "weekly_cost_ceiling_usd": Decimal("5.00"),
        "weekly_monthly_cost_ceiling_usd": Decimal("20.00"),
        "weekly_token_budget_input": 24000,
        "weekly_min_cards_threshold": 8,
        "weekly_raw_message_top_n": 60,
    }
    defaults.update(overrides)
    return DigestConfig(**defaults)


# ── 1. Prompt module shape ───────────────────────────────────────────────────


def test_weekly_prompt_module_exports_required_symbols():
    """digest_weekly_v0_1_0 mirrors digest_v0_1_0 shape: PROMPT_VERSION,
    SYSTEM_PROMPT, build_user_prompt; adds SECTION_NAME_ALLOWLIST (M1)."""
    from bot.services.llm_prompts import digest_weekly_v0_1_0 as mod

    assert mod.PROMPT_VERSION == "digest-weekly-v0.1.0"
    assert isinstance(mod.SYSTEM_PROMPT, str) and "EMPTY_WINDOW" in mod.SYSTEM_PROMPT
    assert isinstance(mod.SECTION_NAME_ALLOWLIST, frozenset)
    assert mod.SECTION_NAME_ALLOWLIST == frozenset(
        {
            "Объявления",
            "Обсуждения",
            "Знания и ресурсы",
            "Встречи и события",
            "Прочее",
        }
    )
    assert callable(mod.build_user_prompt)


def test_weekly_prompt_system_block_demands_section_format_and_citations():
    """§5.F SYSTEM contract: section header format `## Раздел: …`, EVERY bullet
    must contain at least one citation token, Russian neutral framing."""
    from bot.services.llm_prompts.digest_weekly_v0_1_0 import SYSTEM_PROMPT

    assert "## Раздел:" in SYSTEM_PROMPT
    assert "[[cs:UUID]]" in SYSTEM_PROMPT
    assert "[[mv:INT]]" in SYSTEM_PROMPT
    assert "EVERY bullet" in SYSTEM_PROMPT
    # Russian community + weekly framing
    assert "WEEKLY" in SYSTEM_PROMPT


def test_weekly_prompt_user_template_emits_window_and_cards_and_messages():
    """build_user_prompt returns a single-string layout: window line, cards
    section, messages section. Mirrors digest_v0_1_0 contract."""
    from dataclasses import dataclass

    from bot.services.llm_prompts.digest_weekly_v0_1_0 import build_user_prompt

    @dataclass(frozen=True)
    class _Card:
        title: str
        body_markdown: str
        card_source_ids: list

    @dataclass(frozen=True)
    class _Msg:
        message_version_id: int
        author_display: str
        text: str
        ts: datetime

    cards = [_Card(title="C1", body_markdown="body", card_source_ids=["uuid-a"])]
    msgs = [
        _Msg(
            message_version_id=12345,
            author_display="Alice",
            text="hello world",
            ts=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )
    ]
    out = build_user_prompt(
        window_start_msk="2026-05-04T00:00:00",
        window_end_msk="2026-05-11T00:00:00",
        cards=cards,
        messages=msgs,
    )
    assert "Window: 2026-05-04T00:00:00 .. 2026-05-11T00:00:00" in out
    assert "Cards (1)" in out
    assert "uuid-a" in out
    assert "Messages (1)" in out
    assert "[mv:12345]" in out
    assert "hello world" in out


# ── 2. DigestConfig + load_digest_config weekly fields ───────────────────────


def test_digest_config_has_weekly_fields_with_defaults():
    """§5.B DigestConfig extension — weekly defaults independent of daily."""
    from bot.services.digests import DigestConfig

    cfg = DigestConfig()
    # Phase 7 (unchanged)
    assert cfg.daily_cost_ceiling_usd == Decimal("1.00")
    # Phase 8 additions
    assert cfg.weekly_cost_ceiling_usd == Decimal("5.00")
    assert cfg.weekly_monthly_cost_ceiling_usd == Decimal("20.00")
    assert cfg.weekly_token_budget_input == 24000
    assert cfg.weekly_min_cards_threshold == 8
    assert cfg.weekly_raw_message_top_n == 60


def test_load_digest_config_reads_weekly_env_vars(monkeypatch):
    """§5.B load_digest_config reads DIGEST_WEEKLY_* env vars."""
    monkeypatch.setenv("DIGEST_WEEKLY_USD_CEILING", "7.00")
    monkeypatch.setenv("DIGEST_WEEKLY_MONTHLY_USD_CEILING", "30.00")
    monkeypatch.setenv("DIGEST_WEEKLY_TOKEN_BUDGET", "12000")
    monkeypatch.setenv("DIGEST_WEEKLY_MIN_CARDS_THRESHOLD", "10")
    monkeypatch.setenv("DIGEST_WEEKLY_RAW_MESSAGE_TOP_N", "40")

    from bot.services.digests import load_digest_config

    cfg = load_digest_config()
    assert cfg.weekly_cost_ceiling_usd == Decimal("7.00")
    assert cfg.weekly_monthly_cost_ceiling_usd == Decimal("30.00")
    assert cfg.weekly_token_budget_input == 12000
    assert cfg.weekly_min_cards_threshold == 10
    assert cfg.weekly_raw_message_top_n == 40


# ── 3. _cost_ceiling_breached type filter ────────────────────────────────────


async def test_cost_ceiling_breached_weekly_independent_of_daily(db_session):
    """H6 / Q7: a daily-type digest ledger row does NOT trip the weekly bucket
    and vice-versa. SQL `WHERE d.type=:type` enforces separation."""
    from bot.db.models import Digest, LlmUsageLedger
    from bot.services.digests import _cost_ceiling_breached

    # Insert a *daily* digest pinned to a $5.00 ledger entry. The daily ceiling
    # is $1.00 (will trip); the weekly ceiling is $5.00 (must NOT trip — no
    # weekly row present yet).
    ledger = LlmUsageLedger(
        provider="anthropic",
        model="haiku",
        prompt_hash="d" * 64,
        response_hash="r" * 64,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("5.00"),
        latency_ms=10,
        request_id="r1",
        cache_hit=False,
        error=None,
    )
    db_session.add(ledger)
    await db_session.flush()
    daily_digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=2),
        window_end=datetime.now(timezone.utc) - timedelta(days=1),
        body_markdown="prior daily",
        citations=[],
        status="draft",
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(daily_digest)
    await db_session.flush()

    dcfg = _make_digest_config(
        daily_cost_ceiling_usd=Decimal("1.00"),
        weekly_cost_ceiling_usd=Decimal("5.00"),
    )
    # Daily bucket trips (5.00 >= 1.00 daily ceiling).
    assert await _cost_ceiling_breached(db_session, digest_config=dcfg, type="daily") is True
    # Weekly bucket does NOT trip — no weekly-type row present.
    assert await _cost_ceiling_breached(db_session, digest_config=dcfg, type="weekly") is False


async def test_cost_ceiling_breached_weekly_trips_on_weekly_ledger_row(db_session):
    """A weekly-type digest+ledger row trips the weekly ceiling."""
    from bot.db.models import Digest, LlmUsageLedger
    from bot.services.digests import _cost_ceiling_breached

    ledger = LlmUsageLedger(
        provider="anthropic",
        model="haiku",
        prompt_hash="w" * 64,
        response_hash="r" * 64,
        tokens_in=2000,
        tokens_out=500,
        cost_usd=Decimal("6.00"),  # > 5.00 weekly ceiling
        latency_ms=20,
        request_id="r2",
        cache_hit=False,
        error=None,
    )
    db_session.add(ledger)
    await db_session.flush()
    weekly_digest = Digest(
        type="weekly",
        window_start=datetime.now(timezone.utc) - timedelta(days=8),
        window_end=datetime.now(timezone.utc) - timedelta(days=1),
        body_markdown="prior weekly",
        citations=[],
        status="draft",
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(weekly_digest)
    await db_session.flush()

    dcfg = _make_digest_config(
        daily_cost_ceiling_usd=Decimal("1.00"),
        weekly_cost_ceiling_usd=Decimal("5.00"),
    )
    # Weekly bucket trips.
    assert await _cost_ceiling_breached(db_session, digest_config=dcfg, type="weekly") is True
    # Daily bucket does NOT trip — no daily-type row.
    assert await _cost_ceiling_breached(db_session, digest_config=dcfg, type="daily") is False


async def test_cost_ceiling_breached_default_type_is_daily(db_session):
    """Back-compat: existing Phase 7 callsite (no `type=` kwarg) defaults to
    `type='daily'` and behaves identically to pre-T8-02 implementation."""
    from bot.db.models import Digest, LlmUsageLedger
    from bot.services.digests import _cost_ceiling_breached

    ledger = LlmUsageLedger(
        provider="anthropic",
        model="haiku",
        prompt_hash="x" * 64,
        response_hash="r" * 64,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("1.50"),
        latency_ms=10,
        request_id="r",
        cache_hit=False,
        error=None,
    )
    db_session.add(ledger)
    await db_session.flush()
    daily_digest = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=2),
        window_end=datetime.now(timezone.utc) - timedelta(days=1),
        body_markdown="prior",
        citations=[],
        status="draft",
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(daily_digest)
    await db_session.flush()

    dcfg = _make_digest_config(daily_cost_ceiling_usd=Decimal("1.00"))
    # No `type=` kwarg — default daily.
    assert await _cost_ceiling_breached(db_session, digest_config=dcfg) is True


# ── 4. run_digest type='weekly' path ─────────────────────────────────────────


async def test_run_digest_accepts_weekly_type_and_calls_context_with_weekly(
    db_session, monkeypatch
):
    """T8-02 widening: run_digest accepts type='weekly' (no longer raises
    `ValueError(\"Phase 7 only supports type='daily'\")`) and dispatches
    `build_digest_context` with `type='weekly'` per §5.B step 5.

    `build_digest_context` weekly handling lands in T8-03 (parallel sprint),
    so we mock it here to verify the call-chain wiring is correct.
    """
    from bot.services.digest_context import DigestContext
    from bot.services import digests as digests_module
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    dcfg = _make_digest_config(source_chat_id=chat_id)
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=8)
    we = now - timedelta(days=1)

    captured: dict = {}

    async def _stub_build_context(session, **kwargs):
        captured.update(kwargs)
        # Return empty context to short-circuit downstream.
        return DigestContext(
            type="daily",  # T8-03 widens to 'weekly'
            window_start=kwargs["window_start"],
            window_end=kwargs["window_end"],
            source_chat_id=kwargs["source_chat_id"],
            cards=[],
            messages=[],
        )

    monkeypatch.setattr(digests_module, "build_digest_context", _stub_build_context)

    digest = await run_digest(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        ledger_repo=None,
        provider=None,
        config=cfg,
        digest_config=dcfg,
    )
    # build_digest_context was called with type='weekly'.
    assert captured.get("type") == "weekly", f"got kwargs={captured}"
    assert captured["window_start"] == ws
    assert captured["window_end"] == we
    # Row exists with type='weekly' and remains publishable without an LLM call.
    assert digest.type == "weekly"
    assert digest.status == "draft"
    assert digest.body_markdown == "За прошедшую неделю новых обсуждений не было."


async def test_run_digest_weekly_cost_ceiling_fires_before_llm(db_session):
    """T8-02 AC4 / AC6: weekly bucket pre-check fires using `type='weekly'`
    filter. No LLM call. Creates digests + digest_runs rows with
    `status='cost_exceeded'` and the weekly-specific error_text."""
    from bot.db.models import Digest, DigestRun, LlmUsageLedger
    from bot.services.digests import run_digest
    from sqlalchemy import select

    chat_id = _next_chat_id()
    # Pre-insert weekly ledger row exceeding weekly ceiling.
    ledger = LlmUsageLedger(
        provider="anthropic",
        model="haiku",
        prompt_hash="w" * 64,
        response_hash="r" * 64,
        tokens_in=2000,
        tokens_out=500,
        cost_usd=Decimal("6.00"),  # > 5.00 weekly ceiling
        latency_ms=20,
        request_id="r0",
        cache_hit=False,
        error=None,
    )
    db_session.add(ledger)
    await db_session.flush()
    prior = Digest(
        type="weekly",
        window_start=datetime.now(timezone.utc) - timedelta(days=15),
        window_end=datetime.now(timezone.utc) - timedelta(days=8),
        body_markdown="prior weekly",
        citations=[],
        status="draft",
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(prior)
    await db_session.flush()

    dcfg = _make_digest_config(
        source_chat_id=chat_id,
        weekly_cost_ceiling_usd=Decimal("5.00"),
    )
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)
    digest = await run_digest(
        db_session,
        type="weekly",
        window_start=now - timedelta(days=7),
        window_end=now,
        ledger_repo=None,  # not reached
        provider=None,
        config=cfg,
        digest_config=dcfg,
    )
    assert digest.type == "weekly"
    assert digest.status == "cost_exceeded"
    assert digest.error_text == "weekly digest budget exceeded"
    # digest_runs row also created.
    run_row = (
        await db_session.execute(select(DigestRun).where(DigestRun.digest_id == digest.id))
    ).scalar_one()
    assert run_row.status == "cost_exceeded"
    assert run_row.error_text == "weekly digest budget exceeded"


async def test_run_digest_weekly_idempotency(db_session, monkeypatch):
    """T8-02 acceptance: re-run for same (type='weekly', ws, we) returns the
    same digest row without re-invoking the gateway. Mirrors Phase 7 daily
    idempotency. `build_digest_context` is mocked (T8-03 territory)."""
    from bot.services.digest_context import DigestContext
    from bot.services import digests as digests_module
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    dcfg = _make_digest_config(source_chat_id=chat_id)
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=7)
    we = now

    async def _stub_build_context(session, **kwargs):
        return DigestContext(
            type="daily",
            window_start=kwargs["window_start"],
            window_end=kwargs["window_end"],
            source_chat_id=kwargs["source_chat_id"],
            cards=[],
            messages=[],
        )

    monkeypatch.setattr(digests_module, "build_digest_context", _stub_build_context)

    d1 = await run_digest(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        ledger_repo=None,
        provider=None,
        config=cfg,
        digest_config=dcfg,
    )
    await db_session.flush()
    d2 = await run_digest(
        db_session,
        type="weekly",
        window_start=ws,
        window_end=we,
        ledger_repo=None,
        provider=None,
        config=cfg,
        digest_config=dcfg,
    )
    assert d1.id == d2.id
    assert d2.type == "weekly"
    assert d2.status == "draft"


async def test_run_digest_weekly_daily_cost_buckets_are_isolated(db_session, monkeypatch):
    """A weekly run does NOT trip the daily bucket and vice versa (Q7).

    `build_digest_context` is mocked (T8-03 territory) — the assertion is
    about the cost-bucket pre-check, not context-build behaviour.
    """
    from bot.db.models import Digest, LlmUsageLedger
    from bot.services.digest_context import DigestContext
    from bot.services import digests as digests_module
    from bot.services.digests import run_digest

    chat_id = _next_chat_id()
    # Pre-insert a DAILY ledger row > daily ceiling.
    ledger = LlmUsageLedger(
        provider="anthropic",
        model="haiku",
        prompt_hash="d" * 64,
        response_hash="r" * 64,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("2.00"),  # > $1.00 daily ceiling
        latency_ms=10,
        request_id="r",
        cache_hit=False,
        error=None,
    )
    db_session.add(ledger)
    await db_session.flush()
    daily_row = Digest(
        type="daily",
        window_start=datetime.now(timezone.utc) - timedelta(days=2),
        window_end=datetime.now(timezone.utc) - timedelta(days=1),
        body_markdown="prior daily",
        citations=[],
        status="draft",
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(daily_row)
    await db_session.flush()

    dcfg = _make_digest_config(
        source_chat_id=chat_id,
        daily_cost_ceiling_usd=Decimal("1.00"),
        weekly_cost_ceiling_usd=Decimal("5.00"),
    )
    cfg = _make_gateway_config()
    now = datetime.now(timezone.utc)

    async def _stub_build_context(session, **kwargs):
        return DigestContext(
            type="daily",
            window_start=kwargs["window_start"],
            window_end=kwargs["window_end"],
            source_chat_id=kwargs["source_chat_id"],
            cards=[],
            messages=[],
        )

    monkeypatch.setattr(digests_module, "build_digest_context", _stub_build_context)

    # Weekly call: daily ledger row does NOT trip weekly bucket — should not
    # be cost_exceeded.
    weekly = await run_digest(
        db_session,
        type="weekly",
        window_start=now - timedelta(days=7),
        window_end=now,
        ledger_repo=None,
        provider=None,
        config=cfg,
        digest_config=dcfg,
    )
    assert weekly.status == "draft", (
        f"weekly cost ceiling falsely tripped by daily ledger; got {weekly.status!r}, "
        f"error_text={weekly.error_text!r}"
    )


# ── 5. synthesize_digest type='weekly' routing ───────────────────────────────


async def test_synthesize_digest_type_weekly_uses_weekly_prompt(db_session):
    """§5.B step 7 / §5.F: synthesize_digest(type='weekly') imports the weekly
    prompt module (PROMPT_VERSION='digest-weekly-v0.1.0'); body composition
    uses the weekly SYSTEM_PROMPT."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import synthesize_digest

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(days=3),
        text="weekly recap material",
    )
    await db_session.flush()

    # Build a weekly-shaped DigestContext (the dataclass widens type → 'weekly'
    # in T8-03; for T8-02 we test via the existing daily-typed DigestContext
    # — the gateway routing reads the explicit `type=` kwarg, not the context's
    # internal type field).
    now = datetime.now(timezone.utc)
    ctx = DigestContext(
        type="daily",  # T8-03 widens; T8-02 routes via explicit kwarg
        window_start=now - timedelta(days=7),
        window_end=now,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv_id,
                chat_message_id=_cm_id,
                author_display="Alice",
                text="weekly recap material",
                ts=now - timedelta(days=3),
            )
        ],
    )

    # Capture the prompt the provider receives so we can verify it uses the
    # weekly SYSTEM_PROMPT block.
    seen_prompts: list[str] = []

    class _CapturingProvider:
        async def call(self, *, prompt: str, model: str):
            from bot.services.llm_providers import ProviderResult

            seen_prompts.append(prompt)
            body = (
                "TL;DR недельный итог в трёх предложениях. "
                "Вторая сводная фраза. Третья замыкающая.\n"
                "\n"
                "## Раздел: Обсуждения\n"
                f"- Тема обсуждения [[mv:{mv_id}]] краткое резюме\n"
            )
            return ProviderResult(
                answer_text=body,
                citation_ids=(),
                tokens_in=200,
                tokens_out=100,
                request_id="rw",
                raw_latency_ms=10,
            )

    cfg = _make_gateway_config()
    result = await synthesize_digest(
        db_session,
        context=ctx,
        config=cfg,
        ledger_repo=LedgerRepo(),
        provider=_CapturingProvider(),
        type="weekly",
    )
    # Weekly prompt module's SYSTEM block must have been used.
    assert seen_prompts, "provider was not called"
    assert "WEEKLY" in seen_prompts[0], (
        "synthesize_digest(type='weekly') must use the weekly prompt module"
    )
    # Citations parsed correctly with section header present (header not
    # mistaken for a bullet).
    assert len(result.citations) == 1
    assert result.citations[0]["kind"] == "message_version"
    assert result.citations[0]["id"] == mv_id
    # First bullet is at position 0 — section header `## Раздел: …` did NOT
    # count as a bullet (existing _bullet_index_at_offset already filters
    # by `- `/`• ` line-start; verified for weekly section headers).
    assert result.citations[0]["position"] == 0


async def test_synthesize_digest_type_daily_keeps_daily_prompt(db_session):
    """Back-compat: synthesize_digest with type='daily' (default) still imports
    the daily prompt module. Existing Phase 7 daily behaviour byte-for-byte."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import synthesize_digest

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(hours=6),
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
                author_display="Bob",
                text="hello",
                ts=now - timedelta(hours=6),
            )
        ],
    )

    seen_prompts: list[str] = []

    class _CapturingProvider:
        async def call(self, *, prompt: str, model: str):
            from bot.services.llm_providers import ProviderResult

            seen_prompts.append(prompt)
            body = f"TL;DR одна фраза. Вторая. Третья.\n\n- Topic [[mv:{mv_id}]] summary\n"
            return ProviderResult(
                answer_text=body,
                citation_ids=(),
                tokens_in=100,
                tokens_out=40,
                request_id="rd",
                raw_latency_ms=10,
            )

    cfg = _make_gateway_config()
    # Default type='daily' (no kwarg).
    await synthesize_digest(
        db_session,
        context=ctx,
        config=cfg,
        ledger_repo=LedgerRepo(),
        provider=_CapturingProvider(),
    )
    # Daily SYSTEM block uses "daily digest" wording, NOT "WEEKLY".
    assert "WEEKLY" not in seen_prompts[0]
    assert "daily digest" in seen_prompts[0]


# ── 6. Section parsing helpers + allowlist warning (M1) ──────────────────────


def test_extract_sections_returns_title_and_bullets():
    """§5.F `_extract_sections(body_markdown) -> list[tuple[str, list[str]]]`."""
    from bot.services.llm_gateway import _extract_sections

    body = (
        "TL;DR прозой первая фраза. Вторая. Третья.\n"
        "\n"
        "## Раздел: Объявления\n"
        "- Анонс собрания [[mv:1]] coffee Friday\n"
        "- Второй пункт [[cs:abc-uuid]] объявление\n"
        "\n"
        "## Раздел: Обсуждения\n"
        "- Дискуссия про X [[mv:2]] резюме\n"
    )
    sections = _extract_sections(body)
    assert len(sections) == 2
    assert sections[0][0] == "Объявления"
    assert sections[0][1] == [
        "- Анонс собрания [[mv:1]] coffee Friday",
        "- Второй пункт [[cs:abc-uuid]] объявление",
    ]
    assert sections[1][0] == "Обсуждения"
    assert sections[1][1] == ["- Дискуссия про X [[mv:2]] резюме"]


def test_extract_sections_empty_body_returns_empty_list():
    from bot.services.llm_gateway import _extract_sections

    assert _extract_sections("just some prose without headers") == []


def test_extract_sections_handles_no_bullets_under_header():
    """Header with no bullets returns the empty list under that title."""
    from bot.services.llm_gateway import _extract_sections

    body = "## Раздел: Прочее\n\n## Раздел: Обсуждения\n- a [[mv:1]]\n"
    sections = _extract_sections(body)
    titles = [t for t, _ in sections]
    assert titles == ["Прочее", "Обсуждения"]
    assert sections[0][1] == []
    assert sections[1][1] == ["- a [[mv:1]]"]


async def test_synthesize_digest_weekly_warns_on_off_allowlist_section(db_session, caplog):
    """M1 soft contract: a section title outside SECTION_NAME_ALLOWLIST logs a
    structured warning but does NOT raise."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import synthesize_digest

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(days=3),
    )
    await db_session.flush()
    now = datetime.now(timezone.utc)
    ctx = DigestContext(
        type="daily",
        window_start=now - timedelta(days=7),
        window_end=now,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv_id,
                chat_message_id=_cm_id,
                author_display="A",
                text="t",
                ts=now - timedelta(days=3),
            )
        ],
    )

    body = (
        "TL;DR a. b. c.\n"
        "\n"
        "## Раздел: Совет\n"  # NOT in allowlist
        f"- topic [[mv:{mv_id}]] summary\n"
    )

    class _Provider:
        async def call(self, *, prompt: str, model: str):
            from bot.services.llm_providers import ProviderResult

            return ProviderResult(
                answer_text=body,
                citation_ids=(),
                tokens_in=100,
                tokens_out=40,
                request_id="rs",
                raw_latency_ms=5,
            )

    cfg = _make_gateway_config()
    with caplog.at_level(logging.WARNING, logger="bot.services.llm_gateway"):
        result = await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=_Provider(),
            type="weekly",
        )
    # No raise — soft contract.
    assert len(result.citations) == 1
    # Warning emitted, naming the off-allowlist title.
    warn_records = [
        r for r in caplog.records if r.levelno == logging.WARNING and "Совет" in r.getMessage()
    ]
    assert len(warn_records) == 1, (
        f"expected exactly one allowlist warning naming 'Совет'; got "
        f"{[r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]}"
    )


async def test_synthesize_digest_weekly_no_warning_when_all_titles_in_allowlist(db_session, caplog):
    """No allowlist-warning emitted when every section title is allowlisted."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import synthesize_digest

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(days=3),
    )
    await db_session.flush()
    now = datetime.now(timezone.utc)
    ctx = DigestContext(
        type="daily",
        window_start=now - timedelta(days=7),
        window_end=now,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv_id,
                chat_message_id=_cm_id,
                author_display="A",
                text="t",
                ts=now - timedelta(days=3),
            )
        ],
    )

    body = f"TL;DR a. b. c.\n\n## Раздел: Обсуждения\n- topic [[mv:{mv_id}]] summary\n"

    class _Provider:
        async def call(self, *, prompt: str, model: str):
            from bot.services.llm_providers import ProviderResult

            return ProviderResult(
                answer_text=body,
                citation_ids=(),
                tokens_in=100,
                tokens_out=40,
                request_id="rs2",
                raw_latency_ms=5,
            )

    cfg = _make_gateway_config()
    with caplog.at_level(logging.WARNING, logger="bot.services.llm_gateway"):
        await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=_Provider(),
            type="weekly",
        )
    # No allowlist-related warning. ("Совет" / "off-allowlist" should not appear.)
    off_warns = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and (
            "off-allowlist" in r.getMessage().lower()
            or "not in allowlist" in r.getMessage().lower()
        )
    ]
    assert off_warns == []


# ── 7. Defense-in-depth: section header is not counted as a bullet ───────────


def test_bullet_index_at_offset_skips_section_header():
    """§5.F: existing `_bullet_index_at_offset` MUST count only `- ` / `• `
    line-starts as bullets — section header `## Раздел: …` must NOT increment
    the bullet index. Regression guard against future tokenizer drift."""
    from bot.services.llm_gateway import _bullet_index_at_offset

    body = (
        "TL;DR text.\n"
        "\n"
        "## Раздел: Обсуждения\n"  # NOT a bullet
        "- First bullet [[mv:1]] X\n"
        "- Second bullet [[mv:2]] Y\n"
    )
    # offset inside the first real bullet → index 0
    first_idx = body.index("First bullet")
    assert _bullet_index_at_offset(body, first_idx) == 0
    # offset inside the second real bullet → index 1
    second_idx = body.index("Second bullet")
    assert _bullet_index_at_offset(body, second_idx) == 1


def test_validate_every_bullet_has_citation_passes_with_section_headers():
    """§5.F: section headers don't fire bullet-level invariant — only `- ` lines do."""
    from bot.services.llm_gateway import _validate_every_bullet_has_citation

    body = (
        "TL;DR.\n"
        "\n"
        "## Раздел: Объявления\n"
        "- Topic [[mv:1]] a\n"
        "\n"
        "## Раздел: Обсуждения\n"
        "- Topic2 [[mv:2]] b\n"
    )
    _validate_every_bullet_has_citation(
        body, valid_citation_tokens={"[[mv:1]]", "[[mv:2]]"}
    )  # no raise


# ── 8. FHR HIGH-1: provider exception under type='weekly' updates ledger ─────
# ──    (NOT raises TypeError because `type` kwarg shadows builtin)            ─


async def test_synthesize_digest_provider_exception_updates_ledger_weekly(
    db_session,
):
    """FHR HIGH-1: ``synthesize_digest(type='weekly')`` whose provider raises
    must update the placeholder ledger row with the provider error class name
    and re-raise the provider exception — NOT ``TypeError: 'str' object is
    not callable`` from ``type(exc).__name__`` where ``type`` is the kwarg.

    The bug surfaced at lines ~1633 (kwarg ``type: Literal['daily','weekly']``
    shadows the builtin) + ~1745 (``error=f"{type(exc).__name__}"`` in the
    except branch). After fix, the exception path computes the class name
    via ``exc.__class__.__name__`` (no shadowing concern).
    """
    from sqlalchemy import text as _text

    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import synthesize_digest

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(days=3),
    )
    await db_session.flush()

    now = datetime.now(timezone.utc)
    ctx = DigestContext(
        type="daily",  # T8-03 widens later; routing reads explicit kwarg
        window_start=now - timedelta(days=7),
        window_end=now,
        source_chat_id=chat_id,
        cards=[],
        messages=[
            DigestContextMessage(
                message_version_id=mv_id,
                chat_message_id=_cm_id,
                author_display="Alice",
                text="weekly recap",
                ts=now - timedelta(days=3),
            )
        ],
    )

    class _ConnectionError(RuntimeError):
        """Synthetic provider failure — name distinct enough that we can
        verify the ledger error_text carries it (and NOT 'TypeError')."""

    class _FailingProvider:
        async def call(self, *, prompt: str, model: str):
            raise _ConnectionError("upstream unreachable")

    cfg = _make_gateway_config()

    with pytest.raises(_ConnectionError):
        await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=_FailingProvider(),
            type="weekly",
        )
    await db_session.flush()

    # The placeholder ledger row MUST have been updated. Pull the most recent
    # row in this test session.
    row = (
        (
            await db_session.execute(
                _text("SELECT id, error FROM llm_usage_ledger ORDER BY id DESC LIMIT 1")
            )
        )
        .mappings()
        .one_or_none()
    )
    assert row is not None, "placeholder ledger row missing"
    # Must NOT be 'TypeError' (that's the bug signature) and MUST be the
    # provider exception class name.
    assert row["error"] == "_ConnectionError", (
        f"expected ledger.error='_ConnectionError', got {row['error']!r}"
    )


async def test_synthesize_digest_provider_exception_updates_ledger_daily(
    db_session,
):
    """FHR HIGH-1 regression-guard: same fix must keep type='daily' (default)
    path correct — Phase 7 byte-for-byte preserved."""
    from sqlalchemy import text as _text

    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.digest_context import DigestContext, DigestContextMessage
    from bot.services.llm_gateway import synthesize_digest

    chat_id = _next_chat_id()
    _cm_id, mv_id = await _make_msg_and_version(
        db_session,
        chat_id=chat_id,
        ts=datetime.now(timezone.utc) - timedelta(hours=6),
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
                author_display="Bob",
                text="hi",
                ts=now - timedelta(hours=6),
            )
        ],
    )

    class _DailyProviderError(RuntimeError):
        pass

    class _FailingProvider:
        async def call(self, *, prompt: str, model: str):
            raise _DailyProviderError("transient")

    cfg = _make_gateway_config()

    with pytest.raises(_DailyProviderError):
        await synthesize_digest(
            db_session,
            context=ctx,
            config=cfg,
            ledger_repo=LedgerRepo(),
            provider=_FailingProvider(),
            type="daily",
        )
    await db_session.flush()

    row = (
        (
            await db_session.execute(
                _text("SELECT error FROM llm_usage_ledger ORDER BY id DESC LIMIT 1")
            )
        )
        .mappings()
        .one_or_none()
    )
    assert row is not None
    assert row["error"] == "_DailyProviderError"


# ── 9. FHR HIGH-4: DigestConfig.to_context_config() forwards weekly fields ───


def test_digest_config_to_context_config_forwards_weekly_fields():
    """FHR HIGH-4: ``DigestConfig.to_context_config()`` must forward the
    weekly-specific tunables (``weekly_min_cards_threshold``,
    ``weekly_raw_message_top_n``, ``weekly_token_budget_input``) so operator
    env-var overrides actually reach ``build_digest_context``.

    Before the fix the helper only forwarded daily fields, so
    ``_weekly_overrides`` in ``digest_context.py`` always read the dataclass
    defaults — env overrides silently ignored.
    """
    cfg = _make_digest_config(
        weekly_min_cards_threshold=11,
        weekly_raw_message_top_n=77,
        weekly_token_budget_input=32000,
    )
    ctx_cfg = cfg.to_context_config()
    assert ctx_cfg.weekly_min_cards_threshold == 11
    assert ctx_cfg.weekly_raw_message_top_n == 77
    assert ctx_cfg.weekly_token_budget_input == 32000
    # Daily fields still forwarded too.
    assert ctx_cfg.min_cards_threshold == cfg.min_cards_threshold
    assert ctx_cfg.raw_message_top_n == cfg.raw_message_top_n
    assert ctx_cfg.token_budget_input == cfg.token_budget_input


def test_load_digest_config_weekly_env_reaches_context_config(monkeypatch):
    """End-to-end env-var → DigestConfig → DigestCtxConfig forwarding.

    Sets DIGEST_WEEKLY_* env vars, loads config, asserts the to_context_config
    bridge surfaces the same values. Verifies the operator-tuning path
    (HIGH-4 root scenario) is no longer broken.
    """
    monkeypatch.setenv("DIGEST_WEEKLY_TOKEN_BUDGET", "32000")
    monkeypatch.setenv("DIGEST_WEEKLY_MIN_CARDS_THRESHOLD", "15")
    monkeypatch.setenv("DIGEST_WEEKLY_RAW_MESSAGE_TOP_N", "99")

    from bot.services.digests import load_digest_config

    cfg = load_digest_config()
    ctx_cfg = cfg.to_context_config()
    assert ctx_cfg.weekly_token_budget_input == 32000
    assert ctx_cfg.weekly_min_cards_threshold == 15
    assert ctx_cfg.weekly_raw_message_top_n == 99
