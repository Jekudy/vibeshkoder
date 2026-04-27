# Telegram Desktop Export Schema

**Document:** T2-NEW-A (issue #91)
**Status:** reference (gates #93, #94, #98, #99)
**Date:** 2026-04-27
**Scope:** docs + fixtures only — no production code

This document is the implementer-facing reference for the Telegram Desktop JSON export format.
It describes the envelope, message structure, field shapes, and their mapping to the project's
internal `message_kind` taxonomy. The companion fixture set lives at
`tests/fixtures/td_export/`.

---

## Table of Contents

1. [Export envelope](#1-export-envelope)
2. [Message envelope](#2-message-envelope)
3. [Message types and `message_kind` mapping](#3-message-types-and-message_kind-mapping)
4. [Edit history representation](#4-edit-history-representation)
5. [Reply and reference fields](#5-reply-and-reference-fields)
6. [User identity (`from_id`)](#6-user-identity-from_id)
7. [Media file references](#7-media-file-references)
8. [`#nomem` / `#offrecord` policy in imports](#8-nomem--offrecord-policy-in-imports)
9. [Schema versioning and stability](#9-schema-versioning-and-stability)
10. [Out of scope for this doc](#10-out-of-scope-for-this-doc)

---

## 1. Export envelope

### Supported export type

We support: **single chat JSON export** produced by:
> Open the target chat in Telegram Desktop → ⋯ menu (top-right) → "Export chat history"
> → format: **Machine-readable JSON**

(The full-account export flow `Settings → Advanced → Export Telegram data` produces a
different envelope with a top-level `chats: [...]` list and is **not** the supported input
format — see §1 supported-export-types table for how to handle full archives.)

We do NOT support:
- HTML export (Telegram Desktop's other export format)
- Partial CSV exports from third-party tools
- Third-party Telegram backup formats

### `result.json` top-level structure

A single chat export produces one `result.json` file:

```json
{
  "name": "Chat Name",
  "type": "personal_chat | private_supergroup | public_supergroup | ...",
  "id": -1001234567890,
  "messages": [ ... ]
}
```

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Display name of the chat at export time |
| `type` | string | `personal_chat`, `private_supergroup`, `public_supergroup`, `saved_messages`, etc. |
| `id` | integer | Chat ID (negative for groups/channels) |
| `messages` | array | Ordered list of all message objects in the export window |

### Media attachment subdirectories

Telegram Desktop places binary attachments in type-specific subdirectories alongside
`result.json`: `photos/`, `video_files/`, `voice_messages/`, `files/`, `stickers/`,
`video_messages/`. Media fields in messages contain **relative paths** such as:

- `"photos/photo_1@15-01-2024_10-05-00.jpg"`
- `"video_files/video_1@05-03-2024_14-20-00.mp4"`
- `"voice_messages/audio_1@15-01-2024_10-12-00.ogg"`

All paths are relative to the directory containing `result.json` (e.g.
`"photos/photo_1@..."`). Real exports use these type-specific subdirs — there is no
single `media/` parent directory.

### Export types and scope

| Export type | Supported? | Notes |
|-------------|-----------|-------|
| Single chat JSON | **Yes — primary** | One `result.json` per chat |
| Full account archive | Partial — one chat at a time | Single root `result.json` with `chats: [...]` at top level, each chat containing its own `messages: [...]`. We support consumption of one chat at a time — caller must extract the relevant chat from the root structure before passing to the parser. Direct full-archive parsing is NOT supported by #94. |
| Channel as single chat | Yes | Channel posts treated as a single-chat export; anonymous posts have `from_id` starting with `channel` (see §6) |

---

## 2. Message envelope

### `messages` array

Every element of the top-level `messages` array is a **message object**. There are two primary
`type` values: `"message"` (user-authored content) and `"service"` (system events).

### Common fields present on every message object

| Field | Type | Present on | Notes |
|-------|------|-----------|-------|
| `id` | integer | all | Telegram message ID within the chat |
| `type` | string | all | `"message"` or `"service"` |
| `date` | string | all | ISO 8601 datetime string, local time of the export |
| `date_unixtime` | string | all | Unix timestamp as a string (decimal) |

### User-authored message fields (`type: "message"`)

| Field | Type | Notes |
|-------|------|-------|
| `from` | string | Display name of sender at export time |
| `from_id` | string | Prefixed ID — `"user<N>"` for regular users, `"channel<N>"` for anonymous channel posts; absent on some service messages |
| `text` | string or array | Message text. Plain string for simple messages; **mixed array** of plain strings and entity objects for formatted text. May be empty string `""` for media-only messages. See §2 "Mixed-array text form" below. |
| `text_entities` | array | Entity annotation array (see below). May be empty `[]` |
| `reply_to_message_id` | integer | ID of the message being replied to; absent if not a reply |
| `forwarded_from` | string | Original sender display name for forwarded messages; absent if not forwarded |
| `via_bot` | string | Bot username if message was sent via inline bot |
| `edited` | string | ISO 8601 datetime of last edit; absent if never edited |
| `edited_unixtime` | string | Unix timestamp of last edit as string; absent if never edited |

### `text_entities` array

Each element is an object with `type` and `text` fields:

```json
{"type": "plain", "text": "Hello"},
{"type": "hashtag", "text": "#nomem"},
{"type": "bold", "text": "important"},
{"type": "text_link", "text": "click here", "href": "https://example.com"}
```

Common entity types: `plain`, `bold`, `italic`, `code`, `pre`, `strikethrough`,
`underline`, `spoiler`, `hashtag`, `mention`, `link`, `text_link`, `email`,
`phone`, `cashtag`, `bot_command`.

### Mixed-array `text` form

When a message contains inline formatting or links, the `text` field is a **mixed array**
containing both plain strings and entity objects interleaved — not a flat array of entity
objects only. Example:

```json
"text": ["See ", {"type": "bold", "text": "important"}, " update."]
```

The fixture `tests/fixtures/td_export/small_chat.json` message id=1005 demonstrates this shape:

```json
"text": [
  "Interesting article about community building: ",
  {"type": "text_link", "text": "read more", "href": "https://example.com/article"}
]
```

Relationship with `text_entities`: when both fields are present, `text_entities` is the
**canonical normalized representation** and `text` (array form) recapitulates the same
segments positionally. When `text` is an array, a parser SHOULD prefer `text_entities`
for entity-aware processing (they must be consistent — inconsistency indicates a malformed
export). See §3 for the full entity-type list.

### Service message fields (`type: "service"`)

Service messages document chat events. They use different top-level fields:

| Field | Type | Notes |
|-------|------|-------|
| `actor` | string | Display name of user who triggered the action |
| `actor_id` | string | Prefixed ID of actor |
| `action` | string | Event type: `"join_group_by_link"`, `"invite_members"`, `"remove_members"`, `"pin_message"`, `"edit_group_title"`, `"edit_group_photo"`, `"migrate_from_group"`, etc. |
| `text` | string | Usually empty string for service messages |

The parser (#94) will **skip service messages** (no content write). This document describes
them so the parser knows what to ignore.

---

## 3. Message types and `message_kind` mapping

The table below maps TD export field shapes to the project's `message_kind` taxonomy. The
canonical taxonomy (the *set* of allowed values) is defined by
`bot/services/normalization.py::_KIND_PROBES`; this doc reuses the same value names
(`text`, `photo`, `video`, `voice`, `audio`, `document`, `sticker`, `animation`,
`video_note`, `location`, `contact`, `poll`, `dice`, `forward`, `service`, `unknown`).

The *discriminators* differ: `_KIND_PROBES` reads aiogram message attributes (e.g.
`forward_origin`, `photo`, `voice`, `new_chat_members`); TD export has no aiogram surface
and uses different field shapes (e.g. `forwarded_from` string, `media_type` discriminator,
`type: "service"` flag). The mapping below translates TD shapes to the matching
`message_kind` value; the inline helper `_infer_kind` in
`tests/fixtures/test_td_export_fixtures.py` implements this mapping for fixture validation
without importing from `bot/`.

**Priority order matters** — a message with both `forwarded_from` and `photo` is classified
as `forward`. TD export uses `forwarded_from` (a string display-name field) as the forward
discriminator; it pre-empts media kind classification, mirroring `_KIND_PROBES` priority
where `forward` outranks media. TD service messages are identified by `type: "service"` (not
by aiogram attributes like `new_chat_members`); they pre-empt all content classification
because they have no content fields to misclassify.

| TD export field shape | `message_kind` | Notes |
|-----------------------|---------------|-------|
| `type: "service"` | `service` | Highest priority — skip in parser (#94) |
| `forwarded_from` set (any non-null string) | `forward` | Second priority — overrides media |
| `media_type: "photo"` OR `photo` field set | `photo` | Caption goes in `text` field |
| `media_type: "video_file"` | `video` | |
| `media_type: "voice_message"` | `voice` | |
| `media_type: "audio_file"` | `audio` | |
| `media_type: "sticker"` | `sticker` | |
| `media_type: "animation"` | `animation` | GIF equivalent |
| `media_type: "video_message"` | `video_note` | Round video (voice note variant) |
| `location_information` set | `location` | |
| `contact_information` set | `contact` | |
| `poll` set | `poll` | |
| `dice` set | `dice` | |
| `mime_type` starts with `"application/"` | `document` | Files without specific media_type |
| `text` or `text_entities` set, no media | `text` | Fallback for plain text messages |
| anything else | `unknown` | Unknown shape — log and continue |

### `text` field on media messages

For photo, video, voice, and other media messages: the `text` field contains the **caption**,
not a separate field. During import, the parser (#94) must write it to the `caption` column of
`chat_messages`, NOT to `text`.

---

## 4. Edit history representation

### What TD export stores

Telegram Desktop export does **NOT** preserve the full version chain of edited messages.
It stores only:
- The **latest version** of the text/caption at export time
- An `edited` / `edited_unixtime` field indicating the message was ever edited

Example of an edited message in the export:

```json
{
  "id": 2001,
  "type": "message",
  "date": "2024-02-10T09:00:00",
  "date_unixtime": "1707555600",
  "from": "User One",
  "from_id": "user1000001",
  "text": "Project update: we shipped the feature on Friday.",
  "text_entities": [...],
  "edited": "2024-02-10T09:45:00",
  "edited_unixtime": "1707558300"
}
```

There is no way to recover what the original text said before the edit.

### Contrast with live ingestion

Live ingestion via `edited_message` Telegram update (T1-14) produces one `message_versions`
row per detected change: version_seq=1 for original, version_seq=2 for first edit, etc.

### Implication for import

When importing a TD export, an edited message is represented as a **single `message_versions`
row with `version_seq=1`** — there is no v0 original because TD export only stores the
latest state.

**Import apply path (#103, Stream Delta):** The apply path creates this `message_versions`
row. If `edited_unixtime` is present, it populates `edit_date` in that row. Direct content
writes are owned by `persist_message_with_policy()` (#89), not the parser.

**Import dry-run parser (#94):** The dry-run parser does NOT write `message_versions` rows
or any other content rows. It is read-only by design (AUTHORIZED_SCOPE.md "Telegram import
rule"). The parser MAY surface "would-be edited message" counts to the stats output
(#99, T2-02) — but only as a statistic, never as a DB write.

The detailed edit history reconciliation policy (partial imports, merging imported edits
with live versions, version numbering across both paths) is specified in **#106 (T2-NEW-H)**.
This document only establishes the structural constraint: TD export is a snapshot of the
latest state, not a full history.

---

## 5. Reply and reference fields

### `reply_to_message_id`

```json
"reply_to_message_id": 3001
```

- Integer — points to another `id` in the same export
- **May be dangling**: if the export is partial (e.g. user exported only the last 30 days),
  the referenced message may not be present. The reply resolver (#98, T2-NEW-C) must return
  NULL for unresolved references — do not crash the import.
- Fixture `replies_with_media.json` includes a dangling reply (`reply_to_message_id: 2999`
  with no message id=2999) to exercise this edge case.

### `forwarded_from`

```json
"forwarded_from": "Tech News Daily"
```

- String — the **display name** of the original sender at export time, not a numeric ID
- No reverse lookup to a user record is possible from this field alone
- Original sender's user identity is NOT available (Telegram privacy design)
- Contrast with live Bot API: `forward_origin` provides structured origin info

### Quotes / replied-to content

TD export does NOT echo the quoted text of the message being replied to. The `text` field
contains only the replying message's own content. This is different from the Telegram Bot API
`external_reply` / `quote` fields that may include a text excerpt.

The reply resolver (#98) correlates `reply_to_message_id` back to the original message's
content in the DB — it does not rely on an echoed quote in the export.

### `via_bot`

```json
"via_bot": "somebot"
```

- Username string (without `@`)
- Present when the message was sent through an inline bot query
- Not used in current import scope; parser (#94) may store or ignore

---

## 6. User identity (`from_id`)

### Format

| Value | Meaning |
|-------|---------|
| `"user12345"` | Regular Telegram user with ID `12345` |
| `"channel12345"` | Anonymous channel post — no associated user identity |
| absent | Service messages (some actions have no actor id) |

### Anonymous channel posts

When a channel is linked to a group, messages posted by the channel appear in the group feed
with `from_id` starting with `"channel"`. These have no user identity in the Telegram model:
the channel ID is not a user ID. During import:

- Do NOT attempt to resolve a user record for `channel`-prefixed `from_id` values
- Tag the imported message with a synthetic "channel post" attribution
- The full user mapping policy (including ghost users and `is_imported_only` flag) is
  specified in **#93 (T2-NEW-B)**.

### Missing `from_id`

Some service messages omit `from_id` entirely. The import parser (#94) must handle this
gracefully (null attribution, not a crash).

---

## 7. Media file references

### Field shapes

| Message field | Media type | Example value |
|---------------|-----------|---------------|
| `photo` | Photo | `"photos/photo_1@15-01-2024_10-05-00.jpg"` |
| `file` | Any file (voice, audio, document, video) | `"video_files/video_1@05-03-2024_14-20-00.mp4"` |
| `thumbnail` | Video/animation thumbnail | `"video_files/video_1@05-03-2024_14-20-00.jpg"` |

All paths are **relative to the directory containing `result.json`**.

### Dry-run scope

Import dry-run (Phase 2a) does NOT touch media files. It parses paths from the JSON and
records their presence in stats (#99), but does not copy, hash, or reference the binary files.

### Apply scope (out of scope for this stream)

Import apply (Phase 2b, #103, Stream Delta) may copy media files into the project's storage
layer. That logic is not described here. Future implementers should extend this section when
#103 is scoped.

---

## 8. `#nomem` / `#offrecord` policy in imports

### Governance path

Both `text` and caption (the `text` field of media messages) must be passed through
`bot/services/governance.py::detect_policy(text, caption)` during import — exactly as live
messages are handled.

This is a binding cross-cutting rule from `AUTHORIZED_SCOPE.md`:

> **Critical safety rule for `#offrecord`:**
>
> `#offrecord` content **must not** be durably stored as raw visible content.
>
> Implementation default for the policy detector + raw persistence:
> - **Detect `#offrecord` BEFORE committing content-bearing `raw_json`**, OR
> - Write raw update + redaction in the same transaction before commit.
>
> Committed storage for `#offrecord` keeps only minimal metadata: chat id, message id,
> timestamp, hash / tombstone key, policy marker, audit metadata.
>
> **No** search, q&a, extraction, summary, catalog, vector, graph, or wiki may use
> `#offrecord` content. Forbidden content never reaches `llm_gateway`.

### Import modes

From `AUTHORIZED_SCOPE.md` "Telegram import rule" (binding):

> - **Dry-run** — allowed before full governance (Phase 2a). Parses the export, reports
>   stats, **no content writes**.
> - **Apply** — blocked until `#nomem` / `#offrecord` detection AND `forget_events`
>   tombstone skeleton both exist. Apply must use the same normalization + governance
>   path as live Telegram updates.

**Dry-run mode** (issue #99, #94): only counts policies, writes nothing. No redaction needed
because no content is written.

**Apply mode** (issue #103): MUST call `persist_message_with_policy()` (issue #89) which
routes through `detect_policy()` before any content write. Direct INSERT into `chat_messages`
is forbidden — enforced by reviewer checklist (see Phase 2 risk map in HANDOFF.md, R4).

### Fixture coverage

`tests/fixtures/td_export/edited_messages.json` includes:
- Message id=2003: `#nomem` hashtag in text — triggers `detect_policy` returning `'nomem'`
- Message id=2004: `#offrecord` hashtag in the photo's `text` field, which TD export uses
  for media captions (see §3 message_kind mapping) — must be redacted on apply in the same
  transaction; only minimal metadata (ids, timestamps, hash, policy marker) survives

---

## 9. Schema versioning and stability

The TD export JSON format is **undocumented by Telegram** and has changed between Telegram
Desktop versions. This document and the fixtures in `tests/fixtures/td_export/` describe the
format observed in **2026-04 exports** from Telegram Desktop.

### Handling unknown fields

The parser (#94) must follow a tolerant reader pattern:
- **New fields** (fields not listed in this document): ignore silently
- **Missing required fields** (e.g. `id`, `date` absent on a message): soft fail — log a
  warning at run level, skip the individual message, continue with the rest of the import.
  Do NOT crash the entire import run on a single bad message.
- **Unknown `media_type` values**: classify as `unknown` `message_kind`, log

### Fixture pinning

The fixtures in `tests/fixtures/td_export/` are pinned to the 2026-04 format. When a future
Telegram Desktop version changes the format, update the fixtures AND this document together.
Tests in `tests/fixtures/test_td_export_fixtures.py` serve as a regression guard.

---

## 10. Out of scope for this doc

The following topics are specified in companion tickets and must not be implemented or
designed from this document alone:

| Topic | Ticket |
|-------|--------|
| User identity mapping policy (ghost users, `is_imported_only` flag, display_name collision) | **#93 (T2-NEW-B)** |
| Edit history detailed policy (merge imported edits with live versions, version numbering) | **#106 (T2-NEW-H)** |
| Import dry-run parser implementation | **#94 (T2-01)** |
| Reply resolver service (dangling reply handling, NULL on unresolved) | **#98 (T2-NEW-C)** |
| Dry-run duplicate / policy stats | **#99 (T2-02)** |
| Import apply with synthetic updates | **#103 (T2-03, Stream Delta)** |

---

## Appendix: fixture index

| File | Purpose | Messages | Key coverage |
|------|---------|----------|--------------|
| `tests/fixtures/td_export/small_chat.json` | Baseline — clean, <10 messages | 6 | text, photo+caption, voice, reply, forward, service join |
| `tests/fixtures/td_export/edited_messages.json` | Edit representation | 5 | 2 edited fields, `#nomem`, `#offrecord` |
| `tests/fixtures/td_export/replies_with_media.json` | Replies + media + anonymous | 8 | reply chain A→B→C, photo reply, video reply, anonymous channel post, dangling reply |
