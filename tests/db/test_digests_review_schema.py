import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine.url import make_url

from bot.db.models import Digest, DigestRun


PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _connect(database_url: str):
    url = make_url(database_url)
    return await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host or "127.0.0.1",
        port=url.port or 5432,
        database=url.database,
    )


def _alembic(database_url: str, *args: str, check: bool = True):
    env = os.environ | {"DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
        timeout=120,
    )


@pytest_asyncio.fixture()
async def migration_database_url() -> str:
    raw_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if raw_url is None:
        pytest.skip("migration database URL is not configured")
    base = make_url(raw_url)
    database_name = f"shkoder_digest_090_{uuid.uuid4().hex[:10]}"
    admin = await _connect(base.set(database="postgres").render_as_string(hide_password=False))
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
    except Exception as exc:
        pytest.skip(f"cannot create migration database: {exc}")
    finally:
        await admin.close()
    database_url = base.set(database=database_name).render_as_string(hide_password=False)
    try:
        yield database_url
    finally:
        admin = await _connect(base.set(database="postgres").render_as_string(hide_password=False))
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=$1 AND pid <> pg_backend_pid()",
                database_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        finally:
            await admin.close()


def test_digest_orm_has_no_editorial_review_surface() -> None:
    digest_columns = Digest.__table__.c
    run_status = next(
        constraint.sqltext
        for constraint in DigestRun.__table__.constraints
        if constraint.name == "ck_digest_runs_status"
    )

    assert {
        "awaiting_review_at",
        "published_by_admin_id",
        "approved_at",
        "review_notes",
    }.isdisjoint(digest_columns)
    assert "awaiting_review" not in str(run_status)


def test_migration_090_requires_explicit_backup_acknowledgement_for_legacy_data() -> None:
    migration = (PROJECT_ROOT / "alembic/versions/090_remove_digest_review_legacy.py").read_text(
        encoding="utf-8"
    )

    assert "digest_review_backup_sha256" in migration
    assert "[0-9a-f]{64}" in migration
    assert "posted_review_count" in migration
    assert "source_chat_id" not in migration


def test_090_locks_review_tables_before_acknowledgement_guards() -> None:
    """The destructive decision is serialized before any count can become stale."""
    migration = (PROJECT_ROOT / "alembic/versions/090_remove_digest_review_legacy.py").read_text(
        encoding="utf-8"
    )

    assert migration.index("LOCK TABLE digests, digest_runs IN SHARE ROW EXCLUSIVE MODE") < (
        migration.index("review_digest_count = _scalar")
    )


async def test_090_clean_upgrade_removes_review_columns(migration_database_url: str) -> None:
    _alembic(migration_database_url, "upgrade", "head")
    conn = await _connect(migration_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='digests' AND column_name = ANY($1::text[])",
            ["awaiting_review_at", "published_by_admin_id", "approved_at", "review_notes"],
        )
        assert columns == []
    finally:
        await conn.close()


async def test_090_blocks_legacy_data_without_acknowledgement(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status) "
            "VALUES ('weekly', now() - interval '7 days', now(), 'body', '[]', 'awaiting_review')"
        )
    finally:
        await conn.close()
    result = _alembic(migration_database_url, "upgrade", "090", check=False)
    assert result.returncode != 0
    assert "digest_review_backup_sha256" in (result.stdout + result.stderr)


async def test_090_acknowledgement_preserves_posted_rows_and_citations(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    citations = '[{"kind":"message_version","id":7,"position":0}]'
    try:
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status, "
            "posted_chat_id, posted_message_id, posted_at) "
            "VALUES ('weekly', now() - interval '14 days', now() - interval '7 days', 'body', $1, "
            "'posted', -1001, 5, now())",
            citations,
        )
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status) "
            "VALUES ('weekly', now() - interval '7 days', now(), 'body', '[]', 'awaiting_review')"
        )
    finally:
        await conn.close()
    _alembic(
        migration_database_url,
        "-x",
        "digest_review_backup_sha256=" + "a" * 64,
        "upgrade",
        "090",
    )
    conn = await _connect(migration_database_url)
    try:
        assert json.loads(
            await conn.fetchval("SELECT citations::text FROM digests WHERE status='posted'")
        ) == json.loads(citations)
        assert (
            await conn.fetchval("SELECT count(*) FROM digests WHERE status='awaiting_review'") == 0
        )
    finally:
        await conn.close()


async def test_090_blocks_review_row_with_posting_provenance(migration_database_url: str) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status, "
            "posted_chat_id, posted_message_id, posted_at) "
            "VALUES ('weekly', now() - interval '7 days', now(), 'body', '[]', "
            "'awaiting_review', -1001, 5, now())"
        )
    finally:
        await conn.close()
    result = _alembic(
        migration_database_url,
        "-x",
        "digest_review_backup_sha256=" + "a" * 64,
        "upgrade",
        "090",
        check=False,
    )
    assert result.returncode != 0
    assert "posting provenance" in (result.stdout + result.stderr)


async def test_090_downgrade_restores_079_relaxed_approval_audit(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "head")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status, "
            "posted_chat_id, posted_message_id, posted_at) "
            "VALUES ('weekly', now() - interval '7 days', now(), 'body', '[]', 'posted', -1001, 5, now())"
        )
    finally:
        await conn.close()

    _alembic(migration_database_url, "downgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        definition = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_digests_approved_audit'"
        )
        assert "approved_for_publish" in definition
        assert "posting" not in definition and "posted" not in definition
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status, "
            "posted_chat_id, posted_message_id, posted_at) "
            "VALUES ('weekly', now() - interval '14 days', now() - interval '7 days', 'body', '[]', "
            "'posted', -1002, 6, now())"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status) "
                "VALUES ('weekly', now() - interval '21 days', now() - interval '14 days', 'body', '[]', "
                "'approved_for_publish')"
            )
    finally:
        await conn.close()


async def test_alembic_head_is_090(migration_database_url: str) -> None:
    _alembic(migration_database_url, "upgrade", "head")
    conn = await _connect(migration_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
    finally:
        await conn.close()


async def test_080_message_media_and_ledger_call_types(migration_database_url: str) -> None:
    _alembic(migration_database_url, "upgrade", "080")
    conn = await _connect(migration_database_url)
    try:
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='message_media'"
        )
        assert {row["column_name"] for row in columns} >= {
            "chat_message_id",
            "media_kind",
            "description_status",
        }
        for call_type in ("wiki_compilation", "image_description"):
            await conn.execute(
                "INSERT INTO llm_usage_ledger (provider, model, call_type) VALUES ('test', 'test', $1)",
                call_type,
            )
    finally:
        await conn.close()


async def test_080_downgrade_fails_closed_when_message_media_exists(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "080")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO users (id, username, first_name) VALUES (980000000001, 'migration_080', 'Migration 080')"
        )
        chat_message_id = await conn.fetchval(
            "INSERT INTO chat_messages (message_id, chat_id, user_id, text, date) "
            "VALUES (980001, -100980001, 980000000001, 'photo provenance', now()) RETURNING id"
        )
        await conn.execute(
            "INSERT INTO message_media (chat_message_id, media_kind, source_message_url, description_status) "
            "VALUES ($1, 'photo', 'https://t.me/c/980001/980001', 'missing_source')",
            chat_message_id,
        )
    finally:
        await conn.close()

    result = _alembic(migration_database_url, "downgrade", "079", check=False)
    assert result.returncode != 0
    assert (
        "Cannot downgrade 080: message_media contains rollout data" in result.stdout + result.stderr
    )


@pytest.mark.parametrize("rollout_data", ["qa_source", "media_retry"])
async def test_081_downgrade_fails_closed_when_reliability_data_exists(
    migration_database_url: str,
    rollout_data: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "081")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO users (id, username, first_name) VALUES (981000000001, 'migration_081', 'Migration 081')"
        )
        chat_message_id = await conn.fetchval(
            "INSERT INTO chat_messages (message_id, chat_id, user_id, text, date) "
            "VALUES (981001, -100981001, 981000000001, 'reliability', now()) RETURNING id"
        )
        if rollout_data == "qa_source":
            await conn.execute(
                "INSERT INTO qa_traces (user_tg_id, chat_id, source_chat_message_id) "
                "VALUES (981000000001, -100981001, $1)",
                chat_message_id,
            )
            expected_error = "Cannot downgrade 081: qa_traces contains delivery idempotency data"
            preserved_query = (
                "SELECT count(*) FROM qa_traces WHERE source_chat_message_id IS NOT NULL"
            )
        else:
            await conn.execute(
                "INSERT INTO message_media (chat_message_id, media_kind, source_message_url, "
                "description_status, description_attempts, next_attempt_at, last_error_code) "
                "VALUES ($1, 'photo', 'https://t.me/c/981001/981001', 'pending', 2, now(), "
                "'rate_limited')",
                chat_message_id,
            )
            expected_error = "Cannot downgrade 081: message_media contains retry metadata"
            preserved_query = "SELECT count(*) FROM message_media WHERE description_attempts = 2"
    finally:
        await conn.close()

    result = _alembic(migration_database_url, "downgrade", "080", check=False)
    assert result.returncode != 0
    assert expected_error in result.stdout + result.stderr
    conn = await _connect(migration_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "081"
        assert await conn.fetchval(preserved_query) == 1
    finally:
        await conn.close()


async def test_081_downgrade_succeeds_without_reliability_values(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "081")
    _alembic(migration_database_url, "downgrade", "080")
    conn = await _connect(migration_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "080"
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("seed_sql", "expected_error", "preserved_query"),
    [
        (
            "INSERT INTO knowledge_cards (id, topic_slug, title, body_markdown, card_status) "
            "VALUES (gen_random_uuid(), 'migration-083', 'Title', 'Body', 'draft')",
            "Cannot downgrade 083: knowledge_cards.topic_slug contains rollout data",
            "SELECT count(*) FROM knowledge_cards WHERE topic_slug = 'migration-083'",
        ),
        (
            "INSERT INTO extraction_runs (run_status, source_chat_id) "
            "VALUES ('running', -100983001)",
            "Cannot downgrade 083: extraction_runs.source_chat_id contains rollout data",
            "SELECT count(*) FROM extraction_runs WHERE source_chat_id = -100983001",
        ),
    ],
)
async def test_083_downgrade_fails_closed_when_rollout_columns_contain_data(
    migration_database_url: str,
    seed_sql: str,
    expected_error: str,
    preserved_query: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "083")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(seed_sql)
    finally:
        await conn.close()

    result = _alembic(migration_database_url, "downgrade", "082", check=False)
    assert result.returncode != 0
    assert expected_error in result.stdout + result.stderr
    conn = await _connect(migration_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "083"
        assert await conn.fetchval(preserved_query) == 1
    finally:
        await conn.close()


async def test_083_downgrade_succeeds_without_rollout_values(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "083")
    _alembic(migration_database_url, "downgrade", "082")
    conn = await _connect(migration_database_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "082"
    finally:
        await conn.close()


async def test_089_schema_pins_jsonb_delivery_and_numeric_constraints(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        jsonb_columns = await conn.fetch(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (table_name, column_name) IN "
            "(('semantic_index_runs', 'reason_counts'), ('semantic_retrieval_traces', "
            "'candidate_ranks'), ('semantic_retrieval_traces', 'result_source_ids')) "
            "ORDER BY table_name, column_name"
        )
        assert [
            (row["table_name"], row["column_name"], row["data_type"]) for row in jsonb_columns
        ] == [
            ("semantic_index_runs", "reason_counts", "jsonb"),
            ("semantic_retrieval_traces", "candidate_ranks", "jsonb"),
            ("semantic_retrieval_traces", "result_source_ids", "jsonb"),
        ]
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='semantic_qa_attempts' AND column_name='delivery_started_at'"
            )
            == 1
        )
        unit_columns = await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='semantic_retrieval_units' "
            "AND column_name IN ('embedding_status', 'chunk_text', 'embedding', 'indexed_at') "
            "ORDER BY column_name"
        )
        assert [
            (row["column_name"], row["data_type"], row["is_nullable"]) for row in unit_columns
        ] == [
            ("chunk_text", "text", "YES"),
            ("embedding", "USER-DEFINED", "YES"),
            ("embedding_status", "character varying", "NO"),
            ("indexed_at", "timestamp with time zone", "YES"),
        ]
        constraints = await conn.fetch(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
            "('ck_llm_usage_ledger_nonnegative_usage', 'ck_semantic_attempts_state', "
            "'ck_semantic_units_embedding_state', 'ck_semantic_units_embedding_status') "
            "ORDER BY conname"
        )
        assert [(row["conname"], row["convalidated"]) for row in constraints] == [
            ("ck_llm_usage_ledger_nonnegative_usage", True),
            ("ck_semantic_attempts_state", True),
            ("ck_semantic_units_embedding_state", True),
            ("ck_semantic_units_embedding_status", True),
        ]
    finally:
        await conn.close()


async def test_089_enforces_quota_state_matrix_and_nonnegative_ledger(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        valid_states = (
            ("denied", "quota_denied", None, None, None),
            ("reserved", None, 1, None, None),
            ("reserved", "answered", 2, "now()", None),
            ("consumed", "answered", 1, "now()", "now()"),
            ("consumed", "abstained", 2, "now()", "now()"),
            ("released", "technical_failure", 1, None, "now()"),
            ("released", "technical_failure", 2, "now()", "now()"),
        )
        for index, (status, outcome, slot, delivery_sql, finalized_sql) in enumerate(
            valid_states,
            start=1,
        ):
            await conn.execute(
                f"INSERT INTO semantic_qa_attempts (idempotency_key, user_tg_id, chat_id, "
                f"local_day, slot_number, status, outcome, delivery_started_at, finalized_at) "
                f"VALUES ($1, $2, -100404, CURRENT_DATE, $3, $4, $5, "
                f"{delivery_sql or 'NULL'}, {finalized_sql or 'NULL'})",
                f"schema-valid-{index}",
                989100000000 + index,
                slot,
                status,
                outcome,
            )

        invalid_states = (
            ("consumed", "technical_failure", 1, "now()", "now()"),
            ("consumed", "quota_denied", 1, "now()", "now()"),
            ("consumed", "answered", 1, None, "now()"),
            ("released", "answered", 1, "now()", "now()"),
            ("released", "abstained", 1, None, "now()"),
        )
        for index, (status, outcome, slot, delivery_sql, finalized_sql) in enumerate(
            invalid_states,
            start=1,
        ):
            transaction = conn.transaction()
            await transaction.start()
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    f"INSERT INTO semantic_qa_attempts (idempotency_key, user_tg_id, chat_id, "
                    f"local_day, slot_number, status, outcome, delivery_started_at, finalized_at) "
                    f"VALUES ($1, $2, -100404, CURRENT_DATE, $3, $4, $5, "
                    f"{delivery_sql or 'NULL'}, {finalized_sql or 'NULL'})",
                    f"schema-invalid-{index}",
                    989200000000 + index,
                    slot,
                    status,
                    outcome,
                )
            await transaction.rollback()

        transaction = conn.transaction()
        await transaction.start()
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO llm_usage_ledger (provider, model, prompt_hash, tokens_in, tokens_out, "
                "cost_usd, latency_ms, cache_hit, call_type) VALUES ('openai', "
                "'text-embedding-3-small', repeat('a', 64), -1, 0, 0, 0, FALSE, "
                "'semantic_embedding')"
            )
        await transaction.rollback()
    finally:
        await conn.close()


async def test_089_classifies_only_evidenced_authors_and_roundtrips(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "088")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO users (id, first_name, is_member, is_admin) VALUES "
            "(989000000101, 'Member', TRUE, FALSE), (989000000102, 'Admin', FALSE, TRUE), "
            "(989000000103, 'Imported human', FALSE, FALSE), "
            "(989000000104, 'Chat raw human', FALSE, FALSE), "
            "(989000000105, 'Live human', FALSE, FALSE), "
            "(989000000106, 'Edited bot', FALSE, FALSE), "
            "(989000000107, 'Unknown', FALSE, FALSE), "
            "(989000000108, 'Channel', FALSE, FALSE), (989000000109, 'True wins', TRUE, FALSE)"
        )
        ingestion_run_id = await conn.fetchval(
            "INSERT INTO ingestion_runs (run_type, status) VALUES ('import', 'completed') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO telegram_updates (update_id, update_type, raw_json, ingestion_run_id) VALUES "
            "(NULL, 'import_message', '{\"from_id\":\"user989000000103\"}'::json, $1), "
            "(NULL, 'import_message', '{\"from_id\":\"channel989000000108\"}'::json, $1), "
            '(989000000105, \'message\', \'{"message":{"from_user":{"id":989000000105,"is_bot":false}}}\'::json, NULL), '
            '(989000000106, \'edited_message\', \'{"edited_message":{"from_user":{"id":989000000106,"is_bot":true}}}\'::json, NULL), '
            '(989000000109, \'message\', \'{"message":{"from_user":{"id":989000000109,"is_bot":true}}}\'::json, NULL), '
            '(989000000110, \'edited_message\', \'{"edited_message":{"from_user":{"id":989000000109,"is_bot":false}}}\'::json, NULL), '
            '(989000000107, \'message\', \'{"message":{"from_user":{"id":989000000107,"is_bot":"invalid"}}}\'::json, NULL)',
            ingestion_run_id,
        )
        await conn.execute(
            "INSERT INTO chat_messages (message_id, chat_id, user_id, text, date, raw_json) "
            "VALUES (989000000104, -100989000000104, 989000000104, 'human evidence', now(), "
            '\'{"from_user":{"id":989000000104,"is_bot":false}}\'::json)'
        )
    finally:
        await conn.close()

    expected = {
        989000000101: None,
        989000000102: None,
        989000000103: None,
        989000000104: False,
        989000000105: False,
        989000000106: True,
        989000000107: None,
        989000000108: None,
        989000000109: True,
    }
    for _ in range(2):
        _alembic(migration_database_url, "upgrade", "089")
        conn = await _connect(migration_database_url)
        try:
            rows = await conn.fetch(
                "SELECT id, is_bot FROM users WHERE id BETWEEN 989000000101 AND 989000000109"
            )
        finally:
            await conn.close()
        assert {row["id"]: row["is_bot"] for row in rows} == expected
        _alembic(migration_database_url, "downgrade", "088")


def test_089_downgrade_guard_names_every_semantic_table() -> None:
    migration = (PROJECT_ROOT / "alembic/versions/089_semantic_qa.py").read_text(encoding="utf-8")
    for table_name in (
        "semantic_index_runs",
        "semantic_retrieval_units",
        "semantic_retrieval_unit_sources",
        "semantic_qa_attempts",
        "semantic_retrieval_traces",
    ):
        assert f"SELECT 1 FROM {table_name}" in migration


async def test_089_downgrade_fails_closed_with_index_audit_row(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO semantic_index_runs (run_type, embedding_provider, embedding_model, "
            "embedding_dimensions, status) VALUES ('backfill', 'openai', "
            "'text-embedding-3-small', 1536, 'running')"
        )
    finally:
        await conn.close()

    result = _alembic(migration_database_url, "downgrade", "088", check=False)
    assert result.returncode != 0
    assert "Cannot downgrade 089 with semantic Q&A audit data" in result.stdout + result.stderr


async def test_089_downgrade_fails_closed_with_semantic_embedding_audit(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "089")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO llm_usage_ledger (provider, model, prompt_hash, tokens_in, tokens_out, "
            "cost_usd, latency_ms, cache_hit, call_type) VALUES ('openai', "
            "'text-embedding-3-small', repeat('a', 64), 1, 0, 0.000001, 1, FALSE, "
            "'semantic_embedding')"
        )
    finally:
        await conn.close()

    result = _alembic(migration_database_url, "downgrade", "088", check=False)
    assert result.returncode != 0
    assert "Cannot downgrade 089 with semantic Q&A audit data" in result.stdout + result.stderr


async def test_079_approval_audit_allows_automatic_weekly_publish(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "079")
    conn = await _connect(migration_database_url)
    try:
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status, "
            "posting_started_at) VALUES ('weekly', now() - interval '14 days', now(), 'body', '[]', "
            "'posting', now())"
        )
        await conn.execute(
            "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status, "
            "posted_chat_id, posted_message_id, posted_at) VALUES ('weekly', now() - interval '21 days', "
            "now() - interval '14 days', 'body', '[]', 'posted', -1001, 7, now())"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO digests (type, window_start, window_end, body_markdown, citations, status) "
                "VALUES ('weekly', now() - interval '28 days', now(), 'body', '[]', 'approved_for_publish')"
            )
    finally:
        await conn.close()
