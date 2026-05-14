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
  [[ "$path" =~ ^docs/memory-system/PHASE[0-9]+_PLAN\.md$ ]] && return 0

  # Phase 7 digest context module + tests reference the canonical
  # privacy literals in docstrings and test inputs because their job
  # is to ENFORCE the policy — they must name the literals to filter
  # against them. Same rationale as the design docs and phase plans
  # above; new file added in PR #290 (T7-03).
  [[ "$path" == "bot/services/digest_context.py" ]] && return 0
  [[ "$path" == "tests/services/test_digest_context.py" ]] && return 0

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
