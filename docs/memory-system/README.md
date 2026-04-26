<!-- Root: ~/Vibe/CLAUDE.md — ALWAYS read it first for vault-wide rules and structure -->

# Shkoderbot Memory System — Documentation

This directory holds the canonical specification and roadmap for the Shkoderbot memory system —
the migration from a pure community gatekeeper into a governed community memory.

## Read order

1. `HANDOFF.md` — full architect handoff (canonical, verbatim). Covers vision, 12-phase roadmap,
   governance invariants, ticket backlog, migration spec, ingestion/normalization spec, governance
   spec, search/qa spec, future butler boundary, risk register.
2. `AUTHORIZED_SCOPE.md` — what is allowed in the immediate execution cycle (Phase 0 + Phase 1
   only). What is NOT authorized yet. Critical safety rules for `#offrecord` and import.
3. `ROADMAP.md` — condensed phase table with gates, dependencies, parallelization guide.
4. `IMPLEMENTATION_STATUS.md` — what is implemented vs planned. Authoritative status of each
   ticket. Updated after every PR merge.
5. `DEV_SETUP.md` — how to run the dev bot locally with isolated dev postgres for live ingestion
   testing.

## Source of truth

If a previous spec disagrees with `HANDOFF.md`, `HANDOFF.md` wins. The legacy v0.5 design spec
(`docs/superpowers/archive/2026-04-22-shkoderbot-memory-editor-design.SUPERSEDED.md`) is
superseded — do not implement from it.

## Workflow

- Branch: `feat/memory-foundation` in worktree `.worktrees/memory/`.
- Framework: superflow (per-worktree state file, does not collide with the main `security-audit`
  cycle running on `main`).
- Issue tracker: GitHub Issues. Labels: `phase:0`, `phase:1`, `area:memory`,
  `area:gatekeeper-safety`, `area:db`, `area:governance`, `area:ingestion`.
- PR target: `main`. Sprint-PR-queue mode (one PR per ticket, sequential rebase, CI green before
  merge).
- Reviewers per PR: Claude product reviewer + Codex technical reviewer (dual review). Codex used
  for migrations and security-sensitive code.
- Documentation: every merged PR updates `IMPLEMENTATION_STATUS.md`.

## Non-negotiable invariants (from HANDOFF.md §1)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction/search/qa over `#nomem` / `#offrecord` / forgotten content.
4. Citations point to `message_version_id` or approved card sources.
5. Summary is never canonical truth.
6. Graph is never source of truth.
7. Future butler cannot read raw DB directly; must use governance-filtered evidence context.
8. Import apply must go through the same normalization/governance path as live updates.
9. Tombstones are durable; not casually rolled back.
10. Public wiki disabled until review/source-trace/governance proven.
