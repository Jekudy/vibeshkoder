# Eval Results JSONL Schema

`eval_results.jsonl` is the machine-readable Phase 11 verdict artifact produced by
`.github/workflows/evals.yml`. The file uses JSON Lines: one complete JSON object per line,
with a final summary object as the last line.

`PHASE11_PLAN.md §8.1` defines Orch A as the consumer of this artifact for the Phase 5 closure
verdict. Automation must read this file directly; PR comments are only a human mirror of the same
verdict.

## Per-case line

Each non-summary line records one evaluated case.

| Field | Type | Required | Contract |
|-------|------|----------|----------|
| `verdict` | string | yes | One of `PASS` or `FAIL`. |
| `category` | string | yes | One of `leakage`, `citations`, `refusal`, `recall_precision`, `determinism`, `no_llm_imports`. |
| `case_id` | string | yes | Stable case identifier, for example `L1`, `L2`, `C1`, `R1`, `I1`, `I2`, `I3`, `D1`, `M_recall@3`, or `M_precision@5`. |
| `seed_hash` | string | yes | SHA-256 hex digest of the seed bundle used for this run. |
| `harness_version` | string | yes | Git SHA of the harness at run time, equivalent to `git rev-parse HEAD`. |
| `evidence` | object | yes | Arbitrary JSON object with assertion details, expected and actual values, or `file:line` data for import-graph cases. |

`evidence` must stay JSON-serializable and must not require consumers to parse prose. Prefer
explicit keys such as `expected`, `actual`, `line`, `path`, `metric`, `threshold`, and `details`.

## Final summary line

The last line must be exactly one summary object.

| Field | Type | Required | Contract |
|-------|------|----------|----------|
| `summary` | boolean | yes | Always `true`. Distinguishes the final line from per-case lines. |
| `total_cases` | integer | yes | Count of per-case lines written before the summary. |
| `passed` | integer | yes | Count of per-case lines with `verdict == "PASS"`. |
| `failed` | integer | yes | Count of per-case lines with `verdict == "FAIL"`. |
| `seed_hash` | string | yes | SHA-256 hex digest of the seed bundle used for the run. |
| `harness_version` | string | yes | Git SHA of the harness at run time. |
| `wall_clock_seconds` | number | yes | End-to-end harness wall-clock runtime in seconds. |

## Example

```jsonl
{"verdict":"PASS","category":"leakage","case_id":"L1","seed_hash":"a75f6f9c9f0f4d8e7e5e7be6f2e35fb5b7b7d7d15f62b23c8c7b2f4f67c4d2d1","harness_version":"970842f4acfaa7646da12fa4c7b29820e941749b","evidence":{"returned_message_version_ids":[],"blocked_message_version_ids":[101]}}
{"verdict":"FAIL","category":"citations","case_id":"C1","seed_hash":"a75f6f9c9f0f4d8e7e5e7be6f2e35fb5b7b7d7d15f62b23c8c7b2f4f67c4d2d1","harness_version":"970842f4acfaa7646da12fa4c7b29820e941749b","evidence":{"expected":"positive message_version_id","actual":null,"path":"tests/evals/test_citations.py","line":42}}
{"verdict":"PASS","category":"recall_precision","case_id":"M_recall@3","seed_hash":"a75f6f9c9f0f4d8e7e5e7be6f2e35fb5b7b7d7d15f62b23c8c7b2f4f67c4d2d1","harness_version":"970842f4acfaa7646da12fa4c7b29820e941749b","evidence":{"metric":1.0,"threshold":0.8,"k":3}}
{"summary":true,"total_cases":3,"passed":2,"failed":1,"seed_hash":"a75f6f9c9f0f4d8e7e5e7be6f2e35fb5b7b7d7d15f62b23c8c7b2f4f67c4d2d1","harness_version":"970842f4acfaa7646da12fa4c7b29820e941749b","wall_clock_seconds":12.48}
```
