# Phase 4 — Hybrid Search + Q&A with Citations: Design & Stream Plan

**Status:** Planning — design ratified, tickets created, streams allocated
**Cycle:** Memory system Phase 4
**Date:** 2026-04-30
**Predecessor:** Phase 2 CLOSED 2026-04-29 (20/20 issues + FHR), Phase 3 governance skeleton merged in Phase 2 wave Charlie
**Migration window:** 020+
**Critical invariant for this phase:** No LLM calls in Phase 4. Pure FTS retrieval + evidence + citations + abstention. LLM generation is **Phase 5**.

---

## 1. Non-Negotiable Invariants (verbatim from HANDOFF.md §1)

These six invariants are the hard fence around Phase 4 design. Every stream prompt below quotes them verbatim.

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.

(Invariants 5, 6, 7, 10 are not directly in scope for Phase 4 but remain binding.)

**Phase 4 Risk (HANDOFF §2 verbatim):** *hallucination if LLM used too early.*
**Phase 4 Rollback (HANDOFF §2 verbatim):** *q&a feature flag off.*

---

## 2. Phase 4 Spec (HANDOFF §2)

- **Objective:** answer questions from evidence only.
- **Scope:** FTS-first retrieval, evidence bundle, citations, confidence / abstention, q&a traces.
- **Dependencies:** Phase 1 + Phase 3 (both DONE).
- **Entry criteria:** `message_versions` and governance filters exist. ✓ (Phase 1 closed)
- **Exit criteria:** bot can answer simple history questions with citations or refuse.
- **Acceptance:** cites `message_version_id`; excludes forbidden content; refuses no evidence.
- **Risks:** hallucination if LLM used too early.
- **Rollback:** q&a feature flag off (`memory.qa.enabled`).

---

## 3. Phase 5 Boundary — what Phase 4 MUST NOT do

To prevent scope drift into Phase 5+:

- **No LLM calls of any kind.** No paraphrasing, no answer synthesis, no relevance reranking via LLM. Phase 4 returns raw evidence + structured snippets ONLY.
- **No `llm_usage_ledger`, `extraction_runs`, `memory_events`, `observations`, `reflection_runs`, `memory_candidates`** — Phase 5+.
- **No knowledge cards / catalog** — Phase 6.
- **No daily/weekly summaries or digests** — Phase 7/8.
- **No wiki, no graph projection** — Phase 9/10.
- **No vector search / pgvector / embeddings.** AUTHORIZED_SCOPE explicitly lists vector search as "Phase 4+ at earliest" — interpret as **NOT in this Phase 4**. FTS-first per HANDOFF §1.
- **No reranking heuristics that depend on user identity / social graph** beyond simple chat-scoping.

If a stream finds itself wanting to add an LLM/vector/embedding code path → STOP, surface as design question in PR description.

---

## 4. Architecture Overview

```
                ┌─────────────────────────────────────────────────────┐
                │                    Telegram /recall                 │
                │           (handler — feature-flagged, abstains)     │
                └───────────────┬─────────────────────────────────────┘
                                │ query
                                ▼
                ┌─────────────────────────────────────────────────────┐
                │              search_messages(...)                   │
                │  - tsvector FTS over message_versions.normalized_   │
                │    text (russian stemmer)                           │
                │  - JOIN chat_messages: governance filter            │
                │  - LEFT JOIN forget_events: tombstone exclusion     │
                │  - ts_headline → snippet                            │
                │  - ts_rank → ordering                               │
                │  - returns list[SearchHit]                          │
                └───────────────┬─────────────────────────────────────┘
                                │ list[SearchHit]
                                ▼
                ┌─────────────────────────────────────────────────────┐
                │              EvidenceBundle.from_hits(...)          │
                │  - frozen dataclass (immutable, JSON-serializable)  │
                │  - top-N items (default 3)                          │
                │  - never carries forgotten/offrecord content        │
                │  - ready for Phase 5 LLM gateway consumption        │
                └───────────────┬─────────────────────────────────────┘
                                │ EvidenceBundle
                                ▼
                ┌─────────────────────────────────────────────────────┐
                │            Telegram response (Markdown)             │
                │  - top-N evidence items as quoted citations         │
                │  - link to original message via deep-link           │
                │  - abstention if bundle empty                       │
                └─────────────────────────────────────────────────────┘
                                │ + audit
                                ▼
                ┌─────────────────────────────────────────────────────┐
                │  qa_traces (audit)                                  │
                │  - id, user_tg_id, chat_id, query (redacted if     │
                │    detect_policy(query) ≠ 'normal'), evidence_ids  │
                │    JSONB, abstained bool, created_at                │
                └─────────────────────────────────────────────────────┘
```

---

## 5. Component Design

### 5.A. FTS schema (Stream A → T4-01)

**File:** `alembic/versions/020_add_message_version_fts_index.py` + minor model addition.

**Decisions:**

1. **Where to put tsvector?** On `message_versions` (NOT on `chat_messages`).
   - Reason: every edit produces a new `message_versions` row; FTS must reflect the *current* version. Citing `message_version_id` (invariant #4) means the tsvector source must be the same row.
   - The "current version" is denormalized via `chat_messages.current_version_id` — search joins `chat_messages` ← `message_versions` and filters `message_versions.id = chat_messages.current_version_id` to avoid querying older versions.
2. **Generated column.**
   ```sql
   ALTER TABLE message_versions
   ADD COLUMN search_tsv tsvector
   GENERATED ALWAYS AS (
     to_tsvector('russian', coalesce(normalized_text, '') || ' ' || coalesce(caption, ''))
   ) STORED;
   ```
   - Source = `normalized_text` (lowercased + canonicalized in `bot/services/normalization.py`) + `caption`. NOT `text` directly — `normalized_text` is the consistent surface.
3. **Stemmer:** `russian`. Community is Russian-speaking. Mixed-Cyrillic/Latin words still index because PostgreSQL `to_tsvector('russian', ...)` falls back to identity for non-Cyrillic tokens.
4. **GIN index.**
   ```sql
   CREATE INDEX ix_message_versions_search_tsv
     ON message_versions USING GIN (search_tsv);
   ```
   No partial-index governance filter at the schema layer — see #5 below.
5. **Governance filter strategy: pure-query, NOT partial index.** Decision rationale:
   - **Argument for partial index:** smaller index, "defense-in-depth", forgotten/offrecord rows can never appear in FTS.
   - **Argument against:** governance state lives on `chat_messages` (memory_policy, is_redacted), not on `message_versions`. A partial index would need to copy state into `message_versions` (data duplication + race on update) OR rely on cross-table predicate (PostgreSQL doesn't allow this in partial-index expressions). Furthermore, `forget_events` is appended *after* the version is indexed — a partial index would force a full reindex on every forget, which fights invariant #9 (durability).
   - **Final decision:** index ALL versions; rely on the search query's WHERE-clause to enforce `chat_messages.memory_policy='normal' AND chat_messages.is_redacted=false AND message_versions.is_redacted=false AND NOT EXISTS (forget_events ...)`. This is the same pattern Phase 2 uses for query-time tombstone filtering.
   - **Defense-in-depth:** the cascade worker (`bot/services/forget_cascade.py`) already lists `fts_rows` as a layer; Phase 4 wires the cascade so that when a forget event lands, the message_versions row is either deleted OR its `text/caption/normalized_text` are NULLed (offrecord pattern), which makes `to_tsvector('russian', '')` produce the empty `''::tsvector`. Stale FTS index entries become harmless empty vectors.
6. **Migration pattern:** `op.add_column` with `server_default` for backfill, `op.create_index` with `CONCURRENTLY` is **not** available inside a default Alembic transaction; use the `with op.get_context().autocommit_block()` workaround. STORED generated columns auto-populate, no explicit backfill needed.

**Acceptance:**
- Migration 020 applies clean on prod-shape DB (uses staging dump).
- `message_versions.search_tsv` populated for all existing rows post-migration (count check).
- GIN index present (verify via `\d message_versions`).
- Roll-forward only: down-migration drops index + column.
- pytest unit: insert message_version → query `to_tsvector('russian', 'тестовое сообщение') @@ search_tsv` returns row.
- Cascade worker test: forget event on a message → search_tsv eventually returns empty (after cascade processes; details in Stream B).
- No regressions in `bot/db/repos/message_version.py::insert_version` integration tests.

**Risks/Stop signals:**
- Russian stemmer not available in postgres image → STOP, switch to `simple` and surface in PR.
- Migration > 30s on staging → consider non-stored option, surface in PR.

---

### 5.B. Search service (Stream B → T4-02)

**File:** `bot/services/search.py` + `bot/db/repos/search.py` + tests.

**Public API:**
```python
@dataclass(frozen=True)
class SearchHit:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str             # ts_headline output, may contain <b>...</b>
    ts_rank: float
    captured_at: datetime    # message_versions.captured_at
    message_date: datetime   # chat_messages.date

async def search_messages(
    session: AsyncSession,
    query: str,
    *,
    chat_id: int,
    limit: int = 3,
    headline_max_words: int = 35,
) -> list[SearchHit]:
    ...
```

**Behaviour:**

- Empty / whitespace-only query → `return []`.
- Query length cap: 256 chars (longer → silently truncate, log at info).
- Calls `plainto_tsquery('russian', :q)` (NOT `to_tsquery` — user input is unsafe; `plainto` does the parsing for us).
- `ORDER BY ts_rank_cd(search_tsv, query) DESC, captured_at DESC` (recency tiebreaker).
- `LIMIT :limit`.

**SQL (parameterized via SQLAlchemy):**
```sql
SELECT
  mv.id              AS message_version_id,
  mv.chat_message_id AS chat_message_id,
  cm.chat_id         AS chat_id,
  cm.message_id      AS message_id,
  cm.user_id         AS user_id,
  ts_headline('russian',
    coalesce(mv.normalized_text, '') || ' ' || coalesce(mv.caption, ''),
    plainto_tsquery('russian', :query),
    'MaxWords=' || :headline_max_words || ',MinWords=10,ShortWord=2,HighlightAll=false'
  )                  AS snippet,
  ts_rank_cd(mv.search_tsv, plainto_tsquery('russian', :query)) AS ts_rank,
  mv.captured_at     AS captured_at,
  cm.date            AS message_date
FROM message_versions mv
JOIN chat_messages cm
  ON cm.id = mv.chat_message_id
 AND cm.current_version_id = mv.id           -- only current version
WHERE
  cm.chat_id = :chat_id
  AND cm.memory_policy = 'normal'            -- INVARIANT #3 part 1
  AND cm.is_redacted = false                 -- INVARIANT #3 part 2
  AND mv.is_redacted = false                 -- INVARIANT #3 part 3 (defense-in-depth)
  AND mv.search_tsv @@ plainto_tsquery('russian', :query)
  AND NOT EXISTS (                           -- INVARIANT #9 tombstone exclusion
    SELECT 1 FROM forget_events fe
    WHERE fe.tombstone_key IN (
      'message:' || cm.chat_id || ':' || cm.message_id,
      'message_hash:' || coalesce(cm.content_hash, ''),
      'user:' || coalesce(cm.user_id::text, '')
    )
    AND fe.status IN ('pending', 'processing', 'completed')
  )
ORDER BY ts_rank DESC, captured_at DESC
LIMIT :limit;
```

**Defense-in-depth filter triple** (per invariant #3):
1. `chat_messages.memory_policy = 'normal'` (excludes nomem/offrecord/forgotten).
2. `chat_messages.is_redacted = false`.
3. `message_versions.is_redacted = false`.

**Tombstone exclusion:** uses three of the four `tombstone_key` formats from HANDOFF §10:
- `message:<chat_id>:<message_id>`
- `message_hash:<content_hash>`
- `user:<user_id>`

The fourth format (`export:<source>:<export_msg_id>`) is reserved for import-side rollback and does NOT need to be checked here (those rows already get redacted at import time).

**Tests (pytest):**
- Insert 3 chat_messages (normal, nomem, offrecord) + corresponding message_versions; query for common token → only the `normal` row returns.
- Insert chat_message with text "лошадь скачет"; search for "лошади" → matches via russian stemmer.
- Insert message + forget event on `message:<id>` → search returns empty.
- Insert message with `is_redacted=true` on chat_message → empty.
- Insert message with `is_redacted=true` on message_version → empty.
- Insert > 100 messages, search returns ≤ limit ordered by rank.
- Snippet contains query token (case-insensitive).
- Empty query → empty result, no SQL executed.
- Query injection attempt (`'; DROP TABLE...`) → safely passed through `plainto_tsquery`, no error.

**Acceptance:**
- All unit tests pass (`pytest -x --timeout=120 tests/services/test_search.py`).
- Performance: 1000-row dataset, query latency < 50ms p95 on local postgres (informational, not gate).
- Type-checks (`mypy bot/services/search.py`) clean.
- Ruff clean.
- No imports of LLM, vector, embedding libraries (defense vs Phase 5 drift).

**Risks:**
- `current_version_id IS NULL` for some chat_messages → those rows are skipped silently (acceptable: pre-version legacy rows are out of scope).
- `forget_events.tombstone_key` cardinality ratio with `chat_messages` count — index is UNIQUE so lookup is fast; the OR-list against three keys is the dominant cost. Add a comment to the query noting tradeoff.

**Stop signals:**
- If russian stemmer absent on staging → switch to `simple` AND update Stream A migration; surface as design question.
- If governance JOIN drops to seqscan on prod-shape data → flag in PR for index review.

---

### 5.C. Evidence bundle (Stream C → T4-03)

**File:** `bot/services/evidence.py` + tests.

**Purpose:** sealed contract between search results and the (future Phase 5) LLM gateway. The bundle:
- Carries citations + ranks + content snippets.
- Is JSON-serializable (for storing in `qa_traces`, for emitting to Phase 5 LLM context).
- Cannot mutate after construction (`@dataclass(frozen=True, slots=True)`).
- Carries NO content from forgotten/offrecord/redacted rows (enforced at construction time as defense-in-depth — the search service already filters, but `EvidenceBundle.from_hits` re-asserts).

**API:**

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

    def to_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    query: str
    chat_id: int
    items: tuple[EvidenceItem, ...]   # tuple, not list — frozen
    abstained: bool                    # True iff items == ()
    created_at: datetime               # bundle creation, distinct from message dates

    @classmethod
    def from_hits(
        cls,
        query: str,
        chat_id: int,
        hits: Sequence[SearchHit],
    ) -> "EvidenceBundle": ...

    def to_dict(self) -> dict[str, object]: ...

    @property
    def evidence_ids(self) -> list[int]:
        """For qa_traces audit; list of message_version_id."""
        ...
```

**`from_hits` rules:**
- If `hits` is empty → `abstained=True`, `items=()`.
- Items preserve `hits` order (search has already ranked).
- `to_dict()` returns a JSON-safe dict (dates as ISO 8601 strings).

**Defensive assertion (invariant #3):** for each `SearchHit` going into the bundle, `EvidenceBundle.from_hits` does **not** re-query the DB (would defeat the search service); instead, the contract is *the search service is the only producer of SearchHit and is already governance-filtered*. This is a contract test, not a runtime check.

**Tests:**
- Empty hits → `abstained=True`, `items=()`, `evidence_ids=[]`.
- 3 hits → `items` has 3 elements, ordered, `abstained=False`.
- Bundle is frozen (assigning to `.items` raises `FrozenInstanceError`).
- `to_dict()` is round-trip-safe (json.dumps then json.loads back).
- `to_dict()` schema stable across versions (snapshot test against a fixture).
- `evidence_ids` returns the expected list of `message_version_id`.

**Acceptance:**
- `pytest -x --timeout=120 tests/services/test_evidence.py` clean.
- mypy clean (frozen dataclass with explicit types).
- ruff clean.
- No LLM imports.
- Snapshot fixture committed to `tests/fixtures/evidence_bundle_v1.json` so future Phase 5 work has a ratified shape.

---

### 5.D. Q&A handler + feature flag (Stream D → T4-04)

**Files:**
- `bot/handlers/qa.py` (new handler module)
- Wire-in in `bot/__main__.py` (router registration, gated by feature flag)
- `bot/services/qa.py` (orchestration: search → bundle → format)

**Command:** `/recall <query>` (chosen over `/qa` to avoid q/a typing ambiguity in Russian; "recall" reads as "вспомни" semantically).

**Feature flag:** `memory.qa.enabled` (existing `feature_flags` table from Phase 1). Default OFF. Read via existing `feature_flag_repo.is_enabled(...)` helper.

**Authorization (Phase 4 scope only):**
- Must be invoked in a **group chat** that equals `settings.COMMUNITY_CHAT_ID` (single-community Phase 4 deployment; multi-chat is Phase 6+).
- Must be invoked by a user with `users.is_member=True` OR `users.is_admin=True`.
- `chat_id` for the search query = `message.chat.id` of the request (NEVER cross-chat search; invariant #1 + isolation).
- DM invocation → polite refusal: "Команда /recall работает только в community чате."

**Behaviour:**

1. Read feature flag. If OFF → silently return (do not even acknowledge — minimizes attack surface during rollout).
2. Validate authz. If fail → reply "Доступ только участникам сообщества." and return.
3. Parse query: `args = message.text[len('/recall '):].strip()`. Empty query → reply "Использование: `/recall <вопрос>`" and return.
4. Run `detect_policy(query, ...)` over the user's QUERY (not just messages). If result ≠ `'normal'` → redact query in the audit row (see Stream E) but still process search; do NOT echo the query back in the response.
5. Call `search_messages(session, query, chat_id=chat_id, limit=3)`.
6. Build `EvidenceBundle.from_hits(query, chat_id, hits)`.
7. Format response (Markdown):
   - If `bundle.abstained` → "Не нашёл подходящих свидетельств в истории чата." and return after writing the trace.
   - Else: render top-3 items, each as a quoted block with:
     - Snippet (with `<b>...</b>` from `ts_headline` converted to Markdown bold).
     - Author (from `users` lookup) + date (`captured_at` formatted local TZ).
     - Deep link: `https://t.me/c/<short_chat_id>/<message_id>` (Telegram private-chat deep link format).
   - **Never echo the user's query back** if `detect_policy(query) != 'normal'`.
8. Send reply to the user (reply-to original message).
9. Write `qa_traces` audit row (Stream E API).

**Telegram deep-link short_chat_id:** for supergroups, `short_chat_id = chat_id_str.removeprefix('-100')`. Verify on staging.

**Eval seed cases (T4-06, owned by Stream D):**
- File: `tests/fixtures/qa_eval_cases.json` — minimum 10 cases.
- Each: `{query, expected_evidence_present: bool, expected_chat_message_ids: [...], notes}`.
- Cover: Russian morphology (declension match), recency tiebreaker, abstention on no-match, governance exclusion (offrecord row should NOT appear in evidence), tombstone exclusion (forget event should remove an otherwise-matching row).
- Eval runner: `pytest tests/eval/test_qa_eval_cases.py` (loads cases, populates fixture chat, runs `/recall` simulation, asserts evidence_ids set membership).

**Tests:**
- Feature flag OFF → silent return.
- DM invocation → refusal.
- Non-member → refusal.
- Empty query → usage hint.
- Valid member + query with results → bundle populated, response rendered.
- Valid member + query with no results → abstention message.
- `detect_policy(query) == 'offrecord'` → query NOT echoed in response, audit row redacts query.
- All cases write a `qa_traces` row.
- Eval suite: 10/10 cases pass.

**Acceptance:**
- `pytest -x --timeout=120 tests/handlers/test_qa.py tests/eval/test_qa_eval_cases.py` clean.
- mypy + ruff clean.
- Feature flag off by default; smoke test verifies disabled state.
- No LLM imports.

---

### 5.E. qa_traces audit table (Stream E → T4-05)

**Files:**
- `alembic/versions/021_add_qa_traces.py` (depends on 020 — coordinate ordering with Stream A)
- `bot/db/models.py` — `QaTrace` model
- `bot/db/repos/qa_trace.py` — `QaTraceRepo.create(...)`
- Tests

**Schema:**

```sql
CREATE TABLE qa_traces (
  id            BIGSERIAL PRIMARY KEY,
  user_tg_id    BIGINT NOT NULL,                        -- not FK to users; intentionally loose
  chat_id       BIGINT NOT NULL,
  query_redacted BOOLEAN NOT NULL DEFAULT false,
  query_text    TEXT,                                   -- NULL iff query_redacted=true
  evidence_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,     -- list[int] of message_version_id
  abstained     BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_qa_traces_user_tg_id ON qa_traces (user_tg_id);
CREATE INDEX ix_qa_traces_chat_id_created_at ON qa_traces (chat_id, created_at);
```

**Why `user_tg_id` is BIGINT not FK:** users table uses Telegram user_id as PK already; loose BIGINT is fine and avoids cascade weirdness if a user is later forgotten via `/forget_me`. The audit trail must survive user removal (invariant #9 spirit applies to audit, not to PII).

**Forget cascade integration:** when a `/forget_me` cascade processes user `U`, the cascade worker MUST set `qa_traces.query_text = NULL, query_redacted = true` for all rows where `user_tg_id = U`. This is added to the cascade order (`bot/services/forget_cascade.py`) as a new layer between `fts_rows` and end. Stream E delivers the schema; cascade wiring is in Stream B's scope (since B owns the forget_cascade integration test).

**Repo API:**

```python
class QaTraceRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_tg_id: int,
        chat_id: int,
        query: str,
        evidence_ids: list[int],
        abstained: bool,
        redact_query: bool,
    ) -> QaTrace: ...
```

Repo flushes; caller commits. (Same pattern as `MessageVersionRepo`.)

**Tests:**
- Create row with `redact_query=False` → `query_text=='hello'`, `query_redacted=False`.
- Create row with `redact_query=True` → `query_text is None`, `query_redacted=True`.
- `evidence_ids` round-trips through JSONB.
- Cascade test: insert trace, run `/forget_me` cascade for user → trace's `query_text` becomes NULL.

**Acceptance:**
- `pytest -x --timeout=120 tests/db/test_qa_trace.py` clean.
- Migration 021 applies clean; down-migration drops table.
- mypy + ruff clean.
- No LLM imports.

---

## 6. Stream Allocation (parallel waves)

Phase 4 has clear dependency edges; we use 3 waves with 2-3 parallel streams per wave.

### Wave 1 — independent foundations (PARALLEL)

| Stream | Owner ticket | Files touched | Migration | Deps |
|---|---|---|---|---|
| **A** | T4-01 FTS schema | `alembic/versions/020_*`, `bot/db/models.py` | 020 | Phase 1 (DONE) |
| **C** | T4-03 Evidence bundle | `bot/services/evidence.py`, fixture, tests | — | Phase 1 (DONE) |
| **E** | T4-05 qa_traces table | `alembic/versions/021_*`, `bot/db/models.py`, `bot/db/repos/qa_trace.py` | 021 | Phase 1 (DONE) |

**Migration coordination:** Stream A delivers 020, Stream E delivers 021. They MUST land in this order. If Stream E PR opens first, hold merge until Stream A is merged; Stream E rebases. Both streams add a **single column / single table only** to `bot/db/models.py` so merge conflicts on that file are mechanical and resolvable.

**Why C is in Wave 1:** EvidenceBundle is a sealed contract; it depends only on the existence of `SearchHit` shape, which is fully specified in this plan. Stream C imports `SearchHit` from a stub module `bot/services/search_types.py` (which Stream B will adopt). This decoupling keeps C unblocked.

### Wave 2 — search service (after A)

| Stream | Owner ticket | Files | Deps |
|---|---|---|---|
| **B** | T4-02 search service | `bot/services/search.py`, `bot/db/repos/search.py` (or extension to `message_version.py`), tests; cascade wiring in `bot/services/forget_cascade.py` | Stream A merged (FTS index exists); Stream C merged (imports `SearchHit` if shape moved out of stub); Stream E merged (cascade integration test references `qa_traces` cleanup) |

Stream B is the longest-pole stream (largest scope: SQL + governance filter + tombstone exclusion + cascade wiring). Schedule it solo in Wave 2.

### Wave 3 — Q&A handler + eval (after B)

| Stream | Owner ticket | Files | Deps |
|---|---|---|---|
| **D** | T4-04 + T4-06 Q&A handler + flag + eval cases | `bot/handlers/qa.py`, `bot/services/qa.py`, `bot/__main__.py` (router), `tests/fixtures/qa_eval_cases.json`, eval runner | Streams B + C + E merged |

### Wave summary

```
Wave 1 (parallel):  A      C      E
                    │      │      │
                    ▼      │      │
Wave 2 (solo):      B ◄────┴──────┘
                    │
                    ▼
Wave 3 (solo):      D
```

**Estimated calendar time:** 2-3 days for Wave 1 (parallel), 1-2 days for Wave 2, 1-2 days for Wave 3. Total ~5 days assuming reviewers responsive.

---

## 7. Tickets

All tickets land as GitHub issues with label `phase:4`. Issue numbers populated after `gh issue create`.

| ID | Title | Stream | Size | Deps | GitHub # |
|---|---|---|---|---|---|
| **T4-01** | FTS schema: tsvector generated column + GIN index on `message_versions` | A | M | — | [#145](https://github.com/Jekudy/vibeshkoder/issues/145) |
| **T4-02** | Search service: `bot/services/search.py` with governance + tombstone filtering | B | L | T4-01, T4-03, T4-05 | [#146](https://github.com/Jekudy/vibeshkoder/issues/146) |
| **T4-03** | Evidence bundle: frozen `EvidenceBundle`/`EvidenceItem` dataclasses + JSON contract | C | S | — | [#147](https://github.com/Jekudy/vibeshkoder/issues/147) |
| **T4-04** | Q&A handler: `/recall` command + `memory.qa.enabled` feature flag (default OFF) | D | M | T4-02, T4-03, T4-05 | [#148](https://github.com/Jekudy/vibeshkoder/issues/148) |
| **T4-05** | qa_traces audit table + repo (`bot/db/repos/qa_trace.py`) | E | S | — | [#149](https://github.com/Jekudy/vibeshkoder/issues/149) |
| **T4-06** | Eval seed cases: ≥ 10 fixture cases for /recall correctness (rolled into Stream D) | D | S | T4-02, T4-04 | [#150](https://github.com/Jekudy/vibeshkoder/issues/150) |

### T4-01 acceptance (testable)

- Migration 020 applies clean and rolls back clean on a postgres 16 image.
- `message_versions.search_tsv` column exists, type `tsvector`, generated stored.
- Index `ix_message_versions_search_tsv` exists, type `gin`.
- For an inserted message_version with `normalized_text='тестовое сообщение'`, the SQL `SELECT 1 FROM message_versions WHERE search_tsv @@ plainto_tsquery('russian','тест')` returns the row.
- `pytest -x --timeout=120 tests/db/test_fts_schema.py` passes.
- ruff + mypy clean.

### T4-02 acceptance

- Public function `search_messages(session, query, *, chat_id, limit=3) -> list[SearchHit]` exists in `bot/services/search.py`.
- All 8 unit tests in §5.B pass.
- Performance smoke test: 1k-row corpus, p95 < 50ms.
- `bot/services/forget_cascade.py` `fts_rows` layer no longer returns `{status: 'skipped'}` — it deletes/redacts message_versions search content.
- Cascade integration test: forget event → search returns empty within one cascade pass.
- ruff + mypy clean. No LLM imports.

### T4-03 acceptance

- `EvidenceItem` and `EvidenceBundle` are frozen dataclasses (FrozenInstanceError on mutation attempt).
- `EvidenceBundle.from_hits(query, chat_id, [])` returns `abstained=True, items=()`.
- `to_dict()` round-trips through `json.dumps`/`loads`.
- Snapshot fixture `tests/fixtures/evidence_bundle_v1.json` committed.
- 6 unit tests in §5.C pass.
- ruff + mypy clean.

### T4-04 acceptance

- `/recall <query>` registered handler in `bot/__main__.py`, gated by `memory.qa.enabled`.
- Feature flag default OFF; bot smoke test confirms silent return.
- All 8 handler tests in §5.D pass.
- DM invocation → polite refusal.
- Non-member invocation → refusal.
- offrecord query → not echoed; audit redacted.
- ruff + mypy clean. No LLM imports.

### T4-05 acceptance

- Migration 021 applies and rolls back clean.
- `qa_traces` table + 2 indexes exist.
- `QaTraceRepo.create(...)` works for both `redact_query=True` and `False`.
- 4 repo tests pass.
- Cascade integration: `/forget_me` user → `qa_traces.query_text` NULL for that user's rows.
- ruff + mypy clean.

### T4-06 acceptance

- ≥ 10 cases in `tests/fixtures/qa_eval_cases.json`.
- Eval runner `tests/eval/test_qa_eval_cases.py` passes 10/10.
- Cases cover: morphology, recency, abstention, governance exclusion, tombstone exclusion (at least 1 case per category).

---

## 8. Stop Signals (apply to all streams)

A stream MUST stop and surface the issue as a PR comment / draft PR description if any of these fire:

- Invariant collision (e.g., a search returns offrecord content) — STOP, do not proceed.
- Test hangs > 120s — likely unmocked external call; investigate, do not retry.
- Migration conflict with another stream's migration number → coordinate via PR comment.
- A design choice would require LLM/vector/embedding code → STOP, this is Phase 5.
- ROADMAP or AUTHORIZED_SCOPE update lands during the cycle removing Phase 4 authorization → STOP, abort.

---

## 9. PR Workflow (per stream)

Per `~/.claude/rules/superflow-enforcement.md` Rule 3 + this project's CLAUDE.md:

1. Create worktree: `git worktree add .worktrees/p4-stream-<X> -b feat/p4-<X>-<slug> main`.
2. Implement (subagents do the code; orchestrator does state I/O).
3. Run `pytest -x --timeout=120 tests/...` + `ruff check .` + `mypy bot/` — paste evidence in PR description.
4. Unified Review (2 reviewers): Claude product + secondary technical (codex via plugin).
5. After both APPROVE → push → `gh pr create --label phase:4`.
6. Wait for CI green: `gh run list` + `gh run view <id>` if needed. **NEVER `gh pr merge --admin`.**
7. CI green → `gh pr merge <num> --rebase --delete-branch`.
8. Update `IMPLEMENTATION_STATUS.md` Phase 4 row.

Final Holistic Review **REQUIRED** for Phase 4 (≥ 4 sprints + parallel execution + governance-mode-relevant). Run after Wave 3 lands, before declaring Phase 4 closed.

---

## 10. Glossary (Phase 4-specific)

- **FTS:** Full-Text Search — PostgreSQL's `tsvector` + `tsquery` system.
- **search_tsv:** the generated tsvector column on `message_versions`.
- **SearchHit:** the row-shape returned by `search_messages`.
- **EvidenceBundle:** the sealed top-N envelope passed to (future) Phase 5 LLM gateway.
- **Abstention:** the bot's refusal to answer when bundle is empty.
- **Trace:** an audit row in `qa_traces`.
- **Recall:** the user-facing command name, semantically "вспомни".
