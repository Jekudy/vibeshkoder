# Phase 5 — LLM Gateway + Answer Synthesis (Ratified Plan)

**Status:** RATIFIED 2026-05-02 — promoted from `prompts/PHASE5_PLAN_DRAFT.md` by Orchestrator A.
**Owner:** Orchestrator A (synthesis chain Phase 5 → 6 → 7 → 8).
**Predecessor:** Phase 4 (hybrid search + Q&A with citations) — **implementation-closed but production-blocked** by issue #164. Wave 0 of this plan absorbs #164 as a prerequisite hotfix.
**Branch namespace:** `feat/p5-*`, `fix/p5-*`, `hotfix/p5-*`, `plan/p5-ratify` (this branch).
**Owned alembic range:** **023–049** (Wave 0 takes 023 backfill; Wave 1 takes 024 ledger; Wave 2 takes 025 qa_traces extension; remaining 026–049 reserved).
**Phase chain:** Phase 5 (synthesis) → Phase 6 (cards) → Phase 7 (digests) → Phase 8 (reflection / observations).

---

## §0. Implementation grounding (verified against `main` 2026-05-02)

**Phase 4 sealed contracts present on `main` and assumed by Phase 5:**

- `bot/services/search.py` — `SearchHit` 9-field frozen dataclass: `message_version_id, chat_message_id, chat_id, message_id, user_id, snippet, ts_rank, captured_at, message_date`. Phase 5 consumes via `EvidenceBundle`.
- `bot/services/evidence.py` — `EvidenceBundle` frozen dataclass with `from_hits` classmethod and `evidence_ids` property returning `list[int]` of `message_version_id`.
- `bot/services/qa.py` — `run_qa(session, *, query, chat_id, redact_query_in_audit, limit=3) -> QaResult(bundle, query_redacted)`. Phase 5 LLM synthesis sits AFTER `run_qa` (`bundle` is the input).
- `bot/handlers/qa.py` — `/recall` handler at `bot/handlers/qa.py:97`. Flag `memory.qa.enabled` (default OFF). `_write_trace` audit helper.
- `bot/db/repos/qa_trace.py` — `QaTraceRepo.create(session, *, user_tg_id, chat_id, query, evidence_ids, abstained, redact_query) -> QaTrace`. Phase 5 extends with `llm_response_summary` / `llm_response_redacted` / `cost_usd` / `llm_call_id`.
- `bot/services/forget_cascade.py` — `CASCADE_LAYER_ORDER` + `_LAYER_FUNCS`. Phase 5 adds layers for `qa_traces.llm_response_summary` and `llm_usage_ledger`.

**Latest alembic on `main` = `022_add_qa_traces.py`** (head). Phase 5 owns 023+.

**Phase 5 services / repos / handlers ABSENT on `main` (must be created):**
- `bot/services/llm_gateway.py`
- `bot/db/repos/llm_usage_ledger.py`
- `tests/services/test_llm_gateway.py`
- `tests/db/test_llm_usage_ledger_*.py`
- (No extraction / cards / digest / observations / memory_events / reflection_runs services on `main` either — those are Phase 6+ scope per the synthesis-first ratification in §2.)

**Critical production blocker carried over from Phase 4 (issue #164):**
1. Live message persistence does NOT create v1 `MessageVersion` → `chat_messages.current_version_id IS NULL` → `/recall` search join drops every new live message.
2. Imported messages do NOT set `current_version_id` → Phase 2 imported chat history invisible to `/recall` (invariant #8 violation).
3. Imported `MessageVersion` rows omit `normalized_text` → empty `tsvector` → unsearchable even after fix #2.

Without Wave 0 hotfix landed, `/recall` returns abstention for every production query and Phase 5 LLM synthesis would be wired to an empty evidence stream. Wave 0 is therefore a hard prerequisite for Wave 1 implementation — **NOT a parallel track**.

---

## §1. Non-negotiable invariants (verbatim from `HANDOFF.md` §1)

1. Existing gatekeeper must not break.
2. **No LLM calls outside `llm_gateway`.**
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten content.
4. Citations point to `message_version_id` or approved card sources.
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.
10. Public wiki remains disabled until review / source trace / governance are proven.

**Phase 5 amplifies invariant #2 and invariant #3.** Every gateway call MUST:
- Refuse to dispatch if any cited source has `memory_policy IN ('offrecord','forgotten')` OR `is_redacted=TRUE` (defense-in-depth — `search.py` already filters at SQL level, but the gateway re-validates).
- Write a `llm_usage_ledger` row regardless of outcome (success, error, abstain, cache hit, cost-refusal).
- Never log raw prompt / response content above DEBUG; production runs at INFO, so prompt and response only appear in DB in HASHED form.

---

## §2. Phase 5 spec — synthesis-first slice (ratified)

`HANDOFF.md` defines Phase 5 broadly as "LLM gateway + extraction (events / observations / candidates)". `PHASE5_PLAN_DRAFT.md §2` and the Phase chain assignment from the Orchestrator A prompt resolve the ambiguity:

- **Phase 5 (this plan) — synthesis-first.** Ships: `llm_gateway`, `llm_usage_ledger`, governance source-filter, budget guard, cache layer, `/recall` LLM-synthesized answer extension, ledger-backed audit. Closes the "every LLM call logged in ledger; no forbidden source sent to LLM" exit gate.
- **Phase 6 — knowledge cards** (next phase in chain). Ships extraction-as-needed for cards.
- **Phase 7 — daily summaries.** Reuses `llm_gateway` Phase 5 surface.
- **Phase 8 — reflection / observations / memory_events / memory_candidates / reflection_runs tables.** Reuses `llm_gateway`.

**Rationale.** The original HANDOFF wording packs four heavy responsibilities into Phase 5. Splitting synthesis-first lets Phase 5 ship a small auditable surface (gateway + ledger + cache + `/recall` upgrade) and lets each downstream phase add its own extraction tables on top of an already-stable gateway. Recipe stays additive; no cascading rework.

**This plan is the ratification.** If a future revision wants extraction tables in Phase 5, it must update `AUTHORIZED_SCOPE.md` and re-open this plan.

---

## §3. Phase 6 boundary (out of scope for this plan)

Phase 5 does NOT ship and MUST NOT alter:
- `knowledge_cards`, `card_sources`, `card_revisions` tables / repos / handlers.
- Extraction pipelines, `extraction_runs`, `memory_candidates`, `memory_events`, `observations` tables.
- Daily / weekly digests.
- Wiki, graph projection, public surfaces.
- Cross-chat answer surfaces.
- Vector / semantic search (Phase 4 FTS is the only retrieval path Phase 5 consumes).

Phase 5 MAY introduce extension hooks (e.g., a generic provider abstraction inside `llm_gateway`) that Phase 6 will consume — no new tables, no new public APIs beyond what `synthesize_answer` requires.

---

## §4. Architecture overview

```
              ┌─────────────────────────────────────────────────────────┐
/recall …  ─▶ │ bot/handlers/qa.py:_recall                              │
              │   ↓ run_qa(session, query, chat_id, …) → bundle         │
              │   ↓ if bundle.is_empty: write QaTrace(abstained=True)   │
              │   ↓ if memory.qa.llm_synthesis.enabled OFF:             │
              │       → render Phase 4 evidence list (unchanged)        │
              │   ↓ else:                                               │
              │       → synthesize_answer(bundle, query, model_cfg) ──┐ │
              │       → AnswerWithCitations | abstention             │ │
              │       → render synthesized answer + citations        │ │
              │       → write QaTrace(answer_text=…, llm_call_id=…) ─┤ │
              │                                                       │ │
              └───────────────────────────────────────────────────────┼─┘
                                                                      ▼
              ┌──────────────────────────────────────────────────────────┐
              │ bot/services/llm_gateway.py                              │
              │   • single entry: synthesize_answer(bundle, query, cfg)  │
              │   • PRE-call source-filter (defense-in-depth)            │
              │   • PRE-call budget guard (daily / monthly USD ceiling)  │
              │   • cache lookup (cite-stable input hash)                │
              │     → cache hit: write ledger(cache_hit=True), return    │
              │   • provider dispatch (anthropic | openai)               │
              │   • write llm_usage_ledger row (success | error | cost)  │
              │   • on cited message_version_id ∈ forget_events:         │
              │       invalidate cache row + abstain                     │
              └──────────────────────────────────────────────────────────┘
                                  │              │
                                  ▼              ▼
              ┌────────────────────┐    ┌──────────────────────────────┐
              │ Anthropic provider │    │ llm_usage_ledger (alembic 024)│
              │ (default haiku)    │    │  prompt_hash, response_hash,  │
              └────────────────────┘    │  tokens, cost_usd, cache_hit │
              ┌────────────────────┐    └──────────────────────────────┘
              │ OpenAI provider    │
              │ (fallback, opt-in) │
              └────────────────────┘
```

All flag-OFF paths produce **byte-for-byte identical** Phase 4 behavior. Flag is `memory.qa.llm_synthesis.enabled` (default OFF).

---

## §5. Component design

### §5.A — `bot/services/llm_gateway.py`

**Public API:**

```python
@dataclass(frozen=True)
class AnswerWithCitations:
    answer_text: str               # synthesized natural-language answer
    citation_ids: tuple[int, ...]  # subset of bundle.evidence_ids
    cost_usd: Decimal
    cache_hit: bool
    llm_call_id: int               # FK into llm_usage_ledger.id

@dataclass(frozen=True)
class Abstention:
    reason: Literal["empty_bundle", "all_filtered", "budget_exceeded",
                    "provider_error", "forget_invalidated"]
    cost_usd: Decimal              # zero except for refused-after-call
    llm_call_id: int               # ledger row written for every outcome

SynthesisResult = AnswerWithCitations | Abstention

async def synthesize_answer(
    session: AsyncSession,
    *,
    bundle: EvidenceBundle,
    query: str,
    config: LLMGatewayConfig,
    qa_trace_id: int | None = None,
) -> SynthesisResult: ...
```

**Pre-call invariants enforced by gateway (in this order):**

1. **Empty bundle short-circuit** — `len(bundle.evidence_ids) == 0` → write ledger row with `error='empty_bundle'`, return `Abstention(reason='empty_bundle', cost_usd=Decimal(0), llm_call_id=...)`. NO provider call.
2. **Source filter (defense-in-depth)** — re-validate every cited row: `chat_messages.memory_policy NOT IN ('offrecord','forgotten') AND chat_messages.is_redacted=FALSE`. If any cited source fails the filter → `reason='all_filtered'` (or per-source filter; if 0 surviving citations, abstain). Defends against race between Phase 4 search-time filter and gateway-time call.
3. **Cache lookup** — input hash = `sha256(query_normalized || sorted(citation_ids) || model_id || prompt_template_version)`. Hit → write ledger row with `cache_hit=True`, return cached `AnswerWithCitations` with new `llm_call_id`. NO provider call. NO cost.
4. **Forget-invalidation gate** — for every `citation_id IN bundle.evidence_ids`, JOIN against `forget_events` on `tombstone_key = 'message:{chat_id}:{message_id}'`. If any tombstone exists → invalidate cache + return `Abstention(reason='forget_invalidated')`. Belt-and-braces against forget_cascade race.
5. **Budget guard** — `LedgerRepo.daily_cost_usd(today) < config.daily_ceiling_usd` AND monthly equivalent. Failure → `Abstention(reason='budget_exceeded')`. NO provider call. Ledger row written with `error='budget_exceeded'`.
6. **Provider dispatch** — `config.provider` selects implementation; default `anthropic` with `claude-haiku-4-5-20251001`. Provider errors → caught, ledger row written with `error=str(e)`, `Abstention(reason='provider_error')`. NEVER raise.
7. **Citation enforcement** — provider must return `(answer_text, citation_ids)` where `citation_ids ⊆ bundle.evidence_ids`. If provider hallucinates a citation outside the bundle → reject, treat as `provider_error`. Eval harness in T5-05 verifies this property.

**Provider abstraction:**

```python
class LLMProvider(Protocol):
    async def call(self, *, prompt: str, model: str) -> ProviderResult: ...

class ProviderResult(NamedTuple):
    answer_text: str
    citation_ids: tuple[int, ...]
    tokens_in: int
    tokens_out: int
    request_id: str
    raw_latency_ms: int
```

Anthropic implementation default; OpenAI implementation behind `SYNTHESIS_PROVIDER=openai` env / config. **No stub provider in production code** — tests use `pytest`-injected fakes.

**Cache backing storage** — DB-backed table `llm_synthesis_cache` (alembic 024 migration ships it alongside `llm_usage_ledger`). Schema: `id PK, input_hash CHAR(64) UNIQUE, answer_text TEXT, citation_ids JSONB, model VARCHAR, created_at TIMESTAMPTZ, last_hit_at TIMESTAMPTZ, hit_count INT DEFAULT 1`. Multi-instance correct, GDPR-cascadable, simple. **Decision ratified by this plan** — supersedes DRAFT §5.A "minimal internal cache table OR deterministic in-memory test double". In-memory variants stay test-only.

**Cost ceiling defaults:**
- `LLM_DAILY_USD_CEILING` env / `feature_flags.config_json["daily_usd_ceiling"]`. Default **5.00 USD/day**.
- `LLM_MONTHLY_USD_CEILING` default **50.00 USD/month**.

**Decision ratified by this plan.** Override in deploy-specific env.

### §5.B — `alembic/versions/024_add_llm_usage_ledger_and_cache.py`

Single migration creating BOTH `llm_usage_ledger` and `llm_synthesis_cache`:

```sql
-- llm_usage_ledger
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
    request_id VARCHAR(128) NULL,          -- provider request id when available
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    error VARCHAR(255) NULL,               -- 'empty_bundle' | 'all_filtered' | 'budget_exceeded' | 'provider_error' | 'forget_invalidated' | NULL on success
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_usage_ledger_qa_trace_id ON llm_usage_ledger(qa_trace_id);
CREATE INDEX ix_llm_usage_ledger_model_created_at ON llm_usage_ledger(model, created_at);
CREATE INDEX ix_llm_usage_ledger_created_at ON llm_usage_ledger(created_at);

-- llm_synthesis_cache
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

Forward-only `down_revision = "022_add_qa_traces"` after Wave 0 lands 023. Migration 024 builds on 023 (which exists as backfill, no schema). `down()` drops both tables.

### §5.C — `bot/db/repos/llm_usage_ledger.py` and `bot/db/repos/llm_synthesis_cache.py`

```python
class LedgerRepo:
    @staticmethod
    async def record(session, *, qa_trace_id, provider, model,
                     prompt_hash, response_hash, tokens_in, tokens_out,
                     cost_usd, latency_ms, request_id, cache_hit, error
                     ) -> LlmUsageLedger: ...   # flushes; caller commits

    @staticmethod
    async def daily_cost_usd(session, *, day: date) -> Decimal: ...

    @staticmethod
    async def monthly_cost_usd(session, *, year: int, month: int) -> Decimal: ...

class SynthesisCacheRepo:
    @staticmethod
    async def get_or_none(session, *, input_hash: str
                          ) -> LlmSynthesisCache | None: ...

    @staticmethod
    async def store(session, *, input_hash, answer_text, citation_ids, model
                    ) -> LlmSynthesisCache: ...

    @staticmethod
    async def bump_hit(session, *, cache_id: int) -> None: ...

    @staticmethod
    async def invalidate_by_citation(session, *, message_version_id: int
                                     ) -> int: ...
```

Both repos `flush`-only, never `commit` — orchestration tx owned by handler.

### §5.D — `alembic/versions/025_extend_qa_traces_for_llm.py` and ORM update

```sql
ALTER TABLE qa_traces
  ADD COLUMN llm_call_id BIGINT NULL REFERENCES llm_usage_ledger(id) ON DELETE SET NULL,
  ADD COLUMN llm_response_summary TEXT NULL,
  ADD COLUMN llm_response_redacted BOOLEAN NULL DEFAULT FALSE,
  ADD COLUMN cost_usd NUMERIC(10, 6) NULL;

CREATE INDEX ix_qa_traces_llm_call_id ON qa_traces(llm_call_id);
```

`down()` drops the columns / index. Forward-only.

`bot/db/models.py::QaTrace` extended with the four new columns. `bot/db/repos/qa_trace.py` extended with optional kwargs (`llm_call_id`, `llm_response_summary`, `llm_response_redacted`, `cost_usd`) on `create`. Callers without flag preserve previous shape.

### §5.E — `bot/handlers/qa.py` extension + `bot/services/forget_cascade.py` cascade layers

**Handler extension:**
- New flag check: `memory.qa.llm_synthesis.enabled` (per-chat scope, default FALSE).
- If flag OFF → render Phase 4 evidence list verbatim (current behavior).
- If flag ON AND `bundle.is_non_empty` → call `synthesize_answer(...)` → render `AnswerWithCitations` (new template) or fall back to Phase 4 list on `Abstention`.
- `_write_trace` extended with `llm_call_id`, `llm_response_summary`, `llm_response_redacted`, `cost_usd` parameters.

**Cascade layer additions (in `bot/services/forget_cascade.py`):**
- New layer `_cascade_qa_traces_llm` — for `target_type='message'`: NULL `llm_response_summary` on rows where `llm_call_id IN (SELECT id FROM llm_usage_ledger WHERE qa_trace_id IN (target_qa_traces))`. For `target_type='user'`: NULL `query_text` (already covered by Wave 0) AND `llm_response_summary` for that user's traces.
- New layer `_cascade_llm_usage_ledger` — for `target_type='user'`: NULL `prompt_hash` and `response_hash` (the only PII surface; ledger keeps cost/token rows for audit).
- Both layers append to `CASCADE_LAYER_ORDER` after `_cascade_qa_traces` (which Wave 0 introduces).

---

## §6. Wave / stream allocation

| Wave | Stream(s) | Tickets | Parallel within wave? | Blocks Wave+1? | Worktrees |
|------|-----------|---------|------------------------|----------------|-----------|
| **0 (prereq hotfix)** | Single | T5-W0-01 | n/a | YES — must merge before Wave 1 | `.worktrees/p5-w0-hotfix-164` |
| **1 (gateway core)** | A (gateway), B (schema) | T5-01, T5-02 | YES (mock LedgerRepo for A while B builds) | YES — Wave 2 needs ledger | `.worktrees/p5-w1-gateway`, `.worktrees/p5-w1-schema` |
| **2 (repo + /recall)** | C (repo), D (handler) | T5-03, T5-04 | NO — D needs C | YES — Wave 3 needs handler | `.worktrees/p5-w2-repo-handler` (single bundled PR per DRAFT §6) |
| **3 (evals + FHR)** | E | T5-05 | n/a | n/a (final) | `.worktrees/p5-w3-evals` |

---

## §7. Tickets

### Wave 0 — production-blocker hotfix

| ID | Title | Source spec | Migration | LOC est | Dep | Acceptance |
|----|-------|-------------|-----------|---------|-----|------------|
| **T5-W0-01** | Phase 4 hotfix #164 — live v1 + import current_version_id + normalized_text + qa_traces cascade + router order | `docs/memory-system/prompts/PHASE5_WAVE0_HOTFIX164_DESIGN.md` (1044 lines, Critic v2 + Risk v2 closed) | **023** (backfill v1 cohort `WHERE current_version_id IS NULL`) | ~1000 | none | (1) New live message → `chat_messages` + v1 `message_versions` + `current_version_id` set, all in one tx. Tests in `tests/services/test_message_persistence.py` (+12). (2) Import path → same observable state. Tests in `tests/services/test_import_apply.py` (+8). (3) Imported v1 has `normalized_text` populated → FTS hit. (4) Migration 023 backfills legacy v1 cohort idempotently. (5) `qa_traces` cascade layer added to `forget_cascade.CASCADE_LAYER_ORDER`. (6) E2E test `tests/integration/test_phase4_hotfix_e2e.py` (7 scenarios) green. (7) Eval suite `tests/eval/test_qa_eval_cases.py` (12 + 2 import = 14) green via real `persist_message_with_policy` path. (8) `git diff main..feat/p5-w0-hotfix-164 -- alembic/versions/` exposes ONLY `023_*.py`. (9) Single PR, 15 commits per design §6. (10) PAR review: Claude product + Codex technical APPROVE. |

**Wave 0 source-of-truth doc:** `docs/memory-system/prompts/PHASE5_WAVE0_HOTFIX164_DESIGN.md`. Implementation prompt for the executor MUST quote that doc verbatim.

### Wave 1 — LLM gateway core (parallel)

| ID | Stream | Title | Files (new) | Migration | Dep | Parallel? | Acceptance summary |
|----|--------|-------|-------------|-----------|-----|-----------|--------------------|
| **T5-01** | A | `llm_gateway.py` core — `synthesize_answer`, provider abstraction (Anthropic+OpenAI), pre-call invariants, cache lookup | `bot/services/llm_gateway.py`, `bot/services/llm_providers/__init__.py`, `bot/services/llm_providers/anthropic.py`, `bot/services/llm_providers/openai.py`, `tests/services/test_llm_gateway.py`, `tests/services/test_llm_providers.py` | none (consumes 024) | T5-W0-01 merged + T5-02 schema (mock LedgerRepo until T5-03) | YES | All §5.A pre-call invariants enforced; provider errors abstain (no raise); citations subset of `bundle.evidence_ids`; cache hit writes ledger w/ `cache_hit=True`; cache invalidation when cited `message_version_id ∈ forget_events`; no LLM imports outside this file. ≥30 tests covering empty bundle, all-filtered, budget-exceeded, provider-error, forget-invalidated, cache-hit, citation-hallucination-rejection, source-filter race. |
| **T5-02** | B | `llm_usage_ledger` + `llm_synthesis_cache` schema migration | `alembic/versions/024_add_llm_usage_ledger_and_cache.py`, `bot/db/models.py` (new `LlmUsageLedger`, `LlmSynthesisCache` ORM), `tests/db/test_llm_usage_ledger_schema.py`, `tests/db/test_llm_synthesis_cache_schema.py` | **024** | T5-W0-01 merged | YES | Tables + indexes per §5.B SQL. ORM models registered. `alembic upgrade head` + `alembic downgrade -1` clean on test DB. ≥6 schema-shape tests. |

### Wave 2 — repo + /recall integration (sequential)

| ID | Title | Files | Migration | Dep | Acceptance summary |
|----|-------|-------|-----------|-----|--------------------|
| **T5-03** | `LedgerRepo` + `SynthesisCacheRepo` async repositories | `bot/db/repos/llm_usage_ledger.py`, `bot/db/repos/llm_synthesis_cache.py`, `tests/db/test_llm_usage_ledger_repo.py`, `tests/db/test_llm_synthesis_cache_repo.py` | none | T5-02 | All methods per §5.C. `record(...)` flushes without commit. `daily_cost_usd` / `monthly_cost_usd` UTC bounded; zero rows = `Decimal("0")`. `invalidate_by_citation` returns `int` count of invalidated rows. ≥15 tests including rollback safety. |
| **T5-04** | `/recall` LLM synthesis integration + `qa_traces` LLM extension + cascade layers | `bot/handlers/qa.py`, `bot/services/qa.py`, `alembic/versions/025_extend_qa_traces_for_llm.py`, `bot/db/models.py` (QaTrace extension), `bot/db/repos/qa_trace.py`, `bot/services/forget_cascade.py` (new cascade layers per §5.E), `tests/handlers/test_qa_llm_synthesis.py`, `tests/services/test_forget_cascade.py` (+layers) | **025** | T5-01, T5-02, T5-03 | Flag `memory.qa.llm_synthesis.enabled` default OFF preserves Phase 4 output **byte-for-byte**. ON + non-empty bundle → calls `synthesize_answer`; rendered with template `qa_synthesized.html`. Abstention falls back to Phase 4 list. Gateway error → log + Phase 4 fallback. New `qa_traces` columns populated. Cascade layers green for `target_type IN ('message','user')`. ≥20 tests. |

### Wave 3 — evals + FHR

| ID | Title | Files | Dep | Acceptance |
|----|-------|-------|-----|-----------|
| **T5-05** | Eval harness extension + integration fixtures | `tests/eval/test_qa_eval_cases.py` (extend), `tests/eval/test_qa_llm_eval_cases.py` (new), `tests/fixtures/qa_llm_eval_cases.json`, optional `tests/fixtures/qa_llm_gateway_fixture.json` | T5-04 | Mocked unit evals: answer citations subset of expected; empty bundle → abstention; cost-refusal abstention; provider-error abstention; cache-hit reproduces answer; citation-hallucination rejected. Real-gateway integration test opt-in via `RUN_LLM_INTEGRATION=1` env, skipped in CI by default. **Coordination point with Orchestrator C (Phase 11):** Phase 11 evals harness consumes the same fixtures as the regression baseline. |

---

## §8. Stop signals (binding)

Pause work, comment on tracking issue, escalate if any:

1. **Wave 0 hotfix #164 not on main** when starting any Wave 1 PR.
2. Sister Orchestrator B or C opens a PR touching any of: `bot/db/models.py` `QaTrace`, `bot/services/forget_cascade.py`, `bot/services/evidence.py`, `bot/services/search.py`, `bot/handlers/qa.py`, `docs/memory-system/AUTHORIZED_SCOPE.md`, `docs/memory-system/IMPLEMENTATION_STATUS.md`. Coordinate via `ORCHESTRATOR_REGISTRY.md` §3.3.
3. Subagent reports green tests but orchestrator cannot independently re-run them in the worktree.
4. Codex / Claude reviewer cites a file:line that does not exist when grepped — downgrade to "needs investigation", re-prompt.
5. CI on `main` red for an unrelated reason — block all pushes until resolved.
6. Provider call returns content not in `bundle.evidence_ids` — citation hallucination — gateway must reject AND eval suite must capture this regression.
7. Budget guard accidentally bypassed — every gateway call MUST write ledger row, including refusals.
8. Cascade layer for `qa_traces.llm_response_summary` or `llm_usage_ledger` missing when shipping T5-04 → invariant #9 violation (tombstones durable).
9. Phase 6/7/8 work attempts to start before Phase 5 closes (out of scope per §3 + AUTHORIZED_SCOPE.md).

---

## §9. PR workflow & merge order

Per `superflow-enforcement.md` Rule 8 + REGISTRY §3.5 (`git_workflow_mode = parallel_wave_prs`):

1. **Sprint 0 (this branch `plan/p5-ratify`)** — single PR ratifying this plan + REGISTRY §4 update + IMPLEMENTATION_STATUS.md Phase 5 section header. PAR review: 1 product (Claude) + 1 technical (Codex `gpt-5.5` `-c model_reasoning_effort=high`). Auto-merge on CI green.
2. **Wave 0** — single PR `feat/p5-w0-hotfix-164` (carries 15 commits per design §6). PAR review obligatory before merge.
3. **Wave 1** — two PRs in parallel: `feat/p5-w1-gateway` (T5-01) and `feat/p5-w1-schema` (T5-02). Each PAR-reviewed. Merge order does not matter; T5-01 mocks LedgerRepo until T5-03 lands.
4. **Wave 2** — one bundled PR `feat/p5-w2-repo-handler` (T5-03 + T5-04) per DRAFT §6 / Rule 11 single-call-site discipline.
5. **Wave 3** — single PR `feat/p5-w3-evals` (T5-05).
6. **Final Holistic Review (FHR)** — required per Rule 9 (Phase 5 introduces provider calls + cost-bearing behavior + 3 alembic migrations across 4 PRs ≥ 4 sprints AND `parallel_wave_prs` mode). 2 reviewers (Claude `deep-product-reviewer` + Codex `model_reasoning_effort=high`) review all Phase 5 PRs as unified system. CRITICAL/HIGH must close before declaring Phase 5 closed in `IMPLEMENTATION_STATUS.md` and `CLAUDE.md`.
7. **Phase 5 closure update** (final PR `docs(p5): Phase 5 closed`):
   - `IMPLEMENTATION_STATUS.md` — every T5-* ticket marked done.
   - `ROADMAP.md` — Phase 5 = DONE.
   - `CLAUDE.md` — "Phase 5 CLOSED YYYY-MM-DD; next active phase = Phase 6".
   - `AUTHORIZED_SCOPE.md` — authorize Phase 6 (cards) + unblock Orchestrator B Phase 9/10 dependency on cards.
   - Phase 11 evals regression notification to Orchestrator C.
   - Cleanup `.worktrees/p5-*` + delete merged feat branches on origin.

---

## §10. Glossary (cross-orchestrator)

- **Wave 0** — production-blocker prerequisite hotfix sprint that Phase 5 absorbs because no other orchestrator owns it.
- **`AnswerWithCitations`** — frozen dataclass returned on successful synthesis. `citation_ids ⊆ bundle.evidence_ids`.
- **`Abstention`** — frozen dataclass returned on any non-success path; carries `llm_call_id` + ledger row written.
- **`SynthesisResult`** — `AnswerWithCitations | Abstention`.
- **Cite-stable input hash** — `sha256(query_normalized || sorted(citation_ids) || model_id || prompt_template_version)` — keys cache and ledger correlations.
- **Source filter (defense-in-depth)** — second governance check inside the gateway, after `search.py` SQL-level filter, before provider dispatch.
- **Forget-invalidation gate** — `forget_events`-based pre-call check that aborts even if cache or `search.py` raced ahead of cascade.
- **Budget guard** — daily / monthly USD ceiling enforced via `LedgerRepo.daily_cost_usd` / `monthly_cost_usd`.
- **Eval seam** — fixture-driven unit eval path; opt-in real-gateway integration via `RUN_LLM_INTEGRATION=1`.
- **Phase 11 baseline coordination** — Orchestrator C runs Phase 11 evals against current Phase 4 `/recall` (baseline) and again after Phase 5 closes (regression for hallucination / leakage / citation drift).

---

## §11. Open ratification asks (resolved by this plan unless flagged)

| # | Question | Resolution in this plan | Notes |
|---|----------|-------------------------|-------|
| 1 | Provider default | Anthropic `claude-haiku-4-5-20251001` (default) + OpenAI fallback (opt-in via env) | OK. |
| 2 | Cost ceiling default | 5 USD/day + 50 USD/month | OK; override per env. |
| 3 | Cache backing storage | DB table `llm_synthesis_cache` (alembic 024) | OK; supersedes DRAFT §5.A unresolved option. |
| 4 | Source filter location | Gateway re-validates (defense-in-depth) on top of `search.py` SQL filter | OK. |
| 5 | Phase 11 coordination protocol | Phase 5 closure PR notifies Orchestrator C; Phase 11 evals consume same fixtures as Wave 3 | Coordination point — comment on Phase 11 epic issue when Phase 5 closes. |
| 6 | Migration numbering | Wave 0 = 023, Wave 1 ledger+cache = 024, Wave 2 qa_traces ext = 025 | OK; corrected from DRAFT (which referenced stale 022/023). |
| 7 | Synthesis-first vs full Phase 5 | This plan ratifies synthesis-first; extraction tables (`memory_events`, `observations`, `memory_candidates`, `reflection_runs`) deferred to Phase 8 | OK; matches Orchestrator A prompt phase chain "Phase 5 (LLM gateway) → 6 (cards) → 7 (digests) → 8 (reflection)". |

If any of #1–#7 must change, update `AUTHORIZED_SCOPE.md` first, re-open this plan via fresh `plan/p5-revise` PR.

---

## §12. Sprint 0 deliverables (this PR)

This PR (`plan/p5-ratify` → `main`) ships:

1. `docs/memory-system/PHASE5_PLAN.md` — this file (ratified, no `_DRAFT` suffix).
2. `docs/memory-system/prompts/PHASE5_WAVE0_HOTFIX164_DESIGN.md` — salvaged from stale `.worktrees/p4-hotfix-164/.codex-design.md` so the design survives worktree cleanup.
3. `docs/memory-system/ORCHESTRATOR_REGISTRY.md` — append §4 row claiming Sprint 0 (Orchestrator A active).
4. `docs/memory-system/IMPLEMENTATION_STATUS.md` — add Phase 5 section header with Wave 0 + T5-01..T5-05 stub rows in `not started` state.

Deliberately NOT shipped in this PR:
- Any code changes (Wave 0 implementation lands in its own PR).
- `AUTHORIZED_SCOPE.md` updates (Phase 5 already authorized 2026-04-30; no delta needed).
- `CLAUDE.md` Phase 5 status update (deferred to Wave 0 merge — Phase 5 Wave 0 starts then).

---

**Ratified by:** Orchestrator A.
**Date:** 2026-05-02.
**Predecessor:** `prompts/PHASE5_PLAN_DRAFT.md` (kept in repo as historical record).
