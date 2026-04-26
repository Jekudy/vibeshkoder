# Vibe Gatekeeper

Telegram + web gatekeeper for community onboarding, applications, vouching, intro refresh, and admin visibility.

## Deployment Standard

The target deployment path for this project is:

`develop anywhere -> push to GitHub -> GitHub Actions test -> build immutable images -> push to GHCR -> Coolify deploys pre-built images`

The VPS is not the source of truth anymore.

## Local Development

### 1. Create env

Copy `.env.example` to `.env` and fill the required values.

Recommended local settings:

- `DEV_MODE=true` — enables permissive checks (e.g. ephemeral web password)
- a separate development bot token
- local-only `WEB_PASSWORD`
- a `DATABASE_URL` pointing at a local postgres (see step 1.5)

`DEV_MODE=true` makes the web app generate ephemeral credentials and uses in-memory FSM
storage instead of Redis. **It does NOT change the database driver** — postgres is required
in all environments (T0-02; see `bot/db/engine.py`). Sqlite is no longer supported as a
runtime DB.

### 1.5. Start a local postgres

If you are working on the memory system cycle, use the dev postgres in the memory worktree:

```bash
cd .worktrees/memory
cp .env.dev.example .env.dev
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d postgres-dev
alembic upgrade head
```

For other workflows, any reachable postgres 16 instance works — set `DATABASE_URL` to its URL.

### 2. Install dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Run local checks

```bash
pytest -q              # DB-backed tests skip cleanly if postgres is unreachable
ruff check .
```

### 4. Run the bot

```bash
python -m bot
```

### 5. Run the web app

```bash
python -m web
```

## Environment Files

- `.env.example` — local baseline
- `.env.staging.example` — staging shape
- `.env.production.example` — production shape

Secrets must stay outside git:

- `.env`
- `.env.staging`
- `.env.production`
- `credentials.json`

## Images

This repo publishes two GHCR images:

- `ghcr.io/jekudy/vibe-gatekeeper-bot`
- `ghcr.io/jekudy/vibe-gatekeeper-web`

## Production Safety

- The current legacy VPS runtime remains the live path during bootstrap.
- Production bot-token cutover is a separate controlled step because polling bots cannot safely run two prod consumers at once.
