# Data-integrity review

**Verdict:** CHANGES REQUIRED. Spine правильно фиксирует версионирование анкеты,
неизменяемый snapshot и приоритет PostgreSQL, но пока не задаёт безопасную
транзакционную границу между DB и Telegram/Google Sheet и прямо противоречит
унаследованному ADR-0018.

Механический BMAD lint проходит без замечаний. Ниже только семантические
расхождения, влияющие на целостность данных и повторяемость действий.

## Critical

### DI-1 — Transactional outbox нельзя оставлять в Deferred

**Finding.** `Deferred` откладывает transactional outbox, а AD-5 предлагает
сначала отправить Telegram-сообщение и затем одной транзакцией переключить
`Intro`. Это противоречит принятому
`docs/memory-system/decisions/0018-eventing-strategy-postgres-rows.md`: DB
mutation и исходящие Telegram/Sheet effects должны связываться через
PostgreSQL outbox и APScheduler worker.

Прямой `sendMessage` между DB-операциями оставляет два окна:

1. процесс падает после успешной отправки, но до commit — повтор создаёт
   дубликат;
2. DB уже считает переход совершённым, но отправка не произошла — публикация
   теряется без durable work item.

**Smallest complete fix — autofix.**

- Добавить ADR-0018 в `Inherited Invariants`.
- Удалить outbox из `Deferred`.
- Ввести одну feature-scoped таблицу `intro_outbox`, а не новый сервис или
  универсальную event-платформу. Минимальные поля: `application_id`,
  `effect_kind`, `status`, `attempt_count`, `last_attempt_at`, `last_error`,
  Telegram message identity при успехе; уникальность
  `(application_id, effect_kind)`.
- В одной DB-транзакции фиксировать domain transition, immutable snapshot и
  outbox row. Существующий in-process APScheduler worker забирает rows через
  `FOR UPDATE SKIP LOCKED`.
- Через этот outbox проводить только durable business effects данного флоу:
  карточку поручительства, финальное/обновлённое интро и Sheet projection.
  Приватные callback acknowledgements не требуют отдельной очереди.

Это переиспользует уже принятый проектом паттерн (`InviteOutbox`) и не требует
broker, Celery или новой зависимости.

## High

### DI-2 — AD-5/AD-6 не определяют durable claim и точные commit boundaries

**Finding.** Проверка «message ID уже сохранён» защищает только от повтора
после записанного успеха. Два callback/job либо два join update могут
одновременно увидеть `message_id IS NULL` и оба вызвать Telegram. Фраза
«публикуется до смены текущей версии» также не говорит, где заканчивается
транзакция; текущий middleware коммитит только после завершения handler, то
есть claim внутри handler не durable во время сетевого вызова.

**Smallest complete fix — autofix.** Зафиксировать один publication protocol
для карточки поручительства, join-intro и refresh-intro:

1. **Domain transaction:** CAS ожидаемого статуса, валидация полного набора
   `field_id`, сохранение snapshot и unique outbox row; commit до сетевого
   вызова.
2. **Claim transaction:** worker атомарно переводит один row
   `pending -> processing`, записывает attempt metadata и коммитит claim.
3. **Effect:** worker отправляет сохранённый snapshot.
4. **Success transaction:** сохраняет Telegram `chat_id/message_id`, завершает
   outbox row и выполняет зависящий от публикации переход. Для refresh именно
   здесь `Intro.application_id` переключается на новую версию и создаётся
   Sheet effect.

Повторный callback только возвращает состояние уже существующего outbox row;
повторный worker не создаёт второй row.

Для неоднозначного Telegram outcome (`timeout` или crash после фактической
отправки до записи message ID) нельзя обещать exactly-once: Bot API не даёт
idempotency key для `sendMessage`. Stale `processing` должен переходить в
`unknown`, не автоматически переотправляться; оператор сверяет чат и
прикрепляет message ID либо явно разрешает повтор. Заведомо неотправленные
ошибки могут возвращаться в `pending`. Это меньше и честнее, чем outbox с
безусловными retry, который создаёт дубликаты.

### DI-3 — Тип версии хранится только в FSM, поэтому refresh нельзя надёжно продолжить

**Finding.** SPEC обещает продолжение незавершённого draft, но Structural Seed
не хранит, является Application заявкой на вступление или переписью. В
текущем brownfield-флоу `is_existing_member/is_refresh` находятся только в
FSM data. После рестарта или потери FSM одна и та же `Application(status =
filling)` не определяет, нужно ли создавать карточку поручительства или
сообщение «Обновлённое интро».

Двойной `/refresh` также может создать два `filling` draft; AD-6 связывает
callback с `application_id`, но не определяет, какой draft ещё разрешено
подтвердить.

**Smallest complete fix — autofix.**

- Хранить на `Application` обязательный purpose/kind:
  `admission | refresh`.
- Разрешать не более одного активного `filling` draft на пользователя и
  purpose: повторная команда возобновляет существующий draft либо явно
  закрывает его перед созданием нового.
- Confirm callback содержит `application_id`; CAS разрешает подтверждение
  только активного `filling` draft нужного пользователя. Ни FSM, ни
  `User.is_member` не являются источником purpose.

### DI-4 — Join и Sheet projection требуют явного порядка восстановления

**Finding.** AD-6 перечисляет vouch и publication, но не определяет, что
происходит, если join принят, а финальное интро не отправлено. Если join
handler сразу выставит `added`, а Telegram упадёт, нового join event может не
быть и интро останется потерянным. Для Sheet не задана защита от delayed
effect старой версии: ретрай S2 после перехода на S3 способен вернуть строку
к S2.

**Smallest complete fix — autofix.**

- Join transaction фиксирует membership/join evidence и создаёт
  `new_member_intro` outbox row; `added` и текущий `Intro` завершаются только
  в success transaction публикации.
- Sheet effect создаётся только после смены текущего `Intro`.
- Sheet worker перед записью повторно читает `Intro.application_id`; effect
  не для текущей версии помечается `superseded`. Один worker обрабатывает
  effects последовательно, retry пишет всю строку идемпотентно.
- Пятиминутный scheduler может оставаться reconciliation sweep, но не читает
  значения из Sheet и не обходит outbox при записи.

## Scenarios that must become executable checks

Минимальный набор проверок реализации:

1. два confirm callback для одной Application создают один snapshot и один
   outbox row;
2. crash/timeout в Telegram оставляет старый refresh-intro текущим, а effect —
   в `unknown`, без автоматической второй публикации;
3. два join update создают одну публикацию;
4. stale Sheet effect S2 после перехода на S3 не изменяет строку;
5. рестарт между вопросами возобновляет refresh как refresh и не отправляет
   его на поручительство.

## Conclusion

AD-1—AD-4 и AD-7 можно сохранить. AD-5/AD-6 следует переписать вокруг
унаследованного Postgres-outbox протокола, добавить persistent Application
purpose и явные recovery rules. После этих правок spine будет достаточно
полным без отдельного event bus и без требования недостижимого exactly-once
от Telegram.

---

## Recheck after amendment

**Verdict:** CHANGES REQUIRED — существенно исправлено, осталось два High.

### Закрытые findings

- **DI-1 resolved:** ADR-0018 унаследован явно; Postgres-outbox больше не
  отложен.
- **DI-2 mostly resolved:** confirm создаёт effect атомарно, claim коммитится
  до IO, success-транзакция фиксирует Telegram identity и только после
  refresh-публикации продвигает `Intro`.
- **DI-3 resolved:** `flow_kind`, `base_application_id`, partial uniqueness и
  version-bound confirm переживают потерю FSM и отделяют admission от refresh.
- **DI-4 mostly resolved:** join имеет durable final-intro effect, а
  Sheet-effect создаётся только после продвижения `Intro` и проверяет текущую
  application.

### Remaining High

#### DI-R1 — Crash оставляет `processing` без определённого recovery

AD-6 переводит неоднозначный Telegram timeout в `unknown`, но process crash
после durable claim — до отправки, во время отправки или после успешной
отправки до success commit — не выполнит этот переход. В Structural Seed у
outbox нет обязательного `last_attempt_at/processing_started_at` и
`last_error`, поэтому stale `processing` невозможно надёжно обнаружить и
аудировать. Это также не полностью выполняет ADR-0018, который требует
attempt metadata.

**Autofix:** добавить в AD-6/Structural Seed обязательные
`attempt_count`, `last_attempt_at` (либо `processing_started_at`) и
`last_error`; APScheduler reaper переводит stale `processing` Telegram-effect
в `unknown` и поднимает operator alert, но не переотправляет его. Для
идемпотентного Sheet-effect stale `processing` можно вернуть в `pending`.

#### DI-R2 — Sheet pre-check имеет TOCTOU без правила сериализации

AD-9 проверяет `Intro.application_id` перед удалённой записью, но не запрещает
параллельную обработку effects. Возможная последовательность:

1. S2 Sheet-worker проверил, что current = S2;
2. S3 Telegram-effect продвинул `Intro` и Sheet-effect S3 успел записать S3;
3. задержанный remote write S2 завершился последним и оставил Sheet на S2.

Тогда правило AD-9 формально выполнено в момент pre-check, но итоговая строка
устарела.

**Autofix:** закрепить текущую простую эксплуатационную модель: один
APScheduler intro-outbox job с `max_instances=1`, effects обрабатываются
последовательно по outbox ID, без concurrent batch. Поскольку все продвижения
`Intro` тоже выполняет этот worker, pre-check AD-9 становится достаточным.
Per-user locks или distributed coordination не нужны при унаследованном
single-process deployment.

### Recheck conclusion

Admission/refresh separation и join publication теперь достаточны. После
добавления crash reaper metadata и явной последовательной обработки outbox
семантических blocking/high замечаний не останется.

---

## Final recheck

**Verdict: PASS.**

- **DI-R1 resolved:** durable claim сохраняет attempt metadata; stale
  `processing` переводится reaper-ом в `unknown` без слепой повторной
  Telegram-публикации; operator reconciliation определён.
- **DI-R2 resolved:** один последовательный APScheduler worker с
  `max_instances=1` сериализует продвижение `Intro` и Sheet effects, поэтому
  pre-check AD-9 больше не имеет конкурентного stale-write окна в принятом
  single-process deployment.
- Admission и refresh различаются persisted `flow_kind`; restart не меняет
  маршрут версии.
- Join фиксирует durable final-intro effect, а текущий `Intro` и Sheet
  следуют только за успешной публикацией.

Blocking, High и Medium findings не осталось. Механический BMAD lint также
проходит.
