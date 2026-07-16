# Vibe Gatekeeper — BMAD Project Context

## Project

Vibe Gatekeeper is an established Python 3.12 Telegram and web application. GitHub is
the source of truth for code and deployment. Read `AGENTS.md`, `CLAUDE.md`, `README.md`,
`SPEC.md`, and the relevant documents under `docs/` before proposing implementation.

## Mandatory GitHub Issue Contract

GitHub Issues is the only canonical ledger for project scope, backlog, ownership, and
status. GitHub Discussions, Notion, Linear, BMAD output folders, ADRs, plans, charters,
and status files must not become parallel trackers.

Every BMAD workflow that performs research, brainstorming, product discovery, design,
architecture, planning, PRD work, story creation, retrospective, or course correction
must create or update a GitHub Issue before declaring the workflow complete:

- Use a Discovery/RFC issue when the outcome is evidence, options, or a decision.
- Use an Implementation Work issue when the outcome authorizes a concrete change.
- Link every BMAD artifact or ADR from the issue.
- Record rejected or deferred outcomes in the issue instead of a local backlog.
- If GitHub access or authentication fails, stop and report the blocker. Do not silently
  substitute a local file, TODO, or another tracker.

## Development Gate

Before creating a branch or worktree, editing implementation files, or writing code:

1. An open GitHub Issue must exist.
2. The issue must state the problem, scope, acceptance criteria, validation plan, and
   known dependencies.
3. Verify a supplied issue with `gh issue view <number>`.
4. Use the issue number in the branch/worktree context, commits, and PR.
5. The PR body must contain `Closes #<number>` or an equivalent GitHub closing keyword.

BMAD artifacts improve the quality of an issue; they never authorize implementation on
their own.

## Engineering Constraints

- Keep changes small and simple; avoid speculative abstractions.
- Fail fast on invalid configuration or missing required data.
- Do not rename existing environment variables.
- Use Docker for development and deployment workflows where practical.
- Preserve privacy and governance invariants documented under `docs/memory-system/`.
- Run relevant tests and Ruff checks before opening a PR.
