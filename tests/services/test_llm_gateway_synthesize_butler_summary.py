"""Behaviour tests for synthesize_butler_summary in bot.services.llm_gateway (T12-03 fix cycle).

Covers:
  - Happy path: mock provider returns valid text → (str, int, Decimal) returned.
  - Ledger row written with call_type='butler_summary'.
  - Citation-anchor enforcement: reference to mvid not in evidence_ids → ButlerPlanError.
  - Citation-anchor enforcement: valid references pass.
  - Budget guard fires (butler daily $1 ceiling) → ButlerPlanError.
  - Provider failure → ButlerPlanError + ledger row with error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from bot.services.butler_tools import ButlerPlanError
from bot.services.llm_gateway import LLMGatewayConfig, synthesize_butler_summary
from bot.services.llm_providers import ProviderResult
from tests.services.test_llm_gateway import FakeSession, _config
from tests.services.test_llm_gateway_plan_butler_action import (
    FakeLedgerRepo,
    _CHAT_ID,
    _make_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _butler_config() -> LLMGatewayConfig:
    return _config()


@dataclass
class FakeSummaryProvider:
    """LLMProvider stub that returns a configurable answer_text."""

    answer_text: str = ""
    tokens_in: int = 150
    tokens_out: int = 80
    request_id: str = "req-summary-001"
    raise_exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        self.calls.append({"prompt": prompt, "model": model})
        if self.raise_exc is not None:
            raise self.raise_exc
        return ProviderResult(
            answer_text=self.answer_text,
            citation_ids=tuple(),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id=self.request_id,
            raw_latency_ms=40,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_butler_summary_happy_path_returns_tuple() -> None:
    """Happy path: valid text from provider → (str, int, Decimal) returned."""
    context = _make_context(mvids=(10, 11))
    # No citation anchors needed for simple happy path.
    summary_text = "The community has 2 Rust experts."

    provider = FakeSummaryProvider(answer_text=summary_text)
    ledger = FakeLedgerRepo()
    session = FakeSession()
    config = _butler_config()

    result = await synthesize_butler_summary(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        draft_intent="intro draft",
        evidence_context=context,
        config=config,
        ledger_repo=ledger,
        provider=provider,
    )

    assert isinstance(result, tuple)
    assert len(result) == 3
    text, ledger_id, cost_usd = result
    assert isinstance(text, str)
    assert text == summary_text
    assert isinstance(ledger_id, int)
    assert ledger_id > 0
    assert isinstance(cost_usd, Decimal)
    assert cost_usd >= Decimal("0")


@pytest.mark.asyncio
async def test_synthesize_butler_summary_writes_butler_summary_call_type() -> None:
    """Ledger row written with call_type='butler_summary'."""
    context = _make_context(mvids=(10, 11))
    summary_text = "Summary text here."

    provider = FakeSummaryProvider(answer_text=summary_text)
    ledger = FakeLedgerRepo()
    session = FakeSession()
    config = _butler_config()

    await synthesize_butler_summary(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        draft_intent="intro draft",
        evidence_context=context,
        config=config,
        ledger_repo=ledger,
        provider=provider,
    )

    assert any(r.call_type == "butler_summary" for r in ledger.rows), (
        f"No butler_summary row found; rows={ledger.rows}"
    )


# ---------------------------------------------------------------------------
# Citation-anchor enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_butler_summary_rejects_citation_outside_evidence_ids() -> None:
    """Citation anchor referencing mvid not in evidence_ids → ButlerPlanError."""
    # evidence_context has evidence_ids = {10, 11}
    context = _make_context(mvids=(10, 11))
    # Text references mvid:999 which is NOT in evidence_ids
    bad_summary = "Here is a fact (mvid:999) from outside evidence."

    provider = FakeSummaryProvider(answer_text=bad_summary)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError) as exc_info:
        await synthesize_butler_summary(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            draft_intent="intro draft",
            evidence_context=context,
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    assert exc_info.value.error_kind == "unbound_citation"


@pytest.mark.asyncio
async def test_synthesize_butler_summary_accepts_valid_mvid_citations() -> None:
    """Citation anchors referencing valid mvids in evidence_ids pass."""
    context = _make_context(mvids=(10, 11))
    # Both references are in evidence_ids
    good_summary = "Expert Alice (mvid:10) and Bob (mvid:11) know Rust."

    provider = FakeSummaryProvider(answer_text=good_summary)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    text, ledger_id, cost_usd = await synthesize_butler_summary(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        draft_intent="intro draft",
        evidence_context=context,
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )
    assert text == good_summary


@pytest.mark.asyncio
async def test_synthesize_butler_summary_accepts_valid_card_citations() -> None:
    """Citation anchors using card:N prefix referencing valid IDs pass.

    M-C fix: the fixture must actually use ``card:N`` notation (not ``mvid:N``)
    to exercise the card citation path. The gateway matches both ``mvid:N`` and
    ``card:N`` against evidence_context.evidence_ids — so ``card:10`` is valid
    when 10 is in evidence_ids.
    """
    context = _make_context(mvids=(10,))
    # Use card:10 — must be in evidence_ids (which contains 10)
    good_summary = "See knowledge card (card:10) for details."

    provider = FakeSummaryProvider(answer_text=good_summary)
    ledger = FakeLedgerRepo()
    session = FakeSession()

    text, _, _ = await synthesize_butler_summary(
        session=session,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        draft_intent="intro draft",
        evidence_context=context,
        config=_butler_config(),
        ledger_repo=ledger,
        provider=provider,
    )
    assert text == good_summary


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_butler_summary_raises_when_butler_daily_budget_exceeded() -> None:
    """Butler daily $1 budget exceeded → ButlerPlanError raised before provider call."""
    context = _make_context(mvids=(10, 11))
    summary_text = "Some summary."

    provider = FakeSummaryProvider(answer_text=summary_text)
    from decimal import Decimal as D

    # Pre-load ledger so butler daily total is at $1 ceiling
    ledger = FakeLedgerRepo(
        daily_cost=D("1.00"),  # already at butler ceiling
        monthly_cost=D("0"),
    )
    session = FakeSession()
    config = LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=D("100.00"),  # shared daily ceiling is high (not the blocker)
        monthly_ceiling_usd=D("100.00"),
        prompt_template_version="v1.0.0",
    )

    with pytest.raises(ButlerPlanError):
        await synthesize_butler_summary(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            draft_intent="intro draft",
            evidence_context=context,
            config=config,
            ledger_repo=ledger,
            provider=provider,
        )

    # Provider must NOT have been called
    assert len(provider.calls) == 0


# ---------------------------------------------------------------------------
# Provider failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_butler_summary_raises_on_provider_failure() -> None:
    """Provider exception → ButlerPlanError raised + ledger row written with error."""
    from bot.services.llm_providers import ProviderTransientError

    context = _make_context(mvids=(10, 11))
    provider = FakeSummaryProvider(raise_exc=ProviderTransientError("transient", message="timeout"))
    ledger = FakeLedgerRepo()
    session = FakeSession()

    with pytest.raises(ButlerPlanError):
        await synthesize_butler_summary(
            session=session,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            draft_intent="intro draft",
            evidence_context=context,
            config=_butler_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    # Should have written a ledger row with an error
    assert len(ledger.rows) >= 1
    row = ledger.rows[-1]
    assert row.error is not None
    assert "provider_error" in (row.error or "")
