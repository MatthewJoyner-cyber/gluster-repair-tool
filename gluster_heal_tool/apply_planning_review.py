# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Review and cleanup planning helpers for Gluster repair apply results."""
from __future__ import annotations

import json
from posixpath import dirname

from .apply import DEFAULT_REVIEW_POLICIES
from .apply import DEFAULT_SPLIT_BRAIN_NATIVE_POLICY
from .apply import _result_family
from .apply_planning_file import _plan_repair_file
from .apply_planning_file import _plan_repair_file_metadata
from .apply_planning_utils import _action_with_decision_winner
from .apply_planning_utils import _add_revert_dir
from .apply_planning_utils import _append_stale_index_ghost_steps
from .apply_planning_utils import _backup_target
from .apply_planning_utils import _basename
from .apply_planning_utils import _deepest_first
from .apply_planning_utils import _directory_conflict_mismatch_hosts
from .apply_planning_utils import _directory_merge_is_safe
from .apply_planning_utils import _directory_tie_decision_choice
from .apply_planning_utils import _directory_tie_effective_choice
from .apply_planning_utils import _file_metadata_mismatch_hosts_from_action
from .apply_planning_utils import _int_value
from .apply_planning_utils import _local_rm_preview
from .apply_planning_utils import _mount_mkdir_preview
from .apply_planning_utils import _mount_restore_preview
from .apply_planning_utils import _normalize_decision
from .apply_planning_utils import _normalize_split_brain_native_policy
from .apply_planning_utils import _normalize_split_brain_policy
from .apply_planning_utils import _file_quarantine_target
from .apply_planning_utils import _directory_quarantine_target
from .apply_planning_utils import _resolve_file_decision_copy
from .apply_planning_utils import _select_split_brain_file_copy
from .remote_ops import rsync_brick_pull_command
from .apply_planning_utils import _ssh_cp_preview
from .apply_planning_utils import _ssh_getfattr_preview
from .apply_planning_utils import _ssh_mkdir_preview
from .apply_planning_utils import _ssh_rm_preview
from .apply_planning_utils import _ssh_rmr_preview
from .apply_planning_utils import _ssh_relink_gfid_preview
from .apply_planning_utils import _ssh_set_mdata_preview
from .apply_planning_utils import _ssh_mv_preview
from .apply_planning_utils import _ssh_setfattr_preview
from .apply_planning_utils import _stage_tree_contents_preview
from .apply_planning_utils import _mount_tree_restore_preview
from .apply_planning_utils import _step_id
from .controller_paths import default_stage_local_path
from .models import ApplyActionResult, ApplyStep, BackupArtifact

def _plan_reconcile_directory(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    volume: str = "",
    brick_path: str = "",
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    mounted_target = str(action.get("mounted_target") or "")
    repair_strategy = str(action.get("repair_strategy") or "reconcile_directory_set")

    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action["action_type"]),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="planned",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    result.notes.append(f"repair strategy: {repair_strategy}")

    index = 1
    if action.get("depends_on"):
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "wait-children"),
                step_type="wait_for_child_repairs",
                target_path=mounted_target,
                notes=[
                    "directory reconciliation should run only after all child repair actions succeed",
                    f"depends on: {', '.join(action.get('depends_on') or [])}",
                ],
            )
        )
        index += 1

    backups_enabled = backup_mode != "none"
    if backups_enabled and backup_root:
        result.notes.append(f"backup root override: {backup_root}")
    if repair_strategy == "restore_missing_directory":
        result.notes.append("restore directory presence on missing replicas after child repairs")
        result.status = "proposed"
    elif repair_strategy == "cleanup_directory_metadata":
        result.notes.append("remove stale directory GFID metadata while leaving child-set state intact")
        if mounted_target:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "cleanup-directory-metadata"),
                    step_type="cleanup_directory_metadata",
                    target_path=mounted_target,
                    via_mount=True,
                    notes=[
                        "remove only stale directory GFID metadata; do not recreate or delete the directory tree",
                        "this stays review-only until we have a dedicated executor path",
                    ],
                )
            )
            return result
    elif repair_strategy == "reconcile_directory_children":
        child_names = sorted(
            {
                str(child)
                for missing_children in (action.get("missing_directory_children_by_host") or {}).values()
                for child in (missing_children or [])
                if str(child)
            }
        )
        if child_names:
            result.notes.append(f"missing child entries: {', '.join(child_names)}")
        if action.get("depends_on"):
            result.notes.append(
                "the child actions carry the actual backend/GFID repair; this parent action only waits for them and requires a fresh heal/rescan"
            )
            result.notes.append(
                "do not rewrite the already-present parent directory or reattach its GFID as a substitute for repairing the child"
            )
            return result
        result.status = "review"
        result.notes.append(
            "no executable child action is attached; parent presence alone is not enough to recreate a missing child"
        )
        result.notes.append(
            "collect immediate GFID-child evidence for each missing child with mount probing off, then rebuild the plan"
        )
        return result
    elif repair_strategy == "review_directory_state":
        result.notes.append("directory is already present; review-only action")
    elif repair_strategy in {"recreate_missing_directory_backend", "recreate_missing_directory_backend_child_gap"}:
        missing_hosts = [str(host) for host in action.get("missing_hosts") or [] if str(host)]
        target_backends = {
            str(host): str(path)
            for host, path in (action.get("directory_target_backend_by_host") or {}).items()
            if str(host) and str(path)
        }
        missing_targets = [host for host in missing_hosts if not target_backends.get(host)]
        if missing_targets:
            result.status = "review"
            result.notes.append(
                "backend recreation is missing an exact target path for: " + ", ".join(missing_targets)
            )
            result.notes.append(
                "do not substitute the canonical brick path; refresh evidence until every missing host has an explicit backend target"
            )
            return result
        result.status = "proposed"
        if repair_strategy == "recreate_missing_directory_backend_child_gap":
            result.notes.append(
                "directory child-gap variant: keep the backend recreate explicit while the child repair chain is still being worked"
            )
        result.notes.append("brick-side directory recreation is required; mount mkdir is not the repair")
        result.notes.append("use the canonical host/backend/GFID evidence to rebuild the missing brick entry")
    elif repair_strategy == "restore_directory_children_bounded":
        result.status = "proposed"
        result.notes.append(
            "leaf-first bounded subtree repair: stage the canonical subtree locally, push it to the missing brick, then re-check the brick-side GFID and child set"
        )
        result.notes.append(
            "this branch stays canary-first; use it only after the bounded subtree shape is proven on gtest"
        )
    elif repair_strategy == "canonical_content_recreate":
        result.status = "proposed"
        result.notes.append(
            "canonical recreate fallback: quarantine the broken backend state, then rewrite the subtree through the Gluster mount"
        )
        result.notes.append(
            "this fallback preserves chosen content, not historical GFID identity; use it only when GFID-preserving repair is no longer safe"
        )
    elif repair_strategy == "attach_directory_gfid":
        mismatch_hosts = [
            str(host)
            for host in action.get("directory_metadata_mismatch_hosts") or []
            if str(host)
        ]
        result.status = "proposed"
        result.notes.append("directory exists everywhere; attach the canonical trusted.gfid to the mismatched bricks")
        if mismatch_hosts:
            result.notes.append(f"metadata mismatch hosts: {', '.join(mismatch_hosts)}")
    elif repair_strategy == "attach_directory_mdata":
        mismatch_hosts = [
            str(host)
            for host in action.get("directory_mdata_mismatch_hosts") or []
            if str(host)
        ]
        canonical_mdata = str(action.get("directory_canonical_mdata_hex") or "")
        result.status = "proposed"
        result.notes.append(
            "directory exists everywhere; align trusted.glusterfs.mdata on the minority bricks to the clear majority"
        )
        if canonical_mdata:
            result.notes.append(f"canonical directory mdata: {canonical_mdata}")
        if mismatch_hosts:
            result.notes.append(f"mdata mismatch hosts: {', '.join(mismatch_hosts)}")

    revert_index = 1
    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(
                action,
                backup_root,
                str(host),
                "dir-gfid",
                _basename(str(path)),
            )
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="dir_gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["directory GFID backup is informational and best-effort"],
                    )
                )
                result.estimated_unknown_backup_items += 1
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-dir-gfid"),
                        step_type="backup_stale_dir_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort capture of stale directory GFID metadata before removal"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-dir-gfid"),
                    step_type="remove_stale_dir_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["remove stale directory GFID metadata before parent reconciliation"],
                )
            )
            index += 1
            index = _append_stale_index_ghost_steps(
                result,
                action_id=action_id,
                host=str(host),
                path=str(path),
                index=index,
            )
            if backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-dir-gfid"),
                        step_type="restore_dir_gfid_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=["best-effort only; exact original GFID state is not guaranteed"],
                    )
                )
                revert_index += 1

    if repair_strategy in {"recreate_missing_directory_backend", "recreate_missing_directory_backend_child_gap"}:
        canonical_host = str(action.get("directory_canonical_host") or "")
        canonical_backend = str(action.get("directory_canonical_backend") or "")
        canonical_gfid = str(action.get("directory_canonical_gfid") or "")
        missing_hosts = [str(host) for host in action.get("missing_hosts") or [] if str(host)]
        target_backends = {
            str(host): str(path)
            for host, path in (action.get("directory_target_backend_by_host") or {}).items()
            if str(host) and str(path)
        }
        for host in missing_hosts:
            recreated_target = target_backends[host]
            step_type = (
                "mkdir_directory_backend_child_gap"
                if repair_strategy == "recreate_missing_directory_backend_child_gap"
                else "mkdir_directory_backend"
            )
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"mkdir-dir-backend-{host}"),
                    step_type=step_type,
                    host=host,
                    target_path=recreated_target,
                    command_preview=_ssh_mkdir_preview(host, recreated_target),
                    notes=[
                        "brick-side directory recreation target",
                        f"canonical host: {canonical_host or 'unknown'}",
                        f"canonical gfid: {canonical_gfid or 'unknown'}",
                        "exact target comes from fresh evidence for this missing host",
                    ],
                )
            )
            index += 1
            if canonical_gfid:
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"attach-dir-gfid-{host}"),
                        step_type="attach_directory_gfid",
                        host=host,
                        source_path=canonical_gfid,
                        target_path=recreated_target,
                        command_preview=_ssh_setfattr_preview(
                            host,
                            recreated_target,
                            canonical_gfid,
                        ),
                        notes=[
                            "reattach the canonical directory GFID to the recreated backend directory",
                            "this is the missing brick-side identity step after mkdir",
                        ],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"verify-dir-backend-{host}"),
                    step_type="verify_directory_backend",
                    host=host,
                    target_path=recreated_target,
                    command_preview=_ssh_getfattr_preview(host, recreated_target),
                    notes=[
                        "post-repair verification: confirm the recreated backend directory now exposes trusted.gfid on the brick",
                        "this is a real verification step, not a review-only tail",
                    ],
                )
            )
            index += 1
            result.revert_steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, revert_index, f"revert-dir-backend-{host}"),
                    step_type="remove_stale_backend",
                    host=host,
                    target_path=recreated_target,
                    tolerate_missing=True,
                    command_preview=_ssh_rmr_preview(host, recreated_target),
                    notes=[
                        "rollback for the recreated backend directory; remove only the child created by this repair",
                        "revert remains best-effort and should be confirmed against the live brick state",
                    ],
                )
            )
            revert_index += 1
    elif repair_strategy == "restore_directory_children_bounded":
        canonical_host = str(action.get("directory_canonical_host") or "")
        canonical_backend = str(action.get("directory_canonical_backend") or "")
        canonical_gfid = str(action.get("directory_canonical_gfid") or "")
        stage_local_path = str(action.get("stage_local_path") or "")
        child_gap_hosts = sorted(
            host
            for host, missing_children in (action.get("missing_directory_children_by_host") or {}).items()
            if missing_children
        )
        if not child_gap_hosts:
            child_gap_hosts = sorted(str(host) for host in (action.get("missing_hosts") or []) if str(host))
        if not stage_local_path and logical_path:
            stage_local_path = default_stage_local_path(logical_path)
        if not (canonical_host and canonical_backend and canonical_gfid and child_gap_hosts):
            result.status = "review"
            result.notes.append(
                "bounded subtree restore is missing canonical backend or gap-host evidence; keep this path review-only until the canary proves it"
            )
            return result
        if canonical_host and canonical_backend and stage_local_path:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "stage-directory-subtree"),
                    step_type="stage_directory_subtree_local",
                    host=canonical_host,
                    source_path=canonical_backend,
                    target_path=stage_local_path,
                    command_preview=_stage_tree_contents_preview(
                        canonical_host,
                        canonical_backend,
                        stage_local_path,
                    ),
                    notes=[
                        "stage the canonical directory subtree locally before pushing it to the missing brick",
                        "leaf-first repair: preserve the known-good subtree as the source of truth for the bounded restore",
                    ],
                )
            )
            index += 1
        if child_gap_hosts:
            result.notes.append(
                f"directory child-gap hosts targeted by bounded subtree restore: {', '.join(child_gap_hosts)}"
            )
        for host in child_gap_hosts:
            backup_path = _backup_target(
                action,
                backup_root,
                host,
                "directory_subtree",
                _basename(canonical_backend or logical_path),
            )
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=host,
                        source_path=canonical_backend,
                        backup_path=backup_path,
                        kind="directory_subtree",
                        estimated_bytes=None,
                        required_for_revert=backup_mode == "required",
                        notes=[
                            "best-effort backup of the existing backend subtree before bounded restore",
                        ],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"backup-directory-subtree-{host}"),
                        step_type="backup_directory_subtree",
                        host=host,
                        source_path=canonical_backend,
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(host, canonical_backend, backup_path),
                        notes=[
                            "capture the pre-existing backend subtree before the bounded restore replaces it",
                        ],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"restore-directory-subtree-{host}"),
                    step_type="restore_directory_subtree_bounded",
                    host=host,
                    source_path=stage_local_path,
                    target_path=canonical_backend,
                    command_preview=_push_tree_contents_preview(host, stage_local_path, canonical_backend),
                    notes=[
                        "push the staged canonical subtree to the missing brick",
                        f"canonical host: {canonical_host or 'unknown'}",
                        f"canonical gfid: {canonical_gfid or 'unknown'}",
                        "this is the bounded leaf-first restore path; it should remain reversible through the backup step",
                    ],
                )
            )
            index += 1
            if canonical_gfid and canonical_backend:
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"attach-directory-subtree-gfid-{host}"),
                        step_type="attach_directory_gfid",
                        host=host,
                        source_path=canonical_gfid,
                        target_path=canonical_backend,
                        command_preview=_ssh_setfattr_preview(host, canonical_backend, canonical_gfid),
                        notes=[
                            "reattach the canonical directory GFID after the subtree copy",
                            "this keeps the bounded restore aligned with the original directory identity",
                        ],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"verify-directory-subtree-{host}"),
                    step_type="verify_directory_backend_child_gap",
                    host=host,
                    target_path=canonical_backend,
                    command_preview=_ssh_getfattr_preview(host, canonical_backend),
                    notes=[
                        "confirm the bounded restore still exposes the canonical trusted.gfid on the brick",
                        "follow up with a fresh manifest-build if you want to re-evaluate the child set",
                    ],
                )
            )
            index += 1
            result.revert_steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, revert_index, f"revert-directory-subtree-{host}"),
                    step_type="remove_stale_backend",
                    host=host,
                    target_path=canonical_backend,
                    tolerate_missing=True,
                    command_preview=_ssh_rmr_preview(host, canonical_backend),
                    notes=[
                        "rollback for the bounded subtree restore; remove the rebuilt backend subtree if the operator rejects the repair",
                    ],
                )
            )
            revert_index += 1
            if backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, f"restore-directory-subtree-backup-{host}"),
                        step_type="restore_directory_subtree_backup",
                        host=host,
                        source_path=backup_path,
                        target_path=canonical_backend,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(host, backup_path, canonical_backend),
                        notes=[
                            "best-effort revert of the original backend subtree after removing the bounded repair",
                        ],
                    )
                )
                revert_index += 1
        if child_gap_hosts:
            result.notes.append(
                "bounded subtree restore remains a canary-driven leaf-first path; rerun manifest-build and plan-build after execution"
            )
    elif repair_strategy == "canonical_content_recreate":
        canonical_host = str(action.get("directory_canonical_host") or "")
        canonical_backend = str(action.get("directory_canonical_backend") or "")
        canonical_gfid = str(action.get("directory_canonical_gfid") or "")
        stage_local_path = str(action.get("stage_local_path") or "")
        if not stage_local_path and logical_path:
            stage_local_path = default_stage_local_path(logical_path)
        if not (canonical_host and canonical_backend and canonical_gfid and mounted_target):
            result.status = "review"
            result.notes.append(
                "canonical recreate is missing canonical backend or mount evidence; keep this path review-only until the canary proves it"
            )
            return result
        if canonical_host and canonical_backend and stage_local_path:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "stage-directory-canonical"),
                    step_type="stage_directory_subtree_local",
                    host=canonical_host,
                    source_path=canonical_backend,
                    target_path=stage_local_path,
                    command_preview=_stage_tree_contents_preview(
                        canonical_host,
                        canonical_backend,
                        stage_local_path,
                    ),
                    notes=[
                        "stage the canonical directory subtree locally before rewriting the mount path",
                        "canonical recreate preserves the chosen content, then lets Gluster repopulate the missing brick through the mount",
                    ],
                )
            )
            index += 1
        quarantine_targets: list[str] = []
        for host in sorted(set(str(item) for item in (action.get("missing_hosts") or []) if str(item))):
            quarantine_target = _directory_quarantine_target(
                canonical_backend,
                action_id=action_id,
                host=host,
                identity=canonical_gfid or canonical_backend,
                mode="canonical_recreate",
            )
            quarantine_targets.append(f"{host}:{canonical_backend} -> {quarantine_target}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"backup-directory-canonical-{host}"),
                    step_type="backup_directory_subtree",
                    host=host,
                    source_path=canonical_backend,
                    target_path=quarantine_target,
                    tolerate_missing=True,
                    command_preview=_ssh_cp_preview(host, canonical_backend, quarantine_target),
                    notes=[
                        "copy the broken backend directory aside before rewriting through the mount",
                        f"quarantine target: {quarantine_target}",
                    ],
                )
            )
            index += 1
            result.revert_steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, revert_index, f"restore-directory-canonical-backup-{host}"),
                    step_type="restore_directory_subtree_backup",
                    host=host,
                    source_path=quarantine_target,
                    target_path=canonical_backend,
                    tolerate_missing=True,
                    command_preview=_ssh_cp_preview(host, quarantine_target, canonical_backend),
                    notes=[
                        "best-effort revert for the quarantined backend directory",
                        f"restore target: {canonical_backend}",
                    ],
                )
            )
            revert_index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"remove-directory-canonical-{host}"),
                    step_type="remove_stale_backend",
                    host=host,
                    target_path=canonical_backend,
                    tolerate_missing=True,
                    command_preview=_ssh_rmr_preview(host, canonical_backend),
                    notes=[
                        "remove the original backend tree after copying it aside",
                        "this leaves the quarantine copy behind for operator review",
                    ],
                )
            )
            index += 1
        if quarantine_targets:
            result.notes.append(f"quarantine targets: {', '.join(quarantine_targets)}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "restore-directory-canonical-mount"),
                step_type="restore_via_mount_child_gap",
                source_path=stage_local_path,
                target_path=mounted_target,
                command_preview=_mount_tree_restore_preview(stage_local_path, mounted_target),
                notes=[
                    "rewrite the canonical subtree through the mounted Gluster path so Gluster repopulates the missing child set",
                    "this is the explicit content-recreate fallback; preserve the selected content, not the old GFID identity",
                ],
            )
        )
        index += 1
        for host in sorted(set(str(item) for item in (action.get("missing_hosts") or []) if str(item))):
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"verify-directory-canonical-{host}"),
                    step_type="verify_directory_backend_child_gap",
                    host=host,
                    target_path=canonical_backend,
                    command_preview=_ssh_getfattr_preview(host, canonical_backend),
                    notes=[
                        "verify the rebuilt backend directory now exposes trusted.gfid on the sink brick",
                        "follow up with a fresh manifest-build and plan-build to confirm the child gap is gone",
                    ],
                )
            )
            index += 1
        result.revert_steps.append(
            ApplyStep(
                step_id=_step_id(action_id, revert_index, "remove-canonical-mount-rewrite"),
                step_type="remove_stale_backend",
                target_path=mounted_target,
                tolerate_missing=True,
                command_preview=_local_rm_preview(mounted_target),
                notes=[
                    "best-effort rollback for the mount rewrite; remove only the recreated mount subtree if the operator rejects the fallback",
                ],
            )
        )
        revert_index += 1
    elif repair_strategy == "attach_directory_gfid":
        canonical_gfid = str(action.get("directory_canonical_gfid") or "")
        canonical_backend = str(action.get("directory_canonical_backend") or "")
        backend_by_host = {
            str(host): str(path)
            for host, path in (action.get("directory_backend_by_host") or {}).items()
            if str(host) and str(path)
        }
        backend_by_host.update(
            {
                str(host): str(path)
                for host, path in (action.get("directory_target_backend_by_host") or {}).items()
                if str(host) and str(path)
            }
        )
        arbiter_gfid_hosts = {
            str(host) for host in action.get("arbiter_gfid_repair_hosts") or [] if str(host)
        }
        arbiter_gfid_roots = {
            str(host): str(path)
            for host, path in (action.get("arbiter_gfid_repair_backend_roots_by_host") or {}).items()
            if str(host) and str(path)
        }
        arbiter_gfid_stale_paths = {
            str(host): str(path)
            for host, path in (action.get("arbiter_gfid_stale_paths_by_host") or {}).items()
            if str(host) and str(path)
        }
        mismatch_hosts = list(action.get("directory_metadata_mismatch_hosts") or [])
        for host in mismatch_hosts:
            target_backend = backend_by_host.get(str(host), canonical_backend)
            if canonical_gfid and target_backend:
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"attach-dir-metadata-{host}"),
                        step_type="attach_directory_gfid",
                        host=str(host),
                        source_path=canonical_gfid,
                        target_path=target_backend,
                        command_preview=_ssh_setfattr_preview(
                            str(host), target_backend, canonical_gfid
                        ),
                        notes=[
                            "reattach the canonical directory GFID to the existing backend directory",
                            "this repairs metadata drift without recreating the directory tree",
                        ],
                    )
                )
                index += 1
                if str(host) in arbiter_gfid_hosts:
                    backend_root = arbiter_gfid_roots.get(str(host), "")
                    if not backend_root:
                        result.status = "blocked"
                        result.steps = []
                        result.notes.append(f"arbiter GFID repair is missing the exact backend root for {host}")
                        return result
                    result.steps.append(
                        ApplyStep(
                            step_id=_step_id(action_id, index, f"relink-arbiter-dir-gfid-{host}"),
                            step_type="relink_arbiter_directory_gfid",
                            host=str(host),
                            target_path=target_backend,
                            command_preview=_ssh_relink_gfid_preview(
                                str(host), backend_root, target_backend, directory=True
                            ),
                            notes=[
                                "rebuild the missing .glusterfs symlink for the existing arbiter directory placeholder",
                                "no arbiter subtree is read or used as a source",
                            ],
                        )
                    )
                    index += 1
                    stale_handle = arbiter_gfid_stale_paths.get(str(host), "")
                    if stale_handle:
                        stale_target = _directory_quarantine_target(
                            stale_handle,
                            action_id=action_id,
                            host=str(host),
                            identity="stale-arbiter-gfid",
                            mode="stale_arbiter_gfid",
                        )
                        result.steps.append(
                            ApplyStep(
                                step_id=_step_id(action_id, index, f"quarantine-stale-arbiter-dir-gfid-{host}"),
                                step_type="quarantine_stale_arbiter_directory_gfid",
                                host=str(host),
                                source_path=stale_handle,
                                target_path=stale_target,
                                command_preview=_ssh_mv_preview(str(host), stale_handle, stale_target),
                                notes=[
                                    "move the old conflicting arbiter .glusterfs handle aside after relinking the canonical handle",
                                    "the named quarantine target is the rollback artifact for the stale handle",
                                ],
                            )
                        )
                        result.revert_steps.append(
                            ApplyStep(
                                step_id=_step_id(action_id, len(result.revert_steps) + 1, f"restore-stale-arbiter-dir-gfid-{host}"),
                                step_type="restore_quarantined_stale_arbiter_directory_gfid",
                                host=str(host),
                                source_path=stale_target,
                                target_path=stale_handle,
                                tolerate_missing=True,
                                command_preview=_ssh_mv_preview(str(host), stale_target, stale_handle),
                                notes=["best-effort revert for the stale arbiter handle quarantine"],
                            )
                        )
                        index += 1
    elif repair_strategy == "attach_directory_mdata":
        canonical_mdata = str(action.get("directory_canonical_mdata_hex") or "")
        canonical_backend = str(action.get("directory_canonical_backend") or "")
        backend_by_host = {
            str(host): str(path)
            for host, path in (action.get("directory_backend_by_host") or {}).items()
            if str(host) and str(path)
        }
        mismatch_hosts = list(action.get("directory_mdata_mismatch_hosts") or [])
        for host in mismatch_hosts:
            target_backend = backend_by_host.get(str(host), canonical_backend)
            if canonical_mdata and target_backend:
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"attach-dir-mdata-{host}"),
                        step_type="attach_directory_mdata",
                        host=str(host),
                        source_path=canonical_mdata,
                        target_path=target_backend,
                        command_preview=_ssh_set_mdata_preview(str(host), target_backend, canonical_mdata),
                        notes=[
                            "align trusted.glusterfs.mdata to the clear majority value",
                            "this repairs directory ctime/mdata drift without touching trusted.gfid or payload children",
                        ],
                    )
                )
                index += 1
    elif mounted_target and repair_strategy != "review_directory_state":
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "ensure-dir-mount"),
                step_type="ensure_directory_via_mount",
                target_path=mounted_target,
                via_mount=True,
                command_preview=_mount_mkdir_preview(mounted_target),
                    notes=[
                        "ensure the logical directory exists through the mounted Gluster path"
                        if repair_strategy == "restore_missing_directory"
                        else "refresh the parent directory through the mounted Gluster path after child reconciliation",
                        *(
                            ["parent refresh only: missing backend child entries are not repaired by this step"]
                            if repair_strategy == "reconcile_directory_children"
                            else ["child repairs should already have reconciled the directory contents"]
                        ),
                        *(
                            ["bounded subtree candidate: future leaf-first restore may be viable, but not yet executable"]
                            if repair_strategy == "reconcile_directory_children"
                            and "directory_child_gap:bounded_subtree_candidate" in action.get("graph_markers", [])
                            else []
                        ),
                    ],
                )
            )
        result.revert_dirs_to_create.append(mounted_target)
        index += 1
        result.revert_steps.append(
            ApplyStep(
                step_id=_step_id(action_id, revert_index, "review-dir-revert"),
                step_type="review_directory_revert",
                target_path=mounted_target,
                via_mount=True,
                notes=[
                    "directory revert is review-only; do not blindly remove a directory that may now contain repaired children"
                ],
            )
        )
    elif mounted_target:
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "review-dir-state"),
                step_type="review_directory_state",
                target_path=mounted_target,
                via_mount=True,
                notes=["no directory create step planned; review current parent state only"],
            )
        )

    if result.estimated_unknown_backup_items:
        result.notes.append(
            f"backup size unknown for {result.estimated_unknown_backup_items} directory GFID artifacts"
        )
    result.notes.append("directory revert is best-effort and should be reviewed manually")
    return result


def _directory_copy_matches_canonical(
    copy: dict[str, object],
    *,
    canonical_host: str,
    canonical_backend: str,
    canonical_gfid: str,
) -> bool:
    host = str(copy.get("host") or "").strip()
    backend = str(copy.get("backend") or "").strip()
    identity = str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("gfid") or "").strip()
    if canonical_gfid and identity:
        return identity == canonical_gfid
    if canonical_host:
        return host == canonical_host
    if canonical_backend:
        return backend == canonical_backend
    return False


def _plan_quarantine_directory_conflict(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    quarantine_mode: str,
    quarantine_identity: str = "",
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    mounted_target = str(action.get("mounted_target") or "")
    canonical_host = str(action.get("directory_canonical_host") or "")
    canonical_backend = str(action.get("directory_canonical_backend") or "")
    canonical_gfid = str(action.get("directory_canonical_gfid") or "")
    copies = [
        copy
        for copy in action.get("directory_copies") or []
        if isinstance(copy, dict)
        and str(copy.get("host") or "").strip()
        and str(copy.get("backend") or "").strip()
    ]
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action["action_type"]),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )

    selected_copies: list[dict[str, object]] = []
    if quarantine_mode == "quarantine_side":
        quarantine_identity = quarantine_identity.strip()
        selected_copies = [
            copy
            for copy in copies
            if quarantine_identity
            and str(
                copy.get("identity")
                or copy.get("backend_trusted_gfid")
                or copy.get("gfid")
                or ""
            ).strip() == quarantine_identity
        ]
        if not quarantine_identity:
            result.notes.append(
                "quarantine_side requires the exact directory cohort GFID from fresh evidence"
            )
        elif not selected_copies:
            result.notes.append(
                f"selected directory cohort GFID {quarantine_identity} is absent from fresh evidence"
            )
    if quarantine_mode == "quarantine_loser":
        selected_copies = [
            copy
            for copy in copies
            if not _directory_copy_matches_canonical(
                copy,
                canonical_host=canonical_host,
                canonical_backend=canonical_backend,
                canonical_gfid=canonical_gfid,
            )
        ]
        if not (canonical_host or canonical_backend or canonical_gfid):
            result.notes.append(
                "no canonical side was recorded; quarantine_loser will move every visible copy aside"
            )
        if not selected_copies:
            quarantine_mode = "quarantine_both"
    if quarantine_mode == "quarantine_both":
        selected_copies = list(copies)
    result.notes.append("repair strategy: quarantine_conflicting_directory")
    result.notes.append(f"quarantine mode: {quarantine_mode.removeprefix('quarantine_') or quarantine_mode}")
    result.notes.append("proposed action from review policy; preserve the divergent trees and inspect them later")
    if canonical_host:
        result.notes.append(f"quarantine canonical host: {canonical_host}")
    if canonical_backend:
        result.notes.append(f"quarantine canonical backend: {canonical_backend}")
    if canonical_gfid:
        result.notes.append(f"quarantine canonical gfid: {canonical_gfid}")
    if quarantine_mode == "quarantine_side" and quarantine_identity:
        result.notes.append(f"quarantine selected cohort gfid: {quarantine_identity}")

    if not selected_copies:
        result.status = "review"
        result.notes.append(
            "quarantine could not identify any directory copies to move; leaving the conflict review-only"
        )
        if mounted_target:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-directory-gfid-conflict"),
                    step_type="review_directory_gfid_conflict",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "quarantine requires an identifiable selected directory cohort; keep the conflict under review",
                    ],
                )
            )
        return result

    index = 1
    revert_index = 1
    quarantine_targets: list[str] = []
    for copy in sorted(
        selected_copies,
        key=lambda item: (
            str(item.get("host") or ""),
            str(item.get("backend") or ""),
            str(item.get("identity") or ""),
        ),
    ):
        host = str(copy.get("host") or "")
        source = str(copy.get("backend") or "")
        identity = str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("gfid") or "")
        target = _directory_quarantine_target(
            source,
            action_id=action_id,
            host=host,
            identity=identity,
            mode=quarantine_mode,
        )
        quarantine_targets.append(f"{host}:{source} -> {target}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, f"quarantine-directory-{host}"),
                step_type="quarantine_directory_backend",
                host=host,
                source_path=source,
                target_path=target,
                tolerate_missing=False,
                command_preview=_ssh_mv_preview(host, source, target),
                notes=[
                    "quarantine this backend directory tree so the operator can inspect it later",
                    f"quarantine target: {target}",
                ],
            )
        )
        index += 1
        directory_gfid_path = str(copy.get("gfid_path") or "").strip()
        if directory_gfid_path and directory_gfid_path != source:
            gfid_target = _directory_quarantine_target(
                directory_gfid_path,
                action_id=action_id,
                host=host,
                identity=identity or source,
                mode=quarantine_mode,
            )
            quarantine_targets.append(f"{host}:{directory_gfid_path} -> {gfid_target}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"quarantine-directory-gfid-{host}"),
                    step_type="quarantine_directory_gfid",
                    host=host,
                    source_path=directory_gfid_path,
                    target_path=gfid_target,
                    tolerate_missing=False,
                    command_preview=_ssh_mv_preview(host, directory_gfid_path, gfid_target),
                    notes=[
                        "quarantine the directory GFID link alongside the backend directory",
                        f"quarantine target: {gfid_target}",
                    ],
                )
            )
            index += 1
            result.revert_steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"restore-directory-gfid-{host}"),
                    step_type="restore_quarantined_directory_gfid",
                    host=host,
                    source_path=gfid_target,
                    target_path=directory_gfid_path,
                    tolerate_missing=True,
                    command_preview=_ssh_mv_preview(host, gfid_target, directory_gfid_path),
                    notes=[
                        "best-effort revert for the directory GFID quarantine move",
                        f"restore target: {directory_gfid_path}",
                    ],
                )
            )
            index += 1
        result.revert_steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, f"restore-directory-{host}"),
                step_type="restore_quarantined_directory",
                host=host,
                source_path=target,
                target_path=source,
                tolerate_missing=True,
                command_preview=_ssh_mv_preview(host, target, source),
                notes=[
                    "best-effort revert for the quarantine move",
                    f"restore target: {source}",
                ],
            )
        )
        index += 1

    result.notes.append(
        f"quarantine targets: {', '.join(quarantine_targets)}"
    )
    if quarantine_mode == "quarantine_both":
        result.notes.append(
            "both directory trees are moved aside; the logical path should remain unusable until an operator restores a survivor"
        )
    elif quarantine_mode == "quarantine_side":
        result.notes.append(
            "only the explicitly selected GFID cohort is moved aside; every other cohort remains in place without being declared authoritative"
        )
    else:
        result.notes.append(
            "only the losing tree is moved aside; the remaining tree stays in place for later review"
        )
    if mounted_target:
        result.notes.append(f"mounted target: {mounted_target}")
    result.notes.append("quarantine is reversible by moving the backend path back into place")
    return result


def _plan_quarantine_file_conflict(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    quarantine_mode: str,
    quarantine_only: bool = False,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    mounted_target = str(action.get("mounted_target") or "")
    winner_host = str(action.get("winner_host") or "")
    winner_backend = str(action.get("winner_backend") or "")
    winner_file_gfid = str(action.get("winner_file_gfid") or "")
    copies = [
        copy
        for copy in action.get("file_copies") or []
        if isinstance(copy, dict)
        and str(copy.get("host") or "").strip()
        and str(copy.get("backend") or "").strip()
    ]
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action["action_type"]),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )

    def _copy_matches_winner(copy: dict[str, object]) -> bool:
        host = str(copy.get("host") or "")
        identity = str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("file_gfid") or "")
        if winner_file_gfid and identity and identity == winner_file_gfid:
            return True
        if winner_host and host == winner_host and not winner_backend and not winner_file_gfid:
            return True
        if winner_host and host == winner_host:
            return True
        return False

    selected_copies: list[dict[str, object]] = []
    if quarantine_mode == "quarantine_loser":
        selected_copies = [copy for copy in copies if not _copy_matches_winner(copy)]
        if not (winner_host or winner_backend or winner_file_gfid):
            result.notes.append(
                "no canonical file copy was recorded; quarantine_loser will move every visible copy aside"
            )
        if not selected_copies:
            quarantine_mode = "quarantine_both"
    if quarantine_mode == "quarantine_both":
        selected_copies = list(copies)

    result.notes.append("repair strategy: quarantine_conflicting_file")
    result.notes.append(f"quarantine mode: {quarantine_mode.removeprefix('quarantine_') or quarantine_mode}")
    result.notes.append(
        "proposed action from review policy; preserve the divergent file histories and inspect them later"
    )
    result.notes.append("quarantine is move-only; no temp stage copy is needed to make the move reversible")
    if winner_host:
        result.notes.append(f"quarantine canonical host: {winner_host}")
    if winner_backend:
        result.notes.append(f"quarantine canonical backend: {winner_backend}")
    if winner_file_gfid:
        result.notes.append(f"quarantine canonical gfid: {winner_file_gfid}")

    index = 1
    revert_index = 1
    if not selected_copies:
        result.status = "review"
        result.notes.append(
            "quarantine could not identify any file copies to move; leaving the conflict review-only"
        )
        if mounted_target:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-entry-split-brain-file"),
                    step_type="review_entry_split_brain",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "quarantine requires an identifiable file copy; keep the conflict under review",
                    ],
                )
            )
        return result

    quarantine_targets: list[str] = []
    backfill_revert_steps: list[ApplyStep] = []
    for copy in sorted(
        selected_copies,
        key=lambda item: (
            str(item.get("host") or ""),
            str(item.get("backend") or ""),
            str(item.get("identity") or ""),
        ),
    ):
        host = str(copy.get("host") or "")
        backend = str(copy.get("backend") or "")
        identity = str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("file_gfid") or "")
        backend_target = _file_quarantine_target(
            backend,
            action_id=action_id,
            host=host,
            identity=identity,
            mode=quarantine_mode,
        )
        quarantine_targets.append(f"{host}:{backend} -> {backend_target}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, f"quarantine-file-backend-{host}"),
                step_type="quarantine_file_backend",
                host=host,
                source_path=backend,
                target_path=backend_target,
                tolerate_missing=False,
                command_preview=_ssh_mv_preview(host, backend, backend_target),
                notes=[
                    "quarantine this backend file so the operator can inspect it later",
                    f"quarantine target: {backend_target}",
                ],
            )
        )
        index += 1
        result.revert_steps.append(
            ApplyStep(
                step_id=_step_id(action_id, revert_index, f"restore-file-backend-{host}"),
                step_type="restore_quarantined_file_backend",
                host=host,
                source_path=backend_target,
                target_path=backend,
                tolerate_missing=True,
                command_preview=_ssh_mv_preview(host, backend_target, backend),
                notes=[
                    "best-effort revert for the quarantine move",
                    f"restore target: {backend}",
                ],
            )
        )
        revert_index += 1

        file_gfid_path = str(copy.get("file_gfid_path") or copy.get("gfid_path") or "")
        if file_gfid_path and file_gfid_path != backend:
            file_gfid_target = _file_quarantine_target(
                file_gfid_path,
                action_id=action_id,
                host=host,
                identity=identity or backend,
                mode=quarantine_mode,
            )
            quarantine_targets.append(f"{host}:{file_gfid_path} -> {file_gfid_target}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"quarantine-file-gfid-{host}"),
                    step_type="quarantine_file_gfid",
                    host=host,
                    source_path=file_gfid_path,
                    target_path=file_gfid_target,
                    tolerate_missing=False,
                    command_preview=_ssh_mv_preview(host, file_gfid_path, file_gfid_target),
                    notes=[
                        "quarantine the file-GFID link alongside the backend file",
                        f"quarantine target: {file_gfid_target}",
                    ],
                )
            )
            index += 1
            result.revert_steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, revert_index, f"restore-file-gfid-{host}"),
                    step_type="restore_quarantined_file_gfid",
                    host=host,
                    source_path=file_gfid_target,
                    target_path=file_gfid_path,
                    tolerate_missing=True,
                    command_preview=_ssh_mv_preview(host, file_gfid_target, file_gfid_path),
                    notes=[
                        "best-effort revert for the quarantine move",
                        f"restore target: {file_gfid_path}",
                    ],
                )
            )
            revert_index += 1

        if quarantine_mode == "quarantine_loser" and winner_host and winner_backend and not quarantine_only:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"restore-file-backfill-{host}"),
                    step_type="restore_file_backend_gap_fill",
                    host=host,
                    source_host=winner_host,
                    source_path=winner_backend,
                    target_path=backend,
                    command_preview=rsync_brick_pull_command(
                        host,
                        winner_host,
                        winner_backend,
                        backend,
                    ),
                    notes=[
                        "target brick pulls the winner with privileged brick-side rsync",
                        "this avoids any mount-based restore path and keeps the transfer depth-1 and file-only",
                    ],
                )
            )
            index += 1

            if winner_file_gfid:
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"attach-file-backfill-{host}"),
                        step_type="attach_file_gfid",
                        host=host,
                        source_path=winner_file_gfid,
                        target_path=backend,
                        command_preview=_ssh_setfattr_preview(host, backend, winner_file_gfid),
                        notes=[
                            "reattach the canonical file GFID after the brick-side gap fill",
                            "this keeps the repaired brick aligned with the chosen winner",
                        ],
                    )
                )
                index += 1
            backfill_revert_steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, revert_index, f"revert-file-backfill-{host}"),
                    step_type="remove_stale_backend",
                    host=host,
                    target_path=backend,
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(host, backend),
                    notes=[
                        "best-effort rollback for the brick-side gap fill",
                        "remove the recreated backend file before restoring the quarantined copy if the operator rejects the repair",
                    ],
                )
            )
            revert_index += 1

    if quarantine_only:
        result.notes.append(
            "quarantine-only mode: do not recreate the conflicting data copy; rebuild evidence before choosing any recovery operation"
        )

    if backfill_revert_steps:
        result.revert_steps = backfill_revert_steps + result.revert_steps

    result.notes.append(f"quarantine targets: {', '.join(quarantine_targets)}")
    if quarantine_mode == "quarantine_both":
        result.notes.append(
            "all visible file copies are moved aside; restore one survivor manually after review"
        )
    else:
        result.notes.append(
            "only the losing file copies are moved aside; the remaining copy stays in place for later review"
        )
        if winner_host and winner_backend:
            result.notes.append(
                "the missing brick is refilled by pulling the winner directly from the good brick with rsync"
            )
    if mounted_target:
        result.notes.append(f"mounted target: {mounted_target}")
    result.notes.append("quarantine is reversible by moving the backend paths back into place")
    return result


def _plan_quarantine_type_mismatch(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    quarantine_mode: str = "quarantine_both",
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action["action_type"]),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="review",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    file_copies = list(action.get("file_copies") or [])
    directory_copies = list(action.get("directory_copies") or [])
    loser_side = str(action.get("type_mismatch_loser") or "").strip().lower()
    if quarantine_mode == "quarantine_loser" and loser_side not in {"file", "directory"}:
        result.recommended_choice = "quarantine_both"
        result.recommended_reason = (
            "The plan does not contain complete, non-overlapping backend mtime proof for an older branch."
        )
        result.notes.append(
            "quarantine_loser requires a freshly planned, complete, non-overlapping backend mtime proof; use quarantine_both while the older side is unproven"
        )
        return result

    parts: list[ApplyActionResult] = []
    if file_copies and (quarantine_mode == "quarantine_both" or loser_side == "file"):
        file_action = dict(action)
        file_action["action_id"] = f"{action_id}:files"
        parts.append(
            _plan_quarantine_file_conflict(
                file_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode="quarantine_both",
            )
        )
    if directory_copies and (quarantine_mode == "quarantine_both" or loser_side == "directory"):
        directory_action = dict(action)
        directory_action["action_id"] = f"{action_id}:directories"
        parts.append(
            _plan_quarantine_directory_conflict(
                directory_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode="quarantine_both",
            )
        )
    result.notes.extend(
        [
            "repair strategy: quarantine_conflicting_type_mismatch",
            "file-vs-directory disagreement is quarantined without selecting a source",
            (
                "all visible file and directory branches are moved aside for inspection"
                if quarantine_mode == "quarantine_both"
                else f"the complete older {loser_side} branch is moved aside while the newer branch remains in place"
            ),
            "the logical path remains unresolved until an operator confirms or restores one authoritative survivor",
            "quarantine is reversible by applying the recorded revert moves",
        ]
    )
    for part in parts:
        result.steps.extend(part.steps)
        result.revert_steps.extend(part.revert_steps)
        result.notes.extend(part.notes)
    if result.steps:
        result.status = "proposed"
    else:
        result.notes.append("no complete file/directory backend copies were available; leaving the conflict review-only")
    return result


def _plan_delete_review_file(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    mounted_target = str(action.get("mounted_target") or "")
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action["action_type"]),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    result.notes.append("repair strategy: delete_below_quorum_file")
    result.notes.append("proposed action from review policy; not executable yet")
    backups_enabled = backup_mode != "none"
    index = 1

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "backend", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="backend",
                        estimated_bytes=_int_value(action.get("winner_size")) or None,
                        required_for_revert=backup_mode == "required",
                        notes=["best-effort backup before deleting stale surviving backend file"],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-backend"),
                        step_type="backup_stale_backend",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=backup_mode != "required",
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["backup stale surviving backend file before delete"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-backend"),
                    step_type="remove_stale_backend",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete stale surviving backend file"],
                )
            )
            index += 1

    for host, paths in sorted((action.get("stale_file_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "file-gfid", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="file_gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is best-effort before delete"],
                    )
                )
                result.estimated_unknown_backup_items += 1
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-file-gfid"),
                        step_type="backup_stale_file_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["capture stale file GFID before delete"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-file-gfid"),
                    step_type="remove_stale_file_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete stale surviving file GFID entry"],
                )
            )
            index += 1

    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "gfid", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is best-effort before deleting dead file residue"],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-gfid"),
                        step_type="backup_stale_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort backup before deleting dead file residue"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-gfid"),
                    step_type="remove_stale_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete stale surviving GFID metadata"],
                )
            )
            index += 1

    if mounted_target:
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "rm-mount-file"),
                step_type="remove_restored_mount_file",
                target_path=mounted_target,
                tolerate_missing=True,
                via_mount=True,
                command_preview=_local_rm_preview(mounted_target),
                notes=["remove logical mount file if it still exists"],
            )
        )
    return result


def _plan_cleanup_orphaned_symlink(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    mounted_target = str(action.get("mounted_target") or "")
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type="cleanup_orphaned_symlink",
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    result.notes.append("repair strategy: delete_orphaned_symlink_residue")
    result.notes.append("proposed cleanup action from review policy; inspect before execution")
    backups_enabled = backup_mode != "none"
    index = 1
    winner_host = str(action.get("winner_host") or "")
    winner_backend = str(action.get("winner_backend") or "")
    if winner_host and winner_backend:
        backup_path = _backup_target(action, backup_root, winner_host, "symlink-winner-backend", _basename(winner_backend))
        if backups_enabled:
            result.backup_artifacts.append(
                BackupArtifact(
                    host=winner_host,
                    source_path=winner_backend,
                    backup_path=backup_path,
                    kind="symlink_winner_backend",
                    estimated_bytes=None,
                    required_for_revert=False,
                    notes=["best-effort backup before deleting orphaned symlink residue"],
                )
            )
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "backup-symlink-winner-backend"),
                    step_type="backup_stale_backend",
                    host=winner_host,
                    source_path=winner_backend,
                    target_path=backup_path,
                    tolerate_missing=True,
                    command_preview=_ssh_cp_preview(winner_host, winner_backend, backup_path),
                    notes=["backup surviving orphaned symlink backend before delete"],
                )
            )
            index += 1
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "rm-symlink-winner-backend"),
                step_type="remove_stale_backend",
                host=winner_host,
                target_path=winner_backend,
                tolerate_missing=True,
                command_preview=_ssh_rm_preview(winner_host, winner_backend),
                notes=["delete surviving orphaned symlink backend residue"],
            )
        )
        index += 1

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "symlink-backend", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="symlink_backend",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["best-effort backup before deleting orphaned symlink residue"],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-orphaned-symlink-backend"),
                        step_type="backup_stale_backend",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["backup orphaned symlink residue before delete"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-orphaned-symlink-backend"),
                    step_type="remove_stale_backend",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                notes=["delete orphaned symlink backend residue"],
                )
            )
            index += 1

    for host, paths in sorted((action.get("stale_file_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "symlink-file-gfid", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="symlink_file_gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is best-effort before deleting orphaned symlink residue"],
                    )
                )
                result.estimated_unknown_backup_items += 1
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-orphaned-symlink-file-gfid"),
                        step_type="backup_stale_file_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["capture orphaned symlink GFID before delete"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-orphaned-symlink-file-gfid"),
                    step_type="remove_stale_file_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete orphaned symlink GFID entry"],
                )
            )
            index += 1

    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in paths:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-orphaned-symlink-gfid"),
                    step_type="remove_stale_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete orphaned symlink GFID metadata"],
                )
            )
            index += 1

    return result


def _plan_delete_review_directory(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    mounted_target = str(action.get("mounted_target") or "")
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action["action_type"]),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    result.notes.append("repair strategy: delete_below_quorum_subtree")
    result.notes.append("proposed action from review policy; not executable yet")
    index = 1
    backups_enabled = backup_mode != "none"

    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in _deepest_first(list(paths)):
            backup_path = _backup_target(action, backup_root, str(host), "dir-gfid", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="dir_gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["directory GFID backup is best-effort before subtree delete"],
                    )
                )
                result.estimated_unknown_backup_items += 1
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-dir-gfid"),
                        step_type="backup_stale_dir_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["capture stale directory GFID before subtree delete"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-dir-gfid"),
                    step_type="remove_stale_dir_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete stale directory GFID metadata after subtree cleanup"],
                )
            )
            index += 1

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in _deepest_first(list(paths)):
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-subtree"),
                    step_type="remove_stale_backend",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete stale subtree children deepest-first, then remove the parent directory"],
                )
            )
            index += 1

    if mounted_target:
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "review-mount-subtree-delete"),
                step_type="review_probable_stale_subtree",
                target_path=mounted_target,
                via_mount=True,
                notes=["confirm the logical directory tree should remain deleted on the mounted volume"],
            )
        )
    return result


def _plan_cleanup_dead_gfid(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type="cleanup_dead_gfid",
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    result.notes.append("repair strategy: delete_dead_gfid_residue")
    result.notes.append("interactive choices: delete the dead GFID residue, keep for review, or skip")
    result.notes.append("configured batch policy: delete")
    result.notes.append("proposed cleanup action; confirm no live reference remains before execution")
    backups_enabled = backup_mode != "none"
    index = 1

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "backend", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="backend",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["best-effort backup before deleting dead GFID residue"],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-backend"),
                        step_type="backup_stale_backend",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort backup before deleting dead GFID residue"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-backend"),
                    step_type="remove_stale_backend",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["remove backend residue tied to a confirmed dead GFID"],
                )
            )
            index += 1

    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "gfid", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is best-effort before deleting dead residue"],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-gfid"),
                        step_type="backup_stale_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort backup before deleting dead GFID residue"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-gfid"),
                    step_type="remove_stale_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete confirmed dead GFID residue"],
                )
            )
            index += 1
            index = _append_stale_index_ghost_steps(
                result,
                action_id=action_id,
                host=str(host),
                path=str(path),
                index=index,
            )

    if not result.steps:
        result.status = "review"
        result.notes.append("no residue paths were observed in the manifest; keep for review")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "review-dead-gfid-cleanup"),
                step_type="review_dead_gfid_cleanup",
                notes=["no residue paths were observed in the manifest; recheck before deleting"],
            )
        )
    return result


def _plan_cleanup_dead_file_refs(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=str(action.get("action_type") or "cleanup_dead_file_refs"),
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    repair_strategy = str(action.get("repair_strategy") or "delete_dead_file_ref_residue")
    object_type = str(action.get("object_type") or "file")
    result.notes.append(f"repair strategy: {repair_strategy}")
    arbiter_only = repair_strategy == "delete_arbiter_only_residue"
    if arbiter_only:
        result.notes.append("role gate: both data bricks are absent; delete the arbiter-only residue with backups")
        result.notes.append(
            "rollback recommendation: restore a volume snapshot first; recorded residue backups are a best-effort fallback for deliberately restoring the old arbiter metadata state"
        )
    result.notes.append(
        "interactive choices: delete the dead file residue, keep for review, skip, or restore recorded arbiter residue after a prior cleanup"
        if arbiter_only
        else "interactive choices: delete the dead file residue, keep for review, or skip"
    )
    result.notes.append("configured batch policy: delete")
    result.notes.append(
        "proposed cleanup action; confirm no surviving file copy remains before execution"
        if not arbiter_only
        else "proposed arbiter cleanup; both data bricks were explicitly observed absent before execution"
    )
    result.notes.append("brick-side cleanup only; do not rely on mount-side recreation for a dead file ref")
    backups_enabled = backup_mode != "none"
    index = 1
    revert_index = 1

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "backend", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="backend",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["best-effort backup before deleting dead file residue"],
                    )
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-backend"),
                        step_type="backup_stale_backend",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort backup before deleting dead file residue"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-backend"),
                    step_type="remove_stale_backend",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=(
                        _ssh_rmr_preview(str(host), str(path))
                        if object_type == "directory"
                        else _ssh_rm_preview(str(host), str(path))
                    ),
                    notes=[
                        "remove backend directory residue tied to a confirmed dead file ref"
                        if object_type == "directory"
                        else "remove backend residue tied to a confirmed dead file ref"
                    ],
                )
            )
            index += 1
            if arbiter_only and backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-arbiter-backend"),
                        step_type="restore_backend_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=[
                            "best-effort fallback only; prefer volume snapshot rollback for the old arbiter metadata state",
                        ],
                    )
                )
                revert_index += 1

    for host, paths in sorted((action.get("stale_file_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "file-gfid", _basename(str(path)))
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="file_gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is best-effort before deleting dead file residue"],
                    )
                )
                result.estimated_unknown_backup_items += 1
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-file-gfid"),
                        step_type="backup_stale_file_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort backup before deleting dead file residue"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-file-gfid"),
                    step_type="remove_stale_file_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete dead file GFID residue"],
                )
            )
            index += 1
            if arbiter_only and backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-arbiter-file-gfid"),
                        step_type="restore_file_gfid_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=[
                            "best-effort fallback only; prefer volume snapshot rollback for the old arbiter metadata state",
                        ],
                    )
                )
                revert_index += 1

    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(action, backup_root, str(host), "gfid", _basename(str(path)))
            if arbiter_only and backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["best-effort backup before deleting arbiter-only GFID metadata"],
                    )
                )
                result.estimated_unknown_backup_items += 1
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-gfid"),
                        step_type="backup_stale_gfid",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["best-effort backup before deleting arbiter-only GFID metadata"],
                    )
                )
                index += 1
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-gfid"),
                    step_type="remove_stale_gfid",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rm_preview(str(host), str(path)),
                    notes=["delete dead file GFID metadata"],
                )
            )
            index += 1
            if arbiter_only and backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-arbiter-gfid"),
                        step_type="restore_gfid_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=[
                            "best-effort fallback only; prefer volume snapshot rollback for the old arbiter metadata state",
                        ],
                    )
                )
                revert_index += 1

    if not result.steps:
        result.status = "review"
        result.notes.append("no residue paths were observed in the manifest; keep for review")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "review-dead-file-ref-cleanup"),
                step_type="review_dead_file_ref_cleanup",
                notes=["no residue paths were observed in the manifest; recheck before deleting"],
            )
        )
    return result


def _plan_cleanup_stale_glusterfs_index(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type="cleanup_stale_glusterfs_index",
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="proposed",
        depends_on=list(action.get("depends_on") or []),
        notes=list(action.get("notes") or []),
    )
    result.notes.append("repair strategy: delete_stale_glusterfs_index_residue")
    result.notes.append("interactive choices: delete the stale Gluster heal index, keep for review, or skip")
    result.notes.append("configured batch policy: delete")
    result.notes.append("proposed cleanup action; this is internal Gluster bookkeeping, not primary user data")
    result.notes.append("no backup by design; a later crawl or client access can recreate the bookkeeping if it still matters")
    index = 1

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in _deepest_first(list(paths)):
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, "rm-stale-index"),
                    step_type="remove_stale_backend",
                    host=str(host),
                    target_path=str(path),
                    tolerate_missing=True,
                    command_preview=_ssh_rmr_preview(str(host), str(path)),
                    notes=[
                        "delete stale Gluster heal bookkeeping index entry",
                        "recurse one level into internal index residue",
                    ],
                )
            )
            index += 1

    if not result.steps:
        result.status = "review"
        result.notes.append("no stale index paths were observed in the manifest; keep for review")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "review-stale-index-cleanup"),
                step_type="review_stale_glusterfs_index_cleanup",
                notes=["no stale index paths were observed in the manifest; recheck before deleting"],
            )
        )
    return result
