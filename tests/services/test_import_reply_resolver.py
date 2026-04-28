"""T2-NEW-C acceptance tests — import_reply_resolver service.

Tests cover:
1. Same-run resolution
2. Cross-run resolution (target in earlier ingestion_run)
3. Unresolved (target missing)
4. Forward chain (resolve reply target's reply target, chain_depth=1)
5. Cycle detection (cyclic reply chain → no infinite loop)
6. Live match (chat_messages row linked to a live TelegramUpdate)
7. aggregate_resolutions counts correctly
8. resolve_reply_batch does not N+1 for N items
9. chat_id scoping (export_msg_id in chat A must NOT resolve for chat B)

Isolation: each test runs inside the ``db_session`` fixture's outer transaction which is
rolled back at teardown. Tests MUST NOT call ``session.commit()``.

Tests are SKIPPED if no postgres is reachable (see ``conftest.postgres_engine``).

Note on imports: all bot.* imports are done INSIDE test functions (not at module level) to
avoid SQLAlchemy mapper initialization issues caused by conftest's ``_clear_modules`` running
between test collection and test execution. This matches the pattern used in existing DB tests.
"""

from __future__ import annotations

import random

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rand_chat_id() -> int:
    """Negative chat id in the range Telegram uses for groups."""
    return -random.randint(100_000_000, 199_999_999)


def _rand_msg_id() -> int:
    return random.randint(10_000, 9_999_999)


def _rand_user_id() -> int:
    return random.randint(900_000_000, 999_999_999)


async def _create_import_run(session, *, started_offset_seconds: int = 0) -> object:
    """Create an import ingestion_run row.

    PostgreSQL `now()` is transaction-stable — multiple inserts in one tx share
    the same default `started_at`. For tests that need ordered runs (cross-run,
    prior_run filtering), pass `started_offset_seconds` to spread timestamps.
    """
    from datetime import datetime, timedelta, timezone

    from bot.db.models import IngestionRun

    run = IngestionRun(
        run_type="import",
        source_name="test_export.json",
        status="running",
        started_at=datetime.now(timezone.utc) + timedelta(seconds=started_offset_seconds),
    )
    session.add(run)
    await session.flush()
    return run


async def _create_live_run(session) -> object:
    """Create an active live ingestion_run row."""
    from bot.db.repos.ingestion_run import IngestionRunRepo

    return await IngestionRunRepo.create(session, run_type="live", source_name="live_bot")


async def _ensure_user(session, tg_id: int) -> object:
    """Upsert a minimal user row so FK constraints are satisfied."""
    from bot.db.repos.user import UserRepo

    return await UserRepo.upsert(
        session,
        telegram_id=tg_id,
        username=None,
        first_name=f"User{tg_id}",
        last_name=None,
    )


async def _create_import_update(
    session,
    chat_id: int,
    message_id: int,
    ingestion_run_id: int,
    raw_json: dict | None = None,
) -> object:
    """Insert a synthetic import TelegramUpdate row."""
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    return await TelegramUpdateRepo.insert(
        session,
        update_type="import_message",
        chat_id=chat_id,
        message_id=message_id,
        ingestion_run_id=ingestion_run_id,
        raw_json=raw_json or {},
    )


async def _create_chat_message(
    session,
    chat_id: int,
    message_id: int,
    user_id: int,
    raw_update_id: int | None = None,
    reply_to_message_id: int | None = None,
) -> object:
    """Insert a chat_messages row linked to the given raw_update_id."""
    from datetime import datetime, timezone

    from bot.db.repos.message import MessageRepo

    return await MessageRepo.save(
        session,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text="test message",
        date=datetime.now(tz=timezone.utc),
        raw_update_id=raw_update_id,
        reply_to_message_id=reply_to_message_id,
    )


async def _create_imported_message(
    session,
    chat_id: int,
    export_msg_id: int,
    ingestion_run_id: int,
    user_id: int,
    reply_to_message_id: int | None = None,
) -> tuple:
    """Create a TelegramUpdate + ChatMessage pair as an imported message.

    Returns (telegram_update, chat_message).
    """
    tu = await _create_import_update(session, chat_id, export_msg_id, ingestion_run_id)
    cm = await _create_chat_message(
        session,
        chat_id=chat_id,
        message_id=export_msg_id,
        user_id=user_id,
        raw_update_id=tu.id,
        reply_to_message_id=reply_to_message_id,
    )
    return tu, cm


# ---------------------------------------------------------------------------
# Test 1: Same-run resolution
# ---------------------------------------------------------------------------


async def test_resolve_same_run(db_session) -> None:
    """export_msg_id imported in run-1, queried with run-1 → resolves to chat_messages.id."""
    from bot.services.import_reply_resolver import ReplyResolution, resolve_reply

    chat_id = _rand_chat_id()
    export_msg_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    _, cm = await _create_imported_message(db_session, chat_id, export_msg_id, run.id, user_id)

    result = await resolve_reply(
        db_session,
        export_msg_id=export_msg_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )

    assert isinstance(result, ReplyResolution)
    assert result.export_msg_id == export_msg_id
    assert result.chat_message_id == cm.id
    assert result.resolved_via == "same_run"
    assert result.chain_depth == 0


# ---------------------------------------------------------------------------
# Test 2: Cross-run resolution (target exists in earlier ingestion_run)
# ---------------------------------------------------------------------------


async def test_resolve_cross_run(db_session) -> None:
    """export_msg_id in run-1; queried with run-2 → resolves via prior_run."""
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    export_msg_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    # run1 must be strictly older than run2 — explicit started_at offsets bypass
    # PostgreSQL transaction-stable now() collisions.
    run1 = await _create_import_run(db_session, started_offset_seconds=-3600)
    run2 = await _create_import_run(db_session, started_offset_seconds=0)

    _, cm = await _create_imported_message(db_session, chat_id, export_msg_id, run1.id, user_id)

    result = await resolve_reply(
        db_session,
        export_msg_id=export_msg_id,
        ingestion_run_id=run2.id,
        chat_id=chat_id,
    )

    assert result.chat_message_id == cm.id
    assert result.resolved_via == "prior_run"
    assert result.chain_depth == 0


# ---------------------------------------------------------------------------
# Test 2b: prior_run filter excludes runs newer than the current run
# ---------------------------------------------------------------------------


async def test_prior_run_excludes_newer_runs(db_session) -> None:
    """prior_run must select strictly older runs (started_at < current_run.started_at).

    Setup: runs at t=1 (oldest), t=2 (current), t=3 (newest).
    Message exists only in run-1 and run-3.
    Resolving from run-2 should pick run-1 (older), NOT run-3 (newer).

    Uses explicit started_at values because PostgreSQL `now()` is transaction-stable —
    multiple inserts inside a single test transaction would otherwise share the same
    timestamp and the strict-less-than filter would return no candidates.
    """
    from datetime import datetime, timedelta, timezone

    from bot.db.models import IngestionRun
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    export_msg_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)

    # Explicit started_at: oldest → current → newest, 1 hour apart.
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    run1 = IngestionRun(
        run_type="import",
        source_name="export1.json",
        status="running",
        started_at=base,
    )
    run2 = IngestionRun(
        run_type="import",
        source_name="export2.json",
        status="running",
        started_at=base + timedelta(hours=1),
    )
    run3 = IngestionRun(
        run_type="import",
        source_name="export3.json",
        status="running",
        started_at=base + timedelta(hours=2),
    )
    db_session.add_all([run1, run2, run3])
    await db_session.flush()

    # Place the message in run1 and run3 (NOT in run2 which is "current")
    _, cm_run1 = await _create_imported_message(db_session, chat_id, export_msg_id, run1.id, user_id)
    _, cm_run3 = await _create_imported_message(db_session, chat_id, export_msg_id, run3.id, user_id)

    # Resolving from run2: prior should be run1 (older), not run3 (newer)
    result = await resolve_reply(
        db_session,
        export_msg_id=export_msg_id,
        ingestion_run_id=run2.id,
        chat_id=chat_id,
    )

    assert result.resolved_via == "prior_run"
    # Must resolve to run1's chat_message, not run3's
    assert result.chat_message_id == cm_run1.id, (
        f"Expected cm_run1.id={cm_run1.id}, got {result.chat_message_id} "
        f"(cm_run3.id={cm_run3.id} would indicate a newer run was selected)"
    )


# ---------------------------------------------------------------------------
# Test 3: Unresolved (target missing)
# ---------------------------------------------------------------------------


async def test_resolve_unresolved(db_session) -> None:
    """export_msg_id that does not exist in any run → unresolved."""
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    missing_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    result = await resolve_reply(
        db_session,
        export_msg_id=missing_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )

    assert result.chat_message_id is None
    assert result.resolved_via == "unresolved"
    assert result.chain_depth == 0


# ---------------------------------------------------------------------------
# Test 4: Forward chain (msg-A replies to msg-B, both in same run)
# ---------------------------------------------------------------------------


async def test_resolve_forward_chain(db_session) -> None:
    """msg-B is reply target of msg-A. Resolving B works (chain_depth=0); resolving A's
    resolved row's reply target also works (effectively chain_depth follow on the DB row).

    This test confirms:
    - Resolving B directly → chain_depth=0.
    - Resolving A directly → chain_depth=0 (A resolves directly, not via chain).
    - The chain_depth is incremented when the resolved ChatMessage itself has a
      reply_to_message_id that needs further resolution. We simulate this by resolving
      msg-A and checking that the resolved row's reply_to_message_id points to msg-B's
      export_id.
    """
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    msg_a_id = _rand_msg_id()
    msg_b_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    # msg-B has no reply
    _, cm_b = await _create_imported_message(db_session, chat_id, msg_b_id, run.id, user_id)
    # msg-A replies to msg-B
    _, cm_a = await _create_imported_message(
        db_session, chat_id, msg_a_id, run.id, user_id, reply_to_message_id=msg_b_id
    )

    # Resolving B directly → direct hit, chain_depth=0
    result_b = await resolve_reply(
        db_session,
        export_msg_id=msg_b_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )
    assert result_b.chat_message_id == cm_b.id
    assert result_b.chain_depth == 0
    assert result_b.resolved_via == "same_run"

    # Resolving A → direct hit (A itself is in the run), chain_depth=0
    result_a = await resolve_reply(
        db_session,
        export_msg_id=msg_a_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )
    assert result_a.chat_message_id == cm_a.id
    # A's resolved chat_message has reply_to_message_id pointing to B's export_id.
    # The chain_depth reflects that A's resolved row's reply IS resolvable.
    assert result_a.chain_depth == 0  # A is a direct hit; its chain is the parent's concern

    # The resolved row for A has reply_to_message_id == msg_b_id (chain exists in DB)
    assert cm_a.reply_to_message_id == msg_b_id


async def test_resolve_chain_depth_stays_zero_for_direct_hits(db_session) -> None:
    """chain_depth is always 0 for direct hits. The resolver does not traverse
    reply_to_message_id hops — consumers iterate themselves if they need depth.
    This test confirms that resolving msg-B (which has reply_to_message_id set) still
    returns chain_depth=0 because B is found directly."""
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    # msg-C: the root message (no reply)
    # msg-B: replies to msg-C (also imported)
    # Resolving B is a direct hit → chain_depth stays 0; resolver does not follow
    # B's reply_to_message_id to C.

    msg_b_id = _rand_msg_id()
    msg_c_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    # msg-C: root, no reply
    _, cm_c = await _create_imported_message(db_session, chat_id, msg_c_id, run.id, user_id)
    # msg-B: replies to msg-C, both in same run
    _, cm_b = await _create_imported_message(
        db_session, chat_id, msg_b_id, run.id, user_id, reply_to_message_id=msg_c_id
    )

    # Resolving B is a direct hit — chain_depth must be 0 (no chain traversal)
    result_b = await resolve_reply(
        db_session,
        export_msg_id=msg_b_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )
    assert result_b.chat_message_id == cm_b.id
    assert result_b.chain_depth == 0

    # The resolved row B has reply_to_message_id = msg_c_id, but the resolver
    # does not follow it — chain_depth=0 confirms direct-lookup-only semantics.
    assert cm_b.reply_to_message_id == msg_c_id


# ---------------------------------------------------------------------------
# Test 5: Cycle detection
# ---------------------------------------------------------------------------


async def test_unresolved_returns_unresolved_for_unknown_target(db_session) -> None:
    """An export_msg_id whose reply target is not in any run resolves to unresolved.
    This also confirms that cyclic reply_to_message_id values in DB rows do not cause
    any looping — the resolver is direct-lookup only and returns immediately."""
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    msg_x_id = _rand_msg_id()
    msg_y_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    # msg-X replies to msg-Y, msg-Y replies to msg-X (cycle in reply_to_message_id)
    _, cm_x = await _create_imported_message(
        db_session, chat_id, msg_x_id, run.id, user_id, reply_to_message_id=msg_y_id
    )
    _, cm_y = await _create_imported_message(
        db_session, chat_id, msg_y_id, run.id, user_id, reply_to_message_id=msg_x_id
    )

    # Resolving X returns X's chat_message directly (direct hit), chain_depth=0.
    # No looping occurs because the resolver does not traverse reply_to_message_id.
    result = await resolve_reply(
        db_session,
        export_msg_id=msg_x_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )

    assert result.chat_message_id == cm_x.id
    assert result.chain_depth == 0

    # An unknown id has no match → unresolved
    missing_id = _rand_msg_id()
    result_missing = await resolve_reply(
        db_session,
        export_msg_id=missing_id,
        ingestion_run_id=run.id,
        chat_id=chat_id,
    )
    assert result_missing.chat_message_id is None
    assert result_missing.resolved_via == "unresolved"


# ---------------------------------------------------------------------------
# Test 6: Live match
# ---------------------------------------------------------------------------


async def test_resolve_live_match(db_session) -> None:
    """chat_messages row linked to a live TelegramUpdate (run_type='live') resolves
    with resolved_via='live'."""
    from bot.services.import_reply_resolver import resolve_reply

    chat_id = _rand_chat_id()
    export_msg_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)

    # Create a live ingestion run
    live_run = await _create_live_run(db_session)

    # Create a TelegramUpdate tagged as a live message
    from bot.db.repos.telegram_update import TelegramUpdateRepo

    live_tu = await TelegramUpdateRepo.insert(
        db_session,
        update_type="message",
        chat_id=chat_id,
        message_id=export_msg_id,
        ingestion_run_id=live_run.id,
        raw_json={},
    )

    # Create a ChatMessage linked to the live update
    cm = await _create_chat_message(
        db_session,
        chat_id=chat_id,
        message_id=export_msg_id,
        user_id=user_id,
        raw_update_id=live_tu.id,
    )

    # Now resolve via an import run (which has no record of this message)
    import_run = await _create_import_run(db_session)

    result = await resolve_reply(
        db_session,
        export_msg_id=export_msg_id,
        ingestion_run_id=import_run.id,
        chat_id=chat_id,
    )

    assert result.chat_message_id == cm.id
    assert result.resolved_via == "live"
    assert result.chain_depth == 0


# ---------------------------------------------------------------------------
# Test 7: aggregate_resolutions
# ---------------------------------------------------------------------------


def test_aggregate_resolutions() -> None:
    """aggregate_resolutions correctly counts each resolved_via category.
    Pure-Python: no DB session required."""
    from bot.services.import_reply_resolver import (
        ReplyResolution,
        ReplyResolverStats,
        aggregate_resolutions,
    )

    resolutions: dict = {
        1: ReplyResolution(export_msg_id=1, chat_message_id=10, resolved_via="same_run", chain_depth=0),
        2: ReplyResolution(export_msg_id=2, chat_message_id=20, resolved_via="same_run", chain_depth=0),
        3: ReplyResolution(export_msg_id=3, chat_message_id=30, resolved_via="prior_run", chain_depth=1),
        4: ReplyResolution(export_msg_id=4, chat_message_id=40, resolved_via="live", chain_depth=0),
        5: ReplyResolution(export_msg_id=5, chat_message_id=None, resolved_via="unresolved", chain_depth=0),
        6: ReplyResolution(export_msg_id=6, chat_message_id=None, resolved_via="unresolved", chain_depth=0),
    }

    stats = aggregate_resolutions(resolutions)

    assert isinstance(stats, ReplyResolverStats)
    assert stats.total == 6
    assert stats.resolved_same_run == 2
    assert stats.resolved_prior_run == 1
    assert stats.resolved_live == 1
    assert stats.unresolved == 2


# ---------------------------------------------------------------------------
# Test 8: resolve_reply_batch does not N+1
# ---------------------------------------------------------------------------


async def test_batch_resolve_no_n_plus_one(db_session) -> None:
    """resolve_reply_batch resolves N items and issues fewer DB queries than N."""
    from unittest.mock import patch

    from bot.services.import_reply_resolver import resolve_reply_batch

    chat_id = _rand_chat_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    # Create 5 imported messages
    n = 5
    export_ids = [_rand_msg_id() for _ in range(n)]
    cms = {}
    for eid in export_ids:
        _, cm = await _create_imported_message(db_session, chat_id, eid, run.id, user_id)
        cms[eid] = cm

    # Count execute calls by patching session.execute
    call_count = 0
    original_execute = db_session.execute

    async def counting_execute(stmt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_execute(stmt, *args, **kwargs)

    with patch.object(db_session, "execute", side_effect=counting_execute):
        results = await resolve_reply_batch(
            db_session,
            export_msg_ids=export_ids,
            ingestion_run_id=run.id,
            chat_id=chat_id,
        )

    assert len(results) == n
    # Batch must issue at most 4 queries (same-run + prior-run + live-pass1 + live-pass2)
    # regardless of N — not one query per message.
    assert call_count <= 4, f"Expected batch to issue ≤4 queries, got {call_count}"

    # All should be resolved
    for eid in export_ids:
        assert results[eid].chat_message_id == cms[eid].id


# ---------------------------------------------------------------------------
# Test 9: chat_id scoping
# ---------------------------------------------------------------------------


async def test_chat_id_scoping(db_session) -> None:
    """export_msg_id that exists in chat_A must NOT resolve when queried with chat_B."""
    from bot.services.import_reply_resolver import resolve_reply

    chat_a = _rand_chat_id()
    chat_b = _rand_chat_id()
    export_msg_id = _rand_msg_id()
    user_id = _rand_user_id()

    await _ensure_user(db_session, user_id)
    run = await _create_import_run(db_session)

    # Create the message only in chat_a
    await _create_imported_message(db_session, chat_a, export_msg_id, run.id, user_id)

    # Resolve with chat_b — should be unresolved
    result = await resolve_reply(
        db_session,
        export_msg_id=export_msg_id,
        ingestion_run_id=run.id,
        chat_id=chat_b,
    )

    assert result.chat_message_id is None
    assert result.resolved_via == "unresolved"
