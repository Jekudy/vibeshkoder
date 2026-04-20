# Coolify B+C Handoff — vibe-gatekeeper

Self-contained prompt for the session executing Spec B (staging cutover) and Spec C (production cutover + legacy removal). A new session can execute B and C using only this file plus the playbook at `~/Vibe/knowledge/nocoders/docs/architecture/coolify-deploy-playbook.md` and the project docs in `~/Vibe/products/shkoderbot/docs/ops/`.

Once Spec B starts executing, this file is frozen. New decisions during execution go into the playbook Recommendations section or project docs, never edits here.

## 1. Frozen decisions from Spec A

- GHCR pull mechanism: defaults to host-level `docker login` per current reality (2026-04-19 runbook entry). Playbook `p1` documents both mechanisms; Spec B records final choice.
- Proxy strategy recommendation: migrate vaultwarden into Coolify (playbook `p0.1`). Alternatives documented; Spec B picks.
- Observability baseline: healthchecks.io + TG alert + Sentry + error-rate / disk-free / OOM / 5xx alerts.
- Data migration: staging clean, prod `pg_dump`/`pg_restore`, Google creds as mounted file 0400, Redis state per playbook `p3`.
- Legacy removal: immediately after 48h prod monitoring window, secret rotation before cleanup, FS-specific shred, 72h kept-warm contract.
- `ag-mechanic`: Coolify-first default with mandatory playbook read and anchor citation.

## 2. Open Items deferred to Spec B

- Exact vaultwarden resolution (migrate / remove / alt-port).
- Concrete Alembic migration hook style pinned inside Coolify.
- Measured cutover timing budget (filled into `p6`).
- Fallback registry decision.
- Staging-ACME / backup certificate procedure.
- Secondary external observer pick (UptimeRobot / Pingdom / 2nd healthchecks.io).
- Final GHCR pull mechanism recorded in Recommendations section.

## 3. Command shapes ready to use

```
pg_dump --host=<legacy-host> --username=<user> --format=custom --file=/tmp/vibe-prod-$(date +%F).dump <db>
pg_restore --host=<coolify-pg-uuid> --username=<user> --dbname=<db> --no-owner /tmp/vibe-prod-<date>.dump
redis-cli -h <legacy-redis-host> --rdb /tmp/vibe-redis-$(date +%F).rdb
scp /tmp/vibe-redis-*.rdb <coolify-redis-volume-path>
```

## 4. Rollback commands pinned to current legacy digest

```
# Current legacy paths:
#   compose:  /home/claw/vibe-gatekeeper/docker-compose.yml
#   env:      /home/claw/vibe-gatekeeper/.env
# During cutover, compose is renamed to docker-compose.yml.locked-during-cutover.
# Rollback:
cd /home/claw/vibe-gatekeeper
mv docker-compose.yml.locked-during-cutover docker-compose.yml
docker compose up -d
curl -s "https://api.telegram.org/bot$BOT_TOKEN/getUpdates" | jq '.ok'   # expect true, no 409
```

## 5. Smoke-check list including E2E

Follow playbook `p5`. Infra checks plus this specific E2E for vibe-gatekeeper:
- test account applies → another test account vouches → admin approves → invite received.

## 6. Canonical references

- Playbook: `~/Vibe/knowledge/nocoders/docs/architecture/coolify-deploy-playbook.md`. Current STATUS: UNVERIFIED DRAFT. Every section touched by Spec B must update its `verified_on`.
- Spec A: `~/Vibe/products/shkoderbot/docs/superpowers/specs/2026-04-19-coolify-knowledge-layer-design.md`.
- This handoff: frozen once Spec B begins.
