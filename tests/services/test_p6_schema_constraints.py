"""T6-01 Phase 6 schema constraint smoke tests.

Pins the DB-level invariants for migrations 030-034 by directly attempting
violating inserts and asserting the DB rejects them. Constraints are the
last line of defence; application-layer guards CAN be bypassed (raw SQL,
admin maintenance) — these tests ensure the schema itself prevents the
worst privacy / governance violations.

Per PHASE6_PLAN.md §5.A acceptance criteria:

* ``card_status='approved'`` cannot exist without ``approved_by_user_id``
  AND ``approved_at`` set.
* ``extraction_candidates.status='pending'`` implies both reviewer columns
  are NULL; terminal statuses require both reviewer columns set.
* ``extraction_candidates.source_message_version_ids`` must be a JSON
  array (not object/scalar).
* ``card_sources`` UNIQUE(card_id, message_version_id) prevents duplicate
  links.
* ``extraction_decisions.candidate_id`` is UNIQUE — exactly one terminal
  decision per candidate.
* ``extraction_runs.candidate_count >= 0``.
* ``run_status='completed'`` requires both window timestamps to be set.

All tests roll back via the outer-tx ``db_session`` fixture; nothing is
persisted past the test boundary.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_500_000_000)


def _next_user() -> int:
    return next(_user_counter)


async def _make_user(db_session) -> int:
    from bot.db.repos.user import UserRepo

    uid = _next_user()
    await UserRepo.upsert(
        db_session,
        telegram_id=uid,
        username=f"u{uid}",
        first_name="Test",
        last_name=None,
    )
    return uid


# ─── Helpers (raw SQL so model-level guards do not interfere) ────────────────


async def _insert_card(
    db_session,
    *,
    title: str = "t",
    body: str = "b",
    card_status: str = "draft",
    approved_by_user_id: int | None = None,
    approved_at: datetime | None = None,
) -> uuid.UUID:
    """Insert a knowledge_cards row via raw SQL and return its id."""
    new_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO knowledge_cards
                (id, title, body_markdown, card_status,
                 approved_by_user_id, approved_at)
            VALUES (:id, :title, :body, :card_status,
                    :approved_by_user_id, :approved_at)
            """
        ),
        {
            "id": str(new_id),
            "title": title,
            "body": body,
            "card_status": card_status,
            "approved_by_user_id": approved_by_user_id,
            "approved_at": approved_at,
        },
    )
    await db_session.flush()
    return new_id


async def _insert_candidate(
    db_session,
    *,
    status: str = "pending",
    reviewed_by: int | None = None,
    reviewed_at: datetime | None = None,
    source_ids_json: str = "[]",
) -> uuid.UUID:
    new_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO extraction_candidates
                (id, candidate_json, source_message_version_ids,
                 status, reviewed_by, reviewed_at)
            VALUES (:id, '{}'::jsonb, CAST(:src AS jsonb),
                    :status, :rby, :rat)
            """
        ),
        {
            "id": str(new_id),
            "src": source_ids_json,
            "status": status,
            "rby": reviewed_by,
            "rat": reviewed_at,
        },
    )
    await db_session.flush()
    return new_id


# ─── knowledge_cards: card_status='approved' requires approver attribution ──


async def test_knowledge_cards_approved_requires_approver(db_session) -> None:
    """Inserting card_status='approved' without approver columns must fail."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_card(db_session, card_status="approved")


async def test_knowledge_cards_approved_with_partial_attribution_fails(db_session) -> None:
    """approved_by_user_id alone (without approved_at) must fail too."""
    uid = await _make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_card(
                db_session,
                card_status="approved",
                approved_by_user_id=uid,
                approved_at=None,
            )


async def test_knowledge_cards_approved_with_full_attribution_succeeds(
    db_session,
) -> None:
    """Approved with both attribution columns set is allowed."""
    uid = await _make_user(db_session)
    cid = await _insert_card(
        db_session,
        card_status="approved",
        approved_by_user_id=uid,
        approved_at=datetime.now(timezone.utc),
    )
    assert cid is not None


async def test_knowledge_cards_rejects_unknown_status(db_session) -> None:
    """card_status outside the allowed set must fail.

    Specifically, the DRAFT's 'deprecated' value (collapsed into 'archived'
    per Q3) must be rejected, locking the Q3 decision in schema.
    """
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_card(db_session, card_status="deprecated")


# ─── extraction_candidates: reviewer consistency ─────────────────────────────


async def test_extraction_candidates_pending_with_reviewer_fails(db_session) -> None:
    """pending row with a reviewer set must be rejected."""
    uid = await _make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_candidate(
                db_session,
                status="pending",
                reviewed_by=uid,
                reviewed_at=datetime.now(timezone.utc),
            )


async def test_extraction_candidates_approved_without_reviewer_fails(
    db_session,
) -> None:
    """approved row without reviewer must be rejected."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_candidate(db_session, status="approved")


async def test_extraction_candidates_rejected_without_reviewer_fails(
    db_session,
) -> None:
    """rejected row without reviewer must be rejected."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_candidate(db_session, status="rejected")


async def test_extraction_candidates_pending_no_reviewer_succeeds(db_session) -> None:
    """pending with both reviewer columns null is the normal case — must succeed."""
    cid = await _insert_candidate(db_session, status="pending")
    assert cid is not None


# ─── extraction_candidates: source_message_version_ids must be array ─────────


async def test_extraction_candidates_source_ids_must_be_array(db_session) -> None:
    """Non-array source_message_version_ids must be rejected.

    This guards the §5.C /approve transaction protocol which iterates over
    the candidate's source ids; an object/scalar there would either fail
    silently or produce wrong-typed lookups.

    Wrap each violating insert in a SAVEPOINT (begin_nested) so the
    constraint failure rolls back only the bad insert; the outer transaction
    stays alive for the next iteration.
    """
    for bad in ('{"x": 1}', '"scalar"', "123", "null", "true"):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _insert_candidate(db_session, source_ids_json=bad)


async def test_extraction_candidates_source_ids_array_succeeds(db_session) -> None:
    """Empty array and populated array both succeed."""
    cid1 = await _insert_candidate(db_session, source_ids_json="[]")
    cid2 = await _insert_candidate(db_session, source_ids_json="[1, 2, 3]")
    assert cid1 is not None
    assert cid2 is not None


# ─── card_sources: UNIQUE(card_id, message_version_id) ───────────────────────


async def test_card_sources_unique_pair_constraint(db_session) -> None:
    """Inserting the same (card_id, message_version_id) twice must fail."""
    from bot.db.models import ChatMessage, MessageVersion

    # Create a chat_message + message_version to satisfy the FK.
    uid = await _make_user(db_session)
    msg = ChatMessage(
        message_id=1, chat_id=-100, user_id=uid, text="x",
        date=datetime.now(timezone.utc), memory_policy="normal", is_redacted=False,
    )
    db_session.add(msg)
    await db_session.flush()
    v = MessageVersion(
        chat_message_id=msg.id, version_seq=1, text="x",
        normalized_text="x", entities_json={}, content_hash="hh", is_redacted=False,
    )
    db_session.add(v)
    await db_session.flush()

    card_id = await _insert_card(db_session)
    await db_session.execute(
        text(
            "INSERT INTO card_sources (id, card_id, message_version_id) "
            "VALUES (:id, :cid, :mv)"
        ),
        {"id": str(uuid.uuid4()), "cid": str(card_id), "mv": v.id},
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO card_sources (id, card_id, message_version_id) "
                    "VALUES (:id, :cid, :mv)"
                ),
                {"id": str(uuid.uuid4()), "cid": str(card_id), "mv": v.id},
            )
            await db_session.flush()


# ─── extraction_decisions: UNIQUE(candidate_id) ──────────────────────────────


async def test_extraction_decisions_one_per_candidate(db_session) -> None:
    """A candidate cannot get two decisions — appeals out of scope per §11."""
    uid = await _make_user(db_session)
    cand = await _insert_candidate(db_session)
    await db_session.execute(
        text(
            """
            INSERT INTO extraction_decisions
                (id, candidate_id, action, decided_by, decided_by_username)
            VALUES (:id, :cand, 'rejected', :u, 'admin1')
            """
        ),
        {"id": str(uuid.uuid4()), "cand": str(cand), "u": uid},
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    """
                    INSERT INTO extraction_decisions
                        (id, candidate_id, action, decided_by, decided_by_username)
                    VALUES (:id, :cand, 'approved', :u, 'admin1')
                    """
                ),
                {"id": str(uuid.uuid4()), "cand": str(cand), "u": uid},
            )
            await db_session.flush()


# ─── extraction_runs: window + status invariants ─────────────────────────────


async def test_extraction_runs_completed_requires_window(db_session) -> None:
    """run_status='completed' MUST have both window timestamps set."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    """
                    INSERT INTO extraction_runs (id, run_status, candidate_count)
                    VALUES (:id, 'completed', 0)
                    """
                ),
                {"id": str(uuid.uuid4())},
            )
            await db_session.flush()


async def test_extraction_runs_running_allows_null_window(db_session) -> None:
    """running rows MAY have null window timestamps (run is still open)."""
    rid = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO extraction_runs (id, run_status, candidate_count)
            VALUES (:id, 'running', 0)
            """
        ),
        {"id": str(rid)},
    )
    await db_session.flush()


async def test_extraction_runs_candidate_count_nonneg(db_session) -> None:
    """candidate_count must be non-negative."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    """
                    INSERT INTO extraction_runs (id, run_status, candidate_count)
                    VALUES (:id, 'running', -1)
                    """
                ),
                {"id": str(uuid.uuid4())},
            )
            await db_session.flush()
