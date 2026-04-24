# vibe-gatekeeper — Claude Instructions

## Project Overview

Telegram gatekeeper bot (aiogram 3) plus a FastAPI admin web surface for community onboarding: applications, vouching, intro refresh, member visibility. Python 3.12, SQLAlchemy 2 async over asyncpg/PostgreSQL, Redis for FSM in prod, APScheduler for periodic jobs, `gspread` (sync) for Google Sheets sync, Jinja2 + Bootstrap 5 CDN for web UI. All frameworks verified by import scan in `bot/` and `web/`.

## Layout

```
bot/          34 .py, ~2,700 LOC — Telegram bot (aiogram 3)
  __main__.py     entry: Bot + Dispatcher + middleware + 7 routers + scheduler
  handlers/       7 routers: start, questionnaire, vouch, admin, chat_events, forward_lookup, chat_messages
  db/models.py    SQLAlchemy 2 DeclarativeBase (148 LOC)
  db/repos/       6 repositories: user, application, questionnaire, vouch, intro, message
  services/       scheduler.py, sheets.py (largest file), invite.py
  utils/          telegram.py — mention_for() utility
  middlewares/db_session.py   injects AsyncSession per aiogram update
  keyboards/ filters/ states/ texts.py
web/          15 files (10 .py + 4 .html + 1 .css) — FastAPI admin
  app.py          create_app factory + HTTP auth middleware
  auth.py         WEB_PASSWORD compare + itsdangerous cookie signer
  dependencies.py get_session() FastAPI Depends helper
  routes/         auth, dashboard, members
  templates/      base, login, dashboard, members (Bootstrap 5 CDN, no build step)
alembic/      3 .py, 205 LOC — migrations; single revision in 001_initial.py
tests/        12 .py, 52 tests — pytest collects from tests/ only
docs/         runbook.md (authoritative ops), ops/, superpowers/plans+specs, superflow/
```

## Commands

```bash
# Install (README uses pip; uv binary is installed but no uv.lock committed)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # dev extras: pytest, ruff

# Run
python -m bot                      # Telegram bot (long polling)
python -m web                      # FastAPI on 0.0.0.0:8080 via uvicorn

# Tests + lint + format (exactly what CI runs — see .github/workflows/ci.yml)
pytest -q                          # collects 52 tests from tests/ only
ruff check .                       # lint
ruff format --check .              # format (enforced in CI since sprint-1)

# Migrations
alembic revision --autogenerate -m "describe change"
alembic upgrade head

# Docker
docker compose up                  # bot + web + postgres:16-alpine + redis:7-alpine
```

Prereqs: `DATABASE_URL`, `BOT_TOKEN`, `WEB_PASSWORD` and ~9 other env vars. See `.env.example` for the full 12-key list. In `DEV_MODE=true` the bot uses SQLite (`vibe_gatekeeper.db`) and `MemoryStorage` for FSM, so Redis and Postgres are not required locally.

Sprint-2 env vars (all optional, commented out in `.env.example`):
- `INTRO_NUDGE_PHASE_1_MAX=5` — max intros before phase-1 nudge
- `INTRO_NUDGE_PHASE_2_MAX=8` — max intros before phase-2 nudge
- `LOGIN_RATE_LIMIT_PER_15M=5` — max /login attempts per 15 min per IP
- `TRUSTED_PROXY_HOSTS=*` — currently inert (see `bot/config.py` comment); placeholder for future proxy trust enforcement

## Environments

- **Local:** `DEV_MODE=true`, SQLite, in-memory FSM, separate dev bot token (`bot/__main__.py:34-39` + `bot/config.py`).
- **Staging:** `DEV_MODE=false`, separate bot token / DB / Redis / web password. Optional isolated chat. See `docs/runbook.md`.
- **Production:** `DEV_MODE=false`, prod bot token / DB / Redis / web password. Served from Coolify.

## Runtime status (post-cutover)

- **Prod cutover completed 2026-04-20.** `@vibeshkoder_bot` now runs under Coolify on the VPS (`187.77.98.73`, Coolify dashboard at Tailscale `100.101.196.21:8100`). See `docs/runbook.md:65-75, 106-117` for the full cutover log.
- **Legacy runtime at `/home/claw/vibe-gatekeeper` is stopped** (`docker compose down` on 2026-04-20). Files preserved for rollback; retention window ends ~2026-04-27 (7 days post-cutover per `docs/runbook.md:135`).
- **Coolify resources (prod, despite `-staging` suffix in names):** app `vibe-gatekeeper-web`, app `vibe-gatekeeper-bot-staging`, postgres `vibe-gatekeeper-pg-staging` (`postgres:15-alpine`), redis `vibe-gatekeeper-redis-staging` (`redis:7-alpine`). Names are cosmetic; rename later. Table at `docs/runbook.md:90-95`.
- **`credentials.json` is still mounted from the legacy host path** `/home/claw/vibe-gatekeeper/credentials.json` via `custom_docker_run_options` (`docs/runbook.md:104`). Tracked as P0 tech debt in `docs/superflow/project-health-report.md` — must move off legacy path before VPS cleanup.
- **Rollback procedure:** stop Coolify bot → stop Coolify web → `docker compose up -d` on legacy. Full steps in `docs/runbook.md:119-133`.
- **Release flow:** push to `main` → CI green → `.github/workflows/release.yml` builds both Dockerfiles and pushes `sha-<hash>` + `:main` to `ghcr.io/jekudy/vibe-gatekeeper-{bot,web}` → Coolify pulls `:main`.

## Known drift (keep in mind when reading docs)

- **`SPEC.md §7` has an implementation-differs note** (added sprint-2) — the section still describes a Telegram Login Widget + HMAC-SHA256, but now carries an italic note that the actual implementation is a password form + `itsdangerous.URLSafeTimedSerializer` cookie (`web/auth.py`, `web/routes/auth.py`). No Widget code exists.
- **`SPEC.md §1` structure diagram is now accurate** (fixed sprint-2) — `bot/handlers/chat_events.py` and `bot/db/repos/intro.py` added; test file list updated to reflect all 11 test files (12 files in `tests/` including `conftest.py`).
- **`README.md:30-36` + preflight mention `uv`** — `uv` binary is on PATH but `uv.lock` is absent and CI uses `pip install -e ".[dev]"` (`.github/workflows/ci.yml:34`). Treat pip as current truth.
- **Top-level `test_*.py` + `phone_login.py` / `scan_work.py` / `transcribe_voice.py`** are Telethon ops scripts, not pytest targets (`pyproject.toml:42-44` pins `testpaths = ["tests"]`). Needs `[ops]` extra.

## Key rules

- **GitHub is the source of truth; the VPS is not.** All changes flow local → PR → merge → CI → GHCR → Coolify. SSH to the VPS is for logs/diagnostics only.
- **Never commit secrets.** `.env`, `.env.staging`, `.env.production`, `credentials.json` all gitignored; one live credential value already leaked into `docs/runbook.md:110` — rotate before the next commit touches that file (see `docs/superflow/project-health-report.md` P0 #1).
- **`web` depends on `bot`, never the reverse.** `web/config.py`, `web/routes/dashboard.py`, `web/routes/members.py` import from `bot.db` and `bot.config`. There is no shared `core/` package yet — be careful renaming anything under `bot/db/`.
- **Web routes use `session: AsyncSession = Depends(get_session)` from `web/dependencies.py`.** Never open `async_session()` directly in route handlers — `get_session()` handles commit/rollback.
- **Scheduler jobs use `_run_with_session()` helper** (`bot/services/scheduler.py`). Do not open `async_session()` directly in new scheduler jobs.
- **`mention_for(user)` in `bot/utils/telegram.py`** — use for Telegram user mentions; handles username/first_name fallback. Works with both aiogram objects and SQLAlchemy models.
- **DEV_MODE gates schema creation path.** `bot/__main__.py:22-29` calls `Base.metadata.create_all` when `DEV_MODE=true`; prod uses Alembic. Keep the models + migrations in sync manually.
- **Migrations are baked into the bot container command** (`Dockerfile.bot:14` → `alembic upgrade head && python -m bot`). A bad migration = boot loop — tracked as P1 tech debt.
- **All user-facing strings live in `bot/texts.py`** (133 LOC, Russian). Don't inline strings in handlers.
- **Language policy (per global CLAUDE.md):** code, comments, docs in English; user communication in Russian.

## Where to look first

- `docs/runbook.md` — **authoritative** current ops state, Coolify resources, cutover log, rollback.
- `docs/superflow/project-health-report.md` — **authoritative** tech-debt and security backlog (45 findings, 1 critical + 9 high). Read before planning any refactor.
- `SPEC.md` — product intent **with caveats** above. Use as a requirements hint, verify against code.
- `bot/__main__.py` + `web/app.py` — entry points; two-paragraph read to orient on any new task.
- `.github/workflows/ci.yml` + `release.yml` — authoritative on what checks run and how images ship.

## Known issues / tech debt (top items — full list in health report)

| Priority | Issue | Evidence |
|----------|-------|----------|
| P0 | `WEB_PASSWORD` value committed in runbook | `docs/runbook.md:110` |
| P0 | `credentials.json` still on legacy path past 7-day retention window | `docs/runbook.md:104` |
| P1 | Weak config defaults (`changeme`, `admin`) in `bot/config.py:10-19` | `bot/config.py` |
| P1 | Migration in bot CMD → boot-loop on bad revision | `Dockerfile.bot:14` |
| P1 | ~20% test coverage (52 tests, core flows covered; db repos + sheets edge cases remain) | `tests/` |
| P1 | Session cookie missing `Secure` flag, prod served over HTTP | `web/routes/auth.py:37-43` |

<!-- updated-by-superflow:2026-04-25 sprint-2 -->
