#!/bin/bash
# cleanup-legacy.sh — safe removal of /home/claw/vibe-gatekeeper/ after A3 + soak window
#
# Usage:
#   ./scripts/cleanup-legacy.sh            # safe mode: enforces A3 + soak window + disk check
#   ./scripts/cleanup-legacy.sh --force    # skip soak-window check (A3/disk checks still enforced)
#
# Preflight gates (in order):
#   1. A3 decouple complete — no running Coolify vibe-gatekeeper container may bind-mount /home/claw
#   2. Soak window — today >= SOAK_END (default 2026-04-27), unless --force
#   3. Disk — ≥200M free on VPS / partition
#
# Actions:
#   - Tars /home/claw/vibe-gatekeeper → /root/backups/vibe-gatekeeper-legacy-<ts>.tar.gz on VPS
#   - docker compose down -v in legacy dir (best-effort; legacy stack was stopped 2026-04-20)
#   - rm -rf /home/claw/vibe-gatekeeper
#   - Post-verify: Coolify vibe-gatekeeper containers still up, Telegram getMe ok

set -euo pipefail

SOAK_END="2026-04-27"
TODAY=$(date +%Y-%m-%d)
VPS="foodzy-vps-claw"
LEGACY="/home/claw/vibe-gatekeeper"
COOLIFY_BOT_UUID="maiwn569gziz935wv0w7kcch"
COOLIFY_WEB_UUID="cexv50jspo5gl3kq6ojypw43"

FORCE=0
if [ "${1:-}" = "--force" ]; then
  FORCE=1
fi

echo "=== cleanup-legacy.sh ==="
echo "today=$TODAY  soak_end=$SOAK_END  force=$FORCE"

# --- Preflight 1: A3 decouple verification ---
echo
echo "=== Preflight 1/3: A3 credentials decouple ==="
# A3 is complete when BOTH conditions hold for running Coolify vibe-gatekeeper containers:
#   (a) at least one container mounts /srv/secrets/vibe-gatekeeper/credentials.json
#       (i.e. new mount path is live — not just absent due to Coolify mounts=null quirk)
#   (b) no container still references /home/claw in Mounts OR HostConfig.Binds
#
# The old check (only condition b) passed vacuously when mounts were missing entirely.
VERIFY=$(ssh "$VPS" "
  new_mount_hits=0
  home_claw_hits=0
  containers=0
  for prefix in $COOLIFY_BOT_UUID $COOLIFY_WEB_UUID; do
    for cid in \$(docker ps -q --filter name=\$prefix); do
      containers=\$((containers+1))
      mounts=\$(docker inspect \$cid --format '{{range .Mounts}}{{.Source}}|{{end}}' 2>/dev/null)
      binds=\$(docker inspect \$cid --format '{{.HostConfig.Binds}}' 2>/dev/null)
      combined=\"\$mounts \$binds\"
      if echo \"\$combined\" | grep -q '/srv/secrets/vibe-gatekeeper/credentials.json'; then
        new_mount_hits=\$((new_mount_hits+1))
      fi
      if echo \"\$combined\" | grep -q '/home/claw'; then
        home_claw_hits=\$((home_claw_hits+1))
      fi
    done
  done
  echo \"\$containers \$new_mount_hits \$home_claw_hits\"
")
CONTAINERS=$(echo "$VERIFY" | awk '{print $1}')
NEW_MOUNTS=$(echo "$VERIFY" | awk '{print $2}')
HOME_CLAW=$(echo "$VERIFY" | awk '{print $3}')

if [ "$CONTAINERS" -eq 0 ]; then
  echo "FAIL: no running Coolify vibe-gatekeeper containers found. Cannot verify A3."
  exit 2
fi
if [ "$NEW_MOUNTS" -eq 0 ]; then
  echo "FAIL: 0/$CONTAINERS containers mount /srv/secrets/vibe-gatekeeper/credentials.json."
  echo "A3 credentials decouple is NOT complete (new mount path missing). Abort."
  exit 2
fi
if [ "$HOME_CLAW" -gt 0 ]; then
  echo "FAIL: $HOME_CLAW/$CONTAINERS containers still reference /home/claw."
  echo "A3 credentials decouple is NOT complete (legacy refs remain). Abort."
  exit 2
fi
echo "OK: A3 verified — $NEW_MOUNTS/$CONTAINERS containers mount /srv/secrets, 0 reference /home/claw"

# --- Preflight 2: soak window ---
echo
echo "=== Preflight 2/3: soak window ==="
if [ "$FORCE" -eq 0 ] && [ "$TODAY" \< "$SOAK_END" ]; then
  echo "FAIL: today ($TODAY) is before soak_end ($SOAK_END)."
  echo "Use --force to override (operator accepts risk)."
  exit 3
fi
if [ "$FORCE" -eq 1 ]; then
  echo "WARN: --force set, skipping soak window (today=$TODAY, soak_end=$SOAK_END)"
else
  echo "OK: soak window complete"
fi

# --- Preflight 3: disk space ---
echo
echo "=== Preflight 3/3: disk space ==="
FREE=$(ssh "$VPS" "df -BM / | tail -1 | awk '{print \$4}' | sed 's/M//'")
if [ "$FREE" -lt 200 ]; then
  echo "FAIL: VPS / free space ${FREE}M, need ≥200M."
  exit 4
fi
echo "OK: ${FREE}M free on VPS /"

# --- Size report ---
echo
echo "=== Size of legacy dir ==="
ssh "$VPS" "sudo du -sh $LEGACY"

# --- Backup ---
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="/root/backups/vibe-gatekeeper-legacy-${TS}.tar.gz"
echo
echo "=== Backup → $BACKUP ==="
ssh "$VPS" "sudo mkdir -p /root/backups"
ssh "$VPS" "sudo tar czf $BACKUP -C /home/claw vibe-gatekeeper"
ssh "$VPS" "sudo tar tzf $BACKUP > /dev/null && echo 'Archive integrity OK'"
ssh "$VPS" "sudo ls -lh $BACKUP"

# --- Stop stray legacy containers (best-effort) ---
echo
echo "=== Stop any stray legacy compose containers ==="
ssh "$VPS" "cd $LEGACY && sudo docker compose down -v 2>&1 || echo 'No legacy compose running (expected, stopped on 2026-04-20)'"

# --- Pre-delete health check ---
echo
echo "=== Pre-delete Coolify health check ==="
ssh "$VPS" "docker ps --filter name=$COOLIFY_BOT_UUID --filter name=$COOLIFY_WEB_UUID --format 'table {{.Names}}\t{{.Status}}'"

# --- Delete ---
echo
echo "=== Delete $LEGACY ==="
ssh "$VPS" "sudo rm -rf $LEGACY"
ssh "$VPS" "sudo ls -la /home/claw/vibe-gatekeeper 2>&1 || echo 'legacy dir removed ✓'"

# --- Post-verify ---
echo
echo "=== Post-verify: wait 10s, recheck health ==="
sleep 10
ssh "$VPS" "docker ps --filter name=$COOLIFY_BOT_UUID --filter name=$COOLIFY_WEB_UUID --format 'table {{.Names}}\t{{.Status}}'"

echo
echo "=== Telegram getMe ==="
if [ -f ~/.env.tokens ]; then
  TOKEN=$(grep '^SHKODERBOT_BOT_TOKEN=' ~/.env.tokens | cut -d= -f2- | sed -e "s/^'\(.*\)'\$/\1/" -e 's/^"\(.*\)"\$/\1/')
  if [ -n "$TOKEN" ]; then
    curl -sS "https://api.telegram.org/bot${TOKEN}/getMe" | jq '{ok, username:.result.username}'
  else
    echo "WARN: SHKODERBOT_BOT_TOKEN not found in ~/.env.tokens, skipping getMe"
  fi
else
  echo "WARN: ~/.env.tokens missing, skipping getMe"
fi

echo
echo "Cleanup complete."
echo "Backup on VPS: $BACKUP"
echo "To restore if needed: ssh $VPS 'sudo tar xzf $BACKUP -C /home/claw'"
