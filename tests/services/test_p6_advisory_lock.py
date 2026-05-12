"""T6-01 Phase 6 advisory-lock helper (``_p6_mvid_advisory_lock_id``).

PHASE6_PLAN.md §5.A.5 + §5.C step 2 define a single source of truth for the
per-message-version advisory lock id used by:

* ``_cascade_card_sources_on_forget`` (cascade demote when sources are forgotten)
* the ``/approve`` transaction protocol (serialization point with cascade)

Derivation contract::

    mvid_lock_id = signed_int64(sha256(f'p6:mvid:{mvid}'))

Both callers MUST share this derivation byte-for-byte, otherwise the lock
namespace splits and the §5.A.5 race window (Codex H-Cdx-2 round 2/3) reopens.

These tests pin the contract:

* **determinism** — same input → same output across 1000 runs.
* **signed-int64 range** — result fits in ``[-2**63, 2**63-1]`` so it can be
  passed to ``pg_advisory_xact_lock(bigint)`` directly.
* **collision-resistance smoke** — different mvids map to different lock ids
  across a 10k-sample window (no collisions expected; failing this would
  signal a derivation regression).
* **namespace separation** — the lock id MUST differ from
  ``import_chunking._derive_lock_id`` (different SHA-256 input domain). This
  guards Invariant: ``apply_forget_event`` advisory lock cannot collide with
  an in-progress ``import_apply`` advisory lock for the same numeric id.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


# ─── Test 1: deterministic across many runs ───────────────────────────────────


def test_p6_mvid_advisory_lock_id_is_deterministic() -> None:
    """Calling the helper twice (or 1000 times) on the same mvid yields the
    same lock id. PG advisory locks need this — both ``/approve`` and
    ``apply_forget_event`` must hash to the same key for the serialization
    contract to hold.
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    mvid = 4242
    expected = _p6_mvid_advisory_lock_id(mvid)
    for _ in range(1000):
        assert _p6_mvid_advisory_lock_id(mvid) == expected


# ─── Test 2: signed int64 range ──────────────────────────────────────────────


def test_p6_mvid_advisory_lock_id_in_signed_int64_range() -> None:
    """``pg_advisory_xact_lock`` takes a bigint (signed int64). The helper
    must always return a value in that range, including for mvid=0 and large
    mvids.

    Sampling covers the boundary values and a swath of typical mvids.
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    # Span: zero, one, small positives, mid-range, and large values close to
    # int32/int64 limits. PostgreSQL ints are 4-byte; mvids in this codebase
    # are stored as Integer (32-bit), so 2**31-1 is the realistic upper bound,
    # but the helper must handle larger inputs safely too.
    samples = [
        0,
        1,
        42,
        9_999,
        100_000,
        2**31 - 1,  # postgres int max
        2**32,  # exceeds postgres int4 — helper must still produce signed int64
        2**62,  # near upper int64 boundary
    ]
    for mvid in samples:
        lock_id = _p6_mvid_advisory_lock_id(mvid)
        assert isinstance(lock_id, int), f"mvid={mvid}: not int, got {type(lock_id)}"
        assert (
            INT64_MIN <= lock_id <= INT64_MAX
        ), f"mvid={mvid}: lock_id={lock_id} out of signed-int64 range"


# ─── Test 3: collision-resistance smoke (different mvids → different ids) ────


def test_p6_mvid_advisory_lock_id_no_collisions_in_10k_window() -> None:
    """SHA-256 is collision-resistant. A 10k-sample window of consecutive
    mvids should produce 10k distinct lock ids. A collision here means the
    derivation has regressed (e.g. accidentally truncating to int32).
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    ids = {_p6_mvid_advisory_lock_id(mvid) for mvid in range(1, 10_001)}
    assert len(ids) == 10_000, (
        f"collision detected in 10k-sample window: got {len(ids)} unique ids "
        f"out of 10_000 mvids"
    )


# ─── Test 4: namespace separation from import_chunking ───────────────────────


def test_p6_mvid_advisory_lock_id_differs_from_import_chunking_derivation() -> None:
    """``import_chunking._derive_lock_id(n)`` hashes raw 8 bytes of n.
    ``_p6_mvid_advisory_lock_id(n)`` hashes the namespaced string
    ``f'p6:mvid:{n}'``. The two derivations MUST differ for every overlapping
    input so the lock namespaces stay disjoint:

    * An in-progress ``import_apply`` (advisory-locked on
      ``ingestion_run_id=42``) must not block an ``/approve`` targeting
      ``message_version_id=42``, and vice versa.
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id
    from bot.services.import_chunking import _derive_lock_id

    # A collision is theoretically possible for one specific pair across 2**63
    # values but vanishingly unlikely; if this test ever fails for one input,
    # SHA-256 has been broken or the derivation regressed.
    for n in [0, 1, 42, 1_000, 2**31 - 1, 2**32]:
        assert _p6_mvid_advisory_lock_id(n) != _derive_lock_id(n), (
            f"namespace collision at n={n}: p6 and import_chunking derivations "
            f"produced the same lock_id; this breaks the P6/import disjointness "
            f"invariant"
        )


# ─── Test 5: derivation matches the spec verbatim ───────────────────────────


def test_p6_mvid_advisory_lock_id_matches_spec_derivation() -> None:
    """Pin the derivation exactly to the spec:
    ``signed_int64(first 8 bytes (big-endian) of sha256(b"p6:mvid:" + ascii(mvid)))``.

    Any change to the input string, hash algorithm, or byte interpretation
    would break the §5.C/§5.A.5 contract — ``/approve`` and
    ``apply_forget_event`` could end up hashing to different keys and the
    serialization point would silently fall apart.
    """
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    for mvid in [0, 1, 42, 9_999_999]:
        expected_payload = f"p6:mvid:{mvid}".encode("ascii")
        digest = hashlib.sha256(expected_payload).digest()
        (expected_lock_id,) = struct.unpack(">q", digest[:8])
        assert _p6_mvid_advisory_lock_id(mvid) == expected_lock_id, (
            f"derivation drift at mvid={mvid}: expected {expected_lock_id} "
            f"but got {_p6_mvid_advisory_lock_id(mvid)}"
        )
