"""Remove the retired digest editorial-review workflow.

Revision ID: 090
Revises: 089
"""

from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "090"
down_revision: Union[str, Sequence[str], None] = "089"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REVIEW_DIGEST_STATUSES = (
    "'awaiting_review','approved_for_publish','rejected_by_admin','rejected_by_reaper'"
)
_REVIEW_RUN_STATUSES = _REVIEW_DIGEST_STATUSES + ",'regenerated_by_admin'"
_DIGEST_STATUSES = (
    "'running','draft','posting','posted','failed','skipped',"
    "'cost_exceeded','skipped_no_destination','redacted','redacted_edit_failed'"
)
_RUN_STATUSES = "'running','finished','failed','skipped','cost_exceeded','skipped_no_destination'"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _scalar(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar_one())


def _require_review_backup_acknowledgement() -> None:
    acknowledgement = context.get_x_argument(as_dictionary=True).get("digest_review_backup_sha256")
    if not acknowledgement or _SHA256_RE.fullmatch(acknowledgement) is None:
        raise RuntimeError(
            "Migration 090 found legacy digest-review data. Create and verify a backup, then "
            "rerun with -x digest_review_backup_sha256=<64 lowercase hex SHA-256>."
        )


def upgrade() -> None:
    # Serialize the acknowledgement decision and destructive cleanup with
    # writers of review rows. This lock conflicts with INSERT/UPDATE while
    # remaining transaction-scoped through Alembic's migration transaction.
    op.execute("LOCK TABLE digests, digest_runs IN SHARE ROW EXCLUSIVE MODE")
    review_digest_count = _scalar(
        "SELECT count(*) FROM digests WHERE status IN (" + _REVIEW_DIGEST_STATUSES + ")"
    )
    review_run_count = _scalar(
        "SELECT count(*) FROM digest_runs WHERE status IN (" + _REVIEW_RUN_STATUSES + ")"
    )
    review_metadata_count = _scalar(
        "SELECT count(*) FROM digests WHERE awaiting_review_at IS NOT NULL "
        "OR published_by_admin_id IS NOT NULL OR approved_at IS NOT NULL OR review_notes IS NOT NULL"
    )
    posted_review_count = _scalar(
        "SELECT count(*) FROM digests WHERE status IN (" + _REVIEW_DIGEST_STATUSES + ") "
        "AND (posted_chat_id IS NOT NULL OR posted_message_id IS NOT NULL OR posted_at IS NOT NULL)"
    )
    if posted_review_count:
        raise RuntimeError(
            "Migration 090 refuses to delete legacy review rows with posting provenance; "
            "preserve those rows and their citations before retrying."
        )
    if review_digest_count or review_run_count or review_metadata_count:
        _require_review_backup_acknowledgement()

    # Only obsolete, unpublished review states are removed. Posted rows and their
    # citation JSON remain in place.
    op.execute("DELETE FROM digest_runs WHERE status IN (" + _REVIEW_RUN_STATUSES + ")")
    op.execute("DELETE FROM digests WHERE status IN (" + _REVIEW_DIGEST_STATUSES + ")")

    op.execute("DROP INDEX IF EXISTS ix_digests_status_awaiting_review")
    op.execute("ALTER TABLE digests DROP CONSTRAINT IF EXISTS ck_digests_approved_audit")
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_status")
    op.execute(
        "ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses"
    )
    op.execute("ALTER TABLE digest_runs DROP CONSTRAINT ck_digest_runs_status")
    op.execute("ALTER TABLE digests DROP COLUMN awaiting_review_at")
    op.execute("ALTER TABLE digests DROP COLUMN published_by_admin_id")
    op.execute("ALTER TABLE digests DROP COLUMN approved_at")
    op.execute("ALTER TABLE digests DROP COLUMN review_notes")

    op.execute(
        "ALTER TABLE digests ADD CONSTRAINT ck_digests_status "
        "CHECK (status IN (" + _DIGEST_STATUSES + "))"
    )
    op.execute(
        "ALTER TABLE digests ADD CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses "
        "CHECK (status NOT IN ('draft','posting','posted','redacted','redacted_edit_failed') "
        "OR body_markdown IS NOT NULL)"
    )
    op.execute(
        "ALTER TABLE digest_runs ADD CONSTRAINT ck_digest_runs_status "
        "CHECK (status IN (" + _RUN_STATUSES + "))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_status")
    op.execute(
        "ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses"
    )
    op.execute("ALTER TABLE digest_runs DROP CONSTRAINT ck_digest_runs_status")
    op.add_column("digests", sa.Column("published_by_admin_id", sa.BigInteger(), nullable=True))
    op.add_column("digests", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("digests", sa.Column("review_notes", sa.Text(), nullable=True))
    op.add_column(
        "digests", sa.Column("awaiting_review_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "ALTER TABLE digests ADD CONSTRAINT ck_digests_status "
        "CHECK (status IN (" + _DIGEST_STATUSES + "," + _REVIEW_DIGEST_STATUSES + "))"
    )
    op.execute(
        "ALTER TABLE digests ADD CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses "
        "CHECK (status NOT IN ('draft','posting','posted','redacted','redacted_edit_failed',"
        "'awaiting_review','approved_for_publish','rejected_by_admin','rejected_by_reaper') "
        "OR body_markdown IS NOT NULL)"
    )
    op.execute(
        "ALTER TABLE digest_runs ADD CONSTRAINT ck_digest_runs_status "
        "CHECK (status IN (" + _RUN_STATUSES + "," + _REVIEW_RUN_STATUSES + "))"
    )
    op.execute(
        "ALTER TABLE digests ADD CONSTRAINT ck_digests_approved_audit "
        "CHECK (status <> 'approved_for_publish' OR type <> 'weekly' "
        "OR (published_by_admin_id IS NOT NULL AND approved_at IS NOT NULL))"
    )
    op.execute(
        "CREATE INDEX ix_digests_status_awaiting_review ON digests (awaiting_review_at) "
        "WHERE status='awaiting_review'"
    )
