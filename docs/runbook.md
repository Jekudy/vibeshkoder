# Runbook

## Runtime Boundary

### Product Apps

These belong in Coolify:

- `vibe-gatekeeper`
- `foodzy`
- other normal app/web/bot/worker services

### Operator Services

These stay host-managed when they need direct VPS control:

- Telegram-based operator services
- watchdog services
- orchestration tools that need Docker, SSH, or unrestricted shell access

## Environment Model

### Local

- `DEV_MODE=true`
- separate dev bot token
- SQLite
- no shared production resources

### Staging

- `DEV_MODE=false`
- separate staging bot token
- separate staging DB
- separate staging Redis
- separate staging web password
- optional isolated staging chat

### Production

- `DEV_MODE=false`
- production bot token
- production DB
- production Redis
- production web password

## Secret Rules

Never commit:

- `.env`
- `.env.staging`
- `.env.production`
- `credentials.json`

## Release Model

- CI validates the repo.
- Release workflow builds and pushes GHCR images.
- Coolify deploys pre-built images from GHCR.
- Rollback uses the previous image tag.

## Current Server State

As of 2026-04-20 (prod cutover completed):

- GitHub repo exists at `https://github.com/Jekudy/vibe-gatekeeper`.
- CI is green on `main`.
- Release workflow is gated on successful CI and pushes bot/web images to GHCR.
- Coolify dashboard on Tailscale IP only: `http://100.101.196.21:8100`.
- Public VPS IP: `187.77.98.73` (Hostinger srv1435593).
- **Production bot `@vibeshkoder_bot` is now running under Coolify** (Docker-managed via Coolify).
- Legacy runtime at `/home/claw/vibe-gatekeeper` was stopped and removed on 2026-04-20 via `docker compose down`.
- Public web: `0.0.0.0:8080` is now owned by the Coolify-managed web container.

## Coolify Registry & SSH (resolved 2026-04-19)

- GHCR pull is unblocked via `docker login ghcr.io -u Jekudy` on the VPS as root.
- Auth lives in `/root/.docker/config.json`.
- Coolify reuses the host Docker daemon, so no Coolify-side registry resource is needed.
- Coolify localhost server bootstrap was repaired again on 2026-04-19:
  - `servers.id=0` user reverted to `root`
  - Coolify localhost public key re-added to `/root/.ssh/authorized_keys`
  - server validation now reports `is_reachable=true`, `is_usable=true`

## Coolify Prod Resources

Initially created on 2026-04-19 as "staging" shells, repurposed as prod on 2026-04-20 after direct cutover (no staging phase; `DEV_MODE=false` on both apps). Names still carry `-staging` suffix — cosmetic, rename later. Coolify project: `My first project` / environment: `staging` (label, not runtime).

| Kind | UUID | Notes |
|---|---|---|
| App `vibe-gatekeeper-web` | `cexv50jspo5gl3kq6ojypw43` | image `ghcr.io/jekudy/vibe-gatekeeper-web:main`, port `8080:8080` (public), fqdn sslip `cexv50jspo5gl3kq6ojypw43.187.77.98.73.sslip.io` |
| App `vibe-gatekeeper-bot-staging` | `maiwn569gziz935wv0w7kcch` | image `ghcr.io/jekudy/vibe-gatekeeper-bot:main`, polling mode |
| Postgres `vibe-gatekeeper-pg-staging` | `hdazvm5fz836xj9mdyn8c629` | `postgres:15-alpine`, db `vibe_gatekeeper`, user `vibe`, data migrated from legacy on 2026-04-20 |
| Redis `vibe-gatekeeper-redis-staging` | `gl28f0g5exzzo4k8w0auzygk` | `redis:7-alpine`, password set |

Internal connection strings:

- `DATABASE_URL=postgresql+asyncpg://vibe:<DB_PW>@hdazvm5fz836xj9mdyn8c629:5432/vibe_gatekeeper`
- `REDIS_URL=redis://default:<REDIS_PW>@gl28f0g5exzzo4k8w0auzygk:6379/0`

DB / Redis / web passwords are stored in Coolify env vars only. API token in `~/.env.tokens:COOLIFY_API_TOKEN`.

Both apps have `custom_docker_run_options = -v /srv/secrets/vibe-gatekeeper/credentials.json:/app/credentials.json:ro` configured (decoupled from legacy `/home/claw/vibe-gatekeeper/` on 2026-04-24 per playbook `p3.5`). Canonical host path: `/srv/secrets/vibe-gatekeeper/credentials.json`, mode `0400`, owner `root:root` (container runs as `uid=0`, verified).

> **KNOWN BUG (discovered 2026-04-24 during A3 decouple)**: Coolify v4.0.0-beta.472 compose-based deployments **silently ignore `-v` flags in `custom_docker_run_options`** — the generated `/data/coolify/applications/<uuid>/docker-compose.yaml` contains no `volumes:` entry, so `/app/credentials.json` is missing inside both containers. This means `sync_google_sheets` has been failing with `FileNotFoundError: /app/credentials.json` since the 2026-04-20 cutover (confirmed in bot logs at 2026-04-24 18:47:58). The bot gracefully catches the error and continues; only the Sheets sync path is degraded. **Fix path (TBD, separate task)**: register a file storage via the Coolify UI (`Storages → Add → File Mount`) — the API currently exposes only a read endpoint (`GET /applications/{uuid}/storages`), so UI action is required. After adding the file storage, Coolify will inject `volumes:` into the compose yaml and redeploy will restore the mount.

## Prod Cutover — 2026-04-20

Executed directly from legacy → Coolify prod (staging skipped, per user direction to work with prod only).

1. Cleaned Coolify env vars: deleted all `is_preview=true` duplicates; set runtime vars (`BOT_TOKEN`, `COMMUNITY_CHAT_ID=-1002716490518`, `ADMIN_IDS=[149820031]`, `GOOGLE_SHEET_ID`, `WEB_BASE_URL=http://187.77.98.73:8080`, `WEB_BOT_USERNAME=vibeshkoder_bot`, `WEB_PASSWORD=<rotated — see 1Password "Shkoderbot Web Admin">`, `DEV_MODE=false`) sourced from legacy `/home/claw/vibe-gatekeeper/.env`.
2. Mounted `credentials.json` into both apps via `custom_docker_run_options`.
3. Dumped legacy DB (`vibe-gatekeeper-db-1` → `pg_dump --clean --if-exists`) and restored into Coolify postgres. Row counts post-restore: users=275, applications=58, intros=44, vouch_log=39, questionnaire_answers=340, chat_messages=3109, alembic_version=1.
4. Stopped legacy bot first (Telegram session release), made a final incremental dump + restore to capture delta.
5. `docker compose down` on legacy — port 8080 and BOT_TOKEN both free.
6. PATCHed Coolify web `ports_mappings: 18080:8080 → 8080:8080` and redeployed.
7. Deployed Coolify bot for the first time.
8. Verified: `curl http://187.77.98.73:8080 → 302`, bot logs show `Run polling for bot @vibeshkoder_bot id=8271790115 - 'Shkoder'`, scheduler jobs registered (`check_vouch_deadlines`, `check_intro_refresh`, `sync_google_sheets`), real chat updates being handled within 500ms of start.

## Rollback Procedure

If Coolify prod becomes unhealthy:

```bash
ssh claw@187.77.98.73
TOKEN=<coolify api token>
API=http://100.101.196.21:8100/api/v1
# 1. Stop Coolify bot FIRST to release BOT_TOKEN from Telegram session:
curl -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/maiwn569gziz935wv0w7kcch/stop"
# 2. Stop Coolify web to free port 8080:
curl -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/cexv50jspo5gl3kq6ojypw43/stop"
# 3. Restart legacy stack:
cd /home/claw/vibe-gatekeeper && docker compose up -d
```

Legacy `docker-compose.yml`, `.env`, and `credentials.json` are preserved in `/home/claw/vibe-gatekeeper` — do not delete until prod has run stably for 7+ days.

## Coolify deploys

Canonical reference: `~/Vibe/knowledge/nocoders/docs/architecture/coolify-deploy-playbook.md` plus `/coolify-deploy` skill.

- **Start app:** Coolify UI → project → app → Start. CLI: `coolify start <app-uuid>` (if `coolify` CLI available on host) or `docker start <container-uuid>` as fallback.
- **Stop app:** Coolify UI → Stop, or `coolify stop <app-uuid>`.
- **Pull logs (last 500 lines, follow):** `coolify logs <app-uuid> --tail 500 --follow`; fallback `docker logs <container-uuid> --tail 500`.
- **Where secrets live:** Coolify env panel per app. On disk: `/data/coolify/...` (ACL 600 root:root). Never commit to git.
- **Rollback to previous digest:** Coolify UI → app → Deployments → select previous deployment → Redeploy. CLI path: update image reference in app config to the prior `@sha256:` digest, redeploy.

## Known Issues & Quirks

_Filled incrementally as Coolify migration reveals issues. Each entry format:_

```
### <YYYY-MM-DD> — <short issue>
Symptom:
Root cause:
Fix:
```

On 2026-04-24 `credentials.json` was copied to the canonical path `/srv/secrets/vibe-gatekeeper/credentials.json` (sha256 verified match against legacy) per playbook `p3.5`. Both files coexist until legacy cleanup (earliest 2026-04-27), independently of the "mount actually wired" question — the canonical copy is ready for the future Coolify file-storage fix. Legacy copy is safe to delete after 2026-04-27 once the Coolify file-storage mount is wired up and a successful `sync_google_sheets` execution is observed.

## Legacy Dir Cleanup (post-soak)

After the 7-day soak window (earliest: 2026-04-27) and A3 credentials decouple completion, remove the legacy dir via the prepared script:

```bash
# Safe mode (enforces A3 + soak window + disk check):
./scripts/cleanup-legacy.sh

# Force mode (skip soak window, operator accepts risk; A3/disk still enforced):
./scripts/cleanup-legacy.sh --force
```

Script behavior:

- Preflight 1: verifies no running Coolify vibe-gatekeeper container references `/home/claw` via Mounts or HostConfig.Binds. Abort if any match.
- Preflight 2: refuses to run before `SOAK_END=2026-04-27` unless `--force`.
- Preflight 3: requires ≥200M free on VPS `/`.
- Mandatory backup: tars the dir to `/root/backups/vibe-gatekeeper-legacy-<ts>.tar.gz` with integrity check.
- Stops any stray legacy compose containers (best-effort), then `rm -rf`.
- Post-verify: Coolify vibe-gatekeeper containers still up + Telegram `getMe` ok.

Restore from backup if needed:

```bash
ssh foodzy-vps-claw "sudo tar xzf /root/backups/vibe-gatekeeper-legacy-<ts>.tar.gz -C /home/claw"
```

## Rollback Drill (Coolify → Legacy)

Controlled симуляция падения Coolify runtime с переходом на legacy stack. Процедура не тестировалась с момента cutover 2026-04-20 — до первого реального инцидента её нужно прогнать в drill-режиме и зафиксировать результат.

### Drill-окно

- Минимально допустимое downtime: **5–10 минут** (включая откат обратно в Coolify).
- Рекомендуемое окно: **weekend night** (низкая активность community).
- Во время drill: отправленные команды `/start`, verification-токены и любые сообщения в админ-тредах могут быть потеряны либо обработаны дважды. Предупредить активных админов минимум за 24 часа.
- Не запускать drill пока не завершён A3 (credentials decoupling). Если A3 ещё не закрыт — legacy compose ссылается на `/home/claw/vibe-gatekeeper/credentials.json`, и этот файл должен быть физически на диске (проверить перед drill).

### Preconditions (чеклист перед стартом)

- [ ] SSH-доступ работает: `ssh -o BatchMode=yes claw@187.77.98.73 true` возвращает код 0.
- [ ] `COOLIFY_API_TOKEN` в `~/.env.tokens` (локально на ноуте оператора).
- [ ] `SHKODERBOT_BOT_TOKEN` в `~/.env.tokens` (для L1/L2 проверок).
- [ ] На VPS: `/home/claw/vibe-gatekeeper/docker-compose.yml`, `.env`, `credentials.json` — все три файла существуют.
- [ ] В Coolify UI сделан snapshot env vars для обоих app (bot UUID `maiwn569gziz935wv0w7kcch`, web UUID `cexv50jspo5gl3kq6ojypw43`) — экспортировать через API в JSON и сохранить локально.
- [ ] Drill-окно согласовано с community admins (anonymized notice).
- [ ] Drill log готов (см. template ниже).
- [ ] Оператор знает, что **данные, записанные в Coolify DB после drill-start, не попадут в legacy DB** — и принимает эту data-loss window как ожидаемое поведение.

### Backup Coolify Postgres перед drill

Обязательный шаг — snapshot текущего состояния Coolify DB, чтобы при необходимости можно было восстановить delta после возврата:

```bash
ssh claw@187.77.98.73
# Найти контейнер Coolify Postgres по UUID ресурса:
PG_CONTAINER=$(docker ps --format '{{.Names}}' | grep hdazvm5fz836xj9mdyn8c629 | head -1)
# Получить creds из env контейнера:
docker exec "$PG_CONTAINER" env | grep -E 'POSTGRES_(USER|PASSWORD|DB)'
# Сделать dump (заменить <user>/<db> из предыдущего шага):
docker exec "$PG_CONTAINER" pg_dump -U vibe -d vibe_gatekeeper --clean --if-exists \
  > ~/drills/coolify-pre-drill-$(date +%Y%m%d-%H%M).sql
ls -lh ~/drills/
```

Файл dump должен быть не нулевого размера (ожидаемо — единицы MB).

### Steps (последовательность drill)

Все шаги выполнять с одного SSH-сеанса `claw@187.77.98.73`, TOKEN и API заданы один раз в начале.

```bash
# Preamble (один раз):
ssh claw@187.77.98.73
TOKEN=<COOLIFY_API_TOKEN>
API=http://100.101.196.21:8100/api/v1
BOT_UUID=maiwn569gziz935wv0w7kcch
WEB_UUID=cexv50jspo5gl3kq6ojypw43
DRILL_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "Drill started: $DRILL_START"
```

**Step 1 — Stop Coolify bot (освободить BOT_TOKEN в Telegram):**

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$BOT_UUID/stop" | jq .
# Дать Telegram 5–10 секунд на освобождение session slot:
sleep 10
```

**Step 2 — Stop Coolify web (освободить port 8080):**

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$WEB_UUID/stop" | jq .
```

**Step 3 — Verify port 8080 свободен и нет residual vibe-контейнеров:**

```bash
# Порт должен быть свободен:
sudo lsof -iTCP:8080 -sTCP:LISTEN || echo "port 8080 free ✓"
# Не должно быть running Coolify vibe-gatekeeper контейнеров:
docker ps --filter name=vibe --format 'table {{.Names}}\t{{.Status}}'
```

Если порт занят или vibe-* контейнер остался — остановить вручную через `docker stop <container>` до перехода к Step 4. Без этой проверки будет split-brain: два polling клиента на одном BOT_TOKEN.

**Step 4 — Start legacy stack:**

```bash
cd /home/claw/vibe-gatekeeper
docker compose up -d
docker compose ps
# Ожидаемо: bot, web, db, redis — все Up
```

**Step 5 — (Optional) Restore Coolify snapshot в legacy DB.**

Для drill **пропустить** этот шаг и принять, что legacy DB отстаёт с 2026-04-20. Restore требует совпадения схем (alembic revision) и занимает 5+ минут. В реальном production rollback — решать по ситуации (если Coolify DB физически недоступна, работать с legacy как есть).

**Step 6 — Verify liveness (3 слоя, идентично L1/L2/L3 из Task #10):**

```bash
# L1 — getMe:
BOT=$SHKODERBOT_BOT_TOKEN
curl -sS "https://api.telegram.org/bot$BOT/getMe" | jq '{ok, username:.result.username, id:.result.id}'
# Ожидаемо: ok:true, username:"vibeshkoder_bot"

# L2 — polling ownership (два запроса подряд):
curl -sS "https://api.telegram.org/bot$BOT/getUpdates?timeout=0&limit=1" > /dev/null
curl -sS "https://api.telegram.org/bot$BOT/getUpdates?timeout=0&limit=1" | jq '{ok, error_code, description}'
# Ожидаемо на втором запросе: ok:false, error_code:409, "terminated by other getUpdates"
# 409 = кто-то держит polling = legacy bot жив

# L3 — web port 8080:
curl -sS -I http://187.77.98.73:8080/ | head -1
# Ожидаемо: HTTP/1.1 302 Found (redirect на /login)
```

**Step 7 — Manual UX smoke:**

Отправить `/start` боту `@vibeshkoder_bot` из личного чата оператора. Ответ должен прийти ≤3s. Зафиксировать латентность в drill log.

### Success criteria

Drill считается успешным, если **все четыре** выполнены:

1. L1 getMe → `ok:true`, `username:"vibeshkoder_bot"`.
2. L2 getUpdates (второй запрос) → `409 Conflict`.
3. L3 `curl -I http://187.77.98.73:8080/` → `302 Found`.
4. `/start` в боте возвращает ответ ≤3s (manual check).

Downtime (с момента Step 1 до зелёного Step 6) должен быть ≤10 минут.

### Restore-after-drill (возврат в Coolify)

После фиксации результата — обязательный возврат в production state:

```bash
# 1. Stop legacy stack (освободить BOT_TOKEN и port 8080):
cd /home/claw/vibe-gatekeeper
docker compose down
sleep 10

# 2. Verify port 8080 и residual контейнеры (зеркально Step 3):
sudo lsof -iTCP:8080 -sTCP:LISTEN || echo "port 8080 free ✓"
docker ps --filter name=vibe --format 'table {{.Names}}\t{{.Status}}'

# 3. Start Coolify web:
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$WEB_UUID/start" | jq .

# 4. Start Coolify bot:
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$BOT_UUID/start" | jq .

# 5. Повторить L1/L2/L3 verify — всё должно быть зелёное, бот снова в Coolify.
```

Если env vars или custom_docker_run_options разошлись — восстановить из snapshot JSON, сделанного в Preconditions.

### Known limitations

- **Data-loss window**: все записи в Coolify DB после `DRILL_START` не попадут в legacy (и наоборот — записи в legacy DB за время drill не попадут обратно в Coolify после restore). Для 5–10 минут low-activity окна это приемлемо; для реального инцидента нужно принимать решение по ситуации.
- **Schema drift risk**: если между cutover (2026-04-20) и drill были alembic-миграции в Coolify, legacy compose может не стартовать (старый код vs новая схема). Перед drill проверить: `alembic_version` в обеих DB должен совпадать.
- **credentials.json hard dependency**: legacy compose монтирует `/home/claw/vibe-gatekeeper/credentials.json`. Если A3 (credentials decoupling) переместил файл — drill не работает до исправления пути.
- **Split-brain risk не 100% закрыт**: Step 3 минимизирует, но если между Step 3 и Step 4 Coolify авто-рестартует контейнер (auto-healing) — получим двух polling клиентов. Mitigation: в Coolify UI выставить auto-deploy=off на время drill.
- **pg_dump creds**: требуют docker exec в контейнер Coolify Postgres — если SSH-ключ не даёт доступа к Docker socket, шаг backup будет заблокирован.

### Drill log template

Сохранять в `/Users/eekudryavtsev/Vibe/products/shkoderbot/docs/ops/drill-YYYY-MM-DD.md`:

```markdown
# Rollback Drill — YYYY-MM-DD

- Operator: @handle
- Window (UTC): HH:MM–HH:MM
- Actual downtime: M минут S секунд
- Coolify DB snapshot: ~/drills/coolify-pre-drill-YYYYMMDD-HHMM.sql (size: XX MB)
- A3 status at drill time: [DONE / NOT DONE]

## Step results

| Step | Result | Notes |
|---|---|---|
| 1. Stop bot | OK / FAIL | API response code |
| 2. Stop web | OK / FAIL | API response code |
| 3. Port 8080 free | OK / FAIL | residual containers: [...] |
| 4. Legacy up | OK / FAIL | `docker compose ps` output |
| 5. DB restore | SKIPPED / OK | data-loss accepted |
| 6. L1/L2/L3 | OK / FAIL | L1: ..., L2: ..., L3: ... |
| 7. /start smoke | OK / FAIL | latency: Xs |

## Success criteria
- [ ] L1 ok:true
- [ ] L2 409 Conflict
- [ ] L3 HTTP 302
- [ ] /start ≤3s

## Anomalies
- ...

## Restore-after-drill
- Coolify web start: OK / FAIL
- Coolify bot start: OK / FAIL
- Post-restore L1/L2/L3: OK / FAIL
- Env vars drift: none / [diff]

## Verdict
PASS / FAIL / PARTIAL

## Action items
- ...
```
