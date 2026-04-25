<!-- Root: ~/Vibe/CLAUDE.md — ALWAYS read it first for vault-wide rules and structure -->

# CLAUDE.md

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

## Issue Tracker

- This repo uses Notion via `nt` plugin (`/nt:issue`, `/nt:work`, `/nt:status`, ...)
- Other projects use Linear via `ln` plugin
- Do not mix: `nt` commands in non-shkoderbot repos will fail by design
- To override in one-off scenarios: `export NT_TEAM=SHK`

## Current Migration Rule

- The legacy production runtime at `/home/claw/vibe-gatekeeper` remains the live path until the new GitHub/GHCR/Coolify path is verified.
- Do not cut over the production bot token during bootstrap work.
