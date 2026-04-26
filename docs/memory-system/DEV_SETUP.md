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

The dev bot is **in the real community chat**. As soon as `memory.ingestion.raw_updates.enabled`
becomes true, the dev DB will start collecting real member messages. To minimize blast radius:

- Keep the dev DB **local only** (no remote port binding — `127.0.0.1:5433` only).
- `.env.dev` is gitignored. Never commit it.
- Do NOT enable extraction / LLM / catalog / wiki flags in the dev environment until those
  phases are gated.
- When you are done testing for the day: `docker compose -f docker-compose.dev.yml down -v` to
  drop the dev volume, OR keep it but understand the data sensitivity.
- Once T1-12 + T1-13 land, `#nomem` / `#offrecord` detection will at least mark sensitive
  messages — but minimal exposure remains the operative principle.

## Out of scope for dev setup

- staging / prod credentials — never used here
- pulling prod data into dev — explicitly forbidden until a PII scrub pipeline exists
- Google Sheets sync — disabled by default in `.env.dev.example`
