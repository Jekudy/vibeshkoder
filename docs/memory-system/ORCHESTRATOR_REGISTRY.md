# Orchestrator Registry — Memory System Phase 5+

**Purpose.** Coordinate 3 parallel paranoid orchestrators working on Memory System Phase 5–12 implementation. This file is the **shared coordination ground truth**. Each orchestrator MUST read it on `main` before any sprint kickoff and after every 30-min heartbeat.

**Update protocol.** Edit only inside your owned worktree → commit → push → PR → merge. Never edit on `main` directly. Conflicts at merge time → rebase, re-verify your claim against the current state, re-push.

---

## §1. Active orchestrators

| ID | Phase chain | Branch namespace | Worktree | Owned alembic range | Started |
|----|-------------|------------------|----------|----------------------|---------|
| **A — Synthesis chain** | Phase 5 → 6 → 7 → 8 (sequential) | `feat/p5-*`, `feat/p6-*`, `feat/p7-*`, `feat/p8-*`, `fix/p{5,6,7,8}-*`, `hotfix/p{5,6,7,8}-*`, `plan/p{5,6,7,8}-*` | `.worktrees/orch-A` (create on first use) | 022–049 | TBD |
| **B — Lateral expansion** | Phase 9 (wiki) + Phase 10 (graph) + Phase 12 (butler docs only) | `feat/p9-*`, `feat/p10-*`, `feat/p12-*`, `fix/p{9,10,12}-*`, `plan/p{9,10,12}-*` | `.worktrees/orch-B` | 050–069 (only if Phase 9/10 ratified by AUTHORIZED_SCOPE.md and after Orchestrator A unblocks dependency) | TBD |
| **C — Evaluation harness** | Phase 11 (Shkoderbench / evals) | `feat/p11-*`, `fix/p11-*`, `plan/p11-*` | `.worktrees/orch-C` | none (no schema changes; read-only on DB) | 2026-05-02 |

---

## §2. Owned files (collision boundaries)

### Orchestrator A — exclusive write
- `bot/services/llm_gateway.py`, `bot/services/llm_*.py`, `bot/services/extraction*.py`, `bot/services/cards*.py`, `bot/services/digest*.py`, `bot/services/observations*.py`, `bot/services/reflection*.py`
- `bot/db/repos/llm_*.py`, `bot/db/repos/card*.py`, `bot/db/repos/digest*.py`, `bot/db/repos/observation*.py`, `bot/db/repos/extraction*.py`, `bot/db/repos/memory_event*.py`, `bot/db/repos/memory_candidate*.py`
- `bot/handlers/cards*.py`, `bot/handlers/digest*.py`
- alembic versions `022_*.py` through `049_*.py`
- `tests/services/test_llm_*`, `tests/services/test_extraction*`, `tests/services/test_cards*`, `tests/services/test_digest*`, `tests/services/test_observations*`
- `docs/memory-system/PHASE5_PLAN.md`, `PHASE6_PLAN.md`, `PHASE7_PLAN.md`, `PHASE8_PLAN.md` (ratified — drop the `_DRAFT` suffix when promoted)

### Orchestrator B — exclusive write
- `bot/services/wiki*.py`, `bot/services/graph*.py`, `bot/web/wiki/*`, `web/templates/wiki/*`
- `bot/db/repos/wiki*.py`, `bot/db/repos/graph*.py`
- alembic versions `050_*.py` through `069_*.py` (only after Phase 9/10 authorization in AUTHORIZED_SCOPE.md AND after Orchestrator A confirms cards/relations stable)
- `tests/services/test_wiki*`, `tests/services/test_graph*`
- `docs/memory-system/PHASE9_PLAN.md`, `PHASE10_PLAN.md`, `PHASE12_PLAN.md`

### Orchestrator C — exclusive write
- `tests/evals/*` (new top-level test category)
- `bot/services/eval_*.py` (offline harness, no production wiring)
- `tests/fixtures/golden_recall/*`, `tests/fixtures/eval_seeds/*`
- `docs/memory-system/PHASE11_PLAN.md`, `docs/memory-system/eval-*.md`
- No alembic migrations

### Shared (must serialize via PR — pull-immediately-before-edit)
- `bot/db/models.py`
- `bot/services/forget_cascade.py` (specifically the `CASCADE_LAYER_ORDER` constant + `_LAYER_FUNCS` dict — every new content table requires a cascade layer; failure to add = privacy invariant 9 violation)
- `bot/services/governance.py` (rare; only if introducing new policy types)
- `docs/memory-system/IMPLEMENTATION_STATUS.md`
- `docs/memory-system/ROADMAP.md`
- `docs/memory-system/AUTHORIZED_SCOPE.md`
- `docs/memory-system/HANDOFF.md` (only structural updates; per-phase notes go to phase-specific PLAN.md)
- `CLAUDE.md` (root)
- `bot/__main__.py` (`_ALLOWED_UPDATES` for new Telegram update types — see ROADMAP allowed_updates rollout rule)
- `pyproject.toml` (new deps must be reviewed by all 3 orchestrators via comment on PR)
- `.github/workflows/*.yml`

---

## §3. Coordination protocol

### §3.1 Sprint kickoff (paranoid pre-flight)
Before opening **any** new sprint:

1. `git -C <project_root> fetch --all --prune` (NOT `--depth` shallow; we need full history)
2. Read this file on `main` (`git show main:docs/memory-system/ORCHESTRATOR_REGISTRY.md`).
3. Verify your worktree base: `git -C <worktree> log --oneline -1` should be ≤ 5 commits behind `origin/main`. If older — rebase your worktree on `main`.
4. Scan §4 active sprints: if any active sprint touches a file you intend to write, STOP. Comment on the active orchestrator's tracking GitHub issue requesting handoff or scope-split. Do not race.
5. Run `gh pr list --state open --search "head:feat/p<your_phase>-"` — if any open PR is yours from a previous session, finish or close it first.
6. Append your sprint to §4 (PR — see §3.4).

### §3.2 During sprint (heartbeat, every ≤ 30 min)
1. `git fetch --all --prune`. If `origin/main` advanced, run `git diff main..your_branch -- docs/memory-system/IMPLEMENTATION_STATUS.md docs/memory-system/AUTHORIZED_SCOPE.md CLAUDE.md bot/db/models.py bot/services/forget_cascade.py` — if those files were touched on main since you forked, **rebase immediately** before pushing your next commit.
2. Read `gh issue list --label phase:5 --label phase:9 --label phase:10 --label phase:11 --label phase:12 --state open --limit 30` (use whichever apply to you) for fresh tickets created by humans or other orchestrators.
3. If you spot another orchestrator's PR touching the SAME file you have open in a local commit — comment on both PRs immediately, do not push, escalate to human.

### §3.3 Shared-file edit discipline
For files in §2 "Shared":
1. `git checkout main && git pull --rebase origin main` (or worktree equivalent).
2. Make edit → commit → push → PR.
3. Wait for CI green, then merge. Do not bundle shared-file edits with feature commits unless the feature directly requires the change in the same atomic transaction (e.g., a new model + alembic migration).
4. After merge, other orchestrators MUST re-pull main before their next commit.

### §3.4 REGISTRY edit (this file)
- Open a PR titled `chore(orch-<ID>): registry update — <what>`
- One section edit per PR (don't batch §1 + §4 + §6 unless intra-related).
- Other orchestrators are encouraged but not required to comment.

### §3.5 PR & merge rules (per Superflow charter)
- `governance_mode = critical` for all 3 orchestrators (privacy invariants binding; we are inside the memory system).
- `git_workflow_mode = parallel_wave_prs` (per-sprint PR with auto-merge on CI green; Final Holistic Review obligatory at end of every multi-sprint phase).
- Per-PR PAR review: 1 product reviewer (Claude `claude -p` since the orchestrator is in Codex) + 1 technical reviewer (Codex own session via task delegation OR a separate subagent invocation). NEVER skip review citing time pressure.
- Merge command: `gh pr merge <num> --rebase --delete-branch`. **NEVER `--admin`**. CI red ⇒ fix CI, don't bypass.
- Final Holistic Review at end of every Phase: 2 reviewers do a holistic pass over the entire phase's PR-set (not per PR). Required for any phase ≥ 4 PRs.

### §3.6 Codex dual-agent invocation (since orchestrators run inside Codex)
When the orchestrator IS Codex itself, the secondary independent reviewer is invoked as:
```bash
$TIMEOUT_CMD 600 claude -p "<reviewer prompt with diff context>" 2>&1
```
- Use `claude -p` for product/spec reviews (Claude's strength).
- Use Codex own subagent / `spawn_agent` for technical reviews if the orchestrator can self-fork.
- Never use raw recursive `codex exec` from inside Codex (causes shell-wrapper recursion per memory note).

See `docs/memory-system/prompts/CODEX_DUAL_AGENT_PATTERN.md` for the canonical prompt skeleton.

---

## §4. Active sprints

Update this section at sprint start (in your sprint-kickoff PR) and at sprint close (in your closing PR).

| Orch | Sprint label | Tickets | Started (UTC) | Status | PRs | Notes |
|------|--------------|---------|---------------|--------|-----|-------|
| C | Phase 11 — Sprint 0 plan ratification | T11 plan + draft reconciliation | 2026-05-02 | merged | PR #173 (3 commits rebase-merged 12:53 UTC: 8e1e716 + d48de41 + 970842f) | Promotes `PHASE11_PLAN.md` (canonical evals scope); marks `prompts/PHASE11_PLAN_DRAFT.md` (expertise pages) as deferred. No code, no schema. |
| C | Phase 11 — Wave 1 + Wave 2 (round 1) implementation | T11-W1-01..07 (eval harness skeleton) + T11-W2-01..03 (leakage / citations / refusal) | 2026-05-02 → 2026-05-11 | merged (10 PRs) | #192 W1-06 evals.yml + #193 W1-01 runner + #194 W1-03 metrics + #195 W1-07 privacy gate + #196 W1-04 seed_v1 + #202 W1-02 seeds module + #205 W1-05 (determinism + recall_precision + no_llm_imports + conftest loop_scope fix) + #208 W2-02 citations + #211 W2-01 leakage + #216 W2-03 refusal | All PRs `--rebase --delete-branch`; never `--admin`. Class-scoped asyncio loop alignment was the dominant fix-cycle theme — final fix in #205 (conftest fixtures `loop_scope="class"`) cascaded into per-test `pytestmark = pytest.mark.asyncio(loop_scope="class")` for W2-01/02/03. |
| C | Phase 11 — T11-W2-04 baseline freeze (Wave 2 closer) | baseline_thresholds + flip `EVAL_HARNESS_ENABLED` + REGISTRY/CLAUDE.md/ROADMAP updates + activate §8.1 binding | 2026-05-11 | (this PR) | TBD | Records frozen thresholds from CI run on commit `bc98bbd`: recall@K=0.10 floor, precision@1=0.10, precision@3=0.03, precision@5=0.02; abstain rate unbounded (seed_v1 produces 7/8 abstain — known seed-quality issue tracked in follow-up). Flips `EVAL_HARNESS_ENABLED` to `true`. Privacy invariant gates (leakage / citations / refusal / no-LLM-imports) are the real Phase 5 closure gate — recall numbers are advisory until seed quality improves. |
| B | Phase 12 — Sprint 0a ratification (docs only) | n/a — promotes `prompts/PHASE12_PLAN_DRAFT.md` → `docs/memory-system/PHASE12_PLAN.md` | 2026-05-02 | merged (a45702f on main) | PR #171 | Branch `plan/p12-ratify`, worktree `.worktrees/orch-B`. Both PAR reviewers PASSED (Claude product ACCEPTED + fallback Claude technical APPROVE; Codex stuck after reconnects, cancelled per Rule 7). NO source code, NO migrations, NO `models.py` / `forget_cascade.py` / `governance.py` edits. |
| A | Phase 5 — Sprint 0: plan ratification | n/a (docs-only) | 2026-05-02 | merged (`fe2146d` on main) | PR #172 (`plan/p5-ratify` → `main`) | Promotes `prompts/PHASE5_PLAN_DRAFT.md` → `PHASE5_PLAN.md`. Salvages stale `.worktrees/p4-hotfix-164/.codex-design.md` into `prompts/PHASE5_WAVE0_HOTFIX164_DESIGN.md` (Wave 0 source-of-truth). Adds Phase 5 section header to IMPLEMENTATION_STATUS.md. Wave 0 (#164 hotfix) absorbed as prerequisite to Wave 1. PAR (Codex 4 rounds; closed 1 CRITICAL + 4 HIGH + 2 MEDIUM + 1 LOW) per new rule "Codex always for tests/reviews" (memory `feedback-roles-claude-impl-codex-review.md`). |
| A | Phase 5 — Wave 0: hotfix #164 (live v1 + import current_version_id + normalized_text + qa_traces cascade) | T5-W0-01 | 2026-05-03 → 2026-05-08 | merged (PR #203) | PR #203 (`feat/p5-w0-hotfix-164` → `main`); follow-up docs PR #204 closes issue #164 | Single PR, 15 commits per design §6. Alembic 023 backfills legacy `current_version_id IS NULL` cohort idempotently. `qa_traces` cascade layer added to `forget_cascade.CASCADE_LAYER_ORDER`. Phase 4 hotfix gate cleared — unblocks all Wave 1+ work. |
| A | Phase 5 — Wave 1 (T5-01 + T5-02, parallel) | T5-01 gateway, T5-02 schema | 2026-05-08 → 2026-05-11 | merged (2 PRs) | PR #209 (`feat/p5-w1-gateway` → `main`, commit `7dcb218`) + PR #207 (`feat/p5-w1-schema` → `main`, commit `5fcd99b`) | T5-01: `bot/services/llm_gateway.py` (885 LOC) + `bot/services/llm_providers/{__init__,anthropic,openai}.py` + `bot/services/observability.py`; 7 pre-call invariants + F4 cache-race recovery via IntegrityError + budget-lock-released-before-HTTP placeholder pattern (per contracts.md §12.2 REVISED). 59 gateway+provider tests. T5-02: alembic 024 + `LlmUsageLedger` + `LlmSynthesisCache` ORM (`bot/db/models.py` lines 761 / 816) + 19 schema tests. PAR PASS both PRs. |
| A | Phase 5 — Wave 2 (T5-03 repos) | T5-03 | 2026-05-11 | merged (PR #223, commit `18c98893`) | PR #223 (`feat/p5-w2-t5-03` → `main`) | T5-03 scope: `bot/db/repos/llm_usage_ledger.py::LedgerRepo` (4 methods incl. `update_placeholder` per §12.2 REVISED) + `bot/db/repos/llm_synthesis_cache.py::SynthesisCacheRepo` (4 methods incl. `invalidate_by_citation` JSONB `@>`); 17 tests (≥15 gate). Flush-only, never commit. Protocol parity with `bot/services/llm_gateway.py::LedgerRepoProtocol` + `SynthesisCacheRepoProtocol`. PAR: Claude `deep-code-reviewer` ACCEPTED + Codex `gpt-5.5 high` round-1 REQUEST_CHANGES (F-H1 rollback proof + F-M1 SQLite hydration) → fix `e5bc5ea` → round-2 APPROVE. Carryover: contracts.md §5.1+§10.1+§12.2 docs follow-up on `update_placeholder` return type. |
| A | Phase 5 — Wave 2 (T5-04 handler + alembic 025 + cascade + pricing) | T5-04 | 2026-05-11 | merged (PR #226, commit `43f21ee`) | PR #226 (`feat/p5-w2-t5-04` → `main`) | **Privacy-critical** (invariants #2 + #3 + #9). 7 commits, 17 files, ~+2400/-50 LOC. Scope shipped: alembic 025 (qa_traces +4 LLM cols + `prompt_hash` DROP NOT NULL per §12.1) + `QaTrace` ORM ext + `QaTraceRepo.update_llm_fields` (§12.3) + `bot/services/llm_pricing.py` (§12.6: Haiku 4.5 $1/$5 + gpt-4o-mini $0.15/$0.60) + `bot/services/llm_gateway.py` `_estimate_cost` wired to MODEL_PRICING + `prompt_template_version` v0.1.0 → v1.0.0 (§12.5) + `bot/handlers/qa.py` 4-step ORDER (§6.1) + flag `memory.qa.llm_synthesis.enabled` (default FALSE) + flag-OFF byte-for-byte preservation + `bot/services/forget_cascade.py` 3 new layers in ORDER (synthesis_cache FIRST → qa_traces_llm → llm_usage_ledger) + 27 new tests + 4 byte-identity tests + 1 integration test. PAR: Claude `deep-code-reviewer` ACCEPTED (4 stop signals clear) + Codex 2 rounds REQUEST_CHANGES (HIGH `update_llm_fields -> int` + MEDIUM step-order proof + Claude F-M1 OpenAI default) → fixes `c5b5c38`/`33248e2`/`d6b2c51` (lint-privacy allowlist) → CI green. Stall recovery executed: previous deep-implementer dispatch stalled 100min; orchestrator salvaged partial work into 3 atomic commits + dispatched narrower handler implementer. |
| A | Phase 5 — Wave 2 closure (docs only) | n/a | 2026-05-11 | merged (PR #227, commit `358e144`) | PR #227 (`chore/orch-a-w2-closure` → `main`) | Closure doc updates: REGISTRY §4 (T5-03/T5-04 marked merged), IMPLEMENTATION_STATUS.md T5-03/T5-04 done rows with SHAs, brief CLAUDE.md note Wave 2 CLOSED. NO source code. NO migrations. |
| A | Phase 5 — Wave 3 (T5-05 eval harness) | T5-05 | 2026-05-11 | merged (PR #229, commit `5faea1d`) | PR #229 (`feat/p5-w3-evals` → `main`) | 8 fixture cases per contracts.md §9 BINDING schema + mocked unit evals (9 in-CI + 1 opt-in real-gateway smoke). Phase 11 (Orch C) consumes VERBATIM. Stabilized PKs 7005/7008 for eval-005/eval-008. PAR: Claude `deep-code-reviewer` ACCEPTED + Codex round-2 APPROVE (after fix `3a11f1f` for fixture-verbatim consumption + prompt_template_version v1.0.0 alignment). |
| A | Phase 5 — Final Holistic Review (FHR) | n/a (review-only) | 2026-05-11 | ACCEPTED (Claude `deep-product-reviewer` Opus) | n/a | Per superflow Rule 9 (≥4 sprints + parallel_wave_prs + governance_mode=critical). CRITICAL=0, HIGH=0, 4 MEDIUM (qa_trace_id type tightening, contracts.md field-name drift, fixture runtime-seeded annotation, cascade message_hash sub-case test), 4 LOW (cosmetic). M-1 + M-4 → Phase 6 kickoff carryover. M-2 + M-3 → closure PR (next row). Codex FHR dispatched in parallel but returned with internal-bg-task without verdict — orchestrator relies on per-PR Codex APPROVE on each of T5-01..T5-05 + Claude FHR system-level pass for closure justification. |
| **A** | Phase 5 — CLOSED + Phase 6 authorization (docs only) | n/a | 2026-05-11 | (this PR) | TBD | Phase 5 closure: STATUS T5-05 → merged + Phase 5 CLOSED block; CLAUDE.md status flipped to CLOSED + Phase 6 next; ROADMAP Phase 5 = DONE; AUTHORIZED_SCOPE.md Phase 5 marked CLOSED + Phase 6 (cards) authorized. FHR M-2 docs fix (contracts.md `daily_usd_ceiling` → `daily_ceiling_usd` × 4 occurrences). FHR M-3 fixture comment for runtime-seeded convention. M-1 + M-4 carryover to Phase 6 kickoff. NO source code. NO migrations. |
| B | Phase 9 + Phase 10 — Sprint 0b draft refinement (docs only, design-only) | n/a — annotates `prompts/PHASE9_PLAN_DRAFT.md` + `prompts/PHASE10_PLAN_DRAFT.md` with §0a "Refinement Status" blocks | 2026-05-02 | OPEN — PR pending Codex PAR review | PR #191 (`plan/p9-and-p10-refine` → `main`) | Worktree `.worktrees/orch-B` (re-created post sprint-0a cleanup). Touches `prompts/PHASE9_PLAN_DRAFT.md` (§0a + migration window fix 040+→050+ + Phase 6 dep contract reconciled against actual `prompts/PHASE6_PLAN_DRAFT.md` schema), `prompts/PHASE10_PLAN_DRAFT.md` (§0a + provisional resolution of 6 "Open for ratification" decisions + Phase 6/8 dep contract reconciled against actual upstream schemas + GAP A/B/C explicit gap-callouts for missing `card_relations` table / non-triple observations / no `visibility_scope` column), `docs/memory-system/IMPLEMENTATION_STATUS.md` (rows 208–209 refinement annotations), this row. **Mid-sprint fix:** initial sprint-0b commit had vapor cross-refs to fictional Phase 6/8 fields; a fix-mid-sprint commit reconciled them against actual `prompts/PHASE6_PLAN_DRAFT.md` lines 178-197 + `prompts/PHASE8_PLAN_DRAFT.md` lines 198-218 + `bot/db/models.py` lines 460-472. Branch carries 2 commits post-rebase on `origin/main` (heads up to `9ebd513` at the time of writing); pre-rebase hashes (`d405c1f` / `270379a`) are stale — see PR #191 commit log for current SHAs. NO promotion to canonical paths (Phase 6 closure gate not satisfied; PHASE9/10_PLAN.md remain owned-but-unwritten). NO source code, NO migrations, NO `models.py` / `forget_cascade.py` / `governance.py` edits. Per new rule `feedback-roles-claude-impl-codex-review`: review = Codex (single PAR covering product + technical lens). REGISTRY §3.1 step 4 collision scan at kickoff + heartbeat: clear; rebased on origin/main 2026-05-02 to absorb Orch A's 4 Phase 5 commits (`2cb2453`, `b9b0cf8`, `92c7a75`, `fe2146d`). |

---

## §5. Cross-orchestrator known dependencies

| Producer | Output | Consumer | Phase gate |
|----------|--------|----------|------------|
| Orch A (Phase 5) | `llm_gateway` + ledger + governance source-filter | Orch C (Phase 11) for LLM-eval cases; Orch B (Phase 10) for entity extraction | Phase 5 closure |
| Orch A (Phase 5) | `EvidenceBundle` cited synthesis API | Orch B (Phase 9) wiki page rendering; Orch C (Phase 11) citation-quality eval | Phase 5 closure |
| Orch A (Phase 6) | `knowledge_cards` + `card_sources` stable | Orch B (Phase 9) wiki content; Orch B (Phase 10) graph entity nodes | Phase 6 closure |
| Orch A (Phase 8) | `observations` table | Orch B (Phase 10) graph projection of observations | Phase 8 closure |
| Orch A (any phase) | New content table | Orch A self: must add to `forget_cascade.CASCADE_LAYER_ORDER` in same sprint | Privacy invariant 9 |
| Orch C | Phase 4 baseline evals (Wave 2 — leakage / citations / refusal + Wave 1 §5.6 no-LLM-imports) | Orch A (Phase 5) regression baseline before LLM enables | **BINDING ACTIVE since 2026-05-11** (Wave 2 baseline frozen at T11-W2-04). Run command (explicit file paths — never `-k`): `EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x --timeout=60 tests/evals/test_leakage.py tests/evals/test_citations.py tests/evals/test_refusal.py tests/evals/test_no_llm_imports.py`. Verdict source: `eval_results.jsonl`. See `PHASE11_PLAN.md §8.1`. |
| Orch C (Wave 3) | LLM-synthesis hallucination + citation-drift evals | Orch A (Phase 5) post-merge regression suite | After Phase 5 close; Orch A may not declare Phase 5 stable until Wave 3 green. |

---

## §6. Collision history & lessons

Record collisions and their resolution. Each entry: orchestrators involved, file/scope, what triggered it, how resolved, what the protocol learned. Future orchestrators must read this section before sprint kickoff.

| Date (UTC) | Orchestrators | Conflict | Resolution | Protocol delta |
|------------|---------------|----------|------------|----------------|
| (none yet) | | | | |

---

## §7. Stop / escalate signals

If any of the below — pause your work, comment on a tracking issue, ping the human:

1. Two orchestrators' PRs touch the same file in `§2 Shared` and the second cannot rebase cleanly.
2. An invariant from `HANDOFF.md §1` (privacy / governance / no-LLM-outside-gateway / tombstone-durability) appears at risk in your scope.
3. AUTHORIZED_SCOPE.md does not yet authorize the work you are about to do.
4. Your subagent reports success but you cannot independently verify the claim (see §8 paranoid mode).
5. CI on `main` is red because of someone else's merge — do not push your work until red is resolved.
6. The Phase you are about to start has no `_DRAFT.md` ratified into `_PLAN.md` yet.

---

## §8. Paranoid mode (binding for all 3 orchestrators)

Per memory note `feedback-paranoid-orchestrator-mode` (2026-04-30) and `feedback-codex-hallucinated-citations` and `feedback-codex-bg-task-monitoring`:

1. **Distrust executor reports.** A subagent saying "tests pass, ruff clean, mypy clean" is hearsay until you re-run those commands yourself in the worktree.
2. **Verify file references.** Codex (and sometimes Claude) hallucinates `file:line` citations and method/symbol names. Before acting on any review finding, `grep` and `Read` the actual file. If the citation does not match, downgrade the finding to "needs investigation" and re-prompt.
3. **Background agents may return early.** A `codex:codex-rescue` or other background tool may return "launched bg task X" without delivering. Always check the worktree, branch, and PR after ~30 min. If no commit / no PR — assume stuck, take over yourself.
4. **Independent verifier per merge.** Each PR's CI green is necessary but not sufficient. Run an independent reviewer (Claude `-p`) with the diff before merge. The reviewer must confirm: scope match, invariants intact, tests cover the bug class, no hallucinated citations remain.
5. **Never claim phase closed without proof.** "Phase X closed" requires (a) all tickets CLOSED on GitHub, (b) all PRs MERGED, (c) IMPLEMENTATION_STATUS.md reflects every ticket, (d) FHR reviewers gave ACCEPTED + APPROVE, (e) post-merge `pytest -x` on a fresh main checkout passes. Anything less = "in progress".
6. **Worktree collision check before any branch creation.** `git worktree list` before `git worktree add`. If your target path exists with a different branch — STOP, escalate.
7. **Branch namespace check before creating branch.** Verify your branch prefix matches §1; if not, fix the prefix or escalate.
8. **REGISTRY drift detection.** Every heartbeat: `git diff main:docs/memory-system/ORCHESTRATOR_REGISTRY.md $(git show -s --format=%H main^):docs/memory-system/ORCHESTRATOR_REGISTRY.md`. If the file changed since your last read — re-read it fully.

---

## §9. Glossary (cross-orchestrator vocabulary)

- **PAR review** = post-implementation, pre-merge dual review (1 product + 1 technical).
- **FHR** = Final Holistic Review, performed at end of every multi-sprint phase across all PRs in that phase.
- **Wave** = group of streams that can run in parallel within a single phase (e.g., Phase 4 had Wave 1 = Streams A, C, E).
- **Stream** = single thread of execution inside a phase, owned by one subagent / one branch.
- **Sprint** = one PR-shipped chunk of work; usually one sprint = one stream's deliverable.
- **Hub** = the orchestrator's main coordinating context; does not write code, only reads / dispatches / verifies / merges.

---

**Last updated:** 2026-05-11 (Orchestrator A: **Phase 5 CLOSED** — all 6 tickets merged across 4 waves; FHR Claude ACCEPTED with 4 MEDIUM carryovers; Phase 6 (cards) authorized below. T5-05 PR #229 `5faea1d`; closure PR pending CI green.).
