"""Telegram Desktop import apply (T2-03 / issue #103, Stream Delta finale).

Applies a Telegram Desktop single-chat export onto the live DB through the SAME
governance + normalization path live ingestion uses (ADR-0007). The apply path
synthesises ``telegram_updates`` rows (``update_id=NULL``, ``ingestion_run_id`` set)
per imported message and routes content through ``persist_message_with_policy``
(#89 helper). Direct writes to chat_messages bypassing the helper are forbidden.

Pipeline per export message (chronological order, in-chunk):

1. Resume gate     — skip if ``export_msg_id <= last_processed_export_msg_id``.
2. Tombstone gate  — chunk-level ``batch_check_tombstones_by_message_key`` (#97).
3. Duplicate gate  — ``chat_messages`` lookup by ``(chat_id, message_id)``.
4. User resolution — ``import_user_map.resolve_export_user`` (#93). Ghost users
                     are created with ``is_imported_only=True``.
5. Reply resolver  — ``import_reply_resolver`` priority order (same_run > prior_run
                     > live > unresolved) (#98). Read-only.
6. Synthetic raw   — Write a ``telegram_updates`` row with ``update_id=NULL`` and
                     ``ingestion_run_id`` set. Audit row stays even when governance
                     rejects the content.
7. Governance      — ``governance.detect_policy`` runs after the synthetic audit row.
                     ``offrecord`` keeps only that audit row and skips persistence.
8. Persist         — for non-offrecord outcomes, ``persist_message_with_policy`` (#89)
                     writes ``chat_messages``.
9. Edit history    — ``MessageVersionRepo.insert_version(imported_final=True)`` per
                     #106. Skipped when a live row already exists for the same
                     ``(chat_id, message_id)`` (live wins).
10. Checkpoint     — once per CHUNK, ``save_checkpoint`` deep-merges
                     ``last_processed_export_msg_id`` into ``stats_json``.

Cross-stream contract:
- Acquires a single ``AsyncConnection`` from the caller's session and holds it for
  the full apply run. ``acquire_advisory_lock`` (#102) wraps the loop. Per-chunk
  ``session.commit()`` releases each chunk's transaction without releasing the
  underlying connection.

Hard invariants (verified by tests in tests/services/test_import_apply.py):
- Idempotent: re-running on the same export produces zero net DB changes.
- Tombstone gate runs BEFORE every write — confirmed via mock spy.
- ``persist_message_with_policy`` is the SOLE writer to ``chat_messages``.
- Synthetic ``telegram_updates.update_id`` is always NULL; ``ingestion_run_id``
  always matches the apply run.
- ``message_versions.imported_final=TRUE`` for every imported version row.

Out of scope (next ticket — #104 logical rollback):
- Rolling back an apply run via ``ingestion_run_id``.
- The synthetic ``telegram_updates`` rows are deliberately tagged so #104 can
  cascade DELETE them along with the run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ChatMessage, IngestionRun
from bot.db.repos.message_version import MessageVersionRepo
from bot.db.repos.telegram_update import TelegramUpdateRepo
from bot.services.content_hash import compute_content_hash
from bot.services.governance import detect_policy
from bot.services.import_checkpoint import save_checkpoint
from bot.services.import_chunking import ChunkingConfig, acquire_advisory_lock
from bot.services.import_parser import _classify_td_kind, _extract_text_string
from bot.services.import_reply_resolver import resolve_reply_batch
from bot.services.import_tombstone import batch_check_tombstones_by_message_key
from bot.services.import_user_map import resolve_export_user
from bot.services.message_persistence import persist_message_with_policy

logger = logging.getLogger(__name__)

# Maximum number of export-msg-ids retained in error_export_msg_ids (mirrors the
# tombstone_skip_export_msg_ids cap in import_dry_run / parser). Bounded so the
# stats payload never grows linearly with bad messages.
_ERROR_ID_CAP = 1000

# update_type tag for synthetic import telegram_updates rows. Matches the constant
# used by the reply resolver (#98) — keep them in sync.
_IMPORT_UPDATE_TYPE = "import_message"


# ─── Public dataclasses ───────────────────────────────────────────────────────


@dataclass
class ImportApplyReport:
    """Outcome of one apply run.

    NO content fields. NO message bodies. Counts + ids only.
    """

    ingestion_run_id: int
    chat_id: int
    source_path: str
    started_at: datetime
    finished_at: datetime | None = None
    chunking_config: ChunkingConfig | None = None

    # Per-message outcome counters (mutually exclusive bookkeeping).
    applied_count: int = 0
    """Successful synthetic-update insertions that produced a chat_messages row."""

    skipped_duplicate_count: int = 0
    """Message already present in chat_messages for this chat (idempotency hit)."""

    skipped_tombstone_count: int = 0
    """Message blocked by forget_events tombstone."""

    skipped_governance_count: int = 0
    """detect_policy returned offrecord — synthetic audit row kept, content not persisted."""

    skipped_resume_count: int = 0
    """Message id <= last_processed_export_msg_id — already applied in prior run."""

    skipped_service_count: int = 0
    """Service messages (joins/leaves/title changes). Not applied per #94 contract."""

    skipped_overlap_count: int = 0
    """Live row exists for same (chat_id, message_id) — message_versions skipped per #106."""

    error_count: int = 0
    """Per-message exceptions caught at persist time (logged, not swallowed)."""

    error_export_msg_ids: list[int] = field(default_factory=list)
    """Capped list of export_msg_ids that errored. Bounded by _ERROR_ID_CAP."""

    last_processed_export_msg_id: int | None = None
    """Highest export_msg_id from the LAST FULLY-COMPLETED chunk. None if no chunk completed."""

    chunks_processed: int = 0
    """Number of chunks committed (partial chunks rolled back on error)."""


# ─── Public API ───────────────────────────────────────────────────────────────


async def run_apply(
    session: AsyncSession,
    *,
    ingestion_run_id: int,
    resume_point: int | None = None,
    chunking_config: ChunkingConfig,
    export_path: str | None = None,
) -> ImportApplyReport:
    """Run the import apply path for the ingestion_run identified by ``ingestion_run_id``.

    Args:
        session: Active AsyncSession. Will be committed per chunk. The session's bound
            connection is used for the advisory lock — caller MUST NOT cycle connections
            on this session (single-process CLI design satisfies this).
        ingestion_run_id: The PK of the IngestionRun row created by ``init_or_resume_run``.
        resume_point: ``last_processed_export_msg_id`` from a prior partial run. ``None``
            for a fresh run. Messages with ``export_msg_id <= resume_point`` are skipped.
        chunking_config: Chunking + advisory-lock configuration loaded from env / CLI flag.
        export_path: Override path to the export JSON. When ``None`` (production), the
            path is read from ``IngestionRun.source_name``. The override exists for tests
            that want to point at a fixture without touching the run row's source_name.

    Returns:
        ImportApplyReport with counts and the final ``last_processed_export_msg_id``.
    """
    report = ImportApplyReport(
        ingestion_run_id=ingestion_run_id,
        chat_id=0,  # populated below
        source_path="",  # populated below
        started_at=datetime.now(tz=timezone.utc),
        chunking_config=chunking_config,
    )

    # Resolve the run row to read chat_id and source_name. Use NOWAIT-free read to
    # tolerate a concurrent reader that's looking at the same row for finalize_run.
    run_row = await _load_run(session, ingestion_run_id)
    chat_id = _extract_chat_id_from_run(run_row)
    report.chat_id = chat_id

    source_path = export_path or run_row.source_name or ""
    if not source_path:
        raise RuntimeError(
            f"ingestion_run {ingestion_run_id}: source_name is empty and no export_path "
            "override provided — cannot locate export file"
        )
    path = Path(source_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"export file not found: {path}")
    report.source_path = str(path)

    # NOTE on lock semantics: pg_advisory_lock is connection-scoped. We pull the
    # session's bound connection and hold it for the lock lifetime. session.commit()
    # ends each per-chunk tx but does NOT release the underlying conn from the
    # session — the lock survives across chunk commits. See import-chunking.md §
    # "Connection-scope requirement".
    if chunking_config.use_advisory_lock:
        connection = await session.connection()
        async with acquire_advisory_lock(connection, ingestion_run_id):
            await _run_apply_loop(
                session,
                report=report,
                run_row=run_row,
                path=path,
                chat_id=chat_id,
                resume_point=resume_point,
                chunking_config=chunking_config,
            )
    else:
        await _run_apply_loop(
            session,
            report=report,
            run_row=run_row,
            path=path,
            chat_id=chat_id,
            resume_point=resume_point,
            chunking_config=chunking_config,
        )

    report.finished_at = datetime.now(tz=timezone.utc)
    return report


# ─── Apply loop ──────────────────────────────────────────────────────────────


async def _run_apply_loop(
    session: AsyncSession,
    *,
    report: ImportApplyReport,
    run_row: IngestionRun,
    path: Path,
    chat_id: int,
    resume_point: int | None,
    chunking_config: ChunkingConfig,
) -> None:
    messages = list(_iter_export_messages(path))
    # Sort by id ascending so chunk boundaries are stable and resume semantics
    # (last_processed_export_msg_id) hold.
    messages.sort(key=lambda m: m.get("id", 0))

    chunk_size = chunking_config.chunk_size
    sleep_seconds = chunking_config.sleep_between_chunks_ms / 1000.0

    # Pre-skip messages already processed in a prior partial run.
    if resume_point is not None:
        before = len(messages)
        messages = [m for m in messages if int(m.get("id", 0)) > resume_point]
        report.skipped_resume_count = before - len(messages)

    chunk_index = 0
    for chunk_start in range(0, len(messages), chunk_size):
        chunk = messages[chunk_start : chunk_start + chunk_size]

        # Tombstone gate — one bulk SELECT per chunk (#97). Chunks containing only
        # service messages still call this so the contract is uniform; it's a single
        # query so the cost is negligible.
        export_ids = [int(m["id"]) for m in chunk if isinstance(m.get("id"), int)]
        tombstone_hits = await batch_check_tombstones_by_message_key(
            session,
            chat_id=chat_id,
            export_msg_ids=export_ids,
        )

        # Process chunk inside a single per-chunk transaction. session.commit() at
        # the end commits the chunk; if an exception escapes, the caller (CLI)
        # rolls back the session and finalize_run records the failed status.
        last_id_in_chunk: int | None = None
        for msg in chunk:
            try:
                advance = await _apply_one_message(
                    session,
                    msg=msg,
                    chat_id=chat_id,
                    ingestion_run_id=report.ingestion_run_id,
                    tombstone_hits=tombstone_hits,
                    report=report,
                )
            except (ValueError, RuntimeError, SQLAlchemyError) as exc:
                # Per-message envelope: log + bump error counters; chunk continues.
                report.error_count += 1
                if len(report.error_export_msg_ids) < _ERROR_ID_CAP:
                    msg_id = msg.get("id")
                    if isinstance(msg_id, int):
                        report.error_export_msg_ids.append(msg_id)
                logger.error(
                    "import_apply: per-message error",
                    extra={
                        "ingestion_run_id": report.ingestion_run_id,
                        "chat_id": chat_id,
                        "export_msg_id": msg.get("id"),
                        "error_type": type(exc).__name__,
                    },
                )
                # Continue with next message — DO NOT advance last_id_in_chunk.
                advance = None

            if advance is not None:
                last_id_in_chunk = advance

        # Commit the chunk. Per-chunk commits make checkpoint state durable.
        await session.commit()
        report.chunks_processed += 1

        # Persist checkpoint AFTER commit so the chunk's writes are visible if
        # the caller crashes between commit and save_checkpoint (re-run will
        # re-apply, hit the duplicate gate, and continue).
        if last_id_in_chunk is not None:
            await save_checkpoint(
                session,
                ingestion_run_id=report.ingestion_run_id,
                last_processed_export_msg_id=last_id_in_chunk,
                chunk_index=chunk_index,
            )
            report.last_processed_export_msg_id = last_id_in_chunk

        chunk_index += 1

        # Yield CPU between chunks so live ingestion can interleave.
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)


async def _apply_one_message(
    session: AsyncSession,
    *,
    msg: dict[str, Any],
    chat_id: int,
    ingestion_run_id: int,
    tombstone_hits: set[int],
    report: ImportApplyReport,
) -> int | None:
    """Process one export message. Returns the export_msg_id once it has been ACKed
    (applied / duplicate / governance-skipped / tombstone-skipped / overlap-skipped /
    service); returns ``None`` if the message had no usable id (skipped silently).

    Service messages produce no chat_messages row per the parser's contract (#94).
    They are still ACKed for checkpoint progress so the CLI does not reprocess them
    on resume.
    """
    msg_id_raw = msg.get("id")
    if not isinstance(msg_id_raw, int):
        # Without an id we can't ACK or dedup; skip silently per parser tolerant-reader rule.
        return None
    msg_id = msg_id_raw

    # Service messages: per #94 parser contract they are NOT user-authored. We do
    # NOT call detect_policy on them and do NOT write a synthetic update for them
    # (their structure carries no governance content). Bump the counter and ACK so
    # the checkpoint advances past them.
    if msg.get("type") == "service":
        report.skipped_service_count += 1
        return msg_id

    # 1. Tombstone gate (#97) — message is in the chunk-level hit set.
    if msg_id in tombstone_hits:
        report.skipped_tombstone_count += 1
        return msg_id

    # 2. Duplicate gate — chat_messages already has this (chat_id, message_id).
    existing_cm_id = await _find_existing_chat_message_id(session, chat_id, msg_id)
    if existing_cm_id is not None:
        report.skipped_duplicate_count += 1
        return msg_id

    # 3. User resolution (#93). Service-message ducks would set from_id=None;
    # user messages either have a "user<N>" or "channel<N>" string.
    from_id = msg.get("from_id")
    display_name = msg.get("from") if isinstance(msg.get("from"), str) else None
    user_id = await resolve_export_user(
        session,
        from_id if isinstance(from_id, str) else None,
        display_name=display_name,
        create_ghost_if_missing=True,
    )
    if user_id is None:
        # No resolved user → cannot persist (chat_messages.user_id is NOT NULL).
        # This indicates a malformed user message; bump error and continue.
        report.error_count += 1
        if len(report.error_export_msg_ids) < _ERROR_ID_CAP:
            report.error_export_msg_ids.append(msg_id)
        logger.error(
            "import_apply: cannot resolve user for export_msg_id=%d (from_id=%r); skipping",
            msg_id,
            from_id,
        )
        return msg_id

    # 4. Reply resolution (#98). Read-only — no writes.
    reply_export_id_raw = msg.get("reply_to_message_id")
    reply_export_id: int | None = (
        reply_export_id_raw if isinstance(reply_export_id_raw, int) else None
    )
    resolved_reply_to_message_id: int | None = None
    if reply_export_id is not None:
        resolutions = await resolve_reply_batch(
            session,
            export_msg_ids=[reply_export_id],
            ingestion_run_id=ingestion_run_id,
            chat_id=chat_id,
        )
        resolution = resolutions.get(reply_export_id)
        if resolution is not None and resolution.chat_message_id is not None:
            # The resolver returns chat_messages.id — but persist expects a Telegram
            # message_id (i.e. the per-chat sequence). We still pass the raw export
            # reply id forward into the message duck (live behaviour: handler stores
            # the Telegram message_id of the parent). The resolved cm_id is logged
            # but not directly written; persist's normalization extracts
            # reply_to_message_id from the duck's reply_to_message.message_id field.
            resolved_reply_to_message_id = reply_export_id

    # 5. Build kind + text/caption per parser semantics. Mirrors what the dry-run
    # parser counts so apply-side governance verdicts match dry-run preview.
    kind = _classify_td_kind(msg, warnings=None)
    text_value, caption_value = _extract_text_caption_for_kind(msg, kind)

    # 6. Compute content hash (chv1) for the version row idempotency key.
    text_entities = msg.get("text_entities") if isinstance(msg.get("text_entities"), list) else None
    content_hash = compute_content_hash(
        text=text_value,
        caption=caption_value,
        message_kind=kind,
        entities=text_entities,
    )

    # 7. Synthetic raw row — written FIRST and ALWAYS (even for offrecord), tagged
    # with ingestion_run_id so #104 rollback can locate it. update_id MUST be NULL.
    raw_payload = _build_raw_payload(msg, chat_id=chat_id, msg_id=msg_id)
    raw_row = await TelegramUpdateRepo.insert(
        session,
        update_type=_IMPORT_UPDATE_TYPE,
        update_id=None,  # synthetic — partial unique index is on update_id IS NOT NULL
        raw_json=raw_payload,
        raw_hash=None,
        chat_id=chat_id,
        message_id=msg_id,
        ingestion_run_id=ingestion_run_id,
        is_redacted=False,
    )

    # 8. Governance gate. The synthetic row above is the audit trail. If the
    # imported content is offrecord, do NOT call persist_message_with_policy and
    # do NOT create chat_messages/message_versions rows.
    policy, _mark_payload = detect_policy(text_value, caption_value)
    if policy == "offrecord":
        raw_row.is_redacted = True
        raw_row.redaction_reason = "offrecord"
        await session.flush()
        report.skipped_governance_count += 1
        return msg_id

    # 9. Build the message duck and call persist_message_with_policy. The helper
    # remains the sole writer for non-offrecord chat_messages rows and re-runs
    # deterministic governance inside the write path, matching the live route.
    duck = _build_message_duck(
        msg=msg,
        chat_id=chat_id,
        msg_id=msg_id,
        user_id=user_id,
        text=text_value,
        caption=caption_value,
        reply_to_msg_id=resolved_reply_to_message_id,
        message_kind=kind,
    )
    persist_result = await persist_message_with_policy(
        session,
        duck,
        raw_update_id=raw_row.id,
        source="import",
    )

    # 10. message_versions row for live-or-import overlap rule (#106 §5).
    # If a live row already exists for the same (chat_id, message_id), we
    # skip the version insert: the live row is authoritative. The duplicate
    # gate above already covers this for the current ingestion run, but a row
    # could have been written between the dup-check and persist (live ingestion
    # racing with import). Re-check using raw_update_id of the parent row.
    persisted_cm = persist_result.chat_message
    if _is_live_chat_message(persisted_cm):
        # Live row pre-existed → skip imported version per overlap rule.
        report.skipped_overlap_count += 1
    else:
        # Determine edit_date from TD export (last-edit timestamp, if any).
        edit_dt = _parse_edited_at(msg)
        # Insert the imported_final version row. is_redacted mirrors the chat_messages
        # redaction state so downstream consumers see consistent flags.
        await MessageVersionRepo.insert_version(
            session,
            chat_message_id=persisted_cm.id,
            content_hash=content_hash,
            text=None if persist_result.policy == "offrecord" else text_value,
            caption=None if persist_result.policy == "offrecord" else caption_value,
            entities_json=None,
            edit_date=edit_dt,
            raw_update_id=raw_row.id,
            is_redacted=persist_result.policy == "offrecord",
            imported_final=True,
        )
        report.applied_count += 1

    return msg_id


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _iter_export_messages(path: Path) -> Iterable[dict[str, Any]]:
    """Yield message dicts from the export JSON.

    Reuses the parser's tolerant-reader contract: single-chat envelope, ``messages[]``
    list, malformed entries skipped silently. Full-account exports were already
    rejected at the dry-run gate; we re-validate the envelope shape defensively here.
    """
    import json

    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Export envelope is not a JSON object: {path}")
    if "chats" in data and isinstance(data.get("chats"), list):
        raise ValueError(
            f"Unsupported export type: full-account archive detected (top-level 'chats' "
            f"list). Single-chat exports only. File: {path}"
        )
    raw_messages = data.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ValueError(f"Expected 'messages' to be a list, got {type(raw_messages).__name__}")
    for entry in raw_messages:
        if isinstance(entry, dict) and isinstance(entry.get("id"), int):
            yield entry


def _extract_text_caption_for_kind(msg: dict, kind: str) -> tuple[str | None, str | None]:
    """Mirror import_parser._extract_text_content but return (text, caption) with None
    when the slot is empty (parser returns ('', '') — we want None for proper persist).
    """
    raw_text = msg.get("text", "")
    text_str = _extract_text_string(raw_text)
    if not text_str:
        entities = msg.get("text_entities")
        if entities is not None:
            text_str = _extract_text_string(entities)

    # Media-kind taxonomy from import_parser
    media_kinds = {
        "photo", "video", "voice", "audio", "document", "sticker",
        "animation", "video_note",
    }
    if kind in media_kinds:
        # TD text field is the caption for media messages
        return (None, text_str or None)
    return (text_str or None, None)


def _build_raw_payload(msg: dict, *, chat_id: int, msg_id: int) -> dict:
    """Build a minimal envelope to store in telegram_updates.raw_json.

    Strips potentially-content-bearing fields that the parser would normally pass
    through (text, text_entities, caption). The persisted row carries enough
    metadata for #104 rollback (ids, timestamps) but no governance-relevant content
    duplicated outside chat_messages. Content is owned by chat_messages /
    message_versions through persist_message_with_policy.

    The fully-original message dict is INTENTIONALLY not stored here. Per ADR-0003
    the source-of-truth content lives in the chat_messages / message_versions rows
    (which detect_policy filters); duplicating a copy in telegram_updates would
    create a parallel offrecord-bypass channel.
    """
    # Allowlist of safe metadata fields. Anything not in the allowlist is dropped.
    safe_keys = {
        "id", "type", "date", "date_unixtime", "edited", "edited_unixtime",
        "from_id", "reply_to_message_id", "media_type", "mime_type",
        "forwarded_from", "duration_seconds", "width", "height", "actor_id",
        "action",
    }
    minimal: dict[str, Any] = {k: v for k, v in msg.items() if k in safe_keys}
    minimal.setdefault("chat_id", chat_id)
    minimal.setdefault("message_id", msg_id)
    return minimal


def _build_message_duck(
    *,
    msg: dict,
    chat_id: int,
    msg_id: int,
    user_id: int,
    text: str | None,
    caption: str | None,
    reply_to_msg_id: int | None,
    message_kind: str,
) -> SimpleNamespace:
    """Construct a SimpleNamespace shaped like aiogram Message for
    persist_message_with_policy. Mirrors the importer-duck pattern used by
    tests/services/test_message_persistence.py::_make_duck_message.

    Critical: every attribute persist_message_with_policy reads must exist (None
    when absent). Missing attrs are surfaced by the helper as runtime errors.
    """
    # Parse the message's date. Prefer date_unixtime (UTC), fallback to date.
    msg_date = _parse_message_date(msg)

    # Reply target — persist normalisation reads message.reply_to_message.message_id.
    reply_to_message = (
        SimpleNamespace(message_id=reply_to_msg_id) if reply_to_msg_id is not None else None
    )

    # Set the kind-discriminator attribute so normalization.classify_message_kind
    # picks the correct kind. The classifier looks at attribute presence (non-None);
    # we set the matching attr to a truthy sentinel.
    kind_attrs: dict[str, Any] = {
        "photo": None, "video": None, "voice": None, "audio": None,
        "document": None, "sticker": None, "animation": None, "video_note": None,
        "location": None, "contact": None, "poll": None, "dice": None,
        "forward_origin": None, "new_chat_members": None, "left_chat_member": None,
        "pinned_message": None,
    }
    if message_kind in kind_attrs:
        # SimpleNamespace truthy sentinel — normalization probes for non-None.
        kind_attrs[message_kind] = SimpleNamespace(_imported=True)
    if message_kind == "forward":
        kind_attrs["forward_origin"] = SimpleNamespace(_imported=True)

    return SimpleNamespace(
        message_id=msg_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=None,
            first_name=msg.get("from") if isinstance(msg.get("from"), str) else "imported user",
            last_name=None,
        ),
        text=text,
        caption=caption,
        date=msg_date,
        # No model_dump — importer duck does NOT pretend to be aiogram. Persist
        # falls back to None for raw_json (see message_persistence step 4).
        reply_to_message=reply_to_message,
        message_thread_id=None,
        entities=None,
        caption_entities=None,
        **kind_attrs,
    )


def _parse_message_date(msg: dict) -> datetime:
    """Parse the export message's date. Always returns a tz-aware datetime."""
    unix = msg.get("date_unixtime")
    if isinstance(unix, str):
        try:
            return datetime.fromtimestamp(float(unix), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
    iso = msg.get("date")
    if isinstance(iso, str):
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    # Fallback: epoch zero in UTC. Persist requires a non-null date.
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _parse_edited_at(msg: dict) -> datetime | None:
    """Parse the ``edited_unixtime`` / ``edited`` fields. Returns None when absent."""
    unix = msg.get("edited_unixtime")
    if isinstance(unix, str):
        try:
            return datetime.fromtimestamp(float(unix), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
    iso = msg.get("edited")
    if isinstance(iso, str):
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return None


async def _load_run(session: AsyncSession, ingestion_run_id: int) -> IngestionRun:
    stmt = select(IngestionRun).where(IngestionRun.id == ingestion_run_id)
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        raise ValueError(f"ingestion_run {ingestion_run_id} not found")
    return run


def _extract_chat_id_from_run(run: IngestionRun) -> int:
    cfg = run.config_json or {}
    cid = cfg.get("chat_id")
    if not isinstance(cid, int):
        raise ValueError(
            f"ingestion_run {run.id}: config_json.chat_id missing or non-int "
            f"(got {type(cid).__name__})"
        )
    return cid


async def _find_existing_chat_message_id(
    session: AsyncSession, chat_id: int, message_id: int
) -> int | None:
    """Return the chat_messages.id matching (chat_id, message_id), or None."""
    stmt = (
        select(ChatMessage.id)
        .where(ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _is_live_chat_message(cm: ChatMessage) -> bool:
    """Decide whether the row was created by live ingestion (not import).

    persist_message_with_policy returns the row resulting from the upsert:
    - On a fresh insert, the row carries our raw_update_id and is import-owned.
    - On a conflict (live row pre-existed), the row carries the original raw_update_id
      from live ingestion. Detecting this via the raw_update_id pointer is unreliable
      because both live and import rows have non-NULL raw_update_id. The imported_final
      flag on the version row is the correct signal — but that doesn't exist yet at
      this point because we haven't called insert_version.

    Pragmatic check: the duplicate gate at step 2 already rules out the case where a
    chat_messages row pre-existed before this apply call. So if persist returned a row
    whose memory_policy is set BUT id is NOT one we just minted, it indicates an upsert
    conflict — hence overlap. The simpler signal is: if the duplicate-gate fired, we
    bailed before persist. So at the persist step the row is always our INSERT.

    This helper currently always returns False — the duplicate gate covers the case.
    Kept for explicit naming and future cross-stream protection if a race introduces
    a window between the dup check and persist.
    """
    # NOTE: deliberate False — duplicate gate handles overlap. See docstring.
    return False
