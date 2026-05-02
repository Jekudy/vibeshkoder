🚧 DRAFT — NOT AUTHORIZED

# Phase 10 Plan Draft — Graph Projection of Shkoderbot Memory System

**Working directory:** `/Users/eekudryavtsev/Vibe/products/shkoderbot`  
**Output path:** `/tmp/PHASE10_PLAN_DRAFT.md`  
**Mode:** design-only. Do not implement. Do not commit.  
**Phase:** 10 — graph projection.  

---

## §0. TBD/Status

### Status

This is a **draft only**. Phase 10 is **not authorized for implementation**.

Phase 10 may be planned now, but no code, migrations, Docker services, graph store, LLM prompts,
Telegram handlers, or runtime configuration may be changed until explicit authorization.

### Required reading status

Read and used:

- `docs/memory-system/HANDOFF.md`
- `docs/memory-system/AUTHORIZED_SCOPE.md`
- `docs/memory-system/ROADMAP.md`
- `docs/memory-system/decisions/0001-postgres-as-source-of-truth.md`
- `docs/memory-system/decisions/0003-offrecord-irreversibility.md`
- `docs/memory-system/decisions/0004-llm-gateway-as-single-boundary.md`
- `docs/memory-system/decisions/0005-graph-as-projection-not-truth.md`
- `docs/memory-system/GLOSSARY.md`

Requested but not present in the current checkout or `/tmp`:

- `docs/memory-system/PHASE4_PLAN.md`
- `docs/memory-system/prompts/PHASE6_PLAN_DRAFT.md`
- `docs/memory-system/prompts/PHASE8_PLAN_DRAFT.md`

This draft mirrors the **requested Phase 4 section structure** from the task prompt and the
Phase 4 shape in `HANDOFF.md`, but it must not be ratified until the missing requested source
documents are either supplied or explicitly waived.

### Current authorization boundary

`AUTHORIZED_SCOPE.md` says Graph projection is **not authorized**:

- no graph / Neo4j / Graphiti
- no LLM calls
- no LLM extraction
- no catalog / knowledge cards
- no wiki
- no butler
- no public surfaces

Phase 10 can only become executable after prior phase gates are complete, including governance,
LLM gateway, extraction, cards, summaries/digests where applicable, and stable source relations.

### Open for ratification

The following decisions are intentionally left open:

1. Graph store choice: Neo4j vs Graphiti vs Apache AGE vs in-memory NetworkX.
2. Hosting model: same Docker Compose stack as bot vs separate service.
3. Triple extraction prompt design for messages, cards, and observations.
4. Update cadence: real-time hooks vs scheduled batch projection.
5. Privacy model: how `#offrecord`, `#nomem`, forgotten content, roles, and visibility propagate.
6. Phase 8 source contract: exact card/observation/digest source rows are pending because
   `PHASE8_PLAN_DRAFT.md` was not available.

---

## §0a. Refinement Status (Orchestrator B sprint-0b, 2026-05-02)

**RATIFIED PENDING PHASE 6 + PHASE 8 CLOSURE** — design contract approved by
Orchestrator B sprint-0b on 2026-05-02; **implementation deferred** until ALL of:

1. Orchestrator A confirms Phase 6 (cards) AND Phase 8 (observations) BOTH CLOSED
   per `ORCHESTRATOR_REGISTRY.md §5` cross-orch dependency table:
   - "Orch A (Phase 6) → `knowledge_cards` + `card_sources` stable → Orch B (Phase
     10) graph entity nodes" (Phase 6 closure gate)
   - "Orch A (Phase 8) → `observations` table → Orch B (Phase 10) graph projection
     of observations" (Phase 8 closure gate)
2. `AUTHORIZED_SCOPE.md` updated to add Phase 10 implementation authorization block
   (currently lists Phase 10 in §"Conditionally authorized: Phase 9, Phase 10
   (gated)").
3. This draft promoted from `prompts/PHASE10_PLAN_DRAFT.md` to canonical
   `docs/memory-system/PHASE10_PLAN.md` per REGISTRY §2 Orch B exclusive write.

### Refinement deltas applied 2026-05-02

- Output-path artifact at line 6 (`/tmp/PHASE10_PLAN_DRAFT.md`) is from the
  original draft author's tooling; the canonical path on ratification will be
  `docs/memory-system/PHASE10_PLAN.md`. Not corrected in this refinement — intentional
  preservation of draft history; will be replaced at promotion time.
- Required-reading-status §0 lists three "not present" requested files
  (`PHASE4_PLAN.md`, `prompts/PHASE6_PLAN_DRAFT.md`, `prompts/PHASE8_PLAN_DRAFT.md`).
  As of 2026-05-02 ratification: `PHASE4_PLAN.md` exists at HEAD (Phase 4 closed
  2026-04-30); `prompts/PHASE6_PLAN_DRAFT.md` and `prompts/PHASE8_PLAN_DRAFT.md`
  exist at HEAD (ratified docs-only via PR #160). The "not present" notes are
  stale historical context preserved as draft provenance.
- Migration window: §6 / §7 reference T10-* tickets without a hard alembic number
  range. **Binding constraint:** alembic versions for graph schema MUST be in
  **050–069** per `ORCHESTRATOR_REGISTRY.md §2 Orch B exclusive write`
  (specifically scoped after Phase 9 wiki migrations consume the lower end of the
  range). Implementation-time decision: tentatively **060+** for graph tables to
  leave headroom for Phase 9 wiki schema migrations in 050–059.
- Ratification of "Open for ratification" §0.6 list — these 6 decisions are
  intentionally **left open** at refinement time. Each will be resolved at
  promotion time (when Phase 6/8 close) and recorded in the canonical PHASE10_PLAN.md
  Final Report Block:
  - Decision 1 (graph store): provisionally **Apache AGE** as default (postgres
    extension; same DB; no new operational service; native cascade integration with
    `forget_cascade.CASCADE_LAYER_ORDER`). Neo4j / Graphiti remain implementable
    alternatives but require new ops surface and separate forget-cascade wiring.
    Final choice deferred to promotion sprint.
  - Decision 2 (hosting): if AGE → same compose stack (free); if Neo4j/Graphiti →
    separate service. Coupled to decision 1.
  - Decision 3 (triple extraction prompts): blocked on Phase 5 LLM gateway
    closure (Orch A); the prompt template MUST flow through `llm_gateway` per
    invariant 2.
  - Decision 4 (update cadence): provisionally **scheduled batch projection** to
    avoid coupling graph rebuild to live ingestion latency. Real-time hooks
    deferred to a later optimization phase.
  - Decision 5 (privacy model): **forget cascade is canonical** — every `forget`
    event MUST purge graph_nodes / graph_edges in the same transaction layer
    (per invariant 9 + REGISTRY §2 Shared cascade discipline). Visibility
    propagates by edge filtering at query time (graph stores raw cards/observations,
    query layer filters by viewer scope).
  - Decision 6 (Phase 8 source contract): now resolvable from
    `prompts/PHASE8_PLAN_DRAFT.md` (present at HEAD); to be reconciled at promotion
    time with whatever Orch A actually ships.

### Phase 6 + Phase 8 dependency contract (what cards / observations must expose)

When Phase 6 + Phase 8 close, the graph projection service (T10-* in §6) consumes
these fields. Binding contract for the Phase 6/8 → Phase 10 handoff:

| Field needed by graph | Source (Phase 6/8 owned) | Why graph needs it |
|----------------------|--------------------------|--------------------|
| `card.id` + `card.entity_type` | `knowledge_cards` | Node creation: `(card_id, entity_type)` becomes a `graph_node` row |
| `card_relations.from_card_id`, `to_card_id`, `relation_type` | `card_relations` (Orch A Phase 6) | Edge creation: typed `graph_edge` rows |
| `card.visibility_scope`, `card.status` | `knowledge_cards` | Visibility filter at projection time + query time; never project `pending` / `rejected` |
| `card_sources.message_version_id` (for citation back-trace) | `card_sources` | Edge metadata: every projected edge carries source-trace pointer |
| `observations.subject_card_id`, `predicate`, `object_card_id` | `observations` (Orch A Phase 8) | Triple-shaped observation rows project to graph triples (the original Phase 10 design rationale) |
| `observations.confidence`, `observations.source_evidence_ids[]` | `observations` | Edge weight + citation backing |
| `forget_events.tombstone_key` | `forget_events` (existing) | Cascade input: every forget MUST purge derived graph rows |

If Phase 6 / Phase 8 ship with renamed/missing fields, Orch B re-opens this draft
and adjusts §5 / §7 before promoting to canonical path.

### Implementation gate explicitly deferred

Phase 10 implementation tickets in §6 / §7 (T10-01 through T10-09) MUST NOT be
picked up until the three pre-conditions above are satisfied. §6 / §7 / §8
substance remains the binding design contract. Orchestrator B will not open
implementation PRs on this draft until ratified via promotion to canonical path.

---

## §1. Invariants verbatim

From `docs/memory-system/HANDOFF.md §1`:

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

### Phase 10 binding

- **Invariant 6 binding:** the graph is a read-only derived projection. It is never canonical.
  PostgreSQL remains the only source of truth. The graph must be droppable and rebuildable.
- **Invariant 3 binding:** graph projection input must exclude `#nomem`, `#offrecord`, and
  forgotten content before any triple extraction, MERGE, index write, traversal, or query result.
- **Invariant 9 binding:** cascade forget must include graph nodes and graph edges. A forgotten
  source message/card/observation must purge its graph projection rows and graph-store entities.

---

## §2. Phase 10 Spec

### Objective

Project governed Shkoderbot memory into a typed graph so the system can answer butler-style
relationship questions such as:

- who is connected to topic X
- which decisions relate to project Y
- which people, cards, observations, events, and messages support a claim
- how concepts A and B are connected through reviewed community memory

The graph is a **read-only derived view**. It powers traversal and ranking, not truth storage.

### Sources

Phase 10 projects only governance-filtered source rows from PostgreSQL:

- `message_versions` through approved evidence/citation contracts
- `memory_events` from Phase 5, if available and governed
- `observations` from Phase 5, if available and governed
- `memory_candidates` only if their lifecycle allows projection; default is no projection until
  reviewed or explicitly marked projectable
- `knowledge_cards` from Phase 6 after admin review and active status
- `card_sources` and `card_relations`
- digest/summary source links only as secondary context, never canonical source rows

Per the user requirement, Phase 8 draft intent is treated as: **cards + observations are graph
projection sources**. Because `PHASE8_PLAN_DRAFT.md` was unavailable, the exact Phase 8 contract
requires ratification before implementation.

### Graph shape

The graph should be typed and provenance-bearing.

Candidate node types:

- `Person`
- `Topic`
- `Project`
- `Decision`
- `Question`
- `Answer`
- `Event`
- `Observation`
- `KnowledgeCard`
- `MessageVersion`
- `Source`

Candidate edge types:

- `MENTIONS`
- `AUTHORED`
- `KNOWS_ABOUT`
- `ASKED`
- `ANSWERED`
- `DECIDED`
- `RELATED_TO`
- `SUPPORTS`
- `DERIVED_FROM`
- `PART_OF`
- `CONTRADICTS`
- `SUPERSEDES`

Every graph node and edge must carry provenance back to PostgreSQL:

- source table
- source primary key
- source `message_version_id` or approved card source where applicable
- extraction/projection run id
- governance snapshot marker
- content hash or projection hash

### Store decision

Graph store choice is open and must be ratified before implementation:

- Neo4j: strongest general graph query power and operational familiarity, but adds a separate
  service and backup/monitoring surface.
- Graphiti: attractive if temporal graph memory semantics are needed, but introduces framework
  coupling and must still obey Shkoderbot governance and source-of-truth rules.
- Apache AGE: keeps graph closer to PostgreSQL, reducing service sprawl, but query ergonomics,
  maturity, and operational fit need validation.
- NetworkX: simplest for local rebuild/eval/prototype, but not a durable production graph store
  and not enough for concurrent butler queries.

No option may become a second source of truth.

### LLM extraction

Triple extraction, if required, must go through `llm_gateway.extract_graph_triples`.

The gateway must enforce:

- no forbidden content in prompt input
- token budget per source item and per run
- model/cost logging in `llm_usage_ledger`
- structured output schema validation
- fail-closed behavior on malformed triples
- source id preservation on every triple

Open prompt design question:

- What prompt extracts typed triples from messages, cards, and observations without inventing
  unsupported nodes or edges?

Draft prompt constraints:

- extract only claims explicitly supported by the input
- preserve source ids verbatim
- emit `UNKNOWN` or empty arrays instead of guessing
- prefer existing canonical entity ids when supplied
- do not infer expertise, identity, intent, or private relationships from weak evidence
- cap triples per source item
- return typed JSON only

### Cost guardrail

LLM triple extraction must be budgeted:

- feature flag off by default
- per-run max source rows
- per-source max tokens
- per-run max tokens
- per-run max estimated cost
- skip low-signal rows before LLM
- batch size limits
- dry-run mode showing projected token/cost estimate
- ledger required before any provider call

### Read-only query behavior

`graph_query.py` exposes read-only traversal. It may return:

- node/edge ids
- relationship paths
- source references
- snippets only if already allowed by the source evidence layer
- confidence/ranking metadata

It must not return content that cannot be traced to an allowed source row. If a query result cannot
produce source provenance, it is invalid.

---

## §3. Phase 11 Boundary

Phase 11 is **shkoderbench / evals hardening**, not graph productization.

Phase 10 may define graph-specific eval needs for Phase 11, but it must not implement Phase 11.

Allowed Phase 10 handoff notes for Phase 11:

- leakage tests: graph query must not return `#offrecord`, `#nomem`, or forgotten content
- drift tests: graph query must not return nodes/edges without valid source provenance
- rebuild tests: full graph rebuild from PostgreSQL must produce deterministic counts/hashes
- cascade tests: forget must purge graph projections
- no-evidence tests: graph query must refuse or return empty result when no governed path exists

Explicitly out of Phase 10:

- no shkoderbench runner implementation
- no eval dashboard
- no nightly CI wiring
- no expertise pages
- no public/person pages

### Expertise pages boundary

No expertise pages are part of Phase 10.

The graph may later help answer "who knows X" inside a governed butler query, but durable person
expertise pages are outside Phase 10 and must remain in later catalog/wiki/member surface scope.

---

## §4. Architecture ASCII

```
PostgreSQL source of truth
  |
  |  governed source scan
  |  - message_versions
  |  - memory_events
  |  - observations
  |  - approved knowledge_cards
  |  - card_sources / card_relations
  |  - no #nomem
  |  - no #offrecord
  |  - no forgotten/tombstoned source
  v
graph_projector.py
  |
  |  projection run
  |  - graph_projection_runs
  |  - graph_provenance
  |  - idempotency keys
  |  - cost budget checks
  v
llm_gateway.extract_graph_triples
  |
  |  typed triples only
  |  - source ids preserved
  |  - budget logged
  |  - malformed output rejected
  |  - no provider call outside gateway
  v
Graph store adapter
  |
  |  Neo4j MERGE or alternative adapter
  |  - nodes MERGE by stable projection key
  |  - edges MERGE by stable projection key
  |  - provenance attached
  v
Neo4j / Graphiti / Apache AGE / NetworkX
  |
  |  read-only traversal
  v
graph_query.py
  |
  |  butler-style query API
  |  - governance-filtered
  |  - provenance required
  |  - no raw DB bypass
  v
Future butler evidence context


Forget / tombstone cascade
  |
  v
cascade graph_nodes layer
  |
  |  source forgotten
  |  - graph_provenance rows invalidated/deleted
  |  - graph node projection keys collected
  |  - graph edges collected
  |  - graph-store nodes/edges purged
  v
Graph query no longer returns forgotten source paths
```

---

## §5. Components

### A: `graph_projection_runs` + `graph_provenance` schema (Postgres side-tables)

Purpose:

- track every projection attempt
- preserve source-to-graph provenance
- support full rebuild and incremental projection
- support cascade forget into graph layer
- prove graph is derived, not canonical

Draft schema concepts:

- `graph_projection_runs`
  - `id`
  - `mode` (`dry_run`, `incremental`, `full_rebuild`, `repair`)
  - `status` (`started`, `completed`, `failed`, `cancelled`)
  - `source_cutoff_at`
  - `source_row_count`
  - `projected_node_count`
  - `projected_edge_count`
  - `skipped_policy_count`
  - `skipped_budget_count`
  - `llm_prompt_tokens`
  - `llm_completion_tokens`
  - `estimated_cost`
  - `actual_cost`
  - `started_at`
  - `finished_at`
  - `error_code`
  - `error_context`

- `graph_provenance`
  - `id`
  - `projection_run_id`
  - `source_table`
  - `source_pk`
  - `source_message_version_id`
  - `source_content_hash`
  - `graph_store`
  - `graph_node_key`
  - `graph_edge_key`
  - `triple_hash`
  - `governance_policy`
  - `visibility`
  - `created_at`
  - `purged_at`
  - `purge_reason`

Acceptance direction:

- no graph node/edge exists without provenance
- graph can be fully rebuilt from PostgreSQL
- projection rows are idempotent
- forbidden sources are skipped and counted
- tombstone cascade can find graph nodes/edges by source ids

### B: `graph_projector.py` service

Purpose:

- select governed source rows
- build projection inputs
- call `llm_gateway.extract_graph_triples` only when needed
- normalize triples into stable node/edge keys
- write provenance to PostgreSQL
- MERGE nodes/edges into graph store through an adapter

Core operations:

- `dry_run(limit, source_types)`
- `project_incremental(since_run_id | since_timestamp)`
- `project_full_rebuild()`
- `repair_source(source_table, source_pk)`
- `reconcile_counts()`

Rules:

- fail closed if governance filter is unavailable
- never read raw forbidden content
- never call an LLM provider directly
- enforce token/cost budgets before gateway call
- write run status and errors
- support idempotent retry

### C: Cascade `graph_nodes` layer (forget propagation)

Purpose:

- extend governance cascade so forgotten sources are removed from graph projection and graph store

Draft behavior:

- `/forget` or `/forget_me` creates/uses durable tombstone in `forget_events`
- cascade resolves source rows affected by the tombstone
- cascade reads `graph_provenance` for affected source ids
- cascade deletes or tombstones graph-store nodes/edges derived solely from forgotten source
- cascade updates `graph_provenance.purged_at` and `purge_reason`
- graph queries exclude purged provenance immediately

Open decision:

- synchronous purge in the same forget transaction vs async cascade worker with strict read block
  until purge completes.

Invariant binding:

- If cascade cannot purge graph nodes, graph query must fail closed or disable graph feature.

### D: `graph_query.py` read-only API

Purpose:

- provide a governed traversal API for future butler-style queries
- prevent direct graph-store access from bot handlers or future butler

Allowed operations:

- `find_related_topics(topic, filters)`
- `find_people_for_topic(topic, filters)`
- `explain_connection(node_a, node_b, filters)`
- `sources_for_path(path_id | node_edge_keys)`
- `graph_stats()`

Rules:

- read-only
- no writes, no MERGE, no LLM calls
- returns provenance with every answer
- uses role/visibility filters
- rejects results with missing source rows
- returns empty/refusal when source evidence is unavailable

### E: `/graph_project_now`, `/graph_stats`, `/graph_query` admin Telegram handlers

Purpose:

- provide admin-only operational controls and smoke checks

Handlers:

- `/graph_project_now`
  - admin-only
  - feature-flag gated
  - supports dry-run first
  - reports source rows, projected triples, skipped policy rows, token estimate, cost estimate

- `/graph_stats`
  - admin-only
  - reports run status, node/edge counts, provenance counts, purge counts, drift warnings

- `/graph_query`
  - admin-only at Phase 10
  - read-only
  - returns concise paths with source references
  - refuses if governance/provenance checks fail

Rules:

- no member-facing graph commands in Phase 10
- no expertise pages
- no public output
- no hidden butler action execution

---

## §6. Streams — 3 waves

### Wave 1 — Projection foundation

Goal:

- define graph projection contracts without choosing a store prematurely
- implement side-table design when authorized
- define source eligibility and provenance requirements

Work:

- finalize graph store decision
- finalize source row contract for messages/cards/observations
- specify `graph_projection_runs`
- specify `graph_provenance`
- specify idempotency keys and rebuild semantics
- specify dry-run stats

Exit:

- graph projection can be described as a deterministic function:
  governed PostgreSQL source rows -> typed triples -> graph-store projection.

### Wave 2 — Graph projector and cascade

Goal:

- project governed triples and make forget propagation safe

Work:

- design `graph_projector.py`
- design `llm_gateway.extract_graph_triples`
- design graph store adapter
- design cascade graph layer
- design full rebuild and incremental projection
- design budget guardrails

Exit:

- every graph node/edge has provenance
- every forgotten source can purge graph projections
- full rebuild is possible

### Wave 3 — Read-only query API and admin controls

Goal:

- expose graph traversal safely for admin/internal testing and future butler context

Work:

- design `graph_query.py`
- design `/graph_project_now`
- design `/graph_stats`
- design `/graph_query`
- define query refusal behavior
- define drift detection
- define Phase 11 eval handoff cases

Exit:

- graph is queryable only through governed read-only API
- query output is source-traceable
- no Phase 11, wiki, expertise page, public surface, or butler execution code is included

---

## §7. Tickets T10-01 through T10-NN

### T10-01 — Ratify graph store and hosting model

Acceptance criteria:

- decision recorded for Neo4j vs Graphiti vs Apache AGE vs NetworkX
- hosting model recorded: same Docker Compose vs separate service
- backup/restore and rebuild expectations documented
- operational cost and complexity documented
- explicit statement that graph is derived only

Dependencies:

- Phase 6 cards source contract
- Phase 8 source contract, if digests/observations feed graph
- product/architecture ratification

### T10-02 — Define graph source eligibility contract

Acceptance criteria:

- eligible source tables are listed
- excluded source states are listed
- `#nomem`, `#offrecord`, forgotten, non-visible, and unreviewed rows are excluded
- cards + observations source contract is ratified
- source rows define stable ids and content hashes for projection

Dependencies:

- Phase 3 governance complete
- Phase 5 observations schema complete
- Phase 6 card lifecycle complete
- missing Phase 6/8 draft inputs supplied or waived

### T10-03 — Design Postgres side-tables for projection runs and provenance

Acceptance criteria:

- `graph_projection_runs` schema accepted
- `graph_provenance` schema accepted
- uniqueness/idempotency constraints specified
- full rebuild tracking specified
- cascade lookup by source id specified
- no graph node/edge can exist without provenance

Dependencies:

- T10-01
- T10-02

### T10-04 — Design `llm_gateway.extract_graph_triples`

Acceptance criteria:

- API contract defined inside `llm_gateway`
- no direct provider calls allowed from `graph_projector.py`
- typed JSON schema defined
- token budget guardrails defined
- malformed output handling defined as fail-closed
- prompt constraints prevent unsupported inference
- every triple includes source id and confidence/reason metadata

Dependencies:

- Phase 5 `llm_gateway`
- `llm_usage_ledger`
- T10-02

### T10-05 — Design `graph_projector.py`

Acceptance criteria:

- dry-run mode specified
- incremental projection specified
- full rebuild specified
- graph store adapter boundary specified
- idempotent MERGE keys specified
- skipped policy rows are counted
- projection run status is persisted
- projector refuses to run if governance filter is unavailable

Dependencies:

- T10-01
- T10-02
- T10-03
- T10-04 if LLM extraction is used

### T10-06 — Extend cascade design with graph purge layer

Acceptance criteria:

- forget cascade includes graph provenance lookup
- graph-store node/edge purge behavior specified
- behavior for shared nodes with multiple sources specified
- query behavior during pending purge specified
- stop signal defined if purge fails
- tests/evals handed off to Phase 11

Dependencies:

- Phase 3 cascade skeleton
- T10-03
- T10-05

### T10-07 — Design `graph_query.py` read-only API

Acceptance criteria:

- read-only API methods specified
- role/visibility filters specified
- provenance-required output contract specified
- missing provenance refusal specified
- direct graph-store access by butler or handlers prohibited
- result content cannot exceed allowed source evidence

Dependencies:

- T10-01
- T10-03
- T10-05
- T10-06

### T10-08 — Design admin Telegram graph handlers

Acceptance criteria:

- `/graph_project_now` behavior specified
- `/graph_stats` behavior specified
- `/graph_query` behavior specified
- all handlers admin-only
- all handlers feature-flag gated
- dry-run is default for projection command
- no member-facing graph surface created

Dependencies:

- T10-05
- T10-07

### T10-09 — Define graph drift and rebuild checks

Acceptance criteria:

- drift conditions documented
- graph vs Postgres reconciliation counts specified
- rebuild procedure specified
- expected behavior during rebuild specified
- Phase 11 eval cases listed

Dependencies:

- T10-03
- T10-05
- T10-07

---

## §8. Stop Signals

Stop Phase 10 immediately if any of these occur:

1. `#offrecord` projection leak
   - Any `#offrecord` content reaches triple extraction, graph provenance, graph store, query API,
     logs, prompt payloads, or admin command output.

2. `#nomem` projection leak
   - Any `#nomem` source is projected into triples, graph nodes, graph edges, or graph query output.

3. Forgotten content resurrection
   - A source with a tombstone appears in graph projection, graph query, rebuild output, or admin stats
     as active content.

4. Cascade not deleting graph nodes
   - Forget propagation cannot purge graph nodes/edges or cannot prove they are excluded from query.

5. Query returning content not in source = drift
   - `graph_query.py` returns a claim, snippet, node, or path that cannot be traced to governed
     PostgreSQL source rows.

6. LLM outside gateway
   - Any graph component calls an LLM provider directly instead of `llm_gateway.extract_graph_triples`.

7. Cost runaway
   - Projection can run without token/cost budget, ledger logging, or source row caps.

8. Graph treated as source of truth
   - Any feature writes canonical facts only to graph or uses graph output to overwrite Postgres truth.

9. Expertise page scope creep
   - Phase 10 starts building durable person expertise pages or public/member surfaces.

10. Missing source documents remain unresolved at ratification time
   - `PHASE4_PLAN.md`, `PHASE6_PLAN_DRAFT.md`, or `PHASE8_PLAN_DRAFT.md` are still required but absent.

---

## §9. PR Workflow

Phase 10 is not authorized yet. This workflow applies only after explicit authorization.

Rules:

- one ticket per PR
- feature flags default off
- no direct LLM provider calls
- no graph access outside graph adapter/query API
- no member-facing graph surface
- no public surface
- no expertise pages
- every PR lists changed files, tests run, and risks
- every PR demonstrates invariant 3, 6, and 9 compliance

Suggested PR order:

1. T10-01 decision doc PR.
2. T10-02 source eligibility contract PR.
3. T10-03 side-table migration/repo PR.
4. T10-04 `llm_gateway.extract_graph_triples` contract PR.
5. T10-05 `graph_projector.py` dry-run PR.
6. T10-06 cascade graph purge PR.
7. T10-07 `graph_query.py` read-only API PR.
8. T10-08 admin handlers PR.
9. T10-09 drift/rebuild checks PR.

Required PR checks:

- governance filter tests
- offrecord exclusion tests
- nomem exclusion tests
- tombstone cascade tests
- provenance required tests
- dry-run cost estimate tests
- graph rebuild idempotency tests
- query refusal tests

Rollback:

- turn off graph feature flag
- disable admin graph handlers
- stop graph projection jobs
- drop/rebuild graph store if needed
- keep PostgreSQL source rows and tombstones intact

---

## §10. Glossary

### Graph projection

A derived graph representation generated from governed PostgreSQL source rows. It is rebuildable
and never canonical.

### Graph store

The graph query/runtime backend selected for Phase 10, such as Neo4j, Graphiti, Apache AGE, or
NetworkX. The choice is open and requires ratification.

### Triple

A typed relationship extracted from source evidence, usually shaped as subject, predicate, object,
plus provenance and confidence metadata.

### Graph provenance

The mapping from graph nodes/edges back to PostgreSQL source rows and projection runs. Required
for rebuild, drift detection, and forget cascade.

### Projection run

A tracked execution of graph projection in dry-run, incremental, full rebuild, or repair mode.

### Graph drift

A state where graph nodes/edges or query results no longer match governed PostgreSQL source rows.
PostgreSQL wins; graph must be repaired or rebuilt.

### Cascade graph layer

The extension of tombstone/forget propagation that removes graph projections derived from forgotten
sources.

### Butler-style query

A future governed query style for relationship questions. It may use graph traversal as one tool,
but it must receive governance-filtered evidence context and must not read raw DB or graph as truth.

### Store ratification

The explicit architecture decision choosing graph backend and hosting model before implementation.

---

## Final report

DRAFT_PATH: /tmp/PHASE10_PLAN_DRAFT.md  
COMPONENTS: 5  
TICKETS: T10-01..T10-09  
INVARIANT_6_BINDING: yes  
INVARIANT_9_BINDING: yes (cascade includes graph layer)  
COST_GUARDRAIL: yes  
GRAPH_STORE_DECISION: open — flagged for ratification  
OPEN_DESIGN_QUESTIONS:

1. Graph store choice: Neo4j vs Graphiti vs Apache AGE (in-Postgres) vs in-memory NetworkX.
2. Hosting model: same Docker Compose as bot, or separate service.
3. Triple extraction prompt design: what LLM prompt extracts typed triples from messages/cards/observations.
4. Update cadence: real-time via hooks vs scheduled batch projection.
5. Privacy model: how offrecord exclusion propagates into graph — who can query what.
6. Exact Phase 8 graph source contract: cards + observations are required by task, but
   `PHASE8_PLAN_DRAFT.md` was not available for verification.
7. Cascade timing: synchronous graph purge in forget transaction vs async cascade worker with
   read block until purge completes.
8. Shared graph node semantics: when one node is supported by multiple sources, what gets deleted
   vs detached when one source is forgotten.
