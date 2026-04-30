# Codex Dual-Agent Execution Pattern (Phase 4+ default)

This is the canonical execution wrapper for all Phase 4+ stream prompts in the memory cycle. Every stream prompt in `docs/memory-system/prompts/` references this pattern and prepends a short re-statement of it at the top.

## Why dual-agent via Codex

Per `~/.claude/rules/codex-routing.md` + `~/.claude/CLAUDE.md`:

- **gpt-5.5 high-reasoning** outperforms default Sonnet on SQL, governance invariant checking, and async-Python correctness — empirically observed across Phase 2 sprints.
- **Two independent contexts** catch hallucinations that a single agent would commit and then confirm (saw ≥4 hallucinated APIs in this Phase 4 planning round when single-agent — see PR #154 audit).
- **Plugin path `codex:codex-rescue`** routes through `scripts/codex-companion.mjs`, bypassing the broken interactive-shell wrapper. Raw `codex exec` from Bash fails on a ChatGPT account with `model not supported` — never invoke raw codex from Bash.

## Roles

```
┌──────────────────────────────────────────────────────────────────┐
│ ORCHESTRATOR (your main Claude session)                          │
│  • Reads stream prompt                                           │
│  • Dispatches Codex Executor                                     │
│  • Dispatches Codex Verifier (independent, fresh context)        │
│  • Loops (max 2 fix cycles)                                      │
│  • Merges PR after verifier APPROVE + CI green                   │
│  • DOES NOT WRITE CODE                                           │
└──────────────────────────────────────────────────────────────────┘
        │                                          │
        ▼                                          ▼
┌────────────────────────┐              ┌─────────────────────────┐
│ CODEX EXECUTOR         │              │ CODEX VERIFIER          │
│ subagent_type:         │              │ subagent_type:          │
│   codex:codex-rescue   │              │   codex:codex-rescue    │
│ Reasoning: high        │              │ Reasoning: high         │
│ Sees: full stream spec │              │ Sees: PR diff + spec    │
│ Outputs: PR URL        │              │ Outputs: APPROVE /      │
│                        │              │   NEEDS_FIXES / REJECT  │
│                        │              │   + file:line cites     │
└────────────────────────┘              └─────────────────────────┘
```

## Step-by-step (orchestrator perspective)

### Step 1 — Dispatch Codex Executor

```
Agent(
  subagent_type="codex:codex-rescue",
  description="<Stream X> executor",
  model="opus",  # ignored — codex plugin uses gpt-5.5 internally
  prompt=f"""
Ты — autonomous executor для Phase {{N}} Stream {{X}} (Shkoderbot memory cycle).

Полностью выполни приложенный stream spec: создай worktree, читай контекст,
пиши код + тесты, прогоняй pytest --timeout=120 + ruff check . + mypy bot/,
push, gh pr create с label phase:{{N}}, дождись CI green, **НЕ мерджи PR
сам — оркестратор сделает merge после verifier approve**.

В финальном ответе верни:
- PR URL + branch name + last commit SHA
- Пути изменённых файлов
- Pytest/ruff/mypy output (последние ~30 строк каждого)
- Подтверждение: "no LLM imports introduced" + grep evidence
- Любые отклонения от spec и причины

--- STREAM SPEC START ---
{{paste full stream prompt content here}}
--- STREAM SPEC END ---
""",
  run_in_background=True
)
```

Wait for completion notification.

### Step 2 — Dispatch Codex Verifier (FRESH context, independent)

```
Agent(
  subagent_type="codex:codex-rescue",
  description="<Stream X> verifier",
  prompt=f"""
Ты — independent verifier для Phase {{N}} Stream {{X}} PR <URL_FROM_STEP_1>.
Другой codex-агент уже выполнил работу. Твоя задача — независимо проверить.

Цитируй file:line как evidence для каждого утверждения. Проверь следующее:

(a) ACCEPTANCE: каждый чекбокс из секции "Definition of Done" stream spec'а
    объективно выполнен. Если не выполнен — укажи какой и почему.

(b) INVARIANTS — все 6 не нарушены, особенно:
    - Invariant #2 (no LLM): запусти
        grep -r "from anthropic\\|from openai\\|import openai\\|import anthropic\\|langchain\\|huggingface\\|transformers\\|ollama" bot/
      Ожидаемый результат: пусто. Если что-то найдено — REJECT.
    - Invariant #3: search/qa код не возвращает rows where memory_policy != 'normal'
      или is_redacted = true (на любой из таблиц в JOIN-е).
    - Invariant #9: tombstone exclusion присутствует в любых search-paths.

(c) NO HALLUCINATIONS — для каждой ссылки на функцию/модуль в shipped коде
    проверь grep'ом существование. Особенно остерегайся:
    - bot.services.feature_flag.is_enabled (не существует — реальный API
      bot.db.repos.feature_flag.FeatureFlagRepo.get)
    - UserRepo.get_by_id (не существует — реальный UserRepo.get / get_by_tg_id)
    - process_forget_for_user (не существует — реальный run_cascade_worker_once)
    - detect_policy(...) возвращает str (нет — возвращает tuple)

(d) TESTS реально зелёные:
    git checkout <branch> && timeout 120 pytest -x --timeout=120 <test paths из spec>
    ruff check . && mypy bot/<paths>
    Сравни actual output с тем что заявлено в PR description.
    Сравни test cases с заявленными в spec — все ли categories покрыты.

(e) NO --admin merge: gh pr view <PR#> --json mergedBy,mergeMethod
    должно показать REBASE и не bot/admin override. (PR ещё не смерджен на этом
    шаге, но проверь что executor не вызывал --admin в логах.)

(f) PR DESCRIPTION содержит:
    - 6 invariants verbatim
    - issue #<N> reference
    - pytest output paste
    - confirmation "no LLM imports"

Verdict format:
{{verdict}}: APPROVE | NEEDS_FIXES | REJECT

Findings:
1. [severity: critical|high|medium|low] <file>:<line> — <issue> — <fix>
2. ...

Если APPROVE — это сигнал оркестратору запускать merge.
Если NEEDS_FIXES — оркестратор re-dispatch executor с твоим verdict.
Если REJECT — оркестратор эскалирует человеку (что-то сломано фундаментально).

--- STREAM SPEC START ---
{{paste full stream prompt content here}}
--- STREAM SPEC END ---
""",
  run_in_background=True
)
```

Wait for completion.

### Step 3 — If NEEDS_FIXES — loop (max 2 cycles)

```
Agent(
  subagent_type="codex:codex-rescue",
  description="<Stream X> fix-up cycle",
  prompt=f"""
Verifier нашёл issues в твоём PR <URL>. Исправь и push в ту же ветку.
НЕ создавай новую ветку, НЕ открывай новый PR.

После push прогони ВСЕ те же тесты заново и paste output в PR comment.

--- VERIFIER VERDICT ---
{{paste verifier output}}
--- END VERDICT ---
""",
  run_in_background=True
)
```

После fix re-dispatch verifier (Step 2). Limit: 2 fix cycles. Если verifier всё ещё NEEDS_FIXES → escalate.

### Step 4 — Merge

Verifier APPROVE + CI green:

```bash
cd /Users/eekudryavtsev/Vibe/products/shkoderbot   # main repo, NOT worktree!
gh run list -L 3                                    # confirm latest CI green
gh pr merge <PR#> --rebase --delete-branch          # NEVER --admin
```

Если CI red — это сигнал что executor что-то пропустил. Re-dispatch fix cycle.

## Anti-rationalization

Если думаешь:
- "Verifier overkill для маленькой задачи" → НЕТ. Codex дёшев и быстр; стоимость дольше fix-cycle позже.
- "Я сам быстрее напишу код чем дёргать executor" → НЕТ. Single-agent context увеличивает риск hallucination, доказано на этой фазе.
- "Pass на --admin merge быстрее" → НИКОГДА. CI red = реальная проблема. Fix it.
- "Verifier нашёл только nitpicks, замержу как есть" → перечитай findings; severity:critical/high обязательны к фиксу.

## Когда НЕ применять этот pattern

- Тривиальные docs-only changes (typo fix, README tweak) — single Codex agent или ручной edit достаточен.
- Hotfix во время production incident — direct human intervention важнее ритуала.
- Discovery / research tasks без deterministic acceptance — single deep-analyst или general-purpose agent.

Для всех implementation streams Phase 4+ — pattern обязателен.
