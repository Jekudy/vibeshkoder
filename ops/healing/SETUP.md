# Autonomous Healing Setup

Run these steps once from an operator machine with `gh` authenticated for the repository and from the VPS shell where noted.

## 1. Create GitHub token

Create a fine-grained GitHub token for this repository with:

- Repository contents: read and write.
- Pull requests: read and write.
- Issues: read and write.
- Actions: read and write.
- Packages: write.

Store it locally for the next step:

```bash
read -r -s HEALING_GITHUB_TOKEN
export HEALING_GITHUB_TOKEN
```

## 2. Set GitHub Secrets

```bash
REPO="Jekudy/vibeshkoder"

gh secret set HEALING_GITHUB_TOKEN --repo "$REPO" --body "$HEALING_GITHUB_TOKEN"
gh secret set COOLIFY_API_TOKEN --repo "$REPO" --body "$COOLIFY_API_TOKEN"
gh secret set BOT_TOKEN --repo "$REPO" --body "$BOT_TOKEN"
gh secret set DATABASE_URL_RO --repo "$REPO" --body "$DATABASE_URL_RO"
gh secret set HEALING_ENV_KEY --repo "$REPO" --body "$HEALING_ENV_KEY"
```

Generate `HEALING_ENV_KEY` with Fernet-compatible bytes:

```bash
python - <<'PY'
from cryptography.fernet import Fernet

print(Fernet.generate_key().decode("ascii"))
PY
```

## 3. Set GitHub repository variables

```bash
REPO="Jekudy/vibeshkoder"

gh variable set COOLIFY_BASE_URL --repo "$REPO" --body "$COOLIFY_BASE_URL"
gh variable set COOLIFY_APP_UUID --repo "$REPO" --body "$COOLIFY_APP_UUID"
gh variable set HEALING_BOT_CONTAINER --repo "$REPO" --body "$HEALING_BOT_CONTAINER"
```

For shkoderbot prod, the values are:

- `COOLIFY_BASE_URL` = `http://100.101.196.21:8100`
- `COOLIFY_APP_UUID` = `maiwn569gziz935wv0w7kcch`
- `HEALING_BOT_CONTAINER` = the running container name from `docker ps --filter name=maiwn`

## 4. Create runner user on VPS

```bash
sudo useradd -m -G docker runner
sudo mkdir -p /home/runner/actions-runner
sudo chown -R runner:runner /home/runner/actions-runner
```

## 5. Install GitHub Actions runner on VPS

```bash
REPO="Jekudy/vibeshkoder"
RUNNER_VERSION="2.334.0"
RUNNER_TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)"

sudo -u runner bash -lc "
  cd /home/runner/actions-runner
  curl -fsSLO https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
  tar xzf actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
  ./config.sh \
    --url https://github.com/${REPO} \
    --token ${RUNNER_TOKEN} \
    --name shkoder-vps-healing \
    --labels shkoder-vps \
    --unattended \
    --replace
"
```

## 6. Install runner systemd unit

```bash
sudo tee /etc/systemd/system/actions-runner.service >/dev/null <<'UNIT'
[Unit]
Description=GitHub Actions Runner for shkoderbot healing
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=runner
WorkingDirectory=/home/runner/actions-runner
ExecStart=/home/runner/actions-runner/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now actions-runner.service
sudo systemctl status actions-runner.service --no-pager
```

## 7. Authenticate Claude CLI as runner

```bash
sudo -u runner -H bash -lc 'claude login'
sudo -u runner -H bash -lc 'claude -p "echo OK"'
```

## 8. Authenticate Codex CLI as runner

```bash
sudo -u runner -H bash -lc 'codex login'
sudo -u runner -H bash -lc 'codex exec "echo OK"'
```

## 9. Wire Claude PreToolUse hook

After the repo is checked out on the runner, create `/home/runner/.claude/settings.json`:

```bash
sudo -u runner -H mkdir -p /home/runner/.claude
sudo -u runner -H tee /home/runner/.claude/settings.json >/dev/null <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/home/runner/actions-runner/_work/vibeshkoder/vibeshkoder/ops/healing/preToolUse_hook.sh"
          }
        ]
      }
    ]
  }
}
JSON
```

## 10. Create read-only Postgres user

Run in the production Postgres database as an admin role:

```sql
CREATE USER healing_ro WITH PASSWORD :'healing_password';
GRANT CONNECT ON DATABASE vibe_gatekeeper TO healing_ro;
GRANT USAGE ON SCHEMA public TO healing_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO healing_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO healing_ro;
```

Set `DATABASE_URL_RO` to:

```bash
postgresql://healing_ro:${HEALING_RO_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/vibe_gatekeeper
```

## 11. Verify runner labels and auth

```bash
REPO="Jekudy/vibeshkoder"

gh api "repos/$REPO/actions/runners" --jq '.runners[] | select(.labels[].name == "shkoder-vps") | .name'
sudo -u runner -H bash -lc 'claude -p "echo OK"'
sudo -u runner -H bash -lc 'codex exec "echo OK"'
```
