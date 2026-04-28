# Stream Delta — Phase 2 finale (apply + rollback + report)

**Worktree:** `.worktrees/p2-delta`
**Phase branch:** `phase/p2-delta` (origin tracked, от свежей main после закрытия Alpha/Bravo/Charlie — 17/20 issues уже merged)
**Issues (waves):**

| Wave | Issues | Deps | Risk |
|------|--------|------|------|
| D1 | **#100** Tombstone collision dry-run report (S) | #99, #92 — все merged | standard |
| D2 | **#103** Import apply with synthetic telegram_updates — Phase 2 ФИНАЛ (XL) | #91, #92, #93, #94, #97, #98, #101, #102 — все merged | **HIGH-RISK обязательно** |
| D3 | **#104** Logical rollback per ingestion_run_id (M) | #103 | standard (но verify durability) |

**Wave order:** D1 → D2 → D3 (sequential — #103 трогает критическую import-apply поверхность, #104 строится на ней). D1 можно запустить параллельно с research/design D2.

**High-risk override обязателен для #103:**
- Implementer = `deep-implementer` Opus, effort high
- Verifier = 2 параллельных (`deep-analyst` + `ag-reviewer`), консенсус ОБОИХ
- Cross-check Codex = `codex:codex-rescue` с `reasoning_effort=xhigh` в промпте
- Особое внимание:
  - идемпотентность apply на повторных запусках
  - sync с governance (`detect_policy` ДОЛЖЕН run'ить на каждом synthetic update)
  - tombstone gate (#97 `import_tombstone_check`) обязан быть hooked в apply path
  - checkpoint/resume (#101 `import_checkpoint`) интегрирован
  - chunking + rate limit + advisory lock (#102 `import_chunking`) включены
  - reply resolver (#98 `import_reply_resolver`) подключён
  - НИ ОДНОГО прямого write в `chat_messages` или `message_versions` минуя normal ingestion path

**Cross-stream contract:**
- Все остальные streams ЗАКРЫТЫ. Конфликтов нет.
- Все нужные services уже в main (читай `docs/memory-system/import-*.md` перед использованием):
  `import_parser.py`, `import_dry_run.py`, `import_checkpoint.py`, `import_chunking.py`,
  `import_reply_resolver.py`, `import_tombstone_check.py`, `import_user_mapping.py`,
  `governance.py`, `cascade_worker.py`, `forget_event.py` repo, `offrecord_mark.py` repo
  — используй их, **НЕ переписывай**.
- НЕ добавляй новые feature flags вне `memory.import.apply.enabled` (default OFF).

**Per-sprint flow:** см. промпт от тимлида.

**Финал Stream Delta == финал Phase 2.** После merge #104:
1. Обнови `docs/memory-system/IMPLEMENTATION_STATUS.md` с финальным статусом всех 20 issues
2. Обнови `docs/memory-system/ROADMAP.md` — Phase 2 = DONE, gate passed, Phase 3-стрейч закрыт скелетом
3. Сообщи в финальном PR: "Phase 2 closed — готово к Final Holistic Review (20 PR суммарно)"
