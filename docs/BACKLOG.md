# Shkoderbot Memory Editor — Backlog

Deferred items from 4-agent review 2026-04-22. Not in F0 scope, revisit quarterly.

## Future Features (F1-F2)

### Content ingestion expansion
- Voice messages extraction via Whisper API
- OCR on screenshots containing text (GPT-4V or equivalent)
- Forwarded messages — preserve origin context in finding metadata
- Link preview auto-extraction (title + description → finding candidate)

### Search improvements
- Full-text search across mixed ru+en, per-token language detection (current `ru_en` config is a single stemmer compromise — see §11.1)
- Semantic clustering of findings (topic emergence without pre-declared slugs)
- "Related findings" sidebar on /wiki/topic/{slug}
- Saved searches and alert-on-new-match for admins

### Community features
- Hashtag conventions: `#rule`, `#faq`, `#decision` — auto-promote tagged messages to high-confidence findings
- Feedback UI: `/report_finding` command, writes to admin review queue
- Self-subscribe digest DM copy (weekly digest as DM for members who opt in)
- Member-facing `/my-stats` (mention count across topics, top contributions)
- Topic-specific weekly digests (`/wiki/digest/topic/{slug}`)

### Governance
- Multi-admin policy: minimum 2 admins for bus-factor, documented rotation
- Super-admin role: grant/revoke other admins via bot flow
- Admin 2FA for destructive operations (forget-user, bulk delete, feature-flag flip)
- Community self-service: vote on topic merges, tag proposals

## Compliance (when audience expands beyond closed community)

- Full GDPR compliance: DPA with LLM provider, documented DPO
- 152-ФЗ: data locality assessment for RU residents, localization decision
- OpenAI ZDR endpoint (or equivalent zero-data-retention tier on whichever provider is primary)
- Backfill consent flow for historical messages (§10.8 covers going forward only)

## Infrastructure maturity

- Self-hosted Prometheus + Grafana (replace any free-tier external service)
- Loki logs aggregation (replace plain structlog-to-stdout)
- Distributed tracing via OpenTelemetry, trace_id propagation through LiteLLM
- Chaos testing suite (`test_chaos_llm_50pct_fail`, `test_chaos_db_partial_outage`, etc.)
- Per-chat partitioning of `chat_messages` (multi-chat scale, §2.1 trailer)
- pgvector IVFFlat migration at 100k+ findings (HNSW → IVFFlat crossover)
- Partition `llm_usage_ledger` monthly (ledger growth is linear with usage)
- PITR restore test weekly automation (currently manual)

## Security hardening (post-MVP)

- Admin 2FA for bulk destructive actions
- LLM-guard layer: second cheap LLM for pre-extraction classification (replaces §11.5 keyword list)
- Object Lock on backup bucket (immutable backups against ransomware)
- External secret rotation playbook + quarterly drill
- SBOM generation in CI
- Supply chain: pip-audit + socket.dev in CI blocking merges on critical advisories

## DevX improvements

- Full Sentry + OpenTelemetry trace correlation
- Integration tests against live TG staging bot
- Golden-output snapshot tests for LLM extraction (cross-provider diff)
- Terraform / Ansible for Coolify host bootstrap (currently manual)
- `scripts/staging-refresh.sh` with PII scrub (pull prod schema, anonymize, push to staging)

## Data lifecycle (v2)

- Findings cold-storage archive > 2 years (parquet on B2 / S3 Glacier)
- `admin_audit_log` retention policy (1y operational, 7y for compliance if and when needed)
- `extraction_queue` done/failed purge (30d)
- `extraction_log` retention (1y, then aggregate-and-drop)

## Nice-to-have UX

- Accessibility audit (WCAG 2.2 AA) for /wiki and sqladmin
- Colorblind-friendly admin UI severity colors
- Identity-leak detection: rule-based inference (company + stack + location combinations uniquely identifying a person)
- Pinned message auto-update on every rollout phase (currently manual in §10.5)
- Community ritual framing in digest copy (less "here are findings", more "this week we learned")

---

Priority: review this backlog after Phase 4 (search go-live). Re-sort by actual need based on production signal.
