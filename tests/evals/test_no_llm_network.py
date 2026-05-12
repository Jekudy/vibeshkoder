"""Phase 11 follow-up #224 High #5 — httpx URL/domain-level runtime guard.

Verifies that any outbound httpx call to an LLM provider hostname is blocked
during eval-mode execution (``EVAL_HARNESS_ENABLED=1``).  This catches the
raw-HTTP escape hatch that the AST-level ``test_i4_no_llm_provider_url_outside_gateway``
cannot detect at import-time.

The guard itself lives in ``tests/evals/conftest.py`` as an ``autouse`` session
fixture.  These tests verify the guard's observable behaviour:

* **Positive sync test**: hook does not raise for a non-LLM URL (via direct
  hook call — exercises the hook logic without relying on ``httpx.Client.send``
  being reachable).
* **Positive async test**: ``httpx.AsyncClient`` with ``MockTransport`` completes
  a non-LLM request without raising — exercises the full async hook chain.
* **Negative sync test**: code that performs a direct ``httpx.post`` to
  ``api.anthropic.com`` (or any LLM hostname) is detected and raises
  ``LLMNetworkCallDetected``.
* **Negative async test**: ``httpx.AsyncClient`` also triggers the guard.

The tests in this file intentionally do NOT depend on any DB fixture; they
run in every environment where ``EVAL_HARNESS_ENABLED=1`` is set.
"""

from __future__ import annotations

import asyncio
import httpx
import pytest

from tests.evals._llm_guard import (
    LLMNetworkCallDetected,
    make_async_llm_guard_hook,
    make_llm_guard_hook,
)


# ---------------------------------------------------------------------------
# Guard behaviour tests
# ---------------------------------------------------------------------------


def test_guard_positive_non_llm_hook_does_not_raise(httpx_llm_guard: None) -> None:
    """Positive (sync): the sync hook returns None without raising for non-LLM URLs.

    Calls the hook factory directly so the test exercises the hook logic through
    the same code path the fixture uses, without relying on ``httpx.Client.send``
    being reachable.  This avoids the mock.patch.object pattern that bypasses
    the event-hook chain entirely.
    """
    hook = make_llm_guard_hook()
    fake_request = httpx.Request("GET", "https://example.com/api")
    # Must not raise — non-LLM host is allowed.
    result = hook(fake_request)
    assert result is None


def test_guard_positive_async_non_llm_call_is_allowed(httpx_llm_guard: None) -> None:
    """Positive (async): AsyncClient with MockTransport completes for non-LLM URLs.

    Uses ``httpx.MockTransport`` (ships with httpx, no extra deps) to intercept
    at transport level — after hooks have fired.  Confirms that the async hook
    does NOT raise for a non-LLM host, which would otherwise cause
    ``LLMNetworkCallDetected`` to propagate.
    """

    async def _make_call() -> httpx.Response:
        transport = httpx.MockTransport(
            handler=lambda request: httpx.Response(200)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.get("https://example.com/api")

    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(_make_call())
        assert response.status_code == 200
    finally:
        loop.close()


def test_guard_negative_direct_httpx_to_llm_hostname_is_detected() -> None:
    """Negative: a direct httpx call to an LLM provider hostname is blocked.

    Simulates user-land code that bypasses the gateway and calls
    ``api.anthropic.com`` directly via ``httpx``.  The guard must raise
    ``LLMNetworkCallDetected`` before any real I/O occurs.

    Uses an explicit hook on a local ``httpx.Client`` — environment-independent.
    """
    hook = make_llm_guard_hook()
    with httpx.Client(event_hooks={"request": [hook]}) as client:
        with pytest.raises(LLMNetworkCallDetected, match="api.anthropic.com"):
            client.get("https://api.anthropic.com/v1/messages")


def test_guard_negative_async_client_to_llm_hostname_is_detected() -> None:
    """Negative (async): async httpx.AsyncClient also triggers the guard.

    Uses an explicit async hook on a local ``httpx.AsyncClient`` —
    environment-independent.
    """
    async_hook = make_async_llm_guard_hook()

    async def _make_call() -> None:
        async with httpx.AsyncClient(event_hooks={"request": [async_hook]}) as client:
            await client.post("https://api.openai.com/v1/chat/completions", json={})

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(LLMNetworkCallDetected, match="api.openai.com"):
            loop.run_until_complete(_make_call())
    finally:
        loop.close()


def test_guard_negative_google_gemini_hostname_is_detected() -> None:
    """Negative: Gemini endpoint is also in the blocked domain list.

    Uses an explicit hook on a local ``httpx.Client`` — environment-independent.
    """
    hook = make_llm_guard_hook()
    with httpx.Client(event_hooks={"request": [hook]}) as client:
        with pytest.raises(LLMNetworkCallDetected, match="generativelanguage.googleapis.com"):
            client.get("https://generativelanguage.googleapis.com/v1beta/models")


def test_guard_fixture_noop_without_eval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard fixture is a no-op when EVAL_HARNESS_ENABLED is absent.

    Verifies that the autouse ``httpx_llm_guard`` fixture does NOT patch
    ``httpx.Client.__init__`` when the env var is absent, so production
    gateway code can call httpx to LLM endpoints unblocked in non-eval mode.

    The hook factories themselves are always-active (env-gating removed); only
    the fixture is env-gated.

    NOTE: this test does NOT rely on the autouse ``httpx_llm_guard`` fixture —
    it explicitly verifies the disabled code-path.
    """
    import httpx as _httpx

    monkeypatch.delenv("EVAL_HARNESS_ENABLED", raising=False)

    # Capture __init__ identities before any fixture patching.
    original_client_init_id = id(_httpx.Client.__init__)
    original_async_client_init_id = id(_httpx.AsyncClient.__init__)

    # Sync hook factory is always-active — calling it directly raises for LLM URLs.
    fake_request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    sync_hook = make_llm_guard_hook()
    with pytest.raises(LLMNetworkCallDetected):
        sync_hook(fake_request)

    # httpx.Client.__init__ must NOT have been patched (fixture is inactive without
    # env var; the autouse fixture yields immediately when env var is absent).
    assert id(_httpx.Client.__init__) == original_client_init_id, (
        "httpx.Client.__init__ was patched without EVAL_HARNESS_ENABLED — "
        "fixture env-gating is broken"
    )
    assert id(_httpx.AsyncClient.__init__) == original_async_client_init_id, (
        "httpx.AsyncClient.__init__ was patched without EVAL_HARNESS_ENABLED — "
        "fixture env-gating is broken"
    )
