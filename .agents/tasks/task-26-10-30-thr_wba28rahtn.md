# Fix Trivy advisories and merge #556

## Goal
Main passes Trivy; accepted PR #556 is merged and both releases deploy successfully.

## DoD
Issue #557 contains the full user-approved criteria. List 12 advisories and fixed versions; update only affected manifests/locks; pass required checks; receive independent Fable result review; merge fix then #556; verify both deployments and issue #20 closure; commit worklog evidence.

## Todo
- [x] Exact main 936983e scan reproduces 12; #557 comment 6521870030 records inventory.
- [x] PR #558 adc88ff; CI 41050584161 all green; Fable thr_325nb9atkf ACCEPT, no blockers.
- [x] Fix b77c64d merged; main CI 41052041992 and release/deploy 41053897927 succeed.
- [x] #556 updated to 77dc059, CI 41052107561 green; merged 5f08911; #20 CLOSED. Main CI 41054369914 and release/deploy 41055874198 succeed.
- [ ] Commit worklog outcomes and evidence.

## Recovery
Owner: thr_wba28rahtn, Shkoder. Stage: Do. User task accepted as written; merge/deploy authorization from thr_687fnp3zxf on 2026-10-30. Three fix iterations maximum. Stop on unfixed advisory or failed deploy; no rollback. Main checkout has foreign changes; preserve them. Next: commit/push final records through protected-main documentation PR. Both application merges and deployments complete. PR #558 adc88ff; local Trivy zero, focused tests 6 pass, lock/npm checks pass. Fix iteration 1. GitHub #557 is canonical tracker.
