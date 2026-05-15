"""extend_digests_review_states

T8-01 / Phase 8: extend the Phase 7 ``digests`` + ``digest_runs`` schema for
the weekly editorial digest review-gate state machine.

Upgrade (see ``PHASE8_PLAN.md §5.A`` for the full rationale):

1. Widen ``ck_digests_status`` from 10 → 14 values, adding ``awaiting_review``,
   ``approved_for_publish``, ``rejected_by_admin``, ``rejected_by_reaper``.
2. Widen ``ck_digest_runs_status`` from 6 → 11 audit values, adding
   ``awaiting_review``, ``approved_for_publish``, ``rejected_by_admin``,
   ``rejected_by_reaper``, ``regenerated_by_admin``.
3. Add 4 nullable columns supporting admin attribution + reviewer-bookkeeping
   on ``digests``: ``published_by_admin_id BIGINT``, ``approved_at TIMESTAMPTZ``,
   ``review_notes TEXT``, ``awaiting_review_at TIMESTAMPTZ``.
4. Widen ``ck_digests_body_markdown_not_null_for_visible_statuses`` to include
   ``awaiting_review``, ``approved_for_publish``, ``rejected_by_admin``,
   ``rejected_by_reaper`` (body required for visible / audit-trail states).
5. Add partial index ``ix_digests_status_awaiting_review`` keyed on
   ``awaiting_review_at`` WHERE ``status='awaiting_review'`` — drives the
   stale-review reaper scan.
6. Add ``ck_digests_approved_audit``: when ``type='weekly'`` AND
   ``status='approved_for_publish'``, both ``published_by_admin_id`` and
   ``approved_at`` MUST be NOT NULL. Daily digests are exempt (auto-publish
   leaves the audit columns NULL forever).

H1 — every CHECK addition uses the ``NOT VALID`` + ``VALIDATE`` pattern: drop
the existing constraint (brief AccessExclusiveLock, ≤ms), add the new one as
``NOT VALID`` (instant), then ``VALIDATE`` it (scans the table without blocking
writers). For the small ``digests`` / ``digest_runs`` tables this is paranoid
optimization, but it is the project-wide alembic convention.

Downgrade pre-flight: ``DO $$ ... $$`` block raises RAISE EXCEPTION if ANY row
exists with status in the Phase-8 review-only set OR in ``posting`` (H2 — both
daily AND weekly ``posting`` rows block, because dropping the audit columns
under an in-flight publish breaks the publisher's ``RETURNING`` clause /
columns the publisher reads). The error message points operators to the
stale-posting reaper (which sweeps every 5 minutes) and to the downgrade
runbook section in ``PHASE8_ROLLOUT.md`` (when present).

Rollback scope: only ``digests`` table CHECK constraints + new columns + the
partial index + the ``digest_runs.status`` CHECK. No other tables, no FK
changes, no data migration.

Revision ID: 038
Revises: 037
Create Date: 2026-05-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "038"
down_revision: Union[str, Sequence[str], None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Status constants for readability ─────────────────────────────────────────────
_DIGESTS_STATUS_PHASE7 = (
    "'running','draft','posting','posted','failed','skipped',"
    "'cost_exceeded','skipped_no_destination','redacted',"
    "'redacted_edit_failed'"
)
_DIGESTS_STATUS_PHASE8 = (
    _DIGESTS_STATUS_PHASE7 + "," + "'awaiting_review','approved_for_publish',"
    "'rejected_by_admin','rejected_by_reaper'"
)
_RUNS_STATUS_PHASE7 = (
    "'running','finished','failed','skipped','cost_exceeded','skipped_no_destination'"
)
_RUNS_STATUS_PHASE8 = (
    _RUNS_STATUS_PHASE7 + "," + "'awaiting_review','approved_for_publish',"
    "'rejected_by_admin','rejected_by_reaper','regenerated_by_admin'"
)


def upgrade() -> None:
    # ── Group 1: widen digests.status CHECK (10 → 14 values) ────────────────
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_status")
    op.execute(
        f"""
        ALTER TABLE digests ADD CONSTRAINT ck_digests_status CHECK (
            status IN ({_DIGESTS_STATUS_PHASE8})
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_status")

    # ── Group 2: widen digest_runs.status CHECK (6 → 11 audit values) ───────
    op.execute("ALTER TABLE digest_runs DROP CONSTRAINT ck_digest_runs_status")
    op.execute(
        f"""
        ALTER TABLE digest_runs ADD CONSTRAINT ck_digest_runs_status CHECK (
            status IN ({_RUNS_STATUS_PHASE8})
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digest_runs VALIDATE CONSTRAINT ck_digest_runs_status")

    # ── Group 3: new columns supporting the review workflow ─────────────────
    op.execute("ALTER TABLE digests ADD COLUMN published_by_admin_id BIGINT NULL")
    op.execute("ALTER TABLE digests ADD COLUMN approved_at TIMESTAMP WITH TIME ZONE NULL")
    op.execute("ALTER TABLE digests ADD COLUMN review_notes TEXT NULL")
    op.execute("ALTER TABLE digests ADD COLUMN awaiting_review_at TIMESTAMP WITH TIME ZONE NULL")

    # ── Group 4: widen body-NOT-NULL invariant to review-bearing statuses ───
    op.execute(
        "ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses"
    )
    op.execute(
        """
        ALTER TABLE digests ADD CONSTRAINT
        ck_digests_body_markdown_not_null_for_visible_statuses CHECK (
            status NOT IN (
                'draft','posting','posted','redacted','redacted_edit_failed',
                'awaiting_review','approved_for_publish','rejected_by_admin',
                'rejected_by_reaper'
            )
            OR body_markdown IS NOT NULL
        ) NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE digests VALIDATE CONSTRAINT "
        "ck_digests_body_markdown_not_null_for_visible_statuses"
    )

    # ── Group 5: partial index + approval-audit CHECK ───────────────────────
    op.execute(
        """
        CREATE INDEX ix_digests_status_awaiting_review
            ON digests (awaiting_review_at)
            WHERE status='awaiting_review'
        """
    )

    # Audit CHECK: when status='approved_for_publish' AND type='weekly',
    # both admin attribution columns MUST be NOT NULL. Daily digests are
    # exempt (auto-publish leaves audit cols NULL by design). Posting /
    # posted weekly rows must also have these set, but the constraint only
    # asserts the implication for the approved_for_publish snapshot — the
    # publisher copies these cols forward across the posting → posted edge.
    op.execute(
        """
        ALTER TABLE digests ADD CONSTRAINT ck_digests_approved_audit CHECK (
            status NOT IN ('approved_for_publish','posting','posted')
            OR type <> 'weekly'
            OR (
                published_by_admin_id IS NOT NULL
                AND approved_at IS NOT NULL
            )
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_approved_audit")


def downgrade() -> None:
    # ── Downgrade pre-flight: fail hard on Phase-8 state in either table ───
    #
    # M5: once Phase 8 has been exercised, downgrade is an operator decision
    # that requires explicit cleanup. The RAISE EXCEPTION text guides the
    # operator to the PHASE8_ROLLOUT.md "downgrade" runbook section.
    #
    # R2 HIGH-Cdx-2: the pre-flight MUST also block `posting` rows. `posting`
    # itself is a Phase 7 status (in the narrower restored CHECK), so it
    # survives the constraint swap — but a row stuck in `posting` represents
    # an in-transit publish for either daily OR weekly. A weekly publish in
    # `posting` was triggered by admin /digest_approve and is mid-
    # `bot.send_message`; downgrading underneath drops the audit columns the
    # publisher relies on, races the stale-posting reaper, and may surface as
    # a "publish silently lost" incident. Blocking ALL `posting` rows
    # (daily + weekly) is the safer default — neither flavor is acceptable to
    # drop schema cols underneath.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM digests WHERE status IN (
                    'awaiting_review','approved_for_publish',
                    'rejected_by_admin','rejected_by_reaper',
                    'posting'
                )
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: digests rows in Phase 8 review states '
                    '(awaiting_review/approved_for_publish/rejected_by_admin/'
                    'rejected_by_reaper) OR in-transit ''posting'' state exist '
                    '- wait for in-flight publishes to terminate '
                    '(stale_posting_reaper runs every 5 min) then manually '
                    'transition or DELETE Phase-8 review rows per '
                    'PHASE8_ROLLOUT.md "downgrade" runbook section before '
                    're-running downgrade';
            END IF;
            IF EXISTS (
                SELECT 1 FROM digest_runs WHERE status IN (
                    'awaiting_review','approved_for_publish',
                    'rejected_by_admin','rejected_by_reaper','regenerated_by_admin'
                )
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: digest_runs rows in Phase 8 audit '
                    'states (awaiting_review/approved_for_publish/'
                    'rejected_by_admin/rejected_by_reaper/regenerated_by_admin) '
                    'exist - DELETE the audit rows per PHASE8_ROLLOUT.md '
                    '"downgrade" runbook section before re-running downgrade';
            END IF;
        END
        $$
        """
    )

    # ── Drop the partial index + audit CHECK ────────────────────────────────
    op.execute("DROP INDEX ix_digests_status_awaiting_review")
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_approved_audit")

    # ── Restore Phase 7 body-NOT-NULL CHECK ────────────────────────────────
    op.execute(
        "ALTER TABLE digests DROP CONSTRAINT ck_digests_body_markdown_not_null_for_visible_statuses"
    )
    op.execute(
        """
        ALTER TABLE digests ADD CONSTRAINT
        ck_digests_body_markdown_not_null_for_visible_statuses CHECK (
            status NOT IN ('draft','posting','posted','redacted','redacted_edit_failed')
            OR body_markdown IS NOT NULL
        ) NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE digests VALIDATE CONSTRAINT "
        "ck_digests_body_markdown_not_null_for_visible_statuses"
    )

    # ── Drop the 4 review-gate columns ──────────────────────────────────────
    op.execute("ALTER TABLE digests DROP COLUMN awaiting_review_at")
    op.execute("ALTER TABLE digests DROP COLUMN review_notes")
    op.execute("ALTER TABLE digests DROP COLUMN approved_at")
    op.execute("ALTER TABLE digests DROP COLUMN published_by_admin_id")

    # ── Restore Phase 7 digest_runs.status CHECK ───────────────────────────
    op.execute("ALTER TABLE digest_runs DROP CONSTRAINT ck_digest_runs_status")
    op.execute(
        f"""
        ALTER TABLE digest_runs ADD CONSTRAINT ck_digest_runs_status CHECK (
            status IN ({_RUNS_STATUS_PHASE7})
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digest_runs VALIDATE CONSTRAINT ck_digest_runs_status")

    # ── Restore Phase 7 digests.status CHECK ───────────────────────────────
    op.execute("ALTER TABLE digests DROP CONSTRAINT ck_digests_status")
    op.execute(
        f"""
        ALTER TABLE digests ADD CONSTRAINT ck_digests_status CHECK (
            status IN ({_DIGESTS_STATUS_PHASE7})
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE digests VALIDATE CONSTRAINT ck_digests_status")
