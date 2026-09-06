from __future__ import annotations

import json

import httpx
import pytest

from bot.services.llm_providers import ProviderStructuralError
from bot.services.llm_providers.openai import OpenAIProvider


async def test_structured_digest_request_uses_private_non_truncating_responses_contract() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "output": [
                    {"type": "reasoning"},
                    {"type": "message", "content": [{"type": "output_text", "text": '{"x":'}]},
                    {"type": "message", "content": [{"type": "output_text", "text": "1}"}]},
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )

    provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
    result = await provider.call_structured(
        instructions="stable rules",
        input_text="private window",
        model="gpt-5.6-sol",
        schema_name="digest_draft",
        json_schema={"type": "object"},
        reasoning_effort="medium",
    )

    assert result.answer_text == '{"x":1}'
    assert captured["truncation"] == "disabled"
    assert captured["store"] is False
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["instructions"] == "stable rules"
    assert captured["input"] == "private window"
    assert captured["text"]["format"] == {
        "type": "json_schema",
        "name": "digest_draft",
        "strict": True,
        "schema": {"type": "object"},
    }


async def test_structured_digest_refusal_fails_closed() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "id": "resp_refused",
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "output": [
                    {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )
    provider = OpenAIProvider(api_key="test-key", transport=transport)
    with pytest.raises(ProviderStructuralError, match="refused"):
        await provider.call_structured(
            instructions="rules",
            input_text="window",
            model="gpt-5.6-sol",
            schema_name="digest_draft",
            json_schema={"type": "object"},
            reasoning_effort="medium",
        )


async def test_structured_digest_http_error_does_not_expose_response_body() -> None:
    secret_body = "raw-private-body-must-not-escape"
    provider = OpenAIProvider(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, text=secret_body, request=request)
        ),
    )
    with pytest.raises(ProviderStructuralError) as exc_info:
        await provider.call_structured(
            instructions="rules",
            input_text="window",
            model="gpt-5.6-sol",
            schema_name="digest_draft",
            json_schema={"type": "object"},
            reasoning_effort="medium",
        )
    assert secret_body not in str(exc_info.value)
