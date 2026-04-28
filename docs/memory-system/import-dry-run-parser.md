# Import Dry-Run Parser

**Document:** T2-01 (issue #94)
**Status:** implemented
**Date:** 2026-04-28
**Scope:** `bot/services/import_parser.py`, `bot/cli.py` (`import_dry_run` subcommand)

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
- **Apply-stats reporting.** Detailed apply-time stats (rows inserted, ghost
  users created, runs reused) are #99 (T2-02) territory.

<!-- updated-by-superflow:2026-04-28 -->
