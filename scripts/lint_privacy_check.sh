#!/usr/bin/env bash
set -euo pipefail

is_allowed_path() {
  local path="$1"

  # §7 #5 formal allowlist: only the four leakage-test fixture globs.
  # Everything else must be clean or appear in the baseline-diff.
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_offrecord.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_nomem.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_forgotten.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_redacted.*\.jsonl$ ]] && return 0

  # BEGIN PHASE13_COMPLETE_HISTORY_ALLOWLIST
  # Phase 13 deliberately retires legacy marker-based exclusion for human
  # messages. These exact runtime, migration-acceptance, fixture, and test files
  # either implement that complete-history boundary or verify repair of legacy
  # normalized rows. Keep this list path-exact: adjacent files remain linted.
  [[ "$path" == "bot/middlewares/raw_update_persistence.py" ]] && return 0
  [[ "$path" == "bot/services/governance.py" ]] && return 0
  [[ "$path" == "bot/services/import_apply.py" ]] && return 0
  [[ "$path" == "bot/services/import_html_parser.py" ]] && return 0
  [[ "$path" == "bot/services/ingestion.py" ]] && return 0
  [[ "$path" == "bot/services/wiki_compiler.py" ]] && return 0
  [[ "$path" == "docs/ops/phase13-production-preflight-2026-07-14.md" ]] && return 0
  [[ "$path" == "tests/fixtures/qa_eval_cases.json" ]] && return 0
  [[ "$path" == "tests/handlers/test_chat_messages_helper_path.py" ]] && return 0
  [[ "$path" == "tests/handlers/test_chat_messages_redelivery_idempotent.py" ]] && return 0
  [[ "$path" == "tests/handlers/test_edited_message.py" ]] && return 0
  [[ "$path" == "tests/integration/test_offrecord_irreversibility.py" ]] && return 0
  [[ "$path" == "tests/integration/test_phase4_hotfix_e2e.py" ]] && return 0
  [[ "$path" == "tests/services/test_governance_stub.py" ]] && return 0
  [[ "$path" == "tests/services/test_human_memory_policy.py" ]] && return 0
  [[ "$path" == "tests/services/test_import_apply.py" ]] && return 0
  [[ "$path" == "tests/services/test_import_dry_run_stats.py" ]] && return 0
  [[ "$path" == "tests/services/test_import_html_apply.py" ]] && return 0
  [[ "$path" == "tests/services/test_import_parser.py" ]] && return 0
  [[ "$path" == "tests/services/test_llm_gateway_wiki.py" ]] && return 0
  [[ "$path" == "tests/services/test_message_persistence.py" ]] && return 0
  # END PHASE13_COMPLETE_HISTORY_ALLOWLIST

  # T6-03 design doc — describes privacy invariants by name; baseline-stable.
  [[ "$path" == "docs/memory-system/T6-03_design.md" ]] && return 0

  # Phase 6+ design docs document privacy invariants verbatim — they must be
  # allowed to reference the canonical token names defined by the pattern
  # above. This entry only matches Phase implementation/wave design docs
  # under docs/memory-system/, not arbitrary docs.
  [[ "$path" =~ ^docs/memory-system/T[0-9]+(-[0-9A-Z]+)+_design\.md$ ]] && return 0

  # Phase plan documents (PHASE5_PLAN.md, PHASE6_PLAN.md, PHASE7_PLAN.md, …)
  # quote the binding privacy policy by name and freeze the scope contract.
  # Same rationale as Tn-XX_design.md above. Pre-existing phase plans pass
  # via baseline-diff; this entry lets new phase plans land cleanly without
  # a baseline regen step.
  [[ "$path" =~ ^docs/memory-system/PHASE[0-9]+_PLAN(_REFRESH)?\.md$ ]] && return 0

  # Phase design companion documents (PHASE12_DESIGN.md, …) — orientation
  # layer for ratified plans, document Butler boundary + governance contract
  # by literal name. Same rationale as PHASEn_PLAN.md above.
  [[ "$path" =~ ^docs/memory-system/PHASE[0-9]+_DESIGN\.md$ ]] && return 0

  # Phase rollout / closure playbooks (PHASE7_ROLLOUT.md, PHASE8_ROLLOUT.md, …)
  # name the privacy invariants for operator-facing acceptance checks and
  # post-rollout verification matrices. Same rationale as PHASEn_PLAN.md.
  [[ "$path" =~ ^docs/memory-system/PHASE[0-9]+_ROLLOUT\.md$ ]] && return 0

  # Project-root CLAUDE.md and the IMPLEMENTATION_STATUS tracker quote the
  # canonical privacy-invariant literals in per-phase closure entries.
  # Both files are append-only narrative; baseline-diff caught new lines
  # historically but rebase fragility makes that unreliable.
  [[ "$path" == "CLAUDE.md" ]] && return 0
  [[ "$path" == "docs/memory-system/IMPLEMENTATION_STATUS.md" ]] && return 0
  # .par-evidence.json — superflow PAR/FHR audit record; closure entries quote
  # the canonical privacy literals when recording review findings (same rationale
  # as CLAUDE.md / IMPLEMENTATION_STATUS.md above).
  [[ "$path" == ".par-evidence.json" ]] && return 0

  # Phase 7 digest modules + tests reference the canonical
  # privacy literals in docstrings and test inputs because their job
  # is to ENFORCE the policy — they must name the literals to filter
  # against them. Same rationale as the design docs and phase plans
  # above; files added across PRs #290 (T7-03), #293 (T7-02), #296 (T7-05).
  [[ "$path" == "bot/services/digest_context.py" ]] && return 0
  [[ "$path" == "bot/services/digest_publisher.py" ]] && return 0
  [[ "$path" == "bot/services/digest_redactor.py" ]] && return 0
  [[ "$path" == "tests/services/test_digest_context.py" ]] && return 0
  [[ "$path" == "tests/services/test_digest_publisher.py" ]] && return 0
  # T7-07: Phase 11 binding tests for digest leakage / citation / forget cascade.
  [[ "$path" == "tests/evals/test_digest_leakage.py" ]] && return 0
  # T8-07: Phase 11 binding tests for weekly digest review state machine,
  # cascade widening, redactor widening, publisher trigger guard widening, and
  # admin-gate refusals. Same rationale as test_digest_leakage.py above —
  # the file names canonical privacy invariants in docstrings + assertion
  # messages because the tests ENFORCE the policy by name.
  [[ "$path" == "tests/evals/test_digest_weekly_review_invariants.py" ]] && return 0

  # Phase 9 wiki migrations + cascade-binding tests reference the canonical
  # privacy literals in docstrings because they implement / enforce the policy
  # (e.g. wiki_revisions.revision_status='forgotten_redacted' explicitly names
  # the forget-cascade invariant). Same rationale as the digest_* allowlist
  # above. Pattern matches the Phase 9 migration window 050-059.
  [[ "$path" =~ ^alembic/versions/05[0-9]_.*\.py$ ]] && return 0

  # T9-02 wiki governance validator + tests enforce the 7 invalid-source
  # conditions by canonical literals (same rationale as digest_* and
  # design-doc allowlists above — the file's job is to ENFORCE the privacy
  # policy, so it must name the literals to filter against them).
  [[ "$path" == "bot/services/wiki_governance.py" ]] && return 0
  [[ "$path" == "tests/services/test_wiki_governance.py" ]] && return 0

  # T9-04 wiki renderer + tests implement citation-suppression policy.
  # The renderer consumes the governance result to enforce suppression of
  # invalid sources. Same rationale as wiki_governance.py allowlist above.
  [[ "$path" == "bot/services/wiki_renderer.py" ]] && return 0
  [[ "$path" == "tests/services/test_wiki_renderer.py" ]] && return 0

  # T9-05 member wiki router + route tests — enforce governance / suppress
  # policy at the HTTP layer. Same canonical-literals rationale.
  [[ "$path" == "web/routes/wiki.py" ]] && return 0
  [[ "$path" == "tests/web/test_wiki_routes.py" ]] && return 0

  # T9-07 wiki cascade layer tests — test the forget cascade behavior and
  # redaction invariants (edit_reason='forget_cascade',
  # revision_status='forgotten_redacted', body_markdown mask format). The
  # file's job is to ENFORCE the privacy cascade contract.
  [[ "$path" == "tests/services/test_wiki_cascade.py" ]] && return 0

  # T12-01 Butler cascade layer tests — test the forget cascade behavior for
  # butler_actions, butler_tool_invocations, butler_action_confirmations.
  # Docstrings name the canonical privacy literals because the tests ENFORCE
  # the cascade policy by name. Same rationale as test_wiki_cascade.py above.
  [[ "$path" == "tests/services/test_forget_cascade_butler.py" ]] && return 0

  # T12-02 Butler evidence leakage binding tests (L11.b family) — test that
  # governance.detect_policy exclusion works for all 6 fields at the
  # build_butler_evidence boundary. Docstrings and comments name the canonical
  # privacy policy literals because the tests ENFORCE the policy by name.
  # Same rationale as test_digest_leakage.py /
  # test_wiki_leakage.py / test_graph_leakage.py allowlist entries above.
  [[ "$path" == "tests/evals/test_butler_leakage.py" ]] && return 0
  [[ "$path" == "tests/evals/test_butler_forget_cascade.py" ]] && return 0
  [[ "$path" == "tests/evals/test_butler_citations.py" ]] && return 0
  [[ "$path" == "tests/evals/test_butler_refusal.py" ]] && return 0
  [[ "$path" == "tests/evals/test_butler_drift.py" ]] && return 0

  # T12-02 butler_evidence.py — the canonical governance pre-filter for the
  # Butler evidence-build path. Docstrings and inline comments name the
  # privacy policy literals because the file IS the policy enforcer — it
  # calls detect_policy and excludes non-normal rows from Butler evidence.
  # Same rationale as forget_cascade.py / digest_redactor.py above.
  [[ "$path" == "bot/services/butler_evidence.py" ]] && return 0

  # T12-02 rollout fragment — operator-facing rollout doc that documents the
  # privacy constraints honored by butler_evidence.py (Charter Hard
  # Constraint #3 references canonical forget-cascade policy literals by
  # name as part of explaining what the sprint protects against). Same
  # rationale as the butler_evidence.py allowlist above — the doc
  # describes the policy by name, so it must mention the canonical terms.
  [[ "$path" == "docs/rollout-fragments/phase12/t12-02.md" ]] && return 0

  # T12-04 rollout fragment — operator-facing doc documenting the
  # ButlerService state machine. Same rationale as the t12-02.md entry:
  # the doc names canonical privacy literals (Hard Constraint #3) as
  # policy explanation, not as a leak vector. The orchestrator code
  # itself (bot/services/butler.py) is NOT allowlisted — only the
  # documenting rollout fragment.
  [[ "$path" == "docs/rollout-fragments/phase12/t12-04.md" ]] && return 0

  # T9-08 Phase 11 binding tests for wiki — name the canonical privacy
  # literals in docstrings, assertion messages, and seed-data SQL because
  # they ENFORCE the policy by name. Same rationale as test_digest_leakage.py
  # / test_wiki_governance.py allowlist entries above. Covers L9a-e, C8a-b,
  # I7a-e, R6.a-f, G1.
  [[ "$path" == "tests/evals/test_wiki_leakage.py" ]] && return 0
  [[ "$path" == "tests/evals/test_wiki_citations.py" ]] && return 0
  [[ "$path" == "tests/evals/test_wiki_cascade.py" ]] && return 0
  [[ "$path" == "tests/evals/test_wiki_refusal.py" ]] && return 0
  [[ "$path" == "tests/evals/test_wiki_no_graph.py" ]] && return 0

  # 9.5-F Cache-Control binding test — module-level docstring explains WHY the
  # no-store header is required by referencing the privacy cascade contract.
  # Same rationale as other eval test allowlist entries above — the file
  # enforces the privacy-cache policy by name.
  [[ "$path" == "tests/evals/test_wiki_cache_control.py" ]] && return 0

  # T10-04 graph_projector integration test — verifies governance pre-filter
  # excludes governance-restricted cards from projection. Names canonical
  # privacy literals in fixture SQL and assertion messages because the test
  # ENFORCES the invariant by name. Same rationale as test_wiki_*.py / test_digest_*.py.
  [[ "$path" == "tests/db/test_graph_projector_integration.py" ]] && return 0

  # forget_cascade.py — the canonical enforcer of the forget policy across
  # all cascade layers (chat_messages, message_versions, digests, wiki_pages,
  # wiki_revisions, card_sources, llm_synthesis_cache, qa_traces_llm). The
  # file's docstrings and SQL strings legitimately reference the canonical
  # privacy literals because the file IS the policy. Same rationale as the
  # digest_redactor.py / digest_publisher.py / digest_context.py allowlist
  # entries above.
  [[ "$path" == "bot/services/forget_cascade.py" ]] && return 0

  # forget_predicate.py — shared SQL predicate module extracted in #291.
  # The single source of truth for the NOT EXISTS clause across forget_cascade,
  # digest_context, and llm_gateway. The docstring names the policy it enforces.
  # Same rationale as forget_cascade.py above — this file IS the privacy policy.
  [[ "$path" == "bot/services/forget_predicate.py" ]] && return 0

  # T10-09 Phase 11 binding tests for graph privacy, provenance, cascade, refusal, drift.
  # Test files name canonical privacy literals in docstrings and assertion messages because
  # they ENFORCE the governance policy by name (L10/C9/I8/R7/G2 binding tests).
  # Same rationale as test_wiki_*.py / test_digest_*.py allowlist entries above.
  [[ "$path" == "tests/evals/test_graph_leakage.py" ]] && return 0
  [[ "$path" == "tests/evals/test_graph_citations.py" ]] && return 0
  [[ "$path" == "tests/evals/test_graph_cascade.py" ]] && return 0
  [[ "$path" == "tests/evals/test_graph_refusal.py" ]] && return 0
  [[ "$path" == "tests/evals/test_graph_drift.py" ]] && return 0
  [[ "$path" == "tests/evals/test_graph_no_llm_in_rebuild.py" ]] && return 0

  return 1
}

build_pattern() {
  local hash="#"
  local off="off"
  local record="record"
  local no="no"
  local mem="mem"
  local for_part="for"
  local gotten_part="gotten"
  local boundary='(^|[^[:alnum:]_])'
  local end_boundary='([^[:alnum:]_]|$)'

  printf '(%s|%s|%s%s%s|%s%s%s)' \
    "${hash}${off}${record}" \
    "${hash}${no}${mem}" \
    "${boundary}" "${for_part}${gotten_part}" "${end_boundary}" \
    "${boundary}" "${no}${mem}" "${end_boundary}"
}

find_base_ref() {
  local branch
  branch="$(git branch --show-current 2>/dev/null || true)"

  if [[ -n "${PRIVACY_LINT_BASE_REF:-}" ]]; then
    printf '%s\n' "$PRIVACY_LINT_BASE_REF"
    return 0
  fi
  if [[ -n "${GITHUB_BASE_REF:-}" ]] && git rev-parse --verify "origin/${GITHUB_BASE_REF}" >/dev/null 2>&1; then
    printf 'origin/%s\n' "$GITHUB_BASE_REF"
    return 0
  fi
  if [[ "$branch" == "main" ]] && git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    printf '%s\n' "HEAD^"
    return 0
  fi
  if git rev-parse --verify origin/main >/dev/null 2>&1; then
    git merge-base HEAD origin/main
    return 0
  fi
  if git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    printf '%s\n' "HEAD^"
    return 0
  fi

  return 1
}

# Writes baseline matches (path:content, no line numbers) so that line-number
# shifts caused by rebases or unrelated inserts do not produce false positives.
write_baseline_matches() {
  local base_ref="$1"
  local pattern="$2"
  local output_path="$3"
  shift 3
  local -a files=("$@")

  : >"$output_path"
  [[ -z "$base_ref" ]] && return 0

  local grep_output
  local grep_status
  set +e
  grep_output="$(git grep -I -E "$pattern" "$base_ref" -- "${files[@]}")"
  grep_status=$?
  set -e

  if ((grep_status == 1)); then
    return 0
  fi
  if ((grep_status != 0)); then
    printf '%s\n' "$grep_output" >&2
    return "$grep_status"
  fi

  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Strip the "base_ref:" prefix that git grep prepends when given a treeish.
    printf '%s\n' "${line#"$base_ref:"}" >>"$output_path"
  done <<<"$grep_output"
}

main() {
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "privacy lint must run inside a git work tree" >&2
    return 2
  fi

  local -a files=()
  local file
  while IFS= read -r file; do
    case "$file" in
      _bmad/* | _bmad-output/* | .agents/skills/* | .claude/skills/*)
        continue
        ;;
    esac
    files+=("$file")
  done < <(git ls-files)

  if ((${#files[@]} == 0)); then
    return 0
  fi

  local pattern
  pattern="$(build_pattern)"

  local grep_output
  local grep_status
  set +e
  # No -n: output is path:content (no line numbers) so baseline comparison is
  # resilient to line-number shifts caused by rebases or unrelated inserts.
  grep_output="$(git grep -I -E "$pattern" -- "${files[@]}")"
  grep_status=$?
  set -e

  if ((grep_status == 1)); then
    return 0
  fi
  if ((grep_status != 0)); then
    printf '%s\n' "$grep_output" >&2
    return "$grep_status"
  fi

  local base_ref=""
  base_ref="$(find_base_ref || true)"
  local baseline_file
  baseline_file="$(mktemp)"
  trap 'rm -f "${baseline_file:-}"' EXIT
  write_baseline_matches "$base_ref" "$pattern" "$baseline_file" "${files[@]}"

  # Build an allowed-path-filtered version of current matches for multiset
  # comparison.  Lines whose path is in the formal allowlist are excluded
  # before the count comparison so they don't inflate the current count.
  local current_file
  current_file="$(mktemp)"
  trap 'rm -f "${baseline_file:-}" "${current_file:-}"' EXIT

  local line
  local path
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    path="${line%%:*}"
    is_allowed_path "$path" && continue
    printf '%s\n' "$line" >>"$current_file"
  done <<<"$grep_output"

  # Multiset comparison: report lines where current_count > baseline_count.
  # Uses FILENAME-based file discrimination (not NR==FNR) so that an empty
  # baseline file does not cause current lines to be misclassified as baseline.
  local violations
  violations="$(awk -v bfile="$baseline_file" '
    FILENAME == bfile { baseline[$0]++; next }
    {
      if (baseline[$0] > 0) {
        baseline[$0]--
      } else {
        print
      }
    }
  ' "$baseline_file" "$current_file")"

  if [[ -n "$violations" ]]; then
    printf '%s\n' "$violations"
    return 1
  fi

  return 0
}

main "$@"
