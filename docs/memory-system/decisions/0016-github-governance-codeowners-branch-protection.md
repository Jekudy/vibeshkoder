# ADR-0016: GitHub governance — CODEOWNERS + branch protection + ADR-link check

- **Status**: Accepted
- **Date**: 2026-04-27
- **Decision-makers**:
  - @Jekudy — product / architecture owner
  - @&lt;tech-lead-handle&gt; — implementation owner (TODO: заменить когда tech lead появится)
- **Author**: GPT-5.5 Pro / principal architect draft
- **Reviewers**: @&lt;collaborator-handle&gt; (TODO: добавить при онбординге)

> Process-routing note: the former Discussions → ADR → Issue sequence is superseded by
> ADR-0019. CODEOWNERS and branch-protection decisions in this ADR remain accepted.

## Context

Memory cycle вводит чувствительные зоны: governance, llm boundary, db migrations, import
path, privacy-зависимые handlers. До прихода коллаборатора репозиторий жил в режиме
solo-разработки, без формальных правил review и без branch protection.

Без автоматизации правил merge есть риск:
- PR в `bot/services/llm_gateway/` смержится без проверки специалистом → нарушится
  invariant 2 (no LLM calls outside gateway).
- PR с миграцией Alembic смержится без db review → tombstone семантика сломается.
- Архитектурное изменение (например, добавление новой LLM provider) пройдёт без ADR →
  через 3 месяца никто не вспомнит почему так сделано.
- Force-push на `main` уничтожит историю аудита.

Ручная дисциплина не масштабируется на коллабораторов и AI-агентов.

## Decision

GitHub governance включён до старта Phase 1, в составе трёх механизмов:

### 1. CODEOWNERS

Файл `.github/CODEOWNERS` маршрутизирует review по risk areas. Owners — placeholders
(`@<tech-lead-handle>`, `@<db-owner-handle>`, `@<governance-owner-handle>`,
`@<llm-owner-handle>`, `@<qa-owner-handle>`), заменяются реальными handles по мере
онбординга.

Покрытые зоны (см. файл для полного списка):
- `/docs/memory-system/` + `/docs/memory-system/decisions/` — `@Jekudy + @<tech-lead-handle>`
- `/bot/db/` + `/alembic/` — `@<db-owner-handle> + @<tech-lead-handle>`
- `/bot/handlers/chat_messages.py`, `ingestion.py`, `normalization.py`, `governance.py`,
  `search.py`, `evidence.py`, `qa.py` — `@<governance-owner-handle> + @<tech-lead-handle>`
- `/bot/services/llm_gateway/` — `@<llm-owner-handle> + @<tech-lead-handle>`
- `/web/routes/{governance,memory,cards,digests}.py` — `@<governance-owner-handle> + @<tech-lead-handle>`
- `/bot/importers/` + `/scripts/import_telegram_export.py` — `@<db-owner-handle> + @<governance-owner-handle>`
- `/tests/` — `@<qa-owner-handle> + @<tech-lead-handle>`

GitHub нюанс: при нескольких owners для одного паттерна approval **любого одного**
достаточно для code-owner-required check. Для зон, требующих multi-owner sign-off, нужен
отдельный branch protection с `required_approving_review_count: 2`.

### 2. Branch protection на `main`

Включено через `gh api PUT /repos/Jekudy/vibeshkoder/branches/main/protection`:

- `required_approving_review_count: 1` (bootstrap; поднять до 2 после онбординга коллаборатора)
- `dismiss_stale_reviews: true` — старый approval слетает при новом push
- `require_code_owner_reviews: true` — CODEOWNERS реально работает
- `required_linear_history: true` — никаких merge commits, только rebase
- `allow_force_pushes: false`
- `allow_deletions: false`
- `enforce_admins: true` — правила применяются и к админам
- Required CI checks: `CI / test`, `CI / gitleaks`, `CI / trivy`, `CI / semgrep`

### 3. ADR-link check (planned)

GitHub Action `adr-link-check` (см. ADR-0011, ADR-0012) проверяет, что PR, меняющий
sensitive paths (`bot/db/**`, `alembic/**`, `bot/services/governance.py`,
`bot/services/llm_gateway*`, `bot/services/ingestion.py`, `bot/services/normalization.py`,
`bot/services/qa.py`, `docs/memory-system/decisions/**`, `docs/memory-system/**`),
содержит в body `ADR: ADR-NNNN` или `ADR: not required — <reason>`. Не выполнено —
required status check падает, merge заблокирован.

Реализуется отдельным sprint после старта Phase 1.

## Consequences

### Positive

- Каждый PR в чувствительную зону получает auto-assigned reviewer без ручной дисциплины.
- `main` защищён от force-push и accidental deletion → audit trail устойчив.
- ADR-link check (когда внедрён) превращает «забыл сослаться на ADR» из reviewer caffeine
  level в required CI status.
- Onboarding нового коллаборатора сводится к замене placeholder в CODEOWNERS — vs
  пере-объяснению process устно.

### Negative / Trade-offs

- Bootstrap-режим (1 review) недостаточен для critical merges (migrations, governance).
  Требуется ручная дисциплина «два глаза» до перехода в 2-review mode после онбординга.
- CODEOWNERS approval любого одного owner достаточен — не гарантирует multi-owner
  sign-off без явного `required_approving_review_count: 2`.
- ADR-link check ещё не реализован → пока полагается на reviewer attention. Не блокирует
  merge до выкладки Action.
- Solo-режим первого месяца: ты сам себе reviewer для большинства PR; защита от
  собственных ошибок ограничена CI checks и linear history.

## Alternatives considered

1. **Никаких rules, только conventions** — отвергнуто: не масштабируется на коллабораторов
   и агентов; нарушения не отлавливаются.
2. **Только CODEOWNERS без branch protection** — отвергнуто: CODEOWNERS без enforcement
   не блокирует merge; декоративный файл.
3. **Strict 2-review с старта** — отвергнуто на bootstrap: solo-разработчик блокирует сам
   себя; вернёмся к 2 reviews после онбординга.
4. **Rulesets вместо branch protection rules** — рассмотрено, отложено: branch protection
   более привычно и достаточно для текущих задач; rulesets имеет смысл при росте числа
   защищённых веток.

## References

- HANDOFF.md §0 «Agent execution rules», §1 invariants
- ADR-0001 — Postgres as source of truth
- ADR-0004 — LLM gateway as single boundary
- ADR-0007 — Import through same governance
- [GitHub Docs — About code owners](https://docs.github.com/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
- [GitHub Docs — About protected branches](https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [GitHub Docs — Available rules for rulesets](https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- `.github/CODEOWNERS` — текущая схема
- ADR-0019 — GitHub Issues as the sole work ledger and BMAD handoff
- `docs/memory-system/README.md` — current issue-first workflow
