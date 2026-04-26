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

## Memory System Cycle (active 2026-04-26+)

A multi-phase migration from gatekeeper → governed community memory is in progress on a
separate worktree (`feat/memory-foundation`). Read these BEFORE touching anything under
`bot/db/`, `bot/services/`, `bot/handlers/chat_messages.py`, or adding `alembic/versions/`:

1. `docs/memory-system/AUTHORIZED_SCOPE.md` — what is allowed in the immediate cycle
   (Phase 0 + Phase 1). What is **not** authorized. Critical safety rule for `#offrecord`.
2. `docs/memory-system/HANDOFF.md` — canonical 12-phase architecture, ticket backlog
   (T0-* through T11-*), governance / ingestion / search / qa specs, future butler boundary.
3. `docs/memory-system/IMPLEMENTATION_STATUS.md` — current status of every ticket. Updated
   after every PR merge.
4. `docs/memory-system/ROADMAP.md` — at-a-glance phase table with gates.
5. `docs/memory-system/DEV_SETUP.md` — isolated dev postgres + dev bot live ingestion testing
   protocol (sandbox-first; real chat requires team-lead approval).

Issue tracker for memory cycle: **GitHub Issues** (label `phase:0`, `phase:1`, etc.). The
`nt` (Notion) plugin remains the tracker for non-memory work in this repo if any.
