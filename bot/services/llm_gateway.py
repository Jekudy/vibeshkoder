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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle
from bot.services.llm_providers import (
    LLMProvider,
    ProviderStructuralError,
    ProviderTransientError,
)

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
    ) -> Any:
        ...

    async def daily_cost_usd(self, session: Any, *, day: Any) -> Decimal:
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

    Returns ``{"candidates": [...], "llm_usage_ledger_id": int | None}``
    matching the ``ExtractCandidatesGateway`` Protocol surface
    (``bot/services/extractor.py``).

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

    Failure semantics (alignment with T6-02 invariant #4):

    * Empty input → SHORT-CIRCUIT: no provider call, no ledger row,
      returns ``llm_usage_ledger_id=None``. This is the asymmetry T6-02
      handles — "only runs that actually invoked the gateway need a
      ledger row".
    * All other paths write at least a placeholder ledger row that the
      extractor's invariant guard sees as non-None.
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
        )
    finally:
        await session.execute(
            _BUDGET_UNLOCK_SESSION_SQL, {"lock_id": LLM_BUDGET_LOCK_ID}
        )

    # Invariant 6 — provider dispatch with categorised error handling.
    # ALL exceptions are caught + translated into ledger error fields so
    # the SAVEPOINT in ``run_extraction_pass`` stays clean and the
    # extractor's invariant #4 (non-None ledger_id) holds.
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
        return {"candidates": [], "llm_usage_ledger_id": placeholder_row.id}
    except ProviderStructuralError as exc:
        logger.error(
            "llm_gateway: extraction structural provider failure subtype=%s",
            exc.subtype,
            exc_info=True,
        )
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
        return {"candidates": [], "llm_usage_ledger_id": placeholder_row.id}
    except Exception as exc:
        logger.error(
            "llm_gateway: extraction unknown provider failure class=%s",
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
        return {"candidates": [], "llm_usage_ledger_id": placeholder_row.id}

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


__all__ = [
    "Abstention",
    "AbstentionReason",
    "AnswerWithCitations",
    "DEFAULT_PROMPT_TEMPLATE_VERSION",
    "LLM_BUDGET_LOCK_ID",
    "LLMGatewayConfig",
    "LedgerRepoProtocol",
    "LiveExtractCandidatesGateway",
    "MAX_QUERY_LENGTH",
    "SynthesisCacheRepoProtocol",
    "SynthesisResult",
    "_cache_input_hash",
    "_normalize_query",
    "extract_candidates",
    "load_gateway_config",
    "resolve_provider",
    "synthesize_answer",
]
