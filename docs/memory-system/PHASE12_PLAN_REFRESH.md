# Phase 12 Plan Refresh — Sprint 0 Ratification

## §1. Banner + Status

- **Status:** RATIFICATION SPRINT (Sprint 0 — docs only). No source code, no migrations, no handlers, no tests, no feature flags.
- **Authorization date:** 2026-05-25.
- **Authorizer:** jekudy@gmail.com (team lead, this session).
- **Predecessor ratification:** `PHASE12_PLAN.md` ratified 2026-05-02 (Orchestrator B sprint 0a, PR #171). `PHASE12_DESIGN.md` companion landed 2026-05-19.
- **Phase 12 owner:** Orchestrator B per `ORCHESTRATOR_REGISTRY.md §1`.
- **Charter:** `governance_mode = critical` (privacy invariants binding); `git_workflow_mode = parallel_wave_prs`; per-PR PAR dual review (Claude product + Codex technical); FHR mandatory after T12-10.
- **Predecessor gates (all CLOSED before this sprint):** Phase 0 (gatekeeper) CLOSED; Phase 1 (source of truth) CLOSED 2026-04-27; Phase 2 (importer + governance skeleton) CLOSED 2026-04-29; Phase 3 (governance skeleton) CLOSED 2026-04-29; Phase 4 (FTS + Q&A) CLOSED 2026-04-30; Phase 5 (LLM gateway + ledger) CLOSED 2026-05-11; Phase 6 (cards) CLOSED 2026-05-12; Phase 7 (daily digest) CLOSED 2026-05-15; Phase 8 (weekly digest) CLOSED 2026-05-15; Phase 9 (wiki) CLOSED 2026-05-19; Phase 10 (graph projection) CLOSED 2026-05-21; Phase 11 (binding suite) **77/77 green on `main`**.

This document **refreshes** the 2026-05-02 ratified plan with deltas accumulated since Phase 9/10 closure. It does NOT supersede `PHASE12_PLAN.md` — it patches it. Where this file and `PHASE12_PLAN.md` disagree, this file wins for execution; `PHASE12_PLAN.md` remains the authoritative source for §1–§11 invariants, schema sketches, and the §6 wave structure.

---

## §2. Cross-reference to canonical docs

| Doc | Path | Role | Authoritative for |
|---|---|---|---|
| Detailed per-component plan | `docs/memory-system/PHASE12_PLAN.md` (1191 lines) | RATIFIED 2026-05-02 | §1 invariants, §5 component DDL sketches, §6 wave overview, §8 stop signals, §10 glossary, §11 compliance recap |
| Architectural companion | `docs/memory-system/PHASE12_DESIGN.md` (632 lines) | DESIGN-ONLY 2026-05-19 | §3.1 ASCII architecture, §6 cost envelopes, §7 binding test family proposal, §10 open questions surfaced post-Phase 9 |
| THIS Sprint 0 refresh | `docs/memory-system/PHASE12_PLAN_REFRESH.md` | RATIFICATION 2026-05-25 | open-questions resolutions (§3), BLOCKER fixups (§4), HIGH fixups (§5), authoritative artefact contract (§7), wave plan refresh (§10), Sprint 0 DoD (§11), Phase 11 binding contract (§12), cost/rate envelopes (§14) |

Cross-cutting binding contracts live in `HANDOFF.md §1` (invariants 1-10) and `ORCHESTRATOR_REGISTRY.md §2` (shared-file edit discipline).

---

## §3. Open questions resolutions (decided 2026-05-25 by team lead)

The six open questions surfaced in `PHASE12_DESIGN.md §10` were not resolved at 2026-05-02 ratification. They are resolved here, verbatim, and become binding for Phase 12 execution.

### §3.1 Surface (DM vs group chat)

**Decision:** DMs only baseline. `/butler` invocation is allowed ONLY in a private DM with the bot in Phase 12.1–12.4. Group-chat surface (`/butler` in a public community chat) is **Phase 12.5+** and requires separate authorization.

Result-posting exception: `schedule_meeting` and `send_intro` STILL post the resulting message to the target chat (the chat referenced in the action args). The user invokes Butler in DM; the *output* lands in the configured target chat after both confirmation gates pass.

Rationale: smaller blast radius, simpler abuse model, inline-keyboard UX can be tuned in DM context before going public. Closes `PHASE12_DESIGN.md §10.1`.

### §3.2 Triggers / cron

**Decision:** DEFER all scheduled triggers to Phase 12.5+. No `butler_triggers` table, no `butler_*_job` APScheduler entry, no `pg_cron` extension dependency in baseline. Butler is **user-initiated only** — every action starts with an explicit `/butler <request>` from a real user.

Rationale: trigger storage introduces a new persistence + replay-on-startup question (in-memory APScheduler loses state on restart) and proactive nudges expand the consent surface. Closes `PHASE12_DESIGN.md §10.2`.

### §3.3 Per-user opt-in

**Decision:** NO per-user opt-in flag. Authorization = membership (Phase 0 membership check via `UserRepo.get(session, user_id)` returning `User` with `user.is_member is True OR user.is_admin is True` — identical pattern to `bot/handlers/qa.py:369` and `bot/handlers/forward_lookup.py:46`) + per-action confirmation (per `PHASE12_PLAN.md §2 "User Confirmation Default"`).

Users retain granular control via `#nomem` / `#offrecord` at the message level, and via `/forget` / `/forget_me` at the account level. No `butler_consent` table.

Rationale: doubles the policy surface; absence is the signal; existing controls are sufficient. Closes `PHASE12_DESIGN.md §10.3`.

### §3.4 Rate-limit storage

**Decision:** `butler_rate_buckets` Postgres table. Migration lands in **072** as part of T12-01 schema sprint.

Persistent, audit-friendly, multi-replica safe (single Postgres source of truth across bot instances). Schema sketched in §5 below.

Rationale: in-memory dicts lose state on restart and don't survive multi-replica; Redis introduces new infra. Closes `PHASE12_DESIGN.md §10.4`.

### §3.5 Admin override of cross-user consent

**Decision:** NO admin override in baseline. Affected-user consent is **unbypassable** in Phase 12.1–12.4. If admins need to broker introductions, they do so manually outside the Butler surface.

Phase 12.5+ may revisit (e.g. for community moderation contexts), but not as part of Phase 12 baseline. Closes `PHASE12_DESIGN.md §10.5`.

### §3.6 Evidence freshness

**Decision:** SNAPSHOT on plan + TTL ≤ 30 min + cascade-aware revalidation pre-execute.

Mechanics:

1. At planning time (`butler_actions.status='planned'`), the Butler captures the `EvidenceBundle` (see §4.2 below for the contract rename) and stores `evidence_context_hash = butler_context_hash(bundle, visibility_scope, governance_filter_version)` on the action row. `butler_context_hash` is the ONE canonical hash function used by BOTH §3.6 revalidation and §12.5 G3.b replay; spec below:

   ```python
   import hashlib, json
   from bot.services.evidence import EvidenceBundle

   def butler_context_hash(
       bundle: EvidenceBundle,
       visibility_scope: str,
       governance_filter_version: str,
   ) -> str:
       """Canonical context hash. Stable across replays.

       Card identity (card_id + card_source_message_version_ids) is part
       of the input — EvidenceBundle.evidence_ids only flattens to
       message_version_ids, which would LOSE card identity (a card-hit
       with the same anchor mvid as a message-hit would hash equal).
       """
       items_canonical = sorted(
           [
               {
                   "source_type": item.source_type,
                   "message_version_id": item.message_version_id,
                   "card_id": str(item.card_id) if item.card_id is not None else None,
                   "card_source_message_version_ids": sorted(
                       item.card_source_message_version_ids or ()
                   ),
               }
               for item in bundle.items
           ],
           key=lambda d: (
               d["source_type"],
               d["message_version_id"] if d["message_version_id"] is not None else -1,
               d["card_id"] or "",
           ),
       )
       payload = {
           "items": items_canonical,
           "visibility_scope": visibility_scope,
           "governance_filter_version": governance_filter_version,
       }
       canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
       return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
   ```

   Stored as the `evidence_context_hash` column on `butler_actions` (concrete DDL §4.5). G3.b (§12.5) recomputes via the SAME helper and asserts byte equality.
2. The action's `expires_at = now() + TTL` per risk class (5/15/30 min — §14).
3. **At execute time** (callback handler fires), before any Telegram side effect, the Butler revalidates the bundle via this SQL — concrete predicate, read-side `fe.tombstone_key` prefix convention (NOT `target_id`) per memory `feedback-tombstone-key-read-side-convention.md` (2026-05-12):

   ```sql
   -- Pre-execute revalidation guard (privacy invariant binding). Returns any
   -- mvid in the snapshotted bundle that has been forgotten or governance-
   -- redacted since snapshot. Caller treats a non-empty result as fail-closed:
   -- butler_actions.status -> 'expired', no Telegram side effect emitted.
   -- :evidence_mvids   = flattened list of EvidenceItem.message_version_id values
   --                     for items where source_type='message' (NULL filtered out)
   -- :card_source_mvids = flattened CONCAT of EvidenceItem.card_source_message_version_ids
   --                     arrays across items where source_type='card'
   -- Both inputs are sorted ascending for deterministic plan/replay (matches the
   -- canonicalization in butler_context_hash above). Caller MUST pass BOTH arrays;
   -- empty bundle (no messages, no card sources) is rejected pre-revalidation by
   -- T12-04 state-machine guard (no EvidenceContext → no action allowed).
   WITH bundle_mvids AS (
       SELECT unnest(:evidence_mvids::bigint[]) AS mvid
       UNION ALL
       SELECT unnest(:card_source_mvids::bigint[]) AS mvid
   )
   SELECT DISTINCT mvid FROM bundle_mvids m
   WHERE EXISTS (
       SELECT 1 FROM forget_events fe
       JOIN message_versions mv ON mv.id = m.mvid
       JOIN chat_messages cm ON cm.id = mv.chat_message_id
       WHERE fe.status IN ('active','completed')
         AND fe.tombstone_key IN (
             format('message:%s:%s', cm.chat_id, cm.tg_message_id),
             format('message_hash:%s', cm.content_hash),
             format('user:%s', cm.from_user_id)
         )
   ) OR EXISTS (
       SELECT 1 FROM chat_messages cm
       JOIN message_versions mv ON mv.chat_message_id = cm.id
       WHERE mv.id = m.mvid AND (
           cm.memory_policy != 'normal' OR cm.is_redacted = TRUE OR mv.is_redacted = TRUE
       )
   );
   ```

   Any returned mvid → fail-closed: action status → `expired`, no Telegram side effect emitted. T12-08 acceptance criterion references this verbatim. Read-side `fe.tombstone_key` prefix convention (3 keys: `message:<chat>:<mid>` / `message_hash:<hash>` / `user:<uid>`) MUST be used here — NOT `target_id`. This matches `bot/services/search.py:117,126,130,205,210,214` patterns.

   **Card-source variant note (R1 spot-review):** The `UNION ALL` above merges card-source mvids into the same `bundle_mvids` CTE — a single revalidation pass covers BOTH `source_type='message'` items and `source_type='card'` items' backing `card_source_message_version_ids` (`bot/db/models.py:1188`). The same `fe.tombstone_key` predicate + `cm.memory_policy/is_redacted` predicate apply to both — a forgotten message backing a card hits the same EXISTS clauses. Card-level archival (`knowledge_cards.status='archived'`) is a SEPARATE check enforced at evidence-build time (T12-02) — not at execute-time revalidation; archived-card actions are rejected during the planning LLM call (C10.c contract).
4. **Cascade integration** is fail-closed: if `_cascade_butler_actions` (§4.4 below) marks any matching `pending_confirmation` row as `expired` before the keyboard callback fires, the callback handler simply observes `status='expired'` and refuses.
5. **Cascade-vs-callback race lock (M3).** The callback handler reads the `butler_actions` row with `SELECT ... FOR UPDATE` before any side effect; `_cascade_butler_actions` cascade layer acquires the SAME row lock atomically inside the cascade transaction. Lock acquisition order: callback handler → cascade layer (cascade is the long-running side; callback is short-lived). Reused pattern from `bot/handlers/wiki_publish.py /wiki_publish` advisory lock (Phase 9 T9-06). The lock is per-action-row (not per-chat), so concurrent Butler actions in the same chat are not serialised. Combined with the §4.5 `ck_butler_actions_executed_has_inverse` invariant and the §4.6 `butler_card_suggestions` UNIQUE constraint, the lock makes the (cascade redacts mid-callback) race fail-closed.

Rationale: snapshot preserves "what the user saw is what gets sent" for confirmation UX; the cascade-aware expiry layer prevents forgotten content from leaking through stale snapshots. Closes `PHASE12_DESIGN.md §10.6`.

---

## §4. BLOCKER fixups

These BLOCKERS were raised by the Codex pre-execution audit (2026-05-25) and MUST be resolved before any execution sprint opens.

### §4.1 Phase 12 implementation authorization

**What `PHASE12_PLAN.md §0` says:** "NO IMPLEMENTATION AUTHORIZED (per `AUTHORIZED_SCOPE.md`)".

**What `AUTHORIZED_SCOPE.md` says today** (lines 113-122): "Phase 12 is authorized **for documentation only**. NO implementation, NO execution code, NO database tables."

**Decision:** Add a new section "## Authorized: Phase 12 — Future Butler / Action Layer (2026-05-25)" to `AUTHORIZED_SCOPE.md`, inserted **before** the "## NOT authorized" block. Full text in §8 below.

**Fixup location:** `AUTHORIZED_SCOPE.md` edit, committed as part of this Sprint 0 PR.

### §4.2 `EvidenceContext` does not exist — actual code uses `EvidenceBundle` / `EvidenceItem`

**What `PHASE12_PLAN.md §1, §2, §5.F` says:** Butler consumes a sealed `EvidenceContext` envelope. `PHASE12_DESIGN.md §3.2` similarly references `EvidenceContext`.

**Current reality (verified via grep):**

- `bot/services/evidence.py:123` defines `class EvidenceBundle` (frozen dataclass, `query: str`, `chat_id: int`, `items: tuple[EvidenceItem, ...]`, `abstained: bool`, `created_at: datetime`).
- `bot/services/evidence.py:35` defines `class EvidenceItem` (frozen dataclass with `source_type: Literal["message", "card"]`, `message_version_id`, `card_id: uuid.UUID | None`, `card_source_message_version_ids: tuple[int, ...]`).
- `EvidenceContext` does not exist anywhere in `bot/services/`, `bot/db/`, or `bot/handlers/` (zero grep hits).
- The 29 mentions of `EvidenceContext` in `PHASE12_PLAN.md` + `PHASE12_DESIGN.md` are design-doc terminology drift.

**Decision:** Phase 12 execution uses `EvidenceBundle` as the canonical contract. The `EvidenceContext`-named adapter is introduced as a **thin sealed wrapper** that adds Butler-specific metadata (`visibility_scope`, `context_hash`, `governance_filter_version`) on top of an `EvidenceBundle`. Concretely:

```python
# bot/services/butler_evidence.py  (NEW in T12-02)

from dataclasses import dataclass
from typing import Literal
from bot.services.evidence import EvidenceBundle, EvidenceItem


@dataclass(frozen=True, slots=True)
class ButlerEvidenceContext:
    """Sealed Butler-facing wrapper around an EvidenceBundle.

    Adds visibility_scope + context_hash + governance_filter_version on
    top of the raw bundle. The wrapper is immutable after construction
    — the Butler never re-ranks or augments after recall_evidence returns.
    """
    bundle: EvidenceBundle
    visibility_scope: Literal["member", "admin", "self"]
    context_hash: str  # = butler_context_hash(bundle, visibility_scope, governance_filter_version) per §3.6 step 1
    governance_filter_version: str  # detect_policy version + cascade-layer-order hash
    # Convenience accessors that proxy to the wrapped bundle:
    @property
    def evidence_ids(self) -> list[int]:
        return self.bundle.evidence_ids
    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        return self.bundle.items
```

`bot/services/butler.py` (T12-04) accepts `ButlerEvidenceContext` exclusively for memory reads; it never sees a raw `EvidenceBundle` (the bundle is wrapped at the `recall_evidence` boundary).

**Fixup location:** T12-02 (Wave 1 Stream B). `PHASE12_PLAN.md §1` invariant 7 binding interpretation and `PHASE12_PLAN.md §5.F` "Evidence Context Service Contract" remain valid in spirit; the **type name** changes to `ButlerEvidenceContext`. Errata note appended to `PHASE12_PLAN.md` per §7 below.

### §4.3 LLM gateway has no Butler entrypoint

**Current reality:** `bot/services/llm_gateway.py` exposes (verified via grep at lines 355, 1072, 1622, 1932) — H3 erratum: `extract_candidates` already EXISTS in code since Phase 6; the original spec text describing it as "future" was outdated. Full current public API:

- `synthesize_answer` (line 355) — Phase 4 Q&A path, `call_type='qa_synthesis'`.
- `extract_candidates` (line 1072) — Phase 6 card extraction, `call_type='extract_candidates'` (already present per migration 064 backfill + the `RESERVED_LEDGER_CALL_TYPES` tuple).
- `synthesize_digest` (line 1622) — Phase 7/8 digests, routes `call_type='digest_daily'` / `'digest_weekly'`.
- `extract_graph_triples` (line 1932) — Phase 10, `call_type='graph_projection'`.

Phase 12 ADDS two new gateway functions: `plan_butler_action` (call_type=`butler_decision`) + `synthesize_butler_summary` (call_type=`butler_summary`, optional summarization tool). No Butler entrypoint exists today. The `call_type` allow-list in `bot/services/graph_common.py:88` (`RESERVED_LEDGER_CALL_TYPES`) currently includes only `'graph_projection'` and `'extract_candidates'`; migration 071 (T12-01b) extends the allow-list (and adds a DB CHECK constraint that was missing per §5.4) to cover `'butler_decision'` + `'butler_summary'`.

**Decision:** T12-03 adds a new gateway function:

```python
# bot/services/llm_gateway.py  (new function alongside synthesize_answer)

async def plan_butler_action(
    *,
    session: AsyncSession,
    requester_user_id: int,
    request_text: str,
    chat_id: int,
    evidence: ButlerEvidenceContext,
    allowed_tools: frozenset[str],  # frozenset(ALLOWED_BUTLER_TOOLS.keys())
    tool_manifest_version: str,
    call_type: Literal["butler_decision"],
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
) -> ButlerPlan:
    """Single LLM entrypoint for Butler action planning.

    Returns a structured ButlerPlan validated against the tool whitelist
    + evidence whitelist. Raises ButlerPlanRejectedError on schema /
    whitelist / hallucination failure. Writes one llm_usage_ledger row
    with call_type='butler_decision'.

    No retries. Single call. Single ledger row.
    """
    ...
```

Validation contract (enforced inside `plan_butler_action`, fail-closed):

- `tool_name` in `allowed_tools`;
- args validate against the tool's pydantic args model (per `PHASE12_PLAN.md §5.C`);
- every `evidence_ids[i]` in `evidence.evidence_ids`;
- no raw DB ids except citation anchors + approved `card_sources.id`;
- no arbitrary URLs, no shell, no SQL, no provider SDK calls inside the gateway path itself (only one provider call via `provider.call(prompt=..., model=...)` per `bot/services/llm_providers/__init__.py:62` — the actual `LLMProvider` Protocol; same call shape used by `synthesize_answer` line 604, `extract_candidates` line 1221, `synthesize_digest` line 1733, `extract_graph_triples` line 2031).

A second gateway function `synthesize_butler_summary` (`call_type='butler_summary'`) covers the rare case where Butler emits user-facing prose (e.g. an intro draft). Same pattern as `plan_butler_action`, simpler validation (no tool-args schema, only citation-anchor enforcement).

**Fixup location:** T12-03 (Wave 1 Stream C) defines the schemas + the gateway functions; T12-04 (Wave 2 Stream D) wires `bot/services/butler.py` to call them.

### §4.4 `CASCADE_LAYER_ORDER` drift

**What `PHASE12_DESIGN.md §4.4` says:** Butler layers inserted "AFTER `graph_nodes` and BEFORE `card_sources`".

**Current reality** (`bot/services/forget_cascade.py:133-179`, verified):

```
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "qa_traces",
    "llm_synthesis_cache",
    "qa_traces_llm",
    "llm_usage_ledger",
    "digests",
    "wiki_pages",
    "wiki_revisions",
    "card_sources",            # ← DESIGN docs claim "after graph_nodes BEFORE card_sources"
    "message_entities",         #     but card_sources is at index 9, graph_nodes is at index 14 (tail of 15)
    "message_links",
    "attachments",
    "fts_rows",
    "graph_nodes",              # ← graph_nodes is ALREADY AT TAIL
)
```

The DESIGN doc's "between graph_nodes and card_sources" location is physically impossible — `card_sources` runs BEFORE `graph_nodes` in current main.

**Decision:** Butler layers go **AFTER `graph_nodes`, at the very tail** of `CASCADE_LAYER_ORDER`. Three new layer names in order:

```
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    ...                       # existing 15 layers unchanged
    "graph_nodes",            # current tail
    "butler_action_confirmations",  # NEW — confirmations first (preview hashes)
    "butler_tool_invocations",      # NEW — invocations second (response payloads)
    "butler_actions",                # NEW — actions last (audit row)
)
```

Order rationale: confirmations carry preview payload hashes (smallest privacy surface); invocations carry response payloads (Telegram message ids — masked to redact text); the parent `butler_actions` row is touched last so its `status` transitions reflect downstream redaction state.

`_LAYER_FUNCS` adds three new functions: `_cascade_butler_action_confirmations`, `_cascade_butler_tool_invocations`, `_cascade_butler_actions`. Each masks privacy-sensitive payload fields with `[CONTENT_REDACTED: forget_event_id={n}]` per the Phase 9 redaction format (preserves ids + structural metadata for audit continuity).

**Fixup location:** T12-01 (Wave 1 Stream A) ships the three layer functions in the same migration sprint as the new tables. `PHASE12_DESIGN.md §4.4` errata noted in §7 below.

### §4.5 Concrete DDL for the three Butler tables

**What `PHASE12_PLAN.md §5.A` provides:** column lists with type hints but no concrete CHECK constraints, no foreign-key ON DELETE actions, no partial indexes, no enum-style restrictions.

**Decision:** T12-01 ships migration **070** with the following concrete DDL (consolidated from §5.A sketches + Codex audit fixups):

```sql
-- Migration 070: butler_actions / butler_tool_invocations / butler_action_confirmations

CREATE TABLE butler_actions (
  id BIGSERIAL PRIMARY KEY,
  action_uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  parent_action_id BIGINT REFERENCES butler_actions(id) ON DELETE RESTRICT,
  requester_tg_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  action_type TEXT NOT NULL,
  status TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_manifest_version TEXT NOT NULL,
  -- M2: governance filter version is frozen at action creation time.
  -- Captures detect_policy version + CASCADE_LAYER_ORDER hash so audit
  -- replay reproduces the exact governance lens under which the plan was
  -- created. NEVER recomputed; if the version changes mid-flight the action
  -- is expired (C5/I9.e contract).
  governance_filter_version TEXT NOT NULL,
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
  requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE,
  confirmation_policy TEXT NOT NULL DEFAULT 'per_action',
  expires_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  executed_at TIMESTAMPTZ,
  undone_at TIMESTAMPTZ,
  rejection_reason TEXT,
  error_code TEXT,
  error_context JSONB,
  llm_usage_ledger_id BIGINT REFERENCES llm_usage_ledger(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_butler_actions_status CHECK (status IN (
    'requested','evidence_loaded','planned','pending_confirmation',
    'confirmed','executing','succeeded',
    'undo_pending','undo_succeeded','undo_failed',
    'rejected','expired','execution_failed','cancelled'
  )),
  CONSTRAINT ck_butler_actions_tool_name CHECK (tool_name IN (
    'recall_evidence','schedule_meeting','send_intro',
    'update_intro','suggest_card_creation'
  )),
  CONSTRAINT ck_butler_actions_rollback_kind CHECK (rollback_kind IN (
    'delete_message','edit_message','followup_correction',
    'cancel_pending','not_reversible'
  )),
  CONSTRAINT ck_butler_actions_risk_level CHECK (risk_level IN ('low','medium','high')),
  -- H4: 'session_wide' opt-in explicitly rejected by PHASE12_PLAN.md §2; drop
  -- from CHECK enum. Allowed values: 'per_action' (default) and
  -- 'opt_in_by_button' (UX hint — still per-action audit row, the button is
  -- just a one-tap confirmation affordance).
  CONSTRAINT ck_butler_actions_confirmation_policy CHECK (confirmation_policy IN (
    'per_action','opt_in_by_button'
  )),
  CONSTRAINT ck_butler_actions_executed_has_inverse CHECK (
    (status NOT IN ('succeeded','undo_pending','undo_succeeded'))
    OR (inverse_op_payload IS NOT NULL OR rollback_kind = 'not_reversible')
  ),
  -- C4 (Codex CRITICAL #2 reconcile w/ G3.c): once Butler has spent budget on
  -- planning (any status from 'planned' onward, including post-success states
  -- 'undo_pending'/'undo_succeeded'), the linked ledger row MUST exist.
  -- ON DELETE RESTRICT on the FK (above) prevents the row vanishing.
  -- NULL `llm_usage_ledger_id` is allowed ONLY for the three pre-LLM-call
  -- terminal states: 'rejected', 'expired', 'cancelled' (rate-bucket exceed,
  -- TTL expiry without confirmation, or explicit /butler_cancel before plan).
  -- Whitelist form chosen over NOT-IN form so future success-side states
  -- (e.g. 'partially_executed') do not silently inherit NULL ledger.
  CONSTRAINT ck_butler_actions_ledger_required_post_plan
    CHECK (status IN ('rejected','expired','cancelled')
           OR llm_usage_ledger_id IS NOT NULL),
  -- M1 + restated tool_name enum (see ck_butler_actions_tool_name above) — the
  -- action_type column has a smaller user-facing taxonomy than tool_name (a
  -- single action_type can map to multiple tool invocations under the hood).
  CONSTRAINT ck_butler_actions_action_type
    CHECK (action_type IN ('meeting','intro','intro_update','card_suggestion','recall'))
);

CREATE INDEX ix_butler_actions_requester_created ON butler_actions(requester_tg_id, created_at DESC);
CREATE INDEX ix_butler_actions_chat_created ON butler_actions(chat_id, created_at DESC);
CREATE INDEX ix_butler_actions_status_expires ON butler_actions(status, expires_at)
  WHERE status IN ('pending_confirmation','planned');  -- TTL worker scan
CREATE INDEX ix_butler_actions_parent ON butler_actions(parent_action_id)
  WHERE parent_action_id IS NOT NULL;
CREATE INDEX ix_butler_actions_llm_ledger ON butler_actions(llm_usage_ledger_id)
  WHERE llm_usage_ledger_id IS NOT NULL;


CREATE TABLE butler_tool_invocations (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT NOT NULL REFERENCES butler_actions(id) ON DELETE RESTRICT,
  tool_name TEXT NOT NULL,
  invocation_seq INT NOT NULL DEFAULT 1,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_payload JSONB NOT NULL,
  request_payload_hash TEXT NOT NULL,
  response_payload JSONB,
  response_payload_hash TEXT,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error_code TEXT,
  error_context JSONB,

  CONSTRAINT ck_butler_tool_invocations_tool_name CHECK (tool_name IN (
    'recall_evidence','schedule_meeting','send_intro',
    'update_intro','suggest_card_creation'
  )),
  CONSTRAINT ck_butler_tool_invocations_status CHECK (status IN (
    'pending','running','succeeded','failed','rolled_back'
  )),
  CONSTRAINT ck_butler_tool_invocations_seq_positive CHECK (invocation_seq >= 1)
);

CREATE INDEX ix_butler_tool_invocations_action ON butler_tool_invocations(action_id);
CREATE INDEX ix_butler_tool_invocations_status ON butler_tool_invocations(status);


CREATE TABLE butler_action_confirmations (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT NOT NULL REFERENCES butler_actions(id) ON DELETE RESTRICT,
  confirmer_tg_id BIGINT NOT NULL,
  confirmation_role TEXT NOT NULL,
  status TEXT NOT NULL,
  confirmation_message_chat_id BIGINT,
  confirmation_message_id BIGINT,
  preview_payload_hash TEXT NOT NULL,
  confirmed_at TIMESTAMPTZ,
  rejected_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_butler_action_confirmations_role CHECK (confirmation_role IN (
    'requester','affected_user','admin','rollback_requester'
  )),
  CONSTRAINT ck_butler_action_confirmations_status CHECK (status IN (
    'pending','confirmed','rejected','expired','cancelled'
  ))
);

CREATE INDEX ix_butler_action_confirmations_action ON butler_action_confirmations(action_id);
CREATE INDEX ix_butler_action_confirmations_status_expires ON butler_action_confirmations(status, expires_at)
  WHERE status = 'pending';
```

Migration 071 (T12-01b, same sprint): extend `llm_usage_ledger.call_type` allow-list via DB CHECK (see §5.3).

Migration 072 (T12-01c, same sprint): `butler_rate_buckets` table (see §5.2 below).

Migration 073 (T12-01d, same sprint): `butler_card_suggestions` mapping table (see §4.6 below). This is a TIER-1 (BLOCKER) audit row: every `suggest_card_creation` invocation MUST atomically write one `butler_card_suggestions` row + one `extraction_candidates` row (the latter via the existing Phase 6 admin-review surface). The mapping table preserves Butler-side audit linkage even if the candidate is later archived; FK `extraction_candidate_id` is NULLABLE because the candidate row can be lazily created via the Phase 6 admin handler in some flows (`suggest_card_creation.execute` writes the mapping first, candidate creation may follow asynchronously).

### §4.6 `butler_card_suggestions` mapping table (BLOCKER, promoted from MEDIUM)

Concrete DDL for migration **073** (T12-01d, same sprint as 070+071+072):

```sql
-- Migration 073: butler_card_suggestions mapping between Butler audit row and
-- Phase 6 admin-review queue. UNIQUE on butler_action_id — one Butler /butler
-- request creates exactly one suggestion row (the LLM may emit multiple
-- ButlerActionStep items in a single ButlerPlan, but only the
-- suggest_card_creation tool writes a row here).

CREATE TABLE butler_card_suggestions (
  id BIGSERIAL PRIMARY KEY,
  butler_action_id BIGINT NOT NULL REFERENCES butler_actions(id) ON DELETE RESTRICT,
  extraction_candidate_id BIGINT REFERENCES extraction_candidates(id) ON DELETE SET NULL,
  suggested_card_payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

  CONSTRAINT uq_butler_card_suggestions_action UNIQUE (butler_action_id)
);

-- Partial index: only NOT NULL rows are queried by the Phase 6 admin reviewer
-- (rows with NULL extraction_candidate_id are awaiting downstream candidate
-- creation; once linked, the partial index covers admin-review lookups).
CREATE INDEX ix_butler_card_suggestions_candidate
  ON butler_card_suggestions(extraction_candidate_id)
  WHERE extraction_candidate_id IS NOT NULL;

CREATE INDEX ix_butler_card_suggestions_created
  ON butler_card_suggestions(created_at);
```

`extraction_candidate_id` ON DELETE SET NULL preserves Butler audit if a candidate is later purged via Phase 6 admin tooling. The mapping table itself participates in the cascade only via its parent `butler_actions` FK (RESTRICT). Phase 6 admin-review flow is unchanged: the admin sees a normal `extraction_candidates` row; the Butler linkage is invisible at that surface but available for audit replay.

**Fixup location:** T12-01 (Wave 1 Stream A). Single sprint, FOUR sequential migrations 070+071+072+073.

---

## §5. HIGH fixups

### §5.1 Migration window reservation

**Current reality** (`ORCHESTRATOR_REGISTRY.md §2`): Orchestrator B owns "alembic versions `050_*.py` through `069_*.py`". Phase 9 consumed 050-055; Phase 10 consumed 060-068. Only `069` is free.

**Decision:** Extend Orchestrator B's owned range to **050-099** (was `050-069`). Phase 12 explicitly reserves **070-073** (audit triple + call_type CHECK + rate_buckets + card_suggestions). **074-079** is Phase 12 hotfix/follow-up buffer. **080-099** is Phase 12.5+ runway under Orchestrator B without another registry edit. Update `ORCHESTRATOR_REGISTRY.md §2 Orchestrator B exclusive write` row to read:

```
alembic versions `050_*.py` through `099_*.py`
  (Phase 9: 050-055 — CLOSED 2026-05-19;
   Phase 10: 060-068 — CLOSED 2026-05-21;
   Phase 12: 070-073 reserved — Sprint 0 ratification 2026-05-25, execution starts T12-01
     (070 audit triple, 071 call_type CHECK, 072 rate_buckets, 073 card_suggestions);
   074-079 = Phase 12 hotfix/follow-up buffer;
   080-099 = Phase 12.5+ runway, no further registry edit required)
```

Phase 12 starts at **070**. Migration 069 (single slot between Phase 10 closure and Phase 12 reservation) is left unclaimed and may be consumed by a Phase 10.5 carryover if needed.

**Fixup location:** `ORCHESTRATOR_REGISTRY.md §2` edit, committed as part of this Sprint 0 PR.

### §5.2 Butler cost guard + `butler_rate_buckets`

**Current reality:** `bot/services/llm_gateway.py::_budget_check` (line 871) reads daily/monthly totals via `LedgerRepo.daily_cost_usd` / `monthly_cost_usd`, optionally filtered by `call_type`. The repo already supports `call_type` filtering (`bot/db/repos/llm_usage_ledger.py:80`). Per-user and per-chat caps do NOT exist today.

**Decision:** T12-08 ships `bot/services/butler_budget.py` that extends the cost guard pattern:

1. Reads daily Butler spend filtered by `call_type IN ('butler_decision','butler_summary')`:
   - Global daily ceiling `BUTLER_DAILY_USD_CEILING` (default Decimal("1.00")).
   - Global monthly ceiling `BUTLER_MONTHLY_USD_CEILING` (default Decimal("10.00")).
2. Reads per-user spend via JOIN `llm_usage_ledger` ↔ `butler_actions` on `llm_usage_ledger_id`, filtered by `butler_actions.requester_tg_id`:
   - Per-user daily ceiling `BUTLER_PER_USER_DAILY_USD_CEILING` (default Decimal("0.20")).
3. Reads per-action spend (single placeholder row before provider call):
   - Per-action ceiling `BUTLER_PER_ACTION_USD_CEILING` (default Decimal("0.10")) — checked against estimated cost from `_estimate_cost`.

Rate-bucket storage (`butler_rate_buckets`), migration **072**:

H2 (Codex HIGH) reconciliation — **CALENDAR buckets** (not rolling) and **atomic ON CONFLICT upsert**. Calendar windows are simpler, match Phase 7/8 daily window convention (`digest_daily_job` 09:00 MSK), and eliminate the leader-election race-condition class that a rolling window would introduce. Per-hour limits use `hour:{YYYY-MM-DD-HH}` keys (MSK).

```sql
CREATE TABLE butler_rate_buckets (
  id BIGSERIAL PRIMARY KEY,
  bucket_kind TEXT NOT NULL,          -- 'user_plans_day' | 'user_execs_day' | 'chat_actions_day' | 'tool_hour:{tool_name}'
  scope_id BIGINT NOT NULL,           -- tg_id for user_* buckets, chat_id for chat_* buckets, tg_id for tool buckets
  bucket_key TEXT NOT NULL,           -- 'day:{YYYY-MM-DD}' or 'hour:{YYYY-MM-DD-HH}' (MSK calendar)
  window_start TIMESTAMPTZ NOT NULL,  -- explicit MSK calendar boundary
  window_end TIMESTAMPTZ NOT NULL,    -- explicit MSK calendar boundary
  count INT NOT NULL DEFAULT 0,
  ceiling INT NOT NULL,                -- per-bucket ceiling captured at creation
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_butler_rate_buckets_kind CHECK (bucket_kind IN (
    'user_plans_day','user_execs_day','chat_actions_day',
    'tool_hour:recall_evidence','tool_hour:schedule_meeting',
    'tool_hour:send_intro','tool_hour:update_intro','tool_hour:suggest_card_creation'
  )),
  CONSTRAINT ck_butler_rate_buckets_window_positive CHECK (window_end > window_start),
  CONSTRAINT ck_butler_rate_buckets_count_nonneg_under_ceiling
    CHECK (count >= 0 AND count <= ceiling),
  CONSTRAINT ck_butler_rate_buckets_ceiling_positive CHECK (ceiling > 0),

  -- H2: UNIQUE on (kind, scope, key) makes the ON CONFLICT upsert atomic.
  CONSTRAINT uq_butler_rate_buckets_kind_scope_key UNIQUE (bucket_kind, scope_id, bucket_key)
);

CREATE INDEX ix_butler_rate_buckets_window_end ON butler_rate_buckets(window_end);
```

**Atomic upsert pattern** — single SQL statement, no read-then-write race:

```sql
INSERT INTO butler_rate_buckets (
    bucket_kind, scope_id, bucket_key, window_start, window_end, count, ceiling
)
VALUES (:kind, :scope_id, :bucket_key, :win_start, :win_end, 1, :ceiling)
ON CONFLICT (bucket_kind, scope_id, bucket_key) DO UPDATE
SET count = butler_rate_buckets.count + 1,
    updated_at = NOW()
WHERE butler_rate_buckets.count < butler_rate_buckets.ceiling
RETURNING id, count, ceiling;
```

Empty `RETURNING` → the partial-update WHERE clause filtered the row (ceiling already reached) → caller rejects the action. Non-empty `RETURNING` → bucket count is durably incremented within the same transaction as the `butler_actions` insert (single-statement atomicity, no separate `SELECT FOR UPDATE`). Bucket calendar boundaries align to MSK timezone for consistency with `digest_daily_job` 09:00 MSK convention; window keys are deterministic per-(MSK-day) or per-(MSK-hour) so concurrent inserts always converge on the same row. Rows older than `window_end + 24h` are reaped by a once-daily cleanup tick added to the existing scheduler (`bot/services/scheduler.py`).

**Fixup location:** T12-01 (migration 072), T12-08 (rate-bucket repo + budget guard wiring).

### §5.3 Ledger call_type allow-list (constants + DB CHECK)

**Current reality** (verified):

- `bot/db/repos/llm_usage_ledger.py:52` comment lists allowed values: `'unknown'`, `'qa_synthesis'`, `'digest_daily'`, `'digest_weekly'`, `'graph_projection'`.
- `bot/db/models.py:867-869` docstring lists same five values.
- `bot/services/graph_common.py:88` `RESERVED_LEDGER_CALL_TYPES` tuple: `('graph_projection', 'extract_candidates')`.
- Migration 064 added the `call_type` column with `server_default='unknown'` and a composite index — **NO CHECK constraint** (verified by reading the migration file).

**Decision:**

1. T12-03 extends the application-level allow-list in **two places**:
   - `bot/db/repos/llm_usage_ledger.py:52` docstring comment: add `'butler_decision'`, `'butler_summary'`, `'extract_candidates'` (last one was already in `RESERVED_LEDGER_CALL_TYPES` but is missing from the repo docstring — fix the drift).
   - `bot/db/models.py:867-869` docstring: same list.
   - `bot/services/graph_common.py:88` `RESERVED_LEDGER_CALL_TYPES` tuple gains `'butler_decision'` and `'butler_summary'`.
2. T12-01b ships migration **071** that adds a DB-level CHECK constraint:

```sql
-- Migration 071: tighten llm_usage_ledger.call_type with CHECK
-- (Migration 064 added the column without a CHECK; Phase 12 ratifies the allow-list.)

ALTER TABLE llm_usage_ledger
  ADD CONSTRAINT ck_llm_usage_ledger_call_type CHECK (call_type IN (
    'unknown',
    'qa_synthesis',
    'digest_daily',
    'digest_weekly',
    'graph_projection',
    'extract_candidates',
    'butler_decision',
    'butler_summary'
  ))
  NOT VALID;

-- Validate separately so the constraint can be re-validated post-deploy
-- without blocking writes during the ALTER.
ALTER TABLE llm_usage_ledger
  VALIDATE CONSTRAINT ck_llm_usage_ledger_call_type;
```

NOT VALID + VALIDATE pattern mirrors the Phase 8 migration 038 precedent (`AUTHORIZED_SCOPE.md` line 414).

**Fixup location:** T12-01b (migration 071), T12-03 (constants + repo docstring sync).

### §5.4 Verify migration 064 has no CHECK — already verified

Codex audit claim: "DESIGN says migration 064 added CHECK".

**Reality check** (verified by reading `alembic/versions/064_add_llm_ledger_call_type.py` end-to-end): migration 064 adds COLUMN + composite INDEX only. There is no `op.create_check_constraint` call. The DESIGN doc's claim is wrong.

**Decision:** No errata to DESIGN.md (the relevant claim — that 064 already added CHECK — does not appear in DESIGN.md; the audit-finding-text was mistaken). Migration 071 (above) is the **first** CHECK constraint on `call_type`. Documented in this section to close the audit finding.

### §5.5 Sprint 0 decision gate

**Decision:** This Sprint 0 PR (PHASE12_PLAN_REFRESH.md + AUTHORIZED_SCOPE.md amendment + ORCHESTRATOR_REGISTRY.md migration window update + PHASE12_PLAN.md errata addendum) **MUST merge before any code sprint (T12-01..T12-10) opens**.

Sequencing enforcement:

1. Sprint 0 PR: `feat/p12-s0-ratification` → `main`. Dual-model spec review (Claude `deep-spec-reviewer` + Codex `deep audit`) per Rule 3. PAR evidence written.
2. After Sprint 0 merges: T12-01 worktree under `.worktrees/orch-B` may be created on branch `feat/p12-w1-t12-01`.
3. No execution sprint claims migration 070+ until Sprint 0 is on `main`.

**Fixup location:** Codified in §10 Wave plan below.

### §5.6 Phase 11 binding count

**Current reality:**

- `CLAUDE.md` line above the Phase 9 closure block: "Phase 11 binding suite expected to grow from 42 → 75+ at closure of both phases."
- `CLAUDE.md` Phase 10 closure block: "Phase 11 binding **77/77** green."
- `PHASE12_DESIGN.md §7.6` states: "Phase 11 binding suite at end of Phase 9 = 60/60. After Phase 10 lands its 18 tests = 78/78. After Phase 12 execution = 103/103."

The DESIGN doc's 78/78 figure was a forecast made before Phase 10 closure. Actual Phase 10 closure landed **77/77** (60 + 17 not 18 — one I8 sub-case folded into I8.b at execution time per CLAUDE.md Phase 10 closure block).

**Decision:** Authoritative baseline going into Phase 12 = **77/77**. Phase 12 adds 25 new tests (§12 below) → end-of-Phase-12 baseline = **102/102**.

**Fixup location:** §12 binding test contract uses 77 → 102 as the authoritative arithmetic. Errata note appended to `PHASE12_DESIGN.md §7.6` per §7 below.

### §5.7 Graph access for member/butler stance

**Current reality** (`PHASE10_PLAN.md §1` invariant 7 binding): "Phase 10 graph is admin-only; no public exposure". `PHASE12_DESIGN.md §3.2` line 194 says the Butler "may include 2-hop graph traversal results (admin scope only — R7.a is binding)".

**Decision:** Butler does **NOT** consume `graph_query` in baseline Phase 12.1–12.4. The Phase 10 admin-only stance is preserved. `ButlerEvidenceContext.bundle` contains ONLY:

- Phase 4 `message_versions` hits via `bot/services/search.py`;
- Phase 6 approved `card_sources` via `bot/db/repos/knowledge_card.py`;
- Phase 9 published wiki revisions (member-internal only).

Graph traversal results are **deferred to Phase 12.5+** alongside group-chat surface. The `G3.a` binding test (§12.5 below) asserts that no Phase 12 baseline code path imports `bot.services.graph_query`.

**Fixup location:** T12-02 (`recall_evidence` implementation excludes graph_query), G3.a binding test (T12-09). `PHASE12_DESIGN.md §3.2` line 194 graph_query entry gets errata note (see §7).

### §5.8 EvidenceItem shape vs DESIGN

**Current reality** (`bot/services/evidence.py:35-52`):

```python
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime
    source_type: Literal["message", "card"] = "message"
    card_id: uuid.UUID | None = None
    card_source_message_version_ids: tuple[int, ...] = ()
```

**Decision:** `ButlerEvidenceContext` (§4.2) wraps an `EvidenceBundle` whose `items: tuple[EvidenceItem, ...]` carries the above shape. The Butler tool layer consumes these exact fields. Citation tokens in Butler outgoing text use `[^mv:<message_version_id>]` for `source_type='message'` items and `[^card:<knowledge_cards.id>]` for `source_type='card'` items (mirrors `bot/services/wiki_renderer.py:84-87` token shape — the UUID is the `knowledge_cards.id`, not the `card_sources.id`).

The T12-02 acceptance criterion specifies this verbatim so the implementer does not invent a parallel shape.

---

## §6. MEDIUM fixups (consolidated)

- **`governance.detect_policy` scans 6 fields** (`bot/services/governance.py:46`): `text`, `caption`, `poll_question`, `contact_name`, `forward_text`, `forward_caption`. T12-09 Butler evidence tests must include negative cases on all six fields — L11.b test family parameterized over the six fields ensures `#nomem`/`#offrecord` in any field excludes the message from `ButlerEvidenceContext`.
- **`suggest_card_creation` provenance.** `bot/db/models.py:1002` `ExtractionCandidate` has no Butler-provenance column. T12-06 `SuggestCardCreationTool.execute` writes a Butler suggestion into `extraction_candidates` (status `pending`) and ALSO writes a row into the `butler_card_suggestions` mapping table — DDL is in §4.6 above (promoted from MEDIUM to BLOCKER tier; migration **073** ratified there). Phase 6 admin review flow unchanged; the admin sees a normal candidate row while the mapping preserves Butler-side audit linkage.
- **Privacy lint + import-scan extensions** (M7 — split clearly into two mechanisms):
  - **(a) Runtime httpx guard.** `tests/evals/conftest.py:38` `httpx_llm_guard` autouse fixture (Phase 11 follow-up #224 High #5) covers every eval test that exercises Butler paths automatically — NO change needed. The guard's `LLM_PROVIDER_HOSTNAMES` URL set (`tests/evals/_llm_guard.py`) is the single source of truth shared with the AST scan, so the two cannot drift.
  - **(b) AST import scan extension.** T12-09 extends `tests/evals/test_no_llm_imports.py` via the new C8 mechanism: a `forbidden_map: dict[str, frozenset[str]]` helper layered on top of the existing global `LLM_PROVIDER_PREFIXES` scan. Map entries added by T12-09 PR cover `bot/services/butler*.py`, `bot/services/butler_tools/*.py`, and `bot/handlers/butler.py` — each with `frozenset({'anthropic', 'openai', 'bot.services.graph_query'})`. `bot.services.graph_query` exclusion preserves Phase 10 admin-only stance (§5.7).
  - **(c) Privacy literal lint** (`scripts/lint_privacy_check.sh`). T12-09 adds path entries for Butler test files that legitimately name the canonical privacy literals (`#nomem` / `#offrecord` / `forgotten` / `forget`) in docstrings + assertion messages — same rationale as the existing `test_digest_leakage.py` / `test_wiki_leakage.py` allowlist entries.
  - G3.a binding test (§12.5) asserts the AST scan covers `bot/services/butler*.py` + `bot/handlers/butler.py` + `bot/services/butler_tools/*.py` glob expansions.
- **Graph test fakes.** Even though Butler does not consume `graph_query`, any shared fixture under `tests/evals/conftest.py` that touches graph infrastructure must use `bot.services.graph_adapter.NetworkXAdapter` (the Phase 10 unit-test fake at `AUTHORIZED_SCOPE.md` line 243) not real Neo4j. Documented as a hygiene note in T12-09 acceptance.

---

## §7. Authoritative artefacts contract

Sprint 0 PR `feat/p12-s0-ratification` delivers exactly these artefacts:

| # | Artefact | Path | Action |
|---|----------|------|--------|
| 1 | Sprint 0 refresh spec (this file) | `docs/memory-system/PHASE12_PLAN_REFRESH.md` | NEW |
| 2 | AUTHORIZED_SCOPE amendment | `docs/memory-system/AUTHORIZED_SCOPE.md` | EDIT — insert new "## Authorized: Phase 12 …" block before "## NOT authorized" (line 471). Verbatim block in §8. |
| 3 | ORCHESTRATOR_REGISTRY migration window | `docs/memory-system/ORCHESTRATOR_REGISTRY.md` §2 Orchestrator B row | EDIT — extend owned range to **050-099** (Phase 12 reserves 070-073, 074-079 hotfix buffer, 080-099 Phase 12.5+ runway) with phase-by-phase annotation per §9 below. |
| 4 | PHASE12_PLAN errata addendum | `docs/memory-system/PHASE12_PLAN.md` | APPEND a new "## §12. Errata (added 2026-05-25, Sprint 0 refresh)" section after current §11 with the four errata items below; PLUS a single errata-note line at §0 referencing this refresh for the `EvidenceContext` rename audit (§7.2 below). |
| 5 | PHASE12_DESIGN errata addendum | `docs/memory-system/PHASE12_DESIGN.md` | APPEND a new "## §14. Errata (added 2026-05-25, Sprint 0 refresh)" section after §13 with the two errata items below; PLUS a single errata-note line at §0 referencing this refresh for the `EvidenceContext` rename audit (§7.2 below). |
| 6 | lint-privacy allowlist extension | `scripts/lint_privacy_check.sh` | EDIT — extend the existing path-allowlist with a new branch `[[ "$path" =~ ^docs/memory-system/PHASE[0-9]+_PLAN(_REFRESH)?\.md$ ]] && return 0`, replacing the current `PHASE[0-9]+_PLAN\.md` branch. Required because this refresh doc legitimately names `#nomem`, `#offrecord`, `forgotten`, `forget` and would otherwise fail the CI gate on its own merge. See §11 DoD note 10 for the "single authorized script edit" carve-out. |

**Errata items for `PHASE12_PLAN.md` §12:**

1. §1 invariant 7 binding interpretation references `EvidenceContext` — corrected: the canonical contract is `ButlerEvidenceContext` (sealed wrapper around `bot/services/evidence.py::EvidenceBundle`). See `PHASE12_PLAN_REFRESH.md §4.2`.
2. §5.A schema sketches are upgraded to concrete DDL with CHECK constraints + foreign-key ON DELETE actions in migration 070. See `PHASE12_PLAN_REFRESH.md §4.5`.
3. §5.F EvidenceContext shape: `source_type` is `Literal["message", "card"]` (verified against `bot/services/evidence.py:50`), not `"message_version|approved_card"`. The field name is correct in spirit; the literal values differ.
4. §6 Wave structure remains valid; concrete sprint sequencing (Sprint 0 + 10 execution sprints) is enumerated in `PHASE12_PLAN_REFRESH.md §10`.

**Errata items for `PHASE12_DESIGN.md` §14:**

1. §4.4 CASCADE_LAYER_ORDER position: Butler layers go AFTER `graph_nodes` at the very tail, not between `graph_nodes` and `card_sources`. Verified against `bot/services/forget_cascade.py:133-179` — `graph_nodes` is at index 14 (tail of 15 layers). See `PHASE12_PLAN_REFRESH.md §4.4`.
2. §7.6 binding test count: Phase 10 actually closed at 77/77 (CLAUDE.md Phase 10 closure block), not 78/78 forecast. Phase 12 baseline is 77 → 102.

### §7.2 EvidenceContext rename — exhaustive occurrence audit

The `EvidenceContext` symbol does not exist in code (verified: zero grep hits under `bot/`, `tests/`). The two design docs collectively contain 29 textual references that are now superseded by `EvidenceBundle` (the actual class from `bot/services/evidence.py:122-160`) wrapped as `ButlerEvidenceContext` per §4.2 above. Per Sprint 0's docs-only minimal-diff principle, the original docs are NOT edited line-by-line — instead, each receives a single errata-note line at §0 pointing here.

| File | Occurrences (literal `EvidenceContext` matches) | Action |
|---|---|---|
| `docs/memory-system/PHASE12_PLAN.md` | 13 (`grep -c EvidenceContext PHASE12_PLAN.md`) | ERRATA: superseded by `EvidenceBundle` (concrete class) wrapped as `ButlerEvidenceContext` (sealed wrapper). Single errata-note line added at §0 of `PHASE12_PLAN.md` pointing at `PHASE12_PLAN_REFRESH.md §4.2`. Original §-body lines remain untouched (`PHASE12_PLAN.md` was ratified 2026-05-02 — body lines are historical record). |
| `docs/memory-system/PHASE12_DESIGN.md` | 16 (`grep -c EvidenceContext PHASE12_DESIGN.md`) | Same — single errata-note line added at §0 pointing at `PHASE12_PLAN_REFRESH.md §4.2`. Body lines remain untouched. |

**Verbatim errata-note text** (identical for both PLAN/DESIGN §0):

> **2026-05-25 ERRATUM:** All references to `EvidenceContext` in this document are superseded by `EvidenceBundle` from `bot/services/evidence.py:122-160`, optionally wrapped as `ButlerEvidenceContext` for Butler-specific metadata. See `PHASE12_PLAN_REFRESH.md §4.2` for the canonical rename and §7.2 for this audit.

The §-body lines are preserved as historical record; binding interpretation flows through this refresh.

---

## §8. AUTHORIZED_SCOPE.md amendment text (verbatim, ready to copy)

The following block is inserted into `docs/memory-system/AUTHORIZED_SCOPE.md` **before** line 471 (the "## NOT authorized" header), and the existing "## Authorized: Phase 12 — Butler design docs only (2026-04-30)" block at lines 113-122 is retained unchanged (it describes the design-docs-only step that has already happened).

```markdown
## Authorized: Phase 12 — Future Butler / Action Layer (2026-05-25)

Phase 12 authorized for implementation following Phase 9 (CLOSED 2026-05-19) and
Phase 10 (CLOSED 2026-05-21) closures. Predecessor gates ALL closed: Phases 0-11.
Owned by Orchestrator B per `ORCHESTRATOR_REGISTRY.md §2`. Canonical plan:
`docs/memory-system/PHASE12_PLAN.md` (ratified 2026-05-02) + companion
`docs/memory-system/PHASE12_DESIGN.md` (DESIGN-ONLY 2026-05-19) + Sprint 0
refresh `docs/memory-system/PHASE12_PLAN_REFRESH.md` (ratified 2026-05-25,
this block).

Authorized scope (per `PHASE12_PLAN_REFRESH.md`):

- 5 new tables (migrations 070-073): `butler_actions`, `butler_tool_invocations`,
  `butler_action_confirmations` (all three in migration 070), `butler_rate_buckets`
  (migration 072), `butler_card_suggestions` (migration 073). All DDL includes CHECK
  constraints on `status` / `tool_name` / `action_type` / `rollback_kind` /
  `risk_level` / `confirmation_policy` / `confirmation_role` / `bucket_kind`
  enum-style columns. Full DDL in `PHASE12_PLAN_REFRESH.md §4.5` (070 triple) +
  §4.6 (073 mapping) + §5.2 (072 rate buckets).
- Migration 071 adds DB CHECK constraint to `llm_usage_ledger.call_type` extending
  the allow-list to include `'butler_decision'` + `'butler_summary'` + `'extract_candidates'`
  (latter closes a pre-existing docstring drift). NOT VALID + VALIDATE pattern.
- New service modules under `bot/services/`: `butler.py` (state-machine
  orchestrator), `butler_evidence.py` (sealed `ButlerEvidenceContext` wrapper
  around `EvidenceBundle`), `butler_budget.py` (cost guard + rate buckets),
  `butler_tools/*.py` (5-tool registry: `recall_evidence`, `schedule_meeting`,
  `send_intro`, `update_intro`, `suggest_card_creation`).
- New gateway functions `bot/services/llm_gateway.py::plan_butler_action`
  (call_type=`butler_decision`) + `synthesize_butler_summary`
  (call_type=`butler_summary`). NO direct provider SDK calls inside any
  `bot/services/butler*.py` or `bot/handlers/butler.py` — enforced by lint
  + AST scan (G3.a binding).
- New Telegram handler `bot/handlers/butler.py` with commands `/butler`,
  `/butler_status`, `/butler_cancel`, `/butler_undo`. DM-only baseline
  (§3.1 surface decision). Membership gate via Phase 0 pattern:
  `user = await UserRepo.get(session, message.from_user.id)` then
  `if user is None or not (user.is_member or user.is_admin): refuse`
  (mirrors `bot/handlers/qa.py:369` + `bot/handlers/forward_lookup.py:46`).
  Per-action confirmation via inline keyboards. Cross-user consent
  unbypassable (no admin override).
- Forget cascade extension: three new layers
  `_cascade_butler_action_confirmations` + `_cascade_butler_tool_invocations`
  + `_cascade_butler_actions` appended to the tail of `CASCADE_LAYER_ORDER`
  (AFTER `graph_nodes`). All three mask privacy-sensitive payload fields
  with `[CONTENT_REDACTED: forget_event_id={n}]` per Phase 9 redaction format.
- Per-PR PAR (Claude product + Codex technical) on each of 10 execution sprints.
  FHR mandatory after T12-10 (governance_mode=critical + 10 sprints + privacy
  invariants binding triggers superflow Rule 9).
- 5 feature flags all default OFF (1 master + 4 per-tool), layered per substep §10 of refresh:
  `memory.butler.enabled` (parent), `memory.butler.schedule_meeting.enabled`,
  `memory.butler.send_intro.enabled` + `memory.butler.update_intro.enabled`,
  `memory.butler.suggest_card.enabled`.
- Cost ceilings — `BUTLER_DAILY_USD_CEILING` $1.00/day GLOBAL, `BUTLER_PER_USER_DAILY_USD_CEILING`
  $0.20/day per user, `BUTLER_PER_ACTION_USD_CEILING` $0.10/action,
  `BUTLER_MONTHLY_USD_CEILING` $10.00/month GLOBAL. Independent of all other Phase 5/7/8/10
  buckets. Enforced by `bot/services/butler_budget.py` via filtered ledger SUM
  (`LedgerRepo.daily_cost_usd(call_type=...)` + `monthly_cost_usd(call_type=...)` after
  C9 extension) + `butler_rate_buckets` table for per-user/chat/tool rate caps.
  Per-chat caps are RATE-limited (action counts) not COST-limited (USD) — see §14.2.
- Rate envelopes — 10 plans/user/day, 5 executions/user/day, 50 actions/chat/day,
  per-tool hour limits (`send_intro`:3, `update_intro`:5, `schedule_meeting`:5,
  `suggest_card_creation`:10, `recall_evidence`:30).
- Plan TTL: 15 min low-risk / 5 min cross-user intro / 30 min admin-review suggestion.
  Confirmation token TTL: 5 min (inline-keyboard freshness). Evidence snapshot TTL:
  30 min with cascade-aware revalidation pre-execute (fail-closed on forget event
  during TTL window per §3.6 of refresh).
- Phase 11 binding suite expansion: 25 new tests (L11.a-e + C10.a-c + I9.a-f +
  R8.a-g + G3.a-d) → 77/77 → 102/102.
- 11 sprints — Sprint 0 (this PR, docs only) + T12-01..T12-10. Sprint 0 must
  merge before any execution sprint opens.

NOT in Phase 12 baseline (deferred to Phase 12.5+ per §3 of refresh):

- Group-chat surface (`/butler` in public community chats) — DM-only baseline.
- Cron / scheduled triggers / proactive nudges — user-initiated only.
- Per-user opt-in flag — relies on existing `#nomem` / `#offrecord` / `/forget`
  controls.
- Admin override of cross-user consent — consent is unbypassable in baseline.
- Session-wide opt-in — explicitly rejected by `PHASE12_PLAN.md §2`.
- `/butler --dry-run` flag — confirmation preview IS the dry-run (see
  `PHASE12_PLAN.md §11.3`).
- Operator dashboard surface — `PHASE12_PLAN.md §5.G` "out of scope".
- Graph projection consumption — Phase 10 admin-only stance preserved; Butler
  does NOT consume `bot/services/graph_query.py` in baseline (G3.a binding).
```

---

## §9. ORCHESTRATOR_REGISTRY.md amendment text

The Orchestrator B row in `ORCHESTRATOR_REGISTRY.md §2 Orchestrator B exclusive write` is updated. Replace the existing migration-range line with:

```
- alembic versions `050_*.py` through `099_*.py` (extended range reserves Phase 12 + 12.5+ runway).
  Phase 9 consumed 050-055 — CLOSED 2026-05-19.
  Phase 10 consumed 060-068 — CLOSED 2026-05-21.
  Phase 12 reserves 070-073 — Sprint 0 ratification 2026-05-25 (`PHASE12_PLAN_REFRESH.md`).
  074-079 = Phase 12 hotfix/follow-up buffer.
  080-099 = Phase 12.5+ runway (no further registry edit required).
  069 unclaimed; available for Phase 10.5 carryovers if any.
```

The `§1 Active orchestrators` row for Orchestrator B is also updated (replace `050–069` with `050–079` and remove the conditional clause):

```
| B — Lateral expansion | Phase 9 (wiki) CLOSED + Phase 10 (graph) CLOSED + Phase 12 (butler) AUTHORIZED | feat/p9-*, feat/p10-*, feat/p12-*, fix/p{9,10,12}-*, plan/p{9,10,12}-* | .worktrees/orch-B | 050–079 | TBD |
```

---

## §10. Wave / Sprint plan (refresh)

11 sprints total, ordered Sprint 0 → Wave 1 → Wave 2 → Wave 3 → Wave 4.

### Sprint 0 — Ratification (Stream Pre)

| Ticket | Scope | Branch | Worktree | PR target | Reviewers |
|---|---|---|---|---|---|
| (none) | PHASE12_PLAN_REFRESH.md + AUTHORIZED_SCOPE amendment + ORCHESTRATOR_REGISTRY edit + PHASE12_PLAN errata + PHASE12_DESIGN errata | `feat/p12-s0-ratification` | `.worktrees/orch-B-s0` (if not already present) | `main` | Claude `deep-spec-reviewer` + Codex `deep audit` |

Sprint 0 ships **THIS PR**. Docs only. No source code. No migrations. CI green required before merge.

### Wave 1 — Foundations in parallel (3 sprints, all START AFTER Sprint 0 merge)

| Ticket | Scope | Deps |
|---|---|---|
| T12-01 | 4 migrations — **070** (audit triple — `butler_actions` + `butler_tool_invocations` + `butler_action_confirmations`), **071** (`llm_usage_ledger.call_type` CHECK constraint adding `butler_decision` + `butler_summary` + `extract_candidates`), **072** (`butler_rate_buckets`), **073** (`butler_card_suggestions` mapping). ORM models in `bot/db/models.py`. Repos under `bot/db/repos/butler_*.py`. Forget-cascade extension (3 new layer functions in `bot/services/forget_cascade.py`). | none beyond Sprint 0 |
| T12-02 | `bot/services/butler_evidence.py::ButlerEvidenceContext` wrapper. `bot/services/butler_tools/recall_evidence.py` delegates to existing Phase 4/6/9 evidence path (no graph). Tests cover L11.a-e governance pre-filter coverage on all six `detect_policy` fields. | Sprint 0 |
| T12-03 | Tool registry `bot/services/butler_tools/__init__.py::ALLOWED_BUTLER_TOOLS` + 5 tool schemas (pydantic args models). **Defines `ButlerPlan` pydantic model** in `bot/services/butler_tools/__init__.py` (M4) with fields: `plan_summary: str`, `evidence_ids: list[int]`, `actions: list[ButlerActionStep]`. `ButlerActionStep` model has: `tool_name: Literal["recall_evidence","schedule_meeting","send_intro","update_intro","suggest_card_creation"]`, `args: dict`, `requires_confirmation: bool`, `affected_user_ids: list[int]`, `risk_level: Literal["low","medium","high"]`, `rollback_kind: Literal["delete_message","edit_message","followup_correction","cancel_pending","not_reversible"]`, `inverse_op_payload: dict \| None`. LLM gateway extensions `plan_butler_action` (returns validated `ButlerPlan`) + `synthesize_butler_summary` (returns `str`, no plan structure) — both private/no-retry/single-ledger-row. `bot/services/graph_common.py:88` `RESERVED_LEDGER_CALL_TYPES` extension to include `'butler_decision'` + `'butler_summary'`. | Sprint 0 |

### Wave 2 — Orchestration + UI (3 sprints, T12-04 sequential, T12-05+T12-06 parallel after T12-04)

| Ticket | Scope | Deps |
|---|---|---|
| T12-04 | `bot/services/butler.py::ButlerService` state machine. Implements `request_plan` → `confirm_action` → `cancel_action` → `undo_action` → `expire_pending_actions`. Validates `ButlerPlan` against whitelist + evidence + schema. Writes `butler_actions` + `butler_action_confirmations` + (on execute) `butler_tool_invocations` audit rows. Fail-closed on all 8 rejection paths from `PHASE12_PLAN.md §5.B`. | T12-01, T12-02, T12-03 |
| T12-05 | `bot/handlers/butler.py` with `/butler`, `/butler_status`, `/butler_cancel`, `/butler_undo`. Inline keyboards with opaque callback tokens (`butler_action_id` + signed expiry). Cross-user consent prompts to affected users. Visibility-scoped preview rendering. Feature flag `memory.butler.enabled` (default OFF). | T12-04 |
| T12-06 | 5 tool implementations under `bot/services/butler_tools/`: `recall_evidence`, `schedule_meeting`, `send_intro`, `update_intro`, `suggest_card_creation`. Each implements the `ButlerTool` Protocol (`validate_policy` + `execute` + `build_inverse`). Each writes one `butler_tool_invocations` row per attempt; no hidden retries. | T12-03, T12-04 |

### Wave 3 — Rollback + abuse controls (2 sprints, parallel)

| Ticket | Scope | Deps |
|---|---|---|
| T12-07 | `/butler_undo` flow + 5 rollback kinds (`delete_message`, `edit_message`, `followup_correction`, `cancel_pending`, `not_reversible`). Undo writes linked `butler_actions.parent_action_id` row. Original action audit immutable. Authorization check (only requester / affected_user / admin per action type). | T12-01, T12-05, T12-06 |
| T12-08 | `bot/services/butler_budget.py` (cost guard + rate buckets). TTL worker (extension of existing `bot/services/scheduler.py`). Cross-user consent flow wiring. Per-tool hour limits enforced via `butler_rate_buckets`. Cooldown on repeated rejected actions. **Evidence-snapshot freshness revalidation** in `confirm_action` runs the §3.6 SQL predicate verbatim (reproduced here for acceptance-test sharpness — single source of truth remains §3.6 step 2): <pre>WITH bundle_mvids AS (SELECT unnest(:evidence_mvids::bigint[]) AS mvid<br>UNION ALL SELECT unnest(:card_source_mvids::bigint[]) AS mvid)<br>SELECT mvid FROM bundle_mvids m WHERE EXISTS (<br>  SELECT 1 FROM forget_events fe JOIN message_versions mv ON mv.id=m.mvid<br>  JOIN chat_messages cm ON cm.id=mv.chat_message_id<br>  WHERE fe.status IN ('active','completed')<br>    AND fe.tombstone_key IN (<br>      format('message:%s:%s', cm.chat_id, cm.tg_message_id),<br>      format('message_hash:%s', cm.content_hash),<br>      format('user:%s', cm.from_user_id))<br>) OR EXISTS (SELECT 1 FROM chat_messages cm<br>  JOIN message_versions mv ON mv.chat_message_id=cm.id<br>  WHERE mv.id=m.mvid AND (cm.memory_policy!='normal' OR cm.is_redacted=TRUE OR mv.is_redacted=TRUE));</pre> Any returned mvid → action `status='expired'`, no Telegram side effect, audit row written. Read-side `fe.tombstone_key` 3-key prefix MUST be used — NOT `target_id` (memory `feedback-tombstone-key-read-side-convention.md`). `:card_source_mvids` is the flattened concatenation of every `card_source_message_version_ids` array across `bundle.items` where `source_type='card'`. **C9 ledger extension (REQUIRED for `BUTLER_MONTHLY_USD_CEILING`):** extend `LedgerRepo.monthly_cost_usd(session, *, month: date \| None = None, call_type: str \| None = None) -> Decimal` mirroring `daily_cost_usd` signature (`bot/db/repos/llm_usage_ledger.py:77-107`). Also extend the corresponding `LedgerRepoProtocol.monthly_cost_usd` (`bot/services/llm_gateway.py:156-158`) accordingly. No backfill — existing rows already have `call_type` set (post-migration 064 default `'unknown'` plus per-feature backfill performed in 064 itself). | T12-05, T12-06 |

### Wave 4 — Evals + closure (2 sprints, sequential)

| Ticket | Scope | Deps |
|---|---|---|
| T12-09 | Phase 11 binding suite expansion: 25 new tests (L11.a-e + C10.a-c + I9.a-f + R8.a-g + G3.a-d). New eval files under `tests/evals/test_butler_*.py`. **AST scanner extension (verbatim, single source of truth = §12.5 G3.a):** add helper `assert_no_forbidden_imports_per_path(forbidden_map: dict[str, frozenset[str]])` to `tests/evals/test_no_llm_imports.py` (extends, does NOT replace, the existing `LLM_PROVIDER_PREFIXES` global scan at lines 26-39). T12-09 PR adds map entries verbatim: `{'bot/services/butler*.py': frozenset({'anthropic','openai','bot.services.graph_query'}), 'bot/handlers/butler.py': frozenset({'anthropic','openai','bot.services.graph_query'}), 'bot/services/butler_tools/*.py': frozenset({'anthropic','openai','bot.services.graph_query'})}`. Missing glob hits flagged as harness misconfiguration (coverage cannot silently drop). `scripts/lint_privacy_check.sh` extension: append butler-test-file path entries to the existing leakage-test allowlist alongside the §11 DoD #6 `PHASE[0-9]+_PLAN(_REFRESH)?\.md` regex (one consolidated lint-privacy PR slice). Phase 11 binding goes 77/77 → 102/102. | all of T12-01..T12-08 |
| T12-10 | FHR (Claude `deep-product-reviewer` + Codex `deep technical` independent) over all 10 prior PRs (Sprint 0 + T12-01..T12-09) as a unified system. Operator runbook `docs/memory-system/PHASE12_ROLLOUT.md`. Closure updates to `CLAUDE.md` Phase 12 closure block, `ROADMAP.md`, `IMPLEMENTATION_STATUS.md`, `AUTHORIZED_SCOPE.md` CLOSED marker. | T12-09 |

**Wave diagram:**

```
Sprint 0 (this PR — docs only)
   |
   v
Wave 1:  T12-01    T12-02    T12-03      (parallel)
              \     |     /
                v   v   v
Wave 2:        T12-04            (sequential, gate for Wave 2 fan-out)
              /     \
             v       v
          T12-05    T12-06       (parallel after T12-04)
              \     /
                v v
Wave 3:  T12-07     T12-08       (parallel)
              \     /
                v v
Wave 4:        T12-09             (sequential)
                 |
                 v
              T12-10              (FHR + closure)
```

---

## §11. Definition of Done (Sprint 0)

Sprint 0 is DONE when ALL of the following hold:

1. `docs/memory-system/PHASE12_PLAN_REFRESH.md` (this file) merged to `main`.
2. `docs/memory-system/AUTHORIZED_SCOPE.md` contains the new "## Authorized: Phase 12 — Future Butler / Action Layer (2026-05-25)" block — **substantially equivalent to §8 above** (the landed block MAY re-structure §8 bullets for clarity, including an explicit `§10 design decisions` enumeration, provided NO scope item is dropped/added/substantively changed; small editorial fixes such as correcting an in-source typo are allowed). Inserted before the "## NOT authorized" section.
3. `docs/memory-system/ORCHESTRATOR_REGISTRY.md` §1 and §2 reflect the **050-099** migration window for Orchestrator B (§9 above) — Phase 12 reserves 070-073; 074-079 hotfix buffer; 080-099 Phase 12.5+ runway.
4. `docs/memory-system/PHASE12_PLAN.md` has a new §12 errata addendum (4 items per §7) PLUS a single errata-note line at §0 per §7.2.
5. `docs/memory-system/PHASE12_DESIGN.md` has a new §14 errata addendum (2 items per §7) PLUS a single errata-note line at §0 per §7.2.
6. Dual-model spec review verdicts: Claude `deep-spec-reviewer` ACCEPTED + Codex `deep audit` APPROVE. Both verdicts recorded in `.par-evidence.json` at branch root with the schema from `superflow-enforcement.md` Hard Rule 3.
7. CI green on `feat/p12-s0-ratification` (`evals.yml` privacy-binding suite + privacy lint + Phase 11 baseline all green at 77/77).
8. PR title and body follow superflow per-sprint PR convention. Body includes the Sprint 0 DoD checklist.
9. After merge, `git diff main...HEAD` on `feat/p12-s0-ratification` shows ZERO changes to any `bot/`, `tests/`, `alembic/`, `web/`, or `.github/workflows/` paths. Docs-only invariant verified.
10. **C1 single-script carve-out.** `scripts/lint_privacy_check.sh` allowlist regex is extended to `^docs/memory-system/PHASE[0-9]+_PLAN(_REFRESH)?\.md$` (replacing the current `PHASE[0-9]+_PLAN\.md` branch) as the ONE explicitly authorized exception to the docs-only invariant. Justification: this refresh doc legitimately names `#nomem`, `#offrecord`, `forgotten`, `forget` and would otherwise fail the CI gate on its own merge. The rest of `scripts/`, `bot/`, `tests/`, `alembic/`, `web/`, and `.github/workflows/` remain off-limits in Sprint 0.

---

## §12. Phase 11 binding test contract (25 new tests)

Phase 11 binding goes from 77/77 → **102/102** after T12-09 lands. The 25 new tests are grouped into 5 families. Each test has a one-line acceptance criterion.

### §12.1 Leakage family — L11 (5 tests)

| ID | Acceptance criterion |
|---|---|
| L11.a | A `chat_messages` row with `memory_policy='offrecord'` MUST NOT appear in any `ButlerEvidenceContext.bundle.evidence_ids`, `butler_actions.evidence_ids`, or any Telegram outgoing payload from any Butler tool. |
| L11.b | A `chat_messages` row with `memory_policy='nomem'` (detected in any of the 6 `governance.detect_policy` fields: text, caption, poll_question, contact_name, forward_text, forward_caption) MUST NOT appear in any `ButlerEvidenceContext` or outgoing payload. Parameterized across all 6 fields. |
| L11.c | A `forget_events`-active `message_version_id` MUST NOT reach the Butler. A forget event firing mid-flight while a `butler_actions` row is in `pending_confirmation` MUST transition the action to `expired` and the inline keyboard MUST fail closed on callback. |
| L11.d | A redacted `message_versions` row (`is_redacted=TRUE` OR matched by a `_cascade_message_versions` redaction) MUST NOT appear in Butler outgoing text, intro draft, or follow-up correction — even though structural metadata (chat_id, message_id) may remain in audit. |
| L11.e | A Butler preview shown to an `affected_user` (cross-user consent prompt) MUST NOT include any evidence outside that user's `visibility_scope='self'` or `'member'` scope. Admin-only evidence stays redacted in the affected-user preview. |

### §12.2 Citations family — C10 (3 tests)

| ID | Acceptance criterion |
|---|---|
| C10.a | Every executed `butler_actions` row (`status='succeeded'`) has `evidence_ids` resolving to ≥1 live `message_versions.id` OR approved `card_sources.id`. No empty-citation executions. |
| C10.b | An undo row (`parent_action_id` non-NULL) inherits the original's `evidence_context_hash` so audit replay reproduces the original Butler decision context. |
| C10.c | Butler outgoing text citation tokens `[^mv:<message_versions.id>]` MUST resolve to a non-redacted, non-forgotten `message_versions` row. Tokens `[^card:<knowledge_cards.id>]` MUST resolve to a non-archived `knowledge_cards` row whose `card_sources` set has ≥1 non-redacted, non-forgotten `message_versions` row backing it (mirrors `bot/services/wiki_renderer.py:84-87` regex shape: `[^card:<uuid>]` where UUID is the `knowledge_cards.id`, not the `card_sources.id`). Mirrors Phase 9 C8 wiki citation contract. |

### §12.3 Forget cascade family — I9 (6 tests)

| ID | Acceptance criterion |
|---|---|
| I9.a | `forget_event` on a cited `message_version_id` triggers `_cascade_butler_actions` redaction of `butler_tool_invocations.response_payload.text` AND `butler_tool_invocations.response_payload.caption` to `[CONTENT_REDACTED: forget_event_id={n}]`. Action row preserved. |
| I9.b | `forget_event` on a cited `card_sources.id` marks dependent `butler_actions.status='rejected'` (`rejection_reason='source_card_forgotten'`) if still pending, OR triggers a `update_intro` followup_correction if already executed. |
| I9.c | `_cascade_butler_actions` runs at the tail of `CASCADE_LAYER_ORDER` — AFTER `graph_nodes` (asserted via direct `assert CASCADE_LAYER_ORDER[-1] == 'butler_actions'`). |
| I9.d | After cascade, the `butler_actions` row exists with `result_payload.text == '[CONTENT_REDACTED: forget_event_id={n}]'` (preserved row, masked payload). |
| I9.e | A `pending_confirmation` `butler_actions` row whose source becomes forgotten during its TTL window transitions to `expired` BEFORE the inline keyboard's callback handler resolves. Callback handler verifies `status='expired'` and refuses with explicit user message. |
| I9.f | `butler_tool_invocations.idempotency_key` UNIQUE constraint holds — concurrent cascade-fires-mid-execution cannot create a duplicate invocation row. Verified via concurrent-write test with explicit `IntegrityError` assertion. |

### §12.4 Refusal family — R8 (7 tests)

| ID | Acceptance criterion |
|---|---|
| R8.a | A non-member invoking `/butler` is rejected at the handler layer: `UserRepo.get(...)` returns `None` OR the user row has `is_member is not True AND is_admin is not True` (pattern from `bot/handlers/qa.py:369` + `bot/handlers/forward_lookup.py:46`). NO `ButlerEvidenceContext` constructed, NO LLM call, NO `butler_actions` row created. |
| R8.b | The Butler refuses to plan when `ButlerEvidenceContext.bundle.abstained=True` (empty bundle). Explicit refusal via `Abstention` pattern from Phase 5; never a hallucinated empty-citation plan. |
| R8.c | The Butler refuses to execute a cross-user action when the affected user's `butler_action_confirmations` row has `status != 'confirmed'`. Refusal happens at the callback handler, before any Telegram side effect. |
| R8.d | The Butler refuses to act on any source whose mv_id is in an active `graph_purge_pending` row (extends Phase 10 R7.d). Although Butler does not consume `graph_query` in baseline (G3.a), the read-block applies transitively if a future tool surface adds graph consumption. |
| R8.e | The Butler refuses to execute a `pending_confirmation` action whose `expires_at < now()`. Callback handler returns explicit user-facing message; action transitions to `expired`. |
| R8.f | The Butler refuses a `ButlerPlan` whose `tool_name` is not in `ALLOWED_BUTLER_TOOLS`. Rejection happens inside `plan_butler_action` validation, before user confirmation. |
| R8.g | The Butler refuses a `ButlerPlan` whose `args` fail the tool's pydantic args model validation (out-of-bounds values, type mismatches, required fields missing). Rejection happens inside `plan_butler_action`, before user confirmation. |

### §12.5 Drift / invariant-binding family — G3 (4 tests)

| ID | Acceptance criterion |
|---|---|
| G3.a | T12-09 extends `tests/evals/test_no_llm_imports.py` with a new AST-scan helper `assert_no_forbidden_imports_per_path(forbidden_map: dict[str, frozenset[str]])` keyed by glob path under `bot/`, value = frozenset of forbidden module names (extends — does NOT replace — the existing `LLM_PROVIDER_PREFIXES` global scan at lines 26-39). T12-09 PR adds the following map entries verbatim: `{'bot/services/butler*.py': frozenset({'anthropic', 'openai', 'bot.services.graph_query'}), 'bot/handlers/butler.py': frozenset({'anthropic', 'openai', 'bot.services.graph_query'}), 'bot/services/butler_tools/*.py': frozenset({'anthropic', 'openai', 'bot.services.graph_query'})}`. The `bot.services.graph_query` exclusion preserves Phase 10 admin-only stance (§5.7). Test asserts every matched file's AST is free of these imports; missing files (glob hits nothing) are flagged as harness misconfiguration so coverage does not silently drop. |
| G3.b | `butler_actions.evidence_context_hash` is stable across replays. Test recomputes via the canonical `butler_context_hash(bundle, visibility_scope, governance_filter_version)` helper (§3.6 step 1) — input includes per-item `source_type` + `message_version_id` + `card_id` + sorted `card_source_message_version_ids`, NOT just the flattened `bundle.evidence_ids` list. Byte equality required against stored hash. |
| G3.c | NO `butler_actions` row exists without a corresponding `llm_usage_ledger` row linked via `butler_actions.llm_usage_ledger_id`. JOIN integrity verified on all `status IN ('planned','pending_confirmation','confirmed','executing','succeeded')` rows. |
| G3.d | NO `butler_tool_invocations` row exists for a `tool_name` not in `ALLOWED_BUTLER_TOOLS`. DB CHECK constraint (`ck_butler_tool_invocations_tool_name`) enforces this at write time; G3.d asserts the constraint is present and active. |

**Count check:** L11 (5) + C10 (3) + I9 (6) + R8 (7) + G3 (4) = **25**. 77 + 25 = **102**.

---

## §13. Risk register (Phase 12 specific, top 5)

| # | Risk | Likelihood | Severity | Mitigation |
|---|------|------------|----------|------------|
| 1 | Butler reads raw DB directly (invariant #7 breach). | LOW (codified contract + AST scan) | CRITICAL | G3.a binding test + `ButlerEvidenceContext` sealed wrapper at the boundary; T12-04 unit tests assert `butler.py` has no `bot.db.repos.chat_message` import. |
| 2 | Forgotten content leaks via stale evidence snapshot during TTL window. | MEDIUM (forget event timing race) | HIGH | §3.6 snapshot + revalidate-on-execute + cascade-aware expiry (I9.e binding); `_cascade_butler_actions` runs synchronously inside the cascade transaction. |
| 3 | LLM hallucinates a tool name or args that pass pre-validation but cause real Telegram damage. | MEDIUM (LLM output drift) | HIGH | Strict whitelist (R8.f) + pydantic args model (R8.g) + preview-payload-hash check on confirmation (preview_payload_hash stored before any side effect); per-action confirmation gate. |
| 4 | Cross-user intro sent without affected-user consent (consent race). | LOW (consent unbypassable in baseline) | CRITICAL | Affected-user confirmation row REQUIRED before `send_intro.execute()` runs; R8.c binding asserts no consent → no execution; admin override deferred to Phase 12.5+. |
| 5 | Budget overrun (Butler exceeds $1/day global Butler-call-type spend). | MEDIUM (no precedent — first action-cost path) | MEDIUM | Three-layer guard: per-action $0.10 ceiling (estimated-cost check) → per-user $0.20/day → global $1.00/day Butler-call-type total. Each guard writes a `butler_actions` row in `status='rejected'` with `rejection_reason='budget_*_exceeded'` so audit captures the refusal. Per-chat caps are RATE-limited (50 actions/chat/day) not USD-limited (§14.2). |

---

## §14. Cost / rate / TTL envelopes

### §14.1 LLM cost ceilings

C10 reconciliation (Claude HIGH): the daily/monthly ceilings are **GLOBAL** Butler-call-type spend caps — NOT per-chat. This matches the existing `daily_cost_usd(call_type=...)` repo API which has NO chat filter; per-chat caps would require a JOIN against `butler_actions` and a new repo method. Per-user and per-action ceilings remain as-is (per-user uses JOIN; per-action uses `_estimate_cost`).

| Env var | Default | Filter | Scope |
|---|---|---|---|
| `BUTLER_DAILY_USD_CEILING` | `Decimal("1.00")` | `call_type IN ('butler_decision','butler_summary')` via `LedgerRepo.daily_cost_usd(call_type=...)` | global per day |
| `BUTLER_PER_USER_DAILY_USD_CEILING` | `Decimal("0.20")` | `call_type IN ('butler_decision','butler_summary')` JOIN `butler_actions.requester_tg_id` (via `butler_actions.llm_usage_ledger_id`) | per user per day |
| `BUTLER_PER_ACTION_USD_CEILING` | `Decimal("0.10")` | placeholder check vs `_estimate_cost` | per single LLM call |
| `BUTLER_MONTHLY_USD_CEILING` | `Decimal("10.00")` | `call_type IN ('butler_decision','butler_summary')` via `LedgerRepo.monthly_cost_usd(call_type=...)` (C9 ledger extension) | global per month |

Each ceiling is enforced inside `bot/services/butler_budget.py::check_budget` before any provider call. Refusal writes a `butler_actions` row in `status='rejected'` with `rejection_reason='budget_<scope>_exceeded'`. NOTE: per-chat-per-day caps are explicitly DEFERRED — the chat-actions cap is enforced via `butler_rate_buckets.bucket_kind='chat_actions_day'` (§14.2 rate envelope), not via the LLM cost ceiling layer.

### §14.2 Rate envelopes (stored in `butler_rate_buckets`)

H2 reconciliation — **calendar buckets** (MSK timezone, consistent with Phase 7/8 digest jobs). Bucket key format `day:{YYYY-MM-DD}` or `hour:{YYYY-MM-DD-HH}` in MSK; bucket key fully determines `window_start` / `window_end`. NOT rolling windows.

| Bucket | Default | Window | Bucket-key form | Notes |
|---|---|---|---|---|
| `user_plans_day` | 10 | MSK calendar day | `day:{YYYY-MM-DD}` | per requester_tg_id |
| `user_execs_day` | 5 | MSK calendar day | `day:{YYYY-MM-DD}` | per requester_tg_id; counts only `status='confirmed'` → executing transitions |
| `chat_actions_day` | 50 | MSK calendar day | `day:{YYYY-MM-DD}` | per chat_id |
| `tool_hour:recall_evidence` | 30 | MSK calendar hour | `hour:{YYYY-MM-DD-HH}` | per requester_tg_id |
| `tool_hour:schedule_meeting` | 5 | MSK calendar hour | `hour:{YYYY-MM-DD-HH}` | per requester_tg_id |
| `tool_hour:send_intro` | 3 | MSK calendar hour | `hour:{YYYY-MM-DD-HH}` | per requester_tg_id — strictest, cross-user blast radius |
| `tool_hour:update_intro` | 5 | MSK calendar hour | `hour:{YYYY-MM-DD-HH}` | per requester_tg_id |
| `tool_hour:suggest_card_creation` | 10 | MSK calendar hour | `hour:{YYYY-MM-DD-HH}` | per requester_tg_id |

### §14.3 TTL envelopes

| Class | TTL | Field | Notes |
|---|---|---|---|
| Plan (low-risk action) | 15 min | `butler_actions.expires_at` | meetings, recall, card suggestions |
| Plan (cross-user intro) | 5 min | `butler_actions.expires_at` | shorter to limit stale-context blast radius |
| Plan (admin-review card suggestion) | 30 min | `butler_actions.expires_at` | longer because admin review is async |
| Confirmation token | 5 min | `butler_action_confirmations.expires_at` | inline-keyboard freshness |
| Evidence snapshot | 30 min | derived: `now() - butler_actions.created_at` | re-validated against `forget_events` table at execute time per §3.6 |

### §14.4 Rate-bucket window

**Calendar** MSK-day windows for `user_*_day` + `chat_actions_day` buckets; **calendar** MSK-hour windows for `tool_hour:*` buckets. NOT rolling — calendar alignment matches `digest_daily_job` (09:00 MSK) convention and eliminates the leader-election race-condition class. Window boundaries are stored explicitly in `butler_rate_buckets.window_start` / `window_end` so the cleanup tick can reap expired rows. Concurrent increments converge on the same row via UNIQUE `(bucket_kind, scope_id, bucket_key)` + ON CONFLICT upsert (§5.2 SQL).

---

## §15. Final report block

### Resolved questions (decisions baked into spec)

1. Surface — DMs only baseline (Phase 12.1–12.4), group-chat to Phase 12.5+. (§3.1)
2. Triggers — deferred entirely to Phase 12.5+. (§3.2)
3. Per-user opt-in — none. (§3.3)
4. Rate-limit storage — `butler_rate_buckets` table, migration 072. (§3.4, §5.2)
5. Admin override of cross-user consent — none in baseline. (§3.5)
6. Evidence freshness — snapshot + TTL ≤ 30 min + cascade-aware revalidation pre-execute. (§3.6)
7. Migration window — Orchestrator B owns **050-099**; Phase 12 reserves 070-073 (074-079 hotfix buffer; 080-099 Phase 12.5+ runway). (§5.1, §9)
8. Ledger call_type allow-list — extended via migration 071 CHECK + repo/model docstring + `RESERVED_LEDGER_CALL_TYPES`. (§5.3)
9. CASCADE_LAYER_ORDER position — Butler layers AFTER `graph_nodes` at tail. (§4.4)
10. `EvidenceContext` rename — `ButlerEvidenceContext` sealed wrapper around `EvidenceBundle`. (§4.2)
11. Butler gateway entrypoint — `plan_butler_action` + `synthesize_butler_summary`. (§4.3)
12. Concrete DDL — migrations 070+071+072+073 with CHECK constraints on all enum-style columns. (§4.5, §4.6, §5.2)
13. Graph stance — Butler does NOT consume `graph_query` in baseline; admin-only stance preserved. (§5.7)
14. Phase 11 binding count — 77 → 102 (25 new tests). (§5.6, §12)
15. Sprint 0 gate — this PR merges before any code sprint opens. (§5.5, §10)
16. `butler_card_suggestions` BLOCKER-tier DDL — UNIQUE on `butler_action_id`, FK `extraction_candidate_id ON DELETE SET NULL`. (§4.6)
17. `evidence_context_hash` formula — single canonical `butler_context_hash(bundle, visibility_scope, governance_filter_version)` helper used by §3.6 step 1 + §12.5 G3.b; card identity included. (§3.6 step 1)
18. Evidence revalidation SQL — explicit predicate using `fe.tombstone_key` 3-key prefix convention (not `target_id`). (§3.6 step 3)
19. Membership check pattern — `UserRepo.get()` + `user.is_member or user.is_admin` ORM pattern from `qa.py:369`/`forward_lookup.py:46` (closes the audit finding that no such symbol as the previously-spec'd helper exists). (§3.3, §8 amendment)
20. `llm_usage_ledger_id` FK — `ON DELETE RESTRICT` + `ck_butler_actions_ledger_required_post_plan` CHECK (no Butler-post-plan rows without a ledger row). (§4.5)
21. Cost ceiling scope — GLOBAL daily/monthly (NOT per-chat); per-chat caps are RATE-limited via `butler_rate_buckets`. (§14.1)
22. `monthly_cost_usd` extension — `call_type` parameter required to make `BUTLER_MONTHLY_USD_CEILING` enforceable. (§10 T12-08, C9)
23. Forbidden-imports per-path AST scanner — new `assert_no_forbidden_imports_per_path` helper layered on top of existing `LLM_PROVIDER_PREFIXES` scan. (§12.5 G3.a)
24. Rate-bucket semantics — calendar MSK boundaries, atomic ON CONFLICT upsert with WHERE-clause ceiling check. (§5.2, §14.2, §14.4)
25. `confirmation_policy` enum — drop `'session_wide'` (explicitly rejected by PHASE12_PLAN.md §2); allowed values `'per_action'` + `'opt_in_by_button'`. (§4.5)
26. `action_type` + `governance_filter_version` columns — CHECK constraint on action_type, NOT NULL frozen-at-creation governance_filter_version. (§4.5, M1, M2)
27. Cascade-vs-callback race lock — `SELECT ... FOR UPDATE` on `butler_actions` row; reused pattern from Phase 9 `/wiki_publish`. (§3.6 step 5, M3)
28. `ButlerPlan` pydantic model — concrete field list defined in T12-03 deliverables. (M4, §10 T12-03)
29. lint-privacy allowlist extension — single carve-out `^docs/memory-system/PHASE[0-9]+_PLAN(_REFRESH)?\.md$`. (§11 DoD 10, C1)

### Deferred items (Phase 12.5+)

- Group-chat surface (`/butler` in public chats).
- Cron / scheduled triggers / proactive nudges / follow-up summary delivery.
- Admin override of cross-user consent.
- Session-wide opt-in.
- `/butler --dry-run` flag.
- Operator dashboard surface.
- Graph projection consumption.
- Reminders for the requester only (`PHASE12_DESIGN.md §2.1` capability 6).

### Sprint 0 PR title proposal

```
feat(p12-s0): Phase 12 Butler — Sprint 0 ratification (docs only)
```

### Branch name proposal

```
feat/p12-s0-ratification
```

### Worktree

```
.worktrees/orch-B-s0  (or .worktrees/orch-B if a prior phase already cleaned up)
```

---

<!-- updated-by-superflow:2026-05-25 -->
