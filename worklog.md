# Worklog

## Entries

- **2026-09-24 (прод-свип, агент devin/swe-2 thr_re3abrga4v):** PR #416
  (remove superpowers routing) получил МЕРЖИТЬ от reviewer-субтреда
  thr_4qrtgqehu9 (1-строчный docs-дифф, чистый мерж), но не смержен: после
  `update-branch` trivy FAIL на main-side `anyio` CVE-2026-63374 (fixed в
  4.14.2, чинится в #530). Гейт «CI green on head» не пройден.
  Осталось: после мержа #530 — update-branch #416 и мержить.

## 2026-09-30

- 2026-09-30 14:01 — агент (thr_cfph7gt9p5): Evals перепроверен после #548. Nightly run 36697378870 зелёный (241 passed, validate skipped, artifact загружен); ручной dispatch на main 36704461337 fail-closed (validate exit 2, секрета нет). Проверено: `gh run view`.
- 2026-09-30 14:01 — агент (thr_cfph7gt9p5): найден пробел — runbook `semantic-qa.md` §6 `gh workflow run --ref "$RELEASE_GIT_SHA"` → HTTP 422 (dispatch принимает только branch/tag), `gh run watch` без id падает неинтерактивно. Issue #549.
- 2026-09-30 14:01 — агент решил (thr_cfph7gt9p5, дизайн подтверждён Fable claude-fable-5-1): docs-only фикс — временный lightweight tag через `gh api`, id run из вывода `gh workflow run`, удаление тега. Отвергнуто: input `release_sha` (binding становится заявленным), `--ref main` (гонка с main), изменение workflow (не нужно).
- 2026-09-30 14:01 — агент (thr_cfph7gt9p5): точный блок прогнан в zsh — run 36705450962 `workflow_dispatch`, headSha dfce40a, validate exit 2, watch exit 1, тег удалён. BMAD review 18 findings (11 patch, 7 reject), Fable APPROVE, ponytail-review lean. Spec `_bmad-output/implementation-artifacts/spec-gh-549-evals-release-dispatch-runbook.md`.
- 2026-09-30 14:01 — агент (thr_cfph7gt9p5): осталось — мерж PR по #549 и проверка CI на main; остаточные риски: Node20-deprecation warning и миграция ubuntu-latest на Ubuntu 26 с 19.10 (repo-wide, сейчас не ломает).
- 2026-09-30 14:09 — агент (thr_cfph7gt9p5): PR #550 заблокирован main-side trivy (run 36705983719): override `undici 7.29.0` — CVE-2026-19534, CVE-2026-84961 (HIGH). Issue #551; bump override до 7.29.1 + lock-only regen в том же PR по указанию thr_58na3tkuxu. Проверено: `npm ls undici` → 7.29.1 overridden.
