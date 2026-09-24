# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Reporting and output helpers for apply planning results."""
from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

from .canary_shared import _format_brick_roles_by_host_for_notes
from .models import ApplyActionResult
from .shared_io import write_json_shared

DEFAULT_REVIEW_POLICIES = {
    "entry_split_brain_file": "auto",
    "probable_stale_survivor_file": "delete",
    "probable_orphaned_symlink_file": "delete",
    "file_metadata_only": "review",
    "posix_metadata_no_majority": "review",
    "entry_split_brain_directory": "review",
    "probable_stale_survivor_directory": "auto",
    "directory_gfid_conflict": "review",
    "directory_metadata": "review",
    "directory_children": "auto",
}


def _result_family(result: ApplyActionResult) -> str:
    if result.action_type in {
        "repair_file",
        "repair_file_metadata",
        "review_entry_split_brain",
        "review_probable_stale_survivor",
        "review_probable_orphaned_symlink",
        "cleanup_orphaned_symlink",
        "cleanup_dead_file_refs",
    }:
        return "files"
    if result.action_type in {"reconcile_directory", "repair_directory_metadata", "review_directory_children", "review_directory_gfid_conflict", "review_directory_metadata"}:
        return "directories"
    if result.action_type in {"repair_posix_metadata", "review_file_metadata", "review_posix_metadata_no_majority", "review_type_mismatch", "defer_dead_gfid_cleanup", "review_dead_gfid_reference", "cleanup_dead_gfid", "cleanup_stale_glusterfs_index"}:
        return "metadata"
    return "other"


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0

def write_apply_results(
    path: str | Path,
    results: list[ApplyActionResult],
    *,
    decision_file: str = "",
    controller_cycle: dict[str, object] | None = None,
    plan_in: str | Path | None = None,
    plan_payload: dict | None = None,
    previous_payload: dict | None = None,
) -> None:
    backup_root = results[0].backup_root if results else ""
    backup_mode = results[0].backup_mode if results else "required"
    batch = results[0].batch if results else False
    total_stage_bytes = sum(item.estimated_stage_bytes for item in results)
    total_backup_bytes = sum(item.estimated_backup_bytes for item in results)
    total_unknown_backup_items = sum(item.estimated_unknown_backup_items for item in results)
    warnings = [
        "dry-run output only; no filesystem changes have been executed",
        "revert is best-effort and does not guarantee original GFID identity",
    ]
    if backup_mode == "required":
        warnings.append(
            "check available free space before execution; backup mode required may refuse to run"
        )
    elif backup_mode == "best-effort":
        warnings.append(
            "backup mode best-effort may continue when some backups fail"
        )
    else:
        warnings.append(
            "backup mode none disables most revert coverage"
        )
    if total_unknown_backup_items:
        warnings.append(
            f"backup size estimate excludes {total_unknown_backup_items} GFID-related artifacts with unknown size"
        )
    payload = {
        "schema_version": 1,
        "execution_mode": results[0].execution_mode if results else "dry-run",
        "backup_root": backup_root,
        "backup_mode": backup_mode,
        "batch": batch,
        "decision_file": decision_file,
        "controller_cycle": controller_cycle or {},
        "estimates": {
            "total_stage_bytes": total_stage_bytes,
            "total_backup_bytes": total_backup_bytes,
            "total_unknown_backup_items": total_unknown_backup_items,
        },
        "warnings": warnings,
        "actions": [result.to_dict() for result in results],
    }
    from .apply_binding import bind_apply, derive_apply_binding
    if plan_in is not None and previous_payload is not None:
        raise ValueError("choose a plan or a continuation parent, not both")
    if plan_in is not None:
        if plan_payload is None:
            raise ValueError("plan_payload is required with plan_in")
        bind_apply(payload, plan_payload, plan_in)
    elif previous_payload is not None:
        derive_apply_binding(payload, previous_payload)
    if "origin_binding" not in payload:
        warnings.append("unbound artifact: preview only; rebuild evidence, plan and apply before execution")
    write_json_shared(path, payload)


def _disk_report(path: str) -> dict[str, object]:
    requested = Path(path)
    usage_path = requested
    while not usage_path.exists() and usage_path.parent != usage_path:
        usage_path = usage_path.parent
    usage = shutil.disk_usage(usage_path)
    return {
        "path": str(requested),
        "disk_usage_path": str(usage_path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def build_preflight_report(apply_payload: dict[str, object]) -> dict[str, object]:
    require_snapshot = bool(apply_payload.get("require_snapshot"))
    snapshot_ack = bool(apply_payload.get("snapshot_ack"))
    estimates = apply_payload.get("estimates") or {}
    stage_bytes = _int_value(estimates.get("total_stage_bytes"))
    backup_bytes = _int_value(estimates.get("total_backup_bytes"))
    unknown_backup_items = _int_value(estimates.get("total_unknown_backup_items"))
    backup_root = str(apply_payload.get("backup_root") or "")
    backup_mode = str(apply_payload.get("backup_mode") or "required")

    stage_base = "/tmp"
    stage_report = _disk_report(stage_base)
    backup_base = backup_root or stage_base
    backup_report = _disk_report(backup_base)

    warnings: list[str] = []
    if stage_bytes > stage_report["free_bytes"]:
        warnings.append("insufficient free space for staged winner copies")
    if backup_mode != "none" and backup_bytes > backup_report["free_bytes"]:
        warnings.append("insufficient free space for estimated backups")
    if unknown_backup_items:
        warnings.append(
            f"backup estimate excludes {unknown_backup_items} GFID-related artifacts with unknown size"
        )
    if require_snapshot and not snapshot_ack:
        warnings.append(
            "snapshot confirmation is required before execute; take a Gluster snapshot or equivalent rollback point, then pass --snapshot-ack"
        )

    return {
        "schema_version": 1,
        "backup_mode": backup_mode,
        "backup_root": backup_root,
        "reversibility": {
            "snapshot_required": require_snapshot,
            "snapshot_acknowledged": snapshot_ack,
        },
        "stage_storage": {
            "base_path": stage_base,
            "estimated_bytes": stage_bytes,
            "disk": stage_report,
            "fits_estimate": stage_bytes <= stage_report["free_bytes"],
        },
        "backup_storage": {
            "base_path": backup_base,
            "estimated_bytes": backup_bytes,
            "unknown_items": unknown_backup_items,
            "disk": backup_report,
            "fits_estimate": backup_bytes <= backup_report["free_bytes"],
        },
        "warnings": warnings,
    }


def write_preflight_report(path: str | Path, report: dict[str, object]) -> None:
    write_json_shared(path, report)


def collect_temp_mount_verification_paths(results: list[ApplyActionResult]) -> list[str]:
    def _normalize_verification_path(logical_path: str) -> str:
        normalized = str(logical_path or "").strip()
        if not normalized:
            return ""
        marker = "/repair-canary/"
        if marker in normalized:
            tail = normalized.split(marker, 1)[1]
            pieces = tail.split("/", 1)
            if len(pieces) == 2:
                return pieces[1].lstrip("/")
            return ""
        return normalized

    verification_action_types = {
        "repair_file",
        "repair_file_metadata",
        "repair_posix_metadata",
        "repair_directory_metadata",
        "reconcile_directory",
        "review_entry_split_brain",
        "clear_split_brain_marker",
    }
    paths: list[str] = []
    seen: set[str] = set()
    for result in results:
        if result.status not in {"completed", "completed-with-skips"}:
            continue
        if result.action_type not in verification_action_types:
            continue
        logical_path = _normalize_verification_path(result.logical_path)
        if not logical_path or logical_path in seen:
            continue
        seen.add(logical_path)
        paths.append(logical_path)
    return paths


def needs_acl_temp_mount(results: list[ApplyActionResult]) -> bool:
    return any(
        result.action_type == "repair_posix_metadata"
        and any(step.step_type == "apply_posix_metadata_acl" for step in result.steps)
        for result in results
    )


def summarize_apply_results(
    results: list[ApplyActionResult],
    *,
    controller_cycle: dict[str, object] | None = None,
) -> dict[str, object]:
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
    summary = {
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
        "posix_metadata_repairs": action_types.get("repair_posix_metadata", 0),
        "orphaned_symlink_cleanups": action_types.get("cleanup_orphaned_symlink", 0),
        "directory_repairs": (
            action_types.get("reconcile_directory", 0)
            + action_types.get("repair_directory_metadata", 0)
        ),
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
        "posix_metadata_reviews": review_only_types.get("review_posix_metadata_no_majority", 0),
        "dead_gfid_reference_reviews": review_only_types.get("review_dead_gfid_reference", 0),
        "dead_gfid_cleanup_actions": action_types.get("cleanup_dead_gfid", 0),
        "orphaned_symlink_cleanup_actions": action_types.get("cleanup_orphaned_symlink", 0),
    }
    if summary["stale_glusterfs_index_cleanup_actions"]:
        summary["follow_up_action"] = "refresh-heal-snapshot"
        summary["follow_up_detail"] = "rerun heal info, then rebuild manifest-build and plan-build from fresh evidence"
    controller_cycle = controller_cycle or {}
    if controller_cycle:
        summary["controller_next_action"] = str(controller_cycle.get("controller_next_action") or "")
        summary["controller_cycle_report"] = str(controller_cycle.get("controller_cycle_report") or "")
    return summary


def render_apply_summary(summary: dict[str, object]) -> str:
    parts = [
        "APPLY",
        "-----",
        f"Apply ready: {summary.get('ready_repairs', 0)} ready repairs",
        f"{summary.get('file_repairs', 0)} files",
        f"{summary.get('directory_repairs', 0)} directories",
    ]
    if summary.get("file_metadata_repairs", 0):
        parts.append(f"{summary.get('file_metadata_repairs', 0)} file metadata repairs")
    if summary.get("posix_metadata_repairs", 0):
        parts.append(f"{summary.get('posix_metadata_repairs', 0)} POSIX metadata repairs")
    if summary.get("dead_file_ref_cleanup_actions", 0):
        parts.append(f"{summary.get('dead_file_ref_cleanup_actions', 0)} dead-file-ref cleanups")
    if summary.get("stale_glusterfs_index_cleanup_actions", 0):
        parts.append(
            f"{summary.get('stale_glusterfs_index_cleanup_actions', 0)} stale .glusterfs index cleanups"
        )
    if summary.get("orphaned_symlink_cleanups", 0):
        parts.append(f"{summary.get('orphaned_symlink_cleanups', 0)} orphaned symlink cleanups")
    if summary.get("directory_metadata_repairs", 0):
        parts.append(f"{summary.get('directory_metadata_repairs', 0)} directory metadata repairs")
    if summary.get("dead_gfid_cleanup_actions", 0):
        parts.append(f"{summary.get('dead_gfid_cleanup_actions', 0)} dead-GFID cleanups")
    split_brain_actionable = int(summary.get("entry_split_brain_actionable", 0))
    split_brain_review_only = int(summary.get("entry_split_brain_review_only", 0))
    split_brain_auto_majority = int(summary.get("entry_split_brain_auto_majority", 0))
    split_brain_auto_mtime = int(summary.get("entry_split_brain_auto_mtime", 0))
    if split_brain_actionable or split_brain_review_only:
        if split_brain_auto_majority or split_brain_auto_mtime:
            parts.append(
                "split-brain: "
                f"{split_brain_actionable} actionable under auto "
                f"({split_brain_auto_majority} majority winners, {split_brain_auto_mtime} mtime tie)"
            )
        else:
            parts.append(
                "split-brain: "
                f"{split_brain_actionable} actionable, {split_brain_review_only} review-only"
            )
    review_total = int(summary.get("review_items", 0))
    if review_total:
        review_parts = [
            ("split-brain", summary.get("entry_split_brain_review_only", 0)),
            ("stale-survivor", summary.get("probable_stale_survivor_reviews", 0)),
            ("orphaned-symlink", summary.get("probable_orphaned_symlink_reviews", 0)),
            ("directory-child-gap", summary.get("directory_child_gap_reviews", 0)),
            ("dir-conflicts", summary.get("directory_gfid_conflict_reviews", 0)),
            ("dir-metadata", summary.get("directory_metadata_reviews", 0)),
            ("file-metadata", summary.get("file_metadata_reviews", 0)),
            ("posix-metadata", summary.get("posix_metadata_reviews", 0)),
            ("dead-gfid-ref", summary.get("dead_gfid_reference_reviews", 0)),
            ("dead-gfid-cleanup", summary.get("dead_gfid_cleanup_actions", 0)),
        ]
        known_review_total = sum(int(count) for _, count in review_parts)
        if review_total > known_review_total:
            review_parts.append(("other", review_total - known_review_total))
        review_breakdown = ", ".join(f"{label}={count}" for label, count in review_parts if count)
        if review_breakdown:
            parts.append(f"{review_total} review-only items ({review_breakdown})")
        else:
            parts.append(f"{review_total} review-only items")
    completed_with_skips = int(summary.get("completed_with_skips", 0))
    review_only_skipped_steps = int(summary.get("review_only_skipped_steps", 0))
    skipped_steps = int(summary.get("skipped_steps", 0))
    if completed_with_skips:
        if review_only_skipped_steps:
            parts.append(
                f"repair completed; review-only tail skipped ({review_only_skipped_steps} step"
                f"{'' if review_only_skipped_steps == 1 else 's'})"
            )
        elif skipped_steps:
            parts.append(
                f"repair completed with skips ({skipped_steps} non-blocking step"
                f"{'' if skipped_steps == 1 else 's'} skipped)"
            )
        else:
            parts.append("repair completed with skips")
    elif int(summary.get("completed_actions", 0)):
        parts.append("repair completed")
    strategies = summary.get("repair_strategies") or {}
    if strategies:
        parts.append(
            "strategies: "
            + ", ".join(f"{key}={value}" for key, value in sorted(strategies.items()))
        )
    controller_next_action = str(summary.get("controller_next_action") or "")
    if controller_next_action and controller_next_action != "continue":
        parts.append(f"controller-cycle: next_action={controller_next_action}")
    follow_up_action = str(summary.get("follow_up_action") or "")
    if follow_up_action:
        detail = str(summary.get("follow_up_detail") or "").strip()
        parts.append(
            f"follow-up: {follow_up_action}"
            + (f" ({detail})" if detail else "")
        )
    return "\n".join(parts)


def filter_apply_results(
    results: list[ApplyActionResult],
    *,
    action_type: str | None = None,
    path_contains: str | None = None,
    strategy: str | None = None,
    limit: int | None = None,
    include_dependencies: bool = False,
    scope: str | None = None,
) -> list[ApplyActionResult]:
    all_by_id = {item.action_id: item for item in results}
    filtered = results
    if action_type:
        filtered = [item for item in filtered if item.action_type == action_type]
    if path_contains:
        filtered = [item for item in filtered if path_contains in item.logical_path]
    if strategy:
        filtered = [
            item
            for item in filtered
            if any(note == f"repair strategy: {strategy}" for note in item.notes)
        ]
    if scope and scope != "all":
        allowed = {part.strip() for part in scope.split(",") if part.strip()}
        filtered = [item for item in filtered if _result_family(item) in allowed]
    if limit is not None and limit >= 0:
        filtered = filtered[:limit]
    if include_dependencies:
        wanted_ids = {item.action_id for item in filtered}
        queue = [item.action_id for item in filtered]
        while queue:
            action_id = queue.pop(0)
            current = all_by_id.get(action_id)
            if not current:
                continue
            for dep in current.depends_on:
                if dep not in wanted_ids and dep in all_by_id:
                    wanted_ids.add(dep)
                    queue.append(dep)
        filtered = [item for item in results if item.action_id in wanted_ids]
    return filtered


def render_apply_run(
    results: list[ApplyActionResult],
    *,
    summary_only: bool = False,
    controller_cycle: dict[str, object] | None = None,
) -> str:
    lines: list[str] = []
    summary = summarize_apply_results(results)
    lines.append(render_apply_summary(summary))
    if controller_cycle:
        next_action = str(controller_cycle.get("controller_next_action") or "").strip()
        if next_action:
            lines.append(f"controller-cycle: next_action={next_action}")
    if summary_only:
        return "\n".join(lines).rstrip() + "\n"
    lines.append("")
    for result in results:
        lines.append(
            f"[{result.action_type}] {result.logical_path}"
        )
        lines.append(f"original path: {result.logical_path}")
        if result.action_type in {"cleanup_dead_gfid", "cleanup_dead_file_refs", "cleanup_stale_glusterfs_index"}:
            lines.append(f"status: {result.status}")
        elif result.status == "review":
            lines.append("status: review")
        strategy = strategy_for_result(result)
        if strategy:
            lines.append(f"strategy: {strategy}")
        if result.action_type == "repair_file_metadata" and getattr(result, "native_heal_first", False):
            suggestion, why, choice = _review_matrix_hint(result)
            lines.append(f"matrix suggestion: {suggestion}")
            lines.append(f"why: {why}")
            lines.append(choice)
        elif result.status == "review" or result.action_type in {"cleanup_dead_gfid", "cleanup_dead_file_refs", "cleanup_stale_glusterfs_index"}:
            suggestion, why, choice = _review_matrix_hint(result)
            lines.append(f"matrix suggestion: {suggestion}")
            lines.append(f"why: {why}")
            lines.append(choice)
            if result.status == "review":
                for note in _review_evidence_notes(result):
                    lines.append(f"evidence: {note}")
                for note in _review_support_notes(result):
                    lines.append(f"support: {note}")
            elif result.action_type in {"cleanup_dead_gfid", "cleanup_dead_file_refs", "cleanup_stale_glusterfs_index"}:
                roles_note = _format_brick_roles_by_host_for_notes(getattr(result, "brick_roles_by_host", {}))
                if roles_note:
                    for note in roles_note:
                        lines.append(f"note: {note}")
        else:
            important_prefixes = (
                "decision file entry loaded:",
                "repair strategy:",
                "configured batch policy:",
                "batch default:",
                "interactive choices:",
                "preferred choices:",
                "interactive default suggestion:",
                "directory tie decision:",
                "directory tie recommended choice:",
                "quarantine mode:",
                "quarantine targets:",
                "quarantine naming:",
                "quarantine canonical host:",
                "quarantine canonical backend:",
                "quarantine canonical gfid:",
                "quarantine is reversible",
                "split-brain resolution mode:",
                "native split-brain policy:",
                "native split-brain outcome:",
                "file subtype:",
                "directory tie preview:",
                "directory tie prompt:",
                "directory depth cap reached:",
                "post-repair verification:",
                "internal Gluster heal bookkeeping entry",
                "no backup by design",
                "metadata mismatch hosts:",
                "canonical file GFID:",
                "likely winner host:",
                "likely winner backend:",
                "losing/conflicting hosts:",
                "present hosts:",
                "missing hosts:",
                "mounted target:",
            )
            for note in result.notes:
                if note.startswith(important_prefixes):
                    lines.append(f"note: {note}")
            if any(note.startswith("quarantine targets:") for note in result.notes):
                lines.append(
                    "note: quarantine naming: original path + .gluster-quarantine.<mode>.<action-id>.<host>.<identity-prefix>"
                )
            canonical_host = next((note.removeprefix("quarantine canonical host: ").strip() for note in result.notes if note.startswith("quarantine canonical host: ")), "")
            canonical_backend = next((note.removeprefix("quarantine canonical backend: ").strip() for note in result.notes if note.startswith("quarantine canonical backend: ")), "")
            canonical_gfid = next((note.removeprefix("quarantine canonical gfid: ").strip() for note in result.notes if note.startswith("quarantine canonical gfid: ")), "")
            if canonical_host or canonical_backend or canonical_gfid:
                holder_bits = [bit for bit in [
                    canonical_host and f"host={canonical_host}",
                    canonical_backend and f"backend={canonical_backend}",
                    canonical_gfid and f"gfid={canonical_gfid}",
                ] if bit]
                lines.append("note: original GFID holder: " + ", ".join(holder_bits))
            roles_note = _format_brick_roles_by_host_for_notes(getattr(result, "brick_roles_by_host", {}))
            if roles_note:
                for note in roles_note:
                    lines.append(f"note: {note}")
        for step in result.steps:
            cmd = " ".join(step.command_preview) if step.command_preview else "(review step)"
            lines.append(f"- {step.step_type}: {cmd}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def strategy_for_result(result: ApplyActionResult) -> str:
    for note in result.notes:
        if note.startswith("repair strategy: "):
            return note.removeprefix("repair strategy: ")
    return ""


def is_ready_to_execute(result: ApplyActionResult) -> bool:
    strategy = strategy_for_result(result)
    if result.action_type == "repair_file":
        return strategy in {"restore_missing_replica", "restore_missing_child_replica", "replace_conflicting_file"} and result.status in {
            "planned",
            "proposed",
        }
    if result.action_type == "repair_file_metadata":
        return strategy == "attach_file_gfid" and result.status in {"planned", "proposed"}
    if result.action_type == "repair_posix_metadata":
        return strategy in {"align_posix_metadata_majority", "align_posix_metadata_selected_source", "resolve_posix_metadata_source_brick"} and result.status in {"planned", "proposed"}
    if result.action_type == "clear_split_brain_marker":
        return result.status in {"planned", "proposed"} and [step.step_type for step in result.steps] == [
            "resolve_split_brain_gluster_cli"
        ]
    if result.action_type == "repair_directory_metadata":
        return strategy in {"attach_directory_gfid", "attach_directory_mdata"} and result.status in {"planned", "proposed"}
    if result.action_type == "review_entry_split_brain":
        return strategy in {"replace_entry_split_brain_file", "quarantine_conflicting_file"} and result.status in {
            "planned",
            "proposed",
        }
    if result.action_type == "review_directory_gfid_conflict":
        return strategy == "quarantine_conflicting_directory" and result.status in {"planned", "proposed"}
    if result.action_type == "cleanup_orphaned_symlink":
        return strategy == "delete_orphaned_symlink_residue" and result.status in {"planned", "proposed"}
    if result.action_type == "cleanup_dead_file_refs":
        return strategy == "delete_dead_file_ref_residue" and result.status in {"planned", "proposed"}
    if result.action_type == "cleanup_stale_glusterfs_index":
        return strategy == "delete_stale_glusterfs_index_residue" and result.status in {"planned", "proposed"}
    if result.action_type == "reconcile_directory":
        return strategy in {
            "restore_missing_directory",
            "recreate_missing_directory_backend",
            "recreate_missing_directory_backend_child_gap",
            "reconcile_directory_children",
        } and result.status in {
            "planned",
            "proposed",
        }
    if result.action_type == "cleanup_dead_gfid":
        return strategy == "delete_dead_gfid_residue" and result.status in {"planned", "proposed"}
    return False


def filter_execute_ready_results(results: list[ApplyActionResult]) -> tuple[list[ApplyActionResult], list[ApplyActionResult]]:
    ready_ids = {result.action_id for result in results if is_ready_to_execute(result)}
    all_by_id = {result.action_id: result for result in results}
    changed = True
    while changed:
        changed = False
        for action_id in list(ready_ids):
            current = all_by_id[action_id]
            if any(dep not in ready_ids for dep in current.depends_on):
                ready_ids.remove(action_id)
                changed = True
    ready = [result for result in results if result.action_id in ready_ids]
    skipped = [result for result in results if result.action_id not in ready_ids]
    return ready, skipped


def _review_matrix_hint(result: ApplyActionResult) -> tuple[str, str, str]:
    def _child_paths_sentence() -> str:
        child_paths = sorted(
            dep.removeprefix("repair:")
            for dep in result.depends_on
            if dep.startswith("repair:")
        )
        if not child_paths:
            return ""
        if len(child_paths) == 1:
            return f" Child dependency: {child_paths[0]}."
        if len(child_paths) == 2:
            return f" Child dependencies: {child_paths[0]}, {child_paths[1]}."
        return f" Child dependencies: {', '.join(child_paths[:-1])}, and {child_paths[-1]}."

    strategy = strategy_for_result(result)
    planner_choice = (
        str(getattr(result, "recommended_choice", "") or "")
        .strip()
        .replace("-", "_")
        .replace(" ", "_")
        .lower()
    )

    if result.action_type == "repair_file_metadata":
        if getattr(result, "native_heal_first", False):
            fallback = str(getattr(result, "native_heal_fallback_action", "") or "repair_file_metadata")
            reason = str(
                getattr(result, "native_heal_reason", "")
                or "The file already exists on all replicas and only metadata differs; try Gluster heal/rescan first."
            )
            return (
                "run Gluster heal/rescan first",
                reason + f" If the drift remains after rescan, rerun and promote to {fallback}.",
                "choice: run Gluster heal/rescan, apply metadata repair, keep review, or skip",
            )
        return (
            "repair_file_metadata",
            "The file is present everywhere, but one or more bricks are missing the canonical trusted.gfid xattr; attach the canonical GFID to the mismatched bricks.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "repair_posix_metadata":
        source_host = str(getattr(result, "metadata_source_host", "") or "").strip()
        source_reason = str(getattr(result, "metadata_source_reason", "") or "").strip()
        fields = ", ".join(getattr(result, "metadata_fields_to_align", []) or ["mode", "uid", "gid"])
        if source_reason == "native_gluster_source":
            if source_host:
                return (
                    "resolve_posix_metadata_source_brick",
                    f"POSIX metadata differs across replicas; choose the source brick from {source_host} for {fields}, then let Gluster resolve the visible split-brain row natively.",
                    "choice: apply suggestion, keep review, or skip",
                )
            return (
                "resolve_posix_metadata_source_brick",
                f"POSIX metadata differs across replicas; choose the source brick for {fields}, then let Gluster resolve the visible split-brain row natively.",
                "choice: apply suggestion, keep review, or skip",
            )
        if source_host:
            return (
                "repair_posix_metadata",
                f"POSIX metadata differs across replicas; align the minority bricks to the source tuple from {source_host} for {fields}.",
                "choice: apply suggestion, keep review, or skip",
            )
        return (
            "repair_posix_metadata",
            f"POSIX metadata differs across replicas; align the minority bricks to the chosen source tuple for {fields}.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "review_posix_metadata_no_majority":
        source_reason = str(getattr(result, "metadata_source_reason", "") or "").strip()
        if source_reason == "native_gluster_source":
            return (
                "choose_posix_metadata_source",
                "POSIX mode, uid, gid, or ACL values differ without a strict majority; choose a data-brick source explicitly, then use native source-brick after selection.",
                "choice: choose source brick, keep review, or skip",
            )
        return (
            "choose_posix_metadata_source",
            "POSIX mode, uid, gid, or ACL values differ without a strict majority; inspect every data brick and choose an explicit source tuple.",
            "choice: choose source, keep review, or skip",
        )

    if result.action_type == "review_entry_split_brain":
        if strategy == "ambiguous_entry_split_brain_file":
            likely_winner_host = ""
            likely_winner_backend = ""
            for note in result.notes:
                if note.startswith("heuristic winner host: "):
                    likely_winner_host = note.removeprefix("heuristic winner host: ").strip()
                if note.startswith("heuristic winner backend: "):
                    likely_winner_backend = note.removeprefix("heuristic winner backend: ").strip()
            why = "The file cohorts are tied; use a decision file to confirm the likely winner before applying."
            if likely_winner_host:
                why = (
                    f"The file cohorts are tied; the likely winner is {likely_winner_host}"
                    + (f" ({likely_winner_backend})" if likely_winner_backend else "")
                    + "; confirm that keeper in a decision file before applying."
                )
            return (
                "quarantine_both",
                why
                + " Preserve both histories first with quarantine_both; a later decision file may select a keeper.",
                "choice: quarantine_both, supply a decision file with the likely winner, keep review, or skip",
            )
        if strategy == "quarantine_conflicting_file":
            return (
                "quarantine_conflicting_file",
                "The file histories are being preserved instead of resolved; quarantine moves the losing copy aside so the operator can inspect both branches, and the follow-up gap fill can pull the winner directly from the good brick when the mount is unavailable.",
                "choice: apply suggestion, keep review, or skip",
            )
        if strategy == "review_entry_split_brain_directory":
            return (
                "quarantine_both",
                "Directory GFIDs disagree for the same name; preserve both trees first with quarantine_both, then run directory-tie-build to compare bounded trees before choosing merge, prune, or a source.",
                "choice: quarantine_both, run directory-tie-build, keep review, or skip",
            )
        return (
            "replace_entry_split_brain_file",
            "A clear winning file cohort is present; try the official Gluster split-brain resolve first, then clean the losing backend and file-GFID remnants and restore through the mount if needed. Warning: this is a destructive branch if the winner selection is wrong, so keep the policy explicit.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "review_probable_stale_survivor":
        if strategy == "restore_below_quorum_file_from_brick":
            return (
                "brick-side rsync salvage",
                "Below quorum should stay review-first by default; when the operator explicitly opts in, salvage can pull the file directly from the chosen healthy brick with rsync instead of trying to write through the mount.",
                "choice: salvage, keep review, or skip",
            )
        if strategy == "delete_below_quorum_subtree":
            return (
                "delete_below_quorum_subtree",
                "Below quorum usually means a stale subtree survivor; remove it deepest-first and do not recreate. Warning: this is a delete-by-default branch, so only use it when the missing replicas are truly intended to be gone.",
                "choice: apply suggestion, keep review, or skip",
            )
        return (
            "delete_below_quorum_file",
            "Below quorum usually means a stale file survivor or orphan; delete the leftover backend and GFID remnants, then stop. Warning: this is a delete-by-default branch, so do not use it when the file should still exist.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "review_probable_orphaned_symlink":
        return (
            "delete_orphaned_symlink_residue",
            "Below quorum usually means an orphaned symlink residue; delete the leftover symlink backend and GFID remnants, then stop. Warning: this is a delete-by-default branch, so keep the target path outside Gluster in mind before confirming.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "review_directory_gfid_conflict":
        choice = planner_choice if planner_choice in {"quarantine_loser", "quarantine_both"} else "quarantine_both"
        if strategy == "ambiguous_directory_presence_tie":
            choice = "quarantine_both"
            reason = (
                "Exactly half of the replicas carry the directory, so neither restore nor subtree deletion has strict-majority authority; "
                "quarantine every visible tree before an explicit restore-or-delete decision."
            )
            choices = "choice: quarantine_both, keep review, or skip; then explicitly restore or confirm deletion"
        elif choice == "quarantine_loser":
            reason = (
                "A canonical directory side is recorded; quarantine the losing tree first, then run directory-tie-build "
                "before any merge or source decision."
            )
        else:
            reason = (
                "The directory winner is not proven; quarantine both trees first, then run directory-tie-build "
                "before any merge, prune, or source decision."
            )
        return (
            choice,
            reason,
            choices
            if strategy == "ambiguous_directory_presence_tie"
            else "choice: quarantine_loser, quarantine_both, run directory-tie-build, keep review, or skip",
        )

    if result.action_type == "review_directory_children":
        recommended_reason = str(getattr(result, "recommended_reason", "") or "").strip()
        if planner_choice == "recover_missing_child_or_confirm_delete":
            return (
                planner_choice,
                recommended_reason
                or "No child copy survives; recover from an authoritative backup or explicitly confirm deletion without touching the parent GFID handle.",
                "choice: recover from backup, confirm deletion, keep review, or skip",
            )
        if planner_choice == "collect_gfid_child_evidence":
            return (
                planner_choice,
                recommended_reason
                or "The missing child's identity and intent are not proven; collect immediate GFID-child evidence with mount probing off.",
                "choice: collect GFID-child evidence, keep review, or skip",
            )
        return (
            "reconcile_directory_children",
            recommended_reason
            or "Immediate children differ across bricks; repair the child set first, then rerun manifest-build and plan-build so the parent can be reconsidered."
            + _child_paths_sentence(),
            "choice: keep review, apply suggestion, or skip",
        )

    if result.action_type == "review_directory_metadata" and getattr(result, "native_heal_first", False):
        fallback = str(getattr(result, "native_heal_fallback_action", "") or "repair_directory_metadata")
        reason = str(
            getattr(result, "native_heal_reason", "")
            or "The planner carried this directory case to a metadata-only endpoint where Gluster heal/rescan may clear the drift first."
        )
        return (
            "run Gluster heal/rescan first",
            reason + f" If the drift remains after rescan, rerun and promote to {fallback}.",
            "choice: run Gluster heal/rescan, choose explicit mdata source brick/value, keep review, or skip",
        )

    if result.action_type == "review_directory_metadata":
        no_missing_note = any(
            note.startswith("no missing directory or stale metadata found; review only")
            for note in result.notes
        )
        child_set_note = next(
            (
                note
                for note in result.notes
                if note.startswith("directory child-set reconciliation needed across hosts: ")
            ),
            "",
        )
        depth_cap_note = next(
            (note for note in result.notes if note.startswith("directory depth cap reached: ")),
            "",
        )
        tie_preview_note = next(
            (note for note in result.notes if note.startswith("directory tie preview: ")),
            "",
        )
        evidence_bits = [bit for bit in [child_set_note, tie_preview_note, depth_cap_note] if bit]
        evidence_sentence = f" Evidence: {' '.join(evidence_bits)}" if evidence_bits else ""
        has_child_chain_evidence = bool(evidence_bits) or any(
            note.startswith("children must be repaired before parent directory")
            for note in result.notes
        )
        suggestion = "refresh_directory_evidence"
        if strategy == "review_directory_mdata_state":
            suggestion = "choose_directory_mdata_source"
            next_edge_sentence = (
                " Classified as directory mdata review: trusted.gfid and children do not point to a structural repair, "
                "but trusted.glusterfs.mdata has no clear majority. Operator choices: let Gluster try a heal and rescan, "
                "inspect the file/folder on the bricks and provide mdata_source_host or mdata_source_value in a "
                "decision file, or keep review/quarantine if the metadata disagreement accompanies name or GFID uncertainty."
            )
            choices = "choice: run Gluster heal/rescan, choose explicit mdata source brick/value, keep review, or skip"
        elif has_child_chain_evidence:
            suggestion = "reconcile_directory_children"
            next_edge_sentence = (
                " Next edge: follow the directory child chain first "
                "(review_directory_children / reconcile_directory_children for the same subtree), "
                "then rerun the same discovery route (repair-meta on the same path for a path-led review, "
                "or fresh manifest-build and plan-build for a heal-driven scan); if only canonical trusted.gfid drift remains, "
                "the next run can promote to repair_directory_metadata."
            )
            choices = "choice: follow child-chain repair, keep review, or skip"
        elif strategy == "review_directory_presence":
            suggestion = "refresh_directory_evidence"
            next_edge_sentence = (
                " Operator next: inspect mount access and child names, then rerun manifest-build and plan-build "
                "so the case can narrow into recreate_missing_directory_backend, reconcile_directory_children, "
                "or delete_below_quorum_subtree instead of staying review-only."
            )
            choices = "choice: gather focused evidence, classify recreate/reconcile/delete, keep review, or skip"
        else:
            next_edge_sentence = (
                " Classified as directory metadata-only review: no child-gap, child-reference, stale-subtree, "
                "or safe GFID-only repair branch is visible yet. Operator choices: run focused mount access "
                "and child-name probes, then rerun manifest-build and plan-build; promote to "
                "repair_directory_metadata only if the directory is otherwise consistent and only canonical "
                "trusted.gfid drift remains; choose delete_below_quorum_subtree only if the directory is proven "
                "stale or below quorum; run directory-tie-build or quarantine if names or GFIDs disagree. "
                "For arbiter volumes, arbiter evidence is metadata/quorum support only, not a payload source."
            )
            choices = "choice: gather focused evidence, promote safe metadata repair, choose stale cleanup/quarantine, keep review, or skip"
        if strategy == "review_directory_mdata_state":
            why = (
                "The directory is structurally present, but trusted.glusterfs.mdata differs without a clear majority; "
                "the repair tool should not invent a source value."
            )
        elif strategy == "review_directory_presence":
            why = (
                "The directory is present on some bricks, but the presence pattern is not yet safe to classify; "
                "operator next: check mount access, child names, and missing-host count before choosing "
                "recreate_missing_directory_backend, reconcile_directory_children, or delete_below_quorum_subtree."
            )
        elif strategy == "review_directory_state" or no_missing_note:
            why = (
                "The directory is present in the manifest, and no missing directory or stale metadata target was found; "
                "it stays review-only because the planner has no safe automatic filesystem action for this evidence."
            )
        else:
            why = "The directory candidate is inferred from child paths, but no canonical directory copy exists yet."
        return (
            suggestion,
            why
            + evidence_sentence
            + _child_paths_sentence()
            + next_edge_sentence,
            choices,
        )

    if result.action_type == "repair_directory_metadata":
        if strategy == "attach_directory_mdata":
            return (
                "directory_mdata_majority_repair",
                "The directory is present everywhere and trusted.gfid/children agree, but trusted.glusterfs.mdata has a clear majority; align the minority bricks to that majority.",
                "choice: apply majority mdata alignment, keep review, or skip",
            )
        return (
            "repair_directory_metadata",
            "The directory is present everywhere, but one or more bricks are missing the canonical trusted.gfid xattr; attach the canonical GFID to the mismatched bricks.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "repair_file_metadata":
        if getattr(result, "native_heal_first", False):
            fallback = str(getattr(result, "native_heal_fallback_action", "") or "repair_file_metadata")
            reason = str(
                getattr(result, "native_heal_reason", "")
                or "The file already exists on all replicas and only metadata differs; try Gluster heal/rescan first."
            )
            return (
                "run Gluster heal/rescan first",
                reason + f" If the drift remains after rescan, rerun and promote to {fallback}.",
                "choice: run Gluster heal/rescan, apply metadata repair, keep review, or skip",
            )
        return (
            "repair_file_metadata",
            "The file is present everywhere, but one or more bricks are missing the canonical trusted.gfid xattr; attach the canonical GFID to the mismatched bricks.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "repair_posix_metadata":
        source_host = str(getattr(result, "metadata_source_host", "") or "").strip()
        source_reason = str(getattr(result, "metadata_source_reason", "") or "").strip()
        fields = ", ".join(getattr(result, "metadata_fields_to_align", []) or ["mode", "uid", "gid"])
        if source_reason == "native_gluster_source":
            if source_host:
                return (
                    "resolve_posix_metadata_source_brick",
                    f"POSIX metadata differs across replicas; choose the source brick from {source_host} for {fields}, then let Gluster resolve the visible split-brain row natively.",
                    "choice: apply suggestion, keep review, or skip",
                )
            return (
                "resolve_posix_metadata_source_brick",
                f"POSIX metadata differs across replicas; choose the source brick for {fields}, then let Gluster resolve the visible split-brain row natively.",
                "choice: apply suggestion, keep review, or skip",
            )
        if source_host:
            return (
                "repair_posix_metadata",
                f"POSIX metadata differs across replicas; align the minority bricks to the source tuple from {source_host} for {fields}.",
                "choice: apply suggestion, keep review, or skip",
            )
        return (
            "repair_posix_metadata",
            f"POSIX metadata differs across replicas; align the minority bricks to the chosen source tuple for {fields}.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "review_dead_gfid_reference":
        live_refs = [
            note.removeprefix("dead GFID still resolves to live path reference(s): ").strip()
            for note in result.notes
            if note.startswith("dead GFID still resolves to live path reference(s): ")
        ]
        ghost_tail_note = next(
            (
                note
                for note in result.notes
                if "post-resolution ghost tail" in note
                or "nested GFID-child residue does not resolve to a live directory chain target" in note
            ),
            "",
        )
        why = (
            "This is a post-resolution ghost tail; inspect .glusterfs references and logs before deleting the residue."
            if ghost_tail_note
            else
            f"The GFID still resolves to live path reference(s): {', '.join(live_refs)}; inspect the referenced path before deleting the residue."
            if live_refs
            else "The GFID still resolves to a live path reference; inspect the referenced path before deleting the residue."
        )
        return (
            "follow_live_reference",
            why,
            "choice: inspect the live reference, keep review, or skip",
        )

    if result.action_type == "cleanup_dead_gfid":
        ghost_tail_note = next(
            (
                note
                for note in result.notes
                if "post-resolution ghost tail" in note
                or "nested GFID-child residue does not resolve to a live directory chain target" in note
            ),
            "",
        )
        return (
            "delete_dead_gfid_residue",
            (
                "This is a post-resolution ghost tail; delete the dead GFID residue by default once it is unreferenced on all replicas."
                if ghost_tail_note
                else "No live reference remains; delete the dead GFID residue by default once it is unreferenced on all replicas."
            ),
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "cleanup_dead_file_refs":
        return (
            "delete_dead_file_ref_residue",
            "No surviving file copy remains; delete the dead file residue by default once it is unreferenced on all replicas.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "cleanup_stale_glusterfs_index":
        return (
            "delete_stale_glusterfs_index_residue",
            "This is internal Gluster heal bookkeeping; delete the stale index entry by default, then rerun heal info and rebuild the plan from fresh evidence.",
            "choice: apply suggestion, keep review, or skip",
        )

    if result.action_type == "review_file_metadata":
        policy = str(result.decision.get("file_metadata_only") or DEFAULT_REVIEW_POLICIES["file_metadata_only"])
        if getattr(result, "native_heal_first", False):
            fallback = str(getattr(result, "native_heal_fallback_action", "") or "repair_file_metadata")
            reason = str(
                getattr(result, "native_heal_reason", "")
                or "The file already exists on all replicas and only metadata differs; try Gluster heal/rescan first."
            )
            return (
                "run Gluster heal/rescan first",
                reason + f" If the drift remains after rescan, rerun and promote to {fallback}.",
                "choice: run Gluster heal/rescan, apply metadata repair, keep review, or skip",
            )
        if policy == "repair":
            return (
                "repair_file_metadata",
                "The file already exists on all replicas; if the file-metadata xattr drift is confirmed, attach the canonical GFID to the mismatched bricks.",
                "choice: apply suggestion, keep review, or skip",
            )
        return (
            "enable_file_metadata_repair_policy",
            "The file already exists on all replicas; confirm canonical GFID evidence, rerun with --policy-file-metadata repair, and let the planner promote only the metadata attach branch.",
            "choice: enable metadata repair policy, keep review, or skip",
        )

    if result.action_type == "review_posix_metadata_no_majority":
        source_reason = str(getattr(result, "metadata_source_reason", "") or "").strip()
        if source_reason == "native_gluster_source":
            return (
                "choose_posix_metadata_source",
                "POSIX mode, uid, gid, or ACL values differ without a strict majority; choose a data-brick source explicitly, then use native source-brick after selection.",
                "choice: choose source brick, keep review, or skip",
            )
        return (
            "choose_posix_metadata_source",
            "POSIX mode, uid, gid, or ACL values differ without a strict majority; inspect every data brick and choose an explicit source tuple.",
            "choice: choose source, keep review, or skip",
        )

    if result.action_type == "review_type_mismatch":
        recommended_choice = planner_choice if planner_choice in {"quarantine_loser", "quarantine_both"} else "quarantine_both"
        recommended_reason = str(getattr(result, "recommended_reason", "") or "").strip()
        if recommended_choice == "quarantine_loser":
            return (
                "quarantine_loser",
                recommended_reason or "Backend mtime evidence clearly separates the file and directory sides, so quarantine the older branch.",
                "choice: quarantine_loser, quarantine_both, keep review, or skip",
            )
        return (
            "quarantine_both",
            recommended_reason or "Backend mtime evidence is missing, tied, or overlapping, so quarantine both branches first.",
            "choice: quarantine_loser, quarantine_both, keep review, or skip",
        )

    if result.action_type == "defer_dead_gfid_cleanup":
        return (
            "follow_live_reference",
            "Keep it deferred until the live reference chain is resolved on every brick; only then decide whether cleanup is safe.",
            "choice: follow live reference, keep review, or skip",
        )

    return (
        "create_support_case",
        "This unclassified case has no local repair proof; create a bounded support handoff with the manifest, plan, logs, and current heal evidence.",
        "choice: create maintainer evidence handoff, keep review, or skip",
    )


def _review_evidence_notes(result: ApplyActionResult) -> list[str]:
    skipped_prefixes = (
        "repair strategy:",
        "configured batch policy:",
        "batch default:",
        "interactive choices:",
        "preferred choices:",
        "interactive default suggestion:",
        "decision file entry loaded:",
        "choice:",
        "matrix suggestion:",
        "why:",
        "review-only action; no filesystem steps are planned yet",
    )
    evidence_notes: list[str] = []
    for note in result.notes:
        if note.startswith(skipped_prefixes):
            continue
        if note not in evidence_notes:
            evidence_notes.append(note)
    return evidence_notes


def _review_support_notes(result: ApplyActionResult) -> list[str]:
    strategy = strategy_for_result(result)
    identity_bits = [
        f"action_id={result.action_id}",
        f"action_type={result.action_type}",
    ]
    if strategy:
        identity_bits.append(f"strategy={strategy}")
    identity_bits.append(f"path={result.logical_path}")

    notes = [
        "for a classifier ticket, include " + ", ".join(identity_bits),
        "include the manifest object, plan action, apply action, current heal info, current volume info, and health-check output",
        "include targeted log excerpts from gluster-log-ops.sh grep --volume <volume> --pattern <path-or-gfid>",
    ]

    if result.action_type == "review_dead_gfid_reference":
        notes.append(
            "include ls -l, readlink, stat, and getfattr output for the .glusterfs handle plus indices/xattrop, indices/dirty, and indices/entry-changes rows on each brick"
        )
    elif result.action_type in {"review_directory_metadata", "review_directory_children", "review_directory_gfid_conflict"}:
        notes.append(
            "include directory child-name sets, directory-tie-build output when available, and stat/getfattr for the backend directory and GFID handle on each brick"
        )
    elif result.action_type == "review_entry_split_brain":
        notes.append(
            "include heal info split-brain output, file cohorts or directory tie evidence, stat/getfattr/checksum output for each visible backend, and any mount EIO/ENOTCONN error"
        )
    elif result.action_type in {"review_probable_stale_survivor", "review_probable_orphaned_symlink"}:
        notes.append(
            "include present/missing host lists, quorum settings, mount stat result, source-host evidence from heal info, and backend/GFID residue paths"
        )
    elif result.action_type == "review_file_metadata":
        notes.append(
            "include file metadata mismatch hosts plus stat/getfattr for the backend file and file-GFID handle on each brick"
        )
    elif result.action_type == "repair_posix_metadata":
        if str(getattr(result, "metadata_source_reason", "") or "").strip() == "native_gluster_source":
            notes.append(
                "include the chosen source brick, the common brick path, and the native Gluster source-brick preview/output"
            )
        else:
            notes.append(
                "include POSIX metadata mismatch hosts plus stat/getfattr for the backend file and ACL/ownership evidence on each brick"
            )
    elif result.action_type == "review_posix_metadata_no_majority":
        if str(getattr(result, "metadata_source_reason", "") or "").strip() == "native_gluster_source":
            notes.append(
                "include POSIX tuple/ACL evidence, the Gluster-visible split-brain row, and the selected source brick/source-brick command preview"
            )
        else:
            notes.append(
                "include POSIX tuple/ACL evidence plus stat/getfattr for the backend file on each brick"
            )
    elif result.action_type == "review_type_mismatch":
        notes.append(
            "include lstat/file-type evidence from every brick, backend mtime ordering, mount stat output, and any backend symlink target or GFID handle target"
        )
    elif result.action_type == "defer_dead_gfid_cleanup":
        notes.append(
            "include the live references that keep cleanup deferred and the latest probe showing whether the terminal target still exists"
        )
    else:
        notes.append(
            "include raw entries, graph markers, all evidence notes printed above, and the exact command line used to build the manifest and plan"
        )
    roles_note = _format_brick_roles_by_host_for_notes(getattr(result, "brick_roles_by_host", {}))
    if roles_note:
        notes.extend(roles_note)
    return notes
