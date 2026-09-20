# SPDX-License-Identifier: GPL-2.0-only
"""Repair execution helpers."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from posixpath import dirname
from typing import Callable, TextIO

from .models import ApplyActionResult, ApplyStep
from .execution_plan import actions_conflict, execution_dependency_errors
from .execution_outcomes import command_message, missing_removal_tolerated, native_resolver_mode, native_resolver_outcome
from .execution_journal import ExecutionJournal, ExecutionJournalError, recorded_command
from .remote_ops import ssh_remote_command
from .role_safety import payload_source_hosts_from_result, role_evidence_error
from .shared_io import write_json_shared, write_text_shared


ProgressCallback = Callable[[dict[str, object]], None]
_INTERRUPT_HINT = "Ctrl-C stops safely; inspect the recorded run artifacts before resuming."


def _emit_progress(progress_stream: TextIO | None, message: str) -> None:
    if progress_stream is None:
        return
    progress_stream.write(message.rstrip() + "\n")
    progress_stream.flush()


def _notify_progress(
    callback: ProgressCallback | None,
    *,
    started_monotonic: float,
    phase: str,
    message: str,
    action_number: int | None = None,
    total_actions: int | None = None,
    wave_number: int | None = None,
) -> None:
    if callback is None:
        return
    event: dict[str, object] = {
        "phase": phase,
        "message": message,
        "last_activity": message,
        "last_activity_at": _now_iso(),
        "elapsed_seconds": round(max(0.0, time.monotonic() - started_monotonic), 3),
        "interrupt_hint": _INTERRUPT_HINT,
    }
    if action_number is not None:
        event["action_number"] = action_number
    if total_actions is not None:
        event["total_actions"] = total_actions
    if wave_number is not None:
        event["wave_number"] = wave_number
    try:
        callback(event)
    except Exception:
        # Progress is observability; it must not turn a valid repair failure into a different one.
        return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize_apply_results(results: list[ApplyActionResult]) -> dict[str, object]:
    action_types = Counter(result.action_type for result in results)
    statuses = Counter(result.status for result in results)
    ready_repairs = statuses.get("planned", 0) + statuses.get("proposed", 0)
    review_only_types = Counter(result.action_type for result in results if result.status == "review")
    review_items = sum(review_only_types.values())
    split_brain_actionable = sum(
        1
        for result in results
        if result.action_type == "review_entry_split_brain" and result.status in {"planned", "proposed"}
    )
    split_brain_review_only = sum(
        1 for result in results if result.action_type == "review_entry_split_brain" and result.status == "review"
    )
    split_brain_auto_majority = sum(
        1
        for result in results
        if result.action_type == "review_entry_split_brain"
        and result.status in {"planned", "proposed"}
        and any("split-brain winner selected by policy: majority winner" in note for note in result.notes)
    )
    split_brain_auto_mtime = sum(
        1
        for result in results
        if result.action_type == "review_entry_split_brain"
        and result.status in {"planned", "proposed"}
        and any("split-brain winner selected by policy: mtime winner=" in note for note in result.notes)
    )
    repair_strategy_notes = Counter()
    for result in results:
        for note in result.notes:
            if note.startswith("repair strategy: "):
                repair_strategy_notes[note.removeprefix("repair strategy: ")] += 1
                break
    return {
        "actions_total": len(results),
        "action_types": dict(action_types),
        "statuses": dict(statuses),
        "repair_strategies": dict(repair_strategy_notes),
        "ready_repairs": ready_repairs,
        "review_items": review_items,
        "review_only_action_types": dict(review_only_types),
        "estimated_stage_bytes": sum(item.estimated_stage_bytes for item in results),
        "estimated_backup_bytes": sum(item.estimated_backup_bytes for item in results),
        "estimated_unknown_backup_items": sum(
            item.estimated_unknown_backup_items for item in results
        ),
        "file_repairs": action_types.get("repair_file", 0),
        "dead_file_ref_cleanup_actions": action_types.get("cleanup_dead_file_refs", 0),
        "stale_glusterfs_index_cleanup_actions": action_types.get("cleanup_stale_glusterfs_index", 0),
        "file_metadata_repairs": action_types.get("repair_file_metadata", 0),
        "orphaned_symlink_cleanups": action_types.get("cleanup_orphaned_symlink", 0),
        "directory_repairs": action_types.get("reconcile_directory", 0),
        "directory_metadata_repairs": action_types.get("repair_directory_metadata", 0),
        "entry_split_brain_actionable": split_brain_actionable,
        "entry_split_brain_review_only": split_brain_review_only,
        "entry_split_brain_auto_majority": split_brain_auto_majority,
        "entry_split_brain_auto_mtime": split_brain_auto_mtime,
        "probable_stale_survivor_reviews": review_only_types.get("review_probable_stale_survivor", 0),
        "probable_orphaned_symlink_reviews": review_only_types.get("review_probable_orphaned_symlink", 0),
        "directory_child_gap_reviews": review_only_types.get("review_directory_children", 0),
        "directory_gfid_conflict_reviews": review_only_types.get("review_directory_gfid_conflict", 0),
        "directory_metadata_reviews": review_only_types.get("review_directory_metadata", 0),
        "file_metadata_reviews": review_only_types.get("review_file_metadata", 0),
        "dead_gfid_reference_reviews": review_only_types.get("review_dead_gfid_reference", 0),
        "dead_gfid_cleanup_actions": action_types.get("cleanup_dead_gfid", 0),
        "orphaned_symlink_cleanup_actions": action_types.get("cleanup_orphaned_symlink", 0),
    }


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return recorded_command(command, lambda: subprocess.run(
        command, text=True, capture_output=True, check=False,
    ))


def _missing_tolerated(step: ApplyStep, completed: subprocess.CompletedProcess[str]) -> bool:
    return missing_removal_tolerated(step, completed)


def _native_split_brain_command_mode(step: ApplyStep) -> str:
    return native_resolver_mode(step.command_preview)


def _native_split_brain_outcome_note(step: ApplyStep, status: str) -> str:
    mode = _native_split_brain_command_mode(step)
    outcome = native_resolver_outcome(step.command_preview, step.returncode, step.message)
    if status == "ok":
        if outcome == "tie":
            return f"native split-brain outcome: {mode} tie; follow-up remains active before manual fallback"
        if outcome == "already-resolved":
            return "native split-brain outcome: native resolver reported not in split-brain; fallback skipped"
        if mode == "source-brick":
            return "native split-brain outcome: source-brick follow-up resolved by native Gluster heal"
        return "native split-brain outcome: resolved by native Gluster heal"
    return "native split-brain outcome: unknown; fallback stopped; inspect possible writes and refresh evidence before planning again"


def _execute_step(step: ApplyStep) -> tuple[str, int | None, str]:
    if not step.command_preview:
        return ("skipped", None, "review-only step")

    if step.step_type == "resolve_split_brain_gluster_cli":
        completed = _run_command(step.command_preview)
        message = command_message(completed)
        outcome = native_resolver_outcome(step.command_preview, completed.returncode, message)
        return ("unknown" if outcome == "unknown" else "ok", completed.returncode,
                message or "native resolver returned no recognized outcome")

    if step.step_type in {"mkdir_stage_parent", "mkdir_mount_parent", "ensure_directory_via_mount"}:
        if step.step_type == "mkdir_stage_parent":
            Path(step.target_path).mkdir(parents=True, exist_ok=True)
            return ("ok", 0, "directory ensured")
        completed = _run_command(["sudo", "-n", "mkdir", "-p", "--", step.target_path])
        if completed.returncode == 0:
            return ("ok", 0, "directory ensured")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type == "remove_restored_mount_file" and step.target_path:
        completed = _run_command(["sudo", "-n", "rm", "-f", "--", step.target_path])
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type in {"restore_via_mount", "restore_via_mount_child_gap"} and step.source_path and step.target_path:
        completed = _run_command(step.command_preview)
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type == "restore_file_backend_gap_fill" and step.source_path and step.target_path:
        completed = _run_command(step.command_preview)
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message_body = command_message(completed) or "command failed"
        if step.source_host:
            source_label = f"{step.source_host}:{step.source_path}"
        else:
            source_label = step.source_path or "unknown source"
        target_label = f"{step.host}:{step.target_path}" if step.host else step.target_path
        return ("failed", completed.returncode, f"brick-side salvage {source_label} -> {target_label} failed: {message_body}")

    if step.step_type == "stage_directory_subtree_local" and step.target_path:
        Path(step.target_path).parent.mkdir(parents=True, exist_ok=True)
        completed = _run_command(step.command_preview)
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type in {"mkdir_directory_backend", "mkdir_directory_backend_child_gap"} and step.host and step.target_path:
        completed = _run_command(ssh_remote_command(step.host, ["mkdir", "-p", "--", step.target_path]))
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type == "attach_directory_gfid" and step.host and step.target_path and step.source_path:
        raw = base64.b64encode(bytes.fromhex(step.source_path.replace("-", ""))).decode("ascii")
        completed = _run_command(
            ssh_remote_command(
                step.host,
                [
                    "setfattr",
                    "-n",
                    "trusted.gfid",
                    "-v",
                    f"0s{raw}",
                    "--",
                    step.target_path,
                ],
            )
        )
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type == "attach_directory_mdata" and step.host and step.target_path and step.source_path:
        cleaned = step.source_path.strip()
        if cleaned.startswith(("0x", "0X")):
            cleaned = cleaned[2:]
        completed = _run_command(
            ssh_remote_command(
                step.host,
                [
                    "setfattr",
                    "-n",
                    "trusted.glusterfs.mdata",
                    "-v",
                    f"0x{cleaned}",
                    "--",
                    step.target_path,
                ],
            )
        )
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type in {"quarantine_directory_backend", "quarantine_directory_gfid"} and step.host and step.source_path and step.target_path:
        completed = _run_command(
            ssh_remote_command(
                step.host,
                [
                    "mv",
                    "--",
                    step.source_path,
                    step.target_path,
                ],
            )
        )
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type in {"quarantine_file_backend", "quarantine_file_gfid"} and step.host and step.source_path and step.target_path:
        completed = _run_command(
            ssh_remote_command(
                step.host,
                [
                    "mv",
                    "--",
                    step.source_path,
                    step.target_path,
                ],
            )
        )
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type == "attach_file_gfid" and step.host and step.target_path and step.source_path:
        cleaned = step.source_path.replace("-", "").strip()
        if cleaned.startswith(("0x", "0X")):
            cleaned = cleaned[2:]
        raw = base64.b64encode(bytes.fromhex(cleaned)).decode("ascii")
        completed = _run_command(
            ssh_remote_command(
                step.host,
                [
                    "setfattr",
                    "-n",
                    "trusted.gfid",
                    "-v",
                    f"0s{raw}",
                    "--",
                    step.target_path,
                ],
            )
        )
        if completed.returncode == 0:
            return ("ok", 0, "")
        if _missing_tolerated(step, completed):
            message = command_message(completed) or "missing removal target tolerated"
            return ("skipped", completed.returncode, message)
        message = command_message(completed) or "command failed"
        return ("failed", completed.returncode, message)

    if step.step_type.startswith("backup_") and step.host and step.target_path:
        mkdir_cmd = ssh_remote_command(step.host, ["mkdir", "-p", "--", dirname(step.target_path)])
        mkdir_completed = _run_command(mkdir_cmd)
        if mkdir_completed.returncode != 0:
            message = command_message(mkdir_completed) or "remote mkdir failed"
            return ("failed", mkdir_completed.returncode, message)

    completed = _run_command(step.command_preview)
    if completed.returncode == 0:
        return ("ok", 0, "")
    if _missing_tolerated(step, completed):
        message = command_message(completed) or "missing removal target tolerated"
        return ("skipped", completed.returncode, message)
    message = command_message(completed) or "command failed"
    return ("failed", completed.returncode, message)


def _reset_step_execution_state(step: ApplyStep) -> None:
    step.returncode = None
    step.message = ""
    step.notes = [
        note
        for note in step.notes
        if not (
            note.startswith("execution:")
            or note.startswith("started_at=")
            or note.startswith("finished_at=")
        )
    ]


def validate_execute_results(
    results: list[ApplyActionResult],
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for index, reasons in execution_dependency_errors(results).items():
        errors.extend(f"{results[index - 1].logical_path}: {reason}" for reason in reasons)
    selected_ids = {result.action_id for result in results}
    supported_step_types = {
        "resolve_split_brain_gluster_cli",
        "mkdir_stage_parent",
        "mkdir_mount_parent",
        "mkdir_directory_backend",
        "mkdir_directory_backend_child_gap",
        "attach_directory_gfid",
        "attach_directory_mdata",
        "attach_file_gfid",
        "relink_arbiter_file_gfid",
        "relink_arbiter_directory_gfid",
        "apply_posix_metadata_owner",
        "apply_posix_metadata_mode",
        "apply_posix_metadata_acl",
        "stage_winner_local",
        "stage_directory_subtree_local",
        "backup_stale_backend",
        "backup_directory_subtree",
        "remove_stale_backend",
        "backup_stale_file_gfid",
        "remove_stale_file_gfid",
        "backup_stale_gfid",
        "backup_stale_dir_gfid",
        "remove_stale_gfid",
        "remove_stale_dir_gfid",
        "remove_stale_index_ghost",
        "restore_directory_subtree_bounded",
        "restore_directory_subtree_backup",
        "review_directory_backend_recreate",
        "verify_directory_backend",
        "verify_directory_backend_child_gap",
        "ensure_directory_via_mount",
        "wait_for_child_repairs",
        "restore_file_backend_backfill",
        "restore_file_backend_gap_fill",
        "restore_via_mount",
        "restore_via_mount_child_gap",
        "remove_restored_mount_file",
        "quarantine_directory_backend",
        "quarantine_directory_gfid",
        "quarantine_file_backend",
        "quarantine_file_gfid",
        "review_dead_gfid_cleanup",
        "review_dead_file_ref_cleanup",
        "review_stale_glusterfs_index_cleanup",
    }
    review_only_action_types = {
        "review_probable_stale_survivor": "stale-survivor cleanup stays review-only until a live proof branch is added",
        "review_probable_orphaned_symlink": "orphaned symlink cleanup stays review-only until policy allows delete",
        "review_directory_children": "directory child-gap cases stay review-only until a safe child-set branch is proven",
        "review_directory_gfid_conflict": "directory GFID conflicts stay review-only until a directory canary proves a safe branch",
        "review_directory_metadata": "directory metadata-only cases stay review-only",
        "review_unclassified_authority": "planner authority is unclassified; refresh evidence and close the decision card before execution",
        "review_file_metadata": "file metadata-only cases stay review-only",

        "review_dead_gfid_reference": "dead-GFID live-reference cases stay review-only until the referenced path is resolved",
        "defer_dead_gfid_cleanup": "dead-GFID cleanup stays final-step review-only",
    }

    for result in results:
        if result.status not in {"planned", "proposed"}:
            errors.append(f"{result.logical_path}: action state {result.status!r} requires fresh planning")
        strategy = _strategy_for_result(result)
        source_hosts = payload_source_hosts_from_result(result)
        role_error = role_evidence_error(
            result.brick_roles_by_host,
            required=bool(result.brick_role_evidence_required and source_hosts),
            evidence_error=result.brick_role_evidence_error,
            candidate_hosts=source_hosts,
            brick_host_aliases=result.brick_host_aliases,
        )
        if role_error:
            errors.append(f"{result.logical_path}: arbiter safety gate: {role_error}")
            continue
        missing_dependencies = [dep for dep in result.depends_on if dep not in selected_ids]
        if missing_dependencies:
            errors.append(
                f"{result.logical_path}: unresolved dependencies present: {', '.join(missing_dependencies)}"
            )
        if result.action_type == "repair_file":
            if strategy not in {"restore_missing_replica", "restore_missing_child_replica", "replace_conflicting_file"}:
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "repair_file_metadata":
            if strategy != "attach_file_gfid":
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "repair_posix_metadata":
            if strategy not in {"align_posix_metadata_majority", "align_posix_metadata_selected_source", "resolve_posix_metadata_source_brick"}:
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "clear_split_brain_marker":
            if [step.step_type for step in result.steps] != ["resolve_split_brain_gluster_cli"]:
                errors.append(
                    f"{result.logical_path}: marker continuation must contain exactly one official Gluster resolver step"
                )
                continue
        elif result.action_type == "repair_directory_metadata":
            if strategy not in {
                "attach_directory_gfid",
                "attach_directory_mdata",
                "recreate_missing_directory_backend",
                "recreate_missing_directory_backend_child_gap",
                "restore_directory_children_bounded",
                "canonical_content_recreate",
            }:
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "review_entry_split_brain":
            if strategy not in {"replace_entry_split_brain_file", "quarantine_conflicting_file"}:
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "reconcile_directory":
            if strategy not in {
                "restore_missing_directory",
                "recreate_missing_directory_backend",
                "recreate_missing_directory_backend_child_gap",
                "reconcile_directory_children",
                "restore_directory_children_bounded",
            }:
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "cleanup_dead_gfid":
            if strategy != "delete_dead_gfid_residue":
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "cleanup_dead_file_refs":
            if strategy != "delete_dead_file_ref_residue":
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "cleanup_arbiter_residue":
            if strategy != "delete_arbiter_only_residue":
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "cleanup_stale_glusterfs_index":
            if strategy != "delete_stale_glusterfs_index_residue":
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "cleanup_orphaned_symlink":
            if strategy != "delete_orphaned_symlink_residue":
                errors.append(
                    f"{result.logical_path}: strategy {strategy or 'unknown'} is not executable yet"
                )
                continue
        elif result.action_type == "review_probable_stale_survivor":
            if strategy not in {
                "delete_below_quorum_file",
                "delete_below_quorum_subtree",
                "restore_below_quorum_file_from_brick",
            }:
                errors.append(
                    f"{result.logical_path}: stale-survivor cleanup stays review-only until a live proof branch is added"
                )
                continue
            if result.status not in {"planned", "proposed"}:
                errors.append(
                    f"{result.logical_path}: stale-survivor delete action is not in an executable state"
                )
                continue
        elif result.action_type == "review_directory_gfid_conflict":
            if strategy != "quarantine_conflicting_directory":
                errors.append(
                    f"{result.logical_path}: directory GFID conflicts stay review-only until a quarantine branch is selected"
                )
                continue
            if result.status not in {"planned", "proposed"}:
                errors.append(
                    f"{result.logical_path}: directory quarantine action is not in an executable state"
                )
                continue
        elif result.action_type == "review_type_mismatch":
            if strategy != "quarantine_conflicting_type_mismatch":
                errors.append(f"{result.logical_path}: type mismatch remains review-only until a supported quarantine policy is selected")
                continue
            if result.status not in {"planned", "proposed"}:
                errors.append(f"{result.logical_path}: type-mismatch quarantine action is not in an executable state")
                continue
        elif result.action_type in review_only_action_types:
            errors.append(
                f"{result.logical_path}: {review_only_action_types[result.action_type]}"
            )
            continue
        else:
            errors.append(
                f"{result.logical_path}: action_type {result.action_type} is not executable yet"
            )
            continue
        if result.status == "blocked":
            errors.append(f"{result.logical_path}: action is blocked")
        if result.action_type == "review_entry_split_brain" and result.status not in {"proposed", "planned"}:
            errors.append(f"{result.logical_path}: split-brain review action is not in an executable state")
        if result.action_type == "reconcile_directory" and result.status not in {"proposed", "planned"}:
            errors.append(f"{result.logical_path}: directory action is not in an executable state")
        for step in result.steps:
            if step.step_type not in supported_step_types:
                errors.append(
                    f"{result.logical_path}: step type {step.step_type} is not executable yet"
                )
    return (not errors, errors)


def _strategy_for_result(result: ApplyActionResult) -> str:
    for note in result.notes:
        if note.startswith("repair strategy: "):
            return note.removeprefix("repair strategy: ")
    return ""


def _safe_action_name(action_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", action_id).strip("-.")
    return cleaned[:120] or "action"


def _effective_parallel_workers(requested: int, wave_size: int) -> int:
    cpu_ceiling = max(1, (os.cpu_count() or 1) - 1)
    return max(1, min(max(1, requested), cpu_ceiling, max(1, wave_size)))


def _wave_is_parallel_safe(wave: list[tuple[int, ApplyActionResult]]) -> bool:
    if len(wave) <= 1:
        return False
    if not all(result.parallel_safe for _index, result in wave):
        return False
    for index, (_left_position, left_result) in enumerate(wave):
        for _right_position, right_result in wave[index + 1 :]:
            if actions_conflict(left_result, right_result):
                return False
    return True


def _execute_action(
    result: ApplyActionResult,
    *,
    journal: ExecutionJournal,
    stage_only: bool,
    action_number: int,
    total_actions: int,
    nice_level: int = 0,
    progress_callback: ProgressCallback | None = None,
    started_monotonic: float = 0.0,
    wave_number: int = 0,
) -> dict[str, object]:
    if nice_level > 0:
        try:
            os.setpriority(os.PRIO_PROCESS, 0, nice_level)
        except (AttributeError, OSError):
            pass
    log = io.StringIO()
    _emit_progress(
        log,
        f"[{action_number}/{total_actions}] start {result.action_type} {result.logical_path} ({len(result.steps)} steps)",
    )
    _notify_progress(
        progress_callback,
        started_monotonic=started_monotonic,
        phase="action",
        message=f"starting action {action_number}/{total_actions}: {result.logical_path}",
        action_number=action_number,
        total_actions=total_actions,
        wave_number=wave_number,
    )
    result.execution_mode = "execute"
    result.status = "running"
    action_failed = False
    action_skipped = False
    skipped_steps = 0
    review_only_skipped_steps = 0
    split_brain_official_resolved = False

    for step in result.steps:
        _notify_progress(
            progress_callback,
            started_monotonic=started_monotonic,
            phase="step",
            message=f"running {step.step_type} for {result.logical_path}",
            action_number=action_number,
            total_actions=total_actions,
            wave_number=wave_number,
        )
        _reset_step_execution_state(step)
        if split_brain_official_resolved:
            step.status = "skipped"
            step.message = "official Gluster split-brain resolution already succeeded"
            step.notes.append("official Gluster split-brain resolution already succeeded; fallback skipped")
            action_skipped = True
            skipped_steps += 1
            continue
        if stage_only and step.step_type not in {"mkdir_stage_parent", "stage_winner_local"}:
            step.status = "skipped"
            step.message = "stage-only mode"
            step.notes.append("stage-only mode: skipped")
            action_skipped = True
            skipped_steps += 1
            continue
        step.status = "running"
        step_started = _now_iso()
        status, returncode, message = journal.execute_step(result.action_id, step, _execute_step)
        step.status = status
        step.returncode = returncode
        step.message = message or ""
        if message:
            step.notes.append(f"execution: {message}")
        step.notes.append(f"started_at={step_started}")
        step.notes.append(f"finished_at={_now_iso()}")
        _notify_progress(
            progress_callback,
            started_monotonic=started_monotonic,
            phase="step",
            message=f"{status} {step.step_type} for {result.logical_path}",
            action_number=action_number,
            total_actions=total_actions,
            wave_number=wave_number,
        )
        if step.step_type == "resolve_split_brain_gluster_cli":
            outcome_note = _native_split_brain_outcome_note(step, status)
            step.notes.append(outcome_note)
            result.notes.append(outcome_note)
            native_outcome = native_resolver_outcome(step.command_preview, returncode, step.message)
            if status == "ok" and native_outcome in {"resolved", "already-resolved"}:
                split_brain_official_resolved = True
            elif status == "ok" and native_outcome == "tie":
                tie_note = f"official Gluster {_native_split_brain_command_mode(step)} resolver was inconclusive; the planned follow-up remains active before manual fallback"
                step.notes.append(tie_note)
                result.notes.append(tie_note)
        if status not in {"ok", "skipped"}:
            action_failed = True
            result.status = "failed" if status == "failed" else "unknown"
            step.status = result.status
            result.notes.append(f"execution {result.status} at step {step.step_id}: {message}")
            break
        if status == "skipped":
            action_skipped = True
            skipped_steps += 1
            if not step.command_preview:
                review_only_skipped_steps += 1

    if not action_failed:
        result.status = "completed-with-nonblocking-skips" if action_skipped else "completed"
    _emit_progress(log, f"[{action_number}/{total_actions}] done {result.status} {result.logical_path}")
    _notify_progress(
        progress_callback,
        started_monotonic=started_monotonic,
        phase="action",
        message=f"finished action {action_number}/{total_actions}: {result.status} {result.logical_path}",
        action_number=action_number,
        total_actions=total_actions,
        wave_number=wave_number,
    )
    return {
        "failed": action_failed,
        "skipped_action": action_skipped and not action_failed,
        "skipped_steps": skipped_steps,
        "review_only_skipped_steps": review_only_skipped_steps,
        "log": log.getvalue(),
    }


def _unstarted_action(result: ApplyActionResult, reason: str, *, blocked: bool) -> dict[str, object]:
    result.status = "blocked" if blocked else "not-run"
    result.execution_mode = "execute"
    message = f"execution {result.status}: {reason}"
    result.notes.append(message)
    for step in result.steps:
        if step.status == "planned":
            step.status = "not-run"
            step.message = message
    return {"executed": False, "failed": blocked, "skipped_action": False,
            "skipped_steps": 0, "review_only_skipped_steps": 0,
            "log": f"{result.logical_path}: {message}\n"}


def _execute_action_guarded(result: ApplyActionResult, **kwargs) -> dict[str, object]:
    try:
        return _execute_action(result, **kwargs)
    except (Exception, KeyboardInterrupt) as exc:
        # A worker may have written before losing its response. Never let an
        # exceptional result satisfy a dependency or trigger a blind retry.
        result.status = "unknown"
        message = f"execution outcome unknown: {type(exc).__name__}: {exc}"
        result.notes.append(message)
        for step in result.steps:
            if step.status == "running":
                step.status = "unknown"
                step.message = message
        return {"executed": True, "failed": True, "interrupted": isinstance(exc, KeyboardInterrupt),
                "skipped_action": False, "skipped_steps": sum(step.status == "skipped" for step in result.steps),
                "review_only_skipped_steps": sum(step.status == "skipped" and not step.command_preview for step in result.steps),
                "log": f"{result.logical_path}: {message}\n"}


def execute_apply_results(
    results: list[ApplyActionResult],
    *,
    keep_going: bool = False,
    stage_only: bool = False,
    progress_stream: TextIO | None = None,
    progress_callback: ProgressCallback | None = None,
    parallel_actions: int = 1,
    parallel_nice: int = 5,
    run_dir: str | Path | None = None,
) -> dict[str, object]:
    run_path = Path(run_dir) if run_dir else Path(tempfile.mkdtemp(prefix="gluster-execution-"))
    try:
        with ExecutionJournal(run_path) as journal:
            journal.append("attempt-start", actions=[result.to_dict() for result in results])
            try:
                report = _execute_apply_results(
                    results, journal=journal, keep_going=keep_going, stage_only=stage_only,
                    progress_stream=progress_stream, progress_callback=progress_callback,
                    parallel_actions=parallel_actions, parallel_nice=parallel_nice, run_dir=run_path,
                )
            except BaseException as exc:
                journal.append("attempt-stopped", error=f"{type(exc).__name__}: {exc}",
                               actions=[result.to_dict() for result in results])
                raise
            complete = all(result.status in {"completed", "completed-with-nonblocking-skips"} for result in results)
            journal.append("attempt-finish", state="complete" if complete else "incomplete", report=report)
            return report
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise ExecutionJournalError(f"execution stopped; retained attempt records: {run_path / 'attempts'}: {exc}") from exc


def _execute_apply_results(
    results: list[ApplyActionResult],
    *,
    journal: ExecutionJournal,
    keep_going: bool = False,
    stage_only: bool = False,
    progress_stream: TextIO | None = None,
    progress_callback: ProgressCallback | None = None,
    parallel_actions: int = 1,
    parallel_nice: int = 5,
    run_dir: str | Path | None = None,
) -> dict[str, object]:
    started_at = _now_iso()
    started_monotonic = time.monotonic()
    total_actions = len(results)
    run_path = Path(run_dir) if run_dir else None
    _notify_progress(
        progress_callback,
        started_monotonic=started_monotonic,
        phase="execution",
        message=f"starting execution of {total_actions} action(s)",
        total_actions=total_actions,
    )
    actions_path = run_path / "actions" if run_path else None
    verification_path = run_path / "verification" if run_path else None
    backups_path = run_path / "backups" if run_path else None
    if run_path is not None:
        run_path.mkdir(parents=True, exist_ok=True)
    for path_item in (actions_path, verification_path, backups_path):
        if path_item is not None:
            path_item.mkdir(parents=True, exist_ok=True)

    indexed_results = list(enumerate(results, start=1))
    waves: dict[int, list[tuple[int, ApplyActionResult]]] = {}
    for index, result in indexed_results:
        waves.setdefault(int(result.execution_wave), []).append((index, result))

    action_outcomes: dict[int, dict[str, object]] = {}
    dependency_errors = execution_dependency_errors(results)
    index_by_id = {result.action_id: index for index, result in indexed_results}
    initial_states = {index: result.status for index, result in indexed_results}
    stop_reason = ""
    executed_actions = 0
    stopped = False
    interrupted = False

    for wave_number in sorted(waves):
        wave = waves[wave_number]
        ready_wave = []
        for index, result in wave:
            reasons = list(dependency_errors.get(index, []))
            if initial_states[index] not in {"planned", "proposed"}:
                reasons.append(f"action state {initial_states[index]!r} requires fresh planning")
            for dependency in result.depends_on:
                parent_index = index_by_id.get(dependency)
                outcome = action_outcomes.get(parent_index)
                parent = results[parent_index - 1] if parent_index is not None else None
                if (outcome is None or not outcome.get("executed", True) or outcome["failed"]
                        or parent is None or parent.status != "completed"):
                    state = parent.status if parent is not None else "missing"
                    reasons.append(f"prerequisite {dependency!r} did not complete in this run (state={state})")
            if reasons:
                action_outcomes[index] = _unstarted_action(result, "; ".join(reasons), blocked=True)
            elif stopped:
                action_outcomes[index] = _unstarted_action(result, stop_reason, blocked=False)
            else:
                ready_wave.append((index, result))

        if not keep_going and any(action_outcomes.get(index, {}).get("failed") for index, _ in wave):
            stop_reason = stop_reason or "execution blocked by unmet preconditions"
            for index, result in ready_wave:
                action_outcomes[index] = _unstarted_action(result, stop_reason, blocked=False)
            ready_wave = []
        safe_parallel_wave = _wave_is_parallel_safe(ready_wave)
        workers = _effective_parallel_workers(parallel_actions, len(ready_wave)) if safe_parallel_wave else 1
        _notify_progress(
            progress_callback,
            started_monotonic=started_monotonic,
            phase="wave",
            message=f"starting wave {wave_number} with {len(ready_wave)} ready action(s)",
            total_actions=total_actions,
            wave_number=wave_number,
        )
        if workers > 1:
            _emit_progress(progress_stream, f"wave {wave_number}: running {len(ready_wave)} independent actions with {workers} workers")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        _execute_action_guarded,
                        result,
                        journal=journal,
                        stage_only=stage_only,
                        action_number=index,
                        total_actions=total_actions,
                        nice_level=max(0, min(19, parallel_nice)),
                        progress_callback=progress_callback,
                        started_monotonic=started_monotonic,
                        wave_number=wave_number,
                    ): (index, result)
                    for index, result in ready_wave
                }
                try:
                    for future in as_completed(futures):
                        index, _result = futures[future]
                        action_outcomes[index] = future.result()
                except KeyboardInterrupt:
                    interrupted = True
                    for future in futures:
                        future.cancel()
            # The pool has joined already-running workers before recording the
            # stopped wave; do not dispatch another wave after interruption.
            for future, (index, result) in futures.items():
                if index not in action_outcomes:
                    action_outcomes[index] = (
                        _unstarted_action(result, "execution interrupted", blocked=False)
                        if future.cancelled() else future.result()
                    )
        else:
            for index, result in ready_wave:
                action_outcomes[index] = _execute_action_guarded(
                    result,
                    journal=journal,
                    stage_only=stage_only,
                    action_number=index,
                    total_actions=total_actions,
                    progress_callback=progress_callback,
                    started_monotonic=started_monotonic,
                    wave_number=wave_number,
                )
                if (action_outcomes[index].get("interrupted")
                        or action_outcomes[index]["failed"] and not keep_going):
                    break

        wave_failed = False
        for index, result in wave:
            outcome = action_outcomes.get(index)
            if outcome is None:
                outcome = _unstarted_action(result, "execution stopped before dispatch", blocked=False)
                action_outcomes[index] = outcome
            if outcome.get("executed", True):
                executed_actions += 1
            interrupted = interrupted or bool(outcome.get("interrupted"))
            _emit_progress(progress_stream, str(outcome["log"]).rstrip())
            if actions_path is not None:
                stem = f"{index:04d}-{_safe_action_name(result.action_id)}"
                write_text_shared(actions_path / f"{stem}.log", str(outcome["log"]))
                write_json_shared(
                    actions_path / f"{stem}.json",
                    {"execution_wave": wave_number, "action": result.to_dict()},
                )
            if outcome["failed"]:
                wave_failed = True
                if not stop_reason:
                    stop_reason = f"action did not complete: {result.logical_path} ({result.status})"
        if interrupted:
            stop_reason = "execution interrupted; inspect unknown outcomes before retrying"
        if interrupted or wave_failed and not keep_going:
            stopped = True

    _notify_progress(
        progress_callback,
        started_monotonic=started_monotonic,
        phase="execution",
        message="execution pass complete; writing run artifacts",
        total_actions=total_actions,
    )
    ordered_logs = [str(action_outcomes[index]["log"]) for index, _result in indexed_results if index in action_outcomes]
    if run_path is not None:
        write_text_shared(run_path / "repair.log", "".join(ordered_logs))

    completed_actions = sum(1 for result in results if result.status == "completed")
    failed_actions = sum(1 for result in results if result.status == "failed")
    skipped_actions = sum(1 for result in results if result.status == "completed-with-nonblocking-skips")
    skipped_steps = sum(int(outcome["skipped_steps"]) for outcome in action_outcomes.values())
    review_only_skipped_steps = sum(int(outcome["review_only_skipped_steps"]) for outcome in action_outcomes.values())
    summary = _summarize_apply_results(results)
    summary["executed_actions"] = executed_actions
    summary["completed_actions"] = completed_actions
    summary["failed_actions"] = failed_actions
    summary["blocked_actions"] = sum(result.status == "blocked" for result in results)
    summary["unknown_actions"] = sum(result.status == "unknown" for result in results)
    summary["not_run_actions"] = sum(result.status == "not-run" for result in results)
    summary["completed_with_nonblocking_skips"] = skipped_actions
    summary["completed_with_skips"] = skipped_actions
    summary["skipped_steps"] = skipped_steps
    summary["review_only_skipped_steps"] = review_only_skipped_steps
    summary["execution_waves"] = len(waves)
    summary["parallel_actions_requested"] = max(1, parallel_actions)
    summary["parallel_cpu_ceiling"] = max(1, (os.cpu_count() or 1) - 1)
    summary["parallel_nice"] = max(0, min(19, parallel_nice))
    report = {
        "schema_version": 1,
        "attempt_id": journal.attempt_id,
        "attempt_dir": str(journal.path),
        "execution_mode": "execute",
        "started_at": started_at,
        "finished_at": _now_iso(),
        "run_dir": str(run_path) if run_path else "",
        "stop_reason": stop_reason,
        "interrupted": interrupted,
        "summary": summary,
        "actions": [result.to_dict() for result in results],
    }
    if run_path is not None:
        write_json_shared(run_path / "execute.json", report)
        write_json_shared(
            run_path / "status.json",
            {
                "schema_version": 1,
                "phase": "execute-interrupted" if interrupted else "execute-complete",
                "interrupted": interrupted,
                "started_at": started_at,
                "finished_at": report["finished_at"],
                "run_dir": str(run_path),
                "stop_reason": stop_reason,
                "summary": summary,
            },
        )
    if interrupted:
        raise KeyboardInterrupt()
    return report


def write_execute_report(path: str | Path, report: dict[str, object]) -> None:
    write_json_shared(path, report)
