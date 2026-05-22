# Phase 12 — Future Butler / Action Layer (design-only)

**Status:** DESIGN-ONLY / POSTPONED
**Authorized:** NO — docs only, no execution code
**Issue:** #116 (`EPIC: Phase 12 — Future butler action layer (design only / postponed)`)
**Pre-requisites:** Phase 0-11 CLOSED (Phase 0 safety, Phase 1 source-of-truth, Phase 2a/2b import, Phase 3 governance skeleton, Phase 4 FTS+Q&A, Phase 5 `llm_gateway`, Phase 6 cards, Phase 7 daily digest, Phase 8 weekly digest, Phase 9 wiki, Phase 11 binding suite — all green as of 2026-05-19; Phase 10 graph projection authorized 2026-05-17, in progress)
**Relationship to `PHASE12_PLAN.md`:** this design doc is the higher-level orientation companion to the detailed `PHASE12_PLAN.md` (ratified 2026-05-02). PLAN.md carries the per-component SQL schemas and per-ticket acceptance criteria. DESIGN.md (this file) presents the architectural picture, the rollout staging, the §11 Phase 11 binding test family extension, and the open design questions surfaced post-Phase 9 closure. The two are intentionally redundant on §1 invariants and §3 architecture; everything else is complementary.

---

## §1. Goal & Non-Goals

### Goal

Phase 12 documents the boundary contract for a **future** constrained action agent (the "Butler") that turns the now-mature, governance-filtered Shkoderbot memory system into auditable, confirmable, reversible community-facing actions.

The Butler is **not** an autonomous operator. It plans candidate actions from governance-filtered evidence, asks for explicit confirmation, executes only whitelisted tools, records every decision and tool invocation in dedicated audit tables, and provides a per-action rollback path where technically possible.

Phase 12's only deliverable is **this design document plus `PHASE12_PLAN.md`** — no migrations, no Python modules, no handlers, no tests, no feature flags, no scheduler entries. Per `HANDOFF.md §3` Phase 12 row (line 482) and `AUTHORIZED_SCOPE.md`, Phase 12 is a tombstone for the boundary, not an implementation cycle.

### Non-Goals (verbatim from invariants + HANDOFF.md)

The Butler MUST NOT, when it is eventually implemented:

- Read raw DB directly (`telegram_updates`, `chat_messages`, raw SQL rows, raw graph rows). It MUST consume only governance-filtered `EvidenceContext` envelopes from Phase 4/5 services. (Invariant #7.)
- Make LLM calls outside `bot/services/llm_gateway.py`. Direct provider SDK imports (`anthropic`, `openai`) inside any `bot/services/butler*.py` or `bot/services/butler_tools/*.py` are forbidden. (Invariant #2.)
- Read, plan over, or act on `#nomem`, `#offrecord`, redacted, forgotten, or tombstoned content. The governance filter runs BEFORE the LLM gateway call. (Invariant #3.)
- Cite raw `chat_messages.id`. Every Butler decision that uses memory must cite `message_version_id` or approved `card_sources` ids. (Invariant #4.)
- Treat summary, digest, wiki page, or graph traversal output as canonical truth. They are evidence inputs, not authoritative state. (Invariants #5, #6.)
- Bypass the per-action confirmation gate. Session-wide opt-in is explicitly out of baseline scope. (PHASE12_PLAN §2.)
- Execute actions on behalf of a user who is not a community member or admin. Non-members cannot invoke `/butler`. (PHASE12_PLAN §2 Abuse Prevention.)
- Send cross-user messages without the affected user's confirmation. Default flow REQUIRES affected-user consent. (PHASE12_PLAN §2 Cross-User Butler Actions.)
- Touch money, payments, calendar APIs (Google Calendar, etc.), email APIs, CRM, webhooks, browsers, shell, filesystem, or arbitrary HTTP outside the whitelisted Telegram bot wrapper methods. (PHASE12_PLAN §3.)
- Ship a feature flag that defaults to ON. `memory.butler.enabled` and any per-feature sub-flag MUST default OFF. (PHASE12_PLAN §8.)
- Persist butler-side audit tables that are not wired into `bot/services/forget_cascade.CASCADE_LAYER_ORDER`. (Invariant #9; PHASE12_PLAN §11.4 row 9.)

---

## §2. Surface — What the Butler Does

The Butler exposes a deliberately tiny capability set. Six conceptual capabilities, four user-facing Telegram commands, five whitelisted tools.

### 2.1 Conceptual capabilities

1. **Evidence recall before action.** Before any Butler action, the planner pulls governance-filtered evidence from Phase 4/5 (`EvidenceContext`). The Butler can preview the evidence it intends to act on, identical to `/recall` output but framed as "here is what I'd act on, ok?".
2. **Meeting / sync proposal.** The Butler can propose a meeting time in the originating chat. Telegram-native only — no Google Calendar, no iCal, no external API. The proposal is a Telegram message with an inline keyboard for participants to react.
3. **Cross-user introductions.** The Butler can send an intro between two consenting members. Member A asks to be introduced to B around topic X; the Butler builds a draft from approved evidence, A confirms the draft, B receives a consent prompt with the exact intro text, and the intro is sent only if B confirms.
4. **Intro / proposal follow-ups.** The Butler can update or correct a previously-sent Butler message (only Butler-owned messages, recorded in `butler_actions`). If editing fails, a follow-up correction message is posted instead.
5. **Knowledge-card suggestions (admin-review only).** The Butler can suggest creating or updating a knowledge card. The suggestion lands in the Phase 6 admin-review queue. The Butler MUST NOT directly create or activate a card — Phase 6 admin review is the authority.
6. **Deferred reminders for the requester only.** Out of Phase 12 baseline — flagged as a candidate Phase 12.1 extension (see §8 Rollout). The reminder surface introduces cron/persistence dependencies and a per-user trigger-storage decision that is not part of baseline 5-tool scope.

### 2.2 Telegram commands (baseline 4)

| Command | Purpose | Mutates state? | Requires confirmation? |
|---|---|---|---|
| `/butler <request>` | Plan one or more candidate actions and post per-action preview cards. | Writes `butler_actions` in status `requested` → `pending_confirmation`. No external side effect. | N/A (planning only) |
| `/butler_status <action_id>` | Show current state + audit summary for a Butler action. Admin sees all, requester sees own, affected_user sees own. | Read-only. | No |
| `/butler_cancel <action_id>` | Cancel a `pending_confirmation` action before execution. | Transitions to `cancelled`. No external side effect. | No (cancel is itself a stop signal) |
| `/butler_undo <action_id>` | Request rollback. Triggers inverse-op execution where technically possible. | Writes a linked `butler_actions` row with `parent_action_id`. Original audit immutable. | Yes (undo confirmation is a separate per-action gate) |

### 2.3 Tool whitelist (exactly 5)

```
ALLOWED_BUTLER_TOOLS = {
    "recall_evidence",        # sealed EvidenceContext fetch (read-only, audit-only)
    "schedule_meeting",       # post a Telegram-native meeting proposal
    "send_intro",             # send a cross-user intro after both confirmations
    "update_intro",           # edit Butler-owned intro / post follow-up correction
    "suggest_card_creation",  # write a pending admin-review suggestion
}
```

Unknown tool names rejected before user confirmation. Schema-invalid args rejected before user confirmation. No raw Telegram method names appear in LLM output (the LLM emits tool names; the tool layer maps name → wrapper method).

---

## §3. Architecture — Where the Butler Sits

The Butler is a thin orchestrator that sits between a user-facing Telegram command and the existing memory system services. It introduces NO new infrastructure (no new database, no new cache, no new scheduler), only new audit tables and a strict tool registry.

### 3.1 ASCII flow

```
Telegram /butler command (member or admin)
        |
        v
+------------------------------------+
| bot/handlers/butler.py             |
|   - membership / admin authz       |
|   - parse request                  |
|   - create butler_actions row      |
|     in status 'requested'          |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Trigger layer                      |
|   - user-initiated only (baseline) |
|   - no cron, no event listeners    |
|     in baseline (Phase 12.1+)      |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Context-gathering layer            |
|   bot/services/evidence_context.py |
|   (Phase 4 + Phase 5 surface)      |
|   - FTS / hybrid retrieval         |
|   - governance filter applied here |
|   - returns sealed EvidenceContext |
|   - context_hash stored            |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Reasoning layer                    |
|   bot/services/llm_gateway.py      |
|   - call_type='butler_decision'    |
|   - structured ButlerPlan output   |
|   - tool schemas in prompt         |
|   - llm_usage_ledger row written   |
|   - budget guard (per-action +     |
|     per-user + per-chat daily)     |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Plan validation                    |
|   bot/services/butler.py           |
|   - tool in whitelist?             |
|   - args match schema?             |
|     - all evidence_ids in context? |
|   - cross-user consent needed?     |
|   - hallucinated args -> REJECT    |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Per-action confirmation flow       |
|   bot/handlers/butler.py           |
|   - inline keyboard per action     |
|   - exact text preview             |
|   - TTL (5/15/30 min by risk)      |
|   - cross-user consent prompt to   |
|     affected users                 |
|   - preview_payload_hash stored    |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Action layer (NEW)                 |
|   bot/services/butler_tools/*.py   |
|   - strict whitelist registry      |
|   - one tool = one Telegram        |
|     wrapper method                 |
|   - builds inverse_op_payload      |
|     BEFORE marking 'succeeded'     |
|   - butler_tool_invocations row    |
|     per attempt, no hidden retries |
+----------------+-------------------+
                 |
                 v
+------------------------------------+
| Audit layer (NEW)                  |
|   - butler_actions (one per plan)  |
|   - butler_tool_invocations        |
|     (one per tool call attempt)    |
|   - butler_action_confirmations    |
|     (one per confirm / reject)     |
|   - llm_usage_ledger link          |
|     (caller='butler')              |
|   - forget_cascade.CASCADE_LAYER_  |
|     ORDER includes butler_actions  |
+------------------------------------+
```

### 3.2 Layers, numbered

1. **Trigger layer.** Baseline: user-initiated via `/butler <request>` only. NO cron, NO event-driven triggers, NO proactive nudges, NO follow-up summaries in baseline scope. The trigger storage decision (Postgres cron extension vs APScheduler with persistence vs no-trigger) is deferred to Phase 12.1+ and surfaced in §10 Open Questions.
2. **Context-gathering layer.** Reuses Phase 4 `bot/services/search.py` (`SearchHit`) + Phase 4 `bot/services/qa.py` (`EvidenceBundle`) + Phase 6 approved `card_sources` + Phase 9 published wiki revisions + (when available) Phase 10 `bot/services/graph_query.py`. Returns a sealed `EvidenceContext` envelope; the Butler never touches the underlying tables.
3. **Reasoning layer.** Reuses the Phase 5 `llm_gateway` surface. A new `call_type='butler_decision'` discriminator is added to `llm_usage_ledger` (Phase 10 introduced the `call_type` column via migration 064 — Phase 12 adds a new bucket, no schema change). A second `call_type='butler_summary'` covers the rare case where Butler emits user-facing prose (e.g. intro draft text); the same gateway path applies. The output is a structured `ButlerPlan` JSON (see PHASE12_PLAN §2 LLM Gateway Contract).
4. **Action layer (NEW).** The Butler tool registry under `bot/services/butler_tools/`. Each tool implements a common Protocol (`validate_policy` + `execute` + `build_inverse`). Tools receive `ButlerToolContext` carrying the sealed `EvidenceContext` and the validated args. Tools NEVER call the LLM, NEVER hit arbitrary HTTP, NEVER touch the DB except for their own audit / suggestion rows.
5. **Audit layer (NEW).** Three audit tables and one ledger linkage. Every state transition writes a row. Failure to write audit is itself a stop signal — the action does not execute. (PHASE12_PLAN §8.)

The Butler reuses, in design-only fashion, the following existing services as **read-only consumers**:

| Service | Path | Phase | Butler's use |
|---|---|---|---|
| `search` / `qa` | `bot/services/search.py`, `bot/services/qa.py` | Phase 4 | FTS + evidence bundle. Butler asks via a thin wrapper, not directly. |
| `llm_gateway` | `bot/services/llm_gateway.py` | Phase 5 | The only LLM boundary. New `call_type='butler_decision'`. |
| `knowledge_card` repos | `bot/db/repos/knowledge_card.py` | Phase 6 | `suggest_card_creation` writes a pending review row; never an approved card. |
| `digest_*` (read-only) | `bot/db/repos/digest.py` | Phase 7/8 | Evidence context may include posted digest references; Butler does not generate digests. |
| `wiki_*` (read-only) | `bot/services/wiki_renderer.py` | Phase 9 | Evidence context may cite approved member-internal wiki revisions. |
| `graph_query` | `bot/services/graph_query.py` | Phase 10 | Evidence context may include 2-hop graph traversal results (admin scope only — R7.a is binding). |
| `forget_cascade` | `bot/services/forget_cascade.py` | Phase 3 → ongoing | New `butler_actions` layer in `CASCADE_LAYER_ORDER`. |

---

## §4. Data Model (proposed schema — DESIGN-ONLY)

This section restates the proposed schema for orientation. The binding contract lives in `PHASE12_PLAN.md §5.A`. No migration files exist for any of these tables. Migration numbers reserved per `ORCHESTRATOR_REGISTRY.md` are NOT to be claimed until `AUTHORIZED_SCOPE.md` is amended.

### 4.1 NEW tables

```
butler_actions
  - id BIGSERIAL PK
  - action_uuid UUID UNIQUE
  - parent_action_id BIGINT REFERENCES butler_actions(id)  -- undo linkage
  - requester_tg_id BIGINT
  - chat_id BIGINT
  - action_type TEXT          -- meeting | intro | intro_update | card_suggestion | recall
  - status TEXT               -- 11 states per §5 PHASE12_PLAN lifecycle
  - tool_name TEXT            -- must match ALLOWED_BUTLER_TOOLS
  - tool_manifest_version TEXT
  - evidence_context_hash TEXT  -- sha256 over EvidenceContext.context_id + items
  - evidence_ids JSONB           -- citation anchors only, no raw payloads
  - approved_card_source_ids JSONB
  - plan_summary TEXT
  - action_args JSONB
  - action_args_hash TEXT
  - result_payload JSONB         -- Telegram ids + hashes, never private text
  - result_payload_hash TEXT
  - inverse_op_payload JSONB
  - rollback_kind TEXT          -- delete_message | edit_message | followup_correction
                                --   | cancel_pending | not_reversible
  - risk_level TEXT             -- low | medium | high
  - requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE
  - confirmation_policy TEXT    -- per_action (baseline) | session_wide (FUTURE only)
  - expires_at TIMESTAMPTZ      -- TTL for pending_confirmation rows
  - confirmed_at TIMESTAMPTZ
  - executed_at TIMESTAMPTZ
  - undone_at TIMESTAMPTZ
  - rejection_reason TEXT
  - error_code TEXT
  - error_context JSONB
  - llm_usage_ledger_id BIGINT  -- FK to Phase 5 ledger; mandatory for planned actions
  - created_at TIMESTAMPTZ
  - updated_at TIMESTAMPTZ

butler_tool_invocations
  - id BIGSERIAL PK
  - action_id BIGINT FK butler_actions
  - tool_name TEXT
  - invocation_seq INT
  - idempotency_key TEXT UNIQUE
  - request_payload JSONB
  - request_payload_hash TEXT
  - response_payload JSONB
  - response_payload_hash TEXT
  - status TEXT
  - started_at / finished_at TIMESTAMPTZ
  - error_code TEXT
  - error_context JSONB

butler_action_confirmations
  - id BIGSERIAL PK
  - action_id BIGINT FK butler_actions
  - confirmer_tg_id BIGINT
  - confirmation_role TEXT      -- requester | affected_user | admin | rollback_requester
  - status TEXT                 -- pending | confirmed | rejected | expired | cancelled
  - confirmation_message_chat_id BIGINT
  - confirmation_message_id BIGINT
  - preview_payload_hash TEXT
  - confirmed_at / rejected_at TIMESTAMPTZ
  - expires_at TIMESTAMPTZ
  - created_at TIMESTAMPTZ
```

### 4.2 Modified columns on existing tables

```
llm_usage_ledger.call_type IN (
    'qa_synthesis',     -- Phase 5 baseline
    'digest_daily',     -- Phase 7
    'digest_weekly',    -- Phase 8
    'extract_candidates',  -- Phase 6
    'graph_projection', -- Phase 10
    'butler_decision',  -- Phase 12 (NEW)
    'butler_summary',   -- Phase 12 (NEW)
    'unknown'           -- legacy backfill default
)
```

Phase 10 migration 064 already added the `call_type` column (default `'unknown'`). Phase 12 adds NO new column — it adds two new allowed values in the application-level enum and (when implemented) extends the CHECK constraint via a single-line migration.

### 4.3 No new graph nodes

The Butler reads existing `graph_provenance` rows via `graph_query`. It does NOT add new graph node types, NOT add new graph edges, and is NOT a projection source. The graph is read-only to the Butler. Invariant #6 holds.

### 4.4 Forget cascade integration

`CASCADE_LAYER_ORDER` MUST insert two new layers (added in `PHASE12_PLAN §11.4 row 9`):

```
... -> digests -> wiki_revisions -> wiki_pages -> graph_nodes ->
                                                       -> butler_action_confirmations
                                                       -> butler_tool_invocations
                                                       -> butler_actions
                                                       -> card_sources -> message_versions ...
```

The Butler layers come AFTER `graph_nodes` (so graph cascade has already redacted its source projections) and BEFORE `card_sources` (so card-level redaction can still find any Butler-suggested cards). Three layer functions added to `_LAYER_FUNCS`: `_cascade_butler_action_confirmations`, `_cascade_butler_tool_invocations`, `_cascade_butler_actions`. Each masks privacy-sensitive payload fields with `[CONTENT_REDACTED: forget_event_id={n}]` per existing Phase 9 redaction format; ids and structural metadata are preserved for audit continuity.

---

## §5. Governance Contract

Restated as a fail-closed checklist. Each item is a stop signal if violated.

The Butler MUST:

- **Honour the evidence + citations invariant.** Every action references at least one `message_version_id` or approved `card_sources` id from the sealed `EvidenceContext`. (Invariants #3, #4.)
- **Never act on forgotten content.** The Phase 10 `assert_no_pending_purge`-style read-block applies — if any source row has a non-purged `forget_events` entry, the action REJECTS. The graph_query pending-purge read-block is reused unchanged. (Invariant #9.)
- **Respect `#nomem` / `#offrecord`.** The governance pre-filter inside the evidence context service excludes these policies before any data crosses the Butler boundary. The Butler itself never re-evaluates policy — it trusts and verifies via `context_hash`. (Invariant #3.)
- **Log every decision in `butler_actions` — even no-op decisions.** A planned action that the user cancels still writes a `butler_actions` row in status `cancelled` with `result_payload=NULL` and `rejection_reason='user_cancel'`. A planned action with hallucinated args writes a `butler_actions` row in status `rejected` with `rejection_reason='hallucinated_args'` and `error_context` detailing which field failed. Audit is never optional. (PHASE12_PLAN §8.)
- **Use Phase 5 `llm_gateway` for ALL LLM calls.** No direct provider SDK calls in `bot/services/butler*.py`. Enforced by lint (extend `lint_privacy_check.sh` to forbid `anthropic` / `openai` imports in butler paths). (Invariant #2.)
- **Treat the EvidenceContext as immutable.** The Butler does not mutate, re-rank, or augment the context after `recall_evidence` returns. If the plan requires additional evidence, a new `recall_evidence` call writes a new audit row.
- **Default `requires_confirmation = TRUE`.** Per-action confirmation is the baseline. Session-wide opt-in is OUT OF SCOPE (PHASE12_PLAN §2 "User Confirmation Default" rationale).
- **Default `memory.butler.enabled = OFF`.** Feature flag flip requires explicit team-lead approval AND `AUTHORIZED_SCOPE.md` amendment. (PHASE12_PLAN §8 "Feature flag default ON → REJECT".)
- **Reject cross-user actions without affected-user consent.** The default flow REQUIRES the affected user's separate confirmation, with a preview restricted to evidence the affected user is authorized to see. Admin override is OUT OF SCOPE for baseline. (PHASE12_PLAN §2 Cross-User Butler Actions.)
- **Record `inverse_op_payload` BEFORE status transitions to `succeeded`.** If the tool cannot construct an inverse, the action is marked `rollback_kind='not_reversible'` and the confirmation preview MUST surface a "not reversible" warning. (PHASE12_PLAN §8 "inverse_op_payload missing for executable action → REJECT".)

---

## §6. Cost & Rate Envelopes

Butler gets a SEPARATE cost bucket from QA, digests, extraction, and graph projection. Action layers have a higher blast radius than passive synthesis, so the per-user / per-chat caps are stricter than `/recall`.

### 6.1 LLM budget envelopes

```
BUTLER_DAILY_USD_CEILING            default Decimal("1.00")  -- per chat per day
BUTLER_PER_USER_DAILY_USD_CEILING   default Decimal("0.20")  -- per user per day
BUTLER_PER_ACTION_USD_CEILING       default Decimal("0.10")  -- per individual plan call
BUTLER_MONTHLY_USD_CEILING          default Decimal("10.00") -- per chat per month
```

Enforced via `llm_usage_ledger` SUM filtered by `call_type IN ('butler_decision', 'butler_summary')`. Independent of:

- Phase 5 shared `LLM_DAILY_USD_CEILING` ($5/day shared bucket for QA + extraction)
- Phase 7 `DIGEST_DAILY_USD_CEILING`
- Phase 8 `DIGEST_WEEKLY_USD_CEILING`
- Phase 10 `GRAPH_PROJECTION_DAILY_USD_CEILING` ($2/day)
- Phase 10 `GRAPH_PROJECTION_RUN_USD_CEILING` ($0.50/run)

Total LLM exposure across all phases (when Butler is enabled): $5 (QA/extraction) + $1 (daily digest) + $5 (weekly digest, default) + $2 (graph) + $1 (butler) = $14/day worst-case per chat — well within an early-stage operating envelope. Each bucket aborts independently.

### 6.2 Rate envelopes

```
BUTLER_PER_USER_DAILY_ACTIONS       default 10  -- planning calls per user per day
BUTLER_PER_USER_DAILY_EXECUTIONS    default 5   -- confirmed actions per user per day
BUTLER_PER_CHAT_DAILY_ACTIONS       default 50
BUTLER_PER_TOOL_HOUR_LIMIT          default {
    "send_intro": 3,         -- cross-user has higher blast radius
    "update_intro": 5,
    "schedule_meeting": 5,
    "suggest_card_creation": 10,
    "recall_evidence": 30,
}
```

Rate-limit storage: a new `butler_rate_buckets` table (or reuse of the existing rate-limit primitive if one was added in Phase 6+). DESIGN-ONLY — the choice between bucket-table vs in-memory cache vs Redis is one of the surfaced Open Questions (§10).

### 6.3 No hidden retries

A single Butler action invokes the LLM exactly once. A single tool invocation creates exactly one `butler_tool_invocations` row. If a Telegram call returns a recoverable error, the row records the error and the action transitions to `execution_failed`. The user explicitly re-requests if they want a retry — the system does not silently retry.

---

## §7. Phase 11 Binding Tests (proposed — implement at Phase 12 execution time)

This section proposes the next contiguous chunk of the Phase 11 binding suite. **Current end-of-line for Phase 11 is L10c / C9 / I8e / R7.d / G2** (Phase 10 plan, §10). Phase 9 added the L9 / C8 / I7 / R6 / G1 family. Phase 12 starts at L11 / C10 / I9 / R8 / G3.

These tests do NOT exist today. They are the binding contract for a future Phase 12 implementation cycle, to be authored at execution time as part of T12-09 (`PHASE12_PLAN.md §7`).

### 7.1 Leakage family — L11

| Test ID | What it asserts |
|---|---|
| L11.a | A `#offrecord` source row cannot appear in any Butler `EvidenceContext`, `butler_actions.evidence_ids`, or any Telegram preview/outgoing payload — covered by upstream Phase 4 governance, re-asserted here at the Butler boundary. |
| L11.b | A `#nomem` source row cannot appear in any Butler `EvidenceContext` or outgoing payload. |
| L11.c | A forgotten message_version (active `forget_events` row) cannot reach the Butler. If a forget event fires while a Butler action is in `pending_confirmation`, the action transitions to `expired` or `rejected` and the inline keyboard fails closed. |
| L11.d | A redacted message_version's body must not appear in Butler outgoing text, intro draft, or follow-up correction — even if the metadata (chat_id, message_id) remains in audit. |
| L11.e | Butler preview shown to an affected user must not include evidence outside that user's visibility scope. (Cross-user privacy floor.) |

### 7.2 Citations family — C10

| Test ID | What it asserts |
|---|---|
| C10.a | Every executed `butler_actions` row has `evidence_ids` resolving to at least one live `message_versions.id` OR approved `card_sources.id`. No empty-citation executions. |
| C10.b | Citations preserved across undo: an undo (`parent_action_id`) row inherits the original `evidence_context_hash` so audit is replayable. |
| C10.c | Butler intro text token `[^mv:<n>]` resolves to a non-redacted, non-forgotten `message_versions.id`. (Mirrors Phase 9 C8 wiki citation contract.) |

### 7.3 Forget cascade family — I9

| Test ID | What it asserts |
|---|---|
| I9.a | `forget_event` on a cited `message_version_id` redacts the corresponding Butler outgoing text in `butler_tool_invocations.response_payload` (Telegram-side message is redacted by `update_intro` follow-up correction). |
| I9.b | `forget_event` on a cited `card_sources.id` marks dependent `butler_actions.status='rejected'` if still pending, or triggers a follow-up correction if already executed. |
| I9.c | `_cascade_butler_actions` runs in the correct order — AFTER `graph_nodes`, BEFORE `card_sources` (direct assertion on `CASCADE_LAYER_ORDER`). |
| I9.d | Butler audit rows persist with masked payload after cascade — the row exists but `result_payload.text == '[CONTENT_REDACTED: forget_event_id={n}]'`. |
| I9.e | A pending Butler action whose source becomes forgotten while in `pending_confirmation` transitions to `expired` or `rejected` and the inline keyboard's preview hash check fails closed. |
| I9.f | `butler_tool_invocations.idempotency_key UNIQUE` constraint holds — no double execution if the cascade fires mid-execution. |

### 7.4 Refusal family — R8

| Test ID | What it asserts |
|---|---|
| R8.a | A non-member invoking `/butler` is rejected at the handler layer — no `EvidenceContext` call, no LLM call, no `butler_actions` row created. (Membership gate.) |
| R8.b | The Butler refuses to plan when `EvidenceContext` is empty — explicit refusal, not a hallucinated action. |
| R8.c | The Butler refuses cross-user actions when affected-user consent is absent — even after requester confirmation. |
| R8.d | The Butler refuses to act on graph_query results while a `graph_purge_pending` row touches any node in the result — Phase 10 R7.d binding extended to Butler reads. |
| R8.e | The Butler refuses to execute a `pending_confirmation` action whose TTL has expired. |
| R8.f | The Butler refuses a plan that emits a tool name not in `ALLOWED_BUTLER_TOOLS`. |
| R8.g | The Butler refuses a plan whose args fail schema validation — fields out of bounds, types mismatched, required missing. |

### 7.5 Drift / invariant-binding family — G3

| Test ID | What it asserts |
|---|---|
| G3.a | Butler graph_query consumption respects 2-hop traversal limit (Phase 10 graph_query default). The Butler MUST NOT request deeper traversal. |
| G3.b | `butler_actions.evidence_context_hash` is stable across replays — a recomputed hash over the same context produces the same value. |
| G3.c | No `butler_actions` row exists without a corresponding `llm_usage_ledger` row (link integrity). |
| G3.d | No `butler_tool_invocations` row exists for a tool name not in `ALLOWED_BUTLER_TOOLS` (whitelist drift detection). |

### 7.6 Test count summary

Adding L11 (5) + C10 (3) + I9 (6) + R8 (7) + G3 (4) = **25 new binding tests**.

Phase 11 binding suite at end of Phase 9 = 60/60. After Phase 10 lands its 18 tests (per PHASE10_PLAN §10) = 78/78. After Phase 12 execution (whenever authorized) = 103/103.

---

## §8. Rollout (DESIGN-ONLY — no execution gates set)

Phase 12 execution, when authorized, is recommended to ship in four substeps. Each substep behind its own feature flag, all defaulting OFF. No substep ships without the prior substep's binding tests green.

### 8.1 Phase 12.1 — schema + audit + no-op planning

- Migrations: `butler_actions`, `butler_tool_invocations`, `butler_action_confirmations` schema; `call_type` enum extension.
- Service: `bot/services/butler.py` orchestrator + `recall_evidence` tool only.
- Handler: `/butler <request>` returns ONLY an `EvidenceContext` preview — no other tools available.
- Tests: L11.a/b/c, C10.a, I9.a/c/d, R8.a/b/d, G3.b/c.
- Flag: `memory.butler.enabled` default OFF.
- Acceptance: user can ask Butler to recall evidence; nothing else.

### 8.2 Phase 12.2 — schedule_meeting (low-risk single-chat tool)

- Add `schedule_meeting` tool implementation.
- Per-action confirmation flow + inline keyboard.
- TTL worker: 15 min for low-risk meeting proposals.
- Tests: R8.e (TTL expiry), R8.f (whitelist), R8.g (schema), C10.b (citation preservation across undo).
- Flag: `memory.butler.schedule_meeting.enabled` default OFF (gated by parent `memory.butler.enabled`).
- Acceptance: user can propose a meeting in their own chat with explicit confirmation.

### 8.3 Phase 12.3 — send_intro + update_intro + cross-user consent

- Add `send_intro` + `update_intro` tools.
- Affected-user confirmation flow (B receives consent prompt).
- TTL: 5 min for cross-user actions (stricter than meeting).
- Rate limits per §6 enabled here for the first time.
- Tests: L11.d/e (redaction + cross-user visibility), R8.c (no-consent refusal), I9.b/e/f (forget cascade for active intros).
- Flag: `memory.butler.send_intro.enabled` + `memory.butler.update_intro.enabled` default OFF.
- Acceptance: A can request an intro to B around topic X; B confirms; intro is sent; either side can request `/butler_undo`.

### 8.4 Phase 12.4 — suggest_card_creation + admin-review integration

- Add `suggest_card_creation` tool — writes a pending row to the Phase 6 admin-review queue.
- The Butler MUST NOT activate cards directly (Phase 6 invariant preserved).
- TTL: 30 min for admin-review suggestions (longer because review is async).
- Tests: G3.a (graph 2-hop), G3.d (whitelist drift), C10.c (intro citation token format).
- Flag: `memory.butler.suggest_card.enabled` default OFF.
- Acceptance: Butler can propose a card; Phase 6 admin approves or rejects via existing flow; activation goes through Phase 6.

### 8.5 Out of baseline (Phase 12.5+)

The following are explicitly OUT of baseline Phase 12 scope and require separate authorization:

- Reminders / scheduled triggers (cron-fired Butler actions).
- Proactive nudges (event-fired Butler actions).
- Follow-up summary delivery (Butler-initiated digest extension).
- Admin override of cross-user consent.
- Session-wide opt-in.
- Dry-run flag (`/butler --dry-run`) — see PHASE12_PLAN §11.3 for the hardening note.
- Operator dashboard surface — PHASE12_PLAN §5.G "out of scope for baseline implementation".

---

## §9. Carryover from Phase 0-11

The Butler is a **consumer**, not an extender, of Phase 0-11 capabilities. The mapping:

| Existing capability | Phase | Butler's relationship |
|---|---|---|
| Gatekeeper auth + membership check | Phase 0 | Membership gate at `/butler` handler (R8.a). |
| `feature_flags` | Phase 1 | All Butler sub-flags follow the `memory.butler.*` namespace. |
| `message_versions` + citations | Phase 1 | Citation anchor for `evidence_ids`. (Invariant #4.) |
| `#nomem` / `#offrecord` detection | Phase 3 | Governance filter pre-LLM. (Invariant #3.) |
| `forget_events` + cascade | Phase 3 + ongoing | `CASCADE_LAYER_ORDER` extension for new audit tables. (Invariant #9.) |
| FTS + hybrid search | Phase 4 | Underpins `EvidenceContext` retrieval. |
| Q&A evidence bundle | Phase 4 | Direct input to `recall_evidence` tool. |
| `llm_gateway` + `llm_usage_ledger` | Phase 5 | Sole LLM boundary (Invariant #2). `call_type='butler_decision'` / `'butler_summary'`. |
| Budget guard (per-day caps) | Phase 5 | Pattern reused with separate `BUTLER_*_USD_CEILING` env vars. |
| Knowledge cards + admin review | Phase 6 | `suggest_card_creation` writes to existing admin-review queue. |
| Forget cascade (cards / sources) | Phase 6 | Butler layers slot in after `card_sources`. |
| Daily / weekly digests (read-only) | Phase 7/8 | Evidence context may include digest references. |
| Wiki revisions (read-only) | Phase 9 | Evidence context may cite member-internal wiki content. |
| Two-password auth | Phase 9 | N/A — Butler is Telegram-only; no web surface. |
| Graph query (read-only, admin-only) | Phase 10 | Evidence context may include graph traversal results, admin-scope only (R7.a is binding). |
| Pending-purge read-block | Phase 10 | Reused: Butler refuses if any graph node is purge-pending (R8.d). |
| Phase 11 binding suite + privacy lint | Phase 11 | L11 / C10 / I9 / R8 / G3 family appended to existing 78/78 (post-Phase 10). |

What the Butler does NOT carry forward:

- No new schedulers (baseline). Phase 7/8 schedulers exist for digests; no `butler_*_job` cron in baseline.
- No new web surface. Phase 9 wiki / admin web stays as-is.
- No new graph projection. Phase 10 graph stays as-is.
- No new LLM provider. The same `llm_gateway` configuration applies.
- No new auth role. Membership + admin are sufficient.

---

## §10. Open Questions (must be resolved before Phase 12 execution authorization)

The questions below were surfaced after Phase 9 closure and were NOT resolved by `PHASE12_PLAN.md` ratification (2026-05-02). They require explicit team-lead decision before any execution PR is opened.

### 10.1 Surface: DMs only, or also shared chats?

`PHASE12_PLAN §2 Abuse Prevention` says: "DM usage: allowed only for planning / personal preview, not for sending group actions unless target chat is explicit and user is authorized". This implies a baseline of mixed surfaces — DM for planning, group for execution — but does NOT spell out:

- Whether `/butler` in a public community chat is allowed at all in baseline 12.1, or whether the first cut is DM-only with chat-targeting via explicit `chat_id` argument.
- How `send_intro` chooses the destination chat when the requester is in multiple chats (likely answer: the chat where `/butler` was invoked; needs confirmation).

Recommendation: ship Phase 12.1 (recall-only) DM-only. Open group-chat surface in Phase 12.2 once the inline-keyboard UX has been exercised. **Pending team-lead approval.**

### 10.2 Trigger storage — Postgres cron extension, APScheduler with persistence, or no triggers?

Baseline has no triggers (user-initiated only). Phase 12.5+ reminders introduce a per-user, per-time-of-day trigger requirement. Options:

1. **Reuse `AsyncIOScheduler` (Phase 7 scheduler).** Pros: zero new dependency, same UTC/MSK pattern as digests. Cons: scheduler in-memory state lost on restart; would need a `butler_triggers` table for persistence + replay on startup.
2. **`pg_cron` extension.** Pros: persistence is automatic; visible in pg_stat. Cons: Postgres-side function definitions, harder local-dev setup, extension may not be available on the deployment Postgres.
3. **Defer triggers entirely.** Ship reminders as a notebook for a successor phase (Phase 13?).

Recommendation: **defer triggers to Phase 12.5+, ship baseline without them.** Re-examine after Phase 12.1-12.4 produces real usage data. **Pending team-lead approval.**

### 10.3 Per-user opt-in policy

The PHASE12_PLAN baseline says: "community members and admins only; non-member requests: reject without evidence lookup". This is a coarse membership gate. Open question: should there be a per-user opt-in flag (separate from chat-level `memory.butler.enabled`) that lets a community member privately disable Butler for their own evidence?

- **For:** mirrors `/forget_me` pattern; gives users granular control; PHASE9 wiki had a similar self-publish gate.
- **Against:** users already control `#nomem`/`#offrecord` at the message level; per-user-disable is harder to audit (the absence is the signal); doubles the policy surface.

Recommendation: **no per-user opt-in in baseline.** Rely on `#nomem` / `#offrecord` / `/forget` / `/forget_me`. Revisit if usage shows that users want a global-disable button. **Pending team-lead approval.**

### 10.4 Rate-limit storage primitive

§6 specifies per-user / per-chat / per-tool rate buckets. Storage options:

1. New `butler_rate_buckets` table — durable, audit-friendly, slow.
2. In-memory dict (bot-process-local) — fast, lost on restart.
3. Redis — requires new infra dep.

Recommendation: **start with `butler_rate_buckets` table** for durability and audit; add Redis cache later if performance becomes an issue. **Pending team-lead approval.**

### 10.5 Cross-user introduction admin override

PHASE12_PLAN §2 says: "Admin override is out of scope for baseline Phase 12 unless separately authorized". This leaves a small but important hole: can an admin send an intro between two users without those users' consent (e.g. for community moderation context)?

Recommendation: **no admin override in baseline.** Consent is consent. If admins need to broker introductions, they can do so manually outside the Butler surface. **Pending team-lead approval.**

### 10.6 Evidence freshness window

When the Butler plans an action at time T, the `EvidenceContext` is built from data current as of T. The action may not execute until T + (up to 30 min for admin-review suggestions). Open question: should the Butler re-fetch the context at execution time to ensure freshness, or trust the snapshot taken at planning time?

- **Re-fetch on execute:** safer for forget events (re-reads governance state), but invalidates the user's preview (the user confirmed text P, but execution-time evidence might differ).
- **Snapshot on plan:** what the user saw is what gets sent, but a forget event between plan and execute could leak forgotten content.

Recommendation: **snapshot on plan + TTL ≤ 30 min + cascade-aware expiry**. If a forget event fires during the TTL window, the Butler's `_cascade_butler_actions` layer must transition any matching `pending_confirmation` row to `expired` BEFORE Telegram can fire the inline keyboard (I9.e binding). **Pending team-lead approval.**

---

## §11. Non-Negotiable Invariants (verbatim from `ROADMAP.md` / `HANDOFF.md §1`)

For audit clarity. Identical wording to `ROADMAP.md` lines 58-67 and `HANDOFF.md` lines 327-336.

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

**Central for Phase 12:** invariant **#7** is the primary Butler boundary. Invariants **#2, #3, #4, #9** are continuously re-asserted at the Butler tool layer. Invariant **#10** is unaffected — the Butler is Telegram-only with no web surface.

---

## §12. References

- **`docs/memory-system/PHASE12_PLAN.md`** — the detailed, ratified per-component design contract (2026-05-02). This DESIGN.md complements PLAN.md by adding rollout staging, Phase 11 binding test family extension, and post-Phase 9 open questions. Where they overlap (invariants, architecture, schema), they MUST stay consistent — PLAN.md is authoritative.
- **`docs/memory-system/HANDOFF.md`** — §1 invariants (lines 327-336), §3 Phase 12 row (line 482), §16 risk register row "Butler bypassing governance" (line 1254). The architect's original boundary statement.
- **`docs/memory-system/ROADMAP.md`** — Phase 12 row (line 41); points to this DESIGN.md as the exit gate companion.
- **`docs/memory-system/AUTHORIZED_SCOPE.md`** — confirms Phase 12 is design-only / postponed. Implementation requires explicit amendment.
- **GitHub issue #116** — `EPIC: Phase 12 — Future butler action layer (design only / postponed)`.
- **`docs/memory-system/PHASE5_PLAN.md` §3** — Phase 5 `llm_gateway` surface, `call_type` ledger discriminator pattern, budget envelope precedent.
- **`docs/memory-system/PHASE9_PLAN.md` §10** — Phase 11 binding test naming convention (L9 / C8 / I7 / R6 / G1 family).
- **`docs/memory-system/PHASE10_PLAN.md` §5.F + §13.5** — pending-purge read-block pattern (RFC-001:415); reused by R8.d.
- **`docs/memory-system/PHASE10_PLAN.md` §15** — separate cost-bucket precedent (`GRAPH_PROJECTION_DAILY_USD_CEILING`); reused for `BUTLER_*_USD_CEILING`.
- **`docs/memory-system/ORCHESTRATOR_REGISTRY.md` §2** — migration counter reservations; Phase 12 must NOT claim migration numbers until authorization is granted.

---

## §13. Status & Next Steps

**Phase 12 is design-only.** No code, no migrations, no tests, no scheduler entries. This file plus `PHASE12_PLAN.md` are the entire Phase 12 deliverable.

To move Phase 12 from design to execution, the following must happen (NONE of which is authorized today):

1. Team-lead resolves §10 Open Questions in writing.
2. `AUTHORIZED_SCOPE.md` is amended to add a `## Authorized: Phase 12 — Future Butler (YYYY-MM-DD)` block.
3. `ORCHESTRATOR_REGISTRY.md` §2 reserves a migration-counter window for Phase 12 (after Phase 10's 060-064 window closes).
4. The execution Sprint 0 ticket (T12-S0) updates `IMPLEMENTATION_STATUS.md` and opens the substep-1 (Phase 12.1) implementation cycle.
5. The first execution PR honours every binding contract in `PHASE12_PLAN §11.4` row 1-9.

Until then, this document is a tombstone for the boundary — exactly as `HANDOFF.md §3` Phase 12 row prescribes.

<!-- updated-by-superflow:2026-05-22 -->
