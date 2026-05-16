# Phase 9 Plan — Wiki / Community Catalog

**Status:** RATIFIED 2026-05-16. Authorized for implementation.
**Owner:** Orchestrator B (`ORCHESTRATOR_REGISTRY.md §2`).
**Predecessors:** Phase 6 (knowledge cards + admin review, CLOSED 2026-05-12), Phase 7
(daily digest, CLOSED 2026-05-15), Phase 8 (weekly editorial digest, CLOSED 2026-05-15),
Phase 11 (privacy binding suite, ACTIVE 42/42).
**Charter:** `governance_mode = standard`, `git_workflow_mode = sprint_pr_queue`.
Per-sprint PAR (Claude product + Codex technical). FHR mandatory at end of phase.
**Supersedes:** `docs/memory-system/prompts/PHASE9_PLAN_DRAFT.md` (promoted to canonical
path 2026-05-16; all Q1-Q7 + visibility_scope decisions finalized — see §13).

---

## §0. Implementation Status: AUTHORIZED

Phase 9 is authorized for implementation following Phase 8 closure 2026-05-15.
Sprint 0 must update `AUTHORIZED_SCOPE.md` (add Phase 9 authorization block) **before
any code lands**.

| Component | Status | Notes |
|---|---:|---|
| `knowledge_cards` / `card_sources` schema | Exists | Phase 6 migrations 030–035. `card_status='approved'` canonical filter in `bot/db/repos/knowledge_card.py:60, :84, :107`. `source_message_version_ids` JSONB inline on card — no separate `card_sources` table for this column. |
| `digests` / `digest_runs` | Exists | Phase 7 migration 037; Phase 8 migration 038. Digest archive linked by `digest_id` from wiki index (§5.F — digest archive section, not first-class wiki pages). |
| `llm_gateway` | Exists | `bot/services/llm_gateway.py`. Wiki renderer uses NO LLM calls — it is a pure Markdown-to-HTML pipeline. Invariant #2 is N/A to the renderer itself. |
| `forget_cascade.CASCADE_LAYER_ORDER` | Exists | `bot/services/forget_cascade.py:133`. Phase 9 inserts `"wiki_pages"` + `"wiki_revisions"` layers AFTER `"digests"` and BEFORE `"card_sources"` (see §5.F). |
| Web auth (`web/auth.py`) | Exists | Phase 9 extends the cookie payload with `role: 'admin' \| 'member'` field. Role derived from which password matched (`WEB_ADMIN_PASSWORD` → admin, `WEB_MEMBER_PASSWORD` → member). No user_id self-claim. |
| `web/routes/` package | Exists | `web/routes/auth.py`, `dashboard.py`, `health.py`, `members.py`, `cards.py`. Phase 9 adds `web/routes/wiki.py` following the same convention (NOT `web/routers/` — draft discrepancy resolved). |
| `feature_flags` table | Exists | Phase 9 flag: `memory.wiki.enabled` default OFF. Per-page `public_enabled` col gates public surface independently. |
| Migration counter | 038 on main (Phase 8) | Phase 9 starts at **050** per `ORCHESTRATOR_REGISTRY.md §2` Orch B exclusive window. T9-01 implementation re-verifies alembic head before use. |
| Phase 11 binding suite | Active — 42/42 | Phase 9 adds **18 new test IDs** across 5 new test files (see §10). |
| `wiki_pages` / `wiki_revisions` / `wiki_publication_log` | DOES NOT EXIST | New Phase 9 tables: migrations 050, 051, 052. |
| `wiki_page_card_sources` / `wiki_page_message_sources` | DOES NOT EXIST | New FK-normalized source junction tables (migration 050). Replaces JSONB arrays on `wiki_pages`. |

---

## §1. Non-Negotiable Invariants (verbatim from HANDOFF.md §1)

1. Existing gatekeeper must not break.
2. **No LLM calls outside `llm_gateway`.** Wiki renderer is a pure Markdown-to-HTML
   pipeline with no provider calls. No direct `anthropic` / `openai` imports in any
   `bot/services/wiki_*.py` or `web/routes/wiki.py` file.
3. **No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.** Wiki page
   rendering and search filter sources using `memory_policy='normal' AND is_redacted=FALSE`
   plus the no-active-forget-event check before any content is exposed.
4. **Citations point to `message_version_id` or approved card sources.** Every `[^mv:…]`
   token in `wiki_pages.body_markdown` must resolve to a live, non-redacted, non-forgotten
   `message_versions.id`. Every `[^card:…]` token must resolve to an approved, non-archived
   `knowledge_cards.id`.
5. **Summary is never canonical truth.** Wiki pages are curated, not authoritative; the
   source trace is the truth.
6. Graph is never source of truth. (N/A — Phase 10 boundary.)
7. Future butler cannot read raw DB directly. (N/A.)
8. Import apply must go through same normalization / governance path. (N/A.)
9. Tombstones are durable and not casually rolled back. Wiki pages citing a forgotten source
   must be archived or have the relevant body block masked — exercised by I7a/b/c.
10. **Public wiki remains disabled until review / source trace / governance are proven.**
    This is the primary invariant for Phase 9. No `public_enabled=true` without a
    corresponding `wiki_publication_log` row from `/wiki_publish`.

**Central for Phase 9:** invariants **#3, #4, #5, #10** are binding on every ticket.

---

## §2. Objective

Phase 9 turns the reviewed memory system (cards, digests) into a member-only web wiki
with controlled public publication. The default surface is **authenticated, member-only
access**. Wiki pages render from approved visible cards, with explicit source traces
resolving to `message_version_id` or approved card sources.

Public exposure is a two-step admin act: `/wiki_publish` (set `public_enabled=true`) +
optionally `/wiki_robots <slug> index` (allow search-engine indexing). Both writes are
atomic with `wiki_publication_log` insertion. Neither can occur on an unreviewed page,
a page with failed source trace, or a page citing forbidden content.

---

## §3. Authorized Scope

### Tables (new)
- `wiki_pages` — core page store (migration 050)
- `wiki_page_card_sources` — FK-normalized card source junction (migration 050; replaces JSONB)
- `wiki_page_message_sources` — FK-normalized message_version source junction (migration 050; replaces JSONB)
- `wiki_revisions` — edit history mirroring `message_versions` discipline (migration 051);
  new columns: `revision_status` ('active'/'forgotten_redacted'), `redacted_at`,
  `redacted_by_forget_event_id`, `source_message_version_ids_snapshot` JSONB (immutable
  audit snapshot, not FK-normalized)
- `wiki_publication_log` — append-only public-approval audit trail (migration 052)

### Services (new, `bot/services/wiki_*.py`)
- `bot/services/wiki_governance.py` — source/governance validator
- `bot/services/wiki_renderer.py` — server-side Markdown → HTML renderer with citation
  linkification
- `bot/services/wiki_publication.py` — publication service for `/wiki_publish` flow

### Handlers (new)
- `bot/handlers/wiki.py` — Telegram admin commands: `/wiki_publish`, `/wiki_unpublish`,
  `/wiki_robots`

### Web routes (new)
- `web/routes/wiki.py` — member-only GET routes + public-gated GET routes

### Web templates (new)
- `web/templates/wiki/index.html`
- `web/templates/wiki/page.html`
- `web/templates/wiki/search.html`

### Web auth extension
- `web/auth.py` — extend cookie payload with `role: 'admin' | 'member'`; two-password login (role derived from password match, no user_id self-claim)
- `web/config.py` — add `WEB_ADMIN_PASSWORD` (repurpose existing `WEB_PASSWORD`) and `WEB_MEMBER_PASSWORD` env settings

### Forget cascade extension
- `bot/services/forget_cascade.py` — add `"wiki_pages"` layer in `CASCADE_LAYER_ORDER`
  and `_LAYER_FUNCS` dict; add `_cascade_wiki_pages` function

### Migrations
- `alembic/versions/050_add_wiki_pages.py`
- `alembic/versions/051_add_wiki_revisions.py`
- `alembic/versions/052_add_wiki_publication_log.py`

### ORM
- `bot/db/models.py` — new ORM classes: `WikiPage`, `WikiRevision`, `WikiPublicationLog`

### Repos (new, `bot/db/repos/wiki_*.py`)
- `bot/db/repos/wiki_page.py` — `WikiPageRepo`
- `bot/db/repos/wiki_publication.py` — `WikiPublicationRepo`

### Feature flags
- `memory.wiki.enabled` (default OFF)

### Env vars (new)
- `WEB_MEMBER_PASSWORD` — plaintext password for member login (new env var); role derived from password match
- `WEB_ADMIN_PASSWORD` — repurposes existing `WEB_PASSWORD`; if both are set, admin takes precedence when passwords are identical (must differ in practice)
- `WEB_MEMBER_USER_IDS` — retained for display-name lookup / member directory; NOT used for auth role derivation

---

## §4. Non-Goals

- **No graph infrastructure.** No Neo4j, no Graphiti, no `graph_sync_runs`, no
  `networkx`, no graph traversal. Wiki "related pages" MUST NOT use graph inference.
  (Phase 10 boundary — `ORCHESTRATOR_REGISTRY.md §2`.)
- **No multilingual support.** Single locale (primarily Russian). Phase 9.5 candidate.
- **No static export.** Phase 9.5 candidate.
- **No first-class digest wiki pages.** Digest archive is a `/wiki/digests/` index
  section linking by `digest_id` to existing Phase 7/8 `digests` rows. Digests are
  NOT stored as `wiki_pages` rows.
- **No LLM calls in the wiki pipeline.** Renderer is a pure Markdown-to-HTML converter.
- **No wiki create/edit UI** in the web layer. Wiki pages are created and edited via
  Telegram admin commands or direct DB operations (scope to be ratified in a Phase 9.5
  ticket). T9-08 integration tests seed pages directly.
- **No two-admin publication quorum.** Single-admin + `wiki_publication_log` audit is
  the Phase 9 governance model (Q7). Quorum is a Phase 9.5 candidate.
- **No `card_revisions` table** (already deferred from Phase 6.5). `wiki_revisions`
  is Phase 9-scoped only.
- **Concurrent admin edits to `wiki_pages.body_markdown`** are last-writer-wins by
  `revision_seq`; no MVCC guard. Two simultaneous `/wiki_admin/edit/{slug}` flows =
  best-effort. Edit-conflict resolution deferred to Phase 9.5 (D5).

---

## §4.1. Phase 10 Read Surface Contract

Phase 10 (knowledge graph) will read from the Phase 9 wiki layer. This section documents
the exact contract Phase 10 must honor.

**Wiki state that is graph-readable (Phase 10 projection):**
- Only `wiki_pages` where `page_status='reviewed'` AND `validation_status='valid'`.
- Pages in `'stale'`, `'archived'`, or `'draft'` status are NOT projected into the graph.
- Pages with `validation_status='stale'` or `'invalid'` are NOT projected.

**Cascade extension point:**
Phase 10 adds a `"graph_provenance"` layer to `CASCADE_LAYER_ORDER`. This layer MUST
be inserted AFTER `"wiki_revisions"` and BEFORE `"card_sources"`:

```
New CASCADE_LAYER_ORDER after Phase 10 lands (informational — do not implement in Phase 9):
  chat_messages → message_versions → qa_traces → llm_synthesis_cache → qa_traces_llm
  → llm_usage_ledger → digests → wiki_pages → wiki_revisions
  → graph_provenance   ← Phase 10 inserts here
  → card_sources → message_entities → message_links → attachments → fts_rows
```

**Rationale:** `graph_provenance` derives from `card_sources` (graph edges reference
card source rows as provenance). The graph layer must purge its derived edges BEFORE
`card_sources` removes the source rows — otherwise FK integrity is violated.
`wiki_revisions` must run before `graph_provenance` because graph nodes may reference
wiki revision snapshots.

---

## §5. Detailed Designs

### §5.A. Schema

#### Migration 050: `wiki_pages` + `wiki_page_card_sources` + `wiki_page_message_sources`

<!-- BLOCKER A fix: join chain corrected; BLOCKER C: auth redesign; HIGH I: FK-normalized source tables;
     HIGH J: page_status='stale' added; MEDIUM D: validation columns; MEDIUM E: body_tsv FTS -->

```sql
CREATE TABLE wiki_pages (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug           TEXT NOT NULL UNIQUE,
  title          TEXT NOT NULL,
  body_markdown  TEXT NOT NULL DEFAULT '',
    -- raw Markdown; HTML rendered server-side at request time (Q2)
  body_tsv       TSVECTOR GENERATED ALWAYS AS (
                   to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(body_markdown, ''))
                 ) STORED,
    -- wiki-only FTS index (separate from /recall canonical evidence — wiki is derived/editable)
  visibility     TEXT NOT NULL DEFAULT 'member'
    CHECK (visibility IN ('member', 'admin', 'public_candidate')),
    -- 'member': authenticated members only
    -- 'admin': admin-only (internal working pages)
    -- 'public_candidate': reviewed, may be published after /wiki_publish
  public_enabled  BOOLEAN NOT NULL DEFAULT false,
    -- true only after a wiki_publication_log row is written by /wiki_publish
  robots_policy  TEXT NOT NULL DEFAULT 'noindex'
    CHECK (robots_policy IN ('noindex', 'index')),
    -- 'index' requires public_enabled=true AND explicit /wiki_robots admin call
  page_status    TEXT NOT NULL DEFAULT 'draft'
    CHECK (page_status IN ('draft', 'reviewed', 'stale', 'archived')),
    -- 'draft': not yet reviewed; invisible to members in listing
    -- 'reviewed': passed admin review; listed for members
    -- 'stale': automatic transition — any cited card became unapproved OR any source mv
    --          was forgotten/offrecord; forces public_enabled=false (see §5.H)
    -- 'archived': admin-driven or cascade-driven; no longer rendered
  -- Validation optimization columns (defense-in-depth complement; not the sole protection)
  last_validated_at            TIMESTAMPTZ,
  validation_status            TEXT CHECK (validation_status IN ('valid', 'stale', 'invalid')),
  invalidated_at               TIMESTAMPTZ,
  invalidated_by_forget_event_id BIGINT REFERENCES forget_events(id) ON DELETE SET NULL,
  created_by_user_id   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  reviewed_by_user_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at          TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Constraints
ALTER TABLE wiki_pages ADD CONSTRAINT ck_wiki_pages_robots_index_requires_public CHECK (
  robots_policy <> 'index' OR public_enabled = true
);
ALTER TABLE wiki_pages ADD CONSTRAINT ck_wiki_pages_reviewed_requires_sources CHECK (
  -- sources are now enforced via FK junction tables; this constraint is a belt-and-suspenders
  -- signal enforced at service layer (wiki_governance) rather than inline SQL
  page_status NOT IN ('reviewed', 'stale', 'archived') OR true
  -- NOTE: actual source presence check is done at service layer against
  --       wiki_page_card_sources + wiki_page_message_sources counts
);
ALTER TABLE wiki_pages ADD CONSTRAINT ck_wiki_pages_public_requires_reviewed CHECK (
  public_enabled = false OR page_status IN ('reviewed', 'stale')
    -- stale pages retain public_enabled until cascade or admin explicitly unpublishes;
    -- the stale transition itself forces public_enabled=false (see _cascade_wiki_pages)
);
ALTER TABLE wiki_pages ADD CONSTRAINT ck_wiki_pages_stale_not_public CHECK (
  page_status <> 'stale' OR public_enabled = false
);

-- Indexes
CREATE INDEX ix_wiki_pages_status_reviewed ON wiki_pages (page_status)
  WHERE page_status = 'reviewed';
CREATE INDEX ix_wiki_pages_public_enabled ON wiki_pages (public_enabled)
  WHERE public_enabled = true;
CREATE INDEX ix_wiki_pages_body_tsv ON wiki_pages USING GIN (body_tsv);
CREATE INDEX ix_wiki_pages_last_validated ON wiki_pages (last_validated_at);

-- FK-normalized source tables (HIGH I fix: replaces JSONB arrays on wiki_pages)
-- Correct join chain: wiki_page_message_sources.message_version_id
--   → message_versions.id → message_versions.chat_message_id → chat_messages.id
-- (NOT message_versions.message_id — that's the external Telegram integer id)

CREATE TABLE wiki_page_card_sources (
  wiki_page_id  UUID    NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  card_id       UUID    NOT NULL REFERENCES knowledge_cards(id) ON DELETE RESTRICT,
  position      INTEGER NOT NULL,
  PRIMARY KEY (wiki_page_id, card_id)
);
CREATE INDEX ix_wpcs_card_id ON wiki_page_card_sources (card_id);

CREATE TABLE wiki_page_message_sources (
  wiki_page_id       UUID    NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  message_version_id BIGINT  NOT NULL REFERENCES message_versions(id) ON DELETE RESTRICT,
  position           INTEGER NOT NULL,
  PRIMARY KEY (wiki_page_id, message_version_id)
);
CREATE INDEX ix_wpms_mvid ON wiki_page_message_sources (message_version_id);
```

**Visibility view (correct join chain):** The governance validator and renderer derive
visibility of each cited message source via the following join (mirroring `bot/services/search.py`):

```sql
-- Correct join chain (BLOCKER A): message_versions.chat_message_id is the FK to chat_messages
-- NOT message_versions.message_id (which is the external Telegram message integer)
SELECT
    wpms.wiki_page_id,
    wpms.message_version_id,
    mv.is_redacted        AS mv_is_redacted,
    c.memory_policy       AS cm_memory_policy,
    c.is_redacted         AS cm_is_redacted,
    c.chat_id             AS chat_id,
    c.message_id          AS telegram_message_id,
    c.user_id             AS user_id,
    mv.content_hash       AS content_hash
FROM wiki_page_message_sources AS wpms
JOIN message_versions AS mv
    ON mv.id = wpms.message_version_id
JOIN chat_messages AS c
    ON c.id = mv.chat_message_id   -- CORRECT: chat_message_id, not message_id
WHERE
    c.memory_policy = 'normal'
    AND c.is_redacted = false
    AND mv.is_redacted = false
    AND NOT EXISTS (
        SELECT 1 FROM forget_events AS fe
        WHERE fe.status IN ('pending', 'processing', 'completed')
          AND (
              -- tombstone key shape 1: message:{chat_id}:{message_id}
              fe.tombstone_key = 'message:' || c.chat_id::text || ':' || c.message_id::text
              OR (
                  -- tombstone key shape 2: message_hash:{content_hash}
                  mv.content_hash IS NOT NULL
                  AND fe.tombstone_key = 'message_hash:' || mv.content_hash
              )
              OR (
                  -- tombstone key shape 3: user:{user_id}
                  c.user_id IS NOT NULL
                  AND fe.tombstone_key = 'user:' || c.user_id::text
              )
          )
    );
```

**Transitive source resolution:** The validator MUST resolve BOTH:
1. **Direct** `wiki_page_message_sources.message_version_id` rows (direct mv citations)
2. **Transitive** path: `wiki_page_card_sources.card_id` → `card_sources.message_version_id`
   → apply same join chain above. A page citing a card whose `card_sources` include any
   forgotten/offrecord mvid MUST fail validation (L9c binding test).

**Rollback:** `DROP TABLE wiki_page_message_sources; DROP TABLE wiki_page_card_sources; DROP TABLE wiki_pages CASCADE;`

#### Migration 051: `wiki_revisions`

<!-- HIGH J fix: add revision_sources_resolved_at for re-validation tracking;
     note: JSONB snapshot kept as immutable audit trail (not normalized like live sources)
     but revision body must not leak forgotten content — cascade layer handles masking -->

```sql
CREATE TABLE wiki_revisions (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  wiki_page_id                UUID NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  revision_seq                INTEGER NOT NULL,
    -- monotonically increasing per page; start 1
  body_markdown               TEXT NOT NULL,
  -- Source snapshot at edit time: kept as JSONB for immutable audit trail
  -- (unlike live wiki_page_card_sources / wiki_page_message_sources which are FK-normalized)
  -- The cascade layer (_cascade_wiki_revisions) masks body_markdown for forgotten sources
  source_card_ids_snapshot             JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_message_version_ids_snapshot  JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- Tracks when revision sources were last re-validated against current forget_events state
  revision_sources_resolved_at  TIMESTAMPTZ,
  -- Privacy: tracks redaction state of the revision body
  revision_status               VARCHAR(32) NOT NULL DEFAULT 'active'
    CHECK (revision_status IN ('active', 'forgotten_redacted')),
  redacted_at                   TIMESTAMPTZ,
  redacted_by_forget_event_id   BIGINT REFERENCES forget_events(id) ON DELETE SET NULL,
  edited_by_user_id             BIGINT REFERENCES users(id) ON DELETE SET NULL,
  edited_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
  edit_reason                   TEXT,
  created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (wiki_page_id, revision_seq)
);
```

**Mirrors `message_versions` discipline** (Q3 ratification). The current body is
always on `wiki_pages.body_markdown`; `wiki_revisions` is an audit trail only.

**Revision source JSONB is kept (not FK-normalized)** because revisions are historical
snapshots — the FK targets (cards, mvids) may be deleted after the fact, and RESTRICT
would block cascade. Instead, the `_cascade_wiki_revisions` layer masks `body_markdown`
for forgotten sources and updates `revision_sources_resolved_at`. If any future UI ever
renders revision bodies, it MUST re-validate sources first (I7e test, §10).

**Rollback:** `DROP TABLE wiki_revisions;`

#### Migration 052: `wiki_publication_log`

```sql
CREATE TABLE wiki_publication_log (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  wiki_page_id         UUID NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  action               TEXT NOT NULL
    CHECK (action IN ('publish', 'unpublish', 'robots_index', 'robots_noindex', 'legacy_cookie_grace')),
  actor_user_id        BIGINT REFERENCES users(id) ON DELETE SET NULL,
  prior_public_enabled BOOLEAN NOT NULL,
  new_public_enabled   BOOLEAN NOT NULL,
  prior_robots_policy  TEXT NOT NULL,
  new_robots_policy    TEXT NOT NULL,
  source_check_result  JSONB NOT NULL,
    -- structured validation payload from wiki_governance.validate_sources(...)
  reason               TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_wiki_pub_log_page_id ON wiki_publication_log (wiki_page_id, created_at DESC);
```

**Append-only.** No UPDATE or DELETE on this table — ever. Any new publication state
change creates a new row.

**Rollback:** `DROP TABLE wiki_publication_log;`

---

### §5.B. Server-Side Renderer Contract

**Module:** `bot/services/wiki_renderer.py`

**Privacy invariant block (top of every render call):**
- Before emitting any content, call `wiki_governance.validate_sources(session, page)`.
- If ANY source fails (`#nomem`, `#offrecord`, forgotten, redacted, archived card):
  either suppress that citation block or return `page_status='archived'` signal — never
  render forbidden content.

**Render batching (MEDIUM D):** The renderer MUST issue a **single batched SQL** per page
covering all direct + transitive citation mvids. No N-queries-per-citation pattern. The
governance validator uses one query joining `wiki_page_message_sources` + `wiki_page_card_sources`
→ `card_sources` → `message_versions` → `chat_messages` to collect all source mvids and
their forget-event state in one round-trip. `wiki_pages.last_validated_at` and
`wiki_pages.validation_status` are updated as an optimization cache — the batch SQL always
runs regardless (not the sole protection).

**Wiki FTS (MEDIUM E):** Wiki search runs against `wiki_pages.body_tsv` GIN index
(see §5.A migration 050). Wiki search is SEPARATE from `bot/services/search.py` and
does NOT join `/recall` canonical evidence. Wiki pages are derived and editable — not
source of truth. `/recall` handler must not be modified to include wiki pages.

**Input:**
```python
@dataclass(frozen=True)
class WikiRenderInput:
    page: WikiPage
    session: AsyncSession
    role: Literal['admin', 'member']   # affects broken-source visibility
```

**Output:**
```python
@dataclass(frozen=True)
class WikiRenderResult:
    html_body: str              # sanitized HTML via bleach/markupsafe allowlist
    citations: list[CitationRef]
    broken_sources: list[str]   # non-empty only for admins; hidden from members
    page_archived: bool         # true if any source failed governance
```

**Citation tokens:** `[^mv:<message_version_id>]` and `[^card:<card_id>]`. Any
unresolvable token or governance-failed source:
- For **admin role:** renders as `[⚠ SOURCE UNAVAILABLE — <id>]`.
- For **member role:** suppresses the token; if the containing bullet has no remaining
  valid citations, the entire bullet is hidden.

**HTML sanitization:** `bleach` allowlist — `p`, `strong`, `em`, `ul`, `ol`, `li`,
`a` (href restricted to `/` paths or anchors), `code`, `pre`, `blockquote`, `h2`,
`h3`, `b`. No raw `<script>`, `<iframe>`, `<style>`, or inline event handlers.

**No LLM calls.** The renderer is a deterministic Markdown parser. Forbidden by
invariant #2.

---

### §5.C. Governance Validator Contract

**Module:** `bot/services/wiki_governance.py`

**Privacy invariant block:** implements invariant #3 for the wiki layer.

```python
@dataclass(frozen=True)
class SourceCheckResult:
    valid: bool
    invalid_card_ids: list[str]         # UUIDs failing governance
    invalid_mvids: list[int]            # message_version_ids failing governance
    failure_reasons: dict[str, str]     # id → reason string
    checked_at: datetime

async def validate_sources(
    session: AsyncSession,
    page: WikiPage,
) -> SourceCheckResult: ...
```

**Rejection criteria (ANY source that matches ANY of these is invalid):**

<!-- BLOCKER A fix: join chain is message_versions.chat_message_id → chat_messages.id;
     all 3 tombstone key shapes explicitly listed; transitive card_sources path required -->

1. Any cited `message_version_id` (from `wiki_page_message_sources`) where:
   - `message_versions.is_redacted = TRUE`, OR
   - `chat_messages.memory_policy != 'normal'` (join via `mv.chat_message_id`, NOT `mv.message_id`), OR
   - `chat_messages.is_redacted = TRUE`, OR
   - an active `forget_events` row exists matching ANY of the 3 tombstone key shapes:
     - `'message:' || c.chat_id::text || ':' || c.message_id::text`
     - `'message_hash:' || mv.content_hash` (if `mv.content_hash IS NOT NULL`)
     - `'user:' || c.user_id::text` (if `c.user_id IS NOT NULL`)
2. Any cited `card_id` (from `wiki_page_card_sources`) where `knowledge_cards.card_status != 'approved'`
   or `knowledge_cards.card_status = 'archived'`
3. Any cited `card_id` where ALL of its `card_sources.message_version_id` entries have been
   forgotten (transitive check — same join chain and tombstone predicate as criterion 1)
4. **Transitive path (L9c binding test):** Any cited `card_id` whose `card_sources` includes
   any `message_version_id` that fails criterion 1 (even if the card itself is `approved`).
   A wiki page citing an approved card whose underlying source messages are offrecord MUST
   fail validation.

**Visibility resolution:** Both direct (`wiki_page_message_sources`) AND transitive
(`wiki_page_card_sources` → `card_sources` → `message_version_id`) sources are resolved
in a single batched SQL (see §5.B render batching). The validator issues ONE query covering
all mvids before returning `SourceCheckResult`.

**Visibility GAP resolution (Q8):** Phase 9 ships the source visibility check as a
server-side computed batch query in the validator (option 1 from the draft gap analysis) —
no new column on `knowledge_cards`. The validator derives visibility from
`chat_messages.memory_policy + is_redacted` of every cited `message_version_id` (direct
and transitive). This is the strictest safe interpretation of invariant #3 and requires
no Phase 6 schema change.

**Output:** `SourceCheckResult.valid = False` if any source fails. The dict is
serialized to `wiki_publication_log.source_check_result` JSONB at publication time.

---

### §5.D. Web Routes and Auth Role

**Module:** `web/routes/wiki.py`

**Privacy invariant block (top of every route handler):**
```python
# 1. Auth check — member or admin cookie required for member routes
# 2. feature_flag check — memory.wiki.enabled must be ON
# 3. page_status='reviewed' filter applied before any DB query
# 4. validate_sources() called before render on every page view
```

**Endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/wiki` | member/admin | Index of `page_status='reviewed'`, `visibility IN ('member','public_candidate')` pages |
| `GET` | `/wiki/{slug}` | member/admin | Page view — governance-validated render |
| `GET` | `/wiki/search` | member/admin | FTS over `title` + `body_markdown` of reviewed pages |
| `GET` | `/wiki/digests` | member/admin | Digest archive index linking to existing `digests` rows by `digest_id` |
| `GET` | `/wiki/public/{slug}` | none (public) | Only if `public_enabled=true`; re-runs governance before render; returns 404 if disabled |
| `GET` | `/wiki/robots.txt` | none | Emits `Disallow: /wiki/public/` for pages where `robots_policy='noindex'`; page-level `index` is opt-in post-publication |

**Auth role extension (`web/auth.py`) — BLOCKER C fix: two-password model:**

<!-- BLOCKER C fix: replace user_id self-claim design with two-password model.
     Old design (STRUCK): POST /login with password AND user_id; role from ADMIN_IDS membership.
     Risk: a member knowing WEB_PASSWORD could type an admin user_id and get admin role.
     New design: role is derived exclusively from WHICH password matched. No user_id self-claim. -->

```python
# Cookie payload (additive extension — no user_id field)
{
  "role": "admin" | "member"   # derived from password match, not from self-claimed user_id
}
```

Role derivation at login (`POST /login` — password field only, no user_id field):
1. If password matches `WEB_ADMIN_PASSWORD` → role `'admin'`.
2. Elif password matches `WEB_MEMBER_PASSWORD` → role `'member'`.
3. Else → 403 Forbidden.

**`WEB_ADMIN_PASSWORD`** repurposes the existing `WEB_PASSWORD` env var (migration path:
set `WEB_ADMIN_PASSWORD` to the current value; `WEB_PASSWORD` is aliased for one release
cycle, then deprecated). **`WEB_MEMBER_PASSWORD`** is a new separate env var.

**Legacy cookie migration:** Cookies without a `role` field (issued by previous code) are
treated as `role='admin'` for one max-age window (7 days), logged via `WARN`, and refreshed
on the next request with an explicit `role` claim. After max-age expiry the cookie is
invalidated — forced re-login. This ensures existing admin sessions are not broken during
the transition. (I7d binding test.)

**`web/config.py` extension:**
```python
class WebSettings(BaseSettings):
    # ... existing fields ...
    WEB_ADMIN_PASSWORD: str          # replaces WEB_PASSWORD (backward alias provided)
    WEB_MEMBER_PASSWORD: str         # new — separate password for member login
    WEB_MEMBER_USER_IDS: list[int] = []  # retained for member directory display; NOT used for auth role
```

**Member visibility rules:**
- Members see only `page_status='reviewed'` pages where `visibility IN ('member', 'public_candidate')`.
- Admins see all statuses including `draft` and `admin` visibility.
- Broken-source warnings are shown only to admins.
- `public_enabled` toggle is not exposed in the web UI — only via Telegram `/wiki_publish`.

---

### §5.E. Admin Handlers (Telegram Commands)

**Module:** `bot/handlers/wiki.py`

**Privacy invariant block (top of every handler):**
```python
actor = message.from_user
if not _is_admin(message):
    await message.reply("Команда доступна только администраторам.")
    return
```

`_is_admin` pattern follows `bot/handlers/admin_cards.py:58-61` (Phase 6 canonical).

#### `/wiki_publish <slug> [reason]`

Publication flow (all steps in a single transaction):

1. Verify actor is admin.
2. `SELECT wiki_pages WHERE slug=:slug FOR UPDATE`.
3. Require `page_status='reviewed'` — else refuse with "Страница не прошла ревью."
4. Require at least one row in `wiki_page_card_sources` OR `wiki_page_message_sources` for this page — else refuse with "Нет источников." (calls `wiki_governance.assert_publishable(page)` which raises `WikiSourcesMissingError` when both junction tables have zero rows).
5. Call `wiki_governance.validate_sources(session, page)` — if `valid=False`, refuse with source check summary.
6a. `SELECT public_enabled, robots_policy INTO prior_pe, prior_rp FROM wiki_pages WHERE id=:page_id FOR UPDATE;`
   (The `FOR UPDATE` in step 2 already locks the row; this step reads the locked current values into local variables before the UPDATE fires. In SQLAlchemy, use `page.public_enabled` and `page.robots_policy` from the already-locked `page` object — they are the `prior_*` values.)
6b. `UPDATE wiki_pages SET public_enabled=:new_pe, robots_policy=:new_rp WHERE id=:page_id;`
   (Where `:new_pe=true` and `:new_rp` preserves the existing `robots_policy` for a publish action — robots policy is not changed by `/wiki_publish`.)
6c. `INSERT INTO wiki_publication_log (action='publish', actor_user_id, prior_public_enabled=:prior_pe, new_public_enabled=:new_pe, prior_robots_policy=:prior_rp, new_robots_policy=:new_rp, source_check_result, reason);`
   **Critical:** `prior_public_enabled` reads the ACTUAL value from the row locked by the `FOR UPDATE` in step 2 — NOT hardcoded `false`. A second `/wiki_publish` on an already-public page must log `prior_public_enabled=true`, not `false`.
8. COMMIT.
9. Reply: "✓ Страница `<slug>` опубликована. Поисковая индексация: noindex (используй /wiki_robots <slug> index для включения)."

**Key invariant:** `public_enabled=true` can only be set here. No web toggle. No direct DB path. Enforce via `ck_wiki_pages_public_requires_reviewed` DB constraint as defense-in-depth.

#### `/wiki_unpublish <slug> [reason]`

1. Admin check.
2. `SELECT wiki_pages WHERE slug=:slug FOR UPDATE`.
3. `UPDATE wiki_pages SET public_enabled=false, robots_policy='noindex', updated_at=now()`.
4. `INSERT INTO wiki_publication_log (action='unpublish', ...)`.
5. COMMIT.
6. Reply: "✓ Страница `<slug>` снята с публикации. Маршрут /wiki/public/<slug> немедленно вернёт 404."

#### `/wiki_robots <slug> index|noindex [reason]`

1. Admin check.
2. Load page. For `index`: require `public_enabled=true` AND fresh `validate_sources` pass.
3. `UPDATE wiki_pages SET robots_policy=:new_policy, updated_at=now()`.
4. `INSERT INTO wiki_publication_log (action='robots_index'|'robots_noindex', ...)`.
5. COMMIT.
6. Reply with new policy state.

---

### §5.F. Forget Cascade Integration

**Module:** `bot/services/forget_cascade.py`

**New layers:** `"wiki_pages"` and `"wiki_revisions"` inserted into `CASCADE_LAYER_ORDER`
AFTER `"digests"` and BEFORE `"card_sources"`:

<!-- HIGH B fix: wiki_pages + wiki_revisions go BEFORE card_sources, AFTER digests.
     Rationale: transitive validation of cards (card_sources.message_version_id) must be
     possible when wiki cascade runs; card_sources purge cannot precede wiki cascade. -->

```python
CASCADE_LAYER_ORDER: tuple[str, ...] = (
    "chat_messages",
    "message_versions",
    "qa_traces",
    "llm_synthesis_cache",
    "qa_traces_llm",
    "llm_usage_ledger",
    "digests",
    "wiki_pages",       # Phase 9 — AFTER digests, BEFORE wiki_revisions and card_sources
    "wiki_revisions",   # Phase 9 — AFTER wiki_pages, BEFORE card_sources
    "card_sources",
    "message_entities",
    "message_links",
    "attachments",
    "fts_rows",
)
```

**Run-order rationale:** `"wiki_pages"` MUST run AFTER `"digests"` so digest citation
scans complete before any card_source rows disappear. `"wiki_pages"` and `"wiki_revisions"`
MUST run BEFORE `"card_sources"` so transitive card-source validation is still possible
when identifying which wiki body blocks to archive or mask. `"wiki_revisions"` runs after
`"wiki_pages"` because revision body redaction depends on the page's new state.

**New function: `_cascade_wiki_pages`**

```python
async def _cascade_wiki_pages(session: AsyncSession, event) -> int:
    """
    Phase 9 forget cascade layer. On a forget event targeting a message_version_id
    or card_id cited in wiki_pages (via wiki_page_message_sources / wiki_page_card_sources
    FK-normalized tables), either:
      a. Set page_status='stale' + public_enabled=False if some but not all sources are
         invalidated; OR set page_status='archived' if no remaining valid sources, OR
      b. Mask the affected [^mv:...] / [^card:...] citation token in body_markdown with
         [REDACTED — забыто] when partial sources remain.
    Writes a wiki_revisions row for every page modified (edit_reason='forget_cascade').
    Returns count of affected wiki_pages rows.
    """
```

**New function: `_cascade_wiki_revisions`**

```python
async def _cascade_wiki_revisions(session: AsyncSession, event) -> int:
    """
    Phase 9 forget cascade layer for wiki_revisions (I7e binding test).
    Finds all wiki_revisions rows whose source_message_version_ids_snapshot JSONB
    overlaps with the forget event's target message_version_ids (computed from the
    event's tombstone_key shapes via the same join chain as _cascade_wiki_pages).

    For each matching revision row:
      - Sets body_markdown = '[CONTENT_REDACTED: forget_event_id={event.id}]'
        (curly braces interpolated with the actual integer event id at write time)
      - Sets revision_status = 'forgotten_redacted'
      - Sets redacted_at = now()
      - Sets redacted_by_forget_event_id = event.id
      - Sets revision_sources_resolved_at = now()

    This layer runs AFTER _cascade_wiki_pages (wiki_pages must already reflect stale/
    archived state before revision bodies are redacted). Runs BEFORE card_sources.
    Returns count of affected wiki_revisions rows.
    """
```

**Redaction placeholder verbatim:** `[CONTENT_REDACTED: forget_event_id={n}]` where `n`
is the integer `forget_event.id`. Example: `[CONTENT_REDACTED: forget_event_id=42]`.
The placeholder is written to `body_markdown` at cascade time and is immutable thereafter.

**Applicable target types:**
```python
_LAYER_APPLICABLE_TARGET_TYPES["wiki_pages"] = frozenset({"message", "message_hash", "user"})
_LAYER_APPLICABLE_TARGET_TYPES["wiki_revisions"] = frozenset({"message", "message_hash", "user"})
```

**Privacy stop signal:** if `_cascade_wiki_pages` cannot acquire a lock within `statement_timeout=5s`, it logs and skips the page (same pattern as `_cascade_digests` in Phase 7 §5.H). The governance validator catches stale state on next render.

**`/wiki_publish` advisory lock + forget re-check (HIGH B fix):**

`/wiki_publish` runs inside a single transaction and MUST:
1. Acquire per-mvid advisory locks for ALL direct (`wiki_page_message_sources`) AND transitive
   (`wiki_page_card_sources` → `card_sources.message_version_id`) mvids, in ascending
   `message_version_id` order (same deterministic order as `_process_one_event` to prevent
   deadlocks).
2. Re-run `wiki_governance.validate_sources(session, page)` INSIDE the guarded UPDATE —
   i.e., after the advisory locks are held — as defense against the forget-during-publish race.
   Rationale: forget event insertion does NOT acquire mvid advisory locks (it uses its own
   per-event lock), so a forget event can be inserted between the initial `validate_sources`
   call and the final UPDATE. The re-check inside the guarded UPDATE closes this race window.
3. Only if BOTH the lock acquisition AND the in-transaction re-check pass does the UPDATE
   `SET public_enabled=true` proceed.

```python
# Pseudocode for /wiki_publish guarded UPDATE
async with session.begin():
    page = await session.get(WikiPage, page_id, with_for_update=True)
    # Capture prior state INSIDE the lock (H1 fix: not hardcoded false)
    prior_public_enabled = page.public_enabled
    prior_robots_policy = page.robots_policy
    all_mvids = sorted(
        direct_mvids(page) | transitive_mvids(page)
    )
    for mvid in all_mvids:
        await acquire_advisory_lock(conn, mvid)   # same helper as forget_cascade
    # Re-check INSIDE the lock window
    result = await validate_sources(session, page)
    if not result.valid:
        raise SourceValidationError(result)
    page.public_enabled = True
    # INSERT wiki_publication_log with actual prior values (atomic with UPDATE)
    session.add(WikiPublicationLog(
        wiki_page_id=page.id,
        action='publish',
        prior_public_enabled=prior_public_enabled,   # actual value, not False
        new_public_enabled=True,
        prior_robots_policy=prior_robots_policy,
        new_robots_policy=prior_robots_policy,
        source_check_result=result.to_dict(),
        reason=reason,
    ))
```

---

### §5.G. Public Surface Gating

Per-page `public_enabled` is the primary gate. Defense-in-depth layers:

<!-- HIGH F fix: mandatory Cache-Control: no-store headers on public surface -->

1. **DB constraint:** `ck_wiki_pages_public_requires_reviewed` — `public_enabled=true` requires `page_status IN ('reviewed', 'stale')`. Stale pages retain the DB flag until cascade or admin explicitly unpublishes.
2. **DB constraint:** `ck_wiki_pages_robots_index_requires_public` — `robots_policy='index'` requires `public_enabled=true`.
3. **DB constraint:** `ck_wiki_pages_stale_not_public` — `page_status='stale'` forces `public_enabled=false` (set atomically by cascade layer).
4. **Route layer:** `GET /wiki/public/{slug}` checks `public_enabled=true` at the start, before any content query; returns 404 if false.
5. **Governance revalidation:** `/wiki/public/{slug}` always calls `validate_sources` even if page is public; returns 410 Gone if any source has since been forgotten.
6. **Publication log gate:** `wiki_publication_log` row is the only write path to `public_enabled=true`. Any direct `UPDATE wiki_pages SET public_enabled=true` without the log row violates the audit contract — log row is inserted in the same transaction (atomic).
7. **Feature flag:** `memory.wiki.enabled=false` disables all wiki routes including the public route.
8. **Mandatory cache headers (HIGH F):** `GET /wiki/public/{slug}` and `GET /robots.txt` (wiki variant)
   MUST respond with:
   ```http
   Cache-Control: no-store, max-age=0, must-revalidate
   ```
   No CDN trust — per-request source revalidation even when `public_enabled=true`. This prevents
   CDN or browser caches from serving stale public pages after unpublish or forget events.
   Implemented at the route handler level (response header, not middleware) so it applies even
   when other routes use different caching strategies.

**Digest archive section (`/wiki/digests`):** links to existing `digests` rows by `digest_id`. Digests have their own redaction layer (`_cascade_digests` in Phase 7). The wiki digest index section renders only `status IN ('posted', 'redacted')` digests and respects their existing redaction state. No new privacy surface.

---

### §5.H. Lifecycle Invalidation and Stale-Page Reconciler

<!-- HIGH J fix: page_status='stale' state machine + daily reconciliation job -->

**`wiki_pages.page_status` state machine:**

```
draft ──────────────────────────────────────────────────► archived
  │                                                         ▲   ▲
  │  (admin review)                                         │   │
  ▼                                                         │   │
reviewed ──────────────────────────────────────────────────►│   │
  │                                                         │   │
  │  (forget cascade hits cited mv OR card status change)   │   │
  ▼                                                         │   │
stale ────────────────────────────────────────────────────►─┘   │
  │  (+ forces public_enabled=false atomically)                 │
  │                                                             │
  └─ (admin decision: re-review → reviewed, or archive ───────►─┘
```

States:
- **`draft`** → initial state on page creation
- **`reviewed`** → admin applied approval; page listed for members
- **`stale`** → automatic transition when ANY of these fire:
  - (a) forget cascade hits a cited `message_version_id` (sync, inside `_cascade_wiki_pages`)
  - (b) a cited card transitions to non-`approved` status (async daily reconciliation)
  - (c) source validation fails at render time (sync, governance validator returns `valid=False`)
  - On stale transition: `public_enabled` is forced to `false` atomically in the same UPDATE.
    `ck_wiki_pages_stale_not_public` DB constraint enforces this as defense-in-depth.
- **`archived`** → admin-driven or cascade-driven; page no longer rendered anywhere

**Daily stale-page reconciler job (`bot/services/wiki_publication.py::reconcile_stale_pages`):**

Runs once per day (scheduled alongside digest jobs). Queries:
```sql
SELECT wp.id
FROM wiki_pages wp
WHERE wp.page_status = 'reviewed'
  AND EXISTS (
    SELECT 1 FROM wiki_page_card_sources wpcs
    JOIN knowledge_cards kc ON kc.id = wpcs.card_id
    WHERE wpcs.wiki_page_id = wp.id
      AND kc.card_status <> 'approved'
  )
```
For each matching page: transitions `page_status → 'stale'`, sets `public_enabled=false`,
writes a `wiki_revisions` row with `edit_reason='reconciler_stale_transition'`.

This job handles case (b) only (card status drift). Cases (a) and (c) are handled
synchronously by the cascade layer and renderer respectively.

**`wiki_revisions.body_markdown` privacy policy:**

Wiki revisions are audit-trail-only and must not become a forget-leak surface:
- `wiki_revisions` rows are never rendered to members or public. They are accessible
  only to admins via direct DB access (no UI rendering path in Phase 9).
- The `_cascade_wiki_revisions` layer (added to `CASCADE_LAYER_ORDER` after `wiki_pages`)
  sets `body_markdown = '[CONTENT_REDACTED: forget_event_id={n}]'` (with `n` interpolated
  from the actual `forget_event.id`) in revision rows whose
  `source_message_version_ids_snapshot` JSONB overlaps with the forget event's target mvids.
  Also sets `revision_status='forgotten_redacted'`, `redacted_at=now()`, and
  `redacted_by_forget_event_id=event.id`.
- `revision_sources_resolved_at` timestamp is updated after each cascade masking pass to
  track when revision sources were last re-validated. At any UI render point (if added in
  Phase 9.5), the renderer MUST re-validate revision sources before exposing body. (I7e binding test.)

---

## §6. Wave Dependency Diagram

```
Wave 1 (parallel, foundations):
  T9-01 Schema + ORM + repos (migration 050/051/052)
  T9-02 Governance validator (wiki_governance.py)
  T9-03 Auth role extension (web/auth.py + web/config.py)

           │            │            │
           └────────────┴────────────┘
                        │
Wave 2 (sequential after all Wave 1):
                T9-04 Renderer (wiki_renderer.py)
                        │
Wave 3 (parallel after T9-04):
  T9-05 Web routes + templates (wiki.py + wiki/*.html)
  T9-06 Admin handlers (bot/handlers/wiki.py)
  T9-07 Forget cascade layer (_cascade_wiki_pages)
                        │
                        ▼
Wave 4 (sequential, after T9-05/06/07):
                T9-08 Phase 11 binding + integration tests
```

**T9-01 is the hard dependency** for all other tickets (schema must exist before any
service or handler references `WikiPage`, `WikiRevision`, or `WikiPublicationLog`).

T9-02 and T9-03 can run in parallel with T9-01 if the implementer mocks the ORM. In
practice, do T9-01 first, then fan out.

---

## §7. Sprint Breakdown

### T9-01: Wiki Schema Migrations (Wave 1)

**Migration numbers:** 050 (`wiki_pages`), 051 (`wiki_revisions`), 052 (`wiki_publication_log`)

**Description:** Three Alembic migrations plus ORM classes and repo skeletons. No business
logic — pure schema + data access layer.

**Dependencies:** Phase 8 CLOSED (038 is alembic head). `AUTHORIZED_SCOPE.md` updated.

**Phase 11 binding:** G1 (no-graph boundary — `wiki_pages` model must import nothing from
`bot.services.graph_*`).

**Files touched:**
- `alembic/versions/050_add_wiki_pages.py` (new)
- `alembic/versions/051_add_wiki_revisions.py` (new)
- `alembic/versions/052_add_wiki_publication_log.py` (new)
- `bot/db/models.py` — add `WikiPage`, `WikiRevision`, `WikiPublicationLog` ORM classes
- `bot/db/repos/wiki_page.py` (new) — `WikiPageRepo` with CRUD + slug lookup + status filter
- `bot/db/repos/wiki_publication.py` (new) — `WikiPublicationRepo` with append-only insert
- `docs/memory-system/AUTHORIZED_SCOPE.md` — add Phase 9 authorization block

**New test file:**
- `tests/services/test_wiki_schema.py` — migration smoke + ORM round-trips

**Acceptance criteria (AC):**
- [ ] `alembic upgrade head` applies all three migrations cleanly on a fresh Postgres DB.
- [ ] `alembic downgrade -1` from 052 → 051 → 050 → 049 cleans up without error.
- [ ] `wiki_pages.public_enabled=false` and `robots_policy='noindex'` are defaults; a
  bare `INSERT` without those fields confirms defaults.
- [ ] DB constraint `ck_wiki_pages_robots_index_requires_public` rejects
  `INSERT (robots_policy='index', public_enabled=false)` with IntegrityError.
- [ ] DB constraint `ck_wiki_pages_public_requires_reviewed` rejects
  `INSERT (public_enabled=true, page_status='draft')` with IntegrityError.
- [ ] DB constraint `ck_wiki_pages_stale_not_public` rejects
  `INSERT (page_status='stale', public_enabled=true)` with IntegrityError.
- [ ] `wiki_governance.assert_publishable(page)` raises `WikiSourcesMissingError` when
  `page.page_status='reviewed'` but zero rows exist in BOTH `wiki_page_card_sources`
  AND `wiki_page_message_sources` for that page — service-layer source-presence guard
  (replaces removed JSONB `ck_wiki_pages_reviewed_requires_sources` which was a no-op).
- [ ] `wiki_page_card_sources` has FK to `knowledge_cards(id) ON DELETE RESTRICT`; inserting
  a row with a non-existent `card_id` raises IntegrityError.
- [ ] `wiki_page_message_sources` has FK to `message_versions(id) ON DELETE RESTRICT`; inserting
  a row with a non-existent `message_version_id` raises IntegrityError.
- [ ] `wiki_page_message_sources` and `wiki_page_card_sources` rows cascade-delete when parent
  `wiki_pages` row is deleted.
- [ ] `wiki_pages.body_tsv` is populated by the generated column (non-empty for non-empty body).
- [ ] GIN index `ix_wiki_pages_body_tsv` exists and supports `@@` tsvector queries.
- [ ] `WikiPublicationRepo.insert` is append-only: no `update` or `delete` method on that repo.
- [ ] `WikiRevision` has `UNIQUE (wiki_page_id, revision_seq)` enforced at DB level.
- [ ] `WikiPage` ORM class imports nothing from `bot.services.graph_*` (G1 lint).
- [ ] CI alembic heads check: `alembic heads | wc -l` == 1 (MEDIUM G — single-head guard).
- [ ] `tests/services/test_wiki_schema.py` passes green.

---

### T9-02: Wiki Source / Governance Validator (Wave 1)

**Description:** `bot/services/wiki_governance.py` — `validate_sources(session, page) →
SourceCheckResult`. Pure read-only service. No DB writes.

**Dependencies:** T9-01 (ORM must exist).

**Phase 11 binding:** L9a (offrecord source does not surface), L9c (transitive forget),
L9d (message_hash forget), L9e (user-id forget), C8a (mv_id citation validity).

**Files touched:**
- `bot/services/wiki_governance.py` (new)
- `tests/services/test_wiki_governance.py` (new)

**Acceptance criteria (AC):**
- [ ] `validate_sources` returns `valid=True` for a page citing only `card_status='approved'`
  cards and non-redacted, non-forgotten `message_version_id` rows.
- [ ] `validate_sources` returns `valid=False` + correct `invalid_card_ids` for a page
  citing a `card_status='archived'` card.
- [ ] `validate_sources` returns `valid=False` + correct `invalid_mvids` for a page citing
  a `message_version_id` where `is_redacted=True`.
- [ ] `validate_sources` returns `valid=False` for a page citing a `message_version_id`
  where `memory_policy='offrecord'` (L9a).
- [ ] `validate_sources` returns `valid=False` for a page citing a `message_version_id`
  with an active `forget_events` row (`status IN ('pending','processing','completed')`).
- [ ] `validate_sources` returns `valid=False` for a page citing a card where ALL
  `card_sources` rows have been forgotten.
- [ ] **Transitive (L9c):** `validate_sources` returns `valid=False` for a page citing an
  `approved` card whose `card_sources` includes a `message_version_id` that is forgotten
  or `memory_policy='offrecord'` — even if the card itself is `approved`.
- [ ] **message_hash tombstone (L9d):** `validate_sources` returns `valid=False` when
  `forget_events.tombstone_key = 'message_hash:' || mv.content_hash` matches a cited mvid.
- [ ] **user tombstone (L9e):** `validate_sources` returns `valid=False` when
  `forget_events.tombstone_key = 'user:' || c.user_id::text` matches a cited mvid's author.
- [ ] Validator issues a **single batched SQL** covering all direct + transitive source mvids
  (not N queries per citation — see MEDIUM D).
- [ ] Join chain used is `message_versions.chat_message_id → chat_messages.id`
  (NOT `message_versions.message_id` — verified by test asserting correct column name).
- [ ] `SourceCheckResult` serializes to JSON (for `wiki_publication_log.source_check_result`).
- [ ] No graph imports anywhere in `wiki_governance.py` (G1 lint).
- [ ] `tests/services/test_wiki_governance.py` covers all 12 scenarios above.

---

### T9-03: Auth Role Extension (Wave 1)

**Description:** Extend `web/auth.py` cookie payload with `role: 'admin' | 'member'`. Replace
single `WEB_PASSWORD` with two-password model (`WEB_ADMIN_PASSWORD` + `WEB_MEMBER_PASSWORD`).
Add `WEB_MEMBER_PASSWORD` to `web/config.py`. Update login form (password field only — no user_id
self-claim). Add legacy cookie migration.

<!-- BLOCKER C fix: two-password model; role derived from password match, no user_id self-claim -->

**Dependencies:** None (independent of T9-01/02).

**Phase 11 binding:** R6.a (non-admin cannot `/wiki_publish` — tested at handler layer in
T9-06, but auth role is the prerequisite), R6.e (member typing admin user_id cannot escalate —
by design impossible in two-password model), I7d (legacy cookie without `role` field gets
admin-window-grace, not permanent admin).

**Files touched:**
- `web/auth.py` — replace single-password login with two-password role derivation; add legacy
  cookie migration logic; remove `user_id` from login form
- `web/config.py` — add `WEB_ADMIN_PASSWORD` (alias for `WEB_PASSWORD`), `WEB_MEMBER_PASSWORD`
- `tests/web/test_auth_role.py` (new)

**Acceptance criteria (AC):**
- [ ] `POST /login` with `WEB_ADMIN_PASSWORD` returns cookie with `role='admin'` (no user_id required).
- [ ] `POST /login` with `WEB_MEMBER_PASSWORD` (different from admin password) returns
  cookie with `role='member'`.
- [ ] `POST /login` with an incorrect password returns 403 regardless of any `user_id` supplied.
- [ ] **R6.e:** A member knowing `WEB_MEMBER_PASSWORD` cannot obtain `role='admin'` by
  supplying an admin `user_id` in the form — user_id field is ignored for role derivation.
- [ ] **I7d:** A cookie without a `role` field is treated as `role='admin'` for one max-age
  window (7 days), emits a `WARN` log, is refreshed with an explicit `role='admin'` on
  the next request, AND inserts a `wiki_publication_log` row with `action='legacy_cookie_grace'`
  (audit visibility for legacy session promotions). After max-age expiry, the cookie
  is invalidated → forced re-login.
- [ ] Legacy `WEB_PASSWORD` env is aliased to `WEB_ADMIN_PASSWORD` for backward compatibility
  (one release cycle deprecation warning emitted to logs on startup).
- [ ] `WEB_MEMBER_PASSWORD` must not equal `WEB_ADMIN_PASSWORD` — startup raises
  `ConfigurationError` if they are the same.
- [ ] Existing admin session tests remain green (backward-compatible extension).
- [ ] `tests/web/test_auth_role.py` passes green with all 9 scenarios above.

---

### T9-04: Server-Side Wiki Renderer (Wave 2)

**Description:** `bot/services/wiki_renderer.py` — pure Markdown-to-HTML pipeline with
citation linkification and governance-checked suppression. No LLM calls.

**Dependencies:** T9-01 (ORM), T9-02 (governance validator).

**Phase 11 binding:** L9b (forgotten card source triggers stale-render or 410), C8 (mv_id
validity — renderer resolves every `[^mv:...]` token against governance).

**Files touched:**
- `bot/services/wiki_renderer.py` (new)
- `tests/services/test_wiki_renderer.py` (new)

**Acceptance criteria (AC):**
- [ ] Renderer produces sanitized HTML for a page with valid approved-card citations.
- [ ] `[^mv:<id>]` token resolving to a valid non-redacted `message_version_id` renders
  as an inline citation link (C8).
- [ ] `[^mv:<id>]` token resolving to a forgotten/redacted/offrecord `message_version_id`
  is suppressed in member role output; shown as `[⚠ SOURCE UNAVAILABLE]` for admin role.
- [ ] `[^card:<id>]` token resolving to an archived card triggers `page_archived=True` in
  `WikiRenderResult` (L9b).
- [ ] Raw `<script>` injected in `body_markdown` is stripped from HTML output.
- [ ] Raw `<img>` with external `src` is stripped from HTML output (bleach allowlist).
- [ ] A page with ALL cited sources failing governance returns `page_archived=True` and
  empty `html_body`.
- [ ] No LLM imports anywhere in `wiki_renderer.py` (invariant #2 + G1 lint).
- [ ] `tests/services/test_wiki_renderer.py` covers all 8 scenarios above.

---

### T9-05: Member Wiki Router and Templates (Wave 3)

**Description:** `web/routes/wiki.py` with all GET endpoints. Three Jinja2 templates under
`web/templates/wiki/`. App wiring in `web/app.py`.

**Dependencies:** T9-01 (ORM/repos), T9-02 (governance), T9-03 (auth role), T9-04 (renderer).

**Phase 11 binding:** R6.c (admin cannot publish page with `page_status != 'reviewed'`), R6.d
(robots-`index` requires `public_enabled=true`) — enforced at route layer.

**Files touched:**
- `web/routes/wiki.py` (new)
- `web/templates/wiki/index.html` (new)
- `web/templates/wiki/page.html` (new)
- `web/templates/wiki/search.html` (new)
- `web/app.py` — register `wiki_router` (or `include_router` equivalent per existing pattern)
- `tests/web/test_wiki_routes.py` (new)

**Acceptance criteria (AC):**
- [ ] Anonymous request to `GET /wiki` returns 302 redirect to `/login`.
- [ ] Non-member authenticated request (role not set) to `GET /wiki` returns 403.
- [ ] Member request to `GET /wiki` returns 200 with only `page_status='reviewed'` pages.
- [ ] Member request to `GET /wiki/{slug}` for a `page_status='draft'` page returns 404.
- [ ] Member request to `GET /wiki/{slug}` for a page with all sources forgotten returns
  410 Gone (governance revalidation).
- [ ] `GET /wiki/public/{slug}` returns 404 when `public_enabled=false`.
- [ ] `GET /wiki/public/{slug}` returns 200 when `public_enabled=true` and sources valid.
- [ ] **HIGH F — Cache-Control:** `GET /wiki/public/{slug}` response includes header
  `Cache-Control: no-store, max-age=0, must-revalidate` on EVERY response (200, 404, 410).
- [ ] **HIGH F — Cache-Control:** `GET /robots.txt` (wiki variant) includes same
  `Cache-Control: no-store, max-age=0, must-revalidate` header.
- [ ] **R6.f:** After `/wiki_unpublish` + source forget: `GET /wiki/public/{slug}` returns
  404 or 410 on the NEXT request (no stale cache response).
- [ ] `GET /wiki/search?q=foo` returns only reviewed pages matching the query.
- [ ] **Search SQL safety (binds R6.g new):** GET /wiki/search?q=... uses `plainto_tsquery('russian', :q)` with bound parameter; AC includes test passing q=`'; DROP TABLE wiki_pages; --` returning HTTP 200 with empty result set and zero parser errors.
- [ ] **H2 — SQL injection:** `GET /wiki/search?q=...` uses `plainto_tsquery('russian', :q)`
  with a bound parameter — no string concatenation; passing `q` containing SQL metacharacters
  (`; DROP TABLE wiki_pages; --`) returns an empty result set without error (no injection).
- [ ] **L9c transitive offrecord (member view):** `GET /wiki/{slug}` of a `reviewed` page
  where exactly one of 3 cited transitive mvids has `chat_messages.memory_policy='offrecord'`
  returns 200 with that citation bullet suppressed from the rendered body (rendered as nothing
  OR as `[citation withheld]`); NO `[⚠]` marker visible to member role; admin debug view
  (if present) may show the marker. (Binds to test L9c.)
- [ ] **Member route transitive offrecord (binds L9c):** GET /wiki/{slug} of a `reviewed` page where exactly 1 of 3 cited transitive mvids has `chat_messages.memory_policy='offrecord'` returns HTTP 200 with that citation bullet replaced by `[citation withheld]` (no `[⚠]` marker visible to member role; admin debug view may show marker).
- [ ] `GET /wiki` returns 503 when `memory.wiki.enabled=false` (flag gate).
- [ ] Templates render citation/source-trace section for every page view.
- [ ] Admin-only broken-source warnings are absent from member role renders.
- [ ] `tests/web/test_wiki_routes.py` covers all 16 scenarios above.

---

### T9-06: Admin Telegram Handlers (Wave 3)

**Description:** `bot/handlers/wiki.py` implementing `/wiki_publish`, `/wiki_unpublish`,
`/wiki_robots`.

**Dependencies:** T9-01 (ORM), T9-02 (governance), T9-05 (router must exist so handlers
can reference slugs meaningfully in replies).

**Phase 11 binding:** R6.a (non-admin cannot `/wiki_publish`), R6.b (admin cannot publish
`page_status != 'reviewed'`), R6.c (admin cannot publish page with failed source trace),
R6.d (robots-`index` requires `public_enabled=true`).

**Files touched:**
- `bot/handlers/wiki.py` (new)
- `bot/__main__.py` or handler registration module — register `/wiki_publish`,
  `/wiki_unpublish`, `/wiki_robots` commands
- `tests/handlers/test_wiki_handlers.py` (new)

**Acceptance criteria (AC):**
- [ ] `/wiki_publish` from non-admin returns refusal message, no DB write (R6.a).
- [ ] `/wiki_publish` on a `page_status='draft'` page returns "Страница не прошла ревью."
  with no `public_enabled` change (R6.b).
- [ ] `/wiki_publish` on a `page_status='reviewed'` page with empty sources returns
  "Нет источников." with no `public_enabled` change (R6.c precondition).
- [ ] `/wiki_publish` on a page with a failed source (offrecord mv_id) returns source
  check summary with no `public_enabled` change (R6.c).
- [ ] Successful `/wiki_publish` sets `public_enabled=true` AND inserts exactly one
  `wiki_publication_log` row in the same transaction.
- [ ] `public_enabled` CANNOT be set to `true` without a `wiki_publication_log` row
  (enforced by transaction atomicity + DB constraint as defense-in-depth).
- [ ] `/wiki_unpublish` sets `public_enabled=false` AND `robots_policy='noindex'` AND
  inserts `wiki_publication_log(action='unpublish')`.
- [ ] `/wiki_robots <slug> index` refuses when `public_enabled=false` (R6.d).
- [ ] `/wiki_robots <slug> index` on a valid public page sets `robots_policy='index'` AND
  inserts `wiki_publication_log(action='robots_index')`.
- [ ] `/wiki_robots <slug> noindex` always succeeds (for any page) and inserts
  `wiki_publication_log(action='robots_noindex')`.
- [ ] `tests/handlers/test_wiki_handlers.py` covers all 10 scenarios.

---

### T9-07: Forget Cascade Layer `wiki_pages` + `wiki_revisions` (Wave 3)

**Description:** Add `_cascade_wiki_pages` and `_cascade_wiki_revisions` to
`bot/services/forget_cascade.py`. Insert `"wiki_pages"` and `"wiki_revisions"` into
`CASCADE_LAYER_ORDER` between `"digests"` and `"card_sources"`.
Add both to `_LAYER_FUNCS` and `_LAYER_APPLICABLE_TARGET_TYPES`.

<!-- HIGH B fix: wiki_revisions layer added; advisory lock + re-check in /wiki_publish;
     L9d/L9e tombstone target types verified -->

**Dependencies:** T9-01 (ORM — `WikiPage`, `WikiRevision` must exist).

**Phase 11 binding:** I7a (forget event on cited mv_id triggers wiki_page re-evaluation),
I7b (forget event on a card_source triggers same path), I7c (cascade order: wiki layers run
AFTER digests layer, BEFORE card_sources layer).

**Files touched:**
- `bot/services/forget_cascade.py` — `_cascade_wiki_pages`, `_cascade_wiki_revisions`,
  `CASCADE_LAYER_ORDER`, `_LAYER_FUNCS`, `_LAYER_APPLICABLE_TARGET_TYPES` (shared file — serialize via PR)
- `bot/services/wiki_publication.py` — `/wiki_publish` advisory lock + in-transaction
  re-check of `validate_sources` (HIGH B)
- `bot/db/models.py` — ensure `WikiRevision` ORM is importable from cascade module
- `tests/services/test_wiki_cascade.py` (new)

**Acceptance criteria (AC):**
- [ ] `_cascade_wiki_pages` is present in `_LAYER_FUNCS` dict at `forget_cascade.py`.
- [ ] `_cascade_wiki_revisions` is present in `_LAYER_FUNCS` dict at `forget_cascade.py`.
- [ ] `"wiki_pages"` appears in `CASCADE_LAYER_ORDER` after `"digests"` and before
  `"wiki_revisions"` (exact position — I7c).
- [ ] `"wiki_revisions"` appears in `CASCADE_LAYER_ORDER` after `"wiki_pages"` and before
  `"card_sources"` (exact position — I7c).
- [ ] A forget event targeting a `message_version_id` in `wiki_page_message_sources`
  causes the page to transition to `page_status='stale'` (or `'archived'` if no valid
  sources remain) and forces `public_enabled=false` (I7a).
- [ ] A forget event targeting a `message_version_id` in `wiki_page_message_sources`
  masks the corresponding `[^mv:…]` token in `body_markdown` when other valid sources
  remain (I7a — partial forget).
- [ ] Same behaviors for a `card_id` in `wiki_page_card_sources` (I7b).
- [ ] **L9d:** A `message_hash:` tombstone key in `forget_events` matching a cited mvid's
  `content_hash` triggers wiki page re-evaluation (same as message-id forget).
- [ ] **L9e:** A `user:` tombstone key in `forget_events` matching a cited mvid's author
  triggers wiki page re-evaluation.
- [ ] `_cascade_wiki_pages` writes a `wiki_revisions` row with `edit_reason='forget_cascade'`
  for every page modified (including stale transition).
- [ ] **I7e (sub-AC A):** `_cascade_wiki_revisions` sets `body_markdown =
  '[CONTENT_REDACTED: forget_event_id={n}]'` (with `n` = actual `forget_event.id`) for
  every `wiki_revisions` row whose `source_message_version_ids_snapshot` JSONB overlaps
  with the forget event's target mvids.
- [ ] **I7e (sub-AC B):** `_cascade_wiki_revisions` sets `revision_status='forgotten_redacted'`,
  `redacted_at=now()`, `redacted_by_forget_event_id=event.id`, and
  `revision_sources_resolved_at=now()` on each masked revision row (I7e).
- [ ] Cascade layer order is verifiable:
  `CASCADE_LAYER_ORDER.index("wiki_pages") > CASCADE_LAYER_ORDER.index("digests")`
  AND `CASCADE_LAYER_ORDER.index("wiki_pages") < CASCADE_LAYER_ORDER.index("wiki_revisions")`
  AND `CASCADE_LAYER_ORDER.index("wiki_revisions") < CASCADE_LAYER_ORDER.index("card_sources")` (I7c).
- [ ] `_LAYER_APPLICABLE_TARGET_TYPES["wiki_pages"] == frozenset({"message","message_hash","user"})`.
- [ ] `_LAYER_APPLICABLE_TARGET_TYPES["wiki_revisions"] == frozenset({"message","message_hash","user"})`.
- [ ] `/wiki_publish` acquires per-mvid advisory locks for all direct + transitive mvids
  before the guarded UPDATE, and re-runs `validate_sources` inside the lock window (HIGH B).
- [ ] **H3 — scale AC:** Single forget event invalidating N=50 wiki pages sharing the same
  cited mvid completes in a single advisory-lock-guarded transaction; `_cascade_wiki_pages`
  returns `count=50`. Test fixture seeds 50 wiki pages all referencing the same
  `message_version_id` (see `tests/services/test_wiki_cascade.py` stress fixture).
- [ ] **Bulk cascade scale (binds I7f new):** Single forget event invalidating N=50 wiki pages via shared mvid completes in single advisory-lock-guarded transaction; AC verifies via test fixture creating 50 pages all citing the same mvid, then firing forget event, asserting cascade returns count=50 and all pages transition to `page_status='stale'` AND `public_enabled=false`.
- [ ] `tests/services/test_wiki_cascade.py` covers all 17 scenarios.

---

### T9-08: Phase 11 Binding Tests + Integration (Wave 4)

**Description:** All 5 new `tests/evals/` files for Phase 9 privacy binding. End-to-end
integration test covering the full candidate → card → wiki page → render → forget cascade
pipeline. AST import lint for G1. Updates `docs/memory-system/IMPLEMENTATION_STATUS.md`.

<!-- HIGH H fix: test count expanded from 11 to 18 new IDs (L9c/d/e, C8a/b, I7d/e, R6.e/f added) -->

**Scope note (D4):** T9-08 spans 5 test files + AST import check + end-to-end drift
simulator + `IMPLEMENTATION_STATUS.md` update + Phase 9.5 carryover list (§15). This is
approximately 1.5 sprint slots if executed linearly.
**Recommended execution:** dispatch 5 parallel sub-implementers, one per test file
(`test_wiki_leakage`, `test_wiki_citations`, `test_wiki_cascade`, `test_wiki_refusal`,
`test_wiki_no_graph`), then sequential AST lint + drift sim + docs pass. If linear, allocate
1.5× the standard sprint budget.

**Dependencies:** T9-01 through T9-07 (all).

**Phase 11 binding:** L9a/b/c/d/e, C8a/b, I7a/b/c/d/e, R6.a/b/c/d/e/f, G1 — owns all **18 new IDs**.

**Files touched:**
- `tests/evals/test_wiki_leakage.py` (new) — L9a, L9b, L9c, L9d, L9e
- `tests/evals/test_wiki_citations.py` (new) — C8a, C8b
- `tests/evals/test_wiki_cascade.py` (new) — I7a, I7b, I7c, I7d, I7e
- `tests/evals/test_wiki_refusal.py` (new) — R6.a, R6.b, R6.c, R6.d, R6.e, R6.f
- `tests/evals/test_wiki_no_graph.py` (new) — G1 (AST/import lint)
- `docs/memory-system/IMPLEMENTATION_STATUS.md` — Phase 9 rows updated

**Acceptance criteria (AC):**
- [ ] **L9a:** `GET /wiki/{slug}` body and `GET /wiki/search` results do NOT contain content
  from any `message_version_id` where `memory_policy='offrecord'`.
- [ ] **L9b:** A forget event on a cited `card_source_id` triggers `page_status='stale'`/`'archived'`
  or 410 Gone on the next render.
- [ ] **L9c:** Transitive forget — wiki page cites an approved card whose `card_sources` includes
  a forgotten/offrecord mvid. Page MUST mask/stale (approved card status is insufficient).
- [ ] **L9d:** A `message_hash:` tombstone forget invalidates wiki pages citing that mvid
  (not only `message:`-key forget).
- [ ] **L9e:** A `user:` tombstone forget invalidates wiki pages citing any message from that user.
- [ ] **C8a:** Every `[^mv:<id>]` token in `wiki_pages.body_markdown` resolves to an existing,
  non-redacted, non-forgotten `message_versions.id`; any that don't are suppressed from
  member-role output.
- [ ] **C8b:** Revision citations (`wiki_revisions.source_message_version_ids_snapshot`) are
  also validated at render time — forgotten content does not leak via revision body even
  if a UI ever renders them.
- [ ] **I7a:** Forget event on a cited `message_version_id` triggers wiki_page re-evaluation
  (status → `stale`/`archived` or body block masked).
- [ ] **I7b:** Forget event on a `card_source` triggers same path as I7a.
- [ ] **I7c:** `CASCADE_LAYER_ORDER.index("wiki_pages") > CASCADE_LAYER_ORDER.index("digests")`
  AND `CASCADE_LAYER_ORDER.index("wiki_pages") < CASCADE_LAYER_ORDER.index("wiki_revisions")`
  AND `CASCADE_LAYER_ORDER.index("wiki_revisions") < CASCADE_LAYER_ORDER.index("card_sources")`
  asserted in test (import from `bot.services.forget_cascade`).
- [ ] **I7d:** Legacy cookie without `role` field — first request treated as admin (grace window),
  response refreshes cookie with explicit `role='admin'`; second request after max-age has
  explicit role claim. WARN logged on first request.
- [ ] **I7e:** `wiki_revisions.body_markdown` is masked for forgotten content by
  `_cascade_wiki_revisions`; `revision_sources_resolved_at` updated after masking.
- [ ] **R6.a:** Non-admin cannot call `/wiki_publish` (returns refusal, `public_enabled`
  unchanged).
- [ ] **R6.b:** Admin cannot publish page with `page_status != 'reviewed'` (refusal,
  unchanged).
- [ ] **R6.c:** Admin cannot publish page with failed source trace (offrecord source causes
  governance failure → refusal).
- [ ] **R6.d:** `robots_policy='index'` cannot be set when `public_enabled=false` (DB
  constraint + handler layer both enforced).
- [ ] **R6.e:** Member typing admin user_id cannot escalate to admin role — two-password model
  makes this structurally impossible; test verifies login with `WEB_MEMBER_PASSWORD` always
  returns `role='member'` regardless of any `user_id` param in the request body.
- [ ] **R6.f:** Unpublish + immediate forget — `GET /wiki/public/{slug}` returns 410 or 404
  on the next request after unpublish+forget cycle (`Cache-Control: no-store` verified in
  response headers).
- [ ] **G1:** AST/import scan of all `bot/services/wiki_*.py`, `web/routes/wiki.py`,
  `bot/handlers/wiki.py` confirms zero imports of `bot.services.graph_*`, `neo4j`,
  `graphiti`, `networkx` — test uses `ast.parse` or `importlib` inspection, same
  pattern as `tests/evals/test_no_llm_imports.py`.
- [ ] All 18 binding tests pass green before T9-08 PR is opened.
- [ ] `IMPLEMENTATION_STATUS.md` Phase 9 section updated to reflect T9-01..T9-08 status.

---

## §8. Stop Signals

A sprint MUST stop and surface the issue as a PR comment / draft PR description if ANY of
these fire:

1. **`public_enabled=true` set without `wiki_publication_log` row** → invariant #10
   breach → HARD STOP.
2. **`#offrecord` / `#nomem` / forgotten content rendered** in wiki page body, search
   results, citation, or public route → invariant #3 breach → HARD STOP.
3. **Wiki page published without `page_status='reviewed'`** → governance bypass →
   HARD STOP.
4. **`robots_policy='index'` set without `public_enabled=true`** → DB constraint
   violation; if constraint is missing — HARD STOP and fix migration.
5. **Wiki route returns content before member/admin auth check** → invariant #10 /
   public leak → HARD STOP.
6. **Renderer imports `bot.services.graph_*`, `neo4j`, `graphiti`, or `networkx`** → Phase
   10 boundary violation → HARD STOP.
7. **`wiki_pages` layer absent from `CASCADE_LAYER_ORDER`** → invariant #9 violation
   (forgotten source stays visible in wiki) → HARD STOP.
8. **`AUTHORIZED_SCOPE.md` or `ROADMAP.md` changes during the cycle removing Phase 9
   authorization** → escalate to human → STOP all sprints.
9. **Any implementation adds Phase 10 graph infrastructure** (even "just a FK to a future
   `graph_nodes` table") → HARD STOP.

---

## §9. PR-Required Checks

Each PR before merge:

1. **Migration pre-flight:** `alembic upgrade head` AND `alembic downgrade -1` in CI.
2. **Alembic single-head guard (MEDIUM G):**
   ```bash
   if [ $(alembic heads 2>/dev/null | wc -l) -ne 1 ]; then
     echo "ERROR: multiple alembic heads — rebase Phase 9 migrations before merge"
     exit 1
   fi
   ```
   This guard MUST be wired as a concrete CI step in `.github/workflows/ci.yml`:
   ```yaml
   - name: Alembic single-head check
     run: |
       heads=$(alembic heads | wc -l)
       if [ "$heads" -ne 1 ]; then
         echo "ERROR: $heads alembic heads, expected 1"
         alembic heads
         exit 1
       fi
   ```
   Risk: if Orch A ships migration 039 in parallel while Phase 9 is in flight, this check
   will fire. Phase 9 implementer MUST rebase migrations 050–052 on top of 039 before merge.
3. **Ruff clean:** `ruff check .` green.
4. **Type check:** `mypy bot/services/wiki_*.py web/routes/wiki.py bot/handlers/wiki.py`
   (add to existing mypy config).
5. **Pytest:** `timeout 120 pytest -x tests/services/test_wiki_*.py tests/handlers/test_wiki_*.py tests/web/test_wiki_*.py` green.
6. **Privacy lint:** `scripts/lint_privacy_check.sh` passes (no new path added without
   allowlist update).
7. **No graph imports:** `grep -r "graph_\|neo4j\|graphiti\|networkx" bot/services/wiki_*.py web/routes/wiki.py bot/handlers/wiki.py` returns empty.
8. **PAR review:** Claude product reviewer + Codex technical reviewer. Both must pass.
9. **Phase 11 binding (T9-08 only):** `EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x tests/evals/test_wiki_leakage.py tests/evals/test_wiki_citations.py tests/evals/test_wiki_cascade.py tests/evals/test_wiki_refusal.py tests/evals/test_wiki_no_graph.py` green.

**Shared file serialization:** `bot/services/forget_cascade.py` and `bot/db/models.py`
are shared files per `ORCHESTRATOR_REGISTRY.md §2`. T9-07 MUST merge `forget_cascade.py`
changes via its own PR before any other PR that reads the `CASCADE_LAYER_ORDER` constant.
If Orch A has an open PR touching `forget_cascade.py` at T9-07 kickoff → STOP and
coordinate via REGISTRY §3.4.

---

## §10. Phase 11 Binding Tests

Phase 9 adds **18 new test IDs** to the Phase 11 eval suite. Baseline after Phase 8:
**42/42**. After Phase 9: **60/60**.

<!-- HIGH H fix: test table expanded from 11 to 18 IDs; totals corrected from 53 to 60 -->

| Test ID | File | Description |
|---------|------|-------------|
| **L9a** | `tests/evals/test_wiki_leakage.py` | `#offrecord`-tagged source MV does NOT surface in `/wiki/{slug}` body or `/wiki/search` results |
| **L9b** | `tests/evals/test_wiki_leakage.py` | Forgotten card source triggers `page_status='stale'`/`'archived'` or 410 Gone on render |
| **L9c** | `tests/evals/test_wiki_leakage.py` | Transitive forget — wiki page cites approved card whose `card_sources` includes a forgotten/offrecord mvid; page must mask/stale |
| **L9d** | `tests/evals/test_wiki_leakage.py` | `message_hash:` tombstone forget invalidates wiki pages citing that mvid (not only `message:` key) |
| **L9e** | `tests/evals/test_wiki_leakage.py` | `user:` tombstone forget invalidates wiki pages citing messages from that user |
| **C8a** | `tests/evals/test_wiki_citations.py` | Every `[^mv:...]` token resolves to an existing, non-redacted, non-forgotten `message_versions.id`; invalid tokens suppressed from member output |
| **C8b** | `tests/evals/test_wiki_citations.py` | Revision citations (`source_message_version_ids_snapshot`) also valid at render time — forgotten content does not leak via `wiki_revisions.body_markdown` |
| **I7a** | `tests/evals/test_wiki_cascade.py` | Forget event on cited `mv_id` triggers wiki_page re-evaluation (`page_status='stale'`/`'archived'` or content masked) |
| **I7b** | `tests/evals/test_wiki_cascade.py` | Forget event on a `card_source` triggers same path as I7a |
| **I7c** | `tests/evals/test_wiki_cascade.py` | Cascade order: `wiki_pages` AFTER `digests`, `wiki_revisions` AFTER `wiki_pages`, both BEFORE `card_sources` (asserted on `CASCADE_LAYER_ORDER` index values) |
| **I7d** | `tests/evals/test_wiki_cascade.py` | Legacy cookie without `role` field — first request admin-window-grace, second request after refresh has explicit `role` claim; WARN logged |
| **I7e** | `tests/evals/test_wiki_cascade.py` | Revision body redaction — `_cascade_wiki_revisions` masks `body_markdown` for forgotten content; `revision_sources_resolved_at` updated |
| **R6.a** | `tests/evals/test_wiki_refusal.py` | Non-admin cannot `/wiki_publish` (refusal message, no DB change) |
| **R6.b** | `tests/evals/test_wiki_refusal.py` | Admin cannot publish page with `page_status != 'reviewed'` (refusal, no DB change) |
| **R6.c** | `tests/evals/test_wiki_refusal.py` | Admin cannot publish page with failed source trace (offrecord source → governance failure → refusal) |
| **R6.d** | `tests/evals/test_wiki_refusal.py` | `robots_policy='index'` cannot be set when `public_enabled=false` (DB constraint + handler both enforced) |
| **R6.e** | `tests/evals/test_wiki_refusal.py` | Member cannot escalate to admin by self-claiming admin user_id — two-password model makes this structurally impossible (login with `WEB_MEMBER_PASSWORD` always returns `role='member'`) |
| **R6.f** | `tests/evals/test_wiki_refusal.py` | Unpublish + immediate forget — `GET /wiki/public/{slug}` returns 410 or 404 within next request; `Cache-Control: no-store` verified in response |
| **G1** | `tests/evals/test_wiki_no_graph.py` | AST/import lint: zero imports of `graph_*`, `neo4j`, `graphiti`, `networkx` in all wiki modules |

New ID breakdown: L9a/b/c/d/e (5) + C8a/b (2) + I7a/b/c/d/e (5) + R6.a/b/c/d/e/f (6) + G1 (1)
= **19 table rows** but G1 is 1 test, totaling **18 new test parametrizations**. Corrected:
L9 (5) + C8 (2) + I7 (5) + R6 (6) = 18, plus G1 (1 scan covering multiple modules).

Total after Phase 9: **42 + 18 = 60/60**.

**Run command (added to CI / `evals.yml` after T9-08 merges):**
```bash
EVAL_HARNESS_ENABLED=1 timeout 300 pytest -x --timeout=60 \
  tests/evals/test_leakage.py \
  tests/evals/test_citations.py \
  tests/evals/test_refusal.py \
  tests/evals/test_no_llm_imports.py \
  tests/evals/test_digest_leakage.py \
  tests/evals/test_wiki_leakage.py \
  tests/evals/test_wiki_citations.py \
  tests/evals/test_wiki_cascade.py \
  tests/evals/test_wiki_refusal.py \
  tests/evals/test_wiki_no_graph.py
```

---

## §11. Glossary

- **Member wiki:** authenticated internal/member web catalog for reviewed source-backed
  memory pages. Default surface for Phase 9.
- **Wiki page:** a `wiki_pages` row with Markdown body and explicit source refs. Editable
  by admin via Telegram commands; versioned via `wiki_revisions`.
- **Wiki revision:** a `wiki_revisions` row capturing the full body and source refs at a
  specific edit point. Audit trail only — the live body is always on `wiki_pages.body_markdown`.
- **Source trace:** the set of `message_version_id` and/or `card_id` refs proving a page's
  claims. Required for `page_status='reviewed'` and for publication.
- **Public candidate:** a reviewed page whose `visibility='public_candidate'` but
  `public_enabled` is still false. Eligible for `/wiki_publish`.
- **Publication log:** append-only audit trail in `wiki_publication_log` for
  publish/unpublish/robots-index/robots-noindex events. One row per event.
- **Robots policy:** per-page search-engine indexing control. Default `noindex`. Can become
  `index` only after `/wiki_publish` + `/wiki_robots <slug> index`.
- **Renderer:** `bot/services/wiki_renderer.py` — pure server-side Markdown → sanitized
  HTML converter. Zero LLM calls.
- **Governance validator:** `bot/services/wiki_governance.py` — rejects forbidden, offrecord,
  nomem, redacted, missing, or unapproved sources before any render or publication step.
- **Digest archive section:** `/wiki/digests` index listing existing Phase 7/8 `digests`
  rows by `digest_id`. Not first-class `wiki_pages` rows — digests have their own
  redaction layer.
- **G1 boundary:** the hard contract that no Phase 9 wiki module imports graph
  infrastructure. Phase 10 only.
- **Stale page:** a `wiki_pages` row where `page_status='stale'` — automatic transition
  forced by forget cascade or card status drift. `public_enabled` forced to `false`
  atomically on transition. Recoverable by admin re-review → `page_status='reviewed'`.
- **Stale-page reconciler:** daily job in `wiki_publication.py::reconcile_stale_pages`
  that scans `reviewed` pages for cards that became non-`approved` and transitions them to
  `stale` (handles async card-status drift; complements the sync cascade path).
- **Two-password model:** auth design where role is derived from WHICH password matched
  (`WEB_ADMIN_PASSWORD` → admin, `WEB_MEMBER_PASSWORD` → member); no user_id self-claim
  in the login form. Prevents privilege escalation by member knowing the shared password.
- **Transitive source:** a `message_version_id` reached via `wiki_page_card_sources` →
  `card_sources.message_version_id` chain. Governance validator checks transitive sources
  with the same tombstone predicate as direct sources (L9c binding test).

---

## §12. Rollout Plan Reference

Production rollout playbook will be written as `docs/memory-system/PHASE9_ROLLOUT.md`
at Phase 9 closure (T9-08 PR). Placeholder guidance:

1. `memory.wiki.enabled=false` throughout development — no member exposure until flag flip.
2. Flag flip is a two-step: (a) set `memory.wiki.enabled=true` in Coolify env; (b) verify
   `/wiki` returns member index with zero leaked content in staging.
3. Public routes (`/wiki/public/*`) remain 404-by-code until the first `/wiki_publish` is
   executed by admin. No config change needed.
4. Rollback: set `memory.wiki.enabled=false`; all wiki routes return 503 immediately.
5. Downgrade migration path: `wiki_publication_log` → `wiki_revisions` → `wiki_pages`
   (CASCADE DELETE handles FK cleanup). Pre-flight: fail if any `public_enabled=true` rows
   exist (operator must `/wiki_unpublish` before downgrading).

---

## §13. Open Questions Answered (Q1–Q7 + Gaps)

| Q# | Question | Decision | Rationale |
|----|----------|----------|-----------|
| Q1 | Wiki backing store | **Separate `wiki_pages` table** | Page-level governance, drift detection, robots policy persistence require stable rows. Render-from-cards-on-the-fly cannot persist `page_status` or publication audit. |
| Q2 | Markdown vs HTML storage | **Store raw Markdown in `wiki_pages.body_markdown`; render server-side at request time** | Forget cascade can mask tokens in Markdown and invalidate derived HTML. Pre-stored HTML would require a separate invalidation layer. |
| Q3 | Versioning | **Ship `wiki_revisions` table** | Mirrors `message_versions` discipline. Edit audit trail required for governance. |
| Q4 | Multilingual | **OUT OF SCOPE Phase 9** | Single locale, primarily Russian content. Phase 9.5 candidate. |
| Q5 | Static export | **OUT OF SCOPE Phase 9** | Phase 9.5 candidate. |
| Q6 | Digest archive | **Separate `/wiki/digests/` index section** linking by `digest_id` to existing Phase 7/8 records | Digests already have their own redaction layer; making them first-class wiki pages would duplicate governance complexity. |
| Q7 | Publication quorum | **Single-admin + audit via `wiki_publication_log`** | Matches Phase 6 single-admin approval pattern. Quorum upgrade is Phase 9.5 candidate iff leak detected. |
| **visibility_scope GAP** | `knowledge_cards` has no `visibility_scope` column | **Option 1 (inherit from messages):** validator derives visibility from `chat_messages.memory_policy + is_redacted` of every cited `message_version_id`. No Phase 6 schema change. |Strictest safe interpretation of invariant #3. No Phase 9 migration on `knowledge_cards`. |
| **Web role model** | How to gate member vs admin | **Two-password model:** `WEB_ADMIN_PASSWORD` → admin, `WEB_MEMBER_PASSWORD` → member; role derived from password match, no user_id self-claim (BLOCKER C fix) | Eliminates privilege escalation: member cannot claim admin role by knowing shared password and supplying admin user_id. |
| **Migration window** | Which alembic numbers | **050, 051, 052** | Honors `ORCHESTRATOR_REGISTRY.md §2` Orch B exclusive window (050–069). |
| **Path layout** | `web/routers/` vs `web/routes/` | **`web/routes/wiki.py`** | Follows existing repo convention (draft discrepancy resolved). |
| **Feature flag** | Flag name | **`memory.wiki.enabled`** (default OFF) | Consistent with Phase 7 `memory.digests.daily.enabled` and Phase 8 `memory.digests.weekly.enabled` naming. |

---

## §13.5. Codex Audit Revision Log (2026-05-16)

Applied following independent technical audit by Codex (`codex_p9_audit.md`).
Verdict was BLOCK in original form; this revision resolves all BLOCKERs and HIGHs.

| Audit Ref | Severity | Change Applied |
|-----------|----------|----------------|
| **BLOCKER A** | BLOCKER | §5.A visibility view: corrected join chain to `message_versions.chat_message_id → chat_messages.id` (NOT `message_versions.message_id`). Added all 3 tombstone key shapes explicitly. Added transitive source resolution via `card_sources`. SQL definition replaced in §5.A. |
| **BLOCKER C** | BLOCKER | §5.D auth: struck user_id self-claim design. Replaced with two-password model (`WEB_ADMIN_PASSWORD` / `WEB_MEMBER_PASSWORD`); role derived from password match only. Added legacy cookie migration (I7d). T9-03 AC rewritten. |
| **HIGH B** | HIGH | §5.F cascade order: added `wiki_revisions` layer between `wiki_pages` and `card_sources`. Added `/wiki_publish` advisory lock requirement covering direct + transitive mvids, plus in-transaction `validate_sources` re-check. T9-07 AC updated. |
| **HIGH F** | HIGH | §5.G public surface: added mandatory `Cache-Control: no-store, max-age=0, must-revalidate` header on `/wiki/public/{slug}` and `/robots.txt`. Added R6.f binding test. T9-05 AC updated. |
| **HIGH I** | HIGH | §5.A schema: replaced `source_card_ids JSONB` and `source_message_version_ids JSONB` on `wiki_pages` with FK-normalized `wiki_page_card_sources` and `wiki_page_message_sources` junction tables. `wiki_revisions` retains JSONB snapshot as immutable audit trail. T9-01 AC updated. §3 scope updated. |
| **HIGH J** | HIGH | §5.A + §5.H (new): added `page_status='stale'` to state machine. Added `ck_wiki_pages_stale_not_public` constraint. Added §5.H stale-page reconciler. Added `revision_sources_resolved_at` to `wiki_revisions`. Added `invalidated_by_forget_event_id` + `last_validated_at` + `validation_status` to `wiki_pages`. T9-02 + T9-07 AC updated. |
| **HIGH H** | HIGH | §10: expanded Phase 11 binding tests from 11 to 18 new IDs. Added L9c (transitive forget), L9d (message_hash tombstone), L9e (user tombstone), C8a (split from C8), C8b (revision citations), I7d (legacy cookie), I7e (revision body redaction), R6.e (member escalation impossible), R6.f (cache-control + unpublish). Total updated from 53 to 60. T9-08 AC updated. |
| **MEDIUM D** | MEDIUM | §5.B renderer: added render batching requirement (single SQL per page for all direct + transitive mvids). Added `last_validated_at`, `validation_status`, `invalidated_at`, `invalidated_by_forget_event_id` columns to §5.A `wiki_pages`. |
| **MEDIUM E** | MEDIUM | §5.A schema: added `body_tsv TSVECTOR GENERATED ALWAYS AS (...)` GIN-indexed column to `wiki_pages`. Added wiki-only FTS note in §5.B (wiki search SEPARATE from `/recall` canonical evidence). |
| **MEDIUM G** | MEDIUM | §9 PR-Required Checks: added alembic single-head guard as check #2 (explicit bash script + concrete `.github/workflows/ci.yml` step, REVISION 2). Risk note added for parallel Orch A migration. |

---

## §13.6. Dual-Model Spec Review Revision Log (REVISION 2, 2026-05-16)

Applied following consolidated dual-model spec review (Claude product + Codex technical, Round 1).
Both reviewers returned NEEDS_FIXES / REQUEST_CHANGES. Orchestrator decisions D1–D5 locked.

| Fix | Reviewer | Severity | Change Applied |
|-----|----------|----------|----------------|
| **FIX 1** | Codex FAIL-2 + Claude #1 | CRITICAL | T9-01 AC: removed no-op JSONB constraint ref; replaced with `wiki_governance.assert_publishable(page)` → `WikiSourcesMissingError` when both FK junction tables have zero rows. `/wiki_publish` step 4 uses FK tables not JSONB. |
| **FIX 2** | Codex FAIL-1 (T9-03) | CRITICAL | Verified T9-03 ACs correct for two-password model. Added `wiki_publication_log(action='legacy_cookie_grace')` audit row to I7d AC. `legacy_cookie_grace` added to migration 052 CHECK constraint. |
| **FIX 3** | Codex FAIL-3 + Claude #2 | CRITICAL | Migration 051: added `revision_status`, `redacted_at`, `redacted_by_forget_event_id` columns (D2). §5.H: exact placeholder `[CONTENT_REDACTED: forget_event_id={n}]`. Full `_cascade_wiki_revisions` spec added to §5.F. T9-07 ACs: I7e sub-AC A+B. §3 scope updated with new columns. |
| **FIX 4** | Codex FAIL-4 + Claude #3 | CRITICAL | Confirmed canonical count 18 throughout: §0 status table (18 new / 60 total), §10 (42+18=60/60), T9-08 ("all 18 new IDs", file ownership listed). No residual "13" or "53" mismatches. |
| **FIX 5** | Claude #4 | CRITICAL | T9-05 AC: added L9c transitive offrecord bullet-suppression AC (member sees no `[⚠]`). T9-05 scenario count 13→16. |
| **FIX 6** | Codex H1 | HIGH | §5.E step 7: `prior_public_enabled` reads actual FOR UPDATE locked value (not hardcoded `false`). §5.F pseudocode captures `prior_public_enabled`/`prior_robots_policy` before mutation. |
| **FIX 7** | Codex H2 | HIGH | T9-05 AC: SQL injection AC — `plainto_tsquery('russian', :q)` bound param; SQL metacharacters return empty result without error. |
| **FIX 8** | Codex H3 | HIGH | T9-07 AC: scale AC — N=50 pages sharing same mvid, single advisory-lock-guarded tx, count=50. T9-07 scenario count 14→16. |
| **FIX 9** | Claude #5 / D4 | HIGH | T9-08: scope note added — 1.5 sprint slots; parallel dispatch of 5 sub-implementers recommended. |
| **FIX 10** | Claude #7 | HIGH | §4.1 added: Phase 10 read-surface contract (wiki-readable state filter, `graph_provenance` cascade extension point, informational ORDER). |
| **FIX 11** | Codex Medium | MEDIUM | §9: concrete `.github/workflows/ci.yml` YAML step added for alembic single-head check. §13.5 MEDIUM G row updated. |
| **FIX 12** | Claude #11 | MEDIUM | §15 added: Phase 9.5 Carryover Candidates (11 items). |
| **FIX 13** | Claude Q3 / D3 | MEDIUM | §5.C governance validator: `# TODO(#291): refactor to shared helper` comment at inline predicate per Phase 7 pattern. |
| **FIX 14** | Claude #6 / D5 | MEDIUM | §4 non-goals: concurrent admin edits last-writer-wins by `revision_seq`; edit-conflict deferred to Phase 9.5 (D5). |

---

## §15. Phase 9.5 Carryover Candidates

The following items are explicitly deferred from Phase 9 scope. Not authorized for Phase 9
implementation. Tracked for Phase 9.5 planning.

1. **Multilingual support (Russian + English)** — Phase 9.5 candidate (Q4 decision).
2. **Static export option** — Phase 9.5 candidate (Q5 decision).
3. **Two-admin publication quorum upgrade** — Phase 9.5 candidate (Q7; upgrade only if leak detected in production).
4. **Web create/edit UI** — Replace Telegram-command-only editing with HTMX/form-based interface (`/wiki_admin/edit/{slug}` route family).
5. **`card_revisions` infrastructure** — Deferred from Phase 6.5; requires Phase 9.5 design ratification.
6. **Edit-conflict resolution via `expected_revision_seq`** — Two simultaneous admin edits are currently last-writer-wins (D5). MVCC guard deferred.
7. **Page tagging / categories** — Cross-cutting search and navigation improvement.
8. **Content moderation flow (offensive-but-not-offrecord)** — Pages citing content that is not technically `#offrecord` but is sensitive or offensive.
9. **Member-account-compromise runbook** — Operational guide for `WEB_MEMBER_PASSWORD` leakage scenario.
10. **`#291` shared `_forget_excludes_predicate` refactor** — Currently inline-duplicated per D3 in `forget_cascade.py`, `digest_context.py`, and Phase 9 wiki governance (each with `# TODO(#291)` comment). Extract to shared helper in Phase 9.5 or next architectural sprint.
11. **Legacy cookie audit row enrichment** — `wiki_publication_log(action='legacy_cookie_grace')` rows have null `wiki_page_id` (no specific page context). Phase 9.5 may enrich with session metadata or move to dedicated audit table.
