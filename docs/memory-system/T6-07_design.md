# T6-07 Design — EvidenceItem source discriminator

**Status:** Pre-flight design, Wave 2 / Stream D.
**Cycle:** Memory system Phase 6 — knowledge cards / catalog.
**Date:** 2026-05-12.
**Predecessor:** T6-06 (search extension) cross-stream contract — co-developed in Wave 2 / Stream D.
**Companion docs:** `PHASE6_PLAN.md` §5.D, §7; `T6-06_design.md` (sibling — search extension).
**Author:** Wave 2 design sprint agent (pre-flight planning).

---

## §0. Acceptance criteria (verbatim from `PHASE6_PLAN.md` §7)

### T6-07: EvidenceItem source discriminator

- **Scope:** Evidence dataclasses/types and `/recall` formatting path.
- **Acceptance criteria:**
  - `EvidenceItem.source_type` is `Literal['message', 'card']`.
  - Message evidence remains citation-compatible with `message_version_id`.
  - Card evidence carries `card_id` and the `message_version_id` set joined from `card_sources`.
  - `/recall` renders card hits without losing back-citation trace.
- **Dependencies:** T6-06.
- **Stream:** Wave 2 / Stream D.

---

## §1. Invariants enforced by this ticket

Cross-references to `PHASE6_PLAN.md` §1:

1. **#4 Citations point to `message_version_id` or approved card sources (FK-normalized via `card_sources`).** This is THE invariant T6-07 enforces at the consumer boundary. Every card evidence item MUST include the `card_source_message_version_ids` list so the rendered citation traces back to the underlying messages, satisfying invariant #4.
2. **#5 Summary is never canonical truth.** Card content IS canonical (admin-approved), but the rendered citation includes a back-trace to source messages, preserving the auditability the invariant guarantees.

---

## §2. Public API change (frozen contract for callers)

### Current `EvidenceItem` (`bot/services/evidence.py:23-46`)

```python
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "message_version_id": self.message_version_id,
            "chat_message_id": self.chat_message_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "snippet": self.snippet,
            "ts_rank": self.ts_rank,
            "captured_at": self.captured_at.isoformat(),
            "message_date": self.message_date.isoformat(),
        }
```

### Extended `EvidenceItem` (T6-07)

```python
import uuid

@dataclass(frozen=True, slots=True)
class EvidenceItem:
    # Phase 4 fields — unchanged.
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime
    # T6-07 additions — defaulted so Phase 4 message hits don't need to set them.
    source_type: Literal['message', 'card'] = 'message'
    card_id: uuid.UUID | None = None
    card_source_message_version_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            # Phase 4 keys — unchanged.
            "message_version_id": self.message_version_id,
            "chat_message_id": self.chat_message_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "snippet": self.snippet,
            "ts_rank": self.ts_rank,
            "captured_at": self.captured_at.isoformat(),
            "message_date": self.message_date.isoformat(),
            # T6-07 additions.
            "source_type": self.source_type,
            "card_id": str(self.card_id) if self.card_id is not None else None,
            "card_source_message_version_ids": list(self.card_source_message_version_ids),
        }
```

### Default value semantics

- For message evidence: `source_type='message'`, `card_id=None`, `card_source_message_version_ids=()`. Existing callers that construct `EvidenceItem` via `EvidenceBundle.from_hits` with Phase 4 `SearchHit` shape will get these defaults naturally because Phase 4 `SearchHit` doesn't set them either (T6-06 added the same fields with defaults).
- For card evidence: `source_type='card'`, `card_id` is the UUID of the `knowledge_cards` row, `card_source_message_version_ids` is the tuple of mvids from `card_sources` (ordered by `position ASC, id ASC` per T6-06 `card_source_lists` CTE).

### Backward compat

- Phase 4 callers that construct `EvidenceItem(... )` without the new args still work (defaults trip).
- `to_dict()` adds three new keys at the end. Callers that consumed `to_dict()` and iterated keys may see new entries — usually fine (most consumers index by known keys). If a consumer asserted exact key count, that's a test break — sweep.
- `EvidenceBundle.evidence_ids` property (returns `[item.message_version_id for item in self.items]`) — UNCHANGED for both message and card items. For card items, `message_version_id` is the ANCHOR source's mvid (from T6-06 SQL `card_anchors` CTE), so `evidence_ids` still resolves to a list of mvids citable in the gateway. This preserves Phase 5 gateway invariants: `synthesize_answer.bundle.evidence_ids` is still the authoritative source filter input.

### `EvidenceBundle.from_hits` factory

`bot/services/evidence.py:from_hits` (line 57-84) — extends to propagate the new fields:

```python
@classmethod
def from_hits(
    cls,
    query: str,
    chat_id: int,
    hits: Sequence[SearchHitLike],
) -> EvidenceBundle:
    items = tuple(
        EvidenceItem(
            message_version_id=hit.message_version_id,
            chat_message_id=hit.chat_message_id,
            chat_id=hit.chat_id,
            message_id=hit.message_id,
            user_id=hit.user_id,
            snippet=hit.snippet,
            ts_rank=hit.ts_rank,
            captured_at=hit.captured_at,
            message_date=hit.message_date,
            # NEW — fall back to defaults for Phase 4 SearchHit-like objects.
            source_type=getattr(hit, 'source_type', 'message'),
            card_id=getattr(hit, 'card_id', None),
            card_source_message_version_ids=tuple(
                getattr(hit, 'card_source_message_version_ids', ())
            ),
        )
        for hit in hits
    )
    return cls(
        query=query,
        chat_id=chat_id,
        items=items,
        abstained=len(items) == 0,
        created_at=datetime.now(timezone.utc),
    )
```

Using `getattr(..., default)` keeps this resilient to a `SearchHitLike` protocol that doesn't (yet) declare the new fields. The `SearchHitLike` `Protocol` (`bot/services/evidence.py:11-21`) MAY be extended to require the new attributes; recommend AVOID changing the Protocol surface (keeps it backward-compat for fakes used in tests).

---

## §3. `/recall` rendering changes (`bot/handlers/qa.py`)

The current `_format_response` (line 83-100) iterates `bundle.items` and renders Telegram links keyed off `chat_id` + `message_id`. T6-07 introduces a card hit branch.

### Updated renderer

```python
def _format_response(bundle: EvidenceBundle, users_by_id: dict[int, object]) -> str:
    if bundle.abstained:
        return "Не нашёл подходящих свидетельств в истории чата."

    parts = ["<b>Найденные свидетельства:</b>"]
    short_chat_id = _short_chat_id(bundle.chat_id)
    for item in bundle.items:
        if item.source_type == 'card':
            parts.append(_format_card_item(item, short_chat_id))
        else:
            parts.append(_format_message_item(item, short_chat_id, users_by_id))
    return "\n\n".join(parts)


def _format_message_item(
    item: EvidenceItem, short_chat_id: str, users_by_id: dict[int, object]
) -> str:
    # Phase 4 message-hit rendering — unchanged from current code.
    author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
    date_text = _format_date(item.message_date)
    snippet = _safe_headline(item.snippet)
    link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
    return (
        f"<blockquote>{snippet}</blockquote>\n"
        f"<i>— {author_name}, {date_text}</i> · "
        f"<a href=\"{html.escape(link, quote=True)}\">сообщение</a> · "
        f"<code>message_version_id:{item.message_version_id}</code>"
    )


def _format_card_item(item: EvidenceItem, short_chat_id: str) -> str:
    """Card-evidence rendering with back-citation trace per invariant #4."""
    date_text = _format_date(item.message_date)  # card.approved_at substituted in T6-06
    snippet = _safe_headline(item.snippet)
    # Anchor source link — leverages the first card_source row T6-06 surfaced
    # in item.chat_id / item.message_id.
    anchor_link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
    # Inline back-trace: list ALL card_source mvids.
    mvid_list = ", ".join(str(m) for m in item.card_source_message_version_ids)
    return (
        f"<blockquote>{snippet}</blockquote>\n"
        f"<i>📋 Карточка</i> · "
        f"<a href=\"{html.escape(anchor_link, quote=True)}\">первоисточник</a> · "
        f"<code>card_id:{item.card_id}</code> · "
        f"<code>sources:[{mvid_list}]</code>"
    )
```

### Synthesis-mode footer (`_format_synthesized_response`, `bot/handlers/qa.py:151-183`)

Phase 5 LLM synthesis builds a citation footer enumerating `bundle.items`. T6-07 adds card branch:

```python
def _format_synthesized_response(
    answer: AnswerWithCitations,
    bundle: EvidenceBundle,
    users_by_id: dict[int, object],
) -> str:
    answer_text = html.escape(answer.answer_text, quote=False)
    parts = [answer_text, "", "<b>Источники:</b>"]
    for idx, item in enumerate(bundle.items, start=1):
        if item.source_type == 'card':
            parts.append(_format_synth_card_footer(idx, item))
        else:
            author_name = _author_name(
                users_by_id.get(item.user_id) if item.user_id else None
            )
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            parts.append(f"[{idx}] {date_text} — {author_name}: {snippet}")
    return "\n".join(parts)


def _format_synth_card_footer(idx: int, item: EvidenceItem) -> str:
    date_text = _format_date(item.message_date)  # approved_at
    snippet = _safe_headline(item.snippet)
    return (
        f"[{idx}] {date_text} — 📋 Card "
        f"<code>{item.card_id}</code> "
        f"(sources: {len(item.card_source_message_version_ids)}): "
        f"{snippet}"
    )
```

The synthesized response's `Источники:` block now includes cards as citation entries [N] alongside message entries. Snippet rendering uses the same `_safe_headline` pattern for HTML safety.

---

## §4. Files touched

| File | Action | Notes |
|---|---|---|
| `bot/services/evidence.py` | EXTEND | Add `source_type`, `card_id`, `card_source_message_version_ids` to `EvidenceItem` with defaults. Update `to_dict`. Update `from_hits` to read new SearchHit fields via `getattr` for backward compat with non-card hits. |
| `bot/handlers/qa.py:_format_response` (line 83) | EXTEND | Branch on `item.source_type`: card hits render via `_format_card_item`; message hits unchanged. |
| `bot/handlers/qa.py:_format_synthesized_response` (line 151) | EXTEND | Same branching for the Phase 5 synthesis citation footer. |
| `bot/handlers/qa.py` (new helper functions) | NEW | Add `_format_message_item`, `_format_card_item`, `_format_synth_card_footer`. Refactor: extract Phase 4 inline renderer into a named function (no behavior change). |
| `tests/services/test_evidence.py` | EXTEND | Field shape tests; `to_dict` shape tests; `from_hits` backward-compat tests for Phase 4 SearchHit-like fakes. |
| `tests/handlers/test_qa_recall_phase4_preserved.py` | UPDATE | Critical regression test (per `bot/handlers/qa.py:386 // Do NOT refactor or reformat this block — tested by ...`). Update fixture to include both message-only and mixed (message+card) bundle cases. Phase-4-only test variant MUST remain byte-for-byte identical. |
| `tests/handlers/test_qa_llm_synthesis.py` | UPDATE | Per `bot/handlers/qa.py:308 // Tested in tests/handlers/test_qa_llm_synthesis.py`. Add mixed-evidence case for synth footer. |
| `tests/handlers/test_qa_card_rendering.py` | NEW | Dedicated tests for card-hit rendering in both Phase 4 fallback and Phase 5 synth modes. |
| `tests/evals/test_citations.py` | EXTEND | Add C5 card-citation invariant tests. |
| `scripts/lint_privacy_check.sh` | NO CHANGE | `evidence.py` and `qa.py` likely already allowlisted (Phase 4). Verify post-rebase. |

**Out of scope (file-touching):**
- `bot/services/search.py` — T6-06.
- `bot/services/llm_gateway.py` — no API change; gateway already accepts `bundle.evidence_ids` which T6-07 preserves.
- `bot/services/qa.py:run_qa` — pure pass-through; no change.

---

## §5. Phase 4 byte-for-byte preservation

Per `bot/handlers/qa.py:383-389` comment:
```
# Flag OFF (or bundle empty) → Phase 4 byte-for-byte path (UNCHANGED).
# Do NOT refactor or reformat this block — tested by
# tests/handlers/test_qa_recall_phase4_preserved.py.
```

T6-07 changes the `_format_response` body. To preserve Phase 4 byte-for-byte semantics:

**Option A (strict):** Keep the inline rendering for message-only bundles AS-IS. New code path only activates for card-containing bundles. Implementation:

```python
def _format_response(bundle: EvidenceBundle, users_by_id: dict[int, object]) -> str:
    if bundle.abstained:
        return "Не нашёл подходящих свидетельств в истории чата."

    has_card = any(item.source_type == 'card' for item in bundle.items)
    if not has_card:
        # Phase 4 path — preserved byte-for-byte.
        parts = ["<b>Найденные свидетельства:</b>"]
        short_chat_id = _short_chat_id(bundle.chat_id)
        for item in bundle.items:
            author_name = _author_name(users_by_id.get(item.user_id) if item.user_id else None)
            date_text = _format_date(item.message_date)
            snippet = _safe_headline(item.snippet)
            link = f"https://t.me/c/{short_chat_id}/{item.message_id}"
            parts.append(
                f"<blockquote>{snippet}</blockquote>\n"
                f"<i>— {author_name}, {date_text}</i> · "
                f"<a href=\"{html.escape(link, quote=True)}\">сообщение</a> · "
                f"<code>message_version_id:{item.message_version_id}</code>"
            )
        return "\n\n".join(parts)

    # T6-07 mixed/card path.
    parts = ["<b>Найденные свидетельства:</b>"]
    short_chat_id = _short_chat_id(bundle.chat_id)
    for item in bundle.items:
        if item.source_type == 'card':
            parts.append(_format_card_item(item, short_chat_id))
        else:
            parts.append(_format_message_item(item, short_chat_id, users_by_id))
    return "\n\n".join(parts)
```

Pro: literally bytewise identical for the Phase 4 path. Con: duplicated rendering logic.

**Option B (refactor):** Extract `_format_message_item` and use the branching renderer for ALL cases. Test the resulting string is byte-identical to the inlined Phase 4 output (with strict comparison). Pro: clean code. Con: a tiny risk that refactor introduces drift (e.g., spaces, newlines).

**Recommendation:** Option A. The Phase 4 path's preservation is critical (per Phase 11 binding suite); the duplication cost is minor and clearly scoped. Phase 6.5+ may refactor once the binding suite is stable.

For `_format_synthesized_response`, the analogous Phase 5 path is also stability-critical. Apply the same dual-branch pattern: detect `has_card` and skip the new branch entirely for pure-message bundles.

---

## §6. Tests

### Unit (dataclass)

**`tests/services/test_evidence.py` extensions:**

1. `EvidenceItem(...)` constructible with Phase 4-only args; new fields at defaults.
2. `EvidenceItem(..., source_type='card', card_id=uuid.UUID('...'), card_source_message_version_ids=(1,2,3))` works.
3. `EvidenceItem(source_type='message', card_id=uuid.uuid4(), ...)` — accepted (no runtime guard); semantics: defaults expected for message-type items but not enforced. Decision: don't add runtime guard (consistent with Phase 4 NOT enforcing user_id non-null for message items). Test that this combo passes (so future tests don't assume guard).
4. `to_dict()` for message item: matches Phase 4 keys plus three new keys with default values.
5. `to_dict()` for card item: serialises `card_id` as string, `card_source_message_version_ids` as list of ints.
6. `EvidenceBundle.from_hits` with a `SearchHitLike` that has no `source_type` attr → defaults to `'message'`.
7. `EvidenceBundle.from_hits` with a hit object that has `source_type='card'` → propagates correctly.
8. `EvidenceBundle.evidence_ids` returns `[item.message_version_id for item in items]` — for card items, this is the anchor mvid; verified across mixed bundles.

### Renderer

**`tests/handlers/test_qa_card_rendering.py`:**

9. `_format_response` with pure-message bundle: byte-for-byte identical to a captured Phase 4 baseline.
10. `_format_response` with pure-card bundle: renders card items per §3 template (`📋 Карточка`, `первоисточник` link, `card_id`, `sources:[...]`).
11. `_format_response` with mixed bundle (1 card + 2 messages): correct interleaving in bundle order, no rendering crashes.
12. `_format_response` HTML escapes title, snippet correctly; no XSS-style markup leaks.
13. `_format_synthesized_response` with mixed bundle: footer `[1]` = card, `[2]` = message; correct numbering and labels.
14. `_format_synthesized_response` answer_text containing `<code>` blocks is HTML-escaped (Phase 5 behavior preserved).
15. Card with `card_source_message_version_ids = ()` (empty — should not happen per T6-04 contract, but defensive): renderer outputs `sources:[]` and doesn't crash.

### Phase 4 preservation

**`tests/handlers/test_qa_recall_phase4_preserved.py` updates:**

16. Existing pure-message recall test: assert reply text matches the EXACT Phase 4 baseline (byte-identical). This is the regression guard.
17. New mixed-bundle test variant: assert reply contains card rendering AND message rendering.

### Phase 11 binding (citations)

**`tests/evals/test_citations.py` extensions for C5:**

18. C5a: every card EvidenceItem MUST have `card_id IS NOT NULL`.
19. C5b: every card EvidenceItem MUST have `card_source_message_version_ids` non-empty.
20. C5c: every mvid in `card_source_message_version_ids` resolves to a `message_versions` row with `is_redacted=FALSE` and the parent `chat_messages.memory_policy='normal'` AND `is_redacted=FALSE`. (This is the back-trace integrity invariant — invariant #4.)
21. C5d: `evidence_ids` property of a bundle containing card items still returns the anchor mvid set (not the card_id); gateway invariant preserved.

---

## §7. Stop signals

- Card EvidenceItem with `card_id=None` or `card_source_message_version_ids=()` → STOP, contract violation. Test #19.
- `to_dict()` regression breaking Phase 4 consumers — STOP, run audit.
- `/recall` rendering crash on card item — STOP, fix renderer.
- `_format_synthesized_response` synthesized answer containing `[N]` markers that resolve to a card index without correctly labeling — minor; preserve LLM-emitted marker behavior (Phase 5 didn't parse markers in v1.0.0; T6-07 doesn't change that).

---

## §8. Risk register

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R-07-01 | Adding fields to `EvidenceItem` breaks frozen dataclass init for tests that constructed it positionally. | LOW | All current callers use keyword arguments (`bot/services/evidence.py:from_hits`). Defaults make it forward-compat. Test #1 covers. |
| R-07-02 | `to_dict()` extra keys break consumers iterating keys. | LOW | Audit `qa_traces.evidence_ids` JSONB writers — they use `evidence_ids` property, not `to_dict`. Safe. |
| R-07-03 | Phase 4 byte-for-byte rendering drift through refactor. | HIGH | Option A in §5 keeps the inline path literally identical. Test #16 enforces byte comparison. |
| R-07-04 | Card-citation footer in synth mode confuses LLM marker parsing (T5-04 deferred to T6-07). | LOW | v1.0.0 prompt template doesn't emit structured markers; footer is informational. When T6-08 or later wires real marker parsing, the gateway's citation-enforcement set will need to include card_ids — handle in Phase 6.5+. |
| R-07-05 | Card anchor mvid in `evidence_ids` is used by gateway as a source filter input; gateway expects ALL bundle items to be in the source filter, but card item's anchor mvid may pass while OTHER source mvids of the card are tombstoned. | MED | Sibling safety is enforced ONLY by T6-06 search-side filter (the NOT-EXISTS guards in the card branch of `_PHASE6_SQL`). `EvidenceBundle.evidence_ids` returns only the ANCHOR mvid per card item — the gateway's `_TOMBSTONE_GATE_SQL` checks only that anchor, not the full `card_source_message_version_ids` list. If T6-06's search-side filter is bypassed (e.g., a custom caller constructs an EvidenceBundle directly from hand-crafted SearchHits without going through `search_messages`), the gateway has no defense-in-depth at the sibling level. **Follow-up TODO (issue #262):** consider adding a gateway-level guard that checks ALL mvids in `card_source_message_version_ids` against `_TOMBSTONE_GATE_SQL` for defense-in-depth. Deferred — not a T6-07 deliverable. |
| R-07-06 | `card_id` rendered as plain text in Telegram (`<code>...</code>`) is a 36-char UUID; visually noisy. | LOW | Default to first 8 chars in renderer (short form); full UUID accessible via `/card`. Minor UX. |
| R-07-07 | `card_source_message_version_ids` truncation in Telegram render when card has > 10 sources. | LOW | Truncate at 5 with `+N more` continuation; admins use `/card <id>` for full. Adjust template in §3. |
| R-07-08 | `EvidenceBundle.from_hits` `getattr` fallback masks a type error in a non-T6-06 SearchHit. | LOW | Acceptable; the fallback is defensive (defaults to message type). If a future SearchHit-like object adds `source_type` with a wrong type, that's a bug to surface, NOT silently default. Consider a `isinstance` guard. |
| R-07-09 | `to_dict()` consumer in Phase 5 trace persistence (`qa_traces.evidence_ids`) is JSONB; storing card_id strings vs message_version_id ints in the same array breaks downstream SQL queries that ARRAY-contain message_version_id integers. | HIGH | `qa_traces.evidence_ids` stores ONLY mvids today, NOT to_dict() output. Verify: `bot/db/repos/qa_trace.py:create` accepts `evidence_ids: list[int]`. Phase 5 cascade's JSONB `@> CAST(:vid AS jsonb)` operates on mvids only. T6-07 does NOT change this — `EvidenceBundle.evidence_ids` property returns mvids only (anchor for cards). Cross-check at PR time. |
| R-07-10 | `decided_by_username` in Phase 4 evidence isn't a thing; this is T6-04 territory. T6-07 only touches the consumer rendering. No mix-up. | LOW | Documented. |

---

## §9. Open questions

1. **Card author rendering.** Cards are author-less (`user_id=NULL` on the anchor side per T6-06). Should the renderer show "approved by @admin" using `knowledge_cards.approved_by_user_id`? Spec is silent. Recommendation: yes — admins reading `/recall` benefit from knowing which admin approved. Requires JOIN to `users` in `_format_card_item` OR fetch in handler and pass via `users_by_id`. **Open for review.**
2. **Snippet vs body in card rendering.** Snippet is `ts_headline(body_markdown)` from T6-06. Should the renderer link to `/card <id>` for full body? Yes — add `· <code>/card <short_uuid></code>` to the template. Test #10 covers.
3. **Citation marker convention.** Phase 5 footer uses `[1] [2] [3]`. Should card markers be visually distinct (e.g., `[C1] [C2]` and `[M1] [M2]`)? Spec is silent. Recommendation: keep flat numeric markers (simpler); the LABEL after the number ("📋 Card" vs "— @author") provides the discriminator. Open for review.
4. **`card_source_message_version_ids` in synthesis footer.** Should the footer expose ALL source mvids of a card? Recommendation: NO — too noisy. Link to `/card` suffices. The list IS available in `EvidenceItem` for programmatic consumers / future renderers.
5. **`to_dict()` shape stability.** Phase 5 cascade and Phase 4 audit don't serialize `EvidenceItem.to_dict()` to DB; they extract specific fields. Confirmed via cross-check. Safe to extend.
6. **`SearchHitLike` Protocol surface.** Should it be extended to require the new attrs? Recommendation: NO — keep it minimal; rely on `getattr` defaults. Tests verify both paths.
7. **`Literal['message', 'card']` enforcement.** Static via `typing.Literal`; no runtime guard. A future card variant (e.g., `'web_card'` for Phase 9 wiki) extends the Literal without breaking T6-07 callers. Acceptable.
8. **Phase 5 synth response containing `[N]` markers that the LLM emitted referencing index N — when bundle items include cards, the meaning of `[N]` is "the Nth bundle item, which happens to be a card". Existing v1.0.0 prompt doesn't emit `[N]` markers; T6-07 doesn't add LLM marker emission either. Open: when Phase 6.5+ wires real marker parsing, ensure the gateway's citation_ids contract supports card_id alongside message_version_id. Not a T6-07 problem; flag for Phase 6.5.

---

## §10. Out of scope for T6-07

- LLM gateway citation_ids extension to include card_ids — Phase 6.5+.
- Card-specific scoring tweaks in gateway — none needed (gateway operates on mvids).
- Web rendering of card hits — Phase 9.
- pgvector / hybrid scoring for cards — Phase 10.
- Card author rendering via JOIN to users — open in §9; deferrable.

---

## §11. Cross-stream contract with T6-06

T6-06 emits `SearchHit` with `source_type`, `card_id`, `card_source_message_version_ids`. T6-07 consumes those in `EvidenceBundle.from_hits` via `getattr` (forward-compat with pre-T6-06 fakes in tests). The contract is one-way: T6-07 makes no assumption beyond the field names. Both tickets land in the same PAR review pair in Wave 2 / Stream D.

If T6-06 lands first, T6-07 inherits `SearchHit` with the new fields and adds the consumer logic. If T6-07 lands first (unlikely given dependency order), the consumer code reads `getattr` defaults and renders message-only output — degrades gracefully.

Inter-ticket file overlap:
- Both touch `bot/services/evidence.py`: T6-06 changes nothing here; T6-07 owns the `EvidenceItem` field additions.
- `bot/handlers/qa.py`: T6-07 owns rendering changes.
- `bot/services/search.py`: T6-06 owns.

No merge conflicts expected.

---

## §12. Evidence log (files read while drafting)

| File | Key facts extracted |
|---|---|
| `docs/memory-system/PHASE6_PLAN.md` | §5.D contract; §7 T6-07 acceptance; cross-references to T6-06. |
| `bot/services/evidence.py` (1-98) | `EvidenceItem` and `EvidenceBundle` Phase 4 shape; `from_hits` factory; `to_dict` serialisation; `evidence_ids` property. |
| `bot/services/search.py` (1-138) | Phase 4 `SearchHit` shape that T6-06 extends. |
| `bot/handlers/qa.py:_format_response` (line 83-100), `_format_synthesized_response` (line 151-183) | Phase 4 + Phase 5 rendering patterns; HTML escaping; Telegram link construction; the byte-for-byte preservation comment at line 383-389. |
| `bot/handlers/qa.py:_safe_headline` (line 60-62), `_short_chat_id` (line 51-53), `_format_date` (line 56-57), `_author_name` (line 65-80) | Helpers reused by T6-07 card renderer. |
| `bot/services/qa.py` | `run_qa` → `EvidenceBundle.from_hits(query, chat_id, hits)`. Single point of bundle construction. |
| `bot/db/repos/qa_trace.py` | `evidence_ids: list[int]` parameter — confirms mvids-only storage in `qa_traces`; T6-07 preserves. |
| `alembic/versions/022_add_qa_traces.py` (implied) | JSONB column for evidence_ids stores integer arrays only. |
| `bot/services/llm_gateway.py:synthesize_answer` (line 326-450) | Gateway treats `bundle.evidence_ids` as authoritative whitelist; T6-07 preserves. |

---

## §13. Implementation order (suggested)

1. Extend `EvidenceItem` with three new defaulted fields. Update `to_dict`. Update `EvidenceBundle.from_hits` with `getattr` propagation.
2. Add unit tests #1-#8.
3. Refactor `_format_response` into helper functions (`_format_message_item`, `_format_card_item`) keeping Phase 4 path inlined per Option A (§5).
4. Refactor `_format_synthesized_response` similarly.
5. Add card renderer tests #9-#15.
6. Update `test_qa_recall_phase4_preserved.py` (#16, #17).
7. Update `test_qa_llm_synthesis.py` for mixed-evidence synth case.
8. Add Phase 11 C5 binding tests #18-#21.
9. Run full test suite locally.
10. Run Phase 11 binding suite on PR head.
11. Update `IMPLEMENTATION_STATUS.md`.

---

END of T6-07 design.
