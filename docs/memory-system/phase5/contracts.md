# Phase 5 — Frozen Interface Contracts

**Status:** FROZEN 2026-05-02 — derived from `docs/memory-system/PHASE5_PLAN.md` (ratified Sprint 0).
**Owner:** Orchestrator A.
**Audience:** T5-01 / T5-02 / T5-03 / T5-04 / T5-05 implementers.
**Authority:** This file supersedes informal cross-stream coordination. All Wave 1+2+3 streams program against this surface, NOT against each other's WIP code.

---

## §1. Purpose + scope

This document freezes the cross-stream interface contracts for Phase 5 (LLM gateway + answer synthesis). Every Wave 1, Wave 2, and Wave 3 implementer MUST program against this file rather than against another stream's in-flight branch. Wave 1 streams (T5-01 gateway, T5-02 schema) ship in parallel; Wave 2 (T5-03 repos, T5-04 handler+migration) consumes Wave 1 surfaces; Wave 3 (T5-05 evals) consumes Wave 2. Race conditions between T5-01 and T5-02 — and divergence between T5-03's repo signatures and T5-01's mock expectations — are the dominant failure modes that this contract eliminates.

**Frozen rule.** Changes to any contract in §3-§9 require: (a) explicit orchestrator approval, (b) corresponding update to every consuming stream, and (c) re-dispatch of affected implementers if their work is invalidated. Contracts are frozen until Phase 5 closes (per §11). Wave 0 (production-blocker hotfix #164) is a hard prerequisite — its merged surfaces (live v1 persistence, import `current_version_id`, `normalized_text` backfill, `qa_traces` cascade layer) are assumed available on `main` before any Wave 1 PR is opened.

---

## §2. Stream ↔ contract matrix

| Stream | Wave | Ticket | Produces | Consumes | Contract sections |
|--------|------|--------|----------|----------|-------------------|
| A — gateway | 1 | T5-01 | `synthesize_answer`, `AnswerWithCitations`, `Abstention`, `LLMGatewayConfig`, `Provider` Protocol, `ProviderResult` | `EvidenceBundle` (Phase 4 frozen), `LedgerRepoProtocol`, `SynthesisCacheRepoProtocol` (mocks until T5-03 lands real repos) | Produces §3, §10. Consumes Phase 4 `EvidenceBundle` + §10 mocks. |
| B — schema | 1 | T5-02 | `llm_usage_ledger` + `llm_synthesis_cache` tables (alembic 024), `LlmUsageLedger` + `LlmSynthesisCache` ORM | none (greenfield migration) | Produces §4. |
| C — ledger repo | 2 | T5-03 | `LedgerRepo`, `SynthesisCacheRepo` (real implementations) | §4 ORM models | Produces §5. Consumes §4. |
| D — handler + migration + cascade | 2 | T5-04 | `/recall` LLM integration, `qa_traces` extension (alembic 025), three new cascade layers | §3 (`synthesize_answer`), §5 (real repos), §4 (`qa_traces` shape), Phase 4 `run_qa` | Produces §6, §7, §8. Consumes §3, §5, §4. |
| E — evals | 3 | T5-05 | `tests/eval/test_qa_llm_eval_cases.py`, `tests/fixtures/qa_llm_eval_cases.json` | §3, §6 | Produces §9. Consumes §3, §6. |

Wave 1 parallelism rule: T5-01 mocks `LedgerRepo` and `SynthesisCacheRepo` against the Protocols defined in §10 BEFORE T5-03 lands; T5-03 must satisfy those Protocols verbatim or the bundled Wave 2 PR breaks. T5-04 wires real repos via dependency injection (no monkey-patching).

---

## §3. Gateway contracts (T5-01 produces; T5-04 consumes)

### §3.1 `synthesize_answer` signature

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, NamedTuple, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle


async def synthesize_answer(
    session: AsyncSession,
    *,
    bundle: EvidenceBundle,
    query: str,
    config: LLMGatewayConfig,
    qa_trace_id: int,
) -> SynthesisResult:
    """Synthesize an LLM answer over an evidence bundle.

    Pre-conditions (caller-enforced):
      * ``bundle`` is the output of ``run_qa(...)``; immutable, may be empty.
      * ``query`` is the redaction-aware string already used to produce ``bundle``.
      * ``qa_trace_id`` REFERENCES an already-flushed row in ``qa_traces``. The
        caller MUST create the trace BEFORE invoking this function so that every
        ``llm_usage_ledger`` row written here can populate
        ``qa_trace_id`` (cascade FK direction is HANDLER -> LEDGER, never the
        reverse).
      * ``config.prompt_template_version`` is a stable semver string; bumping it
        invalidates cache rows generated under previous versions.

    Post-conditions (gateway-enforced):
      * EXACTLY ONE row is appended to ``llm_usage_ledger`` for every call,
        regardless of outcome (success, abstain, cache hit, error). The row is
        flushed but NOT committed; caller owns the transaction.
      * On ``AnswerWithCitations``: ``set(result.citation_ids) <= set(bundle.evidence_ids)``.
      * No raw provider request/response BODIES are logged above DEBUG level.
      * Provider exceptions never propagate; all are categorized into
        ``Abstention(reason='provider_error')`` per §3.6 step 6.
    """
    ...
```

`qa_trace_id: int` is REQUIRED (NOT `Optional`). This closes Codex Sprint 0 PAR round-1 HIGH 4 (cascade direction). The handler in §6.1 step 1 guarantees the row exists before this call.

### §3.2 `AnswerWithCitations` dataclass

```python
@dataclass(frozen=True, slots=True)
class AnswerWithCitations:
    answer_text: str
    citation_ids: tuple[int, ...]   # immutable; subset of bundle.evidence_ids
    cost_usd: Decimal               # >= Decimal("0"); may be zero on cache hit
    cache_hit: bool                 # True iff served from llm_synthesis_cache
    llm_call_id: int                # FK -> llm_usage_ledger.id
```

Field semantics:

* `answer_text` — final natural-language answer. May contain inline citation markers (e.g., `[1]`, `[2]`) but those are presentation hints; the binding citation list is `citation_ids`.
* `citation_ids` — `tuple[int, ...]` (NOT `list[int]`). Immutability is load-bearing for cache key stability and downstream hashing. Operator-level invariant: `set(citation_ids).issubset(set(bundle.evidence_ids))` MUST be enforced by the gateway BEFORE returning. A return value that violates this invariant is a contract bug, not a recoverable runtime condition; tests in T5-01 verify with explicit `subset(...)` assertion.
* `cost_usd` — `Decimal` (NOT `float`; precision-bearing). Zero allowed on cache hit. Always non-negative.
* `cache_hit` — `True` iff the gateway returned without dispatching to a provider in this call. The corresponding ledger row is also written with `cache_hit=True`.
* `llm_call_id` — primary key of the freshly-written `llm_usage_ledger` row. Stable across the request lifetime.

### §3.3 `Abstention` dataclass

```python
@dataclass(frozen=True, slots=True)
class Abstention:
    reason: Literal[
        "empty_bundle",
        "all_filtered",
        "budget_exceeded",
        "provider_error",
        "forget_invalidated",
    ]
    cost_usd: Decimal               # zero except for refused-after-call paths
    llm_call_id: int                # ledger row written for every outcome
```

Field semantics:

* `reason` — closed enum, exactly five values:
  - `empty_bundle` — `len(bundle.evidence_ids) == 0` (short-circuit before any other invariant runs).
  - `all_filtered` — defense-in-depth source filter dropped every cited row (governance race against `search.py`).
  - `budget_exceeded` — daily or monthly USD ceiling tripped under advisory lock.
  - `provider_error` — categorized provider failure (transient / structural / unknown — see §3.6 step 6). Single abstention reason for all three sub-categories; the categorization is recorded in `llm_usage_ledger.error` per §4.1.
  - `forget_invalidated` — three-key tombstone gate (§3.6 step 3) matched at least one cited `message_version_id`.
* `cost_usd` — `Decimal` (NOT `float`). Zero in the empty-bundle, all-filtered, budget-exceeded, and forget-invalidated paths. May be non-zero on `provider_error` if the provider returned successfully but later failed citation enforcement (the call cost has already been incurred and recorded).
* `llm_call_id` — every abstention writes a ledger row; this is its primary key.

### §3.4 `LLMGatewayConfig` dataclass

```python
@dataclass(frozen=True, slots=True)
class LLMGatewayConfig:
    provider: Literal["anthropic", "openai"]
    model: str                              # e.g., "claude-haiku-4-5-20251001"
    daily_usd_ceiling: Decimal              # default Decimal("5.00")
    monthly_usd_ceiling: Decimal            # default Decimal("50.00")
    prompt_template_version: str            # semver string, e.g. "1.0.0"
    request_timeout_s: float                # provider-call hard timeout
    max_tokens_out: int                     # provider call cap
```

Field semantics:

* `prompt_template_version` — semver-style string used as a component of the cache input hash and ledger correlation. Bumping this version invalidates cache rows generated under previous versions WITHOUT requiring a DDL change. T5-04 hardcodes the initial value; future revisions of the prompt template MUST bump this BEFORE merge.
* `daily_usd_ceiling` / `monthly_usd_ceiling` — `Decimal` only; loaded from env (`LLM_DAILY_USD_CEILING`, `LLM_MONTHLY_USD_CEILING`) or `feature_flags.config_json`. Defaults: 5.00 / 50.00 USD per the ratification table in PHASE5_PLAN.md §11.
* `provider` — closed `Literal`. Adding a new provider requires updating both the literal and the dispatcher in T5-01.
* `model` — provider-specific model identifier. Default for `provider='anthropic'`: `claude-haiku-4-5-20251001`. Stop signal #10 in PHASE5_PLAN.md §8 binds: if SDK rejects this ID at T5-01 implementation time, halt and re-verify against the current Anthropic models catalog before resuming.

### §3.5 `Provider` Protocol

```python
class ProviderResult(NamedTuple):
    answer_text: str
    citation_ids: tuple[int, ...]    # provider-explicit; NOT post-parsed from answer_text
    tokens_in: int
    tokens_out: int
    request_id: str                  # provider-supplied trace id; stored in ledger
    raw_latency_ms: int              # measured by the provider implementation


class Provider(Protocol):
    async def call(
        self,
        *,
        prompt: str,
        model: str,
        max_tokens_out: int,
        request_timeout_s: float,
    ) -> ProviderResult: ...
```

Binding details:

* `citation_ids` is provider-explicit. The gateway treats this as authoritative; it does NOT scan `answer_text` for citation markers. Provider implementations MUST extract citations via the LLM's structured-output channel (e.g., tool-call arguments, JSON schema response), not via regex on free-form text. Closes Codex Sprint 0 review point on hallucination detection.
* `tuple[int, ...]` is again immutable. Provider implementations that build the list internally MUST `tuple(...)` before return.
* `request_id` is non-empty; an empty string is a contract violation. If the provider does not supply a request id, the implementation MUST synthesize a uuid4 and document the substitution in the provider's docstring.
* No stub provider in production code paths. Test fakes are pytest fixtures injected via dependency override.

### §3.6 Pre-call invariant ORDER

The gateway MUST execute the following seven steps in this exact order. Each step has a named identifier so that test code can reference it.

1. **`STEP_EMPTY_BUNDLE_SHORTCIRCUIT`** — If `len(bundle.evidence_ids) == 0`, write a ledger row with `error='empty_bundle'`, `cache_hit=False`, `cost_usd=0`, `tokens_in=0`, `tokens_out=0`, then return `Abstention(reason='empty_bundle', cost_usd=Decimal("0"), llm_call_id=<new>)`. NO further work runs. NO provider call.
2. **`STEP_SOURCE_FILTER`** — Re-validate every cited `message_version_id` against `chat_messages.memory_policy NOT IN ('offrecord','forgotten') AND chat_messages.is_redacted=FALSE`. Defense-in-depth: `search.py` already filters at SQL level, but a race between search-time and call-time can leave a forgotten row in the bundle. Surviving citations replace `bundle.evidence_ids` for the rest of the pipeline. If the surviving set is empty, write ledger row with `error='all_filtered'`, return `Abstention(reason='all_filtered', cost_usd=Decimal("0"), llm_call_id=<new>)`.
3. **`STEP_FORGET_INVALIDATION_GATE`** — Three-key tombstone check, MIRRORING `bot/services/search.py` lines 99/102/106. For every `citation_id`, resolve `(chat_id, message_id, content_hash, user_id)` and JOIN against `forget_events` on ANY of:
   * `tombstone_key = 'message:' || chat_id || ':' || message_id`
   * `tombstone_key = 'message_hash:' || content_hash`
   * `tombstone_key = 'user:' || user_id`

   If ANY tombstone matches, call `SynthesisCacheRepo.invalidate_by_citation(message_version_id)` for each affected id (best-effort cache cleanup), write ledger row with `error='forget_invalidated'`, return `Abstention(reason='forget_invalidated', cost_usd=Decimal("0"), llm_call_id=<new>)`. Closes Codex round-1 HIGH 3 (was previously checking only `message:` keys).
4. **`STEP_CACHE_LOOKUP`** — Compute `input_hash = sha256(query_normalized || sorted(citation_ids) || config.model || config.prompt_template_version)` where `query_normalized = query.strip()[:256].strip()` (double-strip is load-bearing — byte-mirrors `bot/services/search.py:43,55`). Call `SynthesisCacheRepo.get_or_none(input_hash=...)`. On hit: write ledger row with `cache_hit=True`, `cost_usd=Decimal("0")`, `tokens_in=0`, `tokens_out=0`, `response_hash=<sha256 of cached answer_text>`, then return `AnswerWithCitations(answer_text=cached.answer_text, citation_ids=tuple(cached.citation_ids), cost_usd=Decimal("0"), cache_hit=True, llm_call_id=<new>)`. Cache lookup MUST happen AFTER step 3 — closes Codex round-1 HIGH 2 (cache cannot serve forgotten content).
5. **`STEP_BUDGET_GUARD_ATOMIC`** — Wrap budget check + reservation in a single transaction guarded by `pg_advisory_xact_lock(LLM_BUDGET_LOCK_ID)` where `LLM_BUDGET_LOCK_ID` is a deterministic int64 derived from `int.from_bytes(sha256(b"llm_budget_guard").digest()[:8], "big", signed=True)`. Inside the lock: read `LedgerRepo.daily_cost_usd(...)` and `monthly_cost_usd(...)`, compare against `config.daily_usd_ceiling` and `config.monthly_usd_ceiling`. If over either ceiling, write ledger row with `error='budget_exceeded'`, return `Abstention(reason='budget_exceeded', cost_usd=Decimal("0"), llm_call_id=<new>)`. Otherwise, write a PLACEHOLDER ledger row with `error=NULL`, `cost_usd=Decimal("0")`, `tokens_in=0`, `tokens_out=0`, `response_hash=NULL` (capture its `id` for the post-dispatch UPDATE). Closes Codex round-1 MEDIUM 1 (atomic concurrent budget).
6. **`STEP_PROVIDER_DISPATCH`** — Dispatch via the configured provider. Categorize all exceptions (closes Codex round-1 MEDIUM 2):
   * **Transient** (`rate_limit`, `timeout`, `5xx`, `connection_reset`): UPDATE placeholder ledger row with `error='provider_transient:<subtype>'`, return `Abstention(reason='provider_error', cost_usd=Decimal("0"), llm_call_id=placeholder.id)`. Log at WARNING.
   * **Structural** (`auth`, `bad_request`, `contract_violation`, `model_not_found`): UPDATE ledger row with `error='provider_structural:<subtype>'`, log at ERROR with full exception, emit `bot.services.observability.emit_stop_signal("llm_provider_structural")`, return `Abstention(reason='provider_error', cost_usd=Decimal("0"), llm_call_id=placeholder.id)`. NEVER raise into the handler (invariant #1: gatekeeper preservation).
   * **Unknown / catch-all** (any exception not matching above — e.g., new SDK subclass): UPDATE ledger row with `error='provider_unknown:<exception_class_name>'`, log at ERROR with `exc_info=True`, do NOT emit stop signal (avoid false-alarm), return `Abstention(reason='provider_error', cost_usd=Decimal("0"), llm_call_id=placeholder.id)`. Daily ledger query for `error LIKE 'provider_unknown:%'` is the operator-review trigger.

   On success: compute `cost_usd` from `tokens_in + tokens_out` and provider rate card, UPDATE placeholder row with measured `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `request_id`, `response_hash = sha256(provider_result.answer_text)`.
7. **`STEP_CITATION_ENFORCEMENT`** — Validate `set(provider_result.citation_ids).issubset(set(bundle.evidence_ids))` (using the post-source-filter set from step 2). If the subset relation is violated: UPDATE ledger row with `error='citation_hallucination'` (overwriting any non-error state), return `Abstention(reason='provider_error', cost_usd=cost_usd_from_step_6, llm_call_id=placeholder.id)`. Else: store the answer in `llm_synthesis_cache` via `SynthesisCacheRepo.store(...)`, return `AnswerWithCitations(answer_text=provider_result.answer_text, citation_ids=provider_result.citation_ids, cost_usd=cost_usd_from_step_6, cache_hit=False, llm_call_id=placeholder.id)`.

Test code references these step identifiers verbatim. Re-ordering any of the seven steps is a contract change requiring §11 process.

---

## §4. Schema contracts (T5-02 produces; T5-01 / T5-03 / T5-04 consume)

### §4.1 `llm_usage_ledger` row contract

DDL (verbatim from PHASE5_PLAN.md §5.B):

```sql
CREATE TABLE llm_usage_ledger (
    id BIGSERIAL PRIMARY KEY,
    qa_trace_id INTEGER NULL REFERENCES qa_traces(id) ON DELETE SET NULL,
    provider VARCHAR(64) NOT NULL,
    model VARCHAR(128) NOT NULL,
    prompt_hash CHAR(64) NOT NULL,         -- sha256 of normalized prompt
    response_hash CHAR(64) NULL,           -- NULL on errors / abstention
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    request_id VARCHAR(128) NULL,
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    error VARCHAR(255) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_usage_ledger_qa_trace_id ON llm_usage_ledger(qa_trace_id);
CREATE INDEX ix_llm_usage_ledger_model_created_at ON llm_usage_ledger(model, created_at);
CREATE INDEX ix_llm_usage_ledger_created_at ON llm_usage_ledger(created_at);
```

ORM shape (in `bot/db/models.py`):

```python
class LlmUsageLedger(Base):
    __tablename__ = "llm_usage_ledger"
    __table_args__ = (
        Index("ix_llm_usage_ledger_qa_trace_id", "qa_trace_id"),
        Index("ix_llm_usage_ledger_model_created_at", "model", "created_at"),
        Index("ix_llm_usage_ledger_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    qa_trace_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("qa_traces.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    response_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"), server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
```

`error VARCHAR(255)` carries the categorized error taxonomy. Permitted values:

* `NULL` — success path (placeholder row UPDATEd to success on dispatch return).
* `'empty_bundle'` — short-circuit per §3.6 step 1.
* `'all_filtered'` — all citations dropped by source filter per §3.6 step 2.
* `'budget_exceeded'` — daily / monthly ceiling tripped per §3.6 step 5.
* `'provider_transient:<subtype>'` — e.g., `'provider_transient:rate_limit'`, `'provider_transient:timeout'`, `'provider_transient:5xx'`, `'provider_transient:connection_reset'`.
* `'provider_structural:<subtype>'` — e.g., `'provider_structural:auth'`, `'provider_structural:bad_request'`, `'provider_structural:contract_violation'`, `'provider_structural:model_not_found'`.
* `'provider_unknown:<ExceptionClassName>'` — exception class name from `type(exc).__name__` for any uncategorized exception.
* `'forget_invalidated'` — three-key tombstone gate matched per §3.6 step 3.
* `'citation_hallucination'` — provider returned `citation_ids` not subset of bundle (overwrites prior `NULL` state per §3.6 step 7).

The `error` column is the SOLE source of truth for provider-error categorization. The `Abstention.reason` enum collapses all three provider sub-categories into `'provider_error'`; ledger queries that need to break them out MUST scan `error LIKE 'provider_%'`.

### §4.2 `llm_synthesis_cache` row contract

DDL (verbatim from PHASE5_PLAN.md §5.B):

```sql
CREATE TABLE llm_synthesis_cache (
    id BIGSERIAL PRIMARY KEY,
    input_hash CHAR(64) NOT NULL UNIQUE,
    answer_text TEXT NOT NULL,
    citation_ids JSONB NOT NULL,           -- array<int>
    model VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_hit_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    hit_count INTEGER NOT NULL DEFAULT 1
);
```

ORM shape:

```python
class LlmSynthesisCache(Base):
    __tablename__ = "llm_synthesis_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    input_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    citation_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    last_hit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
```

`citation_ids JSONB` is a flat array of integer `message_version_id` values. NEVER nested objects. Example: `[101, 204, 388]`. The cascade layer `_cascade_llm_synthesis_cache` (§8 layer 1) relies on JSONB `@>` containment to bulk-invalidate rows that cite forgotten ids; nested object shape would break the operator.

### §4.3 alembic chain rule

* **Migration 024** owns BOTH `llm_usage_ledger` and `llm_synthesis_cache` (single migration per PHASE5_PLAN.md §5.B). `revision = "024"`, `down_revision = "023"`. Wave 0 hotfix #164 ships migration `023` as the immediate predecessor; setting `down_revision = "022_add_qa_traces"` would create a sibling alembic head and fail `alembic heads` in CI. This closes Codex Sprint 0 review HIGH 1.
* **Migration 025** extends `qa_traces` with the four LLM columns (T5-04 — see §7). `revision = "025"`, `down_revision = "024"`.
* **Sibling-head check:** `alembic heads` MUST show a SINGLE head after each PR merge. The Wave 1 schema PR (T5-02) adds 024; Wave 2 handler PR (T5-04) adds 025; both PRs MUST land on a base where 023 is already merged. Any deviation from linear chain (`023 → 024 → 025`) is a contract violation requiring §11 process.

Future Phase 5 migrations chain linearly within the owned range (023-049); migration 026 onward MAY be reserved for hotfixes during Phase 5 close-out, and Phase 6 takes the chain from 050.

---

## §5. Repository contracts (T5-03 produces; T5-04 consumes)

### §5.1 `LedgerRepo`

```python
class LedgerRepo:
    @staticmethod
    async def record(
        session: AsyncSession,
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
    ) -> LlmUsageLedger:
        """Insert a ledger row. Flushes; caller commits. NEVER commits internally."""
        ...

    @staticmethod
    async def daily_cost_usd(
        session: AsyncSession,
        *,
        day: date,
    ) -> Decimal:
        """SUM(cost_usd) WHERE created_at >= day_utc_start AND < day_utc_end.

        UTC-bounded; ``day`` is interpreted as a UTC calendar date. Zero rows
        => return ``Decimal("0")`` (NEVER ``None``).
        """
        ...

    @staticmethod
    async def monthly_cost_usd(
        session: AsyncSession,
        *,
        year: int,
        month: int,
    ) -> Decimal:
        """SUM(cost_usd) WHERE created_at within (year, month) UTC.

        UTC-bounded. Zero rows => ``Decimal("0")`` (NEVER ``None``).
        """
        ...

    @staticmethod
    async def update_placeholder(
        session: AsyncSession,
        *,
        ledger_id: int,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: str | None,
        response_hash: str | None,
        error: str | None,
    ) -> int:
        """UPDATE the placeholder row written under STEP_BUDGET_GUARD_ATOMIC.

        Returns rowcount (must be 1). Flushes; caller commits.
        """
        ...
```

Binding details:

* All four methods `flush`-only, NEVER `commit`. The orchestration transaction is owned by the caller (T5-04 handler invokes `await session.commit()` once after `synthesize_answer` returns).
* `daily_cost_usd` / `monthly_cost_usd` boundary: UTC midnight. The calendar conversion uses `datetime.combine(day, time(0), tzinfo=timezone.utc)` and the next day at 00:00 UTC as the half-open upper bound.
* Zero-row return is `Decimal("0")` literal — never `None`, never `0.0` (float). T5-04 budget guard does `if daily_cost_usd > config.daily_usd_ceiling`; a `None` here would `TypeError` in the lock window and is forbidden.
* `update_placeholder` is the post-dispatch ledger UPDATE. T5-01 calls it inside `STEP_PROVIDER_DISPATCH` and `STEP_CITATION_ENFORCEMENT`. Returning rowcount allows T5-01 to assert exactly-one-update and surface the regression early.

### §5.2 `SynthesisCacheRepo`

```python
class SynthesisCacheRepo:
    @staticmethod
    async def get_or_none(
        session: AsyncSession,
        *,
        input_hash: str,
    ) -> LlmSynthesisCache | None:
        """Lookup by ``input_hash``. Returns row or None. No side effects."""
        ...

    @staticmethod
    async def store(
        session: AsyncSession,
        *,
        input_hash: str,
        answer_text: str,
        citation_ids: list[int],
        model: str,
    ) -> LlmSynthesisCache:
        """Insert a cache row. Flushes; caller commits.

        Caller MUST handle UNIQUE-violation on ``input_hash`` (concurrent races) by
        falling back to ``get_or_none`` and bumping ``hit_count``. Race-handling
        contract is in T5-01 ``STEP_CITATION_ENFORCEMENT``.
        """
        ...

    @staticmethod
    async def bump_hit(
        session: AsyncSession,
        *,
        cache_id: int,
    ) -> None:
        """UPDATE last_hit_at = now(), hit_count = hit_count + 1. Flushes."""
        ...

    @staticmethod
    async def invalidate_by_citation(
        session: AsyncSession,
        *,
        message_version_id: int,
    ) -> int:
        """DELETE every cache row whose citation_ids JSONB array contains
        ``message_version_id`` (PostgreSQL: ``citation_ids @> :id``).

        Returns rowcount (number of cache rows invalidated).

        Used by:
          - ``forget_cascade._cascade_llm_synthesis_cache`` (§8 layer 1)
          - ``llm_gateway.synthesize_answer`` STEP_FORGET_INVALIDATION_GATE (§3.6 step 3)
        """
        ...
```

`invalidate_by_citation` is consumed in TWO places — both the cascade layer (FIRST in §8) and the gateway pre-call gate (§3.6 step 3). Returning `int` (rowcount) is binding because the gateway uses zero-vs-non-zero to decide whether to log `forget_invalidated_count` in operator metrics; a `None` return would conflate "no rows matched" with "operation skipped".

### §5.3 Concurrency contract

* **Flush-only, caller-commits.** Both repos NEVER call `await session.commit()`. The orchestration transaction is owned by the caller. This matches the established pattern in `bot/db/repos/qa_trace.py` (Phase 4) and `bot/db/repos/forget_event.py` (Phase 3).
* **Advisory-lock pattern in budget guard.** `STEP_BUDGET_GUARD_ATOMIC` (§3.6 step 5) wraps the read-and-decide in `pg_advisory_xact_lock(LLM_BUDGET_LOCK_ID)`:

  ```python
  LLM_BUDGET_LOCK_ID = int.from_bytes(
      hashlib.sha256(b"llm_budget_guard").digest()[:8],
      "big",
      signed=True,
  )
  ```

  This derivation is binding and stable across deploys. Rotating the lock id requires a coordinated migration. The lock is transaction-scoped (`xact`), released on commit/rollback automatically.
* **Per-call transaction lifetime.** T5-04 handler opens a single AsyncSession per `/recall` invocation. `synthesize_answer` runs entirely inside that session. Splitting across multiple sessions would break the placeholder-then-update pattern.

---

## §6. Handler integration contract (T5-04 produces)

### §6.1 Step ORDER (binding)

In `bot/handlers/qa.py::recall_handler` (current name; see file `bot/handlers/qa.py:104`), when `memory.qa.llm_synthesis.enabled = True` AND `bundle.is_non_empty`, the handler MUST execute the following four steps in this exact order:

1. **Create `QaTrace` FIRST.** Call `QaTraceRepo.create(session, user_tg_id=..., chat_id=..., query=..., evidence_ids=bundle.evidence_ids, abstained=False, redact_query=redact_query)`. Capture `qa_trace_id = trace.id`. The new LLM columns (§7) remain NULL initially. This step is split out as `_create_trace_pending(...)` per PHASE5_PLAN.md §5.E.
2. **Call `synthesize_answer`** with `qa_trace_id=qa_trace_id` (REQUIRED). Receive `result: SynthesisResult`. The required parameter ensures every ledger row inside the gateway has its `qa_trace_id` populated, which is what makes the cascade layers in §8 join correctly via either FK direction.
3. **UPDATE `QaTrace`** with `llm_call_id`, `llm_response_summary`, `cost_usd`, `llm_response_redacted` from the result.
   * On `AnswerWithCitations`: set `llm_call_id=result.llm_call_id`, `llm_response_summary=result.answer_text`, `cost_usd=result.cost_usd`, `llm_response_redacted=False`.
   * On `Abstention`: set `llm_call_id=result.llm_call_id`, `llm_response_summary=None`, `cost_usd=result.cost_usd` (typically zero), `llm_response_redacted=False`.
   This step is split out as `_finalize_trace(...)` per PHASE5_PLAN.md §5.E.
4. **Render** `AnswerWithCitations` template OR fall back to Phase 4 evidence list on `Abstention`. Template name: `qa_synthesized.html` (T5-04 ships it). The Phase 4 fallback uses `_format_response(bundle, users_by_id)` verbatim from current `bot/handlers/qa.py:63`.

This ordering is enforced by tests in `tests/handlers/test_qa_llm_synthesis.py`. At least one regression test asserts that a `QaTrace` row exists in the DB BEFORE `synthesize_answer` is observed by a gateway spy — catches a future refactor that reverts to "create-trace-after-synthesis".

### §6.2 Flag-OFF byte-for-byte preservation

When `memory.qa.llm_synthesis.enabled = False`, the handler output (Telegram reply text + parse_mode + disable_web_page_preview + audit trace shape) MUST be byte-identical to Phase 4 behavior. The current Phase 4 path (lines 181-209 of `bot/handlers/qa.py` on `main`) is:

```python
result = await run_qa(session, query=query, chat_id=message.chat.id, redact_query_in_audit=redact_query)
# ... user lookup loop ...
await message.reply(_format_response(result.bundle, users_by_id), parse_mode="HTML", disable_web_page_preview=True)
await _write_trace(session, user_tg_id=..., chat_id=..., query=..., evidence_ids=result.bundle.evidence_ids, abstained=result.bundle.abstained, redact_query=result.query_redacted)
```

T5-04 introduces a flag check immediately before the `run_qa` line. When OFF, the path above runs unchanged. When ON (and bundle non-empty), the four-step §6.1 sequence runs INSTEAD — but the audit trace must still be created in step 1 (now with `evidence_ids` populated and `abstained=False`).

Regression tests in `tests/handlers/test_qa_recall_phase4_preserved.py` (T5-04 ships) assert byte-identity for at least: (a) abstention reply text, (b) non-empty evidence reply text, (c) audit trace row shape (all Phase 4 columns), (d) `parse_mode` and `disable_web_page_preview` arguments to `message.reply`.

---

## §7. `qa_traces` extension contract (T5-04 produces — alembic 025)

DDL (verbatim from PHASE5_PLAN.md §5.D):

```sql
ALTER TABLE qa_traces
  ADD COLUMN llm_call_id BIGINT NULL REFERENCES llm_usage_ledger(id) ON DELETE SET NULL,
  ADD COLUMN llm_response_summary TEXT NULL,
  ADD COLUMN llm_response_redacted BOOLEAN NULL DEFAULT FALSE,
  ADD COLUMN cost_usd NUMERIC(10, 6) NULL;

CREATE INDEX ix_qa_traces_llm_call_id ON qa_traces(llm_call_id);
```

ORM extension (`bot/db/models.py::QaTrace` — currently lines 500-528 — gets four new columns):

```python
class QaTrace(Base):
    __tablename__ = "qa_traces"
    __table_args__ = (
        Index("ix_qa_traces_user_tg_id", "user_tg_id"),
        Index("ix_qa_traces_chat_id_created_at", "chat_id", "created_at"),
        Index("ix_qa_traces_llm_call_id", "llm_call_id"),  # NEW
    )

    # ... existing columns 507-528 unchanged ...

    # NEW columns (alembic 025):
    llm_call_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("llm_usage_ledger.id", ondelete="SET NULL"),
        nullable=True,
    )
    llm_response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_response_redacted: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        default=False,
        server_default="false",
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
```

Repo extension (`bot/db/repos/qa_trace.py::QaTraceRepo.create`) — extends current signature with optional kwargs (PHASE5_PLAN.md §5.D):

```python
class QaTraceRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_tg_id: int,
        chat_id: int,
        query: str,
        evidence_ids: list[int],
        abstained: bool,
        redact_query: bool,
        # NEW optional kwargs (alembic 025):
        llm_call_id: int | None = None,
        llm_response_summary: str | None = None,
        llm_response_redacted: bool | None = False,
        cost_usd: Decimal | None = None,
    ) -> QaTrace: ...

    @staticmethod
    async def update_llm_fields(
        session: AsyncSession,
        *,
        trace_id: int,
        llm_call_id: int,
        llm_response_summary: str | None,
        llm_response_redacted: bool,
        cost_usd: Decimal,
    ) -> int:
        """UPDATE the four new LLM columns. Used by handler step 3 (§6.1).

        Returns rowcount (must be 1). Flushes; caller commits.
        """
        ...
```

Binding details:

* `llm_call_id` is `BIGINT NULL REFERENCES llm_usage_ledger(id) ON DELETE SET NULL`. Setting NULL on parent delete preserves the QaTrace row for audit.
* All four new columns are `NULL`-able so backward compatibility is preserved: existing Phase 4 code paths that call `QaTraceRepo.create` without the new kwargs produce identical row shape (the new columns default to NULL or `FALSE` as specified).
* `update_llm_fields` is the new repo method invoked by §6.1 step 3.

---

## §8. Cascade layer contracts (T5-04 produces; T5-04 self-consumes)

Three new layers added to `bot/services/forget_cascade.CASCADE_LAYER_ORDER`, AFTER the existing `qa_traces` layer (Wave 0 introduced) and in the binding order: cache → traces-llm → ledger.

The current order (per `bot/services/forget_cascade.py:57-65` after Wave 0 lands):

```python
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "qa_traces",                        # Wave 0 — already present in repo
    "llm_synthesis_cache",              # NEW — Phase 5 layer 1 (FIRST among Phase 5 layers)
    "qa_traces_llm",                    # NEW — Phase 5 layer 2
    "llm_usage_ledger",                 # NEW — Phase 5 layer 3 (LAST among Phase 5 layers)
    "message_entities",
    "message_links",
    "attachments",
    "fts_rows",
)
```

The append order is binding because `qa_traces_llm` depends on `llm_synthesis_cache` having been invalidated first (otherwise a parallel `synthesize_answer` could re-populate cache from a half-redacted trace).

### §8.1 Layer 1: `_cascade_llm_synthesis_cache`

Runs FIRST among Phase 5 layers. Per `target_type`:

* **`target_type='message'`** — Resolve `message_version_id = chat_messages.current_version_id` for the forgotten `chat_message`. Call `SynthesisCacheRepo.invalidate_by_citation(message_version_id)`. Returns rowcount.
* **`target_type='message_hash'`** — Resolve all `message_version_id`s by joining `chat_messages.content_hash = forget_event.target_id`. For each id, call `invalidate_by_citation`. Sum rowcounts.
* **`target_type='user'`** — Bulk DELETE every `llm_synthesis_cache` row whose `citation_ids JSONB` array intersects ANY of the user's `message_version_id`s. PostgreSQL implementation:

  ```sql
  DELETE FROM llm_synthesis_cache
  WHERE EXISTS (
      SELECT 1 FROM jsonb_array_elements(citation_ids) AS cid
      WHERE (cid)::int IN (
          SELECT mv.id FROM message_versions mv
          JOIN chat_messages cm ON cm.id = mv.chat_message_id
          WHERE cm.user_id = :telegram_id
      )
  );
  ```

  Returns rowcount.

Pre-condition: layer applicability MUST be guarded via `_LAYER_APPLICABLE_TARGET_TYPES` in `forget_cascade.py:69`. Add entry: `"llm_synthesis_cache": frozenset({"message", "message_hash", "user"})`. The `'export'` target type is skipped per existing dispatcher logic (lines 309).

### §8.2 Layer 2: `_cascade_qa_traces_llm`

Runs SECOND. Uses BOTH FK directions because handler-side trace creation (per §3.1 `qa_trace_id` REQUIRED) populates `qa_traces.llm_call_id` AND `llm_usage_ledger.qa_trace_id` symmetrically.

* **`target_type='message'`** — NULL `llm_response_summary` on traces whose `evidence_ids JSONB` array contains the target message's `message_version_id`. Implementation:

  ```sql
  UPDATE qa_traces SET llm_response_summary = NULL
  WHERE evidence_ids @> to_jsonb(ARRAY[:message_version_id]);
  ```

* **`target_type='message_hash'`** — Same shape as `'message'`, but iterate over all `message_version_id`s with the matching `content_hash`.
* **`target_type='user'`** — NULL both `query_text` (already covered by Wave 0 `_cascade_qa_traces`, idempotent re-run safe) AND `llm_response_summary` for `qa_traces.user_tg_id = telegram_id`. Implementation:

  ```sql
  UPDATE qa_traces SET llm_response_summary = NULL
  WHERE user_tg_id = :telegram_id;
  ```

Add entry: `"qa_traces_llm": frozenset({"message", "message_hash", "user"})` to `_LAYER_APPLICABLE_TARGET_TYPES`.

Audit-row preservation: `qa_traces` row itself is NEVER deleted; only the `llm_response_summary` content is NULLed. This preserves the Phase 4 audit trail.

### §8.3 Layer 3: `_cascade_llm_usage_ledger`

Runs LAST among Phase 5 layers. Only PII fields touched; cost/token aggregates required for budget audit are PRESERVED.

* **`target_type='user'`** — NULL `prompt_hash` and `response_hash` for ledger rows where `qa_trace_id IN (subquery: user's traces)`. Implementation:

  ```sql
  UPDATE llm_usage_ledger
  SET prompt_hash = NULL, response_hash = NULL
  WHERE qa_trace_id IN (SELECT id FROM qa_traces WHERE user_tg_id = :telegram_id);
  ```

  Note: `prompt_hash CHAR(64) NOT NULL` per §4.1, so the column is technically `NOT NULL`. The cascade either (a) overwrites with a sentinel sha256 (e.g., `sha256(b"<redacted>")`) or (b) the migration 024 schema relaxes `prompt_hash` to nullable. **Resolution**: T5-02 (alembic 024) ships `prompt_hash CHAR(64) NULL` (NOT `NOT NULL`) so the cascade can write `NULL` directly. This is a contract amendment to PHASE5_PLAN.md §5.B which will be reflected in the implemented migration; flagged in §11 if orchestrator wants the alternative sentinel approach.
* **`target_type='message'` / `'message_hash'`** — No-op. Ledger rows aggregate per call, not per message; per-message redaction is impossible. Cascade records `{"status": "completed", "rows": 0, "reason": "not_applicable"}` (matching the existing `_LAYER_APPLICABLE_TARGET_TYPES` pattern in `forget_cascade.py:337-343`).

Add entry: `"llm_usage_ledger": frozenset({"user"})` to `_LAYER_APPLICABLE_TARGET_TYPES`.

### §8.4 `_LAYER_FUNCS` registration

In `forget_cascade.py:276-280`:

```python
_LAYER_FUNCS: dict[str, Any] = {
    "chat_messages": _cascade_chat_messages,
    "message_versions": _cascade_message_versions,
    "qa_traces": _cascade_qa_traces,
    "llm_synthesis_cache": _cascade_llm_synthesis_cache,    # NEW
    "qa_traces_llm": _cascade_qa_traces_llm,                # NEW
    "llm_usage_ledger": _cascade_llm_usage_ledger,          # NEW
}
```

All three new layers MUST land in the SAME PR as alembic 025 (T5-04 bundled migration + handler PR). Stop signal #8 in PHASE5_PLAN.md §8 enforces this.

---

## §9. Eval seam (T5-05 produces; Phase 11 consumes)

Fixture format spec for `tests/fixtures/qa_llm_eval_cases.json`:

```json
[
  {
    "id": "eval-001-empty-bundle",
    "description": "Empty bundle short-circuits to abstention",
    "query": "что такое kotlin?",
    "evidence_message_version_ids": [],
    "expected_outcome": "abstention",
    "expected_abstention_reason": "empty_bundle",
    "expected_citation_subset_of": [],
    "expected_cost_usd_max": "0.00"
  },
  {
    "id": "eval-002-citation-hallucination",
    "description": "Provider returns citation_id NOT in bundle -> rejected",
    "query": "когда стартовал проект?",
    "evidence_message_version_ids": [101, 204, 388],
    "provider_fixture": "tests/fixtures/qa_llm_provider_hallucination.json",
    "expected_outcome": "abstention",
    "expected_abstention_reason": "provider_error",
    "expected_ledger_error": "citation_hallucination",
    "expected_cost_usd_max": "0.10"
  },
  {
    "id": "eval-003-cache-hit",
    "description": "Cache hit serves answer without provider call",
    "query": "идентичный запрос",
    "evidence_message_version_ids": [101],
    "preseed_cache": {
      "input_hash": "<sha256 of normalized inputs>",
      "answer_text": "ответ из кэша",
      "citation_ids": [101]
    },
    "expected_outcome": "answer",
    "expected_cache_hit": true,
    "expected_cost_usd_max": "0.00",
    "expected_citation_subset_of": [101]
  }
]
```

Field schema:

| Field | Type | Required | Semantics |
|-------|------|----------|-----------|
| `id` | string | yes | Unique case identifier; stable across runs. |
| `description` | string | yes | Human-readable case purpose. |
| `query` | string | yes | Verbatim `/recall` query. |
| `evidence_message_version_ids` | `list[int]` | yes | Bundle ids; may be empty. |
| `expected_outcome` | `Literal["answer", "abstention"]` | yes | Discriminates result type. |
| `expected_abstention_reason` | `Literal[...]` | conditional | Required if `expected_outcome=='abstention'`. One of the §3.3 reasons. |
| `expected_ledger_error` | string | conditional | The exact `error` value expected in the ledger row (e.g., `'citation_hallucination'`, `'provider_unknown:NewSDKError'`). |
| `expected_citation_subset_of` | `list[int]` | yes | Test asserts `set(result.citation_ids).issubset(set(field))`. |
| `expected_cost_usd_max` | string (Decimal-parseable) | yes | Upper bound on `result.cost_usd`. |
| `expected_cache_hit` | bool | optional | If present, asserts `result.cache_hit == field`. |
| `provider_fixture` | path string | optional | Pre-recorded provider response JSON for deterministic replay. |
| `preseed_cache` | object | optional | Cache row to insert before test runs. |

Coordination contract: **Phase 11 (Orchestrator C) consumes the same fixtures verbatim** for regression-suite generation (per AUTHORIZED_SCOPE.md §"Authorized: Phase 11"). Any field rename in this schema requires cross-orchestrator notification per PHASE5_PLAN.md §9 step 7.

T5-05 ships at least 6 cases covering (per PHASE5_PLAN.md §7 Wave 3 acceptance): empty-bundle abstention, all-filtered abstention, budget-exceeded abstention, provider-error abstention (one per sub-category if feasible), cache-hit reproduces answer, citation-hallucination rejection.

---

## §10. Mock contracts (T5-01 uses BEFORE T5-02 / T5-03 land)

Wave 1 parallelism rule: T5-01 (gateway) and T5-02 (schema) ship in parallel; T5-01 cannot wait for T5-03 (real repos in Wave 2). Therefore T5-01 mocks the repos via Protocol classes defined in `bot/services/llm_gateway.py`. T5-03 implementations MUST satisfy these Protocols verbatim.

### §10.1 `LedgerRepoProtocol`

```python
class LedgerRepoProtocol(Protocol):
    async def record(
        self,
        session: AsyncSession,
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
    ) -> LlmUsageLedger: ...

    async def daily_cost_usd(
        self,
        session: AsyncSession,
        *,
        day: date,
    ) -> Decimal: ...

    async def monthly_cost_usd(
        self,
        session: AsyncSession,
        *,
        year: int,
        month: int,
    ) -> Decimal: ...

    async def update_placeholder(
        self,
        session: AsyncSession,
        *,
        ledger_id: int,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: str | None,
        response_hash: str | None,
        error: str | None,
    ) -> int: ...
```

### §10.2 `SynthesisCacheRepoProtocol`

```python
class SynthesisCacheRepoProtocol(Protocol):
    async def get_or_none(
        self,
        session: AsyncSession,
        *,
        input_hash: str,
    ) -> LlmSynthesisCache | None: ...

    async def store(
        self,
        session: AsyncSession,
        *,
        input_hash: str,
        answer_text: str,
        citation_ids: list[int],
        model: str,
    ) -> LlmSynthesisCache: ...

    async def bump_hit(
        self,
        session: AsyncSession,
        *,
        cache_id: int,
    ) -> None: ...

    async def invalidate_by_citation(
        self,
        session: AsyncSession,
        *,
        message_version_id: int,
    ) -> int: ...
```

### §10.3 Wiring contract

* T5-01 declares both Protocols at the top of `bot/services/llm_gateway.py`. The `synthesize_answer` function accepts repo instances via dependency injection (NOT module-level imports of `LedgerRepo` / `SynthesisCacheRepo`). Recommended: a `GatewayDependencies` dataclass:

  ```python
  @dataclass(frozen=True)
  class GatewayDependencies:
      ledger_repo: LedgerRepoProtocol
      cache_repo: SynthesisCacheRepoProtocol
      provider: Provider
  ```

  `synthesize_answer` takes `deps: GatewayDependencies` as an additional kwarg. T5-01 tests inject mocks; T5-04 wires real repos via:

  ```python
  deps = GatewayDependencies(
      ledger_repo=LedgerRepo(),
      cache_repo=SynthesisCacheRepo(),
      provider=AnthropicProvider() if config.provider == "anthropic" else OpenAIProvider(),
  )
  ```

* T5-03 real implementations MUST satisfy the Protocols structurally — Python's `Protocol` runtime check is informal but `mypy --strict` MUST pass. T5-03 tests include a `mypy --strict` step on the repo modules.
* T5-02 schema PR is a hard prerequisite for T5-03 (the ORM models that the Protocols return are defined there). T5-01 imports `LlmUsageLedger` and `LlmSynthesisCache` from `bot.db.models` for type hints; if T5-02 is not yet merged, T5-01 uses `from typing import TYPE_CHECKING` + string annotations to avoid the import at runtime.

---

## §11. Change discipline

Any change to a contract in §3 through §9 requires:

1. **Orchestrator approval.** Surface the proposed change as a comment on the Phase 5 epic issue and tag Orchestrator A. Wait for explicit ACK before any code change.
2. **Update to all consuming streams.** The change MUST be reflected in this document in the same PR. Streams currently in flight (`feat/p5-w1-gateway`, `feat/p5-w1-schema`, `feat/p5-w2-repo-handler`, `feat/p5-w3-evals`) MUST receive a coordinated rebase or amendment. The PR opener owns this synchronization.
3. **Re-dispatch of affected implementers.** If an in-flight implementer's work is invalidated by the change, the orchestrator re-dispatches with an updated brief. Ad-hoc patching of a stale branch is forbidden.

Contracts are FROZEN until Phase 5 closes (per PHASE5_PLAN.md §9 step 7 closure update). Changes that affect Phase 11 fixture schema (§9) additionally require notifying Orchestrator C BEFORE merge per AUTHORIZED_SCOPE.md §"Authorized: Phase 11" coordination point.

After Phase 5 closure, this document transitions to historical reference. Phase 6 (cards) introduces its own contracts file under `docs/memory-system/phase6/`; it MAY consume `synthesize_answer` but MUST NOT alter the surface defined here without re-opening Phase 5.

---

**Sealed by:** Orchestrator A — 2026-05-02.
**Source of truth:** `docs/memory-system/PHASE5_PLAN.md` §5 + §7 + §11.
**Cross-references:** `docs/memory-system/HANDOFF.md` §1 (invariants 2, 3, 9), `docs/memory-system/AUTHORIZED_SCOPE.md` §"Authorized: Phase 5".
