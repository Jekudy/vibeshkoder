"""Phase 6 end-to-end integration test: candidate → card → recall pipeline.

Covers T6-09 acceptance criteria (PHASE6_PLAN.md §7):

* Test 1: full happy-path pipeline — extractor → pending candidate → admin
  approve → approved card with card_sources → /recall returns card hit.
* Test 2: cascade end-to-end — forget the backing message → cascade demotes
  the card to 'archived' → /recall returns no card.
* Test 3: partial-source forget — card backed by 3 sources, forget ONE → card
  stays 'approved' (remaining count > 0) but excluded from /recall via
  T6-06 search-side defense-in-depth (L6a NOT-EXISTS guard).
* Test 4: rejected candidate → no knowledge_cards row created → /recall
  returns no card.

Patterns follow:
  - tests/integration/test_phase4_hotfix_e2e.py  (real Postgres fixture, no mock DB)
  - tests/services/test_extractor.py             (FakeGateway, _make_chat_message helper)
  - tests/services/test_search_cards.py          (card setup helpers)

All tests use the ``db_session`` fixture (real Postgres, outer-transaction
rollback for isolation). The ``run_extraction_pass`` function commits an
ExtractionRun row through its own ``_engine_session`` independently — this is
intentional and mirrors how existing extractor tests work (the committed row is
readable via the test session but not rolled back at teardown; this is the
accepted trade-off for testing the full production code path, per
test_extractor.py precedent).
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

# Counter ranges chosen to not collide with any other test file in this repo.
_user_counter = itertools.count(start=9_700_000_000)
_msg_counter = itertools.count(start=9_700_000)
_chat_counter = itertools.count(start=970_000)


def _next_user() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_counter)


# ─── Test helpers ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SeedMessage:
    chat_message_id: int
    version_id: int
    chat_id: int
    message_id: int
    user_id: int


async def _seed_message(
    db_session,
    *,
    chat_id: int,
    text: str = "тестовый факт для карточки знаний",
    memory_policy: str = "normal",
) -> _SeedMessage:
    """Insert a ChatMessage + MessageVersion pair, wiring current_version_id.

    Follows the pattern in tests/services/test_extractor.py::_make_chat_message.
    Left ``chat_messages.content_hash`` NULL to mirror the live persistence path
    (``MessageRepo.save`` does not populate it; only import does).
    """
    import uuid as _uuid_module

    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo
    from sqlalchemy import update as sa_update

    user_id = _next_user()
    message_id = _next_msg_id()
    when = datetime.now(timezone.utc)

    await UserRepo.upsert(
        db_session,
        telegram_id=user_id,
        username=f"p6tst_{user_id}",
        first_name="P6",
        last_name=None,
    )

    msg = ChatMessage(
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        date=when,
        created_at=when,
        memory_policy=memory_policy,
        is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()

    mv = MessageVersion(
        chat_message_id=msg.id,
        version_seq=1,
        text=text,
        normalized_text=text,
        entities_json={},
        content_hash=f"p6hash{_uuid_module.uuid4().hex[:16]}",
        is_redacted=False,
    )
    db_session.add(mv)
    await db_session.flush()

    await db_session.execute(
        sa_update(ChatMessage)
        .where(ChatMessage.id == msg.id)
        .values(current_version_id=mv.id)
    )
    await db_session.flush()

    return _SeedMessage(
        chat_message_id=msg.id,
        version_id=mv.id,
        chat_id=chat_id,
        message_id=message_id,
        user_id=user_id,
    )


async def _seed_llm_ledger(db_session) -> int:
    """Insert a synthetic llm_usage_ledger row (FK requirement for ExtractionRun)."""
    from bot.db.models import LlmUsageLedger

    led = LlmUsageLedger(
        provider="p6-fake",
        model="p6-test-model",
        prompt_hash=None,
        response_hash=None,
        tokens_in=0,
        tokens_out=0,
    )
    db_session.add(led)
    await db_session.flush()
    return led.id


@dataclass
class _FakeGateway:
    """Minimal Protocol-conforming fake for ExtractCandidatesGateway.

    Mirrors the FakeGateway in tests/services/test_extractor.py. Records
    every call; replays a predetermined list of candidates and a synthetic
    llm_usage_ledger_id.
    """

    candidates_to_emit: list[dict[str, Any]]
    llm_usage_ledger_id: int | None = None
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def extract_candidates(
        self,
        session: Any,
        *,
        source_versions: list[dict[str, Any]],
        prompt_template_version: str = "v0.1.0",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "source_versions": list(source_versions),
                "prompt_template_version": prompt_template_version,
            }
        )
        return {
            "candidates": list(self.candidates_to_emit),
            "llm_usage_ledger_id": self.llm_usage_ledger_id,
        }


async def _run_extraction(
    db_session,
    *,
    seed: _SeedMessage,
    card_body: str,
    ledger_id: int,
) -> tuple[Any, Any]:
    """Run extractor via ``_force_include_chat_message_ids`` and return (result, candidate).

    Uses the test-only ``_force_include_chat_message_ids`` hook so the
    extraction window does not need to encompass a real time span. The seeded
    message is included by id regardless of ``created_at``.

    Returns ``(ExtractionResult, ExtractionCandidate)`` — the result dataclass
    and the first candidate ORM row.
    """
    from bot.db.models import ExtractionCandidate
    from bot.services.extractor import run_extraction_pass
    from sqlalchemy import select

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    window_end = datetime.now(timezone.utc) + timedelta(hours=1)

    gw = _FakeGateway(
        candidates_to_emit=[
            {
                "candidate_json": {
                    "title": "Тестовая карточка",
                    "body_markdown": card_body,
                },
                "source_message_version_ids": [seed.version_id],
            }
        ],
        llm_usage_ledger_id=ledger_id,
    )

    result = await run_extraction_pass(
        db_session,
        window_start=window_start,
        window_end=window_end,
        gateway=gw,
        _force_include_chat_message_ids=[seed.chat_message_id],
    )

    # Fetch the candidate the extractor wrote.
    cands = (
        await db_session.execute(
            select(ExtractionCandidate).where(
                ExtractionCandidate.extraction_run_id == result.extraction_run_id
            )
        )
    ).scalars().all()

    return result, cands[0] if cands else None


async def _approve_candidate(
    db_session,
    *,
    candidate_id: uuid.UUID,
    mvids: list[int],
    admin_user_id: int,
    card_body: str,
    card_title: str = "Тестовая карточка",
):
    """Approve a pending candidate via service-layer repos (no aiogram handler).

    Implements the same 8-step §5.C protocol the ``/approve`` handler calls,
    but without Telegram message dependencies. Safe to call in tests because
    all writes are flushed to the caller's session (no own-session commits).
    """
    from bot.db.repos.card_source import CardSourceRepo
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo
    from bot.db.repos.extraction_decision import ExtractionDecisionRepo
    from bot.db.repos.knowledge_card import KnowledgeCardRepo
    from bot.services.governance_revalidation import revalidate_sources

    # Step 3: governance re-validation.
    status, payload = await revalidate_sources(db_session, mvids)
    assert status == "ok", f"Governance blocked: {payload}"

    # Step 5: INSERT knowledge_cards.
    card = await KnowledgeCardRepo.create(
        db_session,
        title=card_title,
        body_markdown=card_body,
        approved_by_user_id=admin_user_id,
    )

    # Step 6: INSERT card_sources.
    await CardSourceRepo.bulk_create(
        db_session, card_id=card.id, message_version_ids=mvids
    )

    # Step 7: UPDATE candidate status.
    await ExtractionCandidateRepo.mark_status(
        db_session,
        candidate_id=candidate_id,
        status="approved",
        reviewed_by=admin_user_id,
    )

    # Step 8: INSERT extraction_decisions.
    await ExtractionDecisionRepo.create(
        db_session,
        candidate_id=candidate_id,
        action="approved",
        decided_by=admin_user_id,
        decided_by_username=f"p6admin{admin_user_id}",
        reason=None,
    )

    return card


async def _create_forget_event_completed(
    db_session,
    *,
    chat_id: int,
    message_id: int,
    chat_message_id: int,
) -> Any:
    """Create a completed forget event targeting a specific chat message.

    Uses the canonical tombstone_key format ``message:<chat_id>:<message_id>``
    per forget_cascade.py tombstone conventions.
    """
    from bot.db.repos.forget_event import ForgetEventRepo

    tombstone_key = f"message:{chat_id}:{message_id}"
    event = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(chat_message_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    await ForgetEventRepo.mark_status(db_session, event.id, status="processing")
    # Note: NOT marking as "completed" here — the cascade worker will do that.
    # The event needs to be "processing" so the cascade worker can claim it.
    # But wait — run_cascade_worker_once expects "pending" events.
    # Reset to pending so cascade worker can claim it.
    return event


# ─── Test 1: full happy-path pipeline ─────────────────────────────────────────


async def test_phase6_full_pipeline_candidate_to_card_to_recall(db_session) -> None:
    """Full candidate → approved card → /recall returns card hit.

    Steps:
    1. Seed: chat_message with memory_policy='normal'.
    2. Run extractor: ExtractionRun='completed', ExtractionCandidate='pending'.
    3. Admin approve: knowledge_cards='approved' + card_sources rows.
    4. /recall: card hit returned, source_type='card', card_id set,
       card_source_message_version_ids non-empty (includes anchor mvid).
    5. EvidenceBundle.evidence_ids includes the anchor mvid.
    """
    from bot.db.models import ExtractionRun
    from bot.db.repos.user import UserRepo
    from bot.services.qa import run_qa
    from sqlalchemy import select

    chat_id = _next_chat_id()
    card_body = "питон используется для анализа данных и машинного обучения"

    # Step 1: seed source message.
    seed = await _seed_message(db_session, chat_id=chat_id, text="питон факт для карточки")
    ledger_id = await _seed_llm_ledger(db_session)

    # Step 2: run extractor → ExtractionRun='completed', candidate='pending'.
    ext_result, candidate = await _run_extraction(
        db_session, seed=seed, card_body=card_body, ledger_id=ledger_id
    )

    assert ext_result.run_status == "completed", (
        f"Expected run_status='completed', got {ext_result.run_status!r}"
    )
    assert ext_result.candidate_count == 1

    # ExtractionRun row is readable via the session (committed by _engine_session).
    run_row = await db_session.get(ExtractionRun, ext_result.extraction_run_id)
    assert run_row is not None
    assert run_row.run_status == "completed"

    assert candidate is not None
    assert candidate.status == "pending"
    assert seed.version_id in candidate.source_message_version_ids

    # Step 3: admin approves via service-layer protocol.
    admin_user_id = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=admin_user_id,
        username=f"p6admin{admin_user_id}",
        first_name="Admin",
        last_name=None,
    )

    card = await _approve_candidate(
        db_session,
        candidate_id=candidate.id,
        mvids=[seed.version_id],
        admin_user_id=admin_user_id,
        card_body=card_body,
    )

    # Assert card is approved with FK sources.
    assert card.id is not None
    assert card.card_status == "approved"

    # Refresh candidate to see updated status.
    await db_session.refresh(candidate)
    assert candidate.status == "approved"

    # Assert card_sources row exists.
    from bot.db.models import CardSource
    sources = (
        await db_session.execute(
            select(CardSource).where(CardSource.card_id == card.id)
        )
    ).scalars().all()
    assert len(sources) == 1
    assert sources[0].message_version_id == seed.version_id

    # Step 4: /recall returns card hit.
    qa_result = await run_qa(
        db_session,
        query="питон",
        chat_id=chat_id,
        redact_query_in_audit=False,
    )

    assert not qa_result.bundle.abstained, "Expected /recall to return results, not abstain"

    card_hits = [
        item for item in qa_result.bundle.items if item.source_type == "card"
    ]
    assert len(card_hits) >= 1, (
        f"Expected at least one card hit in /recall result, got items: "
        f"{[(i.source_type, i.card_id) for i in qa_result.bundle.items]}"
    )

    hit = card_hits[0]
    assert hit.card_id == card.id, (
        f"Expected card_id={card.id}, got {hit.card_id}"
    )
    assert len(hit.card_source_message_version_ids) >= 1, (
        "card_source_message_version_ids must be non-empty"
    )
    assert seed.version_id in hit.card_source_message_version_ids, (
        f"Anchor mvid {seed.version_id} must appear in card_source_message_version_ids "
        f"{hit.card_source_message_version_ids}"
    )

    # Step 5: EvidenceBundle.evidence_ids includes the anchor mvid (Phase 5 gateway contract).
    assert seed.version_id in qa_result.bundle.evidence_ids, (
        f"EvidenceBundle.evidence_ids {qa_result.bundle.evidence_ids} must include "
        f"anchor mvid {seed.version_id}"
    )


# ─── Test 2: cascade end-to-end (forget → demote → excluded from /recall) ─────


async def test_phase6_cascade_forget_demotes_card_and_excludes_from_recall(
    db_session,
) -> None:
    """Forget the backing source → cascade demotes card → /recall excludes it.

    Steps:
    1. Seed pipeline (same as Test 1) + approve.
    2. Create forget_event targeting the source message.
    3. Run cascade worker → card demoted to 'archived' (§5.A.5 step 5).
    4. /recall → card NOT in results (approved filter in card branch).
    """
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.db.repos.user import UserRepo
    from bot.services.forget_cascade import run_cascade_worker_once
    from bot.services.qa import run_qa

    chat_id = _next_chat_id()
    card_body = "байт это единица информации используется в компьютерах"

    seed = await _seed_message(db_session, chat_id=chat_id, text="байт информация тест")
    ledger_id = await _seed_llm_ledger(db_session)

    ext_result, candidate = await _run_extraction(
        db_session, seed=seed, card_body=card_body, ledger_id=ledger_id
    )
    assert ext_result.run_status == "completed"
    assert candidate is not None

    admin_user_id = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=admin_user_id,
        username=f"p6admin{admin_user_id}",
        first_name="Admin",
        last_name=None,
    )

    card = await _approve_candidate(
        db_session,
        candidate_id=candidate.id,
        mvids=[seed.version_id],
        admin_user_id=admin_user_id,
        card_body=card_body,
    )
    assert card.card_status == "approved"

    # Confirm /recall returns card BEFORE forget.
    qa_before = await run_qa(
        db_session,
        query="байт",
        chat_id=chat_id,
        redact_query_in_audit=False,
    )
    card_hits_before = [
        item for item in qa_before.bundle.items if item.source_type == "card"
    ]
    assert len(card_hits_before) >= 1, "Card must appear in /recall before forget"

    # Step 2: create forget_event for the source message (pending so cascade can claim it).
    tombstone_key = f"message:{seed.chat_id}:{seed.message_id}"
    await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(seed.chat_message_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    await db_session.flush()

    # Step 3: run cascade worker → processes the pending forget_event.
    stats = await run_cascade_worker_once(db_session)
    assert stats["failed"] == 0, f"Cascade worker reported failures: {stats}"
    assert stats["processed"] >= 1, f"Expected at least 1 processed event, got: {stats}"

    # Assert card is now archived.
    await db_session.refresh(card)
    assert card.card_status == "archived", (
        f"Expected card_status='archived' after cascade, got {card.card_status!r}"
    )

    # Step 4: /recall must NOT return the archived card.
    qa_after = await run_qa(
        db_session,
        query="байт",
        chat_id=chat_id,
        redact_query_in_audit=False,
    )
    card_hits_after = [
        item for item in qa_after.bundle.items if item.source_type == "card"
    ]
    assert len(card_hits_after) == 0, (
        f"Expected no card hits after cascade demote, got: "
        f"{[(i.source_type, i.card_id) for i in qa_after.bundle.items]}"
    )


# ─── Test 3: partial-source forget — card stays approved but excluded ──────────


async def test_phase6_partial_source_forget_card_approved_but_excluded_from_recall(
    db_session,
) -> None:
    """Partial forget: 3 sources, forget ONE → card stays 'approved' but excluded.

    §5.A.5 step 5: "remaining count > 0 → leave the card in its current state".
    T6-06 search-side defense: ANY tombstoned source → card excluded from
    search results (L6a NOT-EXISTS guard) regardless of card_status.

    Setup: 3 source messages → 1 card. Forget source #1 via completed tombstone
    (direct insert, not cascade worker) so card_sources rows remain intact.
    /recall must exclude the card.
    """
    from bot.db.models import CardSource, KnowledgeCard
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.db.repos.user import UserRepo
    from bot.services.qa import run_qa
    from sqlalchemy import func as sa_func, select

    chat_id = _next_chat_id()
    card_body = "сервер обрабатывает запросы клиентов по сети"

    # Seed 3 source messages.
    seed1 = await _seed_message(db_session, chat_id=chat_id, text="сервер сеть запрос один")
    seed2 = await _seed_message(db_session, chat_id=chat_id, text="сервер сеть запрос два")
    seed3 = await _seed_message(db_session, chat_id=chat_id, text="сервер сеть запрос три")

    # Create card directly with 3 sources (no extraction pass needed).
    admin_user_id = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=admin_user_id,
        username=f"p6admin{admin_user_id}",
        first_name="Admin",
        last_name=None,
    )

    card = KnowledgeCard(
        title="Тест частичного забвения",
        body_markdown=card_body,
        card_status="approved",
        approved_by_user_id=admin_user_id,
        approved_at=sa_func.now(),
    )
    db_session.add(card)
    await db_session.flush()
    await db_session.refresh(card)

    for position, mvid in enumerate([seed1.version_id, seed2.version_id, seed3.version_id]):
        db_session.add(CardSource(card_id=card.id, message_version_id=mvid, position=position))
    await db_session.flush()

    # Confirm card is approved with 3 sources.
    sources_before = (
        await db_session.execute(
            select(CardSource).where(CardSource.card_id == card.id)
        )
    ).scalars().all()
    assert len(sources_before) == 3

    # /recall returns card BEFORE forget.
    qa_before = await run_qa(
        db_session,
        query="сервер",
        chat_id=chat_id,
        redact_query_in_audit=False,
    )
    card_hits_before = [
        item for item in qa_before.bundle.items if item.source_type == "card"
    ]
    assert len(card_hits_before) >= 1, "Card must appear before any forget"

    # Tombstone source #1 directly as completed (no cascade worker — we keep
    # card_sources intact to test the §5.A.5 "remaining > 0" branch via the
    # search-side NOT-EXISTS guard, not the cascade demote path).
    tombstone_key = f"message:{seed1.chat_id}:{seed1.message_id}"
    event = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(seed1.chat_message_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    await ForgetEventRepo.mark_status(db_session, event.id, status="processing")
    await ForgetEventRepo.mark_status(db_session, event.id, status="completed")
    await db_session.flush()

    # Card must still be 'approved' — cascade did not run, remaining sources = 2.
    await db_session.refresh(card)
    assert card.card_status == "approved", (
        f"Card must stay 'approved' with remaining sources; got {card.card_status!r}"
    )

    # /recall must NOT include the card: T6-06 search-side L6a guard — ANY
    # source tombstoned → card excluded from search results.
    qa_after = await run_qa(
        db_session,
        query="сервер",
        chat_id=chat_id,
        redact_query_in_audit=False,
    )
    card_hits_after = [
        item for item in qa_after.bundle.items if item.source_type == "card"
    ]
    assert len(card_hits_after) == 0, (
        f"Expected no card hits after tombstoning one source (L6a), "
        f"got: {[(i.source_type, i.card_id) for i in qa_after.bundle.items]}"
    )


# ─── Test 4: rejected candidate → no knowledge_cards row created ──────────────


async def test_phase6_rejected_candidate_produces_no_card(db_session) -> None:
    """Reject a pending candidate → ExtractionDecision='rejected', no card.

    Steps:
    1. Seed + run extractor → pending candidate.
    2. Admin rejects via service-layer repos (ExtractionDecision + candidate flip).
    3. Assert no knowledge_cards row created.
    4. /recall returns no card hit.
    """
    from bot.db.models import ExtractionDecision, KnowledgeCard
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo
    from bot.db.repos.extraction_decision import ExtractionDecisionRepo
    from bot.db.repos.user import UserRepo
    from bot.services.qa import run_qa
    from sqlalchemy import select

    chat_id = _next_chat_id()
    card_body = "алгоритм решает задачу за логарифмическое время"

    seed = await _seed_message(db_session, chat_id=chat_id, text="алгоритм сложность логарифм")
    ledger_id = await _seed_llm_ledger(db_session)

    ext_result, candidate = await _run_extraction(
        db_session, seed=seed, card_body=card_body, ledger_id=ledger_id
    )
    assert ext_result.run_status == "completed"
    assert candidate is not None
    assert candidate.status == "pending"

    admin_user_id = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=admin_user_id,
        username=f"p6admin{admin_user_id}",
        first_name="Admin",
        last_name=None,
    )

    # Reject the candidate (mirrors /reject handler logic).
    await ExtractionCandidateRepo.mark_status(
        db_session,
        candidate_id=candidate.id,
        status="rejected",
        reviewed_by=admin_user_id,
    )
    await ExtractionDecisionRepo.create(
        db_session,
        candidate_id=candidate.id,
        action="rejected",
        decided_by=admin_user_id,
        decided_by_username=f"p6admin{admin_user_id}",
        reason="test rejection",
    )

    # Assert ExtractionDecision exists with action='rejected'.
    decision_rows = (
        await db_session.execute(
            select(ExtractionDecision).where(
                ExtractionDecision.candidate_id == candidate.id
            )
        )
    ).scalars().all()
    assert len(decision_rows) == 1
    assert decision_rows[0].action == "rejected"

    # Refresh candidate to confirm status flip.
    await db_session.refresh(candidate)
    assert candidate.status == "rejected"

    # Assert NO knowledge_cards row was created.
    all_cards = (
        await db_session.execute(
            select(KnowledgeCard)
            # Filter by approved_by_user_id to scope to this test's admin only.
            .where(KnowledgeCard.approved_by_user_id == admin_user_id)
        )
    ).scalars().all()
    assert len(all_cards) == 0, (
        f"Rejection must not produce any knowledge_cards; found: {all_cards}"
    )

    # /recall must return no card hit.
    qa_result = await run_qa(
        db_session,
        query="алгоритм",
        chat_id=chat_id,
        redact_query_in_audit=False,
    )
    card_hits = [
        item for item in qa_result.bundle.items if item.source_type == "card"
    ]
    assert len(card_hits) == 0, (
        f"Expected no card hits after rejection; got: {card_hits}"
    )
