"""Phase 4 FTS search service tests.

DB-backed tests use the shared ``db_session`` fixture. The CI job runs
``alembic upgrade head`` before pytest, so migration 020's generated ``tsv`` column
is present for these tests.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=10_400_000_000)
_message_counter = itertools.count(start=1_040_000)
_hash_counter = itertools.count(start=1)


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
) -> tuple[int, int, int]:
    """Create user + chat_messages + message_versions. Return (chat_pk, version_pk, msg_id)."""
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    user_id = next(_user_counter)
    tg_message_id = message_id if message_id is not None else next(_message_counter)

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
    )
    db_session.add(chat_message)
    await db_session.flush()

    version = MessageVersion(
        chat_message_id=chat_message.id,
        version_seq=1,
        text=text,
        caption=caption,
        normalized_text=text,
        content_hash=f"search-hash-{next(_hash_counter)}",
        is_redacted=version_is_redacted,
    )
    db_session.add(version)
    await db_session.flush()

    return chat_message.id, version.id, tg_message_id


async def _create_forget_event(db_session, *, chat_message_id: int, status: str) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo

    event = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(chat_message_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:-100400:{chat_message_id}:{status}",
    )
    if status == "pending":
        return

    await ForgetEventRepo.mark_status(db_session, event.id, status="processing")
    if status == "completed":
        await ForgetEventRepo.mark_status(db_session, event.id, status="completed")


async def test_search_normal_message_found(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_401
    _, version_id, message_id = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="питон помогает искать память",
    )

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert len(hits) == 1
    assert hits[0].message_version_id == version_id
    assert hits[0].chat_id == chat_id
    assert hits[0].message_id == message_id
    assert hits[0].ts_rank > 0
    assert hits[0].captured_at is not None


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
    chat_message_id, _, _ = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="pending питон",
    )
    await _create_forget_event(db_session, chat_message_id=chat_message_id, status="pending")

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_active_forget_event_completed_excluded(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_408
    chat_message_id, _, _ = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        text="completed питон",
    )
    await _create_forget_event(db_session, chat_message_id=chat_message_id, status="completed")

    assert await search_messages(db_session, "питон", chat_id=chat_id) == []


async def test_search_chat_isolation(db_session) -> None:
    from bot.services.search import search_messages

    chat_a = -100_409
    chat_b = -100_410
    await _create_versioned_message(db_session, chat_id=chat_a, text="изолированный питон")

    assert await search_messages(db_session, "питон", chat_id=chat_b) == []


async def test_search_ts_rank_ordering(db_session) -> None:
    from bot.services.search import search_messages

    chat_id = -100_411
    await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        message_id=1_041_101,
        text="питон память",
    )
    _, _, high_rank_message_id = await _create_versioned_message(
        db_session,
        chat_id=chat_id,
        message_id=1_041_102,
        text="питон питон питон память",
    )

    hits = await search_messages(db_session, "питон", chat_id=chat_id)

    assert [hit.message_id for hit in hits][:2] == [high_rank_message_id, 1_041_101]
    assert hits[0].ts_rank > hits[1].ts_rank


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
