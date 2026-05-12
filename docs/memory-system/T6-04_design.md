# T6-04 Design — Admin candidate review commands

**Status:** Pre-flight design, Wave 2 / Stream C.
**Cycle:** Memory system Phase 6 — knowledge cards / catalog.
**Date:** 2026-05-12.
**Predecessor:** Phase 5 closed 2026-05-11 (commit `c7a10b4`). Wave 1 (T6-00/01/02/03) in flight.
**Companion docs:** `PHASE6_PLAN.md` §5.A, §5.C, §7; `T6-05_design.md` (sibling — admin browse).
**Author:** Wave 2 design sprint agent (pre-flight planning).

---

## §0. Acceptance criteria (verbatim from `PHASE6_PLAN.md` §7)

### T6-04: Admin candidate review commands

- **Scope:** Telegram handlers for `/candidates`, `/approve`, `/reject` only (no `/edit-card` — deferred to Phase 6.5 per Q6).
- **Acceptance criteria:**
  - Commands are admin-only.
  - `/candidates` paginates pending candidates.
  - `/approve` atomically promotes candidate to approved card, inserts `card_sources` rows, and writes decision audit.
  - `/approve` re-runs deterministic governance filter on each candidate source `message_version_id` per §5.C / R3; BLOCKS promotion with explicit error when any source is no longer eligible (no LLM re-prompt).
  - `/approve` handler MUST acquire `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))` for EVERY source `message_version_id` in candidate as first transaction step (§5.C step 2).
  - The forget-cascade orchestrator (`_process_one_event` in `bot/services/forget_cascade.py` — the de-facto `apply_forget_event` per §5.A.5) MUST acquire `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))` for the affected `message_version_id` as the FIRST step of each event apply.
  - T6-09 advisory-lock collision test MUST pass on T6-04 PR head.
  - `/reject` marks candidate rejected and writes `extraction_decisions.action='rejected'`.
- **Dependencies:** T6-01, T6-02.
- **Stream:** Wave 2 / Stream C.

---

## §1. Invariants enforced by this ticket

Cross-references to `PHASE6_PLAN.md` §1:

1. **#1 Existing gatekeeper must not break.** All new handlers register with their own router; chat-message handlers are untouched.
2. **#2 No LLM calls outside `llm_gateway`.** `/approve` re-validation is deterministic SQL only (R3 — no LLM re-prompt).
3. **#3 No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.** `/approve` re-runs the governance filter on EVERY candidate source `message_version_id` and aborts if any source has `memory_policy != 'normal'`, `is_redacted = TRUE`, or matches a `forget_events` tombstone — regardless of tombstone age.
4. **#4 Citations point to `message_version_id` or approved card sources (FK-normalized via `card_sources`).** `/approve` inserts one `card_sources` row per source mvid (FK to `message_versions.id` with `ON DELETE RESTRICT`).
5. **#5 Summary is never canonical truth.** Cards become canonical only after human `/approve`.
9. **#9 Tombstones are durable and not casually rolled back.** `/approve` re-reads `forget_events` under the advisory lock (closes H-Cdx-2).

---

## §2. /approve transaction protocol (verbatim from PHASE6_PLAN.md §5.C)

```
BEGIN
SELECT FOR UPDATE FROM extraction_candidates WHERE id = :candidate_id
-- 1. Lock candidate row (prevents double-approve from concurrent admin).
-- 2. Acquire advisory xact lock on each candidate.source_message_version_id:
--    mvid_lock_id = signed_int64(sha256(f'p6:mvid:{mvid}'))
--    (Same lock namespace as forget_cascade §5.A.5 step 1; serializes /approve vs cascade.)
SELECT pg_advisory_xact_lock(:mvid_lock_id_1), pg_advisory_xact_lock(:mvid_lock_id_2), ...
-- 3. NOW that we hold the serialization lock, check forget_events for ANY matching tombstone
--    (drop age filter — any tombstone blocks regardless of when it was inserted):
SELECT 1 FROM forget_events WHERE
    (target_type='message_version' AND target_id = :mvid) OR
    (target_type='message_hash' AND target_hash = (SELECT content_hash FROM message_versions WHERE id = :mvid)) OR
    (target_type='message' AND chat_id = :chat AND message_id = :mid)
-- ANY hit → ABORT (no extraction_decisions row; see §8 + R3-block log-only behavior).
-- A forget_event inserted between this check and step 6 cannot exist: any concurrent
-- apply_forget_event for this mvid is waiting on the advisory lock from step 2.
-- 4. Lock and re-validate message_versions:
SELECT id FROM message_versions WHERE id IN (:mvids) FOR SHARE
-- Confirm chat_messages.memory_policy='normal' AND chat_messages.is_redacted=false on each.
-- ANY failure → ABORT.
-- 5. INSERT knowledge_cards row (card_status='approved').
-- 6. INSERT card_sources rows (FK enforced).
-- 7. UPDATE extraction_candidates SET status='approved', reviewed_by=:admin, reviewed_at=now().
-- 8. INSERT extraction_decisions (action='approved', decided_by=:admin, decided_by_username=:admin_username).
COMMIT  -- pg_advisory_xact_lock auto-released here.
```

### Step-by-step expansion

**Step 0 — pre-transaction guards (outside BEGIN):**
- Sender resolved via `UserRepo.get(session, message.from_user.id)`; require `user.is_admin` OR `user.id in settings.ADMIN_IDS`. Silent denial (no Telegram reply) if not admin. Pattern reference: `bot/handlers/forward_lookup.py:45` (`is_admin = requester.id in settings.ADMIN_IDS or requester.is_admin is True`).
- Parse `<candidate_id>` from `CommandObject.args`. Reject malformed UUID with HTML-safe usage hint.
- Chat-type filter: `PrivateChatFilter()` (admin commands live in DM) to match `bot/handlers/admin.py` pattern.

**Step 1 — lock candidate row:**
- `SELECT * FROM extraction_candidates WHERE id = :candidate_id FOR UPDATE`. ORM equivalent: `select(ExtractionCandidate).where(ExtractionCandidate.id == cid).with_for_update()`.
- Confirm `candidate.status == 'pending'`. If `'approved'`/`'rejected'`/`'superseded'` → emit error (`already_decided`) and abort. NO write to `extraction_decisions` — the UNIQUE constraint on `(candidate_id)` would already reject a second decision row, but explicit guard is cheaper and clearer.
- Pull `source_message_version_ids` (staging JSONB list of ints).
- If list is empty → abort with `empty_source_set` error. Acceptance: "Must reject promotion if the candidate has no source message versions" (§5.C).

**Step 2 — acquire advisory xact locks (CRITICAL ORDERING):**
- For each `mvid` in `source_message_version_ids` (sorted to ensure deterministic lock acquisition order across concurrent transactions — prevents deadlock):
  - `lock_id = _p6_mvid_advisory_lock_id(mvid)` (single source of truth: `bot/services/forget_cascade.py:61`).
  - `await session.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})`.
- Batched alternative: pass an array of bigint via `SELECT pg_advisory_xact_lock(unnest(:lock_ids))` and consume the result. Either is correct. The deterministic sort order is the load-bearing invariant (deadlock prevention).
- **Why before step 3:** if `forget_events` were read before the lock was held, an `apply_forget_event` running concurrently could land between the SELECT and the INSERT in step 6 (closes H-Cdx-2 round 1+2 race window).
- xact-scoped → auto-release on COMMIT/ROLLBACK; no manual release needed.

**Step 3 — re-validate governance via `forget_events`:**
- For each source mvid, run a deterministic SQL check (NO LLM re-prompt — R3):
  ```sql
  SELECT 1
  FROM forget_events fe
  WHERE fe.status IN ('pending', 'processing', 'completed')
    AND (
      fe.tombstone_key = 'message:' || c.chat_id || ':' || c.message_id
      OR (c.content_hash IS NOT NULL AND fe.tombstone_key = 'message_hash:' || c.content_hash)
      OR (c.user_id IS NOT NULL AND fe.tombstone_key = 'user:' || c.user_id)
    )
  FROM message_versions mv
  JOIN chat_messages c ON c.id = mv.chat_message_id
  WHERE mv.id = :mvid
  LIMIT 1;
  ```
  (Tombstone key construction mirrors `bot/services/search.py:96-108` and `bot/services/llm_gateway.py:_TOMBSTONE_GATE_SQL`. Three keys: `message:`, `message_hash:`, `user:`. NEVER use `target_type='message_version'` form — the codebase tombstones live under `target_type='message'` per `forget_events` schema.)
- Spec quotes the SELECT with `target_type='message_version'` for clarity, but the actual `forget_events` schema (`bot/db/models.py:447-534`) uses `target_type IN ('message','message_hash','user','export')` and stores the resolved key in `tombstone_key`. T6-04 MUST use the tombstone-key form (proven in Phase 4 + Phase 5 paths) — this is canonical.
- ANY hit → R3-block: ABORT (no `extraction_decisions` row written), structured log via `logger.warning("approve_blocked", extra={...})` with fields `event=approve_blocked`, `candidate_id`, `admin_user_id`, `failure_reason='forget_tombstone_match'`, `forget_event_id` or `message_version_id`. Reply Telegram error to admin. Candidate stays `pending`.

**Step 4 — re-validate governance via `chat_messages` / `message_versions`:**
- `SELECT mv.id, c.memory_policy, c.is_redacted, mv.is_redacted AS mv_redacted FROM message_versions mv JOIN chat_messages c ON c.id = mv.chat_message_id WHERE mv.id IN (:mvids) FOR SHARE`.
- For each row: require `c.memory_policy == 'normal'` AND `c.is_redacted == FALSE` AND `mv.is_redacted == FALSE`. If candidate referenced an mvid that no longer exists (FK semantics: `message_versions` rows are NOT deleted, only redacted, so this is unexpected) → R3-block.
- If any row fails → R3-block per the same protocol as step 3.

**Step 5 — INSERT `knowledge_cards`:**
- Fields:
  - `id`: server-default `gen_random_uuid()`.
  - `title`: extracted from `candidate.candidate_json["title"]` (extractor contract — T6-02 emits `candidate_json` with `title` + `body_markdown` keys; verify in T6-02 final PR).
  - `body_markdown`: from `candidate.candidate_json["body_markdown"]`. Stored as Telegram MarkdownV2 per Q1 — handler MUST NOT re-render or escape; the extractor is responsible for emitting MarkdownV2-safe output.
  - `card_status`: `'approved'` (DB CHECK enforces `('draft','approved','archived')`).
  - `approved_by_user_id`: `message.from_user.id` (admin's tg id).
  - `approved_at`: `func.now()`.
- `body_tsv` is a STORED generated column; populated automatically. NO need to set.
- The `ck_knowledge_cards_approved_attribution` CHECK requires both `approved_by_user_id` + `approved_at` set when `card_status='approved'` — satisfied trivially because they're both written in this INSERT.
- Return the new `card.id` for step 6.

**Step 6 — INSERT `card_sources`:**
- One row per `mvid` in `source_message_version_ids`. Position preserved (`position = idx` from enumerate).
- DB UNIQUE constraint `uq_card_sources_card_id_message_version_id` enforces idempotency.
- FK `card_id` → `knowledge_cards.id` ON DELETE CASCADE. FK `message_version_id` → `message_versions.id` ON DELETE RESTRICT.
- Bulk insert via `session.add_all([...])`.

**Step 7 — UPDATE `extraction_candidates`:**
- `UPDATE extraction_candidates SET status='approved', reviewed_by=:admin_id, reviewed_at=now() WHERE id=:candidate_id`.
- The `ck_extraction_candidates_reviewer_consistency` CHECK requires `reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL` when `status='approved'` — both set here.

**Step 8 — INSERT `extraction_decisions`:**
- `INSERT INTO extraction_decisions (candidate_id, action, decided_by, decided_by_username, reason) VALUES (...)`.
- Fields:
  - `candidate_id`: from step 1.
  - `action`: `'approved'`.
  - `decided_by`: `message.from_user.id` (FK SET NULL on user soft-delete).
  - `decided_by_username`: snapshot of `User.username` resolved at decision time — NOT NULL, audit shadow per §5.A.
  - `reason`: optional. For `/approve` typically NULL.
- The `uq_extraction_decisions_candidate_id` UNIQUE constraint enforces "exactly one terminal decision per candidate".

**COMMIT** — outer transaction commits via aiogram middleware's `async with session.begin()` (verify pattern with `bot/handlers/qa.py` which uses the same middleware). Advisory locks auto-release on COMMIT.

### Transaction boundary considerations

- The aiogram middleware injects `session: AsyncSession` whose lifecycle is `async with session.begin()` per handler. Step 1 (FOR UPDATE) → step 8 (INSERT) all run inside that single transaction.
- All `flush()` calls inside the repos (per Phase 5 pattern — repos never commit; caller owns the transaction) are safe: `pg_advisory_xact_lock` is transaction-scoped, so `flush` (which does NOT commit) keeps the lock held.
- If `session.begin_nested()` (SAVEPOINT) is used anywhere, the advisory locks are held by the outer transaction, NOT released on SAVEPOINT release/rollback. Acceptable.

### R3-block error reporting to admin

Telegram reply when R3 aborts:
```
❌ Approval blocked: source message no longer eligible.
Candidate: <candidate_id>
Reason: forget_tombstone_match | source_redacted | source_memory_policy_not_normal
Source mvid: <mvid>
```
Use `parse_mode="HTML"` and escape user-supplied content. No raw body content from forgotten messages may appear in the reply.

---

## §3. /reject transaction protocol

Simpler than `/approve` — no governance re-validation, no advisory locks (does NOT mutate eligible sources, only writes audit row + flips candidate status).

```
BEGIN
SELECT * FROM extraction_candidates WHERE id = :candidate_id FOR UPDATE
-- Confirm status='pending'; abort with already_decided if not.
UPDATE extraction_candidates
   SET status='rejected', reviewed_by=:admin_id, reviewed_at=now()
 WHERE id = :candidate_id;
INSERT INTO extraction_decisions (candidate_id, action, reason, decided_by, decided_by_username)
VALUES (:candidate_id, 'rejected', :reason, :admin_id, :admin_username);
COMMIT
```

**Inputs:**
- `<candidate_id>`: required. UUID.
- `[reason]`: optional free text. Stored verbatim in `extraction_decisions.reason`. No length cap beyond the `Text` column max — handler should still truncate at ~1024 chars defensively (admins type by hand).

**Acceptance ties:**
- "marks candidate rejected" → status flip in `extraction_candidates` (§5.C step 7-equivalent).
- "writes `extraction_decisions.action='rejected'`" → INSERT in this single transaction.

The `UNIQUE(candidate_id)` constraint on `extraction_decisions` means `/reject` of an already-decided candidate fails on DB level. The explicit `FOR UPDATE + status check` short-circuits before INSERT and produces a clean error message.

---

## §4. /candidates paginated list

Read-only handler; no advisory locks, no writes.

### Query

```sql
SELECT id, candidate_json, source_message_version_ids, extraction_run_id, created_at
FROM extraction_candidates
WHERE status = 'pending'
ORDER BY created_at DESC, id DESC  -- DESC: newest first
LIMIT :page_size OFFSET :offset
```

Page size: 10 candidates per page (default; configurable later). Pagination via `?page=N` query string equivalent — for Telegram, parse `/candidates 2` for page 2; default to page 1 if no argument.

### Rendering (Telegram MarkdownV2 — match existing handlers)

```
📋 *Pending candidates* (page 1)

#1  `<short_uuid>`
    title: «extracted title goes here»
    sources: 3 mvids · run: <extraction_run_short>
    created: 2026-05-12 14:32 UTC

#2  ...

Use `/approve <id>` или `/reject <id> [reason]`. Page 2: `/candidates 2`.
```

- `<short_uuid>` = first 8 chars; full UUID accepted by `/approve` and `/reject` but short form is fine for human selection (resolve via `WHERE id::text LIKE :prefix || '%'` if short form is passed).
- HTML escape free-text fields (titles can contain `<`, `>`, `&`).

### Limits

- If zero pending candidates → reply `📋 Nothing pending.` and stop.
- If page is empty (offset > total) → reply `📋 Page <N> is empty. Use /candidates to start over.` Pagination clamp at last available page is also acceptable.

---

## §5. Files touched

| File | Action | Notes |
|---|---|---|
| `bot/handlers/admin_cards.py` | NEW | All three handlers (`/candidates`, `/approve`, `/reject`) in one router. Naming follows `bot/handlers/admin.py` pattern. |
| `bot/handlers/__init__.py` | UPDATE | If a registry / `routers = [...]` list exists, append `admin_cards.router`. Verify pattern in current `__init__.py`. |
| `bot/__main__.py` | UPDATE | Include `admin_cards.router` in dispatcher AFTER `admin.router` (filter ordering). Add BEFORE `chat_messages.router` catch-all so `Command` filters match first — match the existing `forget_reply` registration pattern (§5.A in `bot/handlers/forget_reply.py:14-16`). |
| `bot/db/repos/extraction_candidate.py` | NEW | New repo class `ExtractionCandidateRepo` with: `list_pending(session, limit, offset)`, `get_by_id_for_update(session, candidate_id)`, `mark_status(session, candidate_id, status, reviewed_by)`. Flush-only — caller owns transaction (pattern: `bot/db/repos/qa_trace.py`). |
| `bot/db/repos/knowledge_card.py` | NEW | New repo class `KnowledgeCardRepo` with: `create(session, title, body_markdown, approved_by_user_id)` returning the row. Flush-only. |
| `bot/db/repos/card_source.py` | NEW | New repo class `CardSourceRepo` with: `bulk_create(session, card_id, message_version_ids)`. Flush-only. |
| `bot/db/repos/extraction_decision.py` | NEW | New repo class `ExtractionDecisionRepo` with: `create(session, candidate_id, action, decided_by, decided_by_username, reason)`. Flush-only. |
| `bot/services/governance_revalidation.py` | NEW (or extension to `bot/services/governance.py`) | Pure function: `async def revalidate_sources(session, mvids: list[int]) -> tuple[Literal['ok'], None] \| tuple[Literal['blocked'], dict[str, Any]]`. Returns `('ok', None)` or `('blocked', {"failure_reason": "forget_tombstone_match"\|"source_redacted"\|"source_memory_policy_not_normal", "mvid": int, "forget_event_id": int \| None})`. Used ONLY by `/approve` step 3+4. |
| `tests/handlers/test_admin_cards.py` | NEW | Unit + integration. See §7. |
| `tests/services/test_governance_revalidation.py` | NEW | Pure-function tests on the re-validation helper. |
| `tests/db/test_extraction_candidate_repo.py` | NEW | Repo CRUD. |
| `tests/db/test_knowledge_card_repo.py` | NEW | Repo CRUD. |
| `tests/db/test_card_source_repo.py` | NEW | Repo CRUD + UNIQUE behaviour. |
| `tests/db/test_extraction_decision_repo.py` | NEW | Repo CRUD + UNIQUE behaviour. |
| `tests/services/test_advisory_lock_collision.py` | NEW | T6-09 deferred home, BUT a smoke test of `/approve` vs `apply_forget_event` lock collision lives here from the T6-04 PR. See §7. |
| `scripts/lint_privacy_check.sh` | UPDATE (allowlist) | Add `bot/handlers/admin_cards.py` + `bot/services/governance_revalidation.py` paths to the allowlist if they touch policy strings (`memory_policy`, `is_redacted`, tombstone-key construction). Match pattern: T6-02 added `bot/services/extractor.py` + admin handler in commit `dcc2c67`. |

**Out of scope (file-touching):**
- `bot/handlers/admin.py` — left alone. Optional refactor to a shared admin filter is deferred to Phase 6.5.
- `bot/services/llm_gateway.py` — `/approve` is deterministic SQL only; NO gateway calls.
- `bot/services/forget_cascade.py` — already has `_p6_mvid_advisory_lock_id`; `/approve` imports the helper but adds nothing here. The cascade orchestrator's own `pg_advisory_xact_lock` insertion is also part of T6-04 acceptance (the spec is explicit: "The forget-cascade orchestrator … MUST acquire `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))` for the affected `message_version_id` as the FIRST step of each event apply" — §7 T6-04 bullet 5). This DOES touch `bot/services/forget_cascade.py::_process_one_event` to add the lock acquisition. See §6 below.

---

## §6. Forget-cascade orchestrator lock insertion (T6-04 cross-stream requirement)

Per §7 T6-04 acceptance bullet 5: `_process_one_event` (the de-facto `apply_forget_event` per §5.A.5) MUST acquire the advisory lock as the FIRST step of each event apply.

### Current state (from `bot/services/forget_cascade.py:780-878`)

```python
async def _process_one_event(session: AsyncSession, event) -> None:
    cascade_state: dict[str, Any] = dict(event.cascade_status or {})
    _SKIP_TARGET_TYPES = frozenset({"export"})
    try:
        for layer in CASCADE_LAYER_ORDER:
            ...
```

There is NO `pg_advisory_xact_lock` call. T6-04 adds it.

### Insertion point

```python
async def _process_one_event(session: AsyncSession, event) -> None:
    cascade_state: dict[str, Any] = dict(event.cascade_status or {})
    _SKIP_TARGET_TYPES = frozenset({"export"})
    try:
        # T6-04: acquire P6 advisory lock on every affected message_version_id
        # BEFORE any cascade layer runs. Same lock namespace as /approve §5.C
        # step 2. Auto-released on outer tx commit/rollback.
        mvids_to_lock = await _resolve_affected_mvids(session, event)
        for mvid in sorted(mvids_to_lock):  # deterministic order → no deadlock
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _p6_mvid_advisory_lock_id(mvid)},
            )
        for layer in CASCADE_LAYER_ORDER:
            ...
```

`_resolve_affected_mvids(session, event)` is a new helper in `forget_cascade.py` that mirrors the resolution logic already inlined in `_cascade_card_sources_on_forget` (lines 645-700): for `target_type='message'` → `[chat_message.current_version_id]`; for `target_type='message_hash'` → SELECT mvids by content_hash; for `target_type='user'` → SELECT mvids by user_id; for `target_type='export'` → empty list (cascade skipped).

### Why this is correct

Per `PHASE6_PLAN.md` §5.A.5 paragraph 1: "the `apply_forget_event(session, forget_event_id)` orchestrator MUST acquire `pg_advisory_xact_lock(mvid_lock_id)` for each affected `message_version_id` as the **FIRST operation in the transaction — before the `forget_events` INSERT and before ANY cascade call**".

Subtle nuance: `_process_one_event` is called AFTER the forget_event row already exists (it processes pending rows from `ForgetEventRepo.list_pending`). The "before the `forget_events` INSERT" sequencing applies to the canonical `apply_forget_event(session, forget_event_id)` orchestrator described in the spec; the current cascade-worker pattern in `bot/services/forget_cascade.py::run_cascade_worker_once` claims pending rows and dispatches `_process_one_event`. The race window the spec closes is between `/approve` and the cascade — the lock acquired here serializes them at the cascade-cascade-of-mvids granularity, which is sufficient regardless of where in the orchestrator chain the lock acquisition lands.

H-Cdx-2 round 1+2 race window closure: if `/approve` started before `_process_one_event` took the lock, `/approve` either acquires the lock first (cascade waits, sees no committed approval, cascade demotes the just-approved card via `_cascade_card_sources_on_forget`) or cascade acquires the lock first (`/approve` waits, sees the committed `forget_event`, R3-block fires). No intermediate visible state with approved-card-on-forgotten-source.

### Affect on existing tests

- `tests/services/test_forget_cascade.py` already passes today without lock. Adding lock acquisition is forward-compatible: `pg_advisory_xact_lock` on a SQLite test path is a no-op only if SQLite is the dialect (it's not — production is Postgres 16 only). The test harness MUST use Postgres for cascade tests; if any cascade test runs on SQLite (test-fallback path), the lock call will error.
- Mitigation: dialect-guard the lock call (`if session.bind.dialect.name == "postgresql": ...`). Or, more conservatively, leave SQLite-path tests untouched and rely on Postgres-only test for the lock semantics. The latter matches the codebase pattern (see `bot/db/models.py` `ddl_if(dialect="postgresql")` on `extraction_candidates` check).
- New test in `tests/services/test_advisory_lock_collision.py` validates the cross-stream guarantee.

---

## §7. Tests

### Unit (handler shape)

**`tests/handlers/test_admin_cards.py`:**
1. `/candidates` returns paginated pending list. Mock `ExtractionCandidateRepo.list_pending`.
2. `/candidates 2` reads page 2 (offset = 10).
3. `/candidates` from non-admin → silent denial.
4. `/approve <id>` happy path → all 8 steps execute; expected ORM/SQL calls captured.
5. `/approve <id>` candidate not found → user-facing error; no DB writes beyond the FOR UPDATE.
6. `/approve <id>` candidate already approved → `already_decided` error; no `extraction_decisions` row written (or check that the existing one's count stays the same).
7. `/approve <id>` empty `source_message_version_ids` → reject promotion; structured log; no `knowledge_cards` insert.
8. `/approve <id>` from non-admin → silent denial.
9. `/approve <id>` admin's `User.username` is NULL → fall back to `f"tg{user.id}"` (audit shadow non-null contract). Decision row stored.
10. `/reject <id>` happy path → status flip + decision row INSERT.
11. `/reject <id> abusive language detected` → reason stored verbatim.
12. `/reject <id>` already decided → error.
13. `/reject <id>` from non-admin → silent denial.

### Integration (Postgres)

**`tests/handlers/test_admin_cards_integration.py`:**
14. End-to-end `/approve` on a real seeded candidate + sources. Assertions:
    - `knowledge_cards` has 1 new row with `card_status='approved'`, both audit columns non-null.
    - `card_sources` has N rows matching the candidate's mvids.
    - `extraction_candidates.status='approved'`, `reviewed_by`, `reviewed_at` populated.
    - `extraction_decisions` has 1 row with `action='approved'`.
15. End-to-end `/approve` after a forget_event was inserted for one source mvid (R3 trigger):
    - All 8 steps run up to and including step 3.
    - Step 3 fires R3-block.
    - `extraction_decisions` is UNCHANGED (count before == count after).
    - `extraction_candidates.status='pending'` (unchanged).
    - Admin sees Telegram error reply.
    - Structured log emitted with `failure_reason='forget_tombstone_match'`.
16. End-to-end `/approve` when one source mvid has `chat_messages.is_redacted=TRUE`:
    - Step 4 R3-block fires.
    - Assertions same as #15 but `failure_reason='source_redacted'`.
17. End-to-end `/approve` when one source mvid's parent chat_message has `memory_policy='offrecord'`:
    - Step 4 R3-block fires.
    - `failure_reason='source_memory_policy_not_normal'`.

### Advisory-lock collision (T6-09 sub-gate; carried in T6-04 PR per acceptance bullet 6)

**`tests/services/test_advisory_lock_collision.py`:**
18. Spawn two coroutines: `_process_one_event` for a forget targeting mvid M, and `/approve` for a candidate whose source list contains M. Assert one blocks on `pg_locks` until the other commits. Post-commit, assert either:
    - `card_status='archived'` (cascade won the lock race; demote ran), OR
    - `extraction_decisions` has zero new rows AND `extraction_candidates.status='pending'` (R3-block fired; approve lost the race and rolled back its own work).
    - In neither timeline does `card_status='approved'` AND its source mvid match a committed `forget_event`.

Implementation pattern: use two `AsyncSession`s on separate connections. Coroutine A starts the cascade, hits the lock SQL, holds. Coroutine B kicks off `/approve` in parallel — it waits on `pg_locks`. Coroutine A commits. Coroutine B unblocks, runs step 3, R3-blocks. Then reverse the timing for the second test case.

19. Lock derivation symmetry: `_p6_mvid_advisory_lock_id(mvid)` is called from BOTH `/approve` and `_process_one_event` for the same `mvid`. Run a unit test that imports `bot.services.forget_cascade._p6_mvid_advisory_lock_id` from each caller's import path (no shadowing) and asserts identical output for sample mvids. (Prevents future refactor from forking the lock key.)

### Privacy lint

20. `scripts/lint_privacy_check.sh` MUST allowlist any new file that touches policy strings. Verify CI passes on the T6-04 PR head; if it fails on a path-not-in-allowlist false positive (rebase-fragile per `feedback-lint-privacy-rebase-fragility.md`), expand the allowlist in both `scripts/lint_privacy_check.sh` and the partner CI script.

---

## §8. Stop signals (specific to this ticket; aligns with PHASE6_PLAN.md §8)

- Card promotion bypassing admin review → STOP, governance breach. (Cannot happen here: every `/approve` requires the admin filter.)
- Card `body_markdown` containing quotes from offrecord or forgotten messages → STOP, invariant #3 violation. (`/approve` does NOT re-fetch source body; it only stores `candidate.candidate_json["body_markdown"]` which the extractor already governance-cleared at extraction time. T6-02 acceptance enforces extractor input filter.)
- `/recall` returning card content to a user without checking `card_status='approved'` → STOP. (T6-06 territory.)
- Card promotion attempted when any `card_sources` row's `message_version_id` matches a `forget_event` tombstone → R3-block path; structured log; no decision row. Final state: candidate remains `pending`.

---

## §9. Risk register

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R-04-01 | Admin double-approves the same candidate from two parallel chat sessions. | MED | `FOR UPDATE` on `extraction_candidates` row at step 1; second admin's transaction blocks until first commits, then second sees `status='approved'` and aborts. |
| R-04-02 | Admin's `User` row has `username=NULL`. | LOW | Fallback at handler time: `decided_by_username = user.username or f"tg{user.id}"`. Audit shadow remains NOT NULL per schema. |
| R-04-03 | Deadlock between `/approve` and forget cascade. | LOW | Mitigated by sorted lock acquisition order (smallest `mvid_lock_id` first). Both paths sort. |
| R-04-04 | Tombstone-key SQL drift across `/approve`, search, gateway, extractor. | MED | Centralise the tombstone-key construction in `bot/services/governance_revalidation.py` (single SQL source); future change is a one-line edit. Cross-check against `bot/services/search.py:99-108`, `bot/services/llm_gateway.py:_TOMBSTONE_GATE_SQL`, `bot/services/extractor.py:255-278`. |
| R-04-05 | `body_markdown` contains telegram MarkdownV2 reserved chars that crash the renderer when admin browses via `/card` (T6-05). | LOW | Extractor is responsible for emitting MarkdownV2-safe output; `/card` renderer SHOULD escape defensively. Not blocking for T6-04 — punt to T6-05. |
| R-04-06 | `_p6_mvid_advisory_lock_id` derivation forks (separate helper added accidentally). | HIGH | Test #19 ensures single source of truth. Add to `tests/services/test_advisory_lock_collision.py`. |
| R-04-07 | `/approve` partial state on crash mid-transaction. | LOW | Single DB transaction; crash before COMMIT rolls everything back. Advisory lock auto-released. |
| R-04-08 | `/approve` triggered by admin DURING T6-09 race window leaves `card_sources` rows pointing at later-forgotten mvids before cascade catches them. | LOW | The §5.A.5 cascade always finds the rows on its next pass; `_cascade_card_sources_on_forget` deletes them and demotes the card. Final consistency guaranteed (§5.C invariant). |
| R-04-09 | SQLite test path errors on `pg_advisory_xact_lock` literal SQL. | LOW | Dialect-guard the lock acquisition in `_process_one_event` (same pattern as `extraction_candidates` `ddl_if(dialect="postgresql")` in models). For `/approve` handler unit tests, mock the session entirely; integration tests run on real Postgres. |
| R-04-10 | Lint-privacy false-positive on new `admin_cards.py`. | LOW | Pre-emptively add to allowlist in the SAME PR; matches T6-02 hardening pattern (commit `dcc2c67`). |

---

## §10. Open questions

These need a decision before T6-04 PR can merge. None are blockers for design completion but may surface during round-1 review.

1. **`/candidates` query string parsing.** Aiogram `CommandObject.args` is a single string; `/candidates 2` → `args="2"`. Do we want richer filters like `/candidates --run=<run_id>`? Default: NO for T6-04, defer to T6-05/T6-08.
2. **Decision audit username fallback under FK SET NULL.** When `decided_by` is later set to NULL (user soft-delete), the `decided_by_username` snapshot survives. But if the admin's `username` was NULL at decision time AND we used `f"tg{user.id}"`, the snapshot is `tgXXX`. Acceptable? Spec is silent; recommend yes.
3. **Should `/approve` accept short-prefix UUIDs?** UX-friendly (e.g., `/approve a3f5b1c2`). Implementation: `WHERE id::text LIKE :prefix || '%'` with ambiguity check (raise error if >1 match). Default: YES for `/candidates`-listed prefixes; admins copy-paste short forms in practice.
4. **Bot reply on R3-block: include which mvid blocked?** Spec is explicit: structured log includes `mvid` or `forget_event_id`. Telegram reply: include yes — admins need it to investigate. NO body content (privacy invariant).
5. **`/reject` requires reason?** Spec is silent. Default: reason is optional. UX hint in `/candidates` output shows `/reject <id> [reason]`.
6. **Concurrent /reject vs cascade.** `/reject` does NOT modify any mvid-pointed state, so the advisory lock is unnecessary. Verify: a forget_event for the same mvid pool that the candidate referenced is processed by cascade WITHOUT touching `extraction_candidates`; the candidate stays `pending` (or in T6-04 PR, transitions to `rejected` because the admin chose so). No conflict. Recommend NO advisory lock in `/reject`.
7. **Audit shadow Telegram fields beyond `username`.** Should `decided_by_full_name` or `decided_by_first_name` also be snapshotted? Spec defines only `decided_by_username`. Default: stick with spec; richer audit can layer in Phase 6.5.

---

## §11. Out of scope for T6-04

- `/edit-card` command — deferred to Phase 6.5 per Q6 (PHASE6_PLAN.md §11).
- `/cards` and `/card <id>` browse — sibling T6-05.
- Search-side card inclusion (`include_cards`) — T6-06.
- `EvidenceItem.source_type` discriminator — T6-07.
- Web cards page — T6-08 (deferred).
- Final holistic integration test (T6-09 in full) — runs against the entire Wave 2 stack.
- Bulk-approve / bulk-reject — out of Phase 6 scope.

---

## §12. Evidence log (files read while drafting)

| File | Key facts extracted |
|---|---|
| `docs/memory-system/PHASE6_PLAN.md` | §5.C 8-step protocol verbatim; §5.A schema constraints; §7 acceptance criteria; §11 R3 + Q6 resolutions. |
| `bot/db/models.py` (1-1284) | T6-01 ORM models for ExtractionCandidate (line 988), KnowledgeCard (1079), CardSource (1153), ExtractionDecision (1219). DB constraints + indexes. |
| `bot/services/forget_cascade.py` (1-970) | `_p6_mvid_advisory_lock_id` at line 61 — single source of truth. `_cascade_card_sources_on_forget` resolution logic at line 645. `_process_one_event` cascade entrypoint at line 780. TODO(T6-04) marker at line 581 (orchestrator-level lock acquisition to add). |
| `bot/handlers/admin.py` | Existing admin filter pattern: `if message.from_user.id not in settings.ADMIN_IDS: return`. Private chat filter. |
| `bot/handlers/forget_reply.py` | Pattern for `User.is_admin` check, `ForgetEventRepo.create` audit. Reference for handler ordering before `chat_messages` catch-all (line 14-16). |
| `bot/handlers/forward_lookup.py:45` | `is_admin = requester.id in settings.ADMIN_IDS or requester.is_admin is True` — combined admin check pattern. |
| `bot/handlers/qa.py` (1-400) | Aiogram session middleware usage; `Command(...)` registration; `CommandObject` arg parsing; HTML rendering pattern. |
| `bot/services/search.py:91-110` | Three tombstone-key SQL construction — canonical. |
| `bot/services/llm_gateway.py:_TOMBSTONE_GATE_SQL` | Same three-key construction, mirrors search.py. |
| `bot/db/repos/qa_trace.py` | Repo pattern: flush-only, no commit; raise on missing row. Reference for new repos. |
| `bot/db/repos/feature_flag.py` | UPSERT pattern via `pg_insert.on_conflict_do_update` — reference if any cards repo needs upsert behavior. |
| `tests/evals/test_leakage.py` (1-300) | L3a/L3b/L3c tombstone seed patterns — reference for /approve integration tests that need a tombstone-seeded source. |
| `alembic/versions/032_add_knowledge_cards.py` | Confirms `body_tsv` is GENERATED STORED — handlers don't write to it. |

---

## §13. Implementation order (suggested)

1. New repos (`extraction_candidate.py`, `knowledge_card.py`, `card_source.py`, `extraction_decision.py`) + unit tests.
2. `bot/services/governance_revalidation.py` + unit tests.
3. `bot/handlers/admin_cards.py` with `/candidates` only + tests.
4. Add `/reject` to the handler + tests.
5. Add `/approve` to the handler — minimum viable transaction protocol (no advisory lock yet) + tests.
6. Add advisory-lock acquisition (step 2 of /approve protocol) + tests.
7. Add advisory-lock acquisition to `_process_one_event` (cascade orchestrator) + dialect guard + integration tests.
8. Add `test_advisory_lock_collision.py` cross-stream test.
9. Update `scripts/lint_privacy_check.sh` allowlist if needed.
10. Update `IMPLEMENTATION_STATUS.md`.
11. Run Phase 11 binding suite locally on PR head (`tests/evals/test_leakage.py`, `test_citations.py`, `test_refusal.py`, `test_no_llm_imports.py`); commit + push.

---

END of T6-04 design.
