"""Automatic card-topic compilation and static projection contracts."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")
SOURCE_CHAT_ID = -100_131_001


class _Gateway:
    def __init__(self, *, cited_card_id: uuid.UUID | None = None) -> None:
        self.calls: list[dict] = []
        self.ledger_ids: list[int] = []
        self.cited_card_id = cited_card_id

    async def revise_wiki_topic(self, session, **kwargs):
        from bot.db.models import LlmUsageLedger

        self.calls.append(kwargs)
        ledger = LlmUsageLedger(
            provider="test",
            model="test-model",
            prompt_hash=uuid.uuid4().hex.ljust(64, "0"),
            response_hash=uuid.uuid4().hex.ljust(64, "1"),
            tokens_in=1,
            tokens_out=1,
            cost_usd=Decimal("0"),
            latency_ms=1,
            cache_hit=False,
            call_type="wiki_compilation",
        )
        session.add(ledger)
        await session.flush()
        self.ledger_ids.append(ledger.id)
        citations = (
            f"[^card:{self.cited_card_id}]"
            if self.cited_card_id is not None
            else " ".join(f"[^card:{card['card_id']}]" for card in kwargs["source_cards"])
        )
        return {
            "title": kwargs["title_hint"],
            "body_markdown": f"# {kwargs['title_hint']}\n{citations}",
            "llm_usage_ledger_id": ledger.id,
        }


async def _make_user(session) -> int:
    from bot.db.models import User

    user_id = int(uuid.uuid4().int & 0x7FFFFFFF)
    session.add(
        User(
            id=user_id,
            username=f"wiki-auto-{user_id}",
            first_name="Wiki",
            is_member=True,
            is_admin=True,
        )
    )
    await session.flush()
    return user_id


async def _make_card(
    session,
    *,
    actor_id: int,
    topic_slug: str,
    title: str,
    chat_id: int = SOURCE_CHAT_ID,
) -> uuid.UUID:
    from bot.db.models import CardSource, ChatMessage, KnowledgeCard, MessageVersion

    message_id = int(uuid.uuid4().int & 0x7FFFFFFF)
    message = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=actor_id,
        text=f"source {message_id}",
        date=datetime.now(timezone.utc),
        raw_json={"text": f"source {message_id}"},
        memory_policy="normal",
        is_redacted=False,
    )
    session.add(message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text=message.text,
        normalized_text=message.text,
        entities_json={},
        content_hash=f"wiki-orchestrator-{uuid.uuid4().hex}",
        is_redacted=False,
    )
    session.add(version)
    await session.flush()
    message.current_version_id = version.id
    card = KnowledgeCard(
        title=title,
        body_markdown=f"{title} body",
        topic_slug=topic_slug,
        card_status="approved",
        approved_by_user_id=actor_id,
        approved_at=datetime.now(timezone.utc),
    )
    session.add(card)
    await session.flush()
    session.add(CardSource(card_id=card.id, message_version_id=version.id, position=0))
    await session.flush()
    return card.id


async def test_same_topic_revises_only_after_source_set_changes(db_session) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics

    actor_id = await _make_user(db_session)
    first_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="community-rules",
        title="Правила сообщества",
    )
    gateway = _Gateway()

    first = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    unchanged = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    second_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="community-rules",
        title="Новое правило",
    )
    second = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert first.compiled_topics == 1
    assert first.revisions[0].revision_seq == 1
    assert unchanged.compiled_topics == 0
    assert unchanged.unchanged_topics == 1
    assert second.compiled_topics == 1
    assert second.revisions[0].revision_seq == 2
    assert len(gateway.calls) == 2
    assert {uuid.UUID(card["card_id"]) for card in gateway.calls[0]["source_cards"]} == {first_card}
    assert {uuid.UUID(card["card_id"]) for card in gateway.calls[1]["source_cards"]} == {
        first_card,
        second_card,
    }


async def test_different_topics_create_distinct_public_candidate_pages(db_session) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics

    actor_id = await _make_user(db_session)
    await _make_card(db_session, actor_id=actor_id, topic_slug="events", title="События")
    await _make_card(db_session, actor_id=actor_id, topic_slug="projects", title="Проекты")

    result = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=_Gateway(),
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert result.topics_seen == 2
    assert result.compiled_topics == 2
    pages = (
        await db_session.execute(
            text(
                "SELECT slug, visibility, public_enabled, robots_policy "
                "FROM wiki_pages WHERE slug IN ('events','projects') ORDER BY slug"
            )
        )
    ).all()
    assert [tuple(row) for row in pages] == [
        ("events", "public_candidate", False, "noindex"),
        ("projects", "public_candidate", False, "noindex"),
    ]


async def test_changed_topics_can_be_committed_one_at_a_time(db_session) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics

    actor_id = await _make_user(db_session)
    await _make_card(db_session, actor_id=actor_id, topic_slug="alpha", title="Alpha")
    await _make_card(db_session, actor_id=actor_id, topic_slug="beta", title="Beta")
    gateway = _Gateway()

    first = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
        max_topics=1,
    )
    second = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
        max_topics=1,
    )

    assert first.compiled_topics == 1
    assert first.remaining_changed_topics == 1
    assert second.compiled_topics == 1
    assert second.remaining_changed_topics == 0
    assert [call["slug"] for call in gateway.calls] == ["alpha", "beta"]


async def test_static_export_acquires_and_uses_governed_snapshot_locks(
    db_session,
    tmp_path,
) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics, export_static_wiki

    actor_id = await _make_user(db_session)
    await _make_card(db_session, actor_id=actor_id, topic_slug="locked", title="Locked")
    await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=_Gateway(),
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    result = await export_static_wiki(
        db_session,
        publish_dir=tmp_path / "current",
        site_title="Shkoder Wiki",
        forbidden_origins=("187.77.98.73",),
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert result.page_count == 1
    assert result.generation_dir.is_dir()


async def test_publication_snapshot_locks_block_source_invalidation_until_upload_finishes(
    postgres_engine,
    tmp_path,
) -> None:
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.services.wiki_orchestrator import compile_changed_topics, export_static_wiki

    sessions = async_sessionmaker(
        bind=postgres_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with sessions() as setup:
        actor_id = await _make_user(setup)
        card_id = await _make_card(
            setup,
            actor_id=actor_id,
            topic_slug="publication-lock",
            title="Publication lock",
        )
        gateway = _Gateway()
        await compile_changed_topics(
            setup,
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )
        mvid = await setup.scalar(
            text("SELECT message_version_id FROM card_sources WHERE card_id=:card_id"),
            {"card_id": str(card_id)},
        )
        ledger_ids = gateway.ledger_ids
        await setup.commit()

    try:
        async with sessions() as publication_session:
            result = await export_static_wiki(
                publication_session,
                publish_dir=tmp_path / "locked-current",
                site_title="Shkoder Wiki",
                forbidden_origins=("187.77.98.73",),
                publication_authorized=True,
                source_chat_id=SOURCE_CHAT_ID,
            )
            assert result.page_count == 1

            async with sessions() as invalidation_session:
                await invalidation_session.execute(text("SET LOCAL lock_timeout='250ms'"))
                with pytest.raises(DBAPIError):
                    await invalidation_session.execute(
                        text("UPDATE message_versions SET is_redacted=true WHERE id=:id"),
                        {"id": mvid},
                    )
                await invalidation_session.rollback()
            await publication_session.rollback()
    finally:
        async with sessions() as cleanup, cleanup.begin():
            await cleanup.execute(text("DELETE FROM wiki_pages WHERE slug='publication-lock'"))
            await cleanup.execute(
                text("DELETE FROM knowledge_cards WHERE id=:card_id"),
                {"card_id": str(card_id)},
            )
            await cleanup.execute(
                text("DELETE FROM chat_messages WHERE user_id=:actor_id"),
                {"actor_id": actor_id},
            )
            if ledger_ids:
                await cleanup.execute(
                    text("DELETE FROM llm_usage_ledger WHERE id = ANY(:ids)"),
                    {"ids": ledger_ids},
                )
            await cleanup.execute(
                text("DELETE FROM users WHERE id=:actor_id"),
                {"actor_id": actor_id},
            )


async def test_static_loader_includes_only_governed_automatic_pages(db_session) -> None:
    from bot.services.wiki_orchestrator import (
        compile_changed_topics,
        load_static_wiki_pages,
    )

    actor_id = await _make_user(db_session)
    await _make_card(db_session, actor_id=actor_id, topic_slug="memory", title="Память")
    await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=_Gateway(),
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    await db_session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, visibility, public_enabled, "
            "robots_policy, validation_status, created_by_user_id, created_at, updated_at) VALUES "
            "(gen_random_uuid(), 'manual-member', 'Manual', 'Manual', 'reviewed', "
            "'member', false, 'noindex', 'valid', :actor_id, now(), now())"
        ),
        {"actor_id": actor_id},
    )

    pages = await load_static_wiki_pages(db_session, source_chat_id=SOURCE_CHAT_ID)

    assert [(page.slug, page.revision_seq) for page in pages] == [("memory", 1)]


async def test_static_loader_fails_closed_after_source_redaction(db_session) -> None:
    from bot.services.wiki_orchestrator import (
        WikiStaticSourceInvalidError,
        compile_changed_topics,
        load_static_wiki_pages,
    )

    actor_id = await _make_user(db_session)
    card_id = await _make_card(
        db_session, actor_id=actor_id, topic_slug="safety", title="Безопасность"
    )
    await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=_Gateway(),
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    await db_session.execute(
        text(
            "UPDATE message_versions SET is_redacted=true "
            "WHERE id=(SELECT message_version_id FROM card_sources WHERE card_id=:card_id)"
        ),
        {"card_id": str(card_id)},
    )

    with pytest.raises(WikiStaticSourceInvalidError, match="invalid publication sources"):
        await load_static_wiki_pages(db_session, source_chat_id=SOURCE_CHAT_ID)


async def test_static_loader_fails_closed_on_mixed_chat_live_sources(db_session) -> None:
    from bot.services.wiki_orchestrator import (
        WikiStaticSourceInvalidError,
        compile_changed_topics,
        load_static_wiki_pages,
    )

    actor_id = await _make_user(db_session)
    await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="mixed-publication",
        title="Mixed publication",
    )
    compiled = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=_Gateway(),
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    foreign_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="foreign-source",
        title="Foreign source",
        chat_id=SOURCE_CHAT_ID - 1,
    )
    foreign_mvid = await db_session.scalar(
        text("SELECT message_version_id FROM card_sources WHERE card_id=:card_id"),
        {"card_id": str(foreign_card)},
    )
    await db_session.execute(
        text(
            "INSERT INTO wiki_page_message_sources "
            "(wiki_page_id, message_version_id, position) VALUES (:page_id, :mvid, 0)"
        ),
        {"page_id": str(compiled.revisions[0].page_id), "mvid": int(foreign_mvid)},
    )

    with pytest.raises(WikiStaticSourceInvalidError, match="invalid publication sources"):
        await load_static_wiki_pages(
            db_session,
            source_chat_id=SOURCE_CHAT_ID,
        )


async def test_compile_requires_existing_automation_actor(db_session) -> None:
    from bot.services.wiki_orchestrator import (
        WikiOrchestrationError,
        compile_changed_topics,
    )

    with pytest.raises(WikiOrchestrationError, match="actor user does not exist"):
        await compile_changed_topics(
            db_session,
            actor_user_id=2_147_483_646,
            gateway=_Gateway(),
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )


async def test_full_input_snapshot_prevents_subset_citation_recompile_loop(db_session) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics

    actor_id = await _make_user(db_session)
    cited_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="exactly-once",
        title="Exactly once",
    )
    second_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="exactly-once",
        title="Second input",
    )
    gateway = _Gateway(cited_card_id=cited_card)

    first = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    unchanged = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    third_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="exactly-once",
        title="Third input",
    )
    revised = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    stable_again = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert first.revisions[0].revision_seq == 1
    assert unchanged.compiled_topics == 0
    assert revised.revisions[0].revision_seq == 2
    assert stable_again.compiled_topics == 0
    assert len(gateway.calls) == 2
    page_id = first.revisions[0].page_id
    revision_rows = (
        await db_session.execute(
            text(
                "SELECT revision_seq, source_card_ids_snapshot "
                "FROM wiki_revisions WHERE wiki_page_id=:page_id ORDER BY revision_seq"
            ),
            {"page_id": str(page_id)},
        )
    ).all()
    assert set(revision_rows[0].source_card_ids_snapshot) == {
        str(cited_card),
        str(second_card),
    }
    assert set(revision_rows[1].source_card_ids_snapshot) == {
        str(cited_card),
        str(second_card),
        str(third_card),
    }
    live_cards = set(
        await db_session.scalars(
            text("SELECT card_id FROM wiki_page_card_sources WHERE wiki_page_id=:page_id"),
            {"page_id": str(page_id)},
        )
    )
    assert live_cards == {cited_card}


async def test_mixed_chat_topic_is_staled_without_another_provider_call(db_session) -> None:
    from bot.db.models import CardSource
    from bot.services.wiki_orchestrator import compile_changed_topics, load_static_wiki_pages

    actor_id = await _make_user(db_session)
    local_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="chat-scope",
        title="Chat scope",
    )
    gateway = _Gateway()
    await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    foreign_card = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="foreign-discard",
        title="Foreign",
        chat_id=SOURCE_CHAT_ID - 1,
    )
    foreign_mvid = await db_session.scalar(
        text("SELECT message_version_id FROM card_sources WHERE card_id=:card_id"),
        {"card_id": str(foreign_card)},
    )
    await db_session.execute(
        text("UPDATE knowledge_cards SET card_status='archived' WHERE id=:card_id"),
        {"card_id": str(foreign_card)},
    )
    db_session.add(CardSource(card_id=local_card, message_version_id=int(foreign_mvid), position=1))
    await db_session.flush()

    result = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert result.compiled_topics == 0
    assert result.stale_topics == 1
    assert len(gateway.calls) == 1
    page_status = await db_session.execute(
        text("SELECT page_status, validation_status FROM wiki_pages WHERE slug='chat-scope'")
    )
    assert tuple(page_status.one()) == ("stale", "stale")
    assert (
        await load_static_wiki_pages(
            db_session,
            source_chat_id=SOURCE_CHAT_ID,
        )
        == []
    )


async def test_missing_topic_stales_only_automatic_page_and_new_input_restores_it(
    db_session,
) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics, load_static_wiki_pages

    actor_id = await _make_user(db_session)
    card_id = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="restorable",
        title="Restorable",
    )
    gateway = _Gateway()
    first = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    manual_page_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, visibility, public_enabled, "
            "robots_policy, validation_status, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, 'manual-owned', 'Manual', 'Manual body', 'reviewed', "
            "'public_candidate', false, 'noindex', 'valid', :actor, now(), now())"
        ),
        {"id": str(manual_page_id), "actor": actor_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO wiki_revisions "
            "(wiki_page_id, revision_seq, body_markdown, revision_status, "
            "source_card_ids_snapshot, source_message_version_ids_snapshot, "
            "edited_by_user_id, edit_reason, created_at) "
            "VALUES (:id, 1, 'Manual body', 'active', '[]'::jsonb, '[]'::jsonb, "
            ":actor, 'manual edit', now())"
        ),
        {"id": str(manual_page_id), "actor": actor_id},
    )
    await db_session.execute(
        text("UPDATE knowledge_cards SET card_status='archived' WHERE id=:card_id"),
        {"card_id": str(card_id)},
    )

    stale = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert stale.stale_topics == 1
    assert (
        await load_static_wiki_pages(
            db_session,
            source_chat_id=SOURCE_CHAT_ID,
        )
        == []
    )
    statuses = (
        await db_session.execute(
            text(
                "SELECT slug, page_status FROM wiki_pages "
                "WHERE slug IN ('restorable', 'manual-owned') ORDER BY slug"
            )
        )
    ).all()
    assert [tuple(row) for row in statuses] == [
        ("manual-owned", "reviewed"),
        ("restorable", "stale"),
    ]

    replacement = await _make_card(
        db_session,
        actor_id=actor_id,
        topic_slug="restorable",
        title="Restored",
    )
    restored = await compile_changed_topics(
        db_session,
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert restored.revisions[0].page_id == first.revisions[0].page_id
    assert restored.revisions[0].revision_seq == 2
    assert (
        await db_session.scalar(text("SELECT page_status FROM wiki_pages WHERE slug='restorable'"))
        == "reviewed"
    )
    assert {uuid.UUID(card["card_id"]) for card in gateway.calls[-1]["source_cards"]} == {
        replacement
    }


async def test_automatic_entry_points_reject_zero_chat_scope(db_session) -> None:
    from bot.services.wiki_orchestrator import compile_changed_topics, load_static_wiki_pages

    with pytest.raises(ValueError, match="source_chat_id"):
        await compile_changed_topics(
            db_session,
            actor_user_id=1,
            gateway=_Gateway(),
            publication_authorized=True,
            source_chat_id=0,
        )
    with pytest.raises(ValueError, match="source_chat_id"):
        await load_static_wiki_pages(db_session, source_chat_id=0)
