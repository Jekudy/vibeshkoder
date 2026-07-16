# GitHub-Native Sprint Status

This instruction replaces BMAD's file-system sprint status workflow for this project.

1. Query GitHub Issues and linked pull requests for the requested scope.
2. Derive status only from GitHub issue state, labels, assignees, dependencies, and PR state.
3. Surface blocked work, missing acceptance criteria, stale issues, and the next issue ready
   for implementation.
4. Link every reported item to GitHub.

Do not read, create, or update `sprint-status.yaml`. After reporting, execute the mandatory
GitHub Issue handoff and exit without running the standard file-system steps.
