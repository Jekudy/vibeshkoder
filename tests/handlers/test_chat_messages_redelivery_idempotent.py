"""Handler-level redelivery idempotency test (Issue #67, Codex MEDIUM).

Calls the ``save_chat_message`` handler twice for the same message carrying the legacy
``#offrecord`` token.  The row remains normal memory, no opt-out mark is created, and
redelivery is a true no-op at the persistence layer.

Strategy:
- Uses the real ``db_session`` fixture (postgres-backed, outer-tx isolation).
- Calls ``bot.handlers.chat_messages.save_chat_message`` directly with a
  ``SimpleNamespace`` message, mirroring the pattern used in other handler tests.
- The ``db_session`` is passed directly as the ``session`` argument (bypassing
  aiogram middleware). All saves happen in the same outer transaction, rolled back
  at teardown.

Note: ``save_chat_message`` checks ``message.chat.id == settings.COMMUNITY_CHAT_ID``.
We set ``COMMUNITY_CHAT_ID=-1001234567890`` via the ``app_env`` fixture and craft the
message with a matching chat_id so the handler body is reached.

If postgres is unavailable, the ``db_session`` fixture skips the test automatically
(consistent with all other DB-backed tests in this repo).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

pytestmark = pytest.mark.usefixtures("app_env")

_user_counter = itertools.count(start=9_200_000_000)
_msg_counter = itertools.count(start=990_000)

COMMUNITY_CHAT_ID = -1001234567890


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _make_offrecord_message(
    *,
    message_id: int,
    user_id: int,
    chat_id: int = COMMUNITY_CHAT_ID,
    text: str = "#offrecord secret note",
) -> SimpleNamespace:
    """Build a minimal SimpleNamespace shaped like an aiogram Message.

    The ``model_dump`` Mock is required because normal text persistence records the
    source payload via ``message.model_dump(mode="json", exclude_none=True)``.
    """
    raw_json = {"message_id": message_id, "text": text}
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"u{user_id}",
            first_name="Test",
            last_name=None,
            is_bot=False,
        ),
        text=text,
        caption=None,
        date=datetime.now(timezone.utc),
        model_dump=Mock(return_value=raw_json),
        # Fields probed by extract_normalized_fields:
        reply_to_message=None,
        message_thread_id=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        animation=None,
        video_note=None,
        location=None,
        contact=None,
        poll=None,
        dice=None,
        forward_origin=None,
        new_chat_members=None,
        left_chat_member=None,
        pinned_message=None,
        entities=None,
        caption_entities=None,
    )


async def test_handler_redelivery_keeps_legacy_token_as_one_normal_row(db_session) -> None:
    """Deliver the same #offrecord message through save_chat_message twice.

    Asserts:
    - Exactly 1 chat_messages row for the (chat_id, message_id) pair.
    - The row has normal memory policy.
    - No offrecord_marks row is created.
    """
    from bot.db.models import ChatMessage, OffrecordMark
    from bot.handlers.chat_messages import save_chat_message

    user_id = _next_user()
    msg_id = _next_msg_id()

    message = _make_offrecord_message(message_id=msg_id, user_id=user_id)

    # First delivery.
    await save_chat_message(message, db_session)
    # Second delivery (simulates polling restart / duplicate update).
    await save_chat_message(message, db_session)

    # Exactly one chat_messages row.
    chat_msg_count = await db_session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.chat_id == COMMUNITY_CHAT_ID,
            ChatMessage.message_id == msg_id,
        )
    )
    assert chat_msg_count == 1, (
        f"Expected 1 chat_messages row after double delivery, got {chat_msg_count}"
    )

    # Resolve the saved row's PK to check offrecord_marks.
    chat_msg_row = await db_session.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == COMMUNITY_CHAT_ID,
            ChatMessage.message_id == msg_id,
        )
    )
    assert chat_msg_row is not None
    assert chat_msg_row.memory_policy == "normal"
    assert chat_msg_row.is_redacted is False
    assert chat_msg_row.text == "#offrecord secret note"

    # No legacy opt-out mark.
    mark_count = await db_session.scalar(
        select(func.count())
        .select_from(OffrecordMark)
        .where(OffrecordMark.chat_message_id == chat_msg_row.id)
    )
    assert mark_count == 0
