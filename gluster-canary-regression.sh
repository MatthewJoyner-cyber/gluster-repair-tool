#!/usr/bin/env bash
# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CANARY="$ROOT_DIR/gluster-gtest-canary.sh"

volume="gtest"
prefix="release-regression-$(date +%Y%m%d-%H%M%S)"
dir_name="alpha"
file_name="payload.txt"
parent_name="alpha"
present_child="beta"
missing_child="gamma"
include_symlink=0
dry_run=0
proof_check_label=""
proof_check_input_source=""

usage() {
    cat <<'EOF'
usage: gluster-canary-regression.sh [--volume V] [--prefix PREFIX] [--dir DIR] [--file FILE] [--parent NAME] [--present-child NAME] [--missing-child NAME] [--include-symlink] [--dry-run]
       gluster-canary-regression.sh --check-proof-route LABEL INPUT_SOURCE

Run the stable live canary regression set on a disposable lab volume.
The default set covers the representative file and directory shapes:
- missing file replica
- below-quorum stale survivor
- file handle ghost
- orphaned GFID hardlink
- shallow directory child gap

Each current case is declared cleanup-only with canary_state input: it proves
canary setup, observation, and repair-engine teardown, not production repair
discovery. A future repair-proof declaration must use matching ordinary evidence;
the route checker rejects canary_state for heal-driven or operator-seeded labels.
Use --dry-run to list the declared routes without changing the lab.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --volume)
            volume="$2"
            shift 2
            ;;
        --prefix)
            prefix="$2"
            shift 2
            ;;
        --dir)
            dir_name="$2"
            shift 2
            ;;
        --file)
            file_name="$2"
            shift 2
            ;;
        --parent)
            parent_name="$2"
            shift 2
            ;;
        --present-child)
            present_child="$2"
            shift 2
            ;;
        --missing-child)
            missing_child="$2"
            shift 2
            ;;
        --include-symlink)
            include_symlink=1
            shift
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        --check-proof-route)
            proof_check_label="$2"
            proof_check_input_source="$3"
            shift 3
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

current_case=""

run_case() {
    current_case="$1"
    shift
    printf '\n==> %s\n' "$current_case"
    "$@"
    printf '<== %s ok\n' "$current_case"
}

is_repair_proof_label() {
    case "$1" in
        heal-driven|operator-seeded-live:path|operator-seeded-live:gfid|operator-seeded-live:index)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

validate_proof_route() {
    local proof_label="$1"
    local input_source="$2"
    local expected_input_source=""

    case "$proof_label" in
        heal-driven)
            expected_input_source="heal_info"
            ;;
        operator-seeded-live:path)
            expected_input_source="operator_path"
            ;;
        operator-seeded-live:gfid)
            expected_input_source="operator_gfid"
            ;;
        operator-seeded-live:index)
            expected_input_source="operator_index"
            ;;
        native-heal-smoke|cleanup-only)
            ;;
        *)
            printf 'error: unsupported proof label: %s\n' "$proof_label" >&2
            return 2
            ;;
    esac

    case "$input_source" in
        heal_info|operator_path|operator_gfid|operator_gfid_child|operator_index|canary_state)
            ;;
        *)
            printf 'error: unsupported input source: %s\n' "$input_source" >&2
            return 2
            ;;
    esac

    if is_repair_proof_label "$proof_label" && [[ "$input_source" == "canary_state" ]]; then
        printf 'error: repair proof %s cannot use input_source=canary_state\n' "$proof_label" >&2
        return 2
    fi
    if [[ -n "$expected_input_source" && "$input_source" != "$expected_input_source" ]]; then
        printf 'error: proof label %s requires input_source=%s, got %s\n' \
            "$proof_label" "$expected_input_source" "$input_source" >&2
        return 2
    fi
}

declare_case() {
    local scenario="$1"
    local proof_label="$2"
    local input_source="$3"

    validate_proof_route "$proof_label" "$input_source"
    printf '\n==> %s\n' "$scenario"
    printf '    proof label: %s\n' "$proof_label"
    printf '    input source: %s\n' "$input_source"
}

run_file_case() {
    local proof_label="$1"
    local input_source="$2"
    local label="$3"
    local create_subcmd="$4"
    local scenario="${prefix}-${label}"
    shift 4

    declare_case "$scenario" "$proof_label" "$input_source"
    if [[ "$dry_run" -eq 1 ]]; then
        printf '    dry-run: no canary commands launched\n'
        return
    fi

    run_case "${scenario}:create" \
        "$CANARY" "$create_subcmd" \
        --volume "$volume" \
        --name "$scenario" \
        --dir "$dir_name" \
        --file "$file_name" \
        "$@"
    run_case "${scenario}:observe" \
        "$CANARY" observe-file-state \
        --volume "$volume" \
        --name "$scenario"
    run_case "${scenario}:cleanup" \
        "$CANARY" cleanup \
        --volume "$volume" \
        --name "$scenario" \
        --dir "$dir_name" \
        --file "$file_name"
}

run_directory_case() {
    local proof_label="$1"
    local input_source="$2"
    local label="$3"
    local create_subcmd="$4"
    local scenario="${prefix}-${label}"
    shift 4

    declare_case "$scenario" "$proof_label" "$input_source"
    if [[ "$dry_run" -eq 1 ]]; then
        printf '    dry-run: no canary commands launched\n'
        return
    fi

    run_case "${scenario}:create" \
        "$CANARY" "$create_subcmd" \
        --volume "$volume" \
        --name "$scenario" \
        --parent "$parent_name" \
        --present-child "$present_child" \
        --missing-child "$missing_child" \
        "$@"
    run_case "${scenario}:observe" \
        "$CANARY" observe-directory-child-state \
        --volume "$volume" \
        --name "$scenario"
    run_case "${scenario}:cleanup" \
        "$CANARY" cleanup \
        --volume "$volume" \
        --name "$scenario" \
        --dir "$dir_name" \
        --file "$file_name"
}

trap 'echo "release regression stopped while running ${current_case:-unknown}" >&2' ERR

if [[ -n "$proof_check_label" ]]; then
    validate_proof_route "$proof_check_label" "$proof_check_input_source"
    printf 'proof route accepted: label=%s input_source=%s\n' \
        "$proof_check_label" "$proof_check_input_source"
    exit 0
fi

printf 'Release canary regression: volume=%s prefix=%s dry_run=%s\n' \
    "$volume" "$prefix" "$dry_run"
run_file_case "cleanup-only" "canary_state" "missing-file-replica" "create-missing-file-replica"
run_file_case "cleanup-only" "canary_state" "file-stale-survivor" "create-file-stale-survivor"
run_file_case "cleanup-only" "canary_state" "file-handle-ghost" "create-file-handle-ghost"
run_file_case "cleanup-only" "canary_state" "orphaned-gfid-hardlink" "create-orphaned-gfid-hardlink"
run_directory_case "cleanup-only" "canary_state" "directory-child-gap" "create-directory-child-gap"

if [[ "$include_symlink" -eq 1 ]]; then
    run_file_case "cleanup-only" "canary_state" "symlink-missing-stale" "create-symlink-missing-stale"
fi

printf '\nRelease canary regression completed successfully.\n'
