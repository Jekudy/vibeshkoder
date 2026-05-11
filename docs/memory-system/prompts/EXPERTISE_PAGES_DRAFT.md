🚧 DRAFT — NOT AUTHORIZED. **DEFERRED — NUMBERING CONFLICT RESOLVED 2026-05-02.**

> **DEFERRAL NOTICE (2026-05-02):** This draft describes "person expertise pages" and was
> filed under "Phase 11" by an earlier exploration round. The canonical Phase 11 in
> `ROADMAP.md`, `HANDOFF.md §Phase 11`, and `AUTHORIZED_SCOPE.md §Phase 11` is
> **Shkoderbench / evals harness**, ratified as `docs/memory-system/PHASE11_PLAN.md` on
> 2026-05-02 by Orchestrator C.
>
> This draft is preserved as a **deferred design artifact** for a future phase
> (likely Phase 13+ or an extension of Phase 6/9). It is NOT authorized for
> implementation. Do NOT create issues from this file. Do NOT migrate from it.
>
> When/if person-expertise-page work is authorized, re-number this artifact to its
> assigned phase and re-ratify under the current AUTHORIZED_SCOPE.md before any
> implementation begins.

---

# Phase 11 Plan Draft — Person Expertise Pages / "Who Knows X"

## §0. Banner + Status

🚧 DRAFT — NOT AUTHORIZED.

**Status:** design-only draft. Do not implement, migrate, route, deploy, or create issues from
this file until explicit authorization is given.

**Scope:** per-user expertise pages for Shkoderbot memory system: "what does this person know /
talk about / contribute on?" Pages are member-only and reuse the Phase 9 wiki visibility pattern.

**Canonical-source check:**

- `docs/memory-system/HANDOFF.md` was checked for §1 invariants 1-10 and explicit Phase 11
  mention.
- `docs/memory-system/AUTHORIZED_SCOPE.md` was checked for the Phase 6+ pointer. It explicitly
  lists "Person expertise pages — Phase 6+" as not authorized in the current scope.
- `docs/memory-system/ROADMAP.md` was checked for the phase table. Current canonical Phase 11 is
  "Shkoderbench / evals", not person expertise pages.
- `docs/memory-system/PHASE4_PLAN.md` was requested as a structure source but does not exist in
  this worktree.
- `docs/memory-system/prompts/` was requested for `PHASE6_*`, `PHASE8_*`,
  `PHASE9_PLAN_DRAFT.md`, but the directory does not exist in this worktree.

**Numbering note:** this document uses the user's requested "Phase 11" name for person expertise
pages, but the current roadmap already assigns Phase 11 to evals. Treat this as a draft proposal
that needs roadmap reconciliation before execution.

**Decision summary:** default is member-visible, not public, with explicit opt-out via
`/intro_privacy private`. Summaries are advisory and evidence-backed; they never become canonical
truth.

## §1. Invariants

Copied verbatim from `docs/memory-system/HANDOFF.md` §1 "Non-negotiable invariants":

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

## §2. Phase 11 Spec

### Objective

Build member-only per-person expertise pages answering:

- What does this member know about?
- What topics do they repeatedly discuss or contribute evidence to?
- Which approved cards did they create, cite, or meaningfully support?
- Who might be a useful mentor, reviewer, or intro target for a topic?

The product surface is `/people/<username>` and a future "who knows X" discovery path that ranks
members by evidence-backed topic affinity.

### Source interpretation

`HANDOFF.md` does not define person expertise pages as canonical Phase 11. It explicitly mentions
person expertise pages only as not-yet-buildable future scope and as a risk:

- immediate "Not authorized yet" list includes person expertise pages;
- `AUTHORIZED_SCOPE.md` says "Person expertise pages — Phase 6+";
- risk register calls out "Person expertise creepiness" with mitigation: member/internal,
  evidence-based, opt-out;
- glossary defines visibility filters as applying to wiki, catalog, expertise pages.

Therefore this spec infers the feature from Phase 6+ catalog data, Phase 8 digest/observation
signals, Phase 9 member rendering/visibility rules, and Phase 10 graph traversal when available.

### Product behavior

Each person page renders:

- **Top topics:** cached list of topics with score labels, evidence counts, and last activity date.
- **Evidence trail:** only approved card sources and governed `message_version_id` citations.
- **Created/cited cards:** cards authored by, cited by, or materially linked to the person.
- **Contribution summary:** short advisory text generated from approved evidence only.
- **Graph neighborhood:** related topics, cards, people, and events from Phase 10 when graph is
  enabled.
- **Privacy state:** no page renders for users who have opted out with `/intro_privacy private`.

The page must not present expertise as an objective credential. Language should stay bounded:
"has discussed", "has contributed sources on", "may know about", "ask for context", not "is an
expert" unless the approved card itself says so and cites a source.

### Entry criteria

- Phase 3 governance filters and tombstones are active.
- Phase 6 cards and admin review exist, with required card sources.
- Phase 8 observations/digest signals exist or are explicitly absent with a degraded path.
- Phase 9 member-only rendering/auth pattern exists.
- Phase 10 graph projection is optional; if absent, the page degrades gracefully to SQL-backed
  cards + observations.

### Exit criteria

- A member can open `/people/<username>` only after member authentication/authorization passes.
- The page is hidden for opted-out users.
- Every displayed topic is traceable to an approved card source or governed observation source.
- Recompute produces deterministic cached topics and `last_updated`.
- If Phase 10 is disabled, graph sections disappear without breaking the page.
- Stop-signal tests cover offrecord/forgotten leakage and opted-out users.

### Open Design Questions and Decisions

1. **Privacy default — public to members vs opt-in?** Decision: member-visible by default after
   pre-announcement and admin enablement, never public. Users can opt out with
   `/intro_privacy private`; opted-out pages return 404 or neutral "not available" to avoid
   leaking privacy state.
2. **Expertise scoring algorithm — frequency, recency-weighted, graph centrality?** Decision:
   start with a conservative blended score: approved card evidence count + recency-weighted
   observations + card citation count. Phase 10 graph centrality is an optional boost only, never
   the sole reason a topic appears.
3. **Recompute cadence — on-demand only vs nightly job?** Decision: support on-demand admin
   recompute first via `/recompute_person <username>`, then add nightly recompute after the
   scoring output is stable and evals exist.
4. **Multi-language topic handling?** Decision: store canonical topic slugs plus display labels
   per language. Merge obvious aliases only through reviewed topic/card relations, not by
   free-form LLM guessing.
5. **Mentor/student role distinction on the page?** Decision: do not infer "mentor" or
   "student" as a hard role in v1. Show "can help with" and "has asked about" sections only when
   evidence distinguishes contribution vs question patterns.
6. **What if Phase 10 graph is not deployed?** Decision: degrade gracefully. Render top topics,
   cards, and observations from Postgres; omit graph neighborhood and mark graph-dependent
   recompute metrics as unavailable.
7. **Can admins override a person's visibility?** Decision: admins may hide a page for safety,
   but may not force-show an opted-out person. User privacy wins over admin convenience.
8. **Can low-confidence topics show?** Decision: no. Low-confidence topics remain admin-only
   diagnostics until enough approved evidence exists.

## §3. Phase 12 Boundary

Phase 12 remains the future butler action layer and is out of scope here.

Person expertise pages may help a member decide whom to ask, but they must not:

- send intros automatically;
- message a person on behalf of another person;
- schedule calls;
- create tasks;
- update vouching state;
- infer consent to be contacted;
- call external systems;
- read raw DB tables directly for action context;
- bypass governance-filtered evidence.

Allowed extension point only: a future butler may request governance-filtered evidence context
from the same aggregator/evidence APIs, subject to invariant 7. No butler code, queues,
`action_requests`, or `action_runs` are part of this phase.

## §4. Architecture ASCII Diagram

```text
Member browser
  |
  | GET /people/<username>
  v
FastAPI app
  |
  | include_router(people_router)
  v
web/routers/people.py
  |
  | 1. require member auth (Phase 9 wiki pattern)
  | 2. resolve username -> users row
  | 3. check intro_privacy != private
  | 4. apply visibility/governance filters
  v
bot/services/person_summary.py
  |
  +--> person_pages cache
  |      - cached_topics
  |      - summary_json
  |      - last_updated
  |
  +--> Phase 6 cards
  |      - approved knowledge cards
  |      - card_sources -> message_version_id
  |
  +--> Phase 8 observations
  |      - governed observations
  |      - contribution/question signals
  |
  +--> Phase 10 graph (optional)
         - graph neighborhood
         - topic/person/card relations
         - omitted when memory.graph.enabled = false
  |
  v
web/templates/people/
  |
  | people/detail.html
  | people/not_available.html
  v
HTML member-only page
  |
  +--> /people/<username>
  +--> future /people?topic=<slug> ("who knows X")
```

Data rule: Postgres remains the source of truth. `person_pages` is a cache/materialized view. Graph
data is derived and rebuildable. Any mismatch is resolved in favor of governed Postgres evidence.

## §5. Components

### A. `person_pages` Schema

Add a cached/materialized table for fast page rendering:

- `id`
- `user_id`
- `username_snapshot`
- `privacy_state_snapshot`
- `cached_topics` JSONB list with topic slug, label, score, evidence counts, and last evidence date
- `summary_json` JSONB with advisory sections
- `graph_neighborhood_json` JSONB nullable
- `source_counts_json` JSONB
- `last_updated`
- `computed_from_run_id`

The table is derived. It must be purgeable/recomputable and cannot be used as a canonical source.
All rows must be invalidated when relevant tombstones, card approvals, privacy changes, or graph
sync changes occur.

### B. Aggregator Service: `bot/services/person_summary.py`

Service responsibilities:

- build a governed evidence bundle for one user;
- aggregate approved cards created/cited/linked to that user;
- aggregate Phase 8 observations with policy filters;
- optionally request Phase 10 graph neighborhood;
- compute top topics deterministically;
- write one `person_pages` cache row transactionally;
- expose read model helpers for `web/routers/people.py`.

The service must fail closed: if privacy state, governance filters, or source availability are
uncertain, it returns "not available" instead of rendering partial risky content.

### C. Templates: `web/templates/people/`

Minimum templates:

- `detail.html` — member page with top topics, cards, contribution summary, evidence links,
  optional graph neighborhood.
- `not_available.html` — neutral unavailable page for missing, opted-out, or unauthorized cases.
- `_topic_list.html` — reusable topic list partial.
- `_source_list.html` — citation/source partial.

No raw message text is rendered unless it is already allowed by the same source/citation rules used
by Phase 9 wiki. Templates should prefer source labels and card titles over long quotes.

The templates are rendered only through `web/routers/people.py`, which mounts
`GET /people/<username>` and future `GET /people?topic=<slug>` behind the same member-only gate as
Phase 9 wiki. The router never bypasses `person_summary.py` for raw DB reads.

### D. Admin Command: `/recompute_person <username>`

Admin-only Telegram command that:

- resolves the username to a known member;
- checks current privacy state;
- runs `person_summary.recompute_person(user_id)`;
- reports updated topic count, source count, and `last_updated`;
- refuses on opted-out users unless the command is only purging/hiding the page.

This is the v1 recompute path. Nightly recompute can be added after scoring is validated.

### E. Privacy Controls: `/intro_privacy private`

Privacy contract:

- `/intro_privacy private` hides the person page and removes the user from "who knows X" rankings.
- The cache row is deleted or marked non-renderable immediately.
- Future recomputes must skip the user.
- The route returns a neutral unavailable page, not "this user opted out".
- Reverting privacy to a less restrictive state requires explicit user action and should not
  resurrect hidden content until recompute runs under current governance filters.

Regression tests for this component set must cover: `#offrecord`/`#nomem`/forgotten sources
excluded; opted-out user page not rendered; graph absence still renders SQL-backed sections; score
determinism for fixed evidence; and no page treating `person_pages` summary as canonical citation.

## §6. Streams

### Wave 1 — Contracts and Privacy Gates

Parallel work:

- define `person_pages` schema and cache invalidation rules;
- define `/intro_privacy private` behavior and page hiding semantics;
- define router auth contract by copying Phase 9 member-only wiki pattern;
- define stop-signal tests before implementation.

Exit: schema and privacy contract can be reviewed without touching product code.

### Wave 2 — Aggregation and Rendering

Parallel work:

- implement `person_summary.py` read model against Phase 6 cards;
- add Phase 8 observation aggregation;
- build `web/routers/people.py` and templates;
- add `/recompute_person <username>` admin command.

Exit: one user page can render from governed Postgres evidence with graph disabled.

### Wave 3 — Graph Enhancement and "Who Knows X"

Parallel work:

- add optional Phase 10 graph neighborhood enrichment;
- add topic search/ranking endpoint for "who knows X";
- add nightly recompute job only after on-demand scoring is validated;
- add admin diagnostics for low-confidence/hidden topics.

Exit: graph improves discovery but is never required for correctness.

## §7. Tickets T11-XX

Follow-up note: `HANDOFF.md` currently has only `T11-01 eval tables / runner` for canonical Phase
11. The ticket numbering below is for this draft's proposed person-expertise Phase 11 and should
be reconciled before issue creation.

| ID     | Title                                      | Description |
|--------|--------------------------------------------|-------------|
| T11-01 | Reconcile Phase 11 numbering                | Update roadmap ownership so person expertise pages do not conflict with existing Shkoderbench/evals Phase 11. |
| T11-02 | Define `person_pages` derived schema         | Add a design-approved schema for cached topics, source counts, graph neighborhood, and `last_updated`. |
| T11-03 | Implement privacy state contract             | Specify and implement `/intro_privacy private` semantics for hiding person pages and "who knows X" rankings. |
| T11-04 | Build `person_summary.py` aggregator         | Aggregate approved Phase 6 cards and Phase 8 observations into deterministic advisory person summaries. |
| T11-05 | Add member-only people router                | Add `web/routers/people.py` with `/people/<username>` guarded by the Phase 9 member-only auth pattern. |
| T11-06 | Add `web/templates/people/` rendering         | Render top topics, approved cards, evidence links, privacy-safe unavailable state, and optional graph section. |
| T11-07 | Add `/recompute_person <username>` command   | Provide admin-only on-demand recompute with source counts, topic counts, and safe refusal for opted-out users. |
| T11-08 | Add graph graceful-degradation path          | Integrate Phase 10 graph neighborhood only when enabled and hide graph UI when unavailable. |
| T11-09 | Add "who knows X" topic ranking              | Rank visible members for a topic using the same cached governed evidence and privacy filters. |
| T11-10 | Add leakage and opt-out regression tests     | Prove offrecord/forgotten sources and opted-out users never render in pages or rankings. |

## §8. Stop Signals

Stop implementation immediately if any of these happen:

- A rendered page shows `#offrecord`, `#nomem`, or forgotten content.
- An opted-out user's page still renders.
- An opted-out user appears in "who knows X".
- A page uses graph output as source of truth.
- A page cites a summary/cache row instead of `message_version_id` or approved card sources.
- A route renders without member authorization.
- The implementation needs raw `telegram_updates` access for page rendering.
- Scoring labels a person as an "expert" without explicit reviewed evidence.
- Low-confidence inferred topics appear on member-visible pages.
- Public crawlers can access person pages.

If a stop signal triggers, disable `memory.people.enabled`, purge `person_pages` cache, and review
the evidence path before re-enabling.

## §9. PR Workflow

No PRs are authorized from this draft yet.

When authorized, keep PRs small and ticket-scoped:

1. PR for roadmap reconciliation and feature flags only.
2. PR for schema/cache model only.
3. PR for privacy command/hide behavior only.
4. PR for aggregator service only.
5. PR for member-only router/templates only.
6. PR for admin recompute command only.
7. PR for graph enhancement and "who knows X" only after SQL-backed page works.

Every PR must include:

- changed files list;
- tests run;
- privacy risk note;
- evidence/citation path note;
- confirmation that no raw DB bypass or LLM bypass was added;
- confirmation that `person_pages` is derived/advisory;
- rollback plan, usually feature flag off + cache purge.

Suggested feature flags:

- `memory.people.enabled`
- `memory.people.graph_enabled`
- `memory.people.who_knows_enabled`
- `memory.people.nightly_recompute_enabled`

## §10. Glossary

**Person expertise page** — member-only page describing evidence-backed topics a person has
discussed, contributed sources to, or helped clarify.

**Who knows X** — discovery query that ranks visible members for a topic using governed evidence,
not public search.

**`person_pages`** — derived cache/materialized read model for fast rendering. It is not canonical
truth.

**Cached topics** — JSONB topic list containing slug, label, score, counts, source references, and
last evidence date.

**Advisory summary** — generated or aggregated description that helps humans navigate evidence. It
must never be treated as truth without source citations.

**Approved card source** — Phase 6 `card_sources` entry pointing to a `message_version_id` or other
approved citation anchor.

**Observation** — Phase 8 signal derived from governed source evidence. It can inform ranking only
when the source remains visible and not forgotten.

**Graph neighborhood** — optional Phase 10 derived relations around a person: topics, cards, events,
and related people.

**Member-only** — visible only after the same membership gate used for Phase 9 wiki.

**Opt-out** — user privacy state set by `/intro_privacy private`, which hides pages and removes the
user from rankings.

**Stop signal** — privacy or truthfulness failure requiring immediate disablement and review.

---

FINAL REPORT

```text
DRAFT_PATH: /tmp/PHASE11_PLAN_DRAFT.md
COMPONENTS: 5
TICKETS: T11-01..T11-10
INVARIANT_5_BINDING: yes (person summaries marked advisory; never canonical)
PRIVACY_DEFAULT: member-visible by default after admin enablement and pre-announcement; never public; user opt-out via /intro_privacy private
DEPS_NOTED: Phase 6 (cards), Phase 8 (observations), Phase 10 (graph), Phase 9 (wiki rendering pattern)
OPEN_DESIGN_QUESTIONS: privacy default; expertise scoring algorithm; recompute cadence; multi-language topic handling; mentor/student role distinction; Phase 10 graph graceful degradation; admin override limits; low-confidence topic visibility
```
