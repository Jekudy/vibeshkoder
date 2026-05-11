# TODO before Sprint 0c promotion to main

This file lists findings from the Codex pre-promotion review (`aac6d1aa7fd5d3e7e`,
date 2026-05-11) that MUST be addressed before promoting this experiments branch
into Sprint 0c canonical paths.

The Sprint 0c orchestrator reads this file BEFORE moving any code into
`bot/services/` or `tests/` on `main`.

---

## HIGH severity (privacy invariant #9 binding — fix mandatory)

### Tombstone-format coverage gap

**Status:** NOT FIXED in experiments branch. Must be fixed at Sprint 0c.

**Problem:** `bot/services/visibility_derivation.py:136-145` builds only
`message_hash:{content_hash}` tombstone keys when scanning `forget_events`. Production
code creates THREE tombstone formats:

| Format | Production creator | What it forgets |
|--------|---------------------|-----------------|
| `message_hash:<sha256>` | `forget_cascade.py` content-redact path | Specific message content (current scope of visibility_derivation) |
| `message:<chat_id>:<message_id>` | `bot/handlers/forget_reply.py:135-145` | Specific message by chat+message id |
| `user:<tg_id>` | `bot/handlers/forget_me.py:81-88` | All contributions by a user |

`forget_cascade.py:84-91,305-310` honors all three formats; `visibility_derivation.py`
honors only the first. **This means a card whose source has been forgotten via
`/forget_reply` (message format) or `/forget_me` (user format) will STILL render as
VISIBLE** through derive_card_visibility — a real privacy invariant #9 leak path for
wiki and graph rendering at Phase 9/10.

**Fix at Sprint 0c promotion:**
1. Extend tombstone-key generation in `derive_card_visibility` to build all three
   formats: `message_hash:{content_hash}`, `message:{chat_id}:{message_id}`,
   `user:{from_user_id}` — pulling chat_id, message_id, from_user_id from the joined
   `chat_messages` row alongside content_hash.
2. Single `forget_events` lookup with `tombstone_key IN (...)` covering all three sets.
3. Add 2 new golden fixtures: `06_message_tombstone.json` (message-format tombstone)
   and `07_user_tombstone.json` (user-format tombstone). Existing
   `04_forgotten_source.json` covers message_hash format only.
4. Add ~6 unit tests covering message/user tombstone formats and combinations.

**Re-verification at fix time:** Run `pytest tests/services/test_visibility_derivation.py
-v` in a real Postgres environment (see HIGH finding below) and confirm all tombstone
formats correctly block visibility.

---

## HIGH severity (test verifiability — fix mandatory)

### Self-reported 38-passing claim is unverifiable without Postgres

**Status:** ENVIRONMENTAL — not a code bug, but a process gap.

**Problem:** Codex reproduced `1 passed / 37 skipped` instead of the implementer's
claimed `38 passed`. Tests skip because they all depend on the `db_session` fixture
which requires a reachable Postgres. In CI environments without Postgres available,
the test suite reports green while exercising only 1 test — coverage drops to 34%.

**Fix at Sprint 0c promotion:**
1. Either: split tests into (a) pure-function unit tests that don't need Postgres
   (mockable, can run in any environment), and (b) integration tests gated behind a
   `@pytest.mark.integration` marker requiring Postgres.
2. Or: require Postgres in CI for this test file (add to `.github/workflows/ci.yml`).
3. Add a pytest invariant check: if `db_session` is unreachable, fail loudly instead
   of silently skipping (or use `pytest --strict-markers` to enforce explicit skip).

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
