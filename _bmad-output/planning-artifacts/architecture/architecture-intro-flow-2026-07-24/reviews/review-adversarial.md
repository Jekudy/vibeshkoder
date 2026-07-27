# Adversarial review — Architecture Spine «Единый флоу интро»

Дата: 2026-07-24
Линза: независимые implementation units, каждая формально соблюдает все AD
Проверяемый файл: `../ARCHITECTURE-SPINE.md`

## Вердикт

**BLOCKED FOR HANDOFF.** Основной контракт верен, но spine пока не фиксирует
identity и переходы на четырёх ключевых seams: подтверждение snapshot,
publication effect, promotion текущей версии и уточнение referral. Независимые
units могут соблюдать все семь AD и всё равно опубликовать не тот preview,
переключить `Intro` на проигравшую refresh-версию, принять разные решения о
повторной отправке и выгрузить в Sheet разные версии.

## Attack model

Review исходит из того, что без дополнительного общения работают шесть units:

1. questionnaire/confirmation;
2. Telegram publication;
3. vouch callback;
4. join/promotion;
5. Sheet projection;
6. referral carry/refinement.

Каждая unit знает только spine, получает допустимые данные и не нарушает
буквальный текст AD. Ниже приведены пары реализаций, которые всё равно дают
разный результат.

## Critical

### A-1 — `application_id` не различает вид флоу и этап версии

**Constructed divergence**

- Unit A после confirmation переводит любую `Application` в `pending`: для неё
  это означает «snapshot подтверждён».
- Unit B считает `pending` только заявкой нового участника и показывает vouch
  card; refresh она распознаёт по наличию `Intro` либо данным FSM.
- Unit C после рестарта видит ту же `pending` application, но уже не имеет
  `is_refresh` из FSM, поэтому обрабатывает её как новую заявку.

Все units работают по `application_id`, сохраняют immutable snapshot и не
трогают Sheet. Ни один AD не требует сохранить вид флоу в PostgreSQL и не задаёт
полную state machine.

Отдельный race остаётся между двумя refresh S2 и S3 от одного S1. Обе версии
могут успешно отправить новые сообщения и затем по AD-5 переключить один
`Intro`; последняя транзакция может сделать текущей более старую S2. Формулировка
«reject stale ID» в AD-6 не определяет, относительно чего версия stale.

**Impact**

- refresh может уйти в поручительство;
- после рестарта невозможно надёжно продолжить подтверждённую, но ещё не
  опубликованную перепись;
- два корректных publisher могут создать два «Обновлённых интро» и выбрать
  текущую версию по порядку commit, а не по продуктовой версии.

**Disposition: autofix before handoff.**

Spine должен закрепить:

- persisted `flow_kind = admission | refresh`;
- исчерпывающие допустимые переходы для каждого kind;
- один non-terminal draft на пользователя;
- для refresh — `base_application_id`, равный
  `Intro.application_id` на старте;
- promotion только через CAS
  `Intro.application_id: base_application_id → application_id`; проигравшая
  версия не становится текущей.

### A-2 — Deferred outbox противоречит принятой ADR-0018

**Evidence**

ADR-0018 §6 требует outbox для операции, которая меняет БД и выполняет внешний
эффект, прямо называя Telegram и Google Sheets. Spine, напротив, откладывает
transactional outbox, а AD-5 предписывает сначала `sendMessage`, затем сохранить
message identity и переключить `Intro`.

**Constructed divergence**

- Unit A получает успешный ответ Telegram, падает до commit и на retry отправляет
  второе сообщение, потому что message ID не записан.
- Unit B после того же неопределённого исхода запрещает retry и навсегда оставляет
  S1 текущей.
- Unit C успевает переключить S2 и планирует Sheet update в памяти; процесс
  падает, Sheet остаётся на S1.

Все варианты совместимы с текущими AD: AD-6 запрещает повтор только после
**записанного** успеха, а policy для ambiguous outcome отсутствует.

**Impact**

Split-brain между Telegram, PostgreSQL и Sheet; Deferred ослабляет inherited ADR,
что запрещено reviewer gate.

**Disposition: autofix before handoff.**

Минимальный repair без event bus и новой зависимости:

1. переиспользовать существующий brownfield-паттерн `InviteOutbox`, но не его
   доменную таблицу;
2. завести одну feature-scoped durable очередь эффектов с unique logical key
   `(application_id, effect_kind)`, где достаточно двух kinds:
   `telegram_intro` и `sheet_projection`;
3. confirmation/promotion создаёт effect row в той же DB transaction, что
   canonical mutation;
4. APScheduler worker claim-ит rows через status/lease, хранит
   `attempt_count`, `last_error`, `chat_id`, `message_id`;
5. Sheet effect перед записью проверяет, что `Intro.application_id` всё ещё
   равен job `application_id`; stale job становится `superseded`;
6. Telegram timeout/crash после возможного `sendMessage` переводится в
   `ambiguous` и **не** получает автоматический resend. Без Telegram
   idempotency key outbox даёт durable intent, но не может доказать exactly-once;
   ambiguous row требует operator reconciliation.

Это закрывает ADR-0018 самым малым локальным механизмом. Обобщённый event
framework, Kafka/Celery и исторический registry всех Telegram messages не нужны.

### A-3 — Подтверждение не связано с конкретным preview

**Constructed divergence**

- Unit A показывает preview P1, затем по старой callback-кнопке читает свежие
  answers той же application и сохраняет snapshot P2.
- Unit B сохраняет текст P1 при показе preview и подтверждает его.

Обе реализации используют один `application_id`, нормализуют до preview и после
confirmation делают snapshot immutable. AD-1 и AD-3 не различают ревизии draft
внутри одной application; AD-6 относится к downstream actions.

Это воспроизводимо не только параллельной доставкой: `redo` и повторные сообщения
могут изменить answers, пока старая Telegram-кнопка всё ещё кликабельна.

**Impact**

Пользователь подтверждает не те байты, которые видел. Это нарушает CAP-2, хотя
все существующие AD формально соблюдены.

**Disposition: autofix before handoff.**

Confirmation identity должна включать `application_id + draft_revision`
(или hash/token сохранённого preview). Confirmation выполняется CAS только если
revision совпадает, а snapshot сохраняет именно уже показанный пользовательский
блок. Старая кнопка получает явный stale response. Один `application_id` в FSM
для этого недостаточен.

### A-4 — Одна Telegram message identity неоднозначна

**Constructed divergence**

Одна admission application порождает как минимум:

- questionnaire/vouch card;
- финальное intro после вступления.

Refresh application порождает `updated_intro`. Structural Seed предлагает одно
`published_intro_message_id`, а AD-6 говорит, что message ID хранится «у
породившей его версии». Unit A считает этим ID vouch card, Unit B — финальное
intro. После первого записанного ID одна unit подавит вторую публикацию как
duplicate; другая перезапишет ID и потеряет identity первого эффекта.

Кроме того, Telegram `message_id` уникален только внутри `chat_id`. Один bigint
не является полной identity публикации.

**Impact**

Неверная idempotency, удаление/редактирование не того сообщения и расхождение
между vouch/join handlers.

**Disposition: autofix before handoff.**

Зафиксировать logical publication key
`(application_id, publication_kind)` и физическую identity
`(chat_id, message_id)`. Для текущего объёма достаточно явных известных kinds;
отдельный «исторический реестр интро» не требуется.

## High

### A-5 — Referral refinement не имеет однозначного владельца и источника

**Constructed divergence**

- Unit A берёт referral из последней по времени `Application`.
- Unit B берёт referral из application, на которую указывает текущий `Intro`.
- Unit C хранит referral как изменяемую user-level admission metadata и при
  уточнении меняет её для всех readers.

Каждая unit может соблюдать монотонность «общее → конкретный `@username`» и не
заменять конкретный ник обычным refresh. Но при брошенном или ошибочно
синхронизированном draft они выберут разные исходные значения. Unit C также
обходит immutable history, хотя старый rendered snapshot формально не меняет.

**Impact**

Именно legacy-значение вроде «От участника чата» снова может победить конкретный
ник или оказаться переписанным задним числом.

**Disposition: autofix before handoff.**

Spine должен сказать буквально:

- источник carry/refinement — field `referral` application, на которую указывает
  текущий `Intro`;
- concrete referral копируется только в **новую** refresh application;
- general referral задаётся заново и уточнение записывается только в новую
  версию; предыдущие answers/snapshot не меняются;
- legacy `Intro` без надёжного application pointer не парсится эвристикой:
  referral спрашивается заново.

Predicate `general | concrete` и normalization принадлежат единому field catalog.

### A-6 — Snapshot не задаёт формат хранения и версию каталога

**Constructed divergence**

- Unit A хранит в `confirmed_intro_text` HTML-escaped Telegram markup.
- Unit B хранит raw normalized values и экранирует на каждой поверхности.
- Sheet unit в первом варианте может вывести `&amp;`, а Telegram unit во втором
  перерендерит старый snapshot после изменения labels.

Обе реализации используют «готовый пользовательский блок» и единый актуальный
каталог. Spine не фиксирует, что именно является canonical snapshot для старой
версии каталога.

**Impact**

Двойное экранирование, дрейф старых подписей и невозможность доказать, что Sheet
показывает те же значения, что подтверждённый Telegram block.

**Disposition: autofix before handoff.**

Минимальная граница:

- immutable normalized answer rows keyed by `field_id`;
- immutable Telegram-ready user block с явно заданным parse mode;
- `catalog_version` на application;
- Telegram surfaces используют сохранённый block, Sheet — immutable raw values
  той же application и headers каталога указанной версии.

### A-7 — Trigger и identity Sheet projection противоречат друг другу

**Constructed divergence**

Companion flow проводит Sheet от confirmed S1 до vouch/join. Spine diagram
проводит Sheet от `Intro current pointer`, которого до вступления ещё нет.

- Unit A экспортирует pending application сразу после confirmation.
- Unit B экспортирует только application, которая стала текущим `Intro`.

Обе считают, что проецируют «текущую подтверждённую версию». Для pending user
определение current отсутствует. Также не закреплено, является строка Sheet
identity пользователя или версии.

**Impact**

Разное число строк, разная видимость pending-заявок и возможность stale worker
записать S1 после promotion S2.

**Disposition: discuss, then fix both documents.**

Выбрать один trigger. Если существующий Sheet остаётся реестром опубликованных
интро, минимальная семантика — одна строка на `user_id`, effect создаётся после
успешного `Intro` promotion, payload identity — `application_id`, stale effects
supersede. Pending applications в таком Sheet не появляются. Если pending нужны,
это отдельная явно названная projection, а не вторая трактовка той же строки.

## Medium

### A-8 — Brownfield migration может снова «угадать» неправильную версию

Текущие `Intro` не имеют `application_id`, а legacy
`QuestionnaireAnswer.application_id` nullable. Structural Seed требует pointer,
но Deferred запрещает угадывать legacy answers. Один implementer привяжет Intro к
последней `added` application, другой оставит NULL, третий создаст synthetic
application.

**Disposition: autofix or explicit safe Deferred.**

Безопасный минимум: legacy `Intro.application_id` остаётся nullable и продолжает
читать сохранённый `intro_text`; первый успешно опубликованный refresh создаёт
полную новую version и устанавливает pointer. Миграция не выбирает «последнюю»
application без доказуемой связи.

### A-9 — Stale vouch требует не только ID, но и атомарный expected state

AD-6 требует отклонять stale ID, но не фиксирует precondition. Reader может
считать любую `pending` application допустимой, другой — только последнюю.
Параллельные callbacks должны соревноваться одним CAS
`pending → vouched`; только победитель создаёт vouch log и invite outbox в той же
transaction.

**Disposition: autofix in state-machine rule.**

## Gate condition

До implementation planning обязательны A-1–A-4. A-5 и A-6 нужны до реализации
referral и renderer. A-7 требует одного продуктового решения и синхронного
исправления spine/companion. A-8 можно оформить безопасным migration exception;
A-9 естественно входит в state machine из A-1.

---

## Recheck amended spine

Дата повторной проверки: 2026-07-24

### Вердикт

**STILL BLOCKED FOR HANDOFF.** Поправки закрыли основную identity-модель:
`flow_kind`, `base_application_id`, один активный refresh, preview digest,
logical/physical publication identity, referral от версии текущего `Intro` и
CAS promotion теперь названы явно. Остались две внутренние коллизии, которые
ослабляют эти правила, и два high-seam контракта.

### Что закрыто

| Previous finding | Recheck |
| --- | --- |
| A-1: нет `flow_kind` / base identity | **Partially closed:** поля и CAS появились |
| A-3: confirm не связан с preview | **Partially closed:** digest появился |
| A-4: publication identity неоднозначна | **Closed:** logical key и `(chat_id, message_id)` заданы |
| A-5: referral берётся из неизвестной версии | **Closed для versioned Intro:** источник — application текущего `Intro` |
| A-7: stale Sheet effect | **Closed в spine:** worker сверяет `Intro.application_id` |

### Remaining blocking

#### RC-1 — State names делают single-active constraint и confirm неоднозначными

AD-3 и AD-8 используют общие statuses `filling` / `confirmed`, и partial unique
constraint объявлен именно для них. Structural Seed одновременно вводит для
refresh отдельные statuses `refresh_filling` / `refresh_confirmed` /
`refresh_added`.

Две implementation units могут корректно выбрать разные схемы:

- одна хранит `flow_kind=refresh, status=filling`;
- другая хранит `status=refresh_filling`.

Во втором случае буквальный partial unique из AD-8 не действует, а AD-3 не может
confirm-ить refresh, поскольку ожидает `status=filling`.

В верхней flowchart также осталось прямое ребро `Confirmed snapshot → Intro
current pointer`, противоречащее AD-5: pointer должен меняться только после
успешной Telegram-публикации.

**Required repair:** выбрать одну state vocabulary. Минимум — общие statuses
`filling / confirmed / pending / vouched / added` плюс persisted `flow_kind`;
убрать `refresh_*` из диаграммы и прямое ребро `C --> I`. Partial unique и все
CAS должны ссылаться на тот же набор statuses.

#### RC-2 — Durable `processing` не имеет crash recovery semantics

AD-5 требует durable claim до IO, а AD-6 переводит в `unknown` только
«неоднозначный Telegram timeout». Падение процесса оставляет row в
`processing`; после рестарта worker не может отличить:

1. crash до `sendMessage`;
2. crash после успешного `sendMessage`, но до success transaction.

State diagram разрешает `processing → pending`, поэтому одна реализация
автоматически отправит дубль; другая навсегда оставит row зависшей. Оба
прочтения совместимы с amended spine.

**Required repair:** stale/expired `processing` всегда становится `unknown` и
не retry-ится автоматически; lease timeout, alert и operator reconciliation
должны быть частью Rule. Unambiguous exception, полученная без возможной
доставки, может вернуть row в `pending`.

### Remaining high

#### RH-1 — Digest и snapshot всё ещё не имеют общей byte-level формы

Digest назван, но не задан его input/encoding. Preview unit может хэшировать raw
answers, HTML-rendered block или весь текст вместе с инструкцией; confirm unit
может выбрать другое. Не определены также storage format
`confirmed_intro_text`, parse mode и версия каталога, по которой он собран.

**Required repair:** закрепить, например,
`SHA-256(UTF-8 exact user block)`; сохранять этот exact block с явным parse mode
и `catalog_version`. Telegram использует сохранённый block, Sheet — immutable
normalized answers той же application.

#### RH-2 — Companion всё ещё экспортирует admission до опубликованного Intro

AD-9 говорит: Sheet-effect создаётся после Telegram intro promotion. Но
`user-flow.md` по-прежнему содержит `Confirmed S1 → Google Sheet S1` до
поручительства и вступления. Одна implementation unit последует spine, другая —
его каноническому companion.

**Required repair:** если Sheet — реестр опубликованных интро, заменить ребро на
`Intro S1 → Sheet S1`. Если pending applications нужны в Sheet, оформить их
отдельной projection с отдельной identity.

---

## Final recheck

Дата финальной проверки: 2026-07-24

### Вердикт

**PASS — no blocking or high findings.** Amended spine теперь однозначно
связывает flow/version identity, показанный preview, durable effect, Telegram
publication, условный promotion и Sheet projection. Legacy rows не получают
эвристическую связь.

### Закрытые findings

- **RC-1 closed:** admission и refresh используют единые persisted statuses;
  `flow_kind` различает флоу, partial unique ссылается на тот же набор statuses,
  а прямое ребро confirmation → `Intro` удалено.
- **RC-2 closed:** claim сохраняет `attempt_started_at`; просроченный
  `processing` переходит в `unknown`, автоматический resend запрещён, дальнейшее
  действие требует operator reconciliation.
- **RH-1 closed:** `snapshot_html`, byte input digest, SHA-256 truncation,
  base64url encoding, HTML parse mode и `catalog_version` определены.
- **RH-2 closed:** admission Sheet projection теперь следует только после
  опубликованного `Intro`.
- **A-5/A-8 closed:** versioned `Intro` является единственным источником referral;
  legacy `Intro.application_id` остаётся nullable, все поля спрашиваются заново,
  а новая связь появляется только после успешной публикации.
- **Member-without-intro route closed:** тот же versioned refresh-flow
  публикует системный заголовок «Интро» и не требует ложного legacy backfill.

### Remaining medium

#### FM-1 — Retention каталога короче срока жизни текущего Intro

AD-2 гарантирует доступность `intro-v2`, пока на неё ссылается
**незавершённая** application. После статуса `added` эта application завершена,
но текущий `Intro`, delayed Sheet-effect или повторная Sheet reconciliation
продолжают ссылаться на её `catalog_version`.

Если `intro-v2` удалить при появлении следующей версии, Telegram сохранится за
счёт `snapshot_html`, но Sheet worker не сможет однозначно получить headers и
порядок raw fields старой текущей версии.

**Disposition: autofix or implementation constraint.** Хранить catalog version,
пока на неё ссылается любой текущий `Intro` или non-terminal outbox effect, а не
только незавершённая application. Для текущего `intro-v2` это не блокирует
handoff.

---

## Ultimate recheck

Дата проверки: 2026-07-24

### Вердикт

**CHANGES REQUIRED: one high, one medium.** Naming, cardinality,
`confirmed_intro_html`, nullable `Intro.application_id` и retention для текущего
`Intro`/outbox исправлены. Но migration существующего `filling` draft всё ещё
может законсервировать ровно тот Sheet rollback, который вызвал RFC.

### High

#### UC-1 — Legacy `filling` нельзя безопасно продолжить «с первого несовместимого поля»

AD-10 назначает существующим `filling` applications `intro-v2` и предлагает
resume с первого несовместимого поля. Синтаксическая/schema-валидация не
обнаруживает семантически устаревшее, но валидное значение: `London` валиден как
location, а «От участника чата» валиден как общий referral. Именно такие значения
Sheet уже мог записать поверх нового draft.

Две migration units получат разные анкеты:

- одна сохранит все формально совместимые legacy answers и продолжит дальше;
- другая очистит draft и спросит пользователя заново.

Первая снова покажет пользователю старые данные под новой версией `intro-v2`,
несмотря на запрет эвристического backfill в том же AD-10.

**Required repair:** все существовавшие до migration `filling` applications
считать недоверенными: удалить/деактивировать их answers и начать `intro-v2` с
первого вопроса либо закрыть legacy application и создать новую. Не переносить
даже формально валидные значения. Это также приводит AD-10 в соответствие с
companion-правилом «legacy rewrite задаёт все вопросы заново».

### Medium

#### UC-2 — Catalog retention пропускает admission в `pending`/`vouched`

AD-2 хранит каталог, пока есть `filling` application, текущий `Intro` или
незавершённый effect. Между успешной candidate publication и вступлением
admission находится в `pending`/`vouched`: первый effect уже завершён, `Intro`
ещё не создан. При удалении версии каталога финальный Sheet-effect после join не
сможет получить её headers и field order.

**Required repair:** retention predicate должен включать любую non-terminal
application, в том числе `confirmed`, `pending`, `privacy_block` и `vouched`,
плюс текущий `Intro` и unfinished effect.

---

## Closure recheck

Дата проверки: 2026-07-24

### Вердикт

**PASS — no remaining medium, high or blocking findings.**

- **UC-1 closed:** legacy `filling` answers выводятся из active-set, application
  получает `intro-v2`, а пользователь начинает с первого вопроса; семантически
  устаревшие `London` и «От участника чата» не переносятся.
- **UC-2 closed:** catalog retention охватывает все nonterminal application
  statuses, текущий `Intro` и незавершённый intro-effect.
- Naming `confirmed_intro_html`, nullable legacy cardinality, publication
  identity, CAS promotion, stale-processing recovery и Sheet trigger остаются
  согласованными между Rules, Structural Seed, SPEC и companion flow.

Adversarial gate закрыт; spine готов к implementation planning.
