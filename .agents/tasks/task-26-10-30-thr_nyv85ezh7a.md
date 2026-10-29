# Issue #20: distinguish forward lookup denial logs

## Goal
Make the two denial reasons visible in audit logs without changing forward lookup behavior.
Scope: https://github.com/Jekudy/vibeshkoder/issues/20 only.

## DoD
- A missing requester logs `forward_lookup denied: user_id=%s not found in DB` at INFO.
- An existing unauthorized requester logs `forward_lookup denied: user_id=%s is not a member` at INFO.
- Both logs contain only the requester ID as variable data. Denied requests still send no reply and perform no message or intro lookup.
- Member and admin access remains unchanged. Focused regression tests pass; baseline failures and skips are reported separately.

## Todo
- [x] Confirm the open issue, current source, tests and competing ownership. Evidence: issue API, origin/main `936983ec65ecfa75ff3ef201fb4cfae4aea70e8f`, PR search and issue timeline, unarchived BB thread list.
- [x] Astra approved plan v1 in parent message from `thr_2d5np6zcbg`. Both denial paths must remain silent with no downstream lookup; preserve member/admin cases. Isolate dependencies; report optional tool availability without adding blockers.
- [x] Devin completed as `thr_hkfj8wduxb`, head `0eebfd78334c5236024978e97201cbba5f0a6e02`. Launch used Devin `swe-2-high/max` on Mac `host_auv4agxadi`, new worktree from `origin/main`, branch `fix/20-forward-lookup-denial-logs`. Change only two strings in `bot/handlers/forward_lookup.py`. Extend `tests/test_forward_lookup.py` with compact caplog coverage of both denials, exact INFO records, ID-only arguments, and existing no-lookup/no-reply assertions. Reuse existing fixtures and mocks.
- [x] Devin ran focused tests before and after changes: `python -m pytest tests/test_forward_lookup.py tests/test_html_escape.py -q`; run `ruff check bot/handlers/forward_lookup.py tests/test_forward_lookup.py`, privacy lint and ponytail-review. Show new log assertions fail on the original strings. Record commands, counts, baseline errors and skips. No new dependencies or unrelated fixes.
- [x] Sol accepts DoD at head `0eebfd78334c5236024978e97201cbba5f0a6e02`: only two production strings changed. Independent focused and safety tests: 10 passed, 2 skipped in 2.50s; Ruff, format, privacy lint and diff check pass. PR https://github.com/Jekudy/vibeshkoder/pull/556 remains open with auto-merge null; issue open. Executor stopped and archived. Next gate: Astra final critique; CI is not all green.

## Rollback
Before merge, revise or close the PR. No runtime or data changes are authorized. Never push this task directly to remote main.

## Budget
Accepted: 45 minutes of active work, excluding review waits; at most three fix iterations. Fixed team: Sol plans/accepts, Devin executes, parent Astra critiques. No extra team.

## Recovery
- Owner: `thr_nyv85ezh7a`; parent: `thr_2d5np6zcbg`; project: Shkoder. Stage: Sol accepted; Astra final critique pending. Plan v1 approved by Astra parent message.
- Permission source: user assignment in this thread. Code PR authorized after plan review. No merge/auto-merge, deployment, live migrations, credentials, deletion, spending or messages to people.
- Shared main is `dfce40a`, behind fetched origin/main, with unrelated AGENTS/BMAD edits and deleted CLAUDE.md. Preserve all foreign changes. Do not pull, reset or stage foreign changes. Parent permits staging only this contract and owned log entries when safe; do not push remote main. Only this task and its worklog entries may enter a local records commit. Never push main; production auto-deploys.
- Baseline: `.venv/bin/python -m pytest tests/test_forward_lookup.py tests/test_html_escape.py -q` returned 6 failures in 4.19s. All fail at import because the shared environment lacks declared dependency `pgvector`. Relevant source and test files match fetched origin/main. Executor must establish a clean baseline in its isolated environment before changes; do not change dependency manifests for this setup failure. Existing tests live at `tests/test_forward_lookup.py`, not the issue's suggested `tests/handlers/` path.
- Read-only `bmad-loop validate --project <project> --json` failed: missing sprint status, dirty worktree, missing Claude hooks. No setup or loop run is proposed for this fixed-team assignment.
- Executor `thr_hkfj8wduxb` launched with Devin swe-2-high/max, host_auv4agxadi, fresh worktree from refs/remotes/origin/main. Assignment: /tmp/shkoder-issue20-devin.txt. Result: PR #556, 2 files +43/-3; exact branch base matches `936983ec65ecfa75ff3ef201fb4cfae4aea70e8f`. Devin reports isolated baseline 6 passed, regression red 2 failed/2 passed, green 7 passed. Sol reran all three focused/safety files: 10 passed/2 skipped.
- CI: Trivy fails on unchanged dependency vulnerabilities (job 123713491017, run 40949483867); full CI still running when checked. No dependency changes made or scope expansion.
- Next action: Astra critiques accepted PR #556 at frozen head; no merge authority granted.
