# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Gluster repair apply helpers."""
from __future__ import annotations

import json
from pathlib import Path

from .executor import execute_apply_results, validate_execute_results, write_execute_report
from .models import ApplyActionResult, ApplyStep, BackupArtifact


DEFAULT_REVIEW_POLICIES = {
    "type_mismatch": "review",
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

DEFAULT_SPLIT_BRAIN_NATIVE_POLICY = "auto"


def load_decisions(path: str | Path) -> dict[str, dict[str, object]]:
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, dict) and isinstance(payload.get("decisions"), dict):
        decisions = payload["decisions"]
    elif isinstance(payload, dict):
        decisions = payload
    else:
        decisions = {}
    normalized: dict[str, dict[str, object]] = {}
    for decision_key, choice in decisions.items():
        if not isinstance(choice, dict) or bool(choice.get("stale")):
            continue
        if (
            bool(choice.get("read_only"))
            and not bool(choice.get("confirmed"))
            and not isinstance(choice.get("decision_payload"), dict)
        ):
            continue
        logical_path = str(choice.get("logical_path") or decision_key)
        normalized[logical_path] = dict(choice)
    return normalized


def load_plan(path: str | Path) -> dict[str, object]:
    return json.loads(Path(path).read_text())


def load_apply_payload(path: str | Path) -> dict[str, object]:
    return json.loads(Path(path).read_text())


def load_apply_results(path: str | Path, *, payload: dict | None = None) -> list[ApplyActionResult]:
    if payload is None:
        payload = load_apply_payload(path)
    results: list[ApplyActionResult] = []
    for item in payload.get("actions", []):
        results.append(
            ApplyActionResult(
                action_id=item["action_id"],
                logical_path=item["logical_path"],
                action_type=item["action_type"],
                execution_mode=item["execution_mode"],
                backup_root=item.get("backup_root", ""),
                backup_mode=item.get("backup_mode", "required"),
                batch=item.get("batch", False),
                status=item["status"],
                depends_on=item.get("depends_on", []),
                execution_wave=int(item.get("execution_wave") or 0),
                parallel_safe=bool(item.get("parallel_safe")),
                execution_resources=[str(value) for value in item.get("execution_resources", []) if str(value)],
                execution_serialization_reason=str(item.get("execution_serialization_reason") or ""),
                steps=[ApplyStep(**step) for step in item.get("steps", [])],
                revert_steps=[ApplyStep(**step) for step in item.get("revert_steps", [])],
                backup_artifacts=[BackupArtifact(**art) for art in item.get("backup_artifacts", [])],
                revert_dirs_to_create=item.get("revert_dirs_to_create", []),
                estimated_stage_bytes=item.get("estimated_stage_bytes", 0),
                estimated_backup_bytes=item.get("estimated_backup_bytes", 0),
                estimated_unknown_backup_items=item.get("estimated_unknown_backup_items", 0),
                native_heal_first=bool(item.get("native_heal_first")),
                native_heal_fallback_action=str(item.get("native_heal_fallback_action") or ""),
                native_heal_reason=str(item.get("native_heal_reason") or ""),
                recommended_choice=str(item.get("recommended_choice") or ""),
                recommended_reason=str(item.get("recommended_reason") or ""),
                brick_roles_by_host={str(host): str(role) for host, role in (item.get("brick_roles_by_host") or {}).items() if str(host) and str(role)},
                brick_host_aliases={
                    str(host): [str(alias) for alias in aliases if str(alias)]
                    for host, aliases in (item.get("brick_host_aliases") or {}).items()
                    if str(host) and isinstance(aliases, list)
                },
                brick_role_evidence_required=bool(item.get("brick_role_evidence_required")),
                brick_role_evidence_error=str(item.get("brick_role_evidence_error") or ""),
                decision=item.get("decision", {}),
                notes=item.get("notes", []),
            )
        )
    return results


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
    if result.action_type in {"repair_posix_metadata", "review_file_metadata", "review_posix_metadata_no_majority", "review_type_mismatch", "defer_dead_gfid_cleanup", "review_dead_gfid_reference", "cleanup_dead_gfid", "cleanup_stale_glusterfs_index", "cleanup_arbiter_residue"}:
        return "metadata"
    return "other"


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


from .apply_planning import build_apply_results
from .apply_reporting import _review_matrix_hint
from .apply_reporting import build_preflight_report
from .apply_reporting import collect_temp_mount_verification_paths
from .apply_reporting import filter_apply_results
from .apply_reporting import filter_execute_ready_results
from .apply_reporting import is_ready_to_execute
from .apply_reporting import render_apply_run
from .apply_reporting import render_apply_summary
from .apply_reporting import strategy_for_result
from .apply_reporting import summarize_apply_results
from .apply_reporting import write_apply_results
from .apply_reporting import write_preflight_report
