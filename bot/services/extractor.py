"""Phase 6 extraction service (T6-02).

PHASE6_PLAN.md §5.B + §7 T6-02 acceptance criteria.

This module owns the extractor pass that reads eligible ``chat_messages``
(via current ``message_versions``), builds a privacy-filtered evidence
bundle, calls an LLM gateway through the ``ExtractCandidatesGateway``
Protocol seam (concrete impl lands in T6-03), and writes
``extraction_runs`` + ``extraction_candidates`` rows.

Public surface:

* ``run_extraction_pass(session, *, window_start, window_end, gateway,
  operator_user_id=None)`` — single pass over a time window.
* ``MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG`` — feature flag key.
* ``extraction_scheduler_tick(session, *, gateway)`` — flag-gated
  scheduler entry-point. Default OFF.
* ``ExtractCandidatesGateway`` — Protocol the gateway must implement.
* ``ExtractionResult`` / ``ExtractedCandidate`` / ``SchedulerTickResult``
  — return-value dataclasses.

Privacy invariants (PHASE6_PLAN.md §1 #3, §5.B Stop Conditions):

* ``memory_policy != 'normal'`` rows MUST be excluded from the bundle.
* ``message_versions.is_redacted=true`` rows MUST be excluded.
* Rows matched by any ``forget_events`` tombstone MUST be excluded.
* Defense-in-depth: if a bundle assembled by the SELECT contains ANY
  ineligible row, the pass refuses to call the gateway and records the
  ExtractionRun as ``failed`` with a structured reason.

The gateway call is forbidden whenever ANY source row in the evidence
bundle fails the governance pre-check (PHASE6_PLAN.md §5.B invariant
paragraph).
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import uuid as _uuid_module
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import func, select, text, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db.engine import async_session as _engine_session
from bot.db.models import (
    ExtractionCandidate,
    ExtractionCursor,
    ExtractionRun,
    ExtractionRunResolution,
)
from bot.db.repos.feature_flag import FeatureFlagRepo
from bot.services.extraction_schema import (
    EXTRACTION_CANDIDATE_SCHEMA_VERSION,
    EXTRACTION_PROMPT_TEMPLATE_VERSION,
    MAX_EXTRACTION_INPUT_BYTES,
    extraction_input_size_bytes,
    serialize_untrusted_source_versions,
)

logger = logging.getLogger(__name__)


# ─── Scheduler-tick advisory lock (Codex HIGH #4) ────────────────────────────


def _p6_scheduler_lock_id() -> int:
    """Derive the Phase 6 scheduler-tick advisory lock id.

    Two concurrent ``extraction_scheduler_tick`` calls would otherwise
    race on the same window: the SELECT, gateway call, and audit-row
    writes are not idempotent under concurrency. A
    ``pg_try_advisory_xact_lock`` keyed by a constant
    ``p6:extraction_scheduler`` namespace gates the tick — second
    concurrent tick gets ``False`` from try-lock and short-circuits
    with ``reason='locked'``.

    The namespace prefix ``"p6:extraction_scheduler"`` is disjoint from
    the per-mvid ``"p6:mvid:"`` namespace used by /approve + the
    forget-cascade orchestrator (see
    ``bot.services.forget_cascade._p6_mvid_advisory_lock_id``), so the
    scheduler lock cannot collide with an in-flight cascade lock for
    any numeric id.

    Returns a value in the signed-int64 range expected by
    ``pg_advisory_xact_lock(bigint)``.
    """
    payload = b"p6:extraction_scheduler"
    digest = hashlib.sha256(payload).digest()
    (lock_id,) = struct.unpack(">q", digest[:8])
    return lock_id


async def _try_acquire_scheduler_lock(session: AsyncSession) -> bool:
    """Attempt to acquire the scheduler-tick advisory lock; non-blocking.

    Returns ``True`` if acquired (the current transaction now holds the
    lock for its remaining lifetime), ``False`` if another transaction
    already holds it. Skipping on ``False`` keeps semantics simple — the
    next tick on the next schedule will pick up any pending work.
    """
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": _p6_scheduler_lock_id()},
    )
    locked = result.scalar()
    return bool(locked)


# ─── Feature flag key (PHASE6_PLAN.md §7 T6-02) ──────────────────────────────


MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG = "memory.extraction.scheduler.enabled"


# ─── Protocol seam — concrete impl lands in T6-03 ────────────────────────────


@runtime_checkable
class ExtractCandidatesGateway(Protocol):
    """Typed contract for the Phase 6 LLM gateway extraction entry point.

    T6-02 ships only the seam; T6-03 (Stream B follow-up) provides the
    concrete implementation under ``bot/services/llm_gateway.py::
    extract_candidates``. Tests inject fakes that satisfy this Protocol.

    The gateway is the ONLY allowed LLM call site for extraction (HANDOFF
    invariant #2: "no LLM calls outside ``llm_gateway``"). The extractor
    builds a privacy-cleared evidence bundle, hands it off, and persists
    whatever candidates the gateway emits — no raw provider SDK use.

    ``@runtime_checkable`` enables T6-03 DI middleware to validate gateway
    instances with ``isinstance(gw, ExtractCandidatesGateway)`` at wire
    time. Note: ``isinstance`` checks only attribute presence, NOT
    signatures — call-site contract enforcement still relies on static
    typing.
    """

    @property
    def extraction_provider(self) -> str:
        """Stable provider identifier used in semantic spend identity."""
        ...

    @property
    def extraction_model(self) -> str:
        """Stable model identifier used in semantic spend identity."""
        ...

    async def extract_candidates(
        self,
        session: Any,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = EXTRACTION_PROMPT_TEMPLATE_VERSION,
    ) -> dict[str, Any]:
        """Return ``{"candidates": [...], "llm_usage_ledger_id": int | None}``.

        Each candidate is ``{"candidate_json": <jsonb-compatible dict>,
        "source_message_version_ids": [int, ...]}``. The
        ``llm_usage_ledger_id`` references the Phase 5 ``llm_usage_ledger``
        row the gateway wrote for this call (or ``None`` on abstention
        paths that still produced audit metadata).
        """
        ...


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedCandidate:
    """One LLM-emitted candidate ready for persistence."""

    candidate_json: dict[str, Any]
    source_message_version_ids: list[int]


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of a single ``run_extraction_pass`` call."""

    extraction_run_id: _uuid_module.UUID
    run_status: str  # 'completed' | 'failed'
    candidate_count: int
    failure_reason: str | None = None
    llm_usage_ledger_id: int | None = None
    resumed: bool = False


@dataclass(frozen=True)
class SchedulerTickResult:
    """Outcome of one scheduler tick (`extraction_scheduler_tick`)."""

    skipped: bool
    extraction_result: ExtractionResult | None = None
    reason: str | None = None


class AmbiguousExtractionRunError(RuntimeError):
    """A durable reservation exists without a terminal outcome.

    The provider may already have accepted the request, so automatically
    dispatching it again could double-spend. Operator reconciliation is
    required before the semantic key may be retried.
    """

    def __init__(self, extraction_run_id: _uuid_module.UUID) -> None:
        super().__init__(
            "extraction has a durable in-flight reservation; "
            f"operator reconciliation required for run_id={extraction_run_id}"
        )
        self.extraction_run_id = extraction_run_id


class ReconciledExtractionSnapshotChangedError(RuntimeError):
    """A resolved retry no longer maps to its original semantic snapshot."""

    def __init__(self, extraction_run_id: _uuid_module.UUID) -> None:
        super().__init__(
            "reconciled extraction source snapshot changed before retry; "
            f"run_id={extraction_run_id}"
        )
        self.extraction_run_id = extraction_run_id


# ─── Source-row dataclass for bundle assembly ────────────────────────────────


@dataclass(frozen=True)
class _SourceRow:
    chat_message_id: int
    message_version_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    text: str | None
    caption: str | None
    normalized_text: str | None
    memory_policy: str
    is_redacted: bool
    version_is_redacted: bool
    event_at: datetime

    def to_gateway_payload(self) -> dict[str, Any]:
        """Render for the gateway. NO forbidden fields included."""
        return {
            "chat_message_id": self.chat_message_id,
            "message_version_id": self.message_version_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "text": self.text,
            "caption": self.caption,
            "normalized_text": self.normalized_text,
        }


# ─── Internal helpers ────────────────────────────────────────────────────────


async def _select_eligible_sources(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    source_chat_id: int | None,
    force_include_chat_message_ids: list[int] | None,
    selection_mode: str = "event_time",
    after_message_version_id: int | None = None,
    through_message_version_id: int | None = None,
) -> list[_SourceRow]:
    """SELECT chat_messages + current message_versions that pass governance.

    Filters (PHASE6_PLAN.md §5.B):

    * ``chat_messages.memory_policy = 'normal'``
    * ``chat_messages.is_redacted = FALSE``
    * ``message_versions.is_redacted = FALSE``
    * ``chat_messages.current_version_id = message_versions.id`` (current only)
    * ``chat_messages.date >= window_start AND < window_end`` (Telegram event
      time, not the import-time ``created_at`` audit timestamp).
    * NOT matched by any ``forget_events`` row (defense via NOT EXISTS).

    ``force_include_chat_message_ids`` is a test-only escape hatch that
    re-INCLUDES rows by ``chat_messages.id`` even if they'd otherwise be
    filtered out. Production callers MUST NOT set it — the
    ``run_extraction_pass`` invariant guard catches the leakage in that
    case and records the run as failed.
    """
    # Tombstone matching mirrors bot/services/search.py and the cascade's
    # tombstone_key construction in bot/services/forget_cascade.py +
    # bot/services/import_tombstone.py:
    #
    #   * ``message:<chat_id>:<message_id>``   — emitted by /forget reply
    #   * ``message_hash:<content_hash>``      — emitted on cross-chat dedup
    #   * ``user:<telegram_id>``               — emitted by /forget me
    #
    # The ``message_hash:`` key MUST match ``message_versions.content_hash``,
    # NOT ``chat_messages.content_hash`` (Codex round 3 CRITICAL):
    #
    #   * ``MessageVersion.content_hash`` is NOT NULL (DB-enforced) — every
    #     live message has it populated by ``MessageVersionRepo.insert_version``.
    #   * ``ChatMessage.content_hash`` is nullable AND the live persistence
    #     path (``bot/db/repos/message.py::MessageRepo.save``) never sets it —
    #     only the import path (``bot/services/import_apply.py``) populates it.
    #     Filtering on ``c.content_hash`` silently no-op's every
    #     ``message_hash:`` tombstone for live messages, letting tombstoned
    #     content leak to the LLM.
    #
    # Keep this comment in sync with search.py / llm_gateway.py if any of the
    # three target_type → tombstone_key conventions changes. Note: search.py
    # has the same live-message bug (filed as a separate follow-up; out of
    # scope for T6-02).
    source_chat_predicate = "AND c.chat_id = :source_chat_id" if source_chat_id is not None else ""
    if selection_mode == "event_time":
        selection_predicate = """
            AND c.date >= :window_start
            AND c.date < :window_end
        """
    elif selection_mode == "version_cursor":
        if source_chat_id is None:
            raise ValueError("source_chat_id is required for version_cursor selection")
        if after_message_version_id is None or through_message_version_id is None:
            raise ValueError("version_cursor selection requires both cursor bounds")
        if after_message_version_id < 0 or through_message_version_id < after_message_version_id:
            raise ValueError("invalid version_cursor bounds")
        if force_include_chat_message_ids:
            raise ValueError("forced source ids are supported only for event_time selection")
        selection_predicate = """
            AND mv.id > :after_message_version_id
            AND mv.id <= :through_message_version_id
        """
    else:
        raise ValueError("selection_mode must be 'event_time' or 'version_cursor'")
    base_predicate = f"""
        c.current_version_id = mv.id
        AND c.memory_policy = 'normal'
        AND c.is_redacted = FALSE
        AND mv.is_redacted = FALSE
        {selection_predicate}
        {source_chat_predicate}
        AND NOT EXISTS (
            SELECT 1
            FROM forget_events AS fe
            WHERE (
                fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
                OR (
                    mv.content_hash IS NOT NULL
                    AND fe.tombstone_key = 'message_hash:' || mv.content_hash
                )
                OR (
                    c.user_id IS NOT NULL
                    AND fe.tombstone_key = 'user:' || c.user_id::text
                )
            )
            AND fe.status IN ('pending', 'processing', 'completed')
        )
    """

    if force_include_chat_message_ids:
        # Test-only path. Rows joined by id are forwarded raw — exactly the
        # bypass that lets the defense-in-depth guard fire in tests.
        # SAFE: both dynamic fragments are selected from fixed SQL literals;
        # runtime values use bound parameters.
        stmt = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
            SELECT
                c.id AS chat_message_id,
                mv.id AS message_version_id,
                c.chat_id AS chat_id,
                c.message_id AS message_id,
                c.user_id AS user_id,
                mv.text AS text,
                mv.caption AS caption,
                concat_ws(
                    E'\n',
                    mv.normalized_text,
                    mv.caption,
                    CASE
                        WHEN mm.description_status = 'ready' THEN
                            '[Описание изображения] ' || mm.description
                            || E'\n[Источник изображения] ' || mm.source_message_url
                        ELSE NULL
                    END
                ) AS normalized_text,
                c.memory_policy AS memory_policy,
                c.is_redacted AS is_redacted,
                mv.is_redacted AS version_is_redacted,
                c.date AS event_at
            FROM chat_messages AS c
            JOIN message_versions AS mv ON mv.chat_message_id = c.id
            LEFT JOIN message_media AS mm ON mm.chat_message_id = c.id
            WHERE c.id = ANY(:force_ids)
              AND mv.id = c.current_version_id
              {source_chat_predicate}
            UNION
            SELECT
                c.id AS chat_message_id,
                mv.id AS message_version_id,
                c.chat_id AS chat_id,
                c.message_id AS message_id,
                c.user_id AS user_id,
                mv.text AS text,
                mv.caption AS caption,
                concat_ws(
                    E'\n',
                    mv.normalized_text,
                    mv.caption,
                    CASE
                        WHEN mm.description_status = 'ready' THEN
                            '[Описание изображения] ' || mm.description
                            || E'\n[Источник изображения] ' || mm.source_message_url
                        ELSE NULL
                    END
                ) AS normalized_text,
                c.memory_policy AS memory_policy,
                c.is_redacted AS is_redacted,
                mv.is_redacted AS version_is_redacted,
                c.date AS event_at
            FROM chat_messages AS c
            JOIN message_versions AS mv ON mv.chat_message_id = c.id
            LEFT JOIN message_media AS mm ON mm.chat_message_id = c.id
            WHERE {base_predicate}
            ORDER BY event_at ASC, message_version_id ASC
            """
        )
        params = {
            "window_start": window_start,
            "window_end": window_end,
            "force_ids": force_include_chat_message_ids,
        }
    else:
        # SAFE: both dynamic fragments are selected from fixed SQL literals;
        # runtime values use bound parameters.
        stmt = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
            SELECT
                c.id AS chat_message_id,
                mv.id AS message_version_id,
                c.chat_id AS chat_id,
                c.message_id AS message_id,
                c.user_id AS user_id,
                mv.text AS text,
                mv.caption AS caption,
                concat_ws(
                    E'\n',
                    mv.normalized_text,
                    mv.caption,
                    CASE
                        WHEN mm.description_status = 'ready' THEN
                            '[Описание изображения] ' || mm.description
                            || E'\n[Источник изображения] ' || mm.source_message_url
                        ELSE NULL
                    END
                ) AS normalized_text,
                c.memory_policy AS memory_policy,
                c.is_redacted AS is_redacted,
                mv.is_redacted AS version_is_redacted,
                c.date AS event_at
            FROM chat_messages AS c
            JOIN message_versions AS mv ON mv.chat_message_id = c.id
            LEFT JOIN message_media AS mm ON mm.chat_message_id = c.id
            WHERE {base_predicate}
            ORDER BY c.date ASC, mv.id ASC
            """
        )
        params = {"window_start": window_start, "window_end": window_end}

    if selection_mode == "version_cursor":
        params["after_message_version_id"] = after_message_version_id
        params["through_message_version_id"] = through_message_version_id

    if source_chat_id is not None:
        params["source_chat_id"] = source_chat_id

    result = await session.execute(stmt, params)
    return [
        _SourceRow(
            chat_message_id=row["chat_message_id"],
            message_version_id=row["message_version_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            user_id=row["user_id"],
            text=row["text"],
            caption=row["caption"],
            normalized_text=row["normalized_text"],
            memory_policy=row["memory_policy"],
            is_redacted=bool(row["is_redacted"]),
            version_is_redacted=bool(row["version_is_redacted"]),
            event_at=row["event_at"],
        )
        for row in result.mappings().all()
    ]


async def _bundle_is_clean(
    session: AsyncSession, rows: list[_SourceRow]
) -> tuple[bool, str | None]:
    """Defense-in-depth re-check after SELECT.

    Returns ``(True, None)`` when every row is eligible. Otherwise
    ``(False, <reason>)`` with a structured-log-friendly reason string.

    PHASE6_PLAN.md §5.B "Invariant: the LLM call is forbidden whenever ANY
    source row in the evidence bundle has memory_policy != 'normal'. The
    guard runs BEFORE llm_gateway.extract_candidates() is invoked."

    Two layers of defense:

    1. **Materialized fast-path.** The SELECT already filtered out
       offrecord / redacted rows; this loop re-confirms the snapshotted
       fields before forwarding. Cheap (in-memory only).

    2. **Fresh forget_events re-query.** SAME CLASS OF RACE AS H-Cdx-2:
       between ``_select_eligible_sources`` and the gateway call, a new
       ``forget_events`` row can land that targets a bundle row. The
       materialized snapshot would miss it, so we re-query the live
       ``forget_events`` table for any tombstone matching the bundle's
       chat_message ids. If a fresh tombstone exists, refuse the call.
    """
    for row in rows:
        if row.memory_policy != "normal":
            return (
                False,
                f"source_memory_policy_not_normal:cm_id={row.chat_message_id}",
            )
        if row.is_redacted:
            return False, f"source_redacted:cm_id={row.chat_message_id}"
        if row.version_is_redacted:
            return False, f"source_version_redacted:mv_id={row.message_version_id}"

    if not rows:
        return True, None

    # Fresh forget_events re-query — closes the SELECT→gateway race window.
    # The join clauses match the cascade's tombstone_key construction
    # (see bot/services/forget_cascade.py): forget_reply emits
    # ``message:<chat>:<msg>``, /forget-me emits ``user:<tg_id>``, and
    # message_hash tombstones MUST match against ``message_versions.content_hash``
    # (NOT NULL by schema), not ``chat_messages.content_hash`` (NULL for
    # live messages — see _select_eligible_sources comment for the
    # full Codex round 3 CRITICAL rationale). We pin the JOIN to the
    # row's CURRENT version via ``mv.id = c.current_version_id`` so the
    # tombstone check sees exactly the same MV the bundle forwarded.
    chat_msg_ids = [row.chat_message_id for row in rows]
    result = await session.execute(
        text(
            """
            SELECT c.id AS chat_message_id
            FROM chat_messages AS c
            JOIN message_versions AS mv
              ON mv.chat_message_id = c.id
             AND mv.id = c.current_version_id
            JOIN forget_events AS fe ON (
                fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
                OR (
                    mv.content_hash IS NOT NULL
                    AND fe.tombstone_key = 'message_hash:' || mv.content_hash
                )
                OR (
                    c.user_id IS NOT NULL
                    AND fe.tombstone_key = 'user:' || c.user_id::text
                )
            )
            WHERE c.id = ANY(:chat_msg_ids)
              AND fe.status IN ('pending', 'processing', 'completed')
            LIMIT 1
            """
        ),
        {"chat_msg_ids": chat_msg_ids},
    )
    raced_row = result.first()
    if raced_row is not None:
        return (
            False,
            f"fresh_forget_event_during_extraction:cm_id={raced_row[0]}",
        )

    return True, None


async def _persist_failed_run(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    failure_reason: str,
    operator_user_id: int | None = None,
    source_chat_id: int | None = None,
) -> ExtractionResult:
    """Insert a ``run_status='failed'`` ExtractionRun row and return."""
    run = ExtractionRun(
        ingestion_window_start=window_start,
        ingestion_window_end=window_end,
        candidate_count=0,
        run_status="failed",
        operator_user_id=operator_user_id,
        source_chat_id=source_chat_id,
    )
    session.add(run)
    await session.flush()
    logger.warning(
        "extraction_pass_aborted",
        extra={
            "extraction_run_id": str(run.id),
            "failure_reason": failure_reason,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "operator_user_id": operator_user_id,
        },
    )
    return ExtractionResult(
        extraction_run_id=run.id,
        run_status="failed",
        candidate_count=0,
        failure_reason=failure_reason,
        llm_usage_ledger_id=None,
    )


# ─── Public API ──────────────────────────────────────────────────────────────


def _gateway_identity(gateway: ExtractCandidatesGateway) -> tuple[str, str]:
    provider = getattr(gateway, "extraction_provider", None)
    model = getattr(gateway, "extraction_model", None)
    if not isinstance(provider, str) or not 1 <= len(provider) <= 64:
        raise ValueError("gateway.extraction_provider must contain 1 to 64 characters")
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise ValueError("gateway.extraction_model must contain 1 to 128 characters")
    return provider, model


def _canonical_utc(value: datetime, *, field_name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _semantic_identity(
    *,
    source_payload: list[dict[str, Any]],
    source_chat_id: int | None,
    window_start: datetime,
    window_end: datetime,
    prompt_template_version: str,
    provider: str,
    model: str,
    selection_mode: str,
    cursor_start_message_version_id: int | None,
    cursor_end_message_version_id: int | None,
) -> tuple[str, str]:
    serialized_sources = serialize_untrusted_source_versions(source_payload)
    source_snapshot_hash = hashlib.sha256(serialized_sources.encode("utf-8")).hexdigest()
    identity: dict[str, Any] = {
        "model": model,
        "prompt_template_version": prompt_template_version,
        "provider": provider,
        "selection_mode": selection_mode,
        "source_chat_id": source_chat_id,
        "source_snapshot_hash": source_snapshot_hash,
    }
    if selection_mode == "event_time":
        identity["window_start"] = _canonical_utc(window_start, field_name="window_start")
        identity["window_end"] = _canonical_utc(window_end, field_name="window_end")
    else:
        identity["cursor_start_message_version_id"] = cursor_start_message_version_id
        identity["cursor_end_message_version_id"] = cursor_end_message_version_id
    semantic_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return semantic_key, source_snapshot_hash


def _semantic_lock_id(semantic_key: str) -> int:
    (lock_id,) = struct.unpack(">q", bytes.fromhex(semantic_key)[:8])
    return lock_id


def _factory_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncEngine:
    bind = session_factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("durable extraction requires a session factory bound to AsyncEngine")
    return bind


async def _advance_extraction_cursor(
    session: AsyncSession,
    *,
    source_chat_id: int,
    through_message_version_id: int,
) -> None:
    statement = pg_insert(ExtractionCursor).values(
        source_chat_id=source_chat_id,
        last_message_version_id=through_message_version_id,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ExtractionCursor.source_chat_id],
            set_={
                "last_message_version_id": func.greatest(
                    ExtractionCursor.last_message_version_id,
                    statement.excluded.last_message_version_id,
                ),
                "updated_at": func.now(),
            },
        )
    )


def _existing_run_result(run: ExtractionRun) -> ExtractionResult:
    if run.run_status == "running":
        raise AmbiguousExtractionRunError(run.id)
    return ExtractionResult(
        extraction_run_id=run.id,
        run_status=run.run_status,
        candidate_count=run.candidate_count,
        failure_reason=("previous_failed_run" if run.run_status == "failed" else None),
        llm_usage_ledger_id=run.llm_usage_ledger_id,
        resumed=True,
    )


def _abandoned_run_result(run: ExtractionRun) -> ExtractionResult:
    return ExtractionResult(
        extraction_run_id=run.id,
        run_status="completed",
        candidate_count=0,
        failure_reason="operator_abandoned_memory_gap",
        llm_usage_ledger_id=run.llm_usage_ledger_id,
        resumed=True,
    )


async def _run_durable_semantic_extraction(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: ExtractCandidatesGateway,
    source_payload: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    operator_user_id: int | None,
    source_chat_id: int | None,
    prompt_template_version: str,
    selection_mode: str,
    cursor_start_message_version_id: int | None,
    cursor_end_message_version_id: int | None,
    expected_retry_of_run_id: _uuid_module.UUID | None = None,
) -> ExtractionResult:
    """Reserve one semantic spend, then commit its entire outcome atomically.

    A dedicated transaction-level advisory lock serializes identical semantic
    keys while a separate durable transaction commits the reservation before
    provider dispatch. If the process dies after dispatch but before the
    terminal commit, the durable ``running`` row deliberately blocks any
    automatic repeat.
    """
    if not isinstance(prompt_template_version, str) or not (
        1 <= len(prompt_template_version) <= 64
    ):
        raise ValueError("prompt_template_version must contain 1 to 64 characters")
    provider, model = _gateway_identity(gateway)
    semantic_key, source_snapshot_hash = _semantic_identity(
        source_payload=source_payload,
        source_chat_id=source_chat_id,
        window_start=window_start,
        window_end=window_end,
        prompt_template_version=prompt_template_version,
        provider=provider,
        model=model,
        selection_mode=selection_mode,
        cursor_start_message_version_id=cursor_start_message_version_id,
        cursor_end_message_version_id=cursor_end_message_version_id,
    )
    engine = _factory_engine(session_factory)

    # Keep the xact-level advisory lock on a dedicated connection so the run
    # reservation can commit before dispatch without releasing serialization.
    async with engine.connect() as lock_connection:
        async with lock_connection.begin():
            await lock_connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _semantic_lock_id(semantic_key)},
            )
            async with session_factory() as durable_session:
                existing = await durable_session.scalar(
                    select(ExtractionRun)
                    .where(ExtractionRun.semantic_key == semantic_key)
                    .order_by(ExtractionRun.attempt_no.desc())
                    .limit(1)
                )
                if expected_retry_of_run_id is not None and (
                    existing is None or existing.id != expected_retry_of_run_id
                ):
                    # The scheduler pinned the original cursor bounds, but an
                    # edit/redaction can still change the materialized source
                    # payload inside those bounds. Never create an unrelated
                    # semantic attempt or dispatch it as the operator-approved
                    # retry.
                    raise ReconciledExtractionSnapshotChangedError(expected_retry_of_run_id)
                retry_of_run_id: _uuid_module.UUID | None = None
                attempt_no = 1
                if existing is not None:
                    resolution = await durable_session.scalar(
                        select(ExtractionRunResolution).where(
                            ExtractionRunResolution.run_id == existing.id
                        )
                    )
                    if resolution is not None and resolution.action == "abandon":
                        if (
                            selection_mode == "version_cursor"
                            and source_chat_id is not None
                            and cursor_end_message_version_id is not None
                        ):
                            await _advance_extraction_cursor(
                                durable_session,
                                source_chat_id=source_chat_id,
                                through_message_version_id=cursor_end_message_version_id,
                            )
                            await durable_session.commit()
                        return _abandoned_run_result(existing)
                    if resolution is None:
                        result = _existing_run_result(existing)
                        if (
                            result.run_status == "completed"
                            and selection_mode == "version_cursor"
                            and source_chat_id is not None
                            and cursor_end_message_version_id is not None
                        ):
                            await _advance_extraction_cursor(
                                durable_session,
                                source_chat_id=source_chat_id,
                                through_message_version_id=cursor_end_message_version_id,
                            )
                            await durable_session.commit()
                        return result
                    retry_of_run_id = existing.id
                    attempt_no = existing.attempt_no + 1

                run = ExtractionRun(
                    ingestion_window_start=window_start,
                    ingestion_window_end=window_end,
                    candidate_count=0,
                    run_status="running",
                    operator_user_id=operator_user_id,
                    source_chat_id=source_chat_id,
                    semantic_key=semantic_key,
                    source_snapshot_hash=source_snapshot_hash,
                    prompt_template_version=prompt_template_version,
                    provider=provider,
                    model=model,
                    selection_mode=selection_mode,
                    cursor_start_message_version_id=cursor_start_message_version_id,
                    cursor_end_message_version_id=cursor_end_message_version_id,
                    attempt_no=attempt_no,
                    retry_of_run_id=retry_of_run_id,
                    dispatch_state="not_dispatched",
                )
                durable_session.add(run)
                await durable_session.commit()
                run_id = run.id

                if not source_payload:
                    await durable_session.execute(
                        sa_update(ExtractionRun)
                        .where(ExtractionRun.id == run_id)
                        .values(run_status="completed")
                    )
                    if (
                        selection_mode == "version_cursor"
                        and source_chat_id is not None
                        and cursor_end_message_version_id is not None
                    ):
                        await _advance_extraction_cursor(
                            durable_session,
                            source_chat_id=source_chat_id,
                            through_message_version_id=cursor_end_message_version_id,
                        )
                    await durable_session.commit()
                    return ExtractionResult(
                        extraction_run_id=run_id,
                        run_status="completed",
                        candidate_count=0,
                    )

                # From this durable point onward, absence of a terminal response
                # is ambiguous: the provider might have accepted the paid call.
                await durable_session.execute(
                    sa_update(ExtractionRun)
                    .where(ExtractionRun.id == run_id)
                    .values(dispatch_state="unknown")
                )
                await durable_session.commit()
                try:
                    gateway_result = await gateway.extract_candidates(
                        durable_session,
                        source_versions=source_payload,
                        prompt_template_version=prompt_template_version,
                    )
                except Exception as exc:
                    await durable_session.rollback()
                    await durable_session.execute(
                        sa_update(ExtractionRun)
                        .where(ExtractionRun.id == run_id)
                        .values(
                            run_status="failed",
                            gateway_error=f"provider_exception:{type(exc).__name__}",
                        )
                    )
                    await durable_session.commit()
                    logger.warning(
                        "extraction_pass_gateway_crashed",
                        extra={
                            "extraction_run_id": str(run_id),
                            "error_class": type(exc).__name__,
                        },
                    )
                    raise

                candidates_raw = gateway_result.get("candidates", []) or []
                llm_usage_ledger_id = gateway_result.get("llm_usage_ledger_id")
                gateway_error = gateway_result.get("gateway_error")

                if gateway_error is not None:
                    dispatch_state = (
                        "rejected_pre_accept"
                        if gateway_error == "provider_transient:rate_limit"
                        else "response_received"
                    )
                    await durable_session.execute(
                        sa_update(ExtractionRun)
                        .where(ExtractionRun.id == run_id)
                        .values(
                            run_status="failed",
                            llm_usage_ledger_id=llm_usage_ledger_id,
                            gateway_error=gateway_error,
                            dispatch_state=dispatch_state,
                        )
                    )
                    await durable_session.commit()
                    logger.warning(
                        "extraction_pass_gateway_error",
                        extra={
                            "extraction_run_id": str(run_id),
                            "llm_usage_ledger_id": llm_usage_ledger_id,
                        },
                    )
                    return ExtractionResult(
                        extraction_run_id=run_id,
                        run_status="failed",
                        candidate_count=0,
                        failure_reason="gateway_error",
                        llm_usage_ledger_id=llm_usage_ledger_id,
                    )

                if llm_usage_ledger_id is None:
                    await durable_session.execute(
                        sa_update(ExtractionRun)
                        .where(ExtractionRun.id == run_id)
                        .values(
                            run_status="failed",
                            dispatch_state="response_received",
                        )
                    )
                    await durable_session.commit()
                    return ExtractionResult(
                        extraction_run_id=run_id,
                        run_status="failed",
                        candidate_count=0,
                        failure_reason="no_llm_ledger_entry",
                    )

                candidate_objs = [
                    ExtractionCandidate(
                        extraction_run_id=run_id,
                        candidate_json=dict(candidate.get("candidate_json") or {}),
                        source_message_version_ids=list(
                            candidate.get("source_message_version_ids") or []
                        ),
                        status="pending",
                        payload_schema_version=EXTRACTION_CANDIDATE_SCHEMA_VERSION,
                    )
                    for candidate in candidates_raw
                ]
                durable_session.add_all(candidate_objs)
                await durable_session.execute(
                    sa_update(ExtractionRun)
                    .where(ExtractionRun.id == run_id)
                    .values(
                        run_status="completed",
                        candidate_count=len(candidate_objs),
                        llm_usage_ledger_id=llm_usage_ledger_id,
                        dispatch_state="response_received",
                    )
                )
                if (
                    selection_mode == "version_cursor"
                    and source_chat_id is not None
                    and cursor_end_message_version_id is not None
                ):
                    await _advance_extraction_cursor(
                        durable_session,
                        source_chat_id=source_chat_id,
                        through_message_version_id=cursor_end_message_version_id,
                    )
                # Ledger, candidates, terminal run status, and live cursor are
                # one commit. A caller/middleware rollback cannot split them.
                await durable_session.commit()

                logger.info(
                    "extraction_pass_completed",
                    extra={
                        "extraction_run_id": str(run_id),
                        "candidate_count": len(candidate_objs),
                        "llm_usage_ledger_id": llm_usage_ledger_id,
                    },
                )
                return ExtractionResult(
                    extraction_run_id=run_id,
                    run_status="completed",
                    candidate_count=len(candidate_objs),
                    llm_usage_ledger_id=llm_usage_ledger_id,
                )


async def run_extraction_pass(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    gateway: ExtractCandidatesGateway,
    operator_user_id: int | None = None,
    source_chat_id: int | None = None,
    prompt_template_version: str = EXTRACTION_PROMPT_TEMPLATE_VERSION,
    durable_session_factory: async_sessionmaker[AsyncSession] | None = None,
    selection_mode: str = "event_time",
    cursor_start_message_version_id: int | None = None,
    cursor_end_message_version_id: int | None = None,
    _force_include_chat_message_ids: list[int] | None = None,
    _expected_retry_of_run_id: _uuid_module.UUID | None = None,
) -> ExtractionResult:
    """Run one extraction pass over ``[window_start, window_end)``.

    The function:

    1. SELECTs eligible chat_messages + current message_versions
       (governance-filtered per PHASE6_PLAN §5.B).
    2. Re-checks the bundle (defense-in-depth) before any gateway call.
       Any non-normal / redacted row → record ``run_status='failed'`` and
       return WITHOUT invoking the gateway.
    3. If the bundle is empty, write a ``completed`` run with
       ``candidate_count=0`` and NO gateway call (empty-bundle short-circuit
       mirrors ``llm_gateway.synthesize_answer`` invariant #1).
    4. Else, call ``gateway.extract_candidates`` and persist each emitted
       candidate as an ``extraction_candidates`` row with ``status='pending'``.

    ``operator_user_id`` and ``source_chat_id`` are durable audit dimensions
    on ``extraction_runs``. Production scheduler calls must provide the source
    chat; ``None`` remains only for legacy/manual test compatibility.

    ``_force_include_chat_message_ids`` is a TEST-ONLY hook that adds rows
    to the bundle that would otherwise be filtered. Production callers
    MUST leave it ``None``; if it surfaces an offrecord row in tests, the
    invariant guard at step 2 fires.
    """
    if window_end <= window_start:
        # Defensive: empty/inverted ranges produce no work. Still write a
        # completed run so audit captures the no-op.
        run = ExtractionRun(
            ingestion_window_start=window_start,
            ingestion_window_end=window_end,
            candidate_count=0,
            run_status="completed",
            operator_user_id=operator_user_id,
            source_chat_id=source_chat_id,
        )
        session.add(run)
        await session.flush()
        return ExtractionResult(
            extraction_run_id=run.id,
            run_status="completed",
            candidate_count=0,
        )

    rows = await _select_eligible_sources(
        session,
        window_start=window_start,
        window_end=window_end,
        source_chat_id=source_chat_id,
        force_include_chat_message_ids=_force_include_chat_message_ids,
        selection_mode=selection_mode,
        after_message_version_id=cursor_start_message_version_id,
        through_message_version_id=cursor_end_message_version_id,
    )
    clean, reason = await _bundle_is_clean(session, rows)
    if not clean:
        return await _persist_failed_run(
            session,
            window_start=window_start,
            window_end=window_end,
            failure_reason=reason or "governance_violation",
            operator_user_id=operator_user_id,
            source_chat_id=source_chat_id,
        )

    if not rows and selection_mode == "event_time":
        run = ExtractionRun(
            ingestion_window_start=window_start,
            ingestion_window_end=window_end,
            candidate_count=0,
            run_status="completed",
            operator_user_id=operator_user_id,
            source_chat_id=source_chat_id,
        )
        session.add(run)
        await session.flush()
        logger.info(
            "extraction_pass_empty_window",
            extra={
                "extraction_run_id": str(run.id),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "operator_user_id": operator_user_id,
            },
        )
        return ExtractionResult(
            extraction_run_id=run.id,
            run_status="completed",
            candidate_count=0,
        )

    source_payload = [row.to_gateway_payload() for row in rows]
    if extraction_input_size_bytes(source_payload) > MAX_EXTRACTION_INPUT_BYTES:
        return await _persist_failed_run(
            session,
            window_start=window_start,
            window_end=window_end,
            failure_reason="input_size_exceeded",
            operator_user_id=operator_user_id,
            source_chat_id=source_chat_id,
        )

    return await _run_durable_semantic_extraction(
        session_factory=durable_session_factory or _engine_session,
        gateway=gateway,
        source_payload=source_payload,
        window_start=window_start,
        window_end=window_end,
        operator_user_id=operator_user_id,
        source_chat_id=source_chat_id,
        prompt_template_version=prompt_template_version,
        selection_mode=selection_mode,
        cursor_start_message_version_id=cursor_start_message_version_id,
        cursor_end_message_version_id=cursor_end_message_version_id,
        expected_retry_of_run_id=_expected_retry_of_run_id,
    )


async def _get_phase_6_enabled_at(session: AsyncSession) -> datetime | None:
    """Return the ``updated_at`` of the scheduler flag row.

    Missing row means the flag is OFF. ``updated_at`` is used only to choose
    the initial live-cursor baseline. Once ``extraction_cursors`` contains a
    row, toggling the flag cannot move that durable high-water mark backward.
    """
    from bot.db.models import FeatureFlag

    stmt = (
        select(FeatureFlag.updated_at, FeatureFlag.enabled)
        .where(
            FeatureFlag.flag_key == MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG,
            FeatureFlag.scope_type.is_(None),
            FeatureFlag.scope_id.is_(None),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    updated_at, enabled = row
    if not enabled:
        return None
    return updated_at


async def _get_scheduler_window_start(
    session: AsyncSession,
    *,
    enabled_at: datetime,
    window_end: datetime,
    source_chat_id: int,
) -> datetime:
    """Return an audit-window start for scheduler-driven runs.

    ``operator_user_id IS NULL`` is the existing durable discriminator for a
    scheduler run. Manual ``/admin_extract`` calls persist their actor id and
    therefore cannot move this audit boundary. Source selection itself uses
    ``extraction_cursors`` and does not depend on Telegram event time.
    """
    latest_completed_end = await session.scalar(
        select(func.max(ExtractionRun.ingestion_window_end)).where(
            ExtractionRun.run_status == "completed",
            ExtractionRun.operator_user_id.is_(None),
            ExtractionRun.source_chat_id == source_chat_id,
            ExtractionRun.ingestion_window_end.is_not(None),
            ExtractionRun.ingestion_window_end >= enabled_at,
            ExtractionRun.ingestion_window_end <= window_end,
        )
    )
    if latest_completed_end is None or latest_completed_end < enabled_at:
        return enabled_at
    return latest_completed_end


async def _get_scheduler_cursor_start(
    session: AsyncSession,
    *,
    source_chat_id: int,
    enabled_at: datetime,
) -> int:
    existing = await session.get(ExtractionCursor, source_chat_id)
    if existing is not None:
        return existing.last_message_version_id

    # The first live tick starts after versions that were already captured
    # before enablement. Historical coverage is the explicit event-time
    # backfill's responsibility. Later imports/edits are caught by their new
    # monotonically increasing message_versions.id, regardless of event date.
    baseline = await session.scalar(
        text(
            """
            SELECT COALESCE(MAX(mv.id), 0)
            FROM message_versions AS mv
            JOIN chat_messages AS c
              ON c.id = mv.chat_message_id
             AND c.current_version_id = mv.id
            WHERE c.chat_id = :source_chat_id
              AND mv.captured_at < :enabled_at
            """
        ),
        {"source_chat_id": source_chat_id, "enabled_at": enabled_at},
    )
    return int(baseline or 0)


async def _get_scheduler_cursor_end(
    session: AsyncSession,
    *,
    source_chat_id: int,
    after_message_version_id: int,
) -> tuple[int, int | None]:
    row = (
        await session.execute(
            text(
                """
            SELECT
                COALESCE(MAX(mv.id), 0) AS high_water,
                MIN(mv.id) FILTER (
                    WHERE mm.description_status IN ('pending', 'processing')
                ) AS blocking_image_message_version_id
            FROM message_versions AS mv
            JOIN chat_messages AS c
              ON c.id = mv.chat_message_id
             AND c.current_version_id = mv.id
            LEFT JOIN message_media AS mm
              ON mm.chat_message_id = c.id
            WHERE c.chat_id = :source_chat_id
              AND mv.id > :after_message_version_id
            """
            ),
            {
                "source_chat_id": source_chat_id,
                "after_message_version_id": after_message_version_id,
            },
        )
    ).one()
    high_water = int(row[0] or 0)
    blocking_id = int(row[1]) if row[1] is not None else None
    if blocking_id is not None:
        safe_end = await session.scalar(
            text(
                """
                SELECT MAX(mv.id)
                FROM message_versions AS mv
                JOIN chat_messages AS c
                  ON c.id = mv.chat_message_id
                 AND c.current_version_id = mv.id
                WHERE c.chat_id = :source_chat_id
                  AND mv.id > :after_message_version_id
                  AND mv.id < :blocking_id
                """
            ),
            {
                "source_chat_id": source_chat_id,
                "after_message_version_id": after_message_version_id,
                "blocking_id": blocking_id,
            },
        )
        return int(safe_end or after_message_version_id), blocking_id
    return high_water, None


async def _get_reconciled_cursor_retry(
    session: AsyncSession,
    *,
    source_chat_id: int,
    cursor_start_message_version_id: int,
) -> ExtractionRun | None:
    """Return the exact resolved retry window before admitting newer versions."""

    return await session.scalar(
        select(ExtractionRun)
        .join(
            ExtractionRunResolution,
            ExtractionRunResolution.run_id == ExtractionRun.id,
        )
        .where(
            ExtractionRun.source_chat_id == source_chat_id,
            ExtractionRun.selection_mode == "version_cursor",
            ExtractionRun.cursor_start_message_version_id == cursor_start_message_version_id,
            ExtractionRun.run_status.in_(("running", "failed")),
            ExtractionRunResolution.action.in_(("safe_retry", "risk_accepted_retry")),
        )
        .order_by(ExtractionRun.attempt_no.desc())
        .limit(1)
    )


async def _first_blocking_image_in_cursor_window(
    session: AsyncSession,
    *,
    source_chat_id: int,
    after_message_version_id: int,
    through_message_version_id: int,
) -> int | None:
    blocking = await session.scalar(
        text(
            """
            SELECT MIN(mv.id)
            FROM message_versions AS mv
            JOIN chat_messages AS c
              ON c.id = mv.chat_message_id
             AND c.current_version_id = mv.id
            JOIN message_media AS mm
              ON mm.chat_message_id = c.id
            WHERE c.chat_id = :source_chat_id
              AND mv.id > :after_message_version_id
              AND mv.id <= :through_message_version_id
              AND mm.description_status IN ('pending', 'processing')
            """
        ),
        {
            "source_chat_id": source_chat_id,
            "after_message_version_id": after_message_version_id,
            "through_message_version_id": through_message_version_id,
        },
    )
    return int(blocking) if blocking is not None else None


async def _has_unresolved_cursor_run(
    session: AsyncSession,
    *,
    source_chat_id: int,
    cursor_start_message_version_id: int,
) -> bool:
    unresolved = await session.scalar(
        select(ExtractionRun.id)
        .outerjoin(
            ExtractionRunResolution,
            ExtractionRunResolution.run_id == ExtractionRun.id,
        )
        .where(
            ExtractionRun.source_chat_id == source_chat_id,
            ExtractionRun.selection_mode == "version_cursor",
            ExtractionRun.cursor_start_message_version_id == cursor_start_message_version_id,
            ExtractionRun.run_status.in_(("running", "failed")),
            ExtractionRunResolution.id.is_(None),
        )
        .limit(1)
    )
    return unresolved is not None


async def extraction_scheduler_tick(
    session: AsyncSession,
    *,
    gateway: ExtractCandidatesGateway,
    now: datetime | None = None,
    source_chat_id: int | None = None,
    durable_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SchedulerTickResult:
    """Scheduler entry-point — reads the flag, runs the pass if enabled.

    PHASE6_PLAN.md §4 + §7 T6-02 acceptance:

    * Default (flag missing OR False) → return ``skipped=True`` without
      side effects.
    * Flag True → process current message versions whose monotonically
      increasing id is above the durable per-chat cursor. This catches late
      inserts and edits even when their Telegram event date is old.
    * Historical ``/admin_extract`` and backfill calls remain event-time based.

    The admin handler `/admin/extract` calls ``run_extraction_pass``
    directly and bypasses this entry-point (no flag check, explicit window).
    """
    if not await FeatureFlagRepo.get(session, MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG):
        return SchedulerTickResult(skipped=True, reason="flag_disabled")

    if source_chat_id is None:
        raise ValueError("source_chat_id is required for an enabled extraction scheduler")

    # Idempotency gate (Codex HIGH #4): only one tick may run at a time.
    # Acquired non-blocking so the second concurrent tick exits cleanly
    # rather than queueing up duplicate gateway calls.
    if not await _try_acquire_scheduler_lock(session):
        logger.info(
            "extraction_scheduler_tick_locked",
            extra={"reason": "another_tick_in_progress"},
        )
        return SchedulerTickResult(skipped=True, reason="locked")

    phase_6_enabled_at = await _get_phase_6_enabled_at(session)
    if phase_6_enabled_at is None:
        # Flag.get says enabled, but row vanished — defensive skip.
        return SchedulerTickResult(skipped=True, reason="flag_row_missing")

    if now is None:
        now = datetime.now(tz=phase_6_enabled_at.tzinfo)

    cursor_start = await _get_scheduler_cursor_start(
        session,
        source_chat_id=source_chat_id,
        enabled_at=phase_6_enabled_at,
    )
    if await _has_unresolved_cursor_run(
        session,
        source_chat_id=source_chat_id,
        cursor_start_message_version_id=cursor_start,
    ):
        logger.error(
            "extraction_scheduler_unresolved_run",
            extra={
                "source_chat_id": source_chat_id,
                "cursor_start_message_version_id": cursor_start,
            },
        )
        return SchedulerTickResult(skipped=True, reason="unresolved_extraction_run")

    reconciled_retry = await _get_reconciled_cursor_retry(
        session,
        source_chat_id=source_chat_id,
        cursor_start_message_version_id=cursor_start,
    )
    if reconciled_retry is not None:
        if reconciled_retry.cursor_end_message_version_id is None:
            raise RuntimeError("reconciled cursor retry is missing its end bound")
        cursor_end = reconciled_retry.cursor_end_message_version_id
        blocking_image_message_version_id = await _first_blocking_image_in_cursor_window(
            session,
            source_chat_id=source_chat_id,
            after_message_version_id=cursor_start,
            through_message_version_id=cursor_end,
        )
        if blocking_image_message_version_id is not None:
            return SchedulerTickResult(
                skipped=True,
                reason="waiting_for_image_description",
            )
    else:
        cursor_end, blocking_image_message_version_id = await _get_scheduler_cursor_end(
            session,
            source_chat_id=source_chat_id,
            after_message_version_id=cursor_start,
        )
    if cursor_end <= cursor_start:
        if blocking_image_message_version_id is not None:
            return SchedulerTickResult(
                skipped=True,
                reason="waiting_for_image_description",
            )
        return SchedulerTickResult(skipped=True, reason="up_to_date")

    window_start = await _get_scheduler_window_start(
        session,
        enabled_at=phase_6_enabled_at,
        window_end=now,
        source_chat_id=source_chat_id,
    )
    if window_start >= now:
        return SchedulerTickResult(skipped=True, reason="up_to_date")

    try:
        result = await run_extraction_pass(
            session,
            window_start=window_start,
            window_end=now,
            gateway=gateway,
            source_chat_id=source_chat_id,
            durable_session_factory=durable_session_factory,
            selection_mode="version_cursor",
            cursor_start_message_version_id=cursor_start,
            cursor_end_message_version_id=cursor_end,
            _expected_retry_of_run_id=(
                reconciled_retry.id if reconciled_retry is not None else None
            ),
        )
    except ReconciledExtractionSnapshotChangedError as exc:
        logger.error(
            "extraction_scheduler_reconciled_snapshot_changed",
            extra={
                "source_chat_id": source_chat_id,
                "extraction_run_id": str(exc.extraction_run_id),
            },
        )
        return SchedulerTickResult(
            skipped=True,
            reason="reconciled_source_snapshot_changed",
        )
    return SchedulerTickResult(skipped=False, extraction_result=result)
