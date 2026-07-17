from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest


def _client(handler: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=handler,
        base_url="https://api.deepseek.com",
    )


@pytest.mark.asyncio
async def test_deepseek_provider_calls_v4_flash_non_thinking_and_reads_usage() -> None:
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "ds-request-1",
                "choices": [{"message": {"content": "Ответ [[mv:41]] [[mv:42]]"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            },
        )

    async with _client(httpx.MockTransport(handle)) as client:
        provider = DeepSeekProvider(api_key="test-key", client=client)
        result = await provider.call(prompt="question", model="deepseek-v4-flash")

    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"] == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "question"}],
        "thinking": {"type": "disabled"},
        "max_tokens": 512,
        "stream": False,
    }
    assert result.answer_text == "Ответ [[mv:41]] [[mv:42]]"
    assert result.citation_ids == (41, 42)
    assert result.tokens_in == 120
    assert result.tokens_out == 30
    assert result.request_id == "ds-request-1"


@pytest.mark.asyncio
async def test_deepseek_provider_adds_json_output_only_when_enabled() -> None:
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "ds-json-request-1",
                "choices": [{"message": {"content": '{"title":"Topic","body_markdown":"Fact"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            },
        )

    async with _client(httpx.MockTransport(handle)) as client:
        provider = DeepSeekProvider(
            api_key="test-key",
            client=client,
            json_output=True,
        )
        await provider.call(prompt="return json", model="deepseek-v4-flash")

    assert captured["payload"] == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "return json"}],
        "thinking": {"type": "disabled"},
        "max_tokens": 512,
        "stream": False,
        "response_format": {"type": "json_object"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type", "subtype"),
    [
        (401, "structural", "auth"),
        (429, "transient", "rate_limit"),
        (500, "transient", "5xx"),
    ],
)
async def test_deepseek_provider_maps_http_failures(
    status: int,
    error_type: str,
    subtype: str,
) -> None:
    from bot.services.llm_providers import (
        ProviderStructuralError,
        ProviderTransientError,
    )
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, json={"error": {"message": "redacted"}})
    )
    expected = ProviderStructuralError if error_type == "structural" else ProviderTransientError
    async with _client(transport) as client:
        provider = DeepSeekProvider(api_key="test-key", client=client)
        with pytest.raises(expected) as exc_info:
            await provider.call(prompt="question", model="deepseek-v4-flash")

    assert exc_info.value.subtype == subtype


@pytest.mark.asyncio
async def test_deepseek_provider_requires_api_key_before_network() -> None:
    from bot.services.llm_providers import ProviderStructuralError
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    provider = DeepSeekProvider(api_key="")
    with pytest.raises(ProviderStructuralError) as exc_info:
        await provider.call(prompt="question", model="deepseek-v4-flash")

    assert exc_info.value.subtype == "auth"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": "1", "completion_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
    ],
)
async def test_deepseek_provider_rejects_missing_or_invalid_usage(usage: object) -> None:
    from bot.services.llm_providers import ProviderStructuralError
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    payload = {
        "id": "ds-invalid-usage",
        "choices": [{"message": {"content": "answer"}}],
    }
    if usage is not None:
        payload["usage"] = usage
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    async with _client(transport) as client:
        provider = DeepSeekProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderStructuralError) as exc_info:
            await provider.call(prompt="question", model="deepseek-v4-flash")

    assert exc_info.value.subtype == "contract_violation"


def test_deepseek_pricing_is_pinned_to_official_v4_flash_rates() -> None:
    from bot.services.llm_pricing import MODEL_PRICING, estimate_cost

    pricing = MODEL_PRICING["deepseek-v4-flash"]
    assert pricing.input_per_million_tokens_usd == Decimal("0.14")
    assert pricing.output_per_million_tokens_usd == Decimal("0.28")
    assert estimate_cost(
        model="deepseek-v4-flash",
        tokens_in=13_000_000,
        tokens_out=850_000,
    ) == Decimal("2.058000")


def test_gateway_config_and_provider_resolver_support_deepseek(monkeypatch) -> None:
    from bot.services.llm_gateway import load_gateway_config, resolve_provider
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    config = load_gateway_config()

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-flash"
    assert isinstance(resolve_provider("deepseek"), DeepSeekProvider)


def test_deepseek_provider_accepts_bounded_task_specific_output_limit() -> None:
    from bot.services.llm_gateway import resolve_provider
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    provider = resolve_provider("deepseek", deepseek_max_tokens=8_192)

    assert isinstance(provider, DeepSeekProvider)
    assert provider._max_tokens == 8_192


def test_deepseek_provider_resolver_forwards_json_output() -> None:
    from bot.services.llm_gateway import resolve_provider
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    provider = resolve_provider("deepseek", deepseek_json_output=True)

    assert isinstance(provider, DeepSeekProvider)
    assert provider._json_output is True


@pytest.mark.parametrize("value", [0, -1, True, 384_001])
def test_deepseek_provider_rejects_invalid_output_limit(value: object) -> None:
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    with pytest.raises(ValueError):
        DeepSeekProvider(max_tokens=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_deepseek_provider_rejects_non_boolean_json_output(value: object) -> None:
    from bot.services.llm_providers.deepseek import DeepSeekProvider

    with pytest.raises(ValueError, match="json_output"):
        DeepSeekProvider(json_output=value)  # type: ignore[arg-type]
