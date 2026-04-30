# Autonomous Healing System for shkoderbot — Design

**Status:** draft, awaiting review
**Date:** 2026-04-30
**Author:** brainstormed with eekudryavtsev (jekudy)
**Trigger:** 3-day outage 2026-04-26 → 2026-04-29 caused by stricter config validators in commit `ade13b3` deployed without env update; crash loop went undetected because `health_check_enabled=false` on the Coolify app and there was no liveness probe outside the box.

## 1. Goal

Keep shkoderbot prod constantly working through (a) frequent composite healthcheck and (b) autonomous remediation by a Claude CLI session running on a self-hosted GitHub Actions runner on the VPS. Optimise for: outage detection within one cadence cycle, fix attempt without waking the operator, hard guardrails so the autonomous loop cannot make production worse than it found it.

## 2. Non-goals

- Replacing operator judgement on architectural decisions, schema migrations, or security-sensitive code.
- Replacing Coolify/CI/CD. Healing system *uses* both, doesn't replace them.
- Multi-tenant or generic "self-healing platform". This is shkoderbot-specific; transferable lessons may emerge but are not in scope.
- Solving alert fatigue across other Vibe projects. Single project, single bot.

## 3. Architecture

```
GH Actions cron (ubuntu-latest, every 3h)
  └─ healthcheck.yml
       ├─ run 4 composite checks in parallel
       ├─ append result to healing-state branch healthcheck-log.jsonl
       └─ if any check red →
              workflow_dispatch healing.yml with signal payload
                                    ↓
       GH Actions self-hosted runner on VPS (label: shkoder-vps, user: runner)
         └─ healing.yml (concurrency: healing-singleton)
              ├─ pre-flight: check .healing/disabled flag → abort if present
              ├─ acquire lock (commit .healing/in-progress to healing-state branch)
              ├─ snapshot: SHA + env hash (sha256 of sorted keys+values) + restart_count → snapshot.json
              ├─ collect context bundle (signal + git log -50 + container logs 500 lines + Coolify state)
              ├─ Claude session via claude CLI (auth-based, not API key)
              ├─ if Claude opens PR → codex review gate → APPROVE required
              ├─ if PR approved → gh pr merge --rebase → wait for deploy
              ├─ post-fix watch: 10 min × healthcheck poll
              ├─ green → audit GH issue "succeeded" (closed) + TG notify (info)
              ├─ red after watch → auto-rollback to snapshot (SHA + env restore) → retry++
              └─ after retry == 3 → escalate (TG sendMessage + open ALERT issue) + drop .healing/disabled
```

## 4. Healthcheck

Implemented as `ops/healing/healthcheck.py`, run by `.github/workflows/healthcheck.yml` cron every 3 hours.

Four signals, parallel:

| Signal | What | Red condition |
|---|---|---|
| `coolify_status` | `GET /api/v1/applications/{uuid}` | status == `exited:*`, OR restart_count Δ > 2 vs previous run |
| `telegram_pending` | `getWebhookInfo` | `pending_update_count` > 50 AND growing vs previous run |
| `db_roundtrip` | `psycopg.connect(DATABASE_URL_RO).execute("SELECT 1")` | timeout >5s OR exception |
| `e2e_ping` (deferred) | Service account sends `/healthcheck@vibeshkoder_bot`, awaits reply 30s | no reply / wrong reply |

`e2e_ping` is implemented later — requires a `bot/handlers/healthcheck.py` handler that responds `OK <utc_iso>` to admin_ids only, plus a service-account TG client. Initial cut runs only the first three.

History: every run appends a JSON line to `healthcheck-log.jsonl` on orphan branch `healing-state`. Trend analysis (e.g., "restart_count keeps creeping") is enabled by reading recent lines.

If any signal is red, workflow does:
```
gh workflow run healing.yml -f signal_payload="$JSON"
```

## 5. Healing session

Implemented as `.github/workflows/healing.yml`, runs on self-hosted runner with label `shkoder-vps`.

### 5.1 Concurrency & lock

`concurrency: { group: healing-singleton, cancel-in-progress: false }`. Plus file-based lock `.healing/in-progress` committed to `healing-state` branch — guards against concurrent healing if the GH concurrency primitive ever drifts.

### 5.2 Snapshot

Before any change, write `snapshot.json`:
```json
{
  "ts": "2026-04-30T...",
  "prod_image_sha": "sha-caebb519",
  "env_hash": "sha256:...",
  "env_dump_encrypted": "<encrypted with healing-state key>",
  "restart_count": 12,
  "trigger_signal": {...}
}
```

`env_dump_encrypted` enables full env restore on rollback. Encryption key lives in GH Secrets (`HEALING_ENV_KEY`), decryptable only inside the runner.

### 5.3 Context bundle

`context-bundle.md` assembled by a script before invoking Claude:

- `## Signal` — full failure JSON from healthcheck
- `## Healthcheck history (24h)` — last 8 entries from `healthcheck-log.jsonl`
- `## Recent commits` — `git log --oneline -50`
- `## Last 5 commit diffs (stat only)` — quick scan for size
- `## Coolify state` — apps + statuses + restart_count + last_online_at + env keys (no values)
- `## Container logs` — last 500 lines via `docker logs --tail 500 <bot-container>`
- `## Last 3 deployments` — Coolify deployments API output
- `## Snapshot reference` — pointer to snapshot.json for rollback

### 5.4 Claude invocation

```bash
claude -p \
  --model claude-opus-4-7 \
  --append-system-prompt "$(cat ops/healing/INVARIANTS.md)" \
  --max-turns 50 \
  < context-bundle.md > session.log
```

Auth comes from `/home/runner/.claude/` (one-time `claude login` during VPS setup). No `ANTHROPIC_API_KEY` env var.

Claude has access to:
- Bash with PreToolUse hook (`ops/healing/preToolUse-hook.sh`) blocking forbidden commands
- `gh` CLI (token in env)
- `curl` (Coolify API token in env)
- `docker` via runner user's docker group membership: read-only verbs only (`ps`, `logs`, `inspect`). `exec`/`run`/`rm`/`kill` blocked by hook.
- `psql` against read-only DB user

Auto-loaded skills (set in runner's `~/.claude/CLAUDE.md`):
- `superpowers:systematic-debugging` — mandatory for diagnosis
- `superpowers:test-driven-development` — required when writing code fix
- `superpowers:writing-plans` — when fix needs >1 step

### 5.5 Codex review gate

If Claude opens a PR, before merge:
```bash
codex exec review --base main -m gpt-5.5 -c model_reasoning_effort=high --ephemeral
```
Auth via `codex login`. If verdict ≠ `APPROVE`, healing reads codex feedback, returns control to Claude for attempt #2 (counts toward retry budget).

Rationale: dual-model review per superflow-enforcement rule 6. Single autonomous reviewer can convince itself. Codex is independent and free under subscription.

### 5.6 Post-fix watch

After merge + Coolify deploy completes:
- 10 minutes, healthcheck every 2 minutes (5 polls)
- All 5 must be green for "fix succeeded" verdict
- Any red → trigger rollback

### 5.7 Rollback

If watch fails:
- `gh api PATCH /api/v1/applications/{uuid} -f docker_registry_image_tag=sha-<snapshot.prod_image_sha>` (assumes `:sha-<commit>` pinning is set up — see N5 handoff; if not, use rollback to previous Coolify deployment)
- Restore env: decrypt `snapshot.env_dump_encrypted`, PATCH each env back to snapshot value
- The PR Claude opened: `gh pr close <num> --comment "auto-reverted; root cause not resolved"` and revert commit pushed

After rollback, retry counter increments. If retries < 3, return to context-bundle step (Claude gets new bundle including the failed attempt summary).

## 6. Invariants

Stored in `ops/healing/INVARIANTS.md`, injected as Claude system prompt suffix and enforced by `preToolUse-hook.sh`.

### Hard NEVER (hook-enforced where possible)

1. No direct push to `main`. PR-only.
2. No `--admin`, `--no-verify`, `--force` flags on git/gh.
3. No alembic migrations autonomously.
4. No `DROP`, `DELETE FROM` without `WHERE id = …`, no `rm -rf` outside `/tmp`.
5. No edits to security-sensitive paths: `bot/web/auth.py`, `bot/services/sheets.py`, anything matching `*crypto*` / `*token*` / `*secret*` (case-insensitive).
6. No rotation of `BOT_TOKEN`, `WEB_PASSWORD`, `WEB_SESSION_SECRET`, `DB_PASSWORD`.
7. No Hostinger API calls (no VPS reboot/destroy).
8. No Coolify network/firewall/Tailscale config changes.

### Hard MUST

9. Snapshot before any change.
10. PR diff ≤ 300 lines. Bigger → escalate.
11. PR includes red→green test reproducing the bug.
12. 10-min watch after deploy. Red → auto-rollback.
13. Max 3 retries per incident.
14. 15-min cooldown between retries.
15. Same root cause appearing twice → escalate immediately (no retry #3).
16. 30-min wall-clock budget per incident.

### Soft (documented exception in PR description allowed)

17. Trunk-based, one small PR.
18. CHANGELOG updated.

Hook implementation: `preToolUse-hook.sh` reads tool input JSON, regex-matches against forbidden patterns, exits 1 with explanation if matched. Wired in `~/.claude/settings.json` on runner.

## 7. Escalation & audit

Escalation triggers: retries == 3, or hard-fail invariant tripped (e.g., diff > 300 lines), or wall-clock exceeded.

**Channel 1 — Telegram:**
```python
requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    json={
        "chat_id": 149820031,  # jekudy
        "text": f"⚠️ healing escalation\nincident #{N}\nattempts: {retries}\nverdict: {verdict}\nlast_error: {last_err}\nissue: {issue_url}",
    },
)
```
Works as long as `BOT_TOKEN` is valid and Telegram API is reachable. Independent of bot's polling state.

**Channel 2 — GitHub Issue (always):**
- Title: `[ALERT] healing escalation #N — <signal_summary>`
- Body: full session.log, snapshot.json, all retry transcripts, codex review verdicts.
- Labels: `healing`, `incident`, `priority:high`.
- Opens regardless of TG outcome — always-on audit trail.

After escalation, runner writes `.healing/disabled` to `healing-state` branch. Subsequent healthcheck failures will skip healing and only escalate (open issue), preventing endless retries while operator investigates. Operator clears the flag manually after fix.

**Audit log per session (success or failure):**
- GH issue `[healing] run #N — <verdict>` always created.
- Success → closed automatically with summary.
- Escalation → kept open, requires user ack.

## 8. State storage

Orphan git branch `healing-state` holds all runtime state:
- `healthcheck-log.jsonl` — append-only history.
- `snapshots/<incident-id>.json` — per-incident snapshot.
- `.healing/in-progress` — concurrency lock.
- `.healing/disabled` — manual circuit breaker.
- `.healing/last-restart-count.json` — for trend comparison.

Why orphan branch and not S3 / dedicated DB: zero extra infra, free, naturally version-controlled, GH-Actions-friendly. Tradeoff: not suitable for high-frequency writes — fine here (cadence is 3h, healing sessions are rare).

## 9. Components inventory

| Path | Purpose |
|---|---|
| `.github/workflows/healthcheck.yml` | 3h cron, 4 composite checks, dispatch healing on red |
| `.github/workflows/healing.yml` | Self-hosted runner job, full session lifecycle |
| `ops/healing/healthcheck.py` | Composite check implementation |
| `ops/healing/context-bundle.py` | Assembles context-bundle.md |
| `ops/healing/snapshot.py` | Snapshot create/restore (with env encryption) |
| `ops/healing/preToolUse-hook.sh` | Claude tool-use guard |
| `ops/healing/INVARIANTS.md` | System prompt for Claude session |
| `ops/healing/SETUP.md` | One-time VPS runner setup runbook |
| `bot/handlers/healthcheck.py` (later) | E2E ping responder for admin_ids |

## 10. One-time setup (operator)

1. **GH PAT**: create with `repo, workflow, packages:write` → `gh secret set HEALING_GITHUB_TOKEN`.
2. **GH Secrets**: `COOLIFY_API_TOKEN`, `BOT_TOKEN`, `DATABASE_URL_RO` (separate read-only PG user — needs new role created in PG), `HEALING_ENV_KEY` (32-byte random for env encryption).
3. **Service account TG ID**: hardcoded `149820031` in workflow (can move to secret if rotation needed).
4. **VPS runner setup** (per `ops/healing/SETUP.md`):
   - `useradd -m -G docker runner`
   - download `actions-runner-linux-x64`, configure with label `shkoder-vps`
   - enable systemd `actions-runner.service` under `runner` user
   - `sudo -u runner claude login` (interactive, browser auth)
   - `sudo -u runner codex login` (interactive, ChatGPT auth)
   - drop `INVARIANTS.md` symlink into `/home/runner/.claude/agents/system-suffix.md`
5. **Read-only DB user**: `CREATE USER healing_ro WITH PASSWORD '...'; GRANT CONNECT ON DATABASE vibe_gatekeeper TO healing_ro; GRANT USAGE ON SCHEMA public TO healing_ro;` — write `DATABASE_URL_RO` from this.
6. **Coolify cleanup**: enable health_check on bot app (`/health` endpoint or TCP 3000) — independent of healing system but related; would have caught the 2026-04-26 outage faster on its own.

## 11. Cost & risks

**Cost:** Claude/Codex via subscription auth (no per-token billing). Marginal cost is GH Actions minutes — ubuntu-latest cron `0 */3 * * *` = ~8 runs/day ≈ 4 min/day; self-hosted runner cron usage doesn't count toward GH minutes; healing sessions rare (~1/month). Effectively free.

**Risks:**
- *Bad fix passes codex review.* Mitigation: 10-min watch + auto-rollback. Fail-safe.
- *Healing keeps trying despite escalation.* Mitigation: `.healing/disabled` flag + retry counter persistence in `healing-state` branch.
- *Auth expiry on VPS.* Claude/Codex CLI tokens may expire; healing job must detect auth failure (`claude -p` exits non-zero) and escalate without retry. Add watchdog test in healthcheck (call `claude -p "echo OK"` on runner, alert if fails).
- *Compromised runner.* Has BOT_TOKEN, COOLIFY_API_TOKEN, GH PAT in env per-run. Mitigation: secrets scoped to healing job only, not shared with other workflows; runner user is non-root, no Tailscale config access.
- *Telegram message lands in deleted chat.* Mitigation: secondary GH Issue channel always opens.
- *Codex review hallucination.* See `feedback-codex-hallucinated-citations.md` — codex verdicts directionally correct but file:line refs may be wrong. Healing reads only verdict and high-level feedback, doesn't blindly apply codex suggestions.

## 12. Testing strategy

- **Unit tests**: `ops/healing/healthcheck.py`, `snapshot.py`, `context-bundle.py` — pure Python, mockable.
- **Integration test**: end-to-end dry-run mode (`HEALING_DRY_RUN=true`) where Claude session is replaced by a stub that returns a canned PR. Verifies workflow plumbing, codex gate, watch, rollback paths.
- **Chaos test (manual)**: introduce known-broken commit on a feature branch deployed to a test Coolify app; trigger healing manually; verify it diagnoses correctly and either fixes or escalates appropriately.
- **Invariant tests**: `preToolUse-hook.sh` has its own test suite — feed forbidden commands, expect exit 1.

## 13. Open questions / deferred

- E2E `/healthcheck` handler: deferred to separate ticket. Initial cut uses 3 signals.
- E2E TG service account: how to authenticate without exposing real session string. Options: separate burner bot (`@shkoder_healthcheck_bot`) sending to `@vibeshkoder_bot`; or MTProto session for a dedicated test account. Decision deferred until E2E gets prioritised.
- Coolify deployment SHA pinning (`:sha-<commit>` tag): documented in handoff `2026-04-26_20-05_n5-sha-pin.md`, partially in flight. Healing rollback assumes this is in place — if not, falls back to "redeploy previous successful Coolify deployment" path.
- Multi-region or HA: out of scope; single-VPS architecture by design for this product.

## 14. Decision log (this brainstorm)

| Question | Choice | Why |
|---|---|---|
| Autonomy level | Full incl. code (C) | User explicitly chose; bounded by invariants & dual review |
| Where Claude runs | GH self-hosted runner on VPS (B) | Centralised secrets, PR flow, ops access via localhost |
| Healthcheck signals | Composite, 4-signal (D) | Single signal can't catch all failure modes; today's outage needed `restart_count` trend |
| Codex review gate | Mandatory before merge (A) | Independent eyes prevent echo chamber |
| Auth | CLI auth, not API key | User direction; subscription cost only |
| Cadence | Every 3 hours | User direction |
| Escalation | TG via shkoderbot to jekudy + GH Issue fallback | TG primary, Issue always-on audit |
