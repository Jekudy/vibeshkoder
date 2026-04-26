# Memory System — 12-Phase Roadmap (condensed)

Full architect text in `HANDOFF.md`. This file is the at-a-glance map.

## Strategy

```
phase 0 safety
  → feature_flags
    → ingestion_runs + telegram_updates
      → extend chat_messages
        → message_versions
          → #nomem/#offrecord + offrecord_marks
            → forget_events / tombstones
              → import apply
                → fts / evidence
                  → q&a with citations
                    → llm_gateway / ledger
                      → extraction / events / observations / candidates
                        → cards / admin review
                          → summaries / digests / wiki
```

## Phases

| # | Name                                       | Authorized? | Exit gate                                                                 |
|---|--------------------------------------------|-------------|---------------------------------------------------------------------------|
| 0 | Gatekeeper stabilization                   | YES (now)   | privacy fix, idempotent save, upsert contained, /healthz, regression tests green |
| 1 | Source of truth + raw archive              | YES (now)   | live message produces raw update + normalized message + v1 version, edits create v2 |
| 2a| Telegram Desktop import — dry-run          | STRETCH     | dry-run reports stats; **no content writes**                              |
| 2b| Telegram Desktop import — apply            | NO          | requires Phase 3 skeleton (tombstones + policy detection)                 |
| 3 | Governance (`#nomem` / `#offrecord` / `/forget` / tombstones) | NO  | forbidden content excluded from future search/extraction/import           |
| 4 | Hybrid search + Q&A with citations         | NO          | bot answers from evidence only or refuses; no LLM general knowledge       |
| 5 | LLM gateway + extraction (events / observations / candidates) | NO  | every LLM call logged in ledger; no forbidden source sent to LLM          |
| 6 | Knowledge cards + admin review             | NO          | active card requires source + admin approval                              |
| 7 | Daily summaries                            | NO          | every bullet has source; forgotten source redacts bullet                  |
| 8 | Weekly digest                              | NO          | reviewed sourced sections; no auto-publish                                |
| 9 | Wiki (member / internal)                   | NO          | visibility filter + governance + source trace; public stays disabled      |
| 10| Graph projection (Neo4j / Graphiti)        | NO          | derived only; rebuildable from postgres; forget purges graph              |
| 11| Shkoderbench / evals                       | NO          | leakage / citation / no-answer tests in CI nightly                        |
| 12| Future butler — design-only                | NO          | docs only; no execution code                                              |

## Phase gates (must be true to advance)

| Gate                  | Conditions                                                                              |
|-----------------------|-----------------------------------------------------------------------------------------|
| Gatekeeper safety     | privacy fix, idempotent save, dialect-safe repos, regression tests green                |
| Source of truth       | raw_updates + message_versions + basic normalization persist                            |
| Governance            | `#nomem` / `#offrecord` detection, `forget_events`, cascade skeleton, filters           |
| Q&A                   | FTS, evidence bundle, citations, refusal, policy filters                                |
| Extraction            | `llm_gateway`, ledger, source validation, budget guard                                  |
| Catalog               | cards require sources + admin review                                                    |
| Wiki                  | visibility + review + source trace + forget purge proven                                |

## Non-negotiable invariants (verbatim from HANDOFF.md §1)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction/search/qa over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; uses governance-filtered evidence.
8. Import apply must go through same normalization/governance path as live updates.
9. Tombstones are durable; not casually rolled back.
10. Public wiki remains disabled until review/source trace/governance proven.

## Parallelization

After Phase 0:
- DB migration drafting | tests/fixtures | admin health/read-only screens | import dry-run
  parser | docs implementation status | q&a eval case design — can run in parallel.

Cannot parallelize without gate:
- import **apply** before tombstones
- `edited_message` before `message_versions` + handler
- reactions before reactions table + handler
- LLM extraction before `llm_gateway` + governance
- wiki before review + source trace
