# Import Apply — Synthetic Telegram Updates

**Ticket:** T2-03 / Issue #103
**Stream:** Delta (Phase 2 finale)
**Status:** implemented in #103 (Phase 2 finale).

---

## Module Overview

`bot/services/import_apply.py` applies a Telegram Desktop single-chat export to the
memory source-of-truth tables. It does not write directly to `chat_messages`.
Every persisted content row goes through the same helper used by live ingestion:
`persist_message_with_policy()`.

Public API:

```python
async def run_apply(
    session: AsyncSession,
    *,
    ingestion_run_id: int,
    resume_point: int | None,
    chunking_config: ChunkingConfig,
    export_path: str | None = None,
) -> ImportApplyReport
```

`ImportApplyReport` carries counts, ids, chunk metadata, and timestamps only. It has no
field for message text, caption, entities, or raw message bodies.

---

## Pipeline

Per export message, the apply loop follows this order:

1. **Checkpoint skip** — if `export_msg_id <= last_processed_export_msg_id`, skip.
2. **Tombstone gate** — per chunk, call `batch_check_tombstones_by_message_key(...)`.
3. **Duplicate gate** — lookup `chat_messages` by `(chat_id, message_id)`. If no
   `chat_messages` row exists, also lookup a prior synthetic import `telegram_updates`
   row so audit-only `offrecord` messages are idempotent.
4. **User mapping** — call `resolve_export_user(...)`; create ghost users if needed.
5. **Full tombstone check** — after user mapping and before any synthetic update, call
   `check_tombstone(chat_id, message_id, content_hash, user_tg_id)` so `user:{tg_id}`
   tombstones from `/forget_me` block all sender messages.
6. **Reply resolution** — read-only `resolve_reply_batch(...)` from #98. The resolver
   returns `chat_messages.id`; apply translates that PK to `chat_messages.message_id`.
   Unresolved replies are dropped (`None`).
7. **Synthetic telegram update** — insert `telegram_updates` with `update_id=NULL` and
   `ingestion_run_id=<this run>`.
8. **Governance call** — call `detect_policy(text, caption)` for the user message.
9. **Persist via helper** — for non-`offrecord` outcomes only, call
   `persist_message_with_policy(...)`.
10. **Edit-history row** — create `message_versions` with `imported_final=TRUE` for
    imported content. If `persist_message_with_policy(...)` returns a row whose
    `raw_update_id` differs from the synthetic update row id, a live row won the race;
    skip the version insert and increment `skipped_overlap_count`.
11. **Checkpoint advance** — once per chunk, `save_checkpoint(...)` deep-merges
    `last_processed_export_msg_id` inside the same transaction as the chunk writes.

Per-message pseudocode:

```python
if export_msg_id <= resume_point:
    skip_resume()
elif export_msg_id in tombstone_hits:
    skip_tombstone()
elif chat_message_exists(chat_id, export_msg_id):
    skip_duplicate()
elif import_audit_row_exists(chat_id, export_msg_id):
    skip_duplicate()
else:
    user_id = resolve_export_user(...)
    content_hash = compute_content_hash(...)
    if check_tombstone(..., content_hash=content_hash, user_tg_id=user_id):
        skip_tombstone()
        continue
    reply_to = resolve_reply_batch(...)
    raw = insert_synthetic_telegram_update(update_id=None, ingestion_run_id=run_id)
    policy, _ = detect_policy(text, caption)
    if policy == "offrecord":
        mark_raw_redacted(raw)
        skip_governance()
    else:
        saved = persist_message_with_policy(..., raw_update_id=raw.id, source="import")
        if saved.raw_update_id == raw.id:
            insert_version(saved.id, imported_final=True)
        else:
            skip_overlap()
```

Service messages are skipped per #94. They do not call `detect_policy`, do not create a
synthetic update, and do not create `chat_messages`.

---

## Idempotency Mechanisms

- **Tombstone gate:** message-key tombstones are blocked before user mapping, reply
  resolution, synthetic update, or content persistence. After user mapping, the full
  `check_tombstone(...)` call also blocks content-hash and `user:{tg_id}` tombstones
  before any synthetic update is written.
- **Duplicate gate:** existing `(chat_id, message_id)` rows skip before synthetic update.
  For audit-only `offrecord` messages, an existing synthetic import `telegram_updates`
  row is also a duplicate marker.
- **`source_hash` partial UNIQUE:** `ingestion_runs.source_hash` has a partial unique index
  for `status='running'`, preventing two running imports for the same export.
- **Checkpoint resume:** resumed runs skip messages at or below
  `last_processed_export_msg_id`.
- **`finalize_run`:** idempotent; repeated terminal finalization is a no-op for completed or
  cancelled runs.

---

## Governance Through The Same Path

ADR-0007 states that import apply uses the same normalization and governance path as live
Telegram updates. The binding implementation rule is:

> "Direct writes to `chat_messages` or `message_versions` are forbidden."

`persist_message_with_policy()` is the only write path to `chat_messages` for imported
content. It runs the live governance helper and preserves the sticky `offrecord` policy
semantics from Stream Alpha.

`offrecord` is stricter in apply: the synthetic `telegram_updates` audit row stays, but
`persist_message_with_policy()` is not called. No `chat_messages` or `message_versions`
content row is created for that export message. The report increments
`skipped_governance_count`.

`nomem` still persists through the helper with `memory_policy='nomem'`; downstream search,
q&a, extraction, digest, catalog, wiki, and graph layers must filter it out.

---

## Edit History (#106)

Telegram Desktop exports contain only the final snapshot of edited messages. Migration
`018_add_message_versions_imported_final.py` adds:

```sql
message_versions.imported_final BOOLEAN NOT NULL DEFAULT FALSE
```

Every `message_versions` row created by import apply sets `imported_final=TRUE`, not only
rows whose export object has `edited_unixtime`.

Overlap rule:

- If no live row exists, import creates the version row.
- If a live `chat_messages` row already exists for `(chat_id, message_id)`, import skips;
  live provenance wins.
- If a live row wins the race between the duplicate gate and the helper upsert, the helper
  returns a row whose `raw_update_id` is not the synthetic raw id created by this apply
  message. Apply leaves the synthetic audit row, increments `skipped_overlap_count`, and
  skips `MessageVersionRepo.insert_version(...)`.
- If an imported row later receives a live edit, the later live version keeps
  `imported_final=FALSE`.

When `edited_unixtime` is present, apply stores it in `message_versions.edit_date`.

---

## Checkpoint / Resume (#101)

The checkpoint is stored in `ingestion_runs.stats_json`:

```json
{
  "last_processed_export_msg_id": 1234,
  "chunk_index": 5,
  "last_checkpoint_at": "2026-04-28T12:34:56+00:00"
}
```

Apply advances the checkpoint once per chunk, never per message. The checkpoint update runs
inside the same outer transaction as that chunk's message writes, then the chunk commits
once. The update uses the #101 deep-merge shape:

```sql
UPDATE ingestion_runs
   SET stats_json = COALESCE(stats_json::jsonb, '{}'::jsonb) || CAST(:patch AS jsonb)
 WHERE id = :id
```

Resume reads `last_processed_export_msg_id` and skips all export messages at or below that
id. Because chunk data and checkpoint commit together, a rollback exposes neither the
chunk's rows nor its checkpoint advance.

---

## Chunking + Rate Limit + Advisory Lock (#102)

Configuration comes from `ChunkingConfig`:

| Env var | Default | Meaning |
|---|---:|---|
| `IMPORT_APPLY_CHUNK_SIZE` | `500` | messages per chunk |
| `IMPORT_APPLY_SLEEP_MS` | `100` | sleep between chunks |
| `IMPORT_APPLY_ADVISORY_LOCK` | `true` | acquire per-run advisory lock |

`--chunk-size` overrides only `IMPORT_APPLY_CHUNK_SIZE`. No CLI flags exist for sleep or
advisory-lock settings.

When `use_advisory_lock=True`, `run_apply` calls
`acquire_advisory_lock(connection, ingestion_run_id)` and holds one `AsyncConnection` for
the full apply lifetime. If the caller's session is engine-bound, apply explicitly opens
`engine.connect()` and creates a fresh `AsyncSession(bind=connection)` for all chunk work.
If the caller already supplied a connection-bound session (test fixture / explicit caller),
apply reuses that connection. PostgreSQL advisory locks are connection-scoped and stacked,
so the caller must not re-enter the same lock on the same connection.

---

## Error Policy

Per-message expected validation/mapping failures are caught as specific types
(`ValueError`, `RuntimeError`) inside a per-message SAVEPOINT, logged with:

- `export_msg_id`
- `chat_id`
- `ingestion_run_id`
- `error_type`

No message text, captions, entities, or raw bodies are logged.

Per-message failures roll back that message's SAVEPOINT, increment `error_count`, append
the export id to the capped `error_export_msg_ids` list, and continue the chunk.

`SQLAlchemyError` is not swallowed at per-message level. It aborts the current chunk,
rolls back the outer chunk transaction, restores in-memory report counters to the last
committed chunk, and propagates to the CLI.

Per-chunk infrastructure errors are not swallowed. They propagate to the CLI, which rolls
back the active session, best-effort persists partial apply stats from the attached report,
finalizes the ingestion run as `failed`, sets `finished_at`, and returns exit code `5`.

Cancellation handling: `asyncio.CancelledError` and other `BaseException` subclasses enter
the same failed-finalization path and are then re-raised. A hard process kill before Python
cleanup can still leave `ingestion_runs.status='running'`; the partial-present check at the
next startup classifies that run and requires `--resume` or manual finalization.

No fallback defaults are invented for required state. Missing `chat_id`, missing run rows,
or unreadable export files fail fast.

---

## CLI Integration

Feature flag gate:

```text
memory.import.apply.enabled
```

Default is OFF. When disabled, the CLI prints `import apply disabled` and exits `0`
without creating an ingestion run.

Current command shape:

```bash
python -m bot.cli import_apply /path/to/result.json --chunk-size N
python -m bot.cli import_apply /path/to/result.json --resume
```

There is no `--export-path` flag; `export_path` is the required positional argument. This
preserves the #101/#102 CLI surface and avoids adding new flags in #103.

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | success, or feature flag disabled |
| 2 | usage / unreadable file / invalid chunking config |
| 3 | partial-present block; use `--resume` or finalize prior run |
| 5 | governance/runtime/apply error |

---

## Audit Trail

`ingestion_runs` records:

- `run_type='import'`
- `source_name`
- `source_hash`
- `status`
- `config_json.chat_id`
- `stats_json` checkpoints and final counts
- `error_json` for failed runs
- `started_at` / `finished_at`

Synthetic `telegram_updates` rows record `update_type='import_message'`,
`update_id=NULL`, `chat_id`, `message_id`, and `ingestion_run_id`.

## Rollback (#104)

Logical rollback uses the synthetic raw rows as the only ownership anchor:

```sql
chat_messages.raw_update_id -> telegram_updates.id
telegram_updates.ingestion_run_id = <target>
telegram_updates.update_id IS NULL
```

See `docs/memory-system/import-rollback.md` for the operator command, idempotency,
transactionality, audit-row contract, and the Phase 4+ downstream-dependent TODO.

---

## NO-Content Invariant

`ImportApplyReport.asdict()` carries zero message content by construction. Its fields are
counts, ids, timestamps, chunking config, source path, and run metadata. It does not carry:

- message text
- captions
- entities
- `raw_json` bodies
- quoted/replied-to content

The synthetic raw payload is also allowlisted to metadata fields; content-bearing TD export
fields are not copied into `telegram_updates.raw_json`.

---

## Cross-Refs

- **#89** — `persist_message_with_policy()` is the only non-offrecord `chat_messages` writer.
- **#91** — Telegram Desktop export schema, message kinds, edit/reply/user field shapes.
- **#93** — user mapping and ghost-user policy via `resolve_export_user`.
- **#94** — dry-run parser; service messages skipped, governance preview semantics.
- **#97** — tombstone helper and reimport prevention gate.
- **#98** — read-only reply resolver consumed by apply.
- **#99** — DB-aware dry-run duplicate/reply stats that preview apply outcomes.
- **#100** — tombstone collision stats; tombstone wins over duplicate in operator preview.
- **#101** — checkpoint/resume, source hash, partial-present CLI exit code.
- **#102** — chunking config, sleep, advisory lock.
- **#106** — `imported_final=TRUE` edit-history policy and migration 018.
- **#104** — logical rollback by `ingestion_run_id`.

---

## Out Of Scope

- Media file copying or storage-layer attachment import.
- Full-account Telegram exports; apply accepts one chat export at a time.
- LLM calls, extraction, q&a, catalog, wiki, graph, digest, or public surfaces.
- Reconstructing edit history before Telegram Desktop export time.
- Adding new CLI flags beyond `--resume` and `--chunk-size`.
- Per-user and content-hash tombstone expansion in the chunk-level batch gate.
