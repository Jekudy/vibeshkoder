"""Add version-owned questionnaire snapshots and intro-effect outbox.

Revision ID: 091
Revises: 090
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "091"
down_revision: Union[str, Sequence[str], None] = "090"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ACTIVE_STATUSES = "'pending','vouched','privacy_block'"
_TERMINAL_STATUSES = "'added','rejected'"
_APPLICATION_STATUSES = (
    "'filling','confirmed','delivery_failed','pending','vouched','added','rejected','privacy_block'"
)
_EFFECT_KINDS = (
    "'candidate_card','admission_intro','member_intro','refresh_intro','sheet_projection'"
)
_EFFECT_STATUSES = "'pending','processing','sent','unknown','failed','stale'"
_APPLICATION_FK_NAME = "questionnaire_answers_application_id_fkey"


def _scalar(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar_one())


def _escape_html(column: str) -> str:
    return (
        "replace(replace(replace(replace(replace("
        + column
        + ", '&', '&amp;'), '<', '&lt;'), '>', '&gt;'), '\"', '&quot;'), '''', '&#x27;')"
    )


def _assert_active_legacy_is_complete() -> None:
    duplicate_active_applications = _scalar(
        f"""
        SELECT count(*)
        FROM (
            SELECT user_id
            FROM applications
            WHERE status IN ({_ACTIVE_STATUSES})
            GROUP BY user_id
            HAVING count(*) > 1
        ) AS duplicate_active_legacy_applications
        """
    )
    if duplicate_active_applications:
        raise RuntimeError(
            "Migration 091 found duplicate active legacy applications for a user; "
            "repair them before retrying."
        )

    malformed = _scalar(
        """
        SELECT count(*)
        FROM (
            SELECT a.id
            FROM applications AS a
            LEFT JOIN questionnaire_answers AS qa
              ON qa.application_id = a.id AND qa.is_current = true
            WHERE a.status IN ('pending','vouched','privacy_block')
            GROUP BY a.id
            HAVING count(qa.id) <> 7
                OR count(DISTINCT qa.question_index) <> 7
                OR NOT coalesce(bool_and(qa.question_index BETWEEN 0 AND 6), false)
        ) AS malformed
        """
    )
    if malformed:
        raise RuntimeError(
            "Migration 091 found an incomplete or duplicate active legacy questionnaire; "
            "repair it before retrying."
        )


def upgrade() -> None:
    op.execute(
        "LOCK TABLE users, applications, questionnaire_answers, intros IN SHARE ROW EXCLUSIVE MODE"
    )

    # All columns start nullable so a failed preflight leaves revision 090 intact.
    op.add_column("applications", sa.Column("flow_kind", sa.String(length=16), nullable=True))
    op.add_column("applications", sa.Column("base_application_id", sa.Integer(), nullable=True))
    op.add_column(
        "applications",
        sa.Column("catalog_version", sa.String(length=32), nullable=True),
    )
    op.add_column("applications", sa.Column("confirmed_intro_html", sa.Text(), nullable=True))
    op.add_column(
        "questionnaire_answers", sa.Column("field_id", sa.String(length=32), nullable=True)
    )
    op.add_column("intros", sa.Column("application_id", sa.Integer(), nullable=True))

    _assert_active_legacy_is_complete()

    op.execute(
        "UPDATE questionnaire_answers SET field_id = NULL, is_current = false "
        "WHERE is_current = false"
    )

    escaped_answer = _escape_html("qa.answer_text")
    op.execute(
        f"""
        WITH snapshots AS (
            SELECT qa.application_id,
                   string_agg(
                       CASE qa.question_index
                           WHEN 0 THEN '👤 '
                           WHEN 1 THEN '📍 '
                           WHEN 2 THEN '🔗 Откуда узнал: '
                           WHEN 3 THEN '💡 Опыт: '
                           WHEN 4 THEN '🚀 Проекты: '
                           WHEN 5 THEN '🏋️ Самое сложное: '
                           WHEN 6 THEN '🎯 Цели: '
                       END || {escaped_answer},
                       E'\\n' ORDER BY qa.question_index
                   ) AS html
            FROM questionnaire_answers AS qa
            JOIN applications AS a ON a.id = qa.application_id
            WHERE a.status IN ({_ACTIVE_STATUSES}) AND qa.is_current = true
            GROUP BY qa.application_id
        )
        UPDATE applications AS a
        SET flow_kind = 'admission',
            catalog_version = 'legacy-v1',
            confirmed_intro_html = snapshots.html
        FROM snapshots
        WHERE a.id = snapshots.application_id
        """
    )
    op.execute(
        """
        UPDATE questionnaire_answers AS qa
        SET field_id = CASE qa.question_index
            WHEN 0 THEN 'name'
            WHEN 1 THEN 'location'
            WHEN 2 THEN 'referral'
            WHEN 3 THEN 'experience'
            WHEN 4 THEN 'projects'
            WHEN 5 THEN 'hardest'
            WHEN 6 THEN 'goals'
        END
        FROM applications AS a
        WHERE a.id = qa.application_id
          AND a.status IN ('pending','vouched','privacy_block')
          AND qa.is_current = true
        """
    )
    op.execute(
        """
        UPDATE applications AS a
        SET flow_kind = CASE WHEN u.is_member THEN 'refresh' ELSE 'admission' END,
            catalog_version = 'intro-v2'
        FROM users AS u
        WHERE a.user_id = u.id AND a.status = 'filling'
        """
    )
    op.execute(
        """
        UPDATE questionnaire_answers AS qa
        SET field_id = NULL, is_current = false
        FROM applications AS a
        WHERE a.id = qa.application_id AND a.status = 'filling'
        """
    )
    op.execute(
        f"""
        UPDATE applications
        SET flow_kind = NULL, catalog_version = 'legacy-v1'
        WHERE status IN ({_TERMINAL_STATUSES})
        """
    )
    op.execute(
        f"""
        UPDATE questionnaire_answers AS qa
        SET field_id = NULL, is_current = false
        FROM applications AS a
        WHERE a.id = qa.application_id AND a.status IN ({_TERMINAL_STATUSES})
        """
    )

    if _scalar("SELECT count(*) FROM questionnaire_answers WHERE application_id IS NULL"):
        raise RuntimeError(
            "Migration 091 found questionnaire answers without an application; repair them before retrying."
        )

    op.alter_column("applications", "catalog_version", nullable=False)
    op.alter_column("questionnaire_answers", "application_id", nullable=False)

    op.create_table(
        "intro_effect_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("effect_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"effect_kind IN ({_EFFECT_KINDS})", name="ck_intro_effect_outbox_effect_kind"
        ),
        sa.CheckConstraint(f"status IN ({_EFFECT_STATUSES})", name="ck_intro_effect_outbox_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_intro_effect_outbox_attempt_count"),
        sa.CheckConstraint(
            "message_id IS NULL OR chat_id IS NOT NULL",
            name="ck_intro_effect_outbox_message_requires_chat",
        ),
        sa.CheckConstraint(
            "status <> 'processing' OR (attempt_count > 0 AND attempt_started_at IS NOT NULL)",
            name="ck_intro_effect_outbox_processing_claim",
        ),
        sa.CheckConstraint(
            "status NOT IN ('processing','unknown') "
            "OR (attempt_count > 0 AND attempt_started_at IS NOT NULL)",
            name="ck_intro_effect_outbox_attempt_identity",
        ),
        sa.CheckConstraint(
            "effect_kind <> 'sheet_projection' OR status <> 'unknown'",
            name="ck_intro_effect_outbox_sheet_projection_unknown",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], name="fk_intro_effect_outbox_application_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id", "effect_kind", name="uq_intro_effect_outbox_application_kind"
        ),
    )
    op.create_table(
        "intro_effect_reconciliations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("effect_id", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("evidence_sha256", sa.CHAR(length=64), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("operator_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "action IN ('record-sent','retry-absent')",
            name="ck_intro_effect_reconciliations_action",
        ),
        sa.CheckConstraint(
            "attempt_count > 0", name="ck_intro_effect_reconciliations_attempt_count"
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500", name="ck_intro_effect_reconciliations_reason"
        ),
        sa.CheckConstraint(
            "((action = 'record-sent' AND chat_id IS NOT NULL AND message_id IS NOT NULL "
            "AND message_id > 0 AND evidence_sha256 IS NULL) "
            "OR (action = 'retry-absent' AND chat_id IS NULL AND message_id IS NULL "
            "AND evidence_sha256 ~ '^[0-9a-f]{64}$'))",
            name="ck_intro_effect_reconciliations_action_shape",
        ),
        sa.CheckConstraint(
            "action <> 'retry-absent' OR evidence_sha256 IS NOT NULL",
            name="ck_intro_effect_reconciliations_retry_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["effect_id"],
            ["intro_effect_outbox.id"],
            name="fk_intro_effect_reconciliations_effect_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_user_id"],
            ["users.id"],
            name="fk_intro_effect_reconciliations_operator_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "effect_id", "attempt_count", name="uq_intro_effect_reconciliations_effect_attempt"
        ),
    )

    # Constraints are deliberately last: all existing rows now satisfy the contract.
    op.create_unique_constraint("uq_applications_id_user_id", "applications", ["id", "user_id"])
    op.create_foreign_key(
        "fk_applications_base_owner",
        "applications",
        "applications",
        ["base_application_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_check_constraint(
        "ck_applications_flow_kind",
        "applications",
        "((flow_kind IS NOT NULL AND flow_kind IN ('admission','refresh')) "
        "OR (flow_kind IS NULL AND status IN ('added','rejected'))) "
        "AND (base_application_id IS NULL "
        "OR (flow_kind IS NOT NULL AND flow_kind = 'refresh'))",
    )
    op.create_check_constraint(
        "ck_applications_status", "applications", f"status IN ({_APPLICATION_STATUSES})"
    )
    op.create_check_constraint(
        "ck_applications_catalog_version",
        "applications",
        "catalog_version IN ('legacy-v1','intro-v2')",
    )
    op.create_check_constraint(
        "ck_applications_confirmed_snapshot",
        "applications",
        "status = 'filling' OR confirmed_intro_html IS NOT NULL "
        "OR (catalog_version = 'legacy-v1' AND flow_kind IS NULL "
        "AND status IN ('added','rejected'))",
    )
    op.create_index(
        "uq_applications_active_refresh",
        "applications",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("flow_kind = 'refresh' AND status IN ('filling','confirmed')"),
    )

    op.drop_constraint(_APPLICATION_FK_NAME, "questionnaire_answers", type_="foreignkey")
    op.create_foreign_key(
        "fk_questionnaire_answers_application_owner",
        "questionnaire_answers",
        "applications",
        ["application_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_questionnaire_answers_application_field",
        "questionnaire_answers",
        ["application_id", "field_id"],
    )
    op.create_check_constraint(
        "ck_questionnaire_answers_field_id_legacy",
        "questionnaire_answers",
        "field_id IS NOT NULL OR is_current = false",
    )

    op.create_foreign_key(
        "fk_intros_application_owner",
        "intros",
        "applications",
        ["application_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_index("ix_intro_effect_outbox_status_id", "intro_effect_outbox", ["status", "id"])
    op.create_index(
        "uq_intro_effect_outbox_telegram_identity",
        "intro_effect_outbox",
        ["chat_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )


def downgrade() -> None:
    guards = (
        (
            "SELECT count(*) FROM applications WHERE confirmed_intro_html IS NOT NULL",
            "confirmed snapshots",
        ),
        ("SELECT count(*) FROM intros WHERE application_id IS NOT NULL", "Intro pointers"),
        (
            "SELECT count(*) FROM questionnaire_answers WHERE field_id IS NOT NULL",
            "versioned answers",
        ),
        (
            "SELECT count(*) FROM questionnaire_answers AS qa "
            "JOIN applications AS a ON a.id = qa.application_id "
            "WHERE qa.field_id IS NULL AND qa.is_current = false "
            "AND a.status IN ('filling','added','rejected')",
            "quarantined legacy answers",
        ),
        ("SELECT count(*) FROM intro_effect_reconciliations", "intro effect reconciliations"),
        ("SELECT count(*) FROM intro_effect_outbox", "intro effects"),
        ("SELECT count(*) FROM applications WHERE status = 'confirmed'", "confirmed applications"),
        (
            "SELECT count(*) FROM applications "
            "WHERE flow_kind IS NOT NULL OR base_application_id IS NOT NULL OR catalog_version <> 'legacy-v1'",
            "new application contract data",
        ),
    )
    for sql, surface in guards:
        if _scalar(sql):
            raise RuntimeError(f"Migration 091 downgrade refuses to discard {surface}.")

    op.drop_index("uq_intro_effect_outbox_telegram_identity", table_name="intro_effect_outbox")
    op.drop_index("ix_intro_effect_outbox_status_id", table_name="intro_effect_outbox")
    op.drop_table("intro_effect_reconciliations")
    op.drop_table("intro_effect_outbox")
    op.drop_constraint("fk_intros_application_owner", "intros", type_="foreignkey")
    op.drop_constraint(
        "ck_questionnaire_answers_field_id_legacy", "questionnaire_answers", type_="check"
    )
    op.drop_constraint(
        "uq_questionnaire_answers_application_field", "questionnaire_answers", type_="unique"
    )
    op.drop_constraint(
        "fk_questionnaire_answers_application_owner", "questionnaire_answers", type_="foreignkey"
    )
    op.create_foreign_key(
        _APPLICATION_FK_NAME,
        "questionnaire_answers",
        "applications",
        ["application_id"],
        ["id"],
    )
    op.alter_column("questionnaire_answers", "application_id", nullable=True)
    op.drop_column("questionnaire_answers", "field_id")

    op.drop_index("uq_applications_active_refresh", table_name="applications")
    op.drop_constraint("ck_applications_confirmed_snapshot", "applications", type_="check")
    op.drop_constraint("ck_applications_catalog_version", "applications", type_="check")
    op.drop_constraint("ck_applications_status", "applications", type_="check")
    op.drop_constraint("ck_applications_flow_kind", "applications", type_="check")
    op.drop_constraint("fk_applications_base_owner", "applications", type_="foreignkey")
    op.drop_constraint("uq_applications_id_user_id", "applications", type_="unique")
    op.drop_column("intros", "application_id")
    op.drop_column("applications", "confirmed_intro_html")
    op.drop_column("applications", "catalog_version")
    op.drop_column("applications", "base_application_id")
    op.drop_column("applications", "flow_kind")
