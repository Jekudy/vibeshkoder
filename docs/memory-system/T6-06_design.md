# T6-06 Design — Search extension for approved cards

**Status:** Pre-flight design, Wave 2 / Stream D.
**Cycle:** Memory system Phase 6 — knowledge cards / catalog.
**Date:** 2026-05-12.
**Predecessor:** T6-01 (schema) merged. T6-04 (admin approval flow) merged so seeded approved cards exist for tests. T6-07 (EvidenceItem discriminator) cross-stream contract — co-developed.
**Companion docs:** `PHASE6_PLAN.md` §5.D, §7; `T6-07_design.md` (sibling — evidence discriminator).
**Author:** Wave 2 design sprint agent (pre-flight planning).

---

## §0. Acceptance criteria (verbatim from `PHASE6_PLAN.md` §7)

### T6-06: Search extension for approved cards

- **Scope:** `bot/services/search.py`, card FTS query, scoring.
- **Acceptance criteria:**
  - `search_messages(..., include_cards=True)` queries both `message_versions` and `knowledge_cards`.
  - Card FTS uses `to_tsvector('russian', ...)` to match the Phase 4 baseline.
  - `include_cards=False` preserves Phase 4 behaviour byte-for-byte.
  - Card hits require `card_status='approved'`.
  - Card hits rank slightly above equivalent raw message hits.
- **Dependencies:** T6-01.
- **Stream:** Wave 2 / Stream D.

---

## §1. Invariants enforced by this ticket

Cross-references to `PHASE6_PLAN.md` §1:

1. **#1 Existing gatekeeper must not break.** `include_cards=False` keeps Phase 4 search SQL byte-identical.
2. **#2 No LLM calls outside `llm_gateway`.** `search_messages` is pure SQL; T6-06 does not introduce LLM calls.
3. **#3 No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.** Card hits filter `card_status='approved'` only. Cards demoted to `archived` (e.g., via `_cascade_card_sources_on_forget` per §5.A.5 when all sources are forgotten) are NOT returned. Tombstoned-source-card edge case: cards with at least one surviving non-forgotten source remain `approved` per §5.A.5 step 5, but the card's body content was authored from a mix that may have included now-forgotten content — see §5 below for the privacy invariant T6-06 enforces at the search boundary.
4. **#4 Citations point to `message_version_id` or approved card sources (FK-normalized via `card_sources`).** Card hits carry `card_id` + the joined `message_version_id` set so callers (T6-07) can render full source trace.
5. **#5 Summary is never canonical truth.** Card hits ARE admin-approved canonical; this invariant is about LLM summaries (Phase 7+).
6. **#7 Future butler cannot read raw DB directly; must use governance-filtered evidence context.** The card-hit path stays inside `search_messages` — the only governance-filtered entry point for retrieval.

### Phase 11 binding sub-gate

T6-06 introduces a new content surface (`knowledge_cards.body_markdown`) into search results. Card content was admin-approved and is governance-filtered at the search boundary (`card_status='approved'`). However, NEW leakage classes are possible:

- **L6 (proposed new case) — card body containing forgotten content.** If an admin approved a card BEFORE one of its source mvids was forgotten, the card body may contain a paraphrase or quote of the now-forgotten content. The §5.A.5 cascade demotes the card to `archived` only when ALL sources are forgotten. If even one source survives, the card remains `approved` and would be returned by T6-06. The body content remains in DB and may leak.
- Mitigation paths:
  - **Stricter cascade:** demote on ANY source forgotten. Rejected per §5.A.5: "If remaining count > 0, leave the card in its current state (the lost source is simply unlinked; card content may now be partially un-attributable — flag for later admin review)." Spec choice.
  - **Search-side filter:** `T6-06` additionally checks whether ANY of the card's sources are now forgotten (JOIN to `card_sources` + `forget_events`). If hit → exclude card. Pro: bullet-proof privacy. Con: extra JOIN + index lookup per query; spec doesn't mandate.
  - **Defer to admin review:** spec relies on admins re-reviewing cards after partial-source-loss notifications. Pro: respects §5.A.5 design. Con: relies on operator discipline.

**Recommendation for T6-06:** Add a stricter search-side filter (variant 2) as a defense-in-depth layer. Rationale: §5.A.5 spec acknowledges "partially un-attributable" as a flag-for-review state; T6-06 search-side filter is the natural enforcement boundary. Cost: one extra subquery; tractable with the `ix_card_sources_message_version_id` reverse index. See §3 SQL.

If this is contentious in review, the fallback is variant 3 (defer); we still need a Phase 11 L-case enumeration:

- **L6a:** Forget a single source of an approved 3-source card. Card stays `approved` (cascade rule §5.A.5 step 5). `/recall include_cards=true` MUST NOT return the card if any source is tombstoned. → Search-side filter required.
- **L6b:** Forget ALL sources. Cascade demotes to `archived`. `/recall include_cards=true` MUST NOT return.
- **L6c:** Admin approves a card; later the same admin (or a moderator) marks one source's chat_message `is_redacted=TRUE` directly (e.g., manual redaction). The `card_sources` row is intact (no `forget_event` was issued). `/recall include_cards=true`: enforced by `is_redacted` filter in the JOIN — see §3.

### Citations invariant (Phase 11 C-cases)

- **C5 (proposed new case) — card hit citation trace.** Every card hit MUST expose source_message_version_ids ≥ 1, all of which resolve to non-redacted `message_versions` rows. T6-07 renders this trace in the citation footer; T6-06 returns it.

---

## §2. Public API change (frozen contract for callers)

### Signature

```python
async def search_messages(
    session: AsyncSession,
    query: str,
    *,
    chat_id: int,
    limit: int = 3,
    headline_max_words: int = 35,
    include_cards: bool = True,  # NEW; default TRUE per §5.D spec
) -> list[SearchHit]:
    ...
```

Default `include_cards=True`. Phase 4 callers that want byte-for-byte preservation MUST pass `include_cards=False` — spec contract is "include_cards=False preserves Phase 4 behaviour byte-for-byte". Existing callers (`bot/services/qa.py:run_qa`, `bot/handlers/qa.py:recall_handler`) will get card hits by default; this is the intended behavior post-T6-06 ship.

### `SearchHit` shape change

Phase 4 `SearchHit` (`bot/services/search.py:17-27`):

```python
@dataclass(frozen=True)
class SearchHit:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime
```

T6-06 introduces a new field for callers to discriminate. T6-07 handles the formal `EvidenceItem.source_type` discriminator; for `SearchHit`, the analogue is straightforward:

**Option A (recommended):** Add `source_type: Literal['message', 'card'] = 'message'`, `card_id: uuid.UUID | None = None`, `card_source_message_version_ids: tuple[int, ...] = ()`.

Wait — `SearchHit` is `frozen=True` and currently has no default values. Adding fields with defaults at the END is forward-compatible. New fields:

```python
@dataclass(frozen=True)
class SearchHit:
    message_version_id: int  # for cards: the FIRST card_source.message_version_id (anchor), see §3
    chat_message_id: int     # for cards: the chat_message_id of the anchor source
    chat_id: int             # for cards: the chat_id of the anchor source
    message_id: int          # for cards: the message_id of the anchor source
    user_id: int | None      # for cards: NULL (cards are author-less; the anchor source's user is metadata)
    snippet: str
    ts_rank: float
    captured_at: datetime    # for cards: knowledge_cards.approved_at (substitute, since cards don't have captured_at)
    message_date: datetime   # for cards: knowledge_cards.approved_at (citation footer needs a date)
    # NEW:
    source_type: Literal['message', 'card'] = 'message'
    card_id: uuid.UUID | None = None
    card_source_message_version_ids: tuple[int, ...] = ()
```

For message hits, defaults trip: `source_type='message'`, `card_id=None`, `card_source_message_version_ids=()`. Callers that don't read the new fields see Phase 4 behaviour byte-for-byte (the dataclass is forward-compatible).

For card hits, all three new fields are populated. The "anchor source" is the first (lowest-position) `card_sources` row joined; we reuse the existing message-hit fields (`chat_id` / `message_id` / etc.) to point at this anchor so existing renderers don't crash on a "card without chat_id".

**Option B (alternative):** Introduce `CardSearchHit` as a separate dataclass; `search_messages` returns `list[SearchHit | CardSearchHit]`. Pro: cleaner types. Con: every caller must branch on `isinstance` — large churn. **Rejected.** Option A is the path; T6-07 handles the formal discriminator at the `EvidenceItem` boundary.

### Backward compat

- `include_cards=False` returns exactly the Phase 4 result list, byte-identical (same SQL, same column order, same rank formula). New fields on `SearchHit` are at defaults.
- `include_cards=True` (the new default) MAY change observable behavior for existing callers: card hits will appear in the result list. Callers that assumed pure-message results MAY need updates. The dependent ticket T6-07 handles `EvidenceItem.source_type` so `/recall` rendering stays correct.
- Internal `EvidenceBundle.from_hits` (`bot/services/evidence.py:from_hits`) will inherit the new fields via T6-07.

---

## §3. SQL design

### Message branch (unchanged from Phase 4)

`bot/services/search.py:62-114` is the canonical Phase 4 SQL. Preserved verbatim when `include_cards=False`; merged into a UNION ALL when `include_cards=True`.

### Card branch (new)

```sql
WITH q AS (
    SELECT plainto_tsquery('russian', :query) AS tsq
),
-- ALL approved cards FTS-matched on body_tsv (GIN index from migration 032).
card_hits AS (
    SELECT
        kc.id AS card_id,
        ts_rank_cd(kc.body_tsv, q.tsq) AS rank,
        COALESCE(
            ts_headline(
                'russian',
                kc.body_markdown,
                q.tsq,
                :headline_options
            ),
            ''
        ) AS snippet,
        kc.title AS title,
        kc.approved_at AS approved_at
    FROM knowledge_cards AS kc
    CROSS JOIN q
    WHERE kc.card_status = 'approved'
        AND kc.body_tsv @@ q.tsq
        AND NOT EXISTS (
            -- Defense-in-depth: exclude cards where ANY source mvid is tombstoned.
            -- §5.A.5 step 5 acknowledges partial-source-loss as a flag-for-review state;
            -- T6-06 enforces strict exclusion at the search boundary so /recall cannot
            -- return a card paraphrasing now-forgotten content even before admin review.
            SELECT 1
            FROM card_sources cs2
            JOIN message_versions mv2 ON mv2.id = cs2.message_version_id
            JOIN chat_messages c2 ON c2.id = mv2.chat_message_id
            JOIN forget_events fe2 ON (
                fe2.tombstone_key = 'message:' || c2.chat_id::text || ':' || c2.message_id::text
                OR (
                    c2.content_hash IS NOT NULL
                    AND fe2.tombstone_key = 'message_hash:' || c2.content_hash
                )
                OR (
                    c2.user_id IS NOT NULL
                    AND fe2.tombstone_key = 'user:' || c2.user_id::text
                )
            )
            WHERE cs2.card_id = kc.id
                AND fe2.status IN ('pending', 'processing', 'completed')
        )
        AND NOT EXISTS (
            -- Defense-in-depth: exclude cards where ANY source has memory_policy != 'normal'
            -- OR is_redacted=TRUE (catches manual redaction without forget_event).
            SELECT 1
            FROM card_sources cs3
            JOIN message_versions mv3 ON mv3.id = cs3.message_version_id
            JOIN chat_messages c3 ON c3.id = mv3.chat_message_id
            WHERE cs3.card_id = kc.id
                AND (c3.memory_policy <> 'normal' OR c3.is_redacted = TRUE OR mv3.is_redacted = TRUE)
        )
),
-- Resolve the anchor source per card (lowest position; deterministic).
card_anchors AS (
    SELECT DISTINCT ON (cs.card_id)
        cs.card_id,
        mv.id AS message_version_id,
        c.id AS chat_message_id,
        c.chat_id AS chat_id,
        c.message_id AS message_id,
        c.user_id AS user_id
    FROM card_sources cs
    JOIN message_versions mv ON mv.id = cs.message_version_id
    JOIN chat_messages c ON c.id = mv.chat_message_id
    WHERE cs.card_id IN (SELECT card_id FROM card_hits)
    ORDER BY cs.card_id, cs.position ASC, cs.id ASC
),
-- Aggregate all source mvids per card (T6-07 needs the list).
card_source_lists AS (
    SELECT cs.card_id,
           ARRAY_AGG(cs.message_version_id ORDER BY cs.position ASC, cs.id ASC) AS mvids
    FROM card_sources cs
    WHERE cs.card_id IN (SELECT card_id FROM card_hits)
    GROUP BY cs.card_id
)
SELECT
    'card' AS source_type,
    ca.message_version_id AS message_version_id,
    ca.chat_message_id AS chat_message_id,
    ca.chat_id AS chat_id,
    ca.message_id AS message_id,
    NULL::bigint AS user_id,            -- cards are author-less
    ch.snippet AS snippet,
    ch.rank * :card_rank_boost AS rank, -- card hits rank slightly above message hits
    ch.approved_at AS captured_at,
    ch.approved_at AS message_date,
    ch.card_id AS card_id,
    csl.mvids AS card_source_message_version_ids
FROM card_hits ch
JOIN card_anchors ca ON ca.card_id = ch.card_id
JOIN card_source_lists csl ON csl.card_id = ch.card_id
```

### Chat scope for card hits

Question: do card hits filter by `chat_id`?

Cards are content distilled from messages in the community chat (Phase 6 ingests only `chat_messages.memory_policy='normal'`, which today is COMMUNITY_CHAT_ID). For multi-chat futures, cards may span multiple chats. T6-06 design choice:

- **Recommended:** card hits are NOT filtered by `:chat_id`. Cards are admin-curated canonical knowledge; they may legitimately bridge sources from different chats (future). At T6-06 ship, Phase 6 only ingests from COMMUNITY_CHAT_ID, so all card sources will be in one chat anyway.
- The anchor source's `chat_id` is reported on the hit; renderers can decide how to display the link.

If review wants strict chat scoping (mirror message branch's `c.chat_id = :chat_id`), add a JOIN on `card_sources` → `chat_messages` filtered by `c.chat_id = :chat_id` AT LEAST ONE source (EXISTS subquery). Defer to review.

### UNION ALL + final ranking

```sql
WITH all_hits AS (
    SELECT ... FROM message_hits  -- Phase 4 SQL, with new defaulted columns
    UNION ALL
    SELECT ... FROM card_hits_with_anchor  -- §3 above
)
SELECT *
FROM all_hits
ORDER BY rank DESC, captured_at DESC, message_version_id DESC
LIMIT :limit
```

The message branch SELECT must add NULL placeholders for `card_id` and `card_source_message_version_ids` so UNION ALL column counts match. Use `NULL::uuid AS card_id, ARRAY[]::int[] AS card_source_message_version_ids`.

### Rank boost

Spec: "Card hits rank slightly above equivalent raw message hits."

`card_rank_boost` parameter, default 1.15 (15% boost). Multiplicative on `ts_rank_cd`. Tunable later; the value SHOULD be small enough that a strong message match still beats a weak card match.

Rationale: bumps a card hit with rank 0.20 to 0.23, lifting it above a message hit with rank 0.22 but staying below a message hit with rank 0.30. Empirically tested by seed_v1 fixture; T6-09 integration tests verify the ordering.

### Indexes (already in place from T6-01)

- `ix_knowledge_cards_body_tsv` (GIN) — required.
- `ix_knowledge_cards_card_status` — used by the WHERE filter.
- `ix_card_sources_message_version_id` — reverse index for the cascade; ALSO used by the source-exclusion subqueries.

No new indexes needed.

### SQLite test path

`tsvector` / `to_tsvector` / `ts_rank_cd` are Postgres-only. SQLite tests must use a fallback. Two options:

1. **Skip SQLite test path entirely for include_cards=True.** The Phase 4 search itself already mandates Postgres for FTS tests; SQLite tests cover ORM-only paths. Acceptable.
2. **Dialect-guard the entire card branch.** If `session.bind.dialect.name != 'postgresql'`, return only the message branch (with empty card results). Useful for SQLite-path callers that just want to exercise the `include_cards=True` API without expecting Postgres semantics.

Recommendation: option 2. The function still returns a coherent result; tests requiring real card FTS run on Postgres. The conditional is a small Python branch before SQL execution.

---

## §4. Files touched

| File | Action | Notes |
|---|---|---|
| `bot/services/search.py` | EXTEND | Add `include_cards` parameter; extend `SearchHit` with `source_type` / `card_id` / `card_source_message_version_ids` defaulted fields; implement UNION ALL SQL; preserve Phase 4 byte-for-byte when `include_cards=False`. |
| `bot/services/evidence.py` | EXTEND | (Cross-stream with T6-07.) `EvidenceItem` gains corresponding fields; `EvidenceBundle.from_hits` propagates them. Detailed in `T6-07_design.md`. |
| `bot/services/qa.py` | NO CHANGE (or small) | `run_qa` passes through to `search_messages`. If the default `include_cards=True` causes test regressions, add explicit `include_cards=True` here. |
| `bot/handlers/qa.py:_format_response` (line 83) | UPDATE (or T6-07 territory) | Rendering for card hits is in T6-07 scope. T6-06 only needs to ensure the renderer doesn't crash on card hits; T6-07 adds the proper formatting. Recommend a minimal "skip card hit" branch in T6-06 ship if T6-07 hasn't landed yet — but if T6-06 and T6-07 ship together (Wave 2 / Stream D), this is moot. |
| `tests/services/test_search.py` | EXTEND | Add card-inclusion tests. See §6. |
| `tests/services/test_search_cards.py` | NEW | Dedicated card-search test module if test_search.py exceeds a sensible size. |
| `tests/evals/test_leakage.py` | EXTEND | Add L6 case(s): card body containing forgotten content. See §6 + Phase 11 binding sub-gate notes below. |
| `tests/fixtures/golden_recall/seed_v1/seed_data.py` (or equivalent) | EXTEND if needed | Add a synthetic approved card with sources to exercise the card branch in eval tests. |
| `scripts/lint_privacy_check.sh` | UPDATE | `bot/services/search.py` is likely already allowlisted (Phase 4). If new file `tests/services/test_search_cards.py` references policy strings, allowlist. |

**Out of scope (file-touching):**
- `bot/handlers/admin_cards.py` — T6-04/T6-05.
- `bot/services/forget_cascade.py` — T6-04 territory for orchestrator lock.
- `bot/services/llm_gateway.py` — no LLM use here.

---

## §5. Privacy invariants enforced at the search boundary

Spec invariants (PHASE6_PLAN.md §1) interpreted for T6-06:

1. **Card body must not be returned if any source has been forgotten.** Enforced via the `NOT EXISTS` clauses in §3.
2. **Card body must not be returned if any source has `memory_policy != 'normal'` OR `is_redacted = TRUE`.** Enforced via the second `NOT EXISTS` clause.
3. **Card itself is `card_status='approved'`.** Enforced via the simple WHERE filter.

Together these enforce L6a/L6b/L6c at the search boundary. If a card slips into `approved` with partially-redacted sources (e.g., manual moderator action without `forget_event`), it WON'T be returned by `/recall include_cards=true`.

### Comparison with message-branch privacy

The message branch (Phase 4 SQL) enforces:
- `c.memory_policy = 'normal'`
- `c.is_redacted = FALSE`
- `mv.is_redacted = FALSE`
- `NOT EXISTS` over `forget_events` with three tombstone keys.

The card branch adds equivalent NOT-EXISTS subqueries over the JOIN through `card_sources`. Symmetric structure; SQL plans should be reasonable given the indexes.

### Headline / snippet for cards

`ts_headline` on `kc.body_markdown` returns a snippet with `<b>...</b>` highlight markers. The renderer (T6-07) is responsible for HTML-escaping safely. Telegram MarkdownV2 of the FULL body lives in `/card <id>` (T6-05); the snippet returned here is plain text.

---

## §6. Tests

### Unit / SQL

**`tests/services/test_search.py` extensions:**

1. `search_messages(..., include_cards=False)` returns Phase 4 results byte-for-byte. Compare against a captured baseline (regression test).
2. `search_messages(..., include_cards=True)` with no matching cards returns ONLY message hits.
3. `search_messages(..., include_cards=True)` with no matching messages returns ONLY card hits.
4. `search_messages(..., include_cards=True)` with both → merged list, sorted by rank DESC.
5. Card hits rank above equivalent message hits: seed a card and a message with similar match rank; assert card_rank_boost lifts the card hit.
6. `card_status='draft'` card is NOT returned.
7. `card_status='archived'` card is NOT returned.
8. Card with one source mvid having `chat_messages.memory_policy='offrecord'` is NOT returned.
9. Card with one source mvid having `chat_messages.is_redacted=TRUE` is NOT returned.
10. Card with one source covered by a `forget_event` (status='completed') is NOT returned.
11. Card with one source covered by a `forget_event` (status='pending') is NOT returned (search filter must match the gateway's 3-status filter).
12. `SearchHit.source_type='card'` for card hits; `SearchHit.card_id` is non-null; `SearchHit.card_source_message_version_ids` is a non-empty tuple.
13. `SearchHit.source_type='message'` for message hits; new fields all at defaults.
14. SQLite-path call: function returns gracefully (no Postgres-only SQL fired); card branch yields empty list.
15. `chat_id` filter does NOT filter card hits (cards may span chats; current Phase 6 ingestion is single-chat so the filter is a no-op anyway).
16. `limit` clamps the merged result.

### Integration (Postgres)

**`tests/services/test_search_cards_integration.py`:**

17. End-to-end seed: 5 approved cards + 10 messages. `/recall include_cards=true` returns top 3 by rank with mixed types.
18. Run T6-09 advisory-lock collision test with concurrent `/approve` + cascade + `/recall include_cards=true`. Assert post-COMMIT consistency.
19. Forget a single source of a 3-source card. Card stays `approved` per §5.A.5 step 5. `/recall include_cards=true` MUST NOT return the card.
20. Forget ALL sources. Card demoted to `archived` per §5.A.5 step 4. `/recall include_cards=true` MUST NOT return.
21. Cancel forget mid-flight (status='pending'). `/recall include_cards=true` MUST NOT return the card (pending tombstone blocks per filter in §3). This matches Phase 4 SQL semantics (`fe.status IN ('pending', 'processing', 'completed')`).

### Phase 11 binding (sub-gate for T6-06)

T6-06 introduces card content to `/recall`. The leakage suite needs L6 coverage:

- **L6a:** Approved card with 3 sources; forget 1 source. `/recall include_cards=true` does NOT return the card. Assert via `bundle.items` filter.
- **L6b:** Approved card with 3 sources; forget all 3. Card demotes to archived. `/recall include_cards=true` does NOT return.
- **L6c:** Approved card with 3 sources; manual `is_redacted=TRUE` on one source's chat_message. `/recall include_cards=true` does NOT return.
- **L6d:** Approved card whose body contains a substring that ONLY appears in an offrecord message (semantic leak). Card stayed approved because the offrecord was never ingested as a source. Test: ingest #offrecord message → it never becomes a candidate → not a source → not in any card. Defensive: assert no card has any `card_sources` row pointing at a `chat_message` with `memory_policy != 'normal'`.

These cases extend `tests/evals/test_leakage.py`. Add fixtures in `tests/fixtures/golden_recall/seed_v1/` for at least one approved card per case. Phase 11 binding sub-gate requires these green on the T6-06 PR head.

### Phase 11 binding (citations sub-gate)

**C5:** Every card hit returned by `search_messages(..., include_cards=True)` MUST have `card_source_message_version_ids` non-empty AND every mvid in that tuple MUST resolve to a non-redacted `message_versions` row. Test in `tests/evals/test_citations.py`.

---

## §7. Stop signals (specific to this ticket)

- `/recall include_cards=true` returning a card with `card_status != 'approved'` → STOP, governance breach.
- `/recall include_cards=true` returning a card with ANY source tombstoned or redacted → STOP, privacy edge case. The `NOT EXISTS` clauses in §3 SQL prevent this.
- `include_cards=False` returning anything other than Phase 4 byte-for-byte output → STOP, regression. Test #1 catches.
- Card hit returning a snippet containing forgotten body content (impossible if the NOT-EXISTS guards work; defensive — content was authored from sources at approval time, but bodies of forgotten sources are NULLed at the cascade's message_versions layer, so snippet would only contain card-authored paraphrase, not raw source body).
- Phase 11 binding sub-gate fail → STOP, do NOT merge.

---

## §8. Risk register

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R-06-01 | UNION ALL + extra subqueries make search latency unacceptable at scale. | MED | Card volume expected small (~hundreds, not millions). GIN index on `body_tsv` keeps the FTS branch fast. Defense-in-depth subqueries on `card_sources` use the existing reverse index. Benchmark in T6-09 integration. |
| R-06-02 | Card rank boost (1.15) is wrong magnitude. | LOW | Tunable via parameter. Empirical evaluation in T6-09 + seed_v1 fixture. |
| R-06-03 | New default `include_cards=True` breaks existing tests that asserted Phase-4-exact result counts. | MED | Audit callers: `bot/services/qa.py:run_qa` and tests. Most Phase 4 tests should set `include_cards=False` defensively if they want pure-message semantics. Sweep + update in the same PR. |
| R-06-04 | Card body partially containing forgotten content slips past the NOT-EXISTS guards. | HIGH | Variant 2 (search-side strict filter) is the recommended path. Verify with L6 binding tests. If we chose variant 3 (defer), STOP and add the guard. |
| R-06-05 | Phase 11 binding L1-L5 regress because the UNION ALL altered Phase 4 result ordering even with `include_cards=False`. | HIGH | Test #1 mandates byte-for-byte preservation. SQL paths MUST stay dialect-disjoint: `include_cards=False` → run the exact Phase 4 query (literal copy of bytes); `include_cards=True` → run the new UNION ALL. NO shared code that changes Phase 4 plan. |
| R-06-06 | `ts_headline` on `body_markdown` produces a snippet with MarkdownV2 markup leakage (snippets that contain unbalanced `*` / `_`). | LOW | The snippet field is plain-text-escaped in the renderer (T6-07). Test #20 in T6-05 covered the rendering side; mirror for snippet. |
| R-06-07 | `card_source_message_version_ids` returned as Postgres array; SQLAlchemy mappings may need an explicit type adapter. | MED | Use `ARRAY_AGG(...)` returning `integer[]`; mapping returns `list[int]`. Test #12 verifies. |
| R-06-08 | SQLite-path callers get an empty card branch quietly — could mask test regressions. | LOW | Add an explicit "if dialect != postgresql: return only message_hits" log line at DEBUG level. Documented behaviour. |
| R-06-09 | Card hit returns NULL `user_id` while message-hit consumers (`_author_name` in `bot/handlers/qa.py:65-80`) treat NULL → "—". Telegram link renders OK; author field shows "—". | LOW | Acceptable; cards are author-less. T6-07 can render "Card: <title>" instead. |
| R-06-10 | Card chat_id filter is loosened (no `:chat_id` constraint), so a future multi-chat config could leak cards from chat X into recall on chat Y. | LOW | Add review note: if multi-chat future arrives, tighten with EXISTS subquery. For Phase 6 single-chat ingestion this is a non-issue. |

---

## §9. Open questions

1. **Strict card-source exclusion (variant 2) vs defer to admin (variant 3).** Recommendation: variant 2 (in §3 SQL). Open for review challenge.
2. **`include_cards` default value.** Spec §5.D says `include_cards: bool = True`. Confirmed default. Phase 4 callers must explicitly opt out. Acceptable.
3. **Card rank boost magnitude.** Default 1.15 chosen empirically. Tunable.
4. **Chat scope for cards.** Recommendation: no chat filter on card branch. See §3.
5. **Snippet length for cards.** `headline_max_words` defaults to 35; same for cards. Spec is silent; reuse parameter.
6. **`SearchHit` fields with defaults break frozen dataclass init?** No — `frozen=True` is fine with default values. Verify by importing in a test.
7. **`UNION ALL` column ordering.** Both branches must SELECT in the exact same column order to UNION. Carefully matched in §3.
8. **Backward compat with Phase 4 test fixtures.** The expected `result.bundle.evidence_ids` list in seed_v1 may now include card-anchor mvids. Test #1 (`include_cards=False`) preserves Phase 4 semantics; Phase 11 binding tests that call `/recall` directly via the handler will see `include_cards=True` and may need fixture updates.
9. **Anchor source choice (lowest-position card_source row).** Stable but somewhat arbitrary. Reasonable for Phase 6 — admin curates and orders sources at approval time; position 0 is the "primary" source.

---

## §10. Out of scope for T6-06

- `EvidenceItem.source_type` discriminator — T6-07.
- Renderer changes for card citations — T6-07.
- Web search UI — Phase 9.
- pgvector / hybrid retrieval — Phase 10.
- Cross-chat search — multi-chat future.

---

## §11. Cross-stream contract with T6-07

T6-06 emits `SearchHit` with `source_type`, `card_id`, `card_source_message_version_ids`. T6-07 consumes these and maps them onto `EvidenceItem.source_type`, `EvidenceItem.card_id`, `EvidenceItem.card_source_message_version_ids`. The `from_hits` factory in `EvidenceBundle` propagates one-to-one.

The cross-stream contract is one-way (T6-06 → T6-07) — T6-06 makes no assumption about how T6-07 renders. The two tickets land in the same PAR review pair in Wave 2 / Stream D.

---

## §12. Evidence log (files read while drafting)

| File | Key facts extracted |
|---|---|
| `docs/memory-system/PHASE6_PLAN.md` | §5.D contract; §7 T6-06 acceptance; §5.A.5 cascade semantics for tombstone-driven demotion. |
| `bot/services/search.py` (1-138) | Phase 4 baseline SQL; SearchHit shape; `MAX_QUERY_LENGTH`; `headline_options`. |
| `bot/services/evidence.py` (1-98) | EvidenceItem and EvidenceBundle shape; `from_hits` factory. |
| `bot/services/qa.py` | `run_qa` delegates straight to `search_messages`. Single integration point. |
| `bot/handlers/qa.py:_format_response` | Phase 4 rendering pattern; `_short_chat_id`, `_safe_headline`, message links. |
| `bot/services/llm_gateway.py:_TOMBSTONE_GATE_SQL` (line 289-310) | Three tombstone-key construction — canonical. |
| `bot/db/models.py:1079-1217` | KnowledgeCard + CardSource schema; FTS on body_tsv; UNIQUE on (card_id, message_version_id); reverse index. |
| `bot/services/forget_cascade.py:_cascade_card_sources_on_forget` (line 587-762) | §5.A.5 cascade semantics; archived demote on remaining_count==0. |
| `tests/evals/test_leakage.py` (1-300) | L3a/L3b/L3c patterns; basis for L6 case construction. |
| `alembic/versions/032_add_knowledge_cards.py` | Confirms `body_tsv` GENERATED STORED; GIN index. |

---

## §13. Implementation order (suggested)

1. Add `include_cards` parameter to `search_messages` signature; default False initially (so tests don't regress).
2. Add UNION ALL SQL with the message branch unchanged (literal copy from Phase 4) and the card branch from §3. Add defensive subqueries (variant 2).
3. Extend `SearchHit` with `source_type` / `card_id` / `card_source_message_version_ids` defaulted fields.
4. Add unit tests #1-#16.
5. Add integration tests #17-#21 against real Postgres.
6. Add L6 leakage tests in `tests/evals/test_leakage.py`.
7. Add C5 citation test in `tests/evals/test_citations.py`.
8. Flip default to `include_cards=True` (spec contract).
9. Sweep callers (`bot/services/qa.py`, all tests) to add explicit `include_cards=False` where Phase 4 semantics are required.
10. Run Phase 11 binding suite on PR head. Phase 11 sub-gate: L1-L6 + R1-R4 green.
11. Update `IMPLEMENTATION_STATUS.md`.

---

END of T6-06 design.
