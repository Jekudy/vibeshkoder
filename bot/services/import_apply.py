"""Telegram Desktop import apply (T2-03 / issue #103, Stream Delta finale).

Applies a Telegram Desktop single-chat export onto the live DB through the SAME
governance + normalization path live ingestion uses (ADR-0007). The apply path
synthesises ``telegram_updates`` rows (``update_id=NULL``, ``ingestion_run_id`` set)
per imported message and routes content through ``persist_message_with_policy``
(#89 helper). Direct writes to chat_messages bypassing the helper are forbidden.

Pipeline per export message (chronological order, in-chunk):

    1. Resume gate      — skip if ``export_msg_id <= last_processed_export_msg_id``.
    2. Tombstone gate   — chunk-level ``batch_check_tombstones_by_message_key`` (#97).
    3. Duplicate gate   — ``chat_messages`` or prior import audit row lookup.
    4. User resolution  — ``import_user_map.resolve_export_user`` (#93). Ghost users
                      are created with ``is_imported_only=True``.
    5. Full tombstone   — ``check_tombstone`` with content_hash + ``user:{tg_id}`` before
                      the synthetic raw insert.
    6. Reply resolver   — ``import_reply_resolver`` priority order (same_run > prior_run
                      > live > unresolved) (#98). Translates cm PK to message_id.
    7. Synthetic raw    — Write a ``telegram_updates`` row with ``update_id=NULL`` and
                      ``ingestion_run_id`` set. Audit row stays even when governance
                      rejects the content.
    8. Governance       — ``governance.detect_policy`` runs after the synthetic audit row.
                      ``offrecord`` keeps only that audit row and skips persistence.
    9. Persist          — for non-offrecord outcomes, ``persist_message_with_policy`` (#89)
                      writes ``chat_messages``.
    10. Edit history    — ``MessageVersionRepo.insert_version(imported_final=True)`` per
                      #106. Skipped when persist returns a row whose ``raw_update_id``
                      is not the synthetic raw id (live overlap won).
    11. Checkpoint      — once per CHUNK, ``save_checkpoint`` deep-merges
                      ``last_processed_export_msg_id`` into ``stats_json`` in the same
                      transaction as the chunk data.

Cross-stream contract:
- With advisory locking enabled, engine-bound callers get a fresh
  ``AsyncConnection`` and a fresh ``AsyncSession(bind=conn)`` for the full apply run.
  Per-chunk ``session.commit()`` releases each chunk's transaction without releasing
  the lock connection.

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
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bot.db.models import ChatMessage, IngestionRun, TelegramUpdate
from bot.db.repos.message_version import MessageVersionRepo
from bot.db.repos.telegram_update import TelegramUpdateRepo
from bot.services.content_hash import compute_content_hash
from bot.services.governance import detect_policy
from bot.services.import_checkpoint import save_checkpoint
from bot.services.import_chunking import ChunkingConfig, acquire_advisory_lock
from bot.services.import_parser import _classify_td_kind, _extract_text_string
from bot.services.import_reply_resolver import resolve_reply_batch
from bot.services.import_tombstone import (
    batch_check_tombstones_by_message_key,
    check_tombstone,
)
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

    tombstone_skip_export_msg_ids: list[int] = field(default_factory=list)
    """Capped list of export_msg_ids blocked by tombstones."""

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
        session: Active AsyncSession. Will be committed per chunk when no separate lock
            connection is needed. When the session is engine-bound and advisory locking is
            enabled, run_apply opens one AsyncConnection and binds a fresh AsyncSession to
            it for the whole apply so the connection-scoped lock cannot be lost to pooling.
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

    async def _prepare_and_run(apply_session: AsyncSession) -> None:
        # Resolve the run row to read chat_id and source_name. Use NOWAIT-free read to
        # tolerate a concurrent reader that's looking at the same row for finalize_run.
        run_row = await _load_run(apply_session, ingestion_run_id)
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

        await _run_apply_loop(
            apply_session,
            report=report,
            run_row=run_row,
            path=path,
            chat_id=chat_id,
            resume_point=resume_point,
            chunking_config=chunking_config,
        )

    try:
        if chunking_config.use_advisory_lock:
            async_engine = _get_bound_async_engine(session)
            if async_engine is not None:
                # pg_advisory_lock is connection-scoped. For pooled sessions, bind the
                # entire apply to one explicit AsyncConnection so per-chunk commits cannot
                # swap the connection underneath the lock.
                async with async_engine.connect() as connection:
                    async with acquire_advisory_lock(connection, ingestion_run_id):
                        async with AsyncSession(
                            bind=connection,
                            expire_on_commit=False,
                        ) as locked_session:
                            await _prepare_and_run(locked_session)
            else:
                # Tests and explicitly connection-bound callers already provide a single
                # AsyncConnection. Reuse it rather than opening an invisible second
                # connection that would not see the caller's uncommitted fixture data.
                connection = await session.connection()
                async with acquire_advisory_lock(connection, ingestion_run_id):
                    await _prepare_and_run(session)
        else:
            await _prepare_and_run(session)
    except BaseException as exc:
        report.finished_at = datetime.now(tz=timezone.utc)
        setattr(exc, "import_apply_report", report)
        raise

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

        # Process chunk inside a single per-chunk transaction. Each message gets a
        # SAVEPOINT so validation/mapping failures roll back only that message. The
        # checkpoint is written inside the same outer transaction as the chunk data;
        # one commit makes both visible atomically.
        chunk_snapshot = _snapshot_report(report)
        last_id_in_chunk: int | None = None
        try:
            # Tombstone gate — one bulk SELECT per chunk (#97). Chunks containing only
            # service messages still call this so the contract is uniform; it's a single
            # query so the cost is negligible.
            export_ids = [int(m["id"]) for m in chunk if isinstance(m.get("id"), int)]
            tombstone_hits = await batch_check_tombstones_by_message_key(
                session,
                chat_id=chat_id,
                export_msg_ids=export_ids,
            )

            for msg in chunk:
                try:
                    async with session.begin_nested():
                        advance = await _apply_one_message(
                            session,
                            msg=msg,
                            chat_id=chat_id,
                            ingestion_run_id=report.ingestion_run_id,
                            tombstone_hits=tombstone_hits,
                            report=report,
                        )
                except SQLAlchemyError:
                    logger.error(
                        "import_apply: per-message database error; aborting chunk",
                        extra={
                            "ingestion_run_id": report.ingestion_run_id,
                            "chat_id": chat_id,
                            "export_msg_id": msg.get("id"),
                        },
                    )
                    raise
                except (ValueError, RuntimeError) as exc:
                    _record_message_error(report, msg=msg, chat_id=chat_id, exc=exc)
                    # Continue with next message — DO NOT advance last_id_in_chunk.
                    advance = None

                if advance is not None:
                    last_id_in_chunk = advance

            if last_id_in_chunk is not None:
                await save_checkpoint(
                    session,
                    ingestion_run_id=report.ingestion_run_id,
                    last_processed_export_msg_id=last_id_in_chunk,
                    chunk_index=chunk_index,
                )
                report.last_processed_export_msg_id = last_id_in_chunk

            # Commit the chunk data and checkpoint together.
            await session.commit()
        except BaseException:
            try:
                await session.rollback()
            except SQLAlchemyError as rb_err:
                logger.warning(
                    "import_apply: rollback failed after chunk error "
                    "(ingestion_run_id=%s, chunk_index=%s): %s",
                    report.ingestion_run_id,
                    chunk_index,
                    rb_err,
                )
            _restore_report(report, chunk_snapshot)
            raise

        report.chunks_processed += 1

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
        _record_tombstone_skip(report, msg_id)
        return msg_id

    # 1b. Early live overlap check (§3.2 — must come BEFORE generic dup gate so live
    # overlaps are classified correctly and skipped_overlap_count is bumped instead of
    # skipped_duplicate_count).  No raw row yet — check without raw_update_id guard.
    early_overlap = await _check_early_live_overlap(session, chat_id=chat_id, message_id=msg_id)
    if early_overlap:
        report.skipped_overlap_count += 1
        return msg_id

    # 2. Duplicate gate — chat_messages already has this (chat_id, message_id) from a
    # prior import run (non-live row).
    existing_cm_id = await _find_existing_chat_message_id(session, chat_id, msg_id)
    if existing_cm_id is not None:
        report.skipped_duplicate_count += 1
        return msg_id

    # Offrecord idempotency gate — offrecord messages keep only the synthetic audit
    # telegram_updates row, so chat_messages cannot serve as their duplicate marker.
    existing_import_update_id = await _find_existing_import_update_id(session, chat_id, msg_id)
    if existing_import_update_id is not None:
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

    # 4. Build kind + text/caption per parser semantics. Mirrors what the dry-run
    # parser counts so apply-side governance verdicts match dry-run preview.
    kind = _classify_td_kind(msg, warnings=None)
    text_value, caption_value = _extract_text_caption_for_kind(msg, kind)

    # 5. Compute content hash (chv1) before the full tombstone check so message_hash
    # tombstones are also honored. The new privacy-critical bit is user:{tg_id}.
    text_entities = msg.get("text_entities") if isinstance(msg.get("text_entities"), list) else None
    content_hash = compute_content_hash(
        text=text_value,
        caption=caption_value,
        message_kind=kind,
        entities=text_entities,
    )

    tombstone = await check_tombstone(
        session,
        chat_id=chat_id,
        message_id=msg_id,
        content_hash=content_hash,
        user_tg_id=user_id,
    )
    if tombstone is not None:
        _record_tombstone_skip(report, msg_id)
        return msg_id

    # 6. Reply resolution (#98). Read-only — no writes.
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
            # The resolver returns chat_messages.id. Persist expects the live-handler
            # equivalent Telegram message_id of the parent, so translate the PK before
            # building the importer duck. If the row vanished, drop the pointer.
            resolved_reply_to_message_id = await _find_chat_message_message_id_by_id(
                session,
                resolution.chat_message_id,
            )
        if resolved_reply_to_message_id is None:
            # Cross-overlap guard: a live raw update can carry the export id while the
            # linked chat_messages row has a different live-handler message_id.
            resolved_reply_to_message_id = (
                await _find_chat_message_message_id_by_raw_update_message_id(
                    session,
                    chat_id=chat_id,
                    raw_update_message_id=reply_export_id,
                )
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
    #
    # H1 fix: mirror message_persistence.py broadened-scan (Sprint #89 Commit 2).
    # TD poll dict has a top-level "poll" key with "question". TD contact fields
    # are NESTED under contact_information (not top-level) — confirmed by
    # import_parser.py which uses msg.get("contact_information") as discriminator.
    _poll_dict = msg.get("poll") if kind == "poll" else None
    poll_question: str | None = None
    if isinstance(_poll_dict, dict):
        _q = _poll_dict.get("question")
        if isinstance(_q, str) and _q:
            poll_question = _q
    contact_name: str | None = None
    if kind == "contact":
        _contact_info = msg.get("contact_information")
        if isinstance(_contact_info, dict):
            _first = _contact_info.get("first_name")
            _last = _contact_info.get("last_name")
            parts = [p for p in [_first, _last] if isinstance(p, str) and p]
            if parts:
                contact_name = " ".join(parts)
    policy, _mark_payload = detect_policy(
        text_value,
        caption_value,
        poll_question=poll_question,
        contact_name=contact_name,
    )
    # 8b. Hotfix #164 H2 fix: do NOT short-circuit on offrecord. The helper handles
    # offrecord internally (writes chat_messages with memory_policy='offrecord', creates
    # OffrecordMark, creates redacted v1) — restoring import↔live audit symmetry per
    # invariant #8 and closing risk-audit H2. Counter is retained for operator dashboards.
    if policy == "offrecord":
        raw_row.is_redacted = True
        raw_row.redaction_reason = "offrecord"
        await session.flush()
        report.skipped_governance_count += 1
        # IMPORTANT: do NOT return here. Continue to step 9.

    # 9. Build the message duck.
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

    # 10. Explicit overlap pre-check BEFORE persist (hotfix #164 §3.2).
    # If a LIVE chat_messages row already exists for (chat_id, message_id), the live
    # row is authoritative — skip the helper entirely.
    is_overlap = await _check_live_overlap_pre_persist(
        session,
        chat_id=chat_id,
        message_id=msg_id,
        current_import_raw_update_id=raw_row.id,
    )
    if is_overlap:
        report.skipped_overlap_count += 1
        return msg_id

    # 11. Single helper call — handles both normal AND offrecord paths uniformly.
    # The helper is now the SOLE writer for (chat_messages, message_versions,
    # current_version_id) triples — this closes CRITICAL 2, CRITICAL 3, and H2.
    persist_result = await persist_message_with_policy(
        session,
        duck,
        raw_update_id=raw_row.id,
        source="import",
        captured_at=duck.date,  # preserve export captured_at
    )

    # 12. Counter branching.
    if persist_result.policy != "offrecord":
        report.applied_count += 1
    # offrecord case: skipped_governance_count was already bumped in step 8b.

    return msg_id


# ─── Internal helpers ─────────────────────────────────────────────────────────


async def _check_early_live_overlap(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
) -> bool:
    """Return True if a LIVE chat_messages row already exists for (chat_id, message_id).

    Called BEFORE the generic duplicate gate and BEFORE the synthetic raw row is
    created, so no ``current_import_raw_update_id`` exclusion is needed.  Live rows
    are identified by: chat_messages.raw_update_id → telegram_updates row WHERE
    update_id IS NOT NULL (live handler writes real Telegram update_id; import writes NULL).
    """
    stmt = (
        select(ChatMessage.id)
        .join(TelegramUpdate, TelegramUpdate.id == ChatMessage.raw_update_id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
            TelegramUpdate.update_id.is_not(None),  # live, not synthetic-import
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _check_live_overlap_pre_persist(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    current_import_raw_update_id: int,
) -> bool:
    """Return True if a LIVE chat_messages row already exists for (chat_id, message_id).

    Live rows are identified by:
      chat_messages.raw_update_id → telegram_updates row WHERE update_id IS NOT NULL.
    Synthetic import raw rows (telegram_updates.update_id IS NULL) are NOT treated
    as live overlaps; they are prior import runs and handled by the dup-check earlier.

    Called BEFORE persist_message_with_policy so the live row is authoritative and
    the import is skipped cleanly without creating a duplicate.
    """
    stmt = (
        select(ChatMessage.id)
        .join(TelegramUpdate, TelegramUpdate.id == ChatMessage.raw_update_id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
            TelegramUpdate.update_id.is_not(None),  # live, not synthetic-import
            ChatMessage.raw_update_id != current_import_raw_update_id,  # not us
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


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

    # H1 fix: supply poll.question and contact.first_name/last_name so that
    # persist_message_with_policy's broadened detect_policy scan (Sprint #89)
    # sees the same content as the step-8 governance gate above.
    # TD poll dict is nested under msg["poll"]["question"].
    # TD contact fields are NESTED under contact_information (not top-level) —
    # confirmed by import_parser.py which uses msg.get("contact_information") as
    # the kind discriminator.
    if message_kind == "poll":
        _poll_dict = msg.get("poll")
        _poll_question: str | None = None
        if isinstance(_poll_dict, dict):
            _q = _poll_dict.get("question")
            if isinstance(_q, str):
                _poll_question = _q or None
        kind_attrs["poll"] = SimpleNamespace(_imported=True, question=_poll_question)
    if message_kind == "contact":
        _contact_info = msg.get("contact_information") if isinstance(msg.get("contact_information"), dict) else None
        _first = _contact_info.get("first_name") if _contact_info and isinstance(_contact_info.get("first_name"), str) else None
        _last = _contact_info.get("last_name") if _contact_info and isinstance(_contact_info.get("last_name"), str) else None
        kind_attrs["contact"] = SimpleNamespace(
            _imported=True, first_name=_first, last_name=_last
        )

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


async def _find_existing_import_update_id(
    session: AsyncSession, chat_id: int, message_id: int
) -> int | None:
    """Return an existing import synthetic update id for offrecord idempotency."""
    stmt = (
        select(TelegramUpdate.id)
        .where(
            TelegramUpdate.chat_id == chat_id,
            TelegramUpdate.message_id == message_id,
            TelegramUpdate.update_type == _IMPORT_UPDATE_TYPE,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_chat_message_message_id_by_raw_update_message_id(
    session: AsyncSession,
    *,
    chat_id: int,
    raw_update_message_id: int,
) -> int | None:
    """Return cm.message_id for a row linked to telegram_updates.message_id."""
    stmt = (
        select(ChatMessage.message_id)
        .join(TelegramUpdate, ChatMessage.raw_update_id == TelegramUpdate.id)
        .where(
            ChatMessage.chat_id == chat_id,
            TelegramUpdate.message_id == raw_update_message_id,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_chat_message_message_id_by_id(
    session: AsyncSession,
    chat_message_id: int,
) -> int | None:
    """Translate chat_messages.id to the live-handler-equivalent message_id."""
    stmt = select(ChatMessage.message_id).where(ChatMessage.id == chat_message_id).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _record_tombstone_skip(report: ImportApplyReport, export_msg_id: int) -> None:
    report.skipped_tombstone_count += 1
    if len(report.tombstone_skip_export_msg_ids) < _ERROR_ID_CAP:
        report.tombstone_skip_export_msg_ids.append(export_msg_id)


def _record_message_error(
    report: ImportApplyReport,
    *,
    msg: dict[str, Any],
    chat_id: int,
    exc: BaseException,
) -> None:
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


_REPORT_SNAPSHOT_FIELDS = (
    "applied_count",
    "skipped_duplicate_count",
    "skipped_tombstone_count",
    "skipped_governance_count",
    "skipped_resume_count",
    "skipped_service_count",
    "skipped_overlap_count",
    "error_count",
    "error_export_msg_ids",
    "tombstone_skip_export_msg_ids",
    "last_processed_export_msg_id",
    "chunks_processed",
)


def _snapshot_report(report: ImportApplyReport) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for field_name in _REPORT_SNAPSHOT_FIELDS:
        value = getattr(report, field_name)
        snapshot[field_name] = list(value) if isinstance(value, list) else value
    return snapshot


def _restore_report(report: ImportApplyReport, snapshot: dict[str, Any]) -> None:
    for field_name, value in snapshot.items():
        setattr(report, field_name, list(value) if isinstance(value, list) else value)


def _get_bound_async_engine(session: AsyncSession) -> AsyncEngine | None:
    """Return the AsyncEngine behind an engine-bound AsyncSession, if any."""
    sync_bind = session.get_bind()
    if not isinstance(sync_bind, Engine):
        return None
    return AsyncEngine._retrieve_proxy_for_target(sync_bind)


def _is_live_chat_message(cm: ChatMessage, *, synthetic_raw_update_id: int) -> bool:
    """Decide whether the row was created by live ingestion (not import).

    persist_message_with_policy returns the row resulting from the upsert:
    - On a fresh insert, the row carries our raw_update_id and is import-owned.
    - On a conflict (live row pre-existed), the row carries the original raw_update_id
      from live ingestion. Compare against the synthetic raw row we just inserted.
    """
    return cm.raw_update_id != synthetic_raw_update_id
