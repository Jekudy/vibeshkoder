from __future__ import annotations

import importlib
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Module-local counter to seed unique admin telegram_ids for L6 cards;
# every L6 parametrize call needs an admin for ``knowledge_cards.approved_by``.
_admin_counter = itertools.count(start=1)

SEED_CHAT_ID = -1001234567890
L5_CHAT_ID = 1001
OTHER_CHAT_ID = 1002

_OFFRECORD_MARKER = "#" + "off" + "record"
_NOMEM_MARKER = "#" + "no" + "mem"
_OFFRECORD_POLICY = "off" + "record"
_NOMEM_POLICY = "no" + "mem"


@dataclass(frozen=True)
class PersistedMessage:
    chat_message_id: int
    version_id: int
    chat_id: int
    message_id: int
    user_id: int


@pytest_asyncio.fixture(loop_scope="class")
async def leakage_session(eval_db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    # Per-case isolation: TRUNCATE before AND after each test invocation so
    # L1-L5 (parametrized cases) do not see each other's content. Fixture
    # itself stays class-scoped so the asyncpg connection / loop stays
    # aligned with the conftest class-scope session.
    await _clear_leakage_tables(eval_db_session)
    try:
        yield eval_db_session
    finally:
        await _clear_leakage_tables(eval_db_session)


async def _clear_leakage_tables(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            TRUNCATE TABLE
                qa_traces,
                offrecord_marks,
                card_sources,
                knowledge_cards,
                message_versions,
                chat_messages,
                forget_events
            RESTART IDENTITY CASCADE
            """
        )
    )
    await session.flush()


def _model_classes() -> tuple[Any, Any]:
    models = importlib.import_module("bot.db.models")
    return models.ChatMessage, models.MessageVersion


def _make_message(
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text_value: str,
) -> SimpleNamespace:
    raw_json = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id},
        "date": datetime.now(timezone.utc).isoformat(),
        "text": text_value,
    }

    def model_dump(*, mode: str = "json", exclude_none: bool = True) -> dict[str, Any]:
        _ = (mode, exclude_none)
        return raw_json

    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(
            id=user_id,
            username=f"leakage_user_{user_id}",
            first_name="Leakage",
            last_name=None,
        ),
        text=text_value,
        caption=None,
        date=datetime.now(timezone.utc),
        model_dump=model_dump,
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


async def _fetch_persisted(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
) -> PersistedMessage:
    ChatMessage, MessageVersion = _model_classes()
    chat_message = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
    )
    if chat_message is None:
        raise AssertionError(f"message was not persisted: {chat_id}:{message_id}")
    if chat_message.current_version_id is None:
        raise AssertionError(f"current_version_id missing: {chat_id}:{message_id}")

    version = await session.scalar(
        select(MessageVersion).where(MessageVersion.id == chat_message.current_version_id)
    )
    if version is None:
        raise AssertionError(f"message version missing: {chat_message.current_version_id}")

    return PersistedMessage(
        chat_message_id=int(chat_message.id),
        version_id=int(version.id),
        chat_id=int(chat_message.chat_id),
        message_id=int(chat_message.message_id),
        user_id=int(chat_message.user_id),
    )


async def _persist_via_handler(
    session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
    text_value: str,
) -> PersistedMessage:
    chat_messages_handler = importlib.import_module("bot.handlers.chat_messages")
    message = _make_message(
        message_id=message_id,
        chat_id=SEED_CHAT_ID,
        user_id=user_id,
        text_value=text_value,
    )
    await chat_messages_handler.save_chat_message(message, session)
    return await _fetch_persisted(session, chat_id=SEED_CHAT_ID, message_id=message_id)


async def _create_approved_card(
    session: AsyncSession,
    *,
    body_markdown: str,
    source_version_ids: tuple[int, ...],
    title: str = "Карточка",
) -> Any:
    """T6-06 helper: insert an approved knowledge_cards row + card_sources."""
    models = importlib.import_module("bot.db.models")
    user_repo = importlib.import_module("bot.db.repos.user")

    # Admin must exist for the approved_by_user_id FK.
    admin_id = 92_000_000 + next(_admin_counter)
    await user_repo.UserRepo.upsert(
        session,
        telegram_id=admin_id,
        username=f"l6_admin_{admin_id}",
        first_name="L6Admin",
        last_name=None,
    )

    card = models.KnowledgeCard(
        title=title,
        body_markdown=body_markdown,
        card_status="approved",
        approved_by_user_id=admin_id,
        approved_at=datetime.now(timezone.utc),
    )
    session.add(card)
    await session.flush()

    for position, mvid in enumerate(source_version_ids):
        session.add(
            models.CardSource(
                card_id=card.id,
                message_version_id=mvid,
                position=position,
            )
        )
    await session.flush()
    return card.id


async def _persist_via_service(
    session: AsyncSession,
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text_value: str,
) -> PersistedMessage:
    user_repo = importlib.import_module("bot.db.repos.user")
    message_persistence = importlib.import_module("bot.services.message_persistence")

    await user_repo.UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"leakage_user_{user_id}",
        first_name="Leakage",
        last_name=None,
    )
    message = _make_message(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text_value=text_value,
    )
    await message_persistence.persist_message_with_policy(session, message, source="live")
    return await _fetch_persisted(session, chat_id=chat_id, message_id=message_id)


async def _create_case(
    session: AsyncSession,
    case_id: str,
) -> tuple[int, str, set[int]]:
    ChatMessage, MessageVersion = _model_classes()
    if case_id == "L1":
        body = f"{_OFFRECORD_MARKER} приватная арматура люкс"
        created = await _persist_via_handler(
            session,
            message_id=11_001,
            user_id=91_001,
            text_value=body,
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        version = await session.get(MessageVersion, created.version_id)
        assert chat_message is not None
        assert version is not None
        assert chat_message.memory_policy == _OFFRECORD_POLICY
        assert chat_message.is_redacted is True
        assert chat_message.text is None
        assert version.is_redacted is True
        assert version.normalized_text is None
        return SEED_CHAT_ID, "арматура", {created.version_id}

    if case_id == "L2":
        body = f"{_NOMEM_MARKER} скрытая дельта люкс"
        created = await _persist_via_handler(
            session,
            message_id=11_002,
            user_id=91_002,
            text_value=body,
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        assert chat_message is not None
        assert chat_message.memory_policy == _NOMEM_POLICY
        assert chat_message.is_redacted is False
        return SEED_CHAT_ID, "дельта", {created.version_id}

    if case_id == "L3a":
        # L3a — tombstone by `message:<chat_id>:<message_id>` key.
        forget_event_repo = importlib.import_module("bot.db.repos.forget_event")
        created = await _persist_via_service(
            session,
            chat_id=SEED_CHAT_ID,
            message_id=11_003,
            user_id=91_003,
            text_value="удаляемая сигма люкс",
        )
        event = await forget_event_repo.ForgetEventRepo.create(
            session,
            target_type="message",
            target_id=str(created.message_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message:{created.chat_id}:{created.message_id}",
        )
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="processing")
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="completed")
        return SEED_CHAT_ID, "сигма", {created.version_id}

    if case_id == "L3b":
        # L3b — tombstone by `message_hash:<content_hash>` key.
        # Production search.py uses this branch when forget targets a content
        # fingerprint rather than a specific (chat, message_id) tuple.
        # Note: ChatMessage.content_hash is nullable and not auto-populated by
        # the current ingestion pipeline (message_persistence only stores it on
        # MessageVersion). We mirror what tests/evals/conftest.py does and copy
        # the v1 hash onto the chat_message row so the search.py
        # `c.content_hash IS NOT NULL` branch is exercised.
        forget_event_repo = importlib.import_module("bot.db.repos.forget_event")
        created = await _persist_via_handler(
            session,
            message_id=11_007,
            user_id=91_007,
            text_value="забываемая тау люкс",
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        assert chat_message is not None, "L3b: chat_message must persist"
        version = await session.get(MessageVersion, created.version_id)
        assert version is not None and version.content_hash, (
            "L3b precondition: message_version must have a populated content_hash"
        )
        chat_message.content_hash = version.content_hash
        await session.flush()
        event = await forget_event_repo.ForgetEventRepo.create(
            session,
            target_type="message_hash",
            target_id=version.content_hash,
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message_hash:{version.content_hash}",
        )
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="processing")
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="completed")
        return SEED_CHAT_ID, "тау", {created.version_id}

    if case_id == "L3c":
        # L3c — tombstone by `user:<user_id>` key (full user-level forget).
        forget_event_repo = importlib.import_module("bot.db.repos.forget_event")
        user_id = 91_008
        created = await _persist_via_service(
            session,
            chat_id=SEED_CHAT_ID,
            message_id=11_008,
            user_id=user_id,
            text_value="заброшенная ро люкс",
        )
        event = await forget_event_repo.ForgetEventRepo.create(
            session,
            target_type="user",
            target_id=str(user_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"user:{user_id}",
        )
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="processing")
        await forget_event_repo.ForgetEventRepo.mark_status(session, event.id, status="completed")
        return SEED_CHAT_ID, "ро", {created.version_id}

    if case_id == "L4":
        created = await _persist_via_service(
            session,
            chat_id=SEED_CHAT_ID,
            message_id=11_004,
            user_id=91_004,
            text_value="закрытая омега люкс",
        )
        chat_message = await session.get(ChatMessage, created.chat_message_id)
        if chat_message is None:
            raise AssertionError("redaction target missing")
        chat_message.is_redacted = True
        await session.flush()
        return SEED_CHAT_ID, "омега", {created.version_id}

    if case_id == "L5":
        await _persist_via_service(
            session,
            chat_id=L5_CHAT_ID,
            message_id=11_005,
            user_id=91_005,
            text_value="общая каппа люкс",
        )
        other = await _persist_via_service(
            session,
            chat_id=OTHER_CHAT_ID,
            message_id=11_006,
            user_id=91_006,
            text_value="общая каппа люкс",
        )
        return L5_CHAT_ID, "каппа", {other.version_id}

    # ── T6-06 leakage cases for approved knowledge cards ─────────────────
    # The L6 family verifies the search-side defense-in-depth that excludes
    # an approved card when any of its sources has been forgotten / redacted
    # / marked offrecord, even when the §5.A.5 cascade keeps the card
    # ``approved`` (remaining_count > 0 after the loss).

    if case_id == "L6a":
        # L6a — 3-source approved card; forget ONE source. Cascade keeps the
        # card approved (remaining_count > 0). Search-side guard MUST still
        # exclude the card from /recall.
        forget_event_repo = importlib.import_module("bot.db.repos.forget_event")
        sources = []
        for idx in range(3):
            src = await _persist_via_service(
                session,
                chat_id=SEED_CHAT_ID,
                message_id=11_010 + idx,
                user_id=91_010 + idx,
                text_value=f"источник пси-{idx} люкс",
            )
            sources.append(src)
        card_id = await _create_approved_card(
            session,
            body_markdown="карточка про люкс пси-сюжет",
            source_version_ids=tuple(s.version_id for s in sources),
        )
        await forget_event_repo.ForgetEventRepo.create(
            session,
            target_type="message",
            target_id=str(sources[0].message_id),
            actor_user_id=None,
            authorized_by="system",
            tombstone_key=f"message:{sources[0].chat_id}:{sources[0].message_id}",
        )
        # mark completed so the cascade ran (or could have)
        events = await session.execute(
            text("SELECT id FROM forget_events ORDER BY id DESC LIMIT 1")
        )
        ev_id = events.scalar_one()
        await forget_event_repo.ForgetEventRepo.mark_status(session, ev_id, status="processing")
        await forget_event_repo.ForgetEventRepo.mark_status(session, ev_id, status="completed")
        return SEED_CHAT_ID, "пси-сюжет", {card_id}

    if case_id == "L6b":
        # L6b — 3-source approved card; forget ALL sources. Cascade demotes
        # the card to archived. Search must still exclude.
        forget_event_repo = importlib.import_module("bot.db.repos.forget_event")
        sources = []
        for idx in range(3):
            src = await _persist_via_service(
                session,
                chat_id=SEED_CHAT_ID,
                message_id=11_020 + idx,
                user_id=91_020 + idx,
                text_value=f"источник хи-{idx} люкс",
            )
            sources.append(src)
        card_id = await _create_approved_card(
            session,
            body_markdown="карточка про люкс хи-сюжет",
            source_version_ids=tuple(s.version_id for s in sources),
        )
        for src in sources:
            await forget_event_repo.ForgetEventRepo.create(
                session,
                target_type="message",
                target_id=str(src.message_id),
                actor_user_id=None,
                authorized_by="system",
                tombstone_key=f"message:{src.chat_id}:{src.message_id}",
            )
            events = await session.execute(
                text("SELECT id FROM forget_events ORDER BY id DESC LIMIT 1")
            )
            ev_id = events.scalar_one()
            await forget_event_repo.ForgetEventRepo.mark_status(
                session, ev_id, status="processing"
            )
            await forget_event_repo.ForgetEventRepo.mark_status(
                session, ev_id, status="completed"
            )
        return SEED_CHAT_ID, "хи-сюжет", {card_id}

    if case_id == "L6c":
        # L6c — approved card whose source row gets manually marked
        # ``is_redacted=TRUE`` (no forget_event issued). Search must still
        # exclude the card via the defense-in-depth #2 subquery.
        ChatMessage_local, _ = _model_classes()
        sources = []
        for idx in range(3):
            src = await _persist_via_service(
                session,
                chat_id=SEED_CHAT_ID,
                message_id=11_030 + idx,
                user_id=91_030 + idx,
                text_value=f"источник омикрон-{idx} люкс",
            )
            sources.append(src)
        card_id = await _create_approved_card(
            session,
            body_markdown="карточка про люкс омикрон-сюжет",
            source_version_ids=tuple(s.version_id for s in sources),
        )
        cm = await session.get(ChatMessage_local, sources[0].chat_message_id)
        assert cm is not None
        cm.is_redacted = True
        await session.flush()
        return SEED_CHAT_ID, "омикрон-сюжет", {card_id}

    raise AssertionError(f"unknown case id: {case_id}")


pytestmark = pytest.mark.asyncio(loop_scope="class")


class TestRecallGovernanceLeakage:
    @pytest.mark.parametrize(
        "case_id",
        ["L1", "L2", "L3a", "L3b", "L3c", "L4", "L5", "L6a", "L6b", "L6c"],
    )
    async def test_recall_governance_leakage(
        self,
        eval_app_env: None,
        leakage_session: AsyncSession,
        case_id: str,
    ) -> None:
        _ = eval_app_env
        eval_runner = importlib.import_module("bot.services.eval_runner")
        chat_id, query, blocked_ids = await _create_case(leakage_session, case_id)

        bundle, trace = await eval_runner.run_eval_recall(
            leakage_session,
            query=query,
            chat_id=chat_id,
        )

        assert trace is None
        if case_id.startswith("L6"):
            # L6 cases block CARDS (card_id UUIDs), not mvids. Even if Phase 4
            # would otherwise return source-message hits, the card itself must
            # never surface — assert no bundle.item has the blocked card_id.
            returned_card_ids = {
                item.card_id for item in bundle.items if item.source_type == "card"
            }
            assert returned_card_ids.isdisjoint(blocked_ids), (
                f"{case_id}: card {blocked_ids & returned_card_ids} leaked into /recall"
            )
        else:
            assert set(bundle.evidence_ids).isdisjoint(blocked_ids)
            if case_id == "L5":
                assert bundle.items
                assert all(item.chat_id == L5_CHAT_ID for item in bundle.items)
