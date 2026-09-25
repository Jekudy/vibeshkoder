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
    assert payload["max_completion_tokens"] == 2_048
    assert payload["reasoning_effort"] == "minimal"
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


# Realistic gpt-5-nano prod failure shape (llm_usage_ledger, ~180 rows since
# 2026-07-16, all ``provider_error:ProviderStructuralError:contract_violation``):
# ``max_completion_tokens`` bounds reasoning + output tokens combined (OpenAI
# chat.completions reference), default ``reasoning_effort`` is ``medium``, so a
# cap of 180 is fully consumed by reasoning and ``content`` comes back empty
# with ``finish_reason="length"``.
def _reasoning_exhausted_response(cap: int) -> dict:
    return {
        "id": "chatcmpl-prod-shape",
        "object": "chat.completion",
        "model": "gpt-5-nano",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "", "refusal": None},
                "finish_reason": "length",
            }
        ],
        "usage": {
            "prompt_tokens": 312,
            "completion_tokens": cap,
            "total_tokens": 312 + cap,
            "completion_tokens_details": {"reasoning_tokens": cap},
        },
    }


@pytest.mark.asyncio
async def test_openai_vision_provider_gpt5_nano_reasoning_cap_red_green() -> None:
    """Emulated gpt-5-nano server: medium/default reasoning on a 180-token cap
    reproduces the prod contract_violation; minimal effort + headroom succeeds.

    Red on the pre-fix code (no ``reasoning_effort`` sent, cap 180 → the fake
    returns the empty-content ``finish_reason="length"`` prod response).
    """
    from bot.services.llm_providers.openai_vision import OpenAIVisionProvider

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        effort = payload.get("reasoning_effort") or "medium"  # server-side default
        cap = int(payload.get("max_completion_tokens") or 0)
        # Emulated reasoning spend: medium effort needs ~1024 reasoning tokens
        # for an image description; minimal needs ~32. Output needs ~80.
        reasoning = 1_024 if effort != "minimal" else 32
        if cap and reasoning + 80 > cap:
            return httpx.Response(200, json=_reasoning_exhausted_response(cap))
        return httpx.Response(
            200,
            headers={"x-request-id": "vision-req-2"},
            json={
                "id": "chatcmpl-vision-ok",
                "object": "chat.completion",
                "model": "gpt-5-nano",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "На фото скриншот чата.",
                            "refusal": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 312,
                    "completion_tokens": reasoning + 80,
                    "total_tokens": 312 + reasoning + 80,
                    "completion_tokens_details": {"reasoning_tokens": reasoning},
                },
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
            caption=None,
            model="gpt-5-nano",
        )

    assert result.description == "На фото скриншот чата."
    # completion_tokens includes reasoning tokens — billed as output.
    assert result.tokens_out == 32 + 80


@pytest.mark.parametrize("empty_content", ["", None])
@pytest.mark.asyncio
async def test_openai_vision_provider_reasoning_exhausted_content_still_fails_closed(
    empty_content,
) -> None:
    """The fail-closed contract is preserved: an empty/null ``content`` (the
    documented reasoning-exhaustion shape) remains a contract_violation."""
    from bot.services.llm_providers import ProviderStructuralError
    from bot.services.llm_providers.openai_vision import OpenAIVisionProvider

    def handle(request: httpx.Request) -> httpx.Response:
        body = _reasoning_exhausted_response(2_048)
        body["choices"][0]["message"]["content"] = empty_content
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.openai.com/v1",
    ) as client:
        provider = OpenAIVisionProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderStructuralError) as exc_info:
            await provider.describe(
                image_bytes=b"jpeg-bytes",
                mime_type="image/jpeg",
                caption=None,
                model="gpt-5-nano",
            )
    assert exc_info.value.subtype == "contract_violation"


@pytest.mark.asyncio
async def test_openai_vision_provider_omits_reasoning_effort_for_plain_models() -> None:
    """``reasoning_effort`` is only valid on reasoning models; sending it to a
    non-reasoning model (e.g. gpt-4o-mini, also priced) would 400."""
    from bot.services.llm_providers.openai_vision import OpenAIVisionProvider

    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-plain",
                "choices": [{"message": {"content": "Описание."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.openai.com/v1",
    ) as client:
        provider = OpenAIVisionProvider(api_key="test-key", client=client)
        await provider.describe(
            image_bytes=b"jpeg-bytes",
            mime_type="image/jpeg",
            caption=None,
            model="gpt-4o-mini",
        )

    assert "reasoning_effort" not in captured["payload"]


def test_vision_completion_cap_matches_gateway_reservation() -> None:
    """The durable reservation must cover the adapter's worst-case output."""
    from bot.services.llm_gateway import MAX_VISION_RESERVED_OUTPUT_TOKENS
    from bot.services.llm_providers.openai_vision import MAX_COMPLETION_TOKENS

    assert MAX_COMPLETION_TOKENS == MAX_VISION_RESERVED_OUTPUT_TOKENS
