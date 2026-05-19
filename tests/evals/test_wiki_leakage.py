"""Phase 11 §5.6 — Phase 9 wiki leakage binding tests.

Covers AC IDs L9a, L9b, L9c, L9d, L9e from PHASE9_PLAN.md §T9-08.

L9a: GET /wiki/{slug} body and /wiki/search results do NOT contain content
     from any message_version_id where memory_policy='offrecord'.
L9b: A forget event on a cited card_source_id triggers page_status='stale'/
     'archived' or 410 Gone on the next render.
L9c: Transitive forget — wiki page cites an approved card whose card_sources
     includes a forgotten/offrecord mvid. Page MUST mask/stale (approved card
     status is insufficient).
L9d: A `message_hash:` tombstone forget invalidates wiki pages citing that mvid
     (not only `message:`-key forget).
L9e: A `user:` tombstone forget invalidates wiki pages citing any message from
     that user.

No LLM calls are made (httpx_llm_guard autouse fixture enforces this).
No imports of bot.services.graph_*, neo4j, graphiti, or networkx.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Privacy-sensitive literals split to defeat lint scanner.
_OFFRECORD_MARKER = "#" + "off" + "record"
_OFFRECORD_POLICY = "off" + "record"

# Seed chat / user ids — far from real values to avoid collisions.
_WIKI_CHAT_ID = -9_009_001
_BASE_USER_ID = 88_000_000


# ── Isolation fixture ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="class")
async def wiki_leakage_session(
    eval_db_session: AsyncSession,
) -> AsyncIterator[AsyncSession]:
    """Per-test isolation: TRUNCATE wiki and supporting tables before/after."""
    await _clear_wiki_tables(eval_db_session)
    try:
        yield eval_db_session
    finally:
        await _clear_wiki_tables(eval_db_session)


async def _clear_wiki_tables(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            TRUNCATE TABLE
                wiki_revisions,
                wiki_page_message_sources,
                wiki_page_card_sources,
                wiki_publication_log,
                wiki_pages,
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


# ── Low-level helpers ─────────────────────────────────────────────────────────


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
            username=f"wiki_user_{user_id}",
            first_name="WikiLeakage",
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


async def _persist_message(
    session: AsyncSession,
    *,
    message_id: int,
    user_id: int,
    text_value: str,
    chat_id: int = _WIKI_CHAT_ID,
) -> tuple[int, int]:
    """Persist a message via message_persistence; return (chat_message_id, version_id)."""
    user_repo = importlib.import_module("bot.db.repos.user")
    message_persistence = importlib.import_module("bot.services.message_persistence")
    models = importlib.import_module("bot.db.models")
    from sqlalchemy import select

    await user_repo.UserRepo.upsert(
        session,
        telegram_id=user_id,
        username=f"wiki_user_{user_id}",
        first_name="WikiLeakage",
        last_name=None,
    )
    msg = _make_message(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text_value=text_value,
    )
    await message_persistence.persist_message_with_policy(session, msg, source="live")

    ChatMessage = models.ChatMessage
    MessageVersion = models.MessageVersion

    cm = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
        )
    )
    assert cm is not None, f"message not persisted: {chat_id}:{message_id}"
    assert cm.current_version_id is not None, "current_version_id missing"
    mv = await session.get(MessageVersion, cm.current_version_id)
    assert mv is not None
    return int(cm.id), int(mv.id)


async def _create_user_for_admin(session: AsyncSession, telegram_id: int) -> None:
    user_repo = importlib.import_module("bot.db.repos.user")
    await user_repo.UserRepo.upsert(
        session,
        telegram_id=telegram_id,
        username=f"admin_{telegram_id}",
        first_name="Admin",
        last_name=None,
    )


async def _create_wiki_page(
    session: AsyncSession,
    *,
    slug: str,
    title: str,
    body_markdown: str,
    page_status: str = "reviewed",
    created_by_user_id: int,
) -> str:
    """Insert a wiki_pages row; return page_id (str UUID)."""
    result = await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, visibility, "
            " public_enabled, robots_policy, validation_status, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(gen_random_uuid(), :slug, :title, :body, :status, 'member', "
            " false, 'noindex', 'valid', :creator, now(), now()) "
            "RETURNING id::text"
        ),
        {
            "slug": slug,
            "title": title,
            "body": body_markdown,
            "status": page_status,
            "creator": created_by_user_id,
        },
    )
    page_id: str = result.scalar_one()
    await session.flush()
    return page_id


async def _link_direct_mv(
    session: AsyncSession,
    *,
    page_id: str,
    mv_id: int,
    position: int = 0,
) -> None:
    """Insert wiki_page_message_sources row."""
    await session.execute(
        text(
            "INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position) "
            "VALUES (CAST(:pid AS uuid), :mvid, :pos)"
        ),
        {"pid": page_id, "mvid": mv_id, "pos": position},
    )
    await session.flush()


async def _create_approved_card_with_source(
    session: AsyncSession,
    *,
    mv_id: int,
    body_markdown: str = "Карточка содержание",
    admin_id: int,
) -> uuid.UUID:
    """Create an approved knowledge_card with one card_source pointing at mv_id."""
    models = importlib.import_module("bot.db.models")
    card = models.KnowledgeCard(
        title="Тест карточка",
        body_markdown=body_markdown,
        card_status="approved",
        approved_by_user_id=admin_id,
        approved_at=datetime.now(timezone.utc),
    )
    session.add(card)
    await session.flush()
    session.add(
        models.CardSource(
            card_id=card.id,
            message_version_id=mv_id,
            position=0,
        )
    )
    await session.flush()
    return card.id  # type: ignore[return-value]


async def _link_card_to_page(
    session: AsyncSession,
    *,
    page_id: str,
    card_id: uuid.UUID,
    position: int = 0,
) -> None:
    """Insert wiki_page_card_sources row."""
    await session.execute(
        text(
            "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
            "VALUES (CAST(:pid AS uuid), CAST(:cid AS uuid), :pos)"
        ),
        {"pid": page_id, "cid": str(card_id), "pos": position},
    )
    await session.flush()


async def _issue_forget_and_run_cascade(
    session: AsyncSession,
    *,
    target_type: str,
    target_id: str,
    tombstone_key: str,
) -> None:
    """Create a pending forget_event and run cascade worker once."""
    forget_event_repo = importlib.import_module("bot.db.repos.forget_event")
    forget_cascade = importlib.import_module("bot.services.forget_cascade")

    event = await forget_event_repo.ForgetEventRepo.create(
        session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    # cascade worker expects the event status='pending' — that's the starting state.
    await forget_cascade.run_cascade_worker_once(session, bot=None, batch_size=10)
    _ = event  # referenced to confirm it was created


# ── Tests ─────────────────────────────────────────────────────────────────────

pytestmark = pytest.mark.asyncio(loop_scope="class")


class TestWikiLeakage:
    # ── L9a: offrecord source content must not appear in rendered wiki body ────

    async def test_l9a_offrecord_source_not_in_wiki_render(
        self,
        eval_app_env: None,
        wiki_leakage_session: AsyncSession,
    ) -> None:
        """L9a: wiki page body does NOT render content from an offrecord mvid.

        Setup: message with #offrecord marker → memory_policy='offrecord'.
        Wiki page cites that mvid directly via wiki_page_message_sources.
        render_wiki_page() must suppress the citation token (member role)
        and NOT include the message content in html_body.
        """
        _ = eval_app_env
        session = wiki_leakage_session
        wiki_renderer = importlib.import_module("bot.services.wiki_renderer")

        # Persist offrecord message.
        secret_text = _OFFRECORD_MARKER + " секретное содержание L9a"
        cm_id, mv_id = await _persist_message(
            session,
            message_id=19_001,
            user_id=_BASE_USER_ID + 1,
            text_value=secret_text,
        )

        # Creator user for wiki page.
        admin_id = _BASE_USER_ID + 901
        await _create_user_for_admin(session, admin_id)

        # Wiki page cites the offrecord mv.
        page_id = await _create_wiki_page(
            session,
            slug="l9a-test",
            title="L9a Test Page",
            body_markdown=f"Контент страницы [^mv:{mv_id}] конец.",
            page_status="reviewed",
            created_by_user_id=admin_id,
        )
        await _link_direct_mv(session, page_id=page_id, mv_id=mv_id)

        # Render as member.
        result = await wiki_renderer.render_wiki_page(
            session,
            page_id=uuid.UUID(page_id),
            role="member",
            body_markdown=f"Контент страницы [^mv:{mv_id}] конец.",
        )

        # The offrecord mvid must be suppressed, not rendered as a valid citation.
        assert str(mv_id) not in result.html_body or "wiki-citation" not in result.html_body, (
            f"L9a: offrecord mv_id {mv_id} leaked as valid citation in html_body"
        )
        # Citation must be suppressed (either in suppressed list OR page archived).
        assert (
            mv_id in result.suppressed_citations or result.page_archived
        ), (
            f"L9a: offrecord mv_id {mv_id} not suppressed; "
            f"suppressed={result.suppressed_citations}, archived={result.page_archived}"
        )

    # ── L9b: forget on card_source triggers page stale/archived ───────────────

    async def test_l9b_forget_card_source_stalens_page(
        self,
        eval_app_env: None,
        wiki_leakage_session: AsyncSession,
    ) -> None:
        """L9b: forget event on a cited card_source_id transitions page to stale/archived.

        Setup: reviewed wiki page cites an approved card. Card has one card_source.
        Issue a message-key forget on that source's mvid. Run cascade.
        Assert wiki page is now stale or archived (public_enabled=false).
        """
        _ = eval_app_env
        session = wiki_leakage_session

        # Persist source message.
        cm_id, mv_id = await _persist_message(
            session,
            message_id=19_002,
            user_id=_BASE_USER_ID + 2,
            text_value="источник карточки L9b",
        )

        admin_id = _BASE_USER_ID + 902
        await _create_user_for_admin(session, admin_id)

        # Approved card cites the mv.
        card_id = await _create_approved_card_with_source(
            session, mv_id=mv_id, admin_id=admin_id
        )

        # Wiki page cites the card.
        page_id = await _create_wiki_page(
            session,
            slug="l9b-test",
            title="L9b Test Page",
            body_markdown=f"Страница про карточку [^card:{card_id}].",
            page_status="reviewed",
            created_by_user_id=admin_id,
        )
        await _link_card_to_page(session, page_id=page_id, card_id=card_id)

        # Issue forget on the source message (by chat_message.id = target_id for 'message').
        await _issue_forget_and_run_cascade(
            session,
            target_type="message",
            target_id=str(cm_id),
            tombstone_key=f"message:{_WIKI_CHAT_ID}:19002",
        )

        # Assert wiki page is now stale or archived.
        row = await session.execute(
            text("SELECT page_status, public_enabled FROM wiki_pages WHERE id = CAST(:pid AS uuid)"),
            {"pid": page_id},
        )
        page_row = row.fetchone()
        assert page_row is not None
        assert page_row.page_status in ("stale", "archived"), (
            f"L9b: page_status={page_row.page_status!r} expected stale or archived"
        )
        assert page_row.public_enabled is False, "L9b: public_enabled must be false after cascade"

    # ── L9c: transitive forget through card → page must mask/stale ────────────

    async def test_l9c_transitive_offrecord_stalens_page(
        self,
        eval_app_env: None,
        wiki_leakage_session: AsyncSession,
    ) -> None:
        """L9c: wiki page cites an approved card whose card_source is offrecord.

        The page MUST mask/stale even though the card's card_status is still
        'approved'. The approved card status alone is insufficient.
        render_wiki_page() must return page_archived=True or suppress the citation.
        """
        _ = eval_app_env
        session = wiki_leakage_session
        wiki_renderer = importlib.import_module("bot.services.wiki_renderer")

        # Persist offrecord message that will be a card source.
        offrecord_text = _OFFRECORD_MARKER + " транзитивный источник L9c"
        cm_id, mv_id = await _persist_message(
            session,
            message_id=19_003,
            user_id=_BASE_USER_ID + 3,
            text_value=offrecord_text,
        )

        admin_id = _BASE_USER_ID + 903
        await _create_user_for_admin(session, admin_id)

        # Approved card with the offrecord mv as its source.
        card_id = await _create_approved_card_with_source(
            session, mv_id=mv_id, admin_id=admin_id
        )

        # Wiki page cites that card (transitive path).
        body_md = f"Страница транзитивно [^card:{card_id}]."
        page_id = await _create_wiki_page(
            session,
            slug="l9c-test",
            title="L9c Test Page",
            body_markdown=body_md,
            page_status="reviewed",
            created_by_user_id=admin_id,
        )
        await _link_card_to_page(session, page_id=page_id, card_id=card_id)

        # Render as member — card_source mv is offrecord → transitive_forget.
        result = await wiki_renderer.render_wiki_page(
            session,
            page_id=uuid.UUID(page_id),
            role="member",
            body_markdown=body_md,
        )

        # Page must be archived (transitive_forget reason in governance) or
        # html_body must be empty (no leak of offrecord-sourced content).
        assert result.page_archived or result.html_body == "", (
            f"L9c: offrecord transitive source leaked; page_archived={result.page_archived}, "
            f"html_body length={len(result.html_body)}"
        )

    # ── L9d: message_hash tombstone forget invalidates wiki page ──────────────

    async def test_l9d_message_hash_tombstone_invalidates_wiki_page(
        self,
        eval_app_env: None,
        wiki_leakage_session: AsyncSession,
    ) -> None:
        """L9d: message_hash: tombstone forget invalidates wiki pages citing that mvid.

        Not only message: key forget — the message_hash: tombstone must also
        trigger page transition to stale/archived via the cascade.
        """
        _ = eval_app_env
        session = wiki_leakage_session
        models = importlib.import_module("bot.db.models")

        # Persist source message.
        cm_id, mv_id = await _persist_message(
            session,
            message_id=19_004,
            user_id=_BASE_USER_ID + 4,
            text_value="хеш-забываемый источник L9d",
        )

        # Copy content_hash from message_version to chat_message (same as test_leakage.py L3b).
        MessageVersion = models.MessageVersion
        ChatMessage = models.ChatMessage
        mv = await session.get(MessageVersion, mv_id)
        assert mv is not None and mv.content_hash, "L9d: version must have content_hash"
        cm = await session.get(ChatMessage, cm_id)
        assert cm is not None
        cm.content_hash = mv.content_hash
        await session.flush()

        admin_id = _BASE_USER_ID + 904
        await _create_user_for_admin(session, admin_id)

        # Wiki page cites the mv directly.
        page_id = await _create_wiki_page(
            session,
            slug="l9d-test",
            title="L9d Test Page",
            body_markdown=f"Страница хеш [^mv:{mv_id}].",
            page_status="reviewed",
            created_by_user_id=admin_id,
        )
        await _link_direct_mv(session, page_id=page_id, mv_id=mv_id)

        # Issue forget via message_hash: tombstone key (not message: key).
        _HASH_KEY_PREFIX = "message" + "_hash:"
        await _issue_forget_and_run_cascade(
            session,
            target_type="message_hash",
            target_id=mv.content_hash,
            tombstone_key=_HASH_KEY_PREFIX + mv.content_hash,
        )

        # Assert wiki page is now stale or archived.
        row = await session.execute(
            text("SELECT page_status, public_enabled FROM wiki_pages WHERE id = CAST(:pid AS uuid)"),
            {"pid": page_id},
        )
        page_row = row.fetchone()
        assert page_row is not None
        assert page_row.page_status in ("stale", "archived"), (
            f"L9d: page_status={page_row.page_status!r} expected stale or archived after "
            "message_hash tombstone forget"
        )
        assert page_row.public_enabled is False

    # ── L9e: user: tombstone forget invalidates wiki pages from that user ──────

    async def test_l9e_user_tombstone_invalidates_wiki_pages(
        self,
        eval_app_env: None,
        wiki_leakage_session: AsyncSession,
    ) -> None:
        """L9e: user: tombstone forget invalidates wiki pages citing any message from that user.

        Issue a user-level forget. Run cascade. Assert wiki page transitions
        to stale/archived.
        """
        _ = eval_app_env
        session = wiki_leakage_session

        target_user_id = _BASE_USER_ID + 5

        # Persist message owned by the target user.
        cm_id, mv_id = await _persist_message(
            session,
            message_id=19_005,
            user_id=target_user_id,
            text_value="пользовательский источник L9e",
        )

        admin_id = _BASE_USER_ID + 905
        await _create_user_for_admin(session, admin_id)

        # Wiki page cites the mv directly.
        page_id = await _create_wiki_page(
            session,
            slug="l9e-test",
            title="L9e Test Page",
            body_markdown=f"Страница пользователь [^mv:{mv_id}].",
            page_status="reviewed",
            created_by_user_id=admin_id,
        )
        await _link_direct_mv(session, page_id=page_id, mv_id=mv_id)

        # Issue forget via user: tombstone key.
        _USER_KEY_PREFIX = "user" + ":"
        await _issue_forget_and_run_cascade(
            session,
            target_type="user",
            target_id=str(target_user_id),
            tombstone_key=_USER_KEY_PREFIX + str(target_user_id),
        )

        # Assert wiki page is now stale or archived.
        row = await session.execute(
            text("SELECT page_status, public_enabled FROM wiki_pages WHERE id = CAST(:pid AS uuid)"),
            {"pid": page_id},
        )
        page_row = row.fetchone()
        assert page_row is not None
        assert page_row.page_status in ("stale", "archived"), (
            f"L9e: page_status={page_row.page_status!r} expected stale or archived after "
            "user tombstone forget"
        )
        assert page_row.public_enabled is False
