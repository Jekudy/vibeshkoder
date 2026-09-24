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
import time

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


# ─── telegram_poll check (issue #541) ────────────────────────────────────────


def _health_module():
    from bot.services import health as health_module

    return health_module


def _arm_polling(
    health_module, monkeypatch, *, started_ago: float, last_ok_ago: float | None
) -> None:
    """Put the module-level polling state into a deterministic position."""
    now = time.monotonic()
    monkeypatch.setattr(health_module, "_polling_started_at", now - started_ago)
    monkeypatch.setattr(
        health_module,
        "_last_poll_ok_at",
        None if last_ok_ago is None else now - last_ok_ago,
    )


def test_telegram_poll_fresh_is_ok(app_env, monkeypatch) -> None:
    health = _health_module()
    _arm_polling(health, monkeypatch, started_ago=300, last_ok_ago=5)
    result = health.check_telegram_poll()
    assert result.ok is True


def test_telegram_poll_stale_fails(app_env, monkeypatch) -> None:
    health = _health_module()
    _arm_polling(
        health,
        monkeypatch,
        started_ago=300,
        last_ok_ago=health.TELEGRAM_POLL_STALE_SECONDS + 1,
    )
    result = health.check_telegram_poll()
    assert result.ok is False
    assert "getUpdates" in result.reason


def test_telegram_poll_within_startup_grace_is_ok(app_env, monkeypatch) -> None:
    """Armed polling with no successful getUpdates yet is ok inside the grace
    window — deploy.py health-gates right after restart."""
    health = _health_module()
    _arm_polling(health, monkeypatch, started_ago=10, last_ok_ago=None)
    result = health.check_telegram_poll()
    assert result.ok is True


def test_telegram_poll_grace_expired_fails(app_env, monkeypatch) -> None:
    """Armed polling with no successful getUpdates past the grace window fails."""
    health = _health_module()
    _arm_polling(
        health,
        monkeypatch,
        started_ago=health.TELEGRAM_POLL_STALE_SECONDS + 1,
        last_ok_ago=None,
    )
    result = health.check_telegram_poll()
    assert result.ok is False


def test_telegram_poll_not_armed_is_ok(app_env) -> None:
    """Processes that never poll (web) report the check as not-applicable-ok."""
    health = _health_module()
    result = health.check_telegram_poll()
    assert result.ok is True


def test_note_poll_ok_marks_timestamp(app_env) -> None:
    health = _health_module()
    health.note_polling_started()
    health.note_poll_ok()
    age = health.last_telegram_poll_ok_age_seconds()
    assert age is not None and age < 5


@pytest.mark.asyncio
async def test_poll_liveness_probe_marks_only_getupdates(app_env) -> None:
    """The session middleware must stamp getUpdates responses only — other API
    calls (get_me etc.) must not fake polling liveness."""
    from aiogram.methods import GetMe, GetUpdates

    health = _health_module()
    bot_main = import_module("bot.__main__")

    async def ok_request(bot, method):
        return object()

    await bot_main._poll_liveness_probe(ok_request, None, GetMe())
    assert health._last_poll_ok_at is None

    await bot_main._poll_liveness_probe(ok_request, None, GetUpdates())
    first = health._last_poll_ok_at
    assert first is not None

    async def failing_request(bot, method):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        await bot_main._poll_liveness_probe(failing_request, None, GetUpdates())
    assert health._last_poll_ok_at == first


async def _start_server_with_poll_state(
    monkeypatch, *, started_ago: float, last_ok_ago: float | None
) -> tuple[str, "web.AppRunner"]:
    """Start the healthz server with real check_telegram_poll and fake DB."""
    health = _health_module()
    _arm_polling(health, monkeypatch, started_ago=started_ago, last_ok_ago=last_ok_ago)

    async def _fake_check_db():
        return health.CheckResult(ok=True)

    monkeypatch.setattr(health, "check_db", _fake_check_db)
    bot_main = import_module("bot.__main__")
    monkeypatch.setattr(bot_main, "check_db", _fake_check_db)

    runner, port = await bot_main.start_healthz_runner(host="127.0.0.1", port=0)
    return f"http://127.0.0.1:{port}", runner


@pytest.mark.asyncio
async def test_healthz_503_when_polling_stale(app_env, monkeypatch) -> None:
    base_url, runner = await _start_server_with_poll_state(
        monkeypatch, started_ago=600, last_ok_ago=600
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["telegram_poll"]["ok"] is False
        assert body["last_telegram_poll_ok_age_seconds"] >= 600
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_healthz_200_and_poll_fields_when_fresh(app_env, monkeypatch) -> None:
    base_url, runner = await _start_server_with_poll_state(
        monkeypatch, started_ago=60, last_ok_ago=5
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["telegram_poll"]["ok"] is True
        assert 0 <= body["last_telegram_poll_ok_age_seconds"] < 30
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_healthz_200_within_startup_grace(app_env, monkeypatch) -> None:
    """No poll yet but inside the grace window: 200, age field is null."""
    base_url, runner = await _start_server_with_poll_state(
        monkeypatch, started_ago=10, last_ok_ago=None
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["telegram_poll"]["ok"] is True
        assert body["last_telegram_poll_ok_age_seconds"] is None
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
