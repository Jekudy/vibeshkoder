# Phase 4 — Stream E — qa_traces Audit Table (T4-05) — Copy-Paste Prompt

You are an autonomous agent working on **Stream E** of Memory System Phase 4. Your job is to ship the `qa_traces` audit schema + repo. Every `/recall` invocation will write to this table. Cascade-aware: `/forget_me` must NULL `query_text` for that user's traces (cascade wiring lives in Stream B; you deliver the schema and a skeleton test).

GitHub issue: **#149** — `T4-05: qa_traces audit table + repo (Stream E)`.

This is Wave 1 — runs in parallel with Stream A and Stream C. Migration ordering coordination with Stream A is the only constraint.

---

## 0. Setup

```bash
cd /Users/eekudryavtsev/Vibe/products/shkoderbot
git fetch --all --prune
git worktree add .worktrees/p4-stream-e -b feat/p4-stream-e-qa-traces main
cd .worktrees/p4-stream-e
```

Verify: `git branch --show-current` is `feat/p4-stream-e-qa-traces`. Latest commit equals `origin/main`.

**Migration ordering:** your migration is 021. Stream A's is 020. If Stream A has not yet merged when you push, your PR can still pass CI (CI applies all migrations sequentially), but you MUST hold the merge until Stream A is on `origin/main`. Rebase if needed.

---

## 1. Required Reading (in order)

1. `docs/memory-system/HANDOFF.md` §1 invariants 1-10.
2. `docs/memory-system/HANDOFF.md` §Phase 4 — T4-05 entry: *q&a traces*.
3. `docs/memory-system/HANDOFF.md` §10 — tombstone semantics, cascade order.
4. `docs/memory-system/AUTHORIZED_SCOPE.md`.
5. `docs/memory-system/PHASE4_PLAN.md` §5.E — your component spec.
6. `bot/db/models.py` — read pattern (e.g., `MessageVersion`, `IngestionRun`); mirror types and constraints.
7. `bot/db/repos/message_version.py` — repo pattern (`@staticmethod`, flushes, no commit).
8. `alembic/versions/019_add_ingestion_runs_rolled_back.py` — last migration; verify next is 020 (Stream A) then 021 (you).
9. `bot/services/forget_cascade.py` — cascade order; the worker must add `qa_traces` PII purge as a layer (wiring in Stream B; you deliver schema + skeleton test).

---

## 2. Six Invariants (verbatim)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten — `qa_traces.query_text` is NULL when `detect_policy(query) ≠ 'normal'`.
4. Citations point to `message_version_id` or approved card sources — `evidence_ids JSONB` is `list[message_version_id]`.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back — cascade for `/forget_me` must NULL `query_text` for the user's traces.

For Stream E specifically:
- **Invariant 3** — schema must support PII redaction at write time (`query_redacted=true, query_text=NULL`).
- **Invariant 9** — `/forget_me` cascade NULLs query_text. You deliver the schema; cascade wiring is Stream B's responsibility (your test can mark cascade case xfail until Stream B lands).

---

## 3. Self-Verify Rule

Same as other streams: re-read files; treat memory as hypothesis; verify with Grep.

Specifically: before assuming the `feature_flags` / `ingestion_runs` migration patterns, read at least one prior migration to confirm idiom. `down_revision` must be the EXACT revision id from migration 020 (or 019 if 020 hasn't merged when you author).

---

## 4. Implementation Plan

### Step 4.1 — Migration `alembic/versions/021_add_qa_traces.py`

```python
"""add qa_traces

Revision ID: 021_add_qa_traces
Revises: 020_add_message_version_fts_index
Create Date: 2026-04-30 ...

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "021_add_qa_traces"
down_revision = "020_add_message_version_fts_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_traces",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_tg_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("query_redacted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("abstained", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_qa_traces_user_tg_id", "qa_traces", ["user_tg_id"])
    op.create_index(
        "ix_qa_traces_chat_id_created_at", "qa_traces", ["chat_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_qa_traces_chat_id_created_at", table_name="qa_traces")
    op.drop_index("ix_qa_traces_user_tg_id", table_name="qa_traces")
    op.drop_table("qa_traces")
```

**If Stream A's migration 020 has not merged when you author:** set `down_revision = "019_add_ingestion_runs_rolled_back"` temporarily, then rebase to point at `"020_add_message_version_fts_index"` once Stream A merges. Coordinate via PR comment.

### Step 4.2 — Model `bot/db/models.py`

Add `QaTrace`:

```python
class QaTrace(Base):
    __tablename__ = "qa_traces"

    id = sa.Column(sa.BigInteger, primary_key=True, autoincrement=True)
    user_tg_id = sa.Column(sa.BigInteger, nullable=False)
    chat_id = sa.Column(sa.BigInteger, nullable=False)
    query_redacted = sa.Column(sa.Boolean, nullable=False, default=False)
    query_text = sa.Column(sa.Text, nullable=True)
    evidence_ids = sa.Column(JSONB, nullable=False, default=list)
    abstained = sa.Column(sa.Boolean, nullable=False, default=False)
    created_at = sa.Column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    __table_args__ = (
        sa.Index("ix_qa_traces_user_tg_id", "user_tg_id"),
        sa.Index("ix_qa_traces_chat_id_created_at", "chat_id", "created_at"),
    )
```

(Use the existing `JSONB` import alias if the file already imports it.)

### Step 4.3 — Repo `bot/db/repos/qa_trace.py`

```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import QaTrace


class QaTraceRepo:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_tg_id: int,
        chat_id: int,
        query: str,
        evidence_ids: list[int],
        abstained: bool,
        redact_query: bool,
    ) -> QaTrace:
        trace = QaTrace(
            user_tg_id=user_tg_id,
            chat_id=chat_id,
            query_redacted=redact_query,
            query_text=None if redact_query else query,
            evidence_ids=list(evidence_ids),
            abstained=abstained,
        )
        session.add(trace)
        await session.flush()
        return trace
```

(Caller commits — same pattern as `MessageVersionRepo`.)

### Step 4.4 — Tests `tests/db/test_qa_trace.py`

```python
import pytest

from bot.db.repos.qa_trace import QaTraceRepo


@pytest.mark.asyncio
async def test_create_with_query_text(session):
    trace = await QaTraceRepo.create(
        session,
        user_tg_id=42,
        chat_id=-100123,
        query="hello world",
        evidence_ids=[100, 101],
        abstained=False,
        redact_query=False,
    )
    await session.flush()
    assert trace.query_text == "hello world"
    assert trace.query_redacted is False
    assert trace.evidence_ids == [100, 101]
    assert trace.abstained is False


@pytest.mark.asyncio
async def test_create_with_redacted_query(session):
    trace = await QaTraceRepo.create(
        session,
        user_tg_id=42,
        chat_id=-100123,
        query="some secret query",
        evidence_ids=[],
        abstained=True,
        redact_query=True,
    )
    await session.flush()
    assert trace.query_text is None
    assert trace.query_redacted is True
    assert trace.evidence_ids == []
    assert trace.abstained is True


@pytest.mark.asyncio
async def test_evidence_ids_jsonb_round_trip(session):
    ids = [1, 2, 3, 100, 999]
    trace = await QaTraceRepo.create(
        session,
        user_tg_id=42,
        chat_id=-100123,
        query="x",
        evidence_ids=ids,
        abstained=False,
        redact_query=False,
    )
    await session.flush()
    fetched = await session.get(type(trace), trace.id)
    assert fetched.evidence_ids == ids


@pytest.mark.xfail(reason="cascade wiring lives in Stream B / T4-02; flip to passing once #146 merges")
@pytest.mark.asyncio
async def test_forget_me_cascade_redacts_query(session):
    trace = await QaTraceRepo.create(
        session,
        user_tg_id=42,
        chat_id=-100123,
        query="my private question",
        evidence_ids=[100],
        abstained=False,
        redact_query=False,
    )
    await session.flush()
    # Simulate /forget_me cascade for user 42:
    from bot.services.forget_cascade import process_forget_for_user  # noqa
    await process_forget_for_user(session, user_tg_id=42)
    refetch = await session.get(type(trace), trace.id)
    assert refetch.query_text is None
    assert refetch.query_redacted is True
```

The 4th test is xfail until Stream B's cascade wiring is in. Stream B will flip it to passing.

---

## 5. Test Commands

```bash
$TIMEOUT_CMD 120 alembic upgrade head
$TIMEOUT_CMD 120 pytest -x --timeout=120 tests/db/test_qa_trace.py
ruff check .
mypy bot/db/repos/qa_trace.py
```

All green except the xfail (which counts as passing in pytest).

---

## 6. PR Workflow

```bash
git add alembic/versions/021_add_qa_traces.py \
        bot/db/models.py \
        bot/db/repos/qa_trace.py \
        tests/db/test_qa_trace.py
git commit -m "feat(p4-E): qa_traces audit table + repo (T4-05, #149)"
git push -u origin feat/p4-stream-e-qa-traces
gh pr create --label phase:4 \
  --title "feat(p4-E): qa_traces audit table + repo (T4-05)" \
  --body-file /tmp/p4-stream-e-pr-body.md
```

PR body:
- Quote 6 invariants.
- Reference #149 + PHASE4_PLAN.md §5.E.
- Paste pytest output.
- Note: cascade wiring is Stream B (T4-02 / #146); the 4th test is xfail until then.
- Confirm: no LLM imports.
- Migration ordering: `down_revision = "020_add_message_version_fts_index"` (after Stream A merged) OR `"019_add_ingestion_runs_rolled_back"` if author-time Stream A unmerged (rebase later).

Unified review:
1. Claude product reviewer (background) — focus: schema correctness, redaction semantics.
2. Codex/Claude technical reviewer — focus: down_revision pointer correctness, JSONB defaults, index choice.
3. Address feedback.
4. CI green → `gh pr merge <num> --rebase --delete-branch`. NEVER `--admin`.

---

## 7. Stop Signals

- Migration number 021 already taken on `origin/main` between fetch and push → bump to next free, update PHASE4_PLAN.md.
- Test hangs > 120s.
- Stream A delays past 1-2 days → coordinate. Your PR can be authored against 019 and rebased.
- `/forget_me` cascade contract differs from your test expectation → understand cascade design (Stream B owns); update xfail comment, do NOT silently change schema.

---

## 8. Definition of Done

- [ ] Issue #149 referenced in PR.
- [ ] Migration 021 applies and rolls back clean.
- [ ] `qa_traces` table + 2 indexes exist.
- [ ] All 4 repo tests pass (with xfail on the cascade case until Stream B merges).
- [ ] mypy + ruff clean.
- [ ] No LLM imports.
- [ ] PR merged via `--rebase --delete-branch` AFTER Stream A's PR.
- [ ] `IMPLEMENTATION_STATUS.md` Phase 4 row updated (T4-05 → ✅).

---

## 9. Final Report

After merge:
- PR URL + merge SHA.
- Issue #149 closed.
- Files changed (4 expected).
- Migration ordering note (whether you rebased or had to bump).
- Confirm "no LLM imports".
