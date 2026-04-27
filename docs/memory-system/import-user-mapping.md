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

**Sentinel reservation:** The value `-1` AND the entire range of negative integers is
reserved for import-internal sentinels in the `users` table. Real Telegram user ids are
always positive. Telegram CHAT ids can be negative (e.g. supergroup ids in the
`-1001...` range) but those are NOT stored in `users.id`. A future "per-channel ghost"
ticket that wants to use the channel id as the ghost's `user_id` MUST pick a different
mapping (e.g. `1_000_000_000_000 + channel_id`, or a separate sentinels table) to avoid
colliding with the anonymous-channel singleton.

---

## `is_imported_only` Flag Lifecycle

`users.is_imported_only` (Boolean, NOT NULL, default `false`) is the formal marker for
ghost users.

| State | Meaning |
|-------|---------|
| `false` (default) | Live user — created or confirmed by the gatekeeper registration flow |
| `true` | Ghost user — created by the import service; never registered with the bot |

### Flag set to TRUE

The flag is set to `True` **only** when the import path creates a user row that did not
exist before (`_create_ghost_user` INSERT). It is **NEVER** set to `True` on existing
rows: a live user being touched by an import call returns its existing id with
`is_imported_only` unchanged.

### Flag set to FALSE (ghost-to-live transition)

The flag is set to `False` when the gatekeeper's live-registration path
(`UserRepo.upsert(telegram_id=...)`, called from `bot/handlers/start.py` and other
gatekeeper entry points) writes the row. If the row already existed as a ghost, the
`ON CONFLICT DO UPDATE` clause overwrites the name fields **AND clears the flag** — the
user transitions from "imported-only history" to "live, gatekeeper-known". This is the
**only** ghost-to-live transition path. Imports cannot perform it; only the live
registration path can.

This means `is_ghost_user(user_id)` reflects "this row was last touched by the import
path and has **never** been touched by live registration since".

### Privacy invariant (ag-sa risk R2)

Imports cannot promote themselves to live status. A user imported as a ghost remains a
ghost until they DM the gatekeeper. From that point on, the row is a live row by
definition; the historical imported messages remain attributed to the same `user_id` (see
"Attribution Semantics Under Live/Ghost Overlap" below).

**If a row already exists for a tg_id**, `_create_ghost_user` returns it unchanged
regardless of its current `is_imported_only` value:
- Live row exists → returned as-is (`is_imported_only=False` stays `False`).
- Ghost row exists → returned as-is (`is_imported_only=True` stays `True`).

The `_create_ghost_user` function is the import-internal helper; it never flips live rows
to ghost. Only `UserRepo.upsert` (the live-registration path) can flip ghost to live.

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
| Negative tail (e.g. `"user-5"`, `"channel-5"`) | Raises `ValueError("negative export ids not allowed: ...")`. |
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

## Attribution Semantics Under Live/Ghost Overlap

When `resolve_export_user(session, "user<N>")` is called and a **live** user with `id=N`
already exists, the function returns `N`. The imported message's `chat_messages.user_id`
is set to `N` directly.

This means imported historical messages are **permanently attributed** to the live
member's row. A community member can later see their own pre-membership history attached
to their identity.

This is a deliberate product choice: **identity continuity > separate-history-bucket**.
Splitting (e.g. "always create a parallel ghost regardless of live existence") is
rejected because it would break Q&A / search citations across the live/import boundary.

**For issue #103:** write `chat_messages.user_id = resolve_export_user(from_id)` directly.
Do NOT introduce a parallel "imported user id" column.

**Cross-reference:** `AUTHORIZED_SCOPE.md` "Critical safety rule for `#offrecord`" still
applies — content from imported messages routes through `detect_policy` exactly as live
messages do, regardless of attribution.

---

## Out of Scope

The following are explicitly deferred and NOT authorized in this cycle:

- **Display name updates from imports** — subsequent imports with a different `from` field
  do not update ghost or live display names.
- **Per-channel ghost identity splitting** — all `channel<N>` posts collapse to one user.
  Splitting by channel id is deferred to a future phase.
- **Ghost cleanup / expiry** — ghost rows are permanent unless explicitly deleted via the
  forget / rollback path (T2-NEW-G, issue #104).
- **Bulk-resolve API for #102 (rate-limit + chunking):** current implementation is
  per-call (one SELECT-then-INSERT round-trip per `resolve_export_user`). A bulk variant
  (e.g. `resolve_export_users(session, list_of_export_ids) → dict`) is deferred to
  #102 / #103 and is not provided here.
- **Distinguishing live vs absent in `is_ghost_user()`:** currently returns `False` for
  both "live row exists" and "no row at all" — caller must validate id existence
  separately. A `user_status(user_id) → Literal['live', 'ghost', 'absent']` helper is
  deferred until a downstream ticket needs it (e.g. #98 reply resolver may surface this
  need).
