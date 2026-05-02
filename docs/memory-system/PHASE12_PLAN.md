# Phase 12 — Butler / Action Execution for Shkoderbot Memory System: Ratified Design

## §0. Banner

✅ RATIFIED — DESIGN ONLY

🚫 NO IMPLEMENTATION AUTHORIZED (per `AUTHORIZED_SCOPE.md` §"Phase 12 — Butler design docs only (2026-04-30)" and §"NOT authorized")

**Status:** ratified design contract; future butler implementation requires explicit AUTHORIZED_SCOPE.md update AND human team-lead approval.
**Cycle:** Memory system Phase 12.
**Date:** 2026-04-30 (drafted) → 2026-05-02 (ratified, Orchestrator B sprint 0a).
**Owner:** Orchestrator B per `docs/memory-system/ORCHESTRATOR_REGISTRY.md §1`.
**Predecessors (gates that must close before any §6 implementation work begins):**
Phase 4 evidence / Q&A (CLOSED 2026-04-30), Phase 5 LLM gateway + ledger (Orchestrator A active), Phase 6 cards as suggestions (Orchestrator A blocked), Phase 8 observations / digest context (Orchestrator A blocked).
**Critical invariant for this phase:** Future butler cannot read raw DB directly; it must use governance-filtered evidence context (HANDOFF.md §1 invariant 7).

### Ratification Note (2026-05-02, Orchestrator B)

This document was promoted from `docs/memory-system/prompts/PHASE12_PLAN_DRAFT.md`
(merged via PR #160 as a docs-only artifact) to its canonical location
`docs/memory-system/PHASE12_PLAN.md` per `AUTHORIZED_SCOPE.md` line 111 and
`ORCHESTRATOR_REGISTRY.md §2 Orchestrator B exclusive write`. The substance of §1–§10
was preserved verbatim from the ratified draft. The following changes were applied
during ratification:

- §0 banner switched from "DRAFT — NOT AUTHORIZED" to "RATIFIED — DESIGN ONLY".
- §0 source-reading-notes pruned: gaps about a missing `PHASE4_PLAN.md` and missing
  `prompts/` directory are obsolete (Phase 4 closed 2026-04-30; `prompts/` exists).
- §11 "Compliance Recap" added — explicit cross-reference index for permission model,
  action audit, abort / dry-run, and the future butler boundary as required by the
  Orchestrator B charter directive.
- §Final Report Block updated: `DRAFT_PATH` → `CANONICAL_PATH`, `STATUS: RATIFIED`,
  ratification date stamped.
- §6 Wave / §7 Tickets / §9 PR Workflow remain authored as **future** implementation
  contracts. They MUST NOT be picked up until `AUTHORIZED_SCOPE.md` is updated by
  human or by Orchestrator A's Phase 6+ closing PR.

### Source Reading Notes (verified 2026-05-02)

Required source files read during draft authoring AND verified at ratification time:

- `docs/memory-system/HANDOFF.md` — invariants (§1), roadmap, Phase 12 design-only spec
  (§ "Phase 12 — future butler action layer (design only / postponed)"), service
  boundaries, risk register entry "Butler bypassing governance". Verified present and
  consistent at HEAD on 2026-05-02.
- `docs/memory-system/AUTHORIZED_SCOPE.md` — confirms Butler / action execution is
  Phase 12, postponed, design only. Verified at HEAD on 2026-05-02.
- `docs/memory-system/ROADMAP.md` — confirms Phase 12 exit gate is docs only / no
  execution code.
- `docs/memory-system/ARCHITECTURE.md`, `docs/memory-system/GLOSSARY.md`, ADR-0003,
  ADR-0004, ADR-0005 — used for governance, LLM gateway, graph / butler boundary.
- `docs/memory-system/PHASE4_PLAN.md` — present at HEAD post Phase 4 closure; structural
  model alignment confirmed.

The historical `prompts/PHASE4_PLAN.md` reference (used by the original draft author
because the canonical Phase 4 plan was not yet in the main worktree) is now obsolete
and intentionally not preserved.

---

## §1. Invariants Verbatim

Non-negotiable invariants from `HANDOFF.md §1`:

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

### Phase 12 Binding Interpretation

- Invariant 2 means the Butler planner may use an LLM only through `bot/services/llm_gateway.py`; direct provider SDK usage is forbidden.
- Invariant 3 means Butler evidence context must exclude `#nomem`, `#offrecord`, redacted, forgotten, tombstoned, and unauthorized content before it reaches the LLM gateway.
- Invariant 4 means every Butler decision that depends on memory must cite `message_version_id` or approved card sources inside the action audit.
- Invariant 7 is the primary Butler boundary: `bot/services/butler.py` and every tool under `bot/services/butler_tools/` receive an `EvidenceContext` object, never raw `telegram_updates`, raw `chat_messages`, arbitrary SQL rows, or graph rows.
- Invariant 9 means rollback / undo never deletes audit history. Undo writes a new audit row linked to the original action.

---

## §2. Phase 12 Spec — Detailed Butler Capability Design

### HANDOFF Phase 12 Spec

- **Objective:** preserve extension points only.
- **Scope:** design notes, **no code execution**.
- **Acceptance:** team has documented boundary for future butler.
- **No-go:** no butler code shipped.

### Product Objective

The Butler is a constrained action agent for the Shkoderbot memory system. It helps a user turn governed community memory into explicit, auditable actions, for example:

- schedule a meeting proposal inside Telegram;
- send a Telegram intro message between consenting members;
- update an existing intro with newer approved context;
- recall governed evidence before proposing an action;
- suggest creating or updating a knowledge card, without directly activating the card.

The Butler is not an autonomous operator. It plans candidate actions, asks for confirmation, executes only whitelisted tools, records every decision and tool invocation, and provides a per-action rollback path where technically possible.

### Core Capability Set

Allowed Phase 12 tools are intentionally tiny:

1. `recall_evidence`
   - Purpose: fetch governance-filtered evidence context using the Phase 4 / Phase 5 evidence path.
   - Output: `EvidenceContext` with evidence item ids, citations, visibility scope, and redaction metadata.
   - Hard rule: this tool is the only memory-read tool available to the Butler.

2. `schedule_meeting`
   - Purpose: create a Telegram-native meeting proposal message or reminder prompt.
   - Scope: no Google Calendar, no email, no external calendar API in Phase 12 baseline.
   - Output: Telegram message id, proposed participants, proposed time window, confirmation state.

3. `send_intro`
   - Purpose: send a Telegram introduction message after all required confirmations.
   - Scope: Telegram send/edit/delete operations only, via strict bot API wrapper.
   - Output: delivered Telegram message id and intro payload hash.

4. `update_intro`
   - Purpose: edit a Butler-created intro message or post a follow-up update when editing is unavailable.
   - Scope: only messages previously created by Butler and recorded in `butler_actions`.
   - Output: edited message id or follow-up message id.

5. `suggest_card_creation`
   - Purpose: create an admin-review suggestion for a knowledge card.
   - Scope: proposal only; cannot create an active card and cannot bypass Phase 6 admin review.
   - Output: pending candidate id or admin queue item id.

### Action Lifecycle

Every Butler request follows one state machine:

```
requested
  -> evidence_loaded
  -> planned
  -> pending_confirmation
  -> confirmed
  -> executing
  -> succeeded
  -> undo_pending
  -> undo_succeeded
```

Failure states:

```
rejected
expired
execution_failed
undo_failed
cancelled
```

State rules:

- `requested`: user invokes `/butler`.
- `evidence_loaded`: Butler has only governance-filtered evidence context.
- `planned`: LLM gateway returns a tool-use plan with strict schema.
- `pending_confirmation`: no external action has happened yet.
- `confirmed`: user clicked explicit inline keyboard confirmation.
- `executing`: tool invocation transaction started.
- `succeeded`: tool succeeded and audit is complete.
- `undo_pending`: user invoked `/butler_undo <action_id>`.
- `undo_succeeded`: inverse operation executed or best-effort undo recorded.
- `expired`: pending action TTL passed before confirmation.
- `rejected`: whitelist, schema, authorization, hallucinated args, or governance validation failed.

### LLM Gateway Contract

Butler calls the LLM only through Phase 5 `llm_gateway`.

Gateway input:

- caller: `butler`;
- user id / chat id;
- request text;
- `EvidenceContext` from `recall_evidence`;
- whitelist manifest version;
- allowed tool schemas;
- budget context;
- required output schema.

Gateway output must be a structured `ButlerPlan`:

```json
{
  "plan_summary": "string",
  "evidence_ids": [123],
  "actions": [
    {
      "tool_name": "send_intro",
      "args": {},
      "requires_confirmation": true,
      "affected_user_ids": [111, 222],
      "risk_level": "low|medium|high",
      "rollback_kind": "delete_message|edit_message|followup_correction|not_reversible",
      "inverse_op_payload": {}
    }
  ]
}
```

Strict validation happens before user confirmation:

- `tool_name` must exist in the whitelist.
- `args` must validate against the tool schema.
- all referenced evidence ids must be present in `EvidenceContext`.
- no raw DB ids except citation anchors and approved card source ids.
- no arbitrary URL fetches, arbitrary Telegram methods, payment actions, calendar APIs, email APIs, shell commands, file writes, or admin-only database mutations.
- missing required fields reject the plan before confirmation.

### User Confirmation Default

Default is per-action confirmation, not session-wide opt-in.

Rationale:

- Butler actions can affect other users.
- A multi-action plan can contain mixed risk levels.
- Audit quality is simpler when each action has a distinct confirmation row.
- Session-wide opt-in can be revisited only after Phase 12 has production evidence and stronger policy controls.

Confirmation UX:

- Butler sends a preview card for each action.
- The preview includes action type, target chat, affected users, source citations, exact outgoing text if any, expiry time, and undo availability.
- Inline buttons: `Confirm`, `Cancel`, `Edit request`.
- For cross-user actions, secondary confirmation buttons are sent to affected users where required.

### Action TTL

Pending actions expire automatically.

Default TTL proposal: 15 minutes for low-risk actions, 5 minutes for cross-user intro sends, 30 minutes for admin-only card suggestions. Expired actions move to `expired` and cannot be executed; the user must request a fresh plan.

Reasoning:

- prevents stale context from being executed later;
- limits damage from old inline keyboards;
- keeps LLM-derived arguments tied to a current evidence snapshot.

### Cross-User Butler Actions

Example: user A asks Butler to introduce A to user B.

Default flow:

1. A invokes `/butler introduce me to B about X`.
2. Butler loads governance-filtered evidence that A is allowed to see.
3. Butler prepares an intro draft.
4. A confirms the request.
5. B receives a consent prompt with exact intro text and citations that B is allowed to see.
6. If B confirms, Butler sends the intro.
7. If B rejects or TTL expires, action becomes `rejected` or `expired`.

Admin override is out of scope for baseline Phase 12 unless separately authorized.

### Cost Ceiling

Butler gets a separate cost and rate budget from `/recall`.

Baseline:

- per-user daily Butler LLM budget;
- per-chat daily Butler budget;
- per-action max tokens;
- no hidden retries;
- failed validation still writes a lightweight audit row and ledger entry if the LLM was called.

Butler budget is stricter than Q&A because actions have higher blast radius.

### Abuse Prevention

Who can invoke Butler:

- baseline: community members and admins only;
- DM usage: allowed only for planning / personal preview, not for sending group actions unless target chat is explicit and user is authorized;
- non-member requests: reject without evidence lookup;
- admins can see more admin-only evidence only if the evidence context service marks it visible for admin scope.

Rate limiting:

- per user: small burst, low daily cap;
- per chat: aggregate cap;
- per tool: stricter caps for `send_intro` and `update_intro`;
- repeated rejection / validation failures trigger cooldown.

Abuse controls:

- exact outgoing message preview before confirmation;
- cross-user consent for affected users;
- no arbitrary recipient ids unless resolved from allowed member directory;
- no private data in confirmation preview unless that user is authorized to see it.

### Open Design Questions Addressed

1. User confirmation default — choose per-action confirmation. Session-wide opt-in is not part of baseline Phase 12.
2. Tool whitelist scope — start tiny with exactly five tools: `schedule_meeting`, `send_intro`, `update_intro`, `recall_evidence`, `suggest_card_creation`.
3. Action TTL — auto-expire pending Butler actions. Baseline defaults: 15 minutes for low-risk actions, 5 minutes for cross-user intro sends, 30 minutes for admin-review suggestions.
4. Rollback semantics — best-effort, not guaranteed. Telegram delivery cannot be fully undone; audit must distinguish delete/edit/follow-up correction/not reversible.
5. Cross-user Butler actions — affected user confirmation is required by default. A can request an intro to B, but B must confirm before the intro is sent.
6. Cost ceiling — separate from `/recall`. Butler gets a stricter per-user and per-chat daily budget because it can execute actions.
7. Abuse prevention — members/admins only by default, per-user and per-chat rate limits, stricter caps on send/update tools, cooldown on repeated rejects.

---

## §3. Out-of-Scope

Phase 12 baseline must not include:

- autonomous unattended actions;
- session-wide unattended execution;
- money handling, payments, invoices, subscriptions, financial transfers, crypto, or purchasing;
- arbitrary external API calls;
- Google Calendar, email, CRM, webhook, browser, shell, filesystem, or HTTP tools;
- Telegram methods outside the whitelisted bot wrapper methods required by the five tools;
- arbitrary SQL or raw DB reads;
- direct graph reads as source of truth;
- LLM calls outside `llm_gateway`;
- direct creation of approved knowledge cards;
- public wiki publishing;
- admin override of user consent by default;
- sending messages to users who have not consented where the action affects them directly;
- hidden retries;
- best-effort execution without audit.

Allowed Telegram surface is intentionally narrow:

- send message;
- edit Butler-created message;
- delete Butler-created message when rollback requires it and Telegram permits it;
- answer callback query;
- send inline keyboard confirmation prompts.

---

## §4. Architecture Overview

```
Telegram /butler command
        |
        v
+-----------------------------+
| bot/handlers/butler.py      |
| authz + request parse       |
+--------------+--------------+
               |
               v
+-----------------------------+
| bot/services/butler.py      |
| orchestrator                |
| - creates butler_actions    |
| - asks recall_evidence      |
| - calls llm_gateway         |
| - validates tool schema     |
+--------------+--------------+
               |
               v
+--------------------------------------------------+
| Governance-filtered Evidence Context             |
| from Phase 4/5 evidence services only            |
| - message_version_id citations                   |
| - approved card source ids                       |
| - visibility scope                               |
| - no raw DB payloads                             |
+--------------+-----------------------------------+
               |
               v
+--------------------------------------------------+
| LLM Gateway with Butler tool-use schema          |
| - caller='butler'                                |
| - llm_usage_ledger write                         |
| - budget guard                                   |
| - structured ButlerPlan                          |
+--------------+-----------------------------------+
               |
               v
+--------------------------------------------------+
| Butler tool layer (strict whitelist)             |
| {                                                |
|   recall_evidence,                               |
|   schedule_meeting,                              |
|   send_intro,                                    |
|   update_intro,                                  |
|   suggest_card_creation                          |
| }                                                |
+--------------+-----------------------------------+
               |
               v
+--------------------------------------------------+
| User confirmation flow per action                |
| - inline keyboard                                |
| - exact preview                                  |
| - affected-user consent                          |
| - action TTL                                     |
+--------------+-----------------------------------+
               |
               v
+--------------------------------------------------+
| Execution transaction                            |
| - butler_tool_invocations row                    |
| - whitelisted Telegram wrapper call              |
| - output payload hash                            |
| - inverse_op_payload stored                      |
+--------------+-----------------------------------+
               |
               v
+--------------------------------------------------+
| Audit tables                                     |
| - llm_usage_ledger                               |
| - butler_actions                                 |
| - butler_action_confirmations                    |
| - butler_tool_invocations                        |
+--------------+-----------------------------------+
               |
               v
+--------------------------------------------------+
| Rollback capability per action                   |
| /butler_undo <action_id>                         |
| - validates actor                                |
| - reads inverse_op_payload                       |
| - executes best available inverse                |
| - writes linked undo action                      |
+--------------------------------------------------+
```

Required boundary chain, shown in the exact control order this design enforces:

```
Butler tool layer (strict whitelist)
        |
        v
LLM gateway with tool-use schema
        |
        v
User confirmation flow per action
        |
        v
butler_actions audit table
        |
        v
Rollback capability per action
```

Important boundary: the Butler tool layer is not allowed to fetch memory itself. `recall_evidence` is a tool in name for the planner, but its implementation delegates to the governed evidence service and returns the sealed `EvidenceContext` envelope.

---

## §5. Components

### 5.A. DB Schema: `butler_actions`, `butler_tool_invocations`, `butler_action_confirmations`

#### `butler_actions`

Purpose: one row per planned or executed Butler action.

Proposed columns:

```sql
CREATE TABLE butler_actions (
  id BIGSERIAL PRIMARY KEY,
  action_uuid UUID NOT NULL UNIQUE,
  parent_action_id BIGINT REFERENCES butler_actions(id),
  requester_tg_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  action_type TEXT NOT NULL,
  status TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_manifest_version TEXT NOT NULL,
  evidence_context_hash TEXT NOT NULL,
  evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  approved_card_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  plan_summary TEXT NOT NULL,
  action_args JSONB NOT NULL,
  action_args_hash TEXT NOT NULL,
  result_payload JSONB,
  result_payload_hash TEXT,
  inverse_op_payload JSONB,
  rollback_kind TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  requires_confirmation BOOLEAN NOT NULL DEFAULT true,
  confirmation_policy TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  executed_at TIMESTAMPTZ,
  undone_at TIMESTAMPTZ,
  rejection_reason TEXT,
  error_code TEXT,
  error_context JSONB,
  llm_usage_ledger_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Constraints:

- `tool_name` must be one of the strict whitelist names.
- `status` must be one of the lifecycle states in §2.
- `requires_confirmation = true` by default.
- `evidence_ids` may contain only citation anchors visible in the evidence context.
- `action_args` stores validated structured args, not raw prompt text.
- `inverse_op_payload` is immutable after successful execution except by an explicit linked undo action.

Indexes:

- `(requester_tg_id, created_at DESC)`;
- `(chat_id, created_at DESC)`;
- `(status, expires_at)` for TTL worker;
- `(parent_action_id)`;
- `(llm_usage_ledger_id)`.

#### `butler_tool_invocations`

Purpose: one row per actual tool call attempt.

Proposed columns:

```sql
CREATE TABLE butler_tool_invocations (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT NOT NULL REFERENCES butler_actions(id),
  tool_name TEXT NOT NULL,
  invocation_seq INT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_payload JSONB NOT NULL,
  request_payload_hash TEXT NOT NULL,
  response_payload JSONB,
  response_payload_hash TEXT,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error_code TEXT,
  error_context JSONB
);
```

Rules:

- no hidden retries; each retry, if ever authorized, must create a new invocation row with explicit reason;
- tool implementation cannot execute before its invocation row is created;
- request payload is the validated tool payload, not raw LLM output;
- response payload stores Telegram ids and hashes, not private raw evidence.

#### `butler_action_confirmations`

Purpose: one row per confirmation / rejection event.

Proposed columns:

```sql
CREATE TABLE butler_action_confirmations (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT NOT NULL REFERENCES butler_actions(id),
  confirmer_tg_id BIGINT NOT NULL,
  confirmation_role TEXT NOT NULL,
  status TEXT NOT NULL,
  confirmation_message_chat_id BIGINT,
  confirmation_message_id BIGINT,
  preview_payload_hash TEXT NOT NULL,
  confirmed_at TIMESTAMPTZ,
  rejected_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Roles:

- `requester`;
- `affected_user`;
- `admin`;
- `rollback_requester`.

Status:

- `pending`;
- `confirmed`;
- `rejected`;
- `expired`;
- `cancelled`.

Acceptance:

- migrations apply and rollback cleanly;
- every executed action has at least one confirmation row unless a future explicit policy exception is approved;
- every LLM-planned action links to `llm_usage_ledger`;
- undo creates a linked `butler_actions.parent_action_id` row.

### 5.B. `bot/services/butler.py` Orchestrator

Responsibilities:

- parse high-level Butler request from handler;
- authorize requester;
- create initial `butler_actions` row in `requested`;
- call `recall_evidence` through the governed evidence service;
- call `llm_gateway` with Butler tool schema;
- validate the returned `ButlerPlan`;
- persist planned actions as `pending_confirmation`;
- create confirmation rows;
- execute confirmed actions through the strict tool registry;
- update statuses and audit rows;
- coordinate rollback through `/butler_undo`.

Public API proposal:

```python
class ButlerService:
    async def request_plan(self, session, *, requester_tg_id, chat_id, request_text) -> list[ButlerActionPreview]: ...
    async def confirm_action(self, session, *, action_id, confirmer_tg_id, callback_token) -> ButlerActionResult: ...
    async def cancel_action(self, session, *, action_id, confirmer_tg_id) -> None: ...
    async def undo_action(self, session, *, action_id, requester_tg_id) -> ButlerUndoResult: ...
    async def expire_pending_actions(self, session, *, now) -> int: ...
```

Fail-closed validation:

- unknown tool → reject;
- missing evidence context → reject;
- raw DB accessor attempted → reject;
- expired action → reject;
- actor not authorized → reject;
- cross-user consent missing → reject;
- hallucinated args → reject before confirmation;
- confirmation payload hash mismatch → reject.

### 5.C. Tool Implementations Under `bot/services/butler_tools/`

Tool registry:

```python
ALLOWED_BUTLER_TOOLS = {
    "schedule_meeting": ScheduleMeetingTool,
    "send_intro": SendIntroTool,
    "update_intro": UpdateIntroTool,
    "recall_evidence": RecallEvidenceTool,
    "suggest_card_creation": SuggestCardCreationTool,
}
```

Common tool interface:

```python
class ButlerTool(Protocol):
    name: str
    schema_version: str
    args_model: type[BaseModel]

    async def validate_policy(self, context: ButlerToolContext, args: BaseModel) -> None: ...
    async def execute(self, context: ButlerToolContext, args: BaseModel) -> ButlerToolResult: ...
    async def build_inverse(self, result: ButlerToolResult) -> dict[str, object]: ...
```

Tool constraints:

- no tool accepts arbitrary SQL, arbitrary Telegram method name, arbitrary URL, or raw prompt text;
- every tool receives `EvidenceContext`, not DB session access for memory reads;
- only repository writes required for its own action audit / derived proposal are allowed;
- no tool calls LLM;
- no tool calls external APIs beyond the Telegram wrapper methods explicitly needed.

Tool-specific notes:

- `recall_evidence`: returns sealed context; no side effects except audit.
- `schedule_meeting`: posts a proposal message with confirmed participants; rollback deletes or edits the proposal if Telegram permits.
- `send_intro`: sends exact confirmed text; rollback deletes the message if permitted or sends a correction if not.
- `update_intro`: edits only Butler-owned intro messages; rollback restores prior text if available or posts correction.
- `suggest_card_creation`: writes a pending review suggestion; rollback marks suggestion cancelled, never deletes audit.

### 5.D. Telegram `/butler` Command + Interactive Confirmation UI

Commands:

- `/butler <request>` — create Butler plan and previews.
- `/butler_status <action_id>` — show action state and audit summary.
- `/butler_cancel <action_id>` — cancel pending action.
- `/butler_undo <action_id>` — request rollback / inverse action.

Interactive UI:

- inline keyboard per action;
- buttons: `Confirm`, `Cancel`, `Edit request`;
- affected-user prompts for cross-user actions;
- callback data contains opaque action token, not raw args;
- preview payload hash stored in `butler_action_confirmations`.

Message preview must include:

- action type;
- exact outgoing text for message-sending tools;
- target chat / target users;
- evidence citations;
- TTL / expiry;
- undo availability;
- "not reversible" warning when applicable.

No confirmation preview may reveal evidence the confirmer is not authorized to see.

### 5.E. Rollback Path: `inverse_op_payload` and `/butler_undo <action_id>`

Every action records `inverse_op_payload` before status becomes `succeeded`.

Rollback categories:

- `delete_message`: delete Butler-created Telegram message if permitted.
- `edit_message`: restore prior text of a Butler-owned message.
- `followup_correction`: send a correction / retraction message when deletion or edit is unavailable.
- `cancel_pending`: cancel a pending internal review suggestion.
- `not_reversible`: record undo request and explain why no technical inverse exists.

Rollback semantics are best-effort, not guaranteed, because Telegram delivery cannot be undone in all cases. The audit must make this explicit.

Undo rules:

- only requester, affected user, or admin can request undo depending on action type;
- undo creates a linked Butler action with `parent_action_id`;
- original action audit remains immutable;
- undo writes its own tool invocation rows;
- if Telegram deletion fails, fallback is `followup_correction` if safe.

### 5.F. Evidence Context Service Contract

Butler consumes a sealed `EvidenceContext` produced by Phase 4 / Phase 5 evidence services.

Required shape:

```json
{
  "context_id": "uuid",
  "context_hash": "sha256",
  "visibility_scope": "member|admin|self",
  "items": [
    {
      "source_type": "message_version|approved_card",
      "source_id": 123,
      "chat_id": -100123,
      "message_id": 456,
      "snippet": "string",
      "source_date": "iso8601",
      "policy": "normal",
      "is_redacted": false
    }
  ]
}
```

Contract:

- service enforces governance filters before returning data;
- service returns only snippets / approved card summaries needed for planning;
- service never returns raw `telegram_updates.raw_json`;
- service never returns `#nomem`, `#offrecord`, forgotten, or unauthorized content;
- context hash is stored on every Butler action.

### 5.G. Audit, Ledger, and Observability

Every Butler LLM call:

- goes through `llm_gateway`;
- writes `llm_usage_ledger`;
- records caller `butler`;
- records action ids or request correlation id;
- records token / cost budget outcome.

Every Butler action:

- writes `butler_actions`;
- writes `butler_action_confirmations`;
- writes `butler_tool_invocations` for each tool call;
- stores evidence ids and context hash;
- stores result hash and inverse payload;
- emits structured logs with action id, tool name, status, but never secrets or raw private evidence.

Dashboards / operator views are out of scope for baseline implementation, but the schema must support later read-only admin inspection.

---

## §6. Streams — 3 Waves of Parallel Work

### Wave 1 — Foundations in Parallel

| Stream | Ticket | Scope | Deps |
|---|---|---|---|
| A | T12-01 | Butler audit schema migrations and repos | Phase 5 ledger schema exists |
| B | T12-02 | EvidenceContext contract and `recall_evidence` integration | Phase 4 evidence bundle, governance filters |
| C | T12-03 | Tool registry schemas and whitelist manifest | none beyond Phase 12 authorization |

### Wave 2 — Orchestration and UI

| Stream | Ticket | Scope | Deps |
|---|---|---|---|
| D | T12-04 | `bot/services/butler.py` planning / validation state machine | T12-01, T12-02, T12-03, Phase 5 gateway |
| E | T12-05 | Telegram `/butler` command and confirmation UI | T12-04 |
| F | T12-06 | Implement first tiny tools: `schedule_meeting`, `send_intro`, `update_intro`, `suggest_card_creation` | T12-03, T12-04 |

### Wave 3 — Rollback, Abuse Controls, and Evals

| Stream | Ticket | Scope | Deps |
|---|---|---|---|
| G | T12-07 | `/butler_undo` and rollback execution | T12-01, T12-05, T12-06 |
| H | T12-08 | Rate limits, TTL expiry, cross-user consent | T12-05 |
| I | T12-09 | Butler evals, abuse tests, governance breach tests | all prior |
| J | T12-10 | Final holistic review and operator handoff | all prior |

Wave diagram:

```
Wave 1 (parallel):  A      B      C
                    |      |      |
                    v      v      v
Wave 2 (parallel):  D ---> E      F
                    |      |      |
                    v      v      v
Wave 3 (parallel):  G      H      I
                    \      |      /
                     v     v     v
                         J
```

---

## §7. Tickets

### T12-01 — Butler Audit Schema and Repositories

**Scope:** add `butler_actions`, `butler_tool_invocations`, `butler_action_confirmations` schema and repository layer.

**Acceptance criteria:**

- migrations apply and rollback cleanly;
- repository can create requested, planned, confirmed, executed, rejected, expired, and undo-linked actions;
- executed action cannot exist without action args hash, evidence context hash, and tool name;
- confirmation rows preserve preview payload hash;
- no audit row stores raw evidence context beyond allowed citation ids / snippets hashes.

**Dependencies:** Phase 5 `llm_usage_ledger`; existing DB migration framework.

### T12-02 — Governance-Filtered `EvidenceContext` Contract

**Scope:** define and implement the sealed evidence context adapter Butler uses for all memory reads.

**Acceptance criteria:**

- Butler has no direct repository access to raw message tables;
- context contains `message_version_id` or approved card source ids only;
- tests prove `#nomem`, `#offrecord`, forgotten, redacted, and unauthorized rows are excluded;
- context hash is stable and stored on the action;
- direct raw DB access attempt in Butler code is caught by test or lint rule.

**Dependencies:** Phase 4 evidence bundle; Phase 3 governance; Phase 6 card source model if approved cards are included.

### T12-03 — Tool Registry, Strict Whitelist, and Tool Schemas

**Scope:** create Butler tool manifest and typed schemas for `schedule_meeting`, `send_intro`, `update_intro`, `recall_evidence`, `suggest_card_creation`.

**Acceptance criteria:**

- unknown tool name rejects before confirmation;
- missing or extra disallowed args reject before confirmation;
- whitelist manifest has version and test snapshot;
- tool schemas are passed to `llm_gateway`;
- no arbitrary Telegram method name can appear in LLM output.

**Dependencies:** Phase 5 gateway structured-output support.

### T12-04 — Butler Orchestrator State Machine

**Scope:** implement `bot/services/butler.py` orchestration from request to pending confirmation.

**Acceptance criteria:**

- creates action rows for planned actions;
- calls `llm_gateway` only, never provider SDK;
- rejects hallucinated tool args before execution;
- writes `llm_usage_ledger` link;
- enforces action TTL and status transitions;
- no tool execution happens before confirmation.

**Dependencies:** T12-01, T12-02, T12-03, Phase 5 gateway.

### T12-05 — Telegram `/butler` Command and Inline Confirmation UI

**Scope:** add user-facing command, action previews, inline keyboard callbacks, cancel flow, and status lookup.

**Acceptance criteria:**

- `/butler <request>` returns one preview per action;
- each preview includes exact outgoing text where applicable;
- confirmation callback checks actor, TTL, and preview hash;
- cancellation moves action to `cancelled`;
- feature flag defaults OFF;
- non-members cannot invoke Butler.

**Dependencies:** T12-04; existing aiogram handler patterns; feature flags.

### T12-06 — Whitelisted Tool Implementations

**Scope:** implement the five initial tools under `bot/services/butler_tools/`.

**Acceptance criteria:**

- `recall_evidence` delegates to governed evidence service only;
- `schedule_meeting` posts a Telegram-native proposal only;
- `send_intro` sends confirmed intro text only after required confirmations;
- `update_intro` edits only Butler-owned intro messages or posts a follow-up;
- `suggest_card_creation` creates a pending review suggestion only, never active card;
- each tool returns `inverse_op_payload`.

**Dependencies:** T12-03, T12-04, Telegram wrapper.

### T12-07 — Rollback and `/butler_undo <action_id>`

**Scope:** implement undo flow and inverse operation execution.

**Acceptance criteria:**

- undo creates linked `butler_actions.parent_action_id`;
- original action audit remains immutable;
- delete/edit/follow-up/cancel inverse kinds are supported;
- irreversible actions report `not_reversible` and write audit;
- failed undo records `undo_failed` with structured error context;
- actor authorization for undo is tested.

**Dependencies:** T12-01, T12-05, T12-06.

### T12-08 — TTL, Rate Limits, Cross-User Consent, and Abuse Prevention

**Scope:** implement expiry worker, per-user / per-chat / per-tool rate limits, and affected-user confirmation flow.

**Acceptance criteria:**

- pending actions expire after configured TTL;
- expired inline keyboards cannot execute;
- per-user daily Butler limit enforced separately from `/recall`;
- cross-user intro requires affected-user confirmation;
- repeated rejected actions trigger cooldown;
- no confirmation preview reveals evidence outside the confirmer visibility scope.

**Dependencies:** T12-05, T12-06; membership / admin checks.

### T12-09 — Butler Evals and Stop-Signal Tests

**Scope:** add evaluation and regression tests for governance, hallucinated args, confirmation, audit, and rollback.

**Acceptance criteria:**

- test: Butler reading raw DB directly fails review / lint / unit guard;
- test: non-whitelisted tool is rejected;
- test: confirmation skipped cannot execute;
- test: hallucinated tool arg rejects before execution;
- test: forgotten evidence never appears in Butler context;
- test: LLM call outside gateway is absent;
- test: every executed action has `llm_usage_ledger` and Butler audit rows.

**Dependencies:** T12-01..T12-08.

### T12-10 — Phase 12 Final Holistic Review and Operator Handoff

**Scope:** review implementation against invariants, write operator runbook, and close phase only after evidence is attached.

**Acceptance criteria:**

- invariant checklist passes, especially #2, #3, #7, #9;
- review confirms no external APIs beyond whitelisted Telegram wrapper methods;
- review confirms all actions have per-action confirmation by default;
- operator runbook documents disable flags, rate limits, undo, and audit queries;
- Phase 12 can be disabled by feature flag without breaking gatekeeper.

**Dependencies:** T12-09.

---

## §8. Stop Signals

Any of these stop implementation immediately:

- Butler reading raw DB → invariant #7 breach.
- Tool not in whitelist → REJECT.
- Confirmation skipped → REJECT.
- LLM hallucinated tool args → reject before execution.
- LLM called outside `llm_gateway` → REJECT.
- Evidence context includes `#nomem`, `#offrecord`, forgotten, redacted, tombstoned, or unauthorized content → REJECT.
- Action tries money handling, payments, purchases, or financial transfer → REJECT.
- Tool tries external API beyond whitelisted Telegram methods → REJECT.
- Cross-user action lacks affected-user consent → REJECT.
- Pending action expired → REJECT.
- `inverse_op_payload` missing for executable action → REJECT.
- Action audit cannot be written → REJECT and do not execute.
- Unknown rollback semantics for a tool → mark `not_reversible` in preview or do not ship the tool.
- Feature flag default ON → REJECT.

---

## §9. PR Workflow

Phase 12 is not authorized for implementation yet. When it becomes authorized, use the same small-stream workflow as Phase 4:

1. Create one worktree per stream under `.worktrees/`.
2. One ticket per PR unless explicitly paired by dependency.
3. Keep feature flag default OFF.
4. Implement with tests in the same PR.
5. Run focused tests plus full relevant bot suite.
6. Run ruff and mypy for touched modules.
7. Add PR evidence: changed files, tests run, invariant checklist, audit behavior, rollback behavior, risk notes.
8. Require review from product / governance owner and technical reviewer.
9. Never merge a PR that weakens invariant #7.
10. After Wave 3, run Final Holistic Review before declaring Phase 12 complete.

PR checklist:

- no raw DB reads from Butler;
- no LLM calls outside gateway;
- no non-whitelisted tools;
- per-action confirmation default;
- `llm_usage_ledger` linked;
- `butler_actions` audit linked;
- rollback path present or explicitly `not_reversible`;
- feature flag default OFF;
- no external APIs beyond whitelisted Telegram wrapper methods.

---

## §10. Glossary

- **Butler:** constrained action agent that plans and executes whitelisted actions after confirmation.
- **EvidenceContext:** sealed governance-filtered memory envelope passed to Butler and LLM gateway.
- **Tool whitelist:** fixed list of Butler tools allowed in Phase 12 baseline.
- **ButlerPlan:** structured LLM gateway output containing validated candidate actions.
- **Per-action confirmation:** each action requires its own explicit confirmation row and callback.
- **Affected-user consent:** confirmation required from a user directly affected by a cross-user action.
- **Action TTL:** expiration window after which a planned action cannot be confirmed or executed.
- **`butler_actions`:** primary audit row for every planned, executed, rejected, expired, or undo action.
- **`butler_tool_invocations`:** audit row for each actual tool call attempt.
- **`butler_action_confirmations`:** audit row for confirmation / rejection events.
- **`inverse_op_payload`:** structured payload needed to execute the best available undo path.
- **Best-effort rollback:** undo attempt that may delete, edit, or correct, but cannot guarantee a recipient did not see prior content.
- **Tool manifest version:** versioned schema of allowed tools and arguments used to validate LLM plans.
- **Stop signal:** condition that rejects the action or stops implementation before an invariant breach ships.

---

## §11. Compliance Recap (added at ratification, 2026-05-02)

This section is a cross-reference index, not new substance. It exists because the
Orchestrator B charter directive at sprint 0a explicitly required the ratified plan to
"complete the spec on permission model, action audit, abort/dry-run, future butler
boundary per HANDOFF §Phase 12". Each topic below points to where the binding contract
already lives in §1–§10, so future readers (and Phase 12 implementers, when authorized)
can audit invariant coverage in one pass.

### §11.1 Permission model — where it is specified

Butler authorization is enforced at four layers, fail-closed:

1. **Invocation gate (who can call `/butler`).** §2 "Abuse Prevention" + §5.D "Telegram
   `/butler` Command + Interactive Confirmation UI": baseline limits to community
   members and admins; non-members rejected without evidence lookup; DM usage allowed
   only for personal preview unless target chat is explicit and user is authorized.
2. **Per-action confirmation gate.** §2 "User Confirmation Default" + §5.D + §5.A
   `butler_action_confirmations`: every action carries a `requires_confirmation` flag
   defaulting to TRUE; the confirmer's role is recorded as `requester | affected_user |
   admin | rollback_requester` in `butler_action_confirmations.confirmation_role`.
3. **Cross-user consent gate.** §2 "Cross-User Butler Actions" + §5.D "affected-user
   prompts" + Stop Signal "Cross-user action lacks affected-user consent → REJECT".
4. **Visibility-scoped preview gate.** §5.D "No confirmation preview may reveal
   evidence the confirmer is not authorized to see" + §5.F EvidenceContext
   `visibility_scope: member | admin | self`.

Future butler implementation MUST NOT introduce a fifth bypass (e.g. session-wide
opt-in) without an explicit re-ratification of this plan and an `AUTHORIZED_SCOPE.md`
amendment.

### §11.2 Action audit — where it is specified

Every planned, confirmed, executed, rejected, expired, or undone Butler action writes
audit rows. Audit is never optional and never best-effort — failure to write audit is a
Stop Signal that aborts the action before execution (§8 "Action audit cannot be
written → REJECT and do not execute").

The three audit tables and their roles:

| Table | Purpose | Section |
|-------|---------|---------|
| `butler_actions` | one row per planned / executed / rejected / expired / undo action; carries `evidence_context_hash`, `action_args_hash`, `result_payload_hash`, `inverse_op_payload`, `llm_usage_ledger_id` | §5.A |
| `butler_tool_invocations` | one row per actual tool call attempt; `idempotency_key UNIQUE`; no hidden retries | §5.A |
| `butler_action_confirmations` | one row per confirmation / rejection event; preserves `preview_payload_hash` and `confirmation_role` | §5.A |

Cross-table linkage rules:

- Every executed `butler_actions` row MUST link to at least one
  `butler_action_confirmations` row (default per-action confirmation policy).
- Every Butler LLM call MUST link to `llm_usage_ledger` via
  `butler_actions.llm_usage_ledger_id` (Phase 5 ledger).
- Every undo action MUST set `butler_actions.parent_action_id` to the original action;
  the original's audit is immutable.

Operator inspection surface (admin-only, read-only) is out of scope for the baseline
schema but the columns above MUST be queryable for later read-only admin tooling
(§5.G observability).

### §11.3 Abort and dry-run — where it is specified

The Butler lifecycle exposes three distinct "stop the action" paths:

1. **Reject before execution (validation abort).** §2 "Strict validation happens before
   user confirmation" + §5.B "Fail-closed validation": unknown tool, schema violation,
   hallucinated args, missing evidence, governance breach, expired TTL, unauthorized
   actor — all REJECT before any Telegram side effect.
2. **Cancel after planning, before / during pending confirmation.** §5.D `/butler_cancel
   <action_id>` + lifecycle state `cancelled`. The action transitions to `cancelled`
   without any tool invocation.
3. **TTL expiry (silent abort).** §2 "Action TTL" + §5.A `butler_actions.expires_at` +
   §5.D inline keyboard hash check. After expiry, the inline keyboard cannot trigger
   execution; the action transitions to `expired`.

Dry-run semantics — Phase 12 baseline does NOT ship a separately-named "dry-run" mode
because every action already enforces a mandatory dry-pass before any Telegram side
effect:

- The plan is validated (§5.B) before user confirmation.
- The user confirmation preview (§5.D) shows exact outgoing text, target chat / users,
  evidence citations, expiry, undo availability — that preview IS the dry-run output.
- `recall_evidence` (§5.C tool list) is the only memory-read tool and has no Telegram
  side effect; it is intrinsically dry by construction.

If a future iteration wants an explicit `/butler --dry-run` flag (e.g. for evals or
operator testing), it MUST reuse the existing planning + validation path and stop
before `butler_action_confirmations` is created. It MUST still write a
`llm_usage_ledger` entry (so dry-run cost is metered) and a lightweight `butler_actions`
row in status `rejected` with `rejection_reason='dry_run_inspection'` (so dry-run audit
exists). This is a hardening note, not an authorization to ship the flag.

### §11.4 Future butler boundary — where it is specified

The boundary recap consolidates the binding interpretations of HANDOFF.md §1
invariant 7 into a single audit-friendly list. Each row is a fail-closed contract any
Phase 12 implementer (when authorized) MUST satisfy:

| # | Contract | Where enforced |
|---|----------|----------------|
| 1 | Butler service receives `EvidenceContext`, never raw `telegram_updates`, raw `chat_messages`, raw SQL rows, or raw graph rows. | §1 invariant 7 binding interpretation; §4 architecture chain; §5.C "every tool receives `EvidenceContext`, not DB session access for memory reads"; §5.F EvidenceContext contract. |
| 2 | All LLM calls go through `bot/services/llm_gateway.py`. No provider SDK import inside `bot/services/butler.py` or `bot/services/butler_tools/`. | §1 invariant 2 binding interpretation; §2 LLM Gateway Contract; §5.C "no tool calls LLM"; §8 Stop Signal "LLM called outside `llm_gateway` → REJECT". |
| 3 | EvidenceContext excludes `#nomem`, `#offrecord`, redacted, forgotten, tombstoned, unauthorized content BEFORE the gateway call. | §1 invariant 3 binding interpretation; §5.F contract bullet "service never returns `#nomem`, `#offrecord`, forgotten, or unauthorized content"; T12-02 acceptance criteria. |
| 4 | Citations resolve to `message_version_id` or approved `card_sources` ids only. No raw `chat_messages.id` citations. | §1 invariant 4 binding interpretation; §5.F shape; §5.A `butler_actions.evidence_ids` constraint. |
| 5 | Tool whitelist is closed-list (5 tools at baseline). LLM-emitted tool names not in whitelist REJECT before confirmation. | §2 "Tool whitelist scope" decision; §5.C `ALLOWED_BUTLER_TOOLS`; §8 Stop Signal "Tool not in whitelist → REJECT". |
| 6 | No external API beyond the explicit Telegram bot wrapper methods required by the five tools. | §3 Out-of-Scope; §8 Stop Signal "Tool tries external API beyond whitelisted Telegram methods → REJECT". |
| 7 | Rollback path (`inverse_op_payload`) MUST exist before status transitions to `succeeded`, OR `rollback_kind = 'not_reversible'` is set with explicit user warning in preview. | §5.E rollback categories; §8 Stop Signal "`inverse_op_payload` missing for executable action → REJECT". |
| 8 | Feature flag default OFF. Flipping default ON requires explicit team-lead approval + invariant re-audit. | §5.D acceptance criteria "feature flag defaults OFF"; §8 Stop Signal "Feature flag default ON → REJECT". |
| 9 | Forget cascade: any new Phase 12 content table (`butler_actions`, `butler_tool_invocations`, `butler_action_confirmations`) MUST be wired into `bot/services/forget_cascade.CASCADE_LAYER_ORDER` in the same migration sprint. (Cross-orchestrator binding from `ORCHESTRATOR_REGISTRY.md §2 Shared` and HANDOFF.md §1 invariant 9.) | T12-01 acceptance criteria — added at ratification; cross-ref `forget_cascade._LAYER_FUNCS`. |

Boundary contract #9 was made explicit at ratification (it was implicit in the original
draft but is the most common silent-violation footgun for new content tables). The
T12-01 acceptance criteria already lean on it; this row makes the cross-orchestrator
expectation visible to any future Phase 12 implementer.

---

## Final Report Block

CANONICAL_PATH: docs/memory-system/PHASE12_PLAN.md
STATUS: RATIFIED — DESIGN ONLY (no implementation authorized)
RATIFIED_AT: 2026-05-02
RATIFIED_BY: Orchestrator B (sprint 0a, branch `plan/p12-ratify`)
ORIGINAL_DRAFT_PATH: docs/memory-system/prompts/PHASE12_PLAN_DRAFT.md (renamed via `git mv`)
COMPONENTS: 5+
TICKETS: T12-01..T12-10 (10 tickets, all design-only contracts — not authorized for implementation until AUTHORIZED_SCOPE.md update)
INVARIANT_7_BINDING: yes (butler reads only evidence context)
INVARIANT_2_BINDING: yes (LLM only via gateway)
INVARIANT_9_BINDING: yes (forget cascade integration noted in §11.4 row 9 + T12-01 acceptance)
USER_CONFIRMATION_FLOW_DESIGNED: yes (per-action default; cross-user consent required)
TOOL_WHITELIST_DESIGNED: yes (start small, 5 tools)
ROLLBACK_PATH_DESIGNED: yes (best-effort, audited; `not_reversible` allowed when honest)
DEPS_NOTED: Phase 5 (gateway), Phase 6 (cards as suggestions), Phase 8 (observations as context)
OPEN_DESIGN_QUESTIONS_RESOLVED_AT_RATIFICATION:
  1. User confirmation default → per-action (§2, §5.D);
  2. Tool whitelist scope → start tiny with 5 tools (§5.C);
  3. Action TTL → 15min low-risk / 5min cross-user / 30min admin-suggestion (§2);
  4. Rollback semantics → best-effort, not guaranteed (§5.E);
  5. Cross-user actions → affected-user confirmation required by default (§2);
  6. Cost ceiling → separate per-user / per-chat Butler budget, stricter than Q&A (§2);
  7. Abuse prevention → members/admins only, per-user/per-chat/per-tool rate limits, cooldown (§2).
OPEN_QUESTIONS_DEFERRED: dry-run flag (§11.3); admin override of cross-user consent (§2 "Admin override is out of scope for baseline Phase 12 unless separately authorized"); operator dashboard surface (§5.G "out of scope for baseline implementation").
