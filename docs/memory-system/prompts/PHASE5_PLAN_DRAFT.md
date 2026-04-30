# 🚧 DRAFT — NOT AUTHORIZED
Phase 5 requires AUTHORIZED_SCOPE.md update + design ratification before any implementation begins.

# Phase 5 — LLM Synthesis Gateway + Usage Ledger: Design & Stream Plan

**Status:** draft only. Phase 5 is **not authorized** in `AUTHORIZED_SCOPE.md`.
**Cycle:** Memory system Phase 5 candidate design.
**Date:** 2026-04-30.
**Predecessor:** Phase 4 `/recall` must be closed first: `EvidenceBundle`, `SearchHit` 9-field shape, and `qa_traces` must be stable.
**Migration window:** 022+ for `llm_usage_ledger`, 023+ for `qa_traces` LLM extensions.
**Critical invariant for this phase:** every provider call goes through `bot/services/llm_gateway.py`, is governance-filtered, cited, cost-guarded, cached safely, and audited.

## 0. Implementation Status

TBD — will fill in once Phase 5 work begins.

Current grounding notes from required reading:

- `AUTHORIZED_SCOPE.md` still says Phase 5 is not authorized; LLM calls of any kind are blocked until the phase gate is explicitly updated.
- `ROADMAP.md` Phase 5 row names "LLM gateway + extraction (events / observations / candidates)" and requires every LLM call to be logged in a ledger and no forbidden source sent to LLM.
- `PHASE4_PLAN.md` is present in `.worktrees/p4-prompts/docs/memory-system/PHASE4_PLAN.md`; it is not present at `docs/memory-system/PHASE4_PLAN.md` on root `main`. This draft models the §0-§10 structure from the available Phase 4 plan artifact.
- `bot/services/evidence.py` is not yet present in the inspected tree — designed from the Phase 4 EvidenceBundle contract spec.
- `bot/db/repos/qa_trace.py` is not yet present in the inspected tree — designed from the Phase 4 qa_traces audit pattern spec.
- `bot/services/search.py` in the Phase 4 worktree already exposes the 9-field `SearchHit` shape: `message_version_id`, `chat_message_id`, `chat_id`, `message_id`, `user_id`, `snippet`, `ts_rank`, `captured_at`, `message_date`.

## 1. Non-Negotiable Invariants

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.
10. Public wiki remains disabled until review / source trace / governance are proven.

## 2. Phase 5 Spec

Canonical HANDOFF.md §2 currently defines Phase 5 as extraction, not only answer synthesis:

- **Objective:** create structured pre-catalog memory.
- **Scope:** `llm_usage_ledger`, `extraction_runs`, `memory_events`, `observations`,
  `reflection_runs`, `memory_candidates`.
- **Dependencies:** phase 4 + `llm_gateway`.
- **Entry criteria:** governance filters, evidence bundle, ledger, budget guard.
- **Exit criteria:** high-signal windows produce sourced candidates.
- **Acceptance:** no forbidden source sent to LLM; every output has source refs.
- **Risks:** hallucinated extraction, budget runaway.
- **Rollback:** derived rows rebuildable / deletable.

This draft narrows the first Phase 5 implementation wave to the design brief's LLM synthesis gateway for `/recall`, because it is the smallest gate that can satisfy "LLM gateway, ledger, source validation, budget guard" before extraction candidates. Humans must ratify whether this is Phase 5a, or whether the canonical extraction tables must be included before the phase is considered started.

Design-brief objective for this draft:

- Add audited LLM answer synthesis on top of the sealed Phase 4 `EvidenceBundle`.
- Keep raw Phase 4 behavior as the default through `memory.qa.llm_synthesis.enabled = false`.
- Return an `AnswerWithCitations` that cites only `message_version_id` values present in the bundle.
- Add cost guardrails, hash-based caching, provider abstraction, and usage ledger before any provider call is allowed.

## 3. Phase 6 Boundary

What Phase 5 must NOT implement:

- Knowledge cards → Phase 6. Not in scope: `memory_items`, `knowledge_cards`, `card_sources`, `card_relations`, admin card approval UI, active-card status transitions. HANDOFF epic references: E16 and E17.
- Summaries → Phase 7. Not in scope: `summaries`, `summary_sources`, daily draft generation, review/publish workflow, summary redaction cascade. HANDOFF epic reference: E18.
- Wiki → Phase 9. Not in scope: member catalog pages, internal wiki pages, public wiki, digest archive pages, visibility-filtered browse/search UI. HANDOFF epic reference: E20.
- Graph → Phase 10. Not in scope: Neo4j, Graphiti, `graph_sync_runs`, graph sidecar, incremental/full graph rebuilds, graph forget purge. HANDOFF epic reference: E21.

Concrete ticket-boundary notes:

- T5-01 may define `AnswerWithCitations`; it must not define card/source tables.
- T5-04 may store `qa_traces.llm_response_summary`; it must not create daily summary rows.
- T5-05 may add evaluation fixtures for citation correctness; it must not implement Shkoderbench persistence tables from Phase 11.
- Any extraction tables from canonical HANDOFF Phase 5 (`extraction_runs`, `memory_events`, `observations`, `reflection_runs`, `memory_candidates`) are not included in this synthesis-first draft unless humans ratify expanding scope.

## 4. Architecture Overview

```
Feature flag gate:
  memory.qa.llm_synthesis.enabled
  default OFF
  OFF => existing Phase 4 raw evidence / abstention behavior
  ON  => audited synthesis path below

Telegram /recall
  |
  v
bot/handlers/qa.py
  |
  v
run_qa(session, query)
  |
  v
EvidenceBundle
  Phase 4 contract, sealed:
  - items from governance-filtered SearchHit rows
  - evidence_ids are message_version_id values
  - empty bundle means abstention
  |
  v
llm_gateway.synthesize_answer(
    session,
    bundle,
    query,
    model="claude-haiku-4-5-20251001",
    max_tokens=<configured ceiling>,
)
  |
  +--> cost guardrail check BEFORE provider call
  |      feature_flags:
  |        llm.cost_ceiling.daily_usd
  |        llm.cost_ceiling.monthly_usd
  |      ledger totals:
  |        LedgerRepo.daily_cost_usd(...)
  |        LedgerRepo.monthly_cost_usd(...)
  |
  +--> hash-based cache check BEFORE provider call
  |      key = sha256(system_prompt + user_prompt + model + sorted(bundle.evidence_ids))
  |      on cache hit:
  |        inspect cached citations
  |        if any cited message_version_id is in forget_events => invalidate and abstain/recompute
  |
  +--> provider abstraction
  |      SYNTHESIS_PROVIDER=anthropic default
  |      anthropic primary
  |      openai fallback
  |      no streaming responses
  |
  v
AnswerWithCitations
  - text
  - citations subset of bundle.evidence_ids
  - tokens/cost/model/provider/request_id/latency
  - is_abstention
  |
  v
Render response with formatted citations
  |
  v
Audit writes:
  - llm_usage_ledger row ALWAYS
    provider errors, cache hits, cost-refusals, and abstentions included
  - qa_traces.llm_response_summary WHEN QA context exists
```

## 5. Component Design

### 5.A. LLM gateway (Stream A → T5-01)

**What to build**

Single chokepoint for all LLM calls. It consumes only `EvidenceBundle`, produces `AnswerWithCitations`, enforces prompt discipline, budget checks, cache checks, provider selection, and ledger writes.

**File paths**

- `bot/services/llm_gateway.py`
- `tests/services/test_llm_gateway.py`
- Optional fixture: `tests/fixtures/llm_gateway_cache_v1.json`

**Public API sketch**

```python
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle


@dataclass(frozen=True, slots=True)
class AnswerWithCitations:
    text: str
    citations: tuple[int, ...]
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    model: str
    latency_ms: int
    provider: str
    request_id: str | None
    is_abstention: bool


class SynthesisProvider(Protocol):
    name: str

    async def synthesize(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
    ) -> AnswerWithCitations: ...


class AnthropicProvider:
    name: str = "anthropic"

    async def synthesize(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
    ) -> AnswerWithCitations: ...


class OpenAIProvider:
    name: str = "openai"

    async def synthesize(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
    ) -> AnswerWithCitations: ...


async def synthesize_answer(
    session: AsyncSession,
    bundle: EvidenceBundle,
    query: str,
    *,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int,
) -> AnswerWithCitations: ...
```

**Design decisions**

- Provider chosen via `SYNTHESIS_PROVIDER`; default `anthropic`; fallback provider is `openai`.
- Default model is `claude-haiku-4-5-20251001` for fast/cheap synthesis.
- System prompt forbids "knowledge from training"; answer must be synthesized only from bundle items and cite via `message_version_id`.
- `citations` must be a subset of `bundle.evidence_ids`; otherwise return abstention and log provider-contract violation.
- Empty bundle returns abstention without provider call, but still writes a ledger row.
- Cost guardrail reads feature flag config keys `llm.cost_ceiling.daily_usd` and `llm.cost_ceiling.monthly_usd`; if current ledger total is over either ceiling, refuse with an abstention answer.
- Hash-based cache key is `sha256(system_prompt + user_prompt + model + sorted(bundle.evidence_ids))`.
- Cache hit is valid only if none of the cached cited `message_version_id` values are in `forget_events`.
- Catches all provider errors and returns abstention; errors never raise out to handlers.
- Writes `llm_usage_ledger` on every call, including cache hits, cost refusals, provider errors, and empty-bundle abstentions.
- No streaming responses in Phase 5.

**Acceptance criteria**

- Unit tests prove empty bundle returns `is_abstention=True` and does not call a provider.
- Unit tests prove `SYNTHESIS_PROVIDER` selects Anthropic by default and OpenAI when configured.
- Unit tests prove provider errors return abstention and still call `LedgerRepo.record(...)`.
- Unit tests prove citations outside `bundle.evidence_ids` are rejected.
- Unit tests prove daily and monthly cost ceilings block provider calls.
- Unit tests prove cache hit writes a ledger row with `cache_hit=True`.
- Unit tests prove cache hit invalidates when any cited `message_version_id` has a matching `forget_events` tombstone.
- No imports or provider calls exist outside `bot/services/llm_gateway.py`.

**Risks**

- Provider SDK response shapes may differ; wrapper must normalize strictly.
- Feature flag table currently stores booleans/config JSON, so cost ceiling value location must be ratified.
- Cache storage is not specified in existing schema; this draft assumes a minimal internal cache table or deterministic in-memory test double, requiring ratification before implementation.

### 5.B. LLM usage ledger schema (Stream B → T5-02)

**What to build**

Alembic migration adding durable audit for every gateway attempt.

**File paths**

- `alembic/versions/022_add_llm_usage_ledger.py`
- `bot/db/models.py`
- `tests/db/test_llm_usage_ledger_schema.py`

**Public API sketch**

```python
class LlmUsageLedger(Base):
    __tablename__ = "llm_usage_ledger"
```

**Schema**

```sql
CREATE TABLE llm_usage_ledger (
  id BIGSERIAL PRIMARY KEY,
  qa_trace_id BIGINT NULL REFERENCES qa_traces(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_hash CHAR(64) NOT NULL,
  response_hash CHAR(64) NULL,
  tokens_in INTEGER NOT NULL,
  tokens_out INTEGER NOT NULL,
  cost_usd NUMERIC(10,6) NOT NULL,
  latency_ms INTEGER NOT NULL,
  request_id TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  error TEXT NULL,
  cache_hit BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX ix_llm_usage_ledger_qa_trace_id ON llm_usage_ledger (qa_trace_id);
CREATE INDEX ix_llm_usage_ledger_model_created_at ON llm_usage_ledger (model, created_at);
CREATE INDEX ix_llm_usage_ledger_created_at ON llm_usage_ledger (created_at);
```

**Acceptance criteria**

- Migration 022 applies cleanly after Phase 4 `qa_traces` migration.
- Migration 022 rolls back cleanly by dropping indexes and table.
- `qa_trace_id` accepts NULL for non-QA future calls and references `qa_traces(id)` when present.
- `cache_hit` defaults to false.
- `cost_usd` preserves six decimal places.
- Metadata smoke test verifies model/table/index presence.

**Risks**

- If Phase 4 `qa_traces` migration number is not 021, `down_revision` must be adjusted before implementation.
- If `qa_traces` is not present when Phase 5 starts, this migration must wait or split nullable FK addition into a later migration.

### 5.C. LLM usage ledger repo (Stream C → T5-03)

**What to build**

Async repository for recording ledger rows and aggregating cost totals used by the gateway guardrail.

**File paths**

- `bot/db/repos/llm_usage_ledger.py`
- `tests/db/test_llm_usage_ledger_repo.py`

**Public API sketch**

```python
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import LlmUsageLedger


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
        error: str | None,
        cache_hit: bool,
    ) -> LlmUsageLedger: ...

    @staticmethod
    async def daily_cost_usd(
        session: AsyncSession,
        *,
        date: date,
    ) -> Decimal: ...

    @staticmethod
    async def monthly_cost_usd(
        session: AsyncSession,
        *,
        year: int,
        month: int,
    ) -> Decimal: ...
```

**Acceptance criteria**

- `record(...)` inserts and flushes but does not commit.
- `record(...)` accepts `qa_trace_id=None`.
- `daily_cost_usd(...)` returns `Decimal("0")` when no rows exist.
- `monthly_cost_usd(...)` returns only rows inside the requested UTC month.
- Cache hits and error rows are included in totals if `cost_usd > 0`; zero-cost abstentions do not inflate totals.
- Tests verify caller-owned transaction rollback removes uncommitted rows.

**Risks**

- Date boundary semantics must be UTC unless the project explicitly ratifies another timezone.
- Decimal arithmetic must avoid float conversion.

### 5.D. Q&A handler integration (Stream D → T5-04)

**What to build**

Extend Phase 4 `/recall` so LLM synthesis is opt-in. The default remains raw Phase 4 evidence rendering.

**File paths**

- `bot/handlers/qa.py`
- `bot/services/qa.py` if Phase 4 creates orchestration there
- `alembic/versions/023_extend_qa_traces_for_llm.py`
- `bot/db/models.py`
- `bot/db/repos/qa_trace.py`
- `tests/handlers/test_qa_llm_synthesis.py`

**Public API sketch**

```python
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle
from bot.services.llm_gateway import AnswerWithCitations


async def render_llm_answer(
    session: AsyncSession,
    *,
    answer: AnswerWithCitations,
    bundle: EvidenceBundle,
) -> str: ...


async def should_use_llm_synthesis(session: AsyncSession) -> bool: ...
```

Migration 023:

```sql
ALTER TABLE qa_traces
  ADD COLUMN llm_response_summary TEXT NULL,
  ADD COLUMN llm_response_redacted BOOLEAN NULL DEFAULT FALSE,
  ADD COLUMN cost_usd NUMERIC(10,6) NULL;
```

**Behavior**

- Feature flag: `memory.qa.llm_synthesis.enabled`, default OFF.
- When OFF: preserve existing Phase 4 behavior exactly.
- When ON: call `llm_gateway.synthesize_answer(...)` with the Phase 4 `EvidenceBundle`.
- Render only citations returned in `AnswerWithCitations.citations`, formatted against bundle items.
- If the gateway returns `is_abstention=True`, reply with the Phase 4 abstention or a concise evidence-only refusal.
- If the gateway raises unexpectedly despite its contract, log the error and fall back to raw Phase 4 evidence; do not surface provider errors to the user.
- When QA context exists, update/create `qa_traces.llm_response_summary`, `llm_response_redacted`, and `cost_usd`.

**Acceptance criteria**

- Feature flag OFF test proves no gateway call and Phase 4 output unchanged.
- Feature flag ON test proves gateway called with the exact `EvidenceBundle` returned by `run_qa(...)`.
- Gateway abstention test renders refusal and records audit.
- Gateway error test logs and falls back to raw evidence.
- Render test refuses citations not present in bundle.
- Migration 023 applies and rolls back cleanly.
- `qa_traces.llm_response_summary` is redacted/null when a forget cascade later targets the requesting user or cited content; if cascade behavior is not implemented in this ticket, an xfail test must document the gap.

**Risks**

- Phase 4 handler may not yet have a clean `run_qa(...)` seam; T5-04 must not rewrite Phase 4 behavior broadly.
- `qa_traces` may be in-flight; schema conflicts must stop work.

### 5.E. Eval harness extension (Stream E → T5-05)

**What to build**

Extend QA evals to compare synthesized answer citations against expected citations, while keeping unit tests provider-free.

**File paths**

- `tests/eval/test_qa_eval_cases.py`
- `tests/fixtures/qa_eval_cases.json`
- `tests/fixtures/qa_llm_eval_cases.json`
- Optional opt-in integration fixture: `tests/fixtures/qa_llm_gateway_fixture.json`

**Public API sketch**

```python
import pytest


def expected_citations_for_case(case: dict[str, object]) -> set[int]: ...


@pytest.mark.asyncio
async def test_qa_llm_eval_cases_use_expected_citations(db_session) -> None: ...


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_gateway_with_known_fixtures(db_session) -> None: ...
```

**Acceptance criteria**

- Unit eval mode mocks `synthesize_answer`; no real LLM calls.
- Citation-set comparison fails if answer cites anything outside expected set.
- Empty `EvidenceBundle.evidence_ids` expects `is_abstention=True`.
- Fixtures include at least one successful synthesis, one partial-citation answer, one empty-bundle abstention, one stale-citation invalidation case, and one cost-ceiling refusal case.
- Real gateway integration mode is opt-in via env var and skipped by default.
- CI does not require provider API keys.

**Risks**

- Citation set equality may be too strict if multiple evidence items support the same answer; fixtures should mark acceptable sets clearly.
- Real-provider integration can be flaky due to quota/rate limits; it must never be a default CI gate.

## 6. Stream Allocation

Phase 5 uses three waves. This mirrors Phase 4's parallel execution model where schema and sealed contracts can move first, integration waits for contracts, and eval hardening lands last.

### Wave 1 — parallel foundations

| Stream | Component | Owner ticket | Files touched | Deps |
|---|---|---|---|---|
| A | LLM gateway core | T5-01 | `bot/services/llm_gateway.py`, tests | Phase 4 `EvidenceBundle` contract |
| B | Ledger schema | T5-02 | `alembic/versions/022_*`, `bot/db/models.py`, tests | Phase 4 `qa_traces` table |

Rationale: gateway API and schema can be designed in parallel. Stream A can mock `LedgerRepo` until Stream C lands. Stream B owns migration ordering and should merge before repo implementation.

### Wave 2 — sequential integration foundation

| Stream | Component | Owner ticket | Files touched | Deps |
|---|---|---|---|---|
| C | Ledger repo | T5-03 | `bot/db/repos/llm_usage_ledger.py`, tests | T5-02 |
| D | Q&A handler integration + qa_traces migration | T5-04 | `bot/handlers/qa.py`, `bot/services/qa.py`, `alembic/versions/023_*`, tests | T5-01, T5-02, T5-03 |

Rationale: the gateway must write real ledger rows before handler integration can be accepted. Handler integration depends on both the gateway contract and the repo's cost aggregation.

### Wave 3 — eval harness

| Stream | Component | Owner ticket | Files touched | Deps |
|---|---|---|---|---|
| E | Eval harness extension | T5-05 | `tests/eval/test_qa_eval_cases.py`, fixtures | T5-04 |

Rationale: LLM evals need the final handler/gateway seam and `AnswerWithCitations` behavior. They should not shape the runtime design retroactively.

Wave summary:

```
Wave 1 (parallel):   A gateway core       B ledger schema
                         |                    |
                         v                    v
Wave 2 (sequential): C ledger repo  --->  D handler integration
                                             |
                                             v
Wave 3 (sequential): E eval harness
```

Phase 4 parallel model reused here:

- Independent schema drafting can run alongside service-contract work.
- Contract-first service modules unblock downstream handler work.
- Handler work waits for upstream contracts and migrations.
- Eval fixtures land after behavior is stable enough to avoid rewriting expected outputs.

## 7. Tickets T5-XX

| ID | Title | Stream | Depends | Files Changed | Acceptance Criteria | Risk |
|---|---|---|---|---|---|---|
| **T5-01** | `llm_gateway.py` core: `synthesize_answer`, provider abstraction, cost guardrail, caching | A | Phase 4 `EvidenceBundle` | `bot/services/llm_gateway.py`, `tests/services/test_llm_gateway.py`, optional cache fixture | `AnswerWithCitations` frozen dataclass exists; provider selected by `SYNTHESIS_PROVIDER`; default model `claude-haiku-4-5-20251001`; empty bundle abstains without provider call; provider errors abstain and do not raise; citations must be subset of `bundle.evidence_ids`; cost ceilings block calls; cache hit writes ledger with `cache_hit=True`; stale cache invalidates on forget_events | Provider SDK instability; unspecified cache persistence; cost flag shape needs ratification |
| **T5-02** | `llm_usage_ledger` schema migration (022) | B | Phase 4 `qa_traces` migration | `alembic/versions/022_add_llm_usage_ledger.py`, `bot/db/models.py`, `tests/db/test_llm_usage_ledger_schema.py` | Table has all required columns; indexes `(qa_trace_id)`, `(model, created_at)`, `(created_at)` exist; `qa_trace_id` nullable FK works; migration upgrade/downgrade clean; `cache_hit` default false; `cost_usd` precision is `numeric(10,6)` | Migration number/FK conflict if Phase 4 migrations are still in flight |
| **T5-03** | LedgerRepo implementation | C | T5-02 | `bot/db/repos/llm_usage_ledger.py`, `tests/db/test_llm_usage_ledger_repo.py` | `LedgerRepo.record(...)` flushes without commit; rollback removes uncommitted row; daily/monthly cost totals return `Decimal`; totals are UTC-bounded; zero-row totals return Decimal zero | Timezone boundary mistakes; accidental float conversion |
| **T5-04** | `qa.py` handler integration + `qa_traces` migration (023) | D | T5-01, T5-02, T5-03, Phase 4 `/recall` | `bot/handlers/qa.py`, `bot/services/qa.py`, `alembic/versions/023_extend_qa_traces_for_llm.py`, `bot/db/models.py`, `bot/db/repos/qa_trace.py`, `tests/handlers/test_qa_llm_synthesis.py` | `memory.qa.llm_synthesis.enabled` default OFF preserves Phase 4 output; ON calls gateway with exact bundle; gateway abstention handled; unexpected gateway error logs and falls back to raw evidence; rendered citations map to bundle; `qa_traces` has `llm_response_summary`, `llm_response_redacted`, `cost_usd` | Phase 4 handler seam may be unstable; qa_traces schema conflict |
| **T5-05** | Eval harness extension + integration test fixtures | E | T5-04 | `tests/eval/test_qa_eval_cases.py`, `tests/fixtures/qa_eval_cases.json`, `tests/fixtures/qa_llm_eval_cases.json` | Mocked unit evals compare answer citation set to expected set; empty bundle preserves abstention; no real LLM calls in unit tests; real gateway integration is opt-in via env and skipped by default; fixtures cover success, partial citation, empty bundle, stale cache, cost refusal | Flaky provider integration; overly rigid citation expectations |

## 8. Stop Signals

A stream MUST stop and surface the issue in the PR description or planning thread if any of these fire:

- LLM provider quota exhausted — stop provider-backed testing; keep unit tests mocked; do not switch providers silently.
- Cost ceiling breached, daily or monthly — gateway must return abstention; do not bypass the guardrail for manual testing.
- Cache invalidation loop detected from `forget_events` churn — disable synthesis cache path and surface design question.
- `EvidenceBundle` contract changed from Phase 4 — stop T5-01/T5-04 and update this plan before coding.
- `qa_traces` schema conflict with in-flight Phase 4 migration — stop T5-02/T5-04 until migration order is ratified.
- Any provider call appears outside `bot/services/llm_gateway.py` — stop and remove it.
- Any output cites an id not present in `bundle.evidence_ids` — stop; this is a citation integrity failure.
- Any synthesis path can see `#nomem`, `#offrecord`, redacted, or forgotten content — stop; this violates invariant #3.
- `AUTHORIZED_SCOPE.md` remains unchanged when implementation begins — stop; Phase 5 is not authorized.

## 9. PR Workflow

Phase 5 is draft-only until `AUTHORIZED_SCOPE.md` is updated and this plan is ratified. Once authorized:

1. One PR per wave:
   - Wave 1 PR-A: `feat/p5-llm-gateway-core` for T5-01.
   - Wave 1 PR-B: `feat/p5-llm-ledger-schema` for T5-02.
   - Wave 2 PR: `feat/p5-ledger-repo-qa-integration` for T5-03 + T5-04 only after Wave 1 merges.
   - Wave 3 PR: `feat/p5-llm-evals` for T5-05.
2. Branch naming: `feat/p5-<slug>` from `main`; no umbrella phase branch.
3. Merge order:
   - T5-02 before T5-03.
   - T5-01 before T5-04.
   - T5-03 before T5-04 acceptance.
   - T5-05 last.
4. Review gates:
   - Product/privacy review confirms no forbidden content reaches provider.
   - Technical review confirms no LLM imports outside `llm_gateway`.
   - Cost review confirms ledger rows are written for success, error, cache hit, and refusal.
   - Eval review confirms mocked unit tests do not need provider keys.
5. Evidence required in every PR:
   - Changed files list.
   - Tests run.
   - Feature flag default OFF proof for user-facing changes.
   - Ledger/audit behavior proof for LLM paths.
   - Residual risks.
6. Final holistic review required after Wave 3 before declaring Phase 5 closed, because this introduces provider calls and cost-bearing behavior.

## 10. Glossary

- **AnswerWithCitations:** frozen dataclass returned by `synthesize_answer`; contains answer text, cited `message_version_id` values, token/cost metadata, provider/model/request metadata, and abstention state.
- **LlmUsageLedger:** ORM model/table for durable audit of every gateway call, error, cache hit, and cost refusal.
- **synthesize_answer:** public gateway function that turns an `EvidenceBundle` and query into an `AnswerWithCitations`.
- **prompt_hash:** SHA-256 hash over system prompt, user prompt, model, and sorted evidence ids; used for caching and audit correlation.
- **cache_hit:** ledger boolean marking a response served from synthesis cache rather than a provider call.
- **abstention:** deliberate refusal to answer because evidence is empty, provider failed, cost ceiling is reached, citations are invalid, or the cache is stale.
- **cost_ceiling:** daily/monthly USD guardrail read from feature flags before any provider call.
- **SYNTHESIS_PROVIDER:** environment variable selecting the gateway provider; default `anthropic`, optional `openai` fallback.
- **llm_response_summary:** nullable `qa_traces` field containing the stored/redactable summary of the LLM answer for QA audit context.
