"""HTML adapter integration with the existing import apply path."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text as sa_text


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export_html"
EXCLUDED_RAW_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "td_export_html_excluded_raw"
CHAT_ID = -1001234567890
EXCLUDED_RAW_CHAT_ID = -1001234567891


async def _create_run(
    db_session,
    source_hash: str,
    *,
    source_path: Path = FIXTURE_DIR,
    chat_id: int = CHAT_ID,
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
            "source_name": str(source_path),
            "source_hash": source_hash,
            "cfg": json.dumps({"chat_id": chat_id}),
        },
    )
    await db_session.flush()
    return int(result.scalar_one())


def _chunking():
    from bot.services.import_chunking import ChunkingConfig

    return ChunkingConfig(
        chunk_size=500,
        sleep_between_chunks_ms=0,
        use_advisory_lock=False,
    )


async def test_html_apply_is_idempotent_and_persists_media_provenance(db_session) -> None:
    from bot.services.import_apply import run_apply

    first_run = await _create_run(db_session, "html_apply_first")
    first = await run_apply(
        db_session,
        ingestion_run_id=first_run,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )
    assert first.applied_count == 5
    assert first.skipped_service_count == 1
    assert first.error_count == 0

    raw_result = await db_session.execute(
        sa_text(
            """
            SELECT raw_json
            FROM telegram_updates
            WHERE ingestion_run_id = :run_id AND message_id = 102
            """
        ),
        {"run_id": first_run},
    )
    raw = raw_result.scalar_one()
    assert raw["source_path"] == "messages.html#message102"
    assert raw["media_refs"] == ["photos/photo_1.jpg", "photos/photo_1_thumb.jpg"]
    assert raw["media_metadata"]["description"] == "800x600"
    serialized = json.dumps(raw)
    assert "PHOTO_CAPTION_SENTINEL" in serialized
    assert "Author Dos" in serialized

    media = (
        await db_session.execute(
            sa_text(
                "SELECT mm.source_message_url, mm.description_status, mm.last_error_code "
                "FROM message_media mm JOIN chat_messages cm ON cm.id=mm.chat_message_id "
                "WHERE cm.chat_id=:chat_id AND cm.message_id=102"
            ),
            {"chat_id": CHAT_ID},
        )
    ).one()
    assert media.source_message_url == "https://t.me/c/1234567890/102"
    assert media.description_status == "missing_source"
    assert media.last_error_code == "historical_export_no_file"

    from bot.services.import_dry_run import parse_html_export_with_db

    dry_run = await parse_html_export_with_db(FIXTURE_DIR, db_session, CHAT_ID)
    assert dry_run.chat_id == CHAT_ID
    assert dry_run.db_duplicate_count == 5
    assert dry_run.db_broken_reply_count == 0
    assert dry_run.tombstone_skip_count == 0

    second_run = await _create_run(db_session, "html_apply_second")
    second = await run_apply(
        db_session,
        ingestion_run_id=second_run,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )
    assert second.applied_count == 0
    assert second.skipped_duplicate_count == 5
    assert second.skipped_service_count == 1
    assert second.error_count == 0

    count_result = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM chat_messages WHERE chat_id = :chat_id"),
        {"chat_id": CHAT_ID},
    )
    assert int(count_result.scalar_one()) == 5


async def test_html_reimport_repairs_missing_photo_media_without_duplicate_message(
    db_session,
) -> None:
    from bot.services.import_apply import run_apply

    first_run = await _create_run(db_session, "html_photo_repair_first")
    first = await run_apply(
        db_session,
        ingestion_run_id=first_run,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )
    assert first.applied_count == 5

    chat_message_id = int(
        (
            await db_session.execute(
                sa_text("SELECT id FROM chat_messages WHERE chat_id=:chat_id AND message_id=102"),
                {"chat_id": CHAT_ID},
            )
        ).scalar_one()
    )
    original_raw_update_id = int(
        (
            await db_session.execute(
                sa_text("SELECT raw_update_id FROM chat_messages WHERE id=:chat_message_id"),
                {"chat_message_id": chat_message_id},
            )
        ).scalar_one()
    )
    deleted = await db_session.execute(
        sa_text("DELETE FROM message_media WHERE chat_message_id=:chat_message_id"),
        {"chat_message_id": chat_message_id},
    )
    assert deleted.rowcount == 1
    deleted_raw = await db_session.execute(
        sa_text("DELETE FROM telegram_updates WHERE id=:raw_update_id"),
        {"raw_update_id": original_raw_update_id},
    )
    assert deleted_raw.rowcount == 1

    second_run = await _create_run(db_session, "html_photo_repair_second")
    second = await run_apply(
        db_session,
        ingestion_run_id=second_run,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )

    assert second.applied_count == 0
    assert second.skipped_duplicate_count == 5
    chat_message_count = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM chat_messages WHERE chat_id=:chat_id AND message_id=102"),
        {"chat_id": CHAT_ID},
    )
    assert int(chat_message_count.scalar_one()) == 1

    media = (
        await db_session.execute(
            sa_text(
                "SELECT chat_message_id, source_message_url, description_status, last_error_code "
                "FROM message_media WHERE chat_message_id=:chat_message_id"
            ),
            {"chat_message_id": chat_message_id},
        )
    ).one()
    assert media.chat_message_id == chat_message_id
    assert media.source_message_url == "https://t.me/c/1234567890/102"
    assert media.description_status == "missing_source"
    assert media.last_error_code == "historical_export_no_file"

    third_run = await _create_run(db_session, "html_photo_repair_third")
    third = await run_apply(
        db_session,
        ingestion_run_id=third_run,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )
    assert third.applied_count == 0
    assert third.skipped_duplicate_count == 5

    raw_state = (
        await db_session.execute(
            sa_text(
                "SELECT cm.raw_update_id, COUNT(tu.id) AS raw_count, "
                "MIN(tu.ingestion_run_id) AS raw_run_id "
                "FROM chat_messages cm "
                "LEFT JOIN telegram_updates tu "
                "ON tu.chat_id=cm.chat_id AND tu.message_id=cm.message_id "
                "AND tu.update_type='import_message' AND tu.update_id IS NULL "
                "WHERE cm.id=:chat_message_id "
                "GROUP BY cm.raw_update_id"
            ),
            {"chat_message_id": chat_message_id},
        )
    ).one()
    assert raw_state.raw_update_id is None
    assert int(raw_state.raw_count) == 1
    assert int(raw_state.raw_run_id) == second_run

    from bot.services.import_rollback import rollback_ingestion_run

    rollback = await rollback_ingestion_run(db_session, second_run)
    assert rollback.telegram_updates_deleted == 1
    assert rollback.chat_messages_deleted == 0
    preserved = (
        await db_session.execute(
            sa_text(
                "SELECT COUNT(*) FROM chat_messages cm "
                "JOIN message_media mm ON mm.chat_message_id=cm.id "
                "WHERE cm.id=:chat_message_id"
            ),
            {"chat_message_id": chat_message_id},
        )
    ).scalar_one()
    assert int(preserved) == 1


async def test_html_apply_excludes_exact_bot_author_but_keeps_raw_provenance(
    db_session,
) -> None:
    from bot.services.import_apply import run_apply

    run_id = await _create_run(db_session, "html_apply_excluded_bot")
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"author uno"}),
    )

    assert report.applied_count == 1
    assert report.skipped_excluded_author_count == 4
    assert report.skipped_service_count == 1
    assert report.error_count == 0
    assert report.excluded_author_names == ["author uno"]
    assert report.excluded_author_message_counts == {"author uno": 4}

    from bot.services.import_html_parser import _synthetic_from_id

    excluded_user_id = int(_synthetic_from_id("Author Uno").removeprefix("user"))
    excluded_user_count = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM users WHERE id = :id"),
        {"id": excluded_user_id},
    )
    assert int(excluded_user_count.scalar_one()) == 0

    stored_messages = await db_session.execute(
        sa_text(
            "SELECT message_id FROM chat_messages WHERE chat_id = :chat_id ORDER BY message_id"
        ),
        {"chat_id": CHAT_ID},
    )
    assert [row[0] for row in stored_messages.all()] == [102]

    version_count = await db_session.execute(
        sa_text(
            """
            SELECT COUNT(*)
            FROM message_versions mv
            JOIN chat_messages cm ON cm.id = mv.chat_message_id
            WHERE cm.chat_id = :chat_id
            """
        ),
        {"chat_id": CHAT_ID},
    )
    assert int(version_count.scalar_one()) == 1

    raw_rows = await db_session.execute(
        sa_text(
            """
            SELECT message_id, raw_json
            FROM telegram_updates
            WHERE ingestion_run_id = :run_id
            ORDER BY message_id
            """
        ),
        {"run_id": run_id},
    )
    raw_by_id = {int(message_id): raw for message_id, raw in raw_rows.all()}
    assert set(raw_by_id) == {100, 101, 102, 103, 104}
    for bot_message_id in (100, 101, 103, 104):
        assert raw_by_id[bot_message_id]["excluded_author"] is True
    assert "excluded_author" not in raw_by_id[102]

    from bot.cli import _save_apply_final_stats

    await _save_apply_final_stats(db_session, report)
    run_stats = (
        await db_session.execute(
            sa_text("SELECT stats_json FROM ingestion_runs WHERE id = :id"),
            {"id": run_id},
        )
    ).scalar_one()
    assert run_stats["skipped_excluded_author_count"] == 4
    assert run_stats["excluded_author_names"] == ["author uno"]
    assert run_stats["excluded_author_message_counts"] == {"author uno": 4}

    second_run_id = await _create_run(db_session, "html_apply_excluded_bot_second")
    second = await run_apply(
        db_session,
        ingestion_run_id=second_run_id,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"author uno"}),
    )
    assert second.applied_count == 0
    assert second.skipped_duplicate_count == 5
    assert second.skipped_excluded_author_count == 0
    assert second.skipped_service_count == 1

    second_raw_count = await db_session.execute(
        sa_text("SELECT COUNT(*) FROM telegram_updates WHERE ingestion_run_id = :run_id"),
        {"run_id": second_run_id},
    )
    assert int(second_raw_count.scalar_one()) == 0


async def test_all_authors_keep_full_raw_while_excluded_bot_is_raw_only(db_session) -> None:
    from bot.services.import_apply import (
        _build_excluded_author_raw_payload,
        _build_raw_payload,
        run_apply,
    )
    from bot.services.import_html_parser import _synthetic_from_id

    synthetic = {
        "id": 1,
        "type": "message",
        "from": "Shkoder",
        "text": "text",
        "caption": "caption",
        "text_entities": [{"type": "link", "text": "entity"}],
        "media_metadata": {"description": "meta"},
        "custom_nested": {"preserve": [1, 2, 3]},
    }
    full_raw = _build_excluded_author_raw_payload(
        synthetic,
        chat_id=EXCLUDED_RAW_CHAT_ID,
        msg_id=1,
    )
    assert full_raw["text"] == "text"
    assert full_raw["caption"] == "caption"
    assert full_raw["text_entities"] == [{"type": "link", "text": "entity"}]
    assert full_raw["media_metadata"] == {"description": "meta"}
    assert full_raw["custom_nested"] == {"preserve": [1, 2, 3]}
    assert full_raw["excluded_author"] is True
    assert "excluded_author" not in synthetic

    human_raw = _build_raw_payload(
        synthetic,
        chat_id=EXCLUDED_RAW_CHAT_ID,
        msg_id=1,
    )
    assert human_raw["text"] == "text"
    assert human_raw["caption"] == "caption"
    assert human_raw["text_entities"] == [{"type": "link", "text": "entity"}]
    assert human_raw["from"] == "Shkoder"
    assert human_raw["media_metadata"] == {"description": "meta"}

    run_id = await _create_run(
        db_session,
        "html_excluded_full_raw",
        source_path=EXCLUDED_RAW_FIXTURE_DIR,
        chat_id=EXCLUDED_RAW_CHAT_ID,
    )
    report = await run_apply(
        db_session,
        ingestion_run_id=run_id,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )

    assert report.skipped_excluded_author_count == 1
    assert report.applied_count == 1

    raw_rows = await db_session.execute(
        sa_text(
            """
            SELECT message_id, raw_json
            FROM telegram_updates
            WHERE ingestion_run_id = :run_id
            ORDER BY message_id
            """
        ),
        {"run_id": run_id},
    )
    raw_by_id = {int(message_id): raw for message_id, raw in raw_rows.all()}
    assert raw_by_id[700]["from"] == "Shkoder"
    assert raw_by_id[700]["text"] == ("SHKODER_CAPTION_SENTINEL SHKODER_ENTITY_SENTINEL")
    assert raw_by_id[700]["caption"] == raw_by_id[700]["text"]
    assert raw_by_id[700]["media_refs"] == [
        "photos/shkoder_full.jpg",
        "photos/shkoder_thumb.jpg",
    ]
    assert raw_by_id[700]["media_metadata"]["description"] == "1024x768"
    assert raw_by_id[700]["excluded_author"] is True
    assert raw_by_id[701]["text"] == "HUMAN_MINIMAL_RAW_SENTINEL"
    assert "caption" not in raw_by_id[701]
    assert raw_by_id[701]["from"] == "Human Member"

    shkoder_user_id = int(_synthetic_from_id("Shkoder").removeprefix("user"))
    normalized_counts = {}
    for table, predicate, params in (
        ("users", "id = :user_id", {"user_id": shkoder_user_id}),
        (
            "chat_messages",
            "chat_id = :chat_id AND message_id = 700",
            {"chat_id": EXCLUDED_RAW_CHAT_ID},
        ),
    ):
        count = await db_session.execute(
            # Identifiers come only from the hard-coded test matrix above;
            # runtime values remain bound parameters.
            sa_text(f"SELECT COUNT(*) FROM {table} WHERE {predicate}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            params,
        )
        normalized_counts[table] = int(count.scalar_one())
    assert normalized_counts == {"users": 0, "chat_messages": 0}

    version_count = await db_session.execute(
        sa_text(
            """
            SELECT COUNT(*)
            FROM message_versions mv
            JOIN chat_messages cm ON cm.id = mv.chat_message_id
            WHERE cm.chat_id = :chat_id AND cm.message_id = 700
            """
        ),
        {"chat_id": EXCLUDED_RAW_CHAT_ID},
    )
    assert int(version_count.scalar_one()) == 0

    derived_count = await db_session.execute(
        sa_text(
            """
            SELECT COUNT(*)
            FROM card_sources cs
            JOIN message_versions mv ON mv.id = cs.message_version_id
            JOIN chat_messages cm ON cm.id = mv.chat_message_id
            WHERE cm.chat_id = :chat_id AND cm.message_id = 700
            """
        ),
        {"chat_id": EXCLUDED_RAW_CHAT_ID},
    )
    assert int(derived_count.scalar_one()) == 0


async def test_excluded_bot_reimport_upgrades_legacy_raw_and_quarantines_normalized_row(
    db_session,
) -> None:
    """Changing the exact-author policy must also repair a prior human-like import.

    Early HTML imports could normalize Shkoder and retain only a metadata-shaped raw
    row.  Re-importing with the exact exclusion must preserve one full raw source row
    while making every normalized version ineligible for derived memory.
    """
    from bot.services.import_apply import run_apply

    legacy_run_id = await _create_run(
        db_session,
        "html_excluded_legacy_normalized",
        source_path=EXCLUDED_RAW_FIXTURE_DIR,
        chat_id=EXCLUDED_RAW_CHAT_ID,
    )
    legacy = await run_apply(
        db_session,
        ingestion_run_id=legacy_run_id,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"unrelated bot"}),
    )
    assert legacy.applied_count == 2

    legacy_raw = {
        "chat_id": EXCLUDED_RAW_CHAT_ID,
        "message_id": 700,
        "source_path": "messages.html#message700",
    }
    await db_session.execute(
        sa_text(
            """
            UPDATE telegram_updates
            SET raw_json = CAST(:raw_json AS JSON), raw_hash = 'legacy-hash'
            WHERE ingestion_run_id = :run_id AND message_id = 700
            """
        ),
        {"run_id": legacy_run_id, "raw_json": json.dumps(legacy_raw)},
    )
    await db_session.flush()

    repair_run_id = await _create_run(
        db_session,
        "html_excluded_legacy_repair",
        source_path=EXCLUDED_RAW_FIXTURE_DIR,
        chat_id=EXCLUDED_RAW_CHAT_ID,
    )
    repaired = await run_apply(
        db_session,
        ingestion_run_id=repair_run_id,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )

    assert repaired.skipped_excluded_author_count == 1
    assert repaired.skipped_duplicate_count == 1
    assert repaired.applied_count == 0

    raw_rows = (
        await db_session.execute(
            sa_text(
                """
                SELECT ingestion_run_id, raw_json, raw_hash, is_redacted,
                       redaction_reason
                FROM telegram_updates
                WHERE chat_id = :chat_id AND message_id = 700
                  AND update_type = 'import_message'
                ORDER BY id
                """
            ),
            {"chat_id": EXCLUDED_RAW_CHAT_ID},
        )
    ).all()
    assert len(raw_rows) == 1
    assert int(raw_rows[0].ingestion_run_id) == legacy_run_id
    repaired_raw = raw_rows[0].raw_json
    assert repaired_raw["from"] == "Shkoder"
    assert repaired_raw["text"] == ("SHKODER_CAPTION_SENTINEL SHKODER_ENTITY_SENTINEL")
    assert repaired_raw["media_refs"] == [
        "photos/shkoder_full.jpg",
        "photos/shkoder_thumb.jpg",
    ]
    assert repaired_raw["excluded_author"] is True
    assert raw_rows[0].raw_hash is None
    assert raw_rows[0].is_redacted is False
    assert raw_rows[0].redaction_reason is None

    normalized = (
        await db_session.execute(
            sa_text(
                """
                SELECT id, memory_policy, is_redacted, text, caption, raw_json,
                       current_version_id
                FROM chat_messages
                WHERE chat_id = :chat_id AND message_id = 700
                """
            ),
            {"chat_id": EXCLUDED_RAW_CHAT_ID},
        )
    ).one()
    assert normalized.memory_policy == "forgotten"
    assert normalized.is_redacted is True
    assert normalized.text is None
    assert normalized.caption is None
    assert normalized.raw_json is None
    assert normalized.current_version_id is not None

    versions = (
        await db_session.execute(
            sa_text(
                """
                SELECT id, is_redacted, text, caption, normalized_text, entities_json
                FROM message_versions
                WHERE chat_message_id = :chat_message_id
                ORDER BY version_seq
                """
            ),
            {"chat_message_id": int(normalized.id)},
        )
    ).all()
    assert versions
    assert any(int(row.id) == int(normalized.current_version_id) for row in versions)
    assert all(row.is_redacted is True for row in versions)
    assert all(row.text is None for row in versions)
    assert all(row.caption is None for row in versions)
    assert all(row.normalized_text is None for row in versions)
    assert all(row.entities_json is None for row in versions)

    # Once repaired, the same source is a pure duplicate: no extra raw row and no
    # second quarantine mutation.
    repeat_run_id = await _create_run(
        db_session,
        "html_excluded_legacy_repair_repeat",
        source_path=EXCLUDED_RAW_FIXTURE_DIR,
        chat_id=EXCLUDED_RAW_CHAT_ID,
    )
    repeat = await run_apply(
        db_session,
        ingestion_run_id=repeat_run_id,
        resume_point=None,
        chunking_config=_chunking(),
        excluded_author_names=frozenset({"shkoder"}),
    )
    assert repeat.skipped_excluded_author_count == 0
    assert repeat.skipped_duplicate_count == 2
