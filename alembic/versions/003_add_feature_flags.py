"""add_feature_flags

T1-01: persistent rollout flags for memory surfaces. All memory.* flags default OFF;
the migration does NOT seed any flag rows. Operators enable flags explicitly via the
admin UI (later phase) or via SQL.

Note on NULL semantics: postgres treats NULLs as DISTINCT in unique constraints by default,
so a constraint over ``(flag_key, scope_type, scope_id)`` would let multiple global-scope
rows (both scope columns NULL) coexist for the same flag_key. We need the opposite
behavior — global scope must be unique per flag_key. Postgres 15+ supports
``NULLS NOT DISTINCT`` on unique constraints, which is what we use here. CI runs against
postgres:16 (see ``.github/workflows/ci.yml``); dev compose pins postgres:16
(``docker-compose.dev.yml``).

Revision ID: 003
Revises: 002
Create Date: 2026-04-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, Sequence[str], None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flag_key", sa.String(length=255), nullable=False),
        sa.Column("scope_type", sa.String(length=64), nullable=True),
        sa.Column("scope_id", sa.String(length=255), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique key with NULLS NOT DISTINCT so a single global-scope row per flag_key is
    # actually unique. Without NULLS NOT DISTINCT, two rows with (scope_type=NULL,
    # scope_id=NULL) would coexist and ON CONFLICT in FeatureFlagRepo.set_enabled would
    # never fire. SQLAlchemy 2.0.20+ exposes ``postgresql_nulls_not_distinct`` on
    # UniqueConstraint / unique Index. Postgres 15+ required (we run 16 everywhere).
    op.create_index(
        "uq_feature_flags_key_scope",
        "feature_flags",
        ["flag_key", "scope_type", "scope_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_feature_flags_enabled", "feature_flags", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_feature_flags_enabled", table_name="feature_flags")
    op.drop_index("uq_feature_flags_key_scope", table_name="feature_flags")
    op.drop_table("feature_flags")
