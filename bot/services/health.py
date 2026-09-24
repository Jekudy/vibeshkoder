"""Health and startup checks (T0-05).

This module is the single place that knows how to ask "is the app healthy?". It is used
by:
- the bot startup path in ``bot/__main__.py`` (logs the report at INFO level so we can see
  which environment the process woke up in);
- the FastAPI ``/healthz`` route in ``web/routes/health.py`` (returns a thin JSON payload
  without secrets).

Design rules (per ``docs/memory-system/HANDOFF.md`` §8):
- Every check is small, fast, and self-contained.
- No secrets / env values are returned in the report. The DB URL is rendered with the
  password redacted; tokens are never returned.
- Failures are reported, not raised — the report carries booleans + short reason strings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import logging
import time

from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError

from bot.config import settings
from bot.db.engine import async_session

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str | None = None


# Max allowed age of the last successful getUpdates response before the bot is
# reported as dead (issue #541). start_polling uses polling_timeout=10s, so an
# empty long-poll returns every ~10s; a hung request can occupy up to
# session.timeout + polling_timeout = 70s before aiogram retries. 120s survives
# one full hung attempt plus retry margin and stays below the 180s readiness
# budget in ops/compose/deploy.py, so a dead Telegram path fails a deploy
# instead of silently passing it. The same window doubles as startup grace:
# before the first successful poll the anchor is polling-start time.
TELEGRAM_POLL_STALE_SECONDS: float = 120.0

# Process-local polling liveness state (monotonic timestamps). Written by the
# bot process only: __main__ arms it via note_polling_started() and the aiogram
# session middleware bumps it via note_poll_ok() on each getUpdates response.
# The web process never arms it, so its /healthz reports the check as
# not-applicable-ok instead of going red forever.
_polling_started_at: float | None = None
_last_poll_ok_at: float | None = None


@dataclass(frozen=True)
class HealthReport:
    db: CheckResult
    settings_sanity: CheckResult
    telegram_poll: CheckResult = field(
        default_factory=lambda: CheckResult(ok=True, reason="check not run")
    )

    @property
    def ok(self) -> bool:
        return self.db.ok and self.settings_sanity.ok and self.telegram_poll.ok

    def to_dict(self) -> dict:
        return {
            "status": "ok" if self.ok else "degraded",
            "db": asdict(self.db),
            "settings_sanity": asdict(self.settings_sanity),
            "telegram_poll": asdict(self.telegram_poll),
            "last_telegram_poll_ok_age_seconds": last_telegram_poll_ok_age_seconds(),
        }


def note_polling_started() -> None:
    """Arm the telegram_poll check. Called once by the bot process before
    ``dp.start_polling``; sets the grace anchor for the first poll."""
    global _polling_started_at
    if _polling_started_at is None:
        _polling_started_at = time.monotonic()


def note_poll_ok() -> None:
    """Record a successful getUpdates response (empty long-polls included)."""
    global _last_poll_ok_at, _polling_started_at
    _last_poll_ok_at = time.monotonic()
    if _polling_started_at is None:
        _polling_started_at = _last_poll_ok_at


def check_telegram_poll(now: float | None = None) -> CheckResult:
    """ok while the polling loop produced a getUpdates response within
    TELEGRAM_POLL_STALE_SECONDS, or while still inside that window since
    polling start (deploy-time grace). ok when polling is not armed at all
    (web process, webhook deployments)."""
    if _polling_started_at is None:
        return CheckResult(ok=True, reason="polling not armed in this process")
    anchor = _last_poll_ok_at if _last_poll_ok_at is not None else _polling_started_at
    age = (time.monotonic() if now is None else now) - anchor
    if age > TELEGRAM_POLL_STALE_SECONDS:
        return CheckResult(ok=False, reason=f"no successful getUpdates for {int(age)}s")
    if _last_poll_ok_at is None:
        return CheckResult(ok=True, reason="awaiting first getUpdates response")
    return CheckResult(ok=True)


def last_telegram_poll_ok_age_seconds(now: float | None = None) -> int | None:
    """Seconds since the last successful getUpdates, or None before the first one."""
    if _last_poll_ok_at is None:
        return None
    return int((time.monotonic() if now is None else now) - _last_poll_ok_at)


def _safe_db_url() -> str:
    try:
        return make_url(settings.DATABASE_URL).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable>"


async def check_db() -> CheckResult:
    """Ping the configured DB. Returns ok=True if a trivial SELECT 1 succeeds.

    Catches only SQLAlchemy / driver errors so genuine programming bugs propagate to the
    caller's error handler. The ``reason`` field returns ONLY the exception class name —
    full ``str(exc)`` from asyncpg / psycopg often embeds host / DB name / connection
    details that we do not want to expose in a public ``/healthz`` response. The full
    exception is still logged at WARNING for operator diagnostics.
    """
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        return CheckResult(ok=True)
    except SQLAlchemyError as exc:  # pragma: no cover — exercised via mock in tests
        logger.warning("check_db failed: %s: %s", type(exc).__name__, exc)
        return CheckResult(ok=False, reason=type(exc).__name__)


def check_settings_sanity() -> CheckResult:
    """Confirm a few non-secret invariants the bot relies on:
    - COMMUNITY_CHAT_ID is set (non-zero)
    - DATABASE_URL is set (engine module already validated it points at postgres)
    """
    if settings.COMMUNITY_CHAT_ID == 0:
        return CheckResult(ok=False, reason="COMMUNITY_CHAT_ID is unset")
    if not settings.DATABASE_URL:
        return CheckResult(ok=False, reason="DATABASE_URL is unset")
    return CheckResult(ok=True)


async def report() -> HealthReport:
    return HealthReport(
        db=await check_db(),
        settings_sanity=check_settings_sanity(),
        telegram_poll=check_telegram_poll(),
    )


def startup_log_lines() -> list[str]:
    """Return startup banner lines for the bot process to log at INFO. No secrets."""
    return [
        f"db.url={_safe_db_url()}",
        f"community_chat_id_set={settings.COMMUNITY_CHAT_ID != 0}",
        f"admin_ids_count={len(settings.ADMIN_IDS)}",
        f"dev_mode={settings.DEV_MODE}",
    ]
