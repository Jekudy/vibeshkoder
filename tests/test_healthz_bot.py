"""Tests for the bot's aiohttp /healthz endpoint (issue #168).

Strategy:
- Import ``run_healthz_server`` from ``bot.__main__`` directly.
- Start the server on a random port (port=0 → OS picks a free one).
- Use httpx async client to hit the endpoint.
- Patch ``bot.services.health.report`` to control healthy/degraded state.

No real DB required. No Telegram polling is started.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from aiohttp import web

from tests.conftest import import_module


async def _start_server(monkeypatch, healthy: bool) -> tuple[str, "web.AppRunner"]:
    """Start the healthz aiohttp server on a random port. Returns (base_url, runner)."""
    from bot.services import health as health_module

    async def _fake_report():
        return health_module.HealthReport(
            db=health_module.CheckResult(ok=healthy, reason=None if healthy else "db down"),
            settings_sanity=health_module.CheckResult(ok=True),
        )

    monkeypatch.setattr("bot.services.health.report", _fake_report)
    # Also patch the already-imported symbol inside __main__ module (eager binding):
    bot_main = import_module("bot.__main__")
    monkeypatch.setattr(bot_main, "report", _fake_report)

    runner, port = await bot_main.start_healthz_runner(host="127.0.0.1", port=0)
    base_url = f"http://127.0.0.1:{port}"
    return base_url, runner


async def _stop_server(runner: "web.AppRunner") -> None:
    await runner.cleanup()


# ─── Test 1: 200 when healthy ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_returns_200_when_healthy(app_env, monkeypatch) -> None:
    base_url, runner = await _start_server(monkeypatch, healthy=True)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["db"]["ok"] is True
        assert body["settings_sanity"]["ok"] is True
    finally:
        await _stop_server(runner)


# ─── Test 2: 503 when DB is down ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_returns_503_when_db_down(app_env, monkeypatch) -> None:
    base_url, runner = await _start_server(monkeypatch, healthy=False)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["db"]["ok"] is False
        assert body["db"]["reason"] == "db down"
    finally:
        await _stop_server(runner)


# ─── Test 3: no secrets leaked ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_does_not_leak_secrets(app_env, monkeypatch) -> None:
    """Response body must not contain bot token, web password, db password, admin ids."""
    base_url, runner = await _start_server(monkeypatch, healthy=True)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz")
        body_str = json.dumps(response.json())
        forbidden = [
            "123456:test-token",   # BOT_TOKEN
            "test-pass",           # WEB_PASSWORD
            "test-session-secret", # WEB_SESSION_SECRET
            "changeme",            # DB password
            "149820031",           # ADMIN_IDS member
        ]
        for needle in forbidden:
            assert needle not in body_str, (
                f"healthz bot endpoint leaked secret-shaped string: {needle!r}"
            )
    finally:
        await _stop_server(runner)


# ─── Test 4: concurrent with polling stub ────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_concurrent_with_polling_stub(app_env, monkeypatch) -> None:
    """asyncio.gather(stub_polling, run_healthz_server) must work cleanly."""
    from bot.services import health as health_module

    async def _fake_report():
        return health_module.HealthReport(
            db=health_module.CheckResult(ok=True),
            settings_sanity=health_module.CheckResult(ok=True),
        )

    monkeypatch.setattr("bot.services.health.report", _fake_report)
    bot_main = import_module("bot.__main__")
    monkeypatch.setattr(bot_main, "report", _fake_report)

    stub_completed = False

    async def stub_polling() -> None:
        nonlocal stub_completed
        await asyncio.sleep(0.05)
        stub_completed = True

    runner, port = await bot_main.start_healthz_runner(host="127.0.0.1", port=0)
    try:
        # gather: stub_polling finishes in ~50ms; healthz server keeps running
        # We cancel the gather after stub finishes
        async def run_and_stop():
            await stub_polling()

        await asyncio.wait_for(run_and_stop(), timeout=2.0)
        assert stub_completed
        # Server is still reachable while we didn't stop it
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/healthz")
        assert response.status_code == 200
    finally:
        await runner.cleanup()


# ─── /healthz/db tests ───────────────────────────────────────────────────────


async def _start_server_for_db(
    monkeypatch, db_ok: bool
) -> tuple[str, "web.AppRunner"]:
    """Start the healthz server with check_db patched. Returns (base_url, runner)."""
    from bot.services import health as health_module

    async def _fake_check_db():
        if db_ok:
            return health_module.CheckResult(ok=True)
        return health_module.CheckResult(ok=False, reason="OperationalError")

    monkeypatch.setattr("bot.services.health.check_db", _fake_check_db)
    bot_main = import_module("bot.__main__")
    monkeypatch.setattr(bot_main, "check_db", _fake_check_db)

    runner, port = await bot_main.start_healthz_runner(host="127.0.0.1", port=0)
    base_url = f"http://127.0.0.1:{port}"
    return base_url, runner


@pytest.mark.asyncio
async def test_healthz_db_returns_200_when_db_healthy(app_env, monkeypatch) -> None:
    """GET /healthz/db → 200 + {db: ok} when check_db is green."""
    base_url, runner = await _start_server_for_db(monkeypatch, db_ok=True)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz/db")
        assert response.status_code == 200
        body = response.json()
        assert body.get("db") == "ok"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_healthz_db_returns_503_when_db_down(app_env, monkeypatch) -> None:
    """GET /healthz/db → 503 + {db: fail, reason: ...} when check_db fails."""
    base_url, runner = await _start_server_for_db(monkeypatch, db_ok=False)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz/db")
        assert response.status_code == 503
        body = response.json()
        assert body.get("db") == "fail"
        assert "reason" in body
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_healthz_db_does_not_leak_secrets(app_env, monkeypatch) -> None:
    """Response body of /healthz/db must contain no BOT_TOKEN, WEB_PASSWORD, or DB password."""
    base_url, runner = await _start_server_for_db(monkeypatch, db_ok=True)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz/db")
        body_str = json.dumps(response.json())
        forbidden = [
            "123456:test-token",    # BOT_TOKEN
            "test-pass",            # WEB_PASSWORD
            "test-session-secret",  # WEB_SESSION_SECRET
            "changeme",             # DB password
            "149820031",            # ADMIN_IDS member
        ]
        for needle in forbidden:
            assert needle not in body_str, (
                f"/healthz/db endpoint leaked secret-shaped string: {needle!r}"
            )
    finally:
        await runner.cleanup()
