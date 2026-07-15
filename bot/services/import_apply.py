"""Telegram Desktop import apply (T2-03 / issue #103, Stream Delta finale).

Applies a Telegram Desktop single-chat export onto the live DB through the same
normalization path live ingestion uses. The apply path
synthesises ``telegram_updates`` rows (``update_id=NULL``, ``ingestion_run_id`` set)
per imported message and routes content through ``persist_message_with_policy``
(#89 helper). New human rows may not bypass that helper; the exact-author legacy
repair path only quarantines an existing normalized row and never creates one.

Pipeline per export message (chronological order, in-chunk):

    1. Resume gate      — skip if ``export_msg_id <= last_processed_export_msg_id``.
    2. Duplicate gate   — keep complete human history; legacy tombstones are audit-only.
    4. User resolution  — ``import_user_map.resolve_export_user`` (#93). Ghost users
                      are created with ``is_imported_only=True``.
    5. Reply resolver   — ``import_reply_resolver`` priority order (same_run > prior_run
                      > live > unresolved) (#98). Translates cm PK to message_id.
    6. Synthetic raw    — Atomically upsert one canonical ``telegram_updates`` row with
                      ``update_id=NULL`` per source message. The first writer keeps
                      ``ingestion_run_id`` ownership across duplicate runs.
    7. Persist          — ``persist_message_with_policy`` writes new rows; the explicit
                      import-only rehydrator restores rows hidden by retired policy.
    8. Edit history     — ``MessageVersionRepo.insert_version(imported_final=True)`` per
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
- ``persist_message_with_policy`` is the sole creator of imported ``chat_messages``;
  exact-author repair may only redact an existing row.
- Synthetic ``telegram_updates.update_id`` is always NULL; canonical source identity is
  unique and conflict updates preserve the first owning ``ingestion_run_id``.
- ``message_versions.imported_final=TRUE`` for every imported version row.

Rollback ownership:
- New imported ``chat_messages`` link to their synthetic raw row and roll back with it.
- Existing normalized rows never adopt import raw ownership; rehydrated versions may link
  to it and keep their row when the FK is cleared on rollback.
"""

from __future__ import annotations

import asyncio
import copy
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

from bot.db.models import ChatMessage, IngestionRun, MessageVersion, TelegramUpdate
from bot.db.repos.telegram_update import TelegramUpdateRepo
from bot.services.import_checkpoint import save_checkpoint
from bot.services.import_chunking import ChunkingConfig, acquire_advisory_lock
from bot.services.import_author_exclusion import (
    is_import_author_excluded,
    normalize_import_author_name,
    normalize_import_excluded_author_names,
)
from bot.services.import_parser import _classify_td_kind, _extract_text_string
from bot.services.import_reply_resolver import resolve_reply_batch
from bot.services.import_user_map import resolve_export_user
from bot.services.message_persistence import (
    persist_message_with_policy,
    rehydrate_message_from_import,
)

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

    rehydrated_count: int = 0
    """Existing row restored from a retired redaction/forget policy."""

    skipped_tombstone_count: int = 0
    """Message blocked by forget_events tombstone."""

    tombstone_skip_export_msg_ids: list[int] = field(default_factory=list)
    """Capped list of export_msg_ids blocked by tombstones."""

    skipped_governance_count: int = 0
    """detect_policy returned offrecord — synthetic audit row kept, content not persisted."""

    skipped_excluded_author_count: int = 0
    """Explicit exact-match bot author — raw provenance kept, normalized rows omitted."""

    excluded_author_names: list[str] = field(default_factory=list)
    """Normalized exact author names configured for this import."""

    excluded_author_message_counts: dict[str, int] = field(default_factory=dict)
    """Per-author raw-only counts actually processed in this apply invocation."""

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


@dataclass(frozen=True)
class _ExistingMessageState:
    chat_message_id: int
    memory_policy: str
    is_redacted: bool
    current_version_id: int | None
    current_version_is_redacted: bool
    is_live: bool


# ─── Public API ───────────────────────────────────────────────────────────────


async def run_apply(
    session: AsyncSession,
    *,
    ingestion_run_id: int,
    resume_point: int | None = None,
    chunking_config: ChunkingConfig,
    export_path: str | None = None,
    excluded_author_names: frozenset[str] = frozenset(),
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
        export_path: Override path to the export JSON or HTML export directory. When
            ``None`` (production), the
            path is read from ``IngestionRun.source_name``. The override exists for tests
            that want to point at a fixture without touching the run row's source_name.
        excluded_author_names: Explicit author display names excluded at the normalized
            boundary. Matching is exact after NFKC + whitespace collapse + casefold.
            HTML imports require at least one name because HTML has no ``is_bot`` field.

    Returns:
        ImportApplyReport with counts and the final ``last_processed_export_msg_id``.
    """
    normalized_excluded_author_names = normalize_import_excluded_author_names(excluded_author_names)
    report = ImportApplyReport(
        ingestion_run_id=ingestion_run_id,
        chat_id=0,  # populated below
        source_path="",  # populated below
        started_at=datetime.now(tz=timezone.utc),
        chunking_config=chunking_config,
        excluded_author_names=sorted(normalized_excluded_author_names),
        excluded_author_message_counts={
            name: 0 for name in sorted(normalized_excluded_author_names)
        },
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
        if not path.is_file() and not path.is_dir():
            raise FileNotFoundError(f"export path not found: {path}")
        if (
            path.is_dir() or path.suffix.lower() == ".html"
        ) and not normalized_excluded_author_names:
            raise ValueError("HTML import requires at least one exact excluded author name")
        report.source_path = str(path)

        await _run_apply_loop(
            apply_session,
            report=report,
            run_row=run_row,
            path=path,
            chat_id=chat_id,
            resume_point=resume_point,
            chunking_config=chunking_config,
            excluded_author_names=normalized_excluded_author_names,
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
                        # pg_advisory_lock() autobegins a root transaction on this
                        # explicit connection. End that transaction before binding the
                        # worker session; otherwise its per-chunk commit only joins the
                        # external transaction and the connection context rolls every
                        # apparently successful chunk back on exit. Session-level
                        # advisory locks survive COMMIT and remain held until unlock.
                        await connection.commit()
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
    excluded_author_names: frozenset[str],
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

        # Process the chunk inside one transaction. A message savepoint keeps the
        # session usable long enough to record a structured error, but every message
        # error aborts the entire chunk. This prevents a later successful message from
        # advancing the checkpoint past missing history.
        chunk_snapshot = _snapshot_report(report)
        last_id_in_chunk: int | None = None
        try:
            for msg in chunk:
                try:
                    async with session.begin_nested():
                        advance = await _apply_one_message(
                            session,
                            msg=msg,
                            chat_id=chat_id,
                            ingestion_run_id=report.ingestion_run_id,
                            report=report,
                            excluded_author_names=excluded_author_names,
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
                    raise

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
                    "import_apply: rollback failed after chunk error",
                    extra={
                        "ingestion_run_id": report.ingestion_run_id,
                        "chunk_index": chunk_index,
                        "error_class": type(rb_err).__name__,
                        "error_taxonomy": "import_apply_chunk_rollback_failed",
                    },
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
    report: ImportApplyReport,
    excluded_author_names: frozenset[str],
) -> int | None:
    """Process one export message. Returns the export_msg_id once it has been ACKed
    (applied / duplicate / rehydrated / overlap-skipped /
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
    # do not write a synthetic update for them
    # (their structure carries no governance content). Bump the counter and ACK so
    # the checkpoint advances past them.
    if msg.get("type") == "service":
        report.skipped_service_count += 1
        return msg_id

    # Explicit bot-author exclusion is a raw-only boundary. It must run before
    # overlap/normalized duplicate checks, ghost-user creation, reply
    # resolution, or any content persistence. It also repairs legacy imports made
    # before the exact-author boundary existed: the old raw row is upgraded in
    # place and any human-like normalized row is quarantined from derived memory.
    display_name = msg.get("from") if isinstance(msg.get("from"), str) else None
    if is_import_author_excluded(display_name, excluded_author_names):
        raw_payload = _build_excluded_author_raw_payload(
            msg,
            chat_id=chat_id,
            msg_id=msg_id,
        )
        changed = await _ensure_excluded_author_raw_only(
            session,
            chat_id=chat_id,
            message_id=msg_id,
            ingestion_run_id=ingestion_run_id,
            raw_payload=raw_payload,
        )
        if changed:
            assert isinstance(display_name, str)
            normalized_author = normalize_import_author_name(display_name)
            report.skipped_excluded_author_count += 1
            report.excluded_author_message_counts[normalized_author] += 1
        else:
            report.skipped_duplicate_count += 1
        return msg_id

    # 1. Existing normal rows are idempotent.  Rows hidden by the retired
    # governance policy deliberately continue through the import-only rehydrator.
    existing = await _find_existing_chat_message_state(session, chat_id, msg_id)
    needs_rehydrate = existing is not None and (
        existing.memory_policy != "normal"
        or existing.is_redacted
        or existing.current_version_id is None
        or existing.current_version_is_redacted
    )
    if existing is not None and not needs_rehydrate:
        await _upsert_synthetic_import_raw(
            session,
            raw_payload=_build_raw_payload(msg, chat_id=chat_id, msg_id=msg_id),
            chat_id=chat_id,
            message_id=msg_id,
            ingestion_run_id=ingestion_run_id,
        )
        await _record_import_photo_if_needed(
            session,
            message_kind=_classify_td_kind(msg, warnings=None),
            chat_message_id=existing.chat_message_id,
            chat_id=chat_id,
            message_id=msg_id,
        )
        if existing.is_live:
            report.skipped_overlap_count += 1
        else:
            report.skipped_duplicate_count += 1
        return msg_id

    # 3. User resolution (#93). Service-message ducks would set from_id=None;
    # user messages either have a "user<N>" or "channel<N>" string.
    from_id = msg.get("from_id")
    user_id = await resolve_export_user(
        session,
        from_id if isinstance(from_id, str) else None,
        display_name=display_name,
        create_ghost_if_missing=True,
    )
    if user_id is None:
        # No resolved user → cannot persist (chat_messages.user_id is NOT NULL).
        # ACKing it would let the checkpoint advance past missing history.
        raise ValueError(
            f"cannot resolve user for export_msg_id={msg_id}: human message has no valid from_id"
        )

    # 4. Build kind + text/caption per parser semantics. Mirrors what the dry-run
    # parser counts so apply-side governance verdicts match dry-run preview.
    kind = _classify_td_kind(msg, warnings=None)
    text_value, caption_value = _extract_text_caption_for_kind(msg, kind)

    # 5. Reply resolution (#98). Read-only — no writes.
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

    # 6. Synthetic raw row — full canonical payload, tagged
    # with ingestion_run_id so #104 rollback can locate it. update_id MUST be NULL.
    raw_payload = _build_raw_payload(msg, chat_id=chat_id, msg_id=msg_id)
    raw_row = await _upsert_synthetic_import_raw(
        session,
        raw_payload=raw_payload,
        chat_id=chat_id,
        message_id=msg_id,
        ingestion_run_id=ingestion_run_id,
    )

    # 7. Build the message duck.
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

    # Close the read→insert race: a normal live row may appear after the initial
    # state lookup but before our synthetic raw insert.
    live_overlap_chat_message_id = None
    if existing is None:
        live_overlap_chat_message_id = await _check_live_overlap_pre_persist(
            session,
            chat_id=chat_id,
            message_id=msg_id,
            current_import_raw_update_id=raw_row.id,
        )
    if live_overlap_chat_message_id is not None:
        await _record_import_photo_if_needed(
            session,
            message_kind=kind,
            chat_message_id=live_overlap_chat_message_id,
            chat_id=chat_id,
            message_id=msg_id,
        )
        report.skipped_overlap_count += 1
        return msg_id

    # 8. New rows follow the normal helper; legacy-hidden rows use the explicit
    # complete-history rehydrator that is unavailable to live ingestion.
    if needs_rehydrate:
        assert existing is not None
        persist_result = await rehydrate_message_from_import(
            session,
            duck,
            chat_message_id=existing.chat_message_id,
            raw_update_id=raw_row.id,
            raw_payload=raw_payload,
            captured_at=duck.date,
        )
        report.rehydrated_count += 1
    else:
        persist_result = await persist_message_with_policy(
            session,
            duck,
            raw_update_id=raw_row.id,
            source="import",
            captured_at=duck.date,
        )
        report.applied_count += 1

    await _record_import_photo_if_needed(
        session,
        message_kind=kind,
        chat_message_id=persist_result.chat_message.id,
        chat_id=chat_id,
        message_id=msg_id,
    )

    return msg_id


# ─── Internal helpers ────────────────────────────────────────


async def _upsert_synthetic_import_raw(
    session: AsyncSession,
    *,
    raw_payload: dict[str, Any],
    chat_id: int,
    message_id: int,
    ingestion_run_id: int,
) -> TelegramUpdate:
    """Keep duplicate/overlap imports raw-first without taking normalized ownership."""
    return await TelegramUpdateRepo.upsert_import_message(
        session,
        raw_json=raw_payload,
        chat_id=chat_id,
        message_id=message_id,
        ingestion_run_id=ingestion_run_id,
    )


async def _record_import_photo_if_needed(
    session: AsyncSession,
    *,
    message_kind: str,
    chat_message_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    if message_kind != "photo":
        return
    from bot.services.image_memory import record_missing_import_photo

    await record_missing_import_photo(
        session,
        chat_message_id=chat_message_id,
        chat_id=chat_id,
        message_id=message_id,
    )


def _iter_export_messages(path: Path) -> Iterable[dict[str, Any]]:
    """Yield canonical message dicts from JSON or Telegram HTML export.

    Reuses the parser's tolerant-reader contract: single-chat envelope, ``messages[]``
    list, malformed entries skipped silently. Full-account exports were already
    rejected at the dry-run gate; we re-validate the envelope shape defensively here.
    """
    if path.is_dir() or path.suffix.lower() == ".html":
        from bot.services.import_html_parser import iter_html_messages

        yield from iter_html_messages(path)
        return

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
        "photo",
        "video",
        "voice",
        "audio",
        "document",
        "sticker",
        "animation",
        "video_note",
    }
    if kind in media_kinds:
        # TD text field is the caption for media messages
        return (None, text_str or None)
    return (text_str or None, None)


def _build_raw_payload(msg: dict, *, chat_id: int, msg_id: int) -> dict:
    """Preserve the complete canonical export payload for a human message."""
    raw = copy.deepcopy(msg)
    raw.setdefault("chat_id", chat_id)
    raw.setdefault("message_id", msg_id)
    return raw


def _build_excluded_author_raw_payload(
    msg: dict,
    *,
    chat_id: int,
    msg_id: int,
) -> dict:
    """Preserve the complete canonical export payload for an exact excluded author.

    Excluded bot output has no normalized ``chat_messages``/``message_versions`` row,
    so the raw update is its sole archive and is explicitly labelled.
    """
    raw = copy.deepcopy(msg)
    raw.setdefault("chat_id", chat_id)
    raw.setdefault("message_id", msg_id)
    raw["excluded_author"] = True
    return raw


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
        "photo": None,
        "video": None,
        "voice": None,
        "audio": None,
        "document": None,
        "sticker": None,
        "animation": None,
        "video_note": None,
        "location": None,
        "contact": None,
        "poll": None,
        "dice": None,
        "forward_origin": None,
        "new_chat_members": None,
        "left_chat_member": None,
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
        _contact_info = (
            msg.get("contact_information")
            if isinstance(msg.get("contact_information"), dict)
            else None
        )
        _first = (
            _contact_info.get("first_name")
            if _contact_info and isinstance(_contact_info.get("first_name"), str)
            else None
        )
        _last = (
            _contact_info.get("last_name")
            if _contact_info and isinstance(_contact_info.get("last_name"), str)
            else None
        )
        kind_attrs["contact"] = SimpleNamespace(_imported=True, first_name=_first, last_name=_last)

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


async def _find_existing_chat_message_state(
    session: AsyncSession, chat_id: int, message_id: int
) -> _ExistingMessageState | None:
    """Load enough state to distinguish idempotency, overlap and rehydration."""
    stmt = (
        select(
            ChatMessage.id,
            ChatMessage.memory_policy,
            ChatMessage.is_redacted,
            ChatMessage.current_version_id,
            MessageVersion.is_redacted,
            TelegramUpdate.update_id,
        )
        .outerjoin(MessageVersion, MessageVersion.id == ChatMessage.current_version_id)
        .outerjoin(TelegramUpdate, TelegramUpdate.id == ChatMessage.raw_update_id)
        .where(ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id)
        .limit(1)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return _ExistingMessageState(
        chat_message_id=int(row[0]),
        memory_policy=str(row[1]),
        is_redacted=bool(row[2]),
        current_version_id=int(row[3]) if row[3] is not None else None,
        current_version_is_redacted=bool(row[4]) if row[4] is not None else False,
        is_live=row[5] is not None,
    )


async def _check_live_overlap_pre_persist(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    current_import_raw_update_id: int,
) -> int | None:
    stmt = (
        select(ChatMessage.id)
        .join(TelegramUpdate, TelegramUpdate.id == ChatMessage.raw_update_id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
            TelegramUpdate.update_id.is_not(None),
            ChatMessage.raw_update_id != current_import_raw_update_id,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _ensure_excluded_author_raw_only(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    ingestion_run_id: int,
    raw_payload: dict[str, Any],
) -> bool:
    """Enforce the exact-author raw-only boundary, including legacy repair.

    Returns ``True`` when this import inserted/upgraded raw provenance or quarantined
    normalized content. A fully canonical raw row with no eligible normalized content
    returns ``False`` so repeated imports remain ordinary duplicates.
    """
    raw_row = (
        await session.execute(
            select(TelegramUpdate)
            .where(
                TelegramUpdate.chat_id == chat_id,
                TelegramUpdate.message_id == message_id,
                TelegramUpdate.update_type == _IMPORT_UPDATE_TYPE,
                TelegramUpdate.update_id.is_(None),
            )
            .order_by(TelegramUpdate.id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    changed = raw_row is None or (
        raw_row.raw_json != raw_payload
        or raw_row.raw_hash is not None
        or raw_row.is_redacted
        or raw_row.redaction_reason is not None
    )
    await _upsert_synthetic_import_raw(
        session,
        raw_payload=raw_payload,
        chat_id=chat_id,
        message_id=message_id,
        ingestion_run_id=ingestion_run_id,
    )

    normalized_row = (
        await session.execute(
            select(ChatMessage)
            .where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == message_id,
            )
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if normalized_row is None:
        return changed

    versions = (
        (
            await session.execute(
                select(MessageVersion)
                .where(MessageVersion.chat_message_id == normalized_row.id)
                .order_by(MessageVersion.version_seq)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    normalized_needs_quarantine = (
        normalized_row.memory_policy != "forgotten"
        or not normalized_row.is_redacted
        or normalized_row.text is not None
        or normalized_row.caption is not None
        or normalized_row.raw_json is not None
    )
    versions_need_quarantine = any(
        not version.is_redacted
        or version.text is not None
        or version.caption is not None
        or version.normalized_text is not None
        or version.entities_json is not None
        for version in versions
    )
    if not normalized_needs_quarantine and not versions_need_quarantine:
        return changed

    normalized_row.memory_policy = "forgotten"
    normalized_row.is_redacted = True
    normalized_row.text = None
    normalized_row.caption = None
    normalized_row.raw_json = None
    normalized_row.updated_at = datetime.now(tz=timezone.utc)
    for version in versions:
        version.is_redacted = True
        version.text = None
        version.caption = None
        version.normalized_text = None
        version.entities_json = None
    return True


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


def _record_message_error(
    report: ImportApplyReport,
    *,
    msg: dict[str, Any],
    chat_id: int,
    exc: BaseException,
) -> None:
    # Per-message envelope: capture context before the caller aborts the chunk.
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
    "rehydrated_count",
    "skipped_duplicate_count",
    "skipped_tombstone_count",
    "skipped_governance_count",
    "skipped_excluded_author_count",
    "excluded_author_names",
    "excluded_author_message_counts",
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
        if isinstance(value, list):
            snapshot[field_name] = list(value)
        elif isinstance(value, dict):
            snapshot[field_name] = dict(value)
        else:
            snapshot[field_name] = value
    return snapshot


def _restore_report(report: ImportApplyReport, snapshot: dict[str, Any]) -> None:
    for field_name, value in snapshot.items():
        restored: Any
        if isinstance(value, list):
            restored = list(value)
        elif isinstance(value, dict):
            restored = dict(value)
        else:
            restored = value
        setattr(report, field_name, restored)


def _get_bound_async_engine(session: AsyncSession) -> AsyncEngine | None:
    """Return the AsyncEngine behind an engine-bound AsyncSession, if any."""
    sync_bind = session.get_bind()
    if not isinstance(sync_bind, Engine):
        return None
    return AsyncEngine._retrieve_proxy_for_target(sync_bind)
