"""Weekly-specific bindings for the shared issue #406 editorial contract."""

from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def test_weekly_prompt_is_adaptive_and_has_no_item_quota() -> None:
    from bot.services.llm_prompts import digest_weekly_v0_1_0 as weekly

    assert weekly.PROMPT_VERSION == "digest-weekly-v0.3.0"
    assert "Жёсткого лимита пунктов нет" in weekly.DRAFT_INSTRUCTIONS
    assert "объединяй повторы в кластеры" in weekly.DRAFT_INSTRUCTIONS
    assert "2–3 минуты" in weekly.DRAFT_INSTRUCTIONS
    assert "1-8" not in weekly.DRAFT_INSTRUCTIONS


def test_weekly_structured_schema_has_no_max_items() -> None:
    from bot.services.llm_prompts.digest_weekly_v0_1_0 import draft_response_schema

    schema = draft_response_schema(["[[mv:1]]"])
    sections = schema["properties"]["sections"]
    items = sections["items"]["properties"]["items"]
    assert "maxItems" not in sections
    assert "maxItems" not in items


def test_weekly_gateway_uses_isolated_sol_model_and_prompt_version() -> None:
    from bot.services.llm_gateway import load_digest_gateway_config

    config = load_digest_gateway_config(digest_type="weekly")
    assert config.provider == "openai"
    assert config.model == "gpt-5.6-sol"
    assert config.prompt_template_version == "digest-weekly-v0.3.0"


def test_weekly_incident_guard_defaults_are_not_editorial_budget() -> None:
    from bot.services.digests import DigestConfig

    config = DigestConfig()
    assert config.weekly_cost_ceiling_usd == Decimal("200.00")
    assert config.weekly_monthly_cost_ceiling_usd == Decimal("2000.00")


def test_load_digest_config_reads_explicit_weekly_incident_stops(monkeypatch) -> None:
    from bot.services.digests import load_digest_config

    monkeypatch.setenv("DIGEST_WEEKLY_USD_CEILING", "321")
    monkeypatch.setenv("DIGEST_WEEKLY_MONTHLY_USD_CEILING", "4321")
    config = load_digest_config()
    assert config.weekly_cost_ceiling_usd == Decimal("321")
    assert config.weekly_monthly_cost_ceiling_usd == Decimal("4321")


def test_digest_provider_and_model_are_fail_closed(monkeypatch) -> None:
    from bot.services.llm_gateway import load_digest_gateway_config

    monkeypatch.setenv("DIGEST_LLM_PROVIDER", "deepseek")
    with pytest.raises(ValueError, match="must be openai"):
        load_digest_gateway_config(digest_type="weekly")
    monkeypatch.setenv("DIGEST_LLM_PROVIDER", "openai")
    monkeypatch.setenv("DIGEST_LLM_MODEL", "gpt-5-mini")
    with pytest.raises(ValueError, match="gpt-5.6-sol"):
        load_digest_gateway_config(digest_type="weekly")
