"""Admin Telegram handlers — graph projection, stats, query, purge (T10-07).

PHASE10_PLAN.md §5.G: /graph_project_now, /graph_stats, /graph_query, /graph_purge_now.

All handlers:
- Admin-only: checks message.from_user.id in settings.ADMIN_IDS
  (uses _is_admin from admin_cards.py canonical pattern — §5.G verbatim).
- Silent no-op for non-admins (no content leak).
- Run in private chat only via PrivateChatFilter.
- Log structured events for audit.
"""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.repos.graph_projection_run import list_recent_runs
from bot.db.repos.graph_purge_pending import count_active
from bot.filters.chat_type import PrivateChatFilter
from bot.handlers.admin_cards import _is_admin
from bot.services.graph_common import RefusalError
from bot.services.graph_projector import (
    ServiceDisabledError,
    default_projector_config,
    dry_run,
    project_full_rebuild,
    project_incremental,
    project_repair_source,
)
from bot.services.graph_query import (
    GraphQueryDisabledError,
    explain_connection,
    find_related_topics,
    graph_stats,
)
from bot.services.graph_purge_worker import graph_purge_worker_tick

logger = logging.getLogger(__name__)

router = Router(name="admin_graph")

_MESSAGE_MAX_LEN = 4000


def _trunc(text_: str, max_len: int = _MESSAGE_MAX_LEN) -> str:
    """Truncate long text with a notice."""
    if len(text_) <= max_len:
        return text_
    return text_[:max_len] + "\n… (truncated)"


# ─── /graph_project_now ─────────────────────────────────────────────────────


@router.message(Command("graph_project_now"), PrivateChatFilter())
async def cmd_graph_project_now(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Run graph projection in the given mode (dry_run / incremental / full_rebuild / repair).

    Default mode: dry_run (safe first). Admin must explicitly pass incremental or
    full_rebuild to write to Neo4j.
    Gate: memory.graph.projection.enabled must be ON for non-dry-run modes.
    """
    if not _is_admin(message):
        return

    raw_args = (command.args or "").strip()
    tokens = raw_args.split() if raw_args else []
    mode = tokens[0] if tokens else "dry_run"

    admin_id = message.from_user.id  # type: ignore[union-attr]
    logger.info(
        "graph_project_now",
        extra={
            "event": "graph_project_now",
            "mode": mode,
            "admin_user_id": admin_id,
        },
    )

    if mode == "dry_run":
        # dry_run is always allowed — no flag gate required
        try:
            from bot.services.graph_adapter import NetworkXAdapter

            config = default_projector_config(NetworkXAdapter())
            result = await dry_run(session, config=config)
        except ServiceDisabledError as exc:
            await message.answer(
                f"❌ Graph projection disabled: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            logger.exception("graph_project_now dry_run failed")
            await message.answer(
                f"❌ dry_run failed: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        lines = [
            "🔍 <b>Graph projection dry_run complete</b>",
            f"run_id: <code>{result.run_id}</code>",
            f"status: <code>{result.status}</code>",
            f"sources_total: {result.sources_total}",
            f"sources_processed: {result.sources_processed}",
            f"cost_usd: {result.cost_usd}",
        ]
        if result.errors_list:
            lines.append(f"errors: {len(result.errors_list)}")
        await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")

    elif mode == "incremental":
        try:
            from bot.services.graph_adapter import Neo4jAdapter

            config = default_projector_config(Neo4jAdapter())
            result = await project_incremental(
                session,
                config=config,
                started_by=f"admin:{admin_id}",
            )
        except ServiceDisabledError as exc:
            await message.answer(
                f"❌ Graph projection flag disabled: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            logger.exception("graph_project_now incremental failed")
            await message.answer(
                f"❌ incremental failed: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        lines = [
            "✅ <b>Graph incremental projection complete</b>",
            f"run_id: <code>{result.run_id}</code>",
            f"status: <code>{result.status}</code>",
            f"sources_processed: {result.sources_processed} / {result.sources_total}",
            f"triples_created: {result.triples_created}",
            f"nodes_merged: {result.nodes_merged}  edges_merged: {result.edges_merged}",
            f"cost_usd: {result.cost_usd}",
        ]
        if result.errors_list:
            lines.append(f"errors: {len(result.errors_list)} — first: {result.errors_list[0][:200]}")
        await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")

    elif mode == "full_rebuild":
        # Require explicit --confirm token to proceed — full_rebuild truncates Neo4j.
        args_list = (command.args or "").split()
        if "--confirm" not in args_list:
            await message.answer(
                "⚠️ full_rebuild is destructive (truncates Neo4j; replays from Postgres).\n"
                "Re-run with --confirm to proceed:\n"
                "<code>/graph_project_now full_rebuild --confirm</code>",
                parse_mode="HTML",
            )
            return
        try:
            from bot.services.graph_adapter import Neo4jAdapter

            config = default_projector_config(Neo4jAdapter())
            result = await project_full_rebuild(
                session,
                config=config,
                started_by=f"admin:{admin_id}",
            )
        except ServiceDisabledError as exc:
            await message.answer(
                f"❌ Graph projection flag disabled: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return
        except RefusalError:
            # RefusalError is raised when pg_try_advisory_xact_lock is held by a concurrent
            # rebuild. Generic RuntimeError is not used here — RefusalError is the specific
            # lock-contention signal from graph_projector.project_full_rebuild.
            await message.answer(
                "Another graph rebuild is currently in progress. Retry in a few minutes.",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            logger.exception("graph_project_now full_rebuild failed")
            await message.answer(
                f"❌ full_rebuild failed: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        lines = [
            "✅ <b>Graph full_rebuild complete</b>",
            f"run_id: <code>{result.run_id}</code>",
            f"status: <code>{result.status}</code>",
            f"nodes_merged: {result.nodes_merged}  edges_merged: {result.edges_merged}",
            f"cost_usd: {result.cost_usd}",
        ]
        await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")

    elif mode == "repair":
        # repair requires: repair <source_table> <source_pk>
        if len(tokens) < 3:
            await message.answer(
                "Usage: <code>/graph_project_now repair &lt;source_table&gt; &lt;source_pk&gt;</code>",
                parse_mode="HTML",
            )
            return
        source_table = tokens[1]
        source_pk = tokens[2]
        try:
            from bot.services.graph_adapter import Neo4jAdapter

            config = default_projector_config(Neo4jAdapter())
            result = await project_repair_source(
                session,
                source_table=source_table,
                source_pk=source_pk,
                config=config,
                started_by=f"admin:{admin_id}",
            )
        except ServiceDisabledError as exc:
            await message.answer(
                f"❌ Graph projection flag disabled: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            logger.exception("graph_project_now repair failed")
            await message.answer(
                f"❌ repair failed: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        lines = [
            "✅ <b>Graph repair complete</b>",
            f"run_id: <code>{result.run_id}</code>",
            f"status: <code>{result.status}</code>",
            f"source_table: {html.escape(source_table)}  pk: {html.escape(source_pk)}",
            f"triples_created: {result.triples_created}",
            f"cost_usd: {result.cost_usd}",
        ]
        await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")

    else:
        await message.answer(
            f"❌ Unknown mode: <code>{html.escape(mode)}</code>\n"
            "Valid modes: <code>dry_run</code> | <code>incremental</code> | "
            "<code>full_rebuild</code> | <code>repair &lt;table&gt; &lt;pk&gt;</code>",
            parse_mode="HTML",
        )


# ─── /graph_stats ───────────────────────────────────────────────────────────


@router.message(Command("graph_stats"), PrivateChatFilter())
async def cmd_graph_stats(
    message: Message,
    session: AsyncSession,
) -> None:
    """Admin-only graph statistics from the Postgres canonical store.

    Reports: active provenance rows, active edge rows, purged provenance rows.
    No feature flag gate — always available to admin for diagnostics.
    """
    if not _is_admin(message):
        return

    logger.info(
        "graph_stats",
        extra={
            "event": "graph_stats",
            "admin_user_id": message.from_user.id,  # type: ignore[union-attr]
        },
    )

    try:
        from bot.services.graph_adapter import Neo4jAdapter

        adapter = Neo4jAdapter()
        stats = await graph_stats(session, adapter)
        recent_runs = await list_recent_runs(session, limit=1)
        purge_counts = await count_active(session)
    except Exception as exc:
        logger.exception("graph_stats failed")
        await message.answer(
            f"❌ graph_stats failed: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    lines = [
        "📊 <b>Graph stats</b>",
        f"active_provenance_rows: {stats.active_provenance_rows}",
        f"active_edge_rows: {stats.active_edge_rows}",
        f"purged_provenance_rows: {stats.purged_provenance_rows}",
    ]

    # Last projection run info
    if recent_runs:
        last = recent_runs[0]
        started_at = last.started_at.isoformat() if last.started_at else "—"
        lines.append(
            f"last_run: id={last.id} mode={last.mode} status={last.status} started_at={started_at}"
        )
    else:
        lines.append("last_run: none")

    # Pending purge and DLQ
    lines.append(f"purge_pending: {purge_counts.get('pending', 0)}")
    lines.append(f"purge_dlq: {purge_counts.get('failed_dlq', 0)}")

    await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")


# ─── /graph_query ───────────────────────────────────────────────────────────


@router.message(Command("graph_query"), PrivateChatFilter())
async def cmd_graph_query(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Admin-only graph query.

    Usage:
      /graph_query <topic>           → find_related_topics
      /graph_query path <a> <b>      → explain_connection

    Gate: memory.graph.query.enabled must be ON.
    Returns: concise path list with node labels and source reference counts.
    """
    if not _is_admin(message):
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Usage:\n"
            "  <code>/graph_query &lt;topic&gt;</code>\n"
            "  <code>/graph_query path &lt;node_a&gt; &lt;node_b&gt;</code>",
            parse_mode="HTML",
        )
        return

    tokens = raw_args.split()
    admin_id = message.from_user.id  # type: ignore[union-attr]

    try:
        from bot.services.graph_adapter import Neo4jAdapter

        adapter = Neo4jAdapter()
    except Exception as exc:
        logger.exception("graph_query: adapter init failed")
        await message.answer(
            f"❌ adapter init failed: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    if tokens[0] == "path":
        if len(tokens) < 3:
            await message.answer(
                "Usage: <code>/graph_query path &lt;node_a&gt; &lt;node_b&gt;</code>",
                parse_mode="HTML",
            )
            return
        node_a = tokens[1]
        node_b = tokens[2]
        logger.info(
            "graph_query path",
            extra={
                "event": "graph_query_path",
                "node_a": node_a,
                "node_b": node_b,
                "admin_user_id": admin_id,
            },
        )
        try:
            result = await explain_connection(
                session,
                adapter,
                node_a=node_a,
                node_b=node_b,
                viewer_is_admin=True,
            )
        except GraphQueryDisabledError:
            await message.answer(
                "Graph query is disabled. Enable <code>memory.graph.query.enabled</code> to proceed.",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            logger.exception("graph_query explain_connection failed")
            await message.answer(
                f"❌ explain_connection failed: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        if result.abstained:
            await message.answer(
                f"⚠️ Abstained: <code>{html.escape(result.abstain_reason or 'unknown')}</code>",
                parse_mode="HTML",
            )
            return
        if not result.paths:
            await message.answer(
                f"No graph paths found between <code>{html.escape(node_a)}</code> "
                f"and <code>{html.escape(node_b)}</code>. Abstaining.",
                parse_mode="HTML",
            )
            return

        lines = [
            f"🔗 <b>Paths: {html.escape(node_a)} → {html.escape(node_b)}</b>",
            f"paths_found: {len(result.paths)}",
        ]
        for i, path in enumerate(result.paths[:10], start=1):
            node_labels = " → ".join(
                html.escape(n.get("label", n.get("node_key", "?")))
                for n in path.nodes[:8]
            )
            edge_count = len(path.edges)
            prov_count = len(path.provenance_ids)
            lines.append(f"#{i}: {node_labels}  (edges={edge_count}, prov={prov_count})")
        await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")

    else:
        # find_related_topics
        topic = raw_args
        logger.info(
            "graph_query topic",
            extra={
                "event": "graph_query_topic",
                "topic": topic,
                "admin_user_id": admin_id,
            },
        )
        try:
            result = await find_related_topics(
                session,
                adapter,
                topic=topic,
                viewer_is_admin=True,
            )
        except GraphQueryDisabledError:
            await message.answer(
                "Graph query is disabled. Enable <code>memory.graph.query.enabled</code> to proceed.",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            logger.exception("graph_query find_related_topics failed")
            await message.answer(
                f"❌ find_related_topics failed: <code>{html.escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return

        if result.abstained:
            await message.answer(
                f"⚠️ Abstained: <code>{html.escape(result.abstain_reason or 'unknown')}</code>",
                parse_mode="HTML",
            )
            return
        if not result.paths:
            await message.answer(
                f"No governed graph paths found for '<code>{html.escape(topic)}</code>'. Abstaining.",
                parse_mode="HTML",
            )
            return

        lines = [
            f"🔍 <b>Related to: {html.escape(topic)}</b>",
            f"nodes_found: {len(result.paths)}",
        ]
        for i, path in enumerate(result.paths[:10], start=1):
            if path.nodes:
                n = path.nodes[0]
                label = html.escape(n.get("label", n.get("node_key", "?")))
                node_type = html.escape(n.get("node_type", ""))
                prov_count = len(path.provenance_ids)
                lines.append(f"#{i}: {label} ({node_type})  prov={prov_count}")
        await message.answer(_trunc("\n".join(lines)), parse_mode="HTML")


# ─── /graph_purge_now ───────────────────────────────────────────────────────


@router.message(Command("graph_purge_now"), PrivateChatFilter())
async def cmd_graph_purge_now(
    message: Message,
    session: AsyncSession,
) -> None:
    """Admin-only manual trigger of graph_purge_worker_tick.

    Calls graph_purge_worker_tick with batch_size=20 and returns counts.
    """
    if not _is_admin(message):
        return

    logger.info(
        "graph_purge_now",
        extra={
            "event": "graph_purge_now",
            "admin_user_id": message.from_user.id,  # type: ignore[union-attr]
        },
    )

    try:
        from bot.services.graph_adapter import Neo4jAdapter

        adapter = Neo4jAdapter()
        tick_result = await graph_purge_worker_tick(session, adapter=adapter, batch_size=20)
    except Exception as exc:
        logger.exception("graph_purge_now failed")
        await message.answer(
            f"❌ graph_purge_now failed: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    processed = tick_result.get("processed", 0)
    errors = tick_result.get("errors", 0)
    skipped_paused = tick_result.get("skipped_paused", False)

    if skipped_paused:
        await message.answer(
            "⚠️ Purge worker is PAUSED (<code>memory.graph.write_pending.paused</code> = ON).",
            parse_mode="HTML",
        )
        return

    lines = [
        "🗑️ <b>Graph purge tick complete</b>",
        f"processed: {processed}",
        f"errors: {errors}",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")
