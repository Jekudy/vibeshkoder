"""Reply resolver service for Telegram Desktop import (T2-NEW-C / issue #98).

Resolves a Telegram Desktop export ``reply_to_message_id`` (the integer id from the
source JSON) to the actual ``chat_messages.id`` in our DB.

Resolution priority order:
1. **same_run** — TelegramUpdate in the given ingestion_run + linked ChatMessage.
2. **prior_run** — TelegramUpdate from any prior import run for the same chat_id.
3. **live** — ChatMessage whose (chat_id, message_id) matches and whose raw_update_id
   points to a live TelegramUpdate (run_type='live').
4. **unresolved** — no match found.

Forward-chain semantics: a Telegram reply has exactly one direct target. The resolver
returns that target's chat_messages.id in a single lookup. Consumers that need to traverse
a chain of replies (i.e., resolve the resolved row's own reply target) must iterate
themselves. This avoids per-call recursion overhead and is sufficient for the #103 apply
path's per-message walk.

Batch API: ``resolve_reply_batch`` issues bulk queries instead of N individual queries,
avoiding N+1 for N export ids.

Consumers: #99 (dry-run stats) and #103 (import apply). Do NOT call this from Stream Alpha
or Stream Charlie territory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bot.db.models import ChatMessage, IngestionRun, TelegramUpdate

logger = logging.getLogger(__name__)

# update_type tag used by synthetic import rows (confirmed by tests/db/test_telegram_update_repo.py)
_IMPORT_UPDATE_TYPE = "import_message"


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplyResolution:
    """Resolution result for one export_msg_id."""

    export_msg_id: int
    chat_message_id: int | None
    resolved_via: Literal["same_run", "prior_run", "live", "unresolved"]
    chain_depth: int


@dataclass(frozen=True)
class ReplyResolverStats:
    """Aggregate counts over a batch of ReplyResolution results."""

    total: int
    resolved_same_run: int
    resolved_prior_run: int
    resolved_live: int
    unresolved: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_reply(
    session: AsyncSession,
    export_msg_id: int,
    ingestion_run_id: int,
    *,
    chat_id: int,
) -> ReplyResolution:
    """Resolve one export_msg_id to a chat_messages.id.

    Resolution order: same_run → prior_run → live → unresolved.

    ``chain_depth`` is always 0 for a direct resolution. The resolver performs a
    single lookup — it does not traverse ``reply_to_message_id`` hops on the resolved
    row. Consumers that need deeper chain traversal iterate explicitly.

    For dry-run consumers (#99 stats) that do not yet have a real ingestion_run,
    create a synthetic ``IngestionRun`` with ``run_type='dry_run'`` first. The resolver
    does not enforce run_type — it uses the provided ingestion_run_id only to scope
    same_run lookups. The dry_run row gives the resolver a stable scope without
    writing real import data.

    Args:
        session: Active AsyncSession (no commit issued here).
        export_msg_id: The integer message id from the Telegram Desktop export.
        ingestion_run_id: The current ingestion run's id (used for same-run priority).
        chat_id: Scopes the lookup to this chat only.

    Returns:
        ReplyResolution with chat_message_id=None when unresolved.
    """
    # 1. Same-run lookup
    cm_id = await _lookup_import(session, export_msg_id, chat_id, ingestion_run_id)
    if cm_id is not None:
        return ReplyResolution(
            export_msg_id=export_msg_id,
            chat_message_id=cm_id,
            resolved_via="same_run",
            chain_depth=0,
        )

    # 2. Prior-run lookup (all import runs for this chat, newest first)
    cm_id = await _lookup_prior_import(session, export_msg_id, chat_id, ingestion_run_id)
    if cm_id is not None:
        return ReplyResolution(
            export_msg_id=export_msg_id,
            chat_message_id=cm_id,
            resolved_via="prior_run",
            chain_depth=0,
        )

    # 3. Live match
    cm_id = await _lookup_live(session, export_msg_id, chat_id)
    if cm_id is not None:
        return ReplyResolution(
            export_msg_id=export_msg_id,
            chat_message_id=cm_id,
            resolved_via="live",
            chain_depth=0,
        )

    # 4. Unresolved
    return ReplyResolution(
        export_msg_id=export_msg_id,
        chat_message_id=None,
        resolved_via="unresolved",
        chain_depth=0,
    )


async def resolve_reply_batch(
    session: AsyncSession,
    export_msg_ids: list[int],
    ingestion_run_id: int,
    *,
    chat_id: int,
) -> dict[int, ReplyResolution]:
    """Resolve a list of export_msg_ids in bulk (avoids N+1).

    Issues at most 4 DB queries regardless of how many ids are in the list
    (1 same-run + 1 prior-run + 1 live-with-raw-update-id + 1 live-NULL-fallback).
    Constant bound regardless of N.

    Args:
        session: Active AsyncSession.
        export_msg_ids: List of export message ids to resolve.
        ingestion_run_id: Current ingestion run id.
        chat_id: Chat scope.

    Returns:
        Dict mapping export_msg_id → ReplyResolution for every input id.
    """
    if not export_msg_ids:
        return {}

    results: dict[int, ReplyResolution] = {}
    remaining = list(export_msg_ids)

    # --- Pass 1: same-run bulk lookup ---
    same_run_map = await _bulk_lookup_import(session, remaining, chat_id, ingestion_run_id)
    still_unresolved: list[int] = []
    for eid in remaining:
        if eid in same_run_map:
            results[eid] = ReplyResolution(
                export_msg_id=eid,
                chat_message_id=same_run_map[eid],
                resolved_via="same_run",
                chain_depth=0,
            )
        else:
            still_unresolved.append(eid)

    if not still_unresolved:
        return results
    remaining = still_unresolved

    # --- Pass 2: prior-run bulk lookup ---
    prior_run_map = await _bulk_lookup_prior_import(session, remaining, chat_id, ingestion_run_id)
    still_unresolved = []
    for eid in remaining:
        if eid in prior_run_map:
            results[eid] = ReplyResolution(
                export_msg_id=eid,
                chat_message_id=prior_run_map[eid],
                resolved_via="prior_run",
                chain_depth=0,
            )
        else:
            still_unresolved.append(eid)

    if not still_unresolved:
        return results
    remaining = still_unresolved

    # --- Pass 3: live bulk lookup ---
    live_map = await _bulk_lookup_live(session, remaining, chat_id)
    for eid in remaining:
        if eid in live_map:
            results[eid] = ReplyResolution(
                export_msg_id=eid,
                chat_message_id=live_map[eid],
                resolved_via="live",
                chain_depth=0,
            )
        else:
            results[eid] = ReplyResolution(
                export_msg_id=eid,
                chat_message_id=None,
                resolved_via="unresolved",
                chain_depth=0,
            )

    return results


def aggregate_resolutions(resolutions: dict[int, ReplyResolution]) -> ReplyResolverStats:
    """Aggregate a batch of ReplyResolution results into summary counts.

    Args:
        resolutions: Dict as returned by resolve_reply_batch.

    Returns:
        ReplyResolverStats with per-category counts.
    """
    total = len(resolutions)
    same_run = 0
    prior_run = 0
    live = 0
    unresolved = 0

    for r in resolutions.values():
        if r.resolved_via == "same_run":
            same_run += 1
        elif r.resolved_via == "prior_run":
            prior_run += 1
        elif r.resolved_via == "live":
            live += 1
        elif r.resolved_via == "unresolved":
            unresolved += 1
        else:
            raise ValueError(f"Unknown resolved_via: {r.resolved_via!r}")

    return ReplyResolverStats(
        total=total,
        resolved_same_run=same_run,
        resolved_prior_run=prior_run,
        resolved_live=live,
        unresolved=unresolved,
    )


# ---------------------------------------------------------------------------
# Internal helpers — single-item lookups
# ---------------------------------------------------------------------------


async def _lookup_import(
    session: AsyncSession,
    export_msg_id: int,
    chat_id: int,
    ingestion_run_id: int,
) -> int | None:
    """Find chat_messages.id for an imported message in the given ingestion run.

    Joins telegram_updates (scoped to this run and chat) → chat_messages via raw_update_id.
    """
    stmt = (
        select(ChatMessage.id)
        .join(
            TelegramUpdate,
            ChatMessage.raw_update_id == TelegramUpdate.id,
        )
        .where(
            ChatMessage.chat_id == chat_id,
            TelegramUpdate.chat_id == chat_id,
            TelegramUpdate.message_id == export_msg_id,
            TelegramUpdate.ingestion_run_id == ingestion_run_id,
            TelegramUpdate.update_type == _IMPORT_UPDATE_TYPE,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _lookup_prior_import(
    session: AsyncSession,
    export_msg_id: int,
    chat_id: int,
    current_ingestion_run_id: int,
) -> int | None:
    """Find chat_messages.id from any prior import run for this chat.

    "Prior" means strictly older than the current run (candidate.started_at <
    current_run.started_at). This prevents a newer run from being selected as
    "prior" when the resolver is invoked for a non-latest run.
    Tie-breaker: candidate.id DESC.
    """
    # Scalar subquery: started_at of the current run (used as upper bound)
    current_started_at_sq = (
        select(IngestionRun.started_at)
        .where(IngestionRun.id == current_ingestion_run_id)
        .scalar_subquery()
    )

    CandidateRun = aliased(IngestionRun)

    stmt = (
        select(ChatMessage.id)
        .join(
            TelegramUpdate,
            ChatMessage.raw_update_id == TelegramUpdate.id,
        )
        .join(
            CandidateRun,
            TelegramUpdate.ingestion_run_id == CandidateRun.id,
        )
        .where(
            TelegramUpdate.chat_id == chat_id,
            ChatMessage.chat_id == chat_id,
            TelegramUpdate.message_id == export_msg_id,
            TelegramUpdate.update_type == _IMPORT_UPDATE_TYPE,
            CandidateRun.run_type == "import",
            CandidateRun.started_at < current_started_at_sq,
        )
        .order_by(CandidateRun.started_at.desc(), CandidateRun.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _lookup_live(
    session: AsyncSession,
    export_msg_id: int,
    chat_id: int,
) -> int | None:
    """Find chat_messages.id by matching (chat_id, message_id) for a live-ingested row.

    A live row has raw_update_id pointing to a TelegramUpdate whose ingestion_run has
    run_type='live'. Also handles rows where raw_update_id is NULL (older gatekeeper rows)
    by matching on (chat_id, message_id) directly on chat_messages.
    """
    # Primary: join to telegram_updates and verify live run_type
    stmt = (
        select(ChatMessage.id)
        .join(
            TelegramUpdate,
            ChatMessage.raw_update_id == TelegramUpdate.id,
        )
        .join(
            IngestionRun,
            TelegramUpdate.ingestion_run_id == IngestionRun.id,
        )
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == export_msg_id,
            IngestionRun.run_type == "live",
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    cm_id = result.scalar_one_or_none()
    if cm_id is not None:
        return cm_id

    # Fallback: live messages written by old gatekeeper code (raw_update_id IS NULL)
    # or rows whose raw_update_id points to an update without an ingestion_run.
    stmt_fallback = (
        select(ChatMessage.id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == export_msg_id,
            ChatMessage.raw_update_id.is_(None),
        )
        .limit(1)
    )
    result = await session.execute(stmt_fallback)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Internal helpers — bulk lookups (for resolve_reply_batch)
# ---------------------------------------------------------------------------


async def _bulk_lookup_import(
    session: AsyncSession,
    export_msg_ids: list[int],
    chat_id: int,
    ingestion_run_id: int,
) -> dict[int, int]:
    """Bulk same-run lookup. Returns {export_msg_id: chat_messages.id}."""
    if not export_msg_ids:
        return {}
    stmt = (
        select(TelegramUpdate.message_id, ChatMessage.id)
        .join(
            ChatMessage,
            ChatMessage.raw_update_id == TelegramUpdate.id,
        )
        .where(
            ChatMessage.chat_id == chat_id,
            TelegramUpdate.chat_id == chat_id,
            TelegramUpdate.message_id.in_(export_msg_ids),
            TelegramUpdate.ingestion_run_id == ingestion_run_id,
            TelegramUpdate.update_type == _IMPORT_UPDATE_TYPE,
        )
    )
    result = await session.execute(stmt)
    return {row[0]: row[1] for row in result.all()}


async def _bulk_lookup_prior_import(
    session: AsyncSession,
    export_msg_ids: list[int],
    chat_id: int,
    current_ingestion_run_id: int,
) -> dict[int, int]:
    """Bulk prior-run lookup. Returns {export_msg_id: chat_messages.id}.

    When multiple prior runs contain the same export_msg_id, the most recent run wins.
    We use a subquery approach: select all candidates and pick one per message_id.
    """
    if not export_msg_ids:
        return {}

    # Fetch all candidates strictly older than the current run; pick best per message_id in Python.
    # "Strictly older" = candidate.started_at < current_run.started_at (Fix 2: prevents a newer
    # run from being selected as "prior" when this resolver is invoked for a non-latest run).
    current_started_at_sq = (
        select(IngestionRun.started_at)
        .where(IngestionRun.id == current_ingestion_run_id)
        .scalar_subquery()
    )
    CandidateRun = aliased(IngestionRun)

    stmt = (
        select(TelegramUpdate.message_id, ChatMessage.id, CandidateRun.started_at)
        .join(
            ChatMessage,
            ChatMessage.raw_update_id == TelegramUpdate.id,
        )
        .join(
            CandidateRun,
            TelegramUpdate.ingestion_run_id == CandidateRun.id,
        )
        .where(
            ChatMessage.chat_id == chat_id,
            TelegramUpdate.chat_id == chat_id,
            TelegramUpdate.message_id.in_(export_msg_ids),
            TelegramUpdate.update_type == _IMPORT_UPDATE_TYPE,
            CandidateRun.run_type == "import",
            CandidateRun.started_at < current_started_at_sq,
        )
        .order_by(CandidateRun.started_at.desc(), CandidateRun.id.desc())
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Pick the most recent run's row per export_msg_id (rows are DESC by started_at)
    best: dict[int, int] = {}
    for msg_id, cm_id, _started_at in rows:
        if msg_id not in best:
            best[msg_id] = cm_id
    return best


async def _bulk_lookup_live(
    session: AsyncSession,
    export_msg_ids: list[int],
    chat_id: int,
) -> dict[int, int]:
    """Bulk live match lookup. Returns {export_msg_id: chat_messages.id}.

    Matches on (chat_id, message_id) for live ingestion rows. Tries two passes:
    1. Rows with raw_update_id pointing to a live ingestion_run.
    2. Rows with raw_update_id IS NULL (legacy gatekeeper rows).
    """
    if not export_msg_ids:
        return {}

    found: dict[int, int] = {}

    # Pass 1: raw_update_id → live run
    stmt = (
        select(ChatMessage.message_id, ChatMessage.id)
        .join(
            TelegramUpdate,
            ChatMessage.raw_update_id == TelegramUpdate.id,
        )
        .join(
            IngestionRun,
            TelegramUpdate.ingestion_run_id == IngestionRun.id,
        )
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id.in_(export_msg_ids),
            IngestionRun.run_type == "live",
        )
    )
    result = await session.execute(stmt)
    for msg_id, cm_id in result.all():
        found[msg_id] = cm_id

    remaining = [eid for eid in export_msg_ids if eid not in found]
    if not remaining:
        return found

    # Pass 2: legacy rows (raw_update_id IS NULL)
    stmt2 = (
        select(ChatMessage.message_id, ChatMessage.id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id.in_(remaining),
            ChatMessage.raw_update_id.is_(None),
        )
    )
    result2 = await session.execute(stmt2)
    for msg_id, cm_id in result2.all():
        if msg_id not in found:
            found[msg_id] = cm_id

    return found
