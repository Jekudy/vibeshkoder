# Task Spec: Vibe Gatekeeper Git/GHCR/Coolify Bootstrap

## Scope

Bootstrap `vibe-gatekeeper` into the new server standard without changing the live production runtime yet.

## Acceptance Criteria

- `AC1`: A private production snapshot exists locally and includes source, DB dump, env files, credentials, and runtime metadata.
- `AC2`: A local repository exists at `~/Vibe/products/vibe-gatekeeper` and excludes production secrets from git.
- `AC3`: A private GitHub repository exists and the code is pushed to it.
- `AC4`: GitHub Actions CI and GHCR release workflows exist in the repository.
- `AC5`: The repository includes docs for local, staging, and production env boundaries.
- `AC6`: Coolify installation constraints are documented before any production cutover.
- `AC7`: Coolify is installed in parallel or the exact blocker is documented with evidence.
- `AC8`: The current production `vibe-gatekeeper` stack remains untouched as the live user path.

## Non-Goals

- No production bot token cutover in this batch.
- No migration of host-level operator services into Coolify.
- No deletion of the current VPS working tree.
