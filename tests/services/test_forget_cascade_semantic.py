"""Issue #404 privacy cascade coverage for pgvector semantic Q&A data."""

from __future__ import annotations

import hashlib
import itertools
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update


pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(1)


def _next() -> int:
    return next(_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    user_id = 9_700_000_000 + _next()
    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"semantic_forget_{user_id}",
        first_name="Semantic",
        last_name=None,
        is_bot=False,
    )
    return user_id


async def _make_message(
    db_session,
    *,
    user_id: int,
    texts: tuple[str, ...] = ("semantic secret",),
    content_hashes: tuple[str, ...] | None = None,
):
    from bot.db.models import ChatMessage, MessageVersion

    ordinal = _next()
    message = ChatMessage(
        message_id=1_900_000 + ordinal,
        chat_id=-100_900_000_000 - ordinal,
        user_id=user_id,
        text=texts[-1],
        date=datetime.now(timezone.utc),
        raw_json={"text": texts[-1]},
        memory_policy="normal",
        visibility="member",
        is_redacted=False,
    )
    db_session.add(message)
    await db_session.flush()

    versions = []
    for sequence, body in enumerate(texts, start=1):
        content_hash = (
            content_hashes[sequence - 1]
            if content_hashes is not None
            else hashlib.sha256(f"{message.id}:{sequence}:{body}".encode()).hexdigest()
        )
        version = MessageVersion(
            chat_message_id=message.id,
            version_seq=sequence,
            text=body,
            normalized_text=body,
            entities_json={"entities": []},
            content_hash=content_hash,
            is_redacted=False,
        )
        db_session.add(version)
        await db_session.flush()
        versions.append(version)

    await db_session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == message.id)
        .values(
            current_version_id=versions[-1].id,
            content_hash=versions[-1].content_hash,
        )
    )
    await db_session.flush()
    return message, versions


async def _make_ledger(
    db_session,
    *,
    call_type: str = "semantic_embedding",
    provider: str = "openai",
):
    from bot.db.models import LlmUsageLedger

    row = LlmUsageLedger(
        qa_trace_id=None,
        provider=provider,
        model=("text-embedding-3-small" if provider == "openai" else "deepseek-v4-flash"),
        prompt_hash="a" * 64,
        response_hash="b" * 64,
        tokens_in=123,
        tokens_out=17 if call_type == "qa_synthesis" else 0,
        cost_usd=Decimal("0.001234"),
        latency_ms=321,
        request_id=f"req-{_next()}",
        cache_hit=False,
        error=None,
        call_type=call_type,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _make_unit(
    db_session,
    *,
    source_type: str,
    source_id: str,
    chat_id: int,
    message_version_ids: list[int],
    ledger_id: int,
):
    from bot.db.models import SemanticRetrievalUnit, SemanticRetrievalUnitSource

    ordinal = _next()
    unit = SemanticRetrievalUnit(
        source_type=source_type,
        source_id=source_id,
        source_revision=f"test-r{ordinal}",
        chat_id=chat_id,
        content_hash=hashlib.sha256(f"unit:{ordinal}".encode()).hexdigest(),
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_model_version="text-embedding-3-small",
        embedding_dimensions=1536,
        embedding_status="completed",
        chunk_text=f"semantic chunk {ordinal}",
        embedding=[0.0] * 1536,
        llm_usage_ledger_id=ledger_id,
        indexed_at=datetime.now(timezone.utc),
    )
    db_session.add(unit)
    await db_session.flush()
    for position, message_version_id in enumerate(message_version_ids):
        db_session.add(
            SemanticRetrievalUnitSource(
                unit_id=unit.id,
                message_version_id=message_version_id,
                position=position,
            )
        )
    await db_session.flush()
    return unit


async def _make_attempt_with_trace(
    db_session,
    *,
    user_id: int,
    chat_id: int,
    source_keys: list[str],
    slot_number: int = 1,
    source_chat_message_id: int | None = None,
):
    from bot.db.models import SemanticQaAttempt, SemanticRetrievalTrace

    embedding_ledger = await _make_ledger(db_session)
    synthesis_ledger = await _make_ledger(
        db_session,
        call_type="qa_synthesis",
        provider="deepseek",
    )
    attempt = SemanticQaAttempt(
        idempotency_key=f"semantic-forget-attempt-{_next()}",
        user_tg_id=user_id,
        chat_id=chat_id,
        source_chat_message_id=source_chat_message_id,
        local_day=date.today(),
        slot_number=slot_number,
        status="consumed",
        outcome="answered",
        qa_trace_id=None,
        embedding_llm_call_id=embedding_ledger.id,
        synthesis_llm_call_id=synthesis_ledger.id,
        delivery_started_at=datetime.now(timezone.utc),
        finalized_at=datetime.now(timezone.utc),
    )
    db_session.add(attempt)
    await db_session.flush()
    trace = SemanticRetrievalTrace(
        attempt_id=attempt.id,
        qa_trace_id=None,
        query_hash="c" * 64,
        embedding_model="text-embedding-3-small",
        retrieval_mode="hybrid",
        candidate_ranks={key: {"vector": index + 1} for index, key in enumerate(source_keys)},
        result_source_ids=list(source_keys),
        fts_latency_ms=1,
        vector_latency_ms=2,
        fusion_latency_ms=1,
        total_latency_ms=4,
    )
    db_session.add(trace)
    await db_session.flush()
    return attempt, trace, embedding_ledger, synthesis_ledger


async def _make_forget_event(
    db_session,
    *,
    target_type: str,
    target_id: str,
    tombstone_key: str,
):
    from bot.db.repos.forget_event import ForgetEventRepo

    return await ForgetEventRepo.create(
        db_session,
        target_type=target_type,
        target_id=target_id,
        actor_user_id=None,
        authorized_by="admin",
        tombstone_key=tombstone_key,
    )


@pytest.mark.asyncio
async def test_failed_embedding_claim_is_forgettable_with_its_ledger(db_session) -> None:
    from bot.db.models import LlmUsageLedger, SemanticRetrievalUnit
    from bot.services.forget_cascade import _cascade_semantic_retrieval
    from bot.services.llm_gateway import EmbeddingGatewayConfig
    from bot.services.llm_providers import ProviderTransientError
    from bot.services.semantic_index import (
        _index_batch,
        list_eligible_message_documents,
        vector_search,
    )

    class FailingProvider:
        async def embed(self, *, inputs, model, dimensions):
            raise ProviderTransientError("timeout", message="redacted")

    user_id = await _make_user(db_session)
    message, versions = await _make_message(db_session, user_id=user_id)
    documents = await list_eligible_message_documents(
        db_session,
        chat_id=message.chat_id,
    )
    config = EmbeddingGatewayConfig(
        model="text-embedding-3-small",
        dimensions=1536,
        daily_ceiling_usd=Decimal("100"),
        monthly_ceiling_usd=Decimal("1000"),
    )

    with pytest.raises(ProviderTransientError):
        await _index_batch(
            db_session,
            documents=documents,
            config=config,
            provider=FailingProvider(),
        )

    unit = (await db_session.execute(select(SemanticRetrievalUnit))).scalars().one()
    assert unit.embedding_status == "failed"
    assert unit.embedding is None
    ledger_id = unit.llm_usage_ledger_id
    ledger = await db_session.get(LlmUsageLedger, ledger_id)
    assert ledger is not None and ledger.prompt_hash is not None
    assert (
        await vector_search(
            db_session,
            query_embedding=(1.0,) + (0.0,) * 1535,
            chat_id=message.chat_id,
            embedding_model=config.model,
        )
        == []
    )

    event = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(message.id),
        tombstone_key=f"failed-claim-forget-{versions[-1].id}",
    )
    await _cascade_semantic_retrieval(db_session, event)

    assert await db_session.get(SemanticRetrievalUnit, unit.id) is None
    redacted_ledger = await db_session.get(LlmUsageLedger, ledger_id)
    assert redacted_ledger is not None
    assert redacted_ledger.prompt_hash is None
    assert redacted_ledger.response_hash is None


@pytest.mark.asyncio
async def test_budget_denied_embedding_claim_is_forgettable_without_provider_call(
    db_session,
) -> None:
    from bot.db.models import LlmUsageLedger, SemanticRetrievalUnit
    from bot.services.forget_cascade import _cascade_semantic_retrieval
    from bot.services.llm_gateway import EmbeddingBudgetExceeded, EmbeddingGatewayConfig
    from bot.services.semantic_index import _index_batch, list_eligible_message_documents

    class ForbiddenProvider:
        calls = 0

        async def embed(self, *, inputs, model, dimensions):
            self.calls += 1
            raise AssertionError("budget denial must precede provider dispatch")

    user_id = await _make_user(db_session)
    message, versions = await _make_message(
        db_session,
        user_id=user_id,
        texts=("x" * 800,),
    )
    documents = await list_eligible_message_documents(
        db_session,
        chat_id=message.chat_id,
    )
    provider = ForbiddenProvider()
    config = EmbeddingGatewayConfig(
        model="text-embedding-3-small",
        dimensions=1536,
        daily_ceiling_usd=Decimal("0.000001"),
        monthly_ceiling_usd=Decimal("0.000001"),
    )

    with pytest.raises(EmbeddingBudgetExceeded):
        await _index_batch(
            db_session,
            documents=documents,
            config=config,
            provider=provider,
        )

    assert provider.calls == 0
    unit = (await db_session.execute(select(SemanticRetrievalUnit))).scalars().one()
    assert unit.embedding_status == "failed"
    ledger_id = unit.llm_usage_ledger_id
    ledger = await db_session.get(LlmUsageLedger, ledger_id)
    assert ledger is not None and ledger.error == "budget_exceeded"

    event = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(message.id),
        tombstone_key=f"budget-claim-forget-{versions[-1].id}",
    )
    await _cascade_semantic_retrieval(db_session, event)

    assert await db_session.get(SemanticRetrievalUnit, unit.id) is None
    redacted_ledger = await db_session.get(LlmUsageLedger, ledger_id)
    assert redacted_ledger is not None
    assert redacted_ledger.prompt_hash is None


def test_semantic_layer_runs_before_message_versions_and_is_registered() -> None:
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER, _LAYER_FUNCS

    assert CASCADE_LAYER_ORDER.index("semantic_retrieval") < CASCADE_LAYER_ORDER.index(
        "message_versions"
    )
    assert "semantic_retrieval" in _LAYER_FUNCS


async def test_forgetting_question_message_redacts_query_embedding_audit(
    db_session,
) -> None:
    from bot.db.models import (
        LlmUsageLedger,
        QaTrace,
        SemanticQaAttempt,
        SemanticRetrievalTrace,
    )
    from bot.services.forget_cascade import run_cascade_worker_once

    owner_id = await _make_user(db_session)
    message, _ = await _make_message(
        db_session,
        user_id=owner_id,
        texts=("forgotten semantic question",),
    )
    attempt, trace, embedding_ledger, synthesis_ledger = await _make_attempt_with_trace(
        db_session,
        user_id=owner_id,
        chat_id=message.chat_id,
        source_keys=[],
        source_chat_message_id=message.id,
    )
    qa_trace = QaTrace(
        source_chat_message_id=message.id,
        user_tg_id=owner_id,
        chat_id=message.chat_id,
        query_text="forgotten semantic question",
        query_redacted=False,
        evidence_ids=[],
        abstained=True,
        llm_response_summary="derived answer",
        llm_response_redacted=False,
    )
    db_session.add(qa_trace)
    await db_session.flush()
    attempt.qa_trace_id = qa_trace.id
    trace.qa_trace_id = qa_trace.id
    await db_session.flush()
    await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(message.id),
        tombstone_key=f"message:{message.chat_id}:{message.message_id}",
    )

    stats = await run_cascade_worker_once(db_session)

    assert stats == {"claimed": 1, "processed": 1, "failed": 0}
    assert await db_session.get(SemanticQaAttempt, attempt.id) is not None
    assert await db_session.get(SemanticRetrievalTrace, trace.id) is None
    redacted_qa = await db_session.get(QaTrace, qa_trace.id, populate_existing=True)
    assert redacted_qa.query_text is None
    assert redacted_qa.query_redacted is True
    assert redacted_qa.llm_response_summary is None
    assert redacted_qa.llm_response_redacted is True
    for ledger in (embedding_ledger, synthesis_ledger):
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash is None
        assert row.response_hash is None
        assert row.cost_usd == Decimal("0.001234")


async def test_forget_redacts_inflight_trace_ledgers_without_hash_resurrection(
    db_session,
) -> None:
    from bot.db.models import LlmUsageLedger, QaTrace
    from bot.db.repos.llm_usage_ledger import LedgerRepo
    from bot.services.forget_cascade import run_cascade_worker_once

    owner_id = await _make_user(db_session)
    message, _ = await _make_message(
        db_session,
        user_id=owner_id,
        texts=("question forgotten during provider call",),
    )
    attempt, trace, embedding_ledger, synthesis_ledger = await _make_attempt_with_trace(
        db_session,
        user_id=owner_id,
        chat_id=message.chat_id,
        source_keys=[],
        source_chat_message_id=message.id,
    )
    qa_trace = QaTrace(
        source_chat_message_id=message.id,
        user_tg_id=owner_id,
        chat_id=message.chat_id,
        query_text="question forgotten during provider call",
        query_redacted=False,
        evidence_ids=[],
        abstained=True,
        llm_response_summary=None,
        llm_response_redacted=False,
    )
    db_session.add(qa_trace)
    await db_session.flush()
    attempt.qa_trace_id = qa_trace.id
    trace.qa_trace_id = qa_trace.id
    # Provider placeholders exist before the result FKs can be attached to the
    # attempt.  The forget cascade must discover them through qa_trace_id.
    attempt.embedding_llm_call_id = None
    attempt.synthesis_llm_call_id = None
    embedding_ledger.qa_trace_id = qa_trace.id
    synthesis_ledger.qa_trace_id = qa_trace.id
    await db_session.flush()
    await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(message.id),
        tombstone_key=f"message:{message.chat_id}:{message.message_id}",
    )

    stats = await run_cascade_worker_once(db_session)

    assert stats == {"claimed": 1, "processed": 1, "failed": 0}
    for ledger in (embedding_ledger, synthesis_ledger):
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash is None
        assert row.response_hash is None

    # Simulate the provider result resuming after the forget transaction.  Cost
    # and telemetry remain auditable, while redacted hashes are irreversible.
    await LedgerRepo.update_placeholder(
        db_session,
        llm_call_id=synthesis_ledger.id,
        tokens_in=321,
        tokens_out=42,
        cost_usd=Decimal("0.004200"),
        latency_ms=654,
        request_id="provider-resumed-after-forget",
        response_hash="f" * 64,
        error=None,
    )
    resumed = await db_session.get(
        LlmUsageLedger,
        synthesis_ledger.id,
        populate_existing=True,
    )
    assert resumed.prompt_hash is None
    assert resumed.response_hash is None
    assert resumed.tokens_in == 321
    assert resumed.tokens_out == 42
    assert resumed.cost_usd == Decimal("0.004200")


async def test_message_forget_purges_all_revision_and_card_units_and_related_audit(
    db_session,
) -> None:
    from bot.db.models import (
        CardSource,
        ForgetEvent,
        KnowledgeCard,
        LlmUsageLedger,
        MessageVersion,
        SemanticQaAttempt,
        SemanticRetrievalTrace,
        SemanticRetrievalUnit,
        SemanticRetrievalUnitSource,
    )
    from bot.services.forget_cascade import run_cascade_worker_once

    owner_id = await _make_user(db_session)
    message, versions = await _make_message(
        db_session,
        user_id=owner_id,
        texts=("old forgotten revision", "current forgotten revision"),
    )
    target_ledgers = [await _make_ledger(db_session) for _ in range(3)]
    old_unit = await _make_unit(
        db_session,
        source_type="message",
        source_id=str(versions[0].id),
        chat_id=message.chat_id,
        message_version_ids=[versions[0].id],
        ledger_id=target_ledgers[0].id,
    )
    current_unit = await _make_unit(
        db_session,
        source_type="message",
        source_id=str(versions[1].id),
        chat_id=message.chat_id,
        message_version_ids=[versions[1].id],
        ledger_id=target_ledgers[1].id,
    )

    support_owner_id = await _make_user(db_session)
    _support_message, support_versions = await _make_message(
        db_session,
        user_id=support_owner_id,
        texts=("card anchor must remain",),
    )

    card = KnowledgeCard(
        title="Forgotten card",
        body_markdown="Derived from the forgotten source",
        card_status="approved",
        approved_by_user_id=owner_id,
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    await db_session.flush()
    db_session.add_all(
        [
            CardSource(
                card_id=card.id,
                message_version_id=support_versions[0].id,
                position=0,
            ),
            CardSource(
                card_id=card.id,
                message_version_id=versions[1].id,
                position=1,
            ),
        ]
    )
    await db_session.flush()
    card_unit = await _make_unit(
        db_session,
        source_type="card",
        source_id=str(card.id),
        chat_id=message.chat_id,
        message_version_ids=[support_versions[0].id, versions[1].id],
        ledger_id=target_ledgers[2].id,
    )

    other_owner_id = await _make_user(db_session)
    other_message, other_versions = await _make_message(
        db_session,
        user_id=other_owner_id,
        texts=("must remain",),
    )
    other_unit_ledger = await _make_ledger(db_session)
    other_unit = await _make_unit(
        db_session,
        source_type="message",
        source_id=str(other_versions[0].id),
        chat_id=other_message.chat_id,
        message_version_ids=[other_versions[0].id],
        ledger_id=other_unit_ledger.id,
    )

    querying_user_id = await _make_user(db_session)
    (
        affected_attempt,
        affected_trace,
        query_ledger,
        synthesis_ledger,
    ) = await _make_attempt_with_trace(
        db_session,
        user_id=querying_user_id,
        chat_id=message.chat_id,
        source_keys=[f"message:{versions[0].id}", f"card:{card.id}"],
        slot_number=1,
    )
    from bot.db.models import LlmSynthesisCache, QaTrace

    affected_qa_trace = QaTrace(
        user_tg_id=querying_user_id,
        chat_id=message.chat_id,
        query_text="answer from card",
        query_redacted=False,
        evidence_ids=[support_versions[0].id, versions[1].id],
        abstained=False,
        llm_response_summary="derived from forgotten non-anchor source",
        llm_response_redacted=False,
    )
    db_session.add(affected_qa_trace)
    affected_cache = LlmSynthesisCache(
        input_hash=hashlib.sha256(f"cache:{_next()}".encode()).hexdigest(),
        answer_text="cached from forgotten non-anchor source",
        citation_ids=[support_versions[0].id, versions[1].id],
        model="deepseek-v4-flash",
    )
    db_session.add(affected_cache)
    await db_session.flush()
    affected_attempt.qa_trace_id = affected_qa_trace.id
    affected_trace.qa_trace_id = affected_qa_trace.id
    await db_session.flush()
    (
        other_attempt,
        other_trace,
        other_query_ledger,
        other_synthesis_ledger,
    ) = await _make_attempt_with_trace(
        db_session,
        user_id=querying_user_id,
        chat_id=other_message.chat_id,
        source_keys=[f"message:{other_versions[0].id}"],
        slot_number=2,
    )

    event = await _make_forget_event(
        db_session,
        target_type="message",
        target_id=str(message.id),
        tombstone_key=f"message:{message.chat_id}:{message.message_id}",
    )
    stats = await run_cascade_worker_once(db_session)

    assert stats == {"claimed": 1, "processed": 1, "failed": 0}
    for unit in (old_unit, current_unit, card_unit):
        assert await db_session.get(SemanticRetrievalUnit, unit.id) is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SemanticRetrievalUnitSource)
            .where(
                SemanticRetrievalUnitSource.unit_id.in_(
                    [old_unit.id, current_unit.id, card_unit.id]
                )
            )
        )
        == 0
    )
    assert await db_session.get(SemanticRetrievalUnit, other_unit.id) is not None
    assert (
        await db_session.scalar(
            select(SemanticRetrievalTrace.id).where(SemanticRetrievalTrace.id == affected_trace.id)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(SemanticRetrievalTrace.id).where(SemanticRetrievalTrace.id == other_trace.id)
        )
        == other_trace.id
    )
    assert await db_session.get(SemanticQaAttempt, affected_attempt.id) is not None
    assert await db_session.get(SemanticQaAttempt, other_attempt.id) is not None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(LlmSynthesisCache)
            .where(LlmSynthesisCache.id == affected_cache.id)
        )
        == 0
    )
    redacted_trace = await db_session.get(
        QaTrace,
        affected_qa_trace.id,
        populate_existing=True,
    )
    assert redacted_trace.llm_response_summary is None
    assert redacted_trace.llm_response_redacted is True

    for ledger in (*target_ledgers, synthesis_ledger):
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash is None
        assert row.response_hash is None
        assert row.cost_usd == Decimal("0.001234")
        assert row.tokens_in == 123
        assert row.latency_ms == 321
    for ledger in (query_ledger, other_unit_ledger, other_query_ledger, other_synthesis_ledger):
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash == "a" * 64
        assert row.response_hash == "b" * 64

    for version in versions:
        row = await db_session.get(MessageVersion, version.id, populate_existing=True)
        assert row.is_redacted is True
        assert row.text is None
    event_row = await db_session.get(ForgetEvent, event.id, populate_existing=True)
    assert event_row.cascade_status["semantic_retrieval"]["status"] == "completed"
    assert event_row.cascade_status["semantic_retrieval"]["rows"] >= 8


async def test_user_forget_deletes_query_trace_but_preserves_quota_and_cost_audit(
    db_session,
) -> None:
    from bot.db.models import (
        LlmUsageLedger,
        SemanticQaAttempt,
        SemanticRetrievalTrace,
        SemanticRetrievalUnit,
    )
    from bot.services.forget_cascade import run_cascade_worker_once

    forgotten_user_id = await _make_user(db_session)
    message, versions = await _make_message(db_session, user_id=forgotten_user_id)
    unit_ledger = await _make_ledger(db_session)
    unit = await _make_unit(
        db_session,
        source_type="message",
        source_id=str(versions[0].id),
        chat_id=message.chat_id,
        message_version_ids=[versions[0].id],
        ledger_id=unit_ledger.id,
    )
    attempt, trace, query_ledger, synthesis_ledger = await _make_attempt_with_trace(
        db_session,
        user_id=forgotten_user_id,
        chat_id=message.chat_id,
        source_keys=[],
    )

    other_user_id = await _make_user(db_session)
    other_message, other_versions = await _make_message(db_session, user_id=other_user_id)
    other_unit_ledger = await _make_ledger(db_session)
    other_unit = await _make_unit(
        db_session,
        source_type="message",
        source_id=str(other_versions[0].id),
        chat_id=other_message.chat_id,
        message_version_ids=[other_versions[0].id],
        ledger_id=other_unit_ledger.id,
    )
    (
        other_attempt,
        other_trace,
        other_query_ledger,
        other_synthesis_ledger,
    ) = await _make_attempt_with_trace(
        db_session,
        user_id=other_user_id,
        chat_id=other_message.chat_id,
        source_keys=[f"message:{other_versions[0].id}"],
    )

    await _make_forget_event(
        db_session,
        target_type="user",
        target_id=str(forgotten_user_id),
        tombstone_key=f"user:{forgotten_user_id}",
    )
    stats = await run_cascade_worker_once(db_session)

    assert stats == {"claimed": 1, "processed": 1, "failed": 0}
    assert await db_session.get(SemanticRetrievalUnit, unit.id) is None
    assert await db_session.get(SemanticRetrievalTrace, trace.id) is None
    surviving_attempt = await db_session.get(SemanticQaAttempt, attempt.id)
    assert surviving_attempt is not None
    assert surviving_attempt.status == "consumed"
    assert surviving_attempt.outcome == "answered"
    for ledger in (unit_ledger, query_ledger, synthesis_ledger):
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash is None
        assert row.response_hash is None
        assert row.cost_usd == Decimal("0.001234")

    assert await db_session.get(SemanticRetrievalUnit, other_unit.id) is not None
    assert await db_session.get(SemanticRetrievalTrace, other_trace.id) is not None
    assert await db_session.get(SemanticQaAttempt, other_attempt.id) is not None
    for ledger in (other_unit_ledger, other_query_ledger, other_synthesis_ledger):
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash == "a" * 64
        assert row.response_hash == "b" * 64


async def test_message_hash_forget_purges_matching_units_across_messages(db_session) -> None:
    from bot.db.models import LlmUsageLedger, SemanticRetrievalUnit
    from bot.services.forget_cascade import run_cascade_worker_once

    shared_hash = "d" * 64
    target_units = []
    target_ledgers = []
    for _ in range(2):
        owner_id = await _make_user(db_session)
        message, versions = await _make_message(
            db_session,
            user_id=owner_id,
            content_hashes=(shared_hash,),
        )
        ledger = await _make_ledger(db_session)
        target_ledgers.append(ledger)
        target_units.append(
            await _make_unit(
                db_session,
                source_type="message",
                source_id=str(versions[0].id),
                chat_id=message.chat_id,
                message_version_ids=[versions[0].id],
                ledger_id=ledger.id,
            )
        )

    await _make_forget_event(
        db_session,
        target_type="message_hash",
        target_id=shared_hash,
        tombstone_key=f"message_hash:{shared_hash}:semantic-test",
    )
    stats = await run_cascade_worker_once(db_session)

    assert stats == {"claimed": 1, "processed": 1, "failed": 0}
    for unit in target_units:
        assert await db_session.get(SemanticRetrievalUnit, unit.id) is None
    for ledger in target_ledgers:
        row = await db_session.get(LlmUsageLedger, ledger.id, populate_existing=True)
        assert row.prompt_hash is None
        assert row.response_hash is None
