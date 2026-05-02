# Graph Store Benchmark

Benchmark scripts for RFC-001: graph store choice for Shkoderbot Phase 10.

These scripts live in `.worktrees/orch-B-experiments-rfc` and are **NOT pushed to main**.
They are part of the experiments worktree for pre-promotion design work per
`docs/memory-system/AUTHORIZED_SCOPE.md §"Conditionally authorized: Phase 9, Phase 10 (gated)"`.

## Overview

| Script | Purpose |
|--------|---------|
| `seed.py` | Generate deterministic synthetic dataset (100k message_versions, 50k triples) |
| `harness.py` | Run benchmark operations against a store, write JSONL results |
| `docker-compose.yml` | Service overlays for AGE and Neo4j benchmark containers |
| `Makefile` | Convenience targets |

Results land in `bench/graph-store/results-<store>-<timestamp>.jsonl`.

## Dependencies

The bench scripts use store-specific drivers that are **NOT** in the project's `pyproject.toml`
(they are throwaway benchmark deps). Install them in a venv:

```bash
cd .worktrees/orch-B-experiments-rfc
python -m venv .bench-venv
source .bench-venv/bin/activate

# For AGE benchmark:
pip install "psycopg[binary]"

# For Neo4j / Graphiti benchmark:
pip install neo4j

# For NetworkX benchmark:
pip install networkx
```

## Quick start

### 1. Generate the dataset (one-time, ~1-2 minutes)

```bash
python bench/graph-store/seed.py
# Output: bench/graph-store/data/{message_versions,triples,entities}.jsonl + seed_meta.json
```

Smoke-test with a small dataset first:

```bash
python bench/graph-store/seed.py --message-count 1000 --triple-count 500
```

### 2. Run benchmark for a specific store

#### Apache AGE

```bash
# Start the container
docker compose -f bench/graph-store/docker-compose.yml up age-bench -d
# Wait for healthy (pg_isready)
docker compose -f bench/graph-store/docker-compose.yml ps age-bench

# Run benchmark
python bench/graph-store/harness.py --store age
```

#### Neo4j

```bash
docker compose -f bench/graph-store/docker-compose.yml up neo4j-bench -d
# Wait ~20s for Neo4j to start (JVM startup is slow)
docker compose -f bench/graph-store/docker-compose.yml ps neo4j-bench

python bench/graph-store/harness.py --store neo4j
```

#### Graphiti (Neo4j backend, LLM extraction bypassed)

```bash
# Uses the same neo4j-bench container, different database
docker compose -f bench/graph-store/docker-compose.yml up neo4j-bench -d
python bench/graph-store/harness.py --store graphiti
```

#### NetworkX (no Docker needed)

```bash
python bench/graph-store/harness.py --store networkx
```

### 3. Run all stores

```bash
make bench-all
```

### 4. Stop containers

```bash
docker compose -f bench/graph-store/docker-compose.yml down
```

## Makefile targets

| Target | Description |
|--------|-------------|
| `make seed` | Generate full dataset (100k messages, 50k triples) |
| `make seed-smoke` | Generate small dataset for smoke testing |
| `make bench-age` | Run AGE benchmark (requires age-bench container) |
| `make bench-neo4j` | Run Neo4j benchmark (requires neo4j-bench container) |
| `make bench-graphiti` | Run Graphiti benchmark (requires neo4j-bench container) |
| `make bench-networkx` | Run NetworkX benchmark (in-process, no Docker) |
| `make bench-all` | Run all stores sequentially (starts containers, waits, runs) |
| `make clean-containers` | Stop and remove bench containers and volumes |
| `make syntax-check` | Verify Python scripts compile without errors |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BENCH_SEED` | `42` | Random seed for dataset generation and benchmark sampling |
| `BENCH_AGE_DSN` | `host=127.0.0.1 port=5440 dbname=bench_age user=bench password=bench` | psycopg DSN for AGE |
| `BENCH_NEO4J_URI` | `bolt://127.0.0.1:7688` | Bolt URI for Neo4j |

## Result format

Each `results-<store>-<timestamp>.jsonl` contains one JSON object per operation:

```json
{"operation": "bulk_insert", "store": "age", "triple_count": 50000, "total_seconds": 42.1, "triples_per_second": 1187.6, "bench_timestamp": "...", "host": {...}}
{"operation": "traversal_3hop", "store": "age", "query_count": 1000, "p50_ms": 12.3, "p95_ms": 45.6, "p99_ms": 98.7, "bench_timestamp": "...", "host": {...}}
{"operation": "forget_cascade", "store": "age", "forgotten_source_count": 1000, "total_seconds": 8.4, "ms_per_source": 8.4, "bench_timestamp": "...", "host": {...}}
```

## Important notes

1. **No real message content** — `seed.py` generates synthetic placeholder text. The benchmark
   does not process, store, or transmit any real community data.

2. **No LLM calls** — triple extraction uses a deterministic rule-based extractor. Invariant #2
   is respected.

3. **Graphiti caveat** — the Graphiti benchmark bypasses LLM extraction and measures raw Neo4j
   I/O only. It does NOT benchmark Graphiti's entity resolution or temporal graph management.
   This is intentional: LLM extraction would require an external API call, violating invariant #2.

4. **AGE image** — the `apache/age:PG16` image is community-maintained. If the tag is unavailable,
   check https://hub.docker.com/r/apache/age/tags for the latest PG16-compatible tag.

5. **Neo4j JVM warmup** — allow 20-30 seconds after `docker compose up` before running the
   Neo4j benchmark. The JVM startup creates noise in the first few queries.

6. **Same hardware** — run all store benchmarks on the same host to make results comparable.
   Document host specs (they appear automatically in the results JSONL `host` field).
