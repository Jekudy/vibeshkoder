"""Allow messages without a user sender.

Revision ID: 093
Revises: 092
"""

from alembic import op
import sqlalchemy as sa

revision = "093"
down_revision = "092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("chat_messages", "user_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.execute("LOCK TABLE chat_messages IN ACCESS EXCLUSIVE MODE")
    count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM chat_messages WHERE user_id IS NULL")
    ).scalar_one()
    if count:
        raise RuntimeError("Cannot downgrade 093: chat_messages contains NULL user_id rows")
    op.alter_column("chat_messages", "user_id", existing_type=sa.BigInteger(), nullable=False)
