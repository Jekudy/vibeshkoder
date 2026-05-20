"""Async purge worker for Neo4j bolt DELETE (T10-06 / Phase 10).

Drives graph_purge_pending rows through Neo4j DETACH DELETE via bolt.
Consumes rows claimed via claim_batch (SELECT ... FOR UPDATE SKIP LOCKED) and
calls adapter.delete_provenance for each.

On success: marks row purged_at = now().
On failure: increments retry_count; sets failed_at after MAX_RETRIES (DLQ).
Continues to next row on failure (does not abort the batch).

Race safety:
- claim_batch uses FOR UPDATE SKIP LOCKED — multi-worker safe.
- Rows already purged (purged_at IS NOT NULL) are excluded by claim_batch.
- Stale rows are idempotently retried.

Feature flag: memory.graph.write_pending.paused (default OFF).
When ON, the worker returns immediately without consuming any rows.
Kill-switch for Neo4j downtime in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.repos.graph_purge_pending import claim_batch, mark_failed, mark_purged

_log = logging.getLogger(__name__)

# Feature flag key for the kill-switch.
GRAPH_PURGE_PAUSED_FLAG = "memory.graph.write_pending.paused"


@dataclass
class PurgeTickResult:
    """Result of one graph_purge_worker_tick call."""

    processed: int = 0
    errors: int = 0
    skipped_paused: bool = False
    reprocessed: int = 0  # rows already purged (should never happen via claim_batch)


async def graph_purge_worker_tick(
    session: AsyncSession,
    *,
    adapter: Any,  # GraphAdapter protocol — imported lazily to avoid circular dep
    batch_size: int = 20,
) -> dict[str, Any]:
    """Consume up to batch_size pending purge rows and issue Neo4j DELETE calls.

    Returns a dict with keys: processed, errors, skipped_paused.

    Feature flag kill-switch: if memory.graph.write_pending.paused is ON,
    returns immediately with skipped_paused=True.

    For each claimed row:
      1. Call adapter.delete_provenance(str(row.graph_provenance_id or row.graph_node_key)).
      2. On success: mark_purged.
      3. On failure: mark_failed with error message; continue to next row.

    Idempotent: rows already purged are not in the claim_batch result set
    (purged_at IS NULL filter), so they are never reprocessed.
    """
    # Feature flag check — kill-switch for purge worker.
    from bot.db.repos.feature_flag import FeatureFlagRepo

    flag_paused = await FeatureFlagRepo.get(session, GRAPH_PURGE_PAUSED_FLAG)
    if flag_paused:
        _log.info(
            "graph_purge_worker: flag %s is ON — skipping tick",
            GRAPH_PURGE_PAUSED_FLAG,
        )
        return {"processed": 0, "errors": 0, "skipped_paused": True}

    rows = await claim_batch(session, batch_size=batch_size)
    processed = 0
    errors = 0

    for row in rows:
        # Build the provenance identifier: prefer graph_provenance_id (numeric FK),
        # fall back to graph_node_key (string identifier).
        purge_key = (
            str(row.graph_provenance_id)
            if row.graph_provenance_id is not None
            else row.graph_node_key or ""
        )

        try:
            deleted_count = await adapter.delete_provenance(purge_key)

            # CRITICAL-3 / HIGH-6: verify adapter actually deleted data.
            # If delete_provenance returns 0 AND the provenance_id is non-NULL,
            # this is unexpected — the Neo4j adapter may be drifting. Log a
            # structured warning so ops can detect adapter drift.
            # We still mark purged (idempotency assumption: 0 deletions may mean
            # Neo4j never had this provenance or it was already purged by a prior
            # tick). BUT only when provenance_id was NULL (fall-back path) or the
            # return is truthy.  When provenance_id is non-NULL and count==0: log
            # warning, do NOT mark purged — mark as failed so ops can investigate.
            if deleted_count == 0 and row.graph_provenance_id is not None:
                error_msg = (
                    f"adapter.delete_provenance returned 0 deletions for non-NULL "
                    f"graph_provenance_id={row.graph_provenance_id} (purge_key={purge_key!r}); "
                    "Neo4j data may be missing or adapter is broken"
                )
                _log.warning(
                    "graph_purge_worker: zero-delete id=%s purge_key=%s prov_id=%s — "
                    "marking failed for manual review",
                    row.id,
                    purge_key,
                    row.graph_provenance_id,
                    extra={"graph_purge_zero_delete": True},
                )
                try:
                    await mark_failed(session, row.id, error_msg=error_msg)
                    await session.flush()
                except Exception:
                    _log.exception(
                        "graph_purge_worker: could not mark_failed for id=%s", row.id
                    )
                errors += 1
                continue

            await mark_purged(session, row.id)
            await session.flush()
            processed += 1
            _log.debug(
                "graph_purge_worker: purged id=%s purge_key=%s deleted=%s",
                row.id,
                purge_key,
                deleted_count,
            )
        except Exception as exc:
            error_msg = str(exc)[:500]
            _log.warning(
                "graph_purge_worker: failed id=%s purge_key=%s error=%s",
                row.id,
                purge_key,
                error_msg,
            )
            try:
                await mark_failed(session, row.id, error_msg=error_msg)
                await session.flush()
            except Exception:
                _log.exception(
                    "graph_purge_worker: could not mark_failed for id=%s", row.id
                )
            errors += 1

    _log.info(
        "graph_purge_worker: tick processed=%d errors=%d batch_size=%d",
        processed,
        errors,
        batch_size,
    )
    return {"processed": processed, "errors": errors, "skipped_paused": False}
