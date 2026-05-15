# Authorized Execution Scope — Memory System Bootstrap

**Date:** 2026-04-26
**Cycle:** memory-foundation (first execution cycle on `feat/memory-foundation`)
**Authority:** team lead cover note from architect handoff

---

## TL;DR

Build only the **safety + source-of-truth foundation**. No LLM. No extraction. No catalog. No
wiki. No graph. No butler. No public surfaces.

If a ticket is not in the **authorized list** below, it is **out of scope** for this cycle and
must wait for its phase gate.

---

## Authorized: Phase 0 — Gatekeeper stabilization

| ID    | Title                                                              | Status                          |
|-------|--------------------------------------------------------------------|---------------------------------|
| T0-01 | Fix forward_lookup membership/admin check                          | DONE (PR#11, commit 7f95b53) — verifying coverage |
| T0-02 | Fix/contain sqlite vs postgres upsert in UserRepo                  | TODO                            |
| T0-03 | Make MessageRepo.save idempotent                                   | TODO                            |
| T0-04 | Implementation status doc                                          | DONE in this commit (this dir) |
| T0-05 | Add /healthz and startup checks                                    | TODO                            |
| T0-06 | Add gatekeeper regression tests for T0-01..T0-03                   | TODO                            |

## Authorized: Phase 1 — Source of truth + raw archive

| ID    | Title                                                              | Notes                           |
|-------|--------------------------------------------------------------------|---------------------------------|
| T1-01 | feature_flags table/repo                                           | All memory flags default OFF    |
| T1-02 | ingestion_runs table                                               | Run-id tagging                  |
| T1-03 | telegram_updates table (raw archive)                               | Idempotent on update_id         |
| T1-04 | raw update persistence service (`bot/services/ingestion.py`)       | Persists BEFORE normalization   |
| T1-05 | Extend `chat_messages` (reply_to / thread / caption / kind / policy / visibility / content_hash) | Additive, all nullable |
| T1-06 | message_versions table                                             | Provenance for citations later  |
| T1-07 | Backfill v1 message_versions from existing chat_messages           | Chunked if needed               |
| T1-08 | content_hash strategy                                              | Hash normalized text+caption+entities+kind |
| T1-09 | Persist reply_to_message_id                                        | Nullable, unresolved OK         |
| T1-10 | Persist message_thread_id                                          | Nullable                        |
| T1-11 | Persist caption + classify message_kind                            | First-class content             |
| T1-12 | Minimal `#nomem` / `#offrecord` policy detector                    | Deterministic only, no LLM      |
| T1-13 | Minimal `offrecord_marks` table                                    | Pair with redaction in Phase 3  |
| T1-14 | edited_message handler — only AFTER versions exist                 | Append v2 on hash change        |

## Stretch (only if Phase 0+1 complete with time left)

| ID    | Title                                                              | Notes                           |
|-------|--------------------------------------------------------------------|---------------------------------|
| T3-01 | forget_events tombstone skeleton                                   | Required before import apply    |
| T2-01 | Telegram Desktop import dry-run parser                             | Dry-run only, no apply          |

---

## Authorized: Phase 5 — LLM gateway + answer synthesis (2026-04-30) — **CLOSED 2026-05-11**

Phase 5 is authorized for implementation. Predecessor (Phase 4) closed 2026-04-30 with
6/6 tickets shipped. Owned by Orchestrator A per `ORCHESTRATOR_REGISTRY.md`.

**Status: CLOSED 2026-05-11.** All 6 Phase 5 tickets merged (T5-W0-01 via #203;
T5-01 #209 `7dcb218`; T5-02 #207 `5fcd99b`; T5-03 #223 `18c98893`; T5-04 #226 `43f21ee`;
T5-05 #229 `5faea1d`). Wave-2 closure docs #227 `358e144`. FHR Claude `deep-product-reviewer`
ACCEPTED with 4 MEDIUM carryovers (qa_trace_id type tightening, contracts.md field-name
drift fixed in closure PR, fixture runtime-seeded annotation fixed in closure PR, cascade
message_hash sub-case test deferred to Phase 6 kickoff). Phase 11 (Orch C) binding tests
green on main HEAD. Phase 6 (cards) now authorized below.

Authorized scope:
- `bot/services/llm_gateway.py` — single-entry LLM call interface; ALL provider calls
  must go through this module.
- `bot/services/llm_usage_ledger.py` + `bot/db/repos/llm_usage_ledger.py` — every call
  audited (provider, model, input_tokens, output_tokens, cost_usd_cents, governance
  filter result, ts).
- alembic migration 022+ for `llm_usage_ledger`, 023+ for `qa_traces` LLM extension
  fields (answer_text, citation_count, llm_call_id FK).
- Governance source-filter pre-call: refuse to send any evidence whose source row has
  `memory_policy IN ('offrecord','forgotten')` OR `is_redacted=TRUE`.
- Budget guard (per-day USD ceiling, configurable env var; refuse when exceeded).
- Cache layer (cite-stable input hash → cached answer; respects governance flips).
- `/recall` upgrade: optional LLM-synthesized answer when ≥1 evidence + flag enabled.

NOT in Phase 5 scope (defer to Phase 6+):
- Knowledge cards, extraction pipelines, daily digests, observations, reflection runs.
- Vector / semantic search beyond the FTS already shipped in Phase 4.
- Cross-chat or public answer surfaces.

---

## Authorized: Phase 11 — Shkoderbench / evals harness (2026-04-30)

Phase 11 is authorized for implementation in parallel with Phase 5. Owned by
Orchestrator C. No production runtime impact; offline / CI-only.

Authorized scope:
- `tests/evals/` — new top-level test category with golden datasets, citation quality
  assertions, leakage tests (no `#offrecord` / forgotten content in any answer),
  no-evidence refusal correctness, citation precision/recall.
- `bot/services/eval_*.py` — offline harness invoked by tests; no production wiring.
- Golden recall fixtures (`tests/fixtures/golden_recall/`, `tests/fixtures/eval_seeds/`).
- CI nightly job (`.github/workflows/evals.yml`) gated by env var (default OFF until
  baseline established).
- NO new alembic migrations. NO new production handlers.

Phase 11 evals must run against the live `/recall` from Phase 4 first (baseline) and
later against Phase 5 LLM-synthesized answers (regression suite for hallucination /
leakage / citation drift).

---

## Authorized: Phase 12 — Butler design docs only (2026-04-30)

Phase 12 is authorized **for documentation only**. NO implementation, NO execution
code, NO database tables. Owned by Orchestrator B as a docs side-quest.

Authorized scope:
- `docs/memory-system/PHASE12_PLAN.md` (promoted from `_DRAFT`) — design contract for
  the future butler action layer.
- Permission model spec, action audit log spec, abort / dry-run spec.

---

## Conditionally authorized: Phase 9, Phase 10 (gated)

Phase 9 (wiki) and Phase 10 (graph projection) are NOT yet authorized for runtime
implementation. Orchestrator B may:
- Refine `PHASE9_PLAN_DRAFT.md` and `PHASE10_PLAN_DRAFT.md` into `_PLAN.md` artifacts.
- Sketch schema designs (no migrations until ratified).
- Build read-only experiments in `.worktrees/orch-B-experiments/` (NOT pushed to main).

Promotion to "authorized for implementation" requires:
- Orchestrator A confirms Phase 6 (cards) closed (Phase 9 + 10 both depend on stable
  cards / relations).
- AUTHORIZED_SCOPE.md updated by human or Orchestrator A's closing PR.

---

## Authorized: Phase 6 — Knowledge cards + admin review (2026-05-11)

Phase 6 is authorized for implementation following Phase 5 closure 2026-05-11.
Owned by Orchestrator A per the synthesis chain (Phase 5 → 6 → 7 → 8).

Authorized scope (per `docs/memory-system/PHASE6_PLAN.md` (ratified 2026-05-12, see §Sprint 0 closure below)):
- `extraction_runs` + `extraction_candidates` + `knowledge_cards` + `card_sources` + `extraction_decisions` tables / repos / handlers (`card_revisions` deferred to Phase 6.5/9; see PHASE6_PLAN.md §11)
- Card extraction pipelines (using Phase 5 `llm_gateway` ONLY — no new LLM entry points)
- Admin review surface for card approval / rejection
- Cascade extension for card content tied to `forget_events`
- Privacy invariants #2 / #3 / #5 / #9 binding (cards are summaries — never canonical truth)

Phase 6 implementation must:
- Ratify `prompts/PHASE6_PLAN_DRAFT.md` → `PHASE6_PLAN.md` as Sprint 0 (similar to Phase 5
  Sprint 0 plan ratification).
- Close the Phase 5 FHR carryovers:
  - **M-1**: tighten `bot/services/llm_gateway.py::synthesize_answer` annotation
    `qa_trace_id: int | None` OR add runtime `assert qa_trace_id is not None`.
  - **M-4**: add direct `_cascade_qa_traces_llm` + `_cascade_llm_synthesis_cache`
    `message_hash` sub-case tests with `llm_response_summary` NULL assertions.

NOT in Phase 6 scope (defer):
- Daily/weekly digests (Phase 7).
- Reflection / observations / memory_events / memory_candidates / reflection_runs (Phase 8). Note: Phase 8 `memory_candidates` (reflection cluster queue) is a distinct concept from Phase 6 `extraction_candidates` (LLM-extracted card candidate queue); see PHASE6_PLAN.md §10 glossary.
- Wiki (Phase 9), graph projection (Phase 10), butler (Phase 12).
- `card_revisions` and `card_relations` — deferred to Phase 6.5/9.
- `/edit-card` admin command — deferred to Phase 6.5.

**Sprint 0 RATIFIED 2026-05-12**: see `docs/memory-system/PHASE6_PLAN.md`. Wave 1 (T6-00 carryover + T6-01 schema + T6-02 extractor + T6-03 gateway extract) authorized.

---

## Authorized: Phase 7 — Daily digests (2026-05-14, **CLOSED 2026-05-15**)

Phase 7 closed 2026-05-15. All 8 sprints merged across PRs #285 (T7-S0
docs ratify), #287 (T7-01 migration 037), #290 (T7-03 digest_context),
#293 (T7-02 run_digest + synthesize_digest), #294 (T7-04 scheduler +
reaper), #296 (T7-05 publisher + cascade), #297 (T7-06 admin handlers),
#298 (T7-07 Phase 11 binding), and T7-08 closure docs (this PR).

Feature flag `memory.digests.daily.enabled` default OFF. Production
rollout playbook: `docs/memory-system/PHASE7_ROLLOUT.md`. Phase 11
binding **34/34** green. Phase 7.5 carryovers tracked in issues #291
(shared predicate refactor) and #295 (T7-02 post-merge MED items).

Original authorization (preserved for context): Phase 7 was authorized
for implementation following Phase 5 + Phase 6 closure (both DONE
2026-05-11 / 2026-05-12) and the Phase 11 binding suite remaining green
on main. Owned by Orchestrator A per the synthesis chain (Phase 5 → 6 →
7 → 8).

Authorized scope (per `docs/memory-system/PHASE7_PLAN.md`, ratified 2026-05-14 after
two rounds of dual-model review — Codex technical + Claude product/spec):

- `digests` + `digest_runs` tables / repos (migration `037_add_digests.py`,
  contingent on Phase 6.5 carryover landing first; downshift to `036_` if not).
  Schema includes `posting` transient state + `posting_started_at` column for the
  publisher-vs-redactor race interlock (see PHASE7_PLAN.md §5.A / §5.F / §5.H).
- `bot/services/digests.py::run_digest` orchestrator with advisory-lock-based
  idempotency, dedicated Phase-7 cost ceiling (`DIGEST_DAILY_USD_CEILING`,
  default `Decimal("1.00")`), and `status='cost_exceeded'` short-circuit.
- `bot/services/digest_context.py::build_digest_context` cards-first +
  chronological raw-message fallback context builder, governance-filtered via
  the shared `_forget_excludes_predicate` extracted from `forget_cascade.py`.
- `bot/services/llm_gateway.py::synthesize_digest` — new gateway method with
  pre-provider context revalidation (defense-in-depth against forget races) and
  citation-invariant validation (every bullet ≥1 valid id).
- `bot/services/digest_publisher.py::publish_digest` — single-transaction
  `draft → posting → posted` flow holding the row lock across `bot.send_message`,
  destination guarded by `DIGEST_DESTINATION_CHAT_ID` env var (community chat).
- `bot/services/digest_renderer.py::render_digest_html` — HTML rendering with
  truncation-before-escape, tag-balance assertion, citation tokens stripped from
  public output (audit details exposed via admin handlers only).
- `bot/services/scheduler.py` extension — daily cron at `DIGEST_HOUR_MSK`
  (default 09:00 MSK) gated by `memory.digests.daily.enabled` (default OFF) +
  `digest_stale_posting_reaper_job` every 5 min for orphan recovery.
- Forget cascade extension in `bot/services/forget_cascade.py` — one merged
  `digests` layer placed BEFORE `card_sources`, blocking `SELECT FOR UPDATE`
  with `statement_timeout=5s`, unconditional `bot.edit_message_text` with
  erratum fallback on `TelegramBadRequest` and admin-notify-only on
  `TelegramForbiddenError` (acknowledged privacy stop signal §8 of plan).
- Admin handlers `bot/handlers/digest.py` — `/digest_now [daily]`,
  `/digest_preview <type> [date]`, `/digest_history`. Admin-only via
  `_is_admin` Phase 6 helper.
- Phase 11 binding tests `tests/evals/test_leakage.py::L7a/b`,
  `tests/evals/test_citations.py::C6`, new
  `tests/evals/test_digest_forget_cascade.py::I5a/b/c`. Existing 28/28 binding
  suite preserved → new total 34/34.

Phase 7 implementation must:
- Sprint 0 (T7-S0): land this scope authorization + PHASE7_PLAN.md commit in one
  docs-only PR. No code in Sprint 0.
- Wave 1 (T7-01, T7-02, T7-03): schema + run_digest + context builder.
- Wave 2 (T7-04, T7-05): scheduler + publisher + forget cascade.
- Wave 3 (T7-06, T7-07, T7-08): admin handlers + binding tests + closure docs.
- Final Holistic Review (FHR) required after T7-08 — 8 sprints + privacy
  invariants binding triggers superflow Rule 9.

NOT in Phase 7 scope (defer to Phase 8+):
- Weekly digest scheduler / handler / publisher (Phase 8). The `digests.type`
  enum accepts `'weekly'` for schema readiness only — no Phase 7 code path
  produces or processes weekly rows.
- Reflection / observations / `memory_events` / `memory_candidates` (Phase 8).
- Wiki (Phase 9), graph projection (Phase 10), butler (Phase 12).
- Per-user opt-out for being mentioned in a digest.
- Multi-chat digest support (single-chat MVP).
- Inline citation rendering in public posts (admin-only via `/digest_preview`).
- Topic clustering / LLM-inferred topic ordering (chronological only in MVP).
- Reaction-count / reply-count ranking heuristics (columns don't exist on
  `chat_messages`; adding them is a separate Phase 8 migration ticket).

---

## Authorized: Phase 8 — Weekly editorial digest (2026-05-15)

Phase 8 is authorized for implementation following Phase 7 closure (CLOSED
2026-05-15) and the Phase 11 binding suite remaining green on `main` at
the 30/30 baseline (the legacy "34/34" framing was math drift; corrected
via direct enumeration of `tests/evals/test_*.py` in `PHASE8_PLAN.md` §0/§10).

Authoritative reference: `docs/memory-system/PHASE8_PLAN.md` (this Sprint 0
ratification PR). Phase 8 = **weekly editorial digest only**.
Reflection / observations / `memory_events` / `memory_candidates` /
`reflection_runs` are explicitly deferred to Phase 9+ (the previously
proposed `PHASE8_PLAN_DRAFT.md` is superseded by the ratified plan; the
draft will be archived/flagged in T8-08).

Authorized work in this cycle:
- Migration 038 (T8-01): extend `ck_digests_status` + `ck_digest_runs_status`
  CHECK enums (5 new audit values: `awaiting_review`, `approved_for_publish`,
  `rejected_by_admin`, `rejected_by_reaper`, `regenerated_by_admin`); ADD cols
  `published_by_admin_id`, `approved_at`, `review_notes`, `awaiting_review_at`;
  partial index `ix_digests_status_awaiting_review`; CHECK
  `ck_digests_approved_audit` + body-NOT-NULL visible-states widening;
  pre-flight downgrade guard against `posting` + 4 new review statuses.
  NOT VALID + VALIDATE pattern for all CHECK swaps.
- Weekly orchestrator (T8-02): widen `run_digest(type=Literal['daily','weekly'])`;
  new prompt template `digest_weekly_v0_1_0.py` with section-aware structure
  (allowlist: Highlights / People / Decisions / Open questions / Other);
  `synthesize_digest` routes by type; weekly cost ceiling separate bucket
  (`DIGEST_WEEKLY_USD_CEILING` $5.00 default; independent of daily — Q7).
- Weekly context (T8-03): `build_digest_context` extension for 7-day ISO
  Mon..Mon MSK window; larger token budget (`DIGEST_WEEKLY_TOKEN_BUDGET`
  24000 default); `weekly_min_cards_threshold` 8.
- Review state machine (T8-04): `bot/services/digest_review.py` with
  `transition_to_awaiting_review`, `approve_digest` (3-step: revalidate →
  guarded UPDATE approve → dispatch publisher), `reject_digest`. Canonical
  `_raise_invalid_state_after_guard_miss` helper. Cascade scan + redactor
  allowlist widening to include 4 new statuses
  (`_REDACTOR_ELIGIBLE_STATUSES` 8-tuple). Publisher trigger-state
  widening (`('draft', 'approved_for_publish')`).
- Scheduler (T8-05): `digest_weekly_job` (cron Mon 09:15 MSK — 15-min H8
  stagger past daily 09:00) + `digest_stale_review_reaper_job` (48h DM
  notify + 7d auto-reject `rejected_by_reaper`); flag-gated by
  `memory.digests.weekly.enabled` default OFF.
- Admin handlers (T8-06): `/digest_now weekly` (+ `--regenerate` flag for
  Q5 no-edit refuse-and-rerun flow), `/digest_review`, `/digest_approve <id>`,
  `/digest_reject <id> [reason]`. Admin-gated via existing `_is_admin`.
- Phase 11 binding extension (T8-07): 12 new cases (L8a/b + C7 + I6a +
  I6b.1/.2/.3 + I6c + R5.a/b/c/d) → 30/30 baseline + 12 = **42/42 total**.
- Closure docs (T8-08): `PHASE8_ROLLOUT.md` + `IMPLEMENTATION_STATUS.md` +
  `ROADMAP.md` row 8 update + `CLAUDE.md` Phase 8 closure block +
  `AUTHORIZED_SCOPE.md` Phase 8 CLOSED marker + FHR per Rule 9.

Wave layout (per `PHASE8_PLAN.md` §7 + §14, parallelized per L6):
- Sprint 0 (T8-S0): this PR — `PHASE8_PLAN.md` + scope authorization. No code.
- Wave 2 (T8-01 sequential): migration 038.
- Wave 3 (T8-02 ∥ T8-03 parallel): orchestrator + context.
- Wave 4 (T8-04 ∥ T8-05 parallel): review SM + cascade/redactor/publisher
  widening + scheduler/reaper.
- Wave 5 (T8-06 ∥ T8-07 parallel): admin handlers + binding tests.
- Wave 6 (T8-08): closure + FHR (Rule 9: 8+ sprints + privacy invariants).

NOT in Phase 8 scope (defer to Phase 8.5+ or Phase 9+):
- Reflection / observations / `memory_events` / `memory_candidates` /
  `reflection_runs` (Phase 9 or later; superseded `PHASE8_PLAN_DRAFT.md`
  shifts to Phase 9+ backlog).
- Admin edit-before-approve (Q5: no edit in v1; `--regenerate` flow only).
- Multi-admin approval quorum (Q4: single-admin in v1).
- Per-user opt-out for being mentioned in a digest.
- Multi-chat weekly digest (single-chat MVP).
- Reaction-count / reply-count ranking on weekly window.
- LLM topic clustering / inferred-topic ordering (chronological-by-section
  in v1; allowlist of 5 section names).
- Phase 7 carryover #291 (shared `_forget_excludes_predicate` refactor)
  remains advisory — T8-03 may inline predicate identically to Phase 7
  with explicit TODO if #291 has not landed.
- GIN index `ix_digests_citations_gin` performance rewrite — Phase 7.5 /
  8.5 backlog; not a privacy blocker (M6).

---

## NOT authorized (future phases — gates not passed)

Do not start, design, or write speculative code for:


- Phase 8 reflection / observations — Phase 9+, the previously-deferred
  reflection track stays deferred; the new Phase 8 scope is weekly digest
  per `PHASE8_PLAN.md`.
- Wiki (member or public) implementation — Phase 9, conditionally above.
- Graph projection (Neo4j / Graphiti) implementation — Phase 10, conditionally above.
- Butler / action execution — Phase 12, design-only above.
- Person expertise pages — Phase 6+.
- Public surfaces of any kind — Phase 9 with explicit approval.

---

## Critical safety rule for `#offrecord`

> `#offrecord` content **must not** be durably stored as raw visible content.

Implementation default for the policy detector + raw persistence:

- **Detect `#offrecord` BEFORE committing content-bearing `raw_json`**, OR
- Write raw update + redaction in the same transaction before commit.

Committed storage for `#offrecord` keeps only minimal metadata:
- chat id
- message id
- timestamp
- hash / tombstone key
- policy marker
- audit metadata

**No** search, q&a, extraction, summary, catalog, vector, graph, or wiki may use `#offrecord`
content. Forbidden content never reaches `llm_gateway`.

### `#offrecord` ordering rule (T1-04 ↔ T1-12 cross-cutting requirement)

The ticket order in this cycle puts T1-04 (raw update persistence) BEFORE T1-12 (deterministic
policy detector). Without an explicit rule, a compliant T1-04 implementation would commit
content-bearing `raw_json` for several days before T1-12 lands the detector. That is a silent
violation of the `#offrecord` rule.

**Cross-cutting requirement (binding for both tickets):**

1. **T1-04 must not merge until either (a) the detector stub is in place, or (b) the raw
   archive feature flag `memory.ingestion.raw_updates.enabled` defaults to `false` AND there
   is no production environment in which it is set to `true` until T1-12 lands.**

2. T1-04's PR MUST include `bot/services/governance.py::detect_policy(text, caption) ->
   ('normal'|'nomem'|'offrecord', mark_payload_or_None)` as a stub returning `('normal', None)`
   for any input. The stub MUST be called inside the same DB transaction as the
   `telegram_updates` insert. This guarantees that when T1-12 replaces the stub with the real
   detector, the redaction path is already wired and atomic.

3. T1-04's PR MUST persist content-bearing `raw_json` ONLY in the same DB transaction that
   runs `detect_policy()`. If a future implementation moves the raw write to its own
   transaction, the move requires explicit team-lead approval and a follow-up safety review.

4. T1-12's PR replaces the stub with the real detector AND adds `offrecord_marks` insertion
   (T1-13 is in the same PR or merged immediately after). Between T1-12 merge and T1-13 merge,
   the detector still works — `offrecord_marks` adds the audit row, not the redaction itself.

5. The redaction itself happens inside the same transaction: when `detect_policy()` returns
   `'offrecord'`, the raw_json `text` / `caption` / `entities` fields are nulled or replaced
   with a sentinel before commit. The hash, ids, timestamps, and policy marker are kept.

If you are picking up T1-04 in isolation: implement the stub. Do not skip it. Do not merge a
T1-04 that writes raw_json without going through the (stub) detector path.

### Known gap: `chat_messages.raw_json` and the `caption` column (CLOSED in PR #63)

**STATUS: CLOSED in PR #63 (T1-12 + T1-13 combined sprint).** Both paths now route through
`detect_policy` before persistence; offrecord content is nulled in the same transaction;
`offrecord_marks` audit row is created in the same tx via `OffrecordMarkRepo.create_for_message`.
The historical context below is preserved as a record of why the rule exists — DO NOT
re-introduce a path that bypasses `detect_policy`.

---

**Historical context (pre-PR #63 state):**

The `#offrecord` ordering rule above governed only the `telegram_updates` path. The
`chat_messages` path (gatekeeper-era handler at `bot/handlers/chat_messages.py`) used to
write its own `raw_json` directly via `MessageRepo.save` and did NOT route through
`bot.services.governance.detect_policy()`. Same applied to the `caption` column added in
T1-05 and populated by T1-09/10/11 normalization — it stored the caption verbatim with no
redaction.

That gap was known and intentional in Phase 1, deferred until T1-12. T1-12 was required to
close BOTH paths in one go:

1. The text path through `bot/services/ingestion.py` → `telegram_updates` (already wired
   to call the stub detector; T1-12 swapped the stub for the real detector).
2. The text + caption path through `bot/handlers/chat_messages.py` →
   `chat_messages.raw_json` + `chat_messages.caption`. T1-12 extended the chat_messages
   handler to call `detect_policy()` BEFORE the `MessageRepo.save` call and either redact
   (for offrecord) or annotate (for nomem) accordingly.

Mitigation that was in place until T1-12 landed:
- The `chat_messages` handler in T1-09/10/11 deliberately does NOT extend `raw_json` to
  caption-only media messages. Captions are stored only in the `caption` column, and
  `raw_json` is still populated only when text is present (matching the gatekeeper-era
  behaviour).
- The `caption` column is the new exposure introduced by T1-05/T1-11. Operators running
  the bot in `#offrecord`-active chats accept that captions land in the DB unfiltered
  until T1-12.
- Search / q&a / extraction / catalog / wiki / graph / LLM features all remain disabled by
  feature flag, so the unfiltered caption never reaches downstream consumers in this
  cycle.

T1-12's PR description MUST mention this gap and confirm both paths are now governance-
filtered before merging.

---

## Telegram import rule (relevant if T2-01 is picked up)

Telegram Desktop import has two modes:

- **Dry-run** — allowed before full governance (Phase 2a). Parses the export, reports stats, **no
  content writes**.
- **Apply** — blocked until `#nomem` / `#offrecord` detection AND `forget_events` tombstone
  skeleton both exist. Apply must use the same normalization + governance path as live Telegram
  updates.

### Edit history during import (Phase 2 apply binding rule)

Telegram Desktop export stores only the **final state** of edited messages — no v1/v2 chain is
available. This is a structural constraint of the TD export format (documented in
`docs/memory-system/telegram-desktop-export-schema.md §4` and
`docs/memory-system/import-edit-history.md §2`).

**Binding rule for #103 (Stream Delta — import apply):**

1. Every imported `message_versions` row MUST be created with
   `imported_final = TRUE` (Boolean column, added by the #103 migration).
   This applies to all imported rows, not only those with the `edited` field set.
   `imported_final = TRUE` means "constructed from a static archive; live edit-chain
   knowledge is absent."
   Detailed policy rationale and implementation surface: `docs/memory-system/import-edit-history.md`.

2. This rule does NOT change `#offrecord` discipline. Every imported message — regardless of
   `imported_final` — still routes through `governance.detect_policy` exactly like a live
   message, in the same transaction as the content write.

3. Implementation (migration + write logic) lands in #103 / Stream Delta.
   This sprint (#106) is doc-only.

---

## `allowed_updates` rollout rule

Do not add Telegram update types before storage + handler exist.

| Update type                         | Required prerequisites                                  |
|-------------------------------------|---------------------------------------------------------|
| `edited_message`                    | message_versions table + edit handler (T1-06 + T1-14)   |
| `message_reaction`                  | reactions table + handler (Phase 5)                     |
| `message_reaction_count`            | reactions table + handler (Phase 5)                     |

Adding an update type without a handler causes silent data loss. Always test the allowed_updates
list against the registered routers.

---

## Agent execution rules

Coding agents (any subagent that writes code in this cycle) MUST:

1. Inspect current code before editing.
2. Work ticket-by-ticket. One ticket per PR.
3. Keep PRs small. If diff > ~400 lines, split.
4. Preserve existing gatekeeper behaviour (onboarding / questionnaire / vouching / intro refresh).
5. Add tests with every change.
6. Never assume docs/specs are implemented — verify against the code.
7. Never introduce LLM calls outside `llm_gateway` (which does not exist yet — so no LLM calls
   at all in this cycle).
8. Never implement future phases early.
9. Never log secrets / env values.
10. List changed files, tests run, and risks in the PR body.

---

## First-sprint definition of done

By the end of the first sprint (this cycle):

- Current gatekeeper still working (regression tests green).
- `forward_lookup` privacy fix verified.
- Sqlite/postgres upsert issue contained.
- Duplicate message save safe (idempotent `MessageRepo.save`).
- `feature_flags` table.
- `ingestion_runs` table.
- `telegram_updates` table.
- Raw update persistence for current message updates.
- Extended `chat_messages` fields.
- `message_versions` with v1 backfill.
- `reply_to_message_id` / `message_thread_id` / `caption` / `message_kind` persistence.
- Minimal `#nomem` / `#offrecord` policy detection.
- Minimal `offrecord_marks` table with detector wiring.
- Tests covering all of the above.

Everything else is out of scope until phase gates pass.
