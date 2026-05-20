"""Unit tests for graph_projector.py helpers (T10-04).

Tests governance pre-filter logic, cost ceiling enforcement, max_sources cap,
and run_id propagation. All tests use NetworkXAdapter (no real Neo4j/Postgres).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services.graph_adapter import NetworkXAdapter


# ─── Minimal fakes ────────────────────────────────────────────────────────────


@dataclass
class FakeRunResult:
    id: int = 1
    mode: str = "incremental"
    status: str = "running"


class FakeRunRepo:
    """Minimal fake for graph_projection_run repo."""

    def __init__(self, run_id: int = 42) -> None:
        self._run_id = run_id
        self.finalized: list[dict] = []
        self.stats_patches: list[dict] = []

    async def create_run(self, session, *, mode, started_by=None):
        return FakeRunResult(id=self._run_id, mode=mode)

    async def update_run_stats(self, session, run_id, *, stats_patch):
        self.stats_patches.append({"run_id": run_id, **stats_patch})

    async def finalize_run(self, session, run_id, *, status, cost_usd=None):
        self.finalized.append({"run_id": run_id, "status": status})

    async def get_active_run(self, session):
        return None


class FakeProvenanceRepo:
    created: list[dict] = field(default_factory=list)
    active_rows: list = field(default_factory=list)

    def __init__(self):
        self.created = []
        self.active_rows = []

    async def create_provenance(self, session, **kwargs):
        row = MagicMock()
        row.id = len(self.created) + 1
        self.created.append(kwargs)
        return row

    async def find_active(self, session, *, projection_run_id=None):
        return self.active_rows

    async def find_by_source(self, session, *, source_table, source_pk):
        return []


class FakeEdgeRepo:
    created: list[dict] = field(default_factory=list)

    def __init__(self):
        self.created = []

    async def create_edge(self, session, **kwargs):
        row = MagicMock()
        row.id = len(self.created) + 1
        self.created.append(kwargs)
        return row

    async def find_by_provenance(self, session, provenance_id):
        return []


class FakeLedgerRepo:
    def __init__(self, daily_cost: Decimal = Decimal("0.00")):
        self._daily_cost = daily_cost

    async def daily_cost_usd(self, session, *, day):
        return self._daily_cost

    async def monthly_cost_usd(self, session, *, year, month):
        return self._daily_cost


# ─── Helpers to build a minimal config ────────────────────────────────────────


def _make_config(
    adapter=None,
    run_repo=None,
    provenance_repo=None,
    edge_repo=None,
    ledger_repo=None,
    daily_ceiling_usd: Decimal | None = None,
    run_ceiling_usd: Decimal | None = None,
    max_sources_per_run: int = 200,
):
    from bot.services.graph_projector import GraphProjectorConfig

    return GraphProjectorConfig(
        adapter=adapter or NetworkXAdapter(),
        run_repo=run_repo or FakeRunRepo(),
        provenance_repo=provenance_repo or FakeProvenanceRepo(),
        edge_repo=edge_repo or FakeEdgeRepo(),
        ledger_repo=ledger_repo or FakeLedgerRepo(),
        daily_ceiling_usd=daily_ceiling_usd or Decimal("2.00"),
        run_ceiling_usd=run_ceiling_usd or Decimal("0.50"),
        max_sources_per_run=max_sources_per_run,
    )


# ─── Tests: governance pre-filter (skips non-normal policy) ──────────────────


def test_governance_filter_imports():
    """GraphProjectorConfig and result types are importable."""
    from bot.services.graph_projector import GraphProjectionRunResult, GraphProjectorConfig

    assert GraphProjectorConfig is not None
    assert GraphProjectionRunResult is not None


def test_graph_projection_run_result_is_frozen():
    """GraphProjectionRunResult is a frozen dataclass."""
    from bot.services.graph_projector import GraphProjectionRunResult

    result = GraphProjectionRunResult(
        run_id=1,
        status="completed",
        sources_total=10,
        sources_processed=8,
        sources_skipped_governance=1,
        sources_skipped_budget=0,
        sources_skipped_unknown=1,
        triples_created=5,
        nodes_merged=4,
        edges_merged=5,
        cost_usd=Decimal("0.10"),
        errors_list=[],
    )
    with pytest.raises((AttributeError, TypeError)):
        result.run_id = 999  # type: ignore[misc]


def test_graph_projector_config_is_frozen():
    """GraphProjectorConfig is a frozen dataclass."""
    cfg = _make_config()
    with pytest.raises((AttributeError, TypeError)):
        cfg.max_sources_per_run = 999  # type: ignore[misc]


# ─── Tests: cost ceiling enforcement ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_ceiling_guard_daily_exceeded():
    """_check_graph_budget raises GraphProjectionBudgetError when daily cost exceeded."""
    from bot.services.graph_common import GraphProjectionBudgetError
    from bot.services.graph_projector import _check_graph_budget

    session = AsyncMock()
    ledger = FakeLedgerRepo(daily_cost=Decimal("2.50"))  # over $2.00 daily ceiling
    run_cost = Decimal("0.00")

    with pytest.raises(GraphProjectionBudgetError, match="daily"):
        await _check_graph_budget(
            session,
            ledger_repo=ledger,
            run_cost_usd=run_cost,
            daily_ceiling_usd=Decimal("2.00"),
            run_ceiling_usd=Decimal("0.50"),
        )


@pytest.mark.asyncio
async def test_cost_ceiling_guard_run_exceeded():
    """_check_graph_budget raises GraphProjectionBudgetError when run cost exceeded."""
    from bot.services.graph_common import GraphProjectionBudgetError
    from bot.services.graph_projector import _check_graph_budget

    session = AsyncMock()
    ledger = FakeLedgerRepo(daily_cost=Decimal("0.00"))
    run_cost = Decimal("0.55")  # over $0.50 run ceiling

    with pytest.raises(GraphProjectionBudgetError, match="run"):
        await _check_graph_budget(
            session,
            ledger_repo=ledger,
            run_cost_usd=run_cost,
            daily_ceiling_usd=Decimal("2.00"),
            run_ceiling_usd=Decimal("0.50"),
        )


@pytest.mark.asyncio
async def test_cost_ceiling_guard_passes_under_limit():
    """_check_graph_budget does not raise when under both ceilings."""
    from bot.services.graph_projector import _check_graph_budget

    session = AsyncMock()
    ledger = FakeLedgerRepo(daily_cost=Decimal("0.30"))
    run_cost = Decimal("0.10")

    # Should not raise
    await _check_graph_budget(
        session,
        ledger_repo=ledger,
        run_cost_usd=run_cost,
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
    )


# ─── Tests: max_sources cap ───────────────────────────────────────────────────


def test_max_sources_cap_constant_exists():
    """GRAPH_PROJECTION_MAX_SOURCES_DEFAULT constant exists."""
    from bot.services.graph_projector import GRAPH_PROJECTION_MAX_SOURCES_DEFAULT

    assert GRAPH_PROJECTION_MAX_SOURCES_DEFAULT == 200


def test_graph_rebuild_lock_id_is_integer():
    """GRAPH_REBUILD_LOCK_ID is a valid PostgreSQL signed-int64 advisory lock id."""
    from bot.services.graph_projector import GRAPH_REBUILD_LOCK_ID

    assert isinstance(GRAPH_REBUILD_LOCK_ID, int)
    # Must fit in signed int64: -2^63 to 2^63-1
    assert -(2**63) <= GRAPH_REBUILD_LOCK_ID <= 2**63 - 1


# ─── Tests: run_id propagation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_returns_correct_run_id():
    """dry_run returns GraphProjectionRunResult with run_id matching created run."""
    from bot.services.graph_projector import dry_run

    run_repo = FakeRunRepo(run_id=99)
    config = _make_config(run_repo=run_repo)
    session = AsyncMock()

    # Patch out the governance query to return empty source list
    with patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[]),
    ):
        result = await dry_run(session, limit=5, config=config)

    assert result.run_id == 99
    assert result.status == "dry_run_complete"
    assert result.sources_total == 0
    assert result.sources_processed == 0


@pytest.mark.asyncio
async def test_dry_run_does_not_write_provenance():
    """dry_run creates run row but writes zero graph_provenance rows."""
    from bot.services.graph_projector import dry_run

    provenance_repo = FakeProvenanceRepo()
    config = _make_config(provenance_repo=provenance_repo)
    session = AsyncMock()

    with patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[]),
    ):
        await dry_run(session, limit=5, config=config)

    assert len(provenance_repo.created) == 0


@pytest.mark.asyncio
async def test_dry_run_finalizes_run_as_dry_run_complete():
    """dry_run finalizes the run with status='dry_run_complete'."""
    from bot.services.graph_projector import dry_run

    run_repo = FakeRunRepo(run_id=7)
    config = _make_config(run_repo=run_repo)
    session = AsyncMock()

    with patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[]),
    ):
        await dry_run(session, limit=5, config=config)

    assert any(
        f["status"] == "dry_run_complete" and f["run_id"] == 7
        for f in run_repo.finalized
    )


# ─── Tests: service disabled flag ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_incremental_raises_when_flag_disabled():
    """project_incremental raises ServiceDisabledError when feature flag is off."""
    from bot.services.graph_projector import ServiceDisabledError, project_incremental

    config = _make_config()
    session = AsyncMock()

    # Patch feature flag check to return False (disabled)
    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(ServiceDisabledError, match="memory.graph.projection.enabled"):
            await project_incremental(session, config=config)


@pytest.mark.asyncio
async def test_full_rebuild_raises_when_flag_disabled():
    """project_full_rebuild raises ServiceDisabledError when feature flag is off."""
    from bot.services.graph_projector import ServiceDisabledError, project_full_rebuild

    config = _make_config()
    session = AsyncMock()

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(ServiceDisabledError):
            await project_full_rebuild(session, config=config)


# ─── Tests: CRITICAL — per-source atomicity (SAVEPOINT) ──────────────────────


@pytest.mark.asyncio
async def test_incremental_neo4j_failure_marks_provenance_inactive():
    """Neo4j failure mid-loop marks provenance inactive and continues (SAVEPOINT fix).

    When Neo4j raises for source N, the provenance row should be marked inactive
    (compensating action) and the error recorded in errors_list.
    The run must NOT raise; it should complete with errors.
    """
    from bot.services.llm_gateway import ExtractGraphTriplesResult, GraphTriple
    from bot.services.graph_projector import project_incremental

    # Track provenance creation calls
    provenance_repo = FakeProvenanceRepo()

    # Track which provenance IDs were marked inactive
    marked_inactive: list[int] = []

    # Adapter that raises on merge_node
    class FailingAdapter:
        async def merge_node(self, node_key, labels, properties):
            raise RuntimeError("Neo4j connection failed")

        async def merge_edge(self, *args, **kwargs):
            raise RuntimeError("Neo4j connection failed")

    fake_triple = GraphTriple(
        subject_label="A",
        subject_type="Topic",
        predicate="MENTIONS",
        object_label="B",
        object_type="Topic",
        confidence=0.9,
        source_id="1",
    )
    fake_extract = ExtractGraphTriplesResult(
        triples=[fake_triple],
        llm_usage_ledger_id=None,
        cost_usd=Decimal("0.01"),
        skipped_total=0,
    )

    fake_provider = MagicMock()
    fake_provider.provider = "anthropic"
    fake_provider.model = "claude-3-haiku-20240307"

    config = _make_config(
        adapter=FailingAdapter(),
        provenance_repo=provenance_repo,
        max_sources_per_run=10,
    )
    # Inject provider
    from bot.services.graph_projector import GraphProjectorConfig
    config = GraphProjectorConfig(
        adapter=FailingAdapter(),
        run_repo=FakeRunRepo(),
        provenance_repo=provenance_repo,
        edge_repo=FakeEdgeRepo(),
        ledger_repo=FakeLedgerRepo(),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
        max_sources_per_run=10,
        llm_provider=fake_provider,
    )

    session = AsyncMock()
    # begin_nested must be a synchronous call returning an async CM (SQLAlchemy semantics)
    nested_sp = MagicMock()
    nested_sp.__aenter__ = AsyncMock(return_value=nested_sp)
    nested_sp.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_sp)
    session.flush = AsyncMock()

    card = {"id": "1", "title": "Card", "body_markdown": "body", "created_at": "2024-01-01"}

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[card]),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_message_versions",
        new=AsyncMock(return_value=[]),
    ), patch(
        "bot.services.graph_projector._get_last_successful_run_timestamp",
        new=AsyncMock(return_value=None),
    ), patch(
        "bot.services.graph_projector.extract_graph_triples",
        new=AsyncMock(return_value=fake_extract),
    ), patch(
        "bot.services.graph_projector._mark_provenance_inactive",
        new=AsyncMock(side_effect=lambda s, pid: marked_inactive.append(pid)),
    ):
        result = await project_incremental(session, config=config)

    # Run completes (does not raise)
    assert result is not None
    # Errors were recorded
    assert len(result.errors_list) > 0
    # mark_provenance_inactive was called for the failed provenance
    assert len(marked_inactive) > 0


@pytest.mark.asyncio
async def test_incremental_partial_neo4j_success_finalizes_as_failed_if_any_errors():
    """Run finalizes as 'failed' (not 'completed') when there are any Neo4j errors."""
    from bot.services.llm_gateway import ExtractGraphTriplesResult, GraphTriple
    from bot.services.graph_projector import project_incremental

    class FailingAdapter:
        async def merge_node(self, node_key, labels, properties):
            raise RuntimeError("Neo4j down")

        async def merge_edge(self, *args, **kwargs):
            pass

    fake_triple = GraphTriple(
        subject_label="A",
        subject_type="Topic",
        predicate="MENTIONS",
        object_label="B",
        object_type="Topic",
        confidence=0.9,
        source_id="1",
    )
    fake_extract = ExtractGraphTriplesResult(
        triples=[fake_triple],
        llm_usage_ledger_id=None,
        cost_usd=Decimal("0.01"),
        skipped_total=0,
    )

    fake_provider = MagicMock()
    fake_provider.provider = "anthropic"
    fake_provider.model = "claude-3-haiku-20240307"

    run_repo = FakeRunRepo(run_id=55)
    from bot.services.graph_projector import GraphProjectorConfig
    config = GraphProjectorConfig(
        adapter=FailingAdapter(),
        run_repo=run_repo,
        provenance_repo=FakeProvenanceRepo(),
        edge_repo=FakeEdgeRepo(),
        ledger_repo=FakeLedgerRepo(),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
        max_sources_per_run=10,
        llm_provider=fake_provider,
    )

    session = AsyncMock()
    # begin_nested must be a sync call returning an async CM (SQLAlchemy semantics)
    nested_sp = MagicMock()
    nested_sp.__aenter__ = AsyncMock(return_value=nested_sp)
    nested_sp.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_sp)
    session.flush = AsyncMock()

    card = {"id": "1", "title": "Card", "body_markdown": "body", "created_at": "2024-01-01"}

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[card]),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_message_versions",
        new=AsyncMock(return_value=[]),
    ), patch(
        "bot.services.graph_projector._get_last_successful_run_timestamp",
        new=AsyncMock(return_value=None),
    ), patch(
        "bot.services.graph_projector.extract_graph_triples",
        new=AsyncMock(return_value=fake_extract),
    ), patch(
        "bot.services.graph_projector._mark_provenance_inactive",
        new=AsyncMock(),
    ):
        result = await project_incremental(session, config=config)

    # When any errors exist, final status should be 'failed' not 'completed'
    assert result.status == "failed"
    # run_repo also finalized as failed
    assert any(f["status"] == "failed" for f in run_repo.finalized)


# ─── Tests: HIGH-1 — since_timestamp filter ───────────────────────────────────


@pytest.mark.asyncio
async def test_incremental_with_since_timestamp_skips_older_sources():
    """_fetch_eligible_cards filters by since_timestamp when provided.

    Cards updated before since_timestamp must be excluded.
    """
    from datetime import datetime, timezone
    from bot.services.graph_projector import _fetch_eligible_cards

    session = AsyncMock()
    # Test that the WHERE clause is built with since_timestamp
    captured_params: list[dict] = []

    async def mock_execute(stmt, params=None):
        if params:
            captured_params.append(dict(params))
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        return result

    session.execute = mock_execute

    ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
    await _fetch_eligible_cards(session, limit=10, since_timestamp=ts)

    # Verify since_timestamp was passed as a query param
    assert len(captured_params) == 1
    assert "since_timestamp" in captured_params[0]
    assert captured_params[0]["since_timestamp"] == ts


# ─── Tests: HIGH-2 — incremental lock vs full_rebuild ────────────────────────


@pytest.mark.asyncio
async def test_incremental_aborts_during_full_rebuild():
    """project_incremental raises RefusalError when full_rebuild lock is held."""
    from bot.services.graph_common import RefusalError
    from bot.services.graph_projector import project_incremental

    session = AsyncMock()

    # pg_try_advisory_xact_lock returns FALSE (lock held by full_rebuild)
    lock_result = MagicMock()
    lock_result.scalar.return_value = False
    session.execute = AsyncMock(return_value=lock_result)

    config = _make_config()

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ):
        with pytest.raises(RefusalError, match="graph rebuild in progress"):
            await project_incremental(session, config=config)


# ─── Tests: HIGH-3 — budget call_type filter ─────────────────────────────────


@pytest.mark.asyncio
async def test_check_graph_budget_passes_call_type_graph_projection():
    """_check_graph_budget calls daily_cost_usd with call_type='graph_projection'.

    Verifies that budget check doesn't mix cost with other call types.
    """
    from bot.services.graph_projector import _check_graph_budget

    session = AsyncMock()
    captured_kwargs: list[dict] = []

    class TrackingLedger:
        async def daily_cost_usd(self, session, *, day, call_type=None):
            captured_kwargs.append({"day": day, "call_type": call_type})
            return Decimal("0.00")

    await _check_graph_budget(
        session,
        ledger_repo=TrackingLedger(),
        run_cost_usd=Decimal("0.00"),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
    )

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["call_type"] == "graph_projection"


# ─── Tests: CONCERN/Product #12 — message_version event nodes ────────────────


@pytest.mark.asyncio
async def test_incremental_projects_message_version_event_nodes():
    """project_incremental projects message_version event nodes (no LLM).

    When message_versions are fetched, event nodes are merged into the adapter.
    """
    from bot.services.graph_projector import project_incremental

    session = AsyncMock()
    nested_sp = AsyncMock()
    nested_sp.__aenter__ = AsyncMock(return_value=nested_sp)
    nested_sp.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = AsyncMock(return_value=nested_sp)
    session.flush = AsyncMock()

    adapter = NetworkXAdapter()
    run_repo = FakeRunRepo(run_id=77)
    provenance_repo = FakeProvenanceRepo()

    from bot.services.graph_projector import GraphProjectorConfig
    config = GraphProjectorConfig(
        adapter=adapter,
        run_repo=run_repo,
        provenance_repo=provenance_repo,
        edge_repo=FakeEdgeRepo(),
        ledger_repo=FakeLedgerRepo(),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
        max_sources_per_run=10,
        llm_provider=None,  # No LLM — only event nodes
    )

    mv = {"id": 101, "chat_message_id": 5001, "version_seq": 1, "created_at": "2024-01-01", "chat_id": -100}

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[]),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_message_versions",
        new=AsyncMock(return_value=[mv]),
    ), patch(
        "bot.services.graph_projector._get_last_successful_run_timestamp",
        new=AsyncMock(return_value=None),
    ):
        result = await project_incremental(session, config=config)

    # Event node should be merged in adapter
    assert "msg:101" in adapter.nodes
    assert result.nodes_merged >= 1


# ─── Tests: SUGGESTION — repair_source alias ─────────────────────────────────


def test_repair_source_alias_exists():
    """repair_source is an alias for project_repair_source (spec §5.C API name)."""
    from bot.services.graph_projector import repair_source, project_repair_source

    assert repair_source is project_repair_source


# ─── Tests: SUGGESTION — since_timestamp auto-resume from last run ────────────


@pytest.mark.asyncio
async def test_incremental_auto_resumes_from_last_successful_run():
    """project_incremental without since_* auto-resumes from last completed run.

    When no since_run_id or since_timestamp is provided, the projector should
    query the last completed run and use its started_at as the since_timestamp.
    All eligible cards are fetched when no prior run exists (first run).
    """
    from bot.services.graph_projector import project_incremental

    session = AsyncMock()

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_projector._fetch_eligible_cards",
        new=AsyncMock(return_value=[]),
    ) as mock_fetch, patch(
        "bot.services.graph_projector._fetch_eligible_message_versions",
        new=AsyncMock(return_value=[]),
    ), patch(
        "bot.services.graph_projector._get_last_successful_run_timestamp",
        new=AsyncMock(return_value=None),
    ):
        config = _make_config()
        await project_incremental(session, config=config)

    # When no prior run, fetch is called with since_timestamp=None (scan all)
    assert mock_fetch.called
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs.get("since_timestamp") is None


# ─── Tests: SUGGESTION — >= vs > edge case ───────────────────────────────────


@pytest.mark.asyncio
async def test_cost_ceiling_allows_exact_ceiling_value():
    """_check_graph_budget does NOT raise when cost equals the ceiling exactly.

    The check uses > (strict), not >= (allows the exact ceiling value).
    """
    from bot.services.graph_projector import _check_graph_budget

    session = AsyncMock()
    # Daily cost exactly at ceiling — should NOT raise
    ledger = FakeLedgerRepo(daily_cost=Decimal("2.00"))

    # Should not raise — exact ceiling is allowed
    await _check_graph_budget(
        session,
        ledger_repo=ledger,
        run_cost_usd=Decimal("0.00"),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
    )


@pytest.mark.asyncio
async def test_cost_ceiling_raises_strictly_above_ceiling():
    """_check_graph_budget raises when cost strictly exceeds the ceiling."""
    from bot.services.graph_common import GraphProjectionBudgetError
    from bot.services.graph_projector import _check_graph_budget

    session = AsyncMock()
    ledger = FakeLedgerRepo(daily_cost=Decimal("2.01"))

    with pytest.raises(GraphProjectionBudgetError, match="daily"):
        await _check_graph_budget(
            session,
            ledger_repo=ledger,
            run_cost_usd=Decimal("0.00"),
            daily_ceiling_usd=Decimal("2.00"),
            run_ceiling_usd=Decimal("0.50"),
        )
