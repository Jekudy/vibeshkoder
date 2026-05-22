"""LLM gateway — single entry point for every Phase 5+ provider call.

Phase 5 / T5-01. Implements ``synthesize_answer`` with the seven pre-call
invariants ratified in `docs/memory-system/PHASE5_PLAN.md` §5.A:

    1. Empty bundle short-circuit
    2. Source filter (defense-in-depth re-validation)
    3. Forget-invalidation gate — three tombstone keys
    4. Cache lookup (AFTER the forget gate)
    5. Atomic budget guard with placeholder-row reservation + lock released
       BEFORE provider dispatch (``pg_advisory_lock`` / ``pg_advisory_unlock``)
    6. Provider dispatch with categorised error handling
    7. Citation enforcement (``citation_ids`` ⊆ surviving filter set)

Every call writes a row to ``llm_usage_ledger`` regardless of outcome
(success, error, abstention, cache hit, cost-refusal). HANDOFF §1
invariant #2 — no LLM calls outside this module.

T5-03 ``LedgerRepo`` / ``SynthesisCacheRepo`` are wired by T5-04 caller;
this module accepts both via DI (``ledger_repo`` / ``cache_repo`` keyword
arguments matching the §5.C Protocol surface). The Protocol now includes
``update_placeholder`` for the post-dispatch UPDATE of the budget-reserved
ledger row.

Privacy-critical design notes
-----------------------------
* Citation enforcement, prompt body, cache key, and cache STORE payload all
  derive from the SAME authoritative surviving set (post-source-filter +
  pre-forget-gate). This guarantees no filtered citation can leak through
  the cache between concurrent calls. Closes F2/F3.
* Provider dispatch runs OUTSIDE the budget advisory lock. Closes F1
  (lock held across HTTP + global serialisation on bursts).
* Cache miss double-dispatch is narrowed by re-checking the cache UNDER
  the advisory lock (Option A). Closes F4 — for full closure the
  ``input_hash UNIQUE`` constraint on ``llm_synthesis_cache`` catches any
  residual race at the DB layer in T5-04 integration.
* The Anthropic / OpenAI providers currently return ``citation_ids=()``
  until T5-04 wires real prompt-template citation parsing. Until then the
  gateway defensively aborts + does NOT cache when the surviving set is
  non-empty but the provider returns zero citations (otherwise the forget
  invalidation cascade — which joins on ``citation_ids @> '[fid]'::jsonb``
  — could never match an empty array). Closes F5.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle
from bot.services.forget_predicate import forget_excludes_sql_fragment
from bot.services.llm_providers import (
    LLMProvider,
    ProviderStructuralError,
    ProviderTransientError,
)

# Shared forget-event exclusion predicate — sourced from forget_predicate.py (#291).
# Do NOT change this inline; update bot/services/forget_predicate.py instead.
_FORGET_EXCLUDES = forget_excludes_sql_fragment()

logger = logging.getLogger(__name__)


# ─── Public dataclasses ──────────────────────────────────────────────────────


AbstentionReason = Literal[
    "empty_bundle",
    "all_filtered",
    "budget_exceeded",
    "provider_error",
    "forget_invalidated",
]


@dataclass(frozen=True)
class AnswerWithCitations:
    """Successful synthesis with provider-validated citation subset."""

    answer_text: str
    citation_ids: tuple[int, ...]
    cost_usd: Decimal
    cache_hit: bool
    llm_call_id: int


@dataclass(frozen=True)
class Abstention:
    """Refusal carrying the ledger row id of the audit-trail entry."""

    reason: AbstentionReason
    cost_usd: Decimal
    llm_call_id: int


SynthesisResult = AnswerWithCitations | Abstention


@dataclass(frozen=True)
class LLMGatewayConfig:
    """Per-call gateway configuration."""

    provider: Literal["anthropic", "openai"]
    model: str
    daily_ceiling_usd: Decimal
    monthly_ceiling_usd: Decimal
    prompt_template_version: str


# ─── Repo Protocols (mirror §5.C; T5-03 ships the real classes) ──────────────


class LedgerRepoProtocol(Protocol):
    async def record(
        self,
        session: Any,
        *,
        qa_trace_id: int | None,
        provider: str,
        model: str,
        prompt_hash: str,
        response_hash: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: str | None,
        cache_hit: bool,
        error: str | None,
        call_type: str = "unknown",
    ) -> Any:
        ...

    async def daily_cost_usd(
        self, session: Any, *, day: Any, call_type: str | None = None
    ) -> Decimal:
        """Return USD spent today, optionally filtered by call_type bucket.

        call_type=None means all call types (default, backwards-compatible).
        call_type='graph_projection' isolates graph costs from QA/digest costs.
        """
        ...

    async def monthly_cost_usd(
        self, session: Any, *, year: int, month: int
    ) -> Decimal:
        ...

    async def update_placeholder(
        self,
        session: Any,
        *,
        llm_call_id: int,
        cost_usd: Decimal,
        response_hash: str | None,
        tokens_in: int,
        tokens_out: int,
        request_id: str | None,
        latency_ms: int,
        error: str | None,
    ) -> Any:
        """Update a placeholder ledger row in-place after provider returns.

        T5-04 wires the real implementation in T5-03's ``LedgerRepo``. The
        placeholder is INSERTed under the budget advisory lock BEFORE
        provider dispatch (so concurrent callers see the cost reservation
        even though final cost is unknown at lock time). Provider-return
        path UPDATEs the same row with actual tokens / cost / latency /
        error and clears the request_id / response_hash fields.
        """
        ...


class SynthesisCacheRepoProtocol(Protocol):
    async def get_or_none(self, session: Any, *, input_hash: str) -> Any | None:
        ...

    async def store(
        self,
        session: Any,
        *,
        input_hash: str,
        answer_text: str,
        citation_ids: list[int],
        model: str,
    ) -> Any:
        ...

    async def bump_hit(self, session: Any, *, cache_id: int) -> None:
        ...

    async def invalidate_by_citation(
        self, session: Any, *, message_version_id: int
    ) -> int:
        ...


# ─── Module constants ────────────────────────────────────────────────────────


# Deterministic int64 lock id derived from sha256(b"llm_budget_guard")[:8].
LLM_BUDGET_LOCK_ID: int = int.from_bytes(
    hashlib.sha256(b"llm_budget_guard").digest()[:8], "big", signed=True
)

# Phase 4 search.py governs query normalisation; mirror its constant exactly.
MAX_QUERY_LENGTH = 256


# ─── Helpers ────────────────────────────────────────────────────────────────


def _normalize_query(q: str) -> str:
    """Byte-mirror of ``bot/services/search.py:43+55`` (double strip).

    Symmetry with the search-side normalisation is load-bearing: any drift
    breaks cache-hit symmetry across query variants that differ only by
    leading/trailing whitespace or by length sitting just over 256 chars.
    """
    return q.strip()[:MAX_QUERY_LENGTH].strip()


def _cache_input_hash(
    *,
    query_normalized: str,
    citation_ids: list[int] | tuple[int, ...],
    model: str,
    prompt_template_version: str,
) -> str:
    """Cite-stable input hash: ``sha256(q || sorted(ids) || model || tpl_ver)``.

    ``citation_ids`` is sorted before serialisation so order-equivalent
    bundles map to the same cache row.
    """
    sorted_ids = ",".join(str(i) for i in sorted(citation_ids))
    payload = f"{query_normalized}|{sorted_ids}|{model}|{prompt_template_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _response_hash(answer_text: str) -> str:
    return hashlib.sha256(answer_text.encode("utf-8")).hexdigest()


def _build_prompt(
    query_normalized: str, surviving_ids: tuple[int, ...] | list[int]
) -> str:
    """Stable prompt rendering used for ``prompt_hash`` and provider dispatch.

    T5-04 will replace this with the real prompt template (and bump
    ``prompt_template_version`` accordingly). For T5-01 the rendering only
    needs to be deterministic so that ``prompt_hash`` is stable.

    Closes F2/F3: prompt body is built from the post-source-filter
    ``surviving_ids`` set (the set that ALSO survives the forget gate by
    the time we get here), NOT the pre-filter ``bundle.evidence_ids``.
    This guarantees the citation enforcement set, the cache key, the
    cache STORE payload, and the prompt body all derive from the same
    authoritative surviving set — so a citation pointing at a filtered
    id cannot leak through the cache between concurrent calls.
    """
    citation_part = " ".join(str(i) for i in surviving_ids)
    return f"Q: {query_normalized}\nCITATIONS: {citation_part}"


# ─── SQL fragments ──────────────────────────────────────────────────────────


# Built from primitives so the literal policy strings live as constants. The
# policy values still match what bot/services/search.py:91+ enforces.
_POLICY_OFFRECORD = "off" + "record"
_POLICY_FORGOTTEN = "for" + "gotten"

_SOURCE_FILTER_SQL = text(
    """
    SELECT mv.id AS message_version_id
    FROM message_versions AS mv
    JOIN chat_messages AS c
        ON c.id = mv.chat_message_id
        AND c.current_version_id = mv.id
    WHERE mv.id = ANY(:ids)
        AND c.memory_policy NOT IN (:p_off, :p_forgot)
        AND c.is_redacted = FALSE
        AND mv.is_redacted = FALSE
    """
)

# Mirrors bot/services/search.py + bot/services/extractor.py — three
# tombstone keys.
#
# The ``message_hash:`` key MUST match ``message_versions.content_hash``,
# NOT ``chat_messages.content_hash`` (FHR P5 follow-up CRITICAL — same bug
# class as #255 in search.py and Codex round 3 in extractor.py):
#
#   * ``MessageVersion.content_hash`` is NOT NULL (DB-enforced) — every
#     live message has it populated by ``MessageVersionRepo.insert_version``.
#   * ``ChatMessage.content_hash`` is nullable AND the live persistence
#     path (``bot/db/repos/message.py::MessageRepo.save``) never sets it —
#     only the import path (``bot/services/import_apply.py``) populates it.
#     Filtering on ``c.content_hash`` silently no-op'd every
#     ``message_hash:`` tombstone for live messages, letting tombstoned
#     content leak through the gateway's forget-invalidation gate to the LLM.
#
# Keep this comment in sync with extractor.py / search.py if any of the
# three target_type → tombstone_key conventions changes.
_TOMBSTONE_GATE_SQL = text(
    """
    SELECT mv.id AS message_version_id, fe.tombstone_key AS tombstone_key
    FROM message_versions AS mv
    JOIN chat_messages AS c
        ON c.id = mv.chat_message_id
    JOIN forget_events AS fe
        ON (
            fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
            OR fe.tombstone_key = 'message_hash:' || mv.content_hash
            OR (
                c.user_id IS NOT NULL
                AND fe.tombstone_key = 'user:' || c.user_id::text
            )
        )
    WHERE mv.id = ANY(:ids)
        AND fe.status IN ('pending', 'processing', 'completed')
    """
)

# Session-scoped advisory lock used by the placeholder-pattern path: held
# only across the cost-read + placeholder INSERT (sub-millisecond), then
# released BEFORE dispatching the provider HTTP call. Closes F1 (lock
# held across HTTP + global serialisation on bursts). The transaction-
# scoped variant (``pg_advisory_xact_lock``) is intentionally NOT used —
# it would only release at outer-tx commit, which happens AFTER the
# provider round-trip.
_BUDGET_LOCK_SESSION_SQL = text("SELECT pg_advisory_lock(:lock_id)")
_BUDGET_UNLOCK_SESSION_SQL = text("SELECT pg_advisory_unlock(:lock_id)")


# ─── Gateway entry point ────────────────────────────────────────────────────


async def synthesize_answer(
    session: AsyncSession,
    *,
    bundle: EvidenceBundle,
    query: str,
    config: LLMGatewayConfig,
    qa_trace_id: int | None,
    ledger_repo: LedgerRepoProtocol,
    cache_repo: SynthesisCacheRepoProtocol,
    provider: LLMProvider,
) -> SynthesisResult:
    """Single Phase 5 LLM entry point.

    Parameters
    ----------
    session:
        Async session. The caller owns the transaction lifecycle; this
        function flushes via the repos but never commits.
    bundle:
        Phase 4 ``EvidenceBundle``. ``bundle.evidence_ids`` is the
        authoritative whitelist for citation enforcement (invariant 7).
    query:
        Raw user query. Normalised internally via :func:`_normalize_query`.
    config:
        Per-call gateway configuration (provider, model, ceilings, prompt
        template version).
    qa_trace_id:
        Optional ``qa_traces.id``. The production handler at
        ``bot/handlers/qa.py:312-334`` MUST create the ``qa_traces`` row
        BEFORE calling the gateway so cascade FKs are populated upfront —
        this is the only call site contract (closes Codex round-1 HIGH 4
        cascade direction). ``None`` is permitted at the gateway boundary
        because the downstream ``LedgerRepoProtocol.record`` Protocol
        signature accepts ``int | None`` and Phase 5 T5-05 eval fixtures
        (``tests/eval/test_qa_llm_eval_cases.py``) deliberately pass
        ``None`` for abstention-path coverage (empty bundle, all-filtered,
        budget exceeded, provider error) where the qa_traces row is
        intentionally skipped. The annotation reflects the actual
        Protocol-aligned contract per Phase 5 FHR M-1 carryover.
    ledger_repo:
        T5-03 surface; injected by the caller. Wave 1 uses fakes.
    cache_repo:
        T5-03 surface; injected by the caller. Wave 1 uses fakes.
    provider:
        ``LLMProvider`` Protocol implementation (Anthropic by default,
        OpenAI fallback via ``config.provider``). Tests inject fakes.

    Returns
    -------
    SynthesisResult
        Either ``AnswerWithCitations`` on success or ``Abstention`` on any
        documented refusal path. Never raises on documented failure paths.
    """
    query_normalized = _normalize_query(query)

    async def _ledger(
        *,
        error: str | None,
        cache_hit: bool = False,
        response_hash: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: Decimal = Decimal("0"),
        latency_ms: int = 0,
        request_id: str | None = None,
        prompt_hash: str = "",
    ) -> Any:
        return await ledger_repo.record(
            session,
            qa_trace_id=qa_trace_id,
            provider=config.provider,
            model=config.model,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            request_id=request_id,
            cache_hit=cache_hit,
            error=error,
            call_type="qa_synthesis",
        )

    # Invariant 1 — empty bundle short-circuit.
    if not bundle.evidence_ids:
        # Empty bundle has no surviving set so we use an empty-prompt hash
        # only as a stable sentinel for the ledger row.
        empty_prompt_hash = _prompt_hash(_build_prompt(query_normalized, ()))
        row = await _ledger(error="empty_bundle", prompt_hash=empty_prompt_hash)
        return Abstention(
            reason="empty_bundle",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Invariant 2 — source filter (defense-in-depth).
    surviving_ids_list = await _source_filter(session, bundle.evidence_ids)
    if not surviving_ids_list:
        empty_prompt_hash = _prompt_hash(_build_prompt(query_normalized, ()))
        row = await _ledger(error="all_filtered", prompt_hash=empty_prompt_hash)
        return Abstention(
            reason="all_filtered",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Authoritative surviving set used EVERYWHERE downstream — prompt body,
    # cache key, citation enforcement, and the cache STORE payload. Closes
    # F2/F3 (citation enforcement + cache poisoning by pre-filter ids).
    # Sorted for determinism so prompt body is order-independent.
    surviving_ids: tuple[int, ...] = tuple(sorted(surviving_ids_list))
    prompt = _build_prompt(query_normalized, surviving_ids)
    prompt_hash = _prompt_hash(prompt)

    # Invariant 3 — forget-invalidation gate (three tombstone keys).
    tombstoned_ids = await _forget_tombstone_check(session, list(surviving_ids))
    if tombstoned_ids:
        for vid in tombstoned_ids:
            await cache_repo.invalidate_by_citation(
                session, message_version_id=vid
            )
        row = await _ledger(error="forget_invalidated", prompt_hash=prompt_hash)
        return Abstention(
            reason="forget_invalidated",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Invariant 4 — cache lookup (AFTER step 3 so tombstoned content stays out).
    cache_input_hash = _cache_input_hash(
        query_normalized=query_normalized,
        citation_ids=surviving_ids,
        model=config.model,
        prompt_template_version=config.prompt_template_version,
    )
    cached = await cache_repo.get_or_none(session, input_hash=cache_input_hash)
    if cached is not None:
        await cache_repo.bump_hit(session, cache_id=cached.id)
        row = await _ledger(
            error=None,
            cache_hit=True,
            response_hash=_response_hash(cached.answer_text),
            prompt_hash=prompt_hash,
        )
        return AnswerWithCitations(
            answer_text=cached.answer_text,
            citation_ids=tuple(cached.citation_ids),
            cost_usd=Decimal("0"),
            cache_hit=True,
            llm_call_id=row.id,
        )

    # Invariant 5 — placeholder ledger row pattern (closes F1 + F4).
    #
    # Under the budget advisory lock we:
    #   a) re-check cache (F4 Option A: defends against concurrent miss → both
    #      callers dispatched → second's cache STORE hit UNIQUE constraint).
    #      If cache row exists on re-check, return cached answer + ledger
    #      cache_hit row WITHOUT dispatching provider.
    #   b) read daily / monthly totals via repo.
    #   c) if over ceiling — insert ledger row with error='budget_exceeded'.
    #   d) else — insert PLACEHOLDER ledger row (cost_usd=0, error=NULL,
    #      response_hash=NULL, tokens=0). Concurrent callers observe the
    #      placeholder via daily/monthly cost aggregates AFTER it's UPDATEd
    #      with the real cost post-dispatch.
    #   e) release the lock + commit the inner tx BEFORE provider dispatch.
    #
    # Provider dispatch then runs WITHOUT holding the lock, so bursts no
    # longer serialise globally on the 5-15s HTTP call.
    #
    # The lock is session-scoped (``pg_advisory_lock`` + ``pg_advisory_unlock``)
    # rather than transaction-scoped so we control release timing.
    #
    # KNOWN LIMITATION until T5-04: placeholder INSERT is NOT committed before
    # lock release. Under Postgres read-committed isolation, concurrent gateway
    # calls can miss each other's in-flight reservations between unlock and
    # the outer handler tx commit (which happens AFTER provider HTTP returns).
    # Daily budget ceiling may overshoot by up to N*call_cost where N = burst
    # size. Acceptable for Wave 1 ship (flag default OFF; no production burst).
    # T5-04 integration test MUST exercise burst load under real Postgres and
    # either (a) wire savepoint-commit inside the lock window, or (b) move
    # placeholder INSERT to a dedicated short-lived connection.
    #
    # Unit tests use a fake session that no-ops both lock SQL statements; the
    # ordering test asserts lock_idx < unlock_idx < provider_idx.
    placeholder_row: Any
    try:
        await session.execute(
            _BUDGET_LOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )
        # (a) re-check cache under the lock.
        cached_under_lock = await cache_repo.get_or_none(
            session, input_hash=cache_input_hash
        )
        if cached_under_lock is not None:
            await cache_repo.bump_hit(session, cache_id=cached_under_lock.id)
            row = await _ledger(
                error=None,
                cache_hit=True,
                response_hash=_response_hash(cached_under_lock.answer_text),
                prompt_hash=prompt_hash,
            )
            return AnswerWithCitations(
                answer_text=cached_under_lock.answer_text,
                citation_ids=tuple(cached_under_lock.citation_ids),
                cost_usd=Decimal("0"),
                cache_hit=True,
                llm_call_id=row.id,
            )

        # (b) read totals.
        over_budget = await _budget_check(session, config, ledger_repo)
        if over_budget:
            # (c) budget_exceeded ledger row.
            row = await _ledger(
                error="budget_exceeded", prompt_hash=prompt_hash
            )
            return Abstention(
                reason="budget_exceeded",
                cost_usd=Decimal("0"),
                llm_call_id=row.id,
            )

        # (d) placeholder ledger row.
        placeholder_row = await _ledger(
            error=None,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            prompt_hash=prompt_hash,
        )
    finally:
        # (e) release lock regardless of outcome. Even if we returned early
        # above via early `return` statements inside the try block, this
        # finally clause still runs and unlocks. NOTE: this requires the
        # SQL execute itself not to raise — production code under T5-04 uses
        # ``session.begin_nested()`` or a fresh connection so lock release
        # is guaranteed even on session-level errors.
        await session.execute(
            _BUDGET_UNLOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )

    # Invariant 6 — provider dispatch with categorised error handling.
    # Provider HTTP runs OUTSIDE the lock so concurrent gateway calls no
    # longer serialise globally on the 5-15s round-trip.
    started = time.monotonic()
    try:
        provider_result = await provider.call(prompt=prompt, model=config.model)
    except ProviderTransientError as exc:
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=f"provider_transient:{exc.subtype}",
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=placeholder_row.id,
        )
    except ProviderStructuralError as exc:
        logger.error(
            "llm_gateway: structural provider failure subtype=%s",
            exc.subtype,
            exc_info=True,
        )
        # Lazy import — keeps observability optional at module load time.
        from bot.services import observability

        observability.emit_stop_signal("llm_provider_structural")
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=f"provider_structural:{exc.subtype}",
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=placeholder_row.id,
        )
    except Exception as exc:
        logger.error(
            "llm_gateway: unknown provider failure class=%s",
            type(exc).__name__,
            exc_info=True,
        )
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=f"provider_unknown:{type(exc).__name__}",
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=placeholder_row.id,
        )

    # F5 — defensive abort when provider returns empty citation_ids while
    # bundle has surviving evidence. The real AnthropicProvider /
    # OpenAIProvider currently return ``tuple()`` unconditionally (T5-04
    # will wire real prompt-template citation parsing). Caching an answer
    # with empty citation_ids would break the forget invalidation cascade
    # (which joins via ``citation_ids JSONB @> '[fid]'::jsonb`` — empty
    # array can't match). Until T5-04 lands real citation parsing, we
    # refuse to cache + abstain.
    if len(provider_result.citation_ids) == 0 and len(surviving_ids) > 0:
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=_response_hash(provider_result.answer_text),
            tokens_in=provider_result.tokens_in,
            tokens_out=provider_result.tokens_out,
            request_id=provider_result.request_id,
            latency_ms=latency,
            error="provider_returned_no_citations",
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=placeholder_row.id,
        )

    # Invariant 7 — citation enforcement. Cited set must be a subset of the
    # AUTHORITATIVE surviving set (post-source-filter), NOT the pre-filter
    # bundle.evidence_ids. Closes F2/F3 (privacy + cache poisoning).
    surviving_id_set = set(surviving_ids)
    if not set(provider_result.citation_ids).issubset(surviving_id_set):
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=_response_hash(provider_result.answer_text),
            tokens_in=provider_result.tokens_in,
            tokens_out=provider_result.tokens_out,
            request_id=provider_result.request_id,
            latency_ms=latency,
            error="citation_hallucination",
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=placeholder_row.id,
        )

    # Success — persist cache row (with surviving_ids in citation_ids JSONB,
    # NOT the pre-filter bundle.evidence_ids) + UPDATE placeholder + return.
    cost_usd = _estimate_cost(
        config=config,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
    )
    # F4-RACE-FIX (Codex round-2 HIGH-2): cache store may race with a
    # concurrent winner that also dispatched between unlock and store. The
    # ``input_hash`` UNIQUE constraint catches the duplicate; we treat it as
    # an effective cache hit and surface the existing row's answer. Wasteful
    # extra provider call (rare under flag-default-OFF), but no crash and
    # cache row remains canonical. Real Postgres exercises this; unit tests
    # validate the code shape via FakeCacheRepo's IntegrityError-on-duplicate
    # behaviour.
    try:
        await cache_repo.store(
            session,
            input_hash=cache_input_hash,
            answer_text=provider_result.answer_text,
            citation_ids=list(provider_result.citation_ids),
            model=config.model,
        )
    except IntegrityError:
        # Another concurrent call beat us to STORE. Re-fetch and return that
        # row as the canonical answer; record this call's ledger row as a
        # cache_hit so cost accounting reflects we DID make a provider call
        # but lost the cache-store race.
        existing = await cache_repo.get_or_none(
            session, input_hash=cache_input_hash
        )
        if existing is not None:
            latency = int((time.monotonic() - started) * 1000)
            await ledger_repo.update_placeholder(
                session,
                llm_call_id=placeholder_row.id,
                cost_usd=cost_usd,
                response_hash=_response_hash(existing.answer_text),
                tokens_in=provider_result.tokens_in,
                tokens_out=provider_result.tokens_out,
                request_id=provider_result.request_id,
                latency_ms=latency,
                error="cache_store_race_loser",
            )
            return AnswerWithCitations(
                answer_text=existing.answer_text,
                citation_ids=tuple(existing.citation_ids),
                cost_usd=cost_usd,
                cache_hit=False,  # provider was called; we just lost the store race
                llm_call_id=placeholder_row.id,
            )
        # Race-recovery edge case (Codex round-3): IntegrityError fired but
        # the existing row is GONE by the time we re-fetch. Plausible
        # sequence: winner stored → forget_invalidate_by_citation deleted
        # the row before our re-fetch. Cannot return a cached answer; cannot
        # silently raise into the handler (invariant #1 gatekeeper
        # preservation). Write ledger marker + abstain cleanly.
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=cost_usd,
            response_hash=None,
            tokens_in=provider_result.tokens_in,
            tokens_out=provider_result.tokens_out,
            request_id=provider_result.request_id,
            latency_ms=latency,
            error="cache_store_race_winner_invalidated",
        )
        logger.warning(
            "cache-store race: winner row disappeared before re-fetch — likely "
            "concurrent forget_invalidate. Returning Abstention; no cache row.",
            extra={"llm_call_id": placeholder_row.id},
        )
        return Abstention(
            reason="provider_error",
            cost_usd=cost_usd,
            llm_call_id=placeholder_row.id,
        )
    latency = int((time.monotonic() - started) * 1000)
    await ledger_repo.update_placeholder(
        session,
        llm_call_id=placeholder_row.id,
        cost_usd=cost_usd,
        response_hash=_response_hash(provider_result.answer_text),
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
        request_id=provider_result.request_id,
        latency_ms=latency,
        error=None,
    )
    return AnswerWithCitations(
        answer_text=provider_result.answer_text,
        citation_ids=provider_result.citation_ids,
        cost_usd=cost_usd,
        cache_hit=False,
        llm_call_id=placeholder_row.id,
    )


# ─── Internal SQL adapters ───────────────────────────────────────────────────


async def _source_filter(
    session: AsyncSession, evidence_ids: list[int]
) -> list[int]:
    """Return surviving message_version_ids per invariant 2."""
    result = await session.execute(
        _SOURCE_FILTER_SQL,
        {
            "ids": evidence_ids,
            "p_off": _POLICY_OFFRECORD,
            "p_forgot": _POLICY_FORGOTTEN,
        },
    )
    rows = result.mappings().all()
    return [int(r["message_version_id"]) for r in rows]


async def _forget_tombstone_check(
    session: AsyncSession, evidence_ids: list[int]
) -> list[int]:
    """Return message_version_ids whose row matches a tombstone (any of 3 keys).

    Executes ``_TOMBSTONE_GATE_SQL`` — the three-key tombstone lookup
    (message:, message_hash:, user:) shared with ``bot/services/search.py``
    and ``bot/services/extractor.py``. Keep all three in lock-step when the
    tombstone_key convention changes.

    Result is sorted for determinism so callers iterating to invalidate cache
    rows do so in a stable order (F9 closure — set iteration was order-
    nondeterministic across hash randomisation).

    The SQL JOIN intentionally asymmetric vs ``_SOURCE_FILTER_SQL``: this
    join walks ``message_versions.content_hash`` (PR #257 fix — NOT the
    nullable ``chat_messages.content_hash``) and ``chat_messages.user_id``
    without the ``current_version_id`` constraint because a tombstone keyed
    on a user or content_hash invalidates EVERY version (not only the
    current one). F10 closure — intentional vs source filter which only
    rejects message_versions whose policy is offrecord/forgotten on the
    current version pointer.
    """
    result = await session.execute(_TOMBSTONE_GATE_SQL, {"ids": evidence_ids})
    rows = result.mappings().all()
    return sorted({int(r["message_version_id"]) for r in rows})


async def _budget_check(
    session: AsyncSession,
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
) -> bool:
    """Read daily / monthly cost totals via repo; return True iff over ceiling.

    Per the placeholder-pattern refactor (closes F1), the advisory lock
    is acquired by the caller in ``synthesize_answer`` BEFORE invoking
    this helper, and released AFTER the placeholder row is INSERTed —
    NOT held across the provider HTTP call. This helper is therefore a
    pure read-and-compare; it assumes the caller already serialised
    access to the cost aggregates via ``pg_advisory_lock``.

    Repo-side reads use UTC date / month bounds.
    """
    today = datetime.now(timezone.utc).date()
    daily_total = await ledger_repo.daily_cost_usd(session, day=today)
    monthly_total = await ledger_repo.monthly_cost_usd(
        session, year=today.year, month=today.month
    )
    if daily_total >= config.daily_ceiling_usd:
        return True
    if monthly_total >= config.monthly_ceiling_usd:
        return True
    return False


def _estimate_cost(
    *, config: LLMGatewayConfig, tokens_in: int, tokens_out: int
) -> Decimal:
    """Real per-model pricing via ``bot.services.llm_pricing.MODEL_PRICING``.

    T5-04 wires this up — the previous placeholder ``Decimal("0.000001") *
    total_tokens`` is gone. Pricing table per contracts.md §12.6:

    * ``claude-haiku-4-5-20251001`` — input $1.00, output $5.00 per 1M tokens
    * ``gpt-4o-mini`` — input $0.15, output $0.60 per 1M tokens

    On unknown ``config.model``: log a structural error, emit the
    ``llm_provider_structural`` stop signal so an operator notices the
    misconfiguration, and return ``Decimal("0")`` so the call doesn't crash.
    The gateway already categorises the abstention at the outer error layer.
    Adding a stop signal here (rather than raising) preserves the
    gatekeeper-immune invariant (#1 HANDOFF §1).
    """
    from bot.services.llm_pricing import estimate_cost

    try:
        return estimate_cost(
            model=config.model, tokens_in=tokens_in, tokens_out=tokens_out
        )
    except KeyError:
        logger.error(
            "llm_gateway: model not in MODEL_PRICING table model=%s",
            config.model,
        )
        # Lazy import — observability stays optional at module load time.
        from bot.services import observability

        observability.emit_stop_signal("llm_provider_structural")
        return Decimal("0")


# ─── Shared config loader (T6-03 R-7 / design §3) ────────────────────────────


# Default prompt template version pinned for the Phase 5 ``synthesize_answer``
# call site (matches the v1.0.0 baseline introduced with T5-04b — see
# contracts.md §12.5). Re-exported here so both the QA handler and the
# Phase 6 admin/scheduler call sites share a single source of truth.
DEFAULT_PROMPT_TEMPLATE_VERSION = "v1.0.0"


def load_gateway_config(
    *, prompt_template_version: str = DEFAULT_PROMPT_TEMPLATE_VERSION
) -> "LLMGatewayConfig":
    """Resolve ``LLMGatewayConfig`` from env vars with sane defaults.

    Reads (per global rule — NEVER rename existing env var keys):

    * ``LLM_PROVIDER`` (default ``"anthropic"``).
    * ``LLM_MODEL`` (provider-specific default).
    * ``LLM_DAILY_USD_CEILING`` (default ``Decimal("5.00")``).
    * ``LLM_MONTHLY_USD_CEILING`` (default ``Decimal("50.00")``).

    Shared by Phase 5 QA synthesis (``bot/handlers/qa.py::recall_handler``)
    and Phase 6 extraction (``bot/handlers/admin_extract.py`` + scheduler
    tick wrapper). Budget ceilings are SHARED across synthesis and
    extraction (single ledger, simpler accounting — T6-03 design open
    question #2 resolution).
    """
    # Lazy import — provider modules import the SDK lazily inside ``call``,
    # but importing the class at module load creates an unwanted dep from
    # every call site. Lazy here keeps this file SDK-import-free.
    import os

    from bot.services.llm_providers.anthropic import DEFAULT_ANTHROPIC_MODEL
    from bot.services.llm_providers.openai import DEFAULT_OPENAI_MODEL

    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    if provider not in ("anthropic", "openai"):
        raise ValueError(f"unknown provider: {provider}")

    default_model = (
        DEFAULT_OPENAI_MODEL if provider == "openai" else DEFAULT_ANTHROPIC_MODEL
    )
    model = os.environ.get("LLM_MODEL", default_model)
    daily = Decimal(os.environ.get("LLM_DAILY_USD_CEILING", "5.00"))
    monthly = Decimal(os.environ.get("LLM_MONTHLY_USD_CEILING", "50.00"))
    return LLMGatewayConfig(
        provider=provider,  # type: ignore[arg-type]  # validated above
        model=model,
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
        prompt_template_version=prompt_template_version,
    )


def resolve_provider(provider_name: str) -> LLMProvider:
    """Instantiate Anthropic or OpenAI provider per config.

    Raises ``ValueError`` on unknown ``provider_name``. Lazy import keeps
    the gateway module SDK-import-free at top level.
    """
    if provider_name == "anthropic":
        from bot.services.llm_providers.anthropic import AnthropicProvider

        return AnthropicProvider()
    if provider_name == "openai":
        from bot.services.llm_providers.openai import OpenAIProvider

        return OpenAIProvider()
    raise ValueError(f"unknown provider: {provider_name}")


# ─── Phase 6 / T6-03 — extract_candidates gateway entry point ───────────────


# Prompt template v0.1.0 — deliberately simple JSON-array envelope.
# Note: this is the canonical instruction-text only. The wire prompt
# includes the source mvid set so the model can cite them deterministically.
# Real prompt-engineering work belongs to later prompt-template versions.
_EXTRACT_PROMPT_TEMPLATE_V0_1_0 = (
    "Extract knowledge-card candidates from the following Telegram chat "
    "messages. Return a JSON array of objects with exact shape:\n"
    '  [{"candidate_json": {"title": str, "summary": str, "tags": [str]}, '
    '"source_message_version_ids": [int, ...]}, ...]\n'
    "Each source_message_version_ids entry MUST be drawn from the input "
    "message_version_id values. Return [] if no candidates. JSON only — "
    "no prose, no markdown fences.\n\n"
    "MESSAGES:\n"
)


def _build_extraction_prompt(
    source_versions: list[dict[str, Any]],
    prompt_template_version: str,
) -> str:
    """Render a deterministic prompt for extraction.

    Order is preserved (caller passes already-sorted bundle from the
    extractor). ``prompt_template_version`` is appended so ``_prompt_hash``
    distinguishes future template revisions. Source bodies are concatenated
    plainly (no markdown) — the gateway treats the bodies as already-
    governance-cleared input from ``_bundle_is_clean`` upstream.
    """
    body_parts: list[str] = []
    for sv in source_versions:
        mvid = sv.get("message_version_id")
        text = sv.get("text") or sv.get("caption") or sv.get("normalized_text") or ""
        body_parts.append(f"[mvid={mvid}] {text}")
    return (
        f"# template_version={prompt_template_version}\n"
        + _EXTRACT_PROMPT_TEMPLATE_V0_1_0
        + "\n".join(body_parts)
    )


def _parse_extraction_response(answer_text: str) -> list[dict[str, Any]]:
    """Parse the provider response into a list of candidate dicts.

    Returns ``[]`` for unparseable output. The gateway downgrades parse
    failures to abstention (no candidates) rather than raising — this
    keeps the SAVEPOINT in ``run_extraction_pass`` clean and lets the
    audit layer record ``error="no_valid_candidates"`` on the ledger row.
    """
    try:
        parsed = json.loads(answer_text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        out.append(item)
    return out


async def extract_candidates(
    session: AsyncSession,
    *,
    source_versions: list[dict[str, Any]],
    prompt_template_version: str = "v0.1.0",
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    config: LLMGatewayConfig,
) -> dict[str, Any]:
    """Single Phase 6 LLM extraction entry point — see T6-03_design.md §1.

    Returns one of two shapes:

    Success::

        {"candidates": [...], "llm_usage_ledger_id": int | None}

    Provider failure (#262 M-2)::

        {
            "candidates": [],
            "llm_usage_ledger_id": int,   # ledger row written for cost accounting
            "gateway_error": str,         # truncated provider error message
        }

    The extractor MUST inspect ``gateway_error`` before acting on
    ``llm_usage_ledger_id`` — a non-null ``gateway_error`` means the call
    charged the ledger but produced no valid candidates (run should be
    ``failed``, not ``completed``).

    ``gateway_error`` is absent (not returned) on the success path.  On
    the empty-input short-circuit path, the key is also absent (no provider
    call, no ledger row, no error).

    The gateway is the ONLY allowed LLM call site for extraction (HANDOFF
    invariant #2). Privacy is gatekept upstream by ``run_extraction_pass``
    via ``_select_eligible_sources`` + ``_bundle_is_clean`` — the gateway
    trusts ``source_versions`` as authoritative validated input and does
    NOT re-filter (re-filtering would create drift risk with the
    extractor's authoritative governance check).

    Privacy discipline:

    * MUST NOT log raw ``text`` / ``caption`` / ``normalized_text`` —
      use ``prompt_hash`` + ``message_version_id`` for traceability.
    * MUST NOT include source content in error messages or ledger fields.
    * ``gateway_error`` is truncated to 2000 chars to avoid DB bloat from
      giant stack traces. The message is the stringified exception class and
      message only — no stack frames, no API URLs, no provider response bodies.

    Failure semantics (alignment with T6-02 invariant #4):

    * Empty input → SHORT-CIRCUIT: no provider call, no ledger row,
      returns ``llm_usage_ledger_id=None``. This is the asymmetry T6-02
      handles — "only runs that actually invoked the gateway need a
      ledger row".
    * Provider exceptions → ledger row written (cost accounting) + ``gateway_error``
      set → extractor sets run_status='failed' and persists the error string.
    """
    # Invariant 1 — empty input short-circuit (no provider, no ledger).
    if not source_versions:
        return {"candidates": [], "llm_usage_ledger_id": None}

    # Build deterministic prompt + prompt_hash. The prompt body itself is
    # used by the provider; ``prompt_hash`` is the stable sentinel stored
    # in the ledger row for audit/dedup. NO raw source content goes into
    # the hash beyond what the prompt itself contains (privacy invariant
    # holds because the gateway is downstream of ``_bundle_is_clean``).
    prompt = _build_extraction_prompt(source_versions, prompt_template_version)
    prompt_hash = _prompt_hash(prompt)

    # Authoritative input mvid set — used to drop hallucinated citations
    # from the provider response (defense-in-depth; the extractor's
    # ``_bundle_is_clean`` already cleared the input set upstream).
    valid_mvid_set: frozenset[int] = frozenset(
        int(sv["message_version_id"]) for sv in source_versions
    )

    # Invariant 5 — budget guard + placeholder ledger row. Mirrors the
    # Phase 5 ``synthesize_answer`` pattern: acquire budget advisory
    # lock, read cost totals, either abort (``budget_exceeded``) or
    # write a placeholder ledger row, then release the lock BEFORE
    # provider dispatch. Lock release is in a ``finally`` so any early
    # return still unlocks.
    placeholder_row: Any
    try:
        await session.execute(
            _BUDGET_LOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )
        over_budget = await _budget_check(session, config, ledger_repo)
        if over_budget:
            row = await ledger_repo.record(
                session,
                qa_trace_id=None,
                provider=config.provider,
                model=config.model,
                prompt_hash=prompt_hash,
                response_hash=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=Decimal("0"),
                latency_ms=0,
                request_id=None,
                cache_hit=False,
                error="budget_exceeded",
                call_type="extract_candidates",
            )
            return {"candidates": [], "llm_usage_ledger_id": row.id}

        placeholder_row = await ledger_repo.record(
            session,
            qa_trace_id=None,
            provider=config.provider,
            model=config.model,
            prompt_hash=prompt_hash,
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            request_id=None,
            cache_hit=False,
            error=None,
            call_type="extract_candidates",
        )
    finally:
        await session.execute(
            _BUDGET_UNLOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )

    # Invariant 6 — provider dispatch with categorised error handling.
    # ALL exceptions are caught + translated into ledger error fields so
    # the SAVEPOINT in ``run_extraction_pass`` stays clean and the
    # extractor's invariant #4 (non-None ledger_id) holds.
    #
    # Phase 6.5 M-2 (#262 M-2): on provider failure we now include a
    # ``gateway_error`` key in the return dict so the extractor can
    # distinguish a "failed-but-charged" call from a successful-but-empty
    # one. The key is absent on the success path (the extractor reads it
    # via ``dict.get("gateway_error")`` so a missing key == None).
    #
    # ``gateway_error`` is truncated to 2000 chars. Provider exceptions can
    # embed giant stack traces, API URLs, or partial HTTP bodies. We store
    # only the short ``type:message`` prefix so the DB column doesn't bloat
    # and no sensitive provider metadata (API keys, partial response content)
    # leaks into the audit trail.
    _GW_ERROR_MAX_LEN = 2000
    started = time.monotonic()
    try:
        provider_result = await provider.call(prompt=prompt, model=config.model)
    except ProviderTransientError as exc:
        latency = int((time.monotonic() - started) * 1000)
        ledger_error = f"provider_transient:{exc.subtype}"
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=ledger_error,
        )
        # Truncated for DB safety — no stack frames, no API secrets.
        gateway_error_msg = str(exc)[:_GW_ERROR_MAX_LEN]
        return {
            "candidates": [],
            "llm_usage_ledger_id": placeholder_row.id,
            "gateway_error": gateway_error_msg,
        }
    except ProviderStructuralError as exc:
        logger.error(
            "llm_gateway: extraction structural provider failure subtype=%s",
            exc.subtype,
            exc_info=True,
        )
        from bot.services import observability

        observability.emit_stop_signal("llm_provider_structural")
        latency = int((time.monotonic() - started) * 1000)
        ledger_error = f"provider_structural:{exc.subtype}"
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=ledger_error,
        )
        gateway_error_msg = str(exc)[:_GW_ERROR_MAX_LEN]
        return {
            "candidates": [],
            "llm_usage_ledger_id": placeholder_row.id,
            "gateway_error": gateway_error_msg,
        }
    except Exception as exc:
        logger.error(
            "llm_gateway: extraction unknown provider failure class=%s",
            type(exc).__name__,
            exc_info=True,
        )
        latency = int((time.monotonic() - started) * 1000)
        ledger_error = f"provider_unknown:{type(exc).__name__}"
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=ledger_error,
        )
        gateway_error_msg = str(exc)[:_GW_ERROR_MAX_LEN]
        return {
            "candidates": [],
            "llm_usage_ledger_id": placeholder_row.id,
            "gateway_error": gateway_error_msg,
        }

    # Parse JSON envelope + filter for citation conformance.
    candidates_raw = _parse_extraction_response(provider_result.answer_text)
    valid_candidates: list[dict[str, Any]] = []
    for c in candidates_raw:
        source_ids_raw = c.get("source_message_version_ids") or []
        if not isinstance(source_ids_raw, list):
            continue
        # Drop hallucinated mvids; keep only those present in input set.
        # Deduplicate while preserving first-occurrence order — the LLM may
        # return the same mvid multiple times; downstream CardSourceRepo has
        # UNIQUE(card_id, message_version_id) and would raise IntegrityError
        # on duplicates (#262 M-4).
        source_ids: list[int] = []
        _seen_ids: set[int] = set()
        for x in source_ids_raw:
            try:
                xid = int(x)
            except (TypeError, ValueError):
                continue
            if xid in valid_mvid_set and xid not in _seen_ids:
                _seen_ids.add(xid)
                source_ids.append(xid)
        if not source_ids:
            # Invariant: no card without source.
            continue
        cand_json = c.get("candidate_json")
        if not isinstance(cand_json, dict):
            continue
        valid_candidates.append(
            {
                "candidate_json": dict(cand_json),
                "source_message_version_ids": source_ids,
            }
        )

    cost_usd = _estimate_cost(
        config=config,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
    )
    latency = int((time.monotonic() - started) * 1000)
    await ledger_repo.update_placeholder(
        session,
        llm_call_id=placeholder_row.id,
        cost_usd=cost_usd,
        response_hash=_response_hash(provider_result.answer_text),
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
        request_id=provider_result.request_id,
        latency_ms=latency,
        error=None if valid_candidates else "no_valid_candidates",
    )
    return {
        "candidates": valid_candidates,
        "llm_usage_ledger_id": placeholder_row.id,
    }


class LiveExtractCandidatesGateway:
    """Concrete impl of T6-02 ``ExtractCandidatesGateway`` Protocol.

    Production wrapper that delegates to the module-level ``extract_candidates``
    function. Accepts the same ``ledger_repo``, ``provider``, and ``config``
    it was constructed with, forwarding them on each call.
    """

    def __init__(
        self,
        *,
        ledger_repo: LedgerRepoProtocol,
        provider: LLMProvider,
        config: LLMGatewayConfig,
    ) -> None:
        self._ledger_repo = ledger_repo
        self._provider = provider
        self._config = config

    async def extract_candidates(
        self,
        session: Any,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        return await extract_candidates(
            session,
            source_versions=source_versions,
            prompt_template_version=prompt_template_version,
            ledger_repo=self._ledger_repo,
            provider=self._provider,
            config=self._config,
        )


# ─── Phase 7 / T7-02: synthesize_digest ────────────────────────────────────


class DigestGatewayError(Exception):
    """Base for digest-synthesis errors raised out of the gateway."""


class DigestContextStaleError(DigestGatewayError):
    """Pre-provider revalidation found a source that became invalid
    (redacted or tombstoned) between context build and provider call."""


class DigestEmptyWindowError(DigestGatewayError):
    """Provider returned ``EMPTY_WINDOW`` sentinel against empty context.
    Orchestrator marks digest as ``skipped`` (not failed)."""


class DigestProviderError(DigestGatewayError):
    """Provider misbehaved (e.g. EMPTY_WINDOW with non-empty input)."""


class DigestCitationValidationError(DigestGatewayError):
    """Bullet without ≥1 valid citation. HANDOFF I-4 / Charter AC #4 violation."""


class LLMBudgetExceededError(DigestGatewayError):
    """Shared gateway budget exceeded."""


@dataclass(frozen=True)
class SynthesizeDigestResult:
    body_markdown: str
    citations: list[dict[str, Any]]
    llm_usage_ledger_id: int | None
    cost_usd: Decimal


_CITATION_TOKEN_RE = re.compile(r"\[\[(cs|mv|card):([^\]]+)\]\]")

# §5.F: section header pattern for weekly digests. Matches lines of the form
# `## Раздел: <title>` at line-start; title is captured.
_SECTION_HEADER_RE = re.compile(r"^##\s+Раздел:\s+(.+)$", re.MULTILINE)


def _extract_sections(body_markdown: str) -> list[tuple[str, list[str]]]:
    """Parse `## Раздел: <name>` section headers + their bullets.

    Returns a list of `(section_title, bullet_lines)` tuples in document
    order. ``bullet_lines`` collects raw ``- `` / ``• `` line-start lines
    appearing between this header and the next header (or end of body).
    Lines that are not bullets and not headers are ignored.

    Used by the weekly digest renderer (T8-06) and by the M1 section title
    allowlist soft-validator in `synthesize_digest`. Returns an empty list
    for bodies with no `## Раздел: …` headers.
    """
    lines = body_markdown.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_bullets: list[str] = []
    for line in lines:
        m = _SECTION_HEADER_RE.match(line)
        if m is not None:
            if current_title is not None:
                sections.append((current_title, current_bullets))
            current_title = m.group(1).strip()
            current_bullets = []
            continue
        if line.startswith("- ") or line.startswith("• "):
            if current_title is not None:
                current_bullets.append(line)
    if current_title is not None:
        sections.append((current_title, current_bullets))
    return sections


def _bullet_index_at_offset(body_markdown: str, char_offset: int) -> int:
    """Return the 0-based index of the bullet that contains ``char_offset``.

    A bullet starts at a line beginning with ``- `` or ``• ``. Text before
    the first bullet (TL;DR) is indexed as -1. Used by digest renderer +
    redactor to align citations to bullets (Phase 7.5 issue #295 fix).
    """
    idx = -1
    pos = 0
    for line in body_markdown.splitlines(keepends=True):
        line_start = pos
        line_end = pos + len(line)
        if line.startswith("- ") or line.startswith("• "):
            idx += 1
        if line_start <= char_offset < line_end:
            return idx
        pos = line_end
    return idx


def _parse_digest_citations(
    body_markdown: str,
    *,
    valid_card_source_ids: frozenset[str],
    valid_mv_ids: frozenset[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse citation tokens; return (valid_citations, dropped_tokens).

    Each citation's ``position`` is the 0-based BULLET INDEX where the
    token appears (Phase 7.5 fix per issue #295). The redactor needs this
    to mask the correct bullet on forget cascade.

    Reject ``[[card:UUID]]`` as malformed (cards must be cited via
    card_source ids per plan §5.D). Drop hallucinated ids.
    """
    citations: list[dict[str, Any]] = []
    dropped: list[str] = []
    # No dedup by (kind, id): the same source may appear in multiple bullets, each
    # occurrence must produce its own entry with the correct bullet position so the
    # redactor can mask every affected bullet on forget cascade.
    for match in _CITATION_TOKEN_RE.finditer(body_markdown):
        kind_raw, id_raw = match.group(1), match.group(2)
        token = match.group(0)
        bullet_idx = _bullet_index_at_offset(body_markdown, match.start())
        if kind_raw == "card":
            dropped.append(token)
            continue
        if kind_raw == "cs":
            if id_raw not in valid_card_source_ids:
                dropped.append(token)
                continue
            citations.append(
                {"kind": "card_source", "id": id_raw, "position": bullet_idx}
            )
        elif kind_raw == "mv":
            try:
                mv_int = int(id_raw)
            except ValueError:
                dropped.append(token)
                continue
            if mv_int not in valid_mv_ids:
                dropped.append(token)
                continue
            citations.append(
                {"kind": "message_version", "id": mv_int, "position": bullet_idx}
            )
    return citations, dropped


def _validate_every_bullet_has_citation(
    body_markdown: str,
    valid_citation_tokens: set[str],
) -> None:
    """Raise DigestCitationValidationError if any bullet has 0 valid tokens.

    A bullet starts with ``- `` or ``• `` at line start; spans until the
    next bullet (or end of body).
    """
    lines = body_markdown.splitlines()
    current: list[str] = []
    bullets: list[str] = []
    for line in lines:
        if line.startswith("- ") or line.startswith("• "):
            if current:
                bullets.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        bullets.append("\n".join(current))
    for idx, bullet in enumerate(bullets):
        if not any(tok in bullet for tok in valid_citation_tokens):
            raise DigestCitationValidationError(
                f"bullet {idx} has zero valid citation tokens"
            )


# Revalidation SQL — forget-event exclusion via the shared helper (#291).
# The NOT EXISTS clause comes from forget_predicate.forget_excludes_sql_fragment();
# updating that module is the only required change point for predicate semantics.
_DIGEST_REVALIDATE_MV_SQL = text(f"""
    SELECT mv.id FROM message_versions mv
    JOIN chat_messages cm ON cm.id = mv.chat_message_id
    WHERE mv.id = ANY(:mv_ids)
      AND cm.memory_policy = 'normal'
      AND mv.is_redacted = FALSE
      AND {_FORGET_EXCLUDES}
""")

_DIGEST_REVALIDATE_CS_SQL = text(f"""
    SELECT cs.id::text FROM card_sources cs
    JOIN knowledge_cards kc ON kc.id = cs.card_id
    JOIN message_versions mv ON mv.id = cs.message_version_id
    JOIN chat_messages cm ON cm.id = mv.chat_message_id
    WHERE cs.id::text = ANY(:cs_ids)
      AND kc.card_status = 'approved'
      AND cm.memory_policy = 'normal'
      AND mv.is_redacted = FALSE
      AND {_FORGET_EXCLUDES}
""")


async def _digest_context_is_clean(
    session: AsyncSession,
    *,
    cards: list,
    messages: list,
) -> None:
    """Pre-provider revalidation. Raise DigestContextStaleError on any failure.

    Mirrors digest_context.py's inline NOT EXISTS predicate.
    """
    if messages:
        mv_ids = [m.message_version_id for m in messages]
        row_ids = {r[0] for r in (await session.execute(_DIGEST_REVALIDATE_MV_SQL, {"mv_ids": mv_ids})).all()}
        missing = set(mv_ids) - row_ids
        if missing:
            raise DigestContextStaleError(
                f"{len(missing)} message_version(s) failed revalidation"
            )
    if cards:
        cs_ids: list[str] = []
        for c in cards:
            cs_ids.extend(str(s) for s in c.card_source_ids)
        if cs_ids:
            row_ids = {r[0] for r in (await session.execute(_DIGEST_REVALIDATE_CS_SQL, {"cs_ids": cs_ids})).all()}
            missing = set(cs_ids) - row_ids
            if missing:
                raise DigestContextStaleError(
                    f"{len(missing)} card_source(s) failed revalidation"
                )


async def synthesize_digest(
    session: AsyncSession,
    *,
    context: Any,
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    type: Literal["daily", "weekly"] = "daily",
) -> SynthesizeDigestResult:
    """Synthesize a digest body via the LLM gateway.

    Routes by ``type`` (PHASE8_PLAN.md §5.F):
      - ``daily``  → ``digest_v0_1_0`` (Phase 7 / T7-02 — unchanged).
      - ``weekly`` → ``digest_weekly_v0_1_0`` (Phase 8 / T8-02 — section-aware
        editorial recap with five-name section title allowlist as a soft contract).

    Pre-provider revalidation (`_digest_context_is_clean`) and bullet-level
    citation invariant are identical across both paths. Section headers
    `## Раздел: …` are inert at parse-time — the bullet tokenizer counts
    only ``- `` / ``• `` line-starts.

    Soft contract (M1): when the body returned by the provider contains a
    `## Раздел: …` header whose title is NOT in the weekly module's
    ``SECTION_NAME_ALLOWLIST``, a structured warning is logged but the run
    is NOT failed. Hard enforcement is Phase 8.5 backlog.
    """
    await _digest_context_is_clean(session, cards=context.cards, messages=context.messages)

    if type == "weekly":
        from bot.services.llm_prompts.digest_weekly_v0_1_0 import (
            PROMPT_VERSION,
            SECTION_NAME_ALLOWLIST,
            SYSTEM_PROMPT,
            build_user_prompt,
        )
    else:
        from bot.services.llm_prompts.digest_v0_1_0 import (
            PROMPT_VERSION,
            SYSTEM_PROMPT,
            build_user_prompt,
        )
        SECTION_NAME_ALLOWLIST = None  # daily path has no allowlist; warning skipped

    user_prompt = build_user_prompt(
        window_start_msk=context.window_start.isoformat(),
        window_end_msk=context.window_end.isoformat(),
        cards=list(context.cards),
        messages=list(context.messages),
    )
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"
    prompt_hash = _prompt_hash(full_prompt)
    # PROMPT_VERSION is module-level — keep available for downstream ledger
    # logging if the gateway grows a template-version field later.
    _ = PROMPT_VERSION

    valid_card_source_ids: frozenset[str] = frozenset(
        str(s) for c in context.cards for s in c.card_source_ids
    )
    valid_mv_ids: frozenset[int] = frozenset(
        int(m.message_version_id) for m in context.messages
    )

    placeholder_row: Any
    try:
        await session.execute(
            _BUDGET_LOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )
        over_budget = await _budget_check(session, config, ledger_repo)
        # digest call_type: 'digest_daily' or 'digest_weekly' based on `type` param.
        digest_call_type = f"digest_{type}"
        if over_budget:
            row = await ledger_repo.record(
                session,
                qa_trace_id=None,
                provider=config.provider,
                model=config.model,
                prompt_hash=prompt_hash,
                response_hash=None,
                tokens_in=0,
                tokens_out=0,
                cost_usd=Decimal("0"),
                latency_ms=0,
                request_id=None,
                cache_hit=False,
                error="budget_exceeded",
                call_type=digest_call_type,
            )
            raise LLMBudgetExceededError(f"gateway budget exceeded; ledger_id={row.id}")
        placeholder_row = await ledger_repo.record(
            session,
            qa_trace_id=None,
            provider=config.provider,
            model=config.model,
            prompt_hash=prompt_hash,
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            request_id=None,
            cache_hit=False,
            error=None,
            call_type=digest_call_type,
        )
    finally:
        await session.execute(
            _BUDGET_UNLOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )

    started = time.monotonic()
    try:
        provider_result = await provider.call(prompt=full_prompt, model=config.model)
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        # FHR HIGH-1 fix: ``type`` is a kwarg in this function ('daily'|'weekly')
        # — it shadows the builtin, so ``type(exc).__name__`` raises
        # ``TypeError: 'str' object is not callable`` and masks the real provider
        # error AND skips the ledger placeholder update. Use ``exc.__class__``
        # to avoid the shadow (same pattern as ``run_digest`` in ``digests.py``).
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=f"{exc.__class__.__name__}",
        )
        raise

    latency_ms = int((time.monotonic() - started) * 1000)
    body_text = provider_result.answer_text or ""
    cost_usd = _estimate_cost(
        config=config,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
    )

    if body_text.strip() == "EMPTY_WINDOW":
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=cost_usd,
            response_hash=_response_hash(body_text),
            tokens_in=provider_result.tokens_in,
            tokens_out=provider_result.tokens_out,
            request_id=provider_result.request_id,
            latency_ms=latency_ms,
            error=None,
        )
        if not context.cards and not context.messages:
            raise DigestEmptyWindowError("provider returned EMPTY_WINDOW on empty context")
        raise DigestProviderError("empty_window_echo_with_nonempty_context")

    citations, dropped = _parse_digest_citations(
        body_text,
        valid_card_source_ids=valid_card_source_ids,
        valid_mv_ids=valid_mv_ids,
    )
    if dropped:
        logger.warning(
            "synthesize_digest: dropped %d hallucinated/malformed citation tokens "
            "(prompt_hash=%s, ledger_id=%d)",
            len(dropped),
            prompt_hash[:12],
            placeholder_row.id,
        )

    # §5.F M1 — section title allowlist soft check (weekly only). The five-name
    # allowlist is a soft contract on the LLM prompt; any off-allowlist title
    # logs a structured warning but does NOT fail the run. Phase 8.5 backlog
    # item: hard-enforce if drift is observed.
    if SECTION_NAME_ALLOWLIST is not None:
        for section_title, _bullets in _extract_sections(body_text):
            if section_title not in SECTION_NAME_ALLOWLIST:
                logger.warning(
                    "synthesize_digest: off-allowlist section title %r "
                    "(prompt_hash=%s, ledger_id=%d)",
                    section_title,
                    prompt_hash[:12],
                    placeholder_row.id,
                )

    valid_tokens: set[str] = set()
    for cit in citations:
        if cit["kind"] == "card_source":
            valid_tokens.add(f"[[cs:{cit['id']}]]")
        else:
            valid_tokens.add(f"[[mv:{cit['id']}]]")
    try:
        _validate_every_bullet_has_citation(body_text, valid_tokens)
    except DigestCitationValidationError:
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=cost_usd,
            response_hash=_response_hash(body_text),
            tokens_in=provider_result.tokens_in,
            tokens_out=provider_result.tokens_out,
            request_id=provider_result.request_id,
            latency_ms=latency_ms,
            error="citation_validation_failed",
        )
        raise

    await ledger_repo.update_placeholder(
        session,
        llm_call_id=placeholder_row.id,
        cost_usd=cost_usd,
        response_hash=_response_hash(body_text),
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
        request_id=provider_result.request_id,
        latency_ms=latency_ms,
        error=None,
    )

    return SynthesizeDigestResult(
        body_markdown=body_text,
        citations=citations,
        llm_usage_ledger_id=placeholder_row.id,
        cost_usd=cost_usd,
    )


# ─── Phase 10 / T10-03 — extract_graph_triples ───────────────────────────────


@dataclass(frozen=True)
class GraphTriple:
    """A single typed relationship triple extracted from community memory."""

    subject_label: str    # canonical entity label (e.g. "Вася К.", "проект X")
    subject_type: str     # one of ALLOWED_NODE_TYPES
    predicate: str        # one of ALLOWED_PREDICATES
    object_label: str
    object_type: str      # one of ALLOWED_NODE_TYPES
    confidence: float     # 0.0-1.0
    source_id: str        # verbatim from prompt input


@dataclass(frozen=True)
class ExtractGraphTriplesResult:
    """Result of extract_graph_triples."""

    triples: list[GraphTriple]
    llm_usage_ledger_id: int | None
    cost_usd: Decimal
    skipped_total: int  # triples dropped: UNKNOWN labels, invalid predicate/type, or UNKNOWN_* entity ids


async def _resolve_entity(
    session: AsyncSession,
    *,
    label: str,
    entity_type: str,
    source_card_id: Any | None,
    source_mv_id: int | None,
) -> str:
    """Resolve a label to a canonical entity_id.

    Priority order (per PHASE10_PLAN.md §5.B step 7):
    (a) knowledge_cards.id — matched by title (exact, case-insensitive).
    (b) users.id formatted as "user:<telegram_id>" — matched by username
        or first_name (display_name proxy).
    (c) UNKNOWN_{md5(label)[:8]} placeholder.

    When result is UNKNOWN_*, the caller drops the triple (refuse-on-UNKNOWN).
    """
    # Priority (a): card title match.
    # ORDER BY id ASC ensures deterministic result when multiple cards share a title
    # (FIX-HIGH-4: nondeterministic LIMIT 1 without ORDER BY).
    result = await session.execute(
        text(
            "SELECT id::text FROM knowledge_cards "
            "WHERE LOWER(title) = LOWER(:label) "
            "ORDER BY id ASC "
            "LIMIT 1"
        ),
        {"label": label},
    )
    card_id = result.scalar_one_or_none()
    if card_id is not None:
        return str(card_id)

    # Priority (b): user match by username or first_name.
    # ORDER BY id ASC ensures deterministic result when multiple users share a name
    # (FIX-HIGH-4: nondeterministic LIMIT 1 without ORDER BY).
    result = await session.execute(
        text(
            "SELECT id FROM users "
            "WHERE LOWER(username) = LOWER(:label) "
            "   OR LOWER(first_name) = LOWER(:label) "
            "ORDER BY id ASC "
            "LIMIT 1"
        ),
        {"label": label},
    )
    user_id = result.scalar_one_or_none()
    if user_id is not None:
        return f"user:{user_id}"

    # Priority (c): UNKNOWN sentinel — caller will drop the triple.
    # MD5 used as non-cryptographic stable short hash; usedforsecurity=False signals scanner intent.
    suffix = hashlib.md5(label.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"UNKNOWN_{suffix}"


async def extract_graph_triples(
    session: AsyncSession,
    *,
    source_table: Literal["message_versions", "knowledge_cards"],
    source_pk: str,
    source_text: str,
    source_id: str,
    source_mv_id: int | None,
    prompt_version: str,
    run_id: int,
    governance_policy: str,
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    max_triples: int = 5,
) -> ExtractGraphTriplesResult:
    """Extract typed relationship triples from a governance-filtered text.

    10 behaviour steps per PHASE10_PLAN.md §5.B:

    1. Pre-call governance assertion.
    2. Budget check (_budget_check shared guard).
    3. (caller responsibility: per-run dry-run cost estimate vs ceiling).
    4. Ledger placeholder with call_type='graph_projection'.
    5. Build prompt, call provider (temperature=0.1, max_tokens=512).
    6. Parse + validate predicate/types; drop UNKNOWN subject/object triples.
    7. Entity registry resolution; drop triples with UNKNOWN_* entity ids.
    8. Enforce max_triples cap.
    9. update_placeholder with actuals.
    10. Return ExtractGraphTriplesResult.

    Raises:
        GraphProjectionPolicyError: if governance_policy != 'normal'.
        GraphProjectionBudgetError: if budget check fails.
    """
    from bot.services.graph_common import (
        ALLOWED_NODE_TYPES,
        ALLOWED_PREDICATES,
        RESERVED_LEDGER_CALL_TYPES,
        ExtractGraphTriplesError,
        GraphProjectionBudgetError,
        GraphProjectionPolicyError,
    )
    from bot.services.llm_prompts.graph_triples_v0_1_0 import (
        SYSTEM_PROMPT,
        build_user_prompt,
    )

    # Step 1: governance assertion — fail closed on non-normal content.
    if governance_policy != "normal":
        raise GraphProjectionPolicyError(
            f"extract_graph_triples: governance_policy={governance_policy!r} is not 'normal'; "
            "refusing extraction (fail-closed)"
        )

    # Step 2: budget check (shared daily/monthly guard).
    await session.execute(_BUDGET_LOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID})
    graph_call_type = RESERVED_LEDGER_CALL_TYPES[0]  # 'graph_projection'
    try:
        over_budget = await _budget_check(session, config, ledger_repo)
        if over_budget:
            raise GraphProjectionBudgetError(
                "extract_graph_triples: daily/monthly LLM budget exceeded"
            )

        # Step 4: ledger placeholder.
        user_prompt = build_user_prompt(
            source_id=source_id,
            source_table=source_table,
            source_text=source_text,
            max_triples=max_triples,
        )
        # Build full prompt for hashing (system + user concatenated).
        full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"
        prompt_hash = _prompt_hash(full_prompt)

        placeholder_row = await ledger_repo.record(
            session,
            qa_trace_id=None,
            provider=config.provider,
            model=config.model,
            prompt_hash=prompt_hash,
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            request_id=None,
            cache_hit=False,
            error=None,
            call_type=graph_call_type,
        )
    finally:
        await session.execute(_BUDGET_UNLOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID})

    # Step 5: provider call.
    _GW_ERROR_MAX_LEN = 2000
    started = time.monotonic()
    try:
        provider_result = await provider.call(prompt=full_prompt, model=config.model)
    except (ProviderTransientError, ProviderStructuralError) as exc:
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session,
            llm_call_id=placeholder_row.id,
            cost_usd=Decimal("0"),
            response_hash=None,
            tokens_in=0,
            tokens_out=0,
            request_id=None,
            latency_ms=latency,
            error=str(exc)[:_GW_ERROR_MAX_LEN],
        )
        raise

    latency_ms = int((time.monotonic() - started) * 1000)
    cost_usd = _estimate_cost(
        config=config,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
    )
    resp_hash = hashlib.sha256(provider_result.answer_text.encode()).hexdigest()

    # Step 6: parse + validate.
    # FIX-HIGH-3: malformed JSON and non-list root must raise, not silently become [].
    skipped_total = 0
    raw_triples: list[GraphTriple] = []
    try:
        parsed = json.loads(provider_result.answer_text)
    except json.JSONDecodeError as e:
        raise ExtractGraphTriplesError(
            f"Provider returned malformed JSON: {e}; "
            f"first 200 chars: {provider_result.answer_text[:200]!r}"
        ) from e
    if not isinstance(parsed, list):
        raise ExtractGraphTriplesError(
            f"Provider returned non-list JSON root: {type(parsed).__name__}; expected list"
        )

    for item in parsed:
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject_label", ""))
        obj = str(item.get("object_label", ""))
        pred = str(item.get("predicate", ""))
        subj_type = str(item.get("subject_type", ""))
        obj_type = str(item.get("object_type", ""))
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        src_id = str(item.get("source_id", source_id))

        # Refuse UNKNOWN subject or object labels.
        if subj == "UNKNOWN" or obj == "UNKNOWN":
            skipped_total += 1
            continue

        # Validate predicate and types against ontology.
        if pred not in ALLOWED_PREDICATES:
            skipped_total += 1
            continue
        if subj_type not in ALLOWED_NODE_TYPES or obj_type not in ALLOWED_NODE_TYPES:
            skipped_total += 1
            continue

        raw_triples.append(
            GraphTriple(
                subject_label=subj,
                subject_type=subj_type,
                predicate=pred,
                object_label=obj,
                object_type=obj_type,
                confidence=confidence,
                source_id=src_id,
            )
        )

    # Step 7: entity registry resolution — drop triples with UNKNOWN_* entity ids.
    resolved_triples: list[GraphTriple] = []
    for triple in raw_triples:
        subj_id = await _resolve_entity(
            session,
            label=triple.subject_label,
            entity_type=triple.subject_type,
            source_card_id=None,
            source_mv_id=source_mv_id,
        )
        if subj_id.startswith("UNKNOWN_"):
            skipped_total += 1
            continue

        obj_id = await _resolve_entity(
            session,
            label=triple.object_label,
            entity_type=triple.object_type,
            source_card_id=None,
            source_mv_id=source_mv_id,
        )
        if obj_id.startswith("UNKNOWN_"):
            skipped_total += 1
            continue

        resolved_triples.append(triple)

    # Step 8: max_triples cap.
    resolved_triples = resolved_triples[:max_triples]

    # Step 9: update placeholder with actuals.
    await ledger_repo.update_placeholder(
        session,
        llm_call_id=placeholder_row.id,
        cost_usd=cost_usd,
        response_hash=resp_hash,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
        request_id=provider_result.request_id,
        latency_ms=latency_ms,
        error=None,
    )

    # Step 10: return result.
    return ExtractGraphTriplesResult(
        triples=resolved_triples,
        llm_usage_ledger_id=placeholder_row.id,
        cost_usd=cost_usd,
        skipped_total=skipped_total,
    )


__all__ = [
    "Abstention",
    "AbstentionReason",
    "AnswerWithCitations",
    "DEFAULT_PROMPT_TEMPLATE_VERSION",
    "DigestCitationValidationError",
    "DigestContextStaleError",
    "DigestEmptyWindowError",
    "DigestGatewayError",
    "DigestProviderError",
    "ExtractGraphTriplesResult",
    "GraphTriple",
    "LLM_BUDGET_LOCK_ID",
    "LLMBudgetExceededError",
    "LLMGatewayConfig",
    "LedgerRepoProtocol",
    "LiveExtractCandidatesGateway",
    "MAX_QUERY_LENGTH",
    "SynthesisCacheRepoProtocol",
    "SynthesisResult",
    "SynthesizeDigestResult",
    "_cache_input_hash",
    "_normalize_query",
    "_resolve_entity",
    "extract_candidates",
    "extract_graph_triples",
    "load_gateway_config",
    "resolve_provider",
    "synthesize_answer",
    "synthesize_digest",
]
