"""T8-01 acceptance tests — digests + digest_runs Phase 8 review-gate schema
(migration 038 ``extend_digests_review_states``).

Mirrors the pattern of ``test_digests_schema.py``: a temporary isolated postgres
database is created, ``alembic upgrade head`` is run, then schema-shape and
constraint assertions are executed via asyncpg. Downgrade-blocking pre-flight
``DO $$ ... $$`` block is exercised by inserting Phase-8 review-state rows and
asserting that ``alembic downgrade -1`` fails with the expected RAISE EXCEPTION
text (surfaced via ``subprocess.CalledProcessError`` because ``_run_alembic``
uses ``check=True``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine.url import URL, make_url

from tests.conftest import DEFAULT_LOCAL_POSTGRES_URL

pytestmark = pytest.mark.usefixtures("app_env")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _base_test_url() -> URL:
    raw_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )
    return make_url(raw_url)


def _asyncpg_kwargs(url: URL, *, database: str | None = None) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database or url.database,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _create_database(admin_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await conn.execute(f"CREATE DATABASE {_quote_identifier(database_name)}")
    finally:
        await conn.close()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(**_asyncpg_kwargs(admin_url, database="postgres"))
    try:
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database_name,
        )
        await conn.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
    finally:
        await conn.close()


def _run_alembic(
    database_url: str,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=check,
    )


async def _fetch_value(database_url: str, query: str, *args: object) -> object:
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(database_url)))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


@pytest_asyncio.fixture()
async def temp_database_url() -> AsyncIterator[str]:
    base_url = _base_test_url()
    database_name = f"shkoder_digests_p8_{uuid.uuid4().hex[:12]}"
    try:
        await _create_database(base_url, database_name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot create temporary postgres database: {exc!s}")

    try:
        yield base_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        await _drop_database(base_url, database_name)


@pytest_asyncio.fixture()
async def migrated_database_url(temp_database_url: str) -> AsyncIterator[str]:
    _run_alembic(temp_database_url, "upgrade", "head")
    yield temp_database_url


# ─── Test: alembic head is the latest migration ────────────────────────────────


async def test_alembic_head_is_latest(migrated_database_url: str) -> None:
    """After upgrade head, alembic_version reports the latest migration.

    Previously asserted '038' (Phase 8 weekly review-gate); rolled forward to
    '054' when T9-01 added wiki migrations 050-054; then to '055' when Phase 9
    FHR landed the legacy-grace nullable `wiki_page_id` migration; then to '060'
    when Phase 10 W0-A added graph_projection_runs; then to '062' when T10-02
    added graph_provenance (061) and graph_edges (062); then to '064' when T10-03
    added llm_usage_ledger.call_type; then to '063' when T10-06 added
    graph_purge_pending (chain: 062→064→063); then to '065' when T10-06 CRITICAL-1
    fix updated graph_purge_pending unique constraint to include graph_provenance_id.
    The intent of this test is unchanged — assert the head matches the latest
    shipped migration. Update the literal when new migrations land.
    Migration 067: tightened ck_graph_provenance_has_source OR → XOR (10.5-9).
    Migration 068: graph_provenance.triple_hash TEXT → BIGINT (10.5-S3).
    Migrations 070-073: Phase 12 Butler schema foundation (T12-01) —
    070 audit triple, 071 ledger call_type CHECK, 072 rate buckets, 073 card suggestions.
    Migration 074: butler_actions query/visibility_scope/plan_payload + confirmation_token (T12-04).
    Migration 075: butler_action_confirmations.status adds 'revoked' (T12-05-fix C1).
    Migration 076: T12-06-fix C2 — butler_tool_invocations.posted_message_id.
    """
    current = await _fetch_value(migrated_database_url, "SELECT version_num FROM alembic_version")
    assert current == "076"


# ─── Test: upgrade adds 4 review columns with correct types/nullability ──────


async def test_038_upgrade_adds_review_columns(migrated_database_url: str) -> None:
    """digests gains published_by_admin_id, approved_at, review_notes, awaiting_review_at."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='digests'
              AND column_name IN ('published_by_admin_id','approved_at','review_notes',
                                  'awaiting_review_at')
            ORDER BY column_name
            """
        )
        # Expected:
        #   approved_at            timestamp with time zone  YES
        #   awaiting_review_at     timestamp with time zone  YES
        #   published_by_admin_id  bigint                    YES
        #   review_notes           text                      YES
        seen = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
        assert seen == {
            "approved_at": ("timestamp with time zone", "YES"),
            "awaiting_review_at": ("timestamp with time zone", "YES"),
            "published_by_admin_id": ("bigint", "YES"),
            "review_notes": ("text", "YES"),
        }
    finally:
        await conn.close()


# ─── Test: digests.status enum widened to include Phase 8 review states ──────


async def test_038_upgrade_widens_status_enum(migrated_database_url: str) -> None:
    """INSERT status='awaiting_review' succeeds; INSERT status='bogus' violates CHECK."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        # awaiting_review is now valid; needs body_markdown (widened body CHECK).
        row_id = await conn.fetchval(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations,
                                 body_markdown, awaiting_review_at)
            VALUES (
                'weekly',
                '2026-05-04 00:00:00+00',
                '2026-05-11 00:00:00+00',
                'awaiting_review',
                '[]'::jsonb,
                'body',
                now()
            )
            RETURNING id
            """
        )
        assert row_id is not None

        # All 4 new Phase 8 statuses accepted (with body where required).
        # Pre-baked literal SQL keeps the test free of asyncpg datetime-binding
        # quirks; the goal is exercising the CHECK enum, not parameter codec.
        await conn.execute(
            """
            INSERT INTO digests (
                type, window_start, window_end, status, citations, body_markdown,
                published_by_admin_id, approved_at
            )
            VALUES (
                'weekly','2026-04-13 00:00:00+00','2026-04-20 00:00:00+00',
                'approved_for_publish','[]'::jsonb,'body',
                123456789,'2026-04-20 00:00:00+00'
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO digests (
                type, window_start, window_end, status, citations, body_markdown
            )
            VALUES (
                'weekly','2026-04-06 00:00:00+00','2026-04-13 00:00:00+00',
                'rejected_by_admin','[]'::jsonb,'body'
            )
            """
        )
        # rejected_by_reaper: body still required per widened CHECK (visible/audit).
        await conn.execute(
            """
            INSERT INTO digests (
                type, window_start, window_end, status, citations, body_markdown
            )
            VALUES (
                'weekly','2026-03-30 00:00:00+00','2026-04-06 00:00:00+00',
                'rejected_by_reaper','[]'::jsonb,'body'
            )
            """
        )

        # Unknown status rejected.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (type, window_start, window_end, status, citations,
                                     body_markdown)
                VALUES (
                    'weekly',
                    '2026-05-04 01:00:00+00',
                    '2026-05-11 01:00:00+00',
                    'bogus',
                    '[]'::jsonb,
                    'body'
                )
                """
            )
    finally:
        await conn.close()


# ─── Test: digest_runs.status enum widened to include 5 Phase 8 audit states ─


async def test_038_upgrade_widens_runs_status_enum(migrated_database_url: str) -> None:
    """digest_runs accepts all 5 new audit statuses; unknown rejected."""
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        for status in (
            "awaiting_review",
            "approved_for_publish",
            "rejected_by_admin",
            "rejected_by_reaper",
            "regenerated_by_admin",
        ):
            # Plain string args bind cleanly; status is a Text column with no
            # special codec quirks (unlike timestamptz).
            run_id = await conn.fetchval(
                "INSERT INTO digest_runs (status) VALUES ($1) RETURNING id",
                status,
            )
            assert run_id is not None, f"digest_runs insert for status={status!r} failed"

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute("INSERT INTO digest_runs (status) VALUES ('bogus')")
    finally:
        await conn.close()


# ─── Test: ck_digests_approved_audit — approved_for_publish needs admin id + time ─


async def test_038_approved_audit_check_requires_admin_id(
    migrated_database_url: str,
) -> None:
    """approved_for_publish row with NULL published_by_admin_id → CHECK violation (weekly).

    The audit CHECK is type-aware: it MUST fire for weekly digests; daily digests
    are exempt by predicate.
    """
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown,
                    published_by_admin_id, approved_at
                )
                VALUES (
                    'weekly',
                    '2026-05-04 02:00:00+00',
                    '2026-05-11 02:00:00+00',
                    'approved_for_publish',
                    '[]'::jsonb,
                    'body',
                    NULL,
                    '2026-05-11 00:00:00+00'
                )
                """
            )

        # Same shape with NULL approved_at → also violates.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown,
                    published_by_admin_id, approved_at
                )
                VALUES (
                    'weekly',
                    '2026-05-04 03:00:00+00',
                    '2026-05-11 03:00:00+00',
                    'approved_for_publish',
                    '[]'::jsonb,
                    'body',
                    123456789,
                    NULL
                )
                """
            )

        # Type='daily' approved_for_publish is exempt (daily never sets audit cols).
        row_id = await conn.fetchval(
            """
            INSERT INTO digests (
                type, window_start, window_end, status, citations, body_markdown
            )
            VALUES (
                'daily',
                '2026-05-13 04:00:00+00',
                '2026-05-14 04:00:00+00',
                'approved_for_publish',
                '[]'::jsonb,
                'body'
            )
            RETURNING id
            """
        )
        assert row_id is not None
    finally:
        await conn.close()


# ─── Test: body_markdown NOT NULL widens to review states ────────────────────


async def test_038_body_markdown_required_for_review_states(
    migrated_database_url: str,
) -> None:
    """status='awaiting_review' / 'approved_for_publish' / 'rejected_by_admin' with body=NULL violates.

    The Phase 7 body CHECK covered draft/posting/posted/redacted/redacted_edit_failed.
    Phase 8 widens it to also include awaiting_review, approved_for_publish,
    rejected_by_admin, and rejected_by_reaper (all review-bearing states that carry an
    audit trail).  See §5.A: the reaper terminates a digest that was already in
    awaiting_review (where body was required), so body_markdown IS NOT NULL on entry
    to rejected_by_reaper — the CHECK preserves this invariant at the DB layer.
    """
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown
                )
                VALUES (
                    'weekly','2026-05-04 10:00:00+00','2026-05-11 10:00:00+00',
                    'awaiting_review','[]'::jsonb, NULL
                )
                """
            )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown,
                    published_by_admin_id, approved_at
                )
                VALUES (
                    'weekly','2026-05-04 11:00:00+00','2026-05-11 11:00:00+00',
                    'approved_for_publish','[]'::jsonb, NULL,
                    123456789,'2026-05-11 00:00:00+00'
                )
                """
            )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown
                )
                VALUES (
                    'weekly','2026-05-04 12:00:00+00','2026-05-11 12:00:00+00',
                    'rejected_by_admin','[]'::jsonb, NULL
                )
                """
            )
    finally:
        await conn.close()


async def test_038_body_markdown_required_for_rejected_by_reaper(
    migrated_database_url: str,
) -> None:
    """status='rejected_by_reaper' with body=NULL violates ck_digests_body_markdown_not_null.

    rejected_by_reaper transitions FROM awaiting_review (where body was already required).
    The DB-level CHECK preserves this invariant at the terminal state too — a NULL body
    would hide what content was pending review from audit inspection.
    """
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown
                )
                VALUES (
                    'weekly','2026-05-04 13:00:00+00','2026-05-11 13:00:00+00',
                    'rejected_by_reaper','[]'::jsonb, NULL
                )
                """
            )
    finally:
        await conn.close()


# ─── Test: approved_audit CHECK covers posting + posted statuses ─────────────


async def test_038_approved_audit_check_covers_posting_and_posted(
    migrated_database_url: str,
) -> None:
    """ck_digests_approved_audit rejects weekly 'posting'/'posted' rows missing audit cols.

    The CHECK covers status IN ('approved_for_publish','posting','posted') for weekly
    digests.  All three travel through the same approve→posting→posted pipeline with
    the audit cols set at /digest_approve time and carried through unchanged.
    """
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(migrated_database_url)))
    try:
        # weekly posting with NULL published_by_admin_id → violates
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown,
                    posting_started_at, published_by_admin_id, approved_at
                )
                VALUES (
                    'weekly','2026-05-04 14:00:00+00','2026-05-11 14:00:00+00',
                    'posting','[]'::jsonb,'body',
                    '2026-05-11 09:00:00+00',
                    NULL,
                    '2026-05-11 09:00:00+00'
                )
                """
            )
        # weekly posted with NULL approved_at → violates
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (
                    type, window_start, window_end, status, citations, body_markdown,
                    posted_at, posted_chat_id, posted_message_id,
                    published_by_admin_id, approved_at
                )
                VALUES (
                    'weekly','2026-05-04 15:00:00+00','2026-05-11 15:00:00+00',
                    'posted','[]'::jsonb,'body',
                    '2026-05-11 10:00:00+00', -1001234567890, 42,
                    123456789,
                    NULL
                )
                """
            )
    finally:
        await conn.close()


# ─── Test: partial index ix_digests_status_awaiting_review exists ────────────


async def test_038_awaiting_review_partial_index_exists(
    migrated_database_url: str,
) -> None:
    """ix_digests_status_awaiting_review partial index keyed on awaiting_review_at."""
    indexdef = await _fetch_value(
        migrated_database_url,
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'digests'
          AND indexname = 'ix_digests_status_awaiting_review'
        """,
    )
    assert indexdef is not None, "ix_digests_status_awaiting_review not found"
    lowered = str(indexdef).lower()
    assert "where" in lowered
    assert "awaiting_review" in lowered
    assert "awaiting_review_at" in lowered


# ─── Test: downgrade blocks on review-state rows in digests ──────────────────


async def test_038_downgrade_blocks_on_review_state_rows(
    temp_database_url: str,
) -> None:
    """Insert digest row with status='awaiting_review' → downgrade -1 raises."""
    _run_alembic(temp_database_url, "upgrade", "head")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations,
                                 body_markdown, awaiting_review_at)
            VALUES (
                'weekly',
                '2026-05-04 05:00:00+00',
                '2026-05-11 05:00:00+00',
                'awaiting_review',
                '[]'::jsonb,
                'body',
                now()
            )
            """
        )
    finally:
        await conn.close()

    proc = _run_alembic(temp_database_url, "downgrade", "037", check=False)
    assert proc.returncode != 0, (
        f"expected downgrade to fail on review-state row, "
        f"got rc={proc.returncode} stderr={proc.stderr!r}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "phase 8" in combined or "review" in combined or "awaiting_review" in combined


# ─── Test: downgrade blocks on digest_runs Phase-8 audit rows ────────────────


async def test_038_downgrade_blocks_on_runs_audit_rows(
    temp_database_url: str,
) -> None:
    """Insert digest_runs row with status='regenerated_by_admin' → downgrade -1 raises."""
    _run_alembic(temp_database_url, "upgrade", "head")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute("INSERT INTO digest_runs (status) VALUES ('regenerated_by_admin')")
    finally:
        await conn.close()

    proc = _run_alembic(temp_database_url, "downgrade", "037", check=False)
    assert proc.returncode != 0
    combined = (proc.stdout + proc.stderr).lower()
    assert "digest_runs" in combined


# ─── Test: downgrade blocks on posting rows (daily and weekly) ───────────────


async def test_038_downgrade_blocks_on_posting_rows_daily(
    temp_database_url: str,
) -> None:
    """Insert daily posting row → downgrade fails mentioning stale_posting_reaper."""
    _run_alembic(temp_database_url, "upgrade", "head")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations,
                                 body_markdown, posting_started_at)
            VALUES (
                'daily',
                '2026-05-13 06:00:00+00',
                '2026-05-14 06:00:00+00',
                'posting',
                '[]'::jsonb,
                'body',
                now()
            )
            """
        )
    finally:
        await conn.close()

    proc = _run_alembic(temp_database_url, "downgrade", "037", check=False)
    assert proc.returncode != 0
    combined = (proc.stdout + proc.stderr).lower()
    assert "posting" in combined
    assert "stale_posting_reaper" in combined


async def test_038_downgrade_blocks_on_posting_rows_weekly(
    temp_database_url: str,
) -> None:
    """Insert weekly posting row → downgrade fails mentioning stale_posting_reaper.

    A weekly row in ``posting`` is mid-publish triggered by ``/digest_approve``.
    It satisfies ``ck_digests_approved_audit`` because the approve flow set both
    ``published_by_admin_id`` and ``approved_at`` before transitioning to
    ``posting``; the test mirrors that production shape.
    """
    _run_alembic(temp_database_url, "upgrade", "head")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO digests (
                type, window_start, window_end, status, citations,
                body_markdown, posting_started_at,
                published_by_admin_id, approved_at
            )
            VALUES (
                'weekly',
                '2026-05-04 07:00:00+00',
                '2026-05-11 07:00:00+00',
                'posting',
                '[]'::jsonb,
                'body',
                now(),
                123456789,
                '2026-05-11 09:00:00+00'
            )
            """
        )
    finally:
        await conn.close()

    proc = _run_alembic(temp_database_url, "downgrade", "037", check=False)
    assert proc.returncode != 0
    combined = (proc.stdout + proc.stderr).lower()
    assert "posting" in combined
    assert "stale_posting_reaper" in combined


# ─── Test: downgrade succeeds after posting row transitions to terminal state ─


async def test_038_downgrade_succeeds_after_posting_terminal_transition(
    temp_database_url: str,
) -> None:
    """posting row blocks downgrade; after UPDATE to posted (terminal) downgrade succeeds.

    Validates the operator recovery path documented in §5.A downgrade note: wait for the
    stale_posting_reaper to move the row from posting → posted, then downgrade proceeds.
    The test simulates that by manually UPDATE'ing the row to posted with all required
    cols set (the reaper sets the same cols on Telegram-delivery success).
    """
    _run_alembic(temp_database_url, "upgrade", "head")

    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    row_id: int | None = None
    try:
        row_id = await conn.fetchval(
            """
            INSERT INTO digests (
                type, window_start, window_end, status, citations,
                body_markdown, posting_started_at
            )
            VALUES (
                'daily',
                '2026-05-13 20:00:00+00',
                '2026-05-14 20:00:00+00',
                'posting',
                '[]'::jsonb,
                'body',
                '2026-05-13 20:00:00+00'
            )
            RETURNING id
            """
        )
    finally:
        await conn.close()

    # Attempt downgrade while row is still posting → must fail.
    proc = _run_alembic(temp_database_url, "downgrade", "037", check=False)
    assert proc.returncode != 0, (
        f"expected downgrade to fail on posting row, got rc={proc.returncode}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "posting" in combined

    # Transition the row to posted (simulates stale_posting_reaper completing delivery).
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            UPDATE digests
            SET status='posted',
                posted_at='2026-05-13 20:01:00+00',
                posted_chat_id=-1001234567890,
                posted_message_id=99
            WHERE id=$1
            """,
            row_id,
        )
    finally:
        await conn.close()

    # Now downgrade must succeed.
    _run_alembic(temp_database_url, "downgrade", "037")
    current = await _fetch_value(temp_database_url, "SELECT version_num FROM alembic_version")
    assert current == "037"


# ─── Test: downgrade succeeds on clean Phase-7-only state ────────────────────


async def test_038_downgrade_succeeds_when_clean(temp_database_url: str) -> None:
    """No Phase-8 rows, no posting rows → downgrade -1 restores Phase 7 schema."""
    _run_alembic(temp_database_url, "upgrade", "head")

    # Insert only Phase-7-safe rows: status='running' is the early lifecycle state.
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        await conn.execute(
            """
            INSERT INTO digests (type, window_start, window_end, status, citations)
            VALUES (
                'daily',
                '2026-05-13 08:00:00+00',
                '2026-05-14 08:00:00+00',
                'running',
                '[]'::jsonb
            )
            """
        )
    finally:
        await conn.close()

    _run_alembic(temp_database_url, "downgrade", "037")

    current = await _fetch_value(temp_database_url, "SELECT version_num FROM alembic_version")
    assert current == "037"

    # Phase 7 status CHECK is now back: 'awaiting_review' must be rejected.
    conn = await asyncpg.connect(**_asyncpg_kwargs(make_url(temp_database_url)))
    try:
        # Phase 8 cols must be gone after downgrade.
        cols = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='digests'
              AND column_name IN ('published_by_admin_id','approved_at','review_notes',
                                  'awaiting_review_at')
            """
        )
        assert cols == [], f"expected Phase-8 cols dropped, found {cols!r}"

        # Phase 8 partial index must be gone.
        idx_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE schemaname='public' AND tablename='digests'
                  AND indexname='ix_digests_status_awaiting_review'
            )
            """
        )
        assert idx_exists is False

        # ck_digests_approved_audit must be gone.
        audit_check_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname='ck_digests_approved_audit'
            )
            """
        )
        assert audit_check_exists is False

        # Status enum narrows back: 'awaiting_review' now rejected.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO digests (type, window_start, window_end, status, citations,
                                     body_markdown)
                VALUES (
                    'weekly',
                    '2026-05-04 09:00:00+00',
                    '2026-05-11 09:00:00+00',
                    'awaiting_review',
                    '[]'::jsonb,
                    'body'
                )
                """
            )
    finally:
        await conn.close()


# ─── Test: ORM Digest mapped class exposes the 4 new fields ──────────────────


def test_038_orm_digest_review_columns_registered(app_env) -> None:
    """Digest mapped class declares 4 new review-gate columns."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    cols = models.Digest.__table__.columns
    for name in (
        "awaiting_review_at",
        "published_by_admin_id",
        "approved_at",
        "review_notes",
    ):
        assert name in cols, f"Digest ORM missing {name!r}"
        assert cols[name].nullable is True, f"{name} must be nullable"
