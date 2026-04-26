"""T1-01 acceptance tests — feature_flags table + FeatureFlagRepo.

Test isolation: outer-transaction rollback via the ``db_session`` fixture. Tests do NOT
call ``session.commit()`` — they call repo methods (which flush internally on writes) and
verify state with ``session.execute(select(...))``.

Acceptance:
- migration creates the table with the right columns + unique key + enabled index
- ``FeatureFlagRepo.get`` returns ``False`` for missing flag
- ``FeatureFlagRepo.set_enabled`` upserts; second call updates
- All ``memory.*`` flag keys default OFF (no seed rows enabling them)
- Per-scope flags coexist with global flags under the same flag_key
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("app_env")


def _unique_flag_key() -> str:
    return f"memory.test.flag_{random.randint(900_000_000, 999_999_999)}"


async def test_get_missing_flag_returns_false(db_session) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo

    assert (
        await FeatureFlagRepo.get(db_session, _unique_flag_key())
    ) is False


async def test_set_enabled_creates_row_and_get_returns_true(db_session) -> None:
    from bot.db.models import FeatureFlag
    from bot.db.repos.feature_flag import FeatureFlagRepo

    key = _unique_flag_key()

    flag = await FeatureFlagRepo.set_enabled(
        db_session, flag_key=key, enabled=True, updated_by=42
    )
    assert flag.flag_key == key
    assert flag.enabled is True
    assert flag.scope_type is None
    assert flag.scope_id is None
    assert flag.updated_by == 42

    assert (await FeatureFlagRepo.get(db_session, key)) is True

    # Sanity: exactly one row by logical key.
    rows = await db_session.execute(
        select(FeatureFlag).where(
            FeatureFlag.flag_key == key,
            FeatureFlag.scope_type.is_(None),
            FeatureFlag.scope_id.is_(None),
        )
    )
    assert len(rows.scalars().all()) == 1


async def test_set_enabled_updates_existing_row_no_duplicate(db_session) -> None:
    from bot.db.models import FeatureFlag
    from bot.db.repos.feature_flag import FeatureFlagRepo

    key = _unique_flag_key()

    await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=False)
    await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=True, config_json={"v": 1})

    assert (await FeatureFlagRepo.get(db_session, key)) is True

    rows = await db_session.execute(
        select(FeatureFlag).where(
            FeatureFlag.flag_key == key,
            FeatureFlag.scope_type.is_(None),
            FeatureFlag.scope_id.is_(None),
        )
    )
    persisted = rows.scalars().all()
    assert len(persisted) == 1
    await db_session.refresh(persisted[0])
    assert persisted[0].enabled is True
    assert persisted[0].config_json == {"v": 1}


async def test_per_scope_flags_coexist_with_global(db_session) -> None:
    from bot.db.repos.feature_flag import FeatureFlagRepo

    key = _unique_flag_key()

    # Global OFF; per-chat ON.
    await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=False)
    await FeatureFlagRepo.set_enabled(
        db_session,
        flag_key=key,
        enabled=True,
        scope_type="chat",
        scope_id="-1001234567890",
    )

    assert (await FeatureFlagRepo.get(db_session, key)) is False
    assert (
        await FeatureFlagRepo.get(
            db_session, key, scope_type="chat", scope_id="-1001234567890"
        )
    ) is True


async def test_memory_flags_have_no_enabled_seed_rows(db_session) -> None:
    """T1-01 invariant: the migration MUST NOT seed any ENABLED memory.* flag.

    Stricter than the bare "no seed rows" check — what we actually care about is that no
    migration silently turns on a memory feature. A future ticket may legitimately seed a
    documentation row with ``enabled=False`` (e.g., to expose a flag in an admin UI before
    operators toggle it); that is fine. What is NEVER fine is a seed with ``enabled=True``.
    """
    from bot.db.models import FeatureFlag

    rows = await db_session.execute(
        select(FeatureFlag).where(
            FeatureFlag.flag_key.like("memory.%"),
            FeatureFlag.enabled.is_(True),
        )
    )
    seeded = rows.scalars().all()
    assert seeded == [], (
        "memory.* flag was seeded with enabled=True by a migration: "
        f"{[r.flag_key for r in seeded]}"
    )


async def test_set_enabled_updates_updated_at(db_session) -> None:
    """Audit trail invariant: ``updated_at`` must change on conflict-update path.

    The model declares ``onupdate=func.now()`` for ORM-managed updates, but
    ``pg_insert(...).on_conflict_do_update(...)`` is a Core statement and does NOT trigger
    the ORM hook. Repo sets ``updated_at`` explicitly in the conflict ``set_`` map.
    """
    from bot.db.repos.feature_flag import FeatureFlagRepo

    key = _unique_flag_key()

    inserted = await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=False)
    original_updated_at = inserted.updated_at

    updated = await FeatureFlagRepo.set_enabled(db_session, flag_key=key, enabled=True)
    await db_session.refresh(updated)

    assert updated.updated_at > original_updated_at


def test_engine_module_imports_feature_flag_model(app_env) -> None:
    """Smoke: the FeatureFlag model is importable and registers with SQLAlchemy metadata.
    This is a non-DB sanity check that runs even without postgres."""
    from tests.conftest import import_module

    models = import_module("bot.db.models")
    assert hasattr(models, "FeatureFlag")
    assert "feature_flags" in models.Base.metadata.tables
    table = models.Base.metadata.tables["feature_flags"]
    cols = {c.name for c in table.columns}
    assert {
        "id",
        "flag_key",
        "scope_type",
        "scope_id",
        "enabled",
        "config_json",
        "updated_by",
        "created_at",
        "updated_at",
    } == cols
