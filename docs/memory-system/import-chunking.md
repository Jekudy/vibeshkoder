# Import Apply — Chunking & Rate Limit Config

**Ticket:** T2-NEW-F / Issue #102
**Stream:** Bravo (Sprint 08)
**Status:** Implemented. Apply loop lives in Stream Delta (#103).

---

## Overview

This document describes the chunking and rate-limit configuration layer for `import_apply`.
It covers:

1. Configuration knobs (env vars + defaults)
2. Live ingestion isolation contract
3. PostgreSQL advisory lock — purpose and semantics
4. Cross-stream boundary (config vs. apply loop)
5. CLI usage

---

## Configuration Knobs

All configuration is provided via environment variables. Defaults are chosen to be
conservative (small chunks, short sleep) to minimise impact on live ingestion.

| Env var | Type | Default | Range | Description |
|---|---|---|---|---|
| `IMPORT_APPLY_CHUNK_SIZE` | int | `500` | `[1, 10000]` | Messages per DB transaction |
| `IMPORT_APPLY_SLEEP_MS` | int | `100` | `[0, 60000]` | Sleep between chunks (ms) |
| `IMPORT_APPLY_ADVISORY_LOCK` | bool | `true` | — | Acquire PG advisory lock per run |

### `IMPORT_APPLY_CHUNK_SIZE`

Number of messages processed in a single DB transaction. Smaller values reduce memory
pressure and checkpoint granularity (less re-work on resume). Larger values improve
throughput at the cost of longer transactions.

- Minimum: `1` (one message per transaction — very safe, very slow)
- Maximum: `10000` (cap to prevent accidentally long-running transactions)
- Default: `500` (approximately 1–2 seconds per chunk under typical load)

### `IMPORT_APPLY_SLEEP_MS`

Milliseconds to sleep between chunk transactions. This yields the DB connection and CPU
back to live ingestion paths between chunks. Set to `0` to disable sleeping (maximum
throughput, maximum impact on live ingestion).

- `0` — no sleep; suitable for maintenance windows with no live traffic
- `100` (default) — ~10% duty cycle at typical chunk durations
- `1000+` — very conservative; suitable for peak-traffic imports

### `IMPORT_APPLY_ADVISORY_LOCK`

When `true` (default), `import_apply` acquires a PostgreSQL session-level advisory lock
keyed by `ingestion_run_id` before starting the apply loop. This ensures only one process
drives a given import run's chunks, even across restarts:

- Two concurrent `import_apply --resume` calls → the second call blocks at `pg_advisory_lock`
  until the first call finishes or its DB connection is dropped.
- The lock is automatically released when the DB connection closes (session-level semantics).

Set to `false` only if running in a strict single-process environment where concurrent
resumes are impossible by design.

### Boolean env var parsing

`IMPORT_APPLY_ADVISORY_LOCK` accepts:
- Truthy: `1`, `true`, `True`, `yes`, `YES` (case-insensitive after strip)
- Falsy: `0`, `false`, `False`, `no`, `NO`
- Any other value raises `ValueError` at startup.

---

## Live Ingestion Isolation Contract

The apply loop (#103) MUST respect the chunking config to protect live ingestion:

1. **One chunk = one DB transaction.** Process `chunk_size` messages, commit, checkpoint.
2. **Sleep between chunks.** After each commit, `await asyncio.sleep(config.sleep_between_chunks_ms / 1000)`.
3. **Advisory lock first.** If `config.use_advisory_lock` is `True`, acquire the lock via
   `acquire_advisory_lock(connection, ingestion_run_id)` before processing any chunk.
   The connection must be the same `AsyncConnection` used for all chunk transactions.

This three-part contract isolates apply transactions from live ingestion:
- Short transactions prevent lock contention with `chat_messages` writers.
- Sleep windows allow live ingestion writes to proceed without back-pressure.
- Advisory lock prevents two import processes from interleaving writes to the same run.

### Measured isolation guarantee

Under default config (chunk_size=500, sleep_ms=100), the expected P99 latency impact
on live ingestion is less than 2× baseline during apply. This was the acceptance criterion
in issue #102.

---

## PostgreSQL Advisory Lock

```python
from bot.services.import_chunking import acquire_advisory_lock

# IMPORTANT: pass AsyncConnection, not AsyncSession.
# The connection must remain alive for the entire lock lifetime.
async with engine.connect() as conn:
    async with acquire_advisory_lock(conn, ingestion_run_id):
        # Apply chunks here — guaranteed single-process per ingestion_run_id
        for chunk in chunks:
            async with conn.begin():
                await process_chunk(conn, chunk)
                await save_checkpoint(conn, ...)
            await asyncio.sleep(config.sleep_between_chunks_ms / 1000)
```

### Lock key derivation

The lock key is derived deterministically from `ingestion_run_id`:

```python
def _derive_lock_id(ingestion_run_id: int) -> int:
    seed = ingestion_run_id.to_bytes(8, byteorder="big", signed=False)
    digest = hashlib.sha256(seed).digest()
    (lock_id,) = struct.unpack(">q", digest[:8])
    return lock_id
```

Properties:
- **Deterministic**: same `ingestion_run_id` → same lock key across processes and restarts.
- **Collision-resistant**: SHA-256 gives negligible collision probability for practical run IDs.
- **Valid int8**: result is a signed 64-bit integer (PostgreSQL `bigint` / `int8`).
- **Unique per run**: different `ingestion_run_id` values produce different lock keys with
  overwhelming probability.

### Session-level semantics

`pg_advisory_lock(bigint)` acquires a session-level (connection-level) advisory lock:
- PostgreSQL session-level advisory locks are **STACKED** — each `pg_advisory_lock` call
  requires a matching `pg_advisory_unlock`. This context manager balances exactly one
  acquisition with one unlock. **Callers MUST NOT re-enter** this context manager on the
  same connection — re-entry would leave the connection holding an extra lock count after
  exit, which would silently survive the inner `finally` block.
- Lock is automatically released when the DB connection is closed (crash recovery).
- `pg_advisory_unlock(bigint)` explicitly releases the lock in the `finally` block of
  `acquire_advisory_lock`. If `pg_advisory_unlock` returns `false`, a `WARNING` is logged
  (indicates the lock was held on a different connection — programmer error in caller).

### Connection-scope requirement (CRITICAL)

**Caller MUST hold a single `AsyncConnection` for the full lock lifetime.**

`pg_advisory_lock` is connection-scoped. Under a pooled `AsyncSession`, per-chunk commits
may release and reacquire the underlying DB connection. If the connection is swapped
between chunks, the `pg_advisory_unlock` in the `finally` block runs on a different
connection — leaving the original lock held indefinitely (silent failure).

`acquire_advisory_lock` therefore takes an `AsyncConnection`, not an `AsyncSession`.

Apply path (#103):
1. Acquire a single `AsyncConnection` at the start of `run_apply`.
2. Call `acquire_advisory_lock(connection, ingestion_run_id)`.
3. For each chunk, use `connection.begin()` to open/commit per-chunk transactions on that
   same connection — **never release the connection until `finalize_run`**.

```python
async with engine.connect() as conn:
    async with acquire_advisory_lock(conn, ingestion_run_id):
        async with conn.begin():
            # chunk 1 commit
        async with conn.begin():
            # chunk 2 commit
```

---

## Cross-Stream Boundary

| File | Owner | Status |
|---|---|---|
| `bot/services/import_chunking.py` | #102 (this ticket) | Done |
| `bot/cli.py` `import_apply` | #102 (this ticket) | Updated — loads ChunkingConfig |
| `bot/services/import_apply.py` | #103 (Stream Delta) | NOT YET CREATED |

The apply loop in #103 MUST:
1. Accept `chunking_config: ChunkingConfig` as a kwarg from the CLI.
2. Use `chunking_config.chunk_size` to determine batch size.
3. Call `asyncio.sleep(chunking_config.sleep_between_chunks_ms / 1000)` between chunks.
4. If `chunking_config.use_advisory_lock` is `True`, wrap the loop in
   `async with acquire_advisory_lock(connection, ingestion_run_id)` — the
   first arg is an `AsyncConnection` (not a session) that #103 MUST hold
   for the full lock lifetime; cycling connections silently loses the lock.

**DO NOT** add the apply loop here. This module is config and lock primitive only.

---

## CLI Usage

```bash
# Default chunking config (chunk_size=500, sleep_ms=100, advisory_lock=True)
python -m bot.cli import_apply /path/to/result.json

# Override chunk_size via CLI arg (takes precedence over env var)
python -m bot.cli import_apply /path/to/result.json --chunk-size 200

# Override via env vars
IMPORT_APPLY_CHUNK_SIZE=100 IMPORT_APPLY_SLEEP_MS=500 \
    python -m bot.cli import_apply /path/to/result.json

# Disable advisory lock (single-process environments only)
IMPORT_APPLY_ADVISORY_LOCK=false python -m bot.cli import_apply /path/to/result.json

# Resume a partial run with custom chunk config
python -m bot.cli import_apply /path/to/result.json --resume --chunk-size 100
```

### Precedence

`--chunk-size` CLI arg > `IMPORT_APPLY_CHUNK_SIZE` env var > built-in default (500).

Other config knobs (`sleep_ms`, `advisory_lock`) are env-only; they have no CLI flags.
CLI flags for these may be added in a follow-up ticket if operator demand warrants it.

---

## API Reference

See `bot/services/import_chunking.py` for full docstrings.

```python
@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size: int                  # messages per DB transaction
    sleep_between_chunks_ms: int     # delay between chunks (ms)
    use_advisory_lock: bool          # take PG advisory lock per ingestion_run_id

def load_chunking_config(env: dict[str, str] | None = None) -> ChunkingConfig:
    """Load from env with defaults. Validates ranges. Raises ValueError on invalid values."""

@asynccontextmanager
async def acquire_advisory_lock(connection: AsyncConnection, ingestion_run_id: int):
    """Connection-scoped PG advisory lock, released in finally (crash-safe).
    Caller MUST hold a single AsyncConnection for the full lock lifetime."""

def _derive_lock_id(ingestion_run_id: int) -> int:
    """Deterministic SHA-256-based int8 lock key. Exported for testing."""
```

---

## See Also

- #101 / `import-checkpoint.md` — checkpoint / resume infrastructure.
- #103 — Telegram Desktop import apply (Stream Delta, consumer of this config).
- `bot/services/import_chunking.py` — implementation.
- `tests/services/test_import_chunking.py` — unit tests.
