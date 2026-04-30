# Phase 4 — Stream B — Search Service (T4-02) — Copy-Paste Prompt

You are an autonomous agent working on **Stream B** of Memory System Phase 4 in the Shkoderbot repo. Your job is to implement the FTS-based, governance- and tombstone-filtered search service that Stream D's Q&A handler will consume.

GitHub issue: **#146** — `T4-02: Search service — governance + tombstone-filtered FTS (Stream B)`.

This is the longest-pole stream of Phase 4. Plan accordingly.

---

## 0. Setup

```bash
cd /Users/eekudryavtsev/Vibe/products/shkoderbot
git fetch --all --prune
# Wait until Stream A (T4-01, #145), Stream C (T4-03, #147), and Stream E (T4-05, #149)
# are merged on origin/main. Verify:
git log --oneline origin/main | head -10
# You should see commits referencing T4-01, T4-03, T4-05.
git worktree add .worktrees/p4-stream-b -b feat/p4-stream-b-search-service main
cd .worktrees/p4-stream-b
```

If those streams are not yet merged, STOP and surface the issue. Stream B is Wave 2; it does not start until Wave 1 finishes.

Verify isolation: `git branch --show-current` is `feat/p4-stream-b-search-service`. Latest commit includes Stream A's migration 020.

---

## 1. Required Reading (in order)

1. `docs/memory-system/HANDOFF.md` §1 invariants 1-10 verbatim.
2. `docs/memory-system/HANDOFF.md` §Phase 4 — Acceptance line: *cites message_version_id; excludes forbidden content; refuses no evidence*. This is your contract.
3. `docs/memory-system/HANDOFF.md` §Phase 5 boundary — confirm you do NOT add LLM/vector/embedding code.
4. `docs/memory-system/HANDOFF.md` §10 — tombstone key formats. You'll need three of the four.
5. `docs/memory-system/AUTHORIZED_SCOPE.md`.
6. `docs/memory-system/PHASE4_PLAN.md` §5.B — your component spec (SQL provided verbatim).
7. `bot/db/models.py` — `MessageVersion`, `ChatMessage`, `ForgetEvent`, `User`. Read all four.
8. `bot/services/forget_cascade.py` — current `fts_rows` layer is `{status: 'skipped', reason: 'table_not_exists'}`. You must rewrite it to actually purge / null search content.
9. `bot/db/repos/message_version.py` — repo pattern (flush, no commit; static methods).
10. `bot/db/repos/forget_event.py` (if it exists) — read for patterns.
11. `tests/services/test_forget_cascade.py` — extend, do not regress.
12. `bot/services/normalization.py` — confirm `normalized_text` is what indexed in Stream A's tsvector.

---

## 2. Six Invariants (verbatim)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.

For Stream B specifically:
- **Invariant 3** is the dominant risk. Your WHERE clause is the gate. Test it exhaustively. The defense-in-depth filter triple is non-negotiable: `chat_messages.memory_policy='normal'` AND `chat_messages.is_redacted=false` AND `message_versions.is_redacted=false`.
- **Invariant 9** — tombstone NOT EXISTS clause must check three key formats.
- **Invariant 4** — `SearchHit.message_version_id` is the citation primitive.
- **Invariant 2** — no LLM imports.

---

## 3. Self-Verify Rule

Codex / external reviewers occasionally hallucinate citations. ALWAYS:

- Re-read actual files before quoting file:line.
- Memory recall is a hypothesis, not a fact.
- Verify with Grep before depending on a function/file existing.

---

## 4. Implementation Plan

### Step 4.1 — `bot/services/search.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SearchHit:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime


_QUERY_MAX_LEN = 256


async def search_messages(
    session: AsyncSession,
    query: str,
    *,
    chat_id: int,
    limit: int = 3,
    headline_max_words: int = 35,
) -> list[SearchHit]:
    q = (query or "").strip()
    if not q:
        return []
    if len(q) > _QUERY_MAX_LEN:
        q = q[:_QUERY_MAX_LEN]
    sql = text(
        """
        SELECT
          mv.id              AS message_version_id,
          mv.chat_message_id AS chat_message_id,
          cm.chat_id         AS chat_id,
          cm.message_id      AS message_id,
          cm.user_id         AS user_id,
          ts_headline(
            'russian',
            coalesce(mv.normalized_text, '') || ' ' || coalesce(mv.caption, ''),
            plainto_tsquery('russian', :q),
            'MaxWords=' || :max_words || ',MinWords=10,ShortWord=2,HighlightAll=false'
          )                  AS snippet,
          ts_rank_cd(mv.search_tsv, plainto_tsquery('russian', :q)) AS ts_rank,
          mv.captured_at     AS captured_at,
          cm.date            AS message_date
        FROM message_versions mv
        JOIN chat_messages cm
          ON cm.id = mv.chat_message_id
         AND cm.current_version_id = mv.id
        WHERE
          cm.chat_id = :chat_id
          AND cm.memory_policy = 'normal'
          AND cm.is_redacted = false
          AND mv.is_redacted = false
          AND mv.search_tsv @@ plainto_tsquery('russian', :q)
          AND NOT EXISTS (
            SELECT 1 FROM forget_events fe
            WHERE fe.tombstone_key IN (
              'message:' || cm.chat_id || ':' || cm.message_id,
              'message_hash:' || coalesce(cm.content_hash, ''),
              'user:' || coalesce(cm.user_id::text, '')
            )
            AND fe.status IN ('pending', 'processing', 'completed')
          )
        ORDER BY ts_rank DESC, captured_at DESC
        LIMIT :limit
        """
    )
    result = await session.execute(
        sql, {"q": q, "chat_id": chat_id, "limit": limit, "max_words": headline_max_words}
    )
    rows = result.mappings().all()
    return [SearchHit(**row) for row in rows]
```

### Step 4.2 — Cascade integration `bot/services/forget_cascade.py`

Replace the `fts_rows` layer skipped status. The cascade for a `forget_event` of `target_type='message'` should:

- Look up the affected `message_versions` rows via `chat_messages` JOIN.
- Either DELETE those `message_versions` rows OR set `text=NULL, caption=NULL, normalized_text=NULL, is_redacted=true` (the offrecord redaction pattern is established — mirror it).
- Decision: **null-content + is_redacted=true** matches the offrecord redaction pattern from Phase 1, keeps `chat_messages` rows intact for FK integrity, and makes `to_tsvector('russian', '')` produce empty `search_tsv` (the generated column auto-recomputes on UPDATE).
- Record `{layer: 'fts_rows', status: 'completed', rows_affected: N}` in `cascade_status`.

Verify the existing cascade ordering and that you only touch the `fts_rows` slot.

### Step 4.3 — Unit tests `tests/services/test_search.py` (new file)

9 cases per PHASE4_PLAN.md §5.B:

1. Three rows (normal / nomem / offrecord) → only normal.
2. Russian stemmer match: insert "лошадь скачет" → search "лошади" matches.
3. forget_events `message:<chat_id>:<message_id>` → excluded.
4. `chat_messages.is_redacted=true` → excluded.
5. `message_versions.is_redacted=true` → excluded.
6. > 100 rows → respects limit, ts_rank desc, captured_at desc tiebreaker.
7. Snippet contains query token.
8. Empty / whitespace query → `[]`, no SQL executed.
9. Injection attempt `'; DROP TABLE chat_messages; --` → safely passed through plainto_tsquery, no error.

### Step 4.4 — Cascade integration test

Extend `tests/services/test_forget_cascade.py`:

- Insert a chat_message + message_version with searchable text.
- Insert a forget_event for that message.
- Run the cascade worker.
- Assert `search_messages(...)` returns empty for that chat.
- Assert `cascade_status` includes `{layer: 'fts_rows', status: 'completed'}`.

---

## 5. Test Commands

```bash
$TIMEOUT_CMD 120 alembic upgrade head
$TIMEOUT_CMD 120 pytest -x --timeout=120 tests/services/test_search.py tests/services/test_forget_cascade.py
ruff check .
mypy bot/services/search.py bot/services/forget_cascade.py
```

Performance smoke (informational, not a gate):

```bash
$TIMEOUT_CMD 60 pytest -x --timeout=60 -k smoke_search_perf
```

---

## 6. PR Workflow

```bash
git add bot/services/search.py \
        bot/services/forget_cascade.py \
        tests/services/test_search.py \
        tests/services/test_forget_cascade.py
git commit -m "feat(p4-B): search service + cascade fts_rows wiring (T4-02, #146)"
git push -u origin feat/p4-stream-b-search-service
gh pr create --label phase:4 \
  --title "feat(p4-B): search service — governance + tombstone-filtered FTS (T4-02)" \
  --body-file /tmp/p4-stream-b-pr-body.md
```

PR body must:
- Quote the 6 invariants.
- Reference issue #146.
- Reference PHASE4_PLAN.md §5.B and §5.A (cascade rationale).
- Paste pytest output (all 10 tests green).
- Note ts_rank ordering + captured_at tiebreaker is intentional.
- Confirm: no LLM / vector / embedding imports.

Unified review (2 reviewers):
1. Claude product reviewer (Agent subagent_type=`standard-product-reviewer`, run_in_background=true).
2. Secondary technical reviewer (Codex via plugin: `Agent(subagent_type="codex:codex-rescue")`).
3. Wait for both. Address NEEDS_FIXES / REQUEST_CHANGES.
4. CI: `gh run list -L 5`, `gh run watch <id>` foreground.
5. Green → `gh pr merge <num> --rebase --delete-branch`. **NEVER `--admin`.**

---

## 7. Stop Signals

- Test hangs > 120s → likely unmocked external call. Read the test, find the real call. Do NOT retry.
- Russian stemmer absent on staging → coordinate with Stream A; both migrations may need to switch to `'simple'`.
- Governance JOIN drops to seqscan on prod-shape data → flag in PR for index review.
- Cascade rewrite breaks an existing forget_cascade test → understand the test, do NOT delete it; bring the test up to the new contract or surface as design question.
- Anyone proposes adding LLM / vector / embedding code → STOP, Phase 5.
- `current_version_id IS NULL` for legacy rows → those are skipped silently; document in PR description, do not "fix" the legacy data here.

---

## 8. Definition of Done

- [ ] Issue #146 referenced in PR.
- [ ] All 9 unit tests in `tests/services/test_search.py` pass.
- [ ] Cascade integration test passes; `fts_rows` layer no longer `skipped`.
- [ ] `bot/services/search.py` mypy + ruff clean.
- [ ] No regressions in `tests/services/test_forget_cascade.py` existing cases.
- [ ] No LLM / vector / embedding imports introduced.
- [ ] PR merged via `gh pr merge --rebase --delete-branch`.
- [ ] `IMPLEMENTATION_STATUS.md` Phase 4 row updated (T4-02 → ✅).

---

## 9. Final Report

After merge, reply with:
- PR URL + merge SHA.
- Issue #146 closed.
- Files changed (4 expected).
- Any deviation from PHASE4_PLAN.md §5.B with rationale.
- Confirm "no LLM imports introduced".
- Cascade `fts_rows` semantics chosen (delete vs null-content; recommended: null-content + is_redacted=true).
