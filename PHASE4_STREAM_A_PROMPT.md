# Phase 4 — Stream A — FTS Schema (T4-01) — Copy-Paste Prompt

You are an autonomous agent working on **Stream A** of Memory System Phase 4 in the Shkoderbot repo. Your job is to ship the FTS schema migration on `message_versions`. No search service, no handler — just the column + index + ORM update + tests.

GitHub issue: **#145** — `T4-01: FTS schema — tsvector + GIN on message_versions (Stream A)`.

---

## 0. Setup

```bash
cd /Users/eekudryavtsev/Vibe/products/shkoderbot
git fetch --all --prune
git worktree add .worktrees/p4-stream-a -b feat/p4-stream-a-fts-schema main
cd .worktrees/p4-stream-a
```

If the worktree already exists, `git pull origin main` and continue. If the branch already exists, ensure it is rebased on `origin/main` (not on Stream B/C/D/E branches).

Verify isolation: `git branch --show-current` should be `feat/p4-stream-a-fts-schema`. `git log --oneline -1` should be the same commit `main` is on.

---

## 1. Required Reading (in order)

You MUST read these before any code change. Use Read or dispatch a `deep-analyst` if context budget is tight.

1. `docs/memory-system/HANDOFF.md` §1 (Non-negotiable invariants 1-10) — quote them in your PR description.
2. `docs/memory-system/HANDOFF.md` §Phase 4 — Objective, Scope, Entry, Exit, Acceptance, Risks, Rollback.
3. `docs/memory-system/HANDOFF.md` §Phase 5 boundary — what NOT to build.
4. `docs/memory-system/AUTHORIZED_SCOPE.md` — confirm Phase 4 is authorized this cycle. Confirm vector search is **not** in scope.
5. `docs/memory-system/ROADMAP.md` — Phase 4 row + parallelization rules.
6. `docs/memory-system/IMPLEMENTATION_STATUS.md` — current state of Phase 1/2/3.
7. `docs/memory-system/PHASE4_PLAN.md` §5.A — your component spec.
8. `bot/db/models.py` — class `MessageVersion` (lines ~210-290).
9. `alembic/versions/019_add_ingestion_runs_rolled_back.py` — last migration. Verify next number = 020.
10. `bot/services/normalization.py` — confirm `normalized_text` semantics (input to your tsvector).
11. `tests/db/test_message_version_repo.py` — read once to understand fixture patterns; do NOT regress.

---

## 2. Six Invariants (verbatim — these MUST hold)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.

For Stream A specifically:
- **Invariant 1** is the dominant risk. The migration must NOT break any existing query path. Validate with the full unit-test suite, not just your new tests.
- **Invariant 3** is enforced by the search-time WHERE clause (Stream B), not the schema. The schema-layer decision (no partial index) is documented in PHASE4_PLAN.md §5.A; do NOT relitigate it here.

---

## 3. Self-Verify Rule (anti-hallucination)

Codex / external reviewers occasionally hallucinate file paths and line numbers. ALWAYS:

- Re-read the actual file before quoting a citation in PR description.
- If a memory or external review says "function X exists at file Y:line Z", verify with `Grep -n` or `Read file=Y offset=Z-2 limit=10` first.
- Treat memory recall as a hypothesis, not a fact.

This is a documented project rule (see `~/.claude/projects/.../memory/feedback-codex-hallucinated-citations.md`).

---

## 4. Implementation Plan

### Step 4.1 — Migration `alembic/versions/020_add_message_version_fts_index.py`

**down_revision = "019_add_ingestion_runs_rolled_back"** (verify the exact revision id by reading the file).

```python
"""add message_version fts index

Revision ID: 020_add_message_version_fts_index
Revises: 019_add_ingestion_runs_rolled_back
Create Date: 2026-04-30 ...

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "020_add_message_version_fts_index"
down_revision = "019_add_ingestion_runs_rolled_back"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE message_versions
        ADD COLUMN search_tsv tsvector
        GENERATED ALWAYS AS (
          to_tsvector('russian',
            coalesce(normalized_text, '') || ' ' || coalesce(caption, ''))
        ) STORED
        """
    )
    op.create_index(
        "ix_message_versions_search_tsv",
        "message_versions",
        ["search_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_message_versions_search_tsv", table_name="message_versions")
    op.drop_column("message_versions", "search_tsv")
```

Notes:
- The STORED generated column auto-populates for existing rows; no explicit backfill needed.
- Do NOT use `CONCURRENTLY` here — Alembic transaction wraps this DDL; concurrent index creation requires `with op.get_context().autocommit_block():` and is overkill for current dataset size.

### Step 4.2 — ORM update `bot/db/models.py`

Add to `MessageVersion`:

```python
from sqlalchemy.dialects.postgresql import TSVECTOR

class MessageVersion(Base):
    # ... existing columns ...

    search_tsv = sa.Column(
        TSVECTOR,
        sa.Computed(
            "to_tsvector('russian', coalesce(normalized_text, '') || ' ' || coalesce(caption, ''))",
            persisted=True,
        ),
        nullable=True,
    )
```

(Use the existing `sa` import alias; if the file uses bare imports, mirror that.)

### Step 4.3 — Tests `tests/db/test_fts_schema.py` (new file)

Three cases:

```python
import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_search_tsv_indexes_normalized_text(session, message_version_factory):
    mv = await message_version_factory(normalized_text="тестовое сообщение")
    await session.flush()
    result = await session.execute(
        text(
            "SELECT 1 FROM message_versions "
            "WHERE id = :id AND search_tsv @@ plainto_tsquery('russian', 'тест')"
        ),
        {"id": mv.id},
    )
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_search_tsv_indexes_caption(session, message_version_factory):
    mv = await message_version_factory(normalized_text=None, caption="русская подпись")
    await session.flush()
    result = await session.execute(
        text(
            "SELECT 1 FROM message_versions "
            "WHERE id = :id AND search_tsv @@ plainto_tsquery('russian', 'подпись')"
        ),
        {"id": mv.id},
    )
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_search_tsv_empty_when_no_content(session, message_version_factory):
    mv = await message_version_factory(normalized_text=None, caption=None)
    await session.flush()
    result = await session.execute(
        text("SELECT search_tsv::text FROM message_versions WHERE id = :id"),
        {"id": mv.id},
    )
    assert result.scalar() == ""  # empty tsvector renders as empty string
```

If `message_version_factory` doesn't exist, look in `tests/conftest.py` and `tests/db/test_message_version_repo.py` for the existing factory pattern, then mirror it.

---

## 5. Test Commands

Run sequentially. Paste output in PR description.

```bash
# Apply migration to a fresh test DB:
$TIMEOUT_CMD 120 alembic upgrade head

# Run all schema/migration regression tests:
$TIMEOUT_CMD 120 pytest -x --timeout=120 tests/db/test_fts_schema.py tests/db/test_message_version_repo.py

# Quality gates:
ruff check .
mypy bot/

# Down-migration test (manual or via test):
$TIMEOUT_CMD 60 alembic downgrade -1 && $TIMEOUT_CMD 60 alembic upgrade head
```

Acceptance: all green. Hangs > 120s = stop signal (likely unmocked fixture).

---

## 6. PR Workflow

```bash
git add alembic/versions/020_add_message_version_fts_index.py \
        bot/db/models.py \
        tests/db/test_fts_schema.py
git status   # confirm only intended files
git commit -m "feat(p4-A): FTS schema — tsvector + GIN on message_versions (T4-01, #145)"
git push -u origin feat/p4-stream-a-fts-schema

gh pr create --label phase:4 \
  --title "feat(p4-A): FTS schema — tsvector + GIN on message_versions (T4-01)" \
  --body-file /tmp/p4-stream-a-pr-body.md
```

PR body should:
- Quote the 6 invariants verbatim (proof you read them).
- Cite issue #145.
- Cite PHASE4_PLAN.md §5.A.
- Paste pytest output.
- Paste `\d message_versions` from staging post-migration.
- Note: governance filter is query-time, not partial index (see plan §5.A rationale).

After PR opens:
1. Wait for unified review (Claude product reviewer + Codex/Claude technical reviewer).
2. Fix any flagged issues; re-review the flagging reviewer.
3. Wait for CI: `gh run list -L 5` and `gh run watch <id>` (foreground; do NOT background-poll).
4. CI green → `gh pr merge <num> --rebase --delete-branch`. **NEVER `--admin`.**

---

## 7. Stop Signals

A stop signal means: surface the issue in PR description / draft PR comment, do NOT push past it autonomously.

- Russian stemmer not present in the postgres image → switch to `'simple'` and surface the trade-off; do not silently downgrade.
- Migration applies > 30s on staging-shape DB (informational; Phase 4 corpus is small but flag for review).
- Migration number 020 already taken on `origin/main` between fetch and push → bump to next free, update PHASE4_PLAN.md.
- A test hangs > 120s → likely unmocked external call; investigate the fixture, do not retry blindly.
- Cross-stream merge conflict on `bot/db/models.py` (Stream E also touches it) → coordinate via PR comments; resolve mechanically (both streams add a single isolated change).
- Anyone proposes adding LLM / vector / embedding code in this PR → STOP, this is Phase 5.

---

## 8. Definition of Done

- [ ] Issue #145 referenced in PR.
- [ ] Migration 020 applies and rolls back clean (manual + CI).
- [ ] `search_tsv` column exists, type tsvector, generated stored.
- [ ] GIN index `ix_message_versions_search_tsv` exists.
- [ ] All 3 pytest cases in `tests/db/test_fts_schema.py` pass.
- [ ] No regressions in `tests/db/test_message_version_repo.py`.
- [ ] ruff + mypy clean.
- [ ] PR merged via `gh pr merge --rebase --delete-branch`.
- [ ] `IMPLEMENTATION_STATUS.md` Phase 4 row updated (T4-01 → ✅).

Once merged, post a short note in the parent design PR thread (or on issue #145) so Stream B can unblock.

---

## 9. Final Report (after merge)

Reply with:
- PR URL + merge SHA.
- Issue # closed.
- Files changed (3 expected: migration, model, test).
- One-liner on any decisions deviating from PHASE4_PLAN.md §5.A.
- Confirm "no LLM imports introduced".

That's the whole stream. Self-contained. Go.
