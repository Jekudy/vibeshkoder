# Reality review — единый флоу интро

**Линза:** соответствие текущему brownfield-коду, схеме БД, принятым ADR и
зафиксированным зависимостям.

**Вердикт:** **REVISE — spine пока нельзя передавать в implementation.** Базовая
модель версии верна, а версии библиотек подтверждены, но Deferred про outbox
противоречит принятому ADR-0018. Ещё три решения недостаточно точны, чтобы два
исполнителя одинаково реализовали переключение текущего интро, неизменяемость
snapshot и идемпотентность публикации.

## Critical

### C-1 — Deferred про transactional outbox противоречит принятому ADR-0018

**Spine:** AD-5 связывает публикацию Telegram и смену текущей версии, AD-4 —
DB-состояние и Google Sheet, но в `Deferred` transactional outbox отложен до
появления доказанной crash-window.

**Реальность:** принятый
`docs/memory-system/decisions/0018-eventing-strategy-postgres-rows.md:60-64`
требует outbox table в той же транзакции, что DB update, и отдельный worker с
retry именно для исходящих Telegram- и Google Sheets-side effects. В
репозитории этот паттерн уже реализован специализированными
`InviteOutbox`/`InviteOutboxRepo`/`process_invite_outbox`
(`bot/db/models.py:1013-1043`, `bot/services/invite_worker.py:21-84`).

Текущий refresh показывает исходный дефект порядка: сначала меняются `Intro` и
`Application`, затем внутри той же handler-транзакции выполняется
`bot.send_message`, а ошибка молча проглатывается
(`bot/handlers/questionnaire.py:256-281`;
`bot/middlewares/db_session.py:18-25`). Join-flow также коммитит `added` и
`Intro` даже при неуспешной публикации
(`bot/handlers/chat_events.py:177-211`).

**Почему это блокирует:** AD-5 обещает, что старая версия остаётся текущей до
успешной публикации. Прямой `sendMessage` внутри handler-транзакции не даёт
durable intent, retry и наблюдаемого terminal state. Кроме того, он оставляет
неустранимое окно «Telegram отправил, DB не закоммитила».

**Требуемое исправление spine:**

1. Унаследовать ADR-0018 явно.
2. Удалить outbox из `Deferred`.
3. Зафиксировать минимальный Postgres-outbox flow для intro publication и Sheet
   projection: enqueue в транзакции подтверждения/переключения, worker через
   APScheduler, статусы, attempts/error и idempotency key.
4. Явно назвать принятый residual risk Telegram: API не предоставляет
   idempotency key для `sendMessage`, поэтому crash строго между внешним успехом
   и записью `message_id` может дать повтор. Либо принять этот узкий риск в
   Deferred, либо выбрать иной способ публикации; нельзя откладывать весь
   outbox.

## High

### H-1 — `Intro` одновременно объявлен указателем и второй копией canonical text

**Spine:** парадигма и AD-5 называют `Intro` указателем на текущую
`Application`, но Structural Seed сохраняет и `Intro.application_id`, и
`Intro.intro_text`.

**Реальность:** сегодня `Intro.intro_text` — рабочее хранилище текста:
`forward_lookup` читает его напрямую, `IntroRepo.upsert` изменяет, а Sheet pull
перезаписывает (`bot/handlers/forward_lookup.py:80`,
`bot/db/repos/intro.py:13-32`, `bot/services/sheets.py:307-325`). У
`Application` snapshot и у `Intro` ссылки на application пока нет
(`bot/db/models.py:128-195`).

**Риск:** после добавления `Application.confirmed_intro_text` появятся две
канонические копии. Любой забытый writer или новый reader сможет снова показать
не ту версию, что разрушает AD-3 и исходную парадигму.

**Требуемое исправление spine:** выбрать одно:

- предпочтительно: `Intro` хранит pointer, readers берут snapshot через
  application; `intro_text` мигрируется/удаляется отдельным совместимым шагом;
- либо brownfield-совместимо: `Intro.intro_text` формально объявляется
  денормализованной проекцией snapshot, обновляемой только вместе с pointer в
  одной транзакции и никогда не используемой как независимый источник.

Роль `vouched_by_name`, `sheets_row_number` и `last_synced_at` при этом также
должна быть сохранена или явно мигрирована.

### H-2 — граница immutable snapshot не представима текущими статусами

**Spine:** AD-3 говорит «при переходе из `filling`» сохраняется snapshot, а
Consistency Convention говорит, что ответы изменяются только в `filling`.

**Реальность:** `Application.status` имеет только `filling`, `pending`,
`vouched`, `added`, `rejected`, `privacy_block`
(`bot/db/models.py:134-150`). Новый applicant сегодня переходит
`filling → pending` после отправки vouch-card; refresh —
`filling → added` до отправки обновлённого интро
(`bot/handlers/questionnaire.py:244-302`). `get_active()` и `/start` также
интерпретируют `filling` как редактируемую/возобновляемую анкету.

Если snapshot сохранён, но публикация через outbox ещё pending/failed,
application уже не должна позволять `redo` и изменение ответов. Текущего
состояния для этой границы нет. Если оставить status=`filling`, Rule AD-3 и
Mutation convention противоречат друг другу; если сразу поставить `added`,
сломается обещание AD-5.

**Требуемое исправление spine:** зафиксировать таблицу переходов отдельно для:

- нового applicant: draft → confirmed snapshot/vouch publication → pending →
  vouched → added/final intro publication;
- refresh: draft → confirmed/publication pending → published → current.

Названия могут быть колонкой `status`, отдельным `confirmed_at`/publication
status или состоянием outbox, но правило изменения ответов должно проверять
конкретный durable predicate, а не двусмысленное слово `filling`.

### H-3 — AD-6 не задаёт enforceable механизм защиты от повторной публикации

**Spine:** callback, vouch и publication должны нести `application_id`; handler
отклоняет stale ID и не повторяет действие при сохранённом message ID.

**Реальность:** `VouchCallback` уже несёт `application_id`, но
`ConfirmCallback` несёт только `action` и получает application из текущего FSM
(`bot/keyboards/inline.py:7-16,45-58`;
`bot/handlers/questionnaire.py:228-238`). Два одновременно обработанных confirm
могут оба выполнить внешний send до сохранения message ID. Handler-транзакция
коммитится только после возврата обработчика
(`bot/middlewares/db_session.py:18-25`).

Кроме того, у одной application уже есть
`questionnaire_message_id` для карточки поручительства, а spine добавляет
`published_intro_message_id`; фраза «Telegram message ID хранится у породившей
его версии» не различает эти две публикации.

**Требуемое исправление spine:**

- `ConfirmCallback` включает `application_id` и проверяет владельца;
- stale определяется конкретным predicate состояния/version pointer;
- outbox имеет уникальный idempotency key минимум `(application_id,
  publication_kind)`;
- отдельно названы `vouch_card` и `member_intro`/`updated_intro` publication
  identities;
- повторное нажатие после enqueue является no-op, а не создаёт вторую outbox
  row.

## Medium

### M-1 — AD-1/AD-2 не фиксируют brownfield migration и уникальность полей версии

**Реальность:** `QuestionnaireAnswer.application_id` nullable, identity поля
сегодня — `question_index` + сохранённый `question_text`, `field_id` отсутствует,
а уникального ограничения на `(application_id, question_index)` нет
(`bot/db/models.py:163-176`; initial migration сохраняет ту же форму).
`save_answer()` всегда делает INSERT. При двух доставках одного шага одна версия
может получить два значения, после чего `{question_index: answer}` выбирает
последнее по порядку загрузки без формального контракта.

**Исправление:** зафиксировать deterministic mapping legacy indices `0..6` к
семи `field_id`, политику nullable legacy rows и DB uniqueness для новых
application-bound ответов. Deferred про «не угадывать неоднозначные
legacy-ответы» не мешает безопасному mapping известного индекса; неоднозначные
строки можно оставить legacy.

### M-2 — переход Google Sheet в one-way projection требует явного удаления pull

**Реальность:** `full_sync()` сейчас:

1. отправляет только `Intro` без `sheets_row_number`;
2. вызывает `sync_all_from_sheet()`;
3. pull выбирает ответы по `user_id + is_current`, изменяет
   `QuestionnaireAnswer.answer_text`, `Intro.intro_text` и
   `vouched_by_name`
   (`bot/services/sheets.py:217-329,332-381`).

AD-4 запрещает это, но implementation boundary должен назвать прямое действие:
удалить/вывести из scheduler `sync_all_from_sheet`, заменить фильтр
`sheets_row_number IS NULL` на durable projection current version и формировать
row из application-bound immutable answers. Иначе минимальная реализация может
лишь добавить guard к pull и сохранить исходную двунаправленность.

### M-3 — монотонное уточнение реферера реализуемо, но путь prefill/skip не указан

`normalize_referral_username()` уже отличает конкретный Telegram username от
общего legacy-текста и нормализует bare name, `@name` и `t.me/name` в lowercase
`@name`. Значит отдельная зависимость или таблица рефереров не нужна.

Но FSM сегодня всегда идёт по фиксированным семи `STATES_LIST`. Для AD-7 нужно
зафиксировать: конкретный referral копируется в новую application как её
собственный answer до preview, состояние q3 пропускается; общий/невалидный
legacy referral не копируется, поэтому q3 задаётся. Это важно и для resume после
рестарта.

## Low / verified facts

- **Версии Stack подтверждены:** Docker использует Python 3.12; `uv.lock`
  содержит aiogram 3.28.2, SQLAlchemy 2.0.49, APScheduler 3.11.2 и gspread
  6.0.2. Version drift не найден.
- PostgreSQL 16 — существенная часть реальности (durable outbox, locking), но не
  указан в Stack. Добавить pinned `PostgreSQL 16`, подтверждённый runtime
  compose/config.
- AD-1 совместим с большинством текущих Telegram-readers: preview, join и resume
  уже передают `application_id`. Главный нарушитель — Sheet pull и глобальный
  `mark_not_current`.
- AD-2 корректно нацелен на реальный drift: `QUESTIONS`,
  `INTRO_TEMPLATE`, `HEADERS` и `_Q_INDEX_TO_COL` сейчас четыре отдельных
  определения.
- Новых Python-зависимостей для предложенной архитектуры не требуется.

## Reality matrix

| Commitment | Current reality | Verdict |
| --- | --- | --- |
| AD-1 application-bound answers | Колонка есть; nullable, глобальный `is_current` ещё используется | Feasible, migration detail required |
| AD-2 one field catalog | Четыре независимых определения | Correct target |
| AD-3 immutable snapshot | Snapshot и durable confirmation boundary отсутствуют | State decision required |
| AD-4 Sheet projection only | Текущий scheduler bidirectional, Sheet writes DB | Direct removal/rewrite required |
| AD-5 publish before current switch | Текущий код переключает до send и глотает ошибку | Outbox required by ADR-0018 |
| AD-6 version-bound actions | Vouch bound; confirm not bound; final message ID absent | Callback + outbox identity required |
| AD-7 monotonic referral | Existing normalizer is sufficient | Prefill/skip path required |

## Gate recommendation

Перед Finalize применить C-1 и H-1..H-3 в самом spine. M-1..M-3 можно исправить
там же короткими migration/transition rules. После этого повторить reality и
rubric review; реализацию до устранения Critical не начинать.

---

## Recheck после amendments

**Recheck-вердикт:** **REVISE — Critical снят, но остаются два High.**

### Что исправлено

- **C-1 снят.** ADR-0018 теперь явно унаследован; Postgres outbox входит в
  парадигму, AD-3/AD-5/AD-9 и Structural Seed. Outbox больше не отложен целиком.
- **H-1 снят по ownership.** `Application.confirmed_intro_text` — immutable
  snapshot; `Intro.application_id` — current pointer;
  `Intro.intro_text` явно назван только compatibility-проекцией snapshot, а
  Sheet лишён права менять его.
- **H-2 снят.** Добавлен durable `confirmed` boundary: confirm проверяет
  `status=filling`, digest и полноту полей, в одной транзакции сохраняет
  snapshot, меняет status и enqueue-ит effect. После этого answers immutable.
- **H-3 частично снят.** Confirm и effects application-bound, publication kinds
  различены, unique `(application_id, effect_kind)` блокирует повторный enqueue,
  ambiguous timeout переводится в `unknown`.

### Remaining High

#### RH-1 — process-crash после `processing` claim не классифицирован

AD-5 требует durable `processing` claim до IO, а AD-6 переводит в `unknown`
только неоднозначный Telegram timeout. Но процесс может завершиться:

- после commit claim и до вызова Telegram — effect фактически не отправлен;
- после успешного Telegram send и до success-транзакции — effect отправлен, но
  `message_id` и продвижение `Intro` не записаны.

В обоих случаях в БД останется `processing`. Structural Seed не содержит
`claimed_at`/`last_attempt_at`, а rules не задают recovery для stale
`processing`. Слепой перевод обратно в `pending` создаст повтор во втором
случае; отсутствие reaper навсегда остановит публикацию в обоих.

Это та же residual Telegram crash-window, которую прежний review требовал
назвать явно. Она не устранима outbox-ом, но должна быть enforceable:

- outbox хранит claim/attempt timestamp согласно ADR-0018;
- stale `processing` после process restart/lease expiry становится `unknown`,
  а не автоматически `pending`;
- `unknown` не продвигает `Intro` и проходит operator reconciliation.

После фикса остаётся честно принятый manual-reconciliation risk, а не
неопределённое зависшее состояние.

#### RH-2 — нет brownfield-пути к обязательному `Intro.application_id`

Новая модель требует, чтобы каждый текущий `Intro` указывал на application, а
AD-7 читает referral только через этот pointer. В текущей схеме pointer
отсутствует, `QuestionnaireAnswer.application_id` допускает NULL, и существуют
два legacy-сценария, которые spine не маршрутизирует:

1. существующий `Intro` без однозначно связанной application;
2. текущий `/start` для уже состоящего в чате пользователя без `Intro`
   (`bot/handlers/start.py`) — ему не нужны admission/vouch, но
   `flow_kind` допускает только `admission` или `refresh`.

Нельзя передать это implementation story как «точные имена колонок»: выбор
определяет state machine и сохранность production data. Spine должен решить:

- как additive migration создаёт/выбирает application для каждого legacy
  `Intro` без угадывания отдельных answers (безопасный вариант — synthetic
  confirmed/added application со snapshot из текущего `Intro.intro_text`);
- как называется и проходит member-without-intro flow, либо явно определить
  refresh с nullable `base_application_id` и публикацией без vouch;
- когда `Intro.application_id` становится non-null и reader-ы переключаются с
  compatibility text на snapshot.

### Recheck gate

После RH-1 и RH-2 spine можно передавать на повторный linter/rubric gate.
Других Critical/High по четырём перепроверенным обязательствам не осталось.

---

## Final recheck

**Вердикт:** **PASS по Critical/High; 2 Medium autofix перед Finalize.**

### Закрытые High

- **RH-1 закрыт:** outbox теперь хранит `attempt_started_at`; stale
  `processing` reaper переводит в `unknown` без автоматического retry.
  Reconciliation-путь определён.
- **RH-2 закрыт:** `Intro.application_id` мигрируется nullable без
  эвристического backfill; legacy rewrite задаёт все поля и связывает pointer
  только после новой успешной публикации; member-without-intro получает
  refresh-путь без vouch.

### Remaining Medium

#### FM-1 — Structural Seed расходится с принятыми именами и brownfield-типами

- AD-3 и conventions называют snapshot `snapshot_html`, а ER —
  `confirmed_intro_text`.
- Текущий `Application.id` и `QuestionnaireAnswer.application_id` имеют тип
  `Integer`, но ER показывает `bigint`.
- AD-10 требует nullable `Intro.application_id`, но ER-связь
  `INTRO }o--|| APPLICATION` обозначает обязательную application.

**Autofix:** выбрать одно имя snapshot и использовать его везде; показать
`int` для application IDs; заменить связь на optional-to-one
`INTRO }o--o| APPLICATION`.

#### FM-2 — не определён `catalog_version` для уже незавершённых applications

AD-2 фиксирует версию каталога при создании новой application, но additive
migration встретит существующие `filling`/активные applications без этого
значения. Без backfill `/start` resume не сможет однозначно выбрать вопросы,
порядок и renderer.

**Autofix:** явно назначить существующим незавершённым applications
поддерживаемую legacy/current catalog version и держать её доступной до их
terminal state; новые applications получают `intro-v2`.

После этих двух точечных правок reality gate — **PASS**.

---

## Final recheck 2

**Вердикт:** **PASS по Critical/High; 1 Medium cutover autofix.**

Исправлены единое имя `confirmed_intro_html`, текущие `int` application FK,
nullable `Intro → Application` и правила catalog migration для
`filling`/`pending`/`vouched`.

### Remaining Medium — активный `privacy_block` выпал из migration cohort

`ApplicationRepo.get_active()` считает `privacy_block` активной application.
После пользовательского retry она возвращается в `vouched`, а при join должна
опубликовать тот же application-scoped frozen HTML. AD-10 назначает
`legacy-v1` и frozen HTML только `pending`/`vouched`; существующая
`privacy_block` останется без контракта snapshot/catalog.

**Autofix:** включить `privacy_block` в тот же migration rule, что
`pending`/`vouched`, и явно сказать, что deterministic mapping legacy
`question_index 0..6 → field_id` выполняется для этой active cohort до
установки unique `(application_id, field_id)`.

После этой одной правки reality gate — **PASS**.

---

## Final recheck 3

**Вердикт: PASS.**

Последний cutover-gap закрыт:

- `privacy_block` входит в frozen `legacy-v1` cohort вместе с
  `pending`/`vouched`;
- mapping `question_index 0..6 → field_id` задан явно;
- legacy `filling` обрабатывается fail-closed: прежние ответы исключаются из
  active-set, application получает `intro-v2`, resume начинается с q1.

Оставшихся Critical, High или Medium findings по reality lens нет.
