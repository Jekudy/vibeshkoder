"""Repository for ``butler_rate_buckets`` (T12-01).

Flush-only — NEVER calls ``session.commit()`` or ``session.rollback()``.
The caller owns the transaction lifecycle.

The critical method is ``try_increment``, which uses an atomic ON CONFLICT upsert
(single SQL statement, no read-then-write race) per PHASE12_PLAN_REFRESH.md §5.2:

    INSERT ... ON CONFLICT (bucket_kind, scope_id, bucket_key)
    DO UPDATE SET count = count + 1, updated_at = NOW()
    WHERE butler_rate_buckets.count < butler_rate_buckets.ceiling
    RETURNING id, count, ceiling

Empty RETURNING → ceiling already reached → returns False.
Non-empty RETURNING → count durably incremented within the caller's transaction → returns True.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# Raw SQL for the atomic upsert (no ORM — ON CONFLICT DO UPDATE WHERE
# is not expressible via SQLAlchemy ORM insert().on_conflict_do_update()).
_UPSERT_SQL = text("""
INSERT INTO butler_rate_buckets
    (bucket_kind, scope_id, bucket_key, window_start, window_end, count, ceiling)
VALUES
    (:bucket_kind, :scope_id, :bucket_key, :window_start, :window_end, 1, :ceiling)
ON CONFLICT (bucket_kind, scope_id, bucket_key)
DO UPDATE SET
    count = butler_rate_buckets.count + 1,
    updated_at = NOW()
WHERE butler_rate_buckets.count < butler_rate_buckets.ceiling
RETURNING id, count, ceiling
""")


class ButlerRateBucketRepo:
    """Data-access layer for ``butler_rate_buckets``."""

    @staticmethod
    async def try_increment(
        session: AsyncSession,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
        window_start: datetime,
        window_end: datetime,
        ceiling: int,
    ) -> bool:
        """Atomically increment the bucket count if below ceiling.

        Returns:
            True  — count successfully incremented (action is within limits).
            False — ceiling already reached (action should be rejected).

        The upsert is a single SQL statement — safe under concurrent writes.
        On INSERT (new bucket), count starts at 1.
        On UPDATE (existing bucket), count is incremented only if count < ceiling;
        the WHERE clause in DO UPDATE prevents the ceiling from being exceeded.

        Flushes; caller commits.
        """
        result = await session.execute(
            _UPSERT_SQL,
            {
                "bucket_kind": bucket_kind,
                "scope_id": scope_id,
                "bucket_key": bucket_key,
                "window_start": window_start,
                "window_end": window_end,
                "ceiling": ceiling,
            },
        )
        row = result.fetchone()
        # Empty RETURNING → the WHERE clause filtered the row → ceiling reached.
        if row is None:
            _log.debug(
                "butler_rate_buckets: ceiling reached "
                "kind=%s scope_id=%s key=%s ceiling=%d",
                bucket_kind,
                scope_id,
                bucket_key,
                ceiling,
            )
            return False

        _log.debug(
            "butler_rate_buckets: incremented id=%s count=%d ceiling=%d",
            row[0],
            row[1],
            row[2],
        )
        await session.flush()
        return True

    @staticmethod
    async def decrement(
        session: AsyncSession,
        *,
        bucket_kind: str,
        scope_id: int,
        bucket_key: str,
    ) -> None:
        """Decrement bucket count by 1 (floor 0).

        Used to roll back prior increments when a later rate-check fails within
        the same plan_action call (H1: partial-failure rollback).

        Only decrements if count > 0 — prevents negative counts.
        Does NOT create the row if absent (decrement of a non-existent bucket
        is a no-op; the row may have been removed by a concurrent transaction).

        Flushes; caller commits.
        """
        await session.execute(
            text("""
                UPDATE butler_rate_buckets
                SET count = GREATEST(0, count - 1),
                    updated_at = NOW()
                WHERE bucket_kind = :bucket_kind
                  AND scope_id = :scope_id
                  AND bucket_key = :bucket_key
            """),
            {
                "bucket_kind": bucket_kind,
                "scope_id": scope_id,
                "bucket_key": bucket_key,
            },
        )
        await session.flush()
