# Phase 8 — Weekly Editorial Digest: Ratified Plan

**Status:** RATIFIED 2026-05-15. Implementation authorized for Sprint 0 (`AUTHORIZED_SCOPE.md` update + this plan) + 8 sprints across 3 waves.
**Predecessors:** Phase 4 (FTS + evidence, CLOSED 2026-04-30), Phase 5 (`llm_gateway` + ledger, CLOSED 2026-05-11), Phase 6 (cards + admin review, CLOSED 2026-05-12), Phase 7 (daily digest, CLOSED 2026-05-15), Phase 11 (privacy binding suite, ACTIVE 30/30 — see §10 for the verified per-file enumeration; Round 1 review correctly flagged that the 28/28 → 34/34 narrative was a math drift from earlier docs).
**Owner:** Orchestrator A (sole orchestrator for Phase 5 → 6 → 7 → 8 synthesis chain).
**Charter:** `.superflow/charter.md` (run `phase8-weekly-editorial-digest`).
**Supersedes:** `docs/memory-system/prompts/PHASE8_PLAN_DRAFT.md` (reflection/observations content re-scoped to Phase 9+).

---

## 0. Implementation Status: AUTHORIZED

Phase 8 is authorized for implementation following Phase 7 closure 2026-05-15. All
runtime dependencies (Phase 4 evidence, Phase 5 `llm_gateway`/ledger, Phase 6
cards/sources, Phase 7 digests runtime + cascade + scheduler, Phase 11 binding
suite) are satisfied. Sprint 0 must update `AUTHORIZED_SCOPE.md` (replace the
"NOT in Phase 7 scope (defer to Phase 8+)" bullet for weekly digest at
`AUTHORIZED_SCOPE.md:238-241` with an "Authorized: Phase 8" block) **before any
code lands**.

| Component | Status | Notes |
|---|---:|---|
| `digests` table + `digest_runs` audit | Exists | Migration 037 (`alembic/versions/037_add_digests.py:57-184`). `type` CHECK already permits `'weekly'` (`037_add_digests.py:97-100`) — no ALTER needed. `status` CHECK does NOT include `awaiting_review` / `approved_for_publish` / `rejected_by_admin` / `rejected_by_reaper` (`037_add_digests.py:101-105`) — migration 038 must ALTER. |
| `run_digest` orchestrator | Exists | `bot/services/digests.py:164`. `type: Literal["daily"]` (`digests.py:167`) — Phase 8 widens to `Literal["daily", "weekly"]`. Advisory-lock idempotency, `_cost_ceiling_breached` (`digests.py:104-134`), and `_acquire_idempotency_lock` (`digests.py:137-160`) all reusable verbatim — Phase 8 adds a weekly cost-ceiling SQL variant. |
| `synthesize_digest` gateway method | Exists | `bot/services/llm_gateway.py` — accepts `prompt_template_version` keyword. Phase 8 wires a new template id `"digest-weekly-v0.1.0"`. The gateway itself is type-agnostic and re-validates context inside `_digest_context_is_clean` (`llm_gateway.py` Phase 7 surface) before provider dispatch. |
| `build_digest_context` | Exists | `bot/services/digest_context.py`. Phase 8 calls it with a 7-day window + larger token budget; no signature change. |
| `digest_publisher.publish_digest` | Exists | `bot/services/digest_publisher.py`. Phase 8 publishes only after a `draft → awaiting_review → approved_for_publish → posting` transition gated by `digest_review.approve_digest`. |
| `digest_renderer.render_digest_html` | Exists | `bot/services/digest_renderer.py`. Phase 8 extends to recognize `## Раздел: …` Markdown headers and bold them as `<b>` in the HTML output. |
| `redact_digest_for_forget` + `_cascade_digests` | Exists | `bot/services/digest_redactor.py`; `bot/services/forget_cascade.py:804-880`. **Three coupled allowlists MUST be widened together in T8-04** (C1 in §5.K): (i) `_cascade_digests` JSONB scan filter at `forget_cascade.py:840`; (ii) `redact_digest_for_forget` early-return guard at `digest_redactor.py:105` (`if current_status not in (...): return`); (iii) the no-citation-match status UPDATE at `digest_redactor.py:135-137` (`WHERE id=:id AND status IN (...)`). Widening (i) alone is a silent privacy regression: the cascade selects the row but the redactor short-circuits it back out. See §5.K for the unified allowlist. |
| `digest_publisher.publish_digest` trigger guard | Exists | `bot/services/digest_publisher.py:118` hardcodes `if digest.status != "draft": raise DigestPublisherInvalidState(...)`. T8-04 (or T8-06) MUST widen this guard to accept `approved_for_publish` so the `digest_review.approve_digest → publish_digest` handoff transitions the row through `posting → posted`. See §5.L. |
| `digest_daily_job` + `digest_stale_posting_reaper_job` | Exists | `bot/services/scheduler.py:282-425`. T8-05 adds `digest_weekly_job` (cron Mon 09:15 MSK — H8 15-min stagger past daily 09:00; see §5.G) + `digest_stale_review_reaper_job` (48h DM + 7d auto-reject). |
| `bot/handlers/digest.py` `_is_admin` + Phase 7 commands | Exists | `bot/handlers/digest.py`. T8-06 adds `/digest_approve`, `/digest_reject`, `/digest_review` and widens `/digest_now` to accept `weekly`. |
| `feature_flags` table | Exists | Phase 8 flag `memory.digests.weekly.enabled` default OFF, same shape as Phase 7 daily flag. |
| `tests/evals/` Phase 11 suite | Exists | **Verified baseline 30/30** by direct enumeration of `tests/evals/test_*.py` (`test_leakage.py`: L1, L2, L3a, L3b, L3c, L4, L5, L6a, L6b, L6c = 10; `test_citations.py`: C1, C2, C3, C4, C5a, C5b, C5c, C5d = 8; `test_refusal.py`: R1, R2, R3a, R3b, R3c, R4 = 6; `test_digest_leakage.py`: L7a, L7b, C6, I5a, I5b, I5c = 6). Earlier "28/28 → 34/34" framing in Phase 7 closure docs was a stale enumeration drift (counted by category, not by parametrize ids). Phase 8 adds **12** new cases (L8a/b + C7 + I6a + I6b.1/2/3 + I6c + R5.a/b/c/d) → **42/42 new total**. The split of I6b into 3 windowed sub-cases and R5 into 4 admin-gate sub-cases is required for binding completeness — see §10. |
| Migration counter | 037 on main | T8-01 implementation verifies the alembic head and uses head+1 (default `038`). |
| Carryovers from Phase 7.5 | OPEN | Issue #291 (shared `_forget_excludes_predicate` refactor) and #295 (T7-02 post-merge MED items). Both may land independently before or during Phase 8; Phase 8 plan reads cleanly against either state. See §11 for status notes. |

---

## 1. Non-Negotiable Invariants (verbatim from HANDOFF §1)

1. Existing gatekeeper must not break.
2. **No LLM calls outside `llm_gateway`.** Weekly digest synthesis routes through the existing `synthesize_digest` gateway method with a new prompt template id (`digest-weekly-v0.1.0`). No direct provider imports anywhere in `bot/services/digests*.py`, `bot/services/digest_publisher.py`, `bot/services/digest_review.py`, or `bot/handlers/digest.py`.
3. **No extraction / search / q&a / summary over forbidden content.** Weekly context query inherits the Phase 7 `_forget_excludes_predicate` and applies `memory_policy='normal' AND is_redacted=FALSE` plus the no-active-forget-event check.
4. **Citations point to `message_version_id` or approved card sources.** Citation JSONB stays array-of-ids (`{kind: 'message_version'|'card_source', id, position}`). Section headers (`## Раздел: …`) are inert text; the bullet-level invariant (every bullet has ≥1 citation) is unchanged from Phase 7 and exercised by binding test C7.
5. **Summary is never canonical truth.** Weekly digest is editorial prose grouped by section; consumers must read it as a recap.
6. Graph is never source of truth. (N/A.)
7. Future butler cannot read raw DB directly. (N/A.)
8. Import apply must go through same normalization / governance path. (N/A.)
9. Tombstones are durable and not casually rolled back. (Weekly digests holding citations to a forgotten source must redact same as daily — exercised by I6a/b/c.)
10. Public wiki remains disabled. (N/A.)

**Phase 8 additional invariant:** **No auto-publish.** A weekly digest can only transition to `posting → posted` after an explicit single-admin `/digest_approve <id>` call. Stale drafts are auto-rejected by the reaper after 7 days (`rejected_by_reaper` terminal, distinct from admin-driven `rejected_by_admin`). This is the binding contract that distinguishes Phase 8 from Phase 7 and is exercised by binding test R5.

---

## 2. Phase 8 Spec (HANDOFF §3 + ROADMAP row 8)

### Phase 8 — Weekly digest

- **Objective:** weekly editorial recap with admin review gate.
- **Scope:** `digests.type='weekly'` rows + admin-review state machine + `digest_review.py` service + `digest_weekly_v0_1_0` prompt.
- **Dependencies:** Phase 7 minimum.
- **Acceptance (ROADMAP row 8):** "reviewed sourced sections; **no auto-publish**."
- **Charter ACs:** see §13.

---

## 3. Phase 9 Boundary — what Phase 8 MUST NOT do

- No `reflection_runs`, no `observations`, no `memory_events`, no `memory_candidates`. These are Phase 9+ even though the historical `prompts/PHASE8_PLAN_DRAFT.md` mixed them in — Q1 ratification (§6) re-scopes that draft.
- No graph projection. (Phase 10.)
- No wiki pages or public weekly archive.
- No per-user opt-out for being mentioned in a weekly digest (Phase 9+).
- No multi-chat weekly digest (single-chat MVP; one weekly digest per `(type='weekly', window_start, window_end)`).
- No LLM-driven topic clustering beyond section organization. Sections come from the prompt template; the LLM groups bullets into pre-defined section headers but does NOT invent new section names. The render layer recognizes `## Раздел: …` as a known prefix only.
- No admin pre-publish content edit. Admin uses `/digest_reject + /digest_now weekly` to re-generate.
- No multi-admin quorum / two-admin gate. Single-admin approval is binding (Q4).
- No new gateway methods. Weekly synthesis reuses `synthesize_digest` with a different prompt template id.
- No new `digest_sections` table (Q2). Sections live inline in `body_markdown`.
- No reaction-count / reply-count ranking heuristics (columns don't exist on `chat_messages` — same constraint as Phase 7).
- No `digest_admin_notify` rewrite. Phase 8 reuses the existing helper from Phase 7 with new event types.

---

## 4. Architecture Overview

```
                    ┌────────────────────────────────────────────┐
                    │ AsyncIOScheduler  (existing, UTC)          │
                    │ - existing: digest_daily (Phase 7)         │
                    │ - new: digest_weekly                       │
                    │   cron(day_of_week="mon",                  │
                    │        hour=DIGEST_WEEKLY_HOUR_MSK,        │
                    │        minute=DIGEST_WEEKLY_MINUTE_MSK,    │
                    │        timezone=ZoneInfo("Europe/Moscow")) │
                    │   default Mon 09:15 MSK = Mon 06:15 UTC    │
                    │   (15-min stagger past daily 09:00 to      │
                    │   avoid concurrent LLM gateway pressure —  │
                    │   H8 in §5.G)                              │
                    │   flag: memory.digests.weekly.enabled (OFF)│
                    │ - new: digest_stale_review_reaper          │
                    │   every 30 min, no flag gate (reaper       │
                    │   pattern, same as digest_stale_posting)   │
                    └────────────────────┬───────────────────────┘
                                         │ triggers
                                         ▼
                    ┌────────────────────────────────────────────┐
                    │ digest_weekly_job(bot)                      │
                    │ - opens async_session()                     │
                    │ - flag re-check via FeatureFlagRepo         │
                    │ - resolves ISO-week window:                 │
                    │     last_monday_00_msk..this_monday_00_msk  │
                    │ - calls run_digest(type='weekly', ...)      │
                    │ - on draft: transitions to                  │
                    │   awaiting_review via digest_review         │
                    │   (NEVER calls publish_digest directly —    │
                    │    that path is admin-gated)                │
                    │ - DMs admins on awaiting_review entry       │
                    └────────────────────┬───────────────────────┘
                                         │
                                         ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ run_digest(session, *, type='weekly',                             │
       │            window_start, window_end,                              │
       │            ledger_repo, provider, config,                         │
       │            digest_config) → Digest                                │
       │                                                                   │
       │  1. Idempotency: advisory_xact_lock + SELECT FOR UPDATE on        │
       │     (type='weekly', ws, we). Reuse Phase 7 lock helper            │
       │     (digests.py:137-160).                                          │
       │  2. Weekly cost-ceiling check (DIGEST_WEEKLY_USD_CEILING +        │
       │     DIGEST_WEEKLY_MONTHLY_USD_CEILING — separate bucket from      │
       │     daily). Reuse _cost_ceiling_breached but with                 │
       │     d.type='weekly' filter.                                        │
       │  3. INSERT digest_runs (status='running')                         │
       │  4. INSERT digests (type='weekly', status='running', body=NULL)   │
       │  5. build_digest_context with 7-day window + larger budget        │
       │  6. llm_gateway.synthesize_digest(prompt_template_version=        │
       │     'digest-weekly-v0.1.0') → body_markdown (with sections),      │
       │     citations[], ledger_id                                         │
       │  7. UPDATE digests SET body, citations, ledger_id, status='draft' │
       │  8. UPDATE digest_runs SET status='finished'                      │
       │  9. Return Digest                                                  │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ digest_weekly_job: after run_digest returns status='draft' →      │
       │ calls digest_review.transition_to_awaiting_review(digest_id) +    │
       │ admin DM with /digest_approve and /digest_reject buttons (text   │
       │ commands; aiogram inline kb optional in v1.1).                    │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ Admin commands (bot/handlers/digest.py — T8-06)                   │
       │ /digest_review                                                    │
       │   → lists awaiting_review weekly digests                          │
       │ /digest_approve <id> [--force-revalidate]                         │
       │   → digest_review.approve_digest(id) →                            │
       │     1. SELECT FOR UPDATE; status must be 'awaiting_review'        │
       │     2. Re-run defense-in-depth citation revalidation              │
       │        (same _digest_context_is_clean logic as gateway)           │
       │     3. UPDATE status='approved_for_publish',                      │
       │        published_by_admin_id=:admin_id, approved_at=now()         │
       │     4. Call digest_publisher.publish_digest                       │
       │        (publisher sees 'approved_for_publish' as the trigger      │
       │        state, transitions to 'posting' under the same long-lived  │
       │        transaction as Phase 7 §5.F)                               │
       │ /digest_reject <id> [reason]                                      │
       │   → digest_review.reject_digest(id, reason, admin_id) →           │
       │     1. SELECT FOR UPDATE; status must be 'awaiting_review'        │
       │     2. UPDATE status='rejected_by_admin',                         │
       │        review_notes=:reason, published_by_admin_id=:admin_id      │
       │     3. INSERT digest_runs (status='rejected_by_admin')            │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ digest_publisher.publish_digest (Phase 7 — UNCHANGED public API) │
       │   - widened internal guard: trigger state IS NOW                  │
       │     'approved_for_publish' for weekly, 'draft' for daily          │
       │   - same single-transaction lock + revalidate + send_message     │
       │   - same posting → posted terminal                                │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
                    ┌────────────────────────────────────────────┐
                    │ digest_runs (audit, extended)               │
                    │ status: running / finished / failed         │
                    │   / skipped / cost_exceeded /               │
                    │   skipped_no_destination /                  │
                    │   awaiting_review / approved_for_publish /  │
                    │   rejected_by_admin / rejected_by_reaper    │
                    │ error_text, started/finished timestamps     │
                    └────────────────────────────────────────────┘

Stale-review reaper (separate scheduler job, runs every 30 min, no flag gate):

       ┌──────────────────────────────────────────────────────────────────┐
       │ digest_stale_review_reaper_job()                                  │
       │ - 48h pass: SELECT id, awaiting_review_at FROM digests             │
       │   WHERE type='weekly' AND status='awaiting_review'                │
       │     AND awaiting_review_at < now() - interval '48 hours'          │
       │     AND review_notes NOT LIKE '%[48h_notified]%' (or marker col)  │
       │   → admin DM: "weekly digest #N awaiting review for 48h"          │
       │   → UPDATE review_notes to append '[48h_notified]' marker         │
       │ - 7d pass: SELECT id FROM digests                                 │
       │   WHERE type='weekly' AND status='awaiting_review'                │
       │     AND awaiting_review_at < now() - interval '7 days'            │
       │   → UPDATE status='rejected_by_reaper',                           │
       │     review_notes=COALESCE(review_notes,'') || '[stale_7d]'        │
       │   → INSERT digest_runs (status='rejected_by_reaper')              │
       │   → admin DM on rejection                                         │
       └──────────────────────────────────────────────────────────────────┘

Forget cascade extension (T8-04 widens existing _cascade_digests scan filter):

       ┌──────────────────────────────────────────────────────────────────┐
       │ _cascade_digests (forget_cascade.py:804 — Phase 7 baseline)       │
       │ WIDEN the JSONB scan WHERE clause at forget_cascade.py:840:       │
       │   FROM: status IN ('draft','posting','posted','redacted',         │
       │                    'redacted_edit_failed')                        │
       │   TO:   status IN ('draft','awaiting_review',                     │
       │                    'approved_for_publish','posting','posted',     │
       │                    'redacted','redacted_edit_failed',             │
       │                    'rejected_by_admin')                            │
       │ Rationale (R1 in §3): a weekly digest in awaiting_review or       │
       │ approved_for_publish may already cite a now-forgotten source.     │
       │ If the cascade skips these statuses, the forget event will not    │
       │ propagate to the weekly draft, and an admin /digest_approve may   │
       │ publish forgotten content. The redactor (redact_digest_for_forget)│
       │ already accepts any status in the §5.A enum — only the scan       │
       │ predicate needs widening. rejected_by_admin also redacts so       │
       │ /digest_history audit shows the redaction marker.                 │
       │                                                                    │
       │ skipped / failed / cost_exceeded / skipped_no_destination /        │
       │ rejected_by_reaper are EXCLUDED from the scan — these are          │
       │ terminal states with no user-visible content posted to Telegram   │
       │ and no path back to publish (idempotency returns the existing     │
       │ row to /digest_now). I6a binding test pins this contract.         │
       └──────────────────────────────────────────────────────────────────┘
```

---

## 5. Component Design

### 5.A. Migration 038 `extend_digests_review_states` (T8-01)

**Files created:**
- `alembic/versions/038_extend_digests_review_states.py`
- ORM changes in `bot/db/models.py` (extend `Digest` mapped class — add `published_by_admin_id`, `approved_at`, `review_notes`, `awaiting_review_at`).

**Migration plan:** PostgreSQL CHECK constraints are immutable in place; the canonical alembic pattern is DROP CHECK + ADD CHECK. The Phase 7 migration `037_add_digests.py:101-105` named the constraint `ck_digests_status`, so T8-01 references that name by parameter, not by inspection. **All CHECK additions use the NOT VALID + VALIDATE pattern (H1)** to avoid an AccessExclusiveLock-while-scanning. `ALTER TABLE ... DROP CONSTRAINT` takes a brief lock (≤ms); `ALTER TABLE ... ADD CONSTRAINT ... NOT VALID` is instant (lock-free skip of existing rows); `ALTER TABLE ... VALIDATE CONSTRAINT ...` scans the table without blocking writers (only blocks concurrent schema changes). For a small `digests` / `digest_runs` table this is paranoid optimization, but it is the project-wide alembic convention and removes any future concern if rows grow.

**Upgrade SQL (rendered through SQLAlchemy / `op.execute` for each statement; `op.add_column` for new cols; `op.create_index` for the partial idx). Five distinct statement groups — keep them in this exact order:**

```sql
-- ── Group 1: extend digests.status CHECK to 14 values ─────────────────
ALTER TABLE digests DROP CONSTRAINT ck_digests_status;
ALTER TABLE digests ADD CONSTRAINT ck_digests_status CHECK (
    status IN (
        'running','draft','posting','posted','failed','skipped',
        'cost_exceeded','skipped_no_destination','redacted',
        'redacted_edit_failed',
        -- Phase 8 additions (4):
        'awaiting_review','approved_for_publish',
        'rejected_by_admin','rejected_by_reaper'
    )
) NOT VALID;
ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_status;

-- ── Group 2: extend digest_runs.status CHECK (C4 — incomplete enum) ───
-- transition_to_awaiting_review inserts digest_runs(status='awaiting_review').
-- approve_digest step 5 inserts digest_runs(status='approved_for_publish').
-- reject_digest inserts digest_runs(status='rejected_by_admin').
-- digest_stale_review_reaper inserts digest_runs(status='rejected_by_reaper').
-- --regenerate handler inserts digest_runs(status='regenerated_by_admin').
-- All five values MUST be in the constraint or each insert raises CHECK violation.
ALTER TABLE digest_runs DROP CONSTRAINT ck_digest_runs_status;
ALTER TABLE digest_runs ADD CONSTRAINT ck_digest_runs_status CHECK (
    status IN (
        'running','finished','failed','skipped',
        'cost_exceeded','skipped_no_destination',
        -- Phase 8 additions (5):
        'awaiting_review','approved_for_publish',
        'rejected_by_admin','rejected_by_reaper','regenerated_by_admin'
    )
) NOT VALID;
ALTER TABLE digest_runs VALIDATE CONSTRAINT ck_digest_runs_status;

-- ── Group 3: new columns supporting the review workflow ───────────────
ALTER TABLE digests ADD COLUMN published_by_admin_id BIGINT NULL;
ALTER TABLE digests ADD COLUMN approved_at TIMESTAMP WITH TIME ZONE NULL;
ALTER TABLE digests ADD COLUMN review_notes TEXT NULL;
ALTER TABLE digests ADD COLUMN awaiting_review_at TIMESTAMP WITH TIME ZONE NULL;

-- ── Group 4: extend body-NOT-NULL invariant to review-bearing statuses ─
ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses;
ALTER TABLE digests ADD CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses CHECK (
    status NOT IN (
        'draft','posting','posted','redacted','redacted_edit_failed',
        'awaiting_review','approved_for_publish','rejected_by_admin',
        'rejected_by_reaper'
    )
    OR body_markdown IS NOT NULL
) NOT VALID;
ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses;
-- rejected_by_reaper is included because the reaper may reject a digest that
-- already has a body (transitioned from awaiting_review where body was required).
-- Requiring body non-null for this terminal state preserves the audit trail:
-- the rejected body remains inspectable by admins. NULL body for a rejected
-- row would hide what content was pending review.

-- ── Group 5: partial index + approval-audit CHECK ─────────────────────
CREATE INDEX ix_digests_status_awaiting_review
    ON digests (awaiting_review_at)
    WHERE status='awaiting_review';

-- The audit CHECK covers the full admin-approval window: approved_for_publish
-- (set at /digest_approve time), posting (publisher picked it up), and posted
-- (Telegram delivery confirmed). All three require admin attribution cols for
-- weekly digests — they travel together through the approve→posting→posted
-- pipeline with the same audit cols set at /digest_approve.
ALTER TABLE digests ADD CONSTRAINT ck_digests_approved_audit CHECK (
    status NOT IN ('approved_for_publish','posting','posted')
    OR type <> 'weekly'
    OR (
        published_by_admin_id IS NOT NULL AND approved_at IS NOT NULL
    )
) NOT VALID;
ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_approved_audit;
-- Daily digests are exempt: weekly path mandates admin attribution; daily
-- path is auto-publish and leaves the audit cols NULL forever. The
-- `type <> 'weekly'` predicate keeps the constraint type-aware.
```

**Downgrade SQL (T8-01 must provide a clean reverse):**

```sql
-- ── Downgrade pre-flight: fail hard on Phase-8 state in either table ───
-- (M5: once Phase 8 has been exercised, downgrade is an operator decision
-- that requires explicit cleanup. The MigrationError text guides the operator
-- to docs/memory-system/PHASE8_ROLLOUT.md "downgrade" runbook section.)
DO $$
BEGIN
    -- R2 HIGH-Cdx-2: the pre-flight MUST also block `posting` rows. The
    -- `posting` status itself is Phase 7 (in the narrower restored CHECK),
    -- so it survives the constraint swap — but a row stuck in `posting`
    -- represents an in-transit publish for either daily OR weekly. A
    -- weekly publish in `posting` was triggered by admin /digest_approve
    -- and is mid-`bot.send_message`; downgrading underneath drops the
    -- audit columns the publisher relies on, races the stale-posting
    -- reaper, and may surface as a "publish silently lost" incident.
    -- Blocking ALL `posting` rows (daily + weekly) is the safer default
    -- — neither flavor is acceptable to drop schema cols underneath.
    IF EXISTS (
        SELECT 1 FROM digests WHERE status IN (
            'awaiting_review','approved_for_publish',
            'rejected_by_admin','rejected_by_reaper',
            'posting'  -- R2 HIGH-Cdx-2
        )
    ) THEN
        RAISE EXCEPTION 'cannot downgrade: digests rows in Phase 8 review states '
                        '(awaiting_review/approved_for_publish/rejected_by_admin/'
                        'rejected_by_reaper) OR in-transit `posting` state exist '
                        '— wait for in-flight publishes to terminate '
                        '(stale_posting_reaper runs every 5 min) then manually '
                        'transition or DELETE Phase-8 review rows per '
                        'PHASE8_ROLLOUT.md "downgrade" runbook section before '
                        're-running downgrade';
    END IF;
    IF EXISTS (
        SELECT 1 FROM digest_runs WHERE status IN (
            'awaiting_review','approved_for_publish',
            'rejected_by_admin','rejected_by_reaper','regenerated_by_admin'
        )
    ) THEN
        RAISE EXCEPTION 'cannot downgrade: digest_runs rows in Phase 8 audit states exist';
    END IF;
END$$;

DROP INDEX ix_digests_status_awaiting_review;
ALTER TABLE digests DROP CONSTRAINT ck_digests_approved_audit;

-- Restore Phase 7 body-NOT-NULL constraint.
ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses;
ALTER TABLE digests ADD CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses CHECK (
    status NOT IN ('draft','posting','posted','redacted','redacted_edit_failed')
    OR body_markdown IS NOT NULL
) NOT VALID;
ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses;

ALTER TABLE digests DROP COLUMN awaiting_review_at;
ALTER TABLE digests DROP COLUMN review_notes;
ALTER TABLE digests DROP COLUMN approved_at;
ALTER TABLE digests DROP COLUMN published_by_admin_id;

-- Restore Phase 7 digest_runs.status CHECK.
ALTER TABLE digest_runs DROP CONSTRAINT ck_digest_runs_status;
ALTER TABLE digest_runs ADD CONSTRAINT ck_digest_runs_status CHECK (
    status IN ('running','finished','failed','skipped',
               'cost_exceeded','skipped_no_destination')
) NOT VALID;
ALTER TABLE digest_runs VALIDATE CONSTRAINT ck_digest_runs_status;

-- Restore Phase 7 digests.status CHECK.
ALTER TABLE digests DROP CONSTRAINT ck_digests_status;
ALTER TABLE digests ADD CONSTRAINT ck_digests_status CHECK (
    status IN (
        'running','draft','posting','posted','failed','skipped',
        'cost_exceeded','skipped_no_destination','redacted',
        'redacted_edit_failed'
    )
) NOT VALID;
ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_status;
```

**Downgrade safety contract:** the pre-flight `DO $$ ... $$` block above raises `MigrationError` (Python wrapper) when:
1. Any row exists in a Phase-8-only status (`awaiting_review`, `approved_for_publish`, `rejected_by_admin`, `rejected_by_reaper`).
2. **Any row exists in `posting`** (R2 HIGH-Cdx-2): both daily and weekly `posting` rows block. `posting` is a Phase 7 status (survives the constraint swap) but represents an in-flight publish that may be writing to Telegram while the downgrade runs. Dropping `published_by_admin_id` / `approved_at` / `review_notes` under an in-flight WEEKLY publish breaks the publisher's terminal `UPDATE` (the `RETURNING` clause references columns the publisher reads); dropping under an in-flight DAILY publish is technically safe (daily never writes the new audit cols) but the conservative default is to block both since `posting` is by design short-lived (publisher's 30s statement_timeout + `stale_posting_reaper` 2-min sweep). The operator just waits up to ~5 minutes for the reaper, then re-runs.

This protects against silent data corruption when an operator downgrades after weekly digests have entered the review queue or while ANY publish is in flight. Phase 7's `posted` / `redacted` rows are preserved (their statuses remain valid under the narrower restored CHECK). Operator path: per `PHASE8_ROLLOUT.md` "downgrade" section, either manually transition `awaiting_review`/`approved_for_publish` rows to `rejected_by_admin` (then DELETE) or DELETE outright — weekly digest data is non-critical recap content. For `posting` blocks, just wait for the reaper.

**ORM additions in `bot/db/models.py`:**

```python
class Digest(Base):
    # ... existing Phase 7 columns ...

    # T8-01 / Phase 8: review-gate workflow.
    awaiting_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
```

**Rollback scope:** Migration 038 touches ONLY the `digests` table CHECK constraints + new columns + the `digest_runs.status` CHECK. No other tables. No FK changes. No data migration (existing rows are valid under the new wider constraint).

**Why cascade scan widening is REQUIRED:** see §5.D and stop-signal in §8. The `_cascade_digests` JSONB scan at `forget_cascade.py:840` filters on `status IN ('draft','posting','posted','redacted','redacted_edit_failed')`. A weekly digest in `awaiting_review` or `approved_for_publish` is NOT in that list, so a `forget` event firing during the admin-review window would NOT propagate to the weekly draft. The admin could then `/digest_approve` and publish forgotten content to Telegram. T8-04 widens this filter as the binding fix; I6a is the regression test.

### 5.B. `bot/services/digests.py` extension — weekly path (T8-02 sub-component)

**Signature change:**

```python
async def run_digest(
    session: AsyncSession,
    *,
    type: Literal["daily", "weekly"],  # widened from Literal["daily"]
    window_start: datetime,
    window_end: datetime,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    config: LLMGatewayConfig,
    digest_config: DigestConfig,
) -> Digest: ...
```

**`DigestConfig` extension:**

```python
@dataclass(frozen=True)
class DigestConfig:
    # Phase 7 (unchanged)
    daily_cost_ceiling_usd: Decimal = Decimal("1.00")
    monthly_cost_ceiling_usd: Decimal = Decimal("10.00")
    source_chat_id: int = 0
    destination_chat_id: int | None = None
    hour_msk: int = 9
    min_cards_threshold: int = 3
    raw_message_top_n: int = 15
    token_budget_input: int = 8000
    # Phase 8 additions
    weekly_cost_ceiling_usd: Decimal = Decimal("5.00")
    weekly_monthly_cost_ceiling_usd: Decimal = Decimal("20.00")
    weekly_hour_msk: int = 9
    weekly_minute_msk: int = 15  # H8 — 15-min stagger past daily 09:00.
    weekly_token_budget_input: int = 24000
    # L5: bumped from 5 → 8. Daily threshold is 3 with a 24h window;
    # weekly is a 7× larger window. Scaling the threshold linearly
    # (3 × 7 ≈ 21) over-shoots — weekly cards are a higher-quality cohort
    # (admin-approved over the full week). 8 is the empirical middle:
    # "at least 8 admin-approved cards in the week" is a strong signal
    # that the recap can be cards-first; below that, raw fallback is
    # better.
    weekly_min_cards_threshold: int = 8
    weekly_raw_message_top_n: int = 60
    review_deadline_hours: int = 168  # 7d
    review_48h_notify_hours: int = 48
```

> **M3 — `DIGEST_WEEKLY_DAY` env var removed.** Earlier draft introduced a configurable ISO day-of-week. Wiring it through apscheduler's `day_of_week=` arg requires an ISO→apscheduler mapping (`{1:'mon',...,7:'sun'}`) AND ALSO mirror handling in the window-anchor calculation in `digest_weekly_job`. Two coupled places for no real operator benefit (weekly cadence on a non-Mon day has no business justification in v1). **Decision: hardcode `day_of_week="mon"` in the scheduler registration AND `isoweekday() - 1` in the window-anchor math.** If a future operator wants a different day, they edit the scheduler reg directly; this is a 2-line code change, not a runtime config concern.

**`load_digest_config()` extension** (preserving env-var convention from `bot/services/digests.py:80-101`):

```python
def load_digest_config() -> DigestConfig:
    # ... existing daily env reads ...
    return DigestConfig(
        # ... existing fields ...
        weekly_cost_ceiling_usd=Decimal(
            os.environ.get("DIGEST_WEEKLY_USD_CEILING", "5.00")
        ),
        weekly_monthly_cost_ceiling_usd=Decimal(
            os.environ.get("DIGEST_WEEKLY_MONTHLY_USD_CEILING", "20.00")
        ),
        weekly_hour_msk=int(os.environ.get("DIGEST_WEEKLY_HOUR_MSK", "9")),
        weekly_minute_msk=int(os.environ.get("DIGEST_WEEKLY_MINUTE_MSK", "15")),
        weekly_token_budget_input=int(
            os.environ.get("DIGEST_WEEKLY_TOKEN_BUDGET", "24000")
        ),
        weekly_min_cards_threshold=int(
            os.environ.get("DIGEST_WEEKLY_MIN_CARDS_THRESHOLD", "8")
        ),
        weekly_raw_message_top_n=int(
            os.environ.get("DIGEST_WEEKLY_RAW_MESSAGE_TOP_N", "60")
        ),
        review_deadline_hours=int(
            os.environ.get("DIGEST_REVIEW_DEADLINE_HOURS", "168")
        ),
        review_48h_notify_hours=int(
            os.environ.get("DIGEST_REVIEW_48H_NOTIFY_HOURS", "48")
        ),
    )
```

**`run_digest` behaviour — weekly path divergence:**

1. Advisory lock + idempotency: identical to Phase 7 (`digests.py:137-160`), keyed on `(type='weekly', ws, we)`.
2. Cost ceiling (H6 — type filter pushed into the shared helper): T8-02 refactors Phase 7's `_cost_ceiling_breached(session, digest_config)` to accept a `type: Literal['daily','weekly'] = 'daily'` kwarg, adding `WHERE d.type = :type` to BOTH the daily-bucket and monthly-bucket SQL queries. The Phase 7 callsite passes `type='daily'` (back-compat default); the new Phase 8 weekly callsite passes `type='weekly'`. The helper reads the bucket-specific ceiling out of `digest_config.daily_cost_ceiling_usd` vs `digest_config.weekly_cost_ceiling_usd` based on the `type` arg. Result: two independent buckets sharing one helper function — no `_cost_ceiling_breached_weekly` duplicate. **The Phase 7 `_cost_ceiling_breached` is the file in production; it MUST be updated in T8-02 itself — without the type filter, weekly costs would also count against the daily bucket and starve daily runs.**

```sql
-- T8-02 type-filtered SQL (daily-bucket variant; monthly is the same with month trunc):
SELECT COALESCE(SUM(l.cost_usd), 0)
FROM llm_usage_ledger l
JOIN digests d ON d.llm_usage_ledger_id = l.id
WHERE d.type = :type
  AND d.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                       AT TIME ZONE 'UTC'
```

If the bucket is exceeded → INSERT `digests` row with `status='cost_exceeded'`, INSERT `digest_runs` `status='cost_exceeded'`, return. No LLM call. The two ceilings are INDEPENDENT of the shared Phase 5 `LLM_DAILY_USD_CEILING` (C5 reformulation in §6 Q7) — they gate only the corresponding digest type's bucket. Exercised by acceptance test T8-02-AC4 + regression on daily ceiling.
3. Open `digest_runs` `status='running'`.
4. Insert `digests` row `status='running'`, `type='weekly'`, `body_markdown=NULL`, `citations='[]'::jsonb`.
5. Build context via `build_digest_context(session, type='weekly', window_start=ws, window_end=we, source_chat_id=cfg.source_chat_id, digest_config=cfg)`. The context builder reads `digest_config.weekly_*` fields when `type='weekly'` (see §5.C).
6. Empty-window short-circuit: same as Phase 7 (`status='skipped'`, no LLM).
7. Call `llm_gateway.synthesize_digest(... prompt_template_version='digest-weekly-v0.1.0')`. Gateway records ledger + revalidates context internally.
8. UPDATE `digests` with body, citations, ledger_id, `status='draft'`. **Note: the digest stays `draft` after weekly run — the transition to `awaiting_review` is done by `digest_weekly_job` after `run_digest` returns, NOT inside `run_digest` itself.** This keeps `run_digest` type-agnostic in its terminal-state contract.
9. UPDATE `digest_runs` `status='finished'`. Return.

**Error handling:** unchanged from Phase 7. `failed` / `cost_exceeded` / `skipped` paths all materialize as rows; no `None` return.

### 5.C. `bot/services/digest_context.py` — weekly window extension (T8-03)

**Signature change:**

```python
async def build_digest_context(
    session: AsyncSession,
    *,
    type: Literal["daily", "weekly"],  # widened
    window_start: datetime,
    window_end: datetime,
    source_chat_id: int,
    digest_config: DigestConfig,
) -> DigestContext: ...
```

**Behaviour:**
- When `type='weekly'` the SQL query is identical in shape to the Phase 7 daily query (cards-first, raw-messages fallback, governance filter via `_forget_excludes_predicate`) — only the window bounds change (7-day span instead of 24h) and the limits widen:
  - Cards `LIMIT` raised from `30` → `100`.
  - Raw-message `LIMIT` from `digest_config.raw_message_top_n` (default 15) → `digest_config.weekly_raw_message_top_n` (default 60).
  - Token budget from `digest_config.token_budget_input` (default 8000) → `digest_config.weekly_token_budget_input` (default 24000) with the same `-1000` headroom rule for the prompt template overhead.
  - Min-cards-threshold from `digest_config.min_cards_threshold` (default 3) → `digest_config.weekly_min_cards_threshold` (default 5).
- The `DigestContext` dataclass `type` field widens to `Literal['daily','weekly']`.
- Helper accessor: a small `_weekly_overrides(digest_config) -> tuple[int, int, int, int]` keeps the type-aware param resolution in one place so the SQL builder reads from a single source.

**No new SQL drift.** Phase 7 acceptance T7-03 requires `_forget_excludes_predicate` extracted as a shared helper (still tracked as carryover #291 — see §11). When that lands, Phase 8 inherits the refactor automatically and binding test C7 verifies the same predicate fires for weekly contexts.

### 5.D. `bot/services/forget_cascade.py` — scan-filter widening (T8-04)

**Single-line change at `forget_cascade.py:840`:**

```python
# BEFORE (Phase 7 baseline):
"WHERE d.status IN "
"('draft','posting','posted','redacted','redacted_edit_failed') "

# AFTER (T8-04):
"WHERE d.status IN "
"('draft','awaiting_review','approved_for_publish','posting','posted',"
" 'redacted','redacted_edit_failed','rejected_by_admin') "
```

**Rationale (binding privacy invariant R1 from analyst summary):**
- Without this change, a forget event firing on a `mvid` cited by a weekly digest currently in `awaiting_review` would be silently skipped — the cascade scan would not find the row.
- An admin could then `/digest_approve <id>` later, and the publisher's defense-in-depth revalidation at §5.E.4 would either (a) catch it and fail the publish (best case, but still surprising to admin), or (b) miss it because the citation rows exist but the underlying chat_message_id was tombstoned in a non-Phase-7-tracked way. The cascade is the canonical propagation path; relying on revalidation alone weakens defense in depth.
- Including `rejected_by_admin` ensures `/digest_history` audit reflects the post-rejection redaction (the row was rejected, then forgotten — both events visible).
- Excluding `skipped`, `failed`, `cost_exceeded`, `skipped_no_destination`, `rejected_by_reaper` is intentional: these rows either have no body (`body_markdown` may be NULL) or are terminally rejected with no path back to publish. The cascade has nothing to redact and no Telegram message to amend.

**Redactor (`bot/services/digest_redactor.py`) MUST also widen its two internal allowlists** in lockstep with the cascade scan — see **§5.K** for the unified widening (C1 — Round 1 review identified this as a silent privacy regression if forgotten in T8-04). Otherwise the cascade selects an `awaiting_review` row but the redactor's early-return guard at `digest_redactor.py:105` short-circuits it back out, leaving the row unmasked while the cascade marks the event "completed". The DB redaction then silently fails to happen and an admin `/digest_approve` can still publish forgotten content.

Once §5.K is applied, the body-NOT-NULL CHECK at §5.A continues to hold because `redacted` is in the wider invariant list, and the status transition is `awaiting_review → redacted` (or `approved_for_publish → redacted` / `rejected_by_admin → redacted`). For these statuses, `posted_message_id IS NULL` so the Telegram side-effect is skipped — only DB redaction happens.

**Additional redactor branch — admin notify on review-state redaction (T8-04 sub-step):** when the redactor sees `status='awaiting_review'`, it MUST ALSO call `notify_admins_digest_failure(... error_text='forget_redacted_during_review')` so the admin who was about to approve knows the draft has been silently redacted and the entry has disappeared from `/digest_review` listing (M2). This is **both** a UX nicety and a binding test surface for I6b.1 (forget BEFORE approve).

### 5.E. `bot/services/digest_review.py` (T8-04) — NEW module

**Public API:**

```python
@dataclass(frozen=True)
class ApproveResult:
    digest_id: int
    posted_chat_id: int | None
    posted_message_id: int | None
    error_text: str | None  # populated if approval transitioned but publish failed


class DigestReviewInvalidState(Exception):
    """Approval/reject attempted from a non-awaiting_review state, OR a guarded
    UPDATE returned rowcount=0 because the row was deleted or its status moved
    out from under us between revalidation and commit.

    Structured fields (NOT positional Exception args) so handlers can render
    context-aware admin replies — required by Round 2 HIGH-Cdx-1:

        digest_id:        int                — always populated.
        current_status:   str | None         — None ⇒ row was DELETEd
                                                concurrently (e.g. by
                                                /digest_now --regenerate or
                                                operator manual cleanup);
                                                str  ⇒ row exists in this
                                                status (e.g. 'redacted',
                                                'rejected_by_admin', 'posted').
        reason:           str                — free-text explanation for logs
                                                + admin reply, e.g.
                                                "expected status='awaiting_review',
                                                found 'redacted'" or
                                                "row_deleted_during_transition".
    """

    def __init__(
        self,
        *,
        digest_id: int,
        current_status: str | None,
        reason: str,
    ) -> None:
        self.digest_id = digest_id
        self.current_status = current_status
        self.reason = reason
        super().__init__(
            f"DigestReviewInvalidState(digest_id={digest_id}, "
            f"current_status={current_status!r}, reason={reason!r})"
        )


class DigestReviewNotFound(Exception):
    """Digest id was never found by the pre-flight SELECT in step 2 of
    approve_digest / reject_digest. Distinct from
    DigestReviewInvalidState(current_status=None), which fires only AFTER a
    guarded UPDATE rowcount=0 race when the re-read confirms the row has
    since been DELETEd."""


# ─── Canonical rowcount=0 handler (R2 HIGH-Cdx-1) ─────────────────────────
# Every guarded UPDATE in this module — and the publisher trigger transition
# in §5.L — MUST use this exact pattern. Distinguishes "row deleted
# concurrently" (current=None) from "row in unexpected state"
# (current=<some other status>). Both raise DigestReviewInvalidState with
# structured fields so the handler layer renders a context-aware admin reply
# without re-querying.

async def _raise_invalid_state_after_guard_miss(
    session: AsyncSession,
    *,
    digest_id: int,
    expected_status: str | tuple[str, ...],
) -> None:
    """Call ONLY after a guarded UPDATE returned rowcount=0. Re-reads the
    current status (or detects DELETE) and raises DigestReviewInvalidState
    with structured fields.
    """
    current = (
        await session.execute(
            select(Digest.status).where(Digest.id == digest_id)
        )
    ).scalar_one_or_none()
    if current is None:
        raise DigestReviewInvalidState(
            digest_id=digest_id,
            current_status=None,
            reason="row_deleted_during_transition",
        )
    raise DigestReviewInvalidState(
        digest_id=digest_id,
        current_status=current,
        reason=f"expected status={expected_status!r}, found {current!r}",
    )


# Canonical guarded UPDATE pattern — every transition in this module uses
# this exact shape. Reference this block from §5.L (publisher trigger
# transition) — DO NOT duplicate the pattern there.
#
#     result = await session.execute(
#         update(Digest)
#         .where(Digest.id == digest_id, Digest.status == expected_status)
#         .values(status=new_status, ...other_fields...)
#         .returning(Digest.id)
#     )
#     if result.rowcount == 0:
#         # Concurrent transition won; classify and raise.
#         await _raise_invalid_state_after_guard_miss(
#             session,
#             digest_id=digest_id,
#             expected_status=expected_status,
#         )
#     # rowcount == 1: commit path continues.


async def transition_to_awaiting_review(
    session: AsyncSession,
    *,
    digest_id: int,
) -> None:
    """Called by digest_weekly_job after run_digest returns status='draft'.

    **Guarded UPDATE pattern (H2)** — does NOT use SELECT FOR UPDATE +
    ORM mutation. The state-machine transition is implemented as a single
    DB-side UPDATE keyed on the expected source state; rowcount=0 means a
    racing transition already won.

        UPDATE digests
        SET status='awaiting_review',
            awaiting_review_at=now(),
            updated_at=now()
        WHERE id=:digest_id
          AND status='draft'
          AND type='weekly'
        RETURNING id

    Rowcount=0 → call the canonical handler
    `_raise_invalid_state_after_guard_miss(session, digest_id=digest_id,
    expected_status='draft')` which re-reads, distinguishes deleted-row
    (`current_status=None`) from wrong-status, and raises
    DigestReviewInvalidState with structured fields. Idempotency caveat:
    if the current status is already 'awaiting_review' (a benign re-call),
    the canonical handler raises `DigestReviewInvalidState(current_status=
    'awaiting_review', reason=...)`; the caller (digest_weekly_job's match
    block in §5.G) catches this specific case and treats it as a no-op
    log line, NOT an error.

    On rowcount=1: INSERT digest_runs(digest_id=:id, status='awaiting_review',
    started_at=now()). Commit. Caller (digest_weekly_job) then DMs admins.
    """


async def approve_digest(
    session: AsyncSession,
    *,
    bot: Bot,
    digest_id: int,
    admin_id: int,
    digest_config: DigestConfig,
) -> ApproveResult:
    """Single-admin approval → triggers publisher.

    Six-step flow with guarded UPDATE pattern (H2) at each transition:

    1. Begin transaction; statement_timeout 30s (mirrors §5.F publisher).
    2. SELECT digests.* WHERE id=:digest_id (NO FOR UPDATE — read-only
       inspection; the actual transitions at steps 3 and 4 are guarded
       UPDATEs that handle concurrency atomically).
       - Not found → raise DigestReviewNotFound (pre-flight SELECT miss —
         no race window has opened yet).
       - status != 'awaiting_review' → raise DigestReviewInvalidState(
         digest_id=:id, current_status=status, reason='not_awaiting_review').
       - type != 'weekly' → raise DigestReviewInvalidState(
         digest_id=:id, current_status=status, reason='not_weekly').
    3. Re-run defense-in-depth context revalidation. Re-query every citation
       source id; if any source is now forgotten/redacted/missing → guarded
       UPDATE (canonical pattern from top of this module):

           UPDATE digests
           SET status='failed',
               error_text='citations_stale_at_approval',
               updated_at=now()
           WHERE id=:digest_id AND status='awaiting_review'
           RETURNING id

       Rowcount=0 → call `_raise_invalid_state_after_guard_miss(session,
       digest_id=:id, expected_status='awaiting_review')`. The handler
       distinguishes "row deleted concurrently" (current_status=None ⇒
       reason='row_deleted_during_transition') from "cascade redact won"
       (current_status='redacted' ⇒ reason="expected status=
       'awaiting_review', found 'redacted'"). I6b.2 exercises the
       latter. Rowcount=1 → commit, admin notify, raise
       DigestReviewInvalidState(digest_id=:id, current_status='failed',
       reason='citations_stale_at_approval').
    4. Guarded approval transition (canonical pattern):

           UPDATE digests
           SET status='approved_for_publish',
               published_by_admin_id=:admin_id,
               approved_at=now(),
               updated_at=now()
           WHERE id=:digest_id AND status='awaiting_review'
           RETURNING id

       Rowcount=0 → call `_raise_invalid_state_after_guard_miss(session,
       digest_id=:id, expected_status='awaiting_review')`. Possible
       race winners: cascade redact (current='redacted'), concurrent
       /digest_reject (current='rejected_by_admin'), another concurrent
       /digest_approve (current='approved_for_publish' or further), or
       --regenerate DELETE (current=None). Rowcount=1 → INSERT
       digest_runs(status='approved_for_publish', started_at=now()).
       Commit (releases the row lock).
    5. Call digest_publisher.publish_digest(session, bot=bot, digest=digest,
       digest_config=digest_config). The publisher's own transaction acquires
       FOR UPDATE NOWAIT and its guarded UPDATE accepts 'approved_for_publish'
       OR 'draft' (per §5.L). On publisher success: posted_* fields populated,
       status='posted'. On publisher failure: same terminal handling as
       Phase 7 (status='failed' + error_text).
    6. Return ApproveResult with the publisher's outcome.

    **I6b.3 contract (forget AFTER approve commit, BEFORE publisher
    dispatch):** if cascade redaction lands between step 4 commit and step 5,
    the publisher's guarded UPDATE at §5.L finds status='redacted', returns
    rowcount=0 from `WHERE id=:id AND status IN ('draft','approved_for_publish')`,
    and terminates with `error_text='publisher_status_mismatch'`. Admin notify
    fires; ApproveResult.error_text propagates to the handler reply.
    """


async def reject_digest(
    session: AsyncSession,
    *,
    digest_id: int,
    admin_id: int,
    reason: str | None,
) -> None:
    """Single-admin reject → terminal rejected_by_admin.

    Service-layer normalization (L4): the column is unbounded `Text`;
    truncation happens here, not in the schema.

        reason = (reason or "no reason given")[:1000]

    Then a single guarded UPDATE (canonical pattern from top of this module):

        UPDATE digests
        SET status='rejected_by_admin',
            published_by_admin_id=:admin_id,
            review_notes=:reason,
            updated_at=now()
        WHERE id=:digest_id AND status='awaiting_review'
        RETURNING id

    Rowcount=0 → call `_raise_invalid_state_after_guard_miss(session,
    digest_id=:id, expected_status='awaiting_review')` — same classifier
    as approve_digest steps 3/4. Rowcount=1 → INSERT digest_runs(
    status='rejected_by_admin', error_text=:reason, started_at=now(),
    finished_at=now()). Commit.

    No publish. No Telegram side-effect. Admin can re-run with
    `/digest_now weekly --regenerate` (§5.H) to produce a fresh draft
    for the same window. Plain `/digest_now weekly` without `--regenerate`
    against a rejected row returns the existing terminal row via
    idempotency and replies with the "use --regenerate" hint.
    """
```

**Transaction boundaries:**
- `transition_to_awaiting_review` → single transaction, commits before admin DM.
- `approve_digest` → outer transaction commits at step 4 (releases lock so publisher can re-acquire); publisher uses its own transaction (single long-lived per Phase 7 §5.F). The approve-then-publish sequence is NOT atomic; if the orchestrator crashes between step 4 (commit) and step 5 (publisher dispatch), the row is stuck in `approved_for_publish` and the stale-posting-style scenario applies. T8-05 adds a `digest_stale_approved_reaper` to the existing reaper job that moves `approved_for_publish` older than 5 minutes back to `failed` with `error_text='stale_approved_reaper'`. This mirrors §5.K from Phase 7.
- `reject_digest` → single transaction.

**Error types:**
- `DigestReviewNotFound` → caller (handler) replies "Дайджест #id не найден". Pre-flight SELECT miss only — no guarded-update race involved.
- `DigestReviewInvalidState(digest_id, current_status, reason)` → caller renders a context-aware admin reply by branching on `current_status`:
  - `current_status is None` (`reason='row_deleted_during_transition'`) → "Дайджест #id удалён в процессе (вероятно, --regenerate или ручная очистка). Перезапустите /digest_now weekly."
  - `current_status='redacted'` → "Дайджест #id отредактирован после forget-события. Опубликовать нельзя; используйте /digest_now weekly --regenerate."
  - `current_status='posted'` → "Дайджест #id уже опубликован." + t.me link.
  - `current_status='rejected_by_admin' | 'rejected_by_reaper'` → "Дайджест #id отклонён. Используйте /digest_now weekly --regenerate."
  - `current_status='approved_for_publish'` → "Дайджест #id уже одобрен другим админом, ожидает публикации."
  - `current_status='failed'` (when reason='citations_stale_at_approval') → "Цитаты дайджеста #id устарели. Используйте /digest_now weekly --regenerate."
  - any other status → generic "Дайджест #id в неожиданном статусе `{current_status}`: {reason}".
- All other exceptions → caller replies generic error + logs structured (digest_id, exc_info).

### 5.F. `bot/services/llm_prompts/digest_weekly_v0_1_0.py` (T8-02) — NEW prompt template

**Module structure:** mirrors `bot/services/llm_prompts/digest_v0_1_0.py` (Phase 7) — exports a `SYSTEM` and `USER_TEMPLATE` string plus a `PROMPT_TEMPLATE_VERSION = "digest-weekly-v0.1.0"` constant for ledger logging.

**SYSTEM block (verbatim contract):**

```
You are writing an editorial WEEKLY digest for a private community chat.
This will be reviewed by an admin before publishing — do not assume it
will be sent verbatim. Write the best draft you can; the admin will
approve or reject.

Output format (strict):
  Line 1-4: TL;DR — 3-4 short sentences in Russian, prose. Cover the
            week's main themes at a high level.
  Blank line.
  Then 2-5 SECTIONS, each separated by a blank line. Section header
  format (strict, used by the renderer to bold the heading):
    ## Раздел: {section_title}
  Section title MUST be one of these allowed prefixes (in Russian):
    - Объявления
    - Обсуждения
    - Знания и ресурсы
    - Встречи и события
    - Прочее
  Within each section, 3-7 bullets:
    - Topic title (≤10 words).
    - 1-2 sentence summary.
    - Citation tokens: [[cs:UUID]] for an approved card source,
      [[mv:INT]] for a raw message version. EVERY bullet MUST contain
      at least one citation token referencing input ids verbatim.
  Skip a section entirely if there is no material for it. Do NOT
  invent section names. Do NOT cite ids absent from the input.
  Use Russian. Be neutral. Do not invent facts.
  If the input has no cards and no messages, return exactly: EMPTY_WINDOW
```

**USER block template:**

```
Window: {window_start_msk} .. {window_end_msk} (Europe/Moscow, ISO week)
Cards ({len(cards)}):
  Card "{c.title}" (approved). Source ids you may cite: {c.card_source_ids_csv}
  Card body: {c.body_markdown_stripped}
  ---
Messages ({len(messages)}):
  [mv:{m.message_version_id}] {m.author_display}, {m.ts_msk}: {m.text}
  ---
```

**Critical citation contract (unchanged from Phase 7):** cards are cited by `card_sources.id` (UUID), NEVER `knowledge_cards.id`. The prompt exposes only `card_source_ids`. The output token format is `[[cs:UUID]]` / `[[mv:INT]]`. Section headers are inert — the citation tokenizer scans within bullets only.

**Output parsing extensions (in `bot/services/llm_gateway.py`):**
- The existing `synthesize_digest` body-parsing recognizes `## Раздел: …` lines and treats them as section markers, NOT as bullets. The bullet-level invariant (≥1 valid citation token per `- ` line) does NOT fire on section headers (which start with `## `, not `- `).
- Citation validation is unchanged: hallucinated ids dropped + logged; if any bullet has zero valid citations after drop → `DigestCitationValidationError`. Binding test C7 exercises this.
- EMPTY_WINDOW sentinel handling is unchanged: identical sentinel string, same `DigestEmptyWindowError` → `status='skipped'` mapping.
- A new helper `_extract_sections(body_markdown) -> list[tuple[str, list[str]]]` returns `[(section_title, bullet_lines), ...]` for the renderer. Section header pattern is `re.compile(r"^##\s+Раздел:\s+(.+)$", re.MULTILINE)`.

### 5.G. Scheduler hooks — `bot/services/scheduler.py` extension (T8-05)

**Three new jobs added inside `start_scheduler(bot)`. H8 — stagger weekly cron 15 minutes past the daily cron** so the two LLM gateway invocations do not contend on Mondays (daily fires Mon 09:00, weekly fires Mon 09:15). `max_instances=1` only scopes a single job, so the stagger is the actual interference guard:**

```python
# T8-05: Phase 8 weekly digest. Mon 09:15 MSK (15-min stagger past daily
# digest at 09:00 MSK — H8). Gated by feature flag
# memory.digests.weekly.enabled (default OFF). The job body re-checks the
# flag and is a strict no-op when disabled.
scheduler.add_job(
    digest_weekly_job,
    "cron",
    day_of_week="mon",  # apscheduler accepts "mon" (lower-case three-letter)
    hour=settings.DIGEST_WEEKLY_HOUR_MSK,    # default 9
    minute=settings.DIGEST_WEEKLY_MINUTE_MSK,  # default 15 (H8 stagger)
    args=[bot],
    id="digest_weekly",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=3600,  # 1h grace — weekly cadence is forgiving
    timezone=ZoneInfo("Europe/Moscow"),
)

# T8-05: Phase 8 stale-review reaper. Every 30 min, no flag gate.
scheduler.add_job(
    digest_stale_review_reaper_job,
    "interval",
    minutes=30,
    id="digest_stale_review_reaper",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)
```

**`digest_weekly_job` body (in `bot/services/digests.py`, mirroring `digest_daily_job` at `scheduler.py:282`):**

```python
async def digest_weekly_job(bot: Bot) -> None:
    """Weekly digest run trigger — fires Mon DIGEST_WEEKLY_HOUR_MSK MSK.

    Strict no-op when memory.digests.weekly.enabled is OFF. Window is the
    most recently completed ISO week: last_monday 00:00 MSK..this_monday
    00:00 MSK (stored as UTC).

    Same try/except wrapping as digest_daily_job — apscheduler never sees
    an exception; all outcomes persisted via digests / digest_runs rows.
    """
    try:
        async with async_session() as session:
            from bot.db.repos.feature_flag import FeatureFlagRepo

            flag_enabled = await FeatureFlagRepo.get(
                session, "memory.digests.weekly.enabled"
            )
            if not flag_enabled:
                logger.info("digest_weekly_job: flag disabled, skipping")
                return

            from zoneinfo import ZoneInfo
            from bot.services.digests import load_digest_config, run_digest
            from bot.services.digest_review import transition_to_awaiting_review

            msk = ZoneInfo("Europe/Moscow")
            now_msk = datetime.now(tz=msk)
            # Find most recent Monday 00:00 MSK (this Monday if fired Mon 09:15;
            # the staggered weekly cron — daily cron baseline is 09:00, see H8 stagger).
            today_midnight_msk = now_msk.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # ISO Mon = 1, Tue = 2, ..., Sun = 7.
            days_since_monday = today_midnight_msk.isoweekday() - 1
            this_monday_msk = today_midnight_msk - timedelta(days=days_since_monday)
            last_monday_msk = this_monday_msk - timedelta(days=7)

            window_start = last_monday_msk.astimezone(timezone.utc)
            window_end = this_monday_msk.astimezone(timezone.utc)

            digest_config = load_digest_config()
            gateway_config = load_gateway_config()
            try:
                digest = await run_digest(
                    session,
                    type="weekly",
                    window_start=window_start,
                    window_end=window_end,
                    ledger_repo=LedgerRepo(),
                    provider=resolve_provider(gateway_config.provider),
                    config=gateway_config,
                    digest_config=digest_config,
                )
                await session.commit()
                logger.info(
                    "digest_weekly_job: ws=%s we=%s digest_id=%s status=%s",
                    window_start.isoformat(),
                    window_end.isoformat(),
                    digest.id,
                    digest.status,
                )

                # H4: idempotency may return an EXISTING row in any status.
                # Each branch handles the discovered terminal state. Cron
                # NEVER auto-regenerates; admin /digest_now --regenerate is
                # the only path out of rejected_* / failed states.
                match digest.status:
                    case "draft":
                        # Happy path: fresh run produced a draft.
                        try:
                            await transition_to_awaiting_review(
                                session, digest_id=digest.id
                            )
                            await session.commit()
                            await _dm_admins_weekly_awaiting_review(
                                bot=bot, digest_id=digest.id
                            )
                        except Exception:
                            try:
                                await session.rollback()
                            except Exception:
                                logger.exception(
                                    "digest_weekly_job: review transition rollback failed"
                                )
                            logger.exception(
                                "digest_weekly_job: transition_to_awaiting_review failed"
                            )
                    case "awaiting_review" | "approved_for_publish" | "posting" | "posted":
                        # Idempotency hit: a prior run already advanced
                        # this window. No-op — admin is already in the loop.
                        logger.info(
                            "digest_weekly_job: existing %s row id=%s, no-op",
                            digest.status, digest.id,
                        )
                    case "rejected_by_admin" | "rejected_by_reaper":
                        # Last cycle's run was rejected; cron does NOT
                        # auto-regenerate. Admin must run
                        # /digest_now weekly --regenerate.
                        logger.info(
                            "digest_weekly_job: window has rejected run id=%s status=%s, "
                            "awaiting admin /digest_now weekly --regenerate",
                            digest.id, digest.status,
                        )
                    case "failed" | "cost_exceeded":
                        # Last cycle hit an error state; surface to admin DM.
                        await notify_admins_digest_failure(
                            bot,
                            digest_id=digest.id,
                            status=digest.status,
                            error_text=(
                                digest.error_text
                                or "weekly_digest_window_in_error_state"
                            ),
                        )
                        logger.error(
                            "digest_weekly_job: window in error state %s id=%s, "
                            "admin DM dispatched", digest.status, digest.id,
                        )
                    case "skipped" | "skipped_no_destination":
                        # Empty window or no destination — expected during
                        # initial rollout; no admin attention needed.
                        logger.info(
                            "digest_weekly_job: window status=%s id=%s, no action",
                            digest.status, digest.id,
                        )
                    case "redacted" | "redacted_edit_failed":
                        # Forget cascade hit before/during this run — body
                        # is already redacted. /digest_history shows audit.
                        logger.info(
                            "digest_weekly_job: window redacted id=%s status=%s",
                            digest.id, digest.status,
                        )
                    case _:
                        logger.error(
                            "digest_weekly_job: unexpected status %s for digest_id=%s",
                            digest.status, digest.id,
                        )

            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("digest_weekly_job rollback failed")
                logger.exception("digest_weekly_job: run_digest crashed")
    except Exception:
        logger.exception("digest_weekly_job: session setup failed")
```

**`digest_stale_review_reaper_job` body:**

```python
async def digest_stale_review_reaper_job() -> None:
    """48h DM + 7d auto-reject for awaiting_review weekly digests."""
    try:
        from sqlalchemy import text as _text

        async with async_session() as session:
            # Step 1: 7d auto-reject pass. Guarded UPDATE — type + status +
            # age all enforced in WHERE; rowcount drives both audit insert
            # and admin DM. RETURNING id makes the loop body trivial.
            seven_d_rows = await session.execute(
                _text(
                    """
                    UPDATE digests
                    SET status='rejected_by_reaper',
                        review_notes=COALESCE(review_notes,'') || '[stale_7d]',
                        updated_at=now()
                    WHERE type='weekly'
                      AND status='awaiting_review'
                      AND awaiting_review_at < now() - interval '7 days'
                    RETURNING id
                    """
                )
            )
            for row in seven_d_rows.fetchall():
                await session.execute(
                    _text(
                        "INSERT INTO digest_runs (digest_id, status, "
                        "error_text, started_at, finished_at) "
                        "VALUES (:id, 'rejected_by_reaper', "
                        "'review_deadline_exceeded', now(), now())"
                    ),
                    {"id": row.id},
                )
                logger.warning(
                    "digest_stale_review_reaper: rejected digest_id=%s", row.id
                )
            await session.commit()

            # Step 2: 48h notify pass — only DM once per row.
            forty_eight_h_rows = await session.execute(
                _text(
                    """
                    SELECT id, awaiting_review_at
                    FROM digests
                    WHERE type='weekly'
                      AND status='awaiting_review'
                      AND awaiting_review_at < now() - interval '48 hours'
                      AND (review_notes IS NULL
                           OR review_notes NOT LIKE '%[48h_notified]%')
                    """
                )
            )
            for row in forty_eight_h_rows.fetchall():
                # M4: guarded UPDATE — admin may have approved/rejected between
                # SELECT and UPDATE. Marker must only land on rows STILL in
                # awaiting_review (the only status where the marker is semantic).
                # rowcount=0 → log and skip the DM (state advanced under us).
                update_result = await session.execute(
                    _text(
                        "UPDATE digests SET "
                        "review_notes=COALESCE(review_notes,'') || '[48h_notified]', "
                        "updated_at=now() "
                        "WHERE id=:id AND status='awaiting_review' "
                        "RETURNING id"
                    ),
                    {"id": row.id},
                )
                if update_result.rowcount == 0:
                    logger.info(
                        "digest_stale_review_reaper: digest_id=%s no longer "
                        "awaiting_review, skipping 48h DM (state advanced)",
                        row.id,
                    )
                    continue
                # Only DM AFTER successful guarded marker — order guarantees
                # at-most-once notification per row.
                await _dm_admins_weekly_48h_reminder(digest_id=row.id)
            await session.commit()
    except Exception:
        logger.exception("digest_stale_review_reaper crashed")
```

**`digest_stale_approved_reaper_job`** (sub-component, added to the existing `digest_stale_posting_reaper_job` rather than a new job): widen the existing reaper's UPDATE to also catch `status='approved_for_publish'` rows older than 5 minutes. SQL extension:

```sql
UPDATE digests
SET status='failed',
    error_text=CASE
      WHEN status='posting' THEN 'stale_posting_reaper'
      WHEN status='approved_for_publish' THEN 'stale_approved_reaper'
    END,
    posting_started_at=NULL,
    updated_at=now()
WHERE
  (status='posting' AND posting_started_at < now() - interval '2 minutes')
  OR
  (status='approved_for_publish' AND approved_at < now() - interval '5 minutes')
RETURNING id, status as prev_status, error_text;
```

This keeps the reaper a single job; no new scheduler entry. The 5-minute threshold for `approved_for_publish` is conservative: the admin-approve-then-publisher-dispatch sequence (§5.E step 5) normally completes in <30s; 5 min handles network hiccups + retries.

### 5.H. Admin handlers — `bot/handlers/digest.py` (T8-06) — NEW commands

**Three new commands + one widened existing command:**

```python
@dp.message(Command("digest_now"), F.chat.type == "private")
async def cmd_digest_now(message: Message, bot: Bot):
    if not _is_admin(message):
        await message.answer("Только для админов.")
        return
    args = message.text.split()
    type_arg = args[1].strip().lower() if len(args) > 1 else "daily"
    regenerate = "--regenerate" in args[2:]
    if type_arg not in ("daily", "weekly"):
        await message.answer(
            "Использование: /digest_now [daily|weekly] [--regenerate]",
            parse_mode="HTML",
        )
        return
    # Compute window per type. Run run_digest. For weekly: on draft, call
    # transition_to_awaiting_review and DM admin "awaiting your review".
    # For daily: same Phase 7 publish-on-draft path.
    # --regenerate: if existing row in rejected_by_admin or
    # rejected_by_reaper, delete the existing row + audit insert
    # 'regenerated_by_admin', then re-run. Refuses if existing row is in
    # awaiting_review / approved_for_publish / posted / posting / running.


@dp.message(Command("digest_review"), F.chat.type == "private")
async def cmd_digest_review(message: Message):
    """List weekly digests in awaiting_review.

    Output: numbered list of (digest_id, window_start MSK, body length,
    citations count, awaiting_review_at, hours waiting). Each row links to
    /digest_preview <id> for full body display.
    """
    if not _is_admin(message):
        return
    # SELECT id, window_start, length(body_markdown), jsonb_array_length(citations),
    #   awaiting_review_at, EXTRACT(EPOCH FROM (now() - awaiting_review_at))/3600
    # FROM digests WHERE type='weekly' AND status='awaiting_review' ORDER BY id DESC


@dp.message(Command("digest_approve"), F.chat.type == "private")
async def cmd_digest_approve(message: Message, bot: Bot):
    """Approve a weekly digest → triggers publish.

    Usage: /digest_approve <digest_id>
    Calls digest_review.approve_digest(session, bot=bot, digest_id=id,
                                        admin_id=message.from_user.id,
                                        digest_config=...).
    On success: replies with t.me link to posted message.
    On DigestReviewInvalidState: replies with current status + suggestion.
    On DigestReviewNotFound: replies "Digest #id не найден".
    """
    if not _is_admin(message):
        return
    # ... parse id, dispatch ...


@dp.message(Command("digest_reject"), F.chat.type == "private")
async def cmd_digest_reject(message: Message):
    """Reject a weekly digest → terminal rejected_by_admin.

    Usage: /digest_reject <digest_id> [reason]
    Reason is optional free text, truncated to 1000 chars.
    Calls digest_review.reject_digest(session, digest_id=id,
                                       admin_id=message.from_user.id,
                                       reason=reason).
    On success: replies "Digest #id rejected. Reason: {reason}. /digest_now weekly --regenerate to retry."
    """
    if not _is_admin(message):
        return
    # ... parse id and optional reason ...
```

**Admin override semantics for `/digest_now weekly`:**
- Runs `run_digest` regardless of `memory.digests.weekly.enabled` flag.
- Still respects cost ceiling (separate weekly bucket).
- Still respects all governance filters.
- Existing-state handling:
  - `status='draft'` (weekly) → call `transition_to_awaiting_review`, DM admin "draft #id ready for review".
  - `status='awaiting_review'` → reply "Digest #id already awaiting your review. Use /digest_approve or /digest_reject."
  - `status='approved_for_publish'` → reply "Digest #id approved by admin #X, awaiting publish. Wait or check /digest_history."
  - `status='posting'` → polite "in flight" reply.
  - `status='posted'` → reply with `t.me/c/<chat_id>/<message_id>` link.
  - `status='rejected_by_admin'` or `'rejected_by_reaper'` → reply "Digest rejected. Use /digest_now weekly --regenerate to retry."
  - `status IN ('redacted','redacted_edit_failed')` → same Phase 7 wording, adjusted for weekly.
- `--regenerate` flag (Phase 8 v1 feature, accommodates Q5 design choice that admin cannot edit): only valid when existing row status is `rejected_by_admin` or `rejected_by_reaper`. Refused otherwise with explicit reply naming the current status.

**`--regenerate` transaction (H3 — must hold idempotency lock across DELETE + re-run):** the flag's DELETE-then-re-run sequence is wrapped in a SINGLE session transaction that acquires the Phase-7 `pg_advisory_xact_lock` for the `(type='weekly', ws, we)` triple BEFORE the audit insert + DELETE. The lock is released at COMMIT, at which point `run_digest` is called fresh — `run_digest` re-acquires the same advisory lock internally (PostgreSQL advisory locks are session-scoped; the same `AsyncSession` re-acquiring the same key is safe and idempotent within the same transaction tree). Pseudocode:

```python
async with session.begin():
    # H3: same transaction as run_digest's idempotency lock so a
    # concurrent cron fire blocks instead of inserting a duplicate.
    await _acquire_idempotency_lock(
        session, type='weekly',
        window_start=ws, window_end=we,
    )
    # Audit BEFORE delete so the trail survives even if delete fails.
    await session.execute(
        insert(DigestRun).values(
            digest_id=old.id,
            status='regenerated_by_admin',
            started_at=now(), finished_at=now(),
            error_text=f'regenerated from {old.status} by admin {admin_id}',
        )
    )
    await session.execute(
        delete(Digest).where(
            Digest.id == old.id,
            Digest.status.in_(['rejected_by_admin','rejected_by_reaper']),
        )
    )
    # Inside SAME transaction — run_digest re-uses the session-scoped lock.
    new_digest = await run_digest(
        session, type='weekly',
        window_start=ws, window_end=we,
        ledger_repo=..., provider=..., config=..., digest_config=...,
    )
# Lock released at COMMIT. Subsequent transition_to_awaiting_review and
# admin DM run AFTER the lock release.
```

**H5 — regenerate crash window (acknowledged, documented, accepted):** the DELETE row commit happens before `run_digest` issues its own INSERT. If the orchestrator process crashes in this narrow window:
  - (a) The row was already `rejected_by_admin` or `rejected_by_reaper` — content was NOT going to be published; admin had already rejected or the reaper auto-rejected.
  - (b) On restart, the next admin invocation of `/digest_now weekly [--regenerate]` either finds no row (idempotency creates fresh) or finds a partially-completed row (handled by status-aware branch matrix above).
  - (c) Forget events targeting the deleted row's citations have nothing to redact, but cascade idempotency preserves the event in `forget_events`; any FUTURE digest citing the same `message_version` will be redacted on its next state transition via §5.D scan widening + §5.K redactor widening.
  - (d) The new draft built by `run_digest` reads current governance-filtered state via `build_digest_context` — already-forgotten message_versions are filtered at context-build time and cannot resurrect into the new draft.

**Alternative considered (sentinel-row pattern):** instead of DELETE, UPDATE `status='regenerated_by_admin'` + `body_markdown=NULL`. Cleaner audit but requires adding `'regenerated_by_admin'` to the `digests.status` CHECK constraint AND the body-NOT-NULL invariant exempt list. **Rejected** for v1: the crash-window gap is acceptable (analysis above), DELETE keeps the table compact, and the additional CHECK widening is migration surface for no privacy benefit.

**Non-admin invocations:** silent no-op for all four commands (no leak of digest content). Verified by test against non-admin user id.

### 5.I. HTML rendering extension — `bot/services/digest_renderer.py` (T8-06 sub-component)

**Behaviour delta from Phase 7 (`render_digest_html` baseline §5.G of PHASE7_PLAN.md):**

1. Step 4 of the Phase 7 renderer (minimal Markdown → HTML) is extended to recognize one additional pattern:

   ```python
   # New: detect Phase 8 section header "## Раздел: …" → bold.
   body_html = re.sub(
       r"^##\s+Раздел:\s+(.+)$",
       r"<b>\1</b>",
       body_html,
       flags=re.MULTILINE,
   )
   ```

2. The footer template for weekly digests differs:

   ```python
   if digest_type == "weekly":
       footer = (
           f"\n\n<i>Еженедельный дайджест за {window_start_msk:%d.%m.%Y} – "
           f"{(window_end_msk - timedelta(seconds=1)):%d.%m.%Y}. "
           f"Полный список источников: /digest_history</i>"
       )
   ```

3. The truncation budget is widened: weekly bodies are larger (typical 3-6 sections × 3-7 bullets ≈ 1500-4000 chars body). The renderer keeps the same `3800 + 200` margin from Phase 7 but adds a conservative pre-render assertion: if `len(body_markdown) > 8000` → log structured warning `digest_weekly_oversized` (does NOT raise — truncation handles it). 8000 is a soft alert threshold; the hard cut still happens at 3800 chars per Phase 7.

4. Tag-balance assertion is unchanged. Section `<b>` opens MUST close on the same line (the regex above guarantees this; no multi-line `<b>` spans are introduced).

### 5.J. Admin-notify extensions — `bot/services/digest_admin_notify.py` (T8-04 / T8-05 sub-component)

**No new module.** Reuse the existing `notify_admins_digest_failure` helper from Phase 7. New event types passed via `error_text`:

| Event | `error_text` value | Triggered by |
|---|---|---|
| Weekly digest entered awaiting_review | `weekly_awaiting_review` | `digest_weekly_job` after `transition_to_awaiting_review` |
| 48h review reminder | `review_48h_reminder` | `digest_stale_review_reaper_job` first pass |
| 7d auto-reject | `review_7d_auto_rejected` | `digest_stale_review_reaper_job` second pass |
| Approve-then-publish failed | propagated from publisher | `digest_review.approve_digest` step 5 failure |
| Forget-during-review redact | `forget_redacted_during_review` | `_cascade_digests` → `redact_digest_for_forget` when status was `awaiting_review` |
| Stale approved reaper | `stale_approved_reaper` | `digest_stale_posting_reaper_job` extension |

These all flow through the same Phase 7 admin-notify path (`bot/services/digest_admin_notify.py`), which DMs the first admin in `settings.ADMIN_IDS`. The `weekly_awaiting_review` event has a richer message template (digest_id + window + body length + `/digest_approve` / `/digest_reject` command hints).

### 5.K. Redactor allowlist widening (T8-04 — C1 binding privacy fix)

`bot/services/digest_redactor.py` carries TWO hardcoded status allowlists that mirror the Phase 7 cascade scan filter. After T8-04 widens the cascade scan at `forget_cascade.py:840`, the redactor receives `awaiting_review` / `approved_for_publish` / `rejected_by_admin` rows that the cascade selected — but the redactor's hardcoded allowlists DROP them on entry. This produces a **silent privacy regression**: the cascade reports the layer as "completed" with the redactor row count, but the underlying digest body still contains the forgotten citation. An admin `/digest_approve` then publishes forgotten content.

**Required edits in T8-04 (both must land in the same PR as the cascade-scan widening):**

```python
# bot/services/digest_redactor.py:105 — early-return guard.
# BEFORE (Phase 7 baseline):
if current_status not in ("draft", "posted", "redacted", "redacted_edit_failed"):
    return

# AFTER (T8-04, must mirror §5.D scan filter EXACTLY):
_REDACTOR_ELIGIBLE_STATUSES = (
    "draft",
    "awaiting_review",
    "approved_for_publish",
    "posting",
    "posted",
    "redacted",
    "redacted_edit_failed",
    "rejected_by_admin",
)
if current_status not in _REDACTOR_ELIGIBLE_STATUSES:
    return
```

```python
# bot/services/digest_redactor.py:135-137 — no-citation-match UPDATE.
# BEFORE (Phase 7 baseline):
await session.execute(
    text(
        "UPDATE digests SET status='redacted', updated_at=now() "
        "WHERE id = :id AND status IN "
        "('draft','posted','redacted','redacted_edit_failed')"
    ),
    {"id": digest_id},
)

# AFTER (T8-04, identical 8-tuple via shared constant):
await session.execute(
    text(
        "UPDATE digests SET status='redacted', updated_at=now() "
        "WHERE id = :id AND status IN "
        "('draft','awaiting_review','approved_for_publish','posting',"
        " 'posted','redacted','redacted_edit_failed','rejected_by_admin')"
    ),
    {"id": digest_id},
)
```

**Anti-drift invariant:** the 8-tuple `_REDACTOR_ELIGIBLE_STATUSES` MUST be defined as a module-level constant in `digest_redactor.py` AND referenced (not duplicated as a string literal) anywhere the same enumeration appears. The cascade scan SQL at `forget_cascade.py:840` keeps the literal enumeration (inline SQL) but T8-04 adds an explicit comment cross-reference: `# MUST mirror digest_redactor._REDACTOR_ELIGIBLE_STATUSES`. Future widenings touch ONE constant + ONE inline SQL with the same audit trail.

**T8-04 acceptance addition:** Phase 11 binding test I6a asserts the redactor processes an `awaiting_review` row END-TO-END to `status='redacted'` with the body-redaction applied and admin-notify dispatched (not the silent skip that the un-widened allowlist would produce). The test must construct: digest in `awaiting_review` → forget_event on a cited mvid → run cascade worker → SELECT digest → assert `status='redacted' AND body_markdown LIKE '%REDACTED%'`.

### 5.L. Publisher trigger-state widening (T8-04 — C2 binding state-machine fix)

`bot/services/digest_publisher.py:118` hardcodes the trigger guard:

```python
if digest.status != "draft":
    raise DigestPublisherInvalidState(digest.status)
```

After Phase 8, `digest_review.approve_digest` step 5 calls `publish_digest` with a digest in `status='approved_for_publish'`, NOT `draft`. The unwidened guard raises immediately, leaving the row stuck in `approved_for_publish` forever (until the §5.G stale-approved reaper sweeps it 5 minutes later — privacy-safe but UX-broken).

**Required edits in T8-04 (assigned to T8-04 for sequencing — must land alongside `digest_review.py` so the approve→publish handoff is testable as one unit; T8-06 then ships the admin handler that calls `approve_digest`):**

```python
# bot/services/digest_publisher.py — Phase 7 baseline:
if digest.status != "draft":
    raise DigestPublisherInvalidState(digest.status)
# ... later, the guarded UPDATE to 'posting' status uses WHERE status='draft' ...

# AFTER (T8-04):
_PUBLISHER_TRIGGER_STATUSES = ("draft", "approved_for_publish")
if digest.status not in _PUBLISHER_TRIGGER_STATUSES:
    raise DigestPublisherInvalidState(
        digest_id=digest.id,
        current_status=digest.status,
        reason=f"publisher trigger requires status in {_PUBLISHER_TRIGGER_STATUSES!r}, found {digest.status!r}",
    )
```

The publisher's downstream guarded UPDATE (Phase 7 §5.F step 1) — the one that transitions to `posting` — MUST also widen its WHERE clause. The rowcount=0 handling follows the **canonical pattern from §5.E** (`_raise_invalid_state_after_guard_miss`) — Round 2 HIGH-Cdx-1 — so the publisher distinguishes "row deleted concurrently" (e.g. by `--regenerate` racing a stale `/digest_approve`) from "row in unexpected state" (e.g. cascade redacted it to `'redacted'`). Publisher reuses the same helper or implements an inline mirror with structured `DigestPublisherInvalidState` fields (`digest_id`, `current_status: Optional[str]`, `reason: str`) — both forms are acceptable as long as the classification is the same.

```python
# Per Codex H3 / Phase 7 publisher pattern — guarded transition.
# Rowcount=0 handler follows §5.E canonical pattern (R2 HIGH-Cdx-1):
# re-read, distinguish deleted-row from wrong-status, raise with
# structured fields. expected_status here is the 2-tuple
# ('draft','approved_for_publish'); the §5.E helper accepts a tuple
# and renders it correctly in the reason string.
result = await session.execute(
    text(
        "UPDATE digests "
        "SET status='posting', posting_started_at=now(), updated_at=now() "
        "WHERE id=:id AND status IN ('draft','approved_for_publish') "
        "RETURNING id"
    ),
    {"id": digest.id},
)
if result.rowcount == 0:
    # Canonical rowcount=0 classifier (§5.E):
    #   - row deleted concurrently (--regenerate during stale /digest_approve)
    #     → DigestPublisherInvalidState(current_status=None,
    #                                    reason='row_deleted_during_transition')
    #   - cascade redacted (status='redacted') or stale-approved-reaper
    #     failed (status='failed') or another worker posted (status IN
    #     ('posting','posted'))
    #     → DigestPublisherInvalidState(current_status=<status>,
    #         reason=f"expected status IN ('draft','approved_for_publish'), "
    #                f"found {current!r}")
    # The publisher invokes the §5.E helper with
    # expected_status=('draft','approved_for_publish') and re-raises as
    # DigestPublisherInvalidState — daily flow's existing handler at
    # bot/services/digests.py:digest_daily_job continues to catch
    # DigestPublisherInvalidState the same way.
    current = (
        await session.execute(
            text("SELECT status FROM digests WHERE id=:id"),
            {"id": digest.id},
        )
    ).scalar_one_or_none()
    await session.rollback()
    if current is None:
        raise DigestPublisherInvalidState(
            digest_id=digest.id,
            current_status=None,
            reason="row_deleted_during_transition",
        )
    raise DigestPublisherInvalidState(
        digest_id=digest.id,
        current_status=current,
        reason=(
            "expected status IN ('draft','approved_for_publish'), "
            f"found {current!r}"
        ),
    )
```

> **`DigestPublisherInvalidState` signature extension** (T8-04 mirrors the §5.E pattern): `digest_publisher.py` extends the exception to accept the same three structured kwargs (`digest_id: int`, `current_status: Optional[str]`, `reason: str`). Phase 7 callsites that raised positional `DigestPublisherInvalidState(status)` adapt to the keyword form; the daily callsite's `except DigestPublisherInvalidState as e:` block continues to work — it just gains the structured fields for richer logging.

**Daily flow unaffected:** the daily callsite (`digest_daily_job` after `run_digest` returns `status='draft'`) still passes a `draft` row to `publish_digest`, which matches the first entry of the widened tuple. The guarded UPDATE `WHERE status IN ('draft','approved_for_publish')` is a strict superset of the Phase 7 `WHERE status='draft'`; daily-path behavior is byte-for-byte preserved. Regression covered by Phase 11 binding suite L7a/b + C6 + I5a/b/c.

**T8-06 acceptance addition:** the weekly approve path test must exercise the full chain `transition_to_awaiting_review → approve_digest (step 4 commit) → publish_digest (status='approved_for_publish' trigger) → posting → posted` and assert each transition lands with the expected row state + audit insert.

---

## 6. Ratified Decisions (full list)

All 9 ratification questions locked 2026-05-15.

| # | Question | Decision | Rationale | Alternatives considered (rejected because…) |
|---|---|---|---|---|
| Q1 | Phase 8 scope | **WEEKLY DIGEST ONLY.** Reflection/observations re-scoped to Phase 9+. `PHASE8_PLAN_DRAFT.md` superseded; flagged for archival in T8-08 closure. | Reflection runs and `memory_events`/`memory_candidates` are an entirely separate axis (LLM passive observation about a member). Mixing them with weekly digest doubles surface area + delays the high-value editorial recap. ROADMAP row 8 says "weekly digest" explicitly. | (a) Ship both in one phase — rejected: scope creep, 2× FHR risk, no shared code. (b) Ship reflection first, weekly second — rejected: digest builds on Phase 7 closure (one cohesive arc), reflection needs more upstream design (Phase 9 prerequisite). |
| Q2 | Section schema | **JSONB inline in `body_markdown` as Markdown `## Раздел: …` headers + bullets.** No `digest_sections` table. Renderer bolds the header as `<b>…</b>`. | KISS. No migration scope creep (one CHECK ALTER vs new table + FK + indexes + ORM). Sections are a presentation concern of the body text, not a separate data axis. Forget cascade is unchanged — sections are just text. | (a) Separate `digest_sections` table — rejected: doubles migration size, adds FK from `digest_sections.digest_id → digests.id` + cascade DELETE semantics + new ORM + ix_digest_sections_* indexes, all for a presentational concern. (b) JSONB column on `digests` with structured sections — rejected: makes citations-per-section impossible without a third query layer; current citations are already a flat JSONB list keyed by `position` which can index into the body. |
| Q3 | Window | **ISO week Mon 00:00 MSK..next Mon 00:00 MSK (exclusive-end).** Cron fires **Mon 09:15 MSK** (H8 15-min stagger past daily 09:00; see §5.G + AC1 in §13) so the most-recently-completed ISO week is processed without colliding with the daily cron. | Closed-week — edits/forgets settle within the week. Mon 09:15 = 15-min offset from daily, easy to remember, no LLM gateway pressure overlap. ISO weeks are standard; isoweekday() returns 1 for Mon. | (a) Calendar week (varying first-day-of-week per locale) — rejected: surprise risk with mixed conventions. (b) Sun→Sat — rejected: less common in EU/RU community context. (c) Rolling 7d ending Mon 00:00 — equivalent to ISO if fired Mon, but ambiguous if fired any other day; cron locks the day. (d) Mon 09:00 sharp (no stagger) — rejected per H8: would collide with daily cron on every Monday. |
| Q4 | Approval flow | **Single-admin approval triggers publish.** No quorum, no two-admin gate in v1. | KISS for v1; the community is small enough that one admin's judgment is acceptable. Phase 8.5 backlog item: quorum if false-positive publishes occur. | (a) Two-admin quorum — rejected: blocks publish on admin availability gap (common in small admin team); v1 over-engineering. (b) Auto-publish after 48h with no admin action — rejected: violates ROADMAP "no auto-publish" gate. |
| Q5 | Admin edit pre-approval | **NO edit in v1.** Admin uses `/digest_reject + /digest_now weekly --regenerate` to produce a fresh draft. | Preserves citation invariant (admin-edited text could lose ids → break C7). Implementation cost of pre-approval edit is high (rich-text editor, citation-aware validator, audit trail). | (a) Free-text edit before approval — rejected: defeats citation invariant; admin could insert prose without ids. (b) Comment/annotation on bullets but no body edit — rejected: still surfaces v1.5 complexity (comments storage, UI); doesn't improve quality enough to justify. |
| Q6 | Stale-review reaper | **48h admin DM notify + 7d auto-reject** (`rejected_by_reaper` terminal, distinct from `rejected_by_admin`). Configurable via `DIGEST_REVIEW_DEADLINE_HOURS` (default 168) and `DIGEST_REVIEW_48H_NOTIFY_HOURS` (default 48). | Bounded queue size. 7d aligns with weekly cadence — by the time the next weekly fires, last week's review is cleared. Two-tier (notify→reject) reduces silent ignores. | (a) No reaper, drafts pile forever — rejected: queue bloat + admin guilt. (b) 24h notify + 7d reject — rejected: too aggressive; community admins sometimes need a day's slack. (c) Single 7d reject without notify — rejected: silent timeout, no escalation. |
| Q7 | Cost ceiling | **Separate `DIGEST_WEEKLY_USD_CEILING` env (default $5.00) + `DIGEST_WEEKLY_MONTHLY_USD_CEILING` ($20.00). C5 reformulation:** the weekly ceiling gates ONLY weekly synthesis, accounted in a separate ledger query (`WHERE d.type='weekly'`). It is INDEPENDENT of `LLM_DAILY_USD_CEILING` (Phase 5 shared bucket) and `DIGEST_DAILY_USD_CEILING` (Phase 7 daily-digest bucket). The earlier "must be < shared daily" stop signal is **REMOVED** — there is no must-be-less-than relationship; each bucket is independent. The Phase 5 shared bucket still fires regardless for ALL LLM calls (gateway-level guard), so a $5/week ceiling actually means "weekly synthesis halts when accumulated weekly spend in the current calendar month exceeds $5; in addition, the shared Phase 5 LLM_DAILY_USD_CEILING fires for the day's total LLM spend across daily + weekly + Q&A + cards". | Weekly context is 7× daily (~24k tokens vs ~8k); single ceiling could starve daily or weekly. Three independent buckets (Phase 5 shared, Phase 7 daily, Phase 8 weekly) keep accounting auditable per-feature. | (a) Single shared ceiling — rejected: cross-feature interference. (b) Make weekly < daily — rejected: artificial coupling; the two are different cadences with different volumes. (c) Per-call ceiling only — rejected: a runaway prompt loop could still burn through daily allowance; bucket sums are the canonical guard. |
| Q8 | Daily/weekly race | **No new logic.** Both daily and weekly cascade independently; the cascade walks `digests.citations` and redacts each row in turn. Order is deterministic via `ORDER BY d.id`. I6c binding test asserts both rows redact. | The `_cascade_digests` layer already iterates over `affected_digest_ids` and calls `redact_digest_for_forget` per row. Each row gets its own transaction-scoped lock. No cross-row coordination required. | (a) Transactional barrier across both rows — rejected: unnecessary serialization. (b) Skip one if the other is in `posting` — rejected: would leave one row stale + violate per-row contract. |
| Q9 | Migration head | **Migration 038 ONLY** ALTERs `ck_digests_status` + `ck_digests_body_markdown_not_null_for_visible_statuses` + `ck_digest_runs_status` + ADDs `published_by_admin_id`, `approved_at`, `review_notes`, `awaiting_review_at` + partial index `ix_digests_status_awaiting_review` + new `ck_digests_approved_audit`. No schema rework. Stamp `revision='038'`, `down_revision='037'`. | Aligns with Phase 7's "extend existing tables" pattern. CHECK swap is the canonical alembic move for enum widening in Postgres (no native ENUM type used — TEXT + CHECK is the project convention). | (a) New `digest_review` table with FK to `digests.id` — rejected: doubles surface area; review state is a property of the digest, not a separate entity. (b) New `digest_type='weekly_review'` enum value — rejected: confuses `type` (window cadence) with `status` (lifecycle); status is the right axis. |

**Decisions inherited from Phase 7 (re-affirmed for weekly path):**
- Decisions 2-16 from PHASE7_PLAN.md §6 carry over unchanged where applicable: HTML parse mode, no inline citations in public post, governance filter, citation contract (`[[cs:UUID]]` / `[[mv:INT]]`), `bot.edit_message_text` policy, `TelegramForbiddenError` handling (kicked bot privacy stop signal — same trade-off, same operator escalation path).

---

## 7. Tickets

Sprint grid for Phase 8. **L6 wave layout (revised) — Round 1 review correctly identified that T8-02 and T8-03 are independent post-T8-01, and that T8-06 + T8-07 can also overlap.** Final wave structure:

- **Wave 1:** T8-S0 (this PR, docs ratify)
- **Wave 2:** T8-01 (migration) — sequential, blocks everything below
- **Wave 3:** T8-02 (run + prompt) ∥ T8-03 (context) — **parallel** (independent post-T8-01)
- **Wave 4:** T8-04 (review SM + cascade scan widen + redactor widen + publisher widen) ∥ T8-05 (scheduler + reapers) — **parallel** (T8-05 depends on T8-04 ONLY for the `transition_to_awaiting_review` import; can ship a stub-import branch concurrently with T8-04 finalizing)
- **Wave 5:** T8-06 (handlers + renderer) ∥ T8-07 (binding tests) — **parallel** (T8-07 writes red tests against the T8-06 in-progress branch; both PRs merge in same wave once both green)
- **Wave 6:** T8-08 (closure docs + FHR) — sequential, requires all prior waves merged + Phase 11 42/42 green

| ID | Title | Component | Wave | Size | Files touched | Deps | Status |
|---|---|---|---:|---|---|---|---|
| **T8-S0** | Sprint 0: AUTHORIZED_SCOPE.md update + PHASE8_PLAN.md commit | docs | 1 | S | `docs/memory-system/PHASE8_PLAN.md` (new), `docs/memory-system/AUTHORIZED_SCOPE.md` (Phase 8 block insert), `docs/memory-system/ROADMAP.md` (row 8 note update) | none | **in-flight (this PR)** |
| **T8-01** | Migration 038 `extend_digests_review_states` + ORM model extension | A | 2 | M | `alembic/versions/038_extend_digests_review_states.py` (new), `bot/db/models.py` (Digest cols), `tests/db/test_digests_review_schema.py` (new) | T8-S0 | not-started |
| **T8-02** | `run_digest` weekly path + weekly cost ceiling (H6 type filter) + `digest_weekly_v0_1_0` prompt + `synthesize_digest` template wiring + section allowlist (M1) | B | 3 | L | `bot/services/digests.py` (signature widen, `_cost_ceiling_breached` type kwarg), `bot/services/llm_prompts/digest_weekly_v0_1_0.py` (new), `bot/services/llm_gateway.py` (template id wiring, section-aware parsing + 5-name allowlist), `tests/services/test_digests_weekly.py` (new) | T8-01 | not-started |
| **T8-03** | `build_digest_context` weekly extension (7-day window, larger budget). #291 advisory check. | B2 | 3 | M | `bot/services/digest_context.py` (signature widen, weekly overrides), `tests/services/test_digest_context_weekly.py` (new) | T8-01 | not-started; advisory: land #291 first (M7) |
| **T8-04** | `digest_review.py` state machine (H2 guarded UPDATEs) + `_cascade_digests` scan-filter widening (§5.D) + **`digest_redactor.py` allowlist widening (§5.K, C1)** + **`digest_publisher.py` trigger-state widening (§5.L, C2)** + admin-notify on review-state redaction (M2) | C | 4 | XL (split allowed — see §14) | `bot/services/digest_review.py` (new), `bot/services/forget_cascade.py:840` (status filter widen), `bot/services/digest_redactor.py` (allowlist constant + UPDATE clause), `bot/services/digest_publisher.py:118` + the guarded UPDATE (trigger widen), `tests/services/test_digest_review.py` (new), `tests/services/test_forget_cascade_digests_weekly.py` (new), `tests/services/test_digest_publisher_weekly_trigger.py` (new) | T8-02, T8-03 | not-started |
| **T8-05** | Scheduler `digest_weekly_job` (H4 status-aware match) + `digest_stale_review_reaper_job` (M4 guarded UPDATE) + `digest_stale_approved_reaper` widening + H8 cron stagger (09:15) | D | 4 | M | `bot/services/scheduler.py` (3 job adds / 1 widen), `bot/services/digests.py` (job bodies), `bot/config.py` (env var registration), `tests/services/test_scheduler_weekly.py` (new, includes L3 integration test) | T8-04 (import-only) | not-started |
| **T8-06** | Admin handlers `/digest_approve` `/digest_reject` `/digest_review` + `/digest_now weekly [--regenerate]` widening (H3 single-transaction lock) + renderer section bolding + weekly footer + L4 service-layer reason truncation | E | 5 | M | `bot/handlers/digest.py` (4 commands), `bot/services/digest_renderer.py` (section header regex + weekly footer), `tests/handlers/test_digest_handlers_weekly.py` (new), `tests/services/test_digest_renderer_weekly.py` (new) | T8-04, T8-05 | not-started |
| **T8-07** | Phase 11 binding tests L8a/b + C7 (M1 allowlist) + I6a + I6b.1/.2/.3 (C3 split) + I6c + R5.a/.b/.c/.d (H7 corrected commands) — 12 new cases; regression: existing 30/30 preserved | F | 5 | M | `tests/evals/test_digest_leakage.py` (L8a/b extend), `tests/evals/test_citations.py` (C7), `tests/evals/test_refusal.py` (R5.a/b/c/d), `tests/evals/test_digest_forget_cascade.py` (new — I6a/b.1/b.2/b.3/c), `tests/fixtures/golden_recall/seed_v1/seed_meta.yaml` (baseline 30→42 if numeric thresholds change) | T8-04, T8-05, T8-06 (read-after for assertion target) | not-started |
| **T8-08** | Closure docs (PHASE8_ROLLOUT incl. downgrade runbook, IMPLEMENTATION_STATUS, ROADMAP, CLAUDE, AUTHORIZED_SCOPE) + PHASE8_PLAN_DRAFT.md archival flag + GIN-index follow-up issue (M6) + FHR | G | 6 | S | `docs/memory-system/PHASE8_ROLLOUT.md` (new), `docs/memory-system/IMPLEMENTATION_STATUS.md` (T8-* rows), `docs/memory-system/ROADMAP.md` (row 8 CLOSED), `CLAUDE.md` (Phase 8 closure block), `docs/memory-system/AUTHORIZED_SCOPE.md` (Phase 8 CLOSED note), `docs/memory-system/prompts/PHASE8_PLAN_DRAFT.md` (top banner: SUPERSEDED) | T8-07 | not-started |

### Per-ticket acceptance criteria

**T8-S0 acceptance:**
- `docs/memory-system/PHASE8_PLAN.md` committed (this file).
- `AUTHORIZED_SCOPE.md` "NOT in Phase 7 scope (defer to Phase 8+)" bullet for "Weekly digest scheduler / handler / publisher (Phase 8)" REMOVED (lines ~238-241 of the Phase 7 closure block).
- New `## Authorized: Phase 8 — Weekly editorial digest (2026-05-15)` block inserted immediately BEFORE the `## NOT authorized` heading. Block mirrors Phase 7 structure: TL;DR, owner (Orch A), scope reference to this `PHASE8_PLAN.md`, NOT-in-scope list mirroring §3 of this plan, ratification date.
- `ROADMAP.md` row 8 status updated from `NO` to `AUTHORIZED 2026-05-15 — plan ratified`.
- Single PR, docs-only, no code. PR title: `docs(memory): ratify Phase 8 plan + authorize weekly digest implementation`.

**T8-01 acceptance:**
- `alembic/versions/038_extend_digests_review_states.py` upgrades cleanly on a Phase-7 baseline DB and downgrades cleanly (with the dual-table data-presence guards from §5.A).
- All CHECK additions use NOT VALID + VALIDATE pattern (H1) — no AccessExclusiveLock-while-scanning.
- ORM `Digest` mapped class adds `awaiting_review_at`, `published_by_admin_id`, `approved_at`, `review_notes` with correct types + nullability.
- `tests/db/test_digests_review_schema.py` asserts:
  - `ck_digests_status` accepts all 14 status values, rejects unknown.
  - **`ck_digest_runs_status` accepts all 11 audit values** (running, finished, failed, skipped, cost_exceeded, skipped_no_destination, awaiting_review, approved_for_publish, rejected_by_admin, rejected_by_reaper, regenerated_by_admin), rejects unknown. (C4: this was missing from the earlier acceptance — explicit test required.)
  - Body-NOT-NULL invariant extends to `awaiting_review` / `approved_for_publish` / `rejected_by_admin` / `rejected_by_reaper`.
  - Partial index `ix_digests_status_awaiting_review` exists.
  - `ck_digests_approved_audit` rejects `status IN ('approved_for_publish','posting','posted')` rows missing `published_by_admin_id` or `approved_at` when `type='weekly'`, exempts `type='daily'`.
- Downgrade tests:
  - Insert a Phase-8 review-status row on `digests`, attempt downgrade → fails with the expected RAISE EXCEPTION mentioning `PHASE8_ROLLOUT.md`.
  - Insert a Phase-8 audit-status row on `digest_runs`, attempt downgrade → fails with the expected RAISE EXCEPTION for `digest_runs`.
  - **R2 HIGH-Cdx-2:** insert a row with `status='posting'` (with both `type='daily'` and `type='weekly'` covered), attempt downgrade → fails with the expected RAISE EXCEPTION mentioning the `posting` in-transit block. After UPDATE'ing the row to a terminal state (e.g. `posted`), re-attempt downgrade → succeeds.

**T8-02 acceptance:**
- `run_digest(type='weekly', ...)` returns existing digest by `(type='weekly', ws, we)` without LLM call on re-run (idempotency).
- **H6 type filter:** the refactored `_cost_ceiling_breached(session, digest_config, *, type='daily')` SQL adds `WHERE d.type = :type` to BOTH daily-bucket and monthly-bucket queries. Daily callsite passes `type='daily'`; weekly callsite passes `type='weekly'`. Existing Phase 7 daily behavior is byte-for-byte preserved (default kwarg `type='daily'` for back-compat).
- New weekly run: cost ceiling check fires before LLM. Exceeded → BOTH `digests` row (`type='weekly', status='cost_exceeded', body_markdown=NULL, citations='[]'::jsonb`) AND `digest_runs` row (`status='cost_exceeded', error_text='weekly digest budget exceeded'`) created.
- New weekly run: `synthesize_digest(prompt_template_version='digest-weekly-v0.1.0')` called exactly once with weekly context; LedgerRepo placeholder + update records cost; `body_markdown` contains `## Раздел: <name>` section headers + bullets with `[[cs:UUID]]` / `[[mv:INT]]` tokens.
- **M1 section allowlist:** the new prompt template module exports `SECTION_NAME_ALLOWLIST = frozenset({"Объявления","Обсуждения","Знания и ресурсы","Встречи и события","Прочее"})` as a module-level constant. The `_extract_sections` helper (in `llm_gateway.py`) returns parsed sections; a post-parse validator emits a structured warning if `section_title not in SECTION_NAME_ALLOWLIST` (does NOT raise — soft contract per §8 stop signal). Test asserts: a normal weekly output with all-allowed titles passes silently; a synthetic output with `## Раздел: Совет` triggers exactly one warning log entry.
- Section-aware parsing in `synthesize_digest`: bullets within section headers parse correctly; section headers do NOT count as bullets; bullet-level invariant fires identically per Phase 7.
- Empty window short-circuit: `status='skipped'`, no LLM call.
- Hallucinated citation IDs dropped; if any bullet has zero valid citations → `DigestCitationValidationError`.
- Unit tests cover: idempotency, weekly cost ceiling (independent of daily), empty window, LLM error, hallucinated citation drop, section header recognition, allowlist warning behavior.

**T8-03 acceptance:**
- `build_digest_context(type='weekly', ...)` returns a `DigestContext` with 7-day window bounds, `LIMIT 100` cards, `LIMIT digest_config.weekly_raw_message_top_n` raw messages, token budget `digest_config.weekly_token_budget_input - 1000`.
- Cards-first ordering preserved: cards fetched first; raw messages added only when `len(cards) < digest_config.weekly_min_cards_threshold`.
- Token budget enforced; raw messages dropped first on overflow.
- `_forget_excludes_predicate` shared helper still used identically.
- Unit tests cover: forgotten exclusion in weekly window, redacted exclusion, offrecord exclusion, weekly card threshold fallback, weekly token overflow drop.

**T8-04 acceptance (XL ticket — covers C1 + C2 + H2 + cascade widening):**
- **H2 guarded UPDATE pattern:** every state-machine transition in `digest_review.py` is a single guarded UPDATE keyed on expected source state, with `RETURNING id`. SELECT FOR UPDATE + ORM mutation is NOT used. Tests verify:
  - `transition_to_awaiting_review` rowcount=0 path raises `DigestReviewInvalidState(current_status)`. Idempotent re-call on `awaiting_review` row is a no-op.
  - `approve_digest` step 3 stale-citation guarded UPDATE returns rowcount=0 when cascade already redacted between revalidation and commit (I6b.2 path).
  - `approve_digest` step 4 transitions `awaiting_review → approved_for_publish` with `published_by_admin_id` + `approved_at` set, then dispatches `publish_digest`. On stale citations: `failed` with `error_text='citations_stale_at_approval'`.
  - `reject_digest` rowcount=0 raises `DigestReviewInvalidState(status)`.
- **§5.D cascade scan widened:** `forget_cascade.py:840` WHERE clause includes 8 statuses (`draft, awaiting_review, approved_for_publish, posting, posted, redacted, redacted_edit_failed, rejected_by_admin`). Verified by reading the source after change.
- **§5.K redactor allowlists widened (C1 — critical privacy fix):** `digest_redactor.py:105` early-return guard and `:135-137` UPDATE WHERE clause both use the same 8-tuple via the module-level constant `_REDACTOR_ELIGIBLE_STATUSES`. Verified by reading both lines after change AND by binding test I6a — insert digest in `awaiting_review`, fire forget on cited mvid, run cascade, assert `digest.status='redacted'` AND `body_markdown LIKE '%REDACTED%'` (not silent skip).
- **§5.L publisher trigger widened (C2 — critical state-machine fix):** `digest_publisher.py:118` trigger guard accepts `('draft', 'approved_for_publish')`; the guarded UPDATE WHERE clause to `'posting'` also accepts both source statuses. Daily flow regression-tested via L7a/b + C6 + I5a/b/c (Phase 11 baseline). New `tests/services/test_digest_publisher_weekly_trigger.py` covers: weekly approve handoff posts successfully; publisher called with `posted` row raises `DigestPublisherInvalidState`; publisher called on `redacted` row (I6b.3 race) raises with `error_text='publisher_status_mismatch'`.
- **M2 admin notify on review-state redact:** `redact_digest_for_forget` when called on `awaiting_review` row fires `notify_admins_digest_failure` with `error_text='forget_redacted_during_review'`. This is observable in `/digest_review` listing audit (the row drops out of the list silently; admin DM explains why).
- Unit tests cover: each state-machine transition (guarded UPDATE rowcount=0 path AND rowcount=1 path), citations-stale-at-approval (I6b.2), scan-filter widening (insert weekly row in EACH status, fire forget, assert redact happens for the 8 in-scope statuses and is skipped for `skipped`/`failed`/`cost_exceeded`/`skipped_no_destination`/`rejected_by_reaper`).

**T8-05 acceptance:**
- **H8 stagger:** Scheduler job `digest_weekly` registered with `day_of_week="mon"`, `hour=settings.DIGEST_WEEKLY_HOUR_MSK` (default 9), `minute=settings.DIGEST_WEEKLY_MINUTE_MSK` (default 15 — 15-min stagger past daily 09:00). `timezone=ZoneInfo("Europe/Moscow")`, `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`. A unit test asserts the registered cron expression is `09:15 MSK Mon`, distinct from `digest_daily`'s `09:00 MSK every day`.
- Flag `memory.digests.weekly.enabled` default OFF in `feature_flags`.
- Job body re-checks flag and exits early if disabled.
- **H4 status-aware match:** `digest_weekly_job` body's idempotency-return branch handles 8 statuses explicitly via Python `match`: `draft` → transition + DM; `awaiting_review | approved_for_publish | posting | posted` → no-op log; `rejected_by_admin | rejected_by_reaper` → log "awaiting admin --regenerate"; `failed | cost_exceeded` → `notify_admins_digest_failure` + error log; `skipped | skipped_no_destination` → info log; `redacted | redacted_edit_failed` → info log; unexpected status → error log (no crash). Tests cover EACH branch with a stubbed `run_digest` return value.
- ISO week computation: mock `datetime.now(tz=MSK)` returning Mon 09:15 MSK any week W; asserts `window_start` = W-7d 00:00 MSK in UTC, `window_end` = W 00:00 MSK in UTC.
- **M4 guarded reaper UPDATE:** `digest_stale_review_reaper_job` 48h pass DMs admin ONLY AFTER a guarded UPDATE `WHERE id=:id AND status='awaiting_review'` succeeds (rowcount=1). If the admin approved/rejected between SELECT and UPDATE, rowcount=0 and the DM is skipped (log explains "state advanced under us"). 7d pass uses the same guarded WHERE pattern. Both passes tested with concurrent state mutation simulation.
- `digest_stale_review_reaper_job` 7d pass moves `awaiting_review` rows older than 7d to `rejected_by_reaper` with audit insert + admin DM.
- `digest_stale_posting_reaper_job` widened to also catch `approved_for_publish` rows older than 5 min → `failed` with `error_text='stale_approved_reaper'`.
- **L3 integration test:** real apscheduler instance with `day_of_week="mon"` cron, mocked clock fires at Mon 09:15 MSK, assert the job body actually executes (not just registered) — guards against future `day_of_week` validation drift.
- Unit tests cover: window computation, all H4 match branches, reaper transitions, double-fire reaper idempotency, M4 guard.

**T8-06 acceptance:**
- `/digest_now weekly` admin-only, runs regardless of flag, respects weekly cost ceiling, transitions to `awaiting_review` on draft (NOT auto-publish).
- **H3 `/digest_now weekly --regenerate` single-transaction:** rejects unless current status is `rejected_by_admin` or `rejected_by_reaper`. When valid, the entire (acquire-idempotency-lock + audit-insert `regenerated_by_admin` + DELETE existing row + `run_digest` re-call) sequence wraps in ONE `session.begin()`. Concurrent cron fire blocks on the advisory lock. Test: spawn two concurrent `--regenerate` calls on the same rejected row — first wins, second gets `DigestReviewInvalidState` (because by the time it acquires the lock, the row is `awaiting_review`).
- `/digest_review` lists awaiting_review weekly digests with id/window/citations/hours-waiting columns.
- `/digest_approve <id>` (covers C2 end-to-end weekly happy path AC): transitions to `approved_for_publish` + dispatches publisher; publisher's widened trigger guard accepts `approved_for_publish` (§5.L); on success replies with `t.me/c/<chat_id>/<message_id>` link; on `DigestReviewInvalidState` replies with current status + suggestion.
- `/digest_reject <id> [reason]`: transitions to `rejected_by_admin` with reason; **L4: service-layer truncation** (reason `(reason or 'no reason given')[:1000]`, column type stays `Text` unbounded). Reply mentions `--regenerate` flag.
- Non-admin invocations: all four commands silently no-op (no leak — tested against non-admin user id; R5.a + R5.b binding cases verify).
- Renderer recognizes `## Раздел: <name>` headers and bolds them (`re.sub(r"^##\s+Раздел:\s+(.+)$", r"<b>\1</b>", body_html, flags=re.MULTILINE)`). Weekly footer template differs from daily (full date range, not single day).
- Unit tests cover: admin gate, idempotency reuse, regenerate flag (incl. crash-window H5 documented behavior), awaiting_review listing, approve+publish happy path (weekly), reject + reason (L4 truncation), citations-stale at approval (I6b.2), section bolding render correctness, weekly footer format.

**T8-07 acceptance (12 new binding cases; existing 30/30 baseline preserved):**

- `tests/evals/test_digest_leakage.py` extended:
  - **L8a:** forgotten `message_version` is NOT in `body_markdown` of a weekly digest published after the forget event (cascade widening + redactor allowlist widening exercised).
  - **L8b:** forgotten `card_source` is NOT in body of a weekly digest (dual-kind path).
- `tests/evals/test_citations.py` extended:
  - **C7:** every weekly bullet has ≥1 citation_id resolvable in DB; section headers (`## Раздел: <name>`) are correctly skipped by the bullet scanner; the citation_id appears in the input context (no hallucination); **M1 section title allowlist enforced:** each section header has `<name>` in `SECTION_NAME_ALLOWLIST` (compare post-parse string against the frozenset).
- `tests/evals/test_refusal.py` extended (H7 — references CORRECT command names — `/digest_publish` does NOT exist):
  - **R5.a:** non-admin invokes `/digest_approve <id>` → silent no-op (mirrors Phase 6 admin gate pattern). NO reply, NO state mutation.
  - **R5.b:** non-admin invokes `/digest_reject <id>` → silent no-op.
  - **R5.c:** admin invokes `/digest_approve <id>` on already-`posted` row → reply `DigestReviewInvalidState(current_status='posted')` + link to existing posted message; NO publisher re-dispatch.
  - **R5.d:** admin invokes `/digest_approve <id>` on `rejected_by_admin` row → reply `DigestReviewInvalidState(current_status='rejected_by_admin')` + `--regenerate` suggestion; NO publisher re-dispatch.
- New file `tests/evals/test_digest_forget_cascade.py`:
  - **I6a:** `forget_event` on `mvid` cited by a weekly digest in `awaiting_review` triggers redact (verifies §5.D scan-filter widening + §5.K redactor allowlist widening end-to-end). Also tests `approved_for_publish` and `rejected_by_admin` statuses in the same shape.
  - **I6b.1** (forget BEFORE `/digest_approve`): cascade redacts first → admin sees `[REDACTED]` body OR `/digest_approve` raises `DigestReviewInvalidState(current_status='redacted')`. Assertion: status='redacted' AND admin reply contains guidance about forgotten content.
  - **I6b.2** (forget DURING `approve_digest` — between step 3 revalidation and step 4 commit): either approve commit wins (status='approved_for_publish') then publisher's own revalidation catches stale citation → terminal `failed` with `error_text='citations_stale_at_publish'`; OR cascade wins (status='redacted'), approve_digest's step-4 guarded UPDATE returns rowcount=0 → admin reply `DigestReviewInvalidState`. **Final status MUST NOT be 'posted' under any race interleaving.**
  - **I6b.3** (forget AFTER approve commit, BEFORE publisher dispatch): cascade widening (§5.D) finds `approved_for_publish` row, calls redactor (§5.K) → status='redacted'. Publisher's widened guard (§5.L) sees `redacted`, NOT in trigger tuple → guarded UPDATE rowcount=0 → terminal `failed` with `error_text='publisher_status_mismatch'`. Assertion: status='redacted' OR 'failed' (race winner determines); NO posted_message_id; audit row inserted explaining the path taken.
  - **I6c:** concurrent forget event affecting both a daily and a weekly digest citing the same mvid — both rows redact independently within the same cascade transaction; final statuses both `redacted`; deterministic order via `ORDER BY d.id` in scan SQL.
- **Baseline 30/30 → 30 preserved:** L1, L2, L3a, L3b, L3c, L4, L5, L6a, L6b, L6c (test_leakage.py = 10) + C1, C2, C3, C4, C5a, C5b, C5c, C5d (test_citations.py = 8) + R1, R2, R3a, R3b, R3c, R4 (test_refusal.py = 6) + L7a, L7b, C6, I5a, I5b, I5c (test_digest_leakage.py = 6). **New total: 42/42** (12 new).
- `tests/fixtures/golden_recall/seed_v1/seed_meta.yaml` baseline thresholds updated if Phase 11 binding row-counts change (otherwise unchanged — invariants are binary).
- Idempotency regression test: two concurrent `run_digest(type='weekly')` calls for the same window → only one digest row, second blocks via advisory lock OR returns existing row.

**T8-08 acceptance:**
- `docs/memory-system/PHASE8_ROLLOUT.md` operator playbook: env vars list, flag toggle order, dry-run via `/digest_now weekly`, admin review walkthrough, monitor first cron fire, escalation contacts, **dedicated "downgrade" section** (M5) covering the dual-table data-presence pre-flight (`digests` + `digest_runs`) and the operator path to clean Phase-8 rows before downgrade. Mirrors the structure of `PHASE7_ROLLOUT.md`.
- `docs/memory-system/IMPLEMENTATION_STATUS.md` updated: every T8 ticket marked DONE with PR refs.
- `docs/memory-system/ROADMAP.md` row 8 marked CLOSED with PR list.
- `CLAUDE.md` "Memory System Cycle" section adds Phase 8 closure block (mirrors Phase 7 wording structure).
- `AUTHORIZED_SCOPE.md` "Authorized: Phase 8" block updated to CLOSED 2026-MM-DD; "NOT authorized" Phase 9 (reflection/observations) line remains unchanged.
- `docs/memory-system/prompts/PHASE8_PLAN_DRAFT.md` top banner: `# SUPERSEDED — see PHASE8_PLAN.md. Content below describes the original reflection/observations design, deferred to Phase 9+.` Document body preserved for historical reference (the reflection backlog feeds Phase 9 planning).
- **M6 GIN-index follow-up issue filed** in GitHub Issues, label `phase:8.5`, referencing `forget_cascade.py:840+` scan rewrite from LATERAL `jsonb_array_elements` → `@>` containment.
- Final Holistic Review (FHR) report committed under `docs/memory-system/PHASE8_FHR_REPORT.md` (mirrors Phase 7 closure pattern).

---

## 8. Stop Signals (apply to all streams)

A Phase 8 stream must STOP and surface immediately if any fire:

- Weekly digest context or body contains forgotten content (forbidden tag, redacted, or tombstoned source) → STOP, mark `failed`.
- Weekly digest citations reference raw message text instead of ids → STOP.
- Direct LLM provider import outside `llm_gateway` anywhere in `bot/services/digests*.py`, `bot/handlers/digest.py`, `bot/services/digest_review.py` → STOP.
- Weekly cost ceiling exceeded → `digest_runs` records, no LLM call, admin notify.
- Posting destination unset → `status='skipped_no_destination'`, no error raised, no admin notify (expected during rollout — same as Phase 7).
- Weekly scheduler would run while `memory.digests.weekly.enabled` is OFF → both layers (registration check + runtime re-check) prevent execution.
- Forget cascade scan WHERE clause at `forget_cascade.py:840` does NOT include `awaiting_review` and `approved_for_publish` → STOP, this is a privacy leak (R1 / I6a). Acceptance T8-04 must verify the widened filter by reading the source after change.
- **Migration 038 head conflict with concurrent Phase 9+ work** → STOP and rebase before continuing. The migration counter is monotonic; if `alembic heads` shows a divergent head (e.g. someone landed 039 on main before 038), T8-01 must rebase its own migration to the next available number.
- **Migration 038 partially applied** (CHECK widened on `digests` but NOT on `digest_runs`, or vice versa) → STOP, fix sequencing. The migration's Group 1 + Group 2 (per §5.A) MUST land in the same transaction; alembic runs the entire `upgrade()` function in one transaction by default, so this is the project default — but if anyone refactors to split into multiple migrations, both CHECK widenings MUST move together or the first `transition_to_awaiting_review` raises a CHECK violation on `digest_runs`.
- **`DIGEST_DESTINATION_CHAT_ID == DIGEST_SOURCE_CHAT_ID`** at startup → STOP (inherited from Phase 7).
- **Redactor allowlist drift** (C1): if T8-04 widens the cascade scan at `forget_cascade.py:840` but NOT the redactor allowlists at `digest_redactor.py:105` and `:135-137` → STOP, silent privacy regression. T8-04 PR review MUST verify BOTH files in the same diff; §5.K constraint constant `_REDACTOR_ELIGIBLE_STATUSES` is the binding source of truth.
- **Publisher trigger-state drift** (C2): if T8-04 widens `digest_review.approve_digest` to transition to `approved_for_publish` but NOT `digest_publisher.publish_digest:118` to accept it as a trigger state → STOP, the approve→publish handoff hangs and the row sits in `approved_for_publish` until the 5-min reaper failed it. §5.L is the binding fix.
- `bot.edit_message_text` raises `TelegramBadRequest` on a posted weekly redaction → must NOT propagate; route to erratum path (same as Phase 7 §5.H).
- `TelegramForbiddenError` on weekly redaction edit → **kicked bot privacy gap** (inherited from Phase 7 stop signals). Operator runbook (T8-08) MUST include escalation steps.
- Admin `/digest_approve` called on a digest whose citations are stale (forgotten between draft and approve) → publisher revalidation fails the run, `status='failed'`, `error_text='citations_stale_at_approval'`, admin notify with explicit guidance to `/digest_now weekly --regenerate`.
- Citation parsing yields a weekly bullet with zero valid citation ids after hallucinated-drop → must FAIL the run, do NOT transition to `awaiting_review`.
- Gateway revalidation discovers a stale source between context build and provider call → must FAIL the run, no LLM call (same as Phase 7).
- Section header `## Раздел: …` with a non-allowlisted title returned by LLM → log structured warning, render as-is (the renderer regex is permissive; the prompt's allowlist is a soft contract, not a parser-enforced one). Phase 8.5 backlog item: hard-enforce the allowlist via post-parse validation if drift is observed.
- Layer order violation: `digests` cascade layer placed AFTER `card_sources` (Phase 7 contract) → STOP. T8-04 does NOT move the layer; the widening is scan-filter only.
- **Stale-review reaper triggered (7d pass).** Any rejection event indicates an admin gap. Admin-notify fires per rejected row. Operator MUST investigate: admin availability, alert routing, training on the review commands.
- **Stale-approved reaper triggered (5 min pass).** Indicates a crash between `approve_digest` step 4 commit and step 5 publisher dispatch. Operator MUST inspect logs for the gap; fail-forward by manually re-running `/digest_now weekly --regenerate` (idempotency returns the now-`failed` row; regenerate replaces it).
- Concurrent daily + weekly forget event handling (I6c) shows partial redaction (one row redacted, the other not) → STOP, race not isolated correctly. The per-row redaction loop in `_cascade_digests` (`forget_cascade.py:865-879`) must complete the loop or fail loudly.
- **Phase 11 daily/baseline binding suite regresses** (the verified 30/30 from §0/§10 — earlier "34/34" framing was a math drift corrected in Round 1) → STOP, the weekly extensions broke a daily invariant. Re-run T8-07 baseline before any other Wave 3+ work.

---

## 9. Operator Playbook Hooks

### Env vars introduced (T8-05 wires into `bot/config.py`)

| Var | Default | Purpose |
|---|---|---|
| `DIGEST_WEEKLY_ENABLED` | `false` | Gate scheduler registration (still double-checked by `memory.digests.weekly.enabled` flag in DB). |
| `DIGEST_WEEKLY_HOUR_MSK` | `9` | Cron fire hour (Europe/Moscow). |
| `DIGEST_WEEKLY_MINUTE_MSK` | `15` | Cron fire minute. **15-min stagger past daily 09:00 (H8) to avoid concurrent LLM gateway pressure on Mondays.** |
| `DIGEST_WEEKLY_USD_CEILING` | `5.00` | Weekly digest daily cost ceiling. **C5: independent of `LLM_DAILY_USD_CEILING` and `DIGEST_DAILY_USD_CEILING`** — gates only weekly synthesis via `WHERE d.type='weekly'` ledger filter. |
| `DIGEST_WEEKLY_MONTHLY_USD_CEILING` | `20.00` | Weekly digest monthly ceiling. Same independence semantics as the daily ceiling. |
| `DIGEST_WEEKLY_TOKEN_BUDGET` | `24000` | Weekly context size cap. |
| `DIGEST_WEEKLY_MIN_CARDS_THRESHOLD` | `8` | Weekly cards-first threshold. **L5: bumped from 5 to 8** — weekly window is 7× larger but cards are higher-quality (admin-approved over the full week); 8 is the empirical middle between daily-3 and linear-scaled 21. |
| `DIGEST_WEEKLY_RAW_MESSAGE_TOP_N` | `60` | Weekly raw fallback cap. |
| `DIGEST_REVIEW_DEADLINE_HOURS` | `168` | 7d auto-reject deadline. |
| `DIGEST_REVIEW_48H_NOTIFY_HOURS` | `48` | First admin DM reminder. |

`DIGEST_WEEKLY_DAY` was considered and **rejected per M3** — apscheduler `day_of_week="mon"` is hardcoded in the scheduler registration AND `isoweekday() - 1` in the window-anchor math. Operator preference for a different day is a 2-line code edit, not a runtime config concern.

Inherited from Phase 7 (no change): `DIGEST_SOURCE_CHAT_ID`, `DIGEST_DESTINATION_CHAT_ID`, daily ceilings, daily window vars.

### Feature flags

- `memory.digests.weekly.enabled` — gate the weekly cron + scheduled `transition_to_awaiting_review`. Default OFF. Admin `/digest_now weekly` bypasses the flag (Q12 inheritance from Phase 7).

### Cost-bucket independence (C5 reformulation)

Three independent cost buckets ALL fire for every weekly LLM call (any one tripping aborts the run):

1. **Phase 5 shared LLM bucket** — `LLM_DAILY_USD_CEILING` + `LLM_MONTHLY_USD_CEILING`, gateway-level guard inside `synthesize_*` methods. Fires for ALL LLM calls (digest + Q&A + cards).
2. **Phase 7 daily-digest bucket** — `DIGEST_DAILY_USD_CEILING` + `DIGEST_DAILY_MONTHLY_USD_CEILING`, helper SQL filtered by `WHERE d.type='daily'`. Fires only for daily digest synthesis.
3. **Phase 8 weekly-digest bucket** — `DIGEST_WEEKLY_USD_CEILING` + `DIGEST_WEEKLY_MONTHLY_USD_CEILING`, helper SQL filtered by `WHERE d.type='weekly'`. Fires only for weekly digest synthesis.

There is NO required ordering among the three thresholds. A weekly run can succeed against bucket 3 ($5/month) while bucket 1 ($10/month shared) also has headroom; if bucket 1 trips first, weekly is aborted by the gateway-level guard regardless of bucket 3 state. Operator tuning advice: set bucket 1 to be the highest (covers Q&A + cards + both digests); set buckets 2 and 3 to per-feature budgets; review monthly ledger sums to adjust.

### Runbook references

- `docs/memory-system/PHASE8_ROLLOUT.md` (T8-08 deliverable) — full operator checklist + downgrade procedure (M5 — operator must clear Phase-8 review rows from `digests` AND audit rows from `digest_runs` before alembic downgrade can succeed).
- `CLAUDE.md` "Memory System Cycle" section — Phase 8 closure summary + outstanding issues.
- `docs/memory-system/PHASE7_ROLLOUT.md` — daily digest playbook is a prerequisite; weekly rollout assumes daily is GREEN.

---

## 10. Test Coverage Matrix

Cross-reference of T8 tickets → Phase 11 binding case additions → existing 30/30 baseline regression checks. **L1+L2 + C3 reconciliation:** Round 1 review correctly flagged math drift in the earlier "34/34 → 41/41" framing. Direct enumeration of `tests/evals/test_*.py` parametrize ids on main `aeee781` confirms baseline = 30 (10 leakage + 8 citations + 6 refusal + 6 digest-leakage; see §0 status table). After C3 splits I6b into three sub-cases AND H7 expands R5 into four sub-cases, Phase 8 adds **12** new cases: L8a, L8b, C7, I6a, I6b.1, I6b.2, I6b.3, I6c, R5.a, R5.b, R5.c, R5.d → **new total 42/42**.

### New Phase 8 binding cases (12 total)

| Case | Window / scenario | Assertion shape | File |
|---|---|---|---|
| **L8a** | Forget `mvid` cited in a weekly digest body (any of 8 widened statuses). After cascade, body MUST NOT contain the forgotten content; status='redacted'. | Body redaction + status='redacted' + admin notify dispatched. | `tests/evals/test_digest_leakage.py` (extend) |
| **L8b** | Forget a `card_source` cited (via parent card) in a weekly digest body. Dual-kind path: cascade must walk `kind='card_source'` citations correctly. | Same shape as L8a but `kind='card_source'`. | `tests/evals/test_digest_leakage.py` (extend) |
| **C7** | Every bullet in a weekly digest has ≥1 valid citation token. Section headers (`## Раздел: …`) are correctly EXCLUDED from the bullet scanner. **M1 allowlist enforcement:** every `## Раздел: <name>` line has `<name>` in `SECTION_NAME_ALLOWLIST = {"Объявления", "Обсуждения", "Знания и ресурсы", "Встречи и события", "Прочее"}` (the 5 names ratified in §5.F). | Bullet-line regex finds ≥1 `[[cs:...]]` or `[[mv:...]]` token resolving to an input id; section-header regex match has title in allowlist; hallucinated ids dropped + logged. | `tests/evals/test_citations.py` (extend) |
| **I6a** | Forget `mvid` cited by a weekly digest in `awaiting_review` (also tested for `approved_for_publish`). Cascade scan widening (§5.D) MUST find the row, redactor allowlist widening (§5.K) MUST process it, admin notify with `error_text='forget_redacted_during_review'` MUST dispatch. | digest.status='redacted' AND admin_notify called with the specific error_text. | `tests/evals/test_digest_forget_cascade.py` (new) |
| **I6b.1** | Forget BEFORE `/digest_approve` invocation. After §5.K fix, cascade redacts the row first → admin sees `[REDACTED]` body OR `/digest_approve` raises `DigestReviewInvalidState(current_status='redacted')`. | digest.status='redacted' AND admin reply contains "забыто" / "forgotten" guidance. | `tests/evals/test_digest_forget_cascade.py` (new) |
| **I6b.2** | Forget DURING `approve_digest` (between step-3 revalidation check and step-4 commit). Racy. Either (a) approve commit wins (status='approved_for_publish') then publisher's own revalidation catches stale citation → terminal `failed` with `error_text='citations_stale_at_publish'`; OR (b) cascade wins (status='redacted'), approve_digest's step-4 guarded UPDATE returns rowcount=0 → admin reply `DigestReviewInvalidState`. | Final status ∈ {failed (citations_stale_at_publish), redacted (rowcount=0 path)}. NO row ever reaches 'posted'. | `tests/evals/test_digest_forget_cascade.py` (new) |
| **I6b.3** | Forget AFTER approve commit, BEFORE publisher dispatch. Cascade widening (§5.D) finds `approved_for_publish` row, calls redactor (§5.K) → status='redacted'. Publisher's `digest.status not in (draft, approved_for_publish)` guard (§5.L) fires its guarded UPDATE → rowcount=0 → terminal `failed` with `error_text='publisher_status_mismatch'`. | digest.status='redacted' (from cascade) OR 'failed' (publisher race winner); NO posted_message_id; audit row inserted. | `tests/evals/test_digest_forget_cascade.py` (new) |
| **I6c** | Concurrent forget event affecting both a daily AND a weekly digest citing the same mvid. Cascade walks `affected_digest_ids` deterministically (`ORDER BY d.id`), redacts each row in its own per-row redactor invocation. Both rows MUST end up `status='redacted'` independently. | Both rows redact in the same cascade event; final statuses both 'redacted'; per-row admin notifies fire. | `tests/evals/test_digest_forget_cascade.py` (new) |
| **R5.a** | Non-admin invokes `/digest_approve <id>` → silent no-op (mirrors Phase 6 `_is_admin` gate pattern). NO reply, NO state mutation. | bot.reply_count == 0 AND digest.status unchanged. | `tests/evals/test_refusal.py` (extend) |
| **R5.b** | Non-admin invokes `/digest_reject <id>` → silent no-op. Same assertion shape as R5.a. | bot.reply_count == 0 AND digest.status unchanged. | `tests/evals/test_refusal.py` (extend) |
| **R5.c** | Admin invokes `/digest_approve <id>` on already-`posted` row → reply contains `DigestReviewInvalidState(current_status='posted')` info + link to existing posted message. NO publisher re-dispatch. | digest_review.approve_digest raises DigestReviewInvalidState('posted'); reply text matches; no new digest_runs insert. | `tests/evals/test_refusal.py` (extend) |
| **R5.d** | Admin invokes `/digest_approve <id>` on `rejected_by_admin` row → reply contains `DigestReviewInvalidState(current_status='rejected_by_admin')` info + suggestion to use `/digest_now weekly --regenerate`. NO publisher re-dispatch. | Same shape as R5.c. | `tests/evals/test_refusal.py` (extend) |

### T8 → binding case mapping

| Ticket | Phase 11 binding case | What it asserts | Files |
|---|---|---|---|
| T8-01 | (none — schema only) | Schema validity: CHECK accepts new statuses on both `digests` and `digest_runs`, partial idx exists, downgrade pre-flight fails if Phase-8 rows exist. | `tests/db/test_digests_review_schema.py` |
| T8-02 | C7 (partial — section header skip) | Section headers don't break the bullet citation invariant; M1 allowlist enforcement. | `tests/evals/test_citations.py::C7` |
| T8-03 | (regression on L1-L5, L7a/b) | Forget exclusion still fires for weekly window. | Existing `tests/evals/test_leakage.py` re-run against weekly context fixtures. |
| T8-04 | L8a, L8b, I6a, I6b.1, I6b.3, R5.c, R5.d | Cascade scan widening (§5.D), redactor allowlist widening (§5.K), publisher trigger-state widening (§5.L), state-machine invalid transitions. | `tests/evals/test_digest_leakage.py`, `tests/evals/test_digest_forget_cascade.py`, `tests/evals/test_refusal.py` |
| T8-05 | (regression — schedule timing) | Cron correctness (H8 stagger), reaper idempotency, status-aware match block (H4). | `tests/services/test_scheduler_weekly.py` (unit). Add an `apscheduler` integration test (L3) firing `day_of_week="mon"` with a stubbed time to assert the job actually runs at Mon 09:15 MSK. |
| T8-06 | R5.a, R5.b | Non-admin denial; state-machine refusals from handler layer. | `tests/evals/test_refusal.py::R5.a/b` |
| T8-07 | All 12 new cases | Bound suite extension. | All `tests/evals/*` files. |
| T8-08 | (docs — no test impact) | — | — |

### Regression on existing 30/30 baseline (must remain green throughout Wave 3+)

| Existing case set (count) | Phase 8 risk to this invariant | Where Phase 8 could regress it |
|---|---|---|
| L1, L2, L3a, L3b, L3c, L4, L5 (7) | None direct; weekly context reuses Phase 7 governance filter. | T8-03 if `_forget_excludes_predicate` is forked instead of reused (#291 carryover). |
| L6a/b/c (3) | None — Phase 8 doesn't touch cards. | n/a |
| L7a/b (2) | Cascade scan widening must NOT regress daily. | T8-04 — the widened 8-tuple is a strict superset of the Phase 7 5-tuple; daily flow unchanged. |
| C1-C4 (4) | None direct. | T8-02 section parsing must not affect Phase 7 daily prompt processing. |
| C5a-d (4) | None. | n/a |
| C6 (1) | None — daily prompt unchanged. | n/a |
| R1, R2, R3a, R3b, R3c, R4 (6) | None. | n/a |
| I5a/b/c (3) | None direct; daily redact path unchanged. | T8-04 widening adds branches but doesn't remove any. |

**Baseline total = 10 + 3 + 2 + 4 + 4 + 1 + 6 + 3 wait that's 33** — re-count by file: leakage 10 + citations 8 + refusal 6 + digest_leakage 6 = **30**. The breakdown above double-counts because L7a/b sits in `test_digest_leakage.py` and the 6 there = L7a, L7b, C6, I5a, I5b, I5c (cross-suite case ids). The "30/30 → 42/42" math: 30 baseline + 12 new = 42. Confirmed.

**Definition of pass:** `pytest tests/evals/ -v` shows 42/42 passing post-T8-07. No skipped cases. No xfail.

---

## 11. Phase 8 Backlog (out of v1 scope)

Tracked for Phase 8.5 / 9 / future:

- **Pre-approval admin edit** with citation-aware validator. Rejected in Q5 for v1; if v1 produces high false-positive approves (i.e. admins frequently want to tweak), revisit in 8.5.
- **Two-admin quorum** for high-stakes weekly publishes. Q4 v1 = single admin. 8.5 if false-positive publishes happen.
- **Inline citation rendering in public post.** Phase 7 Q11 inherited; same trade-off (community readability vs audit transparency).
- **Multi-chat weekly digests** (one weekly per chat). Single-chat MVP for v1.
- **Per-user opt-out** for being mentioned in a weekly digest. Phase 9+ as a governance extension.
- **Topic clustering / LLM-inferred section names.** Phase 8 v1 enforces a 5-name allowlist; clustering is Phase 8.5.
- **Reaction-count / reply-count ranking** of messages within the weekly window. Same blocker as Phase 7 — `chat_messages` has no reaction/reply columns; needs a separate migration.
- **Inline-keyboard admin buttons** for `/digest_approve` / `/digest_reject` instead of text commands. UX polish for v1.1.
- **Subscription-based admin DM routing** (currently DMs only first admin in `ADMIN_IDS`). Phase 8.5 if admin team grows.

### Phase 7.5 carryovers (status as of plan ratification)

- **Issue #291** (shared `_forget_excludes_predicate` refactor): OPEN. The predicate currently exists as inline SQL in both `digest_context.py` and `forget_cascade.py`. Phase 8 plan reads cleanly against either state (refactored or inline). **M7 advisory upgrade — strongly recommended (not hard-blocking):** land #291 BEFORE T8-03 to avoid a THIRD inline copy of the predicate landing in the weekly context query. If #291 has NOT landed when T8-03 starts, T8-03 implementation MUST add an explicit code comment `# TODO(#291): extract this predicate; current copy is the third — see digest_context.py:NNN and forget_cascade.py:255+ for the other two` so the drift is visible. T8-03 acceptance reviewer MUST verify the comment exists if #291 still open. Documented drift cost: any future predicate change requires three coordinated edits.
- **Issue #295** (T7-02 post-merge MED items): OPEN. Independent of Phase 8 surface — no blocking concern. Track separately.

### M6 — GIN index dead weight on `ix_digests_citations_gin`

Round 1 (Claude M4) flagged: the Phase 7 `_cascade_digests` JSONB scan at `forget_cascade.py:840+` uses `EXISTS (SELECT 1 FROM jsonb_array_elements(d.citations) AS elem WHERE ...)`, which is a per-row LATERAL unnest — NOT a containment query. PostgreSQL's GIN `jsonb_path_ops` index only helps `@>` / `@?` / `@@` operators against the indexed JSONB column. The current scan plan is a SeqScan with a CTE; the `ix_digests_citations_gin` index is unused.

This is NOT a Phase 8 blocker — the scan works correctly, just sub-optimally for the small `digests` row count (~7/week + ~365/year = <500/year). But once weekly + future phases add more digest types, the linear scan will be slower than needed.

**Tracking as Phase 7.5 follow-up issue (file in T8-08 closure):** either (a) rewrite the `_cascade_digests` JSONB scan as a containment query — build a probe `[{"kind":"message_version","id":N},...]` from `affected_mvids` and `affected_cs_ids`, and use `WHERE citations @> :probe::jsonb`; OR (b) drop the unused index. Recommendation: (a) — keeps the index, fixes the scan plan, and exercises the GIN selectivity properly. Out of Phase 8 scope.

### Stop signal — Phase 8 scope creep

Refer back to §3 if anyone in Wave 1-3 proposes adding reflection, observations, memory_events, or two-admin quorum mid-phase. These belong to Phase 9+ and Phase 8.5; locking them out of v1 is an explicit ratification decision (Q1, Q4). If a Wave-3 reviewer flags a missing feature that maps to one of the §11 backlog items, the answer is "tracked, Phase 8.5" — not "let's add it now".

---

## 12. Open Questions for Phase 8.5

Should v1 prove insufficient, the following are Phase 8.5 candidates:

1. **Two-admin quorum** if v1 shows admin approval errors → quorum reduces false positives.
2. **Hard-enforce section allowlist** at parse time if LLM drifts.
3. **Admin pre-approval edit** if v1 reject-and-regenerate cycle is too coarse.
4. **Inline-keyboard buttons** if text-command UX feels clunky.
5. **Quote attribution policy** (currently sections summarize without quoting; if quotes appear, decide redaction rules for forgotten authors).

None of these are open at plan time — they are deferred-by-design.

---

## 13. Acceptance Criteria (Charter ACs)

Phase 8 closure (T8-08 completion + FHR APPROVE) requires all 8 ACs green:

- **AC1.** Weekly cron `digest_weekly` fires on schedule (default **Mon 09:15 MSK — H8 15-min stagger past daily 09:00**) when flag `memory.digests.weekly.enabled=ON`; strict no-op when flag OFF (verified by logs showing "flag disabled, skipping"). Verified by T8-05 acceptance + manual cron fire during T8-08 rollout dry-run + L3 integration test.
- **AC2.** Weekly digest reaches `awaiting_review` terminal in the auto-pipeline; the scheduler/job NEVER auto-publishes. Verified by T8-04 acceptance (transition_to_awaiting_review) + T8-05 (H4 status-aware branch never calls publish_digest from cron) + binding tests R5.a/.b/.c/.d.
- **AC3.** Single-admin approval transitions `awaiting_review → approved_for_publish → posting → posted`. Defense-in-depth citation revalidation in `approve_digest` step 3 re-checks every cited source against current governance state; stale citations transition to `failed` with `error_text='citations_stale_at_approval'`. Publisher's widened trigger guard (§5.L) accepts `approved_for_publish`. Verified by T8-04 (§5.E + §5.L) + T8-06 + binding tests I6b.1/.2/.3.
- **AC4.** Admin rejection transitions to `rejected_by_admin` terminal; rejected digests can be re-run via `/digest_now weekly --regenerate` (H3 single-transaction). Verified by T8-04 + T8-06.
- **AC5.** Forget cascade redacts weekly rows in any of the 8 in-scope statuses `{draft, awaiting_review, approved_for_publish, posting, posted, redacted, redacted_edit_failed, rejected_by_admin}`. Cascade scan widening (§5.D) AND redactor allowlist widening (§5.K) BOTH applied. Dual-citation-kind handling (`message_version` + `card_source`) identical to Phase 7. Verified by T8-04 + binding tests L8a, L8b, I6a, I6c.
- **AC6.** Cost ceiling enforced via separate INDEPENDENT bucket: `DIGEST_WEEKLY_USD_CEILING` (default $5.00) and `DIGEST_WEEKLY_MONTHLY_USD_CEILING` (default $20.00). Weekly LLM invocations are filtered in the refactored `_cost_ceiling_breached(session, digest_config, type='weekly')` SQL by `WHERE d.type=:type` (H6). The shared Phase 5 `LLM_DAILY_USD_CEILING` ALSO fires for ALL LLM calls (gateway-level guard, separate accounting). Per C5 reformulation in §6 Q7, there is NO must-be-less-than relationship between the three buckets — each is independent. Verified by T8-02 acceptance.
- **AC7.** Stale-review reaper runs every 30 min: 48h pass DMs first admin with a single notification per row (marker `[48h_notified]` in `review_notes` prevents repeats; **M4 guarded UPDATE WHERE status='awaiting_review' RETURNING id** ensures the DM only fires if the row hasn't advanced); 7d pass auto-rejects with `status='rejected_by_reaper'` terminal + audit insert + admin DM. Verified by T8-05 + T8-07 (R5 indirectly covers via state-machine assertions).
- **AC8.** Phase 11 binding suite **30→42 green** (baseline math correction per L1+L2): existing 30 cases preserve regression-free (10 leakage + 8 citations + 6 refusal + 6 digest-leakage; see §0 + §10 for the verified enumeration), 12 new weekly cases pass (L8a, L8b, C7, I6a, I6b.1, I6b.2, I6b.3, I6c, R5.a, R5.b, R5.c, R5.d). Verified by T8-07.

**Final Holistic Review (FHR) trigger:** required per Rule 9 of `~/.claude/rules/superflow-enforcement.md` — Phase 8 has 8 sprints (≥4) and binds new privacy invariants. Two reviewers (Claude deep-product + Codex deep-technical) on the full Phase 8 surface. Fix CRITICAL/HIGH before closure report.

---

## 14. PR Workflow

Sprint-PR-queue mode. One PR per ticket. **L6 — partial parallel waves where deps allow:**

1. **Wave 1:** T8-S0 → main (docs-only authorization). Solo, no review parallel.
2. **Wave 2:** T8-01 → main (schema). Reviewed by Codex + Claude product. Blocks Wave 3.
3. **Wave 3 (parallel):** T8-02 + T8-03 ship on two separate worktrees + branches; both review-and-merge independently. Either order acceptable into main. Both required before Wave 4.
4. **Wave 4 (parallel):** T8-04 (XL — covers C1 + C2 + H2 + cascade scan widening + redactor allowlist widening + publisher trigger widening) ∥ T8-05 (scheduler + reapers, depends on T8-04 only for import — can use a stub import on a feature branch and rebase once T8-04 lands). **T8-04 may split into 4A (digest_review.py + state machine tests) + 4B (forget_cascade scan widening + redactor allowlist widening + publisher trigger widening + cross-component tests) if diff > 400 lines.** PR description for 4A or 4B MUST cross-reference the other half.
5. **Wave 5 (parallel):** T8-06 (admin handlers + renderer) ∥ T8-07 (binding tests). T8-07 writes red tests against the T8-06 in-progress branch; both PRs merge in the same wave once both green AND the full 42/42 suite passes locally.
6. **Wave 6:** T8-08 → main (closure docs). Phase 11 42/42 green prerequisite (gate enforced by T8-07 acceptance).
7. **Final Holistic Review** after T8-08 merged. Two reviewers (Claude deep-product + Codex deep-technical) on the full Phase 8 surface. Fix CRITICAL/HIGH before closure report.

Each PR:
- One ticket (or one half of T8-04 if split), diff ≤400 lines (split if larger).
- Tests added/extended with the change.
- `.par-evidence.json` written before push.
- Codex review via `Agent(subagent_type="codex:codex-rescue")` with technical-lens prompt.
- Claude standard-product-reviewer for product/spec lens.
- Both verdicts PASS / ACCEPTED → PR created.
- CI green → user-initiated merge (Phase 3).

---

## 15. Glossary (Phase 8-specific)

- **Weekly digest:** a derived Markdown editorial recap for a completed ISO week, section-organized. Reviewed by admin before publish.
- **ISO week:** Mon 00:00 MSK..next Mon 00:00 MSK (exclusive-end), stored as UTC. Cron fires **Mon 09:15 MSK** (H8 15-min stagger past daily 09:00; canonical schedule definition in §5.G).
- **Review queue:** the set of `digests` rows with `type='weekly' AND status='awaiting_review'`. Listed via `/digest_review`.
- **`awaiting_review`:** terminal state of the auto-pipeline; admin action required to advance.
- **`approved_for_publish`:** transient state set by `/digest_approve` immediately before publisher dispatch.
- **`rejected_by_admin`:** terminal state set by `/digest_reject`; carries `review_notes` (admin's reason).
- **`rejected_by_reaper`:** terminal state set by `digest_stale_review_reaper_job` after 7d of inactivity.
- **Stale-review reaper:** 30-min interval job that DMs admin at 48h and auto-rejects at 7d.
- **Stale-approved reaper:** 5-min threshold in the existing `digest_stale_posting_reaper_job`; catches crashes between `approve_digest` commit and publisher dispatch.
- **Section header:** Markdown `## Раздел: …` line; recognized by the renderer to bold via `<b>…</b>`. Allowlist of 5 Russian titles defined in the weekly prompt.
- **Separate cost bucket (weekly):** `DIGEST_WEEKLY_USD_CEILING` / `DIGEST_WEEKLY_MONTHLY_USD_CEILING`, queried with `d.type='weekly'` filter on `llm_usage_ledger` join. Independent of daily bucket.
- **`--regenerate` flag:** `/digest_now weekly --regenerate` regenerates a fresh weekly draft when the existing row for the window is in `rejected_by_admin` or `rejected_by_reaper`. Refused otherwise.

---

## 16. Sprint 0 Deliverable (T8-S0)

This document is the deliverable. To complete Sprint 0, the PR must:

1. Add this `PHASE8_PLAN.md` to `docs/memory-system/`.
2. Update `AUTHORIZED_SCOPE.md` via **semantic edits** (not line numbers — line numbers shift between plan-write and PR creation):
   - **Remove** the bullet under the "NOT in Phase 7 scope (defer to Phase 8+)" list (currently at `AUTHORIZED_SCOPE.md:238-241`) whose text begins with `Weekly digest scheduler / handler / publisher (Phase 8).`. Search by exact opening phrase; fail if not unique.
   - **Insert** new `## Authorized: Phase 8 — Weekly editorial digest (2026-05-15)` block immediately BEFORE the `## NOT authorized` heading. Block content mirrors the Phase 7 closure block structure: TL;DR, owner (Orchestrator A), scope reference to this `PHASE8_PLAN.md`, NOT-in-scope list mirroring §3 of this plan, ratification date, FHR requirement note.
3. Update `ROADMAP.md` row 8 status cell from `NO` to `AUTHORIZED 2026-05-15 — plan ratified` (preserve the AC text in the right column unchanged).
4. No code changes. No migrations. No new env vars in `bot/config.py`.
5. PR title: `docs(memory): ratify Phase 8 plan + authorize weekly digest implementation`.
6. PR description: short summary + link to this file + reference to charter + summary of two-reviewer findings (this plan was reviewed by Codex + Claude standard-spec-reviewer 2026-05-15 before ratification).

This unblocks Wave 1.

<!-- updated-by-superflow:2026-05-15 -->
