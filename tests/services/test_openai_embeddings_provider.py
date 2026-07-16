from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from typing import Any, Callable

import httpx
import pytest

from bot.services.llm_providers import ProviderStructuralError, ProviderTransientError
from bot.services.llm_providers.openai_embeddings import (
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    MAX_EMBEDDING_BATCH_CHARS,
    MAX_EMBEDDING_BATCH_SIZE,
    MAX_EMBEDDING_INPUT_CHARS,
    OPENAI_EMBEDDING_DIMENSIONS,
    OpenAIEmbeddingsProvider,
)


def _vector(value: int | float = 0.125) -> list[int | float]:
    return [value] * OPENAI_EMBEDDING_DIMENSIONS


def _response_payload(count: int = 1) -> dict[str, Any]:
    return {
        "object": "list",
        "model": DEFAULT_OPENAI_EMBEDDING_MODEL,
        "data": [
            {
                "object": "embedding",
                "index": index,
                "embedding": _vector(index + 0.25),
            }
            for index in range(count)
        ],
        "usage": {"prompt_tokens": 17, "total_tokens": 17},
    }


@pytest.mark.asyncio
async def test_embed_sends_bounded_ordered_request_and_returns_frozen_result() -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "emb-req-1"},
            json=_response_payload(count=2),
        )

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.openai.com/v1/",
    ) as client:
        provider = OpenAIEmbeddingsProvider(api_key="test-key", client=client)
        result = await provider.embed(inputs=("first", "second"))
        assert not client.is_closed

    assert captured == {
        "path": "/v1/embeddings",
        "auth": "Bearer test-key",
        "payload": {
            "model": DEFAULT_OPENAI_EMBEDDING_MODEL,
            "input": ["first", "second"],
            "dimensions": OPENAI_EMBEDDING_DIMENSIONS,
            "encoding_format": "float",
        },
    }
    assert len(result.vectors) == 2
    assert all(len(vector) == OPENAI_EMBEDDING_DIMENSIONS for vector in result.vectors)
    assert result.vectors[0][0] == 0.25
    assert result.vectors[1][0] == 1.25
    assert isinstance(result.vectors, tuple)
    assert isinstance(result.vectors[0], tuple)
    assert result.tokens_in == 17
    assert result.request_id == "emb-req-1"
    assert result.raw_latency_ms >= 0
    with pytest.raises(FrozenInstanceError):
        result.tokens_in = 18  # type: ignore[misc]


@pytest.mark.asyncio
async def test_embed_reads_existing_openai_api_key_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_auth: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_response_payload())

    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://api.openai.com/v1/",
    ) as client:
        result = await OpenAIEmbeddingsProvider(client=client).embed(inputs=["text"])

    assert captured_auth == ["Bearer environment-key"]
    assert result.request_id == ""


@pytest.mark.asyncio
async def test_embed_missing_api_key_is_structural_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ProviderStructuralError) as caught:
        await OpenAIEmbeddingsProvider().embed(inputs=["text"])

    assert caught.value.subtype == "auth"


@pytest.mark.parametrize(
    ("inputs", "model", "dimensions", "message"),
    [
        ("raw string", DEFAULT_OPENAI_EMBEDDING_MODEL, 1536, "sequence of strings"),
        (b"bytes", DEFAULT_OPENAI_EMBEDDING_MODEL, 1536, "sequence of strings"),
        ([], DEFAULT_OPENAI_EMBEDDING_MODEL, 1536, "must not be empty"),
        (["  \n"], DEFAULT_OPENAI_EMBEDDING_MODEL, 1536, "must not be blank"),
        ([1], DEFAULT_OPENAI_EMBEDDING_MODEL, 1536, "must be a string"),
        (
            ["x"] * (MAX_EMBEDDING_BATCH_SIZE + 1),
            DEFAULT_OPENAI_EMBEDDING_MODEL,
            1536,
            "batch exceeds",
        ),
        (
            ["x" * (MAX_EMBEDDING_INPUT_CHARS + 1)],
            DEFAULT_OPENAI_EMBEDDING_MODEL,
            1536,
            "input at index 0 exceeds",
        ),
        (
            ["x" * MAX_EMBEDDING_INPUT_CHARS]
            * (MAX_EMBEDDING_BATCH_CHARS // MAX_EMBEDDING_INPUT_CHARS + 1),
            DEFAULT_OPENAI_EMBEDDING_MODEL,
            1536,
            "batch exceeds",
        ),
        (["text"], "text-embedding-ada-002", 1536, "unsupported"),
        (["text"], DEFAULT_OPENAI_EMBEDDING_MODEL, 512, "must equal"),
        (["text"], DEFAULT_OPENAI_EMBEDDING_MODEL, True, "must equal"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_network_without_content_leakage(
    inputs: Any,
    model: str,
    dimensions: Any,
    message: str,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://api.openai.com/v1/",
    ) as client:
        provider = OpenAIEmbeddingsProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderStructuralError, match=message) as caught:
            await provider.embed(inputs=inputs, model=model, dimensions=dimensions)

    assert caught.value.subtype in {"bad_request", "model_not_found"}
    assert "raw string" not in str(caught.value)
    assert calls == 0


@pytest.mark.parametrize(
    ("status", "error_type", "subtype"),
    [
        (302, ProviderStructuralError, "bad_request"),
        (400, ProviderStructuralError, "bad_request"),
        (401, ProviderStructuralError, "auth"),
        (403, ProviderStructuralError, "auth"),
        (404, ProviderStructuralError, "model_not_found"),
        (422, ProviderStructuralError, "bad_request"),
        (429, ProviderTransientError, "rate_limit"),
        (500, ProviderTransientError, "5xx"),
        (503, ProviderTransientError, "5xx"),
    ],
)
@pytest.mark.asyncio
async def test_http_errors_are_classified_without_retry(
    status: int,
    error_type: type[ProviderStructuralError] | type[ProviderTransientError],
    subtype: str,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "must not leak"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://api.openai.com/v1/",
    ) as client:
        provider = OpenAIEmbeddingsProvider(api_key="test-key", client=client)
        with pytest.raises(error_type) as caught:
            await provider.embed(inputs=["private input"])

    assert caught.value.subtype == subtype
    assert "private input" not in str(caught.value)
    assert "must not leak" not in str(caught.value)
    assert calls == 1


@pytest.mark.parametrize(
    ("transport_error_factory", "subtype"),
    [
        (lambda request: httpx.ReadTimeout("transport failed", request=request), "timeout"),
        (
            lambda request: httpx.ConnectError("transport failed", request=request),
            "connection_reset",
        ),
        (
            lambda request: httpx.RemoteProtocolError("transport failed", request=request),
            "connection_reset",
        ),
    ],
)
@pytest.mark.asyncio
async def test_transport_errors_are_transient_without_retry(
    transport_error_factory: Callable[[httpx.Request], httpx.TransportError],
    subtype: str,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise transport_error_factory(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://api.openai.com/v1/",
    ) as client:
        provider = OpenAIEmbeddingsProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderTransientError) as caught:
            await provider.embed(inputs=["private input"])

    assert caught.value.subtype == subtype
    assert "private input" not in str(caught.value)
    assert calls == 1


ResponseMutation = Callable[[dict[str, Any]], None]


def _set_object(payload: dict[str, Any]) -> None:
    payload["object"] = "embedding"


def _set_model(payload: dict[str, Any]) -> None:
    payload["model"] = "unexpected-model"


def _remove_embedding(payload: dict[str, Any]) -> None:
    payload["data"] = []


def _reverse_order(payload: dict[str, Any]) -> None:
    payload["data"][0]["index"] = 1


def _set_item_object(payload: dict[str, Any]) -> None:
    payload["data"][0]["object"] = "other"


def _set_bool_index(payload: dict[str, Any]) -> None:
    payload["data"][0]["index"] = False


def _set_short_vector(payload: dict[str, Any]) -> None:
    payload["data"][0]["embedding"] = _vector()[:-1]


def _set_long_vector(payload: dict[str, Any]) -> None:
    payload["data"][0]["embedding"] = _vector() + [0.0]


def _set_vector_value(value: object) -> ResponseMutation:
    def mutate(payload: dict[str, Any]) -> None:
        payload["data"][0]["embedding"][0] = value

    return mutate


def _remove_usage(payload: dict[str, Any]) -> None:
    payload.pop("usage")


def _set_prompt_tokens(value: object) -> ResponseMutation:
    def mutate(payload: dict[str, Any]) -> None:
        payload["usage"]["prompt_tokens"] = value

    return mutate


def _set_total_tokens(value: object) -> ResponseMutation:
    def mutate(payload: dict[str, Any]) -> None:
        payload["usage"]["total_tokens"] = value

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        _set_object,
        _set_model,
        _remove_embedding,
        _reverse_order,
        _set_item_object,
        _set_bool_index,
        _set_short_vector,
        _set_long_vector,
        _set_vector_value("0.125"),
        _set_vector_value(True),
        _set_vector_value(float("nan")),
        _set_vector_value(float("inf")),
        _set_vector_value(float("-inf")),
        _set_vector_value(10**1000),
        _remove_usage,
        _set_prompt_tokens(True),
        _set_prompt_tokens(-1),
        _set_total_tokens(False),
        _set_total_tokens(16),
    ],
)
@pytest.mark.asyncio
async def test_response_contract_violations_are_structural(
    mutate: ResponseMutation,
) -> None:
    payload = _response_payload()
    mutate(payload)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload, allow_nan=True).encode(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://api.openai.com/v1/",
    ) as client:
        provider = OpenAIEmbeddingsProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderStructuralError) as caught:
            await provider.embed(inputs=["private input"])

    assert caught.value.subtype == "contract_violation"
    assert "private input" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"null",
    ],
)
@pytest.mark.asyncio
async def test_malformed_response_body_is_contract_violation(body: bytes) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://api.openai.com/v1/",
    ) as client:
        provider = OpenAIEmbeddingsProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderStructuralError) as caught:
            await provider.embed(inputs=["text"])

    assert caught.value.subtype == "contract_violation"
