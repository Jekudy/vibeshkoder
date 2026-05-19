"""Phase 11 binding tests — I7a, I7b, I7c, I7d, I7e.

T9-08 / PHASE9_PLAN.md §T9-08. Eval-layer wiki cascade privacy binding.

Cases:
- I7c: CASCADE_LAYER_ORDER index assertions (wiki_pages after digests, before
  wiki_revisions; wiki_revisions before card_sources). Pure import assertion.
- I7a: Forget event on a cited message_version_id triggers wiki_page re-evaluation
  (page_status transitions to 'stale' or 'archived').
- I7b: Forget event on a card_source (card whose sources include the mvid) triggers
  the same wiki_page re-evaluation path as I7a.
- I7d: Legacy cookie without 'role' field — first request treated as admin (grace
  window), response refreshes cookie with explicit role='admin'; WARN logged on first
  request.
- I7e: wiki_revisions.body_markdown is masked for forgotten content by
  _cascade_wiki_revisions; revision_sources_resolved_at updated after masking.

Distinct from tests/services/test_wiki_cascade.py (unit-level, uses db_session /
rollback fixture). This file lives at eval layer, follows the same environment
isolation as test_digest_leakage.py, and is gated by EVAL_HARNESS_ENABLED in CI.
"""

from __future__ import annotations

import itertools
import json
import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("app_env")

# Module-level counters to avoid cross-test ID collisions when the session
# isn't fully rolled back between async tests.
_user_counter = itertools.count(start=7_200_000_000)
_msg_counter = itertools.count(start=720_000)
_chat_id_counter = itertools.count(start=8700)


def _next_user_id() -> int:
    return next(_user_counter)


def _next_msg_id() -> int:
    return next(_msg_counter)


def _next_chat_id() -> int:
    return -1_000_000_000_000 - next(_chat_id_counter)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Seed helpers ───────────────────────────────────────────────────────────────


async def _make_user(session) -> int:
    from bot.db.models import User

    uid = _next_user_id()
    user = User(
        id=uid,
        username=f"wc_eval_{uid}",
        first_name="WCEval",
        is_member=True,
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return uid


async def _make_chat_message(session, *, user_id: int, chat_id: int | None = None):
    from bot.db.models import ChatMessage

    chat_id = chat_id or _next_chat_id()
    cm = ChatMessage(
        message_id=_next_msg_id(),
        chat_id=chat_id,
        user_id=user_id,
        text="eval test content",
        date=_now(),
        raw_json={"text": "eval test content"},
        memory_policy="normal",
        is_redacted=False,
    )
    session.add(cm)
    await session.flush()
    return cm


async def _make_message_version(
    session,
    *,
    chat_message_id: int,
    content_hash: str | None = None,
    is_redacted: bool = False,
) -> int:
    from bot.db.models import MessageVersion

    if content_hash is None:
        content_hash = f"wc-eval-{uuid.uuid4().hex[:16]}"

    mv = MessageVersion(
        chat_message_id=chat_message_id,
        version_seq=1,
        text="eval test content",
        normalized_text="eval test content",
        entities_json={},
        content_hash=content_hash,
        is_redacted=is_redacted,
    )
    session.add(mv)
    await session.flush()

    await session.execute(
        text("UPDATE chat_messages SET current_version_id = :mvid WHERE id = :cmid"),
        {"mvid": mv.id, "cmid": chat_message_id},
    )

    return mv.id


async def _make_knowledge_card(session, *, admin_user_id: int) -> uuid.UUID:
    from bot.db.models import KnowledgeCard

    card = KnowledgeCard(
        title="Eval Card",
        body_markdown="eval card body",
        card_status="approved",
        approved_by_user_id=admin_user_id,
        approved_at=_now(),
    )
    session.add(card)
    await session.flush()
    return card.id


async def _make_card_source(session, *, card_id: uuid.UUID, message_version_id: int) -> None:
    from bot.db.models import CardSource

    cs = CardSource(
        card_id=card_id,
        message_version_id=message_version_id,
        position=0,
    )
    session.add(cs)
    await session.flush()


async def _make_wiki_page(
    session,
    *,
    created_by_user_id: int,
    page_status: str = "reviewed",
    slug: str | None = None,
) -> uuid.UUID:
    if slug is None:
        slug = f"wc-eval-{uuid.uuid4().hex[:8]}"

    page_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wiki_pages "
            "(id, slug, title, body_markdown, page_status, public_enabled, robots_policy, "
            " created_by_user_id, created_at, updated_at) "
            "VALUES "
            "(:id, :slug, :title, :body, :page_status, false, 'noindex', "
            " :created_by, now(), now())"
        ),
        {
            "id": str(page_id),
            "slug": slug,
            "title": "Eval Test Page",
            "body": "eval body content",
            "page_status": page_status,
            "created_by": created_by_user_id,
        },
    )
    await session.flush()
    return page_id


async def _link_mv(session, *, page_id: uuid.UUID, message_version_id: int, position: int = 0) -> None:
    await session.execute(
        text(
            "INSERT INTO wiki_page_message_sources (wiki_page_id, message_version_id, position) "
            "VALUES (:page_id, :mvid, :pos)"
        ),
        {"page_id": str(page_id), "mvid": message_version_id, "pos": position},
    )
    await session.flush()


async def _link_card(session, *, page_id: uuid.UUID, card_id: uuid.UUID, position: int = 0) -> None:
    await session.execute(
        text(
            "INSERT INTO wiki_page_card_sources (wiki_page_id, card_id, position) "
            "VALUES (:page_id, :card_id, :pos)"
        ),
        {"page_id": str(page_id), "card_id": str(card_id), "pos": position},
    )
    await session.flush()


async def _make_forget_event_row(
    session,
    *,
    tombstone_key: str,
    target_type: str = "message",
    target_id: str | None = None,
    status: str = "pending",
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO forget_events "
            "(target_type, target_id, authorized_by, tombstone_key, status, policy, created_at, updated_at) "
            "VALUES (:tt, :tid, 'admin', :tk, :st, 'forgotten', now(), now()) "
            "RETURNING id"
        ),
        {
            "tt": target_type,
            "tid": target_id,
            "tk": tombstone_key,
            "st": status,
        },
    )
    event_id = result.scalar_one()
    await session.flush()
    return event_id


async def _insert_wiki_revision(
    session,
    *,
    rev_id: uuid.UUID,
    page_id: uuid.UUID,
    mv_ids: list[int],
    body_markdown: str = "original eval body",
    revision_seq: int | None = None,
) -> None:
    if revision_seq is None:
        count_result = await session.execute(
            text("SELECT count(*) FROM wiki_revisions WHERE wiki_page_id = :pid"),
            {"pid": str(page_id)},
        )
        revision_seq = count_result.scalar_one() + 1

    await session.execute(
        text(
            "INSERT INTO wiki_revisions "
            "(id, wiki_page_id, revision_seq, body_markdown, revision_status, "
            " source_message_version_ids_snapshot, source_card_ids_snapshot, "
            " edited_at, created_at) "
            "VALUES "
            "(:id, :page_id, :seq, :body, 'active', "
            " CAST(:mv_ids AS jsonb), '[]'::jsonb, "
            " now(), now())"
        ),
        {
            "id": str(rev_id),
            "page_id": str(page_id),
            "seq": revision_seq,
            "body": body_markdown,
            "mv_ids": json.dumps(mv_ids),
        },
    )
    await session.flush()


async def _get_page_status(session, page_id: uuid.UUID) -> dict:
    row = (
        await session.execute(
            text("SELECT page_status, public_enabled FROM wiki_pages WHERE id = :id"),
            {"id": str(page_id)},
        )
    ).mappings().one()
    return dict(row)


class _FakeEvent:
    """Minimal fake forget_event for calling cascade functions directly."""

    def __init__(
        self,
        *,
        id: int,
        target_type: str,
        target_id: str | None,
        tombstone_key: str,
    ):
        self.id = id
        self.target_type = target_type
        self.target_id = target_id
        self.tombstone_key = tombstone_key


# ── I7c: CASCADE_LAYER_ORDER index assertions — pure import, no DB ─────────────


def test_I7c_cascade_layer_order_wiki_pages_after_digests_before_revisions() -> None:
    """I7c: wiki_pages appears after digests and before wiki_revisions in CASCADE_LAYER_ORDER."""
    from bot.services.forget_cascade import CASCADE_LAYER_ORDER

    order = list(CASCADE_LAYER_ORDER)
    assert CASCADE_LAYER_ORDER.index("wiki_pages") > CASCADE_LAYER_ORDER.index("digests"), (
        "wiki_pages must appear AFTER digests in CASCADE_LAYER_ORDER"
    )
    assert CASCADE_LAYER_ORDER.index("wiki_pages") < CASCADE_LAYER_ORDER.index("wiki_revisions"), (
        "wiki_pages must appear BEFORE wiki_revisions in CASCADE_LAYER_ORDER"
    )
    assert CASCADE_LAYER_ORDER.index("wiki_revisions") < CASCADE_LAYER_ORDER.index("card_sources"), (
        "wiki_revisions must appear BEFORE card_sources in CASCADE_LAYER_ORDER"
    )
    # Sanity: all three layers exist
    assert "wiki_pages" in order
    assert "wiki_revisions" in order
    assert "card_sources" in order


# ── I7a: forget on cited mvid → wiki_page transitions to stale/archived ────────


async def test_I7a_forget_on_cited_mvid_transitions_wiki_page(db_session) -> None:
    """I7a: Forget event on a cited message_version_id triggers wiki_page re-evaluation.

    The wiki page directly links the mvid via wiki_page_message_sources. After
    _cascade_wiki_pages runs, the page_status must be 'stale' or 'archived' and
    public_enabled must be false.
    """
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, created_by_user_id=uid, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count >= 1, "cascade must report at least one modified page"
    row = await _get_page_status(db_session, page_id)
    assert row["page_status"] in ("stale", "archived"), (
        f"page_status must be 'stale' or 'archived', got: {row['page_status']!r}"
    )
    assert row["public_enabled"] is False, "public_enabled must be false after cascade"


# ── I7b: forget on card_source → wiki_page transitions ─────────────────────────


async def test_I7b_forget_on_card_source_triggers_wiki_page_cascade(db_session) -> None:
    """I7b: Forget event on a card_source triggers same path as I7a.

    The wiki page links via wiki_page_card_sources → card_sources → message_version_id.
    The forget targets the mvid that is a source of the card. The cascade must
    traverse the transitive path and transition the wiki page.
    """
    from bot.services.forget_cascade import _cascade_wiki_pages

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    # Card linked to the mvid as a card_source
    card_id = await _make_knowledge_card(db_session, admin_user_id=uid)
    await _make_card_source(db_session, card_id=card_id, message_version_id=mv_id)

    # Wiki page linked to the card (not directly to the mvid)
    page_id = await _make_wiki_page(db_session, created_by_user_id=uid, page_status="reviewed")
    await _link_card(db_session, page_id=page_id, card_id=card_id)

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_pages(db_session, event)

    assert count >= 1, "cascade must report at least one modified page via card_source path"
    row = await _get_page_status(db_session, page_id)
    assert row["page_status"] in ("stale", "archived"), (
        f"page_status must be 'stale' or 'archived' via card_source path, got: {row['page_status']!r}"
    )
    assert row["public_enabled"] is False, "public_enabled must be false after card_source cascade"


# ── I7d: legacy cookie grace window + refresh ─────────────────────────────────


def test_I7d_legacy_cookie_without_role_treated_as_admin_and_cookie_refreshed(
    monkeypatch,
) -> None:
    """I7d: Cookie without 'role' field treated as admin (grace window); response
    refreshes cookie with explicit role='admin'; WARN logged on first request.

    Uses TestClient against the FastAPI app. The best-effort audit DB insert runs
    as a background task; DB persistence is verified by a separate async test
    (test_I7d_audit_row_persisted_with_null_wiki_page_id).
    """
    from tests.conftest import import_module

    web_auth = import_module("web.auth")

    web_app = import_module("web.app")
    from fastapi.testclient import TestClient
    from itsdangerous import URLSafeTimedSerializer

    # Build a legacy cookie WITHOUT a 'role' field using the same secret key
    s = URLSafeTimedSerializer(web_auth._SECRET_KEY)
    legacy_cookie = s.dumps({"authenticated": True})  # no 'role'

    # Codex LOW #6: wrap TestClient in a context manager so lifespan / background
    # resources are released between tests.
    with TestClient(web_app.create_app(), raise_server_exceptions=True) as client:
        client.cookies.set("session", legacy_cookie)

        # Capture WARNING log on the first request. Use /dashboard — the middleware
        # refreshes the cookie before the route handler runs (even if the route
        # fails with a 500 from a missing DB).
        import logging
        log_records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = _Capture()
        handler.setLevel(logging.WARNING)
        app_logger = logging.getLogger("web.app")
        app_logger.addHandler(handler)
        try:
            response = client.get("/dashboard", follow_redirects=False)
        finally:
            app_logger.removeHandler(handler)

        # The middleware must NOT redirect to /login (legacy cookie is still valid)
        assert response.status_code != 302 or response.headers.get("location") != "/login", (
            "Legacy cookie should not redirect to /login — must be treated as admin"
        )

        # A WARNING must have been logged about the legacy session promotion
        warn_messages = [r.getMessage() for r in log_records if r.levelno >= logging.WARNING]
        assert any("legacy session cookie promoted to admin" in m for m in warn_messages), (
            f"Expected WARN about legacy session promotion, logged: {warn_messages}"
        )

        # The response must set a refreshed cookie with explicit role='admin'
        new_cookie_value = response.cookies.get("session")
        assert new_cookie_value is not None, (
            "Response must set a refreshed 'session' cookie with role='admin'"
        )
        refreshed_payload = s.loads(new_cookie_value)
        assert refreshed_payload.get("role") == "admin", (
            f"Refreshed cookie must carry role='admin', got: {refreshed_payload}"
        )


# ── I7d-db: legacy_cookie_grace audit row persisted with wiki_page_id IS NULL ──


async def test_I7d_audit_row_persisted_with_null_wiki_page_id(
    postgres_engine,
) -> None:
    """I7d-db: _insert_legacy_grace_audit writes a wiki_publication_log row with
    action='legacy_cookie_grace' AND wiki_page_id IS NULL (migration 055 fix).

    Before migration 055: wiki_page_id was NOT NULL with FK to wiki_pages.id.
    The old code used gen_random_uuid() which always failed with ForeignKeyViolation.
    After migration 055: wiki_page_id is NULLABLE; only legacy_cookie_grace rows
    may have NULL (enforced by CHECK constraint).

    This test calls _insert_legacy_grace_audit() via its internal async coroutine
    directly (bypassing the loop.create_task() scheduling) so the insert completes
    synchronously within the test, allowing immediate DB verification.
    Cleans up the inserted row after assertion to leave the DB clean.
    """
    from tests.conftest import import_module
    from sqlalchemy import text

    # Reload web.auth so that env vars from app_env fixture are in effect.
    web_auth = import_module("web.auth")

    # Call _insert_legacy_grace_audit() as it is called in production:
    # sync wrapper that schedules a background task on the running event loop.
    # Since we are in an async test, there IS a running loop — the task is
    # scheduled immediately. We must wait for it to complete before querying.
    import asyncio

    # Collect all currently running tasks so we can wait only for new ones.
    before = set(asyncio.all_tasks())
    web_auth._insert_legacy_grace_audit()
    after_start = set(asyncio.all_tasks())
    new_tasks = after_start - before
    if new_tasks:
        # Wait for all newly scheduled tasks to complete.
        await asyncio.gather(*new_tasks, return_exceptions=True)

    # Verify the row was written with wiki_page_id IS NULL and correct action.
    async with postgres_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT action, wiki_page_id FROM wiki_publication_log "
                    "WHERE action = 'legacy_cookie_grace' AND wiki_page_id IS NULL "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).mappings().first()

    assert row is not None, (
        "Expected a wiki_publication_log row with action='legacy_cookie_grace', "
        "but none found. The column wiki_page_id must be NULLABLE (migration 055) "
        "and the insert must use NULL not gen_random_uuid()."
    )
    assert row["action"] == "legacy_cookie_grace", (
        f"Expected action='legacy_cookie_grace', got: {row['action']!r}"
    )
    assert row["wiki_page_id"] is None, (
        f"Expected wiki_page_id IS NULL for legacy_cookie_grace row, got: {row['wiki_page_id']!r}"
    )

    # Cleanup: delete the test row so it doesn't accumulate between test runs.
    async with postgres_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM wiki_publication_log "
                "WHERE action = 'legacy_cookie_grace' AND wiki_page_id IS NULL"
            )
        )


# ── I7e: _cascade_wiki_revisions masks body_markdown + updates resolved_at ─────


async def test_I7e_cascade_wiki_revisions_masks_body_and_updates_resolved_at(
    db_session,
) -> None:
    """I7e: _cascade_wiki_revisions masks wiki_revisions.body_markdown for forgotten
    content and sets revision_sources_resolved_at after masking.

    Seeds a wiki_revisions row whose source_message_version_ids_snapshot contains the
    affected mvid, fires _cascade_wiki_revisions, then asserts:
    - body_markdown == '[CONTENT_REDACTED: forget_event_id={n}]'
    - revision_sources_resolved_at is NOT NULL
    - revision_status == 'forgotten_redacted'
    - redacted_by_forget_event_id == event_id
    """
    from bot.services.forget_cascade import _cascade_wiki_revisions

    uid = await _make_user(db_session)
    cm = await _make_chat_message(db_session, user_id=uid)
    mv_id = await _make_message_version(db_session, chat_message_id=cm.id)

    page_id = await _make_wiki_page(db_session, created_by_user_id=uid, page_status="reviewed")
    await _link_mv(db_session, page_id=page_id, message_version_id=mv_id)

    rev_id = uuid.uuid4()
    await _insert_wiki_revision(
        db_session,
        rev_id=rev_id,
        page_id=page_id,
        mv_ids=[mv_id],
        body_markdown="original eval revision body — must be redacted",
    )

    event_id = await _make_forget_event_row(
        db_session,
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
        target_type="message",
        target_id=str(cm.id),
    )
    event = _FakeEvent(
        id=event_id,
        target_type="message",
        target_id=str(cm.id),
        tombstone_key=f"message:{cm.chat_id}:{cm.message_id}",
    )

    count = await _cascade_wiki_revisions(db_session, event)

    assert count == 1, "cascade must report exactly one masked revision"

    row = (
        await db_session.execute(
            text(
                "SELECT body_markdown, revision_status, "
                "redacted_by_forget_event_id, redacted_at, "
                "revision_sources_resolved_at "
                "FROM wiki_revisions WHERE id = :id"
            ),
            {"id": str(rev_id)},
        )
    ).mappings().one()

    expected_mask = f"[CONTENT_REDACTED: forget_event_id={event_id}]"
    assert row["body_markdown"] == expected_mask, (
        f"body_markdown must be the canonical mask format, got: {row['body_markdown']!r}"
    )
    assert row["revision_status"] == "forgotten_redacted", (
        f"revision_status must be 'forgotten_redacted', got: {row['revision_status']!r}"
    )
    assert row["redacted_by_forget_event_id"] == event_id, (
        f"redacted_by_forget_event_id must equal event_id={event_id}"
    )
    assert row["redacted_at"] is not None, "redacted_at must be set after masking"
    assert row["revision_sources_resolved_at"] is not None, (
        "revision_sources_resolved_at must be updated after masking"
    )
