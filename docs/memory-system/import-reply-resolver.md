# Import Reply Resolver

**Document:** T2-NEW-C (issue #98)
**Status:** implemented
**Date:** 2026-04-28
**Scope:** `bot/services/import_reply_resolver.py`

---

## Purpose

Two downstream tickets — #99 (T2-02 dry-run duplicate / policy stats) and #103
(T2-03 import apply with synthetic updates) — both need to map a Telegram
Desktop export `reply_to_message_id` (an integer id from the source JSON) onto
the actual `chat_messages.id` row in our DB. The resolution rules are
non-trivial: a reply target may live in the same import run, in an earlier
import run for the same chat, or in the live-ingested archive — and a
malformed export may dangle (point at an id that does not exist anywhere).

A shared resolver service exists so that #99 and #103 cannot drift. If each
ticket reimplemented its own lookup, dry-run preview counts would eventually
disagree with apply-time outcomes — the exact class of bug the dry-run / apply
contract is meant to prevent. One resolver, one priority order, one chat_id
scoping rule, one performance contract.

---

## When To Call

- **During #99 dry-run stats:** after parsing an export, the stats path calls
  `resolve_reply_batch` over the export's `reply_to_message_id` values to
  compute resolved-vs-unresolved breakdowns and feed `aggregate_resolutions`
  into the stats report. No DB writes happen — dry-run is read-only by the
  same contract `import-dry-run-parser.md` documents.

  The `resolve_reply` signature requires a real `ingestion_run_id` (non-Optional). For
  dry-run consumers (#99 stats) that do not have a real ingestion run, create a synthetic
  `IngestionRun` with `run_type='dry_run'` first. The resolver does not enforce `run_type`
  — it uses `ingestion_run_id` only to scope same_run lookups. The dry_run row gives the
  resolver a stable scope without writing real import data.

- **During #103 apply per-message persist:** as each export message is being
  staged for insert, `resolve_reply` (single id) or `resolve_reply_batch`
  (chunk) maps its `reply_to_message_id` so the persisted `chat_messages` row
  carries the correct internal pointer instead of the raw export id.

The resolver is intentionally side-effect-free so it can be called inside the
same transaction the apply path commits — see *Read-only invariant* below.

---

## API Surface

```python
async def resolve_reply(
    session: AsyncSession,
    export_msg_id: int,
    ingestion_run_id: int,
    *,
    chat_id: int,
) -> ReplyResolution

async def resolve_reply_batch(
    session: AsyncSession,
    export_msg_ids: list[int],
    ingestion_run_id: int,
    *,
    chat_id: int,
) -> dict[int, ReplyResolution]

def aggregate_resolutions(
    resolutions: dict[int, ReplyResolution],
) -> ReplyResolverStats
```

`ReplyResolution` is a `@dataclass(frozen=True)` with `export_msg_id`,
`chat_message_id` (None when unresolved), `resolved_via` (one of `same_run`,
`prior_run`, `live`, `unresolved`), and `chain_depth` (always 0 — direct-lookup
only). `ReplyResolverStats` aggregates a batch into per-bucket counts. Both are
frozen so consumers cannot mutate them by accident — important when the same
result dict travels through stats reporting and audit trails.

---

## Resolution Priority

The resolver tries four buckets in a fixed order and returns the first hit:

1. **same_run** — A `TelegramUpdate` row tagged `update_type='import_message'`
   with `ingestion_run_id == <current run>` and `(chat_id, message_id) ==
   (<chat>, <export_msg_id>)`, joined to its `chat_messages` row via
   `raw_update_id`. Why first: a reply target inside the same export is the
   common case; resolving it locally avoids cross-run lookups that would
   otherwise dominate query cost.
2. **prior_run** — Same join shape, but across any earlier import run for the
   same chat (filtered by `started_at < current_run.started_at`, tie-broken by
   `id DESC`). Why second: re-imports of
   the same chat onto an existing memory state are a real workflow (an
   operator extends an import after Telegram Desktop produced an updated
   export). Replies in the new run can legitimately point at messages from the
   prior run; if we skipped this step, all such replies would mis-classify as
   `unresolved`.
3. **live** — A `chat_messages` row matching `(chat_id, message_id)` whose
   `raw_update_id` points to a `TelegramUpdate` with `run_type='live'` (i.e.
   the message was ingested live, not via import). Why third: an import that
   runs after live ingestion has been recording the same chat may legitimately
   reply to a message that already exists from the live path — we want the
   pointer to land on the existing live row, not on a phantom duplicate.
4. **unresolved** — Returned with `chat_message_id=None`. Not an error: the
   export simply references an id we have no record of (target was deleted
   pre-export, or the export covers a partial window). Consumers count
   unresolved replies as a soft signal, not a parse failure.

The order is **safety-first**: same_run > prior_run > live ensures that if an
operator deliberately re-imports a chat to overwrite an older view, replies
inside that re-import bind to the re-import's rows, not to stale prior-run or
live rows.

---

## chat_id Scoping

Every lookup is scoped by `chat_id`. The resolver never matches across chat
boundaries, even if an export message id numerically collides with a message
id in a different chat. The rationale is governance: each chat has its own
membership envelope, its own visibility rules, and its own `#offrecord`
content. A reply pointer that crossed chats would silently leak the existence
of a message between scopes that the rest of the memory system treats as
isolated. This rule is enforced by the SQL: every internal lookup carries
`WHERE telegram_updates.chat_id = :chat_id` and is exercised by the
`chat_id` isolation test in the regression suite.

---

## Forward-Chain Handling — Direct-Lookup Semantics

`resolve_reply` and `resolve_reply_batch` perform a **direct lookup only**.
`chain_depth` on a direct resolution is always `0`. The resolver does not
automatically traverse `reply_to_message_id` hops on the resolved row.

This is a deliberate design choice. Reply chains in real Telegram exports are
shallow (a few hops at most), and the cost of a recursive walk grows with
chain length. More importantly, the two consumers want different things:

- #99 stats wants the **immediate** reply target — it counts whether the
  reply resolves at all, not how deep the thread goes.
- #103 apply wants the **immediate** target written to the row — deep thread
  reconstruction is a query-time concern (Phase 4+ q&a), not an apply-time
  concern.

Consumers that need deeper traversal iterate explicitly: call `resolve_reply`,
inspect the result, and call again on the resolved row's `reply_to_message_id`
if needed. Making this iteration explicit keeps the resolver's cost model
legible and avoids per-call recursion overhead.

---

## Read-Only Invariant

The resolver issues `SELECT` only. It does not `INSERT`, `UPDATE`, `DELETE`,
or `commit()`. This is enforced by inspection of the source — the module
imports `select` from sqlalchemy and exposes no write helpers — and asserted
implicitly by the test suite (every test runs against a populated session and
verifies row counts before and after).

The practical consequence: the resolver is safe to call inside any
transaction, including read-only ones, including transactions that another
caller intends to roll back. #103 apply can resolve replies in the same
transaction it later commits the new rows in, with no risk that the resolver
itself flushes or commits prematurely.

---

## Performance

The single-id path issues at most three queries (one per resolved-via bucket
that misses, terminating at the first hit or after `live`). The batch path
issues **at most four queries total** for any input size N — one same-run bulk
query, one prior-run bulk query, and up to two live bulk queries (first pass
for rows with a live `raw_update_id`, second pass for legacy `raw_update_id IS
NULL` rows). Each pass is restricted to the still-unresolved remainder. There
is no N+1: the common shape ("resolve all replies in this 5,000-message
export") completes in at most four round-trips regardless of N.

This contract is exercised by a dedicated batch test that asserts the query
count (≤ 4), so a future regression that reintroduces per-id lookup will fail
the test, not silently degrade apply throughput.

---

## Cross-References

| Reference | Relevance |
|-----------|-----------|
| `docs/memory-system/telegram-desktop-export-schema.md` (#91 / T2-NEW-A) | Defines the export `reply_to_message_id` field shape and the dangling-reply edge case the resolver returns `unresolved` for. |
| `docs/memory-system/import-user-mapping.md` (#93 / T2-NEW-B) | Sister policy doc — user mapping resolves `from_id`, this one resolves `reply_to_message_id`. Same chat_id scoping rationale. |
| `docs/memory-system/import-dry-run-parser.md` (#94 / T2-01) | Dry-run parser counts `dangling_reply_count`; #99 will replace that count with full resolver-backed stats consuming this service. |
| Issue #99 (T2-02 dry-run duplicate / policy stats) | First consumer. Calls `resolve_reply_batch` + `aggregate_resolutions` to produce the resolved-vs-unresolved breakdown for the dry-run report. |
| Issue #103 (T2-03 import apply) | Second consumer. Calls `resolve_reply` / `resolve_reply_batch` per message during persist so the stored `reply_to_message_id` column points at the correct internal id, not the raw export id. |
| `bot/db/models.py::TelegramUpdate`, `ChatMessage`, `IngestionRun` | Direct columns the resolver joins (`chat_id`, `message_id`, `ingestion_run_id`, `update_type`, `run_type`, `raw_update_id`) — no `raw_json` JSON operators required. |

---

## Out Of Scope / Non-Goals

- **Content-hash resolution.** This is not a "find the row whose `content_hash`
  matches" service. Identity here is `(chat_id, export_msg_id)`, not content.
  Content-hash dedup lives elsewhere (T1-08, T1-14).
- **Deep-thread walking.** The resolver answers "what is the immediate reply
  target?" — not "reconstruct the entire thread tree". Thread reconstruction
  is a Phase 4+ q&a concern that operates on already-persisted rows.
- **Privacy classification.** The resolver does not consult `memory_policy`,
  `is_redacted`, or `offrecord_marks`. Governance owns those fields; the
  resolver only returns the row id. Consumers that need to filter `#offrecord`
  targets do so after resolution.
- **Cross-chat resolution.** Explicitly forbidden by the chat_id scoping rule
  above.
- **Mutating the resolved row.** Read-only by contract.

<!-- updated-by-superflow:2026-04-28 -->
