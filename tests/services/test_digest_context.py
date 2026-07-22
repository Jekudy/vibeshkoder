"""Full-window and governance tests for issue #406 digest context."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_users = itertools.count(7_300_000_000)
_messages = itertools.count(730_000)
_chats = itertools.count(7300)


def _chat_id() -> int:
    return -1_000_000_000_000 - next(_chats)


async def _make_message(
    session,
    *,
    chat_id: int,
    ts: datetime,
    text: str,
    first_name: str = "Test",
    last_name: str | None = None,
    memory_policy: str = "normal",
    message_redacted: bool = False,
    version_redacted: bool = False,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    message_kind: str | None = "text",
    raw_json: dict | None = None,
) -> tuple[int, int, int]:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo

    user_id = next(_users)
    await UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"u{user_id}",
        first_name=first_name,
        last_name=last_name,
    )
    telegram_message_id = next(_messages)
    message = ChatMessage(
        message_id=telegram_message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        caption=caption,
        date=ts,
        raw_json=raw_json if raw_json is not None else {"text": text},
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        message_kind=message_kind,
        memory_policy=memory_policy,
        is_redacted=message_redacted,
    )
    session.add(message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text=text,
        caption=caption,
        normalized_text=text,
        entities_json={"entities": []},
        content_hash=f"digest-406-{telegram_message_id}",
        is_redacted=version_redacted,
    )
    session.add(version)
    await session.flush()
    message.current_version_id = version.id
    await session.flush()
    return message.id, version.id, telegram_message_id


async def _context(session, *, chat_id: int, start: datetime, end: datetime, type="daily"):
    from bot.services.digest_context import build_digest_context

    return await build_digest_context(
        session,
        type=type,
        window_start=start,
        window_end=end,
        source_chat_id=chat_id,
    )


async def test_full_window_returns_exact_set_above_legacy_top_n(db_session) -> None:
    chat_id = _chat_id()
    start = datetime.now(timezone.utc) - timedelta(days=1)
    expected: list[int] = []
    for index in range(75):
        _, version_id, _ = await _make_message(
            db_session,
            chat_id=chat_id,
            ts=start + timedelta(minutes=index),
            text=f"message {index}",
        )
        expected.append(version_id)

    context = await _context(
        db_session, chat_id=chat_id, start=start, end=start + timedelta(days=1)
    )

    assert context.cards == []
    assert [message.message_version_id for message in context.messages] == expected
    assert len(context.messages) == 75


async def test_context_preserves_available_telegram_metadata_without_raw_payload(db_session) -> None:
    from bot.db.models import MessageMedia

    chat_id = _chat_id()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    message_id, version_id, telegram_message_id = await _make_message(
        db_session,
        chat_id=chat_id,
        ts=start + timedelta(minutes=1),
        text="Сравнили две версии",
        first_name="Женя",
        last_name="Кудрявцев",
        caption="Подпись к схеме",
        reply_to_message_id=123,
        message_thread_id=456,
        message_kind="forward",
        raw_json={
            "text": "Сравнили две версии",
            "forward_origin": {
                "type": "user",
                "date": "2026-07-20T18:05:00+00:00",
                "sender_user": {"first_name": "Анна", "last_name": "Петрова"},
            },
        },
    )
    db_session.add(
        MessageMedia(
            chat_message_id=message_id,
            media_kind="photo",
            source_message_url=f"https://t.me/c/123/{telegram_message_id}",
            description="На схеме показаны два потока",
            description_status="ready",
        )
    )
    await db_session.flush()

    context = await _context(
        db_session, chat_id=chat_id, start=start, end=start + timedelta(hours=2)
    )
    message = context.messages[0]
    assert message.message_version_id == version_id
    assert message.telegram_message_id == telegram_message_id
    assert message.author_display == "Женя Кудрявцев"
    assert message.caption == "Подпись к схеме"
    assert message.reply_to_message_id == 123
    assert message.message_thread_id == 456
    assert message.media_kind == "photo"
    assert message.media_description == "На схеме показаны два потока"
    assert message.forward_origin_type == "user"
    assert message.forward_origin_display == "Анна Петрова"
    assert message.forward_origin_date == "2026-07-20T18:05:00+00:00"
    assert not hasattr(message, "raw_json")


@pytest.mark.parametrize("memory_policy", ["nomem", "offrecord", "forgotten"])
async def test_context_excludes_non_normal_memory_policy(db_session, memory_policy: str) -> None:
    chat_id = _chat_id()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    await _make_message(
        db_session,
        chat_id=chat_id,
        ts=start + timedelta(minutes=1),
        text="hidden",
        memory_policy=memory_policy,
    )
    assert (
        await _context(db_session, chat_id=chat_id, start=start, end=start + timedelta(hours=2))
    ).messages == []


@pytest.mark.parametrize(
    ("message_redacted", "version_redacted"), [(True, False), (False, True)]
)
async def test_context_excludes_redacted_rows(
    db_session, message_redacted: bool, version_redacted: bool
) -> None:
    chat_id = _chat_id()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    await _make_message(
        db_session,
        chat_id=chat_id,
        ts=start + timedelta(minutes=1),
        text="hidden",
        message_redacted=message_redacted,
        version_redacted=version_redacted,
    )
    assert (
        await _context(db_session, chat_id=chat_id, start=start, end=start + timedelta(hours=2))
    ).messages == []


async def test_context_excludes_cross_chat_and_outside_window(db_session) -> None:
    target = _chat_id()
    other = _chat_id()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    await _make_message(db_session, chat_id=other, ts=start, text="other chat")
    await _make_message(
        db_session, chat_id=target, ts=start - timedelta(seconds=1), text="before window"
    )
    assert (
        await _context(db_session, chat_id=target, start=start, end=start + timedelta(hours=2))
    ).messages == []


async def test_context_excludes_pending_forget_event(db_session) -> None:
    from bot.db.models import ForgetEvent

    chat_id = _chat_id()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    message_id, _, _ = await _make_message(
        db_session, chat_id=chat_id, ts=start + timedelta(minutes=1), text="forget me"
    )
    db_session.add(
        ForgetEvent(
            target_type="message",
            target_id=str(message_id),
            actor_user_id=None,
            authorized_by="self",
            tombstone_key=f"message:{chat_id}:{message_id}",
            policy="forgotten",
            status="pending",
        )
    )
    await db_session.flush()
    assert (
        await _context(db_session, chat_id=chat_id, start=start, end=start + timedelta(hours=2))
    ).messages == []


async def test_weekly_uses_the_same_complete_raw_contract(db_session) -> None:
    chat_id = _chat_id()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    expected = []
    for day in range(7):
        _, version_id, _ = await _make_message(
            db_session,
            chat_id=chat_id,
            ts=start + timedelta(days=day, hours=1),
            text=f"day {day}",
        )
        expected.append(version_id)
    context = await _context(db_session, chat_id=chat_id, start=start, end=end, type="weekly")
    assert context.type == "weekly"
    assert [message.message_version_id for message in context.messages] == expected


async def test_context_rejects_unknown_type(db_session) -> None:
    with pytest.raises(ValueError, match="unsupported type"):
        await _context(
            db_session,
            chat_id=_chat_id(),
            start=datetime.now(timezone.utc) - timedelta(days=1),
            end=datetime.now(timezone.utc),
            type="monthly",
        )
