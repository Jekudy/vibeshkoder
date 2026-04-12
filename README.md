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

- `DEV_MODE=true`
- a separate development bot token
- local-only `WEB_PASSWORD`

With `DEV_MODE=true`, the app uses:

- SQLite (`vibe_gatekeeper.db`)
- in-memory FSM storage instead of Redis

### 2. Install dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Run local checks

```bash
pytest -q
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
