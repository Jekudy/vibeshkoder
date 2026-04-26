"""T0-05 acceptance tests — GET /healthz endpoint and startup banner.

Strategy: the health route imports ``bot.services.health.report``. We patch ``report`` at
its source module to control the response. The web app is created via ``create_app()`` and
exercised via FastAPI's ``TestClient``. No real DB is required.

Acceptance criteria (from HANDOFF.md §8 and AUTHORIZED_SCOPE.md T0-05):
- GET /healthz returns 200 with ``{"status": "ok", ...}`` when the app is healthy.
- GET /healthz returns 503 when DB is unreachable.
- Response body contains no env values, no secrets, no DB password.
- Startup banner lines (logged by bot/__main__.py) contain no secrets.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import import_module


def _make_client(app_env_fixture, monkeypatch, healthy: bool):
    """Build a TestClient with bot.services.health.report patched."""
    from bot.services import health as health_module

    async def _fake_report():
        return health_module.HealthReport(
            db=health_module.CheckResult(ok=healthy, reason=None if healthy else "db down"),
            settings_sanity=health_module.CheckResult(ok=True),
        )

    monkeypatch.setattr("bot.services.health.report", _fake_report)
    # Also patch the symbol the route already imported (eager binding):
    web_routes_health = import_module("web.routes.health")
    monkeypatch.setattr(web_routes_health, "report", _fake_report)

    web_app = import_module("web.app")
    return TestClient(web_app.create_app())


def test_healthz_returns_200_when_healthy(app_env, monkeypatch) -> None:
    client = _make_client(app_env, monkeypatch, healthy=True)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"]["ok"] is True
    assert body["settings_sanity"]["ok"] is True


def test_healthz_returns_503_when_db_down(app_env, monkeypatch) -> None:
    client = _make_client(app_env, monkeypatch, healthy=False)
    response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"]["ok"] is False
    assert body["db"]["reason"] == "db down"


def test_healthz_does_not_leak_secrets(app_env, monkeypatch) -> None:
    """The /healthz response body must not contain bot token, web password, db password,
    or admin ids."""
    client = _make_client(app_env, monkeypatch, healthy=True)
    response = client.get("/healthz")
    body_str = json.dumps(response.json())

    # These are values app_env sets — they must NOT appear in the response.
    forbidden = [
        "123456:test-token",  # BOT_TOKEN
        "test-pass",  # WEB_PASSWORD
        "test-session-secret",  # WEB_SESSION_SECRET
        "changeme",  # DB password
        "149820031",  # ADMIN_IDS member
    ]
    for needle in forbidden:
        assert needle not in body_str, f"healthz leaked secret-shaped string: {needle!r}"


def test_healthz_is_unauthenticated(app_env, monkeypatch) -> None:
    """/healthz must NOT redirect to /login when no session cookie is present.
    Auth middleware skips it because it's in _PUBLIC_PATHS."""
    client = _make_client(app_env, monkeypatch, healthy=True)
    response = client.get("/healthz", follow_redirects=False)
    # 200 or 503 is acceptable; 302 (login redirect) is the failure mode we test against.
    assert response.status_code in (200, 503)


def test_startup_log_lines_contain_no_secrets(app_env) -> None:
    """``bot.services.health.startup_log_lines()`` must not include the bot token, web
    password, session secret, or DB password."""
    health_module = import_module("bot.services.health")
    lines = health_module.startup_log_lines()
    text = "\n".join(lines)
    forbidden = [
        "123456:test-token",
        "test-pass",
        "test-session-secret",
        "changeme",
    ]
    for needle in forbidden:
        assert needle not in text, f"startup banner leaked secret-shaped string: {needle!r}"
