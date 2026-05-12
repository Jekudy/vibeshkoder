# Phase 6 — Knowledge Cards / Catalog: Plan

**Status:** Ratified 2026-05-12 (Sprint 0)
**Cycle:** Memory system Phase 6
**Date:** 2026-05-12
**Predecessor:** Phase 5 closed 2026-05-11 (commit `c7a10b4`).
**Migration window:** 030–034 (5 migrations).
**Critical invariant for this phase:** Cards become approved citation sources only after human admin review.

---

## §0. Implementation Status

Sprint 0 (this document) — **Ratified 2026-05-12**. Design gate closed; Wave 1 authorized.

Tickets:

- **T6-00** — Phase 5 FHR carryover (M-1 + M-4). Pending. Must merge before Wave 1.
- **T6-01** — Schema migrations 030–034 + ORM. Pending. Wave 1 / Stream A.
- **T6-02** — Extractor service pass. Pending. Wave 1 / Stream B.
- **T6-03** — `llm_gateway.extract_candidates()` contract. Pending. Wave 1 / Stream B. Must clear Phase 11 leakage binding test before merge.
- **T6-04** — Admin candidate review commands (`/candidates`, `/approve`, `/reject`). Pending. Wave 2 / Stream C.
- **T6-05** — Admin card browsing commands (`/cards`, `/card`). Pending. Wave 2 / Stream C.
- **T6-06** — Search extension for approved cards. Pending. Wave 2 / Stream D.
- **T6-07** — `EvidenceItem` source discriminator. Pending. Wave 2 / Stream D.
- **T6-08** — Optional read-only web cards page. Pending. Wave 3 / Stream E.
- **T6-09** — Integration test (candidate → card → recall). Pending. Wave 2 closeout.

**Migrations explicitly:**

1. `030_add_extraction_runs`
2. `031_add_extraction_candidates`
3. `032_add_knowledge_cards`
4. `033_add_card_sources`
5. `034_add_extraction_decisions`

Phase 5 closed 2026-05-11 (commit `c7a10b4`). Phase 11 binding suite is ACTIVE — `tests/evals/test_leakage.py` / `test_citations.py` / `test_refusal.py` / `test_no_llm_imports.py` gate Phase 6 work.

---

## §1. Invariants

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources (FK-normalized via `card_sources`).
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.
10. Public wiki remains disabled until review / source trace / governance are proven.

---

## §2. Phase 6 Spec

- **Objective:** curated catalog with review and source trace.
- **Scope:** `extraction_runs`, `extraction_candidates`, `knowledge_cards`, `card_sources`, `extraction_decisions`; admin review handlers; search extension for approved cards.
- **Dependencies:** Phase 5 (LLM gateway + usage ledger).
- **Entry criteria:** Sprint 0 ratified; Phase 5 FHR carryovers (M-1, M-4) closed via T6-00.
- **Exit criteria:** admin can approve cards with citations; `/recall` may quote `card_status='approved'` cards with source trace; Phase 11 binding suite green.
- **Acceptance:** card cannot become active without source; visibility enforced.
- **Risks:** extractions becoming "truth" without review; governance bypass at `/approve` time.
- **Rollback:** demote via `card_status='archived'` with `archived_reason`; cascade demote on forget per §5.A.5.

---

## §3. Phase 7 Boundary

- **Daily summaries / digests (Phase 7):** OUT OF SCOPE because summaries are derived recaps and must consume approved sources; they are never canonical truth.
- **Reflection runs (Phase 8):** OUT OF SCOPE because analytical reflection requires a stable reviewed catalog and separate governance around generated insights. Phase 8's `memory_candidates` (reflection cluster queue) is a **distinct concept** from Phase 6's `extraction_candidates` (LLM-extracted card candidate queue) — see §10 glossary.
- **Wiki (Phase 9):** OUT OF SCOPE because editable/community-facing catalog pages require visibility filters, source trace, and review workflows beyond card approval.
- **Graph (Phase 10):** OUT OF SCOPE because graph projection is derived only, rebuildable from Postgres, and must wait for stable card/event relations.

---

## §4. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│ Scheduler                                                     │
│ - cron stub (DISABLED via extraction.scheduler.enabled=false) │
│ - operator-triggered pass (default)                           │
└──────────────────────────────┬───────────────────────────────┘
                               │ triggers
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ bot/services/extractor.py                                    │
│ run_extraction_pass(session, window_start, window_end)        │
└──────────────────────────────┬───────────────────────────────┘
                               │ reads only
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ chat_messages                                                 │
│ WHERE memory_policy='normal'                                  │
│ JOIN current message_versions                                 │
└──────────────────────────────┬───────────────────────────────┘
                               │ evidence-bundle context
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ llm_gateway.extract_candidates()                              │
│ - single audited LLM path                                     │
│ - no forbidden source content                                 │
└──────────────────────────────┬───────────────────────────────┘
                               │ writes
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ extraction_candidates                                         │
│ status='pending'                                              │
│ source refs land in card_sources at /approve time             │
└──────────────────────────────┬───────────────────────────────┘
                               │ surfaced through admin commands
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Admin Telegram review                                         │
│ /candidates, /approve, /reject, /cards, /card                 │
└───────────────┬───────────────────────────────┬──────────────┘
                │ /approve                       │ /reject
                ▼                                ▼
┌────────────────────────────────┐   ┌─────────────────────────┐
│ knowledge_cards + card_sources  │   │ extraction_decisions    │
│ card_status='approved'          │   │ action='rejected'       │
│ citation-eligible               │   │ audit trail             │
└────────────────┬───────────────┘   └─────────────────────────┘
                 │
                 │ extends Phase 4+5 recall
                 ▼
┌──────────────────────────────────────────────────────────────┐
│ bot/services/search.py                                       │
│ search_messages(..., include_cards=True)                     │
│ queries BOTH message_versions AND knowledge_cards             │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ EvidenceItem                                                  │
│ source_type: Literal['message', 'card']                       │
│ message hits cite message_version_id                          │
│ card hits cite card_sources rows (FK→message_versions)        │
└──────────────────────────────────────────────────────────────┘
```

---

## §5. Component Design

### 5.A. Migrations

Phase 4 used ~018–021 and Phase 5 used 022–025. Phase 6 owns 030–034. Migration numbers 026–029 are reserved / intentionally unused — Phase 5 closure landed at 025, and Phase 6 starts at 030 to leave room for any post-Phase-5 hotfix migrations without forcing a renumber. If unused at Phase 6 close, the gap is documented in the Phase 6 closure PR as "reserved, no migrations issued".

**030_add_extraction_runs**

`extraction_runs`:

- `id uuid primary key`
- `ingestion_window_start timestamptz`
- `ingestion_window_end timestamptz`
- `candidate_count int not null default 0`
- `run_status text not null check (run_status in ('running','completed','failed'))`
- `llm_usage_ledger_id bigint nullable` — FK to Phase 5 `llm_usage_ledger.id` (which is BIGINT in Phase 5 schema).
- `created_at timestamptz not null default now()`

Constraints:

- `candidate_count >= 0`
- If `run_status='completed'`, `ingestion_window_start` and `ingestion_window_end` must be non-null.

**031_add_extraction_candidates** (renamed from DRAFT's `memory_candidates` per D2)

`extraction_candidates`:

- `id uuid primary key`
- `extraction_run_id uuid references extraction_runs(id) on delete set null`
- `candidate_json jsonb not null`
- `source_message_version_ids jsonb not null default '[]'::jsonb` — staging only on the candidate row; promoted to `card_sources` rows at `/approve` time.
- `status text not null check (status in ('pending','approved','rejected','superseded'))`
- `reviewed_by bigint references users(id) on delete set null`
- `reviewed_at timestamptz`
- `created_at timestamptz not null default now()`

Constraints:

- `source_message_version_ids` must be a JSON array.
- `status='pending'` implies `reviewed_by is null` and `reviewed_at is null`.
- `status in ('approved','rejected','superseded')` implies `reviewed_by is not null` and `reviewed_at is not null`.

**032_add_knowledge_cards**

`knowledge_cards`:

- `id uuid primary key`
- `title text not null`
- `body_markdown text not null` — stored as Telegram MarkdownV2 per Q1.
- `body_tsv tsvector generated always as (to_tsvector('russian', coalesce(body_markdown, ''))) stored` — `'russian'` config matches the Phase 4 baseline (`alembic/versions/020_add_message_versions_fts.py:36`, `021_align_message_versions_search_tsv.py:38,62`, `bot/db/models.py:43`); RU/EN mixed content is handled identically to `message_versions.search_tsv`.
- `card_status text not null check (card_status in ('draft','approved','archived'))` — `deprecated` collapsed into `archived` per Q3.
- `archived_reason text` — nullable; populated when `card_status='archived'`.
- `approved_by_user_id bigint references users(id) on delete set null`
- `approved_at timestamptz`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Indexes:

- GIN index on `body_tsv`.

Constraints:

- `card_status='approved'` implies `approved_by_user_id is not null` and `approved_at is not null`.
- `card_status!='approved'` rows are not citation-eligible.
- Non-empty source-set requirement moves to `card_sources` (see 033 + §5.A.5); it is no longer a column-level constraint on `knowledge_cards`.

Note: `source_message_version_ids jsonb` is **NOT** a column on `knowledge_cards` (D1). Source links live in `card_sources` (033).

**033_add_card_sources** (NEW per D1)

`card_sources`:

- `id uuid primary key`
- `card_id uuid not null references knowledge_cards(id) on delete cascade`
- `message_version_id integer not null references message_versions(id) on delete restrict`
- `position integer not null default 0`
- `created_at timestamptz not null default now()`

Constraints / indexes:

- `UNIQUE(card_id, message_version_id)`
- Index on `message_version_id` (reverse lookup for cascade demote in §5.A.5).

Rationale (D1): FK-normalized sources satisfy invariant #4 (governance-grade citation trace). Forget cascade can target `message_version_id` directly without scanning JSONB arrays. `ON DELETE RESTRICT` on `message_version_id` prevents accidental orphan deletes; the §5.A.5 cascade explicitly DELETES the `card_sources` row (not the underlying `message_versions` row).

**034_add_extraction_decisions** (renumbered from DRAFT's 033)

`extraction_decisions`:

- `id uuid primary key`
- `candidate_id uuid references extraction_candidates(id) on delete cascade`
- `action text not null check (action in ('approved','rejected'))` — log-only R3-block is NOT a decision (see §5.C / §8); no row is written when re-validation aborts approval.
- `reason text`
- `decided_by bigint references users(id) on delete set null` — soft-delete tolerant.
- `decided_by_username text not null` — audit shadow, snapshot at decision time.
- `decided_at timestamptz not null default now()`
- `created_at timestamptz not null default now()`
- `UNIQUE (candidate_id)` — exactly one terminal decision per candidate; appeals not in scope (see §11).

Note: `decided_by` is nullable to survive user soft-delete via `ON DELETE SET NULL`; `decided_by_username` preserves the human-readable audit trail. `UNIQUE(candidate_id)` enforces the single-terminal-decision invariant; if appeals are later required, the migration path is to add a `decision_seq INT` column and switch to a composite `UNIQUE(candidate_id, decision_seq)`.

### 5.A.5. Cascade extension on forget_events (`_cascade_card_sources_on_forget`)

**Transaction-level lock acquisition (P6 contract for `apply_forget_event`):** the `apply_forget_event(session, forget_event_id)` orchestrator MUST acquire `pg_advisory_xact_lock(mvid_lock_id)` for each affected `message_version_id` as the **FIRST operation in the transaction — before the `forget_events` INSERT and before ANY cascade call** (including `_cascade_qa_traces_llm`). `mvid_lock_id = signed_int64(sha256(f'p6:mvid:{mvid}'))`. xact-scoped → auto-release on COMMIT/ROLLBACK; no manual release.

```
BEGIN  # apply_forget_event transaction
SELECT pg_advisory_xact_lock(:mvid_lock_id_1), pg_advisory_xact_lock(:mvid_lock_id_2), ...
  # Lock acquired BEFORE any data mutation. Any concurrent /approve targeting
  # one of these mvids now waits until this transaction commits or rolls back.
INSERT INTO forget_events (...)
_cascade_qa_traces_llm(...)            # Phase 5 cascade — runs UNDER the P6 lock.
_cascade_llm_synthesis_cache(...)      # Phase 5 cascade — runs UNDER the P6 lock.
_cascade_card_sources_on_forget(...)   # P6 cascade — runs UNDER the same lock.
COMMIT  # lock auto-released; concurrent /approve unblocks and re-reads forget_events.
```

This ordering closes the race window Codex flagged in round 2/3: if `/approve` started before `apply_forget_event` took the lock, `/approve` either (a) acquired the lock first → `apply_forget_event` waits → `/approve`'s `SELECT forget_events` sees no row, approval proceeds, then on commit `apply_forget_event` acquires the lock and the cascade demotes the just-approved card to `archived` (final state honors Invariant #4), or (b) `apply_forget_event` acquired the lock first → `/approve` waits → `apply_forget_event` inserts forget_event + cascades + commits → `/approve` unblocks, its `SELECT forget_events` now sees the committed row → R3 abort with no `extraction_decisions` row.

Under no interleaving can `/recall` return an approved card pointing to a forgotten source: either the card is archived before commit, or its approval is aborted before commit. No intermediate visible state with approved-card-on-forgotten-source.

`_cascade_card_sources_on_forget` (the P6-specific function called from step 3 above) does:

1. Acquires `SELECT ... FOR UPDATE` on every affected `knowledge_cards` row BEFORE mutating `card_sources` (defence-in-depth row-level lock; covers cards inserted under a *previous* P6 lock that has since released).
2. DELETEs every `card_sources` row whose `message_version_id` matches the forgotten version.
3. For each affected `card_id`, recounts remaining `card_sources` rows.
4. If remaining count drops to **0**, demote the card: `card_status='archived'`, `archived_reason='all sources forgotten via cascade <forget_event_id>'`, `updated_at=now()`.
5. If remaining count > 0, leave the card in its current state (the lost source is simply unlinked; card content may now be partially un-attributable — flag for later admin review).

Implementation: `_cascade_card_sources_on_forget` lives in `bot/services/forget_cascade.py` alongside Phase 5 `_cascade_qa_traces_llm` / `_cascade_llm_synthesis_cache`. The advisory xact lock is taken by `apply_forget_event` orchestrator (NOT by the individual cascade function) — this is the canonical serialization point with `/approve` (§5.C step 2). `_cascade_card_sources_on_forget` runs AFTER `_cascade_qa_traces_llm` (so `qa_traces` are NULL'd before card-source row deletion affects citation_ids lookup) and BEFORE commit. Pattern reference: `docs/memory-system/import-chunking.md::acquire_advisory_lock` (same SHA-256 → int64 derivation; different namespace prefix).

**Demote, not hard-delete (R2):** Cards are durable artifacts; demote to `archived` preserves audit trail. This is parallel to Phase 5's `_cascade_llm_synthesis_cache.invalidate_by_citation` but with demote semantics instead of hard-delete (Phase 5 cache rows are ephemeral; Phase 6 cards are not).

**Privacy invariant:** `archived_reason` MUST NOT contain quoted body content from the forgotten message; it carries only the `forget_event_id` reference.

### 5.B. `bot/services/extractor.py`

**Public API:** async function `run_extraction_pass(session, *, window_start, window_end)`.

Behaviour:

- Reads `chat_messages` filtered by `memory_policy='normal'` and `created_at` within the requested window.
- Joins only current `message_versions` rows and excludes redacted/forgotten content through the same governance filters as `/recall`.
- Forward-only (Q5): only processes `chat_messages.created_at >= phase_6_enabled_at`. Historical backfill is operator-explicit via `/admin/extract --window <start> <end>` only.
- Builds evidence-bundle context from message text/caption, message metadata, and source `message_version_id`s.
- Calls `llm_gateway.extract_candidates()`; no direct provider SDK calls are allowed.
- Writes one `extraction_runs` row and zero or more `extraction_candidates` rows with `status='pending'`.
- Returns `ExtractionResult` with counts: read messages, eligible source versions, candidates written, rejected by validation, and run status.

Stop conditions:

- If any selected source row has `memory_policy!='normal'`, `is_redacted=true`, or a matching `forget_event` tombstone, the pass stops and records a failed `extraction_runs` row.

Invariant: the LLM call is forbidden whenever ANY source row in the evidence bundle has `memory_policy != 'normal'`. The guard runs **before** `llm_gateway.extract_candidates()` is invoked.

### 5.C. Admin Telegram handlers

Commands (T6-04 + T6-05):

- `/candidates` — admin-only, paginated list of pending `extraction_candidates`.
- `/approve <candidate_id>` — admin-only atomic promotion to `knowledge_cards`.
- `/reject <candidate_id> [reason]` — admin-only rejection; marks candidate rejected and writes an `extraction_decisions` row.
- `/cards` — admin-only paginated list of approved cards.
- `/card <id>` — admin-only card detail with back-citations to source messages via `card_sources`.

`/approve` rules:

- Runs in one DB transaction.
- Changes `extraction_candidates.status` to `approved`, inserts `knowledge_cards` row with `card_status='approved'`, inserts one `card_sources` row per `message_version_id` from the candidate's staging `source_message_version_ids`, fills `approved_by_user_id` and `approved_at`, and writes `extraction_decisions.action='approved'`.
- **Re-validates governance (R3):** for each candidate `message_version_id`, re-runs the deterministic governance filter (SQL only — **no LLM re-prompt**). If ANY source has `memory_policy != 'normal'` OR `is_redacted=true` OR is covered by ANY matching `forget_event` tombstone (regardless of age) → BLOCK approval; abort the transaction; **NO `extraction_decisions` row is written** (R3-block is a precondition failure, not a decision — see M-Pro-4 below and §8). Structured log only.
- Must reject promotion if the candidate has no source message versions.

`/approve` transaction protocol:

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

**Serialization invariant:** the advisory xact lock in step 2 MUST be acquired BEFORE the `forget_events` check in step 3 — otherwise an `apply_forget_event` running concurrently could land between the SELECT and the INSERT in step 6 (closing H-Cdx-2 round 1+2 race window). Lock ordering with `forget_cascade.py` MUST match exactly: both transactions hash the same `mvid` to the same `mvid_lock_id` via the same `f'p6:mvid:{mvid}'` namespace; deadlock-free because the lock is acquired before any data-mutating SQL.

R3-block behavior: when re-validation rejects approval, NO row is written to `extraction_decisions`. The candidate's `status` remains `pending`. Failure is logged via structured logger with fields: `event=approve_blocked`, `candidate_id`, `admin_user_id`, `failure_reason` (e.g., `forget_tombstone_match`, `source_redacted`, `source_memory_policy_not_normal`), and `forget_event_id` or `message_version_id` that triggered the block. The admin sees a Telegram error message explaining the block but no permanent state changes occur. The candidate can re-enter `/candidates` only if its governance state changes externally (currently out of scope; see §11 R3 row).

`/edit-card` is **deferred to Phase 6.5** (Q6). T6-04 ships strict `/approve` + `/reject` only.

### 5.D. Search extension (`bot/services/search.py`)

`search_messages` gains `include_cards: bool = True`.

Behaviour:

- Existing message search remains unchanged when `include_cards=False`.
- When `include_cards=True`, the service runs the current FTS query against `message_versions` and a second GIN FTS query against `knowledge_cards.body_tsv`.
- Card query filters `knowledge_cards.card_status='approved'`.
- `EvidenceItem` gains `source_type: Literal['message', 'card']`.
- Message evidence keeps `message_version_id`.
- Card evidence carries `card_id` plus the `message_version_id` set joined from `card_sources` for citation trace.
- Scorer weighs approved card hits slightly higher than raw message hits because cards are admin-reviewed authority.

Contract:

- `/recall` may quote card content only when `card_status='approved'`.
- A card citation must still expose source trace back to `message_versions` via `card_sources`, satisfying invariant #4.

### 5.E. Web UI scaffolding (optional / deferrable)

Optional read-only admin page: `web/templates/cards.html`.

Behaviour:

- Lists approved cards only.
- Shows title, short body preview, status, approval metadata, and source count.
- Links to card detail if Phase 5 web scaffolding already exists.

Deferral:

- Create/edit workflows are deferred to Phase 9 wiki.
- Implement only if Phase 5 web scaffolding already exists; otherwise keep Phase 6 review inside Telegram admin commands.

### 5.F. T6-00 FHR Carryover Scope (Phase 5 closure follow-ups)

T6-00 must land **before Wave 1** ships. It closes the Phase 5 FHR MEDIUM carryovers ratified in the Phase 5 closure PR.

**M-1 — `synthesize_answer.qa_trace_id` Protocol-aligned annotation:**

- File: `bot/services/llm_gateway.py:332`.
- Change: tighten the annotation `qa_trace_id: int → qa_trace_id: int | None`. NO runtime guard.
- Reason: the Sprint 0 round-1 design (runtime `raise ValueError(qa_trace_id is None)` guard) was reverted after T6-00 round-1 CI surfaced that Phase 5 T5-05 eval fixtures (`tests/eval/test_qa_llm_eval_cases.py`) deliberately pass `qa_trace_id=None` for 8 abstention-path coverage cases (empty bundle, all-filtered, budget exceeded, provider transient/permanent error, cache hit on filtered citation, citation hallucination, valid synthesis without persistence). These are valid gateway call paths — the downstream `LedgerRepoProtocol.record` Protocol surface accepts `qa_trace_id: int | None` (already shipped in T5-03; see `bot/services/llm_gateway.py:123`), and the gateway's `_ledger` closure forwards whatever it receives. A jealous runtime guard would break documented test coverage of abstention paths.
- Production handler at `bot/handlers/qa.py:312–334` always creates the `QaTrace` row via `QaTraceRepo.create` BEFORE calling the gateway and passes `trace.id` (non-None) — this single-call-site contract is preserved at the handler layer, not enforced at the gateway boundary. Cascade FK direction (Codex Phase 5 round-1 HIGH 4 closure) is satisfied via the handler-layer invariant.
- Update `synthesize_answer` docstring §Parameters to (a) state that `None` is permitted at the gateway boundary because the ledger Protocol surface accepts `int | None`, (b) cite the production single-call-site invariant, and (c) cite the test-fixture usage that motivates the `int | None` annotation.

Note on the Phase 5 FHR carryover wording: the original M-1 carryover said "tighten `qa_trace_id: int → int | None` OR add runtime `assert qa_trace_id is not None`". The OR was a genuine choice. Sprint 0 (commits `9590dfa`+`55eb677`) selected the runtime-guard branch; T6-00 round-1 CI revealed this conflicts with shipped test coverage. Selecting the annotation branch honors the FHR's first listed option and matches the existing T5-03 Protocol shape.

**M-4 — direct cascade `message_hash` sub-case tests:**

- Test file: `tests/services/test_forget_cascade.py` (extends existing module).
- Implementations under test: `bot/services/forget_cascade.py::_cascade_qa_traces_llm` and `bot/services/forget_cascade.py::_cascade_llm_synthesis_cache`.
- Add a **direct** unit test for `_cascade_qa_traces_llm` with `target_type='message_hash'` (not via the end-to-end forget pipeline).
- Add a **direct** unit test for `_cascade_llm_synthesis_cache` with `target_type='message_hash'`.
- Both tests MUST assert `qa_traces.llm_response_summary IS NULL` on the affected row post-cascade. Existing direct-case (`target_type='message_version'`) tests are kept.

**L-1 (defer to follow-up, NOT required for T6-00 close):**

- `forget_cascade._cascade_qa_traces_llm` `message_hash` path performs per-row UPDATE; batch UPDATE optimisation noted as a future perf TODO.

**L-4 (cosmetic, defer):**

- `alembic/versions/025_...` downgrade has `import hashlib` placed mid-function instead of module-top. Cosmetic; defer.

**T5-03 docs follow-up (docs-only, defer):**

- `docs/memory-system/phase5/contracts.md` §§5.1, 10.1, 12.2 specify `update_placeholder -> None` but implementations return rowcount. Drift; docs-only fix.

**T5-04 docs follow-up (docs-only, defer):**

- `docs/memory-system/phase5/contracts.md` §12.3 specifies `update_llm_fields -> None` but implementation returns rowcount and raises `LookupError` on miss. Drift; docs-only fix.

---

## §6. Stream Allocation

### Pre-Wave 1 — Phase 5 FHR carryover (sequential)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **T6-00** | Single ticket, must merge first | M-1 annotation `int → int | None` + docstring; M-4 direct cascade tests | Phase 5 closure (done) |

### Wave 1 — independent foundations (PARALLEL)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **A** | Schema migrations 030–034 | `extraction_runs`, `extraction_candidates`, `knowledge_cards`, `card_sources`, `extraction_decisions` | T6-00 |
| **B** | Extractor service skeleton + `llm_gateway.extract_candidates` | `bot/services/extractor.py`, Phase 5 gateway extension | T6-00 |

**T6-03 binding sub-gate:** the LLM gateway `extract_candidates` PR must pass the Phase 11 leakage binding suite green **on the T6-03 PR head specifically before merge**. This is a critical sub-gate because `extract_candidates` is a new LLM entry point and must clear privacy invariants #2 / #3. Required cases: L1, L2, L3a, L3b, L3c, L4, L5 in `tests/evals/test_leakage.py::test_leakage_invariants`; plus R1, R2, R3, R4 in `tests/evals/test_refusal.py`. The CI nightly `evals.yml` workflow result alone is NOT sufficient — re-run must be triggered on the T6-03 PR head.

### Wave 2 — product surfaces (PARALLEL)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **C** | Admin Telegram handlers | `/candidates`, `/approve`, `/reject`, `/cards`, `/card` | Stream A |
| **D** | Search extension + EvidenceItem discriminator | `include_cards`, `source_type`, card scoring | Stream A |

### Wave 3 — optional web surface (SEQUENTIAL)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **E** | Web read-only cards page | `web/templates/cards.html` | Streams C+D; defer if Phase 5 web scaffolding is absent |

### Wave summary

```
Pre-Wave 1:         T6-00
                      │
                      ▼
Wave 1 (parallel):  A      B
                    │      │
                    ▼      ▼
Wave 2 (parallel):  C      D
                    │      │
                    └──┬───┘
                       ▼
Wave 3 (optional):     E
```

---

## §7. Tickets T6-XX

### T6-00: Phase 5 FHR carryover (M-1 + M-4)

- Scope: `bot/services/llm_gateway.py:332` annotation tightening `qa_trace_id: int → int | None`; `synthesize_answer` docstring update; direct `_cascade_qa_traces_llm` + `_cascade_llm_synthesis_cache` `message_hash` sub-case tests with `llm_response_summary IS NULL` assertions + ledger budget aggregate preservation asserts.
- Acceptance criteria:
  - `qa_trace_id` annotation tightened to `int | None` matching `LedgerRepoProtocol.record` Protocol surface and existing T5-05 eval fixture coverage. No runtime guard.
  - Docstring §Parameters explains: (a) `None` is permitted at the gateway boundary, (b) production handler `bot/handlers/qa.py:312-334` always passes non-None as a handler-layer invariant, (c) T5-05 abstention-path fixtures pass None.
  - Two new direct cascade tests added; both pass; both assert `llm_response_summary IS NULL` post-cascade.
  - `_cascade_qa_traces_llm` test also asserts ledger budget aggregates (`cost_usd`, `tokens_in`, `tokens_out`, `latency_ms`) are preserved (this cascade scopes only `qa_traces`, not `llm_usage_ledger`).
  - Phase 11 binding suite remains green.
  - All Phase 5 T5-05 eval cases (`tests/eval/test_qa_llm_eval_cases.py`) remain green — the abstention-path None inputs MUST continue to work.
- Dependencies: none (closes Phase 5 FHR carryover).
- Stream: Pre-Wave 1 (sequential, must land first).

### T6-01: Phase 6 schema migrations

- Scope: Alembic migrations **030–034** (five migrations):
  - `030_add_extraction_runs`
  - `031_add_extraction_candidates`
  - `032_add_knowledge_cards`
  - `033_add_card_sources`
  - `034_add_extraction_decisions`

  Plus ORM models in `bot/db/models.py`.
- Acceptance criteria:
  - All five migrations apply and roll back cleanly on Postgres 16.
  - All checks/FKs/defaults in §5.A are present.
  - `knowledge_cards.body_tsv` uses `to_tsvector('russian', ...)` and has a GIN index.
  - `card_status` CHECK is exactly `('draft','approved','archived')` (no `deprecated`).
  - `card_status='approved'` cannot exist without `approved_by_user_id` + `approved_at`.
  - `card_sources` has `UNIQUE(card_id, message_version_id)` and reverse index on `message_version_id`.
  - `_cascade_card_sources_on_forget` extension wired per §5.A.5.
  - Advisory-lock helper `_p6_mvid_advisory_lock_id(mvid: int) -> int` defined in `bot/services/forget_cascade.py` (or a shared `bot/services/_advisory_locks.py`): returns `signed_int64(sha256(f'p6:mvid:{mvid}'))`. MUST be the single source of truth for lock_id derivation — both `/approve` (§5.C step 2) and the forget-cascade orchestrator (§5.A.5 step 1) MUST import and call this same helper when computing the lock key. Note: the actual `pg_advisory_xact_lock(...)` call sites land in T6-04 (not T6-01); T6-01 delivers the helper and proves determinism + signed-int64 range via unit tests.
- Dependencies: T6-00.
- Stream: Wave 1 / Stream A.

### T6-02: Extractor service pass

- Scope: `bot/services/extractor.py`, `ExtractionResult`, DB reads/writes, feature flag wiring, admin window CLI/handler.
- Acceptance criteria:
  - `run_extraction_pass(session, *, window_start, window_end)` exists.
  - Reads only `chat_messages.memory_policy='normal'` AND `created_at >= phase_6_enabled_at` (forward-only).
  - Writes `extraction_candidates.status='pending'`.
  - Records `extraction_runs.run_status` and `candidate_count`.
  - Refuses to invoke `llm_gateway.extract_candidates` if any source row in the evidence bundle has `memory_policy != 'normal'`.
  - Feature flag key constant `MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG = 'memory.extraction.scheduler.enabled'` defined alongside the `LLM_SYNTHESIS_FEATURE_FLAG` constant (`bot/handlers/qa.py:LLM_SYNTHESIS_FEATURE_FLAG` pattern).
  - Extractor scheduler entry-point reads the flag via `await FeatureFlagRepo.get(session, MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG)`; default behavior (key absent or false) = scheduler disabled. NO alembic data migration to seed the key (forward-only enable).
  - Unit tests cover flag=False (skip pass), flag=True (run pass), flag=unset (default skip).
  - Admin-only Telegram handler `/admin/extract --window <ISO8601-start>..<ISO8601-end>` invocable from the admin command surface.
  - Window string parser validates ISO8601 + range non-empty + window length ≤ 30 days (LLM budget guard).
  - Calls `run_extraction_pass(session, window_start=..., window_end=...)` directly, bypassing the scheduler flag.
  - Records an `ExtractionRun` row with explicit operator `user_id` audit marker.
  - Returns a summary message to the admin (candidates emitted, `run_status`, `llm_usage_ledger` entry id).
- Notes / known limitations:
  - **Router registration deferred to T6-03** alongside concrete gateway DI. Both `admin_extract.router` and the scheduler tick wire concrete `ExtractCandidatesGateway` via DI middleware — neither is registered in `bot/__main__.py` from T6-02 alone (T6-02 ships only the Protocol seam + handlers).
  - **`phase_6_enabled_at` is not monotonic** (Codex MED #1). The value is `FeatureFlag.updated_at`, which regresses if the flag is toggled OFF → ON. This is intentional for the operator-explicit backfill workflow (Q5): re-enabling treats the new timestamp as the new lower bound, and operators cover any OFF window using `/admin_extract --window` for the gap. A durable monotonic watermark may be added in a follow-up if append-only audit is later required.
  - **Atomic 3-stage lifecycle (Codex HIGH #3)**: `run_extraction_pass` INSERTs `run_status='running'` BEFORE the gateway call, wraps the call in `session.begin_nested()` (SAVEPOINT), and transitions to `completed` or `failed` based on outcome. Gateway crashes leave a durable `failed` audit row.
  - **Privacy invariant #4 (Codex CRITICAL #1)**: if the gateway returns `llm_usage_ledger_id=None` with candidates, the pass fails closed (`failure_reason='no_llm_ledger_entry'`). Empty-bundle short-circuit (no gateway call) is exempt.
  - **SELECT→gateway race guard (Codex CRITICAL #3)**: `_bundle_is_clean` re-queries `forget_events` for fresh tombstones AFTER `_select_eligible_sources` and BEFORE the gateway call. Closes the same race-window class as H-Cdx-2.
  - **Scheduler tick idempotency (Codex HIGH #4)**: `extraction_scheduler_tick` acquires `pg_try_advisory_xact_lock` on `_p6_scheduler_lock_id()` (constant `p6:extraction_scheduler` namespace, disjoint from `p6:mvid:`). Second concurrent tick returns `skipped=True, reason='locked'`.
  - **Operator audit marker (Codex HIGH #5 + alembic 035)**: `extraction_runs.operator_user_id BIGINT NULLABLE` durably records the admin who triggered `/admin_extract`. NULL = scheduler-driven.
- Dependencies: T6-00, T6-01.
- Stream: Wave 1 / Stream B.

### T6-03: LLM gateway candidate extraction method

- Scope: Phase 5 `llm_gateway`; add `extract_candidates()` contract.
- Acceptance criteria:
  - No provider SDK call exists outside `llm_gateway`.
  - Every call is associated with the Phase 5 LLM usage ledger.
  - Output schema includes candidate payload and source `message_version_id`s.
  - Forbidden source content cannot be passed to the gateway.
  - **Router registration**: register `bot.handlers.admin_extract.router` in `bot/__main__.py` `dp.include_routers(...)` adjacent to `admin.router` (deferred from T6-02 alongside the concrete gateway DI).
  - **Gateway DI wiring**: wire the concrete `ExtractCandidatesGateway` instance into BOTH the `admin_extract` handler call site AND the `extraction_scheduler_tick` call site. Use the existing aiogram DI middleware pattern (same as the Phase 5 LLM gateway wiring) so per-request session + gateway both reach handlers as kwargs. The Protocol decorator `@runtime_checkable` (added in T6-02) enables a defensive `isinstance(gw, ExtractCandidatesGateway)` validation at wire time if desired.
  - **Phase 11 leakage binding test green on the T6-03 PR head before merge** — critical sub-gate per §6. The sub-gate requires ALL cases L1, L2, L3a, L3b, L3c, L4, L5 in `tests/evals/test_leakage.py::test_leakage_invariants` green, plus R1, R2, R3, R4 refusal cases (`tests/evals/test_refusal.py`) green. The CI nightly `evals.yml` workflow result alone is NOT sufficient — a re-run must be triggered on the T6-03 PR head specifically.
- Dependencies: Phase 5 gateway/ledger, T6-00, T6-02.
- Stream: Wave 1 / Stream B.

### T6-04: Admin candidate review commands

- Scope: Telegram handlers for `/candidates`, `/approve`, `/reject` only (no `/edit-card` — deferred to Phase 6.5 per Q6).
- Acceptance criteria:
  - Commands are admin-only.
  - `/candidates` paginates pending candidates.
  - `/approve` atomically promotes candidate to approved card, inserts `card_sources` rows, and writes decision audit.
  - `/approve` re-runs deterministic governance filter on each candidate source `message_version_id` per §5.C / R3; BLOCKS promotion with explicit error when any source is no longer eligible (no LLM re-prompt).
  - `/approve` handler MUST acquire `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))` for EVERY source `message_version_id` in candidate as first transaction step (§5.C step 2).
  - The forget-cascade orchestrator (`_process_one_event` in `bot/services/forget_cascade.py` — the de-facto `apply_forget_event` per §5.A.5) MUST acquire `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))` for the affected `message_version_id` as the FIRST step of each event apply.
  - T6-09 advisory-lock collision test MUST pass on T6-04 PR head.
  - `/reject` marks candidate rejected and writes `extraction_decisions.action='rejected'`.
- Dependencies: T6-01, T6-02.
- Stream: Wave 2 / Stream C.

### T6-05: Admin card browsing commands

- Scope: Telegram handlers for `/cards`, `/card <id>`.
- Acceptance criteria:
  - `/cards` paginates approved cards only.
  - `/card <id>` shows title, body preview/detail, approval metadata, and source message back-citations via `card_sources`.
  - Archived/draft cards are hidden from default list.
- Dependencies: T6-04.
- Stream: Wave 2 / Stream C.

### T6-06: Search extension for approved cards

- Scope: `bot/services/search.py`, card FTS query, scoring.
- Acceptance criteria:
  - `search_messages(..., include_cards=True)` queries both `message_versions` and `knowledge_cards`.
  - Card FTS uses `to_tsvector('russian', ...)` to match the Phase 4 baseline.
  - `include_cards=False` preserves Phase 4 behaviour byte-for-byte.
  - Card hits require `card_status='approved'`.
  - Card hits rank slightly above equivalent raw message hits.
- Dependencies: T6-01.
- Stream: Wave 2 / Stream D.

### T6-07: EvidenceItem source discriminator

- Scope: Evidence dataclasses/types and `/recall` formatting path.
- Acceptance criteria:
  - `EvidenceItem.source_type` is `Literal['message', 'card']`.
  - Message evidence remains citation-compatible with `message_version_id`.
  - Card evidence carries `card_id` and the `message_version_id` set joined from `card_sources`.
  - `/recall` renders card hits without losing back-citation trace.
- Dependencies: T6-06.
- Stream: Wave 2 / Stream D.

### T6-08: Optional read-only web cards page

- Scope: `web/templates/cards.html` and read-only route if Phase 5 web scaffolding exists.
- Acceptance criteria:
  - Page lists approved cards only.
  - No create/edit UI exists.
  - Page is admin-only and respects existing web auth.
  - Ticket is explicitly deferred if Phase 5 web scaffolding is absent.
- Dependencies: T6-05, T6-06.
- Stream: Wave 3 / Stream E.

### T6-09: Integration test for candidate → card → recall

- Scope: Integration tests covering extractor candidate, admin approval, `/recall` card retrieval, and §5.A.5 cascade.
- Acceptance criteria:
  - A normal source message can produce a pending candidate.
  - Admin approval creates an approved knowledge card with corresponding `card_sources` rows.
  - `/recall` returns the approved card with source trace.
  - Rejected candidates never appear in `/recall`.
  - Offrecord/nomem/forgotten sources never produce candidates or cards.
  - **Cascade demote:** a `forget_event` matching the only source `message_version_id` of an approved card demotes the card to `card_status='archived'` with `archived_reason` referencing the `forget_event_id`.
  - **`/approve` re-validation rejection:** a candidate whose source acquired `memory_policy='offrecord'` or a `forget_event` tombstone between extraction and `/approve` is rejected by the deterministic re-validation with an explicit error.
  - **Concurrent forget+approve race test:** forget_event applied while an admin `/approve` transaction is open must abort approval OR cascade-demote the just-approved card; final state has `card_status='archived'` OR `/approve` returns governance error (no half-approved state).
  - **Advisory-lock serialization test:** explicit test that `/approve` and `apply_forget_event` both call `pg_advisory_xact_lock` with the SAME `mvid_lock_id` derivation. Spawn two concurrent transactions targeting the same `message_version_id`; assert one blocks on `pg_locks` until the other commits; assert no race-induced approved-card-with-forgotten-source state exists post-commit.
- Dependencies: T6-02, T6-04, T6-06, T6-07.
- Stream: Wave 2 closeout / holistic verification.

---

## §8. Stop Signals

- Card promotion bypassing admin review (automated promotion without human `/approve`) → STOP, governance breach.
- Card `body_markdown` containing quotes from offrecord or forgotten messages → STOP, invariant #3 violation.
- Extractor reading messages with `memory_policy != 'normal'` (e.g., offrecord/forgotten) → STOP, invariant #3 violation.
- Extraction run without LLM usage ledger entry → STOP.
- `/recall` returning card content to a user without checking `card_status='approved'` → STOP.
- **Card promotion attempted when any `card_sources` row's `message_version_id` matches a `forget_event` tombstone (per R3 deterministic re-validation) → STOP, governance breach.**
- **R3 re-validation block during `/approve`:** ABORT the transaction; NO `extraction_decisions` row written; structured log only. Candidate stays `pending` and can re-enter `/candidates` only if its governance state changes externally (currently out of scope; see §11 R3 row).

---

## §9. PR Workflow

Standard `parallel_wave_prs`.

- Pre-Wave 1: one PR for T6-00 (must merge first).
- One PR per Wave 1 stream (A + B in parallel).
- One PR per Wave 2 stream (C + D in parallel).
- Wave 3 PR only if Phase 5 web scaffolding exists.
- PAR review (Claude `standard-product-reviewer` + Codex technical) before each PR.
- Final Holistic Review (Rule 9) **required** — `parallel_wave_prs` mode + 5+ sprints + governance_mode=critical.
- T6-03 sub-gate: Phase 11 leakage binding test green before merge.

Sprint 0 ratification commit (this PR) closes the design gate.

---

## §10. Glossary

- **extraction_candidate (Phase 6):** LLM-extracted fact pending human review (`extraction_candidates` row, `status=pending`). Renamed from DRAFT's `memory_candidates` per D2.
- **memory_candidate (Phase 8, out of scope here):** reflection-cluster queue entry produced by Phase 8 reflection runs. Distinct concept from Phase 6's `extraction_candidate`; do not conflate.
- **card:** admin-approved canonical knowledge unit (`knowledge_cards` row, `card_status=approved`) — citation-eligible via `card_sources`.
- **card_source:** FK-normalized link from a `knowledge_cards` row to a single `message_versions` row (`card_sources` table per D1). Replaces the DRAFT's inline `source_message_version_ids jsonb` column.
- **extraction run:** one scheduled pass of the extractor over a time window (`extraction_runs` row).
- **promotion:** atomic operation converting a candidate to a card (`/approve` command), including governance re-validation per R3.

---

## §11. Sprint 0 Resolutions

| Decision | Outcome |
|---|---|
| **D1** | `card_sources` is a separate FK-normalized table (migration 033). `knowledge_cards` has NO `source_message_version_ids jsonb` column. Five migrations total: 030–034. Cascade extension `_cascade_card_sources_on_forget` (§5.A.5) demotes cards to `archived` when all sources are forgotten. |
| **D2** | Rename `memory_candidates` → `extraction_candidates` everywhere in Phase 6. Phase 8 keeps `memory_candidates` for reflection clusters (distinct concept). |
| **D3** | Drop `card_revisions`. `updated_at` overwrite is sufficient. `card_revisions` (and `card_relations`) deferred to Phase 6.5 / 9. |
| **Q1** | `body_markdown` stored as Telegram MarkdownV2 (TG-native rendering). |
| **Q2** | Operator-triggered only. `extraction.scheduler.enabled` feature flag default **false**; cron stub present but disabled. |
| **Q3** | Collapse `archived` + `deprecated` → single `archived` state with new nullable column `archived_reason text`. `card_status` CHECK becomes `('draft','approved','archived')`. |
| **Q4** | No `language` field. `knowledge_cards.body_tsv` uses `to_tsvector('russian', ...)` matching Phase 4 baseline (`alembic/versions/020`, `021`, `bot/db/models.py:43`). RU/EN mixed content handled identically to `message_versions.search_tsv`. |
| **Q5** | Forward-only extraction. Extractor only processes `chat_messages.created_at >= phase_6_enabled_at`. Historical backfill is operator-explicit `/admin/extract --window` invocation. |
| **Q6** | Defer `/edit-card` to Phase 6.5. T6-04 ships strict `/approve` + `/reject` only. |
| **Q7** | Resolved per D3 (no `card_versions` table; `updated_at` overwrite). |
| **R1** | Delete stale `add_memory_items` row from HANDOFF.md migration table — `memory_items` is not a Phase 6 table. |
| **R2** | Cascade behavior is **demote to `archived`** (not hard-delete). See §5.A.5. |
| **R3** | `/approve` re-validation is **deterministic SQL only** (no LLM re-prompt). Re-runs governance filter on each candidate `message_version_id`; BLOCKS promotion with explicit error if any source is no longer eligible. |

END of document.

<!-- updated-by-superflow:2026-05-12 -->
