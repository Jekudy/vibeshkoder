# T10-02-rest Rollout Fragment

## Summary

Adds migrations 061 (graph_provenance) and 062 (graph_edges), their ORM
models, and repository layers. These tables complete the Postgres-side graph
registry needed by Phase 10 Neo4j projection and forget cascade.

## Migrations Applied

| Revision | File | Tables Created |
|----------|------|----------------|
| 061 | alembic/versions/061_add_graph_provenance.py | graph_provenance |
| 062 | alembic/versions/062_add_graph_edges.py | graph_edges |

Migration chain: 055 -> 060 -> 061 -> 062 (head)

## Tables Created

### graph_provenance (migration 061)

Maps each projected source (message_version or knowledge_card) to the Neo4j
graph. One row per projected triple.

- source_table / source_pk: logical refs for forget cascade lookups (NOT typed FKs)
- FK ON DELETE CASCADE on source_card_id / source_message_version_id: safety net only
- CHECK constraints: source_table IN (...), has_source (OR, not XOR), graph_store IN (...)
- Indexes: ix_mvid (partial), ix_card_id (partial), ix_active (partial WHERE purged_at IS NULL),
  uq_triple (unique partial WHERE purged_at IS NULL)

### graph_edges (migration 062)

Postgres-side edge registry for idempotency and drift detection.

- FK to graph_provenance.id with ON DELETE CASCADE
- predicate CHECK constraint: must be in ALLOWED_PREDICATES (12 values)
- confidence_score CHECK: [0.00, 1.00]
- Indexes: ix_active (partial WHERE purged_at IS NULL), uq_key (unique partial)

## New Files

| File | Purpose |
|------|---------|
| alembic/versions/061_add_graph_provenance.py | Migration: graph_provenance |
| alembic/versions/062_add_graph_edges.py | Migration: graph_edges |
| bot/db/repos/graph_provenance.py | Repo: create, mark_inactive, find_by_source, find_active |
| bot/db/repos/graph_edge.py | Repo: create_edge, find_by_provenance, count_for_drift_check |
| tests/db/test_graph_provenance_repo.py | Integration tests for graph_provenance repo |
| tests/db/test_graph_edge_repo.py | Integration tests for graph_edge repo |

## Modified Files

| File | Change |
|------|--------|
| bot/db/models.py | Added GraphProvenance and GraphEdge ORM models |
| tests/db/test_digests_review_schema.py | Updated alembic head assertion from "060" to "062" |

## Coordination Notes

- Unblocks T10-03 (extract_graph_triples in llm_gateway.py writes to graph_provenance)
- Unblocks T10-06 (forget_cascade._cascade_graph_provenance queries graph_provenance
  by source_table/source_pk and soft-deletes via mark_inactive; enqueues graph_purge_pending)
- T10-08 drift reconcile uses count_for_drift_check(session) to compare Postgres edge
  count vs Neo4j edge count

## Scope NOT in This PR

- Migration 063 (graph_purge_pending): T10-06 territory
- Migration 064 (llm_usage_ledger.call_type): T10-03 territory
- bot/services/graph_adapter.py: T10-01 territory
- bot/services/forget_cascade.py _cascade_graph_provenance: T10-06 territory

## Phase 10.5 Carryovers

- Tighten `ck_graph_provenance_has_source` from OR to XOR (spec §5.A amendment). Code-level validation in `create_provenance` enforces source_table↔FK consistency (FIX-1 in W0-A review pass), but the DB-level CHECK remains OR per current spec verbatim. Track for Phase 10.5 if XOR enforcement at DB layer is desired.

## Docs Touched

- docs/rollout-fragments/phase10/T10-02-rest.md (this file, new)
