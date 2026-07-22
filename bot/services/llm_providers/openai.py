"""OpenAI fallback provider for `bot.services.llm_gateway`.

Phase 5 / T5-01. Gated behind ``SYNTHESIS_PROVIDER=openai`` env / config; the
default Phase 5 provider is :mod:`bot.services.llm_providers.anthropic`. SDK
import is lazy so the package stays importable without the optional dep.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from . import (
    LLMProvider,
    ProviderResult,
    ProviderStructuralError,
    ProviderTransientError,
)

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class OpenAIProvider:
    """OpenAI SDK adapter satisfying the ``LLMProvider`` Protocol."""

    def __init__(self, *, api_key: str | None = None, transport: Any | None = None) -> None:
        self._api_key = api_key
        self._transport = transport

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        """Dispatch a single prompt to the OpenAI Responses API."""
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — env guard, not behaviour
            raise ProviderStructuralError(
                "model_not_found",
                message=f"openai SDK not installed: {exc}",
            ) from exc

        try:  # pragma: no cover — SDK call only exercised in real-API tests
            client = openai.AsyncOpenAI(api_key=self._api_key)  # type: ignore[attr-defined]
            response = await client.responses.create(  # type: ignore[no-untyped-call]
                model=model,
                input=prompt,
            )
        except Exception as exc:  # pragma: no cover — exercised only in real-API tests
            err_name = type(exc).__name__
            if "RateLimit" in err_name:
                raise ProviderTransientError(
                    "rate_limit", message=str(exc)
                ) from exc
            if "Timeout" in err_name:
                raise ProviderTransientError(
                    "timeout", message=str(exc)
                ) from exc
            if "Connection" in err_name:
                raise ProviderTransientError(
                    "connection_reset", message=str(exc)
                ) from exc
            if "Authentication" in err_name or "PermissionDenied" in err_name:
                raise ProviderStructuralError(
                    "auth", message=str(exc)
                ) from exc
            if "BadRequest" in err_name or "InvalidRequest" in err_name:
                raise ProviderStructuralError(
                    "bad_request", message=str(exc)
                ) from exc
            if "NotFound" in err_name:
                raise ProviderStructuralError(
                    "model_not_found", message=str(exc)
                ) from exc
            if "InternalServerError" in err_name or "ServerError" in err_name:
                raise ProviderTransientError(
                    "5xx", message=str(exc)
                ) from exc
            raise

        # pragma: no cover — response-parsing reached only with real SDK.
        text = getattr(response, "output_text", "") or ""  # pragma: no cover
        usage = getattr(response, "usage", None)  # pragma: no cover
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0  # pragma: no cover
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0  # pragma: no cover
        request_id = getattr(response, "id", "") or ""  # pragma: no cover
        return ProviderResult(  # pragma: no cover
            answer_text=text,
            citation_ids=tuple(),  # T5-04 populates from prompt-template envelope
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            request_id=str(request_id),
            raw_latency_ms=0,
        )

    async def call_structured(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str,
        schema_name: str,
        json_schema: Mapping[str, Any],
        reasoning_effort: str,
    ) -> ProviderResult:
        """Call Responses Structured Outputs without truncation or storage."""
        import httpx

        api_key = self._api_key
        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY")
        if api_key is None or not api_key.strip():
            raise ProviderStructuralError("auth", message="OPENAI_API_KEY is required")
        request_json = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(json_schema),
                }
            },
            "reasoning": {"effort": reasoning_effort},
            "truncation": "disabled",
            "store": False,
        }
        timeout = httpx.Timeout(180.0, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                http_response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=request_json,
                )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError("timeout", message="digest request timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                "connection_reset", message="digest connection failed"
            ) from exc

        status_code = http_response.status_code
        if status_code in (401, 403):
            raise ProviderStructuralError("auth", message="digest provider auth failed")
        if status_code == 404:
            raise ProviderStructuralError("model_not_found", message="digest model not found")
        if status_code == 429:
            raise ProviderTransientError("rate_limit", message="digest rate limited")
        if status_code >= 500:
            raise ProviderTransientError("5xx", message="digest provider failed")
        if status_code >= 400:
            raise ProviderStructuralError("bad_request", message="digest request was rejected")
        try:
            response = http_response.json()
        except ValueError as exc:
            raise ProviderStructuralError(
                "contract_violation", message="digest response was not JSON"
            ) from exc

        if _field(response, "status") != "completed" or _field(response, "error") is not None:
            raise ProviderStructuralError(
                "contract_violation", message="digest response did not complete"
            )
        if _field(response, "incomplete_details") is not None:
            raise ProviderStructuralError(
                "contract_violation", message="digest response was incomplete"
            )

        output_parts: list[str] = []
        for item in _field(response, "output") or []:
            if _field(item, "type") != "message":
                continue
            for content in _field(item, "content") or []:
                content_type = _field(content, "type")
                if content_type == "refusal" or _field(content, "refusal"):
                    raise ProviderStructuralError(
                        "contract_violation", message="digest response was refused"
                    )
                if content_type == "output_text":
                    value = _field(content, "text")
                    if isinstance(value, str):
                        output_parts.append(value)
        answer_text = "".join(output_parts)
        if not answer_text:
            raise ProviderStructuralError(
                "contract_violation", message="digest response contained no output text"
            )

        usage = _field(response, "usage")
        return ProviderResult(
            answer_text=answer_text,
            citation_ids=(),
            tokens_in=int(_field(usage, "input_tokens") or 0),
            tokens_out=int(_field(usage, "output_tokens") or 0),
            request_id=str(_field(response, "id") or ""),
            raw_latency_ms=0,
        )


def _field(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = ["DEFAULT_OPENAI_MODEL", "LLMProvider", "OpenAIProvider"]
