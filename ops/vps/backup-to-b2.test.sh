#!/usr/bin/env bash
# Runnable check for backup-to-b2.sh: retention scoping and the actual call
# sequence inside publish(). No network, no Docker, no B2.
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

# Without these two guards an empty or missing function turns every
# assert_not_contains below into a false "ok".
if [[ ! -f "$SCRIPT" ]]; then echo "FAIL: script not found: $SCRIPT" >&2; exit 1; fi
# shellcheck source=/dev/null
BACKUP_TO_B2_LIB=1 source "$SCRIPT"
for fn in retention_args read_args bucket_exists publish; do
  if ! declare -F "$fn" >/dev/null; then echo "FAIL: $SCRIPT does not define $fn()" >&2; exit 1; fi
done

# --- retention scoping (invariant 7) ---------------------------------------
# Foodzy's backup deletes across its whole bucket with no name filter. Ours must
# be confined to both its own prefix and its own object-name pattern.
for svc in shkoder harry; do
  args="$(retention_args "$svc" | tr '\n' ' ')"
  assert_contains "$args" "b2:jekudy-vibe-backups/${svc}/" "retention $svc targets its own prefix"
  assert_contains "$args" "--min-age 30d"                  "retention $svc uses the 30d window"
  assert_contains "$args" "--include"                      "retention $svc filters by object name"
done

shk_args="$(retention_args shkoder | tr '\n' ' ')"
assert_contains     "$shk_args" "shkoder-pg-*.dump.gpg" "shkoder retention include pattern is its own"
assert_not_contains "$shk_args" "harry"                 "shkoder retention cannot reach harry objects"

har_args="$(retention_args harry | tr '\n' ' ')"
assert_contains     "$har_args" "harry-*.tar.gz.gpg" "harry retention include pattern is its own"
assert_not_contains "$har_args" "shkoder"            "harry retention cannot reach shkoder objects"

if retention_args nosuchservice >/dev/null 2>&1; then
  fail "unknown service must be rejected by retention_args"
else
  ok "unknown service is rejected by retention_args"
fi

# --- publish(): real call sequence and real arguments ----------------------
# These assertions sit on the execution path, not on a command generator: if
# publish() stops calling gpg or the bucket check, they fail.
set +e
STUB_HOME=$(mktemp -d "${TMPDIR:-/tmp}/backup-to-b2-test.XXXXXX") || { echo "FAIL: mktemp" >&2; exit 1; }
# shellcheck disable=SC2034  # read by publish() from the sourced script
WORKDIR="$STUB_HOME"
# shellcheck disable=SC2034  # read by publish() from the sourced script
GPG_RECIPIENT=TESTKEY
B2_BUCKET=test-bucket

# The bucket check runs inside a pipeline, i.e. a subshell: an in-memory array
# would silently lose that call. Journal to a file instead.
CALLS="$STUB_HOME/calls"
: > "$CALLS"
printf 'plaintext\n' > "$STUB_HOME/plain"

# shellcheck disable=SC2329  # invoked indirectly: these shadow the external commands
rclone() { echo "rclone $*" >> "$CALLS"; [[ "$1" == lsf ]] && printf '%s/\n' "$B2_BUCKET"; return 0; }
# shellcheck disable=SC2329
gpg() {
  echo "gpg $*" >> "$CALLS"
  local out=""
  while [[ $# -gt 0 ]]; do [[ "$1" == --output ]] && out="$2"; shift; done
  printf 'ciphertext\n' > "$out"
}

publish shkoder "$STUB_HOME/plain" "obj.dump.gpg" >/dev/null 2>&1
journal=$(cat "$CALLS")
sequence=$(cut -d' ' -f1-2 < "$CALLS" | tr '\n' ' ' | sed 's/ $//')

assert_eq "rclone lsf gpg --batch rclone copyto rclone delete" "$sequence" \
  "publish verifies the bucket, then encrypts, then uploads, then prunes"

assert_contains     "$journal" "--encrypt"            "publish encrypts to a recipient"
assert_contains     "$journal" "--recipient TESTKEY"  "publish encrypts to the configured recipient"
assert_contains     "$journal" "--trust-model always" "publish does not stall on an untrusted recipient"
# Symmetric mode would force the decryption key onto the host holding the data.
assert_not_contains "$journal" "--symmetric"          "publish never falls back to symmetric mode"
assert_not_contains "$journal" "--passphrase"         "publish never puts a passphrase on the VPS"
assert_contains     "$journal" "lsf b2: --dirs-only"  "publish checks the destination by listing buckets"
assert_not_contains "$journal" "mkdir"                "publish never creates the destination bucket"

# Destination missing: abort with code 4 before encrypting anything.
# shellcheck disable=SC2329
rclone() { echo "rclone $*" >> "$CALLS"; [[ "$1" == lsf ]] && printf 'some-other-bucket/\n'; return 0; }
: > "$CALLS"
( publish shkoder "$STUB_HOME/plain" "obj.dump.gpg" ) >/dev/null 2>&1
assert_eq "4" "$?" "publish exits 4 when the destination bucket is absent"
assert_not_contains "$(cat "$CALLS")" "gpg" "publish does not encrypt when the destination is absent"

unset -f rclone gpg
rm -rf "$STUB_HOME"
unset WORKDIR GPG_RECIPIENT
B2_BUCKET=jekudy-vibe-backups

# --- invariants asserted against the source ---------------------------------
# A cp of a live SQLite file yields a torn copy; only .backup is consistent.
# shellcheck disable=SC2016  # a regex, not an expansion
if grep -qE 'sqlite3 "\$src" "\.backup' "$SCRIPT"; then
  ok "SQLite is snapshotted with .backup"
else
  fail "SQLite snapshot no longer uses sqlite3 .backup"
fi
if grep -nE '\|\|[[:space:]]*(true|:)[[:space:]]*$' "$SCRIPT"; then
  fail "script swallows an error with '|| true' or '|| :'"
else
  ok "script has no '|| true' over error paths"
fi

if [[ "$FAILED" -eq 0 ]]; then echo "ALL CHECKS PASSED"; else echo "CHECKS FAILED" >&2; fi
exit "$FAILED"
