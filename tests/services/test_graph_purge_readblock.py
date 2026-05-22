"""Tests for bot/services/graph_purge_readblock.py (T10-06 / Phase 10).

Uses SQLite in-memory session (app_env fixture) to test the read-block helpers.
Verifies RFC-001:415 fail-closed semantics:
  - assert_no_pending_purge raises RefusalError when pending purge rows exist
  - assert_no_pending_purge passes silently when no pending rows exist
  - assert_no_pending_purge_for_source raises RefusalError on source match
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=7_000_000)


def _next_id() -> int:
    return next(_counter)


async def _insert_purge_pending(
    db_session,
    *,
    forget_event_id: int,
    source_table: str = "message_versions",
    source_pk: str = "1",
    graph_node_key: str | None = None,
    purged_at=None,
    failed_at=None,
) -> None:
    """Direct DB insert for test setup (bypasses repo to keep tests isolated)."""
    from bot.db.models import GraphPurgePending

    row = GraphPurgePending(
        forget_event_id=forget_event_id,
        source_table=source_table,
        source_pk=source_pk,
        graph_node_key=graph_node_key,
        purged_at=purged_at,
        failed_at=failed_at,
    )
    db_session.add(row)
    await db_session.flush()


@pytest.mark.asyncio
async def test_assert_no_pending_purge_raises_when_node_pending(db_session):
    """assert_no_pending_purge raises RefusalError when a node_key is pending."""
    from bot.services.graph_purge_readblock import assert_no_pending_purge
    from bot.services.graph_common import RefusalError

    node_key = f"node:test:{_next_id()}"
    forget_id = _next_id()
    await _insert_purge_pending(
        db_session,
        forget_event_id=forget_id,
        graph_node_key=node_key,
    )

    with pytest.raises(RefusalError):
        await assert_no_pending_purge(db_session, node_keys=[node_key])


@pytest.mark.asyncio
async def test_assert_no_pending_purge_passes_when_no_rows(db_session):
    """assert_no_pending_purge passes silently when no pending purge rows exist."""
    from bot.services.graph_purge_readblock import assert_no_pending_purge

    node_key = f"node:test:{_next_id()}"
    # No rows inserted — should not raise
    await assert_no_pending_purge(db_session, node_keys=[node_key])


@pytest.mark.asyncio
async def test_assert_no_pending_purge_passes_when_purged(db_session):
    """assert_no_pending_purge passes when the row has purged_at set."""
    from bot.services.graph_purge_readblock import assert_no_pending_purge

    node_key = f"node:test:{_next_id()}"
    forget_id = _next_id()
    await _insert_purge_pending(
        db_session,
        forget_event_id=forget_id,
        graph_node_key=node_key,
        purged_at=datetime.now(timezone.utc),
    )

    # Already purged — should not raise
    await assert_no_pending_purge(db_session, node_keys=[node_key])


@pytest.mark.asyncio
async def test_assert_no_pending_purge_for_source_raises(db_session):
    """assert_no_pending_purge_for_source raises RefusalError when source matches."""
    from bot.services.graph_purge_readblock import assert_no_pending_purge_for_source
    from bot.services.graph_common import RefusalError

    pk = str(_next_id())
    forget_id = _next_id()
    await _insert_purge_pending(
        db_session,
        forget_event_id=forget_id,
        source_table="message_versions",
        source_pk=pk,
    )

    with pytest.raises(RefusalError):
        await assert_no_pending_purge_for_source(
            db_session,
            source_table="message_versions",
            source_pk=pk,
        )


@pytest.mark.asyncio
async def test_assert_no_pending_purge_for_source_passes_when_no_rows(db_session):
    """assert_no_pending_purge_for_source passes when no pending rows for source."""
    from bot.services.graph_purge_readblock import assert_no_pending_purge_for_source

    pk = str(_next_id())
    await assert_no_pending_purge_for_source(
        db_session,
        source_table="knowledge_cards",
        source_pk=pk,
    )


# ─── Task 10.5-8: scope narrowing ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assert_no_pending_purge_scope_narrowed_to_message_versions(db_session):
    """assert_no_pending_purge with source_table_filter ignores non-matching rows.

    When a pending purge row has source_table='knowledge_cards' and the caller
    passes source_table_filter='message_versions', the row should NOT trigger
    the read-block — only message_versions purges matter for graph_query's
    pre-guard.

    Task 10.5-8: graph_query pre-guard scope narrowing.
    """
    from bot.services.graph_common import RefusalError
    from bot.services.graph_purge_readblock import assert_no_pending_purge

    node_key = f"node:test:{_next_id()}"
    forget_id = _next_id()
    # Insert a pending row for knowledge_cards (not message_versions)
    await _insert_purge_pending(
        db_session,
        forget_event_id=forget_id,
        source_table="knowledge_cards",
        graph_node_key=node_key,
    )

    # With source_table_filter='message_versions', must NOT raise
    # (knowledge_cards purge is excluded from this guard scope)
    await assert_no_pending_purge(
        db_session,
        node_keys=[node_key],
        source_table_filter="message_versions",
    )


@pytest.mark.asyncio
async def test_assert_no_pending_purge_scope_narrowed_still_blocks_mv(db_session):
    """assert_no_pending_purge with source_table_filter='message_versions' still blocks mv rows.

    When the pending row IS from message_versions, even with the filter, the
    read-block fires normally.

    Task 10.5-8.
    """
    from bot.services.graph_common import RefusalError
    from bot.services.graph_purge_readblock import assert_no_pending_purge

    node_key = f"node:test:{_next_id()}"
    forget_id = _next_id()
    await _insert_purge_pending(
        db_session,
        forget_event_id=forget_id,
        source_table="message_versions",
        graph_node_key=node_key,
    )

    # Even with the filter, message_versions row MUST trigger the read-block
    with pytest.raises(RefusalError):
        await assert_no_pending_purge(
            db_session,
            node_keys=[node_key],
            source_table_filter="message_versions",
        )
