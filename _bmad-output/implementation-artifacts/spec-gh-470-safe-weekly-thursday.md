---
title: 'Безопасный weekly-дайджест по четвергам'
type: 'bugfix'
created: '2026-07-22'
status: 'done'
review_loop_iteration: 0
baseline_commit: '34659407f270929ce677cb97b2746cf7c147f29e'
context:
  - '{project-root}/_bmad-output/project-context.md'
  - '{project-root}/docs/memory-system/phase13-rollout-plan.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Daily уже выключен, но weekly всё ещё запланирован на понедельник, а существующий auto-publish имеет пять подтверждённых блокеров: переписывание проверенного текста, непроверенные headings, forget-race перед Telegram send, слишком большой citation enum и устаревший kill switch.

**Approach:** Перенести weekly на четверг 09:00 MSK с окном четверг 05:00 → четверг 05:00 и минимально закрыть пять блокеров, не меняя prompt, модель или редакционный стиль.

## Boundaries & Constraints

**Always:** Daily остаётся выключен. Keep-текст и его citations сохраняются дословно. Третий LLM-вызов редактирует только verifier `fix` units и не меняет provenance. Переполнение сокращается удалением целых проверенных пунктов. Любая неоднозначность privacy/citation/flag состояния блокирует отправку. Weekly включается только после зелёных CI, Ponytail и Sol ship gate.

**Ask First:** Любое изменение prompt/model/gold/eval, daily pipeline, Telegram-формата, citation-position contract или production rollout с незакрытым gate.

**Never:** Не добавлять retries, fallback-модель, скрытое усечение входных сообщений, LLM-пересказ после verifier, новые review/approval состояния или новые архитектурные слои.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Weekly schedule | Четверг 09:00 MSK, weekly ON | Один запуск за окно Thu 05:00 → Thu 05:00 | Idempotent no-op для уже созданного окна |
| Boundary | Четверг 04:59 / 05:00 MSK | До границы конец — прошлый Thu; после — текущий Thu | Naive datetime отклоняется |
| Verified output | `keep`, `fix`, либо overflow | Keep неизменен; fix правится с прежними citations; overflow удаляет целые хвостовые пункты и пустые секции | Если целый валидный пост не помещается — fail closed |
| Unsafe heading | Secret, телефон, Telegram URL или неподтверждённый heading | Отправки нет | Contract/verifier block |
| Large context | 499+ допустимых сообщений | Schema принимается provider; tokens проверяет server allow-list | Неизвестный token отклоняется |
| Forget race | Forget до или во время publish | Forget выигрывает до lock либо ждёт завершения защищённого send | Locks освобождаются при success/error |
| Kill switch | Weekly `true → false` во время synthesis | Draft остаётся неопубликованным, Telegram-вызовов нет | Structured log, без retry |

</frozen-after-approval>

## Code Map

- `bot/services/digest_windows.py` — граница недельного окна.
- `bot/services/scheduler.py` — cron и повторная проверка weekly flag.
- `bot/services/digest_contract.py` — heading units, validation, merge и детерминированное сокращение.
- `bot/services/llm_gateway.py` — verifier/editor pipeline без full-digest finalizer.
- `bot/services/llm_prompts/digest_v0_1_0.py` — strict schema без citation enum; текст prompts не меняется.
- `bot/services/digest_publisher.py` — advisory-lock scope через Telegram send.
- `bot/services/advisory_locks.py` — существующие provenance lock keys и scope.
- `tests/services/test_digest_{windows,scheduler_weekly,contract_406,digests_run,publisher}.py` — focused regression coverage.

## Tasks & Acceptance

**Execution:**
- [x] `bot/services/digest_windows.py`, `bot/services/scheduler.py` — принять текущий Thu schedule/window patch и добавить pre-publish flag recheck.
- [x] `bot/services/digest_contract.py`, `bot/services/llm_gateway.py` — включить headings в factual units, применять editor только к `fix`, сохранять keep/provenance и детерминированно сокращать weekly.
- [x] `bot/services/llm_prompts/digest_v0_1_0.py` — убрать citation enum, сохранив server-side allow-list.
- [x] `bot/services/digest_publisher.py` — удерживать существующие governed-message locks от последней revalidation до конца Telegram send.
- [x] Focused tests — покрыть все строки I/O matrix, включая PostgreSQL race.

**Acceptance Criteria:**
- Given зелёные focused/full tests и reviews, when release развёрнут, then production имеет `daily=false`, `weekly=true`, cron Thu 09:00 MSK и окно ровно семь суток.
- Given любой из пяти safety invariants нарушен, when weekly доходит до publish gate, then Telegram message не отправляется.

## Spec Change Log

## Design Notes

Weekly использует существующий editor-schema и `parse_editor`: он уже сохраняет citations исходного unit. Full-digest finalizer удаляется. После merge pure deterministic compaction удаляет последние целые items и ставшие пустыми sections; closing сохраняется. Headings становятся обычными verifier units с объединёнными citations своей секции.

## Verification

**Commands:**
- `.venv/bin/ruff check bot tests` — без lint ошибок.
- `.venv/bin/pytest -q tests/services/test_digest_windows.py tests/services/test_digest_scheduler_weekly.py tests/services/test_digest_contract_406.py tests/services/test_digests_run.py tests/services/test_digest_publisher.py` — focused suite green.
- `.venv/bin/pytest -q` — full suite green.
- `docker compose build bot` — production image собирается.
- GitHub CI, Ponytail `full`, финальный Sol ship gate — без blocking findings.

**Результаты 2026-07-22:**
- Focused PostgreSQL suite: `90 passed`.
- Full PostgreSQL suite: `3018 passed, 6 skipped`.
- Ruff, `git diff --check` и `docker compose build bot`: PASS.
- Повторный BMAD review: три подтверждённых patch-находки закрыты, PASS.
- Ponytail `full`: `Lean already. Ship.`
- Sol final ship gate: `SHIP`.

## Suggested Review Order

**Расписание и окно**

- Точка входа: автопубликация с повторным kill-switch перед отправкой.
  [`scheduler.py:755`](../../bot/services/scheduler.py#L755)

- Четверговая граница строит ровно семь завершённых суток.
  [`digest_windows.py:37`](../../bot/services/digest_windows.py#L37)

**Проверенный текст**

- Pipeline ограничен draft, verifier и editor только для `fix`.
  [`llm_gateway.py:3854`](../../bot/services/llm_gateway.py#L3854)

- Headings входят в тот же factual-verifier contract.
  [`digest_contract.py:145`](../../bot/services/digest_contract.py#L145)

- Переполнение удаляет целые пункты и сохраняет живую provenance заголовков.
  [`digest_contract.py:254`](../../bot/services/digest_contract.py#L254)

- Schema больше не раздувает provider enum; allow-list остаётся серверным.
  [`digest_v0_1_0.py:140`](../../bot/services/llm_prompts/digest_v0_1_0.py#L140)

**Forget-race и приватность**

- Provenance locks берутся раньше digest row и живут до финального commit.
  [`digest_publisher.py:232`](../../bot/services/digest_publisher.py#L232)

- Forget удаляет затронутый heading вместе с его источником.
  [`digest_redactor.py:42`](../../bot/services/digest_redactor.py#L42)

**Доказательства**

- Kill-switch и Thu cron закреплены отдельными scheduler-тестами.
  [`test_digest_scheduler_weekly.py:175`](../../tests/services/test_digest_scheduler_weekly.py#L175)

- Compaction проверяет оба исхода для provenance заголовка.
  [`test_digest_contract_406.py:379`](../../tests/services/test_digest_contract_406.py#L379)

- PostgreSQL подтверждает оба исхода forget-race и порядок lock’ов.
  [`test_digest_publisher.py:213`](../../tests/services/test_digest_publisher.py#L213)
