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

As of 2026-04-19:

- GitHub repo exists at `https://github.com/Jekudy/vibe-gatekeeper`.
- CI is green on `main`.
- Release workflow is gated on successful CI and pushes bot/web images to GHCR.
- Coolify is installed on the VPS in parallel to the old runtime.
- Coolify dashboard is intentionally bound to the Tailscale IP only:
  - `http://100.101.196.21:8100`
- The old production runtime is still alive:
  - path: `/home/claw/vibe-gatekeeper`
  - public web: `0.0.0.0:8080`

## Coolify Registry & SSH (resolved 2026-04-19)

- GHCR pull is unblocked via `docker login ghcr.io -u Jekudy` on the VPS as root.
- Auth lives in `/root/.docker/config.json`.
- Coolify reuses the host Docker daemon, so no Coolify-side registry resource is needed.
- Coolify localhost server bootstrap was repaired again on 2026-04-19:
  - `servers.id=0` user reverted to `root`
  - Coolify localhost public key re-added to `/root/.ssh/authorized_keys`
  - server validation now reports `is_reachable=true`, `is_usable=true`

## Coolify Staging Resources

Created in Coolify on 2026-04-19, project `My first project` / environment `staging`:

| Kind | UUID | Notes |
|---|---|---|
| App `vibe-gatekeeper-web` | `cexv50jspo5gl3kq6ojypw43` | image `ghcr.io/jekudy/vibe-gatekeeper-web:main`, port `18080:8080` |
| App `vibe-gatekeeper-bot-staging` | `maiwn569gziz935wv0w7kcch` | image `ghcr.io/jekudy/vibe-gatekeeper-bot:main` |
| Postgres `vibe-gatekeeper-pg-staging` | `hdazvm5fz836xj9mdyn8c629` | `postgres:15-alpine`, db `vibe_gatekeeper`, user `vibe` |
| Redis `vibe-gatekeeper-redis-staging` | `gl28f0g5exzzo4k8w0auzygk` | `redis:7-alpine`, password set |

Internal connection strings:

- `DATABASE_URL=postgresql+asyncpg://vibe:<DB_PW>@hdazvm5fz836xj9mdyn8c629:5432/vibe_gatekeeper`
- `REDIS_URL=redis://default:<REDIS_PW>@gl28f0g5exzzo4k8w0auzygk:6379/0`

DB / Redis / web passwords are stored in Coolify env vars only.

## Remaining Manual Steps Before First Successful Staging Boot

The web app currently fails at startup because pydantic settings reject the placeholder values. To finish staging, the following env vars on **both** apps must be replaced with real values:

- `BOT_TOKEN` — staging bot token from `@BotFather` (or reuse the existing preview-env value if confirmed)
- `COMMUNITY_CHAT_ID` — must be a valid integer
- `GOOGLE_SHEET_ID` — staging Google Sheet ID
- `WEB_BASE_URL` (web only) — public URL once Caddy / sslip mapping is decided

Plus on the file system inside the web/bot containers:

- `/app/credentials.json` — Google service account JSON for `GOOGLE_SHEETS_CREDS_FILE`. Coolify volume mount or build-time secret needs to be wired.

After those values are set, redeploy web and bot from Coolify and run the smoke checks in `docs/ops/vibe-gatekeeper-staging-cutover.md`.

## Current Bootstrap Limitation

The old production runtime at `/home/claw/vibe-gatekeeper` remains the live user path until the new staging path is verified and a controlled bot cutover window is prepared.

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

