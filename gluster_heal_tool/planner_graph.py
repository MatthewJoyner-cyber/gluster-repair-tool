# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Decision-graph metadata for planner actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import asdict
from typing import Any

from .models import PlanAction


@dataclass(frozen=True)
class DecisionEdge:
    edge_id: str
    priority: int
    when: str
    next_node: str
    report: str


@dataclass(frozen=True)
class DecisionNode:
    node_id: str
    matrix_key: str
    action_types: frozenset[str] = field(default_factory=frozenset)
    strategies: frozenset[str] = field(default_factory=frozenset)
    graph_markers: frozenset[str] = field(default_factory=frozenset)
    path_markers: frozenset[str] = field(default_factory=frozenset)
    note_markers: frozenset[str] = field(default_factory=frozenset)
    decision_class: str = ""
    provisional: bool = False
    terminal_policy: str = ""
    post_apply: str = ""
    followups: tuple[DecisionEdge, ...] = ()

    def __post_init__(self) -> None:
        markers = set(self.graph_markers)
        markers.update(f"action_type:{value}" for value in self.action_types)
        markers.update(f"repair_strategy:{value}" for value in self.strategies)
        object.__setattr__(self, "graph_markers", frozenset(markers))


_DECISION_NODES: dict[str, DecisionNode] = {
    "file_presence_tie": DecisionNode(
        node_id="file_presence_tie",
        matrix_key="file_presence_tie",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"ambiguous_file_presence_tie"}),
        graph_markers=frozenset({"file_presence_tie:review"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_presence_tie": DecisionNode(
        node_id="directory_presence_tie",
        matrix_key="directory_presence_tie",
        action_types=frozenset({"review_directory_gfid_conflict"}),
        strategies=frozenset({"ambiguous_directory_presence_tie"}),
        graph_markers=frozenset({"directory_presence_tie:review"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_stale_survivor": DecisionNode(
        node_id="file_stale_survivor",
        matrix_key="review_probable_stale_survivor",
        action_types=frozenset({"review_probable_stale_survivor"}),
        strategies=frozenset({"delete_below_quorum_file"}),
        graph_markers=frozenset({"file_stale_survivor:review"}),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_stale_survivor_mount_visible": DecisionNode(
        node_id="file_stale_survivor_mount_visible",
        matrix_key="review_probable_stale_survivor_visible",
        action_types=frozenset({"review_probable_stale_survivor"}),
        strategies=frozenset({"delete_below_quorum_file"}),
        graph_markers=frozenset({"mount:visible", "file_stale_survivor:mount_visible"}),
        note_markers=frozenset(
            {
                "file is still mount-accessible, but below-quorum survivors should default toward delete review, not recreate",
            }
        ),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_stale_survivor_mount_hidden": DecisionNode(
        node_id="file_stale_survivor_mount_hidden",
        matrix_key="review_probable_stale_survivor_hidden",
        action_types=frozenset({"review_probable_stale_survivor"}),
        strategies=frozenset({"delete_below_quorum_file"}),
        graph_markers=frozenset({"mount:hidden", "file_stale_survivor:mount_hidden"}),
        note_markers=frozenset(
            {
                "file is not accessible through the Gluster mount; likely stale survivor after a failed delete",
            }
        ),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_delete_below_quorum": DecisionNode(
        node_id="file_delete_below_quorum",
        matrix_key="file_delete_below_quorum",
        action_types=frozenset({"review_probable_stale_survivor"}),
        strategies=frozenset({"delete_below_quorum_file"}),
        note_markers=frozenset(
            {
                "file is present on only",
                "below quorum threshold",
                "default batch policy should lean delete of the stale backend file and file/GFID metadata",
            }
        ),
        decision_class="risky_default",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_orphaned_symlink_cleanup": DecisionNode(
        node_id="file_orphaned_symlink_cleanup",
        matrix_key="review_probable_orphaned_symlink",
        action_types=frozenset({"review_probable_orphaned_symlink"}),
        strategies=frozenset({"delete_orphaned_symlink_residue"}),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_orphaned_symlink_mount_visible": DecisionNode(
        node_id="file_orphaned_symlink_mount_visible",
        matrix_key="review_probable_orphaned_symlink_visible",
        action_types=frozenset({"review_probable_orphaned_symlink"}),
        strategies=frozenset({"delete_orphaned_symlink_residue"}),
        graph_markers=frozenset({"mount:visible", "file_orphaned_symlink:mount_visible"}),
        note_markers=frozenset(
            {
                "symlink is still mount-accessible, but below-quorum residues should default toward delete review, not recreate",
            }
        ),
        decision_class="review_or_operator",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_orphaned_symlink_mount_hidden": DecisionNode(
        node_id="file_orphaned_symlink_mount_hidden",
        matrix_key="review_probable_orphaned_symlink_hidden",
        action_types=frozenset({"review_probable_orphaned_symlink"}),
        strategies=frozenset({"delete_orphaned_symlink_residue"}),
        graph_markers=frozenset({"mount:hidden", "file_orphaned_symlink:mount_hidden"}),
        note_markers=frozenset(
            {
                "symlink is not accessible through the Gluster mount; likely orphaned residue after a failed delete",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "directory_child_gap": DecisionNode(
        node_id="directory_child_gap",
        matrix_key="review_directory_children",
        action_types=frozenset({"review_directory_children", "reconcile_directory"}),
        strategies=frozenset({"reconcile_directory_children", "reconcile_directory_set"}),
        graph_markers=frozenset({"directory_child_gap:present"}),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "directory_child_gap_executable": DecisionNode(
        node_id="directory_child_gap_executable",
        matrix_key="review_directory_children_executable",
        action_types=frozenset({"review_directory_children", "reconcile_directory"}),
        strategies=frozenset({"reconcile_directory_children", "reconcile_directory_set"}),
        graph_markers=frozenset({"directory_child_gap:present", "directory_child_gap:executable"}),
        note_markers=frozenset({"child-gap dependency closure is executable"}),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "directory_child_gap_split_brain_child": DecisionNode(
        node_id="directory_child_gap_split_brain_child",
        matrix_key="review_directory_children_split_brain_child",
        action_types=frozenset({"review_directory_children", "reconcile_directory"}),
        strategies=frozenset({"reconcile_directory_children", "reconcile_directory_set"}),
        graph_markers=frozenset({"directory_child_gap:present", "directory_child_gap:split_brain_child"}),
        note_markers=frozenset({"synthetic child-gap with split-brain child"}),
        decision_class="provisional",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "directory_child_reference_immediate_missing": DecisionNode(
        node_id="directory_child_reference_immediate_missing",
        matrix_key="directory_child_reference",
        action_types=frozenset({"review_directory_children"}),
        strategies=frozenset({"verify_directory_child_absence"}),
        graph_markers=frozenset({"directory_child_reference:immediate_missing"}),
        decision_class="chain_follow",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_child_reference_live": DecisionNode(
        node_id="directory_child_reference_live",
        matrix_key="directory_child_reference",
        action_types=frozenset({"review_directory_children"}),
        strategies=frozenset({"reconcile_directory_children"}),
        graph_markers=frozenset({"directory_child_reference:live"}),
        note_markers=frozenset(
            {
                "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup",
            }
        ),
        decision_class="chain_follow",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_child_reference": DecisionNode(
        node_id="directory_child_reference",
        matrix_key="directory_child_reference",
        action_types=frozenset({"review_directory_children"}),
        strategies=frozenset({"reconcile_directory_children"}),
        decision_class="chain_follow",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_gfid_conflict": DecisionNode(
        node_id="directory_gfid_conflict",
        matrix_key="review_directory_gfid_conflict",
        action_types=frozenset({"review_directory_gfid_conflict"}),
        strategies=frozenset({"rename_conflicting_directory"}),
        decision_class="review_or_operator",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_gfid_conflict_mount_first": DecisionNode(
        node_id="directory_gfid_conflict_mount_first",
        matrix_key="review_directory_gfid_conflict",
        action_types=frozenset({"review_directory_gfid_conflict"}),
        strategies=frozenset({"rename_conflicting_directory"}),
        note_markers=frozenset(
            {
                "same path, two directory GFIDs, different contents",
            }
        ),
        decision_class="review_or_operator",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_gfid_merge": DecisionNode(
        node_id="directory_gfid_merge",
        matrix_key="directory_gfid_merge",
        action_types=frozenset({"repair_directory_metadata"}),
        strategies=frozenset({"attach_directory_gfid"}),
        note_markers=frozenset(
            {
                "bounded trees collapse cleanly",
                "majority-backed canonical directory GFID",
                "merge can promote to the existing attach_directory_gfid path",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "directory_split_brain": DecisionNode(
        node_id="directory_split_brain",
        matrix_key="directory_split_brain",
        action_types=frozenset({"review_directory_gfid_conflict"}),
        strategies=frozenset({"rename_conflicting_directory"}),
        note_markers=frozenset(
            {
                "quarantine the losing backend entry",
                "do not auto-merge or auto-delete directory conflicts in place",
                "pure ties review-only",
            }
        ),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_metadata_repair": DecisionNode(
        node_id="directory_metadata_repair",
        matrix_key="directory_metadata_repair",
        action_types=frozenset({"repair_directory_metadata"}),
        strategies=frozenset({"attach_directory_gfid", "attach_directory_mdata"}),
        note_markers=frozenset(
            {
                "reattach the canonical directory GFID xattr to the existing backend directory",
                "directory exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                "majority-backed canonical directory GFID:",
                "merge can promote to the existing attach_directory_gfid path",
                "both data bricks agree on directory identity, while the arbiter placeholder is missing trusted.gfid or its checked .glusterfs handle",
                "both data bricks agree on directory identity, so they authorize replacing a missing or conflicting arbiter trusted.gfid and rebuilding its checked .glusterfs handle",
                "trusted.glusterfs.mdata has a clear majority",
                "this repairs metadata drift without recreating the directory tree",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "directory_metadata_only": DecisionNode(
        node_id="directory_metadata_only",
        matrix_key="directory_metadata_only",
        action_types=frozenset({"review_directory_metadata"}),
        strategies=frozenset({"review_directory_state"}),
        note_markers=frozenset({"no automatic directory metadata action"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_metadata_only_fallback": DecisionNode(
        node_id="directory_metadata_only_fallback",
        matrix_key="directory_metadata_only",
        action_types=frozenset({"review_directory_metadata"}),
        strategies=frozenset({"review_directory_state"}),
        graph_markers=frozenset({"directory_metadata_only:fallback"}),
        note_markers=frozenset({"no missing directory or stale metadata found; review only"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_restore_backend": DecisionNode(
        node_id="directory_restore_backend",
        matrix_key="directory_restore",
        action_types=frozenset({"repair_directory_metadata"}),
        strategies=frozenset({"recreate_missing_directory_backend", "recreate_missing_directory_backend_child_gap"}),
        note_markers=frozenset(
            {
                "recreate the missing backend directory on the affected brick and restore the correct directory GFID linkage",
                "brick-side targets to recreate:",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),

    "directory_restore_review": DecisionNode(
        node_id="directory_restore_review",
        matrix_key="directory_restore",
        action_types=frozenset({"review_directory_metadata"}),
        strategies=frozenset({"review_directory_presence"}),
        note_markers=frozenset({"directory presence pattern does not cleanly fit recreate-vs-delete rules; review before action"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_restore_review_mount_error": DecisionNode(
        node_id="directory_restore_review_mount_error",
        matrix_key="directory_restore",
        action_types=frozenset({"review_directory_metadata"}),
        strategies=frozenset({"review_directory_presence"}),
        note_markers=frozenset(
            {
                "mount access errors observed:",
                "mount returned EIO/ENOTCONN; treat this as split-brain-like or below-quorum survivor evidence even if Gluster split-brain info reports zero",
            }
        ),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_delete_below_quorum": DecisionNode(
        node_id="directory_delete_below_quorum",
        matrix_key="directory_delete_below_quorum",
        action_types=frozenset({"review_probable_stale_survivor"}),
        strategies=frozenset({"delete_below_quorum_subtree"}),
        note_markers=frozenset(
            {
                "directory is still mount-accessible, but below-quorum survivors should default toward subtree-delete review, not recreate",
                "default batch policy should lean delete of the stale subtree deepest-first, then parent last",
            }
        ),
        decision_class="risky_default",
        provisional=True,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_entry_split_brain_mount_error": DecisionNode(
        node_id="file_entry_split_brain_mount_error",
        matrix_key="review_entry_split_brain_file",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"replace_entry_split_brain_file"}),
        graph_markers=frozenset({"mount:error"}),
        note_markers=frozenset(
            {
                "file access through the Gluster mount reports an error or remains inaccessible despite backend presence; likely entry split-brain or name/GFID conflict",
            }
        ),
        decision_class="risky_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_entry_split_brain_replace": DecisionNode(
        node_id="file_entry_split_brain_replace",
        matrix_key="review_entry_split_brain_file",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"replace_entry_split_brain_file"}),
        note_markers=frozenset(
            {
                "file shows entry split-brain or name/GFID conflict evidence; try the official Gluster split-brain resolver first, then pick a winner, remove losing backend files and file/GFID metadata, and recreate through the mount if needed",
            }
        ),
        decision_class="risky_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_entry_split_brain_tie": DecisionNode(
        node_id="file_entry_split_brain_tie",
        matrix_key="review_entry_split_brain_tie",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"ambiguous_entry_split_brain_file"}),
        note_markers=frozenset(
            {
                "file shows entry split-brain evidence, but the top file-identity cohorts are tied; do not auto-replace without checksum or explicit decision",
                "decision file should specify keep_gfid or keep_host",
            }
        ),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_entry_split_brain_mount_error": DecisionNode(
        node_id="directory_entry_split_brain_mount_error",
        matrix_key="review_entry_split_brain_directory",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"review_entry_split_brain_directory"}),
        graph_markers=frozenset({"mount:error"}),
        note_markers=frozenset(
            {
                "directory access through the Gluster mount reports an error or remains inaccessible despite backend presence; likely entry split-brain or name/GFID conflict",
            }
        ),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_dead_ref_cleanup": DecisionNode(
        node_id="file_dead_ref_cleanup",
        matrix_key="file_dead_ref_cleanup",
        action_types=frozenset({"cleanup_dead_file_refs"}),
        strategies=frozenset({"delete_dead_file_ref_residue"}),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "arbiter_only_residue_cleanup": DecisionNode(
        node_id="arbiter_only_residue_cleanup",
        matrix_key="arbiter_only_residue_cleanup",
        action_types=frozenset({"cleanup_dead_file_refs", "cleanup_arbiter_residue"}),
        strategies=frozenset({"delete_dead_file_ref_residue", "delete_arbiter_only_residue"}),
        graph_markers=frozenset({"arbiter_only_residue:file", "arbiter_only_residue:directory"}),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_conflicting_replace": DecisionNode(
        node_id="file_conflicting_replace",
        matrix_key="file_split_brain_majority",
        action_types=frozenset({"repair_file"}),
        strategies=frozenset({"replace_conflicting_file"}),
        decision_class="risky_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "arbiter_data_identity_repair": DecisionNode(
        node_id="arbiter_data_identity_repair",
        matrix_key="arbiter_data_identity_repair",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"arbiter_backed_data_identity_conflict"}),
        graph_markers=frozenset({"file_arbiter_identity:review"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_split_brain_resolver_first": DecisionNode(
        node_id="file_split_brain_resolver_first",
        matrix_key="file_split_brain_majority",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"replace_entry_split_brain_file"}),
        note_markers=frozenset({"try the official Gluster split-brain resolver first"}),
        decision_class="risky_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_split_brain_majority": DecisionNode(
        node_id="file_split_brain_majority",
        matrix_key="file_split_brain_majority",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"replace_entry_split_brain_file"}),
        note_markers=frozenset(
            {
                "try the official Gluster split-brain resolver first",
                "remove losing backend files and file/GFID metadata",
            }
        ),
        decision_class="risky_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_split_brain_tie": DecisionNode(
        node_id="file_split_brain_tie",
        matrix_key="file_split_brain_tie",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"ambiguous_entry_split_brain_file"}),
        note_markers=frozenset(
            {
                "top file-identity cohorts are tied",
                "do not auto-replace without checksum or explicit decision",
                "decision file should specify keep_gfid or keep_host",
            }
        ),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_metadata_repair": DecisionNode(
        node_id="file_metadata_repair",
        matrix_key="file_metadata_repair",
        action_types=frozenset({"repair_file_metadata"}),
        strategies=frozenset({"attach_file_gfid"}),
        note_markers=frozenset(
            {
                "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                "canonical file GFID:",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "posix_metadata_repair": DecisionNode(
        node_id="posix_metadata_repair",
        matrix_key="posix_metadata_repair",
        action_types=frozenset({"repair_posix_metadata"}),
        strategies=frozenset({"align_posix_metadata_majority", "align_posix_metadata_selected_source"}),
        note_markers=frozenset(
            {
                "POSIX metadata is present on all replicas; align the minority bricks to the majority tuple",
                "metadata source host:",
                "metadata mismatch hosts:",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "posix_metadata_no_majority": DecisionNode(
        node_id="posix_metadata_no_majority",
        matrix_key="posix_metadata_no_majority",
        action_types=frozenset({"review_posix_metadata_no_majority"}),
        strategies=frozenset({"choose_posix_metadata_source"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_metadata_only": DecisionNode(
        node_id="file_metadata_only",
        matrix_key="file_metadata_only",
        action_types=frozenset({"review_file_metadata"}),
        strategies=frozenset({"review_metadata_only"}),
        graph_markers=frozenset({"file_metadata_only:review"}),
        note_markers=frozenset(
            {
                "no missing or conflicting backend file found; only metadata review remains",
            }
        ),
        decision_class="policy_gated",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_metadata_only_fallback": DecisionNode(
        node_id="file_metadata_only_fallback",
        matrix_key="file_metadata_only",
        action_types=frozenset({"review_file_metadata"}),
        strategies=frozenset({"review_metadata_only"}),
        graph_markers=frozenset({"file_metadata_only:fallback"}),
        note_markers=frozenset({"Fallback only after file-family"}),
        decision_class="policy_gated",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "type_mismatch_review": DecisionNode(
        node_id="type_mismatch_review",
        matrix_key="review_type_mismatch",
        action_types=frozenset({"review_type_mismatch"}),
        strategies=frozenset({"review_type_mismatch"}),
        graph_markers=frozenset({"type_mismatch:review"}),
        note_markers=frozenset(
            {
                "synthetic type mismatch case",
                "File vs directory disagreement is too risky to auto-resolve.",
                "quarantine both conflicting branches first",
            }
        ),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_symlink_restore": DecisionNode(
        node_id="file_symlink_restore",
        matrix_key="file_symlink",
        action_types=frozenset({"repair_file"}),
        strategies=frozenset({"restore_missing_replica"}),
        graph_markers=frozenset({"file_subtype:symlink"}),
        note_markers=frozenset(
            {
                "file subtype: symlink",
            }
        ),
        decision_class="chain_follow",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_restore": DecisionNode(
        node_id="file_restore",
        matrix_key="file_restore",
        action_types=frozenset({"repair_file"}),
        strategies=frozenset({"restore_missing_replica", "restore_missing_child_replica"}),
        graph_markers=frozenset({"file_restore:review"}),
        note_markers=frozenset(
            {
                "stage winning copy locally",
                "remove only stale metadata on missing replicas",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "file_entry_split_brain": DecisionNode(
        node_id="file_entry_split_brain",
        matrix_key="review_entry_split_brain",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"replace_entry_split_brain_file", "ambiguous_entry_split_brain_file"}),
        decision_class="review_or_operator",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "directory_entry_split_brain": DecisionNode(
        node_id="directory_entry_split_brain",
        matrix_key="review_entry_split_brain_directory",
        action_types=frozenset({"review_entry_split_brain"}),
        strategies=frozenset({"review_entry_split_brain_directory"}),
        decision_class="operator_only",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "dead_gfid_reference": DecisionNode(
        node_id="dead_gfid_reference",
        matrix_key="review_dead_gfid_reference",
        action_types=frozenset({"review_dead_gfid_reference"}),
        strategies=frozenset({"follow_live_reference"}),
        decision_class="review_or_operator",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "dead_gfid_reference_live": DecisionNode(
        node_id="dead_gfid_reference_live",
        matrix_key="dead_gfid_reference",
        action_types=frozenset({"review_dead_gfid_reference"}),
        strategies=frozenset({"follow_live_reference"}),
        graph_markers=frozenset({"dead_gfid:live_reference"}),
        note_markers=frozenset(
            {
                "dead GFID still resolves to live path reference(s)",
                "resolved live reference chain",
                "inspect the referenced live path before deciding whether the GFID residue is stale or part of a live conflict",
            }
        ),
        decision_class="chain_follow",
        provisional=False,
        terminal_policy="operator_decision",
        post_apply="replan",
    ),
    "file_handle_ghost_cleanup": DecisionNode(
        node_id="file_handle_ghost_cleanup",
        matrix_key="file_handle_ghost_cleanup",
        action_types=frozenset({"cleanup_dead_gfid"}),
        strategies=frozenset({"delete_dead_gfid_residue"}),
        graph_markers=frozenset({"dead_gfid:handle_ghost"}),
        note_markers=frozenset({"regular .glusterfs handle ghost"}),
        decision_class="ready_cleanup",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "post_resolution_ghost_tail": DecisionNode(
        node_id="post_resolution_ghost_tail",
        matrix_key="post_resolution_ghost_tail",
        action_types=frozenset({"cleanup_dead_gfid"}),
        strategies=frozenset({"delete_dead_gfid_residue"}),
        graph_markers=frozenset({"dead_gfid:post_resolution_tail"}),
        note_markers=frozenset({"post-resolution ghost tail"}),
        decision_class="ready_cleanup",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "dead_gfid_cleanup_direct": DecisionNode(
        node_id="dead_gfid_cleanup_direct",
        matrix_key="dead_gfid_cleanup_direct",
        action_types=frozenset({"cleanup_dead_gfid"}),
        strategies=frozenset({"delete_dead_gfid_residue"}),
        graph_markers=frozenset({"dead_gfid:cleanup_direct"}),
        note_markers=frozenset(
            {
                "unknown object carries stale GFID residue but no live reference chain; cleanup is safe once the residue is unreferenced on all replicas",
                "dead GFID reference chain resolves to no live manifest object; cleanup is safe once no live backend reference remains",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "dead_gfid_cleanup_terminal": DecisionNode(
        node_id="dead_gfid_cleanup_terminal",
        matrix_key="dead_gfid_cleanup",
        action_types=frozenset({"cleanup_dead_gfid"}),
        strategies=frozenset({"delete_dead_gfid_residue"}),
        graph_markers=frozenset({"dead_gfid:cleanup_terminal"}),
        note_markers=frozenset(
            {
                "final-step-only",
                "confirm-unreferenced-on-all-replicas-before-delete",
                "delete the stale GFID residue after all live references are ruled out",
            }
        ),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "dead_gfid_cleanup": DecisionNode(
        node_id="dead_gfid_cleanup",
        matrix_key="cleanup_dead_gfid",
        action_types=frozenset({"cleanup_dead_gfid"}),
        strategies=frozenset({"delete_dead_gfid_residue"}),
        decision_class="ready_cleanup",
        provisional=False,
        terminal_policy="complete",
        post_apply="complete",
    ),
    "stale_glusterfs_index_cleanup": DecisionNode(
        node_id="stale_glusterfs_index_cleanup",
        matrix_key="cleanup_stale_glusterfs_index",
        action_types=frozenset({"cleanup_stale_glusterfs_index"}),
        strategies=frozenset({"delete_stale_glusterfs_index_residue"}),
        decision_class="ready_cleanup",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "stale_glusterfs_xattrop_cleanup": DecisionNode(
        node_id="stale_glusterfs_xattrop_cleanup",
        matrix_key="stale_glusterfs_xattrop_cleanup",
        action_types=frozenset({"cleanup_stale_glusterfs_index"}),
        strategies=frozenset({"delete_stale_glusterfs_index_residue"}),
        path_markers=frozenset({".glusterfs/indices/xattrop/"}),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
    "stale_glusterfs_dirty_cleanup": DecisionNode(
        node_id="stale_glusterfs_dirty_cleanup",
        matrix_key="stale_glusterfs_dirty_cleanup",
        action_types=frozenset({"cleanup_stale_glusterfs_index"}),
        strategies=frozenset({"delete_stale_glusterfs_index_residue"}),
        path_markers=frozenset({".glusterfs/indices/dirty/"}),
        decision_class="safe_default",
        provisional=False,
        terminal_policy="continue_after_rescan",
        post_apply="heal_rescan",
    ),
}

_FOLLOWUP_EDGE_RULES: dict[str, tuple[DecisionEdge, ...]] = {
    "file_stale_survivor": (
        DecisionEdge(
            edge_id="file_residue_to_directory_child_gap",
            priority=10,
            when="same_subtree_has_directory_child_gap",
            next_node="directory_child_gap",
            report="Resolve file residue first, then replan the child directory from a fresh snapshot.",
        ),
        DecisionEdge(
            edge_id="file_residue_to_file_split_brain",
            priority=20,
            when="same_subtree_has_file_entry_split_brain",
            next_node="file_entry_split_brain",
            report="Resolve file residue first, then replan the file conflict from fresh evidence.",
        ),
    ),
    "directory_child_gap": (
        DecisionEdge(
            edge_id="directory_child_gap_to_parent_replan",
            priority=10,
            when="missing_child_names_or_dependencies",
            next_node="directory_child_gap",
            report="Reconcile the child set, then rebuild manifest and plan before deciding the parent.",
        ),
        DecisionEdge(
            edge_id="directory_child_gap_to_directory_child_reference",
            priority=20,
            when="same_subtree_has_directory_child_reference",
            next_node="directory_child_reference",
            report="Follow the live child reference first, then replan the child gap from a fresh snapshot.",
        ),
    ),
    "directory_restore_backend": (
        DecisionEdge(
            edge_id="directory_restore_backend_to_directory_child_gap",
            priority=10,
            when="same_subtree_has_directory_child_gap_or_reference",
            next_node="directory_child_gap",
            report="Recreate the missing directory backend, then replan the child set from a fresh snapshot.",
        ),
    ),
    "directory_metadata_only": (
        DecisionEdge(
            edge_id="directory_metadata_to_directory_child_reference",
            priority=10,
            when="same_subtree_has_directory_child_reference",
            next_node="directory_child_reference",
            report="Follow the live child reference first, then rebuild manifest and plan; only after the child chain is clean should metadata-only drift promote to directory metadata repair.",
        ),
        DecisionEdge(
            edge_id="directory_metadata_to_directory_child_gap",
            priority=20,
            when="same_subtree_has_directory_child_gap",
            next_node="directory_child_gap",
            report="Reconcile the directory child chain first, then rebuild manifest and plan; if only canonical GFID drift remains, the next plan can promote to directory metadata repair.",
        ),
        DecisionEdge(
            edge_id="directory_metadata_to_directory_metadata_repair",
            priority=30,
            when="same_subtree_has_directory_metadata_repair",
            next_node="directory_metadata_repair",
            report="If the directory is otherwise consistent and only canonical GFID drift remains, promote metadata-only review to directory metadata repair.",
        ),
    ),
    "directory_metadata_only_fallback": (
        DecisionEdge(
            edge_id="directory_metadata_to_directory_child_reference",
            priority=10,
            when="same_subtree_has_directory_child_reference",
            next_node="directory_child_reference",
            report="Follow the live child reference first, then rebuild manifest and plan; only after the child chain is clean should metadata-only drift promote to directory metadata repair.",
        ),
        DecisionEdge(
            edge_id="directory_metadata_to_directory_child_gap",
            priority=20,
            when="same_subtree_has_directory_child_gap",
            next_node="directory_child_gap",
            report="Reconcile the directory child chain first, then rebuild manifest and plan; if only canonical GFID drift remains, the next plan can promote to directory metadata repair.",
        ),
        DecisionEdge(
            edge_id="directory_metadata_to_directory_metadata_repair",
            priority=30,
            when="same_subtree_has_directory_metadata_repair",
            next_node="directory_metadata_repair",
            report="If the directory is otherwise consistent and only canonical GFID drift remains, promote metadata-only review to directory metadata repair.",
        ),
    ),
    "file_metadata_only": (
        DecisionEdge(
            edge_id="file_metadata_to_file_metadata_repair",
            priority=10,
            when="same_subtree_has_file_metadata_repair",
            next_node="file_metadata_repair",
            report="If the file's only remaining issue is canonical GFID drift, promote metadata-only review to file metadata repair.",
        ),
    ),
    "file_metadata_only_fallback": (
        DecisionEdge(
            edge_id="file_metadata_to_file_metadata_repair",
            priority=10,
            when="same_subtree_has_file_metadata_repair",
            next_node="file_metadata_repair",
            report="If the file's only remaining issue is canonical GFID drift, promote metadata-only review to file metadata repair.",
        ),
    ),
    "file_split_brain_majority": (
        DecisionEdge(
            edge_id="file_split_brain_majority_to_file_metadata_repair",
            priority=10,
            when="winner_copy_selected_and_only_metadata_remains",
            next_node="file_metadata_repair",
            report="After the majority winner is chosen, reclassify any leftover GFID drift as file metadata repair.",
        ),
    ),
    "file_split_brain_resolver_first": (
        DecisionEdge(
            edge_id="file_split_brain_majority_to_file_metadata_repair",
            priority=10,
            when="winner_copy_selected_and_only_metadata_remains",
            next_node="file_metadata_repair",
            report="After the majority winner is chosen, reclassify any leftover GFID drift as file metadata repair.",
        ),
    ),
    "file_split_brain_tie": (
        DecisionEdge(
            edge_id="file_split_brain_tie_to_file_metadata_only",
            priority=10,
            when="tie_stays_review_only_after_checksum_or_decision",
            next_node="file_metadata_only",
            report="If the file tie still cannot be resolved automatically, keep the remaining path at metadata-only review.",
        ),
    ),
    "file_entry_split_brain_tie": (
        DecisionEdge(
            edge_id="file_entry_split_brain_tie_to_file_metadata_only",
            priority=10,
            when="tie_stays_review_only_after_checksum_or_decision",
            next_node="file_metadata_only",
            report="If the file tie still cannot be resolved automatically, keep the remaining path at metadata-only review.",
        ),
    ),
    "file_symlink_restore": (
        DecisionEdge(
            edge_id="file_symlink_restore_to_file_metadata_repair",
            priority=10,
            when="same_subtree_has_file_metadata_repair",
            next_node="file_metadata_repair",
            report="Restore the symlink-backed file first, then replan metadata repair if the recreated copy still needs a GFID/xattr fixup.",
        ),
    ),
    "file_restore": (
        DecisionEdge(
            edge_id="file_restore_to_file_metadata_repair",
            priority=10,
            when="same_subtree_has_file_metadata_repair",
            next_node="file_metadata_repair",
            report="Restore the missing replica first, then replan metadata repair if the recreated copy still needs a GFID/xattr fixup.",
        ),
    ),
    "directory_split_brain": (
        DecisionEdge(
            edge_id="directory_split_brain_to_directory_gfid_merge",
            priority=10,
            when="bounded_trees_collapse_cleanly_after_quarantine",
            next_node="directory_gfid_merge",
            report="If the directory conflict resolves to a clean majority-backed tree, promote into directory GFID merge.",
        ),
        DecisionEdge(
            edge_id="directory_split_brain_to_directory_metadata_only",
            priority=20,
            when="still_requires_manual_review_after_quarantine",
            next_node="directory_metadata_only",
            report="If the directory conflict remains unresolved, keep the remaining path at metadata-only review.",
        ),
    ),
    "directory_gfid_merge": (
        DecisionEdge(
            edge_id="directory_gfid_merge_to_directory_metadata_repair",
            priority=10,
            when="merge_collapse_leaves_metadata_drift",
            next_node="directory_metadata_repair",
            report="Once the bounded trees collapse cleanly, reattach any remaining canonical directory GFID drift.",
        ),
    ),
    "dead_gfid_reference": (
        DecisionEdge(
            edge_id="dead_gfid_to_live_reference",
            priority=10,
            when="dead_gfid_live_references_present",
            next_node="dead_gfid_reference",
            report="Follow the referenced live branch before cleanup.",
        ),
        DecisionEdge(
            edge_id="dead_gfid_reference_to_cleanup",
            priority=20,
            when="dead_gfid_chain_resolves_to_stale_only",
            next_node="dead_gfid_cleanup_direct",
            report="Once the live reference chain resolves to no manifest object, reclassify the residue as direct cleanup.",
        ),
    ),
    "dead_gfid_cleanup_direct": (
        DecisionEdge(
            edge_id="dead_gfid_cleanup_direct_to_post_resolution_tail",
            priority=10,
            when="residue_returns_after_cleanup",
            next_node="post_resolution_ghost_tail",
            report="If the residue comes back after cleanup, treat it as a post-resolution ghost tail and inspect the .glusterfs references.",
        ),
    ),
    "stale_glusterfs_index_cleanup": (
        DecisionEdge(
            edge_id="stale_index_to_rescan",
            priority=10,
            when="always",
            next_node="stale_glusterfs_index_cleanup",
            report="Clean stale bookkeeping, then rescan to confirm whether it returns.",
        ),
    ),
}


def _scenario_root(logical_path: str) -> str:
    if not logical_path or ":" in logical_path:
        return ""
    return logical_path.split("/", 1)[0]


def _action_graph_markers(action: PlanAction) -> set[str]:
    return {marker for marker in action.graph_markers if marker}


def _node_matches_action(
    node: DecisionNode,
    action: PlanAction,
    *,
    use_graph_markers: bool = True,
    use_note_markers: bool = True,
) -> bool:
    if action.action_type not in node.action_types:
        return False
    if node.strategies and action.repair_strategy and action.repair_strategy not in node.strategies:
        return False
    if use_graph_markers and node.graph_markers:
        action_markers = _action_graph_markers(action)
        if not action_markers:
            return False
        if not action_markers.intersection(node.graph_markers):
            return False
    if node.path_markers and not any(marker in action.logical_path for marker in node.path_markers):
        return False
    if use_note_markers and node.note_markers and not any(
        marker in note
        for marker in node.note_markers
        for note in action.notes
    ):
        return False
    return True


def node_for_action(action: PlanAction) -> DecisionNode | None:
    action_markers = _action_graph_markers(action)
    if action_markers:
        structured_fallback: tuple[int, DecisionNode] | None = None
        for node in _DECISION_NODES.values():
            if not _node_matches_action(node, action, use_graph_markers=True, use_note_markers=False):
                continue
            score = len(action_markers.intersection(node.graph_markers))
            if structured_fallback is None or score > structured_fallback[0]:
                structured_fallback = (score, node)
        if structured_fallback is not None:
            return structured_fallback[1]

    fallback: DecisionNode | None = None
    for node in _DECISION_NODES.values():
        if not _node_matches_action(node, action, use_graph_markers=False, use_note_markers=True):
            continue
        if node.path_markers or node.note_markers:
            return node
        if fallback is None:
            fallback = node
    return fallback


def annotate_action(action: PlanAction, related_actions: list[PlanAction] | None = None) -> PlanAction:
    node = node_for_action(action)
    if node is None:
        return action
    related_actions = related_actions or []
    action.graph_node = node.node_id
    action.matrix_key = node.matrix_key
    action.decision_class = node.decision_class
    action.provisional = node.provisional
    action.terminal_policy = node.terminal_policy
    action.rescan_after_apply = node.post_apply == "heal_rescan"
    action.graph_markers = sorted(
        {
            *action.graph_markers,
            f"action_type:{action.action_type}",
            f"repair_strategy:{action.repair_strategy}",
            f"object_type:{action.object_type}",
            f"graph_node:{node.node_id}",
            *({"file_metadata_only:review"} if node.node_id == "file_metadata_only" else set()),
            *({"file_metadata_only:fallback"} if node.node_id == "file_metadata_only_fallback" else set()),
            *({"directory_metadata_only:fallback"} if node.node_id == "directory_metadata_only_fallback" else set()),
        }
    )
    action.followup_edges = [asdict(edge) for edge in _followup_edges_for_action(action, related_actions)]
    return action


def _followup_edges_for_action(action: PlanAction, related_actions: list[PlanAction]) -> list[DecisionEdge]:
    edges = list(_FOLLOWUP_EDGE_RULES.get(action.graph_node, ()))
    if not edges and action.graph_node.startswith("file_stale_survivor"):
        edges = list(_FOLLOWUP_EDGE_RULES.get("file_stale_survivor", ()))
    if not edges and action.graph_node.startswith("dead_gfid_reference"):
        edges = list(_FOLLOWUP_EDGE_RULES.get("dead_gfid_reference", ()))
    if not edges and action.graph_node.startswith("dead_gfid_cleanup_direct"):
        edges = list(_FOLLOWUP_EDGE_RULES.get("dead_gfid_cleanup_direct", ()))
    scenario_root = _scenario_root(action.logical_path)
    if not scenario_root:
        scenario_root = ""
    related = [
        other
        for other in related_actions
        if other.logical_path != action.logical_path and _scenario_root(other.logical_path) == scenario_root
    ]
    directory_related = related + [
        other
        for other in related_actions
        if other.logical_path != action.logical_path and other.logical_path.startswith("unresolved-child:")
    ]
    related_node_ids = {
        node.node_id
        for other in related
        if (node := node_for_action(other)) is not None
    }
    directory_related_node_ids = {
        node.node_id
        for other in directory_related
        if (node := node_for_action(other)) is not None
    }
    if action.graph_node.startswith("file_stale_survivor"):
        if any(
            other.action_type in {"review_directory_children", "review_directory_metadata", "reconcile_directory"}
            or str(other.graph_node or "").startswith("directory_child_gap")
            for other in related
        ):
            return [edge for edge in edges if edge.edge_id == "file_residue_to_directory_child_gap"]
        if any(other.action_type == "review_entry_split_brain" for other in related):
            return [edge for edge in edges if edge.edge_id == "file_residue_to_file_split_brain"]
        return []
    if action.graph_node == "directory_child_gap":
        if directory_related_node_ids.intersection(
            {"directory_child_reference", "directory_child_reference_live", "directory_child_reference_immediate_missing"}
        ):
            return [edge for edge in edges if edge.edge_id == "directory_child_gap_to_directory_child_reference"]
        if any(other.action_type == "review_probable_stale_survivor" for other in directory_related):
            return list(edges)
        return []
    if action.graph_node == "directory_restore_backend":
        if "directory_child_gap" in directory_related_node_ids or directory_related_node_ids.intersection(
            {"directory_child_reference", "directory_child_reference_live", "directory_child_reference_immediate_missing"}
        ):
            return list(edges)
        return []
    if action.graph_node in {"directory_metadata_only", "directory_metadata_only_fallback"}:
        if directory_related_node_ids.intersection(
            {"directory_child_reference", "directory_child_reference_live", "directory_child_reference_immediate_missing"}
        ):
            return [edge for edge in edges if edge.edge_id == "directory_metadata_to_directory_child_reference"]
        if (
            "directory_child_gap" in related_node_ids
            or "directory_child_gap" in directory_related_node_ids
            or any(
                other.action_type in {"review_directory_children", "reconcile_directory"}
                or str(other.graph_node or "").startswith("directory_child_gap")
                for other in directory_related
            )
            ):
            return [edge for edge in edges if edge.edge_id == "directory_metadata_to_directory_child_gap"]
        if "directory_metadata_repair" in related_node_ids:
            return [edge for edge in edges if edge.edge_id == "directory_metadata_to_directory_metadata_repair"]
        note_text = " ".join(action.notes).lower()
        if (
            "children must be repaired before parent directory" in note_text
            or "directory child-set reconciliation needed across hosts" in note_text
            or "follow child-chain repair" in note_text
            or "follow the directory child chain first" in note_text
        ):
            if "live chain target" in note_text or "follow the live child reference" in note_text:
                return [edge for edge in _FOLLOWUP_EDGE_RULES["directory_metadata_only"] if edge.edge_id == "directory_metadata_to_directory_child_reference"]
            return [edge for edge in _FOLLOWUP_EDGE_RULES["directory_metadata_only"] if edge.edge_id == "directory_metadata_to_directory_child_gap"]
        return []
    if action.graph_node in {"file_metadata_only", "file_metadata_only_fallback"}:
        if "file_metadata_repair" in related_node_ids:
            return [edge for edge in edges if edge.edge_id == "file_metadata_to_file_metadata_repair"]
        return []
    if action.graph_node == "directory_split_brain":
        if "directory_gfid_merge" in related_node_ids:
            return [edge for edge in edges if edge.edge_id == "directory_split_brain_to_directory_gfid_merge"]
        if "directory_metadata_only" in related_node_ids:
            return [edge for edge in edges if edge.edge_id == "directory_split_brain_to_directory_metadata_only"]
        return []
    if action.graph_node == "directory_gfid_merge":
        if "directory_metadata_repair" in related_node_ids:
            return [edge for edge in edges if edge.edge_id == "directory_gfid_merge_to_directory_metadata_repair"]
        return []
    if action.graph_node == "file_split_brain_majority":
        if "file_metadata_repair" in related_node_ids:
            return list(edges)
        return []
    if action.graph_node == "file_split_brain_resolver_first":
        if "file_metadata_repair" in related_node_ids:
            return list(edges)
        return []
    if action.graph_node in {"file_split_brain_tie", "file_entry_split_brain_tie"}:
        if "file_metadata_only" in related_node_ids:
            return list(edges)
        return []
    if action.graph_node == "file_symlink_restore":
        if "file_metadata_repair" in related_node_ids:
            return list(edges)
        return []
    if action.graph_node == "file_restore":
        if "file_metadata_repair" in related_node_ids:
            return list(edges)
        return []
    if action.graph_node.startswith("dead_gfid_reference"):
        if action.dead_gfid_live_references:
            if any(
                other.action_type == "cleanup_dead_gfid"
                or str(other.graph_node or "").startswith("dead_gfid_cleanup")
                for other in related
            ):
                return [edge for edge in edges if edge.edge_id == "dead_gfid_reference_to_cleanup"]
            return [edge for edge in edges if edge.edge_id == "dead_gfid_to_live_reference"]
        return []
    if action.graph_node == "dead_gfid_cleanup_direct":
        if "post_resolution_ghost_tail" in directory_related_node_ids:
            return [edge for edge in edges if edge.edge_id == "dead_gfid_cleanup_direct_to_post_resolution_tail"]
        return []
    if action.graph_node == "stale_glusterfs_index_cleanup":
        return edges
    return edges


def annotate_plan(actions: list[PlanAction]) -> list[PlanAction]:
    for action in actions:
        annotate_action(action, actions)
        if action.decision_class:
            continue
        previous_action_type = action.action_type
        previous_strategy = action.repair_strategy
        action.graph_node = "unclassified_authority_review"
        action.matrix_key = ""
        action.decision_class = "operator_only"
        action.provisional = False
        action.terminal_policy = "operator_decision"
        action.rescan_after_apply = False
        if action.action_type.startswith("review_") or action.action_type.startswith("defer_"):
            action.notes.append(
                "planner authority was not classified; preserve this existing review card until an explicit decision closes the evidence gap"
            )
            continue
        action.action_type = "review_unclassified_authority"
        action.repair_strategy = "collect_unclassified_authority_evidence"
        action.notes.append(
            "planner authority was not classified; no write plan is allowed until an explicit review closes this evidence gap"
        )
        action.notes.append(
            f"unclassified candidate preserved for diagnosis: {previous_action_type} / {previous_strategy or 'no strategy'}"
        )
    return actions


def summarize_graph(actions: list[PlanAction]) -> dict[str, object]:
    graph_nodes: dict[str, int] = {}
    provisional = 0
    rescan = 0
    edges = 0
    for action in actions:
        if action.graph_node:
            graph_nodes[action.graph_node] = graph_nodes.get(action.graph_node, 0) + 1
        if action.provisional:
            provisional += 1
        if action.rescan_after_apply:
            rescan += 1
        edges += len(action.followup_edges)
    return {
        "graph_nodes": graph_nodes,
        "graph_provisional_actions": provisional,
        "graph_rescan_actions": rescan,
        "graph_followup_edges": edges,
    }


def render_graph_report(plan_payload: dict[str, Any]) -> str:
    actions = plan_payload.get("actions", [])
    if not isinstance(actions, list):
        return "graph: unavailable"
    graph_cycle = plan_payload.get("graph_cycle")
    counts: dict[str, int] = {}
    provisional = 0
    rescan = 0
    followups = 0
    edge_reports: list[str] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        node = str(item.get("graph_node") or "")
        if node:
            counts[node] = counts.get(node, 0) + 1
        if item.get("provisional"):
            provisional += 1
        if item.get("rescan_after_apply"):
            rescan += 1
        for edge in item.get("followup_edges") or []:
            if not isinstance(edge, dict):
                continue
            followups += 1
            edge_id = str(edge.get("edge_id") or "")
            report = str(edge.get("report") or "")
            if edge_id and report:
                edge_reports.append(f"{edge_id}: {report}")
    if not counts and not provisional and not rescan and not followups:
        report = "graph: none"
        if isinstance(graph_cycle, dict) and graph_cycle:
            report += "\n" + render_graph_cycle_report(graph_cycle)
        return report
    nodes = ",".join(f"{node}={count}" for node, count in sorted(counts.items()))
    parts = [f"provisional={provisional}", f"rescan={rescan}", f"followups={followups}"]
    if nodes:
        parts.append(f"nodes={nodes}")
    if edge_reports:
        parts.append("edges=" + " | ".join(edge_reports))
    report = "graph: " + ", ".join(parts)
    if isinstance(graph_cycle, dict) and graph_cycle:
        report += "\n" + render_graph_cycle_report(graph_cycle)
    return report


def action_signature(action: PlanAction) -> tuple[object, ...]:
    return (
        action.logical_path,
        action.action_id,
        action.action_type,
        action.repair_strategy,
        action.graph_node,
        action.matrix_key,
        action.provisional,
        action.terminal_policy,
        action.rescan_after_apply,
    )


def plan_signature(actions: list[PlanAction]) -> tuple[tuple[object, ...], ...]:
    return tuple(action_signature(action) for action in actions)


def assess_graph_cycle(
    previous_signatures: list[tuple[tuple[object, ...], ...]],
    current_signature: tuple[tuple[object, ...], ...],
    *,
    max_repeats: int = 2,
) -> dict[str, object]:
    recent_matches = 0
    for signature in reversed(previous_signatures):
        if signature != current_signature:
            break
        recent_matches += 1
    cycle_detected = recent_matches >= max_repeats
    return {
        "repeat_count": recent_matches,
        "cycle_detected": cycle_detected,
        "stop_reason": "graph signature repeated" if cycle_detected else "",
    }


def render_graph_cycle_report(cycle_result: dict[str, object]) -> str:
    return (
        "graph-cycle: "
        f"repeat_count={cycle_result.get('repeat_count', 0)}, "
        f"cycle_detected={str(bool(cycle_result.get('cycle_detected'))).lower()}"
        + (
            f", stop_reason={cycle_result.get('stop_reason')}"
            if cycle_result.get("stop_reason")
            else ""
        )
    )


def _signature_from_status(value: object) -> tuple[tuple[object, ...], ...]:
    signature: list[tuple[object, ...]] = []
    if not isinstance(value, (list, tuple)):
        return tuple(signature)
    for item in value:
        if isinstance(item, (list, tuple)):
            signature.append(tuple(item))
        else:
            signature.append((item,))
    return tuple(signature)


def assess_graph_cycle_status(
    previous_status: dict[str, object],
    current_actions: list[PlanAction],
    *,
    max_repeats: int = 2,
) -> dict[str, object]:
    current_signature = plan_signature(current_actions)
    previous_graph = previous_status.get("graph_cycle") or {}
    previous_signature = _signature_from_status(previous_graph.get("signature"))
    previous_history = list(_signature_from_status(previous_graph.get("history")))
    if previous_signature:
        previous_history.append(previous_signature)
    cycle_result = assess_graph_cycle(previous_history, current_signature, max_repeats=max_repeats)
    cycle_result["signature"] = current_signature
    cycle_result["history"] = previous_history[-max_repeats:]
    return cycle_result


def build_graph_cycle_baseline(actions: list[PlanAction]) -> dict[str, object]:
    return {
        "signature": plan_signature(actions),
        "history": [],
    }


def build_graph_cycle_baseline_status(actions: list[PlanAction]) -> dict[str, object]:
    baseline = build_graph_cycle_baseline(actions)
    return {
        "graph_cycle": baseline,
        "graph_cycle_report": render_graph_cycle_report(
            {
                "repeat_count": 0,
                "cycle_detected": False,
                "signature": baseline["signature"],
                "history": baseline["history"],
            }
        ),
    }


def build_graph_cycle_status(
    previous_status: dict[str, object],
    current_actions: list[PlanAction],
    *,
    max_repeats: int = 2,
) -> dict[str, object]:
    cycle_result = assess_graph_cycle_status(
        previous_status,
        current_actions,
        max_repeats=max_repeats,
    )
    return {
        "graph_cycle": cycle_result,
        "graph_cycle_report": render_graph_cycle_report(cycle_result),
    }


def render_graph_cycle_status_report(status: dict[str, object]) -> str:
    stored_report = str(status.get("graph_cycle_report") or "").strip()
    if stored_report:
        return stored_report
    graph_cycle = status.get("graph_cycle") or {}
    if not isinstance(graph_cycle, dict) or not graph_cycle:
        return "graph-cycle: unavailable"
    return render_graph_cycle_report(graph_cycle)


def summarize_graph_cycle_status(status: dict[str, object]) -> dict[str, object]:
    graph_cycle = status.get("graph_cycle") or {}
    if not isinstance(graph_cycle, dict):
        graph_cycle = {}
    return {
        "graph_cycle_present": bool(graph_cycle),
        "graph_cycle_report": render_graph_cycle_status_report(status),
    }


def render_graph_cycle_status_summary(status: dict[str, object]) -> str:
    summary = summarize_graph_cycle_status(status)
    present = "yes" if summary.get("graph_cycle_present") else "no"
    return f"graph-cycle-status: present={present}"
