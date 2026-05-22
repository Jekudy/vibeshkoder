"""graph_provenance_triple_hash_bigint

10.5-S3: Sprint 3 switched compute_edge_key_hash() from hex string to signed
int64 for native Neo4j sum() support. The graph_provenance.triple_hash column
was still TEXT — asyncpg rejected INT inserts.

Converts triple_hash from TEXT to BIGINT. In dev/test the table is empty so no
data conversion is needed. On a production DB where legacy rows may contain hex
strings (from the pre-Sprint-3 hex path), the USING clause converts via
PostgreSQL bit manipulation: ('x' || triple_hash)::bit(64)::bigint.
Rows where triple_hash IS NULL are unaffected.

Revision ID: 068
Revises: 067
Create Date: 2026-05-22
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "068"
down_revision: Union[str, Sequence[str], None] = "067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "graph_provenance"
_COLUMN = "triple_hash"


def upgrade() -> None:
    # NULL rows pass through unchanged.
    # Non-NULL rows: if they contain hex strings (legacy pre-Sprint-3 path) the
    # USING clause parses them as 64-bit big-endian integers via bit(64) cast.
    # If the column is already empty (dev/test) the USING clause is a no-op.
    op.execute(
        f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} TYPE BIGINT "
        f"USING CASE WHEN {_COLUMN} IS NULL THEN NULL "
        f"     WHEN {_COLUMN} ~ '^-?[0-9]+$' THEN {_COLUMN}::bigint "
        f"     ELSE ('x' || lpad({_COLUMN}, 16, '0'))::bit(64)::bigint END"
    )


def downgrade() -> None:
    # Convert BIGINT back to TEXT by simply casting to string.
    op.execute(
        f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} TYPE TEXT "
        f"USING {_COLUMN}::text"
    )
