from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import ChatMessage, LlmUsageLedger, MessageMedia, User
from bot.db.repos.llm_usage_ledger import LedgerRepo
from bot.db.repos.message import MessageRepo
from bot.db.repos.user import UserRepo
from bot.services.image_memory import enqueue_photo_memory, process_next_pending_photo
from bot.services.llm_gateway import VisionGatewayConfig, describe_image
from bot.services.llm_providers import ProviderTransientError
from bot.services.llm_providers.openai_vision import VisionDescriptionResult


pytestmark = pytest.mark.usefixtures("app_env")

_CONCURRENCY_TIMEOUT_SECONDS = 30


class _Bot:
    async def get_file(self, file_id: str):
        return SimpleNamespace(file_path=f"photos/{file_id}.jpg")

    async def download_file(self, file_path: str):
        return f"jpeg-durable-sentinel:{file_path}".encode()


def _photo(message_id: int):
    return SimpleNamespace(
        message_id=message_id,
        caption="caption-durable-sentinel",
        chat=SimpleNamespace(id=-1001234567890, username=None),
        photo=[
            SimpleNamespace(
                file_id=f"durable-{message_id}",
                file_unique_id=f"durable-unique-{message_id}",
                width=100,
                height=100,
                file_size=100,
            )
        ],
    )


async def _seed(factory) -> tuple[int, int, int]:
    suffix = int(uuid.uuid4().hex[:10], 16)
    telegram_id = 800_000_000_000 + suffix
    message_id = 1_000_000_000 + suffix
    async with factory() as session:
        user = await UserRepo.upsert(
            session,
            telegram_id=telegram_id,
            username=f"vision_{suffix}",
            first_name="Vision",
            last_name=None,
        )
        message = await MessageRepo.save(
            session,
            message_id=message_id,
            chat_id=-1001234567890,
            user_id=user.id,
            text=None,
            date=datetime.now(timezone.utc),
            caption="caption-durable-sentinel",
            message_kind="photo",
            memory_policy="normal",
            is_redacted=False,
        )
        media = await enqueue_photo_memory(
            session,
            message=_photo(message_id),
            chat_message_id=message.id,
        )
        await session.commit()
        assert media is not None
        return user.id, message.id, media.id


async def _cleanup(factory, *, user_id: int, message_id: int) -> None:
    async with factory() as session:
        prompt_hashes = (
            (
                await session.execute(
                    select(LlmUsageLedger.prompt_hash)
                    .join(
                        MessageMedia,
                        MessageMedia.llm_usage_ledger_id == LlmUsageLedger.id,
                    )
                    .where(
                        MessageMedia.chat_message_id == message_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.execute(delete(ChatMessage).where(ChatMessage.id == message_id))
        if prompt_hashes:
            await session.execute(
                delete(LlmUsageLedger).where(LlmUsageLedger.prompt_hash.in_(prompt_hashes))
            )
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


def _gateway(provider):
    async def call(session, **kwargs):
        return await describe_image(
            session,
            config=VisionGatewayConfig(
                model="gpt-5-nano",
                daily_ceiling_usd=Decimal("1"),
                monthly_ceiling_usd=Decimal("10"),
            ),
            ledger_repo=LedgerRepo(),
            provider=provider,
            **kwargs,
        )

    return call


@pytest_asyncio.fixture()
async def durable_factory(postgres_engine):
    yield async_sessionmaker(
        postgres_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def test_paid_outcome_survives_caller_rollback_and_is_not_dispatched_twice(
    durable_factory,
) -> None:
    class Provider:
        calls = 0

        async def describe(self, **kwargs):
            self.calls += 1
            return VisionDescriptionResult(
                description="Долговечное описание.",
                tokens_in=10,
                tokens_out=4,
                request_id="vision-durable-rollback",
                raw_latency_ms=2,
            )

    provider = Provider()
    user_id, message_id, media_id = await _seed(durable_factory)
    try:
        async with durable_factory() as caller:
            result = await process_next_pending_photo(
                caller,
                bot=_Bot(),
                describe_image_fn=_gateway(provider),
                session_factory=durable_factory,
            )
            await caller.rollback()
        assert result is not None and result.description_status == "ready"

        async with durable_factory() as verify:
            media = await verify.get(MessageMedia, media_id)
            assert media is not None
            assert media.description == "Долговечное описание."
            assert media.llm_usage_ledger_id is not None
            ledger = await verify.get(LlmUsageLedger, media.llm_usage_ledger_id)
            assert ledger is not None and ledger.error is None

        async with durable_factory() as second_caller:
            assert (
                await process_next_pending_photo(
                    second_caller,
                    bot=_Bot(),
                    describe_image_fn=_gateway(provider),
                    session_factory=durable_factory,
                )
                is None
            )
        assert provider.calls == 1
    finally:
        await _cleanup(durable_factory, user_id=user_id, message_id=message_id)


async def test_concurrent_workers_share_one_durable_claim(durable_factory) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    first: asyncio.Task[MessageMedia | None] | None = None

    class Provider:
        calls = 0

        async def describe(self, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return VisionDescriptionResult(
                description="Один вызов.",
                tokens_in=10,
                tokens_out=3,
                request_id="vision-durable-concurrency",
                raw_latency_ms=3,
            )

    provider = Provider()
    user_id, message_id, media_id = await _seed(durable_factory)
    try:
        async with durable_factory() as first_session:
            first = asyncio.create_task(
                process_next_pending_photo(
                    first_session,
                    bot=_Bot(),
                    describe_image_fn=_gateway(provider),
                    session_factory=durable_factory,
                )
            )
            await asyncio.wait_for(
                started.wait(),
                timeout=_CONCURRENCY_TIMEOUT_SECONDS,
            )
            async with durable_factory() as second_session:
                second = await process_next_pending_photo(
                    second_session,
                    bot=_Bot(),
                    describe_image_fn=_gateway(provider),
                    session_factory=durable_factory,
                )
            assert second is None
            release.set()
            result = await asyncio.wait_for(
                first,
                timeout=_CONCURRENCY_TIMEOUT_SECONDS,
            )
            assert result is not None and result.description_status == "ready"
        assert provider.calls == 1
    finally:
        release.set()
        if first is not None:
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await _cleanup(durable_factory, user_id=user_id, message_id=message_id)


async def test_transient_failure_retries_then_resumes_without_losing_ledgers(
    durable_factory,
) -> None:
    class Provider:
        calls = 0

        async def describe(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ProviderTransientError(
                    "rate_limit",
                    message="temporary-secret-sentinel",
                )
            return VisionDescriptionResult(
                description="Описание после повтора.",
                tokens_in=10,
                tokens_out=5,
                request_id="vision-durable-retry",
                raw_latency_ms=4,
            )

    provider = Provider()
    user_id, message_id, media_id = await _seed(durable_factory)
    try:
        async with durable_factory() as first_session:
            first = await process_next_pending_photo(
                first_session,
                bot=_Bot(),
                describe_image_fn=_gateway(provider),
                session_factory=durable_factory,
            )
        assert first is not None and first.description_status == "pending"

        async with durable_factory() as make_due:
            media = await make_due.get(MessageMedia, media_id)
            assert media is not None
            media.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await make_due.commit()

        async with durable_factory() as second_session:
            second = await process_next_pending_photo(
                second_session,
                bot=_Bot(),
                describe_image_fn=_gateway(provider),
                session_factory=durable_factory,
            )
        assert second is not None and second.description_status == "ready"
        assert second.description == "Описание после повтора."
        assert provider.calls == 2

        async with durable_factory() as verify:
            media = await verify.get(MessageMedia, media_id)
            assert media is not None and media.llm_usage_ledger_id is not None
            current_ledger = await verify.get(LlmUsageLedger, media.llm_usage_ledger_id)
            assert current_ledger is not None
            ledgers = (
                (
                    await verify.execute(
                        select(LlmUsageLedger)
                        .where(
                            LlmUsageLedger.call_type == "image_description",
                            LlmUsageLedger.prompt_hash == current_ledger.prompt_hash,
                        )
                        .order_by(LlmUsageLedger.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(ledgers) == 2
            assert any(row.error and row.error.startswith("provider_error:") for row in ledgers)
            assert any(
                row.id == second.llm_usage_ledger_id and row.error is None for row in ledgers
            )
    finally:
        await _cleanup(durable_factory, user_id=user_id, message_id=message_id)


async def test_timeout_preserves_claim_and_reservation_without_automatic_retry(
    durable_factory,
) -> None:
    class Provider:
        calls = 0

        async def describe(self, **kwargs):
            self.calls += 1
            raise ProviderTransientError(
                "timeout",
                message="provider-timeout-secret-sentinel",
            )

    provider = Provider()
    user_id, message_id, media_id = await _seed(durable_factory)
    try:
        async with durable_factory() as caller:
            result = await process_next_pending_photo(
                caller,
                bot=_Bot(),
                describe_image_fn=_gateway(provider),
                session_factory=durable_factory,
            )
        assert result is not None and result.description_status == "processing"
        assert result.description_claim_token is not None
        assert result.description_claimed_at is not None
        assert result.llm_usage_ledger_id is not None

        async with durable_factory() as verify:
            media = await verify.get(MessageMedia, media_id)
            assert media is not None
            assert media.description_status == "processing"
            assert media.last_error_code == "reserved_in_flight"
            assert media.description_claim_token == result.description_claim_token
            ledger = await verify.get(LlmUsageLedger, media.llm_usage_ledger_id)
            assert ledger is not None
            assert ledger.error == "reserved_in_flight"
            assert ledger.cost_usd > 0

        async with durable_factory() as second_tick:
            assert (
                await process_next_pending_photo(
                    second_tick,
                    bot=_Bot(),
                    describe_image_fn=_gateway(provider),
                    session_factory=durable_factory,
                )
                is None
            )
        assert provider.calls == 1
    finally:
        await _cleanup(durable_factory, user_id=user_id, message_id=message_id)


async def test_ambiguous_provider_error_is_fail_closed_and_logs_no_payload(
    durable_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "api_key=vision-secret-sentinel"

    class Provider:
        calls = 0

        async def describe(self, **kwargs):
            self.calls += 1
            raise RuntimeError(f"{secret} caption-durable-sentinel jpeg-durable-sentinel")

    provider = Provider()
    user_id, message_id, media_id = await _seed(durable_factory)
    caplog.set_level(logging.ERROR)
    try:
        async with durable_factory() as caller:
            result = await process_next_pending_photo(
                caller,
                bot=_Bot(),
                describe_image_fn=_gateway(provider),
                session_factory=durable_factory,
            )
        assert result is not None and result.description_status == "processing"
        assert result.last_error_code == "reserved_in_flight"

        async with durable_factory() as retry_session:
            assert (
                await process_next_pending_photo(
                    retry_session,
                    bot=_Bot(),
                    describe_image_fn=_gateway(provider),
                    session_factory=durable_factory,
                )
                is None
            )
        assert provider.calls == 1

        records = [
            record
            for record in caplog.records
            if record.name in {"bot.services.llm_gateway", "bot.services.image_memory"}
        ]
        assert records
        rendered = repr([record.__dict__ for record in records])
        assert secret not in rendered
        assert "caption-durable-sentinel" not in rendered
        assert "jpeg-durable-sentinel" not in rendered
        assert all(record.exc_info is None for record in records)
    finally:
        await _cleanup(durable_factory, user_id=user_id, message_id=message_id)
