# AI Memory Layer — Design Proposal for vibe-gatekeeper

> Status: PROPOSAL — for owner `Jekudy` to review, adapt, or discard.
> Nothing in this document is implemented. All code sketches are illustrative.

---

## 1. Problem & Opportunity

The community has 275 members, 3,109 chat messages already persisted in the `chat_messages` table (confirmed from the Coolify DB restore log at `docs/runbook.md:114`), plus 340 questionnaire answers recording each member's background and interests. Today the bot does exactly one thing after onboarding: it watches for intro-refresh deadlines and syncs to Google Sheets. There is no engagement layer. Members cannot ask "who here knows Rust?" or "what did we discuss last week about funding?" and get a useful answer. An AI memory layer — retrieval over stored messages and member data, periodic digests, an auto-maintained wiki — would turn the bot from a gatekeeper into a community assistant, without requiring any infrastructure beyond what is already deployed (Postgres, Redis, APScheduler).

---

## 2. Design Constraints

**Already in the codebase (verified by reading source files):**

- `ChatMessage` table (`bot/db/models.py:113-127`) stores `message_id`, `chat_id`, `user_id`, `text` (nullable), `date`, and `raw_json` for every group message. The `(chat_id, message_id)` unique index (`ix_chat_messages_chat_msg`) is already in place.
- APScheduler runs inside the bot process (`bot/services/scheduler.py`). Four jobs are registered at startup: `check_vouch_deadlines` (15-min interval), `check_intro_refresh` (daily at 10:00 UTC), `sync_google_sheets` (5-min interval), `heartbeat` (30-sec interval). New jobs can be added by calling `scheduler.add_job()` in `start_scheduler()`.
- aiogram routers (`bot/__main__.py:65-73`): 7 routers registered in priority order — `start`, `questionnaire`, `vouch`, `admin`, `chat_events`, `forward_lookup`, `chat_messages` (lowest priority, catches all group messages). New handlers for mention detection and `/search` would be new routers inserted before `chat_messages`.
- FastAPI admin (`web/app.py`): the existing admin surface at port 8080 can host `/digest` and `/wiki` archive pages with no new infrastructure.
- The `_run_with_session()` helper in `bot/services/scheduler.py:30-41` provides a standard pattern for session lifecycle in scheduler jobs — new jobs should reuse it.

**Assumed (not yet in codebase):**

- Postgres 16 available via Coolify. Current Coolify instance runs `postgres:15-alpine` (note at `docs/runbook.md:92-95`). pgvector requires Postgres 11+; the `pgvector` extension is installable as a package on `postgres:15-alpine`. Verify with `SELECT * FROM pg_available_extensions WHERE name = 'pgvector'` before Sprint A.
- Owner has access to at least one LLM provider (Codex subscription, Claude Code subscription, or API key).

**Flexible (owner decides):**

- LLM provider and embedding provider — see §4.
- Privacy model — opt-in vs opt-out — see §6.

---

## 3. Proposed Architecture

```
Telegram chat → aiogram Router → ChatMessage table ─────────────────┐
                                                                     │
[NEW] chat_messages pipeline also triggers embed job ────────────────┤
       (after DB insert, push message_id to Redis queue)             │
                                                                     ▼
                                      [NEW] message_embeddings table (pgvector)
                                           (vector FLOAT4[], message_id FK,
                                            model_name, embedded_at)
                                                                     ▲
[NEW] bot/handlers/mention.py                                        │
      Bot mention in group → retrieve top-8 chunks → LLM → reply ───┤
                                                                     │
[NEW] /search command handler                                        │
      /search <query> → retrieve top-8 → formatted list ────────────┤
                                                                     │
[NEW] scheduler job: daily digest (23:00 UTC)                        │
      SELECT last 24h messages → LLM summary → post to chat ────────┤
      → store in Digest table for /digest archive                    │
                                                                     │
[NEW] scheduler job: weekly wiki extract (Sunday 09:00 UTC)          │
      retrieve top clusters → LLM → upsert wiki_entries (pgvector) ─┘
      → served via /wiki browse page in FastAPI admin
```

**What each new piece does:**

- **Embedding pipeline:** After `chat_messages` handler saves a row, it enqueues the `message_id` to a Redis list (reusing existing `REDIS_URL`). A lightweight consumer — either an APScheduler interval job or a background asyncio task — pops from the queue, generates an embedding vector, and inserts a row into `message_embeddings`. Decoupled from the message-write path so bot latency is unchanged.

- **mention handler (`bot/handlers/mention.py`):** Detects messages where the bot is @-mentioned. Extracts the question, queries `message_embeddings` for the top-8 nearest vectors (cosine similarity), assembles a prompt with retrieved context and Telegram deep-link citations, calls the LLM, and sends the answer as a reply. Rate-limited per `user_id` (e.g., 5 calls per hour via Redis counter) to avoid abuse.

- **/search handler:** A slash command available in the group. Works like the mention handler but returns a bulleted list without LLM generation — pure retrieval. Cheaper (no LLM call) and useful for "find the exact discussion."

- **Daily digest scheduler job:** Runs at 23:00 UTC. Pulls all `ChatMessage` rows from the last 24 hours, batches them into a prompt, calls LLM for a short summary (3-5 bullets), posts to the chat, and saves to a `Digest` table. The FastAPI admin gets a `/digest` route listing historical digests.

- **Weekly wiki extract scheduler job:** Runs Sunday morning. Retrieves all `message_embeddings`, clusters them by topic (simple k-means on vectors or LLM-driven), extracts named entities and concepts, upserts into `wiki_entries` (each entry has a vector for later retrieval). The FastAPI admin gets a `/wiki` browse route.

---

## 4. Stack Recommendations

| Component | Recommended | Alternative | Tradeoff |
|-----------|-------------|-------------|----------|
| Vector store | pgvector in existing Postgres | Qdrant, Weaviate | No new infra; pgvector handles ~100k vectors with an IVFFlat index at <50ms p95 retrieval. Qdrant is faster at 1M+ scale — not needed here. |
| LLM generation | Subprocess pattern (`codex` or `claude` CLI) using existing subscription | Anthropic/OpenAI API with key | Subscription-based = ~$0 per call. API = pay-per-token, simpler code, more reliable for concurrent calls. See §7 for subprocess sketch. |
| Embedding | Local `sentence-transformers` + `intfloat/multilingual-e5-small` (~117 MB, good Russian support) | OpenAI `text-embedding-3-small` via API | Local = zero ongoing cost, adds ~300 MB to Docker image, ~10ms per embed on CPU. API = ~$0.06 one-time backfill for 3,109 messages, ~$0.002/month ongoing at current message rate, no image growth. |
| Chunking | One chunk per `ChatMessage` row (messages are 10-500 chars each); one chunk per `QuestionnaireAnswer` row | Thread-level aggregation | Telegram messages are atomic units; per-message chunks preserve citation precision. |
| Retrieval | Cosine similarity top-k=8, re-ranked by `score = 0.7 * sim + 0.3 * recency_decay` where `recency_decay = exp(-days_old / 30)` | Pure cosine similarity | Chat is time-sensitive: a message from yesterday about the same topic should rank above one from 6 months ago. |
| Citations | Telegram deep-link `https://t.me/c/{abs(chat_id)}/{message_id}` | Quoted text snippet | Deep-links are clickable and verifiable; users can jump to original context. `chat_id` is already stored in `ChatMessage.chat_id`. |

---

## 5. Sprint Plan (owner's roadmap, 5 sprints)

Each sprint = 1 PR on the owner's branch. File paths are concrete targets; owner may adjust.

**Sprint A — pgvector foundation**
- Enable `pgvector` extension: `CREATE EXTENSION IF NOT EXISTS vector;`
- Alembic migration: add `message_embeddings` table with columns `(id, message_id FK, vector VECTOR(384), model_name, embedded_at)` and an IVFFlat index.
- Create `bot/services/embedding.py` — `EmbeddingClient` interface + two concrete backends: `LocalEmbeddingClient` (sentence-transformers) and `OpenAIEmbeddingClient`.
- Backfill script `scripts/backfill_embeddings.py` — idempotent, processes `ChatMessage` rows without existing embedding rows, batch size 50.
- Tests: mock `EmbeddingClient`, verify migration runs cleanly.
- No user-facing change.

**Sprint B — mention handler + /search + RAG**
- `bot/handlers/mention.py` — mention detection filter, retrieve, LLM call, reply with citations.
- `bot/services/rag.py` — `retrieve(query, top_k=8)` + `rerank()` + `build_prompt()`.
- `/search` command in `bot/handlers/mention.py` (same router, different filter).
- Redis-backed rate limiter: `SEARCH_RATE_LIMIT` env var (default 5/hour/user).
- Register router in `bot/__main__.py` before `chat_messages.router`.
- Tests: mock retrieval and LLM, assert reply contains citation links.

**Sprint C — daily digest**
- `bot/services/digest.py` — `generate_daily_digest(session, bot, date)`.
- Alembic migration: add `Digest` table `(id, date, text, posted_message_id, created_at)`.
- Add job to `start_scheduler()`: `cron` at `hour=23, minute=0`.
- FastAPI route `web/routes/digest.py` — list page at `/digest`.
- Jinja2 template `web/templates/digest.html`.
- Tests: freeze time, mock LLM, verify `Digest` row inserted.

**Sprint D — auto-wiki**
- Alembic migration: add `WikiEntry` table `(id, title, body, vector VECTOR(384), source_message_ids JSON, updated_at)`.
- `bot/services/wiki.py` — `extract_wiki_entries(session)` clusters recent embeddings, calls LLM for concept summaries, upserts entries.
- Add job to `start_scheduler()`: `cron` at `day_of_week=sun, hour=9, minute=0`.
- FastAPI routes `web/routes/wiki.py` — list + detail pages.
- Jinja2 templates `web/templates/wiki_list.html`, `web/templates/wiki_detail.html`.

**Sprint E — hardening**
- Cost observability: add `llm_calls` and `embedding_calls` counters (simple Postgres table or Prometheus if owner adds metrics). Dashboard widget in `web/routes/dashboard.py`.
- Per-user opt-in toggle: `/privacy` command in a new `bot/handlers/privacy.py`. Soft-delete column `is_ai_excluded BOOLEAN DEFAULT FALSE` on `users` table. Exclude opted-out users from retrieval.
- Deletion tracking: `deleted_at` soft-delete column on `chat_messages`. Handler in `bot/handlers/chat_messages.py` to set `deleted_at` on `message_edit` with empty text or explicit deletion event. Exclude from retrieval.

---

## 6. Decisions Owner Should Make Before Sprint A

**(a) LLM backend.** Options in priority order:
1. Codex CLI subprocess (uses existing Codex subscription, ~$0 per call) — see §7 for the exact subprocess pattern.
2. Claude Code CLI subprocess (uses Claude subscription, same subprocess pattern, swap `"codex"` for `"claude"` in the exec args).
3. API key (Anthropic or OpenAI) — simpler async code, pay-per-token, more reliable under concurrent load.

If concurrent digest + mention answering is needed, the subprocess pattern serializes calls. For high concurrency, an API key is cleaner.

**(b) Embedding provider.** Local (`sentence-transformers` + `intfloat/multilingual-e5-small`) adds ~300 MB to Docker image and ~10ms/embed on CPU — acceptable for a batch backfill, borderline for real-time per-message embedding. OpenAI `text-embedding-3-small` at 1536 dimensions costs ~$0.06 for the full 3,109-message backfill and ~$0.002/month going forward. For a community of this size, either works.

**(c) Privacy model.** Two reasonable defaults:
- **Opt-out (default-yes):** all messages included unless user runs `/privacy off`. Simpler to start, broader coverage. Reasonable for an AI-native cohort.
- **Opt-in (default-no):** no message included until user runs `/privacy on`. More conservative; coverage grows slowly. Recommended if community has hesitant members.

**(d) Deletion respect.** Add a `deleted_at` column to `chat_messages` from Sprint A (4 hours of work: migration + handler stub). If deferred to Sprint E, retroactive deletion requests require a backfill. Do it early.

---

## 7. Hermes-Style Subprocess Pattern for Codex/Claude CLI

When using the CLI as an LLM backend inside an async aiogram/FastAPI service, use `asyncio.create_subprocess_exec` to avoid blocking the event loop.

```python
# bot/services/llm.py (sketch — not yet implemented)
import asyncio
import logging

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


async def codex_complete(system: str, user: str, timeout_s: int = 120) -> str:
    """Call Codex CLI as a subprocess. Requires codex on PATH and ~/.codex/auth.json."""
    prompt = f"[SYSTEM]\n{system}\n\n[USER]\n{user}\n"
    proc = await asyncio.create_subprocess_exec(
        "codex", "exec", "--full-auto",
        "-c", "model=gpt-5.5",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"codex timed out after {timeout_s}s")
    if proc.returncode != 0:
        raise LLMError(f"codex exited {proc.returncode}: {stderr.decode()[:500]}")
    return stdout.decode().strip()
```

**Deployment note (Coolify):** Mount Codex auth into the bot container via `custom_docker_run_options` in Coolify — the same mechanism used today for `credentials.json` (`docs/runbook.md:104`):

```
-v /root/.codex:/home/app/.codex:ro
```

Adjust the host path to wherever the VPS user's Codex auth lives (`/root/.codex` or `/home/claw/.codex`). For Claude CLI, the equivalent is `~/.claude/credentials.json` — same `-v` pattern.

---

## 8. Estimates

- **Owner effort:** 2 evenings per sprint × 5 sprints = 10 evenings, roughly 2-3 weeks of part-time work. Autonomous assistance (e.g., having Claude Code write the boilerplate for a sprint) could compress each sprint to 1 evening.
- **One-time backfill cost:** ~$0.06 if using OpenAI `text-embedding-3-small` for 3,109 messages; ~$0 if using local embeddings.
- **Ongoing generation cost:** ~$0 if using Codex or Claude subscription via subprocess; API costs depend on usage — at 50 LLM calls/day (digest + typical mention volume) with `gpt-4o-mini`, ~$1/month.
- **Latency:** retrieval from pgvector at 3,109 vectors is <5ms on an IVFFlat index; LLM generation via Codex CLI adds 5-30s depending on prompt length. Mention replies will feel slow if using CLI — consider a "thinking..." intermediate reply while the subprocess runs.
- **Infrastructure cost:** $0 additional. pgvector runs in the existing Postgres instance.

---

## 9. What This Proposal Does Not Include

These items are explicitly out of scope for this document — they are owner decisions:

- **pgvector migration code** — the SQL is straightforward (`CREATE EXTENSION vector; ALTER TABLE ...`), but writing and merging Alembic migrations is owner's call on timing. A bad migration still causes a boot loop (see `CLAUDE.md` P1 debt item).
- **Provider client code** — no `openai`, `anthropic`, or `voyage` client is written here. The `EmbeddingClient` interface sketch above is illustrative; owner picks the library and writes the concrete implementation.
- **Prompt templates** — digest summaries, wiki extraction prompts, and RAG system prompts all encode the community's voice. That is a product decision, not a technical one.
- **UI/UX for `/wiki` and `/digest` archive** — the FastAPI admin already uses Bootstrap 5 CDN with no build step. Page layout, navigation, and styling are owner's design preferences.

---

## 10. Invitation

Happy to contribute PRs for any of Sprints A-E. If useful, tag on a draft PR or open an issue referencing this doc with which sprint you're starting — we can produce a Sprint A PR within a day once the LLM backend and embedding provider decisions are made.

<!-- updated-by-superflow:2026-04-25 -->
