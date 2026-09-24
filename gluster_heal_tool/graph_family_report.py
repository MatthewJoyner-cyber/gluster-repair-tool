# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Family-scoped graph reporting helpers."""
from __future__ import annotations

from typing import Any

from .planner_graph import render_graph_report


_GRAPH_FAMILY_NODE_IDS: dict[str, frozenset[str]] = {
    "file": frozenset(
        {
            "file_stale_survivor",
            "file_stale_survivor_mount_visible",
            "file_stale_survivor_mount_hidden",
            "file_delete_below_quorum",
            "file_orphaned_symlink_cleanup",
            "file_orphaned_symlink_mount_visible",
            "file_orphaned_symlink_mount_hidden",
            "file_entry_split_brain_mount_error",
            "file_split_brain_majority",
            "file_split_brain_resolver_first",
            "file_split_brain_tie",
            "file_restore",
            "file_metadata_repair",
            "file_metadata_only",
            "file_metadata_only_fallback",
            "file_symlink_restore",
            "file_entry_split_brain_replace",
            "file_entry_split_brain_tie",
            "file_entry_split_brain",
            "file_handle_ghost_cleanup",
            "post_resolution_ghost_tail",
        }
    ),
    "type-mismatch": frozenset({"type_mismatch_review"}),
    "directory": frozenset(
        {
            "directory_child_gap",
            "directory_child_gap_executable",
            "directory_child_gap_split_brain_child",
            "directory_child_reference",
            "directory_child_reference_immediate_missing",
            "directory_child_reference_live",
            "directory_presence_tie",
            "directory_delete_below_quorum",
            "directory_entry_split_brain",
            "directory_entry_split_brain_mount_error",
            "directory_gfid_conflict",
            "directory_gfid_conflict_mount_first",
            "directory_gfid_merge",
            "directory_metadata_repair",
            "directory_metadata_only",
            "directory_metadata_only_fallback",
            "directory_restore_backend",
            "directory_restore_review",
            "directory_restore_review_mount_error",
            "directory_split_brain",
        }
    ),
    "dead-gfid": frozenset(
        {
            "dead_gfid_reference",
            "dead_gfid_reference_live",
            "dead_gfid_cleanup_direct",
            "dead_gfid_cleanup",
            "dead_gfid_cleanup_terminal",
        }
    ),
    "stale-index": frozenset(
        {
            "stale_glusterfs_index_cleanup",
            "stale_glusterfs_xattrop_cleanup",
            "stale_glusterfs_dirty_cleanup",
        }
    ),
}


def normalize_graph_family(family: str) -> str:
    return family.strip().lower().replace("_", "-")


def family_node_ids(family: str) -> frozenset[str]:
    return _GRAPH_FAMILY_NODE_IDS.get(normalize_graph_family(family), frozenset())


def filter_graph_family_actions(plan_payload: dict[str, Any], family: str) -> list[dict[str, Any]]:
    node_ids = family_node_ids(family)
    actions = plan_payload.get("actions", [])
    if not isinstance(actions, list) or not node_ids:
        return []
    filtered: list[dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        if str(item.get("graph_node") or "") in node_ids:
            filtered.append(item)
    return filtered


def summarize_graph_family_report(plan_payload: dict[str, Any], family: str) -> dict[str, object]:
    filtered_actions = filter_graph_family_actions(plan_payload, family)
    return {
        "family": normalize_graph_family(family),
        "actions": len(filtered_actions),
        "matched_node_ids": sorted({str(item.get("graph_node") or "") for item in filtered_actions if item.get("graph_node")}),
    }


def render_graph_family_report(plan_payload: dict[str, Any], family: str) -> str:
    summary = summarize_graph_family_report(plan_payload, family)
    filtered_payload = {
        "actions": filter_graph_family_actions(plan_payload, summary["family"]),
        "graph_cycle": plan_payload.get("graph_cycle"),
    }
    report = render_graph_report(filtered_payload)
    node_bits = ",".join(summary["matched_node_ids"])
    summary_line = f"graph-family-summary: actions={summary['actions']}"
    if node_bits:
        summary_line += f", nodes={node_bits}"
    return f"graph-family: {summary['family']}\n{summary_line}\n{report}"
