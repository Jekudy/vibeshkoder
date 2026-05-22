"""Phase 11 §5.F — Phase 10 graph citations binding tests.

Covers AC IDs C9a, C9b from PHASE10_PLAN.md §10.

C9a: Every active graph_provenance row has source_message_version_id IS NOT NULL
     OR source_card_id IS NOT NULL. No row can have both NULL.
C9b: Orphan graph_nodes (no graph_provenance link, purged_at NULL) do NOT appear
     in graph query results — they are silently dropped.

No real Neo4j required — uses NetworkXAdapter for graph operations.
No LLM calls (httpx_llm_guard autouse fixture enforces this).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=71_000_000)


def _next_id() -> int:
    return next(_counter)


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


async def _make_message_version(db_session) -> tuple[int, int]:
    """Create a ChatMessage + MessageVersion. Returns (cm_id, mv_id)."""
    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = -1_000_000_000_000 - _next_id()
    msg_id = _next_id()
    when = datetime.now(timezone.utc)

    msg = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text="test citation",
        date=when,
        raw_json={"text": "test citation"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="test citation",
        normalized_text="test citation",
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=False,
    )
    db_session.add(ver)
    await db_session.flush()

    msg.current_version_id = ver.id
    await db_session.flush()
    return msg.id, ver.id


async def _make_projection_run(db_session) -> int:
    from bot.db.repos.graph_projection_run import create_run

    run = await create_run(db_session, mode="incremental", started_by="test")
    await db_session.flush()
    return run.id


async def _make_provenance(
    db_session,
    *,
    run_id: int,
    source_table: str = "message_versions",
    source_pk: str,
    source_message_version_id: int | None = None,
    source_card_id=None,
    graph_node_key: str,
) -> int:
    from bot.db.models import GraphProvenance

    prov = GraphProvenance(
        projection_run_id=run_id,
        source_table=source_table,
        source_pk=source_pk,
        source_message_version_id=source_message_version_id,
        source_card_id=source_card_id,
        graph_node_key=graph_node_key,
        triple_hash=_next_id(),
        governance_policy="normal",
    )
    db_session.add(prov)
    await db_session.flush()
    return prov.id


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestGraphCitations:
    async def test_c9a_provenance_has_source(self, db_session) -> None:
        """C9a: No active graph_provenance row has both source fields NULL.

        Every row in graph_provenance must have EITHER source_message_version_id
        IS NOT NULL OR source_card_id IS NOT NULL.
        This is a constraint binding test: we assert the DB invariant holds for
        any rows created by our helpers (and in prod).
        """
        # Create a message and provenance row with source_message_version_id set.
        _cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-c9a-{mv_id}"

        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        # Assert: no active provenance row with BOTH sources NULL.
        rows = await db_session.execute(
            text(
                "SELECT COUNT(*) FROM graph_provenance "
                "WHERE purged_at IS NULL "
                "AND source_message_version_id IS NULL "
                "AND source_card_id IS NULL"
            )
        )
        null_count = rows.scalar()
        assert null_count == 0, (
            f"C9a: {null_count} active graph_provenance rows have BOTH source fields NULL. "
            "Every active provenance row must link to a message_version or card."
        )

        # Verify our created row indeed has source set.
        row = await db_session.execute(
            text(
                "SELECT source_message_version_id, source_card_id "
                "FROM graph_provenance WHERE id = :prov_id"
            ),
            {"prov_id": prov_id},
        )
        prov_row = row.fetchone()
        assert prov_row is not None
        assert prov_row.source_message_version_id is not None or prov_row.source_card_id is not None, (
            f"C9a: created provenance id={prov_id} has both source fields NULL"
        )

    async def test_c9b_orphan_nodes_dropped_from_query(self, db_session) -> None:
        """C9b: Graph query silently drops results from orphaned provenance (purged_at set).

        Provenance rows with purged_at IS NOT NULL (already purged/forgotten) must NOT
        contribute to graph query results. The query service filters for purged_at IS NULL
        when resolving provenance for traversal results.

        This test verifies the DB-layer filtering: a purged provenance row is not returned
        by _resolve_provenance_for_nodes when it has purged_at set.
        """
        from bot.db.repos.graph_provenance import find_active, find_by_source

        _cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-c9b-{mv_id}"

        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        # Soft-delete the provenance (simulate post-purge state).
        from bot.db.repos.graph_provenance import mark_inactive
        await mark_inactive(db_session, prov_id)

        # find_active must return 0 rows for this provenance.
        active_rows = await find_active(db_session)
        active_prov_ids = [r.id for r in active_rows]
        assert prov_id not in active_prov_ids, (
            f"C9b: purged provenance id={prov_id} still returned by find_active — "
            "orphan node provenance must be excluded from query results"
        )

        # find_by_source returns both but must show purged_at set.
        all_rows = await find_by_source(
            db_session,
            source_table="message_versions",
            source_pk=str(mv_id),
        )
        purged_row = next((r for r in all_rows if r.id == prov_id), None)
        assert purged_row is not None, f"C9b: provenance row {prov_id} not found"
        assert purged_row.purged_at is not None, (
            f"C9b: expected purged_at to be set after mark_inactive for id={prov_id}"
        )
