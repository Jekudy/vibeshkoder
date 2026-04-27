# Shkoderbot Threat Model (v1)

**Last updated:** 2026-04-27
**Owner:** founder
**Sprint:** CRIT-07 (Hard Review 2026-04-27)
**Notion issue:** https://www.notion.so/34ff1cc3549b8179810cd4d489930295

---

## TL;DR

Single-admin invite-only Telegram + web bot. **Real attack history** в проекте — operator-induced leaks (3 раза за 4 дня) и process failures, не external attacks. Defense должен быть приоритезирован соответственно: меньше CVE-стен, больше evidence/feedback gates на operator path.

---

## Assets (что защищаем)

| ID | Asset | Sensitivity | Reason |
|----|-------|-------------|--------|
| A1 | Telegram bot tokens (BOT_TOKEN) | High | Compromise = impersonation бота, чтение всех messages, modification community state |
| A2 | Member identities (Telegram user_id, username, first_name) | High | PII, doxxing risk; некоторые мемберы под реальными именами |
| A3 | Intro PII (intro_text — bio, work, location) | High | Пользователи писали в private context, expectation: только members |
| A4 | Admin auth (WEB_PASSWORD, WEB_SESSION_SECRET) | Critical | Compromise = full admin panel access (vouching, member list, ban, kick) |
| A5 | Vouching integrity | High | Бизнес-смысл бота. Если vouch можно обойти — продукт сломан |
| A6 | Coolify API token | Critical | Deploy любых images, чтение env vars, full VPS app control |
| A7 | Hostinger Cloud Firewall token | High | Открытие любых портов, network attack surface manipulation |
| A8 | DB content (applications, vouch_log, intros, chat_messages) | High | Все assets выше в одном месте + audit trail |
| A9 | GHCR PAT | Medium | Pull токен — read access к private images. Не write. |
| A10 | Founder's Mac | Critical (meta-asset) | На нём всё перечисленное выше — single point of compromise |
| A11 | Tailscale account / coordination plane | Critical | Единственный ingress path к VPS после Round 2. Suspension / control plane outage = total ops loss; no break-glass (MID-28) |
| A12 | CI / GitHub repo / release.yml | High | Compromise = malicious image published to GHCR + auto-deployed via Coolify. Trust boundary 7 below |

---

## Actors (кто атакует)

| ID | Actor | Capability | Motivation | Probability (history-based) |
|----|-------|------------|------------|---|
| T1 | External public scanner / bot | Port scan, default creds, known CVE | Opportunistic | High (constant) |
| T2 | Malicious applicant | Submit questionnaire, send chat msgs, social engineering members | Get access to community without vouch | Medium |
| T3 | Compromised member | Has valid invite, can forward, can vouch | Внести non-vetted user; PII leak | Low-medium |
| T4 | Lost / stolen laptop | Physical access to founder's Mac (encrypted at rest) | Random theft → opportunistic credential abuse | Low (physical) |
| T5 | Targeted compromise of founder's Mac | Phishing, malware | Targeted takeover (community is small but has reputation value) | Low-medium |
| **T6** | **Operator-induced leak** (founder OR AI agent committing/saving secret in plaintext) | Internal mistake | Inadvertent disclosure | **High (3 incidents in 4 days history)** |
| **T7** | **Operator process failure** (false DONE, silent regression, unverified fix) | Internal mistake | Bug ships to prod, undetected | **High (23 errors in session 75567d36)** |
| T8 | Memory team agent collision | Parallel cycle on same files | Silent regression of security fix | Medium (3 known collision points) |
| T9 | Supply-chain attacker | Malicious PyPI package, GHCR registry compromise, dependency typosquat | Persistence on VPS via build-time injection | Low (no observed) — partial mitigation via H7 trivy + lock file |
| T10 | Insider / ex-member sabotage | Has community access, can leak intros publicly, can spam | Personal grudge after vouch fight, breakup-style fallout | Low (gипотеза, не наблюдалось) — not yet ranked, monitor if community grows |
| T11 | Tailscale control plane outage / account suspension | External provider failure | n/a (not an attacker, ops resilience) | Low — but blast radius high (no break-glass, see MID-28) |

---

## Trust boundaries

```
[Telegram users] ──┐
                   ▼
              [Bot updates] ─→ Trust boundary 1: input validation,
                                policy detection (#offrecord), html escape
                   │
                   ▼
              [Bot handlers] ─→ Trust boundary 2: auth (vouching, is_member),
                                rate limit, audit log
                   │
                   ▼
              [DB writes]    ─→ Trust boundary 3: transaction integrity,
                                idempotency, race protection
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   [Web admin] ←── Trust boundary 4: cookie auth, hmac, HTTPS (Tailscale)
   [Telegram side
    effects]    ←── Trust boundary 5: outbox pattern, idempotent send

[Operator (founder)] ──→ Trust boundary 6: secret storage, identity split,
                          audit trail на secret writes.
                          Explicit sinks где исторически утекали секреты:
                            - .handoffs/*.md (gitignored, syncs via iCloud)
                            - memory/*.md (gitignored, feeds into AI context)
                            - worklog entries (any file under memory/)
                            - git stage / commit messages (gitleaks pre-commit
                              требуется ДО staging, не только pre-push)
                            - agent stdout / stderr in tasks/*.output
                          ↑↑↑ THIS IS WHERE WE ACTUALLY GET BREACHED

[CI / GitHub Actions /
 release.yml / GHCR] ──→ Trust boundary 7: signed commits required for main,
                          immutable tag pin (sha) для prod, GHCR PAT scope
                          minimal (read:packages only for pull). См. CRIT-06.
```

**Inverted insight:** наша current defense покрывает T1-T5 хорошо (firewall, escape, hmac, etc.). T6-T7 — где история показывает реальные инциденты — практически без protection.

---

## Abuse cases

| ID | Abuse case | Threat actor | Current defense | Gap |
|----|-----------|--------------|-----------------|-----|
| AC1 | Forward invite другу without vouch (CRIT-01) | T2 | None | **OPEN** |
| AC2 | Telegram DM forward → leak member intro to non-member | T1, T2 | C3 (member check) | Partial — exact-text primitive (HIGH-08) |
| AC3 | Default password admin access | T1 | N1 fail-fast | Closed |
| AC4 | Cookie forgery via empty WEB_SESSION_SECRET | T1, T5 | W1 partial | **OPEN** (CRIT-04) |
| AC5 | Auto-reject vouched user (race) | T7 | None | **OPEN** (CRIT-03) |
| AC6 | Ghost invite после rollback | T7 | None | **OPEN** (CRIT-02) |
| AC7 | HTML injection в intro/answers | T2, T3 | N2 | Partial — legacy backfill missing (HIGH-09) |
| AC8 | Auto-grant is_member через chat msg | T1 (joined wrong chat), T3 | H2 | Closed |
| AC9 | Vouch deadline penalizes slow legit applicant | T2/T7 | N3 | Closed |
| AC10 | Public Postgres exposure | T1 | Round 2 N7 | Closed |
| **AC11** | **Plaintext secret в worklog/handoff (`memory/`, `.handoffs/`)** | **T6** | **redaction post-hoc + .gitignore** | **OPEN** — нет pre-commit/pre-write gate. История: 2 раза за 2 дня |
| **AC12** | **False DONE / unverified fix через dual-review ritual** | **T7** | None systematic | **OPEN** — постмортем 75567d36 не починен |
| AC13 | Stolen laptop = total takeover | T4, T5 | FileVault | **PARTIAL** — нет identity split (CRIT-06) |
| AC14 | Tailscale account suspended / control plane outage | T11 | None | **OPEN** — нет break-glass (MID-28). Asset A11 |
| AC15 | Trust boundary 5 bypass (Telegram side effect leaks DB rollback state) | T7 | None | **OPEN** (CRIT-02) |
| AC16 | Memory team T0/T1 silent regression of W1/H2/N2 | T8 | None | **OPEN** (HIGH-16) |
| AC17 | Lock file CVE freeze без regen process | T7 (forgetting) | None | **OPEN** (HIGH-14) |

---

## Re-ranked priorities (post-threat-model)

После анализа **AC11 + AC12 — most damaging based on real history**, не CVE / RCE / etc.

### Re-ranked Top 5 (что реально первым)

1. **CRIT-05 + AC11 mitigation** — ротация утёкших secrets + pre-write gate на `.handoffs/` и `memory/` (gitleaks-pre-commit, file watcher, или просто mandatory placeholder review).

2. **CRIT-06 + AC13 mitigation** — operator identity split. Без этого AC13 — single-step kill chain.

3. **CRIT-01 + AC1 mitigation** — bypass vouching. Бизнес-смысл бота сломан.

4. **CRIT-02, CRIT-03 + AC6, AC5** — transaction correctness в vouch flow.

5. **CRIT-04 + AC4** — empty secret validator.

### Re-ranked что окажется LESS urgent после threat model

Defer обоснование везде: **no observed incident в этом проекте** + **lower immediate risk** чем AC11/AC12 (доминирующие). НЕ "T1-only" — T6/T7 имеют higher observed rate, поэтому идут первыми.

- HIGH-15 (CI gates response runbook) — оборона процесса для T1/T9; реактивная процедура, без неё gates через 2 недели окажутся silenced. Defer пока T6/T7 не закрыты.
- HIGH-14 (dependabot) — closure для T9 (supply chain). Real but без observed incident. Сначала feedback loop (HIGH-12), потом dependabot.
- MID-26 (Dockerfile hash pinning) — T9 mitigation, defer (no observed).
- MID-31 (semgrep custom rules) — T1/T2 hardening, defer (default rules дают baseline).

---

## Operator path defense (где история показывает gap)

Минимальный набор для T6/T7:

1. **Pre-write gate на secrets** — gitleaks pre-commit hook + scan `.handoffs/`, `memory/` локально (даже если gitignored). Закрывает AC11.
2. **Evidence requirement per finding** — нельзя dispatch reviewer без raw command output в done-criterion. Закрывает AC12.
3. **Falsification step** — после approve, попытка найти контр-пример (1 test asserting opposite behaviour). Закрывает part of AC12.
4. **Single source of truth для rotations** — calendar + watchdog (HIGH-13). Закрывает T7-forgetting subset.

---

## Limitations of this model

- **Single-admin assumption baked in.** Когда придёт второй admin, threat model заново.
- **Supply chain coverage shallow.** T9 + A12 + TB7 модель упоминают, но конкретные техники (pip cache poisoning, GHCR registry compromise via stolen PAT, dependency typosquat) детально не разрабатываются. Mitigation в backlog (HIGH-14 dependabot, MID-26 hash pin, H7 trivy) — без observed incident, defer per re-ranking rule.
- **No nation-state actor.** Если сообщество станет интересно адресной угрозе — модель не покрывает.
- **No physical attack on VPS** (datacenter access). Trust assumption: Hostinger как provider.

---

## Review cadence

Re-evaluate quarterly OR при:
- Crossing 50 community members.
- Adding second admin.
- Any new asset (new external integration, new public surface).
- Any new T-class actor demonstration (e.g., если случится realT5 incident).
