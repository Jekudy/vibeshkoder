"""T6-04 acceptance tests — extraction_decisions repo.

PHASE6_PLAN.md §5.A + §5.C step 8: repo backs the ``/approve`` /``/reject``
audit-row write.

Methods covered:

* ``create(session, candidate_id, action, decided_by, decided_by_username,
  reason)`` — insert audit row with username snapshot.
* UNIQUE constraint behaviour: a second decision for the same candidate raises.
"""

from __future__ import annotations

import itertools
import uuid as _uuid_module
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_993_000_000)


def _next_user_id() -> int:
    return next(_user_counter)


async def _make_admin(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user_id()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"admin_{uid}",
        first_name="Admin",
        last_name=None,
    )
    return uid


async def _make_candidate(db_session, *, status: str = "pending") -> _uuid_module.UUID:
    from bot.db.models import ExtractionCandidate, ExtractionRun

    run = ExtractionRun(
        run_status="completed",
        ingestion_window_start=datetime.now(timezone.utc) - timedelta(hours=1),
        ingestion_window_end=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.flush()

    cand = ExtractionCandidate(
        extraction_run_id=run.id,
        candidate_json={"title": "t", "body_markdown": "b"},
        source_message_version_ids=[],
        status=status,
    )
    db_session.add(cand)
    await db_session.flush()
    return cand.id


async def test_create_approved_decision(db_session) -> None:
    """``create`` with action='approved' writes the audit row."""
    from bot.db.repos.extraction_decision import ExtractionDecisionRepo

    cid = await _make_candidate(db_session)
    admin = await _make_admin(db_session)
    decision = await ExtractionDecisionRepo.create(
        db_session,
        candidate_id=cid,
        action="approved",
        decided_by=admin,
        decided_by_username="admin_user",
        reason=None,
    )
    assert decision.id is not None
    assert decision.candidate_id == cid
    assert decision.action == "approved"
    assert decision.decided_by == admin
    assert decision.decided_by_username == "admin_user"
    assert decision.reason is None


async def test_create_rejected_with_reason(db_session) -> None:
    from bot.db.repos.extraction_decision import ExtractionDecisionRepo

    cid = await _make_candidate(db_session)
    admin = await _make_admin(db_session)
    decision = await ExtractionDecisionRepo.create(
        db_session,
        candidate_id=cid,
        action="rejected",
        decided_by=admin,
        decided_by_username="admin",
        reason="duplicate of existing card",
    )
    assert decision.action == "rejected"
    assert decision.reason == "duplicate of existing card"


async def test_create_enforces_unique_per_candidate(db_session) -> None:
    """One decision per candidate (UNIQUE constraint)."""
    from bot.db.repos.extraction_decision import ExtractionDecisionRepo

    cid = await _make_candidate(db_session)
    admin = await _make_admin(db_session)
    await ExtractionDecisionRepo.create(
        db_session,
        candidate_id=cid,
        action="approved",
        decided_by=admin,
        decided_by_username="admin",
        reason=None,
    )
    with pytest.raises(Exception):
        await ExtractionDecisionRepo.create(
            db_session,
            candidate_id=cid,
            action="rejected",
            decided_by=admin,
            decided_by_username="admin",
            reason="conflict",
        )
