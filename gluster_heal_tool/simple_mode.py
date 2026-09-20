# SPDX-License-Identifier: GPL-2.0-only
"""Guided preview and explicitly authorized execution orchestration."""
from __future__ import annotations

import json, sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import load_manifest
from .apply import build_apply_results, load_plan, write_apply_results
from .apply_reporting import render_apply_summary, summarize_apply_results
from .controller_paths import default_repair_run_dir, default_repair_run_root
from .install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from .manager import (
    build_manager_health_report,
    render_manager_preflight_summary,
    resolve_via_backend_path, resolve_via_gfid, resolve_via_gfid_child, resolve_via_index_entry, resolve_via_path,
    resolve_via_volume,
    write_manager_preflight_report,
)
from .planner import build_plan, render_plan_summary, summarize_plan, write_plan
from .volume import discover_brick_paths
from .status_report import render_status_report
from .shared_io import write_json_shared, write_text_shared
from .simple_assistants import build_simple_assistant_report
from .simple_interactive import resume_simple_interaction, run_simple_interaction
from .status import apply_write_history, load_status, update_status


@dataclass(frozen=True)
class SimpleRepairPaths:
    root: Path
    status: Path
    health: Path
    heal_before: Path
    manifest: Path
    observations: Path
    plan: Path
    apply: Path
    decisions: Path
    summary: Path
    backup: Path
    interaction: Path
    events: Path
    support_summary: Path
    assistants: Path


def create_simple_repair_paths(volume: str, run_dir: str | None = None) -> SimpleRepairPaths:
    root = (
        Path(run_dir).expanduser()
        if run_dir
        else default_repair_run_dir(volume, uuid.uuid4().hex[:12])
    )
    root.mkdir(parents=True, exist_ok=False)
    backup = root / "backup"
    backup.mkdir()
    paths = SimpleRepairPaths(
        root=root,
        status=root / "status.json",
        health=root / "health.json",
        heal_before=root / "heal-before.txt",
        manifest=root / "manifest.json",
        observations=root / "observations.json",
        plan=root / "plan.json",
        apply=root / "apply.json",
        decisions=root / "decisions.json",
        summary=root / "summary.txt",
        backup=backup,
        interaction=root / "interaction.json",
        events=root / "events.jsonl",
        support_summary=root / "support-summary.txt",
        assistants=root / "assistants.json",
    )
    write_json_shared(paths.decisions, {"schema_version": 1, "decisions": {}})
    write_json_shared(
        paths.interaction,
        {"schema_version": 1, "mode": "read-only", "state": "preview", "position": 0},
    )
    return paths


def _summary_text(summary: dict[str, Any]) -> str:
    lines = [
        "Simple repair preview",
        "======================",
        f"volume: {summary['volume']}",
        f"run directory: {summary['run_dir']}",
        f"write occurred: {'yes' if summary.get('write_occurred') else 'no'}",
        f"health: {summary['health_summary']}",
        f"evidence: {summary.get('evidence_summary', 'not built')}",
        f"plan: {summary.get('plan_summary', 'not built')}",
        f"apply preview: {summary.get('apply_summary', 'not built')}",
        "",
        f"next action: {summary['next_action']}",
        "",
        "artifacts:",
    ]
    for label, path in summary["artifacts"].items():
        lines.append(f"  {label}: {path}")
    lines.extend(
        [
            "",
            "granular equivalent:",
            f"  plan: gluster-manager plan-build --manifest-in {summary['artifacts']['manifest']} --plan-out {summary['artifacts']['plan']} --status-file {summary['artifacts']['status']}",
            f"  apply: gluster-manager apply-build --plan-in {summary['artifacts']['plan']} --apply-out {summary['artifacts']['apply']} --backup-root {summary['artifacts']['backup']} --status-file {summary['artifacts']['status']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _artifact_map(paths: SimpleRepairPaths) -> dict[str, str]:
    return {
        "status": str(paths.status),
        "health": str(paths.health),
        "heal_before": str(paths.heal_before),
        "manifest": str(paths.manifest),
        "observations": str(paths.observations),
        "plan": str(paths.plan),
        "apply": str(paths.apply),
        "decisions": str(paths.decisions),
        "summary": str(paths.summary),
        "backup": str(paths.backup),
        "interaction": str(paths.interaction),
        "events": str(paths.events),
        "support_summary": str(paths.support_summary),
        "assistants": str(paths.assistants),
    }


def run_simple_preview(
    *,
    volume: str,
    run_dir: str | None = None,
    mountpoint: str | None = None, path: str | None = None, backend_path: str | None = None, gfid: str | None = None, gfid_child: str | None = None, index_entry: str | None = None,
    heal_file: str | None = None,
    heal_latest: bool = False,
    heal_fresh: bool = False,
    backup_mode: str = "required",
    split_brain_policy: str = "auto",
    ssh_user: str = DEFAULT_SERVICE_USER,
    connect_timeout: float = 10.0,
    resolver_path: str = str(DEFAULT_RESOLVER_PATH),
    worker_path: str = str(DEFAULT_WORKER_PATH),
    verbose: bool = False,
    interactive: bool = False,
    execute: bool = False,
    safe_auto: bool = False,
    safe_auto_authorized: bool = False,
    non_interactive_safe_auto: bool = False,
    input_stream: Any | None = None,
    output_stream: Any | None = None,
) -> int:
    output_stream = output_stream or sys.stdout
    input_stream = input_stream or sys.stdin
    paths = create_simple_repair_paths(volume, run_dir)
    mountpoint = mountpoint or f"/{volume.strip().strip('/') or 'volume'}"
    artifacts = _artifact_map(paths)
    health = build_manager_health_report(
        volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
    )
    write_manager_preflight_report(paths.health, health)
    health_summary = render_manager_preflight_summary(health).replace("\n", "; ")
    status = update_status(
        paths.status,
        phase="simple-preview-health-check",
        mode="preview",
        volume=volume,
        mountpoint=mountpoint,
        run_dir=str(paths.root),
        health_out=str(paths.health),
        health_check=health,
        health_check_checked_at=health.get("checked_at", ""),
        artifacts=artifacts,
        write_occurred=False,
    )
    ready = bool((health.get("summary") or {}).get("ready"))
    if not ready:
        summary = {
            "volume": volume,
            "run_dir": str(paths.root),
            "health_summary": health_summary,
            "next_action": f"resolve the health/preflight warnings, then rerun: gluster-manager repair --volume {volume}",
            "artifacts": artifacts,
            "evidence_summary": "blocked by health check",
            "plan_summary": "not built",
            "apply_summary": "not built",
        }
        write_json_shared(paths.root / "summary.json", summary)
        write_text_shared(paths.summary, _summary_text(summary))
        update_status(paths.status, phase="simple-preview-blocked", summary=summary)
        print(_summary_text(summary), end="", file=output_stream)
        return 2

    try:
        manifest_summary, brick_path = _resolve_simple_evidence(volume=volume, path=path, backend_path=backend_path, gfid=gfid, gfid_child=gfid_child, index_entry=index_entry, heal_file=heal_file, heal_latest=heal_latest, heal_fresh=heal_fresh, mountpoint=mountpoint, resolver_path=resolver_path, worker_path=worker_path, manifest_out=str(paths.manifest), observations_out=str(paths.observations), heal_out=str(paths.heal_before), heal_root=str(paths.root / "heal-info"), ssh_user=ssh_user, log_path=str(paths.root / "evidence.log"), verbose=verbose, brick_host_aliases=health.get("host_aliases"))
    except RuntimeError as exc:
        evidence_error = str(exc).strip() or "evidence resolution failed"
        summary = {
            "volume": volume,
            "run_dir": str(paths.root),
            "health_summary": health_summary,
            "evidence_summary": f"blocked: {evidence_error}",
            "plan_summary": "not built",
            "apply_summary": "not built",
            "next_action": (
                "no repair writes occurred; keep this volume in the optional diagnostic lane, "
                "preserve the recorded topology error, and retry after brick-identity-aware "
                "same-host support or a distinct host alias is available"
            ),
            "artifacts": artifacts,
            "write_occurred": False,
            "input_source": "topology-validation",
            "evidence_error": evidence_error,
        }
        write_json_shared(paths.root / "summary.json", summary)
        write_text_shared(paths.summary, _summary_text(summary))
        update_status(
            paths.status,
            phase="simple-preview-evidence-blocked",
            summary=summary,
            evidence_error=evidence_error,
            write_occurred=False,
        )
        print(_summary_text(summary), end="", file=output_stream)
        return 2
    if not manifest_summary.get("heal_file"): artifacts.pop("heal_before", None)
    evidence_summary = (
        f"source={manifest_summary.get("input_source", "heal_info")}; {manifest_summary.get("raw_entries", 0)} raw entries, "
        f"{manifest_summary.get('unique_raw_entries', 0)} unique, "
        f"{manifest_summary.get('observations', 0)} observations"
    )
    status = update_status(
        paths.status,
        phase="simple-preview-manifest-built",
        manifest_out=str(paths.manifest),
        observations_out=str(paths.observations),
        heal_file=str(manifest_summary.get("heal_file") or ""),
        input_source=manifest_summary.get("input_source", "heal_info"),
        requested_seed=manifest_summary.get("requested_seed", ""),
        brick_path=brick_path,
        manifest_summary=manifest_summary,
    )

    manifest_payload = load_plan(paths.manifest)
    plan = build_plan(
        load_manifest(paths.manifest, payload=manifest_payload),
        mountpoint=mountpoint,
        split_brain_policy=split_brain_policy,
    )
    write_plan(paths.plan, plan, manifest_in=paths.manifest, manifest_payload=manifest_payload)
    plan_summary_data = summarize_plan(plan)
    plan_summary = render_plan_summary(plan_summary_data).replace("\n", "; ")
    status = update_status(
        paths.status,
        phase="simple-preview-plan-built",
        manifest_in=str(paths.manifest),
        plan_out=str(paths.plan),
        split_brain_policy=split_brain_policy,
        plan_summary=plan_summary_data,
    )
    assistant_report = build_simple_assistant_report(
        {"schema_version": 1, "actions": [action.to_dict() for action in plan]},
        volume=volume,
        worker_path=worker_path,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        status=status,
    )
    write_json_shared(paths.assistants, assistant_report)
    status = update_status(
        paths.status,
        phase="simple-preview-assistants-built",
        assistants_out=str(paths.assistants),
        assistant_summary=assistant_report.get("summary", {}),
        assistant_plan_fingerprint=assistant_report.get("plan_fingerprint", ""),
        assistant_topology_fingerprint=assistant_report.get("topology_fingerprint", ""),
    )

    plan_payload = load_plan(paths.plan)
    apply_results = build_apply_results(
        plan_payload,
        execution_mode="dry-run",
        backup_root=str(paths.backup),
        backup_mode=backup_mode,
        batch=False,
        volume=volume,
        brick_path=brick_path,
        worker_path=worker_path,
        ssh_user=ssh_user,
    )
    write_apply_results(str(paths.apply), apply_results, controller_cycle={},
                        plan_in=paths.plan, plan_payload=plan_payload)
    apply_summary_data = summarize_apply_results(apply_results, controller_cycle={})
    apply_summary = render_apply_summary(apply_summary_data).replace("\n", "; ")
    if safe_auto:
        next_action = (
            f"review {paths.summary}; safe-auto will execute only the existing ready-safe subset"
            if interactive
            else f"review {paths.summary}; safe-auto will execute the existing ready-safe subset and stop at decisions"
        )
    elif execute:
        next_action = f"review {paths.summary}; the interactive wizard will ask before executing the ready-safe batch"
    else:
        next_action = f"review {paths.summary}; for guided execution, rerun: gluster-manager repair --volume {volume} --interactive --execute"
    summary = {
        "volume": volume,
        "run_dir": str(paths.root),
        "health_summary": health_summary,
        "evidence_summary": evidence_summary,
        "plan_summary": plan_summary,
        "apply_summary": apply_summary,
        "next_action": next_action,
        "artifacts": artifacts,
        "write_occurred": False,
        "input_source": manifest_summary.get("input_source", "heal_info"),
        "requested_seed": manifest_summary.get("requested_seed", ""),
        "assistant_summary": assistant_report.get("summary", {}),
        "mountpoint": mountpoint,
        "backup_mode": backup_mode,
        "split_brain_policy": split_brain_policy,
        "brick_path": brick_path,
        "ssh_user": ssh_user,
        "connect_timeout": connect_timeout,
        "resolver_path": resolver_path,
        "worker_path": worker_path,
        "verbose": verbose,
        "evidence_inputs": {
            "path": path,
            "backend_path": backend_path,
            "gfid": gfid,
            "gfid_child": gfid_child,
            "index_entry": index_entry,
            "heal_file": heal_file,
            "heal_latest": heal_latest,
            "heal_fresh": heal_fresh,
        },
    }
    write_json_shared(paths.root / "summary.json", summary)
    write_text_shared(paths.summary, _summary_text(summary))
    update_status(paths.status, phase="simple-preview-ready", artifacts=artifacts, summary=summary)
    print(_summary_text(summary), end="", file=output_stream)
    if interactive or safe_auto:
        return run_simple_interaction(
            paths,
            summary,
            apply_results,
            input_stream=input_stream,
            output_stream=output_stream,
            allow_execution=execute or safe_auto,
            safe_auto=safe_auto,
            safe_auto_authorized=safe_auto_authorized,
            non_interactive_safe_auto=non_interactive_safe_auto,
        )
    return 0
def configure_simple_repair_parser(cmd: Any) -> None:
    cmd.add_argument("--volume")
    cmd.add_argument("--status", metavar="RUN", help="show the recorded run status without touching Gluster")
    cmd.add_argument("--resume", metavar="RUN", help="show the recorded run and granular resume handoff")
    cmd.add_argument("--granular", action="store_true", help="print exact granular handoff commands for status/resume")
    mode = cmd.add_mutually_exclusive_group()
    mode.add_argument(
        "--interactive",
        action="store_true",
        help="interactive repair guidance; plain repair uses this easy flow by default in a TTY",
    )
    mode.add_argument("--preview", action="store_true", help="read-only preview; never prompts or writes")
    cmd.add_argument(
        "--expert",
        action="store_true",
        help="use explicit expert authority semantics; no implicit writes; combine with --interactive --execute for guided execution",
    )
    cmd.add_argument(
        "--execute",
        action="store_true",
        help="allow the ready-safe batch after review; expert mode requires --expert --interactive --execute",
    )
    cmd.add_argument(
        "--safe-auto",
        action="store_true",
        help="run only the already ready-safe subset; stop at real decisions",
    )
    cmd.add_argument(
        "--batch",
        dest="safe_auto_batch",
        action="store_true",
        help="legacy/explicit non-TTY safe-auto marker; use -y to authorize",
    )
    cmd.add_argument(
        "-y",
        "--yes",
        dest="safe_auto_yes",
        action="store_true",
        help="authorize safe-auto; -y implies --safe-auto and is the non-TTY execution opt-in",
    )
    cmd.add_argument("--run-dir", help="directory for this preview run; defaults below GLUSTER_REPAIR_WORK_ROOT/runs")
    cmd.add_argument("--mountpoint")
    evidence = cmd.add_mutually_exclusive_group()
    evidence.add_argument("--heal-file", help="reuse an existing heal-info snapshot instead of capturing one")
    evidence.add_argument("--heal-latest", action="store_true", help="reuse the newest saved heal-info snapshot")
    evidence.add_argument("--heal-fresh", action="store_true", help="force a fresh heal-info snapshot")
    evidence.add_argument("--path", help="client-visible path on a mounted Gluster volume")
    evidence.add_argument("--backend-path", help="backend object path beneath a recorded brick root")
    evidence.add_argument("--gfid", help="bare GFID UUID or <gfid:...> heal entry")
    evidence.add_argument("--gfid-child", help="child heal entry of the form <gfid:UUID>/child/path")
    evidence.add_argument("--index-entry", help="validated .glusterfs/indices/xattrop or .glusterfs/indices/dirty path")
    cmd.add_argument("--backup-mode", choices=["required", "best-effort", "none"], default="required")
    cmd.add_argument(
        "--policy-entry-split-brain-file",
        choices=["auto", "majority", "mtime", "ctime", "size", "review", "off", "skip", "replace", "quarantine"],
        default="auto",
    )
    cmd.add_argument("--ssh-user", default=DEFAULT_SERVICE_USER)
    cmd.add_argument("--connect-timeout", type=float, default=10.0)
    cmd.add_argument("--resolver-path", default=str(DEFAULT_RESOLVER_PATH))
    cmd.add_argument("--worker-path", default=str(DEFAULT_WORKER_PATH))
    cmd.add_argument("--verbose", action="store_true")
def _resolve_simple_evidence(
    *,
    volume: str,
    path: str | None,
    backend_path: str | None,
    gfid: str | None,
    gfid_child: str | None,
    index_entry: str | None,
    heal_file: str | None,
    heal_latest: bool,
    heal_fresh: bool,
    mountpoint: str,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    heal_out: str,
    heal_root: str,
    ssh_user: str,
    log_path: str,
    verbose: bool,
    brick_host_aliases: object = None,
) -> tuple[dict[str, object], str]:
    common = {
        "resolver_path": resolver_path,
        "worker_path": worker_path,
        "manifest_out": manifest_out,
        "observations_out": observations_out,
        "ssh_user": ssh_user,
        "log_path": log_path,
        "verbose": verbose,
        "brick_host_aliases": brick_host_aliases,
    }
    if path:
        summary = resolve_via_path(path=path, probe_mount=True, **common)
        return summary, str(summary.get("brick_path") or "")
    if backend_path:
        summary = resolve_via_backend_path(
            volume=volume, backend_path=backend_path, mountpoint=mountpoint, **common
        )
        return summary, str(summary.get("brick_path") or "")
    if gfid:
        summary = resolve_via_gfid(
            volume=volume, gfid=gfid, mountpoint=mountpoint, **common
        )
        return summary, str(summary.get("brick_path") or "")
    if gfid_child:
        summary = resolve_via_gfid_child(
            volume=volume, gfid_child=gfid_child, mountpoint=mountpoint, **common
        )
        return summary, str(summary.get("brick_path") or "")
    if index_entry:
        summary = resolve_via_index_entry(
            volume=volume, index_entry=index_entry, mountpoint=mountpoint, **common
        )
        return summary, str(summary.get("brick_path") or "")
    brick_paths = discover_brick_paths(volume)
    brick_path = next(iter(dict.fromkeys(brick_paths.values())), "")
    summary = resolve_via_volume(
        heal_file=heal_file,
        volume=volume,
        brick_path=brick_path,
        brick_paths=brick_paths,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        heal_out=heal_out,
        heal_root=heal_root,
        heal_latest=heal_latest,
        heal_fresh=heal_fresh,
        ssh_user=ssh_user,
        log_path=log_path,
        verbose=verbose,
        probe_mount=False,
    )
    return summary, brick_path
def _resolve_simple_status_path(value: str) -> Path:
    target = Path(value).expanduser()
    if target.is_file():
        return target
    if target.is_dir() and (target / "status.json").is_file():
        return target / "status.json"
    if not target.is_absolute():
        matches = sorted(default_repair_run_root().glob(f"*-{value}"))
        if matches:
            return matches[-1] / "status.json"
    return target / "status.json"


def _simple_paths_from_status(status_path: Path) -> SimpleRepairPaths:
    root = status_path.parent
    backup = root / "backup"
    return SimpleRepairPaths(
        root=root,
        status=status_path,
        health=root / "health.json",
        heal_before=root / "heal-before.txt",
        manifest=root / "manifest.json",
        observations=root / "observations.json",
        plan=root / "plan.json",
        apply=root / "apply.json",
        decisions=root / "decisions.json",
        summary=root / "summary.txt",
        backup=backup,
        interaction=root / "interaction.json",
        events=root / "events.jsonl",
        support_summary=root / "support-summary.txt",
        assistants=root / "assistants.json",
    )
def render_simple_status(target: str, *, granular: bool = False, resume: bool = False) -> int:
    status_path = _resolve_simple_status_path(target)
    status = load_status(status_path)
    if not status:
        print(f"ERROR: simple repair status not found: {status_path}", file=sys.stderr)
        return 2
    summary = status.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    artifacts = status.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    label = "resume handoff" if resume else "status"
    print(f"Simple repair {label}")
    print("====================")
    print(f"run directory: {status.get("run_dir") or status_path.parent}")
    print(f"phase: {status.get("phase") or "unknown"}")
    progress = status.get("execution_progress") or {}
    if isinstance(progress, dict) and progress:
        progress_phase = str(progress.get("phase") or "execution")
        elapsed = float(progress.get("elapsed_seconds") or 0.0)
        activity = str(progress.get("last_activity") or progress.get("message") or "working").replace("\n", " ")
        print(f"execution progress: phase={progress_phase} elapsed={elapsed:.1f}s last_activity={activity}")
        if progress.get("resume_hint") or status.get("execution_resume"):
            print(f"interrupt/resume: {progress.get("resume_hint") or status.get("execution_resume")}")
    print(f"volume: {status.get("volume") or "unknown"}")
    print(f"evidence source: {status.get("input_source") or summary.get("input_source") or "unknown"}")
    print(f"write occurred: {"yes" if status.get("write_occurred") else "no"}")
    interaction = status.get("interaction") or {}
    if isinstance(interaction, dict):
        print(f"interaction state: {interaction.get("state") or "not started"}")
        if interaction.get("pending_decision"):
            print(f"pending decision: {interaction.get("pending_decision")}")
    print("")
    print(render_status_report(status))
    print("artifacts:")
    for name, path in artifacts.items():
        print(f"  {name}: {path}")
    next_action = str(summary.get("next_action") or "").strip()
    if next_action:
        print(f"next action: {next_action}")
    if granular:
        manifest = artifacts.get("manifest")
        plan = artifacts.get("plan")
        apply = artifacts.get("apply")
        status_file = artifacts.get("status") or str(status_path)
        backup = artifacts.get("backup")
        print("granular handoff:")
        if manifest and plan:
            print(f"  plan: gluster-manager plan-build --manifest-in {manifest} --plan-out {plan} --status-file {status_file}")
        if plan and apply:
            backup_arg = f" --backup-root {backup}" if backup else ""
            print(f"  apply: gluster-manager apply-build --plan-in {plan} --apply-out {apply}{backup_arg} --status-file {status_file}")
        if apply:
            print(f"  execute when explicitly approved: gluster-manager apply-run --execute --execute-ready --apply-in {apply} --status-file {status_file} -y")
    elif resume:
        print(f"resume handoff: gluster-manager repair --resume {target} --granular")
    return 0


def resume_simple_run(
    target: str,
    *,
    input_stream: Any,
    output_stream: Any,
    allow_execution: bool = False,
    safe_auto: bool = False,
    safe_auto_authorized: bool = False,
    non_interactive_safe_auto: bool = False,
) -> int:
    status_path = _resolve_simple_status_path(target)
    status = load_status(status_path)
    if not status:
        print(f"ERROR: simple repair status not found: {status_path}", file=output_stream)
        return 2
    paths = _simple_paths_from_status(status_path)
    summary = status.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    summary_path = paths.root / "summary.json"
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            # Execution summaries are useful status data but do not carry the
            # immutable evidence route required for a safe resume refresh.
            summary = {**summary, **loaded}
    apply_write_history(summary, status)
    for key in (
        "volume",
        "mountpoint",
        "run_dir",
        "input_source",
        "requested_seed",
        "ssh_user",
        "connect_timeout",
        "backup_mode",
        "split_brain_policy",
        "worker_path",
        "resolver_path",
        "verbose",
    ):
        if not summary.get(key) and status.get(key):
            summary[key] = status[key]
    if not summary.get("evidence_inputs"):
        input_source = str(summary.get("input_source") or "").strip().lower()
        requested_seed = str(summary.get("requested_seed") or "").strip()
        recovered_key = {
            "operator_path": "path",
            "backend_path": "backend_path",
            "operator_backend_path": "backend_path",
            "gfid": "gfid",
            "gfid_child": "gfid_child",
            "index_entry": "index_entry",
        }.get(input_source)
        if recovered_key and requested_seed:
            summary["evidence_inputs"] = {recovered_key: requested_seed}
    if not paths.apply.is_file():
        print(f"ERROR: simple repair apply preview not found: {paths.apply}", file=output_stream)
        return 2
    return resume_simple_interaction(
        paths,
        summary,
        input_stream=input_stream,
        output_stream=output_stream,
        allow_execution=allow_execution,
        safe_auto=safe_auto,
        safe_auto_authorized=safe_auto_authorized,
        non_interactive_safe_auto=non_interactive_safe_auto,
    )


def _simple_interactive_requested(args: Any) -> bool:
    if getattr(args, "preview", False):
        return False
    if getattr(args, "interactive", False):
        return True
    if getattr(args, "expert", False):
        return False
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def run_simple_command(args: Any) -> int:
    interactive = _simple_interactive_requested(args)
    expert = bool(getattr(args, "expert", False))
    safe_auto_requested = bool(getattr(args, "safe_auto", False))
    safe_auto_batch = bool(getattr(args, "safe_auto_batch", False))
    safe_auto_yes = bool(getattr(args, "safe_auto_yes", False))
    safe_auto = safe_auto_requested or safe_auto_yes
    execute = bool(getattr(args, "execute", False))
    safe_auto_authorized = safe_auto and safe_auto_yes
    if expert and safe_auto:
        print("ERROR: --expert cannot be combined with --safe-auto or -y", file=sys.stderr)
        return 2
    if safe_auto and execute:
        print("ERROR: --safe-auto/-y and --execute are separate modes; use safe-auto alone", file=sys.stderr)
        return 2
    if (safe_auto_batch or safe_auto_yes) and not safe_auto:
        print("ERROR: --batch is a safe-auto marker and requires --safe-auto or -y", file=sys.stderr)
        return 2
    if safe_auto and not interactive and not safe_auto_authorized:
        print("ERROR: non-TTY safe-auto requires -y; legacy form is --safe-auto --batch -y", file=sys.stderr)
        return 2
    if getattr(args, "preview", False) and safe_auto:
        print("ERROR: --preview cannot be combined with safe-auto execution; omit --preview or use it alone", file=sys.stderr)
        return 2
    if execute and not interactive:
        if expert:
            print("ERROR: expert execution requires --expert --interactive --execute", file=sys.stderr)
        else:
            print("ERROR: --execute requires an interactive TTY; use -y for non-interactive safe-auto execution", file=sys.stderr)
        return 2
    if interactive and not expert and not safe_auto and not execute and not args.status and not args.resume:
        execute = True
    if args.status or args.resume:
        if args.status and args.resume:
            print("ERROR: choose only one of --status or --resume", file=sys.stderr)
            return 2
        if args.status and execute:
            print("ERROR: --execute cannot be combined with --status", file=sys.stderr)
            return 2
        if safe_auto and args.status:
            print("ERROR: --safe-auto cannot be combined with --status", file=sys.stderr)
            return 2
        if args.resume and not args.granular and (interactive or safe_auto_authorized):
            return resume_simple_run(
                args.resume,
                input_stream=sys.stdin,
                output_stream=sys.stdout,
                allow_execution=execute or safe_auto,
                safe_auto=safe_auto,
                safe_auto_authorized=safe_auto_authorized,
                non_interactive_safe_auto=safe_auto_authorized,
            )
        return render_simple_status(
            args.status or args.resume,
            granular=args.granular,
            resume=bool(args.resume),
        )
    if not args.volume:
        print("ERROR: repair requires --volume unless --status or --resume is used", file=sys.stderr)
        return 2
    return run_simple_preview(
        volume=args.volume,
        run_dir=args.run_dir,
        mountpoint=args.mountpoint,
        path=args.path,
        backend_path=args.backend_path,
        gfid=args.gfid,
        gfid_child=args.gfid_child,
        index_entry=args.index_entry,
        heal_file=args.heal_file,
        heal_latest=args.heal_latest,
        heal_fresh=args.heal_fresh,
        backup_mode=args.backup_mode,
        split_brain_policy=args.policy_entry_split_brain_file,
        ssh_user=args.ssh_user,
        connect_timeout=args.connect_timeout,
        resolver_path=args.resolver_path,
        worker_path=args.worker_path,
        verbose=args.verbose,
        interactive=interactive,
        execute=execute,
        safe_auto=safe_auto,
        safe_auto_authorized=safe_auto_authorized,
        non_interactive_safe_auto=safe_auto_authorized,
        input_stream=sys.stdin,
        output_stream=sys.stdout,
    )
