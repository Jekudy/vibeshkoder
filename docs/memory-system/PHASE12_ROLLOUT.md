# Phase 12 — Butler / Action Execution: Operator Rollout Checklist

**Status:** Phase 12 CLOSED 2026-05-30. All feature flags default OFF — production rollout below.

This document is the operator playbook for enabling Phase 12 Butler / action execution
on production. Phase 12 ships DARK (all flags default OFF). Follow this checklist in
order; do NOT skip steps or combine flag flips. The Butler surface must only be enabled
after all migrations are applied and smoke tests pass.

## §1 Scope Summary

Phase 12 adds a Butler / action execution layer to the memory system. Members can
issue natural-language action requests via `/butler` in a DM. The bot plans an action
using LLM gateway evidence, shows a preview with an inline keyboard for confirmation,
and executes exactly 5 tool types after requester + (if applicable) affected-user
consent.

The privacy-critical invariant is cross-user consent: when a planned action would
affect another member, that member receives a separate DM with an Approve / Reject
keyboard. Rejection is UNBYPASSABLE — no admin override exists (charter Hard Constraint
#5). The forget cascade covers all Butler audit tables
(`butler_actions` / `butler_tool_invocations` / `butler_undo_invocations` /
`butler_action_confirmations`) so forgotten content is purged from Butler audit rows
atomically with the forget event.

**Known carryover (Phase 6.5/12.5 — NOT a rollout blocker):** `extraction_candidates.candidate_json`
and `butler_card_suggestions.suggested_card_payload` persist suggested content AT REST
until manual rejection by an admin. The read-side tombstone gates (extractor `NOT EXISTS`
check + `/approve` R3 block) prevent any forgotten content from being promoted, cited,
or surfaced to users or the LLM. Redact-at-rest for these two columns is a pre-existing
Phase-6 hardening item deferred to Phase 6.5/12.5.

## §2 Sprint Summary Table

| Sprint | PR | Description |
|---|---|---|
| Sprint 0 (plan refresh) | — | `PHASE12_PLAN_REFRESH.md` — patches PLAN/DESIGN for current-main reality; authorized 2026-05-23 |
| T12-01 | — | Schema foundation: `butler_actions`, `butler_tool_invocations`, `butler_action_confirmations`, `butler_rate_buckets`, `butler_card_suggestions` ORM + repos + cascade + migrations 070–073 |
| T12-02 | — | `ButlerEvidenceContext` + `butler_context_hash` + `build_butler_evidence` (spec §3.6 / §4.2); Phase 11 binding 77 → 86 |
| T12-03 | — | Butler tools registry + LLM gateway entry points: `ButlerTool` Protocol, `ButlerPlan` pydantic v2, `plan_butler_action` + `synthesize_butler_summary` in `llm_gateway.py` |
| T12-04 | — | `ButlerService` state machine (11 statuses), 6 exception classes, MSK rate buckets w/ rollback, cross-user consent re-verified at execute, cascade `SELECT FOR UPDATE NOWAIT` guard; migration 074 |
| T12-05 | #348 | `bot/handlers/butler.py` — `/butler`, `/butler_status`, `/butler_cancel`, `/butler_undo` (stub) + 4 inline keyboard callbacks; migration 075 adds `'revoked'` to confirmation status CHECK |
| T12-06 | #349 | 5 tool implementations (`recall_evidence`, `schedule_meeting`, `send_intro`, `update_intro`, `suggest_card_creation`); migration 076 adds `butler_tool_invocations.posted_message_id` |
| T12-07 | #351 | `/butler_undo` full implementation + 5 rollback kinds + `butler_undo_invocations` audit table; migration 077 (`butler_undo_invocations` + `'undone'` to actions status CHECK); migration 078 (`inverse_op_payload` JSONB) |
| T12-08 | #350 | TTL expiry worker (`butler_expire_tick_job`), per-user-day budget guard (`ButlerBudgetChecker`), monthly cap filter fix; no migration |
| T12-09 | — | Phase 11 binding suite 77 → 102 (+25 ACs: L11.a-e, C10.a-c, I9.a-f, R8.a-g, G3.a-d); empty-evidence abstention guard in `plan_action`; no migration |
| T12-10 | — | FHR fix (scheduler savepoint), models.py CHECK drift fix, closure docs; no migration |

## §3 Migrations Applied

| Revision | File | Change |
|---|---|---|
| 070 | `070_add_butler_rate_buckets.py` | CREATE TABLE `butler_rate_buckets` |
| 071 | `071_add_llm_ledger_call_type_check.py` | ALTER TABLE `llm_usage_ledger` ADD CHECK constraint on `call_type` values |
| 072 | `072_add_butler_card_suggestions.py` | CREATE TABLE `butler_card_suggestions` |
| 073 | `073_add_butler_core_tables.py` | CREATE TABLE `butler_actions`, `butler_tool_invocations`, `butler_action_confirmations` |
| 074 | `074_butler_actions_fields.py` | ALTER TABLE `butler_actions` ADD `query`/`visibility_scope`/`plan_payload`; ADD `confirmation_token` + UNIQUE to `butler_action_confirmations` |
| 075 | `075_butler_confirmation_revoked_status.py` | Widens `ck_butler_action_confirmations_status` CHECK to include `'revoked'` |
| 076 | `076_butler_invocations_posted_message_id.py` | ALTER TABLE `butler_tool_invocations` ADD `posted_message_id` BIGINT NULLABLE + partial index |
| 077 | `077_butler_undo_invocations.py` | CREATE TABLE `butler_undo_invocations`; widens `ck_butler_actions_status` CHECK to include `'undone'` |
| 078 | `078_butler_tool_invocations_inverse_op.py` | ALTER TABLE `butler_tool_invocations` ADD `inverse_op_payload` JSONB |

Migration chain after Phase 12: linear head is **078**. No branching.

**Downgrade caveat:** Migrations 075 (`'revoked'`) and 077 (`'undone'`) widen CHECK
constraints. Downgrading past either revision while live rows hold those status values
will fail with a constraint violation. Ensure no such rows exist (or move them to a
terminal status) before attempting a downgrade. Migration 077 also creates
`butler_undo_invocations` — downgrade drops this table; ensure no undo audit data
is needed before proceeding.

Verify: `alembic current` must return `078`.

## §4 Feature Flags Introduced (all default OFF)

Feature flags for Butler live in the `feature_flags` DB table (NOT as env vars).
The `memory.butler.*` flags are inserted via SQL — see §9 Rollout Playbook.

| Flag | Default | Description |
|---|---|---|
| `memory.butler.enabled` | OFF | Master kill-switch. All Butler surfaces refuse when OFF. TTL worker also gated here. |
| `memory.butler.recall_evidence.enabled` | OFF | Enables the `recall_evidence` tool. |
| `memory.butler.schedule_meeting.enabled` | OFF | Enables the `schedule_meeting` tool. |
| `memory.butler.send_intro.enabled` | OFF | Enables the `send_intro` tool. |
| `memory.butler.update_intro.enabled` | OFF | Enables the `update_intro` tool. |
| `memory.butler.suggest_card.enabled` | OFF | Enables the `suggest_card_creation` tool. |
| `memory.butler.undo.enabled` | OFF | Enables `/butler_undo`. Dual-gated with master flag (both must be ON). |

**Flag-flip order (staging first, then production):**
1. Flip master flag ON → smoke test (see §9 step 7)
2. Flip per-tool flags one at a time, starting with `recall_evidence` (read-only, lowest risk)
3. Flip `memory.butler.undo.enabled` after at least one per-tool flag has been stable for 24h
4. Repeat the above sequence in production after staging is stable
5. Any flag flip requires team-lead approval and an amendment to `AUTHORIZED_SCOPE.md`

## §5 Cost Ceilings (frozen)

| Ceiling | Value | Env Var | Scope |
|---|---|---|---|
| Daily Butler LLM spend | $1.00/day | `BUTLER_DAILY_USD_CEILING` | All Butler call types, calendar day |
| Per-user daily spend | $0.20/day | `BUTLER_PER_USER_DAILY_USD_CEILING` | Per user, calendar day (MSK) |
| Per-action spend | $0.10 | `BUTLER_PER_ACTION_USD_CEILING` | Per single action plan |
| Monthly Butler cap | $10.00 | `BUTLER_MONTHLY_USD_CEILING` | All Butler call types, calendar month |

Budget-tracked `call_type` values: `'butler_decision'` and `'butler_summary'`.
Separate from the shared `LLM_DAILY_USD_CEILING` ($5/day).

Budget exceeded → `ButlerActionError(error_kind='budget_exceeded')` raised after rolling
back rate-bucket increments. Handler maps this to a user-facing message without exposing
cost details.

## §6 Env Vars Added

| Variable | Default | Description |
|---|---|---|
| `BUTLER_EXPIRE_TICK_SECONDS` | `60` | TTL expiry worker interval (seconds). Backlogs drain across ticks. |
| `BUTLER_EXPIRE_BATCH_SIZE` | `200` | Max stale actions processed per tick (prevents pickup-storm after bot downtime). |
| `BUTLER_PER_USER_DAILY_USD_CEILING` | `0.20` | Per-user daily Butler LLM spend ceiling (USD). |
| `BUTLER_UNDO_TTL_MINUTES` | `60` | How long after execution a Butler action can be undone (minutes). |
| `BUTLER_DAILY_USD_CEILING` | `1.00` | Daily total Butler LLM spend ceiling (USD). |
| `BUTLER_PER_ACTION_USD_CEILING` | `0.10` | Per-action LLM spend ceiling (USD). |
| `BUTLER_MONTHLY_USD_CEILING` | `10.00` | Monthly total Butler LLM spend ceiling (USD). |

Add to production secrets (Coolify / GitHub Secrets). Defaults are safe — the surface
is unreachable while `memory.butler.enabled = false`.

## §7 New Scheduler Jobs

| Job ID | Schedule | Description | Flag Gate |
|---|---|---|---|
| `butler_expire_tick` | Interval every `BUTLER_EXPIRE_TICK_SECONDS` (default 60s) | Calls `_expire_action_inline` for each pending action past `expires_at`; batch-limited to `BUTLER_EXPIRE_BATCH_SIZE` (default 200). | `memory.butler.enabled` checked at tick start |

Job: `max_instances=1`, `coalesce=True`. All exceptions caught to prevent APScheduler
from stopping the fire schedule. Bot threaded via `args=[bot]` (T7 FHR F2 pattern).

## §8 Admin and User Handlers

All user-facing handlers: DM-only (`PrivateChatFilter`), member auth gate
(`UserRepo.get` + `is_member or is_admin`), master flag gate, structured audit logging,
no raw content in log entries.

| Command | Description | Flag Gate |
|---|---|---|
| `/butler <request>` | Plan an action from natural-language request. Shows inline-keyboard preview (Confirm / Cancel). Cross-user consent DM sent automatically when action affects another member. | `memory.butler.enabled` + per-tool flag |
| `/butler_status <action_id>` | Show current action state + plan summary. | `memory.butler.enabled` |
| `/butler_cancel <action_id>` | Cancel a pending or confirmed action (requester or admin only). | `memory.butler.enabled` |
| `/butler_undo <action_id>` | Undo an executed action via LIFO rollback over `butler_tool_invocations`. TTL: `BUTLER_UNDO_TTL_MINUTES` from `executed_at`. | `memory.butler.enabled` + `memory.butler.undo.enabled` |

**Cross-user consent:** Affected user receives a separate Approve / Reject DM.
On reject → `revoke_affected_user_consent` → action cancelled → requester preview
edited to "consent revoked" notice. NO admin override (charter HC#5, immutable).

**Undo invariants:**
- Dual flag gate: both `memory.butler.enabled` AND `memory.butler.undo.enabled` must be ON
- TTL window: `BUTLER_UNDO_TTL_MINUTES` (default 60) from `executed_at`
- LIFO ordering over `butler_tool_invocations`
- 5 rollback kinds: `not_reversible`, `delete_message`, `edit_message`, `followup_correction`, `cancel_pending`
- Idempotency: UNIQUE `(butler_action_id, butler_tool_invocation_id)` in `butler_undo_invocations`
- Privacy-safe: `_resolve_prior_text` returns `None` for cascade-redacted `message_versions` rows; undo never resurrects forgotten content

## §9 Rollout Playbook — Operator Steps

1. **Verify Phase 11 binding suite green on main HEAD:**
   ```bash
   EVAL_HARNESS_ENABLED=1 timeout 300 pytest tests/evals/ -v
   ```
   Should show **102/102 passing** (77 prior + 25 new Phase 12 Butler ACs).

2. **Apply migrations:**
   ```bash
   alembic upgrade head
   ```
   Verify `alembic current` returns `078`. Confirm the Butler tables exist:
   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema='public'
     AND table_name IN (
       'butler_actions','butler_tool_invocations',
       'butler_action_confirmations','butler_rate_buckets',
       'butler_card_suggestions','butler_undo_invocations'
     )
   ORDER BY table_name;
   ```
   Should return 6 rows.

3. **Set env vars in production** (see §6). Restart the bot.

4. **Seed feature flag rows (all OFF):**
   ```sql
   INSERT INTO feature_flags (flag_key, scope_type, scope_id, enabled)
   VALUES
     ('memory.butler.enabled',                  NULL, NULL, FALSE),
     ('memory.butler.recall_evidence.enabled',  NULL, NULL, FALSE),
     ('memory.butler.schedule_meeting.enabled', NULL, NULL, FALSE),
     ('memory.butler.send_intro.enabled',       NULL, NULL, FALSE),
     ('memory.butler.update_intro.enabled',     NULL, NULL, FALSE),
     ('memory.butler.suggest_card.enabled',     NULL, NULL, FALSE),
     ('memory.butler.undo.enabled',             NULL, NULL, FALSE)
   ON CONFLICT (flag_key, scope_type, scope_id) DO NOTHING;
   ```

5. **Smoke-test the flag-gated surface** (safe — master flag OFF, no LLM calls):
   Send `/butler test` in a DM. Expected: silent no-op (flag-OFF path, no response or
   "Butler is not available" message depending on handler version).

6. **Team-lead approval required** before flipping any flag ON. Amend
   `AUTHORIZED_SCOPE.md` with the specific flag and date of authorization.

7. **Flip `memory.butler.enabled` ON (staging first):**
   ```sql
   UPDATE feature_flags SET enabled=TRUE, updated_at=now()
   WHERE flag_key='memory.butler.enabled'
     AND scope_type IS NULL AND scope_id IS NULL;
   ```
   No restart required. Send `/butler test` in DM — expect planning attempt (may
   fail with `empty_evidence` abstention if no community messages yet — that is correct
   behavior).

8. **Flip per-tool flags one at a time**, starting with `recall_evidence`:
   ```sql
   UPDATE feature_flags SET enabled=TRUE, updated_at=now()
   WHERE flag_key='memory.butler.recall_evidence.enabled'
     AND scope_type IS NULL AND scope_id IS NULL;
   ```
   Monitor for 24h before enabling the next tool.

9. **Flip `memory.butler.undo.enabled` ON** after at least one per-tool flag has been
   stable for 24h:
   ```sql
   UPDATE feature_flags SET enabled=TRUE, updated_at=now()
   WHERE flag_key='memory.butler.undo.enabled'
     AND scope_type IS NULL AND scope_id IS NULL;
   ```

10. **Monitor** (see §10). After 24h of clean operation on staging, repeat steps 7–9
    in production.

## §10 Monitoring

| Signal | Where to look | Action threshold |
|---|---|---|
| Stuck `pending_confirmation` past `expires_at` | `SELECT count(*) FROM butler_actions WHERE status='pending_confirmation' AND expires_at < now()` | >0 after 5 min → investigate TTL worker |
| Undo rate spike | `SELECT count(*) FROM butler_actions WHERE status='undone' AND updated_at > now()-interval '1h'` | Spike → check tool implementations for correctness |
| Tool error spikes | `SELECT error_kind, count(*) FROM butler_actions WHERE status='rejected' GROUP BY error_kind` | `plan_error` or `empty_evidence` spikes → investigate evidence quality |
| Budget ledger vs ceilings | `SELECT call_type, sum(cost_usd) FROM llm_usage_ledger WHERE call_type IN ('butler_decision','butler_summary') AND created_at > date_trunc('day', now()) GROUP BY call_type` | Approaching ceiling → flip master flag OFF |
| `butler_card_suggestions` pending rows | `SELECT count(*) FROM butler_card_suggestions WHERE status='pending'` | Large backlog → admin review queue clogged |

## §11 Kill Switch (Emergency Disable)

Flip master flag OFF immediately if a privacy regression is suspected:

```sql
UPDATE feature_flags SET enabled=FALSE, updated_at=now()
WHERE flag_key='memory.butler.enabled'
  AND scope_type IS NULL AND scope_id IS NULL;
```

Effect: all Butler handler paths return immediately (silent no-op or refusal).
The TTL worker stops on next tick. In-flight `pending_confirmation` actions remain
in the DB but are unreachable — they will expire via `butler_expire_tick_job` when the
master flag is re-enabled. No data is lost.

This is fully reversible — no data is lost; only the surfaces are gated.

## §12 Privacy Invariants

- **Forget cascade coverage:** `forget_cascade._cascade_butler_actions` (and sibling
  cascade functions for confirmations, invocations, undo invocations) atomically redact
  Butler audit rows within the same Postgres transaction as the forget event.
  `CASCADE_LAYER_ORDER` ordering: butler layers are appended AFTER `graph_nodes` at the tail.

- **Cross-user consent (UNBYPASSABLE):** Affected user receives a separate DM with
  Approve / Reject keyboard. On reject → action cancelled. No admin override exists.
  This is a Hard Constraint (#5) in the Phase 12 charter — it cannot be relaxed in
  Phase 12.5 without a new charter amendment.

- **Empty-evidence abstention:** `ButlerService.plan_action` Step 2b rejects when
  `evidence_context.bundle.abstained is True` — writes a `rejected/empty_evidence`
  audit row and raises before the LLM gateway call. Prevents hallucinated plans.

- **Undo privacy:** `_resolve_prior_text` returns `None` for cascade-redacted
  `message_versions` rows (NULL text on redaction). Undo never resurrects forgotten
  content — verified: edit-undo sources only from cascade-redacted `message_versions`,
  fails closed on `None`.

- **Known carryover (Phase 6.5/12.5):** `extraction_candidates.candidate_json` and
  `butler_card_suggestions.suggested_card_payload` persist suggested content AT REST
  until manual admin rejection. Read-side tombstone gates prevent any promotion or
  LLM surfacing. This is a pre-existing Phase-6 condition; redact-at-rest is deferred.

## §13 Phase 11 Binding Suite

Phase 12 adds 25 new binding tests. Total after T12-09: **102/102**.

New test IDs: L11.a-e (Butler leakage, 5), C10.a-c (Butler citations, 3),
I9.a-f (Butler forget cascade, 6), R8.a-g (Butler refusal, 7),
G3.a-d (Butler drift / AST no-LLM-imports, 4).

## §14 Phase 12.5 Carryovers

These are deferred items tracked for post-launch follow-up:

- **Redact-at-rest for candidate content** (T12-06 / Phase 6.5): `extraction_candidates.candidate_json`
  and `butler_card_suggestions.suggested_card_payload` hold suggested content AT REST.
  Read-side gates (extractor `NOT EXISTS` + R3 block) prevent promotion. Full redact-at-rest
  hardening deferred.
- **Downgrade-guard hardening** (migrations 075/077): Downgrades past these revisions are
  unsafe while `'revoked'` / `'undone'` status rows exist. A pre-flight downgrade guard
  (mirroring Phase 8's `ck_digests_status` approach) is deferred.
- **Per-tool-flag service-layer defense-in-depth** (T12-05): Per-tool flags are checked at
  the handler layer. A redundant service-layer check inside `execute_action` per tool would
  add defense-in-depth. Deferred.
- **Callback master-flag guard** (T12-05): Inline keyboard callback handlers do not re-check
  the master flag at callback time (action was already gated at plan time). A re-check guard
  for long-running confirmations is deferred.
- **`_resolve_prior_text` live-PG exercise** (T12-07): The JOIN through `chat_messages` is
  tested with mocks only; a DB-backed integration test is deferred to Phase 12.5.
- **I9.b auto-followup_correction edge case** (T12-09): Already-executed cited action
  re-trigger edge case deferred.
- **Spec mask-format divergence** (T12-09): Phase 9 spec defines `[CONTENT_REDACTED: forget_event_id={n}]`;
  shipped Butler columns use JSONB `{"redacted":true,"forget_event_id":n}`. Privacy invariant
  holds; format normalisation deferred.
- **Group-chat `/butler` surface** — Phase 12.5+.
- **Cron/scheduler triggers, deferred reminders** — Phase 12.5+.

## §15 References

- `docs/memory-system/PHASE12_PLAN.md` — canonical plan (ratified 2026-05-02).
- `docs/memory-system/PHASE12_PLAN_REFRESH.md` — post-Phase 9/10 plan patches (authorized 2026-05-23).
- `docs/memory-system/PHASE12_DESIGN.md` — companion design doc.
- Per-sprint rollout fragments: `docs/rollout-fragments/phase12/` (T12-02 through T12-10).
- Per-sprint PRs: #348 (T12-05), #349 (T12-06), #350 (T12-08), #351 (T12-07).

## Operator Sign-Off

Before enabling any Butler flag in production, confirm:

- [ ] All migrations applied; `alembic current` = `078`
- [ ] Phase 11 binding suite 102/102 green on deployed HEAD
- [ ] Feature flag rows seeded (all OFF)
- [ ] Env vars set in Coolify / GitHub Secrets
- [ ] Team-lead (jekudy@gmail.com) approval for each flag flip recorded
- [ ] `AUTHORIZED_SCOPE.md` amended with flag and date
- [ ] Staging soak (24h per tool) completed before production flip
- [ ] Monitoring dashboard / queries set up (see §10)
- [ ] Kill switch procedure verified (master flag OFF test on staging)
