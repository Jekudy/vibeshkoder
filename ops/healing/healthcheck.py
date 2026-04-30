from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg

DEFAULT_STATE_FILE = Path(".healing/last-state.json")
Status = Literal["green", "red"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    reason: str
    details: dict[str, Any]
    duration_ms: int

    @property
    def is_red(self) -> bool:
        return self.status == "red"


@dataclass(frozen=True)
class CheckReport:
    generated_at: str
    coolify_status: CheckResult
    telegram_pending: CheckResult
    db_roundtrip: CheckResult

    @property
    def is_red(self) -> bool:
        return any(
            result.is_red
            for result in (self.coolify_status, self.telegram_pending, self.db_roundtrip)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"is_red": self.is_red}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise KeyError(f"missing required env var: {name}")
    return value


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _read_previous_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    value = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{state_file} must contain a JSON object")
    return value


def _write_previous_state(state_file: Path, report: CheckReport) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "generated_at": report.generated_at,
        "coolify_status": report.coolify_status.details,
        "telegram_pending": report.telegram_pending.details,
        "db_roundtrip": report.db_roundtrip.details,
    }
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _client_context(http_client: httpx.Client | None):
    if http_client is not None:
        return nullcontext(http_client)
    return httpx.Client(timeout=10)


def check_coolify_status(
    state: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> CheckResult:
    start = time.monotonic()
    try:
        base_url = _require_env("COOLIFY_BASE_URL").rstrip("/")
        token = _require_env("COOLIFY_API_TOKEN")
        app_uuid = _require_env("COOLIFY_APP_UUID")
        with _client_context(http_client) as client:
            response = client.get(
                f"{base_url}/api/v1/applications/{app_uuid}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()

        status = str(payload["status"])
        restart_count = int(payload["restart_count"])
        previous = state.get("coolify_status", {})
        previous_restart = previous.get("restart_count") if isinstance(previous, dict) else None
        restart_delta = (
            restart_count - int(previous_restart) if previous_restart is not None else 0
        )
        details = {
            "status": status,
            "restart_count": restart_count,
            "previous_restart_count": previous_restart,
            "restart_count_delta": restart_delta,
        }
        if status.startswith("exited:"):
            return CheckResult(
                "coolify_status",
                "red",
                f"Coolify app status is {status}",
                details,
                _elapsed_ms(start),
            )
        if restart_delta > 2:
            return CheckResult(
                "coolify_status",
                "red",
                f"restart_count increased by {restart_delta}",
                details,
                _elapsed_ms(start),
            )
        return CheckResult("coolify_status", "green", "Coolify app is stable", details, _elapsed_ms(start))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return CheckResult(
            "coolify_status",
            "red",
            str(exc),
            {"error_type": type(exc).__name__},
            _elapsed_ms(start),
        )


def check_telegram_pending(
    state: dict[str, Any],
    http_client: httpx.Client | None = None,
) -> CheckResult:
    start = time.monotonic()
    try:
        bot_token = _require_env("BOT_TOKEN")
        with _client_context(http_client) as client:
            response = client.get(f"https://api.telegram.org/bot{bot_token}/getWebhookInfo")
            response.raise_for_status()
            payload = response.json()

        pending = int(payload["result"]["pending_update_count"])
        previous = state.get("telegram_pending", {})
        previous_pending = previous.get("pending_update_count") if isinstance(previous, dict) else None
        pending_delta = pending - int(previous_pending) if previous_pending is not None else 0
        details = {
            "pending_update_count": pending,
            "previous_pending_update_count": previous_pending,
            "pending_update_delta": pending_delta,
        }
        if previous_pending is not None and pending > 50 and pending > int(previous_pending):
            return CheckResult(
                "telegram_pending",
                "red",
                f"pending_update_count grew to {pending}",
                details,
                _elapsed_ms(start),
            )
        return CheckResult(
            "telegram_pending",
            "green",
            "Telegram pending updates are stable",
            details,
            _elapsed_ms(start),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return CheckResult(
            "telegram_pending",
            "red",
            str(exc),
            {"error_type": type(exc).__name__},
            _elapsed_ms(start),
        )


def check_db_roundtrip(
    state: dict[str, Any],
    psycopg_module: Any | None = None,
) -> CheckResult:
    del state
    start = time.monotonic()
    driver = psycopg if psycopg_module is None else psycopg_module
    try:
        database_url = _require_env("DATABASE_URL_RO")
        with driver.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
        if row != (1,):
            return CheckResult(
                "db_roundtrip",
                "red",
                f"unexpected SELECT 1 result: {row!r}",
                {"row": list(row) if isinstance(row, tuple) else row},
                _elapsed_ms(start),
            )
        return CheckResult(
            "db_roundtrip",
            "green",
            "database roundtrip succeeded",
            {"row": [1]},
            _elapsed_ms(start),
        )
    except (Exception,) as exc:
        return CheckResult(
            "db_roundtrip",
            "red",
            str(exc),
            {"error_type": type(exc).__name__},
            _elapsed_ms(start),
        )


def run_all(state_file: Path = DEFAULT_STATE_FILE) -> CheckReport:
    state = _read_previous_state(state_file)
    with ThreadPoolExecutor(max_workers=3) as pool:
        coolify_future = pool.submit(check_coolify_status, state)
        telegram_future = pool.submit(check_telegram_pending, state)
        db_future = pool.submit(check_db_roundtrip, state)
        report = CheckReport(
            generated_at=_utc_now(),
            coolify_status=coolify_future.result(),
            telegram_pending=telegram_future.result(),
            db_roundtrip=db_future.result(),
        )
    _write_previous_state(state_file, report)
    return report


def main() -> int:
    report = run_all()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 1 if report.is_red else 0


if __name__ == "__main__":
    raise SystemExit(main())
