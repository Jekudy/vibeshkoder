"""Phase 11 additional invariant — graph_no_llm_in_rebuild.

Verifies: project_full_rebuild creates NO new llm_usage_ledger rows with
call_type='graph_projection' (replay-only invariant).

project_full_rebuild is REPLAY-ONLY from stored graph_provenance/graph_edges.
No LLM extraction occurs. This test guards against regressions that would
accidentally invoke extract_graph_triples during full_rebuild.

No real Neo4j required — uses NetworkXAdapter.
No LLM calls (httpx_llm_guard autouse fixture enforces this).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from bot.services.graph_adapter import NetworkXAdapter

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=75_000_000)


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
        text="no-llm test",
        date=when,
        raw_json={"text": "no-llm test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="no-llm test",
        normalized_text="no-llm test",
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
    graph_node_key: str,
) -> int:
    from bot.db.models import GraphProvenance

    prov = GraphProvenance(
        projection_run_id=run_id,
        source_table=source_table,
        source_pk=source_pk,
        source_message_version_id=source_message_version_id,
        graph_node_key=graph_node_key,
        triple_hash=f"th-{_next_id()}",
        governance_policy="normal",
    )
    db_session.add(prov)
    await db_session.flush()
    return prov.id


async def _make_graph_edge(
    db_session,
    *,
    provenance_id: int,
    subject_node_key: str,
    object_node_key: str,
    predicate: str = "RELATED_TO",
) -> int:
    from bot.db.models import GraphEdge

    edge = GraphEdge(
        graph_provenance_id=provenance_id,
        subject_node_key=subject_node_key,
        object_node_key=object_node_key,
        predicate=predicate,
        edge_key=f"ek-{_next_id()}",
        confidence_score=0.9,
    )
    db_session.add(edge)
    await db_session.flush()
    return edge.id


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestGraphNoLlmInRebuild:
    async def test_full_rebuild_no_llm_calls(self, db_session) -> None:
        """project_full_rebuild does NOT call extract_graph_triples (replay-only).

        Setup:
        1. Seed graph_provenance + graph_edges rows in Postgres.
        2. Enable the graph projection feature flag.
        3. Patch extract_graph_triples to track calls.
        4. Call project_full_rebuild.
        5. Assert extract_graph_triples was NOT called.
        6. Assert no new llm_usage_ledger rows with call_type='graph_projection' appear.
        """
        from bot.db.repos.feature_flag import FeatureFlagRepo
        from bot.services.graph_projector import (
            default_projector_config,
            project_full_rebuild,
            GRAPH_PROJECTION_FEATURE_FLAG,
        )

        await FeatureFlagRepo.set_enabled(db_session, GRAPH_PROJECTION_FEATURE_FLAG, True)

        # Seed data for rebuild.
        _cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-nollm-{mv_id}"
        obj_key = f"obj-nollm-{mv_id}"
        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )
        await _make_graph_edge(
            db_session,
            provenance_id=prov_id,
            subject_node_key=node_key,
            object_node_key=obj_key,
        )

        # Count ledger rows before rebuild (graph_projection call_type).
        before_count_result = await db_session.execute(
            text(
                "SELECT COUNT(*) FROM llm_usage_ledger "
                "WHERE call_type = 'graph_projection'"
            )
        )
        before_count = before_count_result.scalar() or 0

        # Patch extract_graph_triples to detect any accidental calls.
        mock_extractor = AsyncMock(return_value=[])

        adapter = NetworkXAdapter()
        config = default_projector_config(adapter)

        with patch(
            "bot.services.graph_projector.extract_graph_triples",
            new=mock_extractor,
        ):
            result = await project_full_rebuild(db_session, config=config, started_by="test")

        # Assert: extract_graph_triples was never called during full_rebuild.
        assert mock_extractor.call_count == 0, (
            f"No-LLM rebuild invariant violated: extract_graph_triples was called "
            f"{mock_extractor.call_count} time(s) during project_full_rebuild. "
            "full_rebuild is REPLAY-ONLY — no LLM calls allowed."
        )

        # Assert: no new ledger rows for graph_projection (replay creates none).
        after_count_result = await db_session.execute(
            text(
                "SELECT COUNT(*) FROM llm_usage_ledger "
                "WHERE call_type = 'graph_projection'"
            )
        )
        after_count = after_count_result.scalar() or 0

        assert after_count == before_count, (
            f"No-LLM rebuild invariant violated: llm_usage_ledger gained "
            f"{after_count - before_count} new graph_projection row(s) during "
            "project_full_rebuild. Replay-only rebuild must not write ledger entries."
        )

        # Confirm rebuild actually ran (sanity check).
        assert result.status == "completed", (
            f"project_full_rebuild returned unexpected status={result.status!r}"
        )
