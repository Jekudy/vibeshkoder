# Phase 11 Plan — Shkoderbench / Evaluation Harness

**Status:** RATIFIED 2026-05-02. Authorized for implementation in parallel with Phase 5.
**Owner:** Orchestrator C (`ORCHESTRATOR_REGISTRY.md §1`).
**Charter:** `governance_mode = critical`, `git_workflow_mode = parallel_wave_prs`. Per-PR PAR (Claude product + Codex technical). FHR mandatory at end of phase.

---

## §0. Banner

✅ AUTHORIZED — Phase 11 evals harness, offline / CI-only.

This is the canonical Phase 11 plan. The earlier file `prompts/PHASE11_PLAN_DRAFT.md` is a **deferred draft for person expertise pages** (a future Phase 6+ feature) and is NOT this plan. See §11 for the reconciliation note.

**Supersede note (2026-05-02).** This plan **supersedes the original Phase 11 scope sketched in `HANDOFF.md §Phase 11` (lines ~476-480) and the `add_eval_tables` migration row in `HANDOFF.md §5` (line ~715).** Per `AUTHORIZED_SCOPE.md §Phase 11` (ratified 2026-04-30, narrowed to offline / CI-only), Phase 11 ships **no DB tables** (`eval_cases` / `eval_runs` / `eval_results`), **no migrations**, and **no admin eval view** in this iteration. If durable eval persistence is later required, it lands in a successor phase (e.g., a future Phase 11.x or 13) under separate authorization. HANDOFF.md will be reconciled in a follow-up shared-file PR (Sprint 0 keeps shared-file edits minimal per `ORCHESTRATOR_REGISTRY.md §3.3`).

**No production runtime impact.** Phase 11 ships:

- a new top-level test category `tests/evals/`
- an offline harness module `bot/services/eval_runner.py` (no production wiring)
- golden fixtures under `tests/fixtures/golden_recall/` and `tests/fixtures/eval_seeds/`
- a CI nightly workflow `.github/workflows/evals.yml`, **gated by env var, default OFF** until baseline established

**No alembic migrations.** **No new production handlers.** **No LLM imports.**

---

## §1. Invariants (verbatim from `HANDOFF.md §1`)

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

**Phase 11 is the binding compliance gate for invariants 2, 3, 4, 9** when downstream phases (5, 6, 7, 9, 10) come online. Every Phase 11 test category exists to detect a violation of one of these invariants:

| Invariant | Binding category | Failure mode caught |
|-----------|------------------|---------------------|
| 2 — no LLM outside `llm_gateway` | §5.6 Import-graph guard | a service module silently imports `anthropic`/`openai`/etc. |
| 3 — no q&a over offrecord/nomem/forgotten | §5.1 Leakage tests (L1-L5) | a redacted/tombstoned mv_id surfaces in `bundle.items` |
| 4 — citations point to `message_version_id` | §5.2 Citation tests (C1-C4) | non-abstaining answer with empty/invalid mv_id |
| 9 — tombstones durable | §5.1 L3 (forgotten) | tombstone present but mv_id still returned by search |

---

## §2. Objective

Build a deterministic, programmatic evaluation harness that:

1. Replays a **golden seed** of (chat history, query, ground-truth) tuples through `/recall` and downstream answer surfaces.
2. Asserts **leakage = 0**: no `#offrecord` / `#nomem` / forgotten / redacted content appears in any answer or evidence bundle.
3. Asserts **citation correctness**: every non-abstaining answer cites at least one valid `message_version_id` that exists in the visible (governance-filtered) set for the seed.
4. Asserts **abstention correctness**: when the seed contains zero ground-truth evidence, `/recall` produces `abstained=True` and no fabricated content.
5. Reports **recall@K** and **precision@K** against ground truth (K ∈ {1, 3, 5}).

The harness is the **regression baseline** that Phase 5 (LLM-synthesized answers), Phase 6 (cards), Phase 9 (wiki), Phase 10 (graph) must pass before their respective closures.

### Out of scope (this phase)

- LLM-as-judge metrics (added in Phase 11 Wave 3 only after Phase 5 closes — uses `llm_gateway` exclusively).
- Online / staging traffic replay. This is offline only.
- Person expertise pages (separate future feature; see §11).

---

## §3. Source contract — what we evaluate against

**Phase 4 baseline (current `/recall` surface):**

- Entry point: `bot/handlers/qa.py::recall_handler`
- Service: `bot/services/qa.py::run_qa(session, query, chat_id, redact_query_in_audit)`
- Search: `bot/services/search.py::search_messages` (governance-filtered FTS)
- Output: `bot/services/evidence.py::EvidenceBundle` (frozen dataclass; `abstained` flag; `evidence_ids` property)
- Audit: `bot/db/repos/qa_trace.py::QaTraceRepo.create`

**Governance filter (already inline in `search_messages`):**

```sql
WHERE c.memory_policy = 'normal'
  AND c.is_redacted = FALSE
  AND mv.is_redacted = FALSE
  AND NOT EXISTS ( ... forget_events tombstone JOIN ... )
```

Phase 11 **re-asserts** the filter holds end-to-end via independent post-hoc inspection of returned `message_version_id`s, not by trusting the SQL.

**Phase 5 regression target (Wave 3, after Orch A closes):**

- `bot/services/llm_gateway.py` (single-entry LLM)
- `qa_traces.answer_text`, `qa_traces.citation_count`, `qa_traces.llm_call_id` (Phase 5 schema extension per `AUTHORIZED_SCOPE.md §Phase 5`)
- LLM-synthesized `/recall` answer when ≥1 evidence + flag enabled

---

## §4. Architecture (offline harness)

```text
tests/evals/                            ← pytest entry points (collected only when EVAL_HARNESS_ENABLED=1)
  conftest.py                           ← seeds DB, applies fixture, returns AsyncSession
  test_leakage.py                       ← invariant 3 binding
  test_citations.py                     ← invariant 4 binding
  test_refusal.py                       ← abstention correctness (no hallucination)
  test_recall_precision.py              ← @1/@3/@5 metric reporters
  test_phase5_regression.py             ← Wave 3 only; xfail until Orch A Phase 5 merges

bot/services/eval_runner.py             ← programmatic /recall caller; NO new prod wiring
bot/services/eval_seeds.py              ← seed loader / hasher / version
bot/services/eval_metrics.py            ← deterministic recall@K / precision@K

tests/fixtures/golden_recall/
  seed_v1/
    chat_history.jsonl                  ← messages → bot.services.ingestion fixture-seeded
    queries.jsonl                       ← {query, expected_message_version_ids[], expected_abstain: bool}
    seed_meta.yaml                      ← seed_id, version, hash, governance markers manifest

tests/fixtures/eval_seeds/
  leakage_offrecord.jsonl               ← messages with #offrecord; queries that would surface them
  leakage_nomem.jsonl                   ← #nomem markers
  leakage_forgotten.jsonl               ← forget_events tombstone fixtures
  refusal_no_evidence.jsonl             ← queries with no ground-truth in seed

.github/workflows/evals.yml             ← nightly cron + manual dispatch; gate: secrets.EVAL_HARNESS_ENABLED == 'true'
```

**Read path:** harness uses the **same** `run_qa` and `search_messages` that production uses. No bypass, no parallel re-implementation. We test the real path; the harness only owns seed / oracle / metric.

**Write path:** harness writes ONLY to:

- ephemeral test postgres schema (per-test-class isolation)
- a side-effect-free `eval_results.jsonl` artifact uploaded by CI

Harness MUST NOT write to production tables. Harness MUST NOT call the live Telegram bot.

---

## §5. Test categories (binding contract)

### §5.1 Leakage tests (`tests/evals/test_leakage.py`)

| Sub-case | Seed | Query | Assertion |
|----------|------|-------|-----------|
| L1 offrecord | seed contains a message tagged `#offrecord` (redacted in `chat_messages` + `message_versions`) | query whose unredacted text would match | `bundle.items` does NOT contain that `message_version_id`; `bundle.abstained` may be true; assertion: `bundle.evidence_ids` ∩ `seed.offrecord_message_version_ids` == ∅ |
| L2 nomem | seed contains a `#nomem` policy row | query that would match | identical assertion against `seed.nomem_message_version_ids` |
| L3 forgotten | seed contains a `forget_events` tombstone over a user_id and message hash | query that would match the forgotten message | identical assertion against `seed.tombstoned_message_version_ids` |
| L4 redacted | seed contains `is_redacted=TRUE` rows | query | identical assertion |
| L5 cross-chat | seed has matching content in chat A and chat B; query targets chat A | bundle items all have `chat_id == A` |

**Failure semantics:** any non-empty intersection = test FAIL with full evidence dump (bundle items, seed manifest, governance markers). No retries.

### §5.2 Citation tests (`tests/evals/test_citations.py`)

| Sub-case | Assertion |
|----------|-----------|
| C1 every-item-has-id | every `EvidenceItem.message_version_id` is a positive int that resolves in the live DB |
| C2 cited-row-visible | for each cited `mv_id`, the underlying `chat_messages` row satisfies `memory_policy='normal' AND is_redacted=FALSE` |
| C3 cited-row-not-tombstoned | for each cited `mv_id`, no `forget_events` row matches its tombstone keys |
| C4 audit-trace-matches | `qa_traces.evidence_ids` after `/recall` == `bundle.evidence_ids` |

### §5.3 Refusal tests (`tests/evals/test_refusal.py`)

| Sub-case | Seed | Query | Assertion |
|----------|------|-------|-----------|
| R1 empty-seed | empty governance-filtered set | any non-empty query | `bundle.abstained == True` AND `bundle.items == ()` |
| R2 only-redacted | seed contains only redacted/forgotten content | query that would match unredacted | same as R1 |
| R3 wrong-chat | query targets chat with no membership | handler returns access-denied; no `bundle` produced |
| R4 empty-query | query is empty/whitespace | handler returns usage hint; no DB read |

**No-hallucination assertion (Wave 3, Phase 5 LLM-synthesis):** answer text MUST NOT contain claims unsupported by `bundle.items[i].snippet`. Implemented as substring-overlap heuristic in Wave 3; LLM-as-judge gated.

### §5.4 Recall@K / Precision@K (`tests/evals/test_recall_precision.py`)

For each query in `golden_recall/seed_v1/queries.jsonl` with `expected_message_version_ids`:

- `recall@K = |returned[:K] ∩ expected| / |expected|`
- `precision@K = |returned[:K] ∩ expected| / K`

**K ∈ {1, 3, 5}.** Baseline thresholds **set after first run** (no fictional thresholds — establish empirically). Test fails if metric drops below baseline minus a tolerance band (defined in `seed_meta.yaml`).

### §5.5 Determinism (`tests/evals/test_determinism.py` — Wave 1)

Same seed + same query run twice in the same process MUST produce byte-identical `bundle.evidence_ids`. Catches non-deterministic orderings before they corrupt baseline.

### §5.6 Import-graph guard (`tests/evals/test_no_llm_imports.py` — Wave 1)

**Binds invariant 2** (no LLM calls outside `llm_gateway`). This is a static / AST-level test that does not depend on any runtime fixture.

| Sub-case | Assertion |
|----------|-----------|
| I1 | walking the AST of every file under `bot/` (excluding `bot/services/llm_gateway.py` once Phase 5 introduces it), no `Import` / `ImportFrom` node references modules in `{anthropic, openai, langchain, langchain_*, transformers, huggingface_hub, ollama, cohere, mistral, replicate}` |
| I2 | `pyproject.toml` `[project.dependencies]` does not list any of the above as a direct runtime dep (Phase 5 will add them under a clearly-named section limited to the gateway path) |
| I3 | Once `llm_gateway.py` exists (Phase 5+), it is the **only** Python file allowed to do those imports — assertion compares observed import sites set against the allow-list `{"bot/services/llm_gateway.py"}` |

**Failure semantics:** any non-allow-listed import site = test FAIL with the offending file path + import statement quoted. No retries.

This category is wired in Wave 1 (T11-W1-05) so it is green BEFORE Phase 5 starts merging LLM code, and provides a definitive automated gate for invariant 2 — replacing the prior "reviewer vigilance" enforcement.

---

## §6. Wave allocation

### Wave 1 — Harness skeleton (no LLM)

Tickets T11-W1-01 .. T11-W1-07. Goal: produce a green CI run on Phase 4 baseline.

| Ticket | Title | Owns |
|--------|-------|------|
| T11-W1-01 | `bot/services/eval_runner.py` skeleton | calls `run_qa` programmatically; returns `(bundle, qa_trace)` |
| T11-W1-02 | `bot/services/eval_seeds.py` + seed loader | loads JSONL seed; computes seed_hash; resolves message_version_ids post-fixture-apply |
| T11-W1-03 | `bot/services/eval_metrics.py` | deterministic recall@K / precision@K |
| T11-W1-04 | `tests/evals/conftest.py` + golden_recall/seed_v1 fixture | 20+ messages, 8+ queries with expected ids |
| T11-W1-05 | `tests/evals/test_determinism.py` + smoke `test_recall_precision.py` + `tests/evals/test_no_llm_imports.py` (§5.6 I1-I3) | Phase 4 baseline metrics + invariant 2 binding test |
| T11-W1-06 | `.github/workflows/evals.yml` (gated default OFF) + `eval_results.jsonl` artifact schema | nightly cron + workflow_dispatch + structured verdict output |
| T11-W1-07 | Privacy-allowlist CI gate + local pre-commit hook proposal (per §7 #5) | mechanical enforcement of `tests/fixtures/eval_seeds/leakage_*` allowlist |

### Wave 2 — Privacy + correctness binding (Phase 4 baseline)

Tickets T11-W2-01 .. T11-W2-04. Runs against shipped Phase 4 `/recall`. **This is the privacy gate for Phase 5 closure.**

| Ticket | Title | Categories |
|--------|-------|-----------|
| T11-W2-01 | `tests/evals/test_leakage.py` (L1–L5) | invariant 3 |
| T11-W2-02 | `tests/evals/test_citations.py` (C1–C4) | invariant 4 |
| T11-W2-03 | `tests/evals/test_refusal.py` (R1–R4) | abstention correctness |
| T11-W2-04 | Wave 2 baseline freeze | record metric thresholds in `seed_meta.yaml`; flip CI workflow `EVAL_HARNESS_ENABLED=true` after FHR sign-off |

**Wave 2 closure gate:** all Wave 2 categories pass on Phase 4 baseline. After this, Phase 11 exists as a binding regression suite for any Phase 5+ change.

### Wave 3 — Phase 5 regression (after Orch A closes Phase 5)

Tickets T11-W3-01 .. T11-W3-04. Triggered when Orch A submits Phase 5 closure PR.

| Ticket | Title | Notes |
|--------|-------|-------|
| T11-W3-01 | LLM-synthesis hallucination test | substring-overlap of answer_text vs cited snippets; ≥X% overlap required |
| T11-W3-02 | Citation drift test | answer_text-cited mv_ids must be subset of `bundle.evidence_ids` from search |
| T11-W3-03 | Cost / latency benchmark | wall-clock + `llm_usage_ledger.cost_usd_cents`; soft thresholds (warn-only at first) |
| T11-W3-04 | Phase 11 FHR | independent reviewer pass over the entire Wave 1+2+3 PR set |

---

## §7. Stop signals (binding for executor agents)

If any of these triggers in implementation, STOP and escalate:

1. Subagent proposes adding a production handler / runtime endpoint → outside scope.
2. Subagent proposes an alembic migration → outside scope (Phase 11 is no-schema-change).
3. Subagent proposes importing `anthropic`, `openai`, `langchain`, `transformers`, `ollama`, `huggingface` anywhere in `bot/` → invariant 2 violation.
4. Eval reports 100% pass on first run → suspect false-negative; verify the assertion path is wired before celebrating.
5. New seed file contains the literal strings `#offrecord`, `#nomem`, `forgotten`, `nomem` in a position OTHER than a leakage-test fixture explicitly designed to verify exclusion. **Formal allowlist (binding):** these literals are permitted ONLY in files matching exactly one of these glob patterns:
   - `tests/fixtures/eval_seeds/leakage_offrecord*.jsonl`
   - `tests/fixtures/eval_seeds/leakage_nomem*.jsonl`
   - `tests/fixtures/eval_seeds/leakage_forgotten*.jsonl`
   - `tests/fixtures/eval_seeds/leakage_redacted*.jsonl`

   Any other path containing these literals = pre-commit gate FAIL. Implemented as a CI check in T11-W1-06 (`evals.yml`) AND a local pre-commit hook proposal in T11-W1-07 (new ticket added per PAR feedback). The allowlist exists because §5.1 requires these literals to verify exclusion semantics — they are intentionally present in a tightly-scoped fixture set, never in source code, plan docs, or other test fixtures. Even within allow-listed seed files, the harness MUST treat the seed as durable storage of marker text and never write its content to logs / qa_traces / Telegram messages.
6. Orch A claims Phase 5 closed but Wave 3 has not run / has failures → Orch C blocks closure with explicit failing-seed dump.
7. CI workflow `evals.yml` is enabled (env var = true) before Wave 2 baseline frozen → revert immediately.
8. Eval baseline metric jumps upward by >10% without an explainable seed change → assume regression in Phase 4 surface, not improvement; investigate before re-baselining.

---

## §8. Coordination with sister orchestrators

### §8.1 Contract with Orch A (Phase 5 → 6 → 7 → 8)

- **Producer:** Orch C, Wave 2 baseline (Phase 4 evals green).
- **Consumer:** Orch A, Phase 5 closure precondition.
- **Mechanism:** `ORCHESTRATOR_REGISTRY.md §5` cross-dep table updated by Sprint 0 PR. Comment on Orch A's Phase 5 closing PR with run command + verdict.
- **Activation gate.** The contract is **NOT YET BINDING** at Sprint 0 ratification time, because `tests/evals/` is empty and the listed test files do not exist. The contract activates **at T11-W2-04 closure** (Wave 2 baseline freeze), at which point `tests/evals/test_leakage.py`, `tests/evals/test_citations.py`, `tests/evals/test_refusal.py`, and `tests/evals/test_no_llm_imports.py` MUST exist and MUST be green on `main`. Orch C announces activation via a comment on this PR's tracking thread + a dedicated row update in `ORCHESTRATOR_REGISTRY.md §5`.
- **Run command (canonical, activates at T11-W2-04 closure).** Use **explicit file paths**, never `pytest -k` — `-k` matches against test item names and silently runs zero tests if a future test rename drops the keyword:

  ```bash
  cd /Users/eekudryavtsev/Vibe/products/shkoderbot
  EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x --timeout=60 \
      tests/evals/test_leakage.py \
      tests/evals/test_citations.py \
      tests/evals/test_refusal.py \
      tests/evals/test_no_llm_imports.py
  ```

  The harness MUST also write a structured `eval_results.jsonl` artifact (one line per failing case + a final summary line) so Orch A can consume the verdict programmatically rather than scraping comment text. Schema (minimal): `{"verdict": "PASS"|"FAIL", "category": "leakage"|"citations"|"refusal"|"no_llm_imports", "case_id": "L1"|"L2"|...|"I1"|..., "seed_hash": "<sha256>", "harness_version": "<git-sha>", "evidence": <object>}`. The schema is finalized in T11-W1-06.

- **Verdict format (machine-readable, comment-mirrored):** `PASS` (Orch C ACKs Phase 5 closure) | `FAIL: <category> <case_id> — see eval_results.jsonl line N` (Orch A must fix before merge). The `eval_results.jsonl` artifact is the source of truth; the PR comment is the human echo.

### §8.2 Contract with Orch B (Phase 9 wiki + Phase 10 graph + Phase 12 docs)

- **Trigger:** Orch B's Phase 9 wiki page rendering ships → Orch C adds wiki citation-precision category in a new Wave 4 sprint (out of scope for this plan; tracked as future ticket T11-W4-01).
- **Until then:** no coupling.

### §8.3 Shared-file edits

Phase 11 touches the following shared files (per `ORCHESTRATOR_REGISTRY.md §2 Shared`):

- `docs/memory-system/IMPLEMENTATION_STATUS.md` — append T11-* rows on each merge.
- `docs/memory-system/ROADMAP.md` — flip Phase 11 row to "Authorized: in progress" → "DONE Wave 2" after baseline freeze.
- `CLAUDE.md` (root) — add the Phase 11 reading-list entry on Wave 2 close.
- `pyproject.toml` — IF a dev-dep is needed (e.g., `pytest-asyncio` already present? confirm). Coordinate via PR comment to A and B per §3.5 of REGISTRY.
- `.github/workflows/evals.yml` — new file owned by Orch C exclusively per `AUTHORIZED_SCOPE.md §Phase 11`.

---

## §9. PAR review protocol (per Sprint, per PR)

Per `~/.claude/rules/superflow-enforcement.md` Rule 3 + REGISTRY §3.6:

1. **Claude product reviewer** (`subagent_type: standard-product-reviewer`) — checks scope match, invariants intact, no production wiring leak, seed manifests do not contain raw `#offrecord` text in positive positions.
2. **Codex technical reviewer** via `Agent(subagent_type="codex:codex-rescue")` per `prompts/CODEX_DUAL_AGENT_PATTERN.md` — checks: no LLM imports, no migration, governance filter unchanged, eval runner does not write to production tables, CI workflow gated.
3. **Mandatory pre-PR doc update step** — `IMPLEMENTATION_STATUS.md` row + (Wave 2/3) `CLAUDE.md` reading list reference.
4. **`.par-evidence.json`** required before `gh pr create`.

**Hallucination guard (per memory `feedback-codex-hallucinated-citations`):** Codex verdicts are directionally correct but `file:line` citations may be hallucinated. Orch C verifies every cited path / symbol via `grep` before acting on a finding.

---

## §10. Initial ticket backlog (T11-*)

Created as GitHub issues with label `phase:11` in Sprint 0 PR follow-up.

| ID | Wave | Title | Streamable in parallel? |
|----|------|-------|-------------------------|
| T11-W1-01 | 1 | `bot/services/eval_runner.py` skeleton | yes |
| T11-W1-02 | 1 | `bot/services/eval_seeds.py` + JSONL loader | yes (after W1-01 import surface stable) |
| T11-W1-03 | 1 | `bot/services/eval_metrics.py` recall@K / precision@K | yes |
| T11-W1-04 | 1 | `tests/fixtures/golden_recall/seed_v1` + `conftest.py` | yes |
| T11-W1-05 | 1 | `test_determinism.py` + smoke `test_recall_precision.py` + `test_no_llm_imports.py` (§5.6 I1-I3) | sequential after W1-01..W1-04 |
| T11-W1-06 | 1 | `.github/workflows/evals.yml` gated nightly + `eval_results.jsonl` schema | yes |
| T11-W1-07 | 1 | Privacy-allowlist CI gate + local pre-commit hook proposal (per §7 #5) | yes |
| T11-W2-01 | 2 | `test_leakage.py` (L1–L5). **Split rule:** if implementer estimate >2 days, split into T11-W2-01a (L1+L2+L4) and T11-W2-01b (L3+L5). | yes |
| T11-W2-02 | 2 | `test_citations.py` (C1–C4) | yes |
| T11-W2-03 | 2 | `test_refusal.py` (R1–R4) | yes |
| T11-W2-04 | 2 | Baseline freeze + flip workflow ON + activate §8.1 Phase 5 binding | sequential, last |
| T11-W3-01 | 3 | LLM-synthesis hallucination test | after Orch A Phase 5 merged |
| T11-W3-02 | 3 | Citation drift test | after Orch A Phase 5 merged |
| T11-W3-03 | 3 | Cost / latency benchmark | after Orch A Phase 5 merged |
| T11-W3-04 | 3 | Phase 11 FHR | end of phase |
| T11-CHORE-01 | — | Follow-up: rename `prompts/PHASE11_PLAN_DRAFT.md` → `prompts/EXPERTISE_PAGES_DRAFT.md` (deferrable; per PAR §7 finding) | low priority |
| T11-CHORE-02 | — | Follow-up: reconcile HANDOFF.md §Phase 11 + §5 stale rows in a separate shared-file PR | low priority |

---

## §11. Reconciliation note — `prompts/PHASE11_PLAN_DRAFT.md`

The file `docs/memory-system/prompts/PHASE11_PLAN_DRAFT.md` (created earlier in design exploration) drafts **person expertise pages** ("who knows X"). That feature:

- is explicitly listed in `AUTHORIZED_SCOPE.md` as "Person expertise pages — Phase 6+" (NOT authorized in current cycle);
- conflicts with the canonical Phase 11 numbering established in `ROADMAP.md`, `HANDOFF.md §Phase 11`, and `AUTHORIZED_SCOPE.md §Phase 11`;
- is acknowledged by its own §0 banner: *"current canonical Phase 11 is Shkoderbench / evals, not person expertise pages."*

**Resolution:**

1. The draft remains in `prompts/` as a deferred design artifact. A header marker is added in the same Sprint 0 PR to flag deferral.
2. When Phase 6 cards close (Orch A), expertise pages may be re-numbered as a future Phase (e.g., Phase 13) and ratified separately.
3. This file (`PHASE11_PLAN.md`) is the canonical Phase 11 plan from 2026-05-02 forward.

---

## §12. Rollback plan

Phase 11 has **no production rollback** (no production wiring exists to roll back). Rollback paths:

- Failed CI workflow → set `secrets.EVAL_HARNESS_ENABLED='false'` (workflow-level disable, no code change).
- Bad seed corrupting the baseline → bump `seed_meta.yaml: version`; old seed remains as `seed_v0` archive; tests pin to `seed_v1`.
- False-positive leakage assertion blocking unrelated work → temporary `xfail` with linked tracking issue; never silent skip.

---

## §13. Definition of Done — Phase 11

Phase 11 is closed (per Paranoid Mode Rule 5) only when ALL of:

- [ ] All T11-* GitHub issues CLOSED.
- [ ] All Phase 11 PRs MERGED via `--rebase --delete-branch`. Never `--admin`.
- [ ] `IMPLEMENTATION_STATUS.md` reflects every ticket.
- [ ] `ROADMAP.md` Phase 11 row marked `DONE`.
- [ ] FHR (T11-W3-04) reviewers signed off `ACCEPTED + APPROVE`.
- [ ] On a fresh `main` checkout: `EVAL_HARNESS_ENABLED=1 pytest -x --timeout=60 tests/evals/` returns green.
- [ ] CI nightly `evals.yml` has been green for ≥3 consecutive nights.
- [ ] Orch A acknowledged Phase 11 Wave 2 as a binding gate for Phase 5 closure.

Anything less = "in progress."
