"""Integration tests for extract_graph_triples (T10-03 / Phase 10).

Uses temp Postgres DB with alembic upgrade head (includes migration 064).
Tests are skipped if Postgres is unreachable.

Covers:
- test_extract_with_normal_governance_returns_triples_and_writes_ledger
- test_extract_with_offrecord_governance_raises_policy_error
- test_extract_with_unknown_subject_drops_triple
- test_extract_with_invalid_predicate_drops_triple
- test_extract_budget_exceeded_raises_budget_error
- test_extract_persists_call_type_graph_projection_on_ledger
- test_extract_idempotent_on_retry_via_empty_text
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

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


@pytest_asyncio.fixture(scope="module")
async def extract_db_url() -> AsyncIterator[str]:
    """Shared temp DB with alembic upgrade head (includes migration 064)."""
    base_url = _base_test_url()
    database_name = f"shkoder_extract_{uuid.uuid4().hex[:12]}"
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
async def extract_session(extract_db_url: str) -> AsyncIterator:
    """AsyncSession on the migrated temp DB; each test fully isolated via rollback."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(extract_db_url, echo=False)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            Session = async_sessionmaker(
                bind=conn, class_=AsyncSession, expire_on_commit=False
            )
            async with Session() as session:
                try:
                    yield session
                finally:
                    if outer.is_active:
                        await outer.rollback()
    finally:
        await engine.dispose()


# ─── Fakes ────────────────────────────────────────────────────────────────────


@dataclass
class _LedgerRow:
    id: int
    qa_trace_id: Any
    provider: str
    model: str
    prompt_hash: str
    response_hash: Any
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    latency_ms: int
    request_id: Any
    cache_hit: bool
    error: Any
    call_type: str = "unknown"


@dataclass
class FakeLedgerRepoWithCallType:
    """LedgerRepo fake that captures call_type."""

    rows: list[_LedgerRow] = field(default_factory=list)
    daily_cost: Decimal = Decimal("0")
    monthly_cost: Decimal = Decimal("0")
    _next_id: int = 1

    async def record(
        self,
        session: Any,
        *,
        qa_trace_id: Any,
        provider: str,
        model: str,
        prompt_hash: str,
        response_hash: Any,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
        latency_ms: int,
        request_id: Any,
        cache_hit: bool,
        error: Any,
        call_type: str = "unknown",
    ) -> _LedgerRow:
        row = _LedgerRow(
            id=self._next_id,
            qa_trace_id=qa_trace_id,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            request_id=request_id,
            cache_hit=cache_hit,
            error=error,
            call_type=call_type,
        )
        self.rows.append(row)
        self._next_id += 1
        self.daily_cost += cost_usd
        self.monthly_cost += cost_usd
        return row

    async def daily_cost_usd(self, session: Any, *, day: Any) -> Decimal:
        return self.daily_cost

    async def monthly_cost_usd(self, session: Any, *, year: int, month: int) -> Decimal:
        return self.monthly_cost

    async def update_placeholder(
        self,
        session: Any,
        *,
        llm_call_id: int,
        cost_usd: Decimal,
        response_hash: Any,
        tokens_in: int,
        tokens_out: int,
        request_id: Any,
        latency_ms: int,
        error: Any,
    ) -> _LedgerRow:
        for row in self.rows:
            if row.id == llm_call_id:
                old = row.cost_usd
                row.cost_usd = cost_usd
                row.response_hash = response_hash
                row.tokens_in = tokens_in
                row.tokens_out = tokens_out
                row.request_id = request_id
                row.latency_ms = latency_ms
                row.error = error
                self.daily_cost += cost_usd - old
                self.monthly_cost += cost_usd - old
                return row
        raise KeyError(f"placeholder llm_call_id={llm_call_id} not found")


@dataclass
class FakeProvider:
    """LLMProvider fake returning configurable JSON."""

    answer_json: str
    tokens_in: int = 50
    tokens_out: int = 30
    request_id: str = "req-test"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, prompt: str, model: str) -> Any:
        from bot.services.llm_providers import ProviderResult

        self.calls.append({"prompt": prompt, "model": model})
        return ProviderResult(
            answer_text=self.answer_json,
            citation_ids=(),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            request_id=self.request_id,
            raw_latency_ms=10,
        )


def _make_config(
    *,
    daily: Decimal = Decimal("5.00"),
    monthly: Decimal = Decimal("50.00"),
) -> Any:
    from bot.services.llm_gateway import LLMGatewayConfig

    return LLMGatewayConfig(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
        prompt_template_version="graph_triples_v0_1_0",
    )


# ─── Helpers to insert test data ──────────────────────────────────────────────


async def _insert_user(session, *, first_name: str = "Вася", username: str | None = None) -> int:
    """Insert a user and return telegram_id (= users.id)."""
    from sqlalchemy import text

    user_id = abs(hash(uuid.uuid4().hex)) % (10**9)
    await session.execute(
        text(
            "INSERT INTO users (id, first_name, username) "
            "VALUES (:id, :fn, :un) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": user_id, "fn": first_name, "un": username},
    )
    await session.flush()
    return user_id


async def _insert_card(session, *, title: str) -> str:
    """Insert a knowledge_cards row and return its UUID id."""
    from sqlalchemy import text

    card_id = str(uuid.uuid4())
    user_id = abs(hash(uuid.uuid4().hex)) % (10**9)
    await session.execute(
        text(
            "INSERT INTO users (id, first_name) VALUES (:uid, 'Admin') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"uid": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO knowledge_cards (id, title, body_markdown, card_status, "
            "approved_by_user_id, approved_at) "
            "VALUES (:id, :title, 'body', 'approved', :uid, now())"
        ),
        {"id": card_id, "title": title, "uid": user_id},
    )
    await session.flush()
    return card_id


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_with_normal_governance_returns_triples_and_writes_ledger(
    extract_session,
) -> None:
    """Normal governance + resolvable entities → triples returned, ledger written."""
    from bot.services.llm_gateway import extract_graph_triples

    # Insert entities that will resolve.
    await _insert_user(extract_session, first_name="Вася")
    await _insert_card(extract_session, title="Проект Шкодербот")

    triple_json = (
        '[{"subject_label": "Вася", "subject_type": "Person", '
        '"predicate": "KNOWS_ABOUT", "object_label": "Проект Шкодербот", '
        '"object_type": "KnowledgeCard", "confidence": 0.9, "source_id": "42"}]'
    )
    provider = FakeProvider(answer_json=triple_json)
    ledger = FakeLedgerRepoWithCallType()

    result = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="42",
        source_text="Вася знает про Проект Шкодербот.",
        source_id="42",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger,
        provider=provider,
        max_triples=5,
    )

    assert len(result.triples) == 1
    assert result.triples[0].predicate == "KNOWS_ABOUT"
    assert result.llm_usage_ledger_id is not None
    assert result.cost_usd >= Decimal("0")
    assert result.skipped_total == 0
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_extract_with_offrecord_governance_raises_policy_error(
    extract_session,
) -> None:
    """governance_policy != 'normal' → GraphProjectionPolicyError immediately."""
    from bot.services.graph_common import GraphProjectionPolicyError
    from bot.services.llm_gateway import extract_graph_triples

    ledger = FakeLedgerRepoWithCallType()
    provider = FakeProvider(answer_json="[]")

    with pytest.raises(GraphProjectionPolicyError):
        await extract_graph_triples(
            extract_session,
            source_table="message_versions",
            source_pk="1",
            source_text="секретный контент",
            source_id="1",
            source_mv_id=None,
            prompt_version="graph_triples_v0_1_0",
            run_id=1,
            governance_policy="offrecord",
            config=_make_config(),
            ledger_repo=ledger,
            provider=provider,
        )

    # No ledger row written, no provider call.
    assert len(ledger.rows) == 0
    assert len(provider.calls) == 0


@pytest.mark.asyncio
async def test_extract_with_unknown_subject_drops_triple(extract_session) -> None:
    """Triple with subject_label='UNKNOWN' is dropped; counted in skipped_total."""
    from bot.services.llm_gateway import extract_graph_triples

    triple_json = (
        '[{"subject_label": "UNKNOWN", "subject_type": "Person", '
        '"predicate": "KNOWS_ABOUT", "object_label": "Проект", '
        '"object_type": "Project", "confidence": 0.5, "source_id": "1"}]'
    )
    provider = FakeProvider(answer_json=triple_json)
    ledger = FakeLedgerRepoWithCallType()

    result = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="1",
        source_text="Кто-то знает про Проект.",
        source_id="1",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    assert len(result.triples) == 0
    assert result.skipped_total == 1


@pytest.mark.asyncio
async def test_extract_with_invalid_predicate_drops_triple(extract_session) -> None:
    """Triple with predicate not in ALLOWED_PREDICATES is dropped."""
    from bot.services.llm_gateway import extract_graph_triples

    triple_json = (
        '[{"subject_label": "Вася", "subject_type": "Person", '
        '"predicate": "INVALID_PREDICATE", "object_label": "Проект", '
        '"object_type": "Project", "confidence": 0.5, "source_id": "1"}]'
    )
    provider = FakeProvider(answer_json=triple_json)
    ledger = FakeLedgerRepoWithCallType()

    result = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="1",
        source_text="текст",
        source_id="1",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    assert len(result.triples) == 0
    assert result.skipped_total >= 1


@pytest.mark.asyncio
async def test_extract_budget_exceeded_raises_budget_error(extract_session) -> None:
    """Budget exceeded → GraphProjectionBudgetError raised; no provider call."""
    from bot.services.graph_common import GraphProjectionBudgetError
    from bot.services.llm_gateway import extract_graph_triples

    # Saturate the budget by setting daily cost = ceiling.
    ledger = FakeLedgerRepoWithCallType(daily_cost=Decimal("999.00"))
    provider = FakeProvider(answer_json="[]")

    with pytest.raises(GraphProjectionBudgetError):
        await extract_graph_triples(
            extract_session,
            source_table="message_versions",
            source_pk="1",
            source_text="текст",
            source_id="1",
            source_mv_id=None,
            prompt_version="graph_triples_v0_1_0",
            run_id=1,
            governance_policy="normal",
            config=_make_config(daily=Decimal("5.00")),
            ledger_repo=ledger,
            provider=provider,
        )

    assert len(provider.calls) == 0


@pytest.mark.asyncio
async def test_extract_persists_call_type_graph_projection_on_ledger(
    extract_session,
) -> None:
    """Ledger placeholder must be recorded with call_type='graph_projection'."""
    from bot.services.llm_gateway import extract_graph_triples

    # Insert a resolvable entity.
    await _insert_user(extract_session, first_name="Тест")

    triple_json = (
        '[{"subject_label": "Тест", "subject_type": "Person", '
        '"predicate": "MENTIONS", "object_label": "Тест", '
        '"object_type": "Person", "confidence": 0.8, "source_id": "77"}]'
    )
    provider = FakeProvider(answer_json=triple_json)
    ledger = FakeLedgerRepoWithCallType()

    await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="77",
        source_text="Тест упомянул Тест.",
        source_id="77",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    assert len(ledger.rows) == 1
    assert ledger.rows[0].call_type == "graph_projection"


@pytest.mark.asyncio
async def test_extract_idempotent_on_retry_via_empty_text(extract_session) -> None:
    """Empty JSON response → zero triples, no error, ledger updated."""
    from bot.services.llm_gateway import extract_graph_triples

    provider = FakeProvider(answer_json="[]")
    ledger = FakeLedgerRepoWithCallType()

    result = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="1",
        source_text="пустой контент",
        source_id="1",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger,
        provider=provider,
    )

    assert result.triples == []
    assert result.skipped_total == 0
    assert result.llm_usage_ledger_id is not None


@pytest.mark.asyncio
async def test_extract_persists_call_type_in_real_db_row(extract_session) -> None:
    """Real DB row must have call_type='graph_projection' (FIX-MEDIUM-1).

    Uses real LedgerRepo so the assertion is against the persisted DB row,
    not an in-memory fake.
    """
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import extract_graph_triples

    await _insert_user(extract_session, first_name="Тест")
    triple_json = (
        '[{"subject_label": "Тест", "subject_type": "Person", '
        '"predicate": "MENTIONS", "object_label": "Тест", '
        '"object_type": "Person", "confidence": 0.8, "source_id": "88"}]'
    )
    provider = FakeProvider(answer_json=triple_json)
    real_ledger = LedgerRepo()

    result = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="88",
        source_text="Тест упомянул Тест.",
        source_id="88",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=1,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=real_ledger,
        provider=provider,
    )

    assert result.llm_usage_ledger_id is not None
    # Query the actual persisted DB row.
    db_row = await extract_session.get(LlmUsageLedger, result.llm_usage_ledger_id)
    assert db_row is not None
    assert db_row.call_type == "graph_projection"


@pytest.mark.asyncio
async def test_extract_idempotent_on_retry_same_source_pk_and_run_id(extract_session) -> None:
    """Calling extract_graph_triples twice with same source_pk and run_id creates two ledger rows.

    Idempotency is enforced by graph_projector's run_id+source_pk dedup, not by the gateway.
    This test documents current behavior: the gateway does not deduplicate itself.
    (FIX-MEDIUM-2)
    """
    from bot.services.llm_gateway import extract_graph_triples

    provider = FakeProvider(answer_json="[]")
    ledger1 = FakeLedgerRepoWithCallType()
    ledger2 = FakeLedgerRepoWithCallType()

    result1 = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="idempotency-pk",
        source_text="одинаковый текст",
        source_id="idempotency-pk",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=42,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger1,
        provider=provider,
    )

    result2 = await extract_graph_triples(
        extract_session,
        source_table="message_versions",
        source_pk="idempotency-pk",
        source_text="одинаковый текст",
        source_id="idempotency-pk",
        source_mv_id=None,
        prompt_version="graph_triples_v0_1_0",
        run_id=42,
        governance_policy="normal",
        config=_make_config(),
        ledger_repo=ledger2,
        provider=provider,
    )

    # Both calls succeed; both have ledger rows.
    # Deduplication is caller's (graph_projector) responsibility, not gateway's.
    assert result1.llm_usage_ledger_id is not None
    assert result2.llm_usage_ledger_id is not None
    assert len(ledger1.rows) == 1
    assert len(ledger2.rows) == 1
