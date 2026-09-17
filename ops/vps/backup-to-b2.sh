#!/usr/bin/env bash
# backup-to-b2.sh — encrypt and upload Shkoder / Harry backups to Backblaze B2.
# SHK #523. Spec: docs/spec-523-external-backups.md. Runbook: docs/ops/external-backup-runbook.md.
#
# Usage: backup-to-b2.sh shkoder|harry
#
# Shkoder reuses the dump that /usr/local/sbin/shkoder-pg-backup.sh already produces —
# this script never runs a second pg_dump against the production database.
# Harry dumps its own Postgres and snapshots selected SQLite/state files.
#
# Exit codes: 0 ok | 1 config error | 2 source unusable | 3 encryption failed | 4 upload/retention failed
set -euo pipefail

B2_REMOTE="${B2_REMOTE:-b2}"
B2_BUCKET="${B2_BUCKET:-vibe-backups}"
RETAIN_DAYS="${RETAIN_DAYS:-30}"
ENV_FILE="${BACKUP_ENV_FILE:-/srv/secrets/backup.env}"

SHKODER_DUMP_DIR="${SHKODER_DUMP_DIR:-/data/coolify/backups/shkoder-postgres}"
SHKODER_MAX_AGE_HOURS="${SHKODER_MAX_AGE_HOURS:-24}"
SHKODER_MIN_BYTES="${SHKODER_MIN_BYTES:-102400}"

HARRY_DB_CONTAINER="${HARRY_DB_CONTAINER:-harry-honcho-db}"
HARRY_DB_USER="${HARRY_DB_USER:-postgres}"
HARRY_DB_NAME="${HARRY_DB_NAME:-postgres}"
HARRY_VOLUME="${HARRY_VOLUME:-vgtwjekx8w3aujw15ykkv8x4_harry-hermes-data}"
HARRY_CONFIG_DIR="${HARRY_CONFIG_DIR:-/srv/harry/config}"
HARRY_MIN_BYTES="${HARRY_MIN_BYTES:-10240}"

# SQLite files are snapshotted via `sqlite3 .backup`; a plain cp while the WAL is
# live yields a torn copy (spec invariant 9). Harry's processes never stop for us.
HARRY_SQLITE_FILES=(state.db kanban.db cron/executions.db mcp_sessions/telegram_user.session)
HARRY_PLAIN_FILES=(auth.json .env channel_directory.json)

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { local code="$1"; shift; log "ERROR: $*" >&2; exit "$code"; }

# Object names. Shkoder derives its name from the source dump so that re-running
# the script on the same dump overwrites one object instead of piling up copies.
object_name() {
  case "${1:-}" in
    shkoder) printf '%s.gpg\n' "$(basename "${2:?source dump path required}")" ;;
    harry)   printf 'harry-%s.tar.gz.gpg\n' "${2:?run timestamp required}" ;;
    *)       printf 'unknown service: %s\n' "${1:-}" >&2; return 2 ;;
  esac
}

# Retention arguments, one per line — the caller reads them into an array so the
# --include glob is never expanded by the local shell.
#
# Invariant 7: deletion is confined both to this service's prefix AND to this
# script's own object-name pattern. Foodzy's backup script runs
# `rclone delete --min-age 30d b2:foodzy-backups/` recursively with no name
# filter, which is exactly the behaviour that must not be repeated here: it would
# let one service's retention policy delete another service's backups.
retention_args() {
  local base="${B2_REMOTE}:${B2_BUCKET}"
  case "${1:-}" in
    shkoder) printf '%s\n' --min-age "${RETAIN_DAYS}d" --include 'shkoder-pg-*.dump.gpg' "${base}/shkoder/" ;;
    harry)   printf '%s\n' --min-age "${RETAIN_DAYS}d" --include 'harry-*.tar.gz.gpg'    "${base}/harry/" ;;
    *)       printf 'unknown service: %s\n' "${1:-}" >&2; return 2 ;;
  esac
}

sqlite_backup_cmd() {
  printf '%s\n' sqlite3 "${1:?source required}" ".backup '${2:?destination required}'"
}

# Symmetric AES256. The passphrase is read from a file, never passed in argv —
# command-line arguments are world-readable through `ps` on a shared host.
gpg_encrypt_cmd() {
  printf '%s\n' gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
    --passphrase-file "${1:?passphrase file required}" \
    --output "${3:?output path required}" "${2:?input path required}"
}

notify_failure() {
  local message="$1"
  if ! curl --fail --show-error --silent --max-time 10 -X POST \
      "https://api.telegram.org/bot${TELEGRAM_DEV_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${ADMIN_TELEGRAM_ID}" \
      --data-urlencode "text=🔴 [${SERVICE} backup] FAILURE: ${message}" >/dev/null; then
    log "ERROR: Telegram failure notification could not be delivered" >&2
  fi
}

on_error() {
  local code=$? line="$1"
  log "backup failed at line ${line} (exit ${code})" >&2
  notify_failure "backup-to-b2.sh ${SERVICE} failed at line ${line} (exit ${code})"
  exit "$code"
}

load_env() {
  [[ -r "$ENV_FILE" ]] || die 1 "env file not readable: ${ENV_FILE}"
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  [[ -n "${BACKUP_PASSPHRASE_FILE:-}" ]]  || die 1 "BACKUP_PASSPHRASE_FILE missing in ${ENV_FILE}"
  [[ -r "$BACKUP_PASSPHRASE_FILE" ]]     || die 1 "passphrase file not readable: ${BACKUP_PASSPHRASE_FILE}"
  [[ -s "$BACKUP_PASSPHRASE_FILE" ]]     || die 1 "passphrase file is empty: ${BACKUP_PASSPHRASE_FILE}"
  [[ -n "${TELEGRAM_DEV_BOT_TOKEN:-}" ]]  || die 1 "TELEGRAM_DEV_BOT_TOKEN missing in ${ENV_FILE}"
  [[ -n "${ADMIN_TELEGRAM_ID:-}" ]]       || die 1 "ADMIN_TELEGRAM_ID missing in ${ENV_FILE}"
}

# Encrypt, record the checksum, upload, then prune.
publish() {
  local service="$1" plain="$2" object="$3"
  local encrypted="${WORKDIR}/${object}"

  local -a encrypt
  mapfile -t encrypt < <(gpg_encrypt_cmd "$BACKUP_PASSPHRASE_FILE" "$plain" "$encrypted")
  "${encrypt[@]}" || die 3 "gpg encryption failed for ${object}"

  local size sha
  size=$(stat -c%s "$encrypted")
  sha=$(sha256sum "$encrypted" | cut -d' ' -f1)
  log "encrypted ${object}: ${size} bytes sha256=${sha}"

  rclone copyto "$encrypted" "${B2_REMOTE}:${B2_BUCKET}/${service}/${object}" --log-level ERROR \
    || die 4 "upload to ${B2_REMOTE}:${B2_BUCKET}/${service}/${object} failed"
  log "uploaded ${B2_REMOTE}:${B2_BUCKET}/${service}/${object}"

  local -a prune
  mapfile -t prune < <(retention_args "$service")
  rclone delete "${prune[@]}" --log-level ERROR \
    || die 4 "retention prune failed for ${service}"
  log "retention applied: objects older than ${RETAIN_DAYS}d under ${service}/ removed"
}

backup_shkoder() {
  [[ -d "$SHKODER_DUMP_DIR" ]] || die 2 "dump directory missing: ${SHKODER_DUMP_DIR}"

  local -a found
  mapfile -t found < <(find "$SHKODER_DUMP_DIR" -maxdepth 1 -name 'shkoder-pg-*.dump' -printf '%T@ %p\n' | sort -rn)
  [[ "${#found[@]}" -gt 0 ]] || die 2 "no shkoder-pg-*.dump found in ${SHKODER_DUMP_DIR}"

  local dump="${found[0]#* }"

  # A stale dump means the local backup cron is broken. Uploading yesterday's copy
  # under today's date would hide that, so fail loudly instead.
  local age_seconds=$(( $(date +%s) - $(stat -c%Y "$dump") ))
  [[ "$age_seconds" -le $(( SHKODER_MAX_AGE_HOURS * 3600 )) ]] \
    || die 2 "newest dump is $(( age_seconds / 3600 ))h old, limit is ${SHKODER_MAX_AGE_HOURS}h: ${dump}"

  local size
  size=$(stat -c%s "$dump")
  [[ "$size" -ge "$SHKODER_MIN_BYTES" ]] \
    || die 2 "dump too small (${size} bytes < ${SHKODER_MIN_BYTES}): ${dump}"

  log "source dump ${dump} (${size} bytes, $(( age_seconds / 60 ))m old)"
  publish shkoder "$dump" "$(object_name shkoder "$dump")"
}

backup_harry() {
  local ts stage data
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  stage="${WORKDIR}/harry"
  mkdir -p "${stage}/sqlite" "${stage}/files"

  data=$(docker volume inspect "$HARRY_VOLUME" --format '{{.Mountpoint}}') \
    || die 2 "docker volume not found: ${HARRY_VOLUME}"

  docker exec "$HARRY_DB_CONTAINER" pg_dump -U "$HARRY_DB_USER" -d "$HARRY_DB_NAME" \
    --format=custom --no-owner --no-privileges > "${stage}/harry-honcho.dump" \
    || die 2 "pg_dump failed for ${HARRY_DB_CONTAINER}"
  log "honcho dump: $(stat -c%s "${stage}/harry-honcho.dump") bytes"

  local relative source destination
  local -a snapshot
  for relative in "${HARRY_SQLITE_FILES[@]}"; do
    source="${data}/${relative}"
    [[ -f "$source" ]] || die 2 "expected SQLite file missing: ${source}"
    destination="${stage}/sqlite/$(basename "$relative")"
    mapfile -t snapshot < <(sqlite_backup_cmd "$source" "$destination")
    "${snapshot[@]}" || die 2 "sqlite .backup failed for ${source}"
  done

  for relative in "${HARRY_PLAIN_FILES[@]}"; do
    source="${data}/${relative}"
    [[ -f "$source" ]] || die 2 "expected file missing: ${source}"
    cp -a "$source" "${stage}/files/"
  done

  [[ -d "$HARRY_CONFIG_DIR" ]] || die 2 "config directory missing: ${HARRY_CONFIG_DIR}"
  cp -a "$HARRY_CONFIG_DIR" "${stage}/files/config"

  # Caches (cache/, audio_cache/, .cache/, .codex/, home/) are deliberately absent:
  # they rebuild themselves and account for ~500 MB of the 525 MB volume.
  local archive="${WORKDIR}/harry-${ts}.tar.gz"
  tar -czf "$archive" -C "$stage" . || die 2 "tar failed for ${stage}"

  local size
  size=$(stat -c%s "$archive")
  [[ "$size" -ge "$HARRY_MIN_BYTES" ]] \
    || die 2 "archive too small (${size} bytes < ${HARRY_MIN_BYTES}): ${archive}"
  log "archive ${archive} (${size} bytes)"

  publish harry "$archive" "$(object_name harry "$ts")"
}

main() {
  SERVICE="${1:-}"
  case "$SERVICE" in
    shkoder|harry) ;;
    *) printf 'usage: %s shkoder|harry\n' "$0" >&2; exit 1 ;;
  esac

  load_env
  trap 'on_error $LINENO' ERR

  WORKDIR=$(mktemp -d /var/tmp/vibe-b2-backup.XXXXXX)
  # Intermediate copies never accumulate: the VPS has ~9.7 GB free at 80% usage.
  trap 'rm -rf "$WORKDIR"' EXIT

  log "START ${SERVICE} → ${B2_REMOTE}:${B2_BUCKET}/${SERVICE}/"
  "backup_${SERVICE}"
  log "OK ${SERVICE}"
}

if [[ -z "${BACKUP_TO_B2_LIB:-}" ]]; then
  main "$@"
fi
