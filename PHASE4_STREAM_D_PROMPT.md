# Phase 4 — Stream D — Q&A Handler `/recall` + Eval Cases (T4-04 + T4-06) — Copy-Paste Prompt

You are an autonomous agent working on **Stream D** of Memory System Phase 4. Your job is to ship the user-facing `/recall <query>` command, the `memory.qa.enabled` feature flag wiring, and ≥ 10 eval seed cases. This is the final stream and the most user-facing — extra care on authorization and PII handling.

GitHub issues:
- **#148** — `T4-04: Q&A handler /recall + memory.qa.enabled flag (Stream D)`
- **#150** — `T4-06: Eval seed cases for /recall (≥ 10 cases, rolled into Stream D)`

This is Wave 3. It depends on Streams B (#146), C (#147), and E (#149) all merged.

---

## 0. Setup

```bash
cd /Users/eekudryavtsev/Vibe/products/shkoderbot
git fetch --all --prune
# Verify Streams B, C, E merged on origin/main:
git log --oneline origin/main | head -10
# Look for commits referencing T4-02 (#146), T4-03 (#147), T4-05 (#149).
git worktree add .worktrees/p4-stream-d -b feat/p4-stream-d-qa-handler main
cd .worktrees/p4-stream-d
```

If any dependency stream is missing, STOP. Surface the issue.

Verify: `git branch --show-current` is `feat/p4-stream-d-qa-handler`. Latest `origin/main` includes Streams B/C/E.

---

## 1. Required Reading (in order)

1. `docs/memory-system/HANDOFF.md` §1 invariants 1-10.
2. `docs/memory-system/HANDOFF.md` §Phase 4 — Acceptance line: *cites message_version_id; excludes forbidden content; refuses no evidence*. Also §Phase 4 Risks: *hallucination if LLM used too early*. Also §Phase 4 Rollback: *q&a feature flag off*.
3. `docs/memory-system/HANDOFF.md` §Phase 5 boundary — confirm no LLM here.
4. `docs/memory-system/HANDOFF.md` §8 Feature flags — `memory.qa.enabled`.
5. `docs/memory-system/AUTHORIZED_SCOPE.md`.
6. `docs/memory-system/PHASE4_PLAN.md` §5.D — your component spec.
7. `bot/handlers/chat_messages.py` — handler pattern (advisory lock, governance, repos, no LLM).
8. `bot/services/feature_flag.py` (or wherever `is_enabled` lives) — flag check pattern.
9. `bot/services/governance.py` — `detect_policy(...)` signature; you call it on the user's QUERY.
10. `bot/services/search.py` (Stream B) — `search_messages(...)` API.
11. `bot/services/evidence.py` (Stream C) — `EvidenceBundle.from_hits(...)`.
12. `bot/db/repos/qa_trace.py` (Stream E) — `QaTraceRepo.create(...)`.
13. `bot/db/repos/user.py` — for member/admin lookup if not already done in middleware.
14. `bot/__main__.py` — router registration pattern.
15. `tests/handlers/` — existing test structure for handlers.

---

## 2. Six Invariants (verbatim)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.

For Stream D specifically:
- **Invariant 2** — handler MUST NOT import any LLM library, anthropic, openai, ollama, huggingface, transformers, langchain, etc. Add a CI grep gate if helpful.
- **Invariant 3** — query echo on `detect_policy(query) ≠ 'normal'` is forbidden. Audit row redacts query.
- **Invariant 4** — every cited evidence in the response carries `message_version_id` (in the deep link or in a footnote).
- **Invariant 1** — feature flag default OFF; existing handlers must not behave differently when flag is off.

---

## 3. Self-Verify Rule

Same as other streams: re-read files, treat memory recall as hypothesis, verify with Grep.

Specifically: before assuming a helper exists (`is_member_or_admin`, `feature_flag.is_enabled`, deep-link formatting), `Grep -n` for the function name across `bot/`. Hallucinated helpers waste a review cycle.

---

## 4. Implementation Plan

### Step 4.1 — `bot/services/qa.py` (orchestration)

```python
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.evidence import EvidenceBundle
from bot.services.search import search_messages


@dataclass(frozen=True)
class QaResult:
    bundle: EvidenceBundle
    query_redacted: bool


async def run_qa(
    session: AsyncSession,
    *,
    query: str,
    chat_id: int,
    redact_query_in_audit: bool,
    limit: int = 3,
) -> QaResult:
    hits = await search_messages(session, query, chat_id=chat_id, limit=limit)
    bundle = EvidenceBundle.from_hits(query, chat_id, hits)
    return QaResult(bundle=bundle, query_redacted=redact_query_in_audit)
```

### Step 4.2 — `bot/handlers/qa.py`

```python
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.repos.qa_trace import QaTraceRepo
from bot.db.repos.user import UserRepo
from bot.services.feature_flag import is_enabled
from bot.services.governance import detect_policy
from bot.services.qa import run_qa

router = Router()


def _short_chat_id(chat_id: int) -> str:
    """Telegram supergroup deep-link short form: strip -100 prefix."""
    s = str(chat_id)
    return s.removeprefix("-100") if s.startswith("-100") else s


def _format_response(bundle, users_by_id: dict[int, "User"]) -> str:
    if bundle.abstained:
        return "Не нашёл подходящих свидетельств в истории чата."
    parts = ["**Найденные свидетельства:**\n"]
    short = _short_chat_id(bundle.chat_id)
    for item in bundle.items:
        author = users_by_id.get(item.user_id, None)
        author_name = author.first_name if author else "—"
        date = item.message_date.strftime("%Y-%m-%d %H:%M")
        snippet = item.snippet.replace("<b>", "**").replace("</b>", "**")
        link = f"https://t.me/c/{short}/{item.message_id}"
        parts.append(f"> {snippet}\n> — _{author_name}, {date}_ · [→]({link})\n")
    return "\n".join(parts)


@router.message(Command("recall"))
async def recall_handler(message: Message, session: AsyncSession) -> None:
    # 1. Feature flag — silent return on OFF
    if not await is_enabled(session, "memory.qa.enabled"):
        return

    # 2. Authz: only in COMMUNITY_CHAT_ID group
    if message.chat.id != settings.COMMUNITY_CHAT_ID:
        if message.chat.type == "private":
            await message.reply("Команда /recall работает только в community чате.")
        return

    # 3. Authz: member or admin
    if message.from_user is None:
        return
    user = await UserRepo.get_by_id(session, message.from_user.id)
    if user is None or not (user.is_member or user.is_admin):
        await message.reply("Доступ только участникам сообщества.")
        return

    # 4. Parse query
    text_payload = message.text or ""
    query = text_payload.removeprefix("/recall").strip()
    if not query:
        await message.reply("Использование: `/recall <вопрос>`", parse_mode="Markdown")
        return

    # 5. Governance on the QUERY
    policy = detect_policy(text=query, caption=None)
    redact_query = policy != "normal"

    # 6. Run search + bundle
    result = await run_qa(
        session,
        query=query,
        chat_id=message.chat.id,
        redact_query_in_audit=redact_query,
    )

    # 7. Render response (NEVER echo query if redact_query)
    users_by_id: dict[int, "User"] = {}
    for item in result.bundle.items:
        if item.user_id and item.user_id not in users_by_id:
            u = await UserRepo.get_by_id(session, item.user_id)
            if u:
                users_by_id[item.user_id] = u
    response = _format_response(result.bundle, users_by_id)
    await message.reply(response, parse_mode="Markdown", disable_web_page_preview=True)

    # 8. Audit
    await QaTraceRepo.create(
        session,
        user_tg_id=message.from_user.id,
        chat_id=message.chat.id,
        query=query,
        evidence_ids=result.bundle.evidence_ids,
        abstained=result.bundle.abstained,
        redact_query=redact_query,
    )
    await session.commit()
```

### Step 4.3 — Wire in `bot/__main__.py`

Add the qa router to the dispatcher. Keep the registration unconditional; the handler self-checks the flag at runtime (supports rollout without restart).

### Step 4.4 — Tests `tests/handlers/test_qa.py`

8 cases per PHASE4_PLAN.md §5.D:

```python
import pytest

# (Use existing test fixtures: bot, dispatcher, session, message_factory, user_factory)


@pytest.mark.asyncio
async def test_flag_off_silent(...): ...
@pytest.mark.asyncio
async def test_dm_invocation_refuses(...): ...
@pytest.mark.asyncio
async def test_non_member_refuses(...): ...
@pytest.mark.asyncio
async def test_empty_query_usage_hint(...): ...
@pytest.mark.asyncio
async def test_member_with_results(...): ...
@pytest.mark.asyncio
async def test_member_no_results_abstains(...): ...
@pytest.mark.asyncio
async def test_offrecord_query_not_echoed(...): ...
@pytest.mark.asyncio
async def test_audit_row_written(...): ...
```

Each test asserts:
- The bot reply (or absence) matches expectation.
- `qa_traces` row exists with correct `query_redacted`, `evidence_ids`, `abstained`.
- The query string never appears in the response when `query_redacted=True`.

### Step 4.5 — Eval cases `tests/fixtures/qa_eval_cases.json` (T4-06)

≥ 10 cases. Schema per PHASE4_PLAN.md §5.D T4-06 subsection:

```json
[
  {
    "id": "rec_001",
    "category": "morphology",
    "query": "лошади",
    "fixture_messages": [
      {"chat_id": -100123, "message_id": 1, "user_id": 42,
       "normalized_text": "лошадь скачет по полю", "memory_policy": "normal"}
    ],
    "expected_evidence_present": true,
    "expected_chat_message_ids": [1],
    "notes": "Russian declension match via russian stemmer"
  },
  {
    "id": "rec_002",
    "category": "recency",
    "query": "встреча",
    "fixture_messages": [
      {"chat_id": -100123, "message_id": 1, "user_id": 42,
       "normalized_text": "встреча в субботу", "memory_policy": "normal",
       "captured_at": "2026-04-25T12:00:00+00:00"},
      {"chat_id": -100123, "message_id": 2, "user_id": 42,
       "normalized_text": "встреча в воскресенье", "memory_policy": "normal",
       "captured_at": "2026-04-29T12:00:00+00:00"}
    ],
    "expected_evidence_present": true,
    "expected_chat_message_ids": [2, 1],
    "notes": "Recency tiebreaker — newer message ranked first"
  },
  {
    "id": "rec_003",
    "category": "abstention",
    "query": "квантовая физика",
    "fixture_messages": [
      {"chat_id": -100123, "message_id": 1, "user_id": 42,
       "normalized_text": "обед в столовой", "memory_policy": "normal"}
    ],
    "expected_evidence_present": false,
    "expected_chat_message_ids": [],
    "notes": "No match → abstention"
  },
  {
    "id": "rec_004",
    "category": "governance",
    "query": "секрет",
    "fixture_messages": [
      {"chat_id": -100123, "message_id": 1, "user_id": 42,
       "normalized_text": "секрет проекта", "memory_policy": "offrecord"},
      {"chat_id": -100123, "message_id": 2, "user_id": 42,
       "normalized_text": "большой секрет", "memory_policy": "normal"}
    ],
    "expected_evidence_present": true,
    "expected_chat_message_ids": [2],
    "notes": "Offrecord row excluded; normal row returned"
  },
  {
    "id": "rec_005",
    "category": "tombstone",
    "query": "ошибка",
    "fixture_messages": [
      {"chat_id": -100123, "message_id": 1, "user_id": 42,
       "normalized_text": "ошибка в коде", "memory_policy": "normal"}
    ],
    "fixture_forget_events": [
      {"target_type": "message", "target_id": "-100123:1",
       "tombstone_key": "message:-100123:1", "status": "completed"}
    ],
    "expected_evidence_present": false,
    "expected_chat_message_ids": [],
    "notes": "Tombstone excludes message"
  }
]
```

Add 5+ more cases — combinations of categories, edge cases (caption-only match, mixed Cyrillic/Latin, multi-token query, very long query).

### Step 4.6 — Eval runner `tests/eval/test_qa_eval_cases.py`

```python
import json
from pathlib import Path

import pytest

from bot.services.qa import run_qa

EVAL_FIXTURE = Path("tests/fixtures/qa_eval_cases.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", json.loads(EVAL_FIXTURE.read_text()), ids=lambda c: c["id"])
async def test_eval_case(case, session, populate_chat):
    chat_id = case["fixture_messages"][0]["chat_id"]
    await populate_chat(case["fixture_messages"], case.get("fixture_forget_events", []))
    result = await run_qa(
        session,
        query=case["query"],
        chat_id=chat_id,
        redact_query_in_audit=False,
    )
    if case["expected_evidence_present"]:
        actual_ids = [item.message_id for item in result.bundle.items]
        assert actual_ids == case["expected_chat_message_ids"], (
            f"{case['id']}: expected {case['expected_chat_message_ids']}, got {actual_ids}"
        )
    else:
        assert result.bundle.abstained is True, f"{case['id']}: expected abstention"
```

Implement `populate_chat` fixture to create chat_messages, message_versions, and forget_events from the case JSON.

---

## 5. Test Commands

```bash
$TIMEOUT_CMD 120 pytest -x --timeout=120 tests/handlers/test_qa.py
$TIMEOUT_CMD 180 pytest -x --timeout=120 tests/eval/test_qa_eval_cases.py
ruff check .
mypy bot/handlers/qa.py bot/services/qa.py
```

Smoke test on dev bot (manual, optional):
- Set `memory.qa.enabled=false` → `/recall test` produces no reply.
- Set `memory.qa.enabled=true` → `/recall <real word>` returns evidence.

---

## 6. PR Workflow

```bash
git add bot/handlers/qa.py \
        bot/services/qa.py \
        bot/__main__.py \
        tests/handlers/test_qa.py \
        tests/eval/test_qa_eval_cases.py \
        tests/fixtures/qa_eval_cases.json
git commit -m "feat(p4-D): /recall handler + eval cases (T4-04, T4-06, #148, #150)"
git push -u origin feat/p4-stream-d-qa-handler
gh pr create --label phase:4 \
  --title "feat(p4-D): /recall handler + memory.qa.enabled flag + eval cases (T4-04, T4-06)" \
  --body-file /tmp/p4-stream-d-pr-body.md
```

PR body:
- Quote 6 invariants.
- Reference #148 + #150 + PHASE4_PLAN.md §5.D.
- Paste pytest + eval output.
- Confirm: feature flag default OFF.
- Confirm: no LLM imports (paste `grep -r "from anthropic\|from openai\|import openai" bot/` output, expect empty).

Unified review:
1. Claude product reviewer (background) — focus: user-flow correctness, abstention semantics, deep-link format.
2. Codex/Claude technical reviewer — focus: authz logic, race in feature flag check, query-redaction PII.
3. Address feedback.
4. CI green → `gh pr merge <num> --rebase --delete-branch`. NEVER `--admin`.

**Final Holistic Review (FHR):** because Phase 4 = 4-5 sprints with parallel execution, FHR is REQUIRED after this PR merges. Coordinate with the master design PR / next-step prompt.

---

## 7. Stop Signals

- Test hangs > 120s → unmocked external call.
- Telegram deep-link format does not work for the live supergroup → fall back to plain text "msg id N from <date>"; surface as design question.
- Authz logic conflicts with existing middleware (e.g., the `RawUpdatePersistenceMiddleware` runs only on a different filter) → use existing helper, do NOT duplicate.
- A test would require a real LLM call → STOP, redesign without.
- Eval case requires cross-chat search → out of Phase 4 scope.
- An eval case fails due to Stream B's ranking choice → coordinate via PR comment with Stream B owner; do NOT silently change ranking semantics.

---

## 8. Definition of Done

- [ ] Issues #148 and #150 referenced in PR.
- [ ] All 8 handler tests pass.
- [ ] All 10+ eval cases pass.
- [ ] Feature flag default OFF; smoke test confirms silent return.
- [ ] Telegram deep-link format verified on staging.
- [ ] mypy + ruff clean.
- [ ] No LLM imports.
- [ ] PR merged via `--rebase --delete-branch`.
- [ ] `IMPLEMENTATION_STATUS.md` Phase 4 row updated (T4-04, T4-06 → ✅).
- [ ] Final Holistic Review triggered (after this merge — see master plan).

---

## 9. Final Report

After merge:
- PR URL + merge SHA.
- Issues #148 #150 closed.
- Files changed (~6 expected).
- Eval results: 10/10 (or N/M with reasons for any failures).
- Confirm "no LLM imports".
- Note any deviations from PHASE4_PLAN.md §5.D.
- Trigger FHR by referencing #145..#150 in a single PR comment.
