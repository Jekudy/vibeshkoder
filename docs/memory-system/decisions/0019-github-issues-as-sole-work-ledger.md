# ADR-0019: GitHub Issues как единый реестр работы и handoff из BMAD

- **Status**: Accepted
- **Date**: 2026-07-16
- **Decision-maker**: @Jekudy
- **Source issue**: [#412](https://github.com/Jekudy/vibeshkoder/issues/412)

## Context

В проекте одновременно существовали GitHub Issues, GitHub Discussions, Notion, локальные
backlog-файлы, charters и status-документы. После установки BMAD появляется ещё один класс
артефактов: research, brief, PRD, architecture и stories. Без единого handoff каждый такой
артефакт может стать параллельным backlog или разрешить разработку без отслеживаемого scope.

Проекту нужен один механизм, который отвечает на три вопроса: что делаем, почему это
авторизовано и в каком состоянии работа сейчас.

## Decision

1. GitHub Issues — единственный canonical tracker для scope, backlog и status.
2. Любая проработка — research, brainstorming, design, architecture, planning, PRD,
   retrospective или course correction — завершена только после создания или обновления
   GitHub Issue с результатом.
3. Разработка не начинается без открытого issue с problem, scope, acceptance criteria,
   validation plan и dependencies. Это проверяется до создания branch/worktree и до правок.
4. Проработка без готового implementation scope заканчивается Discovery/RFC issue.
   Конкретная разработка получает отдельный Implementation Work issue.
5. BMAD outputs, ADR, планы, charters и status-файлы — supporting/derived artifacts. Они
   линкуются из issue и не заменяют его.
6. Branch, commit и PR ссылаются на issue. PR обязан содержать closing keyword
   (`Closes`, `Fixes` или `Resolves`) и номер открытого issue.
7. Если GitHub недоступен, workflow останавливается. Локальный TODO или другой tracker не
   используется как fallback.

## Consequences

### Positive

- У каждой разработки есть наблюдаемый origin и проверяемый done-критерий.
- BMAD улучшает качество issues, не создавая параллельную систему управления.
- Решения, отклонённые идеи и отложенная работа не теряются в локальных документах.
- CI механически блокирует PR без открытого issue.

### Negative / Trade-offs

- Даже маленькая разработка требует сначала оформить issue.
- BMAD workflow не считается завершённым сразу после записи локального артефакта.
- При недоступном GitHub нельзя продолжить implementation «временно локально».

## Alternatives considered

1. **Оставить Discussions для RFC** — отвергнуто: это второй вход и отдельный lifecycle.
2. **Хранить BMAD artifacts как backlog** — отвергнуто: файлы не дают единого статуса,
   ownership и связи с PR.
3. **Только правило в документации** — отвергнуто: PR gate нужен как механическая защита.

## References

- [Issue #412](https://github.com/Jekudy/vibeshkoder/issues/412)
- ADR-0016 — CODEOWNERS и branch protection
- `_bmad-output/project-context.md` — BMAD issue-first contract
- `.github/workflows/pr-issue-gate.yml` — механическая проверка PR
