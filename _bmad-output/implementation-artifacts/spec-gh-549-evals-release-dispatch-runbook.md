---
title: 'gh-549: runnable release-bound Evals dispatch in semantic-qa runbook'
type: 'bugfix'
created: '2026-09-30'
status: 'done'
baseline_revision: 'dfce40aa9905705beaa17c117a1969e2444aa994'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** After #548 the nightly Evals path is green and a manual dispatch fails closed, but the release-bound validation procedure in `docs/runbooks/semantic-qa.md` §6 cannot be executed: `gh workflow run evals.yml --ref "$RELEASE_GIT_SHA"` returns `HTTP 422: No ref found` (workflow_dispatch accepts only a branch or tag), and `gh run watch --exit-status` without a run id fails non-interactively (GitHub issue #549).

**Approach:** Docs-only fix: dispatch on a temporary lightweight tag `semantic-eval-${RELEASE_GIT_SHA}` pushed at the release SHA, watch the exact run id, delete the tag afterwards. Use `${VAR}` expansion so the snippet works in bash and zsh.

## Boundaries & Constraints

**Always:** `GITHUB_SHA` of the dispatched run equals `RELEASE_GIT_SHA` (binding preserved); validate-report stays fail-closed; tag is lightweight (`git push origin "<sha>:refs/tags/<name>"`, never `git tag -a`); the tag name carries a non-hex prefix.

**Never:** change `.github/workflows/evals.yml` or any code; add a `release_sha` dispatch input (makes the bound SHA dispatcher-asserted); recommend `--ref main` (races with moving main); add a docs-grep test; touch unrelated runbook sections.

</intent-contract>

## Code Map

- `docs/runbooks/semantic-qa.md:362-372` -- §6 "Передать exact sanitized report…" paragraph + bash block; the only lines to change. `RELEASE_GIT_SHA` is defined earlier in the same section (`:321`).
- `.github/workflows/evals.yml:104-112` -- read-only: validate step gated `if: github.event_name == 'workflow_dispatch'`, binds `--expected-release-sha "$GITHUB_SHA"`.
- `.github/workflows/ci.yml:3-8` -- read-only evidence: CI triggers on branch push (main/master) and pull_request only, so a tag push starts no workflow; `release.yml` is `workflow_run` on CI with `head_branch == 'main'`.
- `tests/scripts/test_evaluate_semantic_qa.py:225-246` -- read-only: existing binding test pinning the workflow gate; no change.

## Tasks & Acceptance

**Execution:**
- `docs/runbooks/semantic-qa.md` -- replace the §6 dispatch bash block with: set `TAG="semantic-eval-${RELEASE_GIT_SHA}"`; `gh secret set …`; `git push origin "${RELEASE_GIT_SHA}:refs/tags/${TAG}"`; `gh workflow run evals.yml --ref "${TAG}"`; short `sleep`; `RUN_ID=$(gh run list --workflow evals.yml --branch "${TAG}" --limit 1 --json databaseId --jq '.[0].databaseId')`; `gh run watch "${RUN_ID}" --exit-status`; `git push origin --delete "refs/tags/${TAG}"`. Add one Russian sentence before the block: workflow_dispatch accepts only branch/tag, so a temporary lightweight tag at the release commit is used and deleted after the run; release commit must be fetched locally. -- makes the documented procedure executable.

**Acceptance Criteria:**
- Given a fetched release commit, when the operator runs the §6 block in bash or zsh, then Evals starts with `event=workflow_dispatch` and `headSha == RELEASE_GIT_SHA`.
- Given the dispatched run, when `gh run watch "${RUN_ID}" --exit-status` runs non-interactively, then it waits for that exact run and returns non-zero if validate-report fails.
- Given the run finished, when the last command runs, then `refs/tags/semantic-eval-<sha>` no longer exists on origin.
- Given no `SEMANTIC_EVAL_REPORT_JSON` secret, when dispatched this way, then validate-report fails with exit 2 (fail-closed unchanged).

## Spec Change Log

## Review Triage Log

### 2026-09-30 — Review pass
- verdicts: 18 findings — high 0, medium 4, low 9, false 5, maybe-false 0
- findings:
  - `[medium]` `[patch]` blind: `sleep 10` + `--limit 1` may watch a previous run for the same tag on re-validation — replaced by `RUN_URL=$(gh workflow run …)` (gh 2.102 returns the created run URL) + `gh run watch "${RUN_URL##*/}"`; verified run 36705450962.
  - `[low]` `[patch]` blind: tag deletion not guaranteed on interrupt — one prose sentence tells the operator to delete the tag with the last command; no trap (leftover tag triggers no workflow).
  - `[false]` `[reject]` blind: green exit-status without a validate step for pre-#411 SHAs — such SHAs have no semantic-eval runner, so no report can exist for them; every evals.yml since #411 has validate-report.
  - `[low]` `[patch]` blind: `git push <sha>:refs/tags` can upload an arbitrary local commit — tag now created via `gh api POST git/refs` (server-side commit only).
  - `[low]` `[patch]` blind: local fetch requirement removable via API — same patch; clause dropped.
  - `[low]` `[reject]` blind: tag may already exist after an interrupted run — API POST fails loudly (422 Reference already exists); the prose instructs deletion; adding a pre-check is a guard for a rare path.
  - `[false]` `[reject]` blind: spec verification does not exercise the block / status stale — fix would edit this build's spec; block verified e2e by run 36705450962; status/logs are populated by this step.
  - `[low]` `[patch]` blind: two consecutive paragraphs end with a colon — merged into one paragraph.
  - `[medium]` `[patch]` edge: stale/null RUN_ID — same root cause and patch as the first row.
  - `[low]` `[patch]` edge: empty RELEASE_GIT_SHA turns the git refspec into a remote delete — git push removed; API POST with empty sha fails 422.
  - `[low]` `[reject]` edge: no errexit/`&&` — `set -e` in an interactive shell kills the session; any failed step fails loudly and validate stays SHA-bound fail-closed.
  - `[low]` `[patch]` edge: trap for tag deletion — same as the deletion row (prose, no trap).
  - `[medium]` `[patch]` edge claim: "waits for that exact run" — same patch as the first row.
  - `[low]` `[patch]` edge claim: tag deletion — same as the deletion row.
  - `[medium]` `[patch]` verification-gap other finding: stale run on re-validation — same patch as the first row.
  - `[false]` `[reject]` intent: diff edits docs, not workflow — reading B (confirmed gap in the documented manual contract); workflow verified unchanged and correct by runs 36697378870 / 36704461337.
  - `[false]` `[reject]` intent: PR closes #549 not #545 — #545 already closed by #548; the new gap has its own issue.
  - `[false]` `[reject]` intent: foreign `_bmad/scripts` changes — BMAD setup noise, excluded by explicit staging.

## Verification

**Commands:**
- `pytest tests/scripts/test_evaluate_semantic_qa.py -q` -- expected: all pass (workflow binding unchanged).
- `git diff --stat origin/main -- .github scripts bot tests` -- expected: empty (docs-only).

**Manual checks (if no CLI):**
- Empirical evidence already gathered on 2026-09-30 with the exact commands: raw-SHA dispatch → HTTP 422; tag `semantic-eval-dfce40a…` dispatch → run 36704860833, `workflow_dispatch`, headSha `dfce40aa9905705beaa17c117a1969e2444aa994`, validate exit 2, artifact uploaded; `gh run list --branch <tag>` returned that run id; `git push origin --delete refs/tags/<tag>` removed the tag (0 remote tags). Inspect the diff to confirm the block matches those commands verbatim (with `${VAR}` braces).

## Auto Run Result

- Summary: `docs/runbooks/semantic-qa.md` §6 release-bound Evals dispatch is now executable: temporary lightweight tag via GitHub API, dispatch on the tag, watch the exact run URL, delete the tag.
- Files: `docs/runbooks/semantic-qa.md` (dispatch block + one paragraph); this spec.
- Review: 11 patch rows (4 root causes: run selection, tag creation, tag cleanup prose, paragraph colon) applied, 0 deferred, 7 rejected with reasons above.
- Follow-up review recommendation: false (no `high` patched; medium rows share one root cause).
- Verification: `pytest tests/scripts/test_evaluate_semantic_qa.py` 11 passed; docs-only (`git diff --stat` on .github/scripts/bot/tests empty); exact block run in zsh → run 36705450962 `workflow_dispatch`, headSha `dfce40aa9905705beaa17c117a1969e2444aa994`, validate exit 2, `gh run watch` exit 1, remote tags 0.
- Residual risks: tag left behind if the operator interrupts and skips the last command (harmless, blocks the next POST loudly); `gh workflow run` must return the run URL (gh ≥ 2.102 verified).
