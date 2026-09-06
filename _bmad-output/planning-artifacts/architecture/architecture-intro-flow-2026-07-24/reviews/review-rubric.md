# Rubric review — Architecture Spine «Единый флоу интро»

Дата: 2026-07-24
Режим: BMAD reviewer gate, good-spine checklist
Проверяемый файл: `../ARCHITECTURE-SPINE.md`

## Вердикт

**CHANGES REQUIRED.** Основной замысел верен: PostgreSQL остаётся единственным
источником истины, Sheet становится односторонней проекцией, refresh создаёт
новое сообщение «Обновлённое интро», старое сообщение сохраняется, а общий
реферер может быть уточнён до конкретного `@username`. До implementation handoff
нужно закрыть пять точек расхождения, из них одна противоречит принятой ADR.

Механический lint:

```text
ok: true
total_findings: 0
```

## Что уже держит форму

- Парадигма соответствует задаче и brownfield-модели: `Application` становится
  identity версии, `Intro` — указателем на опубликованную текущую версию.
- AD-1 и AD-4 закрывают непосредственную причину инцидента: ответы больше нельзя
  выбирать по `user_id + is_current`, а Sheet не пишет обратно в canonical rows.
- AD-2 фиксирует общий контракт вопросов, публичных подписей, порядка,
  нормализации и заголовков экспорта.
- AD-3 сохраняет подтверждённый пользовательский блок, поэтому deploy или новая
  версия шаблона не меняют уже согласованный текст.
- AD-5 и companion flow явно требуют **новое** сообщение «Обновлённое интро» и
  сохранение старого сообщения.
- AD-7 точно отражает согласованный продуктовый контракт: общий источник можно
  уточнить конкретным Telegram-ником, конкретный ник обычный refresh не заменяет.
- Все версии Stack подтверждены текущим `uv.lock`.

## Critical

### R-1 — Deferred outbox ослабляет принятую ADR-0018 и оставляет split-brain публикации

**Evidence**

- Spine AD-5: Telegram-сообщение отправляется до транзакции, сохраняющей message
  identity и переключающей `Intro` (`ARCHITECTURE-SPINE.md:80-84`).
- Spine AD-6 предотвращает повтор только когда Telegram message ID уже сохранён
  (`ARCHITECTURE-SPINE.md:86-90`).
- Transactional outbox отложен (`ARCHITECTURE-SPINE.md:162`), а companion SPEC
  называет его non-goal (`SPEC.md:63`).
- Принятая ADR-0018 требует outbox для операций, одновременно меняющих БД и
  отправляющих внешний сигнал, прямо называя Telegram и Google Sheets
  (`docs/memory-system/decisions/0018-eventing-strategy-postgres-rows.md:60-64`).
- В текущем brownfield уже есть `InviteOutbox` и worker, то есть это не новый для
  системы архитектурный паттерн.

**Why it matters**

Падение после успешного `sendMessage`, но до commit оставит новое сообщение в
Telegram без publication identity в PostgreSQL. Повтор может создать дубль, а
`Intro` останется указывать на старую версию. Это именно расхождение между двумя
units, которое нельзя оставлять в `Deferred` по good-spine checklist.

**Action: discuss, затем fix.**

До handoff нужно выбрать одно:

1. наследовать ADR-0018 и зафиксировать durable publication job/outbox с
   `application_id` как idempotency identity и явной политикой ambiguous result;
2. либо формально supersede/ограничить ADR-0018 отдельным решением и описать
   безопасное состояние `publishing`, запрет автоматической повторной отправки
   после неоднозначного результата и operator reconciliation.

Простого «проверить, сохранён ли message ID» недостаточно: оно не закрывает окно
между внешним side effect и commit. После решения нужно синхронно обновить
companion SPEC.

## High

### R-2 — Spine одновременно ссылается на root SPEC и противоречит ему

**Evidence**

- Root `SPEC.md:270-275` по-прежнему объявляет Sheet источником истины и требует
  `Sheet edits → update local DB`.
- Spine включает root `SPEC.md` в `sources` (`ARCHITECTURE-SPINE.md:18-20`), но
  AD-4 запрещает Sheet-sync изменять `QuestionnaireAnswer`, `Application` и
  `Intro` (`ARCHITECTURE-SPINE.md:74-78`).
- Текущий код буквально реализует старый контракт:
  `bot/services/sheets.py:217-327`.

**Why it matters**

Implementation units смогут обоснованно выбрать противоположные контракты из
двух документов, оба из которых объявлены источниками. Spine должен
ратифицировать brownfield или явно описать его supersession, а не оставлять
скрытое противоречие.

**Action: autofix.**

Зафиксировать, что AD-4 supersedes root `SPEC.md` §5.3/§6 для этого флоу, и
добавить обновление этих разделов в обязательный migration surface. В
implementation story явно потребовать удалить DB-write path
`sync_all_from_sheet`, а не только перестать его вызывать из одного scheduler.

### R-3 — Identity поля задана, но единственность ответа внутри версии не закреплена

**Evidence**

- AD-2 вводит `field_id`, но не задаёт ключ/constraint
  (`ARCHITECTURE-SPINE.md:62-66`).
- Structural Seed показывает `application_id + field_id`, но не фиксирует их
  уникальность (`ARCHITECTURE-SPINE.md:133-137`).
- Текущая таблица имеет только индекс `(user_id, is_current)` и допускает
  несколько строк одного вопроса в одной application
  (`bot/db/models.py:163-176`).
- Текущий `save_answer` всегда делает insert
  (`bot/db/repos/questionnaire.py:11-29`).

**Why it matters**

Повторно доставленный Telegram update или параллельный обработчик может создать
два значения одного поля в одной версии. Разные readers смогут выбрать разные
строки, хотя везде присутствует правильный `application_id`; AD-1 это не
предотвращает.

**Action: autofix.**

В AD-1 или AD-2 закрепить:

- для новых версий `application_id` и `field_id` обязательны;
- в одной application существует не более одного ответа на `field_id`
  (DB unique key `(application_id, field_id)`);
- запись в `filling` использует выбранную единую семантику insert-or-update;
- после confirmation изменения запрещены централизованной write-path проверкой.

Legacy rows с `application_id IS NULL` могут остаться как совместимый исторический
слой; это не должно ослаблять constraint для новых версий.

## Medium

### R-4 — Не решено, когда новая подтверждённая анкета попадает в Sheet

**Evidence**

- Companion flow нового участника проводит проекцию в Sheet сразу от confirmed
  S1, ещё до поручительства и вступления (`user-flow.md:19-29`).
- Spine diagram проводит Sheet только от текущего `Intro` pointer
  (`ARCHITECTURE-SPINE.md:36-43`).
- Export convention говорит «текущая подтверждённая версия», но не определяет,
  означает ли это pending application или только опубликованное `Intro`
  (`ARCHITECTURE-SPINE.md:106`).

**Why it matters**

Одна implementation unit экспортирует pending-заявки после confirmation,
другая — только участников с опубликованным intro. Это меняет видимость данных и
семантику существующего листа.

**Action: discuss.**

Выбрать один trigger и закрепить его в AD-4 и обеих схемах. Если Sheet остаётся
реестром интро участников, наиболее непротиворечивый вариант — проецировать
версию только после переключения `Intro.application_id`; если Sheet нужен для
pending-заявок, это должно быть отдельным явно названным представлением, не
неявным побочным эффектом.

### R-5 — Resume семантика refresh не связывает пользователя с единственным draft

**Evidence**

- Companion contract обещает, что незавершённый draft можно продолжить
  (`user-flow.md:48-49`).
- AD-5 сохраняет старое Intro, но не определяет, создаёт ли повторный `/refresh`
  новый draft или возобновляет существующий.
- В brownfield `ApplicationRepo.create` не ограничивает число `filling`
  applications пользователя, а `get_active` выбирает последнюю по времени
  (`bot/db/repos/application.py:10-29`).

**Why it matters**

Два draft одного пользователя создают неоднозначное понятие stale callback,
копирование реферера и выбор версии для confirmation.

**Action: autofix.**

Добавить правило: у пользователя один активный refresh-draft; повторный
`/refresh` продолжает его либо явно закрывает прежний перед созданием нового.
Выбранная операция должна быть CAS/transaction-safe, а stale определяется через
конкретный `application_id` и допустимый статус.

## Low tail

- `Deployment, environments, auth и privacy` отложены ссылкой на «существующую
  архитектуру» без точных документов. Для feature altitude допустимо наследование,
  но перед final желательно назвать конкретные источники хотя бы для privacy и
  deployment.
- У `AD-2`, `AD-3`, `AD-5`, `AD-6`, `AD-7` нет явного status marker, тогда как
  `AD-1` и `AD-4` помечены `[ADOPTED]`. Либо статус должен быть единообразным,
  либо легенда должна объяснять различие.

## Checklist result

| Good-spine criterion | Result |
| --- | --- |
| Реальные divergence points нижнего уровня закрыты | **Fail:** R-1, R-3, R-5 |
| Rule каждого AD enforceable и предотвращает stated divergence | **Partial:** AD-1/2/6 требуют unique/CAS binding |
| Deferred не позволяет units разойтись | **Fail:** R-1 |
| Named tech verified-current | **Pass:** версии совпадают с `uv.lock` |
| Brownfield ратифицирован или миграция явно согласована | **Fail:** R-2 |
| Все capabilities companion SPEC покрыты | **Partial:** CAP-5 trigger расходится, R-4 |
| Inherited decisions не ослаблены | **Fail:** R-1 против ADR-0018 |
| Feature-owned dimensions decided/deferred/open | **Partial:** publication recovery и refresh resume недоопределены |

## Gate condition

После закрытия R-1–R-3 spine можно передавать в implementation planning. R-4
требует одного продуктового решения; R-5 допускает прямой autofix с
рекомендованной семантикой «resume existing draft».

## Recheck после amendments

Дата повторной проверки: 2026-07-24
Механический lint: **PASS**, findings: 0.

- **R-1 — RESOLVED.** Парадигма и inherited invariants теперь включают
  Postgres-outbox; AD-5/AD-6 определяют durable claim, уникальный effect,
  `unknown` для неоднозначного Telegram outcome и запрет слепого retry.
- **R-2 — RESOLVED.** Root `SPEC.md` удалён из canonical sources, а обязательное
  обновление его §5.3 закреплено как часть implementation PR.
- **R-3 — RESOLVED.** AD-1 и Structural Seed требуют unique
  `(application_id, field_id)`; AD-3 проверяет полноту и уникальность ответов
  перед атомарным переходом из `filling`.
- **R-4 — NOT RESOLVED (Medium).** AD-9 и Export convention теперь однозначно
  создают Sheet-effect только после успешной Telegram-публикации и продвижения
  `Intro`. Однако canonical companion `user-flow.md:24-29` по-прежнему ведёт
  новый admission из `Confirmed S1` прямо в `Google Sheet: проекция S1`, до
  поручительства, вступления и финального интро. Нижний уровень всё ещё получает
  два разных trigger-контракта.
- **R-5 — RESOLVED.** AD-8 задаёт `flow_kind`, `base_application_id`, partial
  unique одной активной refresh-application и resume при повторном `/refresh`;
  companion flow синхронизирован.

### Recheck verdict

**CHANGES REQUIRED: осталась одна Medium-consistency правка; Critical/High
findings больше нет.** Для PASS нужно заменить в admission-схеме companion
прямую связь `C → S` на проекцию после успешного финального интро/продвижения
`Intro`, в соответствии с AD-9.

## Final recheck

`user-flow.md` теперь ведёт admission `Вступление → Новое интро S1 → Google
Sheet`, что совпадает с AD-9 и Export convention. R-4 закрыт.

**FINAL VERDICT: PASS.** Все R-1–R-5 закрыты; mechanical lint остаётся чистым,
remaining Critical/High/Medium findings: 0.
