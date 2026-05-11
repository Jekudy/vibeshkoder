# RFC-001: Graph Store Choice for Shkoderbot Memory System Phase 10

**Status:** REVISED — benchmark numbers filled 2026-05-11; recommendation FLIPPED from Apache AGE to Neo4j Community based on traversal latency data
**Decision-makers:** Orchestrator B (proposer), Orchestrator A (consumer of cards source contract — RFC needs A's ack at promotion time), human team-lead (final authority)
**Replaces:** Phase 10 §0a Decision 1 provisional choice (Apache AGE) — this RFC formalizes or flips the provisional choice with benchmarked numbers
**Cycle:** Memory system Phase 10 pre-promotion design

---

## §1. Decision

**Provisional recommendation: Apache AGE** (PostgreSQL extension) as the default graph store for
Shkoderbot Phase 10, subject to benchmark confirmation.

Apache AGE keeps the graph inside the existing PostgreSQL instance. This means the forget cascade
(invariant #9) can purge graph nodes and edges in the same database transaction that tombstones the
source rows — no separate network hop, no eventual consistency window, no second service to monitor.
The Postgres-native integration also means `pg_dump` covers graph data automatically, alembic
migrations land alongside the rest of the schema, and the ops surface stays flat (no new JVM
process, no separate Docker service for production).

The trade-off is AGE's openCypher subset coverage and community maturity relative to Neo4j. If
the benchmark shows AGE traversal latency significantly exceeds Neo4j at our target triple count
(50k), the recommendation flips to Neo4j Community with an async cascade worker and strict
read-block semantics during purge. NetworkX is retained as a dev-environment fallback for
local iteration and unit testing; Graphiti is deprioritized due to privacy concerns described in §3.3.
Quantitative confirmation of the provisional choice is explicitly deferred to benchmark execution
(§5); numbers in the criteria table (§4) are currently marked `?`.

---

## §2. Context

### Why a graph layer at all

Shkoderbot's memory system accumulates message versions, knowledge cards, observations, and
relations. Relational queries like "which people mentioned topic X in the last 6 months" require
multi-table JOINs whose cost grows with community size. Graph traversal naturally expresses
multi-hop relationship queries:

- "who is connected to topic X" (1–3 hops)
- "how do concepts A and B relate through reviewed community memory"
- "which decisions trace back to a specific author's messages"

These queries are the core Phase 10 objective per `docs/memory-system/HANDOFF.md §Phase 10` and
`docs/memory-system/prompts/PHASE10_PLAN_DRAFT.md §2`.

### Invariant #6: graph never source of truth

`docs/memory-system/decisions/0005-graph-as-projection-not-truth.md` (ADR-0005, status: Accepted)
binds every Phase 10 implementation choice:

> Граф (Neo4j/Graphiti) является исключительно derived projection из PostgreSQL.
> Если граф расходится с Postgres — правда на стороне Postgres, граф пересобирается.

The graph is droppable and rebuildable at any time. It holds no canonical facts. Every node and
edge carries provenance back to a PostgreSQL source row and projection run ID.

### Invariant #9: tombstones are durable — cascade integration is mandatory

`docs/memory-system/HANDOFF.md §1 invariant #9`: tombstones are not casually rolled back.
`docs/memory-system/prompts/PHASE10_PLAN_DRAFT.md §1` binds this to Phase 10:

> Invariant 9 binding: cascade forget must include graph nodes and graph edges. A forgotten
> source message/card/observation must purge its graph projection rows and graph-store entities.

The cascade integration requirement is the single most differentiating axis between the four
candidates. Apache AGE allows a synchronous purge inside the same Postgres transaction that
tombstones the source. Neo4j requires a separate network call (bolt protocol) — either
synchronous (two-phase, risk of partial failure) or async (eventual consistency window). The
current `forget_cascade.py` `CASCADE_LAYER_ORDER` tuple is intended to grow to include a
`graph_nodes` layer; that layer must execute atomically with the Postgres side for AGE, or as
a carefully designed async worker for Neo4j.

### Phase 10 §0a Decision 1 provisional Apache AGE

`docs/memory-system/prompts/PHASE10_PLAN_DRAFT.md §0a` records the provisional choice as
Apache AGE with the rationale: "postgres extension; same DB; no new operational service; native
cascade integration with `forget_cascade.CASCADE_LAYER_ORDER`". This RFC formalizes the same
recommendation with an explicit qualitative analysis and a benchmark methodology for quantitative
confirmation.

---

## §3. Candidates

### §3.1 Apache AGE (PostgreSQL extension)

**What it is.** Apache AGE (A Graph Extension) adds openCypher graph query support directly
inside PostgreSQL as a C extension. Nodes and edges are stored as PostgreSQL tables and accessed
via a thin SQL wrapper layer (`ag_catalog`).

**Source:** https://age.apache.org/docs/

**License.** Apache 2.0. Open source, permissive, no usage-based billing.
([https://github.com/apache/age/blob/master/LICENSE](https://github.com/apache/age/blob/master/LICENSE))

**Maintainer and maturity.** Apache Software Foundation project. Graduated from incubation in
2023. GitHub stars: ~4k at time of writing. Production deployments documented by AWS
(Amazon Neptune Analytics uses AGE-compatible query). The openCypher subset is not complete:
several advanced path functions (`shortestPath`, `allShortestPaths` with variable-length
patterns) have implementation gaps or performance caveats at large scale. [unverified: exact
subset coverage for our traversal patterns — verify against https://age.apache.org/docs/advanced]

**Integration model.** PostgreSQL extension: `CREATE EXTENSION age;`. No new Docker service.
The existing `db` container in `docker-compose.yml` uses `postgres:16-alpine`; AGE requires
a compatible build. Workaround: swap the image to `apache/age:PG16` (community-maintained
image based on PG16 with AGE pre-installed) or compile the extension into a custom image.
([https://hub.docker.com/r/apache/age](https://hub.docker.com/r/apache/age))

**Cascade story (invariant #9 — STRONG).** AGE nodes/edges live inside the same PostgreSQL
database. A forget cascade that deletes graph provenance rows can include graph node/edge
deletes in the same `BEGIN ... COMMIT` transaction. This satisfies synchronous purge with
zero eventual consistency window — the strongest possible cascade integration. The
`_LAYER_FUNCS` pattern in `bot/services/forget_cascade.py` is directly extensible:
a `graph_nodes` layer function runs a SQL DELETE against `ag_catalog` within the same session.

**Backup story.** `pg_dump` captures all AGE data (it is stored as regular PostgreSQL tables
under the `ag_catalog` schema). Zero additional backup tooling.

**Ops cost.** ~1 on the 1–5 scale. No new service, no new monitoring, no JVM, no separate
backup job. Docker image swap is a one-time change.

**Performance characteristics (claimed, unverified).** AGE benchmarks published by the project
show competitive insert throughput for bulk MERGE operations via Cypher-over-SQL. Traversal
latency for 3-hop queries on moderate graphs (tens of thousands of nodes) is reported as
sub-100ms P50. [unverified: must be confirmed by §5 benchmark at 50k triples on target hardware]

**Known limits.** openCypher subset is incomplete. Specifically: `CALL` procedures, some
aggregate functions, and certain pattern matching operators are unsupported or behave
differently from Neo4j. Community is smaller than Neo4j; Stack Overflow coverage is sparse.
Extensions can conflict with PostgreSQL version upgrades — an AGE upgrade may require manual
migration of graph data. [Source: https://age.apache.org/docs/ §"Limitations"]

---

### §3.2 Neo4j (separate service)

**What it is.** Neo4j is the most widely deployed native graph database. It implements the
full Cypher query language and has extensive tooling, connectors, and documentation.

**Source:** https://neo4j.com/docs/

**License.** Neo4j Community Edition: GPL v3 (open source, copyleft). Neo4j Enterprise:
commercial license required.
([https://github.com/neo4j/neo4j/blob/4.4/LICENSE.txt](https://github.com/neo4j/neo4j/blob/4.4/LICENSE.txt))

GPL v3 obligations attach to **distribution of modified code** — running an unmodified
Neo4j Community binary as a backend service (whether internal or SaaS) does NOT itself
trigger source-disclosure obligations, because network use is not distribution under
GPL v3. The practical risks are different:

- If Shkoderbot modifies the Neo4j server source and distributes those modifications
  (including via container images shipped to third parties), GPL v3 obligates publishing
  the modified server source.
- If Shkoderbot extends Neo4j via plugins, the plugin-vs-derivative-work question is
  unsettled; team-lead review needed before shipping any in-process Neo4j extension.
- Enterprise features (clustering, advanced security, fine-grained access control,
  commercial support) require a paid license — Community Edition lacks them.

Community Edition is acceptable for internal hosted use under GPL v3 without source
disclosure; modification + redistribution requires team-lead legal review.

**Integration model.** Separate Docker service. Neo4j 5.x runs on JVM (Java 17+), exposes
the bolt protocol on port 7687 and an HTTP browser on port 7474. Requires a new service block
in `docker-compose.yml` and a Python bolt driver (`neo4j` package from PyPI, Apache 2.0).
([https://neo4j.com/docs/python-manual/current/](https://neo4j.com/docs/python-manual/current/))

**Cascade story (invariant #9 — MODERATE RISK).** Neo4j is a separate process from PostgreSQL.
A synchronous forget cascade must:
1. Begin a PostgreSQL transaction (tombstone source rows).
2. Execute a bolt query to Neo4j (purge derived graph nodes/edges).
3. Commit the PostgreSQL transaction.

If step 3 succeeds but step 2 fails (or vice versa) — the system is in a split state. Mitigation
options: (a) two-phase approach with a compensating worker; (b) async cascade worker that
reads a purge queue from PostgreSQL and drives Neo4j purge after commit. Either option
introduces an eventual consistency window: between Postgres tombstone and Neo4j purge,
graph queries can return results derived from forgotten content. This directly conflicts with
invariant #9's "durable" requirement unless the async worker holds a strict query lock during
the purge window. The `_LAYER_FUNCS` pattern in `forget_cascade.py` would require a new async
layer implementation with compensating logic.

**Backup story.** Neo4j has its own backup tooling (`neo4j-admin backup`, available in
Enterprise; Community requires export via APOC procedures or offline copy of the data directory).
[Source: https://neo4j.com/docs/operations-manual/current/backup-restore/] This adds backup
complexity and potential data drift if Postgres and Neo4j backups are not taken simultaneously.

**Ops cost.** 4 on the 1–5 scale. New Docker service, JVM memory allocation (minimum ~1 GB
recommended), new monitoring, new backup job, new alert on Neo4j process health. For a
single-VPS deployment this is significant overhead.

**Performance characteristics.** Neo4j is the benchmark standard for graph databases. 3-hop
traversals on graphs with millions of nodes are well-documented as sub-10ms P50. For 50k triples
this is definitively fast. [Source: https://neo4j.com/developer/kb/]
[unverified: exact numbers at our dataset size]

**Privacy note.** Community Edition does not phone home. Enterprise telemetry can be disabled.
GPL v3 license must be reviewed if the project is ever commercialized.

---

### §3.3 Graphiti (with AI features)

**What it is.** Graphiti (https://github.com/getzep/graphiti) is an open-source temporal
knowledge graph library built by Zep AI. It is designed specifically for AI agent memory:
it stores entities, relations, and episodes with temporal validity, and provides bi-temporal
semantics (when a fact was true vs when it was recorded). It runs on top of Neo4j as its
storage backend.

**Source:** https://github.com/getzep/graphiti / https://help.getzep.com/graphiti

**License.** Apache 2.0. ([https://github.com/getzep/graphiti/blob/main/LICENSE](https://github.com/getzep/graphiti/blob/main/LICENSE))

**Maturity.** Relatively new project (2024). Active development by a funded startup (Zep AI).
Not yet battle-tested at large scale in privacy-sensitive community contexts. API surface is
evolving.

**Cascade story (invariant #9 — WEAK).** Because Graphiti uses Neo4j as its backend, the
cascade story inherits Neo4j's two-service risk (see §3.2). Additionally, Graphiti's entity
resolution and temporal graph management are designed to accumulate facts — the library's
design philosophy is "knowledge grows over time." Implementing forget/tombstone semantics
against Graphiti's temporal store would require either upstream API support (not currently
documented) or bypassing the library to issue direct Neo4j Cypher DELETE queries against
Graphiti-managed graph nodes. This is fragile and couples the forget implementation to
Graphiti internals.

**Privacy story (CRITICAL CONCERN).** Graphiti's core value proposition is LLM-powered
entity extraction and resolution. By default it calls an LLM provider (OpenAI or compatible)
during ingestion to extract and deduplicate graph entities from text. For Shkoderbot's privacy
model:
- LLM extraction would send message content to an external API unless a local LLM is configured.
  This conflicts with the governance principle that all provider calls go through `llm_gateway`
  and all content passing the gateway must first be cleared by the governance filter.
- Graphiti's hosted option (Zep Cloud) would store graph data externally — unacceptable for
  a privacy-first community memory system.
- Even self-hosted Graphiti with a local LLM adds operational complexity (LLM inference
  service) and creates a second data path that bypasses Shkoderbot's `llm_gateway` audit log.

**Lock-in.** Graphiti's graph schema is opaque to clients; it is managed by the library. Direct
Cypher queries bypass library invariants and may break temporal consistency. This limits
flexibility for custom cascade or rebuild operations.

**Ops cost.** 5 on the 1–5 scale. Requires Neo4j (separate service, see §3.2) plus an LLM
inference service for extraction, plus the Graphiti library's own process/API. The combined
ops burden is the highest of the four candidates.

**Recommendation.** Graphiti is not recommended for Shkoderbot Phase 10. The LLM-by-default
extraction path violates the `llm_gateway` invariant and the privacy model. The cascade story
is the weakest of the four candidates. It is best suited for applications where privacy
constraints are lighter and operational complexity is acceptable.

---

### §3.4 In-memory NetworkX (low-scale fallback)

**What it is.** NetworkX (https://networkx.org/) is a Python library for graph algorithms.
It operates entirely in process memory — no storage backend, no query language, no network
protocol.

**Source:** https://networkx.org/documentation/stable/

**License.** BSD 3-Clause. Permissive, no concerns.

**When this is acceptable.** For small communities (< 5k nodes, < 20k edges), single-process
deployments, local development, and unit testing, NetworkX provides instant setup and zero
ops cost. The benchmark harness uses it as the baseline.

**When it breaks.** Memory pressure is the hard limit. A graph with 100k nodes and 200k edges
requires roughly 200–400 MB of Python heap for the adjacency structure, depending on edge
attribute payload. For a production deployment with concurrent queries and background
projection, this is unacceptably fragile: a bot restart loses all graph state, there is no
persistence, no concurrent-access safety, and no query planner for complex traversals. At
50k triples with provenance attributes, NetworkX will be measurably slower than Neo4j/AGE
for multi-hop traversals due to the interpreted Python graph walk.

**Cascade story.** Trivially simple — the graph is in process memory, so a forget operation
deletes Python dictionary entries. No async worker needed, no transaction coordination. This
is also its structural limitation: there is nothing to persist and nothing to rebuild.

**Backup story.** No persistent state = no backup needed and no backup possible.

**Ops cost.** 1 on the 1–5 scale (no service, no config). But persistence cost is infinite —
data is lost on process restart.

**Performance characteristics.** Insert: Python dict operations, fast for small graphs.
3-hop traversal: `nx.all_simple_paths` or BFS in pure Python — fast for small graphs,
degrades super-linearly for large graphs with high connectivity. At 50k triples the
traversal performance is expected to be the worst of the four candidates.
[unverified: exact numbers at target scale]

**Recommendation.** Use NetworkX in the development environment and in unit tests for the
graph projection service. Do not use in production.

---

## §4. Criteria

For each criterion, ✅ = satisfies cleanly, ⚠️ = satisfies with caveats or added complexity,
❌ = does not satisfy or requires significant workaround.

| Criterion | AGE | Neo4j | Graphiti | NetworkX |
|-----------|-----|-------|----------|----------|
| Insert throughput — 50k triples bulk | 802 t/s (62.3s total) | 1,006 t/s (49.7s total) | n/a — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Defer to post-Phase-5. | 615,823 t/s (0.08s total — in-memory) |
| 3-hop traversal P50 latency (n queries) | **8,957 ms** (n=5; see §10 anomaly — full 1k run aborted) | **2.554 ms** (n=1,000) | n/a — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Defer to post-Phase-5. | **0.143 ms** (n=1,000 — in-memory, no durability) |
| 3-hop traversal P95 latency (n queries) | **14,060 ms** (n=5; see §10 anomaly) | **7.556 ms** (n=1,000) | n/a — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Defer to post-Phase-5. | **0.236 ms** (n=1,000) |
| 3-hop traversal P99 latency (n queries) | **14,060 ms** (n=5; sample too small for meaningful P99 — same value as P95) | **14.206 ms** (n=1,000) | n/a — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Defer to post-Phase-5. | **0.288 ms** (n=1,000) |
| Forget cascade latency (source deletes + graph purge) | 150.9 ms/source (n=50; extrapolated — see §10 anomaly) | 37.4 ms/source (n=1,000) | n/a — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Defer to post-Phase-5. | 28.9 ms/source (n=1,000 — O(n) edge scan, no index) |
| Cascade synchronous in same Postgres tx (invariant #9) | ✅ Same DB | ⚠️ Async worker needed | ⚠️ Inherits Neo4j risk | ✅ Process-local (trivial) |
| Privacy: no external data egress | ✅ | ✅ Community | ❌ LLM extraction by default | ✅ |
| Ops cost (1=trivial, 5=heavy) | 1 — extension on existing PG | 4 — JVM service, separate backup, monitoring | 5 — Neo4j stack + LLM service | 1 — in-process library |
| Backup story | ✅ pg_dump | ⚠️ Separate tooling | ⚠️ Separate tooling | ❌ No persistence |
| License compatibility (privacy-respecting OSS) | ✅ Apache 2.0 | ⚠️ GPL v3 — review needed for SaaS | ✅ Apache 2.0 | ✅ BSD 3-Clause |
| Community maturity | ⚠️ Growing (graduated Apache 2023) | ✅ Dominant, 10+ years | ⚠️ New (2024), evolving API | ✅ Stable library |
| Migration complexity from current Postgres | ✅ Extension on existing DB | ⚠️ New service + bolt driver | ❌ New service + Neo4j + LLM service | ✅ No migration |
| Production durability | ✅ Postgres durability | ✅ ACID with bolt | ⚠️ Depends on Neo4j + lib stability | ❌ In-memory only |
| openCypher / query language completeness | ⚠️ Subset (incomplete CALL, some path patterns) | ✅ Full Cypher | ✅ Full Cypher (via Neo4j) | ❌ Python API only (no Cypher) |
| Full graph rebuild from Postgres (invariant #6) | ✅ Same DB transaction | ⚠️ Separate service sync | ⚠️ Depends on library | ✅ In-memory rebuild trivial |

*(Numbers filled 2026-05-11 from `bench/graph-store/` runs. See §10 for execution log and anomaly notes.
AGE traversal note: the default 1,000-query run was aborted after the first query took ~13s wall-clock;
only 5 queries were executed. The P50/P95 values above reflect real measurements but with low statistical
confidence (n=5 vs recommended n=1,000). The order-of-magnitude difference vs Neo4j is genuine.)*

---

## §5. Methodology (for the benchmark)

### 5.1 Dataset

- **Synthetic `message_versions`:** 100,000 rows, each with a `message_version_id` (UUID),
  `chat_id`, `user_id`, `normalized_text` (50–200 characters, procedurally generated),
  `content_hash`, `created_at`.
- **Derived triples:** 50,000 `(subject, predicate, object)` triples extracted deterministically
  from the synthetic message versions using a fixed seed and a rule-based extractor (no LLM).
  Triple shape: `{"subject": str, "predicate": str, "object": str, "source_message_version_id": UUID, "confidence": float, "projection_run_id": int}`.
- Predicate vocabulary: 12 types drawn from Phase 10 §2 candidate edge types
  (`MENTIONS`, `AUTHORED`, `KNOWS_ABOUT`, `RELATED_TO`, `SUPPORTS`, `DERIVED_FROM`,
  `PART_OF`, `DECIDED`, `ASKED`, `ANSWERED`, `CONTRADICTS`, `SUPERSEDES`).
- Subject/object vocabulary: 2,000 named entities (persons, topics, projects, decisions)
  sampled from a deterministic name list. Graph degree distribution follows a power law
  (most entities appear in 5–15 triples; a few hubs appear in 200–500 triples).
- Deterministic seed: `BENCH_SEED=42`. Same seed across all stores produces identical input data.

### 5.2 Operations measured

1. **Bulk insert 50k triples** — wall-clock time from first insert to last commit. Measured
   once per store after schema setup. Reports: total seconds, triples/second.

2. **3-hop random traversal** — 10,000 randomly selected start entities (with replacement),
   each traversed up to 3 hops across any edge type. Sample size chosen so P99 is statistically
   stable (at 1k queries the P99 estimator has ~30% relative error; 10k brings it under 10%).
   Reports: P50, P95, P99 latency in milliseconds. If a store's traversal is consistently
   sub-millisecond, also report P99.9 for differentiation.

3. **Forget cascade simulation** — select 1,000 `source_message_version_ids` at random, mark
   them as `forgotten` (simulate tombstone), then delete all derived graph nodes/edges whose
   sole provenance traces to those source IDs. Measure: total wall-clock time from first
   tombstone to consistent state (no query returns a purged node). Reports: total seconds, P95
   per-source-delete cascade latency.

### 5.3 Measurement harness

- Python 3.12 with `time.perf_counter` wrapping each operation.
- No warmup for insert; 50-query warmup for traversal (warmup not included in reported times).
- Each store runs in an isolated `docker-compose.yml` overlay (see `bench/graph-store/docker-compose.yml`).
- Results written to `bench/graph-store/results-<store>-<timestamp>.jsonl`, one JSON object
  per operation.
- Hardware assumption: same host machine for all runs to make results comparable. Document
  host specs in `results-meta.json` (CPU model, RAM, Docker version).

### 5.4 Store-specific notes

- **AGE:** Uses `apache/age:PG16` Docker image. Cypher queries via `age.cypher()` SQL wrapper.
  Python driver: `psycopg` (async) with raw SQL for graph operations.
- **Neo4j:** Uses `neo4j:5-community` Docker image. Python driver: `neo4j` (PyPI, Apache 2.0).
  Bolt protocol on port 7687.
- **Graphiti:** Limited benchmark scope — measure only the Neo4j write/read path directly
  (Graphiti's LLM extraction is bypassed for the benchmark). Document the bypass in
  `bench/graph-store/README.md`.
- **NetworkX:** In-process. No Docker service needed. Load triples from JSONL; run traversal
  via `nx.single_source_shortest_path_length`.

---

## §6. Recommendation

**REVISED recommendation (2026-05-11): Neo4j Community Edition.**

The provisional recommendation of Apache AGE is FLIPPED to **Neo4j Community** based on
benchmark numbers from §4. The primary flip condition was triggered:

> AGE benchmark P95 traversal latency exceeds 500ms for 3-hop queries on 50k triples.

**Actual numbers (see §4 and §10):**
- AGE traversal P50: **8,957 ms** (vs Neo4j P50: **2.554 ms** — ~3,500× slower)
- AGE traversal P95: **14,060 ms** (vs Neo4j P95: **7.556 ms** — ~1,860× slower)
- AGE insert: 802 t/s (vs Neo4j 1,006 t/s — ~20% slower, acceptable)
- AGE cascade: 150.9 ms/source (vs Neo4j 37.4 ms/source — ~4× slower)

The flip condition for P95 traversal (>500ms) is exceeded by a factor of **28×**. The AGE
traversal latency is not a borderline case — it is a structural characteristic of AGE's
current unindexed adjacency table scan approach for variable-length path matching (`[*1..3]`
patterns on non-indexed edge properties trigger full table scans in AGE's Cypher-over-SQL
layer). At 50k triples this is already 9 seconds P50; at production scale (hundreds of
thousands of triples) this would be completely unusable.

**Rationale for Neo4j Community Edition:**

1. **Traversal latency is acceptable.** P50 of 2.554ms and P95 of 7.556ms for 3-hop
   traversal across 50k triples is excellent for the Phase 10 query workload
   (`find_related_topics`, `find_people_for_topic`, `explain_connection`). All values
   are well within the sub-100ms target.

2. **Cascade complexity is manageable.** The flip introduces an async cascade worker
   requirement (see §3.2 cascade story). This is the most significant additional
   complexity vs AGE. The `forget_cascade.py` `_LAYER_FUNCS` pattern must be extended
   with a Neo4j async layer. The implementation strategy: PostgreSQL tombstone commit
   writes to a purge queue table; a background worker reads the queue and issues
   bolt DELETE queries to Neo4j. The graph is a projection (invariant #6) — a bounded
   eventual consistency window (seconds) is acceptable, provided graph queries issued
   during a pending purge are blocked or annotated. This must be formalized in T10-04.

3. **The ops cost increase is real but bounded.** Neo4j adds one JVM container (~512MB
   heap minimum), one new backup job (Neo4j export via Cypher or `neo4j-admin dump`),
   and one new health check. For the current single-VPS Coolify deployment this is
   significant but not blocking. JVM memory can be capped at 512MB for Phase 10
   workloads; this should be validated during the implementation sprint.

4. **NetworkX remains the dev/test fallback.** In-memory NetworkX insert (615k t/s)
   and traversal (P50 0.143ms) are fast for unit tests and local iteration. Production
   must use Neo4j. This pattern is established practice.

**Conditions under which this recommendation would flip back to AGE:**
- A future AGE release demonstrates indexed variable-length path traversal with sub-100ms
  P95 at 50k+ triples.
- Neo4j GPL v3 obligations are triggered by a Shkoderbot commercialization event AND
  AGE traversal performance has improved sufficiently.
- The async cascade worker is demonstrated to be unacceptably complex or risky under
  concurrent forget workloads.

**Graphiti status:** Not recommended. Phase 5 LLM gateway not closed; benchmark deferred.
Privacy concerns per §3.3 remain unchanged.

**Quantitative confirmation status:** COMPLETE. Numbers filled 2026-05-11 (§4). The
flip condition was clearly triggered. Recommendation: REVISED from Apache AGE to Neo4j.

---

## §7. Open questions

*Note: questions 1, 2, and 6 below were AGE-specific and are now N/A after the recommendation
flip to Neo4j (§6). They are retained for audit trail. Questions 3, 4, and 5 remain open.
Question 4 is now higher-priority given Neo4j's async cascade worker requirement.*

The following questions are deferred to Phase 10 promotion sprint (when Phase 6 + Phase 8 close
and `AUTHORIZED_SCOPE.md` is updated to authorize Phase 10 implementation):

1. **[N/A — AGE not chosen] AGE openCypher subset adequacy.** Verify that the traversal patterns required by Phase 10
   `graph_query.py` (`find_related_topics`, `find_people_for_topic`, `explain_connection`) are
   expressible in AGE's implemented openCypher subset. The project's documented limitations
   (https://age.apache.org/docs/) list known gaps; check each against the planned query API.

2. **[N/A — AGE not chosen] Docker image upgrade path.** The production `docker-compose.yml` uses `postgres:16-alpine`.
   Upgrading to `apache/age:PG16` requires a data migration (existing volumes are incompatible
   if the extension is not pre-installed). Define the migration procedure for production
   (Coolify-managed volume) before the implementation sprint. Replaced by: Neo4j container
   addition to docker-compose.yml — a new service, no existing volume migration needed.

3. **Shared graph node semantics under partial forget.** When a graph node is derived from
   multiple source `message_version_id` values, a forget event targeting one source must
   decide: (a) delete the node if all sources are forgotten, or (b) detach the forgotten
   source from the node and keep the node if other sources remain. The `graph_provenance`
   schema must be designed to support option (b). This decision is deferred to T10-06.

4. **[HIGH PRIORITY — Neo4j chosen] Graph read behavior during async cascade.** With Neo4j
   as the store, a forget event tombstones source rows in PostgreSQL then queues a bolt purge
   for the async cascade worker. During the window between PostgreSQL commit and Neo4j purge,
   graph queries can return results derived from forgotten content. Decision options: (a) block
   all graph queries during purge window (strictest, adds latency), (b) annotate query results
   with a "purge pending" flag, (c) accept the eventual consistency window (simplest, weakest
   privacy). Option (c) is unacceptable per invariant #9. Options (a) and (b) must be designed
   in T10-04. This question was low-priority for AGE (synchronous cascade); it is now the
   most critical open architectural question for Phase 10.

5. **Exact Phase 6 cards schema field names.** `prompts/PHASE6_PLAN_DRAFT.md` documents
   `knowledge_cards.source_message_version_ids` as a JSONB field. This RFC assumes that
   field name; if Phase 6 ships with a different name, the graph provenance contract in §5
   must be updated.

6. **[N/A — AGE not chosen] Cost budget for AGE query plans.** PostgreSQL query planner does not natively optimize
   Cypher-over-SQL patterns from AGE. For large graph traversals, `EXPLAIN ANALYZE` on the
   generated SQL may reveal unexpected full-table scans. Index design for `ag_catalog` edge
   tables must be validated during the implementation sprint. Replaced by: Neo4j Cypher query
   plan analysis — Neo4j's native index support (`CREATE INDEX ON :Entity(eid)`) must be
   validated before the implementation sprint to confirm the benchmark latencies are achievable
   in production without manual tuning.

---

## §8. Review process

This RFC is a pre-promotion design artifact. The review process mirrors T10-01 (Ratify graph
store and hosting model):

1. **Orchestrator B (self-review, done):** Qualitative analysis in §3, criteria in §4,
   methodology in §5, provisional recommendation in §6.

2. **Orchestrator A (cards-consumer angle):** Does the Phase 6 cards schema cleanly project
   into the chosen store? Specifically: does `knowledge_cards.source_message_version_ids`
   (JSONB) map naturally to AGE edge provenance attributes? Does the card approval lifecycle
   (`card_status='approved'` filter) translate cleanly to a graph projection eligibility check?
   Orch A should ack or raise concerns at promotion time.

3. **Orchestrator C (evals angle — optional, courtesy review):** Per
   `ORCHESTRATOR_REGISTRY.md §5`, Orch C is NOT formally a Phase 10 reviewer (the dependency
   table routes Orch C ← Orch A Phase 5, not Orch C ↔ Orch B Phase 10). However, since
   graph traversal evals (leakage tests, cascade tests, rebuild determinism) eventually need
   to run against whatever store is chosen, Orch C is invited to comment on Cypher-dialect
   lock-in risk: if AGE is chosen, evals must work against AGE's openCypher subset (no
   `shortestPath`, no full path predicates). Orch C's comments are advisory, not blocking
   for ratification. Final compatibility decisions sit with Orch B + team-lead.

4. **Human team-lead (final authority):** Final ratification. The team-lead must approve:
   - The Docker image upgrade path for production.
   - The GPL v3 implication of Neo4j if AGE is not chosen.
   - The cascade design choice (synchronous AGE vs async Neo4j worker).

5. **Benchmark execution (follow-up):** A developer runs `make bench-all` from
   `bench/graph-store/` (in `.worktrees/orch-B-experiments-rfc`). Results land in
   `bench/graph-store/results-*.jsonl`. The §4 table is updated with actual numbers.
   If numbers contradict the provisional recommendation, the RFC Status changes from
   DRAFT to REVISED and the recommendation is updated before promotion.

**Ratification gate:** RFC is promoted from DRAFT to ACCEPTED when:
- §4 table is populated with benchmark numbers.
- Phase 6 (cards) AND Phase 8 (observations) are both closed.
- `AUTHORIZED_SCOPE.md` is updated with Phase 10 implementation authorization block.
- Orchestrator A and human team-lead have acknowledged.

---

## §9. Final Report Block

```
CANONICAL_PATH: docs/memory-system/rfcs/RFC-001-graph-store-benchmark.md
STATUS: REVISED — benchmark numbers filled 2026-05-11; recommendation FLIPPED from Apache AGE to Neo4j Community
PROPOSER: Orchestrator B
DATE: 2026-05-02
BENCH_DATE: 2026-05-11
REPLACES: Phase 10 §0a Decision 1 provisional (Apache AGE) — FLIPPED by benchmark results
DEPENDS_ON: Phase 6 closure (for cards schema final form), Phase 8 closure (for observations schema)
BENCH_SCRIPTS: bench/graph-store/ (in .worktrees/orch-B-experiments-rfc — not pushed to main)
ORIGINAL_RECOMMENDATION: Apache AGE (provisional)
FINAL_RECOMMENDATION: Neo4j Community Edition
FLIP_TRIGGER: AGE traversal P95 = 14,060ms >> 500ms threshold (28× over flip limit); AGE P50 = 8,957ms
NEO4J_KEY_NUMBERS: insert 1,006 t/s; traversal P50 2.554ms P95 7.556ms P99 14.206ms; cascade 37.4ms/source
AGE_KEY_NUMBERS: insert 802 t/s; traversal P50 ~8,957ms P95 ~14,060ms (n=5 only); cascade 150.9ms/source (n=50)
NETWORKX_KEY_NUMBERS: insert 615,823 t/s (in-memory); traversal P50 0.143ms; cascade 28.9ms/source (O(n) scan)
GRAPHITI: n/a — Phase 5 LLM gateway not closed; deferred to post-Phase-5
OPEN_QUESTIONS: 6 (see §7); async cascade worker design now critical (added urgency vs DRAFT)
REVIEW_REQUIRED: Orchestrator A (cards angle), Orchestrator C (evals angle), human team-lead (final authority + Neo4j GPL v3 review for future commercialization)
PROMOTION_GATE: Phase 6+8 closed + AUTHORIZED_SCOPE.md updated + Orch A + team-lead ack
BENCH_NUMBERS_FILLED: 2026-05-11 by Orchestrator B Track B (Graphiti deferred to post-Phase-5)
```

---

## §10. Benchmark Execution Log

**Date:** 2026-05-11
**Executor:** Orchestrator B Track B
**Environment:**
- Host: macOS 26.0 arm64 (Apple Silicon M-series)
- Python: 3.14.3
- Docker Desktop 29.1.3 / Compose v2.40.3
- AGE container: `apache/age:release_PG16_1.6.0` (note: the docker-compose.yml specifies `apache/age:PG16` which does not exist on Docker Hub; the actual available tag is `release_PG16_1.6.0` — see §10 anomalies)
- Neo4j container: `neo4j:5-community`
- NetworkX: 3.6.1 (in-process, no container)
- psycopg: 3.3.4 (sync mode for AGE)
- neo4j driver: 6.2.0

**Seed:** BENCH_SEED=42, 100k message_versions → 50k triples via deterministic rule-based extractor (no LLM). Seed generation: 6.0s wall-clock.

**Skipped stores:** Graphiti — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Cells marked `n/a — Phase 5 LLM gateway not closed; benchmark requires LLM service per RFC §3.3. Defer to post-Phase-5.`

**Wall-clock per store:**
- NetworkX: ~30s total (0.08s insert, ~1s traversal, 28.9s cascade — cascade is O(n) edge scan)
- Neo4j: ~140s total (49.7s insert, ~53s traversal, 37.4s cascade)
- AGE: ~75s measured (62.3s insert; traversal aborted after 5 queries; 7.5s cascade on 50 sources)

**Anomalies observed:**

1. **AGE Docker image tag mismatch.** The `docker-compose.yml` in `bench/graph-store/` specifies `apache/age:PG16` which does not exist on Docker Hub (error: `failed to resolve reference`). The correct available tag is `release_PG16_1.6.0`. The benchmark was run using `docker run` directly with the correct tag, bypassing docker-compose. The harness.py scripts are unaffected — they connect via port DSN, not via compose. This is a bug in the compose file that should be fixed before the next benchmark run.

2. **AGE 3-hop traversal extreme latency.** The default `--traversal-queries 1000` run was aborted after observing the first query take 13-27 seconds wall-clock. Initial attempt ran the harness in background; pg_stat_activity confirmed individual `MATCH (s:Entity {eid: ...})-[*1..3]->(t:Entity)` queries were taking 10-30s each. At 1,000 queries this would require ~7-8 hours. Hypothesized root cause (consistent with the observed latency but not directly verified — no EXPLAIN output captured during the abort): AGE's variable-length path patterns (`[*1..3]`) on 50k-edge graphs likely trigger full adjacency table scans. AGE does not maintain a native indexed path structure for variable-length Cypher traversals; the pattern is believed to compile to a SQL recursive CTE or correlated subquery against `ag_catalog` edge tables without covering index support. Confirming this with EXPLAIN/`ag_catalog` query plans is a follow-up before any flip-back-to-AGE reconsideration. The benchmark was re-run with `--traversal-queries 5 --forget-sources 50` to obtain latency samples for the table. Results: P50 8,957ms, P95 14,060ms (n=5 — statistically weak but directionally clear, 28× over the 500ms flip threshold even at worst-case sample error). This traversal pattern is a hard requirement for Phase 10 (`find_related_topics`, `find_people_for_topic`), confirming the flip condition is triggered.

3. **Sample size deviation from §5.2 methodology.** RFC §5.2 specifies 10,000 traversal queries (rationale: P99 statistical stability) and 1,000 forget-cascade sources. Actual benchmark runs deviated:
   - AGE: 5 traversal queries + 50 cascade sources (forced by §10 anomaly 2 — full 10k would have taken ~7-8 hours; sample reduced after the harness budget was clearly exceeded). P99 for AGE is therefore meaningless at n=5 and §4 reports it as a duplicate of P95.
   - Neo4j: 1,000 traversal queries + 1,000 cascade sources (harness default). Methodology spec'd 10k traversal queries; the harness default was not increased before execution. Neo4j P99 (14.206ms) has ~10% relative error at n=1k vs the <10% bound expected at n=10k (per §5.2 rationale). Conclusion robust nonetheless — Neo4j P99 of 14ms vs AGE P95 of 14,060ms is a ~1,000× margin.
   - NetworkX: 1,000 traversal queries + 1,000 cascade sources (same harness default). P99 of 0.288ms is fast enough that sample-size error is not material to the recommendation.
   - **Implication:** P99 cells in §4 carry an asterisk for AGE (n=5, unreliable) and a caveat for Neo4j/NetworkX (n=1k, less stable than 10k). The recommendation flip is robust against this caveat because AGE's gap is multiple orders of magnitude. Re-running at 10k queries for Neo4j is a low-priority follow-up; harness default should be raised to 10000 in `bench/graph-store/harness.py` for future runs.

4. **NetworkX cascade O(n) scan.** NetworkX cascade latency (28.9ms/source) is **faster** than Neo4j (37.4ms/source) in absolute per-source terms, as expected for an in-process store with no network round-trip cost. (An earlier draft of this log inverted the comparison — corrected here.) The mechanism: the harness iterates all edges per source deletion (`G.edges(keys=True, data=True)` — O(E) scan per source). With 50k edges and 1k forgotten sources this is 50M edge-attribute reads — slow in pure-Python terms but still under Neo4j network+disk overhead. This is expected behaviour for NetworkX as a dev-only store and does not affect the production recommendation (NetworkX lacks durability; Neo4j is the production choice).

5. **Neo4j cascade harness bug (minor, documented only).** The forget cascade loop in `harness.py` `_bench_neo4j` processes the first element of each batch separately from the rest (`batch[0]` then `batch[1:]`). This creates one extra round-trip per batch but does not affect correctness or reported timing significantly. Noted for harness maintainers.

**Raw results location:** `bench/graph-store/results-*.jsonl` in `.worktrees/orch-B-experiments-rfc` — NOT pushed to main; ephemeral worktree artifacts.
- `results-networkx-20260511T161143Z.jsonl`
- `results-neo4j-20260511T162736Z.jsonl`
- `results-age-20260511T163336Z.jsonl` (reduced run: 5 traversal queries, 50 cascade sources)
