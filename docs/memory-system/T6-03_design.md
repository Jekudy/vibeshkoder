# T6-03 Design — Gateway `extract_candidates`

**Status:** Pre-flight design (draft, not yet implemented)
**Author:** Pre-flight planning agent
**Date:** 2026-05-12
**Predecessor:** T6-02 (extractor service + admin handler, PR in PAR round 2 on `feat/p6-t6-02-extractor` HEAD `182095f`)
**Target sprint:** Phase 6 Wave 1 Stream B — Sprint 2
**Purpose:** Lay out implementation steps so the future T6-03 implementer can follow without re-reading the full plan + Phase 5 gateway source.

This document is **descriptive of the future implementation, not the current code**. As of `chore/t6-03-design`, `bot/services/llm_gateway.py` has only `synthesize_answer` (912 lines, Phase 5); no `extract_candidates` method exists yet. The router `bot.handlers.admin_extract.router` and the scheduler `extraction_scheduler_tick` exist on the T6-02 worktree branch but are **not yet wired** into `bot/__main__.py`.

---

## Acceptance criteria (verbatim from PHASE6_PLAN.md §7 T6-03 + T6-02 spec addendum)

From `PHASE6_PLAN.md` §7 T6-03 (lines 534-545) + the T6-02 addendum at `+547,+548` of the T6-02 worktree diff against main:

- **Scope:** Phase 5 `llm_gateway`; add `extract_candidates()` contract.
- **Acceptance criteria:**
  - No provider SDK call exists outside `llm_gateway`.
  - Every call is associated with the Phase 5 LLM usage ledger.
  - Output schema includes candidate payload and source `message_version_id`s.
  - Forbidden source content cannot be passed to the gateway.
  - **Router registration**: register `bot.handlers.admin_extract.router` in `bot/__main__.py` `dp.include_routers(...)` adjacent to `admin.router` (deferred from T6-02 alongside the concrete gateway DI).
  - **Gateway DI wiring**: wire the concrete `ExtractCandidatesGateway` instance into BOTH the `admin_extract` handler call site AND the `extraction_scheduler_tick` call site. Use the existing aiogram DI middleware pattern (same as the Phase 5 LLM gateway wiring) so per-request session + gateway both reach handlers as kwargs. The Protocol decorator `@runtime_checkable` (added in T6-02) enables a defensive `isinstance(gw, ExtractCandidatesGateway)` validation at wire time if desired.
  - **Phase 11 leakage binding test green on the T6-03 PR head before merge** — critical sub-gate per §6. The sub-gate requires ALL cases L1, L2, L3a, L3b, L3c, L4, L5 in `tests/evals/test_leakage.py::test_leakage_invariants` green, plus R1, R2, R3, R4 refusal cases (`tests/evals/test_refusal.py`) green. The CI nightly `evals.yml` workflow result alone is NOT sufficient — a re-run must be triggered on the T6-03 PR head specifically.
- **Dependencies:** Phase 5 gateway/ledger, T6-00, T6-02.
- **Stream:** Wave 1 / Stream B.

Additional invariants inherited from `PHASE6_PLAN.md`:

- **§1 invariant #2:** No LLM calls outside `llm_gateway`.
- **§1 invariant #3:** No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
- **§4 architecture diagram:** `llm_gateway.extract_candidates()` is the only LLM path; "single audited LLM path / no forbidden source content."
- **§5.B "Stop conditions":** Empty bundle short-circuit; non-normal / redacted / forgotten source ⇒ refuse the LLM call AND record `extraction_runs.run_status='failed'`.
- **§8 stop signal:** "Extraction run without LLM usage ledger entry → STOP." (Already enforced in T6-02 via `llm_usage_ledger_id is None` guard at `bot/services/extractor.py:636-654` in the T6-02 worktree.)

---

## 1. Gateway concrete implementation

### File and location

**File:** `bot/services/llm_gateway.py` (extend; do NOT create a new file)
**Module-level surface:** add `extract_candidates` to `__all__`; keep the Phase 5 `synthesize_answer` exports intact.

### Method signature

```python
async def extract_candidates(
    session: AsyncSession,
    *,
    source_versions: list[dict[str, Any]],
    prompt_template_version: str = "v0.1.0",
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    config: LLMGatewayConfig,
) -> dict[str, Any]:
    """Single Phase 6 LLM extraction entry point.

    Returns ``{"candidates": [...], "llm_usage_ledger_id": int | None}`` —
    must MATCH the T6-02 ``ExtractCandidatesGateway`` Protocol surface (see
    bot/services/extractor.py:113-148 on the T6-02 worktree branch).
    """
```

### Pattern (mirror existing `synthesize_answer`)

`synthesize_answer` (`bot/services/llm_gateway.py:326-791`) provides the load-bearing reference. Reuse:

1. **DI of `LedgerRepoProtocol` + `LLMProvider`.** Same Protocol surface (`bot/services/llm_gateway.py:118-168`). The `synthesize_answer` Protocol already accepts `qa_trace_id: int | None` (line 123), so the **extraction path passes `qa_trace_id=None`** when calling `ledger_repo.record` — this is the same shape T5-05 abstention-path fixtures use (see PHASE6_PLAN.md §5.F M-1).
2. **No cache.** Extraction differs from Q&A: no per-call cache (cards become the cached output after `/approve`). Drop `cache_repo` entirely from `extract_candidates`.
3. **Budget guard reuse.** Reuse `_budget_check` (lines 835-860) to honor daily/monthly ceilings configured in `LLMGatewayConfig`. Acquire `pg_advisory_lock(LLM_BUDGET_LOCK_ID)` (`bot/services/llm_gateway.py:199-201,313-321`) for budget read; release BEFORE provider dispatch (Phase 5 placeholder pattern, lines 478-567).
4. **Placeholder ledger row pattern.** Insert `llm_usage_ledger` row with `cost_usd=0`, `error=None`, `response_hash=None` BEFORE provider call; UPDATE post-dispatch with real cost via `ledger_repo.update_placeholder` (lines 121-167 in `bot/db/repos/llm_usage_ledger.py`). On any failure, UPDATE the placeholder with `error=...`.
5. **Cost lookup.** Reuse `bot/services/llm_pricing.py::estimate_cost` (lines 44-75). `KeyError` ⇒ structural error path (emit stop signal `"llm_provider_structural"`).

### Privacy at the gateway boundary

The T6-02 extractor (`bot/services/extractor.py::run_extraction_pass`) is the privacy gatekeeper: it runs `_select_eligible_sources` + `_bundle_is_clean` (defense-in-depth: re-fetches `forget_events` after SELECT to close the SELECT→gateway race window, see `bot/services/extractor.py:371-448`). By the time `extract_candidates` receives `source_versions`, the rows are governance-validated.

**The gateway is NOT a re-filter point** — re-filtering at this layer would duplicate work and create drift risk. The gateway treats `source_versions` as authoritative trusted input AND:

- **MUST NOT log raw source text** (privacy invariant — no raw bodies in structured logs).
- **MUST NOT include `text` / `caption` / `normalized_text` in error messages** (would leak content into ledger or observability).
- **MUST set `prompt_hash`** from a SHA-256 over the canonical prompt envelope (mirrors `_prompt_hash` line 237-238).

### Output schema

`extract_candidates` returns:

```python
{
    "candidates": [
        {
            "candidate_json": dict,                  # JSONB-compatible
            "source_message_version_ids": [int, ...],
        },
        ...
    ],
    "llm_usage_ledger_id": int | None,
}
```

This matches the T6-02 Protocol exactly (`bot/services/extractor.py:133-148`).

**Failure semantics (alignment with T6-02 invariant #4):**
- Provider succeeded → return `{"candidates": [...], "llm_usage_ledger_id": <updated row id>}`.
- Provider transient/structural/unknown error → return `{"candidates": [], "llm_usage_ledger_id": <placeholder row id with error= field set>}`. The extractor's invariant #4 guard at `bot/services/extractor.py:636` will see non-None ledger id ⇒ run will be marked `completed` with `candidate_count=0`.
- Budget exceeded → return `{"candidates": [], "llm_usage_ledger_id": <budget_exceeded row id>}`. Same code path as provider error from extractor's POV (`run_status='completed', candidate_count=0`).
- Empty input (`source_versions == []`) → SHORT-CIRCUIT: return `{"candidates": [], "llm_usage_ledger_id": None}` WITHOUT calling the provider AND without writing a ledger row. T6-02 already short-circuits the gateway call on empty bundles (`bot/services/extractor.py:560-583`), but the gateway should defensively no-op too.

**Important:** the extractor's invariant #4 says "an extraction run that actually invoked the gateway MUST have a ledger row." Empty-input no-call ≠ "invoked the gateway", so `llm_usage_ledger_id=None` is correct on that path. This is exactly the asymmetry the extractor handles — see commit `50d6f7a` "fix(p6-t6-02): enforce ledger_id non-null invariant".

### Citation set semantics

Phase 5 has a citation enforcement invariant (`bot/services/llm_gateway.py:671-693`): provider `citation_ids` ⊆ `surviving_ids` (post-source-filter).

For extraction:
- The "citation set" is the input `source_message_version_ids` per candidate.
- Each candidate's `source_message_version_ids` MUST be ⊆ the union of input `source_versions[*]["message_version_id"]`. If the provider hallucinates a `source_message_version_id` not present in input ⇒ abort the candidate (drop it from output), update ledger placeholder with `error="citation_hallucination"`.
- Discard candidates with empty `source_message_version_ids` — invariant #4 of the plan says a card cannot become active without source.

### Pseudocode skeleton

```python
async def extract_candidates(
    session, *, source_versions, prompt_template_version, ledger_repo,
    provider, config,
):
    # Invariant 1: empty short-circuit (no provider call, no ledger row).
    if not source_versions:
        return {"candidates": [], "llm_usage_ledger_id": None}

    # Build deterministic prompt + envelope (no raw content in this hash).
    prompt = _build_extraction_prompt(source_versions, prompt_template_version)
    prompt_hash = _prompt_hash(prompt)

    valid_mvid_set: frozenset[int] = frozenset(
        int(sv["message_version_id"]) for sv in source_versions
    )

    # Budget guard + placeholder row (same as Phase 5 invariant 5).
    placeholder_row = None
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

    # Provider dispatch outside lock (same as Phase 5 invariant 6).
    started = time.monotonic()
    try:
        provider_result = await provider.call(prompt=prompt, model=config.model)
    except ProviderTransientError as exc:
        latency = int((time.monotonic() - started) * 1000)
        await ledger_repo.update_placeholder(
            session, llm_call_id=placeholder_row.id, cost_usd=Decimal("0"),
            response_hash=None, tokens_in=0, tokens_out=0,
            request_id=None, latency_ms=latency,
            error=f"provider_transient:{exc.subtype}",
        )
        return {"candidates": [], "llm_usage_ledger_id": placeholder_row.id}
    except ProviderStructuralError as exc:
        # ... emit stop signal + ledger update (mirror llm_gateway.py:593-619)
        return {"candidates": [], "llm_usage_ledger_id": placeholder_row.id}
    except Exception as exc:
        # ... unknown error (mirror llm_gateway.py:620-642)
        return {"candidates": [], "llm_usage_ledger_id": placeholder_row.id}

    # Parse provider response into candidate list. T6-03 v0.1.0 prompt-template
    # contract is the IMPLEMENTER's responsibility — keep parsing simple +
    # deterministic. v0.1.0 may emit a JSON array; later versions may parse
    # structured-output APIs.
    candidates_raw = _parse_extraction_response(provider_result.answer_text)

    # Filter for citation conformance + non-empty source set.
    valid_candidates = []
    for c in candidates_raw:
        source_ids = [int(x) for x in c.get("source_message_version_ids") or []]
        # Drop hallucinated IDs.
        source_ids = [sid for sid in source_ids if sid in valid_mvid_set]
        if not source_ids:
            # invariant: no card without source.
            continue
        valid_candidates.append({
            "candidate_json": dict(c.get("candidate_json") or {}),
            "source_message_version_ids": source_ids,
        })

    cost_usd = _estimate_cost(
        config=config,
        tokens_in=provider_result.tokens_in,
        tokens_out=provider_result.tokens_out,
    )
    latency = int((time.monotonic() - started) * 1000)
    await ledger_repo.update_placeholder(
        session, llm_call_id=placeholder_row.id, cost_usd=cost_usd,
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
```

### Open implementation points for T6-03 implementer

- **Prompt template v0.1.0 wire format.** The plan does not pin a specific format. Recommend a simple JSON envelope similar to Phase 5: a single user message with a system instruction asking the model to return `[{"candidate_json": {...}, "source_message_version_ids": [...]}, ...]` as JSON. Anthropic provider returns text only at present (`bot/services/llm_providers/anthropic.py:103-117`) — same constraint as Phase 5. Parsing JSON-text directly is acceptable; structured-output APIs are out of scope.
- **`prompt_hash` content.** Should hash the canonical prompt body (including the sorted `message_version_id` set, `prompt_template_version`, and `model`) — same shape as `_cache_input_hash` (line 220-234) but cache key is not used here.
- **Logging discipline.** No raw `text` / `caption` / `normalized_text` in `logger.warning` / `logger.error`. Use `prompt_hash` + `message_version_id` for traceability.

---

## 2. Router registration

### File and location

**File:** `bot/__main__.py` (modify; do NOT create a new file)
**Lines to touch:** `dp.include_routers(...)` block at lines 106-118.

### Step

Add `admin_extract.router` to the import line + `include_routers` list. Place adjacent to `admin.router` (line 110) per the T6-02 addendum:

```python
# imports — line 11-23
from bot.handlers import (
    admin,
    admin_extract,                  # NEW (T6-03)
    chat_events,
    ...
)

# include_routers — line 106-118
dp.include_routers(
    start.router,
    questionnaire.router,
    vouch.router,
    admin.router,
    admin_extract.router,           # NEW (T6-03)
    chat_events.router,
    edited_message.router,
    forget_me.router,
    forget_reply.router,
    qa.router,
    forward_lookup.router,
    chat_messages.router,
)
```

Order rationale: `admin_extract` is private-chat-only + admin-only with a Command filter, so it's collision-free with `chat_messages` catch-all. Placing it after `admin.router` follows the T6-02 addendum exactly ("adjacent to `admin.router`").

---

## 3. Gateway DI wiring

### The problem

`admin_extract.cmd_admin_extract` (T6-02 worktree, `bot/handlers/admin_extract.py:115-219`) takes `gateway: ExtractCandidatesGateway` as a handler kwarg. For aiogram to inject this kwarg automatically, the gateway needs to be made available in the dispatcher's workflow-data dict OR via a middleware that injects it into `data` per-update.

Phase 5 used a different approach: the QA handler instantiates `LedgerRepo()` + `SynthesisCacheRepo()` + provider locally inside the handler (`bot/handlers/qa.py:340-343`). This is fine because both repos are stateless static-method classes and the provider is cheap to construct (`bot/services/llm_providers/anthropic.py:35-37`).

### Decision: Local instantiation, NOT aiogram middleware

**Rationale:** The Phase 5 pattern is the established precedent. Introducing a new aiogram middleware for the gateway adds complexity (DI lifecycle, mock injection in tests) without buying anything that local instantiation lacks. The Protocol seam from T6-02 (`bot/services/extractor.py:113-148`) is sufficient for testability.

**Concrete impl (T6-03 implementer):**

1. **Build a `LiveExtractCandidatesGateway` adapter class** in `bot/services/llm_gateway.py` that wraps the new `extract_candidates` function:

   ```python
   class LiveExtractCandidatesGateway:
       """Concrete impl of T6-02 ``ExtractCandidatesGateway`` Protocol."""

       def __init__(self, *, ledger_repo, provider, config):
           self._ledger_repo = ledger_repo
           self._provider = provider
           self._config = config

       async def extract_candidates(
           self, session, *, source_versions, prompt_template_version="v0.1.0"
       ):
           return await extract_candidates(
               session,
               source_versions=source_versions,
               prompt_template_version=prompt_template_version,
               ledger_repo=self._ledger_repo,
               provider=self._provider,
               config=self._config,
           )
   ```

2. **In `bot/handlers/admin_extract.py`**, drop the `gateway` kwarg from the handler signature and instead build the live gateway locally:

   ```python
   # Replace lines 116-121 ("gateway: ExtractCandidatesGateway") and the
   # call at lines 198-204 with a locally-constructed gateway.
   from bot.services.llm_gateway import LiveExtractCandidatesGateway, _load_gateway_config_extract
   from bot.db.repos.llm_usage_ledger import LedgerRepo
   from bot.services.llm_providers.anthropic import AnthropicProvider

   @router.message(Command("admin_extract"), PrivateChatFilter())
   async def cmd_admin_extract(
       message: Message,
       command: CommandObject,
       session: AsyncSession,
   ) -> None:
       # ... admin check, window parse ...
       cfg = _load_gateway_config_extract()
       provider = AnthropicProvider() if cfg.provider == "anthropic" else OpenAIProvider()
       gateway = LiveExtractCandidatesGateway(
           ledger_repo=LedgerRepo(), provider=provider, config=cfg,
       )
       result = await run_extraction_pass(
           session,
           window_start=window_start,
           window_end=window_end,
           gateway=gateway,
           operator_user_id=message.from_user.id,
       )
       # ... summary reply ...
   ```

3. **In the scheduler** (`bot/services/scheduler.py`), add a new job that constructs the live gateway inside its tick function:

   ```python
   from bot.services.extractor import extraction_scheduler_tick
   from bot.services.llm_gateway import LiveExtractCandidatesGateway, _load_gateway_config_extract
   from bot.db.repos.llm_usage_ledger import LedgerRepo

   async def run_extraction_scheduler_tick() -> None:
       """T6-03 wiring of T6-02 scheduler tick into apscheduler."""
       async with async_session() as session:
           try:
               cfg = _load_gateway_config_extract()
               provider = (
                   AnthropicProvider() if cfg.provider == "anthropic"
                   else OpenAIProvider()
               )
               gateway = LiveExtractCandidatesGateway(
                   ledger_repo=LedgerRepo(), provider=provider, config=cfg,
               )
               await extraction_scheduler_tick(session, gateway=gateway)
               await session.commit()
           except Exception:
               logger.exception("extraction_scheduler_tick crashed")

   def start_scheduler(bot: Bot) -> None:
       # ... existing jobs ...
       scheduler.add_job(
           run_extraction_scheduler_tick,
           "interval",
           minutes=15,        # placeholder; tune by T6-03 implementer
           id="extraction_scheduler_tick",
           replace_existing=True,
           max_instances=1,
           coalesce=True,
           misfire_grace_time=60,
       )
   ```

### Why this avoids a new aiogram middleware

The T6-02 Protocol `@runtime_checkable` decorator (`bot/services/extractor.py:113`, commit `63d7385`) enables a defensive `isinstance(gw, ExtractCandidatesGateway)` check at wire time, but T6-03 does not need to add such a check — the `LiveExtractCandidatesGateway` class statically implements the Protocol.

Tests can continue to use fake gateways without any DI plumbing: `run_extraction_pass` already accepts `gateway: ExtractCandidatesGateway` as a kwarg. T6-02's test suite (verify via `tests/services/test_extractor*.py` if present, or new files at T6-03) injects fakes directly.

### Open implementation point

- `_load_gateway_config_extract()` — should this be a new helper or reuse `_load_gateway_config` from `bot/handlers/qa.py:103-133`? Likely reuse via factoring it out into `bot/services/llm_gateway.py` (or a new `bot/services/_llm_config.py`) and importing from both sites. Implementer's choice. The configuration ENV vars (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_DAILY_USD_CEILING`, `LLM_MONTHLY_USD_CEILING`) MUST be re-used unchanged per global rule "NEVER change names of existing variables that read values from env files."

---

## 4. Scheduler DI wiring

Already detailed in §3 above. The scheduler integration adds **one new apscheduler job** in `start_scheduler` (`bot/services/scheduler.py:229-282`) calling a thin wrapper `run_extraction_scheduler_tick` that:

1. Opens a fresh `async_session()`.
2. Constructs `LiveExtractCandidatesGateway` locally.
3. Calls `extraction_scheduler_tick(session, gateway=gateway)` from `bot/services/extractor.py:739-787` (T6-02 worktree).
4. Commits or logs+ignores on exception.

The T6-02 scheduler tick already does the right thing under the flag gate (`memory.extraction.scheduler.enabled` default OFF) and acquires `pg_try_advisory_xact_lock` for idempotency (`bot/services/extractor.py:766-771`). No additional locking concerns for T6-03.

**Interval choice:** Default flag is OFF, so any interval is fine. 15 min mirrors the gatekeeper jobs (`check_vouch_deadlines`, line 246). Operator-explicit `/admin_extract` calls remain the primary path.

---

## 5. Phase 11 binding sub-gate

This is the **critical merge gate** for T6-03 per PHASE6_PLAN.md §6.

### Required tests on PR head (NOT nightly evals.yml)

The plan explicitly says "the CI nightly `evals.yml` workflow result alone is NOT sufficient — re-run must be triggered on the T6-03 PR head."

Run on the T6-03 PR head:

```bash
EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x --timeout=60 \
  tests/evals/test_leakage.py \
  tests/evals/test_citations.py \
  tests/evals/test_refusal.py \
  tests/evals/test_no_llm_imports.py
```

### Required case coverage

- **`tests/evals/test_leakage.py::TestRecallGovernanceLeakage`** parametrized over `["L1", "L2", "L3a", "L3b", "L3c", "L4", "L5"]` (line 363). These cover offrecord, nomem, three forget-tombstone keys (message / message_hash / user), redaction, and cross-chat isolation. The harness invokes `bot.services.eval_runner::run_eval_recall` — which goes through `bot.services.qa::run_qa` — so the test surface today is the **/recall path**, not extraction. T6-03 should:
  1. Confirm these still pass byte-for-byte (no regression).
  2. **Add a new binding-style test** for extraction (see §5 below).
- **`tests/evals/test_refusal.py::TestRefusal`** R1-R4 (lines 209-364) — covers empty seed, only-redacted-and-offrecord, non-member, wrong-chat, empty-query. Same /recall surface as leakage. Confirm green.
- **`tests/evals/test_citations.py`** — C1-C4 (not opened in this design pass; implementer should re-read).
- **`tests/evals/test_no_llm_imports.py`** — the AST + URL-level boundary check. CRITICAL: `bot/services/llm_gateway.py` MUST remain in `ALLOWED_LLM_IMPORT_FILES` only via the providers it imports lazily. The current allowlist is `frozenset(["bot/services/llm_providers/anthropic.py", "bot/services/llm_providers/openai.py"])` (line 45-50 of `test_no_llm_imports.py`). T6-03 changes to `llm_gateway.py` MUST NOT add a top-level `import anthropic` or `import openai` — providers must continue to be imported via the `bot.services.llm_providers` Protocol surface only. If T6-03 implementer adds a non-provider URL string anywhere in `bot/`, the `_LLM_GUARD_HOSTNAMES` line-scan in `test_i4_no_llm_provider_url_outside_gateway` (lines 174-193 of `test_no_llm_imports.py`) will fail.

### New tests T6-03 should add

These are NEW test files (not modifications to existing eval tests):

#### 5.1. `tests/services/test_llm_gateway_extract_candidates.py` (unit-level, fake repos)

Coverage:
- **Empty bundle short-circuit**: `extract_candidates(session, source_versions=[], ...)` returns `{"candidates": [], "llm_usage_ledger_id": None}` without calling `ledger_repo.record` and without calling `provider.call`.
- **Provider returns valid candidates**: returns dict with non-empty `candidates` and non-None ledger id; `ledger_repo.update_placeholder` called once with `error=None`.
- **Provider returns candidate with hallucinated message_version_id**: candidate dropped from output, but no error raised — the kept candidates ride through normally.
- **Provider returns no candidates**: result `{"candidates": [], "llm_usage_ledger_id": <id>}` — ledger row has `error="no_valid_candidates"`.
- **`ProviderTransientError`**: returns empty candidates, ledger row updated with `error="provider_transient:<subtype>"`.
- **`ProviderStructuralError`**: returns empty candidates, ledger row updated, stop signal `"llm_provider_structural"` emitted.
- **`Exception` (unknown)**: returns empty candidates, ledger row updated with `error="provider_unknown:<class>"`.
- **Budget exceeded**: ledger row written with `error="budget_exceeded"`, returns empty candidates.
- **Citation hallucination — all candidates invalid**: result has `candidates=[]` AND `error="no_valid_candidates"` on the ledger row.

Reuse `FakeLedgerRepo` + `FakeProvider` pattern from `tests/services/test_llm_gateway.py:140-228,289-300`.

#### 5.2. `tests/evals/test_extract_candidates_binding.py` (binding-style test against real DB)

Coverage:
- **E1 (analog of L1)**: offrecord chat_message with `memory_policy='offrecord'` is in the DB. Call `run_extraction_pass` with a window covering it. Assert `result.run_status == "completed"` AND `result.candidate_count == 0` AND no `extraction_candidates` row exists. (T6-02 should already do this — the extractor's SELECT filter excludes the row before the gateway. Test confirms the chain still works under T6-03 gateway wiring.)
- **E2 (analog of L4)**: redacted message present (`is_redacted=true`). Same assertion.
- **E3 (analog of L3a)**: forget_event tombstone matches a message. Same assertion.
- **E4 (gateway-bypass attempt)**: bypass the extractor SELECT by injecting a forbidden mvid directly into the gateway with a fake adapter. Assert the gateway either drops it (citation hallucination — not in `valid_mvid_set`) OR (defense-in-depth) the gateway re-validates and drops the candidate. Stretch goal: implementer's call.
- **E5 (clean-input happy path)**: normal-policy message + fake provider returning 1 valid candidate ⇒ `run_extraction_pass` writes 1 `extraction_candidates` row + 1 `extraction_runs` row (`run_status=completed`) + 1 `llm_usage_ledger` row.

The test seeds the DB with messages, runs the production extraction pipeline through `bot/services/extractor.py`, and verifies via SQL queries.

Test infrastructure: `tests/evals/conftest.py` already supports `eval_db_session` (used by `test_leakage.py:34`). The new file should use the same fixture.

---

## 6. Files touched (planned)

### NEW files

- `tests/services/test_llm_gateway_extract_candidates.py` — unit tests for new gateway method.
- `tests/evals/test_extract_candidates_binding.py` — binding-style tests confirming privacy invariants on the extraction path (analog of L1-L5 / R1-R4 on the recall path).

### MODIFY (minimal)

- `bot/services/llm_gateway.py` — add `extract_candidates(...)`, `LiveExtractCandidatesGateway`, optional `_load_gateway_config_extract` helper. Extend `__all__`. **No new top-level imports of `anthropic` / `openai`** (Phase 11 invariant #2).
- `bot/__main__.py` — add `admin_extract` to `bot.handlers` import block (line 11-23) and to `dp.include_routers(...)` (line 106-118).
- `bot/services/scheduler.py` — add a `run_extraction_scheduler_tick` wrapper + `scheduler.add_job` registration. Import `extraction_scheduler_tick` from `bot.services.extractor` + `LiveExtractCandidatesGateway` from `bot.services.llm_gateway`.
- `bot/handlers/admin_extract.py` — drop the `gateway: ExtractCandidatesGateway` kwarg from `cmd_admin_extract` signature (line 121); instead build a local `LiveExtractCandidatesGateway` inside the handler body before calling `run_extraction_pass`. This is a small breaking change vs T6-02's signature but aligns with the Phase 5 QA handler precedent (local instantiation, not aiogram middleware DI).
- `scripts/lint_privacy_check.sh` — if any new test file paths trigger the privacy lint, add them to the allowlist (lines 7-39). Most likely needed for `tests/services/test_llm_gateway_extract_candidates.py` and `tests/evals/test_extract_candidates_binding.py`.

### OUT OF SCOPE for T6-03 (deferred to later tickets)

- T6-04 admin commands (`/candidates`, `/approve`, `/reject`) — Wave 2 Stream C.
- T6-05 (`/cards`, `/card`) — Wave 2 Stream C.
- T6-06 search extension `include_cards=True` — Wave 2 Stream D.
- T6-07 `EvidenceItem.source_type` discriminator — Wave 2 Stream D.
- T6-08 web UI — Wave 3 Stream E.
- T6-09 integration test (candidate → card → recall) — Wave 2 closeout.
- Real Anthropic structured-output API integration — Phase 5/6 future work; current provider returns `tuple()` for citations (`bot/services/llm_providers/anthropic.py:112`).
- `extraction_decisions` write — Wave 2 Stream C (at `/approve` time).
- `card_sources` write — Wave 2 Stream C.

---

## 7. Risk register

### R-1 — Privacy regression on the forget_events race window
**Already closed by T6-02 commit `242b0f8`** (CRITICAL #3 — `_bundle_is_clean` re-queries `forget_events` after `_select_eligible_sources`). T6-03 must not weaken this guard. The gateway is downstream of `_bundle_is_clean` and trusts its input. If the implementer adds re-filtering inside the gateway, ensure it does not OVERRIDE the extractor's authoritative filter.

### R-2 — Ledger write atomicity on failure path
The T6-02 extractor wraps the gateway call in `session.begin_nested()` SAVEPOINT (`bot/services/extractor.py:606`). If the gateway raises, the savepoint rolls back any `ledger_repo.record` writes the gateway might have made. **This means the gateway MUST NOT rely on the placeholder row persisting if it raises.** Either:
- Always catch all exceptions inside `extract_candidates` and return the placeholder ledger id (current pseudocode in §1), OR
- Let exceptions propagate AND ensure the caller observes the savepoint rollback semantically (T6-02 already does this — `run_extraction_pass` updates the `ExtractionRun.run_status='failed'` row in the OUTER transaction, lines 612-627 of T6-02 worktree).

Recommended: **the gateway catches all provider exceptions and returns `{"candidates": [], "llm_usage_ledger_id": <id>}`** so the savepoint succeeds and the ledger row is durable. Exceptions should only escape if they represent a bug in the gateway itself (e.g., `KeyError` in `_estimate_cost` for unknown model — which already emits stop signal + returns `Decimal("0")` in Phase 5 path, line 882-896).

### R-3 — Router DI design choice forecloses future flexibility
The chosen local-instantiation pattern (§3) is simpler and matches Phase 5 precedent. If a future ticket needs hot-swappable gateway implementations (e.g., per-chat A/B), local instantiation must be replaced by aiogram DI middleware. Document this in the PR: "T6-03 prefers local instantiation following Phase 5 QA pattern; aiogram-DI refactor is a follow-up if hot-swap becomes a requirement."

### R-4 — Phase 11 binding sub-gate failure blocks merge
This is the **most likely failure mode**. If `tests/evals/test_no_llm_imports.py::test_i1_no_llm_provider_imports_anywhere_in_bot` fails, it means T6-03 added a top-level `import anthropic` or `import openai` outside the allow-list. Triage: move the import to lazy inside `bot/services/llm_providers/{anthropic,openai}.py`, NOT into `llm_gateway.py`.

If `tests/evals/test_leakage.py` fails, it's a regression on the /recall path that should NEVER happen from a T6-03 change unless the implementer touched `bot/services/{qa.py,search.py,evidence.py,governance.py}` — which T6-03 should not.

If the new `tests/evals/test_extract_candidates_binding.py` fails, the privacy invariant is genuinely broken; do NOT merge.

### R-5 — Provider returns no citation parsing (T5-04 follow-up)
Current Anthropic provider returns `citation_ids=tuple()` always (`bot/services/llm_providers/anthropic.py:112`). For Phase 6 extraction, the analog is the parsed candidate list. T6-03 implementer must NOT depend on provider-level citation parsing; the `source_message_version_ids` per candidate come from the response BODY (JSON envelope), not from the provider's `citation_ids` field. This means `_parse_extraction_response` is the canonical extractor in §1 pseudocode.

### R-6 — Lint privacy baseline rebase fragility
Per global memory `feedback-lint-privacy-rebase-fragility.md`: `scripts/lint_privacy_check.sh` uses path:line:content baselines; rebases against main can shift line numbers and produce false-positive failures. Workaround: extend the allowlist in `scripts/lint_privacy_check.sh` (path-pattern match, not line-based). If line-based baselines are used elsewhere, prefer rebasing AFTER T6-02 merges to minimize drift.

### R-7 — `_load_gateway_config` duplication
Phase 5's `_load_gateway_config` (`bot/handlers/qa.py:103-133`) reads `LLM_PROVIDER`, `LLM_MODEL`, `LLM_DAILY_USD_CEILING`, `LLM_MONTHLY_USD_CEILING`. T6-03 should NOT duplicate this — factor it out into `bot/services/llm_gateway.py` (preferred) and import from both call sites. The env-var names MUST NOT change (global rule).

---

## 8. PAR strategy

### Reviewer split

Mirror PHASE6_PLAN.md §6 reviewer pattern. T6-03 is Phase 6 Wave 1, where parallel sprint PRs and critical governance apply.

- **Claude product reviewer** (`subagent_type: standard-product-reviewer` or `deep-product-reviewer` if governance escalation is required): spec compliance (PHASE6_PLAN.md §7 T6-03 checklist + T6-02 addendum), `T6-04 hand-off completeness` (does T6-03 leave the right surface for `/approve` work in T6-04?), `extraction_runs` durable audit invariant (every gateway call ⇒ ledger row).
- **Codex technical reviewer** (`$TIMEOUT_CMD 600 codex exec review --base main -m gpt-5.5 -c model_reasoning_effort=high --ephemeral`): correctness (placeholder ledger row lifecycle, budget guard race-window vs Phase 5 placeholder pattern), Protocol conformance (does `LiveExtractCandidatesGateway` actually implement `ExtractCandidatesGateway` from T6-02 byte-for-byte?), ledger atomicity (rollback semantics under SAVEPOINT), citation hallucination handling.

### PAR rounds expected

Phase 6 complexity ≥1 round of fixes is the realistic baseline. T6-02 took 5+ rounds (Codex CRITICAL #1 + CRITICAL #3 + HIGH #3 + HIGH #4 + HIGH #5 + MED #2). T6-03 should plan for ≥2 rounds:
- **Round 1**: spec compliance + Protocol shape + lint privacy.
- **Round 2**: race-window correctness + lifecycle invariants.
- **Round 3**: Phase 11 binding sub-gate pass.

### Sub-gate enforcement

Before any PR-ready signal:

```bash
EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x --timeout=60 \
  tests/evals/test_leakage.py \
  tests/evals/test_citations.py \
  tests/evals/test_refusal.py \
  tests/evals/test_no_llm_imports.py
```

This MUST be re-run on the T6-03 PR head specifically (not just nightly). PR description should include the test run output (paste verbatim per discipline rule "tests with evidence").

---

## 9. Open questions

The following points are NOT clearly specified in PHASE6_PLAN.md §7 T6-03 acceptance or in the T6-02 addendum. Implementer should clarify with the orchestrator before starting:

1. **Prompt template v0.1.0 wire format.** What exact JSON envelope must the prompt instruct the model to emit? Recommend: `[{"candidate_json": {"title": str, "summary": str, "tags": [str]}, "source_message_version_ids": [int]}, ...]`. Plan does not pin this; implementer decides + documents in `prompt_template_version="v0.1.0"` semantics in the docstring.

2. **`extract_candidates` budget ceiling source.** Phase 5 uses `LLM_DAILY_USD_CEILING` + `LLM_MONTHLY_USD_CEILING`. Is the extraction budget shared with synthesis (same env vars, same `LedgerRepo.daily_cost_usd` sum) or separate? Recommend: SHARED (single ledger, simpler accounting). Document this decision.

3. **Scheduler tick interval.** PHASE6_PLAN.md Q2 says scheduler is OFF by default; the interval doesn't matter until an operator enables the flag. 15 min mirrors `check_vouch_deadlines` precedent; implementer's choice or new env var `EXTRACTION_SCHEDULER_INTERVAL_MINUTES`.

4. **Anthropic vs OpenAI default for extraction.** Phase 5 default is `anthropic` (`bot/handlers/qa.py:116`). Should extraction default differ? Recommend: same default; share `LLM_PROVIDER` env var.

5. **Reentrant gateway call?** Could `extract_candidates` and `synthesize_answer` run concurrently in the same session? Recommend NO — both acquire `LLM_BUDGET_LOCK_ID` (`bot/services/llm_gateway.py:199-201`), so they're serialized at the budget read. Document this if it surprises a reviewer.

6. **`_parse_extraction_response` error handling.** If the LLM returns malformed JSON, should the gateway abstain (return empty candidates) or raise? Recommend: abstain with `error="response_parse_error"` on ledger row. Falls under R-2 above.

7. **Where should `LiveExtractCandidatesGateway` live?** Option A: alongside `extract_candidates` in `bot/services/llm_gateway.py`. Option B: dedicated `bot/services/llm_gateway_extract.py`. Recommend A for proximity to the function it wraps.

8. **Should T6-03 ship the binding-test fixture alongside `tests/evals/test_extract_candidates_binding.py`?** PHASE6_PLAN.md §6 sub-gate refers to the four existing eval test files only. Adding a new file under `tests/evals/` is the implementer's call; the new file must use `EVAL_HARNESS_ENABLED=1` gating and `eval_db_session` fixture.

---

## Evidence log (files read for this design)

- `docs/memory-system/PHASE6_PLAN.md` (682 lines) — full plan, §§1-11 + Sprint 0 resolutions + T6-02 addendum at lines 528-540 / 547-548 (from T6-02 worktree).
- `.worktrees/p6-w1-stream-b/bot/services/extractor.py` (788 lines, T6-02 worktree HEAD `182095f`) — `ExtractCandidatesGateway` Protocol seam (lines 113-148), `run_extraction_pass` (lines 491-698), 3-stage atomic lifecycle (HIGH #3), invariant #4 ledger guard (lines 636-654), SELECT→gateway race close (lines 371-448), scheduler-tick advisory lock (lines 61-101).
- `.worktrees/p6-w1-stream-b/bot/handlers/admin_extract.py` (219 lines, T6-02 worktree) — admin handler signature, window parser, current `gateway: ExtractCandidatesGateway` kwarg pattern, summary reply format.
- `bot/services/llm_gateway.py` (912 lines, main HEAD) — `synthesize_answer` reference: 7 pre-call invariants, placeholder ledger pattern (lines 478-567), budget guard SQL constants (lines 313-321), citation enforcement (lines 671-693), provider error taxonomy (lines 569-642), `_estimate_cost` (lines 863-896).
- `bot/handlers/qa.py` (399 lines, main HEAD) — Phase 5 handler precedent: `_load_gateway_config` (lines 103-133), `_resolve_provider` (lines 136-148), `synthesize_answer` call site with local `LedgerRepo()` + `SynthesisCacheRepo()` (lines 332-343), 4-step ORDER (Step 1 QaTrace before gateway, Step 2 dispatch, Step 3 update LLM fields, Step 4 render).
- `bot/__main__.py` (171 lines, main HEAD) — `dp.include_routers(...)` block (lines 106-118), middleware order (lines 102-103), startup hook (lines 121-154), allowed_updates rule (lines 50-56).
- `bot/services/scheduler.py` (290 lines, main HEAD) — apscheduler job pattern (`scheduler.add_job(...)` lines 231-281), session lifecycle (`async with async_session()`).
- `bot/db/repos/llm_usage_ledger.py` (165 lines) — `LedgerRepo` Protocol surface: `record` (lines 30-68), `daily_cost_usd` / `monthly_cost_usd` (lines 70-118), `update_placeholder` (lines 120-165) with `LookupError` on missing placeholder.
- `bot/services/llm_pricing.py` (78 lines) — `estimate_cost` + `MODEL_PRICING` table.
- `bot/services/llm_providers/__init__.py` (74 lines) — `LLMProvider` Protocol, `ProviderResult` NamedTuple, `ProviderTransientError` / `ProviderStructuralError` taxonomies.
- `bot/services/llm_providers/anthropic.py` (120 lines) — current Anthropic provider, lazy SDK import, error mapping.
- `tests/evals/test_leakage.py` (386 lines) — L1-L5 binding test cases for /recall path, uses `bot.services.eval_runner::run_eval_recall`.
- `tests/evals/test_refusal.py` (365 lines) — R1-R4 binding test cases.
- `tests/evals/test_no_llm_imports.py` (213 lines) — Phase 11 invariant #2 AST + URL boundary check, `ALLOWED_LLM_IMPORT_FILES` allowlist (lines 45-50), `LLM_PROVIDER_PREFIXES` (lines 26-39).
- `tests/evals/_llm_guard.py` (header) — `LLM_GUARD_HOSTNAMES` block-list source.
- `tests/services/test_llm_gateway.py` (1689 lines, first 300 read) — `FakeLedgerRepo` (lines 140-228), `FakeCacheRepo` (lines 231-286), `FakeProvider` (lines 289-300) — reuse patterns for §5.1.
- `bot/db/models.py` lines 920-1148 — `ExtractionRun` ORM (Phase 6 / T6-01 / alembic 030), `ExtractionCandidate` ORM (alembic 031), `KnowledgeCard` ORM (alembic 032).
- `alembic/versions/030_add_extraction_runs.py` (header + first 80 lines) — confirms `llm_usage_ledger_id BIGINT NULLABLE` FK with `ON DELETE SET NULL`.
- `scripts/lint_privacy_check.sh` (first 60 lines) — privacy-lint allowlist patterns.
- T6-02 worktree `git log --oneline` — confirms commit sequence: `8ed09cc` extractor, `b9f76de` admin handler, `dcc2c67` lint allowlist, `50d6f7a` CRITICAL #1, `242b0f8` CRITICAL #3, `dd586f9` HIGH #3 atomic 3-stage lifecycle, `bbbfb01` HIGH #4 scheduler advisory lock, `6826556` HIGH #5 operator_user_id audit marker, `63d7385` MED #2 `@runtime_checkable`, `182095f` T6-03 acceptance addendum.

---

## Ambiguities for orchestrator

- **Section 8 § "T6-04 hand-off completeness"**: T6-03 does NOT need to ship `/approve` re-validation or `card_sources` writes. Those are T6-04. The T6-03 PR description should explicitly note "Does NOT implement `/approve`; deferred to T6-04 / Wave 2 Stream C." This avoids confusion in the FHR.

- **Sub-gate "evals.yml workflow not sufficient"**: the nightly workflow runs against `main`. T6-03 PR head is a branch. The implementer MUST manually run the four-test command (§5) on the PR's HEAD commit AND paste output in the PR. The orchestrator may want to enforce this via a CI workflow gate; not in T6-03 scope.

- **Whether `extract_candidates` writes ANY `extraction_candidates` rows directly**: NO. The gateway returns the candidate list; `run_extraction_pass` (T6-02) writes the rows. This separation is correct — the gateway is provider-facing, the extractor is DB-facing. Implementer must not write `extraction_candidates` rows from inside `extract_candidates`.

END of document.
