<!-- Root: ~/Vibe/CLAUDE.md — ALWAYS read it first for vault-wide rules and structure -->

# Onboarding: Memory System Cycle

Документ для нового коллаборатора (человек или AI-агент). Цель — за 30 минут понять
что строится, где мы сейчас, во что ты включаешься и как.

---

## TL;DR в одном абзаце

Shkoderbot — Telegram-бот для гейткипинга комьюнити (заявки, vouching, intro). Сейчас идёт
миграция: бот превращается в **governed community memory** — систему, которая собирает
сообщения чата, нормализует их, хранит как `message_version` (источник истины), извлекает
факты/события/observations через governance-фильтр, отдаёт ответы на вопросы участников
с цитатами, и в перспективе становится butler-агентом для community-задач. Архитектура
спроектирована AI-архитектором (ChatGPT 5.5 Pro) за 5 промптов; canonical документ —
`HANDOFF.md` (~1200 строк); читать его сначала **не надо** — есть короткие точки входа.

---

## Read this first (по порядку, ~30 минут)

1. **`ARCHITECTURE.md`** (5 мин) — диаграмма потока, компоненты, границы. Здесь увидишь систему целиком.
2. **`GLOSSARY.md`** (5 мин) — словарь: `message_version`, `tombstone`, `#offrecord`, `evidence card`, `llm_gateway`. Без этого HANDOFF читать невозможно.
3. **`decisions/`** (10 мин) — ADR-0001…0007. Это **почему** сделано именно так, а не иначе. Главный документ для понимания философии.
4. **`ROADMAP.md`** (5 мин) — 12 фаз, gates, что заблокировано чем.
5. **`AUTHORIZED_SCOPE.md`** (5 мин) — **критично**: что прямо сейчас разрешено делать, что **нельзя**. Здесь же safety-rules для `#offrecord`.

После этого тебе достаточно контекста чтобы обсуждать систему. `HANDOFF.md` читай как
reference, когда нужны детали — не как введение.

---

## Где мы сейчас

- **Активный цикл**: Phase 0 (Gatekeeper Stabilization) + Phase 1 (Source of Truth + Raw Archive).
- **Branch**: `feat/memory-foundation` в worktree `.worktrees/memory/`.
- **Issue tracker**: GitHub Issues в этом репо. Лейблы: `phase:0`, `phase:1`, `area:*`, `size:*`, `priority:*`.
- **Milestones**: 14 штук, по фазам ROADMAP. Прогресс виден в Issues UI.
- **Project board**: GitHub Project v2 "Memory System Cycle" — визуальный pipeline тикетов.
- **Status snapshot**: `IMPLEMENTATION_STATUS.md` — обновляется после каждого merged PR.

---

## Workflow на каждый день

### Когда берёшь тикет

1. Открываешь Project board → колонка `Ready` → берёшь тикет с твоим area.
2. Проверяешь `AUTHORIZED_SCOPE.md` — твоя работа в рамках разрешённого scope?
3. Если нет — создаёшь Discovery/RFC issue в GitHub Issues, не пишешь код.
4. Если да — assign себя, переводишь карточку в `In Progress`.
5. Создаёшь ветку `feat/<short-slug>` от `feat/memory-foundation` (или от `main`, если работа вне memory cycle).

### Когда делаешь PR

PR template сам поднимет чек-лист. Без всех галочек PR не мержится:

- CI green
- AUTHORIZED_SCOPE проверен
- IMPLEMENTATION_STATUS обновлён (если тикет завершён)
- Если архитектурное решение → ADR создан в `docs/memory-system/decisions/`
- Non-negotiable invariants соблюдены (см. `README.md`)
- Тесты добавлены/обновлены
- Документация обновлена (HANDOFF / ROADMAP / GLOSSARY / ARCHITECTURE — что релевантно)

### Когда у тебя архитектурный вопрос

1. **Не пиши код.** Открой `decisions/` — может уже есть ADR.
2. Если нет — создай Discovery/RFC issue с проблемой, evidence, опциями и рекомендацией.
3. После accept — оформи ADR при необходимости и отдельный implementation issue.

### Когда что-то непонятно

- Термин не понятен → `GLOSSARY.md`.
- Решение не понятно → `decisions/` (поиск по ключевому слову).
- Граница системы не понятна → `ARCHITECTURE.md` раздел "Boundaries".
- Что можно делать сейчас → `AUTHORIZED_SCOPE.md`.
- Что **вообще** не понятно → создай Discovery/RFC issue с контекстом и вопросом.

---

## Non-negotiable invariants (выучить наизусть)

Эти правила нарушать **нельзя** — система спроектирована вокруг них:

1. Existing gatekeeper must not break — старая функциональность бота приоритетна.
2. No LLM calls outside `llm_gateway` — все обращения к LLM через единственный модуль.
3. No extraction/search/qa over `#nomem` / `#offrecord` / forgotten content — этот контент система **не видит**.
4. Citations point to `message_version_id` или approved card sources — нельзя цитировать "из воздуха".
5. Summary is never canonical truth — суммаризации derived, не источник истины.
6. Graph is never source of truth — граф это projection, истина в Postgres.
7. Future butler не может читать raw DB — только через governance-filtered evidence context.
8. Import apply идёт через ту же normalization/governance что и live updates.
9. Tombstones durable — не откатываются casual'но.
10. Public wiki disabled пока review/source-trace/governance не доказаны рабочими.

Если ты не понимаешь **почему** какое-то правило — открой соответствующий ADR. Если ADR нет
для этого правила — создай Discovery/RFC issue.

---

## Первая зона ответственности коллаборатора

**Owner**: `@<collaborator-handle>` (заменить на handle при онбординге)
**Area**: Phase 0 stabilization + tests
**НЕ реализуй (явный анти-список)**: LLM extraction, knowledge cards, wiki, graph DB, butler actions. Всё это дальше по roadmap, после стабилизации.

### First sprint tickets

- `T0-01` fix forward_lookup membership/admin check (privacy leak)
- `T0-02` fix/contain sqlite vs postgres upsert path
- `T0-03` make `MessageRepo.save` idempotent
- `T0-06` add gatekeeper regression tests

### First success criteria

- privacy leak в forward_lookup закрыт
- duplicate message save идемпотентен
- dev/test db upsert path безопасен
- regression tests добавлены и зелёные

### Second assignment (после Phase 0)

Если коллаборатор сильный backend/db — переходим к Phase 1 foundation:

- `T1-01` add `feature_flags` table
- `T1-02` add `ingestion_runs` table
- `T1-03` add `telegram_updates` raw archive

### Почему именно так

Phase 1 трогает source-of-truth schema. Новый человек сначала должен прочувствовать
current gatekeeper, repo style, handlers/repos/migrations. Phase 0 даёт реальный impact
+ small blast radius + tests + понимание privacy/governance context.

См. ADR-0008 (preserve gatekeeper during migration) и ADR-0009 (extend chat_messages
before new table) для архитектурного обоснования.

---

## Полезные ссылки

- Главный README: `README.md` (порядок чтения, invariants, workflow)
- Canonical spec: `HANDOFF.md` (читать после первых пяти документов)
- Status: `IMPLEMENTATION_STATUS.md`
- Project board: GitHub → Projects → "Memory System Cycle"
- Milestones: GitHub → Issues → Milestones
- RFC / Q&A / Show & Tell: GitHub → Discussions

Удачи. Если что-то в этом документе тебя сбило с толку — это баг документации, скажи.
