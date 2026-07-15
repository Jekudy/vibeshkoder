from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from bot.db.models import MessageMedia
from bot.db.repos.message import MessageRepo
from bot.db.repos.user import UserRepo


async def _chat_message(db_session, *, message_id: int = 77):
    user = await UserRepo.upsert(
        db_session,
        telegram_id=707,
        username="member",
        first_name="Member",
        last_name=None,
    )
    return await MessageRepo.save(
        db_session,
        message_id=message_id,
        chat_id=-1001234567890,
        user_id=user.id,
        text=None,
        date=datetime.now(timezone.utc),
        caption="caption",
        message_kind="photo",
        memory_policy="normal",
        is_redacted=False,
    )


def _photo_message(*, message_id: int = 77):
    return SimpleNamespace(
        message_id=message_id,
        caption="caption",
        chat=SimpleNamespace(id=-1001234567890, username=None),
        photo=[
            SimpleNamespace(
                file_id="small",
                file_unique_id="unique-small",
                width=90,
                height=90,
                file_size=1_000,
            ),
            SimpleNamespace(
                file_id="large",
                file_unique_id="unique-large",
                width=1280,
                height=960,
                file_size=90_000,
            ),
        ],
        voice=None,
    )


async def test_enqueue_photo_memory_persists_safe_source_and_is_idempotent(db_session):
    from bot.services.image_memory import enqueue_photo_memory

    stored_message = await _chat_message(db_session)
    message = _photo_message()

    first = await enqueue_photo_memory(
        db_session,
        message=message,
        chat_message_id=stored_message.id,
    )
    second = await enqueue_photo_memory(
        db_session,
        message=message,
        chat_message_id=stored_message.id,
    )

    assert first.id == second.id
    assert first.telegram_file_id == "large"
    assert first.telegram_file_unique_id == "unique-large"
    assert first.source_message_url == "https://t.me/c/1234567890/77"
    assert first.description_status == "pending"
    rows = (await db_session.execute(select(MessageMedia))).scalars().all()
    assert len(rows) == 1


async def test_enqueue_photo_memory_ignores_voice_without_photo(db_session):
    from bot.services.image_memory import enqueue_photo_memory

    stored_message = await _chat_message(db_session, message_id=78)
    message = SimpleNamespace(
        message_id=78,
        caption=None,
        chat=SimpleNamespace(id=-1001234567890, username=None),
        photo=None,
        voice=SimpleNamespace(file_id="voice"),
    )

    assert (
        await enqueue_photo_memory(
            db_session,
            message=message,
            chat_message_id=stored_message.id,
        )
        is None
    )


async def test_photo_worker_downloads_and_records_gateway_description(db_session):
    from bot.db.models import LlmUsageLedger
    from bot.services.image_memory import process_next_pending_photo
    from bot.services.llm_gateway import ImageDescriptionResult

    stored_message = await _chat_message(db_session, message_id=79)
    from bot.services.image_memory import enqueue_photo_memory

    media = await enqueue_photo_memory(
        db_session,
        message=_photo_message(message_id=79),
        chat_message_id=stored_message.id,
    )
    ledger = LlmUsageLedger(
        provider="openai",
        model="gpt-5-nano",
        prompt_hash="a" * 64,
        response_hash="b" * 64,
        tokens_in=10,
        tokens_out=5,
        cost_usd=Decimal("0.000100"),
        latency_ms=1,
        cache_hit=False,
        call_type="image_description",
    )
    db_session.add(ledger)
    await db_session.flush()

    class FakeBot:
        async def get_file(self, file_id: str):
            assert file_id == "large"
            return SimpleNamespace(file_path="photos/file_79.jpg")

        async def download_file(self, file_path: str):
            assert file_path == "photos/file_79.jpg"
            return BytesIO(b"jpeg-data")

    calls = []

    async def fake_describe(session, **kwargs):
        calls.append(kwargs)
        return ImageDescriptionResult(
            description="Люди обсуждают схему у доски.",
            model="gpt-5-nano",
            cost_usd=Decimal("0.000100"),
            llm_usage_ledger_id=ledger.id,
        )

    result = await process_next_pending_photo(
        db_session,
        bot=FakeBot(),
        describe_image_fn=fake_describe,
    )

    assert result is not None
    assert result.id == media.id
    assert result.description_status == "ready"
    assert result.description == "Люди обсуждают схему у доски."
    assert result.description_model == "gpt-5-nano"
    assert result.llm_usage_ledger_id == ledger.id
    assert calls[0]["image_bytes"] == b"jpeg-data"
    assert calls[0]["mime_type"] == "image/jpeg"
    assert calls[0]["caption"] == "caption"


@pytest.mark.parametrize(
    ("chat_id", "username", "expected"),
    [
        (-1001234567890, None, "https://t.me/c/1234567890/55"),
        (-1001234567890, "vibe_chat", "https://t.me/vibe_chat/55"),
    ],
)
def test_telegram_source_message_url(chat_id, username, expected):
    from bot.services.image_memory import telegram_source_message_url

    assert telegram_source_message_url(chat_id, 55, username=username) == expected


async def test_rate_limit_vision_failure_is_deferred_for_bounded_retry(db_session):
    from bot.services.image_memory import enqueue_photo_memory, process_next_pending_photo
    from bot.services.llm_providers import ProviderTransientError

    stored_message = await _chat_message(db_session, message_id=80)
    media = await enqueue_photo_memory(
        db_session,
        message=_photo_message(message_id=80),
        chat_message_id=stored_message.id,
    )

    class FakeBot:
        async def get_file(self, file_id: str):
            return SimpleNamespace(file_path="photos/file_80.jpg")

        async def download_file(self, file_path: str):
            return b"jpeg-data"

    async def transient(*args, **kwargs):
        raise ProviderTransientError("rate_limit", message="temporary")

    result = await process_next_pending_photo(
        db_session,
        bot=FakeBot(),
        describe_image_fn=transient,
    )

    assert result.id == media.id
    assert result.description_status == "pending"
    assert result.description_attempts == 1
    assert result.next_attempt_at is not None
    assert result.next_attempt_at > datetime.now(timezone.utc)
    assert result.last_error_code == "provider_rate_limit"


async def test_third_rate_limit_vision_failure_becomes_terminal(db_session):
    from bot.services.image_memory import enqueue_photo_memory, process_next_pending_photo
    from bot.services.llm_providers import ProviderTransientError

    stored_message = await _chat_message(db_session, message_id=81)
    media = await enqueue_photo_memory(
        db_session,
        message=_photo_message(message_id=81),
        chat_message_id=stored_message.id,
    )
    media.description_attempts = 2
    await db_session.flush()

    class FakeBot:
        async def get_file(self, file_id: str):
            return SimpleNamespace(file_path="photos/file_81.jpg")

        async def download_file(self, file_path: str):
            return b"jpeg-data"

    async def transient(*args, **kwargs):
        raise ProviderTransientError("rate_limit", message="temporary")

    result = await process_next_pending_photo(
        db_session,
        bot=FakeBot(),
        describe_image_fn=transient,
    )

    assert result.description_status == "failed"
    assert result.description_attempts == 3
    assert result.next_attempt_at is None
    assert result.last_error_code == "provider_rate_limit"


async def test_budget_refusal_is_deferred_without_burning_attempt(db_session):
    from bot.services.image_memory import enqueue_photo_memory, process_next_pending_photo
    from bot.services.llm_gateway import ImageDescriptionBudgetExceeded

    stored_message = await _chat_message(db_session, message_id=82)
    media = await enqueue_photo_memory(
        db_session,
        message=_photo_message(message_id=82),
        chat_message_id=stored_message.id,
    )

    class FakeBot:
        async def get_file(self, file_id: str):
            return SimpleNamespace(file_path="photos/file_82.jpg")

        async def download_file(self, file_path: str):
            return b"jpeg-data"

    async def over_budget(*args, **kwargs):
        raise ImageDescriptionBudgetExceeded("daily ceiling")

    result = await process_next_pending_photo(
        db_session,
        bot=FakeBot(),
        describe_image_fn=over_budget,
    )

    assert result.id == media.id
    assert result.description_status == "pending"
    assert result.description_attempts == 0
    assert result.next_attempt_at is not None
    assert result.last_error_code == "budget_exceeded"
