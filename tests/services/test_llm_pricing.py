"""Tests for ``bot.services.llm_pricing`` (T5-04).

Per contracts.md §12.6: `MODEL_PRICING` dict + `estimate_cost` helper.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


def test_model_pricing_has_anthropic_haiku_4_5() -> None:
    """MODEL_PRICING must include Anthropic claude-haiku-4-5-20251001."""
    from bot.services.llm_pricing import MODEL_PRICING

    assert "claude-haiku-4-5-20251001" in MODEL_PRICING
    p = MODEL_PRICING["claude-haiku-4-5-20251001"]
    assert p.input_per_million_tokens_usd == Decimal("1.00")
    assert p.output_per_million_tokens_usd == Decimal("5.00")


def test_model_pricing_has_openai_gpt_4o_mini() -> None:
    """MODEL_PRICING must include OpenAI gpt-4o-mini fallback."""
    from bot.services.llm_pricing import MODEL_PRICING

    assert "gpt-4o-mini" in MODEL_PRICING
    p = MODEL_PRICING["gpt-4o-mini"]
    assert p.input_per_million_tokens_usd == Decimal("0.15")
    assert p.output_per_million_tokens_usd == Decimal("0.60")


def test_estimate_cost_claude_haiku_one_million_input() -> None:
    """Acceptance test from contracts.md §12.6: 1M input tokens → $1.000000."""
    from bot.services.llm_pricing import estimate_cost

    cost = estimate_cost(
        model="claude-haiku-4-5-20251001",
        tokens_in=1_000_000,
        tokens_out=0,
    )
    assert cost == Decimal("1.000000")
    # Returned value must be quantized to 6 dp to match NUMERIC(10,6).
    assert -cost.as_tuple().exponent == 6


def test_estimate_cost_mixed_input_output() -> None:
    """Mixed token counts produce sum of input + output costs."""
    from bot.services.llm_pricing import estimate_cost

    # claude-haiku: 100k input × $1/M = $0.10; 50k output × $5/M = $0.25; total $0.35
    cost = estimate_cost(
        model="claude-haiku-4-5-20251001",
        tokens_in=100_000,
        tokens_out=50_000,
    )
    assert cost == Decimal("0.350000")


def test_estimate_cost_quantized_to_six_decimal_places() -> None:
    """Output must be Decimal quantized to 6 dp regardless of token counts."""
    from bot.services.llm_pricing import estimate_cost

    cost = estimate_cost(
        model="gpt-4o-mini",
        tokens_in=1,
        tokens_out=1,
    )
    # 1 token × $0.15/M = $0.00000015, 1 token × $0.60/M = $0.00000060
    # total $0.00000075 → quantized 6 dp → $0.000001 (ROUND_HALF_EVEN)
    # But Decimal default rounding is ROUND_HALF_EVEN; verify exponent only.
    assert -cost.as_tuple().exponent == 6


def test_estimate_cost_unknown_model_raises_key_error() -> None:
    """Missing model id raises KeyError; caller (gateway) categorises this."""
    from bot.services.llm_pricing import estimate_cost

    with pytest.raises(KeyError):
        estimate_cost(model="nonexistent-model", tokens_in=10, tokens_out=10)


def test_gpt_5_6_sol_uses_long_context_pricing_above_272k_input_tokens() -> None:
    from bot.services.llm_pricing import estimate_cost

    standard = estimate_cost(model="gpt-5.6-sol", tokens_in=272_000, tokens_out=1_000)
    long_context = estimate_cost(model="gpt-5.6-sol", tokens_in=272_001, tokens_out=1_000)

    assert standard == Decimal("1.390000")
    assert long_context == Decimal("2.765010")
