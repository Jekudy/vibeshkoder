"""T2-03 / #103 — Import apply service tests (Stream Delta finale).

Verifies the contract from the issue body:
- Import apply routes through persist_message_with_policy (no direct INSERT into
  chat_messages).
- Synthetic telegram_updates rows: update_id=NULL, ingestion_run_id set.
- Idempotent apply: re-running on same export → zero net DB change.
- Reply resolver wired (#98), tombstone gate (#97), rate limit / chunking (#102),
  checkpoint (#101).
- ingestion_runs row tracks started_at / finished_at / stats_json on success.

Tests use the db_session fixture from tests/conftest.py (outer-tx rollback).
NEVER call session.commit() in the test bodies — the apply path commits per chunk
under SQLAlchemy's join-tx semantics, which still rolls back at fixture teardown.

Cross-stream invariants asserted explicitly (test 10 = direct-insert audit).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text as sa_text

pytestmark = pytest.mark.usefixtures("app_env")

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"
EDITED_MESSAGES = FIXTURE_DIR / "edited_messages.json"


# ─── helpers ───────────────────────────────────────────────────────────────────


async def _create_apply_run(
    db_session,
    *,
    source_path: str,
    chat_id: int,
    source_hash: str,
) -> int:
    """Insert an ingestion_runs row mimicking what init_or_resume_run would create."""
    result = await db_session.execute(
        sa_text(
            """
            INSERT INTO ingestion_runs (run_type, source_name, source_hash, status, config_json)
            VALUES ('import', :source_name, :source_hash, 'running', CAST(:cfg AS JSON))
            RETURNING id
            """
        ),
        {
            "source_name": source_path,
            "source_hash": source_hash,
            "cfg": json.dumps({"chat_id": chat_id}),
        },
    )
    run_id = result.scalar_one()
    await db_session.flush()
    return run_id


def _default_chunking(*, advisory_lock: bool = False, chunk_size: int = 500, sleep_ms: int = 0):
    """Build a ChunkingConfig safe for tests.

    The advisory lock is False by default in tests because the db_session fixture binds
    a single connection; pg_advisory_lock against a fixture connection works, but tests
    that don't need it skip the call to keep the SQL noise low.
    """
    from bot.services.import_chunking import ChunkingConfig

    return ChunkingConfig(
        chunk_size=chunk_size,
        sleep_between_chunks_ms=sleep_ms,
        use_advisory_lock=advisory_lock,
    )


async def _count_chat_messages(db_session, chat_id: int) -> int:
    result = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM chat_messages WHERE chat_id = :cid"),
        {"cid": chat_id},
    )
    return int(result.scalar_one())


async def _count_telegram_updates(db_session, ingestion_run_id: int) -> int:
    result = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM telegram_updates WHERE ingestion_run_id = :rid"),
        {"rid": ingestion_run_id},
    )
    return int(result.scalar_one())


# ─── Test 1: Happy path ───────────────────────────────────────────────────────


async def test_apply_happy_path_small_chat(db_session) -> None:
    """Apply small_chat.json once. Assert chat_messages count, telegram_updates with
    update_id=NULL + correct ingestion_run_id, message_versions.imported_final=TRUE."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999  # matches small_chat.json envelope id
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_apply_happy_path",
    )

    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    # 5 user messages + 1 service in fixture; service is skipped, 5 user messages
    # apply (none are tombstoned, none are duplicate).
    assert report.applied_count == 5
    assert report.skipped_service_count == 1
    assert report.skipped_tombstone_count == 0
    assert report.skipped_duplicate_count == 0
    assert report.error_count == 0

    # chat_messages count for this chat == applied_count
    assert await _count_chat_messages(db_session, chat_id) == 5

    # telegram_updates: one per applied OR governance-skipped message.
    # Service messages produce NO synthetic update (per parser contract).
    assert await _count_telegram_updates(db_session, run_id) == 5

    # All synthetic rows have update_id IS NULL and ingestion_run_id matches.
    null_check = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM telegram_updates "
            "WHERE ingestion_run_id = :rid AND update_id IS NULL"
        ),
        {"rid": run_id},
    )
    assert int(null_check.scalar_one()) == 5

    # message_versions.imported_final=TRUE for every imported row.
    versions = await db_session.execute(
        sa_text(
            """
            SELECT mv.imported_final
            FROM message_versions mv
            JOIN chat_messages cm ON mv.chat_message_id = cm.id
            WHERE cm.chat_id = :cid
            """
        ),
        {"cid": chat_id},
    )
    flags = [row[0] for row in versions.all()]
    assert len(flags) == 5
    assert all(flags), "every imported message_versions row must have imported_final=TRUE"


# ─── Test 2: Idempotency ──────────────────────────────────────────────────────


async def test_apply_is_idempotent(db_session) -> None:
    """Apply twice (separate ingestion_runs). Second run produces zero new chat_messages.
    Every message in the second run lands in skipped_duplicate_count."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    # First run
    run1 = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_idempotent_first",
    )
    rep1 = await run_apply(
        db_session,
        ingestion_run_id=run1,
        resume_point=None,
        chunking_config=_default_chunking(),
    )
    assert rep1.applied_count == 5
    cm_count_after_first = await _count_chat_messages(db_session, chat_id)
    assert cm_count_after_first == 5

    # Second run — separate run id.
    run2 = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_idempotent_second",
    )
    rep2 = await run_apply(
        db_session,
        ingestion_run_id=run2,
        resume_point=None,
        chunking_config=_default_chunking(),
    )
    # Zero new chat_messages, every user message hits the duplicate gate.
    assert rep2.applied_count == 0
    assert rep2.skipped_duplicate_count == 5
    assert await _count_chat_messages(db_session, chat_id) == cm_count_after_first

    # Synthetic telegram_updates for run2: zero (duplicate gate fires before insert).
    assert await _count_telegram_updates(db_session, run2) == 0


# ─── Test 3: Tombstone gate ───────────────────────────────────────────────────


async def test_apply_tombstone_blocks_message(db_session) -> None:
    """Pre-insert a forget_event for one export message id. Apply must skip that
    message (skipped_tombstone_count == 1) and persist must NOT be called for it."""
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    blocked_msg_id = 1003  # voice message in small_chat fixture

    # Pre-create tombstone
    await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(blocked_msg_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"message:{chat_id}:{blocked_msg_id}",
    )
    await db_session.flush()

    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_tombstone_block",
    )

    # Spy on persist_message_with_policy so we can assert it was NOT called for the
    # blocked id. We don't replace it — we wrap to keep behaviour identical.
    import bot.services.import_apply as apply_mod

    original_persist = apply_mod.persist_message_with_policy
    persisted_msg_ids: list[int] = []

    async def _spy_persist(session, message, **kwargs):
        persisted_msg_ids.append(message.message_id)
        return await original_persist(session, message, **kwargs)

    with patch("bot.services.import_apply.persist_message_with_policy", new=_spy_persist):
        report = await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(),
        )

    assert report.skipped_tombstone_count == 1
    assert report.applied_count == 4  # 5 user msgs minus 1 tombstoned
    assert blocked_msg_id not in persisted_msg_ids, (
        f"persist_message_with_policy must NOT be called for tombstoned id {blocked_msg_id}"
    )

    # Tombstoned message did NOT land in chat_messages
    blocked = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM chat_messages WHERE chat_id = :cid AND message_id = :mid"
        ),
        {"cid": chat_id, "mid": blocked_msg_id},
    )
    assert int(blocked.scalar_one()) == 0


# ─── Test 4: Governance gate ──────────────────────────────────────────────────


async def test_apply_governance_offrecord_keeps_only_synthetic_audit_row(db_session) -> None:
    """Apply edited_messages.json which contains an #offrecord message (id 2004).
    Verify telegram_updates row exists, but persist_message_with_policy is NOT called
    for the offrecord message and no chat_messages/message_versions content row is created."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(EDITED_MESSAGES),
        chat_id=chat_id,
        source_hash="hash_governance_offrecord",
    )

    import bot.services.import_apply as apply_mod

    original_persist = apply_mod.persist_message_with_policy
    persisted_msg_ids: list[int] = []

    async def _spy_persist(session, message, **kwargs):
        persisted_msg_ids.append(message.message_id)
        return await original_persist(session, message, **kwargs)

    with patch("bot.services.import_apply.persist_message_with_policy", new=_spy_persist):
        report = await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(),
        )

    # Fixture contains 5 user messages: one #nomem (2003) persists, one #offrecord (2004)
    # is governance-skipped after the synthetic update.
    assert report.applied_count == 4
    assert report.skipped_governance_count == 1
    assert 2004 not in persisted_msg_ids

    # The offrecord message should NOT land in chat_messages at all.
    offrecord_cm = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE chat_id = :cid AND message_id = :mid"
        ),
        {"cid": chat_id, "mid": 2004},
    )
    assert int(offrecord_cm.scalar_one()) == 0

    # Synthetic telegram_updates audit row remains and is marked redacted.
    raw_row = await db_session.execute(
        sa_text(
            "SELECT update_id, ingestion_run_id, is_redacted, redaction_reason "
            "FROM telegram_updates WHERE chat_id = :cid AND message_id = :mid"
        ),
        {"cid": chat_id, "mid": 2004},
    )
    update_id, raw_run_id, is_redacted, redaction_reason = raw_row.one()
    assert update_id is None
    assert raw_run_id == run_id
    assert is_redacted is True
    assert redaction_reason == "offrecord"


# ─── Test 5: Reply resolver wired ─────────────────────────────────────────────


async def test_apply_resolves_intra_export_reply(db_session) -> None:
    """small_chat fixture has msg 1004 replying to msg 1001. After apply, the reply
    target export-id is preserved on the chat_messages row."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_reply_resolve",
    )
    await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    # The reply-source row (msg 1004) should carry reply_to_message_id = 1001.
    reply_row = await db_session.execute(
        sa_text(
            "SELECT reply_to_message_id FROM chat_messages "
            "WHERE chat_id = :cid AND message_id = :mid"
        ),
        {"cid": chat_id, "mid": 1004},
    )
    assert reply_row.scalar_one() == 1001


# ─── Test 6: Edit history / imported_final ────────────────────────────────────


async def test_apply_sets_imported_final_and_edit_date(db_session) -> None:
    """edited_messages.json has 2 messages with `edited_unixtime`. After apply,
    those rows have non-NULL edit_date and imported_final=TRUE."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(EDITED_MESSAGES),
        chat_id=chat_id,
        source_hash="hash_edit_history",
    )
    await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    # 4 persisted user messages → 4 message_versions rows, all imported_final=TRUE.
    # The offrecord message 2004 keeps only a synthetic telegram_updates audit row.
    rows = await db_session.execute(
        sa_text(
            """
            SELECT cm.message_id, mv.imported_final, mv.edit_date
            FROM message_versions mv
            JOIN chat_messages cm ON mv.chat_message_id = cm.id
            WHERE cm.chat_id = :cid
            ORDER BY cm.message_id
            """
        ),
        {"cid": chat_id},
    )
    by_msg_id = {row[0]: (row[1], row[2]) for row in rows.all()}
    assert set(by_msg_id) == {2001, 2002, 2003, 2005}

    # Every imported row has imported_final=TRUE
    for mid, (final, _ed) in by_msg_id.items():
        assert final is True, f"msg {mid} expected imported_final=TRUE"

    # Messages 2001 and 2002 have edited_unixtime → edit_date set.
    assert by_msg_id[2001][1] is not None, "msg 2001 has edited_unixtime → edit_date should be set"
    assert by_msg_id[2002][1] is not None, "msg 2002 has edited_unixtime → edit_date should be set"
    # Messages 2003/2004/2005 have no edited field → edit_date NULL
    assert by_msg_id[2003][1] is None
    assert by_msg_id[2005][1] is None


# ─── Test 7: Checkpoint / resume from mid-chunk failure ───────────────────────


async def test_apply_resume_skips_processed_messages(db_session) -> None:
    """Simulate a prior partial run by passing resume_point=1003. Apply should skip
    messages with id <= 1003 (counted under skipped_resume_count) and apply the rest."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_resume_skip",
    )

    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=1003,
        chunking_config=_default_chunking(),
    )

    # small_chat ids: 1001, 1002, 1003 (user), 1004, 1005, 1006 (service).
    # ids <= 1003: 1001, 1002, 1003 → skipped_resume_count = 3.
    # remaining: 1004, 1005 (user) + 1006 (service) → apply 2 user, skip 1 service.
    assert report.skipped_resume_count == 3
    assert report.applied_count == 2
    assert report.skipped_service_count == 1


# ─── Test 8: Advisory lock — re-entrant blocks ────────────────────────────────


async def test_apply_advisory_lock_acquired(db_session) -> None:
    """When use_advisory_lock=True, run_apply MUST call acquire_advisory_lock with the
    bound connection and the run id. Verify via spy that the lock primitive is invoked."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_advisory_lock_spy",
    )

    spy_calls: list[tuple] = []

    # Wrap the original context manager so we can record invocation but keep behaviour.
    import bot.services.import_apply as apply_mod
    original_lock = apply_mod.acquire_advisory_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _spy_lock(connection, run_id_arg):
        spy_calls.append((id(connection), run_id_arg))
        async with original_lock(connection, run_id_arg):
            yield

    with patch("bot.services.import_apply.acquire_advisory_lock", new=_spy_lock):
        await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(advisory_lock=True),
        )

    assert len(spy_calls) == 1
    _conn_id, recorded_run_id = spy_calls[0]
    assert recorded_run_id == run_id


# ─── Test 9: Chunking — checkpoint advances per chunk ─────────────────────────


async def test_apply_chunking_advances_checkpoint(db_session) -> None:
    """chunk_size=2 against a 6-message export → 3 chunks (2 + 2 + 2 messages including
    service). checkpoint should be saved exactly 3 times via save_checkpoint, and the
    inter-chunk asyncio.sleep is invoked 3 times (sleep_ms > 0)."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_chunking_advance",
    )

    save_chkpt_calls: list[tuple] = []
    sleep_calls: list[float] = []

    import bot.services.import_apply as apply_mod
    original_chkpt = apply_mod.save_checkpoint

    async def _spy_chkpt(session, *, ingestion_run_id, last_processed_export_msg_id, chunk_index):
        save_chkpt_calls.append((ingestion_run_id, last_processed_export_msg_id, chunk_index))
        await original_chkpt(
            session,
            ingestion_run_id=ingestion_run_id,
            last_processed_export_msg_id=last_processed_export_msg_id,
            chunk_index=chunk_index,
        )

    async def _spy_sleep(seconds):
        sleep_calls.append(seconds)

    with patch("bot.services.import_apply.save_checkpoint", new=_spy_chkpt), \
         patch("bot.services.import_apply.asyncio.sleep", new=_spy_sleep):
        await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(chunk_size=2, sleep_ms=10),
        )

    # 6 messages / chunk_size=2 → 3 chunks → 3 checkpoint saves + 3 inter-chunk sleeps.
    assert len(save_chkpt_calls) == 3, f"expected 3 checkpoint saves, got {len(save_chkpt_calls)}"
    # All saves are for the right run id; chunk_index increments 0, 1, 2.
    chunk_indices = [c[2] for c in save_chkpt_calls]
    assert chunk_indices == [0, 1, 2]

    assert len(sleep_calls) == 3
    # All sleep durations are 0.01 seconds (10ms / 1000).
    assert all(abs(s - 0.01) < 1e-6 for s in sleep_calls)


# ─── Test 10: Direct-insert audit ─────────────────────────────────────────────


def test_import_apply_module_has_no_direct_chat_messages_insert() -> None:
    """Static check: the import_apply.py source MUST NOT contain raw INSERT into
    chat_messages or insert(ChatMessage) — apply must route through
    persist_message_with_policy. This is the safety rail enforcing ADR-0007.
    """
    src = Path(__file__).parents[2] / "bot" / "services" / "import_apply.py"
    text = src.read_text(encoding="utf-8")
    forbidden_substrings = [
        "INSERT INTO chat_messages",
        "insert(ChatMessage)",
        "pg_insert(ChatMessage)",
        "session.add(ChatMessage(",
    ]
    for needle in forbidden_substrings:
        assert needle not in text, (
            f"bot/services/import_apply.py contains forbidden substring {needle!r} — "
            "apply MUST go through persist_message_with_policy (ADR-0007)."
        )


# ─── Test 11: Audit trail (stats_json populated) ──────────────────────────────


async def test_apply_audit_trail_stats_json(db_session) -> None:
    """After a successful run, ingestion_runs.stats_json contains the apply counts
    via the deep-merge save_checkpoint mechanism. started_at is set on row creation
    by the schema default."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_audit_trail",
    )
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    # report carries the canonical counts
    assert report.applied_count == 5
    assert report.last_processed_export_msg_id == 1006

    # Stats_json contains last_processed_export_msg_id (set by save_checkpoint).
    stats_row = await db_session.execute(
        sa_text("SELECT stats_json, started_at FROM ingestion_runs WHERE id = :id"),
        {"id": run_id},
    )
    stats, started = stats_row.one()
    assert started is not None
    assert stats is not None
    assert stats.get("last_processed_export_msg_id") == 1006


# ─── Test 12: Error envelope — bad message resilience ─────────────────────────


async def test_apply_resilient_to_bad_user_resolution(db_session) -> None:
    """A message with from_id=None (malformed) should bump error_count and continue
    with the rest of the chunk — not abort the whole run."""
    from bot.services.import_apply import run_apply

    # Build a tiny synthetic export with one good and one bad message.
    bad_export = {
        "name": "Malformed Test Chat",
        "type": "private_supergroup",
        "id": -2002,
        "messages": [
            {
                "id": 7001,
                "type": "message",
                "date": "2024-03-01T10:00:00",
                "date_unixtime": "1709287200",
                "from": "User OK",
                "from_id": "user2000001",
                "text": "ok",
                "text_entities": [{"type": "plain", "text": "ok"}],
            },
            {
                "id": 7002,
                "type": "message",
                "date": "2024-03-01T10:01:00",
                "date_unixtime": "1709287260",
                # Intentionally NO from_id — user resolution returns None,
                # apply bumps error_count and continues.
                "text": "no sender",
                "text_entities": [{"type": "plain", "text": "no sender"}],
            },
        ],
    }
    tmp = Path(__file__).parents[1] / "fixtures" / "td_export" / "_tmp_bad_export.json"
    tmp.write_text(json.dumps(bad_export), encoding="utf-8")
    try:
        run_id = await _create_apply_run(
            db_session,
            source_path=str(tmp),
            chat_id=-2002,
            source_hash="hash_bad_export_resilient",
        )
        report = await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(),
        )

        assert report.applied_count == 1  # only the good message
        assert report.error_count == 1
        assert 7002 in report.error_export_msg_ids
    finally:
        tmp.unlink(missing_ok=True)
