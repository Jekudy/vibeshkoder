"""Integration tests for bot/services/graph_projector.py (T10-04 / Phase 10).

Uses the temp_database_url pattern: creates an isolated temp Postgres DB,
runs alembic upgrade head (includes migrations 060-064), tests projector
behaviour, then drops the DB.

Tests are skipped (not failed) if Postgres is unreachable.

Test inventory:
- test_dry_run_no_writes — postgres state unchanged
- test_incremental_writes_provenance_and_edges — provenance + edge rows created
- test_incremental_skips_offrecord_sources — governance filter
- test_full_rebuild_acquires_advisory_lock — pg_advisory_lock acquired
- test_full_rebuild_aborts_if_purge_pending — SKIPPED (needs T10-06 migration 063)
- test_repair_mode_re_extracts_single_source — repair for specific source
- test_cost_ceiling_aborts_run — budget guard terminates projection
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine.url import URL, make_url

from tests.conftest import DEFAULT_LOCAL_POSTGRES_URL

pytestmark = pytest.mark.usefixtures("app_env")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ─── Temp DB helpers ──────────────────────────────────────────────────────────


def _base_test_url() -> URL:
    raw_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )
    return make_url(raw_url)


def _asyncpg_kwargs(url: URL, *, database: str | None = None) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database or url.database,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _create_database(admin_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await conn.execute(f"CREATE DATABASE {_quote_identifier(database_name)}")
    finally:
        await conn.close()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database_name,
        )
        await conn.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
    finally:
        await conn.close()


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=True,
    )


@pytest_asyncio.fixture()
async def projector_db_url() -> AsyncIterator[str]:
    """Temp DB with alembic upgrade head (includes migrations 060-064)."""
    base_url = _base_test_url()
    database_name = f"shkoder_projector_{uuid.uuid4().hex[:12]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    db_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    try:
        _run_alembic(db_url, "upgrade", "head")
    except subprocess.CalledProcessError as exc:
        await _drop_database(base_url, database_name)
        pytest.skip(f"alembic upgrade head failed: {exc.stderr}")

    try:
        yield db_url
    finally:
        await _drop_database(base_url, database_name)


@pytest_asyncio.fixture()
async def proj_session(projector_db_url: str) -> AsyncIterator:
    """AsyncSession connected to the migrated temp DB.

    Does NOT wrap in an outer transaction (to allow advisory lock tests to commit).
    Each test must manage its own cleanup.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(projector_db_url, echo=False)
    try:
        Session = async_sessionmaker(class_=AsyncSession, expire_on_commit=False)
        async with Session(bind=engine) as session:
            yield session
    finally:
        await engine.dispose()


# ─── Fake fakes for integration tests ────────────────────────────────────────


class FakeLedgerRepo:
    def __init__(self, daily_cost: Decimal = Decimal("0.00")):
        self._daily_cost = daily_cost

    async def daily_cost_usd(self, session, *, day, call_type: str | None = None):
        return self._daily_cost


def _make_real_config(session_engine_url: str, adapter=None, ledger=None):
    """Build a GraphProjectorConfig using real repo implementations."""
    from bot.db.repos.graph_projection_run import (
        create_run,
        finalize_run,
        update_run_stats,
    )
    from bot.db.repos.graph_provenance import create_provenance, find_active, find_by_source
    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_projector import GraphProjectorConfig

    # Build thin wrapper objects that match the Protocol shapes
    @dataclass
    class RunRepo:
        async def create_run(self, session, *, mode, started_by=None):
            return await create_run(session, mode=mode, started_by=started_by)

        async def update_run_stats(self, session, run_id, *, stats_patch):
            return await update_run_stats(session, run_id, stats_patch=stats_patch)

        async def finalize_run(self, session, run_id, *, status, cost_usd=None):
            return await finalize_run(session, run_id, status=status, cost_usd=cost_usd)

        async def get_active_run(self, session):
            return None

    @dataclass
    class ProvenanceRepo:
        async def create_provenance(self, session, **kwargs):
            return await create_provenance(session, **kwargs)

        async def find_active(self, session, *, projection_run_id=None):
            return await find_active(session, projection_run_id=projection_run_id)

        async def find_by_source(self, session, *, source_table, source_pk):
            return await find_by_source(session, source_table=source_table, source_pk=source_pk)

    @dataclass
    class EdgeRepo:
        async def create_edge(self, session, **kwargs):
            from bot.db.repos.graph_edge import create_edge
            return await create_edge(session, **kwargs)

        async def find_by_provenance(self, session, provenance_id):
            from bot.db.repos.graph_edge import find_by_provenance
            return await find_by_provenance(session, provenance_id)

    return GraphProjectorConfig(
        adapter=adapter or NetworkXAdapter(),
        run_repo=RunRepo(),
        provenance_repo=ProvenanceRepo(),
        edge_repo=EdgeRepo(),
        ledger_repo=ledger or FakeLedgerRepo(),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
        max_sources_per_run=200,
        llm_provider=None,
    )


# ─── Data helpers ─────────────────────────────────────────────────────────────


async def _insert_test_user(session) -> int:
    """Insert a minimal user row and return the user id."""
    from sqlalchemy import text
    result = await session.execute(
        text(
            "INSERT INTO users (id, username, first_name, is_admin, created_at, updated_at) "
            "VALUES (:id, 'testuser', 'Test', false, now(), now()) "
            "RETURNING id"
        ),
        {"id": 999000001},
    )
    return result.scalar_one()


async def _insert_approved_card(session, *, user_id: int, title: str = "Test Card") -> str:
    """Insert an approved knowledge_card and return its id (as str)."""
    from sqlalchemy import text
    card_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO knowledge_cards "
            "(id, title, body_markdown, card_status, approved_by_user_id, approved_at, created_at, updated_at) "
            "VALUES (:id, :title, 'body text', 'approved', :user_id, now(), now(), now())"
        ),
        {"id": card_id, "title": title, "user_id": user_id},
    )
    return str(card_id)


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_no_writes(proj_session) -> None:
    """dry_run creates projection_run row but writes NO provenance or edge rows."""
    from sqlalchemy import text

    from bot.services.graph_projector import dry_run

    config = _make_real_config(None)

    result = await dry_run(proj_session, limit=5, config=config)
    await proj_session.commit()

    assert result.status == "dry_run_complete"
    assert result.run_id is not None

    # Verify: projection_run row exists
    run_rows = await proj_session.execute(
        text("SELECT count(*) FROM graph_projection_runs WHERE id = :rid"),
        {"rid": result.run_id},
    )
    assert run_rows.scalar_one() == 1

    # Verify: NO provenance rows written
    prov_rows = await proj_session.execute(
        text("SELECT count(*) FROM graph_provenance WHERE projection_run_id = :rid"),
        {"rid": result.run_id},
    )
    assert prov_rows.scalar_one() == 0

    # Verify: NO edge rows written
    edge_rows = await proj_session.execute(
        text(
            "SELECT count(*) FROM graph_edges ge "
            "JOIN graph_provenance gp ON gp.id = ge.graph_provenance_id "
            "WHERE gp.projection_run_id = :rid"
        ),
        {"rid": result.run_id},
    )
    assert edge_rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_incremental_writes_provenance_and_edges(proj_session) -> None:
    """project_incremental writes graph_provenance and graph_edges rows for a card.

    Uses a fake LLM extractor to avoid real API calls.
    """
    from sqlalchemy import text
    from unittest.mock import AsyncMock, patch

    # Enable feature flag
    from bot.db.repos.feature_flag import FeatureFlagRepo
    await FeatureFlagRepo.set_enabled(proj_session, "memory.graph.projection.enabled", True)
    await proj_session.flush()

    # Insert test data
    user_id = await _insert_test_user(proj_session)
    card_id = await _insert_approved_card(proj_session, user_id=user_id, title="Шкодерbot")
    await proj_session.flush()

    from bot.services.llm_gateway import ExtractGraphTriplesResult, GraphTriple
    from bot.services.graph_adapter import NetworkXAdapter

    fake_triple = GraphTriple(
        subject_label="Шкодерbot",
        subject_type="Topic",
        predicate="MENTIONS",
        object_label="Python",
        object_type="Topic",
        confidence=0.9,
        source_id=card_id,
    )
    fake_extract_result = ExtractGraphTriplesResult(
        triples=[fake_triple],
        llm_usage_ledger_id=None,
        cost_usd=Decimal("0.01"),
        skipped_total=0,
    )

    # Build a fake LLM provider
    fake_provider = MagicMock()
    fake_provider.provider = "anthropic"
    fake_provider.model = "claude-3-haiku-20240307"

    config = _make_real_config(None, adapter=NetworkXAdapter())
    # Inject fake LLM provider
    from bot.services.graph_projector import GraphProjectorConfig
    config = GraphProjectorConfig(
        adapter=config.adapter,
        run_repo=config.run_repo,
        provenance_repo=config.provenance_repo,
        edge_repo=config.edge_repo,
        ledger_repo=config.ledger_repo,
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
        max_sources_per_run=200,
        llm_provider=fake_provider,
    )

    with patch(
        "bot.services.graph_projector.extract_graph_triples",
        new=AsyncMock(return_value=fake_extract_result),
    ):
        result = await _run_incremental_with_flag_enabled(proj_session, config)

    await proj_session.commit()

    assert result.status == "completed"
    assert result.triples_created >= 1
    assert result.edges_merged >= 1

    # Verify graph_provenance rows exist
    prov_count = await proj_session.execute(
        text("SELECT count(*) FROM graph_provenance WHERE projection_run_id = :rid"),
        {"rid": result.run_id},
    )
    assert prov_count.scalar_one() >= 1

    # Verify graph_edges rows exist
    edge_count = await proj_session.execute(
        text(
            "SELECT count(*) FROM graph_edges ge "
            "JOIN graph_provenance gp ON gp.id = ge.graph_provenance_id "
            "WHERE gp.projection_run_id = :rid"
        ),
        {"rid": result.run_id},
    )
    assert edge_count.scalar_one() >= 1


async def _run_incremental_with_flag_enabled(session, config):
    """Helper: run project_incremental (flag already enabled by caller)."""
    from bot.services.graph_projector import project_incremental

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ):
        return await project_incremental(session, config=config)


@pytest.mark.asyncio
async def test_incremental_skips_offrecord_sources(proj_session) -> None:
    """project_incremental skips cards whose source messages have a forget_event.

    Sets up: chat_message → message_version → card_source → knowledge_card → forget_event.
    The governance query in _fetch_eligible_cards excludes cards with a forgotten message.
    """
    from sqlalchemy import text

    # Enable flag
    from bot.db.repos.feature_flag import FeatureFlagRepo
    await FeatureFlagRepo.set_enabled(proj_session, "memory.graph.projection.enabled", True)
    await proj_session.flush()

    user_id = await _insert_test_user(proj_session)

    # Insert a chat_message (chat_id is plain BigInteger, no FK to a chats table)
    await proj_session.execute(
        text(
            "INSERT INTO chat_messages (id, chat_id, message_id, user_id, "
            "date, memory_policy, created_at) "
            "VALUES (10001, -1001, 1, :uid, now(), 'normal', now())"
        ),
        {"uid": user_id},
    )
    # Insert a message_version
    await proj_session.execute(
        text(
            "INSERT INTO message_versions "
            "(id, chat_message_id, version_seq, content_hash, is_redacted, captured_at) "
            "VALUES (20001, 10001, 1, 'abc123', false, now())"
        )
    )
    # Insert a knowledge card
    card_id = uuid.uuid4()
    await proj_session.execute(
        text(
            "INSERT INTO knowledge_cards "
            "(id, title, body_markdown, card_status, approved_by_user_id, approved_at, created_at, updated_at) "
            "VALUES (:id, 'Forgotten Card', 'body', 'approved', :uid, now(), now(), now())"
        ),
        {"id": card_id, "uid": user_id},
    )
    # Link card → message_version via card_sources
    await proj_session.execute(
        text(
            "INSERT INTO card_sources (card_id, message_version_id, position) "
            "VALUES (:cid, 20001, 0)"
        ),
        {"cid": card_id},
    )
    # Insert a forget_event targeting the message (id=10001)
    tombstone = f"test-forget-{uuid.uuid4().hex}"
    await proj_session.execute(
        text(
            "INSERT INTO forget_events "
            "(target_type, target_id, authorized_by, tombstone_key, policy, status, created_at) "
            "VALUES ('message', '10001', 'admin', :tk, 'forgotten', 'completed', now())"
        ),
        {"tk": tombstone},
    )
    await proj_session.flush()

    config = _make_real_config(None)

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ):
        from bot.services.graph_projector import project_incremental
        result = await project_incremental(proj_session, config=config)

    # The forgotten card must NOT be projected (governance pre-filter)
    prov_count = await proj_session.execute(
        text("SELECT count(*) FROM graph_provenance WHERE projection_run_id = :rid"),
        {"rid": result.run_id},
    )
    # Since the card has a forgotten source, it should be excluded by the governance query
    assert prov_count.scalar_one() == 0


@pytest.mark.asyncio
async def test_full_rebuild_acquires_advisory_lock(proj_session) -> None:
    """project_full_rebuild acquires pg_advisory_xact_lock(GRAPH_REBUILD_LOCK_ID)."""
    from sqlalchemy import text

    # Enable flag
    from bot.db.repos.feature_flag import FeatureFlagRepo
    await FeatureFlagRepo.set_enabled(proj_session, "memory.graph.projection.enabled", True)
    await proj_session.flush()

    from bot.services.graph_projector import GRAPH_REBUILD_LOCK_ID

    # Track whether advisory lock was acquired by checking pg_locks during execution
    lock_acquired_during_run: list[bool] = []

    original_execute = proj_session.execute

    async def _spy_execute(stmt, params=None, *args, **kwargs):
        result = await original_execute(stmt, params, *args, **kwargs)
        # After any execute, check if our lock is in pg_locks
        try:
            lock_check = await original_execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND classid = :upper AND objid = :lower"
                ),
                {
                    "upper": (GRAPH_REBUILD_LOCK_ID >> 32) & 0xFFFFFFFF,
                    "lower": GRAPH_REBUILD_LOCK_ID & 0xFFFFFFFF,
                },
            )
            count = lock_check.scalar_one()
            if count > 0:
                lock_acquired_during_run.append(True)
        except Exception:
            pass
        return result

    config = _make_real_config(None)

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ):
        from bot.services.graph_projector import project_full_rebuild
        with patch.object(proj_session, "execute", side_effect=_spy_execute):
            result = await project_full_rebuild(proj_session, config=config)

    assert result.status == "completed"
    # Advisory lock should have been acquired at least once
    assert len(lock_acquired_during_run) > 0


@pytest.mark.asyncio
async def test_full_rebuild_aborts_if_purge_pending() -> None:
    """full_rebuild pre-condition: aborts when graph_purge_pending has in_flight rows.

    SKIPPED: Requires T10-06 migration 063 (graph_purge_pending table).
    This test will be activated when T10-06 merges to main.
    Phase 10.5 carryover — see PHASE10_PLAN.md §7 T10-06.
    """
    pytest.skip(
        "Needs T10-06 migration 063 (graph_purge_pending table) — Phase 10.5 carryover. "
        "Activate this test after T10-06 merges."
    )


@pytest.mark.asyncio
async def test_repair_mode_re_extracts_single_source(proj_session) -> None:
    """project_repair_source re-projects a specific (source_table, source_pk) pair."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.db.repos.feature_flag import FeatureFlagRepo
    await FeatureFlagRepo.set_enabled(proj_session, "memory.graph.projection.enabled", True)
    await proj_session.flush()

    # Insert test card
    user_id = await _insert_test_user(proj_session)
    card_id = await _insert_approved_card(proj_session, user_id=user_id, title="Repair Card")
    await proj_session.flush()

    from bot.services.llm_gateway import ExtractGraphTriplesResult, GraphTriple

    fake_triple = GraphTriple(
        subject_label="RepairSubject",
        subject_type="Topic",
        predicate="RELATED_TO",
        object_label="RepairObject",
        object_type="Topic",
        confidence=0.8,
        source_id=card_id,
    )
    fake_extract_result = ExtractGraphTriplesResult(
        triples=[fake_triple],
        llm_usage_ledger_id=None,
        cost_usd=Decimal("0.005"),
        skipped_total=0,
    )

    fake_provider = MagicMock()
    fake_provider.provider = "anthropic"
    fake_provider.model = "claude-3-haiku-20240307"

    from bot.services.graph_adapter import NetworkXAdapter
    from bot.services.graph_projector import GraphProjectorConfig

    config = GraphProjectorConfig(
        adapter=NetworkXAdapter(),
        run_repo=_make_real_config(None).run_repo,
        provenance_repo=_make_real_config(None).provenance_repo,
        edge_repo=_make_real_config(None).edge_repo,
        ledger_repo=FakeLedgerRepo(),
        daily_ceiling_usd=Decimal("2.00"),
        run_ceiling_usd=Decimal("0.50"),
        max_sources_per_run=200,
        llm_provider=fake_provider,
    )

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "bot.services.graph_projector.extract_graph_triples",
        new=AsyncMock(return_value=fake_extract_result),
    ):
        from bot.services.graph_projector import project_repair_source
        result = await project_repair_source(
            proj_session,
            source_table="knowledge_cards",
            source_pk=card_id,
            config=config,
        )

    await proj_session.commit()

    assert result.status == "completed"
    assert result.triples_created >= 1
    assert result.run_id is not None


@pytest.mark.asyncio
async def test_cost_ceiling_aborts_run(proj_session) -> None:
    """project_incremental raises GraphProjectionBudgetError when daily ceiling exceeded."""
    from bot.services.graph_common import GraphProjectionBudgetError
    from bot.services.graph_projector import project_incremental

    # Daily cost already at ceiling
    expensive_ledger = FakeLedgerRepo(daily_cost=Decimal("2.50"))

    config = _make_real_config(None, ledger=expensive_ledger)

    with patch(
        "bot.services.graph_projector._is_projection_enabled",
        new=AsyncMock(return_value=True),
    ):
        with pytest.raises(GraphProjectionBudgetError, match="daily"):
            await project_incremental(proj_session, config=config)

    # Run should be marked cost_exceeded
    from sqlalchemy import text
    run_rows = await proj_session.execute(
        text("SELECT status FROM graph_projection_runs ORDER BY id DESC LIMIT 1")
    )
    status = run_rows.scalar_one_or_none()
    assert status == "cost_exceeded"
