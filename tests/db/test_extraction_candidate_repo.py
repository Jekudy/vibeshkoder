"""T6-04 acceptance tests — extraction_candidates repo.

PHASE6_PLAN.md §5.C / T6-04 design §5: repo backs the ``/candidates`` paginated
list + ``/approve`` and ``/reject`` row-level locking.

Methods covered:

* ``list_pending(session, limit, offset)`` — page of pending candidates ordered
  newest-first.
* ``get_by_id_for_update(session, candidate_id)`` — row-level lock on a single
  candidate (step 1 of the §5.C 8-step protocol).
* ``mark_status(session, candidate_id, status, reviewed_by)`` — flip status
  with audit columns; raises if the status check constraint would be violated.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


_user_counter = itertools.count(start=9_900_000_000)


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


async def _make_extraction_run(db_session) -> uuid.UUID:
    from bot.db.models import ExtractionRun

    run = ExtractionRun(
        run_status="completed",
        ingestion_window_start=datetime.now(timezone.utc) - timedelta(hours=1),
        ingestion_window_end=datetime.now(timezone.utc),
        candidate_count=0,
    )
    db_session.add(run)
    await db_session.flush()
    return run.id


async def _make_candidate(
    db_session,
    *,
    status: str = "pending",
    title: str = "Sample title",
    body_markdown: str = "Body text",
    source_mvids: list[int] | None = None,
) -> uuid.UUID:
    from bot.db.models import ExtractionCandidate

    run_id = await _make_extraction_run(db_session)
    cand = ExtractionCandidate(
        extraction_run_id=run_id,
        candidate_json={"title": title, "body_markdown": body_markdown},
        source_message_version_ids=source_mvids if source_mvids is not None else [],
        status=status,
    )
    db_session.add(cand)
    await db_session.flush()
    return cand.id


# ─── list_pending ────────────────────────────────────────────────────────────


async def test_list_pending_returns_pending_only(db_session) -> None:
    """Only ``status='pending'`` rows are returned."""
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo

    pending_id = await _make_candidate(db_session, status="pending")
    # approved candidate must have non-null reviewer columns per CHECK.
    from bot.db.models import ExtractionCandidate
    from sqlalchemy import update

    approved_id = await _make_candidate(db_session, status="pending")
    admin = await _make_admin(db_session)
    await db_session.execute(
        update(ExtractionCandidate)
        .where(ExtractionCandidate.id == approved_id)
        .values(
            status="approved",
            reviewed_by=admin,
            reviewed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    rows = await ExtractionCandidateRepo.list_pending(
        db_session, limit=10, offset=0
    )
    ids = [r.id for r in rows]
    assert pending_id in ids
    assert approved_id not in ids


async def test_list_pending_orders_newest_first(db_session) -> None:
    """Order: ``created_at DESC, id DESC`` per design §4.

    Forces distinct ``created_at`` via explicit timestamps; ``func.now()`` in
    Postgres returns transaction-start time so adjacent inserts within the
    same transaction share a ``created_at`` and would tie-break only on UUID
    (random for v4).
    """
    from bot.db.models import ExtractionCandidate
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo

    older = await _make_candidate(db_session, status="pending", title="t1")
    newer = await _make_candidate(db_session, status="pending", title="t2")
    # Force explicit timestamps so the ORDER BY can decide.
    from sqlalchemy import update

    base = datetime.now(timezone.utc)
    await db_session.execute(
        update(ExtractionCandidate)
        .where(ExtractionCandidate.id == older)
        .values(created_at=base - timedelta(minutes=5))
    )
    await db_session.execute(
        update(ExtractionCandidate)
        .where(ExtractionCandidate.id == newer)
        .values(created_at=base)
    )
    await db_session.flush()

    rows = await ExtractionCandidateRepo.list_pending(
        db_session, limit=10, offset=0
    )
    ids = [r.id for r in rows]
    # Newest first → newer appears before older.
    assert ids.index(newer) < ids.index(older)


async def test_list_pending_respects_limit_and_offset(db_session) -> None:
    """LIMIT/OFFSET drive pagination."""
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo

    ids = []
    for _ in range(5):
        ids.append(await _make_candidate(db_session, status="pending"))

    page1 = await ExtractionCandidateRepo.list_pending(
        db_session, limit=2, offset=0
    )
    page2 = await ExtractionCandidateRepo.list_pending(
        db_session, limit=2, offset=2
    )

    assert len(page1) == 2
    assert len(page2) == 2
    # No overlap between pages.
    p1_ids = {r.id for r in page1}
    p2_ids = {r.id for r in page2}
    assert p1_ids.isdisjoint(p2_ids)


# ─── get_by_id_for_update ────────────────────────────────────────────────────


async def test_get_by_id_for_update_returns_row(db_session) -> None:
    """Returns the candidate row by id."""
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo

    cid = await _make_candidate(db_session, status="pending")
    row = await ExtractionCandidateRepo.get_by_id_for_update(db_session, cid)
    assert row is not None
    assert row.id == cid
    assert row.status == "pending"


async def test_get_by_id_for_update_returns_none_when_missing(db_session) -> None:
    """Returns None for a missing id (caller decides whether to error)."""
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo

    row = await ExtractionCandidateRepo.get_by_id_for_update(
        db_session, uuid.uuid4()
    )
    assert row is None


# ─── mark_status ─────────────────────────────────────────────────────────────


async def test_mark_status_sets_reviewer_columns(db_session) -> None:
    """``mark_status`` updates status + reviewer audit columns atomically."""
    from bot.db.repos.extraction_candidate import ExtractionCandidateRepo

    cid = await _make_candidate(db_session, status="pending")
    admin = await _make_admin(db_session)

    await ExtractionCandidateRepo.mark_status(
        db_session,
        candidate_id=cid,
        status="approved",
        reviewed_by=admin,
    )

    row = await ExtractionCandidateRepo.get_by_id_for_update(db_session, cid)
    assert row.status == "approved"
    assert row.reviewed_by == admin
    assert row.reviewed_at is not None
