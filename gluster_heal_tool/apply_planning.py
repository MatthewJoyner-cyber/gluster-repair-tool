"""Planning helpers for Gluster repair apply results."""
from __future__ import annotations

import json
from posixpath import dirname

from .apply import DEFAULT_REVIEW_POLICIES
from .apply import DEFAULT_SPLIT_BRAIN_NATIVE_POLICY
from .apply import _result_family
from .apply_planning_utils import _action_with_decision_winner
from .apply_planning_utils import _add_revert_dir
from .apply_planning_utils import _append_stale_index_ghost_steps
from .apply_planning_utils import _backup_base
from .apply_planning_utils import _backup_target
from .apply_planning_utils import _basename
from .apply_planning_utils import _copy_identity
from .apply_planning_utils import _deepest_first
from .apply_planning_utils import _directory_child_signatures
from .apply_planning_utils import _directory_conflict_mismatch_hosts
from .apply_planning_utils import _directory_merge_is_safe
from .apply_planning_utils import _directory_tie_decision_choice
from .apply_planning_utils import _directory_tie_effective_choice
from .apply_planning_utils import _file_metadata_mismatch_hosts_from_action
from .apply_planning_utils import _gluster_split_brain_preview
from .apply_planning_utils import _int_value
from .apply_planning_utils import _local_rm_preview
from .apply_planning_utils import _mkdir_preview
from .apply_planning_utils import _mount_mkdir_preview
from .apply_planning_utils import _normalize_decision
from .apply_planning_utils import _normalize_split_brain_native_policy
from .apply_planning_utils import _normalize_split_brain_policy
from .apply_planning_utils import _representative_copy_for_identity
from .apply_planning_utils import _resolve_file_decision_copy
from .apply_planning_utils import _restore_preview
from .apply_planning_utils import _select_copy_by_metric
from .apply_planning_utils import _select_split_brain_file_copy
from .apply_planning_utils import _ssh_cp_preview
from .apply_planning_utils import _ssh_mkdir_preview
from .apply_planning_utils import _ssh_rm_preview
from .apply_planning_utils import _ssh_rmr_preview
from .apply_planning_utils import _ssh_setfattr_preview
from .apply_planning_utils import _stage_preview
from .apply_planning_utils import _step_id
from .models import ApplyActionResult, ApplyStep, BackupArtifact
from .role_safety import enforce_apply_role_safety
from .apply_binding import validate_plan_context

from .apply_planning_file import _plan_repair_file
from .apply_planning_file import _plan_repair_file_metadata
from .apply_planning_file import _plan_repair_posix_metadata

from .apply_planning_review import _plan_cleanup_dead_file_refs
from .apply_planning_review import _plan_cleanup_dead_gfid
from .apply_planning_review import _plan_cleanup_orphaned_symlink
from .apply_planning_review import _plan_cleanup_stale_glusterfs_index
from .apply_planning_review import _plan_delete_review_directory
from .apply_planning_review import _plan_delete_review_file
from .apply_planning_review import _plan_reconcile_directory
from .apply_planning_review_action import _plan_review_action
from .install_paths import DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH


def build_apply_results(
    plan: dict[str, object],
    *,
    execution_mode: str = "dry-run",
    backup_root: str | None = None,
    backup_mode: str = "required",
    batch: bool = False,
    review_policies: dict[str, str] | None = None,
    split_brain_native_policy: str = DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
    decisions: dict[str, dict[str, object]] | None = None,
    volume: str = "",
    brick_path: str = "",
    worker_path: str = str(DEFAULT_WORKER_PATH),
    ssh_user: str = DEFAULT_SERVICE_USER,
    scope: str = "all",
) -> list[ApplyActionResult]:
    validate_plan_context(plan, volume=volume, brick_path=brick_path)
    results: list[ApplyActionResult] = []
    review_policies = {**DEFAULT_REVIEW_POLICIES, **(review_policies or {})}
    for action in plan.get("actions", []):
        action_type = action.get("action_type")
        if action_type == "repair_file":
            results.append(
                _plan_repair_file(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    volume=volume,
                    brick_path=brick_path,
                    split_brain_native_policy=split_brain_native_policy,
                    worker_path=worker_path,
                    ssh_user=ssh_user,
                )
            )
            continue
        if action_type == "repair_file_metadata":
            results.append(
                _plan_repair_file_metadata(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    volume=volume,
                    brick_path=brick_path,
                )
            )
            continue
        if action_type == "repair_posix_metadata":
            results.append(
                _plan_repair_posix_metadata(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    volume=volume,
                    brick_path=brick_path,
                    ssh_user=ssh_user,
                )
            )
            continue
        if action_type == "reconcile_directory":
            results.append(
                _plan_reconcile_directory(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type == "repair_directory_metadata":
            results.append(
                _plan_reconcile_directory(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type == "cleanup_dead_file_refs":
            results.append(
                _plan_cleanup_dead_file_refs(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type == "cleanup_arbiter_residue":
            results.append(
                _plan_cleanup_dead_file_refs(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type == "cleanup_stale_glusterfs_index":
            results.append(
                _plan_cleanup_stale_glusterfs_index(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type == "cleanup_dead_gfid":
            results.append(
                _plan_cleanup_dead_gfid(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type == "cleanup_orphaned_symlink":
            results.append(
                _plan_cleanup_orphaned_symlink(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
            )
            continue
        if action_type in {
            "review_entry_split_brain",
            "review_probable_stale_survivor",
            "review_probable_orphaned_symlink",
            "review_directory_children",
            "review_directory_gfid_conflict",
            "review_directory_metadata",
            "review_unclassified_authority",
            "review_dead_gfid_reference",
            "review_type_mismatch",
            "review_file_metadata",
            "review_posix_metadata_no_majority",
            "defer_dead_gfid_cleanup",
        }:
            results.append(
                _plan_review_action(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    review_policies=review_policies,
                    split_brain_native_policy=split_brain_native_policy,
                    decisions=decisions,
                    volume=volume,
                    brick_path=brick_path,
                    worker_path=worker_path,
                    ssh_user=ssh_user,
                )
            )
    action_schedule = {
        str(action.get("action_id") or ""): action
        for action in plan.get("actions", [])
        if isinstance(action, dict)
    }
    for result in results:
        schedule = action_schedule.get(result.action_id) or {}
        result.execution_wave = int(schedule.get("execution_wave") or 0)
        result.parallel_safe = bool(schedule.get("parallel_safe"))
        result.execution_resources = [str(item) for item in schedule.get("execution_resources") or [] if str(item)]
        result.execution_serialization_reason = str(schedule.get("execution_serialization_reason") or "")
        result.brick_roles_by_host = {
            str(host): str(role)
            for host, role in (schedule.get("brick_roles_by_host") or {}).items()
            if str(host) and str(role)
        }
        result.brick_host_aliases = {
            str(host): [str(alias) for alias in aliases if str(alias)]
            for host, aliases in (schedule.get("brick_host_aliases") or {}).items()
            if str(host) and isinstance(aliases, list)
        }
        result.brick_role_evidence_required = bool(schedule.get("brick_role_evidence_required"))
        result.brick_role_evidence_error = str(schedule.get("brick_role_evidence_error") or "")
        enforce_apply_role_safety(schedule, result)

    if scope and scope != "all":
        allowed = {part.strip() for part in scope.split(",") if part.strip()}
        results = [result for result in results if _result_family(result) in allowed]
    return results
