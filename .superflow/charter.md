# Phase 10 Execution Charter

**Topic:** Phase 10 — Graph Projection / Neo4j (Graphiti-style)
**Goal:** Close Phase 10 — all 8 remaining sprints (T10-02..T10-09) merged, Neo4j CI service running, dev mode verified, ready for production testing.
**Authorized by:** PHASE10_PLAN.md ratified 2026-05-17 (2 BLOCKER + 9 HIGH + 4 MEDIUM addressed in revision passes). User /superflow invocation 2026-05-20.

## Governance
- **Mode:** standard
- **Git workflow:** sprint_pr_queue (one PR per sprint, rebase merge, CI green before merge)
- **Secondary provider:** codex (via `codex:codex-rescue` subagent — raw `codex` CLI broken in non-interactive shells per `~/.claude/rules/codex-routing.md`)
- **Unified Review per PR:** Claude standard-product-reviewer + Codex deep-technical via codex-rescue
- **Docs update gate per PR:** CLAUDE.md / llms.txt explicit check + per-PR rollout fragment
- **`.par-evidence.json` required before push:** review verdicts + docs verdict
- **NEVER `gh pr merge --admin`:** fix CI red first

## Wave Plan

```
WAVE 0 (3 PRs):
  W0-A (BLOCKING, sequential first) — feat/p10-w0a-foundation
    Migration 060 (graph_projection_runs) per PHASE10_PLAN section 5.A
    bot/services/graph_common.py: GraphNodeRef, GraphEdgeRef, RefusalError,
      GraphProjectionPolicyError, ALLOWED_NODE_TYPES, ALLOWED_PREDICATES,
      RESERVED ledger.call_type Literal values, repair_mode contract
    bot/db/repos/graph_projection_run.py: create_run, update_run_stats,
      finalize_run, list_recent_runs, get_active_run
    docs/rollout-fragments/phase10/W0-A.md (per-PR rollout fragment)
    Tests: unit tests for repo CRUD + dataclass freezing/validation
  
  After W0-A merged - dispatch in parallel:
  
  W0-B - feat/p10-w0b-eligibility (T10-02)
    bot/services/graph_source_eligibility.py: governance-aware source filter
    Reuses bot/services/governance.py forget_excludes predicate
    Pure code, no migrations. Imports from graph_common.py
    Tests: positive (eligible source) + negative (all 3 governance-excluded categories per governance.py contract)
  
  W0-D - feat/p10-w0d-neo4j-ci (NEW per duo iter 2)
    .github/workflows/evals.yml: Neo4j 5.x service block + healthcheck
    .github/workflows/ci.yml: optional Neo4j integration job
    docker-compose-test.yml: Neo4j --profile graph for local
    tests/conftest_neo4j.py: pytest fixture neo4j_session (cleaned per test)
    Tests: verifies fixture connects + cleans state between tests

WAVE 1 (2 PRs, sequential):
  W1-C first - migrations 061-064 + repos
    Migration 061: graph_provenance per section 5.A
    Migration 062: graph_edges per section 5.A
    Migration 063: graph_purge_pending per section 5.A
    Migration 064: ledger.call_type ALTER + backfill
    bot/db/repos/graph_provenance.py, graph_edge.py, graph_purge_pending.py
  
  W1-B after W1-C merged - T10-04 LLM extract
    bot/services/llm_gateway.py: extract_graph_triples per section 5.B
    Entity registry resolution INLINE (cards.id + users.id + UNKNOWN sentinel)
    Real Neo4j binding tests via W0-D fixture

WAVE 2 (2 PRs, parallel):
  W2-B - T10-05 graph_projector modes
    bot/services/graph_projector.py per section 5.C
    pg_advisory_lock(GRAPH_REBUILD_LOCK) for full_rebuild — prevents race with
      W2-C purge worker
    Pre-condition: SELECT count from graph_purge_pending status='in_flight' = 0
  
  W2-C UNIFIED - T10-06 cascade + worker + readblock
    bot/services/forget_cascade.py: _cascade_graph_provenance layer
    bot/services/graph_purge_worker.py: drives Neo4j DETACH DELETE
    bot/services/graph_purge_readblock.py: assert_no_pending_purge(node_keys)
      (NO stub-in-main pattern — full impl in single PR per duo iter 2 critique)

WAVE 2.5 (1 PR):
  W2.5-D after W2-C merged - T10-07 graph_query
    bot/services/graph_query.py per section 5.E
    Role/visibility filters, parameterized Cypher, provenance-required output
    Imports assert_no_pending_purge from W2-C

WAVE 3 (2 PRs, parallel):
  W3-E - T10-08 admin handlers + cron + ROLLOUT
    bot/handlers/admin_graph.py: /graph_project_now (advisory lock) +
      /graph_stats + /graph_query
    bot/scheduler.py: graph_projection_nightly_job at 03:30 MSK
    docs/memory-system/PHASE10_ROLLOUT.md: ASSEMBLED from
      docs/rollout-fragments/phase10/*.md via concat script
  
  W3-F - T10-09 cross-component binding tests
    tests/evals/test_graph_*.py: 5 cross-component scenarios
    Phase 11 binding count: 60/60 -> 75-76/75-76
```

## Coordination Rules (anti-conflict)

| Rule | Detail |
|---|---|
| Max parallel streams | 2 active per wave (3 only in W0 since helpers are tiny) |
| Migration numbers | Reserved 060-064 (W0/W1) per canonical PHASE10_PLAN. No reordering. |
| `bot/services/graph_query.py` ownership | Stream Query (W2.5-D) |
| `bot/services/graph_purge_readblock.py` ownership | Stream Privacy (W2-C) |
| `bot/services/forget_cascade.py` ownership | Stream Privacy (W2-C edits _cascade_graph_provenance) |
| `bot/services/llm_gateway.py` ownership | Stream Projection (W1-B adds extract_graph_triples) |
| Per-PR rollout fragments | `docs/rollout-fragments/phase10/<sprint-id>.md` — append-only, no merge conflict |
| PR >24h stale | Mandatory rebase before review |
| After merge | Rebase signal to other worktrees |
| Race protection | `pg_advisory_lock(GRAPH_REBUILD_LOCK)` in W2-B + precondition check on pending purges |

## Hard Constraints (must not violate)

1. RFC-001:415 async cascade pattern — Postgres enqueue + separate worker + read-block fail-closed
2. 3 feature flags default OFF: `memory.graph.projection.enabled`, `memory.graph.query.enabled`, `memory.graph.write_pending.paused`
3. Cost ceilings separate from shared LLM_DAILY_USD_CEILING:
   - `GRAPH_PROJECTION_DAILY_USD_CEILING` $2/day
   - `GRAPH_PROJECTION_RUN_USD_CEILING` $0.50/run
   - Max 200 sources/run
4. replay-only `full_rebuild` (no LLM re-extraction)
5. Ontology split: `knowledge_cards` -> CONCEPT nodes + LLM triples; `message_versions` -> provenance/event nodes only

## Definition of Done (Phase 10 closure)

1. All 9 PRs merged to main, CI green throughout
2. Phase 11 binding tests: 60/60 -> 75-76/75-76
3. Neo4j CI service runs in nightly `evals.yml`
4. PHASE10_ROLLOUT.md complete and committed
5. docker-compose --profile graph spins up cleanly in dev (manual verify)
6. All 3 feature flags exist in `.env.example` with default OFF
7. CLAUDE.md + ROADMAP.md + IMPLEMENTATION_STATUS.md updated to "Phase 10 CLOSED"
8. Cleanup: stale worktrees removed (needs user permission)
