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

from dataclasses import asdict, dataclass

import logging

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


@dataclass(frozen=True)
class HealthReport:
    db: CheckResult
    settings_sanity: CheckResult

    @property
    def ok(self) -> bool:
        return self.db.ok and self.settings_sanity.ok

    def to_dict(self) -> dict:
        return {
            "status": "ok" if self.ok else "degraded",
            "db": asdict(self.db),
            "settings_sanity": asdict(self.settings_sanity),
        }


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
    )


def startup_log_lines() -> list[str]:
    """Return startup banner lines for the bot process to log at INFO. No secrets."""
    return [
        f"db.url={_safe_db_url()}",
        f"community_chat_id_set={settings.COMMUNITY_CHAT_ID != 0}",
        f"admin_ids_count={len(settings.ADMIN_IDS)}",
        f"dev_mode={settings.DEV_MODE}",
    ]
