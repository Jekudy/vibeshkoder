---
title: 'Versioned-флоу анкеты и интро'
type: 'feature'
created: '2026-07-24'
status: 'ready-for-dev'
baseline_revision: 'd9c2a21a6b23eb8057b285da18e9fb6c9073121a'
review_loop_iteration: 1
followup_review_recommended: false
context:
  - CLAUDE.md
  - _bmad-output/project-context.md
  - _bmad-output/specs/spec-intro-contract/SPEC.md
  - _bmad-output/specs/spec-intro-contract/user-flow.md
  - _bmad-output/planning-artifacts/architecture/architecture-intro-flow-2026-07-24/ARCHITECTURE-SPINE.md
warnings: []
---

<intent-contract>

## Intent

**Problem:** Google Sheet является вторым writer для ответов и интро: во время
`/refresh` он смешивает разные applications и возвращает старые значения. Вопросы,
preview, Telegram и Sheet также определяют поля независимо.

**Approach:** Сделать `Application + QuestionnaireAnswer` версией анкеты,
сохранять точный подтверждённый HTML snapshot и доставлять Telegram/Sheet эффекты
через узкий Postgres outbox. Postgres становится единственным владельцем данных;
Sheet остаётся однонаправленной проекцией.

## Boundaries & Constraints

**Always:** Все canonical reads/writes содержат `application_id`; confirm проверяет
owner, семь полей и digest preview; old Intro остаётся current до Telegram success;
callback/vouch/effect относятся к одной application; concrete referral переносится,
generic можно уточнить; Sheet никогда не пишет в application/answers/Intro; Telegram
timeout и stale claim становятся `unknown`; production deploy использует exact SHA,
backup, migration/health proof и single polling instance; успешный refresh завершает
открытый `IntroRefreshTracking`.

**Block If:** Migration видит неполную или duplicate legacy application в
`pending/vouched/privacy_block`; production backup не подтверждён; одновременно
запущено больше одного bot runtime; raw updates 19231–19237 не принадлежат
`@predko`/user `169419687`; release health/migration красные; recovery Telegram effect
получает `unknown`.

**Never:** Не мутировать application 176; не публиковать ручным `send_message` или
одним SQL; не угадывать legacy links; не редактировать и не удалять прежнее интро;
не менять concrete referral через обычный refresh; не возвращать Sheet pull; не
делать blind retry `unknown`; не добавлять broker, generic outbox framework или
catalog DB-table.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Admission | Полный draft S1 | Frozen S1 → candidate → vouch → final intro S1 | Stale callback отклоняется |
| Refresh | Current S1, draft S2 | Новое «Обновлённое интро» S2; затем promotion | Ошибка оставляет S1 current |
| Referral | Generic или concrete в current app | Generic спрашивается; concrete копируется и пропускается | Concrete→different запрещён |
| Sheet conflict | Old `Лондон` / `От участника чата` | DB не меняется; Sheet получает current app | Stale effect → `stale` |
| Ambiguous delivery | Telegram timeout / stale processing | Effect `unknown`, без повтора | Только operator reconcile |
| Legacy | Filling или active admission | Filling с q1; active admission frozen `legacy-v1` | Malformed cohort aborts migration |
| Predko remediation | Raw rows 19231–19237, admin authorization | Новая recovery refresh через standard confirm/outbox | App 176 не используется |
| Concurrent refresh | Два одновременных `/refresh` одного user | Одна active refresh application; второй запрос resume её | Partial unique + transaction retry |
| Member without Intro | Участник чата без строки `Intro` | Полная анкета q1…q7, публикация с заголовком «Интро» | Promotion требует отсутствия Intro |
| Legacy Intro | `Intro.application_id IS NULL` | Полная анкета q1…q7; Sheet не восстанавливает ответы | Promotion только если pointer всё ещё NULL |
| Unknown reconcile | `unknown` Telegram effect | Явный `record-sent` или доказанный `retry-absent` | Другие переходы запрещены |

</intent-contract>

## Normative Contracts

### Referral

`concrete` — только значение, для которого существующий
`normalize_referral_username()` успешно принимает `@name`, `name`, `t.me/name` или
`https://t.me/name` и возвращает lowercase `@name`. `NULL`, пустая строка и любое
сохранённое значение, которое normalizer отклоняет, считаются `generic`: при refresh
вопрос задаётся снова. Новый невалидный ответ отклоняется и пользователь остаётся на
третьем вопросе; новый произвольный generic-текст не принимается. Обычный refresh,
resume и redo не могут заменить уже concrete referral.

### Intro effect outbox

Допустимые `effect_kind`: `candidate_card`, `admission_intro`, `member_intro`,
`refresh_intro`, `sheet_projection`. Допустимые `status`: `pending`, `processing`,
`sent`, `unknown`, `failed`, `stale`. БД обеспечивает unique
`(application_id, effect_kind)`, а для записанного Telegram identity — partial unique
`(chat_id, message_id) WHERE message_id IS NOT NULL`.

- Ошибка до начала Telegram request однозначна: bounded retry возвращает effect в
  `pending`.
- Timeout, cancellation или потеря соединения после dispatch неоднозначны: effect
  становится `unknown` и автоматически не повторяется.
- Telegram `BadRequest`/`Forbidden` терминальны: effect становится `failed`.
- Sheet projection идемпотентна: retry bounded; несовпавший current pointer даёт
  `stale`.
- После Telegram success refresh promotion выполняется CAS: существующий
  `Intro.application_id` обязан равняться `base_application_id`; для legacy base
  требуется `Intro.application_id IS NULL`; для member без Intro строка `Intro`
  должна отсутствовать. CAS failure даёт `stale`, а не меняет current Intro.

`unknown` исправляется только audited-командой:

```text
python -m bot.cli intro_effect_reconcile \
  --effect-id <id> \
  --action record-sent \
  --chat-id <chat_id> \
  --message-id <message_id> \
  --operator-user-id <admin_user_id> \
  --reason <reason>

python -m bot.cli intro_effect_reconcile \
  --effect-id <id> \
  --action retry-absent \
  --evidence-sha256 <sha256> \
  --operator-user-id <admin_user_id> \
  --reason <reason>
```

`record-sent` использует тот же finalization/promotion path, что worker.
`retry-absent` разрешён только после зафиксированного доказательства отсутствия
публикации и делает единственный переход `unknown → pending`.

### Predko operator remediation

Команда запускается только после релиза migration 091 и требует, чтобы
`operator-user-id` входил в `ADMIN_IDS`/проходил `is_admin`:

```text
python -m bot.cli intro_recover_raw \
  --user-id 169419687 \
  --answer-row-ids 19231,19232,19233,19234,19235,19236,19237 \
  --source-confirm-row-id 19238 \
  --operator-user-id 149820031 \
  --authorize-operator-remediation \
  --expected-input-sha256 5762b931895dc1837abf75209055a86a1b56e3bcdcd472b3346bf2d01b2b1fd5 \
  --reason issue-484-predko-remediation
```

Значения 19231…19238 — DB primary keys `telegram_updates.id`, не Telegram
`update_id`; команда выбирает строки строго по `TelegramUpdate.id`, читает только
`telegram_updates.raw_json` и проверяет ровно семь
последовательных private text messages от user `169419687`, маппит порядок в
`name, location, referral, experience, projects, hardest, goals`, проверяет callback
row 19238 от того же user, но не объявляет его подтверждением восстановленного
snapshot. Затем domain renderer создаёт новую refresh application, frozen snapshot и
стандартный `refresh_intro` effect. Application 176 не читается как источник и не
изменяется. SHA256 считается от UTF-8 строки семи ответов, соединённых ровно одним LF.

Ожидаемый frozen body:

```text
👤 Имя: Сергей
📍 Основная локация: UK
🔗 От кого узнал о чате: @oxanagesina
💡 Опыт с вайб-кодингом: Есть реализованная система лидогенерации и авторассылок.
🚀 Проекты и автоматизации: Есть реализованная система лидогенерации и авторассылок.
🏋️ Самое сложное: Вот эту систему и сделал. И вокруг нее поднял всю необходимую инфраструктуру.
🎯 Цели: Хочу делать больше и сложнее. Чтобы автономность и надежность были высокими + решения были ценными и генерящими выручку.
```

## Code Map

- `bot/db/models.py` -- Application aggregate, Intro pointer, intro outbox.
- `alembic/versions/091_versioned_intro_flow.py` -- brownfield backfill and constraints.
- `bot/services/intro_contract.py` -- `legacy-v1`/`intro-v2` field catalog, renderer and digest.
- `bot/db/repos/application.py` -- application lifecycle and refresh CAS.
- `bot/db/repos/questionnaire.py` -- application/field-scoped answer writes.
- `bot/db/repos/intro.py` -- current published application pointer.
- `bot/db/repos/intro_effect_outbox.py` -- durable effect claims and terminal states.
- `bot/services/intro_workflow.py` -- refresh/resume/confirm and referral policy.
- `bot/services/intro_effect_worker.py` -- claim-before-IO delivery, promotion and reconciliation.
- `bot/handlers/start.py`, `bot/handlers/questionnaire.py` -- questionnaire and refresh entry points.
- `bot/handlers/chat_events.py`, `bot/handlers/vouch.py` -- version-bound admission transitions.
- `bot/services/sheets.py`, `bot/services/scheduler.py` -- projection-only Sheet and sequential worker wiring.
- `bot/keyboards/inline.py`, `bot/texts.py` -- application/digest callbacks and unified wording.
- `bot/cli.py` -- explicit unknown-effect reconciliation and audited predko recovery command.
- `SPEC.md`, `.github/workflows/ci.yml` -- target source-of-truth and migration head.
- `tests/intro/`, `tests/db/test_intro_flow_migration.py` -- RED→GREEN acceptance suite.

## Tasks & Acceptance

**Execution:**
- [ ] `tests/intro/test_contract.py`, `tests/db/test_intro_flow_migration.py` -- first RED batch for catalog, snapshot, constraints and legacy cutover.
- [ ] `bot/services/intro_contract.py`, `bot/db/models.py`, `alembic/versions/091_versioned_intro_flow.py` -- minimal schema/catalog GREEN without new dependencies.
- [ ] `tests/intro/test_confirmation.py`, `tests/intro/test_referral_refresh.py` -- RED for owner/digest/immutability/resume/redo/referral.
- [ ] `tests/intro/test_refresh_concurrency.py`, `tests/intro/test_legacy_member_flow.py` -- RED for concurrent `/refresh`, member without Intro and legacy Intro resume.
- [ ] `bot/db/repos/application.py`, `bot/db/repos/questionnaire.py`, `bot/db/repos/intro_effect_outbox.py`, `bot/services/intro_workflow.py`, `bot/handlers/start.py`, `bot/handlers/questionnaire.py`, `bot/keyboards/inline.py` -- application-scoped workflow GREEN.
- [ ] `tests/intro/test_admission_flow.py`, `tests/intro/test_refresh_flow.py`, `tests/intro/test_effect_worker.py`, `tests/intro/test_failure_safety.py` -- RED for downstream version binding and delivery safety.
- [ ] `tests/intro/test_effect_reconciliation.py` -- RED for `unknown` `record-sent`/`retry-absent`, audit fields and forbidden transitions.
- [ ] `bot/services/intro_effect_worker.py`, `bot/handlers/chat_events.py`, `bot/handlers/vouch.py`, `bot/services/scheduler.py`, `bot/db/repos/intro.py` -- transactional effect GREEN, completed refresh tracking and no direct canonical publication.
- [ ] `tests/intro/test_sheet_projection.py`, `tests/intro/test_predko_regression.py` -- RED for one-way Sheet and full incident regression.
- [ ] `bot/services/sheets.py`, `bot/texts.py`, `SPEC.md` -- delete inbound sync, derive surfaces from catalog and document Postgres ownership.
- [ ] `tests/intro/test_recovery_cli.py`, `bot/cli.py` -- fail-closed standard-path recovery/reconcile for raw evidence; no direct Telegram/SQL publication.
- [ ] `.github/workflows/ci.yml`, `tests/db/test_fts_schema.py`, `tests/db/test_digests_review_schema.py`, `tests/scripts/test_postgres_image_contract.py` -- update migration head; update PR #485 title/body and attach RED→GREEN evidence.
- [ ] `docs/ops/db-backup-runbook.md`, `.github/workflows/release.yml` -- execute fresh backup, merge, main CI, Release Images, exact-SHA Coolify deploy, migration/health/single-instance/10-minute proof.
- [ ] `bot/cli.py`, `bot/services/intro_workflow.py` -- execute @predko remediation: validate raw rows, create a new refresh version, standard confirm/outbox publish, verify Telegram identity/body, Intro pointer, Sheet projection and unchanged old message.

**Acceptance Criteria:**
- Given `intro-v2`, when any surface renders fields, then IDs, order, meaning and labels come from one catalog.
- Given two applications for one user, when reading/writing answers, then only the named application participates and duplicate field IDs are rejected by DB.
- Given a shown preview, when confirm has the same application and digest, then exact escaped LF-separated HTML is frozen once; stale, incomplete or wrong-owner confirm creates no effect.
- Given confirmed admission S1, when candidate/vouch/join complete, then every publication uses frozen S1 and duplicate callbacks/events do not duplicate effects.
- Given current S1 and confirmed refresh S2, when Telegram publication succeeds, then a new «Обновлённое интро» is recorded and only then S2 becomes current; on failure S1 remains current.
- Given an open refresh reminder cycle, when refresh publication promotes S2, then its `IntroRefreshTracking` is marked completed and no further stale reminder is sent.
- Given a generic referral, when refreshing, then a concrete username is accepted; given a concrete referral, then resume/redo preserve it and skip replacement.
- Given two concurrent `/refresh` commands, when both transactions create or resume a draft, then both resolve to one active refresh application and the DB partial unique constraint is authoritative.
- Given a current member without Intro, when they complete the questionnaire, then all q1…q7 are collected and the publication header is «Интро».
- Given a legacy Intro with no application pointer, when refresh starts, then all q1…q7 are collected without restoring Sheet values and promotion succeeds only while the pointer remains NULL.
- Given an edited or stale Sheet row, when scheduled work runs, then no canonical DB row changes and only the current published application is projected.
- Given a claimed Telegram effect times out or becomes stale, when the worker runs again, then status is `unknown` and no blind duplicate is sent.
- Given an `unknown` effect, when an admin records a found message, then the normal finalizer promotes it exactly once; when an admin proves absence, then only `unknown → pending` is allowed.
- Given legacy data, when migration 091 runs, then filling starts at q1, valid active admission is frozen as `legacy-v1`, and Intro pointers remain unguessed.
- Given old Sheet values plus fresh `UK`/`@oxanagesina`, when sync interleaves with filling and publication, then preview, Telegram, Intro and Sheet retain the fresh version.
- Given a green exact-SHA release, when operator remediation uses raw updates 19231–19237, then a new application publishes the authorized corrected intro through the same production outbox and application 176 remains untouched.
- Given the predko evidence, when remediation finishes, then the published user block equals the complete seven-line expected body byte-for-byte and the open `IntroRefreshTracking` row is completed.

## Spec Change Log

- 2026-07-24: repair loop 1 — зафиксированы referral predicate, outbox/reconcile,
  конкурентные и legacy-сценарии, fail-closed predko recovery и exact-SHA release.

## Review Triage Log

- 2026-07-24: senior pre-dev gate PASS; все шесть блокеров закрыты, дополнительных
  сущностей и абстракций не требуется.

## Design Notes

Migration 091 deliberately keeps `question_index`, `question_text`, `is_current` and
`Intro.intro_text` as compatibility fields. New business logic uses stable `field_id`,
`catalog_version`, `confirmed_intro_html` and `Intro.application_id`. The specialized
outbox borrows claim-before-IO from image memory, uncertainty handling from digest
publishing and scheduler wiring from invite delivery without creating a shared
framework.

Raw Telegram row 19238 proves a `confirm:yes` click but not the exact fresh preview:
the first Sheet pull may already have mixed it. The predko publication is therefore an
explicitly authorized operator remediation using intact raw inputs, not a claim that
the recovered snapshot was previously confirmed byte-for-byte.

## Release Protocol

1. Перед merge создать и проверить свежий backup:

   ```text
   ssh foodzy-vps 'sudo /usr/local/sbin/shkoder-pg-backup.sh && sudo tail -20 /var/log/shkoder-pg-backup.log'
   ssh foodzy-vps 'latest=$(sudo find /data/coolify/backups/shkoder-postgres -type f -name "*.dump" -print0 | sudo xargs -0 ls -1t | head -1); sudo pg_restore --list "$latest" >/dev/null'
   ```

2. При зелёном PR выполнить `gh pr merge 485 --squash`, получить
   `mergeCommit.oid` через `gh pr view 485 --json mergeCommit`, дождаться успешных
   `CI` и `Release Images` именно для этого SHA. Обязательный tag:
   `sha-<full_merge_sha>`.
3. Загрузить `COOLIFY_API_TOKEN` из `~/.env.tokens`, не печатая его, и выполнить:

   ```text
   PATCH ${COOLIFY_API_URL:-http://100.101.196.21:8100/api/v1}/applications/maiwn569gziz935wv0w7kcch
   JSON: {"docker_registry_image_tag":"sha-<full_merge_sha>"}

   POST ${COOLIFY_API_URL:-http://100.101.196.21:8100/api/v1}/applications/maiwn569gziz935wv0w7kcch/start?force=true
   ```

4. Доказать: OCI revision равен merge SHA, работает ровно один polling container с
   restart count 0, `alembic current` равен `091`, `/healthz` и `/healthz/db` дают
   200, в логах нет `POLL_CONFLICT_409` и ошибок intro worker. Повторить прямые
   проверки пять раз с интервалом 120 секунд.
5. После remediation добавить в PR #485 или issue #484 operator proof: merge SHA,
   release run URL, backup filename/check, deployment UUID, container/OCI/migration/
   health/soak evidence, recovery application/effect/chat/message IDs, exact body
   digest, Intro pointer, Sheet projection и completed tracking. Секреты не включать.

Rollback на старый image небезопасен: он вернёт destructive Sheet pull. При ошибке
миграции или health требуется forward-fix; старый image допустим только после
механического отключения inbound Sheet sync. Production schema не downgrade.

## Verification

**Commands:**
- `pytest -q tests/intro tests/db/test_intro_flow_migration.py` -- all feature ACs GREEN.
- `pytest -q` -- complete regression suite GREEN.
- `ruff check .` -- zero findings.
- `alembic upgrade 090 && alembic upgrade 091` on PostgreSQL fixtures -- clean cutover and asserted guards.
- `docker build -f Dockerfile.bot .` -- release image builds.
- `gh pr checks 485 --watch` -- PR checks GREEN.

**Manual checks:**
- Exact release image revision, Alembic 091, one bot container, `/healthz` and `/healthz/db` 200, no polling conflict or intro-worker errors across five 120-second polls.
- Predko effect is `sent` with Telegram identity; current Intro points to the new application and equals its frozen HTML; the open refresh tracking row is completed; Sheet contains `UK` and `@oxanagesina`; old Telegram intro remains.
