"""Race-free concurrent increment test for butler_rate_buckets (T12-01).

Verifies that the ON CONFLICT atomic upsert pattern is race-free:
- 2 concurrent async tasks race to increment the same bucket.
- Final count equals the number of successful increments.
- Ceiling is never exceeded.

Each task runs in its own connection/session to simulate real concurrency.
The outer-transaction isolation of db_session cannot be used here because
both sessions need to see each other's committed writes.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=7_900_000_000)


def _next_id() -> int:
    return next(_counter)


async def _increment_task(
    url: str,
    bucket_kind: str,
    scope_id: int,
    bucket_key: str,
    window_start: datetime,
    window_end: datetime,
    ceiling: int,
    n: int,
) -> list[bool]:
    """Run `n` try_increment calls in a fresh session and return results."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

    engine = create_async_engine(url, echo=False)
    try:
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        results = []
        async with Session() as session:
            async with session.begin():
                from bot.db.repos.butler_rate_bucket import ButlerRateBucketRepo
                for _ in range(n):
                    ok = await ButlerRateBucketRepo.try_increment(
                        session,
                        bucket_kind=bucket_kind,
                        scope_id=scope_id,
                        bucket_key=bucket_key,
                        window_start=window_start,
                        window_end=window_end,
                        ceiling=ceiling,
                    )
                    results.append(ok)
        return results
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_increment_race_free(postgres_engine) -> None:
    """Two concurrent tasks each trying to increment the same bucket by 3.

    Ceiling = 4. Expected: exactly 4 successes and 2 failures across both tasks.
    The ceiling is never exceeded.
    """
    import os

    url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://shkoder_dev:shkoder_dev@127.0.0.1:5433/shkoder_dev"
    )

    scope_id = _next_id()
    bucket_key = f"day:2026-05-25-race-{scope_id}"
    window_start = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)
    ceiling = 4
    bucket_kind = "user_plans_day"

    # Run 3 increments in task A and 3 increments in task B concurrently.
    results_a, results_b = await asyncio.gather(
        _increment_task(
            url, bucket_kind, scope_id, bucket_key,
            window_start, window_end, ceiling, 3
        ),
        _increment_task(
            url, bucket_kind, scope_id, bucket_key,
            window_start, window_end, ceiling, 3
        ),
    )

    all_results = results_a + results_b
    successes = sum(1 for r in all_results if r)
    failures = sum(1 for r in all_results if not r)

    # Exactly `ceiling` increments should succeed; the rest should fail.
    assert successes == ceiling, (
        f"Expected exactly {ceiling} successes, got {successes}. "
        f"Results: A={results_a} B={results_b}"
    )
    assert failures == len(all_results) - ceiling, (
        f"Expected {len(all_results) - ceiling} failures, got {failures}."
    )

    # Verify the DB count matches exactly.
    from sqlalchemy import text
    async with postgres_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT count FROM butler_rate_buckets "
                "WHERE bucket_kind = :kind AND scope_id = :sid AND bucket_key = :key"
            ),
            {"kind": bucket_kind, "sid": scope_id, "key": bucket_key},
        )
        db_count = row.scalar_one()

    assert db_count == ceiling, (
        f"DB count={db_count} does not match ceiling={ceiling}. "
        f"Ceiling was exceeded!"
    )

    # Cleanup: delete the test row so it doesn't pollute other tests.
    async with postgres_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM butler_rate_buckets "
                "WHERE bucket_kind = :kind AND scope_id = :sid AND bucket_key = :key"
            ),
            {"kind": bucket_kind, "sid": scope_id, "key": bucket_key},
        )
