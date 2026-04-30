> 🚧 DRAFT — NOT AUTHORIZED. Phase 6 requires AUTHORIZED_SCOPE.md update + Phase 5 closure + design ratification.

# Phase 6 — Knowledge Cards / Catalog: Design & Stream Plan

**Status:** **Draft only** — not authorized for implementation.
**Cycle:** Memory system Phase 6
**Date:** 2026-04-30
**Predecessor:** Phase 5 must be closed before Phase 6 begins.
**Migration window:** tentative 030+ (Phase 4 uses ~018-021; Phase 5 likely 022-029)
**Critical invariant for this phase:** Cards become approved citation sources only after human admin review.

---

## §0. Implementation Status

TBD — not started.

**Authorization status:** NOT AUTHORIZED. `AUTHORIZED_SCOPE.md` explicitly lists Catalog / knowledge cards as Phase 6 future scope.

**Source status:** [SOURCE NOT FOUND — INFERRED] `docs/memory-system/PHASE4_PLAN.md` was not present in `/Users/eekudryavtsev/Vibe/products/shkoderbot`; the structural template was read from `/Users/eekudryavtsev/Vibe/products/shkoderbot/.worktrees/p4-prompts/docs/memory-system/PHASE4_PLAN.md`.

**Existing schema baseline from `bot/db/models.py`:**

- `chat_messages`: `id`, `message_id`, `chat_id`, `user_id`, `text`, `date`, `raw_json`, `created_at`, `raw_update_id`, `reply_to_message_id`, `message_thread_id`, `caption`, `message_kind`, `current_version_id`, `memory_policy`, `visibility`, `is_redacted`, `content_hash`, `updated_at`.
- `chat_messages` FKs: `user_id → users.id`, `raw_update_id → telegram_updates.id ON DELETE SET NULL`, `current_version_id → message_versions.id ON DELETE SET NULL`.
- `message_versions`: `id`, `chat_message_id`, `version_seq`, `text`, `caption`, `normalized_text`, `entities_json`, `edit_date`, `captured_at`, `content_hash`, `raw_update_id`, `is_redacted`, `imported_final`.
- `message_versions` FKs: `chat_message_id → chat_messages.id ON DELETE CASCADE`, `raw_update_id → telegram_updates.id ON DELETE SET NULL`.
- `qa_traces`: [SOURCE NOT FOUND — INFERRED] no `qa_traces` model/table was present in the read `bot/db/models.py`; Phase 6 must verify Phase 4 closure before depending on it.

---

## §1. Invariants

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.
10. Public wiki remains disabled until review / source trace / governance are proven.

---

## §2. Phase 6 Spec

- **Objective:** curated catalog with review and source trace.
- **Scope:** `memory_items`, `knowledge_cards`, `card_sources`, `card_relations`, review UI.
- **Dependencies:** phase 5.
- **Entry criteria:** candidates with source refs and admin actions.
- **Exit criteria:** admin can approve cards with citations.
- **Acceptance:** card cannot become active without source; visibility enforced.
- **Risks:** extractions becoming "truth" without review.
- **Rollback:** hide cards via status / flag.

---

## §3. Phase 7 Boundary

- **Daily summaries / digests (Phase 7):** OUT OF SCOPE because summaries are derived recaps and must consume approved sources; they are never canonical truth.
- **Reflection runs (Phase 8):** OUT OF SCOPE because analytical reflection requires a stable reviewed catalog and separate governance around generated insights.
- **Wiki (Phase 9):** OUT OF SCOPE because editable/community-facing catalog pages require visibility filters, source trace, and review workflows beyond card approval.
- **Graph (Phase 10):** OUT OF SCOPE because graph projection is derived only, rebuildable from Postgres, and must wait for stable card/event relations.

---

## §4. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│ Scheduler                                                     │
│ - cron / operator-triggered pass                              │
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
│ memory_candidates                                             │
│ status='pending'                                              │
│ source_message_version_ids JSONB                              │
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
│ knowledge_cards                 │   │ extraction_decisions    │
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
│ card hits cite approved card sources                          │
└──────────────────────────────────────────────────────────────┘
```

---

## §5. Component Design

### 5.A. Migrations (tentative numbers starting after Phase 5)

Phase 4 uses ~018-021 and Phase 5 likely owns 022-029; Phase 6 starts around 030. Exact revision IDs must be coordinated after Phase 5 closure.

**030_add_extraction_runs**

`extraction_runs`:

- `id uuid primary key`
- `ingestion_window_start timestamptz`
- `ingestion_window_end timestamptz`
- `candidate_count int not null default 0`
- `run_status text not null check (run_status in ('running','completed','failed'))`
- `llm_usage_ledger_id uuid nullable` — FK if Phase 5 ledger exists, otherwise nullable UUID placeholder until ratified.
- `created_at timestamptz not null default now()`

Constraints:

- `candidate_count >= 0`
- If `run_status='completed'`, `ingestion_window_start` and `ingestion_window_end` must be non-null.

**031_add_memory_candidates**

`memory_candidates`:

- `id uuid primary key`
- `extraction_run_id uuid references extraction_runs(id) on delete set null`
- `candidate_json jsonb not null`
- `source_message_version_ids jsonb not null default '[]'::jsonb`
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
- `body_markdown text not null`
- `body_tsv tsvector generated always as (to_tsvector('russian', coalesce(body_markdown, ''))) stored`
- `source_message_version_ids jsonb not null default '[]'::jsonb`
- `card_status text not null check (card_status in ('draft','approved','archived','deprecated'))`
- `approved_by_user_id bigint references users(id) on delete set null`
- `approved_at timestamptz`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Constraints:

- `source_message_version_ids` must be a non-empty JSON array before `card_status='approved'`.
- `card_status='approved'` implies `approved_by_user_id is not null` and `approved_at is not null`.
- `card_status!='approved'` rows are not citation-eligible.

**033_add_extraction_decisions**

`extraction_decisions`:

- `id uuid primary key`
- `candidate_id uuid references memory_candidates(id) on delete cascade`
- `action text not null check (action in ('approved','rejected'))`
- `reason text`
- `decided_by bigint references users(id) on delete set null`
- `decided_at timestamptz not null default now()`
- `created_at timestamptz not null default now()`

Constraints:

- Exactly one terminal decision per candidate unless a later ratified lifecycle adds appeals.
- `decided_by` is required for human governance unless a system migration is explicitly approved.

### 5.B. `bot/services/extractor.py`

**Public API:** async function `run_extraction_pass(session, *, window_start, window_end)`.

Behaviour:

- Reads `chat_messages` filtered by `memory_policy='normal'` and `created_at` within the requested window.
- Joins only current `message_versions` rows and excludes redacted/forgotten content through the same governance filters as `/recall`.
- Builds evidence-bundle context from message text/caption, message metadata, and source `message_version_id`s.
- Calls `llm_gateway.extract_candidates()`; no direct provider SDK calls are allowed.
- Writes one `extraction_runs` row and zero or more `memory_candidates` rows with `status='pending'`.
- Returns `ExtractionResult` with counts: read messages, eligible source versions, candidates written, rejected by validation, and run status.

Stop condition:

- If any selected source row has `memory_policy!='normal'`, `is_redacted=true`, or a matching tombstone, the pass stops and records a failed `extraction_runs` row.

### 5.C. Admin Telegram handlers

Commands:

- `/candidates` — admin-only, paginated list of pending candidates.
- `/approve <candidate_id>` — admin-only atomic promotion of a candidate to `knowledge_cards`.
- `/reject <candidate_id> [reason]` — admin-only rejection; marks candidate rejected and writes an `extraction_decisions` row.
- `/cards` — admin-only paginated list of approved cards.
- `/card <id>` — admin-only card detail with back-citations to source messages.

Promotion rules:

- `/approve` must run in one DB transaction.
- It changes `memory_candidates.status` to `approved`, inserts `knowledge_cards.card_status='approved'`, copies `source_message_version_ids`, fills `approved_by_user_id` and `approved_at`, and writes `extraction_decisions.action='approved'`.
- It must reject promotion if the candidate has no source message versions or if any source is no longer eligible under governance filters.

### 5.D. Search extension (`bot/services/search.py`)

`search_messages` gains `include_cards: bool = True`.

Behaviour:

- Existing message search remains unchanged when `include_cards=False`.
- When `include_cards=True`, the service runs the current FTS query against `message_versions` and a second GIN FTS query against `knowledge_cards.body_tsv`.
- Card query filters `knowledge_cards.card_status='approved'`.
- `EvidenceItem` gains `source_type: Literal['message', 'card']`.
- Message evidence keeps `message_version_id`.
- Card evidence carries `card_id` plus `source_message_version_ids` for citation trace.
- Scorer weighs approved card hits slightly higher than raw message hits because cards are admin-reviewed authority.

Contract:

- `/recall` may quote card content only when `card_status='approved'`.
- A card citation must still expose source trace back to message versions, satisfying invariant #4.

### 5.E. Web UI scaffolding (optional / deferrable)

Optional read-only admin page: `web/templates/cards.html`.

Behaviour:

- Lists approved cards only.
- Shows title, short body preview, status, approval metadata, and source count.
- Links to card detail if Phase 5 web scaffolding already exists.

Deferral:

- Create/edit workflows are deferred to Phase 9 wiki.
- Implement only if Phase 5 web scaffolding already exists; otherwise keep Phase 6 review inside Telegram admin commands.

---

## §6. Stream Allocation

### Wave 1 — independent foundations (PARALLEL)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **A** | Schema migrations 030-033 | `extraction_runs`, `memory_candidates`, `knowledge_cards`, `extraction_decisions` | Phase 5 closed |
| **B** | Extractor service skeleton + `llm_gateway.extract_candidates` stub | `bot/services/extractor.py`, Phase 5 gateway extension | Phase 5 gateway exists |

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

### T6-01: Phase 6 schema migrations

- Scope: Alembic migrations 030-033; tables `extraction_runs`, `memory_candidates`, `knowledge_cards`, `extraction_decisions`; ORM models.
- Acceptance criteria:
  - Migrations apply and roll back cleanly on Postgres 16.
  - All checks/FKs/defaults in §5.A are present.
  - `knowledge_cards.body_tsv` has a GIN index.
  - `card_status='approved'` cannot exist without source refs and admin approval metadata.
- Dependencies: Phase 5 closure, `AUTHORIZED_SCOPE.md` update.
- Stream: Wave 1 / Stream A.

### T6-02: Extractor service pass

- Scope: `bot/services/extractor.py`, `ExtractionResult`, DB reads/writes.
- Acceptance criteria:
  - `run_extraction_pass(session, *, window_start, window_end)` exists.
  - It reads only `chat_messages.memory_policy='normal'`.
  - It writes `memory_candidates.status='pending'`.
  - It records `extraction_runs.run_status` and `candidate_count`.
- Dependencies: T6-01.
- Stream: Wave 1 / Stream B.

### T6-03: LLM gateway candidate extraction method

- Scope: Phase 5 `llm_gateway`; add `extract_candidates()` contract only.
- Acceptance criteria:
  - No provider SDK call exists outside `llm_gateway`.
  - Every call is associated with the Phase 5 LLM usage ledger if ledger is required.
  - Output schema includes candidate payload and source `message_version_id`s.
  - Forbidden source content cannot be passed to the gateway.
- Dependencies: Phase 5 gateway/ledger, T6-02.
- Stream: Wave 1 / Stream B.

### T6-04: Admin candidate review commands

- Scope: Telegram handlers for `/candidates`, `/approve`, `/reject`.
- Acceptance criteria:
  - Commands are admin-only.
  - `/candidates` paginates pending candidates.
  - `/approve` atomically promotes candidate to approved card and writes decision audit.
  - `/reject` marks candidate rejected and writes `extraction_decisions.action='rejected'`.
- Dependencies: T6-01, T6-02.
- Stream: Wave 2 / Stream C.

### T6-05: Admin card browsing commands

- Scope: Telegram handlers for `/cards`, `/card <id>`.
- Acceptance criteria:
  - `/cards` paginates approved cards only.
  - `/card <id>` shows title, body preview/detail, approval metadata, and source message back-citations.
  - Archived/deprecated/draft cards are hidden from default list.
- Dependencies: T6-04.
- Stream: Wave 2 / Stream C.

### T6-06: Search extension for approved cards

- Scope: `bot/services/search.py`, card FTS query, scoring.
- Acceptance criteria:
  - `search_messages(..., include_cards=True)` queries both `message_versions` and `knowledge_cards`.
  - `include_cards=False` preserves Phase 4 behaviour.
  - Card hits require `card_status='approved'`.
  - Card hits rank slightly above equivalent raw message hits.
- Dependencies: T6-01.
- Stream: Wave 2 / Stream D.

### T6-07: EvidenceItem source discriminator

- Scope: Evidence dataclasses/types and `/recall` formatting path.
- Acceptance criteria:
  - `EvidenceItem.source_type` is `Literal['message', 'card']`.
  - Message evidence remains citation-compatible with `message_version_id`.
  - Card evidence carries `card_id` and `source_message_version_ids`.
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

### T6-09: Integration test for candidate to card to recall

- Scope: Integration tests covering extractor candidate, admin approval, and `/recall` card retrieval.
- Acceptance criteria:
  - A normal source message can produce a pending candidate.
  - Admin approval creates an approved knowledge card.
  - `/recall` returns the approved card with source trace.
  - Rejected candidates never appear in `/recall`.
  - Offrecord/nomem/forgotten sources never produce candidates or cards.
- Dependencies: T6-02, T6-04, T6-06, T6-07.
- Stream: Wave 2 closeout / holistic verification.

---

## §8. Stop Signals

- Card promotion bypassing admin review (automated promotion without human `/approve`) → STOP, governance breach.
- Card `body_markdown` containing quotes from offrecord or forgotten messages → STOP, invariant #3 violation.
- Extractor reading messages with `memory_policy != 'normal'` (e.g., offrecord/forgotten) → STOP, invariant #3 violation.
- Extraction run without LLM usage ledger entry → STOP if ledger is required by Phase 5 governance.
- `/recall` returning card content to a user without checking `card_status='approved'` → STOP.

---

## §9. PR Workflow

Standard `sprint_pr_queue`.

- One PR per Wave.
- Branch pattern: `feat/p6-NN-slug`.
- PAR review before each PR.
- Holistic review after all waves.
- `AUTHORIZED_SCOPE.md` must be updated before Phase 6 work begins.

Phase 6 must not start from this draft alone. Required gates:

- Phase 5 closure confirmed.
- Phase 6 authorization added to `AUTHORIZED_SCOPE.md`.
- Design ratified by team lead.
- Migration numbers reconciled with actual Phase 4/5 revisions.

---

## §10. Glossary

- **candidate:** LLM-extracted fact pending human review (`memory_candidates` row, `status=pending`).
- **card:** admin-approved canonical knowledge unit (`knowledge_cards` row, `status=approved`) — citation-eligible.
- **extraction run:** one scheduled pass of the extractor over a time window (`extraction_runs` row).
- **promotion:** atomic operation converting a candidate to a card (`/approve` command).

---

## Open Design Questions

1. Should cards be stored/rendered as Telegram-Markdown or full HTML? (impacts `/recall` reply format and web UI)
2. Should extraction runs trigger on a schedule (cron every N hours) or event-based (every M new messages ingested)?
3. Card lifecycle: should `archived` and `deprecated` be distinct states, or collapse into one? (impacts admin UX)
4. Multi-language: should `knowledge_cards` have an explicit language field, or rely on content detection?
5. Backfill strategy: should the extractor process all historical `chat_messages` from the beginning, or only messages ingested after Phase 6 goes live?
6. Should `/approve` allow editing the `candidate_json` before promotion, or promote as-is and require a separate `/edit-card` command?
7. Should `knowledge_cards` have a versioning mechanism (`card_versions` table) for audit, or is `updated_at` + `body_markdown` overwrite sufficient for Phase 6?

END of document.
