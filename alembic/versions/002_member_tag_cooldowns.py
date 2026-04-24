"""member tag cooldowns

Revision ID: 002
Revises: 001
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "member_tag_cooldowns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("last_tag", sa.String(length=16), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_member_tag_cooldowns_chat_user",
        "member_tag_cooldowns",
        ["chat_id", "user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_member_tag_cooldowns_chat_user", table_name="member_tag_cooldowns")
    op.drop_table("member_tag_cooldowns")
