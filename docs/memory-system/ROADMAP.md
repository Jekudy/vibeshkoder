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
| 2a| Telegram Desktop import — dry-run          | Authorized: DONE 2026-04-29 | dry-run reports stats; **no content writes**                              |
| 2b| Telegram Desktop import — apply            | Authorized: DONE 2026-04-29 | gate passed: tombstones + policy detection + apply + rollback net (#104) + Final Holistic Review hotfix (PR #143) |
| 3 | Governance (`#nomem` / `#offrecord` / `/forget` / tombstones) | SKELETON DONE 2026-04-29 (T3-01..T3-05 merged in Phase 2 wave Charlie) | forbidden content excluded from future search/extraction/import           |
| 4 | Hybrid search + Q&A with citations         | NO          | bot answers from evidence only or refuses; no LLM general knowledge       |
| 5 | LLM gateway + answer synthesis (synthesis-first slice)        | **DONE 2026-05-11** (Orch A; canonical plan: `PHASE5_PLAN.md`; closure PR pending) | every LLM call logged in ledger; no forbidden source sent to LLM; flag-OFF Phase 4 byte-for-byte preserved (T5-W0-01..T5-05 merged; FHR ACCEPTED; extraction tables deferred to Phase 8 per §2 ratification) |
| 6 | Knowledge cards + admin review             | **CLOSED 2026-05-12; Wave 2 closure 2026-05-13; T6-08 retro-shipped 2026-05-13** | All 10 tickets merged: T6-00..T6-03 (Wave 1) + T6-04/T6-05/T6-09 (Stream C) + T6-06/T6-07 (Stream D) + T6-08 (web cards page) retroactively via PR #281 2026-05-13. Migrations 030–035. Advisory lock H-Cdx-2 race window closed (3-layer defense). FHR APPROVE. Carryover #260 (PR #266), #261 (PR #265), #262 trivial (PR #266) resolved 2026-05-13. T6-09 e2e (PR #267) + design docs (PR #264) + Codex follow-ups (chore/p6-wave2-closure) also 2026-05-13. MED items in #262 remain open. |
| 7 | Daily summaries                            | **CLOSED 2026-05-15** | 8/8 tickets merged (T7-S0..T7-08): migration 037, digest_context, run_digest + synthesize_digest gateway, scheduler + reaper, publisher + renderer + redactor + forget cascade `digests` layer, admin handlers, Phase 11 binding 34/34. Flag `memory.digests.daily.enabled` default OFF. Phase 7.5 carryovers: #291 shared predicate refactor, #295 T7-02 MED items. Rollout playbook: `PHASE7_ROLLOUT.md`. |
| 8 | Weekly digest                              | **CLOSED 2026-05-15** | 8/8 tickets merged (T8-S0..T8-08): migration 038 (CHECK widening + review cols + downgrade guard), `run_digest` weekly + `synthesize_digest` weekly + weekly prompt template, `build_digest_context` 7-day ISO Mon..Mon, review state machine (`digest_review.py` — `transition_to_awaiting_review` / `approve_digest` / `reject_digest`) + cascade/redactor/publisher widening, scheduler `digest_weekly_job` (Mon 09:15 MSK) + `digest_stale_review_reaper_job` (48h DM + 7d auto-reject), admin handlers `/digest_now weekly` + `/digest_review` + `/digest_approve` + `/digest_reject`, Phase 11 binding 30 → 42/42. Flag `memory.digests.weekly.enabled` default OFF. Phase 8.5 carryovers: §5.I renderer polish, M6 GIN index, #291 shared predicate refactor, R5.a/b handler-layer tightening. Rollout playbook: `PHASE8_ROLLOUT.md`. |
| 9 | Wiki (member / internal)                   | **CLOSED 2026-05-19** | 8/8 sprints merged (T9-01 schema PR #314 migrations 050-054, T9-02 governance PR #316, T9-03 auth role split PR #317 — BLOCKER C closed, T9-04 renderer + bleach PR #318, T9-05 member router + Jinja + /robots.txt PR #319, T9-06 admin /wiki_publish/_unpublish/_robots PR #320, T9-07 forget cascade + advisory lock PR #321 — 4 Codex security fixes, T9-08 Phase 11 binding 30 tests / 18 AC PR #322 — 5 Codex PAR fixes) + FHR closure PR (CRITICAL cascade audit mask + HIGH-1 member login flow + HIGH-2 legacy_cookie_grace migration 055). Phase 11 binding **60/60**. Flag `memory.wiki.enabled` default OFF. Two-password split `WEB_ADMIN_PASSWORD` + `WEB_MEMBER_PASSWORD`. Phase 9.5 carryovers: FK action mismatch (Codex MED #3), `_cascade_wiki_revisions` idempotency (Codex LOW #4), stale-page member 410 (Claude MED-4), missing-password warning (Claude MED-5), Cache-Control on member route (Claude MED-6), L9a OR-assertion polish (Claude product r1 MED). Rollout playbook: `PHASE9_ROLLOUT.md`. |
| 10| Graph projection (Neo4j / Graphiti)        | **CLOSED 2026-05-21 — all 10 sprints merged: W0-A foundation, W0-D Neo4j CI, T10-02-rest migrations 061-062, T10-03 LLM extract + migration 064, T10-04 projector 4 modes, T10-05 graph_query read-only API + migration 066, T10-06 cascade + purge worker + readblock + migrations 063+065, T10-07 admin handlers + scheduler, T10-08 drift detection + reconcile_counts, T10-09 Phase 11 binding suite 60→77. Flags: memory.graph.projection.enabled / memory.graph.query.enabled / memory.graph.write_pending.paused (all default OFF). Cost ceiling $2/day. Rollout playbook: PHASE10_ROLLOUT.md.** | 9 sprints T10-01..T10-09; Neo4j 5.x via async cascade worker per RFC-001:415; replay-only full rebuild; 3 feature flags default OFF; ~15-16 new Phase 11 binding tests. |
| 11| Shkoderbench / evals                       | **DONE 2026-05-11** + follow-ups all closed 2026-05-12 | leakage / citation / no-answer / no-LLM-imports tests in CI nightly (`evals.yml` + `lint-privacy.yml`). Follow-ups: #224 High #5 (PR #243), #224 Critical #4 (PR #247), #224 High #1-#4 (already on main), #219 seed_v1 quality (PR #253), #255 message-branch tombstone (PR #257). |
| 12| Butler / action execution                  | **CLOSED 2026-05-30 — all 10 sprints merged (Sprint 0 plan refresh + T12-01..T12-10). Flags: memory.butler.enabled / memory.butler.{recall_evidence,schedule_meeting,send_intro,update_intro,suggest_card}.enabled / memory.butler.undo.enabled (all default OFF). Migrations 070–078. Phase 11 binding 77→102. FHR APPROVE. Rollout playbook: PHASE12_ROLLOUT.md.** | 10 sprints T12-01..T12-10; DM-only baseline; 5 tool types; cross-user consent UNBYPASSABLE; undo with LIFO + TTL; forget cascade covers all Butler audit tables; 7 feature flags all default OFF. |

## Phase gates (must be true to advance)

| Gate                  | Conditions                                                                              |
|-----------------------|-----------------------------------------------------------------------------------------|
| Gatekeeper safety     | privacy fix, idempotent save, dialect-safe repos, regression tests green                |
| Source of truth       | raw_updates + message_versions + basic normalization persist                            |
| Governance            | `#nomem` / `#offrecord` detection, `forget_events`, cascade skeleton, filters           |
| Governance skeleton (T3-01..T3-05) | Merged in Phase 2 wave Charlie 2026-04-29; full Phase 3 governance criteria are met for the skeleton scope. Full search/extraction filter integration lands in Phase 4+. |
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

## Next phase

Phase 12 CLOSED 2026-05-30. All 12 phases of the memory system cycle are now COMPLETE.
Phase 12.5 carryovers (redact-at-rest candidate content, downgrade guards, per-tool
service-layer defense-in-depth) are tracked in `IMPLEMENTATION_STATUS.md §Phase 12.5`.

<!-- updated-by-superflow:2026-05-30 -->
