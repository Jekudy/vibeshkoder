# Import Checkpoint / Resume Infrastructure

**Ticket:** T2-NEW-E / Issue #101
**Stream:** Bravo (Sprint 06)
**Status:** Implemented — infrastructure only. Apply path lives in Stream Delta (#103).

---

## Overview

This document describes the checkpoint/resume infrastructure for Telegram Desktop import
(`import_apply`). It covers:

1. Checkpoint contract (what is stored, where, how)
2. Resume decision matrix (`init_or_resume_run` semantics)
3. `source_hash` partial unique index (race-condition safety)
4. Cross-stream boundary (infrastructure vs. apply path)
5. CLI usage
6. Deferred integration test rationale

---

## Checkpoint Contract

Checkpoints are stored in `ingestion_runs.stats_json` using deep-merge semantics.
After each chunk the apply path (#103) calls `save_checkpoint()`, which atomically
updates these keys while preserving all other keys in `stats_json`:

```json
{
  "last_processed_export_msg_id": 1234,
  "chunk_index": 5,
  "last_checkpoint_at": "2026-04-28T12:34:56.789012+00:00"
}
```

Deep-merge means operator-set fields (e.g. `"operator_note": "..."`) survive across
checkpoint updates. The merge is performed in a single atomic `UPDATE ... SET stats_json
= COALESCE(stats_json, '{}') || :patch::jsonb` — no race between read-modify-write.

### `Checkpoint` dataclass

```python
@dataclass(frozen=True)
class Checkpoint:
    ingestion_run_id: int
    source_path: str             # source_name from the run row
    last_processed_export_msg_id: int | None   # None if no checkpoint written yet
    chunk_index: int             # 0-based
    started_at: datetime
    last_updated_at: datetime    # last_checkpoint_at, or started_at if no checkpoint yet
    status: Literal["running", "completed", "failed", "cancelled"]
```

---

## Resume Decision Matrix

`init_or_resume_run(session, *, source_path, source_hash, chat_id, resume)` returns a
`ResumeDecision` with `mode`, `ingestion_run_id`, `last_processed_export_msg_id`, and
`reason`.

| State on disk | `--resume` passed? | Decision |
|---|---|---|
| No prior run for this `source_hash` | yes or no | `start_fresh` — create new run, checkpoint=None |
| Prior run, `status='completed'` | yes or no | `start_fresh` — completed runs are immutable; log prior run id |
| Prior run, `status='running'\|'failed'` | no | `block_partial_present` — CLI exits 3; do NOT touch the partial run |
| Prior run, `status='running'\|'failed'` | yes | `resume_existing` — return existing `run_id` + `last_processed_export_msg_id` |
| Prior run with **different** `source_hash`, non-terminal | yes | `block_partial_present` with "source hash mismatch" reason |

### Crash recovery

Crash between `init_or_resume_run` and first `save_checkpoint` (no checkpoint written):
- Resume sees `last_processed_export_msg_id=None` → apply restarts from message 0.
- This is correct: no data was written, restart is safe.

---

## `source_hash` Partial Unique Index

Migration 016 adds:

```sql
-- Column (nullable for live/dry_run rows)
ALTER TABLE ingestion_runs ADD COLUMN source_hash VARCHAR(128);

-- Partial unique index: at most one RUNNING import per source_hash
CREATE UNIQUE INDEX ix_ingestion_runs_source_hash_running
    ON ingestion_runs (source_hash)
    WHERE status = 'running';
```

This prevents two concurrent `import_apply` invocations from creating duplicate partial
runs for the same export file. Race scenario:

1. Process A and B both call `init_or_resume_run` — both see no prior run.
2. Process A flushes first → row inserted, index entry created.
3. Process B flushes → `IntegrityError` on the partial unique index.
4. Process B catches `IntegrityError`, rolls back, re-queries, and returns
   `block_partial_present` to the CLI caller.

Only `status='running'` rows participate in the uniqueness constraint.
Completed/failed rows for the same `source_hash` are allowed (re-import case).

---

## Cross-Stream Boundary

This ticket (#101) implements the **checkpoint/resume infrastructure only**.

| File | Owner | Status |
|---|---|---|
| `bot/services/import_checkpoint.py` | #101 (this ticket) | Done |
| `alembic/versions/016_*.py` | #101 (this ticket) | Done |
| `bot/cli.py` `import_apply` subcommand | #101 (this ticket) | Done (lazy-imports apply path) |
| `bot/services/import_apply.py` | #103 (Stream Delta) | NOT YET CREATED |

The CLI imports `bot.services.import_apply.run_apply` **lazily** inside
`_cmd_import_apply_async`. If the import fails (`ImportError`), the CLI prints:

```
import_apply not yet implemented (#103) — checkpoint/resume infrastructure (#101) is ready.
```

and exits with code 4. This allows operators to test the checkpoint infrastructure
(`import_apply --dry-run` style) before the apply path lands.

**DO NOT** create `bot/services/import_apply.py` in this ticket. It belongs to #103.

---

## CLI Usage

```bash
# Start fresh import (will fail with exit 4 until #103 lands)
python -m bot.cli import_apply /path/to/result.json

# Resume a partial run
python -m bot.cli import_apply /path/to/result.json --resume

# Custom chunk size
python -m bot.cli import_apply /path/to/result.json --chunk-size 200
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Apply completed successfully |
| 2 | File not found or unreadable |
| 3 | Partial run found and `--resume` not passed (use `--resume` or finalize the prior run) |
| 4 | Apply path not yet implemented (#103); checkpoint infrastructure is ready |

---

## Deferred Integration Test Rationale

Issue #101's acceptance criterion:

> Integration test: kill apply at 50%, resume, end state is identical to apply-without-failure

This cannot be tested directly because the apply path (`bot.services.import_apply`) does
not exist yet — it lives in Stream Delta (#103).

**Substitution:** `test_checkpoint_simulated_kill_and_resume` (test 13 in
`tests/services/test_import_checkpoint.py`) is the strongest infrastructure proxy:

1. Calls `init_or_resume_run` to create a fresh run.
2. Calls `save_checkpoint` with `last_processed_export_msg_id=50` (simulates apply running).
3. Raises `RuntimeError("simulated kill at 50%")` — simulates process death.
4. Calls `finalize_run(status='failed', ...)` in the except block.
5. Asserts `load_checkpoint` returns `last_processed_export_msg_id=50`.
6. Calls `init_or_resume_run(..., resume=True)` and asserts `resume_existing` + `last_processed_export_msg_id=50`.

The "identical end state" half of the requirement is verifiable only when #103 lands and
provides a real apply path. At that point, stream Delta should add an integration test
that:
- Runs a full apply, kills it at 50%.
- Resumes, completes.
- Verifies `chat_messages` row count equals that of a non-interrupted apply.

---

## Operator Playbook

### Cancelling a stuck partial run

If a partial run cannot be resumed (file changed, host migration, operator decision):

```sql
UPDATE ingestion_runs
   SET status='cancelled',
       finished_at=NOW(),
       error_json='{"reason": "operator cancelled"}'::jsonb
 WHERE id = <run_id>
   AND status IN ('running', 'failed');
```

A future ticket will expose this as `python -m bot.cli import_cancel <run_id>`.

### Identifying stuck running runs

```sql
SELECT id, source_name, started_at, NOW() - started_at AS age
  FROM ingestion_runs
 WHERE run_type = 'import' AND status = 'running'
 ORDER BY started_at;
```

If `age` exceeds reasonable apply duration (e.g., > 24h), the run is likely abandoned.

---

## Resume Race Contract for #103

Apply path (#103) MUST atomically flip `status='running'` as its first DB op on resume;
the partial unique index then enforces single-resumer semantics. Without this, two
concurrent `--resume` invocations against the same failed run would both proceed.

---

## Operational Follow-ups

The following are tracked as documentation-only (no immediate code change required):

- **Streaming `source_hash`**: currently reads the full file in 1 MB chunks (safe for most
  exports). True streaming for multi-GB files via `ijson` is a future optimisation.
- **`source_name` index**: `_find_partial_run_by_path` uses a full table scan on
  `source_name`. Add `CREATE INDEX ix_ingestion_runs_source_name` if the table grows large.
- **True IntegrityError-branch test**: `test_create_fresh_run_integrity_error_branch` mocks
  `session.begin_nested`. A real concurrent-insert test requires two concurrent DB connections
  and is deferred to a follow-up ticket.
- **Resume race enforcement in #103**: Stream Delta must enforce the contract documented in
  the "Resume Race Contract" section above.
- **Integration test "kill at 50%, resume, identical state"**: to be added in #103 once the
  apply path exists and can be tested end-to-end.

---

## See Also

- #93 / `import-user-mapping.md` — Telegram Desktop user mapping (ghost users, anonymous channels).
- #94 / `import-dry-run-parser.md` — Dry-run pre-flight before apply.
- #103 — Telegram Desktop import apply (Stream Delta, consumer of this checkpoint API).
- #106 / `import-edit-history.md` — Edit history during import.

---

## API Reference

See `bot/services/import_checkpoint.py` for full docstrings.

```python
async def init_or_resume_run(
    session, *, source_path, source_hash, chat_id, resume
) -> ResumeDecision: ...

async def save_checkpoint(
    session, *, ingestion_run_id, last_processed_export_msg_id, chunk_index
) -> None: ...

async def load_checkpoint(
    session, ingestion_run_id
) -> Checkpoint | None: ...

async def finalize_run(
    session, *, ingestion_run_id, final_status, error_payload=None
) -> None: ...
```
