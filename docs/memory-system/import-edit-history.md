# Import Edit History Policy

**Document:** T2-NEW-H (issue #106)
**Status:** decision (gates #103 / Stream Delta implementation)
**Date:** 2026-04-27
**Scope:** docs only — no production code in this sprint

This document records the policy decision for how Telegram Desktop export messages that
were edited before export are represented in the import path. It does not implement
anything; implementation lands in #103 (Stream Delta).

---

## Table of Contents

1. [Purpose](#1-purpose)
2. [Background — what each side has](#2-background--what-each-side-has)
3. [The three options](#3-the-three-options)
4. [Recommendation](#4-recommendation)
5. [Implementation surface (for #103 Stream Delta)](#5-implementation-surface-for-103-stream-delta)
6. [Honesty about loss](#6-honesty-about-loss)
7. [Cross-references](#7-cross-references)
8. [Out of scope for this doc](#8-out-of-scope-for-this-doc)

---

## 1. Purpose

This document exists because Telegram Desktop export and live ingestion tell fundamentally
different stories about the edit history of a message, and the import apply path (#103,
Stream Delta) needs an explicit, reviewed policy for how to handle the gap.

### The structural problem

Telegram Desktop (TD) export stores **only the latest version** of a message at export
time. If a message was edited five times, the export contains only the text as it stood
when the operator clicked "Export". The `edited` / `edited_unixtime` fields confirm that
an edit happened, but provide no record of what the original text was, how many times the
message changed, or what intermediate states looked like.

Live ingestion, by contrast, captures every CONTENT-CHANGING edit (for a known prior
message) as a separate `message_versions` row via the `edited_message` Telegram Bot API
update (handler T1-14); edits with an unchanged hash or an unknown prior message are
skipped — see §2 detailed semantics. The result is a faithful chain: `version_seq=1`
for the original, `version_seq=2` for the first content-changing edit, and so on.

When an operator imports a TD export into a community that also has live ingestion, the
system needs a coherent policy for what an imported edited message looks like in the DB —
and what downstream consumers (search, q&a, catalog — all currently gated by feature
flags) are allowed to infer from it.

---

## 2. Background — what each side has

### Live ingestion (T1-14)

Each content-changing `edited_message` Telegram update for a known prior message produces:

1. A lookup of the existing `chat_messages` row by `(chat_id, message_id)`.
2. A new content hash computed via `compute_content_hash` (chv1 recipe, T1-08).
3. If the hash changed: a new `message_versions` row with
   `version_seq = max_existing + 1`.
4. The `chat_messages.current_version_id` pointer advanced to the new row.

The original message text arrives as a regular `message` update (not `edited_message`),
so it always produces the first `message_versions` row with `version_seq=1`. Edit
history is lossless from the moment the bot is running.

Edits with an unchanged `content_hash` are no-ops (the existing row is returned by
`insert_version` without creating a duplicate). Edits for messages whose
`chat_messages` row is not yet found (unknown prior message) are logged and skipped
(see `bot/handlers/edited_message.py`).

The `edit_date` field from the Telegram API is stored in `message_versions.edit_date`
(DateTime, nullable). For `version_seq=1` (the original), `edit_date` is NULL because
the message was not yet edited.

### TD export

A TD export contains, for a message that was edited:

```json
{
  "id": 2001,
  "type": "message",
  "date": "2024-02-10T09:00:00",
  "date_unixtime": "1707555600",
  "from": "User One",
  "from_id": "user1000001",
  "text": "Project update: we shipped the feature on Friday.",
  "edited": "2024-02-10T09:45:00",
  "edited_unixtime": "1707558300"
}
```

There is no `original_text`, no `v1`, no edit chain. There is only the final state plus a
timestamp confirming an edit occurred. The original text is unrecoverable from the export.

The TD export schema is described in full in
`docs/memory-system/telegram-desktop-export-schema.md` (T2-NEW-A, issue #91). Section 4
("Edit history representation") establishes this structural fact: TD export is a snapshot
of the latest state, not a full history. This document elaborates the policy choice that
follows from that constraint.

### Fixture evidence

The fixture `tests/fixtures/td_export/edited_messages.json` (shipped in #91) contains
5 messages, 2 of which have an `edited` field set. In real community exports a non-trivial fraction of messages have `edited` set
(anecdotally observed; not measured in this repository).

---

## 3. The three options

### Option A — Lossy import with marker

**What it stores:**

- One `chat_messages` row (the message envelope).
- One `message_versions` row with `version_seq=1` (conditional on no prior live row for
  the same `(chat_id, message_id)` — see §5 step 5 for the overlap rule).
- A new boolean flag `imported_final=TRUE` on the `message_versions` row.
- `edit_date` populated from `edited_unixtime` if the TD message has it.

**What it loses:**

- All intermediate states of the message text (unrecoverable).
- The number of edits.
- The reason for each edit.
- The original text at time of first posting.

**Downstream implications:**

- Q&a, search, and catalog (all feature-flag gated) see `version_seq=1` with
  `imported_final=TRUE`. They can distinguish this row from a live-ingested v1 row.
- The `edit_date` field on the version row tells them "this content was last edited at T"
  even though the chain before T is missing.
- Citation surfaces (Phase 4+) can add a caveat: "imported snapshot — full edit history
  unavailable."
- Schema cost: 1 new boolean column on `message_versions` (default FALSE, server default
  'false').

**Assessment:** Honest. Explicit about uncertainty. Minimal schema cost.

### Option B — Best-effort v1 with marker

**What it stores:** Identical to Option A. A single `message_versions` row,
`version_seq=1` (conditional on no prior live row — see §5 step 5), `imported_final=TRUE`.

**What it loses:** Identical to Option A.

The label "best-effort v1" suggests we are doing our best to reconstruct the original,
but because TD export provides no reconstruction data — only the final snapshot — Options
A and B collapse into the same concrete implementation. There is no mechanism in the TD
export format to "synthesize" a v1 that differs from the final state. Both options
produce the same DB row.

**Assessment:** Options A and B are functionally identical. The document retains both
labels for traceability (they appeared in the acceptance criteria) but recommends treating
them as one approach.

### Option C — Refuse to import edited messages

**What it stores:** Nothing. Any TD message with an `edited` field set is skipped.

**What it loses:** Substantial content. In the test fixture, 2 of 5 messages (40%) have
`edited` set.

**Assessment:** Overly restrictive and not useful. Edited messages are semantically
equivalent to other messages once we have their final text — we lose no more accuracy by
importing them than by refusing them. In real community exports a non-trivial fraction of
messages have `edited` set (anecdotally observed; not measured in this repository).
Option C is rejected.

---

## 4. Recommendation

**Adopt Options A/B as a single approach.** Import every user-authored TD message
(service messages are skipped per `telegram-desktop-export-schema.md` §3) as a new
`message_versions` row using `MessageVersionRepo.insert_version` (which assigns
`version_seq = max(existing version_seq) + 1` per chat_message; for a fresh
chat_message that means `version_seq=1`). When a live row already exists for the same
`(chat_id, message_id)`, the overlap rule in §5 applies — import skips. Every row
created by the import gets a boolean marker `imported_final=TRUE` on the
`message_versions` row.

### Where to place the marker

The marker belongs on **`message_versions`**, not on `chat_messages`.

`chat_messages` is the message envelope: it records that a message with a given
`(chat_id, message_id)` exists, who sent it, and when. It is a structural fact about the
message, not about any specific content snapshot.

`message_versions` rows represent content snapshots — a specific state of the text at a
specific moment. The `imported_final` flag means "this content snapshot was captured from
a static export at an unknown point relative to the original message's posting time."
That is a property of the content snapshot, not of the message envelope.

Placing the flag on `chat_messages` would be conceptually wrong: a message that is
imported and then later receives a live edit (if the import precedes the live bot covering
the same time range) would have future `message_versions` rows that are NOT imported
snapshots. The flag on the version row correctly scopes the uncertainty to the specific
snapshot it describes.

### Column definition

```
message_versions.imported_final  Boolean  NOT NULL  DEFAULT FALSE  SERVER DEFAULT 'false'
```

Only the import apply path (#103) sets this to TRUE. All live-ingested rows have
`imported_final=FALSE` (the server default ensures this for existing rows without a
migration). The column requires one Alembic migration with `server_default='false'` so
existing live-ingested rows are populated automatically.

### What `imported_final=TRUE` means for downstream consumers

The flag means: "this content came from a static archive; the real edit history is
unrecoverable." Downstream consumers MUST treat such rows the same as live v1 rows for
retrieval (they carry real, valid content — the final version of the message). However,
they MAY surface a caveat in citation contexts.

The policy for surfacing this caveat ("imported snapshot — full edit history unavailable")
is a Phase 4 decision and is out of scope here. The flag is the mechanism; the UX is
deferred.

### Governance is unaffected

The `imported_final` marker does NOT change governance behavior. Every imported message
— regardless of whether it has `edited` set or `imported_final=TRUE` — still routes
through `bot/services/governance.py::detect_policy` exactly as live messages do.

This is a binding cross-cutting rule from `AUTHORIZED_SCOPE.md` (Critical safety rule for
`#offrecord`). See also the "Telegram import rule" in `AUTHORIZED_SCOPE.md` and the
governance section of `telegram-desktop-export-schema.md` §8.

If a message with `imported_final=TRUE` contains `#offrecord` in its final text, the
apply path must redact it in the same transaction, exactly as it would for a live message.
The `imported_final` flag has no bearing on this path.

### Why not derive provenance from the FK chain instead?

An alternative to the `imported_final` Boolean is to derive provenance at read time via
the existing FK chain: `message_versions.raw_update_id → telegram_updates.ingestion_run_id
→ ingestion_runs.run_type`. This alternative is rejected for four reasons:

1. **Query cost**: every read-side query that needs to filter `message_versions` by
   provenance (search, q&a, citations, audit) would require a JOIN to `telegram_updates`
   and `ingestion_runs`. On hot read paths this is a measurable performance penalty.
2. **Consumer model**: downstream consumers (q&a, search, citation surfaces) treat
   provenance as a row-level attribute of the version — not as a chain-derived property.
   A Boolean column maps directly to this mental model; the FK chain does not.
3. **Write cost**: the Boolean denormalisation is a single column set at insert time. It
   adds no ongoing cost relative to rows that would already be written with `raw_update_id`.
4. **Audit trail preserved**: the FK chain (`raw_update_id → ingestion_run_id →
   run_type`) is retained on the row as the AUDIT trail of last resort. If `imported_final`
   ever drifts from the chain, the chain is the cross-check — #103 is responsible for
   keeping them consistent on every write. In normal operation they never disagree.

### `imported_final=TRUE` for all imported rows, not just edited ones

There is a choice: set `imported_final=TRUE` only for rows where `edited` is present in
the TD export, or for ALL imported rows.

**Recommendation: set `imported_final=TRUE` for ALL imported user-authored messages that
create a `message_versions` row.** (Service messages are skipped by the parser per
`docs/memory-system/telegram-desktop-export-schema.md` §3 service row — they produce no
`message_versions` row at all.) The reason is that the flag means "constructed from a
static archive without live edit-chain knowledge" — and that is true of every imported
user-authored row, not just the ones with `edited` set. Even a message that was never
edited in TD export is still a snapshot; we do not have the live ingestion history for it.
The flag is about provenance (archive vs. live), not about whether the message was ever
edited.

If downstream needs to know "was this message ever edited before the export?", that is a
separate signal. The implementation SHOULD preserve the `edited` field presence as a
separate boolean, e.g. `imported_was_edited`, on the same `message_versions` row. This
is out of scope for this ticket but is documented as future work in §8.

Invariant: `imported_final=TRUE` denormalises provenance for query efficiency and audit
clarity. The `imported_final` Boolean is the OPERATIONAL source of truth at the row
level — every read-side query against `message_versions` filters/groups on this column
directly. The FK chain (`raw_update_id → telegram_updates.ingestion_run_id →
ingestion_runs.run_type`) is the AUDIT trail: if drift is ever suspected, the chain is
the cross-check. A row's `imported_final` MUST be TRUE iff that chain resolves to
`ingestion_runs.run_type = 'import'`. The boolean denormalisation avoids a JOIN to
`telegram_updates` + `ingestion_runs` on every read-side query — a single Boolean column
is cheaper to filter than a two-table join on hot read paths. Downstream consumers
(q&a, search, citations) treat provenance as a row-level attribute, not a chain attribute.
#103 is responsible for keeping them consistent on every write: every row written by an
import run sets `imported_final=TRUE`; every row written by live ingestion leaves it
`FALSE`. There is no scenario in normal operation where they disagree.

---

## 5. Implementation surface (for #103 Stream Delta)

This sprint (T2-NEW-H, issue #106) is doc-only. No production code is modified. The
implementation lands entirely in #103 (T2-03, Stream Delta).

### What #103 must do

1. **Add Alembic migration** adding `message_versions.imported_final` Boolean NOT NULL
   DEFAULT FALSE with `server_default='false'`. Existing live-ingested rows acquire
   `imported_final=FALSE` without touching them.

2. **Set `imported_final=TRUE`** in the import apply path for every `message_versions`
   row created during an import run. The apply path knows at call time that it is
   operating on an import run; it will pass `imported_final=True` to
   `MessageVersionRepo.insert_version` (or its equivalent in the import path).
   The FK chain `message_versions.raw_update_id → telegram_updates.ingestion_run_id
   → ingestion_runs.run_type` is the audit trail confirming the provenance; the
   Boolean is the denormalised fast-read copy.

3. **For messages with `edited_unixtime` set:** populate `message_versions.edit_date`
   from that timestamp. This preserves at minimum the timestamp of the last known edit.
   This is already implied by `telegram-desktop-export-schema.md §4` which states:
   "If `edited_unixtime` is present, it populates `edit_date` in that row."

4. **For messages without `edited` set:** still set `imported_final=TRUE` (provenance
   flag, not an "was-edited" flag) and leave `edit_date=NULL`.

5. **Overlap handling — `version_seq` is NOT always 1.** `MessageVersionRepo.insert_version`
   computes `version_seq = max(existing) + 1`. The resulting `version_seq` therefore
   depends on whether a live row already exists for the same `chat_message_id`:

   - **New chat_message (no prior `message_versions` row):** The import inserts
     `version_seq=1`, `imported_final=TRUE`. This is the common case for messages that
     predate the bot's deployment window.

   - **Existing chat_message (a live-ingested row already present for the same
     `(chat_id, message_id)`):** The import path **MUST NOT** insert a new
     `message_versions` row. The existing live row is authoritative. Behavior:
     - Skip the version insert entirely.
     - Increment a counter on the import run's `stats_json` (e.g.
       `"import.skip_existing_message_versions"`) so dry-run #99 / apply #103 can
       report it to the operator.
     - Do NOT mutate the existing row's `imported_final` flag. A live-ingested row
       with `imported_final=FALSE` stays `FALSE` — the live row's provenance is not
       shifted by an import overlap. **Live-ingested rows always win the provenance
       flag.**

   - **chat_message imported first, live edit arrives later (T1-14 path):** The live
     `edited_message` handler creates a new `message_versions` row at `version_seq=2`.
     That new row is a live row — `imported_final=FALSE`. The original v1 (imported)
     retains `imported_final=TRUE`. Both flags are stable; no action needed.

6. **`MessageVersionRepo.insert_version`** must be extended to accept an optional
   `imported_final: bool = False` parameter and pass it through to the INSERT.

7. **Dry-run parser (#94, T2-01)** does NOT write `message_versions` rows. However, the
   dry-run stats output (#99, T2-02) SHOULD report the count of messages with `edited`
   set as "would import as a new `message_versions` row with `imported_final=TRUE`
   (version_seq=1 if no live row exists for that chat_message; skip otherwise per §5
   overlap rule), edit history unrecoverable." This gives operators visibility into how
   many messages have only their final state preserved.

### What #103 must NOT do

- Do not add `imported_final` to `chat_messages`. The reasoning is in §4 above.
- Do not attempt to reconstruct intermediate edit states. TD export does not provide them.
- Do not skip messages because they have `edited` set. Option C is rejected.
- Do not bypass `detect_policy` for any imported message, regardless of `imported_final`.

---

## 6. Honesty about loss

### What is permanently lost

For every TD-imported message that had `edited` set before the export, the following
information is unrecoverable:

- **The original text** (what the sender first posted, before any edit).
- **The number of edits** (how many times the message was changed).
- **The content of intermediate versions** (what the message said at each edit step).
- **The reason for each edit** (Telegram does not expose this even to live bots).

This is a permanent loss. No Phase 5+ work, no re-import, no API call can recover this
data. Telegram does not expose edit history through any public API or export mechanism.

### What is preserved

- **The final text at export time** — stored in `message_versions.text` (and
  `message_versions.caption` for media messages with a caption).
- **The timestamp of the last edit** — stored in `message_versions.edit_date` when
  `edited_unixtime` is present in the TD message.
- **The `imported_final=TRUE` flag** — signals to all downstream consumers that this
  row is a static snapshot, not a live-chain record.
- **Governance markers** — `memory_policy`, `is_redacted`, and `offrecord_marks` rows
  are still created correctly if the final text contains `#nomem` or `#offrecord`.

### Mitigation

The `edit_date` field on the `message_versions` row carries at minimum "this content was
last edited at time T." For citation surfaces (Phase 4+), the display SHOULD always
append "imported snapshot — full edit history unavailable" alongside any "edited at T"
timestamp, so that readers understand the provenance of the content.

The `imported_final=TRUE` flag is the machine-readable form of this caveat; the citation
surface is responsible for rendering it appropriately in Phase 4.

---

## 7. Cross-references

| Reference | Relevance |
|-----------|-----------|
| `docs/memory-system/telegram-desktop-export-schema.md §4` (T2-NEW-A, issue #91) | Establishes the structural fact: TD export is a snapshot, not a history. This policy doc elaborates the response to that constraint. |
| Issue #94 (T2-01) dry-run parser | The parser MUST surface the count of messages with `edited` set in its dry-run output (#99). Cross-ref: #99 T2-02 stats doc. |
| Issue #99 (T2-02) dry-run stats | Stats SHOULD report "N messages with `edited` set — would import as a new `message_versions` row with `imported_final=TRUE` (version_seq=1 if no live row exists; skip otherwise per §5 overlap rule), edit history unrecoverable." |
| Issue #103 (T2-03) import apply, Stream Delta | Implements this policy: the Alembic migration, `imported_final` writes, `edit_date` population. This doc is the authoritative spec for that work. |
| `bot/handlers/edited_message.py` (T1-14) | Live edit handler. Documents the contrast story: live ingestion creates full v(n+1) chains; import creates a new row (version_seq=1 for a fresh chat_message; skips if a live row already exists — §5 overlap rule) with `imported_final=TRUE`. |
| `bot/services/content_hash.py` (T1-08) | The chv1 hash recipe. Import apply must compute hashes using the same recipe for all imported `message_versions` rows. |
| `docs/memory-system/HANDOFF.md` — `message_versions` semantics | Canonical table description. `imported_final` column is additive; it does not change `version_seq` semantics or the `(chat_message_id, content_hash)` idempotency key. |
| `docs/memory-system/AUTHORIZED_SCOPE.md` — "Telegram import rule" | Dry-run vs apply boundary. This policy is binding only for apply (#103). |
| `docs/memory-system/AUTHORIZED_SCOPE.md` — "Critical safety rule for `#offrecord`" | `#offrecord` governance runs unchanged; `imported_final=TRUE` does not exempt any message from policy detection. |
| `docs/memory-system/AUTHORIZED_SCOPE.md` — "Edit history during import (Phase 2 apply binding rule)" | The subsection added in this sprint that makes this policy binding for #103. |

---

## 8. Out of scope for this doc

| Topic | Owner |
|-------|-------|
| Alembic migration adding `imported_final` column | #103 / Stream Delta |
| `MessageVersionRepo.insert_version` extension to accept `imported_final` | #103 / Stream Delta |
| Dry-run stats reporting count of edited messages | #99 / T2-02 |
| Citation surface treatment of `imported_final=TRUE` rows (UX: "imported snapshot" caveat) | Phase 4 |
| Splitting `imported_was_edited` (was the TD message marked edited?) from `imported_final` (was this row built from a static archive?) | Future work — not scoped for this ticket or #103; document here for record |
| Recovering lost edit history | Impossible — Telegram does not expose it |
| Merging imported rows with simultaneously live-ingested rows for the same `(chat_id, message_id)` | Overlap is handled by the explicit policy in §5 (skip insert when a live row exists + stats counter). The existing `MessageVersionRepo.insert_version` idempotency on `(chat_message_id, content_hash)` is a SEPARATE safety net (catches identical-content re-imports), not a substitute for the §5 skip rule. |
| Per-channel-import variation (e.g. only some chats get `imported_final=TRUE`) | Not scoped; the simpler invariant (all import-run rows get `imported_final=TRUE`) is preferred |
| Recovering v(n+1) from a future hypothetical Telegram API | Not on any roadmap; would be a separate Phase 5+ design |
