"""Async SQLAlchemy engine, postgres-only.

Sqlite was previously used as a dev fallback, but the codebase relies on postgres-specific
SQL (e.g. ``ON CONFLICT DO UPDATE`` in ``UserRepo.upsert``) that compiles only against the
postgres dialect. Running against sqlite caused silent runtime failures.

Per ``docs/memory-system/HANDOFF.md`` §0 and ticket T0-02 acceptance, dev/test now requires
postgres. The dev compose stack provides one on ``127.0.0.1:5433`` (see
``docker-compose.dev.yml`` and ``docs/memory-system/DEV_SETUP.md``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings


def _validate_postgres_url(url: str) -> str:
    if not url:
        raise RuntimeError(
            "DATABASE_URL is empty. Set it to a postgres URL such as "
            "'postgresql+asyncpg://user:pass@host:port/dbname'. See "
            "docs/memory-system/DEV_SETUP.md for the dev setup."
        )
    if not (url.startswith("postgresql://") or url.startswith("postgresql+")):
        raise RuntimeError(
            f"DATABASE_URL must point at postgres (got scheme: {url.split('://', 1)[0]!r}). "
            "The codebase relies on postgres-specific SQL (see ticket T0-02). Use the dev "
            "postgres from docker-compose.dev.yml — see docs/memory-system/DEV_SETUP.md."
        )
    return url


_url = _validate_postgres_url(settings.DATABASE_URL)
engine = create_async_engine(_url, echo=False)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
