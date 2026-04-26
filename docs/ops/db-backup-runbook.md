# Shkoder Postgres — Daily Backup Runbook

Last updated: 2026-04-26
Owner: ag-mechanic
Sprint: H5

## Summary

The Coolify-managed Postgres for the Shkoderbot prod stack is now snapshotted
daily by a host-cron `pg_dump` script. Backups live on the same VPS volume that
holds the database (single-volume risk acknowledged — see "Known limitations").

## What is backed up

- **Coolify service:** `vibe-gatekeeper-pg-prod`
- **Coolify DB UUID:** `hdazvm5fz836xj9mdyn8c629`
- **Container name:** `hdazvm5fz836xj9mdyn8c629`
- **Image:** `postgres:15-alpine` (PG 15.17)
- **Database:** `vibe_gatekeeper`
- **DB user:** `vibe`
- **Data volume (on host):** `/var/lib/docker/volumes/postgres-data-hdazvm5fz836xj9mdyn8c629/_data`
- **Tables snapshotted:** `users`, `applications`, `intros`, `intro_refresh_tracking`,
  `vouch_log`, `questionnaire_answers`, `chat_messages`, `alembic_version`
  (TOC count: 70 entries on first run).

The other Postgres on the host (`q14nlmnl2b7duhvqq9805a2e`, postgres:16-alpine —
foodzy stack inside Coolify) and the legacy `foodzy-postgres-1` container are
explicitly **out of scope** for this runbook.

## Why host cron + pg_dump (not Coolify built-in toggle)

Coolify v4.0.0-beta.472 has a built-in scheduled-backup feature
(`scheduled_database_backups` table in `coolify-db`), but the public v1 API does
not expose `POST` endpoints to create or edit a backup schedule. As of
2026-04-26 there are zero rows in that table for ANY database on this Coolify
instance. The supported path to enable it is the Coolify UI → service →
**Backups** → enable.

We chose a host-cron `pg_dump` for now because:

1. It is explicit and version-independent — no dependency on Coolify internals.
2. It can be reviewed, tested, and rotated without touching the Coolify DB.
3. The Coolify UI toggle can be enabled in addition later, redundantly. See
   "Future work" below.

## Backup mechanism

### Script

- Path: `/usr/local/sbin/shkoder-pg-backup.sh`
- Mode: `0750`, owner `root:root`
- Behavior:
  1. `docker exec hdazvm5fz836xj9mdyn8c629 pg_dump -U vibe -d vibe_gatekeeper --format=custom --no-owner --no-privileges`
     into `/data/coolify/backups/shkoder-postgres/shkoder-pg-<UTC-timestamp>.dump.tmp`
  2. Atomic rename to `.dump` after the dump completes successfully.
  3. `chmod 0600` on the final file.
  4. Sanity floor: file must be at least 100 KB; otherwise script exits non-zero
     and leaves the file in place for inspection.
  5. `pg_restore --list` round-trip — verifies the dump is structurally valid
     before pruning anything.
  6. Retention prune: `find -mtime +7 -delete` for any `shkoder-pg-*.dump` in
     the destination dir.
- Logs: `/var/log/shkoder-pg-backup.log` (mode `0640`, owner `root`).
- Exit codes:
  - `0` — success
  - `1` — `pg_dump` failed
  - `2` — dump too small or `pg_restore --list` rejected the file
  - `3` — retention prune failed

### Schedule

- Cron entry: `/etc/cron.d/shkoder-pg-backup`
- Runs as `root`, daily at **03:17 UTC** (off-peak; ≈04:17 Belgrade local).
- `cron.service` is `active (running)` on the VPS (verified 2026-04-26).

### Storage

- Destination dir: `/data/coolify/backups/shkoder-postgres/`
- Mode: `0700`, owner `root:root`
- Filename pattern: `shkoder-pg-YYYYMMDDTHHMMSSZ.dump`
- Format: Postgres custom (`pg_dump --format=custom`). Restore via
  `pg_restore`.
- Retention: **7 days rolling** (older files removed at end of each successful
  run).
- Typical size today: ~960 KB per dump (DB is small — ~3.1k chat_messages rows,
  ~275 users).

### Disk headroom

- VPS root partition (`/dev/sda1`) at the time of setup: 48 GB total, 24 GB
  free (51% used).
- 7 daily dumps at ~1 MB each ≈ 7 MB peak. No disk-pressure concern.

## How to verify the last backup ran

```bash
ssh foodzy-vps
sudo ls -lt /data/coolify/backups/shkoder-postgres/ | head
sudo tail -20 /var/log/shkoder-pg-backup.log
```

The most recent file should be ≤24 h old. The log should end in `=== ... OK ===`.

To inspect contents of a dump without restoring:

```bash
sudo docker exec -i hdazvm5fz836xj9mdyn8c629 pg_restore --list \
  < /data/coolify/backups/shkoder-postgres/shkoder-pg-<ts>.dump | head -30
```

To trigger a one-off backup manually (for testing or before risky operations):

```bash
sudo /usr/local/sbin/shkoder-pg-backup.sh
echo exit=$?
sudo tail -20 /var/log/shkoder-pg-backup.log
```

## How to restore

### Full restore into the existing Coolify Postgres (DESTRUCTIVE)

This drops and re-creates objects. Stop the bot first to avoid open connections.

```bash
ssh foodzy-vps
# Set these env vars from your operator notes / ~/.env.tokens:
TOKEN="${COOLIFY_API_TOKEN:?set Coolify API token}"
API="${COOLIFY_API_URL:-http://100.101.196.21:8100/api/v1}"
BOT_UUID="${SHKODER_BOT_APP_UUID:?set bot app UUID — see Coolify UI}"
WEB_UUID="${SHKODER_WEB_APP_UUID:?set web app UUID — see Coolify UI}"

# 1) Stop bot + web (release DB connections):
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$BOT_UUID/stop"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$WEB_UUID/stop"

# 2) Pick the dump:
DUMP=/data/coolify/backups/shkoder-postgres/shkoder-pg-<ts>.dump
sudo ls -lh "$DUMP"

# 3) Pipe into pg_restore inside the PG container:
sudo docker exec -i hdazvm5fz836xj9mdyn8c629 \
  pg_restore --clean --if-exists --no-owner --no-privileges \
             --dbname=vibe_gatekeeper -U vibe < "$DUMP"

# 4) Spot-check row counts:
sudo docker exec hdazvm5fz836xj9mdyn8c629 \
  psql -U vibe -d vibe_gatekeeper \
  -c "SELECT 'users' t, count(*) FROM users
      UNION ALL SELECT 'applications', count(*) FROM applications
      UNION ALL SELECT 'intros', count(*) FROM intros
      UNION ALL SELECT 'chat_messages', count(*) FROM chat_messages;"

# 5) Restart apps:
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$WEB_UUID/start"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$API/applications/$BOT_UUID/start"
```

Expected baseline row counts (from 2026-04-20 cutover): `users=275`,
`applications=58`, `intros=44`, `vouch_log=39`, `questionnaire_answers=340`,
`chat_messages=3109+`. Counts only grow over time.

### Smoke restore into a throwaway DB (recommended monthly)

Validates the dump is restorable without touching prod:

```bash
ssh foodzy-vps
DUMP=/data/coolify/backups/shkoder-postgres/shkoder-pg-<ts>.dump
sudo docker run -d --name pg-restore-test \
  --network coolify \
  -e POSTGRES_USER=vibe -e POSTGRES_PASSWORD=test -e POSTGRES_DB=scratch \
  postgres:15-alpine
sleep 5
sudo docker exec -i pg-restore-test \
  pg_restore --clean --if-exists --no-owner --no-privileges \
             --dbname=scratch -U vibe < "$DUMP"
sudo docker exec pg-restore-test \
  psql -U vibe -d scratch -c "SELECT count(*) FROM users;"
sudo docker rm -f pg-restore-test
```

## Known limitations & risks

1. **Single-volume blast radius.** Dumps live on the same `/dev/sda1` as the
   PG data volume. A full disk failure or VPS loss would wipe both. Off-host
   storage (S3 / B2) is the next iteration — see "Future work".
2. **No encryption.** Dumps are written in plaintext custom format. They
   contain user data and should be encrypted (e.g. `age` or `gpg`) before
   any off-host upload. For local-only storage on a single-tenant VPS,
   the file mode `0600` + `0700` directory mode + root-only access is the
   current control.
3. **Ad-hoc pause is not built in.** If the DB is undergoing a long migration
   at 03:17 UTC, the script will still try to dump. PG handles this fine
   (snapshot under MVCC), but a multi-GB migration could slow down. Today the
   DB is ~1 MB so this is a non-issue.
4. **Coolify UI toggle is not enabled.** Coolify's `scheduled_database_backups`
   table has zero rows. If the Coolify UI is later used to enable backups, the
   two systems will run independently. That is fine (redundant) but should be
   documented.
5. **`pg_restore --list` validation runs in the source container.** A future
   PG major version mismatch between the source container and the dump version
   would surface as a validation failure here, not at restore time. Acceptable
   trade-off.
6. **Cron job is host-managed**, not in Coolify. Removing the VPS or
   re-installing the OS will lose the cron entry. The script and cron file are
   tracked in this runbook, not in git. Future work: move into a versioned
   `scripts/` folder + a deploy step (out of scope for H5).

## Future work (deferred)

- **Off-host storage.** Add a post-backup hook that uploads the latest dump
  encrypted (age) to S3-compatible storage (Backblaze B2 candidate). Per the
  H5 task, this is **deferred to user** — needs B2 account + bucket creation.
- **Coolify UI redundancy.** Enable Coolify UI → service `vibe-gatekeeper-pg-prod`
  → Backups, with the same daily schedule. Both will coexist.
- **Restore drill cadence.** Schedule a monthly smoke restore into a throwaway
  PG (per playbook `p3.5`) and log results in `docs/ops/restore-drill-YYYY-MM-DD.md`.
- **Alerting.** Currently the cron job has no alerting on failure. A simple
  enhancement: write `last_run_ts` and `last_run_status` files into the
  destination dir, and have a separate watchdog (or a Telegram operator
  service) check them daily.

## Change log

- **2026-04-26** — Initial setup: script `/usr/local/sbin/shkoder-pg-backup.sh`,
  cron `/etc/cron.d/shkoder-pg-backup`, dest `/data/coolify/backups/shkoder-postgres/`,
  retention 7 days. First manual run produced
  `shkoder-pg-20260426T173833Z.dump` (962 KB), validated by `pg_restore --list`.
