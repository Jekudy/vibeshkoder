"""T6-09 acceptance test — advisory lock cross-transaction collision.

PHASE6_PLAN.md §5.A.5 + §5.C step 2 + T6-04 acceptance bullet 6:

    T6-09 advisory-lock collision test MUST pass on T6-04 PR head.

Pins the H-Cdx-2 race-window closure:

* Transaction A (cascade orchestrator) holds
  ``pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(M))`` for an mvid M.
* Transaction B (``/approve``) attempts to acquire the same lock for M.
* B MUST block on A's lock — neither side races to write conflicting state.
* When A commits/rolls back, B unblocks and proceeds.

Verifies the canonical serialization contract that both ``/approve`` (§5.C
step 2) and ``_process_one_event`` (§5.A.5 step 1) implement against the
same lock namespace (``_p6_mvid_advisory_lock_id``).

This test runs against real Postgres only (no SQLite fallback — the lock
SQL is Postgres-specific). The ``postgres_engine`` fixture skips when PG
is unreachable.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")


# A reserved sample mvid for collision tests — does NOT need to exist in
# message_versions because we exercise the lock primitive in isolation. The
# real /approve and cascade paths separately verify they call the helper.
_SAMPLE_MVID = 770_088_001


# ─── Test 1: lock_id derivation matches across call sites ────────────────────


def test_p6_mvid_advisory_lock_id_single_source_of_truth() -> None:
    """Both /approve and _process_one_event MUST resolve the same mvid to the
    same lock_id — the lock derivation must live in exactly one place.

    Smoke test: importing ``_p6_mvid_advisory_lock_id`` from two different
    code paths returns identical values for the same mvid. If a future
    refactor accidentally forks the helper (e.g. inlines it in /approve),
    this test fires.
    """
    # Both /approve and the cascade import from bot.services.forget_cascade.
    from bot.handlers.admin_cards import _p6_mvid_advisory_lock_id as via_handler
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id as via_cascade

    for mvid in [0, 1, 42, 4_242, _SAMPLE_MVID, 2**31 - 1]:
        assert via_handler(mvid) == via_cascade(mvid), (
            f"derivation drift at mvid={mvid}: /approve and cascade got "
            "different lock_ids; this re-opens the H-Cdx-2 race window"
        )


# ─── Test 2: lock acquired on one connection blocks another ──────────────────


async def test_advisory_xact_lock_blocks_cross_connection(postgres_engine) -> None:
    """A transaction holding ``pg_advisory_xact_lock(L)`` blocks a second
    transaction trying to acquire the same lock until the first commits.

    Asserts via ``pg_try_advisory_lock`` (non-blocking attempt) from the
    second connection — it MUST return FALSE while connection 1 holds the
    xact lock.
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    lock_id = _p6_mvid_advisory_lock_id(_SAMPLE_MVID)

    async with postgres_engine.connect() as conn_a:
        trans_a = await conn_a.begin()
        try:
            # conn_a acquires the xact lock — held until trans_a commits.
            await conn_a.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )

            # conn_b TRIES to acquire the SAME lock via pg_try_advisory_lock
            # (session-scoped non-blocking variant).
            async with postgres_engine.connect() as conn_b:
                result = await conn_b.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
                acquired = result.scalar()
                # The xact lock on conn_a means conn_b cannot get it.
                assert acquired is False, (
                    "pg_try_advisory_lock returned TRUE while another "
                    "transaction holds the xact lock on the same key — "
                    "P6 serialization contract is broken"
                )
        finally:
            await trans_a.rollback()


# ─── Test 3: lock released on commit unblocks waiter ─────────────────────────


async def test_advisory_xact_lock_released_on_commit(postgres_engine) -> None:
    """After the holding transaction commits, the lock is released and
    another transaction can acquire it freely."""
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    lock_id = _p6_mvid_advisory_lock_id(_SAMPLE_MVID + 1)

    # Stage 1: conn_a takes the xact lock + commits → lock released.
    async with postgres_engine.connect() as conn_a:
        trans_a = await conn_a.begin()
        await conn_a.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
        await trans_a.commit()

    # Stage 2: conn_b should now be able to acquire the lock with
    # pg_try_advisory_lock = TRUE.
    async with postgres_engine.connect() as conn_b:
        result = await conn_b.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
        assert result.scalar() is True
        await conn_b.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": lock_id},
        )


# ─── Test 4: concurrent acquirers serialize (one waits, one proceeds) ────────


async def test_concurrent_lock_acquirers_serialize(postgres_engine) -> None:
    """Two concurrent coroutines both calling pg_advisory_xact_lock on the
    same key serialize: the second one waits until the first commits, then
    acquires the lock and completes its work.

    Simulates the ``/approve`` vs forget-cascade interleaving on the same
    source mvid. The test does NOT touch knowledge_cards / forget_events —
    only the lock primitive — because we already have unit tests for both
    call sites and the lock derivation. The interleaving guarantee is the
    primary invariant T6-09 exists to prove.
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    lock_id = _p6_mvid_advisory_lock_id(_SAMPLE_MVID + 2)

    completion_order: list[str] = []
    holder_inside_event = asyncio.Event()
    waiter_started = asyncio.Event()

    async def holder() -> None:
        async with postgres_engine.connect() as conn:
            trans = await conn.begin()
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
            # Signal that the holder has the lock.
            holder_inside_event.set()
            # Wait for the waiter coroutine to be actively blocking on the
            # lock before we commit (so the test exercises the contention
            # path, not just sequential ordering).
            await waiter_started.wait()
            # Brief sleep so the waiter has time to start blocking on
            # pg_advisory_xact_lock.
            await asyncio.sleep(0.1)
            await trans.commit()
            completion_order.append("holder")

    async def waiter() -> None:
        await holder_inside_event.wait()
        async with postgres_engine.connect() as conn:
            trans = await conn.begin()
            # Signal that we are about to block.
            waiter_started.set()
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
            # If we got here, the holder has released the lock.
            await trans.commit()
            completion_order.append("waiter")

    await asyncio.gather(holder(), waiter())

    # Holder MUST finish first because waiter is blocking on its lock.
    assert completion_order == ["holder", "waiter"], (
        f"expected holder→waiter serialization; got {completion_order}"
    )


# ─── Test 5: namespaced — different mvids don't block each other ─────────────


async def test_different_mvids_dont_block(postgres_engine) -> None:
    """Two locks on DIFFERENT mvids do not block each other — the lock
    namespace is mvid-keyed, not global."""
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    lock_id_a = _p6_mvid_advisory_lock_id(_SAMPLE_MVID + 10)
    lock_id_b = _p6_mvid_advisory_lock_id(_SAMPLE_MVID + 11)

    async with postgres_engine.connect() as conn_a:
        trans_a = await conn_a.begin()
        try:
            await conn_a.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id_a},
            )
            # A SECOND connection can take lock_b without blocking on lock_a.
            async with postgres_engine.connect() as conn_b:
                result = await conn_b.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id_b},
                )
                assert result.scalar() is True, (
                    "different mvids must hash to different locks; got "
                    "blocking behaviour across mvid namespaces"
                )
                await conn_b.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": lock_id_b},
                )
        finally:
            await trans_a.rollback()
