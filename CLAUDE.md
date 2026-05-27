<!-- Root: ~/Vibe/CLAUDE.md — ALWAYS read it first for vault-wide rules and structure -->

# CLAUDE.md

## What

Vibe Gatekeeper is a Telegram + web gatekeeping system for managing community applications, vouching, intro refresh, and admin/member visibility.

## Runtime Standard

- Source of truth is GitHub, not the VPS.
- Production deploys from pre-built GHCR images.
- Coolify is the target runtime manager for product apps.
- Host-level operator services stay outside Coolify if they need direct VPS control.
- Bot process exposes `/healthz` on `:3000` via aiohttp for Coolify health_check (added 2026-05-14, issue #168). Env var `HEALTHZ_PORT` overrides the port (default 3000). Post-merge operator step: enable Coolify health_check via `PATCH /api/v1/applications/<id>` with `{"health_check_enabled": true, "health_check_path": "/healthz", "health_check_port": 3000}` — safe to flip only AFTER this release is deployed.
- `/healthz/db` is a DB-only sub-endpoint (faster, separates DB hiccups from app crashes; consumed by `ops/healing/healthcheck.py` after #270).

## Environments

- Local development uses `DEV_MODE=true`.
- Staging and production must use separate bot tokens and isolated data stores.
- Secrets never belong in git.

## Issue Tracker

- This repo uses Notion via `nt` plugin (`/nt:issue`, `/nt:work`, `/nt:status`, ...)
- Other projects use Linear via `ln` plugin
- Do not mix: `nt` commands in non-shkoderbot repos will fail by design
- To override in one-off scenarios: `export NT_TEAM=SHK`

## Current Migration Rule

- Coolify is the production runtime for bot and web deploys.
- Legacy `/home/claw/vibe-gatekeeper` is retained only as rollback fallback until
  `scripts/cleanup-legacy.sh` passes its A3, soak window, and disk preflights.

## Memory System Cycle (active 2026-04-26+)

**Phase 11 (Shkoderbench / evaluation harness) CLOSED 2026-05-11** — Wave 1
(eval harness skeleton: runner, seeds module, metrics, fixture, gated CI
workflow, privacy allowlist gate, determinism + smoke + AST no-LLM-imports
tests) + Wave 2 round 1 (leakage L1-L5, citations C1-C4, refusal R1-R4)
merged as 10 PRs. Baseline thresholds frozen at T11-W2-04 (commit `bc98bbd`)
in `tests/fixtures/golden_recall/seed_v1/seed_meta.yaml`. `EVAL_HARNESS_ENABLED`
secret flipped ON; nightly `evals.yml` workflow runs the privacy binding suite
(`tests/evals/test_leakage.py` / `test_citations.py` / `test_refusal.py` /
`test_no_llm_imports.py`). Binding **ACTIVE** for Orch A's Phase 5 closure —
see `ORCHESTRATOR_REGISTRY.md §5` and `PHASE11_PLAN.md §8.1`.

Known follow-up: seed_v1 produces 7/8 abstain rate on Phase 4 FTS — separate
seed-quality issue. Privacy invariant gates work regardless.

Phase 1 closed 2026-04-27. Phase 2 (importer + governance skeleton) **CLOSED 2026-04-29** —
20/20 issues merged across 4 parallel stream worktrees (Alpha/Bravo/Charlie/Delta) +
Final Holistic Review hotfix (PR #143). Phase 3 governance skeleton (T3-01..T3-05) merged
as part of Phase 2 wave.

**Phase 4 (hybrid search + Q&A with citations) CLOSED 2026-04-30** — 6/6 implementation
tickets merged: T4-01/T4-02 via PRs #151 + #156 (FTS schema + search service + hardening),
T4-03 via #157 (evidence bundle), T4-04 via #162 (/recall handler), T4-05 via #158
(qa_traces audit), T4-06 via #162 (12 eval cases). T4-02H (#153) closed as duplicate of
#156. Forward-looking design drafts for Phase 5/6/7/8/9/10/11/12 ratified as docs-only
artifacts in `docs/memory-system/prompts/` (PRs #159 + #160). FHR in flight.

**Phase 5 (LLM gateway + answer synthesis) — Wave 0 + Wave 1 + Wave 2 CLOSED 2026-05-11.**
Plan ratified per `docs/memory-system/PHASE5_PLAN.md`. Wave 0 = T5-W0-01 hotfix #164
(PR #203). Wave 1 = T5-01 gateway (PR #209, commit `7dcb218`) + T5-02 schema (PR #207,
commit `5fcd99b`). Wave 2 = T5-03 repos (PR #223, commit `18c98893`) + T5-04 handler +
alembic 025 + cascade + pricing (PR #226, commit `43f21ee`). Flag
`memory.qa.llm_synthesis.enabled` default OFF — Phase 4 byte-for-byte preserved.

**Phase 5 (LLM gateway + answer synthesis) — CLOSED 2026-05-11.** All 6 tickets merged
across Wave 0/1/2/3 (T5-W0-01..T5-05). Wave 3 = T5-05 eval harness (PR #229
`5faea1d`). FHR Claude `deep-product-reviewer` ACCEPTED with 4 MEDIUM carryovers in
closure PR. Phase 11 (Orch C) binding tests green on main.

**Phase 6 (knowledge cards + admin review) — CLOSED 2026-05-12.** All 9 tickets merged
across Wave 1 (T6-00..T6-03) + Wave 2 Stream C (T6-04, T6-05, T6-09 + cascade advisory
lock wiring) + Stream D (T6-06, T6-07). T6-08 (web cards page) originally deferred at
closure (no Phase 5 web scaffold); **shipped retroactively 2026-05-13 via PR #281**
(commit `d4b2185`) once a web scaffold (`web/app.py`, `web/auth.py`, `web/routes/*`)
landed. PR was opened by an autonomous Claude session during healing verification run
25813274803 with a synthetic SIGNAL_PAYLOAD — not by a human or scoped Phase 6.5
sprint. Merge by @Jekudy. Follow-up tracked: healing orchestrator needs a
synthetic-signal / no-real-bug abstention guard (see issue filed 2026-05-14). Forget cascade orchestrator advisory lock wiring closes H-Cdx-2
race window via 3 layered defenses (per-mvid lock + event-level coarse lock + FOR SHARE
row lock). Phase 11 binding 28/28 green (L1-L5 + L6a/b/c + C1-C4 + C5a-d + R1-R4 +
I1-I4). FHR APPROVE — Phase 5 LLM gateway tombstone fix landed in same closure cycle
(PR #259). Carryover issues for Phase 6.5: #260 _process_one_event rename, #261
extractor running-row leak, #262 MED/LOW + deferred items.

**Phase 6 Wave 2 closure (2026-05-13):**
- T6-09 broader e2e pipeline test (candidate→card→recall) merged via PR #267.
- Phase 6.5 carryovers: #261 (extractor running-row leak) fixed PR #265; #260
  (_process_one_event rename) fixed PR #266; #262 trivial items (UNION ALL comment,
  column order annotation) fixed PR #266. MED/LOW items M-1/M-2/M-4 from Codex
  audit remain open (tracked in #262).
- T6-06/T6-07 retrospective design docs landed PR #264 (anomaly: code preceded docs).
- Codex post-merge follow-ups on PR #266 (M1+M2 SQL drift, M3 multi-item snapshot,
  L1+L2 docstring) addressed in chore/p6-wave2-closure branch.

**Phase 7 (daily summaries) — CLOSED 2026-05-15.** All 8 tickets merged
(T7-S0 docs ratify #285, T7-01 migration 037 #287, T7-03 digest_context
#290, T7-02 run_digest + synthesize_digest gateway #293, T7-04 scheduler +
reaper #294, T7-05 publisher + renderer + redactor + cascade `digests`
layer #296, T7-06 admin handlers #297, T7-07 Phase 11 binding L7a/b + C6 +
I5a/b/c #298, T7-08 closure docs in this PR). Phase 11 binding **34/34**
green (28 prior + 6 new). Flag `memory.digests.daily.enabled` default
OFF — production rollout playbook in `docs/memory-system/PHASE7_ROLLOUT.md`.
Phase 7.5 carryovers tracked: #291 (shared forget-events predicate refactor
between forget_cascade and digest_context), #295 (T7-02 post-merge MED
items: provider-error categorization, EMPTY_WINDOW ledger error field).
HIGH item from #295 (citation `position` as bullet index, not token
ordinal) shipped as part of T7-05.

**Phase 7 FHR fix sprint (PR #300, commit `df4bb71`+`0fcd54b`):** Final
Holistic Review (Claude `deep-product-reviewer` + Codex deep-technical,
independent) returned NEEDS_FIXES with 4 critical wiring/state-machine
items + 2 high. All shipped: F1 scheduler `digest_daily_job` now calls
`publish_digest` on draft (Charter AC #6 was broken — cron created drafts
but never posted to Telegram); F2 cascade worker threads `Bot` through
`cascade_worker_tick → run_cascade_worker_once → _process_one_event` with
`event._runtime_bot = bot`, scheduler reg uses `args=[bot]` (Telegram-side
forget redaction was dead code in prod — forgotten content stayed visible
in posted digests); F3 `_parse_digest_citations` no longer dedupes by
`(kind, id)` so multi-bullet citations to the same source keep all
`position` values (partial-forget privacy gap — redactor was masking only
first bullet); F4 idempotency-collision path returns
`session.get(Digest, existing)` instead of detached `_row_to_digest(row)`
(publisher's `digest.status='posting'` mutation now actually persists, so
the guarded `posted` UPDATE no longer leaves untracked Telegram posts);
F5 `load_digest_config()` raises `ConfigurationError` on `src == dst`
(echo loop prevention, Plan §8 stop signal); F6 `/digest_now` has
`status='posting'` branch with 1s refresh + polite retry message (Plan
§5.I). 6 new red-green tests cover each fix. CI green, privacy lint green.

**Phase 8 (weekly editorial digest) — CLOSED 2026-05-15.** All 8 tickets
merged (T8-S0 docs ratify PR #302 `e1ee542`; T8-01 migration 038 PR #303
`bdb13c6` + `467cf77`; T8-02 weekly run_digest + synthesize_digest PR #304
`dbefa1a`; T8-03 weekly build_digest_context PR #305 `733ad8f`; T8-04
review state machine + cascade/redactor/publisher widening PR #306
`05cfa88` + `e574ae2`; T8-05 scheduler `digest_weekly_job` Mon 09:15 MSK
+ `digest_stale_review_reaper_job` 48h DM / 7d auto-reject PR #307
`6225789`; T8-06 admin handlers `/digest_now weekly` / `/digest_review` /
`/digest_approve` / `/digest_reject` PR #308 `f6568e2`; T8-07 Phase 11
binding 30 → 42 PR #309 `7a389c1`; T8-08 closure docs in this PR).
Phase 11 binding **42/42** green (30 prior + 12 new: L8a/b + C7 + I6a +
I6b.1/.2/.3 + I6c + R5.a/b/c/d). Flag `memory.digests.weekly.enabled`
default OFF — production rollout playbook in
`docs/memory-system/PHASE8_ROLLOUT.md`. Three independent cost buckets
preserved (Phase 5 shared / Phase 7 daily / Phase 8 weekly) per C5; weekly
ceiling default $5/$20. Migration 038 widens `ck_digests_status` +
`ck_digest_runs_status` with 5 new audit values (`awaiting_review`,
`approved_for_publish`, `rejected_by_admin`, `rejected_by_reaper`,
`regenerated_by_admin`); pre-flight downgrade guard refuses to downshift
while review-state rows exist. Phase 8.5 carryovers: §5.I renderer
section-header bolding + weekly footer (out-of-scope for T8-06),
M6 GIN index dead weight on `_cascade_digests`, #291 shared
`_forget_excludes_predicate` refactor (T8-03 inlined with TODO),
R5.a/R5.b handler-layer tightening of admin-gate refusals (service-layer
contract asserted, handler-layer follow-up). ~3-hour autonomous
orchestration run with multi-round dual-model review on Sprint 0
(3 rounds Claude+Codex); per-sprint unified review attempted but partial
due to reviewer infrastructure stalls — mitigated via inline spot-checks
on highest-risk surfaces (migration 038 guard semantics, review SM
transitions, cascade widening) plus strong implementer evidence in PR
bodies.

**Phase 6 implementation/docs order anomaly (read before any T6-XX work):** T6-06
(`bot/services/search.py` include_cards: commits `fcb5a3c`, `b5949b2`, `84beecd`,
`50d1818`, `6f93105`, `2f1ffab`) and T6-07 (`bot/services/evidence.py` discriminator
+ `bot/handlers/qa.py` renderer: commits `2b89bcd`, `3b34a9f`) **landed on main
BEFORE** their pre-flight design docs (PR #264, 2026-05-13). The design docs are
retrospective. Issues #238 and #239 remain OPEN only because the impl commits
lacked `Closes #` lines — close manually if/when convenient; code is shipped.
Before starting any new T6-XX impl sprint, `git log --follow <core-file>` first to
confirm the work isn't already on main.

**Phase 11 follow-ups** all merged 2026-05-12: #224 (High #5 httpx guard PR #243,
Critical #4 allowlist PR #247, High #1-#4 already on main), #219 seed_v1 quality
(PR #253), #255 Phase 4 message-branch tombstone (PR #257).

**Phase 9 (wiki / community catalog) + Phase 10 (graph projection / Neo4j) —
AUTHORIZED 2026-05-17.** Canonical plans `docs/memory-system/PHASE9_PLAN.md`
(8 sprints T9-01..T9-08, migrations 050-054, member-internal wiki via web
roles split into WEB_ADMIN_PASSWORD + WEB_MEMBER_PASSWORD — closes
self-typed-user_id privilege escalation hole — admin publication gate
via `/wiki_publish` with FOR UPDATE prior_pe capture + advisory
mvid locks, server-side renderer with bleach allowlist + batched
citation revalidation, forget cascade integration inserting
`wiki_pages` + `wiki_revisions` layers between `digests` and
`card_sources`, page lifecycle draft→reviewed→stale→archived,
`[CONTENT_REDACTED: forget_event_id={n}]` mask format for forgotten
revision body_markdown, ~18-21 new Phase 11 binding tests
L9a-e/C8a-b/I7a-f/R6.a-g/G1) + `docs/memory-system/PHASE10_PLAN.md`
(9 sprints T10-01..T10-09, migrations 060-064 inc. ledger call_type,
Neo4j 5.x Community Edition in docker-compose `--profile graph` dev-only
initially, **async graph_purge_worker + pending-purge read-block per
RFC-001:415 conditional approval — replaces earlier synchronous-purge
proposal that violated RFC condition**, replay-only `full_rebuild` from
stored Postgres triples — no LLM re-extraction, ontology split with
`knowledge_cards` → CONCEPT nodes + LLM triples vs `message_versions` →
provenance/event nodes only, 3 separate feature flags
`memory.graph.projection.enabled` / `memory.graph.query.enabled` /
`memory.graph.write_pending.paused` all default OFF, graph-only cost
ceiling `GRAPH_PROJECTION_DAILY_USD_CEILING` $2/day + per-run $0.50
abort, ~15-16 new Phase 11 binding tests L10a-c/C9/I8a-e/R7.a-d/G2).
Both ratified after dual-model spec review (Claude product +
Codex technical) with revision passes addressing 2 BLOCKER + 7-9 HIGH +
4 MEDIUM audit findings per phase. Phase 11 binding suite expected to
grow from 42 → 75+ at closure of both phases.

**Phase 9 (wiki / community catalog) — CLOSED 2026-05-19.** All 8 tickets
merged (T9-01 schema PR #314, T9-02 governance PR #316, T9-03 auth role
split PR #317 — closes BLOCKER C, T9-04 renderer + bleach PR #318, T9-05
member router + Jinja + /robots.txt PR #319, T9-06 admin /wiki_publish /
/wiki_unpublish / /wiki_robots PR #320, T9-07 forget cascade +
advisory lock binding PR #321 — closes T9-06 lock carryover with 4
Codex security fixes inline, T9-08 Phase 11 binding 30 tests / 18 AC
PR #322 — 5 Codex PAR fixes applied inline). FHR Claude
`deep-product-reviewer` NEEDS_FIXES + Codex deep-technical FAIL with 1
CRITICAL + 2 HIGH + 2 MEDIUM + 2 LOW — all CRITICAL + HIGH addressed in
closure PR. CRITICAL: `_cascade_wiki_pages` audit revision INSERT now
masks `body_markdown` with `[CONTENT_REDACTED: forget_event_id={n}]` +
`revision_status='forgotten_redacted'` at insert (previously copied
forgotten content into audit row with empty source snapshot, bypassing
`_cascade_wiki_revisions` overlap filter); HIGH-1: member login flow
end-to-end — `POST /login` + `GET /` redirect role-aware (member →
`/wiki`, admin → `/dashboard`), role-aware nav (Dashboard/Members/Cards
admin-only, Wiki visible to all authenticated), login copy fixed
("Enter your password"); HIGH-2: legacy_cookie_grace audit persists —
migration 055 makes `wiki_publication_log.wiki_page_id` NULLABLE with
CHECK `(wiki_page_id IS NOT NULL OR action='legacy_cookie_grace')`,
`_insert_legacy_grace_audit` writes NULL page_id, I7d binding test no
longer patches the helper to no-op. Phase 11 binding **60/60** green
(42 prior + 18 new). Flag `memory.wiki.enabled` default OFF —
production rollout playbook in `docs/memory-system/PHASE9_ROLLOUT.md`.
Two-password role split: `WEB_ADMIN_PASSWORD` + `WEB_MEMBER_PASSWORD`
(must differ); legacy `WEB_PASSWORD` aliased one release cycle.
Phase 9.5 carryovers: FK action mismatch `created_by_user_id NOT NULL`
+ `ON DELETE SET NULL` (Codex MED #3, only relevant if/when user delete
is used), `_cascade_wiki_revisions` no idempotency guard for already-
redacted rows (Codex LOW #4), stale-page member silent 404 → 410 with
explanation template (Claude MED-4), missing `WEB_MEMBER_PASSWORD`
startup warning (Claude MED-5), `Cache-Control: no-store` on member
`/wiki/{slug}` (Claude MED-6), L9a OR-form assertion polish (Claude
product r1 MED, non-blocking).

**Phase 10 (graph projection / Neo4j) — CLOSED 2026-05-21.** All 10 sprints
merged (W0-A foundation PR #324, W0-D Neo4j CI PR #325, T10-02-rest migrations
061-062 PR #326, T10-03 `extract_graph_triples` + migration 064 PR #327,
T10-04 `graph_projector.py` 4 modes PR #328, T10-06 async purge cascade +
readblock + migration 063 PR #329, T10-05 `graph_query.py` read-only API
PR #330, T10-08 drift detection + `reconcile_counts` PR #331, T10-07 admin
handlers + scheduler PR #332, T10-09 Phase 11 binding suite 60→77 PR #333).
Privacy-critical invariant: forget cascade atomically enqueues
`graph_purge_pending` rows in Postgres; `graph_purge_worker` drives Neo4j
DELETE asynchronously; `graph_query` fails-closed via pending-purge read-block
(RFC-001:415). Ontology split: `knowledge_cards` → CONCEPT nodes + LLM triples
(`call_type='graph_projection'`); `message_versions` → provenance/event nodes
only (no LLM). Migrations 060-066. Three flags all default OFF:
`memory.graph.projection.enabled` / `memory.graph.query.enabled` /
`memory.graph.write_pending.paused`. Cost ceilings: $2/day, $0.50/run, max 200
sources/run. Scheduler: `graph_projection_nightly` (03:30 MSK) +
`graph_purge_worker` (5-min interval). Phase 11 binding **77/77** green.
Rollout playbook: `docs/memory-system/PHASE10_ROLLOUT.md`.

**Phase 12 (Butler / action execution) — IN PROGRESS, authorized 2026-05-23.**
T12-01 (schema + migrations 073, merged), T12-02 (evidence context, PR pending),
T12-03 (tools registry + gateway entry points, PR pending), T12-04 (`ButlerService`
state machine + 6 exceptions + rate buckets + cross-user consent + cascade guard,
migration 074, PR pending) completed across Waves 1–2. **T12-05 (Telegram handlers)
— PR #348 MERGED 2026-05-27.** Lands `bot/handlers/butler.py`: `/butler`,
`/butler_status`, `/butler_cancel`, `/butler_undo` (stub) + 4 inline keyboard
callbacks (confirm/cancel/affected_approve/affected_reject). DM-only baseline
(PrivateChatFilter). Cross-user consent E2E: affected user receives separate DM; on
reject → `revoke_affected_user_consent` → action cancelled; requester preview edited
to "consent revoked" notice. `AffectedUserUnreachableError` new exception class
(additive). Migration 075 widens `ck_butler_action_confirmations_status` CHECK to
include `'revoked'`. Round-2 fix: all early-return paths call `await
session.rollback()` before return (defeats `DbSessionMiddleware` unconditional
commit). Commits `d7045ca`..`4c09fe5`. Alembic head 074 → 075. 86 tests green (+9).
`memory.butler.enabled` master flag default OFF + 5 per-tool flags default OFF.
Phase 11 binding **86 → 86** (delta 0; L12/C10/I9/R8/G3 family lands in T12-09).
FHR required at T12-10 (cycle-end). Rollout: `docs/rollout-fragments/phase12/T12-05.md`.

**Phase 12 (Butler) — T12-07 (`/butler_undo` + undo audit) PR #351 pending merge, 2026-05-27.**
`/butler_undo <action_id>` — DM-only, member auth, dual flag gate (`memory.butler.enabled` +
`memory.butler.undo.enabled`, both default OFF). 5 rollback kinds: `not_reversible`,
`delete_message`, `edit_message`, `followup_correction`, `cancel_pending`. LIFO ordering over
`butler_tool_invocations`. Migration 077: `butler_undo_invocations` audit table (UNIQUE
`(action_id, invocation_id)` — DB-level idempotency) + widens `butler_actions.status` to
include `'undone'`. Migration 078: `butler_tool_invocations.inverse_op_payload` JSONB —
populated by `execute_action` after `tool.build_inverse(result)`. Key invariants:
FOR UPDATE NOWAIT cascade lock; idempotency check BEFORE TTL; coded error messages (no raw
exc text); `dismiss_by_undo` sets `reviewed_by`+`reviewed_at` (CHECK constraint compliance).
`_resolve_prior_text` cascade-aware: relies on `forget_cascade._cascade_message_versions`
nullifying `MessageVersion.text` on redaction; narrow `except (SQLAlchemyError, OperationalError)`.
Forget cascade widened: `butler_undo_invocations` layer between `butler_tool_invocations` and
`butler_actions`; `_cascade_butler_undo_invocations` redacts `error_message`. 3-round PAR:
round-1 NEEDS_FIXES (4C+3H+5M: field-mismatches, missing column, CHECK violation, broad except);
round-2 NEEDS_FIXES (all round-1 closed + 1 CRITICAL REGRESSION — `_resolve_prior_text` SQL used
non-existent ORM columns, masked by mock-only tests — class of bug: mock tests don't catch
column-name mismatches); round-3 APPROVE (JOIN through `chat_messages`, narrow except, 3 new
tests). 115 tests green (+13: 10 e2e + 3 resolve_prior_text). Alembic head 076 → 077 → 078.
Phase 11 binding delta = 0 (L12/C10/I9/R8/G3 lands in T12-09). FHR required at T12-10.
Rollout: `docs/rollout-fragments/phase12/T12-07.md`.

**Phase 12 (Butler) — T12-06 (5 tool implementations) PR #349 pending merge, 2026-05-27.**
Wave 2 sprint F: 5 butler tool implementations under `bot/services/butler_tools/`.
Tool semantic invariants: `recall_evidence` = sealed `ButlerEvidenceContext` only
(no direct DB access, rollback_kind=`not_reversible`); `schedule_meeting` =
Telegram-native text proposal only, no calendar API (Hard Constraint #6,
rollback_kind=`delete_message`); `send_intro` = confirmed text from args, never
re-fetched from DB (privacy), rollback_kind=`delete_message`; `update_intro` =
ownership verified via `invocation_repo.find_by_posted_message_id` + edit-or-followup
fallback, no exception raised on 48h timeout (rollback_kind=`edit_message` or
`followup_correction`); `suggest_card_creation` = writes pending-only
`extraction_candidates` + `butler_card_suggestions` mapping, NEVER approved
(rollback_kind=`cancel_pending`). `TOOL_DISPATCH` registry dict added to
`bot/services/butler_tools/__init__.py` with module-load assertion
`set(TOOL_DISPATCH.keys()) == ALLOWED_BUTLER_TOOLS`.
Round-2 wiring discipline: `ButlerTool` Protocol contract extended with
`invocation_repo` kwarg — full signature `execute(ctx, args, *, session, bot,
action_repo, invocation_repo, action_id)`. Migration **076** adds
`butler_tool_invocations.posted_message_id BIGINTEGER NULLABLE` + partial index;
`ButlerToolInvocationRepo` gains `find_by_posted_message_id` +
`update_invocation(posted_message_id=...)`. `ButlerService.execute_action` extracts
message_id from result.payload for `send_intro`/`schedule_meeting` and persists it,
making `update_intro` ownership lookup non-dead-code. 15 `getattr(args, X, default)`
calls removed across all 5 tools — each raises `ButlerPlanError(invariant_broken)`
on wrong arg type. `action_id` required (no default, defense guard) in
`suggest_card_creation`. `parse_mode=None` pinned on all 3 send/edit paths.
`except` in `update_intro` narrowed to `(SQLAlchemyError, OperationalError)`.
136 tests green (71 unit + 58 state-machine + 5 integration + 2 top-level
registry). Phase 11 binding **86 → 86** (L12/C10/I9/R8/G3 lands in T12-09).
Commits `a411e9e`..`3122431`. Alembic head 075 → 076. No flag flipped.
Operator steps: none. 2-round dual-model review: Claude product ACCEPTED +
Claude tech APPROVE (Codex companion stalled; fell back to second Claude
reviewer per Rule 7).

**Phase 12 (Butler) — T12-08 (TTL expiry worker + per-user budget + monthly cap filter) PR #350 pending merge, 2026-05-27.**
Wave 3 sprint: abuse controls + scheduler reaper. Key invariants: (1) TTL worker
(`butler_expire_tick_job`) only fires when master flag `memory.butler.enabled`=ON —
gated by flag check at tick start, not at job registration. (2) Budget check
(`ButlerBudgetChecker.is_user_daily_exceeded`) wired into `ButlerService.plan_action`
between rate-bucket increments and evidence build: on ceiling hit → rate buckets
rolled back + audit ledger row + raises `ButlerActionError(error_kind='budget_exceeded')`.
(3) Monthly butler LLM cap sums both `call_type='butler_decision'` and
`call_type='butler_summary'` (H1 fix — TODO removed). Round-2 fix cycle
(4 commits `b1ca1cf`..`92326c3`): C1 budget wiring, H1 monthly call_type filter,
H2 `get_pending_past_ttl` LIMIT (default 200), H3 `LedgerRepoProtocol` signature,
M1 `_expire_action_inline` free function (avoids `ButlerService(None,...)` antipattern),
M2 `expire_action` FOR UPDATE NOWAIT, M3 `.env.example` new vars documented,
M4 configurable fake session in tests. No migration (uses existing
`butler_actions.expires_at` from T12-04 + `llm_usage_ledger`). 80 tests green.
Phase 11 binding **86 → 86** (delta 0; L12/C10/I9/R8/G3 family lands in T12-09).
Commits `e8dc08f`..`92326c3`. No flag flipped. Rollout: `docs/rollout-fragments/phase12/T12-08.md`.
2-round dual-model review: Claude product ACCEPTED + Claude tech APPROVE (Codex companion
stalled; fell back to second Claude reviewer per Rule 7). FHR required at T12-10 (cycle-end).

Read these BEFORE touching anything under `bot/db/`, `bot/services/`,
`bot/handlers/chat_messages.py`, or adding `alembic/versions/`:

1. `docs/memory-system/AUTHORIZED_SCOPE.md` — what is allowed in the immediate cycle
   (Phase 0 + Phase 1). What is **not** authorized. Critical safety rule for `#offrecord`.
2. `docs/memory-system/HANDOFF.md` — canonical 12-phase architecture, ticket backlog
   (T0-* through T11-*), governance / ingestion / search / qa specs, future butler boundary.
3. `docs/memory-system/IMPLEMENTATION_STATUS.md` — current status of every ticket. Updated
   after every PR merge.
4. `docs/memory-system/ROADMAP.md` — at-a-glance phase table with gates.
5. `docs/memory-system/DEV_SETUP.md` — isolated dev postgres + dev bot live ingestion testing
   protocol (sandbox-first; real chat requires team-lead approval).
6. `docs/memory-system/telegram-desktop-export-schema.md` — Telegram Desktop JSON export
   reference. Read BEFORE touching any code under `bot/services/import_*` or
   `tests/fixtures/td_export/`. Cross-stream contract: import schema details (envelope,
   message_kind taxonomy, edit/reply semantics, anonymous channel posts, mixed-array text
   form), governance quote, and downstream-ticket cross-refs.
7. `docs/memory-system/import-edit-history.md` — Telegram Desktop import edit-history policy.
   Read BEFORE implementing #103 import apply. Defines: `message_versions.imported_final=TRUE`
   marker, version_seq overlap semantics (live wins; import skips when live row exists),
   governance unchanged (`detect_policy` still runs). Schema/migration land in #103.
8. `docs/memory-system/import-user-mapping.md` — Telegram Desktop import user-mapping policy.
   Read BEFORE touching any code under `bot/services/import_*` that reads `from_id` / writes
   `users` rows. Defines: known-user resolution, ghost-user creation with `is_imported_only=true`
   flag, anonymous channel singleton, privacy R2 (imports cannot promote themselves to live;
   only the gatekeeper live-registration path flips ghost→live by clearing `is_imported_only`),
   display_name first-write-wins, attribution semantics under live/import overlap.
9. `docs/memory-system/import-dry-run-parser.md` — Telegram Desktop dry-run parser. Read BEFORE
   touching `bot/services/import_parser.py`, `bot/services/import_dry_run.py`, or invoking
   `python -m bot.cli import_dry_run [--with-db] <path>`.
   Defines: `ImportDryRunReport` field semantics, single-chat-only input contract (full-account
   exports rejected), NO-content guarantee (`asdict(report)` carries zero message bodies),
   `governance.detect_policy` invocation contract (called per user message, service messages
   skipped), operator pre-flight role before any #103 apply run. Also covers the DB-aware
   mode (`parse_export_with_db(path, session, chat_id) -> ImportDryRunReport` + CLI
   `--with-db` flag) which extends the offline report with `db_duplicate_count` /
   `db_duplicate_export_msg_ids` / `db_broken_reply_count` (#99 / T2-02), backed by a
   synthetic `IngestionRun(run_type='dry_run')` for `import_reply_resolver` scope.
   Cross-refs #91 schema, #93 user mapping, #98 reply resolver, #106 edit-history policy.
   Also covers tombstone collision stats added per #100 (T2-NEW-D): `tombstone_skip_count` /
   `tombstone_skip_export_msg_ids` surface messages blocked by a forget event; tombstone wins
   over duplicate (a message matching both is counted only as tombstone skip); DB-aware mode only.
10. `docs/memory-system/import-reply-resolver.md` — Telegram Desktop import reply resolver. Read
    BEFORE touching `bot/services/import_reply_resolver.py` or before #99 (T2-02 dry-run stats) /
    #103 (T2-03 apply) consume reply mappings. Defines: priority order (same_run → prior_run →
    live → unresolved), chat_id scoping (never resolves across chat boundaries), batch query
    semantics (4 queries max regardless of N — no N+1), `ReplyResolution` / `ReplyResolverStats`
    API contract, read-only invariant (NO DB writes; safe inside any transaction), forward-chain
    direct-lookup design choice (chain_depth always 0; consumers iterate if they need deeper
    traversal). Cross-refs #91 schema, #93 user mapping, #94 dry-run parser.
11. `docs/memory-system/import-checkpoint.md` — Telegram Desktop import apply checkpoint /
    resume infrastructure. Read BEFORE touching `bot/services/import_checkpoint.py`,
    `bot/cli.py::import_apply`, or implementing #103 (Stream Delta apply). Defines:
    resume decision matrix (`start_fresh` / `resume_existing` / `block_partial_present`),
    `ingestion_runs.stats_json.last_processed_export_msg_id` deep-merge contract (atomic
    `UPDATE ... SET stats_json = COALESCE(stats_json, '{}') || CAST(:patch AS jsonb)`), partial
    UNIQUE index on `(source_hash) WHERE status='running'` (race-safe at-most-one running
    import per export), `source_hash` sha256 dedup, CLI exit codes (3=block partial-present,
    4=apply-not-implemented placeholder until #103), `finalize_run` idempotency, lazy
    `run_apply` import dance. Cross-refs #94 dry-run parser, #98 reply resolver, #103 apply
    (deferred). HIGH-RISK boundary: idempotency / no-double-write / no-orphan-rows
    invariants.
12. `docs/memory-system/import-chunking.md` — Telegram Desktop import apply chunking + rate
    limit + advisory lock config. Read BEFORE touching `bot/services/import_chunking.py` or
    implementing #103 (Stream Delta apply). Defines: env vars (`IMPORT_APPLY_CHUNK_SIZE`
    default 500, `IMPORT_APPLY_SLEEP_MS` default 100, `IMPORT_APPLY_ADVISORY_LOCK` default
    true), `ChunkingConfig` frozen dataclass surface (with `__post_init__` range validation),
    `acquire_advisory_lock(connection: AsyncConnection, ingestion_run_id)` context manager
    (deterministic lock_id from SHA-256 → signed int64; auto-release in finally; PostgreSQL
    advisory locks are connection-scoped — caller MUST hold a single AsyncConnection for the
    full lock lifetime; locks are SESSION-stacked, NOT idempotent — re-entry leaves extra
    lock count after exit), CLI `--chunk-size` override semantics. Cross-stream contract:
    #103 `run_apply` must accept `chunking_config: ChunkingConfig` kwarg (replaces old
    `chunk_size=` kwarg from #101 placeholder).
13. `docs/memory-system/import-rollback.md` — Telegram Desktop import logical rollback.
    Read BEFORE touching `bot/services/import_rollback.py`, `bot/cli.py::rollback_ingestion_run`,
    or rollback-related `ingestion_runs` migrations. Defines: the FK-chain selector
    (`chat_messages.raw_update_id → telegram_updates.id → ingestion_run_id`) with
    `telegram_updates.update_id IS NULL` synthetic guard, single-transaction delete +
    audit insert, idempotent `run_type='rolled_back'` audit row keyed by
    `stats_json.original_run_id`, NO-content logging, live-row protection, tombstones not
    rolled back, and the Phase 4+ downstream-dependent TODO.

Issue tracker for memory cycle: **GitHub Issues** (label `phase:0`, `phase:1`, etc.). The
`nt` (Notion) plugin remains the tracker for non-memory work in this repo if any.

<!-- updated-by-superflow:2026-05-27 -->
