#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import json
import sys

from gluster_heal_tool.manager import (
    build_manager_health_report,
    render_manifest_summary,
    render_manager_preflight_summary,
    resolve_via_backend_path,
    resolve_via_gfid,
    resolve_via_gfid_child,
    resolve_via_index_entry,
    resolve_via_path,
    resolve_via_volume,
    resolve_via_workers,
    write_manager_preflight_report,
)
from gluster_heal_tool.install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from gluster_heal_tool.canary_directory import build_directory_tie_plan_from_canary_state
from gluster_heal_tool.planner import build_plan, render_plan_summary, summarize_plan, write_plan
from gluster_heal_tool.apply_binding import BindingError, validate_plan_context
from gluster_heal_tool.planner_graph import (
    build_graph_cycle_baseline_status,
    build_graph_cycle_status,
    render_graph_cycle_status_report,
    render_graph_cycle_status_summary,
    render_graph_report,
)
from gluster_heal_tool.controller_cycle_report import (
    render_controller_cycle_report,
)
from gluster_heal_tool.graph_audit import render_graph_audit_report
from gluster_heal_tool.graph_family_report import render_graph_family_report
from gluster_heal_tool.planner_payload import load_plan_actions
from gluster_heal_tool.status_report import render_status_report
from gluster_heal_tool.manifest import load_manifest
from gluster_heal_tool.directory_tie import (
    build_directory_tie_decisions,
    build_directory_tie_report,
    prompt_directory_tie_budget_override,
    prompt_directory_tie_selections,
    resolve_directory_tie_budget,
    render_directory_tie_report,
    write_directory_tie_report,
)
from gluster_heal_tool.status import DEFAULT_STATUS_FILE, load_status, status_warnings, update_status
from gluster_heal_tool.version import __version__
from gluster_heal_tool.apply import (
    DEFAULT_REVIEW_POLICIES,
    DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
    build_apply_results,
    build_preflight_report,
    filter_apply_results,
    load_decisions,
    load_plan,
    render_apply_summary,
    summarize_apply_results,
    write_apply_results,
    write_preflight_report,
)
from gluster_heal_tool.backup_maintenance import restore_backup_archive
from gluster_heal_tool.cli import main as cli_main
from gluster_heal_tool.decisions import build_decision_report, write_decision_file, write_decision_report
from gluster_heal_tool.heal_guard import DEFAULT_HEAL_INFO_REPEAT_LIMIT, DEFAULT_HEAL_INFO_REFRESH_TIMEOUT_SECONDS
from gluster_heal_tool.repair_matrix import render_repair_matrix, render_repair_matrix_json
from gluster_heal_tool.temp_mount import (
    DEFAULT_TEMP_MOUNT_ROOT,
    render_temp_mount_result,
    run_temp_mount,
)
from gluster_heal_tool.controller_paths import default_backup_archive_dir, default_health_report_path
from gluster_heal_tool.simple_mode import configure_simple_repair_parser, run_simple_command
from gluster_heal_tool.volume import (
    discover_brick_hosts,
    discover_brick_paths,
    get_heal_settings,
    set_heal_settings,
    summarize_heal_settings,
)
from gluster_heal_tool.heal_performance import execute_heal_performance


def _hosts(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _review_policies_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "entry_split_brain_file": args.policy_entry_split_brain_file,
        "probable_stale_survivor_file": args.policy_stale_survivor_file,
        "probable_orphaned_symlink_file": args.policy_stale_survivor_symlink,
        "file_metadata_only": args.policy_file_metadata,
        "entry_split_brain_directory": args.policy_entry_split_brain_directory,
        "probable_stale_survivor_directory": args.policy_stale_survivor_directory,
        "directory_gfid_conflict": args.policy_directory_gfid_conflict,
        "directory_metadata": args.policy_directory_metadata,
        "type_mismatch": args.policy_type_mismatch,
    }


def _split_brain_native_policy_from_args(args: argparse.Namespace) -> str:
    return args.policy_entry_split_brain_native


def _scope_from_args(args: argparse.Namespace) -> str:
    return args.scope


def _print_status_warnings(status_file: str) -> None:
    for message in status_warnings(status_file):
        print(f"WARNING: {message}", file=sys.stderr)


def _default_mountpoint(volume: str) -> str:
    volume = volume.strip().strip("/")
    return f"/{volume}" if volume else "/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gluster-manager",
        description="Gluster repair tool: human-friendly repair by default, with explicit safe-auto and expert workflows.",
        epilog=(
            "Human flow: gluster-manager repair --volume VOL. In a TTY it previews, explains, and asks before the ready-safe batch. "
            "Use --preview for read-only, -y for the safe-auto subset, or --expert --interactive --execute for expert-guided execution."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    manifest_cmd = sub.add_parser("manifest-build")
    manifest_cmd.add_argument(
        "--heal-file",
        help="existing heal snapshot file to ingest; use 'latest' to reuse the newest snapshot for this volume",
    )
    manifest_cmd.add_argument(
        "--heal-latest",
        action="store_true",
        help="reuse the newest timestamped heal snapshot for this volume under the heal snapshot root",
    )
    manifest_cmd.add_argument(
        "--heal-fresh",
        action="store_true",
        help="force a fresh live heal snapshot even if a cached latest snapshot exists",
    )
    manifest_cmd.add_argument(
        "--heal-out",
        help="where to save a live heal snapshot when --heal-file is omitted; defaults to a timestamped file under the heal snapshot root",
    )
    manifest_cmd.add_argument(
        "--heal-root",
        help="root directory that stores per-volume heal snapshots for latest reuse (default: $GLUSTER_REPAIR_WORK_ROOT/heal-info)",
    )
    manifest_cmd.add_argument("--volume", required=True)
    manifest_cmd.add_argument("--hosts")
    manifest_cmd.add_argument("--brick-path")
    manifest_cmd.add_argument("--mountpoint")
    manifest_cmd.add_argument("--resolver-path", default=str(DEFAULT_RESOLVER_PATH))
    manifest_cmd.add_argument("--worker-path", default=str(DEFAULT_WORKER_PATH))
    manifest_cmd.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="SSH login user on the brick hosts (default: gluster-repair)",
    )
    manifest_cmd.add_argument("--manifest-out", required=True)
    manifest_cmd.add_argument("--observations-out", required=True)
    manifest_cmd.add_argument("--log-out")
    manifest_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    manifest_cmd.add_argument("--verbose", action="store_true")
    manifest_cmd.add_argument("--skip-mount-probe", action="store_true")

    def _configure_evidence_build_parser(
        cmd: argparse.ArgumentParser,
        *,
        observations_required: bool = False,
        include_log_out: bool = False,
    ) -> None:
        evidence_input = cmd.add_mutually_exclusive_group(required=True)
        evidence_input.add_argument("--path", help="client-visible path on a mounted Gluster volume")
        evidence_input.add_argument(
            "--backend-path",
            help="backend object path beneath a Gluster-recorded brick root; gathers brick evidence without probing a client mount",
        )
        evidence_input.add_argument("--gfid", help="bare GFID UUID or <gfid:...> heal entry")
        evidence_input.add_argument(
            "--gfid-child",
            help="child heal entry of the form <gfid:UUID>/child/path",
        )
        evidence_input.add_argument(
            "--index-entry",
            help="validated .glusterfs/indices/xattrop or .glusterfs/indices/dirty path",
        )
        cmd.add_argument(
            "--volume",
            help="required with --backend-path, --gfid, --gfid-child, or --index-entry evidence routes",
        )
        cmd.add_argument(
            "--mountpoint", help="display mount root for non-path evidence routes (default: /<volume>; not probed)"
        )
        cmd.add_argument("--resolver-path", default=str(DEFAULT_RESOLVER_PATH))
        cmd.add_argument(
            "--ssh-user",
            default=DEFAULT_SERVICE_USER,
            help="SSH login user on the brick hosts (default: gluster-repair)",
        )
        cmd.add_argument("--worker-path", default=str(DEFAULT_WORKER_PATH))
        cmd.add_argument("--manifest-out", required=True)
        cmd.add_argument("--observations-out", required=observations_required)
        probe_group = cmd.add_mutually_exclusive_group()
        probe_group.add_argument(
            "--mount-probe",
            choices=["auto", "off", "on"],
            default="auto",
            help="mount probing policy for evidence-build and repair-meta: auto=operator-safe default, off=never probe, on=probe once",
        )
        probe_group.add_argument(
            "--skip-mount-probe",
            action="store_true",
            help="compatibility alias for --mount-probe off",
        )
        if include_log_out:
            cmd.add_argument("--log-out")
        cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
        cmd.add_argument("--verbose", action="store_true")

    evidence_build_cmd = sub.add_parser(
        "evidence-build",
        help="build manifest and observations from operator-supplied path, GFID, backend path, or index evidence",
    )
    _configure_evidence_build_parser(evidence_build_cmd, observations_required=True, include_log_out=True)

    repair_meta_cmd = sub.add_parser(
        "repair-meta",
        help="compatibility alias for evidence-build",
    )
    _configure_evidence_build_parser(repair_meta_cmd, observations_required=True, include_log_out=True)

    plan_cmd = sub.add_parser("plan-build")
    plan_cmd.add_argument("--manifest-in", required=True)
    plan_cmd.add_argument("--mountpoint")
    plan_cmd.add_argument("--plan-out", required=True)
    plan_cmd.add_argument(
        "--policy-entry-split-brain-file",
        choices=["auto", "majority", "mtime", "ctime", "size", "review", "off", "skip", "replace", "quarantine"],
        default=DEFAULT_REVIEW_POLICIES["entry_split_brain_file"],
        help="split-brain file policy for recursive child-gap promotion",
    )
    plan_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    apply_cmd = sub.add_parser("apply-build", help="build dry-run apply steps from a plan, including review items")
    apply_cmd.add_argument("--plan-in", required=True)
    apply_cmd.add_argument("--apply-out", required=True)
    apply_cmd.add_argument("--decision-file")
    apply_cmd.add_argument("--backup-root")
    apply_cmd.add_argument(
        "--backup-mode",
        choices=["required", "best-effort", "none"],
        default="required",
    )
    apply_cmd.add_argument("--action-type")
    apply_cmd.add_argument("--path-contains")
    apply_cmd.add_argument("--strategy")
    apply_cmd.add_argument("--limit", type=int)
    apply_cmd.add_argument("-y", "--batch", action="store_true")
    apply_cmd.add_argument(
        "--policy-entry-split-brain-file",
        choices=["auto", "majority", "mtime", "ctime", "size", "review", "off", "skip", "replace", "quarantine"],
        default=DEFAULT_REVIEW_POLICIES["entry_split_brain_file"],
    )
    apply_cmd.add_argument(
        "--policy-entry-split-brain-native",
        choices=["auto", "source-brick", "latest-mtime", "bigger-file"],
        default=DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
        help="native Gluster split-brain policy used by the official resolver step: auto=latest-mtime; use source-brick only when the keeper brick is already known",
    )
    apply_cmd.add_argument(
        "--policy-stale-survivor-file",
        choices=["delete", "salvage", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["probable_stale_survivor_file"],
        help="below-quorum file policy: delete by default, or salvage directly from a chosen brick when the operator explicitly opts in",
    )
    apply_cmd.add_argument(
        "--policy-stale-survivor-symlink",
        choices=["delete", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["probable_orphaned_symlink_file"],
    )
    apply_cmd.add_argument(
        "--policy-file-metadata",
        choices=["repair", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["file_metadata_only"],
        help="file-metadata policy: repair when the canonical GFID evidence is present, otherwise review",
    )
    apply_cmd.add_argument(
        "--policy-entry-split-brain-directory",
        choices=["rename", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["entry_split_brain_directory"],
    )
    apply_cmd.add_argument(
        "--policy-stale-survivor-directory",
        choices=["auto", "delete", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["probable_stale_survivor_directory"],
    )
    apply_cmd.add_argument(
        "--policy-directory-gfid-conflict",
        choices=["rename", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["directory_gfid_conflict"],
    )
    apply_cmd.add_argument(
        "--policy-directory-metadata",
        choices=["review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["directory_metadata"],
    )
    apply_cmd.add_argument(
        "--policy-type-mismatch",
        choices=["review", "quarantine-both", "quarantine-loser"],
        default=DEFAULT_REVIEW_POLICIES["type_mismatch"],
        help="file-vs-directory mismatch policy: review, quarantine both sides, or quarantine a completely observed older side",
    )
    apply_cmd.add_argument(
        "--scope",
        choices=["all", "files", "directories", "metadata", "files,directories", "files,metadata", "directories,metadata"],
        default="all",
        help="limit apply-build output to a repair family",
    )
    apply_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    preflight_cmd = sub.add_parser("apply-preflight")
    preflight_cmd.add_argument("--apply-in", required=True)
    preflight_cmd.add_argument("--preflight-out", required=True)
    preflight_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    manager_preflight_cmd = sub.add_parser("manager-preflight")
    manager_preflight_cmd.add_argument("--volume", required=True)
    manager_preflight_cmd.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="SSH login user on the brick hosts (default: gluster-repair)",
    )
    manager_preflight_cmd.add_argument("--connect-timeout", type=float, default=10.0)
    manager_preflight_cmd.add_argument("--preflight-out", required=True)
    manager_preflight_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    health_cmd = sub.add_parser(
        "health-check",
        help="check Gluster volume, brick, and helper health before repair execution",
    )
    health_cmd.add_argument("--volume", required=True)
    health_cmd.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="SSH login user on the brick hosts (default: gluster-repair)",
    )
    health_cmd.add_argument("--connect-timeout", type=float, default=10.0)
    health_cmd.add_argument(
        "--health-out",
        help="where to save the health-check report; defaults to $GLUSTER_REPAIR_WORK_ROOT/health-check/<volume>-health-check.json",
    )
    health_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    decision_cmd = sub.add_parser("decision-build")
    decision_cmd.add_argument("--plan-in", required=True)
    decision_cmd.add_argument("--volume", required=True)
    decision_cmd.add_argument("--hosts")
    decision_cmd.add_argument("--worker-path", default=str(DEFAULT_WORKER_PATH))
    decision_cmd.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="SSH login user on the brick hosts (default: gluster-repair)",
    )
    decision_cmd.add_argument("--report-out", required=True)
    decision_cmd.add_argument("--decision-out")
    decision_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    directory_tie_cmd = sub.add_parser(
        "directory-tie-build",
        help="build a bounded tree-diff report for directory tie review cases",
    )
    directory_tie_cmd.add_argument("--plan-in")
    directory_tie_cmd.add_argument(
        "--canary-volume",
        help="read a live directory GFID canary state and synthesize a tie report input from it",
    )
    directory_tie_cmd.add_argument(
        "--canary-name",
        help="scenario name for --canary-volume when using a live directory GFID canary as input",
    )
    directory_tie_cmd.add_argument("--report-out", required=True)
    directory_tie_cmd.add_argument("--decision-out")
    directory_tie_cmd.add_argument("--interactive", action="store_true")
    directory_tie_cmd.add_argument("--max-depth", type=int)
    directory_tie_cmd.add_argument("--max-children-per-dir", type=int)
    directory_tie_cmd.add_argument("--max-total-nodes", type=int)
    directory_tie_cmd.add_argument("--progress-interval", type=float, default=60.0)
    directory_tie_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    graph_report_cmd = sub.add_parser("graph-report", help="render graph metadata from an existing plan")
    graph_report_cmd.add_argument("--plan-in", required=True)
    graph_audit_cmd = sub.add_parser(
        "graph-audit",
        help="render a read-only audit of structured-vs-note graph matching from an existing plan",
    )
    graph_audit_cmd.add_argument("--plan-in", required=True)
    graph_family_report_cmd = sub.add_parser(
        "graph-family-report",
        help="render graph metadata for one family from an existing plan",
    )
    graph_family_report_cmd.add_argument("--plan-in", required=True)
    graph_family_report_cmd.add_argument(
        "--family",
        choices=["file", "directory", "dead-gfid", "stale-index"],
        required=True,
    )
    graph_cycle_report_cmd = sub.add_parser(
        "graph-cycle-report",
        help="render graph-cycle status for an existing plan against the saved status baseline",
    )
    graph_cycle_report_cmd.add_argument("--plan-in", required=True)
    graph_cycle_report_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    graph_cycle_status_cmd = sub.add_parser(
        "graph-cycle-status",
        help="render the saved graph-cycle baseline from status without loading a plan",
    )
    graph_cycle_status_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    controller_cycle_cmd = sub.add_parser(
        "controller-cycle-report",
        help="render the combined controller stop/continue decision from saved status",
    )
    controller_cycle_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    repair_cycle_cmd = sub.add_parser(
        "repair-cycle",
        help="render the current repair-cycle recommendation from saved status",
    )
    repair_cycle_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    status_cmd = sub.add_parser("status-report", help="render the current controller status summary")
    status_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    run_cmd = sub.add_parser("apply-run", help="print dry-run commands or execute the ready subset from apply json")
    run_cmd.add_argument("--apply-in", required=True)
    run_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    run_cmd.add_argument("--summary-only", action="store_true")
    run_cmd.add_argument("--action-type")
    run_cmd.add_argument("--path-contains")
    run_cmd.add_argument("--strategy")
    run_cmd.add_argument("--limit", type=int)
    run_cmd.add_argument("--execute", action="store_true")
    run_cmd.add_argument("--stage-only", action="store_true", help="execute only the staging step for file-family repairs")
    run_cmd.add_argument(
        "--parallel-actions",
        type=int,
        default=1,
        help="maximum parallel worker count for planner-assigned safe actions",
    )
    run_cmd.add_argument(
        "--parallel-nice",
        type=int,
        default=5,
        help="niceness level for parallel workers (lower runs first, default: 5)",
    )
    run_cmd.add_argument(
        "--run-dir",
        help="override the controller-local repair run directory (default: a timestamped directory under the work root)",
    )
    run_cmd.add_argument(
        "--execute-ready",
        action="store_true",
        help="execute only ready non-review actions and skip review-only items",
    )
    run_cmd.add_argument(
        "--verify-temp-mount",
        action="store_true",
        help="after execute, stat the repaired logical paths from a temporary mount",
    )
    run_cmd.add_argument(
        "--verify-temp-mount-cleanup",
        action="store_true",
        help="unmount and remove the temporary verification mount after the temp-mount check",
    )
    run_cmd.add_argument(
        "--verify-rescan",
        action="store_true",
        help="after execute, recursively stat the repaired logical roots from a temporary mount",
    )
    run_cmd.add_argument("--results-out")
    run_cmd.add_argument(
        "--archive-backups",
        metavar="PATH",
        help="archive backup artifacts to a tgz at the given path after execution",
    )
    run_cmd.add_argument(
        "--cleanup-backups",
        action="store_true",
        help="remove backup artifacts after execution",
    )
    run_cmd.add_argument("--allow-heal-on", action="store_true")
    run_cmd.add_argument("--heal-repeat-limit", type=int, default=DEFAULT_HEAL_INFO_REPEAT_LIMIT, help="maximum unchanged post-execute heal snapshots before stopping (default: 5)")
    run_cmd.add_argument("--heal-refresh-timeout", type=float, default=DEFAULT_HEAL_INFO_REFRESH_TIMEOUT_SECONDS, help="maximum post-execute heal refresh window in seconds (default: 60)")
    run_cmd.add_argument(
        "--manage-heal",
        action="store_true",
        help="temporarily turn heal off for execution and restore the previous heal settings afterward",
    )
    run_cmd.add_argument(
        "--require-snapshot",
        action="store_true",
        help="refuse execution unless a snapshot or equivalent rollback point is acknowledged",
    )
    run_cmd.add_argument(
        "--snapshot-ack",
        action="store_true",
        help="confirm that a snapshot or equivalent rollback point exists for this execution",
    )
    run_cmd.add_argument("--keep-going", action="store_true")
    run_cmd.add_argument("-y", "--batch", action="store_true")
    run_cmd.add_argument(
        "--skip-health-check",
        action="store_true",
        help="skip the required pre-execution health check (lab/debug only)",
    )
    run_cmd.add_argument(
        "--scope",
        choices=["all", "files", "directories", "metadata", "files,directories", "files,metadata", "directories,metadata"],
        default="all",
        help="limit apply-run output to a repair family",
    )

    heal_cmd = sub.add_parser("heal-control")
    heal_cmd.add_argument("--volume", required=True)
    heal_cmd.add_argument("--mode", choices=["status", "off", "on"], required=True)
    heal_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    performance_cmd = sub.add_parser(
        "heal-performance",
        help="inspect or explicitly tune heal throughput; full namespace heal is never automatic",
    )
    performance_cmd.add_argument("--volume", required=True)
    performance_cmd.add_argument("--ssh-user", default=DEFAULT_SERVICE_USER)
    performance_cmd.add_argument("--connect-timeout", type=float, default=10.0)
    performance_cmd.add_argument("--shd-max-threads", type=int)
    performance_cmd.add_argument("--shd-wait-qlength", type=int)
    performance_cmd.add_argument("--background-self-heal-count", type=int)
    performance_cmd.add_argument("--heal-wait-queue-length", type=int)
    full_heal_group = performance_cmd.add_mutually_exclusive_group()
    full_heal_group.add_argument(
        "--full-heal-once",
        action="store_true",
        help="plan one explicit gluster volume heal <vol> full; requires --execute -y",
    )
    full_heal_group.add_argument(
        "--full-heal-already-run",
        action="store_true",
        help="record that the operator already started or completed full healing; never launches another",
    )
    performance_cmd.add_argument("--execute", action="store_true")
    performance_cmd.add_argument("-y", "--batch", action="store_true")
    performance_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    temp_mount_cmd = sub.add_parser(
        "temp-mount",
        help="create or clean up a temporary verification mount under $GLUSTER_REPAIR_WORK_ROOT/gluster-repair",
    )
    temp_mount_cmd.add_argument("--volume", required=True)
    temp_mount_cmd.add_argument("--source-host")
    temp_mount_cmd.add_argument("--aux-gfid-mount", action="store_true")
    temp_mount_cmd.add_argument("--mount-root", default=str(DEFAULT_TEMP_MOUNT_ROOT))
    temp_mount_cmd.add_argument(
        "--cleanup",
        action="store_true",
        help="unmount and remove the temporary mountpoint instead of creating it",
    )

    restore_cmd = sub.add_parser(
        "backup-restore",
        help="restore archived backup artifacts from a tgz archive",
    )
    restore_cmd.add_argument("--archive", help="path to the backup archive tgz; omit to use the recorded or newest archive")
    restore_cmd.add_argument(
        "--restore-root",
        help="override the restore root when the archive has no manifest metadata",
    )
    restore_cmd.add_argument(
        "--backup-dir",
        default=str(default_backup_archive_dir()),
        help="directory to search when no archive is given",
    )
    restore_cmd.add_argument(
        "--cleanup-archive",
        action="store_true",
        help="remove the archive after a successful restore",
    )
    restore_cmd.add_argument(
        "--on-conflict",
        choices=["prompt", "overwrite", "skip", "rename"],
        default="prompt",
        help="how to handle an existing path during restore",
    )
    restore_cmd.add_argument(
        "--rename-suffix",
        default=".restore",
        help="suffix used when renaming an existing path aside during restore",
    )
    restore_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    matrix_cmd = sub.add_parser("repair-matrix", help="print the situation-to-repair matrix")
    matrix_cmd.add_argument("--json", action="store_true")

    repair_cmd = sub.add_parser(
        "repair",
        help="run the human-friendly repair flow (interactive in a TTY; --preview for read-only)",
        description=(
            "Default: in a TTY, repair runs the easy interactive flow and asks before the ready-safe batch. "
            "Use --preview for read-only, -y for non-interactive safe-auto, or --expert --interactive for expert guidance."
        ),
        epilog=(
            "Examples: gluster-manager repair --volume VOL; "
            "gluster-manager repair --volume VOL --preview; "
            "gluster-manager repair --volume VOL --expert --interactive --execute."
        ),
    )
    configure_simple_repair_parser(repair_cmd)
    return parser


def main(argv: list[str] | None = None) -> int:
    command_args = list(sys.argv[1:] if argv is None else argv)
    if command_args and command_args[0] == "apply-run":
        # Every spelling, including preview/help and plain --execute, shares
        # the same parser, pre-write gates and execution lifecycle.
        return cli_main(command_args)
    args = build_parser().parse_args(command_args)
    if args.cmd == "repair": return run_simple_command(args)
    if args.cmd == "manifest-build":
        mountpoint = args.mountpoint or _default_mountpoint(args.volume)
        brick_paths = {}
        brick_path = args.brick_path or ""
        if not args.brick_path:
            brick_paths = discover_brick_paths(args.volume)
            brick_path = next(iter(dict.fromkeys(brick_paths.values())), "")
        resolved_hosts = _hosts(args.hosts) if args.hosts else discover_brick_hosts(args.volume)
        if args.hosts:
            summary = resolve_via_workers(
                heal_file=args.heal_file,
                volume=args.volume,
                hosts=resolved_hosts,
                brick_path=brick_path,
                brick_paths=brick_paths,
                mountpoint=mountpoint,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                ssh_user=args.ssh_user,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out,
                heal_out=args.heal_out,
                heal_root=args.heal_root,
                heal_latest=args.heal_latest,
                heal_fresh=args.heal_fresh,
                log_path=args.log_out,
                verbose=args.verbose,
                probe_mount=not args.skip_mount_probe,
            )
        else:
            summary = resolve_via_volume(
                heal_file=args.heal_file,
                volume=args.volume,
                brick_path=brick_path,
                brick_paths=brick_paths,
                mountpoint=mountpoint,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                ssh_user=args.ssh_user,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out,
                heal_out=args.heal_out,
                heal_root=args.heal_root,
                heal_latest=args.heal_latest,
                heal_fresh=args.heal_fresh,
                log_path=args.log_out,
                verbose=args.verbose,
                probe_mount=not args.skip_mount_probe,
            )
        update_status(
            args.status_file,
            phase="manifest-built",
            volume=args.volume,
            mountpoint=mountpoint,
            hosts=resolved_hosts,
            brick_path=brick_path,
            brick_paths=brick_paths,
            heal_file=summary.get("heal_file", args.heal_file or ""),
            manifest_out=args.manifest_out,
            observations_out=args.observations_out,
            input_source=summary.get("input_source", "heal_info"),
            requested_seed=summary.get("requested_seed", ""),
            summary=summary,
        )
        print(render_manifest_summary(summary))
        return 0
    if args.cmd in {"repair-meta", "evidence-build"}:
        _print_status_warnings(args.status_file)
        mount_probe_mode = "off" if args.skip_mount_probe else getattr(args, "mount_probe", "auto")
        probe_mount = mount_probe_mode == "on"
        input_kind = "path"
        if args.backend_path:
            if not args.volume:
                print("ERROR: repair-meta --backend-path requires --volume", file=sys.stderr)
                return 2
            summary = resolve_via_backend_path(
                volume=args.volume,
                backend_path=args.backend_path,
                mountpoint=args.mountpoint,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out or "",
                ssh_user=args.ssh_user,
                verbose=args.verbose,
            )
            probe_mount = False
            input_kind = "backend-path"
        elif args.gfid:
            if not args.volume:
                print("ERROR: repair-meta --gfid requires --volume", file=sys.stderr)
                return 2
            summary = resolve_via_gfid(
                volume=args.volume,
                gfid=args.gfid,
                mountpoint=args.mountpoint,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out or "",
                ssh_user=args.ssh_user,
                verbose=args.verbose,
            )
            probe_mount = False
            input_kind = "gfid"
        elif args.gfid_child:
            if not args.volume:
                print("ERROR: repair-meta --gfid-child requires --volume", file=sys.stderr)
                return 2
            summary = resolve_via_gfid_child(
                volume=args.volume,
                gfid_child=args.gfid_child,
                mountpoint=args.mountpoint,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out or "",
                ssh_user=args.ssh_user,
                verbose=args.verbose,
            )
            probe_mount = False
            input_kind = "gfid-child"
        elif args.index_entry:
            if not args.volume:
                print("ERROR: repair-meta --index-entry requires --volume", file=sys.stderr)
                return 2
            summary = resolve_via_index_entry(
                volume=args.volume,
                index_entry=args.index_entry,
                mountpoint=args.mountpoint,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out or "",
                ssh_user=args.ssh_user,
                verbose=args.verbose,
            )
            probe_mount = False
            input_kind = "index-entry"
        else:
            summary = resolve_via_path(
                path=args.path,
                resolver_path=args.resolver_path,
                worker_path=args.worker_path,
                manifest_out=args.manifest_out,
                observations_out=args.observations_out or "",
                ssh_user=args.ssh_user,
                verbose=args.verbose,
                probe_mount=probe_mount,
            )
        volume = str(summary.get("volume") or args.volume or "").strip()
        repair_meta_input = str(
            summary.get("repair_meta_input")
            or args.path
            or args.backend_path
            or args.gfid
            or args.gfid_child
            or args.index_entry
            or ""
        ).strip()
        update_status(
            args.status_file,
            phase=f"{args.cmd}-built",
            volume=volume,
            mountpoint=str(summary.get("mountpoint") or "").strip(),
            hosts=discover_brick_hosts(volume) if volume else [],
            brick_path=str(summary.get("brick_path") or "").strip(),
            manifest_out=args.manifest_out,
            observations_out=args.observations_out or "",
            repair_meta_path=str(summary.get("path") or repair_meta_input).strip(),
            repair_meta_backend_path=str(summary.get("backend_path") or "").strip(),
            repair_meta_evidence_route=str(summary.get("evidence_route") or input_kind).strip(),
            repair_meta_source=str(summary.get("source") or "").strip(),
            repair_meta_logical_path=str(summary.get("logical_path") or "").strip(),
            repair_meta_input=repair_meta_input,
            repair_meta_input_kind=str(summary.get("repair_meta_input_kind") or input_kind).strip(),
            repair_meta_mount_probe=probe_mount,
            repair_meta_mount_probe_mode=mount_probe_mode,
            input_source=str(summary.get("input_source") or "").strip(),
            requested_seed=str(summary.get("requested_seed") or repair_meta_input).strip(),
        )
        print(render_manifest_summary(summary))
        return 0

    if args.cmd == "plan-build":
        _print_status_warnings(args.status_file)
        manifest_payload = load_plan(args.manifest_in)
        manifest = load_manifest(args.manifest_in, payload=manifest_payload)
        mountpoint = args.mountpoint
        if not mountpoint:
            mountpoint = str((load_status(args.status_file).get("mountpoint") or "")).strip()
        if not mountpoint:
            print(
                "ERROR: plan-build needs --mountpoint or a prior manifest-build status entry with mountpoint",
                file=sys.stderr,
            )
            return 2
        plan = build_plan(
            manifest,
            mountpoint=mountpoint,
            split_brain_policy=args.policy_entry_split_brain_file,
        )
        write_plan(args.plan_out, plan, manifest_in=args.manifest_in, manifest_payload=manifest_payload)
        summary = summarize_plan(plan)
        update_status(
            args.status_file,
            phase="plan-built",
            manifest_in=args.manifest_in,
            plan_out=args.plan_out,
            split_brain_policy=args.policy_entry_split_brain_file,
            controller_cycle={},
            final_check={},
            final_check_guard={},
            controller_cycle_report="",
            **build_graph_cycle_baseline_status(plan),
            summary=summary,
        )
        print(render_plan_summary(summary))
        return 0
    if args.cmd == "graph-report":
        plan = load_plan(args.plan_in)
        print(render_graph_report(plan))
        return 0

    if args.cmd == "graph-audit":
        plan = load_plan(args.plan_in)
        print(render_graph_audit_report(load_plan_actions(plan)))
        return 0

    if args.cmd == "graph-family-report":
        plan = load_plan(args.plan_in)
        print(render_graph_family_report(plan, args.family))
        return 0
    if args.cmd == "graph-cycle-report":
        plan = load_plan(args.plan_in)
        status = load_status(args.status_file)
        graph_cycle_status = build_graph_cycle_status(status, load_plan_actions(plan))
        print(graph_cycle_status["graph_cycle_report"])
        return 0
    if args.cmd == "graph-cycle-status":
        status = load_status(args.status_file)
        print(render_graph_cycle_status_summary(status))
        print(render_graph_cycle_status_report(status))
        return 0
    if args.cmd == "controller-cycle-report":
        status = load_status(args.status_file)
        print(render_controller_cycle_report(status))
        return 0
    if args.cmd == "repair-cycle":
        status = load_status(args.status_file)
        print(render_controller_cycle_report(status))
        return 0
    if args.cmd == "status-report":
        status = load_status(args.status_file)
        print(render_status_report(status))
        return 0
    if args.cmd == "apply-build":
        _print_status_warnings(args.status_file)
        current_status = load_status(args.status_file)
        plan = load_plan(args.plan_in)
        try:
            validate_plan_context(plan, volume=str(current_status.get("volume") or ""),
                                  brick_path=str(current_status.get("brick_path") or ""))
        except BindingError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        results = build_apply_results(
            plan,
            execution_mode="dry-run",
            backup_root=args.backup_root,
            backup_mode=args.backup_mode,
            batch=args.batch,
            review_policies=_review_policies_from_args(args),
            split_brain_native_policy=_split_brain_native_policy_from_args(args),
            decisions=load_decisions(args.decision_file) if args.decision_file else None,
            volume=str(current_status.get("volume") or "").strip(),
            brick_path=str(current_status.get("brick_path") or "").strip(),
            worker_path=str(current_status.get("worker_path") or DEFAULT_WORKER_PATH),
            ssh_user=str(current_status.get("ssh_user") or DEFAULT_SERVICE_USER),
            scope=_scope_from_args(args),
        )
        results = filter_apply_results(
            results,
            action_type=args.action_type,
            path_contains=args.path_contains,
            strategy=args.strategy,
            limit=args.limit,
            include_dependencies=True,
            scope=args.scope,
        )
        controller_cycle = current_status.get("controller_cycle") or {}
        write_apply_results(
            args.apply_out,
            results,
            decision_file=args.decision_file or "",
            controller_cycle=controller_cycle if isinstance(controller_cycle, dict) else {},
            plan_in=args.plan_in,
            plan_payload=plan,
        )
        summary = summarize_apply_results(
            results,
            controller_cycle=controller_cycle if isinstance(controller_cycle, dict) else {},
        )
        update_status(
            args.status_file,
            phase="apply-built",
            apply_in=args.plan_in,
            apply_out=args.apply_out,
            backup_root=args.backup_root or "",
            backup_mode=args.backup_mode,
            batch=args.batch,
            decision_file=args.decision_file or "",
            selection={
                "action_type": args.action_type or "",
                "path_contains": args.path_contains or "",
                "strategy": args.strategy or "",
                "limit": args.limit or 0,
            },
            review_policies=_review_policies_from_args(args),
            split_brain_native_policy=_split_brain_native_policy_from_args(args),
            summary=summary,
        )
        print(render_apply_summary(summary))
        return 0
    if args.cmd == "apply-preflight":
        _print_status_warnings(args.status_file)
        payload = json.loads(open(args.apply_in).read())
        report = build_preflight_report(payload)
        write_preflight_report(args.preflight_out, report)
        update_status(
            args.status_file,
            phase="apply-preflight",
            apply_in=args.apply_in,
            preflight_out=args.preflight_out,
            preflight=report,
        )
        print(json.dumps(report, indent=2))
        return 0
    if args.cmd == "manager-preflight":
        _print_status_warnings(args.status_file)
        report = build_manager_health_report(
            args.volume,
            ssh_user=args.ssh_user,
            connect_timeout=args.connect_timeout,
        )
        write_manager_preflight_report(args.preflight_out, report)
        update_status(
            args.status_file,
            phase="manager-preflight",
            volume=args.volume,
            ssh_user=args.ssh_user,
            connect_timeout=args.connect_timeout,
            manager_preflight_out=args.preflight_out,
            manager_preflight=report,
            health_check=report,
            health_check_checked_at=report.get("checked_at", ""),
        )
        print(render_manager_preflight_summary(report))
        for message in report.get("summary", {}).get("warnings", []):
            print(f"WARNING: {message}", file=sys.stderr)
        return 0 if report.get("summary", {}).get("ready") else 2
    if args.cmd == "health-check":
        _print_status_warnings(args.status_file)
        if not args.health_out:
            args.health_out = str(default_health_report_path(args.volume))
        report = build_manager_health_report(
            args.volume,
            ssh_user=args.ssh_user,
            connect_timeout=args.connect_timeout,
        )
        write_manager_preflight_report(args.health_out, report)
        update_status(
            args.status_file,
            phase="health-check",
            volume=args.volume,
            ssh_user=args.ssh_user,
            connect_timeout=args.connect_timeout,
            health_out=args.health_out,
            health_check=report,
            health_check_checked_at=report.get("checked_at", ""),
        )
        print(render_manager_preflight_summary(report))
        for message in report.get("summary", {}).get("warnings", []):
            print(f"WARNING: {message}", file=sys.stderr)
        return 0 if report.get("summary", {}).get("ready") else 2
    if args.cmd == "decision-build":
        _print_status_warnings(args.status_file)
        current_status = load_status(args.status_file)
        plan = load_plan(args.plan_in)
        report = build_decision_report(
            plan,
            volume=args.volume,
            worker_path=args.worker_path,
            ssh_user=args.ssh_user,
            hosts=_hosts(args.hosts) if args.hosts else None,
            status=current_status,
        )
        write_decision_report(args.report_out, report)
        if args.decision_out:
            write_decision_file(args.decision_out, report)
        update_status(
            args.status_file,
            phase="decision-built",
            plan_in=args.plan_in,
            decision_report_out=args.report_out,
            decision_out=args.decision_out or "",
            controller_cycle=report.get("controller_cycle", {}),
            controller_cycle_report=report.get("controller_cycle_report", ""),
            summary={
                "ambiguous_actions": report.get("ambiguous_actions", 0),
                "outcomes": report.get("outcomes", {}),
            },
        )
        print(json.dumps(report.get("outcomes", {}), indent=2))
        return 0
    if args.cmd == "directory-tie-build":
        _print_status_warnings(args.status_file)
        if args.canary_volume or args.canary_name:
            if not args.canary_volume or not args.canary_name:
                print(
                    "ERROR: directory-tie-build canary mode requires both --canary-volume and --canary-name",
                    file=sys.stderr,
                )
                return 2
            if args.plan_in:
                print(
                    "ERROR: directory-tie-build uses either --plan-in or --canary-volume/--canary-name, not both",
                    file=sys.stderr,
                )
                return 2
            plan = build_directory_tie_plan_from_canary_state(volume=args.canary_volume, scenario=args.canary_name)
        else:
            if not args.plan_in:
                print(
                    "ERROR: directory-tie-build needs --plan-in or live canary inputs",
                    file=sys.stderr,
                )
                return 2
            plan = load_plan(args.plan_in)
        plan_source = str(plan.get("input_source") or args.plan_in or "").strip()
        budget = resolve_directory_tie_budget(
            args.max_depth,
            args.max_children_per_dir,
            args.max_total_nodes,
        )
        while True:
            report = build_directory_tie_report(
                plan,
                max_depth=budget[0],
                max_children_per_dir=budget[1],
                max_total_nodes=budget[2],
                progress_stream=sys.stderr,
                progress_interval_seconds=args.progress_interval,
            )
            if not args.interactive:
                break
            next_budget = prompt_directory_tie_budget_override(
                report,
                current_budget=budget,
                output_stream=sys.stderr,
            )
            if next_budget is None or next_budget == budget:
                break
            budget = next_budget
        write_directory_tie_report(args.report_out, report)
        selections = prompt_directory_tie_selections(report, output_stream=sys.stderr) if args.interactive else None
        if args.decision_out:
            decision_payload = {"decisions": build_directory_tie_decisions(report, selections=selections)}
            write_decision_file(args.decision_out, decision_payload)
        update_status(
            args.status_file,
            phase="directory-tie-built",
            plan_in=plan_source,
            directory_tie_report_out=args.report_out,
            directory_tie_decision_out=args.decision_out or "",
            directory_tie_interactive=args.interactive,
            directory_tie_input_source=plan_source,
            directory_tie_budget={
                "max_depth": budget[0],
                "max_children_per_dir": budget[1],
                "max_total_nodes": budget[2],
            },
            summary={
                "directory_tie_actions": report.get("directory_tie_actions", 0),
                "branches": report.get("branch_counts", {}),
            },
        )
        print(render_directory_tie_report(report))
        return 0
    if args.cmd == "heal-control":
        if args.mode == "status":
            settings = get_heal_settings(args.volume)
        elif args.mode == "off":
            settings = set_heal_settings(args.volume, enabled=False)
        else:
            settings = set_heal_settings(args.volume, enabled=True)
        report = summarize_heal_settings(args.volume, settings)
        update_status(
            args.status_file,
            phase=f"heal-{args.mode}",
            volume=args.volume,
            heal=report,
            heal_restore_required=args.mode == "off",
        )
        print(json.dumps(report, indent=2))
        return 0
    if args.cmd == "heal-performance":
        requested = {
            "cluster.shd-max-threads": args.shd_max_threads,
            "cluster.shd-wait-qlength": args.shd_wait_qlength,
            "cluster.background-self-heal-count": args.background_self_heal_count,
            "cluster.heal-wait-queue-length": args.heal_wait_queue_length,
        }
        full_heal_mode = "already-run" if args.full_heal_already_run else (
            "once" if args.full_heal_once else "never"
        )
        report = execute_heal_performance(
            args.volume,
            ssh_user=args.ssh_user,
            connect_timeout=args.connect_timeout,
            requested=requested,
            full_heal_mode=full_heal_mode,
            execute=args.execute,
            batch=args.batch,
        )
        update_status(
            args.status_file,
            phase="heal-performance",
            volume=args.volume,
            heal_performance=report,
        )
        print(json.dumps(report, indent=2))
        return 0 if not report.get("errors") else 2
    if args.cmd == "temp-mount":
        result = run_temp_mount(
            args.volume,
            mount_root=args.mount_root,
            source_host=args.source_host,
            aux_gfid_mount=args.aux_gfid_mount,
            cleanup=args.cleanup,
        )
        print(render_temp_mount_result(result))
        return 0
    if args.cmd == "backup-restore":
        _print_status_warnings(args.status_file)
        current_status = load_status(args.status_file)
        status_archive_path = ""
        backup_maintenance = current_status.get("backup_maintenance")
        if isinstance(backup_maintenance, dict):
            archive_report = backup_maintenance.get("archive")
            if isinstance(archive_report, dict):
                status_archive_path = str(archive_report.get("archive_path") or "").strip()
        report = restore_backup_archive(
            args.archive or "",
            restore_root=args.restore_root or "",
            cleanup_archive=args.cleanup_archive,
            on_conflict=args.on_conflict,
            rename_suffix=args.rename_suffix,
            status_archive_path=status_archive_path,
            backup_dir=args.backup_dir,
        )
        update_status(
            args.status_file,
            phase="backup-restored",
            backup_archive=report.get("archive_path") or args.archive or status_archive_path,
            backup_restore=report,
        )
        if report.get("archive_removed"):
            print(f"NOTICE: removed archive {report.get('archive_path')}", file=sys.stderr)
        for warning in report.get("warnings", []):
            print(f"WARNING: {warning}", file=sys.stderr)
        for error in report.get("errors", []):
            print(f"ERROR: restore failed: {error}", file=sys.stderr)
        print(json.dumps(report, indent=2))
        return 0 if not report.get("errors") else 2
    if args.cmd == "repair-matrix":
        if args.json:
            print(render_repair_matrix_json())
        else:
            print(render_repair_matrix())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
