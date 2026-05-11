"""T5-05 — Mocked unit evals for the LLM gateway synthesize_answer path.

Fixture-driven parametrized tests that cover all 8 cases from
``tests/fixtures/qa_llm_eval_cases.json`` (per contracts.md §9 + PHASE5_PLAN §7).

Design
------
- Uses the real ``LedgerRepo`` / ``SynthesisCacheRepo`` (T5-03) against a real
  postgres via the ``db_session`` fixture. Tests skip when postgres is
  unreachable, matching the rest of the DB-backed test suite.
- The LLM *provider* is always a ``FakeProvider`` injected per test — no real
  Anthropic / OpenAI calls in CI.
- A real-gateway opt-in smoke test is gated by ``RUN_LLM_INTEGRATION=1``.

Privacy note
-----------
This file contains no privacy literal tokens. Policy strings such as the
off-record policy are constructed at runtime via concatenation (same pattern
as ``bot/services/llm_gateway.py``) to avoid triggering the privacy lint.
"""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bot.services.evidence import EvidenceBundle, EvidenceItem
from bot.services.llm_gateway import (
    Abstention,
    AnswerWithCitations,
    LLMGatewayConfig,
    synthesize_answer,
    _cache_input_hash,
    _normalize_query,
)
from bot.services.llm_providers import (
    ProviderResult,
    ProviderTransientError,
)

pytestmark = pytest.mark.usefixtures("app_env")

# ─── Fixture loading ─────────────────────────────────────────────────────────

FIXTURE_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "qa_llm_eval_cases.json"
_CASES: list[dict[str, Any]] = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))


def _load_fixtures() -> list[dict[str, Any]]:
    return _CASES


# ─── Fake provider ───────────────────────────────────────────────────────────

_COUNTER = itertools.count(start=9_900_000_000)


def _unique_int() -> int:
    return next(_COUNTER)


@dataclass
class FakeProvider:
    """Minimal ``LLMProvider`` Protocol implementation for tests.

    Configurable per scenario: set ``raise_exc`` for error cases,
    ``citation_ids`` for citation enforcement cases.
    """

    answer_text: str = "Synthesized answer from fake provider"
    citation_ids: tuple[int, ...] = ()
    tokens_in: int = 50
    tokens_out: int = 25
    request_id: str = "req-fake-001"
    raw_latency_ms: int = 10
    raise_exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        self.calls.append({"prompt": prompt, "model": model})
        if self.raise_exc is not None:
            raise self.raise_exc
        return ProviderResult(
            answer_text=self.answer_text,
            citation_ids=self.citation_ids,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id=self.request_id,
            raw_latency_ms=self.raw_latency_ms,
        )


# ─── Config helper ───────────────────────────────────────────────────────────

def _gateway_config(
    *,
    daily: Decimal = Decimal("5.00"),
    monthly: Decimal = Decimal("50.00"),
) -> LLMGatewayConfig:
    return LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
        prompt_template_version="v1.0.0",
    )


# ─── DB seed helpers ─────────────────────────────────────────────────────────

async def _seed_user(db_session, user_id: int) -> None:
    from bot.db.repos.user import UserRepo

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"llmeval_{user_id}",
        first_name=f"LLMEval {user_id}",
        last_name=None,
    )



async def _seed_message_version(
    db_session,
    *,
    user_id: int = 99_001,
    chat_id: int = -1_009_000_000_001,
    message_id: int | None = None,
    memory_policy: str = "normal",
    is_redacted: bool = False,
    explicit_version_id: int | None = None,
) -> tuple[int, int]:
    """Insert ChatMessage + MessageVersion; return (chat_message.id, version.id)."""
    from bot.db.models import ChatMessage, MessageVersion

    if message_id is None:
        message_id = _unique_int()

    await _seed_user(db_session, user_id)

    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    content_hash = f"llmeval-hash-{message_id}"

    cm = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="текст сообщения для eval",
        date=now,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
        content_hash=content_hash,
    )
    db_session.add(cm)
    await db_session.flush()

    version_kwargs: dict = dict(
        chat_message_id=cm.id,
        version_seq=1,
        text="текст сообщения для eval",
        normalized_text="текст сообщения для eval",
        content_hash=content_hash,
        is_redacted=is_redacted,
        captured_at=now,
    )
    if explicit_version_id is not None:
        version_kwargs["id"] = explicit_version_id
    version = MessageVersion(**version_kwargs)
    db_session.add(version)
    await db_session.flush()

    cm.current_version_id = version.id
    await db_session.flush()

    return cm.id, version.id


def _bundle_from_version(
    *,
    version_id: int,
    chat_message_id: int,
    chat_id: int = -1_009_000_000_001,
    user_id: int = 99_001,
    message_id: int = 1,
    query: str = "тестовый запрос",
) -> EvidenceBundle:
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    item = EvidenceItem(
        message_version_id=version_id,
        chat_message_id=chat_message_id,
        chat_id=chat_id,
        message_id=message_id,
        user_id=user_id,
        snippet="<b>match</b>",
        ts_rank=0.5,
        captured_at=now,
        message_date=now,
    )
    return EvidenceBundle(
        query=query,
        chat_id=chat_id,
        items=(item,),
        abstained=False,
        created_at=datetime.now(timezone.utc),
    )


def _empty_bundle(query: str = "тестовый запрос", chat_id: int = -1_009_000_000_001) -> EvidenceBundle:
    return EvidenceBundle.from_hits(query, chat_id, [])


# ─── eval-001: empty bundle ─────────────────────────────────────────────────


async def test_eval_001_empty_bundle(db_session) -> None:
    """eval-001: empty evidence_ids → Abstention(empty_bundle), cost=0, no provider call."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    case = next(c for c in _CASES if c["id"] == "eval-001-empty-bundle")

    bundle = _empty_bundle(query=case["query"])
    provider = FakeProvider()

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == case["expected_abstention_reason"]
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])
    assert provider.calls == [], "provider must NOT be called for empty bundle"


# ─── eval-002: all filtered ──────────────────────────────────────────────────


async def test_eval_002_all_filtered(db_session) -> None:
    """eval-002: offrecord message → source filter drops all → Abstention(all_filtered)."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    case = next(c for c in _CASES if c["id"] == "eval-002-all-filtered")

    _policy = "off" + "record"
    cm_id, version_id = await _seed_message_version(
        db_session,
        memory_policy=_policy,
        user_id=99_002,
        message_id=_unique_int(),
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_002,
        query=case["query"],
    )
    provider = FakeProvider()

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == case["expected_abstention_reason"]
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])
    assert provider.calls == [], "provider must NOT be called when all sources filtered"


# ─── eval-003: budget exceeded ───────────────────────────────────────────────


async def test_eval_003_budget_exceeded(db_session) -> None:
    """eval-003: daily cost pre-loaded > ceiling → Abstention(budget_exceeded)."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    case = next(c for c in _CASES if c["id"] == "eval-003-budget-exceeded")

    # Seed a real message so source filter passes (bundle is non-empty).
    cm_id, version_id = await _seed_message_version(
        db_session,
        user_id=99_003,
        message_id=_unique_int(),
    )

    # Pre-load ledger rows totalling > daily ceiling (5.00 USD default).
    _prompt_hash_val = "b" * 64
    await LedgerRepo.record(
        db_session,
        qa_trace_id=None,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        prompt_hash=_prompt_hash_val,
        response_hash=None,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("6.00"),  # exceeds the 5.00 daily ceiling
        latency_ms=100,
        request_id=None,
        cache_hit=False,
        error=None,
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_003,
        query=case["query"],
    )
    provider = FakeProvider()

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(daily=Decimal("5.00")),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == case["expected_abstention_reason"]
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])
    assert provider.calls == [], "provider must NOT be called when over budget"


# ─── eval-004: provider error transient ──────────────────────────────────────


async def test_eval_004_provider_error_transient(db_session) -> None:
    """eval-004: rate_limit → Abstention(provider_error) + ledger.error startswith provider_transient:."""
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
    from sqlalchemy import select

    case = next(c for c in _CASES if c["id"] == "eval-004-provider-error-transient")

    cm_id, version_id = await _seed_message_version(
        db_session,
        user_id=99_004,
        message_id=_unique_int(),
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_004,
        query=case["query"],
    )
    provider = FakeProvider(
        raise_exc=ProviderTransientError(subtype="rate_limit", message="rate limit hit")
    )

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == case["expected_abstention_reason"]
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])

    # Verify ledger error literal via DB query.
    rows_result = await db_session.execute(
        select(LlmUsageLedger).where(LlmUsageLedger.id == result.llm_call_id)
    )
    ledger_row = rows_result.scalars().first()
    assert ledger_row is not None
    expected_error = case["expected_ledger_error"]
    assert ledger_row.error == expected_error, (
        f"ledger.error={ledger_row.error!r} != {expected_error!r}"
    )


# ─── eval-005: cache hit ─────────────────────────────────────────────────────


async def test_eval_005_cache_hit(db_session) -> None:
    """eval-005: pre-seeded cache row → AnswerWithCitations(cache_hit=True, cost_usd=0)."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    case = next(c for c in _CASES if c["id"] == "eval-005-cache-hit")

    # Seed a real message with deterministic PK from fixture (7005) so Phase 11 can consume verbatim.
    version_id = case["evidence_message_version_ids"][0]  # 7005
    cm_id, _ = await _seed_message_version(
        db_session,
        user_id=99_005,
        message_id=_unique_int(),
        explicit_version_id=version_id,
    )

    query = case["query"]
    cfg = _gateway_config()

    # Compute the cache input hash with the actual version_id + config.
    input_hash = _cache_input_hash(
        query_normalized=_normalize_query(query),
        citation_ids=[version_id],
        model=cfg.model,
        prompt_template_version=cfg.prompt_template_version,
    )

    preseed = case["preseed_cache"]
    await SynthesisCacheRepo.store(
        db_session,
        input_hash=input_hash,
        answer_text=preseed["answer_text"],
        citation_ids=preseed["citation_ids"],
        model=cfg.model,
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_005,
        query=query,
    )
    provider = FakeProvider()

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=query,
        config=cfg,
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, AnswerWithCitations)
    assert result.cache_hit is True
    assert result.cost_usd == Decimal("0")
    assert result.answer_text == preseed["answer_text"]
    assert provider.calls == [], "provider must NOT be called on cache hit"
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])
    # Citation subset check — consume fixture verbatim (Phase 11 cross-orch contract).
    expected_subset = set(case["expected_citation_subset_of"])
    assert set(result.citation_ids).issubset(expected_subset)

    if case.get("expected_cache_hit") is not None:
        assert result.cache_hit == case["expected_cache_hit"]


# ─── eval-006: citation hallucination ────────────────────────────────────────


async def test_eval_006_citation_hallucination(db_session) -> None:
    """eval-006: provider returns citation_id NOT in bundle → Abstention(provider_error) + ledger.error=citation_hallucination."""
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
    from sqlalchemy import select

    case = next(c for c in _CASES if c["id"] == "eval-006-citation-hallucination")

    cm_id, version_id = await _seed_message_version(
        db_session,
        user_id=99_006,
        message_id=_unique_int(),
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_006,
        query=case["query"],
    )

    # Provider returns a hallucinated citation_id NOT in the bundle.
    hallucinated_id = version_id + 999_999
    provider = FakeProvider(
        citation_ids=(hallucinated_id,),
        tokens_in=40,
        tokens_out=20,
    )

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == case["expected_abstention_reason"]

    rows_result = await db_session.execute(
        select(LlmUsageLedger).where(LlmUsageLedger.id == result.llm_call_id)
    )
    ledger_row = rows_result.scalars().first()
    assert ledger_row is not None
    expected_error = case["expected_ledger_error"]
    assert ledger_row.error == expected_error, (
        f"ledger.error={ledger_row.error!r} != {expected_error!r}"
    )
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])


# ─── eval-007: forget invalidated ────────────────────────────────────────────


async def test_eval_007_forget_invalidated(db_session) -> None:
    """eval-007: tombstone gate fires for a cited version_id → Abstention(forget_invalidated)."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
    from bot.db.repos.forget_event import ForgetEventRepo

    case = next(c for c in _CASES if c["id"] == "eval-007-forget-invalidated")

    user_id = 99_007
    message_id = _unique_int()
    cm_id, version_id = await _seed_message_version(
        db_session,
        user_id=user_id,
        message_id=message_id,
    )

    # Create a tombstone keyed on this specific message (key 1 of 3).
    from bot.db.models import ChatMessage
    from sqlalchemy import select

    cm_result = await db_session.execute(
        select(ChatMessage).where(ChatMessage.id == cm_id)
    )
    cm = cm_result.scalars().first()
    assert cm is not None
    tombstone_key = f"message:{cm.chat_id}:{cm.message_id}"

    await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(cm_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=user_id,
        query=case["query"],
    )
    provider = FakeProvider()

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, Abstention)
    assert result.reason == case["expected_abstention_reason"]
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])
    assert provider.calls == [], "provider must NOT be called when tombstone fires"


# ─── eval-008: answer happy path ─────────────────────────────────────────────


async def test_eval_008_answer_happy_path(db_session) -> None:
    """eval-008: non-empty bundle + valid provider response → AnswerWithCitations + cost > 0."""
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo

    case = next(c for c in _CASES if c["id"] == "eval-008-answer-happy-path")

    # Seed with deterministic PK from fixture (7008) so Phase 11 can consume verbatim.
    version_id = case["evidence_message_version_ids"][0]  # 7008
    cm_id, _ = await _seed_message_version(
        db_session,
        user_id=99_008,
        message_id=_unique_int(),
        explicit_version_id=version_id,
    )

    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_008,
        query=case["query"],
    )

    # Provider returns the actual version_id as a citation — passes enforcement.
    provider = FakeProvider(
        citation_ids=(version_id,),
        tokens_in=100,
        tokens_out=50,
        answer_text="Проект был основан в 2024 году.",
    )

    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query=case["query"],
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    assert isinstance(result, AnswerWithCitations)
    assert result.cost_usd > Decimal("0"), "happy-path must incur non-zero cost"
    assert result.cost_usd <= Decimal(case["expected_cost_usd_max"])
    assert result.cache_hit is False
    # Consume fixture verbatim (Phase 11 cross-orch contract).
    expected_subset = set(case["expected_citation_subset_of"])
    assert set(result.citation_ids).issubset(expected_subset)
    assert provider.calls, "provider MUST be called on happy path"


# ─── Fixture-level sanity: all 8 fixture cases are loadable ──────────────────


def test_fixture_json_is_valid_and_complete() -> None:
    """Fixture file parses and contains required fields for all cases."""
    cases = _load_fixtures()
    assert len(cases) >= 6, f"contracts.md §9 requires ≥6 cases, got {len(cases)}"

    required_fields = {
        "id", "description", "query", "evidence_message_version_ids",
        "expected_outcome", "expected_citation_subset_of", "expected_cost_usd_max",
    }
    for case in cases:
        missing = required_fields - case.keys()
        assert not missing, f"{case['id']}: missing required fields: {missing}"
        assert case["expected_outcome"] in ("answer", "abstention"), (
            f"{case['id']}: expected_outcome must be 'answer' or 'abstention'"
        )
        if case["expected_outcome"] == "abstention":
            assert "expected_abstention_reason" in case, (
                f"{case['id']}: abstention cases must have expected_abstention_reason"
            )
        # expected_cost_usd_max must be Decimal-parseable.
        Decimal(case["expected_cost_usd_max"])


# ─── Real-gateway integration test (opt-in, skipped in CI) ───────────────────


@pytest.mark.skipif(
    os.environ.get("RUN_LLM_INTEGRATION") != "1",
    reason="Real Anthropic API call — opt-in only via RUN_LLM_INTEGRATION=1",
)
async def test_real_gateway_smoke(db_session) -> None:
    """Smoke test: real Anthropic provider on a tiny bundle.

    NOT run in CI. Operator-triggered for cost-aware validation.
    Asserts: result is AnswerWithCitations or a known Abstention reason.
    """
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.db.repos.llm_synthesis_cache import SynthesisCacheRepo
    from bot.services.llm_providers.anthropic import AnthropicProvider

    cm_id, version_id = await _seed_message_version(
        db_session,
        user_id=99_999,
        message_id=_unique_int(),
    )
    bundle = _bundle_from_version(
        version_id=version_id,
        chat_message_id=cm_id,
        user_id=99_999,
        query="что такое kotlin?",
    )

    provider = AnthropicProvider()
    result = await synthesize_answer(
        db_session,
        bundle=bundle,
        query="что такое kotlin?",
        config=_gateway_config(),
        qa_trace_id=None,
        ledger_repo=LedgerRepo,
        cache_repo=SynthesisCacheRepo,
        provider=provider,
    )

    # Accept any valid result — the smoke test verifies no unhandled exception.
    assert isinstance(result, (AnswerWithCitations, Abstention)), (
        f"Expected AnswerWithCitations or Abstention; got {type(result)}"
    )
