"""Shared types and constants for Phase 10 graph projection.

Ownership model:
  - graph_common (this file): shared constants, dataclasses, error types, Protocol stubs
  - llm_gateway.py (W1-B): extract_graph_triples implementation
  - graph_projector.py (W2-B): projection modes (dry_run, incremental, full_rebuild, repair)
  - cascade_worker.py (W2-C): forget cascade + Neo4j purge worker
  - graph_query.py (W2.5-D): read-side traversal with read-block

References: PHASE10_PLAN.md §5.A (schema) and §5.B (extract_graph_triples contract).

RESERVED_LEDGER_CALL_TYPES contract:
  Reserved values for llm_usage_ledger.call_type: 'graph_projection' and
  'extract_candidates'. Migration 064 (owned by W1-C) reserves 'graph_projection'.
  'extract_candidates' was added in Task 10.5-6 (renamed from legacy 'unknown').
  Discrimination between dry_run / incremental / full_rebuild / repair modes is
  done via the graph_projection_runs.mode column (CHECK-constrained), NOT via
  call_type subdivision.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


# ─── Hash helpers ────────────────────────────────────────────────────────────


def compute_edge_key_hash(edge_key: str) -> int:
    """Deterministic signed int64 hash for edge_key.

    Used by drift detection sum() in graph_query._compute_neo4j_edge_hash.
    Stored as Neo4j native int (not hex string) so Cypher sum(r.edge_key_hash)
    works without type errors.

    Formula: first 8 bytes of sha256(edge_key), interpreted as big-endian
    signed int64. Frozen — changing this formula will break stored hashes.
    """
    digest = hashlib.sha256(edge_key.encode()).digest()[:8]
    return struct.unpack(">q", digest)[0]  # signed int64, big-endian


# ─── Allowed ontology values ─────────────────────────────────────────────────

# Verbatim from PHASE10_PLAN.md §5.B prompt template.
# These are the only node types the LLM extractor may emit; any other value
# is invalid and causes the triple to be dropped (refuse-on-unknown rule).
ALLOWED_NODE_TYPES: tuple[str, ...] = (
    "Person",
    "Topic",
    "Project",
    "Decision",
    "Question",
    "Answer",
    "Event",
    "KnowledgeCard",
    "Source",
)

# Verbatim from PHASE10_PLAN.md §5.B prompt template.
# Predicate vocabulary; enforced by GraphEdgeRef.__post_init__ and by
# graph_edges.ck_graph_edges_predicate DB CHECK (migration 062).
ALLOWED_PREDICATES: tuple[str, ...] = (
    "MENTIONS",
    "AUTHORED",
    "KNOWS_ABOUT",
    "ASKED",
    "ANSWERED",
    "DECIDED",
    "RELATED_TO",
    "SUPPORTS",
    "DERIVED_FROM",
    "PART_OF",
    "CONTRADICTS",
    "SUPERSEDES",
)

# Reserved llm_usage_ledger.call_type values for Phase 10.
# Migration 064 (owned by W1-C) added the call_type column and reserved
# 'graph_projection' per PHASE10_PLAN.md §5.B step 4. 'extract_candidates'
# was added in Task 10.5-6 (renamed from legacy 'unknown' default).
# Mode discrimination (dry_run vs incremental vs full_rebuild vs repair) is
# handled via graph_projection_runs.mode column (CHECK-constrained), NOT via
# call_type subdivision.
RESERVED_LEDGER_CALL_TYPES: tuple[str, ...] = (
    "graph_projection",    # per PHASE10_PLAN §5.B step 4
                           # discrimination of dry/incremental/full/repair modes
                           # is via graph_projection_runs.mode column (CHECK-constrained),
                           # NOT via ledger.call_type subdivision. This is the reserved
                           # value contract for migration 064 in W1-C.
    "extract_candidates",  # Phase 6 T6-03 extraction gateway (Task 10.5-6).
                           # Renamed from legacy 'unknown' default to enable
                           # per-feature cost reporting in llm_usage_ledger.
    "butler_decision",     # Phase 12 T12-01 — LLM call for Butler plan generation.
                           # Added to allow-list in migration 071 + DB CHECK.
    "butler_summary",      # Phase 12 T12-01 — LLM call for Butler result summarization.
                           # Added to allow-list in migration 071 + DB CHECK.
)


# ─── Literal type aliases ────────────────────────────────────────────────────

# Exact CHECK constraint values from migration 060 (graph_projection_runs).
GraphProjectionMode = Literal["dry_run", "incremental", "full_rebuild", "repair"]
GraphProjectionRunStatus = Literal[
    "running", "completed", "failed", "cancelled", "cost_exceeded", "dry_run_complete"
]


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GraphNodeRef:
    """Typed reference to a Neo4j node identified by node_key.

    node_key: stable Postgres-derived identifier (e.g. "user:12345", "card:uuid")
    node_type: must be one of ALLOWED_NODE_TYPES
    """

    node_key: str
    node_type: str

    def __post_init__(self) -> None:
        if self.node_type not in ALLOWED_NODE_TYPES:
            raise ValueError(
                f"node_type {self.node_type!r} is not in ALLOWED_NODE_TYPES: {ALLOWED_NODE_TYPES}"
            )


@dataclass(frozen=True)
class GraphEdgeRef:
    """Typed reference to a Neo4j relationship (edge) identified by edge_key.

    edge_key: stable SHA-256 key (subject+predicate+object)
    source_key: node_key of the subject node
    target_key: node_key of the object node
    predicate: must be one of ALLOWED_PREDICATES
    """

    edge_key: str
    source_key: str
    target_key: str
    predicate: str

    def __post_init__(self) -> None:
        if self.predicate not in ALLOWED_PREDICATES:
            raise ValueError(
                f"predicate {self.predicate!r} is not in ALLOWED_PREDICATES: {ALLOWED_PREDICATES}"
            )


# ─── Error hierarchy ─────────────────────────────────────────────────────────


class RefusalError(Exception):
    """Base error for graph query and projection refusals.

    Raised when Phase 10 components must abort an operation due to a
    governance or budget constraint. Subclasses carry specific semantics
    used by the projector, cascade worker, and read-block.

    Used by future read-block in W2-C (assert_no_pending_purge) and by
    extract_graph_triples in W1-B (governance pre-call assertion).
    """


class GraphProjectionPolicyError(RefusalError):
    """Raised by extract_graph_triples when governance_policy != 'normal'.

    Per PHASE10_PLAN.md §5.B: the pre-call assertion is fail-closed —
    never extract over non-normal content. Caller must catch this, mark
    the source as skipped_policy_count, and proceed to the next source.
    """


class GraphProjectionBudgetError(RefusalError):
    """Raised by the graph projector when a cost ceiling is exceeded.

    Two ceilings apply:
    - GRAPH_PROJECTION_RUN_USD_CEILING ($0.50/run)
    - GRAPH_PROJECTION_DAILY_USD_CEILING ($2/day)

    When either ceiling is hit, the projector stops processing further
    sources and raises this error; the run is marked 'cost_exceeded'.
    """


class ExtractGraphTriplesError(RefusalError):
    """Raised when extract_graph_triples cannot parse provider response.

    Per FIX-HIGH-3 (Codex review): malformed JSON or non-list JSON root
    from the provider must raise this error so callers can mark the source
    as skipped rather than silently treating it as empty extraction.
    """


# ─── Protocols ───────────────────────────────────────────────────────────────


@runtime_checkable
class RepairModeContract(Protocol):
    """API surface for repair_mode invocation.

    Declares the interface that the W2-B graph_projector will provide to
    the W2-C cascade worker. The cascade worker calls request_repair when
    a Postgres forget event requires a targeted Neo4j node re-projection
    rather than a full purge (e.g. shared-node provenance cleanup).

    Implementations are owned by W2-B (graph_projector.py). This Protocol
    is defined here so W2-C (cascade_worker.py) can type-annotate its
    injected projector reference without importing from graph_projector
    and creating a circular dependency.
    """

    async def request_repair(
        self,
        *,
        source_table: str,
        source_pk: str,
        reason: str,
    ) -> None:
        """Request a targeted repair projection for a specific source row.

        source_table: 'message_versions' or 'knowledge_cards'
        source_pk: PK value of the row requiring repair (str form)
        reason: human-readable reason for the repair (e.g. 'forget_cascade')
        """
        ...
