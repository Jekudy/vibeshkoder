# RFC-001: Graph Store Choice for Shkoderbot Memory System Phase 10

**Status:** DRAFT — proposed by Orchestrator B sprint Track 3, date 2026-05-02
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
| Insert throughput — 50k triples bulk | ? | ? | ? | ? |
| 3-hop traversal P50 latency (10k queries) | ? | ? | ? | ? |
| 3-hop traversal P95 latency (10k queries) | ? | ? | ? | ? |
| Forget cascade latency (1k source deletes + graph purge) | ? | ? | ? | ? |
| Cascade synchronous in same Postgres tx (invariant #9) | ✅ Same DB | ⚠️ Async worker needed | ⚠️ Inherits Neo4j risk | ✅ Process-local (trivial) |
| Privacy: no external data egress | ✅ | ✅ Community | ❌ LLM extraction by default | ✅ |
| Ops cost (1=trivial, 5=heavy) — *qualitative pre-benchmark estimate* | ? (~1 — extension on existing PG) | ? (~4 — JVM service, separate backup, monitoring) | ? (~5 — Neo4j stack + LLM service) | ? (~1 — in-process library) |
| Backup story | ✅ pg_dump | ⚠️ Separate tooling | ⚠️ Separate tooling | ❌ No persistence |
| License compatibility (privacy-respecting OSS) | ✅ Apache 2.0 | ⚠️ GPL v3 — review needed for SaaS | ✅ Apache 2.0 | ✅ BSD 3-Clause |
| Community maturity | ⚠️ Growing (graduated Apache 2023) | ✅ Dominant, 10+ years | ⚠️ New (2024), evolving API | ✅ Stable library |
| Migration complexity from current Postgres | ✅ Extension on existing DB | ⚠️ New service + bolt driver | ❌ New service + Neo4j + LLM service | ✅ No migration |
| Production durability | ✅ Postgres durability | ✅ ACID with bolt | ⚠️ Depends on Neo4j + lib stability | ❌ In-memory only |
| openCypher / query language completeness | ⚠️ Subset (incomplete CALL, some path patterns) | ✅ Full Cypher | ✅ Full Cypher (via Neo4j) | ❌ Python API only (no Cypher) |
| Full graph rebuild from Postgres (invariant #6) | ✅ Same DB transaction | ⚠️ Separate service sync | ⚠️ Depends on library | ✅ In-memory rebuild trivial |

*(Numbers in the performance rows will be filled after §5 benchmark execution. RFC ships with
methodology + qualitative analysis; quantitative confirmation is a follow-up multi-hour run.)*

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

**Provisional recommendation: Apache AGE.**

Based on the qualitative analysis in §3, AGE is recommended because:

1. **Cascade integration (invariant #9)** is the dominant constraint for this codebase.
   AGE allows synchronous graph purge inside the same PostgreSQL transaction as the source
   tombstone. `forget_cascade.py`'s `_LAYER_FUNCS` pattern can be extended with a
   `graph_nodes` layer that issues a Cypher DELETE via `ag_catalog.cypher()` inside the
   same `AsyncSession`. No async worker, no eventual consistency window, no compensating
   transaction logic.

2. **Ops simplicity.** The existing `docker-compose.yml` uses `postgres:16-alpine`. Swapping
   to `apache/age:PG16` adds zero new services, zero new monitoring, and zero new backup jobs.
   For a single-VPS Coolify deployment (current production topology), this matters significantly.

3. **Backup story.** `pg_dump` already backs up the AGE graph because it lives in the same
   PostgreSQL instance as the application data. A Neo4j backup out-of-sync with a Postgres
   backup creates a split-state risk.

4. **License.** Apache 2.0 is unambiguous. Neo4j Community's GPL v3 requires review if
   Shkoderbot is ever commercialized or offered as a hosted service.

The known weakness is AGE's incomplete openCypher subset. The traversal patterns needed for
Phase 10 (3-hop entity queries, path-finding, degree queries) are within AGE's documented
capabilities, but this must be verified against `https://age.apache.org/docs/advanced` before
the implementation sprint. If any required traversal pattern is missing, a workaround using
SQL JOINs against the AGE adjacency tables may be acceptable as a fallback — the graph is a
projection; complex traversals that cannot be expressed in AGE's Cypher subset can fall back
to PostgreSQL queries.

**Conditions for flipping to Neo4j:**
- AGE benchmark P95 traversal latency exceeds 500ms for 3-hop queries on 50k triples.
- A required Phase 10 traversal pattern is not expressible in AGE's openCypher subset
  AND cannot be reasonably approximated with a SQL fallback.
- The cascade layer for Neo4j can be demonstrated to be safe under concurrent forget events
  (e.g., via a compensating async worker with a strict read-block during purge).

**Quantitative confirmation status:** PENDING. Numbers in §4 are `?` and will be filled by
benchmark execution per §5. This RFC ships with the methodology and qualitative rationale;
the recommendation stands provisionally and will be confirmed or flipped after benchmark runs.

---

## §7. Open questions

The following questions are deferred to Phase 10 promotion sprint (when Phase 6 + Phase 8 close
and `AUTHORIZED_SCOPE.md` is updated to authorize Phase 10 implementation):

1. **AGE openCypher subset adequacy.** Verify that the traversal patterns required by Phase 10
   `graph_query.py` (`find_related_topics`, `find_people_for_topic`, `explain_connection`) are
   expressible in AGE's implemented openCypher subset. The project's documented limitations
   (https://age.apache.org/docs/) list known gaps; check each against the planned query API.

2. **Docker image upgrade path.** The production `docker-compose.yml` uses `postgres:16-alpine`.
   Upgrading to `apache/age:PG16` requires a data migration (existing volumes are incompatible
   if the extension is not pre-installed). Define the migration procedure for production
   (Coolify-managed volume) before the implementation sprint.

3. **Shared graph node semantics under partial forget.** When a graph node is derived from
   multiple source `message_version_id` values, a forget event targeting one source must
   decide: (a) delete the node if all sources are forgotten, or (b) detach the forgotten
   source from the node and keep the node if other sources remain. The `graph_provenance`
   schema must be designed to support option (b). This decision is deferred to T10-06.

4. **Graph read behavior during cascade.** While a `graph_nodes` cascade layer is running,
   should graph queries be blocked (strictest privacy) or allowed to return stale results
   (possible leakage window)? If AGE is chosen, the cascade is synchronous within the
   PostgreSQL transaction, so this question only applies to long-running cascades; for Neo4j
   it is critical.

5. **Exact Phase 6 cards schema field names.** `prompts/PHASE6_PLAN_DRAFT.md` documents
   `knowledge_cards.source_message_version_ids` as a JSONB field. This RFC assumes that
   field name; if Phase 6 ships with a different name, the graph provenance contract in §5
   must be updated.

6. **Cost budget for AGE query plans.** PostgreSQL query planner does not natively optimize
   Cypher-over-SQL patterns from AGE. For large graph traversals, `EXPLAIN ANALYZE` on the
   generated SQL may reveal unexpected full-table scans. Index design for `ag_catalog` edge
   tables must be validated during the implementation sprint.

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
STATUS: DRAFT — methodology + qualitative analysis ratified; benchmark numbers pending execution
PROPOSER: Orchestrator B
DATE: 2026-05-02
REPLACES: Phase 10 §0a Decision 1 provisional (Apache AGE) — same recommendation, now formalized
DEPENDS_ON: Phase 6 closure (for cards schema final form), Phase 8 closure (for observations schema)
BENCH_SCRIPTS: bench/graph-store/ (in .worktrees/orch-B-experiments-rfc — not pushed to main)
PROVISIONAL_RECOMMENDATION: Apache AGE
FLIP_CONDITIONS: AGE P95 traversal > 500ms on 50k triples; OR required traversal pattern missing from AGE Cypher subset without SQL fallback; OR Neo4j cascade safety demonstrated under concurrent forgets
OPEN_QUESTIONS: 6 (see §7)
REVIEW_REQUIRED: Orchestrator A (cards angle), Orchestrator C (evals angle), human team-lead (final authority)
PROMOTION_GATE: Phase 6+8 closed + AUTHORIZED_SCOPE.md updated + benchmark numbers filled + Orch A + team-lead ack
```
