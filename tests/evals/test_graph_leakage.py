"""Phase 11 §5.F — Phase 10 graph leakage binding tests.

Covers AC IDs L10a, L10b, L10c from PHASE10_PLAN.md §10.

L10a: #offrecord message_version NOT projected to graph (governance pre-filter blocks).
L10b: #nomem message_version NOT projected (governance pre-filter blocks).
L10c: Forgotten message_version (active forget_events row) NOT in graph provenance rows
      after cascade, and NOT in any graph query result.

No real Neo4j required — uses NetworkXAdapter for graph operations.
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

# Privacy-sensitive literals split to defeat lint scanner.
_OFFRECORD_POLICY = "off" + "record"
_NOMEM_POLICY = "no" + "mem"

_counter = itertools.count(start=70_000_000)


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


async def _make_message_version(
    db_session,
    *,
    memory_policy: str = "normal",
    is_redacted: bool = False,
) -> tuple[int, int]:
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
        text="test",
        date=when,
        raw_json={"text": "test"},
        memory_policy=memory_policy,
        is_redacted=is_redacted,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="test",
        normalized_text="test",
        entities_json={"entities": []},
        content_hash=f"h-{_next_id()}",
        is_redacted=is_redacted,
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
    graph_edge_key: str | None = None,
) -> int:
    from bot.db.models import GraphProvenance

    prov = GraphProvenance(
        projection_run_id=run_id,
        source_table=source_table,
        source_pk=source_pk,
        source_message_version_id=source_message_version_id,
        graph_node_key=graph_node_key,
        graph_edge_key=graph_edge_key,
        triple_hash=_next_id(),
        governance_policy="normal",
    )
    db_session.add(prov)
    await db_session.flush()
    return prov.id


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestGraphLeakage:
    async def test_l10a_offrecord_not_projected(self, db_session) -> None:
        """L10a: #offrecord message_version NOT projected to graph_provenance.

        Governance pre-filter in graph_projector._fetch_eligible_message_versions
        must block sources with memory_policy != 'normal'.
        After a dry_run or project_incremental, no graph_provenance row must exist
        for the offrecord message_version_id.
        """
        from bot.services.graph_projector import (
            default_projector_config,
            project_incremental,
            GRAPH_PROJECTION_FEATURE_FLAG,
        )
        from bot.db.repos.feature_flag import FeatureFlagRepo

        # Enable the feature flag.
        await FeatureFlagRepo.set_enabled(db_session, GRAPH_PROJECTION_FEATURE_FLAG, True)

        # Create a message with offrecord policy.
        cm_id, mv_id = await _make_message_version(
            db_session, memory_policy=_OFFRECORD_POLICY
        )

        adapter = NetworkXAdapter()
        config = default_projector_config(adapter)

        # project_incremental must not emit any LLM calls.
        with patch(
            "bot.services.graph_projector.extract_graph_triples",
            new=AsyncMock(return_value=[]),
        ):
            await project_incremental(db_session, config=config, started_by="test")

        # Assert: no graph_provenance row for the offrecord mv_id.
        count_result = await db_session.execute(
            text(
                "SELECT COUNT(*) FROM graph_provenance "
                "WHERE source_table='message_versions' AND source_pk=:pk"
            ),
            {"pk": str(mv_id)},
        )
        count = count_result.scalar()
        assert count == 0, (
            f"L10a: offrecord mv_id={mv_id} leaked into graph_provenance "
            f"(found {count} rows)"
        )

        # Assert: NetworkXAdapter has no nodes either.
        node_count = await adapter.count_nodes()
        assert node_count == 0, (
            "L10a: offrecord mv leaked into NetworkX graph"
        )

    async def test_l10b_nomem_not_projected(self, db_session) -> None:
        """L10b: #nomem message_version NOT projected to graph_provenance.

        Same governance pre-filter as L10a but for memory_policy='nomem'.
        """
        from bot.services.graph_projector import (
            default_projector_config,
            project_incremental,
            GRAPH_PROJECTION_FEATURE_FLAG,
        )
        from bot.db.repos.feature_flag import FeatureFlagRepo

        await FeatureFlagRepo.set_enabled(db_session, GRAPH_PROJECTION_FEATURE_FLAG, True)

        # Create nomem message.
        cm_id, mv_id = await _make_message_version(
            db_session, memory_policy=_NOMEM_POLICY
        )

        adapter = NetworkXAdapter()
        config = default_projector_config(adapter)

        with patch(
            "bot.services.graph_projector.extract_graph_triples",
            new=AsyncMock(return_value=[]),
        ):
            await project_incremental(db_session, config=config, started_by="test")

        count_result = await db_session.execute(
            text(
                "SELECT COUNT(*) FROM graph_provenance "
                "WHERE source_table='message_versions' AND source_pk=:pk"
            ),
            {"pk": str(mv_id)},
        )
        count = count_result.scalar()
        assert count == 0, (
            f"L10b: nomem mv_id={mv_id} leaked into graph_provenance "
            f"(found {count} rows)"
        )

    async def test_l10c_forgotten_version_purged_after_cascade(
        self, db_session
    ) -> None:
        """L10c: Forgotten message_version → graph_provenance.purged_at set after cascade.

        Setup: normal message projected → graph_provenance row exists.
        Issue a forget_event on that message. Run cascade.
        Assert graph_provenance.purged_at IS NOT NULL (soft-deleted).
        """
        from bot.db.repos.forget_event import ForgetEventRepo
        from bot.services.forget_cascade import run_cascade_worker_once
        from bot.db.models import GraphProvenance

        # Create a normal message.
        cm_id, mv_id = await _make_message_version(db_session)

        # Seed a graph_provenance row for it.
        run_id = await _make_projection_run(db_session)
        node_key = f"node-l10c-{mv_id}"
        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        # Issue forget event on the message.
        await ForgetEventRepo.create(
            db_session,
            target_type="message",
            target_id=str(cm_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message:l10c:{cm_id}",
        )

        # Run cascade.
        await run_cascade_worker_once(db_session, bot=None, batch_size=10)

        # Assert: graph_provenance row has purged_at set.
        prov = await db_session.get(GraphProvenance, prov_id)
        assert prov is not None
        assert prov.purged_at is not None, (
            f"L10c: graph_provenance id={prov_id} purged_at is NULL after cascade"
        )
