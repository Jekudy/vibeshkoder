# Phase 10 Plan — Graph Projection / Neo4j

**Status:** RATIFIED 2026-05-16. Authorized for implementation.
**Predecessors:** Phase 4 (FTS + evidence, CLOSED 2026-04-30), Phase 5 (`llm_gateway` + ledger, CLOSED 2026-05-11), Phase 6 (cards + admin review, CLOSED 2026-05-12), Phase 7 (daily digest, CLOSED 2026-05-15), Phase 8 (weekly digest, CLOSED 2026-05-15), Phase 11 (privacy binding suite, ACTIVE 42/42).
**Owner:** Orchestrator B.
**Charter:** governance_mode = critical (Neo4j is new infrastructure; public wiki leak adjacency risk per HANDOFF §16). git_workflow_mode = sprint_pr_queue. Per-PR PAR (dual-model: Claude product + Codex technical). FHR mandatory after T10-09.
**Supersedes:** `docs/memory-system/prompts/PHASE10_PLAN_DRAFT.md` (open questions resolved at ratification 2026-05-16).

---

## §0. Implementation Status: AUTHORIZED

Phase 10 is authorized for implementation following Phase 8 closure 2026-05-15. All runtime
dependencies (Phase 5 `llm_gateway`/ledger, Phase 6 `knowledge_cards`/`card_sources`, Phase 8
closed, Phase 11 binding suite) are satisfied. Sprint 0 must update `AUTHORIZED_SCOPE.md`
(replace the "conditionally authorized" bullet for Phase 10 with an "Authorized: Phase 10"
block) **before any code lands**.

### Banner — derived-only emphasis

**The graph is a read-only derived projection. It is never canonical. PostgreSQL is the only
source of truth. The graph is droppable and rebuildable at any time from PostgreSQL source rows.**
Invariant #6 is the central architectural constraint for every Phase 10 decision. Any feature that
treats graph output as authoritative, or writes canonical facts to the graph only, violates this
invariant and is a stop-signal condition.

### Ratification decisions encoded

All six previously open design questions are resolved (see §13 for full rationale). Key decisions:

| Decision | Choice |
|---|---|
| Q1 Graph store | **Neo4j 5.x Community Edition** (RFC-001 §6: traversal P50 2.554ms vs AGE 8,957ms — 3,500× faster; flip confirmed 2026-05-11) |
| Q2 Hosting | `neo4j` service in `docker-compose.yml` — dev profile only; bolt 7687, HTTP 7474, volume `neo4j_data` |
| Q3 Prompt design | Typed JSON schema, source-id preservation mandatory, temperature 0.1, max 5 triples per source row, refuse-on-UNKNOWN, refuse if no canonical entity id |
| Q4 Cadence | **Nightly batch projection** at 03:30 MSK; real-time hooks deferred to Phase 10.5 |
| Q5/Q7 Privacy | **Async purge worker** (RFC-001:415 compliant). Forget event atomically enqueues `graph_purge_pending` rows in the Postgres transaction (same commit as Postgres-side cascade). Separate `graph_purge_worker` (extension of `cascade_worker_tick`) drives Neo4j bolt DELETE. `graph_query.py` fails-closed (`abstained=True`) while any non-purged `graph_purge_pending` row touches query result nodes. |
| Q6 Sources | `knowledge_cards` (approved only) + `message_versions` (governance-filtered); observations deferred (never shipped as standalone table per CLAUDE.md); digests NOT projection sources |
| Q8 Shared node | Node deleted iff ALL provenance rows purged; otherwise source edge detached only |

### Component existence table

| Component | Status | Notes |
|---|---:|---|
| `llm_gateway.synthesize_digest` + `extract_candidates` | Exists | `bot/services/llm_gateway.py:1058,1626`. Phase 10 adds `extract_graph_triples` as a peer method following the `extract_candidates` skeleton. |
| `LedgerRepo.record` / `update_placeholder` | Exists | `bot/db/repos/llm_usage_ledger.py:31,:121`. Graph triple extraction reuses placeholder → update pattern. |
| `forget_cascade.CASCADE_LAYER_ORDER` | Exists | `bot/services/forget_cascade.py:133-161`. Phase 10 inserts `graph_nodes` layer AFTER `digests` and AFTER `card_sources`. The `_cascade_graph_provenance` function (not FK CASCADE) is the primary mechanism; it queries by `source_table`/`source_pk` and enqueues `graph_purge_pending` rows. |
| `_process_one_event` layer dispatch | Exists | `bot/services/forget_cascade.py:878`. Phase 10 adds `_cascade_graph_provenance` function to `_LAYER_FUNCS`. |
| `KnowledgeCard` / `CardSource` | Exists | `bot/db/models.py:1085,:1159`. `card_status='approved'` filter at `bot/db/repos/knowledge_card.py:60`. |
| Admin identity check | Exists | `_is_admin(message)` at `bot/handlers/admin_cards.py:58-61`. |
| `AsyncIOScheduler` UTC | Exists | `bot/services/scheduler.py:35`. Phase 10 adds graph projection cron at 03:30 MSK. |
| `feature_flags` | Exists | Phase 10 adds two flags: writer `memory.graph.projection.enabled` (OFF) and reader `memory.graph.query.enabled` (OFF). |
| `tests/evals/` Phase 11 suite | Exists | 42 cases across 4 test files. Phase 10 adds 6 new test files (L10/C9/I8/R7/G2) → **57/57** new total (I8e + R7.d + G2 sub-case added per Codex audit). |
| Migration counter | 038 on main | Phase 10 starts from **060** per ORCHESTRATOR_REGISTRY.md §2 Orch B reservation. Phase 9 consumes 050-059. |
| `graph_projection_runs` / `graph_provenance` / `graph_edges` / `graph_purge_pending` | DOES NOT EXIST | New Phase 10 tables (migrations 060-063). Migration 064 adds `llm_usage_ledger.call_type`. |
| Neo4j service | DOES NOT EXIST | New Docker service; dev profile only until prod rollout. |

---

## §1. Non-Negotiable Invariants (verbatim from HANDOFF §1)

1. Existing gatekeeper must not break.
2. **No LLM calls outside `llm_gateway`.** Triple extraction is a new gateway method (`extract_graph_triples`). No direct provider imports in `bot/services/graph_projector.py`, `bot/services/graph_query.py`, `bot/services/neo4j_adapter.py`, or `bot/handlers/admin_graph.py`.
3. **No extraction / search / q&a / graph projection over `#nomem` / `#offrecord` / forgotten.** Governance pre-filter is mandatory before any source row reaches triple extraction or graph MERGE. Fail closed if filter is unavailable.
4. **Citations point to `message_version_id` or approved card sources.** Every graph node and edge must carry a non-NULL provenance row with `source_message_version_id` OR `source_card_id` traceable to PostgreSQL.
5. **Summary is never canonical truth.** (N/A for Phase 10 graph nodes/edges — same principle: graph is derived, not canonical.)
6. **Graph is never source of truth.** CENTRAL invariant for Phase 10. PostgreSQL wins on any divergence. The graph must be droppable and rebuildable without data loss.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context. (N/A for Phase 10 directly — but graph_query.py must enforce this boundary now so the butler can consume it later.)
8. Import apply must go through the same normalization / governance path. (N/A for Phase 10 graph layer itself.)
9. **Tombstones are durable and not casually rolled back.** Cascade forget MUST include graph purge. A forgotten source message or card must: (a) soft-delete `graph_provenance`/`graph_edges` rows and enqueue `graph_purge_pending` atomically in the Postgres cascade transaction; (b) complete Neo4j bolt DELETE via `graph_purge_worker` within seconds. Graph queries MUST fail-closed (return `abstained=True`) for any node with a pending non-purged purge row — ensuring no forgotten content is ever returned during the async window.
10. Public wiki remains disabled. Phase 10 graph is admin-only; no public exposure.

### Invariant bindings central to Phase 10

- **#6 CENTRAL:** the graph can be rebuilt from PostgreSQL at any time. `project_full_rebuild()` is a first-class operation. No canonical data lives only in Neo4j.
- **#2 binding:** `extract_graph_triples` in `llm_gateway` is the single LLM boundary. No graph code calls any provider SDK directly.
- **#3 binding:** governance pre-filter in `graph_projector.py` runs before any triple extraction pass. Source rows that fail `memory_policy='normal'`, `is_redacted=FALSE`, or have an active `forget_events` row are skipped and counted as `skipped_policy_count`.
- **#9 binding:** `graph_nodes` layer in `CASCADE_LAYER_ORDER` is mandatory. If cascade cannot enqueue `graph_purge_pending` rows (Postgres write fails), the cascade layer fails. If `graph_purge_worker` bolt calls fail, `graph_query.py` fails-closed automatically via pending-purge read-block until worker recovers.

---

## §2. Objective

Project governed Shkoderbot memory into a typed graph so the system can answer butler-style
relationship questions:

- Who is connected to topic X?
- Which decisions relate to project Y?
- How do concepts A and B connect through reviewed community memory?
- Which people, cards, and messages support a claim?

The graph is a **read-only derived view**. It powers traversal and ranking for future butler context,
not truth storage. Phase 10 ships the projection engine, cascade integration, and admin-only query
API. No member-facing surfaces. No public output. No expertise pages.

**Ontology (HIGH E):** The graph stores semantic concept relationships derived from approved
`knowledge_cards` only (concept nodes + typed edges via LLM triple extraction).
`message_versions` appear ONLY as provenance event nodes (`MessageEvent`) for traceability
— they are NOT extracted into semantic triples themselves. This avoids double-counting since
cards are already derived from messages.

---

## §3. Authorized Scope

Phase 10 is authorized to create and modify the following — nothing else:

**New PostgreSQL tables (migrations 060-064):**
- `graph_projection_runs` — projection run audit with mode, status, counts, cost (migration 060)
- `graph_provenance` — source-to-graph mapping; logical refs to source tables; cascade purge lookup (migration 061). Note: `source_table`/`source_pk` are NOT typed FK columns — they are logical application-code refs queried by `_cascade_graph_provenance`. FK ON DELETE CASCADE on `source_card_id`/`source_message_version_id` is a safety net only.
- `graph_edges` — typed edge registry in Postgres for idempotency and drift detection (migration 062)
- `graph_purge_pending` — async purge queue for Neo4j bolt DELETE (keyed by `forget_event_id + source_table + source_pk`); worker marks `purged_at` on success or `failed_at + error` on failure (migration 063)
- `add_llm_ledger_call_type` — `ALTER TABLE llm_usage_ledger ADD COLUMN call_type VARCHAR(32) NOT NULL DEFAULT 'unknown'`; backfill existing rows to `'qa_synthesis'` or `'digest'`; `graph_projection` bucket for new graph extraction calls (migration 064)

**New Python services:**
- `bot/services/graph_projector.py` — dry_run / incremental / full_rebuild / repair modes
- `bot/services/neo4j_adapter.py` — bolt connection, MERGE Cypher templates, schema constraints
- `bot/services/graph_query.py` — read-only traversal API, role filters, provenance-required output

**Extensions to existing services:**
- `bot/services/llm_gateway.py` — new `extract_graph_triples` method (peer to `extract_candidates`)
- `bot/services/forget_cascade.py` — new `graph_nodes` cascade layer added to `CASCADE_LAYER_ORDER` after `card_sources`
- `bot/services/scheduler.py` — new `graph_projection_job` cron at 03:30 MSK

**New Telegram handlers:**
- `bot/handlers/admin_graph.py` — `/graph_project_now`, `/graph_stats`, `/graph_query` (admin-only, flag-gated)

**New config in `bot/config.py`:**
- `NEO4J_BOLT_URI`, `NEO4J_AUTH_USER`, `NEO4J_AUTH_PASSWORD` (min-32-char enforced in prod via `NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}` — see §12), `NEO4J_DATABASE`
- `GRAPH_PROJECTION_ENABLED` — gates scheduler registration
- `GRAPH_PROJECTION_HOUR_MSK` (default 3), `GRAPH_PROJECTION_MINUTE_MSK` (default 30)
- `GRAPH_PROJECTION_MAX_SOURCES_PER_RUN` (default 200) — graph-specific cap, lower than shared 1000 to contain first-run cost
- `GRAPH_PROJECTION_MAX_TOKENS_PER_SOURCE` (default 2000) — per-source input truncation before LLM dispatch
- `GRAPH_PROJECTION_MAX_TRIPLES_PER_SOURCE` (default 5)
- `GRAPH_PROJECTION_DAILY_USD_CEILING` (default `Decimal("2.00")`) — separate graph-only daily bucket; does NOT share with QA/digest `LLM_DAILY_USD_CEILING` ($5/day). Enforced via `llm_usage_ledger` SUM filtered by `call_type='graph_projection'`.
- `GRAPH_PROJECTION_RUN_USD_CEILING` (default `Decimal("0.50")`) — per-run abort before any provider call. `graph_projector` computes dry-run cost estimate first; aborts if estimate exceeds this value.
- `GRAPH_PROJECTION_MONTHLY_USD_CEILING` (default 20.00)

**Feature flags (3):**
- `memory.graph.projection.enabled` — writer flag, default OFF
- `memory.graph.query.enabled` — reader flag, default OFF (split so projector can be staged before query API opens)
- `memory.graph.write_pending.paused` — kill-switch for `graph_purge_worker`, default OFF. When ON, purge worker stops consuming `graph_purge_pending` rows; `graph_query.py` immediately fails-closed on any pending-purge row. Use if Neo4j becomes unreliable in production.

**Docker Compose:**
- `neo4j` service added under `profiles: [graph]`. Dev activation: `docker compose --profile graph up`. Prod compose deferred to Phase 10 rollout.

**New test files:**
- `tests/evals/test_graph_leakage.py` (L10a/b/c)
- `tests/evals/test_graph_citations.py` (C9)
- `tests/evals/test_graph_cascade.py` (I8a/b/c/d)
- `tests/evals/test_graph_refusal.py` (R7.a/b/c)
- `tests/evals/test_graph_drift.py` (G2)
- `tests/services/test_graph_projector.py`
- `tests/services/test_neo4j_adapter.py`
- `tests/services/test_graph_query.py`
- `tests/services/test_extract_graph_triples.py`
- `tests/db/test_graph_schema.py`

---

## §4. Non-Goals (Phase 10 MUST NOT implement)

- **No real-time graph update hooks.** Graph is updated only by the nightly batch projection job. Real-time hooks deferred to Phase 10.5.
- **No member graph.** No member-facing `/graph_*` commands. No person expertise pages. No public "who knows X" surface.
- **No public output.** Graph traversal results never leave admin-only handlers.
- **No knowledge card page from graph.** Graph may reference cards but does not generate new card pages.
- **No rebuild from graph.** Postgres is source of truth. Graph is never used to reconstruct Postgres rows.
- **No observation table.** Observations were scoped out of Phase 8; they never shipped as a standalone table. Phase 10 does NOT project a phantom `observations` table.
- **No multi-admin quorum for projection approval.** Projection is admin-initiated or cron-triggered; no approval workflow.
- **No butler action layer.** `graph_query.py` is a read-only evidence context API. No butler execution code ships in Phase 10.
- **No Phase 9 wiki content.** Phase 10 graph does not power wiki page rendering (Phase 9 scope).
- **No Phase 11 eval runner or dashboard.** Phase 11 binding tests are written in T10-09 but the eval runner and nightly wiring already exist.
- **Feature flag ≠ operational readiness.** `memory.graph.projection.enabled = OFF` is a rollout control switch, NOT a substitute for Neo4j operational readiness. The production Neo4j checklist (§12) MUST be satisfied before flipping the flag ON. Do not conflate "flag is OFF so we're safe" with "Neo4j is not yet production-ready".

---

## §5. Detailed Designs

### §5.A. Schema — PostgreSQL side-tables

**Migration 060: `graph_projection_runs`**

```sql
CREATE TABLE graph_projection_runs (
    id              BIGSERIAL PRIMARY KEY,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,
    source_cutoff_at            TIMESTAMPTZ,
    source_card_count           INTEGER NOT NULL DEFAULT 0,
    source_message_version_count INTEGER NOT NULL DEFAULT 0,
    projected_node_count        INTEGER NOT NULL DEFAULT 0,
    projected_edge_count        INTEGER NOT NULL DEFAULT 0,
    skipped_policy_count        INTEGER NOT NULL DEFAULT 0,
    skipped_budget_count        INTEGER NOT NULL DEFAULT 0,
    llm_prompt_tokens           INTEGER NOT NULL DEFAULT 0,
    llm_completion_tokens       INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd          NUMERIC(10,6) NOT NULL DEFAULT 0,
    actual_cost_usd             NUMERIC(10,6) NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    error_code      TEXT,
    error_context   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_graph_projection_runs_mode CHECK (
        mode IN ('dry_run', 'incremental', 'full_rebuild', 'repair')
    ),
    CONSTRAINT ck_graph_projection_runs_status CHECK (
        status IN ('running', 'completed', 'failed', 'cancelled', 'cost_exceeded', 'dry_run_complete')
    )
);

CREATE INDEX ix_graph_projection_runs_started_at
    ON graph_projection_runs(started_at DESC);
CREATE INDEX ix_graph_projection_runs_status
    ON graph_projection_runs(status)
    WHERE status IN ('running', 'failed');
```

**Migration 061: `graph_provenance`**

```sql
CREATE TABLE graph_provenance (
    id                          BIGSERIAL PRIMARY KEY,
    projection_run_id           BIGINT NOT NULL REFERENCES graph_projection_runs(id) ON DELETE CASCADE,
    source_table                TEXT NOT NULL,
    source_pk                   TEXT NOT NULL,
    source_message_version_id   BIGINT REFERENCES message_versions(id) ON DELETE CASCADE,
    source_card_id              UUID REFERENCES knowledge_cards(id) ON DELETE CASCADE,
    source_content_hash         TEXT,
    graph_store                 TEXT NOT NULL DEFAULT 'neo4j',
    graph_node_key              TEXT,
    graph_edge_key              TEXT,
    triple_hash                 TEXT,
    governance_policy           TEXT NOT NULL DEFAULT 'normal',
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_at                   TIMESTAMPTZ,
    purge_reason                TEXT,
    CONSTRAINT ck_graph_provenance_source_table CHECK (
        source_table IN ('message_versions', 'knowledge_cards')
    ),
    CONSTRAINT ck_graph_provenance_has_source CHECK (
        source_message_version_id IS NOT NULL OR source_card_id IS NOT NULL
    ),
    CONSTRAINT ck_graph_provenance_graph_store CHECK (
        graph_store IN ('neo4j', 'networkx_dev')
    )
);

-- Cascade forget lookup: find all graph provenance rows for a given message_version
CREATE INDEX ix_graph_provenance_mvid
    ON graph_provenance(source_message_version_id)
    WHERE source_message_version_id IS NOT NULL;

-- Cascade forget lookup: find all graph provenance rows for a given card
CREATE INDEX ix_graph_provenance_card_id
    ON graph_provenance(source_card_id)
    WHERE source_card_id IS NOT NULL;

-- Drift detection: active (non-purged) provenance rows
CREATE INDEX ix_graph_provenance_active
    ON graph_provenance(projection_run_id)
    WHERE purged_at IS NULL;

-- Idempotency: stable triple key within a projection run
CREATE UNIQUE INDEX uq_graph_provenance_triple
    ON graph_provenance(source_table, source_pk, triple_hash)
    WHERE purged_at IS NULL;
```

**Migration 062: `graph_edges`**

```sql
-- Postgres-side edge registry for idempotency, drift detection,
-- and cascade lookup. Neo4j holds the traversable graph; this table
-- proves every Neo4j edge has a Postgres-side provenance record.
CREATE TABLE graph_edges (
    id                  BIGSERIAL PRIMARY KEY,
    graph_provenance_id BIGINT NOT NULL REFERENCES graph_provenance(id) ON DELETE CASCADE,
    subject_node_key    TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object_node_key     TEXT NOT NULL,
    edge_key            TEXT NOT NULL,  -- stable MERGE key: SHA-256(subject+predicate+object)
    confidence_score    NUMERIC(3,2) NOT NULL DEFAULT 0.50,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_at           TIMESTAMPTZ,
    CONSTRAINT ck_graph_edges_predicate CHECK (
        predicate IN (
            'MENTIONS', 'AUTHORED', 'KNOWS_ABOUT', 'ASKED', 'ANSWERED',
            'DECIDED', 'RELATED_TO', 'SUPPORTS', 'DERIVED_FROM',
            'PART_OF', 'CONTRADICTS', 'SUPERSEDES'
        )
    ),
    CONSTRAINT ck_graph_edges_confidence CHECK (
        confidence_score >= 0.00 AND confidence_score <= 1.00
    )
);

-- Drift detection: Neo4j edge count vs graph_edges count must match
CREATE INDEX ix_graph_edges_active
    ON graph_edges(graph_provenance_id)
    WHERE purged_at IS NULL;

-- Idempotent MERGE lookup: is this edge already projected?
CREATE UNIQUE INDEX uq_graph_edges_key
    ON graph_edges(edge_key)
    WHERE purged_at IS NULL;
```

**Migration 063: `graph_purge_pending`** (BLOCKER A — async cascade queue)

```sql
-- Async purge queue for Neo4j bolt DELETE. Written atomically in the same
-- Postgres transaction as the Postgres-side cascade (forget_event commit).
-- graph_purge_worker consumes rows, marks purged_at on success or failed_at+error on failure.
-- graph_query.py checks this table before any Neo4j traversal: if any non-purged
-- row exists for nodes in the result set → return abstained=True (fail-closed).
CREATE TABLE graph_purge_pending (
    id                  BIGSERIAL PRIMARY KEY,
    forget_event_id     BIGINT NOT NULL,
    source_table        TEXT NOT NULL,
    source_pk           TEXT NOT NULL,
    graph_node_key      TEXT,           -- known at enqueue time if provenance row exists
    graph_edge_key      TEXT,           -- known at enqueue time
    graph_provenance_id BIGINT REFERENCES graph_provenance(id) ON DELETE SET NULL,
    enqueued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_at           TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    error               TEXT,
    retry_count         SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_graph_purge_pending_source_table CHECK (
        source_table IN ('message_versions', 'knowledge_cards', 'card_sources')
    ),
    CONSTRAINT uq_graph_purge_pending_event_source UNIQUE (forget_event_id, source_table, source_pk)
);

-- Worker queue: fetch pending (non-purged, non-failed) rows ordered by enqueue time
CREATE INDEX ix_graph_purge_pending_queue
    ON graph_purge_pending(enqueued_at)
    WHERE purged_at IS NULL AND failed_at IS NULL;

-- Fail-closed check in graph_query.py: are there pending purge rows?
CREATE INDEX ix_graph_purge_pending_node_key
    ON graph_purge_pending(graph_node_key)
    WHERE purged_at IS NULL;
```

**Async purge worker behaviour (`graph_purge_worker` in `bot/services/graph_projector.py`):**
- Extension of existing `cascade_worker_tick` pattern.
- Fetches `graph_purge_pending` rows where `purged_at IS NULL AND failed_at IS NULL` (up to 50/tick).
- For each row: executes Neo4j bolt DELETE via `neo4j_adapter.purge_provenance(graph_node_key, graph_provenance_id)`.
- On success: sets `purged_at = now()`.
- On bolt timeout / failure: increments `retry_count`, sets `failed_at` after 5 retries. DLQ: rows with `failed_at IS NOT NULL` trigger structured alert log (`level=ERROR, graph_purge_dlq=True`).
- On Neo4j down: `graph_purge_pending` rows accumulate, `graph_query.py` fails-closed automatically (pending rows exist for affected nodes).
- On network partition: no split-brain — Postgres-side tombstone is authoritative; graph re-converges when worker resumes.

**`graph_query.py` read-block contract (BLOCKER A):**
Before any Neo4j traversal, check:
```sql
SELECT 1 FROM graph_purge_pending
WHERE graph_node_key = ANY(:candidate_node_keys)
  AND purged_at IS NULL
LIMIT 1
```
If any row found → return `abstained=True` without executing Cypher. This implements RFC-001:415 strict fail-closed pattern.

**Migration 064: `add_llm_ledger_call_type`** (HIGH D)

```sql
ALTER TABLE llm_usage_ledger
    ADD COLUMN call_type VARCHAR(32) NOT NULL DEFAULT 'unknown';

-- Backfill existing rows: qa_synthesis for qa_trace-linked rows, digest for digest-linked rows
UPDATE llm_usage_ledger SET call_type = 'qa_synthesis'
    WHERE qa_trace_id IS NOT NULL AND call_type = 'unknown';
-- (digest-linked rows identified by pattern on trace_id prefix — implementation detail)
-- All remaining 'unknown' rows retain 'unknown' for audit trail integrity.

CREATE INDEX ix_llm_usage_ledger_call_type_created_at
    ON llm_usage_ledger(call_type, created_at);
```

**Daily ceiling SQL example** (graph-only bucket, used by `graph_projector._check_daily_graph_budget`):
```sql
SELECT COALESCE(SUM(cost_usd), 0)
FROM llm_usage_ledger
WHERE call_type = 'graph_projection'
  AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC');
```

**Rollback:** drops `graph_purge_pending` → `graph_edges` → `graph_provenance` → `graph_projection_runs` (FK order). Migration 064 rollback: `ALTER TABLE llm_usage_ledger DROP COLUMN call_type`. No touch on `chat_messages`, `message_versions`, `knowledge_cards`, `card_sources`, `llm_usage_ledger` (rows), `digests`.

**Neo4j schema constraints (applied via adapter on startup):**

```cypher
-- Unique node constraint: each Postgres-side source maps to exactly one Neo4j node
CREATE CONSTRAINT node_key_unique IF NOT EXISTS
    FOR (n:MemoryNode) REQUIRE n.node_key IS UNIQUE;

-- Provenance required: every node must carry its Postgres provenance id
CREATE CONSTRAINT node_provenance_id_exists IF NOT EXISTS
    FOR (n:MemoryNode) REQUIRE n.provenance_ids IS NOT NULL;

-- Edge key uniqueness
CREATE CONSTRAINT edge_key_unique IF NOT EXISTS
    FOR ()-[r:GRAPH_EDGE]-() REQUIRE r.edge_key IS UNIQUE;

-- Performance indexes for traversal
CREATE INDEX node_label_idx IF NOT EXISTS FOR (n:MemoryNode) ON (n.label);
CREATE INDEX node_topic_idx IF NOT EXISTS FOR (n:MemoryNode) ON (n.topic_tags);
```

---

### §5.B. `llm_gateway.extract_graph_triples` contract

**Public API (added to `bot/services/llm_gateway.py`, peer to `extract_candidates` at :1058):**

```python
@dataclass(frozen=True)
class GraphTriple:
    subject_label: str     # canonical entity label (e.g. "Вася К.", "проект X")
    subject_type: str      # one of ALLOWED_NODE_TYPES
    predicate: str         # one of ALLOWED_PREDICATES
    object_label: str
    object_type: str       # one of ALLOWED_NODE_TYPES
    confidence: float      # 0.0-1.0
    source_id: str         # verbatim from prompt input (message_version_id or card_source_id)

@dataclass(frozen=True)
class ExtractGraphTriplesResult:
    triples: list[GraphTriple]
    llm_usage_ledger_id: int | None
    cost_usd: Decimal
    skipped_unknown: int   # triples the model emitted with UNKNOWN subject or object

async def extract_graph_triples(
    session: AsyncSession,
    *,
    source_table: Literal['message_versions', 'knowledge_cards'],
    source_pk: str,
    source_text: str,              # governance-filtered body text, NO forbidden content
    source_id: str,                # message_version_id (int as str) or card_source_id (UUID str)
    source_mv_id: int | None,      # message_version_id int for entity resolution; None for cards
    prompt_version: str,           # e.g. 'graph_triples_v0_1_0' — stored on ledger row
    run_id: int,                   # graph_projection_runs.id — stored on ledger row
    governance_policy: str,        # must be 'normal' — caller verified
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    max_triples: int = 5,
) -> ExtractGraphTriplesResult: ...
```

**Behaviour:**

1. **Pre-call governance assertion.** If `governance_policy != 'normal'` → raise `GraphProjectionPolicyError`. Fail closed — never extract over non-normal content.
2. Call `_budget_check(session, config, ledger_repo)` — shared gateway budget guard.
3. Additional per-run budget check from caller: `graph_projector` verifies `GRAPH_PROJECTION_RUN_USD_CEILING` before dispatching the gateway call (dry-run cost estimate computed BEFORE any provider call).
4. `LedgerRepo.record` placeholder with `qa_trace_id=None`, `call_type='graph_projection'` (migration 064 column).
5. Build prompt with typed JSON schema (see below). `temperature=0.1`. Max output tokens = 512.
6. Parse structured JSON response. For each triple: validate `predicate` in `ALLOWED_PREDICATES`, validate `subject_type` and `object_type` in `ALLOWED_NODE_TYPES`. Refuse if `subject_label == "UNKNOWN"` or `object_label == "UNKNOWN"` (model signals it cannot identify canonical entities) — count as `skipped_unknown`, do not add to result.
7. **Entity registry resolution** (applied post-parse, before returning): for each accepted triple's subject/object label, resolve canonical entity_id via priority order:
   (a) `knowledge_cards.id` — if card-source row triggered extraction and label matches card title.
   (b) `users.id` (string-formatted as `"user:<telegram_id>"`) — for person-type entities matched by display_name or username.
   (c) `UNKNOWN_{md5(label)[:8]}` placeholder — for unresolvable mentions.
   If ANY entity resolves to `UNKNOWN_*` placeholder → triple is DROPPED (refuse-on-UNKNOWN rule). Count as `skipped_unknown`. This prevents dangling node_key values in Neo4j.
8. Enforce `max_triples` cap — take first N after validation and entity resolution.
9. `LedgerRepo.update_placeholder` with actuals.
10. Return `ExtractGraphTriplesResult`.

**Prompt template (`bot/services/llm_prompts/graph_triples_v0_1_0.py`):**

```
SYSTEM:
You are extracting typed relationship triples from a single piece of community memory.

Output ONLY a JSON array. Each element:
{
  "subject_label": "<canonical entity name in Russian, verbatim>",
  "subject_type": "<one of: Person, Topic, Project, Decision, Question, Answer, Event, KnowledgeCard, Source>",
  "predicate": "<one of: MENTIONS, AUTHORED, KNOWS_ABOUT, ASKED, ANSWERED, DECIDED, RELATED_TO, SUPPORTS, DERIVED_FROM, PART_OF, CONTRADICTS, SUPERSEDES>",
  "object_label": "<canonical entity name in Russian, verbatim>",
  "object_type": "<one of the same types>",
  "confidence": <float 0.0-1.0>,
  "source_id": "<verbatim source_id from input>"
}

Rules:
- Extract ONLY claims explicitly stated in the input. Do not infer.
- If you cannot identify a canonical entity name, use "UNKNOWN" as the label.
- Preserve source_id verbatim.
- Maximum {max_triples} triples.
- If no triples can be extracted, return: []

USER:
source_id: {source_id}
source_table: {source_table}

Text:
{source_text}
```

**Refusal modes:**
- `subject_label == "UNKNOWN"` or `object_label == "UNKNOWN"` → triple dropped, counted in `skipped_unknown`.
- Malformed JSON → `ExtractGraphTriplesError`, caller marks source as `skipped_budget_count` (reusable counter for non-budget skips in future).
- Empty array `[]` → `ExtractGraphTriplesResult(triples=[], ...)` — valid, means no extractable relations.
- Provider error → propagate as `LLMProviderError`; `graph_projector` marks source as failed in run stats.

---

### §5.C. `graph_projector.py` service

**File:** `bot/services/graph_projector.py`

**Public API:**

```python
async def dry_run(
    session: AsyncSession,
    *,
    limit: int = 20,
    source_types: list[str] | None = None,  # ['message_versions', 'knowledge_cards']
) -> GraphProjectionRunResult: ...

async def project_incremental(
    session: AsyncSession,
    *,
    since_run_id: int | None = None,
    since_timestamp: datetime | None = None,
    config: GraphProjectorConfig,
) -> GraphProjectionRunResult: ...

async def project_full_rebuild(
    session: AsyncSession,
    *,
    config: GraphProjectorConfig,
) -> GraphProjectionRunResult: ...

async def repair_source(
    session: AsyncSession,
    *,
    source_table: str,
    source_pk: str,
    config: GraphProjectorConfig,
) -> GraphProjectionRunResult: ...

async def reconcile_counts(
    session: AsyncSession,
    *,
    neo4j_adapter: Neo4jAdapter,
) -> GraphDriftReport: ...
```

**Governance pre-filter (mandatory, runs before any LLM dispatch):**

```sql
-- For message_versions source:
SELECT mv.id, mv.content_text, mv.version_seq
FROM message_versions mv
JOIN chat_messages cm ON cm.id = mv.chat_message_id
LEFT JOIN forget_events fe ON (
    fe.target_type = 'message' AND fe.target_id = cm.id::TEXT
)
WHERE cm.memory_policy = 'normal'
  AND mv.is_redacted = FALSE
  AND fe.id IS NULL
  AND mv.id > :since_mvid  -- for incremental mode
ORDER BY mv.id
LIMIT :batch_size;

-- For knowledge_cards source:
SELECT kc.id, kc.title, kc.body_markdown, kc.source_message_version_ids
FROM knowledge_cards kc
WHERE kc.card_status = 'approved'
  AND NOT EXISTS (
      SELECT 1 FROM forget_events fe
      WHERE fe.target_type = 'message'
        AND fe.target_id IN (
            SELECT jsonb_array_elements_text(kc.source_message_version_ids)
        )
  )
LIMIT :batch_size;
```

**Ontology split — source contract (HIGH E):**

- **`knowledge_cards` (approved only)** → **semantic CONCEPT nodes + edges** via LLM triple extraction. Cards are the primary semantic projection source. They have already been curated from message_versions; projecting both would double-count.
  Example: `(:Person {entity_id})-[:DISCUSSED_IN]->(:Topic {entity_id})`
- **`message_versions` (governance-filtered)** → **provenance/event nodes ONLY** (no LLM triple extraction). Each message_version that is a source of an approved card gets a `(:MessageEvent {mv_id, chat_id, ts})-[:DERIVED]->(:Concept)` provenance edge — no semantic triple extraction via LLM. This avoids the double-counting problem (cards are already derived from messages) and eliminates the need for LLM calls on raw message text.

This ontology split is encoded in `graph_projector.py` source selection logic:
```python
# cards → LLM triple extraction (semantic concept layer)
for card in governance_filtered_cards:
    result = await extract_graph_triples(source_table='knowledge_cards', ...)

# message_versions → bulk provenance event node insertion (no LLM call)
for mv in card_source_message_versions:
    await neo4j_adapter.merge_event_node(mv_id=mv.id, chat_id=mv.chat_id, ts=mv.created_at)
    await neo4j_adapter.merge_derived_edge(mv_id=mv.id, concept_node_key=...)
```

**Projection modes:**

- `dry_run`: scan source rows with governance filter, estimate token/cost (cards only, since message_versions have no LLM cost), write `graph_projection_runs(mode='dry_run', status='dry_run_complete')`, no Neo4j writes, no `graph_provenance` rows.
- `incremental`: project source rows newer than the last completed full_rebuild or incremental run. Uses `source_cutoff_at` from last completed run. Writes provenance + Neo4j MERGE.
- `full_rebuild`: drops all active `graph_provenance` rows and all Neo4j graph nodes/edges, then re-projects from scratch. Creates new `graph_projection_runs(mode='full_rebuild')` row. Idempotent MERGE keys ensure Neo4j remains consistent.
- `repair`: re-projects a single `(source_table, source_pk)` pair. Used by `/graph_project_now repair <table> <pk>`.

**Idempotency:** `uq_graph_provenance_triple` unique index on `(source_table, source_pk, triple_hash)` WHERE `purged_at IS NULL`. On conflict → skip (already projected). Neo4j `MERGE` on `node_key` is idempotent by Neo4j constraint.

**Fail-closed rules:**
- If governance filter raises (DB error, policy check fails) → abort projection run with `status='failed'`.
- If `extract_graph_triples` raises `LLMProviderError` for a source → count as failed source in run stats, continue with remaining sources. Partial projection is acceptable; full abort is not required on per-source LLM failure.
- If Neo4j adapter raises on MERGE → retry once; on second failure → mark run `status='failed'`, roll back Postgres provenance rows for that batch.

---

### §5.D. Neo4j adapter

**File:** `bot/services/neo4j_adapter.py`

**Connection:** `neo4j` async driver via `neo4j-driver` Python package. Bolt URI from `settings.NEO4J_BOLT_URI`. Auth from `settings.NEO4J_AUTH_USER` + `settings.NEO4J_AUTH_PASSWORD`. Connection pool size: 10 (configurable).

**MERGE node template:**

```cypher
MERGE (n:MemoryNode {node_key: $node_key})
ON CREATE SET
    n.label = $label,
    n.node_type = $node_type,
    n.topic_tags = $topic_tags,
    n.provenance_ids = [$provenance_id],
    n.created_at = datetime()
ON MATCH SET
    n.provenance_ids = n.provenance_ids + [$provenance_id],
    n.updated_at = datetime()
RETURN n.node_key
```

**MERGE edge template:**

```cypher
MATCH (s:MemoryNode {node_key: $subject_key})
MATCH (o:MemoryNode {node_key: $object_key})
MERGE (s)-[r:GRAPH_EDGE {edge_key: $edge_key}]->(o)
ON CREATE SET
    r.predicate = $predicate,
    r.confidence = $confidence,
    r.provenance_ids = [$provenance_id],
    r.created_at = datetime()
ON MATCH SET
    r.provenance_ids = r.provenance_ids + [$provenance_id],
    r.updated_at = datetime()
RETURN r.edge_key
```

**Purge template (forget cascade):**

```cypher
-- Detach provenance from node (shared-node case: other sources remain)
MATCH (n:MemoryNode)
WHERE $provenance_id IN n.provenance_ids
SET n.provenance_ids = [x IN n.provenance_ids WHERE x <> $provenance_id]

-- Delete node if no remaining provenance (last-provenance-removed case)
MATCH (n:MemoryNode)
WHERE size(n.provenance_ids) = 0
DETACH DELETE n
```

**Schema constraints:** applied at adapter `__aenter__` startup via `CREATE CONSTRAINT IF NOT EXISTS` Cypher (see §5.A Neo4j schema constraints). Startup is idempotent.

**Neo4j operational readiness (HIGH B):** Adding Neo4j to compose is dev-only via `--profile graph`. Production deploy requires the Neo4j operational readiness checklist in §12. The feature flag controls rollout; the checklist controls operational readiness. These are separate gates.

**`edge_key_hash` on relationships:** Every Neo4j relationship created via MERGE must carry `r.edge_key_hash = crc32(r.edge_key)` (signed int64, stored on relationship). Used for drift hash aggregation (§5.I drift algorithm).

**Async purge template (replaces synchronous purge; BLOCKER A):** The `neo4j_adapter.py` purge methods are still the same Cypher templates (detach-or-delete), but they are now called ONLY from `graph_purge_worker`, never inline during cascade. The adapter remains stateless; the worker drives the call sequence.

**Drift detection (MEDIUM F — concrete hash-based algorithm per §5.I):**

```cypher
-- Count-based (fast sanity check)
MATCH (n:MemoryNode)
RETURN count(n) AS node_count

MATCH ()-[r:GRAPH_EDGE]->()
RETURN count(r) AS edge_count

-- Hash-based (authoritative drift signal): edge_key_hash stored on each relationship at MERGE time
MATCH ()-[r:GRAPH_EDGE]->()
RETURN toString(sum(r.edge_key_hash)) AS neo4j_edge_hash
```

These are compared against active `graph_provenance`/`graph_edges` rows in Postgres by `reconcile_counts()`. The `edge_key_hash` is a signed int64 derived from `crc32(edge_key)`, stored via MERGE `ON CREATE SET r.edge_key_hash = $edge_key_hash`.

---

### §5.E. `graph_query.py` read-only traversal API

**File:** `bot/services/graph_query.py`

**Public API:**

```python
@dataclass(frozen=True)
class GraphPath:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    provenance_ids: list[int]  # graph_provenance.id references
    source_message_version_ids: list[int]
    source_card_ids: list[str]  # UUIDs

async def find_related_topics(
    session: AsyncSession,
    neo4j_adapter: Neo4jAdapter,
    *,
    topic: str,
    viewer_is_admin: bool,
    max_hops: int = 3,
    max_results: int = 20,
) -> list[GraphPath]: ...

async def find_people_for_topic(
    session: AsyncSession,
    neo4j_adapter: Neo4jAdapter,
    *,
    topic: str,
    viewer_is_admin: bool,
) -> list[GraphPath]: ...

async def explain_connection(
    session: AsyncSession,
    neo4j_adapter: Neo4jAdapter,
    *,
    node_a: str,
    node_b: str,
    viewer_is_admin: bool,
    max_hops: int = 5,
) -> list[GraphPath]: ...

async def sources_for_path(
    session: AsyncSession,
    *,
    provenance_ids: list[int],
) -> list[GraphProvenanceRow]: ...

async def graph_stats(
    session: AsyncSession,
    neo4j_adapter: Neo4jAdapter,
) -> GraphStatsResult: ...
```

**Rules:**

- **Read-only.** No writes, no MERGE, no LLM calls.
- **Provenance required.** Every `GraphPath` returned must have at least one `graph_provenance.id`. Results with zero provenance are silently dropped (never returned). Missing provenance = drift = fail closed.
- **Pending-purge read-block (BLOCKER A — RFC-001:415 strict pattern).** Before executing any Cypher traversal: check `graph_purge_pending` for non-purged rows with `graph_node_key` matching candidate nodes. If any found → return `abstained=True` without Neo4j query. This enforces fail-closed behavior during the async purge window.
- **Role/visibility filter.** `viewer_is_admin=False` is not supported in Phase 10 (all callers are admin handlers). Field exists for future butler expansion.
- **Abstain semantics.** If the Neo4j traversal finds nodes but all provenance rows have `purged_at IS NOT NULL`, return empty list (forgotten-source abstain). Never return results traceable only to purged provenance.
- **No raw content.** `GraphPath` nodes carry only `label` and `node_type` from Neo4j, plus `provenance_ids`. Raw message text is never included in graph query results. If the caller wants source text, it must go through the evidence layer separately.
- **Flag gate.** All public methods raise `GraphQueryDisabledError` if `memory.graph.query.enabled` is OFF OR if `memory.graph.write_pending.paused` is ON.

**Cypher traversal template:**

```cypher
MATCH path = (start:MemoryNode)-[*1..{max_hops}]-(end:MemoryNode)
WHERE start.label = $topic
  AND ALL(r IN relationships(path) WHERE size(r.provenance_ids) > 0)
WITH path, nodes(path) AS ns, relationships(path) AS rs
RETURN ns, rs
LIMIT {max_results}
```

---

### §5.F. Forget cascade integration — `_cascade_graph_provenance` layer

**This async-cascade design implements RFC-001 conditional Neo4j approval per
`docs/memory-system/rfcs/RFC-001-graph-store-benchmark.md:415`.**

**Layer name:** `"graph_nodes"` (external name in `CASCADE_LAYER_ORDER`; internal function is `_cascade_graph_provenance`)

**Position in `CASCADE_LAYER_ORDER`** (`bot/services/forget_cascade.py:133`):

```python
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "qa_traces",
    "llm_synthesis_cache",
    "qa_traces_llm",
    "llm_usage_ledger",
    "digests",
    "card_sources",
    "message_entities",
    "message_links",
    "attachments",
    "fts_rows",
    # Phase 10 / T10-06 layer:
    # MUST run AFTER card_sources so card_source ids are still
    # resolvable when we look up graph_provenance rows keyed by card.
    # MUST run AFTER message_versions so the affected mvid set is
    # fully resolved before the graph purge begins.
    # Advisory-lock-guarded Postgres writes only in this layer;
    # actual Neo4j bolt DELETE is delegated to graph_purge_worker (async).
    "graph_nodes",
)
```

**Why `_cascade_graph_provenance` (not FK CASCADE) is the primary mechanism (HIGH C):**

- `knowledge_cards` rows are ARCHIVED (not deleted) when all sources are forgotten (`forget_cascade.py:791` sets `card_status='archived'`). FK `ON DELETE CASCADE` on `graph_provenance.source_card_id` would NOT fire for archived cards. The application layer must query by `source_card_id` explicitly.
- `message_versions` rows are REDACTED/UPDATED (not deleted) on forget (`forget_cascade.py:297`). FK `ON DELETE CASCADE` on `graph_provenance.source_message_version_id` would NOT fire on updates. The application layer must query by `source_message_version_id`.
- `card_sources` rows ARE deleted (`forget_cascade.py:770`). FK CASCADE would fire for direct card_source deletes — but this is treated as a safety net only, not the primary mechanism.

**Cascade function `_cascade_graph_provenance(session, event, affected_mvids, bot)` behaviour:**

1. Query `graph_provenance` by `source_table + source_pk + source_message_version_id`:
   - Rows where `source_message_version_id IN :affected_mvids`
   - Rows where `source_card_id` is linked to a card whose ALL `source_message_version_ids` are in `:affected_mvids`
   - Note: these are logical application-code queries, NOT FK CASCADE triggers.
2. Soft-delete matching `graph_provenance` rows: `SET purged_at = now(), purge_reason = 'forget_cascade'`.
3. Soft-delete corresponding `graph_edges` rows: `SET purged_at = now()`.
4. **Enqueue `graph_purge_pending` rows** (one per affected `graph_provenance` row) with `forget_event_id`, `source_table`, `source_pk`, `graph_node_key`, `graph_edge_key`. This write is ATOMIC with the Postgres-side cascade transaction — same commit.
5. All Postgres writes in the same advisory-lock-guarded transaction. NO Neo4j bolt call in this layer. Actual Neo4j purge is delegated to `graph_purge_worker`.

**Fail-closed contract:**
- While `graph_purge_pending` has non-purged rows for a node, `graph_query.py` returns `abstained=True` — fail-closed during async purge window.
- If `graph_purge_worker` bolt calls fail repeatedly → DLQ (`failed_at IS NOT NULL`) + structured alert. `graph_query.py` continues to fail-closed because `graph_purge_pending` rows remain non-purged.
- Manual recovery: fix Neo4j bolt connection, restart `graph_purge_worker`. Pending rows are idempotently retried.

**Invariant #9 binding:** the `graph_nodes` layer is mandatory. The cascade worker MUST NOT skip it when the feature flag `memory.graph.projection.enabled` is OFF — the flag gates new projections, not purges of already-projected content. Purge always runs if `graph_provenance` rows exist for the affected source.

---

### §5.G. Admin Telegram handlers

**File:** `bot/handlers/admin_graph.py`

All handlers use `_is_admin(message)` from `bot/handlers/admin_cards.py:58-61` (canonical Phase 6 pattern). Non-admin invocations → silent no-op (no content leak).

**`/graph_project_now [dry_run|incremental|full_rebuild|repair <table> <pk>]`**

- Default mode: `dry_run` (safe first). Admin must explicitly pass `incremental` or `full_rebuild` to write to Neo4j.
- Gate: `memory.graph.projection.enabled` must be ON for non-dry-run modes. Dry_run always allowed.
- Reports: source rows scanned, projected triples estimate (dry) or actual (live), skipped policy rows, token estimate or actual, cost estimate or actual, run_id for audit.
- `/graph_project_now full_rebuild` requires admin confirmation reply within 60 seconds ("yes" or "да") before executing (invariant #6 protection — rebuild clears all provenance).

**`/graph_stats`**

- Admin-only, no flag gate.
- Reports: last run status + timestamp + mode, total active provenance rows, total active edge rows, Neo4j node count, Neo4j edge count, drift count (Postgres active edges vs Neo4j edges), last purge event timestamp, **pending-purge row count** (from `graph_purge_pending WHERE purged_at IS NULL`), **DLQ count** (from `graph_purge_pending WHERE failed_at IS NOT NULL`).

**`/graph_query <topic|concept>`**

- Admin-only.
- Gate: `memory.graph.query.enabled` must be ON. If OFF → "Graph query is disabled. Enable memory.graph.query.enabled to proceed."
- Calls `graph_query.find_related_topics(...)` with `viewer_is_admin=True`.
- Returns concise path list (≤10 results) with node labels and source reference counts. No raw message text in output.
- If result list is empty → "No governed graph paths found for '{topic}'. Abstaining."
- Fails closed on missing provenance or disabled flag.

---

### §5.H. Scheduler integration

**Addition to `bot/services/scheduler.py`** in `setup_scheduler(bot)`:

```python
if settings.GRAPH_PROJECTION_ENABLED:
    scheduler.add_job(
        graph_projection_job,
        "cron",
        hour=settings.GRAPH_PROJECTION_HOUR_MSK,
        minute=settings.GRAPH_PROJECTION_MINUTE_MSK,
        args=[bot],
        id="graph_projection_nightly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,  # 30-min grace (projection can run long)
        timezone=ZoneInfo("Europe/Moscow"),
    )
```

Default: `03:30 MSK` (03:30 MSK = 00:30 UTC). Separate from digest cron (09:00/09:15 MSK) — no LLM gateway pressure overlap.

**Job body (`graph_projection_job` in `bot/services/graph_projector.py`):**

```python
async def graph_projection_job(bot: Bot) -> None:
    async with async_session() as session:
        flag = await FeatureFlagRepo(session).is_enabled("memory.graph.projection.enabled")
        if not flag:
            logger.info("graph_projection_job: flag disabled, skipping")
            return
        config = load_graph_projector_config()
        try:
            result = await project_incremental(session, config=config)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("graph_projection_job: incremental projection failed")
```

---

### §5.I. Drift detection and rebuild semantics

**`reconcile_counts(session, neo4j_adapter) -> GraphDriftReport` concrete algorithm (MEDIUM F):**

1. **Postgres active hash**: compute `postgres_active_hash` by aggregating over sorted active `graph_edges.triple_hash` values:
   ```sql
   SELECT md5(string_agg(triple_hash, ',' ORDER BY triple_hash))
   FROM graph_provenance
   WHERE purged_at IS NULL AND triple_hash IS NOT NULL;
   ```
2. **Neo4j active hash**: aggregate over Neo4j relationship `edge_key_hash` values (stored on each relationship at MERGE time):
   ```cypher
   MATCH ()-[r:GRAPH_EDGE]->()
   RETURN toString(sum(r.edge_key_hash)) AS neo4j_edge_hash
   ```
3. **Hash comparison**: if `postgres_active_hash != neo4j_edge_hash` → `drift_detected=True` → alert + auto-trigger repair recommendation.
4. **Orphan/missing detection**:
   - **Neo4j orphan keys**: node/edge keys in Neo4j with no active `graph_provenance` row → `drift_orphan_node_count`.
   - **Neo4j missing keys**: active `graph_provenance` rows with no corresponding Neo4j node → `drift_missing_node_count`.
5. Return `GraphDriftReport(postgres_active_provenance, postgres_active_edges, neo4j_node_count, neo4j_edge_count, postgres_active_hash, neo4j_edge_hash, drift_detected, drift_orphan_node_count, drift_orphan_edge_count, drift_missing_node_count)`.

Also check: `SELECT count(*) FROM graph_purge_pending WHERE purged_at IS NULL` — non-zero means async purge still in progress (not a drift error, but included in report as `pending_purge_count`).

**Rebuild semantics (HIGH G — replay-based, not re-extraction):**

`project_full_rebuild()` rebuilds by **replaying stored `graph_edges` + `graph_provenance` rows into Neo4j via MERGE — no LLM calls.** The Postgres tables are the canonical store; Neo4j is a materialized projection. Each `graph_edges` row stores `prompt_version`, `model`, `source_content_hash`, `triple_hash`, `extraction_run_id` — enabling deterministic replay.

Cold rebuild procedure:
1. Fetch all active `graph_provenance` + `graph_edges` rows from Postgres.
2. Issue Neo4j `MERGE` for each node/edge using stored `node_key`, `edge_key`, `predicate`, `confidence`.
3. No LLM calls during rebuild. All semantic content comes from Postgres-side stored triples.

Full LLM re-extraction (new triples from source text) is a SEPARATE operation (`project_incremental` or `project_full_rebuild(force_reextract=True)`), NOT the default rebuild path.

**I8d binding test** (`test_graph_cascade.py::test_rebuild_determinism`) verifies: cold Neo4j rebuild from Postgres `graph_edges` + `graph_provenance` (replay mode, no LLM) produces identical Cypher MERGE output (sorted by `triple_hash`) as a sequential replay run. This is exact-equality deterministic.

**I8e binding test** (NEW — `test_graph_cascade.py::test_reextraction_jaccard`): re-extraction with same `prompt_version` + `model` + source set produces `triple_hash` multiset within 90% Jaccard overlap. Softer determinism eval that acknowledges LLM temperature variance while bounding regression.

---

## §6. Wave Dependency Diagram

```
Wave 1 — Foundation (T10-01 .. T10-03)
  T10-01: Docker Compose neo4j service + Neo4j adapter skeleton + schema constraints
  T10-02: Postgres migrations 060-064 (graph_projection_runs, graph_provenance, graph_edges, graph_purge_pending, add_llm_ledger_call_type)
  T10-03: llm_gateway.extract_graph_triples + prompt template + entity resolution contract
  (all Wave 1 tickets may run in parallel after T10-S0)

Wave 2 — Projection + Cascade (T10-04 .. T10-06)
  T10-04: graph_projector.py (dry_run + incremental + full_rebuild + repair)
          depends on T10-01, T10-02, T10-03
  T10-05: graph_query.py read-only API
          depends on T10-01, T10-02
  T10-06: Async cascade layer (Postgres enqueue + worker) — _cascade_graph_provenance + graph_purge_worker extension of cascade_worker_tick + scheduler hook
          depends on T10-02, T10-04
  (T10-04 and T10-05 may run in parallel; T10-06 depends on T10-04)

Wave 3 — Query API + Handlers + Tests (T10-07 .. T10-09)
  T10-07: admin handlers /graph_project_now /graph_stats /graph_query
          depends on T10-04, T10-05, T10-06
  T10-08: drift detection reconcile_counts + /graph_stats drift reporting
          depends on T10-05, T10-06
  T10-09: Phase 11 binding tests (L10/C9/I8/R7/G2) + drift simulation harness
          depends on T10-01..T10-08

Final Holistic Review (FHR) after T10-09 merged. Two reviewers:
  Claude deep-product-reviewer (product lens + privacy invariants)
  Codex deep-technical (migration safety + cascade correctness + Neo4j ops)
Fix CRITICAL/HIGH before closure report.
```

---

## §7. Sprint Breakdown — T10-01 through T10-09

### T10-S0 — Sprint 0: Authorization + plan commit (docs-only)

**Title:** Authorize Phase 10 + promote canonical PHASE10_PLAN.md

**Description:** Update `AUTHORIZED_SCOPE.md` to replace "conditionally authorized" bullet for Phase 10 with "Authorized: Phase 10" block. Commit `docs/memory-system/PHASE10_PLAN.md` (this file). No code, no migrations, no handlers.

**Dependencies:** Phase 8 closure confirmed.

**Acceptance criteria:**
- `AUTHORIZED_SCOPE.md` "NOT authorized" section no longer lists Phase 10 as conditional.
- New `## Authorized: Phase 10 — Graph Projection (2026-05-16)` block inserted, structured like Phase 7/8 blocks.
- `PHASE10_PLAN.md` committed at canonical path.
- Single docs-only PR, no code changes.

**Phase 11 tests added:** none (docs-only sprint).

**Migration:** none.

---

### T10-01 — Neo4j service + adapter skeleton + schema constraints

**Title:** Neo4j Docker service + `neo4j_adapter.py` + schema constraints

**Description:** Add `neo4j` service to `docker-compose.yml` under `profiles: [graph]`. Implement `bot/services/neo4j_adapter.py` with bolt connection, schema constraint application on startup, MERGE node/edge templates, purge template, and count queries for drift detection. No projection logic yet.

**Dependencies:** T10-S0

**Acceptance criteria:**
- `docker compose --profile graph up` starts Neo4j 5.x Community.
- `NEO4J_BOLT_URI`, `NEO4J_AUTH_USER`, `NEO4J_AUTH_PASSWORD`, `NEO4J_DATABASE` env vars in `bot/config.py` with defaults for dev (`bolt://neo4j:7687`, `neo4j`/`password`, `neo4j`). Dev default password is `password`; production password rotation via `NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}` with min-32-char enforcement (see §12 operational checklist).
- Schema constraints applied on `Neo4jAdapter.__aenter__`: `node_key_unique`, `node_provenance_id_exists`, `edge_key_unique`. Startup is idempotent (IF NOT EXISTS).
- `MERGE` node + edge templates tested against real neo4j (dev) and mock bolt (CI). Relationship MERGE stores `edge_key_hash` (signed int64 crc32 of edge_key) for drift hash aggregation.
- Purge methods exist on adapter (called from `graph_purge_worker` only, never inline during cascade): node with last provenance removed is `DETACH DELETE`'d; node with remaining provenance has its `provenance_ids` array updated.
- Count queries return `node_count`, `edge_count`, and `neo4j_edge_hash` (sum of edge_key_hash).
- `tests/services/test_neo4j_adapter.py`: constraint application, MERGE idempotency, purge semantics (shared node vs sole-provenance node), count + hash queries. Mock bolt driver in CI (no real Neo4j in CI for unit tests — T10-09 adds testcontainers integration fixture, HIGH K).
- **HIGH B gate documented in §12 PHASE10_ROLLOUT.md**: production Neo4j requires completing the operational readiness checklist before flipping `memory.graph.projection.enabled = ON`.

**Files touched:**
- `docker-compose.yml`
- `bot/services/neo4j_adapter.py` (NEW)
- `bot/config.py`
- `tests/services/test_neo4j_adapter.py` (NEW)

**Phase 11 tests added:** none (foundational, tests in T10-09).

**Migration:** none.

---

### T10-02 — PostgreSQL migrations 060-062

**Title:** Add `graph_projection_runs`, `graph_provenance`, `graph_edges`, `graph_purge_pending` tables + `llm_usage_ledger.call_type`

**Description:** Implement alembic migrations 060 (`graph_projection_runs`), 061 (`graph_provenance`), 062 (`graph_edges`), 063 (`graph_purge_pending`), 064 (`add_llm_ledger_call_type`) with full DDL per §5.A. Add ORM classes `GraphProjectionRun`, `GraphProvenance`, `GraphEdge`, `GraphPurgePending` to `bot/db/models.py`. Add corresponding repos to `bot/db/repos/`.

**Dependencies:** T10-S0

**Acceptance criteria:**
- All five migrations run forward and backward cleanly in CI Postgres.
- Rollback drops `graph_purge_pending` → `graph_edges` → `graph_provenance` → `graph_projection_runs` (FK order respected). Migration 064 rollback drops `call_type` column from `llm_usage_ledger`.
- `ck_graph_projection_runs_mode` CHECK constraint enforces `('dry_run', 'incremental', 'full_rebuild', 'repair')`.
- `ck_graph_projection_runs_status` CHECK enforces `('running', 'completed', 'failed', 'cancelled', 'cost_exceeded', 'dry_run_complete')`.
- `ck_graph_provenance_has_source` CHECK: `source_message_version_id IS NOT NULL OR source_card_id IS NOT NULL`.
- `uq_graph_provenance_triple` unique index on `(source_table, source_pk, triple_hash) WHERE purged_at IS NULL`.
- `uq_graph_edges_key` unique index on `(edge_key) WHERE purged_at IS NULL`.
- `uq_graph_purge_pending_event_source` UNIQUE constraint on `(forget_event_id, source_table, source_pk)`.
- `llm_usage_ledger.call_type` column exists as `VARCHAR(32) NOT NULL DEFAULT 'unknown'`.
- `ix_graph_purge_pending_queue` index for worker polling (pending rows by enqueue time).
- `ix_graph_purge_pending_node_key` index for fail-closed check in `graph_query.py`.
- `tests/db/test_graph_schema.py`: unique constraint, CHECK constraint violations, FK CASCADE from `graph_projection_runs` to `graph_provenance` to `graph_edges`, `graph_purge_pending` unique constraint, `call_type` column presence, rollback.

**Files touched:**
- `alembic/versions/060_add_graph_projection_runs.py` (NEW)
- `alembic/versions/061_add_graph_provenance.py` (NEW)
- `alembic/versions/062_add_graph_edges.py` (NEW)
- `alembic/versions/063_add_graph_purge_pending.py` (NEW)
- `alembic/versions/064_add_llm_ledger_call_type.py` (NEW)
- `bot/db/models.py`
- `bot/db/repos/graph_projection_run.py` (NEW)
- `bot/db/repos/graph_provenance.py` (NEW)
- `bot/db/repos/graph_edge.py` (NEW)
- `bot/db/repos/graph_purge_pending.py` (NEW)
- `tests/db/test_graph_schema.py` (NEW)

**Phase 11 tests added:** none (schema tests only; evals in T10-09).

**Migration:** 060, 061, 062, 063, 064.

---

### T10-03 — `llm_gateway.extract_graph_triples`

**Title:** New `extract_graph_triples` gateway method + prompt template

**Description:** Add `extract_graph_triples` to `bot/services/llm_gateway.py` (peer to `extract_candidates` at :1058). Implement `bot/services/llm_prompts/graph_triples_v0_1_0.py` prompt template. Full behaviour per §5.B: pre-call governance assertion, budget check, ledger placeholder, structured JSON parse, UNKNOWN-label refusal, max_triples cap, update_placeholder.

**Dependencies:** T10-S0 (Phase 5 `llm_gateway` is already closed — this extends it)

**Acceptance criteria:**
- `extract_graph_triples` raises `GraphProjectionPolicyError` if `governance_policy != 'normal'`.
- Empty `source_text` → returns `ExtractGraphTriplesResult(triples=[], ...)` (no LLM call — short-circuit before prompt).
- `subject_label == "UNKNOWN"` triples counted in `skipped_unknown`, not included in result.
- Entity resolving to `UNKNOWN_{md5}` placeholder → triple DROPPED, counted in `skipped_unknown`.
- **Entity resolution contract (HIGH D AC):** For each triple subject/object: (a) check `knowledge_cards.id` by title match if source is card-triggered; (b) check `users.id` by display_name/username for Person-type entities; (c) generate `UNKNOWN_{md5(label)[:8]}` if unresolvable → drop triple.
- `max_triples=5` cap enforced after validation and entity resolution.
- Malformed JSON from provider → `ExtractGraphTriplesError` raised.
- Budget check fires from shared `_budget_check` + caller-side `GRAPH_PROJECTION_RUN_USD_CEILING` check is separate.
- `LedgerRepo` placeholder + update recorded for every provider call with `call_type='graph_projection'` (migration 064 column).
- `tests/services/test_extract_graph_triples.py`: governance assertion, UNKNOWN refusal, entity resolution (card match, user match, unresolvable → drop), max_triples cap, budget exceeded, hallucinated predicate rejection, empty source text short-circuit, ledger `call_type` field.

**Files touched:**
- `bot/services/llm_gateway.py`
- `bot/services/llm_prompts/graph_triples_v0_1_0.py` (NEW)
- `tests/services/test_extract_graph_triples.py` (NEW)

**Phase 11 tests added:** none (gateway unit tests; evals in T10-09).

**Migration:** none.

---

### T10-04 — `graph_projector.py`

**Title:** Graph projector service — dry_run / incremental / full_rebuild / repair modes

**Description:** Implement `bot/services/graph_projector.py` with all four projection modes per §5.C. Governance pre-filter mandatory before any LLM dispatch. Idempotency via `uq_graph_provenance_triple`. Fail-closed on governance filter failure. Per-source LLM failure is counted (not aborting). Scheduler job `graph_projection_job` added to `bot/services/scheduler.py`.

**Dependencies:** T10-01, T10-02, T10-03

**Acceptance criteria:**
- `dry_run` writes `graph_projection_runs(mode='dry_run', status='dry_run_complete')`, no Neo4j writes, no `graph_provenance` rows.
- `project_incremental` correctly scans only source rows newer than last completed run's `source_cutoff_at`.
- `project_full_rebuild` clears all active provenance rows and all Neo4j nodes/edges, then rebuilds. Two consecutive calls produce identical `projected_node_count` (I8d binding, pre-verified here).
- Governance pre-filter excludes `memory_policy != 'normal'`, `is_redacted=TRUE`, active `forget_events` rows. Filtered rows counted in `skipped_policy_count`.
- Per-source LLM failure counted in run stats; projection continues with remaining sources.
- Budget ceiling checks fire before any gateway call: (a) per-run dry-run cost estimate vs `GRAPH_PROJECTION_RUN_USD_CEILING` (abort if estimate exceeds it); (b) daily bucket SUM filtered by `call_type='graph_projection'` vs `GRAPH_PROJECTION_DAILY_USD_CEILING`. Either exceeded → `status='cost_exceeded'`, no LLM calls.
- Idempotency: second `project_incremental` on already-projected sources does not create duplicate provenance rows.
- Scheduler job `graph_projection_nightly` registered at 03:30 MSK. Flag `memory.graph.projection.enabled` OFF → job body exits early without DB writes.
- `tests/services/test_graph_projector.py`: dry_run stats, governance exclusion, budget ceiling, incremental idempotency, full_rebuild determinism, per-source LLM failure tolerance, flag gate.

**Files touched:**
- `bot/services/graph_projector.py` (NEW)
- `bot/services/scheduler.py`
- `bot/config.py`
- `tests/services/test_graph_projector.py` (NEW)

**Phase 11 tests added:** L10a/b (governance pre-filter prevents offrecord/forgotten content from reaching projection — pre-verified here, binding version in T10-09).

**Migration:** none.

---

### T10-05 — `graph_query.py` read-only API

**Title:** Read-only graph traversal API with provenance-required contract

**Description:** Implement `bot/services/graph_query.py` with `find_related_topics`, `find_people_for_topic`, `explain_connection`, `sources_for_path`, and `graph_stats` per §5.E. All methods fail closed on missing provenance. `memory.graph.query.enabled` flag gate on all query methods.

**Dependencies:** T10-01, T10-02

**Acceptance criteria:**
- Every `GraphPath` returned has at least one non-NULL provenance_id referencing an active `graph_provenance` row (`purged_at IS NULL`).
- Paths with all provenance rows purged are silently dropped (abstain).
- `memory.graph.query.enabled = OFF` → `GraphQueryDisabledError` on any query call.
- `graph_stats` returns Postgres active counts + Neo4j node/edge counts + drift detected flag.
- No raw message text in any return value.
- `tests/services/test_graph_query.py`: provenance-required enforcement, purged-provenance abstain, flag gate, empty-result abstain, drift flag in stats.

**Files touched:**
- `bot/services/graph_query.py` (NEW)
- `tests/services/test_graph_query.py` (NEW)

**Phase 11 tests added:** R7.a (non-admin cannot `/graph_query` — pre-verified here, binding in T10-09), R7.b (no governed evidence → abstain).

**Migration:** none.

---

### T10-06 — Forget cascade `_cascade_graph_provenance` layer + `graph_purge_worker`

**Title:** Extend `CASCADE_LAYER_ORDER` with `graph_nodes` async-cascade layer + `graph_purge_worker`

**Description:** Add `_cascade_graph_provenance` function to `bot/services/forget_cascade.py`. Insert `"graph_nodes"` into `CASCADE_LAYER_ORDER` AFTER `"card_sources"` and `"fts_rows"` (last layer). Register in `_LAYER_FUNCS`. The cascade function atomically enqueues `graph_purge_pending` rows and soft-deletes `graph_provenance`/`graph_edges` rows — NO bolt call in cascade layer. `graph_purge_worker` drains queue asynchronously; `graph_query.py` fails-closed on pending-purge nodes via read-block (RFC-001:415). Separately, implement `graph_purge_worker` in `bot/services/graph_projector.py` as an extension of `cascade_worker_tick` to consume `graph_purge_pending` rows and drive Neo4j bolt DELETE.

**Key design: `_cascade_graph_provenance` uses application-code queries (NOT FK CASCADE) as the primary purge lookup, because `knowledge_cards` are archived (not deleted) and `message_versions` are redacted (not deleted) — FK CASCADE would not fire for these paths (HIGH C).**

**Dependencies:** T10-02, T10-04

**Acceptance criteria:**
- `CASCADE_LAYER_ORDER` ends with `"graph_nodes"` — verified by assertion in test.
- `_cascade_graph_provenance` queries `graph_provenance` by `source_message_version_id IN :affected_mvids` AND by `source_card_id` for cards with all sources forgotten — NOT relying on FK CASCADE.
- Postgres `graph_provenance.purged_at = now()` and `graph_edges.purged_at = now()` set within same cascade transaction.
- `graph_purge_pending` rows enqueued atomically in same cascade transaction (keyed by `forget_event_id + source_table + source_pk`).
- `graph_purge_worker` (extension of `cascade_worker_tick`) fetches pending rows, issues bolt DELETE, marks `purged_at` on success, `failed_at + error` after 5 retries.
- `graph_query.py` fails-closed (`abstained=True`) while any `graph_purge_pending` row for affected node_keys is non-purged — verified by pending-purge pre-check test.
- `memory.graph.projection.enabled = OFF` does NOT skip graph_nodes cascade — if `graph_provenance` rows exist, purge enqueue runs unconditionally.
- Layer order binding test: `CASCADE_LAYER_ORDER.index("graph_nodes") > CASCADE_LAYER_ORDER.index("card_sources")` (I8b).
- Integration test: insert message → project → forget → verify `graph_provenance.purged_at` set + `graph_purge_pending` enqueued → worker runs → Neo4j node absent (I8a).
- New test R7.d: `graph_query.find_related_topics` returns `abstained=True` while `graph_purge_pending` row for queried node exists with `purged_at IS NULL`.
- `tests/services/test_graph_cascade.py`: shared-node detach logic, last-provenance delete logic, `graph_purge_pending` enqueue within cascade tx, worker DLQ on repeated bolt failure, flag-OFF still enqueues purge, layer order assertion.

**Files touched:**
- `bot/services/forget_cascade.py`
- `bot/services/graph_projector.py` (extend with `graph_purge_worker`)
- `tests/services/test_graph_cascade.py` (NEW)

**Phase 11 tests added:** I8a (forget enqueues purge + worker completes + Neo4j node absent), I8b (cascade order: graph_nodes after card_sources), I8c (worker bolt failure → DLQ row, graph query abstains), R7.d (graph query abstains while pending-purge row exists).

**Migration:** none (migrations 063/064 already in T10-02).

---

### T10-07 — Admin handlers `/graph_project_now`, `/graph_stats`, `/graph_query`

**Title:** Admin Telegram handlers for graph projection and query

**Description:** Implement `bot/handlers/admin_graph.py` with all three handlers per §5.G. Wire into `bot/handlers/__init__.py` router. All admin-only via `_is_admin(message)`. Flag-gated. Dry-run default for projection. Full-rebuild requires admin confirmation reply.

**Dependencies:** T10-04, T10-05, T10-06

**Acceptance criteria:**
- Non-admin → silent no-op for all three handlers.
- `/graph_project_now` with no args defaults to `dry_run`, reports source counts, triple estimate, cost estimate, run_id.
- `/graph_project_now incremental` requires `memory.graph.projection.enabled = ON`.
- `/graph_project_now full_rebuild` requires `memory.graph.projection.enabled = ON` AND admin confirmation reply "yes"/"да" within 60s.
- `/graph_stats` returns last run status + timestamps + Postgres/Neo4j count comparison + drift detected flag.
- `/graph_query <topic>` requires `memory.graph.query.enabled = ON`. Returns ≤10 path results with node labels + source reference counts. Empty result → abstain message.
- R7.a binding: non-admin `/graph_query` → silent no-op (verified here and in T10-09).
- Unit tests cover all flag-gate states, admin gate, dry-run output format, abstain message, confirmation timeout.

**Files touched:**
- `bot/handlers/admin_graph.py` (NEW)
- `bot/handlers/__init__.py`
- `tests/` (handler unit tests inline with T10-07 PR)

**Phase 11 tests added:** R7.c (query referencing forgotten source → aborts, not included in results), R7.d (graph_query abstains while graph_purge_pending non-purged row exists — verified in T10-06 + T10-09).

**Migration:** none.

---

### T10-08 — Drift detection + reconcile_counts + `/graph_stats` drift reporting

**Title:** Drift detection service + `/graph_stats` drift signals + PHASE10_ROLLOUT.md

**Description:** Implement `reconcile_counts(session, neo4j_adapter) -> GraphDriftReport` per §5.I. Wire into `/graph_stats` handler. Write `docs/memory-system/PHASE10_ROLLOUT.md` (env vars, flag toggle order, dry-run procedure, monitor first 3 cron fires, escalation contacts).

**Dependencies:** T10-05, T10-06

**Acceptance criteria:**
- `reconcile_counts` queries Postgres active provenance/edge counts + Neo4j node/edge counts + computes `postgres_active_hash` (md5 over sorted triple_hash) + `neo4j_edge_hash` (sum of edge_key_hash from Neo4j relationships). Returns `GraphDriftReport` with all fields including `drift_missing_node_count`.
- `/graph_stats` shows `drift_detected: true/false`, `drift_orphan_node_count`, `drift_orphan_edge_count`, `drift_missing_node_count`, `pending_purge_count`, `dlq_count`.
- Integration test simulates drift via direct Cypher MERGE of a node with no Postgres provenance, then calls `reconcile_counts` and asserts `drift_detected=True` AND `postgres_active_hash != neo4j_edge_hash` (G2 binding, pre-verified here).
- `PHASE10_ROLLOUT.md` covers: `NEO4J_*` env var setup, Neo4j operational readiness checklist (§12), `memory.graph.projection.enabled` flag toggle (writer before reader), first `dry_run` dry-run, monitoring `graph_projection_runs` + `graph_purge_pending` tables, escalation path for drift detected.
- `tests/evals/test_graph_drift.py` (G2) scaffolded in this ticket, finalized in T10-09.

**Files touched:**
- `bot/services/graph_query.py` (extend `reconcile_counts`)
- `bot/handlers/admin_graph.py` (extend `/graph_stats` with drift fields)
- `docs/memory-system/PHASE10_ROLLOUT.md` (NEW)
- `tests/evals/test_graph_drift.py` (NEW)

**Phase 11 tests added:** G2 (drift detection: reconcile_counts identifies drift correctly; hash mismatch confirmed; integration test via direct Cypher write).

**Note (MEDIUM H):** R7.a (non-admin graph_query refusal) is a Phase 10 stance, not a permanent restriction. Future butler / member graph use requires a separate phase with role filters, evidence-context mediation, and visibility scoping — Phase 10 explicitly defers this (see §4 Non-Goals and §1 invariant #7).

**Migration:** none.

---

### T10-09 — Phase 11 binding tests (L10/C9/I8/R7/G2) + drift simulation harness

**Title:** Phase 11 binding tests for graph privacy, provenance, cascade, refusal, drift

**Description:** Write all six new test files plus test infrastructure (HIGH K). All use pytest-asyncio + real AsyncSession against CI Postgres. Unit tests use `NetworkX` in-memory fake via `bot/services/graph_adapter_networkx.py` (tagged `@pytest.mark.graph_unit`). Integration + Phase 11 binding tests use real Neo4j via `pytest-testcontainers` (tagged `@pytest.mark.neo4j`, separate CI job). Each binding test was red before the implementation ticket's code was merged. Verify all 15 new binding cases green.

**Dependencies:** T10-01 through T10-08 (all merged)

**Acceptance criteria (per binding case):**

**L10a** (`test_graph_leakage.py`): `#offrecord` message_version_id does NOT appear as a `graph_provenance.source_message_version_id` row nor in any Neo4j node's `provenance_ids` after a full_rebuild. Governance pre-filter must block it before LLM dispatch.

**L10b** (`test_graph_leakage.py`): `#nomem` message_version_id excluded by same governance pre-filter. No graph provenance row. No graph query result.

**L10c** (`test_graph_leakage.py`): Forgotten message_version (active `forget_events` row) does NOT appear in graph provenance rows AND does NOT appear in any graph query result (`find_related_topics`, `explain_connection`).

**C9** (`test_graph_citations.py`): Every active `graph_provenance` row has `source_message_version_id IS NOT NULL OR source_card_id IS NOT NULL`. Every `graph_edges` row is linked (via `graph_provenance_id`) to a provenance row with non-NULL source. Query: assert no `graph_provenance` row has both sources NULL.

**I8a** (`test_graph_cascade.py`): `forget_event` on a cited `message_version_id` → `_cascade_graph_nodes` runs → `graph_provenance.purged_at IS NOT NULL` → Neo4j node absent (MATCH returns 0 nodes). Single cascade run; no retry needed.

**I8b** (`test_graph_cascade.py`): `CASCADE_LAYER_ORDER.index("graph_nodes") > CASCADE_LAYER_ORDER.index("card_sources")` — direct assertion on the tuple. Cascade order test.

**I8c** (`test_graph_cascade.py`): Simulate Neo4j bolt failure in `graph_purge_worker` → DLQ row (`failed_at IS NOT NULL`) written after 5 retries → `graph_query.find_related_topics(...)` returns `abstained=True` (fails closed because `graph_purge_pending` row still exists with `purged_at IS NULL`). Verify `graph_provenance.purged_at` IS set (cascade Postgres commit succeeded) and `graph_purge_pending.purged_at` IS NULL (Neo4j side not yet purged).

**I8d** (`test_graph_cascade.py`): Cold Neo4j rebuild from Postgres `graph_edges` + `graph_provenance` (replay mode, NO LLM calls) produces identical Cypher MERGE output sorted by `triple_hash` as a sequential replay run. Exact-equality determinism test.

**I8e** (`test_graph_cascade.py`): NEW — Re-extraction with same `prompt_version` + `model` + source set produces `triple_hash` multiset within 90% Jaccard overlap between two independent extraction runs. Softer eval that acknowledges LLM temperature variance.

**R7.a** (`test_graph_refusal.py`): Non-admin user sends `/graph_query topic` → handler returns no output (silent no-op). No `graph_query` method call.

**R7.b** (`test_graph_refusal.py`): `graph_query.find_related_topics(...)` on a topic with no governed graph paths → returns empty list. Handler returns abstain message "No governed graph paths found". No error raised.

**R7.c** (`test_graph_refusal.py`): Query result contains provenance rows where `purged_at IS NOT NULL` (forgotten source) → those paths silently dropped from result → if all paths dropped → abstain (not error). Verify no forgotten-source path appears in output.

**R7.d** (`test_graph_refusal.py`): NEW — `graph_query.find_related_topics` called while `graph_purge_pending` has a non-purged row for the queried node → returns `abstained=True` without executing Cypher. Pending-purge read-block verification (BLOCKER A — RFC-001:415 pattern).

**G2** (`test_graph_drift.py`): Insert a Neo4j node directly via Cypher (no Postgres provenance row) → call `reconcile_counts()` → assert `drift_detected=True`, `drift_orphan_node_count >= 1`, `postgres_active_hash != neo4j_edge_hash`. Also: normal state (all provenance matched) → `drift_detected=False`.

**Phase 11 baseline update:** 42 existing cases (L1-L5 + L6a/b/c + C1-C4 + C5a-d + R1-R4 + I1-I4 + L7a/b + C6 + I5a/b/c + L8a/b + C7 + I6a + I6b.1/.2/.3 + I6c + R5.a/b/c/d) + 15 new Phase 10 cases (L10a/b/c + C9 + I8a/b/c/d/e + R7.a/b/c/d + G2) = **57/57 new total**.

**Files touched:**
- `tests/evals/test_graph_leakage.py` (NEW)
- `tests/evals/test_graph_citations.py` (NEW)
- `tests/evals/test_graph_cascade.py` (NEW — includes I8d/I8e rebuild + Jaccard tests)
- `tests/evals/test_graph_refusal.py` (NEW — includes R7.d pending-purge read-block)
- `tests/evals/test_graph_drift.py` (NEW — scaffolded in T10-08, finalized here with hash verification)
- `bot/services/graph_adapter_networkx.py` (NEW — NetworkX test double for `@pytest.mark.graph_unit`)
- `tests/conftest.py` (extend with Neo4j testcontainers fixture for `@pytest.mark.neo4j`)
- `pyproject.toml` — add `neo4j>=5.0,<6`, `networkx>=3.0,<4`, `testcontainers[neo4j]>=4.0` to `[dependency-groups.dev]`
- `.github/workflows/evals.yml` — add Neo4j service block when `EVAL_HARNESS_ENABLED=true` (image: `neo4j:5-community`, ports: `7687:7687`, env `NEO4J_AUTH=neo4j/test_password_min_32_chars_neo4j5`)
- `CLAUDE.md` — Phase 10 closure block
- `docs/memory-system/IMPLEMENTATION_STATUS.md`
- `docs/memory-system/ROADMAP.md`
- `AUTHORIZED_SCOPE.md` — Phase 10 CLOSED marker

**Phase 11 tests added:** all 15 new cases (L10a/b/c + C9 + I8a/b/c/d/e + R7.a/b/c/d + G2). Baseline: 57/57 green.

**Migration:** none.

---

## §8. Stop Signals

Stop Phase 10 immediately and surface to the human team lead if ANY of these occur:

1. **`#offrecord` projection leak.** Any `#offrecord` source_message_version_id appears in `graph_provenance`, Neo4j node provenance_ids, triple extraction input, graph query result, admin handler output, logs, or prompt payloads. STOP ALL projection runs immediately.

2. **`#nomem` projection leak.** Same as (1) for `#nomem` governance policy.

3. **Forgotten content resurrection.** A source with an active `forget_events` row appears in `graph_provenance.purged_at IS NULL` or in a live graph query result. The forget cascade graph_nodes layer has failed.

4. **Cascade fails to enqueue graph purge.** `GraphCascadePurgeError` is raised in `_cascade_graph_provenance` (Postgres enqueue failed) → STOP. Manual remediation: disable `memory.graph.query.enabled`, investigate Postgres write failure. Separately: if `graph_purge_worker` accumulates DLQ rows (bolt repeated failure) → set `memory.graph.write_pending.paused = ON`, fix Neo4j bolt, then resume worker. Graph queries remain fail-closed automatically while DLQ rows exist.

5. **Graph query returns unprovenanced content.** `graph_query.py` returns a path where any node or edge has zero active `graph_provenance` rows (drift). STOP projection + query until `reconcile_counts` confirms clean state.

6. **Direct LLM provider call outside gateway.** Any import of `anthropic`, `openai`, or similar provider SDK directly in `graph_projector.py`, `neo4j_adapter.py`, `graph_query.py`, or admin handlers. STOP — this violates invariant #2.

7. **Cost ceiling not enforced.** Graph projection runs LLM extractions without first checking `GRAPH_PROJECTION_RUN_USD_CEILING` (per-run dry-run estimate) and `GRAPH_PROJECTION_DAILY_USD_CEILING` (daily graph-only bucket) and `GRAPH_PROJECTION_MONTHLY_USD_CEILING` (monthly graph-only bucket). STOP — projection without budget guard violates the cost guardrail.

8. **Graph used as source of truth.** Any code path writes canonical facts ONLY to Neo4j and reads them back without a Postgres source row. STOP — this violates invariant #6.

9. **Expertise page or public surface created.** Phase 10 handler or service exposes graph output to non-admin users, or generates a durable "person expertise" page. STOP — this violates Phase 10 non-goals.

10. **Neo4j schema constraints not applied.** Adapter starts but `node_key_unique` or `node_provenance_id_exists` constraints are absent → nodes without provenance can be written → invariant #4 violated. STOP and fix adapter startup.

---

## §9. PR-Required Checks

Each PR in the sprint_pr_queue must pass before merge:

- `pytest tests/db/test_graph_schema.py` green (schema PRs)
- `pytest tests/services/test_graph_*.py tests/services/test_extract_graph_triples.py` green
- `pytest tests/evals/test_graph_*.py` green (T10-09 only, but L10 governance tests pre-verified from T10-04)
- `scripts/lint_privacy_check.sh` green (no new offrecord/nomem paths in graph code)
- `alembic upgrade head && alembic downgrade -1` round-trip clean in CI Postgres
- `.par-evidence.json` written with `claude_product` + `technical_review` verdicts before `gh pr create`
- CI green before merge (no `gh pr merge --admin`)
- Neo4j adapter tests pass with mock bolt driver in CI (no real Neo4j required until T10-09 integration fixtures)

**Required PR checks per invariant:**
- Invariant #2: no direct provider import in graph services (grep `import anthropic|import openai` in changed files)
- Invariant #3: governance pre-filter present in every `project_*` code path
- Invariant #6: no canonical fact written Neo4j-only
- Invariant #9: `"graph_nodes"` present in `CASCADE_LAYER_ORDER` (T10-06+)

---

## §10. Phase 11 Binding Tests (6 new test files, 15 new cases)

Baseline at Phase 10 start: **42/42** (L1-L5 + L6a/b/c + C1-C4 + C5a-d + R1-R4 + I1-I4 + L7a/b + C6 + I5a/b/c + L8a/b + C7 + I6a + I6b.1/.2/.3 + I6c + R5.a/b/c/d).

Phase 10 adds **15 new cases** (original 12 + I8e Jaccard + R7.d pending-purge read-block + G2 hash sub-case) → baseline at Phase 10 closure: **57/57**.

| Test | File | Ticket | What it tests |
|---|---|---|---|
| L10a | `tests/evals/test_graph_leakage.py` | T10-04, T10-09 | `#offrecord` mvid blocked from projection |
| L10b | `tests/evals/test_graph_leakage.py` | T10-04, T10-09 | `#nomem` mvid blocked from projection |
| L10c | `tests/evals/test_graph_leakage.py` | T10-04, T10-09 | Forgotten mvid absent from provenance + query results |
| C9 | `tests/evals/test_graph_citations.py` | T10-02, T10-09 | Every provenance row has non-NULL source id |
| I8a | `tests/evals/test_graph_cascade.py` | T10-06, T10-09 | Forget purges graph_provenance + graph_edges + Neo4j node |
| I8b | `tests/evals/test_graph_cascade.py` | T10-06, T10-09 | `graph_nodes` layer after `card_sources` in order |
| I8c | `tests/evals/test_graph_cascade.py` | T10-06, T10-09 | Cascade bolt failure → aborts, graph query fails-closed |
| I8d | `tests/evals/test_graph_cascade.py` | T10-04, T10-09 | Cold rebuild from Postgres graph_edges (replay, no LLM) → exact-equality Cypher output |
| I8e | `tests/evals/test_graph_cascade.py` | T10-04, T10-09 | Re-extraction with same prompt+model → ≥90% Jaccard overlap on triple_hash multiset |
| R7.a | `tests/evals/test_graph_refusal.py` | T10-07, T10-09 | Non-admin `/graph_query` → silent no-op |
| R7.b | `tests/evals/test_graph_refusal.py` | T10-05, T10-09 | No governed evidence → abstain, not error |
| R7.c | `tests/evals/test_graph_refusal.py` | T10-05, T10-09 | Forgotten-source path dropped from results |
| R7.d | `tests/evals/test_graph_refusal.py` | T10-06, T10-09 | graph_query returns abstained=True while graph_purge_pending non-purged row exists |
| G2 | `tests/evals/test_graph_drift.py` | T10-08, T10-09 | Drift detected via direct Cypher insert without Postgres provenance; hash mismatch confirmed |

---

## §11. Test Infrastructure (HIGH K)

Phase 10 introduces a two-tier test adapter pattern:

### Tier 1: Unit tests — NetworkX in-memory fake
- **File:** `bot/services/graph_adapter_networkx.py` — implements the same adapter protocol as `neo4j_adapter.py` using `networkx.DiGraph` in-process.
- **Marker:** `@pytest.mark.graph_unit` (no Docker service needed)
- **Scope:** All `tests/services/test_graph_*.py` unit tests use this fake by default.
- **Purpose:** Fast local iteration, CI without Docker, test isolation.

### Tier 2: Integration + Phase 11 binding tests — Neo4j via testcontainers
- **Fixture:** `tests/conftest.py` — `neo4j_session` fixture using `testcontainers.neo4j.Neo4jContainer` (starts real Neo4j 5-community in Docker).
- **Marker:** `@pytest.mark.neo4j` (separate CI job, requires Docker)
- **Scope:** All `tests/evals/test_graph_*.py` binding tests use real Neo4j.
- **`pyproject.toml` dependencies (dev group):**
  ```toml
  [dependency-groups.dev]
  neo4j = ">=5.0,<6"
  networkx = ">=3.0,<4"
  testcontainers = {version = ">=4.0", extras = ["neo4j"]}
  ```

### CI wiring (`.github/workflows/evals.yml`)

Add under `services:` when `EVAL_HARNESS_ENABLED=true`:
```yaml
services:
  neo4j:
    image: neo4j:5-community
    env:
      NEO4J_AUTH: neo4j/test_password_min_32_chars_for_neo4j_5
    ports:
      - '7687:7687'
    options: >-
      --health-cmd "wget -q --spider http://localhost:7474/db/manage/server/lifecycle || exit 1"
      --health-interval 10s
      --health-timeout 5s
      --health-retries 10
```

---

## §11b. Glossary (formerly §11)

**Drift.** A state where Neo4j nodes or edges exist without a corresponding active `graph_provenance` row in Postgres, or vice versa. Postgres wins. Drift is resolved by `project_full_rebuild()`.

**Graph edge.** A typed relationship between two graph nodes: `(subject)-[:PREDICATE]->(object)`. Has a stable `edge_key` (SHA-256 of subject+predicate+object labels) for idempotent MERGE.

**Graph node.** A typed entity node in Neo4j (`MemoryNode` label) keyed by a stable `node_key`. Carries a `provenance_ids` array listing all `graph_provenance.id` values that contributed to it.

**Governed source.** A source row that passes the governance pre-filter: `memory_policy='normal'`, `is_redacted=FALSE`, no active `forget_events` row. Only governed sources may be projected into the graph.

**Projection run.** A tracked execution of graph projection in `dry_run`, `incremental`, `full_rebuild`, or `repair` mode. Each run writes a `graph_projection_runs` row and zero or more `graph_provenance` + `graph_edges` rows.

**Provenance.** The `graph_provenance` row linking a Neo4j node or edge back to a specific Postgres source row and projection run. No node or edge may exist without provenance.

**Shared node.** A graph node supported by provenance rows from more than one source. When one source is forgotten, the node's `provenance_ids` array is updated (source entry removed) but the node is not deleted. The node is deleted only when ALL supporting provenance rows are purged.

**Async cascade purge.** The forget cascade `_cascade_graph_provenance` layer runs within the same advisory-lock-guarded cascade worker tick — but performs ONLY Postgres writes: soft-deleting `graph_provenance`/`graph_edges` rows and enqueuing `graph_purge_pending` rows atomically. Actual Neo4j bolt DELETE is delegated to the `graph_purge_worker` (extension of `cascade_worker_tick`). During the window between Postgres commit and Neo4j purge, `graph_query.py` fails-closed via the pending-purge read-block (RFC-001:415 pattern). If `graph_purge_worker` bolt calls fail repeatedly, rows enter DLQ (`failed_at IS NOT NULL`) and graph queries remain blocked until manually recovered.

**Async purge worker.** Forget cascade ENQUEUES `graph_purge_pending` rows synchronously in the same Postgres transaction as Postgres-side cascade. A separate `graph_purge_worker` (extension of `cascade_worker_tick`) drives Neo4j bolt DELETE asynchronously. `graph_query.py` fails-closed (`abstained=True`) on any node touched by a non-purged `graph_purge_pending` row. RFC-001:415 compliant; replaces an earlier "synchronous purge" proposal that violated RFC-001 conditional Neo4j approval.

---

## §12. Rollout Plan Reference

Operator runbook: `docs/memory-system/PHASE10_ROLLOUT.md` (written in T10-08).

### Neo4j Operational Readiness Checklist (HIGH B — MUST complete before flag ON)

The feature flags control rollout; this checklist controls operational readiness. **Do NOT flip `memory.graph.projection.enabled = ON` until all checklist items are satisfied:**

- [ ] **Healthcheck** wired to Coolify health_check: Neo4j `/db/manage/server/lifecycle` endpoint responding; Coolify configured with `health_check_path=/db/manage/server/lifecycle health_check_port=7474`.
- [ ] **Auth rotation**: Default `neo4j/neo4j` password replaced via `NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}` env var. `NEO4J_PASSWORD` must be ≥ 32 chars. Set in Coolify secrets, never in git.
- [ ] **Memory limits**: `NEO4J_server_memory_heap_initial__size=512m` and `NEO4J_server_memory_pagecache_size=512m` set in compose env. Validated at startup (no OOM restart in 24h soak).
- [ ] **Backup/restore runbook** documented in `PHASE10_ROLLOUT.md`: `neo4j-admin database dump neo4j --to-path=/backup/` + S3 sync daily. Restore path tested in staging.
- [ ] **Monitoring**: Prometheus exporter sidecar OR `/db/manage/server/jmx/...` scrape configured. Alert on `neo4j_process_health != 'available'`.
- [ ] **Version upgrade policy**: Neo4j 5.x → 5.x+1 migration path documented (Community Edition uses offline dump/load; no rolling upgrade).
- [ ] **APOC plugin decision**: Either include `neo4j-apoc-core` plugin for advanced Cypher procedures, or document explicit non-use. Phase 10 base queries do not require APOC; future phases may.
- [ ] **Bolt TLS**: In production, `NEO4J_dbms_ssl_policy_bolt_enabled=true` with self-signed cert OR mTLS. Not required for dev.

### Rollout Sequence

1. Complete Neo4j Operational Readiness Checklist above.
2. Deploy with `memory.graph.projection.enabled = OFF`, `memory.graph.query.enabled = OFF`, `memory.graph.write_pending.paused = OFF`.
3. Start Neo4j service on VPS (`docker compose --profile graph up -d neo4j`). Verify health via `/db/manage/server/lifecycle`.
4. Run `/graph_project_now dry_run` — verify source counts and cost estimate.
5. Enable `memory.graph.projection.enabled = ON`. Run `/graph_project_now full_rebuild`. Monitor `graph_projection_runs` table.
6. Run `reconcile_counts` via `/graph_stats`. Verify `drift_detected = False`, `pending_purge_count = 0`.
7. Enable `memory.graph.query.enabled = ON`. Test `/graph_query <topic>` with known community topics.
8. Monitor nightly `graph_projection_nightly` cron for 3 fires. Check `/graph_stats` drift flag + pending_purge_count + DLQ count after each.

Rollback: set both flags OFF, stop neo4j service. Postgres source tables and tombstones untouched. `graph_purge_pending` rows accumulate (harmless when query is disabled).

---

## §13. Open Questions — All Answered at Ratification

| Q | Question | Decision |
|---|---|---|
| Q1 | Graph store: Neo4j vs Apache AGE vs Graphiti vs NetworkX? | **Neo4j 5.x Community.** RFC-001 §6 REVISED: AGE P50 traversal 8,957ms vs Neo4j 2.554ms (~3,500× slower). Flip condition (P95 > 500ms) exceeded by factor of 28×. AGE traversal latency is structural (table scan, no index), not configurable. Neo4j wins. NetworkX retained as dev/unit-test fallback only. |
| Q2 | Hosting model? | `neo4j` service in `docker-compose.yml` under `profiles: [graph]`. Dev activation: `docker compose --profile graph up`. Prod compose in PHASE10_ROLLOUT.md. Bolt 7687, HTTP 7474, volume `neo4j_data`. |
| Q3 | Prompt design? | Typed JSON schema (see §5.B). Source-id preservation mandatory. Temperature 0.1. Max 5 triples per source. Refuse if `subject_label == "UNKNOWN"` or `object_label == "UNKNOWN"`. |
| Q4 | Cadence? | Nightly batch projection at 03:30 MSK. Real-time hooks deferred to Phase 10.5. |
| Q5/Q7 | Privacy + cascade timing? | **Async purge worker** (RFC-001:415 compliant). `_cascade_graph_provenance` atomically enqueues `graph_purge_pending` rows in Postgres (same transaction as cascade). `graph_purge_worker` drives Neo4j bolt DELETE asynchronously. `graph_query.py` fails-closed via pending-purge read-block during async window. DECISION REVISED from original "synchronous purge" per Codex audit BLOCKER A (see §13.5). |
| Q6 | Source contract? | `knowledge_cards` (approved only, via `card_status='approved'`) + `message_versions` (governance-filtered). Observations table never shipped. Digests are NOT projection sources (invariant #5). |
| Q8 | Shared node semantics? | Node deleted iff ALL supporting `graph_provenance` rows purged. Otherwise only the source-specific edge is detached and the provenance_id removed from node's array. |

**RFC-001 conclusions (key numbers cited from §4):**
- AGE 3-hop traversal P50: **8,957 ms** (n=5, anomaly flagged); Neo4j P50: **2.554 ms** (n=1,000).
- AGE forget cascade: **150.9 ms/source** (n=50); Neo4j: **37.4 ms/source** (n=1,000).
- Recommendation FLIPPED from Apache AGE to **Neo4j Community** on 2026-05-11.

### §13.5 RFC-001 Condition Satisfaction (Codex Audit Revision 2026-05-16)

RFC-001 §6 (line 415) approved Neo4j Community **conditionally** on async cascade worker + read-block during purge window. This plan satisfies each condition:

| RFC-001 Condition | How This Plan Satisfies It |
|---|---|
| "PostgreSQL tombstone commit writes to a purge queue table" (line 419) | `_cascade_graph_provenance` enqueues `graph_purge_pending` rows atomically in the same Postgres cascade transaction (§5.F). |
| "background worker reads the queue and issues bolt DELETE queries to Neo4j" (line 420) | `graph_purge_worker` (extension of `cascade_worker_tick`) consumes `graph_purge_pending` rows and calls `neo4j_adapter.purge_provenance()` (§5.F, §5.A migration 063). |
| "graph queries issued during pending purge are blocked or annotated" (line 421) | `graph_query.py` pending-purge read-block: checks `graph_purge_pending WHERE graph_node_key = ANY(:candidates) AND purged_at IS NULL` before any Cypher traversal; returns `abstained=True` if any pending row found (§5.E, §5.A migration 063). |
| "bounded eventual consistency window (seconds) is acceptable" (line 422) | Worker runs as extension of `cascade_worker_tick`; expected purge latency = seconds under normal Neo4j health. DLQ + alert on repeated failure. |
| "formalized in T10-04" (line 423) | Formalized in T10-06 (cascade) + T10-05 (graph_query read-block) as per revised sprint plan. |

---

## §14. Risk Register

| Risk | Phase 10 mitigation |
|---|---|
| **GAP A — no `card_relations` table in Phase 6** | Phase 10 owns `graph_edges` table (migration 062). Edges derived from triple extraction over `card.body_markdown` via `extract_graph_triples`. No dependency on Phase 6 amendment. |
| **GAP B — observations not triple-shaped** | Observations never shipped as standalone table (Phase 8 = weekly digest only). Phase 10 does NOT project observations. Deferred to future phase. |
| **GAP C — no `visibility_scope` column in knowledge_cards** | Graph visibility derived from source `chat_messages.memory_policy` + `message_versions.is_redacted` of cited message_version_ids. If any source is forbidden → card excluded from projection. |
| **Neo4j JVM memory** | Default Neo4j Community heap is 512MB. For 50k triples this is adequate. Config `NEO4J_server_memory_heap_initial__size=512m` in compose. Monitor OOM via `/graph_stats` run failures. |
| **Neo4j bolt connection pool exhaustion** | Pool size 10 (configurable). Projection is single-process nightly; cascade is single advisory-lock event. No concurrent bolt saturation expected. |
| **Neo4j backup/restore lag** | Neo4j backup separate from Postgres `pg_dump`. Operator runbook must include daily `neo4j-admin dump` + verification. Postgres provenance is the source of truth — Neo4j can always be rebuilt. |
| **graph_purge_worker bolt failure leaves ghost provenance** | `I8c` binding test covers this. Mitigation: cascade Postgres commit succeeds (graph_provenance rows soft-deleted, graph_purge_pending enqueued). Graph query fails-closed automatically via pending-purge read-block. Manual recovery: fix Neo4j bolt connection, restart worker (retries idempotently from `failed_at IS NULL` rows). Full rebuild unnecessary unless DLQ accumulates significantly. |
| **Phase 9 migration window collision** | Phase 9 consumes 050-059. Phase 10 starts at 060. ORCHESTRATOR_REGISTRY.md §2 Orch B reservation enforces this. Verify no 050-059 migration exists before creating 060. |
| **`lint_privacy_check.sh` rebase fragility** | Per `feedback-lint-privacy-rebase-fragility.md`: script uses path:line:content baseline. Graph service files may shift baselines. Allowlist expansion may be needed in T10-04/T10-06 PRs. |
| **Draft HANDOFF.md §6 `add_graph_sync_runs` naming** (MEDIUM I) | HANDOFF.md §6 lists `add_graph_sync_runs` as Phase 10 migration name. Actual names are `add_graph_projection_runs` (060), `add_graph_provenance` (061), `add_graph_edges` (062), `add_graph_purge_pending` (063), `add_llm_ledger_call_type` (064) — 5 migrations total (vs HANDOFF §6 singular). Follow-up shared-file PR at Phase 10 closure reconciles HANDOFF.md §6 + ADR refs. Do not block implementation on it. |
| **R7.a member graph deferred** (MEDIUM H) | Non-admin `/graph_query` refusal (R7.a) is a Phase 10 stance. Future butler/member graph access requires a separate phase with role filters, evidence-context mediation, and visibility scoping. Document in PHASE10_ROLLOUT.md under "future phases" section so the scope boundary is explicit at handoff. |
| **CI has no Neo4j service** (HIGH K) | `ci.yml` and `evals.yml` currently have Postgres service only. T10-09 adds `pytest-testcontainers[neo4j]` for integration tests tagged `@pytest.mark.neo4j`. `evals.yml` adds Neo4j service block when `EVAL_HARNESS_ENABLED=true`. Unit tests use `NetworkX` in-memory fake via `bot/services/graph_adapter_networkx.py` test double (tagged `@pytest.mark.graph_unit`). |

---

## §15. Environment Variables Introduced

| Var | Default | Purpose |
|---|---|---|
| `NEO4J_BOLT_URI` | `bolt://neo4j:7687` | Bolt connection URI for Neo4j adapter |
| `NEO4J_AUTH_USER` | `neo4j` | Neo4j auth username |
| `NEO4J_AUTH_PASSWORD` | `password` | Neo4j auth password — **min 32 chars enforced in prod** (see §12 checklist). In compose: `NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}`. |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database name |
| `GRAPH_PROJECTION_ENABLED` | `false` | Gates scheduler registration |
| `GRAPH_PROJECTION_HOUR_MSK` | `3` | Cron fire hour (Europe/Moscow) |
| `GRAPH_PROJECTION_MINUTE_MSK` | `30` | Cron fire minute |
| `GRAPH_PROJECTION_MAX_SOURCES_PER_RUN` | `200` | Per-run source row cap (graph-specific; lower than shared 1000 to contain first-run cost) |
| `GRAPH_PROJECTION_MAX_TOKENS_PER_SOURCE` | `2000` | Per-source input truncation before LLM dispatch |
| `GRAPH_PROJECTION_MAX_TRIPLES_PER_SOURCE` | `5` | Per-source triple cap passed to gateway |
| `GRAPH_PROJECTION_DAILY_USD_CEILING` | `2.00` | Graph-only daily LLM budget (separate from shared `LLM_DAILY_USD_CEILING` $5). Enforced via `llm_usage_ledger` filtered by `call_type='graph_projection'`. |
| `GRAPH_PROJECTION_RUN_USD_CEILING` | `0.50` | Per-run abort ceiling. Dry-run cost estimate computed BEFORE any provider call; aborts if estimate exceeds this. |
| `GRAPH_PROJECTION_MONTHLY_USD_CEILING` | `20.00` | Monthly LLM cost ceiling (separate graph bucket) |

Settings registered in `bot/config.py` following the same env-var parsing pattern as `DIGEST_*` vars.

---

## §16. Codex Audit Revision Log (2026-05-16)

Applied to PHASE10_PLAN.md as revision following Codex independent technical audit.
Source: `.superflow/p9_p10_ratification/codex_p10_audit.md`.

| Finding | Severity | Change | Section(s) |
|---|---|---|---|
| **BLOCKER A** — sync purge violates RFC-001 conditional approval; split-brain risk | BLOCKER | DECISION FLIP: replaced synchronous bolt call in cascade with async `graph_purge_pending` queue + `graph_purge_worker`. Added `graph_query.py` pending-purge read-block (fail-closed). Added migration 063 `graph_purge_pending`. | §3, §5.A, §5.D, §5.E, §5.F, §5.G, §5.I, §8, §11b glossary, §12, §13, §14, ratification table |
| **BLOCKER J** — graph extraction could exhaust shared LLM daily bucket | BLOCKER | Added separate `GRAPH_PROJECTION_DAILY_USD_CEILING` ($2/day, `call_type='graph_projection'`-filtered) + `GRAPH_PROJECTION_RUN_USD_CEILING` ($0.50, dry-run estimate before any provider call). Added migration 064 `add_llm_ledger_call_type`. | §3, §5.A, §5.B, §5.C, §13, §15 |
| **HIGH B** — Neo4j has no prod readiness gate; flag ≠ readiness | HIGH | Added Neo4j Operational Readiness Checklist to §12. Distinguished flag (rollout control) from checklist (ops readiness). Added to §4 Non-Goals. | §4, §5.D, §12, T10-01 AC |
| **HIGH C** — FK CASCADE won't fire for archived cards / redacted message_versions | HIGH | Renamed cascade function to `_cascade_graph_provenance`. Documented why FK ON DELETE CASCADE is only safety net. Added explicit application-code query by `source_table`/`source_pk`. Updated §5.A schema note. | §3, §5.A, §5.F, §6, T10-06 |
| **HIGH D** — LLM ledger has no `call_type` discriminator; entity resolution contract undefined | HIGH | Added `call_type='graph_projection'` to ledger placeholder writes (migration 064). Added entity resolution contract: card-title match → user.id match → `UNKNOWN_*` → drop triple. Added to `extract_graph_triples` signature and AC. | §5.A, §5.B, T10-03 AC |
| **HIGH E** — double-counting: cards AND message_versions both as semantic sources | HIGH | Ontology split: cards → semantic CONCEPT nodes + LLM triples; message_versions → provenance event nodes only (no LLM extraction). Updated §2 objective, §5.C projector source selection. | §2, §5.C, §13 Q6 |
| **HIGH G** — I8d exact-equality determinism invalid for LLM at temp 0.1 | HIGH | I8d revised: cold rebuild = REPLAY stored `graph_edges` (no LLM). New I8e: re-extraction Jaccard ≥90% overlap. Each `graph_edges` row stores `prompt_version`, `model`, `source_content_hash`, `triple_hash`, `extraction_run_id`. | §5.I, §10, T10-09 AC |
| **HIGH K** — CI has no Neo4j service; no test adapter protocol | HIGH | Added §11 Test Infrastructure: NetworkX fake (`@pytest.mark.graph_unit`), testcontainers Neo4j (`@pytest.mark.neo4j`). Added `pyproject.toml` deps. Added `evals.yml` Neo4j service block. | §11 (new), §10, T10-09 files |
| **HIGH L** — RFC-001 compliance undocumented | HIGH | Added §13.5 RFC-001 Condition Satisfaction table. Added reference in §5.F: "implements RFC-001 conditional Neo4j approval per RFC-001:415". | §5.F, §13.5 (new) |
| **MEDIUM F** — drift detection algorithm underspecified | MEDIUM | Concrete algorithm: `postgres_active_hash` (md5 over sorted triple_hash) vs `neo4j_edge_hash` (sum of `edge_key_hash` on relationships). Neo4j orphan + missing detection. `edge_key_hash` (crc32 signed int64) stored at MERGE time. | §5.D, §5.I |
| **MEDIUM H** — R7.a scope (non-admin refusal) not documented as Phase 10 stance | MEDIUM | Added note to T10-08, §4 Non-Goals, §14 Risk Register: R7.a is Phase 10 stance; member/butler graph access is a separate future phase. | §4, §14, T10-08 |
| **MEDIUM I** — HANDOFF.md naming drift (singular `add_graph_sync_runs` vs 5 tables) | MEDIUM | Added to §14 Risk Register: 5 migrations total (060-064) vs HANDOFF singular. Closure PR must reconcile. | §14 |

**Counts updated by revision:**
- Migrations: 3 → 5 (added 063 `graph_purge_pending`, 064 `add_llm_ledger_call_type`)
- Feature flags: 2 → 3 (added `memory.graph.write_pending.paused`)
- Phase 11 test cases: 54 → 57 (added I8e Jaccard, R7.d pending-purge read-block, G2 hash sub-case)
- Env vars: 11 → 13 (added `GRAPH_PROJECTION_DAILY_USD_CEILING`, `GRAPH_PROJECTION_RUN_USD_CEILING`, `GRAPH_PROJECTION_MAX_TOKENS_PER_SOURCE`; renamed `GRAPH_PROJECTION_USD_CEILING` → `GRAPH_PROJECTION_RUN_USD_CEILING`)

---

## §17. Sprint 0 Deliverable (T10-S0)

This document is the deliverable. To complete Sprint 0, the PR must:

1. Add this `PHASE10_PLAN.md` to `docs/memory-system/`.
2. Update `AUTHORIZED_SCOPE.md`:
   - **Remove** the "conditionally authorized" bullet for Phase 10 from `## Conditionally authorized: Phase 9, Phase 10 (gated)`.
   - **Insert** new `## Authorized: Phase 10 — Graph Projection / Neo4j (2026-05-16)` block immediately before `## NOT authorized (future phases — gates not passed)`. Block content: TL;DR (Neo4j 5.x community, async nightly batch projection, 5 Postgres tables/columns, 3 feature flags), owner (Orchestrator B), scope reference to this `PHASE10_PLAN.md`, NOT-in-scope list mirroring §4, ratification date.
3. No code changes, no migrations.
