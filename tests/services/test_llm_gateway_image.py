from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def _session_factory(db_session):
    return async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def test_describe_image_records_costed_image_ledger_row(db_session):
    from bot.db.models import LlmUsageLedger
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import VisionGatewayConfig, describe_image
    from bot.services.llm_providers.openai_vision import VisionDescriptionResult

    class FakeProvider:
        async def describe(self, **kwargs):
            assert kwargs["image_bytes"] == b"jpeg"
            assert kwargs["mime_type"] == "image/jpeg"
            assert kwargs["model"] == "gpt-5-nano"
            return VisionDescriptionResult(
                description="Описание",
                tokens_in=100,
                tokens_out=20,
                request_id="vision-1",
                raw_latency_ms=12,
            )

    result = await describe_image(
        db_session,
        image_bytes=b"jpeg",
        mime_type="image/jpeg",
        caption=None,
        config=VisionGatewayConfig(
            model="gpt-5-nano",
            daily_ceiling_usd=Decimal("1"),
            monthly_ceiling_usd=Decimal("10"),
        ),
        ledger_repo=LedgerRepo(),
        provider=FakeProvider(),
        ledger_session_factory=_session_factory(db_session),
    )

    assert result.description == "Описание"
    assert result.model == "gpt-5-nano"
    assert result.cost_usd == Decimal("0.000013")
    row = await db_session.get(LlmUsageLedger, result.llm_usage_ledger_id)
    assert row.call_type == "image_description"
    assert row.provider == "openai"
    assert row.model == "gpt-5-nano"


async def test_describe_image_rejects_unpriced_model_before_provider(db_session):
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.llm_gateway import VisionGatewayConfig, describe_image

    class ForbiddenProvider:
        async def describe(self, **kwargs):
            raise AssertionError("provider must not be called for an unpriced model")

    with pytest.raises(ValueError, match="unsupported image description model"):
        await describe_image(
            db_session,
            image_bytes=b"jpeg",
            mime_type="image/jpeg",
            caption=None,
            config=VisionGatewayConfig(
                model="unpriced-vision-model",
                daily_ceiling_usd=Decimal("1"),
                monthly_ceiling_usd=Decimal("10"),
            ),
            ledger_repo=LedgerRepo(),
            provider=ForbiddenProvider(),
        )


async def test_describe_image_serializes_budget_check_and_provider(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot.services.llm_gateway import VisionGatewayConfig, describe_image
    from bot.services.llm_providers.openai_vision import VisionDescriptionResult

    events: list[str] = []
    session = AsyncMock()

    async def execute(*args, **kwargs):
        events.append("lock")

    session.execute.side_effect = execute

    @asynccontextmanager
    async def transaction():
        yield

    session.begin = transaction

    @asynccontextmanager
    async def session_factory():
        yield session

    class Ledger:
        async def daily_cost_usd(self, *args, **kwargs):
            events.append("daily")
            return Decimal("0")

        async def monthly_cost_usd(self, *args, **kwargs):
            events.append("monthly")
            return Decimal("0")

        async def record(self, *args, **kwargs):
            events.append("ledger")
            return SimpleNamespace(id=44)

        async def update_placeholder(self, *args, **kwargs):
            events.append("terminal")
            return 1

    class Provider:
        async def describe(self, **kwargs):
            events.append("provider")
            return VisionDescriptionResult(
                description="Описание",
                tokens_in=1,
                tokens_out=1,
                request_id="vision-lock",
                raw_latency_ms=1,
            )

    await describe_image(
        session,
        image_bytes=b"jpeg",
        mime_type="image/jpeg",
        caption=None,
        config=VisionGatewayConfig(
            model="gpt-5-nano",
            daily_ceiling_usd=Decimal("1"),
            monthly_ceiling_usd=Decimal("10"),
        ),
        ledger_repo=Ledger(),
        provider=Provider(),
        ledger_session_factory=session_factory,
    )

    assert events == ["lock", "daily", "monthly", "ledger", "provider", "terminal"]
