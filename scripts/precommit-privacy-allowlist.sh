#!/usr/bin/env bash
set -euo pipefail

# Advisory local hook. CI is authoritative.

is_allowed_path() {
  local path="$1"

  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_offrecord.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_nomem.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_forgotten.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^tests/fixtures/eval_seeds/leakage_redacted.*\.jsonl$ ]] && return 0
  [[ "$path" =~ ^docs/.*\.md$ ]] && return 0
  [[ "$path" =~ ^bot/services/governance\.py$ ]] && return 0
  [[ "$path" =~ ^tests/services/test_governance\.py$ ]] && return 0
  [[ "$path" =~ ^tests/handlers/.*test_chat_messages.*\.py$ ]] && return 0
  [[ "$path" =~ ^tests/services/test_ingestion.*\.py$ ]] && return 0
  [[ "$path" =~ ^\.github/workflows/lint-privacy\.yml$ ]] && return 0

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
  grep_output="$(git grep -I -n -E "$pattern" HEAD -- "${files[@]}")"
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
  grep_output="$(git grep --cached -I -n -E "$pattern" -- "${files[@]}")"
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

  local has_violation=0
  local line
  local path
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    path="${line%%:*}"
    if is_allowed_path "$path"; then
      continue
    fi
    if grep -F -x -q -- "$line" "$baseline_file"; then
      continue
    fi
    printf '%s\n' "$line"
    has_violation=1
  done <<<"$grep_output"

  return "$has_violation"
}

main "$@"
