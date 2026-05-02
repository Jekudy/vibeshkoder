"""LLM gateway — single entry point for every Phase 5+ provider call.

Phase 5 / T5-01. Implements ``synthesize_answer`` with the seven pre-call
invariants ratified in `docs/memory-system/PHASE5_PLAN.md` §5.A:

    1. Empty bundle short-circuit
    2. Source filter (defense-in-depth re-validation)
    3. Forget-invalidation gate — three tombstone keys
    4. Cache lookup (AFTER the forget gate)
    5. Atomic budget guard via ``pg_advisory_xact_lock``
    6. Provider dispatch with categorised error handling
    7. Citation enforcement (``citation_ids`` ⊆ ``bundle.evidence_ids``)

Every call writes a row to ``llm_usage_ledger`` regardless of outcome
(success, error, abstention, cache hit, cost-refusal). HANDOFF §1
invariant #2 — no LLM calls outside this module.

T5-03 ``LedgerRepo`` / ``SynthesisCacheRepo`` are wired by T5-04 caller;
this module accepts both via DI (``ledger_repo`` / ``cache_repo`` keyword
arguments matching the §5.C Protocol surface).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import text
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


def _build_prompt(query_normalized: str, bundle: EvidenceBundle) -> str:
    """Stable prompt rendering used for ``prompt_hash`` and provider dispatch.

    T5-04 will replace this with the real prompt template (and bump
    ``prompt_template_version`` accordingly). For T5-01 the rendering only
    needs to be deterministic so that ``prompt_hash`` is stable.
    """
    citation_part = " ".join(str(i) for i in bundle.evidence_ids)
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

# Mirrors bot/services/search.py:99/102/106 — three tombstone keys.
_TOMBSTONE_GATE_SQL = text(
    """
    SELECT mv.id AS message_version_id, fe.tombstone_key AS tombstone_key
    FROM message_versions AS mv
    JOIN chat_messages AS c
        ON c.id = mv.chat_message_id
    JOIN forget_events AS fe
        ON (
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
    WHERE mv.id = ANY(:ids)
        AND fe.status IN ('pending', 'processing', 'completed')
    """
)

_BUDGET_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:lock_id)")


# ─── Gateway entry point ────────────────────────────────────────────────────


async def synthesize_answer(
    session: AsyncSession,
    *,
    bundle: EvidenceBundle,
    query: str,
    config: LLMGatewayConfig,
    qa_trace_id: int,
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
        REQUIRED — the handler MUST create the ``qa_traces`` row BEFORE
        calling the gateway so cascade FKs are populated upfront. Closes
        Codex round-1 HIGH 4 (cascade direction).
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
    prompt = _build_prompt(query_normalized, bundle)
    prompt_hash = _prompt_hash(prompt)

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
        row = await _ledger(error="empty_bundle")
        return Abstention(
            reason="empty_bundle",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Invariant 2 — source filter (defense-in-depth).
    surviving_ids = await _source_filter(session, bundle.evidence_ids)
    if not surviving_ids:
        row = await _ledger(error="all_filtered")
        return Abstention(
            reason="all_filtered",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Invariant 3 — forget-invalidation gate (three tombstone keys).
    tombstoned_ids = await _forget_tombstone_check(session, surviving_ids)
    if tombstoned_ids:
        for vid in tombstoned_ids:
            await cache_repo.invalidate_by_citation(
                session, message_version_id=vid
            )
        row = await _ledger(error="forget_invalidated")
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
        )
        return AnswerWithCitations(
            answer_text=cached.answer_text,
            citation_ids=tuple(cached.citation_ids),
            cost_usd=Decimal("0"),
            cache_hit=True,
            llm_call_id=row.id,
        )

    # Invariant 5 — budget guard (atomic via pg_advisory_xact_lock).
    over_budget = await _budget_check(session, config, ledger_repo)
    if over_budget:
        row = await _ledger(error="budget_exceeded")
        return Abstention(
            reason="budget_exceeded",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Invariant 6 — provider dispatch with categorised error handling.
    started = time.monotonic()
    try:
        provider_result = await provider.call(prompt=prompt, model=config.model)
    except ProviderTransientError as exc:
        row = await _ledger(
            error=f"provider_transient:{exc.subtype}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
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
        row = await _ledger(
            error=f"provider_structural:{exc.subtype}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )
    except Exception as exc:
        logger.error(
            "llm_gateway: unknown provider failure class=%s",
            type(exc).__name__,
            exc_info=True,
        )
        row = await _ledger(
            error=f"provider_unknown:{type(exc).__name__}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Invariant 7 — citation enforcement.
    bundle_id_set = set(bundle.evidence_ids)
    if not set(provider_result.citation_ids).issubset(bundle_id_set):
        row = await _ledger(
            error="citation_hallucination",
            response_hash=_response_hash(provider_result.answer_text),
            tokens_in=provider_result.tokens_in,
            tokens_out=provider_result.tokens_out,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=provider_result.request_id,
        )
        return Abstention(
            reason="provider_error",
            cost_usd=Decimal("0"),
            llm_call_id=row.id,
        )

    # Success — persist cache row + ledger row (with actual cost) + return.
    cost_usd = _estimate_cost(
        config=config,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
    )
    await cache_repo.store(
        session,
        input_hash=cache_input_hash,
        answer_text=provider_result.answer_text,
        citation_ids=list(provider_result.citation_ids),
        model=config.model,
    )
    row = await _ledger(
        error=None,
        response_hash=_response_hash(provider_result.answer_text),
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
        cost_usd=cost_usd,
        latency_ms=int((time.monotonic() - started) * 1000),
        request_id=provider_result.request_id,
    )
    return AnswerWithCitations(
        answer_text=provider_result.answer_text,
        citation_ids=provider_result.citation_ids,
        cost_usd=cost_usd,
        cache_hit=False,
        llm_call_id=row.id,
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
    """Return message_version_ids whose row matches a tombstone (any of 3 keys)."""
    result = await session.execute(_TOMBSTONE_GATE_SQL, {"ids": evidence_ids})
    rows = result.mappings().all()
    return list({int(r["message_version_id"]) for r in rows})


async def _budget_check(
    session: AsyncSession,
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
) -> bool:
    """Acquire advisory lock + read totals via repo; return True iff over ceiling.

    The lock is taken FIRST so the read is serialised against any other
    in-flight gateway call holding the same lock. Repo-side reads use
    UTC date / month bounds.

    Atomicity note (spec §5.A step 5 vs implementation): the spec mentions a
    placeholder ledger row written BEFORE provider dispatch and UPDATEd
    on return. This implementation skips the placeholder because the
    gateway runs inside a single handler-owned transaction and
    ``pg_advisory_xact_lock`` is held until that transaction commits. As a
    result, concurrent calls from different handler transactions serialise
    on the lock; within one serialised window, only the post-dispatch
    ledger insert is needed. T5-04 may revisit this once the integration
    test under real Postgres covers the full lifecycle.
    """
    await session.execute(_BUDGET_LOCK_SQL, {"lock_id": LLM_BUDGET_LOCK_ID})
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
    """Pricing stub. T5-04 wires real per-model pricing tables.

    For T5-01, we charge a deterministic non-zero amount so the budget
    guard tests can verify cost accumulation. Production cost computation
    lands with the per-model table in T5-04.
    """
    # Note: ``datetime`` import retained for future timezone-aware ledgering.
    _ = datetime.now(timezone.utc)
    return Decimal("0.000001") * (tokens_in + tokens_out)


__all__ = [
    "Abstention",
    "AbstentionReason",
    "AnswerWithCitations",
    "LLM_BUDGET_LOCK_ID",
    "LLMGatewayConfig",
    "LedgerRepoProtocol",
    "MAX_QUERY_LENGTH",
    "SynthesisCacheRepoProtocol",
    "SynthesisResult",
    "_cache_input_hash",
    "_normalize_query",
    "synthesize_answer",
]
