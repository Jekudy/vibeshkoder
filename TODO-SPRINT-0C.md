# TODO before Sprint 0c promotion to main

This file lists findings from the Codex pre-promotion review (`aac6d1aa7fd5d3e7e`,
date 2026-05-11) that MUST be addressed before promoting this experiments branch
into Sprint 0c canonical paths.

The Sprint 0c orchestrator reads this file BEFORE moving any code into
`bot/services/` or `tests/` on `main`.

---

## HIGH severity (privacy invariant #9 binding — fix mandatory)

### Tombstone-format coverage gap

**Status: RESOLVED** — commit `6123e33` (2026-05-02).

**Problem was:** `bot/services/visibility_derivation.py:136-145` built only
`message_hash:{content_hash}` tombstone keys when scanning `forget_events`. Production
code creates THREE tombstone formats:

| Format | Production creator | What it forgets |
|--------|---------------------|-----------------|
| `message_hash:<sha256>` | `forget_cascade.py` content-redact path | Specific message content (current scope of visibility_derivation) |
| `message:<chat_id>:<message_id>` | `bot/handlers/forget_reply.py:135-145` | Specific message by chat+message id |
| `user:<tg_id>` | `bot/handlers/forget_me.py:81-88` | All contributions by a user |

**Fix applied:**
1. Added `_build_tombstone_keys(content_hash, chat_id, message_id, from_user_id)` pure
   helper that emits all 3 formats (gracefully skips if any field is None).
2. Extended SELECT query to pull `chat_id`, `message_id`, `user_id` from `chat_messages`
   JOIN — no N+1, single query covers all formats.
3. Single `forget_events` lookup with `tombstone_key IN (...)` covering all three key sets.
4. `_build_reason()` now includes matched tombstone format(s) in audit trail.
5. Added golden fixtures `06_message_tombstone.json` and `07_user_tombstone.json`.
6. Added 26 unit tests in `test_visibility_derivation_unit.py` covering all formats,
   combinations, and malformed-key graceful-skip cases.

---

## HIGH severity (test verifiability — fix mandatory)

### Self-reported 38-passing claim is unverifiable without Postgres

**Status: RESOLVED** — commit `6123e33` (2026-05-02). Fix iterated — coverage now ≥70%
in unit mode via `classify_visibility` extraction. Commit: `5e4983e`.

**Problem was:** All tests depended on `db_session` fixture (real Postgres). In CI
without Postgres, 37/38 tests silently skipped — coverage dropped to 34%.
Second iteration: unit coverage was still only 50% because classification logic was
inline inside the async orchestration body of `derive_card_visibility`.

**Fix applied (iteration 1 — commit `6123e33`):**
1. Split into `test_visibility_derivation_unit.py` (26 pure-function tests, no Postgres)
   and existing `test_visibility_derivation.py` with all DB-backed tests marked
   `@pytest.mark.integration` (31 integration tests + 1 enum test that stays unmarked).
2. Added `markers = ["integration: ..."]` to `pyproject.toml` `[tool.pytest.ini_options]`.
3. `pytest tests/services/ -m "not integration"` → **27 passed, 0 skipped** (no DB needed).
4. `pytest tests/services/ -m integration` → requires Postgres; all 37 integration tests run.

**Fix applied (iteration 2 — commit `5e4983e`, coverage ≥70%):**
1. Extracted `_VersionRow` frozen dataclass — pure-data carrier for fetched rows.
2. Extracted `classify_visibility(versions, matched_tombstone_keys) -> VisibilityDerivation`
   — pure function with no session. All precedence resolution, blocking_ids, reason logic.
3. Extracted `_fetch_versions()` and `_fetch_matched_tombstones()` — thin async SQL helpers.
4. `derive_card_visibility` now delegates to all three: fetch → classify.
5. Added 9 new unit tests in `test_visibility_derivation_unit.py` covering classify_visibility
   directly: all-visible, offrecord/nomem/tombstone blocks, precedence matrix, multiple blocking
   ids, empty versions.
6. Unit coverage: **83%** (was 50%). Async fetchers + derive_card_visibility stay uncovered
   in unit mode (expected — require real Postgres).

**Verification:**
```
pytest tests/services/test_visibility_derivation_unit.py --cov=bot.services.visibility_derivation --cov-report=term-missing
# → 35 passed, 83% coverage
```

---

## LOW severity (cosmetic — optional cleanup)

### Mock factory silent default `approved_by_user_id=1`

`tests/fixtures/mock_cards/__init__.py:32-37` — `make_approved_card()` defaults
`approved_by_user_id=1` if caller doesn't pass it. Could mask a "forgot to set
approver" bug in calling tests. Fix: require explicit non-None at call site OR use
a sentinel like `_UNSET` to force explicit choice.

### Golden fixture tests check visibility only, not `blocking_ids`/`reason`

`tests/services/test_visibility_derivation.py:624-628` — the golden-fixture
parameterized test asserts only the final `visibility` enum value. The
`blocking_source_ids` tuple and `reason` text are not verified. A regression that
returned correct visibility but wrong provenance would pass.

Fix: extend the parameterized assertion to compare `blocking_source_ids` and `reason`
against expected values in each fixture JSON. Add the expected fields to each
fixture file.

### Dead code in `_build_reason()` line 205

`bot/services/visibility_derivation.py:205` — fallback `return` line is unreachable
because all four `CardVisibility` enum values are handled in the preceding `if`
branches. Either remove the line or annotate `# pragma: no cover`.

### Pre-existing mypy error `bot/db/models.py:95,101`

`invite_user_id` is declared twice in `bot/db/models.py`. NOT introduced by this
experiments branch — pre-existing. Blocks full-project mypy. Should be fixed as a
separate hotfix PR before Sprint 0c promotion, but is out of Orch B's `models.py`
write scope (REGISTRY §2 Shared). Surface to Orch A or human team-lead.

---

## What was verified clean (no fix needed)

- All 9 mock factory fields match `prompts/PHASE6_PLAN_DRAFT.md` lines 178-197 exactly
  (Codex: "9/9 PRESENT").
- `derive_card_visibility` is read-only (no `session.add/flush/commit/insert/update/delete`).
- 3 spot-checked tests exercise documented behavior (not tautologies).
- `ruff` clean on all new files.
- Push state clean (origin and local in sync as of 2026-05-11).
- Precedence rule (REDACTED > NOMEM > FORGOTTEN > VISIBLE) is design-choice not
  invariant violation — Codex flagged as "QUESTIONABLE, not blocker" — document
  the precedence in HANDOFF at Sprint 0c if promoted.

---

**Audit reference:** Codex review verdict ID `aac6d1aa7fd5d3e7e`, full output in
session transcript `/private/tmp/claude-501/-Users-eekudryavtsev-Vibe-products-shkoderbot/`
on 2026-05-11.
