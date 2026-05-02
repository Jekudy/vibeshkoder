"""Anthropic provider for `bot.services.llm_gateway`.

Phase 5 / T5-01. Default Phase 5 provider, model
``claude-haiku-4-5-20251001`` (verified against Anthropic Models overview
2026-05-02 — passes Stop Signal #10).

The actual SDK is imported lazily inside ``call`` so unit tests can run
without the optional dep (anthropic isn't in the project's runtime deps yet;
it lands with deployment wiring in T5-04). Tests in
``tests/services/test_llm_providers.py`` and
``tests/services/test_llm_gateway.py`` use ``pytest``-injected fakes that
satisfy the ``LLMProvider`` Protocol — no real network calls are made.
"""

from __future__ import annotations

from . import (
    LLMProvider,
    ProviderResult,
    ProviderStructuralError,
    ProviderTransientError,
)

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


class AnthropicProvider:
    """Anthropic SDK adapter satisfying the ``LLMProvider`` Protocol.

    The class instance is intentionally cheap to construct so the gateway can
    spin one up per call. The SDK client is created lazily inside ``call`` so
    a missing ``anthropic`` package only fails at runtime, not at import time.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        """Dispatch a single prompt to the Anthropic Messages API.

        Raises:
            ProviderStructuralError: auth / bad_request / model_not_found.
            ProviderTransientError: rate_limit / timeout / 5xx / connection_reset.
        """
        # Lazy import: keeps the package importable in environments that don't
        # ship the anthropic SDK (CI unit tests for T5-01 don't need it; T5-04
        # adds the dep in pyproject.toml).
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — env guard, not behaviour
            raise ProviderStructuralError(
                "model_not_found",
                message=f"anthropic SDK not installed: {exc}",
            ) from exc

        try:
            client = anthropic.AsyncAnthropic(api_key=self._api_key)  # type: ignore[attr-defined]
            response = await client.messages.create(  # type: ignore[no-untyped-call]
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # pragma: no cover — exercised only in real-API tests
            # Map known SDK error classes back into the gateway taxonomy. The
            # gateway treats any non-taxonomy exception as ``provider_unknown``.
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
            if "ServerError" in err_name or "InternalServerError" in err_name:
                raise ProviderTransientError(
                    "5xx", message=str(exc)
                ) from exc
            raise

        # Anthropic Messages API returns a list of content blocks; the gateway
        # contract expects ProviderResult with ``citation_ids`` already parsed.
        # T5-04 will define the prompt template that asks the model to return
        # citations as a JSON envelope; for T5-01 the parser is intentionally
        # minimal and tests inject fakes with the citation list pre-populated.
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
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


__all__ = ["AnthropicProvider", "DEFAULT_ANTHROPIC_MODEL", "LLMProvider"]
