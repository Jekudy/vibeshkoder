"""DeepSeek Chat Completions adapter for the shared LLM gateway."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from . import (
    ProviderResult,
    ProviderStructuralError,
    ProviderTransientError,
)

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_MAX_TOKENS = 512
MAX_DEEPSEEK_OUTPUT_TOKENS = 384_000
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_MV_CITATION_RE = re.compile(r"\[\[mv:(\d+)\]\]")


class DeepSeekProvider:
    """Text-only DeepSeek provider using the OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        max_tokens: int = DEFAULT_DEEPSEEK_MAX_TOKENS,
        json_output: bool = False,
    ) -> None:
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_DEEPSEEK_OUTPUT_TOKENS:
            raise ValueError(f"max_tokens must be between 1 and {MAX_DEEPSEEK_OUTPUT_TOKENS}")
        if type(json_output) is not bool:
            raise ValueError("json_output must be a boolean")
        self._api_key = api_key
        self._client = client
        self._max_tokens = max_tokens
        self._json_output = json_output

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        api_key = self._api_key
        if api_key is None:
            api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ProviderStructuralError(
                "auth",
                message="DEEPSEEK_API_KEY is missing",
            )

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=DEEPSEEK_BASE_URL,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        started = time.monotonic()
        request_payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "disabled"},
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        if self._json_output:
            request_payload["response_format"] = {"type": "json_object"}
        try:
            response = await client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_payload,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError("timeout", message="DeepSeek request timed out") from exc
        except httpx.NetworkError as exc:
            raise ProviderTransientError(
                "connection_reset",
                message="DeepSeek network request failed",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        self._raise_for_status(response)
        try:
            payload: dict[str, Any] = response.json()
            answer_text = payload["choices"][0]["message"]["content"]
            usage = payload["usage"]
            if not isinstance(answer_text, str):
                raise TypeError("content must be a string")
            if not isinstance(usage, dict):
                raise TypeError("usage must be an object")
            tokens_in = usage["prompt_tokens"]
            tokens_out = usage["completion_tokens"]
            if (
                type(tokens_in) is not int
                or type(tokens_out) is not int
                or tokens_in < 0
                or tokens_out < 0
            ):
                raise TypeError("usage token counts must be non-negative integers")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderStructuralError(
                "contract_violation",
                message="DeepSeek response did not match the chat completion contract",
            ) from exc

        citation_ids = tuple(
            dict.fromkeys(int(value) for value in _MV_CITATION_RE.findall(answer_text))
        )
        request_id = str(payload.get("id") or response.headers.get("x-request-id") or "")
        return ProviderResult(
            answer_text=answer_text,
            citation_ids=citation_ids,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            request_id=request_id,
            raw_latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status in (401, 403):
            raise ProviderStructuralError("auth", message="DeepSeek authentication failed")
        if status == 404:
            raise ProviderStructuralError(
                "model_not_found",
                message="DeepSeek endpoint or model was not found",
            )
        if status == 429:
            raise ProviderTransientError("rate_limit", message="DeepSeek rate limit exceeded")
        if status >= 500:
            raise ProviderTransientError("5xx", message="DeepSeek service unavailable")
        raise ProviderStructuralError("bad_request", message="DeepSeek rejected the request")


__all__ = [
    "DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_MAX_TOKENS",
    "DEFAULT_DEEPSEEK_MODEL",
    "MAX_DEEPSEEK_OUTPUT_TOKENS",
    "DeepSeekProvider",
]
