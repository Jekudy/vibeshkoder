#!/usr/bin/env bash
set -euo pipefail

# Advisory local hook. CI is authoritative.

is_allowed_path() {
  local path="$1"

  # §7 #5 formal allowlist: only the four leakage-test fixture globs.
  # Everything else must be clean or appear in the baseline-diff (HEAD).
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_offrecord.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_nomem.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_forgotten.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_redacted.*\.jsonl$ ]] && return 0

  # Phase 6+ design docs document privacy invariants verbatim — they must be
  # allowed to reference the canonical token names defined by the pattern
  # above. This entry only matches Phase implementation/wave design docs
  # under docs/memory-system/, not arbitrary docs.
  [[ "$path" =~ ^docs/memory-system/T[0-9]+(-[0-9A-Z]+)+_design\.md$ ]] && return 0

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

write_baseline_matches() {
  local pattern="$1"
  local output_path="$2"
  shift 2
  local -a files=("$@")

  : >"$output_path"
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    return 0
  fi

  local grep_output
  local grep_status
  set +e
  # No -n: path:content format so line-number shifts don't break matching.
  grep_output="$(git grep -I -E "$pattern" HEAD -- "${files[@]}")"
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
    printf '%s\n' "${line#"HEAD:"}" >>"$output_path"
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
  done < <(git diff --cached --name-only --diff-filter=ACMR)

  if ((${#files[@]} == 0)); then
    return 0
  fi

  local pattern
  pattern="$(build_pattern)"

  local grep_output
  local grep_status
  set +e
  # No -n: path:content format so line-number shifts don't break matching.
  grep_output="$(git grep --cached -I -E "$pattern" -- "${files[@]}")"
  grep_status=$?
  set -e

  if ((grep_status == 1)); then
    return 0
  fi
  if ((grep_status != 0)); then
    printf '%s\n' "$grep_output" >&2
    return "$grep_status"
  fi

  local baseline_file
  baseline_file="$(mktemp)"
  trap 'rm -f "${baseline_file:-}"' EXIT
  write_baseline_matches "$pattern" "$baseline_file" "${files[@]}"

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
