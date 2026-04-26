# GHCR PAT Rotation Policy

Last verified: 2026-04-26.

This policy covers the GitHub PAT used by the VPS Docker daemon to pull private
GHCR images for Coolify-managed `vibe-gatekeeper` services.

## Scope

Use a GitHub PAT with `read:packages` for runtime pulls.

Do not grant `write:packages` to the runtime pull PAT. The release workflow pushes
images with `secrets.GITHUB_TOKEN` and workflow permission `packages: write`, so a
separate write-capable PAT is not required by the current repo.

## Expiry

Chosen expiry: 90 days.

Set reminders before expiry:

- 14 days before expiry.
- 7 days before expiry.
- 1 day before expiry.

## Storage Locations

Current active locations:

- VPS root Docker auth: `/root/.docker/config.json`.
- Local operator backup: `~/.env.tokens`, entry `GHCR_PAT=<your-PAT>`.

Current non-locations:

- Coolify Registry Credentials are not used for this project as of 2026-04-26.
- GitHub Actions secret `GHCR_PAT` is not referenced by `.github/workflows/*.yml`.

If either non-location becomes active later, update this policy in the same PR
that introduces it.

## Rotation Procedure

1. Generate a new GitHub PAT in GitHub Settings -> Developer settings ->
   Personal access tokens with `read:packages` scope and 90-day expiry.
2. Update the local recovery copy in `~/.env.tokens`:
   `GHCR_PAT=<your-PAT>`.
3. Update the VPS host-level Docker login:

   ```bash
   source ~/.env.tokens
   test -n "${GHCR_PAT:?GHCR_PAT missing}"
   printf '%s\n' "$GHCR_PAT" \
     | ssh claw@187.77.98.73 'sudo -n docker login ghcr.io -u Jekudy --password-stdin'
   ```

   Do not paste the PAT value into shell history.
4. Coolify Registry Credentials are not active for this project. If a registry
   credential is added later, update the GHCR credential in the Coolify UI or API
   before redeploying.
5. If a future workflow references `secrets.GHCR_PAT`, update the GitHub Actions
   repository secret before triggering a release.
6. Trigger one Coolify deploy for bot and web, or manually verify both pulls:

   ```bash
   docker pull ghcr.io/jekudy/vibe-gatekeeper-bot:main
   docker pull ghcr.io/jekudy/vibe-gatekeeper-web:main
   ```

7. Confirm the new deploy starts cleanly and logs do not show `denied` or
   `unauthorized` on GHCR pull.
8. Revoke the old PAT in GitHub Settings.
9. Append an audit entry to `memory/worklog.md` with date, operator, token name,
   expiry date, verification command, and result. Do not record the token value.

## Calendar Reminder

Create a recurring reminder at day 75 of each 90-day token window. Example:
`todoist quick add "Rotate vibe-gatekeeper GHCR PAT" due "every 75 days"`.

## Audit Trail Template

Append this to `memory/worklog.md` after each rotation:

```markdown
## YYYY-MM-DD - GHCR PAT rotation

- Operator: <name>
- Token name: <token-name>
- Scope: `read:packages`
- New expiry: YYYY-MM-DD
- Updated locations: `/root/.docker/config.json`, `~/.env.tokens`
- Verification: bot and web `docker pull` checks succeeded
- Old token revoked: yes
```
