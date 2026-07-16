<!-- Root: ~/Vibe/CLAUDE.md — ALWAYS read it first for vault-wide rules and structure -->

# Shkoderbot Memory System — Documentation

This directory holds the canonical specification and roadmap for the Shkoderbot memory system —
the migration from a pure community gatekeeper into a governed community memory.

## Project Origin

Архитектура системы памяти спроектирована AI-архитектором (ChatGPT 5.5 Pro) за 5 промптов
в апреле 2026. Прямой вывод той сессии лежит в `HANDOFF.md` (~1200 строк, canonical).

Документы `ARCHITECTURE.md`, `GLOSSARY.md` и `decisions/*.md` (ADR) — это **пост-обработка**
вывода архитектора для онбординга людей и коллабораторов: HANDOFF слишком плотный для первого
знакомства. Если ADR/ARCHITECTURE противоречат HANDOFF — побеждает HANDOFF (он canonical),
а ADR/ARCHITECTURE правятся. Если правки в HANDOFF — обязательно обновить связанные ADR.

Новые архитектурные решения, не вошедшие в исходный HANDOFF, начинаются с Discovery/RFC
issue в GitHub Issues. После решения создаётся ADR в `decisions/` (если нужен) и отдельный
implementation issue.

## Read order (15-minute onboarding first, details later)

0. `ONBOARDING.md` — **если ты новый коллаборатор**, начни здесь. 30-минутный путь от нуля до "могу взять тикет".
1. `ARCHITECTURE.md` — система целиком: компоненты, поток данных, mermaid-диаграмма, что куда не ходит.
2. `GLOSSARY.md` — термины: message_version, tombstone, #offrecord, evidence card, llm_gateway и др.
3. `decisions/` — ADR (Architecture Decision Records): почему сделано именно так. Начни с ADR-0001.
4. `ROADMAP.md` — 12 фаз, gates, что авторизовано сейчас, что заблокировано и чем.
5. `AUTHORIZED_SCOPE.md` — точный список тикетов, авторизованных в текущем цикле. Critical safety rule для `#offrecord`.
6. `IMPLEMENTATION_STATUS.md` — что реализовано vs запланировано. Статус каждого тикета. Обновляется после каждого PR.
7. `DEV_SETUP.md` — как запустить dev bot локально с изолированным dev postgres.
8. `HANDOFF.md` — canonical detailed spec (1200+ строк). Читай после первых семи — как reference, а не как введение.

## Source of truth

If a previous spec disagrees with `HANDOFF.md`, `HANDOFF.md` wins. The legacy v0.5 design spec
(`docs/superpowers/archive/2026-04-22-shkoderbot-memory-editor-design.SUPERSEDED.md`) is
superseded — do not implement from it.

This rule governs product and architecture content. GitHub Issues is the separate canonical
source for work scope and status; this directory contains supporting specifications and
derived status snapshots.

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

## Workflow для архитектурных решений

```
Discovery/RFC Issue → Decision → ADR (if needed) → Implementation Issue → PR
```

1. Новое архитектурное предложение фиксируется Discovery/RFC issue до реализации.
2. В issue собираются evidence, варианты, рекомендация и итоговое решение.
3. После accept создаётся ADR в `decisions/`, если решение влияет на архитектуру.
4. Для реализации создаётся отдельный implementation issue со ссылкой на RFC/ADR.
5. Implementation issue → PR с `Closes #N`.

### Issue types

- **Discovery / RFC Issue** — research, architecture, changes to invariants, governance,
  retention/privacy, LLM policy, public surfaces, graph/butler, or schema strategy.
- **Implementation Work Issue** — features, bugs, tactical changes, tests, migrations, docs,
  and infrastructure after the decision is concrete.

См. ADR-0016 для CODEOWNERS и branch protection; ADR-0019 — для issue-first процесса.

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
