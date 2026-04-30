"""Phase 4 FTS search service tests.

DB-backed tests use the shared ``db_session`` fixture. The CI job runs
``alembic upgrade head`` before pytest, so the generated ``search_tsv`` column
is present for these tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=10_400_000_000)
_message_counter = itertools.count(start=1_040_000)
_hash_counter = itertools.count(start=1)


@dataclass(frozen=True)
class CreatedMessage:
    chat_message_id: int
    version_id: int
    message_id: int
    chat_id: int
    user_id: int
    content_hash: str


async def _create_versioned_message(
    db_session,
    *,
    chat_id: int = -100_400,
    message_id: int | None = None,
    text: str | None = "питон любит память",
    caption: str | None = None,
    memory_policy: str = "normal",
    chat_is_redacted: bool = False,
    version_is_redacted: bool = False,
) -> CreatedMessage:
    """Create user + current message version."""
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    user_id = next(_user_counter)
    tg_message_id = message_id if message_id is not None else next(_message_counter)
    content_hash = f"search-hash-{next(_hash_counter)}"

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"u{user_id}",
        first_name="Search",
        last_name=None,
    )

    chat_message = ChatMessage(
        message_id=tg_message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        caption=caption,
        date=datetime.now(timezone.utc),
        memory_policy=memory_policy,
        is_redacted=chat_is_redacted,
        content_hash=content_hash,
    )
    db_session.add(chat_message)
    await db_session.flush()

    version = MessageVersion(
        chat_message_id=chat_message.id,
        version_seq=1,
        text=text,
        caption=caption,
        normalized_text=text,
        content_hash=content_hash,
        is_redacted=version_is_redacted,
    )
    db_session.add(version)
    await db_session.flush()

    chat_message.current_version_id = version.id
    await db_session.flush()

    return CreatedMessage(
        chat_message_id=chat_message.id,
        version_id=version.id,
        message_id=tg_message_id,
        chat_id=chat_id,
        user_id=user_id,
        content_hash=content_hash,
    )


async def _create_forget_event(
    db_session,
    *,
    tombstone_key: str,
    target_type: str = "message",
    target_id: str | None = None,
    status: str,
) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    event = await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    if status == "pending":
        return

    await ForgetEventRepo.mark_status(db_session, event.id, status="processing")
    if status == "completed":
        await ForgetEventRepo.mark_status(db_session, event.id, status="completed")


async def test_search_normal_message_found(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_401
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="питон помогает искать память",
    )

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert len(hits) == 1
    assert hits[0].message_version_id == created.version_id
    assert hits[0].chat_message_id == created.chat_message_id
    assert hits[0].chat_id == chat_id
    assert hits[0].message_id == created.message_id
    assert hits[0].user_id == created.user_id
    assert hits[0].ts_rank > 0
    assert hits[0].captured_at is not None
    assert hits[0].message_date is not None


async def test_search_offrecord_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_402
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="секретный питон",
        memory_policy="offrecord",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_nomem_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_403
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="nomem питон",
        memory_policy="nomem",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_forgotten_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_404
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="забытый питон",
        memory_policy="forgotten",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_redacted_chat_message_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_405
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="редактированный питон",
        chat_is_redacted=True,
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_redacted_version_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_406
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="версия питон",
        version_is_redacted=True,
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_active_forget_event_pending_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_407
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="pending питон",
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"message:{created.chat_id}:{created.message_id}",
        target_id=str(created.chat_message_id),
        status="pending",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_active_forget_event_completed_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_408
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="completed питон",
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"message:{created.chat_id}:{created.message_id}",
        target_id=str(created.chat_message_id),
        status="completed",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_active_user_forget_event_pending_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_416
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="user tombstone питон",
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"user:{created.user_id}",
        target_type="user",
        target_id=str(created.user_id),
        status="pending",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_active_message_hash_forget_event_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_417
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="hash tombstone питон",
    )
    await _create_forget_event(
        db_session,
        tombstone_key=f"message_hash:{created.content_hash}",
        target_type="message_hash",
        status="pending",
    )

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_empty_message_hash_tombstone_does_not_match_null_hash(
    db_session,
) -> None:
    from bot.db.models import ChatMessage
    from bot.services.search import search_messages

    chat_id = -100_421
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="null hash питон",
    )
    chat_message = await db_session.get(ChatMessage, created.chat_message_id)
    assert chat_message is not None
    chat_message.content_hash = None
    await _create_forget_event(
        db_session,
        tombstone_key="message_hash:",
        target_type="message_hash",
        status="pending",
    )
    await db_session.flush()

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert [hit.message_version_id for hit in hits] == [created.version_id]


async def test_search_chat_isolation(db_session) -> None:
    from bot.services.search import search_messages

    chat_a = -100_409
    chat_b = -100_410
    await _create_versioned_message(db_session, chat_id=chat_a, text="изолированный питон")

    assert await search_messages(db_session, "питон", chat_id=chat_b) == []


async def test_search_only_current_version(db_session) -> None:
    from bot.db.models import MessageVersion
    from bot.services.search import search_messages

    chat_id = -100_418
    created = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="текущая память",
    )
    db_session.add(
        MessageVersion(
            chat_message_id=created.chat_message_id,
            version_seq=2,
            text="старая некуррентная версия",
            normalized_text="старая некуррентная версия",
            content_hash=f"old-version-{created.content_hash}",
        )
    )
    await db_session.flush()

    assert await search_messages(db_session, "некуррентная", chat_id=chat_id) == []


async def test_search_ts_rank_ordering(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_411
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        message_id=1_041_101,
        text="питон память",
    )
    high_rank = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        message_id=1_041_102,
        text="питон питон питон память",
    )

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert [hit.message_id for hit in hits][:2] == [high_rank.message_id, 1_041_101]
    assert hits[0].ts_rank > hits[1].ts_rank


async def test_search_russian_stemmer_matches_inflection(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_419
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="лошадь скачет быстро",
    )

    hits = await search_messages(db_session, "лошади", chat_id=chat_id)

    assert len(hits) == 1


async def test_search_snippet_contains_query_terms(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_412
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="в этой строке питон находится внутри",
    )

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert "питон" in hits[0].snippet.lower()


async def test_search_caption_hit_has_snippet(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_413
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text=None,
        caption="подпись содержит питон",
    )

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert len(hits) == 1
    assert "питон" in hits[0].snippet.lower()


async def test_search_limit_respected(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_414
    for _ in range(3):
        await _create_versioned_message(db_session, chat_id=chat_id, text="лимит питон")

    hits = await search_messages(db_session, "питон", chat_id=chat_id, limit=2)

    assert len(hits) == 2


async def test_search_empty_query_returns_empty(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_415
    await _create_versioned_message(db_session, chat_id=chat_id, text="питон")

    assert await search_messages(db_session, "   ", chat_id=chat_id) == []


async def test_search_query_injection_attempt_is_safe(db_session) -> None:
    from sqlalchemy import text

    from bot.services.search import search_messages

    chat_id = -100_420
    await _create_versioned_message(db_session, chat_id=chat_id, text="питон")

    hits = await search_messages(
        db_session,
        "питон'; DROP TABLE chat_messages; --",
        chat_id=chat_id,
    )
    table_exists = await db_session.execute(
        text("SELECT to_regclass('public.chat_messages') IS NOT NULL")
    )

    assert isinstance(hits, list)
    assert table_exists.scalar_one() is True
