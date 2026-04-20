# GHCR Registry Access — vibe-gatekeeper

Document records the exact GHCR pull mechanism used for this project plus rotation history.

## Chosen mechanism

<filled by Spec B on <date>> — one of:
- host-level `docker login` (see playbook#p1 mechanism 1)
- Coolify Registry Credentials (see playbook#p1 mechanism 2)

## PAT details

- Token name: <filled by Spec B on <date>>
- Scope: `read:packages`
- Creation date: <filled by Spec B on <date>>
- Expiry date: <filled by Spec B on <date>>
- Calendar alerts set: 14 / 7 / 1 days before expiry — <filled by Spec B on <date>>

## Rotation log

| Date | Action | New token ID | Notes |
|------|--------|--------------|-------|
| <filled by Spec B on <date>> | initial | <filled by Spec B on <date>> | <filled by Spec B on <date>> |
