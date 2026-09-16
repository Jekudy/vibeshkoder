"""Independent Compose health report for #521. No secrets leave the containers."""

import json
import os
import subprocess
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.request import urlopen

COMPOSE = [
    "docker",
    "compose",
    "--project-directory",
    "/srv/shkoder",
    "-f",
    "/srv/shkoder/compose.yaml",
]


def container_check(service: str, code: str) -> bool:
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", service, "python", "-c", code],
        capture_output=True,
        timeout=45,
    )
    # Never print stderr: HTTP failures can include Telegram tokens in URLs.
    return result.returncode == 0


def local_checks() -> dict[str, bool]:
    checks = {}
    for name, service, url, expected in (
        ("bot", "bot", "http://127.0.0.1:3000/healthz", '{"status": "ok"}'),
        ("db", "bot", "http://127.0.0.1:3000/healthz/db", '{"db": "ok"}'),
        ("web", "web", "http://127.0.0.1:8080/healthz", '{"status": "ok"}'),
    ):
        code = (
            "import json; from urllib.request import urlopen; "
            f"payload=json.load(urlopen({url!r}, timeout=10)); "
            f"raise SystemExit(not all(payload.get(k) == v for k, v in {expected}.items()))"
        )
        try:
            checks[name] = container_check(service, code)
        except subprocess.TimeoutExpired:
            checks[name] = False
    return checks


def main() -> int:
    public_url = os.environ["PUBLIC_HEALTH_URL"]
    if not public_url.startswith("https://"):
        raise ValueError("PUBLIC_HEALTH_URL must use HTTPS")
    checks = local_checks()
    telegram_code = """
import json, os
from urllib.request import urlopen
payload = json.load(urlopen('https://api.telegram.org/bot' + os.environ['BOT_TOKEN'] + '/getWebhookInfo', timeout=10))
result = payload['result']
raise SystemExit(not (payload['ok'] is True and result['url'] == '' and result['pending_update_count'] <= 50))
"""
    try:
        checks["telegram"] = container_check("bot", telegram_code)
    except subprocess.TimeoutExpired:
        checks["telegram"] = False
    try:
        with urlopen(public_url, timeout=15) as response:
            checks["public_https"] = (
                response.status == 200 and json.load(response)["status"] == "ok"
            )
    except (URLError, TimeoutError, ValueError, KeyError):
        checks["public_https"] = False
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "is_red": not all(checks.values()),
    }
    print(json.dumps(report, indent=2))
    return int(report["is_red"])


if __name__ == "__main__":
    raise SystemExit(main())
