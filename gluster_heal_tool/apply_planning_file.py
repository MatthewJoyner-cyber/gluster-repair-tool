# SPDX-License-Identifier: GPL-2.0-only
"""File-family planning helpers for Gluster repair apply results."""
from __future__ import annotations

from pathlib import Path
from posixpath import dirname

from .apply import DEFAULT_SPLIT_BRAIN_NATIVE_POLICY
from .apply_planning_utils import _action_with_decision_winner
from .apply_planning_utils import _add_revert_dir
from .apply_planning_utils import _backup_base
from .apply_planning_utils import _backup_target
from .apply_planning_utils import _basename
from .apply_planning_utils import _gluster_split_brain_preview
from .apply_planning_utils import _int_value
from .apply_planning_utils import _local_rm_preview
from .apply_planning_utils import _mkdir_preview
from .apply_planning_utils import _mount_mkdir_preview
from .apply_planning_utils import _mount_restore_preview
from .apply_planning_utils import _normalize_split_brain_native_policy
from .apply_planning_utils import _file_quarantine_target
from .apply_planning_utils import _select_split_brain_file_copy
from .apply_planning_utils import _restore_preview
from .apply_planning_utils import _ssh_cp_preview
from .apply_planning_utils import _ssh_chmod_numeric_preview
from .apply_planning_utils import _ssh_chown_numeric_preview
from .apply_planning_utils import _ssh_acl_reference_preview
from .apply_planning_utils import _ssh_mkdir_preview
from .apply_planning_utils import _ssh_mv_preview
from .apply_planning_utils import _ssh_rm_preview
from .apply_planning_utils import _ssh_relink_gfid_preview
from .apply_planning_utils import _ssh_setfattr_preview
from .apply_planning_utils import _stage_preview
from .apply_planning_utils import _step_id
from .decisions import build_split_brain_file_checksum_report
from .install_paths import DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from .models import ApplyActionResult, ApplyStep, BackupArtifact

def _plan_repair_file(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    volume: str = "",
    brick_path: str = "",
    split_brain_native_policy: str = DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
    native_split_brain_auto_followup: bool = True,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    ssh_user: str = DEFAULT_SERVICE_USER,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    winner_host = str(action.get("winner_host") or "")
    winner_backend = str(action.get("winner_backend") or "")
    stage_local_path = str(action.get("stage_local_path") or "")
    mounted_target = str(action.get("mounted_target") or "")
    winner_size = _int_value(action.get("winner_size"))
    repair_strategy = str(action.get("repair_strategy") or "replace_conflicting_file")
    restore_via_mount = bool(action.get("restore_via_mount", True))

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
        estimated_stage_bytes=winner_size,
        notes=list(action.get("notes") or []),
        native_heal_first=bool(action.get("native_heal_first")),
        native_heal_fallback_action=str(action.get("native_heal_fallback_action") or ""),
        native_heal_reason=str(action.get("native_heal_reason") or ""),
    )

    if not winner_host or not winner_backend or not stage_local_path or not mounted_target:
        result.status = "blocked"
        result.notes.append("repair_file action is missing winner or path fields")
        return result

    result.notes.append(f"repair strategy: {repair_strategy}")
    preserve_symlink = "file subtype: symlink" in result.notes or "orphaned_symlink_present" in result.notes
    preview_winner_host = winner_host
    preview_winner_backend = winner_backend
    if repair_strategy == "replace_entry_split_brain_file" and not (preview_winner_host and preview_winner_backend):
        preview_selected, _ = _select_split_brain_file_copy(action, "auto")
        if preview_selected:
            preview_winner_host = preview_winner_host or str(preview_selected.get("host") or "")
            preview_winner_backend = preview_winner_backend or str(preview_selected.get("backend") or "")
    if repair_strategy == "replace_entry_split_brain_file":
        result.notes.append(
            "split-brain resolution mode: official Gluster heal first, fallback to brick-side cleanup if needed"
        )
        native_policy = _normalize_split_brain_native_policy(split_brain_native_policy)
        result.notes.append(f"native split-brain policy: {native_policy}")
        checksum_report: dict[str, object] | None = None
        checksum_outcome = ""
        checksum_available = bool(volume and Path(worker_path).exists())
        if native_policy == "auto":
            result.notes.append(
                "native split-brain policy auto: latest-mtime on the official Gluster resolver; if that ties, checksum the candidate copies before any source-brick follow-up"
            )
            if checksum_available:
                checksum_report = build_split_brain_file_checksum_report(
                    action,
                    volume=volume,
                    worker_path=worker_path,
                    ssh_user=ssh_user,
                )
                checksum_outcome = str(checksum_report.get("outcome") or "")
                checksum_reason = str(checksum_report.get("reason") or "")
                if checksum_outcome == "content-equal":
                    result.notes.append(
                        "native split-brain policy auto checksum tie: candidate copies matched; source-brick follow-up remains safe"
                    )
                elif checksum_outcome == "content-different":
                    result.notes.append(
                        "native split-brain policy auto checksum tie: candidate copies differ; keep review-only or quarantine"
                    )
                    if checksum_reason:
                        result.notes.append(f"checksum evidence: {checksum_reason}")
                else:
                    result.notes.append(
                        "native split-brain policy auto checksum tie: checksum evidence unavailable; keep review-only or quarantine"
                    )
                    if checksum_reason:
                        result.notes.append(f"checksum evidence: {checksum_reason}")
            else:
                result.notes.append(
                    "native split-brain policy auto checksum tie: checksum worker unavailable in this environment; retaining legacy source-brick follow-up"
                )
        elif native_policy == "source-brick" and not (winner_host and brick_path):
            result.notes.append(
                "native split-brain policy source-brick requested, but no explicit source brick is known; native resolver step omitted"
            )

    stage_parent = dirname(stage_local_path)
    if repair_strategy == "replace_entry_split_brain_file":
        official_preview = _gluster_split_brain_preview(
            volume,
            logical_path,
            winner_host=preview_winner_host,
            brick_path=brick_path,
            native_policy=split_brain_native_policy,
        )
        if official_preview:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "resolve-split-brain-gluster-cli"),
                    step_type="resolve_split_brain_gluster_cli",
                    host=preview_winner_host or "",
                    source_path=preview_winner_backend,
                    target_path=mounted_target,
                    command_preview=official_preview,
                    notes=[
                        "try the Gluster-native split-brain resolver before any brick-side cleanup",
                        f"native split-brain policy: {_normalize_split_brain_native_policy(split_brain_native_policy)}",
                        "this resolver runs on the mounted Gluster path; lookup messages refer to the client-visible mount path, not the backend brick path",
                        "if the resolver reports no difference in mtime, treat it as an inconclusive tie and keep the fallback path active",
                        "if Gluster resolves the split-brain, skip the manual fallback steps",
                    ],
                )
            )
        if _normalize_split_brain_native_policy(split_brain_native_policy) == "auto" and native_split_brain_auto_followup:
            if checksum_available and checksum_outcome == "content-equal" and preview_winner_host and brick_path:
                source_brick_preview = _gluster_split_brain_preview(
                    volume,
                    logical_path,
                    winner_host=preview_winner_host,
                    brick_path=brick_path,
                    native_policy="source-brick",
                )
                if source_brick_preview:
                    result.steps.append(
                        ApplyStep(
                            step_id=_step_id(action_id, len(result.steps) + 1, "resolve-split-brain-gluster-source-brick"),
                            step_type="resolve_split_brain_gluster_cli",
                            host=preview_winner_host or "",
                            source_path=preview_winner_backend,
                            target_path=mounted_target,
                            command_preview=source_brick_preview,
                            notes=[
                                "native split-brain policy auto tie fallback: checksum matched the candidate copies; now try source-brick before manual fallback",
                                "native split-brain policy: source-brick",
                                "this resolver runs on the mounted Gluster path; lookup messages refer to the client-visible mount path, not the backend brick path",
                                "if Gluster resolves the split-brain, skip the manual fallback steps",
                            ],
                    )
                )
            elif checksum_available and checksum_outcome in {"content-different", "unavailable"}:
                result.action_type = "review_entry_split_brain"
                result.repair_strategy = "ambiguous_entry_split_brain_file"
                result.status = "review"
                result.notes.append(
                    "native split-brain policy auto tie fallback: checksum evidence did not prove the copies equivalent; keep review-only or quarantine"
                )
                if checksum_report:
                    checksum_reason = str(checksum_report.get("reason") or "")
                    if checksum_reason:
                        result.notes.append(f"checksum evidence: {checksum_reason}")
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, len(result.steps) + 1, "review-native-split-brain-checksum"),
                        step_type="review_ambiguous_entry_split_brain",
                        target_path=mounted_target or logical_path,
                        notes=[
                            "checksum tie-break did not prove the candidate copies equivalent",
                            "keep the file under review or quarantine it before choosing a keeper",
                        ],
                    )
                )
                return result
            elif not checksum_available and preview_winner_host and brick_path:
                source_brick_preview = _gluster_split_brain_preview(
                    volume,
                    logical_path,
                    winner_host=preview_winner_host,
                    brick_path=brick_path,
                    native_policy="source-brick",
                )
                if source_brick_preview:
                    result.steps.append(
                        ApplyStep(
                            step_id=_step_id(action_id, len(result.steps) + 1, "resolve-split-brain-gluster-source-brick"),
                            step_type="resolve_split_brain_gluster_cli",
                            host=preview_winner_host or "",
                            source_path=preview_winner_backend,
                            target_path=mounted_target,
                            command_preview=source_brick_preview,
                            notes=[
                                "native split-brain policy auto tie fallback: checksum worker unavailable here; keeping legacy source-brick follow-up",
                                "native split-brain policy: source-brick",
                                "this resolver runs on the mounted Gluster path; lookup messages refer to the client-visible mount path, not the backend brick path",
                                "if Gluster resolves the split-brain, skip the manual fallback steps",
                            ],
                        )
                    )
    if stage_parent:
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, len(result.steps) + 1, "mkdir-stage-parent"),
                step_type="mkdir_stage_parent",
                target_path=stage_parent,
                command_preview=_mkdir_preview(stage_parent),
                notes=["create the local staging directory before copying the winner"],
            )
        )
    result.steps.append(
        ApplyStep(
            step_id=_step_id(action_id, len(result.steps) + 1, "stage-winner"),
            step_type="stage_winner_local",
            host=winner_host,
            source_path=winner_backend,
            target_path=stage_local_path,
            command_preview=_stage_preview(
                winner_host,
                winner_backend,
                stage_local_path,
                preserve_symlink=preserve_symlink,
            ),
            notes=[
                "copy the winning backend file to a local staging path before cleanup",
                *(
                    ["preserve the symlink inode instead of dereferencing it"]
                    if preserve_symlink
                    else []
                ),
            ],
        )
    )
    if restore_via_mount:
        _add_revert_dir(result, mounted_target)
    mount_parent = dirname(mounted_target)
    if restore_via_mount and mount_parent:
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, len(result.steps) + 1, "mkdir-mount-parent"),
                step_type="mkdir_mount_parent",
                target_path=mount_parent,
                via_mount=True,
                command_preview=_mount_mkdir_preview(mount_parent),
                notes=["ensure the mounted parent directory exists before restoring the file"],
            )
        )

    index = len(result.steps) + 1
    revert_index = 1
    backups_enabled = backup_mode != "none"
    if not backups_enabled:
        result.notes.append("backup mode is none; revert coverage is reduced")
    elif backup_mode == "best-effort":
        result.notes.append("backup mode is best-effort; execution may continue if some backups fail")
    else:
        result.notes.append("backup mode is required; execution should refuse if backups cannot be created")
    if backup_root:
        result.notes.append(f"backup root override: {backup_root}")
    if repair_strategy in {"restore_missing_replica", "restore_missing_child_replica"}:
        if repair_strategy == "restore_missing_child_replica":
            result.notes.append("file-child-gap variant: target only missing child replicas and stale metadata remnants")
        else:
            result.notes.append("target only missing replicas and stale metadata remnants")
    elif repair_strategy == "replace_conflicting_file":
        if restore_via_mount:
            result.notes.append("remove conflicting replicas before restoring the winner through the mount")
        else:
            result.notes.append(
                "backup and remove only the conflicting data residue, then run or observe native Gluster heal before rebuilding evidence"
            )

    for host, paths in sorted((action.get("stale_backends_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(
                action,
                backup_root,
                str(host),
                "backend",
                _basename(str(path)),
            )
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="backend",
                        estimated_bytes=winner_size or None,
                        required_for_revert=backup_mode == "required",
                        notes=["best-effort backup before removing stale backend file"],
                    )
                )
                result.estimated_backup_bytes += winner_size
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, "backup-stale-backend"),
                        step_type="backup_stale_backend",
                        host=str(host),
                        source_path=str(path),
                        target_path=backup_path,
                        tolerate_missing=backup_mode != "required",
                        command_preview=_ssh_cp_preview(str(host), str(path), backup_path),
                        notes=["backup conflicting backend file before removal"],
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
                    notes=["remove conflicting backend file before restore-through-mount"],
                )
            )
            index += 1
            if backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-backend"),
                        step_type="restore_backend_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=["best-effort revert of the removed backend file"],
                    )
                )
                revert_index += 1

    for host, paths in sorted((action.get("stale_file_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(
                action,
                backup_root,
                str(host),
                "file-gfid",
                _basename(str(path)),
            )
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="file_gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is best-effort and may not be reusable as-is"],
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
                        notes=["best-effort capture of stale child file GFID path before removal"],
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
                    notes=["remove stale child file GFID before restore-through-mount"],
                )
            )
            index += 1
            if backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-file-gfid"),
                        step_type="restore_file_gfid_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=["best-effort only; exact original GFID state is not guaranteed"],
                    )
                )
                revert_index += 1

    for host, paths in sorted((action.get("stale_gfid_paths_by_host") or {}).items()):
        for path in paths:
            backup_path = _backup_target(
                action,
                backup_root,
                str(host),
                "gfid",
                _basename(str(path)),
            )
            if backups_enabled:
                result.backup_artifacts.append(
                    BackupArtifact(
                        host=str(host),
                        source_path=str(path),
                        backup_path=backup_path,
                        kind="gfid",
                        estimated_bytes=None,
                        required_for_revert=False,
                        notes=["GFID-path backup is informational and best-effort"],
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
                        notes=["best-effort capture of stale GFID metadata before removal"],
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
                    notes=["remove stale GFID metadata before restore-through-mount"],
                )
            )
            index += 1
            if backups_enabled:
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, "revert-gfid"),
                        step_type="restore_gfid_backup",
                        host=str(host),
                        source_path=backup_path,
                        target_path=str(path),
                        tolerate_missing=True,
                        command_preview=_ssh_cp_preview(str(host), backup_path, str(path)),
                        notes=["best-effort only; exact original GFID state is not guaranteed"],
                    )
                )
                revert_index += 1

    if repair_strategy == "replace_entry_split_brain_file" and mounted_target:
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "rm-restored-mount-file"),
                step_type="remove_restored_mount_file",
                target_path=mounted_target,
                tolerate_missing=True,
                via_mount=True,
                command_preview=_local_rm_preview(mounted_target),
                notes=[
                    "clear the logical file through the mount before recreating the chosen split-brain winner",
                ],
            )
        )
        index += 1

    if restore_via_mount:
        restore_step_type = (
            "restore_via_mount_child_gap"
            if repair_strategy == "restore_missing_child_replica"
            else "restore_via_mount"
        )
        restore_step_notes = [
            "restore once through the mounted Gluster path"
            if repair_strategy == "replace_conflicting_file"
            else "restore once through the mounted Gluster path to refill missing replicas",
            *(
                ["child-gap variant: remove stale child residue on the missing replica before recreating the file"]
                if repair_strategy == "restore_missing_child_replica"
                else []
            ),
            *(
                ["preserve the symlink inode instead of dereferencing it"]
                if preserve_symlink
                else []
            ),
        ]
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "restore-mount"),
                step_type=restore_step_type,
                source_path=stage_local_path,
                target_path=mounted_target,
                via_mount=True,
                command_preview=_mount_restore_preview(
                    stage_local_path,
                    mounted_target,
                    preserve_symlink=preserve_symlink,
                ),
                notes=restore_step_notes,
            )
        )
        result.revert_steps.append(
            ApplyStep(
                step_id=_step_id(action_id, revert_index, "remove-restored-file"),
                step_type="remove_restored_mount_file",
                target_path=mounted_target,
                tolerate_missing=True,
                via_mount=True,
                command_preview=_local_rm_preview(mounted_target),
                notes=["best-effort revert of the recreated logical file from the mounted volume"],
            )
        )
        revert_index += 1
    else:
        result.notes.append(
            "do not recreate the cleared conflicting data copy directly; let native Gluster heal reconstruct it, then rebuild evidence"
        )
        result.notes.append(
            "operator option pending: a direct brick-side rebuild requires an explicit decision card and separate live proof"
        )
    result.revert_steps.append(
        ApplyStep(
            step_id=_step_id(action_id, revert_index, "restore-staged-winner"),
            step_type="restore_stage_backup_locally",
            source_path=stage_local_path,
            target_path=f"{_backup_base(action, backup_root)}/winner/{_basename(stage_local_path)}",
            tolerate_missing=False,
            command_preview=_restore_preview(
                stage_local_path,
                f"{_backup_base(action, backup_root)}/winner/{_basename(stage_local_path)}",
                preserve_symlink=preserve_symlink,
            ),
            notes=["preserve the staged winner copy as local reference material for manual recovery"],
        )
    )
    if result.estimated_stage_bytes:
        result.notes.append(
            f"estimated local stage bytes: {result.estimated_stage_bytes}"
        )
    if backups_enabled:
        result.notes.append(
            f"estimated backup bytes: {result.estimated_backup_bytes}"
        )
        if result.estimated_unknown_backup_items:
            result.notes.append(
                f"backup size unknown for {result.estimated_unknown_backup_items} GFID-related artifacts"
            )
    result.notes.append("revert is best-effort and does not guarantee original GFID identity")
    return result


def _plan_repair_file_metadata(
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
    canonical_gfid = str(action.get("winner_file_gfid") or "")
    canonical_backend = str(action.get("winner_backend") or "")
    mismatch_hosts = [
        str(host)
        for host in action.get("file_metadata_mismatch_hosts") or []
        if str(host)
    ]
    backend_by_host = {
        str(host): str(path)
        for host, path in (action.get("file_metadata_backend_by_host") or {}).items()
        if str(host) and str(path)
    }
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
        native_heal_first=bool(action.get("native_heal_first")),
        native_heal_fallback_action=str(action.get("native_heal_fallback_action") or ""),
        native_heal_reason=str(action.get("native_heal_reason") or ""),
    )
    result.notes.append("repair strategy: attach_file_gfid")
    if backup_root:
        result.notes.append(f"backup root override: {backup_root}")
    result.notes.append("file exists everywhere; attach the canonical trusted.gfid to the mismatched bricks")
    if mismatch_hosts:
        result.notes.append(f"metadata mismatch hosts: {', '.join(mismatch_hosts)}")
    if arbiter_gfid_hosts:
        result.notes.append(
            "arbiter GFID recovery: rebuild the arbiter's own .glusterfs file handle after attaching its canonical identity"
        )
    if canonical_gfid:
        result.notes.append(f"canonical file GFID: {canonical_gfid}")
    if not canonical_gfid or not canonical_backend or not mismatch_hosts:
        result.status = "blocked"
        result.notes.append("file metadata repair is missing canonical identity or mismatch hosts")
        return result

    index = 1
    for host in mismatch_hosts:
        target_backend = backend_by_host.get(host, canonical_backend)
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, f"attach-file-metadata-{host}"),
                step_type="attach_file_gfid",
                host=host,
                source_path=canonical_gfid,
                target_path=target_backend,
                command_preview=_ssh_setfattr_preview(host, target_backend, canonical_gfid),
                notes=[
                    "reattach the canonical file GFID to the existing backend file",
                    "this repairs metadata drift without recreating the file",
                ],
            )
        )
        index += 1
        if host in arbiter_gfid_hosts:
            backend_root = arbiter_gfid_roots.get(host, "")
            if not backend_root:
                result.status = "blocked"
                result.steps = []
                result.notes.append(f"arbiter GFID repair is missing the exact backend root for {host}")
                return result
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, index, f"relink-arbiter-file-gfid-{host}"),
                    step_type="relink_arbiter_file_gfid",
                    host=host,
                    target_path=target_backend,
                    command_preview=_ssh_relink_gfid_preview(host, backend_root, target_backend, directory=False),
                    notes=[
                        "rebuild the missing .glusterfs hardlink for the existing arbiter file placeholder",
                        "no arbiter payload is read or used as a source",
                    ],
                )
            )
            index += 1
            stale_handle = arbiter_gfid_stale_paths.get(host, "")
            if stale_handle:
                stale_target = _file_quarantine_target(
                    stale_handle,
                    action_id=action_id,
                    host=host,
                    identity="stale-arbiter-gfid",
                    mode="stale_arbiter_gfid",
                )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"quarantine-stale-arbiter-file-gfid-{host}"),
                        step_type="quarantine_stale_arbiter_file_gfid",
                        host=host,
                        source_path=stale_handle,
                        target_path=stale_target,
                        command_preview=_ssh_mv_preview(host, stale_handle, stale_target),
                        notes=[
                            "move the old conflicting arbiter .glusterfs handle aside after relinking the canonical handle",
                            "the named quarantine target is the rollback artifact for the stale handle",
                        ],
                    )
                )
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, len(result.revert_steps) + 1, f"restore-stale-arbiter-file-gfid-{host}"),
                        step_type="restore_quarantined_stale_arbiter_file_gfid",
                        host=host,
                        source_path=stale_target,
                        target_path=stale_handle,
                        tolerate_missing=True,
                        command_preview=_ssh_mv_preview(host, stale_target, stale_handle),
                        notes=["best-effort revert for the stale arbiter handle quarantine"],
                    )
                )
                index += 1
    result.status = "proposed"
    return result


def _plan_repair_posix_metadata(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    volume: str = "",
    brick_path: str = "",
    ssh_user: str = DEFAULT_SERVICE_USER,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    source_host = str(action.get("metadata_source_host") or "")
    source_backend = str(action.get("metadata_source_backend") or "")
    mismatch_hosts = [
        str(host)
        for host in action.get("metadata_mismatch_hosts") or []
        if str(host)
    ]
    backend_by_host = {
        str(host): str(path)
        for host, path in (action.get("metadata_backend_by_host") or {}).items()
        if str(host) and str(path)
    }
    metadata_by_host = {
        str(host): dict(record)
        for host, record in (action.get("metadata_tuple_by_host") or {}).items()
        if str(host) and isinstance(record, dict)
    }
    source_metadata = metadata_by_host.get(source_host, {})
    source_mode = source_metadata.get("mode_bits")
    source_uid = source_metadata.get("uid")
    source_gid = source_metadata.get("gid")
    fields = [str(field) for field in action.get("metadata_fields_to_align") or ["mode", "uid", "gid"] if str(field)]
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
    result.brick_roles_by_host = {
        str(host): str(role)
        for host, role in (action.get("brick_roles_by_host") or {}).items()
        if str(host) and str(role)
    }
    result.brick_host_aliases = {
        str(host): [str(alias) for alias in aliases if str(alias)]
        for host, aliases in (action.get("brick_host_aliases") or {}).items()
        if str(host) and isinstance(aliases, list)
    }
    strategy = str(action.get("repair_strategy") or "align_posix_metadata_majority")
    result.notes.append(f"repair strategy: {strategy}")
    if backup_root:
        result.notes.append(f"backup root override: {backup_root}")
    result.metadata_source_host = source_host
    result.metadata_source_backend = source_backend
    result.metadata_source_reason = str(action.get("metadata_source_reason") or "")
    result.metadata_fields_to_align = fields
    if strategy == "resolve_posix_metadata_source_brick":
        result.notes.append(
            "POSIX metadata is a Gluster-visible split-brain; choose the source brick and let native source-brick resolve the row"
        )
    else:
        result.notes.append(
            "POSIX metadata is present on all replicas; align the minority bricks to the chosen source tuple"
        )
    if source_host:
        result.notes.append(f"metadata source host: {source_host}")
    if fields:
        result.notes.append(f"metadata fields to align: {', '.join(fields)}")
    if mismatch_hosts:
        result.notes.append(f"metadata mismatch hosts: {', '.join(mismatch_hosts)}")
    if not source_backend or not source_host or not mismatch_hosts:
        result.status = "blocked"
        result.notes.append("POSIX metadata repair is missing a source host/backend or mismatch hosts")
        return result
    if strategy == "resolve_posix_metadata_source_brick":
        if not brick_path:
            result.status = "blocked"
            result.notes.append("POSIX source-brick resolution is missing the common brick path")
            return result
        native_preview = _gluster_split_brain_preview(
            volume,
            logical_path,
            winner_host=source_host,
            brick_path=brick_path,
            native_policy="source-brick",
        )
        if not native_preview:
            result.status = "blocked"
            result.notes.append("POSIX source-brick resolution preview is unavailable")
            return result
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "resolve-posix-metadata-source-brick"),
                step_type="resolve_split_brain_gluster_cli",
                host=source_host,
                source_path=source_backend,
                target_path=logical_path,
                command_preview=native_preview,
                notes=[
                    "Gluster-visible POSIX split-brain: resolve the selected source brick natively after the operator chooses it",
                    "this runs on the mounted Gluster path; keep the direct backend alignment path disabled for this case",
                ],
            )
        )
        result.status = "proposed"
        return result

    step_index = 1
    for host in mismatch_hosts:
        target_backend = backend_by_host.get(host, "")
        if not target_backend:
            result.status = "blocked"
            result.notes.append(f"missing backend path for mismatch host {host}")
            return result
        if "uid" in fields or "gid" in fields:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, step_index, f"chown-posix-metadata-{host}"),
                    step_type="apply_posix_metadata_owner",
                    host=host,
                    source_path=source_backend,
                    target_path=target_backend,
                    command_preview=_ssh_chown_numeric_preview(host, int(source_uid), int(source_gid), target_backend, ssh_user=ssh_user),
                    notes=[
                        "align numeric uid/gid from the source brick to the mismatch brick",
                        "this keeps content intact and only updates POSIX ownership metadata",
                    ],
                )
            )
            step_index += 1
        if "mode" in fields:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, step_index, f"chmod-posix-metadata-{host}"),
                    step_type="apply_posix_metadata_mode",
                    host=host,
                    source_path=source_backend,
                    target_path=target_backend,
                    command_preview=_ssh_chmod_numeric_preview(host, int(source_mode), target_backend, ssh_user=ssh_user),
                    notes=[
                        "align numeric mode bits from the source brick to the mismatch brick",
                        "this keeps content intact and only updates POSIX permission metadata",
                    ],
                )
            )
            step_index += 1
        if "acl_access" in fields or "acl_default" in fields:
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, step_index, f"setfacl-posix-metadata-{host}"),
                    step_type="apply_posix_metadata_acl",
                    host=host,
                    source_host=source_host,
                    source_path=source_backend,
                    target_path=target_backend,
                    command_preview=_ssh_acl_reference_preview(
                        source_host,
                        source_backend,
                        host,
                        target_backend,
                        ssh_user=ssh_user,
                        clear_default="acl_default" in fields,
                    ),
                    notes=[
                        "copy ACL entries from the majority source brick to the mismatch brick",
                        "this keeps content intact and only updates POSIX ACL metadata",
                    ],
                )
            )
            step_index += 1
    result.status = "proposed"
    return result
