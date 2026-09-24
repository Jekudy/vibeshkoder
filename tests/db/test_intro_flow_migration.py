"""Schema and cutover acceptance for migration 091 versioned intro flow."""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine.url import URL, make_url

from bot.db import models
from tests.conftest import DEFAULT_LOCAL_POSTGRES_URL


pytestmark = pytest.mark.usefixtures("app_env")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _base_url() -> URL:
    return make_url(os.environ.get("TEST_DATABASE_URL") or DEFAULT_LOCAL_POSTGRES_URL)


def test_091_migration_database_url_uses_only_the_explicit_test_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://production.example/prod")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert _base_url().render_as_string(hide_password=False) == DEFAULT_LOCAL_POSTGRES_URL

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test.example/isolated")
    assert _base_url().render_as_string(hide_password=False) == "postgresql://test.example/isolated"


def test_091_explicit_test_database_connection_errors_are_not_skipped() -> None:
    fixture_source = inspect.getsource(migration_database_url.__wrapped__)
    assert 'if "TEST_DATABASE_URL" in os.environ:' in fixture_source
    assert "raise" in fixture_source


def test_091_migration_locks_all_classification_tables_before_preflight() -> None:
    source = (PROJECT_ROOT / "alembic/versions/091_versioned_intro_flow.py").read_text(
        encoding="utf-8"
    )
    lock = (
        "LOCK TABLE users, applications, questionnaire_answers, intros IN SHARE ROW EXCLUSIVE MODE"
    )
    assert lock in source
    upgrade = source.index("def upgrade()")
    assert source.index(lock, upgrade) < source.index(
        "_assert_active_legacy_is_complete()", upgrade
    )


def _kwargs(url: URL, *, database: str | None = None) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database or url.database,
    }


def _alembic(url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=check,
    )


@pytest_asyncio.fixture()
async def migration_database_url() -> AsyncIterator[str]:
    base = _base_url()
    name = f"shkoder_intro_091_{uuid.uuid4().hex[:10]}"
    try:
        admin = await asyncpg.connect(**_kwargs(base, database="postgres"))
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()
    except Exception as exc:  # pragma: no cover - local PostgreSQL guard
        if "TEST_DATABASE_URL" in os.environ:
            raise
        if os.environ.get("CI"):
            raise
        pytest.skip(f"cannot create migration database: {exc!s}")
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        admin = await asyncpg.connect(**_kwargs(base, database="postgres"))
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=$1 AND pid <> pg_backend_pid()",
                name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


def _checks(table_name: str) -> dict[str, str]:
    table = models.Base.metadata.tables[table_name]
    return {
        constraint.name: str(constraint.sqltext).lower()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _quoted_literals(sql: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", sql))


def _normalized_sql(sql: str) -> str:
    return "".join(sql.lower().split())


def _fk_pairs(table_name: str) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    table = models.Base.metadata.tables[table_name]
    return {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def _fk_ondelete(table_name: str) -> dict[str, str | None]:
    return {
        element.parent.name: element.ondelete
        for constraint in models.Base.metadata.tables[table_name].constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }


def test_091_metadata_declares_version_ownership_and_named_checks() -> None:
    applications = models.Base.metadata.tables["applications"]
    assert {"flow_kind", "base_application_id", "catalog_version", "confirmed_intro_html"} <= set(
        applications.c.keys()
    )
    assert applications.c.catalog_version.nullable is False
    assert applications.c.catalog_version.default is None
    assert applications.c.catalog_version.server_default is None
    assert any(
        isinstance(c, UniqueConstraint)
        and tuple(column.name for column in c.columns) == ("id", "user_id")
        for c in applications.constraints
    )
    assert (
        ("base_application_id", "user_id"),
        ("applications.id", "applications.user_id"),
    ) in _fk_pairs("applications")
    app_checks = _checks("applications")
    assert set(app_checks) >= {
        "ck_applications_flow_kind",
        "ck_applications_catalog_version",
        "ck_applications_status",
        "ck_applications_confirmed_snapshot",
    }
    assert _quoted_literals(app_checks["ck_applications_catalog_version"]) == {
        "legacy-v1",
        "intro-v2",
    }
    assert "".join(app_checks["ck_applications_flow_kind"].split()) == (
        "((flow_kindisnotnullandflow_kindin('admission','refresh'))"
        "or(flow_kindisnullandstatusin('added','rejected')))"
        "and(base_application_idisnullor(flow_kindisnotnullandflow_kind='refresh'))"
    )
    assert "confirmed" in app_checks["ck_applications_status"]
    assert "delivery_failed" in app_checks["ck_applications_status"]
    assert _normalized_sql(app_checks["ck_applications_confirmed_snapshot"]) == (
        "status='filling'orconfirmed_intro_htmlisnotnullor"
        "(catalog_version='legacy-v1'andflow_kindisnullandstatusin('added','rejected'))"
    )
    active = [
        index
        for index in applications.indexes
        if index.unique
        and tuple(column.name for column in index.columns) == ("user_id",)
        and index.dialect_options["postgresql"].get("where") is not None
    ]
    assert len(active) == 1
    assert _quoted_literals(str(active[0].dialect_options["postgresql"]["where"]).lower()) == {
        "refresh",
        "filling",
        "confirmed",
    }


def test_091_reconciliation_metadata_is_a_restrictive_attempt_audit() -> None:
    table = models.Base.metadata.tables["intro_effect_reconciliations"]
    assert set(table.c.keys()) == {
        "id",
        "effect_id",
        "attempt_count",
        "action",
        "reason",
        "evidence_sha256",
        "chat_id",
        "message_id",
        "operator_user_id",
        "created_at",
    }
    assert (("effect_id",), ("intro_effect_outbox.id",)) in _fk_pairs(
        "intro_effect_reconciliations"
    )
    assert (("operator_user_id",), ("users.id",)) in _fk_pairs("intro_effect_reconciliations")
    assert _fk_ondelete("intro_effect_reconciliations") == {
        "effect_id": "RESTRICT",
        "operator_user_id": "RESTRICT",
    }
    assert table.c.effect_id.nullable is False
    assert table.c.attempt_count.nullable is False
    assert table.c.action.nullable is False
    assert table.c.reason.nullable is False
    assert table.c.evidence_sha256.nullable is True
    assert table.c.chat_id.nullable is True
    assert table.c.message_id.nullable is True
    assert table.c.operator_user_id.nullable is False
    assert table.c.created_at.nullable is False
    assert isinstance(table.c.effect_id.type, Integer)
    assert isinstance(table.c.attempt_count.type, Integer)
    assert isinstance(table.c.action.type, String) and table.c.action.type.length == 32
    assert isinstance(table.c.reason.type, String) and table.c.reason.type.length == 500
    assert (
        isinstance(table.c.evidence_sha256.type, String)
        and table.c.evidence_sha256.type.length == 64
    )
    assert isinstance(table.c.chat_id.type, BigInteger)
    assert isinstance(table.c.message_id.type, BigInteger)
    assert isinstance(table.c.operator_user_id.type, BigInteger)
    assert (
        isinstance(table.c.created_at.type, DateTime) and table.c.created_at.type.timezone is True
    )
    assert table.c.created_at.server_default is not None
    assert any(
        isinstance(c, UniqueConstraint)
        and tuple(column.name for column in c.columns) == ("effect_id", "attempt_count")
        for c in table.constraints
    )
    checks = _checks("intro_effect_reconciliations")
    assert _quoted_literals(checks["ck_intro_effect_reconciliations_action"]) == {
        "record-sent",
        "retry-absent",
    }
    assert _normalized_sql(checks["ck_intro_effect_reconciliations_attempt_count"]) == (
        "attempt_count>0"
    )
    assert _normalized_sql(checks["ck_intro_effect_reconciliations_reason"]) == (
        "length(btrim(reason))between1and500"
    )
    assert _normalized_sql(checks["ck_intro_effect_reconciliations_action_shape"]) == (
        "((action='record-sent'andchat_idisnotnullandmessage_idisnotnull"
        "andmessage_id>0andevidence_sha256isnull)"
        "or(action='retry-absent'andchat_idisnullandmessage_idisnull"
        "andevidence_sha256~'^[0-9a-f]{64}$'))"
    )

    answers = models.Base.metadata.tables["questionnaire_answers"]
    assert answers.c.application_id.nullable is False
    assert answers.c.field_id.nullable is True
    assert (
        ("application_id", "user_id"),
        ("applications.id", "applications.user_id"),
    ) in _fk_pairs("questionnaire_answers")
    assert any(
        isinstance(c, UniqueConstraint)
        and tuple(column.name for column in c.columns) == ("application_id", "field_id")
        for c in answers.constraints
    )
    answer_checks = _checks("questionnaire_answers")
    assert "ck_questionnaire_answers_field_id_legacy" in answer_checks
    assert "field_id" in answer_checks["ck_questionnaire_answers_field_id_legacy"]
    assert "is_current" in answer_checks["ck_questionnaire_answers_field_id_legacy"]

    intros = models.Base.metadata.tables["intros"]
    assert intros.c.application_id.nullable is True
    assert (
        ("application_id", "user_id"),
        ("applications.id", "applications.user_id"),
    ) in _fk_pairs("intros")


def test_091_outbox_metadata_is_exact() -> None:
    table = models.Base.metadata.tables["intro_effect_outbox"]
    required = {
        "id": (Integer, False),
        "application_id": (Integer, False),
        "effect_kind": (String, False),
        "status": (String, False),
        "chat_id": (BigInteger, True),
        "message_id": (BigInteger, True),
        "attempt_count": (Integer, False),
        "attempt_started_at": (DateTime, True),
        "last_error": (Text, True),
        "created_at": (DateTime, False),
        "completed_at": (DateTime, True),
    }
    assert set(table.c.keys()) == set(required)
    for name, (type_class, nullable) in required.items():
        assert isinstance(table.c[name].type, type_class)
        assert table.c[name].nullable is nullable
    assert table.c.effect_kind.type.length == 32
    assert table.c.status.type.length == 16
    assert table.c.attempt_started_at.type.timezone is True
    assert table.c.created_at.type.timezone is True
    assert table.c.completed_at.type.timezone is True
    assert table.c.status.server_default is not None
    assert table.c.attempt_count.server_default is not None
    assert (("application_id",), ("applications.id",)) in _fk_pairs("intro_effect_outbox")
    assert any(
        isinstance(c, UniqueConstraint)
        and tuple(column.name for column in c.columns) == ("application_id", "effect_kind")
        for c in table.constraints
    )
    checks = _checks("intro_effect_outbox")
    assert set(checks) >= {
        "ck_intro_effect_outbox_effect_kind",
        "ck_intro_effect_outbox_status",
        "ck_intro_effect_outbox_attempt_count",
        "ck_intro_effect_outbox_message_requires_chat",
        "ck_intro_effect_outbox_processing_claim",
        "ck_intro_effect_outbox_attempt_identity",
        "ck_intro_effect_outbox_sheet_projection_unknown",
    }
    assert _normalized_sql(checks["ck_intro_effect_outbox_attempt_count"]) == "attempt_count>=0"
    assert _normalized_sql(checks["ck_intro_effect_outbox_message_requires_chat"]) == (
        "message_idisnullorchat_idisnotnull"
    )
    assert _normalized_sql(checks["ck_intro_effect_outbox_processing_claim"]) == (
        "status<>'processing'or(attempt_count>0andattempt_started_atisnotnull)"
    )
    assert _normalized_sql(checks["ck_intro_effect_outbox_attempt_identity"]) == (
        "statusnotin('processing','unknown')or(attempt_count>0andattempt_started_atisnotnull)"
    )
    assert _normalized_sql(checks["ck_intro_effect_outbox_sheet_projection_unknown"]) == (
        "effect_kind<>'sheet_projection'orstatus<>'unknown'"
    )
    assert _quoted_literals(checks["ck_intro_effect_outbox_effect_kind"]) == {
        "candidate_card",
        "admission_intro",
        "member_intro",
        "refresh_intro",
        "sheet_projection",
    }
    assert _quoted_literals(checks["ck_intro_effect_outbox_status"]) == {
        "pending",
        "processing",
        "sent",
        "unknown",
        "failed",
        "stale",
    }
    assert any(
        tuple(column.name for column in index.columns) == ("status", "id")
        for index in table.indexes
    )
    telegram_identity = [
        index
        for index in table.indexes
        if index.unique
        and tuple(column.name for column in index.columns) == ("chat_id", "message_id")
    ]
    assert len(telegram_identity) == 1
    assert (
        "message_id is not null"
        in str(telegram_identity[0].dialect_options["postgresql"]["where"]).lower()
    )


async def _seed_users(conn: asyncpg.Connection) -> None:
    await conn.executemany(
        "INSERT INTO users (id, username, first_name) VALUES ($1, $2, $3)",
        [
            (910001, "intro_a", "A"),
            (910002, "intro_b", "B"),
            (910003, "intro_member", "Member"),
            (910004, "intro_added", "Added"),
            (910005, "intro_rejected", "Rejected"),
        ],
    )


async def _insert_application(
    conn: asyncpg.Connection, user_id: int, status: str, **values: object
) -> int:
    columns = ["user_id", "status", *values]
    args = [user_id, status, *values.values()]
    placeholders = ", ".join(f"${number}" for number in range(1, len(args) + 1))
    return int(
        await conn.fetchval(
            f"INSERT INTO applications ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
            *args,
        )
    )


async def test_091_database_enforces_cross_user_ownership_refresh_and_outbox_contract(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(conn, 910005, "rejected", catalog_version="unsupported-v3")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn,
                910005,
                "rejected",
                flow_kind="admission",
                catalog_version="intro-v2",
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn,
                910005,
                "added",
                catalog_version="intro-v2",
            )
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_application(conn, 910004, "added", flow_kind="admission")
        base_id = await _insert_application(conn, 910001, "added", catalog_version="legacy-v1")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn,
                910001,
                "added",
                base_application_id=base_id,
                catalog_version="legacy-v1",
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(conn, 910004, "filling", catalog_version="intro-v2")
        await _insert_application(conn, 910004, "added", catalog_version="legacy-v1")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn, 910002, "confirmed", flow_kind="admission", catalog_version="intro-v2"
            )
        await _insert_application(
            conn,
            910002,
            "confirmed",
            flow_kind="admission",
            catalog_version="intro-v2",
            confirmed_intro_html="frozen admission",
        )
        refresh_base_id = await _insert_application(
            conn, 910003, "added", catalog_version="legacy-v1"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn,
                910003,
                "confirmed",
                flow_kind="refresh",
                base_application_id=refresh_base_id,
                catalog_version="intro-v2",
            )
        confirmed_refresh_id = await _insert_application(
            conn,
            910003,
            "confirmed",
            flow_kind="refresh",
            base_application_id=refresh_base_id,
            catalog_version="intro-v2",
            confirmed_intro_html="frozen refresh",
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE applications SET confirmed_intro_html=NULL, status='delivery_failed' "
                "WHERE id=$1",
                confirmed_refresh_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn, 910001, "added", flow_kind="wrong", catalog_version="intro-v2"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn, 910001, "wrong", flow_kind="admission", catalog_version="intro-v2"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_application(
                conn,
                910001,
                "added",
                flow_kind="admission",
                base_application_id=base_id,
                catalog_version="intro-v2",
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await _insert_application(
                conn,
                910002,
                "filling",
                flow_kind="refresh",
                base_application_id=base_id,
                catalog_version="intro-v2",
            )
        refresh_id = await _insert_application(
            conn,
            910001,
            "filling",
            flow_kind="refresh",
            base_application_id=base_id,
            catalog_version="intro-v2",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_application(
                conn,
                910001,
                "confirmed",
                flow_kind="refresh",
                base_application_id=base_id,
                catalog_version="intro-v2",
                confirmed_intro_html="frozen duplicate",
            )
        await _insert_application(
            conn, 910001, "filling", flow_kind="admission", catalog_version="intro-v2"
        )
        await _insert_application(
            conn,
            910001,
            "added",
            flow_kind="refresh",
            base_application_id=base_id,
            catalog_version="intro-v2",
            confirmed_intro_html="archived refresh",
        )
        await _insert_application(
            conn, 910002, "filling", flow_kind="refresh", catalog_version="intro-v2"
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO questionnaire_answers (user_id, application_id, question_index, question_text, answer_text, field_id) VALUES (910002, $1, 0, 'q', 'a', 'name')",
                refresh_id,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO intros (user_id, intro_text, vouched_by_name, application_id) VALUES (910002, 'i', 'v', $1)",
                refresh_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO questionnaire_answers (user_id, application_id, question_index, question_text, answer_text, field_id) VALUES (910001, $1, 0, 'q', 'a', NULL)",
                refresh_id,
            )
        await conn.execute(
            "INSERT INTO questionnaire_answers (user_id, application_id, question_index, question_text, answer_text, field_id, is_current) VALUES (910001, $1, 0, 'q', 'a', NULL, false)",
            refresh_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind) VALUES ($1, 'wrong')",
                refresh_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind, status) VALUES ($1, 'refresh_intro', 'wrong')",
                refresh_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind, attempt_count) "
                "VALUES ($1, 'admission_intro', -1)",
                refresh_id,
            )
        pending_effect = await conn.fetchrow(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind, attempt_count) "
            "VALUES ($1, 'member_intro', 0) RETURNING status, attempt_count",
            refresh_id,
        )
        assert pending_effect is not None
        assert tuple(pending_effect) == ("pending", 0)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind, status) "
                "VALUES ($1, 'candidate_card', 'processing')",
                refresh_id,
            )
        processing_effect = await conn.fetchrow(
            "INSERT INTO intro_effect_outbox "
            "(application_id, effect_kind, status, attempt_count, attempt_started_at) "
            "VALUES ($1, 'candidate_card', 'processing', 1, now()) "
            "RETURNING attempt_count, attempt_started_at",
            refresh_id,
        )
        assert processing_effect is not None
        assert processing_effect["attempt_count"] == 1
        assert processing_effect["attempt_started_at"].tzinfo is not None
        effect = await conn.fetchrow(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind) VALUES ($1, 'refresh_intro') "
            "RETURNING id, status, attempt_count, completed_at",
            refresh_id,
        )
        assert effect is not None
        assert effect["status"] == "pending"
        assert effect["attempt_count"] == 0
        assert effect["completed_at"] is None
        effect_id = effect["id"]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE intro_effect_outbox SET status='wrong' WHERE id=$1", effect_id
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE intro_effect_outbox SET effect_kind='wrong' WHERE id=$1", effect_id
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind, message_id) "
                "VALUES ($1, 'admission_intro', 2)",
                base_id,
            )
        await conn.execute(
            "UPDATE applications SET status='confirmed', confirmed_intro_html='frozen refresh' WHERE id=$1",
            refresh_id,
        )
        await conn.execute(
            "UPDATE applications SET status='delivery_failed' WHERE id=$1", refresh_id
        )
        await _insert_application(
            conn,
            910001,
            "filling",
            flow_kind="refresh",
            base_application_id=base_id,
            catalog_version="intro-v2",
        )
        await conn.execute(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind, chat_id) "
            "VALUES ($1, 'sheet_projection', 1)",
            base_id,
        )
        await conn.execute(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind, chat_id, message_id) "
            "VALUES ($1, 'candidate_card', 1, 2)",
            base_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind, chat_id, message_id) "
                "VALUES ($1, 'admission_intro', 1, 2)",
                base_id,
            )
        await conn.execute(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind) VALUES ($1, 'member_intro')",
            base_id,
        )
    finally:
        await conn.close()


async def test_091_database_enforces_unknown_attempt_identity_and_sheet_boundary(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(
            conn, 910001, "added", catalog_version="legacy-v1"
        )
        invalid_claims = (
            "(application_id, effect_kind, status) VALUES ($1, 'candidate_card', 'unknown')",
            "(application_id, effect_kind, status, attempt_count) "
            "VALUES ($1, 'candidate_card', 'unknown', 2)",
            "(application_id, effect_kind, status, attempt_started_at) "
            "VALUES ($1, 'candidate_card', 'unknown', now())",
        )
        for sql in invalid_claims:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"INSERT INTO intro_effect_outbox {sql}", application_id)

        accepted = await conn.fetchrow(
            "INSERT INTO intro_effect_outbox "
            "(application_id, effect_kind, status, attempt_count, attempt_started_at) "
            "VALUES ($1, 'candidate_card', 'unknown', 2, now()) "
            "RETURNING status, attempt_count, attempt_started_at",
            application_id,
        )
        assert accepted is not None
        assert accepted["status"] == "unknown"
        assert accepted["attempt_count"] == 2
        assert accepted["attempt_started_at"].tzinfo is not None

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_outbox "
                "(application_id, effect_kind, status, attempt_count, attempt_started_at) "
                "VALUES ($1, 'sheet_projection', 'unknown', 1, now())",
                application_id,
            )
    finally:
        await conn.close()


async def test_091_backfill_maps_only_current_answers_when_an_inactive_duplicate_exists(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(conn, 910001, "pending")
        await conn.executemany(
            "INSERT INTO questionnaire_answers "
            "(user_id, application_id, question_index, question_text, answer_text, is_current) "
            "VALUES (910001, $1, $2, 'q', $3, true)",
            [(application_id, index, f"current-{index}") for index in range(7)],
        )
        await conn.execute(
            "INSERT INTO questionnaire_answers "
            "(user_id, application_id, question_index, question_text, answer_text, is_current) "
            "VALUES (910001, $1, 0, 'q', 'stale duplicate', false)",
            application_id,
        )
    finally:
        await conn.close()

    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        rows = await conn.fetch(
            "SELECT answer_text, field_id, is_current FROM questionnaire_answers "
            "WHERE application_id=$1 ORDER BY id",
            application_id,
        )
        assert [(row["field_id"], row["is_current"]) for row in rows[:-1]] == [
            ("name", True),
            ("location", True),
            ("referral", True),
            ("experience", True),
            ("projects", True),
            ("hardest", True),
            ("goals", True),
        ]
        assert tuple(rows[-1]) == ("stale duplicate", None, False)
    finally:
        await conn.close()


async def test_091_rejects_active_legacy_cohort_with_seven_total_but_six_current_answers(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(conn, 910001, "vouched")
        await conn.executemany(
            "INSERT INTO questionnaire_answers "
            "(user_id, application_id, question_index, question_text, answer_text, is_current) "
            "VALUES (910001, $1, $2, 'q', 'a', $3)",
            [(application_id, index, index != 6) for index in range(7)],
        )
    finally:
        await conn.close()

    failed = _alembic(migration_database_url, "upgrade", "091", check=False)
    assert failed.returncode != 0
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
    finally:
        await conn.close()


async def test_091_rejects_two_complete_active_legacy_applications_for_one_user(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_ids = [
            await _insert_application(conn, 910001, status) for status in ("pending", "vouched")
        ]
        await conn.executemany(
            "INSERT INTO questionnaire_answers "
            "(user_id, application_id, question_index, question_text, answer_text, is_current) "
            "VALUES (910001, $1, $2, 'q', 'a', true)",
            [
                (application_id, question_index)
                for application_id in application_ids
                for question_index in range(7)
            ],
        )
    finally:
        await conn.close()

    failed = _alembic(migration_database_url, "upgrade", "091", check=False)
    assert failed.returncode != 0
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
    finally:
        await conn.close()


@pytest.mark.parametrize("status", ["pending", "vouched", "privacy_block"])
async def test_091_backfills_active_legacy_and_quarantines_filling_without_guessing_intro(
    migration_database_url: str, status: str
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        active_id = await _insert_application(conn, 910001, status)
        filling_id = await _insert_application(conn, 910002, "filling")
        member_filling_id = await _insert_application(conn, 910003, "filling")
        await conn.execute("UPDATE users SET is_member=true WHERE id=910003")
        await conn.executemany(
            "INSERT INTO questionnaire_answers (user_id, application_id, question_index, question_text, answer_text) VALUES ($1, $2, $3, 'q', $4)",
            [(910001, active_id, index, f"a{index}") for index in range(7)]
            + [(910002, filling_id, 0, "stale"), (910003, member_filling_id, 0, "member stale")],
        )
        await conn.execute(
            "INSERT INTO intros (user_id, intro_text, vouched_by_name) VALUES (910001, 'legacy', 'v')"
        )
        await conn.execute(
            "INSERT INTO intros (user_id, intro_text, vouched_by_name) "
            "VALUES (910003, 'member legacy', 'v')"
        )
    finally:
        await conn.close()
    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert (
            await conn.fetchval("SELECT catalog_version FROM applications WHERE id=$1", active_id)
            == "legacy-v1"
        )
        assert (
            await conn.fetchval("SELECT flow_kind FROM applications WHERE id=$1", active_id)
            == "admission"
        )
        assert await conn.fetchval(
            "SELECT confirmed_intro_html FROM applications WHERE id=$1", active_id
        ) == (
            "👤 a0\n📍 a1\n🔗 Откуда узнал: a2\n💡 Опыт: a3\n🚀 Проекты: a4\n"
            "🏋️ Самое сложное: a5\n🎯 Цели: a6"
        )
        mapped = await conn.fetch(
            "SELECT question_index, field_id FROM questionnaire_answers "
            "WHERE application_id=$1 ORDER BY question_index",
            active_id,
        )
        assert [(row["question_index"], row["field_id"]) for row in mapped] == [
            (0, "name"),
            (1, "location"),
            (2, "referral"),
            (3, "experience"),
            (4, "projects"),
            (5, "hardest"),
            (6, "goals"),
        ]
        assert (
            await conn.fetchval("SELECT catalog_version FROM applications WHERE id=$1", filling_id)
            == "intro-v2"
        )
        assert (
            await conn.fetchval("SELECT flow_kind FROM applications WHERE id=$1", filling_id)
            == "admission"
        )
        quarantined = await conn.fetchrow(
            "SELECT field_id, is_current FROM questionnaire_answers WHERE application_id=$1",
            filling_id,
        )
        assert quarantined is not None
        assert quarantined["field_id"] is None
        assert quarantined["is_current"] is False
        assert (
            await conn.fetchval("SELECT flow_kind FROM applications WHERE id=$1", member_filling_id)
            == "refresh"
        )
        assert (
            await conn.fetchval(
                "SELECT catalog_version FROM applications WHERE id=$1", member_filling_id
            )
            == "intro-v2"
        )
        member_quarantined = await conn.fetchrow(
            "SELECT field_id, is_current FROM questionnaire_answers WHERE application_id=$1",
            member_filling_id,
        )
        assert member_quarantined is not None
        assert member_quarantined["field_id"] is None
        assert member_quarantined["is_current"] is False
        assert await conn.fetchval("SELECT application_id FROM intros WHERE user_id=910001") is None
        assert await conn.fetchval("SELECT application_id FROM intros WHERE user_id=910003") is None
    finally:
        await conn.close()


@pytest.mark.parametrize("answers", [range(6), [0, 1, 2, 3, 4, 5, 6, 6]])
async def test_091_rejects_malformed_active_legacy_cohort_and_keeps_090(
    migration_database_url: str, answers: object
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(conn, 910001, "vouched")
        await conn.executemany(
            "INSERT INTO questionnaire_answers (user_id, application_id, question_index, question_text, answer_text) VALUES (910001, $1, $2, 'q', 'a')",
            [(application_id, index) for index in answers],  # type: ignore[arg-type]
        )
    finally:
        await conn.close()

    failed = _alembic(migration_database_url, "upgrade", "091", check=False)
    assert failed.returncode != 0
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
    finally:
        await conn.close()


async def test_091_rejects_duplicate_active_legacy_applications_and_keeps_090(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_ids = [
            await _insert_application(conn, 910001, "pending"),
            await _insert_application(conn, 910001, "vouched"),
        ]
        for application_id in application_ids:
            await conn.executemany(
                "INSERT INTO questionnaire_answers "
                "(user_id, application_id, question_index, question_text, answer_text) "
                "VALUES (910001, $1, $2, 'q', 'a')",
                [(application_id, index) for index in range(7)],
            )
    finally:
        await conn.close()

    failed = _alembic(migration_database_url, "upgrade", "091", check=False)
    assert failed.returncode != 0
    assert "duplicate active legacy applications" in failed.stderr
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
    finally:
        await conn.close()


async def test_091_downgrade_accepts_only_clean_legacy_terminal_rows(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(
            conn,
            910001,
            "added",
            flow_kind=None,
            base_application_id=None,
            catalog_version="legacy-v1",
        )
    finally:
        await conn.close()

    _alembic(migration_database_url, "downgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
        assert (
            await conn.fetchval("SELECT status FROM applications WHERE id=$1", application_id)
            == "added"
        )
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("surface", "error_surface"),
    [
        ("snapshot", "confirmed snapshots"),
        ("intro_pointer", "Intro pointers"),
        ("answer", "versioned answers"),
        ("outbox", "intro effects"),
        ("reconciliation", "intro effect reconciliations"),
    ],
)
async def test_091_downgrade_fails_closed_and_preserves_each_surface(
    migration_database_url: str, surface: str, error_surface: str
) -> None:
    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(
            conn,
            910001,
            "added",
            flow_kind=None,
            base_application_id=None,
            catalog_version="legacy-v1",
            confirmed_intro_html="frozen" if surface == "snapshot" else None,
        )
        if surface == "intro_pointer":
            await conn.execute(
                "INSERT INTO intros (user_id, intro_text, vouched_by_name, application_id) "
                "VALUES (910001, 'current', 'v', $1)",
                application_id,
            )
        elif surface == "answer":
            await conn.execute(
                "INSERT INTO questionnaire_answers "
                "(user_id, application_id, question_index, question_text, answer_text, field_id) "
                "VALUES (910001, $1, 0, 'q', 'a', 'name')",
                application_id,
            )
        elif surface in {"outbox", "reconciliation"}:
            effect_id = await conn.fetchval(
                "INSERT INTO intro_effect_outbox (application_id, effect_kind, status) "
                "VALUES ($1, 'admission_intro', 'sent') RETURNING id",
                application_id,
            )
            if surface == "reconciliation":
                await conn.execute(
                    "UPDATE intro_effect_outbox "
                    "SET status='unknown', attempt_count=1, attempt_started_at=now() "
                    "WHERE id=$1",
                    effect_id,
                )
                await conn.execute(
                    "INSERT INTO intro_effect_reconciliations "
                    "(effect_id, attempt_count, action, reason, chat_id, message_id, "
                    "operator_user_id) "
                    "VALUES ($1, 1, 'record-sent', 'operator verified delivery', -1001, 77, "
                    "910001)",
                    effect_id,
                )
    finally:
        await conn.close()

    failed = _alembic(migration_database_url, "downgrade", "090", check=False)
    assert failed.returncode != 0
    assert error_surface in failed.stderr
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "091"
        if surface == "snapshot":
            assert (
                await conn.fetchval(
                    "SELECT confirmed_intro_html FROM applications WHERE id=$1", application_id
                )
                == "frozen"
            )
        elif surface == "intro_pointer":
            assert (
                await conn.fetchval("SELECT application_id FROM intros WHERE user_id=910001")
                == application_id
            )
        elif surface == "answer":
            assert (
                await conn.fetchval(
                    "SELECT field_id FROM questionnaire_answers WHERE application_id=$1",
                    application_id,
                )
                == "name"
            )
        elif surface == "outbox":
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM intro_effect_outbox WHERE application_id=$1",
                    application_id,
                )
                == 1
            )
        else:
            audit = await conn.fetchrow(
                "SELECT action, attempt_count, reason "
                "FROM intro_effect_reconciliations WHERE effect_id=$1",
                effect_id,
            )
            assert audit is not None
            assert tuple(audit) == ("record-sent", 1, "operator verified delivery")
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM intro_effect_outbox WHERE id=$1", effect_id
                )
                == 1
            )
    finally:
        await conn.close()


@pytest.mark.parametrize("status,user_id", [("added", 910004), ("rejected", 910005)])
async def test_091_quarantines_terminal_legacy_answers_and_refuses_their_downgrade(
    migration_database_url: str, status: str, user_id: int
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(conn, user_id, status)
        await conn.executemany(
            "INSERT INTO questionnaire_answers "
            "(user_id, application_id, question_index, question_text, answer_text) "
            "VALUES ($1, $2, $3, 'q', $4)",
            [(user_id, application_id, index, f"terminal-{index}") for index in range(7)],
        )
        await conn.execute(
            "INSERT INTO intros (user_id, intro_text, vouched_by_name) VALUES ($1, 'legacy', 'v')",
            user_id,
        )
    finally:
        await conn.close()

    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        application = await conn.fetchrow(
            "SELECT flow_kind, catalog_version FROM applications WHERE id=$1", application_id
        )
        assert application is not None
        assert application["flow_kind"] is None
        assert application["catalog_version"] == "legacy-v1"
        rows = await conn.fetch(
            "SELECT field_id, is_current FROM questionnaire_answers "
            "WHERE application_id=$1 ORDER BY question_index",
            application_id,
        )
        assert [(row["field_id"], row["is_current"]) for row in rows] == [(None, False)] * 7
        assert (
            await conn.fetchval("SELECT application_id FROM intros WHERE user_id=$1", user_id)
            is None
        )
    finally:
        await conn.close()

    failed = _alembic(migration_database_url, "downgrade", "090", check=False)
    assert failed.returncode != 0
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "091"
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM questionnaire_answers "
                "WHERE application_id=$1 AND field_id IS NULL AND is_current=false",
                application_id,
            )
            == 7
        )
    finally:
        await conn.close()


async def _relevant_foreign_keys(conn: asyncpg.Connection) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        "SELECT conrelid::regclass::text AS table_name, pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint WHERE contype='f' "
        "AND conrelid::regclass::text IN ('applications', 'questionnaire_answers', 'intros') "
        "ORDER BY table_name, definition"
    )
    return [(row["table_name"], row["definition"]) for row in rows]


async def test_091_empty_roundtrip_restores_original_foreign_keys(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        original_foreign_keys = await _relevant_foreign_keys(conn)
        assert original_foreign_keys
    finally:
        await conn.close()

    _alembic(migration_database_url, "upgrade", "091")
    _alembic(migration_database_url, "downgrade", "090")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "090"
        assert await _relevant_foreign_keys(conn) == original_foreign_keys
    finally:
        await conn.close()

    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "091"
        assert await conn.fetchval("SELECT to_regclass('intro_effect_outbox')") == (
            "intro_effect_outbox"
        )
    finally:
        await conn.close()


async def test_091_reconciliation_rows_reject_invalid_audit_shapes(
    migration_database_url: str,
) -> None:
    _alembic(migration_database_url, "upgrade", "091")
    conn = await asyncpg.connect(**_kwargs(make_url(migration_database_url)))
    try:
        await _seed_users(conn)
        application_id = await _insert_application(
            conn, 910001, "added", catalog_version="legacy-v1"
        )
        record_effect_id = await conn.fetchval(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind, status, attempt_count, "
            "attempt_started_at) VALUES ($1, 'admission_intro', 'unknown', 3, now()) RETURNING id",
            application_id,
        )
        retry_effect_id = await conn.fetchval(
            "INSERT INTO intro_effect_outbox (application_id, effect_kind, status, attempt_count, "
            "attempt_started_at) VALUES ($1, 'member_intro', 'unknown', 4, now()) RETURNING id",
            application_id,
        )
        evidence = "a" * 64
        await conn.execute(
            "INSERT INTO intro_effect_reconciliations "
            "(effect_id, attempt_count, action, reason, evidence_sha256, chat_id, message_id, "
            "operator_user_id) "
            "VALUES ($1, 3, 'record-sent', 'found in Telegram', NULL, -1001, 31, 910001)",
            record_effect_id,
        )
        await conn.execute(
            "INSERT INTO intro_effect_reconciliations "
            "(effect_id, attempt_count, action, reason, evidence_sha256, chat_id, message_id, "
            "operator_user_id) "
            "VALUES ($1, 4, 'retry-absent', 'checked Telegram', $2, NULL, NULL, 910001)",
            retry_effect_id,
            evidence,
        )
        rows = await conn.fetch(
            "SELECT effect_id, attempt_count, action, reason, evidence_sha256, chat_id, "
            "message_id, operator_user_id FROM intro_effect_reconciliations ORDER BY id"
        )
        assert [tuple(row) for row in rows] == [
            (
                record_effect_id,
                3,
                "record-sent",
                "found in Telegram",
                None,
                -1001,
                31,
                910001,
            ),
            (
                retry_effect_id,
                4,
                "retry-absent",
                "checked Telegram",
                evidence,
                None,
                None,
                910001,
            ),
        ]

        fk_delete_rules = await conn.fetch(
            "SELECT rc.delete_rule "
            "FROM information_schema.referential_constraints AS rc "
            "JOIN information_schema.table_constraints AS tc "
            "ON tc.constraint_catalog=rc.constraint_catalog "
            "AND tc.constraint_schema=rc.constraint_schema "
            "AND tc.constraint_name=rc.constraint_name "
            "WHERE tc.table_schema=current_schema() "
            "AND tc.table_name='intro_effect_reconciliations' "
            "ORDER BY tc.constraint_name"
        )
        assert [row["delete_rule"] for row in fk_delete_rules] == ["RESTRICT", "RESTRICT"]

        invalid_checks = (
            (
                "VALUES ($1, 5, 'record-sent', '  ', NULL, -1001, 35, 910001)",
                (record_effect_id,),
            ),
            (
                "VALUES ($1, 6, 'record-sent', 'reason', $2, -1001, 36, 910001)",
                (record_effect_id, evidence),
            ),
            (
                "VALUES ($1, 5, 'retry-absent', 'reason', NULL, NULL, NULL, 910001)",
                (retry_effect_id,),
            ),
            (
                "VALUES ($1, 6, 'retry-absent', 'reason', 'bad', NULL, NULL, 910001)",
                (retry_effect_id,),
            ),
        )
        for values_sql, args in invalid_checks:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO intro_effect_reconciliations "
                    "(effect_id, attempt_count, action, reason, evidence_sha256, chat_id, "
                    f"message_id, operator_user_id) {values_sql}",
                    *args,
                )

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_reconciliations "
                "(effect_id, attempt_count, action, reason, chat_id, message_id, "
                "operator_user_id) "
                "VALUES ($1, 7, 'record-sent', 'reason', -1001, 37, 999999)",
                record_effect_id,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_reconciliations "
                "(effect_id, attempt_count, action, reason, chat_id, message_id, "
                "operator_user_id) "
                "VALUES (999999, 1, 'record-sent', 'reason', -1001, 38, 910001)"
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO intro_effect_reconciliations "
                "(effect_id, attempt_count, action, reason, chat_id, message_id, "
                "operator_user_id) "
                "VALUES ($1, 3, 'record-sent', 'duplicate decision', -1001, 31, 910001)",
                record_effect_id,
            )
        assert await conn.fetchval("SELECT count(*) FROM intro_effect_reconciliations") == 2
    finally:
        await conn.close()
