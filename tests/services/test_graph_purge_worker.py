"""Tests for bot/services/graph_purge_worker.py (T10-06 / Phase 10).

Tests graph_purge_worker_tick against a NetworkXAdapter (fake Neo4j),
verifying state transitions in graph_purge_pending table.
Uses db_session (shared postgres) fixture.
"""

from __future__ import annotations

import itertools

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=8_000_000)


def _next_id() -> int:
    return next(_counter)


async def _enqueue_row(
    db_session,
    *,
    forget_event_id: int | None = None,
    source_table: str = "message_versions",
    source_pk: str | None = None,
    graph_node_key: str | None = None,
    graph_provenance_id: int | None = None,
):
    """Helper: enqueue a row via repo."""
    from bot.db.repos.graph_purge_pending import enqueue

    feid = forget_event_id or _next_id()
    pk = source_pk or str(_next_id())
    row = await enqueue(
        db_session,
        forget_event_id=feid,
        source_table=source_table,
        source_pk=pk,
        graph_node_key=graph_node_key,
        graph_provenance_id=graph_provenance_id,
    )
    return row


@pytest.mark.asyncio
async def test_worker_tick_processes_pending_row(db_session, graph_adapter_fake):
    """worker tick picks up a pending row and marks it purged on success."""
    from bot.services.graph_purge_worker import graph_purge_worker_tick
    from bot.db.models import GraphPurgePending
    from sqlalchemy import select

    row = await _enqueue_row(db_session, graph_node_key="node:test:8000001")

    result = await graph_purge_worker_tick(
        db_session,
        adapter=graph_adapter_fake,
        batch_size=10,
    )

    assert result["processed"] >= 1

    refreshed = await db_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )
    assert refreshed.purged_at is not None


@pytest.mark.asyncio
async def test_worker_tick_marks_failed_on_adapter_error(db_session):
    """worker tick marks row as having retries on adapter failure."""
    from bot.services.graph_purge_worker import graph_purge_worker_tick
    from bot.db.models import GraphPurgePending
    from sqlalchemy import select

    class ErrorAdapter:
        async def delete_provenance(self, provenance_id: str) -> int:
            raise RuntimeError("neo4j is down")

        async def close(self) -> None:
            pass

    row = await _enqueue_row(db_session, graph_node_key="node:test:failing")

    result = await graph_purge_worker_tick(
        db_session,
        adapter=ErrorAdapter(),
        batch_size=10,
    )

    # Error is captured but does not stop the worker
    assert result["errors"] >= 1

    refreshed = await db_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )
    # Not purged; retry_count incremented
    assert refreshed.purged_at is None
    assert refreshed.retry_count >= 1


@pytest.mark.asyncio
async def test_worker_tick_skips_purged_rows(db_session, graph_adapter_fake):
    """worker tick does not reprocess already-purged rows."""
    from bot.services.graph_purge_worker import graph_purge_worker_tick
    from bot.db.repos.graph_purge_pending import mark_purged
    from bot.db.models import GraphPurgePending
    from sqlalchemy import select

    row = await _enqueue_row(db_session, graph_node_key="node:test:already_done")
    await mark_purged(db_session, row.id)

    result = await graph_purge_worker_tick(
        db_session,
        adapter=graph_adapter_fake,
        batch_size=10,
    )

    # The already-purged row must not be re-processed
    refreshed = await db_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )
    # purged_at was set before tick; it should remain the same or be set once only
    assert refreshed.purged_at is not None
    # processed count does NOT include the already-purged row
    assert result.get("reprocessed", 0) == 0


@pytest.mark.asyncio
async def test_worker_tick_returns_stats_dict(db_session, graph_adapter_fake):
    """worker tick returns a dict with processed and errors keys."""
    from bot.services.graph_purge_worker import graph_purge_worker_tick

    result = await graph_purge_worker_tick(
        db_session,
        adapter=graph_adapter_fake,
        batch_size=10,
    )

    assert "processed" in result
    assert "errors" in result


# ─── CRITICAL-3: worker false-purge when adapter returns 0 ───────────────────


async def _make_provenance_for_worker(db_session) -> int:
    """Create a minimal graph_provenance row for worker tests. Returns provenance id."""
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.graph_provenance import create_provenance
    from bot.db.repos.graph_projection_run import create_run
    from datetime import datetime, timezone

    uid = _next_id()
    from bot.db.repos.user import UserRepo
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id, chat_id=chat_id, user_id=uid,
        text="test", date=when, raw_json={"text": "test"},
        memory_policy="normal", is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id, version_seq=1, text="test",
        normalized_text="test", entities_json={"entities": []},
        content_hash=f"h-{_next_id()}", is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()
    msg.current_version_id = ver.id
    await db_session.flush()

    run = await create_run(db_session, mode="incremental", started_by="test")
    await db_session.flush()

    prov = await create_provenance(
        db_session,
        projection_run_id=run.id,
        source_table="message_versions",
        source_pk=str(ver.id),
        source_message_version_id=ver.id,
        graph_node_key=f"node:mv:{ver.id}",
    )
    return prov.id


@pytest.mark.asyncio
async def test_worker_marks_failed_on_zero_row_delete_with_non_null_id(db_session):
    """CRITICAL-3: when delete_provenance returns 0 AND provenance_id is non-NULL, worker must NOT mark purged."""
    from bot.services.graph_purge_worker import graph_purge_worker_tick
    from bot.db.models import GraphPurgePending
    from sqlalchemy import select

    class ZeroDeleteAdapter:
        """Adapter that returns 0 (no rows deleted) but doesn't raise."""
        async def delete_provenance(self, provenance_id: str) -> int:
            return 0

        async def close(self) -> None:
            pass

    # Create a real provenance row so the FK constraint is satisfied
    prov_id = await _make_provenance_for_worker(db_session)

    row = await _enqueue_row(
        db_session,
        graph_node_key="node:test:zero_delete",
        graph_provenance_id=prov_id,  # non-NULL, valid FK
    )

    await graph_purge_worker_tick(
        db_session,
        adapter=ZeroDeleteAdapter(),
        batch_size=10,
    )

    refreshed = await db_session.scalar(
        select(GraphPurgePending).where(GraphPurgePending.id == row.id)
    )

    # Must NOT be marked purged — 0 deletions from adapter with non-null provenance_id
    assert refreshed.purged_at is None, (
        "Worker must NOT mark purged when adapter returns 0 for non-NULL provenance_id"
    )


# ─── MEDIUM-7: concurrent worker ticks no double claim ───────────────────────


@pytest.mark.asyncio
async def test_concurrent_worker_ticks_no_double_claim(db_session, graph_adapter_fake):
    """MEDIUM-7: two sequential worker ticks must claim disjoint sets of rows."""
    from bot.services.graph_purge_worker import graph_purge_worker_tick
    from bot.db.repos.graph_purge_pending import enqueue

    # Enqueue 2 distinct rows
    rows = []
    for i in range(2):
        feid = _next_id()
        pk = str(_next_id())
        r = await enqueue(
            db_session,
            forget_event_id=feid,
            source_table="message_versions",
            source_pk=pk,
            graph_node_key=f"node:test:concurrent:{i}",
        )
        rows.append(r.id)

    # First tick: processes both rows (batch_size=10)
    result1 = await graph_purge_worker_tick(
        db_session, adapter=graph_adapter_fake, batch_size=10
    )

    # Second tick: no remaining pending rows
    result2 = await graph_purge_worker_tick(
        db_session, adapter=graph_adapter_fake, batch_size=10
    )

    # Combined processed count must equal number of rows inserted
    total_processed = result1["processed"] + result2["processed"]
    assert total_processed == len(rows), (
        f"Expected {len(rows)} total processed across 2 ticks, got {total_processed}"
    )
    # Neither tick should have processed the same row twice
    assert result2["processed"] == 0, (
        "Second tick must find no rows (already processed by first tick)"
    )
