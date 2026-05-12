"""Phase 11 follow-up #224 High #5 — httpx URL/domain-level runtime guard.

This module is imported by both ``tests/evals/conftest.py`` (which installs
the guard as an autouse fixture) and ``tests/evals/test_no_llm_network.py``
(which exercises the guard's observable behaviour).

Keeping the guard logic in a dedicated module ensures that both sides import
the same ``LLMNetworkCallDetected`` class object — avoiding the identity
mismatch that occurs when conftest.py is loaded under the ``conftest`` module
name while the test imports it under ``tests.evals.conftest``.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# LLM hostname block-list (mirrors LLM_PROVIDER_HOSTNAMES in
# tests/evals/test_no_llm_imports.py — keep in sync).
# ---------------------------------------------------------------------------

LLM_GUARD_HOSTNAMES: frozenset[str] = frozenset(
    [
        "api.anthropic.com",
        "api.openai.com",
        "api.cohere.ai",
        "api.cohere.com",
        "api.mistral.ai",
        "generativelanguage.googleapis.com",
        "api.replicate.com",
        "api-inference.huggingface.co",
    ]
)


class LLMNetworkCallDetected(AssertionError):
    """Raised when eval-mode code makes a direct httpx call to an LLM endpoint.

    Subclasses ``AssertionError`` so pytest surfaces it as a clear test
    failure rather than an unexpected exception.
    """


def _check_llm_hostname(request: Any, enabled: bool) -> None:
    """Shared guard logic for both sync and async hooks.

    Raises ``LLMNetworkCallDetected`` when *enabled* and the request targets a
    known LLM provider hostname.  Returns ``None`` otherwise (safe for both
    sync and async hook paths).
    """
    if not enabled:
        return None
    host = urlparse(str(request.url)).hostname or ""
    if host in LLM_GUARD_HOSTNAMES:
        raise LLMNetworkCallDetected(
            f"[eval-guard] Direct httpx call to LLM endpoint blocked: "
            f"{host!r} — all LLM calls must go through bot.services.llm_gateway. "
            f"Full URL: {request.url}"
        )
    return None


def make_llm_guard_hook() -> Any:
    """Return a **synchronous** httpx event-hook for use with ``httpx.Client``.

    When ``EVAL_HARNESS_ENABLED`` is absent or falsy the returned callable is
    a no-op (returns ``None`` for every request).  This ensures the production
    gateway — which legitimately calls httpx to LLM endpoints — is unaffected
    in non-eval mode.

    When ``EVAL_HARNESS_ENABLED`` is truthy the hook inspects the request URL
    and raises ``LLMNetworkCallDetected`` for any host in
    ``LLM_GUARD_HOSTNAMES`` before any TCP connection is attempted.

    .. note::
        ``httpx.Client`` calls ``hook(request)`` (sync call).
        ``httpx.AsyncClient`` calls ``await hook(request)`` (coroutine required).
        Use :func:`make_async_llm_guard_hook` for ``AsyncClient`` instances.
    """
    enabled = bool(os.environ.get("EVAL_HARNESS_ENABLED"))

    def _hook(request: Any) -> None:
        return _check_llm_hostname(request, enabled)

    return _hook


def make_async_llm_guard_hook() -> Any:
    """Return an **async** httpx event-hook for use with ``httpx.AsyncClient``.

    ``httpx.AsyncClient`` executes request hooks via ``await hook(request)``.
    A synchronous function cannot be awaited — returning ``None`` from a sync
    hook causes ``TypeError: 'NoneType' object can't be awaited`` on any
    non-LLM async request.  This factory returns a proper ``async def`` hook
    so that both LLM-blocked and non-LLM (passthrough) paths are safe.

    Same enable/disable semantics as :func:`make_llm_guard_hook`.
    """
    enabled = bool(os.environ.get("EVAL_HARNESS_ENABLED"))

    async def _async_hook(request: Any) -> None:
        return _check_llm_hostname(request, enabled)

    return _async_hook
