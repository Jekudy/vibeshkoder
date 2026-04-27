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
| T0-02  | Fix/contain sqlite vs postgres upsert in UserRepo           | done           | Sprint 2 / PR #42. Option A chosen (postgres-only dev). `bot/db/engine.py` drops sqlite branch, validates DATABASE_URL, raises clear error on sqlite/empty. CI gets postgres service container. New test module `tests/db/test_user_repo.py` (4 DB-backed tests + 2 engine-validation tests). Existing 24 tests still pass. `pytest-asyncio` added to dev deps with `asyncio_mode = "auto"`. `aiosqlite` moved from runtime to dev deps (used only by `tests/test_scheduler_deadlines.py`). |
| T0-03  | Make MessageRepo.save idempotent                            | done           | Sprint 3 / PR #43. `MessageRepo.save` rewritten with `INSERT ... ON CONFLICT DO NOTHING RETURNING` + SELECT-existing fallback. Duplicate `(chat_id, message_id)` returns the existing row without raising and without creating a duplicate. Handler `bot/handlers/chat_messages.py` no longer needs `try/except + session.rollback()` — that broad rollback was wiping the upstream `UserRepo.upsert` and `set_member` work in the same transaction. New tests under `tests/db/test_message_repo.py` cover: insert, duplicate-returns-existing, no-duplicate-row, original-text-preserved, distinct messages coexist. |
| T0-04  | Implementation status doc                                   | done           | This file + ROADMAP.md + AUTHORIZED_SCOPE.md + HANDOFF.md. |
| T0-05  | /healthz + startup checks                                   | done           | Sprint 4 / PR #45. New `bot/services/health.py` (DB ping + settings-sanity check + non-secret startup banner). New `web/routes/health.py` exposes `GET /healthz` (public, 200 healthy / 503 degraded, no secrets in response). `bot/__main__.py` extended with startup logging (bot identity, DB OK, allowed_updates list canonicalised in `_ALLOWED_UPDATES` constant with rollout-rule comment). Tests under `tests/web/test_health.py`: 200 healthy / 503 db-down / no-secret-leak / unauthenticated path / startup banner no-secret. |
| T0-06  | Regression tests for T0-01..T0-03 + T0-05                   | done           | Sprint 5 / PR #46. New `tests/regression/test_gatekeeper_safety.py` smoke-checks all Phase 0 invariants in one file: non-member forward_lookup denied, admin allowed, UserRepo.upsert round-trips, MessageRepo.save duplicate-safe, /healthz reachable. Suite runs in <2s offline (DB-backed checks skip cleanly without postgres; CI runs them all). |

## Phase 1 — Source of truth + raw archive

| Ticket | Title                                                       | Status         | Notes |
|--------|-------------------------------------------------------------|----------------|-------|
| T1-01  | feature_flags table/repo                                    | done           | Sprint 6 / PR #TBD. Alembic migration `003_add_feature_flags` (id pk, flag_key non-null, scope_type/scope_id nullable, enabled bool default false, config_json, updated_by, created_at/updated_at; unique `(flag_key, scope_type, scope_id)`; index on `enabled`). New `bot/db/models.py::FeatureFlag` + `bot/db/repos/feature_flag.py::FeatureFlagRepo` with `get(flag_key, scope_type, scope_id) -> bool` (missing → False) and `set_enabled(...)` upsert helper. Migration intentionally seeds NO rows — all `memory.*` flags default OFF. Tests under `tests/db/test_feature_flag_repo.py` (5 DB-backed + 1 metadata smoke) cover: missing-returns-false, set-creates-row, set-updates-no-duplicate, per-scope coexists with global, no-seed-rows invariant, model registered in metadata. |
| T1-02  | ingestion_runs table                                        | done           | Sprint 7 / PR #49. Alembic migration `004_add_ingestion_runs` (id pk, run_type non-null + check live/import/dry_run/cancelled, source_name nullable, started_at default now, finished_at nullable, status default 'running' + check, stats_json/config_json/error_json; indexes (run_type, started_at) and (status)). New `bot/db/models.py::IngestionRun` and `bot/db/repos/ingestion_run.py::IngestionRunRepo` with `create / update_status / get_active_live` methods. Validates run_type / status against allowed sets; refuses payloads with secret-shaped top-level keys (`token`, `secret`, `password`, `api_key`, `passphrase`). `update_status` sets `finished_at = now(UTC)` once on first terminal transition. Tests under `tests/db/test_ingestion_run_repo.py` (13 total: 12 DB-backed + 1 metadata smoke). |
| T1-03  | telegram_updates table                                      | done           | Sprint 8 / PR #TBD. Alembic migration `005_add_telegram_updates` (id pk, update_id BigInteger nullable, update_type non-null, raw_json/raw_hash, received_at default now, chat_id/message_id nullable, FK ingestion_run_id → ingestion_runs.id nullable, is_redacted bool default false, redaction_reason; partial unique index on `update_id WHERE update_id IS NOT NULL`; indexes (update_type, received_at), (chat_id, message_id)). New `bot/db/models.py::TelegramUpdate` and `bot/db/repos/telegram_update.py::TelegramUpdateRepo` with `insert(idempotent on update_id) / get_by_update_id`. Live updates conflict-safe; synthetic import updates (NULL update_id) bypass partial index and rely on raw_hash + ingestion_run_id for dedup (importer enforces). Tests: 6 DB-backed + 1 metadata smoke. Service/handler wiring is T1-04. |
| T1-04  | raw update persistence service                              | done           | Sprint 9 / PR #TBD. New `bot/services/governance.py` (T1-04 stub: `detect_policy(text, caption) -> ('normal', None)` + `redact_raw_for_offrecord` no-op helper) — T1-12 will replace the stub with real `#nomem`/`#offrecord` detection, no other changes needed. New `bot/services/ingestion.py` with `record_update(session, update, ingestion_run_id)`, `get_or_create_live_run(session)`, `is_raw_archive_enabled(session)` + helpers (`_compute_raw_hash` SHA-256 of canonical JSON, `_classify_update_type`, `_extract_chat_and_message_ids`, `_extract_text_and_caption`). New `bot/middlewares/raw_update_persistence.py::RawUpdatePersistenceMiddleware` registered AFTER `DbSessionMiddleware` in `bot/__main__.py` so persistence + detection + (future) redaction run inside the same DB transaction the handler commits. Behaviour gated by feature flag `memory.ingestion.raw_updates.enabled` (default OFF — set via `FeatureFlagRepo.set_enabled`); when OFF the middleware is a pass-through and no rows are written. Failures in the raw-archive path are logged and swallowed so the gatekeeper bot keeps working. Tests: `tests/services/test_governance_stub.py` (5 tests: stub returns 'normal' for plain / nomem-token / offrecord-token / None-inputs / redactor passthrough — T1-12 will flip several of these). `tests/services/test_ingestion.py` (10 tests: flag-off no-op, flag-on insert, idempotent duplicate, raw_hash deterministic + key-order independent, get_or_create_live_run create + attach, stub detector wiring spy, update-type classifier, chat/message id extractor). |
| T1-05  | Extend chat_messages columns                                | done           | Sprint 10 / PR #TBD. Alembic migration `006_extend_chat_messages` adds 11 nullable/default columns: `raw_update_id` (FK to telegram_updates.id, ON DELETE SET NULL), `reply_to_message_id` BigInt, `message_thread_id` BigInt, `caption` Text, `message_kind` String(64), `current_version_id` Integer (forward-ref to message_versions.id — T1-06 adds FK), `memory_policy` String default 'normal' + check, `visibility` String default 'member' + check, `is_redacted` Bool default false, `content_hash` String(128), `updated_at` DateTime nullable. Indexes: (chat_id, date), reply_to_message_id, message_thread_id, memory_policy, content_hash. Server defaults populate existing rows automatically (no destructive backfill needed). ChatMessage model extended to match. Tests: legacy-row-shape-survives, new-fields-persist, invalid memory_policy/visibility rejected via CHECK, all 4 valid policies accepted, T0-03 MessageRepo.save still idempotent (regression), metadata smoke. |
| T1-06  | message_versions table + FK closure                         | done           | Sprint 11 / PR #TBD. Alembic migration `007_add_message_versions` creates message_versions (id pk, chat_message_id FK→chat_messages.id ON DELETE CASCADE, version_seq Int NOT NULL, text/caption/normalized_text Text nullable, entities_json JSON nullable, edit_date DateTime nullable, captured_at default now NOT NULL, content_hash String(128) NOT NULL, raw_update_id FK→telegram_updates.id ON DELETE SET NULL, is_redacted Bool default false; unique (chat_message_id, version_seq); indexes content_hash + captured_at + chat_message_id). Also closes T1-05's forward-ref: adds FK constraint `fk_chat_messages_current_version_id` (chat_messages.current_version_id → message_versions.id ON DELETE SET NULL). New `MessageVersion` model + `MessageVersionRepo` with `get_by_hash`, `get_max_version_seq`, `insert_version` (idempotent on (chat_message_id, content_hash) — duplicate hash returns existing). Tests: 9 (8 DB-backed + 1 metadata smoke) covering v1 creation, seq increment, duplicate-hash idempotency, max-seq lookup, get-by-hash null path, FK closure round-trip, unique (msg_id, seq) violation, ON DELETE CASCADE wipes versions, metadata smoke. |
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
