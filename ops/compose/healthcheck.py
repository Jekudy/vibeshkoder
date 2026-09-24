"""Independent Compose health report for #521. No secrets leave the containers."""

import json
import os
import subprocess
from datetime import UTC, datetime

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
    # The bot check additionally requires the telegram_poll liveness signal
    # from issue #541: the check itself must be ok and the age field must be
    # present (null only until the first successful getUpdates).
    for name, service, url, expected, extra in (
        (
            "bot",
            "bot",
            "http://127.0.0.1:3000/healthz",
            '{"status": "ok"}',
            "payload.get('telegram_poll', {}).get('ok') is True "
            "and 'last_telegram_poll_ok_age_seconds' in payload "
            "and (payload['last_telegram_poll_ok_age_seconds'] is None "
            "or payload['last_telegram_poll_ok_age_seconds'] >= 0)",
        ),
        ("db", "bot", "http://127.0.0.1:3000/healthz/db", '{"db": "ok"}', "True"),
        ("web", "web", "http://127.0.0.1:8080/healthz", '{"status": "ok"}', "True"),
    ):
        code = (
            "import json; from urllib.request import urlopen; "
            f"payload=json.load(urlopen({url!r}, timeout=10)); "
            f"ok=all(payload.get(k) == v for k, v in {expected}.items()) and ({extra}); "
            "raise SystemExit(not ok)"
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
        response = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--proto", "=https",
             "--connect-timeout", "10", "--max-time", "15", public_url],
            capture_output=True,
            timeout=20,
        )
        checks["public_https"] = (
            response.returncode == 0 and json.loads(response.stdout)["status"] == "ok"
        )
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
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
