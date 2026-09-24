# Внешние бэкапы Shkoder и Harry — runbook

Задача: SHK #523. Спецификация: `docs/spec-523-external-backups.md`.
Развёрнуто и проверено 2026-09-18. Владелец: тред `thr_u72ausqdic`.

Этот runbook описывает механизм, который увозит копии **вне VPS**. Локальный ежедневный
дамп Shkoder (`/usr/local/sbin/shkoder-pg-backup.sh`, cron 03:17 UTC, retention 7 дней)
работает отдельно и не изменялся — см. `docs/ops/db-backup-runbook.md`.

## Что, куда, когда

| | Shkoder | Harry |
|---|---|---|
| Источник | свежайший `/data/coolify/backups/shkoder-postgres/shkoder-pg-*.dump` | `pg_dump` из `harry-honcho-db` + выборочное состояние из volume `…_harry-hermes-data` |
| Объект | `b2:jekudy-vibe-backups/shkoder/shkoder-pg-<ts>.dump.gpg` | `b2:jekudy-vibe-backups/harry/harry-<ts>.tar.gz.gpg` |
| Размер (2026-09-18) | ~50 МБ | ~5.9 МБ |
| Расписание (UTC) | 03:50 | 03:40 |
| Retention в B2 | 30 дней | 30 дней |

Имя бакета — `jekudy-vibe-backups`: `vibe-backups` в B2 уже занято чужим аккаунтом,
имена бакетов там глобально уникальны.

В состав Harry входят: `harry-honcho.dump`, снимки `state.db`, `kanban.db`,
`cron/executions.db`, `mcp_sessions/telegram_user.session`, а также `auth.json`, `.env`,
`channel_directory.json` и `/srv/harry/config/`. Кэши (`cache/`, `audio_cache/`, `.cache/`,
`.codex/`, `home/`) исключены — это ~500 из 525 МБ volume'а, и они восстанавливаются сами.
Redis Harry не бэкапится: это эфемерная очередь deriver'а.

## Файлы

| На VPS | Что |
|---|---|
| `/usr/local/sbin/backup-to-b2.sh` | скрипт, `0750 root:root`. Источник — `ops/vps/backup-to-b2.sh` |
| `/etc/cron.d/vibe-b2-backup` | расписание. Источник — `ops/vps/vibe-b2-backup.cron` |
| `/srv/secrets/backup.env` | `0600 root:root`: `GPG_RECIPIENT`, `TELEGRAM_DEV_BOT_TOKEN`, `ADMIN_TELEGRAM_ID` |
| `/srv/secrets/backup-pubkey.asc` | публичная половина ключа, импортирована в keyring root |
| `/var/log/vibe-b2-backup.log` | лог обоих сервисов |

Приватный ключ на VPS отсутствует и не должен там появляться. Он лежит только на маке:
в keyring GnuPG и резервной копией в `~/.env.tokens`
(`SHK_HARRY_BACKUP_GPG_SECKEY_B64`, отпечаток в `SHK_HARRY_BACKUP_GPG_FPR`).

## Установка с нуля

```bash
# 1. публичный ключ на VPS (приватный остаётся на маке)
gpg --armor --export "$FPR" | ssh foodzy-vps 'sudo tee /srv/secrets/backup-pubkey.asc >/dev/null &&
  sudo gpg --batch --import /srv/secrets/backup-pubkey.asc'

# 2. конфигурация
ssh foodzy-vps 'sudo sh -c "
  { echo GPG_RECIPIENT=<отпечаток>
    grep -E \"^TELEGRAM_DEV_BOT_TOKEN=\" /opt/foodzy/.env.prod
    grep -E \"^ADMIN_TELEGRAM_ID=\" /opt/foodzy/.env.prod
  } > /srv/secrets/backup.env && chmod 0600 /srv/secrets/backup.env"'

# 3. скрипт и расписание
scp ops/vps/backup-to-b2.sh ops/vps/vibe-b2-backup.cron foodzy-vps:/tmp/
ssh foodzy-vps 'sudo install -m 0750 -o root -g root /tmp/backup-to-b2.sh /usr/local/sbin/backup-to-b2.sh &&
  sudo install -m 0644 -o root -g root /tmp/vibe-b2-backup.cron /etc/cron.d/vibe-b2-backup'

# 4. бакет (скрипт его НЕ создаёт — см. «известные грабли»)
ssh foodzy-vps 'sudo rclone mkdir b2:jekudy-vibe-backups'
```

Обновление скрипта — повтор шага 3. Установка идемпотентна: `install` перезаписывает файл
целиком, дублей расписания не возникает.

## Проверка

```bash
ssh foodzy-vps 'sudo rclone lsl b2:jekudy-vibe-backups/'      # объекты с датой и размером
ssh foodzy-vps 'sudo tail -30 /var/log/vibe-b2-backup.log'    # последние прогоны
ssh foodzy-vps 'cat /etc/cron.d/vibe-b2-backup'               # расписание
ssh foodzy-vps 'sudo /usr/local/sbin/backup-to-b2.sh harry; echo exit=$?'   # ручной прогон
bash ops/vps/backup-to-b2.test.sh                             # логика скрипта, без сети
```

Успешный прогон заканчивается строкой `OK <сервис>`. Провал пишет `FAILED <сервис>: …`
и отправляет сообщение в тот же Telegram-канал, что использует бэкап Foodzy.

Коды возврата: `0` успех · `1` ошибка конфигурации · `2` источник непригоден ·
`3` ошибка шифрования · `4` ошибка заливки или retention.

## Восстановление

Расшифровка выполняется **на маке** — приватного ключа на VPS нет. Это же делает проверку
честной: она доказывает, что копии читаются при полной потере сервера.

### Shkoder

```bash
OBJ=shkoder-pg-<ts>.dump.gpg
ssh foodzy-vps "sudo rclone cat b2:jekudy-vibe-backups/shkoder/$OBJ" > "$OBJ"
shasum -a 256 "$OBJ"        # сверить с sha256 из /var/log/vibe-b2-backup.log
gpg --batch --decrypt --output restore.dump "$OBJ"

# образ обязан содержать pgvector, иначе часть объектов не восстановится
IMG=$(ssh foodzy-vps 'sudo grep -E "^DB_IMAGE=" /srv/shkoder/.env | cut -d= -f2-')
docker pull "$IMG"          # GHCR: нужен docker login ghcr.io
docker run -d --name shk-restore -e POSTGRES_USER=vibe -e POSTGRES_PASSWORD=t \
  -e POSTGRES_DB=scratch "$IMG"
docker exec -i shk-restore pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname=scratch -U vibe < restore.dump
docker exec shk-restore psql -U vibe -d scratch -c "SELECT count(*) FROM users;"
docker rm -f shk-restore
```

### Harry

```bash
OBJ=$(ssh foodzy-vps 'sudo rclone lsf b2:jekudy-vibe-backups/harry/' | sort | tail -1)
ssh foodzy-vps "sudo rclone cat b2:jekudy-vibe-backups/harry/$OBJ" > "$OBJ"
gpg --batch --decrypt --output harry.tar.gz "$OBJ"
mkdir harry && tar -xzf harry.tar.gz -C harry

sqlite3 harry/sqlite/state.db 'PRAGMA integrity_check;'   # ожидается ok

# у honcho тоже pgvector; прод-образ Harry собран локально и в реестре отсутствует,
# поэтому для проверки берётся публичный эквивалент
docker run -d --name harry-restore -e POSTGRES_PASSWORD=t pgvector/pgvector:pg15
docker exec -i harry-restore pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname=postgres -U postgres < harry/harry-honcho.dump
docker exec harry-restore psql -U postgres -c "SELECT count(*) FROM messages;"
docker rm -f harry-restore
```

Файлы состояния (`files/.env`, `auth.json`, `config/`) возвращаются в volume
`…_harry-hermes-data` вручную при реальном восстановлении сервиса.

**После восстановления удалите расшифрованные файлы** — это боевые данные в открытом виде.

## Ротация ключа шифрования

Старые объекты остаются зашифрованными старым ключом: не удаляйте приватный ключ, пока в
бакете есть объекты старше даты ротации (при retention 30 дней — 30 дней после смены).

1. `gpg --batch --passphrase '' --quick-generate-key 'SHK523 Backup <…>' rsa4096 default never`
   и `--quick-add-key <FPR> rsa4096 encr never`
2. Экспортировать публичную половину на VPS, импортировать в keyring root.
3. Поменять `GPG_RECIPIENT` в `/srv/secrets/backup.env`.
4. Обновить `SHK_HARRY_BACKUP_GPG_SECKEY_B64` и `SHK_HARRY_BACKUP_GPG_FPR` в `~/.env.tokens`.
5. Прогнать оба сервиса вручную и проверить расшифровку нового объекта.

## Известные грабли

1. **`rclone` сам создаёт отсутствующий бакет.** B2-backend делает это молча при `copyto`:
   опечатка в `B2_BUCKET` выглядела бы как успешный бэкап, уехавший в пустоту. Поэтому скрипт
   сначала проверяет наличие бакета через `rclone lsf --dirs-only` и отказывается его создавать.
   Обнаружено на прогоне негативного пути 2026-09-18.
2. **`exit` не запускает `ERR`-trap.** Уведомление о провале живёт в `EXIT`-trap, иначе
   контролируемые падения (`die`) не отправляли бы алерт вовсе. Тоже находка прогона.
3. **Retention строго по своему префиксу и своему шаблону имени.** Бэкап Foodzy выполняет
   `rclone delete --min-age 30d b2:foodzy-backups/` рекурсивно и без фильтра имени — если
   положить дампы Shkoder или Harry в его бакет, они попадут под чужую политику удаления.
   Отдельный бакет выбран именно поэтому.
4. **Для восстановления нужен образ с pgvector.** `postgres:15-alpine` даёт `extension "vector"
   is not available` и неполное восстановление с кодом 1. Прод-образ Shkoder есть в GHCR;
   прод-образ Harry собран на хосте и в реестре отсутствует — подходит `pgvector/pgvector:pg15`.
5. **Ошибка конфигурации не уведомляет в Telegram.** Если `/srv/secrets/backup.env` недоступен,
   токен прочитать неоткуда; скрипт пишет в лог `cannot alert — Telegram credentials were never
   loaded` и выходит с кодом 1. Такой случай ловится только чтением лога.
