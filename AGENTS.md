<!-- Root: ~/Vibe/AGENTS.md — ALWAYS read it first for vault-wide rules and structure -->

# AGENTS.md

## What

Vibe Gatekeeper is a Telegram + web gatekeeping system for managing community applications, vouching, intro refresh, and admin/member visibility.

## Runtime Standard

- Source of truth is GitHub, not the VPS.
- Production uses Docker Compose at `/srv/shkoder/compose.yaml`; versioned manifests
  live in `ops/vps/`. Coolify is not required for application operation or deployment.
- GitHub Actions builds SHA-pinned GHCR images and runs `ops/compose/deploy.py` on the
  VPS runner. Failed application deployments restore previous bot/web image pins,
  never database data. Stop the old polling bot before starting its replacement.
- Bot health is private on port 3000: `/healthz` and DB-only `/healthz/db`.
  `HEALTHZ_PORT` overrides the bot port; keep the deployment checks aligned.
- Web HTTPS is routed through the independent proxy; `ops/compose/healthcheck.py`
  checks applications, DB, Telegram and public HTTPS without Coolify.
- Runtime secrets stay in private service `.env` files outside git; image pins live
  in `/srv/shkoder/.env`. Host cron/systemd operator services remain outside Compose.

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

- Issue #521 tracks the migration of Shkoder, Foodzy, Harry and shared OTP to
  independent Compose projects. Check its latest evidence before assuming a service
  has completed cutover and for verification evidence.
- Preserve old stopped containers, their configuration snapshots and existing external
  volumes until cutover and rollback checks are complete. Container/image rollback
  does not restore a database; never run old and new consumers or database containers
  against the same production data at once.
- Never use `docker compose down -v`, prune user data, remove retained volumes or
  restore database backups without a verified backup and explicit user approval.
- Legacy `/home/claw/vibe-gatekeeper` remains retained; cleanup requires its existing
  `scripts/cleanup-legacy.sh` preflights and the approval above.
