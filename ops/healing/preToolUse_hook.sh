#!/usr/bin/env bash
set -euo pipefail

payload="$(cat)"
lower="$(printf '%s' "$payload" | tr '[:upper:]' '[:lower:]')"

block() {
  printf 'BLOCKED by healing PreToolUse hook: %s\n' "$1" >&2
  exit 1
}

if [[ "$lower" =~ rm[[:space:]]+-[^[:space:]]*r[^[:space:]]*f ]]; then
  if [[ ! "$lower" =~ rm[[:space:]]+-[^[:space:]]*r[^[:space:]]*f[[:space:]]+/tmp(/|[[:space:]\"]|$) ]]; then
    block "rm -rf is only allowed inside /tmp"
  fi
fi

if [[ "$lower" =~ --no-verify ]]; then
  block "--no-verify is forbidden"
fi

if [[ "$lower" =~ --admin ]]; then
  block "--admin is forbidden"
fi

if [[ "$lower" =~ git[[:space:]]+push && "$lower" =~ --force ]]; then
  block "force-push is forbidden"
fi

if [[ "$lower" =~ drop[[:space:]]+(table|database|schema) ]]; then
  block "DROP statements are forbidden"
fi

if [[ "$lower" =~ delete[[:space:]]+from && ! "$lower" =~ where[[:space:]]+id[[:space:]]*= ]]; then
  block "DELETE FROM requires WHERE id ="
fi

if [[ "$lower" =~ bot/web/auth\.py ]]; then
  block "edits to bot/web/auth.py are forbidden"
fi

if [[ "$lower" =~ bot/services/sheets\.py ]]; then
  block "edits to bot/services/sheets.py are forbidden"
fi

if [[ "$lower" =~ (crypto|token|secret) ]]; then
  if [[ "$lower" =~ (file_path|path|command|edit|write|patch|update) ]]; then
    block "edits to crypto, token, or secret paths are forbidden"
  fi
fi

if [[ "$lower" =~ (set|update|patch|delete|rotate)[^[:alnum:]_]+(bot_token|web_password|web_session_secret|db_password) ]]; then
  block "rotation of protected production env vars is forbidden"
fi

if [[ "$lower" =~ hostinger ]]; then
  block "Hostinger API calls are forbidden"
fi

if [[ "$lower" =~ tailscale && "$lower" =~ (up|set|serve|funnel|config|ssh|advertise) ]]; then
  block "Tailscale configuration changes are forbidden"
fi

exit 0
