🚧 DRAFT — NOT AUTHORIZED. Phase 7 requires Phase 6 closure + scope authorization.

# Phase 7 — Daily/weekly Digests: Design & Stream Plan

**Status:** Draft only — no implementation authorized.
**Cycle:** Memory system Phase 7 planning.
**Date:** 2026-04-30.
**Predecessor:** Phase 5 `llm_gateway` + usage ledger, Phase 6 approved cards.
**Critical invariant for this phase:** digests are derived, sourced, opt-in, and never canonical truth.

---

## 0. Implementation Status: TBD

Phase 7 is not authorized for implementation in the current scope.

Current source reading confirms:

| Component | Existing? | Notes |
|---|---:|---|
| `AsyncIOScheduler` integration | Yes | `bot/services/scheduler.py` uses UTC scheduler, cron/interval jobs, `replace_existing=True`, `max_instances` on workers where needed. |
| `feature_flags` | Yes | `FeatureFlag` exists; missing flags default OFF by repo contract. |
| `chat_messages` / `message_versions` | Yes | Existing governance fields include `memory_policy`, `is_redacted`, `current_version_id`, `content_hash`. |
| `forget_events` | Yes | Tombstone skeleton and cascade status exist. |
| `llm_usage_ledger` / `llm_gateway` | Not in current models | Phase 5 dependency. This plan assumes Phase 5 closes before Phase 7 starts. |
| `knowledge_cards` / `card_sources` | Not in current models | Phase 6 dependency. This plan assumes approved card sources exist before digest launch. |
| `digests` / `digest_runs` | No | Designed here only. No source files changed. |

Done criterion for this planning task: `/tmp/PHASE7_PLAN_DRAFT.md` exists, contains sections 0-11 plus Final Report, includes an ASCII architecture diagram, 6-8 tickets with acceptance criteria, and marks all unknowns as open design questions.

---

## 1. Non-Negotiable Invariants (verbatim from HANDOFF.md §1)

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

---

## 2. Phase 7 Spec (HANDOFF §2)

### Phase 7 — daily summaries

- **Objective:** daily sourced recap.
- **Scope:** `summaries`, `summary_sources`, daily draft, review / publish.
- **Dependencies:** phase 4 minimum; phase 5 / 6 recommended.
- **Acceptance:** every bullet has source; forgotten source redacts bullet.

---

## 3. Phase 8 Boundary — what Phase 7 MUST NOT do

This Phase 7 draft may design a shared `digests` primitive with `type IN ('daily','weekly')` because the requested implementation surface uses the same runner, storage, publisher, and admin controls. However, Phase 7 must not blur future-phase ownership.

Strict boundary:

- No `reflection_runs`.
- No `observations`.
- No `memory_events`.
- Those are Phase 8 for this planning boundary and must not be modeled, queried, or backfilled by Phase 7 work.
- No raw extraction lifecycle tables.
- No graph projection.
- No wiki pages or public digest archive.
- No auto-publish until destination, format, and review policy are ratified.
- No LLM provider imports in digest code; synthesis routes only through Phase 5 `llm_gateway`.

The digest context is limited to:

- governance-filtered `chat_messages` / current `message_versions`;
- approved Phase 6 cards and their source ids;
- citation ids, never raw message text as a citation anchor.

---

## 4. Architecture Overview

```
                    ┌────────────────────────────────────────────┐
                    │ APScheduler (UTC)                          │
                    │ - daily: 09:00 UTC, configurable            │
                    │ - weekly: Monday 09:00 UTC, configurable    │
                    │ - flags default OFF                         │
                    └────────────────────┬───────────────────────┘
                                         │ triggers
                                         ▼
                    ┌────────────────────────────────────────────┐
                    │ digest_runner / run_digest(...)             │
                    │ type = 'daily' | 'weekly'                   │
                    │ window_start..window_end                    │
                    └────────────────────┬───────────────────────┘
                                         │ queries
                                         ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ Governed source window                                            │
       │ - chat_messages + current message_versions                        │
       │ - memory_policy = 'normal'                                        │
       │ - not redacted                                                    │
       │ - no active tombstone                                             │
       │ - approved cards from last N days                                 │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │ context + citation ids
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ llm_gateway.synthesize_digest(window, type='daily'|'weekly')      │
       │ - same Phase 5 gateway                                            │
       │ - cost ceiling checked before invoke                              │
       │ - usage ledger id returned                                        │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │ body_markdown + citations
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ digests                                                           │
       │ id, type, window_start, window_end, body_markdown, citations      │
       │ JSONB, status, llm_usage_ledger_id, posted_message_id, posted_at  │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │ draft rows
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ digest_publisher                                                  │
       │ - reads draft digests                                             │
       │ - posts to DIGEST_DESTINATION_CHAT_ID via aiogram Bot             │
       │ - records posted_chat_id, posted_message_id, posted_at            │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │ audit
                                       ▼
                    ┌────────────────────────────────────────────┐
                    │ digest_runs                                │
                    │ run_started_at, run_finished_at, error_text │
                    └────────────────────────────────────────────┘
```

Core flow:

1. Scheduler triggers digest_runner daily at 09:00 UTC, configurable by environment/settings.
2. Scheduler triggers weekly digest Mondays at 09:00 UTC, also configurable.
3. `digest_runner` queries a window of `chat_messages` plus approved cards from the last N days.
4. Context query excludes `memory_policy != 'normal'`, redacted rows, and tombstoned rows.
5. Runner calls `llm_gateway.synthesize_digest(window, type='daily'|'weekly')`.
6. Runner stores result in `digests`.
7. Publisher sends draft digest to Telegram destination if configured.
8. `digest_runs` records run audit, including failures and stop signals.

---

## 5. Component Design

### 5.A. Migration `add_digests` (Wave 1 → T7-01)

**Files planned later:** Alembic migration + `bot/db/models.py`. Not changed by this draft.

**Schema: `digests`**

- `id BIGSERIAL PRIMARY KEY`
- `type TEXT NOT NULL CHECK (type IN ('daily','weekly'))`
- `window_start TIMESTAMPTZ NOT NULL`
- `window_end TIMESTAMPTZ NOT NULL`
- `body_markdown TEXT NOT NULL`
- `citations JSONB NOT NULL DEFAULT '[]'::jsonb`
- `status TEXT NOT NULL CHECK (status IN ('draft','posted','failed')) DEFAULT 'draft'`
- `llm_usage_ledger_id BIGINT REFERENCES llm_usage_ledger(id) ON DELETE SET NULL`
- `posted_chat_id BIGINT`
- `posted_message_id BIGINT`
- `posted_at TIMESTAMPTZ`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`

**Constraint:**

```sql
UNIQUE (type, window_start, window_end)
```

This is the idempotency key. A rerun for the same digest type and window must not create a duplicate.

**Schema: `digest_runs`**

- `id BIGSERIAL PRIMARY KEY`
- `digest_id BIGINT REFERENCES digests(id) ON DELETE SET NULL`
- `run_started_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `run_finished_at TIMESTAMPTZ`
- `error_text TEXT`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`

**Citation JSON contract:**

`citations` must reference identifiers only:

- `message_version_id`
- `chat_message_id`
- approved card id
- card source id where available

It must not store raw source message text. The digest body is derived prose; citations are the audit trail back to message versions or approved card sources.

### 5.B. `bot/services/digests.py` (Wave 1 → T7-02)

**Public API:**

```python
async def run_digest(
    session,
    *,
    type: Literal['daily', 'weekly'],
    window_start: datetime,
    window_end: datetime,
) -> Digest
```

This signature is a design contract only. No implementation code is authorized in this draft.

**Behaviour:**

- Checks idempotency first: if `(type, window_start, window_end)` already exists, returns the existing digest and skips LLM invocation.
- Checks Phase 5 `llm_gateway` cost ceiling before invoking synthesis.
- Builds context from `chat_messages` where `memory_policy='normal'` and rows are not redacted.
- Uses only current `message_versions` for message text context and citation anchors.
- Excludes rows with matching pending / processing / completed `forget_events`.
- Adds approved Phase 6 cards from the window.
- Calls `llm_gateway.synthesize_digest(window, type='daily'|'weekly')`.
- Stores `body_markdown`, citation ids, `llm_usage_ledger_id`, and `status='draft'`.
- Returns the `Digest`.

**Context rules:**

- No direct LLM imports.
- No `memory_policy != 'normal'`.
- No redacted content.
- No forgotten/tombstoned content.
- No derived summary as source of truth.
- Card-derived context must be approved and source-backed.

### 5.C. Scheduler hook: extend `bot/services/scheduler.py` (Wave 2 → T7-03)

Existing scheduler pattern:

- global `AsyncIOScheduler(timezone="UTC")`;
- `scheduler.add_job(...)`;
- cron jobs with `hour`, `minute`, `args=[bot]`, `id`, `replace_existing=True`;
- worker-style jobs use `max_instances=1`, `coalesce=True`, and `misfire_grace_time`.

Phase 7 scheduler design:

- Daily job at 09:00 UTC, configurable by settings/env.
- Weekly job Mondays 09:00 UTC.
- Both gated by feature flags:
  - `memory.digests.daily.enabled`
  - `memory.digests.weekly.enabled`
- Both flags default OFF.
- Use `max_instances=1`, `coalesce=True`, and bounded `misfire_grace_time` to avoid duplicate digest generation.
- Job bodies open `async_session()` and call `run_digest(...)`.
- Publisher may run in the same scheduled path or as a separate publish worker; human ratification needed.

### 5.D. Telegram delivery: `bot/services/digest_publisher.py` (Wave 2 → T7-04)

**Purpose:** publish draft digests to Telegram when a destination is configured.

**Behaviour:**

- Reads `digests` where `status='draft'`.
- Requires `settings.DIGEST_DESTINATION_CHAT_ID`.
- If destination is unset, leaves digest as draft and does not raise an error.
- Sends via aiogram `Bot`.
- Handles Markdown formatting safely.
- Marks digest as posted.
- Records `posted_chat_id`, `posted_message_id`, `posted_at`.
- Must not post if a final governance check finds forbidden citations.

**Formatting safety:**

- Digest body must be valid Telegram Markdown or HTML according to whichever parse mode the project ratifies.
- Failure to render must mark the publish attempt in `digest_runs` or an equivalent audit path, not silently swallow the error.
- Publisher must not mutate digest content except for transport-safe escaping if that policy is explicitly chosen.

### 5.E. Admin commands: `bot/handlers/digest.py` (Wave 3 → T7-05)

**Commands:**

- `/digest_now [daily|weekly]` — admin-only manual trigger.
- `/digest_preview <type> [date]` — show without posting.
- `/digest_history` — list recent digests with citations.

**Authorization:**

- Admin-only, using the existing admin identity pattern.
- Non-admin invocation must not reveal digest content or source counts.
- DM vs group availability needs human ratification.

**Behaviour:**

- `/digest_now` creates or returns an idempotent draft for the computed window.
- `/digest_preview` renders the body and citations without posting to the destination chat.
- `/digest_history` lists recent rows with status, window, posted timestamp, and citation count.
- Manual commands must respect feature flags unless explicitly ratified as admin override.

---

## 6. Stream Allocation

Phase 7 has three waves. These are planning streams only until Phase 6 closure and explicit scope authorization.

### Wave 1 — schema and digest core

| Stream | Owner ticket | Component | Deps |
|---|---|---|---|
| **A** | T7-01 | `add_digests` schema migration | Phase 5 ledger schema, Phase 6 cards schema |
| **B** | T7-02 | `bot/services/digests.py` core runner | Phase 5 `llm_gateway`, Phase 6 approved cards |
| **B2** | T7-03 | context/citation builder tests | T7-02 design contract |

### Wave 2 — scheduling and publishing

| Stream | Owner ticket | Component | Deps |
|---|---|---|---|
| **C** | T7-04 | scheduler hooks | T7-02 |
| **D** | T7-05 | Telegram publisher | T7-01, T7-02 |

### Wave 3 — admin controls and closure checks

| Stream | Owner ticket | Component | Deps |
|---|---|---|---|
| **E** | T7-06 | admin commands | T7-01..T7-05 |
| **F** | T7-07 | governance/cost/idempotency regression suite | T7-01..T7-06 |
| **G** | T7-08 | operator docs + rollout checklist | T7-01..T7-07 |

### Wave summary

```
Wave 1:  A schema      B digest core      B2 context tests
            │              │                 │
            └──────┬───────┴────────┬────────┘
                   ▼                ▼
Wave 2:        C scheduler       D publisher
                   │                │
                   └──────┬─────────┘
                          ▼
Wave 3:            E admin commands
                          │
                          ▼
                   F regression suite
                          │
                          ▼
                   G rollout docs
```

---

## 7. Tickets

All tickets are draft backlog items only. Do not create GitHub issues from this plan until Phase 7 is authorized.

| ID | Title | Component | Wave | Size | Deps | Status |
|---|---|---|---:|---|---|---|
| **T7-01** | Schema: `digests` + `digest_runs` tables | A | 1 | M | Phase 5 ledger, Phase 6 cards | Draft |
| **T7-02** | Digest service core: `run_digest(...)` | B | 1 | L | T7-01, Phase 5 gateway | Draft |
| **T7-03** | Context and citation builder | B | 1 | M | T7-02, Phase 6 card sources | Draft |
| **T7-04** | Scheduler hooks for daily/weekly digest jobs | C | 2 | S | T7-02 | Draft |
| **T7-05** | Telegram digest publisher | D | 2 | M | T7-01, T7-02 | Draft |
| **T7-06** | Admin digest commands | E | 3 | M | T7-01..T7-05 | Draft |
| **T7-07** | Governance, idempotency, and cost regression tests | F | 3 | M | T7-01..T7-06 | Draft |
| **T7-08** | Operator rollout docs and ratification checklist | G | 3 | S | T7-07 | Draft |

### T7-01 acceptance

- Migration creates `digests` with all planned columns, CHECK constraints, timestamps, and `UNIQUE(type, window_start, window_end)`.
- Migration creates `digest_runs` with nullable `digest_id`, start/finish timestamps, and `error_text`.
- ORM models match migration types, including JSONB-on-Postgres citation storage.
- Rollback drops only Phase 7 tables and does not touch `chat_messages`, `message_versions`, cards, or ledger tables.

### T7-02 acceptance

- `run_digest(session, *, type, window_start, window_end)` returns existing digest without LLM call when the idempotency key already exists.
- New run checks `llm_gateway` cost ceiling before synthesis and records failure if exceeded.
- New run calls only `llm_gateway.synthesize_digest(...)`, with no direct provider imports.
- New run stores `status='draft'`, body, citations, and `llm_usage_ledger_id`.

### T7-03 acceptance

- Context query includes only `chat_messages.memory_policy='normal'` and non-redacted current versions.
- Tombstoned messages are excluded before reaching `llm_gateway`.
- Approved cards from the window are included only when card sources exist.
- Citations contain ids (`message_version_id`, card id/source id) and never raw message text.

### T7-04 acceptance

- Daily scheduler job is registered at 09:00 UTC by default and is configurable.
- Weekly scheduler job is registered for Monday 09:00 UTC by default and is configurable.
- `memory.digests.daily.enabled` and `memory.digests.weekly.enabled` default OFF and gate job execution.
- Jobs use duplicate-prevention scheduler settings (`replace_existing`, `max_instances=1`, `coalesce=True` where applicable).

### T7-05 acceptance

- Publisher posts only `status='draft'` digests to `settings.DIGEST_DESTINATION_CHAT_ID`.
- If destination is unset, digest remains draft and no error is raised.
- Successful post records `posted_chat_id`, `posted_message_id`, `posted_at`, and sets `status='posted'`.
- Markdown/HTML formatting errors are audited and do not mark the digest posted.

### T7-06 acceptance

- `/digest_now [daily|weekly]` is admin-only and respects idempotency.
- `/digest_preview <type> [date]` renders without posting.
- `/digest_history` lists recent digest windows, statuses, and citation counts.
- Non-admin calls do not reveal digest content or source metadata.

### T7-07 acceptance

- Test proves offrecord/nomem/forgotten content never enters digest context.
- Test proves cost ceiling exceeded creates a failed run and no Telegram post.
- Test proves rerun of the same window does not call `llm_gateway` twice.
- Test proves citation ids survive JSON round-trip.

### T7-08 acceptance

- Rollout checklist documents flags, destination setting, scheduler time, and manual preview path.
- Operator docs state digests are derived and never canonical truth.
- Docs include stop-signal handling for forbidden content, cost ceiling, and missing destination.
- Docs list human decisions required before enabling either flag.

---

## 8. Stop Signals (apply to all streams)

A Phase 7 stream must stop and surface the issue immediately if any of these fire:

- Digest context or body contains `#offrecord`, `#nomem`, redacted, or forgotten content → STOP, do not post.
- Digest citations reference raw message text instead of `message_version_id` or approved card/source ids → STOP.
- LLM cost ceiling exceeded → `digest_runs` records failure, no post.
- Posting destination unset → digest stays as draft, no error raised.
- Direct LLM provider import appears outside `llm_gateway` → STOP.
- Scheduler would run while feature flag is missing or disabled → skip, do not synthesize.
- Weekly digest behavior conflicts with Phase 8 authorization boundary → STOP for human ratification.
- Source card lacks approved source trace → exclude it; if digest depends on it, STOP.

---

## 9. PR Workflow

Standard workflow after authorization:

1. Create feature branch from the correct phase worktree branch.
2. Implement one ticket per PR.
3. Run focused tests for the touched component plus governance regression tests.
4. Run ruff, mypy, and relevant integration tests.
5. PAR review before merge.
6. CI green before merge.
7. Merge only after all stop signals are clear.
8. Update implementation status only after the PR lands.

No PRs, commits, branches, or GitHub issues are created from this draft.

---

## 10. Glossary (Phase 7-specific)

- **Digest:** a derived Markdown recap for a bounded time window. It is not canonical truth and must carry citations.
- **Window:** the inclusive/exclusive time range used to build digest context, represented by `window_start` and `window_end`.
- **Citations:** JSONB references to `message_version_id` and approved card/source ids. Citations do not store raw message text.
- **`digest_runs`:** audit table for digest generation attempts, including start time, finish time, linked digest id, and error text.
- **Feature flag:** opt-in rollout key stored in `feature_flags`. Phase 7 uses `memory.digests.daily.enabled` and `memory.digests.weekly.enabled`, both default OFF.

---

## 11. Open Design Questions

1. Digest format: long-form prose vs bulleted highlights?
2. Telegram destination: community chat itself, or separate digest channel?
3. Admin edit capability: should digests be editable post-LLM before posting?
4. Weekly window: rolling 7 days, or calendar week (Mon-Sun)?
5. Topic organization: cluster by inferred topics, or chronological order?
6. Phase numbering conflict: HANDOFF/ROADMAP assign weekly digest to Phase 8, but this task asks for daily/weekly in Phase 7. Should weekly remain schema-only until Phase 8, or ship behind a separate flag in Phase 7?
7. Publish policy: should scheduled runs create drafts only, or auto-post when destination is configured?
8. Source mix: what ratio should the digest use between raw message evidence and approved cards?
9. Citation rendering: should Telegram output show visible citations inline, footnotes, or admin-only citation detail?
10. Manual override: may admins run `/digest_now` when the feature flag is OFF?
11. Timezone policy: should windows be UTC, Cairo local time, community-configured timezone, or destination-chat timezone?
12. Empty window policy: create an empty draft, skip without row, or post "no digest today"?
13. Cost budget: should daily and weekly digests share one Phase 5 budget bucket or have separate ceilings?
14. Failure visibility: should failed digest runs notify admins immediately, or only appear in history?
15. Markdown policy: use Telegram MarkdownV2 or HTML parse mode for digest posting?

---

## Final Report

DRAFT_PATH: /tmp/PHASE7_PLAN_DRAFT.md
WORD_COUNT: approximately 3216
COMPONENTS: 5
TICKETS: T7-01..T7-08
WAVES: 3
DEPS_NOTED: Phase 5 (llm_gateway) + Phase 6 (cards as digest sources)
GOVERNANCE_RESPECTED: yes (no offrecord/forgotten in digests)
COST_GUARDRAIL: yes (uses Phase 5 ceiling)
OPEN_DESIGN_QUESTIONS:
- Digest format: long-form prose vs bulleted highlights?
- Telegram destination: community chat itself, or separate digest channel?
- Admin edit capability: should digests be editable post-LLM before posting?
- Weekly window: rolling 7 days, or calendar week (Mon-Sun)?
- Topic organization: cluster by inferred topics, or chronological order?
- Phase numbering conflict: HANDOFF/ROADMAP assign weekly digest to Phase 8, but this task asks for daily/weekly in Phase 7. Should weekly remain schema-only until Phase 8, or ship behind a separate flag in Phase 7?
- Publish policy: should scheduled runs create drafts only, or auto-post when destination is configured?
- Source mix: what ratio should the digest use between raw message evidence and approved cards?
- Citation rendering: should Telegram output show visible citations inline, footnotes, or admin-only citation detail?
- Manual override: may admins run `/digest_now` when the feature flag is OFF?
- Timezone policy: should windows be UTC, Cairo local time, community-configured timezone, or destination-chat timezone?
- Empty window policy: create an empty draft, skip without row, or post "no digest today"?
- Cost budget: should daily and weekly digests share one Phase 5 budget bucket or have separate ceilings?
- Failure visibility: should failed digest runs notify admins immediately, or only appear in history?
- Markdown policy: use Telegram MarkdownV2 or HTML parse mode for digest posting?
