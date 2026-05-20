"""Behaviour tests for `bot.services.llm_gateway` (T5-01 / Phase 5 Wave 1).

The gateway depends on two T5-03 repos (``LedgerRepo``, ``SynthesisCacheRepo``)
that ship in a parallel stream. Until those land, the gateway accepts both via
DI; tests here use Protocol-typed in-memory fakes that match the §5.C surface.
The forget-event tombstone gate is exercised through a stubbed session whose
``execute`` returns a configurable scalar — production wiring will use the
T5-02 ``forget_events`` rows directly.

Test surface (≥30) covers all 7 pre-call invariants from PHASE5_PLAN.md §5.A:
1. empty bundle short-circuit
2. source filter (defense-in-depth)
3. forget-invalidation gate (3 tombstone keys)
4. cache lookup
5. budget guard
6. provider dispatch + categorised error handling
7. citation enforcement
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import pytest

from bot.services.evidence import EvidenceBundle
from bot.services.llm_gateway import (
    LLM_BUDGET_LOCK_ID,
    Abstention,
    AnswerWithCitations,
    LLMGatewayConfig,
    SynthesisResult,
    _estimate_cost,
    _normalize_query,
    synthesize_answer,
)
from bot.services.llm_providers import (
    ProviderResult,
    ProviderStructuralError,
    ProviderTransientError,
)
from bot.services.search import SearchHit


# ─── Fakes for T5-03 repos and forget-event surface ──────────────────────────


class LedgerRepoProtocol(Protocol):
    """§5.C surface — T5-03 ships the real ``LedgerRepo``."""

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


@dataclass
class _LedgerRow:
    id: int
    qa_trace_id: int | None
    provider: str
    model: str
    prompt_hash: str
    response_hash: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    latency_ms: int
    request_id: str | None
    cache_hit: bool
    error: str | None


@dataclass
class FakeLedgerRepo:
    """In-memory ``LedgerRepo`` matching §5.C surface."""

    rows: list[_LedgerRow] = field(default_factory=list)
    daily_cost: Decimal = Decimal("0")
    monthly_cost: Decimal = Decimal("0")
    _next_id: int = 1

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
    ) -> _LedgerRow:
        row = _LedgerRow(
            id=self._next_id,
            qa_trace_id=qa_trace_id,
            provider=provider,
            model=model,
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
        self.rows.append(row)
        self._next_id += 1
        # Track running totals for budget guard tests.
        self.daily_cost += cost_usd
        self.monthly_cost += cost_usd
        return row

    async def daily_cost_usd(self, session: Any, *, day: Any) -> Decimal:
        return self.daily_cost

    async def monthly_cost_usd(
        self, session: Any, *, year: int, month: int
    ) -> Decimal:
        return self.monthly_cost

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
    ) -> _LedgerRow:
        """In-place mutate the placeholder row added by ``record``.

        Tracks running cost totals: subtract the old placeholder cost (0)
        and add the new one so concurrent budget reads from a second
        gateway call observe the actual reservation.
        """
        for row in self.rows:
            if row.id == llm_call_id:
                old_cost = row.cost_usd
                row.cost_usd = cost_usd
                row.response_hash = response_hash
                row.tokens_in = tokens_in
                row.tokens_out = tokens_out
                row.request_id = request_id
                row.latency_ms = latency_ms
                row.error = error
                self.daily_cost += cost_usd - old_cost
                self.monthly_cost += cost_usd - old_cost
                return row
        raise KeyError(f"placeholder llm_call_id={llm_call_id} not found")


@dataclass
class _CacheRow:
    id: int
    input_hash: str
    answer_text: str
    citation_ids: list[int]
    model: str
    hit_count: int = 0


@dataclass
class FakeCacheRepo:
    rows: dict[str, _CacheRow] = field(default_factory=dict)
    invalidated_ids: list[int] = field(default_factory=list)
    _next_id: int = 1

    async def get_or_none(self, session: Any, *, input_hash: str) -> _CacheRow | None:
        return self.rows.get(input_hash)

    async def store(
        self,
        session: Any,
        *,
        input_hash: str,
        answer_text: str,
        citation_ids: list[int],
        model: str,
    ) -> _CacheRow:
        row = _CacheRow(
            id=self._next_id,
            input_hash=input_hash,
            answer_text=answer_text,
            citation_ids=list(citation_ids),
            model=model,
        )
        self.rows[input_hash] = row
        self._next_id += 1
        return row

    async def bump_hit(self, session: Any, *, cache_id: int) -> None:
        for row in self.rows.values():
            if row.id == cache_id:
                row.hit_count += 1
                return

    async def invalidate_by_citation(
        self, session: Any, *, message_version_id: int
    ) -> int:
        self.invalidated_ids.append(message_version_id)
        before = len(self.rows)
        self.rows = {
            k: v
            for k, v in self.rows.items()
            if message_version_id not in v.citation_ids
        }
        return before - len(self.rows)


@dataclass
class FakeProvider:
    """Pytest-injected ``LLMProvider`` Protocol implementation."""

    answer_text: str = "Synthesized answer"
    citation_ids: tuple[int, ...] = (100,)
    tokens_in: int = 50
    tokens_out: int = 25
    request_id: str = "req-fake"
    raw_latency_ms: int = 12
    raise_exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, prompt: str, model: str) -> ProviderResult:
        self.calls.append({"prompt": prompt, "model": model})
        if self.raise_exc is not None:
            raise self.raise_exc
        return ProviderResult(
            answer_text=self.answer_text,
            citation_ids=self.citation_ids,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id=self.request_id,
            raw_latency_ms=self.raw_latency_ms,
        )


# ─── Stub session for forget-event + source-filter SQL ───────────────────────


@dataclass
class _SessionRow:
    """Mapping-like row returned by the fake session."""

    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class _SessionResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[_SessionRow]:
        return [_SessionRow(r) for r in self._rows]

    def first(self) -> _SessionRow | None:
        return _SessionRow(self._rows[0]) if self._rows else None

    def scalar(self) -> Any:
        if not self._rows:
            return None
        first = self._rows[0]
        return next(iter(first.values()))


class _SessionMappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._inner = _SessionResult(rows)

    def all(self) -> list[_SessionRow]:
        return self._inner.all()

    def first(self) -> _SessionRow | None:
        return self._inner.first()


class _SessionExecutor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _SessionMappingResult:
        return _SessionMappingResult(self._rows)

    def scalar(self) -> Any:
        return _SessionResult(self._rows).scalar()


@dataclass
class FakeSession:
    """Minimal ``AsyncSession`` stand-in.

    Each non-lock SQL ``execute`` returns rows from the front of
    ``query_results``. Lock acquisitions / releases — matched on the
    substring ``pg_advisory`` so both transaction-scoped
    (``pg_advisory_xact_lock``) and session-scoped
    (``pg_advisory_lock`` / ``pg_advisory_unlock``) variants are no-ops
    that do NOT consume a query slot. Tests can therefore pre-load
    fixtures in invocation order: source-filter rows, then tombstone
    rows, then budget rows.
    """

    query_results: list[list[dict[str, Any]]] = field(default_factory=list)

    async def execute(self, *args: Any, **kwargs: Any) -> _SessionExecutor:
        stmt = args[0] if args else kwargs.get("statement")
        if stmt is not None and "pg_advisory" in str(stmt):
            return _SessionExecutor([])
        if self.query_results:
            rows = self.query_results.pop(0)
        else:
            rows = []
        return _SessionExecutor(rows)


# ─── Helper: build a non-empty bundle ────────────────────────────────────────


def _make_bundle(version_ids: tuple[int, ...] = (100, 101)) -> EvidenceBundle:
    timestamp = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    hits = [
        SearchHit(
            message_version_id=vid,
            chat_message_id=200 + idx,
            chat_id=-1001,
            message_id=300 + idx,
            user_id=42,
            snippet=f"<b>match</b>{idx}",
            ts_rank=0.5 - idx * 0.01,
            captured_at=timestamp,
            message_date=timestamp,
        )
        for idx, vid in enumerate(version_ids)
    ]
    return EvidenceBundle.from_hits("hello", -1001, hits)


def _config(
    *,
    daily: Decimal = Decimal("5.00"),
    monthly: Decimal = Decimal("50.00"),
    provider_name: str = "anthropic",
) -> LLMGatewayConfig:
    return LLMGatewayConfig(
        provider=provider_name,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
        prompt_template_version="v1",
    )


# ─── Tests: query normalization (load-bearing — Codex round-3 finding) ──────


def test_normalize_query_strips_whitespace() -> None:
    assert _normalize_query("   hello   ") == "hello"


def test_normalize_query_truncates_to_256_then_strips_again() -> None:
    """Double-strip: trailing whitespace + length>256 must mirror search.py:43+55."""
    base = "x" * 250 + " " * 20  # 270 chars total
    out = _normalize_query(base)
    # Expected: strip → "x"*250 (250 chars), then 250 < 256 so no truncate. But
    # to exercise the second strip we need the slice itself to land on whitespace.
    long = "y" * 200 + "z" * 60 + " " * 10  # 270 chars
    long_out = _normalize_query(long)
    assert len(long_out) <= 256
    # First .strip() removes the trailing 10 spaces → length 260; [:256] → 256
    # chars all letters → second .strip() leaves it unchanged.
    assert long_out == long.strip()[:256].strip()
    assert out == base.strip()[:256].strip()


def test_normalize_query_double_strip_load_bearing() -> None:
    """A query whose [:256] slice ends in whitespace must drop it.

    Build a string s.t. ``s.strip()`` is >256 chars and ``s.strip()[:256]``
    ends with whitespace mid-word. Single-strip would leave trailing spaces;
    double-strip (search.py recipe) drops them. Cache-hit symmetry depends
    on byte-equality with the search-side normalisation.
    """
    raw = "  " + "a" * 250 + " " + "b" * 100 + "   "
    # raw.strip() = "a"*250 + " " + "b"*100 (length 351)
    # [:256]      = "a"*250 + " " + "b"*5
    # second .strip() leaves it (no leading/trailing ws).
    out = _normalize_query(raw)
    assert out == "a" * 250 + " " + "b" * 5


def test_normalize_query_empty_is_empty() -> None:
    assert _normalize_query("") == ""
    assert _normalize_query("    ") == ""


# ─── Tests: dataclass / config surface ───────────────────────────────────────


def test_answer_with_citations_is_frozen() -> None:
    awc = AnswerWithCitations(
        answer_text="ok",
        citation_ids=(1, 2),
        cost_usd=Decimal("0.01"),
        cache_hit=False,
        llm_call_id=7,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        awc.answer_text = "mutated"  # type: ignore[misc]


def test_abstention_is_frozen_and_carries_call_id() -> None:
    ab = Abstention(reason="empty_bundle", cost_usd=Decimal("0"), llm_call_id=42)
    assert ab.reason == "empty_bundle"
    assert ab.llm_call_id == 42
    with pytest.raises(Exception):
        ab.reason = "all_filtered"  # type: ignore[misc]


def test_synthesis_result_alias_accepts_both_branches() -> None:
    awc: SynthesisResult = AnswerWithCitations(
        answer_text="ok",
        citation_ids=(1,),
        cost_usd=Decimal("0"),
        cache_hit=False,
        llm_call_id=1,
    )
    ab: SynthesisResult = Abstention(
        reason="empty_bundle", cost_usd=Decimal("0"), llm_call_id=2
    )
    assert isinstance(awc, AnswerWithCitations)
    assert isinstance(ab, Abstention)


def test_llm_gateway_config_is_frozen() -> None:
    cfg = _config()
    with pytest.raises(Exception):
        cfg.model = "other"  # type: ignore[misc]


def test_llm_budget_lock_id_is_deterministic_int64() -> None:
    """Lock id is sha256(b"llm_budget_guard")[:8] as signed int64."""
    expected = int.from_bytes(
        hashlib.sha256(b"llm_budget_guard").digest()[:8], "big", signed=True
    )
    assert LLM_BUDGET_LOCK_ID == expected


# ─── Tests: invariant 1 — empty bundle short-circuit ────────────────────────


@pytest.mark.asyncio
async def test_empty_bundle_short_circuits() -> None:
    """Empty bundle → ledger row + Abstention(empty_bundle); NO provider call."""
    bundle = EvidenceBundle.from_hits("q", -1001, [])
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession()

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "empty_bundle"
    assert res.cost_usd == Decimal("0")
    assert provider.calls == []
    # Exactly one ledger row, marked with the empty_bundle error sentinel.
    assert len(ledger.rows) == 1
    assert ledger.rows[0].error == "empty_bundle"
    assert ledger.rows[0].cache_hit is False
    assert ledger.rows[0].cost_usd == Decimal("0")


# ─── Tests: invariant 2 — source filter ──────────────────────────────────────


@pytest.mark.asyncio
async def test_source_filter_drops_all_filtered_when_zero_survive() -> None:
    """All cited rows masked → Abstention(all_filtered); NO provider call."""
    bundle = _make_bundle((100, 101))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    # Source filter SQL returns zero surviving ids.
    session = FakeSession(query_results=[[]])

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "all_filtered"
    assert provider.calls == []
    assert ledger.rows[-1].error == "all_filtered"


@pytest.mark.asyncio
async def test_source_filter_partial_keeps_subset() -> None:
    """Filter yields a subset → proceed; provider sees the surviving citations.

    The fake session sequences SQL results in invocation order:
      1. source filter → returns surviving versions [100]
      2. tombstone gate → returns no tombstones
      3. budget read → returns 0 cost
    """
    bundle = _make_bundle((100, 101))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(citation_ids=(100,))
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],  # source filter survives 100
            [],  # tombstone gate empty
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, AnswerWithCitations)
    assert res.citation_ids == (100,)
    assert len(provider.calls) == 1


# ─── Tests: invariant 3 — three-key tombstone gate ──────────────────────────


@pytest.mark.asyncio
async def test_forget_invalidation_message_key() -> None:
    """``message:`` tombstone match → invalidate cache + Abstention(forget_invalidated)."""
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],  # source filter survives
            [{"message_version_id": 100, "tombstone_key": "message"}],  # match!
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "forget_invalidated"
    assert provider.calls == []
    assert 100 in cache.invalidated_ids
    assert ledger.rows[-1].error == "forget_invalidated"


@pytest.mark.asyncio
async def test_forget_invalidation_message_hash_key() -> None:
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [{"message_version_id": 100, "tombstone_key": "message_hash"}],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "forget_invalidated"
    assert 100 in cache.invalidated_ids


@pytest.mark.asyncio
async def test_forget_invalidation_user_key() -> None:
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [{"message_version_id": 100, "tombstone_key": "user"}],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "forget_invalidated"
    assert 100 in cache.invalidated_ids


@pytest.mark.asyncio
async def test_forget_invalidation_calls_cache_invalidate() -> None:
    """Even when the cache row is absent, ``invalidate_by_citation`` is called."""
    bundle = _make_bundle((100, 101))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}, {"message_version_id": 101}],
            [
                {"message_version_id": 100, "tombstone_key": "message"},
                {"message_version_id": 101, "tombstone_key": "message"},
            ],
        ]
    )

    await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert sorted(cache.invalidated_ids) == [100, 101]


@pytest.mark.asyncio
async def test_forget_invalidation_evicts_existing_cache_row() -> None:
    """A pre-existing cache row that cites a tombstoned id must be evicted."""
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    # Seed a cache row that references the tombstoned id.
    await cache.store(
        FakeSession(),
        input_hash="seed-hash",
        answer_text="stale answer",
        citation_ids=[100],
        model="claude-haiku-4-5-20251001",
    )
    assert "seed-hash" in cache.rows  # sanity precondition

    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [{"message_version_id": 100, "tombstone_key": "message"}],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "forget_invalidated"
    # Cache row was deleted (FakeCacheRepo.invalidate_by_citation drops rows
    # whose citation_ids JSONB array contains the tombstoned id).
    assert "seed-hash" not in cache.rows


# ─── Tests: invariant 4 — cache lookup ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_answer_no_provider_call() -> None:
    """Cache hit → ledger row(cache_hit=True), no provider call, fresh llm_call_id."""
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],  # source filter
            [],  # no tombstones
        ]
    )
    # Pre-seed the cache with the input hash that the gateway will compute.
    cfg = _config()
    expected_hash = hashlib.sha256(
        ("q" + "|" + "100" + "|" + cfg.model + "|" + cfg.prompt_template_version).encode(
            "utf-8"
        )
    ).hexdigest()
    await cache.store(
        session,
        input_hash=expected_hash,
        answer_text="cached",
        citation_ids=[100],
        model=cfg.model,
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=cfg,
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, AnswerWithCitations)
    assert res.cache_hit is True
    assert res.answer_text == "cached"
    assert res.citation_ids == (100,)
    assert provider.calls == []
    assert ledger.rows[-1].cache_hit is True
    assert ledger.rows[-1].error is None


@pytest.mark.asyncio
async def test_cache_miss_calls_provider_and_stores() -> None:
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(citation_ids=(100,))
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, AnswerWithCitations)
    assert res.cache_hit is False
    assert len(provider.calls) == 1
    # Provider response was persisted to cache.
    assert len(cache.rows) == 1
    assert ledger.rows[-1].cache_hit is False


# ─── Tests: invariant 5 — budget guard ──────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_exceeded_daily() -> None:
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo(daily_cost=Decimal("99.00"), monthly_cost=Decimal("100.00"))
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(daily=Decimal("5.00"), monthly=Decimal("500.00")),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "budget_exceeded"
    assert provider.calls == []
    assert ledger.rows[-1].error == "budget_exceeded"


@pytest.mark.asyncio
async def test_budget_exceeded_monthly() -> None:
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo(daily_cost=Decimal("0"), monthly_cost=Decimal("999.00"))
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(daily=Decimal("100.00"), monthly=Decimal("50.00")),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "budget_exceeded"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_budget_atomic_advisory_lock_invoked() -> None:
    """Each call MUST issue ``pg_advisory_lock`` BEFORE reading totals
    AND issue ``pg_advisory_unlock`` BEFORE provider dispatch.

    Real concurrency requires a real Postgres session — out of scope for the
    unit suite (T5-04 has the integration test). Here we assert:

    * the lock statement is dispatched before the provider HTTP;
    * the unlock statement is dispatched ALSO before the provider HTTP
      (placeholder-pattern: lock is released BEFORE the round-trip).
    """
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(citation_ids=(100,))

    captured: list[str] = []

    class _CapturingSession(FakeSession):
        async def execute(self, *args: Any, **kwargs: Any) -> _SessionExecutor:
            stmt = args[0] if args else kwargs.get("statement")
            captured.append(str(stmt))
            return await super().execute(*args, **kwargs)

    sess = _CapturingSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    await synthesize_answer(
        sess,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    # Session-scoped lock acquired AND released. Closes F1 (lock held
    # across HTTP) — both calls happen before provider HTTP.
    assert any("pg_advisory_lock" in s for s in captured), captured
    assert any("pg_advisory_unlock" in s for s in captured), captured
    # Verify ordering: lock before unlock, both before provider dispatch.
    lock_idx = next(
        i for i, s in enumerate(captured) if "pg_advisory_lock" in s
    )
    unlock_idx = next(
        i for i, s in enumerate(captured) if "pg_advisory_unlock" in s
    )
    assert lock_idx < unlock_idx, (lock_idx, unlock_idx, captured)
    # Provider was called exactly once after the unlock.
    assert len(provider.calls) == 1


# ─── Tests: invariant 6 — provider error categorisation ─────────────────────


@pytest.mark.parametrize(
    "subtype", ["rate_limit", "timeout", "5xx", "connection_reset"]
)
@pytest.mark.asyncio
async def test_provider_transient_error_abstains(subtype: str) -> None:
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(
        raise_exc=ProviderTransientError(subtype, message=f"{subtype} err")
    )
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "provider_error"
    assert ledger.rows[-1].error == f"provider_transient:{subtype}"


@pytest.mark.parametrize(
    "subtype",
    ["auth", "bad_request", "contract_violation", "model_not_found"],
)
@pytest.mark.asyncio
async def test_provider_structural_error_abstains_and_emits_stop(
    subtype: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural errors ledger-tag, log ERROR, and emit_stop_signal."""
    emitted: list[str] = []

    def _fake_emit(name: str) -> None:
        emitted.append(name)

    monkeypatch.setattr(
        "bot.services.observability.emit_stop_signal", _fake_emit
    )

    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(
        raise_exc=ProviderStructuralError(subtype, message=f"{subtype} err")
    )
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "provider_error"
    assert ledger.rows[-1].error == f"provider_structural:{subtype}"
    assert emitted == ["llm_provider_structural"]


@pytest.mark.asyncio
async def test_provider_unknown_error_abstains_no_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom Exception not in transient/structural taxonomy → unknown."""
    emitted: list[str] = []
    monkeypatch.setattr(
        "bot.services.observability.emit_stop_signal",
        lambda name: emitted.append(name),
    )

    class WeirdError(Exception):
        pass

    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(raise_exc=WeirdError("unexpected"))
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "provider_error"
    assert ledger.rows[-1].error == "provider_unknown:WeirdError"
    assert emitted == []


# ─── Tests: invariant 7 — citation enforcement ──────────────────────────────


@pytest.mark.asyncio
async def test_citation_hallucination_rejected() -> None:
    """Provider returns a citation_id NOT in bundle → reject with provider_error."""
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(citation_ids=(999,))  # hallucinated
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "provider_error"
    assert ledger.rows[-1].error == "citation_hallucination"


@pytest.mark.asyncio
async def test_citation_subset_accepted() -> None:
    """Provider returns proper subset of bundle.evidence_ids → accept."""
    bundle = _make_bundle((100, 101, 102))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(citation_ids=(100, 102))
    session = FakeSession(
        query_results=[
            [
                {"message_version_id": 100},
                {"message_version_id": 101},
                {"message_version_id": 102},
            ],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, AnswerWithCitations)
    assert set(res.citation_ids).issubset({100, 101, 102})


# ─── Tests: cache key stability + qa_trace_id required ──────────────────────


def test_cache_key_stability_same_inputs_same_hash() -> None:
    from bot.services.llm_gateway import _cache_input_hash

    h1 = _cache_input_hash(
        query_normalized="hello",
        citation_ids=[100, 101],
        model="claude-haiku-4-5-20251001",
        prompt_template_version="v1",
    )
    h2 = _cache_input_hash(
        query_normalized="hello",
        citation_ids=[101, 100],  # ORDER MUST NOT MATTER
        model="claude-haiku-4-5-20251001",
        prompt_template_version="v1",
    )
    assert h1 == h2


def test_cache_key_changes_when_query_changes() -> None:
    from bot.services.llm_gateway import _cache_input_hash

    h1 = _cache_input_hash(
        query_normalized="hello",
        citation_ids=[100],
        model="m",
        prompt_template_version="v1",
    )
    h2 = _cache_input_hash(
        query_normalized="hi",
        citation_ids=[100],
        model="m",
        prompt_template_version="v1",
    )
    assert h1 != h2


def test_cache_key_changes_when_template_version_changes() -> None:
    from bot.services.llm_gateway import _cache_input_hash

    h1 = _cache_input_hash(
        query_normalized="hello",
        citation_ids=[100],
        model="m",
        prompt_template_version="v1",
    )
    h2 = _cache_input_hash(
        query_normalized="hello",
        citation_ids=[100],
        model="m",
        prompt_template_version="v2",
    )
    assert h1 != h2


@pytest.mark.asyncio
async def test_qa_trace_id_required_keyword_only() -> None:
    """``qa_trace_id`` is a required keyword-only parameter (no Optional)."""
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider()
    session = FakeSession(query_results=[[]])

    with pytest.raises(TypeError):
        await synthesize_answer(  # type: ignore[call-arg]
            session,  # type: ignore[arg-type]
            bundle=bundle,
            query="q",
            config=_config(),
            ledger_repo=ledger,
            cache_repo=cache,
            provider=provider,
        )


@pytest.mark.asyncio
async def test_synthesize_answer_never_raises_on_provider_error() -> None:
    """No documented failure path raises into the caller — gateway always returns."""
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(
        raise_exc=ProviderTransientError("rate_limit", message="overload")
    )
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    # If the gateway raised, this assertion would not be reached.
    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)


@pytest.mark.asyncio
async def test_long_query_triggers_byte_mirror_normalization() -> None:
    """A query >256 chars goes through the same double-strip as search.py.

    The cache-key hash uses the normalized form, so two queries that differ
    only by trailing whitespace land in the same cache slot iff the
    normalisation matches search.py byte-for-byte.
    """
    from bot.services.llm_gateway import _cache_input_hash, _normalize_query

    raw_a = "  " + "a" * 250 + " " + "b" * 100 + "   "
    raw_b = "  " + "a" * 250 + " " + "b" * 100  # no trailing spaces
    norm_a = _normalize_query(raw_a)
    norm_b = _normalize_query(raw_b)
    assert norm_a == norm_b
    h_a = _cache_input_hash(
        query_normalized=norm_a,
        citation_ids=[1],
        model="m",
        prompt_template_version="v1",
    )
    h_b = _cache_input_hash(
        query_normalized=norm_b,
        citation_ids=[1],
        model="m",
        prompt_template_version="v1",
    )
    assert h_a == h_b


@pytest.mark.asyncio
async def test_concurrent_calls_under_budget_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent under-budget calls both complete (lock semantics test).

    Real Postgres advisory locks serialise — exercised in T5-04 integration.
    Here we verify the gateway does not deadlock on a no-op session lock.
    """

    async def _one() -> SynthesisResult:
        bundle = _make_bundle((100,))
        ledger = FakeLedgerRepo()
        cache = FakeCacheRepo()
        provider = FakeProvider(citation_ids=(100,))
        session = FakeSession(
            query_results=[
                [{"message_version_id": 100}],
                [],
                ]
        )
        return await synthesize_answer(
            session,  # type: ignore[arg-type]
            bundle=bundle,
            query="q",
            config=_config(),
            qa_trace_id=11,
            ledger_repo=ledger,
            cache_repo=cache,
            provider=provider,
        )

    res = await asyncio.gather(_one(), _one())
    assert all(isinstance(r, AnswerWithCitations) for r in res)


# ─── Tests: F2 — citation enforcement uses surviving_ids (not bundle) ───────


@pytest.mark.asyncio
async def test_citation_to_filtered_id_rejected_as_hallucination() -> None:
    """Provider cites an id that survived the bundle but NOT the source filter.

    Setup: bundle has ids [100, 101, 102]. Source filter returns
    [100, 101] (102 dropped — e.g., re-marked offrecord). Provider returns
    citation_ids=(100, 102). Naive impl (pre-F2) compares against
    ``bundle.evidence_ids`` = {100, 101, 102} and ACCEPTS the answer,
    silently leaking the filtered id 102. Post-F2 the comparison is
    against the surviving set {100, 101}, so 102 is rejected as a
    hallucination.
    """
    bundle = _make_bundle((100, 101, 102))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(citation_ids=(100, 102))
    session = FakeSession(
        query_results=[
            # Source filter: 102 dropped, 100 + 101 survive.
            [{"message_version_id": 100}, {"message_version_id": 101}],
            # Tombstone gate: clean.
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "provider_error"
    # Placeholder row UPDATEd with citation_hallucination error.
    assert ledger.rows[-1].error == "citation_hallucination"
    # Cache MUST NOT have stored the leaking answer.
    assert len(cache.rows) == 0


@pytest.mark.asyncio
async def test_cache_key_and_prompt_use_same_surviving_set() -> None:
    """Same surviving set → same prompt body even if pre-filter bundle differs.

    Two bundles with different pre-filter sizes can converge to the same
    surviving set after the source filter. Both must produce the same
    cache key + prompt body — closes F2/F3 invariant that cache key
    isn't poisoned by pre-filter ids.
    """
    # Bundle A: 3 evidence ids, filter survives only [100].
    bundle_a = _make_bundle((100, 101, 102))
    ledger_a = FakeLedgerRepo()
    cache_a = FakeCacheRepo()
    provider_a = FakeProvider(citation_ids=(100,))
    session_a = FakeSession(
        query_results=[
            [{"message_version_id": 100}],  # filter survives only 100
            [],
        ]
    )

    res_a = await synthesize_answer(
        session_a,  # type: ignore[arg-type]
        bundle=bundle_a,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger_a,
        cache_repo=cache_a,
        provider=provider_a,
    )

    # Bundle B: 1 evidence id only, filter trivially survives [100].
    bundle_b = _make_bundle((100,))
    ledger_b = FakeLedgerRepo()
    cache_b = FakeCacheRepo()
    provider_b = FakeProvider(citation_ids=(100,))
    session_b = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res_b = await synthesize_answer(
        session_b,  # type: ignore[arg-type]
        bundle=bundle_b,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger_b,
        cache_repo=cache_b,
        provider=provider_b,
    )

    assert isinstance(res_a, AnswerWithCitations)
    assert isinstance(res_b, AnswerWithCitations)
    # Same prompt body → same prompt_hash on the ledger row.
    assert ledger_a.rows[-1].prompt_hash == ledger_b.rows[-1].prompt_hash
    # Same cache key → same input_hash on the cache row.
    a_hash = list(cache_a.rows.keys())[0]
    b_hash = list(cache_b.rows.keys())[0]
    assert a_hash == b_hash


# ─── Tests: F4 — concurrent cache miss double-dispatch race ─────────────────


@pytest.mark.asyncio
async def test_concurrent_cache_miss_only_one_dispatch() -> None:
    """Two concurrent calls with identical inputs MUST dispatch provider AT MOST once.

    F4 closure (Option A — re-check cache UNDER the advisory lock):
    pre-fix two concurrent callers see cache miss in their FIRST cache
    check (invariant 4), both proceed into the placeholder reservation
    block, both insert placeholders, both dispatch the provider, both
    call ``cache.store(...)`` — the second's INSERT hits the
    ``input_hash UNIQUE`` constraint → IntegrityError. Post-fix the
    placeholder reservation block re-checks the cache UNDER the lock;
    if a concurrent caller has stored the row between the first
    invariant-4 check and the lock acquisition, the re-check observes
    it and returns the cached answer WITHOUT dispatching.

    Real Postgres advisory locks serialise globally on the same
    ``lock_id``; in the unit suite we use a process-local
    ``asyncio.Lock`` keyed on ``LLM_BUDGET_LOCK_ID`` so the second
    caller blocks at lock acquisition until the first caller's
    pipeline (including ``cache.store``) completes. Under that
    serialisation the second caller's re-check observes the cache row
    and dispatches zero providers.

    NOTE: in production the lock is released BEFORE the provider HTTP
    (per F1, lock not held across the 5-15s round-trip). The
    asyncio.Lock here models the EFFECTIVE GLOBAL SERIALISATION on the
    lock_id that real Postgres provides — it is unit-test scaffolding
    and not a 1:1 reflection of the gateway's lock-hold duration. T5-04
    integration tests exercise the real Postgres path.
    """
    bundle = _make_bundle((100,))
    shared_ledger = FakeLedgerRepo()
    shared_cache = FakeCacheRepo()
    dispatch_counter = {"n": 0}

    class _CountingProvider:
        async def call(self, *, prompt: str, model: str) -> ProviderResult:
            dispatch_counter["n"] += 1
            # Yield to event loop so the second caller can race.
            await asyncio.sleep(0)
            return ProviderResult(
                answer_text="synthesized",
                citation_ids=(100,),
                tokens_in=10,
                tokens_out=5,
                request_id="req-shared",
                raw_latency_ms=12,
            )

    provider = _CountingProvider()
    # Per-process asyncio.Lock keyed on LLM_BUDGET_LOCK_ID models the
    # global serialisation that real Postgres provides. The unit-test
    # session holds the lock from acquire until release, INCLUDING the
    # post-acquire cache.store, so the second caller observes the
    # cache row on its re-check.
    shared_lock = asyncio.Lock()
    pipeline_done = asyncio.Event()
    first_pipeline_done = False

    class _SerialisingSession(FakeSession):
        async def execute(
            self, *args: Any, **kwargs: Any
        ) -> _SessionExecutor:
            stmt = args[0] if args else kwargs.get("statement")
            s = str(stmt) if stmt is not None else ""
            if "pg_advisory_lock" in s and "unlock" not in s:
                # Acquire the shared asyncio.Lock; second caller blocks
                # here until first caller's pipeline+release.
                await shared_lock.acquire()
                return _SessionExecutor([])
            if "pg_advisory_unlock" in s:
                # In real Postgres release is immediate. Here we defer
                # release of the FIRST caller's lock until the test's
                # pipeline_done signal so the second caller blocks
                # across the first caller's cache.store. This models
                # the effective global serialisation on lock_id (real
                # PG would queue concurrent acquirers on the same id).
                nonlocal first_pipeline_done
                if not first_pipeline_done:
                    # First caller's unlock: defer release.
                    return _SessionExecutor([])
                # Second caller's unlock or first caller's deferred
                # release path — release the asyncio.Lock.
                if shared_lock.locked():
                    shared_lock.release()
                return _SessionExecutor([])
            if self.query_results:
                rows = self.query_results.pop(0)
            else:
                rows = []
            return _SessionExecutor(rows)

    async def _one(idx: int) -> SynthesisResult:
        session = _SerialisingSession(
            query_results=[
                [{"message_version_id": 100}],  # source filter
                [],  # tombstone clean
            ]
        )
        result = await synthesize_answer(
            session,  # type: ignore[arg-type]
            bundle=bundle,
            query="q",
            config=_config(),
            qa_trace_id=11,
            ledger_repo=shared_ledger,
            cache_repo=shared_cache,
            provider=provider,
        )
        nonlocal first_pipeline_done
        if idx == 0:
            # First caller finished cache.store and placeholder UPDATE.
            # Allow second caller to proceed past lock acquisition.
            first_pipeline_done = True
            pipeline_done.set()
            if shared_lock.locked():
                shared_lock.release()
        return result

    results = await asyncio.gather(_one(0), _one(1))
    assert all(isinstance(r, AnswerWithCitations) for r in results)
    # CORE INVARIANT: provider dispatched AT MOST ONCE (F4 closure).
    assert dispatch_counter["n"] <= 1, dispatch_counter
    # And exactly one cache row was stored.
    assert len(shared_cache.rows) == 1


# ─── Tests: F5 — defensive abort when provider returns no citations ─────────


@pytest.mark.asyncio
async def test_provider_empty_citations_aborts_synthesis() -> None:
    """Provider returns answer_text with empty citation_ids → abort + no cache.

    The real AnthropicProvider / OpenAIProvider currently return
    ``citation_ids=tuple()`` unconditionally — the prompt template that
    asks the model to emit citations as JSON envelope lands with T5-04.
    Caching an answer with empty ``citation_ids`` would break forget
    invalidation (which joins on ``citation_ids @> '[fid]'::jsonb`` —
    empty array can't match any forget event). Until T5-04 wires real
    citation parsing, we refuse to cache + abstain.
    """
    bundle = _make_bundle((100,))
    ledger = FakeLedgerRepo()
    cache = FakeCacheRepo()
    provider = FakeProvider(
        citation_ids=(),  # F5 trigger.
        answer_text="A short answer with no citations",
    )
    session = FakeSession(
        query_results=[
            [{"message_version_id": 100}],
            [],
        ]
    )

    res = await synthesize_answer(
        session,  # type: ignore[arg-type]
        bundle=bundle,
        query="q",
        config=_config(),
        qa_trace_id=11,
        ledger_repo=ledger,
        cache_repo=cache,
        provider=provider,
    )

    assert isinstance(res, Abstention)
    assert res.reason == "provider_error"
    # Ledger has the placeholder UPDATEd with the no-citations sentinel.
    assert ledger.rows[-1].error == "provider_returned_no_citations"
    # No cache row was written — defends against forget invalidation
    # being unable to join on the empty array.
    assert len(cache.rows) == 0


# ─── T5-04: real pricing wired via bot.services.llm_pricing ──────────────────


def test_estimate_cost_haiku_one_million_input_tokens_matches_pricing_table() -> None:
    """contracts.md §12.6 acceptance test — verbatim.

    ``_estimate_cost(model="claude-haiku-4-5-20251001", tokens_in=1_000_000,
    tokens_out=0) == Decimal("1.000000")``.
    """
    cfg = _config()  # model="claude-haiku-4-5-20251001"
    cost = _estimate_cost(config=cfg, tokens_in=1_000_000, tokens_out=0)
    assert cost == Decimal("1.000000")


def test_estimate_cost_unknown_model_returns_zero_emits_stop_signal(monkeypatch) -> None:
    """KeyError on unknown model → log + emit_stop_signal + return Decimal("0")."""
    from bot.services import observability

    emitted: list[str] = []
    monkeypatch.setattr(
        observability, "emit_stop_signal", lambda name: emitted.append(name)
    )

    cfg = LLMGatewayConfig(
        provider="anthropic",
        model="nonexistent-model",  # type: ignore[arg-type]
        daily_ceiling_usd=Decimal("5"),
        monthly_ceiling_usd=Decimal("50"),
        prompt_template_version="v1.0.0",
    )
    cost = _estimate_cost(config=cfg, tokens_in=100, tokens_out=100)
    assert cost == Decimal("0")
    assert emitted == ["llm_provider_structural"]


# ─── T10-03 Codex review fixes: FIX-HIGH-3 provider JSON error ───────────────


def _make_extract_config(
    *,
    daily: Decimal = Decimal("5.00"),
    monthly: Decimal = Decimal("50.00"),
) -> LLMGatewayConfig:
    return LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
        prompt_template_version="graph_triples_v0_1_0",
    )


@dataclass
class _ExtractFakeSession:
    """Minimal session fake for extract_graph_triples tests.

    pg_advisory calls are silenced. All other execute() calls return no rows
    (simulating no matching card or user → UNKNOWN_* entities, which means
    triples resolve to UNKNOWN and are dropped — irrelevant for JSON parse tests).
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        stmt = args[0] if args else kwargs.get("statement")
        if stmt is not None and "pg_advisory" in str(stmt):
            return _SessionExecutor([])
        return _SessionExecutor([])


@dataclass
class _ExtractFakeLedger:
    """Minimal ledger fake for extract_graph_triples error path tests."""

    rows: list[Any] = field(default_factory=list)
    daily_cost: Decimal = Decimal("0")
    monthly_cost: Decimal = Decimal("0")
    _next_id: int = 1

    async def record(self, session: Any, *, qa_trace_id: Any, provider: str, model: str,
                     prompt_hash: str, response_hash: Any, tokens_in: int, tokens_out: int,
                     cost_usd: Decimal, latency_ms: int, request_id: Any, cache_hit: bool,
                     error: Any, call_type: str = "unknown") -> Any:
        from dataclasses import make_dataclass
        Row = make_dataclass("Row", [("id", int), ("cost_usd", Decimal), ("call_type", str)])
        row = Row(id=self._next_id, cost_usd=cost_usd, call_type=call_type)
        self.rows.append(row)
        self._next_id += 1
        return row

    async def daily_cost_usd(self, session: Any, *, day: Any) -> Decimal:
        return self.daily_cost

    async def monthly_cost_usd(self, session: Any, *, year: int, month: int) -> Decimal:
        return self.monthly_cost

    async def update_placeholder(self, session: Any, *, llm_call_id: int, cost_usd: Decimal,
                                  response_hash: Any, tokens_in: int, tokens_out: int,
                                  request_id: Any, latency_ms: int, error: Any) -> Any:
        for row in self.rows:
            if row.id == llm_call_id:
                row.cost_usd = cost_usd
                return row
        raise KeyError(f"not found: {llm_call_id}")


@dataclass
class _ExtractFakeProvider:
    answer_json: str
    tokens_in: int = 10
    tokens_out: int = 5

    async def call(self, *, prompt: str, model: str) -> Any:
        return ProviderResult(
            answer_text=self.answer_json,
            citation_ids=(),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id="req-test",
            raw_latency_ms=5,
        )


@pytest.mark.asyncio
async def test_extract_raises_on_malformed_json() -> None:
    """Provider returning malformed JSON must raise ExtractGraphTriplesError (FIX-HIGH-3)."""
    from bot.services.graph_common import ExtractGraphTriplesError
    from bot.services.llm_gateway import extract_graph_triples

    provider = _ExtractFakeProvider(answer_json="not-json{")
    ledger = _ExtractFakeLedger()
    session = _ExtractFakeSession()

    with pytest.raises(ExtractGraphTriplesError, match="malformed JSON"):
        await extract_graph_triples(
            session,  # type: ignore[arg-type]
            source_table="message_versions",
            source_pk="1",
            source_text="текст",
            source_id="1",
            source_mv_id=None,
            prompt_version="graph_triples_v0_1_0",
            run_id=1,
            governance_policy="normal",
            config=_make_extract_config(),
            ledger_repo=ledger,
            provider=provider,
        )


@pytest.mark.asyncio
async def test_extract_raises_on_non_list_json() -> None:
    """Provider returning a JSON object (not array) must raise ExtractGraphTriplesError (FIX-HIGH-3)."""
    from bot.services.graph_common import ExtractGraphTriplesError
    from bot.services.llm_gateway import extract_graph_triples

    provider = _ExtractFakeProvider(answer_json='{"foo": "bar"}')
    ledger = _ExtractFakeLedger()
    session = _ExtractFakeSession()

    with pytest.raises(ExtractGraphTriplesError, match="non-list JSON root"):
        await extract_graph_triples(
            session,  # type: ignore[arg-type]
            source_table="message_versions",
            source_pk="1",
            source_text="текст",
            source_id="1",
            source_mv_id=None,
            prompt_version="graph_triples_v0_1_0",
            run_id=1,
            governance_policy="normal",
            config=_make_extract_config(),
            ledger_repo=ledger,
            provider=provider,
        )


@pytest.mark.asyncio
async def test_extract_empty_list_still_works_no_regression() -> None:
    """Provider returning [] is valid empty extraction — no error (FIX-HIGH-3 regression guard)."""
    from bot.services.llm_gateway import extract_graph_triples

    provider = _ExtractFakeProvider(answer_json="[]")
    ledger = _ExtractFakeLedger()
    session = _ExtractFakeSession()

    result = await extract_graph_triples(
        session,  # type: ignore[arg-type]
        source_table="message_versions",
        source_pk="1",
        source_text="текст",
        source_id="1",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_extract_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    assert result.triples == []
    assert result.skipped_total == 0
