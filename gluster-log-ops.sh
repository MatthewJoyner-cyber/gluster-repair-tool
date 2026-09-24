#!/usr/bin/env bash
# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  gluster-log-ops.sh tail --volume VOLUME [--lines N]
  gluster-log-ops.sh grep --volume VOLUME --pattern TEXT [--context N]

Options:
  tail:
    --volume VOLUME     Volume name used to select the relevant Gluster logs
    --lines N           Number of tail lines per file (default: 200)

  grep:
    --volume VOLUME     Volume name used to select the relevant Gluster logs
    --pattern TEXT      Fixed string to search for in the selected logs
    --context N         Context lines to show around each match (default: 2)

Environment:
  GLUSTER_REPAIR_LOG_ROOT  Override the log root (default: /var/log/glusterfs).
                           Intended for tests and local overrides only.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

LOG_ROOT="${GLUSTER_REPAIR_LOG_ROOT:-/var/log/glusterfs}"

validate_volume() {
  local volume="${1:-}"
  [[ -n "$volume" ]] || die "--volume is required"
  case "$volume" in
    *[!A-Za-z0-9._-]*) die "unsupported volume name: $volume" ;;
  esac
  printf '%s' "$volume"
}

validate_positive_int() {
  local name="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || die "--${name} is required"
  case "$value" in
    ''|*[!0-9]*) die "--${name} must be a positive integer" ;;
  esac
  [[ "$value" -gt 0 ]] || die "--${name} must be greater than zero"
  printf '%s' "$value"
}

collect_log_files() {
  local volume="$1"
  local -a files=()
  local candidate

  for candidate in \
    "$LOG_ROOT/glusterd.log" \
    "$LOG_ROOT/glustershd.log" \
    "$LOG_ROOT/glfsheal-${volume}.log" \
    "$LOG_ROOT/${volume}.log"; do
    [[ -f "$candidate" ]] && files+=("$candidate")
  done

  if [[ -d "$LOG_ROOT/bricks" ]]; then
    while IFS= read -r -d '' candidate; do
      files+=("$candidate")
    done < <(find "$LOG_ROOT/bricks" -maxdepth 1 -type f -name "*${volume}*.log" -print0 2>/dev/null)
  fi

  printf '%s\n' "${files[@]}"
}

tail_logs() {
  local volume=""
  local lines="200"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --volume) volume="$(validate_volume "${2:-}")"; shift 2 ;;
      --lines) lines="$(validate_positive_int "lines" "${2:-}")"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  [[ -n "$volume" ]] || die "--volume is required"

  mapfile -t files < <(collect_log_files "$volume")
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "no matching log files for volume $volume under $LOG_ROOT"
    exit 0
  fi

  for file in "${files[@]}"; do
    printf '==> %s <==\n' "$file"
    tail -n "$lines" -- "$file"
  done
}

grep_logs() {
  local volume=""
  local pattern=""
  local context="2"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --volume) volume="$(validate_volume "${2:-}")"; shift 2 ;;
      --pattern) pattern="${2:-}"; shift 2 ;;
      --context) context="$(validate_positive_int "context" "${2:-}")"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  [[ -n "$volume" ]] || die "--volume is required"
  [[ -n "$pattern" ]] || die "--pattern is required"

  mapfile -t files < <(collect_log_files "$volume")
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "no matching log files for volume $volume under $LOG_ROOT"
    exit 0
  fi

  local matched=0
  local output=""
  for file in "${files[@]}"; do
    output="$(grep -n -C "$context" -F -- "$pattern" "$file" 2>/dev/null || true)"
    if [[ -n "$output" ]]; then
      matched=1
      printf '==> %s <==\n%s\n' "$file" "$output"
    fi
  done

  if [[ $matched -eq 0 ]]; then
    echo "no matches for pattern '$pattern' in logs for volume $volume"
  fi
}

[[ $# -gt 0 ]] || { usage; exit 2; }
case "$1" in
  tail) shift; tail_logs "$@" ;;
  grep) shift; grep_logs "$@" ;;
  -h|--help) usage; exit 0 ;;
  *) die "unsupported command: $1" ;;
esac
