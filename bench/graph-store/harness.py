"""Graph store benchmark harness.

Runs three benchmark operations against the selected graph store:
  1. Bulk insert 50k triples
  2. 3-hop random traversal (1,000 queries, P50/P95/P99 latency)
  3. Forget cascade simulation (1,000 source deletes + derived graph purge)

Results are written to bench/graph-store/results-<store>-<timestamp>.jsonl,
one JSON object per operation.

Prerequisites (per store):
  age:      docker compose -f bench/graph-store/docker-compose.yml up age-bench -d
            pip install psycopg[binary]
  neo4j:    docker compose -f bench/graph-store/docker-compose.yml up neo4j-bench -d
            pip install neo4j
  graphiti: docker compose -f bench/graph-store/docker-compose.yml up neo4j-bench -d
            pip install neo4j  (Graphiti LLM extraction is bypassed in this benchmark)
  networkx: pip install networkx  (no Docker service needed)

Usage:
    python bench/graph-store/harness.py --store age
    python bench/graph-store/harness.py --store neo4j
    python bench/graph-store/harness.py --store graphiti
    python bench/graph-store/harness.py --store networkx
    python bench/graph-store/harness.py --store age --data-dir /tmp/bench-data
    python bench/graph-store/harness.py --store age --traversal-queries 100  # faster smoke run
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR: str = str(Path(__file__).parent / "data")
DEFAULT_RESULTS_DIR: str = str(Path(__file__).parent)
DEFAULT_TRAVERSAL_QUERIES: int = 1_000
DEFAULT_FORGET_SOURCES: int = 1_000
BENCH_SEED: int = int(os.environ.get("BENCH_SEED", "42"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_triples(data_dir: str) -> list[dict]:
    path = Path(data_dir) / "triples.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"Triples file not found: {path}\n"
            "Run: python bench/graph-store/seed.py first."
        )
    triples = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                triples.append(json.loads(line))
    return triples


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, int(len(sorted_values) * p / 100) - 1)
    return sorted_values[idx]


def _write_result(results_dir: str, store: str, record: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(results_dir) / f"results-{store}-{ts}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  → result written to {path}")


def _host_meta() -> dict:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "machine": platform.machine(),
    }


# ---------------------------------------------------------------------------
# Apache AGE store
# ---------------------------------------------------------------------------


def _bench_age(triples: list[dict], n_traversal: int, n_forget: int) -> list[dict]:
    """Benchmark Apache AGE via psycopg (sync) + raw SQL/Cypher."""
    try:
        import psycopg  # type: ignore
    except ImportError:
        raise RuntimeError(
            "psycopg not installed. Run: pip install 'psycopg[binary]'"
        )

    dsn = os.environ.get(
        "BENCH_AGE_DSN",
        "host=127.0.0.1 port=5440 dbname=bench_age user=bench password=bench",
    )
    results: list[dict] = []

    with psycopg.connect(dsn) as conn:
        # Setup: load AGE extension and create graph
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS age;")
            cur.execute("LOAD 'age';")
            cur.execute("SET search_path = ag_catalog, '$user', public;")
            # Drop and recreate graph for clean benchmark
            cur.execute(
                "SELECT * FROM ag_catalog.drop_graph('bench', true) WHERE EXISTS "
                "(SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'bench');"
            )
        conn.commit()

        with psycopg.connect(dsn) as conn2:
            with conn2.cursor() as cur2:
                cur2.execute("LOAD 'age';")
                cur2.execute("SET search_path = ag_catalog, '$user', public;")
                cur2.execute(
                    "SELECT * FROM ag_catalog.create_graph('bench');"
                )
            conn2.commit()

        # ---- Operation 1: Bulk insert ----
        print(f"  [AGE] inserting {len(triples)} triples ...")
        t_start = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute("LOAD 'age';")
            cur.execute("SET search_path = ag_catalog, '$user', public;")
            for t in triples:
                cypher = (
                    "SELECT * FROM ag_catalog.cypher('bench', $$"
                    f"MERGE (s:Entity {{eid: '{t['subject']}'}}) "
                    f"MERGE (o:Entity {{eid: '{t['object']}'}}) "
                    f"CREATE (s)-[r:{t['predicate']} {{triple_id: '{t['id']}', "
                    f"source_mv_id: '{t['source_message_version_id']}', "
                    f"confidence: {t['confidence']}}}]->(o) "
                    "RETURN r"
                    "$$) AS (r agtype);"
                )
                cur.execute(cypher)
        conn.commit()
        t_insert = time.perf_counter() - t_start
        print(f"  [AGE] insert done in {t_insert:.2f}s ({len(triples)/t_insert:.0f} triples/s)")
        results.append({
            "operation": "bulk_insert",
            "store": "age",
            "triple_count": len(triples),
            "total_seconds": round(t_insert, 4),
            "triples_per_second": round(len(triples) / t_insert, 1),
        })

        # ---- Operation 2: 3-hop traversal ----
        print(f"  [AGE] running {n_traversal} traversal queries ...")
        # Collect unique entity ids from triples
        all_subjects = list({t["subject"] for t in triples})
        rng = random.Random(BENCH_SEED)
        start_entities = rng.sample(all_subjects, min(n_traversal, len(all_subjects)))
        latencies: list[float] = []
        with conn.cursor() as cur:
            cur.execute("LOAD 'age';")
            cur.execute("SET search_path = ag_catalog, '$user', public;")
            for eid in start_entities:
                q_start = time.perf_counter()
                cypher = (
                    "SELECT * FROM ag_catalog.cypher('bench', $$"
                    f"MATCH (s:Entity {{eid: '{eid}'}})-[*1..3]->(t:Entity) "
                    "RETURN DISTINCT t.eid LIMIT 50"
                    "$$) AS (eid agtype);"
                )
                cur.execute(cypher)
                cur.fetchall()
                latencies.append((time.perf_counter() - q_start) * 1000)  # ms
        latencies.sort()
        results.append({
            "operation": "traversal_3hop",
            "store": "age",
            "query_count": len(latencies),
            "p50_ms": round(_percentile(latencies, 50), 3),
            "p95_ms": round(_percentile(latencies, 95), 3),
            "p99_ms": round(_percentile(latencies, 99), 3),
        })
        print(f"  [AGE] traversal P50={latencies[len(latencies)//2]:.1f}ms")

        # ---- Operation 3: Forget cascade ----
        print(f"  [AGE] simulating forget cascade for {n_forget} sources ...")
        source_ids = list({t["source_message_version_id"] for t in triples})
        rng2 = random.Random(BENCH_SEED + 1)
        forget_sources = rng2.sample(source_ids, min(n_forget, len(source_ids)))
        t_start = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute("LOAD 'age';")
            cur.execute("SET search_path = ag_catalog, '$user', public;")
            for mv_id in forget_sources:
                cypher = (
                    "SELECT * FROM ag_catalog.cypher('bench', $$"
                    f"MATCH ()-[r {{source_mv_id: '{mv_id}'}}]-() "
                    "DELETE r"
                    "$$) AS (result agtype);"
                )
                cur.execute(cypher)
        conn.commit()
        t_cascade = time.perf_counter() - t_start
        results.append({
            "operation": "forget_cascade",
            "store": "age",
            "forgotten_source_count": len(forget_sources),
            "total_seconds": round(t_cascade, 4),
            "ms_per_source": round(t_cascade / len(forget_sources) * 1000, 3),
        })
        print(f"  [AGE] cascade done in {t_cascade:.2f}s")

    return results


# ---------------------------------------------------------------------------
# Neo4j store
# ---------------------------------------------------------------------------


def _bench_neo4j(
    triples: list[dict], n_traversal: int, n_forget: int, db_name: str = "neo4j"
) -> list[dict]:
    """Benchmark Neo4j via the official Python bolt driver."""
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        raise RuntimeError(
            "neo4j driver not installed. Run: pip install neo4j"
        )

    uri = os.environ.get("BENCH_NEO4J_URI", "bolt://127.0.0.1:7688")
    results: list[dict] = []

    driver = GraphDatabase.driver(uri, auth=None)
    try:
        with driver.session(database=db_name) as session:
            # Clean slate
            session.run("MATCH (n) DETACH DELETE n")

            # ---- Operation 1: Bulk insert ----
            print(f"  [Neo4j/{db_name}] inserting {len(triples)} triples ...")
            t_start = time.perf_counter()
            # Batch in chunks of 1000 for throughput
            batch_size = 1_000
            for i in range(0, len(triples), batch_size):
                batch = triples[i : i + batch_size]
                session.run(
                    """
                    UNWIND $triples AS t
                    MERGE (s:Entity {eid: t.subject})
                    MERGE (o:Entity {eid: t.object})
                    CREATE (s)-[r:RELATION {
                        triple_id: t.id,
                        predicate: t.predicate,
                        source_mv_id: t.source_message_version_id,
                        confidence: t.confidence
                    }]->(o)
                    """,
                    triples=batch,
                )
            t_insert = time.perf_counter() - t_start
            print(
                f"  [Neo4j/{db_name}] insert done in {t_insert:.2f}s "
                f"({len(triples)/t_insert:.0f} triples/s)"
            )
            results.append({
                "operation": "bulk_insert",
                "store": f"neo4j_{db_name}",
                "triple_count": len(triples),
                "total_seconds": round(t_insert, 4),
                "triples_per_second": round(len(triples) / t_insert, 1),
            })

            # ---- Operation 2: 3-hop traversal ----
            print(f"  [Neo4j/{db_name}] running {n_traversal} traversal queries ...")
            all_subjects = list({t["subject"] for t in triples})
            rng = random.Random(BENCH_SEED)
            start_entities = rng.sample(all_subjects, min(n_traversal, len(all_subjects)))
            latencies: list[float] = []
            for eid in start_entities:
                q_start = time.perf_counter()
                result = session.run(
                    "MATCH (s:Entity {eid: $eid})-[*1..3]->(t:Entity) "
                    "RETURN DISTINCT t.eid LIMIT 50",
                    eid=eid,
                )
                list(result)  # consume
                latencies.append((time.perf_counter() - q_start) * 1000)
            latencies.sort()
            results.append({
                "operation": "traversal_3hop",
                "store": f"neo4j_{db_name}",
                "query_count": len(latencies),
                "p50_ms": round(_percentile(latencies, 50), 3),
                "p95_ms": round(_percentile(latencies, 95), 3),
                "p99_ms": round(_percentile(latencies, 99), 3),
            })
            print(f"  [Neo4j/{db_name}] traversal P50={latencies[len(latencies)//2]:.1f}ms")

            # ---- Operation 3: Forget cascade ----
            print(f"  [Neo4j/{db_name}] simulating forget cascade for {n_forget} sources ...")
            source_ids = list({t["source_message_version_id"] for t in triples})
            rng2 = random.Random(BENCH_SEED + 1)
            forget_sources = rng2.sample(source_ids, min(n_forget, len(source_ids)))
            t_start = time.perf_counter()
            # Delete edges derived from forgotten sources in batches
            batch_size = 100
            for i in range(0, len(forget_sources), batch_size):
                batch = forget_sources[i : i + batch_size]
                session.run(
                    "MATCH ()-[r:RELATION {source_mv_id: $mv_id}]-() DELETE r",
                    # Note: for a production cascade, each source would be processed
                    # individually in a transaction. Here we iterate over the batch.
                    mv_id=batch[0],
                )
                for mv_id in batch[1:]:
                    session.run(
                        "MATCH ()-[r:RELATION {source_mv_id: $mv_id}]-() DELETE r",
                        mv_id=mv_id,
                    )
            t_cascade = time.perf_counter() - t_start
            results.append({
                "operation": "forget_cascade",
                "store": f"neo4j_{db_name}",
                "forgotten_source_count": len(forget_sources),
                "total_seconds": round(t_cascade, 4),
                "ms_per_source": round(t_cascade / len(forget_sources) * 1000, 3),
            })
            print(f"  [Neo4j/{db_name}] cascade done in {t_cascade:.2f}s")
    finally:
        driver.close()

    return results


# ---------------------------------------------------------------------------
# Graphiti store (Neo4j backend, LLM extraction bypassed)
# ---------------------------------------------------------------------------


def _bench_graphiti(triples: list[dict], n_traversal: int, n_forget: int) -> list[dict]:
    """Benchmark Graphiti's backing store (Neo4j) with LLM extraction bypassed.

    Graphiti uses Neo4j as its storage backend. For this benchmark we measure the
    raw Neo4j write/read path using a separate database ('bench_graphiti') to
    isolate results from the base neo4j benchmark. LLM extraction is bypassed —
    triples are written directly as Neo4j nodes/edges without Graphiti's entity
    resolution step.

    This benchmark answers: "what is the Neo4j storage performance Graphiti would
    experience?" — it does NOT benchmark Graphiti's LLM extraction overhead.
    """
    print(
        "  [Graphiti] NOTE: LLM extraction is bypassed. "
        "Measuring raw Neo4j storage path only (bench_graphiti database)."
    )
    return _bench_neo4j(triples, n_traversal, n_forget, db_name="bench_graphiti")


# ---------------------------------------------------------------------------
# NetworkX store (in-process)
# ---------------------------------------------------------------------------


def _bench_networkx(triples: list[dict], n_traversal: int, n_forget: int) -> list[dict]:
    """Benchmark NetworkX in-process graph."""
    try:
        import networkx as nx  # type: ignore
    except ImportError:
        raise RuntimeError(
            "networkx not installed. Run: pip install networkx"
        )

    results: list[dict] = []
    G: Any = nx.MultiDiGraph()

    # ---- Operation 1: Bulk insert ----
    print(f"  [NetworkX] inserting {len(triples)} triples ...")
    t_start = time.perf_counter()
    for t in triples:
        G.add_edge(
            t["subject"],
            t["object"],
            key=t["id"],
            predicate=t["predicate"],
            source_mv_id=t["source_message_version_id"],
            confidence=t["confidence"],
        )
    t_insert = time.perf_counter() - t_start
    print(f"  [NetworkX] insert done in {t_insert:.2f}s ({len(triples)/t_insert:.0f} triples/s)")
    results.append({
        "operation": "bulk_insert",
        "store": "networkx",
        "triple_count": len(triples),
        "total_seconds": round(t_insert, 4),
        "triples_per_second": round(len(triples) / t_insert, 1),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
    })

    # ---- Operation 2: 3-hop traversal ----
    print(f"  [NetworkX] running {n_traversal} traversal queries ...")
    all_nodes = list(G.nodes())
    rng = random.Random(BENCH_SEED)
    start_nodes = rng.sample(all_nodes, min(n_traversal, len(all_nodes)))
    latencies: list[float] = []
    for node in start_nodes:
        q_start = time.perf_counter()
        # BFS up to depth 3; collect up to 50 reachable nodes
        visited: set[str] = set()
        frontier = {node}
        for _depth in range(3):
            next_frontier: set[str] = set()
            for n in frontier:
                neighbors = set(G.successors(n)) - visited - {node}
                next_frontier.update(neighbors)
            visited.update(next_frontier)
            frontier = next_frontier
            if len(visited) >= 50:
                break
        latencies.append((time.perf_counter() - q_start) * 1000)
    latencies.sort()
    results.append({
        "operation": "traversal_3hop",
        "store": "networkx",
        "query_count": len(latencies),
        "p50_ms": round(_percentile(latencies, 50), 3),
        "p95_ms": round(_percentile(latencies, 95), 3),
        "p99_ms": round(_percentile(latencies, 99), 3),
    })
    print(f"  [NetworkX] traversal P50={latencies[len(latencies)//2]:.1f}ms")

    # ---- Operation 3: Forget cascade ----
    print(f"  [NetworkX] simulating forget cascade for {n_forget} sources ...")
    source_ids = list({t["source_message_version_id"] for t in triples})
    rng2 = random.Random(BENCH_SEED + 1)
    forget_sources = rng2.sample(source_ids, min(n_forget, len(source_ids)))
    t_start = time.perf_counter()
    for mv_id in forget_sources:
        edges_to_remove = [
            (u, v, k)
            for u, v, k, d in G.edges(keys=True, data=True)
            if d.get("source_mv_id") == mv_id
        ]
        G.remove_edges_from(edges_to_remove)
    t_cascade = time.perf_counter() - t_start
    results.append({
        "operation": "forget_cascade",
        "store": "networkx",
        "forgotten_source_count": len(forget_sources),
        "total_seconds": round(t_cascade, 4),
        "ms_per_source": round(t_cascade / len(forget_sources) * 1000, 3),
    })
    print(f"  [NetworkX] cascade done in {t_cascade:.2f}s")

    return results


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

STORE_FUNCS = {
    "age": _bench_age,
    "neo4j": _bench_neo4j,
    "graphiti": _bench_graphiti,
    "networkx": _bench_networkx,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Graph store benchmark harness")
    parser.add_argument(
        "--store",
        choices=list(STORE_FUNCS.keys()),
        required=True,
        help="Which store to benchmark",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Directory containing triples.jsonl from seed.py",
    )
    parser.add_argument(
        "--results-dir",
        default=DEFAULT_RESULTS_DIR,
        help="Directory for results JSONL output",
    )
    parser.add_argument(
        "--traversal-queries",
        type=int,
        default=DEFAULT_TRAVERSAL_QUERIES,
        help="Number of traversal queries to run (default 1000)",
    )
    parser.add_argument(
        "--forget-sources",
        type=int,
        default=DEFAULT_FORGET_SOURCES,
        help="Number of source IDs to simulate forget for (default 1000)",
    )
    args = parser.parse_args()

    print(f"\n=== Graph store benchmark: {args.store} ===")
    print(f"Data dir: {args.data_dir}")
    print(f"Results dir: {args.results_dir}")

    print(f"\nLoading triples from {args.data_dir} ...")
    triples = _load_triples(args.data_dir)
    print(f"Loaded {len(triples)} triples.\n")

    bench_fn = STORE_FUNCS[args.store]
    raw_results = bench_fn(triples, args.traversal_queries, args.forget_sources)

    # Annotate results with host metadata and timestamp
    ts = datetime.now(timezone.utc).isoformat()
    meta = _host_meta()
    for record in raw_results:
        record["bench_timestamp"] = ts
        record["host"] = meta
        record["data_dir"] = args.data_dir
        _write_result(args.results_dir, args.store, record)

    print(f"\n=== Benchmark complete: {args.store} ===")
    for r in raw_results:
        op = r.get("operation", "?")
        if op == "bulk_insert":
            print(f"  bulk_insert: {r['total_seconds']:.2f}s ({r['triples_per_second']:.0f} t/s)")
        elif op == "traversal_3hop":
            print(f"  traversal:   P50={r['p50_ms']}ms P95={r['p95_ms']}ms P99={r['p99_ms']}ms")
        elif op == "forget_cascade":
            print(f"  cascade:     {r['total_seconds']:.2f}s total ({r['ms_per_source']}ms/src)")


if __name__ == "__main__":
    main()
