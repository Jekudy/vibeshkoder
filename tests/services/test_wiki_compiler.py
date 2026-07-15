"""TDD contract for automatic, revision-based wiki compilation."""

from __future__ import annotations

import ast
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")
SOURCE_CHAT_ID = -100_131_002


class _FakeGateway:
    def __init__(self, *drafts: dict, mutation=None) -> None:
        self._drafts = deque(drafts)
        self.calls: list[dict] = []
        self.mutation = mutation

    async def revise_wiki_topic(self, session, **kwargs):
        self.calls.append(kwargs)
        if self.mutation is not None:
            await self.mutation(session)
        return self._drafts.popleft()


async def _make_user(session) -> int:
    from bot.db.models import User

    user_id = int(uuid.uuid4().int & 0x7FFFFFFF)
    session.add(
        User(
            id=user_id,
            username=f"wiki{user_id}",
            first_name="Wiki",
            is_member=True,
            is_admin=True,
        )
    )
    await session.flush()
    return user_id


async def _make_message(
    session,
    *,
    user_id: int,
    policy: str = "normal",
    chat_id: int = SOURCE_CHAT_ID,
) -> int:
    from bot.db.models import ChatMessage, MessageVersion

    message_id = int(uuid.uuid4().int & 0x7FFFFFFF)
    chat_message = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=f"source {message_id}",
        date=datetime.now(timezone.utc),
        raw_json={"text": f"source {message_id}"},
        memory_policy=policy,
        is_redacted=False,
    )
    session.add(chat_message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=chat_message.id,
        version_seq=1,
        text=chat_message.text,
        normalized_text=chat_message.text,
        entities_json={},
        content_hash=f"wiki-{uuid.uuid4().hex}",
        is_redacted=False,
    )
    session.add(version)
    await session.flush()
    chat_message.current_version_id = version.id
    await session.flush()
    return version.id


async def _make_card(session, *, actor_id: int, source_mvid: int) -> uuid.UUID:
    from bot.db.models import CardSource, KnowledgeCard

    card = KnowledgeCard(
        title="Durable decisions",
        body_markdown="The group selected a durable memory model.",
        card_status="approved",
        approved_by_user_id=actor_id,
        approved_at=datetime.now(timezone.utc),
    )
    session.add(card)
    await session.flush()
    session.add(CardSource(card_id=card.id, message_version_id=source_mvid, position=0))
    await session.flush()
    return card.id


async def _make_ledger(session, *, call_type: str = "wiki_compilation") -> int:
    from bot.db.models import LlmUsageLedger

    row = LlmUsageLedger(
        provider="test",
        model="test-model",
        prompt_hash="0" * 64,
        response_hash="1" * 64,
        tokens_in=1,
        tokens_out=1,
        cost_usd=0,
        latency_ms=1,
        cache_hit=False,
        call_type=call_type,
    )
    session.add(row)
    await session.flush()
    return row.id


async def test_compiler_creates_reviewed_page_revision_and_provenance(db_session) -> None:
    from bot.services.wiki_compiler import compile_topic_page

    actor_id = await _make_user(db_session)
    card_mvid = await _make_message(db_session, user_id=actor_id)
    direct_mvid = await _make_message(db_session, user_id=actor_id)
    card_id = await _make_card(db_session, actor_id=actor_id, source_mvid=card_mvid)
    ledger_id = await _make_ledger(db_session)
    body = f"# Memory\nDecision [^card:{card_id}]. Detail [^mv:{direct_mvid}]."
    gateway = _FakeGateway(
        {"title": "Community memory", "body_markdown": body, "llm_usage_ledger_id": ledger_id}
    )

    result = await compile_topic_page(
        db_session,
        slug="community-memory",
        title_hint="Community memory",
        source_card_ids=[card_id],
        source_message_version_ids=[direct_mvid],
        actor_user_id=actor_id,
        gateway=gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )

    assert result.changed is True
    assert result.revision_seq == 1
    assert result.llm_usage_ledger_id == ledger_id
    page = (
        await db_session.execute(
            text(
                "SELECT title, body_markdown, page_status, validation_status, "
                "visibility, public_enabled, robots_policy, reviewed_by_user_id "
                "FROM wiki_pages WHERE slug='community-memory'"
            )
        )
    ).one()
    assert tuple(page) == (
        "Community memory",
        body,
        "reviewed",
        "valid",
        "public_candidate",
        False,
        "noindex",
        actor_id,
    )
    revision = (
        await db_session.execute(
            text(
                "SELECT revision_seq, body_markdown, source_card_ids_snapshot, "
                "source_message_version_ids_snapshot FROM wiki_revisions "
                "WHERE wiki_page_id=:pid"
            ),
            {"pid": str(result.page_id)},
        )
    ).one()
    assert revision.revision_seq == 1
    assert revision.body_markdown == body
    assert revision.source_card_ids_snapshot == [str(card_id)]
    assert revision.source_message_version_ids_snapshot == sorted([card_mvid, direct_mvid])
    assert gateway.calls[0]["prior_body_markdown"] is None
    assert gateway.calls[0]["prior_revision_seq"] == 0


async def test_compiler_revises_existing_page_instead_of_replacing_history(db_session) -> None:
    from bot.services.wiki_compiler import compile_topic_page

    actor_id = await _make_user(db_session)
    mvid = await _make_message(db_session, user_id=actor_id)
    first_body = f"# Topic\nFirst fact [^mv:{mvid}]."
    second_body = f"# Topic\nFirst fact and a durable update [^mv:{mvid}]."
    first_ledger = await _make_ledger(db_session)
    second_ledger = await _make_ledger(db_session)
    gateway = _FakeGateway(
        {"title": "Topic", "body_markdown": first_body, "llm_usage_ledger_id": first_ledger},
        {"title": "Topic", "body_markdown": second_body, "llm_usage_ledger_id": second_ledger},
    )
    kwargs = {
        "slug": "topic",
        "title_hint": "Topic",
        "source_card_ids": [],
        "source_message_version_ids": [mvid],
        "actor_user_id": actor_id,
        "gateway": gateway,
        "publication_authorized": True,
        "source_chat_id": SOURCE_CHAT_ID,
    }

    first = await compile_topic_page(db_session, **kwargs)
    second = await compile_topic_page(db_session, **kwargs)

    assert first.page_id == second.page_id
    assert second.revision_seq == 2
    assert gateway.calls[1]["prior_body_markdown"] == first_body
    assert gateway.calls[1]["prior_revision_seq"] == 1
    rows = (
        await db_session.execute(
            text(
                "SELECT revision_seq, body_markdown FROM wiki_revisions "
                "WHERE wiki_page_id=:pid ORDER BY revision_seq"
            ),
            {"pid": str(first.page_id)},
        )
    ).all()
    assert [tuple(row) for row in rows] == [(1, first_body), (2, second_body)]


async def test_compiler_rejects_ungoverned_source_before_gateway(db_session) -> None:
    from bot.services.wiki_compiler import WikiSourceRejectedError, compile_topic_page

    actor_id = await _make_user(db_session)
    mvid = await _make_message(db_session, user_id=actor_id, policy="offrecord")
    gateway = _FakeGateway(
        {"title": "No", "body_markdown": f"Never [^mv:{mvid}].", "llm_usage_ledger_id": 1}
    )

    with pytest.raises(WikiSourceRejectedError, match="message_version"):
        await compile_topic_page(
            db_session,
            slug="blocked",
            title_hint="Blocked",
            source_card_ids=[],
            source_message_version_ids=[mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )

    assert gateway.calls == []
    assert (
        await db_session.execute(text("SELECT count(*) FROM wiki_pages WHERE slug='blocked'"))
    ).scalar_one() == 0


async def test_compiler_rejects_hallucinated_citation_without_writes(db_session) -> None:
    from bot.services.wiki_compiler import WikiCompilerContractError, compile_topic_page

    actor_id = await _make_user(db_session)
    mvid = await _make_message(db_session, user_id=actor_id)
    gateway = _FakeGateway(
        {"title": "Bad", "body_markdown": "Unsupported [^mv:999999999].", "llm_usage_ledger_id": 1}
    )

    with pytest.raises(WikiCompilerContractError, match="unsupported citation"):
        await compile_topic_page(
            db_session,
            slug="bad-citation",
            title_hint="Bad",
            source_card_ids=[],
            source_message_version_ids=[mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )

    assert (
        await db_session.execute(text("SELECT count(*) FROM wiki_pages WHERE slug='bad-citation'"))
    ).scalar_one() == 0


async def test_compiler_requires_explicit_publication_authorization(db_session) -> None:
    from bot.services.wiki_compiler import WikiCompilerContractError, compile_topic_page

    actor_id = await _make_user(db_session)
    mvid = await _make_message(db_session, user_id=actor_id)
    gateway = _FakeGateway(
        {"title": "No", "body_markdown": f"No [^mv:{mvid}].", "llm_usage_ledger_id": 1}
    )

    with pytest.raises(WikiCompilerContractError, match="publication authorization"):
        await compile_topic_page(
            db_session,
            slug="not-authorized",
            title_hint="No",
            source_card_ids=[],
            source_message_version_ids=[mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=False,
            source_chat_id=SOURCE_CHAT_ID,
        )

    assert gateway.calls == []


async def test_compiler_requires_direct_source_to_be_current_version(db_session) -> None:
    from bot.db.models import ChatMessage, MessageVersion
    from bot.services.wiki_compiler import WikiSourceRejectedError, compile_topic_page

    actor_id = await _make_user(db_session)
    old_mvid = await _make_message(db_session, user_id=actor_id)
    old = await db_session.get(MessageVersion, old_mvid)
    assert old is not None
    current = MessageVersion(
        chat_message_id=old.chat_message_id,
        version_seq=2,
        text="new current content",
        normalized_text="new current content",
        entities_json={},
        content_hash=f"wiki-{uuid.uuid4().hex}",
        is_redacted=False,
    )
    db_session.add(current)
    await db_session.flush()
    chat_message = await db_session.get(ChatMessage, old.chat_message_id)
    assert chat_message is not None
    chat_message.current_version_id = current.id
    await db_session.flush()
    gateway = _FakeGateway(
        {"title": "Stale", "body_markdown": f"Stale [^mv:{old_mvid}].", "llm_usage_ledger_id": 1}
    )

    with pytest.raises(WikiSourceRejectedError, match="message_version"):
        await compile_topic_page(
            db_session,
            slug="stale-version",
            title_hint="Stale",
            source_card_ids=[],
            source_message_version_ids=[old_mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )
    assert gateway.calls == []


@pytest.mark.parametrize("mutation_target", ["message", "card"])
async def test_compiler_refuses_source_content_mutation_during_gateway(
    db_session, mutation_target: str
) -> None:
    from bot.services.wiki_compiler import WikiConcurrentUpdateError, compile_topic_page

    actor_id = await _make_user(db_session)
    card_mvid = await _make_message(db_session, user_id=actor_id)
    direct_mvid = await _make_message(db_session, user_id=actor_id)
    card_id = await _make_card(db_session, actor_id=actor_id, source_mvid=card_mvid)
    ledger_id = await _make_ledger(db_session)

    async def mutate(session) -> None:
        if mutation_target == "message":
            await session.execute(
                text("UPDATE message_versions SET normalized_text='mutated' WHERE id=:id"),
                {"id": direct_mvid},
            )
        else:
            await session.execute(
                text("UPDATE knowledge_cards SET body_markdown='mutated' WHERE id=:id"),
                {"id": str(card_id)},
            )

    body = f"Topic [^card:{card_id}] and [^mv:{direct_mvid}]."
    gateway = _FakeGateway(
        {"title": "Topic", "body_markdown": body, "llm_usage_ledger_id": ledger_id},
        mutation=mutate,
    )

    with pytest.raises(WikiConcurrentUpdateError, match="source snapshot"):
        await compile_topic_page(
            db_session,
            slug=f"mutated-{mutation_target}",
            title_hint="Topic",
            source_card_ids=[card_id],
            source_message_version_ids=[direct_mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )


async def test_compiler_rejects_non_wiki_ledger_row(db_session) -> None:
    from bot.services.wiki_compiler import WikiCompilerContractError, compile_topic_page

    actor_id = await _make_user(db_session)
    mvid = await _make_message(db_session, user_id=actor_id)
    ledger_id = await _make_ledger(db_session, call_type="qa_synthesis")
    gateway = _FakeGateway(
        {
            "title": "Wrong ledger",
            "body_markdown": f"Fact [^mv:{mvid}].",
            "llm_usage_ledger_id": ledger_id,
        }
    )

    with pytest.raises(WikiCompilerContractError, match="wiki_compilation"):
        await compile_topic_page(
            db_session,
            slug="wrong-ledger",
            title_hint="Wrong ledger",
            source_card_ids=[],
            source_message_version_ids=[mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )


async def test_compiler_rejects_missing_ledger_row(db_session) -> None:
    from bot.services.wiki_compiler import WikiCompilerContractError, compile_topic_page

    actor_id = await _make_user(db_session)
    mvid = await _make_message(db_session, user_id=actor_id)
    gateway = _FakeGateway(
        {
            "title": "Missing ledger",
            "body_markdown": f"Fact [^mv:{mvid}].",
            "llm_usage_ledger_id": 9_999_999_999,
        }
    )

    with pytest.raises(WikiCompilerContractError, match="wiki_compilation"):
        await compile_topic_page(
            db_session,
            slug="missing-ledger",
            title_hint="Missing ledger",
            source_card_ids=[],
            source_message_version_ids=[mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )


async def test_compiler_rejects_foreign_chat_source_before_gateway(db_session) -> None:
    from bot.services.wiki_compiler import WikiSourceRejectedError, compile_topic_page

    actor_id = await _make_user(db_session)
    foreign_mvid = await _make_message(
        db_session,
        user_id=actor_id,
        chat_id=SOURCE_CHAT_ID - 1,
    )
    gateway = _FakeGateway(
        {
            "title": "Foreign",
            "body_markdown": f"Foreign [^mv:{foreign_mvid}].",
            "llm_usage_ledger_id": 1,
        }
    )

    with pytest.raises(WikiSourceRejectedError, match="message_version"):
        await compile_topic_page(
            db_session,
            slug="foreign-source",
            title_hint="Foreign",
            source_card_ids=[],
            source_message_version_ids=[foreign_mvid],
            actor_user_id=actor_id,
            gateway=gateway,
            publication_authorized=True,
            source_chat_id=SOURCE_CHAT_ID,
        )

    assert gateway.calls == []


async def test_compiler_never_overwrites_same_slug_page_from_another_chat(db_session) -> None:
    from bot.services.wiki_compiler import WikiSourceRejectedError, compile_topic_page

    actor_id = await _make_user(db_session)
    local_mvid = await _make_message(db_session, user_id=actor_id)
    first_ledger = await _make_ledger(db_session)
    first_body = f"Local [^mv:{local_mvid}]."
    first_gateway = _FakeGateway(
        {
            "title": "Local",
            "body_markdown": first_body,
            "llm_usage_ledger_id": first_ledger,
        }
    )
    await compile_topic_page(
        db_session,
        slug="shared-slug",
        title_hint="Local",
        source_card_ids=[],
        source_message_version_ids=[local_mvid],
        actor_user_id=actor_id,
        gateway=first_gateway,
        publication_authorized=True,
        source_chat_id=SOURCE_CHAT_ID,
    )
    foreign_chat_id = SOURCE_CHAT_ID - 1
    foreign_mvid = await _make_message(
        db_session,
        user_id=actor_id,
        chat_id=foreign_chat_id,
    )
    second_ledger = await _make_ledger(db_session)
    second_gateway = _FakeGateway(
        {
            "title": "Foreign",
            "body_markdown": f"Foreign [^mv:{foreign_mvid}].",
            "llm_usage_ledger_id": second_ledger,
        }
    )

    with pytest.raises(WikiSourceRejectedError, match="outside source_chat_id"):
        await compile_topic_page(
            db_session,
            slug="shared-slug",
            title_hint="Foreign",
            source_card_ids=[],
            source_message_version_ids=[foreign_mvid],
            actor_user_id=actor_id,
            gateway=second_gateway,
            publication_authorized=True,
            source_chat_id=foreign_chat_id,
        )

    assert second_gateway.calls == []
    assert (
        await db_session.scalar(
            text("SELECT body_markdown FROM wiki_pages WHERE slug='shared-slug'")
        )
        == first_body
    )


def test_compiler_has_no_provider_or_network_imports() -> None:
    path = Path("bot/services/wiki_compiler.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not any(
        name.startswith(("httpx", "requests", "openai", "anthropic", "bot.services.llm_providers"))
        for name in imported
    )
