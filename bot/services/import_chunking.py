"""Chunking / rate-limit configuration for Telegram Desktop import apply (T2-NEW-F / issue #102).

This module provides:
- ChunkingConfig: frozen dataclass holding chunking parameters.
- load_chunking_config: reads config from environment with validation.
- acquire_advisory_lock: async context manager for PostgreSQL advisory locks
  keyed by ingestion_run_id.
- _derive_lock_id: deterministic int8 lock id from ingestion_run_id (internal, exported
  for testing).

The actual sleep between chunks (asyncio.sleep) and the apply loop belong to Stream Delta
(#103 — bot/services/import_apply.py). This module provides the config object and the
advisory lock primitive that #103 will consume.

Env vars:
    IMPORT_APPLY_CHUNK_SIZE     int  [1, 10000]   default 500
    IMPORT_APPLY_SLEEP_MS       int  [0, 60000]   default 100
    IMPORT_APPLY_ADVISORY_LOCK  bool              default true

Cross-stream boundary:
    DO NOT modify Alpha/Charlie files.
    DO NOT create or modify bot/services/import_apply.py (#103 territory).
"""

from __future__ import annotations

import hashlib
import os
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ─── Config dataclass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChunkingConfig:
    """Immutable chunking / rate-limit configuration for import apply.

    Fields:
        chunk_size:               Number of messages to process per DB transaction.
        sleep_between_chunks_ms:  Milliseconds to sleep between chunk transactions
                                  (to yield CPU time to live ingestion).
        use_advisory_lock:        If True, acquire_advisory_lock is called per
                                  ingestion_run_id before starting the apply loop.
    """

    chunk_size: int
    sleep_between_chunks_ms: int
    use_advisory_lock: bool


# ─── Config loader ────────────────────────────────────────────────────────────

_CHUNK_SIZE_MIN = 1
_CHUNK_SIZE_MAX = 10_000
_SLEEP_MS_MIN = 0
_SLEEP_MS_MAX = 60_000

_TRUTHY = frozenset({"1", "true", "yes"})
_FALSY = frozenset({"0", "false", "no"})


def load_chunking_config(env: dict[str, str] | None = None) -> ChunkingConfig:
    """Load ChunkingConfig from environment variables.

    When ``env`` is None (default), reads from ``os.environ``. Passing an explicit
    dict allows callers and tests to inject values without mutating the real env.

    Env vars (all optional, all have defaults):
        IMPORT_APPLY_CHUNK_SIZE:      int in [1, 10000], default 500.
        IMPORT_APPLY_SLEEP_MS:        int in [0, 60000], default 100.
        IMPORT_APPLY_ADVISORY_LOCK:   "1"/"true"/"yes" → True,
                                      "0"/"false"/"no" → False, default True.

    Raises:
        ValueError: on invalid or out-of-range values. Message names the affected field.
    """
    if env is None:
        env = dict(os.environ)

    chunk_size = _parse_int_env(
        env,
        key="IMPORT_APPLY_CHUNK_SIZE",
        default=500,
        field_name="chunk_size",
        min_val=_CHUNK_SIZE_MIN,
        max_val=_CHUNK_SIZE_MAX,
    )

    sleep_ms = _parse_int_env(
        env,
        key="IMPORT_APPLY_SLEEP_MS",
        default=100,
        field_name="sleep_between_chunks_ms",
        min_val=_SLEEP_MS_MIN,
        max_val=_SLEEP_MS_MAX,
    )

    use_lock = _parse_bool_env(env, key="IMPORT_APPLY_ADVISORY_LOCK", default=True)

    return ChunkingConfig(
        chunk_size=chunk_size,
        sleep_between_chunks_ms=sleep_ms,
        use_advisory_lock=use_lock,
    )


def _parse_int_env(
    env: dict[str, str],
    *,
    key: str,
    default: int,
    field_name: str,
    min_val: int,
    max_val: int,
) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise ValueError(
            f"Invalid value for {key}: expected integer, got {raw!r}. "
            f"(field: {field_name})"
        ) from None
    if not (min_val <= value <= max_val):
        raise ValueError(
            f"Invalid value for {key}: {value} is out of range [{min_val}, {max_val}]. "
            f"(field: {field_name})"
        )
    return value


def _parse_bool_env(env: dict[str, str], *, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    lower = raw.strip().lower()
    if lower in _TRUTHY:
        return True
    if lower in _FALSY:
        return False
    raise ValueError(
        f"Invalid value for {key}: expected one of "
        f"{sorted(_TRUTHY | _FALSY)}, got {raw!r}"
    )


# ─── Advisory lock helpers ────────────────────────────────────────────────────


def _derive_lock_id(ingestion_run_id: int) -> int:
    """Derive a deterministic PostgreSQL int8 advisory lock id from ingestion_run_id.

    Uses SHA-256 of the big-endian 8-byte representation, then takes the first 8 bytes
    of the digest and interprets them as a signed int64 (big-endian). This gives a
    uniform, collision-resistant mapping from any ingestion_run_id to a valid PostgreSQL
    advisory lock key.

    The result fits in a signed 64-bit integer (PostgreSQL bigint / int8), as required by
    pg_advisory_lock(bigint).
    """
    # Encode ingestion_run_id as 8 bytes (big-endian) before hashing.
    seed = ingestion_run_id.to_bytes(8, byteorder="big", signed=False)
    digest = hashlib.sha256(seed).digest()
    # Take the first 8 bytes and interpret as signed int64.
    (lock_id,) = struct.unpack(">q", digest[:8])
    return lock_id


@asynccontextmanager
async def acquire_advisory_lock(session: AsyncSession, ingestion_run_id: int):
    """Async context manager: take a PostgreSQL session-level advisory lock on enter,
    release it on exit (even if the body raises).

    The lock key is derived deterministically from ingestion_run_id via _derive_lock_id.
    This ensures:
    - Same ingestion_run_id → same lock (idempotent single-run protection).
    - Different ingestion_run_ids → different locks (parallel runs for distinct imports
      do not block each other).

    PostgreSQL session-level advisory locks:
    - pg_advisory_lock(key bigint) — blocks until the lock is acquired.
    - pg_advisory_unlock(key bigint) — releases the lock; called in finally block.
    - Re-acquisition from the same connection is idempotent (nest count incremented).

    Usage::

        async with acquire_advisory_lock(session, ingestion_run_id):
            # Only one process per ingestion_run_id can be here at a time.
            await run_apply_chunks(...)

    Raises:
        Any exception raised by the lock acquisition (e.g., DB connection errors) or
        by the body block — the finally clause always attempts pg_advisory_unlock.
    """
    lock_id = _derive_lock_id(ingestion_run_id)
    await session.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
    try:
        yield
    finally:
        await session.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
