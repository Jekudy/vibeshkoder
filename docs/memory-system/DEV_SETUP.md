# Dev Setup — Memory System Live Testing

Goal: run a dev instance of the bot against a separate dev postgres so we can verify the full
ingestion + normalization + policy detection pipeline on real Telegram messages, without
touching staging or prod data.

## Components

- **Dev bot** (token `8732572884:...` — kept in `.env.dev`, gitignored). Already added to the
  target community chat with sufficient rights.
- **Dev postgres** — isolated container on port 5433 (`docker-compose.dev.yml`).
- **Local bot process** — runs on the host (`python -m bot`) reading `.env.dev` so we can
  hot-iterate.

## First-time setup

```bash
cd .worktrees/memory
cp .env.dev.example .env.dev
# fill in TELEGRAM_BOT_TOKEN, COMMUNITY_CHAT_ID, ADMIN_IDS, WEB_PASSWORD
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d postgres-dev
docker compose -f docker-compose.dev.yml ps   # confirm postgres-dev is healthy
alembic upgrade head
```

## Run the dev bot

```bash
# in worktree root
set -a; source .env.dev; set +a
python -m bot
```

Logs go to stdout. Keep this in one terminal; in another terminal do `psql` or `docker exec` to
inspect tables.

## Inspect dev DB

```bash
docker exec -it shkoder-postgres-dev psql -U shkoder_dev -d shkoder_dev -c "\dt"
docker exec -it shkoder-postgres-dev psql -U shkoder_dev -d shkoder_dev -c "select count(*) from chat_messages;"
```

## Reset dev DB

```bash
docker compose -f docker-compose.dev.yml down -v   # WARNING: drops the dev volume
docker compose -f docker-compose.dev.yml up -d postgres-dev
alembic upgrade head
```

## Live testing flow per ticket

For each Phase 1 ticket that touches ingestion (T1-03, T1-04, T1-05, T1-06, T1-12, T1-14):

1. Apply migration locally: `alembic upgrade head`.
2. Restart dev bot.
3. Send a test message in the dev community chat (or use `/forward` for forward_lookup tests).
4. Verify the row appears in the expected table:
   - `select * from telegram_updates order by id desc limit 5;`
   - `select * from chat_messages order by id desc limit 5;`
   - `select * from message_versions order by id desc limit 5;`
   - `select * from offrecord_marks order by id desc limit 5;`
5. For edited_message: edit the test message in Telegram, verify a new `message_versions` row
   appears with `version_seq = 2`.
6. For policy detection: send a message containing `#nomem` or `#offrecord` token, verify
   `memory_policy` and `offrecord_marks`.

## Privacy guardrails for dev DB

**Default rule: dev testing happens in a dedicated sandbox Telegram chat, NOT the real
community chat.** The dev bot ALSO has access to the real community chat for cases where
real-traffic verification is required, but that path requires explicit team-lead approval AND
a verified pre-commit `#offrecord` redaction (T1-12 + T1-13 + the `#offrecord` ordering rule
in AUTHORIZED_SCOPE.md).

### Hard guardrails (verifiable)

1. **Sandbox-first.** Default `COMMUNITY_CHAT_ID` in `.env.dev` points at a dedicated test
   chat. Tests in the real community chat require a separate `.env.dev.real` (which still
   stays gitignored) AND team-lead sign-off in the PR description.
2. **Raw archive disabled by default.** `.env.dev.example` sets
   `MEMORY_INGESTION_RAW_UPDATES_ENABLED=false`. The T1-01 feature flag migration MUST default
   the DB flag to `false`. Verify with:
   ```bash
   docker exec shkoder-postgres-dev psql -U shkoder_dev -d shkoder_dev -c \
     "select flag_key, enabled from feature_flags where flag_key like 'memory.%';"
   ```
   Expected: every memory.* flag → `f`. If any is `t` without explicit team-lead approval,
   stop and investigate.
3. **No remote port binding.** `docker-compose.dev.yml` binds postgres to `127.0.0.1:5433`
   only — never `0.0.0.0`. Verify with `docker ps --format '{{.Ports}}' | grep 5433`.
4. **`.env.dev` and `.env.dev.real` are gitignored.** Never commit. `git check-ignore -q
   .env.dev` must succeed.
5. **No LLM / extraction / catalog / wiki flags** in dev until those phases are gated.
6. **Volume cleanup after risky tests:** `docker compose -f docker-compose.dev.yml down -v`
   drops the dev volume. Run after any session that touched the real chat.
7. **No prod data pulls.** Never copy staging or prod DB content into dev. There is no PII
   scrub pipeline yet — until one exists, this is forbidden.

### Pre-commit `#offrecord` requirement (cross-ref)

Until T1-12 + T1-13 land AND verifiable redaction is wired in T1-04 (per AUTHORIZED_SCOPE.md
§`#offrecord` ordering rule), the dev environment MUST run with
`MEMORY_INGESTION_RAW_UPDATES_ENABLED=false` even in sandbox mode. This prevents accidental
durable storage of `#offrecord` content during early development.

## Out of scope for dev setup

- staging / prod credentials — never used here
- pulling prod data into dev — explicitly forbidden until a PII scrub pipeline exists
- Google Sheets sync — disabled by default in `.env.dev.example`
