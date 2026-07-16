"""PostgreSQL integration and leakage tests for the semantic index."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import delete, select

from bot.db.models import (
    CardSource,
    ChatMessage,
    ForgetEvent,
    KnowledgeCard,
    LlmUsageLedger,
    MessageMedia,
    MessageVersion,
    SemanticIndexRun,
    SemanticRetrievalUnit,
    SemanticRetrievalUnitSource,
    User,
)
from bot.services.llm_gateway import EmbeddingGatewayConfig
from bot.services.llm_providers.openai_embeddings import EmbeddingResult
from bot.services.search import SearchHit, search_messages
from bot.services.semantic_index import (
    _index_batch,
    _reconcile_ineligible_units,
    _with_conversation_roots,
    backfill_semantic_index,
    hybrid_search,
    list_eligible_card_documents,
    list_eligible_message_documents,
    reciprocal_rank_fusion,
    vector_search,
)


CHAT_ID = -1_004_040_001
OTHER_CHAT_ID = -1_004_040_002
CONFIG = EmbeddingGatewayConfig(
    model="text-embedding-3-small",
    dimensions=1536,
    daily_ceiling_usd=Decimal("100"),
    monthly_ceiling_usd=Decimal("1000"),
)
VECTOR_X = (1.0,) + (0.0,) * 1535
VECTOR_Y = (0.0, 1.0) + (0.0,) * 1534


@pytest.fixture(autouse=True)
def _semantic_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow the canonical forget-lock helper to import runtime settings."""

    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("DEV_MODE", "true")


@dataclass(frozen=True, slots=True)
class CreatedMessage:
    message: ChatMessage
    version: MessageVersion


class CountingEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, *, inputs, model: str, dimensions: int) -> EmbeddingResult:
        values = tuple(inputs)
        self.calls.append(values)
        return EmbeddingResult(
            vectors=tuple(
                VECTOR_X if index % 2 == 0 else VECTOR_Y for index, _ in enumerate(values)
            ),
            tokens_in=sum(len(value) for value in values),
            request_id=f"semantic-test-{len(self.calls)}",
            raw_latency_ms=1,
        )


async def _user(
    session,
    *,
    user_id: int,
    is_bot: bool | None,
    is_admin: bool = False,
) -> User:
    row = User(
        id=user_id,
        username=f"semantic_{user_id}",
        first_name="Semantic",
        last_name=None,
        is_member=True,
        is_admin=is_admin,
        is_bot=is_bot,
    )
    session.add(row)
    await session.flush()
    return row


async def _message(
    session,
    *,
    user_id: int,
    message_id: int,
    text: str,
    chat_id: int = CHAT_ID,
    memory_policy: str = "normal",
    message_kind: str = "text",
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    chat_redacted: bool = False,
    version_redacted: bool = False,
) -> CreatedMessage:
    now = datetime.now(timezone.utc)
    message = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=now,
        memory_policy=memory_policy,
        message_kind=message_kind,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
        is_redacted=chat_redacted,
    )
    session.add(message)
    await session.flush()
    version = MessageVersion(
        chat_message_id=message.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        content_hash=f"semantic-{uuid.uuid4().hex}",
        is_redacted=version_redacted,
    )
    session.add(version)
    await session.flush()
    message.current_version_id = version.id
    await session.flush()
    return CreatedMessage(message=message, version=version)


def _evidence_bundle(source: CreatedMessage):
    from bot.services.evidence import EvidenceBundle, EvidenceItem

    now = datetime.now(timezone.utc)
    return EvidenceBundle(
        query="delivery lock test",
        chat_id=source.message.chat_id,
        items=(
            EvidenceItem(
                message_version_id=source.version.id,
                chat_message_id=source.message.id,
                chat_id=source.message.chat_id,
                message_id=source.message.message_id,
                user_id=source.message.user_id,
                snippet="governed evidence",
                ts_rank=1.0,
                captured_at=now,
                message_date=source.message.date,
            ),
        ),
        abstained=False,
        created_at=now,
    )


async def _approved_card(
    session,
    *,
    admin_id: int,
    source_ids: tuple[int, ...],
    title: str,
    status: str = "approved",
) -> KnowledgeCard:
    approved = status == "approved"
    card = KnowledgeCard(
        title=title,
        body_markdown=f"{title}: подтвержденное знание",
        card_status=status,
        approved_by_user_id=admin_id if approved else None,
        approved_at=datetime.now(timezone.utc) if approved else None,
    )
    session.add(card)
    await session.flush()
    for position, message_version_id in enumerate(source_ids):
        session.add(
            CardSource(
                card_id=card.id,
                message_version_id=message_version_id,
                position=position,
            )
        )
    await session.flush()
    return card


async def _ledger(session, *, suffix: str) -> LlmUsageLedger:
    row = LlmUsageLedger(
        provider="openai",
        model="text-embedding-3-small",
        prompt_hash=(suffix * 64)[:64],
        response_hash=(suffix * 64)[:64],
        tokens_in=1,
        tokens_out=0,
        cost_usd=Decimal("0"),
        latency_ms=1,
        cache_hit=False,
        call_type="semantic_embedding",
    )
    session.add(row)
    await session.flush()
    return row


async def _unit(
    session,
    *,
    source: CreatedMessage,
    vector: tuple[float, ...],
    ledger_id: int,
) -> SemanticRetrievalUnit:
    unit = SemanticRetrievalUnit(
        source_type="message",
        source_id=str(source.version.id),
        source_revision="v1:media:none",
        chat_id=source.message.chat_id,
        content_hash=source.version.content_hash,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_model_version="text-embedding-3-small",
        embedding_dimensions=1536,
        embedding=list(vector),
        llm_usage_ledger_id=ledger_id,
    )
    session.add(unit)
    await session.flush()
    session.add(
        SemanticRetrievalUnitSource(
            unit_id=unit.id,
            message_version_id=source.version.id,
            position=0,
        )
    )
    await session.flush()
    return unit


@pytest.mark.asyncio
async def test_message_and_card_eligibility_fail_closed_without_leakage(db_session) -> None:
    human = await _user(db_session, user_id=8_044_041_001, is_bot=False)
    bot = await _user(db_session, user_id=8_044_041_002, is_bot=True)
    unknown = await _user(db_session, user_id=8_044_041_003, is_bot=None)
    admin = await _user(db_session, user_id=8_044_041_004, is_bot=False, is_admin=True)

    eligible = await _message(
        db_session,
        user_id=human.id,
        message_id=4041001,
        text="человеческое текущее знание",
    )
    media = MessageMedia(
        chat_message_id=eligible.message.id,
        media_kind="photo",
        source_message_url="https://t.me/c/404/1",
        description="готовое описание изображения",
        description_status="ready",
        description_model="gpt-5-nano",
    )
    db_session.add(media)
    bot_message = await _message(
        db_session, user_id=bot.id, message_id=4041002, text="секрет от бота"
    )
    await _message(db_session, user_id=unknown.id, message_id=4041003, text="неизвестный автор")
    await _message(
        db_session,
        user_id=human.id,
        message_id=4041004,
        text="offrecord секрет",
        memory_policy="offrecord",
    )
    await _message(
        db_session,
        user_id=human.id,
        message_id=4041005,
        text="голосовая расшифровка",
        message_kind="voice",
    )
    await _message(
        db_session,
        user_id=human.id,
        message_id=4041006,
        text="редактировано",
        chat_redacted=True,
    )
    await _message(
        db_session,
        user_id=human.id,
        message_id=4041007,
        text="версия редактирована",
        version_redacted=True,
    )
    forgotten = await _message(
        db_session, user_id=human.id, message_id=4041008, text="ожидает удаления"
    )
    db_session.add(
        ForgetEvent(
            target_type="message",
            target_id=str(forgotten.message.id),
            actor_user_id=human.id,
            authorized_by="self",
            tombstone_key=f"message:{CHAT_ID}:{forgotten.message.message_id}",
            status="pending",
        )
    )
    foreign = await _message(
        db_session,
        user_id=human.id,
        message_id=4041009,
        text="другой чат",
        chat_id=OTHER_CHAT_ID,
    )
    stale_parent = await _message(
        db_session, user_id=human.id, message_id=4041010, text="старая редакция"
    )
    current = MessageVersion(
        chat_message_id=stale_parent.message.id,
        version_seq=2,
        text="текущая редакция",
        normalized_text="текущая редакция",
        content_hash=f"semantic-{uuid.uuid4().hex}",
        is_redacted=False,
    )
    db_session.add(current)
    await db_session.flush()
    stale_parent.message.current_version_id = current.id
    approved = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(eligible.version.id,),
        title="Одобренная карточка",
    )
    await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(eligible.version.id,),
        title="Черновик",
        status="draft",
    )
    await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(bot_message.version.id,),
        title="Карточка бота",
    )
    await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(eligible.version.id, foreign.version.id),
        title="Карточка из разных чатов",
    )
    await db_session.flush()

    message_documents = await list_eligible_message_documents(
        db_session, chat_id=CHAT_ID, limit=100
    )
    card_documents = await list_eligible_card_documents(db_session, chat_id=CHAT_ID, limit=100)

    assert {document.source_id for document in message_documents} == {
        str(eligible.version.id),
        str(current.id),
    }
    eligible_document = next(
        document for document in message_documents if document.source_id == str(eligible.version.id)
    )
    assert "готовое описание изображения" in eligible_document.canonical_text
    assert str(stale_parent.version.id) not in {
        document.source_id for document in message_documents
    }
    assert str(foreign.version.id) not in {document.source_id for document in message_documents}
    assert [document.source_id for document in card_documents] == [str(approved.id)]
    assert card_documents[0].message_version_ids == (eligible.version.id,)


@pytest.mark.asyncio
async def test_unchanged_edit_copies_prior_vector_without_provider_and_preserves_audit(
    db_session,
) -> None:
    human = await _user(db_session, user_id=8_044_042_001, is_bot=False)
    source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042001,
        text="первая редакция проекта",
    )
    provider = CountingEmbeddingProvider()
    first_documents = await list_eligible_message_documents(db_session, chat_id=CHAT_ID)

    first_result = await _index_batch(
        db_session,
        documents=first_documents,
        config=CONFIG,
        provider=provider,
    )
    assert (first_result.indexed, first_result.skipped) == (1, 0)
    assert first_result.reason_counts == {"indexed:new_embedding": 1}
    rerun_result = await _index_batch(
        db_session,
        documents=first_documents,
        config=CONFIG,
        provider=provider,
    )
    assert (rerun_result.indexed, rerun_result.skipped) == (0, 1)
    assert rerun_result.reason_counts == {"skipped:unchanged": 1}
    assert len(provider.calls) == 1

    edited = MessageVersion(
        chat_message_id=source.message.id,
        version_seq=2,
        text="первая редакция проекта",
        normalized_text="первая редакция проекта",
        content_hash=f"semantic-{uuid.uuid4().hex}",
        is_redacted=False,
    )
    db_session.add(edited)
    await db_session.flush()
    source.message.current_version_id = edited.id
    await db_session.flush()
    edited_documents = await list_eligible_message_documents(db_session, chat_id=CHAT_ID)

    assert [document.source_id for document in edited_documents] == [str(edited.id)]
    edit_result = await _index_batch(
        db_session,
        documents=edited_documents,
        config=CONFIG,
        provider=provider,
    )
    assert (edit_result.indexed, edit_result.skipped) == (1, 0)
    assert edit_result.reason_counts == {"indexed:reused_embedding": 1}
    assert len(provider.calls) == 1
    units = (
        (await db_session.execute(select(SemanticRetrievalUnit).order_by(SemanticRetrievalUnit.id)))
        .scalars()
        .all()
    )
    assert len(units) == 2
    assert units[0].source_id == str(source.version.id)
    assert units[0].invalidation_reason == "source_revised"
    assert units[0].invalidated_at is not None
    assert units[1].source_id == str(edited.id)
    assert units[1].invalidated_at is None
    assert tuple(units[1].embedding) == tuple(units[0].embedding)
    assert units[1].embedding_provider == units[0].embedding_provider
    assert units[1].embedding_model_version == units[0].embedding_model_version
    assert units[1].llm_usage_ledger_id == units[0].llm_usage_ledger_id
    source_links = (
        (
            await db_session.execute(
                select(SemanticRetrievalUnitSource).where(
                    SemanticRetrievalUnitSource.unit_id == units[1].id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [link.message_version_id for link in source_links] == [edited.id]


@pytest.mark.asyncio
async def test_backfill_persists_exact_reason_counts(postgres_engine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().int % 1_000_000
    user_id = 8_044_042_051 + suffix
    chat_id = -9_044_042_051 - suffix
    run_ids: set[int] = set()
    ledger_ids: set[int] = set()
    try:
        async with factory() as session:
            human = await _user(session, user_id=user_id, is_bot=False)
            await _message(
                session,
                user_id=human.id,
                message_id=4042051 + suffix,
                text="persisted backfill reasons",
                chat_id=chat_id,
            )
            await session.commit()
            provider = CountingEmbeddingProvider()

            first = await backfill_semantic_index(
                session,
                config=CONFIG,
                provider=provider,
                chat_id=chat_id,
            )
            run_ids.add(first.run_id)
            first_run = await session.get(SemanticIndexRun, first.run_id)
            assert first.reason_counts == {"indexed:new_embedding": 1}
            assert first_run is not None
            assert first_run.reason_counts == first.reason_counts

            rerun = await backfill_semantic_index(
                session,
                config=CONFIG,
                provider=provider,
                chat_id=chat_id,
            )
            run_ids.add(rerun.run_id)
            rerun_row = await session.get(SemanticIndexRun, rerun.run_id)
            assert rerun.reason_counts == {"skipped:unchanged": 1}
            assert rerun_row is not None
            assert rerun_row.reason_counts == rerun.reason_counts
            assert len(provider.calls) == 1
    finally:
        async with factory() as cleanup:
            ledger_ids.update(
                int(value)
                for value in (
                    await cleanup.execute(
                        select(SemanticRetrievalUnit.llm_usage_ledger_id).where(
                            SemanticRetrievalUnit.chat_id == chat_id
                        )
                    )
                ).scalars()
            )
            await cleanup.execute(
                delete(SemanticRetrievalUnit).where(SemanticRetrievalUnit.chat_id == chat_id)
            )
            if run_ids:
                await cleanup.execute(
                    delete(SemanticIndexRun).where(SemanticIndexRun.id.in_(run_ids))
                )
            await cleanup.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
            if ledger_ids:
                await cleanup.execute(
                    delete(LlmUsageLedger).where(LlmUsageLedger.id.in_(ledger_ids))
                )
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


@pytest.mark.asyncio
async def test_failed_backfill_accounts_for_every_eligible_document(postgres_engine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    class FailingEmbeddingProvider:
        async def embed(self, *, inputs, model: str, dimensions: int) -> EmbeddingResult:
            raise ValueError("provider contract failure")

    async with postgres_engine.connect() as connection:
        outer_transaction = await connection.begin()
        sessions = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with sessions() as session:
                human = await _user(session, user_id=8_044_042_071, is_bot=False)
                for offset in range(2):
                    await _message(
                        session,
                        user_id=human.id,
                        message_id=4042071 + offset,
                        text=f"failed batch document {offset}",
                    )

                with pytest.raises(ValueError, match="provider contract failure"):
                    await backfill_semantic_index(
                        session,
                        config=CONFIG,
                        provider=FailingEmbeddingProvider(),
                        chat_id=CHAT_ID,
                    )

                run = (
                    (
                        await session.execute(
                            select(SemanticIndexRun).order_by(SemanticIndexRun.id.desc())
                        )
                    )
                    .scalars()
                    .first()
                )
                assert run is not None
                assert run.status == "failed"
                assert run.eligible_count == 2
                assert run.indexed_count == 0
                assert run.skipped_count == 0
                assert run.failed_count == 2
                assert run.reason_counts == {"failed:ValueError": 2}
                assert (
                    run.eligible_count == run.indexed_count + run.skipped_count + run.failed_count
                )
        finally:
            await outer_transaction.rollback()


@pytest.mark.asyncio
async def test_same_card_hash_reuses_vector_and_refreshes_revision_and_provenance(
    db_session,
) -> None:
    human = await _user(db_session, user_id=8_044_042_101, is_bot=False)
    admin = await _user(db_session, user_id=8_044_042_102, is_bot=False, is_admin=True)
    first_source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042101,
        text="первый источник без изменения карточки",
    )
    second_source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042102,
        text="второй источник без изменения карточки",
    )
    card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(first_source.version.id,),
        title="Неизменное каноническое знание",
    )
    first_document = (await list_eligible_card_documents(db_session, chat_id=CHAT_ID))[0]
    first_provider = CountingEmbeddingProvider()
    first_result = await _index_batch(
        db_session,
        documents=[first_document],
        config=CONFIG,
        provider=first_provider,
    )
    assert (first_result.indexed, first_result.skipped) == (1, 0)

    old_source_link = (
        await db_session.execute(select(CardSource).where(CardSource.card_id == card.id))
    ).scalar_one()
    await db_session.delete(old_source_link)
    db_session.add(
        CardSource(
            card_id=card.id,
            message_version_id=second_source.version.id,
            position=0,
        )
    )
    card.updated_at = card.updated_at + timedelta(seconds=1)
    await db_session.flush()
    second_document = (await list_eligible_card_documents(db_session, chat_id=CHAT_ID))[0]
    assert second_document.content_hash == first_document.content_hash
    assert second_document.source_revision != first_document.source_revision
    assert second_document.message_version_ids == (second_source.version.id,)

    zero_call_provider = CountingEmbeddingProvider()
    reused_result = await _index_batch(
        db_session,
        documents=[second_document],
        config=CONFIG,
        provider=zero_call_provider,
    )
    assert (reused_result.indexed, reused_result.skipped) == (1, 0)
    assert reused_result.reason_counts == {"indexed:reused_embedding": 1}
    assert zero_call_provider.calls == []

    units = (
        (
            await db_session.execute(
                select(SemanticRetrievalUnit).where(
                    SemanticRetrievalUnit.source_type == "card",
                    SemanticRetrievalUnit.source_id == str(card.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(units) == 1
    assert units[0].source_revision == second_document.source_revision
    sources = (
        (
            await db_session.execute(
                select(SemanticRetrievalUnitSource).where(
                    SemanticRetrievalUnitSource.unit_id == units[0].id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [source.message_version_id for source in sources] == [second_source.version.id]


@pytest.mark.asyncio
async def test_pending_forget_created_during_embedding_prevents_unit_store(db_session) -> None:
    human = await _user(db_session, user_id=8_044_042_201, is_bot=False)
    source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042201,
        text="контент забывается во время embedding",
    )
    documents = await list_eligible_message_documents(db_session, chat_id=CHAT_ID)

    class ForgetBeforeStoreProvider(CountingEmbeddingProvider):
        async def embed(self, *, inputs, model: str, dimensions: int) -> EmbeddingResult:
            db_session.add(
                ForgetEvent(
                    target_type="message",
                    target_id=str(source.message.id),
                    actor_user_id=human.id,
                    authorized_by="self",
                    tombstone_key=f"message:{CHAT_ID}:{source.message.message_id}",
                    status="pending",
                )
            )
            await db_session.flush()
            return await super().embed(inputs=inputs, model=model, dimensions=dimensions)

    provider = ForgetBeforeStoreProvider()
    result = await _index_batch(
        db_session,
        documents=documents,
        config=CONFIG,
        provider=provider,
    )
    assert (result.indexed, result.skipped) == (0, 1)
    assert result.reason_counts == {"skipped:governance_race": 1}
    assert len(provider.calls) == 1
    assert (await db_session.execute(select(SemanticRetrievalUnit.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_forget_that_wins_source_lock_blocks_stale_text_before_provider(
    postgres_engine,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.services.advisory_locks import hold_session_advisory_locks
    from bot.services.forget_cascade import _p6_mvid_advisory_lock_id

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().int % 1_000_000
    user_id = 8_044_042_210 + suffix
    chat_id = -9_044_042_210 - suffix
    provider = CountingEmbeddingProvider()
    try:
        async with factory() as setup:
            human = await _user(setup, user_id=user_id, is_bot=False)
            source = await _message(
                setup,
                user_id=human.id,
                message_id=4042210 + suffix,
                text="этот текст нельзя отправлять после forget",
                chat_id=chat_id,
            )
            await setup.commit()

        async with factory() as index_session:
            stale_documents = await list_eligible_message_documents(
                index_session,
                chat_id=chat_id,
            )
            assert len(stale_documents) == 1
            lock_id = _p6_mvid_advisory_lock_id(source.version.id)

            async with factory() as forget_session:
                async with hold_session_advisory_locks(forget_session, (lock_id,)):
                    forget_session.add(
                        ForgetEvent(
                            target_type="message",
                            target_id=str(source.message.id),
                            actor_user_id=human.id,
                            authorized_by="self",
                            tombstone_key=f"message:{chat_id}:{source.message.message_id}",
                            status="pending",
                        )
                    )
                    await forget_session.commit()
                    index_task = asyncio.create_task(
                        _index_batch(
                            index_session,
                            documents=stale_documents,
                            config=CONFIG,
                            provider=provider,
                        )
                    )
                    await asyncio.sleep(0.1)
                    assert index_task.done() is False
                    assert provider.calls == []

            result = await asyncio.wait_for(index_task, timeout=2)

        assert (result.indexed, result.skipped) == (0, 1)
        assert result.reason_counts == {"skipped:governance_race": 1}
        assert provider.calls == []
        async with factory() as verification:
            assert (
                await verification.execute(
                    select(SemanticRetrievalUnit.id).where(SemanticRetrievalUnit.chat_id == chat_id)
                )
            ).scalar_one_or_none() is None
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                delete(ForgetEvent).where(ForgetEvent.target_id == str(source.message.id))
            )
            await cleanup.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


@pytest.mark.asyncio
async def test_concurrent_winner_is_reported_as_conflict_not_unchanged(db_session) -> None:
    human = await _user(db_session, user_id=8_044_042_221, is_bot=False)
    source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042221,
        text="конкурирующая индексация",
    )
    document = (await list_eligible_message_documents(db_session, chat_id=CHAT_ID))[0]
    winner_ledger = await _ledger(db_session, suffix="winner")

    class ConcurrentWinnerProvider(CountingEmbeddingProvider):
        async def embed(self, *, inputs, model: str, dimensions: int) -> EmbeddingResult:
            winner = SemanticRetrievalUnit(
                source_type=document.source_type,
                source_id=document.source_id,
                source_revision=document.source_revision,
                chat_id=document.chat_id,
                content_hash=document.content_hash,
                embedding_provider="openai",
                embedding_model=model,
                embedding_model_version="text-embedding-3-small",
                embedding_dimensions=dimensions,
                embedding=list(VECTOR_X),
                llm_usage_ledger_id=winner_ledger.id,
            )
            db_session.add(winner)
            await db_session.flush()
            db_session.add(
                SemanticRetrievalUnitSource(
                    unit_id=winner.id,
                    message_version_id=source.version.id,
                    position=0,
                )
            )
            await db_session.flush()
            return await super().embed(inputs=inputs, model=model, dimensions=dimensions)

    provider = ConcurrentWinnerProvider()
    result = await _index_batch(
        db_session,
        documents=[document],
        config=CONFIG,
        provider=provider,
    )

    assert (result.indexed, result.skipped) == (0, 1)
    assert result.reason_counts == {"skipped:conflict": 1}
    assert len(provider.calls) == 1
    assert len((await db_session.execute(select(SemanticRetrievalUnit.id))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_two_session_backfill_serializes_before_paid_provider(postgres_engine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().int % 1_000_000
    user_id = 8_044_043_000 + suffix
    chat_id = -9_044_043_000 - suffix
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    class BarrierProvider(CountingEmbeddingProvider):
        async def embed(self, *, inputs, model: str, dimensions: int) -> EmbeddingResult:
            self.calls.append(tuple(inputs))
            first_entered.set()
            await release_first.wait()
            return EmbeddingResult(
                vectors=(VECTOR_X,),
                tokens_in=sum(len(value) for value in inputs),
                request_id="serialized-backfill",
                raw_latency_ms=1,
            )

    provider = BarrierProvider()
    ledger_ids: set[int] = set()
    try:
        async with factory() as setup:
            await _user(setup, user_id=user_id, is_bot=False)
            await _message(
                setup,
                user_id=user_id,
                message_id=4043000 + suffix,
                text="serialized paid embedding",
                chat_id=chat_id,
            )
            await setup.commit()

        async def run_once():
            async with factory() as session:
                documents = await list_eligible_message_documents(
                    session,
                    chat_id=chat_id,
                )
                return await _index_batch(
                    session,
                    documents=documents,
                    config=CONFIG,
                    provider=provider,
                )

        first_task = asyncio.create_task(run_once())
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        second_task = asyncio.create_task(run_once())
        await asyncio.sleep(0.1)
        assert len(provider.calls) == 1
        release_first.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert sorted((first.indexed, second.indexed)) == [0, 1]
        assert sorted((first.skipped, second.skipped)) == [0, 1]
        assert len(provider.calls) == 1
        async with factory() as verification:
            units = (
                (
                    await verification.execute(
                        select(SemanticRetrievalUnit).where(
                            SemanticRetrievalUnit.chat_id == chat_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(units) == 1
            ledger_ids = {unit.llm_usage_ledger_id for unit in units}
            assert len(ledger_ids) == 1
    finally:
        release_first.set()
        async with factory() as cleanup:
            await cleanup.execute(
                delete(SemanticRetrievalUnit).where(SemanticRetrievalUnit.chat_id == chat_id)
            )
            from bot.db.models import ChatMessage

            await cleanup.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
            if ledger_ids:
                await cleanup.execute(
                    delete(LlmUsageLedger).where(LlmUsageLedger.id.in_(ledger_ids))
                )
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


@pytest.mark.asyncio
async def test_delivery_scope_blocks_pending_forget_creation(postgres_engine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.db.models import ForgetEvent
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.llm_gateway import hold_evidence_delivery_locks

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().int % 1_000_000
    user_id = 8_044_044_000 + suffix
    chat_id = -9_044_044_000 - suffix
    async with factory() as setup:
        await _user(setup, user_id=user_id, is_bot=False)
        source = await _message(
            setup,
            user_id=user_id,
            message_id=4044000 + suffix,
            text="pending forget must wait",
            chat_id=chat_id,
        )
        await setup.commit()
    bundle = _evidence_bundle(source)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_delivery() -> None:
        async with factory() as session:
            async with hold_evidence_delivery_locks(session, bundle):
                entered.set()
                await release.wait()

    async def create_forget() -> None:
        async with factory() as session:
            await ForgetEventRepo.create(
                session,
                target_type="message",
                target_id=str(source.message.id),
                actor_user_id=None,
                authorized_by="system",
                tombstone_key=f"message:{chat_id}:{source.message.message_id}:delivery-lock",
            )
            await session.commit()

    holder = asyncio.create_task(hold_delivery())
    await asyncio.wait_for(entered.wait(), timeout=2)
    writer = asyncio.create_task(create_forget())
    await asyncio.sleep(0.1)
    assert writer.done() is False
    release.set()
    await asyncio.gather(holder, writer)

    async with factory() as cleanup:
        await cleanup.execute(
            delete(ForgetEvent).where(ForgetEvent.tombstone_key.like("%:delivery-lock"))
        )
        await cleanup.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
        await cleanup.execute(delete(User).where(User.id == user_id))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_delivery_scope_blocks_edit_offrecord_writer(postgres_engine) -> None:
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.db.locks import advisory_lock_chat_message
    from bot.services.llm_gateway import hold_evidence_delivery_locks

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().int % 1_000_000
    user_id = 8_044_045_000 + suffix
    chat_id = -9_044_045_000 - suffix
    async with factory() as setup:
        await _user(setup, user_id=user_id, is_bot=False)
        source = await _message(
            setup,
            user_id=user_id,
            message_id=4045000 + suffix,
            text="offrecord edit must wait",
            chat_id=chat_id,
        )
        await setup.commit()
    bundle = _evidence_bundle(source)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_delivery() -> None:
        async with factory() as session:
            async with hold_evidence_delivery_locks(session, bundle):
                entered.set()
                await release.wait()

    async def apply_offrecord() -> None:
        async with factory() as session:
            await advisory_lock_chat_message(
                session,
                source.message.chat_id,
                source.message.message_id,
            )
            await session.execute(
                update(ChatMessage)
                .where(ChatMessage.id == source.message.id)
                .values(memory_policy="offrecord", is_redacted=True, text=None)
            )
            await session.commit()

    holder = asyncio.create_task(hold_delivery())
    await asyncio.wait_for(entered.wait(), timeout=2)
    writer = asyncio.create_task(apply_offrecord())
    await asyncio.sleep(0.1)
    assert writer.done() is False
    release.set()
    await asyncio.gather(holder, writer)

    async with factory() as cleanup:
        await cleanup.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
        await cleanup.execute(delete(User).where(User.id == user_id))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_cancelled_delivery_scope_releases_dedicated_xact_lock(postgres_engine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bot.services.advisory_locks import hold_session_advisory_locks

    factory = async_sessionmaker(postgres_engine, class_=AsyncSession, expire_on_commit=False)
    lock_id = 8_044_046_000 + (uuid.uuid4().int % 1_000_000)
    entered = asyncio.Event()

    async def cancelled_holder() -> None:
        async with factory() as session:
            async with hold_session_advisory_locks(session, (lock_id,)):
                entered.set()
                await asyncio.Event().wait()

    holder = asyncio.create_task(cancelled_holder())
    await asyncio.wait_for(entered.wait(), timeout=2)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    async def reacquire() -> None:
        async with factory() as session:
            async with hold_session_advisory_locks(session, (lock_id,)):
                return

    await asyncio.wait_for(reacquire(), timeout=2)


@pytest.mark.asyncio
async def test_reconcile_invalidates_every_active_unit_that_became_ineligible(db_session) -> None:
    human = await _user(db_session, user_id=8_044_042_251, is_bot=False)
    admin = await _user(db_session, user_id=8_044_042_252, is_bot=False, is_admin=True)
    offrecord = await _message(
        db_session, user_id=human.id, message_id=4042251, text="offrecord source"
    )
    redacted = await _message(
        db_session, user_id=human.id, message_id=4042252, text="redacted source"
    )
    stale = await _message(db_session, user_id=human.id, message_id=4042253, text="stale source")
    card_source = await _message(
        db_session, user_id=human.id, message_id=4042254, text="deapproved card source"
    )
    card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(card_source.version.id,),
        title="deapproved card",
    )
    ledger = await _ledger(db_session, suffix="reconcile")
    await _unit(db_session, source=offrecord, vector=VECTOR_X, ledger_id=ledger.id)
    await _unit(db_session, source=redacted, vector=VECTOR_X, ledger_id=ledger.id)
    await _unit(db_session, source=stale, vector=VECTOR_X, ledger_id=ledger.id)
    card_unit = SemanticRetrievalUnit(
        source_type="card",
        source_id=str(card.id),
        source_revision="updated:test:approved:test",
        chat_id=CHAT_ID,
        content_hash="d" * 64,
        embedding_provider="openai",
        embedding_model=CONFIG.model,
        embedding_model_version="text-embedding-3-small",
        embedding_dimensions=CONFIG.dimensions,
        embedding=list(VECTOR_X),
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(card_unit)
    await db_session.flush()
    db_session.add(
        SemanticRetrievalUnitSource(
            unit_id=card_unit.id,
            message_version_id=card_source.version.id,
            position=0,
        )
    )
    offrecord.message.memory_policy = "offrecord"
    redacted.version.is_redacted = True
    replacement = MessageVersion(
        chat_message_id=stale.message.id,
        version_seq=2,
        text="new current version",
        normalized_text="new current version",
        content_hash=f"semantic-{uuid.uuid4().hex}",
        is_redacted=False,
    )
    db_session.add(replacement)
    await db_session.flush()
    stale.message.current_version_id = replacement.id
    card.card_status = "draft"
    await db_session.flush()

    assert (
        await _reconcile_ineligible_units(
            db_session,
            embedding_model=CONFIG.model,
            chat_id=CHAT_ID,
        )
        == 4
    )
    units = (await db_session.execute(select(SemanticRetrievalUnit))).scalars().all()
    assert len(units) == 4
    assert all(unit.invalidated_at is not None for unit in units)
    assert {unit.invalidation_reason for unit in units} == {"source_ineligible"}
    assert (
        await _reconcile_ineligible_units(
            db_session,
            embedding_model=CONFIG.model,
            chat_id=CHAT_ID,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_stale_vector_card_cannot_displace_fresh_fts_provenance(db_session) -> None:
    human = await _user(db_session, user_id=8_044_042_301, is_bot=False)
    admin = await _user(db_session, user_id=8_044_042_302, is_bot=False, is_admin=True)
    old_source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042301,
        text="old card provenance",
    )
    new_source = await _message(
        db_session,
        user_id=human.id,
        message_id=4042302,
        text="new card provenance",
    )
    card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(old_source.version.id,),
        title="уникальный семантический маяк",
    )
    document = (await list_eligible_card_documents(db_session, chat_id=CHAT_ID))[0]
    await _index_batch(
        db_session,
        documents=[document],
        config=CONFIG,
        provider=CountingEmbeddingProvider(),
    )

    old_link = (
        await db_session.execute(select(CardSource).where(CardSource.card_id == card.id))
    ).scalar_one()
    await db_session.delete(old_link)
    db_session.add(
        CardSource(
            card_id=card.id,
            message_version_id=new_source.version.id,
            position=0,
        )
    )
    await db_session.flush()

    result = await hybrid_search(
        db_session,
        query="уникальный семантический маяк",
        query_embedding=VECTOR_X,
        chat_id=CHAT_ID,
        embedding_model=CONFIG.model,
    )

    matching = [hit for hit in result.hits if hit.card_id == card.id]
    assert len(matching) == 1
    assert matching[0].card_source_message_version_ids == (new_source.version.id,)
    assert result.candidate_ranks[f"card:{card.id}"] == {"fts": 1}


@pytest.mark.asyncio
async def test_human_only_fts_excludes_voice_audio_and_any_card_voice_provenance(
    db_session,
) -> None:
    human = await _user(db_session, user_id=8_044_042_301, is_bot=False)
    admin = await _user(db_session, user_id=8_044_042_302, is_bot=False, is_admin=True)
    clean = await _message(
        db_session,
        user_id=human.id,
        message_id=4042301,
        text="утечкомаркер безопасный текст",
    )
    voice = await _message(
        db_session,
        user_id=human.id,
        message_id=4042302,
        text="утечкомаркер голосовой секрет",
        message_kind="voice",
    )
    audio = await _message(
        db_session,
        user_id=human.id,
        message_id=4042303,
        text="утечкомаркер аудио секрет",
        message_kind="audio",
    )
    clean_card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(clean.version.id,),
        title="утечкомаркер чистая карточка",
    )
    mixed_voice_card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(clean.version.id, voice.version.id),
        title="утечкомаркер смешанная голосовая карточка",
    )
    audio_card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(audio.version.id,),
        title="утечкомаркер аудио карточка",
    )
    await db_session.flush()

    hits = await search_messages(
        db_session,
        "утечкомаркер",
        chat_id=CHAT_ID,
        limit=20,
        include_cards=True,
        human_only=True,
    )

    assert {hit.message_version_id for hit in hits if hit.source_type == "message"} == {
        clean.version.id
    }
    assert {hit.card_id for hit in hits if hit.source_type == "card"} == {clean_card.id}
    assert mixed_voice_card.id not in {hit.card_id for hit in hits}
    assert audio_card.id not in {hit.card_id for hit in hits}
    assert all(voice.version.id not in hit.card_source_message_version_ids for hit in hits)
    assert all(audio.version.id not in hit.card_source_message_version_ids for hit in hits)


@pytest.mark.asyncio
async def test_exact_cosine_excludes_current_question_and_foreign_chat(db_session) -> None:
    human = await _user(db_session, user_id=8_044_043_001, is_bot=False)
    bot = await _user(db_session, user_id=8_044_043_002, is_bot=True)
    current_question = await _message(
        db_session, user_id=human.id, message_id=4043001, text="текущий вопрос"
    )
    citable = await _message(
        db_session, user_id=human.id, message_id=4043002, text="точный полезный ответ"
    )
    weaker = await _message(
        db_session, user_id=human.id, message_id=4043004, text="менее похожий ответ"
    )
    forgotten = await _message(
        db_session, user_id=human.id, message_id=4043005, text="удаляемый секрет"
    )
    bot_source = await _message(
        db_session, user_id=bot.id, message_id=4043006, text="ответ от бота"
    )
    foreign = await _message(
        db_session,
        user_id=human.id,
        message_id=4043003,
        text="секрет другого чата",
        chat_id=OTHER_CHAT_ID,
    )
    ledger = await _ledger(db_session, suffix="a")
    await _unit(db_session, source=current_question, vector=VECTOR_X, ledger_id=ledger.id)
    await _unit(db_session, source=citable, vector=VECTOR_X, ledger_id=ledger.id)
    await _unit(db_session, source=weaker, vector=VECTOR_Y, ledger_id=ledger.id)
    await _unit(db_session, source=forgotten, vector=VECTOR_X, ledger_id=ledger.id)
    await _unit(db_session, source=bot_source, vector=VECTOR_X, ledger_id=ledger.id)
    await _unit(db_session, source=foreign, vector=VECTOR_X, ledger_id=ledger.id)
    db_session.add(
        ForgetEvent(
            target_type="message",
            target_id=str(forgotten.message.id),
            actor_user_id=human.id,
            authorized_by="self",
            tombstone_key=f"message:{CHAT_ID}:{forgotten.message.message_id}",
            status="pending",
        )
    )
    await db_session.flush()

    hits = await vector_search(
        db_session,
        query_embedding=VECTOR_X,
        chat_id=CHAT_ID,
        embedding_model="text-embedding-3-small",
        exclude_chat_message_id=current_question.message.id,
    )

    assert [hit.message_version_id for hit in hits] == [
        citable.version.id,
        weaker.version.id,
    ]
    assert all(hit.chat_id == CHAT_ID for hit in hits)
    assert all("секрет другого чата" not in hit.snippet for hit in hits)
    assert all("удаляемый секрет" not in hit.snippet for hit in hits)
    assert all("ответ от бота" not in hit.snippet for hit in hits)


@pytest.mark.asyncio
async def test_vector_search_returns_approved_card_then_blocks_pending_forget(db_session) -> None:
    human = await _user(db_session, user_id=8_044_044_001, is_bot=False)
    admin = await _user(db_session, user_id=8_044_044_002, is_bot=False, is_admin=True)
    source = await _message(
        db_session, user_id=human.id, message_id=4044001, text="источник карточки"
    )
    card = await _approved_card(
        db_session,
        admin_id=admin.id,
        source_ids=(source.version.id,),
        title="Каноническая карточка",
    )
    ledger = await _ledger(db_session, suffix="b")
    unit = SemanticRetrievalUnit(
        source_type="card",
        source_id=str(card.id),
        source_revision="updated:test:approved:test",
        chat_id=CHAT_ID,
        content_hash="c" * 64,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_model_version="text-embedding-3-small",
        embedding_dimensions=1536,
        embedding=list(VECTOR_X),
        llm_usage_ledger_id=ledger.id,
    )
    db_session.add(unit)
    await db_session.flush()
    db_session.add(
        SemanticRetrievalUnitSource(
            unit_id=unit.id,
            message_version_id=source.version.id,
            position=0,
        )
    )
    await db_session.flush()

    hits = await vector_search(
        db_session,
        query_embedding=VECTOR_X,
        chat_id=CHAT_ID,
        embedding_model="text-embedding-3-small",
    )
    assert len(hits) == 1
    assert hits[0].source_type == "card"
    assert hits[0].card_id == card.id
    assert hits[0].card_source_message_version_ids == (source.version.id,)

    db_session.add(
        ForgetEvent(
            target_type="message",
            target_id=str(source.message.id),
            actor_user_id=human.id,
            authorized_by="self",
            tombstone_key=f"message:{CHAT_ID}:{source.message.message_id}",
            status="pending",
        )
    )
    await db_session.flush()
    blocked = await vector_search(
        db_session,
        query_embedding=VECTOR_X,
        chat_id=CHAT_ID,
        embedding_model="text-embedding-3-small",
    )
    assert blocked == []


def _hit(
    *,
    version_id: int,
    author_id: int | None,
    source_type: Literal["message", "card"] = "message",
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    conversation_root_message_id: int | None = None,
) -> SearchHit:
    now = datetime.now(timezone.utc)
    card_id = uuid.UUID(int=version_id) if source_type == "card" else None
    return SearchHit(
        message_version_id=version_id,
        chat_message_id=version_id,
        chat_id=CHAT_ID,
        message_id=version_id,
        user_id=author_id,
        snippet=f"hit-{version_id}",
        ts_rank=1.0,
        captured_at=now,
        message_date=now,
        source_type=source_type,
        card_id=card_id,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
        conversation_root_message_id=conversation_root_message_id,
    )


def test_rrf_deduplicates_and_enforces_author_and_card_diversity() -> None:
    same_author = [_hit(version_id=index, author_id=77) for index in range(1, 5)]
    cards = [_hit(version_id=index, author_id=None, source_type="card") for index in range(10, 13)]
    another_author = _hit(version_id=20, author_id=88)

    selected, ranks = reciprocal_rank_fusion(
        vector_hits=[*same_author, *cards, another_author],
        fts_hits=[same_author[0], another_author, *cards],
        limit=5,
    )

    assert len(selected) == 5
    assert sum(hit.user_id == 77 for hit in selected) == 2
    assert sum(hit.source_type == "card" for hit in selected) == 2
    assert len({(hit.source_type, hit.card_id, hit.message_version_id) for hit in selected}) == 5
    assert ranks["message:1"] == {"vector": 1, "fts": 1}


def test_rrf_caps_explicit_telegram_topic_without_collapsing_non_topic_chat() -> None:
    topic_hits = [
        _hit(version_id=index, author_id=100 + index, message_thread_id=9001)
        for index in range(1, 4)
    ]
    non_topic_hits = [_hit(version_id=index, author_id=100 + index) for index in range(10, 13)]

    selected, _ = reciprocal_rank_fusion(
        vector_hits=[*topic_hits, *non_topic_hits],
        fts_hits=[],
        limit=5,
    )

    assert sum(hit.message_thread_id == 9001 for hit in selected) == 2
    assert sum(hit.message_thread_id is None for hit in selected) == 3


def test_rrf_caps_direct_reply_conversation_without_collapsing_standalone_messages() -> None:
    replies = [
        _hit(version_id=index, author_id=100 + index, reply_to_message_id=9001)
        for index in range(1, 4)
    ]
    standalone = [_hit(version_id=index, author_id=100 + index) for index in range(10, 13)]

    selected, _ = reciprocal_rank_fusion(
        vector_hits=[*replies, *standalone],
        fts_hits=[],
        limit=5,
    )

    assert sum(hit.reply_to_message_id == 9001 for hit in selected) == 2
    assert sum(hit.reply_to_message_id is None for hit in selected) == 3


@pytest.mark.asyncio
async def test_nested_replies_share_resolved_root_for_diversity(db_session) -> None:
    human = await _user(db_session, user_id=8_044_045_001, is_bot=False)
    await _message(db_session, user_id=human.id, message_id=5000, text="root")
    await _message(
        db_session,
        user_id=human.id,
        message_id=5001,
        text="child",
        reply_to_message_id=5000,
    )
    await _message(
        db_session,
        user_id=human.id,
        message_id=5002,
        text="grandchild",
        reply_to_message_id=5001,
    )
    await _message(
        db_session,
        user_id=human.id,
        message_id=5003,
        text="sibling",
        reply_to_message_id=5000,
    )
    await _message(db_session, user_id=human.id, message_id=6000, text="standalone")
    hits = [
        _hit(
            version_id=message_id,
            author_id=100 + message_id,
            reply_to_message_id=reply_to,
        )
        for message_id, reply_to in (
            (5000, None),
            (5001, 5000),
            (5002, 5001),
            (5003, 5000),
            (6000, None),
        )
    ]

    enriched = await _with_conversation_roots(db_session, chat_id=CHAT_ID, hits=hits)
    selected, _ = reciprocal_rank_fusion(vector_hits=enriched, fts_hits=[], limit=5)

    assert {hit.conversation_root_message_id for hit in enriched[:4]} == {5000}
    assert sum(hit.conversation_root_message_id == 5000 for hit in selected) == 2
    assert any(hit.message_id == 6000 for hit in selected)
