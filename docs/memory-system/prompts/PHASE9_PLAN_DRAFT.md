🚧 DRAFT — NOT AUTHORIZED

# Phase 9 — Member Wiki / Community Catalog: Design & Stream Plan

**Status:** **Draft only** — not authorized for implementation.
**Cycle:** Memory system Phase 9
**Date:** 2026-04-30
**Predecessor:** Phase 8 must be closed before Phase 9 begins.
**Migration window:** **050+** per `ORCHESTRATOR_REGISTRY.md §2 Orchestrator B exclusive write` (corrected from original draft's "040+" by Orch B sprint-0b refinement 2026-05-02; 040–049 belong to Orch A's Phase 5/6/7/8 chain).
**Critical invariant for this phase:** Public wiki remains disabled until review/source trace/governance are proven.

---

## §0. TBD / Metadata

TBD — not started.

**Authorization status:** NOT AUTHORIZED. `AUTHORIZED_SCOPE.md` explicitly lists Wiki (member or public) as future Phase 9 scope and public surfaces as Phase 9 with explicit approval.

**Source status:**

- `docs/memory-system/HANDOFF.md` read from current checkout; Phase 9, Phase 10, invariants, feature flags, governance and cascade sections used.
- `docs/memory-system/AUTHORIZED_SCOPE.md` read from current checkout; Phase 9 wiki and public surfaces are not authorized.
- `docs/memory-system/ROADMAP.md` read from current checkout; Wiki gate and invariant #10 used.
- `docs/memory-system/PHASE4_PLAN.md` was not present at the requested path in current checkout; structural template read from `.worktrees/p4-stream-e/docs/memory-system/PHASE4_PLAN.md` and cross-checked against `origin/docs/p4-codex-prompts:docs/memory-system/PHASE4_PLAN.md`.
- `docs/memory-system/prompts/PHASE6_PLAN_DRAFT.md` was not present at the requested path in current checkout; source read from `origin/docs/p4-codex-prompts:docs/memory-system/prompts/PHASE6_PLAN_DRAFT.md`.
- `ls -R web/` read from current checkout. Existing layout uses `web/routes/`, `web/templates/`, and `web/static/`; this Phase 9 design follows the requested `web/routers/wiki.py` path, and implementation must reconcile that with the existing route package before code starts.

**Existing web layout baseline from `ls -R web/`:**

- `web/app.py`, `web/auth.py`, `web/config.py`, `web/__main__.py`
- `web/routes/auth.py`, `web/routes/dashboard.py`, `web/routes/health.py`, `web/routes/members.py`
- `web/templates/base.html`, `dashboard.html`, `login.html`, `members.html`
- `web/static/style.css`

**Grounding:** HANDOFF §1 says catalog must precede digest/wiki and digest/wiki must reference approved cards/sources. HANDOFF §2 Phase 9 defines the wiki/community catalog scope. ROADMAP phase 9 says public stays disabled. AUTHORIZED_SCOPE marks wiki and public surfaces as not authorized future scope.

---

## §0a. Refinement Status (Orchestrator B sprint-0b, 2026-05-02)

**RATIFIED PENDING PHASE 6 CLOSURE** — design contract approved by Orchestrator B
sprint-0b on 2026-05-02; **implementation deferred** until ALL of the following:

1. Orchestrator A confirms Phase 6 (knowledge cards + admin review) CLOSED — every
   T6-* ticket merged, IMPLEMENTATION_STATUS.md reflects, post-merge `pytest -x` on
   fresh main green, FHR reviewers ACCEPTED. (Per `ORCHESTRATOR_REGISTRY.md §5`,
   Phase 6 closure is the cross-orch dependency that gates Phase 9 wiki content.)
2. `AUTHORIZED_SCOPE.md` is updated by human or by Orchestrator A's Phase 6 closing
   PR to add a "Phase 9 — wiki" authorization block (currently §"Conditionally
   authorized: Phase 9, Phase 10 (gated)" lists three pre-conditions; this status
   block satisfies the design refinement pre-condition).
3. This draft is promoted from `prompts/PHASE9_PLAN_DRAFT.md` to canonical
   `docs/memory-system/PHASE9_PLAN.md` per `ORCHESTRATOR_REGISTRY.md §2 Orchestrator
   B exclusive write` (mirrors the Phase 12 promotion pattern from sprint-0a /
   PR #171).

### Refinement deltas applied 2026-05-02

- Migration window in front-matter corrected: original "040+" → **050+** to match
  `ORCHESTRATOR_REGISTRY.md §2` (Orch A owns 022–049, Orch B owns 050–069).
- Web layout discrepancy explicitly logged: spec uses `web/routers/wiki.py`; the
  existing repo layout uses `web/routes/`. Implementation-time decision; do NOT
  rewrite the spec ahead of Phase 6 closure since the route module path is a
  trivial rename. **Resolution:** Wave 1 implementer reconciles this in their first
  commit (rename `routers` → `routes` if going with existing layout, or vice versa).
  The §5.A spec carries this as a known discrepancy.
- Phase 11 numbering conflict — **resolved upstream by Orchestrator C** in commit
  `8e1e716` (`docs(p11): ratify Phase 11 evals plan + reconcile draft numbering
  conflict`). Phase 11 = Shkoderbench/evals (canonical); the "expertise pages"
  draft was deferred. No Phase 9 spec changes required for that reconciliation.

### Phase 6 dependency contract (what cards must expose for wiki)

When Phase 6 closes, the wiki render pipeline (T9-04 / T9-05 in §7) consumes these
fields from the cards layer. This list is the binding contract Orchestrator B will
hold Orchestrator A to at the Phase 6 closure handoff:

| Field needed by wiki | Source (Phase 6 owned) | Why wiki needs it |
|---------------------|------------------------|-------------------|
| `card.id` (BIGINT) | `knowledge_cards.id` | Page anchor, page-source FK |
| `card.title` (TEXT) | `knowledge_cards.title` | Page heading + sidebar link text |
| `card.body` (TEXT) | `knowledge_cards.body` | Page body section |
| `card.visibility_scope` ENUM(`member`, `admin`) | `knowledge_cards.visibility_scope` | Phase 9 first-gate filter — invariant #3 / #10 binding |
| `card.status` ENUM (`approved`, `pending`, `rejected`) | `knowledge_cards.status` | Wiki MUST render only `approved`; never `pending` |
| `card.source_ids[]` → `card_sources.id[]` | `card_sources` table | Citation rendering — citations point to `message_version_id` per invariant #4 |
| `card.updated_at` (TIMESTAMPTZ) | `knowledge_cards.updated_at` | Page freshness sort + last-updated stamp |
| `card.created_by_tg_id` (BIGINT) | `knowledge_cards.created_by_tg_id` | Page author attribution |

If Phase 6 ships with field renames or omissions, Orch B re-opens this draft and
adjusts §5 / §7 before promoting to canonical.

### Implementation gate explicitly deferred

Phase 9 implementation tickets in §6 / §7 (T9-01 through T9-08) MUST NOT be picked
up until the three pre-conditions above are satisfied. The §6 / §7 / §8 substance
remains the binding design contract. Orchestrator B will not open implementation
PRs on this draft until ratified via promotion to canonical path.

---

## §1. Invariants Verbatim

Verbatim from HANDOFF.md §1:

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

**Phase 9 binding:** invariant #10 is active for every ticket in this plan. A public toggle without a governed `/wiki_publish` approval path is a hard stop.

---

## §2. Phase 9 Spec from HANDOFF

### Verbatim summary

- **Objective:** internal / member browsable catalog.
- **Scope:** member / internal pages, digest archive, source citations, visibility filters.
- **Dependencies:** phases 3, 6, 8.
- **Acceptance:** member catalog shows only approved visible cards. **Public wiki disabled by default.**

### Interpretation

Phase 9 turns the reviewed memory system into a member-only web wiki. The default product surface is authenticated, internal/member access only. Its source of truth is not raw chat history and not summaries; wiki pages render from approved visible cards, digest archive entries, and explicit source traces that resolve back to `message_version_id` or approved card sources.

Architectural decisions:

- Wiki content must be derived from approved cards or approved wiki pages with source trace, because HANDOFF §1 says catalog must precede digest/wiki and HANDOFF §2 Phase 6 says cards cannot become active without source and visibility enforcement.
- Wiki reads must apply governance filters before rendering, because HANDOFF §1 invariant #3 forbids downstream memory surfaces over `#nomem`, `#offrecord`, or forgotten content, and HANDOFF §10 says catalog/wiki/graph must not consume forbidden content.
- Member-only access is the first gate in `web/routers/wiki.py`, because HANDOFF §2 Phase 9 scopes "member / internal pages" and ROADMAP phase 9 requires visibility + governance + source trace before the wiki gate is passed.
- Public publication is not a normal edit flag. It is a separate admin-approved event through `/wiki_publish`, because HANDOFF §1 invariant #10 and ROADMAP invariant #10 keep public wiki disabled until review/source trace/governance are proven.
- Per-page `robots.txt` control is part of the public governance envelope, because HANDOFF §16 names public wiki leak as a critical Phase 9 risk and ROADMAP phase 9 keeps public disabled by default.

---

## §3. Phase 10 Boundary

Phase 9 MUST NOT implement graph behavior.

- **No graph.**
- **No knowledge graph.**
- **No Neo4j / Graphiti sidecar.**
- **No `graph_sync_runs`.**
- **No graph traversal, graph scoring, graph-derived related pages, or relation expansion beyond card/source links that already exist in Postgres.**

That is Phase 10 territory. HANDOFF §2 Phase 10 defines Graphiti / Neo4j projection as a derived graph traversal layer whose dependencies are stable cards/events/relations. HANDOFF §1 invariant #6 says graph is never source of truth. ROADMAP phase 10 says graph projection is derived only, rebuildable from Postgres, and forget purges graph.

If wiki implementation wants "related pages", it may use explicit approved card relations already created in Phase 6, but it must not create graph infrastructure or graph-derived inference in Phase 9.

---

## §4. Architecture ASCII Diagram

```
                         ┌────────────────────────────────────────────┐
                         │              Web request /wiki             │
                         │        FastAPI router web/routers/wiki.py  │
                         └──────────────────────┬─────────────────────┘
                                                │
                                                ▼
                         ┌────────────────────────────────────────────┐
                         │ Member-only gate FIRST                    │
                         │ - existing web session/auth                │
                         │ - user must be member or admin             │
                         └──────────────────────┬─────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│ wiki_pages                                                                         │
│ - source card/page refs                                                            │
│ - visibility='member' by default                                                   │
│ - public_enabled=false by default                                                  │
│ - robots_policy per page                                                           │
└──────────────────────────────────────┬─────────────────────────────────────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Governance/source validator                                                        │
│ - only approved visible cards                                                      │
│ - source trace to message_version_id or approved card sources                      │
│ - exclude nomem/offrecord/forgotten/redacted/tombstoned sources                    │
└──────────────────────────────────────┬─────────────────────────────────────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Server-side Markdown -> HTML renderer                                              │
│ - safe allowlist HTML                                                              │
│ - citation linkification to source chat_messages / message_versions               │
│ - no client-side raw Markdown rendering                                            │
└──────────────────────────────────────┬─────────────────────────────────────────────┘
                                       │
                                       ▼
                         ┌────────────────────────────────────────────┐
                         │ web/templates/wiki/                       │
                         │ - index.html                              │
                         │ - page.html                               │
                         │ - search.html                             │
                         └────────────────────────────────────────────┘

Admin publication path:

Telegram admin /wiki_publish <page_slug>
        │
        ▼
wiki_publication_log
- admin actor
- pre-publish source/governance check result
- public_enabled transition
- robots_policy transition
- immutable audit trail
```

Architecture decisions:

- `web/routers/wiki.py` is the planned router named by this Phase 9 task; before implementation, reconcile with the current `web/routes/` package discovered by `ls -R web/`. The grounding requirement for the surface remains HANDOFF §2 Phase 9 member/internal pages and ROADMAP phase 9 visibility + review + source trace.
- The member-only gate runs before page lookup, search, rendering, or public checks. This follows HANDOFF §2 Phase 9 member/internal scope and HANDOFF §16 public wiki leak risk.
- `public_enabled` is per page and defaults false. This follows ROADMAP phase 9 "public stays disabled" and HANDOFF §1 invariant #10.
- Publication writes an append-only audit event. This follows HANDOFF §10 durable tombstone/audit posture and HANDOFF §15 sequential review requirement for visibility/public surfaces.
- Per-page robots policy is stored with publication state, not as a global afterthought. This follows the Phase 9 risk boundary in HANDOFF §16 and keeps public indexing controlled only after explicit approval.

---

## §5. Components

### 5.A. DB schema: `wiki_pages` + `wiki_publication_log`

**Files planned:** Alembic migration, `bot/db/models.py` or web-adjacent model module, repository tests.

**Schema:**

```sql
CREATE TABLE wiki_pages (
  id UUID PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  body_markdown TEXT NOT NULL,
  source_card_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_message_version_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  visibility TEXT NOT NULL DEFAULT 'member'
    CHECK (visibility IN ('member', 'admin', 'public_candidate')),
  public_enabled BOOLEAN NOT NULL DEFAULT false,
  robots_policy TEXT NOT NULL DEFAULT 'noindex'
    CHECK (robots_policy IN ('noindex', 'index')),
  page_status TEXT NOT NULL DEFAULT 'draft'
    CHECK (page_status IN ('draft', 'reviewed', 'archived')),
  created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  reviewed_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE wiki_publication_log (
  id UUID PRIMARY KEY,
  wiki_page_id UUID NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('publish', 'unpublish', 'robots_index', 'robots_noindex')),
  actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  prior_public_enabled BOOLEAN NOT NULL,
  new_public_enabled BOOLEAN NOT NULL,
  prior_robots_policy TEXT NOT NULL,
  new_robots_policy TEXT NOT NULL,
  source_check_result JSONB NOT NULL,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Rules:**

- `wiki_pages.public_enabled` defaults false and cannot be set true without a `wiki_publication_log` row from `/wiki_publish`.
- A reviewed/member page must have at least one `source_card_ids` entry or one `source_message_version_ids` entry.
- `public_enabled=true` requires `page_status='reviewed'`, `visibility='public_candidate'`, non-empty source trace, and a fresh governance validation result.
- `robots_policy='index'` is allowed only when `public_enabled=true`.
- Forgotten/offrecord/nomem source checks are evaluated at render time and at publication time.

**Grounding:** HANDOFF §2 Phase 9 requires source citations and visibility filters. HANDOFF §1 invariant #4 allows citations to `message_version_id` or approved card sources. HANDOFF §1 invariant #10 requires public wiki disabled until review/source trace/governance are proven. ROADMAP phase 9 says the wiki gate is visibility + review + source trace + forget purge.

**Acceptance:**

- Migrations apply and roll back cleanly on Postgres.
- `public_enabled=false` and `robots_policy='noindex'` are defaults.
- DB or repo layer rejects `robots_policy='index'` when `public_enabled=false`.
- Repo tests prove public publication cannot occur without a publication log entry and source validation payload.
- Tests prove a page with no sources cannot become reviewed/public.

---

### 5.B. FastAPI router `web/routers/wiki.py`

**Files planned:**

- `web/routers/wiki.py`
- app wiring in existing web app after reconciling with current `web/routes/` layout
- handler tests

**Endpoints:**

- `GET /wiki` — member-only index of reviewed member-visible pages.
- `GET /wiki/{slug}` — member-only page view.
- `GET /wiki/search?q=...` — member-only page search over titles/body/snippets.
- `GET /wiki/public/{slug}` — disabled unless `public_enabled=true`; still revalidates governance before rendering.
- `GET /robots.txt` or route-level robots response — emits page-aware policy only for public pages if the existing web stack supports this routing cleanly.

**Authorization:**

- Member/admin gate runs first for `/wiki`, `/wiki/{slug}`, and `/wiki/search`.
- Public route sees only `public_enabled=true` pages and still re-runs source/governance checks.
- Admin-only publication is not a web toggle in Phase 9; publication goes through Telegram `/wiki_publish` audit path.

**Grounding:** HANDOFF §2 Phase 9 scopes member/internal pages and visibility filters. HANDOFF §15 says visibility/public surfaces require sequential review. AUTHORIZED_SCOPE says wiki and public surfaces are future/not authorized until phase gate.

**Acceptance:**

- Anonymous user cannot access member wiki index/search/page.
- Non-member authenticated user cannot access member wiki pages.
- Member can access only reviewed member-visible pages.
- Search returns only pages whose sources pass governance checks.
- Public route returns 404/disabled for `public_enabled=false`.
- No endpoint returns offrecord/nomem/forgotten/redacted content.

---

### 5.C. Templates: `web/templates/wiki/`

**Files planned:**

- `web/templates/wiki/index.html`
- `web/templates/wiki/page.html`
- `web/templates/wiki/search.html`

**Template contract:**

- `index.html`: lists reviewed member-visible pages, titles, updated dates, and source counts.
- `page.html`: renders sanitized HTML body, citation list, source trace links, visibility badge, and public status for admins only.
- `search.html`: renders query results with snippets and source counts.

**Rules:**

- Templates receive sanitized HTML, not raw untrusted Markdown.
- Citation links are explicit and visible on every page.
- Public state is not editable from templates.
- No page shows raw Telegram JSON or unreviewed extraction output.

**Grounding:** HANDOFF §2 Phase 9 requires source citations and visibility filters. HANDOFF §1 invariant #5 says summary is never canonical truth, so templates must distinguish digest/archive material from source-backed wiki content. ROADMAP phase 9 requires source trace.

**Acceptance:**

- Templates exist under `web/templates/wiki/`.
- Page template renders citation/source trace section for every page.
- Admin-only metadata is hidden from normal members.
- Template tests verify raw Markdown/HTML injection is escaped or sanitized before display.

---

### 5.D. Server-side Markdown -> HTML renderer with citation linkification

**Files planned:** `web/services/wiki_render.py` or equivalent service module, renderer tests.

**Renderer input:**

- `body_markdown`
- `source_card_ids`
- `source_message_version_ids`
- request context for authz/source-link permissions

**Renderer output:**

- sanitized HTML body
- structured citation list
- broken-source warnings for admins
- member-safe links to source `chat_messages` / `message_versions`

**Rules:**

- Markdown is stored as raw Markdown in `wiki_pages.body_markdown`; HTML is rendered server-side at request time or cached only as rebuildable derived output.
- Citation tokens in Markdown use explicit source refs, for example `[^mv:<message_version_id>]` or `[^card:<card_id>]`.
- Linkification resolves `message_version_id` through current source/governance filters before emitting a source link.
- If a source is forgotten/offrecord/nomem/redacted after page approval, the renderer suppresses the affected citation/body block or marks the page unavailable until reviewed again.

**Grounding:** HANDOFF §1 invariant #4 defines citation anchors. HANDOFF §10 says catalog/wiki/graph must not consume forbidden content. ROADMAP gate for Wiki requires forget purge proven. HANDOFF §12 says derived rows/surfaces are hidden or disabled by feature flag and rebuildable where possible.

**Acceptance:**

- Renderer produces sanitized HTML only.
- Renderer links valid `message_version_id` citations to source chat/message references without exposing raw forbidden content.
- Renderer refuses or marks stale pages when any required source fails governance validation.
- Tests cover Markdown injection, missing source, forgotten source, offrecord source, and approved card source.

---

### 5.E. Public approval gate: `/wiki_publish` admin Telegram command with audit trail

**Files planned:** Telegram handler module, wiki publication service, audit tests.

**Command:**

- `/wiki_publish <slug> [reason]` — admin-only.
- `/wiki_unpublish <slug> [reason]` — admin-only rollback path for public exposure.
- Optional `/wiki_robots <slug> index|noindex [reason]` — admin-only robots policy change after publication.

**Publication flow:**

1. Verify actor is admin.
2. Load page by slug.
3. Require `page_status='reviewed'`.
4. Require non-empty source trace.
5. Re-run governance validation for all source cards and message versions.
6. Set `public_enabled=true` only if validation passes.
7. Keep `robots_policy='noindex'` unless admin explicitly changes it after publication.
8. Insert `wiki_publication_log` row with before/after state and validation payload.

**Grounding:** HANDOFF §1 invariant #10 binds public wiki. HANDOFF §15 requires sequential review for visibility/public surfaces. HANDOFF §16 identifies public wiki leak as critical and says public disabled by default. ROADMAP phase 9 requires public stays disabled.

**Acceptance:**

- Non-admin cannot publish.
- Admin cannot publish unreviewed page.
- Admin cannot publish page with missing or failed source trace.
- Publish writes exactly one audit log row in the same transaction as state transition.
- Unpublish writes audit log and immediately disables public route.
- Publication tests include a forgotten source after review; command must refuse.

---

## §6. Stream Allocation

### Wave 1 — independent foundations (PARALLEL)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **A** | Schema + repos | `wiki_pages`, `wiki_publication_log`, repo validation | Phase 8 closed; Phase 9 authorized |
| **B** | Renderer contract | server-side Markdown renderer, citation parser/linkifier, governance validation API | Phase 6 card/source schema exists |
| **C** | Template skeleton | `web/templates/wiki/index.html`, `page.html`, `search.html` using sanitized renderer output | Existing web templates baseline |

### Wave 2 — member web surface (SEQUENTIAL after A+B, parallel with C completion)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **D** | FastAPI router | `web/routers/wiki.py`, member gate, list/view/search/public-disabled routes | Streams A+B; reconcile `web/routes` vs `web/routers` |

### Wave 3 — publication governance (PARALLEL where safe)

| Stream | Owner | Scope | Deps |
|---|---|---|---|
| **E** | Telegram admin commands | `/wiki_publish`, `/wiki_unpublish`, `/wiki_robots`, audit trail | Stream A |
| **F** | Integration/eval closeout | member visibility, public disabled, forgotten-source purge, robots behavior | Streams B+D+E |

### Wave summary

```
Wave 1 (parallel):  A      B      C
                    │      │      │
                    └──┬───┘      │
                       ▼          │
Wave 2 (solo):         D ◄────────┘
                       │
                       ▼
Wave 3 (parallel):  E      F
                    │      │
                    └──┬───┘
                       ▼
                 Phase 9 review
```

**Grounding:** HANDOFF §3 dependency graph blocks wiki by governance + review + source trace. ROADMAP phase gate for Wiki requires visibility + review + source trace + forget purge proven. AUTHORIZED_SCOPE requires wiki to wait for its phase gate.

---

## §7. Tickets T9-XX

### T9-01: Wiki schema migrations

- **Scope:** Add `wiki_pages` and `wiki_publication_log`, ORM models, repo skeleton, DB constraints.
- **Acceptance criteria:**
  - Migrations apply and roll back cleanly.
  - `wiki_pages.public_enabled=false` and `robots_policy='noindex'` by default.
  - Reviewed/public candidate pages require non-empty source trace.
  - `wiki_publication_log` captures actor, action, before/after state, reason, and source validation payload.
  - Tests prove `robots_policy='index'` cannot be set when `public_enabled=false`.
- **Dependencies:** Phase 8 closed; Phase 9 added to `AUTHORIZED_SCOPE.md`; migration number reconciliation.
- **Stop signals:** Schema allows public pages without source trace; schema stores public indexed pages by default.

### T9-02: Wiki source/governance validator

- **Scope:** Service that validates page source refs against approved cards, `message_versions`, visibility, redaction, memory policy, and forget tombstones.
- **Acceptance criteria:**
  - Validator accepts approved visible card sources.
  - Validator accepts eligible normal `message_version_id` sources.
  - Validator rejects `#nomem`, `#offrecord`, forgotten, redacted, missing, or unapproved sources.
  - Validator returns structured payload suitable for `wiki_publication_log.source_check_result`.
  - Tests cover all governance policy states.
- **Dependencies:** T9-01; Phase 3 governance; Phase 6 card/source schema.
- **Stop signals:** Any forbidden source returns "valid"; validator requires graph/Neo4j lookup.

### T9-03: Server-side wiki renderer

- **Scope:** Markdown-to-HTML renderer, citation parser, source linkification, sanitized output.
- **Acceptance criteria:**
  - Renderer stores/accepts raw Markdown but emits sanitized HTML only.
  - Citation tokens resolve to source `message_version_id` or approved card sources.
  - Forgotten/offrecord/nomem source suppresses rendering or marks page stale for review.
  - Tests cover Markdown injection, missing citation, approved card citation, and forgotten-source purge.
- **Dependencies:** T9-02.
- **Stop signals:** Renderer emits raw unsanitized HTML; renderer links to forbidden source content.

### T9-04: Wiki templates

- **Scope:** Add `web/templates/wiki/index.html`, `page.html`, `search.html`.
- **Acceptance criteria:**
  - Index lists reviewed member-visible pages only.
  - Page template shows sanitized body and citation/source trace section.
  - Search template shows snippets and source counts only for eligible pages.
  - Admin-only metadata is hidden from members.
  - No create/edit/public toggle UI appears in templates.
- **Dependencies:** T9-03; current web template layout.
- **Stop signals:** Template exposes public toggle; template displays raw Markdown/HTML.

### T9-05: Member wiki router

- **Scope:** `web/routers/wiki.py`, app wiring, member gate, list/view/search endpoints, disabled public route behavior.
- **Acceptance criteria:**
  - Member/admin auth runs before page lookup.
  - Anonymous and non-member users cannot access member wiki.
  - Members can view only reviewed visible pages.
  - Search returns only governance-valid pages.
  - Public route is disabled for `public_enabled=false`.
  - Tests cover authz, page visibility, search filtering, and public disabled default.
- **Dependencies:** T9-01, T9-02, T9-03, T9-04; reconcile existing `web/routes` layout.
- **Stop signals:** Route returns wiki content before authz; route bypasses source/governance validator.

### T9-06: `/wiki_publish` public approval command

- **Scope:** Admin Telegram command and publication service for `/wiki_publish <slug> [reason]`.
- **Acceptance criteria:**
  - Command is admin-only.
  - Command refuses missing, unreviewed, source-less, or governance-invalid pages.
  - Command sets `public_enabled=true` only in the same transaction as `wiki_publication_log` insert.
  - Command leaves `robots_policy='noindex'` unless explicitly changed by separate admin action.
  - Tests cover non-admin, unreviewed page, forgotten source, successful publish, and audit row.
- **Dependencies:** T9-01, T9-02.
- **Stop signals:** Any public toggle exists outside `/wiki_publish`; publish succeeds without audit row.

### T9-07: `/wiki_unpublish` and per-page robots control

- **Scope:** Admin `/wiki_unpublish <slug> [reason]` and `/wiki_robots <slug> index|noindex [reason]`.
- **Acceptance criteria:**
  - Unpublish is admin-only and writes audit log.
  - Unpublish immediately disables public route.
  - Robots `index` requires `public_enabled=true` and fresh governance validation.
  - Robots `noindex` can be applied to any page with audit log.
  - Tests cover robots index refusal for private page and successful noindex rollback.
- **Dependencies:** T9-06.
- **Stop signals:** Robots indexing enabled for private or unvalidated page.

### T9-08: Phase 9 integration and regression tests

- **Scope:** End-to-end tests for member wiki, public disabled invariant, publication audit, source purge behavior, and no graph boundary.
- **Acceptance criteria:**
  - Member can view a reviewed source-backed page.
  - Member cannot view page whose source becomes forgotten/offrecord/nomem.
  - Public route stays disabled until `/wiki_publish` succeeds.
  - Public publish fails if source trace breaks after review.
  - Test asserts no `graph`, `neo4j`, `graphiti`, or `graph_sync_runs` code path is imported by wiki modules.
  - Final review checklist references invariant #10.
- **Dependencies:** T9-01..T9-07.
- **Stop signals:** Integration test needs graph infrastructure; public route passes without publication audit.

---

## §8. Stop Signals

A stream MUST stop and surface the issue as a PR comment / draft PR description if any of these fire:

- Public toggle without `/wiki_publish` admin command → invariant #10 breach → STOP.
- offrecord/forgotten content rendered → STOP.
- `#nomem` content rendered in wiki, search, citations, or public page → STOP.
- Page publication succeeds without source trace to `message_version_id` or approved card sources → STOP.
- Wiki route returns content before member/admin auth check → STOP.
- Renderer emits unsanitized user-authored HTML → STOP.
- Any Phase 9 implementation adds graph, knowledge graph, Neo4j, Graphiti, or `graph_sync_runs` → STOP; this is Phase 10.
- AUTHORIZED_SCOPE or ROADMAP changes during the cycle and removes Phase 9 authorization → STOP.

---

## §9. PR Workflow

Standard `sprint_pr_queue`.

- One PR per Wave unless schema and renderer need separate migration review.
- Branch pattern: `feat/p9-NN-slug`.
- PAR review before each PR.
- Sequential review required for migration, visibility, and public-surface PRs.
- Holistic review after all waves.
- `AUTHORIZED_SCOPE.md` must be updated before Phase 9 work begins.

Phase 9 must not start from this draft alone. Required gates:

- Phase 8 closure confirmed.
- Phase 9 authorization added to `AUTHORIZED_SCOPE.md`.
- Design ratified by team lead.
- Migration numbers reconciled with actual Phase 4-8 revisions.
- Phase 6 approved-card source model confirmed.
- Phase 8 digest archive model confirmed if wiki index links digest archive entries.

Per PR:

1. Create worktree: `git worktree add .worktrees/p9-stream-<X> -b feat/p9-<X>-<slug> main`.
2. Implement ticket-scoped changes only.
3. Run focused pytest for changed area plus `ruff check .` and relevant type checks.
4. Include changed files, tests run, risks, rollback/disable path, and invariant #10 check in PR body.
5. Unified Review with product/governance and technical reviewer.
6. Wait for CI green. Never use admin merge.
7. Merge with rebase and delete branch after approval.
8. Update `IMPLEMENTATION_STATUS.md` Phase 9 row only after implementation is authorized.

**Grounding:** HANDOFF §13 agentic development instructions require ticket scope, tests, no future-phase scope, no forbidden content, and changed files/tests/risks. HANDOFF §15 requires sequential review for visibility/public surfaces.

---

## §10. Glossary

- **Member wiki:** authenticated internal/member web catalog for reviewed source-backed memory pages.
- **Wiki page:** a `wiki_pages` row with Markdown body and explicit source refs.
- **Source trace:** the set of `message_version_id` and/or approved card source refs proving a page's claims.
- **Public candidate:** a reviewed page that may be considered for public publication but is still private until `/wiki_publish` succeeds.
- **Publication log:** append-only audit trail in `wiki_publication_log` for publish/unpublish/robots changes.
- **Robots policy:** per-page indexing control; default `noindex`, may become `index` only after public approval.
- **Renderer:** server-side Markdown-to-HTML converter that sanitizes content and linkifies citations.
- **Governance validator:** service that rejects forgotten, offrecord, nomem, redacted, missing, or unapproved sources before render/publication.

---

## Open Design Questions

1. Wiki backing store: separate `wiki_pages` table vs render-from-cards on-the-fly.
2. Markdown vs HTML storage: store raw Markdown or pre-rendered HTML.
3. Versioning strategy for wiki page edits.
4. Multilingual support scope.
5. Static export option for archive/backup.
6. Should digest archive entries be first-class wiki pages or linked as a separate archive section?
7. Should public publication require two-admin approval or is single admin plus audit sufficient for Phase 9?

DRAFT_PATH: /tmp/PHASE9_PLAN_DRAFT.md
COMPONENTS: 5
TICKETS: T9-01..T9-08
INVARIANT_10_BINDING: yes
PHASE_10_BOUNDARY_CHECK: no graph
GOVERNANCE_RESPECTED: yes
OPEN_DESIGN_QUESTIONS: Wiki backing store: separate wiki_pages table vs render-from-cards on-the-fly; Markdown vs HTML storage; Versioning strategy for wiki page edits; Multilingual support scope; Static export option for archive/backup; Digest archive page model; Public publication approval quorum
