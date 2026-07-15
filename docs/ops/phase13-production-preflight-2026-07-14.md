# Phase 13 production preflight — 2026-07-14

## Production baseline

- Bot image: `ghcr.io/jekudy/vibe-gatekeeper-bot:main`
- Container uptime на момент проверки: 3 недели
- Alembic revision: `019`
- Новые memory feature flags отсутствуют
- Coolify health check выключен
- `chat_messages`: `6 292`
- `message_versions`: `4 021`
- `chat_messages.current_version_id IS NULL`: `2 271`

## Telegram HTML export

Источник: `ChatExport_2026-07-14`, десять HTML-файлов. Offline dry-run:

- всего записей: `9 770`
- пользовательских сообщений: `9 403`
- service events: `367`
- авторов: `172`
- exact author `Shkoder`, raw-only: `107`
- reply: `4 717`, offline dangling: `23`
- parser warnings: `0`
- duplicate export message IDs: `0`

DB-aware dry-run был подключён к production в read-only режиме:

- существующие нормальные сообщения, которые будут пропущены: `3 952`
- hidden/legacy сообщения, которые будут rehydrate: `2 231`
- dangling reply после учёта production: `12`
- `offrecord`: `0`
- `nomem`: `0`
- exact `Shkoder`, raw-only: `107`

## Safety gate

Перед любыми миграциями создан свежий backup
`/home/claw/backups/shkoder-postgres/shkoder-pg-preverify-20260714.dump` с
правами `0600`. Размер — `2 141 059` байт, SHA-256 —
`b91b9eda6e651c302b7280eae3cbaf911cc4d8955474d61a7a31cbe8ed06f2b4`.
Отдельный restore и migration drill `019 → 087 → 086 → 087` завершён успешно.
Детали и контрольные counts: `docs/ops/restore-drill-2026-07-14.md`.

Production migration/import разрешены только после полного green test/lint/image
gate, green PR CI и проверки точного release SHA.

## Локальный verification gate

- Fresh PostgreSQL 16, Alembic `087`: `2 567 passed, 6 skipped`.
- Linux/amd64 bot и web images собраны и запущены от UID `10001`.
- `pip check` green в обоих images; bot runtime и web app импортируются.
- Статический publisher содержит точный Wrangler `4.110.0`.
- `ruff format --check`: `147 files already formatted`.
- `ruff check .`, `git diff --check`: green.
- `npm audit --omit=dev`: `0 vulnerabilities`.
- Gitleaks `8.28.0`: `no leaks found` по tracked и новым non-ignored файлам.
- Финальный независимый P0/P1 review: блокеров нет.

## Reconciliation неоднозначных платных вызовов

Автоматический retry запрещён, если неизвестно, принял ли провайдер запрос.
Сначала оператор сверяет внешний provider audit, затем выполняет ровно одну
команду. В `--reason` нельзя помещать provider response, исходный контент или
секреты. `--evidence-hash` — только необязательный lowercase SHA-256 внешнего
артефакта, не сам артефакт.

Безопасный extraction retry допустим только для `not_dispatched` или
`rejected_pre_accept`:

```bash
python -m bot.cli memory_reconcile_extraction \
  --run-id 00000000-0000-0000-0000-000000000000 \
  --action safe_retry \
  --actor-user-id 149820031 \
  --reason 'Provider подтвердил отказ до принятия запроса' \
  --evidence-hash aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Если состояние `unknown` или `response_received`, повтор возможен только с
явным принятием риска повторной оплаты:

```bash
python -m bot.cli memory_reconcile_extraction \
  --run-id 00000000-0000-0000-0000-000000000000 \
  --action risk_accepted_retry \
  --actor-user-id 149820031 \
  --reason 'Provider audit не позволяет доказать итог запроса' \
  --accept-possible-duplicate-cost
```

Чтобы закрыть extraction без повторного вызова:

```bash
python -m bot.cli memory_reconcile_extraction \
  --run-id 00000000-0000-0000-0000-000000000000 \
  --action abandon \
  --actor-user-id 149820031 \
  --reason 'Окно сознательно пропускается после внешней проверки' \
  --accept-memory-gap
```

Для `version_cursor` команда `abandon` атомарно продвигает cursor до
`cursor_end_message_version_id`. Для retry старый run и ledger не меняются;
следующий scheduler/backfill сначала повторяет ровно исходные cursor bounds и
source snapshot, создаёт новый `attempt_no` со ссылкой `retry_of_run_id`, и
только после его завершения забирает более новые message versions. Если
snapshot внутри исходных bounds изменился, scheduler не вызывает provider и
возвращает `reconciled_source_snapshot_changed` для ручной проверки.

Неоднозначное описание картинки можно повторить только из состояния
`processing` и только с принятием риска повторной оплаты:

```bash
python -m bot.cli memory_reconcile_image \
  --message-media-id 123 \
  --action risk_accepted_retry \
  --actor-user-id 149820031 \
  --reason 'Provider audit не позволяет доказать итог описания' \
  --accept-possible-duplicate-cost
```

Или завершить как видимый terminal `failed`, приняв пропуск в памяти:

```bash
python -m bot.cli memory_reconcile_image \
  --message-media-id 123 \
  --action abandon \
  --actor-user-id 149820031 \
  --reason 'Описание сознательно пропущено после внешней проверки' \
  --accept-memory-gap
```

Обе image-команды сохраняют старую ledger-строку без изменений. Решение
записывается ровно один раз для пары `(message_media_id, description_attempts)`.
Если risk retry создаст новый claim и он тоже останется неоднозначным, новый
`description_attempts` можно отдельно повторить или завершить через ту же
команду. Повторное решение для одной и той же попытки и reconciliation уже
завершённого результата fail closed.
