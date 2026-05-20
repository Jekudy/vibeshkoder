# W0-D: Neo4j CI Service Infrastructure

## Summary

Adds Neo4j as a GitHub Actions service container (evals.yml + ci.yml), registers the
`graph_integration` pytest marker, adds a `neo4j_session` fixture with auto-skip when
`NEO4J_BOLT_URI` is absent, and provides smoke tests. Neo4j is already present in
docker-compose.yml under `profiles: [graph]` — updated to align env var naming (see Coordination Notes).

## Neo4j Image

`neo4j:5.20-community` — stable Neo4j 5.x Community Edition tag.

## Env Vars Introduced

| Variable | Value in CI | Where set |
|---|---|---|
| `NEO4J_BOLT_URI` | `bolt://localhost:7687` | GitHub Actions `env:` block in ci.yml and evals.yml |
| `NEO4J_AUTH_USER` | `neo4j` | Same |
| `NEO4J_AUTH_PASSWORD` | `ci-test-password-min-32-chars-ok!` | Same (32 chars; matches Neo4j 5 default policy) |

For local dev, these are set automatically by `docker-compose.dev.yml neo4j-dev` service or
`docker-compose.yml --profile graph neo4j` service. The fixture auto-skips if `NEO4J_BOLT_URI`
is not exported.

## New Pytest Marker

`graph_integration` — marks tests that require a live Neo4j bolt endpoint. Tests skip
automatically if `NEO4J_BOLT_URI` is not set. Registered in `[tool.pytest.ini_options]` in
`pyproject.toml`.

## New Fixture

`neo4j_session` in `tests/conftest.py` — async Neo4j session that cleans all nodes/edges
before each test (`MATCH (n) DETACH DELETE n`). Closes driver on teardown.

Safety guard: the fixture validates the `NEO4J_BOLT_URI` host against an allowlist
(`localhost`, `127.0.0.1`, `neo4j`, `neo4j-test`) and calls `pytest.fail()` if the host is
outside the list, preventing accidental wipes of staging/production data.

## Smoke Tests

`tests/integration/test_neo4j_fixture.py`:
- `test_neo4j_fixture_connects_and_returns_data` — MERGE + read a TestNode
- `test_neo4j_fixture_cleans_between_tests` — verify DB is empty at test start

Both tests are `@pytest.mark.graph_integration` and SKIP locally without Neo4j.

## Coordination Notes

### Env var naming alignment

`NEO4J_AUTH_PASSWORD` is the canonical variable name — it matches `Settings.NEO4J_AUTH_PASSWORD`
in `bot/config.py` and is used directly by `conftest.py` `neo4j_session` and the CI workflow
`env:` blocks.

`docker-compose.yml` resolves the password as
`${NEO4J_AUTH_PASSWORD:-${NEO4J_PASSWORD:-<default>}}` — it prefers the canonical name and
falls back to `NEO4J_PASSWORD` for backward compatibility with pre-W0-D setups.

This sprint unblocks:
- T10-03 (LLM extract) binding tests that write/read Neo4j
- W2-B (graph_projector) integration tests
- W2-C (purge worker) integration tests
- W2.5-D (graph_query) integration tests
- T10-09 cross-component binding tests in tests/evals/

## Files Changed

| File | Change |
|---|---|
| `.github/workflows/evals.yml` | Add neo4j service + NEO4J_* env vars; fix password to 32 chars |
| `.github/workflows/ci.yml` | Add neo4j service + NEO4J_* env vars; fix password to 32 chars |
| `docker-compose.yml` | Align NEO4J_AUTH to prefer NEO4J_AUTH_PASSWORD (canonical); add NEO4J_AUTH_USER |
| `tests/conftest.py` | Add neo4j_session fixture with host allowlist guard |
| `pyproject.toml` | Register graph_integration marker |
| `tests/integration/test_neo4j_fixture.py` | New smoke tests |
| `docs/rollout-fragments/phase10/W0-D.md` | This file |

## Docs Touched

- CLAUDE.md: UNCHANGED
- llms.txt: N/A (no new public API surface)
- IMPLEMENTATION_STATUS: deferred to W3-E per plan
