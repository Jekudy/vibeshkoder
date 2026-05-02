"""OpenAI fallback provider for `bot.services.llm_gateway`.

Phase 5 / T5-01. Gated behind ``SYNTHESIS_PROVIDER=openai`` env / config; the
default Phase 5 provider is :mod:`bot.services.llm_providers.anthropic`. SDK
import is lazy so the package stays importable without the optional dep.
"""

from __future__ import annotations

from . import (
    LLMProvider,
    ProviderResult,
    ProviderStructuralError,
    ProviderTransientError,
)

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


class OpenAIProvider:
    """OpenAI SDK adapter satisfying the ``LLMProvider`` Protocol."""

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        """Dispatch a single prompt to the OpenAI Responses API."""
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — env guard, not behaviour
            raise ProviderStructuralError(
                "model_not_found",
                message=f"openai SDK not installed: {exc}",
            ) from exc

        try:
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

        text = getattr(response, "output_text", "") or ""
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
        request_id = getattr(response, "id", "") or ""
        return ProviderResult(
            answer_text=text,
            citation_ids=tuple(),  # T5-04 populates from prompt-template envelope
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            request_id=str(request_id),
            raw_latency_ms=0,
        )


__all__ = ["DEFAULT_OPENAI_MODEL", "LLMProvider", "OpenAIProvider"]
