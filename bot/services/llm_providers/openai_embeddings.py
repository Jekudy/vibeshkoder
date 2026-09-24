"""Strict OpenAI embeddings adapter for semantic community-memory retrieval."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from . import ProviderStructuralError, ProviderTransientError


DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDINGS_BASE_URL = "https://api.openai.com/v1/"
OPENAI_EMBEDDING_DIMENSIONS = 1536

# Product-side request bounds.  They deliberately stay below the provider's
# account-dependent limits so one malformed retrieval unit cannot create an
# unbounded payload or monopolise a worker.
MAX_EMBEDDING_BATCH_SIZE = 100
MAX_EMBEDDING_INPUT_CHARS = 8_000
MAX_EMBEDDING_BATCH_CHARS = 64_000


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    tokens_in: int
    request_id: str
    raw_latency_ms: int


class OpenAIEmbeddingsProvider:
    """Create ordered 1536-dimensional embeddings without retries or fallback."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client

    async def embed(
        self,
        *,
        inputs: Sequence[str],
        model: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dimensions: int = OPENAI_EMBEDDING_DIMENSIONS,
    ) -> EmbeddingResult:
        normalized_inputs = self._validate_request(
            inputs=inputs,
            model=model,
            dimensions=dimensions,
        )

        api_key = self._api_key
        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ProviderStructuralError("auth", message="OPENAI_API_KEY is missing")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=OPENAI_EMBEDDINGS_BASE_URL,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        started = time.monotonic()
        try:
            response = await client.post(
                "embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "input": list(normalized_inputs),
                    "dimensions": dimensions,
                    "encoding_format": "float",
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                "timeout",
                message="OpenAI embeddings request timed out",
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                "connection_reset",
                message="OpenAI embeddings network request failed",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        self._raise_for_status(response)
        vectors, tokens_in = self._parse_response(
            response=response,
            expected_count=len(normalized_inputs),
            expected_model=model,
            dimensions=dimensions,
        )
        return EmbeddingResult(
            vectors=vectors,
            tokens_in=tokens_in,
            request_id=response.headers.get("x-request-id", ""),
            raw_latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _validate_request(
        *,
        inputs: Sequence[str],
        model: str,
        dimensions: int,
    ) -> tuple[str, ...]:
        if isinstance(inputs, (str, bytes)) or not isinstance(inputs, Sequence):
            raise ProviderStructuralError(
                "bad_request",
                message="embedding inputs must be a sequence of strings",
            )
        values = tuple(inputs)
        if not values:
            raise ProviderStructuralError(
                "bad_request",
                message="embedding inputs must not be empty",
            )
        if len(values) > MAX_EMBEDDING_BATCH_SIZE:
            raise ProviderStructuralError(
                "bad_request",
                message=f"embedding batch exceeds {MAX_EMBEDDING_BATCH_SIZE} inputs",
            )
        if model != DEFAULT_OPENAI_EMBEDDING_MODEL:
            raise ProviderStructuralError(
                "model_not_found",
                message="unsupported OpenAI embedding model",
            )
        if type(dimensions) is not int or dimensions != OPENAI_EMBEDDING_DIMENSIONS:
            raise ProviderStructuralError(
                "bad_request",
                message=f"embedding dimensions must equal {OPENAI_EMBEDDING_DIMENSIONS}",
            )

        total_chars = 0
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise ProviderStructuralError(
                    "bad_request",
                    message=f"embedding input at index {index} must be a string",
                )
            if not value.strip():
                raise ProviderStructuralError(
                    "bad_request",
                    message=f"embedding input at index {index} must not be blank",
                )
            if len(value) > MAX_EMBEDDING_INPUT_CHARS:
                raise ProviderStructuralError(
                    "bad_request",
                    message=(
                        f"embedding input at index {index} exceeds "
                        f"{MAX_EMBEDDING_INPUT_CHARS} characters"
                    ),
                )
            total_chars += len(value)

        if total_chars > MAX_EMBEDDING_BATCH_CHARS:
            raise ProviderStructuralError(
                "bad_request",
                message=f"embedding batch exceeds {MAX_EMBEDDING_BATCH_CHARS} characters",
            )
        return values

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise ProviderStructuralError("auth", message="OpenAI authentication failed")
        if status == 404:
            raise ProviderStructuralError(
                "model_not_found",
                message="OpenAI embeddings endpoint or model was not found",
            )
        if status == 429:
            raise ProviderTransientError("rate_limit", message="OpenAI rate limit exceeded")
        if status >= 500:
            raise ProviderTransientError("5xx", message="OpenAI service unavailable")
        raise ProviderStructuralError(
            "bad_request",
            message="OpenAI rejected embeddings request",
        )

    @staticmethod
    def _parse_response(
        *,
        response: httpx.Response,
        expected_count: int,
        expected_model: str,
        dimensions: int,
    ) -> tuple[tuple[tuple[float, ...], ...], int]:
        try:
            body: Any = response.json()
            if not isinstance(body, dict) or body.get("object") != "list":
                raise TypeError("response must be an embedding list")
            if body.get("model") != expected_model:
                raise ValueError("response model does not match request")

            data = body.get("data")
            if not isinstance(data, list) or len(data) != expected_count:
                raise ValueError("response embedding count does not match request")

            parsed_vectors: list[tuple[float, ...]] = []
            for expected_index, item in enumerate(data):
                if not isinstance(item, dict) or item.get("object") != "embedding":
                    raise TypeError("response item must be an embedding object")
                index = item.get("index")
                if type(index) is not int or index != expected_index:
                    raise ValueError("response embedding order does not match request")
                raw_vector = item.get("embedding")
                if not isinstance(raw_vector, list) or len(raw_vector) != dimensions:
                    raise ValueError("response embedding dimensions do not match request")

                vector: list[float] = []
                for value in raw_vector:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise TypeError("embedding value must be numeric")
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        raise ValueError("embedding value must be finite")
                    vector.append(numeric)
                parsed_vectors.append(tuple(vector))

            usage = body.get("usage")
            if not isinstance(usage, dict):
                raise TypeError("response usage must be an object")
            tokens_in = usage.get("prompt_tokens")
            total_tokens = usage.get("total_tokens")
            if type(tokens_in) is not int or tokens_in < 0:
                raise ValueError("prompt_tokens must be a non-negative integer")
            if type(total_tokens) is not int or total_tokens < tokens_in:
                raise ValueError("total_tokens must be an integer not below prompt_tokens")
        except (OverflowError, TypeError, ValueError) as exc:
            raise ProviderStructuralError(
                "contract_violation",
                message="OpenAI embeddings response did not match the expected contract",
            ) from exc

        return tuple(parsed_vectors), tokens_in


__all__ = [
    "DEFAULT_OPENAI_EMBEDDING_MODEL",
    "EmbeddingResult",
    "MAX_EMBEDDING_BATCH_CHARS",
    "MAX_EMBEDDING_BATCH_SIZE",
    "MAX_EMBEDDING_INPUT_CHARS",
    "OPENAI_EMBEDDING_DIMENSIONS",
    "OPENAI_EMBEDDINGS_BASE_URL",
    "OpenAIEmbeddingsProvider",
]
