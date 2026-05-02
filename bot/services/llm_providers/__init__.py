"""LLM provider abstraction for `bot.services.llm_gateway`.

Phase 5 / T5-01. Defines the ``LLMProvider`` Protocol, the immutable
``ProviderResult`` returned by every provider, and the two error taxonomies
the gateway uses to categorise failures (``ProviderTransientError`` and
``ProviderStructuralError``). Any exception not matching one of these
taxonomies is treated as ``provider_unknown:<class>`` by the gateway.

Per HANDOFF §1 invariant #2, ALL Anthropic / OpenAI client imports MUST stay
inside this sub-package. ``bot/services/llm_gateway.py`` only depends on the
Protocol surface defined here.
"""

from __future__ import annotations

from typing import Literal, NamedTuple, Protocol, runtime_checkable


class ProviderResult(NamedTuple):
    """Immutable provider output passed back to the gateway."""

    answer_text: str
    citation_ids: tuple[int, ...]
    tokens_in: int
    tokens_out: int
    request_id: str
    raw_latency_ms: int


TransientSubtype = Literal["rate_limit", "timeout", "5xx", "connection_reset"]
StructuralSubtype = Literal[
    "auth", "bad_request", "contract_violation", "model_not_found"
]


class ProviderError(Exception):
    """Base class for provider error taxonomies."""

    def __init__(self, subtype: str, *, message: str) -> None:
        super().__init__(message)
        self.subtype = subtype


class ProviderTransientError(ProviderError):
    """Transient provider failure — gateway abstains, NEVER raises into handler.

    Subtypes: ``rate_limit``, ``timeout``, ``5xx``, ``connection_reset``.
    """


class ProviderStructuralError(ProviderError):
    """Structural provider failure — gateway abstains AND emits a stop signal.

    Subtypes: ``auth``, ``bad_request``, ``contract_violation``, ``model_not_found``.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol every provider implementation must satisfy."""

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        ...


__all__ = [
    "LLMProvider",
    "ProviderError",
    "ProviderResult",
    "ProviderStructuralError",
    "ProviderTransientError",
    "StructuralSubtype",
    "TransientSubtype",
]
