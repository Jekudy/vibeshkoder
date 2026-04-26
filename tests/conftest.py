from __future__ import annotations

import importlib
import os
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio


def _clear_modules() -> None:
    for name in list(sys.modules):
        if name == "bot" or name.startswith("bot.") or name == "web" or name.startswith("web."):
            sys.modules.pop(name, None)


DEFAULT_LOCAL_POSTGRES_URL = "postgresql+asyncpg://shkoder_dev:shkoder_dev@127.0.0.1:5433/shkoder_dev"


@pytest.fixture()
def app_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("COMMUNITY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "[149820031]")
    # Set DATABASE_URL ONLY if it is not already set externally. CI provides the postgres-
    # service URL via env (`localhost:5432`); overriding it here would route DB-backed tests
    # at a non-existent host and cause silent skips. Locally, fall back to the dev postgres
    # exposed by ``docker-compose.dev.yml`` on 127.0.0.1:5433. Tests that mock the DB never
    # connect and accept any valid postgres URL.
    if not os.environ.get("DATABASE_URL"):
        monkeypatch.setenv("DATABASE_URL", DEFAULT_LOCAL_POSTGRES_URL)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDS_FILE", "")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setenv("WEB_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("WEB_BOT_USERNAME", "vibeshkoder_dev_bot")
    monkeypatch.setenv("DB_PASSWORD", "changeme")
    monkeypatch.setenv("WEB_PASSWORD", "test-pass")
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEV_MODE", "true")
    _clear_modules()
    yield
    _clear_modules()


def import_module(name: str):
    return importlib.import_module(name)


# ─── DB-backed test fixtures ─────────────────────────────────────────────────────────────────
#
# Tests that exercise real SQL (e.g. UserRepo.upsert) require a reachable postgres.
#
# Resolution order for the URL:
#   1. TEST_DATABASE_URL env var (preferred — explicit override)
#   2. DATABASE_URL env var (CI sets this to the postgres service)
#   3. Local default postgres on 127.0.0.1:5433 (matches docker-compose.dev.yml dev postgres)
#
# If postgres is unreachable, DB-backed tests are SKIPPED (not failed) so that contributors
# without local postgres can still run the unit suite. CI runs against a real postgres
# service container, so missed coverage is caught before merge.

def _resolve_test_postgres_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_LOCAL_POSTGRES_URL
    )


def _safe_url_repr(url_str: str) -> str:
    """Render a SQLAlchemy URL string with the password redacted, for safe logging."""
    from sqlalchemy.engine.url import make_url

    try:
        return make_url(url_str).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable URL>"


@pytest_asyncio.fixture()
async def postgres_engine():
    """Yield an async SQLAlchemy engine bound to the test postgres, or skip the test if
    postgres is unreachable. Each test gets a fresh engine so connection pools do not leak."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = _resolve_test_postgres_url()
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover — environment guard, not behaviour
        await engine.dispose()
        pytest.skip(f"postgres unreachable at {_safe_url_repr(url)}: {exc!s}")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def db_session(postgres_engine) -> AsyncIterator:
    """Yield an AsyncSession bound to the test postgres.

    Isolation: the session uses a single connection wrapped in an outer transaction. The
    outer transaction is rolled back at fixture teardown, discarding all changes the test
    made. Tests MUST NOT call ``session.commit()`` — doing so would commit data to the DB
    permanently and break isolation. Use ``session.flush()`` instead (UserRepo methods
    already flush internally).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    async with postgres_engine.connect() as conn:
        outer = await conn.begin()
        Session = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            try:
                yield session
            finally:
                # Always roll back the outer transaction; this discards all test data.
                if outer.is_active:
                    await outer.rollback()
