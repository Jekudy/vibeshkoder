"""T2-NEW-B acceptance tests — import_user_map service.

Tests exercise the three mapping cases (known user, unknown/ghost user, anonymous channel),
privacy invariants (ghost never merges with live), display-name first-write-wins policy,
and edge cases (None export_id, bad prefix, non-numeric tail).

Isolation: each test runs inside the ``db_session`` fixture's outer transaction which is
rolled back at teardown. Tests MUST NOT call ``session.commit()``.

Tests are SKIPPED if no postgres is reachable (see ``conftest.postgres_engine``).

Note on imports: all bot.* imports are done INSIDE test functions (not at module level) to
avoid SQLAlchemy mapper initialization issues caused by conftest's ``_clear_modules`` running
between test collection and test execution. This matches the pattern used in existing DB tests.
"""

from __future__ import annotations

import random

import pytest

pytestmark = pytest.mark.usefixtures("app_env")


def _rand_tg_id() -> int:
    """High-range random id to avoid collision with fixture-seeded ids."""
    return random.randint(900_000_000, 999_999_999)


async def _create_live_user(
    session,
    tg_id: int,
    first_name: str = "Live",
    last_name: str | None = None,
):
    """Insert a live (is_imported_only=False) user directly."""
    from bot.db.repos.user import UserRepo

    return await UserRepo.upsert(
        session,
        telegram_id=tg_id,
        username=None,
        first_name=first_name,
        last_name=last_name,
    )


async def _get_user(session, tg_id: int):
    from sqlalchemy import select

    from bot.db.models import User

    result = await session.execute(select(User).where(User.id == tg_id))
    return result.scalar_one_or_none()


async def _count_users_with_tg_id(session, tg_id: int) -> int:
    from sqlalchemy import select

    from bot.db.models import User

    result = await session.execute(select(User).where(User.id == tg_id))
    return len(result.scalars().all())


# ─── Case 1: known user ──────────────────────────────────────────────────────

async def test_resolve_known_user(db_session) -> None:
    """Known user (tg_id exists in DB) → returns users.id; is_imported_only stays False."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()
    live = await _create_live_user(db_session, tg_id, first_name="Alice")

    resolved_id = await resolve_export_user(db_session, f"user{tg_id}")

    assert resolved_id == live.id
    refreshed = await _get_user(db_session, tg_id)
    assert refreshed is not None
    assert refreshed.is_imported_only is False


# ─── Case 2: unknown user → create ghost ─────────────────────────────────────

async def test_resolve_unknown_user_creates_ghost(db_session) -> None:
    """Unknown user → creates ghost row with is_imported_only=True and display_name."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()
    assert await _count_users_with_tg_id(db_session, tg_id) == 0

    resolved_id = await resolve_export_user(
        db_session, f"user{tg_id}", display_name="Foo Bar"
    )

    assert resolved_id is not None
    ghost = await _get_user(db_session, tg_id)
    assert ghost is not None
    assert ghost.is_imported_only is True
    # display_name mapped to first_name
    assert ghost.first_name == "Foo Bar"


async def test_resolve_unknown_user_no_create_returns_none(db_session) -> None:
    """create_ghost_if_missing=False and no row → returns None, no row created."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()

    result = await resolve_export_user(
        db_session, f"user{tg_id}", create_ghost_if_missing=False
    )

    assert result is None
    assert await _count_users_with_tg_id(db_session, tg_id) == 0


async def test_resolve_unknown_idempotent(db_session) -> None:
    """Two calls with same export_id → only one row created; same id returned both times."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()

    id1 = await resolve_export_user(db_session, f"user{tg_id}", display_name="Ghost")
    id2 = await resolve_export_user(db_session, f"user{tg_id}", display_name="Ghost")

    assert id1 == id2
    assert await _count_users_with_tg_id(db_session, tg_id) == 1


# ─── Case 3: anonymous channel post ──────────────────────────────────────────

async def test_resolve_channel_creates_anonymous_user(db_session) -> None:
    """channel<N> → singleton anonymous channel ghost (tg_id=-1, is_imported_only=True)."""
    from bot.services.import_user_map import (
        ANONYMOUS_CHANNEL_USER_TG_ID,
        resolve_export_user,
    )

    resolved_id = await resolve_export_user(db_session, "channel100")

    assert resolved_id is not None
    anon = await _get_user(db_session, ANONYMOUS_CHANNEL_USER_TG_ID)
    assert anon is not None
    assert anon.is_imported_only is True
    assert anon.id == resolved_id


async def test_resolve_multiple_channels_collapse_to_one(db_session) -> None:
    """channel100 and channel200 both resolve to the SAME singleton users.id."""
    from bot.services.import_user_map import resolve_export_user

    id1 = await resolve_export_user(db_session, "channel100")
    id2 = await resolve_export_user(db_session, "channel200")

    assert id1 == id2
    assert id1 is not None


# ─── None export_id ──────────────────────────────────────────────────────────

async def test_resolve_none_returns_none(db_session) -> None:
    """None export_id → returns None; no rows created."""
    from bot.services.import_user_map import resolve_export_user

    result = await resolve_export_user(db_session, None)

    assert result is None


# ─── Error cases ─────────────────────────────────────────────────────────────

async def test_resolve_unknown_prefix_raises(db_session) -> None:
    """Unrecognised prefix (e.g. 'bot42') → ValueError."""
    from bot.services.import_user_map import resolve_export_user

    with pytest.raises(ValueError, match="unrecognised export_id shape"):
        await resolve_export_user(db_session, "bot42")


async def test_resolve_non_numeric_tail_raises(db_session) -> None:
    """'userabc' → ValueError (non-numeric tail)."""
    from bot.services.import_user_map import resolve_export_user

    with pytest.raises(ValueError):
        await resolve_export_user(db_session, "userabc")


# ─── Display name policy ─────────────────────────────────────────────────────

async def test_display_name_first_write_wins(db_session) -> None:
    """Ghost created with 'Original'; second call with 'New' must NOT overwrite."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()

    await resolve_export_user(db_session, f"user{tg_id}", display_name="Original")
    await resolve_export_user(db_session, f"user{tg_id}", display_name="New")

    ghost = await _get_user(db_session, tg_id)
    assert ghost is not None
    assert ghost.first_name == "Original"


async def test_ghost_user_id_returned_on_repeat_call_with_different_display_name(
    db_session,
) -> None:
    """Same ghost id returned even when display_name differs on second call."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()

    id1 = await resolve_export_user(
        db_session, f"user{tg_id}", display_name="Alice"
    )
    id2 = await resolve_export_user(
        db_session, f"user{tg_id}", display_name="Алиса (new name in later export)"
    )

    assert id1 == id2
    ghost = await _get_user(db_session, tg_id)
    assert ghost.first_name == "Alice"  # first-write wins


# ─── Privacy invariant: ghost NEVER flips live user ──────────────────────────

async def test_ghost_NEVER_flips_live_user_to_ghost(db_session) -> None:
    """Pre-existing live user must NOT have is_imported_only flipped to True."""
    from bot.services.import_user_map import resolve_export_user

    tg_id = _rand_tg_id()
    live = await _create_live_user(db_session, tg_id, first_name="RealPerson")

    resolved_id = await resolve_export_user(
        db_session,
        f"user{tg_id}",
        create_ghost_if_missing=True,
        display_name="Some Import Name",
    )

    # Returns the existing live user's id
    assert resolved_id == live.id
    refreshed = await _get_user(db_session, tg_id)
    assert refreshed is not None
    assert refreshed.is_imported_only is False  # NEVER flipped


# ─── is_ghost_user helper ─────────────────────────────────────────────────────

async def test_is_ghost_user(db_session) -> None:
    """is_ghost_user returns True for ghost, False for live."""
    from bot.services.import_user_map import is_ghost_user, resolve_export_user

    live_tg_id = _rand_tg_id()
    ghost_tg_id = _rand_tg_id()

    await _create_live_user(db_session, live_tg_id)
    live = await _get_user(db_session, live_tg_id)

    ghost_user_id = await resolve_export_user(
        db_session, f"user{ghost_tg_id}", display_name="Ghost"
    )

    assert await is_ghost_user(db_session, live.id) is False
    assert await is_ghost_user(db_session, ghost_user_id) is True
