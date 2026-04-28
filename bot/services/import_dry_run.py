"""DB-aware dry-run stats for Telegram Desktop import (T2-02 / issue #99).

Extends T2-01 ``ImportDryRunReport`` with DB-backed statistics:
- ``db_duplicate_count`` / ``db_duplicate_export_msg_ids``: export message ids that
  already exist in ``chat_messages`` for the same chat (would be skipped on apply).
- ``db_broken_reply_count``: reply targets that cannot be resolved via the reply
  resolver against a synthetic dry_run ``IngestionRun``.

The original sync ``parse_export(path)`` is preserved unchanged. This module adds
``parse_export_with_db(path, session, chat_id)`` as an async companion that calls
``parse_export`` first, then enriches the report with DB queries.

Usage:
    from bot.services.import_dry_run import parse_export_with_db
    report = await parse_export_with_db(path, session, chat_id)

DO NOT call this from Stream Alpha or Stream Charlie territory.
DO NOT import bot.services.import_apply here — that is Stream Delta (#103).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ChatMessage
from bot.db.repos.ingestion_run import IngestionRunRepo
from bot.services.import_parser import ImportDryRunReport, parse_export
from bot.services.import_reply_resolver import resolve_reply_batch

logger = logging.getLogger(__name__)


async def parse_export_with_db(
    path: str | Path,
    session: AsyncSession,
    chat_id: int,
) -> ImportDryRunReport:
    """Parse a Telegram Desktop export and enrich with DB-backed duplicate / reply stats.

    Steps:
    1. Run the sync parser (same as parse_export) to get in-memory stats.
    2. Query chat_messages for export msg ids that already exist in this chat.
    3. Create a synthetic dry_run IngestionRun (no commit — session stays uncommitted).
    4. Run resolve_reply_batch over the export's reply targets to count broken chains.
       Intra-export reply targets (target present in this export) are excluded before
       DB resolution — apply will create them, so they are not broken (Fix 1).
       db_broken_reply_count counts MESSAGES with broken replies, not unique target ids:
       two messages replying to the same missing target = 2 (option A semantics, Fix 2).
    5. Return an enriched ImportDryRunReport with db_* fields set.

    NEVER commits the session. The synthetic IngestionRun row is flushed but the
    caller owns the transaction; the outer rollback in test fixtures discards it cleanly.

    Args:
        path: Path to the Telegram Desktop single-chat export JSON.
        session: Active AsyncSession. Must NOT be committed by this function.
        chat_id: The chat id to scope DB queries against.

    Raises:
        FileNotFoundError: if path doesn't exist.
        ValueError: if the JSON is unparseable or has an unsupported envelope shape.
    """
    path = Path(path)

    # Step 1: run sync parser
    base_report = parse_export(path)

    # Step 2: DB duplicate detection
    # Collect all export message ids (int) from the parsed export.
    all_export_ids = _extract_export_msg_ids(path)
    db_duplicate_export_msg_ids = await _find_db_duplicates(session, chat_id, all_export_ids)
    db_duplicate_count = len(db_duplicate_export_msg_ids)

    # Step 3: Create synthetic dry_run ingestion_run for reply resolver scope.
    # We must flush so the row has an id, but we do NOT commit.
    dry_run_row = await IngestionRunRepo.create(
        session,
        run_type="dry_run",
        source_name=str(path),
    )

    # Step 4: Resolve reply targets against DB.
    # all_reply_targets: list of reply_to_message_id values from the export's user messages
    # (may contain duplicates when multiple messages reply to the same target).
    all_reply_targets = _extract_reply_targets(path)
    db_broken_reply_count = 0
    if all_reply_targets:
        # Fix 1: Exclude reply targets that are present in the export itself.
        # When apply runs, those messages will be created — so the chain is NOT broken.
        # Only targets that are absent from both the export AND the DB are truly missing.
        # Reuse all_export_ids from step 2 (already computed, avoids a 3rd file read).
        export_msg_ids_set = set(all_export_ids)
        db_reply_targets = [t for t in all_reply_targets if t not in export_msg_ids_set]

        if db_reply_targets:
            # Resolve only the deduplicated set of truly-external targets.
            db_reply_targets_unique = list(dict.fromkeys(db_reply_targets))
            resolutions = await resolve_reply_batch(
                session,
                export_msg_ids=db_reply_targets_unique,
                ingestion_run_id=dry_run_row.id,
                chat_id=chat_id,
            )
            # Fix 2: Count MESSAGES (not unique target ids) whose reply target is unresolved.
            # Two messages replying to the same missing target = 2 broken replies (option A
            # semantics per issue #99 spec: "J broken reply chains" means messages, not targets).
            # ``resolutions`` is keyed by target_id (deduplicated); we iterate over the full
            # per-message list to count each message individually.
            db_broken_reply_count = sum(
                1 for target_id in db_reply_targets
                if resolutions.get(target_id) is not None
                and resolutions[target_id].resolved_via == "unresolved"
            )

    # Step 5: Build enriched report (frozen dataclass — must construct fresh).
    # We rebuild from base_report fields plus the new db_* values.
    return ImportDryRunReport(
        source_file=base_report.source_file,
        chat_id=base_report.chat_id,
        chat_name=base_report.chat_name,
        chat_type=base_report.chat_type,
        total_messages=base_report.total_messages,
        user_messages=base_report.user_messages,
        service_messages=base_report.service_messages,
        media_count=base_report.media_count,
        distinct_users=base_report.distinct_users,
        distinct_export_user_ids=base_report.distinct_export_user_ids,
        date_range_start=base_report.date_range_start,
        date_range_end=base_report.date_range_end,
        reply_count=base_report.reply_count,
        dangling_reply_count=base_report.dangling_reply_count,
        duplicate_export_msg_ids=base_report.duplicate_export_msg_ids,
        edited_message_count=base_report.edited_message_count,
        forward_count=base_report.forward_count,
        anonymous_channel_message_count=base_report.anonymous_channel_message_count,
        message_kind_counts=base_report.message_kind_counts,
        policy_marker_counts=base_report.policy_marker_counts,
        parse_warnings=base_report.parse_warnings,
        db_duplicate_count=db_duplicate_count,
        db_duplicate_export_msg_ids=db_duplicate_export_msg_ids,
        db_broken_reply_count=db_broken_reply_count,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_export_msg_ids(path: Path) -> list[int]:
    """Return the list of integer message ids from the export JSON.

    Re-parses the file (second pass). Tolerant: skips non-dict or id-less entries.
    This mirrors the parsing logic in parse_export() but is kept separate to avoid
    coupling the two parsers together.
    """
    import json

    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    result: list[int] = []
    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        msg_id = msg.get("id")
        if isinstance(msg_id, int):
            result.append(msg_id)
    return result


def _extract_reply_targets(path: Path) -> list[int]:
    """Return the list of reply_to_message_id values from user messages in the export.

    Re-parses the file. Tolerant: skips service messages, non-int reply targets.
    """
    import json

    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    result: list[int] = []
    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "service":
            continue
        reply_to = msg.get("reply_to_message_id")
        if isinstance(reply_to, int):
            result.append(reply_to)
    return result


async def _find_db_duplicates(
    session: AsyncSession,
    chat_id: int,
    export_msg_ids: list[int],
) -> list[int]:
    """Return sorted list of export_msg_ids that already exist in chat_messages for this chat.

    Issues a single bulk SELECT. Returns an empty list when export_msg_ids is empty.
    """
    if not export_msg_ids:
        return []

    stmt = (
        select(ChatMessage.message_id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id.in_(export_msg_ids),
        )
    )
    result = await session.execute(stmt)
    found = sorted(row[0] for row in result.all())
    return found
