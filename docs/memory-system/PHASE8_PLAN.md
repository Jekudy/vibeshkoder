# Phase 8 — Weekly Editorial Digest: Ratified Plan

**Status:** RATIFIED 2026-05-15. Implementation authorized for Sprint 0 (`AUTHORIZED_SCOPE.md` update + this plan) + 8 sprints across 3 waves.
**Predecessors:** Phase 4 (FTS + evidence, CLOSED 2026-04-30), Phase 5 (`llm_gateway` + ledger, CLOSED 2026-05-11), Phase 6 (cards + admin review, CLOSED 2026-05-12), Phase 7 (daily digest, CLOSED 2026-05-15), Phase 11 (privacy binding suite, ACTIVE 28/28 → 34/34 after Phase 7).
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
| `redact_digest_for_forget` + `_cascade_digests` | Exists | `bot/services/digest_redactor.py`; `bot/services/forget_cascade.py:804-880`. The `WHERE d.status IN (...)` filter at `forget_cascade.py:840` MUST be widened in T8-04 to include `awaiting_review`, `approved_for_publish`, and `rejected_by_admin` so the cascade does not skip weekly drafts in review. |
| `digest_daily_job` + `digest_stale_posting_reaper_job` | Exists | `bot/services/scheduler.py:282-425`. T8-05 adds `digest_weekly_job` (cron Mon 09:00 MSK) + `digest_stale_review_reaper_job` (48h DM + 7d auto-reject). |
| `bot/handlers/digest.py` `_is_admin` + Phase 7 commands | Exists | `bot/handlers/digest.py`. T8-06 adds `/digest_approve`, `/digest_reject`, `/digest_review` and widens `/digest_now` to accept `weekly`. |
| `feature_flags` table | Exists | Phase 8 flag `memory.digests.weekly.enabled` default OFF, same shape as Phase 7 daily flag. |
| `tests/evals/` Phase 11 suite | Exists | Phase 7 closure left 34/34 green (L1-L5 + L6a/b/c + L7a/b + C1-C4 + C5a-d + C6 + R1-R4 + I1-I4 + I5a/b/c). Phase 8 adds L8a/b + C7 + I6a/b/c + R5 → 41/41 new total. |
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
                    │   cron(day_of_week=DIGEST_WEEKLY_DAY,      │
                    │        hour=DIGEST_WEEKLY_HOUR_MSK,        │
                    │        timezone=ZoneInfo("Europe/Moscow")) │
                    │   default Mon 09:00 MSK = Mon 06:00 UTC    │
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

**Migration plan:** PostgreSQL CHECK constraints are immutable in place; the canonical alembic pattern is DROP CHECK + ADD CHECK. The Phase 7 migration `037_add_digests.py:101-105` named the constraint `ck_digests_status`, so T8-01 references that name by parameter, not by inspection.

**Upgrade SQL (rendered through SQLAlchemy / `op.execute` for the CHECK swap; `op.add_column` for new cols; `op.create_index` for the partial idx):**

```sql
-- 1. Extend status CHECK constraint to include the four new review states.
ALTER TABLE digests DROP CONSTRAINT ck_digests_status;
ALTER TABLE digests ADD CONSTRAINT ck_digests_status CHECK (
    status IN (
        'running','draft','posting','posted','failed','skipped',
        'cost_exceeded','skipped_no_destination','redacted',
        'redacted_edit_failed',
        -- Phase 8 additions:
        'awaiting_review','approved_for_publish',
        'rejected_by_admin','rejected_by_reaper'
    )
);

-- 2. New columns supporting the review workflow.
ALTER TABLE digests ADD COLUMN published_by_admin_id BIGINT NULL;
ALTER TABLE digests ADD COLUMN approved_at TIMESTAMP WITH TIME ZONE NULL;
ALTER TABLE digests ADD COLUMN review_notes TEXT NULL;
ALTER TABLE digests ADD COLUMN awaiting_review_at TIMESTAMP WITH TIME ZONE NULL;

-- 3. Body NOT NULL invariant must extend to review states (mirror §5.A in
-- Phase 7). Drop + re-add with the wider state list.
ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses;
ALTER TABLE digests ADD CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses CHECK (
    status NOT IN (
        'draft','posting','posted','redacted','redacted_edit_failed',
        'awaiting_review','approved_for_publish','rejected_by_admin'
    )
    OR body_markdown IS NOT NULL
);

-- 4. Partial index for the stale-review reaper scan.
CREATE INDEX ix_digests_status_awaiting_review
    ON digests (awaiting_review_at)
    WHERE status='awaiting_review';

-- 5. Audit guard: approved_for_publish requires published_by_admin_id + approved_at.
ALTER TABLE digests ADD CONSTRAINT ck_digests_approved_audit CHECK (
    status NOT IN ('approved_for_publish','posting','posted') OR (
        type <> 'weekly' OR (
            published_by_admin_id IS NOT NULL AND approved_at IS NOT NULL
        )
    )
);
-- Daily digests are exempt: weekly path mandates admin attribution; daily
-- path is auto-publish and leaves the audit cols NULL forever. The
-- `type <> 'weekly' OR (...)` predicate keeps the constraint type-aware.
```

**Downgrade SQL (T8-01 must provide a clean reverse):**

```sql
DROP INDEX ix_digests_status_awaiting_review;
ALTER TABLE digests DROP CONSTRAINT ck_digests_approved_audit;

-- Drop the wider body-NOT-NULL constraint and restore the Phase 7 version.
ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses;
ALTER TABLE digests ADD CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses CHECK (
    status NOT IN ('draft','posting','posted','redacted','redacted_edit_failed')
    OR body_markdown IS NOT NULL
);

ALTER TABLE digests DROP COLUMN awaiting_review_at;
ALTER TABLE digests DROP COLUMN review_notes;
ALTER TABLE digests DROP COLUMN approved_at;
ALTER TABLE digests DROP COLUMN published_by_admin_id;

ALTER TABLE digests DROP CONSTRAINT ck_digests_status;
ALTER TABLE digests ADD CONSTRAINT ck_digests_status CHECK (
    status IN (
        'running','draft','posting','posted','failed','skipped',
        'cost_exceeded','skipped_no_destination','redacted',
        'redacted_edit_failed'
    )
);
```

**Downgrade safety rule:** the downgrade FAILS HARD if any row currently has a Phase-8 review status. The migration code asserts:

```sql
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM digests WHERE status IN (
            'awaiting_review','approved_for_publish',
            'rejected_by_admin','rejected_by_reaper'
        )
    ) THEN
        RAISE EXCEPTION 'cannot downgrade: digests rows in Phase 8 review states exist';
    END IF;
END$$;
```

This protects against silent data corruption when an operator downgrades after a weekly digest has already moved into review. Phase 7's `posted`/`redacted` rows are preserved (their statuses remain valid under the narrower CHECK).

**`digest_runs.status` CHECK** (`037_add_digests.py:173-177`) MUST also widen to accept `awaiting_review`, `approved_for_publish`, `rejected_by_admin`, `rejected_by_reaper`. Same DROP+ADD pattern.

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
    weekly_day_of_week: int = 1  # ISO Monday=1, Sunday=7 (apscheduler uses 0-6 sun-sat; converted in scheduler)
    weekly_hour_msk: int = 9
    weekly_token_budget_input: int = 24000
    weekly_min_cards_threshold: int = 5  # higher bar — week-scale recap
    weekly_raw_message_top_n: int = 60
    review_deadline_hours: int = 168  # 7d
    review_48h_notify_hours: int = 48
```

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
        weekly_day_of_week=int(os.environ.get("DIGEST_WEEKLY_DAY", "1")),
        weekly_hour_msk=int(os.environ.get("DIGEST_WEEKLY_HOUR_MSK", "9")),
        weekly_token_budget_input=int(
            os.environ.get("DIGEST_WEEKLY_TOKEN_BUDGET", "24000")
        ),
        weekly_min_cards_threshold=int(
            os.environ.get("DIGEST_WEEKLY_MIN_CARDS_THRESHOLD", "5")
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
2. Cost ceiling: a new private helper `_cost_ceiling_breached_weekly(session, digest_config)` runs alongside Phase 7's `_cost_ceiling_breached`. The SQL is:

```sql
SELECT COALESCE(SUM(l.cost_usd), 0)
FROM llm_usage_ledger l
JOIN digests d ON d.llm_usage_ledger_id = l.id
WHERE d.type = 'weekly'
  AND d.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                       AT TIME ZONE 'UTC'
```

A second query handles the monthly bucket (`date_trunc('month', ...)`). Both must be below the corresponding `DIGEST_WEEKLY_USD_CEILING` / `DIGEST_WEEKLY_MONTHLY_USD_CEILING`. If exceeded → INSERT `digests` row with `status='cost_exceeded'`, INSERT `digest_runs` `status='cost_exceeded'`, return. No LLM call. (Same shape as Phase 7 `_cost_ceiling_breached` but separate bucket — exercised by acceptance test T8-02-AC4.)
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

**Redactor (`bot/services/digest_redactor.py`) requires no change.** Its existing logic walks `digest.citations` JSONB, masks affected bullets, persists redaction, and attempts `bot.edit_message_text` if `posted_message_id IS NOT NULL`. For `awaiting_review` / `approved_for_publish` / `rejected_by_admin` rows, `posted_message_id IS NULL` so the Telegram side-effect is skipped — only DB redaction happens. The status transition is `awaiting_review → redacted` (or `approved_for_publish → redacted` / `rejected_by_admin → redacted`). The body-NOT-NULL CHECK at §5.A continues to hold because `redacted` is in the wider invariant list.

**One additional redactor branch (T8-04 sub-step):** when the redactor sees `status='awaiting_review'`, it must ALSO admin-notify (reuses Phase 7 `notify_admins_digest_failure` with `error_text='forget_redacted_during_review'`) so the admin who was about to approve knows the draft is now redacted and there's nothing to approve. This is a UX nicety, not a privacy invariant — the privacy invariant is already covered by the scan widening.

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
    """Approval/reject attempted from a non-awaiting_review state."""


class DigestReviewNotFound(Exception):
    """Digest id does not exist."""


async def transition_to_awaiting_review(
    session: AsyncSession,
    *,
    digest_id: int,
) -> None:
    """Called by digest_weekly_job after run_digest returns status='draft'.

    SELECT FOR UPDATE; require status='draft' and type='weekly'.
    UPDATE status='awaiting_review', awaiting_review_at=now(),
           updated_at=now().
    INSERT digest_runs (digest_id=:id, status='awaiting_review', started_at=now()).
    Commit. Caller (digest_weekly_job) then DMs admins.
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

    1. Begin transaction; statement_timeout 30s (mirrors §5.F publisher).
    2. SELECT * FROM digests WHERE id=:id FOR UPDATE.
       - Not found → raise DigestReviewNotFound.
       - status != 'awaiting_review' → raise DigestReviewInvalidState(status).
       - type != 'weekly' → raise DigestReviewInvalidState('not_weekly').
    3. Re-run defense-in-depth context revalidation. Re-query every citation
       source id; if any source is now forgotten/redacted/missing → UPDATE
       status='failed', error_text='citations_stale_at_approval', commit,
       admin notify, raise DigestReviewInvalidState('citations_stale').
    4. UPDATE status='approved_for_publish', published_by_admin_id=:admin_id,
       approved_at=now(), updated_at=now(). Commit (releases the row lock).
    5. Call digest_publisher.publish_digest(session, bot=bot, digest=digest,
       digest_config=digest_config). The publisher's own transaction acquires
       FOR UPDATE again and trigger-state check accepts 'approved_for_publish'.
       On publisher success: posted_* fields populated, status='posted'.
       On publisher failure: same terminal handling as Phase 7 (status='failed'
       + error_text).
    6. Return ApproveResult with the publisher's outcome.
    """


async def reject_digest(
    session: AsyncSession,
    *,
    digest_id: int,
    admin_id: int,
    reason: str | None,
) -> None:
    """Single-admin reject → terminal rejected_by_admin.

    1. SELECT FOR UPDATE; require status='awaiting_review'.
    2. UPDATE status='rejected_by_admin',
       published_by_admin_id=:admin_id,
       review_notes=:reason (truncated to 1000 chars; NULL→'no reason given'),
       updated_at=now().
    3. INSERT digest_runs (status='rejected_by_admin', error_text=:reason).
    4. Commit.

    No publish. No Telegram side-effect. Admin can re-run with
    /digest_now weekly to produce a fresh draft for the same window
    (Phase 7 idempotency returns the existing rejected_by_admin row in
    that case — admin must first delete the row manually OR /digest_now
    must accept a --regenerate flag). Q5: Phase 8 v1 ships
    /digest_now <type> --regenerate to handle this; see §5.G.
    """
```

**Transaction boundaries:**
- `transition_to_awaiting_review` → single transaction, commits before admin DM.
- `approve_digest` → outer transaction commits at step 4 (releases lock so publisher can re-acquire); publisher uses its own transaction (single long-lived per Phase 7 §5.F). The approve-then-publish sequence is NOT atomic; if the orchestrator crashes between step 4 (commit) and step 5 (publisher dispatch), the row is stuck in `approved_for_publish` and the stale-posting-style scenario applies. T8-05 adds a `digest_stale_approved_reaper` to the existing reaper job that moves `approved_for_publish` older than 5 minutes back to `failed` with `error_text='stale_approved_reaper'`. This mirrors §5.K from Phase 7.
- `reject_digest` → single transaction.

**Error types:**
- `DigestReviewNotFound` → caller (handler) replies "Дайджест #id не найден".
- `DigestReviewInvalidState(status)` → caller replies "Дайджест уже в статусе `{status}`" with context-aware variants for `posted`, `rejected_by_admin`, `rejected_by_reaper`, `redacted`, `citations_stale`, `not_weekly`.
- All other exceptions → caller replies generic error + logs structured.

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

**Three new jobs added inside `start_scheduler(bot)`:**

```python
# T8-05: Phase 8 weekly digest. ISO Mon 09:00 MSK, gated by feature flag
# memory.digests.weekly.enabled (default OFF). The job body re-checks the
# flag and is a strict no-op when disabled.
scheduler.add_job(
    digest_weekly_job,
    "cron",
    day_of_week="mon",  # apscheduler accepts "mon" or 0 (Mon ISO=1, ap=0)
    hour=settings.DIGEST_WEEKLY_HOUR_MSK,  # default 9
    minute=0,
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
            # Find most recent Monday 00:00 MSK (this Monday if fired Mon 09:00).
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

                if digest.status == "draft":
                    # Phase 8 differs from Phase 7: NEVER auto-publish.
                    # Transition to awaiting_review and DM admins.
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
            # Step 1: 7d auto-reject pass.
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
                await _dm_admins_weekly_48h_reminder(digest_id=row.id)
                await session.execute(
                    _text(
                        "UPDATE digests SET "
                        "review_notes=COALESCE(review_notes,'') || '[48h_notified]', "
                        "updated_at=now() WHERE id=:id"
                    ),
                    {"id": row.id},
                )
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
- `--regenerate` flag (Phase 8 v1 feature, accommodates Q5 design choice that admin cannot edit): only valid when existing row status is `rejected_by_admin` or `rejected_by_reaper`. Deletes the existing row + INSERTs `digest_runs` `status='regenerated_by_admin'` audit, then re-runs `run_digest`. Idempotency lock prevents racing.

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

---

## 6. Ratified Decisions (full list)

All 9 ratification questions locked 2026-05-15.

| # | Question | Decision | Rationale | Alternatives considered (rejected because…) |
|---|---|---|---|---|
| Q1 | Phase 8 scope | **WEEKLY DIGEST ONLY.** Reflection/observations re-scoped to Phase 9+. `PHASE8_PLAN_DRAFT.md` superseded; flagged for archival in T8-08 closure. | Reflection runs and `memory_events`/`memory_candidates` are an entirely separate axis (LLM passive observation about a member). Mixing them with weekly digest doubles surface area + delays the high-value editorial recap. ROADMAP row 8 says "weekly digest" explicitly. | (a) Ship both in one phase — rejected: scope creep, 2× FHR risk, no shared code. (b) Ship reflection first, weekly second — rejected: digest builds on Phase 7 closure (one cohesive arc), reflection needs more upstream design (Phase 9 prerequisite). |
| Q2 | Section schema | **JSONB inline in `body_markdown` as Markdown `## Раздел: …` headers + bullets.** No `digest_sections` table. Renderer bolds the header as `<b>…</b>`. | KISS. No migration scope creep (one CHECK ALTER vs new table + FK + indexes + ORM). Sections are a presentation concern of the body text, not a separate data axis. Forget cascade is unchanged — sections are just text. | (a) Separate `digest_sections` table — rejected: doubles migration size, adds FK from `digest_sections.digest_id → digests.id` + cascade DELETE semantics + new ORM + ix_digest_sections_* indexes, all for a presentational concern. (b) JSONB column on `digests` with structured sections — rejected: makes citations-per-section impossible without a third query layer; current citations are already a flat JSONB list keyed by `position` which can index into the body. |
| Q3 | Window | **ISO week Mon 00:00 MSK..next Mon 00:00 MSK (exclusive-end).** Cron fires Mon 09:00 MSK so the most-recently-completed ISO week is processed. | Closed-week — edits/forgets settle within the week. Mon 09:00 = same hour as daily, easy to remember. ISO weeks are standard; isoweekday() returns 1 for Mon. | (a) Calendar week (varying first-day-of-week per locale) — rejected: surprise risk with mixed conventions. (b) Sun→Sat — rejected: less common in EU/RU community context. (c) Rolling 7d ending Mon 00:00 — equivalent to ISO if fired Mon, but ambiguous if fired any other day; cron locks the day. |
| Q4 | Approval flow | **Single-admin approval triggers publish.** No quorum, no two-admin gate in v1. | KISS for v1; the community is small enough that one admin's judgment is acceptable. Phase 8.5 backlog item: quorum if false-positive publishes occur. | (a) Two-admin quorum — rejected: blocks publish on admin availability gap (common in small admin team); v1 over-engineering. (b) Auto-publish after 48h with no admin action — rejected: violates ROADMAP "no auto-publish" gate. |
| Q5 | Admin edit pre-approval | **NO edit in v1.** Admin uses `/digest_reject + /digest_now weekly --regenerate` to produce a fresh draft. | Preserves citation invariant (admin-edited text could lose ids → break C7). Implementation cost of pre-approval edit is high (rich-text editor, citation-aware validator, audit trail). | (a) Free-text edit before approval — rejected: defeats citation invariant; admin could insert prose without ids. (b) Comment/annotation on bullets but no body edit — rejected: still surfaces v1.5 complexity (comments storage, UI); doesn't improve quality enough to justify. |
| Q6 | Stale-review reaper | **48h admin DM notify + 7d auto-reject** (`rejected_by_reaper` terminal, distinct from `rejected_by_admin`). Configurable via `DIGEST_REVIEW_DEADLINE_HOURS` (default 168) and `DIGEST_REVIEW_48H_NOTIFY_HOURS` (default 48). | Bounded queue size. 7d aligns with weekly cadence — by the time the next weekly fires, last week's review is cleared. Two-tier (notify→reject) reduces silent ignores. | (a) No reaper, drafts pile forever — rejected: queue bloat + admin guilt. (b) 24h notify + 7d reject — rejected: too aggressive; community admins sometimes need a day's slack. (c) Single 7d reject without notify — rejected: silent timeout, no escalation. |
| Q7 | Cost ceiling | **Separate `DIGEST_WEEKLY_USD_CEILING` env (default $5.00) + `DIGEST_WEEKLY_MONTHLY_USD_CEILING` ($20.00).** Separate monthly bucket in ledger queries (`d.type='weekly'` filter). | Weekly context is 7× daily (~24k tokens vs ~8k); single ceiling could starve daily or weekly. Separate buckets keep the two paths independent + auditable. | (a) Single shared ceiling — rejected: $1/day implies $30/month, leaving no room for weekly. (b) Per-call ceiling only — rejected: a runaway prompt loop could still burn through daily allowance; bucket sums are the canonical guard. |
| Q8 | Daily/weekly race | **No new logic.** Both daily and weekly cascade independently; the cascade walks `digests.citations` and redacts each row in turn. Order is deterministic via `ORDER BY d.id`. I6c binding test asserts both rows redact. | The `_cascade_digests` layer already iterates over `affected_digest_ids` and calls `redact_digest_for_forget` per row. Each row gets its own transaction-scoped lock. No cross-row coordination required. | (a) Transactional barrier across both rows — rejected: unnecessary serialization. (b) Skip one if the other is in `posting` — rejected: would leave one row stale + violate per-row contract. |
| Q9 | Migration head | **Migration 038 ONLY** ALTERs `ck_digests_status` + `ck_digests_body_markdown_not_null_for_visible_statuses` + `ck_digest_runs_status` + ADDs `published_by_admin_id`, `approved_at`, `review_notes`, `awaiting_review_at` + partial index `ix_digests_status_awaiting_review` + new `ck_digests_approved_audit`. No schema rework. Stamp `revision='038'`, `down_revision='037'`. | Aligns with Phase 7's "extend existing tables" pattern. CHECK swap is the canonical alembic move for enum widening in Postgres (no native ENUM type used — TEXT + CHECK is the project convention). | (a) New `digest_review` table with FK to `digests.id` — rejected: doubles surface area; review state is a property of the digest, not a separate entity. (b) New `digest_type='weekly_review'` enum value — rejected: confuses `type` (window cadence) with `status` (lifecycle); status is the right axis. |

**Decisions inherited from Phase 7 (re-affirmed for weekly path):**
- Decisions 2-16 from PHASE7_PLAN.md §6 carry over unchanged where applicable: HTML parse mode, no inline citations in public post, governance filter, citation contract (`[[cs:UUID]]` / `[[mv:INT]]`), `bot.edit_message_text` policy, `TelegramForbiddenError` handling (kicked bot privacy stop signal — same trade-off, same operator escalation path).

---

## 7. Tickets

Sprint grid for Phase 8. All sprints are sequential within their wave; waves are sequential.

| ID | Title | Component | Wave | Size | Files touched | Deps | Status |
|---|---|---|---:|---|---|---|---|
| **T8-S0** | Sprint 0: AUTHORIZED_SCOPE.md update + PHASE8_PLAN.md commit | docs | 0 | S | `docs/memory-system/PHASE8_PLAN.md` (new), `docs/memory-system/AUTHORIZED_SCOPE.md` (Phase 8 block insert), `docs/memory-system/ROADMAP.md` (row 8 note update) | none | **in-flight (this PR)** |
| **T8-01** | Migration 038 `extend_digests_review_states` + ORM model extension | A | 1 | M | `alembic/versions/038_extend_digests_review_states.py` (new), `bot/db/models.py` (Digest cols), `tests/db/test_digests_review_schema.py` (new) | T8-S0 | not-started |
| **T8-02** | `run_digest` weekly path + weekly cost ceiling + `digest_weekly_v0_1_0` prompt + `synthesize_digest` template wiring | B | 1 | L | `bot/services/digests.py` (signature widen, weekly cost helper), `bot/services/llm_prompts/digest_weekly_v0_1_0.py` (new), `bot/services/llm_gateway.py` (template id wiring, section-aware parsing), `tests/services/test_digests_weekly.py` (new) | T8-01 | not-started |
| **T8-03** | `build_digest_context` weekly extension (7-day window, larger budget) | B2 | 1 | M | `bot/services/digest_context.py` (signature widen, weekly overrides), `tests/services/test_digest_context_weekly.py` (new) | T8-01 | not-started |
| **T8-04** | `digest_review.py` state machine + `_cascade_digests` scan-filter widening + `redact_digest_for_forget` awaiting_review branch | C | 2 | L | `bot/services/digest_review.py` (new), `bot/services/forget_cascade.py:840` (status filter widen), `bot/services/digest_redactor.py` (awaiting_review admin-notify branch), `tests/services/test_digest_review.py` (new), `tests/services/test_forget_cascade_digests_weekly.py` (new) | T8-02, T8-03 | not-started |
| **T8-05** | Scheduler `digest_weekly_job` + `digest_stale_review_reaper_job` + `digest_stale_approved_reaper` widening + config | D | 2 | M | `bot/services/scheduler.py` (3 job adds / 1 widen), `bot/services/digests.py` (job bodies), `bot/config.py` (env var registration), `tests/services/test_scheduler_weekly.py` (new) | T8-04 | not-started |
| **T8-06** | Admin handlers `/digest_approve` `/digest_reject` `/digest_review` + `/digest_now weekly [--regenerate]` widening + renderer section bolding + weekly footer | E | 3 | M | `bot/handlers/digest.py` (4 commands), `bot/services/digest_renderer.py` (section header regex + weekly footer), `bot/services/digest_publisher.py` (trigger-state widen to accept `approved_for_publish`), `tests/handlers/test_digest_handlers_weekly.py` (new), `tests/services/test_digest_renderer_weekly.py` (new) | T8-05 | not-started |
| **T8-07** | Phase 11 binding tests L8a/b + C7 + I6a/b/c + R5 (regression: existing 34/34 preserved) | F | 3 | M | `tests/evals/test_leakage.py` (L8a/b), `tests/evals/test_citations.py` (C7), `tests/evals/test_refusal.py` (R5), `tests/evals/test_digest_forget_cascade.py` (I6a/b/c), `tests/fixtures/golden_recall/seed_v1/seed_meta.yaml` (baseline update 34→41 if needed) | T8-01..T8-06 | not-started |
| **T8-08** | Closure docs (PHASE8_ROLLOUT, IMPLEMENTATION_STATUS, ROADMAP, CLAUDE, AUTHORIZED_SCOPE) + PHASE8_PLAN_DRAFT.md archival flag + FHR | G | 3 | S | `docs/memory-system/PHASE8_ROLLOUT.md` (new), `docs/memory-system/IMPLEMENTATION_STATUS.md` (T8-* rows), `docs/memory-system/ROADMAP.md` (row 8 CLOSED), `CLAUDE.md` (Phase 8 closure block), `docs/memory-system/AUTHORIZED_SCOPE.md` (Phase 8 CLOSED note), `docs/memory-system/prompts/PHASE8_PLAN_DRAFT.md` (top banner: SUPERSEDED — content reflects Phase 9+ reflection backlog) | T8-07 | not-started |

### Per-ticket acceptance criteria

**T8-S0 acceptance:**
- `docs/memory-system/PHASE8_PLAN.md` committed (this file).
- `AUTHORIZED_SCOPE.md` "NOT in Phase 7 scope (defer to Phase 8+)" bullet for "Weekly digest scheduler / handler / publisher (Phase 8)" REMOVED (lines ~238-241 of the Phase 7 closure block).
- New `## Authorized: Phase 8 — Weekly editorial digest (2026-05-15)` block inserted immediately BEFORE the `## NOT authorized` heading. Block mirrors Phase 7 structure: TL;DR, owner (Orch A), scope reference to this `PHASE8_PLAN.md`, NOT-in-scope list mirroring §3 of this plan, ratification date.
- `ROADMAP.md` row 8 status updated from `NO` to `AUTHORIZED 2026-05-15 — plan ratified`.
- Single PR, docs-only, no code. PR title: `docs(memory): ratify Phase 8 plan + authorize weekly digest implementation`.

**T8-01 acceptance:**
- `alembic/versions/038_extend_digests_review_states.py` upgrades cleanly on a Phase-7 baseline DB and downgrades cleanly (with the data-presence guard from §5.A).
- ORM `Digest` mapped class adds `awaiting_review_at`, `published_by_admin_id`, `approved_at`, `review_notes` with correct types + nullability.
- `tests/db/test_digests_review_schema.py` asserts: CHECK accepts all 14 status values, rejects unknown, body-NOT-NULL invariant extends to `awaiting_review` / `approved_for_publish` / `rejected_by_admin`, partial index `ix_digests_status_awaiting_review` exists, `ck_digests_approved_audit` rejects `status='approved_for_publish'` rows missing `published_by_admin_id` or `approved_at` when `type='weekly'`, exempts daily.
- Downgrade test: insert a Phase-8 review-status row, attempt downgrade → fails with the expected RAISE EXCEPTION.

**T8-02 acceptance:**
- `run_digest(type='weekly', ...)` returns existing digest by `(type='weekly', ws, we)` without LLM call on re-run (idempotency).
- New weekly run: cost ceiling check fires before LLM. Exceeded → BOTH `digests` row (`type='weekly', status='cost_exceeded', body_markdown=NULL, citations='[]'::jsonb`) AND `digest_runs` row (`status='cost_exceeded', error_text='weekly digest budget exceeded'`) created.
- New weekly run: `synthesize_digest(prompt_template_version='digest-weekly-v0.1.0')` called exactly once with weekly context; LedgerRepo placeholder + update records cost; `body_markdown` contains `## Раздел: …` section headers + bullets with `[[cs:UUID]]` / `[[mv:INT]]` tokens.
- Section-aware parsing in `synthesize_digest`: bullets within section headers parse correctly; section headers do NOT count as bullets; bullet-level invariant fires identically per Phase 7.
- Empty window short-circuit: `status='skipped'`, no LLM call.
- Hallucinated citation IDs dropped; if any bullet has zero valid citations → `DigestCitationValidationError`.
- Unit tests cover: idempotency, weekly cost ceiling (independent of daily), empty window, LLM error, hallucinated citation drop, section header recognition.

**T8-03 acceptance:**
- `build_digest_context(type='weekly', ...)` returns a `DigestContext` with 7-day window bounds, `LIMIT 100` cards, `LIMIT digest_config.weekly_raw_message_top_n` raw messages, token budget `digest_config.weekly_token_budget_input - 1000`.
- Cards-first ordering preserved: cards fetched first; raw messages added only when `len(cards) < digest_config.weekly_min_cards_threshold`.
- Token budget enforced; raw messages dropped first on overflow.
- `_forget_excludes_predicate` shared helper still used identically.
- Unit tests cover: forgotten exclusion in weekly window, redacted exclusion, offrecord exclusion, weekly card threshold fallback, weekly token overflow drop.

**T8-04 acceptance:**
- `digest_review.transition_to_awaiting_review` transitions `draft → awaiting_review` atomically with `awaiting_review_at=now()`. Idempotent: re-call on `awaiting_review` row is a no-op (returns without raise).
- `digest_review.approve_digest` transitions `awaiting_review → approved_for_publish` with `published_by_admin_id` + `approved_at` set, then dispatches `publish_digest`. On stale citations: `failed` with `error_text='citations_stale_at_approval'`.
- `digest_review.reject_digest` transitions `awaiting_review → rejected_by_admin` with `review_notes` + `published_by_admin_id` set.
- All three reject calls from non-`awaiting_review` states raise `DigestReviewInvalidState(status)`.
- `_cascade_digests` scan filter at `forget_cascade.py:840` widened to include 8 statuses (verified by reading the source after change).
- `redact_digest_for_forget`: when called on an `awaiting_review` row, admin-notify fires with `error_text='forget_redacted_during_review'`.
- Unit tests cover: each state-machine transition, citations-stale-at-approval, scan-filter widening (insert weekly row in each status, fire forget, assert redact happens for the 8 statuses and is skipped for the others).

**T8-05 acceptance:**
- Scheduler job `digest_weekly` registered with `day_of_week="mon"`, `timezone=ZoneInfo("Europe/Moscow")`, `hour=settings.DIGEST_WEEKLY_HOUR_MSK`, `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`.
- Flag `memory.digests.weekly.enabled` default OFF in `feature_flags`.
- Job body re-checks flag and exits early if disabled.
- ISO week computation: mock `datetime.now(tz=MSK)` returning Mon 09:00 MSK any week W; asserts `window_start` = W-7d 00:00 MSK in UTC, `window_end` = W 00:00 MSK in UTC.
- `digest_stale_review_reaper_job` runs every 30 min; 7d pass moves `awaiting_review` rows older than 7d to `rejected_by_reaper` with audit insert + admin DM; 48h pass DMs admin + appends `[48h_notified]` marker (idempotent — single DM per row).
- `digest_stale_posting_reaper_job` widened to also catch `approved_for_publish` rows older than 5 min → `failed` with `error_text='stale_approved_reaper'`.
- Unit tests cover: window computation, reaper transitions, double-fire reaper idempotency.

**T8-06 acceptance:**
- `/digest_now weekly` admin-only, runs regardless of flag, respects weekly cost ceiling, transitions to `awaiting_review` on draft (NOT auto-publish).
- `/digest_now weekly --regenerate`: rejects unless current status is `rejected_by_admin` or `rejected_by_reaper`.
- `/digest_review` lists awaiting_review weekly digests with id/window/citations/hours-waiting columns.
- `/digest_approve <id>`: transitions to `approved_for_publish` + dispatches publisher; on success replies with `t.me/c/<chat_id>/<message_id>` link; on `DigestReviewInvalidState` replies with current status + suggestion.
- `/digest_reject <id> [reason]`: transitions to `rejected_by_admin` with reason; reply mentions `--regenerate` flag.
- Non-admin invocations: all four commands silently no-op (no leak — tested against non-admin user id).
- Renderer recognizes `## Раздел: …` headers and bolds them. Weekly footer template differs from daily.
- Unit tests cover: admin gate, idempotency reuse, regenerate flag, awaiting_review listing, approve+publish happy path, reject + reason, citations-stale at approval, section bolding render correctness.

**T8-07 acceptance:**
- `tests/evals/test_leakage.py` adds:
  - **L8a:** forgotten `message_version` is NOT in `body_markdown` of a weekly digest published after the forget event (cascade widening exercised).
  - **L8b:** forgotten `card_source` is NOT in body of a weekly digest (dual-kind path).
- `tests/evals/test_citations.py` adds:
  - **C7:** every weekly bullet has ≥1 citation_id resolvable in DB; section headers (`## Раздел: …`) are correctly skipped by the bullet scanner; the citation_id appears in the input context (no hallucination).
- `tests/evals/test_refusal.py` adds:
  - **R5:** review state-machine refusals: non-admin → `_is_admin` gate; cannot `/digest_approve` on `posted` row; cannot `/digest_approve` on `rejected_by_admin` row; cannot `/digest_publish` (Phase 7 path) on `awaiting_review` weekly row directly without admin approve.
- New file `tests/evals/test_digest_forget_cascade.py` (additions for weekly):
  - **I6a:** `forget_event` on `mvid` cited by a weekly digest in `awaiting_review` triggers redact (verifies scan-filter widening). Includes redact for `approved_for_publish` status too.
  - **I6b:** `forget_event` after approval but before publish — `approve_digest`'s step-3 revalidation catches the stale source and transitions to `failed` with `error_text='citations_stale_at_approval'`.
  - **I6c:** concurrent forget event affecting both a daily and a weekly digest citing the same mvid — both rows redact independently within the same cascade transaction; final statuses both `redacted`.
- Existing 34/34 baseline preserved: L1-L5 + L6a/b/c + L7a/b + C1-C4 + C5a-d + C6 + R1-R4 + I1-I4 + I5a/b/c. **New total: 41/41.**
- `tests/fixtures/golden_recall/seed_v1/seed_meta.yaml` baseline thresholds updated if Phase 11 binding row-counts change (otherwise unchanged — invariants are binary).
- Idempotency regression test: two concurrent `run_digest(type='weekly')` calls for the same window → only one digest row, second blocks via advisory lock OR returns existing row.

**T8-08 acceptance:**
- `docs/memory-system/PHASE8_ROLLOUT.md` operator playbook: env vars list, flag toggle order, dry-run via `/digest_now weekly`, admin review walkthrough, monitor first cron fire, escalation contacts. Mirrors the structure of `PHASE7_ROLLOUT.md`.
- `docs/memory-system/IMPLEMENTATION_STATUS.md` updated: every T8 ticket marked DONE with PR refs.
- `docs/memory-system/ROADMAP.md` row 8 marked CLOSED with PR list.
- `CLAUDE.md` "Memory System Cycle" section adds Phase 8 closure block (mirrors Phase 7 wording structure).
- `AUTHORIZED_SCOPE.md` "Authorized: Phase 8" block updated to CLOSED 2026-MM-DD; "NOT authorized" Phase 9 (reflection/observations) line remains unchanged.
- `docs/memory-system/prompts/PHASE8_PLAN_DRAFT.md` top banner: `# SUPERSEDED — see PHASE8_PLAN.md. Content below describes the original reflection/observations design, deferred to Phase 9+.` Document body preserved for historical reference (the reflection backlog feeds Phase 9 planning).
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
- **`DIGEST_WEEKLY_USD_CEILING` >= `LLM_DAILY_USD_CEILING`** at startup → STOP with `ConfigurationError("weekly digest ceiling must be below shared daily LLM ceiling — otherwise a single weekly run can starve the day's other LLM operations")`. The shared Phase 5 ceiling fires regardless; weekly ceiling must be the more conservative bound.
- **`DIGEST_DESTINATION_CHAT_ID == DIGEST_SOURCE_CHAT_ID`** at startup → STOP (inherited from Phase 7).
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
- **Daily 34/34 Phase 11 binding suite regresses** → STOP, the weekly extensions broke a daily invariant. Re-run T8-07 baseline before any other Wave 3 work.

---

## 9. Operator Playbook Hooks

### Env vars introduced (T8-05 wires into `bot/config.py`)

| Var | Default | Purpose |
|---|---|---|
| `DIGEST_WEEKLY_ENABLED` | `false` | Gate scheduler registration (still double-checked by `memory.digests.weekly.enabled` flag in DB). |
| `DIGEST_WEEKLY_DAY` | `1` | ISO day-of-week (Mon=1). Converted to apscheduler day string in scheduler. |
| `DIGEST_WEEKLY_HOUR_MSK` | `9` | Cron fire hour (Europe/Moscow). |
| `DIGEST_WEEKLY_USD_CEILING` | `5.00` | Weekly digest daily cost ceiling. MUST be < shared `LLM_DAILY_USD_CEILING`. |
| `DIGEST_WEEKLY_MONTHLY_USD_CEILING` | `20.00` | Weekly digest monthly ceiling. |
| `DIGEST_WEEKLY_TOKEN_BUDGET` | `24000` | Weekly context size cap. |
| `DIGEST_WEEKLY_MIN_CARDS_THRESHOLD` | `5` | Weekly cards-first threshold (higher than daily 3). |
| `DIGEST_WEEKLY_RAW_MESSAGE_TOP_N` | `60` | Weekly raw fallback cap. |
| `DIGEST_REVIEW_DEADLINE_HOURS` | `168` | 7d auto-reject deadline. |
| `DIGEST_REVIEW_48H_NOTIFY_HOURS` | `48` | First admin DM reminder. |

Inherited from Phase 7 (no change): `DIGEST_SOURCE_CHAT_ID`, `DIGEST_DESTINATION_CHAT_ID`, daily ceilings, daily window vars.

### Feature flags

- `memory.digests.weekly.enabled` — gate the weekly cron + scheduled `transition_to_awaiting_review`. Default OFF. Admin `/digest_now weekly` bypasses the flag (Q12 inheritance from Phase 7).

### Runbook references

- `docs/memory-system/PHASE8_ROLLOUT.md` (T8-08 deliverable) — full operator checklist.
- `CLAUDE.md` "Memory System Cycle" section — Phase 8 closure summary + outstanding issues.
- `docs/memory-system/PHASE7_ROLLOUT.md` — daily digest playbook is a prerequisite; weekly rollout assumes daily is GREEN.

---

## 10. Test Coverage Matrix

Cross-reference of T8 tickets → Phase 11 binding case additions → existing 34/34 baseline regression checks.

| Ticket | Phase 11 binding case | What it asserts | Files |
|---|---|---|---|
| T8-01 | (none — schema only) | Schema validity: CHECK accepts new statuses, downgrade fails if Phase-8 rows exist. | `tests/db/test_digests_review_schema.py` |
| T8-02 | C7 (partial) | Section headers don't break the bullet citation invariant. | `tests/evals/test_citations.py::C7` |
| T8-03 | (regression on L1-L5, L7a/b) | Forget exclusion still fires for weekly window. | Existing `tests/evals/test_leakage.py` re-run against weekly context fixtures. |
| T8-04 | L8a, L8b, I6a, R5 | Cascade scan widening, redact-during-review, admin gate. | `tests/evals/test_leakage.py::L8a/b`, `tests/evals/test_digest_forget_cascade.py::I6a`, `tests/evals/test_refusal.py::R5` |
| T8-05 | (regression — schedule timing) | Cron correctness, reaper idempotency. | `tests/services/test_scheduler_weekly.py` (unit) — not in evals because not a binding invariant. |
| T8-06 | R5 (admin gate) | Non-admin denial; state-machine refusals. | `tests/evals/test_refusal.py::R5` |
| T8-07 | All new cases (L8a/b + C7 + I6a/b/c + R5) | Bound suite extension. | All `tests/evals/*` files. |
| T8-08 | (docs — no test impact) | — | — |

### Regression on existing 34/34 baseline (must remain green throughout Wave 3)

| Existing case | Phase 8 risk to this invariant | Where Phase 8 could regress it |
|---|---|---|
| L1-L5 (leakage) | None direct; weekly context reuses Phase 7 governance filter. | T8-03 if `_forget_excludes_predicate` is forked instead of reused. |
| L6a/b/c (knowledge card leakage) | None — Phase 8 doesn't touch cards. | n/a |
| L7a/b (daily digest leakage) | Cascade widening must NOT regress daily. | T8-04 — the widened WHERE clause includes all Phase 7 statuses; daily flow unchanged. |
| C1-C4 (citation invariants) | None direct. | T8-02 section parsing must not affect Phase 7 daily prompt processing. |
| C5a-d (knowledge card citations) | None. | n/a |
| C6 (daily digest bullets) | None — daily prompt unchanged. | n/a |
| R1-R4 (refusal — Phase 4) | None. | n/a |
| I1-I4 (knowledge card forget cascade) | None — card cascade ordering preserved. | n/a |
| I5a/b/c (Phase 7 forget cascade) | None direct; daily redact path unchanged. | T8-04 widening adds branches but doesn't remove any. |

**Definition of pass:** `pytest tests/evals/ -v` shows 41/41 passing post-T8-07. No skipped cases. No xfail.

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

- **Issue #291** (shared `_forget_excludes_predicate` refactor): OPEN. The predicate currently exists as inline SQL in both `digest_context.py` and `forget_cascade.py`. Phase 8 plan reads cleanly against either state (refactored or inline). If #291 lands during Phase 8 implementation, T8-03 inherits the refactor; if not, T8-03 keeps the inline SQL pattern. **Recommendation:** land #291 BEFORE T8-03 to avoid SQL drift between weekly and daily.
- **Issue #295** (T7-02 post-merge MED items): OPEN. Independent of Phase 8 surface — no blocking concern. Track separately.

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

- **AC1.** Weekly cron `digest_weekly` fires on schedule (default Mon 09:00 MSK) when flag `memory.digests.weekly.enabled=ON`; strict no-op when flag OFF (verified by logs showing "flag disabled, skipping"). Verified by T8-05 acceptance + manual cron fire during T8-08 rollout dry-run.
- **AC2.** Weekly digest reaches `awaiting_review` terminal in the auto-pipeline; the scheduler/job NEVER auto-publishes. Verified by T8-04 acceptance + T8-05 + binding test R5.
- **AC3.** Single-admin approval transitions `awaiting_review → approved_for_publish → posting → posted`. Defense-in-depth citation revalidation in `approve_digest` step 3 re-checks every cited source against current governance state; stale citations transition to `failed` with `error_text='citations_stale_at_approval'`. Verified by T8-04 + T8-06 + binding test I6b.
- **AC4.** Admin rejection transitions to `rejected_by_admin` terminal; rejected digests can be re-run via `/digest_now weekly --regenerate`. Verified by T8-04 + T8-06.
- **AC5.** Forget cascade redacts weekly rows in any of `{draft, awaiting_review, approved_for_publish, posting, posted, redacted, redacted_edit_failed, rejected_by_admin}`. Dual-citation-kind handling (`message_version` + `card_source`) identical to Phase 7. Verified by T8-04 cascade widening + binding tests L8a, L8b, I6a, I6c.
- **AC6.** Cost ceiling enforced via separate bucket: `DIGEST_WEEKLY_USD_CEILING` (default $5.00) and `DIGEST_WEEKLY_MONTHLY_USD_CEILING` (default $20.00). Weekly LLM invocations are filtered in the ledger query by `d.type='weekly'`. The shared Phase 5 `LLM_DAILY_USD_CEILING` ALSO fires (both bounds checked). Verified by T8-02 acceptance.
- **AC7.** Stale-review reaper runs every 30 min: 48h pass DMs first admin with a single notification per row (marker `[48h_notified]` in `review_notes` prevents repeats); 7d pass auto-rejects with `status='rejected_by_reaper'` terminal + audit insert + admin DM. Verified by T8-05 + T8-07 (R5 indirectly covers).
- **AC8.** Phase 11 binding suite 34→41 green: existing 34 daily/Phase-7 cases preserve regression-free (no new fails on L1-L5, L6a/b/c, L7a/b, C1-C4, C5a-d, C6, R1-R4, I1-I4, I5a/b/c), 7 new weekly cases pass (L8a/b, C7, I6a/b/c, R5). Verified by T8-07.

**Final Holistic Review (FHR) trigger:** required per Rule 9 of `~/.claude/rules/superflow-enforcement.md` — Phase 8 has 8 sprints (≥4) and binds new privacy invariants. Two reviewers (Claude deep-product + Codex deep-technical) on the full Phase 8 surface. Fix CRITICAL/HIGH before closure report.

---

## 14. PR Workflow

Sprint-PR-queue mode. One PR per ticket. Linear order:

1. T8-S0 → main (docs-only authorization). Solo, no review parallel.
2. T8-01 → main (Wave 1 schema). Reviewed by Codex + Claude product.
3. T8-02 + T8-03 — Wave 1 implementation. Can ship in EITHER order but both on main before T8-04. Sequential PRs.
4. T8-04 → main (Wave 2 review service + cascade widening + redactor branch). Largest Wave 2 PR; may split into 4A (digest_review.py + tests) and 4B (forget_cascade widening + redactor branch + tests) if diff >400 lines.
5. T8-05 → main (Wave 2 scheduler + reaper).
6. T8-06 → main (Wave 3 admin handlers + renderer).
7. T8-07 → main (Wave 3 binding tests). Phase 11 41/41 green prerequisite.
8. T8-08 → main (Wave 3 closure docs).
9. **Final Holistic Review** after T8-08 merged. Two reviewers (Claude deep-product + Codex deep-technical) on the full Phase 8 surface. Fix CRITICAL/HIGH before closure report.

Each PR:
- One ticket, diff ≤400 lines (split if larger).
- Tests added/extended with the change.
- `.par-evidence.json` written before push.
- Codex review via `Agent(subagent_type="codex:codex-rescue")` with technical-lens prompt.
- Claude standard-product-reviewer for product/spec lens.
- Both verdicts PASS / ACCEPTED → PR created.
- CI green → user-initiated merge (Phase 3).

---

## 15. Glossary (Phase 8-specific)

- **Weekly digest:** a derived Markdown editorial recap for a completed ISO week, section-organized. Reviewed by admin before publish.
- **ISO week:** Mon 00:00 MSK..next Mon 00:00 MSK (exclusive-end), stored as UTC. Cron fires Mon 09:00 MSK.
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
