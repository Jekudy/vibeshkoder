# Phase 9 — Wiki / Community Catalog: Operator Rollout Checklist

**Status:** Phase 9 CLOSED 2026-05-19. Feature flag default OFF — production rollout below.

This document is the operator playbook for enabling the Phase 9 community
wiki on production. Phase 9 ships DARK (`memory.wiki.enabled` default OFF).
Follow this checklist in order; do NOT skip steps. Two-password role split
is a HARD prerequisite — if `WEB_MEMBER_PASSWORD` is not set, member access
to the wiki is structurally impossible.

## Pre-rollout invariants

Before flipping any flag, confirm:

- [ ] Phase 11 binding suite is GREEN on `main` HEAD. Run:
  ```
  EVAL_HARNESS_ENABLED=1 timeout 300 pytest tests/evals/ -v
  ```
  Should show **60/60 passing** (42 prior + 18 new Phase 9 IDs:
  L9a-e + C8a-b + I7a-e + R6.a-f + G1).
- [ ] Database migrations `050` through `054` are APPLIED in the target
  environment. Verify: `alembic current` returns `054` (or later).
- [ ] `WEB_ADMIN_PASSWORD` is set AND `WEB_MEMBER_PASSWORD` is set as a
  DIFFERENT value (config rejects equality). If `WEB_MEMBER_PASSWORD` is
  unset, member login is disabled and wiki is admin-only.
- [ ] The legacy `WEB_PASSWORD` env var is REMOVED (it's aliased to
  `WEB_ADMIN_PASSWORD` for one release cycle but emits a deprecation
  warning on every startup — cleanup is part of Phase 9 rollout).
- [ ] Operator (`telegram_id ∈ ADMIN_IDS`) has tested `/wiki_publish`,
  `/wiki_unpublish`, `/wiki_robots` against a draft page in staging.
- [ ] `Cache-Control: no-store` on 410/404 responses for public wiki paths
  is verified via `curl -I` against a stale/unpublished slug (smoke check).

## Required environment variables (Phase 9 additions)

| Var | Default | Description |
|---|---|---|
| `WEB_ADMIN_PASSWORD` | none (REQUIRED) | Admin login password. Min length enforced by `_validate_password_field`. Replaces legacy `WEB_PASSWORD`. |
| `WEB_MEMBER_PASSWORD` | none (optional) | Member login password. If unset, only admin can authenticate via web — member-internal wiki routes return 401/302. MUST differ from `WEB_ADMIN_PASSWORD`. |

No Phase-9-specific env vars beyond auth: the wiki feature is gated by a
DB-controlled `feature_flags.memory.wiki.enabled` row, not env. Rate
limiting / cache TTL / robots policy live in code defaults; per-page
`public_enabled` and `robots_policy` are admin-curated via Telegram
handlers.

Inherited from prior phases (no change): `ADMIN_IDS`, `BOT_TOKEN`,
`DATABASE_URL`, web session secrets (`WEB_SESSION_SECRET_KEY` if used),
Phase 5 LLM env vars.

## Rollout sequence

1. **Set / verify auth env vars in production.**
   - Add `WEB_ADMIN_PASSWORD` (REQUIRED) and `WEB_MEMBER_PASSWORD`
     (optional but recommended for full wiki access). Remove legacy
     `WEB_PASSWORD` if present.
   - Restart the bot + web app. Log scan: confirm NO
     `WEB_PASSWORD is deprecated` warnings; confirm `web/auth.py`
     reports two configured roles.

2. **Apply migrations.** From the bot host:
   ```
   alembic upgrade head
   ```
   Verify the 5 new tables exist:
   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema='public'
     AND table_name IN ('wiki_pages','wiki_revisions','wiki_publication_log',
                        'wiki_page_message_sources','wiki_page_card_sources')
   ORDER BY table_name;
   ```
   Should return 5 rows.

3. **Seed the feature flag row (OFF).** Phase 9 expects the flag row to
   exist explicitly so admin handlers can return a deterministic
   "wiki disabled" message rather than a missing-row error.
   ```sql
   INSERT INTO feature_flags (flag_key, scope_type, scope_id, enabled)
   VALUES ('memory.wiki.enabled', NULL, NULL, FALSE)
   ON CONFLICT (flag_key, scope_type, scope_id) DO NOTHING;
   ```

4. **Seed one draft wiki page** via the page-management workflow that
   your team owns (no admin Telegram command currently creates pages;
   pages are seeded via SQL or via a forthcoming UI in Phase 9.5).
   - `slug` is the URL fragment. Lowercase, kebab-case.
   - `page_status='draft'` initially.
   - `public_enabled=false`, `robots_policy='noindex'`.
   - Body markdown can reference `[^mv:N]` and `[^card:UUID]` tokens —
     the renderer validates each at render time.

5. **Flip the flag ON.** This is the kill switch — flipping back to FALSE
   immediately disables all wiki routes + admin handlers.
   ```sql
   UPDATE feature_flags
   SET enabled = TRUE, updated_at = now()
   WHERE flag_key = 'memory.wiki.enabled'
     AND scope_type IS NULL AND scope_id IS NULL;
   ```
   No restart required — handlers re-read the flag per request.

6. **Smoke test as admin.** Open the web UI, log in with
   `WEB_ADMIN_PASSWORD`. Navigate to `/wiki/<slug>` for the seeded draft.
   Expected: page renders, `[⚠ SOURCE UNAVAILABLE]` markers visible for
   any invalid citation tokens, page status banner shows `draft`.

7. **Smoke test as member.** Open in a fresh browser (or incognito), log
   in with `WEB_MEMBER_PASSWORD`. Navigate to the same `/wiki/<slug>`.
   Expected: page renders WITHOUT admin markers (invalid citations are
   silently suppressed for members). If `page_status != 'reviewed'`, the
   member surface MAY still render — but `/wiki/public/{slug}` will return
   404 (only `reviewed + public_enabled=true` makes it public).

8. **Promote to reviewed.** Once an admin verifies the page content is
   suitable for community visibility:
   ```sql
   UPDATE wiki_pages
   SET page_status = 'reviewed', updated_at = now()
   WHERE slug = '<slug>';
   ```
   This makes the page eligible for `/wiki_publish`.

9. **Publish the page.** From an admin Telegram session:
   ```
   /wiki_publish <slug>
   ```
   The handler:
   - Acquires advisory lock on every cited mvid (matches cascade lock
     order — closes T9-07 race window).
   - Runs `validate_sources()`. Aborts if ANY offrecord / forgotten /
     redacted source is detected.
   - Captures `prior_public_enabled` via `SELECT ... FOR UPDATE`.
   - Sets `public_enabled=true`, `robots_policy='noindex'` (default —
     index requires explicit `/wiki_robots`).
   - Inserts `wiki_publication_log` audit row with `action='publish'`,
     `prior_*` and `new_*` captured atomically.

10. **(Optional) Allow search engine indexing.** Only after operator
    consensus that the page is durably appropriate for public visibility:
    ```
    /wiki_robots <slug> index
    ```
    DB constraint `ck_wiki_pages_robots_index_requires_public` enforces
    that `robots_policy='index'` is only valid when `public_enabled=true`.

11. **Verify `/robots.txt`.** Should now `Allow: /wiki/public/<slug>` for
    indexed slugs and `Disallow: /wiki/` for the rest. Cache TTL is
    intentionally short to allow rapid revocation.

12. **Verify public surface.** `curl -I https://<host>/wiki/public/<slug>`
    should return `200 OK` for the published slug. For an unpublished or
    forgotten slug it MUST return 410 (gone) or 404 with
    `Cache-Control: no-store`. This is the privacy invariant — never
    leak cached forgotten content via CDN/edge.

## Verification matrix (post-rollout)

| Check | Command | Expected |
|---|---|---|
| Flag is ON | `SELECT enabled FROM feature_flags WHERE flag_key='memory.wiki.enabled';` | `t` |
| Migrations applied | `alembic current` | `055` (or later — includes legacy-grace nullable wiki_page_id) |
| Two passwords configured | `web/auth.py` logs on startup | `WEB_ADMIN_PASSWORD set; WEB_MEMBER_PASSWORD set` |
| Member login → role='member' | `curl -X POST /login -d 'password=<member-pw>'` then decode session cookie | `role=='member'`. R6.e binding test additionally proves that *if* a `user_id` form field is supplied, it's silently ignored (`web/routes/auth.py` accepts only `password`); role is derived from password match alone. |
| Privacy binding green | `EVAL_HARNESS_ENABLED=1 pytest tests/evals/test_wiki_*.py` | 30/30 pass |
| Forget cascade hits wiki | Trigger `/forget_reply` on a cited message → `SELECT page_status FROM wiki_pages WHERE id=<id>` | `stale` or `archived`, `public_enabled=false` |
| robots.txt gated correctly | `curl https://<host>/robots.txt` | Only `/wiki/public/<indexed-slug>` allowed |
| 410 on forgotten public page | `curl -I /wiki/public/<forgotten-slug>` | `410 Gone` + `Cache-Control: no-store` |

## Kill switch (emergency disable)

If a privacy regression is suspected:

```sql
UPDATE feature_flags
SET enabled = FALSE, updated_at = now()
WHERE flag_key = 'memory.wiki.enabled';
```

Effect within ~1 request:
- Admin handlers `/wiki_publish` / `/wiki_unpublish` / `/wiki_robots`
  reply with "wiki disabled" and refuse to mutate state.
- Member routes `/wiki/{slug}` and `/wiki/search` return 503.
- Public route `/wiki/public/{slug}` returns 404 with `Cache-Control:
  no-store`.
- `/robots.txt` returns the disallow-all variant.

This is reversible — flip the flag back to TRUE to restore service. No
data is lost; only the surfaces are gated.

## Rollback (migration downgrade)

Downgrade is destructive (drops 5 tables + per-page audit log). Only run
on staging or after confirmed irreparable data corruption:

```bash
alembic downgrade 049
```

Pre-flight check: ensure no Phase 10 migrations (060+) are applied that
might reference Phase 9 tables. If Phase 10 graph_provenance references
`wiki_pages.id` (it does NOT in the current design, but verify),
downgrade will fail FK pre-flight.

## Phase 9.5 carryovers (defer to follow-up)

These are deferred items tracked for post-launch follow-up:

- **L9a assertion polish** (Claude product r1 MEDIUM, non-blocking) —
  L9a uses an OR-form assertion; paired with L9b/L9d/L9e which assert
  `page_status` directly. Hardening to AND-form is cosmetic.
- **FK action mismatch on `created_by_user_id`** (Codex FHR MED #3) —
  column is NOT NULL but FK action is `ON DELETE SET NULL`. Future
  user-row delete will fail. Only relevant if/when anonymization
  workflow is added; deferred until then.
- **`_cascade_wiki_revisions` idempotency guard** (Codex FHR LOW #4) —
  rewriting already-redacted revision rows on overlapping later forget
  events overwrites `redacted_by_forget_event_id`. Preserve first-
  redaction provenance via partial predicate. Cosmetic; mask format is
  already deterministic.
- **Stale-page member silent 404 → 410** (Claude FHR MED-4) — member
  route returns generic 404 when `page_status='stale'/'archived'`;
  should return 410 Gone with templated explanation. Public path
  already returns 410 + `Cache-Control: no-store` correctly.
- **Missing `WEB_MEMBER_PASSWORD` startup warning** (Claude FHR MED-5) —
  unset env var silently disables member login. Should emit explicit
  log warning if `memory.wiki.enabled` is ON.
- **Cache-Control on member `/wiki/{slug}`** (Claude FHR MED-6) — public
  path applies `Cache-Control: no-store`; member path does not.
- **Two-admin quorum** (deferred) — current model: one admin can
  publish. Future: require N-of-M admin approval before
  `public_enabled=true`. Out of scope for v1.
- **Edit-conflict resolution UI** — current model: last-writer-wins on
  `wiki_pages.body_markdown`. Web UI for diff/merge deferred.
- **Multilingual rendering** — current model: single body_markdown per
  page. Per-language variants deferred.
- **Static export** — current model: dynamic render every request.
  Pre-rendered HTML cache deferred.
- **Page tagging + moderation flow** — deferred to Phase 9.5+.

**Closed in this cycle** (do not list as carryover):
- `_insert_legacy_grace_audit` FK violation — **FIXED** via migration 055
  (nullable `wiki_page_id` + CHECK `(wiki_page_id IS NOT NULL OR
  action='legacy_cookie_grace')`). I7d binding test now asserts row
  persists.
- `_cascade_wiki_pages` audit revision retaining forgotten body —
  **FIXED** (Codex FHR CRITICAL #1) — INSERT pre-masks
  `body_markdown` + sets `revision_status='forgotten_redacted'` +
  `redacted_at` + `redacted_by_forget_event_id`. New integration test
  `test_cascade_wiki_pages_audit_revision_pre_masked` verifies.
- Member login flow broken — **FIXED** (Claude FHR HIGH-1) — role-aware
  redirect + role-aware nav + login copy.

## Communications

After the flag flip, the operator should:

1. Post an announcement in the operator channel: "Wiki is LIVE.
   `/wiki_publish <slug>` to publish; `/wiki_unpublish <slug>` to
   revoke. `/wiki_robots <slug> [index|noindex]` to control search
   engines."
2. Update the community-facing welcome message / pinned post to point
   to the published wiki slugs.
3. Monitor `wiki_publication_log` for the first 24h for any
   unauthorized publish attempts (`actor_user_id NOT IN ADMIN_IDS` —
   should never appear, but verify).
