# Memory System — Architect Handoff (Canonical)

**Captured:** 2026-04-26
**Status:** canonical specification. All planning, ticketing, and execution derive from this
document. Supersedes the v0.5 design spec (archived).

---

## Execution cover note for team lead

This is the final architecture / backlog handoff for shkoder.

**Important:** the document below describes the entire roadmap, but immediate execution scope is
**limited**.

### Authorized immediate scope

On the first cycle of development, allowed to implement only:

**Phase 0:**
- fix `forward_lookup` privacy issue
- fix / contain sqlite vs postgres upsert issue
- make `MessageRepo.save` idempotent
- add implementation status doc
- add health / startup checks
- add regression tests

**Phase 1:**
- add `feature_flags`
- add `ingestion_runs`
- add `telegram_updates`
- add raw update persistence service
- extend `chat_messages`
- add `message_versions` + v1 backfill
- persist `reply_to_message_id`
- persist `message_thread_id`
- persist `caption` / `message_kind`
- add minimal `#nomem` / `#offrecord` detection
- add `offrecord_marks` minimal table
- add `edited_message` handler **only after** `message_versions` exists

**Stretch:**
- add `forget_events` tombstone skeleton
- import dry-run parser

### Not authorized yet

Do not implement yet:

- import apply
- q&a bot
- LLM calls
- LLM extraction
- vector search
- catalog / cards
- daily summaries
- weekly digest
- wiki
- graph / Neo4j / Graphiti
- butler / action execution
- person expertise pages
- public surfaces

### Critical safety rule for `#offrecord`

`#offrecord` must **not** be durably stored as raw visible content.

Implementation default:

- detect `#offrecord` BEFORE committing content-bearing `raw_json`, OR
- write raw update + redaction in the same transaction before commit

Committed storage for `#offrecord` should keep only minimal metadata:
- chat id
- message id
- timestamp
- hash / tombstone key
- policy marker
- audit metadata

No search, q&a, extraction, summary, catalog, vector, graph, or wiki may use `#offrecord`
content.

### Import rule

Telegram Desktop import has two modes:

- **dry-run** — allowed before full governance
- **apply** — blocked until `#nomem` / `#offrecord` detection AND `forget_events` /
  tombstone skeleton both exist

Import apply must use the same normalization / governance path as live Telegram updates.

### `allowed_updates` rollout rule

Do not add Telegram update types before storage / handler exists.

- `edited_message` can be added only after `message_versions` and edited handler exist.
- `message_reaction` / `message_reaction_count` can be added only after reactions table and
  handlers exist.

### Agent execution rules

Coding agents must:

- inspect current code before editing
- work ticket-by-ticket
- keep PRs small
- preserve existing gatekeeper behaviour
- add tests with every change
- never assume docs / specs are implemented
- never introduce LLM calls outside `llm_gateway`
- never implement future phases early
- never log secrets / env values
- list changed files, tests run, and risks

### First sprint target

By the end of the first sprint:

- current gatekeeper still working
- `forward_lookup` privacy fixed
- DB upsert issue contained
- duplicate message save safe
- `feature_flags` table
- `ingestion_runs` table
- `telegram_updates` table
- raw update persistence for current message updates
- extended `chat_messages` fields
- `message_versions` with v1 backfill
- reply / thread / caption / message_kind persistence
- minimal `#nomem` / `#offrecord` policy detection
- tests covering all of the above

Everything else is out of scope until phase gates pass.

---

## §0. Team lead summary

### What exists today

The current repo is a vibe-gatekeeper foundation, **not a memory system**.

Confirmed by audit of current git:

- bot runs on aiogram long polling
- `allowed_updates` currently includes only `message`, `callback_query`, `chat_member`,
  `my_chat_member`
- **no** `edited_message`, `message_reaction`, `message_reaction_count`
- **no** handlers for edits / reactions
- `chat_messages` stores only minimal fields: `id`, `message_id`, `chat_id`, `user_id`, `text`,
  `date`, `raw_json`, `created_at`
- **no** raw `telegram_updates`
- **no** `message_versions`
- **no** `reply_to_message_id`, `message_thread_id`, captions / entities / links / attachments
  normalization
- **no** import telegram desktop export
- **no** `#nomem`, `#offrecord`, `/forget`, `/forget_me`, tombstones
- admin web is the gatekeeper dashboard, not a memory review UI
- **no** q&a with citations / confidence / abstention
- **no** `llm_usage_ledger`, `feature_flags`
- immediate risks present:
  - `forward_lookup` could leak member intro without membership / admin check
  - dev sqlite could conflict with postgres-specific upsert
  - `chat_messages.save` is not a clean idempotent path

### What we are building

The next goal is a librarian / governed community memory system on top of the current
gatekeeper:

```
gatekeeper
  → source-of-truth archive
    → governance
      → import
        → search / q&a with citations
          → events / observations / candidates
            → reviewed catalog / cards
              → daily / weekly digest
                → internal / member wiki
```

Future butler / action assistant is **not** built now. Only preserve extension points:
permissions, audit, action boundary, future `action_requests` / `action_runs`.

### What must happen first

1. Close current safety / runtime risks.
2. Add `feature_flags`.
3. Add raw `telegram_updates`.
4. Extend `chat_messages` without breaking existing gatekeeper.
5. Add `message_versions`.
6. Add minimal `#nomem` / `#offrecord` detector and `offrecord_marks`.
7. Add `forget_events` tombstone skeleton.
8. Only then — import apply, search / q&a, LLM, extraction.

### What cannot be built yet

Do not build until phase gates:

- public wiki
- graph DB
- LLM extraction
- vector-only q&a
- auto digest
- catalog without admin review
- person expertise pages
- butler action execution

### Key risks

| Risk                              | Why it matters                                                      |
|-----------------------------------|---------------------------------------------------------------------|
| Gatekeeper breakage               | Current functionality must survive.                                 |
| Privacy leak                      | `forward_lookup` risk already exists; memory amplifies consequences |
| Forgotten content resurrection    | Import / extraction / catalog without tombstones revives deleted    |
| Bad citations                     | Without `message_versions`, q&a cites unstable text                 |
| Docs / spec confusion             | Old docs ≠ implementation                                           |
| AI agents jumping ahead           | LLM / catalog / wiki before governance — forbidden                  |

### Recommended execution mode

- One lead holds the phase gates.
- AI dev agents take small tickets.
- Every PR:
  - small
  - with tests
  - with listed changed files
  - without future-phase scope creep
  - without secrets / logging values
  - without LLM calls outside `llm_gateway`
  - without search / extraction access to forbidden content

---

## §1. Final implementation strategy

### Strategy

1. **Preserve gatekeeper.** Existing onboarding / questionnaire / vouching / intro refresh /
   Google Sheets / admin dashboard must keep working. Early migrations are additive.
   `chat_messages` is extended, not replaced.

2. **Build source-of-truth archive.** Before q&a / import / extraction:
   - `feature_flags`
   - `ingestion_runs`
   - `telegram_updates`
   - extended `chat_messages`
   - `message_versions`
   - `chat_threads`
   - metadata tables: `entities`, `links`, `attachments`

3. **Add governance before memory.** `#nomem`, `#offrecord`, `/forget`, `/forget_me`,
   tombstones, audit must appear before extraction / catalog / digest / wiki.

4. **Add search / q&a before extraction.** First lexical / full-text retrieval + evidence bundle
   + citations + abstention. Provides value without premature "smart" memory extraction.

5. **Add extraction before catalog.** Only after governance / search / source trace:
   - deterministic prefilter
   - event extraction
   - observations
   - candidates
   - LLM ledger
   - admin review

6. **Add catalog before digest / wiki.** Digest / wiki must reference approved cards / sources,
   not loose extractions.

7. **Postpone graph and butler.** Graph is a derived projection after cards / events stable.
   Butler is design-only, no implementation.

### Critical path

```
phase 0 safety
  → feature_flags
    → ingestion_runs + telegram_updates
      → extend chat_messages
        → message_versions
          → #nomem/#offrecord + offrecord_marks
            → forget_events / tombstones
              → import apply
                → fts / evidence
                  → q&a with citations
                    → llm_gateway / ledger
                      → extraction / events / observations / candidates
                        → cards / admin review
                          → summaries / digests / wiki
```

### Parallelizable tracks

After phase 0:

- DB migration drafting
- tests / fixtures
- admin health / read-only screens
- import dry-run parser
- docs implementation status
- q&a eval case design

Cannot parallelize without gate:

- import apply before tombstones
- `edited_message` before `message_versions`
- reactions before reactions table / handler
- LLM extraction before `llm_gateway` + governance
- wiki before review / source trace

### Phase gates

| Gate                  | Must be true                                                                            |
|-----------------------|-----------------------------------------------------------------------------------------|
| Gatekeeper safety     | privacy fix, idempotent save, upsert contained, tests green                             |
| Source of truth       | raw updates + message versions + basic normalization exist                              |
| Governance            | `#nomem` / `#offrecord`, tombstones, forget skeleton, filters exist                     |
| Q&A                   | FTS, evidence bundle, citations, refusal, policy filters                                |
| Extraction            | LLM gateway, ledger, source validation, budget guard                                    |
| Catalog               | cards require sources and admin review                                                  |
| Wiki                  | visibility + review + source trace + forget purge                                       |

### Non-negotiable invariants

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

## §2. Phase roadmap

### Phase 0 — gatekeeper stabilization

- **Objective:** close immediate safety / runtime risks.
- **Starting point:** working gatekeeper, simple admin web, minimal message capture.
- **Scope:** `forward_lookup` privacy, upsert containment, idempotent message save, docs
  implementation status, health checks, regression tests.
- **Out of scope:** memory schema, import, q&a, LLM.
- **Dependencies:** none.
- **Entry criteria:** current repo baseline audited.
- **Exit criteria:** current gatekeeper tests pass; privacy risk fixed; duplicate message safe.
- **Acceptance:** non-member cannot use `forward_lookup` to reveal intro; `MessageRepo.save`
  duplicate-safe; dev / test upsert safe; `/healthz` exists.
- **Risks:** small fixes accidentally change onboarding / vouch behaviour.
- **Rollback:** code-only rollback; no destructive data changes.

### Phase 1 — source of truth + raw archive

- **Objective:** create governed source-of-truth archive.
- **Starting point:** no raw `telegram_updates`, no versions.
- **Scope:** `feature_flags`, `ingestion_runs`, `telegram_updates`, extend `chat_messages`,
  `message_versions`, `chat_threads`, metadata tables.
- **Out of scope:** import apply, q&a, LLM, catalog.
- **Dependencies:** phase 0.
- **Entry criteria:** duplicate-safe message save; tests green.
- **Exit criteria:** live message produces raw update + normalized message + v1 version.
- **Acceptance:** reply / thread / caption / `message_kind` persisted; raw update idempotent;
  existing messages backfilled to v1 versions.
- **Risks:** raw payload privacy; transaction boundaries.
- **Rollback:** additive migrations; feature flags default off.

### Phase 2a — Telegram Desktop import dry-run

- **Objective:** parse export and produce safe stats without content writes.
- **Starting point:** no importer.
- **Scope:** JSON parser, dry-run report, fixture tests.
- **Out of scope:** apply mode, LLM, catalog.
- **Dependencies:** phase 1 preferred; can start parser earlier with fixtures.
- **Entry criteria:** import file format understood enough for fixture parser.
- **Exit criteria:** dry-run reports counts / date / users / replies / duplicates / policy
  markers.
- **Acceptance:** dry-run does not write message content.
- **Risks:** export format ambiguity.
- **Rollback:** no content mutation.

### Phase 2b — Telegram Desktop import apply (only after governance skeleton)

- **Objective:** import history through same governed normalization path.
- **Scope:** synthetic `telegram_updates`, idempotent apply, duplicate prevention, reply
  resolver, tombstone / policy checks.
- **Dependencies:** phase 1 + phase 3 skeleton.
- **Entry criteria:** `forget_events`, `offrecord_marks`, `#nomem` / `#offrecord` detector
  exist.
- **Exit criteria:** same export can be applied twice without duplicates.
- **Acceptance:** imported rows tagged by `ingestion_run_id`; tombstone conflicts skipped.
- **Risks:** resurrection, duplicate imports, sensitive historical content.
- **Rollback:** imported run can be logically rolled back before derived layers.

### Phase 3 — governance

- **Objective:** implement memory privacy / governance primitives.
- **Scope:** `#nomem`, `#offrecord`, `/forget`, `/forget_me`, `offrecord_marks`,
  `forget_events`, `admin_actions`, cascade skeleton.
- **Dependencies:** phase 1.
- **Entry criteria:** messages and versions exist.
- **Exit criteria:** forbidden content is excluded from future search / extraction / import.
- **Acceptance:** `/forget` creates tombstone; offrecord content redacted; nomem excluded;
  reimport tombstone check exists.
- **Risks:** incomplete cascade, historical `raw_json` content.
- **Rollback:** completed forget is not casually rolled back.

### Phase 4 — hybrid search + q&a with citations

- **Objective:** answer questions from evidence only.
- **Scope:** FTS-first retrieval, evidence bundle, citations, confidence / abstention, q&a
  traces.
- **Dependencies:** phases 1 and 3.
- **Entry criteria:** `message_versions` and governance filters exist.
- **Exit criteria:** bot can answer simple history questions with citations or refuse.
- **Acceptance:** cites `message_version_id`; excludes forbidden content; refuses no evidence.
- **Risks:** hallucination if LLM used too early.
- **Rollback:** q&a feature flag off.

### Phase 5 — events + observations + extraction candidates

- **Objective:** create structured pre-catalog memory.
- **Scope:** `llm_usage_ledger`, `extraction_runs`, `memory_events`, `observations`,
  `reflection_runs`, `memory_candidates`.
- **Dependencies:** phase 4 + `llm_gateway`.
- **Entry criteria:** governance filters, evidence bundle, ledger, budget guard.
- **Exit criteria:** high-signal windows produce sourced candidates.
- **Acceptance:** no forbidden source sent to LLM; every output has source refs.
- **Risks:** hallucinated extraction, budget runaway.
- **Rollback:** derived rows rebuildable / deletable.

### Phase 6 — knowledge cards + admin review

- **Objective:** curated catalog with review and source trace.
- **Scope:** `memory_items`, `knowledge_cards`, `card_sources`, `card_relations`, review UI.
- **Dependencies:** phase 5.
- **Entry criteria:** candidates with source refs and admin actions.
- **Exit criteria:** admin can approve cards with citations.
- **Acceptance:** card cannot become active without source; visibility enforced.
- **Risks:** extractions becoming "truth" without review.
- **Rollback:** hide cards via status / flag.

### Phase 7 — daily summaries

- **Objective:** daily sourced recap.
- **Scope:** `summaries`, `summary_sources`, daily draft, review / publish.
- **Dependencies:** phase 4 minimum; phase 5 / 6 recommended.
- **Acceptance:** every bullet has source; forgotten source redacts bullet.

### Phase 8 — weekly digest

- **Objective:** weekly editorial digest.
- **Scope:** `digests`, `digest_sections`, weekly ranking / review / publish.
- **Dependencies:** phase 7 and cards / events preferred.
- **Acceptance:** required sections have sources and review state.

### Phase 9 — wiki / community catalog

- **Objective:** internal / member browsable catalog.
- **Scope:** member / internal pages, digest archive, source citations, visibility filters.
- **Dependencies:** phases 3, 6, 8.
- **Acceptance:** member catalog shows only approved visible cards. **Public wiki disabled by
  default.**

### Phase 10 — Graphiti / Neo4j projection

- **Objective:** derived graph traversal.
- **Scope:** `graph_sync_runs`, incremental / full rebuild, graph sidecar.
- **Dependencies:** stable cards / events / relations.
- **Acceptance:** graph can be rebuilt from postgres; forget purges graph.

### Phase 11 — shkoderbench / evals hardening

- **Objective:** regression safety for q&a / catalog / digest / governance.
- **Scope:** `eval_cases`, `eval_runs`, `eval_results`, runner, admin eval view.
- **Acceptance:** no-evidence, citation, leakage, stale tests exist.

### Phase 12 — future butler action layer (design only / postponed)

- **Objective:** preserve extension points only.
- **Scope:** design notes, **no code execution**.
- **Acceptance:** team has documented boundary for future butler.
- **No-go:** no butler code shipped.

---

## §3. Dependency graph

```
phase 0 safety
├── forward_lookup privacy
├── upsert containment
├── idempotent message save
└── tests / health / docs

phase 1 source-of-truth
├── feature_flags
├── ingestion_runs
├── telegram_updates
├── extend chat_messages
│   ├── reply_to_message_id
│   ├── message_thread_id
│   ├── caption / message_kind
│   └── memory_policy / visibility fields
├── message_versions
│   └── edited_message handler
└── metadata tables
    ├── message_entities
    ├── message_links
    └── attachments

phase 3 governance
├── offrecord_marks
├── forget_events
├── admin_actions
├── /forget
├── /forget_me
└── cascade skeleton

phase 2 import
├── 2a dry-run can start after parser fixtures
└── 2b apply blocked by phase 3 skeleton

phase 4 q&a
├── blocked by message_versions
├── blocked by governance filters
├── full-text search
├── evidence bundle
└── q&a handler

phase 5 extraction
├── blocked by q&a / source trace
├── blocked by governance
├── blocked by llm_gateway / ledger
└── events / observations / candidates

phase 6 cards
└── blocked by candidates + admin review

phase 7/8 digest
└── blocked by source trace + events / cards

phase 9 wiki
└── blocked by governance + review + source trace

phase 10 graph
└── blocked by cards / events / relations stable

phase 12 butler
└── blocked / postponed; design only
```

### Sequential blockers

| Blocked item                | Blocker                                                  |
|-----------------------------|----------------------------------------------------------|
| `edited_message` allowed    | `message_versions` table + handler                       |
| `message_reaction` updates  | reactions table + handler                                |
| import apply                | tombstone + policy skeleton                              |
| q&a                         | `message_versions` + governance filters                  |
| LLM answer generation       | `llm_gateway` + ledger                                   |
| extraction                  | governance + evidence / source trace + LLM gateway       |
| cards                       | candidates + admin review                                |
| wiki                        | cards + review + visibility + governance                 |
| graph                       | postgres events / cards / relations stable               |
| butler                      | future permissions / action audit, not now               |

---

## §4. Implementation epics

| Epic | Title                                       | Phase | Pri | Description                                              |
|------|---------------------------------------------|-------|-----|----------------------------------------------------------|
| E0   | Gatekeeper safety fixes                     | 0     | p0  | Fix current privacy / runtime risks from audit.          |
| E1   | Feature flags                               | 1     | p0  | Persistent rollout flags for memory surfaces.            |
| E2   | Raw Telegram update log                     | 1     | p0  | Store raw updates before normalization.                  |
| E3   | Normalized message archive                  | 1     | p0  | Extend `chat_messages` for normalized fields.            |
| E4   | Message versions and edit handling          | 1     | p0  | Create `message_versions`; add edit handler.             |
| E5   | Reply / thread / caption / entities / links / attachments metadata | 1 | p1 | Normalize Telegram metadata. |
| E6   | `#nomem` / `#offrecord` policy detection    | 3     | p0  | Deterministic policy detection before memory / search.   |
| E7   | `forget_events` / tombstones / cascade      | 3     | p0  | Right-to-forget primitives.                              |
| E8   | Telegram Desktop import dry-run             | 2a    | p1  | Parse export and report stats.                           |
| E9   | Telegram Desktop import apply               | 2b    | p1  | Import via synthetic updates.                            |
| E10  | Full-text search and evidence bundle        | 4     | p0  | FTS + evidence / citations.                              |
| E11  | Q&A with citations / confidence / abstention| 4     | p0  | Member q&a from evidence only.                           |
| E12  | LLM gateway and usage ledger                | 5     | p0  | Single audited LLM path.                                 |
| E13  | Reactions ingestion and scoring             | 5     | p1  | Add reactions as importance signal.                      |
| E14  | Event calendar                              | 5     | p1  | Structured `memory_events`.                              |
| E15  | Observations / reflection / candidates      | 5     | p1  | Analytical layer and review candidates.                  |
| E16  | Knowledge cards / sources / relations       | 6     | p1  | Curated catalog data model.                              |
| E17  | Admin review UI                             | 6     | p1  | Review candidates / cards / digests / governance.        |
| E18  | Daily summary                               | 7     | p2  | Sourced daily recap.                                     |
| E19  | Weekly digest                               | 8     | p2  | Weekly editorial digest.                                 |
| E20  | Wiki / community catalog                    | 9     | p2  | Internal / member catalog / wiki.                        |
| E21  | Graph projection                            | 10    | p2  | Graphiti / Neo4j projection.                             |
| E22  | Shkoderbench / evals                        | 4–11  | p1  | Eval cases / runs / results.                             |
| E23  | Future butler extension design — postponed  | 12    | p2  | Document action boundary only.                           |

---

## §5. Ticket backlog (full)

> Authorized for the immediate cycle: **all of T0-* and T1-* below**, plus stretch T3-01 and
> T2-01. Everything else is for later phases. See `AUTHORIZED_SCOPE.md`.

### Phase 0 tickets

| ID    | Title                                          | Pri | Cmplx | Deps     | Acceptance                                                              |
|-------|------------------------------------------------|-----|-------|----------|-------------------------------------------------------------------------|
| T0-01 | Fix `forward_lookup` membership/admin check    | p0  | s     | none     | non-member denied; member allowed; admin allowed; no intro in denial    |
| T0-02 | Fix / contain sqlite vs postgres upsert        | p0  | m     | none     | upsert works in configured dev / test                                   |
| T0-03 | Make `MessageRepo.save` idempotent             | p0  | m     | T0-02    | duplicate same chat / message no error                                  |
| T0-04 | Implementation status doc                      | p1  | s     | none     | doc names memory as not implemented                                     |
| T0-05 | `/healthz` + startup checks                    | p1  | m     | none     | `/healthz` exists; web health test                                      |
| T0-06 | Regression tests for T0-01..T0-03              | p0  | m     | T0-01..03| privacy / upsert / duplicate tests green                                |

### Phase 1 tickets

| ID    | Title                                          | Pri | Cmplx | Deps     | Acceptance                                                              |
|-------|------------------------------------------------|-----|-------|----------|-------------------------------------------------------------------------|
| T1-01 | `feature_flags` table / repo                   | p0  | s     | T0       | default memory flags off                                                |
| T1-02 | `ingestion_runs`                               | p0  | s     | T1-01    | run row tracks live / import                                            |
| T1-03 | `telegram_updates`                             | p0  | m     | T1-02    | unique `update_id`; idempotent insert                                   |
| T1-04 | Raw update persistence service                 | p0  | m     | T1-03    | message update writes raw row before normalization; **MUST include stub `governance.detect_policy()` returning `('normal', None)` called in same DB transaction as raw insert** (cross-ref AUTHORIZED_SCOPE `#offrecord` ordering rule); raw archive flag defaults OFF |
| T1-05 | Extend `chat_messages` fields                  | p0  | l     | T1-04    | old rows survive; new fields nullable                                   |
| T1-06 | `message_versions`                             | p0  | l     | T1-05    | new message has v1                                                      |
| T1-07 | Backfill v1 `message_versions`                 | p0  | m     | T1-06    | all existing messages get v1                                            |
| T1-08 | `content_hash` strategy                        | p0  | s     | T1-06    | same content same hash; uses normalized text+caption+entities+kind      |
| T1-09 | Persist `reply_to_message_id`                  | p0  | s     | T1-05    | reply id stored                                                         |
| T1-10 | Persist `message_thread_id`                    | p0  | s     | T1-05    | thread id stored if present                                             |
| T1-11 | Persist caption / `message_kind`               | p0  | m     | T1-05    | caption not lost; media classified                                      |
| T1-12 | Minimal `#nomem` / `#offrecord` detector       | p0  | m     | T1-05    | `#nomem` and `#offrecord` detected in BOTH text AND caption fields; `chat_messages.memory_policy` persists; `offrecord_marks` row created (with T1-13); `#offrecord` raw_json text/caption fields redacted in same transaction as raw insert (cross-ref AUTHORIZED_SCOPE §`#offrecord` ordering rule) |
| T1-13 | `offrecord_marks` minimal table                | p0  | m     | T1-12    | mark exists for policy token                                            |
| T1-14 | `edited_message` handler (after versions)      | p1  | m     | T1-06    | edit creates v2                                                         |

### Phase 2/3 tickets (stretch only T2-01 and T3-01 in this cycle)

| ID    | Title                                          | Pri | Cmplx | Deps             | Acceptance                                              |
|-------|------------------------------------------------|-----|-------|------------------|---------------------------------------------------------|
| T2-01 | Import dry-run parser                          | p1  | m     | T1-02            | no content writes; fixture dry-run                      |
| T2-02 | Import dry-run duplicate / policy stats        | p1  | m     | T2-01, T1-12     | stats include conflicts                                 |
| T3-01 | `forget_events` table                          | p0  | m     | T1-13            | tombstone key unique                                    |
| T3-02 | `/forget` reply command                        | p0  | m     | T3-01, T1-06     | own / admin allowed, others denied                      |
| T3-03 | `/forget_me` skeleton                          | p1  | l     | T3-01            | event created and queued                                |
| T3-04 | Cascade worker skeleton                        | p0  | l     | T3-01            | event status progresses                                 |
| T3-05 | Reimport tombstone prevention                  | p0  | m     | T3-01, T2-01     | skipped content recorded                                |
| T2-03 | Import apply with synthetic updates            | p1  | xl    | T3-05, T1-04     | no duplicates                                           |

### Phase 4+ tickets (later cycles)

T4-01 FTS, T4-02 evidence bundle, T4-03 q&a handler, T4-04 citations / confidence / abstention,
T4-05 q&a traces, T5-01 LLM gateway + ledger, T5-02 extraction runs, T5-03 memory events,
T5-04 observations / reflection / candidates, T6-01 cards + sources, T6-02 card relations + admin
review UI, T7-01 daily summary, T8-01 weekly digest, T9-01 member / internal wiki, T10-01 graph
projection, T11-01 eval tables / runner.

---

## §6. Database migration specification (essentials)

### Global migration rules

- Early migrations are **additive**.
- Do not add NOT NULL columns to populated tables without default / backfill.
- Preserve existing gatekeeper tables and behaviour.
- `chat_messages` stays the canonical normalized message table early on.
- Imported data must be tagged by `ingestion_run_id`.
- Tombstones are durable.
- Derived layers are rebuildable.

### Authorized migrations (this cycle — strict)

Only migrations that have a corresponding ticket in `AUTHORIZED_SCOPE.md` are authorized in
this cycle. Implementing additional migrations without a ticket is out-of-scope creep.

| # | Migration name                          | Ticket  | Notes                                                          |
|---|-----------------------------------------|---------|----------------------------------------------------------------|
| 1 | `add_feature_flags`                     | T1-01   | Unique `(flag_key, scope_type, scope_id)`. Memory flags off.   |
| 2 | `add_ingestion_runs`                    | T1-02   | `run_type` check: live / import / dry_run / cancelled.         |
| 3 | `add_telegram_updates`                  | T1-03   | Unique `update_id` (partial where not null). Raw archive.      |
| 4 | `extend_chat_messages`                  | T1-05   | All new columns nullable / default. Backfill memory_policy='normal'. |
| 5 | `add_message_versions`                  | T1-06 + T1-07 | Unique `(chat_message_id, version_seq)`. v1 backfill.    |
| 6 | `add_offrecord_marks`                   | T1-13   | Deterministic detector writes here.                            |
| 7 | (stretch) `add_forget_events`           | T3-01   | Tombstone skeleton. Stretch.                                   |

### NOT authorized this cycle (later phases — DO NOT IMPLEMENT)

| Migration name                          | Phase   | Why not now                                                    |
|-----------------------------------------|---------|----------------------------------------------------------------|
| `add_chat_threads`                      | 1+      | No ticket in this cycle. Adding a `message_thread_id` int on `chat_messages` is enough for T1-10. |
| `add_message_entities`                  | 1+      | Derived metadata. Defer until search / qa needs it.            |
| `add_message_links`                     | 1+ / 4  | Defer until search / qa needs link normalization.              |
| `add_attachments`                       | 1+ / 5  | Defer until reactions / extraction needs media metadata.       |
| `add_user_consent_anonymization_fields` | 3       | Phase 3 governance.                                            |
| `add_admin_actions`                     | 3       | Phase 3 governance audit.                                      |
| `add_search_indexes_and_qa_traces`      | 4       | Phase 4 q&a.                                                   |
| `add_reactions`                         | 5       | Phase 5; requires reactions handler first.                     |
| `add_llm_usage_ledger`                  | 5       | Phase 5; requires `llm_gateway`.                               |
| `add_extraction_runs` and downstream    | 5+      | Phase 5+ extraction.                                           |
| `add_memory_events`                     | 5       | Phase 5.                                                       |
| `add_observations`                      | 5       | Phase 5.                                                       |
| `add_reflection_runs`                   | 5       | Phase 5.                                                       |
| `add_memory_candidates`                 | 5       | Phase 5.                                                       |
| `add_memory_items`                      | 6       | Phase 6 catalog.                                               |
| `add_knowledge_cards`                   | 6       | Phase 6 catalog.                                               |
| `add_card_sources`                      | 6       | Phase 6.                                                       |
| `add_card_relations`                    | 6       | Phase 6.                                                       |
| `add_summaries` / `add_summary_sources` | 7       | Phase 7.                                                       |
| `add_digests` / `add_digest_sections`   | 8       | Phase 8.                                                       |
| `add_graph_sync_runs`                   | 10      | Phase 10.                                                      |
| `add_eval_tables`                       | 11      | Phase 11.                                                      |

Full column-by-column spec lives further down in the architect's original handoff (see archived
copy if needed). For each migration in this cycle, the implementation ticket holds the column
list it implements.

---

## §7. Module and service contracts (this cycle)

### `bot/services/ingestion.py` (Phase 1)

- **Public methods:** `record_update(update, source_kind='live', ingestion_run_id=None) ->
  TelegramUpdateRecord`; `get_or_create_live_run()`; `mark_update_processed(update_id, status)`.
- **Idempotency:** `update_id` for live; raw hash / export id for import.
- **Must not:** call LLM; do extraction; do catalog; do policy redaction beyond delegating to
  governance redactor when it exists.
- **Tests:** duplicate update; raw hash; import synthetic update.

### `bot/services/normalization.py` (Phase 1)

- **Public methods:** `normalize_message(raw_update_id, message_obj)`,
  `normalize_edited_message(raw_update_id, edited_message_obj)`,
  `extract_entities_links_attachments(message_version_id, message_obj)`.
- **Outputs:** `chat_message`, `message_version`, metadata rows.
- **Idempotency:** `(chat_id, message_id)` and `(message_id, content_hash)` for versions.
- **Must not:** summarize / extract; fetch external; generate answers.
- **Tests:** text / caption / reply / thread / edit / media.

### `bot/services/governance.py` (Phase 1 detector skeleton; Phase 3 full)

- **Public methods (this cycle):** `detect_policy(text, caption)`,
  `apply_policy(message_id, policy)`. Tombstone / cascade / forget come in Phase 3.
- **Fail closed:** if policy unknown, exclude from memory until resolved.
- **Must not:** silently hard-delete without tombstone / admin action.
- **Tests:** `#nomem` / `#offrecord` token detection in text and caption.

### `bot/services/health.py` (Phase 0)

- **Public methods:** `check_db()`, `check_bot_identity()`, `check_settings()`, `report()`.
- Used by `/healthz` and startup.

---

## §8. Bot runtime changes

### `allowed_updates` rollout

| Stage                    | `allowed_updates`                                        | Reason                              |
|--------------------------|----------------------------------------------------------|-------------------------------------|
| Current                  | `message`, `callback_query`, `chat_member`, `my_chat_member` | current gatekeeper              |
| After Phase 1 versions   | add `edited_message`                                     | only when `message_versions` and handler exist |
| After reactions table    | add `message_reaction`, `message_reaction_count`         | only when persisted / processed     |

Never silently — adding an update type without a handler = invisible data loss.

### Feature flag gating (Phase 1+)

> **Naming convention:** feature flag keys use **dot notation** (`memory.ingestion.raw_updates.enabled`).
> Database column names use **underscore** (`memory_policy`, `is_redacted`). These are
> deliberately different — a flag-key lookup is `feature_flags.get('memory.ingestion.raw_updates.enabled')`,
> a column read is `chat_messages.memory_policy`. Do not confuse them.

Flag keys (canonical defaults: ALL OFF until their phase gate):

- `memory.ingestion.raw_updates.enabled` — raw `telegram_updates` write enabled (T1-03 / T1-04). MUST stay OFF until T1-12 + T1-13 land per the `#offrecord` ordering rule.
- `memory.qa.enabled` — q&a handler enabled (Phase 4)
- `memory.import.apply.enabled` — Telegram Desktop import apply (Phase 2b)
- `memory.extraction.enabled` — LLM extraction runs (Phase 5)
- `memory.cards.enabled` — knowledge cards lifecycle (Phase 6)
- `memory.digest.enabled` — daily / weekly digests (Phase 7-8)
- `memory.wiki.enabled` — internal / member wiki (Phase 9)
- `memory.graph.enabled` — graph projection sync (Phase 10)

Default: all flags OFF in this cycle. Operators may toggle `memory.ingestion.raw_updates.enabled`
ON ONLY after T1-12 / T1-13 land AND the `#offrecord` ordering rule is verifiable.

### Startup checks (Phase 0)

- bot identity check
- DB connection
- configured community chat id present
- `allowed_updates` logged
- feature flags snapshot logged without secrets

### Health (`/healthz`)

- app alive + DB reachable
- no env values in response

---

## §9. Ingestion + normalization spec

### Live message update flow

> **Critical ordering rule:** steps 1 and 3 happen in the SAME DB transaction. The raw insert
> calls `governance.detect_policy()` (stub or real) BEFORE commit. If `detect_policy` returns
> `'offrecord'`, the raw_json text/caption/entities are redacted (nulled or sentinel-replaced)
> in the same transaction. Hash / ids / timestamps / policy marker are kept. See
> AUTHORIZED_SCOPE.md §`#offrecord` ordering rule.

1. Persist raw `telegram_updates` row in transaction T (idempotent on `update_id`); do NOT
   commit yet.
2. Normalize user / chat (upsert users; create chat row if phase includes chats; do not infer
   consent). Same transaction T.
3. Detect `#nomem` / `#offrecord` deterministically (T1-12 detector or T1-04 stub); write
   `memory_policy`; write `offrecord_marks` when table exists. If `'offrecord'`: redact
   raw_json content fields in transaction T BEFORE commit.
4. Upsert `chat_messages` keyed by `(chat_id, message_id)` with new normalized fields.
5. Create `message_versions` v1 if new message; if duplicate same hash → no new version.
6. Persist `reply_to_message_id` (nullable; unresolved OK).
7. Persist `message_thread_id` (nullable; create `chat_threads` row if explicit topic).
8. Persist caption / `message_kind`. Captions are first-class content. Media-only messages still
   get metadata.
9. Persist entities / links / attachments metadata as the phase allows. No external fetch. No
   attachment download by default.
10. Do not extract / summarize / search forbidden content. `#nomem`: exclude from memory /
    search / q&a / extraction. `#offrecord`: redact content per policy.

### Edited message flow

1. Persist raw update.
2. Find `chat_messages` by `(chat_id, message_id)`.
3. Compute new content hash.
4. If hash changed:
   - insert `message_versions` with `version_seq = max + 1`
   - update `current_version_id`
   - update current text / caption compatibility fields if needed
5. Re-run policy detection.
6. Mark derived objects stale later.
7. If original message missing: create placeholder `chat_messages` with
   `message_kind='unknown_prior_version'`, then version from edit; log unresolved prior version.

### Transaction boundaries

- Raw update insert in its own safe transaction OR at the beginning of the update transaction.
- Normalization in same transaction after raw insert.
- Derived jobs queued only after commit.
- Duplicate conflicts handled in repos, not broad handler rollback.

### Idempotency keys

| Object                        | Key                                                            |
|-------------------------------|----------------------------------------------------------------|
| Raw live update               | `update_id`                                                    |
| Synthetic import update       | `ingestion_run_id` + `export_message_id` or raw hash           |
| Chat message                  | `(chat_id, message_id)`                                        |
| Message version               | `(chat_message_id, content_hash)` or `(chat_message_id, version_seq)` |
| Link                          | `(message_version_id, canonical_url)`                          |
| Attachment                    | `file_unique_id` + message ref                                 |

### `content_hash` strategy

T1-08 (commit f017be4) ratified the recipe with a format version tag and entity
normalization. The canonical `chv1` payload is:

```
[HASH_FORMAT_VERSION, text, caption, message_kind, normalized_entities]
```

Where:
- `HASH_FORMAT_VERSION` = `"chv1"` (bumped on any recipe change that produces
  different output for the same logical content)
- `text` and `caption` default to `""` if `None`
- `message_kind` defaults to `"text"` if `None`
- `normalized_entities` is the entity list sorted by `(offset, length, type)`;
  empty list and `None` are treated identically as `[]`

Serialized via `json.dumps(..., sort_keys=True, separators=(",":"), ensure_ascii=False)`,
SHA-256 hex.

Do not hash volatile `raw_json` fields. The `compute_content_hash()` signature
accepts ONLY the four canonical inputs; passing `date`, `message_id`, `raw_json`,
`from_user`, etc. raises `TypeError`.

T1-07 backfilled v1 rows persist with the legacy first-cut hashes (no version tag,
no entity normalization). chv1 hashes apply to live-ingested versions only
(T1-14+). Repo idempotency `(chat_message_id, content_hash)` is unaffected by the
divergence — different recipes for the same logical content correctly produce
distinct version rows.

### `raw_json` redaction strategy

For `#offrecord`:
- keep ids / timestamps / hash / policy marker
- redact text / caption / entities / media captions
- keep tombstone key

For `#nomem`:
- content may remain internal / raw but excluded from derived layers

---

## §10. Governance spec (skeleton)

### Policy table

| Policy   | Stored content                  | Search / q&a | Extraction | Summaries / digest | Catalog / wiki / graph | LLM         |
|----------|---------------------------------|--------------|------------|--------------------|-------------------------|-------------|
| normal   | yes                             | yes if visibility allows | yes if eligible | yes              | yes if reviewed         | via gateway |
| nomem    | internal / raw allowed          | no           | no         | no                 | no                      | no          |
| offrecord| redacted / minimal metadata     | no           | no         | no                 | no                      | no          |
| forgotten| tombstone / minimal metadata    | no           | no         | no                 | no                      | no          |

### `#nomem`

- Detect in text / caption.
- Set `memory_policy='nomem'`.
- Write `offrecord_marks(mark_type='nomem')`.
- Exclude before FTS / vector / extraction / q&a / summary / catalog.

### `#offrecord`

- Detect in text / caption.
- Set `memory_policy='offrecord'`.
- Write `offrecord_marks(mark_type='offrecord')`.
- Redact durable content. Keep ids / timestamps / hash / policy marker.

### `#offrecord` ordering rule (binding for all ingestion paths)

`detect_policy(text, caption)` MUST be invoked BEFORE any UPDATE that writes content
fields to the DB. The detector + the redactor + the persistence MUST run inside the
same transaction so that a crash mid-flow rolls back atomically and never leaves the
DB with `text` set while `memory_policy='offrecord'`.

Live paths that satisfy this today: `bot/handlers/chat_messages.py` (new messages),
`bot/handlers/edited_message.py` (edits), `bot/services/ingestion.py::record_update`
(raw archive, gated). Phase 2 importer and any new writer MUST route through the same
helper (issue #89: `persist_message_with_policy`).

### `#offrecord` irreversibility doctrine

Once a message has been ingested as `offrecord` (or flipped to it via edit), the
original content window is **destroyed permanently**:

- `chat_messages.text/caption/raw_json` are NULL.
- Every existing `message_versions` row of that chat_message is also redacted in the
  same transaction (added by Phase 1 final-review hotfix — see issue closing the
  Codex CRITICAL "PRIVACY_LEAK_CLASS_4"): `text/caption/normalized_text/entities_json`
  → NULL, `is_redacted=True`. `content_hash` is intentionally LEFT INTACT so prior
  citations resolve, but the redacted flag tells consumers to skip the body.
- `offrecord_marks` row records the transition.
- A subsequent edit removing `#offrecord` flips `memory_policy='normal'` on the parent
  but does NOT restore `text/caption/raw_json` — the original content is unrecoverable.
- The new edit's content is recorded only in a new `message_versions` row (when the
  hash differs from the redacted-state hash) — Phase 4 q&a sees `is_redacted=True`
  on the version anyway and excludes it from citations.

### `#offrecord` redacted-state hashing in `message_versions`

For any `message_versions` row whose `is_redacted=True`, `content_hash` MUST be derived
from the redacted state (`compute_content_hash(text=None, caption=None, message_kind,
entities=None)`) — NOT from the raw edit content. Storing the chv1 of the un-redacted
content would let anyone with read access verify "was content X said?" by computing
chv1(X) and grep'ing the column. This rule applies on the version-insert path; existing
backfilled v1 rows whose content was NULLed by the irreversibility hotfix above retain
their original hash (citation stability) but are flagged `is_redacted=True`.

### `forget_events` (Phase 3)

Required fields: target type / id; actor; `authorized_by`; `tombstone_key`; reason; policy;
status; cascade status.

Tombstone keys (multiple matching where possible):
- `message:<chat_id>:<message_id>`
- `message_hash:<sha256 normalized content>`
- `user:<telegram_user_id>`
- `export:<source>:<export_message_id>` for import

### `/forget` authorization (Phase 3)

| Actor          | Target              | Allowed                        |
|----------------|---------------------|--------------------------------|
| author         | own message         | yes                            |
| admin          | any message         | yes                            |
| other member   | another user message| no, or admin-review request    |
| unknown        | any                 | no                             |

### Cascade layers (Phase 3 skeleton)

`chat_messages` → `message_versions` → `message_entities` → `message_links` → `attachments` →
FTS / search materialized rows if present. Later: vectors, events, observations, candidates,
cards, summaries, digests, graph, wiki rendered pages, eval cases.

### Phase 2 risk map (added after Phase 1 final ag-sa audit)

These risks are unique to the importer + tombstone phase and should drive the test design
and review checklist for the corresponding tickets. Numbering matches the ag-sa audit.

| Risk | Severity | Mitigation | Tracking |
|------|----------|-----------|---------|
| R1: Resurrection via re-import — operator forgets a message, later re-runs an old export, T3-05 misses → forgotten content returns. | HIGH | Tombstones immortal (no TTL); T3-05 always-on (no feature flag); test re-import after delayed forget. | T3-05 (issue #97) |
| R2: User identity collision — ghost users (deleted Telegram accounts) merged with live users by display_name match → cross-user privacy leak. | HIGH | Ghost users tagged `is_imported_only=true`, NEVER merged with live users; explicit policy in T2-NEW-B. | T2-NEW-B (issue #93) |
| R3: Lock contention live ↔ import — bulk INSERTs in import block live ingestion. | MEDIUM | Chunking + sleep between chunks (T2-NEW-F); optional advisory lock per ingestion_run_id. | T2-NEW-F (issue #102) |
| R4: Silent governance bypass in importer — implementer skips `detect_policy` on "old" historical data → `#offrecord` from history lands in raw without redaction. | HIGH | Cross-cutting binding rule: import apply MUST call `persist_message_with_policy()` (issue #89). Direct INSERT into `chat_messages` forbidden — enforced by reviewer checklist. | T2-03 (issue #103) + #89 |
| R5: Reply chain partial truth — incomplete export → `reply_to_message_id` points at missing parent, Phase 4 q&a evidence shows `reply to [missing]`. | MEDIUM | Reply resolver returns NULL on unresolved (T2-NEW-C); downstream filters; report % broken in dry-run (T2-02). | T2-NEW-C (issue #98), T2-02 (#99) |

---

## §11. Test strategy and quality gates

| Category               | Phase | What to test                                              |
|------------------------|-------|-----------------------------------------------------------|
| Unit                   | 0     | Pure services / repos. Policy detector, content hash.     |
| DB                     | 0–1   | Migrations / repos / constraints. Upsert, duplicate, v1.  |
| Handler                | 0–4   | Bot handlers offline. forward lookup, q&a trigger, forget.|
| Import                 | 2     | Dry-run / apply fixtures. Duplicate, replies, media.      |
| Governance             | 3     | Policy / forget / cascade. nomem exclusion, offrecord.    |
| Search / q&a           | 4     | FTS / evidence / answer. No-evidence refusal, citations.  |
| Extraction             | 5     | Schemas / ledger / source validation.                     |
| Catalog                | 6     | Card lifecycle. Active card needs source / review.        |
| Digest                 | 7–8   | Summary / digest sources. Every bullet has source.        |
| Wiki visibility        | 9     | member / public / internal filters.                       |
| Graph rebuild          | 10    | Derived graph. Full rebuild, forget purge.                |
| Eval / shkoderbench    | 4–11  | Regression suite.                                         |

### Must-have tests (this cycle)

- non-member `forward_lookup` denied — DONE in T0-01
- `UserRepo.upsert` works in dev / test — T0-02
- duplicate message safe — T0-03
- raw update idempotency — T1-03
- `message_versions` v1 backfill — T1-07
- edited message creates v2 — T1-14 (after T1-06)
- `reply_to_message_id` persists — T1-09
- `message_thread_id` persists — T1-10
- caption persists — T1-11
- `#nomem` excluded from memory derived layers — T1-12 / T1-13

### §11.1 Dual-team independent implementation pattern

When to apply:
- Ticket touches privacy-critical irreversibility (offrecord, redaction, retention).
- Bug would be invisible to unit tests (data-flow / state-transition logic).
- Cost of post-merge fix > cost of 2x agent time.

Protocol:
1. Spawn Team A and Team B in isolated worktrees, same ticket spec, NO cross-talk.
2. Both submit PR drafts.
3. Codex reviews both diffs side-by-side, flags divergences.
4. Merge winning approach OR synthesis.

Precedent: T1-14 (PR #75 + hotfix #90) — Codex caught privacy bug in Team A's diff
(`new_policy != "offrecord"` was too broad, restored content on offrecord→normal flip).
Next candidates: T2-03 (import apply), any T-* touching `message_versions` retention.

---

## §12. Rollout and deployment plan

### Phase 0 rollout

- Deploy code-only fixes.
- Verify `forward_lookup` denial.
- Verify `/healthz` endpoint.
- Verify gatekeeper flows.

### Phase 1 rollout

- Apply additive migrations.
- Deploy raw update persistence disabled or shadow mode.
- Enable raw update logging.
- Verify no handler latency spike.
- Backfill v1 versions after reviewing script / migration.

### Rollback

| Layer                 | Rollback                                                |
|-----------------------|---------------------------------------------------------|
| Feature               | turn flag off                                           |
| Raw update logging    | disable new writes; keep existing rows                  |
| Import                | rollback import run before derived layers               |
| Q&A                   | disable q&a handler flag                                |
| Extraction            | disable extraction; derived rows hidden / deleted       |
| Forget                | do not rollback tombstones casually                     |

### Monitoring (this cycle)

- `/healthz` uptime
- ingestion error counts
- duplicate update count (after T1-03)
- policy detection count (after T1-12)

---

## §13. Agentic development instructions

Coding agents:

1. Always inspect current code before editing.
2. Never assume docs / specs are implemented.
3. Keep PRs small and ticket-scoped.
4. Preserve gatekeeper behaviour.
5. Add tests with every change.
6. Do not bypass repositories / services.
7. Do not log secrets / env values.
8. Do not introduce LLM calls outside `llm_gateway`.
9. Do not access forbidden content in search / extraction.
10. Do not implement future phases early.
11. Do not add update types without handler / storage.
12. Every ticket must include self-review.

### Pre-PR checklist

- ticket id included
- changed files listed
- tests added / updated
- tests run locally or verification method stated
- no secrets logged
- no future-phase scope creep
- gatekeeper compatibility checked
- migration additive or explicitly justified
- forbidden content filters considered
- rollback / disable path stated
- risks stated

---

## §14. First sprint execution plan

### Sprint goal

Within 7–10 working days: stabilize gatekeeper and lay source-of-truth foundation without
launching memory product.

### Exact task order

1. T0-01 — DONE (verified).
2. T0-02 fix / contain sqlite / postgres upsert.
3. T0-03 make `MessageRepo.save` idempotent.
4. T0-06 add regression tests for above.
5. T1-01 add `feature_flags`.
6. T1-02 add `ingestion_runs`.
7. T1-03 add `telegram_updates`.
8. T1-04 add raw update persistence service.
9. T1-05 extend `chat_messages`.
10. T1-06 / T1-07 add / backfill `message_versions`.
11. T1-09 / T1-10 / T1-11 persist reply / thread / caption / kind.
12. T1-12 / T1-13 minimal policy detector + `offrecord_marks`.
13. T1-14 `edited_message` handler only after versions.
14. Stretch: T3-01 `forget_events` skeleton.
15. Stretch: T2-01 import dry-run parser.

### Recommended parallelization (subagents)

| Agent | Work                                          |
|-------|-----------------------------------------------|
| A     | T0-01 verify (done) + T0-06 regression tests  |
| B     | T0-02, T0-03 repo / DB idempotency            |
| C     | T1-01 / T1-02 migrations                      |
| D     | T1-03 / T1-04 ingestion service               |
| E     | T1-05 / T1-06 / T1-07 schema + versions       |
| F     | T2-01 import dry-run fixture (no apply)       |

### Files / modules touched

- `bot/handlers/forward_lookup.py` (verified — no further changes this ticket)
- `bot/db/repos/user.py`
- `bot/db/repos/message.py`
- `bot/db/models.py`
- `bot/handlers/chat_messages.py`
- `bot/__main__.py`
- `bot/services/ingestion.py` (new)
- `bot/services/normalization.py` (new)
- `bot/services/governance.py` (new)
- `bot/services/health.py` (new)
- `alembic/versions/*` (new)
- `tests/*`

---

## §15. Team lead operating guide

### Sequential review required

- migrations touching existing tables
- governance / tombstones
- raw update persistence
- visibility / public surfaces

### Code review checklist

- no secrets logged
- no future-phase scope
- no direct LLM provider call
- no raw DB access from future action layer
- service / repo boundaries respected
- migrations additive
- tests cover failure cases

### Privacy checklist

- requester authorization checked
- visibility filters applied
- forbidden content excluded
- public / member separation respected
- audit actions logged

### Escalate to product owner only for

- exact retention / legal policy if stronger than defaults
- whether `/forget_me` redacts all authored content or anonymizes only
- whether public wiki is ever allowed
- whether member identity can appear in expertise pages
- whether historical already-saved content should be retrospectively scanned / redacted

---

## §16. Risk register

| Risk                                         | Phase  | Impact     | Mitigation                                              |
|----------------------------------------------|--------|------------|---------------------------------------------------------|
| Gatekeeper breakage                          | 0–1    | high       | Additive changes, feature flags                         |
| `forward_lookup` privacy leak                | 0      | high       | membership / admin check (DONE)                         |
| Dev / prod DB mismatch                       | 0      | medium-high| Dialect-safe repos or postgres dev                      |
| Raw update privacy                           | 1      | high       | offrecord redaction, no public raw views                |
| `#offrecord` stored too durably              | 3      | critical   | Redact content fields, keep minimal metadata            |
| Forget resurrection                          | 3 / 2b+| critical   | Tombstones checked everywhere                           |
| Import duplicates                            | 2      | high       | Idempotency keys                                        |
| Bad citations                                | 4      | high       | `message_versions` / evidence validation                |
| Q&A hallucination                            | 4      | high       | Evidence-only prompt / refusal                          |
| LLM cost runaway                             | 5      | medium-high| Gateway caps                                            |
| Public wiki leak                             | 9      | critical   | Public disabled by default                              |
| Person expertise creepiness                  | 6 / 9  | high       | Member / internal, evidence-based, opt-out              |
| Graph divergence                             | 10     | medium-high| Graph derived / rebuildable                             |
| Butler bypassing governance                  | 12 (future) | critical | Postponed; action boundary docs                       |
| Docs / spec confusion                        | 0+     | medium     | Implementation status doc maintained                    |
| Agents implement future phases too early     | all    | high       | Ticket scope, phase gates, checklist enforcement        |

---

## §17. Definition of done

### Ticket DoD
- ticket acceptance criteria met
- tests added / updated
- changed files listed
- no secrets
- no scope creep
- rollback / disable path stated
- privacy impact considered

### Phase DoD
- phase exit criteria met
- phase gate checklist passed
- deployment / rollback reviewed
- docs / status updated
- next phase blockers known

### Migration DoD
- additive or explicitly reviewed
- no unsafe non-null on populated table
- indexes / constraints reviewed
- downgrade / rollback note present
- backfill tested
- existing gatekeeper data preserved

### Governance feature DoD
- policy behaviour tested
- audit / tombstone created
- search / extraction exclusion tested
- import resurrection tested if applicable

---

## §18. Final recommendation

**Go for Phase 0 and Phase 1.**
**No-go for q&a / extraction / catalog / digest / wiki / graph / butler** until their phase gates
are satisfied.

### Exact first move

T0-01 was already merged by the security audit cycle (PR#11, commit `7f95b53`) and is verified
PASS by the architect's hard acceptance criteria. Three minor follow-up gaps captured as
T0-01-r1/r2/r3 (test coverage only).

**Next first move: T0-02 fix / contain sqlite vs postgres upsert.** Direct gatekeeper safety
fix and unblocks T0-03 (idempotent message save).

### Recommendation to team lead

Run this as a controlled migration from gatekeeper to librarian: first close current safety
gaps, then build raw / source / version / governance foundation, and only then add search /
q&a, extraction, cards, digest and wiki. Do not let AI agents accelerate into LLM, graph or
butler: without tombstones, source trace and review this becomes not a memory system but a very
confident archiver of private problems. The first sprint must be boring and ironclad — privacy,
idempotency, raw archive, versions, policy skeleton. That is what gives shkoder the right to
remember later.
