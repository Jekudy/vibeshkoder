"""Repository for ``ingestion_runs`` (T1-02).

The repo intentionally exposes only the operations the ingestion service needs:
``create``, ``update_status``, and ``get_active_live``.

HANDOFF.md §7 names the full live-startup helper ``get_or_create_live_run()``; the
"create if missing" half intentionally lives in ``bot/services/ingestion.py`` (T1-04) so
the repo stays a thin data-access layer. The repo only answers "is there a live run
right now?" via ``get_active_live``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import IngestionRun

# Allowed run_type / status values mirror the CheckConstraint in the migration.
_ALLOWED_RUN_TYPES = {"live", "import", "dry_run", "cancelled"}
_TERMINAL_STATUSES = {"completed", "failed", "dry_run", "cancelled"}
_ALLOWED_STATUSES = {"running"} | _TERMINAL_STATUSES

# Substring guard for ``config_json`` keys. The repo refuses to persist any payload whose
# top-level keys look secret-shaped (``token``, ``secret``, ``password``, ``api_key``, etc.)
# because the table is dumped in admin views and may be copied for support. Recursive
# checks would be more thorough but also more expensive; we accept the top-level-only check
# as a pragmatic enforcement of the docstring contract.
_SECRET_KEY_PATTERN = re.compile(r"(?i)(token|secret|password|passphrase|api[_-]?key)")


def _reject_secret_shaped_keys(name: str, payload: dict | None) -> None:
    if not payload:
        return
    leaked = [k for k in payload.keys() if _SECRET_KEY_PATTERN.search(str(k))]
    if leaked:
        raise ValueError(
            f"{name} must not contain secret-shaped keys; refused: {sorted(leaked)}"
        )


class IngestionRunRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        run_type: str,
        source_name: str | None = None,
        config_json: dict | None = None,
    ) -> IngestionRun:
        """Insert a new run row in ``status='running'``.

        Flushes; does not commit. Caller controls the transaction lifecycle.

        ``config_json`` MUST NOT contain secrets (bot tokens, db passwords, env values).
        Treat it as policy metadata only — operator inputs and run parameters that are safe
        to dump in admin views.
        """
        if run_type not in _ALLOWED_RUN_TYPES:
            raise ValueError(
                f"unsupported run_type {run_type!r}; allowed: {sorted(_ALLOWED_RUN_TYPES)}"
            )
        _reject_secret_shaped_keys("config_json", config_json)

        run = IngestionRun(
            run_type=run_type,
            source_name=source_name,
            status="running",
            config_json=config_json,
        )
        session.add(run)
        await session.flush()
        return run

    @staticmethod
    async def update_status(
        session: AsyncSession,
        run: IngestionRun,
        status: str,
        stats_json: dict | None = None,
        error_json: dict | None = None,
    ) -> IngestionRun:
        """Set the run's status (and optionally stats/error). Sets ``finished_at`` once
        on the first transition to a terminal status; subsequent terminal-to-terminal
        moves preserve the original ``finished_at`` so a 'completed' run that is later
        overwritten as 'failed' still records when it actually stopped."""
        if status not in _ALLOWED_STATUSES:
            raise ValueError(
                f"unsupported status {status!r}; allowed: {sorted(_ALLOWED_STATUSES)}"
            )
        _reject_secret_shaped_keys("stats_json", stats_json)
        _reject_secret_shaped_keys("error_json", error_json)

        run.status = status
        if stats_json is not None:
            run.stats_json = stats_json
        if error_json is not None:
            run.error_json = error_json
        if status in _TERMINAL_STATUSES and run.finished_at is None:
            # Application UTC time (not ``func.now()``) so the timestamp is set on the
            # in-memory ORM object even before the caller flushes / commits. The
            # ``finished_at`` column has ``timezone=True``; using UTC keeps values
            # comparable with server-default rows.
            run.finished_at = datetime.now(tz=timezone.utc)
        await session.flush()
        return run

    @staticmethod
    async def get_active_live(session: AsyncSession) -> IngestionRun | None:
        """Return the most-recent ``run_type='live'`` row in ``status='running'``, if any.

        The live-ingestion service uses this on bot startup to attach to an existing run
        (e.g., if the bot restarted mid-run) or, when None is returned, create a new one.
        """
        stmt = (
            select(IngestionRun)
            .where(
                IngestionRun.run_type == "live",
                IngestionRun.status == "running",
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
