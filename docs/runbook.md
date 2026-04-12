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

## Current Bootstrap Limitation

The old production runtime at `/home/claw/vibe-gatekeeper` remains the live user path until the new staging path is verified and a controlled bot cutover window is prepared.
