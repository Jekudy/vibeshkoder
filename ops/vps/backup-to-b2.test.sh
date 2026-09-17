#!/usr/bin/env bash
# Runnable check for backup-to-b2.sh logic: object naming, retention scoping,
# SQLite snapshot and gpg encryption commands. No network, no disk writes, no Docker.
# Run: bash ops/vps/backup-to-b2.test.sh
set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/backup-to-b2.sh"
FAILED=0

fail() { echo "FAIL: $*" >&2; FAILED=1; }
ok()   { echo "ok: $*"; }

assert_eq() {
  local want="$1" got="$2" what="$3"
  if [[ "$want" == "$got" ]]; then ok "$what"; else fail "$what: want [$want], got [$got]"; fi
}

assert_contains() {
  local hay="$1" needle="$2" what="$3"
  if [[ "$hay" == *"$needle"* ]]; then ok "$what"; else fail "$what: [$needle] not found in [$hay]"; fi
}

assert_not_contains() {
  local hay="$1" needle="$2" what="$3"
  if [[ "$hay" != *"$needle"* ]]; then ok "$what"; else fail "$what: [$needle] must not appear in [$hay]"; fi
}

if [[ ! -f "$SCRIPT" ]]; then
  echo "FAIL: script not found: $SCRIPT" >&2
  exit 1
fi

# shellcheck source=/dev/null
BACKUP_TO_B2_LIB=1 source "$SCRIPT"

for fn in object_name retention_args sqlite_backup_cmd gpg_encrypt_cmd; do
  if ! declare -F "$fn" >/dev/null; then
    echo "FAIL: $SCRIPT does not define $fn()" >&2
    exit 1
  fi
done

# --- object naming ---------------------------------------------------------
assert_eq "shkoder-pg-20260917T031701Z.dump.gpg" \
  "$(object_name shkoder /data/coolify/backups/shkoder-postgres/shkoder-pg-20260917T031701Z.dump)" \
  "shkoder object name derives from source dump timestamp"

assert_eq "harry-20260918T034000Z.tar.gz.gpg" \
  "$(object_name harry 20260918T034000Z)" \
  "harry object name uses run timestamp"

# --- retention scoping (invariant 7) ---------------------------------------
for svc in shkoder harry; do
  args="$(retention_args "$svc" | tr '\n' ' ')"
  assert_contains "$args" "b2:vibe-backups/${svc}/" "retention $svc targets its own prefix"
  assert_contains "$args" "--min-age 30d"           "retention $svc uses the 30d window"
  assert_contains "$args" "--include"               "retention $svc filters by object name"
  # The bucket root must never be a deletion target: foodzy's script deletes
  # recursively across b2:foodzy-backups/ and that is exactly what we must not do.
  assert_not_contains "$args" "b2:vibe-backups/ "   "retention $svc never targets the bucket root"
  assert_not_contains "$args" "--rmdirs"            "retention $svc does not remove directories"
done

shk_args="$(retention_args shkoder | tr '\n' ' ')"
assert_contains "$shk_args" "shkoder-pg-*.dump.gpg" "shkoder retention include pattern is its own"
assert_not_contains "$shk_args" "harry"             "shkoder retention cannot reach harry objects"

har_args="$(retention_args harry | tr '\n' ' ')"
assert_contains "$har_args" "harry-*.tar.gz.gpg"    "harry retention include pattern is its own"
assert_not_contains "$har_args" "shkoder"           "harry retention cannot reach shkoder objects"

# --- SQLite consistency (invariant 9) --------------------------------------
sql_cmd="$(sqlite_backup_cmd /src/state.db /tmp/out/state.db | tr '\n' ' ')"
assert_contains "$sql_cmd" ".backup" "SQLite snapshot uses .backup, not cp"
assert_not_contains "$sql_cmd" "cp " "SQLite snapshot never shells out to cp"

# --- gpg encryption (user decision: symmetric gpg, not age) -----------------
gpg_cmd="$(gpg_encrypt_cmd /pass/file /in/dump /out/dump.gpg | tr '\n' ' ')"
assert_contains "$gpg_cmd" "--symmetric"            "gpg encrypts symmetrically"
assert_contains "$gpg_cmd" "--cipher-algo AES256"   "gpg uses AES256"
assert_contains "$gpg_cmd" "--passphrase-file"      "gpg reads the passphrase from a file"
assert_contains "$gpg_cmd" "--batch"                "gpg runs non-interactively"
# A passphrase passed as an argument is visible in `ps` to every user on the box.
assert_not_contains "$gpg_cmd" "--passphrase "      "gpg never takes the passphrase in argv"

# --- unknown service is rejected -------------------------------------------
if object_name nosuchservice x >/dev/null 2>&1; then
  fail "unknown service must be rejected by object_name"
else
  ok "unknown service is rejected by object_name"
fi

if retention_args nosuchservice >/dev/null 2>&1; then
  fail "unknown service must be rejected by retention_args"
else
  ok "unknown service is rejected by retention_args"
fi

# --- no silent failures (invariant 6) --------------------------------------
if grep -nE '\|\|[[:space:]]*true' "$SCRIPT" | grep -vE '^\s*[0-9]+:\s*#'; then
  fail "script contains '|| true' over an error path"
else
  ok "script has no '|| true' over error paths"
fi

if [[ "$FAILED" -eq 0 ]]; then
  echo "ALL CHECKS PASSED"
else
  echo "CHECKS FAILED" >&2
fi
exit "$FAILED"
