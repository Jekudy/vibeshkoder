# Stream Bravo — Importer infrastructure

**Worktree:** `.worktrees/p2-bravo`
**Phase branch:** `phase/p2-bravo` (origin tracked)
**Issues (waves):**

| Wave | Issues | Deps |
|------|--------|------|
| B1 | **#91** Telegram Desktop export schema + anonymized fixture set (M, docs+fixtures) | none |
| B2 | **#93** Import user mapping policy (M), **#106** Edit history policy doc (S) | #91 |
| B3 | **#94** Import dry-run parser, no content writes (M) | #91, #93 |
| B4 | **#98** Reply resolver service (M), **#101** Import apply checkpoint/resume (M, **HIGH-RISK**) | #94 |
| B5 | **#99** Import dry-run duplicate/policy stats (M), **#102** Import apply rate-limit + chunking (S) | #94, #98, #101 |

Внутри wave — параллельно (создавай sprint worktrees `.worktrees/p2-bravo-sprint-NN-<slug>` от `phase/p2-bravo`).

**High-risk override:** #101 (resume/checkpoint — durability + recovery semantics).

**Cross-stream contract:**
- НЕ трогать `bot/handlers/chat_messages.py`, `bot/db/repos/message.py` (Alpha)
- НЕ трогать `forget_events`, `bot/handlers/forget*.py`, cascade worker (Charlie)
- Если parser использует governance — читай уже-merged Alpha #89 helper. Если #89 ещё не merged — используй существующий `bot/services/governance.py::detect_policy` напрямую, БЕЗ refactor.
- НЕ трогать `bot/services/import_apply.py` — это Stream Delta (#103)

**Per-sprint flow:** см. промпт от тимлида.

**Конец Stream:** все 8 PR merged, IMPLEMENTATION_STATUS.md обновлён, phase branch удалён. После этого Stream Delta может стартовать.
