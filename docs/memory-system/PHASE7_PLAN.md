# Phase 7 — Daily Digest: Ratified Plan

**Status:** RATIFIED 2026-05-13. Implementation authorized for Sprint 0 (AUTHORIZED_SCOPE.md update) + 8 sprints across 3 waves.
**Predecessors:** Phase 4 (FTS + evidence, CLOSED 2026-04-30), Phase 5 (`llm_gateway` + ledger, CLOSED 2026-05-11), Phase 6 (cards + admin review, CLOSED 2026-05-12), Phase 11 (privacy binding suite, ACTIVE).
**Owner:** Orchestrator A (sole orchestrator for Phase 5 → 6 → 7 → 8 synthesis chain).
**Charter:** `.superflow/charter.md` (run `phase7-daily-summaries`).

---

## 0. Implementation Status: AUTHORIZED

Phase 7 is authorized for implementation following Phase 6 closure 2026-05-12. All
dependencies (Phase 4 evidence, Phase 5 `llm_gateway`/ledger, Phase 6 cards/sources,
Phase 11 binding suite) are satisfied. Sprint 0 must update `AUTHORIZED_SCOPE.md`
(line 177 removal + Phase 7 authorization block) **before any code lands**.

| Component | Status | Notes |
|---|---:|---|
| `AsyncIOScheduler` UTC | Exists | `bot/services/scheduler.py:35`. No MSK precedent — Phase 7 introduces it via `timezone=ZoneInfo("Europe/Moscow")` per cron-trigger. |
| `feature_flags` | Exists | Phase 7 flags default OFF per repo contract. |
| `llm_gateway.synthesize_answer` | Exists | `bot/services/llm_gateway.py:341`. Phase 7 adds `synthesize_digest` as a new method following the same skeleton, including the **defense-in-depth pre-provider revalidation** pattern (`synthesize_answer` re-checks source visibility at `bot/services/llm_gateway.py:436+` before dispatch). |
| `llm_gateway.extract_candidates` | Exists | `bot/services/llm_gateway.py:1057`. Closest analog for new non-Q&A gateway method. |
| `LedgerRepo.record` / `update_placeholder` | Exists | `bot/db/repos/llm_usage_ledger.py:31, :121`. Digest LLM call reuses placeholder → update pattern. `qa_trace_id` nullable — pass `None` for digest calls. |
| `forget_cascade._process_one_event` | Exists | `bot/services/forget_cascade.py:878`. Layer-based dispatch via `_LAYER_FUNCS` (`:799-809`). Phase 7 adds a SINGLE merged `digests` layer (detection + redaction in one transaction; see §5.H — earlier two-layer design was rejected because forget_cascade has no cross-layer payload persistence). |
| `KnowledgeCard` / `CardSource` | Exists | `bot/db/models.py:1085, :1159`. `card_status='approved'` is canonical filter (`bot/db/repos/knowledge_card.py:60, :84, :107`). `CardSource.message_version_id` FK to `message_versions` is `ON DELETE RESTRICT` — cascade DELETE is handled by `_cascade_card_sources` layer (`:619`). |
| Admin identity check | Exists | `_is_admin(message)` helper at `bot/handlers/admin_cards.py:58-61` is the canonical Phase 6 pattern. |
| HTML default parse mode | Exists | All `parse_mode="HTML"` across `bot/handlers/`. `bot/html_escape.py:6` provides `html_escape()` wrapper. |
| `bot.edit_message_text` | NOT used anywhere | Phase 7 is first consumer. Exception handling pattern from `bot/services/invite.py:6, :45` (`TelegramForbiddenError, TelegramBadRequest`). |
| `tests/evals/` Phase 11 suite | Exists | 13 test files using pytest-asyncio + real AsyncSession + parametrize. Phase 7 adds digest-specific cases following the same shape. |
| Migration counter | 035 + 036 on main | `035_add_extraction_runs_operator_user_id.py` on main; `036_add_gateway_error_to_extraction_runs.py` landed via PR #281 (Phase 6.5 carryover, merged 2026-05-13). **Phase 7 starts from 037**. T7-01 implementation re-verifies the current alembic head on main and uses `head+1`. Numbering gap 026-029 unchanged. |
| `digests` / `digest_runs` | DOES NOT EXIST | New Phase 7 tables (migration 037 — see migration counter row above for 036 collision context). |

---

## 1. Non-Negotiable Invariants (verbatim from HANDOFF §1)

1. Existing gatekeeper must not break.
2. **No LLM calls outside `llm_gateway`.** Digest synthesis is a new gateway method (`synthesize_digest`); no direct provider imports anywhere in `bot/services/digests*.py`, `bot/services/digest_publisher.py`, or `bot/handlers/digest.py`.
3. **No extraction / search / q&a / summary over `#nomem` / `#offrecord` / forgotten.** Digest context query has explicit allowlist: `memory_policy='normal'` AND `is_redacted=FALSE` AND no active `forget_events` row for the `message_version_id`.
4. **Citations point to `message_version_id` or approved card sources.** Citation JSONB is array-of-ids only; no raw message text stored as citation anchor.
5. **Summary is never canonical truth.** Digest body is derived prose; consumers (admin handlers, Phase 8 future) must read it as a *recap*, not a source.
6. Graph is never source of truth. (N/A for Phase 7.)
7. Future butler cannot read raw DB directly. (N/A.)
8. Import apply must go through same normalization / governance path. (N/A.)
9. Tombstones are durable and not casually rolled back.
10. Public wiki remains disabled. (N/A.)

---

## 2. Phase 7 Spec (HANDOFF §2)

### Phase 7 — daily summaries

- **Objective:** daily sourced recap.
- **Scope:** `summaries` / `summary_sources` (renamed in this plan to `digests` / `digest_runs` per draft naming convention; `summary_sources` is *not* a separate table — the source list lives inside `digests.citations` JSONB, see §5.A).
- **Dependencies:** Phase 4 minimum; Phase 5 + 6 satisfied.
- **Acceptance:** every bullet has source; forgotten source redacts bullet.

---

## 3. Phase 8 Boundary — what Phase 7 MUST NOT do

- No `reflection_runs`, no `observations`, no `memory_events`, no `memory_candidates`.
- No raw extraction lifecycle tables.
- No graph projection.
- No wiki pages or public digest archive.
- **No weekly digest scheduler / handler / publisher.** The `digests.type` enum accepts `'weekly'` so Phase 8 does not require an ALTER, but Phase 7 ships **only** the `'daily'` runtime path. Phase 7 scheduler does not register a weekly cron. Phase 7 admin handlers reject `weekly` argument with "Phase 8 not yet shipped" message.
- No LLM provider imports in digest code; synthesis routes only through Phase 5 `llm_gateway`.
- No per-user opt-out for being mentioned in digest (Phase 8+).
- No multi-chat digest support (single-chat MVP; one digest per `(type, window)`).

---

## 4. Architecture Overview

```
                    ┌────────────────────────────────────────────┐
                    │ AsyncIOScheduler  (existing, UTC)          │
                    │ - new job: digest_daily                    │
                    │   cron(hour=DIGEST_HOUR_MSK, minute=0,     │
                    │        timezone=ZoneInfo("Europe/Moscow")) │
                    │   default 09:00 MSK = 06:00 UTC            │
                    │   flag: memory.digests.daily.enabled (OFF) │
                    └────────────────────┬───────────────────────┘
                                         │ triggers
                                         ▼
                    ┌────────────────────────────────────────────┐
                    │ digest_daily_job(bot)                       │
                    │ - opens async_session()                     │
                    │ - resolves window: yesterday 00:00 MSK..    │
                    │   today 00:00 MSK (exclusive-end)           │
                    │ - calls run_digest(...)                     │
                    │ - on draft: calls publish_digest(...)       │
                    │ - rollback-on-failure (apscheduler-safe)    │
                    └────────────────────┬───────────────────────┘
                                         │
                                         ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ run_digest(session, *, type='daily',                              │
       │            window_start, window_end,                              │
       │            ledger_repo, provider, config) → Digest                │
       │                                                                   │
       │  1. Idempotency: SELECT digests WHERE (type, ws, we) → return     │
       │  2. Separate cost ceiling check (DIGEST_DAILY_USD_CEILING)        │
       │  3. Build context via context_builder.build_digest_context(...)   │
       │  4. INSERT digest_runs (status=running)                           │
       │  5. INSERT digests (status=draft, body=NULL, llm_usage_ledger_id  │
       │      placeholder)                                                  │
       │  6. llm_gateway.synthesize_digest(...) → body_markdown,            │
       │     citations[], ledger_id                                         │
       │  7. UPDATE digests SET body, citations, ledger_id; commit         │
       │  8. UPDATE digest_runs SET status=finished, finished_at           │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ context_builder.build_digest_context(...)                         │
       │ - approved cards in window (card_sources.created_at OR linked      │
       │   message_version.ts within window) — cards-first per Q5          │
       │ - if <3 cards: top-N raw message_versions, chronological order   │
       │   (governance-filtered, current_version only)                     │
       │ - excludes:                                                        │
       │   * memory_policy != 'normal'                                      │
       │   * is_redacted=TRUE                                               │
       │   * any forget_events row referencing the message_version          │
       │ - token budget ~8k input                                           │
       │ - returns DigestContext: {cards: [...], messages: [...]}          │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ llm_gateway.synthesize_digest(session, *, context, config,        │
       │                              ledger_repo, provider, qa_trace_id  │
       │                              =None) → SynthesisDigestResult       │
       │ - new gateway method following extract_candidates skeleton        │
       │ - shared budget guard (gateway-level _budget_check)               │
       │ - LedgerRepo.record placeholder → LLM call → update_placeholder  │
       │ - returns: body_markdown, citations[], cost_usd, ledger_id        │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
       ┌──────────────────────────────────────────────────────────────────┐
       │ digest_publisher.publish_digest(session, *, digest)               │
       │ - reads draft digest                                              │
       │ - requires DIGEST_DESTINATION_CHAT_ID env (unset → leave draft,   │
       │   audit "skipped_no_destination" in digest_runs)                  │
       │ - renders HTML body via _render_digest_html(body_markdown)        │
       │ - bot.send_message(chat_id, parse_mode="HTML")                    │
       │ - UPDATE digests SET status=posted, posted_*; commit              │
       └───────────────────────────────┬──────────────────────────────────┘
                                       │
                                       ▼
                    ┌────────────────────────────────────────────┐
                    │ digest_runs (audit)                         │
                    │ status: running / finished / failed         │
                    │         / skipped / cost_exceeded /         │
                    │         skipped_no_destination              │
                    │ error_text, started/finished timestamps     │
                    └────────────────────────────────────────────┘

Forget cascade extension (separate path, runs in worker, not in run_digest):

       ┌──────────────────────────────────────────────────────────────────┐
       │ _process_one_event(session, event)                                │
       │ adds ONE merged layer 'digests' in CASCADE_LAYER_ORDER, placed    │
       │ BEFORE 'card_sources' (so card_source ids still queryable when    │
       │ the JSONB scan runs — Codex C4).                                  │
       │ Single-transaction detection + redaction via                      │
       │   redact_digest_for_forget(digest_id, affected_mvids,             │
       │                            affected_card_source_ids, bot):       │
       │     a. SELECT digest FOR UPDATE (blocking, statement_timeout 5s)  │
       │        — waits for the row lock that the publisher holds across   │
       │        bot.send_message (single transaction §5.F); on timeout     │
       │        redactor logs+skips (no raise; per-event isolation         │
       │        preserves remaining layers; publisher revalidation +       │
       │        stale-posting reaper handle the race window)               │
       │     b. masks affected bullets with [REDACTED — забыто]            │
       │     c. updates digests.body_markdown + status='redacted'          │
       │     d. attempts bot.edit_message_text UNCONDITIONALLY for         │
       │        bot-posted messages (no 48h limit per Bot API — Codex H5)  │
       │     e. on TelegramBadRequest (edit refused, bot still in chat) → │
       │        status='redacted_edit_failed' + erratum follow-up via      │
       │        bot.send_message (not as reply).                           │
       │     f. on TelegramForbiddenError (bot kicked) →                   │
       │        status='redacted_edit_failed' + admin notify only          │
       │        (no erratum — bot can't post in that chat;                 │
       │        privacy stop signal, see §8).                              │
       └──────────────────────────────────────────────────────────────────┘
```

---

## 5. Component Design

### 5.A. Migration 037 `add_digests` (T7-01)

**Files created:**
- `alembic/versions/037_add_digests.py` (or `036_` if `chore/p6-codex-h2-h3-followups` is abandoned before Wave 1 — verify at T7-01 implementation)
- ORM additions in `bot/db/models.py` (new classes `Digest`, `DigestRun`).

**Schema: `digests`**

| Column | Type | Constraint |
|---|---|---|
| `id` | `BIGSERIAL` | PK |
| `type` | `TEXT NOT NULL` | `CHECK (type IN ('daily','weekly'))` — `'weekly'` schema-ready, not runtime |
| `window_start` | `TIMESTAMPTZ NOT NULL` | UTC-stored |
| `window_end` | `TIMESTAMPTZ NOT NULL` | UTC-stored, exclusive-end |
| `body_markdown` | `TEXT` | NULL while `status='running'`, NOT NULL after |
| `citations` | `JSONB NOT NULL DEFAULT '[]'::jsonb` | array of `{kind: 'message_version'\|'card_source', id: int_or_uuid_string, position: int}`. For `kind='message_version'`, `id` is `message_versions.id` (integer). For `kind='card_source'`, `id` is `card_sources.id` (UUID as string). **NEVER** `knowledge_cards.id` — citing the card directly bypasses the source-of-truth and survives card_source DELETE, breaking the forget cascade. |
| `status` | `TEXT NOT NULL` | `CHECK (status IN ('running','draft','posting','posted','failed','skipped','cost_exceeded','skipped_no_destination','redacted','redacted_edit_failed'))`. `posting` is a transient publish-in-flight state used by §5.F to interlock with §5.H. |
| `llm_usage_ledger_id` | `BIGINT` | FK `llm_usage_ledger.id` ON DELETE SET NULL |
| `posted_chat_id` | `BIGINT` | NULL until posted |
| `posted_message_id` | `BIGINT` | NULL until posted |
| `posted_at` | `TIMESTAMPTZ` | NULL until posted |
| `posting_started_at` | `TIMESTAMPTZ` | NULL except during the `posting` transient. Stale-posting reaper (§5.K) sweeps rows where `now() - posting_started_at > interval '2 minutes'` back to `failed`. **Explicitly cleared (`SET posting_started_at=NULL`) on every terminal transition** in §5.F steps 6/7/8 so the field is a precise "publish in flight" boolean, not a historical record. |
| `error_text` | `TEXT` | NULL on success. Populated on terminal failure transitions (`failed`, `cost_exceeded`, `redacted_edit_failed`, `publish_lock_timeout`, etc.) — see §5.B / §5.F / §5.H. Mirrors `digest_runs.error_text` for self-contained inspection without joining; the two are written together. |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**Constraints:**
- `UNIQUE (type, window_start, window_end)` — idempotency key.
- Partial index `CREATE INDEX ix_digests_status_draft ON digests(status) WHERE status='draft';` — publisher scan.
- `CHECK (status='posted' ⇒ posted_chat_id IS NOT NULL AND posted_message_id IS NOT NULL AND posted_at IS NOT NULL)`.
- `CHECK (status IN ('draft','posting','posted','redacted','redacted_edit_failed') ⇒ body_markdown IS NOT NULL)` — explicit guard against NULL-body in user-visible states. `status IN ('running','skipped','failed','cost_exceeded','skipped_no_destination')` may have `body_markdown=NULL`. The `posting` transient state inherits body NOT NULL from its `draft` predecessor (§5.F step 1 transitions in-place).
- Indexes for forget cascade scan: `CREATE INDEX ix_digests_citations_gin ON digests USING GIN (citations jsonb_path_ops);` — enables efficient `citations @? '$ ? (@.kind == "message_version" && @.id == $mvid)'` containment checks across all `posted`/`draft`/`redacted*` rows.
- Index for stale-posting reaper (§5.K): `CREATE INDEX ix_digests_posting_started_at ON digests(posting_started_at) WHERE status='posting';` — partial index limited to in-flight rows.

**Schema: `digest_runs`**

| Column | Type | Constraint |
|---|---|---|
| `id` | `BIGSERIAL` | PK |
| `digest_id` | `BIGINT` | FK `digests.id` ON DELETE SET NULL |
| `status` | `TEXT NOT NULL` | `CHECK (status IN ('running','finished','failed','skipped','cost_exceeded','skipped_no_destination'))` |
| `started_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `finished_at` | `TIMESTAMPTZ` | NULL while running |
| `error_text` | `TEXT` | NULL on success |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

**Rollback:** Drops only `digest_runs` then `digests`. No touch on `chat_messages`, `message_versions`, `knowledge_cards`, `card_sources`, `llm_usage_ledger`.

### 5.B. `bot/services/digests.py` (T7-02)

**Public API:**

```python
async def run_digest(
    session: AsyncSession,
    *,
    type: Literal['daily'],  # 'weekly' rejected with ValueError in Phase 7
    window_start: datetime,  # UTC
    window_end: datetime,    # UTC
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    config: LLMGatewayConfig,
    digest_config: DigestConfig,  # new, see below
) -> Digest: ...

@dataclass(frozen=True)
class DigestConfig:
    daily_cost_ceiling_usd: Decimal  # from DIGEST_DAILY_USD_CEILING, default Decimal("1.00")
    monthly_cost_ceiling_usd: Decimal  # from DIGEST_MONTHLY_USD_CEILING, default Decimal("10.00")
    source_chat_id: int  # from DIGEST_SOURCE_CHAT_ID
    destination_chat_id: int | None  # from DIGEST_DESTINATION_CHAT_ID
    hour_msk: int  # from DIGEST_HOUR_MSK, default 9
    min_cards_threshold: int  # default 3 — below this, fall back to raw messages
    raw_message_top_n: int  # default 15 — top-N when cards insufficient
    token_budget_input: int  # default 8000

def load_digest_config() -> DigestConfig: ...
```

**Return contract:** `run_digest` ALWAYS returns a `Digest` row. Cost-exceeded, skipped, failed states all materialize as rows (with appropriate status). Caller inspects `digest.status` to decide next action. No `None` return path.

**Behaviour:**

1. **Race-safe idempotency.** Compute lock key with explicit UTC canonicalization to avoid timezone-sensitive string divergence (Codex A): `lock_key = hashtextextended(:type || '|' || to_char(:ws AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') || '|' || to_char(:we AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 0)`. Acquire `pg_advisory_xact_lock(lock_key)` for the transaction. Then `SELECT * FROM digests WHERE type=:t AND window_start=:ws AND window_end=:we FOR UPDATE`. If row exists, COMMIT (releasing lock) and return the row. No LLM call, no `digest_runs` insert.

> **PostgreSQL version note:** `hashtextextended` is available in PostgreSQL 11+. CI Postgres is 14+ (verify via `docker-compose.yml`); fallback is not needed. If a future migration to Postgres 10- ever happens, swap to `hashtext(...)` (returns int32, also fits advisory-lock signature).
2. **Cost ceiling pre-check (Phase 7 separate bucket):**
   - `SELECT COALESCE(SUM(l.cost_usd), 0) FROM llm_usage_ledger l JOIN digests d ON d.llm_usage_ledger_id=l.id WHERE d.created_at >= today_00_utc` → if `≥ digest_config.daily_cost_ceiling_usd` → INSERT `digests` row with `status='cost_exceeded'`, `body_markdown=NULL`, `citations='[]'::jsonb`; INSERT `digest_runs` `status='cost_exceeded'`, `error_text='daily digest budget exceeded'`; commit and return the digest row. **No LLM call**, no provider import.
   - Same shape for monthly check (separate env var).
3. **Open `digest_runs`** with `status='running'`.
4. **Insert `digests`** with `status='running'`, `body_markdown=NULL`, `citations='[]'::jsonb`. The advisory lock prevents the SELECT/INSERT race that two concurrent callers would otherwise see.
5. **Build context** via `build_digest_context(...)` (§5.C).
6. **Empty-window short-circuit:** if `context.cards == [] AND context.messages == []` → UPDATE `digests` `status='skipped'`, no LLM call, UPDATE `digest_runs` `status='skipped'`, return the digest row.
7. **Call `llm_gateway.synthesize_digest(...)`** (§5.D). Gateway records ledger row internally AND re-validates context (defense-in-depth).
8. **UPDATE `digests`** with `body_markdown`, `citations`, `llm_usage_ledger_id`, `status='draft'`. Commit (releases advisory lock).
9. **UPDATE `digest_runs`** `status='finished'`, `finished_at=now()`.
10. **Return Digest** (caller decides to publish or not).

**Error handling:**
- LLM call raises → UPDATE `digests` `status='failed'`, UPDATE `digest_runs` `status='failed'`, `error_text=str(exc)`. No post.
- Context build raises → same.
- Cost exceeded → see #2.

### 5.C. `bot/services/digest_context.py` (T7-03)

**Public API:**

```python
@dataclass(frozen=True)
class DigestContextCard:
    card_id: UUID
    title: str
    body_markdown: str  # MarkdownV2 from KnowledgeCard.body_markdown
    source_count: int
    card_source_ids: list[UUID]  # for citations

@dataclass(frozen=True)
class DigestContextMessage:
    message_version_id: int
    chat_message_id: int
    author_display: str  # html_escape'd
    text: str           # current version normalized_text
    ts: datetime
    # NOTE: reaction_count / reply_count fields intentionally absent.
    # Codex H4: chat_messages has no reaction/reply columns. Phase 7 uses
    # chronological ordering only. If ranking heuristics are needed, they
    # land in a separate Phase 8 migration ticket.

@dataclass(frozen=True)
class DigestContext:
    type: Literal['daily']
    window_start: datetime
    window_end: datetime
    cards: list[DigestContextCard]
    messages: list[DigestContextMessage]
    source_chat_id: int

async def build_digest_context(
    session: AsyncSession,
    *,
    type: Literal['daily'],
    window_start: datetime,
    window_end: datetime,
    source_chat_id: int,
    digest_config: DigestConfig,
) -> DigestContext: ...
```

**Query shape (cards):**

```sql
SELECT kc.id, kc.title, kc.body_markdown, COUNT(cs.id) AS source_count,
       array_agg(cs.id) AS source_ids
FROM knowledge_cards kc
JOIN card_sources cs ON cs.card_id = kc.id
JOIN message_versions mv ON mv.id = cs.message_version_id
JOIN chat_messages cm ON cm.id = mv.chat_message_id
WHERE kc.card_status = 'approved'
  AND cm.chat_id = :source_chat_id
  AND mv.ts >= :window_start
  AND mv.ts <  :window_end
  AND cm.memory_policy = 'normal'
  AND mv.is_redacted = FALSE
  AND NOT EXISTS (
      SELECT 1 FROM forget_events fe
      WHERE fe.target_type IN ('message','message_hash','user')
        AND fe.status IN ('pending','processing','completed')
        AND <fe matches mv per existing forget_cascade matching logic>
  )
GROUP BY kc.id
ORDER BY kc.approved_at DESC NULLS LAST
LIMIT 30;
```

> Note: the `NOT EXISTS forget_events` clause must reuse the same matching predicate that `_cascade_message_versions` uses (`bot/services/forget_cascade.py:255+`). T7-03 implementation extracts this predicate into a SQL-text helper `_forget_excludes_predicate(mvid_column: str) -> str` that both forget_cascade and digest_context import — DRY guard against drift.

**Query shape (raw messages, fallback):**

If `len(cards) < digest_config.min_cards_threshold`:

```sql
SELECT mv.id, mv.chat_message_id, u.display_name, mv.normalized_text, mv.ts
FROM message_versions mv
JOIN chat_messages cm ON cm.id = mv.chat_message_id
JOIN users u ON u.id = cm.user_id
WHERE cm.chat_id = :source_chat_id
  AND cm.current_version_id = mv.id  -- only current version
  AND mv.ts >= :window_start
  AND mv.ts <  :window_end
  AND cm.memory_policy = 'normal'
  AND mv.is_redacted = FALSE
  AND NOT EXISTS (<forget_excludes_predicate>)
ORDER BY mv.ts ASC
LIMIT :raw_message_top_n;
```

> **Important** (Codex H4): `chat_messages.user_id` is the canonical column (verified against `bot/db/models.py:226+`). `from_user_id` does NOT exist. Reaction / reply count columns also do NOT exist on `chat_messages` — Phase 7 commits to **chronological ordering only**. Adding ranking heuristics (reactions, reply chain depth) requires a separate migration ticket and is out of scope for Phase 7. Token-budget overflow is dropped from the **tail** (most recent excluded last, oldest included first — chronological priority).

**Token budget enforcement:**
- After fetch, accumulate token estimate (rough: `len(text) // 3.5`) until ≤ `digest_config.token_budget_input - 1000` (1k headroom for prompt template).
- Drop overflow from raw_messages first (cards have higher priority).

### 5.D. `bot/services/llm_gateway.py` extension — `synthesize_digest` (T7-02 sub-component)

**New public method, following `extract_candidates` skeleton:**

```python
async def synthesize_digest(
    session: AsyncSession,
    *,
    context: DigestContext,
    config: LLMGatewayConfig,
    ledger_repo: LedgerRepoProtocol,
    provider: LLMProvider,
    prompt_template_version: str = "digest-v0.1.0",
) -> SynthesizeDigestResult: ...

@dataclass(frozen=True)
class SynthesizeDigestResult:
    body_markdown: str
    citations: list[dict]  # [{kind: 'message_version'|'card_source', id: ..., position: int}]
    llm_usage_ledger_id: int | None
    cost_usd: Decimal
```

**Behaviour:**
1. **Pre-provider revalidation (defense-in-depth, Codex C1)** via private helper `_digest_context_is_clean(session, *, cards, messages) -> None` (raises on any failure). The helper re-queries every source id in `context.cards` (`card_sources.id` set) and `context.messages` (`message_versions.id` set) under a single read transaction and asserts:
   - For each `mv` in context.messages: `chat_messages.memory_policy='normal'` AND `message_versions.is_redacted=FALSE` AND no row in `forget_events` matches it via `_forget_excludes_predicate`.
   - For each `cs` in context.cards: parent `knowledge_cards.card_status='approved'` AND the `card_sources` row still exists AND the linked `message_versions` row passes the same check above.
   - If ANY source fails revalidation → **raise `DigestContextStaleError`** (defined in `bot/services/digest_errors.py`) before the provider call. Caller (`run_digest`) catches → UPDATE `digests` `status='failed'` with `error_text='context_stale_post_forget_race'`, no LLM call. Phase 11 binding test I5a covers this exact race.
   - This mirrors the Phase 5 `synthesize_answer` revalidation pattern (`bot/services/llm_gateway.py:436+`).
2. Call `_budget_check(session, config, ledger_repo)` — shared gateway-level guard. If exceeded, raise `LLMBudgetExceededError`. (Phase 7 *also* has its own ceiling pre-check upstream in `run_digest` for separate bucket accounting; both fire.)
3. Hash context for `prompt_hash` (deterministic, ordered).
4. `LedgerRepo.record` placeholder with `qa_trace_id=None`, `provider`, `model`, `prompt_hash`, `tokens_in=estimate`, `tokens_out=0`, `cost_usd=0`, `latency_ms=0`, `cache_hit=False`, `error=None`.
5. Call provider with digest prompt (see below).
6. Parse provider response → `body_markdown` + `citations[]`.
   - **EMPTY_WINDOW sentinel handling (Codex M round-3):** if `body_markdown.strip() == "EMPTY_WINDOW"` (exact sentinel from prompt):
     - If `context.cards == [] AND context.messages == []` → expected; raise `DigestEmptyWindowError`. Caller marks `status='skipped'`.
     - If `context.cards != [] OR context.messages != []` → provider misbehaviour (echoed sentinel despite non-empty input); raise `DigestProviderError("empty_window_echo_with_nonempty_context")`. Caller marks `status='failed'`. Do not proceed to citation validation (step 7) — it would spuriously fire on a degenerate body.
   - Otherwise proceed to step 7.
7. **Citation validation (Codex H3 + H tightening).** For each parsed citation token in the body:
   - `kind='message_version'`, `id=int` MUST appear in input `context.messages` ids.
   - `kind='card_source'`, `id=UUID` MUST appear in input `context.cards` source_ids.
   - Hallucinated ids (don't match either set) → drop from citation list, log structured warning.
   - **Bullet-level invariant — checked AFTER drop AND regardless of whether the prompt produced any citations at all:** scan `body_markdown` for bullet boundaries (lines starting with `- ` or `• `) and verify every bullet contains at least one **valid** citation token (matching an input id). The check fires identically for: (a) bullets with no citation tokens whatsoever, (b) bullets where all tokens were hallucinated and got dropped, (c) bullets with only malformed `[[card:...]]` tokens. Any such bullet → raise `DigestCitationValidationError`. Caller marks digest `status='failed'`, no post. Charter AC #4 + HANDOFF I-4 are otherwise violated.
8. `LedgerRepo.update_placeholder` with actual `cost_usd`, `response_hash`, `tokens_in`, `tokens_out`, `latency_ms`.
9. Return `SynthesizeDigestResult`.

**Provider prompt template (`bot/services/llm_prompts/digest_v0_1_0.py`):**

```
SYSTEM:
You are writing a daily digest for a private community chat.
Output format (strict):
  Line 1-3: TL;DR — 3 short sentences in Russian, prose.
  Blank line.
  Then 5-7 bullets, each:
    - Topic title (≤8 words).
    - 1-2 sentence summary.
    - Citation tokens: [[cs:UUID]] for an approved card source, [[mv:INT]] for
      a raw message version. EVERY bullet MUST contain at least one citation
      token. Citation tokens MUST reference verbatim ids from the input below.
Use Russian. Be neutral. Do not invent facts.
Citations MUST reference input ids verbatim. Do not invent ids.
If the input has no cards and no messages, return exactly: EMPTY_WINDOW

USER:
Window: {window_start_msk} .. {window_end_msk} (Europe/Moscow)
Cards ({len(cards)}):
  Card "{c.title}" (approved). Source ids you may cite: {c.card_source_ids_csv}
  Card body: {c.body_markdown_stripped}
  ---
Messages ({len(messages)}):
  [mv:{m.message_version_id}] {m.author_display}, {m.ts_msk}: {m.text}
  ---
```

> **Critical citation contract** (Codex C4): cards are cited by `card_sources.id` (UUID), NOT by `knowledge_cards.id`. Citing the card directly would survive a forget-cascade DELETE on the underlying card_source row and leave forgotten content visible. The prompt deliberately exposes only the `card_source_ids` and never the `card_id` to the model. The output token is `[[cs:UUID]]` (`cs` = card_source).

**Output parsing:**
- Tokenize for `[[cs:UUID]]` and `[[mv:INT]]` patterns. Reject any `[[card:...]]` token as malformed — log + drop.
- Citations list = ordered unique appearances. Position = bullet index (0-based).
- Validation per behaviour step 7 above. Hallucinated drop + zero-citation-bullet check are both binding.
- **If body == "EMPTY_WINDOW":** raise `DigestEmptyWindowError` — caller (`run_digest`) converts to `status='skipped'`.

### 5.E. Scheduler hook: `bot/services/scheduler.py` extension (T7-04)

**Addition at end of `setup_scheduler(bot)` function:**

```python
from zoneinfo import ZoneInfo
from bot.config import settings

if settings.DIGEST_DAILY_ENABLED:
    scheduler.add_job(
        digest_daily_job,
        "cron",
        hour=settings.DIGEST_HOUR_MSK,
        minute=0,
        args=[bot],
        id="digest_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        timezone=ZoneInfo("Europe/Moscow"),
    )
```

**Note on flag check:** the existing scheduler pattern uses inline `if` at registration time (e.g. there is no early-return inside job body if a flag flips OFF at runtime). For Phase 7 we follow the same convention BUT also add a runtime check inside `digest_daily_job` for safety — `await feature_flag_repo.is_enabled('memory.digests.daily.enabled')` → if False, log and return without DB writes. **Both layers gate execution.**

**Job body (in `bot/services/digests.py`):**

```python
async def digest_daily_job(bot: Bot) -> None:
    async with async_session() as session:
        flag = await FeatureFlagRepo(session).is_enabled("memory.digests.daily.enabled")
        if not flag:
            logger.info("digest_daily_job: flag disabled, skipping")
            return
        # Compute window: yesterday 00:00 MSK .. today 00:00 MSK
        msk = ZoneInfo("Europe/Moscow")
        now_msk = datetime.now(tz=msk)
        today_msk_midnight = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_msk_midnight = today_msk_midnight - timedelta(days=1)
        window_start = yesterday_msk_midnight.astimezone(timezone.utc)
        window_end = today_msk_midnight.astimezone(timezone.utc)
        digest_config = load_digest_config()
        gateway_config = load_gateway_config()
        try:
            digest = await run_digest(
                session, type='daily',
                window_start=window_start, window_end=window_end,
                ledger_repo=LedgerRepo(session),
                provider=resolve_provider(gateway_config.provider_name),
                config=gateway_config,
                digest_config=digest_config,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("digest_daily_job: run_digest failed")
            return
        if digest.status == 'draft':
            try:
                await publish_digest(session, bot=bot, digest=digest, digest_config=digest_config)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("digest_daily_job: publish_digest failed")
```

### 5.F. `bot/services/digest_publisher.py` (T7-05)

**Public API:**

```python
async def publish_digest(
    session: AsyncSession,
    *,
    bot: Bot,
    digest: Digest,
    digest_config: DigestConfig,
) -> Digest: ...
```

**Behaviour — single long-lived transaction (Codex round-3 CRITICAL race fix):**

The entire publish operation (lock → revalidate → render → send_message → terminal commit) runs in **ONE transaction**. The previous design committed the `draft → posting` transition before `send_message`, which released the row lock and opened a window where the redactor could see `posting` but find the row unlocked — letting forgotten content reach Telegram. Holding one transaction from posting through send_message keeps the row exclusively locked across the entire network call. Digest publish is at most ~1/day per window, so a long-held transaction is acceptable.

1. **Begin one publisher transaction.** Open transaction with `set_config('statement_timeout', '30s', true)` (covers 5s lock wait + ~20s Telegram network + cushion). The third argument `true` makes the setting transaction-local (form aligned with §5.H redactor for consistency — Codex round-4 nitpick d). **NO automatic retry on `psycopg.errors.QueryCanceled` / statement_timeout fire** (Codex round-4 CRITICAL a). Rationale: if the 30s window elapses mid-`bot.send_message`, the Telegram API may have already received the request and posted the message even though our local transaction rolled back. A naive retry could double-post. Explicit policy: on `QueryCanceled` raised from this transaction → log structured error `publish_statement_timeout digest_id=:id`, admin notify with `error_text='publish_statement_timeout_possible_partial_send'`, run **a fresh transaction** that does `UPDATE digests SET status='failed', error_text='publish_statement_timeout_possible_partial_send', posting_started_at=NULL, updated_at=now() WHERE id=:id AND status='posting' RETURNING id` (the `status='posting'` guard prevents trampling a row that some other recovery path already touched). Mark in operator runbook (T7-08) that this exception class requires manual Telegram-side check: did the original publish in fact post? If yes → manually mark `posted` via admin DB query; if no → safe to leave as `failed`. Scheduler retry on next tick MUST NOT auto-republish — `run_digest` idempotency returns the existing `failed` row, and `/digest_now` admin path is the only authorized recovery. Try `SELECT * FROM digests WHERE id=:id FOR UPDATE NOWAIT`. **NOWAIT failure handling (Codex C):** on `psycopg.errors.LockNotAvailable`, retry up to 3 times with `asyncio.sleep(0.1 * 2**attempt)` backoff (100ms, 200ms, 400ms). After 3 failed retries → in a **fresh transaction** `UPDATE digests SET status='failed', error_text='publish_lock_timeout', updated_at=now() WHERE id=:id AND status='draft' RETURNING id` (the `status='draft'` predicate prevents racing another worker that successfully published; if 0 rows returned, another worker has the row — log and exit silently). INSERT `digest_runs` `status='failed'`; admin notify (§5.J); return. If row is not `status='draft'` after lock acquired → ROLLBACK and raise `DigestPublisherInvalidState(status)` (already published, skipped, or in-flight via another worker). Otherwise UPDATE `digests` SET `status='posting'`, `posting_started_at=now()`, `updated_at=now()` — but **DO NOT commit** yet. The row lock is held for the remainder of the transaction.
2. If `digest_config.destination_chat_id is None`:
   - UPDATE `digests` `status='skipped_no_destination'` (reverting from `posting`), no error raised, log info.
   - INSERT `digest_runs` `status='skipped_no_destination'`, return.
3. **Defense-in-depth citation revalidation** (Codex C1 reapplied at publish boundary). Re-query every citation source id; if any source is now forgotten/redacted/missing → UPDATE `digests` `status='failed'`, `error_text='citations_stale_at_publish'`, admin notify, return. This blocks the narrow race window where forget completes between draft creation and publish dispatch.
4. Render HTML: `body_html = _render_digest_html(digest.body_markdown, digest.citations)` (§5.G).
5. Call `bot.send_message(chat_id=destination_chat_id, text=body_html, parse_mode="HTML", disable_web_page_preview=True)`. The row lock is still held across this call — any concurrent redactor on the same digest_id either blocks on `FOR UPDATE` (until our terminal commit) or hits its own short statement_timeout and skips.
6. On success: `UPDATE digests SET status='posted', posted_chat_id=:cid, posted_message_id=:mid, posted_at=now(), posting_started_at=NULL WHERE id=:id AND status='posting' RETURNING id` (Codex round-3 HIGH: the `status='posting'` guard rejects any racing transition; if rowcount=0, the reaper or another worker already moved the row → log and rollback). On rowcount=1: COMMIT.
7. On `TelegramBadRequest` (parse error / format error): `UPDATE digests SET status='failed', error_text=:msg, posting_started_at=NULL, updated_at=now() WHERE id=:id AND status='posting'`; INSERT `digest_runs` `status='failed'`, `error_text=str(exc)`. COMMIT. **Admin notify** (§5.J).
8. On `TelegramForbiddenError` (bot not in destination chat at publish time): `UPDATE digests SET status='failed', error_text='bot_not_in_destination', posting_started_at=NULL, updated_at=now() WHERE id=:id AND status='posting'`; INSERT `digest_runs` `status='failed'`, `error_text='bot_not_in_destination'`. COMMIT. Admin notify.

> The `posting` status is added to the `status` CHECK constraint in §5.A. It indicates "publish transaction in flight". Because the publisher holds the row lock across `bot.send_message`, the redactor's `SELECT ... FOR UPDATE` blocks on the row, not on the `posting` *state* — the state is just a runbook signal for the reaper (§5.K) to know how to interpret an orphan row whose publisher transaction crashed.

### 5.G. HTML rendering — `bot/services/digest_renderer.py` (T7-05 sub-component)

**Public API:**

```python
def render_digest_html(
    body_markdown: str,
    citations: list[dict],
) -> str: ...
```

**Behaviour (Codex M3 — truncation order):**
1. Strip `[[cs:UUID]]` and `[[mv:INT]]` tokens from `body_markdown` (per Q6: no inline citations in public post). Use a single regex pass.
2. **Truncate plain MarkDown FIRST** if length > 3800 (leave 200 chars for footer + HTML overhead). Truncate at the last paragraph boundary (`\n\n`) before the limit. Append `...`. Truncating before MD→HTML avoids cutting inside an open `<b>`/`<i>` tag (which would produce invalid Telegram HTML and silently drop the entire message).
3. Escape via `html_escape` (already used elsewhere).
4. Convert minimal Markdown to HTML on the escaped text:
   - `**bold**` → `<b>...</b>` (regex match the escaped `**` pairs)
   - `*italic*` → `<i>...</i>`
   - Bullets (`- ` or `• ` at line start) → `• ` prefix (plain text, no `<ul>` — Telegram doesn't support lists; bullets become `• Topic\n  Summary\n`).
   - Preserve line breaks.
5. Append footer: `\n\n<i>Дайджест за {window_start_msk:%d.%m.%Y}. Полный список источников: /digest_history</i>`.
6. **Tag balance assertion.** Before returning, count opening vs closing `<b>` and `<i>` tags. If imbalanced → log structured error and strip ALL HTML formatting tags, returning plain-escaped text + footer. Better dumb-but-valid than malformed-and-silently-dropped.

### 5.H. Forget cascade extension (T7-05 sub-component) — SINGLE-LAYER DESIGN

**Modifications to `bot/services/forget_cascade.py`:**

> **Critical design constraint** (Codex C2): `forget_cascade._process_one_event` checkpoints layers and skips completed ones on retry (`forget_cascade.py:979+`). `ForgetEvent` has no `payload` column for cross-layer state passing (`models.py:496`). Phase 7 therefore uses a **single combined layer** that performs detection + redaction in one DB transaction — no payload-passing across layers. This eliminates the crash-recovery hole where layer-1 (detection) completes, crash, layer-2 (redaction) resumes with empty payload.

1. Add ONLY `'digests'` to `CASCADE_LAYER_ORDER` (`:132-154`). Position: **BEFORE** `'card_sources'` (Codex C4: digests must scan `kind='card_source'` citations BEFORE the card_sources layer DELETEs the underlying rows; otherwise the join-back becomes impossible). The layer is **idempotent**: re-running it after `redacted` rows already exist is a no-op for those rows (status check).
2. Add applicability in `_LAYER_APPLICABLE_TARGET_TYPES` — `digests` layer applies to all `target_type IN ('message','message_hash','user')`.
3. Implement:

```python
async def _cascade_digests(session: AsyncSession, event) -> int:
    """Detect digests citing the forgotten source and redact them in a
    single transaction. Handles both kind='message_version' and
    kind='card_source' citations. Runs BEFORE _cascade_card_sources so
    the card_source rows still exist for the JSONB scan.
    """
    mvids = await _resolve_affected_mvids(session, event)
    if not mvids:
        return 0

    # Resolve affected card_source ids: card_sources rows whose
    # message_version_id is in mvids. We do this NOW because the next
    # layer (_cascade_card_sources) will DELETE these rows.
    from sqlalchemy import bindparam, BigInteger, String
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY

    cs_rows = await session.execute(
        text("""
            SELECT id::text FROM card_sources
            WHERE message_version_id = ANY(:mvids)
        """).bindparams(bindparam("mvids", type_=PG_ARRAY(BigInteger))),
        {"mvids": list(mvids)},
    )
    affected_cs_ids = {r[0] for r in cs_rows}

    # JSONB scan using jsonb_array_elements — bindparam-safe (Codex H1).
    # Typed bindparams: mvids as bigint[], cs_ids as text[]. SQLAlchemy
    # then renders the appropriate cast and prevents string-injection.
    digest_rows = await session.execute(
        text("""
            SELECT d.id
            FROM digests d
            WHERE d.status IN ('draft','posting','posted','redacted','redacted_edit_failed')
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(d.citations) AS elem
                  WHERE (
                      (elem->>'kind') = 'message_version'
                      AND (elem->>'id')::bigint = ANY(:mvids)
                  ) OR (
                      (elem->>'kind') = 'card_source'
                      AND (elem->>'id') = ANY(:cs_ids)
                  )
              )
            ORDER BY d.id
            FOR UPDATE OF d
        """).bindparams(
            bindparam("mvids", type_=PG_ARRAY(BigInteger)),
            bindparam("cs_ids", type_=PG_ARRAY(String)),
        ),
        {"mvids": list(mvids), "cs_ids": list(affected_cs_ids)},
    )
    affected_digest_ids = [r[0] for r in digest_rows]
    if not affected_digest_ids:
        return 0

    from bot.services.digest_redactor import redact_digest_for_forget
    count = 0
    for digest_id in affected_digest_ids:
        # bot is threaded via cascade worker arg; None in tests.
        bot = getattr(event, "_runtime_bot", None)
        await redact_digest_for_forget(
            session,
            digest_id=digest_id,
            affected_mvids=mvids,
            affected_card_source_ids=affected_cs_ids,
            bot=bot,
        )
        count += 1
    return count
```

4. Register in `_LAYER_FUNCS` (`:799-809`). The `event._runtime_bot` attribute is set by the cascade worker wrapper before `_process_one_event` runs (see "bot threading" note below).

**New file `bot/services/digest_redactor.py`:**

```python
async def redact_digest_for_forget(
    session: AsyncSession,
    *,
    digest_id: int,
    affected_mvids: set[int],
    affected_card_source_ids: set[str],
    bot: Bot | None,
) -> None: ...
```

Logic:
1. **Acquire row lock — BLOCKING with transaction-local statement_timeout (Codex D + round-3 M fix).** Use `set_config('statement_timeout', '5s', true)` (third arg `true` = transaction-local; the previous `SET LOCAL` formulation was correct but the `set_config` form is more defensible against being inherited by an outer transaction's savepoint scope). Run `SELECT * FROM digests WHERE id=:id FOR UPDATE` (no NOWAIT). The lock either acquires (after publisher commits — when publisher held the row lock across `send_message`, the row is now in `posted` / `failed` / `skipped_no_destination` terminal state) OR the 5s statement_timeout fires.
   - **Verified contract (corrected):** `bot/services/forget_cascade.py` has NO retry-with-backoff path. `_process_one_event` per-event isolation marks the event row `failed` on any raised exception. There is no automatic retry.
   - **On statement_timeout:** the redactor catches `psycopg.errors.QueryCanceled` (or equivalent) and **does NOT raise** — it logs a structured warning `digest_redact_timeout digest_id=...` and **returns without redacting that row** (returns rowcount 0 for the layer for this digest). The forget event proceeds to the next layer; the event row is NOT marked failed.
   - **Why this is privacy-safe:** the publisher's step-3 revalidation (§5.F) re-checks every citation immediately before `bot.send_message`. If forget-cascade fired during `posting`, the publisher's revalidation sees the now-forgotten source and fails the publish (status → `failed`, never `posted`). The redactor's missed-redact is harmless because there is no Telegram post to redact.
   - **Edge case (publish-then-crash):** if publisher sent the Telegram message but crashed BEFORE the commit transitioning `posting → posted`, the row is stuck `posting` and reaper (§5.K) marks it `failed`. The Telegram message is visible with original content (which may include forgotten data). This is the **acknowledged Case 3 privacy gap** in §8 stop signals — operator runbook (T7-08) provides manual escalation steps. This race window is intrinsic to any send-then-commit pattern and applies equally to Phase 6 card posting; documenting and notifying is the chosen mitigation rather than introducing 2-phase commit infrastructure.
2. If status not in `('draft','posted','redacted','redacted_edit_failed')` → no-op return (e.g. row is in `skipped`, `failed`, `cost_exceeded`, `skipped_no_destination` terminal state — nothing to redact).
3. Walk `digest.citations` JSONB; for each citation matching `affected_mvids` or `affected_card_source_ids`, mark its `position` (bullet index) for masking.
4. Mask `body_markdown`: replace each affected bullet (split by `\n` boundary, locate by position) with `- [REDACTED — забыто]`. Preserve TL;DR header. Build `filtered_citations` = remove affected entries.
5. UPDATE `digests` SET `body_markdown=masked`, `citations=filtered`, `status='redacted'`, `updated_at=now()`. **Persist DB redaction unconditionally before any Telegram call** so a Telegram failure cannot leave the row in inconsistent state.
6. If `digest.posted_message_id IS NOT NULL` AND `bot is not None`:
   - Attempt `bot.edit_message_text(chat_id=posted_chat_id, message_id=posted_message_id, text=render_digest_html(masked, filtered_citations), parse_mode='HTML')` **unconditionally** (Codex H5: bots may edit their own posts indefinitely — there is no 48h limit on bot-posted messages). The previous "≤48h" rule is removed.
   - On `TelegramBadRequest` ("message can't be edited" / "message is not modified" / unknown parse error): UPDATE `status='redacted_edit_failed'`, post erratum via `bot.send_message(chat_id=posted_chat_id, text=erratum_html, parse_mode='HTML')`. Erratum is **not** sent as a reply (does not bump the original).
   - On `TelegramForbiddenError` (bot kicked from `posted_chat_id`): UPDATE `status='redacted_edit_failed'`, log structured error. **No erratum is attempted** because the bot cannot post in that chat. Admin notify with explicit `error_text='bot_kicked_from_posted_chat_id'`. This is a **privacy stop signal**: the forgotten content remains visible in the original Telegram message until an operator manually re-adds the bot or asks an admin to delete the message via Telegram's normal channels. See §8 stop-signal entry for the operator runbook expectation.
7. If `bot is None` (test mode / cascade running without Telegram): DB redaction stands; no Telegram side effects.

**Erratum format:**
```
Дайджест за {window_start_msk:%d.%m.%Y} обновлён: цитата по запросу автора удалена.
Полный текст в /digest_history.
```

> **`bot` threading into cascade worker.** `_process_one_event` runs in `run_cascade_worker_once` (`forget_cascade.py:1085`), which currently does NOT receive `bot`. T7-05 makes the smallest possible change: `setup_scheduler` already has `bot`; pass it as `args=[bot]` to the cascade worker job; the worker wrapper sets `event._runtime_bot = bot` on each event before calling `_process_one_event`. Default `event._runtime_bot = None` for tests and for non-scheduled invocations (e.g. admin manual cascade). When `bot is None`, the digests layer redacts DB-side only — no Telegram side effects. This is back-compat-safe: `_process_one_event` signature unchanged, no existing call site needs modification.

> **Race interlock with `publish_digest`** (Codex C3 + Codex D corrected): the `posting` intermediate status (set in §5.F step 1) is the explicit handshake. The cascade redactor uses **blocking `SELECT ... FOR UPDATE`** with **short statement_timeout = 5s** (§5.H step 1). When the publisher is mid-`posting`, the redactor blocks until the publisher commits its terminal status (`posted` / `failed` / `skipped_no_destination`). The publisher's step-3 revalidation guarantees that if a forget happened during `posting`, the publish transitions to `failed` (never `posted`). On the rare 5s timeout, the redactor logs and returns without raising — the forget event is NOT marked failed (per-event isolation preserved). The stale-posting reaper (§5.K) eventually fails any orphan `posting` row.

### 5.I. Admin handlers — `bot/handlers/digest.py` (T7-06)

**Commands:**

```python
@dp.message(Command("digest_now"), F.chat.type == "private")
async def cmd_digest_now(message: Message, bot: Bot):
    if not _is_admin(message):
        await message.answer("Только для админов.")
        return
    args = message.text.split(maxsplit=1)
    type_arg = args[1].strip().lower() if len(args) > 1 else "daily"
    if type_arg == "weekly":
        await message.answer("Weekly дайджест появится в Phase 8.", parse_mode="HTML")
        return
    if type_arg != "daily":
        await message.answer("Использование: /digest_now [daily]", parse_mode="HTML")
        return
    # Compute window (same as scheduler), run_digest (admin override: bypass flag check),
    # call publish_digest if draft, send result to admin DM with status + posted message link.

@dp.message(Command("digest_preview"), F.chat.type == "private")
async def cmd_digest_preview(message: Message):
    if not _is_admin(message):
        return
    # Parse args: <type> [date YYYY-MM-DD], default daily/yesterday.
    # Run run_digest → render → reply HTML (NOT post to destination).
    # Show full citation breakdown.

@dp.message(Command("digest_history"), F.chat.type == "private")
async def cmd_digest_history(message: Message):
    if not _is_admin(message):
        return
    # SELECT last 14 digests, render table: window, status, citation count, posted_at link.
```

**Admin override semantics for `/digest_now`** (per ratified Q7):
- Runs `run_digest` regardless of `memory.digests.daily.enabled` flag.
- Still respects cost ceiling (separate bucket).
- Still respects all governance filters.
- **Existing-state handling** (Codex M / orphan-draft recovery):
  - If `run_digest` returns existing row with `status='draft'` → publish it (handles cron-tick publisher failures that left an orphan draft).
  - If `status='posting'` → wait briefly (read again after 1s); if still `posting` → reply `"Дайджест уже публикуется, попробуйте через минуту."` Do not race.
  - If `status='posted'` → reply with link to the existing posted message (`t.me/c/<chat_id>/<message_id>`).
  - If `status='skipped'` or `'skipped_no_destination'` → reply `"Окно пустое / destination не настроен. Дайджест не отправлен."`
  - If `status='failed'` or `'cost_exceeded'` → reply with `error_text` and suggested next step (raise ceiling / fix prompt / etc.).
  - If `status IN ('redacted','redacted_edit_failed')` → reply: "Дайджест за это окно был отредактирован после публикации (forget event). Original Telegram message may have been edited or have an erratum. Use `/digest_history` for full audit."

### 5.J. Admin notification on critical failures (T7-05 sub-component)

**New helper `bot/services/digest_admin_notify.py`:**

```python
async def notify_admins_digest_failure(
    bot: Bot,
    *,
    digest_id: int | None,
    status: str,
    error_text: str,
) -> None:
    """Send DM to first admin in settings.ADMIN_IDS on cost_exceeded or
    markdown_render_error. Silent failures for transient errors."""
```

**Triggered on:**
- `status='cost_exceeded'`
- `status='failed'` with TelegramBadRequest (markdown render)
- `status='redacted_edit_failed'`

**NOT triggered on:**
- `status='skipped'` (empty window)
- `status='skipped_no_destination'` (unset destination — expected during rollout)
- Transient LLM errors that get retried (Phase 7 does no auto-retry → all failures land here, but only the actionable subset notifies)

### 5.K. Stale-posting reaper — scheduler job (T7-04 sub-component, Codex F)

**Purpose:** clear orphan `digests.status='posting'` rows that result from a publisher process crash while §5.F's single transaction is open (between `UPDATE ... status='posting'` and the terminal `posted`/`failed` commit). On clean process exit the row reaches a terminal state in one transaction; only an abrupt crash (worker killed, machine reboot, network partition surfacing as `QueryCanceled` partway through) leaves a `posting` row visible to other workers. The reaper handles such orphans on a fixed 5-minute interval.

**Scheduler registration (in `setup_scheduler`, alongside `digest_daily`):**

```python
scheduler.add_job(
    digest_stale_posting_reaper_job,
    "interval",
    minutes=5,
    args=[],  # no bot needed; DB-only operation
    id="digest_stale_posting_reaper",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=60,
)
```

The reaper runs **always** (not gated by `memory.digests.daily.enabled`) so that even after a flag-flip-OFF, any in-flight `posting` rows still get reaped.

**Job body (in `bot/services/digests.py`):**

```python
async def digest_stale_posting_reaper_job() -> None:
    async with async_session() as session:
        # Reap rows stuck in 'posting' for >2 minutes.
        # Codex round-4 MED (g): also clear posting_started_at on the
        # transition so the §5.A invariant (NULL except during posting) holds.
        # Capture the original value in RETURNING for the audit row.
        result = await session.execute(
            text("""
                UPDATE digests
                SET status='failed',
                    error_text='stale_posting_reaper',
                    posting_started_at=NULL,
                    updated_at=now()
                WHERE status='posting'
                  AND posting_started_at < now() - interval '2 minutes'
                RETURNING id, (xmax::text::bigint = 0) AS dummy_for_returning
            """)
        )
        rows = result.fetchall()
        if not rows:
            await session.commit()
            return
        for row in rows:
            await session.execute(
                text("""
                    INSERT INTO digest_runs (digest_id, status, error_text, started_at, finished_at)
                    VALUES (:id, 'failed', 'stale_posting_reaper', now(), now())
                """),
                {"id": row.id},
            )
            logger.warning("digest_stale_posting_reaper: reaped digest_id=%s", row.id)
        await session.commit()
```

**Behaviour invariants:**
- Reaper runs every 5 minutes; partial index `ix_digests_posting_started_at WHERE status='posting'` makes the scan O(orphans) not O(rows).
- 2-minute threshold > typical `posting` duration (publisher transaction including `send_message`, normally <2s) AND > redactor `SELECT FOR UPDATE` statement_timeout (5s in §5.H) AND > publisher's own statement_timeout (30s in §5.F step 1) — guarantees no false-positive reaping of a healthy in-flight publish.
- Reaped rows transition to `status='failed'` with `digest_runs.error_text='stale_posting_reaper'`. Operators see them in `/digest_history`.
- Admin notify on each reaped row (uses §5.J path), so operators are alerted to publisher instability.

**Test coverage (added to T7-04 acceptance below):** insert `digests` row with `status='posting'` and `posting_started_at = now() - interval '3 minutes'`; run reaper; assert row transitions to `failed` with audit row created.

---

## 6. Ratified Decisions (full list)

All 16 decisions locked 2026-05-13.

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Weekly digest in Phase 7? | NO. `digests.type` enum allows `'weekly'` for schema readiness. Scheduler / handler / publisher are daily-only. | Avoid scope creep into Phase 8. |
| 2 | Destination | Community chat (`DIGEST_DESTINATION_CHAT_ID` env). | User Q2=a. |
| 3 | Publish policy | Auto-post (draft → publisher → posted). | User Q3. |
| 4 | Timezone | Europe/Moscow for trigger + display. UTC for storage. | User Q4. |
| 5 | Cost budget | Separate bucket: `DIGEST_DAILY_USD_CEILING` (default `Decimal("1.00")` USD, shape consistent with existing `LLM_DAILY_USD_CEILING`). Phase 5 gateway also enforces shared ceiling — both fire. Charter AC #9 env-var name reconciled from draft `DIGEST_DAILY_COST_USD_CENTS` to this. Status refinement: exceeded → `status='cost_exceeded'` on both `digests` and `digest_runs` (was literal `'failed'` in draft). | User Q5; review 2026-05-13. |
| 6 | Digest format | TL;DR (3 prose lines) + 5-7 bullets. | Best of prose+bullets. Telegram-fit. |
| 7 | Window range | `yesterday 00:00 MSK .. today 00:00 MSK`. | Closed-day, edits/forgets stable. |
| 8 | Admin edit pre-post | N/A (auto-post). `/digest_now` is admin override path. | Q3 implies. |
| 9 | Topic organization | Chronological, no LLM clustering. | MVP simplicity. Cluster → Phase 8 backlog. |
| 10 | Source mix | Cards-first; raw messages if `<3` cards. Token budget 8k input. | Cards = ratified; raw = quality fallback. |
| 11 | Citation rendering | No inline citations in public post. Audit details via `/digest_preview`. | Community readability. |
| 12 | Manual override | `/digest_now` bypasses flag. Cost ceiling still enforced. | Admin pre-rollout testing. |
| 13 | Empty window | Skip without post. `digest_runs.status='skipped'`. | No noise on quiet days. |
| 14 | Failure visibility | Admin notify on `cost_exceeded` + `redacted_edit_failed` + markdown render error. Other → `digest_runs` audit only. | Actionable subset only. |
| 15 | Markdown parse mode | HTML. `body_markdown` storage parses minimal MD; renderer converts to Telegram HTML. | Project default. |
| 16 | Forget-post-publish handling | Try `bot.edit_message_text` **unconditionally** for bot-posted digests (bot may edit own posts without time limit — Codex H5 correction). On `TelegramBadRequest` (edit failure with bot still in chat) → status `redacted_edit_failed` + post erratum follow-up via `bot.send_message`. On `TelegramForbiddenError` (bot kicked from `posted_chat_id`) → status `redacted_edit_failed` + admin notify, **no erratum** (bot can't post anywhere in that chat); forgotten content remains visible until operator escalates (privacy stop signal, §8). Never delete the original Telegram message. | Bot API + transparency. Updated 2026-05-13 from earlier "48h cutoff" rule which was incorrect; further clarified 2026-05-13 (Codex I) that kicked-bot case has no erratum path. |

---

## 7. Tickets

| ID | Title | Component | Wave | Size | Deps | Status |
|---|---|---|---:|---|---|---|
| **T7-S0** | Sprint 0: AUTHORIZED_SCOPE.md update + PHASE7_PLAN.md commit | docs | 0 | S | none | **Required first PR** |
| **T7-01** | Migration 037 `add_digests` + ORM models | A | 1 | M | T7-S0 | Wave 1 |
| **T7-02** | `bot/services/digests.py::run_digest` + `synthesize_digest` gateway method | B | 1 | L | T7-01 | Wave 1 |
| **T7-03** | `bot/services/digest_context.py` builder + governance filter + forget_excludes_predicate refactor | B2 | 1 | M | T7-01 | Wave 1 |
| **T7-04** | Scheduler hook + `digest_daily_job` + config loader | C | 2 | S | T7-02, T7-03 | Wave 2 |
| **T7-05** | `digest_publisher.py` + `digest_renderer.py` + `digest_redactor.py` + forget cascade single merged `digests` layer + admin notify | D | 2 | L | T7-04 | Wave 2 |
| **T7-06** | Admin handlers `/digest_now` `/digest_preview` `/digest_history` | E | 3 | M | T7-05 | Wave 3 |
| **T7-07** | Phase 11 binding tests (leakage L7, citation C6, forget-cascade I5) + governance + cost ceiling regression | F | 3 | M | T7-01..T7-06 | Wave 3 |
| **T7-08** | Operator rollout docs + ratification checklist + IMPLEMENTATION_STATUS update | G | 3 | S | T7-07 | Wave 3 |

### T7-S0 acceptance

- `AUTHORIZED_SCOPE.md` line 177 removed from "NOT authorized" section.
- New "## Authorized: Phase 7" section added between current Phase 6 (`:140-168`) and "NOT authorized" (`:172`), structured like the Phase 6 block.
- `PHASE7_PLAN.md` committed (this file).
- Single PR, docs-only, no code.
- No new migrations, no handler/service changes.

### T7-01 acceptance

- `alembic/versions/037_add_digests.py` creates `digests` + `digest_runs` with all columns and constraints in §5.A. (T7-01 implementation verifies current alembic head on main and uses head+1; plan assumes 037, may downshift to 036 if Phase 6.5 carryover abandoned.)
- `bot/db/models.py` adds `Digest` + `DigestRun` ORM classes matching migration types (JSONB on Postgres for `citations`).
- Migration runs forward and backward cleanly in CI Postgres.
- Rollback drops only Phase 7 tables.
- `tests/db/test_digests_schema.py` asserts: idempotency unique, status check, partial index existence, citations default `[]`.

### T7-02 acceptance

- `run_digest(...)` returns existing digest by `(type, ws, we)` without LLM call when idempotency hits.
- New run: cost ceiling check fires before LLM. Exceeded → BOTH `digests` row (`status='cost_exceeded'`, `body_markdown=NULL`, `citations='[]'::jsonb`) AND `digest_runs` row (`status='cost_exceeded'`, `error_text='daily digest budget exceeded'`) are created. Idempotency-safe re-runs of the same window see the cost_exceeded digest row and short-circuit (per §5.B step 1).
- New run: `synthesize_digest` called exactly once; `LedgerRepo` placeholder + update records cost.
- New run: persists `body_markdown`, `citations`, `llm_usage_ledger_id`, `status='draft'`.
- `synthesize_digest` rejects calls when input context has invalid kinds, logs warning on hallucinated citation ids (drops them, doesn't fail).
- Empty window short-circuit: `status='skipped'`, no LLM call.
- Unit tests cover: idempotency, cost ceiling, empty window, LLM error, hallucinated citation drop.

### T7-03 acceptance

- Context query includes only `memory_policy='normal'` AND `is_redacted=FALSE` AND no active `forget_events` row matching the message_version.
- Cards-first ordering: cards fetched first; raw messages added only when `len(cards) < min_cards_threshold`.
- Token budget enforced (rough estimate; raw messages dropped first on overflow).
- `_forget_excludes_predicate` shared helper extracted, imported by both `digest_context` and `forget_cascade` (no SQL drift).
- Phase 11 binding tests reuse `_forget_excludes_predicate` for assertion logic.
- Unit tests cover: forgotten exclusion, redacted exclusion, offrecord exclusion, card threshold fallback, token overflow drop.

### T7-04 acceptance

- Scheduler job `digest_daily` registered with `timezone=ZoneInfo("Europe/Moscow")`, `hour=settings.DIGEST_HOUR_MSK` (default 9), `max_instances=1`, `coalesce=True`.
- Flag `memory.digests.daily.enabled` default OFF in `feature_flags`.
- Runtime double-check: job body re-checks flag and exits early if disabled.
- Window computation: yesterday 00:00 MSK .. today 00:00 MSK, stored as UTC.
- `bot` threaded through `setup_scheduler` → cascade worker for §5.H access.
- Unit test: mock `datetime.now(tz=MSK)` returning 2026-05-13 09:00 MSK, asserts window = 2026-05-12 00:00..2026-05-13 00:00 MSK in UTC.

### T7-05 acceptance

- Publisher sends HTML body to `DIGEST_DESTINATION_CHAT_ID`, marks `status='posted'`.
- Destination unset → `status='skipped_no_destination'`, no error raised.
- `TelegramBadRequest` → `status='failed'`, admin notify.
- HTML renderer strips `[[cs:UUID]]` / `[[mv:INT]]` tokens from body (rejects malformed `[[card:UUID]]` tokens with log + drop), converts minimal markdown, appends footer.
- HTML rendered output validates against Telegram parse mode (tested with mock + real Telegram in dev env if available). Tag-balance assertion rebalances to plain text if open/close `<b>`/`<i>` counts diverge.
- Forget cascade adds a SINGLE merged `digests` layer (detection + redaction in one transaction); placed BEFORE `card_sources` so card_source ids are still queryable when the JSONB scan runs.
- `redact_digest_for_forget` masks bullets, updates body + citations, transitions `posted → redacted` (or `redacted_edit_failed`), attempts `bot.edit_message_text` UNCONDITIONALLY for bot-posted messages; on `TelegramBadRequest` (bot still in chat, edit refused) → erratum follow-up via `bot.send_message`; on `TelegramForbiddenError` (bot kicked) → admin notify only, **no erratum** (cannot post in that chat). (No 48h cutoff: bots may edit their own posts indefinitely per Bot API.)
- Admin notify fires on `cost_exceeded`, `redacted_edit_failed`, markdown render errors.
- Integration test: insert digest with citations, fire forget_event on cited mvid, run cascade worker, assert digest is redacted in DB + Telegram edit attempted.

### T7-06 acceptance

- `/digest_now [daily]` admin-only, runs even when flag OFF, respects cost ceiling, returns posted message link on success.
- `/digest_now weekly` returns "Phase 8" message.
- `/digest_now` invoked when existing `draft` exists for the window → publishes the existing draft (orphan-draft recovery path). Tested.
- `/digest_now` invoked when existing `posting` row exists → polite "in flight" reply, no race.
- `/digest_now` invoked when window already `posted` → reply with link to the posted message.
- `/digest_preview <type> [date]` renders body + citations to admin DM, does NOT post to destination. Shows the full citation list (resolved ids + source kind).
- `/digest_history` lists last 14 digests with status + citation count + posted message link if available.
- Non-admin invocations: silent no-op (no leak of digest content). Verified by test against non-admin user id.
- Unit tests: admin gate, idempotency reuse, weekly rejection, orphan-draft republish, posting-state polite reply, posted-state link reply.

### T7-07 acceptance

- `tests/evals/test_leakage.py` adds cases L7a (forgotten message_version not in digest body) + L7b (forgotten card_source not in digest body — exercises the dual-kind cascade path).
- `tests/evals/test_citations.py` adds case C6: every digest bullet has at least one citation_id, all ids resolvable in DB, every cited row visible (passes existing C2/C3 invariants).
- New file `tests/evals/test_digest_forget_cascade.py` cases:
  - I5a: forget_event on cited mvid triggers redact + unconditional edit attempt within cascade worker run.
  - I5b: forget_event on cited card_source (via parent card) triggers same path; layer ordering verified (digests runs BEFORE card_sources).
  - I5c: publish-vs-redact race — concurrent `publish_digest` and forget cascade firing on the same digest, with `posting` status interlock; assert exactly one of {posted, redacted, failed} terminal states.
- Existing Phase 11 binding suite preserved with **sub-letter cases intact**: L1-L5 + L6a/b/c + C1-C4 + C5a-d + R1-R4 + I1-I4 = 28/28 baseline (per CLAUDE.md Phase 6 closure). Phase 7 additions land as L7a/b + C6 + I5a/b/c → new total 34/34. No collapsing of existing case names.
- Cost ceiling regression test: insert ledger rows summing > ceiling → next `run_digest` returns `cost_exceeded`.
- Idempotency regression test: two concurrent `run_digest(...)` for same window → only one digest row, second blocks via advisory lock OR returns existing row (verify race-safety).

### T7-08 acceptance

- `docs/memory-system/PHASE7_ROLLOUT.md` checklist: env vars, flag toggle order, dry-run via `/digest_now`, monitor first 3 cron fires, escalation contacts.
- `docs/memory-system/IMPLEMENTATION_STATUS.md` updated: every T7 ticket marked DONE with PR refs.
- `docs/memory-system/ROADMAP.md` line for Phase 7 marked CLOSED with PR list.
- `CLAUDE.md` "Memory System Cycle" section adds Phase 7 closure block (mirrors Phase 5 / 6 wording).
- `AUTHORIZED_SCOPE.md` "Authorized: Phase 7" block updated to CLOSED, "NOT authorized" line for Phase 8 / 9 etc. unchanged.

---

## 8. Stop Signals (apply to all streams)

A Phase 7 stream must STOP and surface immediately if any fire:

- Digest context or body contains `#offrecord`, `#nomem`, redacted, or forgotten content → STOP, do not post.
- Digest citations reference raw message text instead of ids → STOP.
- Direct LLM provider import outside `llm_gateway` → STOP.
- Cost ceiling exceeded → `digest_runs` records, no post, admin notify.
- Posting destination unset → `status='skipped_no_destination'`, no error raised, no admin notify (expected during rollout).
- Scheduler would run while feature flag OFF → both layers (registration check + runtime re-check) prevent execution.
- Weekly digest path activates → STOP for human ratification (Phase 8 boundary breach).
- `_forget_excludes_predicate` drift between digest_context and forget_cascade → STOP, refactor first.
- `bot.edit_message_text` raises `TelegramBadRequest` (any reason) → must NOT propagate; route to erratum path. Bot-posted messages have no edit time limit (Codex H5 correction); the previous "≤48h" guard is removed.
- Citation parsing yields a bullet with **zero** valid citation ids after hallucinated-drop → must FAIL the run, do NOT post.
- Gateway revalidation discovers a stale source (forgotten/redacted between context build and provider call) → must FAIL the run, no LLM call, no post.
- Cascade redactor encounters digest in `posting` state via blocking `FOR UPDATE`: it WAITS on the row lock that the publisher holds across `send_message`. On 5s statement_timeout: **LOG + SKIP without raise**; cascade per-event isolation preserved; no re-queue is attempted (there is no automatic retry path in `forget_cascade.py`). Eventual consistency comes from the publisher's step-3 revalidation (catches forget that happened before send) and the stale-posting reaper (§5.K) handling crashed publishers.
- Layer order violation: `digests` cascade layer placed AFTER `card_sources` → STOP, layer ordering broken (digests must scan card_source citations BEFORE they are deleted).
- `DIGEST_DESTINATION_CHAT_ID == DIGEST_SOURCE_CHAT_ID` (misconfig) → STOP at startup with `ConfigurationError("digest source and destination chat must differ")`. Posting a digest to the same chat it reads from would echo content and confuse community readers.
- **Kicked bot privacy gap (Codex I).** `TelegramForbiddenError` on edit attempt + `posted_message_id IS NOT NULL` → DB redacted, BUT original Telegram message still visible with the forgotten content. Operator runbook (T7-08) MUST include: detect via admin-notify alert `bot_kicked_from_posted_chat_id`, escalate to a chat admin, request manual delete of the Telegram message via `/digest_history`-provided link. Until operator action, the privacy invariant is partially violated by external state. Acknowledged trade-off: alternative would be never posting digests (would block Phase 7 entirely).
- **Publisher lock-not-available (NOWAIT retry exhaustion, Codex C).** `psycopg.errors.LockNotAvailable` after 3 retries with exponential backoff → publisher fails the run hard with `error_text='publish_lock_timeout'`, admin notify. Operator runbook MUST include investigation: who else holds the row lock? Concurrent `/digest_now` from two admins? Stuck `posting` row needing reaper?
- **Stale-posting reaper triggered (§5.K).** Any reap event indicates a publisher crash or runaway. Admin-notify fires per reaped row. Operator MUST investigate logs around `posting_started_at` to find root cause.

---

## 9. PR Workflow

Sprint-PR-queue mode. One PR per ticket. Linear order:

1. T7-S0 → main (docs-only authorization). Solo, no review parallel.
2. T7-01 → main (Wave 1 schema). Reviewed by Codex + Claude product.
3. T7-02 + T7-03 — Wave 1 implementation. **Can ship in EITHER order** but both must be on main before T7-04. Sequential PRs (sprint_pr_queue).
4. T7-04 → main (Wave 2 scheduler).
5. T7-05 → main (Wave 2 publisher + cascade). Largest PR. Likely split into 5A (publisher+renderer) and 5B (cascade+redactor+admin notify) if diff >400 lines.
6. T7-06 → main (Wave 3 admin handlers).
7. T7-07 → main (Wave 3 tests). Phase 11 binding green prerequisite.
8. T7-08 → main (Wave 3 docs + closure).
9. **Final Holistic Review** after T7-08 merged. Two reviewers (Claude deep-product + Codex deep-technical) on the full Phase 7 surface. Fix CRITICAL/HIGH before closure report.

Each PR:
- One ticket, diff ≤400 lines (split if larger).
- Tests added/extended with the change.
- `.par-evidence.json` written before push.
- Codex review via `Agent(subagent_type="codex:codex-rescue")` with technical-lens prompt.
- Claude standard-product-reviewer for product/spec lens.
- Both verdicts PASS / ACCEPTED → PR created.
- CI green → user-initiated merge (Phase 3).

---

## 10. Glossary (Phase 7-specific)

- **Digest:** a derived Markdown recap for a bounded daily window. Not canonical truth.
- **Window:** `yesterday 00:00 MSK .. today 00:00 MSK`, inclusive-start exclusive-end, stored as UTC.
- **Citation:** JSONB entry `{kind: 'message_version'|'card_source', id: int|UUID, position: int}`. Never raw text.
- **`digest_runs`:** audit table for digest generation attempts (start, finish, status, error_text).
- **Feature flag:** `memory.digests.daily.enabled`. Default OFF.
- **Separate cost bucket:** Phase 7 enforces its own `DIGEST_DAILY_USD_CEILING` / `DIGEST_MONTHLY_USD_CEILING` in addition to gateway-shared `LLM_DAILY_USD_CEILING` / `LLM_MONTHLY_USD_CEILING`. Both fire.
- **Erratum:** follow-up Telegram message acknowledging a redaction when `bot.edit_message_text` fails (e.g. `TelegramBadRequest` "message is not modified" or unknown parse error). Bot-posted messages have no edit time limit per Bot API; the earlier "≤48h" rule was incorrect and is removed.
- **Schema-ready `'weekly'`:** the `digests.type` CHECK allows `'weekly'` so Phase 8 needs no ALTER, but no Phase 7 code path produces a weekly row.

---

## 11. Open Design Questions

All 16 questions from `prompts/PHASE7_PLAN_DRAFT.md §11` are RATIFIED (see §6). No open questions remain at plan ratification time.

Future Phase 8 backlog items derived from Phase 7 ratification (not authorized here):
- Topic clustering / inferred-topic ordering.
- Per-user opt-out for being mentioned.
- Multi-chat digest support.
- Weekly digest scheduler / handler / publisher.
- Admin-edit pre-post review queue.
- Inline citation rendering experiment.

---

## 12. Environment variables introduced

| Var | Default | Purpose |
|---|---|---|
| `DIGEST_DAILY_ENABLED` | `false` | Gate scheduler registration (still double-checked by feature flag in DB). |
| `DIGEST_HOUR_MSK` | `9` | Cron fire hour (Europe/Moscow). |
| `DIGEST_SOURCE_CHAT_ID` | — | Required. Community chat to read from. |
| `DIGEST_DESTINATION_CHAT_ID` | unset | If unset → publisher skips. |
| `DIGEST_DAILY_USD_CEILING` | `1.00` | Daily separate-bucket ceiling. |
| `DIGEST_MONTHLY_USD_CEILING` | `10.00` | Monthly separate-bucket ceiling. |
| `DIGEST_MIN_CARDS_THRESHOLD` | `3` | Below this, fall back to raw messages. |
| `DIGEST_RAW_MESSAGE_TOP_N` | `15` | Cap on raw fallback. |
| `DIGEST_TOKEN_BUDGET_INPUT` | `8000` | Context size cap. |

Settings registered in `bot/config.py` with the same parsing pattern as existing memory env vars.

---

## 13. Sprint 0 deliverable (T7-S0)

This document is the deliverable. To complete Sprint 0, the PR must:

1. Add this `PHASE7_PLAN.md` to `docs/memory-system/`.
2. Update `AUTHORIZED_SCOPE.md` via **semantic edits** (not line numbers — line numbers shift between plan-write and PR creation):
   - **Remove** the bullet under `## NOT authorized` section whose text is `- Phase 7 daily summaries — depends on Phase 5 + Phase 6.` (search by exact text, fail if not unique).
   - **Insert** new `## Authorized: Phase 7 — Daily digests (2026-05-13)` block immediately BEFORE the `## NOT authorized` heading. Block content mirrors the Phase 6 block structure: TL;DR, owner (Orchestrator A), scope reference to this `PHASE7_PLAN.md`, NOT-in-scope list mirroring §3 of this plan, ratification date.
3. No code changes.
4. PR title: `docs(memory): ratify Phase 7 plan + authorize implementation`.
5. PR description: short summary + link to this file + reference to charter + summary of two-reviewer findings (this plan was reviewed by Codex + Claude standard-spec-reviewer 2026-05-13 and revised before ratification).

This unblocks Wave 1.
