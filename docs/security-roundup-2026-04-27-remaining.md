# Security Roundup — Remaining Work Map (2026-04-27)

**Trigger:** Round 5 P0 cycle complete (7 CRIT issues, 14 PRs merged). This map enumerates every finding from the audit + final cross-check that is still open, grouped by priority and effort.

**Sources:**
- `docs/security-audit-2026-04-27-hard-review.md` (40 findings, 4 reviewers, 2 passes)
- Final cross-check (Round 5 retrospective): ag-sa, ag-mentor, Claude Code Reviewer, codex (pending)
- New regressions discovered in Round 5: tracked separately as REGRESSION-NN

**Status legend:** 🟢 done · 🟡 in progress · ⚪ open · 🔵 needs decision

---

## Round 5 P0 — Done (closed)

| ID | Title | PR | Status |
|---|---|---|---|
| CRIT-01 | Bypass vouching via bearer invite | #62 | 🟢 merged |
| CRIT-02 | Ghost invites after DB rollback (outbox pattern) | #65 | 🟢 merged |
| CRIT-02-r3 | Hotfix: privacy_block flow restored | #69 | 🟢 merged |
| CRIT-03 | CAS auto-reject — don't overwrite vouched | #64 | 🟢 merged |
| CRIT-04 | Empty WEB_SESSION_SECRET fail-fast | #60 | 🟢 merged |
| CRIT-05 | Coolify token + WEB_SESSION_SECRET rotation | n/a | 🟢 done (ag-mechanic) |
| CRIT-07 | Threat model 1-pager | #59 | 🟢 merged |
| CRIT-06 | Operator security split (YubiKey, Tailscale) | n/a | ⚪ user-side handoff |

---

## New issues from Round 5 final cross-check

### REGRESSION-01: invite_worker = new SPOF without watchdog
**Source:** ag-sa final pass · **Class:** systems / open-loop
**File:** `bot/services/invite_worker.py` (CRIT-02 introduction)
**Issue:** worker scheduled by APScheduler with `max_instances=1, coalesce=True`. If scheduler thread dies or worker hangs in retry loop — no probe, no alert, no fallback. Outbox rows accumulate `pending` silently.
**Fix:** sentinel watchdog — alert if `invite_outbox.status='pending' AND created_at < now() - interval '10min'`. Either cron-script or `/healthz` derived check.
**Effort:** S (1 sprint)

### REGRESSION-02: vouch flow invariants not encoded as DB constraints
**Source:** ag-sa final pass · **Class:** data integrity
**Issue:** 3 implicit invariants live only in handler code, not enforced by schema:
1. `applications.invite_user_id IS NOT NULL` when `status='vouched'`
2. `users.is_member=True` requires matching row in `vouch_log` (verified per H2 audit, but constraint missing)
3. Outbox row exists for every `vouched` application that ever sent an invite
**Fix:** add CHECK constraints + foreign keys + audit query for legacy violations.
**Effort:** M (1-2 sprints, includes data audit)

### REGRESSION-03: 14 fixes without post-deploy verification (open-loop pattern)
**Source:** ag-sa final pass + HIGH-12 echo · **Class:** observability
**Issue:** Round 5 closed 7 P0 issues but introduced zero verification probes in production. No metric for "rejected admissions", "outbox failures", "auto-reject CAS conflicts", etc. Means: drift detection = zero. Same critique that ag-sa flagged 2026-04-27 morning still applies.
**Fix:** define minimum viable health probe set per Round 5 fix. See HIGH-12 below — same fix.
**Effort:** S (covered by HIGH-12 if executed properly)

---

## P1 — High priority (9 items, all open)

| ID | Title | File | Effort | Notes |
|---|---|---|---|---|
| HIGH-08 | forward_lookup exact-text identification (privacy leak between members) | `bot/handlers/forward_lookup.py:58-65` | S | use `forward_origin` metadata instead of text match |
| HIGH-09 | N2 missing legacy intro_text backfill (XSS on legacy intros) | `bot/__main__.py:77-80`, `bot/handlers/forward_lookup.py:76-80` | S | alembic migration: html_escape backfill |
| HIGH-10 | Cookie `secure=True` missing (cookie leak if HTTP fallback) | `web/routes/auth.py:37-43` | XS | one-line fix |
| HIGH-11 | pg_dump = fake disaster recovery (same disk, no offsite, never restored) | infra | M | restic→B2 + age encryption + quarterly drill |
| HIGH-12 | No feedback loops anywhere (open-loop control across 14 fixes) | meta | M | minimal cron health-script with TG alerts |
| HIGH-13 | Operator burden — 8+ rotation rituals without automation | infra | M | dependabot + GH Actions reminders + `docs/ops/rotation-calendar.md` |
| HIGH-14 | requirements.lock without CVE update process | `requirements.lock` | XS | dependabot weekly auto-PR |
| HIGH-15 | CI gates without owner / SLA / runbook (security theater) | `.github/workflows/` | S | `docs/ops/ci-security-response.md` per gate |
| HIGH-16 | Cross-team conflict in 30 days (memory team T0/T1 collisions) | `bot/web/app.py`, `bot/web/middleware.py`, `bot/db/models.py` | S | `docs/contracts/shared-files.md` |

---

## P1 — Process / methodology (2 items)

| ID | Title | Effort | Notes |
|---|---|---|---|
| MID-17 | Open-loop control pattern (9/40 findings, systemic) | M | every CI gate / mount / backup / rate limit / review needs (probe, owner, SLA) before merge |
| MID-18 | Process antipattern — repeated 75567d36 (no raw evidence, prompts >200 lines, no falsification step) | S | sentinel pre-dispatch checklist for reviewer agents |

---

## P1 — Code-level (10 items, MID class)

| ID | Title | File | Effort |
|---|---|---|---|
| MID-19 | N3 vouch flooding via filling state — no TTL on filling apps | scheduler | S |
| MID-20 | H2 phantom members audit — existing `is_member=True` rows not verified | DB query | XS |
| MID-21 | get_or_create_live_run race | `bot/services/ingestion.py:98-109`, migration `004_add_ingestion_runs.py:58-63` | S |
| MID-22 | NULL update_id dedupe gap | `bot/db/repos/telegram_update.py:77-90` | S |
| MID-23 | /healthz exposed without auth + DB pressure | `web/routes/health.py:17-20`, `bot/services/health.py:72-75` | S |
| MID-24 | Raw archive fail-open (no DLQ) | `bot/middlewares/raw_update_persistence.py:53-61` | S |
| MID-25 | Offrecord retains content hash (privacy regression) | `bot/services/ingestion.py:141-158` | S |
| MID-26 | Dockerfile without hash pinning | `Dockerfile.bot:1,13`, `Dockerfile.web:1,13` | S |
| MID-27 | Phantom legacy rollback path (rollback drill never run) | runbook | M |
| MID-28 | Tailscale without break-glass (single-point lockout) | runbook | S |

---

## P2 — Lower priority (5 items, deferred)

| ID | Title | Status |
|---|---|---|
| LOW-29 | vaultwarden over-deletion | deferred |
| LOW-30 | N5 sha pin coupling | handoff exists (`.handoffs/handoff_2026-04-26_20-05_n5-sha-pin.md`) |
| LOW-31 | H7 semgrep ruleset not custom-tuned | future sprint |
| LOW-32 | Strategic blind spot — single-admin baked into infra | roadmap note |
| LOW-33 | Strategic blind spot — review process not fixed | covered by MID-18 |

---

## Recommended GitHub Issue seeding (proposed)

**Tier 1 — Must seed (10 issues, all P0/blocker-adjacent):**
1. CRIT-06 (already exists, status: Todo) — operator security split
2. REGRESSION-01 — invite_worker watchdog
3. REGRESSION-02 — vouch invariants as DB constraints
4. HIGH-08 — forward_lookup privacy leak (privacy)
5. HIGH-09 — legacy intro_text XSS (security)
6. HIGH-10 — cookie secure=True (XS, ship now)
7. HIGH-11 — disaster recovery (operational survival)
8. HIGH-12 — feedback loops (closes REGRESSION-03 & MID-17)
9. HIGH-15 — CI gates runbook (closes security theater)
10. MID-20 — H2 phantom members audit (XS, single SQL)

**Tier 2 — Track but don't seed individually (group as epics):**
- Operator hygiene: HIGH-13, HIGH-14, MID-26, MID-27, MID-28
- Code-level fixes: MID-19, MID-21..25
- Cross-team coordination: HIGH-16, MID-18

**Tier 3 — Park (P2):**
- LOW-29..33 stay in this document only.

---

## Next steps

1. **User decision:** confirm Tier 1 list. Each becomes a GitHub Issue.
2. After seeding: schedule which Tier 1 to attack in Round 6 (probably HIGH-10, HIGH-12, MID-20 first — small, high-leverage).
3. Tier 2 epics get one parent issue per epic.
4. GitHub Issues are canonical for scope and status; this document remains supporting evidence.

---

## Appendix: meta-finding

ag-sa pass-2 honest meta: *"Этот review — работа на 30%, ритуал на 70%."* Most findings lack raw evidence. Reviewers went deep monotonically without falsification step. Same antipattern as 75567d36. **Before Round 6 — fix the review process itself (MID-18).**
