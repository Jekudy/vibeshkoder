"""Reimport tombstone prevention service (T3-05, issue #97).

Provides a pre-write safety check for the import apply path (#103):
before inserting any message, the caller MUST call ``check_tombstone`` and
abort if a tombstone is found.

Privacy hardening (AUTHORIZED_SCOPE.md ``#offrecord`` doctrine, HANDOFF.md §3 risk R1):

  A tombstone in ANY status — including ``'failed'`` — BLOCKS the import.

  Rationale: ``status='failed'`` means the cascade encountered an error and
  may have left content partially or fully in place (i.e. NOT fully deleted).
  Re-importing that content on top of a failed cascade is *more* dangerous
  than a clean tombstone: it risks silently resurecting data the operator
  explicitly requested to be forgotten.

  Callers MUST NOT filter by status when deciding whether to block.
  The only correct check is: "does a row exist?" → "yes → block".

Usage by #103 apply path (do NOT wire until #103 is implemented)::

    tombstone = await check_tombstone(
        session,
        chat_id=message.chat_id,
        message_id=message.message_id,
        content_hash=computed_hash,   # may be None if unavailable
        user_tg_id=sender_user_id,    # may be None for service messages
    )
    if tombstone is not None:
        stats_json = record_tombstone_skip(
            stats_json,
            matched_key=tombstone.tombstone_key,
            matched_status=tombstone.status,
            forget_event_id=tombstone.id,
            export_message_id=message.id,
            chat_id=message.chat_id,
        )
        continue  # skip this message

Cross-references:
  - ``bot/db/repos/forget_event.py`` — ``ForgetEventRepo.get_by_tombstone_key`` (frozen API)
  - ``bot/services/content_hash.py`` — ``compute_content_hash`` (callers pre-compute the hash)
  - ``bot/db/repos/ingestion_run.py`` — ``IngestionRunRepo.update_status(stats_json=...)``
  - Issue #103 — import apply path (wires this service)
  - docs/memory-system/import-dry-run-parser.md — integration note
"""

from __future__ import annotations

import copy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ForgetEvent
from bot.db.repos.forget_event import ForgetEventRepo


async def check_tombstone(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    content_hash: str | None,
    user_tg_id: int | None,
) -> ForgetEvent | None:
    """Return the FIRST active tombstone matching any of three keys, in priority order.

    Priority (most-specific first):
      1. ``message:{chat_id}:{message_id}``    — exact message identifier
      2. ``message_hash:{content_hash}``        — cross-chat content dedup
                                                  (SKIPPED if ``content_hash`` is None)
      3. ``user:{user_tg_id}``                  — broadest: blocks all messages from user
                                                  (SKIPPED if ``user_tg_id`` is None)

    A tombstone is "active" if it EXISTS in ``forget_events`` at all.
    Status is NOT filtered — ``status='failed'`` still blocks (privacy hardening;
    see module docstring for the full rationale).

    Returns:
        The first matching ``ForgetEvent`` row, or ``None`` if no tombstone found.
    """
    # 1. Most specific: exact message location
    msg_key = f"message:{chat_id}:{message_id}"
    row = await ForgetEventRepo.get_by_tombstone_key(session, msg_key)
    if row is not None:
        return row

    # 2. Cross-chat content dedup (skip if caller doesn't have a hash yet)
    if content_hash is not None:
        hash_key = f"message_hash:{content_hash}"
        row = await ForgetEventRepo.get_by_tombstone_key(session, hash_key)
        if row is not None:
            return row

    # 3. Broadest: entire user's data wiped (skip if no sender — service messages)
    if user_tg_id is not None:
        user_key = f"user:{user_tg_id}"
        row = await ForgetEventRepo.get_by_tombstone_key(session, user_key)
        if row is not None:
            return row

    return None


def record_tombstone_skip(
    stats_json: dict | None,
    *,
    matched_key: str,
    matched_status: str,
    forget_event_id: int,
    export_message_id: int,
    chat_id: int,
) -> dict:
    """Append a tombstone-skip entry to ``stats_json['skipped_tombstones']``.

    Returns a NEW dict — does NOT mutate the input (immutable approach; easier
    to reason about in async context, simpler to unit-test for side effects).

    Caller is responsible for persisting the returned dict via::

        await IngestionRunRepo.update_status(session, run, status="running",
                                             stats_json=new_stats)

    Args:
        stats_json: Existing stats dict from the ingestion run, or ``None`` if
            the run hasn't accumulated any stats yet.
        matched_key: The tombstone key that triggered the block, e.g. ``"message:-100:42"``.
        matched_status: The ``status`` field of the matched ``ForgetEvent``.
        forget_event_id: PK of the matched ``ForgetEvent`` row (for audit).
        export_message_id: The ``id`` field from the TD export message (for audit).
        chat_id: The chat from which the message was being imported.

    Returns:
        A new dict with ``skipped_tombstones`` list extended by one entry.

    Entry shape::

        {
            "matched_key": str,       # e.g. "message:-100:42"
            "matched_status": str,    # e.g. "completed"
            "forget_event_id": int,
            "export_message_id": int,
            "chat_id": int,
        }
    """
    # Build a deep copy to guarantee no mutation of the caller's original dict.
    # deepcopy is intentional: stats_json may contain nested dicts from other counters
    # (e.g. message_kind_counts) and shallow copy would leave those shared.
    base: dict = copy.deepcopy(stats_json) if stats_json is not None else {}

    if "skipped_tombstones" not in base:
        base["skipped_tombstones"] = []

    base["skipped_tombstones"].append(
        {
            "matched_key": matched_key,
            "matched_status": matched_status,
            "forget_event_id": forget_event_id,
            "export_message_id": export_message_id,
            "chat_id": chat_id,
        }
    )

    return base


async def batch_check_tombstones_by_message_key(
    session: AsyncSession,
    *,
    chat_id: int,
    export_msg_ids: list[int],
) -> set[int]:
    """Return the set of export_msg_ids that have a ``message:{chat_id}:{msg_id}`` tombstone.

    Issues a SINGLE bulk SELECT against forget_events. No per-message N+1 queries.
    Read-only. Safe to call inside any transaction (including the synthetic dry_run).

    Only checks the ``message:{chat_id}:{msg_id}`` key format — the most specific key
    for an export message. For the dry-run tombstone report (#100), per-user and
    per-hash tombstones are intentionally NOT checked: the dry-run does not have access
    to message content hashes (no text stored, NO-content guarantee) or pre-resolved
    user ids (ghost-user creation is the apply path's job). The message-location key is
    sufficient to surface the most operator-relevant collision signal.

    Args:
        session: Active AsyncSession. Not committed by this function.
        chat_id: The chat scope to build tombstone keys for.
        export_msg_ids: List of integer message ids from the export.

    Returns:
        Set of export_msg_ids whose ``message:{chat_id}:{id}`` tombstone key exists
        in ``forget_events``. Empty set if no collisions or input is empty.
    """
    if not export_msg_ids:
        return set()

    # Build the set of tombstone keys to look up: ``message:{chat_id}:{msg_id}``
    key_to_msg_id: dict[str, int] = {
        f"message:{chat_id}:{mid}": mid for mid in export_msg_ids
    }
    candidate_keys = list(key_to_msg_id.keys())

    stmt = (
        select(ForgetEvent.tombstone_key)
        .where(ForgetEvent.tombstone_key.in_(candidate_keys))
    )
    result = await session.execute(stmt)
    matched_keys = {row[0] for row in result.all()}

    # Map matched tombstone_keys back to export_msg_ids
    return {key_to_msg_id[k] for k in matched_keys if k in key_to_msg_id}
