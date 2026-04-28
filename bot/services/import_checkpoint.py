"""Checkpoint / resume infrastructure for Telegram Desktop import (T2-NEW-E / issue #101).

This module owns the checkpoint API that the future import-apply path (#103) will consume.
It does NOT implement the apply path itself — that lives in Stream Delta (bot/services/import_apply.py).

Public API:
    init_or_resume_run(session, *, source_path, source_hash, chat_id, resume) -> ResumeDecision
    save_checkpoint(session, *, ingestion_run_id, last_processed_export_msg_id, chunk_index) -> None
    load_checkpoint(session, ingestion_run_id) -> Checkpoint | None
    finalize_run(session, *, ingestion_run_id, final_status, error_payload=None) -> None

Design choices:
- New module (not extending IngestionRunRepo) to minimize cross-stream surface; the repo
  stays a thin data-access layer for live ingestion; checkpoint logic is import-specific.
- save_checkpoint uses deep-merge semantics so operator-set keys in stats_json survive.
- finalize_run is idempotent: re-calling on an already-terminal run logs a warning and returns.
- Concurrent CLI safety: partial unique index on (source_hash) WHERE status='running' means
  the second concurrent caller's INSERT raises IntegrityError; we catch it and re-query to
  return block_partial_present.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import IngestionRun

logger = logging.getLogger(__name__)

# Non-terminal statuses where a run is considered "in-progress" (partial).
_PARTIAL_STATUSES = frozenset({"running", "failed"})
# Terminal statuses — finalize_run is a no-op when status is already in this set.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "dry_run"})


# ─── Public dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Checkpoint:
    """Snapshot of a checkpoint stored in ingestion_runs.stats_json."""

    ingestion_run_id: int
    source_path: str
    last_processed_export_msg_id: int | None
    chunk_index: int
    started_at: datetime
    last_updated_at: datetime
    status: Literal["running", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class ResumeDecision:
    """Decision returned by init_or_resume_run to the CLI caller."""

    mode: Literal["start_fresh", "resume_existing", "block_partial_present"]
    ingestion_run_id: int | None
    last_processed_export_msg_id: int | None
    reason: str


# ─── Public API ───────────────────────────────────────────────────────────────


async def init_or_resume_run(
    session: AsyncSession,
    *,
    source_path: str,
    source_hash: str,
    chat_id: int,
    resume: bool,
) -> ResumeDecision:
    """Decide whether to start fresh, resume, or block based on existing partial runs.

    Decision matrix (see docs/memory-system/import-checkpoint.md):

    | State on disk                            | resume? | Decision               |
    |------------------------------------------|---------|------------------------|
    | No prior run for this source_hash        | any     | start_fresh            |
    | Prior run, status='completed'            | any     | start_fresh            |
    | Prior run, status='running'|'failed'     | no      | block_partial_present  |
    | Prior run, status='running'|'failed'     | yes     | resume_existing        |
    | Different source_hash, non-terminal      | yes     | block_partial_present  |

    Race condition safety: If a concurrent caller already inserted a running row for the
    same source_hash, our INSERT will raise IntegrityError (partial unique index). We
    catch it, re-query, and return block_partial_present.
    """
    # First: look for a partial run matching the EXACT same source_hash.
    existing = await _find_partial_run_by_hash(session, source_hash)

    if existing is None:
        # No run matches this exact hash. But there may be a non-terminal run for the
        # SAME source_path with a DIFFERENT hash (file was re-exported / modified).
        # That is the hash-mismatch safety case: operator passes --resume thinking they
        # are resuming a prior run, but the file changed. Detect and block.
        if resume:
            path_conflict = await _find_partial_run_by_path(session, source_path)
            if path_conflict is not None and path_conflict.status in _PARTIAL_STATUSES:
                prior_hash = path_conflict.source_hash or ""
                return ResumeDecision(
                    mode="block_partial_present",
                    ingestion_run_id=None,
                    last_processed_export_msg_id=None,
                    reason=(
                        f"Source hash mismatch — resume not safe. "
                        f"Existing partial run {path_conflict.id} for path '{source_path}' has "
                        f"hash {prior_hash[:16]}..., but current file hash is {source_hash[:16]}.... "
                        "Finalize or cancel the old run first, or re-export from the original file."
                    ),
                )
        return await _create_fresh_run(session, source_path=source_path, source_hash=source_hash, chat_id=chat_id)

    prior_status = existing.status

    # If prior run is completed → treat as fresh (completed runs are immutable).
    if prior_status not in _PARTIAL_STATUSES:
        logger.info(
            "Prior %s run %d for source_hash=%s found; starting new run.",
            prior_status,
            existing.id,
            source_hash[:16],
        )
        return await _create_fresh_run(session, source_path=source_path, source_hash=source_hash, chat_id=chat_id)

    if not resume:
        return ResumeDecision(
            mode="block_partial_present",
            ingestion_run_id=None,
            last_processed_export_msg_id=None,
            reason=(
                f"Partial run {existing.id} (status={prior_status!r}) found for this source. "
                "Use --resume to continue it, or finalize the prior run manually."
            ),
        )

    # resume=True and same hash → resume_existing
    chk = await load_checkpoint(session, existing.id)
    resume_point = chk.last_processed_export_msg_id if chk else None
    return ResumeDecision(
        mode="resume_existing",
        ingestion_run_id=existing.id,
        last_processed_export_msg_id=resume_point,
        reason=(
            f"Resuming partial run {existing.id} (status={prior_status!r}) "
            f"from export_msg_id={resume_point}."
        ),
    )


async def save_checkpoint(
    session: AsyncSession,
    *,
    ingestion_run_id: int,
    last_processed_export_msg_id: int,
    chunk_index: int,
) -> None:
    """Update ingestion_runs.stats_json with deep-merge semantics.

    Preserves existing keys (e.g. operator-set fields). Atomically adds/updates:
        - last_processed_export_msg_id
        - chunk_index
        - last_checkpoint_at (ISO8601 UTC)

    Uses PostgreSQL jsonb merge (||) to perform the deep-merge atomically. This is safe
    to call from a caller-managed transaction — does NOT commit.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    patch = {
        "last_processed_export_msg_id": last_processed_export_msg_id,
        "chunk_index": chunk_index,
        "last_checkpoint_at": now_iso,
    }
    # Use JSONB || merge: preserves existing keys, overwrites checkpoint keys.
    # COALESCE handles the NULL initial case.
    import json

    await session.execute(
        text(
            """
            UPDATE ingestion_runs
               SET stats_json = COALESCE(stats_json, '{}'::jsonb) || :patch::jsonb
             WHERE id = :id
            """
        ),
        {"id": ingestion_run_id, "patch": json.dumps(patch)},
    )
    await session.flush()


async def load_checkpoint(
    session: AsyncSession,
    ingestion_run_id: int,
) -> Checkpoint | None:
    """Load the current checkpoint state for a run.

    Returns None if the run does not exist. Returns a Checkpoint even when no
    save_checkpoint has been called yet (last_processed_export_msg_id will be None).
    """
    stmt = select(IngestionRun).where(IngestionRun.id == ingestion_run_id)
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        return None

    stats = run.stats_json or {}
    return Checkpoint(
        ingestion_run_id=run.id,
        source_path=run.source_name or "",
        last_processed_export_msg_id=stats.get("last_processed_export_msg_id"),
        chunk_index=stats.get("chunk_index", 0),
        started_at=run.started_at,
        last_updated_at=_parse_checkpoint_ts(stats.get("last_checkpoint_at")) or run.started_at,
        status=run.status,  # type: ignore[arg-type]
    )


async def finalize_run(
    session: AsyncSession,
    *,
    ingestion_run_id: int,
    final_status: Literal["completed", "failed", "cancelled"],
    error_payload: dict | None = None,
) -> None:
    """Set a run to a terminal status. Idempotent: re-calling on an already-terminal run
    logs a warning and returns without modifying the row.

    Sets:
        - status = final_status
        - finished_at = now (first transition only; preserved on re-call)
        - error_json = error_payload (for failed/cancelled)
    """
    stmt = select(IngestionRun).where(IngestionRun.id == ingestion_run_id)
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        raise ValueError(f"ingestion_run {ingestion_run_id} not found in finalize_run")

    if run.status in _TERMINAL_STATUSES:
        logger.warning(
            "finalize_run called on already-terminal run %d (status=%r); no-op.",
            ingestion_run_id,
            run.status,
        )
        return

    run.status = final_status
    if run.finished_at is None:
        run.finished_at = datetime.now(tz=timezone.utc)
    if error_payload is not None:
        run.error_json = error_payload
    await session.flush()


# ─── Internal helpers ─────────────────────────────────────────────────────────


async def _find_partial_run_by_hash(
    session: AsyncSession,
    source_hash: str,
) -> IngestionRun | None:
    """Find the most recent import run matching source_hash exactly (any status).

    Returns any status so the caller can decide (completed → start fresh, partial → block/resume).
    """
    stmt = (
        select(IngestionRun)
        .where(
            IngestionRun.source_hash == source_hash,
            IngestionRun.run_type == "import",
        )
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_partial_run_by_path(
    session: AsyncSession,
    source_path: str,
) -> IngestionRun | None:
    """Find the most recent import run for a source_path (regardless of hash).

    Used for the hash-mismatch safety check: the operator re-exported the file (new hash)
    but a partial run for the OLD hash of the same path is still in-progress.
    Returns any status; caller checks .status and .source_hash.
    """
    stmt = (
        select(IngestionRun)
        .where(
            IngestionRun.source_name == source_path,
            IngestionRun.run_type == "import",
        )
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _create_fresh_run(
    session: AsyncSession,
    *,
    source_path: str,
    source_hash: str,
    chat_id: int,
) -> ResumeDecision:
    """Insert a new ingestion_run row with status='running'.

    Handles the concurrent-insertion race: if a parallel caller already inserted a
    running row for the same source_hash (partial unique index fires), catch
    IntegrityError, re-query, and return block_partial_present.
    """
    run = IngestionRun(
        run_type="import",
        source_name=source_path,
        source_hash=source_hash,
        status="running",
        config_json={"chat_id": chat_id},
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent caller already inserted a running row for this source_hash.
        await session.rollback()
        # Re-query to get the winning row.
        existing = await _find_partial_run_by_hash(session, source_hash)
        return ResumeDecision(
            mode="block_partial_present",
            ingestion_run_id=None,
            last_processed_export_msg_id=None,
            reason=(
                f"Concurrent import_apply detected: run {existing.id if existing else '?'} "
                f"already started for this source_hash. Use --resume if the other process died."
            ),
        )

    return ResumeDecision(
        mode="start_fresh",
        ingestion_run_id=run.id,
        last_processed_export_msg_id=None,
        reason=f"No prior partial run found; created new run {run.id}.",
    )


def _parse_checkpoint_ts(ts_str: str | None) -> datetime | None:
    if ts_str is None:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
