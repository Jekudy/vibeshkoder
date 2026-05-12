"""T6-04 + T6-05 acceptance tests — card_sources repo.

PHASE6_PLAN.md §5.A + §5.C step 6: repo backs the ``/approve`` INSERT of
``card_sources`` rows + ``/card <id>`` source back-citation query.

Methods covered:

* ``bulk_create(session, card_id, message_version_ids)`` — one row per mvid,
  positions enumerated, enforces UNIQUE on (card_id, mvid).
* ``list_for_card(session, card_id)`` — JOIN to message_versions +
  chat_messages for the back-citation rendering in ``/card <id>``.
"""

from __future__ import annotations

import itertools
import uuid as _uuid_module
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_991_000_000)
_chat_counter = itertools.count(start=991_000)
_msg_counter = itertools.count(start=991_000_000)


def _next_user_id() -> int:
    return next(_user_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="U",
        last_name=None,
    )
    return uid


async def _make_chat_message_with_version(
    db_session,
    *,
    text: str = "src",
    memory_policy: str = "normal",
    is_redacted: bool = False,
    version_is_redacted: bool = False,
) -> tuple[int, int, int, int]:
    """Insert a chat_messages + v1 message_versions row. Returns
    ``(chat_message_id, message_version_id, chat_id, message_id)``."""
    from sqlalchemy import update as sa_update

    from bot.db.models import ChatMessage, MessageVersion

    uid = await _make_user(db_session)
    chat_id = _next_chat_id()
    msg_id = _next_msg_id()
    when = datetime.now(timezone.utc)
    cm = ChatMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=uid,
        text=text,
        date=when,
        created_at=when,
        memory_policy=memory_policy,
        is_redacted=is_redacted,
    )
    db_session.add(cm)
    await db_session.flush()

    mv = MessageVersion(
        chat_message_id=cm.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={},
        content_hash=f"h{_uuid_module.uuid4().hex[:16]}",
        is_redacted=version_is_redacted,
    )
    db_session.add(mv)
    await db_session.flush()
    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == cm.id)
        .values(current_version_id=mv.id)
    )
    await db_session.flush()
    return cm.id, mv.id, chat_id, msg_id


async def _make_card(db_session, admin: int) -> _uuid_module.UUID:
    from bot.db.repos.knowledge_card import KnowledgeCardRepo

    card = await KnowledgeCardRepo.create(
        db_session,
        title="t",
        body_markdown="b",
        approved_by_user_id=admin,
    )
    return card.id


async def _make_admin(db_session) -> int:
    return await _make_user(db_session)


# ─── bulk_create ─────────────────────────────────────────────────────────────


async def test_bulk_create_inserts_one_row_per_mvid(db_session) -> None:
    """One row per source mvid with enumerated position."""
    from bot.db.repos.card_source import CardSourceRepo

    admin = await _make_admin(db_session)
    card_id = await _make_card(db_session, admin)
    _, mvid1, _, _ = await _make_chat_message_with_version(db_session)
    _, mvid2, _, _ = await _make_chat_message_with_version(db_session)

    rows = await CardSourceRepo.bulk_create(
        db_session,
        card_id=card_id,
        message_version_ids=[mvid1, mvid2],
    )
    assert len(rows) == 2
    assert rows[0].card_id == card_id
    assert rows[0].message_version_id == mvid1
    assert rows[0].position == 0
    assert rows[1].message_version_id == mvid2
    assert rows[1].position == 1


async def test_bulk_create_enforces_unique(db_session) -> None:
    """Inserting the same (card_id, mvid) twice violates the UNIQUE constraint."""
    from bot.db.repos.card_source import CardSourceRepo

    admin = await _make_admin(db_session)
    card_id = await _make_card(db_session, admin)
    _, mvid, _, _ = await _make_chat_message_with_version(db_session)

    await CardSourceRepo.bulk_create(
        db_session,
        card_id=card_id,
        message_version_ids=[mvid],
    )
    with pytest.raises(Exception):
        await CardSourceRepo.bulk_create(
            db_session,
            card_id=card_id,
            message_version_ids=[mvid],
        )


# ─── list_for_card ───────────────────────────────────────────────────────────


async def test_list_for_card_returns_join_rows(db_session) -> None:
    """Returns rows with mvid + chat_id + message_id + memory_policy joined."""
    from bot.db.repos.card_source import CardSourceRepo

    admin = await _make_admin(db_session)
    card_id = await _make_card(db_session, admin)
    _, mvid, chat_id, msg_id = await _make_chat_message_with_version(
        db_session, text="hello"
    )
    await CardSourceRepo.bulk_create(
        db_session,
        card_id=card_id,
        message_version_ids=[mvid],
    )

    rows = await CardSourceRepo.list_for_card(db_session, card_id)
    assert len(rows) == 1
    row = rows[0]
    # Returned record exposes the joined fields needed by /card <id> renderer.
    assert row.message_version_id == mvid
    assert row.chat_id == chat_id
    assert row.message_id == msg_id
    assert row.memory_policy == "normal"
    assert row.is_redacted is False
    assert row.mv_is_redacted is False


async def test_list_for_card_orders_by_position(db_session) -> None:
    """Rows ordered by ``position ASC`` then ``id ASC``."""
    from bot.db.repos.card_source import CardSourceRepo

    admin = await _make_admin(db_session)
    card_id = await _make_card(db_session, admin)
    _, mvid1, _, _ = await _make_chat_message_with_version(db_session)
    _, mvid2, _, _ = await _make_chat_message_with_version(db_session)
    _, mvid3, _, _ = await _make_chat_message_with_version(db_session)

    await CardSourceRepo.bulk_create(
        db_session,
        card_id=card_id,
        message_version_ids=[mvid1, mvid2, mvid3],
    )
    rows = await CardSourceRepo.list_for_card(db_session, card_id)
    positions = [r.position for r in rows]
    assert positions == [0, 1, 2]


async def test_list_for_card_returns_empty_for_unknown_card(db_session) -> None:
    from bot.db.repos.card_source import CardSourceRepo

    rows = await CardSourceRepo.list_for_card(db_session, _uuid_module.uuid4())
    assert rows == []
