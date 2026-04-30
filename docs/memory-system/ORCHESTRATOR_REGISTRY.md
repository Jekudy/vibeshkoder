# Orchestrator Registry — Memory System Phase 5+

**Purpose.** Coordinate 3 parallel paranoid orchestrators working on Memory System Phase 5–12 implementation. This file is the **shared coordination ground truth**. Each orchestrator MUST read it on `main` before any sprint kickoff and after every 30-min heartbeat.

**Update protocol.** Edit only inside your owned worktree → commit → push → PR → merge. Never edit on `main` directly. Conflicts at merge time → rebase, re-verify your claim against the current state, re-push.

---

## §1. Active orchestrators

| ID | Phase chain | Branch namespace | Worktree | Owned alembic range | Started |
|----|-------------|------------------|----------|----------------------|---------|
| **A — Synthesis chain** | Phase 5 → 6 → 7 → 8 (sequential) | `feat/p5-*`, `feat/p6-*`, `feat/p7-*`, `feat/p8-*`, `fix/p{5,6,7,8}-*`, `hotfix/p{5,6,7,8}-*`, `plan/p{5,6,7,8}-*` | `.worktrees/orch-A` (create on first use) | 022–049 | TBD |
| **B — Lateral expansion** | Phase 9 (wiki) + Phase 10 (graph) + Phase 12 (butler docs only) | `feat/p9-*`, `feat/p10-*`, `feat/p12-*`, `fix/p{9,10,12}-*`, `plan/p{9,10,12}-*` | `.worktrees/orch-B` | 050–069 (only if Phase 9/10 ratified by AUTHORIZED_SCOPE.md and after Orchestrator A unblocks dependency) | TBD |
| **C — Evaluation harness** | Phase 11 (Shkoderbench / evals) | `feat/p11-*`, `fix/p11-*`, `plan/p11-*` | `.worktrees/orch-C` | none (no schema changes; read-only on DB) | TBD |

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
| (none yet) | | | | | | |

---

## §5. Cross-orchestrator known dependencies

| Producer | Output | Consumer | Phase gate |
|----------|--------|----------|------------|
| Orch A (Phase 5) | `llm_gateway` + ledger + governance source-filter | Orch C (Phase 11) for LLM-eval cases; Orch B (Phase 10) for entity extraction | Phase 5 closure |
| Orch A (Phase 5) | `EvidenceBundle` cited synthesis API | Orch B (Phase 9) wiki page rendering; Orch C (Phase 11) citation-quality eval | Phase 5 closure |
| Orch A (Phase 6) | `knowledge_cards` + `card_sources` stable | Orch B (Phase 9) wiki content; Orch B (Phase 10) graph entity nodes | Phase 6 closure |
| Orch A (Phase 8) | `observations` table | Orch B (Phase 10) graph projection of observations | Phase 8 closure |
| Orch A (any phase) | New content table | Orch A self: must add to `forget_cascade.CASCADE_LAYER_ORDER` in same sprint | Privacy invariant 9 |
| Orch C | Phase 4 baseline evals | Orch A (Phase 5) regression baseline before LLM enables | Phase 11 sprint 1 closure |

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

**Last updated:** 2026-04-30 (created at orchestrator-registry kickoff).
