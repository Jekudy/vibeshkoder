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

## Authorized: Phase 5 — LLM gateway + answer synthesis (2026-04-30)

Phase 5 is authorized for implementation. Predecessor (Phase 4) closed 2026-04-30 with
6/6 tickets shipped, FHR pending. Owned by Orchestrator A per `ORCHESTRATOR_REGISTRY.md`.

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

## NOT authorized (future phases — gates not passed)

Do not start, design, or write speculative code for:

- Phase 6 catalog / cards — Orchestrator A unblocks after Phase 5 closure.
- Phase 7 daily summaries — depends on Phase 5 + Phase 6.
- Phase 8 reflection / observations — depends on Phase 7.
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
