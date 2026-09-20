# SPDX-License-Identifier: GPL-2.0-only
"""Plan reporting helpers."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .models import PlanAction
from .planner_graph import summarize_graph
from .shared_io import write_json_shared


def write_plan(path: str | Path, actions: list[PlanAction], *,
               manifest_in: str | Path | None = None, manifest_payload: dict | None = None) -> None:
    payload = {
        "schema_version": 1,
        "actions": [action.to_dict() for action in actions],
    }
    if manifest_in is not None:
        from .apply_binding import bind_plan
        if manifest_payload is None:
            raise ValueError("manifest_payload is required with manifest_in")
        bind_plan(payload, manifest_payload, manifest_in)
    write_json_shared(path, payload)


def summarize_plan(actions: list[PlanAction]) -> dict[str, object]:
    action_types = Counter(action.action_type for action in actions)
    repair_strategies = Counter(
        action.repair_strategy for action in actions if action.repair_strategy
    )
    layered_tail_sequences = sum(1 for action in actions if action.followup_edges)
    ready_repairs = (
        action_types.get("repair_file", 0)
        + action_types.get("repair_file_metadata", 0)
        + action_types.get("reconcile_directory", 0)
        + action_types.get("repair_directory_metadata", 0)
        + action_types.get("cleanup_dead_gfid", 0)
        + action_types.get("cleanup_dead_file_refs", 0)
        + action_types.get("cleanup_stale_glusterfs_index", 0)
        + action_types.get("cleanup_arbiter_residue", 0)
    )
    review_items = (
        action_types.get("review_entry_split_brain", 0)
        + action_types.get("review_probable_stale_survivor", 0)
        + action_types.get("review_probable_orphaned_symlink", 0)
        + action_types.get("review_directory_children", 0)
        + action_types.get("review_directory_gfid_conflict", 0)
        + action_types.get("review_directory_metadata", 0)
        + action_types.get("review_file_metadata", 0)
        + action_types.get("review_dead_gfid_reference", 0)
        + action_types.get("review_type_mismatch", 0)
        + action_types.get("cleanup_dead_gfid", 0)
    )
    summary = {
        "actions_total": len(actions),
        "action_types": dict(action_types),
        "repair_strategies": dict(repair_strategies),
        "ready_repairs": ready_repairs,
        "review_items": review_items,
        "layered_tail_sequences": layered_tail_sequences,
        "files_to_repair": action_types.get("repair_file", 0),
        "file_metadata_repairs": action_types.get("repair_file_metadata", 0),
        "dead_file_ref_cleanups": action_types.get("cleanup_dead_file_refs", 0),
        "stale_glusterfs_index_cleanups": action_types.get("cleanup_stale_glusterfs_index", 0),
        "arbiter_residue_cleanups": action_types.get("cleanup_arbiter_residue", 0),
        "directories_to_reconcile": (
            action_types.get("reconcile_directory", 0)
            + action_types.get("repair_directory_metadata", 0)
        ),
        "entry_split_brain_reviews": action_types.get("review_entry_split_brain", 0),
        "probable_stale_survivor_reviews": action_types.get("review_probable_stale_survivor", 0),
        "probable_orphaned_symlink_reviews": action_types.get("review_probable_orphaned_symlink", 0),
        "directory_gfid_conflict_reviews": action_types.get("review_directory_gfid_conflict", 0),
        "directory_metadata_reviews": action_types.get("review_directory_metadata", 0),
        "directory_metadata_repairs": action_types.get("repair_directory_metadata", 0),
        "file_metadata_reviews": action_types.get("review_file_metadata", 0),
        "dead_gfid_reference_reviews": action_types.get("review_dead_gfid_reference", 0),
        "dead_gfid_cleanups": action_types.get("cleanup_dead_gfid", 0),
    }
    graph_summary = summarize_graph(actions)
    if graph_summary.get("graph_nodes"):
        summary.update(graph_summary)
    return summary


def render_plan_summary(summary: dict[str, object]) -> str:
    parts = [
        "PLAN",
        "----",
        f"Plan ready: {summary.get('ready_repairs', 0)} ready repairs",
    ]
    details = [
        f"{summary.get('files_to_repair', 0)} files",
        f"{summary.get('directories_to_reconcile', 0)} directories",
    ]
    if summary.get("dead_gfid_cleanups", 0):
        details.append(f"{summary.get('dead_gfid_cleanups', 0)} dead-GFID cleanups")
    if summary.get("dead_file_ref_cleanups", 0):
        details.append(f"{summary.get('dead_file_ref_cleanups', 0)} dead-file-ref cleanups")
    if summary.get("stale_glusterfs_index_cleanups", 0):
        details.append(f"{summary.get('stale_glusterfs_index_cleanups', 0)} stale .glusterfs index cleanups")
    if summary.get("arbiter_residue_cleanups", 0):
        details.append(f"{summary.get('arbiter_residue_cleanups', 0)} arbiter-residue cleanups")
    if summary.get("file_metadata_repairs", 0):
        details.append(f"{summary.get('file_metadata_repairs', 0)} file metadata repairs")
    parts.append(", ".join(details))
    review_parts = [
        ("split-brain", summary.get("entry_split_brain_reviews", 0)),
        ("stale-survivor", summary.get("probable_stale_survivor_reviews", 0)),
        ("orphaned-symlink", summary.get("probable_orphaned_symlink_reviews", 0)),
        ("layered-tails", summary.get("layered_tail_sequences", 0)),
        ("dir-conflicts", summary.get("directory_gfid_conflict_reviews", 0)),
        ("dir-metadata", summary.get("directory_metadata_reviews", 0)),
        ("file-metadata", summary.get("file_metadata_reviews", 0)),
        ("dead-gfid-ref", summary.get("dead_gfid_reference_reviews", 0)),
        ("dead-gfid-cleanup", summary.get("dead_gfid_cleanups", 0)),
    ]
    reviews = ", ".join(f"{label}={count}" for label, count in review_parts if count)
    if reviews:
        parts.append(f"review items: {summary.get('review_items', 0)} ({reviews})")
    graph_nodes = summary.get("graph_nodes") or {}
    if graph_nodes:
        graph_bits = [
            f"provisional={summary.get('graph_provisional_actions', 0)}",
            f"rescan={summary.get('graph_rescan_actions', 0)}",
            f"followups={summary.get('graph_followup_edges', 0)}",
        ]
        node_bits = ",".join(f"{node}={count}" for node, count in sorted(graph_nodes.items()))
        if node_bits:
            graph_bits.append(f"nodes={node_bits}")
        parts.append("graph: " + ", ".join(graph_bits))
    return "\n".join(parts)
