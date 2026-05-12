"""Phase 11 follow-up #224 High #5 — httpx URL/domain-level runtime guard.

Verifies that any outbound httpx call to an LLM provider hostname is blocked
during eval-mode execution (``EVAL_HARNESS_ENABLED=1``).  This catches the
raw-HTTP escape hatch that the AST-level ``test_i4_no_llm_provider_url_outside_gateway``
cannot detect at import-time.

The guard itself lives in ``tests/evals/conftest.py`` as an ``autouse`` session
fixture.  These tests verify the guard's observable behaviour:

* **Positive test**: code that never calls an LLM hostname passes silently.
* **Negative test**: code that performs a direct ``httpx.post`` to
  ``api.anthropic.com`` (or any LLM hostname) is detected and raises
  ``LLMNetworkCallDetected``.

The tests in this file intentionally do NOT depend on any DB fixture; they
run in every environment where ``EVAL_HARNESS_ENABLED=1`` is set.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.evals._llm_guard import LLMNetworkCallDetected, make_llm_guard_hook


# ---------------------------------------------------------------------------
# Guard behaviour tests
# ---------------------------------------------------------------------------


def test_guard_positive_non_llm_call_is_allowed(httpx_llm_guard: None) -> None:
    """Positive: a request to a non-LLM host is NOT blocked by the guard.

    We simply verify that the guard fixture does not interfere with regular
    (non-provider) httpx usage.  We monkeypatch httpx.Client.send so no real
    network I/O occurs.
    """
    import unittest.mock as mock

    # Patch at transport level to avoid real network call.
    with mock.patch.object(httpx.Client, "send", return_value=httpx.Response(200)):
        client = httpx.Client()
        # A call to a non-LLM host must NOT raise LLMNetworkCallDetected.
        resp = client.get("https://example.com/api")
        assert resp.status_code == 200


def test_guard_negative_direct_httpx_to_llm_hostname_is_detected(
    httpx_llm_guard: None,
) -> None:
    """Negative: a direct httpx call to an LLM provider hostname is blocked.

    Simulates user-land code that bypasses the gateway and calls
    ``api.anthropic.com`` directly via ``httpx``.  The guard must raise
    ``LLMNetworkCallDetected`` before any real I/O occurs.
    """
    with pytest.raises(LLMNetworkCallDetected, match="api.anthropic.com"):
        httpx.post("https://api.anthropic.com/v1/messages", json={})


def test_guard_negative_async_client_to_llm_hostname_is_detected(
    httpx_llm_guard: None,
) -> None:
    """Negative (async): async httpx.AsyncClient also triggers the guard."""
    import asyncio

    async def _make_call() -> None:
        async with httpx.AsyncClient() as client:
            await client.post("https://api.openai.com/v1/chat/completions", json={})

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(LLMNetworkCallDetected, match="api.openai.com"):
            loop.run_until_complete(_make_call())
    finally:
        loop.close()


def test_guard_negative_google_gemini_hostname_is_detected(
    httpx_llm_guard: None,
) -> None:
    """Negative: Gemini endpoint is also in the blocked domain list."""
    with pytest.raises(LLMNetworkCallDetected, match="generativelanguage.googleapis.com"):
        httpx.get("https://generativelanguage.googleapis.com/v1beta/models")


def test_guard_is_disabled_without_eval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard must NOT fire when EVAL_HARNESS_ENABLED is absent/falsy.

    This protects production runtime: the gateway *must* be able to call
    httpx to LLM endpoints in non-eval mode.

    NOTE: this test does NOT use the ``httpx_llm_guard`` fixture — it
    explicitly verifies the code-path taken when the guard is inactive.
    """
    monkeypatch.delenv("EVAL_HARNESS_ENABLED", raising=False)

    hook = make_llm_guard_hook()

    # When EVAL_HARNESS_ENABLED is absent, the hook must be a no-op: calling it
    # directly should NOT raise even for an LLM hostname.
    fake_request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    # The hook returns None (no-op) — must not raise.
    result = hook(fake_request)
    assert result is None
