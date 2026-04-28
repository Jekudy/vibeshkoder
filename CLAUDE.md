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
8. `docs/memory-system/import-user-mapping.md` — Telegram Desktop import user-mapping policy.
   Read BEFORE touching any code under `bot/services/import_*` that reads `from_id` / writes
   `users` rows. Defines: known-user resolution, ghost-user creation with `is_imported_only=true`
   flag, anonymous channel singleton, privacy R2 (imports cannot promote themselves to live;
   only the gatekeeper live-registration path flips ghost→live by clearing `is_imported_only`),
   display_name first-write-wins, attribution semantics under live/import overlap.
9. `docs/memory-system/import-dry-run-parser.md` — Telegram Desktop dry-run parser. Read BEFORE
   touching `bot/services/import_parser.py` or invoking `python -m bot.cli import_dry_run`.
   Defines: `ImportDryRunReport` field semantics, single-chat-only input contract (full-account
   exports rejected), NO-content guarantee (`asdict(report)` carries zero message bodies),
   `governance.detect_policy` invocation contract (called per user message, service messages
   skipped), operator pre-flight role before any #103 apply run. Cross-refs #91 schema, #93
   user mapping, #106 edit-history policy.
10. `docs/memory-system/import-reply-resolver.md` — Telegram Desktop import reply resolver. Read
    BEFORE touching `bot/services/import_reply_resolver.py` or before #99 (T2-02 dry-run stats) /
    #103 (T2-03 apply) consume reply mappings. Defines: priority order (same_run → prior_run →
    live → unresolved), chat_id scoping (never resolves across chat boundaries), batch query
    semantics (4 queries max regardless of N — no N+1), `ReplyResolution` / `ReplyResolverStats`
    API contract, read-only invariant (NO DB writes; safe inside any transaction), forward-chain
    direct-lookup design choice (chain_depth always 0; consumers iterate if they need deeper
    traversal). Cross-refs #91 schema, #93 user mapping, #94 dry-run parser.
11. `docs/memory-system/import-checkpoint.md` — Telegram Desktop import apply checkpoint /
    resume infrastructure. Read BEFORE touching `bot/services/import_checkpoint.py`,
    `bot/cli.py::import_apply`, or implementing #103 (Stream Delta apply). Defines:
    resume decision matrix (`start_fresh` / `resume_existing` / `block_partial_present`),
    `ingestion_runs.stats_json.last_processed_export_msg_id` deep-merge contract (atomic
    `UPDATE ... SET stats_json = COALESCE(stats_json, '{}') || CAST(:patch AS jsonb)`), partial
    UNIQUE index on `(source_hash) WHERE status='running'` (race-safe at-most-one running
    import per export), `source_hash` sha256 dedup, CLI exit codes (3=block partial-present,
    4=apply-not-implemented placeholder until #103), `finalize_run` idempotency, lazy
    `run_apply` import dance. Cross-refs #94 dry-run parser, #98 reply resolver, #103 apply
    (deferred). HIGH-RISK boundary: idempotency / no-double-write / no-orphan-rows
    invariants.

Issue tracker for memory cycle: **GitHub Issues** (label `phase:0`, `phase:1`, etc.). The
`nt` (Notion) plugin remains the tracker for non-memory work in this repo if any.

<!-- updated-by-superflow:2026-04-28 -->
