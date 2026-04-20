# Vibe Gatekeeper Production Cutover Plan

Scaffold. Every `<filled by Spec C on <date>>` must be resolved before cutover.

## Data migration

- Postgres: `pg_dump` from legacy → `pg_restore` into Coolify Postgres. Command shape: <filled by Spec C on <date>>.
- Redis: approach chosen per playbook#p3 — <filled by Spec C on <date>> (drain / RDB copy / accept loss).
- Google credentials: mounted at `/app/credentials.json`, mode 0400. Transfer procedure: <filled by Spec C on <date>>.

## Cutover window

- Scheduled time: <filled by Spec C on <date>>.
- Expected duration (per playbook#p6 timing budget × 2): <filled by Spec C on <date>>.
- Announcement channel: <filled by Spec C on <date>>.

## Rollback commands (ready before cutover)

See playbook#p6 — copy the exact rollback runbook here with UUIDs and paths filled in:

```
<filled by Spec C on <date>>
```

## Legacy archive

- Archive path: `/root/vibe-gatekeeper-legacy-archive-<date>.tar.gz`.
- 72h silence window ends: <filled by Spec C on <date>>.
- Re-verification checkpoints (T+24h, T+48h): see playbook#p7 legacy-kept-warm contract.

## Secret rotation list

Executed BEFORE cleanup, per playbook#p7:

- [ ] bot token
- [ ] DB password
- [ ] Redis password
- [ ] Google service-account key
- [ ] GHCR PAT
- [ ] webhook secrets
- [ ] admin web password
- [ ] session secrets

## Shred procedure (filesystem-specific)

- Detected VPS filesystem: <filled by Spec C on <date>>.
- Chosen path (per playbook#p7): <filled by Spec C on <date>>.
- Commands: <filled by Spec C on <date>>.
