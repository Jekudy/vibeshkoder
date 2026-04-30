# Phase 4 — Stream C — Evidence Bundle (T4-03) — Copy-Paste Prompt

You are an autonomous agent working on **Stream C** of Memory System Phase 4 in the Shkoderbot repo. Your job is to ship the sealed `EvidenceBundle` contract that wraps top-N `SearchHit`s for downstream consumers. Q&A handler (Stream D) will consume it now; the future Phase 5 LLM gateway will consume the same shape.

GitHub issue: **#147** — `T4-03: Evidence bundle — sealed contract for top-N evidence (Stream C)`.

This is a Wave 1 stream — runs in parallel with Stream A and Stream E. No DB changes. Pure Python contracts + tests.

---

## 0. Setup

```bash
cd /Users/eekudryavtsev/Vibe/products/shkoderbot
git fetch --all --prune
git worktree add .worktrees/p4-stream-c -b feat/p4-stream-c-evidence-bundle main
cd .worktrees/p4-stream-c
```

Verify: `git branch --show-current` is `feat/p4-stream-c-evidence-bundle`. Latest commit equals `origin/main`.

You do NOT depend on Stream A or Stream E. You may run in parallel.

---

## 1. Required Reading (in order)

1. `docs/memory-system/HANDOFF.md` §1 invariants 1-10.
2. `docs/memory-system/HANDOFF.md` §Phase 4 — Acceptance line.
3. `docs/memory-system/HANDOFF.md` §Phase 5 boundary — confirm bundle is consumed by future LLM gateway, NOT producing one here.
4. `docs/memory-system/AUTHORIZED_SCOPE.md`.
5. `docs/memory-system/PHASE4_PLAN.md` §5.C — your component spec.
6. `bot/services/normalization.py` (~50 lines) — read pattern for frozen dataclass usage if any exists.
7. `bot/db/models.py` — type sketch for `MessageVersion`, `ChatMessage` (you don't import these but the contract mirrors their identifiers).

You do **not** need to read Stream B's search service code. The contract here is forward-compatible: you accept any `Sequence` of objects with the right attributes (duck-typed `SearchHit`).

---

## 2. Six Invariants (verbatim)

1. Existing gatekeeper must not break.
2. No LLM calls outside `llm_gateway`.
3. No extraction / search / q&a over `#nomem` / `#offrecord` / forgotten.
4. Citations point to `message_version_id` or approved card sources.
8. Import apply must go through the same normalization / governance path as live updates.
9. Tombstones are durable and not casually rolled back.

For Stream C specifically:
- **Invariant 3** — `EvidenceBundle.from_hits` performs NO de-novo DB lookup. Its only inputs are `SearchHit`s already governance-filtered by Stream B. This is a contract test, not a runtime check.
- **Invariant 4** — `EvidenceItem.message_version_id` is the citation primitive. The bundle's `evidence_ids` property returns `list[message_version_id]`.
- **Invariant 2** — no LLM imports in this module or its tests.

---

## 3. Self-Verify Rule

Same as other streams: re-read files before quoting; treat memory recall as hypothesis; verify with Grep.

---

## 4. Implementation Plan

### Step 4.1 — Search shape stub (optional, only if Stream B not yet shipping)

If Stream B has not landed `SearchHit` yet, create `bot/services/search_types.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


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
```

Stream B's PR can later move this canonical shape into `bot/services/search.py` and re-export from `search_types.py` for backwards compatibility.

If Stream B has already merged, import from `bot/services/search.py` directly.

### Step 4.2 — `bot/services/evidence.py`

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

# Forward-compatible import:
try:
    from bot.services.search import SearchHit  # type: ignore
except ImportError:
    from bot.services.search_types import SearchHit  # type: ignore


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    message_version_id: int
    chat_message_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    snippet: str
    ts_rank: float
    captured_at: datetime
    message_date: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "message_version_id": self.message_version_id,
            "chat_message_id": self.chat_message_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "snippet": self.snippet,
            "ts_rank": self.ts_rank,
            "captured_at": self.captured_at.isoformat(),
            "message_date": self.message_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    query: str
    chat_id: int
    items: tuple[EvidenceItem, ...]
    abstained: bool
    created_at: datetime

    @classmethod
    def from_hits(
        cls,
        query: str,
        chat_id: int,
        hits: Sequence[SearchHit],
    ) -> "EvidenceBundle":
        items = tuple(
            EvidenceItem(
                message_version_id=h.message_version_id,
                chat_message_id=h.chat_message_id,
                chat_id=h.chat_id,
                message_id=h.message_id,
                user_id=h.user_id,
                snippet=h.snippet,
                ts_rank=h.ts_rank,
                captured_at=h.captured_at,
                message_date=h.message_date,
            )
            for h in hits
        )
        return cls(
            query=query,
            chat_id=chat_id,
            items=items,
            abstained=(len(items) == 0),
            created_at=datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "chat_id": self.chat_id,
            "items": [item.to_dict() for item in self.items],
            "abstained": self.abstained,
            "created_at": self.created_at.isoformat(),
        }

    @property
    def evidence_ids(self) -> list[int]:
        return [item.message_version_id for item in self.items]
```

### Step 4.3 — Tests `tests/services/test_evidence.py`

```python
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.services.evidence import EvidenceBundle, EvidenceItem
try:
    from bot.services.search import SearchHit
except ImportError:
    from bot.services.search_types import SearchHit


def _make_hit(idx: int) -> SearchHit:
    ts = datetime(2026, 1, 1, 12, idx, tzinfo=timezone.utc)
    return SearchHit(
        message_version_id=100 + idx,
        chat_message_id=200 + idx,
        chat_id=-100123,
        message_id=300 + idx,
        user_id=42,
        snippet=f"<b>match</b>{idx}",
        ts_rank=0.5 - idx * 0.01,
        captured_at=ts,
        message_date=ts,
    )


def test_empty_hits_abstains():
    b = EvidenceBundle.from_hits("hello", -100123, [])
    assert b.abstained is True
    assert b.items == ()
    assert b.evidence_ids == []


def test_three_hits():
    hits = [_make_hit(i) for i in range(3)]
    b = EvidenceBundle.from_hits("hello", -100123, hits)
    assert b.abstained is False
    assert len(b.items) == 3
    assert b.evidence_ids == [100, 101, 102]


def test_frozen_bundle():
    b = EvidenceBundle.from_hits("x", 1, [])
    with pytest.raises(FrozenInstanceError):
        b.query = "mutated"  # type: ignore[misc]


def test_to_dict_round_trip():
    hits = [_make_hit(0)]
    b = EvidenceBundle.from_hits("hello", -100123, hits)
    encoded = json.dumps(b.to_dict())
    decoded = json.loads(encoded)
    assert decoded["abstained"] is False
    assert decoded["items"][0]["message_version_id"] == 100


def test_snapshot(tmp_path: Path):
    fixture_path = Path("tests/fixtures/evidence_bundle_v1.json")
    if not fixture_path.exists():
        pytest.skip("snapshot fixture not yet committed")
    expected = json.loads(fixture_path.read_text())
    hits = [_make_hit(i) for i in range(2)]
    b = EvidenceBundle.from_hits("hello", -100123, hits)
    actual = b.to_dict()
    actual.pop("created_at")
    expected.pop("created_at", None)
    assert actual == expected


def test_evidence_ids_preserves_order():
    hits = [_make_hit(2), _make_hit(0), _make_hit(1)]
    b = EvidenceBundle.from_hits("hello", -100123, hits)
    assert b.evidence_ids == [102, 100, 101]
```

### Step 4.4 — Snapshot fixture `tests/fixtures/evidence_bundle_v1.json`

Generate by running the test suite once locally with snapshot-write enabled, OR hand-write:

```json
{
  "query": "hello",
  "chat_id": -100123,
  "items": [
    {
      "message_version_id": 100,
      "chat_message_id": 200,
      "chat_id": -100123,
      "message_id": 300,
      "user_id": 42,
      "snippet": "<b>match</b>0",
      "ts_rank": 0.5,
      "captured_at": "2026-01-01T12:00:00+00:00",
      "message_date": "2026-01-01T12:00:00+00:00"
    },
    {
      "message_version_id": 101,
      "chat_message_id": 201,
      "chat_id": -100123,
      "message_id": 301,
      "user_id": 42,
      "snippet": "<b>match</b>1",
      "ts_rank": 0.49,
      "captured_at": "2026-01-01T12:01:00+00:00",
      "message_date": "2026-01-01T12:01:00+00:00"
    }
  ],
  "abstained": false
}
```

(Note: `created_at` removed from the snapshot since it's not deterministic; the test pops it before compare.)

---

## 5. Test Commands

```bash
$TIMEOUT_CMD 60 pytest -x --timeout=120 tests/services/test_evidence.py
ruff check .
mypy bot/services/evidence.py
```

All green. No external services touched.

---

## 6. PR Workflow

```bash
git add bot/services/evidence.py \
        bot/services/search_types.py \
        tests/services/test_evidence.py \
        tests/fixtures/evidence_bundle_v1.json
git commit -m "feat(p4-C): evidence bundle frozen contract (T4-03, #147)"
git push -u origin feat/p4-stream-c-evidence-bundle
gh pr create --label phase:4 \
  --title "feat(p4-C): evidence bundle — sealed contract (T4-03)" \
  --body-file /tmp/p4-stream-c-pr-body.md
```

PR body:
- Quote 6 invariants.
- Reference #147 + PHASE4_PLAN.md §5.C.
- Note: snapshot fixture is the contract for Phase 5 LLM gateway consumption.
- Paste pytest output (6 tests green).
- Confirm: no LLM imports.

Unified review:
1. Claude product reviewer (background).
2. Codex/Claude technical reviewer.
3. Address feedback; re-review the flagging reviewer.
4. CI green → `gh pr merge <num> --rebase --delete-branch`. NEVER `--admin`.

---

## 7. Stop Signals

- Test hangs > 120s.
- Stream B's `SearchHit` shape diverges from your stub → adopt Stream B's authoritative shape; coordinate via PR comment.
- Snapshot fixture would need to embed real (non-synthetic) message content from forgotten/offrecord users → STOP, invariant #3.
- Anyone asks for "render this bundle as LLM context" → STOP, that's Phase 5; leave the contract pure.

---

## 8. Definition of Done

- [ ] Issue #147 referenced.
- [ ] All 6 pytest cases pass.
- [ ] Snapshot fixture committed.
- [ ] mypy + ruff clean.
- [ ] No LLM imports.
- [ ] PR merged via `--rebase --delete-branch`.
- [ ] `IMPLEMENTATION_STATUS.md` Phase 4 row updated (T4-03 → ✅).

---

## 9. Final Report

After merge:
- PR URL + merge SHA.
- Issue #147 closed.
- Files changed (3-4 expected).
- Decision on `search_types.py` stub vs direct import.
- Confirm "no LLM imports".
