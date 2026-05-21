"""Phase 11 §5.F — Phase 10 graph refusal binding tests.

Covers AC IDs R7a, R7b, R7c, R7d from PHASE10_PLAN.md §10.

R7a: graph_query when memory.graph.query.enabled=False → GraphQueryDisabledError.
R7b: graph_query when memory.graph.write_pending.paused=True → GraphQueryDisabledError.
R7c: project_full_rebuild requires advisory lock; feature disabled raises ServiceDisabledError.
R7d: graph_query.find_related_topics abstains while graph_purge_pending row exists with
     purged_at IS NULL (pending-purge read-block; RFC-001:415 pattern).

No real Neo4j required — uses NetworkXAdapter for graph operations.
No LLM calls (httpx_llm_guard autouse fixture enforces this).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from bot.services.graph_adapter import NetworkXAdapter

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=73_000_000)


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
        text="refusal test",
        date=when,
        raw_json={"text": "refusal test"},
        memory_policy="normal",
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    ver = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text="refusal test",
        normalized_text="refusal test",
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


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestGraphRefusal:
    async def test_r7a_query_disabled_when_flag_off(self, db_session) -> None:
        """R7a: graph_query.find_related_topics raises GraphQueryDisabledError when
        memory.graph.query.enabled is False (default is OFF).

        Spec: graph_query._is_query_enabled returns False when flag missing/false.
        All traversal functions check this before executing Cypher.
        """
        from bot.services.graph_query import find_related_topics, GraphQueryDisabledError

        adapter = NetworkXAdapter()

        # Flag is not set (default OFF) — should raise GraphQueryDisabledError.
        with pytest.raises(GraphQueryDisabledError):
            await find_related_topics(
                db_session,
                adapter,
                topic="test topic",
                viewer_is_admin=True,
                max_hops=2,
                max_results=10,
            )

    async def test_r7b_query_disabled_when_paused(self, db_session) -> None:
        """R7b: graph_query raises GraphQueryDisabledError when
        memory.graph.write_pending.paused is True, even if query flag is ON.

        The paused kill-switch must override the query-enabled flag.
        """
        from bot.db.repos.feature_flag import FeatureFlagRepo
        from bot.services.graph_query import (
            find_related_topics,
            GraphQueryDisabledError,
            GRAPH_QUERY_FEATURE_FLAG,
        )

        adapter = NetworkXAdapter()

        # Enable query flag but set paused kill-switch.
        await FeatureFlagRepo.set_enabled(db_session, GRAPH_QUERY_FEATURE_FLAG, True)
        await FeatureFlagRepo.set_enabled(db_session, "memory.graph.write_pending.paused", True)

        with pytest.raises(GraphQueryDisabledError):
            await find_related_topics(
                db_session,
                adapter,
                topic="test topic",
                viewer_is_admin=True,
                max_hops=2,
                max_results=10,
            )

    async def test_r7c_full_rebuild_disabled_when_flag_off(self, db_session) -> None:
        """R7c: project_full_rebuild raises ServiceDisabledError when feature flag is OFF.

        Advisory lock cannot be acquired if the service isn't enabled.
        ServiceDisabledError must be raised before advisory lock acquisition.
        """
        from bot.services.graph_projector import (
            project_full_rebuild,
            ServiceDisabledError,
            default_projector_config,
        )

        adapter = NetworkXAdapter()
        config = default_projector_config(adapter)

        # Feature flag NOT set → disabled by default.
        with pytest.raises(ServiceDisabledError):
            await project_full_rebuild(db_session, config=config, started_by="test")

    async def test_r7d_query_abstains_during_pending_purge(self, db_session) -> None:
        """R7d: find_related_topics returns abstained=True while graph_purge_pending
        has a non-purged row for a queried node (RFC-001:415 pending-purge read-block).

        Setup:
        1. Enable query flag.
        2. Seed graph_provenance + graph_purge_pending (purged_at IS NULL) for node_key.
        3. Seed NetworkXAdapter with that node.
        4. Call find_related_topics — must return abstained=True.
        """
        from bot.db.repos.feature_flag import FeatureFlagRepo
        from bot.db.models import GraphPurgePending
        from bot.services.graph_query import find_related_topics, GRAPH_QUERY_FEATURE_FLAG

        await FeatureFlagRepo.set_enabled(db_session, GRAPH_QUERY_FEATURE_FLAG, True)

        _cm_id, mv_id = await _make_message_version(db_session)
        run_id = await _make_projection_run(db_session)
        node_key = f"node-r7d-{mv_id}"

        prov_id = await _make_provenance(
            db_session,
            run_id=run_id,
            source_table="message_versions",
            source_pk=str(mv_id),
            source_message_version_id=mv_id,
            graph_node_key=node_key,
        )

        # Insert pending purge row (purged_at IS NULL = still pending).
        pending = GraphPurgePending(
            forget_event_id=_next_id(),
            source_table="message_versions",
            source_pk=str(mv_id),
            graph_node_key=node_key,
            graph_provenance_id=prov_id,
        )
        db_session.add(pending)
        await db_session.flush()

        # Seed the adapter with the node so traversal would normally return it.
        adapter = NetworkXAdapter()
        topic_label = node_key
        await adapter.merge_node(
            node_key=node_key,
            labels=["MemoryNode"],
            properties={"label": topic_label, "node_type": "Topic"},
        )

        # Call find_related_topics — must abstain due to pending purge.
        result = await find_related_topics(
            db_session,
            adapter,
            topic=topic_label,
            viewer_is_admin=True,
            max_hops=2,
            max_results=10,
        )
        assert result.abstained is True, (
            f"R7d: find_related_topics did not abstain during pending purge for "
            f"node_key={node_key}. Expected abstained=True, got abstained={result.abstained}"
        )
