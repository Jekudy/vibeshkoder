# Import User Mapping Policy

**Document:** T2-NEW-B (issue #93)
**Status:** implemented
**Date:** 2026-04-27
**Scope:** `bot/services/import_user_map.py`, migration `013`, `bot/db/repos/user.py`

---

## Purpose

Telegram Desktop exports identify message senders with an opaque prefixed string in the
`from_id` field (e.g. `"user12345"`, `"channel50000"`). The live gatekeeper stores users
in `users.id` (the raw Telegram user id integer — there is no separate `tg_id` column).

This document describes the mapping policy that translates a TD export `from_id` to an
internal `users.id` so that imported messages can carry a valid `user_id` foreign key.

---

## Three Cases

### Case 1 — Known user

**Condition:** `from_id = "user<N>"` and a row exists in `users` with `id = N`.

**Action:** Return `users.id` (which equals N). No fields are modified; `is_imported_only`
is never read or changed.

**Rationale:** This is a live or previously-imported user. The caller just needs the PK.

---

### Case 2 — Unknown user (ghost creation)

**Condition:** `from_id = "user<N>"` and no row exists with `users.id = N`.

**Action:** Create a ghost row:
- `users.id = N` (Telegram user id)
- `users.first_name = display_name` (from the export `from` field if available, otherwise
  `"imported user N"`)
- `users.is_imported_only = True`
- All other columns at their defaults (`is_member=False`, `is_admin=False`, etc.)

Return the new (or existing — idempotent) `users.id`.

**Ghost semantics:**
- A ghost user represents a Telegram account that has never interacted with the gatekeeper
  bot directly (e.g. a left member, a deleted account, a historical contributor).
- Ghost rows are safe to create — they do not grant bot access or community membership.
- The `is_imported_only` flag marks the row as import-only so downstream code can apply
  appropriate restrictions (e.g. skip ghost users in @-mention suggestions).

**Display name policy (first-write wins):**
The `first_name` (display_name) is set on first creation and NEVER overwritten by
subsequent import calls. Export-time display names are untrusted — a user may have a
different display name at different export dates, and we must not retroactively alter
ghost rows. See "Display name policy" section below.

---

### Case 3 — Anonymous channel post

**Condition:** `from_id = "channel<N>"` (any N).

**Action:** Resolve to the singleton anonymous-channel ghost user:
- `users.id = -1` (sentinel; all real Telegram ids are positive)
- `users.first_name = "[anonymous channel]"`
- `users.is_imported_only = True`

All anonymous channel posts collapse to this one logical user. Per-channel identity
is NOT preserved in Phase 2 — all `channel<N>` posts, regardless of N, map to the
same row. Future phases may split per-channel identity.

**Cross-reference:** `telegram-desktop-export-schema.md` §6.

---

## `is_imported_only` Flag Semantics

`users.is_imported_only` (Boolean, NOT NULL, default `false`) is the formal marker for
ghost users.

| State | Meaning |
|-------|---------|
| `false` (default) | Live user — created by the gatekeeper registration flow |
| `true` | Ghost user — created by the import service; never interacted with the bot |

**Rules:**

1. The flag is set to `True` **only on ghost row creation** by the import service.
2. The flag is **NEVER automatically flipped** in either direction by the import service.
3. Live users (`is_imported_only=False`) and ghost users (`is_imported_only=True`) are
   **NEVER merged**. If a ghost's `tg_id` later becomes a real Telegram user (they DM the
   gatekeeper and a live row is required), the registration path creates a **new live row**
   — the ghost is left untouched. This avoids retroactively giving a live user the
   import-only data history.
4. **If a row already exists for a tg_id**, `_create_ghost_user` returns it unchanged
   regardless of its current `is_imported_only` value:
   - Live row exists → returned as-is (is_imported_only=False stays False).
   - Ghost row exists → returned as-is (is_imported_only=True stays True).

**ag-sa risk R2:** User identity collision — ghost users merged with live users by
display_name match → cross-user privacy leak. This policy is the mitigation:
`is_imported_only` is the explicit boundary, and the import service never crosses it.

---

## Display Name Policy

The export `from` field contains the sender's display name **at export time**. This name
may differ from the live user's current name or from names in other exports.

Policy: **first-write wins**.

- When a ghost row is created, `first_name` is set from `display_name` (the `from` field).
- Subsequent calls to `resolve_export_user` for the same `tg_id` with a different
  `display_name` do NOT modify the existing `first_name`.
- The import service does not and must not update live user display names from import data.
  Live user data is managed by the gatekeeper registration/profile flow only.

---

## Public API

```python
from bot.services.import_user_map import (
    ANONYMOUS_CHANNEL_USER_TG_ID,
    ANONYMOUS_CHANNEL_USER_DISPLAY_NAME,
    resolve_export_user,
    is_ghost_user,
)
```

### `resolve_export_user`

```python
async def resolve_export_user(
    session: AsyncSession,
    export_id: str | None,
    *,
    display_name: str | None = None,
    create_ghost_if_missing: bool = True,
) -> int | None:
```

Maps a TD export `from_id` to `users.id`.

- `export_id=None` → returns `None` (service message or absent sender field; caller handles).
- `export_id="user<N>"`, row exists → returns `users.id`.
- `export_id="user<N>"`, no row, `create_ghost_if_missing=True` → creates ghost, returns id.
- `export_id="user<N>"`, no row, `create_ghost_if_missing=False` → returns `None`.
- `export_id="channel<N>"` → resolves to singleton anonymous-channel ghost, returns id.
- Unknown prefix → raises `ValueError("unrecognised export_id shape: ...")`.
- Non-numeric tail → raises `ValueError`.

### `is_ghost_user`

```python
async def is_ghost_user(session: AsyncSession, user_id: int) -> bool:
```

Returns `True` if `users.is_imported_only=True` for the given `users.id`. Returns `False`
for live users and for non-existent ids (fail-safe).

---

## Failure Modes

| Condition | Behaviour |
|-----------|-----------|
| `export_id=None` | Returns `None`; no DB write. |
| Unknown prefix (e.g. `"bot42"`) | Raises `ValueError("unrecognised export_id shape: ...")`. |
| Non-numeric tail (e.g. `"userabc"`) | Raises `ValueError`. |
| Row exists for tg_id | Returned unchanged regardless of `is_imported_only`; no upsert. |
| Concurrent imports same tg_id | `INSERT ... ON CONFLICT DO NOTHING RETURNING` + SELECT fallback; both callers get the same row. |

---

## Idempotency Under Concurrent Calls

`_create_ghost_user` uses the T0-03 pattern:

1. `INSERT ... ON CONFLICT DO NOTHING RETURNING` — if the row is new, returns it.
2. If RETURNING is empty (conflict), `SELECT` fetches the existing row.

Under concurrent imports, both callers succeed and return the same row. There is no
race condition that can create duplicate rows (the PK `users.id` is unique).

---

## Cross-References

- **Issue #94** — import dry-run parser calls `resolve_export_user` for every message.
- **Issue #98** — reply resolver uses ghost users when resolving dangling reply targets.
- **Issue #103** — import apply routes all message `from_id` fields through this service.
- **`telegram-desktop-export-schema.md` §6** — `from_id` field shape and anonymous channel
  posts.
- **`HANDOFF.md` risk map R2** — user identity collision; this policy is the mitigation.

---

## Out of Scope

The following are explicitly deferred and NOT authorized in this cycle:

- **Display name updates from imports** — subsequent imports with a different `from` field
  do not update ghost or live display names.
- **Per-channel ghost identity splitting** — all `channel<N>` posts collapse to one user.
  Splitting by channel id is deferred to a future phase.
- **Ghost-to-live merge** — there is no merge path. A ghost and a live user for the same
  Telegram id coexist as separate rows. The live registration flow creates a new row; the
  ghost retains its import history.
- **Ghost cleanup / expiry** — ghost rows are permanent unless explicitly deleted via the
  forget / rollback path (T2-NEW-G, issue #104).
