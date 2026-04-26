# GHCR Registry Access

Last verified: 2026-04-26.

This project deploys pre-built private GHCR images into Coolify. The active pull
mechanism is host-level Docker registry authentication on the VPS, not a Coolify
Registry Credential resource.

## Images and Tags

Release workflow: `.github/workflows/release.yml`.

Published images:

- `ghcr.io/jekudy/vibe-gatekeeper-bot`
- `ghcr.io/jekudy/vibe-gatekeeper-web`

Each successful release from `main` pushes two tags per image:

- `:sha-<git-commit-sha>` - immutable build tag from the CI head SHA.
- `:main` - mutable production tracking tag used by current Coolify resources.

Current Coolify prod resources in `docs/runbook.md` reference `:main`. Rollback
should prefer a previous deployment digest or a known-good `:sha-<git-commit-sha>`
tag instead of guessing from `:main`.

## Pull Mechanism

Current runtime path:

1. The VPS root user is logged in to GHCR with a GitHub PAT:
   `docker login ghcr.io -u Jekudy`.
2. Docker stores the credential material in `/root/.docker/config.json`.
3. Coolify reuses the host Docker daemon, so Coolify can pull the private GHCR
   images without a separate Coolify Registry Credential resource.

Do not add a second Coolify Registry Credential unless the project intentionally
moves away from host-level auth. If that migration happens, update this document
and `docs/ops/ghcr-pat-rotation.md` in the same change.

## PAT Storage

Active deployment pull auth:

- VPS: `/root/.docker/config.json` after root-level `docker login`.
- Coolify Registry Credentials: not used for this project as of 2026-04-26.

Operator-side recovery copy:

- Local: `~/.env.tokens`, entry `GHCR_PAT=<your-PAT>`.
- Current cleanup note: no local `GHCR_PAT` entry was found on 2026-04-26; add
  it during the next rotation so the deployment pull credential is recoverable.

GitHub Actions:

- `.github/workflows/release.yml` pushes with `secrets.GITHUB_TOKEN` and
  workflow permission `packages: write`.
- No repository secret named `GHCR_PAT` is referenced by the current workflows.
- Runtime pull PAT only needs `read:packages`; it must not be used for release
  pushes unless a future workflow explicitly documents that requirement.

## Rotation

Rotation policy and steps live in `docs/ops/ghcr-pat-rotation.md`.
