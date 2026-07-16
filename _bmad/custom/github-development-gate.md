# Mandatory GitHub Development Gate

Before creating a branch or worktree, editing implementation files, writing code, or applying
review fixes:

1. Identify the GitHub Issue that authorizes the work.
2. Verify it is open with `gh issue view <number> --json number,state,title,body,url`.
3. Confirm the issue contains the problem, scope, acceptance criteria, validation plan, and
   dependencies. Refine the issue before implementation if any are missing.
4. Keep the issue number in the active workflow context and use it in the branch, commits,
   and PR closing reference.

Do not read or write `sprint-status.yaml`; GitHub Issues is the status system. Local BMAD story
or implementation files are supporting artifacts only and must link back to the issue.

If no issue exists yet, planning may continue only until the issue is created and verified.
No implementation edit may happen before that verification.
