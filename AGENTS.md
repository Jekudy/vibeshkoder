<!-- Root: ~/Vibe/AGENTS.md — ALWAYS read it first for vault-wide rules and structure -->

# AGENTS.md

## What

Vibe Gatekeeper is a Telegram + web gatekeeping system for managing community applications, vouching, intro refresh, and admin/member visibility.

## Runtime Standard

- Source of truth is GitHub, not the VPS.
- Production deploys from pre-built GHCR images.
- Coolify is the target runtime manager for product apps.
- Host-level operator services stay outside Coolify if they need direct VPS control.

## Environments

- Local development uses `DEV_MODE=true`.
- Staging and production must use separate bot tokens and isolated data stores.
- Secrets never belong in git.

## GitHub Issue Workflow

- GitHub Issues is the only canonical tracker for scope, backlog, and status. Do not
  create or mirror project work in Discussions, Notion, Linear, or local backlog files.
- Every research, brainstorming, design, architecture, planning, PRD, retrospective,
  or course-correction session is complete only after it creates or updates a GitHub
  Issue with the outcome and links any supporting artifacts.
- Before creating a branch or worktree, editing implementation files, or writing code,
  verify that an open issue exists with problem, scope, acceptance criteria, and known
  dependencies. Use `gh issue view <number>` when the issue is supplied by another tool.
- Branches, commits, and PRs must reference the issue. PR bodies must contain a GitHub
  closing keyword such as `Closes #123`.
- BMAD documents, ADRs, plans, charters, and status files are supporting artifacts.
  They never replace the GitHub Issue that authorizes and tracks the work.

## BMAD

- BMAD BMM is installed under `_bmad/`; generated skills live in `.agents/skills/`
  and `.claude/skills/`.
- Start an unfamiliar workflow with `bmad-help` in a fresh task. Use `bmad-quick-dev`
  for small changes and the full analysis/planning flow for major changes.
- Read `_bmad-output/project-context.md` before running a BMAD workflow.

## Current Migration Rule

- Coolify is the production runtime for bot and web deploys.
- Legacy `/home/claw/vibe-gatekeeper` is retained only as rollback fallback until
  `scripts/cleanup-legacy.sh` passes its A3, soak window, and disk preflights.
