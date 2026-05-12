# T6-05 Design — Admin card browsing commands

**Status:** Pre-flight design, Wave 2 / Stream C.
**Cycle:** Memory system Phase 6 — knowledge cards / catalog.
**Date:** 2026-05-12.
**Predecessor:** T6-04 (admin review commands) MUST merge first — `/card <id>` may show approval metadata only after the approval audit trail exists.
**Companion docs:** `PHASE6_PLAN.md` §5.C, §7; `T6-04_design.md` (sibling — admin review).
**Author:** Wave 2 design sprint agent (pre-flight planning).

---

## §0. Acceptance criteria (verbatim from `PHASE6_PLAN.md` §7)

### T6-05: Admin card browsing commands

- **Scope:** Telegram handlers for `/cards`, `/card <id>`.
- **Acceptance criteria:**
  - `/cards` paginates approved cards only.
  - `/card <id>` shows title, body preview/detail, approval metadata, and source message back-citations via `card_sources`.
  - Archived/draft cards are hidden from default list.
- **Dependencies:** T6-04.
- **Stream:** Wave 2 / Stream C.

---

## §1. Invariants enforced by this ticket

Cross-references to `PHASE6_PLAN.md` §1:

1. **#1 Existing gatekeeper must not break.** Read-only handlers; no writes.
2. **#4 Citations point to `message_version_id` or approved card sources (FK-normalized via `card_sources`).** `/card <id>` JOINs `card_sources` → `message_versions` → `chat_messages` to render back-citations as Telegram message links.
3. **#5 Summary is never canonical truth.** `/card <id>` body_markdown IS the admin-approved canonical content — it is rendered as-is (Q1: Telegram MarkdownV2). The "summary is never canonical truth" rule applies to LLM-derived summaries (Phase 7+), NOT to admin-approved cards.
4. **#10 Public wiki remains disabled until review / source trace / governance are proven.** `/cards` and `/card <id>` are admin-only (private chat); no public web exposure. The Phase 9 wiki ticket inherits this gate.

### Not enforced (read-only handler)

- No advisory locks (no DB mutations).
- No governance re-validation (the card's status was already governance-cleared at `/approve` time per T6-04; subsequent forget-cascade events demote the card to `archived` per §5.A.5, hiding it from `/cards`).
- No `extraction_decisions` writes.

### Privacy hygiene

The renderer MUST NOT expose:
- Body content of FORGOTTEN source messages (the `message_versions` row is redacted by the cascade; the `card_sources` row for that mvid would have been DELETED first by `_cascade_card_sources_on_forget` per §5.A.5; if remaining count == 0, the card itself was demoted to `archived` and is not surfaced by `/cards` default filter).
- Cards with `card_status='draft'` or `'archived'`.

---

## §2. /cards paginated browse

Read-only handler. Filter `card_status='approved'` ONLY. No exception even for admins.

### Query

```sql
SELECT kc.id,
       kc.title,
       LEFT(kc.body_markdown, 200) AS body_preview,
       kc.approved_by_user_id,
       kc.approved_at,
       kc.created_at,
       kc.updated_at,
       (SELECT COUNT(*) FROM card_sources cs WHERE cs.card_id = kc.id) AS source_count
FROM knowledge_cards AS kc
WHERE kc.card_status = 'approved'
ORDER BY kc.approved_at DESC, kc.id DESC  -- newest approvals first
LIMIT :page_size OFFSET :offset
```

- `body_preview` is the first 200 chars of `body_markdown`. Telegram MarkdownV2 entities can be cut mid-formatting; the renderer MUST collapse trailing partial markup to a safe form (e.g., HTML-escape preview, append `…`).
- `source_count` subquery is cheap given `ix_card_sources_message_version_id` reverse index + small N typical. For high-volume future runs, replace with a left-joined aggregate.

### Page size

10 cards per page. Page param: `/cards 2` parsed via `command.args.strip()`; default 1. Match T6-04 pagination behavior.

### Rendering

```
📚 *Approved cards* (page 1)

#1  `<short_uuid>`
    "Title here"
    preview: «extracted body preview, first 200 chars escaped»
    sources: 3 · approved 2026-05-12 14:32 UTC by @admin_username
    `/card <full-uuid-or-prefix>` for detail.

#2  ...

Page 2: `/cards 2`.
```

- `<short_uuid>` is the first 8 chars (uniqueness within 8 chars is good enough for human selection; collision risk negligible at expected scale).
- `@admin_username` resolution: JOIN to `users` table on `approved_by_user_id` and pull `username`. If `username` is NULL or the user row has been soft-deleted (FK SET NULL → `approved_by_user_id IS NULL`), render `[deleted user]`. Audit shadow doesn't help here — `knowledge_cards` doesn't store a username snapshot.
- Body preview MUST be plain-text escaped (HTML.escape) even though the body is MarkdownV2. The preview line is for browsing only; full body lives in `/card <id>`.
- All field rendering uses HTML parse_mode for the list view (safer; the body is shown via MarkdownV2 only in detail view).

### Limits

- Zero approved cards → `📚 No approved cards yet.` reply, stop.
- Page exceeds total → `📚 Page <N> is empty. Use /cards to start over.`
- Non-admin → silent denial (no Telegram reply).
- Non-private chat → silent denial; or, if invoked in a group, replying with "Команда работает только в личке" matches existing admin pattern. Recommendation: silent denial.

### Filters not supported in T6-05

- Searching by title text — defer to T6-06 (when search ext lands, admin can use `/recall` with `include_cards=True`).
- Filtering by approver — defer to Phase 6.5.
- Status filter to surface archived/draft for forensic review — defer to Phase 6.5. (Sufficient for now: an admin can SQL-query directly if needed.)

---

## §3. /card <id> detail view

Read-only. Resolves UUID (or short prefix). Renders title, full body_markdown, approval metadata, and source back-citations.

### Resolution

`<id>` argument: full UUID OR short prefix (≥ 8 chars). Resolution query:

```sql
SELECT id, title, body_markdown, card_status, archived_reason,
       approved_by_user_id, approved_at, created_at, updated_at
FROM knowledge_cards
WHERE id::text LIKE :id_prefix || '%'
LIMIT 2
```

- LIMIT 2 lets the handler detect ambiguity: if more than 1 row returns, reply `Multiple cards match prefix '<prefix>'. Specify more.` (rare in practice given UUID space).
- If 0 rows: `Card not found.`

### Card-status visibility

- `card_status='approved'` → render in full.
- `card_status='draft'` → render `Card is in DRAFT state — not yet approved. Use /candidates to find pending candidates.` (cards never become drafts post-approval in T6-04 scope; this branch exists for forward-compat with Phase 6.5 edit flow.)
- `card_status='archived'` → render `Card is ARCHIVED. archived_reason: <reason>.` Show metadata but NOT body content unless admin explicitly invoked something like `/card <id> --archived` (recommend defer flag to Phase 6.5).

Default behavior (no flag): if `card_status != 'approved'`, do NOT show body content. Archived cards may have been demoted because all sources were forgotten (privacy invariant: `archived_reason` references the `forget_event_id` only — but the body content was authored from source content that is now forgotten; rendering it without flag is arguably a privacy edge case). Conservative posture: hide body for non-approved status. Spec is silent; recommend this restriction.

### Source back-citation query

```sql
SELECT cs.id AS card_source_id,
       cs.message_version_id,
       cs.position,
       mv.chat_message_id,
       c.chat_id,
       c.message_id,
       c.memory_policy,
       c.is_redacted,
       mv.is_redacted AS mv_is_redacted
FROM card_sources AS cs
JOIN message_versions AS mv ON mv.id = cs.message_version_id
JOIN chat_messages AS c ON c.id = mv.chat_message_id
WHERE cs.card_id = :card_id
ORDER BY cs.position ASC, cs.id ASC
```

For each row, render a Telegram message link: `https://t.me/c/<short_chat_id>/<message_id>` (pattern from `bot/handlers/qa.py:_short_chat_id` + `_format_response:88-100`).

Filter / rendering rules:

- If `c.memory_policy != 'normal'` OR `c.is_redacted=TRUE` OR `mv.is_redacted=TRUE` → the source has been demoted/redacted SINCE approval. The `_cascade_card_sources_on_forget` cascade SHOULD have deleted the corresponding `card_sources` row already; the source's appearance in this query result implies cascade hasn't run yet OR `card_status` is already `'archived'`. Either way, do NOT render the message link — replace with `<source redacted/forgotten>` placeholder and the bare `card_source_id` for forensics. NO body content.
- Render an explicit count: `Source 1 of 3`, etc.

### Rendering

```
📄 *Card detail*  `<full-uuid>`

Title: Title here
Status: approved
Approved: 2026-05-12 14:32 UTC by @admin_username
Created: 2026-05-12 14:30 UTC
Updated: 2026-05-12 14:32 UTC

*Body:*
<body_markdown rendered as MarkdownV2>

*Sources (3):*
[1] <a href="https://t.me/c/XXX/123">message</a>  (mvid 4521)
[2] <a href="https://t.me/c/XXX/124">message</a>  (mvid 4522)
[3] <source redacted/forgotten>  (mvid 4523, card_source_id <uuid>)
```

- Telegram MarkdownV2 body rendering: this is the FIRST place `body_markdown` is shown to humans. The card body MUST be MarkdownV2-safe per Q1; the extractor (T6-02) and `/approve` flow (T6-04) are responsible. If a malformed body crashes Telegram's parser, the handler MUST catch `TelegramAPIError` and fall back to HTML-escape preview with a warning. Defensive — not a normal path.
- Use `parse_mode="MarkdownV2"` for the body block; use `parse_mode="HTML"` for the wrapping header / source list (mixed parse modes require splitting messages — recommend rendering as 2 sequential messages or use HTML wrapping with `<pre>` for the body if Markdown rendering fidelity isn't critical).
- Alternative simpler approach: render the entire `/card` reply in HTML, with the body in `<pre>...</pre>` (preserves whitespace, no Markdown parsing). Loses Markdown formatting fidelity but avoids parse_mode juggling. Recommend this for T6-05 ship.

### Limits

- Body length > Telegram 4096 char limit: paginate over multiple messages or truncate with `…` continuation hint. Recommend truncate at first 3500 chars + `(truncated; use SQL for full body)`. Add overflow note to risk register.
- Source list > N items (say > 20): truncate with `…` and `+N more`. Rare in practice for admin-approved cards (typical 3-10 sources).
- Non-admin → silent denial.

---

## §4. Files touched

| File | Action | Notes |
|---|---|---|
| `bot/handlers/admin_cards.py` | EXTEND | Add `/cards` and `/card` handlers to the same router as T6-04. |
| `bot/db/repos/knowledge_card.py` | EXTEND | Add `list_approved(session, limit, offset)`, `get_by_id_prefix(session, prefix)`, `get_by_id(session, card_id)`. Flush not used (pure read). |
| `bot/db/repos/card_source.py` | EXTEND | Add `list_for_card(session, card_id)` returning rows with JOIN to message_versions + chat_messages (read-only). |
| `tests/handlers/test_admin_cards_browse.py` | NEW | Unit + integration. See §6. |
| `tests/db/test_knowledge_card_repo_browse.py` | NEW | List + get-by-prefix tests. |
| `tests/db/test_card_source_repo_list.py` | NEW | List with JOIN tests. |
| `scripts/lint_privacy_check.sh` | NO CHANGE | This handler does not touch memory_policy / is_redacted strings directly — the repo queries do. If the repo file is allowlisted in T6-04, no further change. Re-verify post-rebase. |

**Out of scope (file-touching):**
- `bot/services/search.py` — T6-06.
- `bot/services/evidence.py` — T6-07.
- `bot/services/llm_gateway.py` — not touched; no LLM calls in browse.

---

## §5. Concurrency / race considerations

`/card <id>` is a pure read. No advisory locks needed. The concurrency considerations:

1. **Card demoted to archived between `/cards` and `/card <id>`.** Admin browses `/cards`, sees a card, then runs `/card <id>` after a forget_event was applied. The card may now be `card_status='archived'`. Handler MUST re-check status at read time (the query in §3 includes `card_status`) and follow the visibility rules per §3.
2. **Source mvid forgotten between approval and `/card <id>`.** The `_cascade_card_sources_on_forget` cascade deletes the `card_sources` row first, then potentially demotes the card. If the cascade is in progress (mid-transaction with the advisory lock held), the read here will block on `SELECT FOR UPDATE`-style ops only — and we use plain `SELECT`, no `FOR UPDATE`, so we read whatever the cascade has committed (snapshot isolation under Postgres read-committed). Filter logic in §3 handles the partial-redaction case gracefully.
3. **Two admins browse simultaneously.** Pure read; no conflict.

---

## §6. Tests

### Unit

**`tests/handlers/test_admin_cards_browse.py`:**

1. `/cards` returns paginated approved list. Mock `KnowledgeCardRepo.list_approved`. Assert query filter `card_status='approved'`, order `approved_at DESC, id DESC`, limit, offset.
2. `/cards` page 2 → offset=10.
3. `/cards` from non-admin → silent denial.
4. `/cards` with no approved cards → `No approved cards yet.` reply.
5. `/card <full-uuid>` happy path → title, body, sources, approval metadata.
6. `/card <short-prefix>` → resolves to single card.
7. `/card <ambiguous-prefix>` → `Multiple cards match` error reply.
8. `/card <not-found>` → `Card not found.` error reply.
9. `/card <id-of-draft>` → status info, NO body.
10. `/card <id-of-archived>` → archived_reason info, NO body.
11. `/card <approved-id-with-redacted-source>` → renders body, but source list shows `<source redacted/forgotten>` placeholder for the redacted source. No body content of source leaks.
12. `/card <id>` from non-admin → silent denial.
13. `/cards`/`/card` invoked in group chat → silent denial (or polite error per implementation choice).

### Integration (Postgres)

**`tests/handlers/test_admin_cards_browse_integration.py`:**

14. Seed 3 cards (1 approved, 1 draft, 1 archived). `/cards` returns only the approved one. Counts and pagination correct.
15. Seed 1 approved card with 3 sources, all healthy. `/card <id>` renders all 3 message links.
16. Seed 1 approved card with 3 sources; redact 1 source via `chat_messages.is_redacted=TRUE` (simulating cascade-in-progress where `card_sources` row was NOT yet deleted — edge case). `/card <id>` renders 2 message links + 1 placeholder.
17. Seed 1 approved card; forget one source (run the full cascade). Assert `card_sources` row for that mvid is gone (§5.A.5 step 2). `/card <id>` renders remaining sources only.
18. Seed 1 card; forget ALL sources (cascade demote). Card is now `card_status='archived'`. `/cards` no longer returns it. `/card <id>` shows archived status + reason, no body.
19. Telegram body length overflow: seed a card with `body_markdown` > 4500 chars. `/card <id>` truncates + appends `(truncated; …)` note. Reply does NOT crash Telegram API.
20. Body Markdown V2 parse error: seed a card with intentionally malformed MarkdownV2. Handler catches `TelegramBadRequest`, falls back to HTML-escape preview. Reply DOES land.

### Privacy

21. Confirm: across all tests, no test reads or asserts on the body content of a redacted/forgotten source row. The handler must NEVER include forgotten body content. (Defensive — the body is already nulled at the `message_versions` cascade layer, but the assertion provides a clear guarantee.)

---

## §7. Stop signals (specific to this ticket)

- `/card <id>` returning body content for `card_status != 'approved'` → STOP, privacy edge case. Handler guards in §3.
- `/cards` returning archived or draft cards in the default list → STOP, governance breach (Q3 collapsed `deprecated` into `archived`; archived means demoted, NOT browseable in default flow).
- `/card <id>` exposing body content of forgotten source messages — should be impossible since source body is NULLed at the cascade's message_versions layer (Phase 3 cascade), AND `card_sources` rows for forgotten mvids are deleted by §5.A.5 cascade. But defensive renderer logic in §3 guarantees this even under partial cascade state.
- `/cards` paginating over a frozen / inconsistent snapshot (e.g., `LIMIT/OFFSET` skipping rows due to concurrent inserts shifting the page boundary) — minor UX issue, not a privacy break. Defer.

---

## §8. Risk register

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R-05-01 | Body MarkdownV2 from a hand-edited (Phase 6.5) card breaks Telegram parser. | MED | Try/except `TelegramBadRequest`; fall back to HTML-escape preview. Test #20 covers. |
| R-05-02 | Short-prefix UUID collision when admin types `/card abc` and 2 cards match. | LOW | Limit 2 in resolution query; reply `Multiple cards match` and require longer prefix. Test #7. |
| R-05-03 | `approved_by_user_id` SET NULL after admin soft-delete leaves `/cards` rendering `[deleted user]`. | LOW | Acceptable per audit shadow design (T6-04 §1). Phase 6.5 can add a username snapshot column to `knowledge_cards` if needed. |
| R-05-04 | Body > 4096 chars crashes single-message reply. | MED | Truncate at 3500 + continuation hint. Optionally split into multiple messages; defer to Phase 6.5. Test #19. |
| R-05-05 | Source list count subquery becomes slow at scale (>10K cards × >20 sources/card). | LOW | Defer to Phase 6.5 (replace with cached `source_count` column or LATERAL JOIN). Current N is small. |
| R-05-06 | Admin browses `/card <id>` mid-cascade; some `card_sources` rows are gone, some remain → user-visible inconsistency. | LOW | Conservative renderer: per-source filter on `chat_messages.is_redacted` AND `message_versions.is_redacted` (§3). Eventually consistent at next page reload. |
| R-05-07 | Body rendering as HTML `<pre>` loses Markdown fidelity → admins see raw `**bold**` instead of rendered bold. | LOW | Acceptable for T6-05. Phase 6.5 can switch to true MarkdownV2 once extractor output is proven safe across cases. |
| R-05-08 | Old `card_status='deprecated'` rows (none expected; Q3 collapse) escape filter. | LOW | DB CHECK `card_status IN ('draft','approved','archived')` enforces. Confirmed in `bot/db/models.py:1100-1108`. |
| R-05-09 | Pagination skips/duplicates rows on concurrent inserts. | LOW | Inherent LIMIT/OFFSET behaviour; not a correctness break. Defer keyset pagination to Phase 6.5. |
| R-05-10 | Lint-privacy false-positive on `admin_cards.py` (post-rebase). | LOW | Inherited from T6-04 risk; same allowlist update applies. |

---

## §9. Open questions

1. **MarkdownV2 vs HTML for body rendering.** Spec says body is stored as MarkdownV2 (Q1). Browser handler can either parse-render or display as `<pre>` text. Recommend `<pre>` for T6-05 to avoid first-render parser crashes. Defer Markdown-rendered display to Phase 6.5 once T6-02 extractor output is validated across 100+ real cards.
2. **Should `/cards` show source counts > 0 only?** Cards always have ≥1 source post-approval (T6-04 step 6 inserts at least one card_sources row; rejects empty source set). After cascade demote, the count CAN reach 0 — but card is then archived (§5.A.5 step 4). So `card_status='approved'` implies source_count > 0 invariant. Recommend NOT filtering on source_count > 0 in the SELECT; rely on the status filter.
3. **Should `/card <id>` show the `extraction_decisions.reason` if any?** Admins might want to see "approved with note: X". Add `decision_reason` to detail view. Spec is silent. Defer to Phase 6.5 unless review feedback raises priority.
4. **Sort order for `/cards`.** Spec: "paginates approved cards only". Default sort: `approved_at DESC`. Alternative: `created_at DESC` (matches T6-04 candidate order). Recommend `approved_at DESC` — admins care about what was just approved.
5. **Render Telegram links for sources from chats other than COMMUNITY_CHAT_ID.** If a multi-chat future surfaces, all source `chat_id`s should be linkable. For now, COMMUNITY_CHAT_ID is the single ingestion source; `_short_chat_id` works generically. No code change needed.
6. **Search inside `/cards` (typeahead).** Out of scope for T6-05. T6-06 enables card-FTS via `/recall include_cards`. If admins need title search standalone, defer to Phase 6.5.
7. **Forensic archived-card view.** Should there be a separate `/cards-archived` for forensic browse? Spec says "archived/draft cards are hidden from default list" — implies a non-default view could exist. Defer to Phase 6.5.

---

## §10. Out of scope for T6-05

- `/edit-card` (Phase 6.5 / Q6).
- Card revision history (Phase 6.5 / D3 — `card_revisions` deferred).
- Search inside cards from a non-`/recall` entry point (T6-06).
- Web cards page (T6-08, deferred if Phase 5 web scaffolding absent).
- Bulk operations (archive, delete) on cards.
- Per-admin filtering (`/cards --by @admin`).

---

## §11. Evidence log (files read while drafting)

| File | Key facts extracted |
|---|---|
| `docs/memory-system/PHASE6_PLAN.md` | §5.A `knowledge_cards` schema; §5.C handler list; §7 T6-05 acceptance; §11 Q1 / Q3. |
| `bot/db/models.py:1079-1217` | KnowledgeCard + CardSource schema. CHECK on `card_status`, FK directions, generated `body_tsv`. |
| `bot/handlers/qa.py:_short_chat_id` (line 51-53), `_format_response` (line 83-100) | Telegram message link pattern: `https://t.me/c/<short_chat_id>/<message_id>`. HTML escape pattern. |
| `bot/handlers/admin.py:27-56` | Admin filter pattern for /stats — same shape for /cards. PrivateChatFilter pattern. |
| `bot/services/forget_cascade.py:_cascade_card_sources_on_forget:587-762` | Cascade semantics — confirms `card_sources` row deletion + card demotion to archived when remaining count == 0. |
| `bot/db/repos/qa_trace.py` | Repo pattern reference (flush-only, raise on miss). |
| `alembic/versions/032_add_knowledge_cards.py` | Confirms FTS column `body_tsv` is generated; no manual writes. |

---

## §12. Implementation order (suggested)

1. Extend `bot/db/repos/knowledge_card.py` (list_approved, get_by_id_prefix, get_by_id) + unit tests.
2. Extend `bot/db/repos/card_source.py` (list_for_card with JOIN) + unit tests.
3. Add `/cards` handler to `bot/handlers/admin_cards.py` + unit tests.
4. Add `/card <id>` handler + resolution logic + unit tests.
5. Add MarkdownV2/HTML fallback + integration tests.
6. Add source back-citation rendering + integration tests on cascade-in-flight scenarios.
7. Update `IMPLEMENTATION_STATUS.md`.
8. Verify lint-privacy clean (path inheritance from T6-04).
9. Run Phase 11 binding suite locally (sanity; T6-05 is NOT a privacy-touching ticket and should pass trivially).

---

END of T6-05 design.
