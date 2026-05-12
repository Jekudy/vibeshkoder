# Phase 6 Wave 1 — Knowledge Cards Subsystem

**Status**: IN PROGRESS (2026-05-12)
**Wave 1 sprints merged**: T6-00, T6-01, T6-02 (3 / 4)
**Wave 1 in flight**: T6-03 (gateway wiring + router registration)
**Phase predecessor**: Phase 5 LLM gateway closed 2026-05-11

---

## What's new

### Schema (migrations 030–035)

Six new Alembic migrations land as part of Wave 1. All are additive — no existing columns
altered, no data migrations required.

| Migration | Table | Purpose |
|-----------|-------|---------|
| `030_add_extraction_runs` | `extraction_runs` | Tracks each LLM extraction pass: window bounds, candidate count, run status (`running`/`completed`/`failed`), FK to `llm_usage_ledger`. |
| `031_add_extraction_candidates` | `extraction_candidates` | Holds LLM-produced card candidates: `candidate_json`, staging `source_message_version_ids`, review status (`pending`/`approved`/`rejected`/`superseded`), reviewer FK and timestamp. |
| `032_add_knowledge_cards` | `knowledge_cards` | Persists approved cards: title, `body_markdown` (Telegram MarkdownV2), `body_tsv` generated tsvector (`'russian'` config — matches existing `message_versions.search_tsv`), `card_status` (`draft`/`approved`/`archived`), `archived_reason`. |
| `033_add_card_sources` | `card_sources` | FK-normalized citation links from a knowledge card to the `message_versions` rows it was derived from. Source refs are promoted from `extraction_candidates.source_message_version_ids` at `/approve` time. |
| `034_add_extraction_decisions` | `extraction_decisions` | Audit trail for every admin `/approve` and `/reject` action with actor, timestamp, and rationale. |
| `035_add_extraction_runs_operator_user_id` | `extraction_runs` (ALTER) | Adds `operator_user_id` column — records the Telegram user ID of the admin who triggered a manual extraction pass. NULL for scheduler-initiated runs. |

PR #245 (migrations 030–034, ORM models, advisory-lock helper, cascade extension)
merged 2026-05-12T15:10:49Z.

PR #248 (migration 035, extractor service, admin handler)
merged 2026-05-12T19:35:41Z.

### Cascade and forget-event extension (`_cascade_card_sources_on_forget`)

`bot/db/models.py` gains `_cascade_card_sources_on_forget`: when a forget event is applied
to a `message_version_id`, any knowledge card whose ALL source rows are now gone transitions
to `card_status='archived'`. The `archived_reason` field references the `forget_event_id`
only — never message body content. This closes the Phase 6 privacy invariant that cards
must not survive the deletion of their only citations.

### Advisory-lock helper (`_p6_mvid_advisory_lock_id`)

A deterministic PostgreSQL advisory-lock ID derivation function keyed on `message_version_id`
was added in PR #245. It is the single source of truth for serializing concurrent access to
the same message version during extraction. The lock site at `/approve` wiring (Wave 2
T6-04) and the forget-cascade orchestrator will both reference this helper.

### Extractor service (`bot/services/extractor.py`)

New service module implementing:

- `run_extraction_pass(session, window_start, window_end, *, gateway)` — the main
  extraction pass. Reads `chat_messages WHERE memory_policy='normal'` joined to current
  `message_versions` within the given window, assembles an evidence bundle, calls the
  `ExtractCandidatesGateway` protocol seam, persists the `ExtractionRun` row, and writes
  `extraction_candidates` rows with status `'pending'`.
- `ExtractCandidatesGateway` — a `@runtime_checkable` Protocol (not imported from any LLM
  provider directly; the concrete implementation lands in T6-03).
- `extraction_scheduler_tick(session, *, gateway)` — flag-gated scheduler entry point
  protected by `pg_try_advisory_xact_lock` to guarantee at-most-one concurrent tick across
  replicas. Uses feature flag `memory.extraction.scheduler.enabled` (default: OFF).

Key safety invariants enforced in the extractor (all verified by Codex three-round review):

1. **ledger_id non-null guard** — an `ExtractionRun` cannot complete without a linked
   `llm_usage_ledger` entry (CRITICAL guard, commit `8aa142c`).
2. **SELECT→gateway race** — a fresh `forget_events` re-query inside `_bundle_is_clean`
   closes the window between the initial candidate SELECT and the gateway call
   (CRITICAL fix, commit `58469d2`).
3. **Tombstone filter on live path** — uses `message_versions.content_hash`, not
   `chat_messages.content_hash`, to correctly identify live-message tombstones
   (CRITICAL fix round 3, commit `1ce196a`).
4. **Atomic `running` INSERT before gateway** — the `ExtractionRun` row with
   `run_status='running'` is committed before the gateway call so crashes do not leave
   ghost runs (HIGH fix round 3, commit `2a27ba5`).

### Admin handler (`bot/handlers/admin_extract.py`)

New Telegram handler: `/admin_extract --window <start>..<end>`

- Private-chat only (`PrivateChatFilter`).
- Admin guard: `message.from_user.id in settings.ADMIN_IDS`.
- Window format: ISO 8601 datetime range, e.g.
  `2026-05-01T00:00:00Z..2026-05-11T23:59:59Z`.
- Maximum window: 30 days (`MAX_WINDOW_DAYS = 30`).
- Both `--window X..Y` and bare `X..Y` syntax accepted.
- Calls `run_extraction_pass` directly, bypassing the scheduler flag.
- Persists `operator_user_id` to the `ExtractionRun` row (audit marker, migration 035).

**Note**: as of Wave 1, the router is defined but NOT registered in `bot/__main__.py`.
The handler is not yet invocable from Telegram. Router registration and concrete gateway
DI wiring land in T6-03.

---

## Privacy guarantees (Phase 11 binding: 21/21 green)

The following privacy invariants are enforced at merge time and gate every future sprint:

| Invariant | Enforcement |
|-----------|-------------|
| No LLM calls outside `llm_gateway` | AST-level `test_no_llm_imports.py` + runtime `_llm_guard` httpx hook (PR #243) |
| No extraction over non-normal-policy or tombstoned messages | `memory_policy='normal'` filter + `_bundle_is_clean` fresh re-query |
| Forget cascade demotes cards when all sources tombstoned | `_cascade_card_sources_on_forget` (PR #245) |
| `archived_reason` never contains message body | FK to `forget_event_id` only |
| Advisory lock serializes concurrent access per `message_version_id` | `_p6_mvid_advisory_lock_id` helper (PR #245) |
| Privacy allowlist narrowed to 4 exact globs | PR #247 (see `scripts/lint_privacy_check.sh`) |
| httpx URL-level no-LLM guard in eval mode | `tests/evals/_llm_guard.py` autouse fixture (PR #243) |

---

## Phase 11 follow-ups merged today

These are hardening fixes against Phase 11 FHR findings (#224):

| Issue | PR | What |
|-------|----|------|
| #224 High #5 | #243 (merged 15:03Z) | `tests/evals/_llm_guard.py`: runtime httpx URL/domain guard blocks direct LLM calls in eval mode; AST-check cannot catch dynamic URL construction |
| #224 Critical #4 | #247 (merged 19:25Z) | `scripts/lint_privacy_check.sh` + `scripts/precommit-privacy-allowlist.sh` narrowed to 4 globs; baseline-diff now uses `path:content` keys (line-shift resilient after rebase); multiset duplicate-line fix |

#224 High #1–#4 verified already on main (no PR required).

---

## Operator migration guide

### Applying migrations

```bash
alembic upgrade head
```

Brings the database from revision `025` (Phase 5 close) to revision `035`.
All 6 migrations are schema-only additions. No existing data is modified.
Downgrade tested clean (round-trip verified in CI).

### Feature flag: scheduler

The extraction scheduler is disabled by default. To enable automatic extraction runs:

```python
# via FeatureFlagRepo (one-time setup in a management session)
await repo.set_enabled("memory.extraction.scheduler.enabled", enabled=True)
```

When disabled (default), no automatic extraction passes run. Manual passes via
`/admin_extract` bypass this flag regardless of its state.

### Manual extraction (available after T6-03 merges)

```
/admin_extract --window 2026-05-01T00:00:00Z..2026-05-11T23:59:59Z
```

Constraints:
- Sender must be in `settings.ADMIN_IDS`.
- Message must be sent in a private chat.
- Window maximum: 30 days.

---

## Known limitations (as of 2026-05-12)

1. **`/admin_extract` not yet invocable**: handler is implemented (T6-02) but the router
   is not registered in `bot/__main__.py` until T6-03 lands.

2. **No concrete gateway**: `ExtractCandidatesGateway` is a Protocol seam. The concrete
   `llm_gateway.extract_candidates()` implementation is the T6-03 deliverable.

3. **`phase_6_enabled_at` derived from `FeatureFlag.updated_at`**: non-monotonic if the
   flag is toggled off and back on. Documented in `extractor.py` docstring. Backfill via
   `/admin_extract --window` covers any gap.

4. **T6-09 advisory-lock collision integration test pending**: the integration test
   (`candidate → card → recall` full cycle with concurrent `/approve` and forget-cascade)
   is deferred until T6-04 wires the `/approve` handler.

5. **`card_revisions` and `card_relations`** deferred to Phase 6.5 / Phase 9.

---

## What's coming

### Wave 1 — in flight

| Ticket | Sprint | What |
|--------|--------|------|
| T6-03 | Wave 1 / Stream B sprint 2 | `llm_gateway.extract_candidates()` concrete implementation + router registration in `bot/__main__.py` + scheduler DI wiring. Must clear Phase 11 leakage binding test before merge. Design doc: `docs/memory-system/T6-03_design.md` (PR #249, merged 2026-05-12T20:47Z). |

### Wave 2 — designed, awaiting Wave 1 close

Wave 2 design PRs merged 2026-05-12. Execution authorized after T6-03 merge.

| Ticket | Stream | What |
|--------|--------|------|
| T6-04 | C | Admin `/candidates` list + `/approve` + `/reject` handlers. Also wires `pg_advisory_xact_lock(_p6_mvid_advisory_lock_id(mvid))` at `/approve` and forget-cascade orchestrator. |
| T6-05 | C | Admin `/cards` list + `/card <id>` browse. |
| T6-06 | D | `search_messages(..., include_cards=True)` — search extension to query both `message_versions` and `knowledge_cards` in a single pass. |
| T6-07 | D | `EvidenceItem.source_type: Literal['message', 'card']` discriminator for `/recall` response attribution. Parallelizable with T6-03. |
| T6-09 | closeout | Advisory-lock collision integration test (`candidate → card → recall` full cycle). Depends on T6-04. |

### Wave 3 — optional

| Ticket | What | Condition |
|--------|------|-----------|
| T6-08 | Read-only web cards page | Deferred unless Phase 5 web scaffold lands before Phase 6 close. |

---

## Review evidence

All Wave 1 tickets went through the standard PAR gate (Claude product reviewer + Codex
technical reviewer, gpt-5.5 high reasoning):

| Ticket | PR | Claude verdict | Codex verdict | Rounds |
|--------|----|----------------|---------------|--------|
| T6-00 | #242 | — | — | Tests-only, no full PAR required |
| T6-01 | #245 | ACCEPTED | APPROVE | 2 rounds (HIGH SQLite compat fixed) |
| T6-02 | #248 | ACCEPTED (0 CRIT/HIGH) | APPROVE | 3 rounds (3 CRIT + 5 HIGH fixed across rounds 1–2) |
| #224 Critical #4 | #247 | ACCEPTED | APPROVE | 2 rounds (multiset HIGH fixed) |
| #224 High #5 | #243 | ACCEPTED | APPROVE (after round 2) | 2 rounds |

---

## Reference

- Plan: `docs/memory-system/PHASE6_PLAN.md` (ratified PR #231, 2026-05-12T11:52Z)
- Implementation status: `docs/memory-system/IMPLEMENTATION_STATUS.md`
- T6-03 design: `docs/memory-system/T6-03_design.md` (PR #249)
- Phase 11 binding: `tests/evals/test_leakage.py`, `test_citations.py`, `test_refusal.py`, `test_no_llm_imports.py`
- Feature flag: `memory.extraction.scheduler.enabled` (default OFF)
- Migration head after Wave 1: revision `035`

<!-- updated-by-superflow:2026-05-12 -->
