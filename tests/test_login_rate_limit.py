from __future__ import annotations

import sys
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(app_env):
    """Return a TestClient with a fresh in-memory rate-limit store."""
    # Ensure a clean import so the Limiter starts with empty counts.
    for name in list(sys.modules):
        if name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)

    web_app = importlib.import_module("web.app")
    app = web_app.create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _post_login(client: TestClient, password: str = "wrong"):
    return client.post(
        "/login",
        data={"password": password},
        follow_redirects=False,
    )


def test_five_wrong_passwords_all_401(client):
    """The first 5 failed login attempts return 401, not 429."""
    for _ in range(5):
        resp = _post_login(client, password="wrong")
        assert resp.status_code == 200  # login page re-rendered (not redirect)


def test_sixth_attempt_hits_rate_limit(client):
    """After 5 failed attempts the 6th is rate-limited with 429."""
    for _ in range(5):
        _post_login(client, password="wrong")

    resp = _post_login(client, password="wrong")
    assert resp.status_code == 429


def test_rate_limit_keys_per_forwarded_client_ip(app_env, monkeypatch):
    """Two different X-Forwarded-For IPs must have independent rate-limit buckets
    when TRUSTED_PROXY_HOSTS is set (i.e. the app is behind a known proxy).

    IP A exhausts its quota (5 wrong passwords → 429 on attempt 6).
    IP B makes its first attempt → must get 200 (login page re-rendered),
    NOT 429. Failure here means all users share one bucket and a single
    attacker can lock everyone out.
    """
    # Enable proxy trust so XFF is honoured — simulates a real proxy deployment.
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "*")

    # Fresh module import gives us a clean Limiter with empty counters.
    for name in list(sys.modules):
        if name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)

    web_app = importlib.import_module("web.app")
    app = web_app.create_app()

    ip_a_headers = {"X-Forwarded-For": "10.0.0.1"}
    ip_b_headers = {"X-Forwarded-For": "10.0.0.2"}

    with TestClient(app, raise_server_exceptions=False) as c:
        # IP A exhausts its quota.
        for _ in range(5):
            c.post(
                "/login", data={"password": "wrong"}, headers=ip_a_headers, follow_redirects=False
            )

        # IP A's 6th attempt must be rate-limited.
        resp_a = c.post(
            "/login", data={"password": "wrong"}, headers=ip_a_headers, follow_redirects=False
        )
        assert resp_a.status_code == 429, "IP A should be rate-limited after 5 wrong passwords"

        # IP B has never posted — its first attempt must NOT be blocked.
        resp_b = c.post(
            "/login", data={"password": "wrong"}, headers=ip_b_headers, follow_redirects=False
        )
        assert resp_b.status_code == 200, (
            f"IP B got {resp_b.status_code} instead of 200 — "
            "rate-limit buckets are not isolated per forwarded IP; "
            "a single attacker can lock out all users"
        )


def test_xff_ignored_from_untrusted_client(app_env):
    """With TRUSTED_PROXY_HOSTS="" (default), rotating X-Forwarded-For does NOT
    produce independent rate-limit buckets — all requests key off the TCP peer
    address, so an attacker cannot bypass the limit by spoofing XFF.
    """
    # Ensure default (empty = no proxy trust).
    # app_env fixture does not set TRUSTED_PROXY_HOSTS, which defaults to "".

    # Fresh module import gives a clean Limiter.
    for name in list(sys.modules):
        if name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)

    web_app = importlib.import_module("web.app")
    app = web_app.create_app()

    with TestClient(app, raise_server_exceptions=False) as c:
        # First 5 attempts with different fake XFF values — should all be 200.
        for i in range(5):
            resp = c.post(
                "/login",
                data={"password": "wrong"},
                headers={"X-Forwarded-For": f"1.2.3.{i}"},
                follow_redirects=False,
            )
            assert resp.status_code == 200

        # 6th attempt with yet another fake XFF — should be 429 because the
        # real TCP peer (testclient) has exhausted its quota regardless of XFF.
        resp = c.post(
            "/login",
            data={"password": "wrong"},
            headers={"X-Forwarded-For": "9.9.9.9"},
            follow_redirects=False,
        )
        assert resp.status_code == 429, (
            "Untrusted client spoofing XFF should still be rate-limited by TCP peer IP"
        )


def test_xff_honored_when_proxy_trusted(app_env, monkeypatch):
    """With TRUSTED_PROXY_HOSTS="*", XFF is trusted and different forwarded IPs
    get independent rate-limit buckets (correct proxy-behind-proxy behaviour).
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "*")

    # Fresh module import gives a clean Limiter.
    for name in list(sys.modules):
        if name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)

    web_app = importlib.import_module("web.app")
    app = web_app.create_app()

    with TestClient(app, raise_server_exceptions=False) as c:
        # Exhaust quota for 10.0.0.100 via XFF.
        for _ in range(5):
            c.post(
                "/login",
                data={"password": "wrong"},
                headers={"X-Forwarded-For": "10.0.0.100"},
                follow_redirects=False,
            )

        # 6th attempt for 10.0.0.100 should be 429.
        resp_limited = c.post(
            "/login",
            data={"password": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.100"},
            follow_redirects=False,
        )
        assert resp_limited.status_code == 429, "Exhausted XFF bucket should be rate-limited"

        # First attempt for a different XFF IP should be 200 (fresh bucket).
        resp_fresh = c.post(
            "/login",
            data={"password": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.200"},
            follow_redirects=False,
        )
        assert resp_fresh.status_code == 200, (
            "Different XFF IP should have its own fresh bucket when proxy is trusted"
        )
