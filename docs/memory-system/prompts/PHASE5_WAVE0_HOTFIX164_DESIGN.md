# Phase 4 Hotfix — Implementation Design v3 FINAL

**Issue:** [#164](https://github.com/Jekudy/vibeshkoder/issues/164) — Phase 4 hotfix sprint after FHR found 3 production-blocking gaps + risk-audit found 6 cohort/audit-chain risks.
**Status:** SHIP-READY for Codex executor (post-Iteration-3, all Critic v2 + Risk v2 findings closed).
**Author:** ag-partner (mechanical merge of v2 + ADDENDUM v3).
**Date:** 2026-04-30.
**Single source of truth:** this document. v2 + ADDENDUM v3 + Critic v2 + Risk v2 are predecessors; do NOT re-read them — everything actionable is folded in below.
**Phase 5 boundary:** untouched. No LLM, no embeddings, no vector code.

---

## §0 — Executive summary

This hotfix closes Phase 4 by relocating v1 `MessageVersion` creation INTO `persist_message_with_policy` so it becomes the SOLE writer for `(chat_messages, message_versions, current_version_id)` triples. The single relocation closes:

- **CRITICAL 1** — live messages did not create v1 → `current_version_id IS NULL` → search JOIN drops every new conversation.
- **CRITICAL 2** — imports created v1 but never set `current_version_id` → invariant #8 violated.
- **CRITICAL 3** — imports omitted `normalized_text` → empty FTS vectors.
- **Risk H2** — imported `#offrecord` rows skipped `chat_messages` + `OffrecordMark` entirely; live↔import audit symmetry broken.

Migration **023** closes the post-008/pre-hotfix legacy cohort. Risk H1 (live `ingestion_run_id` wiring) and Risk H3 (eval fixture bypass) are folded in because they touch the same provenance/coverage surface. Audit-chain threading (`raw_update_id`) is wired through both `recall_handler` and the dominant `save_chat_message` live path.

**Scope:** 1 migration, ~10 files, 15 commits, ~36 tests + 6 eval cases, ~1000 LOC. Single PR.

**Deferred (out of hotfix scope, tracked in §10):** raw-archive flag flip (ops decision), `telegram_updates.ingestion_run_id` backfill for pre-hotfix rows, normalization canonicalization (#153), middleware-based universal persistence (R-4), cascade worker race window for `target_type='message'`, qa_traces `query_text` retention review, cascade liveness in `/healthz`.

---

## §1 — Invariants binding

Verbatim from `docs/memory-system/HANDOFF.md` §1 ("Non-negotiable invariants"):

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.
10. Public wiki remains disabled until review / source trace / governance are proven.

### Binding table (v3 — final)

| Fix | Primary invariant | Secondary |
|---|---|---|
| **CRITICAL 1 — live v1 creation** | #4 — `chat_messages.current_version_id IS NULL` ⇒ search SQL JOIN drops the row ⇒ false abstention. | #1 (gatekeeper preserved); #3 (offrecord redaction composes with v1 creation). |
| **CRITICAL 2+3 — import current_version_id + normalized_text** | #8 — same path means same surface. | #4 (citations to imported messages broken); audit chain via `raw_update_id`. |
| **qa_traces cascade layer** | #3 — forgotten user's queries cannot remain plaintext in audit table. | #9. |
| **Asymmetric refusal** | UX consistency; weakly tied to #1. | none. |
| **Router order / flag-OFF persistence** | #1 — current behavior is "persist every group message"; flag-OFF `/recall` silently swallows messages ⇒ contract violation. | #4 (citations require persistence). |
| **FTS partial index ratification** | #3 — defense-in-depth. | none. |
| **QaTrace ORM/migration drift** | none (cosmetic). | hides bugs in tests that rely on raw INSERTs. |
| **Migration 023 backfill (post-008 cohort)** | #4 — closes legacy live cohort that has `current_version_id IS NULL`. | #1 — preserves existing-message visibility post-hotfix-deploy. |
| **H1 ingestion_run_id wiring** | provenance (#9 + invariant #1's "audit chain" subclaim). | rollback / audit / GDPR export joinability. |
| **H2 imported-offrecord audit symmetry** | #8 (import = live observable state). | #3 (forget keyed by chat_id+message_id must work for imported offrecord rows too). |
| **H3 eval fixture through real path** | test-coverage integrity (no invariant directly, but test green = production green is a meta-invariant). | exposes regressions in CRITICAL 1 fix. |
| **NEW v3 — `chat_messages.save_chat_message` `raw_update_id` threading** | provenance #9; closes audit chain on the dominant live path. | invariant #1 (gatekeeper unchanged — same handler). |

### Interpretation note

The FHR + risk-audit findings collapse to **one structural gap**: the v1-creation responsibility was unassigned at PR #81 (deferred to "follow-up sprint" per `bot/services/message_persistence.py:16`). This hotfix relocates v1 creation INTO `persist_message_with_policy` so it is the SOLE writer for `(chat_messages, message_versions, current_version_id)` pairs — closing CRITICAL 1, CRITICAL 2, CRITICAL 3, and Risk H2 in one move. Migration 023 closes the existing cohort gap that the structural fix cannot retroactively reach.

---

## §2 — Architecture decision

**Decision: Option (A) — v1 creation lives INSIDE `persist_message_with_policy`, in the same transaction as `MessageRepo.save`.** Critic v1 ratified ("Architectural backbone of the design is sound"). Critic v2 confirmed.

### Why (A) wins

- Invariant #8 (import = live): one helper, two callers. Both fixed by one change.
- Atomicity: same advisory-lock-protected tx envelope.
- Privacy ordering: governance runs FIRST, v1 creation AFTER — `is_redacted_flag` already computed.
- Hot-path cost: +5–15ms p95.

### Where v1 sits for live offrecord

When `policy == "offrecord"`:
- `persist_text = persist_caption = None`, `is_redacted_flag = True`.
- v1 row: `text=None, caption=None, normalized_text=None, entities_json=None, is_redacted=True`.
- `content_hash = compute_content_hash(text=None, caption=None, message_kind=kind, entities=None)` — mirrors `bot/handlers/edited_message.py:361-367`.
- `current_version_id` IS still set; FK closed; search filters via `mv.is_redacted=False AND cm.memory_policy='normal'`.

### Where v1 sits for IMPORTED offrecord (closes Risk H2)

Today's import path (`bot/services/import_apply.py:535-540`) when `policy == "offrecord"`:
1. Sets `raw_row.is_redacted = True, redaction_reason = "offrecord"`.
2. Bumps `report.skipped_governance_count`.
3. Returns. **No `chat_messages`, no `message_versions`, no `offrecord_marks`**.

Live path creates all three. Invariant #8 violated.

**Fix:** import path delegates to `persist_message_with_policy` for offrecord too, mirroring live. Specifically: remove the early-return on `policy == "offrecord"` at lines 535-540 (keep `raw_row.is_redacted = True` and the counter, but DO continue — call `persist_message_with_policy` with `source="import"`). The helper itself handles offrecord internally: writes `chat_messages` row with `memory_policy="offrecord"`, NULLs content, creates `OffrecordMark`, creates redacted v1 with `is_redacted=True`.

Mechanically: **deletion of 6 lines** (the early-return block) plus rewiring the counter. The helper's existing offrecord logic (line 91-141 of `message_persistence.py`) handles everything else.

---

## §3 — Component-by-component spec

### 3.1 — CRITICAL 1: live v1 version creation

**File:** `bot/services/message_persistence.py`.

**Function signature (v3 — finalized with `captured_at` kwarg from ADDENDUM):**

```python
async def persist_message_with_policy(
    session: AsyncSession,
    message: Any,
    *,
    raw_update_id: int | None = None,
    source: Literal["live", "import"] = "live",
    captured_at: datetime | None = None,    # NEW v3 (ADDENDUM OVERRIDE for §3.9 eval rewrite)
) -> PersistResult:
```

`captured_at=None` → `MessageRepo.save` and `MessageVersionRepo.insert_version` both fall back to their existing default behavior (server_default `func.now()` for the v1 row; whatever `MessageRepo.save` does today for `chat_messages.captured_at`). When non-None, both rows take the explicit value. Required for eval recency-sensitive cases (rec_002 / rec_005 / rec_009 / rec_010).

**Pseudo-code (v3 — applies entities-unified + captured_at + raw_update_id threading):**

```python
# (existing steps 1–6 unchanged: advisory lock, governance, MessageRepo.save with captured_at, optional offrecord_mark)

# 7. Build v1 content fields per redaction policy.
if is_redacted_flag:
    v1_text = None
    v1_caption = None
    v1_normalized_text = None
    v1_entities_list = None
    v1_entities_json = None
else:
    v1_text = persist_text
    v1_caption = persist_caption
    v1_normalized_text = persist_text                            # raw text now; canonicalization is #153
    v1_entities_list = _extract_entities_unified(message)        # NEW v3 — single helper merges entities + caption_entities
    v1_entities_json = json.dumps(v1_entities_list) if v1_entities_list else None

# 8. Compute content_hash (chv1 recipe: text, caption, message_kind, entities).
v1_content_hash = compute_content_hash(
    text=v1_text,
    caption=v1_caption,
    message_kind=normalized["message_kind"],
    entities=v1_entities_list,                                   # SAME unified list — symmetric with v1_entities_json
)

# 9. Insert v1 (idempotent on (chat_message_id, content_hash)).
v1 = await MessageVersionRepo.insert_version(
    session,
    chat_message_id=saved.id,
    content_hash=v1_content_hash,
    text=v1_text,
    caption=v1_caption,
    normalized_text=v1_normalized_text,
    entities_json=v1_entities_json,
    edit_date=None,                                              # v1 is NOT an edit
    raw_update_id=raw_update_id,
    is_redacted=is_redacted_flag,
    imported_final=(source == "import"),
    captured_at=captured_at,                                     # NEW v3 (ADDENDUM)
)

# 10. Close FK loop on chat_messages (idempotent guard for retry-races).
if saved.current_version_id != v1.id:
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id == saved.id)
        .values(current_version_id=v1.id)
    )
    await session.flush()
    saved.current_version_id = v1.id

# 11. (existing) marks already created in step 6.
return PersistResult(saved, policy, mark_created)
```

**Imports change:** add `from sqlalchemy import update` and `from datetime import datetime` (for type hint) to the top of `bot/services/message_persistence.py`. Verified absent in current file.

**Atomicity:** if `MessageRepo.save` raises before step 9, no v1 row is created (existing all-or-nothing semantics preserved). The savepoint inside `MessageVersionRepo.insert_version` is nested in the SAME outer tx — savepoint cannot commit independently of the outer tx.

**`MessageRepo.save` `captured_at`:** the helper's existing `MessageRepo.save(...)` call must also accept `captured_at` per ADDENDUM (verified that the duck `message.date` resolution path already handles override; if not, plumb explicitly). Symmetry between chat_messages.captured_at and message_versions.captured_at is what eval rec_005 / rec_009 (tombstone tie-break) depends on.

**Footnote A — `normalized_text`:** ship `v1_normalized_text = persist_text` (raw text). Mirrors the currently-deployed tsv source (`coalesce(text,'') || ' ' || coalesce(caption,'')`). Hardening #153 may switch tsv source to a canonicalize_text() function in a future sprint without data migration.

**Footnote B — entities helper unification (ADDENDUM OVERRIDE):** define `_extract_entities_unified(message)` in `bot/services/normalization.py` as a public function. The helper merges `message.entities` and `message.caption_entities` (deduplicated by `(offset, length, type)`) into a single list and returns it. Both call sites (live persist v1 build + edit handler chv1 hash compute) use this single helper. The legacy `_build_entities_json` and `_extract_entities_list` in `bot/handlers/edited_message.py:71-93` are DELETED (the asymmetry between them — caption_entities ignored vs. fallback — has no semantic justification).

**chv1 hash impact for caption-only-photo with caption_entities:** legacy `_build_entities_json` returned `[]` for these messages; unified returns the actual entities list. For NEW messages this is fine (no historical hash to match). For migration 023 backfill: 023 walks rows with `current_version_id IS NULL` — those rows have NO existing v1, so chv1 hash is computed fresh from the unified helper. **No data conflict.**

#### Edge cases (v3)

| Case | Behavior | Mechanism |
|---|---|---|
| **Telegram retry (duplicate message_id)** | UPSERT idempotent; insert_version idempotent on `(chat_message_id, content_hash)`; UPDATE no-op via `if saved.current_version_id != v1.id` guard. | Existing UPSERT + repo idempotency; explicit guard. |
| **Offrecord redaction on first insert** | Helper's existing redaction branch sets `is_redacted_flag=True`, content nulled. v1 inserted with `text=None, caption=None`, hash from null state. Search excludes via two filters. | Reuse line 91-95. |
| **Caption-only photo (with or without caption_entities)** | `persist_text=None, persist_caption="hello"`. v1: `text=None, caption="hello", normalized_text=None`. tsv = non-empty (`coalesce('','') || ' ' || 'hello'`). Searchable. With caption_entities, unified helper now includes them. | Unified helper + existing tsv generated column. |
| **Anonymous channel post** | `message.from_user is None`. `chat_messages.user_id = None` (column nullable). v1's `content_hash` does NOT consume user_id. | Existing nullable user_id; chv1 recipe unchanged. |
| **Forwarded message** | `kind="forward"` (per `normalization.py:29-47` priority order). chv1 hash includes `message_kind`. | chv1 recipe consumes message_kind. |
| **FK race: users.upsert hasn't run yet** | Helper called from chat_messages.py handler AFTER UserRepo.upsert. Import path: `import_user_map.resolve_export_user` runs before persist. No race. | Call-site discipline. |
| **Edit arrives for legacy message with no v1** | Edit handler at `edited_message.py:318-331` already handles via `existing_chv1` recompute + idempotency. After hotfix + migration 023, NEW messages always have v1; the post-008 cohort is closed by 023; pre-008 closed by 008. | Existing edit handler + new migration 023. |
| **Concurrent helper invocations** | Advisory lock on (chat_id, message_id); `MessageVersionRepo.insert_version` savepoint+IntegrityError. | Existing protections. |
| **Insert succeeds, UPDATE fails** | Same tx → rollback wipes both. Telegram redelivers, retry succeeds. | Atomic tx. |
| **Bot restart mid-helper-tx** | Outer tx never commits → connection close → PG rolls back → no orphan state. Telegram redelivers. | PG default + Telegram replay. |
| **forget_event arrives during persist** | Pre-existing race in cascade worker: forget for `chat_id+message_id` lands while helper is mid-tx → cascade UPDATE affects 0 rows. Helper commits later, leaving v1 unredacted. Hotfix slightly enlarges window by ~10ms (the v1 INSERT). **Documented in §7 R-10.** Mitigation deferred. | Race window slightly wider; correctness gap inherited not introduced. |
| **Stale `saved.current_version_id` on retry** | Comparison `None != v1.id` → True → UPDATE proceeds → no-op. Or `v1.id == v1.id` → False → UPDATE skipped. | Comparison logic + advisory lock serialization. |

#### Tests to add (`tests/services/test_message_persistence.py`)

1. `test_persist_creates_v1_with_current_version_fk` — normal text → v1 with `text=<msg>, version_seq=1, is_redacted=False, imported_final=False`; `current_version_id IS NOT NULL`.
2. `test_persist_offrecord_creates_redacted_v1` — text contains `#offrecord` → v1 with `text=None, caption=None, is_redacted=True`; hash matches `compute_content_hash(None, None, kind, None)`.
3. `test_persist_caption_only_photo_creates_v1` — photo+caption → v1 with `text=None, caption=<c>`; tsv non-empty.
4. `test_persist_idempotent_on_telegram_retry` — call twice → 1 v1, stable `current_version_id`, `version_seq=1`.
5. `test_persist_v1_under_advisory_lock_before_insert_version` — advisory_lock_chat_message called BEFORE `MessageVersionRepo.insert_version`.
6. `test_persist_v1_imported_final_flag_for_import_source` — `source="import"` → v1.imported_final=True.
7. `test_persist_failure_rolls_back_v1` — patch insert_version to raise → no chat_messages row.
8. `test_persist_concurrent_two_tasks_one_v1` — `asyncio.gather` two helper invocations against same (chat_id, message_id) → exactly one v1 row, advisory lock serialization observable.
9. `test_persist_anonymous_channel_post_creates_v1` — `message.from_user=None` → v1 created, `chat_messages.user_id=None`, hash equals same content from known user.
10. `test_persist_forwarded_text_creates_v1_with_kind_forward` — forwarded text → v1 with `kind="forward"`, hash differs from native equivalent.
11. **NEW v3** `test_persist_threads_captured_at_to_v1` — pass `captured_at=datetime(2025,1,1,tzinfo=UTC)` → both `chat_messages.captured_at` and `message_versions.captured_at` match the override.
12. Regression: `test_persist_normal_text_message_returns_result` continues passing.

#### Tests in `tests/services/test_normalization.py`

13. **NEW v3** `test_extract_entities_unified_merges_caption_entities` — caption-only photo with `caption_entities=[bold(0,5)]`, no `entities` → unified returns `[bold]`. Asserts the previous asymmetric `_build_entities_json` would have returned `[]` (counter-fixture). Caption-only-photo + text + caption_entities + entities deduplicated case included.

#### Tests in `tests/db/test_message_version_repo.py`

14. **NEW v3** `test_insert_version_with_explicit_captured_at_overrides_default` — pass `captured_at=datetime(2025,1,1,tzinfo=UTC)` → row's `captured_at` matches the override (not `now()`).

---

### 3.2 — CRITICAL 2 + 3: import current_version_id + normalized_text + H2 imported-offrecord symmetry

**File:** `bot/services/import_apply.py`.

**Decision:** because the helper now creates v1 for `source="import"` automatically, **delete the manual `MessageVersionRepo.insert_version` block at lines 562-590**, AND **delete the offrecord early-return at lines 535-540**, AND **add an explicit live-overlap pre-check before calling the helper**. Result: one helper call serves all three import cases (normal, offrecord, overlap-with-live). Counters branch on the pre-check + helper return.

**Pseudo-code (replaces import_apply.py:535-590):**

```python
# (steps 1-7 of import_apply.run_apply_one_message unchanged: parse, dedup, governance detect_policy)

# 8. Do NOT short-circuit on policy == "offrecord". The helper handles offrecord
#    internally (writes chat_messages with memory_policy='offrecord', creates OffrecordMark,
#    creates redacted v1) — restoring import↔live audit symmetry per invariant #8 and
#    closing risk-audit H2.
if policy == "offrecord":
    raw_row.is_redacted = True
    raw_row.redaction_reason = "offrecord"
    # IMPORTANT: do NOT return here. Continue to step 9.
    report.skipped_governance_count += 1   # Counter retained for operator dashboards.

# 9. Build the message duck.
duck = _build_message_duck(...)

# 10. Explicit overlap pre-check BEFORE persist (closes Critic H-2).
#     The pre-check queries chat_messages for (chat_id, message_id). If a live row exists
#     (raw_update_id is non-null AND points at a live telegram_updates row, i.e. update_id
#     IS NOT NULL on the joined raw row), skip helper invocation entirely — live is
#     authoritative.
is_overlap = await _check_live_overlap_pre_persist(
    session, chat_id=chat_id, message_id=msg_id, current_import_raw_update_id=raw_row.id
)
if is_overlap:
    report.skipped_overlap_count += 1
    return msg_id

# 11. Single helper call — handles normal AND offrecord paths uniformly.
persist_result = await persist_message_with_policy(
    session,
    duck,
    raw_update_id=raw_row.id,
    source="import",
    captured_at=duck.date,                                   # import preserves export captured_at
)

# 12. Counter branching.
if persist_result.policy != "offrecord":
    report.applied_count += 1
# offrecord case: report.skipped_governance_count was already bumped in step 8.

return msg_id
```

**New helper `_check_live_overlap_pre_persist`** (private, in `import_apply.py`):

```python
async def _check_live_overlap_pre_persist(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    current_import_raw_update_id: int,
) -> bool:
    """Return True if a LIVE chat_messages row already exists for (chat_id, message_id).

    Live rows are identified by:
      chat_messages.raw_update_id → telegram_updates row WHERE update_id IS NOT NULL.
    Synthetic import raw rows (telegram_updates.update_id IS NULL) are NOT treated
    as live overlaps; they are prior import runs and handled by the dup-check earlier.
    """
    stmt = (
        select(ChatMessage.id)
        .join(TelegramUpdate, TelegramUpdate.id == ChatMessage.raw_update_id)
        .where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.message_id == message_id,
            TelegramUpdate.update_id.is_not(None),                      # live, not synthetic-import
            ChatMessage.raw_update_id != current_import_raw_update_id,  # not us
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None
```

One extra SELECT per imported message — acceptable for the apply path.

**Tests (`tests/services/test_import_apply.py`):**

1. `test_import_apply_sets_current_version_id` — apply fixture → `chat_messages.current_version_id IS NOT NULL` and points at imported v1.
2. `test_import_apply_v1_normalized_text_populated` — normalized_text non-NULL → tsv non-empty.
3. `test_import_apply_searchable_via_recall` — e2e: apply → search returns hit citing imported v1.
4. `test_import_apply_overlap_with_live_v1_skips_via_pre_check` — pre-seed live chat_message + live v1; apply import with same (chat_id, message_id) → exactly one v1 (live's), `imported_final=False`, `report.skipped_overlap_count == 1`.
5. `test_import_apply_overlap_with_whitespace_divergence_still_skips` — pre-seed live "hello world"; apply import "hello world\n" → import skipped per pre-check (NOT created as v2). Asserts contract holds even when content_hash differs.
6. `test_import_apply_offrecord_creates_chat_message_and_mark` — apply fixture with `#offrecord` → `chat_messages` row exists with `memory_policy='offrecord', text=NULL`; `OffrecordMark` row exists; `MessageVersion` row exists with `is_redacted=True, text=NULL`; `report.skipped_governance_count == 1` and `report.applied_count` not bumped.
7. `test_import_apply_offrecord_then_forget_by_message_id_works` — apply offrecord import; issue forget_event with `target_type='message', target_id=<chat_message.id>`; run cascade → `_find_chat_message` returns the row, NOT None.
8. Regression: `test_import_apply_idempotent_rerun` — rerun produces zero new rows.

---

### 3.3 — qa_traces cascade layer

**File:** `bot/services/forget_cascade.py` + tests.

**Pseudo-code:**

```python
# At module scope, _LAYER_FUNCS now includes qa_traces in the literal (NOT post-hoc assignment).
_LAYER_FUNCS: dict[str, Any] = {
    "chat_messages": _cascade_chat_messages,
    "message_versions": _cascade_message_versions,
    "qa_traces": _cascade_qa_traces,                    # NEW (literal entry, not dynamic registration)
}

# CASCADE_LAYER_ORDER updated:
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "qa_traces",                                        # NEW
    "message_entities",                                 # Phase 4+ (skipped via _SKIP_LAYERS)
    "message_links",
    "attachments",
    "fts_rows",
)

# Per-layer applicability set.
# qa_traces only applies to user-targeted forgets. For target_type='message' or
# 'message_hash', the dispatcher pre-filters and records {"status":"completed","rows":0}
# WITHOUT calling _cascade_qa_traces.
_LAYER_APPLICABLE_TARGET_TYPES: dict[str, frozenset[str]] = {
    "qa_traces": frozenset({"user"}),                   # users only
    # other layers: no entry → applies to all target_types (preserves existing behavior)
}


async def _cascade_qa_traces(session: AsyncSession, event) -> int:
    """Redact qa_traces.query_text for the forget_event's user.

    Per ADR-0003, preserves the row (audit) but nulls query content and flips
    query_redacted=True. Idempotent: re-runs find no un-redacted rows for the user.

    Pre-condition: dispatcher has verified event.target_type == 'user' (see
    _LAYER_APPLICABLE_TARGET_TYPES). This function does not re-validate.
    """
    if event.target_id is None:
        raise ValueError("forget_event target_type='user' requires non-None target_id")

    try:
        telegram_id = int(event.target_id)
    except (TypeError, ValueError):
        raise ValueError(f"target_id must be integer telegram_id; got {event.target_id!r}")

    stmt = (
        update(QaTrace)
        .where(
            QaTrace.user_tg_id == telegram_id,
            QaTrace.query_redacted == False,             # idempotency guard
        )
        .values(
            query_text=None,
            query_redacted=True,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount or 0


# Dispatcher change (in _process_one_event):
# BEFORE calling _LAYER_FUNCS[layer], check applicability:
applicable = _LAYER_APPLICABLE_TARGET_TYPES.get(layer)
if applicable is not None and event.target_type not in applicable:
    cascade_status[layer] = {"status": "completed", "rows": 0, "reason": "not_applicable"}
    continue
rows = await _LAYER_FUNCS[layer](session, event)
cascade_status[layer] = {"status": "completed", "rows": rows}
```

**`evidence_ids` policy:** intentionally NOT scrubbed (Critic ratified OQ-4). Phase 5+ privacy review may revisit.

**Idempotency:** `WHERE query_redacted == False` is the guard. Re-runs are no-ops.

**Tests:**

1. Flip `tests/db/test_qa_trace.py:77 test_forget_me_cascade_redacts_query` from `xfail` to passing.
2. `test_qa_trace_cascade_idempotent` — second run reports 0 rows.
3. `test_qa_trace_cascade_message_target_skips` — `target_type='message'` event → `qa_traces` layer reports `{"status":"completed","rows":0,"reason":"not_applicable"}` via dispatcher pre-filter.
4. `test_qa_trace_cascade_only_affects_target_user` — two users, forget A → only A's traces redacted.
5. `test_qa_trace_cascade_redacts_query_user_with_existing_message_traces` — user with 5+ qa_traces rows; forget_me → all 5 redacted in single UPDATE.

---

### 3.4 — Asymmetric `/recall` refusal

**File:** `bot/handlers/qa.py:124-127`.

**Decision:** unify to "always reply with same refusal text in non-community chats" (DM and other groups alike).

**Pseudo-code:**

```python
# Replace lines 124-128 with:
if message.chat.id != settings.COMMUNITY_CHAT_ID:
    try:
        await message.reply("Команда /recall работает только в community чате.")
    except TelegramForbiddenError:
        # Bot lacks can_send_messages in this chat (e.g., kicked, restricted).
        # Audit-only path: still record the abstain trace, do not raise.
        logger.info(
            "recall refused: bot lacks send permission",
            extra={"chat_id": message.chat.id, "user_id": getattr(message.from_user, "id", None)},
        )
    await audit_empty()
    return
```

**Imports needed:** `from aiogram.exceptions import TelegramForbiddenError`. Verify present.

**Tests:**

- `test_recall_in_non_community_group_replies_and_audits` — supergroup with `chat.id != COMMUNITY_CHAT_ID` → reply called + qa_traces row with `abstained=True`.
- `test_recall_in_non_community_group_handles_forbidden` — patch reply to raise TelegramForbiddenError → no exception escapes; qa_traces row still created.
- Existing `test_recall_in_dm_replies` continues passing.

---

### 3.5 — Router order / flag-OFF persistence (Critic v1 C-1 fix + raw_update threading)

**File:** `bot/handlers/qa.py` (top of `recall_handler`); `bot/middlewares/raw_update_persistence.py`.

**Verified at `bot/__main__.py:115-117`:**
```
115: dp.include_router(qa.router)             # registered FIRST
117: dp.include_router(chat_messages.router)  # registered SECOND
```

aiogram routes by first match. `Command("recall")` filter on qa.router matches. **No other handler currently consumes `/recall`** (verified by grep). Persist inside qa handler is the right place because qa is the unique consumer.

**To be robust against future handler additions**, persist call is explicit and guarded; if another handler ever shadows qa.router's `Command("recall")`, the system fails loud (logged warning + skipped persist) rather than silently dropping. Architectural fix (R-4 middleware-based universal persistence) remains a Phase 4 hardening follow-up.

**Pseudo-code (addresses Critic v1 C-1 raw_update_id threading):**

```python
@router.message(Command("recall"))
async def recall_handler(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    raw_update: TelegramUpdate | None = None,        # NEW: surfaced from middleware data
    **data: Any,
) -> None:
    # Persist the /recall message itself FIRST, regardless of feature-flag state.
    # This closes the silent-drop hole when memory.qa.enabled=False.
    #
    # NOTE: this is the ONLY handler that consumes Command("recall"); see qa.py and
    # bot/__main__.py:115-117 for router ordering rationale. If a future handler
    # registers a Command("recall") filter BEFORE qa.router, that handler MUST
    # call persist_message_with_policy itself or we lose the archive contract.
    if (
        message.chat.id == settings.COMMUNITY_CHAT_ID
        and message.from_user is not None
    ):
        await UserRepo.upsert(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        await persist_message_with_policy(
            session,
            message,
            raw_update_id=raw_update.id if raw_update is not None else None,
            source="live",
        )

    # Existing flag check + handler body (unchanged from current qa.py:103+)
    if not await FeatureFlagRepo.get(session, QA_FEATURE_FLAG):
        return
    # ... rest of recall_handler
```

**`raw_update` middleware surfacing:** the `RawUpdatePersistenceMiddleware` at `bot/middlewares/raw_update_persistence.py:55` calls `record_update(session, event)` and discards the return. Modify to store the persisted row in aiogram's `data` dict:

```python
# bot/middlewares/raw_update_persistence.py — modified __call__:
async def __call__(self, handler, event, data):
    if isinstance(event, Update):
        session = data.get("session")
        live_run_id = data.get("live_ingestion_run_id")    # NEW v3 — surfaced from dp["..."] (see §3.8)
        if session is not None:
            try:
                async with session.begin_nested():
                    raw_row = await record_update(session, event, ingestion_run_id=live_run_id)
                    if raw_row is not None:
                        data["raw_update"] = raw_row              # NEW
            except SQLAlchemyError as exc:
                logger.warning(...)
    return await handler(event, data)
```

If `record_update` returns None (raw-archive flag OFF), `data["raw_update"]` is never set → handler gets the default None → graceful degradation.

**Tests:**

- `test_recall_with_flag_off_still_persists_message_and_threads_raw_update_id` — flag OFF, send `/recall foo` in community → chat_messages row has `text="/recall foo"`, v1 created, `chat_messages.raw_update_id` matches the middleware-persisted telegram_updates row.
- `test_recall_with_flag_on_persists_and_responds` — flag ON → message persisted AND qa response rendered AND qa_traces row.
- `test_recall_in_dm_does_not_persist` — DM `/recall foo` → no chat_messages row (gated by `chat.id == COMMUNITY_CHAT_ID`).
- `test_recall_with_flag_off_creates_no_qa_trace` — flag OFF, `/recall foo` → chat_messages persisted BUT qa_traces table empty (no audit_empty call when flag is OFF — early `return` happens before audit_empty).

---

### 3.6 — Secondary: FTS partial index ratification

**File:** `alembic/versions/020_add_message_version_fts_index.py` (existing).

**Decision:** accept the deviation; do NOT change. Both shipped behaviors are correct in practice (cascade nulls `normalized_text` → `search_tsv` recomputes to empty for redacted rows).

**Documentation update:** add a section to `docs/memory-system/PHASE4_PLAN.md` §5.A noting the ratified deviation and the cascade-coupling rationale. (Bundled into commit 15 docs.)

---

### 3.7 — Secondary: QaTrace ORM/migration drift

**File:** `bot/db/models.py:514-518`.

**Decision:** add `server_default=text("'[]'::jsonb")` to ORM column declaration to mirror migration 022.

```python
evidence_ids: Mapped[list[int]] = mapped_column(
    JSON().with_variant(JSONB(), "postgresql"),
    nullable=False,
    default=list,
    server_default=text("'[]'::jsonb"),     # NEW: align ORM with migration 022
)
```

**Test:**

```python
# tests/db/test_qa_trace.py
async def test_qa_trace_evidence_ids_default_via_raw_insert(db_session):
    """Insert via raw SQL (no ORM defaults), assert PG server_default produces []."""
    result = await db_session.execute(text(
        "INSERT INTO qa_traces (user_tg_id, chat_id, query_redacted, abstained, created_at) "
        "VALUES (:uid, :cid, false, false, now()) RETURNING evidence_ids"
    ), {"uid": _next_user_id(), "cid": _next_chat_id()})
    row = result.scalar_one()
    assert row == []
```

---

### 3.8 — Risk H1: Live ingestion_run_id wiring

**Files:** `bot/__main__.py` (startup hook), `bot/middlewares/raw_update_persistence.py`, `bot/services/ingestion.py` (no change — already accepts the kwarg).

**Risk Audit H1:** `record_update` at line 55 of the middleware is called WITHOUT `ingestion_run_id`. Function default is None. Result: every live `telegram_updates` row has `ingestion_run_id=NULL`.

**Fix:**

1. **At startup (`bot/__main__.py::on_startup`)**, open a session and call `await get_or_create_live_run(session)` once. Cache the returned `IngestionRun.id` on the dispatcher's workflow data:

```python
# bot/__main__.py — inside on_startup:
async with async_session_maker() as session:
    live_run = await get_or_create_live_run(session)
    await session.commit()
    dp["live_ingestion_run_id"] = live_run.id    # accessible via data dict in middlewares
```

2. **In `RawUpdatePersistenceMiddleware.__call__`**, read the cached id from `data` and pass it to `record_update` (already wired in §3.5 patch above; same single middleware change covers both `data["raw_update"]` and `ingestion_run_id`).

3. **No change needed in `record_update`** — verified at `ingestion.py:114` it already accepts `ingestion_run_id` kwarg.

**Concurrency note:** `get_or_create_live_run` documents the single-instance assumption (`ingestion.py:99-103`). Hotfix preserves it.

**Production dependency (Risk N2):** `bot/services/ingestion.py:136-137` returns None when `memory.ingestion.raw_updates.enabled=false`. Migration 003 does NOT seed this flag → it defaults False. With the flag OFF, `record_update` short-circuits → no `telegram_updates` rows created → all wiring is dormant. **The hotfix does NOT flip this flag**; ops decision. Documented in `docs/runbook.md` (commit 15).

**Tests:**

- `test_live_ingestion_run_created_on_startup` — invoke `on_startup` → assert exactly one `IngestionRun` row with `run_type='live', status='running'`.
- `test_record_update_threads_live_run_id_when_provided` — middleware run with `live_ingestion_run_id` in data → telegram_updates row has the run id.
- `test_record_update_idempotent_on_repeated_startup` — call `get_or_create_live_run` twice → returns same row, no duplicates.

---

### 3.9 — Risk H3: Eval fixture goes through real path (with import-path coverage)

**File:** `tests/eval/test_qa_eval_cases.py`, `tests/fixtures/qa_eval_cases.json`.

**Risk Audit H3:** lines 39-67 hand-build `ChatMessage` + `MessageVersion` + manually set `current_version_id`. This bypasses `persist_message_with_policy` — the very function CRITICAL 1 says is broken.

**Fix:** rewrite the fixture builder so it dispatches by case prefix:
- `live_*` cases → `_seed_case_via_real_path` calls `persist_message_with_policy(session, <ducked message>, source="live", captured_at=<from fixture>)` for each message.
- `imp_*` cases → `_seed_case_via_import_path` calls `import_apply.run_apply` end-to-end with a synthetic Telegram Desktop export fixture.

**Recency-sensitive cases (rec_002, rec_005, rec_009, rec_010):** seed function reads `captured_at` from each fixture row and passes through to the helper. With ADDENDUM `captured_at` kwarg now plumbed through `persist_message_with_policy → MessageVersionRepo.insert_version`, all 12 cases pass without hand-built rows.

**Acceptance criteria:**

1. After CRITICAL 1 + 2 + 3 fixes land, the rewritten fixture rebuilds eval data via real path.
2. Re-run eval suite → all 12 existing cases pass + 2 new import-mode cases pass = 14 total.
3. Without the CRITICAL 1 fix, the rewritten fixture would produce `current_version_id IS NULL` rows and eval cases would fail. Inverse-test proves eval now meaningfully exercises the production path.

**Inline assertion in seed function:**

```python
async def _seed_case_via_real_path(db_session, case):
    for msg_dict in case["messages"]:
        duck = _build_eval_message_duck(msg_dict)
        result = await persist_message_with_policy(
            db_session,
            duck,
            source="live",
            raw_update_id=None,
            captured_at=msg_dict.get("captured_at"),       # preserves recency fixtures
        )
        assert result.chat_message.current_version_id is not None, (
            f"persist_message_with_policy did not set current_version_id "
            f"for case={case['id']}, msg={msg_dict.get('id')} — CRITICAL 1 regression."
        )


async def _seed_case_via_import_path(db_session, case):
    """Drive the full import_apply pipeline for an imp_* case."""
    # Build synthetic single-chat TD export from case["messages"].
    export_path = _build_synthetic_td_export(case)
    # Drive run_apply (idempotent, real path; uses _check_live_overlap_pre_persist).
    report = await import_apply.run_apply(db_session, export_path, chat_id=case["chat_id"])
    assert report.applied_count + report.skipped_governance_count == len(case["messages"])
    # Verify imported rows are searchable (CRITICAL 2 + 3 + H2 inverse-test).
    for msg in case["messages"]:
        if msg.get("policy") == "offrecord":
            continue
        cm = await ChatMessageRepo.get_by_chat_message_id(
            db_session, chat_id=case["chat_id"], message_id=msg["id"]
        )
        assert cm is not None
        assert cm.current_version_id is not None, (
            f"imported message {msg['id']} did not set current_version_id "
            f"— CRITICAL 2 regression."
        )
```

**Two new eval cases in `qa_eval_cases.json`:**

1. `imp_001_basic_text` — single text message imported via real `import_apply.run_apply` path → searchable via /recall.
2. `imp_002_offrecord_abstain` — import message with `#offrecord` → /recall abstains (validates H2 fix).

**Plus existing scope per v2 §5:**

- +3 cases for live imported-shape messages (recency / edits).
- +1 case for offrecord (must abstain).
- +1 case for forgotten (must abstain).

---

### 3.10 — NEW v3: `chat_messages.save_chat_message` + `edited_message.py` raw_update_id threading (Risk N3)

**Files:** `bot/handlers/chat_messages.py`, `bot/handlers/edited_message.py`.

**Risk Audit N3 / Critic v2 M-3:** `bot/handlers/chat_messages.py:44` calls `persist_message_with_policy(session, message)` WITHOUT `raw_update_id`. Every live group message has `chat_messages.raw_update_id = NULL`. This is the dominant volume code path; without this patch, audit chain remains broken regardless of qa-handler fix.

**Patch (chat_messages.py):**

```python
# bot/handlers/chat_messages.py::save_chat_message — modified signature
async def save_chat_message(
    message: Message,
    session: AsyncSession,
    raw_update: TelegramUpdate | None = None,    # NEW
) -> None:
    ...
    result = await persist_message_with_policy(
        session,
        message,
        raw_update_id=raw_update.id if raw_update is not None else None,    # NEW
    )
```

**Patch (edited_message.py):** the analogous `MessageVersionRepo.insert_version` call site at line ~384 also lacks `raw_update_id`. Add the same threading:

```python
# bot/handlers/edited_message.py — line ~384 (chv2+ insert)
async def edited_message_handler(
    message: Message,
    session: AsyncSession,
    raw_update: TelegramUpdate | None = None,    # NEW
) -> None:
    ...
    new_version = await MessageVersionRepo.insert_version(
        session,
        chat_message_id=existing.id,
        ...
        raw_update_id=raw_update.id if raw_update is not None else None,    # NEW
    )
```

aiogram unpacks `data` dict into handler kwargs by name. The middleware (§3.5 patch) sets `data["raw_update"]` when `record_update` returns non-None. When raw-archive flag is OFF, `data["raw_update"]` stays unset → handler default None → graceful.

**Test (`tests/handlers/test_chat_messages.py`):**

- `test_save_chat_message_threads_raw_update_id` — middleware sets `data["raw_update"]`, handler invoked, assert `chat_messages.raw_update_id` matches the persisted telegram_updates row's id.
- (Optional, if cheap:) `test_edited_message_threads_raw_update_id` — same for the edit path.

---

## §4 — Migration plan

**One new migration: `alembic/versions/023_backfill_v1_live_post_t1_07.py`.**

**Revision constants:**
- `revision = "023"`
- `down_revision = "022_add_qa_traces"` (string-match the actual revision id in `alembic/versions/022_add_qa_traces.py` — verify before commit)

**Why 023, not 009:** `alembic/versions/009_bind_application_invites.py` already owns revision 009 (CRIT-01, merged 2026-04-27). Verbatim 009 would break `alembic upgrade head`. 023 is next free after the current head 022.

**Why 023-as-separate-revision (not a re-stamp of 008):** separate alembic revision for audit-history clarity (deploy attributes the post-008 backfill to the hotfix release rather than re-stamping 008). 008 is idempotent on `WHERE current_version_id IS NULL` already; 023 walks the same WHERE and produces the same result. Treating it as a fresh revision keeps the hotfix's data intervention visible in `alembic_version` history.

**Mechanics (mirror migration 008's body verbatim, no date filter):**

```python
"""Backfill v1 MessageVersion for chat_messages with current_version_id IS NULL (post-008 cohort).

Created post-T1-07 deploy of v1 backfill (008). This migration walks the same WHERE
clause as 008 to close the cohort of live messages persisted between 008 deploy and
the Phase 4 hotfix deploy (issue #164). Walks `chat_messages WHERE current_version_id
IS NULL`, with no date filter — 008 was idempotent on the same WHERE, so 023 produces
the same effect on any uncovered row regardless of ingestion date.
"""

revision: str = "023"
down_revision: str = "022_add_qa_traces"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade():
    bind = op.get_bind()
    # Walk chat_messages WHERE current_version_id IS NULL, batched per 008's pattern.
    # For each row: pg_advisory_xact_lock(hash(chat_id, message_id)),
    # SELECT FOR UPDATE the chat_messages row, compute chv1 hash from text/caption,
    # INSERT into message_versions (idempotent on (chat_message_id, content_hash)),
    # UPDATE chat_messages SET current_version_id = <new_v1.id>.
    # Mirror 008's per-row commit pattern (_BATCH_SIZE = 1000, advisory lock, FOR UPDATE).
    ...

def downgrade():
    # Forward-only data migration. v1 rows are not destroyed on rollback.
    pass
```

**Idempotency:** `MessageVersionRepo.insert_version` already idempotent on `(chat_message_id, content_hash)`. UPDATE current_version_id is no-op when already set. Re-running migration 023 produces zero net mutations.

**Operational note (pre-deploy step):** size of cohort needs to be measured BEFORE deploy. Operator query:

```sql
SELECT count(*) FROM chat_messages WHERE current_version_id IS NULL;
```

Realistic estimate (community chat, ~1000 msg/day, 008 deployed 2026-04-27, hotfix ETA 2026-05-01): ~2k–5k rows → migration runs in <30 seconds. If unexpectedly larger (>10k), batch with `IMPORT_APPLY_CHUNK_SIZE` semantics + `pg_sleep(0.1)` per batch. If >1M → ops decision (maintenance window).

**Hotfix bundles the migration code regardless of cohort size**; ops decides when to run `alembic upgrade head`. Reversibility: code reverts via `git revert`; data migration is forward-only (rollback leaves v1 rows in place — harmless, search SQL tolerates them).

### Other migrations

| Migration | Why not |
|---|---|
| ORM-only QaTrace fix | Schema already correct in 022. |
| 024 for tsv source | Out of scope; hardening #153. |
| MessageVersion.tsv ORM | Already declared via `MessageVersionSearchVectorExpression` (models.py:28-49). |
| `telegram_updates.ingestion_run_id` backfill | Out of scope; tracked in §10. |

---

## §5 — Test strategy

### Unit tests

| File | Tests |
|---|---|
| `tests/services/test_message_persistence.py` | +12 (incl. `test_persist_threads_captured_at_to_v1`) |
| `tests/services/test_normalization.py` | +1 (`test_extract_entities_unified_merges_caption_entities`) |
| `tests/db/test_message_version_repo.py` | +1 (`test_insert_version_with_explicit_captured_at_overrides_default`) |
| `tests/services/test_import_apply.py` | +8 |
| `tests/db/test_forget_cascade.py` | +5 |
| `tests/db/test_qa_trace.py` | flip 1 xfail; +1 server_default |
| `tests/handlers/test_qa.py` | +4 |
| `tests/handlers/test_chat_messages.py` | +1 (`test_save_chat_message_threads_raw_update_id`) |
| `tests/services/test_ingestion.py` | +3 (Risk H1) |

### Integration (`tests/integration/test_phase4_hotfix_e2e.py`)

1. **Live → recall:** group message → `/recall <token>` returns hit citing v1.
2. **Import → recall:** apply fixture → `/recall <token>` returns hit citing imported v1.
3. **Forget → recall:** send → forget_message → `/recall <token>` abstains.
4. **forget_me → qa_traces:** `/recall foo` → `/forget_me` → trace's `query_text IS NULL`.
5. **Flag-OFF → message archived:** flag OFF, `/recall foo` → chat_messages row exists with v1.
6. **Imported offrecord round-trip:** apply fixture with `#offrecord` → assert `chat_messages` row + `OffrecordMark` + redacted v1; `/recall <offrecord-content>` abstains.
7. **Live ingestion_run_id is set (raw-archive flag ON in test):** receive a live update via dev-bot → assert `telegram_updates.ingestion_run_id IS NOT NULL`.

### Eval test (Risk H3 + S3) — see §3.9

12 existing cases via real path + 2 new import-path cases (imp_001, imp_002) + offrecord + forgotten + edit cases per §5 nits = 14+ total.

### Performance gate

- Microbenchmark `tests/perf/test_persist_latency.py`. Skipped by default; run nightly.
- normal p95 < 80ms (absolute floor).
- offrecord p95 < 50ms (no GIN cost).

### Regression suite (must stay green)

- All existing `test_message_persistence.py` (11 tests).
- All existing `test_import_apply.py` (including `test_import_apply_idempotent_rerun`).
- `test_edited_message.py` — edit handler creates v(n+1) AFTER hotfix v1 (no behavior change).
- `test_message_version_repo.py` — repo idempotency unchanged.
- `tests/eval/test_qa_eval_cases.py` — passes after fixture rewrite.

---

## §6 — Rollout sequencing

**SINGLE PR**, branch `feat/p4-hotfix-164`, worktree `.worktrees/p4-hotfix-164` (already created). 15 commits in order:

1. **Refactor: unified entities helper.** Add `_extract_entities_unified(message)` to `bot/services/normalization.py`. Delete `_build_entities_json` and `_extract_entities_list` from `bot/handlers/edited_message.py`. Update edit handler call sites. +1 test (`test_extract_entities_unified_merges_caption_entities`).
2. **Repo signature extension.** `MessageVersionRepo.insert_version` accepts `captured_at: datetime | None = None`. +1 test.
3. **§3.1 — v1 in `persist_message_with_policy`.** Add `from sqlalchemy import update` import. Plumb `captured_at` kwarg through helper. +12 tests.
4. **§3.2 — import_apply.** Pre-check + delete inline insert_version + delete offrecord early-return. +8 tests.
5. **§3.3 — qa_traces cascade layer.** Flip xfail. Dispatcher applicability gate. +5 tests.
6. **§3.4 — asymmetric refusal.** Including TelegramForbiddenError handling. +2 tests.
7. **§3.5 — flag-OFF persistence in qa handler + middleware surfacing of `raw_update`.** +4 tests.
8. **§3.7 — QaTrace ORM `server_default`.** +1 raw-INSERT test.
9. **§3.10 — `chat_messages.py` + `edited_message.py` raw_update_id threading.** +1 test.
10. **§3.8 — live `ingestion_run_id` wiring.** Startup hook + middleware threading (already partially wired in commit 7's middleware patch — extend with `live_run_id` lookup). +3 tests.
11. **§3.9 — eval fixture rewrite via real path + import path.** Plumb per-case `captured_at`. Confirm all existing cases pass + 2 new import-path eval cases (`imp_001`, `imp_002`).
12. **Migration 023 — backfill v1 for post-008 cohort.** WHERE clause matches 008 (no date filter).
13. **Integration tests.** `tests/integration/test_phase4_hotfix_e2e.py` per §5 (7 scenarios).
14. **Eval cases.** `qa_eval_cases.json` +offrecord +forgotten +edit cases.
15. **Docs + tests evidence.** Update `PHASE4_PLAN.md` §0 (close hardening items) and §5.A (FTS deviation ratification per §3.6); `IMPLEMENTATION_STATUS.md` (#164 closed); `docs/runbook.md` (raw_updates flag verification step + post-revert migration note); §11 cross-stream note. PR body: paste pytest output, alembic dry-run output, microbench results.

### CI gates before merge

- Full pytest green. No new xfail.
- `alembic upgrade head` dry-run validates migration 023.
- mypy clean on new code.
- ruff clean.
- `tests/perf/test_persist_latency.py` informational (fail at p95 > 150ms).
- Manual: dev-bot per `DEV_SETUP.md` smoke — live message → v1 with current_version_id set; if `memory.ingestion.raw_updates.enabled=true` in test env, `telegram_updates.ingestion_run_id` non-NULL.
- **NEW v3:** verify hotfix PR doesn't trip new healing/healthcheck workflows (`feat/autonomous-healing` merged to main 2026-04-30 added `.github/workflows/healing.yml` + `healthcheck.yml`).

### Rollback path

- Code reverts via `git revert`.
- Migration 023: forward-only. v1 rows created by 023 stay in place. Harmless.
- qa_traces redacted by new cascade layer stay redacted (irreversibility doctrine).
- Feature flag `memory.qa.enabled=true` should be flipped OFF before code revert.
- **Post-revert risk:** new live messages written between revert and re-deploy will have `current_version_id IS NULL` and require a 024 backfill on re-deploy. Acceptable given hotfix urgency.

### Post-merge ops

- Run cohort sizing query: `SELECT count(*) FROM chat_messages WHERE current_version_id IS NULL`.
- Run `alembic upgrade head` on staging → verify migration 023 lands cleanly.
- (Ops decision, NOT in hotfix:) flip `memory.ingestion.raw_updates.enabled=true` if audit-chain is desired in production.
- Flip `memory.qa.enabled=true` only AFTER migration 023 has run on production.

---

## §7 — Risk register

| Risk | Likelihood | Severity | Mitigation | Detection |
|---|---|---|---|---|
| **R-1** Helper change breaks legacy fixtures | Medium | Low | Audit 3 known call sites; eval fixture rewrite covers test-side. | CI pre-merge. |
| **R-2** Hot-path latency regression > +15ms p95 | Low | Medium | Microbenchmark gate; normal vs offrecord broken out. | `test_persist_latency.py` nightly. |
| **R-3** Content_hash mismatch live↔import → phantom v2 | N/A — closed | N/A | `_check_live_overlap_pre_persist` + whitespace-divergence test. | Tests. |
| **R-4** qa_traces cascade redacts traces of returning users | Low | Low (intended) | Document in HANDOFF.md §10. | N/A — by design. |
| **R-5** Asymmetric refusal exposes bot in non-community groups | Very low | Low | TelegramForbiddenError swallow + Telegram rate limit. | Sentry / Coolify logs. |
| **R-6** Flag-OFF persistence in qa handler accidentally persists DMs | Low | Medium | Guard `chat.id == COMMUNITY_CHAT_ID` BEFORE persist; `test_recall_in_dm_does_not_persist`. | Test. |
| **R-7** ORM-only change → schema drift | Very low | Low | Existing tests cover both paths. | CI. |
| **R-8** Race live↔import on same (chat_id, message_id) | Low | Medium | Advisory lock + idempotency. Pre-check closes the documented contract. | Concurrency tests. |
| **R-9** Phase 5 starts before this lands | High → Low (with hotfix) | Critical | Hotfix BEFORE Phase 5 entry. | Phase 5 entry gate verifies live messages cite v1. |
| **R-10** forget_event arrives during persist; ~10ms widened race window | Low | Medium | Pre-existing race; widened by ~10ms. Documented; tracked as `phase4-hardening`. | Manual operator audit; future test in hardening sprint. |
| **R-11** Migration 023 cohort larger than expected → long-running DDL | Medium | Medium | Operator query before deploy; batched + idempotent migration body; ops can pause/resume. | Cohort query before deploy. |
| **R-12** Live ingestion_run_id wiring fails at startup → bot won't start | Very low | High | `get_or_create_live_run` is idempotent (read-then-create); startup errors propagate to operator. Roll back to non-wired code if startup hangs. | Coolify health check / log inspection. |
| **R-13** Imported offrecord rows now consume a chat_messages slot — bumps row count vs prior import semantics | Medium | Low | Operator dashboards (`report.skipped_governance_count` and `chat_messages` total) will show different absolute numbers post-hotfix. Documented. | One-time row-count comparison post-deploy. |
| **R-14 NEW v3** Raw archive flag is OFF in production → H1 wiring is dormant; `chat_messages.raw_update_id` and `telegram_updates.ingestion_run_id` stay NULL on all new rows | High (default state) | HIGH (documentation) | DO NOT claim "audit chain closed" in PR. Document explicitly in `docs/runbook.md` + PR description: "H1 closure is conditional on ops flipping `memory.ingestion.raw_updates.enabled=true`. Until flipped, all new rows have NULL raw_update_id and NULL ingestion_run_id." Add `phase4-hardening` ticket "operator runbook: flip raw-archive flag + verify telegram_updates inflow." | `SELECT count(*) FROM telegram_updates WHERE captured_at > now() - interval '1 day'` post-deploy. |
| **R-15 NEW v3** New CI workflows (healing.yml + healthcheck.yml from `feat/autonomous-healing` merge) may fail hotfix PR for unrelated reasons | Medium | Medium | Pre-PR check: review the new workflow definitions; ensure the hotfix doesn't trigger healing-system gates spuriously. | First CI run on the PR. |

---

## §8 — Cross-stream consistency

### Phase 5 compatibility

Hotfix is Phase 5's prerequisite. `EvidenceBundle.from_hits` + `qa_traces` + `current_version_id`-based search SQL all depend on this hotfix landing first.

### Phase 11 numbering conflict

`docs/memory-system/IMPLEMENTATION_STATUS.md:184/210/231` flags a numbering conflict: HANDOFF.md Phase 11 = Shkoderbench/evals; `PHASE11_PLAN_DRAFT.md` = expertise pages. **Hotfix does NOT touch Phase 11 territory.** Conflict tracked at `IMPLEMENTATION_STATUS.md:184` for human reconcile in a separate session.

### Stream E xfail flip

Mechanically remove `@pytest.mark.xfail` decorator at `tests/db/test_qa_trace.py:77-82`. Test body at lines 83+ passes once the cascade layer wires up.

### qa_eval_cases.json updates

3 import-shape live + 1 edit + 1 offrecord + 1 forgotten + 2 import-path = 8 new cases (live-shape uses real_path; imp_* uses import_path).

### #153 hardening dependency

Hotfix touches v1 creation; #153 touches tsv source (`text` → `normalized_text`). Independent.

---

## §9 — Codex executor instructions

The design is SHIP-READY. Codex should:

1. Read this design end-to-end before opening any files. **Do NOT re-read v2, ADDENDUM v3, Critic v2, or Risk v2** — everything is folded in here.
2. Verify the worktree exists: `.worktrees/p4-hotfix-164` on branch `feat/p4-hotfix-164`. If not, create it from main: `git worktree add .worktrees/p4-hotfix-164 -b feat/p4-hotfix-164 main`. Verify `.worktrees/` is in `.gitignore`.
3. Read these files BEFORE writing any code:
   - `bot/services/message_persistence.py` (current state — note line 16's "follow-up sprint" comment)
   - `bot/services/import_apply.py:520-600`
   - `bot/middlewares/raw_update_persistence.py`
   - `bot/handlers/qa.py:103-130`
   - `bot/handlers/chat_messages.py:30-60`
   - `bot/handlers/edited_message.py:71-93, 360-400`
   - `bot/__main__.py:60-150`
   - `bot/services/forget_cascade.py:230-280`
   - `bot/db/repos/message_version.py:40-70`
   - `bot/db/models.py:299-303` (MessageVersion.captured_at server_default)
   - `bot/db/models.py:514-518` (QaTrace.evidence_ids)
   - `bot/services/ingestion.py:99-140` (record_update + raw archive flag)
   - `alembic/versions/008_backfill_message_versions_v1.py` (template for 023)
   - `alembic/versions/022_add_qa_traces.py` (verify revision id for `down_revision`)
   - `tests/eval/test_qa_eval_cases.py:39-67` (current hand-built fixture)
4. Implement in the commit order listed in §6 (15 commits). One commit per logical unit; do not bundle.
5. Run the full test suite after each commit; do not advance with regressions.
6. For migration 023: copy `008_backfill_message_versions_v1.py` body, scope filter to `WHERE current_version_id IS NULL` (NO date filter), rename, set `revision="023"`, `down_revision="022_add_qa_traces"` (verify the actual revision id string in 022). Do NOT skip the per-row advisory lock.
7. For the eval fixture rewrite (commit 11): verify that the inline assertion in `_seed_case_via_real_path` would FAIL without the CRITICAL 1 fix (commits 1–3). Order is observable.
8. PR title: `fix(p4): hotfix #164 — v1 creation in persist helper + cohort backfill + audit-chain threading`.
9. PR body MUST include:
   - Pytest output (test counts pass/fail by file).
   - `alembic upgrade head` dry-run output against ephemeral pg.
   - Microbench p95 numbers (normal + offrecord).
   - **Explicit operator note:** "Post-hotfix audit chain (`chat_messages.raw_update_id`, `telegram_updates.ingestion_run_id`) is dormant until ops flips `memory.ingestion.raw_updates.enabled=true`. See `docs/runbook.md` for verification steps."
   - Closes: #164. Closes: #165 (FHR-blocked doc PR superseded by this hotfix).
10. Post-PR: dispatch Codex deep-spec-reviewer + deep-product-reviewer (independent contexts) per `CODEX_DUAL_AGENT_PATTERN`. Wait for APPROVE+CI green before `gh pr merge --rebase --delete-branch`. **NEVER use `--admin`** — fix CI failures, don't bypass branch protection.

---

## §10 — Deferred items (out of hotfix scope)

| Item | Reason | Tracker |
|---|---|---|
| **OQ-1 normalized_text canonicalization** (NFKC + lowercase + whitespace collapse) | Hardening #153; no data migration needed when introduced | Existing #153 |
| **OQ-4 evidence_ids privacy review** | Default = leave intact; revisit before Phase 5 closes | New issue, Phase 5+ privacy review |
| **OQ-5 R-4 middleware-based universal persistence** | Bigger blast radius than hotfix budget | New issue `phase4-hardening` |
| **R-10 cascade worker advisory lock for `target_type='message'`** | Pre-existing race, slightly widened; not a regression | New issue `phase4-hardening` |
| **Risk M1 qa_traces.query_text retention** | Design conversation, not a bug | New issue Phase 5+ privacy |
| **Risk M2 current_version_id silent break invariant check** | Defense-in-depth job; non-urgent | New issue `phase4-hardening` |
| **Risk M3 cascade worker liveness in /healthz** | Cheap, recommended NOT for hotfix scope discipline; **escalated by autonomous-healing consumer** | New issue `phase4-hardening` (priority MEDIUM-HIGH per Risk v2 S2) |
| **Risk M4 naming drift `redact_query`/`query_redacted`** | Cosmetic | New issue `phase4-hardening` |
| **Risk M5 020→021 deploy cost** | Operational; not code | Add to `docs/runbook.md` |
| **L1-L7 risk-audit observations** | File as separate issues | Each as new issue |
| **OQ-A Migration 023 cohort size measurement** | Operator query before deploy | Operator pre-deploy step |
| **Phase 11 numbering conflict reconcile** | Cross-phase doc work | Existing tracker `IMPLEMENTATION_STATUS.md:184` |
| **NEW v3: `telegram_updates.ingestion_run_id` backfill for pre-hotfix live rows** | Hotfix wires go-forward path; pre-hotfix rows retain `ingestion_run_id=NULL`. Acceptance: ops may run a one-shot `UPDATE telegram_updates SET ingestion_run_id = <live_run_id> WHERE ingestion_run_id IS NULL AND update_id IS NOT NULL` if rollback/audit queries on historical live ranges become necessary. | New issue `phase4-hardening` |
| **NEW v3: Raw archive feature flag default flip** | `memory.ingestion.raw_updates.enabled` defaults to false. H1 fix's `live_ingestion_run_id` plumbing AND H1's `raw_update_id` threading only take effect once ops flips this flag. Deploy notes (`docs/runbook.md`) MUST include 'verify raw_updates flag enabled in production before measuring H1 audit-chain effect.' | Operator decision + runbook update |

---

## §11 — Iteration changelog (v1 → v2 → v3)

### v3 (this doc) closures vs v2 + ADDENDUM

| Finding | Source | Resolution in v3 |
|---|---|---|
| **Migration 009 collision** (Critic v2 C-1 / Risk v2 N1) | CRITICAL | Renamed to **023** in §4; commit 12 in §6 references 023. |
| **Eval rewrite breaks recency cases** (Critic v2 H-1) | HIGH | `captured_at` kwarg added to `MessageVersionRepo.insert_version` + plumbed through `persist_message_with_policy`; eval seed reads per-case captured_at. Commits 2 + 3 + 11. |
| **Migration scope unjustified** (Critic v2 H-2 / M-1) | HIGH/MEDIUM | Date filter dropped in §4; rationale for separate revision (audit clarity, not behavior change) stated. |
| **Entities helpers asymmetry** (Critic v2 M-2) | MEDIUM | Unified into `_extract_entities_unified` in `bot/services/normalization.py`; legacy helpers in `edited_message.py` deleted. Commit 1. |
| **chat_messages.py raw_update_id threading missing** (Critic v2 M-3 / Risk v2 N3) | HIGH | NEW §3.10 + commit 9 in §6: explicit patch for `chat_messages.py` + `edited_message.py`. |
| **Eval rewrite doesn't cover import path** (Risk v2 S3) | MEDIUM | §3.9: `_seed_case_via_import_path` + 2 new `imp_*` eval cases (imp_001, imp_002). |
| **Raw archive flag OFF in production** (Risk v2 N2) | HIGH | Documented in §3.8 + R-14 in §7 + §9 PR body operator note + §10 deferred + commit 15 docs. NOT addressed in code (ops decision). |
| **`telegram_updates.ingestion_run_id` backfill for pre-hotfix rows** (Critic v2 M-3 secondary) | MEDIUM | §10 deferred. |
| **CI workflows from autonomous-healing merge** (Risk v2 S2 corollary) | LOW | §6 CI gates note + R-15 in §7. |
| **§3.6 docs in commit ordering** (Critic v2 L-1) | LOW | §6 commit 15 explicitly references §3.6 ratification note. |
| **Commit ordering migration position** (Critic v2 L-2) | LOW | Documented post-merge ops in §6: cohort query → migration → flip qa flag. |

### v2 closures (verified by Critic v2 §2)

8/9 v1 findings closed (C-1, H-2, M-1, M-2, M-3, M-4, L-1, L-2). H-1 partially closed in v2; fully closed in v3 via §4 rename + §10 deferred backfill.

### v1 closures

All 10 v1 open questions closed by Critic v1 ratification or deferred to §10.

---

## Summary

This hotfix is **one architectural correction** + **one cohort-closing migration (023)** + **two folded-in audit-symmetry fixes (H1 + H2)** + **one folded-in coverage fix (H3)** + **one folded-in audit-chain threading patch (N3 / chat_messages.py)**. The v1-creation responsibility — unassigned at PR #81 per `bot/services/message_persistence.py:16` — is relocated INTO `persist_message_with_policy` as the SOLE writer for `(chat_messages, message_versions, current_version_id)` triples. This single relocation closes CRITICAL 1, CRITICAL 2, CRITICAL 3, and Risk H2.

15 commits, single PR, ~1000 LOC, 36+ tests + 8 eval cases, 1 migration (023).

Phase 5 entry gate depends on this hotfix landing first.

All 10 v1 + 9 Critic v2 + 6 Risk v2 findings are either closed in code or deferred with explicit owners in §10.

**SHIP-READY for Codex executor.**
