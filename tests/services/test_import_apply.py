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
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text as sa_text

pytestmark = pytest.mark.usefixtures("app_env")

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"
EDITED_MESSAGES = FIXTURE_DIR / "edited_messages.json"
REPLIES_WITH_MEDIA = FIXTURE_DIR / "replies_with_media.json"


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

    # Offrecord sub-case: edited_messages has one #offrecord message that creates only
    # a synthetic audit row on the first run. The second run must treat that audit row
    # as the duplicate marker and create zero new telegram_updates.
    edited_run1 = await _create_apply_run(
        db_session,
        source_path=str(EDITED_MESSAGES),
        chat_id=chat_id,
        source_hash="hash_idempotent_offrecord_first",
    )
    edited_rep1 = await run_apply(
        db_session,
        ingestion_run_id=edited_run1,
        resume_point=None,
        chunking_config=_default_chunking(),
    )
    assert edited_rep1.applied_count == 4
    assert edited_rep1.skipped_governance_count == 1

    edited_counts_before_second = await db_session.execute(
        sa_text(
            """
            SELECT
                (SELECT COUNT(*) FROM chat_messages WHERE chat_id = :cid),
                (SELECT COUNT(*) FROM telegram_updates WHERE chat_id = :cid AND update_type = 'import_message')
            """
        ),
        {"cid": chat_id},
    )
    cm_before, tu_before = map(int, edited_counts_before_second.one())

    edited_run2 = await _create_apply_run(
        db_session,
        source_path=str(EDITED_MESSAGES),
        chat_id=chat_id,
        source_hash="hash_idempotent_offrecord_second",
    )
    edited_rep2 = await run_apply(
        db_session,
        ingestion_run_id=edited_run2,
        resume_point=None,
        chunking_config=_default_chunking(),
    )
    assert edited_rep2.applied_count == 0
    assert edited_rep2.skipped_duplicate_count == 5
    assert await _count_telegram_updates(db_session, edited_run2) == 0

    edited_counts_after_second = await db_session.execute(
        sa_text(
            """
            SELECT
                (SELECT COUNT(*) FROM chat_messages WHERE chat_id = :cid),
                (SELECT COUNT(*) FROM telegram_updates WHERE chat_id = :cid AND update_type = 'import_message')
            """
        ),
        {"cid": chat_id},
    )
    cm_after, tu_after = map(int, edited_counts_after_second.one())
    assert (cm_after, tu_after) == (cm_before, tu_before)


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


async def test_apply_user_tombstone_blocks_all_sender_messages(db_session) -> None:
    """A user:{tg_id} tombstone must block every resolved sender message before
    synthetic telegram_updates or chat_messages rows are written."""
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    blocked_user_id = 1000001
    blocked_msg_ids = [3001, 3004, 3008]  # user1000001 in replies_with_media fixture

    await ForgetEventRepo.create(
        db_session,
        target_type="user",
        target_id=str(blocked_user_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=f"user:{blocked_user_id}",
    )
    await db_session.flush()

    run_id = await _create_apply_run(
        db_session,
        source_path=str(REPLIES_WITH_MEDIA),
        chat_id=chat_id,
        source_hash="hash_user_tombstone_block",
    )

    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    assert report.skipped_tombstone_count == 3
    assert report.tombstone_skip_export_msg_ids == blocked_msg_ids

    blocked_cm = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE chat_id = :cid AND user_id = :uid"
        ),
        {"cid": chat_id, "uid": blocked_user_id},
    )
    assert int(blocked_cm.scalar_one()) == 0

    blocked_updates = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM telegram_updates "
            "WHERE ingestion_run_id = :rid AND message_id IN (3001, 3004, 3008)"
        ),
        {"rid": run_id},
    )
    assert int(blocked_updates.scalar_one()) == 0


# ─── Test 4: Governance gate ──────────────────────────────────────────────────


async def test_apply_governance_offrecord_creates_redacted_row_and_audit(db_session) -> None:
    """Apply edited_messages.json which contains an #offrecord message (id 2004).

    Hotfix #164 H2 fix: persist_message_with_policy IS now called for offrecord messages
    (import path mirrors live path). The helper writes a chat_messages row with
    memory_policy='offrecord' and is_redacted=True (content nulled), creates an
    OffrecordMark, and inserts a redacted MessageVersion. skipped_governance_count is
    bumped; applied_count is NOT bumped.
    """
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

    # Fixture contains 5 user messages: one #nomem (2003) persists normally,
    # one #offrecord (2004) is persisted as redacted (H2 fix).
    assert report.applied_count == 4
    assert report.skipped_governance_count == 1
    # H2 fix: persist IS called for msg 2004 (helper handles offrecord internally)
    assert 2004 in persisted_msg_ids

    # H2 fix: chat_messages row IS created with memory_policy='offrecord', is_redacted=True
    offrecord_cm = await db_session.execute(
        sa_text(
            "SELECT memory_policy, is_redacted, text FROM chat_messages "
            "WHERE chat_id = :cid AND message_id = :mid"
        ),
        {"cid": chat_id, "mid": 2004},
    )
    cm_row = offrecord_cm.fetchone()
    assert cm_row is not None, "H2: chat_messages row must exist for imported #offrecord"
    assert cm_row[0] == "offrecord"
    assert cm_row[1] is True
    assert cm_row[2] is None  # text nulled

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
    target uses the resolved chat_messages.message_id, not the resolver's raw PK."""
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

    # Translation guard: the resolver returns chat_messages.id. Seed a live parent whose
    # TelegramUpdate.message_id (7777) differs from chat_messages.message_id (424242);
    # apply must store 424242 as the live-handler-equivalent reply target.
    await db_session.execute(
        sa_text(
            """
            INSERT INTO users (id, first_name, is_imported_only)
            VALUES (4242, 'Live Parent', false)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    live_run_id = (
        await db_session.execute(
            sa_text(
                """
                INSERT INTO ingestion_runs (run_type, status)
                VALUES ('live', 'running')
                RETURNING id
                """
            )
        )
    ).scalar_one()
    live_raw_id = (
        await db_session.execute(
            sa_text(
                """
                INSERT INTO telegram_updates (
                    update_type, update_id, chat_id, message_id, ingestion_run_id
                )
                VALUES ('message', 7777000, :cid, 7777, :rid)
                RETURNING id
                """
            ),
            {"cid": -3003, "rid": live_run_id},
        )
    ).scalar_one()
    await db_session.execute(
        sa_text(
            """
            INSERT INTO chat_messages (
                message_id, chat_id, user_id, text, date, raw_update_id
            )
            VALUES (424242, :cid, 4242, 'parent', now(), :raw_id)
            """
        ),
        {"cid": -3003, "raw_id": live_raw_id},
    )
    await db_session.flush()

    reply_export = {
        "name": "Reply Translation Chat",
        "type": "private_supergroup",
        "id": -3003,
        "messages": [
            {
                "id": 7778,
                "type": "message",
                "date": "2024-03-01T10:00:00",
                "date_unixtime": "1709287200",
                "from": "Child User",
                "from_id": "user5555",
                "reply_to_message_id": 7777,
                "text": "child",
                "text_entities": [{"type": "plain", "text": "child"}],
            }
        ],
    }
    tmp = FIXTURE_DIR / "_tmp_reply_translation.json"
    tmp.write_text(json.dumps(reply_export), encoding="utf-8")
    try:
        translation_run_id = await _create_apply_run(
            db_session,
            source_path=str(tmp),
            chat_id=-3003,
            source_hash="hash_reply_translation",
        )
        await run_apply(
            db_session,
            ingestion_run_id=translation_run_id,
            resume_point=None,
            chunking_config=_default_chunking(),
        )
    finally:
        tmp.unlink(missing_ok=True)

    translated_reply = await db_session.execute(
        sa_text(
            "SELECT reply_to_message_id FROM chat_messages "
            "WHERE chat_id = :cid AND message_id = 7778"
        ),
        {"cid": -3003},
    )
    assert translated_reply.scalar_one() == 424242


# ─── Test 6: Edit history / imported_final ────────────────────────────────────


async def test_apply_sets_imported_final(db_session) -> None:
    """edited_messages.json has 5 user messages. After apply, all message_versions rows
    have imported_final=TRUE.

    Hotfix #164 §3.2: the explicit MessageVersionRepo.insert_version call with edit_date
    was removed; persist_message_with_policy creates v1 with edit_date=None (v1 is not
    an edit). The #offrecord message 2004 now also creates a redacted v1 (H2 fix).
    """
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

    # Hotfix #164 H2 fix: 5 persisted user messages → 5 message_versions rows, all
    # imported_final=TRUE. The offrecord message 2004 now also gets a redacted
    # chat_messages row + redacted MessageVersion (import path mirrors live path).
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
    # 2004 is now included — redacted v1 row exists (H2 fix)
    assert set(by_msg_id) == {2001, 2002, 2003, 2004, 2005}

    # Every imported row has imported_final=TRUE
    for mid, (final, _ed) in by_msg_id.items():
        assert final is True, f"msg {mid} expected imported_final=TRUE"

    # v1 is not an edit — edit_date is always NULL for the v1 row created by
    # persist_message_with_policy (hotfix #164 §3.2 removed explicit edit_date pass).
    for mid, (_final, edit_date) in by_msg_id.items():
        assert edit_date is None, f"msg {mid} v1 edit_date must be NULL (v1 is not an edit)"


async def test_apply_skips_version_when_live_row_wins_overlap_race(db_session) -> None:
    """If a live row appears after the duplicate gate but before persist, the import
    synthetic raw row remains but imported_final version insertion is skipped."""
    from bot.services.import_apply import run_apply

    chat_id = -4004
    await db_session.execute(
        sa_text(
            """
            INSERT INTO users (id, first_name, is_imported_only)
            VALUES (900001, 'Live Race User', false)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    live_run_id = (
        await db_session.execute(
            sa_text(
                "INSERT INTO ingestion_runs (run_type, status) "
                "VALUES ('live', 'running') RETURNING id"
            )
        )
    ).scalar_one()
    live_raw_id = (
        await db_session.execute(
            sa_text(
                """
                INSERT INTO telegram_updates (
                    update_type, update_id, chat_id, message_id, ingestion_run_id
                )
                VALUES ('message', 9001000, :cid, 9001, :rid)
                RETURNING id
                """
            ),
            {"cid": chat_id, "rid": live_run_id},
        )
    ).scalar_one()
    live_cm_id = (
        await db_session.execute(
            sa_text(
                """
                INSERT INTO chat_messages (
                    message_id, chat_id, user_id, text, date, raw_update_id
                )
                VALUES (9001, :cid, 900001, 'live won', now(), :raw_id)
                RETURNING id
                """
            ),
            {"cid": chat_id, "raw_id": live_raw_id},
        )
    ).scalar_one()
    await db_session.flush()

    overlap_export = {
        "name": "Overlap Race Chat",
        "type": "private_supergroup",
        "id": chat_id,
        "messages": [
            {
                "id": 9001,
                "type": "message",
                "date": "2024-03-01T10:00:00",
                "date_unixtime": "1709287200",
                "from": "Live Race User",
                "from_id": "user900001",
                "text": "import lost",
                "text_entities": [{"type": "plain", "text": "import lost"}],
            }
        ],
    }
    tmp = FIXTURE_DIR / "_tmp_overlap_race.json"
    tmp.write_text(json.dumps(overlap_export), encoding="utf-8")
    try:
        run_id = await _create_apply_run(
            db_session,
            source_path=str(tmp),
            chat_id=chat_id,
            source_hash="hash_overlap_race",
        )

        async def _miss_duplicate_gate(session, chat_id_arg, message_id_arg):
            assert chat_id_arg == chat_id
            assert message_id_arg == 9001
            return None

        with patch("bot.services.import_apply._find_existing_chat_message_id", new=_miss_duplicate_gate):
            report = await run_apply(
                db_session,
                ingestion_run_id=run_id,
                resume_point=None,
                chunking_config=_default_chunking(),
            )
    finally:
        tmp.unlink(missing_ok=True)

    assert report.applied_count == 0
    assert report.skipped_overlap_count == 1

    imported_versions = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM message_versions "
            "WHERE chat_message_id = :cmid AND imported_final IS TRUE"
        ),
        {"cmid": live_cm_id},
    )
    assert int(imported_versions.scalar_one()) == 0

    synthetic_updates = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM telegram_updates "
            "WHERE ingestion_run_id = :rid AND message_id = 9001"
        ),
        {"rid": run_id},
    )
    assert int(synthetic_updates.scalar_one()) == 1


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

    lock_connection_ids: list[int] = []
    persist_connection_ids: list[int] = []

    # Wrap the original context manager so we can record invocation but keep behaviour.
    import bot.services.import_apply as apply_mod
    original_lock = apply_mod.acquire_advisory_lock
    original_persist = apply_mod.persist_message_with_policy

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _spy_lock(connection, run_id_arg):
        lock_connection_ids.append(id(connection))
        assert run_id_arg == run_id
        async with original_lock(connection, run_id_arg):
            yield

    async def _spy_persist(session, message, **kwargs):
        persist_connection_ids.append(id(await session.connection()))
        return await original_persist(session, message, **kwargs)

    with patch("bot.services.import_apply.acquire_advisory_lock", new=_spy_lock), \
         patch("bot.services.import_apply.persist_message_with_policy", new=_spy_persist):
        await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(advisory_lock=True),
        )

    assert len(lock_connection_ids) == 1
    assert persist_connection_ids
    assert set(persist_connection_ids) == {lock_connection_ids[0]}


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
    forbidden_patterns = [
        r"INSERT\s+INTO\s+chat_messages",
        r"\binsert\s*\(\s*ChatMessage",
        r"\bsa\.insert\s*\(\s*ChatMessage",
        r"\bpg_insert\s*\(\s*ChatMessage",
        r"session\.execute\s*\(\s*text\s*\(\s*['\"]\s*INSERT\s+INTO\s+chat_messages",
        r"session\.add\s*\(\s*ChatMessage\s*\(",
    ]
    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, (
            f"bot/services/import_apply.py matches forbidden pattern {pattern!r} — "
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
    with the rest of the chunk. A later validation RuntimeError rolls back only that
    message's savepoint, including its synthetic telegram_updates row."""
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
            {
                "id": 7003,
                "type": "message",
                "date": "2024-03-01T10:02:00",
                "date_unixtime": "1709287320",
                "from": "User Later",
                "from_id": "user2000002",
                "text": "roll back my synthetic raw row",
                "text_entities": [{"type": "plain", "text": "roll back my synthetic raw row"}],
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
        import bot.services.import_apply as apply_mod

        original_persist = apply_mod.persist_message_with_policy

        async def _persist_or_raise(session, message, **kwargs):
            if message.message_id == 7003:
                raise RuntimeError("synthetic validation failure after raw insert")
            return await original_persist(session, message, **kwargs)

        with patch("bot.services.import_apply.persist_message_with_policy", new=_persist_or_raise):
            report = await run_apply(
                db_session,
                ingestion_run_id=run_id,
                resume_point=None,
                chunking_config=_default_chunking(),
            )

        assert report.applied_count == 1  # only the good message
        assert report.error_count == 2
        assert 7002 in report.error_export_msg_ids
        assert 7003 in report.error_export_msg_ids

        raw_7003 = await db_session.execute(
            sa_text(
                "SELECT COUNT(*) FROM telegram_updates "
                "WHERE ingestion_run_id = :rid AND message_id = 7003"
            ),
            {"rid": run_id},
        )
        assert int(raw_7003.scalar_one()) == 0
    finally:
        tmp.unlink(missing_ok=True)


# ─── H1: poll.question and contact.name passed to detect_policy in import path ──


async def test_apply_poll_with_offrecord_question_yields_offrecord_policy(db_session) -> None:
    """H1 fix verification: TD-imported poll whose question contains #offrecord MUST
    result in memory_policy='offrecord' (or the row being redacted/skipped).

    Before the fix, import_apply called detect_policy(text, caption) without
    poll_question, so #offrecord in a poll question was silently stored as 'normal'.
    """
    import json as _json
    from pathlib import Path as _Path

    from bot.services.import_apply import run_apply

    chat_id = -9901

    poll_export = {
        "name": "Poll Offrecord Chat",
        "type": "private_supergroup",
        "id": chat_id,
        "messages": [
            {
                "id": 9901,
                "type": "message",
                "date": "2024-03-01T10:00:00",
                "date_unixtime": "1709287200",
                "from": "PollUser",
                "from_id": "user9901001",
                # poll kind: TD format uses top-level "poll" dict (no media_type for polls)
                "poll": {
                    "question": "Do you agree? #offrecord",
                    "closed": False,
                    "answers": ["Yes", "No"],
                },
            }
        ],
    }

    tmp = _Path(__file__).parents[1] / "fixtures" / "td_export" / "_tmp_poll_offrecord.json"
    tmp.write_text(_json.dumps(poll_export), encoding="utf-8")
    try:
        run_id = await _create_apply_run(
            db_session,
            source_path=str(tmp),
            chat_id=chat_id,
            source_hash="hash_poll_offrecord_h1",
        )

        await db_session.execute(
            sa_text(
                "INSERT INTO users (id, first_name, is_imported_only) "
                "VALUES (9901001, 'PollUser', false) "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        await db_session.flush()

        report = await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(),
        )

        # The poll with #offrecord question must be skipped via governance path.
        assert report.skipped_governance_count == 1, (
            f"H1: expected governance skip for poll with #offrecord question; "
            f"got skipped_governance_count={report.skipped_governance_count}, "
            f"applied_count={report.applied_count}"
        )
        assert report.applied_count == 0

        # H2 fix: offrecord messages now create a chat_messages row with
        # memory_policy='offrecord' and is_redacted=True (import mirrors live path).
        # Verify the row has offrecord policy, NOT normal policy.
        cm_row = await db_session.execute(
            sa_text(
                "SELECT memory_policy, is_redacted FROM chat_messages "
                "WHERE chat_id = :cid AND message_id = 9901"
            ),
            {"cid": chat_id},
        )
        row = cm_row.fetchone()
        assert row is not None, "H1+H2: chat_messages row must exist for offrecord poll (H2 fix)"
        assert row[0] == "offrecord", f"H1: memory_policy must be offrecord; got {row[0]}"
        assert row[1] is True, "H1: is_redacted must be True for offrecord row"
    finally:
        tmp.unlink(missing_ok=True)


async def test_apply_contact_with_offrecord_in_first_name_yields_offrecord_policy(
    db_session,
) -> None:
    """H1 fix coverage: TD contact-shaped message with #offrecord in
    contact_information.first_name must be detected as offrecord and skipped
    (not persisted to chat_messages).

    Before the fix, import_apply read msg["first_name"] / msg["last_name"] at
    the top level — fields that do not exist in the real TD export format.  TD
    nests them under contact_information: {"first_name": ..., "last_name": ...}.
    """
    import json as _json
    from pathlib import Path as _Path

    from bot.services.import_apply import run_apply

    chat_id = -9902

    contact_export = {
        "name": "Contact Offrecord Chat",
        "type": "private_supergroup",
        "id": chat_id,
        "messages": [
            {
                "id": 9902,
                "type": "message",
                "date": "2024-03-01T11:00:00",
                "date_unixtime": "1709290800",
                "from": "Sender",
                "from_id": "user9902001",
                # TD format: contact fields NESTED under contact_information
                "contact_information": {
                    "first_name": "Alice #offrecord",
                    "last_name": "Smith",
                    "phone_number": "+1234567890",
                    "vcard": "",
                },
            }
        ],
    }

    tmp = (
        _Path(__file__).parents[1]
        / "fixtures"
        / "td_export"
        / "_tmp_contact_offrecord.json"
    )
    tmp.write_text(_json.dumps(contact_export), encoding="utf-8")
    try:
        run_id = await _create_apply_run(
            db_session,
            source_path=str(tmp),
            chat_id=chat_id,
            source_hash="hash_contact_offrecord_h1",
        )

        await db_session.execute(
            sa_text(
                "INSERT INTO users (id, first_name, is_imported_only) "
                "VALUES (9902001, 'Sender', false) "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        await db_session.flush()

        report = await run_apply(
            db_session,
            ingestion_run_id=run_id,
            resume_point=None,
            chunking_config=_default_chunking(),
        )

        # The contact with #offrecord in first_name must be skipped via governance.
        assert report.skipped_governance_count == 1, (
            f"H1: expected governance skip for contact with #offrecord in "
            f"contact_information.first_name; "
            f"got skipped_governance_count={report.skipped_governance_count}, "
            f"applied_count={report.applied_count}"
        )
        assert report.applied_count == 0

        # H2 fix: offrecord messages now create a chat_messages row with
        # memory_policy='offrecord' and is_redacted=True (import mirrors live path).
        cm_row = await db_session.execute(
            sa_text(
                "SELECT memory_policy, is_redacted FROM chat_messages "
                "WHERE chat_id = :cid AND message_id = 9902"
            ),
            {"cid": chat_id},
        )
        row = cm_row.fetchone()
        assert row is not None, "H1+H2: chat_messages row must exist for offrecord contact (H2 fix)"
        assert row[0] == "offrecord", f"H1: memory_policy must be offrecord; got {row[0]}"
        assert row[1] is True, "H1: is_redacted must be True for offrecord row"
    finally:
        tmp.unlink(missing_ok=True)


# ─── Hotfix #164 §3.2 tests ───────────────────────────────────────────────────


def _make_offrecord_export(chat_id: int, msg_id: int, text: str) -> str:
    """Build a minimal single-message TD export JSON string."""
    import json
    return json.dumps({
        "id": chat_id,
        "name": "Test Chat",
        "type": "public_supergroup",
        "messages": [
            {
                "id": msg_id,
                "type": "message",
                "date": "2024-01-15T10:00:00",
                "date_unixtime": "1705312800",
                "from": "User One",
                "from_id": "user1000001",
                "text": text,
                "text_entities": [{"type": "plain", "text": text}],
            }
        ],
    })


async def test_import_apply_sets_current_version_id(db_session) -> None:
    """Apply fixture → chat_messages.current_version_id IS NOT NULL for every imported row.

    Verifies CRITICAL 2 fix: import path now delegates to persist_message_with_policy
    which creates v1 and sets current_version_id.
    """
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_164_cvid",
    )

    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    assert report.applied_count > 0

    null_cvid = await db_session.execute(
        sa_text(
            "SELECT COUNT(*) FROM chat_messages WHERE chat_id = :cid AND current_version_id IS NULL"
        ),
        {"cid": chat_id},
    )
    assert int(null_cvid.scalar_one()) == 0, (
        "CRITICAL 2: every imported chat_messages row must have current_version_id set"
    )


async def test_import_apply_v1_normalized_text_populated(db_session) -> None:
    """normalized_text IS NOT NULL for imported text messages → FTS tsv non-empty.

    Verifies CRITICAL 3 fix.
    """
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run_id = await _create_apply_run(
        db_session,
        source_path=str(SMALL_CHAT),
        chat_id=chat_id,
        source_hash="hash_164_normtext",
    )

    await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_default_chunking(),
    )

    # At least one text message should have normalized_text populated in its v1 row.
    populated = await db_session.execute(
        sa_text(
            """
            SELECT COUNT(*) FROM message_versions mv
            JOIN chat_messages cm ON mv.chat_message_id = cm.id
            WHERE cm.chat_id = :cid AND mv.normalized_text IS NOT NULL
            """
        ),
        {"cid": chat_id},
    )
    count = int(populated.scalar_one())
    assert count > 0, "CRITICAL 3: at least one v1 row must have normalized_text populated"


async def test_import_apply_overlap_with_live_v1_skips_via_pre_check(db_session) -> None:
    """Pre-seed live chat_message + live v1; apply same (chat_id, message_id) import.

    Import should be skipped (skipped_overlap_count or skipped_duplicate_count) and only
    the live v1 (imported_final=False) must remain.
    """
    import json as _json
    import tempfile, os

    from bot.db.models import ChatMessage, MessageVersion
    from bot.db.repos.user import UserRepo
    from bot.services.import_apply import run_apply
    from bot.services.message_persistence import persist_message_with_policy
    from sqlalchemy import select
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from datetime import datetime, timezone

    chat_id = -1_009_000_000_001
    msg_id = 50_001
    user_id = 88_001

    # Pre-seed a live user + chat_message
    await UserRepo.upsert(db_session, telegram_id=user_id, username="u88001", first_name="T", last_name=None)

    live_msg = SimpleNamespace(
        message_id=msg_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, username="u88001", first_name="T", last_name=None),
        text="hello world",
        caption=None,
        date=datetime.now(timezone.utc),
        model_dump=MagicMock(return_value={"text": "hello world"}),
        reply_to_message=None, message_thread_id=None,
        photo=None, video=None, voice=None, audio=None, document=None,
        sticker=None, animation=None, video_note=None, location=None,
        contact=None, poll=None, dice=None, forward_origin=None,
        new_chat_members=None, left_chat_member=None, pinned_message=None,
        entities=None, caption_entities=None,
    )
    live_result = await persist_message_with_policy(db_session, live_msg, source="live")
    live_cm = live_result.chat_message
    assert live_cm.current_version_id is not None

    # Build a TD export for the same (chat_id, msg_id)
    export_data = _json.dumps({
        "id": chat_id,
        "name": "Test",
        "type": "public_supergroup",
        "messages": [{
            "id": msg_id,
            "type": "message",
            "date": "2024-01-15T10:00:00",
            "date_unixtime": "1705312800",
            "from": "User One",
            "from_id": f"user{user_id}",
            "text": "hello world\n",  # whitespace divergence
            "text_entities": [{"type": "plain", "text": "hello world\n"}],
        }],
    })

    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        tmp.write_text(export_data)
        run_id = await _create_apply_run(
            db_session, source_path=str(tmp), chat_id=chat_id, source_hash="hash_overlap_pre_check"
        )
        report = await run_apply(
            db_session, ingestion_run_id=run_id, resume_point=None, chunking_config=_default_chunking()
        )

        # Either the dup-check or pre-check skips the import
        total_skipped = report.skipped_duplicate_count + report.skipped_overlap_count
        assert total_skipped >= 1, f"Expected overlap skip; got {report}"

        # Only ONE version row for this chat_message (the live v1)
        versions = (await db_session.execute(
            select(MessageVersion).where(MessageVersion.chat_message_id == live_cm.id)
        )).scalars().all()
        assert len(versions) == 1
        assert versions[0].imported_final is False  # live v1, not import
    finally:
        tmp.unlink(missing_ok=True)


async def test_import_apply_offrecord_creates_chat_message_and_mark(db_session) -> None:
    """Apply fixture with #offrecord text → chat_messages row, OffrecordMark, redacted v1.

    Verifies Risk H2 fix: import #offrecord now delegates to helper (same as live path).
    """
    import tempfile

    from bot.db.models import OffrecordMark, MessageVersion
    from bot.services.import_apply import run_apply
    from sqlalchemy import select

    chat_id = -1_009_000_000_002
    msg_id = 50_002

    export_str = _make_offrecord_export(chat_id, msg_id, "secret #offrecord info")
    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        tmp.write_text(export_str)
        run_id = await _create_apply_run(
            db_session, source_path=str(tmp), chat_id=chat_id, source_hash="hash_offrecord_164"
        )
        report = await run_apply(
            db_session, ingestion_run_id=run_id, resume_point=None, chunking_config=_default_chunking()
        )

        # Governance counter is bumped (kept for operator dashboards)
        assert report.skipped_governance_count == 1
        # applied_count is NOT bumped for offrecord
        assert report.applied_count == 0

        # chat_messages row MUST now exist with memory_policy='offrecord'
        cm_check = await db_session.execute(
            sa_text(
                "SELECT memory_policy, is_redacted FROM chat_messages "
                "WHERE chat_id = :cid AND message_id = :mid"
            ),
            {"cid": chat_id, "mid": msg_id},
        )
        row = cm_check.fetchone()
        assert row is not None, "H2: chat_messages row must exist for imported #offrecord"
        assert row[0] == "offrecord"
        assert row[1] is True

        # OffrecordMark must exist
        from bot.db.models import ChatMessage
        from sqlalchemy import select as sa_select
        cm_obj = (await db_session.execute(
            sa_select(ChatMessage).where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == msg_id,
            )
        )).scalar_one()

        mark_check = await db_session.execute(
            sa_text("SELECT COUNT(*) FROM offrecord_marks WHERE chat_message_id = :id"),
            {"id": cm_obj.id},
        )
        assert int(mark_check.scalar_one()) >= 1, "H2: OffrecordMark must exist for imported offrecord"

        # MessageVersion row must exist with is_redacted=True
        mv_check = await db_session.execute(
            sa_text(
                "SELECT is_redacted, text FROM message_versions WHERE chat_message_id = :id"
            ),
            {"id": cm_obj.id},
        )
        mv_row = mv_check.fetchone()
        assert mv_row is not None, "H2: MessageVersion must be created for imported offrecord"
        assert mv_row[0] is True, "H2: MessageVersion must be redacted"
        assert mv_row[1] is None, "H2: MessageVersion.text must be NULL for offrecord"
    finally:
        tmp.unlink(missing_ok=True)


async def test_import_apply_idempotent_rerun(db_session) -> None:
    """Regression: re-running same export on same DB produces zero net new rows."""
    from bot.services.import_apply import run_apply

    chat_id = -1001999999999
    run1 = await _create_apply_run(
        db_session, source_path=str(SMALL_CHAT), chat_id=chat_id, source_hash="hash_idempotent_164_r1"
    )
    await run_apply(db_session, ingestion_run_id=run1, resume_point=None, chunking_config=_default_chunking())

    count_after_first = await _count_chat_messages(db_session, chat_id)

    run2 = await _create_apply_run(
        db_session, source_path=str(SMALL_CHAT), chat_id=chat_id, source_hash="hash_idempotent_164_r2"
    )
    report2 = await run_apply(db_session, ingestion_run_id=run2, resume_point=None, chunking_config=_default_chunking())

    count_after_second = await _count_chat_messages(db_session, chat_id)
    assert count_after_second == count_after_first, "Idempotency: second run must not add new chat_messages rows"
    assert report2.applied_count == 0
