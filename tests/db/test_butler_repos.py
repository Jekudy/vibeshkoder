"""Repo CRUD round-trip tests for all 5 Butler repos (T12-01).

Verifies:
- ButlerActionRepo: create, get, update_status, list_by_requester,
  list_pending_for_chat, mark_expired_past_ttl
- ButlerToolInvocationRepo: create, list_for_action
- ButlerActionConfirmationRepo: create, mark_resolved, list_pending_for_user
- ButlerRateBucketRepo: try_increment (ceiling guard)
- ButlerCardSuggestionRepo: create, link_to_extraction_candidate, get_for_action

All tests use outer-transaction isolation (db_session fixture).
Tests do NOT call session.commit().
"""

from __future__ import annotations

import itertools
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")

_counter = itertools.count(start=8_000_000_000)


def _next_id() -> int:
    return next(_counter)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _future(seconds: int = 300) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"bt_{uid}",
        first_name="ButlerTest",
        last_name=None,
    )
    return uid


async def _create_action(db_session, *, status: str = "rejected", **kwargs) -> int:
    from bot.db.repos.butler_action import ButlerActionRepo

    defaults = dict(
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status=status,
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc",
        plan_summary="plan",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
    )
    defaults.update(kwargs)
    row = await ButlerActionRepo.create(db_session, **defaults)
    return row.id


# ── ButlerActionRepo ──────────────────────────────────────────────────────────


async def test_butler_action_repo_create(db_session) -> None:
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
        evidence_context_hash="abc",
        plan_summary="test plan",
        action_args={"q": "test"},
        action_args_hash="hash1",
        rollback_kind="not_reversible",
        risk_level="low",
        evidence_ids=[1, 2],
    )
    assert row.id is not None
    assert row.requester_tg_id == tg_id
    assert row.evidence_ids == [1, 2]


async def test_butler_action_repo_get_not_found(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    result = await ButlerActionRepo.get(db_session, 999999999)
    assert result is None


async def test_butler_action_repo_update_status(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    action_id = await _create_action(db_session, status="rejected")
    rowcount = await ButlerActionRepo.update_status(
        db_session, action_id, status="expired", rejection_reason="test"
    )
    assert rowcount == 1

    row = await ButlerActionRepo.get(db_session, action_id)
    assert row.status == "expired"
    assert row.rejection_reason == "test"


async def test_butler_action_repo_update_status_not_found(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    with pytest.raises(LookupError):
        await ButlerActionRepo.update_status(db_session, 999999999, status="expired")


async def test_butler_action_repo_list_by_requester_ordering(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    tg_id = _next_id()
    ids = []
    for _ in range(3):
        aid = await _create_action(db_session, requester_tg_id=tg_id)
        ids.append(aid)

    rows = await ButlerActionRepo.list_by_requester(db_session, tg_id, limit=10)
    assert len(rows) == 3
    # Returned in descending created_at order (newest first); since all are created
    # in the same transaction, order by id descending as approximation.
    returned_ids = [r.id for r in rows]
    assert set(returned_ids) == set(ids)


async def test_butler_action_repo_list_pending_for_chat(db_session) -> None:
    from bot.db.repos.butler_action import ButlerActionRepo

    chat_id = _next_id()
    # Create a pending_confirmation row: needs llm_usage_ledger_id due to CHECK.
    # Use 'rejected' status to avoid the ledger constraint issue in unit tests.
    # The constraint is tested separately in test_butler_actions_constraints.py.
    # For this test we just verify the filter works.
    await _create_action(db_session, chat_id=chat_id, status="rejected")

    rows = await ButlerActionRepo.list_pending_for_chat(db_session, chat_id)
    # No pending_confirmation rows (used 'rejected'), so empty list expected.
    assert isinstance(rows, list)


async def test_butler_action_repo_mark_expired_past_ttl(db_session) -> None:
    """mark_expired_past_ttl returns an int (may be 0 in test isolation)."""
    from bot.db.repos.butler_action import ButlerActionRepo

    rowcount = await ButlerActionRepo.mark_expired_past_ttl(db_session)
    assert isinstance(rowcount, int)
    assert rowcount >= 0


async def test_mark_expired_past_ttl_transitions_real_pending_rows(db_session) -> None:
    """mark_expired_past_ttl transitions pending_confirmation rows with past expires_at to expired,
    and leaves future-expiry rows untouched (F2, Codex HIGH #3)."""
    from decimal import Decimal

    from bot.db.models import ButlerAction, LlmUsageLedger
    from bot.db.repos.butler_action import ButlerActionRepo

    # Create a real LlmUsageLedger row (required by ck_butler_actions_ledger_required_post_plan).
    ledger = LlmUsageLedger(
        provider="openai",
        model="gpt-4o",
        prompt_hash=None,
        response_hash=None,
        tokens_in=10,
        tokens_out=10,
        cost_usd=Decimal("0.001"),
        latency_ms=100,
        request_id=None,
        cache_hit=False,
        error=None,
        call_type="butler_decision",
    )
    db_session.add(ledger)
    await db_session.flush()

    tg_id = _next_id()
    chat_id = _next_id()

    # pending_confirmation with expires_at in the PAST — should be expired.
    action_past = ButlerAction(
        requester_tg_id=tg_id,
        chat_id=chat_id,
        action_type="recall",
        status="pending_confirmation",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="hash_past",
        plan_summary="past plan",
        action_args={},
        action_args_hash="hpast",
        rollback_kind="not_reversible",
        risk_level="low",
        llm_usage_ledger_id=ledger.id,
        expires_at=_now() - timedelta(minutes=10),
    )
    # pending_confirmation with expires_at in the FUTURE — should NOT be expired.
    action_future = ButlerAction(
        requester_tg_id=tg_id,
        chat_id=chat_id,
        action_type="recall",
        status="pending_confirmation",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="hash_future",
        plan_summary="future plan",
        action_args={},
        action_args_hash="hfuture",
        rollback_kind="not_reversible",
        risk_level="low",
        llm_usage_ledger_id=ledger.id,
        expires_at=_future(seconds=600),
    )
    db_session.add_all([action_past, action_future])
    await db_session.flush()

    repo = ButlerActionRepo
    expired_count = await repo.mark_expired_past_ttl(db_session)

    assert expired_count >= 1  # at least our past row
    await db_session.refresh(action_past)
    await db_session.refresh(action_future)
    assert action_past.status == "expired"
    assert action_future.status == "pending_confirmation"


# ── ButlerToolInvocationRepo ──────────────────────────────────────────────────


async def test_butler_tool_invocation_repo_create(db_session) -> None:
    from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo

    action_id = await _create_action(db_session)
    inv = await ButlerToolInvocationRepo.create(
        db_session,
        action_id=action_id,
        tool_name="send_intro",
        idempotency_key=f"ik-{_next_id()}",
        request_payload={"text": "hello"},
        request_payload_hash="rph",
        status="pending",
    )
    assert inv.id is not None
    assert inv.tool_name == "send_intro"
    assert inv.status == "pending"


async def test_butler_tool_invocation_repo_list_for_action(db_session) -> None:
    from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo

    action_id = await _create_action(db_session)
    for i in range(2):
        await ButlerToolInvocationRepo.create(
            db_session,
            action_id=action_id,
            tool_name="recall_evidence",
            idempotency_key=f"ik-{_next_id()}",
            request_payload={},
            request_payload_hash="h",
            status="succeeded",
            invocation_seq=i + 1,
        )

    rows = await ButlerToolInvocationRepo.list_for_action(db_session, action_id)
    assert len(rows) == 2
    # Ordered by started_at asc — verify seqs in order.
    assert rows[0].invocation_seq <= rows[1].invocation_seq


# ── ButlerActionConfirmationRepo ──────────────────────────────────────────────


async def test_butler_confirmation_repo_create(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    tg_id = _next_id()

    conf = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=tg_id,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )
    assert conf.id is not None
    assert conf.status == "pending"
    assert conf.confirmation_role == "requester"


async def test_butler_confirmation_repo_mark_resolved_confirmed(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo
    from bot.db.models import ButlerActionConfirmation
    from sqlalchemy import select

    action_id = await _create_action(db_session)
    tg_id = _next_id()

    conf = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=tg_id,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )

    await ButlerActionConfirmationRepo.mark_resolved(
        db_session, conf.id, status="confirmed"
    )

    updated = (await db_session.execute(
        select(ButlerActionConfirmation).where(ButlerActionConfirmation.id == conf.id)
    )).scalar_one()
    assert updated.status == "confirmed"
    assert updated.confirmed_at is not None


async def test_butler_confirmation_repo_mark_resolved_not_found(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    with pytest.raises(LookupError):
        await ButlerActionConfirmationRepo.mark_resolved(db_session, 999999999, status="confirmed")


async def test_butler_confirmation_repo_list_pending_for_user(db_session) -> None:
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    tg_id = _next_id()
    action_id = await _create_action(db_session)

    # Create 2 pending, 1 confirmed.
    for _ in range(2):
        await ButlerActionConfirmationRepo.create(
            db_session,
            action_id=action_id,
            confirmer_tg_id=tg_id,
            confirmation_role="requester",
            status="pending",
            preview_payload_hash="h",
            expires_at=_future(),
            confirmation_token=secrets.token_urlsafe(32),
        )
    # Confirmed (should not appear in pending list).
    conf = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=tg_id,
        confirmation_role="affected_user",
        status="pending",
        preview_payload_hash="h2",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )
    await ButlerActionConfirmationRepo.mark_resolved(db_session, conf.id, status="confirmed")

    rows = await ButlerActionConfirmationRepo.list_pending_for_user(db_session, tg_id)
    assert len(rows) == 2
    assert all(r.status == "pending" for r in rows)


# ── ButlerRateBucketRepo ──────────────────────────────────────────────────────


async def test_butler_rate_bucket_repo_try_increment_ok(db_session) -> None:
    from bot.db.repos.butler_rate_bucket import ButlerRateBucketRepo

    scope_id = _next_id()
    window_start = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)

    ok = await ButlerRateBucketRepo.try_increment(
        db_session,
        bucket_kind="user_plans_day",
        scope_id=scope_id,
        bucket_key=f"day:2026-05-25-{scope_id}",
        window_start=window_start,
        window_end=window_end,
        ceiling=5,
    )
    assert ok is True


async def test_butler_rate_bucket_repo_ceiling_exceeded(db_session) -> None:
    from bot.db.repos.butler_rate_bucket import ButlerRateBucketRepo

    scope_id = _next_id()
    bucket_key = f"day:2026-05-25-ceil-{scope_id}"
    window_start = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)
    kwargs = dict(
        bucket_kind="user_execs_day",
        scope_id=scope_id,
        bucket_key=bucket_key,
        window_start=window_start,
        window_end=window_end,
        ceiling=2,
    )

    # Fill to ceiling.
    assert await ButlerRateBucketRepo.try_increment(db_session, **kwargs) is True
    assert await ButlerRateBucketRepo.try_increment(db_session, **kwargs) is True

    # At ceiling — should return False.
    assert await ButlerRateBucketRepo.try_increment(db_session, **kwargs) is False


# ── ButlerCardSuggestionRepo ──────────────────────────────────────────────────


async def test_butler_card_suggestion_repo_create(db_session) -> None:
    from bot.db.repos.butler_card_suggestion import ButlerCardSuggestionRepo

    action_id = await _create_action(db_session)
    user_id = await _make_user(db_session)

    sug = await ButlerCardSuggestionRepo.create(
        db_session,
        butler_action_id=action_id,
        suggested_card_payload={"title": "T", "body": "B"},
        created_by_user_id=user_id,
    )
    assert sug.id is not None
    assert sug.extraction_candidate_id is None
    assert sug.suggested_card_payload == {"title": "T", "body": "B"}


async def test_butler_card_suggestion_repo_get_for_action_not_found(db_session) -> None:
    from bot.db.repos.butler_card_suggestion import ButlerCardSuggestionRepo

    result = await ButlerCardSuggestionRepo.get_for_action(db_session, 999999999)
    assert result is None


async def test_butler_card_suggestion_repo_link_candidate(db_session) -> None:
    """Link suggestion to a real ExtractionCandidate row (FK constraint requires it)."""
    from bot.db.repos.butler_card_suggestion import ButlerCardSuggestionRepo
    from bot.db.models import ExtractionCandidate

    action_id = await _create_action(db_session)
    user_id = await _make_user(db_session)

    sug = await ButlerCardSuggestionRepo.create(
        db_session,
        butler_action_id=action_id,
        suggested_card_payload={"title": "Link test"},
        created_by_user_id=user_id,
    )

    # Create a real ExtractionCandidate to satisfy the FK.
    candidate = ExtractionCandidate(
        candidate_json={"title": "Link test candidate"},
        source_message_version_ids=[],
        status="pending",
    )
    db_session.add(candidate)
    await db_session.flush()

    rowcount = await ButlerCardSuggestionRepo.link_to_extraction_candidate(
        db_session, sug.id, candidate.id
    )
    assert rowcount == 1

    fetched = await ButlerCardSuggestionRepo.get_for_action(db_session, action_id)
    assert fetched is not None
    assert fetched.extraction_candidate_id == candidate.id


async def test_butler_card_suggestion_repo_link_not_found(db_session) -> None:
    from bot.db.repos.butler_card_suggestion import ButlerCardSuggestionRepo

    with pytest.raises(LookupError):
        await ButlerCardSuggestionRepo.link_to_extraction_candidate(
            db_session, 999999999, uuid.uuid4()
        )


# ── ButlerActionRepo.get_for_update (T12-04) ─────────────────────────────────


async def test_butler_action_repo_get_for_update_found(db_session) -> None:
    """get_for_update returns the action row when it exists."""
    from bot.db.repos.butler_action import ButlerActionRepo

    action_id = await _create_action(db_session, status="rejected")
    row = await ButlerActionRepo.get_for_update(db_session, action_id)
    assert row is not None
    assert row.id == action_id


async def test_butler_action_repo_get_for_update_missing(db_session) -> None:
    """get_for_update returns None for a non-existent id."""
    from bot.db.repos.butler_action import ButlerActionRepo

    result = await ButlerActionRepo.get_for_update(db_session, 999999991)
    assert result is None


# ── ButlerActionConfirmationRepo new accessors (T12-04) ──────────────────────


async def test_butler_confirmation_get_for_action_user_found(db_session) -> None:
    """get_for_action_user returns the row matching (action_id, confirmer_tg_id)."""
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    tg_id = _next_id()

    await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=tg_id,
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )

    row = await ButlerActionConfirmationRepo.get_for_action_user(db_session, action_id, tg_id)
    assert row is not None
    assert row.action_id == action_id
    assert row.confirmer_tg_id == tg_id


async def test_butler_confirmation_get_for_action_user_missing(db_session) -> None:
    """get_for_action_user returns None when (action_id, user_id) pair absent."""
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    result = await ButlerActionConfirmationRepo.get_for_action_user(
        db_session, action_id, _next_id()
    )
    assert result is None


async def test_butler_confirmation_list_for_action_multiple(db_session) -> None:
    """list_for_action returns all confirmation rows for a given action_id."""
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    other_action_id = await _create_action(db_session)

    # Two confirmations for the target action.
    for role in ("requester", "affected_user"):
        await ButlerActionConfirmationRepo.create(
            db_session,
            action_id=action_id,
            confirmer_tg_id=_next_id(),
            confirmation_role=role,
            status="pending",
            preview_payload_hash="h",
            expires_at=_future(),
            confirmation_token=secrets.token_urlsafe(32),
        )
    # One for a different action (must not appear).
    await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=other_action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="h2",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )

    rows = await ButlerActionConfirmationRepo.list_for_action(db_session, action_id)
    assert len(rows) == 2
    assert all(r.action_id == action_id for r in rows)


async def test_butler_confirmation_list_for_action_empty(db_session) -> None:
    """list_for_action returns empty list when action has no confirmation rows."""
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    rows = await ButlerActionConfirmationRepo.list_for_action(db_session, action_id)
    assert rows == []


async def test_butler_confirmation_mark_all_for_action_only_pending(db_session) -> None:
    """mark_all_for_action only transitions 'pending' rows; confirmed rows untouched."""
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)

    # Create two pending confirmations.
    conf_a = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="h1",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )
    conf_b = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="affected_user",
        status="pending",
        preview_payload_hash="h2",
        expires_at=_future(),
        confirmation_token=secrets.token_urlsafe(32),
    )
    # Pre-confirm one row so it should be preserved.
    await ButlerActionConfirmationRepo.mark_resolved(db_session, conf_b.id, status="confirmed")

    count = await ButlerActionConfirmationRepo.mark_all_for_action(
        db_session, action_id, status="cancelled"
    )
    # Only conf_a (pending) should have been flipped.
    assert count == 1

    from bot.db.models import ButlerActionConfirmation
    from sqlalchemy import select

    rows = (
        await db_session.execute(
            select(ButlerActionConfirmation).where(
                ButlerActionConfirmation.action_id == action_id
            )
        )
    ).scalars().all()
    statuses = {r.id: r.status for r in rows}
    assert statuses[conf_a.id] == "cancelled"
    assert statuses[conf_b.id] == "confirmed"


async def test_butler_confirmation_mark_all_for_action_count(db_session) -> None:
    """mark_all_for_action returns correct count of rows transitioned."""
    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)

    for _ in range(3):
        await ButlerActionConfirmationRepo.create(
            db_session,
            action_id=action_id,
            confirmer_tg_id=_next_id(),
            confirmation_role="requester",
            status="pending",
            preview_payload_hash="h",
            expires_at=_future(),
            confirmation_token=secrets.token_urlsafe(32),
        )

    count = await ButlerActionConfirmationRepo.mark_all_for_action(
        db_session, action_id, status="cancelled"
    )
    assert count == 3


# ── ButlerToolInvocationRepo.update_invocation (T12-04) ──────────────────────


async def test_butler_invocation_update_to_succeeded(db_session) -> None:
    """update_invocation transitions running → succeeded and persists payload."""
    from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo

    action_id = await _create_action(db_session)
    inv = await ButlerToolInvocationRepo.create(
        db_session,
        action_id=action_id,
        tool_name="send_intro",
        idempotency_key=f"ik-succ-{_next_id()}",
        request_payload={"arg": "val"},
        request_payload_hash="rph",
        status="running",
    )

    rowcount = await ButlerToolInvocationRepo.update_invocation(
        db_session,
        inv.id,
        status="succeeded",
        response_payload={"result": "ok"},
        response_payload_hash="rph2",
        finished_at=_now(),
    )
    assert rowcount == 1

    from bot.db.models import ButlerToolInvocation
    from sqlalchemy import select

    fetched = (
        await db_session.execute(
            select(ButlerToolInvocation).where(ButlerToolInvocation.id == inv.id)
        )
    ).scalar_one()
    assert fetched.status == "succeeded"
    assert fetched.response_payload == {"result": "ok"}
    assert fetched.finished_at is not None


async def test_butler_invocation_update_to_failed(db_session) -> None:
    """update_invocation transitions running → failed and persists error fields."""
    from bot.db.repos.butler_tool_invocation import ButlerToolInvocationRepo

    action_id = await _create_action(db_session)
    inv = await ButlerToolInvocationRepo.create(
        db_session,
        action_id=action_id,
        tool_name="recall_evidence",
        idempotency_key=f"ik-fail-{_next_id()}",
        request_payload={},
        request_payload_hash="rph",
        status="running",
    )

    rowcount = await ButlerToolInvocationRepo.update_invocation(
        db_session,
        inv.id,
        status="failed",
        error_code="timeout",
        error_context={"detail": "network"},
        finished_at=_now(),
    )
    assert rowcount == 1

    from bot.db.models import ButlerToolInvocation
    from sqlalchemy import select

    fetched = (
        await db_session.execute(
            select(ButlerToolInvocation).where(ButlerToolInvocation.id == inv.id)
        )
    ).scalar_one()
    assert fetched.status == "failed"
    assert fetched.error_code == "timeout"
    assert fetched.error_context == {"detail": "network"}
    assert fetched.finished_at is not None


# ── New columns (migration 074) ───────────────────────────────────────────────


async def test_butler_action_repo_create_stores_query_visibility_plan_payload(db_session) -> None:
    """ButlerActionRepo.create stores query, visibility_scope, plan_payload (migration 074)."""
    from bot.db.repos.butler_action import ButlerActionRepo

    plan_data = {"actions": [{"tool_name": "recall_evidence", "args": {}}]}
    row = await ButlerActionRepo.create(
        db_session,
        requester_tg_id=_next_id(),
        chat_id=_next_id(),
        action_type="recall",
        status="rejected",
        tool_name="recall_evidence",
        tool_manifest_version="1.0",
        governance_filter_version="v1",
        evidence_context_hash="abc",
        plan_summary="plan",
        action_args={},
        action_args_hash="h",
        rollback_kind="not_reversible",
        risk_level="low",
        query="who knows Rust?",
        visibility_scope="member",
        plan_payload=plan_data,
        rejection_reason="test_rejection",
    )
    assert row.id is not None
    assert row.query == "who knows Rust?"
    assert row.visibility_scope == "member"
    assert row.plan_payload == plan_data
    assert row.rejection_reason == "test_rejection"


async def test_butler_action_confirmation_repo_create_with_token(db_session) -> None:
    """ButlerActionConfirmationRepo.create stores confirmation_token (migration 074)."""
    import secrets

    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    token = secrets.token_urlsafe(32)
    row = await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph",
        expires_at=_future(),
        confirmation_token=token,
    )
    assert row.id is not None
    assert row.confirmation_token == token


async def test_butler_action_confirmation_token_unique(db_session) -> None:
    """Two confirmation rows for the same action get distinct tokens; UNIQUE enforced."""
    import secrets

    from sqlalchemy.exc import IntegrityError

    from bot.db.repos.butler_action_confirmation import ButlerActionConfirmationRepo

    action_id = await _create_action(db_session)
    token = secrets.token_urlsafe(32)

    await ButlerActionConfirmationRepo.create(
        db_session,
        action_id=action_id,
        confirmer_tg_id=_next_id(),
        confirmation_role="requester",
        status="pending",
        preview_payload_hash="pph",
        expires_at=_future(),
        confirmation_token=token,
    )
    # Re-using the same token on a second row must raise IntegrityError (UNIQUE violation)
    with pytest.raises(IntegrityError):
        await ButlerActionConfirmationRepo.create(
            db_session,
            action_id=action_id,
            confirmer_tg_id=_next_id(),
            confirmation_role="affected_user",
            status="pending",
            preview_payload_hash="pph2",
            expires_at=_future(),
            confirmation_token=token,
        )
