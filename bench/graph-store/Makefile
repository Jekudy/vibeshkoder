# Makefile for graph store benchmark
# Run from project root: make -f bench/graph-store/Makefile <target>
# Or from bench/graph-store/: make <target>
#
# Assumes a virtualenv is activated with the required drivers installed.
# See bench/graph-store/README.md for setup instructions.

BENCH_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
COMPOSE    := docker compose -f $(BENCH_DIR)docker-compose.yml
PYTHON     := python

.PHONY: seed seed-smoke bench-age bench-neo4j bench-graphiti bench-networkx \
        bench-all clean-containers syntax-check help

help:
	@echo "Graph store benchmark targets:"
	@echo "  seed             Generate full dataset (100k messages, 50k triples)"
	@echo "  seed-smoke       Generate small dataset for smoke testing (1k/500)"
	@echo "  bench-age        Run Apache AGE benchmark"
	@echo "  bench-neo4j      Run Neo4j benchmark"
	@echo "  bench-graphiti   Run Graphiti benchmark (Neo4j backend, LLM bypassed)"
	@echo "  bench-networkx   Run NetworkX benchmark (in-process)"
	@echo "  bench-all        Run all stores sequentially"
	@echo "  clean-containers Stop and remove bench containers and volumes"
	@echo "  syntax-check     Verify Python scripts compile"

seed:
	$(PYTHON) $(BENCH_DIR)seed.py

seed-smoke:
	$(PYTHON) $(BENCH_DIR)seed.py --message-count 1000 --triple-count 500

bench-age:
	@echo "=== Starting age-bench container ==="
	$(COMPOSE) up age-bench -d
	@echo "Waiting for PostgreSQL+AGE to be healthy ..."
	@until $(COMPOSE) ps age-bench | grep -q "healthy"; do \
		echo "  waiting ..."; sleep 3; done
	@echo "=== Running AGE benchmark ==="
	$(PYTHON) $(BENCH_DIR)harness.py --store age

bench-neo4j:
	@echo "=== Starting neo4j-bench container ==="
	$(COMPOSE) up neo4j-bench -d
	@echo "Waiting 30s for Neo4j JVM startup ..."
	@sleep 30
	@echo "=== Running Neo4j benchmark ==="
	$(PYTHON) $(BENCH_DIR)harness.py --store neo4j

bench-graphiti:
	@echo "=== Starting neo4j-bench container (Graphiti uses Neo4j backend) ==="
	$(COMPOSE) up neo4j-bench -d
	@echo "Waiting 30s for Neo4j JVM startup ..."
	@sleep 30
	@echo "=== Running Graphiti benchmark (LLM extraction bypassed) ==="
	$(PYTHON) $(BENCH_DIR)harness.py --store graphiti

bench-networkx:
	@echo "=== Running NetworkX benchmark (no Docker needed) ==="
	$(PYTHON) $(BENCH_DIR)harness.py --store networkx

bench-all: seed bench-networkx bench-age bench-neo4j bench-graphiti
	@echo ""
	@echo "=== All benchmarks complete ==="
	@echo "Results in bench/graph-store/results-*.jsonl"

clean-containers:
	$(COMPOSE) down -v

syntax-check:
	$(PYTHON) -m py_compile $(BENCH_DIR)seed.py
	$(PYTHON) -m py_compile $(BENCH_DIR)harness.py
	@echo "Syntax check passed."
