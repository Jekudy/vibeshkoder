"""Graph Projection Service (Phase 10 / T10-04).

Implements four projection modes per PHASE10_PLAN.md §5.C:

- dry_run: estimates cost, scans governance-eligible sources, no writes
- project_incremental: extracts new triples from sources changed since last run
- project_full_rebuild: REPLAY-ONLY from stored graph_provenance/graph_edges (no LLM)
- project_repair_source: re-extracts triples for a specific (source_table, source_pk)

Ontology split (HIGH E):
- knowledge_cards → LLM triple extraction (semantic CONCEPT nodes + edges)
- message_versions → provenance/event nodes ONLY (no LLM extraction)

Feature flag: memory.graph.projection.enabled (default OFF).
Cost ceilings: GRAPH_PROJECTION_DAILY_USD_CEILING / GRAPH_PROJECTION_RUN_USD_CEILING.
Advisory lock: GRAPH_REBUILD_LOCK_ID (pg_advisory_lock for full_rebuild mode).

References: PHASE10_PLAN.md §5.C, §5.D, §5.I
"""

from __future__ import annotations

import hashlib
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.graph_common import (
    ExtractGraphTriplesError,
    GraphProjectionBudgetError,
    GraphProjectionPolicyError,
    GraphProjectionMode,
    GraphProjectionRunStatus,
    RefusalError,
)
from bot.services.llm_gateway import extract_graph_triples  # noqa: E402 — module-level for patchability

_log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Default max sources per projection run. Configurable via GraphProjectorConfig.
GRAPH_PROJECTION_MAX_SOURCES_DEFAULT: int = 200

# Feature flag key controlling whether any projection mode is enabled.
GRAPH_PROJECTION_FEATURE_FLAG: str = "memory.graph.projection.enabled"

# PostgreSQL advisory lock ID for full_rebuild — prevents race with cascade purge worker.
# Derived from namespace prefix "p10:graph_rebuild" via struct-pack hash.
# Must fit in signed int64.
_LOCK_NAMESPACE = b"p10:graph_rebuild"
_lock_hash = struct.unpack(">q", hashlib.sha256(_LOCK_NAMESPACE).digest()[:8])[0]
GRAPH_REBUILD_LOCK_ID: int = _lock_hash

# Prompt version used when calling extract_graph_triples.
# Must match the template file in bot/services/llm_prompts/graph_triples_v0_1_0.py.
_GRAPH_TRIPLES_PROMPT_VERSION: str = "v0.1.0"


# ─── Errors ───────────────────────────────────────────────────────────────────


class ServiceDisabledError(Exception):
    """Raised when the projection feature flag is off.

    Callers (scheduler, admin handlers) should catch this and log / skip silently.
    """


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GraphProjectionRunResult:
    """Result of a graph projection run.

    All counts are non-negative integers. errors_list carries per-source error
    strings for failed sources that did not abort the run.
    """

    run_id: int
    status: GraphProjectionRunStatus
    sources_total: int
    sources_processed: int
    sources_skipped_governance: int
    sources_skipped_budget: int
    sources_skipped_unknown: int
    triples_created: int
    nodes_merged: int
    edges_merged: int
    cost_usd: Decimal
    errors_list: list[str]


# ─── Protocols for injectable dependencies ────────────────────────────────────


class _RunRepoProtocol(Protocol):
    async def create_run(
        self, session: AsyncSession, *, mode: GraphProjectionMode, started_by: str | None
    ) -> Any: ...
    async def update_run_stats(
        self, session: AsyncSession, run_id: int, *, stats_patch: dict
    ) -> None: ...
    async def finalize_run(
        self,
        session: AsyncSession,
        run_id: int,
        *,
        status: GraphProjectionRunStatus,
        cost_usd: Decimal | None = None,
    ) -> None: ...
    async def get_active_run(self, session: AsyncSession) -> Any: ...


class _ProvenanceRepoProtocol(Protocol):
    async def create_provenance(self, session: AsyncSession, **kwargs: Any) -> Any: ...
    async def find_active(
        self, session: AsyncSession, *, projection_run_id: int | None = None
    ) -> list: ...
    async def find_by_source(
        self, session: AsyncSession, *, source_table: str, source_pk: str
    ) -> list: ...


class _EdgeRepoProtocol(Protocol):
    async def create_edge(self, session: AsyncSession, **kwargs: Any) -> Any: ...
    async def find_by_provenance(self, session: AsyncSession, provenance_id: int) -> list: ...


class _LedgerRepoProtocol(Protocol):
    async def daily_cost_usd(self, session: AsyncSession, *, day: Any) -> Decimal: ...


@dataclass(frozen=True)
class GraphProjectorConfig:
    """Configuration for the graph projection service.

    All fields are required; no defaults except cost ceilings which match spec values.
    adapter: GraphAdapter — production Neo4jAdapter or NetworkXAdapter for tests.
    """

    adapter: Any  # GraphAdapter Protocol
    run_repo: Any  # _RunRepoProtocol
    provenance_repo: Any  # _ProvenanceRepoProtocol
    edge_repo: Any  # _EdgeRepoProtocol
    ledger_repo: Any  # _LedgerRepoProtocol
    daily_ceiling_usd: Decimal = field(default_factory=lambda: Decimal("2.00"))
    run_ceiling_usd: Decimal = field(default_factory=lambda: Decimal("0.50"))
    max_sources_per_run: int = GRAPH_PROJECTION_MAX_SOURCES_DEFAULT
    # Optional: LLMProvider for incremental/repair modes. None = dry_run and full_rebuild only.
    llm_provider: Any | None = None


# ─── Internal helpers ─────────────────────────────────────────────────────────


async def _is_projection_enabled(session: AsyncSession) -> bool:
    """Check the feature flag memory.graph.projection.enabled (default False)."""
    from bot.db.repos.feature_flag import FeatureFlagRepo

    return await FeatureFlagRepo.get(session, GRAPH_PROJECTION_FEATURE_FLAG)


async def _check_graph_budget(
    session: AsyncSession,
    *,
    ledger_repo: _LedgerRepoProtocol,
    run_cost_usd: Decimal,
    daily_ceiling_usd: Decimal,
    run_ceiling_usd: Decimal,
) -> None:
    """Raise GraphProjectionBudgetError if any cost ceiling is exceeded.

    Checks:
    1. Daily LLM cost (graph_projection call_type only) vs daily_ceiling_usd.
    2. Accumulated run cost vs run_ceiling_usd.

    Uses strict > comparison — the exact ceiling value is allowed (soft cap).
    Raises GraphProjectionBudgetError with 'daily' or 'run' in the message.

    HIGH-3 fix: passes call_type='graph_projection' to daily_cost_usd so the
    budget check is scoped to graph projection costs only, not all LLM costs.
    SUGGESTION fix: changed >= to > (soft cap — exact ceiling is allowed).
    """
    today = datetime.now(tz=timezone.utc).date()
    # Pass call_type to isolate graph_projection costs from QA/digest costs (HIGH-3).
    # Falls back gracefully if the repo doesn't support call_type kwarg.
    import inspect
    sig = inspect.signature(ledger_repo.daily_cost_usd)
    if "call_type" in sig.parameters:
        daily_cost = await ledger_repo.daily_cost_usd(
            session, day=today, call_type="graph_projection"
        )
    else:
        daily_cost = await ledger_repo.daily_cost_usd(session, day=today)

    if daily_cost > daily_ceiling_usd:
        raise GraphProjectionBudgetError(
            f"daily LLM cost ceiling exceeded: {daily_cost} > {daily_ceiling_usd}"
        )
    if run_cost_usd > run_ceiling_usd:
        raise GraphProjectionBudgetError(
            f"run cost ceiling exceeded: {run_cost_usd} > {run_ceiling_usd}"
        )


async def _fetch_eligible_cards(
    session: AsyncSession,
    *,
    limit: int,
    since_id: int | None = None,
    since_timestamp: datetime | None = None,
) -> list[dict]:
    """Fetch knowledge_cards eligible for projection (governance-filtered).

    Applies the governance pre-filter from §5.C:
    - card_status = 'approved'
    - No forget_events targeting any source message_version of the card

    Returns list of dicts with id, title, body_markdown, created_at.
    Only returns cards newer than since_id when provided (for incremental mode).

    HIGH-1 fix: when since_timestamp provided, adds WHERE kc.updated_at > :since_timestamp
    to filter out already-projected sources (avoids wasted LLM cost).
    """
    where_clauses = [
        "kc.card_status = 'approved'",
        # Exclude cards whose source message_versions are covered by a forget_event.
        # The forget_event target_id is the chat_messages.id (as text).
        # card_sources → message_versions → chat_messages is the lookup path.
        "NOT EXISTS (\n"
        "    SELECT 1 FROM forget_events fe\n"
        "    WHERE fe.target_type = 'message'\n"
        "    AND fe.target_id IN (\n"
        "        SELECT mv.chat_message_id::text\n"
        "        FROM card_sources cs\n"
        "        JOIN message_versions mv ON mv.id = cs.message_version_id\n"
        "        WHERE cs.card_id = kc.id\n"
        "    )\n"
        ")",
    ]
    params: dict = {"limit": limit}

    if since_id is not None:
        where_clauses.append("kc.id > :since_card_id")
        params["since_card_id"] = since_id

    if since_timestamp is not None:
        where_clauses.append("kc.updated_at > :since_timestamp")
        params["since_timestamp"] = since_timestamp

    where_sql = " AND ".join(where_clauses)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- where_sql is a static-clause string built from in-code conditions; no user input flows in.
    stmt = text(
        f"SELECT kc.id, kc.title, kc.body_markdown, kc.created_at\n"
        f"FROM knowledge_cards kc\n"
        f"WHERE {where_sql}\n"
        f"ORDER BY kc.created_at ASC, kc.id ASC\n"
        f"LIMIT :limit"
    )
    result = await session.execute(stmt, params)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


async def _mark_provenance_inactive(session: AsyncSession, provenance_id: int) -> None:
    """Mark a graph_provenance row inactive (compensating action on Neo4j failure).

    Called when Neo4j raises mid-loop to ensure Postgres provenance is not
    left in an active state without a matching Neo4j node/edge.

    CRITICAL fix: part of per-source SAVEPOINT atomicity pattern.
    """
    await session.execute(
        text("UPDATE graph_provenance SET is_active = FALSE WHERE id = :pid"),
        {"pid": provenance_id},
    )


async def _get_last_successful_run_timestamp(session: AsyncSession) -> datetime | None:
    """Return started_at of the most recent completed incremental run, or None.

    HIGH-1 fix: used by project_incremental to auto-resume from the last
    successful run without requiring caller to pass since_timestamp explicitly.
    Returns None if no prior successful incremental run exists.
    """
    result = await session.execute(
        text(
            "SELECT started_at FROM graph_projection_runs "
            "WHERE mode = 'incremental' AND status = 'completed' "
            "ORDER BY started_at DESC LIMIT 1"
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    return row[0]


async def _fetch_eligible_message_versions(
    session: AsyncSession,
    *,
    limit: int,
    since_mvid: int | None = None,
    since_timestamp: datetime | None = None,
) -> list[dict]:
    """Fetch message_versions eligible for event-node projection (governance-filtered).

    Per ontology split: no LLM extraction — provenance/event nodes only.
    Applies governance pre-filter from §5.C:
    - memory_policy = 'normal'
    - is_redacted = FALSE
    - No active forget_events targeting the message

    Only returns versions newer than since_mvid when provided (incremental).
    """
    where_clauses = [
        "cm.memory_policy = 'normal'",
        "mv.is_redacted = FALSE",
        "fe.id IS NULL",
    ]
    params: dict = {"limit": limit}

    if since_mvid is not None:
        where_clauses.append("mv.id > :since_mvid")
        params["since_mvid"] = since_mvid

    if since_timestamp is not None:
        where_clauses.append("mv.captured_at > :since_timestamp")
        params["since_timestamp"] = since_timestamp

    where_sql = " AND ".join(where_clauses)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- where_sql is a static-clause string built from in-code conditions; no user input flows in.
    stmt = text(
        f"SELECT mv.id, mv.chat_message_id, mv.version_seq, mv.captured_at AS created_at,\n"
        f"       cm.chat_id\n"
        f"FROM message_versions mv\n"
        f"JOIN chat_messages cm ON cm.id = mv.chat_message_id\n"
        f"LEFT JOIN forget_events fe ON (\n"
        f"    fe.target_type = 'message' AND fe.target_id = cm.id::TEXT\n"
        f")\n"
        f"WHERE {where_sql}\n"
        f"ORDER BY mv.id ASC\n"
        f"LIMIT :limit"
    )
    result = await session.execute(stmt, params)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


def _compute_content_hash(text_content: str) -> str:
    """Compute SHA-256 hex digest of source content for drift/idempotency."""
    return hashlib.sha256(text_content.encode()).hexdigest()


def _compute_edge_key(subject_key: str, predicate: str, object_key: str) -> str:
    """Compute stable edge_key from subject+predicate+object triple."""
    canonical = f"{subject_key}|{predicate}|{object_key}"
    return hashlib.sha256(canonical.encode()).hexdigest()


# ─── Public API ───────────────────────────────────────────────────────────────


async def dry_run(
    session: AsyncSession,
    *,
    limit: int = 20,
    source_types: list[str] | None = None,
    config: GraphProjectorConfig,
) -> GraphProjectionRunResult:
    """Estimate cost and scan governance-eligible sources without any writes.

    Writes a graph_projection_runs row with mode='dry_run', status='dry_run_complete'.
    Does NOT write graph_provenance, graph_edges, or Neo4j nodes/edges.

    Does NOT check the feature flag — dry_run is always allowed for diagnostics.
    """

    run = await config.run_repo.create_run(
        session, mode="dry_run", started_by="dry_run"
    )
    run_id = run.id

    try:
        # Fetch eligible cards (primary semantic source)
        cards = await _fetch_eligible_cards(session, limit=limit)
        sources_total = len(cards)
        sources_processed = sources_total

        await config.run_repo.update_run_stats(
            session,
            run_id,
            stats_patch={"source_card_count": sources_total},
        )
        await config.run_repo.finalize_run(
            session, run_id, status="dry_run_complete", cost_usd=Decimal("0.00")
        )

        return GraphProjectionRunResult(
            run_id=run_id,
            status="dry_run_complete",
            sources_total=sources_total,
            sources_processed=sources_processed,
            sources_skipped_governance=0,
            sources_skipped_budget=0,
            sources_skipped_unknown=0,
            triples_created=0,
            nodes_merged=0,
            edges_merged=0,
            cost_usd=Decimal("0.00"),
            errors_list=[],
        )
    except Exception:
        await config.run_repo.finalize_run(session, run_id, status="failed")
        raise


async def project_incremental(
    session: AsyncSession,
    *,
    since_run_id: int | None = None,
    since_timestamp: datetime | None = None,
    config: GraphProjectorConfig,
    started_by: str | None = "scheduler",
) -> GraphProjectionRunResult:
    """Project new triples from sources changed since last run.

    Governance pre-filter applied before LLM dispatch. Writes:
    - graph_projection_runs row
    - graph_provenance rows (per source triple)
    - graph_edges rows
    - Neo4j MERGE for nodes + edges (via config.adapter)

    HIGH-2 fix: acquires pg_try_advisory_xact_lock before proceeding.
    If the lock is held by a full_rebuild, raises RefusalError immediately.

    HIGH-1 fix: auto-resumes from last completed run when no since_* given.
    Uses since_timestamp to filter _fetch_eligible_cards.

    CRITICAL fix: per-source SAVEPOINT atomicity. Neo4j failure marks provenance
    inactive and records error; run finalizes as 'failed' if any errors occurred.

    Product #12 fix: also projects message_version event nodes (no LLM).

    Raises ServiceDisabledError if feature flag is off.
    Raises GraphProjectionBudgetError if cost ceilings exceeded.
    Raises RefusalError if full_rebuild lock is held.
    """
    if not await _is_projection_enabled(session):
        raise ServiceDisabledError(
            f"{GRAPH_PROJECTION_FEATURE_FLAG} is disabled; incremental projection skipped"
        )

    # HIGH-2: acquire advisory try-lock — refuse if full_rebuild holds it
    lock_result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": GRAPH_REBUILD_LOCK_ID},
    )
    lock_acquired = lock_result.scalar()
    if not lock_acquired:
        raise RefusalError(
            "graph rebuild in progress, retry later — "
            "pg_try_advisory_xact_lock refused (full_rebuild holds the lock)"
        )

    run = await config.run_repo.create_run(
        session, mode="incremental", started_by=started_by
    )
    run_id = run.id
    run_cost = Decimal("0.00")
    errors: list[str] = []
    sources_total = 0
    sources_processed = 0
    sources_skipped_governance = 0
    sources_skipped_budget = 0
    sources_skipped_unknown = 0
    triples_created = 0
    nodes_merged = 0
    edges_merged = 0

    try:
        # Check daily budget before starting
        await _check_graph_budget(
            session,
            ledger_repo=config.ledger_repo,
            run_cost_usd=run_cost,
            daily_ceiling_usd=config.daily_ceiling_usd,
            run_ceiling_usd=config.run_ceiling_usd,
        )

        # HIGH-1: resolve since_timestamp — auto-resume from last successful run
        effective_since_ts = since_timestamp
        if effective_since_ts is None and since_run_id is None:
            effective_since_ts = await _get_last_successful_run_timestamp(session)
            if effective_since_ts is None:
                _log.info(
                    "graph_projector incremental: no prior completed run found; "
                    "scanning ALL eligible sources (first run)"
                )

        # Fetch governance-eligible cards (filtered by since_timestamp when available)
        cards = await _fetch_eligible_cards(
            session,
            limit=config.max_sources_per_run,
            since_timestamp=effective_since_ts,
        )
        sources_total = len(cards)

        # Product #12: fetch eligible message_versions for event-node projection (no LLM)
        message_versions = await _fetch_eligible_message_versions(
            session,
            limit=config.max_sources_per_run,
            since_timestamp=effective_since_ts,
        )

        # Project message_version event nodes (no LLM — ontology split §5.C HIGH E)
        for mv in message_versions:
            mv_id = mv["id"]
            node_key = f"msg:{mv_id}"
            try:
                await config.adapter.merge_node(
                    node_key=node_key,
                    labels=["MessageEvent", "MemoryNode"],
                    properties={
                        "node_type": "MessageEvent",
                        "label": node_key,
                        "chat_id": str(mv.get("chat_id", "")),
                        "version_seq": mv.get("version_seq"),
                        "chat_message_id": str(mv.get("chat_message_id", "")),
                    },
                )
                nodes_merged += 1
            except Exception as exc:
                errors.append(f"msg:{mv_id}: {exc!r}")
                _log.warning("graph_projector incremental: event-node error mv=%s: %s", mv_id, exc)

        # Project each card via LLM triple extraction
        for card in cards:
            if sources_processed + sources_skipped_governance + sources_skipped_budget >= config.max_sources_per_run:
                break

            # Check per-source budget ceiling
            try:
                await _check_graph_budget(
                    session,
                    ledger_repo=config.ledger_repo,
                    run_cost_usd=run_cost,
                    daily_ceiling_usd=config.daily_ceiling_usd,
                    run_ceiling_usd=config.run_ceiling_usd,
                )
            except GraphProjectionBudgetError:
                sources_skipped_budget += (sources_total - sources_processed - sources_skipped_governance - sources_skipped_budget)
                break

            if config.llm_provider is None:
                # No provider configured — skip LLM extraction, write event provenance only
                sources_processed += 1
                continue

            card_id = str(card["id"])
            source_text = f"{card['title']}\n\n{card['body_markdown']}"
            content_hash = _compute_content_hash(source_text)

            try:
                from bot.services.llm_gateway import LLMGatewayConfig

                llm_config = LLMGatewayConfig(
                    provider=config.llm_provider.provider if hasattr(config.llm_provider, "provider") else "anthropic",
                    model=config.llm_provider.model if hasattr(config.llm_provider, "model") else "claude-3-haiku-20240307",
                    daily_ceiling_usd=config.daily_ceiling_usd,
                    monthly_ceiling_usd=config.daily_ceiling_usd * 30,
                    prompt_template_version=_GRAPH_TRIPLES_PROMPT_VERSION,
                )

                extract_result = await extract_graph_triples(
                    session,
                    source_table="knowledge_cards",
                    source_pk=card_id,
                    source_text=source_text,
                    source_id=card_id,
                    source_mv_id=None,
                    prompt_version=_GRAPH_TRIPLES_PROMPT_VERSION,
                    run_id=run_id,
                    governance_policy="normal",
                    config=llm_config,
                    ledger_repo=config.ledger_repo,
                    provider=config.llm_provider,
                )

                run_cost += extract_result.cost_usd
                sources_skipped_unknown += extract_result.skipped_total

                # CRITICAL: per-source SAVEPOINT atomicity.
                # Write provenance + edges + Neo4j for each valid triple.
                # Neo4j failure marks provenance inactive (compensating action).
                for triple in extract_result.triples:
                    edge_key = _compute_edge_key(
                        triple.subject_label, triple.predicate, triple.object_label
                    )
                    triple_hash = hashlib.sha256(edge_key.encode()).hexdigest()[:16]

                    # SAVEPOINT: Postgres writes (provenance + edge) are isolated
                    async with session.begin_nested():
                        prov = await config.provenance_repo.create_provenance(
                            session,
                            projection_run_id=run_id,
                            source_table="knowledge_cards",
                            source_pk=card_id,
                            source_card_id=card["id"],
                            triple_hash=triple_hash,
                            graph_node_key=f"card:{card_id}",
                            source_content_hash=content_hash,
                            governance_policy="normal",
                        )

                        await config.edge_repo.create_edge(
                            session,
                            graph_provenance_id=prov.id,
                            subject_node_key=triple.subject_label,
                            predicate=triple.predicate,
                            object_node_key=triple.object_label,
                            edge_key=edge_key,
                            confidence_score=Decimal(str(triple.confidence)),
                        )

                    # Flush savepoint state before Neo4j writes
                    await session.flush()

                    # Neo4j writes — if they fail, compensate by marking provenance inactive
                    try:
                        # Neo4j MERGE — subject node
                        await config.adapter.merge_node(
                            node_key=triple.subject_label,
                            labels=[triple.subject_type, "MemoryNode"],
                            properties={
                                "node_type": triple.subject_type,
                                "label": triple.subject_label,
                                "provenance_id": str(prov.id),
                            },
                        )
                        nodes_merged += 1

                        # Neo4j MERGE — object node
                        await config.adapter.merge_node(
                            node_key=triple.object_label,
                            labels=[triple.object_type, "MemoryNode"],
                            properties={
                                "node_type": triple.object_type,
                                "label": triple.object_label,
                                "provenance_id": str(prov.id),
                            },
                        )
                        nodes_merged += 1

                        # Neo4j MERGE — edge (include edge_key_hash for drift detection)
                        await config.adapter.merge_edge(
                            edge_key=edge_key,
                            source_key=triple.subject_label,
                            target_key=triple.object_label,
                            relationship_type=triple.predicate,
                            properties={
                                "predicate": triple.predicate,
                                "confidence": triple.confidence,
                                "provenance_id": str(prov.id),
                                "edge_key_hash": triple_hash,
                            },
                        )
                        edges_merged += 1
                        triples_created += 1

                    except Exception as neo4j_exc:
                        # Compensating action: mark provenance inactive so Postgres
                        # doesn't have orphaned active provenance without Neo4j data.
                        await _mark_provenance_inactive(session, prov.id)
                        error_msg = f"card:{card_id} triple neo4j: {neo4j_exc!r}"
                        errors.append(error_msg)
                        _log.warning(
                            "graph_projector incremental: Neo4j failure card=%s prov=%s: %s",
                            card_id, prov.id, neo4j_exc,
                        )
                        continue

                sources_processed += 1

            except GraphProjectionPolicyError as exc:
                sources_skipped_governance += 1
                _log.warning("graph_projector incremental: governance skip card=%s: %s", card_id, exc)

            except ExtractGraphTriplesError as exc:
                errors.append(f"card:{card_id}: {exc}")
                _log.warning("graph_projector incremental: extract error card=%s: %s", card_id, exc)
                sources_processed += 1  # count as processed (partial)

            except Exception as exc:
                errors.append(f"card:{card_id}: {exc!r}")
                _log.exception("graph_projector incremental: unexpected error card=%s", card_id)
                sources_processed += 1

        # CRITICAL fix: finalize as 'failed' if any errors occurred, not 'completed'
        final_status: GraphProjectionRunStatus = "failed" if errors else "completed"
        await config.run_repo.update_run_stats(
            session,
            run_id,
            stats_patch={
                "source_card_count": sources_total,
                "projected_node_count": nodes_merged,
                "projected_edge_count": edges_merged,
                "skipped_policy_count": sources_skipped_governance,
                "skipped_budget_count": sources_skipped_budget,
                "actual_cost_usd": run_cost,
            },
        )
        await config.run_repo.finalize_run(session, run_id, status=final_status, cost_usd=run_cost)

        return GraphProjectionRunResult(
            run_id=run_id,
            status=final_status,
            sources_total=sources_total,
            sources_processed=sources_processed,
            sources_skipped_governance=sources_skipped_governance,
            sources_skipped_budget=sources_skipped_budget,
            sources_skipped_unknown=sources_skipped_unknown,
            triples_created=triples_created,
            nodes_merged=nodes_merged,
            edges_merged=edges_merged,
            cost_usd=run_cost,
            errors_list=errors,
        )

    except GraphProjectionBudgetError:
        await config.run_repo.finalize_run(session, run_id, status="cost_exceeded", cost_usd=run_cost)
        raise
    except (ServiceDisabledError, RefusalError):
        raise
    except Exception:
        await config.run_repo.finalize_run(session, run_id, status="failed")
        raise


async def project_full_rebuild(
    session: AsyncSession,
    *,
    config: GraphProjectorConfig,
    started_by: str | None = "admin",
) -> GraphProjectionRunResult:
    """Rebuild the Neo4j graph by replaying stored graph_provenance/graph_edges.

    REPLAY-ONLY — no LLM calls. Postgres is the canonical store; Neo4j is the projection.

    Pre-conditions (fail-closed):
    - Feature flag must be enabled.
    - No active purge in graph_purge_pending with status='in_flight' (T10-06 carryover).
    - Acquires pg_advisory_lock(GRAPH_REBUILD_LOCK_ID) for the duration.

    Procedure:
    1. Acquire advisory lock.
    2. Fetch all active graph_provenance + graph_edges from Postgres.
    3. Issue Neo4j MERGE for each node/edge.
    4. Finalize run.
    """
    if not await _is_projection_enabled(session):
        raise ServiceDisabledError(
            f"{GRAPH_PROJECTION_FEATURE_FLAG} is disabled; full_rebuild skipped"
        )

    # CONCERN Product #6: pre-condition check on graph_purge_pending (T10-06 carryover).
    # Uses SAVEPOINT so that a ProgrammingError (table doesn't exist) doesn't poison the
    # outer transaction (pre-T10-06 state where the table hasn't been created yet).
    try:
        async with session.begin_nested():
            active_purges_result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM graph_purge_pending "
                    "WHERE status IN ('pending', 'in_flight')"
                )
            )
            active_purge_count = active_purges_result.scalar()
            if active_purge_count and active_purge_count > 0:
                raise GraphProjectionBudgetError(
                    f"Cannot full_rebuild: {active_purge_count} active purge_pending rows"
                )
    except GraphProjectionBudgetError:
        raise  # Re-raise budget errors (real pre-condition violation)
    except ProgrammingError:
        # graph_purge_pending table doesn't exist yet (pre-T10-06) — log warning + continue
        # The SAVEPOINT rollback above preserves the outer transaction.
        _log.warning(
            "graph_purge_pending table not found; T10-06 not yet merged. "
            "Skipping pre-condition check."
        )
    except Exception:
        # Other unexpected errors from the purge check — log and continue
        _log.warning(
            "graph_purge_pending pre-condition check failed unexpectedly; continuing.",
            exc_info=True,
        )

    # Acquire advisory lock to prevent race with cascade purge worker (T10-06)
    # Note: pg_advisory_lock blocks until acquired; use xact-level lock released at COMMIT.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": GRAPH_REBUILD_LOCK_ID},
    )

    run = await config.run_repo.create_run(
        session, mode="full_rebuild", started_by=started_by
    )
    run_id = run.id
    nodes_merged = 0
    edges_merged = 0

    try:
        # Fetch all active provenance rows
        active_provenance = await config.provenance_repo.find_active(session)

        for prov in active_provenance:
            # Find edges associated with this provenance
            edges = await config.edge_repo.find_by_provenance(session, prov.id)

            for edge in edges:
                # MERGE subject node
                await config.adapter.merge_node(
                    node_key=edge.subject_node_key,
                    labels=["MemoryNode"],
                    properties={
                        "label": edge.subject_node_key,
                        "provenance_id": str(prov.id),
                    },
                )
                nodes_merged += 1

                # MERGE object node
                await config.adapter.merge_node(
                    node_key=edge.object_node_key,
                    labels=["MemoryNode"],
                    properties={
                        "label": edge.object_node_key,
                        "provenance_id": str(prov.id),
                    },
                )
                nodes_merged += 1

                # MERGE edge (include edge_key_hash for drift detection)
                edge_key_hash = hashlib.sha256(edge.edge_key.encode()).hexdigest()[:16]
                await config.adapter.merge_edge(
                    edge_key=edge.edge_key,
                    source_key=edge.subject_node_key,
                    target_key=edge.object_node_key,
                    relationship_type=edge.predicate,
                    properties={
                        "predicate": edge.predicate,
                        "confidence": float(edge.confidence_score),
                        "provenance_id": str(prov.id),
                        "edge_key_hash": edge_key_hash,
                    },
                )
                edges_merged += 1

        sources_total = len(active_provenance)
        await config.run_repo.update_run_stats(
            session,
            run_id,
            stats_patch={
                "projected_node_count": nodes_merged,
                "projected_edge_count": edges_merged,
            },
        )
        await config.run_repo.finalize_run(
            session, run_id, status="completed", cost_usd=Decimal("0.00")
        )

        return GraphProjectionRunResult(
            run_id=run_id,
            status="completed",
            sources_total=sources_total,
            sources_processed=sources_total,
            sources_skipped_governance=0,
            sources_skipped_budget=0,
            sources_skipped_unknown=0,
            triples_created=edges_merged,
            nodes_merged=nodes_merged,
            edges_merged=edges_merged,
            cost_usd=Decimal("0.00"),
            errors_list=[],
        )

    except Exception:
        await config.run_repo.finalize_run(session, run_id, status="failed")
        raise


async def project_repair_source(
    session: AsyncSession,
    *,
    source_table: str,
    source_pk: str,
    config: GraphProjectorConfig,
    started_by: str | None = "repair",
) -> GraphProjectionRunResult:
    """Re-extract triples for a specific (source_table, source_pk) pair.

    Used by cascade / cleanup paths when a single source needs re-projection.
    Only knowledge_cards sources support LLM re-extraction; message_versions
    are event-node only (no LLM).

    Raises ServiceDisabledError if feature flag is off.
    """
    if source_table not in ("message_versions", "knowledge_cards"):
        raise ValueError(
            f"source_table {source_table!r} must be 'message_versions' or 'knowledge_cards'"
        )

    if not await _is_projection_enabled(session):
        raise ServiceDisabledError(
            f"{GRAPH_PROJECTION_FEATURE_FLAG} is disabled; repair skipped"
        )

    run = await config.run_repo.create_run(
        session, mode="repair", started_by=started_by
    )
    run_id = run.id
    triples_created = 0
    nodes_merged = 0
    edges_merged = 0
    run_cost = Decimal("0.00")

    try:
        if source_table == "knowledge_cards" and config.llm_provider is not None:
            # Fetch the specific card
            result = await session.execute(
                text(
                    "SELECT kc.id, kc.title, kc.body_markdown "
                    "FROM knowledge_cards kc "
                    "WHERE kc.id = :card_id AND kc.card_status = 'approved'"
                ),
                {"card_id": source_pk},
            )
            row = result.mappings().one_or_none()
            if row is None:
                _log.warning("project_repair_source: card %s not found or not approved", source_pk)
                await config.run_repo.finalize_run(session, run_id, status="completed")
                return GraphProjectionRunResult(
                    run_id=run_id,
                    status="completed",
                    sources_total=0,
                    sources_processed=0,
                    sources_skipped_governance=1,
                    sources_skipped_budget=0,
                    sources_skipped_unknown=0,
                    triples_created=0,
                    nodes_merged=0,
                    edges_merged=0,
                    cost_usd=Decimal("0.00"),
                    errors_list=[f"card:{source_pk} not found or not approved"],
                )

            source_text = f"{row['title']}\n\n{row['body_markdown']}"
            content_hash = _compute_content_hash(source_text)

            await _check_graph_budget(
                session,
                ledger_repo=config.ledger_repo,
                run_cost_usd=run_cost,
                daily_ceiling_usd=config.daily_ceiling_usd,
                run_ceiling_usd=config.run_ceiling_usd,
            )

            from bot.services.llm_gateway import LLMGatewayConfig

            llm_config = LLMGatewayConfig(
                provider=config.llm_provider.provider if hasattr(config.llm_provider, "provider") else "anthropic",
                model=config.llm_provider.model if hasattr(config.llm_provider, "model") else "claude-3-haiku-20240307",
                daily_ceiling_usd=config.daily_ceiling_usd,
                monthly_ceiling_usd=config.daily_ceiling_usd * 30,
                prompt_template_version=_GRAPH_TRIPLES_PROMPT_VERSION,
            )

            extract_result = await extract_graph_triples(
                session,
                source_table="knowledge_cards",
                source_pk=source_pk,
                source_text=source_text,
                source_id=source_pk,
                source_mv_id=None,
                prompt_version=_GRAPH_TRIPLES_PROMPT_VERSION,
                run_id=run_id,
                governance_policy="normal",
                config=llm_config,
                ledger_repo=config.ledger_repo,
                provider=config.llm_provider,
            )

            run_cost = extract_result.cost_usd

            for triple in extract_result.triples:
                edge_key = _compute_edge_key(
                    triple.subject_label, triple.predicate, triple.object_label
                )
                triple_hash = hashlib.sha256(edge_key.encode()).hexdigest()[:16]

                prov = await config.provenance_repo.create_provenance(
                    session,
                    projection_run_id=run_id,
                    source_table="knowledge_cards",
                    source_pk=source_pk,
                    source_card_id=row["id"],
                    triple_hash=triple_hash,
                    graph_node_key=f"card:{source_pk}",
                    source_content_hash=content_hash,
                    governance_policy="normal",
                )

                await config.edge_repo.create_edge(
                    session,
                    graph_provenance_id=prov.id,
                    subject_node_key=triple.subject_label,
                    predicate=triple.predicate,
                    object_node_key=triple.object_label,
                    edge_key=edge_key,
                    confidence_score=Decimal(str(triple.confidence)),
                )

                await config.adapter.merge_node(
                    node_key=triple.subject_label,
                    labels=[triple.subject_type, "MemoryNode"],
                    properties={"node_type": triple.subject_type, "label": triple.subject_label, "provenance_id": str(prov.id)},
                )
                nodes_merged += 1

                await config.adapter.merge_node(
                    node_key=triple.object_label,
                    labels=[triple.object_type, "MemoryNode"],
                    properties={"node_type": triple.object_type, "label": triple.object_label, "provenance_id": str(prov.id)},
                )
                nodes_merged += 1

                await config.adapter.merge_edge(
                    edge_key=edge_key,
                    source_key=triple.subject_label,
                    target_key=triple.object_label,
                    relationship_type=triple.predicate,
                    properties={
                        "predicate": triple.predicate,
                        "confidence": triple.confidence,
                        "provenance_id": str(prov.id),
                        "edge_key_hash": triple_hash,
                    },
                )
                edges_merged += 1
                triples_created += 1

        await config.run_repo.finalize_run(session, run_id, status="completed", cost_usd=run_cost)

        return GraphProjectionRunResult(
            run_id=run_id,
            status="completed",
            sources_total=1,
            sources_processed=1,
            sources_skipped_governance=0,
            sources_skipped_budget=0,
            sources_skipped_unknown=0,
            triples_created=triples_created,
            nodes_merged=nodes_merged,
            edges_merged=edges_merged,
            cost_usd=run_cost,
            errors_list=[],
        )

    except GraphProjectionBudgetError:
        await config.run_repo.finalize_run(session, run_id, status="cost_exceeded", cost_usd=run_cost)
        raise
    except Exception:
        await config.run_repo.finalize_run(session, run_id, status="failed")
        raise


# ─── Public name aliases for spec contract ───────────────────────────────────
# The spec names the repair function project_repair; alias it.
project_repair = project_repair_source

# SUGGESTION Product #1: repair_source alias per §5.C public API name compliance.
repair_source = project_repair_source


# ─── Config factory ──────────────────────────────────────────────────────────


def default_projector_config(adapter: Any) -> GraphProjectorConfig:
    """Build a GraphProjectorConfig wired to the canonical Postgres repos.

    Eliminates the repeated inline _RunRepo / _ProvRepo / _EdgeRepo anonymous class
    boilerplate in admin handlers and the scheduler. adapter is caller-supplied so
    tests can pass a NetworkXAdapter without touching real Neo4j.

    Usage (admin handler / scheduler):
        from bot.services.graph_projector import default_projector_config
        from bot.services.graph_adapter import Neo4jAdapter

        config = default_projector_config(Neo4jAdapter())
        result = await project_incremental(session, config=config, started_by="scheduler")
    """
    from bot.db.repos.graph_edge import create_edge, find_by_provenance as _fp
    from bot.db.repos.graph_projection_run import (
        create_run,
        finalize_run,
        get_active_run,
        update_run_stats,
    )
    from bot.db.repos.graph_provenance import (
        create_provenance,
        find_active,
        find_by_source,
    )
    from bot.db.repos.llm_usage_ledger import LedgerRepo

    class _RunRepo:
        async def create_run(self, s: AsyncSession, *, mode: Any, started_by: Any) -> Any:
            return await create_run(s, mode=mode, started_by=started_by)

        async def update_run_stats(
            self, s: AsyncSession, run_id: int, *, stats_patch: dict
        ) -> None:
            return await update_run_stats(s, run_id, stats_patch=stats_patch)

        async def finalize_run(
            self, s: AsyncSession, run_id: int, *, status: Any, cost_usd: Any = None
        ) -> None:
            return await finalize_run(s, run_id, status=status, cost_usd=cost_usd)

        async def get_active_run(self, s: AsyncSession) -> Any:
            return await get_active_run(s)

    class _ProvRepo:
        async def create_provenance(self, s: AsyncSession, **kw: Any) -> Any:
            return await create_provenance(s, **kw)

        async def find_active(
            self, s: AsyncSession, *, projection_run_id: int | None = None
        ) -> list:
            return await find_active(s, projection_run_id=projection_run_id)

        async def find_by_source(
            self, s: AsyncSession, *, source_table: str, source_pk: str
        ) -> list:
            return await find_by_source(s, source_table=source_table, source_pk=source_pk)

    class _EdgeRepo:
        async def create_edge(self, s: AsyncSession, **kw: Any) -> Any:
            return await create_edge(s, **kw)

        async def find_by_provenance(self, s: AsyncSession, prov_id: int) -> list:
            return await _fp(s, prov_id)

    return GraphProjectorConfig(
        adapter=adapter,
        run_repo=_RunRepo(),
        provenance_repo=_ProvRepo(),
        edge_repo=_EdgeRepo(),
        ledger_repo=LedgerRepo(),
    )
