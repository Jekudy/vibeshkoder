"""ORM round-trip tests for all 5 Butler models (T12-01).

Verifies:
- Create + query (insert and fetch by PK)
- Update fields
- FK relationships between models
- JSON column round-trips

All tests use outer-transaction isolation (db_session fixture).
Tests do NOT call session.commit().
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=7_700_000_000)


def _next_id() -> int:
    return next(_counter)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _future(seconds: int = 300) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"butler_test_{uid}",
        first_name="Butler",
        last_name=None,
    )
    return uid


async def _make_butler_action(db_session) -> tuple[int, int]:
    """Create a minimal ButlerAction in 'rejected' status (no ledger required).
    Returns (action_id, requester_tg_id).
    """
    from bot.db.repos.butler_action import ButlerActionRepo

    tg_id = _next_id()
    row = await ButlerActionRepo.create(
        db_session,
        requester_tg_id=tg_id,
        chat_id=_next_id(),
        action_type="recall",
        status="rejected",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc123",
        plan_summary="test plan",
        action_args={"query": "test"},
        action_args_hash="hashval",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    return row.id, tg_id


# ── ButlerAction ──────────────────────────────────────────────────────────────


async def test_butler_action_create_and_fetch(db_session) -> None:
    from bot.db.models import ButlerAction
    from sqlalchemy import select

    tg_id = _next_id()
    chat_id = _next_id()

    row = ButlerAction(
        requester_tg_id=tg_id,
        chat_id=chat_id,
        action_type="recall",
        status="rejected",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc123",
        evidence_ids=[1, 2, 3],
        approved_card_source_ids=[],
        plan_summary="test plan",
        action_args={"query": "test"},
        action_args_hash="hashval",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    db_session.add(row)
    await db_session.flush()

    assert row.id is not None
    assert row.action_uuid is not None

    fetched = (await db_session.execute(
        select(ButlerAction).where(ButlerAction.id == row.id)
    )).scalar_one()

    assert fetched.requester_tg_id == tg_id
    assert fetched.chat_id == chat_id
    assert fetched.action_type == "recall"
    assert fetched.status == "rejected"
    assert fetched.evidence_ids == [1, 2, 3]
    assert fetched.action_args == {"query": "test"}


async def test_butler_action_update_status(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    action_id, _ = await _make_butler_action(db_session)
    rowcount = await ButlerActionRepo.update_status(
        db_session, action_id, status="expired", rejection_reason="test_reason"
    )
    assert rowcount == 1

    row = await ButlerActionRepo.get(db_session, action_id)
    assert row is not None
    assert row.status == "expired"
    assert row.rejection_reason == "test_reason"


async def test_butler_action_list_by_requester(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    tg_id = _next_id()
    for _ in range(3):
        await ButlerActionRepo.create(
            db_session,
            requester_tg_id=tg_id,
            chat_id=_next_id(),
            action_type="recall",
            status="rejected",
            tool_name="recall_evidence",
            tool_manifest_version="1.0",
            governance_filter_version="v1",
            evidence_context_hash="x",
            plan_summary="p",
            action_args={},
            action_args_hash="h",
            rollback_kind="not_reversible",
            risk_level="low",
        )

    rows = await ButlerActionRepo.list_by_requester(db_session, tg_id)
    assert len(rows) == 3
    # newest first
    for r in rows:
        assert r.requester_tg_id == tg_id


# ── ButlerToolInvocation ──────────────────────────────────────────────────────


async def test_butler_tool_invocation_create_and_list(db_session) -> None:
    from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo

    action_id, _ = await _make_butler_action(db_session)

    inv = await ButlerToolInvocationRepo.create(
        db_session,
        action_id=action_id,
        tool_name="recall_evidence",
        idempotency_key=f"key-{_next_id()}",
        request_payload={"q": "hello"},
        request_payload_hash="rph1",
        status="pending",
    )
    assert inv.id is not None
    assert inv.invocation_seq == 1

    rows = await ButlerToolInvocationRepo.list_for_action(db_session, action_id)
    assert len(rows) == 1
    assert rows[0].tool_name == "recall_evidence"
    assert rows[0].request_payload == {"q": "hello"}


async def test_butler_tool_invocation_fk_to_action(db_session) -> None:
    from bot.db.models import ButlerToolInvocation
    from sqlalchemy import select

    action_id, _ = await _make_butler_action(db_session)

    inv = ButlerToolInvocation(
        action_id=action_id,
        tool_name="send_intro",
        invocation_seq=1,
        idempotency_key=f"ikey-{_next_id()}",
        request_payload={"text": "hello"},
        request_payload_hash="h1",
        status="succeeded",
    )
    db_session.add(inv)
    await db_session.flush()

    fetched = (await db_session.execute(
        select(ButlerToolInvocation).where(ButlerToolInvocation.id == inv.id)
    )).scalar_one()
    assert fetched.action_id == action_id


# ── ButlerActionConfirmation ──────────────────────────────────────────────────


async def test_butler_action_confirmation_create_and_resolve(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id, tg_id = await _make_butler_action(db_session)

    conf = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=tg_id,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph1",
        expires_at=_future(),
    )
    assert conf.id is not None
    assert conf.status == "pending"

    rowcount = await ButlerActionConfirmationRepo.mark_resolved(
        db_session, conf.id, status="confirmed"
    )
    assert rowcount == 1

    from sqlalchemy import select
    from bot.db.models import ButlerActionConfirmation
    updated = (await db_session.execute(
        select(ButlerActionConfirmation).where(ButlerActionConfirmation.id == conf.id)
    )).scalar_one()
    assert updated.status == "confirmed"
    assert updated.confirmed_at is not None


async def test_butler_action_confirmation_list_pending_for_user(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id, tg_id = await _make_butler_action(db_session)

    await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=tg_id,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph2",
        expires_at=_future(),
    )

    rows = await ButlerActionConfirmationRepo.list_pending_for_user(db_session, tg_id)
    assert len(rows) == 1
    assert rows[0].confirmer_tg_id == tg_id


# ── ButlerRateBucket ──────────────────────────────────────────────────────────


async def test_butler_rate_bucket_increment_and_ceiling(db_session) -> None:
    from bot.db.repos.butler_rate_bucket import ButlerRateBucketRepo

    scope_id = _next_id()
    bucket_key = f"day:2026-05-25-{scope_id}"
    window_start = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)

    # First increment: should succeed (new row, count=1).
    ok = await ButlerRateBucketRepo.try_increment(
        db_session,
        bucket_kind="user_plans_day",
        scope_id=scope_id,
        bucket_key=bucket_key,
        window_start=window_start,
        window_end=window_end,
        ceiling=2,
    )
    assert ok is True

    # Second increment: count becomes 2 which equals ceiling → still OK (2 <= 2).
    ok2 = await ButlerRateBucketRepo.try_increment(
        db_session,
        bucket_kind="user_plans_day",
        scope_id=scope_id,
        bucket_key=bucket_key,
        window_start=window_start,
        window_end=window_end,
        ceiling=2,
    )
    assert ok2 is True

    # Third increment: count is already 2 = ceiling → should return False.
    ok3 = await ButlerRateBucketRepo.try_increment(
        db_session,
        bucket_kind="user_plans_day",
        scope_id=scope_id,
        bucket_key=bucket_key,
        window_start=window_start,
        window_end=window_end,
        ceiling=2,
    )
    assert ok3 is False


# ── ButlerCardSuggestion ──────────────────────────────────────────────────────


async def test_butler_card_suggestion_create_and_fetch(db_session) -> None:
    from bot.db.repos.butler_card_suggestion import ButlerCardSuggestionRepo

    action_id, _ = await _make_butler_action(db_session)
    user_id = await _make_user(db_session)

    suggestion = await ButlerCardSuggestionRepo.create(
        db_session,
        butler_action_id=action_id,
        suggested_card_payload={"title": "Test Card", "body": "Content"},
        created_by_user_id=user_id,
    )
    assert suggestion.id is not None
    assert suggestion.extraction_candidate_id is None

    fetched = await ButlerCardSuggestionRepo.get_for_action(db_session, action_id)
    assert fetched is not None
    assert fetched.suggested_card_payload["title"] == "Test Card"


async def test_butler_card_suggestion_link_candidate(db_session) -> None:
    """Link suggestion to a real ExtractionCandidate row (FK constraint requires it)."""
    from bot.db.repos.butler_card_suggestion import ButlerCardSuggestionRepo
    from bot.db.models import ExtractionCandidate

    action_id, _ = await _make_butler_action(db_session)
    user_id = await _make_user(db_session)

    suggestion = await ButlerCardSuggestionRepo.create(
        db_session,
        butler_action_id=action_id,
        suggested_card_payload={"title": "Card"},
        created_by_user_id=user_id,
    )

    # Create a real ExtractionCandidate to satisfy the FK.
    candidate = ExtractionCandidate(
        candidate_json={"title": "Test", "body": "body"},
        source_message_version_ids=[],
        status="pending",
    )
    db_session.add(candidate)
    await db_session.flush()

    rowcount = await ButlerCardSuggestionRepo.link_to_extraction_candidate(
        db_session, suggestion.id, candidate.id
    )
    assert rowcount == 1

    from sqlalchemy import select
    from bot.db.models import ButlerCardSuggestion
    updated = (await db_session.execute(
        select(ButlerCardSuggestion).where(ButlerCardSuggestion.id == suggestion.id)
    )).scalar_one()
    assert updated.extraction_candidate_id == candidate.id
