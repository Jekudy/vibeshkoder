# Phase 10 — Graph Projection / Neo4j: Operator Rollout Checklist

**Status:** Phase 10 CLOSED 2026-05-21. All feature flags default OFF — production rollout below.

This document is the operator playbook for enabling Phase 10 graph projection
on production. Phase 10 ships DARK (all three flags default OFF). Follow this
checklist in order; do NOT skip steps. Neo4j must be operational and all
migrations applied before flipping any flag.

## §1 Scope Summary

Phase 10 adds a knowledge graph projection layer to the memory system. Approved
`knowledge_cards` are projected via LLM triple extraction into semantic CONCEPT
nodes and typed GRAPH_EDGE relationships in Neo4j 5.x Community Edition.
`message_versions` produce event/provenance nodes only (no LLM extraction —
avoids double-counting with cards).

The privacy-critical invariant is async cascade purge: when a forget event fires,
`forget_cascade._cascade_graph_provenance` atomically enqueues
`graph_purge_pending` rows in the same Postgres transaction. A separate
`graph_purge_worker` drives Neo4j bolt DELETE asynchronously. Graph queries fail
closed (`abstained=True`) via a pending-purge read-block during the async window.
This design follows RFC-001:415 conditional Neo4j approval — the earlier
synchronous-purge proposal was rejected because it violated the RFC condition.

## §2 Sprint Summary Table

| Sprint | PR | Description |
|---|---|---|
| W0-A | #324 | Postgres foundation: migration 060 (`graph_projection_runs`), ORM model, repo, `graph_common.py` shared types/errors |
| W0-D | #325 | Neo4j CI service: GitHub Actions service container, `neo4j_session` fixture with host-allowlist guard, `graph_integration` pytest marker |
| T10-02-rest | #326 | Migrations 061 (`graph_provenance`) + 062 (`graph_edges`), ORM models, repos |
| T10-03 | #327 | Migration 064 (`llm_usage_ledger.call_type` ALTER + backfill), `extract_graph_triples` gateway function, `graph_triples_v0_1_0` prompt template |
| T10-04 | #328 | `graph_projector.py` — 4 modes: dry_run, incremental, full_rebuild (replay-only), repair; `GraphProjectorConfig`; advisory lock `GRAPH_REBUILD_LOCK_ID`; `memory.graph.projection.enabled` flag |
| T10-06 | #329 | Migration 063 (`graph_purge_pending`), `graph_purge_readblock.py`, `graph_purge_worker.py`, `forget_cascade._cascade_graph_provenance`, `memory.graph.write_pending.paused` kill-switch |
| T10-05 | #330 | `graph_query.py` read-only traversal API — 5 functions, RFC-001:415 read-block integration, `memory.graph.query.enabled` flag |
| T10-08 | #331 | Drift detection: `reconcile_counts()`, `GraphDriftReport`, extended `graph_stats()`, `count_nodes()`/`count_edges()` on adapters |
| T10-07 | #332 | Admin Telegram handlers (`/graph_project_now`, `/graph_stats`, `/graph_query`, `/graph_purge_now`), scheduler jobs (nightly 03:30 MSK + 5-min purge worker) |
| T10-09 | #333 | Phase 11 binding suite: 60 → 77 tests (L10a-c + C9a-b + I8a-e + R7a-d + G2a-b + replay-only invariant) |

## §3 Migrations Applied

| Revision | File | Change |
|---|---|---|
| 060 | `060_add_graph_projection_runs.py` | CREATE TABLE `graph_projection_runs` |
| 061 | `061_add_graph_provenance.py` | CREATE TABLE `graph_provenance` |
| 062 | `062_add_graph_edges.py` | CREATE TABLE `graph_edges` |
| 063 | `063_add_graph_purge_pending.py` | CREATE TABLE `graph_purge_pending` (non-contiguous: `down_revision="064"`) |
| 064 | `064_add_llm_ledger_call_type.py` | ALTER TABLE `llm_usage_ledger` ADD COLUMN `call_type VARCHAR(32) NOT NULL DEFAULT 'unknown'` + backfill + composite index |
| 065 | (migration 065, graph_edges projection index) | See T10-06 |
| 066 | (migration 066, graph_query read-index) | See T10-05 |

Migration chain after Phase 10: `055 → 060 → 061 → 062 → 064 → 063` (Alembic
supports non-contiguous revision IDs; chain is linear, no branching).

Verify: `alembic current` must return `063` (or later).

## §4 Feature Flags Introduced (all default OFF)

| Flag | Default | Description |
|---|---|---|
| `memory.graph.projection.enabled` | OFF | Enables `project_incremental`, `project_full_rebuild`, `project_repair` modes. `dry_run` always available regardless. |
| `memory.graph.query.enabled` | OFF | Enables `find_related_topics`, `find_people_for_topic`, `explain_connection` traversal functions. |
| `memory.graph.write_pending.paused` | OFF | Kill-switch for the purge worker. When ON: `graph_purge_worker_tick` returns immediately; graph queries remain fail-closed on any pending purge row. |

## §5 Cost Ceilings

| Ceiling | Value | Scope |
|---|---|---|
| `GRAPH_PROJECTION_DAILY_USD_CEILING` | $2.00/day | Per-calendar-day LLM spend on `call_type='graph_projection'` |
| `GRAPH_PROJECTION_RUN_USD_CEILING` | $0.50/run | Per-run abort trigger |
| `GRAPH_PROJECTION_MAX_SOURCES_DEFAULT` | 200 | Max sources per projector run |

Separate from the shared `LLM_DAILY_USD_CEILING` ($5/day). Budget exceeded → run
finalized with `status='cost_exceeded'`, raises `GraphProjectionBudgetError`.

## §6 Env Vars Added

| Variable | Example | Description |
|---|---|---|
| `NEO4J_BOLT_URI` | `bolt://localhost:7687` | Neo4j bolt endpoint |
| `NEO4J_AUTH_USER` | `neo4j` | Neo4j username |
| `NEO4J_AUTH_PASSWORD` | (32+ chars min) | Neo4j password. Canonical name; docker-compose falls back to `NEO4J_PASSWORD` for backward compat. |

Add to production secrets (Coolify / GitHub Secrets). Not required while all
feature flags are OFF — but `Neo4jAdapter` will fail on init if `NEO4J_BOLT_URI`
is set with unreachable host. Safest: set vars before flipping flags.

## §7 New Scheduler Jobs

| Job ID | Schedule | Description | Flag Gate |
|---|---|---|---|
| `graph_projection_nightly` | Cron 03:30 MSK daily | Calls `project_incremental(started_by="scheduler")` | `memory.graph.projection.enabled` checked in job body |
| `graph_purge_worker` | Interval every 5 minutes | Calls `graph_purge_worker_tick(batch_size=20)` | `memory.graph.write_pending.paused` kill-switch inside tick |

Both jobs: `max_instances=1`, `coalesce=True`. Projection job has 30-min
`misfire_grace_time` (long runs). All exceptions are caught to prevent
APScheduler from stopping the fire schedule.

## §8 Admin Handlers

All handlers: admin-only (silent no-op for non-admins), `PrivateChatFilter`,
structured audit logging, Telegram-safe output truncation at 4000 chars.

| Command | Description | Flag Gate |
|---|---|---|
| `/graph_project_now [dry_run\|incremental\|full_rebuild\|repair]` | Run projection in specified mode. `dry_run` available when flag is OFF. `full_rebuild` requires `--confirm` token. | `memory.graph.projection.enabled` (not for dry_run) |
| `/graph_stats` | Postgres-canonical counts: active provenance, active edges, purged provenance. Extended with Neo4j node/edge counts and drift signal when adapter is available. | None (always available to admin) |
| `/graph_query <topic>` | Find related nodes via `find_related_topics`. Max 5 hops. | `memory.graph.query.enabled` |
| `/graph_query path <a> <b>` | Explain path between two nodes via `explain_connection`. | `memory.graph.query.enabled` |
| `/graph_purge_now` | Manually trigger `graph_purge_worker_tick` batch (size=20). | Kill-switch is inside the tick itself |

## §9 Rollout Playbook — Operator Steps

1. **Verify Phase 11 binding suite green on main HEAD:**
   ```
   EVAL_HARNESS_ENABLED=1 timeout 300 pytest tests/evals/ -v
   ```
   Should show **77/77 passing** (60 prior + 17 new Phase 10 IDs).

2. **Apply migrations:**
   ```bash
   alembic upgrade head
   ```
   Verify `alembic current` returns `063` (or later). Confirm the 4 new tables:
   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema='public'
     AND table_name IN ('graph_projection_runs','graph_provenance',
                        'graph_edges','graph_purge_pending')
   ORDER BY table_name;
   ```
   Should return 4 rows.

3. **Start Neo4j:**
   ```bash
   docker compose --profile graph up neo4j -d
   ```
   Wait for bolt to be reachable:
   ```bash
   until nc -z localhost 7687; do sleep 2; done && echo "Neo4j ready"
   ```

4. **Set env vars in production** (`NEO4J_BOLT_URI`, `NEO4J_AUTH_USER`,
   `NEO4J_AUTH_PASSWORD`). Restart the bot.

5. **Seed feature flag rows (OFF):**
   ```sql
   INSERT INTO feature_flags (flag_key, scope_type, scope_id, enabled)
   VALUES
     ('memory.graph.projection.enabled', NULL, NULL, FALSE),
     ('memory.graph.query.enabled',      NULL, NULL, FALSE),
     ('memory.graph.write_pending.paused', NULL, NULL, FALSE)
   ON CONFLICT (flag_key, scope_type, scope_id) DO NOTHING;
   ```

6. **Smoke-test dry_run** (safe — no LLM, no Neo4j writes):
   ```
   /graph_project_now dry_run
   ```
   Expected: reply with source counts, estimated cost, status `dry_run_complete`.

7. **Flip `memory.graph.projection.enabled` ON:**
   ```sql
   UPDATE feature_flags SET enabled=TRUE, updated_at=now()
   WHERE flag_key='memory.graph.projection.enabled'
     AND scope_type IS NULL AND scope_id IS NULL;
   ```
   No restart required. Nightly scheduler will pick up at 03:30 MSK.
   Trigger manually if desired: `/graph_project_now incremental`.

8. **After 24h: verify `graph_projection_runs` has a successful run:**
   ```sql
   SELECT mode, status, projected_node_count, projected_edge_count, actual_cost_usd
   FROM graph_projection_runs
   WHERE status = 'completed'
   ORDER BY finished_at DESC
   LIMIT 1;
   ```

9. **Flip `memory.graph.query.enabled` ON** once projection is proven:
   ```sql
   UPDATE feature_flags SET enabled=TRUE, updated_at=now()
   WHERE flag_key='memory.graph.query.enabled'
     AND scope_type IS NULL AND scope_id IS NULL;
   ```
   Admin can now use `/graph_query <topic>` and `/graph_query path <a> <b>`.

10. **Monitor `/graph_stats` daily** for drift count. If `drift_detected=True`
    and `drift_orphan_node_count > 0`, run `/graph_project_now repair` on the
    affected source pair, or a full `/graph_project_now full_rebuild --confirm`
    for a clean slate.

11. **Watch `graph_purge_pending` DLQ count.** If `failed_count` is growing
    (visible in `/graph_stats`), investigate Neo4j connectivity. The kill-switch
    (`memory.graph.write_pending.paused`) stops the worker and forces all graph
    queries to abstain, buying time for investigation without data exposure.

## §10 Phase 11 Binding Suite

Phase 10 adds 17 new binding tests. Total after T10-09: **77/77**.

New test IDs: L10a (off-record mvid blocked), L10b (no-memory mvid blocked),
L10c (forgotten mvid: `purged_at` set after cascade), C9a (no active provenance
with both source fields NULL), C9b (purged rows excluded from `find_active`),
I8a (forget → provenance soft-delete + purge_pending atomic), I8b (cascade
layer order: `graph_nodes` after `card_sources`), I8c (purge worker processes
pending row), I8d (full_rebuild deterministic), I8e (`graph_edges.purged_at` set
in same transaction), R7a (query flag OFF → `GraphQueryDisabledError`), R7b
(paused kill-switch → `GraphQueryDisabledError`), R7c (projection flag OFF →
`ServiceDisabledError`), R7d (pending purge → `abstained=True`), G2a (matching
state → no drift), G2b (orphan NetworkX node → drift detected), plus replay-only
invariant test (full_rebuild calls `extract_graph_triples` 0 times).

## §11 Phase 10.5 Carryovers

These are deferred items tracked for post-launch follow-up:

- **`LedgerRepo.daily_cost_usd` call_type filter** (T10-04): daily budget SQL
  filters `WHERE call_type='graph_projection'` — clean, but `LedgerRepo` protocol
  does not yet expose a typed `daily_cost_usd(call_type)` method; queried inline
  in projector. Cleanup in Phase 10.5.
- **Hard budget cap pre-call estimation** (T10-04): current ceiling is checked
  after each call; a pre-call estimate abort (before LLM round-trip) would reduce
  wasted spend when approaching ceiling. Deferred.
- **`dry_run` source_types parameter** (T10-04): `dry_run` mode scans all
  eligible sources; a `source_types=['knowledge_cards']` filter was spec'd but
  not wired in this sprint.
- **Migration 064 backfill batching** (T10-03): current single UPDATE is safe
  up to ~1M rows; refactor to batched `WHERE id BETWEEN N AND N+10000` if
  `llm_usage_ledger` grows past that threshold.
- **`source_card_id` arg parity in `_resolve_entity`** (T10-03): entity resolver
  accepts `source_table/source_pk` but not a direct `source_card_id` shortcut.
- **`extract_candidates` call_type bucket** (T10-03): placeholder call_type
  `'unknown'` for `extract_candidates` paths; should be tightened to a named
  bucket once the extraction pipeline is formalized.
- **`GraphPath.edges` async edge fetch optimization** (T10-05): edges are loaded
  individually per path node; batch fetch would reduce Neo4j round-trips for
  large traversal results.
- **Pre-guard scope refinement in `graph_query`** (T10-05): current read-block
  pre-check scans all pending rows for the anchor nodes; could be narrowed to
  only `source_table='message_versions'` rows (cards are never individually
  forgotten via message-level cascade).
- **`ck_graph_provenance_has_source` OR → XOR** (T10-02-rest): DB-level CHECK
  remains OR (per current spec verbatim); code-level validation in `create_provenance`
  enforces XOR semantics. DB-level tightening deferred to Phase 10.5.
- **`edge_key_hash` MERGE in T10-04 projector** (T10-08): hash-based drift
  detection works only when `edge_key_hash` is stored on GRAPH_EDGE relationships
  at MERGE time. T10-04 projector should set the property; current reconcile_counts
  falls back to count-based drift when hash is absent.
- **`NetworkXAdapter` synthetic `edge_key_hash`** (T10-08): NetworkX adapter
  returns a synthetic edge count for drift tests; full hash comparison requires
  the Neo4j adapter's Cypher `sum(r.edge_key_hash)` path.
- **Fixture host-allowlist refinement** (W0-D): `neo4j_session` allowlist
  (`localhost`, `127.0.0.1`, `neo4j`, `neo4j-test`) is conservative; may need
  extension for Docker bridge network hostnames in some CI setups.
- **`full_rebuild` 60s reply-confirmation gate** (T10-07): spec §5.G required
  a 60s "yes/да" interactive gate. Shipped `--confirm` token as a safer stopgap;
  interactive 60s gate deferred.
- **I8e Jaccard re-extraction softer eval** (T10-09): spec §10 Jaccard
  re-extraction determinism test requires two independent LLM extraction runs in
  CI. Implemented as determinism test (I8d) using replay-only `full_rebuild`
  instead. Full Jaccard test deferred.
- **Member-facing graph queries** — deferred to Phase 10.5+.
- **Public graph surface, expertise pages, APOC procedures** — deferred.

## §12 References

- `docs/memory-system/PHASE10_PLAN.md` — canonical plan (1593 lines, ratified
  2026-05-17 after dual-model spec review with 2 BLOCKER + 9 HIGH + 4 MEDIUM
  audit findings addressed).
- Per-sprint PRs: #324 (W0-A), #325 (W0-D), #326 (T10-02-rest), #327 (T10-03),
  #328 (T10-04), #329 (T10-06), #330 (T10-05), #331 (T10-08), #332 (T10-07),
  #333 (T10-09).

## Kill Switch (Emergency Disable)

If a privacy regression is suspected, pause the purge worker first:

```sql
UPDATE feature_flags SET enabled=TRUE, updated_at=now()
WHERE flag_key='memory.graph.write_pending.paused';
```

Effect: `graph_purge_worker_tick` returns immediately; all graph queries return
`abstained=True` on any pending purge row.

Then disable projection and query flags:
```sql
UPDATE feature_flags SET enabled=FALSE, updated_at=now()
WHERE flag_key IN ('memory.graph.projection.enabled','memory.graph.query.enabled');
```

This is reversible — no data is lost; only the surfaces are gated.
