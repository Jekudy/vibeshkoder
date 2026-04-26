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

## Current Migration Rule

- Coolify is the production runtime for bot and web deploys.
- Legacy `/home/claw/vibe-gatekeeper` is retained only as rollback fallback until
  `scripts/cleanup-legacy.sh` passes its A3, soak window, and disk preflights.
