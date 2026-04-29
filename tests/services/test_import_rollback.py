"""T2-NEW-G / #104 — logical rollback per ingestion_run_id.

DB-backed tests use the postgres ``db_session`` fixture. The rollback service commits
internally by design; the fixture's outer transaction still rolls the test data back at
teardown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

pytestmark = pytest.mark.usefixtures("app_env")

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export"
SMALL_CHAT = FIXTURE_DIR / "small_chat.json"


async def _create_apply_run(
    db_session,
    *,
    source_path: str = str(SMALL_CHAT),
    chat_id: int = -1001999999999,
    source_hash: str,
) -> int:
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
    return int(run_id)


def _test_chunking():
    from bot.services.import_chunking import ChunkingConfig

    return ChunkingConfig(
        chunk_size=500,
        sleep_between_chunks_ms=0,
        use_advisory_lock=False,
    )


async def _apply_small_import(db_session, *, source_hash: str) -> int:
    from bot.services.import_apply import run_apply

    run_id = await _create_apply_run(db_session, source_hash=source_hash)
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_test_chunking(),
    )
    assert report.applied_count == 5
    assert report.error_count == 0
    return run_id


async def _count_rows(db_session) -> tuple[int, int, int]:
    result = await db_session.execute(
        sa_text(
            """
            SELECT
                (SELECT COUNT(*) FROM chat_messages),
                (SELECT COUNT(*) FROM telegram_updates),
                (SELECT COUNT(*) FROM message_versions)
            """
        )
    )
    return tuple(map(int, result.one()))


async def _owned_counts(db_session, run_id: int) -> tuple[int, int, int]:
    result = await db_session.execute(
        sa_text(
            """
            SELECT
                (
                    SELECT COUNT(*)
                      FROM chat_messages cm
                      JOIN telegram_updates tu ON tu.id = cm.raw_update_id
                     WHERE tu.ingestion_run_id = :rid
                       AND tu.update_id IS NULL
                ),
                (
                    SELECT COUNT(*)
                      FROM telegram_updates
                     WHERE ingestion_run_id = :rid
                       AND update_id IS NULL
                ),
                (
                    SELECT COUNT(*)
                      FROM message_versions mv
                      JOIN chat_messages cm ON cm.id = mv.chat_message_id
                      JOIN telegram_updates tu ON tu.id = cm.raw_update_id
                     WHERE tu.ingestion_run_id = :rid
                       AND tu.update_id IS NULL
                )
            """
        ),
        {"rid": run_id},
    )
    return tuple(map(int, result.one()))


async def _rollback_audit_rows(db_session, original_run_id: int) -> list[tuple[int, dict]]:
    result = await db_session.execute(
        sa_text(
            """
            SELECT id, stats_json
              FROM ingestion_runs
             WHERE run_type = 'rolled_back'
               AND stats_json::jsonb ->> 'original_run_id' = :rid
             ORDER BY id
            """
        ),
        {"rid": str(original_run_id)},
    )
    return [(int(row[0]), row[1]) for row in result.all()]


async def test_rollback_after_small_import(db_session) -> None:
    from bot.services.import_rollback import rollback_ingestion_run

    before_counts = await _count_rows(db_session)
    run_id = await _apply_small_import(db_session, source_hash="rollback_small_import")

    assert await _owned_counts(db_session, run_id) == (5, 5, 5)

    report = await rollback_ingestion_run(db_session, run_id)

    assert report.original_run_id == run_id
    assert report.chat_messages_deleted == 5
    assert report.telegram_updates_deleted == 5
    assert report.message_versions_cascade_deleted == 5
    assert report.idempotent_skip is False
    assert await _owned_counts(db_session, run_id) == (0, 0, 0)
    assert await _count_rows(db_session) == before_counts

    audit_rows = await _rollback_audit_rows(db_session, run_id)
    assert len(audit_rows) == 1
    assert audit_rows[0][0] == report.audit_run_id
    assert audit_rows[0][1]["original_run_id"] == run_id


async def test_rollback_idempotent(db_session) -> None:
    from bot.services.import_rollback import rollback_ingestion_run

    run_id = await _apply_small_import(db_session, source_hash="rollback_idempotent")

    first = await rollback_ingestion_run(db_session, run_id)
    second = await rollback_ingestion_run(db_session, run_id)

    assert first.idempotent_skip is False
    assert second.idempotent_skip is True
    assert second.chat_messages_deleted == first.chat_messages_deleted
    assert second.telegram_updates_deleted == first.telegram_updates_deleted
    assert second.message_versions_cascade_deleted == first.message_versions_cascade_deleted
    assert second.audit_run_id == first.audit_run_id
    assert len(await _rollback_audit_rows(db_session, run_id)) == 1


async def test_rollback_race_unique_index_fallback(db_session, monkeypatch) -> None:
    import bot.services.import_rollback as rollback_mod

    run_id = await _apply_small_import(
        db_session,
        source_hash="rollback_race_unique_index_fallback",
    )
    first = await rollback_mod.rollback_ingestion_run(db_session, run_id)
    counts_before_second = await _count_rows(db_session)

    original_find = rollback_mod._find_existing_rollback_audit
    call_count = 0

    async def _miss_precheck_once(connection, ingestion_run_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return await original_find(connection, ingestion_run_id)

    monkeypatch.setattr(
        rollback_mod,
        "_find_existing_rollback_audit",
        _miss_precheck_once,
    )

    second = await rollback_mod.rollback_ingestion_run(db_session, run_id)

    assert second.idempotent_skip is True
    assert second.audit_run_id == first.audit_run_id
    assert second.chat_messages_deleted == first.chat_messages_deleted
    assert second.telegram_updates_deleted == first.telegram_updates_deleted
    assert second.message_versions_cascade_deleted == first.message_versions_cascade_deleted
    assert await _count_rows(db_session) == counts_before_second
    assert await _owned_counts(db_session, run_id) == (0, 0, 0)
    assert len(await _rollback_audit_rows(db_session, run_id)) == 1

    original_run_exists = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM ingestion_runs WHERE id = :rid"),
        {"rid": run_id},
    )
    assert int(original_run_exists.scalar_one()) == 1


async def test_rollback_does_not_touch_live_rows(db_session) -> None:
    from bot.services.import_rollback import rollback_ingestion_run

    chat_id = -1001999999999
    await db_session.execute(
        sa_text(
            """
            INSERT INTO users (id, first_name, is_imported_only)
            VALUES (777, 'Live User', false)
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
                VALUES ('message', 777001, :cid, 7777, :rid)
                RETURNING id
                """
            ),
            {"cid": chat_id, "rid": live_run_id},
        )
    ).scalar_one()
    live_chat_message_id = (
        await db_session.execute(
            sa_text(
                """
                INSERT INTO chat_messages (
                    message_id, chat_id, user_id, text, date, raw_update_id, content_hash
                )
                VALUES (7777, :cid, 777, 'live row', now(), :raw_id, 'live-parent-hash')
                RETURNING id
                """
            ),
            {"cid": chat_id, "raw_id": live_raw_id},
        )
    ).scalar_one()
    live_version_id = (
        await db_session.execute(
            sa_text(
                """
                INSERT INTO message_versions (
                    chat_message_id, version_seq, text, content_hash, imported_final
                )
                VALUES (:cmid, 1, 'live row', 'live-version-hash', false)
                RETURNING id
                """
            ),
            {"cmid": live_chat_message_id},
        )
    ).scalar_one()
    await db_session.flush()

    run_id = await _apply_small_import(db_session, source_hash="rollback_live_protection")
    await rollback_ingestion_run(db_session, run_id)

    live_counts = await db_session.execute(
        sa_text(
            """
            SELECT
                (SELECT COUNT(*) FROM telegram_updates WHERE id = :raw_id AND update_id IS NOT NULL),
                (SELECT COUNT(*) FROM chat_messages WHERE id = :cmid),
                (SELECT COUNT(*) FROM message_versions WHERE id = :mvid)
            """
        ),
        {
            "raw_id": live_raw_id,
            "cmid": live_chat_message_id,
            "mvid": live_version_id,
        },
    )
    assert tuple(map(int, live_counts.one())) == (1, 1, 1)
    assert await _owned_counts(db_session, run_id) == (0, 0, 0)


async def test_rollback_does_not_touch_forget_events(db_session) -> None:
    from bot.db.repos.forget_event import ForgetEventRepo
    from bot.services.import_rollback import rollback_ingestion_run

    chat_id = -1001999999999
    message_id = 1001
    tombstone_key = f"message:{chat_id}:{message_id}"
    run_id = await _apply_small_import(
        db_session,
        source_hash="rollback_forget_events_non_interference",
    )

    event = await ForgetEventRepo.create(
        db_session,
        target_type="message",
        target_id=str(message_id),
        actor_user_id=None,
        authorized_by="system",
        tombstone_key=tombstone_key,
    )
    await db_session.flush()

    before_count = await db_session.execute(sa_text("SELECT COUNT(*) FROM forget_events"))

    await rollback_ingestion_run(db_session, run_id)

    after_count = await db_session.execute(sa_text("SELECT COUNT(*) FROM forget_events"))
    assert int(after_count.scalar_one()) == int(before_count.scalar_one())

    existing = await ForgetEventRepo.get_by_tombstone_key(db_session, tombstone_key)
    assert existing is not None
    assert existing.id == event.id


async def test_rollback_rejects_live_run(db_session) -> None:
    from bot.services.import_rollback import InvalidRollbackRunError, rollback_ingestion_run

    run_id = (
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

    with pytest.raises(InvalidRollbackRunError, match="accepts only import runs"):
        await rollback_ingestion_run(db_session, int(run_id))


async def test_rollback_rejects_unknown_run(db_session) -> None:
    from bot.services.import_rollback import IngestionRunNotFoundError, rollback_ingestion_run

    with pytest.raises(IngestionRunNotFoundError, match="ingestion_run not found"):
        await rollback_ingestion_run(db_session, 987654321)


async def test_rollback_audit_row_records_original(db_session) -> None:
    from bot.services.import_rollback import rollback_ingestion_run

    run_id = await _apply_small_import(db_session, source_hash="rollback_audit_original")
    report = await rollback_ingestion_run(db_session, run_id)

    audit_rows = await _rollback_audit_rows(db_session, run_id)
    assert audit_rows == [(report.audit_run_id, audit_rows[0][1])]
    stats = audit_rows[0][1]
    assert stats["original_run_id"] == run_id
    assert stats["chat_messages_deleted"] == 5
    assert stats["telegram_updates_deleted"] == 5
    assert stats["message_versions_cascade_deleted"] == 5
    assert "rolled_back_at" in stats


async def test_rollback_atomic_on_partial_failure(db_session, monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from bot.services.import_rollback import rollback_ingestion_run

    run_id = await _apply_small_import(db_session, source_hash="rollback_atomic_failure")
    assert await _owned_counts(db_session, run_id) == (5, 5, 5)

    original_execute = AsyncConnection.execute

    async def _fail_on_update_delete(self, statement, *args, **kwargs):
        if "DELETE FROM telegram_updates" in str(statement):
            raise RuntimeError("simulated telegram_updates delete failure")
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", _fail_on_update_delete)

    with pytest.raises(RuntimeError, match="simulated telegram_updates delete failure"):
        await rollback_ingestion_run(db_session, run_id)

    assert await _owned_counts(db_session, run_id) == (5, 5, 5)
    assert await _rollback_audit_rows(db_session, run_id) == []


async def test_rollback_recovers_after_partial_failure(db_session, monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from bot.services.import_rollback import rollback_ingestion_run

    run_id = await _apply_small_import(db_session, source_hash="rollback_recovery_after_failure")
    assert await _owned_counts(db_session, run_id) == (5, 5, 5)

    original_execute = AsyncConnection.execute

    async def _fail_on_update_delete(self, statement, *args, **kwargs):
        if "DELETE FROM telegram_updates" in str(statement):
            raise RuntimeError("simulated telegram_updates delete failure")
        return await original_execute(self, statement, *args, **kwargs)

    with monkeypatch.context() as patch_ctx:
        patch_ctx.setattr(AsyncConnection, "execute", _fail_on_update_delete)

        with pytest.raises(RuntimeError, match="simulated telegram_updates delete failure"):
            await rollback_ingestion_run(db_session, run_id)

    assert await _owned_counts(db_session, run_id) == (5, 5, 5)
    assert await _rollback_audit_rows(db_session, run_id) == []

    report = await rollback_ingestion_run(db_session, run_id)

    assert report.idempotent_skip is False
    assert report.chat_messages_deleted == 5
    assert report.telegram_updates_deleted == 5
    assert report.message_versions_cascade_deleted == 5
    assert await _owned_counts(db_session, run_id) == (0, 0, 0)
    audit_rows = await _rollback_audit_rows(db_session, run_id)
    assert len(audit_rows) == 1
    assert audit_rows[0][0] == report.audit_run_id


async def test_message_versions_cascade_count(db_session) -> None:
    from bot.services.import_rollback import rollback_ingestion_run

    run_id = await _apply_small_import(db_session, source_hash="rollback_cascade_count")

    report = await rollback_ingestion_run(db_session, run_id)

    assert report.message_versions_cascade_deleted == 5
    remaining = await db_session.execute(
        sa_text(
            """
            SELECT COUNT(*)
              FROM message_versions mv
              JOIN chat_messages cm ON cm.id = mv.chat_message_id
              JOIN telegram_updates tu ON tu.id = cm.raw_update_id
             WHERE tu.ingestion_run_id = :rid
            """
        ),
        {"rid": run_id},
    )
    assert int(remaining.scalar_one()) == 0
