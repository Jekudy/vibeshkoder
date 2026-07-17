from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services.llm_gateway import (
    EmbeddingBudgetExceeded,
    EmbeddingGatewayConfig,
    embed_texts,
    load_embedding_gateway_config,
)
from bot.services.llm_providers.openai_embeddings import EmbeddingResult


@dataclass
class _Ledger:
    daily: Decimal = Decimal("0")
    monthly: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []

    async def daily_cost_usd(self, session, *, day, call_type=None):
        assert call_type == "semantic_embedding"
        return self.daily

    async def monthly_cost_usd(self, session, *, year, month, call_type=None):
        assert call_type == "semantic_embedding"
        return self.monthly

    async def record(self, session, **kwargs):
        self.records.append(kwargs)
        return SimpleNamespace(id=len(self.records))

    async def update_placeholder(self, session, **kwargs):
        self.updates.append(kwargs)
        return 1


def _config(
    *,
    daily: Decimal = Decimal("1"),
    monthly: Decimal = Decimal("10"),
) -> EmbeddingGatewayConfig:
    return EmbeddingGatewayConfig(
        model="text-embedding-3-small",
        dimensions=1536,
        daily_ceiling_usd=daily,
        monthly_ceiling_usd=monthly,
    )


async def test_embedding_gateway_commits_reservation_before_provider() -> None:
    session = AsyncMock()
    ledger = _Ledger()
    events: list[str] = []

    async def commit() -> None:
        events.append("commit_reservation")

    session.commit.side_effect = commit

    class Provider:
        async def embed(self, **kwargs):
            events.append("provider")
            assert ledger.records[0]["error"] == "reserved_in_flight"
            assert kwargs == {
                "inputs": ("semantic query",),
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            }
            return EmbeddingResult(
                vectors=((0.0,) * 1536,),
                tokens_in=12,
                request_id="emb-request-1",
                raw_latency_ms=7,
            )

    result = await embed_texts(
        session,
        inputs=["semantic query"],
        config=_config(),
        ledger_repo=ledger,
        provider=Provider(),
    )

    assert events == ["commit_reservation", "provider", "commit_reservation"]
    assert result.llm_usage_ledger_id == 1
    assert result.vectors == ((0.0,) * 1536,)
    assert ledger.records[0]["call_type"] == "semantic_embedding"
    assert ledger.records[0]["prompt_hash"] != "semantic query"
    assert ledger.updates[0]["request_id"] == "emb-request-1"
    assert ledger.updates[0]["response_hash"] is not None
    lock_sql = str(session.execute.await_args_list[0].args[0])
    assert "pg_advisory_xact_lock" in lock_sql


async def test_embedding_recorder_shares_reservation_and_terminal_commits() -> None:
    session = AsyncMock()
    ledger = _Ledger()
    events: list[str] = []

    async def commit() -> None:
        events.append("commit")

    session.commit.side_effect = commit

    class Recorder:
        async def reserve(self, session, *, llm_usage_ledger_id, budget_denied):
            assert llm_usage_ledger_id == 1
            assert budget_denied is False
            events.append("reserve")

        async def fail(self, session, *, llm_usage_ledger_id):
            raise AssertionError("successful embedding must not fail its recorder")

        async def complete(self, session, *, llm_usage_ledger_id, vectors):
            assert llm_usage_ledger_id == 1
            assert len(vectors) == 1
            events.append("complete")

    class Provider:
        async def embed(self, **kwargs):
            events.append("provider")
            return EmbeddingResult(
                vectors=((0.0,) * 1536,),
                tokens_in=1,
                request_id="recorder-order",
                raw_latency_ms=1,
            )

    await embed_texts(
        session,
        inputs=["semantic recorder"],
        config=_config(),
        ledger_repo=ledger,
        provider=Provider(),
        outcome_recorder=Recorder(),
    )

    assert events == ["reserve", "commit", "provider", "complete", "commit"]


async def test_embedding_budget_denial_is_audited_and_skips_provider() -> None:
    session = AsyncMock()
    ledger = _Ledger()
    provider = AsyncMock()

    with pytest.raises(EmbeddingBudgetExceeded) as raised:
        await embed_texts(
            session,
            inputs=["x" * 8_000],
            config=_config(daily=Decimal("0.000100")),
            ledger_repo=ledger,
            provider=provider,
        )

    assert raised.value.llm_usage_ledger_id == 1
    assert ledger.records[0]["error"] == "budget_exceeded"
    assert (
        ledger.records[0]["prompt_hash"]
        == hashlib.sha256(b"embedding_budget_exceeded_without_provider_dispatch").hexdigest()
    )
    session.commit.assert_awaited_once()
    provider.embed.assert_not_awaited()


async def test_embedding_provider_failure_keeps_safe_taxonomy() -> None:
    from bot.services.llm_providers import ProviderTransientError

    session = AsyncMock()
    ledger = _Ledger()

    class Provider:
        async def embed(self, **kwargs):
            raise ProviderTransientError("timeout", message="secret provider payload")

    with pytest.raises(ProviderTransientError) as raised:
        await embed_texts(
            session,
            inputs=["query"],
            config=_config(),
            ledger_repo=ledger,
            provider=Provider(),
        )

    assert raised.value.llm_usage_ledger_id == 1
    assert ledger.updates[0]["error"] == "provider_ProviderTransientError:timeout"
    assert "secret provider payload" not in str(ledger.updates[0])
    assert session.commit.await_count == 2


@pytest.mark.parametrize(
    ("subtype", "expected_zero_cost"),
    [("auth", True), ("rate_limit", True), ("timeout", False), ("5xx", False)],
)
async def test_embedding_failure_cost_and_latency_match_provider_taxonomy(
    monkeypatch,
    subtype: str,
    expected_zero_cost: bool,
) -> None:
    from bot.services.llm_providers import (
        ProviderStructuralError,
        ProviderTransientError,
    )

    session = AsyncMock()
    ledger = _Ledger()
    error_type = ProviderStructuralError if subtype == "auth" else ProviderTransientError

    class Provider:
        async def embed(self, **kwargs):
            raise error_type(subtype, message="redacted")

    clock = iter((10.0, 10.125))
    monkeypatch.setitem(embed_texts.__globals__, "_monotonic", lambda: next(clock))
    with pytest.raises(error_type):
        await embed_texts(
            session,
            inputs=["query"],
            config=_config(),
            ledger_repo=ledger,
            provider=Provider(),
        )

    assert ledger.updates[0]["latency_ms"] == 125
    if expected_zero_cost:
        assert ledger.updates[0]["cost_usd"] == Decimal("0")
    else:
        assert ledger.updates[0]["cost_usd"] == ledger.records[0]["cost_usd"]


def test_embedding_config_is_fixed_and_fails_fast(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", "different-model")
    with pytest.raises(ValueError, match="text-embedding-3-small"):
        load_embedding_gateway_config()

    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3072")
    with pytest.raises(ValueError, match="1536"):
        load_embedding_gateway_config()


def test_embedding_config_rejects_non_positive_cost_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_DAILY_USD_CEILING", "0")
    with pytest.raises(ValueError, match="positive"):
        load_embedding_gateway_config()
