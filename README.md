# Vibe Gatekeeper

Telegram + web gatekeeper for community onboarding, applications, vouching, intro refresh, and admin visibility.

## Deployment Standard

The production deployment path is:

`develop anywhere -> push to GitHub -> GitHub Actions test -> build immutable images -> push to GHCR -> VPS runner deploys with Docker Compose`

GitHub is the source of truth. `ops/compose/deploy.py` deploys SHA-pinned bot/web
images and restores their previous image pins on failed readiness checks. It never
restores database data automatically.

## VPS Operations (#521)

Production manifests live in `ops/vps/`; each runtime directory contains
`compose.yaml` (the proxy uses `proxy.compose.yaml`), private service env files
and application `.env` files with image pins:

| Runtime directory | Service |
| --- | --- |
| `/srv/shkoder` | Gatekeeper bot, web, PostgreSQL and Redis |
| `/srv/foodzy` | Foodzy bot and PostgreSQL |
| `/srv/harry` | Harry and its dependencies |
| `/srv/otp` | Shared OTP bot |
| `/srv/edge-proxy` | Independent HTTPS proxy |

The existing external Docker network retains the name `coolify`; that name does not
require the Coolify panel. Host cron and systemd jobs remain outside Compose.
Existing database volumes and bind mounts are preserved. Secrets never belong in git.

On the VPS, check the private bot database endpoint:

```bash
docker compose -f /srv/shkoder/compose.yaml exec -T bot python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:3000/healthz/db", timeout=10).read().decode())'
```

Bot `/healthz` also uses private port 3000; web is public only through HTTPS.
The scheduled health workflow checks app/DB/Telegram health on the VPS and public
HTTPS independently from a GitHub-hosted runner (`PUBLIC_HEALTH_URL` repository
variable). Retain old stopped containers and configuration snapshots for rollback;
stop their replacements before restarting them. This is container/image rollback,
not a database restore. See [issue #521](https://github.com/Jekudy/vibeshkoder/issues/521)
for current cutover and verification evidence.

## Development Workflow

GitHub Issues is the only project tracker. Every change starts from an open issue and
every PR closes one:

`idea or research -> BMAD workflow -> GitHub Issue -> branch/worktree -> implementation -> PR`

- Thinking work is complete only after its outcome creates or updates a GitHub Issue.
- Do not create a branch, worktree, or implementation change before the issue exists.
- PR descriptions must contain `Closes #<issue-number>` or an equivalent closing keyword.
- BMAD artifacts and repository documents support an issue; they are not a parallel backlog.

[BMAD BMM](https://docs.bmad-method.org/) is installed for Codex and Claude Code. Run
`bmad-help` in a fresh task to choose the next workflow. For an established project,
`bmad-quick-dev` fits small changes; major features should pass through analysis, PRD,
architecture, and stories as needed.

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

- Stop the old production polling bot before starting its replacement.
- Keep retained containers and external volumes until migration checks and rollback
  verification are complete; never start two database containers on the same volume.
- Never use `docker compose down -v`, prune user data, delete retained volumes or
  restore database backups without a verified backup and explicit user approval.
