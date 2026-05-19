# Memory System — Implementation Status

**Last updated:** 2026-05-15 (Phase 8 CLOSED — all 8 weekly digest tickets merged; review SM + reaper + binding 42/42; Phase 8.5 carryovers tracked. Phase 7 CLOSED 2026-05-15 prior. Phase 6 CLOSED 2026-05-12.)
**Active worktrees:** none (Phase 6 closed). Historical: `.worktrees/p6-w1-stream-b` (T6-02, closed); `.worktrees/orch-A` (Phase 5–6 planning); `.worktrees/orch-B` (Phase 9/10/12 planning); `.worktrees/orch-C` (Phase 11 planning); `.worktrees/p2-alpha/bravo/charlie/delta` (Phase 2 closed 2026-04-29); `.worktrees/p4-hotfix-164` (closed by PR #203).
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
| T1-06  | message_versions table + FK closure                         | done           | Sprint 11 / PR #56. Alembic migration `007_add_message_versions` creates message_versions (id pk, chat_message_id FK→chat_messages.id ON DELETE CASCADE, version_seq Int NOT NULL, text/caption/normalized_text Text nullable, entities_json JSON nullable, edit_date DateTime nullable, captured_at default now NOT NULL, content_hash String(128) NOT NULL, raw_update_id FK→telegram_updates.id ON DELETE SET NULL, is_redacted Bool default false; unique (chat_message_id, version_seq); indexes content_hash + captured_at + chat_message_id). Also closes T1-05's forward-ref: adds FK constraint `fk_chat_messages_current_version_id` (chat_messages.current_version_id → message_versions.id ON DELETE SET NULL). New `MessageVersion` model + `MessageVersionRepo` with `get_by_hash`, `get_max_version_seq`, `insert_version` (idempotent on (chat_message_id, content_hash) — duplicate hash returns existing). Tests: 10 (9 DB-backed + 1 metadata smoke) covering v1 creation, seq increment on different hash, duplicate-hash idempotency, max-seq zero/after-inserts, get-by-hash null path, FK closure round-trip, unique (msg_id, seq) violation, ON DELETE CASCADE wipes versions, metadata smoke. **Deferred from T1-06 acceptance**: live-ingestion wiring that AUTO-creates v1 on every new chat_messages insert + populates `chat_messages.current_version_id`. This wiring depends on T1-08 (content_hash strategy) and lands in T1-14 edited_message handler (which also covers v(n+1) on edits). Issue #30 acceptance bullet "Update chat_messages.current_version_id" → moved to T1-14. T1-07 v1 backfill of existing rows is a separate ticket. |
| T1-07  | v1 backfill                                                 | done           | Sprint 12 / PR #TBD. New `bot/services/content_hash.py::compute_content_hash` (SHA-256 of canonical JSON tuple [text, caption, message_kind, entities_json]; T1-08 will ratify/extend). New `bot/services/backfill.py::backfill_v1_message_versions(session, batch_size=1000)` walks `chat_messages WHERE current_version_id IS NULL`, computes hash, INSERTs `message_versions` v1 with `normalized_text=text`, then UPDATEs `current_version_id`. Chunked. Idempotent: re-run returns 0. Alembic data migration `008_backfill_message_versions_v1` invokes the service via async engine glue; rejects non-postgres dialect (T0-02). Tests: 8 (5 DB-backed: happy path 5 rows, idempotent re-run, chunking with batch_size=2, NULL-text rows, skip-existing-current-version-id; 3 offline: content_hash determinism / case-sensitivity / None / caption-included). |
| T1-08  | content_hash strategy                                       | done           | Sprint 13 / PR #TBD. Ratifies + extends `bot/services/content_hash.py` from T1-07's first-cut to formal canonical recipe per HANDOFF §9: payload = `[HASH_FORMAT_VERSION, text, caption, message_kind, normalized_entities]`. New `_normalize_entities()` sorts entity list by `(offset, length, type)`. `HASH_FORMAT_VERSION = "chv1"` included in hashed payload — future recipe changes bump tag and produce new hashes cleanly. Function signature accepts ONLY 4 canonical inputs (no kwargs catch-all) — passing volatile `date`/`raw_json` raises TypeError. Backward-compat: T1-07-backfilled v1 rows persist with legacy hashes; chv1 applies to live-ingested versions only (T1-14+). `MessageVersionRepo.insert_version` idempotency unaffected (keys on (msg_id, hash)). Tests: 15 covering determinism, sensitivity to text/caption/kind, kind-None defaults to 'text', entity list-order independence, entity dict-key order independence, empty ≡ None, different entities → different hash, offset sensitivity, format-version-in-payload (monkeypatch flips constant → different hash), chv1 smoke, sha256-hex shape, unicode handled, signature rejects volatile kwargs (date / raw_json). |
| T1-09  | Persist reply_to_message_id                                 | done           | Sprint 14 / PR #TBD (combined T1-09/10/11). New `bot/services/normalization.py::extract_reply_to_message_id` extracts from aiogram `message.reply_to_message.message_id`; returns None if reply absent or stub. `bot/handlers/chat_messages.py` calls `extract_normalized_fields(message)` and passes the dict into extended `MessageRepo.save`. T1-05's nullable `reply_to_message_id` column populated. Tests under `tests/services/test_normalization.py`. |
| T1-10  | Persist message_thread_id                                   | done           | Sprint 14 / PR #TBD (combined T1-09/10/11). `extract_message_thread_id` reads aiogram `message.message_thread_id`; nullable for non-forum chats. T1-05's nullable column populated. Tests under `tests/services/test_normalization.py`. |
| T1-11  | Persist caption + message_kind                              | done           | Sprint 14 / PR #TBD (combined T1-09/10/11). `extract_caption` keeps caption SEPARATE from text (Phase 4 q&a wants captions as first-class content). `classify_message_kind` returns deterministic kind (`text`/`photo`/`video`/`voice`/`audio`/`document`/`sticker`/`animation`/`video_note`/`location`/`contact`/`poll`/`dice`/`forward`/`service`/`unknown`); `forward` takes priority over `text` for forwarded messages. Handler now persists raw_json when message has text OR caption (was: only when text). Tests under `tests/services/test_normalization.py` cover text / photo+caption / video / voice / document / forward-priority / service / unknown / extract_normalized_fields composition. MessageRepo.save extended with optional `reply_to_message_id`/`message_thread_id`/`caption`/`message_kind`/`raw_update_id` kwargs (backward-compat — defaults to None preserves T0-03 behavior). |
| T1-12  | Minimal #nomem / #offrecord detector                        | merged         | Sprint 15 / PR #63 (combined T1-12+T1-13). REAL deterministic detector replaces T1-04 stub. `bot/services/governance.py::detect_policy` regex-matches `#nomem` and `#offrecord` in text + caption (case-insensitive, hashtag-bounded so `#nomembership` doesn't match). offrecord takes precedence. Returns `(policy, mark_payload)` with audit metadata. `redact_raw_for_offrecord` actually drops content fields (`text`, `caption`, `entities`, `caption_entities`) from known event fields (`message`, `edited_message`, `channel_post`, `edited_channel_post`) **and recurses into nested message-shaped fields** (`reply_to_message`, `pinned_message`, `external_reply`, `quote`) — closes Codex HIGH on parent-content leak via reply_to_message snapshot. Preserves ids/timestamps/sender/chat metadata. **Both governance gaps closed**: telegram_updates path (T1-04 wiring already in place — stub swapped in this PR) AND chat_messages path (handler calls detect_policy BEFORE save, redacts content for offrecord, sets memory_policy column, creates offrecord_marks row via T1-13 repo). MessageRepo.save extended with optional `memory_policy` + `is_redacted` kwargs. 22 detector/redactor tests + 5 chat_messages handler tests. Follow-up issues: #66 (extend _NESTED_MESSAGE_FIELDS for unsubscribed Telegram event types), #67 (ON CONFLICT DO NOTHING returns stale memory_policy), #68 (offrecord_marks asymmetry between handlers). |
| T1-13  | offrecord_marks minimal table                               | merged         | Sprint 15 / PR #63 (combined T1-12+T1-13). Alembic migration `011_add_offrecord_marks` (renumbered from 009 after rebase — main merged 009/010 invite-outbox migrations in parallel) (id, mark_type non-null + check ('nomem','offrecord'), scope_type non-null + check ('message','thread','chat'), scope_id nullable, chat_message_id FK→chat_messages.id ON DELETE CASCADE, thread_id BigInt, set_by_user_id FK→users.id ON DELETE SET NULL, detected_by non-null, detected_at default now NOT NULL, expires_at, status default 'active' + check; 3 indexes). New `OffrecordMark` model + `OffrecordMarkRepo.create_for_message(chat_message_id, mark_type, detected_by, set_by_user_id, thread_id)` — flushes; no commit. Tests: 5 (4 DB-backed + 1 metadata smoke) covering active row creation, thread_id, invalid mark_type rejected, CASCADE on message delete, model registered. |
| T1-14  | edited_message handler                                      | merged + hotfix | Sprint 16 / PR #75 (merged 2026-04-27). Hotfix PR #TBD addresses Codex Phase 1 final-review CRITICAL: `_apply_offrecord_flip` now also nulls text/caption/normalized_text/entities_json + sets is_redacted=True on every existing message_versions row of the parent (privacy invariant — without this, T1-07 backfilled v1 rows + any prior v(n+1) rows retained raw content after the offrecord flip). Final Phase 1 ticket. New `bot/handlers/edited_message.py` (Router, GroupChatFilter, COMMUNITY_CHAT_ID guard) handles `edited_message` Telegram updates: (a) hash-based idempotency via `compute_content_hash` (chv1) + `MessageVersionRepo.insert_version` keyed on (chat_message_id, content_hash) — unchanged content is no-op; (b) `detect_policy` runs BEFORE any DB content mutation (privacy ordering rule); (c) flip `normal→offrecord` retroactively nulls `chat_messages.text/caption/raw_json` + `is_redacted=True` + `memory_policy='offrecord'` + creates `offrecord_marks` row, all in same tx; (d) flip `offrecord→normal` updates `memory_policy='normal'` but content fields stay NULL (irreversibility doctrine — HANDOFF.md §10); (e) unknown prior message → log warning + return (no placeholder); (f) legacy v1 rows: runtime recompute chv1 from existing row's text/caption/kind before comparison (no migration). Adds `edited_message` to `bot/__main__.py::_ALLOWED_UPDATES` and registers router before `chat_messages` catch-all. **Process**: dual-team independent implementation (two parallel ag-developer agents in isolated worktrees) + Codex cross-team review → caught a privacy bug in Team A on offrecord→normal flip restoring text/caption to parent; final branch combines correct ordering + legacy hash from Team A with correct flip semantics + __main__.py wiring from Team B + a 10th test (`test_edit_offrecord_to_normal_does_not_write_text_caption_to_parent`) that captures the actual UPDATE statement values dict to assert the bug class is locked behind a regression test. 10/10 edited_message tests pass; 131 pass / 66 skip across full suite. Follow-up issues to log on merge: (i) re-confirmed offrecord edit does not create fresh `offrecord_marks` row (Claude MEDIUM-1, audit-trail asymmetry, complementary to #68), (ii) integration-style state-based test pairing the value-capture test with a real db_session assertion (Claude NIT-1, refactor-resilience). |

## Phase 2 — Importer + governance skeleton

| Ticket | Title                                        | Status        | Notes |
|--------|----------------------------------------------|---------------|-------|
| T3-01  | forget_events table + repo                   | done          | Sprint 1 (Stream Charlie) / commits a613ce1 → 8c5d983. Alembic 013 creates forget_events with named UNIQUE on tombstone_key, FK actor_user_id → users.id ON DELETE SET NULL, CHECK constraints on target_type ∈ {message, message_hash, user, export}, authorized_by ∈ {self, admin, system, gdpr_request}, policy ∈ {forgotten, offrecord_propagated}, status ∈ {pending, processing, completed, failed}, cascade_status JSONB. ForgetEventRepo: race-safe `create` (postgres ON CONFLICT DO NOTHING RETURNING + fallback SELECT, mirrors MessageRepo.save), `get_by_tombstone_key`, `list_pending` (status='pending', ordered by created_at ASC, id ASC for tie-break, limit), atomic `mark_status` (UPDATE ... WHERE status IN allowed_old RETURNING — race-safe state machine for cascade worker; populate_existing=True on error-path re-fetch). Tests: 14 (insert all 4 target_type variants, idempotent re-create, valid pending→processing→{completed, failed}, rejected pending→completed and completed→processing, list_pending FIFO+limit with id tie-breaker, JSONB nested-dict round-trip, failed-state terminal lockout, model metadata smoke). Process: 3 reviewers (deep-analyst PASS, Claude product ACCEPTED, Codex 3 rounds → APPROVE after HIGH×2 race-safety + MEDIUM JSONB + LOW ordering + HIGH identity-map fixes). |

## Phase 2 — Importer (planned, all tickets logged as GitHub issues)

Backlog logged after Phase 1 close. Ag-sa final audit produced the dependency DAG and
identified 8 spec-defined tickets (T2-01..T2-03, T3-01..T3-05) plus 8 NEW tickets
(T2-NEW-A..H) covering documentation, helpers, resume/checkpoint, rate limiting and
rollback that the original spec left implicit.

Critical path to "Phase 2 closed": **#89 → T3-01 → T3-05 → T2-03 → T2-NEW-G**.
Phase 2 status: **DONE — 20/20 issues complete after #104**.

| Order | Issue | Ticket | Title                                                | Pri | Size | Deps |
|-------|-------|--------|------------------------------------------------------|-----|------|------|
| 1     | #91   | T2-NEW-A | Telegram Desktop export schema + fixtures          | P0  | M    | none |
| 2     | #92   | T3-01    | forget_events table + repo                         | P0  | M    | T1-13 (done) |
| 3     | #89   | (helper) | persist_message_with_policy() — Phase 2 prerequisite | P1 | M    | issues #67/#80/#81 |
| 4     | #93   | T2-NEW-B | Import user mapping policy                         | P0  | M    | T2-NEW-A |
| 5     | #94   | T2-01    | Import dry-run parser                              | P0  | M    | T1-02 (done), #91, #93 |
| 6     | #95   | T3-02    | /forget reply command                              | P0  | M    | T3-01 |
| 7     | #96   | T3-04    | Cascade worker skeleton                            | P0  | L    | T3-01 |
| 8     | #97   | T3-05    | Reimport tombstone prevention                      | P0  | M    | T3-01, T2-01 |
| 9     | #98   | T2-NEW-C | Reply resolver service                             | P1  | M    | T2-01 |
| 10    | #99   | T2-02    | Dry-run duplicate / policy stats                   | P1  | M    | T2-01, T1-12 (done), T2-NEW-C |
| 11    | #100  | T2-NEW-D | Tombstone collision dry-run report                 | P1  | S    | T2-02, T3-01 |
| 12    | #101  | T2-NEW-E | Apply checkpoint / resume                          | P0  | M    | T2-01 |
| 13    | #102  | T2-NEW-F | Apply rate limit + chunking                        | P1  | S    | T2-NEW-E |
| 14    | #103  | T2-03    | Import apply with synthetic updates                | P1  | XL   | many (see issue) |
| 15    | #104  | T2-NEW-G | Logical rollback per ingestion_run                 | P1  | M    | T2-03 |
| 16    | #105  | T3-03    | /forget_me skeleton                                | P1  | L    | T3-01 |
| 17    | #106  | T2-NEW-H | Edit history policy doc                            | P2  | S    | T2-NEW-A |

**Sprint progress (Stream Charlie):**
- #92 T3-01 — DONE (commit `8c5d983`, see Phase 2 — Importer + governance skeleton table above)

Three parallel tracks possible from day 1 (no shared deps):
- Track A: #89 helper (unblocks downstream)
- Track B: #91 schema doc → #93 user mapping → #94 parser
- Track C: #92 forget_events → #95/#96/#97/#105 (all parallel after #92)

### Phase 2 — Stream Bravo progress

| Issue | Ticket   | Status | Notes |
|-------|----------|--------|-------|
| #91   | T2-NEW-A | done   | Sprint Bravo-01 / PR #TBD. Two-commit branch (286b46a + 3f691bc). New `docs/memory-system/telegram-desktop-export-schema.md` (10 sections: envelope, message envelope, message_kind taxonomy + mixed-array text form, edit history, reply/forward fields, identity (anonymous channel), media references, #offrecord governance quote from AUTHORIZED_SCOPE.md, schema versioning, out-of-scope cross-refs). Three anonymized fixtures under `tests/fixtures/td_export/`: `small_chat.json` (6 msgs incl. mixed-array text edge case), `edited_messages.json` (5 msgs with both #nomem and #offrecord), `replies_with_media.json` (8 msgs with A→B→C reply chain, anonymous channel post, dangling reply for #98). 12 stdlib-only tests in `tests/fixtures/test_td_export_fixtures.py` (all pass). Unblocks #93, #94, #98, #99, #103, #106. |
| #106  | T2-NEW-H | done   | Sprint Bravo-03 / PR #TBD. New `docs/memory-system/import-edit-history.md` + binding-rule append to `AUTHORIZED_SCOPE.md` under "Telegram import rule". Decision: imported messages get `imported_final=TRUE` marker on `message_versions` row (denormalised provenance; FK chain `raw_update_id → telegram_updates.ingestion_run_id` is audit trail). Schema/migration deferred to #103. |
| #93   | T2-NEW-B | done   | Sprint Bravo-02 / PR #TBD. New `bot/services/import_user_map.py` + 17 tests + alembic migration 015 (`users.is_imported_only` flag, sparse partial index) + new doc `docs/memory-system/import-user-mapping.md`. Three cases (known/unknown/anonymous channel) with privacy R2 mitigation: imports cannot promote themselves to live; only the gatekeeper live-registration path flips ghost→live (via `UserRepo.upsert` clearing `is_imported_only`). Display_name first-write-wins; negative export-id tails rejected (parser hardening); attribution semantics under live/import overlap explicit (imported messages permanently attributed to live member's row when overlap occurs). |
| #94   | T2-01    | done   | Sprint Bravo-04 / PR #TBD (commit `e3bd259`). New `bot/services/import_parser.py::parse_export(path) -> ImportDryRunReport` (frozen dataclass, NO content fields by construction — `asdict(report)` carries zero message bodies). New `bot/cli.py` with `import_dry_run` subcommand (`python -m bot.cli import_dry_run <path>`). Hard-rejects full-account exports (top-level `chats:[]`); accepts single-chat envelopes only. Tolerant reader: malformed individual messages, missing optional fields, and unrecognised chat types accumulate in `parse_warnings` and the parse continues; hard-fails only on missing `messages[]`, unparseable JSON, or full-account envelope. Calls `bot.services.governance.detect_policy(text, caption)` per user message (service messages skipped) — same regex live ingestion uses, so dry-run governance verdict matches the future #103 apply outcome. Media messages route TD `text` field to `caption` arg per schema §3.1. Report fields: envelope identity (chat_id/name/type), counts (total/user/service/media/forward/edited/anonymous-channel/replies/dangling-replies), `distinct_users` + `distinct_export_user_ids` (preview ghost-creation volume per #93), `date_range_start/end`, `duplicate_export_msg_ids`, `message_kind_counts`, `policy_marker_counts` (`{normal, nomem, offrecord}`), `parse_warnings`. New doc `docs/memory-system/import-dry-run-parser.md` covers operator pre-flight role, NO-content guarantee, governance contract, CLI usage, out-of-scope (no DB writes, no LLM, no reply resolution, no tombstone collision detection — those are #98/#99/#100/#103). Unblocks #97 (T3-05 reimport tombstone prevention), #98 (T2-NEW-C reply resolver), #99 (T2-02 dry-run stats), #103 (T2-03 import apply). |
| #98   | T2-NEW-C | done   | Sprint Bravo-05 / PR #131 (commits `a1feec0` initial + `f79b733` review fixes + `851206f`/`8c4e76d` codex re-review nits). New `bot/services/import_reply_resolver.py::resolve_reply(session, export_msg_id, ingestion_run_id, *, chat_id) -> ReplyResolution` and `resolve_reply_batch(session, export_msg_ids, ingestion_run_id, *, chat_id) -> dict[int, ReplyResolution]` map Telegram Desktop export message ids to `chat_messages.id`. Resolution priority: **same_run** → **prior_run** (`started_at < current.started_at`, tie-breaker `id DESC`) → **live** → **unresolved**. `chat_id` scopes every lookup; defense-in-depth `ChatMessage.chat_id == chat_id` on every join. Batch path issues at most 4 DB queries regardless of N (one per priority level + NULL-fallback for legacy live rows). Frozen dataclasses `ReplyResolution` and `ReplyResolverStats`; `aggregate_resolutions` raises `ValueError` on unknown `resolved_via`. Forward-chain semantics: direct-lookup only, `chain_depth` always 0; consumers iterate via `chat_messages.reply_to_message_id` if deeper traversal needed. Read-only invariant: zero DB writes. #99 dry-run consumers create synthetic `IngestionRun(run_type='dry_run')` for scope. Tests: 11 (10 DB-backed + 1 offline). Shared by #99 / #103. New doc `docs/memory-system/import-reply-resolver.md`. |
| #99   | T2-02    | done   | Sprint Bravo-07 (B5a) / PR #136 (commits `450ea5d` initial + `9e166e1` doc + `78be911` codex round-1 fixes + `6e36df3` CI asyncio.run conflict fix). DB-aware extension to the T2-01 dry-run parser, surfacing pre-flight duplicate / governance / broken-reply counts so operators can audit a Telegram Desktop export against the live DB before authorising #103 apply. New `bot/services/import_dry_run.py::parse_export_with_db(path, session, chat_id) -> ImportDryRunReport` runs the offline `parse_export(path)` (T2-01) first, then issues a small fixed number of DB queries to compute three new fields on `ImportDryRunReport`: `db_duplicate_count`, `db_duplicate_export_msg_ids`, `db_broken_reply_count`. All three default to `0` / `[]` on the offline path so existing #94 callers are backwards compatible. `bot/cli.py` extended with `--with-db` flag on the existing `import_dry_run` subcommand printing exact operator summary: `"N duplicates would be skipped, M offrecord messages, K nomem, J broken reply chains."` **Synthetic dry_run IngestionRun**: scopes #98 reply resolver without writing real import data; rolled back with fixture transaction in tests. Codex fixes: CRITICAL intra-export targets no longer counted as broken (filter via `all_export_ids` set before `resolve_reply_batch`); MEDIUM multiplicity semantics (count messages, not unique target ids); MEDIUM CLI exact-format assertion. Tests: 11 in `tests/services/test_import_dry_run_stats.py` (1 offline + 10 DB-backed). Extends `docs/memory-system/import-dry-run-parser.md` with DB-aware mode section. Shared by #103. |
| #102  | T2-NEW-F | done   | Sprint Bravo-08 / PR #137 (commits `138b502` initial + `3f63119` doc + `02baa7b` codex round-1 (5 findings) + `8287e4a` codex round-2 doc nits). Apply chunking + rate limit + advisory lock config — small infrastructure ticket layered on top of #101 checkpoint/resume; the actual apply path still lives in #103 (Stream Delta). New `bot/services/import_chunking.py` with: (1) frozen `ChunkingConfig` dataclass (fields: `chunk_size: int`, `sleep_between_chunks_ms: int`, `use_advisory_lock: bool`; validates `chunk_size in [1, 10000]` and `sleep_between_chunks_ms in [0, 60000]` in `__post_init__`); (2) `load_chunking_config(env=None) -> ChunkingConfig` reads env vars `IMPORT_APPLY_CHUNK_SIZE` (default 500), `IMPORT_APPLY_SLEEP_MS` (default 100), `IMPORT_APPLY_ADVISORY_LOCK` (default true); (3) `acquire_advisory_lock(connection: AsyncConnection, ingestion_run_id)` async context manager — takes `AsyncConnection` (NOT `AsyncSession`) because `pg_advisory_lock` is connection-scoped; caller MUST hold the connection for the full lock lifetime; deterministic `lock_id` derived via `_derive_lock_id(ingestion_run_id)` (SHA-256 of 8 big-endian bytes of ingestion_run_id → first 8 bytes → signed int64); emits `SELECT pg_advisory_lock(:id)` on enter and `SELECT pg_advisory_unlock(:id)` in `finally` with a `WARNING` log if unlock returns false; PG session-level locks are STACKED — callers MUST NOT re-enter on same connection. (4) `_derive_lock_id` is pure / deterministic. `bot/cli.py` extended: `import_apply` subcommand applies `--chunk-size` CLI override BEFORE calling `load_chunking_config` (via `os.environ.copy()` + key injection) so invalid `IMPORT_APPLY_CHUNK_SIZE` env does not block a valid `--chunk-size` arg; `ValueError` from config is caught and returns exit 2 with operator-readable message. Tests: 25 in `tests/services/test_import_chunking.py` — 23 offline + 2 DB-backed (skipped locally without postgres). Codex review fixes: CRITICAL advisory lock now takes `AsyncConnection`; MEDIUM CLI override timing fixed; MEDIUM `ChunkingConfig.__post_init__` validation added; MEDIUM release-on-exception test now verifies from SEPARATE connection; LOW re-entry stacked semantics documented. New doc `docs/memory-system/import-chunking.md` updated with connection-scope requirement, stacked semantics, `AsyncConnection` usage pattern. |
| #101  | T2-NEW-E | done (HIGH-RISK) | Sprint Bravo-06 / PR #134 (commits `000a1c2` initial + `5568487` hash-mismatch + `476a45d` codex round-1 (12 findings) + `55de8c8` codex round-2 (5 findings) + `890c0b0` migration 016→017 alembic head conflict resolution). Apply checkpoint / resume infrastructure for `import_apply` — infrastructure-only ticket; the actual apply path lives in #103 (Stream Delta). New `bot/services/import_checkpoint.py` with `init_or_resume_run` / `save_checkpoint` / `load_checkpoint` / `finalize_run` + frozen `Checkpoint` / `ResumeDecision`. Resume decision matrix: no prior → fresh; completed prior → fresh; running/failed without `--resume` → block (CLI exit 3); running/failed with `--resume` → resume; mismatched `source_hash` → block. `save_checkpoint` writes via atomic `UPDATE ingestion_runs SET stats_json = COALESCE(stats_json::jsonb, '{}'::jsonb) \|\| CAST(:patch AS jsonb) WHERE id = :id` (CAST form required under SQLAlchemy 2.0 `text()`); commits immediately so concurrent CLI invocations see the running row. `_create_fresh_run` wraps INSERT in `begin_nested()` SAVEPOINT so IntegrityError on partial unique index doesn't poison outer tx. Alembic migration `017_ingestion_runs_partial_unique_running.py`: adds `source_hash VARCHAR(128)` nullable + partial UNIQUE index `ON (source_hash) WHERE status='running'` (CONCURRENTLY in autocommit block). `bot/cli.py` extended with `import_apply <path> [--resume] [--chunk-size N]` subcommand; streams sha256 in 1 MB chunks; lazy `run_apply` import → exit 4 until #103 lands. Exit codes: 0 / 2 / 3 / 4. Tests: 13 (10 DB-backed + 3 offline CLI smoke); test 13 is infrastructure-level proxy for the deferred 50%-kill integration test (real apply doesn't exist yet — #103 will add cross-process test). HIGH-RISK governance: deep-implementer Opus, dual verifier (deep-analyst ACCEPT + ag-reviewer partial-confirm), Codex xhigh 3 rounds (REQUEST_CHANGES round 1+2, APPROVE round 3 — closed 2 CRITICAL [JSON\|\|JSONB cast bind, failed-status-flow] + 4 HIGH [SAVEPOINT scope, save_checkpoint commit contract, finalize_run on aborted tx, run-not-committed-before-apply] + 2 MEDIUM [bool-as-int, streaming sha256] + 2 LOW). Cross-stream boundary: `bot/services/import_apply.py` MUST NOT exist on this branch (Stream Delta #103 owns it). New doc `docs/memory-system/import-checkpoint.md` (decision matrix, partial unique index, deferred-test rationale, Operator Playbook with cancellation SQL, cross-references). Unblocks #102 (T2-NEW-F rate limit + chunking) and is required infrastructure for #103. |

### Phase 2 — Stream Charlie progress

| Issue | Ticket | Status | Notes |
|-------|--------|--------|-------|
| #92   | T3-01  | done   | Foundation: forget_events table + repo. Sprint Charlie-01 / PR #122. 5 commits on main: `af29b25` feat (initial), `d22185f` race safety + JSONB + ordering fix (Codex round 1), `2cde618` identity-map fix in mark_status fallback (Codex round 2), `f8549b8` docs DONE, `825e6e2` stale Branch header replacement. Alembic migration `014_add_forget_events.py`. New `bot/db/repos/forget_event.py` with race-safe `create()` (INSERT ... ON CONFLICT (tombstone_key) DO NOTHING RETURNING + fallback SELECT), `get_by_tombstone_key`, `list_pending` (FIFO `(created_at ASC, id ASC)`), `mark_status` (atomic `UPDATE ... WHERE status IN (allowed_old) RETURNING`; pending → processing → {completed | failed}; terminal states are dead-ends). 14 tests in `tests/db/test_forget_event_repo.py`. 4 rounds Codex review (HIGH×2 race safety + MEDIUM JSONB + LOW ordering + HIGH identity-map fix). |
| #95   | T3-02  | done   | Sprint Charlie-02 / PR #TBD. Two-commit branch (`1cf91fc` feat + `64bc707` review fixes). New `bot/handlers/forget_reply.py` (~150 lines): Router `/forget` reply-only command. GroupChatFilter + COMMUNITY_CHAT_ID guard. Authz per HANDOFF §10: `is_author OR is_admin`. `_find_chat_message` uses `.with_for_update()` (race-safe — closes Codex round-1 HIGH on TOCTOU between authz read + tombstone write). Creates `forget_event` via `ForgetEventRepo.create` (idempotent on tombstone_key `message:<chat_id>:<message_id>`). target_id = chat_message.id (DB row PK). authorized_by = 'self' (author) or 'admin'. `bot/__main__.py` (+2 lines additive): import + router register before chat_messages catch-all. Tests: 10 (5 mock + 3 DB-backed using db_session fixture + 2 branch coverage for no-reply usage hint and replied-to-unknown silent return). Reviews: deep-analyst PASS, Claude product ACCEPTED (3 LOW forward-looking notes), Codex round 2 APPROVE (after round-1 fixes for HIGH TOCTOU + MEDIUM mock-only tests + LOW missing branches). |
| #96   | T3-04  | done   | Sprint Charlie-03 / PR #TBD. Three-commit branch (`2dda016` initial + `688fc26` CRITICAL fix + `1b5a07d` uniformity fix). HIGH-RISK skeleton — irreversible content destruction. New `bot/services/forget_cascade.py` (~300 lines): `run_cascade_worker_once(session, batch_size=10)` + `cascade_worker_tick()` production wrapper. Cascade order matches HANDOFF §10 EXACTLY: chat_messages → message_versions → message_entities → message_links → attachments → fts_rows. Per-event try/except, per-layer checkpoint via new `ForgetEventRepo.update_cascade_status` (atomic `UPDATE ... WHERE id=? AND status='processing' RETURNING` — Option A architecture; preserves `mark_status` state machine semantics). Restart-safe: skips layers with `status='completed'`. Cascade content semantics: `target_type='message'` NULLs text/caption/raw_json + sets is_redacted=True + memory_policy='forgotten' on chat_messages; NULLs text/caption/normalized_text/entities_json + sets is_redacted=True on message_versions; content_hash PRESERVED per ADR-0003 (citation stability). `target_type='user'` walks ALL chat_messages WHERE user_id = CAST(target_id AS BIGINT); message_versions cascaded via subquery on chat_messages.user_id (resolves correctly after layer 1 NULLs). `target_type ∈ {message_hash, export}` recorded as `{status: 'skipped', reason: 'target_type_not_supported_yet'}` per layer; event finalizes as `completed`. Phase 4+ layers (entities/links/attachments/fts_rows) recorded as `{status: 'skipped', reason: 'table_not_exists'}` for supported target_types. `bot/services/scheduler.py` (+16 lines additive): APScheduler interval 30s, `max_instances=1, coalesce=True, misfire_grace_time=60`, gated by feature flag `memory.forget.cascade_worker.enabled` (default OFF). Worker tick reads flag EVERY fire (not cached). Tests: 12 cascade + 19 repo = 31 new; full suite 274 passed (post-rebase). Reviews: deep-analyst PASS, ag-reviewer PASS, Claude product-reviewer ACCEPTED, Codex 3-round (FAIL → REQUEST_CHANGES → APPROVE — closed CRITICAL target_type fallthrough + MEDIUM concurrent test gap + LOW failed-state test gap + reason field uniformity). |

| #105  | T3-03  | done   | Sprint Charlie-04 / PR #TBD. Two-commit branch (`bb5ba77` feat + `10ad5c2` review fixes). New `bot/handlers/forget_me.py` (~96 lines): Router `/forget_me`, no GroupChatFilter (DM + in-chat both allowed). Resolves `UserRepo.get` by tg_id; if None → silent return (privacy — no leak that user is unknown). Counts `chat_messages WHERE user_id = X` BEFORE creating event (info shown back to user). Creates `forget_event` with `target_type='user'`, `target_id=str(user.id)` (== telegram_id since `User.id == telegram_id` in this codebase), `tombstone_key='user:{tg_id}'`, `authorized_by='self'`. Idempotency: second `/forget_me` returns the SAME event id (verified by test), no duplicate row. `bot/__main__.py` (+1 line additive, alphabetical): `forget_me` import + router register before `forget_reply`. Tests: 8 (5 mock + 3 DB-backed using db_session fixture). Reviews: deep-analyst PASS, Claude product ACCEPTED (4 LOW forward-looking notes), Codex round 2 APPROVE (after round-1 fixes for MEDIUM ruff lint + LOW idempotency same-id assertion + LOW unknown-user privacy `assert_not_awaited()`). |

### Phase 2 — Stream Alpha progress (Phase 1 cleanup chain)

| Issue | Status | Notes |
|-------|--------|-------|
| #67   | done   | Sprint Alpha-01 / PR #120. Two-commit branch (`11e80df` feat + `cec051f` review fixes). Closes the `MessageRepo.save` "stale `memory_policy` on duplicate delivery" bug flagged on PR #63. Implements recommended fix combo (1)+(3) plus defensive (2): (1) alembic migration `013_offrecord_marks_unique_partial.py` adds partial UNIQUE INDEX `ix_offrecord_marks_chat_message_id_mark_type ON offrecord_marks (chat_message_id, mark_type) WHERE chat_message_id IS NOT NULL` with a one-shot pre-create DELETE-by-min(id) guard against pre-existing duplicates from the T1-13→#67 bug window; (3) `MessageRepo.save` switches to `ON CONFLICT DO UPDATE SET memory_policy=EXCLUDED.memory_policy, is_redacted=EXCLUDED.is_redacted` ONLY for the policy fields the caller explicitly passes (immutables `text`/`caption`/`raw_json`/`date`/`user_id`/etc. never appear in `set_clause`); legacy callers passing both policy args as `None` retain the original `ON CONFLICT DO NOTHING + SELECT` semantics (no `NULL`-clobber); (2) `OffrecordMarkRepo.create_for_message` becomes idempotent via `pg_insert(...).on_conflict_do_nothing(index_elements=['chat_message_id','mark_type'], index_where=text("chat_message_id IS NOT NULL")).returning(...)` + `SELECT` fallback so redelivery is a true no-op (no duplicate audit rows, no `IntegrityError`). New tests: 5 in `tests/db/test_message_repo.py` (refresh-policy on dup, both-None preserves existing, only-policy doesn't clobber `is_redacted`, irreversibility extended to assert `caption`+`raw_json` immutable, `is_redacted` flip-back guard) + 1 in `tests/db/test_offrecord_mark_repo.py` (repo idempotency) + 1 in `tests/handlers/test_chat_messages_redelivery_idempotent.py` (handler-level integration: same `#offrecord` update fed twice → exactly 1 row in both `chat_messages` and `offrecord_marks`). Dual review: Codex tech `APPROVE`, Claude product `ACCEPTED`. CI: 4/4 green. Unblocks the `#67/#80/#81/#89 → persist_message_with_policy()` critical path. |
| #81   | done   | Sprint Alpha-03 / PR #130. Two-commit branch (`08bf88c` feat schema + savepoint + 2 new tests + `aace220` Codex+product LOW fixup: stale class docstring + monkeypatched savepoint-branch coverage test). Closes the gap Codex flagged on PR #75 (T1-14): `MessageVersionRepo.insert_version` docstring promised idempotency on `(chat_message_id, content_hash)` but only `(chat_message_id, version_seq)` had a UNIQUE constraint. Two concurrent identical edits could both bypass `get_by_hash` and one would raise `IntegrityError` propagating out of the handler under `DbSessionMiddleware`. Implementation: (1) **`bot/db/models.py::MessageVersion.__table_args__`** — added `UniqueConstraint("chat_message_id", "content_hash", name="uq_message_versions_chat_message_content_hash")` alongside existing `uq_message_versions_chat_message_seq`. Class docstring rewritten to describe both DB-level idempotency (UNIQUE) and the savepoint+reselect repo pattern. (2) **`alembic/versions/016_add_message_version_content_hash_unique.py`** — `revision="016"`, `down_revision="015"`. `upgrade()` deduplicates pre-existing duplicates via `ROW_NUMBER() OVER (PARTITION BY chat_message_id, content_hash ORDER BY id) WHERE rn > 1` then `op.create_unique_constraint(...)`. `downgrade()` only drops the constraint (acceptable irreversibility for legacy duplicate cleanup). FK `chat_messages.current_version_id ON DELETE SET NULL` documented inline — dropped duplicate referents leave parent `current_version_id IS NULL` (acceptable; readers already tolerate this post-`forget` state). (3) **`bot/db/repos/message_version.py::insert_version`** — INSERT wrapped in `async with session.begin_nested()` savepoint. On `IntegrityError` (unique-constraint hit from concurrent loser), savepoint rolls back, `get_by_hash` reselects winner's row and returns it. If reselect returns None (defensive — should not happen under the new constraint), original `IntegrityError` re-raised so unrelated FK/NOT NULL errors propagate cleanly. (4) **Tests** in `tests/db/test_message_version_repo.py`: `test_insert_version_concurrent_same_hash_returns_existing` (get_by_hash short-circuit path) + `test_insert_version_integrity_error_path_reselects_existing` (idempotency on direct ORM pre-insert) + `test_insert_version_savepoint_branch_reselects_on_integrity_error` (load-bearing test that monkeypatches `MessageVersionRepo.get_by_hash` to lie once: returns `None` on first call to force `begin_nested()` + IntegrityError path, defers to real impl on reselect; asserts `call_count == 2` proving both savepoint entry and reselect actually executed). Reviews: deep-analyst PASS (verified all ACs against 120 local postgres tests), Claude product `ACCEPTED` (1 LOW nit on docstring already addressed), Codex tech round-1 `REQUEST_CHANGES` (2 LOW: stale docstring + uncovered savepoint branch) → round-2 `APPROVE` after `aace220`. CI: 4/4 green. Unblocks #89 and the `persist_message_with_policy()` critical path. |
| #89   | done (partial — chv2 + edited_message migration deferred to #132) | Sprint Alpha-04 / PR #133. Four-commit branch (`7641c5d` feat helper + tests + `f6a23ab` broaden detect_policy with kwargs + `0af3a1f` migrate chat_messages.py to helper + `7f76b70` ruff lint cleanup). Closes Phase 2 readiness gap flagged by Codex final Phase 1 verification: introduces single `bot.services.message_persistence.persist_message_with_policy()` helper that the live new-message handler AND the future #103 import-apply both call, ending the chat_messages.py vs edited_message.py asymmetry from #68 for the new-message path. Implementation: (1) **NEW `bot/services/message_persistence.py`** — `PersistResult` frozen dataclass (chat_message, policy, is_offrecord_mark_created) + `persist_message_with_policy(session, message: Any, *, raw_update_id: int | None = None, source: Literal["live","import"] = "live") -> PersistResult`. Internal flow: advisory_lock_chat_message (sprint #80 invariant — FIRST DB op) → extract_normalized_fields → detect_policy (broadened) → policy-gated content fields (offrecord nulls text/caption/raw_json + is_redacted=True; else preserved with raw_json conditional on `message.text` truthiness, exactly mirroring old chat_messages.py:64-78 byte-for-byte) → MessageRepo.save (sticky CASE preserved) → conditional OffrecordMarkRepo.create_for_message. Accepts `Any` for message (aiogram Message OR importer-shaped duck via SimpleNamespace); falls back gracefully on missing `model_dump`. (2) **`bot/services/governance.py::detect_policy` broadened** with keyword-only kwargs `poll_question`, `contact_name`, `forward_text`, `forward_caption` (all default None — backward compatible with Stream Bravo's `bot/services/import_parser.py:239` and `bot/services/ingestion.py:146` 2-arg positional calls). All 6 fields scanned with offrecord > nomem precedence; `mark_payload` extended with `in_poll_question`/`in_contact_name`/`in_forward_text`/`in_forward_caption` keys when respective field carries the tag. (3) **`bot/handlers/chat_messages.py` refactored** from ~110 LOC to ~53 LOC: handler-only guards (community chat ID, group filter, `from_user is None` early-exit) preserved at top, then `UserRepo.upsert` (gatekeeper-era invariant), then single `persist_message_with_policy(session, message)` call. Sprint #67 redelivery idempotency, Sprint #80 sticky CASE, Sprint #80 advisory lock, Sprint #81 savepoint+reselect — all preserved through the helper's call to `MessageRepo.save`. Tests: 11 helper unit tests in `tests/services/test_message_persistence.py` (text-normal/nomem/offrecord, caption-only photo normal/offrecord, importer SimpleNamespace duck, advisory-lock ordering, raw_update_id passthrough, source="import" smoke); 4 new tests in `tests/services/test_governance_stub.py` for kwargs (offrecord-in-poll, nomem-in-contact, offrecord-in-forward_text, offrecord-precedence-across-fields); 1 NEW DB-backed `tests/handlers/test_chat_messages_helper_path.py` (poll-with-offrecord end-to-end through handler — verifies broadened detection works end-to-end); cosmetic update to `tests/test_chat_messages_no_auto_member.py` (patch target moved from handler.MessageRepo to message_persistence.MessageRepo + advisory-lock patch — behavioral assertions preserved). All 12 existing detect_policy tests unchanged and passing. **Scope reduced** from issue body: chv2 content_hash recipe broadening + content_hash_version schema migration + edited_message.py migration to helper → deferred to follow-up issue #132 (kept this sprint at M size, isolated risk). `forward_text`/`forward_caption` kwargs are wired through helper and governance but always pass `None` from live handler today (forward content already lives in text/caption columns; placeholder for future expansion). Reviews: deep-analyst PASS (39/39 tests on local postgres, all ACs verified), Claude product `ACCEPTED` (3 non-blocking suggestions), Codex tech `APPROVE`. CI: 4/4 green on `7f76b70`. Closes the helper portion of #68 handler asymmetry (chat_messages.py path unified — edit handler still TBD via #132). Phase 2 R4 binding rule (HANDOFF.md:1010) "import apply MUST call persist_message_with_policy()" now achievable. **STREAM ALPHA COMPLETE** after this merge. |
| #80   | done   | Sprint Alpha-02 / PR #123. Six-commit branch (`f9fa2f8` feat advisory lock + `a1284df` Codex CRITICAL sticky-policy fix + `01716ac` ORM identity-map populate_existing fix + `9e58dcb` Codex CRITICAL #1 sticky offrecord in edit handler + `eaeed87` Codex HIGH #2 backfill per-row lock + `b3b0d80` Codex MEDIUM #3 end-to-end sticky regression). HIGH-RISK (M, privacy invariant). Closes the TOCTOU race in the offrecord-policy invariant across THREE attack surfaces: (a) duplicate ORIGINAL delivery (polling glitch / restart re-delivery) → fixed by sticky CASE on `MessageRepo.save`; (b) edit-revert (user removes `#offrecord` tag in a subsequent edit) → fixed by sticky guard in `edited_message` handler; (c) backfill stale-read race (concurrent flip during v1 backfill) → fixed by per-row advisory lock + FOR UPDATE re-read. Implementation: (1) **`bot/db/locks.py`** (NEW) — `advisory_lock_chat_message(session, chat_id, message_id)` helper emits `SELECT pg_advisory_xact_lock(hashtext(:k))` with key `chat_msg:{chat_id}:{message_id}`; auto-released at tx end. Wired into `bot/handlers/chat_messages.py` (after `from_user is None` early-exit, before any chat_messages DB ops), `bot/handlers/edited_message.py` (before `_find_chat_message`), and `bot/services/backfill.py` (per-row, before re-read). `bot/services/ingestion.py` not touched (writes only `telegram_updates`, no chat_messages access). (2) **Sticky-policy CASE expressions in `bot/db/repos/message.py::MessageRepo.save`** — ON CONFLICT DO UPDATE SET uses `case((ChatMessage.memory_policy == 'offrecord', 'offrecord'), else_=insrt.excluded.memory_policy)` for `memory_policy` (offrecord sticky — never downgradable) and `case((ChatMessage.is_redacted.is_(True), True), else_=insrt.excluded.is_redacted)` for `is_redacted` (True sticky — once redacted, stays redacted). Selectivity from #67 preserved. Legacy DO-NOTHING + SELECT path uses `with_for_update(key_share=False)` (FOR UPDATE) for row-level lock. (3) **`execution_options={"populate_existing": True}`** on the upsert `session.execute()` — REQUIRED because server-side CASE evaluation can produce a value different from the EXCLUDED value passed in (e.g. upgrade `normal→offrecord` goes through, downgrade `offrecord→normal` is blocked). Without this flag, SQLAlchemy's identity map would return a cached ORM instance and silently drop RETURNING-clause attributes. (4) **Sticky offrecord in `bot/handlers/edited_message.py`** — when `old_policy == 'offrecord'`, the edit is treated as no-op for policy AND `is_redacted_version` is forced True regardless of detected `new_policy`, so any `message_versions` row inserted for the edit uses redacted state (text=None, caption=None, redacted-state hash). Defense-in-depth early-return added to `_update_memory_policy` helper: refuses to mutate `chat_messages.memory_policy` if the row is already offrecord. (5) **Per-row protection in `bot/services/backfill.py`** — for each row in batch, take `advisory_lock_chat_message` then re-read with `select(ChatMessage).where(id=msg.id).with_for_update(key_share=False)`; skip if `current_version_id` was concurrently filled (T1-06 live path); build `MessageVersion` from post-lock fresh values. Backfill is now race-free by construction; safe to run with live ingestion. New tests: 3 sticky regression in `tests/db/test_message_repo.py` (downgrade-resistance: both-args, only-`memory_policy`, only-`is_redacted`) + 3 end-to-end scenarios in `tests/integration/test_offrecord_irreversibility.py` (exact Codex CRITICAL trace, idempotent offrecord→offrecord, allowed normal→offrecord upgrade) + 1 new end-to-end DB-backed `test_handler_offrecord_flip_then_attempted_revert_stays_sticky_end_to_end` covering Codex MEDIUM #3 + 4 unit tests in `tests/db/test_locks.py` + 4 state-based ordering tests in `tests/integration/test_chat_messages_toctou.py` + 4 new handler tests in `tests/handlers/test_edited_message.py` (offrecord→normal no-op, offrecord→nomem no-op, offrecord caption-only edit no-op, `_update_memory_policy` helper refuses downgrade) + 1 inverted (`test_edit_offrecord_to_normal_flip_no_restoration` → `test_edited_message_offrecord_to_normal_is_noop_for_policy_and_content`) + 3 new state-based race tests in `tests/db/test_v1_backfill.py` (advisory-lock-per-row, skip-on-concurrent-fill, fresh-row-for-hash-and-version). Updated test `test_save_duplicate_with_only_policy_normal_does_not_unflip_redacted` for post-Sprint-#80 sticky semantics. HIGH-RISK protocol: 2 verifiers (deep-analyst + ag-reviewer) PASS, Codex tech REQUEST_CHANGES on `01716ac` resolved by 3 fixup commits, Codex re-review APPROVE on final HEAD. CI: 4/4 green. Privacy invariant `offrecord` sticky is now enforced across THREE code paths (`MessageRepo.save` + edit handler + backfill) — no live code path can downgrade it. Unblocks #81, #89. |

### Phase 2 — Stream Delta progress

| Issue | Ticket | Status | Notes |
|-------|--------|--------|-------|
| #97   | T3-05  | done   | Sprint Delta-01 / PR #TBD. Single-commit branch (`a51d19f` feat). HIGH-RISK privacy gate (HANDOFF.md §3 risk R1: "resurrection attack" — operator forgets, importer resurrects). New `bot/services/import_tombstone.py` with two functions: (1) `check_tombstone(session, *, chat_id, message_id, content_hash, user_tg_id) -> ForgetEvent | None` — returns FIRST active tombstone matching any of three keys in priority order: `message:{chat_id}:{message_id}` → `message_hash:{content_hash}` (skipped if content_hash is None) → `user:{user_tg_id}` (skipped if user_tg_id is None). Status NOT filtered: failed-status forget_events STILL block (privacy hardening — failed cascade means content may still be live, even MORE dangerous than completed). Delegates to `ForgetEventRepo.get_by_tombstone_key` (frozen Charlie API). (2) `record_tombstone_skip(stats_json, *, matched_key, matched_status, forget_event_id, export_message_id, chat_id) -> dict` — appends entry to `stats_json['skipped_tombstones'][]`; immutable approach (`copy.deepcopy` input; returns new dict; caller persists via `IngestionRunRepo.update_status`). 7 DB-backed tests in `tests/services/test_import_tombstone.py` covering: no-tombstone passthrough, message-key hit, message_hash-key hit (uses `compute_content_hash` chv1 verbatim), user-key hit, message-over-user precedence, failed-status still blocks (transitions pending → processing → failed via mark_status), record_tombstone_skip append + mutation safety. T2-03 (#103) import apply path does NOT exist on main yet — this sprint delivers the lookup helper + tests, ready to be wired in by #103. Reviews: Claude product-reviewer (opus) ACCEPTED (3 LOW carry-over for #103: top-level skipped_total counter optional; anonymous-channel ghost user_tg_id contract for #103; explicit content_hash assertion for cross-chat dedup), Codex tech (xhigh) APPROVE (1 LOW: test 5 priority chain doesn't exercise message_hash middle path — non-blocking, documented as carry-over). Full suite: 379 passed (post-rebase on origin/main with #98 reply resolver). ruff clean. **STREAM DELTA COMPLETE** after this merge. |
| #100  | T2-NEW-D | done (merged-on-branch / ready-to-merge) | Sprint Delta-02 / PR #TBD. Tombstone collision stats added to dry-run report (`tombstone_skip_count`, `tombstone_skip_export_msg_ids`); separate from duplicate bucket; tombstone-wins precedence (tombstone checked before duplicate; a message matching both is counted only as tombstone skip, not duplicate). DB-aware mode only (`parse_export_with_db` / `--with-db`); offline path returns defaults (0 / []). Extends `ImportDryRunReport` frozen dataclass (new fields default 0 / [] — backwards compatible). CLI operator summary extended: `"P messages blocked by tombstone (forget event)."` Extends `docs/memory-system/import-dry-run-parser.md` with tombstone collision section. Deps: #97 (tombstone helper `check_tombstone`), #99 (T2-02 dry-run DB-aware stats), #94 (T2-01 dry-run parser). Cross-refs: #103 import apply must honour tombstone-wins ordering when calling `check_tombstone`. |
| #103  | T2-03  | done (HIGH-RISK, ready-to-merge) | 2026-04-28 / Sprint Delta-03. Phase 2 finale: `bot/services/import_apply.py::run_apply` applies Telegram Desktop exports through synthetic `telegram_updates` (`update_id=NULL`, `ingestion_run_id=<run>`), tombstone-before-duplicate ordering, #93 user mapping, #98 reply resolver, explicit governance gate, and `persist_message_with_policy()` as the only non-offrecord `chat_messages` writer. `offrecord` keeps only the synthetic audit row and does NOT call the persist helper. `MessageVersionRepo.insert_version(imported_final=True)` implements #106 with Alembic migration `018_add_message_versions_imported_final.py`; live overlap skips. CLI `import_apply` gated by `memory.import.apply.enabled` default OFF; exit codes now 0 / 2 / 3 / 5. New doc `docs/memory-system/import-apply.md`. Tests: targeted `29 passed, 42 skipped`; full suite `255 passed, 197 skipped` (local, timeout enforced via perl because GNU `timeout` is unavailable on macOS). Forward: #104 logical rollback by `ingestion_run_id`. |
| #104  | T2-NEW-G | done (Phase 2 final) | 2026-04-29 / Sprint Delta-04. New `bot/services/import_rollback.py::rollback_ingestion_run(session, ingestion_run_id) -> RollbackReport` deletes import-owned rows by FK chain only (`chat_messages.raw_update_id → telegram_updates.id → telegram_updates.ingestion_run_id`) with mandatory synthetic guard `telegram_updates.update_id IS NULL`; `message_versions` are counted before delete and removed via `ON DELETE CASCADE`. Single transaction: delete synthetic `telegram_updates` through the first CTE, delete import-owned `chat_messages` through the second CTE, insert audit `ingestion_runs(run_type='rolled_back', status='completed', stats_json.original_run_id=...)`, commit; any failure rolls back all deletes. Idempotency: per-run advisory lock + existing `rolled_back` audit row check; second invocation returns an idempotent report that echoes the prior audit row's delete counts (no further deletes performed) and reuses the audit row id. Race fallback wraps only the audit insert in a SAVEPOINT and catches `IntegrityError` to re-read the winning audit row (`1b97cc5`). Alembic `019_add_ingestion_runs_rolled_back.py` adds `rolled_back` to the run_type check and unique partial index on `stats_json->>'original_run_id'` for rollback audit rows; downgrade has an emergency preflight warning for existing rollback audit rows (`1c9af94`). CLI `rollback_ingestion_run <id>` exits 0 success/idempotent, 2 invalid run type, 3 not found, 4 downstream dependents. New doc `docs/memory-system/import-rollback.md`. Tests: 8 DB-backed scenarios cover small import rollback, idempotency, live-row protection, live-run rejection, unknown-run rejection, audit stats, atomic rollback on second-delete failure, and cascade count. **PHASE 2 CLOSED: 20/20 issues complete.** |

## Phase 2 Final Holistic Review hotfix (PR #143)

Merged 2026-04-29. Five findings identified by dual-review (Claude product reviewer + Codex
technical reviewer) over the closed Phase 2 surface. Commits landed on `main` as
`5002f26 fix(p2-fhr): H4 ...`, `e0ffd21`, `0fabc58 fix(p2-fhr): H1 — extract contact fields from contact_information dict`,
plus earlier merged C1 + H1 + H4 patches.

| ID | Severity | Status | Notes |
|----|----------|--------|-------|
| C1 | CRITICAL | FIXED   | `forgotten` is now sticky in `MessageRepo.save` (HANDOFF §1 invariant 9 strengthened). Once `memory_policy='forgotten'`, redelivery cannot downgrade. |
| H1 | HIGH     | FIXED   | `detect_policy` now scans `poll.question` + `contact_information.{first_name,last_name}`. Restores invariant 8: import path must produce the same governance verdict as the live path. |
| H2 | HIGH     | VERIFIED CLEAN | Per-message SAVEPOINT already in place in apply path; partial apply on per-message failure is intentional and matches the resume contract. No code change. |
| H3 | HIGH     | VERIFIED CLEAN | Logical rollback already deletes synthetic orphan `telegram_updates` rows along with their `chat_messages` via the FK chain. No code change. |
| H4 | HIGH     | FIXED   | Cascade worker now wraps each per-layer write in its own SAVEPOINT, preserving the restart-safe semantic (one bad layer cannot poison the rest of the event). |

## Deferred follow-ups (Phase 2.5 hotfix sprint)

Out-of-scope for Phase 2 closure but logged as named follow-ups. Tracked outside the Phase 2
ticket grid; will be picked up before or alongside Phase 4 work.

- **#132** — migrate `bot/handlers/edited_message.py` to use `persist_message_with_policy`
  helper (write-path uniformity; closes the second half of the #68 asymmetry).
- **CC-2** — anonymous-channel singleton tombstone semantics docs gap (clarify how
  `target_type='user'` interacts with the anonymous channel ghost user).
- **CC-3** — `cancel_ingestion_run` CLI subcommand (operator ergonomics; today operators
  cancel via SQL).
- **CC-5** — `/forget_me` defensive guard around unguarded `from_user.id` access (privacy
  hardening; current path is correct under aiogram contract but lacks an explicit None
  guard).

## Phase 11 — Shkoderbench / evaluation harness — **CLOSED 2026-05-11**

11 PRs merged (Sprint 0 + Wave 1 + Wave 2 round 1 + W2-04 baseline freeze).

**Sprint 0 — plan ratification:**
- PR #173 — `PHASE11_PLAN.md` canonical + draft reconciliation + REGISTRY/ROADMAP updates

**Wave 1 — eval harness skeleton (7/7):**

| ID    | Title                                                            | PR    | Status |
|-------|------------------------------------------------------------------|-------|--------|
| T11-W1-01 | `bot/services/eval_runner.py` skeleton                       | #193  | done   |
| T11-W1-02 | `bot/services/eval_seeds.py` (SeedSpec loader)               | #202  | done   |
| T11-W1-03 | `bot/services/eval_metrics.py` (recall@K/precision@K)        | #194  | done   |
| T11-W1-04 | `tests/fixtures/golden_recall/seed_v1` + `tests/evals/conftest.py` | #196 | done |
| T11-W1-05 | `test_determinism.py` + `test_recall_precision.py` smoke + `test_no_llm_imports.py` AST + conftest `loop_scope='class'` | #205 | done |
| T11-W1-06 | `.github/workflows/evals.yml` (gated) + `eval-results-schema.md` | #192 | done |
| T11-W1-07 | `.github/workflows/lint-privacy.yml` + allowlist script + precommit hook proposal | #195 | done |

**Wave 2 round 1 — privacy + correctness binding (3/3):**

| ID    | Title                                          | PR    | Status |
|-------|------------------------------------------------|-------|--------|
| T11-W2-01 | `tests/evals/test_leakage.py` (L1–L5)      | #211  | done   |
| T11-W2-02 | `tests/evals/test_citations.py` (C1–C4)    | #208  | done   |
| T11-W2-03 | `tests/evals/test_refusal.py` (R1–R4)      | #216  | done   |

**Wave 2 closer (W2-04):**
- PR #217 — baseline_thresholds frozen in `seed_meta.yaml` (commit `bc98bbd`); REGISTRY §5 binding flipped to **ACTIVE since 2026-05-11**; ROADMAP row 11 → DONE; CLAUDE.md narrative; `lint_privacy_check.sh` allowlist extended for root `CLAUDE.md`.

**Binding contract for Phase 5 closure (verbatim from `PHASE11_PLAN.md §8.1` + `ORCHESTRATOR_REGISTRY.md §5`):**

```bash
EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x --timeout=60 \
    tests/evals/test_leakage.py \
    tests/evals/test_citations.py \
    tests/evals/test_refusal.py \
    tests/evals/test_no_llm_imports.py
```

These four files collectively bind invariants 2 (no LLM imports outside `llm_gateway` / `llm_providers/*`), 3 (no q&a over offrecord/nomem/forgotten), 4 (citations point to `message_version_id`), and 9 (tombstones durable).

**Observed baseline (commit `bc98bbd`):**

| Metric | @1 | @3 | @5 |
|---|---|---|---|
| mean_recall | 0.125 | 0.125 | 0.125 |
| mean_precision | 0.125 | 0.042 | 0.025 |
| abstain rate | 7/8 (87.5%) | 7/8 | 7/8 |

**Deferred to post-Phase-5 (Wave 3):**

| ID    | Title                                          | GitHub issue | Status |
|-------|------------------------------------------------|--------------|--------|
| T11-W3-01 | LLM-synthesis hallucination test           | #185 | deferred (needs `/recall` wired to LLM) |
| T11-W3-02 | Citation drift test                        | #186 | deferred (needs `qa_traces.answer_text` Phase 5 schema) |
| T11-W3-03 | Cost / latency benchmark                   | #187 | deferred (needs `llm_usage_ledger`) |
| T11-W3-04 | Phase 11 Final Holistic Review (FHR)       | #188 | retrospective FHR scheduled on Phase 11 PR set; full Wave 3 FHR after Phase 5 closes |

**Outstanding follow-ups (not blocking):**
- #219 — `seed_v1` quality (7/8 abstain rate; rewrite queries or expand corpus)
- #220 — soak window (monitor first 3 nightly `evals.yml` runs; revert flag if flaky)
- T11-CHORE-01 / T11-CHORE-02 (this PR) — rename deferred draft + reconcile HANDOFF stale rows

---

## Phase 4 — Hybrid search + Q&A with citations — **CLOSED 2026-04-30**

6/6 implementation tickets merged. FHR in flight (Codex deep-product + deep-spec reviewers running over the full Phase 4 diff).

**Closed deliverables:**
- T4-01 + T4-02 via PR #151 + PR #156 hardening
- T4-03 via PR #157 (evidence bundle)
- T4-04 via PR #162 (/recall handler + memory.qa.enabled flag, default OFF)
- T4-05 via PR #158 (qa_traces audit, migration 022)
- T4-06 via PR #162 (12 eval cases, all 5 categories)
- T4-02H closed as duplicate of PR #156

**Outstanding known follow-ups (deferred to next cycle):**
- qa_traces cascade layer wiring on `/forget_me` — Stream E xfail test documents the gap. To be added to `bot/services/forget_cascade.py::CASCADE_LAYER_ORDER`.
- Phase 11 numbering conflict: HANDOFF Phase 11 = Shkoderbench/evals, draft Phase 11 = expertise pages. Human reconcile required before Phase 11 authorization.

Design + stream allocation: `PHASE4_PLAN.md` (PR #152, commit `5bd4888`). Audit + corrections: PR #154 (commit `276983a`). Wave 1 status update: PR #161.

| Issue | Ticket | Status | Notes |
|---|---|---|---|
| [#145](https://github.com/Jekudy/vibeshkoder/issues/145) | T4-01 | ✅ closed | FTS schema (migration 020) shipped via PR #151 with deviations: column `tsv` (not `search_tsv`), index `idx_message_versions_tsv` (not `ix_*_search_tsv`), source uses `text` (not planned `normalized_text`), partial GIN index `WHERE is_redacted=false` (plan §5.A item 5 rejected partial-index strategy). `MessageVersion.tsv` ORM column not declared; SQL via `text()` works. Cosmetic deviations accepted; source switch + ORM column tracked in #153. |
| [#146](https://github.com/Jekudy/vibeshkoder/issues/146) | T4-02 | ✅ closed | `bot/services/search.py::search_messages` shipped via PR #151. **Material gaps tracked in #153:** `SearchHit` ships 6 fields (planned 9); `current_version_id = mv.id` JOIN clause missing → returns historical message versions; tombstone NOT EXISTS uses single `target_type='message'` key, missing 3-key (`message:`/`message_hash:`/`user:`) pattern from plan §5.B → invariant #9 weakened for `message_hash`-targeted and `user`-targeted forget events. Test coverage gaps: russian stemmer, injection safety, current_version_id filtering, 100-row pagination. Cascade `fts_rows` layer remains `{status: 'skipped'}` but de-facto correctness preserved by existing `_cascade_message_versions` nulling content + partial index `WHERE is_redacted=false`. |
| [#147](https://github.com/Jekudy/vibeshkoder/issues/147) | T4-03 | ✅ closed | **Stream C MERGED via PR #157** (commit `8dda534`) on 2026-04-30. `bot/services/evidence.py` ships frozen `EvidenceBundle`/`EvidenceItem` dataclasses (slots, immutable, JSON-serializable, 9-field shape matching canonical `SearchHit`). `tests/services/test_evidence.py` — 6 tests passing. `tests/fixtures/evidence_bundle_v1.json` snapshot committed for Phase 5 contract stability. `from_hits()` performs no DB lookup. |
| [#148](https://github.com/Jekudy/vibeshkoder/issues/148) | T4-04 | ✅ closed | **MERGED via PR #162**. `/recall` handler (`bot/handlers/qa.py`), `memory.qa.enabled` feature flag, `run_qa` service. Phase 4 Hotfix #164 extended with: flag-OFF message persistence (§3.5), TelegramForbiddenError handling in non-community refusal (§3.4), raw_update_id threading to QaTraceRepo (§3.5). |
| [#149](https://github.com/Jekudy/vibeshkoder/issues/149) | T4-05 | ✅ closed | **Stream E MERGED via PR #158** (commit `e952b81`) on 2026-04-30. Migration **022_add_qa_traces** (down_revision="021"). `bot/db/repos/qa_trace.py::QaTraceRepo.create(...)`. Hotfix #164: xfail flipped — qa_traces cascade layer now wired in `CASCADE_LAYER_ORDER` with `_LAYER_APPLICABLE_TARGET_TYPES` gate; `server_default="'[]'"` on `evidence_ids`; +5 §3.3 tests. |
| [#150](https://github.com/Jekudy/vibeshkoder/issues/150) | T4-06 | ✅ closed | **MERGED via PR #162**. 12 eval cases (5 categories). Hotfix #164 extended to 14 cases: `imp_001_basic_text` + `imp_002_offrecord_abstain` via real `run_apply` import path. |
| [#164](https://github.com/Jekudy/vibeshkoder/issues/164) | hotfix | ✅ closed | **Phase 4 Final Holistic Review hotfix — MERGED via PR #TBD** on 2026-05-02. Addresses 3 CRITICAL + 6 risk-audit findings (H1/H2/H3/N3 + §3.4/§3.5/§3.7/§3.8/§3.9/§3.10). Key changes: (1) `persist_message_with_policy` now creates v1 `MessageVersion` + closes `current_version_id` FK loop (CRITICAL 1); (2) `import_apply` delegates to `persist_message_with_policy` as sole writer (CRITICAL 2/3, H2); (3) `qa_traces` cascade layer wired (§3.3); (4) qa handler persists `/recall` message before flag check + handles `TelegramForbiddenError` (§3.4/§3.5); (5) `live_ingestion_run_id` wired from startup through middleware to `record_update` (H1/§3.8); (6) `raw_update_id` threaded through `chat_messages.py` + `edited_message.py` (N3/§3.10); (7) eval fixture uses real import path for `imp_*` cases (H3/§3.9); (8) Migration 023 backfills post-008 cohort. 572 tests pass (566 new vs baseline). Operator note: raw-archive audit chain dormant until `memory.ingestion.raw_updates.enabled=true`; see `docs/runbook.md`. |
| [#153](https://github.com/Jekudy/vibeshkoder/issues/153) | T4-02H | ✅ closed (duplicate) | **Closed as duplicate of PR #156** (commit `2b2a38a`, `fix(p4-fts): close FTS review findings`) which already shipped: SearchHit 9 fields, `current_version_id = mv.id` JOIN, three-key tombstone NOT EXISTS, `ts_rank_cd`, query length cap (256), column rename `tsv`→`search_tsv`, source `text`→`normalized_text`, ORM column `MessageVersion.search_tsv` via `MessageVersionSearchVectorExpression` compiler. Independent collision audit (PR #154 corrections cycle) confirmed redundancy. |

### Phase 4 forward-looking design drafts (NOT AUTHORIZED — design only)

Merged via PR #159 (commit `df7c016`) and PR #160 on 2026-04-30, all under `docs/memory-system/prompts/`:

- `CODEX_DUAL_AGENT_PATTERN.md` — canonical executor+verifier orchestration pattern (Codex-based dual-agent default for Phase 4+).
- `ORCHESTRATOR_PROMPT.md` — copy-paste meta-prompt for orchestrator session; paranoid-mode rules; collision watch; stale-state recovery.
- `PHASE5_PLAN_DRAFT.md` — LLM synthesis gateway + usage ledger (T5-01..T5-05).
- `PHASE6_PLAN_DRAFT.md` — knowledge cards / catalog (T6-01..T6-09).
- `PHASE7_PLAN_DRAFT.md` — daily/weekly digests (T7-01..T7-08).
- `PHASE8_PLAN_DRAFT.md` — reflection runs / observations / memory_events (T8-01..T8-09).
- `PHASE9_PLAN_DRAFT.md` — wiki (member-only first; per-page public approval gate). Invariant #10 binding. T9-01..T9-08. **REFINED 2026-05-02** by Orchestrator B sprint-0b: §0a "Refinement Status" added (RATIFIED PENDING PHASE 6 CLOSURE; implementation deferred until Phase 6 close + AUTHORIZED_SCOPE update + promotion to canonical PHASE9_PLAN.md); migration window front-matter corrected 040+ → 050+ per REGISTRY §2; §0a Phase 6 dependency contract enumerates 8 fields cards must expose for wiki; web layout discrepancy (`web/routers/` vs existing `web/routes/`) noted as Wave 1 implementer reconciliation task.
- `PHASE10_PLAN_DRAFT.md` — graph projection. Invariant #6 binding. T10-01..T10-09. Includes cascade `graph_nodes` layer. **REFINED 2026-05-02** by Orchestrator B sprint-0b: §0a "Refinement Status" added (RATIFIED PENDING PHASE 6 + PHASE 8 CLOSURE); 6 "Open for ratification" decisions provisionally resolved (graph store: Apache AGE default, hosting: same compose stack, update cadence: scheduled batch, privacy: forget cascade canonical) with final-choice deferral to promotion sprint; alembic window pinned to 060+ within Orch B owned 050–069 range to leave headroom for Phase 9 wiki migrations; Phase 6 + Phase 8 dependency contract enumerates 13 fields across `knowledge_cards` (5: id, title+body_markdown, card_status, source_message_version_ids), `observations` (5: id, cited_message_version_ids, confidence_score, topic_tags, policy), and `forget_events` (3: target_type, target_id, tombstone_key) that upstream phases must expose for graph projection. Three explicit gap-callouts (no `card_relations` table in Phase 6 actual scope; observations not triple-shaped; no `visibility_scope` column) with provisional resolutions documented.
- `PHASE11_PLAN_DRAFT.md` — person expertise pages. **NUMBERING CONFLICT:** HANDOFF currently has Phase 11 = Shkoderbench/evals; this draft repurposes Phase 11 = expertise. Stop signal flags conflict for human reconcile.
- `PHASE12_PLAN.md` — butler / action execution (postponed per AUTHORIZED_SCOPE; design-only). **RATIFIED 2026-05-02** by Orchestrator B (sprint 0a, branch `plan/p12-ratify`); promoted from `prompts/PHASE12_PLAN_DRAFT.md` to canonical path via `git mv`; §11 Compliance Recap added; Final Report Block updated. Invariant #7 + #2 + #9 binding. T12-01..T12-10 remain design-only contracts; implementation requires AUTHORIZED_SCOPE.md update.

All drafts open with 🚧 DRAFT — NOT AUTHORIZED banner; cite HANDOFF §1 invariants verbatim; defer LLM/vector implementation to phase boundary; list 5+ open design questions for human ratification.

### Wave allocation (post-audit)

- **Wave 1 (parallel, unblocked):** Stream C (#147), Stream E (#149).
- **Wave 1.5 (hardening, parallel-safe with Wave 1):** #153 — coordinate so Stream D consumes the expanded `SearchHit`.
- **Wave 3:** Stream D (#148 + #150) — after Wave 1 + #153.

### Stream prompts (in repo root)

`PHASE4_STREAM_C_PROMPT.md`, `PHASE4_STREAM_D_PROMPT.md`, `PHASE4_STREAM_E_PROMPT.md` — corrected and ready for autonomous execution. Stream A and Stream B prompts retained as historical reference (their work shipped via PR #151).

---

## Phase 5 — LLM gateway + answer synthesis (synthesis-first slice)

**Plan:** `docs/memory-system/PHASE5_PLAN.md` (ratified 2026-05-02 by Orchestrator A).
**Authorization:** `AUTHORIZED_SCOPE.md` §"Authorized: Phase 5" (added 2026-04-30).
**Predecessor blocker:** issue #164 (3 CRITICAL Phase 4 production gaps) absorbed as Wave 0 — single PR using design at `docs/memory-system/prompts/PHASE5_WAVE0_HOTFIX164_DESIGN.md` (1044-line v3 FINAL spec, Critic v2 + Risk v2 closed).
**Worktrees:** `.worktrees/orch-A` (planning), future per-wave worktrees `.worktrees/p5-w0-hotfix-164`, `.worktrees/p5-w1-gateway`, `.worktrees/p5-w1-schema`, `.worktrees/p5-w2-repo-handler`, `.worktrees/p5-w3-evals`.
**Owned alembic range:** 023–049. Wave 0 = 023, Wave 1 = 024, Wave 2 = 025, remainder reserved.

| Ticket | Wave | Title | Status | Notes |
|--------|------|-------|--------|-------|
| T5-W0-01 | 0 | Phase 4 hotfix #164 — live v1 + import current_version_id + normalized_text + qa_traces cascade + router order | merged | Single PR (15 commits per design §6). Migration 023 backfills legacy `current_version_id IS NULL` cohort. Source: `prompts/PHASE5_WAVE0_HOTFIX164_DESIGN.md`. Blocks all Wave 1+ work. **Merged via PR #203** (issue #164 remains open until PR #204 closes it). |
| T5-01 | 1 | `bot/services/llm_gateway.py` core — `synthesize_answer`, provider abstraction, pre-call invariants, cache lookup — GitHub: #197 | merged | Stream A. Shipped via **PR #209** (commit `7dcb218`). `synthesize_answer(session, *, bundle, query, config, qa_trace_id, ledger_repo, cache_repo, provider) -> SynthesisResult` with 7 pre-call invariants + F4 cache-race recovery via IntegrityError + budget-lock-released-before-HTTP placeholder pattern (per contracts.md §12.2 REVISED). 59 gateway+provider tests. |
| T5-02 | 1 | Alembic 024 — `llm_usage_ledger` + `llm_synthesis_cache` schema — GitHub: #198 | merged | Stream B. Shipped via **PR #207** (commit `5fcd99b`). ORM in `bot/db/models.py::LlmUsageLedger` (line 761) + `LlmSynthesisCache` (line 816). 19 schema tests. |
| T5-03 | 2 | `LedgerRepo` + `SynthesisCacheRepo` async repos — GitHub: #199 | merged | Shipped via **PR #223** (commit `18c98893`). 4 methods on each repo (incl. `update_placeholder` per §12.2 REVISED + `invalidate_by_citation` JSONB `@>` with SQLite portable fallback). 17 tests. PAR: Claude critic ACCEPTED + Codex round-2 APPROVE (after fix `e5bc5ea` for rollback proof + SQLite hydration hygiene + bump_hit ts advance). |
| T5-04 | 2 | `/recall` LLM synthesis + `qa_traces` extension (alembic 025) + new cascade layers in `forget_cascade.py` — GitHub: #200 | merged | Shipped via **PR #226** (commit `43f21ee`). 7 commits: alembic 025 + QaTrace ORM ext + LlmUsageLedger.prompt_hash nullable + QaTraceRepo.update_llm_fields + llm_pricing.py + gateway `_estimate_cost` wired + `prompt_template_version` v0.1.0→v1.0.0 + qa.py 4-step ORDER + flag `memory.qa.llm_synthesis.enabled` (default FALSE; Phase 4 byte-for-byte preserved when OFF) + forget_cascade 3 new layers (synthesis_cache FIRST → qa_traces_llm → llm_usage_ledger). 27 new tests + 4 byte-identity + 1 integration. PAR: Claude critic ACCEPTED (4 stop signals clear); Codex 2 rounds REQUEST_CHANGES → fixes `c5b5c38` + `33248e2` + `d6b2c51` (lint-privacy allowlist). Stall recovery executed: prior deep-implementer dispatch stalled 100min; orchestrator salvaged partial work into 3 atomic commits + narrower handler dispatch succeeded. |
| T5-05 | 3 | Eval harness extension + integration fixtures — GitHub: #201 | merged | Shipped via **PR #229** (commit `5faea1d`). 8 fixture cases per contracts.md §9 + mocked unit evals (9 in-CI + 1 opt-in real-gateway smoke). Stabilized PKs 7005/7008 for cache-hit + happy-path. Phase 11 (Orch C) consumes fixture VERBATIM (cross-orch contract per REGISTRY §5). PAR: Claude `deep-code-reviewer` ACCEPTED + Codex round-2 APPROVE (after fix `3a11f1f` for fixture-verbatim consumption + prompt_template_version v1.0.0 alignment). Carryover M-1: `qa_trace_id=None` type drift — gateway robust per ledger FK nullable; tighten in Phase 6 kickoff. |

## Phase 5 — **CLOSED 2026-05-11**

All 6 implementation tickets merged. **FHR Claude `deep-product-reviewer` (Opus) ACCEPTED** with 0 CRITICAL / 0 HIGH / 4 MEDIUM carryovers (documented below).

Phase 5 ships the **synthesis-first slice** of LLM gateway per ratified PHASE5_PLAN.md §2:
- `bot/services/llm_gateway.py` (885 LOC) — `synthesize_answer` + 7 pre-call invariants + provider abstraction (Anthropic + OpenAI) + DB-backed cache + budget guard with lock-released-before-HTTP placeholder pattern + categorized error handling + citation enforcement
- `bot/services/llm_providers/{anthropic,openai}.py` + `bot/services/observability.py::emit_stop_signal`
- `bot/services/llm_pricing.py` — MODEL_PRICING (Haiku 4.5 $1/$5 + gpt-4o-mini $0.15/$0.60)
- `bot/db/models.py::LlmUsageLedger / LlmSynthesisCache / QaTrace` (extended)
- `bot/db/repos/llm_*.py` — 4 methods each (flush-only)
- `alembic 023/024/025` — backfill + ledger/cache + qa_traces LLM ext
- `bot/handlers/qa.py` — 4-step ORDER (CREATE trace → synthesize → UPDATE fields → render) + flag `memory.qa.llm_synthesis.enabled` default OFF + flag-OFF Phase 4 byte-for-byte preservation
- `bot/services/forget_cascade.py` — 3 new layers in BINDING ORDER (`_cascade_llm_synthesis_cache` FIRST → `_cascade_qa_traces_llm` → `_cascade_llm_usage_ledger`)
- `tests/eval/test_qa_llm_eval_cases.py` + `tests/fixtures/qa_llm_eval_cases.json` (8 cases for Phase 11 handoff)

**Privacy invariants verified end-to-end:**
- #2 (no LLM outside gateway) — `tests/evals/test_no_llm_imports.py` 4-test suite green on main
- #3 (no offrecord/forgotten through) — gateway STEP_SOURCE_FILTER + STEP_FORGET_INVALIDATION_GATE (3-key tombstone) + cache-FIRST cascade layer
- #9 (tombstones durable) — `_cascade_llm_usage_ledger` NULLs PII (`prompt_hash`/`response_hash`) while preserving budget aggregates (`cost_usd`/`tokens`/`latency_ms`)

**Phase 11 (Orch C) cross-orch binding ACTIVE**: T11-W2-04 baseline frozen 2026-05-11; nightly `evals.yml` runs leakage/citations/refusal/no_llm_imports tests.

**FHR carryovers (documented):**
- **M-1** (Phase 6 kickoff): tighten `bot/services/llm_gateway.py::synthesize_answer` annotation `qa_trace_id: int` → `int | None` OR add runtime `assert qa_trace_id is not None` at function entry. Gateway is currently robust to None (ledger FK nullable) but contracts.md §3.1 says REQUIRED.
- **M-2** (closure PR — this commit): contracts.md §3.4 + §3.6 + §5.1 field-name drift `daily_usd_ceiling` → `daily_ceiling_usd` (impl shipped `daily_ceiling_usd`).
- **M-3** (closure PR — this commit): `tests/fixtures/qa_llm_eval_cases.json` — runtime-seeded convention documented for cases with `evidence_message_version_ids=[]` (eval-002/003/004/006/007).
- **M-4** (Phase 6 kickoff): add direct `_cascade_qa_traces_llm` + `_cascade_llm_synthesis_cache` `message_hash` sub-case tests with `llm_response_summary` NULL assertions.

Plus L-1..L-4 cosmetic carryovers (N+1 perf, alembic 025 `import hashlib` placement, etc.).

**Carryovers from per-PR PAR reviews (documented in `.par-evidence.json`):**
- contracts.md §5.1+§10.1+§12.2 update_placeholder return type drift (-> None vs -> int rowcount).
- contracts.md §12.3 update_llm_fields return type drift (-> None vs -> rowcount + LookupError note).

## Phase 6 — Knowledge cards + admin review (CLOSED 2026-05-12)

**Sprint 0 RATIFIED 2026-05-12.** Plan promoted to `docs/memory-system/PHASE6_PLAN.md`. All 9 tickets merged across Wave 1 (T6-00..T6-03) + Wave 2 Stream C (T6-04, T6-05, T6-09) + Stream D (T6-06, T6-07). T6-08 originally deferred at closure (no Phase 5 web scaffold); **shipped retroactively 2026-05-13 via PR #281** once a web scaffold was in place. FHR: Claude product APPROVED + Claude technical APPROVED 2026-05-12. FHR blocker (LLM gateway `_TOMBSTONE_GATE_SQL` `mv.content_hash` path) fixed via PR #259 in same closure cycle. Carryover issues filed: #260 `_process_one_event` rename, #261 extractor running-row leak, #262 MED/LOW + deferred items (Phase 6.5).

**Phase 6.5 closure (2026-05-13):** Carryover bundle resolved — #261 (extractor running-row leak) via PR #265, #260 (_process_one_event rename) + #262 trivial items (UNION ALL comment, column order annotation) via PR #266. MED/LOW items M-1/M-2/M-4 from Codex post-merge audit remain open in #262. T6-06/T6-07 retrospective design docs landed in PR #264 (anomaly: implementation preceded docs). T6-09 broader e2e pipeline test (candidate→card→recall) merged via PR #267 2026-05-13. Codex post-merge follow-ups (SQL drift M1+M2, multi-item snapshot M3, docstring L1+L2) addressed in chore/p6-wave2-closure.

**Owned alembic range:** 030–049 (Wave 1 = 030–035).

| Ticket | Wave | Title | Status | Notes |
|--------|------|-------|--------|-------|
| T6-00 | 0 | FHR carryover — M-1 ValueError guard + M-4 message_hash cascade tests | merged | **Merged via PR #242** 2026-05-12, commits `1a33c16`..`793cddf` on main. M-1: `qa_trace_id` annotation changed to `int \| None`. M-4: direct `_cascade_qa_traces_llm` + `_cascade_llm_synthesis_cache` message_hash sub-case tests with `llm_response_summary IS NULL` assertions added. Codex round 1 MED fixed in `7e8d558`. |
| T6-01 | 1 | Phase 6 schema — migrations 030-034 + ORM + `_p6_mvid_advisory_lock_id` helper + `_cascade_card_sources_on_forget` | merged | **Merged via PR #245** 2026-05-12. Creates tables: `extraction_runs` (030), `extraction_candidates` (031), `knowledge_cards` (032), `card_sources` (033), `extraction_decisions` (034). Advisory lock helper + cascade. PAR: Codex round 2 fixes landed. |
| T6-02 | 1 | Extractor service + scheduler flag + `/admin_extract` handler + migration 035 `operator_user_id` | merged | **Merged via PR #248** 2026-05-12. Extractor service, admin Telegram handler, migration 035 `operator_user_id`. |
| T6-03 | 1 | Gateway `extract_candidates` endpoint | merged | **Merged via PR #254** 2026-05-12. LLM gateway `extract_candidates` endpoint + router + DI. Phase 11 leakage binding test cleared before merge. Wave 1 complete. |
| T6-04 | 2 (Stream C) | Admin `/candidates` + `/approve` + `/reject` handlers | merged | **Merged via PR #258** 2026-05-12. Admin candidate review flow with approval/rejection. |
| T6-05 | 2 (Stream C) | Admin `/cards` handler | merged | **Merged via PR #258** 2026-05-12. Admin cards listing handler. |
| T6-06 | 2 (Stream D) | Search `include_cards` flag | merged | **Merged via PR #256** 2026-05-12. `/recall` search can include knowledge cards in results. Design doc (retrospective): PR #264 2026-05-13. Codex M1+M2 SQL drift in design doc fixed in chore/p6-wave2-closure. |
| T6-07 | 2 (Stream D) | `EvidenceItem` `source_type` field | merged | **Merged via PR #256** 2026-05-12. Adds `source_type` discriminator to evidence bundle items. Design doc (retrospective): PR #264 2026-05-13. |
| T6-08 | #240 | Web cards page (optional) | **merged 2026-05-13 (PR #281, commit `d4b2185`)** | Read-only `/cards` + `/cards/<id>` admin pages, cookie-auth via existing `_PUBLIC_PATHS` middleware, 5 acceptance tests (auth redirect, list filter, 404-on-draft, body+sources render, privacy redaction). Web scaffold (`web/app.py`, `web/auth.py`, `web/routes/*`) existed at merge time — original "no scaffold" deferral rationale was stale. PR was opened autonomously by a Claude session during healing verification run 25813274803 (synthetic SIGNAL_PAYLOAD with no real bug); merge by @Jekudy. See follow-up issue for synthetic-signal scope guard in healing orchestrator. |
| T6-09 | 2 (Stream C) | Collision test + cascade advisory lock wiring | merged | **Merged via PR #258** 2026-05-12 (advisory lock wiring + collision regression test). Broader e2e pipeline test (candidate→card→recall) merged via **PR #267** 2026-05-13, commit on main. |

**FHR fix (same closure cycle):**
| Item | Title | Status | Notes |
|------|-------|--------|-------|
| FHR blocker | LLM gateway `_TOMBSTONE_GATE_SQL` uses `mv.content_hash` final path | fixed | **Merged via PR #259** 2026-05-12. |

**Phase 6.5 carryover resolution:**
| Issue | Title | Resolution |
|-------|-------|------------|
| #260 | `_process_one_event` rename | **Fixed PR #266** 2026-05-13. |
| #261 | Extractor running-row leak | **Fixed PR #265** 2026-05-13. |
| #262 trivial | UNION ALL comment, column order annotation | **Fixed PR #266** 2026-05-13. |
| #262 MED M-1 | T6-06 design doc SQL drift (`kc.title`, `card_anchors` columns) | **Fixed chore/p6-wave2-closure** 2026-05-13 (design doc only). |
| #262 MED M-2 | `card_anchors` CTE column mismatch in design | **Fixed chore/p6-wave2-closure** 2026-05-13 (design doc only). |
| #262 MED M-4 | (other MED items) | Open — tracked in #262. |

### Phase 11 follow-ups (#224, #219, #255) — all closed 2026-05-12

| Issue | Title | Status | Notes |
|-------|-------|--------|-------|
| #224 High #5 | httpx URL-level no-LLM guard | merged | **Merged via PR #243** 2026-05-12. Adds async `httpx` hook blocking non-allowlisted HTTP calls from LLM gateway. |
| #224 Critical #4 | Privacy allowlist narrowing + multiset baseline | merged | **Merged via PR #247** 2026-05-12. |
| #224 High #1-#4 | Test hardening items 1-4 | verified on main | Verified already-on-main; no separate PR needed. |
| #219 | seed_v1 quality fix (abstain rate 7/8 → 0/8) | merged | **Merged via PR #253** 2026-05-12. |
| #255 | Phase 4 message-branch tombstone uses `mv.content_hash` | merged | **Merged via PR #257** 2026-05-12. |

## Phases 7–12

**Implementation:** not started.

- **Phase 7 (digests):** gated on Phase 6 closure.
- **Phase 8 (reflection / observations / memory_events / memory_candidates / reflection_runs):** gated on Phase 7 closure. Note: HANDOFF originally placed extraction tables in Phase 5; PHASE5_PLAN.md §2 ratifies synthesis-first slice and defers extraction tables to Phase 8.
- **Phase 9 (wiki) + Phase 10 (graph projection):** Orchestrator B owns; gated on Phase 6 cards closure.
- **Phase 12 (butler design):** Orchestrator B; design-only, no implementation, authorized 2026-04-30.

Design drafts for Phase 6/7/8/9/10/11/12 remain in `docs/memory-system/prompts/PHASE{6,7,8,9,10,11,12}_PLAN_DRAFT.md` until each phase's owning orchestrator promotes to `_PLAN.md`.

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

## Phase 7 — Daily digests (CLOSED 2026-05-15)

**Sprint 0 RATIFIED 2026-05-14.** Plan ratified per `docs/memory-system/PHASE7_PLAN.md`
after 5 rounds of dual-model review (Codex technical + Claude product/spec). All 8
implementation tickets merged 2026-05-14..2026-05-15.

| ID | Wave | Description | Status | Notes |
|----|------|-------------|--------|-------|
| T7-S0 | 0 | docs ratify + AUTHORIZED_SCOPE | merged | **PR #285** 2026-05-14. Sprint 0 docs-only PR: `PHASE7_PLAN.md` (~1100 lines) + `AUTHORIZED_SCOPE.md` Phase 7 authorization block. |
| T7-01 | 1A | Migration 037 + ORM `Digest`/`DigestRun` | merged | **PR #287** 2026-05-14. `digests` table (16 cols, idempotency unique, 10 status values incl. `posting`, GIN citations index, partial draft/posting indexes); `digest_runs` audit table. 13 schema tests. |
| T7-03 | 1B | `digest_context.build_digest_context` | merged | **PR #290** 2026-05-14. Cards-first + chronological raw fallback; governance filter (memory_policy='normal' + is_redacted=FALSE + NOT EXISTS active forget_events). 14 tests. |
| T7-02 | 1B | `run_digest` orchestrator + `synthesize_digest` gateway extension | merged | **PR #293** 2026-05-15. Advisory-lock idempotency, Phase 7 separate cost bucket, EMPTY_WINDOW sentinel handling, citation invariant. 13 tests. |
| T7-04 | 2A | Scheduler `digest_daily_job` + stale-posting reaper | merged | **PR #294** 2026-05-15. Cron MSK timezone, `memory.digests.daily.enabled` flag-gated (default OFF), reaper every 5 min (always-on). 5 tests. |
| T7-05 | 2B | Publisher + renderer + redactor + cascade `digests` layer + bullet-index fix | merged | **PR #296** 2026-05-15. Single-transaction publisher holding row lock across send_message; HTML renderer with truncate-before-escape + tag-balance fallback; redactor with unconditional edit + erratum fallback + admin notify; forget cascade layer placed BEFORE card_sources (handles both `kind='message_version'` and `kind='card_source'` citations); bullet-index fix from #295 HIGH. 13 tests. |
| T7-06 | 3A | Admin handlers `/digest_now` `/digest_preview` `/digest_history` | merged | **PR #297** 2026-05-15. Admin-gated (silent no-op for non-admins); flag bypass; status-branched replies. 10 tests. |
| T7-07 | 3B | Phase 11 binding L7a/b + C6 + I5a/b/c | merged | **PR #298** 2026-05-15. Phase 11 binding extension 28 → 34. Forgotten exclusion, citation invariant, cascade redaction e2e, publish revalidation race. 6 tests. |
| T7-08 | 3C | Closure docs (PHASE7_ROLLOUT, IMPLEMENTATION_STATUS, ROADMAP, CLAUDE, AUTHORIZED_SCOPE) | merged | This PR. Operator rollout playbook + status updates. |

**Phase 11 binding suite: 34/34 green** (L1-L5 + L6a/b/c + L7a/b + C1-C4 + C5a-d + C6 + R1-R4 + I1-I4 + I5a/b/c).

**Carryover issues (Phase 7.5):**
- **#291** — Extract `_forget_excludes_predicate` shared helper between `forget_cascade._cascade_message_versions` and `digest_context.py` / `llm_gateway._digest_context_is_clean` (DRY guard against drift).
- **#295** — T7-02 post-merge Codex review items: provider-error categorization (currently falls to `unexpected:*`), EMPTY_WINDOW ledger error field (currently `error=None` on misbehavior). HIGH item (citation `position` as bullet index) already shipped in T7-05.

**FHR fix sprint (PR #300, 2026-05-15):** Final Holistic Review (Claude
`deep-product-reviewer` + Codex deep-technical, independent) returned
NEEDS_FIXES with **4 critical + 2 high**. All shipped in PR #300
(commits `df4bb71` + `0fcd54b`):

| Fix | Severity | File | Issue → Resolution |
|-----|----------|------|--------------------|
| F1 | CRITICAL | `bot/services/scheduler.py` | `digest_daily_job` never called `publish_digest` → Charter AC #6 violated. Fix: post-commit `if status='draft'` → call `publish_digest`. |
| F2 | CRITICAL | `bot/services/scheduler.py` + `bot/services/forget_cascade.py` | Cascade worker had no `bot` threading → `digest_redactor` skipped `bot.edit_message_text` in prod → forgotten content stayed visible in posted Telegram digests. Fix: thread `Bot` through `cascade_worker_tick → run_cascade_worker_once → _process_one_event`; set `event._runtime_bot = bot`; scheduler uses `args=[bot]`. |
| F3 | CRITICAL | `bot/services/llm_gateway.py` | `_parse_digest_citations` deduped by `(kind, id)` → multi-bullet citations to same source kept only first `position` → partial-forget left forgotten content in other bullets. Fix: removed `seen_keys` dedup for `mv`/`cs`. |
| F4 | CRITICAL | `bot/services/digests.py` | Idempotency-collision returned detached `_row_to_digest(row)` → publisher's `digest.status='posting'` mutation didn't persist → guarded `posted` UPDATE failed, leaving untracked Telegram posts. Fix: `await session.get(Digest, existing)` returns session-attached ORM object. |
| F5 | HIGH | `bot/services/digests.py` | `load_digest_config` didn't compare src vs dst → echo loop possible. Fix: raise `ConfigurationError` on `src == dst`. |
| F6 | HIGH | `bot/handlers/digest.py` | `/digest_now` had no `posting`-status branch. Fix: 1s refresh + polite Russian retry message. |

6 new red-green tests added. CI green. Privacy lint green.

**Production rollout:** see `docs/memory-system/PHASE7_ROLLOUT.md`.

## Phase 8 — Weekly editorial digest (CLOSED 2026-05-15)

**Sprint 0 RATIFIED 2026-05-15.** Plan ratified per `docs/memory-system/PHASE8_PLAN.md`
(~1970 lines, 16 sections, 9 ratified decisions Q1–Q9) after 3 rounds of dual-model
review on Sprint 0 (Claude product/spec + Codex technical). All 8 implementation
tickets merged 2026-05-15 across an autonomous ~3-hour orchestration session
(per-sprint unified review attempted but partial due to reviewer infrastructure
stalls; mitigated via inline spot-checks on highest-risk surfaces — migration 038
guard semantics, review SM transitions, cascade widening — plus strong implementer
evidence in PR bodies). Phase 11 binding extended 30 → **42/42** with 12 new cases.

| ID | Wave | Description | Status | Notes |
|----|------|-------------|--------|-------|
| T8-S0 | 0 | docs ratify + AUTHORIZED_SCOPE Phase 8 block + ROADMAP planned | merged | **PR #302** 2026-05-15 (commit `e1ee542`). Sprint 0 docs-only PR: `PHASE8_PLAN.md` (~1970 lines) + `AUTHORIZED_SCOPE.md` Phase 8 authorization block + `ROADMAP.md` row 8 PLANNED. |
| T8-01 | 1 | Migration 038 + ORM `Digest` review fields | merged | **PR #303** 2026-05-15 (commits `bdb13c6` + `467cf77` review fix follow-up). Migration 038: `ck_digests_status` + `ck_digest_runs_status` CHECK enums widened with 5 new audit values (`awaiting_review` / `approved_for_publish` / `rejected_by_admin` / `rejected_by_reaper` / `regenerated_by_admin`); ADD cols `published_by_admin_id`, `approved_at`, `review_notes`, `awaiting_review_at`; partial index `ix_digests_status_awaiting_review`; CHECK `ck_digests_approved_audit` + body-NOT-NULL visible-states widening; pre-flight downgrade guard. NOT VALID + VALIDATE pattern. |
| T8-02 | 2 | `run_digest` weekly + `synthesize_digest` weekly | merged | **PR #304** 2026-05-15 (commit `dbefa1a`). Widened `run_digest(type=Literal['daily','weekly'])`; new prompt template `digest_weekly_v0_1_0.py` with section-aware structure (allowlist Highlights / People / Decisions / Open questions / Other); `synthesize_digest` routes by type; weekly cost ceiling separate bucket (`DIGEST_WEEKLY_USD_CEILING` $5.00 default — independent of daily/shared per C5). |
| T8-03 | 2 | `build_digest_context` weekly window | merged | **PR #305** 2026-05-15 (commit `733ad8f`). Weekly extension for 7-day ISO Mon..Mon MSK window; larger token budget (`DIGEST_WEEKLY_TOKEN_BUDGET` 24000 default); `weekly_min_cards_threshold` 8 (L5 empirical middle between daily-3 and linear-scaled 21). Forget-excludes predicate inlined with explicit TODO referencing #291. |
| T8-04 | 3 | Review SM + cascade/redactor/publisher widening | merged | **PR #306** 2026-05-15 (commits `05cfa88` + `e574ae2`). `bot/services/digest_review.py` with `transition_to_awaiting_review`, `approve_digest` (3-step revalidate → guarded UPDATE → dispatch publisher), `reject_digest`. Canonical `_raise_invalid_state_after_guard_miss` helper. Cascade scan + redactor allowlist widened to include 4 new statuses (`_REDACTOR_ELIGIBLE_STATUSES` 8-tuple). Publisher trigger-state widening `('draft','approved_for_publish')`. |
| T8-05 | 3 | Scheduler `digest_weekly_job` + reaper | merged | **PR #307** 2026-05-15 (commit `6225789`). Cron Mon 09:15 MSK (15-min H8 stagger past daily 09:00); `memory.digests.weekly.enabled` flag default OFF; `digest_stale_review_reaper_job` (48h DM notify + 7d auto-reject `rejected_by_reaper`); `day_of_week="mon"` hardcoded per M3. |
| T8-06 | 4 | Admin handlers weekly | merged | **PR #308** 2026-05-15 (commit `f6568e2`). `/digest_now weekly` (+ `--regenerate` for Q5 no-edit refuse-and-rerun), `/digest_review`, `/digest_approve <id>`, `/digest_reject <id> [reason]`. Admin-gated via `_is_admin`. Renderer §5.I section-header bolding + weekly footer flagged as out-of-scope by implementer → Phase 8.5 carryover. |
| T8-07 | 4 | Phase 11 binding 30 → 42 | merged | **PR #309** 2026-05-15 (commit `7a389c1`). 12 new cases: L8a/b (weekly forget exclusion), C7 (weekly citation invariant), I6a (review-state cascade redaction), I6b.1/.2/.3 (publisher trigger guard widening), I6c (redactor widening over awaiting_review), R5.a/b/c/d (admin-gate refusals on weekly handlers). Existing 30/30 binding preserved → new total 42/42. |
| T8-08 | 4 | Closure docs | merged | This PR. `PHASE8_ROLLOUT.md` (new), `IMPLEMENTATION_STATUS.md` (Phase 8 section), `ROADMAP.md` row 8 → CLOSED, `CLAUDE.md` Phase 8 closure entry, `AUTHORIZED_SCOPE.md` Phase 8 CLOSED marker. |

**Phase 11 binding suite: 42/42 green** (L1-L5 + L6a/b/c + L7a/b + L8a/b + C1-C4 + C5a-d + C6 + C7 + R1-R4 + R5.a/b/c/d + I1-I4 + I5a/b/c + I6a + I6b.1/.2/.3 + I6c).

**Carryover issues (Phase 8.5):**

- **§5.I renderer extension** — section header bolding + weekly-specific footer copy. Flagged by T8-06 implementer as out-of-scope for that brief; functional renderer ships, styling polish deferred.
- **M6 GIN index dead weight on `_cascade_digests`** — `forget_cascade` JSONB containment scan over `digests.citations` does not optimally use the existing GIN index. Perf-only Phase 7.5 / 8.5 follow-up; not a privacy or correctness blocker.
- **#291** — Extract `_forget_excludes_predicate` shared helper between `forget_cascade._cascade_message_versions` and `digest_context.py` / `llm_gateway._digest_context_is_clean`. T8-03 added the predicate inline (identical to Phase 7 inline copy) with explicit TODO comment referencing #291.
- **R5.a / R5.b handler-layer tightening** — service-layer contract assertions ship in T8-07; handler-layer assertion (against `/digest_review` / `/digest_approve` / `/digest_reject` direct invocation by non-admin) can be tightened in a small follow-up. T8-06 handlers are already shipped and admin-gated; binding currently asserts the service-layer refusal contract.

**Production rollout:** see `docs/memory-system/PHASE8_ROLLOUT.md`.

---

## Phase 9 — Wiki / Community Catalog (AUTHORIZED 2026-05-17, in progress)

**Status:** Sprint 0c complete (plan ratified). Wave 2 — 8/8 implementation
sprints merged. Phase 11 binding tests (T9-08) green — 30 tests / 18 AC IDs
across L9a-e + C8a-b + I7a-e + R6.a-f + G1. Awaiting FHR + ROLLOUT.

**Sprint completion (T9-01..T9-08 merged):**
- T9-01: PR #314 (schema migrations 050-054)
- T9-02: PR #316 (governance validator)
- T9-03: PR #317 (web auth role split — BLOCKER C closed)
- T9-04: PR #318 (renderer + bleach allowlist)
- T9-05: PR #319 (member router + Jinja + /robots.txt)
- T9-06: PR #320 (admin /wiki_publish / /wiki_unpublish / /wiki_robots)
- T9-07: PR #321 (forget cascade + advisory lock binding — closes T9-06 lock carryover)
- T9-08: Phase 11 binding tests — 30 tests / 18 AC IDs (this PR)

**Plan:** `docs/memory-system/PHASE9_PLAN.md` (1550+ lines, ratified
2026-05-17 after dual-model spec review — Claude product + Codex technical
— with 2 BLOCKER + 7 HIGH audit findings addressed across revision passes).

**Authorized scope:**
- 5 tables, migrations 050-054
- Web routes under `web/routes/wiki.py` + role expansion in `web/auth.py`
  (TWO passwords: `WEB_ADMIN_PASSWORD` + `WEB_MEMBER_PASSWORD`; closes
  privilege escalation hole)
- Server-side `wiki_render.py` + `wiki_governance.py` validator
- Admin Telegram handlers `/wiki_publish`, `/wiki_unpublish`, `/wiki_robots`
- Forget cascade extension: `_cascade_wiki_pages` + `_cascade_wiki_revisions`
  inserted between `digests` and `card_sources` in `CASCADE_LAYER_ORDER`
- Page lifecycle: `draft → reviewed → stale → archived` (stale forces
  `public_enabled=false`)
- Feature flag `memory.wiki.enabled` default OFF

**Sprint queue (8 tickets):**
- T9-01: schema migrations + ORM + constraints
- T9-02: governance validator with visibility view (mv → chat_message_id →
  memory_policy + transitive card_sources path + 3 forget tombstone keys)
- T9-03: web auth role expansion (2-password model)
- T9-04: server-side Markdown renderer with bleach allowlist + batched
  citation validation
- T9-05: member web routes with transitive offrecord bullet suppression
- T9-06: admin `/wiki_publish` with FOR UPDATE prior_pe capture + advisory
  mvid locks + audit log
- T9-07: forget cascade integration (wiki_pages + wiki_revisions layers)
  with `[CONTENT_REDACTED: forget_event_id={n}]` masking + N=50 bulk scale
  AC
- T9-08: Phase 11 binding tests (5 new test files + AST no-graph-imports
  + drift simulator) — 18-21 new test IDs; total target 60-63

**Phase 9.5 carryovers** (deferred):
- Multilingual, static export, two-admin quorum, edit-conflict resolution,
  page tagging, content moderation flow, member-compromise runbook, #291
  shared predicate refactor

---

## Phase 10 — Graph Projection / Neo4j (AUTHORIZED 2026-05-17, in progress)

**Status:** Sprint 0c complete (plan ratified). Sprint 0d implementation
waves dispatching.

**Plan:** `docs/memory-system/PHASE10_PLAN.md` (1593 lines, ratified
2026-05-17 after dual-model spec review with 2 BLOCKER + 9 HIGH + 4 MEDIUM
audit findings addressed across revision passes — including critical
sync→async cascade FLIP per RFC-001:415 conditional Neo4j approval).

**Authorized scope:**
- 4 Postgres tables, migrations 060-063: `graph_projection_runs`,
  `graph_provenance`, `graph_edges`, `graph_purge_pending`. Migration 064
  adds `llm_usage_ledger.call_type` discriminator with backfill.
- Neo4j 5.x Community Edition via docker-compose `--profile graph` (dev
  only initially; prod gated on hard ops checklist incl. Bolt SSL,
  password rotation, backup runbook, healthcheck, memory limits).
- Services: `graph_projector.py`, `graph_query.py`, `graph_adapter.py`
  (Protocol with `Neo4jAdapter` prod + `NetworkXAdapter` unit-test fake),
  `graph_purge_worker.py`.
- LLM gateway extension: `extract_graph_triples()` with `call_type=
  'graph_projection'` ledger column.
- Async cascade integration (RFC-001:415 strict pattern): forget cascade
  atomically enqueues `graph_purge_pending` rows in Postgres transaction;
  separate `graph_purge_worker` drives Neo4j bolt DELETE asynchronously;
  `graph_query.py` fails-closed via pending-purge read-block during async
  window. Replaces an earlier synchronous-purge proposal that violated
  RFC-001 condition.
- Ontology split: cards → semantic CONCEPT nodes + LLM triples;
  message_versions → provenance/event nodes only (no LLM extraction;
  avoids double-counting).
- Cost ceilings (separate from shared `LLM_DAILY_USD_CEILING` $5/day):
  `GRAPH_PROJECTION_DAILY_USD_CEILING` $2/day,
  `GRAPH_PROJECTION_RUN_USD_CEILING` $0.50/run, max 200 sources/run.
- 3 feature flags default OFF: `memory.graph.projection.enabled`,
  `memory.graph.query.enabled`, `memory.graph.write_pending.paused`.

**Sprint queue (9 tickets):**
- T10-01: docker-compose Neo4j service + graph_adapter Protocol +
  Neo4jAdapter + NetworkXAdapter + testcontainers dev dep
- T10-02: source eligibility contract + governance pre-filter helpers
- T10-03: schema migrations 060-064 (projection_runs + provenance +
  edges + purge_pending + ledger call_type backfill) + repos
- T10-04: `llm_gateway.extract_graph_triples()` + entity registry
  (cards.id → users.id → UNKNOWN refuse-on-UNKNOWN)
- T10-05: `graph_projector.py` modes (dry_run, incremental, full_rebuild
  replay-only no LLM, repair) + dry-run cost estimate
- T10-06: forget cascade integration — async layer
  (`_cascade_graph_provenance` enqueues purge_pending) + `graph_purge_worker`
  in cascade_worker_tick + read-block in graph_query
- T10-07: `graph_query.py` read-only API with role/visibility filters
  + provenance-required output + parameterized Cypher (no string
  interpolation)
- T10-08: admin Telegram handlers `/graph_project_now` (advisory lock
  serialization), `/graph_stats`, `/graph_query` + scheduler nightly
  03:30 MSK + PHASE10_ROLLOUT.md ops checklist
- T10-09: Phase 11 binding tests (6 new test files + Neo4j CI service
  block in evals.yml) — 15-16 new test IDs; total target 57-58

**Phase 10.5 carryovers** (deferred):
- Real-time projection hooks
- Member-facing graph queries
- Public graph surface
- Expertise pages
- APOC procedures (security review surface)

<!-- updated-by-superflow:2026-05-17 -->
