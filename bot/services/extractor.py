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

import logging
import uuid as _uuid_module
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    ExtractionCandidate,
    ExtractionRun,
)
from bot.db.repos.feature_flag import FeatureFlagRepo

logger = logging.getLogger(__name__)


# ─── Feature flag key (PHASE6_PLAN.md §7 T6-02) ──────────────────────────────


MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG = "memory.extraction.scheduler.enabled"


# ─── Protocol seam — concrete impl lands in T6-03 ────────────────────────────


class ExtractCandidatesGateway(Protocol):
    """Typed contract for the Phase 6 LLM gateway extraction entry point.

    T6-02 ships only the seam; T6-03 (Stream B follow-up) provides the
    concrete implementation under ``bot/services/llm_gateway.py::
    extract_candidates``. Tests inject fakes that satisfy this Protocol.

    The gateway is the ONLY allowed LLM call site for extraction (HANDOFF
    invariant #2: "no LLM calls outside ``llm_gateway``"). The extractor
    builds a privacy-cleared evidence bundle, hands it off, and persists
    whatever candidates the gateway emits — no raw provider SDK use.
    """

    async def extract_candidates(
        self,
        session: Any,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
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


@dataclass(frozen=True)
class SchedulerTickResult:
    """Outcome of one scheduler tick (`extraction_scheduler_tick`)."""

    skipped: bool
    extraction_result: ExtractionResult | None = None
    reason: str | None = None


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
    created_at: datetime

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
    force_include_chat_message_ids: list[int] | None,
) -> list[_SourceRow]:
    """SELECT chat_messages + current message_versions that pass governance.

    Filters (PHASE6_PLAN.md §5.B):

    * ``chat_messages.memory_policy = 'normal'``
    * ``chat_messages.is_redacted = FALSE``
    * ``message_versions.is_redacted = FALSE``
    * ``chat_messages.current_version_id = message_versions.id`` (current only)
    * ``chat_messages.created_at >= window_start AND < window_end``
    * NOT matched by any ``forget_events`` row (defense via NOT EXISTS).

    ``force_include_chat_message_ids`` is a test-only escape hatch that
    re-INCLUDES rows by ``chat_messages.id`` even if they'd otherwise be
    filtered out. Production callers MUST NOT set it — the
    ``run_extraction_pass`` invariant guard catches the leakage in that
    case and records the run as failed.
    """
    base_predicate = """
        c.current_version_id = mv.id
        AND c.memory_policy = 'normal'
        AND c.is_redacted = FALSE
        AND mv.is_redacted = FALSE
        AND c.created_at >= :window_start
        AND c.created_at < :window_end
        AND NOT EXISTS (
            SELECT 1
            FROM forget_events AS fe
            WHERE (
                fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
                OR (
                    c.content_hash IS NOT NULL
                    AND fe.tombstone_key = 'message_hash:' || c.content_hash
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
        stmt = text(
            f"""
            SELECT
                c.id AS chat_message_id,
                mv.id AS message_version_id,
                c.chat_id AS chat_id,
                c.message_id AS message_id,
                c.user_id AS user_id,
                mv.text AS text,
                mv.caption AS caption,
                mv.normalized_text AS normalized_text,
                c.memory_policy AS memory_policy,
                c.is_redacted AS is_redacted,
                mv.is_redacted AS version_is_redacted,
                c.created_at AS created_at
            FROM chat_messages AS c
            JOIN message_versions AS mv ON mv.chat_message_id = c.id
            WHERE c.id = ANY(:force_ids)
              AND mv.id = c.current_version_id
            UNION
            SELECT
                c.id AS chat_message_id,
                mv.id AS message_version_id,
                c.chat_id AS chat_id,
                c.message_id AS message_id,
                c.user_id AS user_id,
                mv.text AS text,
                mv.caption AS caption,
                mv.normalized_text AS normalized_text,
                c.memory_policy AS memory_policy,
                c.is_redacted AS is_redacted,
                mv.is_redacted AS version_is_redacted,
                c.created_at AS created_at
            FROM chat_messages AS c
            JOIN message_versions AS mv ON mv.chat_message_id = c.id
            WHERE {base_predicate}
            ORDER BY created_at ASC, message_version_id ASC
            """
        )
        params = {
            "window_start": window_start,
            "window_end": window_end,
            "force_ids": force_include_chat_message_ids,
        }
    else:
        stmt = text(
            f"""
            SELECT
                c.id AS chat_message_id,
                mv.id AS message_version_id,
                c.chat_id AS chat_id,
                c.message_id AS message_id,
                c.user_id AS user_id,
                mv.text AS text,
                mv.caption AS caption,
                mv.normalized_text AS normalized_text,
                c.memory_policy AS memory_policy,
                c.is_redacted AS is_redacted,
                mv.is_redacted AS version_is_redacted,
                c.created_at AS created_at
            FROM chat_messages AS c
            JOIN message_versions AS mv ON mv.chat_message_id = c.id
            WHERE {base_predicate}
            ORDER BY c.created_at ASC, mv.id ASC
            """
        )
        params = {"window_start": window_start, "window_end": window_end}

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
            created_at=row["created_at"],
        )
        for row in result.mappings().all()
    ]


def _bundle_is_clean(rows: list[_SourceRow]) -> tuple[bool, str | None]:
    """Defense-in-depth re-check after SELECT.

    Returns ``(True, None)`` when every row is eligible. Otherwise
    ``(False, <reason>)`` with a structured-log-friendly reason string.

    PHASE6_PLAN.md §5.B "Invariant: the LLM call is forbidden whenever ANY
    source row in the evidence bundle has memory_policy != 'normal'. The
    guard runs BEFORE llm_gateway.extract_candidates() is invoked."
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
    return True, None


async def _persist_failed_run(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    failure_reason: str,
) -> ExtractionResult:
    """Insert a ``run_status='failed'`` ExtractionRun row and return."""
    run = ExtractionRun(
        ingestion_window_start=window_start,
        ingestion_window_end=window_end,
        candidate_count=0,
        run_status="failed",
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


async def run_extraction_pass(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    gateway: ExtractCandidatesGateway,
    operator_user_id: int | None = None,
    prompt_template_version: str = "v0.1.0",
    _force_include_chat_message_ids: list[int] | None = None,
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

    ``operator_user_id`` is opaque audit metadata stored only in structured
    logs for now (no dedicated column on ``extraction_runs`` per
    PHASE6_PLAN.md §5.A migration spec).

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
        force_include_chat_message_ids=_force_include_chat_message_ids,
    )

    clean, reason = _bundle_is_clean(rows)
    if not clean:
        return await _persist_failed_run(
            session,
            window_start=window_start,
            window_end=window_end,
            failure_reason=reason or "governance_violation",
        )

    if not rows:
        run = ExtractionRun(
            ingestion_window_start=window_start,
            ingestion_window_end=window_end,
            candidate_count=0,
            run_status="completed",
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
    gateway_result = await gateway.extract_candidates(
        session,
        source_versions=source_payload,
        prompt_template_version=prompt_template_version,
    )
    candidates_raw = gateway_result.get("candidates", []) or []
    llm_usage_ledger_id = gateway_result.get("llm_usage_ledger_id")

    candidate_objs: list[ExtractionCandidate] = []
    for c in candidates_raw:
        candidate_objs.append(
            ExtractionCandidate(
                candidate_json=dict(c.get("candidate_json") or {}),
                source_message_version_ids=list(
                    c.get("source_message_version_ids") or []
                ),
                status="pending",
            )
        )

    run = ExtractionRun(
        ingestion_window_start=window_start,
        ingestion_window_end=window_end,
        candidate_count=len(candidate_objs),
        run_status="completed",
        llm_usage_ledger_id=llm_usage_ledger_id,
    )
    session.add(run)
    await session.flush()

    for cand in candidate_objs:
        cand.extraction_run_id = run.id
        session.add(cand)
    if candidate_objs:
        await session.flush()

    logger.info(
        "extraction_pass_completed",
        extra={
            "extraction_run_id": str(run.id),
            "candidate_count": len(candidate_objs),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "operator_user_id": operator_user_id,
            "llm_usage_ledger_id": llm_usage_ledger_id,
        },
    )

    return ExtractionResult(
        extraction_run_id=run.id,
        run_status="completed",
        candidate_count=len(candidate_objs),
        llm_usage_ledger_id=llm_usage_ledger_id,
    )


async def _get_phase_6_enabled_at(session: AsyncSession) -> datetime | None:
    """Return the ``updated_at`` of the scheduler flag row.

    This is the forward-only lower bound (PHASE6_PLAN.md §5.B Q5). Missing
    row → no phase_6_enabled_at and the tick must skip (the flag is OFF
    by default).
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


async def extraction_scheduler_tick(
    session: AsyncSession,
    *,
    gateway: ExtractCandidatesGateway,
    now: datetime | None = None,
) -> SchedulerTickResult:
    """Scheduler entry-point — reads the flag, runs the pass if enabled.

    PHASE6_PLAN.md §4 + §7 T6-02 acceptance:

    * Default (flag missing OR False) → return ``skipped=True`` without
      side effects.
    * Flag True → call ``run_extraction_pass`` with
      ``window_start = phase_6_enabled_at`` (the flag row's ``updated_at``)
      and ``window_end = now`` (UTC).

    The admin handler `/admin/extract` calls ``run_extraction_pass``
    directly and bypasses this entry-point (no flag check, explicit window).
    """
    if not await FeatureFlagRepo.get(
        session, MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG
    ):
        return SchedulerTickResult(skipped=True, reason="flag_disabled")

    phase_6_enabled_at = await _get_phase_6_enabled_at(session)
    if phase_6_enabled_at is None:
        # Flag.get says enabled, but row vanished — defensive skip.
        return SchedulerTickResult(skipped=True, reason="flag_row_missing")

    if now is None:
        now = datetime.now(tz=phase_6_enabled_at.tzinfo)

    result = await run_extraction_pass(
        session,
        window_start=phase_6_enabled_at,
        window_end=now,
        gateway=gateway,
    )
    return SchedulerTickResult(skipped=False, extraction_result=result)
