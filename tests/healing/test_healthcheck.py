from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ops.healing import healthcheck


@dataclass
class FakeCursor:
    row: tuple[int] | None = (1,)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, query: str) -> None:
        assert query == "SELECT 1"

    def fetchone(self) -> tuple[int] | None:
        return self.row


@dataclass
class FakeConnection:
    cursor_obj: FakeCursor

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


@dataclass
class FakePsycopg:
    should_fail: bool = False

    def connect(self, url: str, connect_timeout: int) -> FakeConnection:
        assert url == "postgresql://healing_ro:test@db:5432/vibe_gatekeeper"
        assert connect_timeout == 5
        if self.should_fail:
            raise TimeoutError("database timeout")
        return FakeConnection(FakeCursor())


def _set_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.invalid")
    monkeypatch.setenv("COOLIFY_API_TOKEN", "coolify-token")
    monkeypatch.setenv("COOLIFY_APP_UUID", "app-uuid")
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("DATABASE_URL_RO", "postgresql://healing_ro:test@db:5432/vibe_gatekeeper")


def _client(coolify_restart_count: int = 10, coolify_status: str = "running") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/applications/app-uuid":
            return httpx.Response(
                200,
                json={"status": coolify_status, "restart_count": coolify_restart_count},
            )
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {"pending_update_count": 12}})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_coolify_status_green(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_coolify_status(
        {"coolify_status": {"restart_count": 9}},
        http_client=_client(),
    )

    assert result.status == "green"
    assert result.details["restart_count_delta"] == 1


def test_check_coolify_status_red_on_exited(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_coolify_status(
        {"coolify_status": {"restart_count": 10}},
        http_client=_client(coolify_status="exited:1"),
    )

    assert result.status == "red"
    assert "exited" in result.reason


def test_check_coolify_status_red_on_restart_delta(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_coolify_status(
        {"coolify_status": {"restart_count": 5}},
        http_client=_client(coolify_restart_count=10),
    )

    assert result.status == "red"
    assert result.details["restart_count_delta"] == 5


def test_check_telegram_pending_green(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_telegram_pending(
        {"telegram_pending": {"pending_update_count": 11}},
        http_client=_client(),
    )

    assert result.status == "green"


def test_check_telegram_pending_red_when_growing_above_threshold(monkeypatch: Any) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"pending_update_count": 80}})

    result = healthcheck.check_telegram_pending(
        {"telegram_pending": {"pending_update_count": 51}},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.status == "red"
    assert result.details["pending_update_delta"] == 29


def test_check_db_roundtrip_green(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_db_roundtrip({}, psycopg_module=FakePsycopg())

    assert result.status == "green"


def test_check_db_roundtrip_red_on_exception(monkeypatch: Any) -> None:
    _set_env(monkeypatch)
    result = healthcheck.check_db_roundtrip({}, psycopg_module=FakePsycopg(should_fail=True))

    assert result.status == "red"
    assert "database timeout" in result.reason


def test_run_all_writes_state_file(monkeypatch: Any, tmp_path: Path) -> None:
    _set_env(monkeypatch)
    monkeypatch.setattr(
        healthcheck,
        "check_coolify_status",
        lambda state: healthcheck.CheckResult(
            name="coolify_status",
            status="green",
            reason="running",
            details={"restart_count": 10},
            duration_ms=1,
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "check_telegram_pending",
        lambda state: healthcheck.CheckResult(
            name="telegram_pending",
            status="green",
            reason="pending stable",
            details={"pending_update_count": 1},
            duration_ms=1,
        ),
    )
    monkeypatch.setattr(
        healthcheck,
        "check_db_roundtrip",
        lambda state: healthcheck.CheckResult(
            name="db_roundtrip",
            status="green",
            reason="select ok",
            details={"row": [1]},
            duration_ms=1,
        ),
    )
    state_file = tmp_path / "last-state.json"

    report = healthcheck.run_all(state_file=state_file)

    assert report.is_red is False
    written = json.loads(state_file.read_text(encoding="utf-8"))
    assert written["coolify_status"]["restart_count"] == 10
