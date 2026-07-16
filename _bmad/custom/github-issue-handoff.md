# Mandatory GitHub Issue Handoff

A BMAD workflow is not complete until its outcome is recorded in GitHub Issues.

1. Identify the existing issue for this work. If a number was supplied, verify it with
   `gh issue view <number> --json number,state,title,url`.
2. If no suitable issue exists, search for duplicates and create one using the repository's
   Discovery/RFC or Implementation Work issue contract.
3. Create or update the issue with the workflow outcome, decisions, artifact paths, scope,
   acceptance criteria, validation evidence, dependencies, and unresolved questions.
4. For implementation work, require the issue to remain open until its acceptance criteria
   are achieved. Link the branch, PR, or commit when available.
5. Verify the final issue with `gh issue view` and return its URL in the workflow result.

If GitHub access or authentication fails, report the workflow as incomplete and stop. Never
substitute a local TODO, sprint-status file, Discussion, Notion, or Linear item.
