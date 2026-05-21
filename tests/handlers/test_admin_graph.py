"""T10-07 admin graph handler tests.

PHASE10_PLAN.md §5.G: /graph_project_now, /graph_stats, /graph_query, /graph_purge_now.

All handlers are admin-only (settings.ADMIN_IDS). Non-admin invocations → silent no-op.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def _make_admin_message() -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 149820031
    msg.from_user.username = "admin_user"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.chat.id = 149820031
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _make_nonadmin_message() -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 9999
    msg.from_user.username = "regular"
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.chat.id = 9999
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _make_cmd(args: str | None = None) -> MagicMock:
    cmd = MagicMock()
    cmd.args = args
    return cmd


def _collect_replies(msg: MagicMock) -> str:
    parts: list[str] = []
    for call in msg.answer.await_args_list:
        if call.args:
            parts.append(str(call.args[0]))
    return "\n".join(parts)


# ─── /graph_project_now ─────────────────────────────────────────────────────


async def test_graph_project_now_dry_run_returns_estimate(db_session) -> None:
    """dry_run mode returns run_id + summary without requiring flag or advisory lock."""
    from bot.services.graph_projector import GraphProjectionRunResult
    from bot.handlers.admin_graph import cmd_graph_project_now

    stub_result = GraphProjectionRunResult(
        run_id=42,
        status="dry_run_complete",
        sources_total=10,
        sources_processed=10,
        sources_skipped_governance=0,
        sources_skipped_budget=0,
        sources_skipped_unknown=0,
        triples_created=0,
        nodes_merged=0,
        edges_merged=0,
        cost_usd=Decimal("0.00"),
        errors_list=[],
    )

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.dry_run",
        new=AsyncMock(return_value=stub_result),
    ):
        await cmd_graph_project_now(msg, _make_cmd(None), session=db_session)

    reply = _collect_replies(msg)
    assert "42" in reply
    assert msg.answer.await_count >= 1


async def test_graph_project_now_requires_admin(db_session) -> None:
    """Non-admin → silent no-op (no answer sent)."""
    from bot.handlers.admin_graph import cmd_graph_project_now

    msg = _make_nonadmin_message()
    await cmd_graph_project_now(msg, _make_cmd(None), session=db_session)

    assert msg.answer.await_count == 0


async def test_graph_project_now_requires_flag_enabled(db_session) -> None:
    """incremental mode with flag OFF → ServiceDisabledError is handled gracefully."""
    from bot.services.graph_projector import ServiceDisabledError
    from bot.handlers.admin_graph import cmd_graph_project_now

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.project_incremental",
        new=AsyncMock(side_effect=ServiceDisabledError("flag off")),
    ):
        await cmd_graph_project_now(msg, _make_cmd("incremental"), session=db_session)

    reply = _collect_replies(msg)
    assert msg.answer.await_count >= 1
    assert "disabled" in reply.lower() or "flag" in reply.lower()


# ─── /graph_stats ───────────────────────────────────────────────────────────


async def test_graph_stats_returns_counts(db_session) -> None:
    """Admin /graph_stats returns Postgres-canonical counts."""
    from bot.services.graph_query import GraphStatsResult
    from bot.handlers.admin_graph import cmd_graph_stats

    stub_stats = GraphStatsResult(
        active_provenance_rows=5,
        active_edge_rows=8,
        purged_provenance_rows=2,
    )

    msg = _make_admin_message()
    with (
        patch(
            "bot.handlers.admin_graph.graph_stats",
            new=AsyncMock(return_value=stub_stats),
        ),
        patch(
            "bot.handlers.admin_graph.list_recent_runs",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "bot.handlers.admin_graph.count_active",
            new=AsyncMock(return_value={"pending": 0, "failed_dlq": 0, "total": 0}),
        ),
    ):
        await cmd_graph_stats(msg, session=db_session)

    reply = _collect_replies(msg)
    assert "5" in reply
    assert "8" in reply
    assert msg.answer.await_count >= 1


async def test_graph_stats_requires_admin(db_session) -> None:
    """Non-admin /graph_stats → silent no-op."""
    from bot.handlers.admin_graph import cmd_graph_stats

    msg = _make_nonadmin_message()
    await cmd_graph_stats(msg, session=db_session)
    assert msg.answer.await_count == 0


async def test_graph_stats_includes_last_run_pending_dlq(db_session) -> None:
    """Admin /graph_stats reply includes last_run, purge_pending, and purge_dlq sections."""
    from unittest.mock import MagicMock
    from datetime import datetime, timezone
    from bot.services.graph_query import GraphStatsResult
    from bot.handlers.admin_graph import cmd_graph_stats

    stub_stats = GraphStatsResult(
        active_provenance_rows=3,
        active_edge_rows=7,
        purged_provenance_rows=1,
    )
    fake_run = MagicMock()
    fake_run.id = 99
    fake_run.mode = "incremental"
    fake_run.status = "completed"
    fake_run.started_at = datetime(2026, 5, 21, 3, 30, 0, tzinfo=timezone.utc)

    msg = _make_admin_message()
    with (
        patch(
            "bot.handlers.admin_graph.graph_stats",
            new=AsyncMock(return_value=stub_stats),
        ),
        patch(
            "bot.handlers.admin_graph.list_recent_runs",
            new=AsyncMock(return_value=[fake_run]),
        ),
        patch(
            "bot.handlers.admin_graph.count_active",
            new=AsyncMock(return_value={"pending": 4, "failed_dlq": 2, "total": 6}),
        ),
    ):
        await cmd_graph_stats(msg, session=db_session)

    reply = _collect_replies(msg)
    # last_run info
    assert "99" in reply
    assert "incremental" in reply
    assert "completed" in reply
    # purge stats
    assert "4" in reply   # pending
    assert "2" in reply   # dlq
    assert msg.answer.await_count >= 1


# ─── /graph_project_now full_rebuild --confirm ──────────────────────────────


async def test_full_rebuild_requires_confirm_token(db_session) -> None:
    """full_rebuild without --confirm → refusal reply, no dispatch."""
    from bot.handlers.admin_graph import cmd_graph_project_now

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.project_full_rebuild",
        new=AsyncMock(),
    ) as mock_rebuild:
        await cmd_graph_project_now(msg, _make_cmd("full_rebuild"), session=db_session)

    reply = _collect_replies(msg)
    assert "--confirm" in reply
    assert mock_rebuild.await_count == 0


async def test_full_rebuild_proceeds_with_confirm_token(db_session) -> None:
    """full_rebuild with --confirm → dispatches project_full_rebuild."""
    from decimal import Decimal
    from bot.services.graph_projector import GraphProjectionRunResult
    from bot.handlers.admin_graph import cmd_graph_project_now

    stub_result = GraphProjectionRunResult(
        run_id=7,
        status="completed",
        sources_total=50,
        sources_processed=50,
        sources_skipped_governance=0,
        sources_skipped_budget=0,
        sources_skipped_unknown=0,
        triples_created=0,
        nodes_merged=100,
        edges_merged=200,
        cost_usd=Decimal("0.00"),
        errors_list=[],
    )

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.project_full_rebuild",
        new=AsyncMock(return_value=stub_result),
    ):
        await cmd_graph_project_now(
            msg, _make_cmd("full_rebuild --confirm"), session=db_session
        )

    reply = _collect_replies(msg)
    assert "7" in reply  # run_id
    assert msg.answer.await_count >= 1


# ─── /graph_query ───────────────────────────────────────────────────────────


async def test_graph_query_returns_nodes(db_session) -> None:
    """Admin /graph_query <topic> returns related nodes summary."""
    from bot.services.graph_query import GraphQueryResult, GraphPath
    from bot.handlers.admin_graph import cmd_graph_query

    stub_path = GraphPath(
        nodes=[{"label": "Python", "node_type": "Topic", "node_key": "python"}],
        edges=[],
        provenance_ids=[1],
        source_message_version_ids=[100],
        source_card_ids=["abc"],
    )
    stub_result = GraphQueryResult(
        abstained=False,
        abstain_reason=None,
        paths=[stub_path],
        query_metadata={"mode": "find_related_topics"},
    )

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.find_related_topics",
        new=AsyncMock(return_value=stub_result),
    ):
        await cmd_graph_query(msg, _make_cmd("python"), session=db_session)

    reply = _collect_replies(msg)
    assert "Python" in reply or "python" in reply
    assert msg.answer.await_count >= 1


async def test_graph_query_path_returns_edges(db_session) -> None:
    """Admin /graph_query path <a> <b> calls explain_connection."""
    from bot.services.graph_query import GraphQueryResult, GraphPath
    from bot.handlers.admin_graph import cmd_graph_query

    stub_path = GraphPath(
        nodes=[
            {"label": "A", "node_type": "Topic", "node_key": "a"},
            {"label": "B", "node_type": "Topic", "node_key": "b"},
        ],
        edges=[{"source_key": "a", "target_key": "b", "predicate": "relates_to"}],
        provenance_ids=[1],
        source_message_version_ids=[200],
        source_card_ids=["def"],
    )
    stub_result = GraphQueryResult(
        abstained=False,
        abstain_reason=None,
        paths=[stub_path],
        query_metadata={"mode": "explain_connection"},
    )

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.explain_connection",
        new=AsyncMock(return_value=stub_result),
    ):
        await cmd_graph_query(msg, _make_cmd("path a b"), session=db_session)

    reply = _collect_replies(msg)
    assert msg.answer.await_count >= 1
    assert "a" in reply.lower() or "b" in reply.lower()


async def test_graph_query_requires_admin(db_session) -> None:
    """Non-admin /graph_query → silent no-op."""
    from bot.handlers.admin_graph import cmd_graph_query

    msg = _make_nonadmin_message()
    await cmd_graph_query(msg, _make_cmd("python"), session=db_session)
    assert msg.answer.await_count == 0


# ─── /graph_purge_now ───────────────────────────────────────────────────────


async def test_graph_purge_now_drives_worker(db_session) -> None:
    """Admin /graph_purge_now calls graph_purge_worker_tick and returns stats."""
    from bot.handlers.admin_graph import cmd_graph_purge_now

    stub_tick_result = {"processed": 5, "errors": 1, "skipped_paused": False}

    msg = _make_admin_message()
    with patch(
        "bot.handlers.admin_graph.graph_purge_worker_tick",
        new=AsyncMock(return_value=stub_tick_result),
    ):
        await cmd_graph_purge_now(msg, session=db_session)

    reply = _collect_replies(msg)
    assert "5" in reply
    assert msg.answer.await_count >= 1


async def test_graph_purge_now_requires_admin(db_session) -> None:
    """Non-admin /graph_purge_now → silent no-op."""
    from bot.handlers.admin_graph import cmd_graph_purge_now

    msg = _make_nonadmin_message()
    await cmd_graph_purge_now(msg, session=db_session)
    assert msg.answer.await_count == 0
