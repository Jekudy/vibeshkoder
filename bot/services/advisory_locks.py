"""Safe dedicated-transaction PostgreSQL advisory-lock scopes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


def _engine_for_session(session: AsyncSession) -> AsyncEngine | None:
    bind: Any = session.bind
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    return None


def session_uses_postgresql(session: AsyncSession) -> bool:
    return getattr(getattr(session.bind, "dialect", None), "name", None) == "postgresql"


def chat_message_advisory_key(chat_id: int, message_id: int) -> str:
    return f"chat_msg:{chat_id}:{message_id}"


def forget_target_advisory_key(target_type: str, target_id: str) -> str:
    if target_type not in {"message", "user", "message_hash"}:
        raise ValueError(f"unsupported forget target lock type: {target_type}")
    return f"forget_target:{target_type}:{target_id}"


@asynccontextmanager
async def hold_session_advisory_locks(
    session: AsyncSession,
    lock_ids: Iterable[int],
    *,
    lock_keys: Iterable[str] = (),
) -> AsyncIterator[None]:
    """Hold sorted xact locks in a dedicated transaction across caller commits.

    The transaction belongs to a separate checked-out connection, so application
    commits inside the protected scope cannot release its locks. Transaction-level
    locks require no manual unlock and cannot leak when the connection returns to
    the pool after an exception or task cancellation.
    """

    ordered = sorted(set(lock_ids))
    ordered_keys = sorted(set(lock_keys))
    if not ordered and not ordered_keys:
        yield
        return
    engine = _engine_for_session(session)
    if engine is None or not session_uses_postgresql(session):
        # Unit fakes and non-PostgreSQL tests cannot execute advisory locks.
        # Production is PostgreSQL and always takes the dedicated lock path.
        yield
        return

    async with engine.connect() as connection:
        async with connection.begin():
            for lock_id in ordered:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
            for lock_key in ordered_keys:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": lock_key},
                )
            yield


__all__ = [
    "chat_message_advisory_key",
    "forget_target_advisory_key",
    "hold_session_advisory_locks",
    "session_uses_postgresql",
]
