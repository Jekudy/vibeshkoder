"""Import user mapping service (T2-NEW-B, issue #93).

Maps a Telegram Desktop export ``from_id`` string to an internal ``users.id`` (integer PK).

The Telegram Desktop export uses opaque prefixed strings for sender identity:
  ``"user<N>"``     — a regular Telegram user whose numeric id is N.
  ``"channel<N>"``  — an anonymous channel post (no individual user identity available).

The live gatekeeper stores users in the ``users`` table with ``users.id = N`` (the raw
Telegram user id as the PK — there is no separate ``tg_id`` column).

Three mapping cases:
  1. **Known user** — ``from_id="user<N>"``, N matches an existing ``users.id``.
     Returned directly; ``is_imported_only`` is never modified.
  2. **Unknown user** — ``from_id="user<N>"``, no ``users.id=N`` row exists.
     A ghost row is created with ``is_imported_only=True`` (unless
     ``create_ghost_if_missing=False``).
  3. **Anonymous channel post** — ``from_id="channel<N>"``.
     All channel posts collapse to one singleton ghost user with ``tg_id=-1``.
     We do NOT preserve per-channel identity in Phase 2 — see issue #93 and
     ``docs/memory-system/import-user-mapping.md`` §3 for the cross-phase note.

Privacy invariant (ag-sa risk R2):
  Ghost users (``is_imported_only=True``) are NEVER merged with live users
  (``is_imported_only=False``). If a row already exists with the requested ``tg_id``,
  this service returns its ``id`` WITHOUT modifying ``is_imported_only``.  A live user
  (``is_imported_only=False``) is returned as-is — it is NOT flipped to imported_only.
  A ghost user is also returned as-is — it is NOT flipped back to live.

Display-name policy:
  Ghost user ``first_name`` (the display_name) is set on creation and NEVER overwritten
  by subsequent import calls. Export-time display names are untrusted — they must not
  rewrite live user data nor silently mutate ghost names.

Cross-references:
  - Issue #94 (import dry-run parser) consumes ``resolve_export_user`` for every message.
  - Issue #98 (reply resolver) uses ghost users to resolve dangling reply targets.
  - Issue #103 (import apply) routes all messages through this service.
  - ``docs/memory-system/import-user-mapping.md`` — full implementer doc.
  - ``docs/memory-system/telegram-desktop-export-schema.md`` §6 — from_id field shape.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User

# Sentinel Telegram id used for the singleton anonymous-channel ghost.
# Real Telegram user ids are always positive; -1 is safe as a dedicated sentinel.
ANONYMOUS_CHANNEL_USER_TG_ID: int = -1
ANONYMOUS_CHANNEL_USER_DISPLAY_NAME: str = "[anonymous channel]"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_export_user(
    session: AsyncSession,
    export_id: str | None,
    *,
    display_name: str | None = None,
    create_ghost_if_missing: bool = True,
) -> int | None:
    """Map a Telegram Desktop export ``from_id`` to ``users.id``.

    Args:
        session: SQLAlchemy async session (caller manages transaction).
        export_id: The ``from_id`` field from the TD export message object.
            ``None`` means the field was absent (service message or no sender).
        display_name: The ``from`` field from the export message, used as the
            ghost user's ``first_name`` when a new ghost row is created.
            Ignored if the row already exists (first-write-wins policy).
        create_ghost_if_missing: If True (default), an unknown ``user<N>`` creates
            a ghost row. Set to False for dry-run paths that must not write.

    Returns:
        ``users.id`` integer, or None when:
          - ``export_id`` is None (caller treats the message as a service message).
          - ``export_id`` is a ``user<N>`` form, no row exists, and
            ``create_ghost_if_missing=False``.

    Raises:
        ValueError: ``export_id`` has an unrecognised prefix or a non-numeric tail.
    """
    if export_id is None:
        return None

    prefix, numeric_id = _parse_export_id(export_id)

    if prefix == "channel":
        return (await _get_or_create_anonymous_channel_user(session)).id

    # prefix == "user"
    tg_id = numeric_id
    existing = await session.execute(select(User).where(User.id == tg_id))
    user = existing.scalar_one_or_none()
    if user is not None:
        # Privacy invariant: return as-is, never flip is_imported_only.
        return user.id

    if not create_ghost_if_missing:
        return None

    ghost = await _create_ghost_user(
        session,
        tg_id=tg_id,
        display_name=display_name or f"imported user {tg_id}",
    )
    return ghost.id


async def is_ghost_user(session: AsyncSession, user_id: int) -> bool:
    """Return True if the user row identified by ``user_id`` (users.id) has
    ``is_imported_only=True``.

    Returns False if the row does not exist (fail-safe; callers should not pass
    non-existent ids, but guard against races after delete).
    """
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return False
    return bool(user.is_imported_only)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_export_id(export_id: str) -> tuple[str, int]:
    """Parse a TD export ``from_id`` string into ``(prefix, numeric_tail)``.

    Valid forms: ``"user<N>"`` and ``"channel<N>"`` where N is a positive integer.

    Returns:
        Tuple of (prefix, numeric_tail) where prefix is ``"user"`` or ``"channel"``.

    Raises:
        ValueError: Unknown prefix or non-numeric tail.
    """
    for prefix in ("user", "channel"):
        if export_id.startswith(prefix):
            tail = export_id[len(prefix):]
            if not tail.lstrip("-").isdigit():
                raise ValueError(
                    f"non-numeric tail in export_id: {export_id!r} "
                    f"(tail={tail!r})"
                )
            return prefix, int(tail)
    raise ValueError(f"unrecognised export_id shape: {export_id!r}")


async def _create_ghost_user(
    session: AsyncSession,
    tg_id: int,
    display_name: str,
) -> User:
    """INSERT a ghost user (is_imported_only=True) for the given ``tg_id``.

    Idempotent on ``tg_id``: if a row already exists for that id, returns it WITHOUT
    modifying any fields (first-write-wins; privacy invariant: is_imported_only is
    never flipped regardless of the existing row's value).

    Uses INSERT ... ON CONFLICT DO NOTHING RETURNING, with a SELECT-fallback for the
    conflict path — mirrors the T0-03 MessageRepo.save pattern for race safety under
    concurrent imports.

    Privacy assertion: if the existing row has is_imported_only=False (live user),
    this method returns it unchanged. The caller MUST NOT treat this as a ghost-creation
    success — the caller should check the returned row's is_imported_only if it needs to
    distinguish ghost vs live. This function's contract is "return the row for tg_id,
    creating it as a ghost if it does not exist".
    """
    stmt = (
        pg_insert(User)
        .values(
            id=tg_id,
            username=None,
            first_name=display_name,
            last_name=None,
            is_imported_only=True,
        )
        .on_conflict_do_nothing(index_elements=[User.id])
        .returning(User)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    await session.flush()

    if row is not None:
        return row

    # Conflict path: row already exists; fetch and return it unchanged.
    existing = await session.execute(select(User).where(User.id == tg_id))
    return existing.scalar_one()


async def _get_or_create_anonymous_channel_user(session: AsyncSession) -> User:
    """Find or create the singleton anonymous-channel ghost user (tg_id=-1).

    All anonymous channel posts (``from_id`` starting with ``"channel"``) collapse to
    this one logical user in Phase 2. Future phases may split per-channel identity —
    see issue #93 and ``docs/memory-system/import-user-mapping.md`` §3 for the note.

    Idempotent: multiple concurrent imports will resolve to the same row.
    """
    return await _create_ghost_user(
        session,
        tg_id=ANONYMOUS_CHANNEL_USER_TG_ID,
        display_name=ANONYMOUS_CHANNEL_USER_DISPLAY_NAME,
    )
