"""T0-02 acceptance tests — UserRepo.upsert against real postgres.

Test isolation strategy: each test runs inside the `db_session` fixture's outer transaction
which is rolled back at fixture teardown. Tests do NOT call ``session.commit()`` — they call
``UserRepo.upsert()`` (which flushes internally) and verify state with ``session.execute(select(...))``.
``pg_insert.on_conflict_do_update`` works correctly within a single transaction: the second
upsert sees the first via the in-progress flush.

Tests use random telegram ids (high range, randomized per test) so any leaked rows from a
prior failed run cannot collide with a fresh run, and so concurrent test runs against the
same DB do not interfere.

Tests are SKIPPED if no postgres is reachable (see ``conftest.postgres_engine``). CI runs
against a postgres service container so this coverage runs on every PR.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

# `app_env` from conftest sets BOT_TOKEN, ADMIN_IDS, etc. via env vars, but does NOT override
# DATABASE_URL when one is set externally (CI provides the postgres service URL there).
# The `postgres_engine` fixture in conftest is the single resolver for the test DB URL.
pytestmark = pytest.mark.usefixtures("app_env")


def _random_test_id() -> int:
    # 9-digit space, well above any real telegram id seen in test fixtures.
    return random.randint(900_000_000, 999_999_999)


async def _count_users_with_id(session, telegram_id: int) -> int:
    from bot.db.models import User

    result = await session.execute(select(User).where(User.id == telegram_id))
    return len(result.scalars().all())


async def test_upsert_new_user_inserts_row(db_session) -> None:
    from bot.db.repos.user import UserRepo

    telegram_id = _random_test_id()

    user = await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="newcomer",
        first_name="New",
        last_name="Comer",
    )

    assert user.id == telegram_id
    assert user.username == "newcomer"
    assert user.first_name == "New"
    assert user.last_name == "Comer"
    assert await _count_users_with_id(db_session, telegram_id) == 1


async def test_upsert_existing_user_updates_fields_no_duplicate(db_session) -> None:
    from bot.db.repos.user import UserRepo

    telegram_id = _random_test_id()

    await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="old_name",
        first_name="Old",
        last_name=None,
    )

    updated = await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="new_name",
        first_name="New",
        last_name="Surname",
    )

    assert updated.id == telegram_id
    assert updated.username == "new_name"
    assert updated.first_name == "New"
    assert updated.last_name == "Surname"
    assert await _count_users_with_id(db_session, telegram_id) == 1


async def test_upsert_returns_row_after_insert(db_session) -> None:
    """The return value of upsert is the persisted row (matches a follow-up SELECT)."""
    from bot.db.models import User
    from bot.db.repos.user import UserRepo

    telegram_id = _random_test_id()

    returned = await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="probe",
        first_name="Probe",
        last_name=None,
    )

    fetched = (await db_session.execute(select(User).where(User.id == telegram_id))).scalar_one()
    assert returned.id == fetched.id
    assert returned.username == fetched.username
    assert returned.first_name == fetched.first_name


async def test_upsert_returns_row_after_update(db_session) -> None:
    """After a conflict, the return value reflects the new (updated) values."""
    from bot.db.repos.user import UserRepo

    telegram_id = _random_test_id()

    await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="first_iter",
        first_name="First",
        last_name=None,
    )

    updated = await UserRepo.upsert(
        db_session,
        telegram_id=telegram_id,
        username="second_iter",
        first_name="Second",
        last_name="Iteration",
    )

    assert updated.username == "second_iter"
    assert updated.first_name == "Second"
    assert updated.last_name == "Iteration"


def test_engine_rejects_sqlite_url(monkeypatch) -> None:
    """Engine module must refuse a sqlite URL with a clear error pointing at T0-02."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///vibe_gatekeeper.db")
    # Force re-import so the module sees the new env var.
    import sys

    for name in list(sys.modules):
        if name == "bot.db.engine" or name == "bot.config":
            sys.modules.pop(name, None)

    with pytest.raises(RuntimeError, match="sqlite"):
        import bot.db.engine  # noqa: F401


def test_engine_rejects_empty_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    import sys

    for name in list(sys.modules):
        if name == "bot.db.engine" or name == "bot.config":
            sys.modules.pop(name, None)

    with pytest.raises(RuntimeError, match="DATABASE_URL is empty"):
        import bot.db.engine  # noqa: F401
