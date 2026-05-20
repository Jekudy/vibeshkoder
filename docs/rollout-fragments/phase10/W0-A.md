# Rollout Fragment: W0-A (Phase 10 Foundation)

## Summary

Sprint W0-A establishes the Postgres-side foundation for Phase 10 graph projection:

- Migration 060: `graph_projection_runs` table — audit anchor for every projector run
- ORM model `GraphProjectionRun` in `bot/db/models.py`
- Repo `bot/db/repos/graph_projection_run.py` — CRUD for projection runs
- `bot/services/graph_common.py` — shared constants, dataclasses, error hierarchy, Protocol

No feature flags are toggled in this sprint. All Phase 10 feature flags default OFF and are
defined in later sprints (W1-B / W3-E introduce the three env-var feature flags).

## Env Vars

None added in this sprint.

## Migration Applied

| Migration | Table | Action |
|-----------|-------|--------|
| 060 | `graph_projection_runs` | CREATE TABLE |

down_revision = 055 (chain: 055 -> 060)

## Tables Created

### `graph_projection_runs`

One row per graph projector invocation. Columns:

| Column | Type | Notes |
|--------|------|-------|
| id | BIGSERIAL PK | |
| mode | TEXT NOT NULL | CHECK: dry_run / incremental / full_rebuild / repair |
| status | TEXT NOT NULL | CHECK: running / completed / failed / cancelled / cost_exceeded / dry_run_complete |
| source_cutoff_at | TIMESTAMPTZ | nullable — set when incremental watermark is known |
| source_card_count | INTEGER | count of knowledge_cards sources processed |
| source_message_version_count | INTEGER | count of message_versions sources processed |
| projected_node_count | INTEGER | Neo4j nodes written |
| projected_edge_count | INTEGER | Neo4j edges written |
| skipped_policy_count | INTEGER | sources skipped due to governance policy |
| skipped_budget_count | INTEGER | sources skipped due to cost ceiling |
| llm_prompt_tokens | INTEGER | aggregated prompt tokens across all LLM calls in run |
| llm_completion_tokens | INTEGER | aggregated completion tokens |
| estimated_cost_usd | NUMERIC(10,6) | pre-run cost estimate |
| actual_cost_usd | NUMERIC(10,6) | post-run actual cost from ledger |
| started_at | TIMESTAMPTZ NOT NULL | server default now() |
| finished_at | TIMESTAMPTZ | null while running; set by finalize_run |
| error_code | TEXT | structured error code on failure |
| error_context | TEXT | human-readable error detail |
| started_by | TEXT | free-text label (e.g. 'scheduler', 'admin:149820031') |
| created_at | TIMESTAMPTZ NOT NULL | server default now() |

Indexes:
- `ix_graph_projection_runs_started_at` on `started_at DESC`
- `ix_graph_projection_runs_status` on `status` WHERE status IN ('running', 'failed')

## Coordination Notes for Next Streams

**Unblocked after W0-A merge:**

- W0-B (`feat/p10-w0b-eligibility`): `graph_source_eligibility.py` imports from
  `bot/services/graph_common.py` (ALLOWED_NODE_TYPES, RefusalError). Can start
  immediately after merge.

- W0-D (`feat/p10-w0d-neo4j-ci`): Neo4j CI service, docker-compose profile, and
  `conftest_neo4j.py` fixture. No dependencies on W0-A schema — can start in parallel
  after W0-A merge.

**Sequential after W0-A (Wave 1):**

- W1-C must add migrations 061-064 sequentially after 060. The `down_revision` chain is:
  - 061 (graph_provenance) references `graph_projection_runs.id` via FK
  - 062 (graph_edges) references `graph_provenance.id` via FK
  - 063 (graph_purge_pending) references `graph_provenance.id` via FK
  - 064 (llm_usage_ledger.call_type) — independent ALTER; no FK to above tables

- W1-B (extract_graph_triples) waits for W1-C migrations and can reference
  `RESERVED_LEDGER_CALL_TYPES` from `graph_common.py` for the `call_type` values.

**Reserved ledger.call_type values (documented in graph_common.py):**

Migration 064 (W1-C) will add `call_type VARCHAR(32) NOT NULL DEFAULT 'unknown'` to
`llm_usage_ledger`. Per PHASE10_PLAN §5.B step 4, all Phase 10 graph projection LLM
calls use a SINGLE canonical call_type value:

- `graph_projection` — every `extract_graph_triples` LLM call across all projector modes

Mode discrimination (dry_run / incremental / full_rebuild / repair) is captured by the
`graph_projection_runs.mode` column (CHECK-constrained), NOT by ledger.call_type
subdivision. The §5.A daily-cost ceiling SQL filters `WHERE call_type = 'graph_projection'`
unchanged.

See `bot/services/graph_common.py::RESERVED_LEDGER_CALL_TYPES` for the authoritative
single-value tuple.

## Docs Touched

- `CLAUDE.md` — UNCHANGED (W0-A is infrastructure-only; Phase 10 progress is tracked in IMPLEMENTATION_STATUS.md and will roll into CLAUDE.md "Memory System Cycle" only at Phase 10 closure)
- `llms.txt` — N/A (file does not exist in repo)
- `docs/memory-system/PHASE10_PLAN.md` — UPDATED §5.A with `started_by` implementation note (MEDIUM-1 fix)
- `docs/memory-system/IMPLEMENTATION_STATUS.md` — not yet updated; will be added at W3-E closure assembly
