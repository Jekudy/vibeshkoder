# Vibe Gatekeeper VPS Standardization Design

## Goal

Move `vibe-gatekeeper` away from "source of truth on VPS" to the standard deployment model:

`develop anywhere -> git push -> GitHub Actions test -> build immutable Docker images -> push to GHCR -> Coolify deploys pre-built images`

The migration must preserve the current production runtime until the new path is fully ready.

## Current State

- Production is running from `/home/claw/vibe-gatekeeper` on the VPS.
- The VPS directory has a `.git` folder, but no configured `origin` remote.
- The VPS working tree contains uncommitted changes and helper scripts.
- Runtime uses Docker Compose with:
  - `bot`
  - `web`
  - `db`
  - `redis`
- Secrets and runtime files exist only on the server:
  - `.env`
  - `.env.production`
  - `credentials.json`
  - live PostgreSQL data
- A local `DEV_MODE=true` path already exists:
  - SQLite for DB
  - `MemoryStorage` instead of Redis

## Target Standard

### Source of Truth

- Source of truth is GitHub, not the VPS.
- VPS must not be used as the primary development workspace.
- Production must not depend on `git pull` or server-side code builds.

### Build and Release

- GitHub Actions runs CI checks.
- Only green commits are eligible for release images.
- Release workflow builds immutable images and pushes them to GHCR.
- Production deployments use image tags, not branch state.
- Rollback uses a previous image tag.

### Runtime Management

- Product applications are managed through Coolify.
- Coolify deploys pre-built images from GHCR.
- Product applications keep separate `staging` and `production` environments.
- Host-level operator services remain outside Coolify when they need VPS control.

## Service Classification

### Product Applications

These should move to Coolify:

- `vibe-gatekeeper`
- `foodzy`
- future app/web/bot/worker services with normal runtime boundaries

### Operator / Infra Services

These should remain host-managed unless there is a compelling reason otherwise:

- `claude-tg-watchdog.service`
- Telegram-based operator services that must control the VPS
- tools that need Docker, SSH, or arbitrary shell access
- possible `Hermes`-style orchestration services

Rationale: containerizing a host-control service usually ends with `docker.sock` exposure or other root-equivalent access. For these services, systemd plus explicit privilege boundaries is the cleaner model.

## Migration Strategy

### Wave 0: Snapshot and Recovery

- Create a private production snapshot before changing anything:
  - source archive
  - DB dump
  - runtime env files
  - credentials file
  - compose config
  - docker inspect output
- Keep this snapshot outside git.

### Wave 1: Local Repo Import

- Create a clean local project at `~/Vibe/products/vibe-gatekeeper`.
- Import the current working tree from VPS without secrets and generated files.
- Initialize a fresh git repository locally.
- Preserve the VPS snapshot separately as the rollback baseline.

### Wave 2: GitHub + GHCR

- Create a private GitHub repository.
- Push the imported codebase.
- Add CI workflow:
  - install deps
  - run tests
  - run lint
- Add release workflow:
  - build `bot` image
  - build `web` image
  - push both to GHCR
  - tag by commit SHA

### Wave 3: Environment Model

- Keep local development simple:
  - `.env` for local dev
  - `DEV_MODE=true`
  - local SQLite
  - separate dev bot token
- Introduce explicit non-local environments:
  - `staging`
  - `production`

Each environment must have isolated:

- bot token
- database
- Redis
- web password
- community/admin IDs if needed

### Wave 4: Coolify Parallel Install

- Install Coolify without touching the existing `vibe-gatekeeper` production stack.
- Investigate host port constraints before enabling the Coolify proxy, because the server already contains other services and at least one existing Caddy-based deployment.
- If direct proxy ports conflict, install Coolify first and delay public ingress changes until conflicts are resolved safely.

### Wave 5: Parallel Staging in Coolify

- Create Coolify resources for `vibe-gatekeeper-staging`.
- Deploy from GHCR images, not from git builds.
- Validate:
  - bot startup
  - web login
  - DB migrations
  - scheduler
  - Google Sheets sync path

### Wave 6: Production Cutover

- Because the bot uses Telegram polling, the same production bot token cannot be safely consumed by both old and new runtimes at once.
- The final cutover will be a controlled short stop/start window:
  - fresh DB backup
  - stop old production bot
  - start new production bot in Coolify
  - verify real message flow

This is not absolute zero-downtime, but it is the safest realistic cutover for a polling bot.

## Acceptance Criteria

- `AC1`: A private VPS snapshot exists locally with source, DB dump, env files, credentials, and runtime metadata.
- `AC2`: `vibe-gatekeeper` exists as a clean local git repo on this Mac with secrets excluded from git.
- `AC3`: A private GitHub repository exists and the local repo is pushed to it.
- `AC4`: GitHub Actions CI and GHCR release workflows exist in the repo.
- `AC5`: The repo documents the new `local/staging/production` env model and the operator-service boundary.
- `AC6`: Coolify is installed in parallel without replacing the current production `vibe-gatekeeper` runtime.
- `AC7`: Coolify staging resources can deploy `vibe-gatekeeper` from GHCR images.
- `AC8`: Current production remains untouched until the new path is verified.

## Risks

### Existing Port Usage

Coolify expects access to its dashboard and proxy ports, and normal reverse-proxy operation needs ports `80` and `443` open. Existing services on the VPS may conflict with this and must be checked before final proxy exposure. Source: Coolify official docs on installation and firewall requirements.

Sources:

- https://coolify.io/docs/installation
- https://coolify.io/docs/knowledge-base/server/firewall
- https://coolify.io/docs/knowledge-base/custom-compose-overrides

### Polling Bot Cutover

Two runtimes must not poll the same production token simultaneously. The production cutover must be a bounded stop/start switch.

### Server Drift

The imported VPS tree includes helper scripts and local modifications. The imported repo must be normalized carefully without accidentally removing needed behavior.
