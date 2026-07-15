from __future__ import annotations

import base64
import json

import httpx
import pytest


@pytest.mark.asyncio
async def test_openai_vision_provider_sends_low_detail_bounded_request() -> None:
    from bot.services.llm_providers.openai_vision import OpenAIVisionProvider

    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "vision-req-1"},
            json={
                "id": "chatcmpl-vision-1",
                "choices": [{"message": {"content": "На фото люди у доски."}}],
                "usage": {"prompt_tokens": 250, "completion_tokens": 12},
            },
        )

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.openai.com/v1",
    ) as client:
        provider = OpenAIVisionProvider(api_key="test-key", client=client)
        result = await provider.describe(
            image_bytes=b"jpeg-bytes",
            mime_type="image/jpeg",
            caption="Встреча",
            model="gpt-5-nano",
        )

    assert captured["path"] == "/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5-nano"
    assert payload["max_completion_tokens"] == 180
    assert payload["stream"] is False
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "Russian" in content[0]["text"]
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(b"jpeg-bytes").decode("ascii"),
            "detail": "low",
        },
    }
    assert result.description == "На фото люди у доски."
    assert result.tokens_in == 250
    assert result.tokens_out == 12
    assert result.request_id == "chatcmpl-vision-1"


@pytest.mark.parametrize("mime_type", ["image/svg+xml", "text/html", "image/bmp"])
@pytest.mark.asyncio
async def test_openai_vision_provider_rejects_unsupported_mime_before_network(
    mime_type: str,
) -> None:
    from bot.services.llm_providers import ProviderStructuralError
    from bot.services.llm_providers.openai_vision import OpenAIVisionProvider

    provider = OpenAIVisionProvider(api_key="test-key")
    with pytest.raises(ProviderStructuralError, match="unsupported image MIME"):
        await provider.describe(
            image_bytes=b"data",
            mime_type=mime_type,
            caption=None,
            model="gpt-5-nano",
        )


@pytest.mark.asyncio
async def test_openai_vision_provider_rejects_oversized_image_before_network() -> None:
    from bot.services.llm_providers import ProviderStructuralError
    from bot.services.llm_providers.openai_vision import MAX_IMAGE_BYTES, OpenAIVisionProvider

    provider = OpenAIVisionProvider(api_key="test-key")
    with pytest.raises(ProviderStructuralError, match="image exceeds"):
        await provider.describe(
            image_bytes=b"x" * (MAX_IMAGE_BYTES + 1),
            mime_type="image/jpeg",
            caption=None,
            model="gpt-5-nano",
        )
