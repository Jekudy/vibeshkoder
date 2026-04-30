🚧 DRAFT — NOT AUTHORIZED. Phase 8 requires Phase 7 closure + scope authorization.

# Phase 8 — Reflection Runs / Observations / Memory Events: Draft Design & Stream Plan

**Status:** DRAFT ONLY — not authorized for implementation.
**Cycle:** Memory system Phase 8 planning draft.
**Date:** 2026-04-30.
**Predecessor:** Phase 7 must be closed before this work can start.
**Authorization:** none. This document is a planning artifact only.
**Critical invariant for this phase:** observations are advisory derived data, never canonical truth.

---

## §0. Document Status

This document is a draft planning artifact for a proposed Phase 8 scope: reflection runs,
observations, and memory events.

It is not an implementation ticket, not a migration authorization, and not a permission to add
runtime LLM calls, scheduler jobs, admin commands, or database tables.

Prerequisites before any Phase 8 implementation:

- Phase 7 closure is confirmed.
- Team lead explicitly authorizes Phase 8 scope.
- The canonical phase mismatch is ratified: `HANDOFF.md` currently places
  `memory_events`, `observations`, and `reflection_runs` in Phase 5, while Phase 8 is weekly
  digest. This draft treats reflection as a proposed Phase 8 slice and marks that discrepancy
  as an open ratification item.
- `llm_gateway` and `llm_usage_ledger` already exist and are the only path for LLM calls.
- Governance filters are proven for `#nomem`, `#offrecord`, and forgotten content.
- Cost ceiling policy exists and is enforced before calling `llm_gateway`.

No implementation files are changed by this draft.

---

## §1. Non-Negotiable Invariants (verbatim from HANDOFF.md §1)

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

Phase 8 binding interpretation:

- Invariant #5 binds all observations: an observation is advisory derived interpretation, not
  canonical memory, not a card, not a summary, and not a source of truth.
- Invariant #6 binds this phase by exclusion: no graph projection, no graph sync, and no graph
  as source of truth. Graph remains Phase 10.

---

## §2. Phase 8 Spec (from HANDOFF.md + draft interpretation)

### Canonical HANDOFF text for Phase 8

- **Phase:** Phase 8 — weekly digest.
- **Objective:** weekly editorial digest.
- **Scope:** `digests`, `digest_sections`, weekly ranking / review / publish.
- **Dependencies:** phase 7 and cards / events preferred.
- **Acceptance:** required sections have sources and review state.

### Canonical HANDOFF text for reflection-related objects

`HANDOFF.md` currently lists reflection-related objects in Phase 5:

- **Phase:** Phase 5 — events + observations + extraction candidates.
- **Objective:** create structured pre-catalog memory.
- **Scope:** `llm_usage_ledger`, `extraction_runs`, `memory_events`, `observations`,
  `reflection_runs`, `memory_candidates`.
- **Dependencies:** phase 4 + `llm_gateway`.
- **Entry criteria:** governance filters, evidence bundle, ledger, budget guard.
- **Exit criteria:** high-signal windows produce sourced candidates.
- **Acceptance:** no forbidden source sent to LLM; every output has source refs.
- **Risks:** hallucinated extraction, budget runaway.
- **Rollback:** derived rows rebuildable / deletable.

### Draft Phase 8 interpretation requiring ratification

This draft assumes Phase 8 may be authorized as a reflection layer that supports later digests
and recall without becoming a public surface. Under that interpretation, Phase 8 creates:

- `reflection_runs`: auditable runs over bounded windows.
- `observations`: advisory, sourced, confidence-scored interpretations.
- `memory_events`: append-only operational event stream for audit and future replay.
- optional consumption by `/recall` and digests as soft hints only.

This interpretation must be explicitly approved because it differs from the current condensed
roadmap, where Phase 8 is weekly digest and observations are Phase 5.

---

## §3. Phase 9 Boundary

Phase 8 must not drift into Phase 9 or Phase 10.

Out of scope:

- No public wiki.
- No member wiki pages.
- No internal browsable catalog pages.
- No public surfaces of any kind.
- No wiki archive for observations.
- No Graphiti, Neo4j, graph schema, graph sync, graph sidecar, or graph projection.
- No person expertise pages.
- No automatic promotion from observation to public knowledge.

Phase 9 remains the first wiki / community catalog phase:

- **Objective:** internal / member browsable catalog.
- **Scope:** member / internal pages, digest archive, source citations, visibility filters.
- **Dependencies:** phases 3, 6, 8.
- **Acceptance:** member catalog shows only approved visible cards. Public wiki disabled by
  default.

Phase 10 remains graph projection:

- **Objective:** derived graph traversal.
- **Acceptance:** graph can be rebuilt from postgres; forget purges graph.

---

## §4. Architecture Overview

```
Scheduler
  └── reflection_runner
        ├── different cadence from digest_runner
        ├── different scope from digest_runner
        ├── selects governed chat_messages window
        ├── joins current message_versions for citations
        ├── reads approved cards when relevant
        ├── reads recent qa_traces for recurring questions
        ├── calls llm_gateway with reflection prompts
        ├── writes reflection_runs
        ├── writes observations with policy='advisory'
        └── appends memory_events

/recall and digests
  └── may optionally consume observations as soft hints
      ├── feature flagged
      ├── clearly marked advisory
      └── never replace direct citations or approved cards
```

Core design:

- `reflection_runner` is separate from `digest_runner`. Reflection is analytical and advisory;
  digest is editorial and review/publish oriented. They need different cadence, scope, failure
  handling, and output tables.
- A reflection pass queries a time window of governed `chat_messages`, current
  `message_versions`, approved cards, and recent `qa_traces`.
- The runner sends only governance-filtered evidence to `llm_gateway`.
- Prompt outputs become structured `observations` rows, not cards and not summaries.
- Each observation carries `confidence_score`, `topic_tags`, `cited_message_version_ids`,
  `cited_card_ids`, and `policy='advisory'`.
- `/recall` can use observations only as query expansion or context hints, behind
  `memory.qa.consume_observations.enabled`.
- Digests can include a "Recent observations" section only when the digest itself cites real
  sources and labels the section as advisory.

---

## §5. Component Design

### 5.A. Migration `add_observations_and_reflection` (Stream A)

Design-only files, if later authorized:

- `alembic/versions/NNN_add_observations_and_reflection.py`
- `bot/db/models.py`
- repositories for `reflection_runs`, `observations`, and `memory_events`

Proposed schema:

```sql
CREATE TABLE reflection_runs (
  id BIGSERIAL PRIMARY KEY,
  run_started_at TIMESTAMPTZ NOT NULL,
  run_finished_at TIMESTAMPTZ,
  scope TEXT NOT NULL CHECK (scope IN ('chat_window', 'topic', 'user_activity')),
  window_start TIMESTAMPTZ NOT NULL,
  window_end TIMESTAMPTZ NOT NULL,
  llm_usage_ledger_id BIGINT REFERENCES llm_usage_ledger(id),
  observation_count INTEGER NOT NULL DEFAULT 0,
  error_text TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE observations (
  id BIGSERIAL PRIMARY KEY,
  reflection_run_id BIGINT NOT NULL REFERENCES reflection_runs(id),
  observation_kind TEXT NOT NULL CHECK (
    observation_kind IN (
      'topic_shift',
      'user_activity',
      'sentiment',
      'recurring_question',
      'other'
    )
  ),
  title TEXT NOT NULL,
  body_markdown TEXT NOT NULL,
  confidence_score NUMERIC(3,2) NOT NULL CHECK (
    confidence_score >= 0.00 AND confidence_score <= 1.00
  ),
  policy TEXT NOT NULL DEFAULT 'advisory' CHECK (policy = 'advisory'),
  cited_message_version_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  cited_card_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  topic_tags TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ
);

CREATE TABLE memory_events (
  id BIGSERIAL PRIMARY KEY,
  event_type TEXT NOT NULL CHECK (
    event_type IN (
      'reflection_started',
      'reflection_completed',
      'observation_added',
      'observation_expired',
      'observation_cited'
    )
  ),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Schema decisions:

- `observations.policy` is required and restricted to `advisory` at schema level. This is the
  database-level binding for invariant #5.
- `memory_events` is append-only. It is for audit and future butler-context replay, not a
  mutable state table.
- `cited_message_version_ids` must refer to current or historically valid message versions that
  pass governance filters at run time.
- `cited_card_ids` may point only to approved cards.
- `expires_at` is nullable until the expiry policy is ratified.

Acceptance:

- Migration applies and rolls back cleanly on postgres.
- `observations.policy != 'advisory'` is rejected by the database.
- invalid `observation_kind` is rejected by the database.
- invalid `confidence_score` outside 0.00-1.00 is rejected by the database.
- no graph tables or wiki tables are introduced.

### 5.B. Reflection service (Stream B)

Design-only file, if later authorized:

- `bot/services/reflection.py`

Public API shape:

```python
async def run_reflection_pass(
    session,
    *,
    window_start,
    window_end,
    scope,
):
    ...
```

Responsibilities:

- validate `scope in {'chat_window', 'topic', 'user_activity'}`;
- enforce idempotency on `(scope, window_start, window_end)`;
- check feature flag before doing work;
- check cost ceiling before calling `llm_gateway`;
- create `reflection_started` memory event;
- create `reflection_runs(status='running')`;
- query only governance-filtered content:
  - `chat_messages.memory_policy = 'normal'`;
  - `chat_messages.is_redacted = false`;
  - current `message_versions.is_redacted = false`;
  - no active forget tombstone applies;
- gather approved cards and recent `qa_traces` where relevant;
- call `llm_gateway` with bounded prompts and max-output constraints;
- validate returned observation payloads before inserting;
- write `observations` and `observation_added` memory events in one transaction;
- update `reflection_runs(status='completed', observation_count=N)`;
- on cost ceiling breach, write `reflection_runs(status='failed', error_text=...)` and no
  observations.

Idempotency:

- A completed run for the same `(scope, window_start, window_end)` skips by default.
- A failed run can be retried manually only through admin command.
- Idempotency must not hide partial inserts. Inserts happen after payload validation; failed
  payload validation records failure and writes no observations.

Cost guardrail:

- Reflection must pre-estimate prompt tokens from the selected evidence window.
- If the estimate exceeds the configured ceiling, the run fails closed.
- `llm_usage_ledger_id` must be linked when an LLM call occurs.
- No direct provider SDK usage is allowed.

Acceptance:

- offrecord, nomem, redacted, and forgotten content never reaches the prompt.
- cost ceiling failure writes a failed run and zero observations.
- duplicate manual trigger for the same completed window is skipped or returns the existing run.
- no imports of model provider SDKs.

### 5.C. Reflection prompts (Stream B)

Design-only file, if later authorized:

- `bot/services/reflection_prompts.py`

Prompt templates:

1. `topic_shift`
2. `user_activity`
3. `sentiment`
4. `recurring_question`
5. `other`

Each prompt must enforce:

- cite every claim through `message_version_id` or approved `card_id`;
- never produce canonical-sounding text;
- use explicit advisory phrasing;
- include `confidence_score` in the 0.00-1.00 range;
- include `policy='advisory'`;
- refuse when evidence is too thin;
- never infer identity, intent, or sentiment without evidence;
- never include source text from forbidden content because forbidden content must not be in the
  input bundle.

Output contract:

```json
{
  "observation_kind": "topic_shift",
  "title": "Short advisory title",
  "body_markdown": "Advisory observation with citations.",
  "confidence_score": 0.72,
  "policy": "advisory",
  "cited_message_version_ids": [123, 124],
  "cited_card_ids": [],
  "topic_tags": ["example"]
}
```

Acceptance:

- all templates include the advisory policy requirement;
- all templates require citations;
- all templates include refusal behavior;
- parser rejects missing citations, missing `policy`, non-advisory policy, and invalid confidence.

### 5.D. Admin commands (Stream D)

Design-only file, if later authorized:

- `bot/handlers/reflection.py`

Commands:

- `/reflect_now [scope]` — admin-only manual trigger.
- `/observations [topic|user|date]` — list observations with filters.
- `/observation <id>` — full details with citations.

Authorization:

- all commands are admin-only;
- non-admin access logs an audit event without running reflection;
- commands must respect feature flags.

Behavior:

- `/reflect_now` validates scope and uses a bounded default window.
- `/observations` lists only advisory observations and includes confidence, kind, age, expiry,
  and citation count.
- `/observation <id>` shows the full advisory body, confidence score, topic tags, cited
  message version IDs, cited card IDs, and run metadata.

Acceptance:

- non-admin cannot trigger or inspect observations;
- missing observation ID returns a clear not-found response;
- command output visibly marks observations as advisory;
- no command publishes to wiki or digest automatically.

### 5.E. Integration with `/recall` and digests (Stream E)

Design-only files, if later authorized:

- `/recall` integration in the existing Q&A service.
- digest integration in the Phase 7/8 digest service.

Feature flags:

- `memory.reflection.enabled`
- `memory.qa.consume_observations.enabled`
- optional `memory.digest.include_observations.enabled`

Rules for `/recall`:

- observations are soft hints only;
- direct evidence still wins over observations;
- answer citations must still point to `message_version_id` or approved cards;
- if an observation is used to expand or bias retrieval, write `observation_cited` memory event;
- the response must not present observation text as canonical truth.

Rules for digests:

- "Recent observations" section is optional;
- every digest bullet still needs source trace;
- advisory observations can suggest topics, not become published facts;
- digest review state remains separate from reflection state.

Acceptance:

- disabling `memory.qa.consume_observations.enabled` removes observation consumption entirely;
- disabling digest observation flag removes the section entirely;
- observation usage is auditable through `memory_events`;
- recall and digest tests prove advisory labeling.

---

## §6. Stream Allocation

### Wave 1 — foundations

| Stream | Component | Tickets | Deps |
|---|---|---|---|
| A | Schema | T8-01, T8-02 | Phase 7 closure + authorization |
| B+C | Reflection service + prompts | T8-03, T8-04, T8-05 | schema design ratified, `llm_gateway`, ledger, budget guard |

Wave 1 establishes the derived-data substrate and the only permitted runtime path for
reflection. B and C are paired because prompt output validation is part of service correctness.

### Wave 2 — admin operations

| Stream | Component | Tickets | Deps |
|---|---|---|---|
| D | Admin handlers | T8-06 | T8-01..T8-05 |

Wave 2 adds manual control and visibility, but no public surface.

### Wave 3 — optional consumers

| Stream | Component | Tickets | Deps |
|---|---|---|---|
| E | `/recall` and digest integration | T8-07, T8-08 | T8-06, Phase 5/7 interfaces |
| QA | tests / eval hardening | T8-09 | T8-01..T8-08 |

Wave 3 is intentionally last because observations must first prove advisory handling before any
consumer is allowed to read them.

---

## §7. Tickets

| ID | Title | Wave | Scope | Dependencies |
|---|---|---|---|---|
| T8-01 | Migration: `reflection_runs`, `observations`, `memory_events` | Wave 1 | Add the three derived-layer tables with advisory policy checks, confidence range checks, and event type checks. | Phase 7 closure; scope authorization |
| T8-02 | Repos and models for reflection tables | Wave 1 | Add typed persistence surfaces for reflection runs, observations, and append-only memory events. | T8-01 |
| T8-03 | Reflection runner service | Wave 1 | Implement `run_reflection_pass(session, *, window_start, window_end, scope)` with governance filters, idempotency, ledger linkage, and cost ceiling failure behavior. | T8-01, T8-02, existing `llm_gateway`, ledger |
| T8-04 | Reflection prompt templates | Wave 1 | Add five prompt templates for `topic_shift`, `user_activity`, `sentiment`, `recurring_question`, and `other`, each enforcing citations and `policy='advisory'`. | T8-03 interface design |
| T8-05 | Reflection output validator | Wave 1 | Validate prompt outputs before insert: allowed kind, advisory policy, citation presence, confidence range, topic tags shape. | T8-03, T8-04 |
| T8-06 | Admin reflection commands | Wave 2 | Add admin-only `/reflect_now [scope]`, `/observations [topic|user|date]`, and `/observation <id>` command surfaces. | T8-01..T8-05 |
| T8-07 | `/recall` optional observation hints | Wave 3 | Let `/recall` optionally consume observations as soft hints behind `memory.qa.consume_observations.enabled`, with direct evidence still required. | T8-06; Phase 4 `/recall`; feature flag ratification |
| T8-08 | Digest optional observation section | Wave 3 | Let digests optionally include "Recent observations" when relevant, clearly advisory and backed by source trace. | T8-06; Phase 7 digest service |
| T8-09 | Tests and evals for reflection safety | Wave 3 | Cover schema rejects, offrecord exclusion, cost ceiling failure, advisory labeling, idempotency, admin auth, and optional consumer flags. | T8-01..T8-08 |

Ticket acceptance highlights:

- T8-01 rejects non-advisory observations at schema level.
- T8-03 proves forbidden content never reaches `llm_gateway`.
- T8-04/T8-05 prove no uncited observation can be stored.
- T8-07/T8-08 prove observations are hints, not answers or published facts.
- T8-09 proves cost ceiling failures produce failed runs and zero observations.

---

## §8. Stop Signals

A stream must stop and surface the issue before proceeding if any of these fire:

- Observations are posted as canonical without `policy='advisory'` marker → STOP, invariant #5
  breach.
- Reflection run consumes `#offrecord`, `#nomem`, redacted, or forgotten content → STOP,
  invariant #3 breach.
- Any code path calls an LLM outside `llm_gateway` → STOP, invariant #2 breach.
- LLM cost ceiling would be exceeded → record `reflection_runs.status='failed'`, set
  `error_text`, write no observations.
- Observation kind is not in the CHECK list → schema-level reject.
- Observation lacks citations → reject before insert.
- Observation tries to write `policy` other than `advisory` → schema-level reject.
- `/recall` or digest treats observation text as canonical source → STOP, invariant #5 breach.
- Any graph table, graph sync, or graph projection is introduced → STOP, invariant #6 breach.
- Any wiki or public surface is introduced → STOP, Phase 9 boundary breach.
- ROADMAP / AUTHORIZED_SCOPE changes and removes or narrows Phase 8 authorization → STOP.

---

## §9. PR Workflow

Use the Phase 4-style stream PR queue, adapted for Phase 8 authorization gates.

Pattern:

1. Keep one ticket per PR where possible.
2. Maintain a `sprint_pr_queue` with ticket ID, stream, branch, PR, migration number, deps, CI
   status, review status, and merge state.
3. Queue Wave 1 PRs first, but merge schema before service code.
4. Hold any PR with a migration until migration numbering is coordinated.
5. Every PR lists changed files, tests run, risks, and explicit invariant checks.
6. No PR merges without CI green.
7. No admin merge.
8. Merge with rebase and delete branch.
9. Update implementation status only after merge.
10. Run Final Holistic Review before declaring Phase 8 closed because this phase includes LLM
    calls, derived memory, admin commands, and downstream consumers.

Draft queue shape:

| Queue | Ticket | Stream | Expected PR | Merge order |
|---|---|---|---|---|
| 1 | T8-01 | A | schema | first |
| 2 | T8-02 | A | repos/models | after T8-01 |
| 3 | T8-03 | B | runner | after T8-02 |
| 4 | T8-04 | C | prompts | parallel with T8-03, merge before T8-05 |
| 5 | T8-05 | B+C | validator | after T8-03/T8-04 |
| 6 | T8-06 | D | admin commands | after Wave 1 |
| 7 | T8-07 | E | recall hints | after T8-06 |
| 8 | T8-08 | E | digest section | after T8-06 |
| 9 | T8-09 | QA | tests/evals | after implementation PRs, can start fixtures earlier |

---

## §10. Glossary

- **reflection_run:** one bounded execution of reflection over a time window and scope. It has
  status, cost ledger linkage, observation count, and failure text.
- **observation:** a derived, advisory, sourced interpretation. It has citations, confidence,
  tags, and `policy='advisory'`. It is not canonical truth.
- **memory_event:** append-only audit event for reflection lifecycle and observation usage.
- **advisory policy:** mandatory marker stating that derived observation text is a hint, not a
  source of truth, summary truth, card truth, or wiki truth.
- **confidence_score:** bounded 0.00-1.00 model-reported confidence, useful for sorting and
  review priority but not independently trustworthy.
- **cited_message_version_ids:** JSON list of message version IDs that support the observation.
- **cited_card_ids:** JSON list of approved card IDs that support the observation.
- **reflection_runner:** scheduler-triggered service that gathers governed evidence and creates
  advisory observations through `llm_gateway`.
- **digest_runner:** separate service that creates editorial digest drafts; not the same cadence,
  scope, or output as reflection.

---

## Open Design Questions for Ratification

1. Observations vs cards: when does an observation graduate to a card? Options include manual
   admin action only, or automatic candidate creation after N citations.
2. Reflection cadence: daily, weekly, on-demand only, or a hybrid where scheduler runs are
   disabled until manual confidence is built?
3. Confidence score calibration: how do we trust or normalize the LLM's self-reported confidence?
   Should confidence be treated only as sorting metadata?
4. Observation expiry: do observations TTL out via `expires_at`, or stay forever marked advisory
   until explicitly expired?
5. Cross-chat reflection: single chat scope only, or multi-chat windows once visibility and
   governance filters are proven?
6. Phase placement: should reflection runs remain canonical Phase 5 per HANDOFF, or is Phase 8
   being intentionally re-scoped to add a reflection layer after Phase 7?
7. Consumer policy: should `/recall` use observations at all, or should observations be limited
   to admin/digest preparation until evals prove no canonicalization leakage?
8. Event replay: which `memory_events.payload` fields are stable enough for future
   butler-context replay without overcommitting to a butler implementation now?
