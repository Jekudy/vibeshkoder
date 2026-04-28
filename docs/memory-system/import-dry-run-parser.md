# Import Dry-Run Parser

**Document:** T2-01 (issue #94) + T2-02 DB-aware extension (issue #99)
**Status:** implemented
**Date:** 2026-04-28
**Scope:** `bot/services/import_parser.py`, `bot/services/import_dry_run.py`,
`bot/cli.py` (`import_dry_run [--with-db]` subcommand)

---

## Purpose

Operators about to authorise an import apply run (#103) need a way to inspect a
Telegram Desktop export **before** any row is written to the DB. Without this,
the only feedback signal would come from the apply path itself — which is
write-side, irreversible (per the `#offrecord` irreversibility doctrine), and
therefore the wrong place to discover that an export is malformed, has the wrong
shape, or contains a surprising volume of `#offrecord` content.

The dry-run parser answers one question: *"If we apply this export, what will
the system see?"* — counts only, no content, no DB writes, no LLM calls. It
exists so the operator can sanity-check shape, scale, and policy distribution
before flipping the apply switch.

---

## When To Run

Always before authorising a #103 apply on a fresh export. The expected workflow
is:

1. Operator obtains a Telegram Desktop single-chat export (`result.json`).
2. Operator runs `python -m bot.cli import_dry_run <path>` and reviews the JSON
   report.
3. Operator confirms message counts match expectations, dangling-reply count is
   acceptable, `policy_marker_counts.offrecord` is non-surprising, and
   `parse_warnings` is empty (or warnings are understood).
4. Only then is the apply path (#103) invoked against the same file.

A dry-run that surfaces unexpected counts is a **STOP** signal — investigate
before applying.

---

## Inputs

The parser accepts **single-chat** Telegram Desktop exports only. A
single-chat export has a top-level envelope with `name`, `type`, `id`, and
`messages[]`. Full-account archives (with a top-level `chats:[]` list) are
rejected with a `ValueError` — they aggregate multiple chats and require a
different ingestion path that is out of scope for this cycle.

The reason for the single-chat constraint: each TD chat has its own `chat_id`,
its own membership/visibility envelope, and its own governance scope. Bundling
them under one apply run would conflate boundaries that the rest of the memory
system treats as independent. Forcing the operator to export one chat at a
time keeps the apply path's chat scope unambiguous.

The parser is tolerant of unknown optional fields and malformed individual
messages (warnings collected in `parse_warnings`), but hard-fails on missing
`messages[]`, unparseable JSON, or a full-account envelope.

---

## `ImportDryRunReport` Fields

The report is a `@dataclass(frozen=True)` returned by
`parse_export(path) -> ImportDryRunReport`. Every field answers a specific
operator question:

| Field | What it tells the operator |
|-------|----------------------------|
| `source_file` | Absolute path of the parsed export — for audit trail. |
| `chat_id` / `chat_name` / `chat_type` | Envelope identity — confirms operator imported the right chat. |
| `total_messages` | Sanity check: matches Telegram Desktop's UI count? |
| `user_messages` / `service_messages` | Split between actual content and join/leave/title-change events. |
| `media_count` | How many rows will have a media kind (photo/video/voice/etc.). |
| `distinct_users` / `distinct_export_user_ids` | How many ghost users will be created (worst case) per #93 user-mapping policy. |
| `date_range_start` / `date_range_end` | Time span covered — does this overlap live-ingestion window? |
| `reply_count` / `dangling_reply_count` | How many replies, and how many point to ids not in this export (#98 reply-resolver input). |
| `duplicate_export_msg_ids` | Repeated `id` values in `messages[]` — should be empty for a clean export. |
| `edited_message_count` | How many rows will get `imported_final=TRUE` with non-NULL `edit_date` per #106 policy. |
| `forward_count` | How many rows will be classified as `message_kind='forward'`. |
| `anonymous_channel_message_count` | How many rows will resolve to the anonymous-channel singleton ghost (#93 case 3). |
| `message_kind_counts` | Full breakdown by kind — taxonomy from `telegram-desktop-export-schema.md` §3. |
| `policy_marker_counts` | `{normal, nomem, offrecord}` distribution — operator-visible governance preview. |
| `parse_warnings` | Tolerant-reader soft-fails. Empty for clean exports; non-empty means investigate. |

---

## Governance Integration

For every user message (i.e. `type == "message"`, not `"service"`), the parser
calls `bot.services.governance.detect_policy(text, caption)` and increments
`policy_marker_counts[outcome]`. Service messages are skipped — they have no
authored content and no `#nomem` / `#offrecord` semantics.

This is the same `detect_policy` the live ingestion path and the future #103
apply path use. Running it during dry-run gives the operator the **exact same
governance verdict** the apply will produce — no drift between dry-run preview
and apply outcome.

For media messages, the TD `text` field is treated as a caption per
`telegram-desktop-export-schema.md` §3.1 and passed as the `caption` argument to
`detect_policy`, matching the semantics of live ingestion where captions are a
separate field.

---

## NO-Content Guarantee

**The report contains zero message bodies.** `asdict(report)` produces a JSON
blob with counts, ids (export message ids and `from_id` strings), kind labels,
warnings, and timestamps — but no `text`, `caption`, or other authored content.

This is a deliberate privacy property. Dry-run output is operator-visible (it
prints to stdout, may be pasted into chat, may be archived in audit logs). Any
content leak at this stage would defeat the `#offrecord` irreversibility
doctrine — content seen by a human can never be unseen, regardless of what the
apply path subsequently does with the row.

The parser enforces this by construction: `ImportDryRunReport` has no field
that holds message text. There is no escape hatch flag to "include content for
debugging". If a future ticket needs content-bearing dry-run output, it must
introduce a separate API and a separate authorisation gate — not extend this
report.

---

## Cross-References

| Reference | Relevance |
|-----------|-----------|
| `docs/memory-system/telegram-desktop-export-schema.md` (#91 / T2-NEW-A) | Envelope shape, `message_kind` taxonomy, mixed-array `text` form, service-vs-user split, anonymous channel posts. The parser implements the read side of this schema. |
| `docs/memory-system/import-user-mapping.md` (#93 / T2-NEW-B) | The user-mapping policy that the apply path will use. `distinct_users` and `anonymous_channel_message_count` preview ghost-creation volume. |
| `docs/memory-system/import-edit-history.md` (#106 / T2-NEW-H) | `edited_message_count` previews how many rows will receive `imported_final=TRUE` with non-NULL `edit_date` under the lossy-with-marker policy. |
| Issue #103 (T2-03) — import apply | The deferred write-side path. Dry-run is its mandatory pre-flight. |
| `bot/services/governance.py` (T1-12) | The shared `detect_policy` regex used identically by live ingestion, this parser, and #103. |
| `tests/fixtures/td_export/` (#91) | Anonymized fixtures — the parser's regression suite is built against them. |

---

## CLI Usage

```bash
python -m bot.cli import_dry_run /path/to/result.json
```

Prints the report as indented JSON to stdout. Exit codes:

- `0` — parse succeeded, report printed.
- `1` — parse failed (`ValueError` or unparseable JSON).
- `2` — file not found.

Datetime fields are serialised as ISO 8601 strings in CLI output. The Python
API (`parse_export(path)`) returns native `datetime` objects.

---

## DB-aware mode (`--with-db`)

The offline `parse_export(path)` answers questions about the export in isolation
— shape, kinds, governance distribution, dangling replies *within the export*.
It cannot answer questions that require the live DB: *"how many of these
messages already exist?"*, *"how many replies will fail to resolve against
prior runs / live ingestion?"*. Those are the questions an operator actually
needs answered before authorising apply.

DB-aware mode (T2-02 / #99) extends the report with three counts that depend
on the DB:

- `db_duplicate_count: int` — messages whose `(chat_id, export_msg_id)` already
  exists in `chat_messages` from a prior import or from live ingestion. Apply
  (#103) will skip these via the same lookup; dry-run surfaces the count
  ahead of time.
- `db_duplicate_export_msg_ids: tuple[int, ...]` — the actual export ids of the
  duplicates. Bounded by export size; carries no content. Useful for spot-check
  / audit trail.
- `db_broken_reply_count: int` — messages whose `reply_to.message_id` resolves
  to **unresolved** through the import reply-resolver priority chain
  (`same_run` → `prior_run` → `live`). These are reply chains that will land
  with `chat_messages.reply_to_message_id = NULL` under apply.

All three default to `0` / `()` on the offline path so the offline
`parse_export(path)` API and existing #94 callers stay byte-for-byte compatible
— no field is non-optional. The frozen-dataclass NO-content guarantee
(§ NO-Content Guarantee above) holds: the new fields carry counts and export
ids, never message text.

### When To Use

Run `--with-db` when the operator wants to know, before flipping the apply
switch:

1. *How many messages would be skipped as duplicates?* — re-import of an
   already-applied export should report close to 100 % duplicates; surprising
   low counts mean the operator is about to apply a different export than they
   think.
2. *How many reply chains would land broken?* — high `db_broken_reply_count`
   means most reply parents live outside the export and outside any prior
   imported run, which is usually a sign the operator should also import the
   referenced earlier chats first.

The offline `parse_export(path)` remains the right tool when the DB is not
available (CI, fixture validation, schema regression tests).

### API

```python
from bot.services.import_dry_run import parse_export_with_db

async with session_factory() as session:
    report = await parse_export_with_db(
        path="/path/to/result.json",
        session=session,
        chat_id=community_chat_id,  # operator-supplied target chat
    )
```

`parse_export_with_db` runs the offline `parse_export(path)` first (cheap, no
DB), then issues a small fixed number of DB queries to compute the three new
fields. It returns the same `ImportDryRunReport` dataclass with the DB fields
populated.

### Synthetic `dry_run` IngestionRun

Reply resolution is delegated to `bot.services.import_reply_resolver`
(#98 / T2-NEW-C), which scopes lookups by `ingestion_run_id`. The resolver
needs *some* run id — it is not designed to operate run-less. The dry-run
path therefore creates a **synthetic** `IngestionRun(run_type='dry_run')` row
(per the T1-02 check constraint that already lists `dry_run` as a legal
value) before invoking the resolver, then reuses that id as
`current_run.id`.

Rationale:

- It gives the resolver a stable, queryable scope without polluting
  `live` / `import` runs.
- The row is intentionally metadata-only — no `chat_messages` /
  `message_versions` / `telegram_updates` are written under it. The
  NO-content guarantee still holds for the DB-aware path.
- In the test suite the synthetic run is rolled back with the rest of the
  fixture transaction. In the CLI it is left in place as an audit row
  (when did the operator run a DB-aware dry-run, against which export, with
  what counts) — `stats_json` carries the report's count fields, `error_json`
  is NULL.
- It is **not** reused: every `parse_export_with_db` call creates a fresh
  `dry_run` row. They are cheap and chronologically interesting.

The synthetic row is **not** treated as a `prior_run` by subsequent dry-runs
or by #103 apply (the resolver's `prior_run` lookup filters on
`run_type IN ('live', 'import')` only, never `dry_run`).

### CLI Usage

```bash
python -m bot.cli import_dry_run --with-db /path/to/result.json
```

Without `--with-db`, the CLI runs the offline path (T2-01 default) and the new
fields are present in JSON output with their default values (`0` / `[]`).

Operator-facing summary printed to stderr after the JSON body:

```
N duplicates would be skipped, M offrecord, K nomem, J broken reply chains.
```

`N` = `db_duplicate_count`, `M` = `policy_marker_counts.offrecord`, `K` =
`policy_marker_counts.nomem`, `J` = `db_broken_reply_count`. Operators are
expected to read this line first; the full JSON is for audit / scripting.

Exit codes for `--with-db` are the same as for the offline path (`0` /
`1` / `2`); DB connectivity failure produces exit `1` with the underlying
error printed to stderr.

### Out-of-Scope (DB-aware mode)

- **No content writes.** `parse_export_with_db` does not write any
  `chat_messages` / `message_versions` / `telegram_updates`. The synthetic
  `dry_run` `IngestionRun` row is metadata-only — its existence is the
  exception that proves the rule.
- **No tombstone collision detection.** `db_duplicate_count` is a presence
  check against `chat_messages`, not against `forget_events`. Tombstone
  collisions for re-import (#100 / T2-NEW-D) are a separate report on top of
  this one.
- **No content-hash dedup.** Duplicate detection is keyed on
  `(chat_id, export_msg_id)` only, mirroring how #103 apply will dedup.
  Different content with the same id is already an upstream contradiction
  the operator must resolve before running apply.

---

## Out Of Scope / Non-Goals

- **Writing to the DB.** Dry-run is read-only by contract. All writes happen in
  #103.
- **Resolving export `from_id` to `users.id`.** The parser counts distinct
  `from_id` strings only. Actual ghost-user creation is the apply path's job
  (per #93 policy).
- **Resolving reply targets across chats.** `dangling_reply_count` is a count,
  not a resolution. The reply-resolver service (#98) handles cross-chat lookups
  during apply.
- **Full-account exports.** Rejected with `ValueError` — operator must export
  one chat at a time.
- **LLM-based content classification.** Governance is regex-based
  (`detect_policy`); no extraction, no Q&A, no catalog work happens here.
- **Tombstone collision detection.** Surfacing `forget_events` collisions for
  re-import is #100 (T2-NEW-D), built on top of dry-run output.
- **Apply-time stats (rows inserted, ghost users created, runs reused).** Those
  are produced by the apply path itself (#103). Pre-flight DB-aware dry-run
  stats (#99 / T2-02) are covered in the *DB-aware mode* section above.

<!-- updated-by-superflow:2026-04-28 -->
