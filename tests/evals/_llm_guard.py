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

from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# LLM hostname block-list — single source of truth.
#
# LLM_PROVIDER_HOSTNAMES is the canonical frozenset used by:
#   - the runtime httpx guard (LLM_GUARD_HOSTNAMES alias below)
#   - the static AST/text scan in tests/evals/test_no_llm_imports.py
#     (imported directly as LLM_PROVIDER_HOSTNAMES)
#
# Adding a new LLM provider? Add it here only — both guards pick it up.
# ---------------------------------------------------------------------------

LLM_PROVIDER_HOSTNAMES: frozenset[str] = frozenset(
    [
        "api.anthropic.com",
        "api.openai.com",
        "api.deepseek.com",
        "generativelanguage.googleapis.com",
        "api.together.xyz",
        "api.groq.com",
        "api.mistral.ai",
        "api.cohere.ai",
        "api.cohere.com",
        "api.replicate.com",
        "api-inference.huggingface.co",
    ]
)

# Alias kept for backwards-compat with existing callers that import
# LLM_GUARD_HOSTNAMES directly.
LLM_GUARD_HOSTNAMES: frozenset[str] = LLM_PROVIDER_HOSTNAMES


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

    The returned hook is **always active** — it raises ``LLMNetworkCallDetected``
    for any request targeting a known LLM provider hostname.  Environment-based
    gating (``EVAL_HARNESS_ENABLED``) is the responsibility of the caller: the
    ``httpx_llm_guard`` fixture in ``conftest.py`` installs this hook only when
    the env var is truthy, leaving production gateway code unaffected in
    non-eval mode.

    Tests that need to verify the hook's blocking behaviour regardless of the
    eval env can create their own ``httpx.Client`` with this hook installed
    explicitly — no env var required.

    .. note::
        ``httpx.Client`` calls ``hook(request)`` (sync call).
        ``httpx.AsyncClient`` calls ``await hook(request)`` (coroutine required).
        Use :func:`make_async_llm_guard_hook` for ``AsyncClient`` instances.
    """

    def _hook(request: Any) -> None:
        return _check_llm_hostname(request, enabled=True)

    return _hook


def make_async_llm_guard_hook() -> Any:
    """Return an **async** httpx event-hook for use with ``httpx.AsyncClient``.

    ``httpx.AsyncClient`` executes request hooks via ``await hook(request)``.
    A synchronous function cannot be awaited — returning ``None`` from a sync
    hook causes ``TypeError: 'NoneType' object can't be awaited`` on any
    non-LLM async request.  This factory returns a proper ``async def`` hook
    so that both LLM-blocked and non-LLM (passthrough) paths are safe.

    The returned hook is **always active**.  See :func:`make_llm_guard_hook`
    for the env-gating design rationale.
    """

    async def _async_hook(request: Any) -> None:
        return _check_llm_hostname(request, enabled=True)

    return _async_hook
