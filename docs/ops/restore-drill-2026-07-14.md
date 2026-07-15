# Restore и migration drill Postgres — 2026-07-14

## Контекст

Проверка выполнена перед production rollout Phase 13. Рабочая база не
останавливалась и не изменялась: свежий production dump восстановлен в
одноразовый контейнер PostgreSQL `15.18`, после чего на копии пройден полный
round-trip миграции.

## Артефакт

- Backup: `shkoder-pg-preverify-20260714.dump`
- VPS: `/home/claw/backups/shkoder-postgres/`, права файла `0600`
- Размер: `2 141 059` байт
- SHA-256: `b91b9eda6e651c302b7280eae3cbaf911cc4d8955474d61a7a31cbe8ed06f2b4`
- Исходная версия Alembic: `019`

## Проверка

После `pg_restore --exit-on-error --no-owner --no-privileges` выполнены upgrade
`019 → 087`, допустимый до появления rollout audit rows downgrade `087 → 086`
и повторный upgrade `086 → 087`. Контрольные counts:

| Таблица | До миграции | После round-trip |
|---|---:|---:|
| `applications` | 158 | 158 |
| `chat_messages` | 6 299 | 6 299 |
| `intros` | 93 | 93 |
| `message_versions` | 4 021 | 6 299 |
| `telegram_updates` | 0 | 0 |
| `users` | 365 | 365 |

Рост `message_versions` ожидаемый: migration `023` восстановила текущую версию
для `2 278` старых `chat_messages`, у которых её не было.

Проверено дополнительно:

- финальная Alembic revision — `087`;
- все девять ключевых constraints существуют и `convalidated=true`;
- `message_media`, `wiki_static_deployments`, `extraction_cursors`,
  `extraction_run_resolutions`, `image_description_resolutions` и
  `telegram_updates` существуют;
- raw INSERT в `extraction_runs` без `attempt_no` и `dispatch_state` получает
  `1` и `not_dispatched` из DB-default;
- `tests/db/test_memory_reconciliation_schema.py`: `6 passed`.

## Результат

`PASS`: backup структурно валиден, полностью восстанавливается PostgreSQL 15,
production-данные мигрируются до `087` без потерь, безопасный pre-rollout
downgrade и повторный upgrade работают. Одноразовые контейнер и сеть удалены;
production не изменялся.
