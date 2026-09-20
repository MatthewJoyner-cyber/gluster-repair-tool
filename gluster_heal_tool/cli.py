# SPDX-License-Identifier: GPL-2.0-only
"""Gluster repair CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from threading import Lock

from .heal_parser import parse_heal_info_file
from .apply import (
    DEFAULT_REVIEW_POLICIES,
    DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
    collect_temp_mount_verification_paths,
    build_apply_results,
    build_preflight_report,
    execute_apply_results,
    filter_apply_results,
    filter_execute_ready_results,
    load_decisions,
    load_apply_payload,
    load_apply_results,
    load_plan as load_plan_payload,
    render_apply_summary,
    render_apply_run,
    summarize_apply_results,
    validate_execute_results,
    write_apply_results,
    write_execute_report,
    write_preflight_report,
)
from .heal_guard import (
    DEFAULT_HEAL_INFO_REPEAT_LIMIT, DEFAULT_HEAL_INFO_REFRESH_TIMEOUT_SECONDS,
    assess_heal_info_repeat,
    build_heal_info_snapshot,
)
from .decisions import build_decision_report, write_decision_file, write_decision_report
from .backup_maintenance import manage_backup_artifacts, restore_backup_archive
from .install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from .manager import (
    build_manager_preflight_report,
    build_manager_health_report,
    render_manifest_summary,
    render_manager_preflight_summary,
    refresh_manager_health_check,
    resolve_heal_snapshot,
    resolve_via_backend_path,
    resolve_via_gfid,
    resolve_via_gfid_child,
    resolve_via_index_entry,
    resolve_via_path,
    write_manager_preflight_report,
)
from .repair_matrix import render_repair_matrix, render_repair_matrix_json
from .apply_reporting import needs_acl_temp_mount
from .manifest import build_manifest, load_manifest, write_manifest
from .apply_binding import BindingError, validate_apply_binding, validate_live_topology, validate_plan_context
from .execution_journal import ExecutionJournalError, check_run_reusable
from .directory_tie import (
    build_directory_tie_decisions,
    build_directory_tie_report,
    prompt_directory_tie_budget_override,
    prompt_directory_tie_selections,
    resolve_directory_tie_budget,
    render_directory_tie_report,
    write_directory_tie_report,
)
from .planner import build_plan, render_plan_summary, summarize_plan, write_plan
from .planner_graph import (
    build_graph_cycle_baseline_status,
    build_graph_cycle_status,
    render_graph_cycle_status_report,
    render_graph_cycle_status_summary,
    render_graph_report,
)
from .graph_audit import render_graph_audit_report
from .controller_cycle_report import (
    build_controller_cycle_status,
    controller_cycle_gate_warnings,
    render_controller_cycle_report,
)
from .graph_family_report import render_graph_family_report
from .planner_payload import load_plan_actions
from .resolver import CachedResolver, LiveResolver, dump_observations
from .status_report import render_status_report
from .temp_mount import (
    DEFAULT_TEMP_MOUNT_ROOT,
    render_temp_mount_result,
    run_temp_mount,
    verify_temp_mount_rescan,
    verify_temp_mount_targets,
)
from .controller_paths import default_backup_archive_dir, default_health_report_path
from .status import (
    DEFAULT_STATUS_FILE,
    health_check_warnings,
    load_status,
    render_reversibility_summary,
    snapshot_gate_warnings,
    status_warnings,
    update_status,
)
from .simple_mode import configure_simple_repair_parser, run_simple_command
from .version import __version__
from .volume import (
    get_heal_info_text,
    discover_brick_hosts,
    get_heal_settings,
    run_heal,
    set_heal_settings,
    set_heal_settings_exact,
    summarize_heal_settings,
    get_volume_info,
    parse_brick_roles,
    discover_brick_paths,
)
from .heal_performance import execute_heal_performance


def _hosts(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _refresh_post_execute_heal_preserving_settings(
    volume: str,
    current_status: dict[str, object],
    *,
    repeat_limit: int = DEFAULT_HEAL_INFO_REPEAT_LIMIT,
    total_timeout_seconds: float = DEFAULT_HEAL_INFO_REFRESH_TIMEOUT_SECONDS,
) -> tuple[dict[str, object], dict[str, object]]:
    original_settings = get_heal_settings(volume)
    heal_summary = summarize_heal_settings(volume, original_settings)
    restore_required = not bool(heal_summary.get("all_on_effective"))
    try:
        if restore_required:
            print(f"NOTICE: temporarily enabling heal for the post-execute index refresh on {volume}", file=sys.stderr)
            set_heal_settings(volume, enabled=True)
        return _refresh_post_execute_heal(
            volume, current_status, repeat_limit=repeat_limit,
            total_timeout_seconds=total_timeout_seconds,
        )
    finally:
        if restore_required:
            set_heal_settings_exact(volume, original_settings)
            print(f"NOTICE: restored the pre-execute heal settings on {volume}", file=sys.stderr)


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
        "directory_children": args.policy_directory_children,
        "type_mismatch": args.policy_type_mismatch,
    }


def _split_brain_native_policy_from_args(args: argparse.Namespace) -> str:
    return args.policy_entry_split_brain_native


def _print_status_warnings(status_file: str) -> None:
    for message in status_warnings(status_file):
        print(f"WARNING: {message}", file=sys.stderr)


def _execution_progress_callback(status_file: str, run_dir: Path):
    output_lock = Lock()
    resume_hint = f"gluster-manager repair --status {status_file} --granular"

    def report(event: dict[str, object]) -> None:
        with output_lock:
            progress = dict(event)
            progress["resume_hint"] = resume_hint
            update_status(
                status_file,
                phase="apply-run-execution",
                execution_run_dir=str(run_dir),
                execution_progress=progress,
                execution_resume=resume_hint,
            )
            phase = str(progress.get("phase") or "execution")
            elapsed = float(progress.get("elapsed_seconds") or 0.0)
            activity = str(progress.get("last_activity") or progress.get("message") or "working").replace("\n", " ")
            print(
                "PROGRESS: "
                f"phase={phase} elapsed={elapsed:.1f}s last_activity={activity} "
                "interrupt=Ctrl-C; inspect artifacts before resuming",
                file=sys.stderr,
                flush=True,
            )

    return report


def _reversibility_status(require_snapshot: bool, snapshot_ack: bool) -> dict[str, bool]:
    return {
        "snapshot_required": bool(require_snapshot),
        "snapshot_acknowledged": bool(snapshot_ack),
    }


def _unique_heal_entries(volume: str) -> dict[str, object]:
    return build_heal_info_snapshot(volume)


def _trigger_post_execute_heal(volume: str, *, retries: int = 3, pause_seconds: float = 2.0) -> dict[str, object]:
    disabled_markers = (
        "self-heal-daemon is disabled",
        "heal will not be triggered",
        "heal-related volume options are off",
    )
    last_error = ""
    for attempt in range(retries):
        try:
            run_heal(volume, settle_seconds=0)
            if pause_seconds > 0:
                time.sleep(pause_seconds)
            return {"triggered": True, "error": ""}
        except Exception as exc:  # pragma: no cover - exercised via tests
            last_error = str(exc).strip() or "failed to launch gluster heal"
            lowered = last_error.lower()
            if attempt + 1 < retries and any(marker in lowered for marker in disabled_markers):
                time.sleep(pause_seconds)
                continue
            break
    return {"triggered": False, "error": last_error}


def _refresh_post_execute_heal(
    volume: str,
    current_status: dict[str, object],
    *,
    repeat_limit: int = DEFAULT_HEAL_INFO_REPEAT_LIMIT, total_timeout_seconds: float = DEFAULT_HEAL_INFO_REFRESH_TIMEOUT_SECONDS,
) -> tuple[dict[str, object], dict[str, object]]:
    final_check: dict[str, object] = {}
    final_check_guard: dict[str, object] = {}
    if not volume:
        return final_check, final_check_guard

    previous_snapshot_status = current_status
    max_post_execute_refreshes = max(1, repeat_limit)
    refresh_timeout = max(1.0, float(total_timeout_seconds))
    refresh_started = time.monotonic()
    timeout_hit = False
    for attempt in range(max_post_execute_refreshes):
        if attempt and time.monotonic() - refresh_started >= refresh_timeout:
            timeout_hit = True
            print(f"WARNING: post-execute heal refresh deadline reached after {refresh_timeout:.1f}s", file=sys.stderr)
            break
        trigger_result = _trigger_post_execute_heal(volume)
        if not trigger_result.get('triggered'):
            print(
                'WARNING: post-execute heal launch failed: {}'.format(
                    trigger_result.get('error') or 'unknown error'
                ),
                file=sys.stderr,
            )
            break

        final_check = _unique_heal_entries(volume)
        if not final_check.get('available'):
            print(
                'WARNING: post-execute heal info check unavailable: {}'.format(
                    final_check.get('error') or 'unknown error'
                ),
                file=sys.stderr,
            )
            break

        unique_count = int(final_check.get('unique_count', 0))
        final_check_guard = assess_heal_info_repeat(
            previous_snapshot_status,
            final_check,
            repeat_limit=repeat_limit,
        )
        previous_snapshot_status = {
            **previous_snapshot_status,
            'final_check': final_check,
            'final_check_guard': final_check_guard,
        }
        if not unique_count:
            break

        sample_paths = ', '.join(str(path) for path in final_check.get('sample_paths', []))
        repeat_count = int(final_check_guard.get('repeat_count', 0))
        if attempt + 1 < max_post_execute_refreshes and not final_check_guard.get('hit_limit'):
            print(
                'NOTICE: post-execute heal info still reports {} unique entries; retrying index heal once more before rebuilding manifest-build and plan-build'.format(
                    unique_count
                ),
                file=sys.stderr,
            )
            if sample_paths:
                print('NOTICE: sample entries: {}'.format(sample_paths), file=sys.stderr)
            continue

        print(
            'NOTICE: post-execute heal info still reports {} unique entries; rerun manifest-build and plan-build on the fresh snapshot'.format(
                unique_count
            ),
            file=sys.stderr,
        )
        if repeat_count > 1:
            print(
                'NOTICE: post-execute heal info shape is unchanged for {} consecutive checks'.format(
                    repeat_count
                ),
                file=sys.stderr,
            )
        if final_check_guard.get('hit_limit'):
            print(
                'WARNING: post-execute heal info shape has not changed for {} consecutive checks; stop repeating the same repair/verify cycle and rebuild manifest-build and plan-build'.format(
                    repeat_count
                ),
                file=sys.stderr,
            )
        if sample_paths:
            print('NOTICE: sample entries: {}'.format(sample_paths), file=sys.stderr)
        break

    if timeout_hit:
        final_check_guard = {
            **final_check_guard,
            "timeout_hit": True,
            "hit_limit": True,
            "timeout_seconds": refresh_timeout,
            "warning": f"post-execute heal refresh deadline reached after {refresh_timeout:.1f}s",
        }
    return final_check, final_check_guard


def _verify_post_execute_temp_mount(
    volume: str,
    results: list[object],
    *,
    cleanup: bool = False,
) -> dict[str, object]:
    acl_mount = needs_acl_temp_mount(results)  # type: ignore[arg-type]
    paths = collect_temp_mount_verification_paths(results)  # type: ignore[arg-type]
    if not paths:
        return {
            "available": False,
            "paths": [],
            "acl_mount": acl_mount,
            "summary": {"paths_checked": 0, "paths_ok": 0, "paths_failed": 0},
            "error": "",
        }
    try:
        report = verify_temp_mount_targets(volume, paths, cleanup=cleanup, acl_mount=acl_mount)
    except Exception as exc:  # pragma: no cover - best-effort live check
        return {
            "available": False,
            "paths": paths,
            "acl_mount": acl_mount,
            "summary": {"paths_checked": 0, "paths_ok": 0, "paths_failed": 0},
            "error": str(exc),
        }
    report["available"] = True
    report["acl_mount"] = acl_mount
    report["paths"] = paths
    report["error"] = ""
    return report


def _verify_post_execute_temp_rescan(
    volume: str,
    results: list[object],
    *,
    cleanup: bool = False,
) -> dict[str, object]:
    acl_mount = needs_acl_temp_mount(results)  # type: ignore[arg-type]
    paths = collect_temp_mount_verification_paths(results)  # type: ignore[arg-type]
    if not paths:
        return {
            "available": False,
            "paths": [],
            "acl_mount": acl_mount,
            "summary": {"roots_checked": 0, "nodes_checked": 0, "nodes_ok": 0, "nodes_failed": 0},
            "error": "",
        }
    try:
        report = verify_temp_mount_rescan(volume, paths, cleanup=cleanup, acl_mount=acl_mount)
    except Exception as exc:  # pragma: no cover - best-effort live check
        return {
            "available": False,
            "paths": paths,
            "acl_mount": acl_mount,
            "summary": {"roots_checked": 0, "nodes_checked": 0, "nodes_ok": 0, "nodes_failed": 0},
            "error": str(exc),
        }
    report["available"] = True
    report["acl_mount"] = acl_mount
    report["paths"] = paths
    report["error"] = ""
    return report


def _default_mountpoint(volume: str) -> str:
    volume = volume.strip().strip("/")
    return f"/{volume}" if volume else "/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gluster-heal-tool",
        description="Gluster repair tool: human-friendly repair by default, with explicit safe-auto and expert workflows.",
        epilog=(
            "Human flow: gluster-manager repair --volume VOL. In a TTY it previews, explains, and asks before the ready-safe batch. "
            "Use --preview for read-only, -y for the safe-auto subset, or --expert --interactive --execute for expert-guided execution."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    manifest_cmd = sub.add_parser("manifest-build", help="build logical manifest from heal output")
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
    manifest_cmd.add_argument(
        "--skip-heal-refresh",
        action="store_true",
        help="reuse the live heal snapshot without launching a fresh heal crawl first",
    )
    manifest_cmd.add_argument("--volume", required=True)
    manifest_cmd.add_argument("--hosts")
    manifest_cmd.add_argument("--brick-path")
    manifest_cmd.add_argument("--mountpoint")
    manifest_cmd.add_argument("--resolver-path", default=str(DEFAULT_RESOLVER_PATH))
    manifest_cmd.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="SSH login user on the brick hosts (default: gluster-repair)",
    )
    manifest_cmd.add_argument("--manifest-out", required=True)
    manifest_cmd.add_argument("--observations-out")
    manifest_cmd.add_argument("--observations-in")
    manifest_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    manifest_cmd.add_argument("--verbose", action="store_true")

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
    _configure_evidence_build_parser(evidence_build_cmd)

    repair_meta_cmd = sub.add_parser(
        "repair-meta",
        help="compatibility alias for evidence-build",
    )
    _configure_evidence_build_parser(repair_meta_cmd)

    plan_cmd = sub.add_parser("plan-build", help="build dry-run plan from manifest")
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
    apply_cmd.add_argument(
        "--scope",
        choices=["all", "files", "directories", "metadata", "files,directories", "files,metadata", "directories,metadata"],
        default="all",
        help="limit apply-build output to a repair family",
    )
    apply_cmd.add_argument("-y", "--batch", action="store_true")
    apply_cmd.add_argument(
        "--policy-entry-split-brain-file",
        choices=["auto", "majority", "mtime", "ctime", "size", "review", "off", "skip", "replace", "quarantine"],
        default=DEFAULT_REVIEW_POLICIES["entry_split_brain_file"],
        help="split-brain file policy: auto=majority then mtime, or choose a specific fallback",
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
        choices=["merge", "quarantine", "rename", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["directory_gfid_conflict"],
        help="directory GFID conflict policy: merge when the bounded trees collapse cleanly, quarantine for the reversible quarantine test path, rename as a legacy alias, or keep review/skip",
    )
    apply_cmd.add_argument(
        "--policy-directory-metadata",
        choices=["review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["directory_metadata"],
    )
    apply_cmd.add_argument(
        "--policy-directory-children",
        choices=["auto", "review", "skip"],
        default=DEFAULT_REVIEW_POLICIES["directory_children"],
        help="directory child-gap policy: auto promotes executable child reconciliations",
    )
    apply_cmd.add_argument(
        "--policy-type-mismatch",
        choices=["review", "quarantine-both", "quarantine-loser"],
        default=DEFAULT_REVIEW_POLICIES["type_mismatch"],
        help="file-vs-directory mismatch policy: review, quarantine both sides, or quarantine a completely observed older side",
    )
    apply_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    preflight_cmd = sub.add_parser("apply-preflight", help="check local space against apply estimates")
    preflight_cmd.add_argument("--apply-in", required=True)
    preflight_cmd.add_argument("--preflight-out", required=True)
    preflight_cmd.add_argument(
        "--require-snapshot",
        action="store_true",
        help="record that this run should use the stricter snapshot/rollback gate before execute",
    )
    preflight_cmd.add_argument(
        "--snapshot-ack",
        action="store_true",
        help="record that a snapshot or equivalent rollback point has been taken for this run",
    )
    preflight_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    manager_preflight_cmd = sub.add_parser(
        "manager-preflight",
        help="check SSH reachability from the manager host to every brick host",
    )
    manager_preflight_cmd.add_argument("--volume", required=True)
    manager_preflight_cmd.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="SSH login user on the brick hosts (default: gluster-repair)",
    )
    manager_preflight_cmd.add_argument("--connect-timeout", type=float, default=10.0)
    manager_preflight_cmd.add_argument(
        "--require-snapshot",
        action="store_true",
        help="record that the health check should also treat snapshot confirmation as required",
    )
    manager_preflight_cmd.add_argument(
        "--snapshot-ack",
        action="store_true",
        help="record that a snapshot or equivalent rollback point has been taken for this health check",
    )
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
        "--require-snapshot",
        action="store_true",
        help="record that this health check should also treat snapshot confirmation as required",
    )
    health_cmd.add_argument(
        "--snapshot-ack",
        action="store_true",
        help="record that a snapshot or equivalent rollback point has been taken for this health check",
    )
    health_cmd.add_argument(
        "--health-out",
        help="where to save the health-check report; defaults to $GLUSTER_REPAIR_WORK_ROOT/health-check/<volume>-health-check.json",
    )
    health_cmd.add_argument("--status-file", default=DEFAULT_STATUS_FILE)

    decision_cmd = sub.add_parser("decision-build", help="build machine-readable decisions for ambiguous review cases")
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
    directory_tie_cmd.add_argument("--plan-in", required=True)
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
    run_cmd.add_argument(
        "--scope",
        choices=["all", "files", "directories", "metadata", "files,directories", "files,metadata", "directories,metadata"],
        default="all",
        help="limit apply-run output to a repair family",
    )
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
        help="opt into the stricter gate that refuses execute unless a snapshot or equivalent rollback point is acknowledged",
    )
    run_cmd.add_argument(
        "--snapshot-ack",
        action="store_true",
        help="confirm that a snapshot or equivalent rollback point exists for this execution",
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
    run_cmd.add_argument("--keep-going", action="store_true")
    run_cmd.add_argument("-y", "--batch", action="store_true")
    run_cmd.add_argument(
        "--skip-health-check",
        action="store_true",
        help="skip the required pre-execution health check (lab/debug only)",
    )

    heal_cmd = sub.add_parser("heal-control", help="show or change Gluster heal-related settings")
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
    temp_mount_cmd.add_argument("--acl-mount", action="store_true", help="request ACL support on the temporary Gluster mount")
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
        "--preview",
        action="store_true",
        help="inspect planned local and remote restore operations without writing them",
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
    args = build_parser().parse_args(argv)
    if args.cmd == "repair": return run_simple_command(args)
    if args.cmd == "manifest-build":
        mountpoint = args.mountpoint or _default_mountpoint(args.volume)
        heal_file = resolve_heal_snapshot(
            args.volume,
            heal_file=args.heal_file,
            heal_latest=args.heal_latest,
            heal_fresh=args.heal_fresh,
            heal_out=args.heal_out,
            heal_root=args.heal_root,
            heal_refresh=not args.skip_heal_refresh,
        )
        heal_entries = parse_heal_info_file(heal_file)
        if args.hosts:
            hosts = _hosts(args.hosts)
        else:
            hosts = None
        resolved_hosts = hosts or discover_brick_hosts(args.volume)
        brick_paths = {}
        brick_path = args.brick_path or ""
        if not args.brick_path:
            brick_paths = discover_brick_paths(args.volume)
            brick_path = next(iter(dict.fromkeys(brick_paths.values())), "")
        if args.observations_in:
            resolver = CachedResolver.from_file(args.observations_in)
        else:
            resolver = LiveResolver(
                volume=args.volume,
                hosts=resolved_hosts,
                brick_path=brick_path,
                mountpoint=mountpoint,
                brick_paths=brick_paths,
                resolver_path=args.resolver_path,
                ssh_user=args.ssh_user,
                verbose=args.verbose,
            )
        brick_roles_by_host = {}
        brick_role_evidence_error = ""
        brick_role_evidence_required = bool(heal_entries)
        if brick_role_evidence_required:
            try:
                brick_roles_by_host = parse_brick_roles(get_volume_info(args.volume))
            except RuntimeError as exc:
                brick_role_evidence_error = str(exc)
        manifest, observations = build_manifest(
            heal_entries,
            resolver,
            brick_roles_by_host=brick_roles_by_host,
            brick_role_evidence_required=brick_role_evidence_required,
            brick_role_evidence_error=brick_role_evidence_error,
        )
        write_manifest(
            # Bare observation caches have no independently recorded volume origin.
            args.manifest_out, manifest, volume="" if args.observations_in else args.volume,
            bricks=[{"host": host, "path": brick_paths.get(host, brick_path),
                     "role": brick_roles_by_host.get(host, "")} for host in resolved_hosts],
        )
        if args.observations_out:
            dump_observations(args.observations_out, observations)
        update_status(
            args.status_file,
            phase="manifest-built",
            volume=args.volume,
            mountpoint=mountpoint,
            hosts=resolved_hosts,
            brick_path=brick_path,
            brick_paths=brick_paths,
            manifest_out=args.manifest_out,
            observations_out=args.observations_out or "",
            heal_file=heal_file,
            input_source="heal_info",
            requested_seed=heal_file,
        )
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
        manifest_payload = load_plan_payload(args.manifest_in)
        manifest = load_manifest(args.manifest_in, payload=manifest_payload)
        mountpoint = args.mountpoint or str((load_status(args.status_file).get("mountpoint") or "")).strip()
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
        print(render_manifest_summary(summary))
        return 0

    if args.cmd == "graph-report":
        plan = load_plan_payload(args.plan_in)
        print(render_graph_report(plan))
        return 0

    if args.cmd == "graph-audit":
        plan = load_plan_payload(args.plan_in)
        print(render_graph_audit_report(load_plan_actions(plan)))
        return 0

    if args.cmd == "graph-family-report":
        plan = load_plan_payload(args.plan_in)
        print(render_graph_family_report(plan, args.family))
        return 0

    if args.cmd == "graph-cycle-report":
        plan = load_plan_payload(args.plan_in)
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
        plan = load_plan_payload(args.plan_in)
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
            scope=args.scope,
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
            volume=str(current_status.get("volume") or "").strip(),
            brick_path=str(current_status.get("brick_path") or "").strip(),
            backup_root=args.backup_root or "",
            backup_mode=args.backup_mode,
            batch=args.batch,
            decision_file=args.decision_file or "",
            split_brain_native_policy=_split_brain_native_policy_from_args(args),
            selection={
                "action_type": args.action_type or "",
                "path_contains": args.path_contains or "",
                "strategy": args.strategy or "",
                "limit": args.limit or 0,
            },
            review_policies=_review_policies_from_args(args),
            summary=summary,
        )
        print(render_apply_summary(summary))
        return 0

    if args.cmd == "apply-preflight":
        _print_status_warnings(args.status_file)
        payload = load_plan_payload(args.apply_in)
        payload["require_snapshot"] = args.require_snapshot
        payload["snapshot_ack"] = args.snapshot_ack
        report = build_preflight_report(payload)
        write_preflight_report(args.preflight_out, report)
        update_status(
            args.status_file,
            phase="apply-preflight",
            apply_in=args.apply_in,
            preflight_out=args.preflight_out,
            preflight=report,
            reversibility=report.get("reversibility", {}),
        )
        return 0

    if args.cmd == "manager-preflight":
        _print_status_warnings(args.status_file)
        report = build_manager_health_report(
            args.volume,
            ssh_user=args.ssh_user,
            connect_timeout=args.connect_timeout,
            require_snapshot=args.require_snapshot,
            snapshot_ack=args.snapshot_ack,
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
            reversibility=report.get("reversibility", {}),
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
            require_snapshot=args.require_snapshot,
            snapshot_ack=args.snapshot_ack,
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
            reversibility=report.get("reversibility", {}),
        )
        print(render_manager_preflight_summary(report))
        for message in report.get("summary", {}).get("warnings", []):
            print(f"WARNING: {message}", file=sys.stderr)
        return 0 if report.get("summary", {}).get("ready") else 2

    if args.cmd == "apply-run":
        _print_status_warnings(args.status_file)
        apply_payload = load_apply_payload(args.apply_in)
        if args.execute:
            try:
                bound_volume = validate_apply_binding(apply_payload, load_status(args.status_file))
                validate_live_topology(apply_payload, get_volume_info(bound_volume))
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"ERROR: execution binding check failed: {exc}", file=sys.stderr)
                return 2
        results = load_apply_results(args.apply_in, payload=apply_payload)
        controller_cycle = apply_payload.get("controller_cycle") or {}
        if not isinstance(controller_cycle, dict):
            controller_cycle = {}
        results = filter_apply_results(
            results,
            action_type=args.action_type,
            path_contains=args.path_contains,
            strategy=args.strategy,
            limit=args.limit,
            include_dependencies=args.execute,
            scope=args.scope,
        )
        if args.execute:
            if args.execute_ready:
                ready_results, skipped_results = filter_execute_ready_results(results)
                if skipped_results:
                    skipped_counts = {}
                    for item in skipped_results:
                        skipped_counts[item.action_type] = skipped_counts.get(item.action_type, 0) + 1
                    print(
                        "WARNING: execute-ready skipped "
                        + ", ".join(f"{count} {action_type}" for action_type, count in sorted(skipped_counts.items()))
                        + " review-only or unresolved items",
                        file=sys.stderr,
                    )
                results = ready_results
                if not results:
                    print(
                        "ERROR: no ready executable actions remain after filtering review-only items",
                        file=sys.stderr,
                    )
                    return 2
            current_status = load_status(args.status_file)
            try:
                validate_apply_binding(apply_payload, current_status)
            except BindingError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            if not args.skip_health_check:
                volume = bound_volume
                if not volume:
                    print(
                        "ERROR: execution requires a saved volume in status; run health-check first",
                        file=sys.stderr,
                    )
                    return 2
                ssh_user = str(current_status.get("ssh_user") or DEFAULT_SERVICE_USER)
                connect_timeout = float(current_status.get("connect_timeout") or 10.0)
                current_status, health_report = refresh_manager_health_check(
                    args.status_file,
                    current_status=current_status,
                    volume=volume,
                    ssh_user=ssh_user,
                    connect_timeout=connect_timeout,
                )
                print(f"NOTICE: refreshed health-check -> {render_manager_preflight_summary(health_report)}", file=sys.stderr)
                for message in health_report.get("summary", {}).get("warnings", []):
                    print(f"WARNING: {message}", file=sys.stderr)
                if not health_report.get("summary", {}).get("ready"):
                    return 2
                try:
                    validate_apply_binding(apply_payload, current_status)
                    validate_live_topology(apply_payload, get_volume_info(bound_volume))
                except (ValueError, RuntimeError, OSError) as exc:
                    print(f"ERROR: execution binding check failed after health refresh: {exc}", file=sys.stderr)
                    return 2
            controller_cycle_warnings = controller_cycle_gate_warnings(current_status)
            controller_cycle_warnings.extend(
                controller_cycle_gate_warnings({"controller_cycle": controller_cycle})
            )
            if controller_cycle_warnings:
                print(
                    "ERROR: execution is blocked by the saved controller-cycle decision",
                    file=sys.stderr,
                )
                for message in controller_cycle_warnings:
                    print(f"ERROR: {message}", file=sys.stderr)
                return 2
            volume = bound_volume
            heal = (
                summarize_heal_settings(volume, get_heal_settings(volume))
                if volume
                else current_status.get("heal") or {}
            )
            current_reversibility = current_status.get("reversibility") or {}
            if not isinstance(current_reversibility, dict):
                current_reversibility = {}
            effective_require_snapshot = bool(args.require_snapshot or current_reversibility.get("snapshot_required"))
            if args.require_snapshot or args.snapshot_ack:
                update_status(
                    args.status_file,
                    reversibility=_reversibility_status(
                        effective_require_snapshot,
                        args.snapshot_ack or bool(current_reversibility.get("snapshot_acknowledged")),
                    ),
                )
                current_status = load_status(args.status_file)
                try:
                    validate_apply_binding(apply_payload, current_status)
                except BindingError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    return 2
                current_reversibility = current_status.get("reversibility") or {}
                if not isinstance(current_reversibility, dict):
                    current_reversibility = {}
            reversibility_notice = render_reversibility_summary(current_status)
            if reversibility_notice:
                print(f"NOTICE: rollback readiness: {reversibility_notice}", file=sys.stderr)
            if effective_require_snapshot:
                snapshot_warnings = snapshot_gate_warnings(
                    current_status,
                    require_snapshot=effective_require_snapshot,
                    snapshot_ack=args.snapshot_ack,
                )
                if snapshot_warnings:
                    print(
                        "ERROR: execution requires an acknowledged snapshot or equivalent rollback point",
                        file=sys.stderr,
                    )
                    for message in snapshot_warnings:
                        print(f"ERROR: {message}", file=sys.stderr)
                    return 2
            if not args.skip_health_check:
                health_warnings = health_check_warnings(current_status)
                if health_warnings:
                    print(
                        "ERROR: execution requires a recent passing health-check; run health-check first",
                        file=sys.stderr,
                    )
                    for message in health_warnings:
                        print(f"ERROR: {message}", file=sys.stderr)
                    return 2
            if not (args.batch or all(result.batch for result in results)):
                print(
                    "ERROR: execution requires -y/--batch on apply-run or batch=true in the apply file",
                    file=sys.stderr,
                )
                return 2
            if not args.manage_heal and not args.allow_heal_on and not heal.get("all_off"):
                print(
                    "ERROR: heal-related options are not recorded as off; run heal-control --mode off first or pass --allow-heal-on",
                    file=sys.stderr,
                )
                return 2
            valid, errors = validate_execute_results(results)
            if not valid:
                print(json.dumps({"execution_ready": False, "errors": errors}, indent=2), file=sys.stderr)
                return 2
            execution_fingerprint = hashlib.sha256(json.dumps(apply_payload, sort_keys=True).encode()).hexdigest()
            run_dir = Path(args.run_dir).expanduser() if args.run_dir else default_repair_run_dir(bound_volume, uuid.uuid4().hex[:8])
            try:
                check_run_reusable(run_dir)
                previous_run = current_status.get("execution_run_dir")
                if previous_run and current_status.get("execution_apply_fingerprint") == execution_fingerprint:
                    check_run_reusable(str(previous_run))
                    if current_status.get("execution_write_state") == "unknown":
                        raise ExecutionJournalError("this saved apply artifact has an unknown prior write; refresh evidence and planning")
            except (ExecutionJournalError, OSError) as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            # Persist the artifact/run association before any repair dispatch.
            update_status(args.status_file, execution_apply_fingerprint=execution_fingerprint,
                          execution_run_dir=str(run_dir))
            managed_heal = False
            original_heal_settings: dict[str, str] | None = None
            volume = bound_volume
            restore_failed = False
            interrupted = False
            execution_error = ""
            try:
                if args.manage_heal:
                    if not volume:
                        print(
                            "ERROR: apply-run --manage-heal needs a volume in status; run manifest-build or heal-control status first",
                            file=sys.stderr,
                        )
                        return 2
                    original_heal_settings = get_heal_settings(volume)
                    if not heal.get("all_off"):
                        set_heal_settings(volume, enabled=False)
                        managed_heal = True
                        update_status(
                            args.status_file,
                            phase="apply-run-heal-off",
                            volume=volume,
                            heal=summarize_heal_settings(volume, get_heal_settings(volume)),
                            heal_restore_required=True,
                        )
                run_dir.mkdir(parents=True, exist_ok=True)
                try:
                    report = execute_apply_results(
                        results,
                        keep_going=args.keep_going,
                        stage_only=args.stage_only,
                        progress_stream=sys.stderr,
                        progress_callback=_execution_progress_callback(args.status_file, run_dir),
                        parallel_actions=args.parallel_actions,
                        parallel_nice=args.parallel_nice,
                        run_dir=run_dir,
                    )
                except KeyboardInterrupt:
                    interrupted = True
                except (ExecutionJournalError, OSError) as exc:
                    execution_error = str(exc)
            finally:
                if args.manage_heal and volume and original_heal_settings is not None:
                    try:
                        if managed_heal or not heal.get("all_off"):
                            set_heal_settings_exact(volume, original_heal_settings)
                        update_status(
                            args.status_file,
                            phase="apply-run-heal-restored",
                            volume=volume,
                            heal=summarize_heal_settings(volume, get_heal_settings(volume)),
                            heal_restore_required=False,
                        )
                    except Exception as restore_exc:
                        current_heal: dict[str, object] = {}
                        try:
                            current_heal = summarize_heal_settings(volume, get_heal_settings(volume))
                        except Exception as heal_exc:
                            current_heal = {"restore_check_error": str(heal_exc)}
                        update_status(
                            args.status_file,
                            phase="apply-run-heal-state-unverified",
                            volume=volume,
                            heal=current_heal,
                            heal_restore_required=True,
                            heal_restore_error=str(restore_exc),
                            completion_blocked=True,
                        )
                        print(
                            f"ERROR: failed to restore heal settings for {volume}; "
                            "the repair is not complete until heal settings are verified: "
                            f"{restore_exc}",
                            file=sys.stderr,
                        )
                        restore_failed = True
            if restore_failed:
                return 2
            if execution_error:
                try:
                    update_status(args.status_file, phase="apply-run-journal-stopped",
                                  execution_run_dir=str(run_dir), completion_blocked=True,
                                  execution_write_state="unknown", write_outcome_unknown=True,
                                  execution_error=execution_error)
                except OSError as exc:
                    print(f"ERROR: status update also failed: {exc}", file=sys.stderr)
                print(f"ERROR: {execution_error}; inspect {run_dir / 'attempts'} before another attempt", file=sys.stderr)
                return 2
            if interrupted:
                resume_hint = f"gluster-manager repair --status {args.status_file} --granular"
                update_status(
                    args.status_file,
                    phase="apply-run-execution-interrupted",
                    execution_interrupted=True,
                    execution_run_dir=str(run_dir),
                    execution_write_state="unknown",
                    execution_resume=resume_hint,
                    execution_progress={
                        "phase": "interrupted",
                        "message": "execution interrupted by Ctrl-C",
                        "last_activity": "execution interrupted by Ctrl-C",
                        "interrupt_hint": "Inspect the recorded run artifacts before retrying.",
                        "resume_hint": resume_hint,
                    },
                )
                print(
                    "WARNING: execution interrupted; heal settings were restored, but the write state is unknown.",
                    file=sys.stderr,
                )
                print(f"Inspect before resuming: {resume_hint}", file=sys.stderr)
                return 130
            results_out = args.results_out or f"{args.apply_in}.results.json"
            try:
                write_execute_report(results_out, report)
            except OSError as exc:
                try:
                    update_status(args.status_file, phase="apply-run-report-failed", completion_blocked=True,
                                  execution_run_dir=str(run_dir), execution_write_state="unknown",
                                  write_outcome_unknown=True, execution_error=str(exc))
                except OSError:
                    pass
                print(f"ERROR: results report failed: {exc}; inspect retained records at {run_dir / 'attempts'}", file=sys.stderr)
                return 2
            execution_summary = report.get("summary") or {}
            incomplete = any(int(execution_summary.get(key, 0) or 0) for key in (
                "failed_actions", "blocked_actions", "unknown_actions", "not_run_actions",
            ))
            if incomplete:
                unknown = bool(execution_summary.get("unknown_actions") or execution_summary.get("failed_actions"))
                update_status(
                    args.status_file, phase="apply-run-execution-stopped", apply_in=args.apply_in,
                    results_out=results_out, summary=execution_summary, completion_blocked=True,
                    execution_write_state="unknown" if unknown else "incomplete",
                    write_outcome_unknown=unknown,
                    write_occurred=bool(execution_summary.get("executed_actions")),
                    execution_resume=f"gluster-manager repair --status {args.status_file} --granular",
                )
                print("ERROR: execution is incomplete; inspect failed/blocked actions and refresh evidence before retrying", file=sys.stderr)
                print(json.dumps(execution_summary, indent=2))
                return 2
            final_check: dict[str, object] = {}
            final_check_guard: dict[str, object] = {}
            if volume:
                try:
                    final_check, final_check_guard = _refresh_post_execute_heal_preserving_settings(
                        volume, current_status, repeat_limit=args.heal_repeat_limit,
                        total_timeout_seconds=args.heal_refresh_timeout,
                    )
                except Exception as exc:
                    current_heal: dict[str, object] = {}
                    try:
                        current_heal = summarize_heal_settings(volume, get_heal_settings(volume))
                    except Exception as heal_exc:
                        current_heal = {"restore_check_error": str(heal_exc)}
                    update_status(
                        args.status_file, phase="apply-run-heal-state-unverified", volume=volume,
                        heal=current_heal, heal_restore_required=True, heal_restore_error=str(exc),
                        completion_blocked=True,
                    )
                    print(
                        f"ERROR: post-execute heal refresh or restore failed for {volume}; "
                        f"the repair is not complete until heal settings are verified: {exc}",
                        file=sys.stderr,
                    )
                    return 2
            verification: dict[str, object] = {}
            backup_maintenance: dict[str, object] = {}
            controller_cycle_status = build_controller_cycle_status(
                {
                    **current_status,
                    "final_check_guard": final_check_guard,
                }
            )
            execute_summary = dict(report["summary"])
            execute_summary["controller_next_action"] = controller_cycle_status["controller_cycle"].get(
                "controller_next_action", ""
            )
            completed_with_skips = int(execute_summary.get("completed_with_skips", 0))
            review_only_skipped_steps = int(execute_summary.get("review_only_skipped_steps", 0))
            skipped_steps = int(execute_summary.get("skipped_steps", 0))
            if completed_with_skips:
                if review_only_skipped_steps:
                    print(
                        "NOTICE: repair completed; review-only tail skipped "
                        f"({review_only_skipped_steps} step{'s' if review_only_skipped_steps != 1 else ''})",
                        file=sys.stderr,
                    )
                elif skipped_steps:
                    print(
                        "NOTICE: repair completed with skips "
                        f"({skipped_steps} non-blocking step{'s' if skipped_steps != 1 else ''} skipped)",
                        file=sys.stderr,
                    )
                else:
                    print("NOTICE: repair completed with skips", file=sys.stderr)
            elif int(execute_summary.get("completed_actions", 0)):
                print("NOTICE: repair completed", file=sys.stderr)
            if args.verify_rescan or args.verify_temp_mount:
                if args.stage_only:
                    print("NOTICE: temp mount verification skipped in stage-only mode", file=sys.stderr)
                elif not volume:
                    print(
                        "WARNING: temp mount verification unavailable: no volume recorded in status",
                        file=sys.stderr,
                    )
                else:
                    if args.verify_rescan:
                        print(
                            "NOTICE: verify-rescan may be slow; use the focused find|xargs stat check for small passes",
                            file=sys.stderr,
                        )
                        verification = _verify_post_execute_temp_rescan(
                            volume,
                            results,
                            cleanup=args.verify_temp_mount_cleanup,
                        )
                    else:
                        verification = _verify_post_execute_temp_mount(
                            volume,
                            results,
                            cleanup=args.verify_temp_mount_cleanup,
                        )
                    if verification.get("available"):
                        summary = verification.get("summary") or {}
                        mount_info = verification.get("mount") or {}
                        if args.verify_rescan:
                            checked = int(summary.get("nodes_checked", 0))
                            ok = int(summary.get("nodes_ok", 0))
                            failed = int(summary.get("nodes_failed", 0))
                            label = "recursive temp mount verification"
                        else:
                            checked = int(summary.get("paths_checked", 0))
                            ok = int(summary.get("paths_ok", 0))
                            failed = int(summary.get("paths_failed", 0))
                            label = "temp mount verification"
                        print(
                            f"NOTICE: {label} checked "
                            f"{ok}/{checked} repaired paths at {mount_info.get('mountpoint', str(DEFAULT_TEMP_MOUNT_ROOT))}",
                            file=sys.stderr,
                        )
                        if summary.get("truncated"):
                            print(
                                "WARNING: verify-rescan hit its budget and returned a truncated tree; "
                                "use the focused find|xargs stat check or rerun with a wider budget later",
                                file=sys.stderr,
                            )
                        if failed:
                            failed_paths = ", ".join(
                                str(item.get("logical_path") or "")
                                for item in verification.get("checks") or []
                                if item.get("status") != "ok"
                            )
                            print(
                                "WARNING: temp mount verification failed for: "
                                f"{failed_paths}",
                                file=sys.stderr,
                            )
                    else:
                        print(
                            "WARNING: temp mount verification unavailable: "
                            f"{verification.get('error') or 'no repaired paths found'}",
                            file=sys.stderr,
                        )
            backup_maintenance: dict[str, object] = {}
            if args.archive_backups or args.cleanup_backups:
                backup_maintenance = manage_backup_artifacts(
                    results,
                    archive_path=args.archive_backups or "",
                    cleanup=args.cleanup_backups,
                )
                archive_report = backup_maintenance.get("archive") or {}
                cleanup_report = backup_maintenance.get("cleanup") or {}
                if archive_report.get("archive_created"):
                    print(
                        "NOTICE: archived backup artifacts to "
                        f"{archive_report.get('archive_path')}",
                        file=sys.stderr,
                    )
                if cleanup_report.get("cleanup_completed"):
                    print(
                        "NOTICE: cleaned up backup artifacts after execution",
                        file=sys.stderr,
                    )
                for warning in backup_maintenance.get("warnings", []):
                    print(f"WARNING: {warning}", file=sys.stderr)
                for error in backup_maintenance.get("errors", []):
                    print(f"ERROR: backup maintenance failed: {error}", file=sys.stderr)
                if backup_maintenance.get("errors"):
                    update_status(
                        args.status_file,
                        phase="apply-run-execute",
                        apply_in=args.apply_in,
                        results_out=results_out,
                        summary=execute_summary,
                        final_check=final_check,
                        final_check_guard=final_check_guard,
                        **controller_cycle_status,
                        verification=verification,
                        backup_maintenance=backup_maintenance,
                    )
                    print(json.dumps(execute_summary, indent=2))
                    return 2
            update_status(
                args.status_file,
                phase="apply-run-execute",
                apply_in=args.apply_in,
                results_out=results_out,
                summary=execute_summary,
                final_check=final_check,
                final_check_guard=final_check_guard,
                **controller_cycle_status,
                verification=verification,
                backup_maintenance=backup_maintenance,
            )
            print(json.dumps(execute_summary, indent=2))
            return 0
        update_status(
            args.status_file,
            phase="apply-run-dry-run",
            apply_in=args.apply_in,
            summary=summarize_apply_results(results),
        )
        print(
            render_apply_run(
                results,
                summary_only=args.summary_only,
                controller_cycle=controller_cycle,
            ),
            end="",
        )
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
            acl_mount=args.acl_mount,
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
            preview=args.preview,
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
    if args.cmd == "decision-build":
        _print_status_warnings(args.status_file)
        current_status = load_status(args.status_file)
        plan = load_plan_payload(args.plan_in)
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
        plan = load_plan_payload(args.plan_in)
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
            plan_in=args.plan_in,
            directory_tie_report_out=args.report_out,
            directory_tie_decision_out=args.decision_out or "",
            directory_tie_interactive=args.interactive,
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
