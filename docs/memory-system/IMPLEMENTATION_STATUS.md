# Memory System — Implementation Status

**Last updated:** 2026-04-26 (cycle start)
**Branch:** `feat/memory-foundation` (worktree `.worktrees/memory`)
**Source of truth:** this file is updated after every PR merge into `main`.

---

## Reading this file

- **Status legend:**
  - `not started` — no code exists
  - `in progress` — branch open, PR not merged
  - `done` — merged into `main`, verified
  - `verified` — `done` + independent reviewer confirmed acceptance criteria
  - `done (legacy)` — code existed before this cycle; needs verification mapping

- If a phase is missing from the table — it has not begun.

---

## Phase 0 — Gatekeeper stabilization

| Ticket | Title                                                       | Status         | Notes |
|--------|-------------------------------------------------------------|----------------|-------|
| T0-01  | Fix forward_lookup membership/admin check                   | verified       | Merged in PR#11 / commit `7f95b53` (security audit C3). Verified 2026-04-26 by independent code-reviewer subagent (output preserved in PR #16 description and commit message). Acceptance verbatim: "non-member denied; member allowed; admin allowed; no intro in denial response; auth guard runs BEFORE any DB lookup of message author or intro." Tests cover (a) non-member denied, (b) member allowed, (c) admin allowed via DB flag, (d) no intro leaked in denial. Independent reviewer also confirmed no bypass code path (`F.forward_origin` registered only in `forward_lookup.py`). |
| T0-01-r1 | Test: admin authorized via `settings.ADMIN_IDS` (env-only) | not started    | nice-to-have. Does NOT block T0-06 regression suite. Standalone GitHub issue #18. |
| T0-01-r2 | Test: unknown user (UserRepo.get returns None) silent return | not started   | nice-to-have. Does NOT block T0-06. GitHub issue #19. |
| T0-01-r3 | Distinguish denial log lines: "user not in DB" vs "not a member" | not started | quality. Does NOT block T0-06. GitHub issue #20. |
| T0-02  | Fix/contain sqlite vs postgres upsert in UserRepo           | not started    | First implementation ticket of this cycle. |
| T0-03  | Make MessageRepo.save idempotent                            | not started    |
| T0-04  | Implementation status doc                                   | done           | This file + ROADMAP.md + AUTHORIZED_SCOPE.md + HANDOFF.md. |
| T0-05  | /healthz + startup checks                                   | not started    |
| T0-06  | Regression tests for T0-01..T0-03                           | not started    |

## Phase 1 — Source of truth + raw archive

| Ticket | Title                                                       | Status         | Notes |
|--------|-------------------------------------------------------------|----------------|-------|
| T1-01  | feature_flags table/repo                                    | not started    |
| T1-02  | ingestion_runs table                                        | not started    |
| T1-03  | telegram_updates table                                      | not started    |
| T1-04  | raw update persistence service                              | not started    |
| T1-05  | Extend chat_messages columns                                | not started    | All new columns nullable / default. |
| T1-06  | message_versions table                                      | not started    |
| T1-07  | v1 backfill                                                 | not started    | Chunked if needed. |
| T1-08  | content_hash strategy                                       | not started    | Normalized text+caption+entities+kind. |
| T1-09  | Persist reply_to_message_id                                 | not started    |
| T1-10  | Persist message_thread_id                                   | not started    |
| T1-11  | Persist caption + message_kind                              | not started    |
| T1-12  | Minimal #nomem / #offrecord detector                        | not started    | Deterministic only. No LLM. |
| T1-13  | offrecord_marks minimal table                               | not started    |
| T1-14  | edited_message handler                                      | not started    | Blocked by T1-06. |

## Phase 2a — Import dry-run (stretch)

| Ticket | Title                                                       | Status         | Notes |
|--------|-------------------------------------------------------------|----------------|-------|
| T2-01  | Telegram Desktop import dry-run parser                      | not started    | Stretch only. No apply. |

## Phase 3 — Governance (stretch skeleton)

| Ticket | Title                                                       | Status         | Notes |
|--------|-------------------------------------------------------------|----------------|-------|
| T3-01  | forget_events tombstone skeleton                            | not started    | Stretch. Required before T2-03 import apply (which is itself out of scope this cycle). |

## Phases 2b, 4–12

Not started. Not authorized. See `AUTHORIZED_SCOPE.md` for gating rules.

---

## What exists in the current codebase (baseline 2026-04-26)

Confirmed by inspecting `bot/`, `web/`, `alembic/`, `tests/` on `main`:

- aiogram bot (long polling), `bot/__main__.py`. `allowed_updates` currently includes only:
  `message`, `callback_query`, `chat_member`, `my_chat_member`. **No** `edited_message`,
  `message_reaction`, `message_reaction_count`. No edit / reaction handlers.
- `bot/db/models.py` — `users`, `applications`, `questionnaire_answers`, `intros`,
  `chat_messages`, `intro_refresh_tracking`, `vouch_log`. `chat_messages` has only:
  `id`, `message_id`, `chat_id`, `user_id`, `text`, `date`, `raw_json`, `created_at`. No
  `reply_to_message_id`, no `message_thread_id`, no `caption`, no `message_kind`, no
  `memory_policy`, no `visibility`, no `content_hash`, no `current_version_id`.
- No `telegram_updates` table.
- No `message_versions` table.
- No `feature_flags` / `ingestion_runs` / `offrecord_marks` / `forget_events` / `chat_threads`.
- No import path (Telegram Desktop or otherwise).
- No `#nomem` / `#offrecord` detection.
- No `/forget` / `/forget_me` commands.
- No q&a, no LLM gateway, no extraction, no catalog, no wiki, no graph.
- Admin web is the gatekeeper dashboard, not a memory review UI.
- Tests: `test_all.py`, `test_flow.py`, plus `tests/` with security audit additions and
  `scheduler_deadlines` isolation fix from commit `c70cc4e`.

## Active risks (carried from architect handoff)

| Risk                                                | Status                                |
|-----------------------------------------------------|---------------------------------------|
| `forward_lookup` privacy leak                       | fixed in PR#11; verifier confirming   |
| Dev sqlite vs postgres-specific upsert              | open — T0-02                          |
| `MessageRepo.save` not cleanly idempotent           | open — T0-03                          |
| Old `SPEC.md` and v0.5 design spec out of date      | mitigated — v0.5 archived; SPEC.md    |
|                                                     | will get a status banner in T0-04 PR  |

---

## Update protocol

After each PR merge into `main`:

1. Move ticket(s) from `not started` / `in progress` → `done`.
2. After verifier subagent confirms acceptance criteria, mark `verified`.
3. Add the merge commit SHA in the Notes column.
4. If a ticket is split or new follow-ups appear, add rows. Never silently delete a row — if
   superseded, write `superseded by T#-##` in Notes.
5. Update `Last updated` at the top.
