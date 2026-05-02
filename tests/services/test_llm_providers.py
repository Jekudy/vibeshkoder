"""Provider abstraction conformance tests for T5-01 Wave 1."""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from bot.services.llm_providers import (
    LLMProvider,
    ProviderResult,
    ProviderStructuralError,
    ProviderTransientError,
)
from bot.services.llm_providers.anthropic import AnthropicProvider
from bot.services.llm_providers.openai import OpenAIProvider


def test_provider_result_is_named_tuple() -> None:
    """``ProviderResult`` is a NamedTuple with the §5.A fields."""
    res = ProviderResult(
        answer_text="ok",
        citation_ids=(1, 2),
        tokens_in=10,
        tokens_out=20,
        request_id="req-abc",
        raw_latency_ms=42,
    )

    assert res.answer_text == "ok"
    assert res.citation_ids == (1, 2)
    assert res.tokens_in == 10
    assert res.tokens_out == 20
    assert res.request_id == "req-abc"
    assert res.raw_latency_ms == 42


def test_provider_protocol_has_async_call() -> None:
    """``LLMProvider.call`` is an async method per §5.A."""
    sig = inspect.signature(LLMProvider.call)  # type: ignore[arg-type]
    assert "prompt" in sig.parameters
    assert "model" in sig.parameters


def test_anthropic_provider_implements_protocol() -> None:
    """``AnthropicProvider`` honours the ``LLMProvider`` Protocol surface."""
    provider: LLMProvider = AnthropicProvider()
    assert hasattr(provider, "call")
    assert inspect.iscoroutinefunction(provider.call)


def test_openai_provider_implements_protocol() -> None:
    """``OpenAIProvider`` honours the ``LLMProvider`` Protocol surface."""
    provider: LLMProvider = OpenAIProvider()
    assert hasattr(provider, "call")
    assert inspect.iscoroutinefunction(provider.call)


def test_provider_transient_error_carries_subtype() -> None:
    """Transient errors expose ``subtype`` for ledger taxonomy."""
    err = ProviderTransientError("rate_limit", message="429 from upstream")
    assert err.subtype == "rate_limit"
    assert "429" in str(err)


def test_provider_structural_error_carries_subtype() -> None:
    """Structural errors expose ``subtype`` for ledger taxonomy."""
    err = ProviderStructuralError("auth", message="401 missing api key")
    assert err.subtype == "auth"
    assert "401" in str(err)


@pytest.mark.parametrize("subtype", ["rate_limit", "timeout", "5xx", "connection_reset"])
def test_transient_subtypes_documented(subtype: str) -> None:
    """All four transient subtypes can be constructed."""
    err = ProviderTransientError(subtype, message=f"{subtype} happened")
    assert err.subtype == subtype


@pytest.mark.parametrize(
    "subtype",
    ["auth", "bad_request", "contract_violation", "model_not_found"],
)
def test_structural_subtypes_documented(subtype: str) -> None:
    """All four structural subtypes can be constructed."""
    err = ProviderStructuralError(subtype, message=f"{subtype} happened")
    assert err.subtype == subtype


def test_provider_result_type_hints() -> None:
    """ProviderResult exposes the typed fields the gateway depends on."""
    hints = get_type_hints(ProviderResult)
    assert "answer_text" in hints
    assert "citation_ids" in hints
    assert "tokens_in" in hints
    assert "tokens_out" in hints
    assert "request_id" in hints
    assert "raw_latency_ms" in hints
