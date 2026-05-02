#!/usr/bin/env bash
set -euo pipefail

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
  grep_output="$(git grep -I -n -E "$pattern" "$base_ref" -- "${files[@]}")"
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
  grep_output="$(git grep -I -n -E "$pattern" -- "${files[@]}")"
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
