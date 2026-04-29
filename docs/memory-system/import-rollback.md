# Import Rollback — Logical Undo By ingestion_run_id

**Ticket:** T2-NEW-G / Issue #104
**Status:** implemented.

---

## What

`rollback_ingestion_run` is the emergency net for a bad Telegram Desktop import apply run.
It deletes import-owned source rows and normalized rows for one `ingestion_runs.id`, then
records a separate audit run with `run_type='rolled_back'`.

The operator command is:

```bash
python -m bot.cli rollback_ingestion_run <ingestion_run_id>
```

Successful output reports counts only:

```text
rollback_ingestion_run 123
chat_messages_deleted: 5
telegram_updates_deleted: 5
message_versions_cascade_deleted: 5
audit_run_id: 124
```

If the run was already rolled back, the command exits `0` and prints
`(idempotent no-op)` with the existing audit row id.

---

## Why

Import apply writes historical messages through synthetic `telegram_updates` rows:

- `telegram_updates.update_id IS NULL`
- `telegram_updates.ingestion_run_id = <import run id>`
- `chat_messages.raw_update_id -> telegram_updates.id`
- `message_versions.raw_update_id -> telegram_updates.id`

That gives operators a narrow, auditable rollback selector before Phase 4+ derived layers
exist.

---

## Selector Contract

Rollback selects imported content through this FK chain only:

```sql
chat_messages.raw_update_id
  -> telegram_updates.id
  -> telegram_updates.ingestion_run_id = <target>
```

The synthetic guard is mandatory:

```sql
telegram_updates.update_id IS NULL
```

Rollback must never select by `chat_id` alone. A live row in the same chat survives because
live `telegram_updates` rows have real Telegram `update_id` values and do not match the
synthetic selector.

---

## Transactionality

The service performs all deletes and the audit insert in one database transaction:

1. count import-owned `chat_messages`
2. count import-owned `message_versions`
3. delete import-owned `chat_messages`
4. let `message_versions` delete via `ON DELETE CASCADE`
5. delete synthetic `telegram_updates`
6. insert `ingestion_runs(run_type='rolled_back', status='completed', stats_json=...)`
7. commit

If any step fails, the transaction rolls back. Partial rollback is not allowed.

---

## Idempotency

Before deleting, the service checks under the per-run advisory lock:

```sql
ingestion_runs.run_type = 'rolled_back'
AND stats_json->>'original_run_id' = <target>
```

If an audit row already exists, rollback returns a zero-delete report and does not create
a second audit row. Migration `019_add_ingestion_runs_rolled_back.py` adds the
`rolled_back` run type and a unique partial index for this audit key.

---

## NO-Content Guarantee

Rollback logs and returns only:

- run ids
- row counts
- audit run id

It does not log message text, captions, entities, raw JSON bodies, usernames, or exported
message bodies.

---

## What Rollback Does Not Touch

Rollback does not delete or alter:

- live `telegram_updates` (`update_id IS NOT NULL`)
- live `chat_messages` or live `message_versions` in the same chat
- `forget_events` tombstones
- users / ghost users created during import
- Phase 4+ derived rows

Tombstones are durable governance records and are not casually rolled back.

---

## Downstream Dependents

The service has a placeholder:

```python
_check_no_downstream_dependents(connection, ingestion_run_id) -> None
```

Today it passes through because Phase 4+ derived tables do not exist. When extracted facts,
search rows, evidence bundles, cards, summaries, graph rows, or wiki projections land, this
function must fail closed if any derived row references the import run.

---

## Exit Codes

| Code | Meaning |
|---:|---|
| 0 | success or idempotent no-op |
| 2 | run exists but is not an import run |
| 3 | ingestion run not found |
| 4 | downstream dependents exist |

---

## See Also

- `import-apply.md` — synthetic `telegram_updates` layout and import apply pipeline.
- `import-checkpoint.md` — `ingestion_runs.stats_json` and status lifecycle.
- `import-edit-history.md` — `message_versions.imported_final=TRUE` provenance marker.
- `AUTHORIZED_SCOPE.md` — governance and tombstone invariants.
