from __future__ import annotations

from sqlalchemy import UniqueConstraint

from bot.db.models import Base


def test_tracking_metadata_has_one_claim_per_user_and_wave() -> None:
    table = Base.metadata.tables["intro_refresh_tracking"]
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_intro_refresh_tracking_user_cycle"
        and tuple(column.name for column in constraint.columns)
        == (
            "user_id",
            "cycle_started_at",
        )
        for constraint in table.constraints
    )
