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


def test_rate_limit_keys_per_forwarded_client_ip(app_env):
    """Two different X-Forwarded-For IPs must have independent rate-limit buckets.

    IP A exhausts its quota (5 wrong passwords → 429 on attempt 6).
    IP B makes its first attempt → must get 200 (login page re-rendered),
    NOT 429. Failure here means all users share one bucket and a single
    attacker can lock everyone out.
    """
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
