# Phase 8 — Weekly Editorial Digest: Operator Rollout Checklist

**Status:** Phase 8 CLOSED 2026-05-15. Feature flag default OFF — production rollout below.

This document is the operator playbook for enabling the Phase 8 weekly
editorial digest on production. Phase 8 is shipped DARK (flag default OFF).
Follow this checklist in order; do NOT skip steps. The weekly rollout
**assumes Phase 7 daily digest is already GREEN** (`PHASE7_ROLLOUT.md`).

## Pre-rollout invariants

Before flipping any flag, confirm:

- [ ] Phase 7 (daily digest) is rolled out and stable. Weekly editorial
  digest reuses the same `digests` / `digest_runs` tables, the same forget
  cascade `digests` layer, the same `digest_redactor`, the same publisher,
  and the same renderer; weekly only widens trigger states and adds a
  review state machine on top.
- [ ] Phase 11 binding suite is GREEN on `main` HEAD. Run:
  `pytest tests/evals/ -v` — should show 42/42 passing
  (L1-L5 + L6a/b/c + L7a/b + L8a/b + C1-C4 + C5a-d + C6 + C7 + R1-R4 + R5.a/b/c/d
  + I1-I4 + I5a/b/c + I6a + I6b.1/.2/.3 + I6c).
- [ ] Database migration 038 is APPLIED in the target environment.
  Verify: `alembic current` returns `038` (or later).
- [ ] Operator has access to the bot's admin DM (their telegram_id is in
  `ADMIN_IDS`) and is ready to handle weekly approval / rejection workflow.
- [ ] `DIGEST_SOURCE_CHAT_ID` and `DIGEST_DESTINATION_CHAT_ID` are configured
  (inherited from Phase 7) and `src != dst` (enforced by
  `load_digest_config` raising `ConfigurationError`).

## Required environment variables (Phase 8 additions)

| Var | Default | Description |
|---|---|---|
| `DIGEST_WEEKLY_ENABLED` | `false` | Gate scheduler registration. Still double-checked at runtime by `memory.digests.weekly.enabled` flag in DB. |
| `DIGEST_WEEKLY_HOUR_MSK` | `9` | Cron fire hour (Europe/Moscow). Monday-only. |
| `DIGEST_WEEKLY_MINUTE_MSK` | `15` | Cron fire minute. **15-minute stagger past daily 09:00 (H8)** — avoids concurrent LLM gateway pressure on Mondays. |
| `DIGEST_WEEKLY_USD_CEILING` | `5.00` | Weekly digest daily cost ceiling (USD, decimal). **Independent of `LLM_DAILY_USD_CEILING` and `DIGEST_DAILY_USD_CEILING`** (C5). Gates only weekly synthesis via `WHERE d.type='weekly'` ledger filter. |
| `DIGEST_WEEKLY_MONTHLY_USD_CEILING` | `20.00` | Weekly digest monthly ceiling. Same independence semantics. |
| `DIGEST_WEEKLY_TOKEN_BUDGET` | `24000` | Weekly LLM context size cap. 7× daily window → larger budget. |
| `DIGEST_WEEKLY_MIN_CARDS_THRESHOLD` | `8` | Weekly cards-first threshold. L5: bumped from daily-3 to weekly-8 (empirical middle between daily-3 and linear-scaled 21). |
| `DIGEST_WEEKLY_RAW_MESSAGE_TOP_N` | `60` | Weekly raw fallback cap. |
| `DIGEST_REVIEW_DEADLINE_HOURS` | `168` | 7-day auto-reject deadline (`rejected_by_reaper`). |
| `DIGEST_REVIEW_48H_NOTIFY_HOURS` | `48` | First admin DM reminder before auto-reject. |

`DIGEST_WEEKLY_DAY` was considered and **rejected per M3** —
`day_of_week="mon"` is hardcoded in the scheduler registration AND
`isoweekday() - 1` in the window-anchor math. Operator preference for a
different day is a 2-line code edit, not a runtime config concern.

Inherited from Phase 7 (no change): `DIGEST_SOURCE_CHAT_ID`,
`DIGEST_DESTINATION_CHAT_ID`, `DIGEST_HOUR_MSK`, daily ceilings, daily
window vars, plus the shared Phase 5 LLM env vars (`LLM_PROVIDER`,
`LLM_MODEL`, `LLM_DAILY_USD_CEILING`, etc.).

## Rollout sequence

1. **Set env vars in production.** Add the `DIGEST_WEEKLY_*` block above.
   Restart the bot. Log scan: confirm `digest_weekly_job: ...` and
   `digest_stale_review_reaper_job: ...` registrations on startup.

2. **Manual weekly dry-run via admin.** With flag still OFF, run
   `/digest_now weekly` from admin DM. The handler bypasses the flag (Q12
   inheritance from Phase 7).
   - Expected: `✅ Posted to review queue. digest_id=X status=awaiting_review`
     (weekly draft does NOT auto-post — it enters the review state machine).
   - Inspect the synthesized content via `/digest_review`. Verify section
     structure (Highlights / People / Decisions / Open questions / Other —
     allowlist of 5 sections), citation density, and absence of forbidden
     content (`#offrecord`, `#nomem`, forgotten).

3. **Approve dry-run via `/digest_approve <id>`.** Confirms the 3-step
   approve flow (revalidate context → guarded UPDATE
   `awaiting_review → approved_for_publish` → dispatch publisher → posted).
   Expected: `✅ Posted. digest_id=X`. Inspect posted Telegram message
   manually.

4. **Reject path test via `/digest_reject <id> [reason]`** on a fresh
   weekly draft. Verifies the
   `awaiting_review → rejected_by_admin` transition without publishing.

5. **Flip the feature flag.** In a DB session:
   ```sql
   INSERT INTO feature_flags (flag_key, scope_type, scope_id, enabled)
   VALUES ('memory.digests.weekly.enabled', NULL, NULL, TRUE)
   ON CONFLICT (flag_key, scope_type, scope_id)
   DO UPDATE SET enabled = TRUE;
   ```

6. **Monitor first 2 weekly cron fires.** Cron is **Mon 09:15 MSK**.
   - Check `/digest_review` Monday morning — there should be a fresh
     `awaiting_review` weekly digest.
   - Inspect within 48h. If 48h elapses without approve/reject, expect
     admin DM reminder. After 7d total, expect auto-reject
     (`rejected_by_reaper`) and admin DM notification.
   - Inspect `digest_runs` for errors:
     `SELECT * FROM digest_runs WHERE digest_type='weekly' ORDER BY id DESC LIMIT 5;`

## Operator alerts (admin DM notifications)

The Phase 8 review SM + reaper add new DM channels on top of Phase 7
alerts. The bot DMs the first admin in `ADMIN_IDS` on these conditions:

- **Review queue arrival** — every Monday after weekly cron, an admin DM
  surfaces the new `awaiting_review` digest with `/digest_review` link.
- **48h reminder** — `digest_stale_review_reaper_job` DMs the admin when
  a `awaiting_review` row is older than `DIGEST_REVIEW_48H_NOTIFY_HOURS`
  (default 48h) and hasn't been actioned.
- **7d auto-reject** — when `DIGEST_REVIEW_DEADLINE_HOURS` (default 168h)
  elapses, the reaper transitions the row to `rejected_by_reaper` and DMs
  the admin with the final disposition.
- **Redaction during review** — if a `/forget` event fires while a weekly
  digest is in `awaiting_review`, the forget cascade widens to redact the
  in-review row (per `_REDACTOR_ELIGIBLE_STATUSES` 8-tuple including
  `awaiting_review`). Admin DM notifies that the queued digest was
  redacted and may need `/digest_now weekly --regenerate`.
- **Existing Phase 7 alerts** continue to fire for weekly rows once they
  reach `posting` / `posted` / `redacted_edit_failed` /
  `bot_kicked_from_posted_chat_id` / `publish_lock_timeout` /
  `cost_exceeded` statuses.

## Disable / rollback

To turn the weekly feature OFF:

```sql
UPDATE feature_flags
SET enabled = FALSE
WHERE flag_key = 'memory.digests.weekly.enabled';
```

The next Monday cron fire becomes a no-op. The 48h/7d reaper continues to
run regardless of flag state — that's intentional, so any in-flight
`awaiting_review` rows still resolve cleanly.

**Existing `awaiting_review` rows** can be cleared manually via
`/digest_reject <id>` from admin DM.

**Migration 038 downgrade is BLOCKED while review-state rows exist.** The
migration's pre-flight downgrade guard refuses to downshift the
`ck_digests_status` CHECK enum if any row is in
`awaiting_review` / `approved_for_publish` / `rejected_by_admin` /
`rejected_by_reaper` / `regenerated_by_admin` (or the Phase 7 `posting`
state). Manual cleanup before downgrade:

```sql
-- Inspect what's blocking
SELECT id, type, status, awaiting_review_at FROM digests
 WHERE status IN ('awaiting_review','approved_for_publish',
                  'rejected_by_admin','rejected_by_reaper',
                  'regenerated_by_admin');

-- Then either approve / reject each row via admin handlers, OR drop the
-- rows entirely (after audit-log archival):
DELETE FROM digest_runs WHERE digest_id IN (<ids>);
DELETE FROM digests     WHERE id        IN (<ids>);
```

Then re-run `alembic downgrade 037`.

## Schema summary (delta vs Phase 7)

- `digests`:
  - New columns: `published_by_admin_id` (bigint, nullable),
    `approved_at` (timestamptz, nullable), `review_notes` (text,
    nullable), `awaiting_review_at` (timestamptz, nullable).
  - `ck_digests_status` widened with 5 new audit values:
    `awaiting_review`, `approved_for_publish`, `rejected_by_admin`,
    `rejected_by_reaper`, `regenerated_by_admin` (NOT VALID + VALIDATE
    pattern, per L9 of plan).
  - `ck_digests_approved_audit` — new CHECK enforcing that
    `approved_at IS NOT NULL` whenever `status='approved_for_publish'`.
  - `body NOT NULL` visible-states widened to also cover the new
    visible-state values (`awaiting_review` / `approved_for_publish`).
  - Partial index `ix_digests_status_awaiting_review` for fast review
    queue scans.
- `digest_runs`: `ck_digest_runs_status` CHECK widened with the same
  5 new audit values (append-only audit log preserves admin actions).

## Citation kinds (unchanged from Phase 7)

- `kind='message_version'`, `id` = `message_versions.id` (bigint).
- `kind='card_source'`, `id` = `card_sources.id` (UUID as string).
- Tokens written by the prompt: `[[mv:INT]]` and `[[cs:UUID]]`.
- Citation `position` is bullet-index (not token ordinal) — partial-forget
  cascade redacts per bullet correctly (F3 from Phase 7 FHR fix sprint).

## Phase 8.5 carryovers (post-closure)

Tracked for future cleanup:

- **§5.I renderer extension** — section header bolding + weekly-specific
  footer copy. Flagged by T8-06 implementer as out-of-scope for that
  brief; the current renderer produces functional output without the
  styling polish.
- **M6 GIN index dead weight on `_cascade_digests`** — `forget_cascade`
  scans `digests.citations` via JSONB containment; the existing
  `ix_digests_citations_gin` index is not optimally used. Perf-only
  Phase 7.5 / 8.5 follow-up; not a privacy or correctness blocker.
- **#291** — shared `_forget_excludes_predicate` helper refactor between
  `forget_cascade._cascade_message_versions` and `digest_context.py` /
  `llm_gateway._digest_context_is_clean`. T8-03 added the predicate
  inline with an explicit TODO referencing #291 (predicate is identical
  to the Phase 7 inline copy — safe to extract when #291 lands).
- **R5.a/R5.b binding cases** — service-layer contract assertions for
  weekly admin-gate refusals. Full handler-layer assertion can be
  tightened in a small follow-up once `/digest_review` /
  `/digest_approve` / `/digest_reject` handler interactions are exercised
  in the binding suite at the handler level (currently asserted at the
  service layer; handler layer is already shipped in T8-06).

These are NOT blockers — Phase 8 ships as ratified.
