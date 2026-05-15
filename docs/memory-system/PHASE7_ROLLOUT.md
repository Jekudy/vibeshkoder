# Phase 7 — Daily Digest: Operator Rollout Checklist

**Status:** Phase 7 CLOSED 2026-05-15. Feature flag default OFF — production rollout below.

This document is the operator playbook for enabling the Phase 7 daily-digest
feature on production. Phase 7 is shipped DARK (flag default OFF). Follow
this checklist in order; do NOT skip steps.

## Pre-rollout invariants

Before flipping any flag, confirm:

- [ ] Phase 6 (knowledge cards + admin review) is CLOSED and stable.
  Cards must be actively populating; the digest fallback path uses
  approved cards as the cards-first source.
- [ ] Phase 11 binding suite is GREEN on `main` HEAD. Run:
  `pytest tests/evals/ -v` — should show 34/34 passing
  (L1-L5 + L6a/b/c + L7a/b + C1-C4 + C5a-d + C6 + R1-R4 + I1-I4 + I5a/b/c).
- [ ] Database migration 037 is APPLIED in the target environment.
  Verify: `alembic current` returns `037` (or later).
- [ ] Operator has access to the bot's admin DM (their telegram_id is in
  `ADMIN_IDS`).

## Required environment variables

| Var | Default | Description |
|---|---|---|
| `DIGEST_SOURCE_CHAT_ID` | `0` (UNCONFIGURED) | Chat the digest reads FROM. MUST be set before enabling. |
| `DIGEST_DESTINATION_CHAT_ID` | unset | Chat the digest posts TO. If unset, digests stay as drafts (`status='skipped_no_destination'`). Set before enabling auto-post. **Must differ from `DIGEST_SOURCE_CHAT_ID`.** |
| `DIGEST_HOUR_MSK` | `9` | Cron trigger hour, Europe/Moscow. Daily fire. |
| `DIGEST_DAILY_USD_CEILING` | `1.00` | Daily Phase-7 cost ceiling (USD, decimal). Independent of Phase 5 shared ceiling. |
| `DIGEST_MONTHLY_USD_CEILING` | `10.00` | Monthly Phase-7 cost ceiling. |
| `DIGEST_MIN_CARDS_THRESHOLD` | `3` | If approved cards in window < this, raw messages added as fallback. |
| `DIGEST_RAW_MESSAGE_TOP_N` | `15` | Cap on raw-message fallback count. |
| `DIGEST_TOKEN_BUDGET_INPUT` | `8000` | LLM context size cap. |

Plus the shared Phase 5 LLM env vars (`LLM_PROVIDER`, `LLM_MODEL`,
`LLM_DAILY_USD_CEILING`, etc.) — already configured if Phase 5 is live.

## Rollout sequence

1. **Set env vars in production.** `DIGEST_SOURCE_CHAT_ID` and (optionally)
   `DIGEST_DESTINATION_CHAT_ID`. Verify both with a bot restart and log scan
   for `digest_daily_job: ...` traces.

2. **Dry-run via admin.** With flag still OFF, run `/digest_now daily` from
   admin DM. The handler bypasses the flag.
   - Expected: `✅ Posted. digest_id=X` if destination is set, or `⚠️ Draft
     создан ... DIGEST_DESTINATION_CHAT_ID не настроен` if not.
   - Inspect the posted message manually. Verify content quality,
     formatting, and absence of forbidden content.

3. **Run `/digest_preview daily`** to see the citation audit. Verify
   citations point to expected message_version_id / card_source_id values.

4. **Flip the feature flag.** In a DB session:
   ```sql
   INSERT INTO feature_flags (flag_key, scope_type, scope_id, enabled)
   VALUES ('memory.digests.daily.enabled', NULL, NULL, TRUE)
   ON CONFLICT (flag_key, scope_type, scope_id)
   DO UPDATE SET enabled = TRUE;
   ```
   Or via admin tool if available.

5. **Monitor first 3 cron fires.** Default cron is daily at 09:00 MSK.
   - Check `/digest_history` after each fire.
   - Inspect `digest_runs` for errors:
     `SELECT * FROM digest_runs ORDER BY id DESC LIMIT 5;`

## Operator alerts (admin DM notifications)

The digest pipeline DMs the first admin in `ADMIN_IDS` on these conditions:

- `status='cost_exceeded'` — daily or monthly USD ceiling tripped.
  Investigate: raise ceiling or check for prompt inefficiency.
- `status='failed'` with `error_text` containing `TelegramBadRequest`
  text — markdown render error or destination misconfig.
- `status='redacted_edit_failed'` — forget cascade ran but
  `bot.edit_message_text` rejected. Either the message is too old or the
  bot was kicked. Check `error_text` for the specific case.
- `error_text='bot_kicked_from_posted_chat_id'` — **PRIVACY GAP**: the bot
  was kicked from the destination chat AFTER posting a digest, then a
  forget event fired. The old digest is still visible with forgotten
  content. Escalation: re-add the bot to the chat (manual edit then becomes
  possible), or ask a chat admin to delete the original Telegram message
  via `/digest_history`-provided link.
- `error_text='publish_lock_timeout'` — 3-retry NOWAIT exhaustion. Likely
  another worker is holding the row. Investigate concurrent
  `/digest_now` invocations.
- `error_text='posted_transition_rowcount_zero_after_send'` — Telegram
  message was sent but the DB transition rejected. Manual reconciliation
  required.

## Disable rollback

To turn the feature OFF:

```sql
UPDATE feature_flags
SET enabled = FALSE
WHERE flag_key = 'memory.digests.daily.enabled';
```

The next cron fire becomes a no-op. The stale-posting reaper continues
to run (not flag-gated) — that's intentional, so any in-flight `posting`
rows from a crashed publisher still get cleaned up after a disable.

## Schema summary

- `digests`: 1 row per `(type, window_start, window_end)`. Idempotency
  unique. Citations stored as JSONB `[{kind, id, position}]`.
- `digest_runs`: append-only audit log of every `run_digest()` invocation.

## Citation kinds

- `kind='message_version'`, `id` = `message_versions.id` (bigint).
- `kind='card_source'`, `id` = `card_sources.id` (UUID as string).
- Tokens written by the prompt: `[[mv:INT]]` and `[[cs:UUID]]`. Malformed
  `[[card:UUID]]` tokens are dropped and logged.

## Phase 7.5 carryovers (post-closure)

Tracked as GitHub issues for future cleanup:
- **#291** — extract shared `_forget_excludes_predicate` helper between
  `forget_cascade._cascade_message_versions` and `digest_context.py` /
  `llm_gateway._digest_context_is_clean`.
- **#295** — Codex T7-02 post-merge carryovers (mostly addressed in T7-05
  bullet-index fix; remaining MED items: provider error categorization,
  EMPTY_WINDOW ledger error field).

These are NOT blockers — Phase 7 ships as ratified.
