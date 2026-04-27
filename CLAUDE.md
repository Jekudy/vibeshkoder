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

- Coolify is the production runtime for bot and web deploys.
- Legacy `/home/claw/vibe-gatekeeper` is retained only as rollback fallback until
  `scripts/cleanup-legacy.sh` passes its A3, soak window, and disk preflights.

## Memory System Cycle (active 2026-04-26+)

Phase 1 (foundation: raw archive, message_versions, governance detector, edited_message
handler) **CLOSED 2026-04-27**. Phase 2 (importer + governance skeleton) is in progress
on three parallel stream worktrees:

- `.worktrees/p2-alpha` (`phase/p2-alpha`) — Stream Alpha (MessageRepo policy refresh /
  persist_message_with_policy helper)
- `.worktrees/p2-bravo` (`phase/p2-bravo`) — Stream Bravo (Telegram Desktop import schema /
  user mapping / dry-run parser)
- `.worktrees/p2-charlie` (`phase/p2-charlie`) — Stream Charlie (governance skeleton:
  forget_events, /forget command, cascade worker, /forget_me)

Read these BEFORE touching anything under `bot/db/`, `bot/services/`,
`bot/handlers/chat_messages.py`, or adding `alembic/versions/`:

1. `docs/memory-system/AUTHORIZED_SCOPE.md` — what is allowed in the immediate cycle
   (Phase 0 + Phase 1). What is **not** authorized. Critical safety rule for `#offrecord`.
2. `docs/memory-system/HANDOFF.md` — canonical 12-phase architecture, ticket backlog
   (T0-* through T11-*), governance / ingestion / search / qa specs, future butler boundary.
3. `docs/memory-system/IMPLEMENTATION_STATUS.md` — current status of every ticket. Updated
   after every PR merge.
4. `docs/memory-system/ROADMAP.md` — at-a-glance phase table with gates.
5. `docs/memory-system/DEV_SETUP.md` — isolated dev postgres + dev bot live ingestion testing
   protocol (sandbox-first; real chat requires team-lead approval).
6. `docs/memory-system/telegram-desktop-export-schema.md` — Telegram Desktop JSON export
   reference. Read BEFORE touching any code under `bot/services/import_*` or
   `tests/fixtures/td_export/`. Cross-stream contract: import schema details (envelope,
   message_kind taxonomy, edit/reply semantics, anonymous channel posts, mixed-array text
   form), governance quote, and downstream-ticket cross-refs.
7. `docs/memory-system/import-edit-history.md` — Telegram Desktop import edit-history policy.
   Read BEFORE implementing #103 import apply. Defines: `message_versions.imported_final=TRUE`
   marker, version_seq overlap semantics (live wins; import skips when live row exists),
   governance unchanged (`detect_policy` still runs). Schema/migration land in #103.

Issue tracker for memory cycle: **GitHub Issues** (label `phase:0`, `phase:1`, etc.). The
`nt` (Notion) plugin remains the tracker for non-memory work in this repo if any.

<!-- updated-by-superflow:2026-04-27 -->
