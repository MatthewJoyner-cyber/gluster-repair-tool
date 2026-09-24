# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for planner decision-graph metadata."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from gluster_heal_tool.models import PlanAction
from gluster_heal_tool import cli as package_cli
from gluster_heal_tool.planner_payload import load_plan_actions
from gluster_heal_tool.planner import build_plan, render_plan_summary, summarize_plan
from gluster_heal_tool.planner_graph import (
    action_signature,
    annotate_action,
    annotate_plan,
    assess_graph_cycle,
    build_graph_cycle_status,
    build_graph_cycle_baseline,
    build_graph_cycle_baseline_status,
    assess_graph_cycle_status,
    node_for_action,
    plan_signature,
    render_graph_cycle_report,
    render_graph_cycle_status_report,
    render_graph_cycle_status_summary,
    render_graph_report,
    summarize_graph,
)
from gluster_heal_tool.graph_audit import render_graph_audit_report
from tests.test_replica_topology import _directory_manifest, _file_manifest


def _load_manager_main():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module.main


class PlannerGraphTests(unittest.TestCase):
    def test_node_for_action_maps_known_provisional_file(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=1,
            repair_strategy="delete_below_quorum_file",
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_stale_survivor", node.node_id)
        self.assertEqual("review_probable_stale_survivor", node.matrix_key)

    def test_annotate_plan_converts_unclassified_authority_to_review_card(self) -> None:
        action = PlanAction(
            action_id="repair:unclassified",
            logical_path="unclassified",
            action_type="repair_unknown",
            object_type="unknown",
            depth=1,
            repair_strategy="unclassified_candidate",
        )

        annotate_plan([action])

        self.assertEqual("review_unclassified_authority", action.action_type)
        self.assertEqual("collect_unclassified_authority_evidence", action.repair_strategy)
        self.assertEqual("operator_only", action.decision_class)
        self.assertEqual("unclassified_authority_review", action.graph_node)
        self.assertIn("no write plan is allowed", "\n".join(action.notes))

    def test_node_for_action_prefers_stale_survivor_mount_visible_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=1,
            repair_strategy="delete_below_quorum_file",
            graph_markers=["mount:visible"],
            notes=[
                "file is not accessible through the Gluster mount; likely stale survivor after a failed delete",
                "file is still mount-accessible, but below-quorum survivors should default toward delete review, not recreate",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_stale_survivor_mount_visible", node.node_id)
        self.assertEqual("review_probable_stale_survivor_visible", node.matrix_key)

    def test_node_for_action_prefers_stale_survivor_mount_hidden_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=1,
            repair_strategy="delete_below_quorum_file",
            graph_markers=["mount:hidden"],
            notes=[
                "file is not accessible through the Gluster mount; likely stale survivor after a failed delete",
                "file is still mount-accessible, but below-quorum survivors should default toward delete review, not recreate",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_stale_survivor_mount_hidden", node.node_id)
        self.assertEqual("review_probable_stale_survivor_hidden", node.matrix_key)

    def test_node_for_action_prefers_stale_survivor_marker_over_conflicting_notes(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=1,
            repair_strategy="delete_below_quorum_file",
            graph_markers=["mount:visible"],
            notes=[
                "file is not accessible through the Gluster mount; likely stale survivor after a failed delete",
                "default batch policy should lean delete of the stale backend file and file/GFID metadata",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_stale_survivor_mount_visible", node.node_id)
        self.assertEqual("review_probable_stale_survivor_visible", node.matrix_key)

    def test_annotate_action_sets_graph_metadata(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
        )

        annotate_action(action)

        self.assertEqual("dead_gfid_cleanup", action.graph_node)
        self.assertEqual("cleanup_dead_gfid", action.matrix_key)
        self.assertEqual("ready_cleanup", action.decision_class)
        self.assertFalse(action.provisional)
        self.assertFalse(action.rescan_after_apply)
        self.assertIn("graph_node:dead_gfid_cleanup", action.graph_markers)
        self.assertIn("action_type:cleanup_dead_gfid", action.graph_markers)
        self.assertIn("repair_strategy:delete_dead_gfid_residue", action.graph_markers)

    def test_node_for_action_prefers_handle_ghost_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            notes=["regular .glusterfs handle ghost detected; safe to prune once its backlink no longer resolves"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_handle_ghost_cleanup", node.node_id)
        self.assertEqual("file_handle_ghost_cleanup", node.matrix_key)

    def test_node_for_action_prefers_handle_ghost_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            graph_markers=["dead_gfid:handle_ghost"],
            notes=["post-resolution ghost tail"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_handle_ghost_cleanup", node.node_id)
        self.assertEqual("file_handle_ghost_cleanup", node.matrix_key)

    def test_node_for_action_prefers_orphaned_symlink_mount_visible_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/symlink",
            action_type="review_probable_orphaned_symlink",
            object_type="file",
            depth=1,
            repair_strategy="delete_orphaned_symlink_residue",
            graph_markers=["mount:visible"],
            notes=[
                "symlink is still mount-accessible, but below-quorum residues should default toward delete review, not recreate",
                "default batch policy should lean delete of the orphaned symlink backend and symlink/GFID metadata",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_orphaned_symlink_mount_visible", node.node_id)
        self.assertEqual("review_probable_orphaned_symlink_visible", node.matrix_key)

    def test_node_for_action_prefers_orphaned_symlink_mount_hidden_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/symlink",
            action_type="review_probable_orphaned_symlink",
            object_type="file",
            depth=1,
            repair_strategy="delete_orphaned_symlink_residue",
            graph_markers=["mount:hidden"],
            notes=[
                "symlink is not accessible through the Gluster mount; likely orphaned residue after a failed delete",
                "default batch policy should lean delete of the orphaned symlink backend and symlink/GFID metadata",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_orphaned_symlink_mount_hidden", node.node_id)
        self.assertEqual("review_probable_orphaned_symlink_hidden", node.matrix_key)

    def test_node_for_action_prefers_symlink_restore_marker_over_generic_restore(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/symlink",
            action_type="repair_file",
            object_type="file",
            depth=1,
            repair_strategy="restore_missing_replica",
            graph_markers=["file_subtype:symlink"],
            notes=[
                "file subtype: symlink",
                "delete the logical file through the mount if needed, then recreate it once from staged /tmp data to refill missing replicas",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_symlink_restore", node.node_id)
        self.assertEqual("file_symlink", node.matrix_key)

    def test_node_for_action_prefers_file_symlink_restore_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/symlink",
            action_type="repair_file",
            object_type="file",
            depth=1,
            repair_strategy="restore_missing_replica",
            notes=[
                "file subtype: symlink",
                "delete the logical file through the mount if needed, then recreate it once from staged /tmp data to refill missing replicas",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_symlink_restore", node.node_id)
        self.assertEqual("file_symlink", node.matrix_key)

    def test_node_for_action_prefers_file_restore_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/file",
            action_type="repair_file",
            object_type="file",
            depth=1,
            repair_strategy="restore_missing_replica",
            notes=[
                "stage winning copy locally",
                "remove only stale metadata on missing replicas",
                "delete the logical file through the mount if needed, then recreate it once from staged /tmp data to refill missing replicas",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_restore", node.node_id)
        self.assertEqual("file_restore", node.matrix_key)

    def test_node_for_action_prefers_file_child_gap_restore_strategy(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/file",
            action_type="repair_file",
            object_type="file",
            depth=1,
            repair_strategy="restore_missing_child_replica",
            graph_markers=["file_child_gap:present", "file_restore:review"],
            notes=[
                "stage winning copy locally",
                "remove only stale metadata on missing replicas",
                "file-child-gap marker present; keep the restore branch explicit so the child-gap variant can be tested and reasoned about separately",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_restore", node.node_id)
        self.assertEqual("file_restore", node.matrix_key)

    def test_node_for_action_prefers_file_presence_tie_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/file",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=2,
            repair_strategy="ambiguous_file_presence_tie",
            graph_markers=["file_presence_tie:review"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_presence_tie", node.node_id)
        self.assertEqual("file_presence_tie", node.matrix_key)

    def test_node_for_action_prefers_directory_presence_tie_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/directory",
            action_type="review_directory_gfid_conflict",
            object_type="directory",
            depth=2,
            repair_strategy="ambiguous_directory_presence_tie",
            graph_markers=["directory_presence_tie:review"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_presence_tie", node.node_id)
        self.assertEqual("directory_presence_tie", node.matrix_key)

    def test_node_for_action_prefers_immediate_missing_child_reference(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="unresolved-child:parent-gfid/gamma",
            action_type="review_directory_children",
            object_type="unknown",
            depth=2,
            repair_strategy="verify_directory_child_absence",
            graph_markers=["directory_child_reference:immediate_missing"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_reference_immediate_missing", node.node_id)
        self.assertEqual("directory_child_reference", node.matrix_key)

    def test_node_for_action_prefers_type_mismatch_review_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="review_type_mismatch",
            object_type="type_mismatch",
            depth=1,
            notes=["synthetic type mismatch case"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("type_mismatch_review", node.node_id)
        self.assertEqual("review_type_mismatch", node.matrix_key)

    def test_node_for_action_prefers_file_delete_below_quorum_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/file",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=1,
            repair_strategy="delete_below_quorum_file",
            notes=[
                "file is present on only 1 of 4 replicas; below quorum threshold 3",
                "default batch policy should lean delete of the stale backend file and file/GFID metadata",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_delete_below_quorum", node.node_id)
        self.assertEqual("file_delete_below_quorum", node.matrix_key)

    def test_node_for_action_prefers_file_metadata_only_fallback_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/file",
            action_type="review_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="review_metadata_only",
            notes=["Fallback only after file-family, symlink-chain, split-brain, and stale-survivor checks are exhausted."],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_metadata_only_fallback", node.node_id)
        self.assertEqual("file_metadata_only", node.matrix_key)

    def test_node_for_action_prefers_file_metadata_only_fallback_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/file",
            action_type="review_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="review_metadata_only",
            graph_markers=["file_metadata_only:fallback"],
            notes=["no missing or conflicting backend file found; only metadata review remains"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_metadata_only_fallback", node.node_id)
        self.assertEqual("file_metadata_only", node.matrix_key)

    def test_node_for_action_prefers_directory_delete_below_quorum_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/dir",
            action_type="review_probable_stale_survivor",
            object_type="directory",
            depth=1,
            repair_strategy="delete_below_quorum_subtree",
            notes=[
                "directory is still mount-accessible, but below-quorum survivors should default toward subtree-delete review, not recreate",
                "default batch policy should lean delete of the stale subtree deepest-first, then parent last",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_delete_below_quorum", node.node_id)
        self.assertEqual("directory_delete_below_quorum", node.matrix_key)

    def test_node_for_action_prefers_post_resolution_ghost_tail_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/child",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=2,
            repair_strategy="delete_dead_gfid_residue",
            notes=["nested GFID-child residue does not resolve to a live directory chain target; treat it as a post-resolution ghost tail that is safe to prune once no live manifest object remains"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("post_resolution_ghost_tail", node.node_id)
        self.assertEqual("post_resolution_ghost_tail", node.matrix_key)

    def test_node_for_action_prefers_post_resolution_ghost_tail_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/child",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=2,
            repair_strategy="delete_dead_gfid_residue",
            graph_markers=["dead_gfid:post_resolution_tail"],
            notes=["regular .glusterfs handle ghost"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("post_resolution_ghost_tail", node.node_id)
        self.assertEqual("post_resolution_ghost_tail", node.matrix_key)

    def test_node_for_action_prefers_post_resolution_ghost_tail_graph_marker_over_notes(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example/child",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=2,
            repair_strategy="delete_dead_gfid_residue",
            graph_markers=["dead_gfid:post_resolution_tail"],
            notes=["regular .glusterfs handle ghost"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("post_resolution_ghost_tail", node.node_id)
        self.assertEqual("post_resolution_ghost_tail", node.matrix_key)

    def test_node_for_action_prefers_dead_gfid_cleanup_direct_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="cleanup_dead_gfid",
            object_type="dead_gfid",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            notes=[
                "unknown object carries stale GFID residue but no live reference chain; cleanup is safe once the residue is unreferenced on all replicas",
                "final-step-only",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("dead_gfid_cleanup_direct", node.node_id)
        self.assertEqual("dead_gfid_cleanup_direct", node.matrix_key)

    def test_node_for_action_prefers_dead_gfid_cleanup_direct_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="example",
            action_type="cleanup_dead_gfid",
            object_type="dead_gfid",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            graph_markers=["dead_gfid:cleanup_direct"],
            notes=[
                "unknown object carries stale GFID residue but no live reference chain; cleanup is safe once the residue is unreferenced on all replicas",
                "dead GFID reference chain resolves to no live manifest object; cleanup is safe once no live backend reference remains",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("dead_gfid_cleanup_direct", node.node_id)
        self.assertEqual("dead_gfid_cleanup_direct", node.matrix_key)

    def test_dead_gfid_followup_edges_chain_into_cleanup(self) -> None:
        reference = PlanAction(
            action_id="repair:ref",
            logical_path="dead-gfid-live",
            action_type="review_dead_gfid_reference",
            object_type="unknown",
            depth=1,
            repair_strategy="follow_live_reference",
            dead_gfid_live_references=["/live/path"],
            notes=[
                "dead GFID still resolves to live path reference(s): /live/path",
                "resolved live reference chain: /live/path -> /live/path (file)",
                "inspect the referenced live path before deciding whether the GFID residue is stale or part of a live conflict",
            ],
        )
        cleanup = PlanAction(
            action_id="repair:cleanup",
            logical_path="dead-gfid-live/cleanup",
            action_type="cleanup_dead_gfid",
            object_type="dead_gfid",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            notes=[
                "unknown object carries stale GFID residue but no live reference chain; cleanup is safe once the residue is unreferenced on all replicas",
                "final-step-only",
            ],
        )
        tail = PlanAction(
            action_id="repair:tail",
            logical_path="dead-gfid-live/tail",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            notes=["post-resolution ghost tail"],
        )

        annotate_action(reference, [reference, cleanup, tail])
        annotate_action(cleanup, [reference, cleanup, tail])
        annotate_action(tail, [reference, cleanup, tail])

        self.assertEqual("dead_gfid_reference_live", reference.graph_node)
        self.assertTrue(reference.followup_edges)
        self.assertEqual("dead_gfid_reference_to_cleanup", reference.followup_edges[0]["edge_id"])
        self.assertEqual("dead_gfid_cleanup_direct", reference.followup_edges[0]["next_node"])
        self.assertEqual("dead_gfid_cleanup_direct", cleanup.graph_node)
        self.assertTrue(cleanup.followup_edges)
        self.assertEqual("dead_gfid_cleanup_direct_to_post_resolution_tail", cleanup.followup_edges[0]["edge_id"])
        self.assertEqual("post_resolution_ghost_tail", cleanup.followup_edges[0]["next_node"])
        self.assertEqual("post_resolution_ghost_tail", tail.graph_node)

    def test_node_for_action_prefers_dead_gfid_live_reference_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dead-gfid",
            action_type="review_dead_gfid_reference",
            object_type="unknown",
            depth=1,
            repair_strategy="follow_live_reference",
            graph_markers=["dead_gfid:live_reference"],
            notes=[
                "dead GFID still resolves to live path reference(s): /live/path",
                "inspect the referenced live path before cleanup",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("dead_gfid_reference_live", node.node_id)
        self.assertEqual("dead_gfid_reference", node.matrix_key)

    def test_node_for_action_prefers_directory_merge_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="attach_directory_gfid",
            notes=[
                "same logical directory name appears with multiple GFIDs, but the bounded trees collapse cleanly",
                "merge can promote to the existing attach_directory_gfid path without recreating the directory tree",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_gfid_merge", node.node_id)
        self.assertEqual("directory_gfid_merge", node.matrix_key)

    def test_node_for_action_prefers_directory_child_gap_executable_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="reconcile_directory",
            object_type="directory",
            depth=1,
            repair_strategy="reconcile_directory_children",
            graph_markers=["directory_child_gap:executable"],
            notes=["child-gap dependency closure is not executable"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_gap_executable", node.node_id)
        self.assertEqual("review_directory_children_executable", node.matrix_key)

    def test_node_for_action_prefers_directory_child_gap_executable_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="reconcile_directory",
            object_type="directory",
            depth=1,
            repair_strategy="reconcile_directory_children",
            notes=["child-gap dependency closure is executable", "wait_for_child_repairs"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_gap_executable", node.node_id)
        self.assertEqual("review_directory_children_executable", node.matrix_key)

    def test_node_for_action_prefers_directory_child_gap_split_brain_child_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="reconcile_directory",
            object_type="directory",
            depth=1,
            repair_strategy="reconcile_directory_children",
            graph_markers=["directory_child_gap:split_brain_child"],
            notes=["child-gap dependency closure is executable"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_gap_split_brain_child", node.node_id)
        self.assertEqual("review_directory_children_split_brain_child", node.matrix_key)

    def test_node_for_action_prefers_directory_child_gap_split_brain_child_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="reconcile_directory",
            object_type="directory",
            depth=1,
            repair_strategy="reconcile_directory_children",
            notes=["synthetic child-gap with split-brain child", "wait_for_child_repairs"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_gap_split_brain_child", node.node_id)
        self.assertEqual("review_directory_children_split_brain_child", node.matrix_key)

    def test_node_for_action_prefers_directory_child_reference_live_graph_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="unresolved-child:parent-gfid/alpha/payload.txt",
            action_type="review_directory_children",
            object_type="unknown",
            depth=2,
            repair_strategy="reconcile_directory_children",
            graph_markers=["directory_child_reference:live"],
            notes=["nested GFID-child residue does not resolve to a live directory chain target; treat it as cleanup"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_reference_live", node.node_id)
        self.assertEqual("directory_child_reference", node.matrix_key)

    def test_node_for_action_prefers_directory_child_reference_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="unresolved-child:parent-gfid/alpha/payload.txt",
            action_type="review_directory_children",
            object_type="unknown",
            depth=2,
            repair_strategy="reconcile_directory_children",
            notes=[
                "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup",
                "live chain targets: /dir/child",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_child_reference_live", node.node_id)
        self.assertEqual("directory_child_reference", node.matrix_key)

    def test_node_for_action_prefers_directory_split_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="review_directory_gfid_conflict",
            object_type="directory",
            depth=1,
            repair_strategy="rename_conflicting_directory",
            notes=[
                "same logical directory name appears with multiple GFIDs; quarantine the losing backend entry before choosing a canonical directory",
                "do not auto-merge or auto-delete directory conflicts in place",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_split_brain", node.node_id)
        self.assertEqual("directory_split_brain", node.matrix_key)

    def test_build_plan_emits_directory_child_gap_graph_markers(self) -> None:
        from gluster_heal_tool.planner import build_plan

        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        dir_manifest = _directory_manifest(hosts, set(hosts), "replica-4/mixed/child-dir")
        for host, observations in dir_manifest["replica-4/mixed/child-dir"].observations.items():
            observations[0].backend_child_names = ["alpha"] if host != hosts[-1] else []

        plan = build_plan(dir_manifest, mountpoint="/testvol")
        directory_action = next(action for action in plan if action.logical_path.endswith("child-dir"))

        self.assertIn("directory_child_gap:present", directory_action.graph_markers)
        self.assertNotIn("directory_child_gap:executable", directory_action.graph_markers)
        self.assertEqual("directory_child_gap", directory_action.graph_node)
        self.assertIn(
            "collect immediate --gfid-child",
            " ".join(directory_action.notes),
        )

    def test_directory_split_brain_followup_edges_chain_into_merge_or_review(self) -> None:
        split = PlanAction(
            action_id="repair:split",
            logical_path="dir",
            action_type="review_directory_gfid_conflict",
            object_type="directory",
            depth=1,
            repair_strategy="rename_conflicting_directory",
            notes=[
                "same logical directory name appears with multiple GFIDs; quarantine the losing backend entry before choosing a canonical directory",
                "do not auto-merge or auto-delete directory conflicts in place",
            ],
        )
        merge = PlanAction(
            action_id="repair:merge",
            logical_path="dir/merge",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="attach_directory_gfid",
            notes=[
                "same logical directory name appears with multiple GFIDs, but the bounded trees collapse cleanly",
                "merge can promote to the existing attach_directory_gfid path without recreating the directory tree",
            ],
        )
        metadata_repair = PlanAction(
            action_id="repair:metadata",
            logical_path="dir/metadata",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="attach_directory_gfid",
            notes=[
                "reattach the canonical directory GFID xattr to the existing backend directory",
                "this repairs metadata drift without recreating the directory tree",
            ],
        )
        review = PlanAction(
            action_id="repair:review",
            logical_path="dir/review",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="review_directory_state",
            notes=["no automatic directory metadata action"],
        )

        annotate_action(split, [split, merge, metadata_repair, review])
        annotate_action(merge, [split, merge, metadata_repair, review])
        annotate_action(metadata_repair, [split, merge, metadata_repair, review])
        annotate_action(review, [split, merge, metadata_repair, review])

        split_only = PlanAction(
            action_id="repair:split-only",
            logical_path="dir-only",
            action_type="review_directory_gfid_conflict",
            object_type="directory",
            depth=1,
            repair_strategy="rename_conflicting_directory",
            notes=[
                "same logical directory name appears with multiple GFIDs; quarantine the losing backend entry before choosing a canonical directory",
                "do not auto-merge or auto-delete directory conflicts in place",
            ],
        )
        review_only = PlanAction(
            action_id="repair:review-only",
            logical_path="dir-only/review",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="review_directory_state",
            notes=["no automatic directory metadata action"],
        )
        annotate_action(split_only, [split_only, review_only])
        annotate_action(review_only, [split_only, review_only])

        self.assertEqual("directory_split_brain", split.graph_node)
        self.assertTrue(split.followup_edges)
        self.assertEqual("directory_split_brain_to_directory_gfid_merge", split.followup_edges[0]["edge_id"])
        self.assertEqual("directory_gfid_merge", split.followup_edges[0]["next_node"])
        self.assertEqual("directory_gfid_merge", merge.graph_node)
        self.assertTrue(merge.followup_edges)
        self.assertEqual("directory_gfid_merge_to_directory_metadata_repair", merge.followup_edges[0]["edge_id"])
        self.assertEqual("directory_metadata_repair", merge.followup_edges[0]["next_node"])
        self.assertEqual("directory_metadata_repair", metadata_repair.graph_node)
        self.assertEqual("directory_split_brain", split_only.graph_node)
        self.assertTrue(split_only.followup_edges)
        self.assertEqual("directory_split_brain_to_directory_metadata_only", split_only.followup_edges[0]["edge_id"])
        self.assertEqual("directory_metadata_only", split_only.followup_edges[0]["next_node"])

    def test_node_for_action_prefers_directory_metadata_repair_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="attach_directory_gfid",
            notes=[
                "reattach the canonical directory GFID xattr to the existing backend directory",
                "this repairs metadata drift without recreating the directory tree",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_metadata_repair", node.node_id)
        self.assertEqual("directory_metadata_repair", node.matrix_key)

    def test_node_for_action_prefers_directory_metadata_only_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="review_directory_state",
            notes=["no automatic directory metadata action"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_metadata_only", node.node_id)
        self.assertEqual("directory_metadata_only", node.matrix_key)

    def test_node_for_action_prefers_directory_metadata_only_fallback_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="review_directory_state",
            notes=["no missing directory or stale metadata found; review only"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_metadata_only_fallback", node.node_id)
        self.assertEqual("directory_metadata_only", node.matrix_key)

    def test_directory_metadata_review_followup_edges_chain_to_metadata_repair(self) -> None:
        metadata_review = PlanAction(
            action_id="repair:metadata-review",
            logical_path="replica-4/mixed/dir/review",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="review_directory_state",
            graph_markers=["directory_metadata_only:fallback"],
            notes=["no missing directory or stale metadata found; review only"],
        )
        metadata_repair = PlanAction(
            action_id="repair:metadata-repair",
            logical_path="replica-4/mixed/dir/metadata",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="attach_directory_gfid",
            notes=[
                "reattach the canonical directory GFID xattr to the existing backend directory",
                "this repairs metadata drift without recreating the directory tree",
            ],
        )

        annotate_action(metadata_review, [metadata_review, metadata_repair])
        annotate_action(metadata_repair, [metadata_review, metadata_repair])

        self.assertEqual("directory_metadata_only_fallback", metadata_review.graph_node)
        self.assertTrue(metadata_review.followup_edges)
        self.assertEqual("directory_metadata_to_directory_metadata_repair", metadata_review.followup_edges[0]["edge_id"])
        self.assertEqual("directory_metadata_repair", metadata_review.followup_edges[0]["next_node"])

    def test_node_for_action_prefers_directory_restore_backend_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="recreate_missing_directory_backend",
            notes=[
                "directory missing on replicas: brick-a",
                "recreate the missing backend directory on the affected brick and restore the correct directory GFID linkage",
                "brick-side targets to recreate: brick-a",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_restore_backend", node.node_id)
        self.assertEqual("directory_restore", node.matrix_key)

    def test_node_for_action_prefers_directory_restore_child_gap_strategy(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="recreate_missing_directory_backend_child_gap",
            graph_markers=["directory_backend_child_gap:present"],
            notes=[
                "directory child-gap variant: keep the backend recreate explicit while the child repair chain is still being worked",
                "directory missing on replicas: brick-a",
                "recreate the missing backend directory on the affected brick and restore the correct directory GFID linkage",
                "brick-side targets to recreate: brick-a",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_restore_backend", node.node_id)
        self.assertEqual("directory_restore", node.matrix_key)

    def test_node_for_action_prefers_directory_restore_review_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="review_directory_presence",
            notes=["directory presence pattern does not cleanly fit recreate-vs-delete rules; review before action"],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_restore_review", node.node_id)
        self.assertEqual("directory_restore", node.matrix_key)

    def test_node_for_action_prefers_directory_restore_review_mount_error_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="review_directory_presence",
            notes=[
                "mount access errors observed: Input/output error",
                "mount returned EIO/ENOTCONN; treat this as split-brain-like or below-quorum survivor evidence even if Gluster split-brain info reports zero",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_restore_review_mount_error", node.node_id)
        self.assertEqual("directory_restore", node.matrix_key)

    def test_node_for_action_prefers_directory_entry_split_brain_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir/file",
            action_type="review_entry_split_brain",
            object_type="directory",
            depth=1,
            repair_strategy="review_entry_split_brain_directory",
            notes=[
                "interactive choices: keep for review, quarantine the losing directory tree, or skip",
                "default action: review and quarantine the losing directory tree before any subtree reconcile decision",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_entry_split_brain", node.node_id)
        self.assertEqual("review_entry_split_brain_directory", node.matrix_key)

    def test_node_for_action_prefers_directory_entry_split_brain_mount_error_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir/file",
            action_type="review_entry_split_brain",
            object_type="directory",
            depth=2,
            repair_strategy="review_entry_split_brain_directory",
            notes=[
                "directory access through the Gluster mount reports an error or remains inaccessible despite backend presence; likely entry split-brain or name/GFID conflict",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_entry_split_brain_mount_error", node.node_id)
        self.assertEqual("review_entry_split_brain_directory", node.matrix_key)

    def test_node_for_action_prefers_directory_gfid_conflict_mount_first_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dir",
            action_type="review_directory_gfid_conflict",
            object_type="directory",
            depth=1,
            repair_strategy="rename_conflicting_directory",
            notes=[
                "same path, two directory GFIDs, different contents",
                "quarantine the losing backend entry before choosing a canonical directory",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("directory_gfid_conflict_mount_first", node.node_id)
        self.assertEqual("review_directory_gfid_conflict", node.matrix_key)

    def test_node_for_action_prefers_file_split_majority_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=1,
            repair_strategy="replace_entry_split_brain_file",
            notes=[
                "file shows entry split-brain or name/GFID conflict evidence; try the official Gluster split-brain resolver first, then pick a winner, remove losing backend files and file/GFID metadata, and recreate through the mount if needed"
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_entry_split_brain_replace", node.node_id)
        self.assertEqual("review_entry_split_brain_file", node.matrix_key)

    def test_node_for_action_prefers_file_split_tie_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=1,
            repair_strategy="ambiguous_entry_split_brain_file",
            notes=[
                "file shows entry split-brain evidence, but the top file-identity cohorts are tied; do not auto-replace without checksum or explicit decision",
                "decision file should specify keep_gfid or keep_host for this path",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_entry_split_brain_tie", node.node_id)
        self.assertEqual("review_entry_split_brain_tie", node.matrix_key)

    def test_node_for_action_prefers_file_entry_split_brain_replace_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=1,
            repair_strategy="replace_entry_split_brain_file",
            notes=[
                "file shows entry split-brain or name/GFID conflict evidence; try the official Gluster split-brain resolver first, then pick a winner, remove losing backend files and file/GFID metadata, and recreate through the mount if needed",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_entry_split_brain_replace", node.node_id)
        self.assertEqual("review_entry_split_brain_file", node.matrix_key)

    def test_node_for_action_prefers_file_entry_split_brain_mount_error_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=1,
            repair_strategy="replace_entry_split_brain_file",
            notes=[
                "file access through the Gluster mount reports an error or remains inaccessible despite backend presence; likely entry split-brain or name/GFID conflict",
                "file shows entry split-brain or name/GFID conflict evidence; try the official Gluster split-brain resolver first, then pick a winner, remove losing backend files and file/GFID metadata, and recreate through the mount if needed",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_entry_split_brain_mount_error", node.node_id)
        self.assertEqual("review_entry_split_brain_file", node.matrix_key)

    def test_node_for_action_prefers_file_entry_split_brain_tie_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=1,
            repair_strategy="ambiguous_entry_split_brain_file",
            notes=[
                "file shows entry split-brain evidence, but the top file-identity cohorts are tied; do not auto-replace without checksum or explicit decision",
                "decision file should specify keep_gfid or keep_host",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_entry_split_brain_tie", node.node_id)
        self.assertEqual("review_entry_split_brain_tie", node.matrix_key)

    def test_file_split_brain_followup_edges_chain_into_metadata(self) -> None:
        majority = PlanAction(
            action_id="repair:majority",
            logical_path="replica-4/split/file-a",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=2,
            repair_strategy="replace_entry_split_brain_file",
            notes=[
                "try the official Gluster split-brain resolver first",
                "remove losing backend files and file/GFID metadata",
            ],
        )
        metadata = PlanAction(
            action_id="repair:metadata",
            logical_path="replica-4/split/file-a/meta",
            action_type="repair_file_metadata",
            object_type="file",
            depth=3,
            repair_strategy="attach_file_gfid",
            notes=[
                "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                "canonical file GFID: abcdef",
            ],
        )
        tie = PlanAction(
            action_id="repair:tie",
            logical_path="replica-4/split/file-b",
            action_type="review_entry_split_brain",
            object_type="file",
            depth=2,
            repair_strategy="ambiguous_entry_split_brain_file",
            notes=[
                "top file-identity cohorts are tied",
                "do not auto-replace without checksum or explicit decision",
                "decision file should specify keep_gfid or keep_host",
            ],
        )
        metadata_only = PlanAction(
            action_id="repair:metadata-only",
            logical_path="replica-4/split/file-b/meta",
            action_type="review_file_metadata",
            object_type="file",
            depth=3,
            repair_strategy="review_metadata_only",
            notes=[
                "no missing or conflicting backend file found; only metadata review remains",
                "Fallback only after file-family, symlink-chain, split-brain, and stale-survivor checks are exhausted.",
            ],
        )

        annotate_action(majority, [majority, metadata, tie, metadata_only])
        annotate_action(metadata, [majority, metadata, tie, metadata_only])
        annotate_action(tie, [majority, metadata, tie, metadata_only])
        annotate_action(metadata_only, [majority, metadata, tie, metadata_only])

        self.assertEqual("file_split_brain_resolver_first", majority.graph_node)
        self.assertTrue(majority.followup_edges)
        self.assertEqual("file_split_brain_majority_to_file_metadata_repair", majority.followup_edges[0]["edge_id"])
        self.assertEqual("file_metadata_repair", majority.followup_edges[0]["next_node"])
        self.assertEqual("file_entry_split_brain_tie", tie.graph_node)
        self.assertTrue(tie.followup_edges)
        self.assertEqual("file_entry_split_brain_tie_to_file_metadata_only", tie.followup_edges[0]["edge_id"])
        self.assertEqual("file_metadata_only", tie.followup_edges[0]["next_node"])

    def test_file_symlink_followup_edges_chain_into_metadata_repair(self) -> None:
        restore = PlanAction(
            action_id="repair:restore",
            logical_path="symlink-root/link",
            action_type="repair_file",
            object_type="file",
            depth=1,
            repair_strategy="restore_missing_replica",
            notes=[
                "file subtype: symlink",
                "delete the logical file through the mount if needed, then recreate it once from staged /tmp data to refill missing replicas",
            ],
        )
        metadata = PlanAction(
            action_id="repair:metadata",
            logical_path="symlink-root/link/meta",
            action_type="repair_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="attach_file_gfid",
            notes=[
                "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                "canonical file GFID: deadbeef-0000-0000-0000-000000000000",
            ],
        )

        annotate_action(restore, [restore, metadata])
        annotate_action(metadata, [restore, metadata])

        self.assertEqual("file_symlink_restore", restore.graph_node)
        self.assertTrue(restore.followup_edges)
        self.assertEqual("file_symlink_restore_to_file_metadata_repair", restore.followup_edges[0]["edge_id"])
        self.assertEqual("file_metadata_repair", restore.followup_edges[0]["next_node"])

    def test_node_for_action_prefers_file_metadata_repair_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="repair_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="attach_file_gfid",
            notes=[
                "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                "canonical file GFID: abcdef",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_metadata_repair", node.node_id)
        self.assertEqual("file_metadata_repair", node.matrix_key)

    def test_node_for_action_prefers_file_metadata_review_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="file",
            action_type="review_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="review_metadata_only",
            notes=[
                "no missing or conflicting backend file found; only metadata review remains",
                "Fallback only after file-family, symlink-chain, split-brain, and stale-survivor checks are exhausted.",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("file_metadata_only", node.node_id)
        self.assertEqual("file_metadata_only", node.matrix_key)

    def test_file_metadata_review_followup_edges_chain_to_metadata_repair(self) -> None:
        metadata_review = PlanAction(
            action_id="repair:file-metadata-review",
            logical_path="replica-4/mixed/file-a/review",
            action_type="review_file_metadata",
            object_type="file",
            depth=2,
            repair_strategy="review_metadata_only",
            graph_markers=["file_metadata_only:fallback"],
            notes=[
                "no missing or conflicting backend file found; only metadata review remains",
                "Fallback only after file-family, symlink-chain, split-brain, and stale-survivor checks are exhausted.",
            ],
        )
        metadata_repair = PlanAction(
            action_id="repair:file-metadata-repair",
            logical_path="replica-4/mixed/file-a/metadata",
            action_type="repair_file_metadata",
            object_type="file",
            depth=2,
            repair_strategy="attach_file_gfid",
            notes=[
                "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                "canonical file GFID: abcdef",
            ],
        )

        annotate_action(metadata_review, [metadata_review, metadata_repair])
        annotate_action(metadata_repair, [metadata_review, metadata_repair])

        self.assertEqual("file_metadata_only_fallback", metadata_review.graph_node)
        self.assertTrue(metadata_review.followup_edges)
        self.assertEqual("file_metadata_to_file_metadata_repair", metadata_review.followup_edges[0]["edge_id"])
        self.assertEqual("file_metadata_repair", metadata_review.followup_edges[0]["next_node"])

    def test_node_for_action_prefers_stale_index_xattrop_path(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path=".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555",
            action_type="cleanup_stale_glusterfs_index",
            object_type="stale_glusterfs_index",
            depth=3,
            repair_strategy="delete_stale_glusterfs_index_residue",
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("stale_glusterfs_xattrop_cleanup", node.node_id)
        self.assertEqual("stale_glusterfs_xattrop_cleanup", node.matrix_key)

    def test_node_for_action_prefers_stale_index_dirty_path(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path=".glusterfs/indices/dirty/aaaa1111-bbbb-2222-cccc-333344445555",
            action_type="cleanup_stale_glusterfs_index",
            object_type="stale_glusterfs_index",
            depth=3,
            repair_strategy="delete_stale_glusterfs_index_residue",
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("stale_glusterfs_dirty_cleanup", node.node_id)
        self.assertEqual("stale_glusterfs_dirty_cleanup", node.matrix_key)

    def test_node_for_action_prefers_dead_gfid_live_reference_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dead-gfid",
            action_type="review_dead_gfid_reference",
            object_type="unknown",
            depth=1,
            repair_strategy="follow_live_reference",
            notes=[
                "dead GFID still resolves to live path reference(s): /live/path",
                "resolved live reference chain: /live/path -> /live/path (file)",
                "inspect the referenced live path before deciding whether the GFID residue is stale or part of a live conflict",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("dead_gfid_reference_live", node.node_id)
        self.assertEqual("dead_gfid_reference", node.matrix_key)

    def test_node_for_action_prefers_dead_gfid_cleanup_marker(self) -> None:
        action = PlanAction(
            action_id="repair:example",
            logical_path="dead-gfid",
            action_type="cleanup_dead_gfid",
            object_type="dead_gfid",
            depth=1,
            repair_strategy="delete_dead_gfid_residue",
            notes=[
                "final-step-only",
                "confirm-unreferenced-on-all-replicas-before-delete",
                "delete the stale GFID residue after all live references are ruled out",
            ],
        )

        node = node_for_action(action)

        self.assertIsNotNone(node)
        self.assertEqual("dead_gfid_cleanup_terminal", node.node_id)
        self.assertEqual("dead_gfid_cleanup", node.matrix_key)

    def test_graph_summary_and_report_render(self) -> None:
        actions = [
            PlanAction(
                action_id="repair:one",
                logical_path="one",
                action_type="review_probable_stale_survivor",
                object_type="file",
                depth=1,
                repair_strategy="delete_below_quorum_file",
            ),
            PlanAction(
                action_id="repair:two",
                logical_path="two",
                action_type="cleanup_dead_gfid",
                object_type="unknown",
                depth=1,
                repair_strategy="delete_dead_gfid_residue",
                notes=["regular .glusterfs handle ghost detected; safe to prune once its backlink no longer resolves"],
            ),
            PlanAction(
                action_id="repair:three",
                logical_path="three/child",
                action_type="cleanup_dead_gfid",
                object_type="unknown",
                depth=2,
                repair_strategy="delete_dead_gfid_residue",
                notes=["nested GFID-child residue does not resolve to a live directory chain target; treat it as a post-resolution ghost tail that is safe to prune once no live manifest object remains"],
            ),
            PlanAction(
                action_id="repair:four",
                logical_path="dir-merge",
                action_type="repair_directory_metadata",
                object_type="directory",
                depth=1,
                repair_strategy="attach_directory_gfid",
                notes=[
                    "same logical directory name appears with multiple GFIDs, but the bounded trees collapse cleanly",
                    "merge can promote to the existing attach_directory_gfid path without recreating the directory tree",
                ],
            ),
            PlanAction(
                action_id="repair:five",
                logical_path="dir-split",
                action_type="review_directory_gfid_conflict",
                object_type="directory",
                depth=1,
                repair_strategy="rename_conflicting_directory",
                notes=[
                    "same logical directory name appears with multiple GFIDs; quarantine the losing backend entry before choosing a canonical directory",
                    "do not auto-merge or auto-delete directory conflicts in place",
                ],
            ),
            PlanAction(
                action_id="repair:six",
                logical_path="file-majority",
                action_type="review_entry_split_brain",
                object_type="file",
                depth=1,
                repair_strategy="replace_entry_split_brain_file",
                notes=[
                    "file shows entry split-brain or name/GFID conflict evidence; try the official Gluster split-brain resolver first, then pick a winner, remove losing backend files and file/GFID metadata, and recreate through the mount if needed"
                ],
            ),
            PlanAction(
                action_id="repair:seven",
                logical_path="file-tie",
                action_type="review_entry_split_brain",
                object_type="file",
                depth=1,
                repair_strategy="ambiguous_entry_split_brain_file",
                notes=[
                    "file shows entry split-brain evidence, but the top file-identity cohorts are tied; do not auto-replace without checksum or explicit decision",
                    "decision file should specify keep_gfid or keep_host for this path",
                ],
            ),
            PlanAction(
                action_id="repair:eight",
                logical_path="file-meta",
                action_type="repair_file_metadata",
                object_type="file",
                depth=1,
                repair_strategy="attach_file_gfid",
                notes=[
                    "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr",
                    "canonical file GFID: abcdef",
                ],
            ),
            PlanAction(
                action_id="repair:nine",
                logical_path="file-meta-review",
                action_type="review_file_metadata",
                object_type="file",
                depth=1,
                repair_strategy="review_metadata_only",
                notes=[
                    "no missing or conflicting backend file found; only metadata review remains",
                    "Fallback only after file-family, symlink-chain, split-brain, and stale-survivor checks are exhausted.",
                ],
            ),
            PlanAction(
                action_id="repair:ten",
                logical_path=".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555",
                action_type="cleanup_stale_glusterfs_index",
                object_type="stale_glusterfs_index",
                depth=3,
                repair_strategy="delete_stale_glusterfs_index_residue",
            ),
            PlanAction(
                action_id="repair:eleven",
                logical_path=".glusterfs/indices/dirty/ffff1111-bbbb-2222-cccc-333344445555",
                action_type="cleanup_stale_glusterfs_index",
                object_type="stale_glusterfs_index",
                depth=3,
                repair_strategy="delete_stale_glusterfs_index_residue",
            ),
            PlanAction(
                action_id="repair:twelve",
                logical_path="dead-gfid-live",
                action_type="review_dead_gfid_reference",
                object_type="unknown",
                depth=1,
                repair_strategy="follow_live_reference",
                notes=[
                    "dead GFID still resolves to live path reference(s): /live/path",
                    "resolved live reference chain: /live/path -> /live/path (file)",
                    "inspect the referenced live path before deciding whether the GFID residue is stale or part of a live conflict",
                ],
            ),
            PlanAction(
                action_id="repair:thirteen",
                logical_path="dead-gfid-cleanup",
                action_type="cleanup_dead_gfid",
                object_type="dead_gfid",
                depth=1,
                repair_strategy="delete_dead_gfid_residue",
                notes=[
                    "final-step-only",
                    "confirm-unreferenced-on-all-replicas-before-delete",
                    "delete the stale GFID residue after all live references are ruled out",
                ],
            ),
        ]
        for action in actions:
            annotate_action(action)
        actions[0].followup_edges = [
            {
                "edge_id": "file_residue_to_directory_child_gap",
                "priority": 10,
                "when": "same_subtree_has_directory_child_gap",
                "next_node": "directory_child_gap",
                "report": "Resolve file residue first, then replan the child directory from a fresh snapshot.",
            }
        ]

        summary = summarize_graph(actions)
        report = render_graph_report({"actions": [action.to_dict() for action in actions]})

        self.assertEqual(
            {
                "file_stale_survivor": 1,
                "file_handle_ghost_cleanup": 1,
                "post_resolution_ghost_tail": 1,
                "directory_gfid_merge": 1,
                "directory_split_brain": 1,
                "file_entry_split_brain_replace": 1,
                "file_entry_split_brain_tie": 1,
                "file_metadata_repair": 1,
                "file_metadata_only": 1,
                "stale_glusterfs_xattrop_cleanup": 1,
                "stale_glusterfs_dirty_cleanup": 1,
                "dead_gfid_reference_live": 1,
                "dead_gfid_cleanup_terminal": 1,
            },
            summary["graph_nodes"],
        )
        self.assertEqual(1, summary["graph_provisional_actions"])
        self.assertEqual(9, summary["graph_rescan_actions"])
        self.assertEqual(1, summary["graph_followup_edges"])
        self.assertIn("graph: provisional=1", report)
        self.assertIn("rescan=9", report)
        self.assertIn(
            "nodes=dead_gfid_cleanup_terminal=1,dead_gfid_reference_live=1,directory_gfid_merge=1,directory_split_brain=1,file_entry_split_brain_replace=1,file_entry_split_brain_tie=1,file_handle_ghost_cleanup=1,file_metadata_only=1,file_metadata_repair=1,file_stale_survivor=1,post_resolution_ghost_tail=1,stale_glusterfs_dirty_cleanup=1,stale_glusterfs_xattrop_cleanup=1",
            report,
        )
        self.assertIn("edges=file_residue_to_directory_child_gap:", report)

    def test_graph_report_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        plan_payload = {
            "schema_version": 1,
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "one",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(["graph-report", "--plan-in", str(plan_path)])
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(["graph-report", "--plan-in", str(plan_path)])

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertIn("graph: provisional=1", package_stdout.getvalue())
        self.assertIn("graph: provisional=1", manager_stdout.getvalue())

    def test_graph_audit_report_counts_structured_note_only_and_unmatched(self) -> None:
        structured = PlanAction(
            action_id="repair:structured",
            logical_path="dir",
            action_type="reconcile_directory",
            object_type="directory",
            depth=1,
            repair_strategy="reconcile_directory_children",
            graph_markers=["directory_child_gap:executable"],
            notes=["child-gap dependency closure is not executable"],
        )
        note_only = PlanAction(
            action_id="repair:note",
            logical_path="note",
            action_type="review_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="review_metadata_only",
            notes=["no missing or conflicting backend file found; only metadata review remains"],
        )
        unmatched = PlanAction(
            action_id="repair:unmatched",
            logical_path="unmatched",
            action_type="review_unknown",
            object_type="unknown",
            depth=1,
            repair_strategy="none",
            notes=["no known shape"],
        )

        annotate_action(structured)
        annotate_action(note_only)
        annotate_action(unmatched)

        report = render_graph_audit_report([structured, note_only, unmatched])

        self.assertIn("graph-audit: actions=3, structured=2, note_only=0, unmatched=1", report)
        self.assertIn("graph-audit-structured: nodes=directory_child_gap_executable=1,file_metadata_only=1", report)
        self.assertNotIn("graph-audit-note-only:", report)
        self.assertIn("graph-audit-unmatched: nodes=unmatched=1", report)

    def test_load_plan_actions_preserves_graph_markers_for_graph_audit(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:structured",
                    "logical_path": "dir",
                    "action_type": "review_file_metadata",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "review_metadata_only",
                    "graph_markers": ["file_metadata_only:review"],
                    "notes": ["no missing or conflicting backend file found; only metadata review remains"],
                }
            ]
        }

        actions = load_plan_actions(plan_payload)
        report = render_graph_audit_report(actions)

        self.assertEqual(1, len(actions))
        self.assertIn("file_metadata_only:review", actions[0].graph_markers)
        self.assertIn("graph-audit: actions=1, structured=1, note_only=0, unmatched=0", report)
        self.assertIn("graph-audit-structured: nodes=file_metadata_only=1", report)

    def test_graph_audit_reports_file_metadata_fallback_as_structured(self) -> None:
        action = PlanAction(
            action_id="repair:fallback",
            logical_path="fallback",
            action_type="review_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="review_metadata_only",
            graph_markers=["file_metadata_only:fallback"],
            notes=["no missing or conflicting backend file found; only metadata review remains"],
        )

        annotate_action(action)
        report = render_graph_audit_report([action])

        self.assertIn("file_metadata_only:fallback", action.graph_markers)
        self.assertIn("graph-audit: actions=1, structured=1, note_only=0, unmatched=0", report)
        self.assertIn("graph-audit-structured: nodes=file_metadata_only_fallback=1", report)

    def test_graph_audit_reports_directory_metadata_fallback_as_structured(self) -> None:
        action = PlanAction(
            action_id="repair:directory-fallback",
            logical_path="dir",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=1,
            repair_strategy="review_directory_state",
            graph_markers=["directory_metadata_only:fallback"],
            notes=["no missing directory or stale metadata found; review only"],
        )

        annotate_action(action)
        report = render_graph_audit_report([action])

        self.assertIn("directory_metadata_only:fallback", action.graph_markers)
        self.assertIn("graph-audit: actions=1, structured=1, note_only=0, unmatched=0", report)
        self.assertIn("graph-audit-structured: nodes=directory_metadata_only_fallback=1", report)

    def test_graph_audit_reports_type_mismatch_as_structured(self) -> None:
        action = PlanAction(
            action_id="repair:type-mismatch",
            logical_path="type-mismatch",
            action_type="review_type_mismatch",
            object_type="type_mismatch",
            depth=1,
            repair_strategy="review_type_mismatch",
            graph_markers=["type_mismatch:review"],
            notes=["synthetic type mismatch case"],
        )

        annotate_action(action)
        report = render_graph_audit_report([action])

        self.assertIn("type_mismatch:review", action.graph_markers)
        self.assertIn("graph-audit: actions=1, structured=1, note_only=0, unmatched=0", report)
        self.assertIn("graph-audit-structured: nodes=type_mismatch_review=1", report)

    def test_graph_audit_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        structured = PlanAction(
            action_id="repair:structured",
            logical_path="dir",
            action_type="reconcile_directory",
            object_type="directory",
            depth=1,
            repair_strategy="reconcile_directory_children",
            graph_markers=["directory_child_gap:executable"],
            notes=["child-gap dependency closure is not executable"],
        )
        note_only = PlanAction(
            action_id="repair:note",
            logical_path="note",
            action_type="review_file_metadata",
            object_type="file",
            depth=1,
            repair_strategy="review_metadata_only",
            notes=["no missing or conflicting backend file found; only metadata review remains"],
        )
        annotate_action(structured)
        annotate_action(note_only)
        plan_payload = {
            "schema_version": 1,
            "actions": [structured.to_dict(), note_only.to_dict()],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(["graph-audit", "--plan-in", str(plan_path)])
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(["graph-audit", "--plan-in", str(plan_path)])

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertIn("graph-audit: actions=2", package_stdout.getvalue())
        self.assertIn("graph-audit: actions=2", manager_stdout.getvalue())
        self.assertIn("graph-audit-structured: nodes=directory_child_gap_executable=1,file_metadata_only=1", package_stdout.getvalue())
        self.assertIn("graph-audit-structured: nodes=directory_child_gap_executable=1,file_metadata_only=1", manager_stdout.getvalue())

    def test_representative_plan_flow_renders_graph_status_and_apply_outputs(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        manifest = _file_manifest(hosts, {"brick-a", "brick-b", "brick-c"}, "replica-4/stale-survivor")
        plan = build_plan(manifest, mountpoint="/testvol")
        actual_node = plan[0].graph_node if plan else ""
        plan_payload = {
            "schema_version": 1,
            "actions": [action.to_dict() for action in plan],
        }
        status_payload = {
            "phase": "plan-built",
            "volume": "testvol",
            "mountpoint": "/testvol",
            "controller_cycle": {
                "controller_stop": True,
                "controller_reason": "graph cycle detected",
                "controller_next_action": "review-graph-cycle",
                "graph_cycle_present": True,
                "graph_cycle_repeat_count": 2,
                "graph_cycle_detected": True,
                "heal_repeat_count": 0,
                "heal_repeat_hit_limit": False,
                "heal_repeat_warning": "",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            status_path = Path(tmpdir) / "status.json"
            apply_path = Path(tmpdir) / "apply.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            graph_stdout = io.StringIO()
            family_stdout = io.StringIO()
            audit_stdout = io.StringIO()
            status_stdout = io.StringIO()
            apply_build_stdout = io.StringIO()
            apply_run_stdout = io.StringIO()

            with contextlib.redirect_stdout(graph_stdout):
                graph_rc = package_cli.main(["graph-report", "--plan-in", str(plan_path)])
            with contextlib.redirect_stdout(family_stdout):
                family_rc = package_cli.main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "file"]
                )
            with contextlib.redirect_stdout(audit_stdout):
                audit_rc = package_cli.main(["graph-audit", "--plan-in", str(plan_path)])
            with contextlib.redirect_stdout(status_stdout):
                status_rc = package_cli.main(["status-report", "--status-file", str(status_path)])
            with contextlib.redirect_stdout(apply_build_stdout):
                apply_build_rc = package_cli.main(
                    [
                        "apply-build",
                        "--plan-in",
                        str(plan_path),
                        "--apply-out",
                        str(apply_path),
                        "--status-file",
                        str(status_path),
                    ]
                )
            with contextlib.redirect_stdout(apply_run_stdout):
                apply_run_rc = package_cli.main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(status_path),
                        "--summary-only",
                    ]
                )

        self.assertEqual(0, graph_rc)
        self.assertEqual(0, family_rc)
        self.assertEqual(0, audit_rc)
        self.assertEqual(0, status_rc)
        self.assertEqual(0, apply_build_rc)
        self.assertEqual(0, apply_run_rc)
        self.assertIn("graph: provisional=", graph_stdout.getvalue())
        self.assertIn("rescan=1", graph_stdout.getvalue())
        self.assertIn(f"nodes={actual_node}=1", graph_stdout.getvalue())
        self.assertIn("graph-family: file", family_stdout.getvalue())
        self.assertIn("graph-family-summary: actions=1", family_stdout.getvalue())
        self.assertIn(f"nodes={actual_node}=1", family_stdout.getvalue())
        self.assertIn("graph-audit: actions=1, structured=1, note_only=0, unmatched=0", audit_stdout.getvalue())
        self.assertIn(f"graph-audit-structured: nodes={actual_node}=1", audit_stdout.getvalue())
        self.assertIn("controller-cycle: stop=yes, next_action=review-graph-cycle, reason=graph cycle detected", status_stdout.getvalue())
        self.assertIn("controller-cycle: next_action=review-graph-cycle", apply_build_stdout.getvalue())
        self.assertIn("controller-cycle: next_action=review-graph-cycle", apply_run_stdout.getvalue())

    def test_graph_cycle_report_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        action = PlanAction(
            action_id="repair:one",
            logical_path="one",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=1,
            repair_strategy="delete_below_quorum_file",
        )
        annotate_action(action)
        plan_payload = {
            "schema_version": 1,
            "actions": [action.to_dict()],
        }
        baseline = build_graph_cycle_baseline([action])
        status_payload = {
            "graph_cycle": {
                "signature": baseline["signature"],
                "history": [baseline["signature"]],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            status_path = Path(tmpdir) / "status.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(
                    ["graph-cycle-report", "--plan-in", str(plan_path), "--status-file", str(status_path)]
                )
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(
                    ["graph-cycle-report", "--plan-in", str(plan_path), "--status-file", str(status_path)]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertIn("graph-cycle: repeat_count=1", package_stdout.getvalue())
        self.assertIn("graph-cycle: repeat_count=1", manager_stdout.getvalue())
        self.assertIn("cycle_detected=false", package_stdout.getvalue())
        self.assertIn("cycle_detected=false", manager_stdout.getvalue())

    def test_graph_report_handles_empty_plan(self) -> None:
        report = render_graph_report({"actions": []})

        self.assertEqual("graph: none", report)

    def test_graph_report_includes_cycle_verdict_when_present(self) -> None:
        report = render_graph_report(
            {
                "actions": [],
                "graph_cycle": {
                    "repeat_count": 2,
                    "cycle_detected": True,
                    "stop_reason": "graph signature repeated",
                },
            }
        )

        self.assertIn("graph: none", report)
        self.assertIn("graph-cycle: repeat_count=2", report)

    def test_graph_report_can_show_edges_and_cycle_together(self) -> None:
        report = render_graph_report(
            {
                "actions": [
                    {
                        "action_id": "repair:one",
                        "logical_path": "one",
                        "action_type": "review_probable_stale_survivor",
                        "object_type": "file",
                        "depth": 1,
                        "repair_strategy": "delete_below_quorum_file",
                        "graph_node": "file_stale_survivor",
                        "provisional": True,
                        "rescan_after_apply": True,
                        "followup_edges": [
                            {
                                "edge_id": "file_residue_to_directory_child_gap",
                                "priority": 10,
                                "when": "same_subtree_has_directory_child_gap",
                                "next_node": "directory_child_gap",
                                "report": "Resolve file residue first, then replan the child directory from a fresh snapshot.",
                            }
                        ],
                    }
                ],
                "graph_cycle": {
                    "repeat_count": 2,
                    "cycle_detected": True,
                    "stop_reason": "graph signature repeated",
                },
            }
        )

        self.assertIn("graph: provisional=1", report)
        self.assertIn("edges=file_residue_to_directory_child_gap:", report)
        self.assertIn("graph-cycle: repeat_count=2", report)

    def test_followup_edge_is_added_for_mixed_subtree(self) -> None:
        from gluster_heal_tool.planner import build_plan

        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        file_manifest = _file_manifest(hosts, {"brick-a"}, "replica-4/mixed/file-a")
        dir_manifest = _directory_manifest(hosts, set(hosts), "replica-4/mixed/child-dir")
        for host, observations in dir_manifest["replica-4/mixed/child-dir"].observations.items():
            observations[0].backend_child_names = ["alpha"] if host != hosts[-1] else []

        plan = build_plan({**file_manifest, **dir_manifest}, mountpoint="/testvol")
        file_action = next(action for action in plan if action.logical_path.endswith("file-a"))

        self.assertEqual("file_stale_survivor_mount_visible", file_action.graph_node)
        self.assertTrue(file_action.followup_edges)
        self.assertEqual("file_residue_to_directory_child_gap", file_action.followup_edges[0]["edge_id"])
        self.assertEqual("directory_child_gap", file_action.followup_edges[0]["next_node"])

    def test_directory_followup_edges_chain_through_child_reference(self) -> None:
        child_gap = PlanAction(
            action_id="repair:child-gap",
            logical_path="replica-4/mixed/child-dir",
            action_type="review_directory_children",
            object_type="directory",
            depth=2,
            repair_strategy="reconcile_directory_children",
        )
        child_reference = PlanAction(
            action_id="repair:child-ref",
            logical_path="unresolved-child:parent-gfid/alpha/payload.txt",
            action_type="review_directory_children",
            object_type="unknown",
            depth=2,
            repair_strategy="reconcile_directory_children",
            notes=[
                "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup",
                "live chain targets: /dir/child",
            ],
        )
        restore = PlanAction(
            action_id="repair:restore",
            logical_path="replica-4/mixed/dir",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="recreate_missing_directory_backend",
            notes=[
                "recreate the missing backend directory on the affected brick and restore the correct directory GFID linkage",
                "brick-side targets to recreate: brick-a",
            ],
        )

        annotate_action(child_gap, [child_gap, child_reference, restore])
        annotate_action(child_reference, [child_gap, child_reference, restore])
        annotate_action(restore, [child_gap, child_reference, restore])

        self.assertEqual("directory_child_gap", child_gap.graph_node)
        self.assertEqual("directory_child_reference_live", child_reference.graph_node)
        self.assertEqual("directory_restore_backend", restore.graph_node)
        self.assertEqual("directory_child_gap_to_directory_child_reference", child_gap.followup_edges[0]["edge_id"])
        self.assertEqual("directory_child_reference", child_gap.followup_edges[0]["next_node"])
        self.assertEqual("directory_restore_backend_to_directory_child_gap", restore.followup_edges[0]["edge_id"])
        self.assertEqual("directory_child_gap", restore.followup_edges[0]["next_node"])

    def test_directory_followup_edges_include_immediate_missing_child_reference(self) -> None:
        child_gap = PlanAction(
            action_id="repair:child-gap",
            logical_path="replica-4/mixed/child-dir",
            action_type="review_directory_children",
            object_type="directory",
            depth=2,
            repair_strategy="reconcile_directory_children",
        )
        child_reference = PlanAction(
            action_id="repair:child-ref",
            logical_path="unresolved-child:parent-gfid/gamma",
            action_type="review_directory_children",
            object_type="unknown",
            depth=2,
            repair_strategy="verify_directory_child_absence",
            graph_markers=["directory_child_reference:immediate_missing"],
        )

        annotate_action(child_gap, [child_gap, child_reference])

        self.assertEqual("directory_child_gap", child_gap.graph_node)
        self.assertEqual(
            "directory_child_gap_to_directory_child_reference",
            child_gap.followup_edges[0]["edge_id"],
        )
        self.assertEqual(
            "directory_child_reference",
            child_gap.followup_edges[0]["next_node"],
        )

    def test_directory_metadata_review_followup_edges_chain_to_child_gap(self) -> None:
        metadata_review = PlanAction(
            action_id="repair:metadata-review",
            logical_path="replica-4/mixed/dir",
            action_type="review_directory_metadata",
            object_type="directory",
            depth=2,
            repair_strategy="review_directory_state",
            graph_markers=["directory_metadata_only:fallback"],
            notes=[
                "children must be repaired before parent directory",
                "directory child-set reconciliation needed across hosts: brick-a, brick-b, brick-c",
                "no missing directory or stale metadata found; review only",
            ],
        )
        child_gap = PlanAction(
            action_id="repair:child-gap",
            logical_path="replica-4/mixed/dir/child",
            action_type="review_directory_children",
            object_type="directory",
            depth=3,
            repair_strategy="reconcile_directory_children",
            graph_markers=["directory_child_gap:present"],
        )
        child_reference = PlanAction(
            action_id="repair:child-ref",
            logical_path="unresolved-child:parent-gfid/child/payload.txt",
            action_type="review_directory_children",
            object_type="unknown",
            depth=3,
            repair_strategy="reconcile_directory_children",
            notes=[
                "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup",
                "live chain targets: /replica-4/mixed/dir/child",
            ],
        )

        annotate_action(metadata_review, [metadata_review])
        self.assertEqual("directory_metadata_to_directory_child_gap", metadata_review.followup_edges[0]["edge_id"])
        self.assertEqual("directory_child_gap", metadata_review.followup_edges[0]["next_node"])

        annotate_action(metadata_review, [metadata_review, child_gap])
        annotate_action(child_gap, [metadata_review, child_gap, child_reference])

        self.assertEqual("directory_metadata_only_fallback", metadata_review.graph_node)
        self.assertTrue(metadata_review.followup_edges)
        self.assertEqual("directory_metadata_to_directory_child_gap", metadata_review.followup_edges[0]["edge_id"])
        self.assertEqual("directory_child_gap", metadata_review.followup_edges[0]["next_node"])

        annotate_action(metadata_review, [metadata_review, child_gap, child_reference])

        self.assertEqual("directory_metadata_to_directory_child_reference", metadata_review.followup_edges[0]["edge_id"])
        self.assertEqual("directory_child_reference", metadata_review.followup_edges[0]["next_node"])

    def test_stale_index_cleanup_keeps_rescan_edge(self) -> None:
        action = PlanAction(
            action_id="repair:index",
            logical_path="index",
            action_type="cleanup_stale_glusterfs_index",
            object_type="stale_glusterfs_index",
            depth=1,
            repair_strategy="delete_stale_glusterfs_index_residue",
        )

        annotate_action(action)

        self.assertEqual("stale_glusterfs_index_cleanup", action.graph_node)
        self.assertTrue(action.rescan_after_apply)
        self.assertEqual(1, len(action.followup_edges))
        self.assertEqual("stale_index_to_rescan", action.followup_edges[0]["edge_id"])

    def test_plan_summary_counts_layered_tails_from_graph_edges(self) -> None:
        file_action = PlanAction(
            action_id="repair:file",
            logical_path="replica-4/mixed/file-a",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=2,
            repair_strategy="delete_below_quorum_file",
        )
        cleanup_action = PlanAction(
            action_id="repair:cleanup",
            logical_path="replica-4/mixed/cleanup",
            action_type="cleanup_dead_gfid",
            object_type="unknown",
            depth=2,
            repair_strategy="delete_dead_gfid_residue",
        )
        annotate_action(file_action, [file_action, cleanup_action])
        annotate_action(cleanup_action, [file_action, cleanup_action])
        file_action.followup_edges = [
            {
                "edge_id": "file_residue_to_directory_child_gap",
                "priority": 10,
                "when": "same_subtree_has_directory_child_gap",
                "next_node": "directory_child_gap",
                "report": "Resolve file residue first, then replan the child directory from a fresh snapshot.",
            }
        ]

        summary = summarize_plan([file_action, cleanup_action])
        rendered = render_plan_summary(summary)

        self.assertEqual(1, summary["layered_tail_sequences"])
        self.assertIn("layered-tails=1", rendered)
        self.assertIn("graph: provisional=1", rendered)

    def test_plan_summary_counts_safe_default_directory_backend_recreate_as_ready(self) -> None:
        action = PlanAction(
            action_id="repair:directory",
            logical_path="replica-3/alpha/beta/gamma",
            action_type="repair_directory_metadata",
            object_type="directory",
            depth=3,
            repair_strategy="recreate_missing_directory_backend",
            decision_class="safe_default",
        )

        summary = summarize_plan([action])
        rendered = render_plan_summary(summary)

        self.assertEqual("safe_default", action.decision_class)
        self.assertEqual(1, summary["ready_repairs"])
        self.assertEqual(1, summary["directories_to_reconcile"])
        self.assertEqual(0, summary["review_items"])
        self.assertIn("Plan ready: 1 ready repairs", rendered)
        self.assertIn("0 files, 1 directories", rendered)
        self.assertNotIn("dir-meta-repair", rendered)

    def test_graph_cycle_helpers_detect_repeat_signatures(self) -> None:
        action = PlanAction(
            action_id="repair:file",
            logical_path="replica-4/mixed/file-a",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=2,
            repair_strategy="delete_below_quorum_file",
        )
        annotate_action(action)

        signature = plan_signature([action])
        repeated = assess_graph_cycle([signature, signature], signature, max_repeats=2)

        self.assertEqual(action_signature(action), signature[0])
        self.assertEqual(2, repeated["repeat_count"])
        self.assertTrue(repeated["cycle_detected"])
        self.assertEqual("graph signature repeated", repeated["stop_reason"])

    def test_graph_cycle_report_renders_verdict(self) -> None:
        report = render_graph_cycle_report(
            {
                "repeat_count": 2,
                "cycle_detected": True,
                "stop_reason": "graph signature repeated",
            }
        )

        self.assertIn("graph-cycle: repeat_count=2", report)
        self.assertIn("cycle_detected=true", report)
        self.assertIn("stop_reason=graph signature repeated", report)

    def test_graph_cycle_status_tracks_previous_signature(self) -> None:
        action = PlanAction(
            action_id="repair:file",
            logical_path="replica-4/mixed/file-a",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=2,
            repair_strategy="delete_below_quorum_file",
        )
        annotate_action(action)
        previous_status = {
            "graph_cycle": {
                "signature": plan_signature([action]),
                "history": [plan_signature([action])],
            }
        }

        cycle_result = assess_graph_cycle_status(previous_status, [action], max_repeats=2)

        self.assertTrue(cycle_result["cycle_detected"])
        self.assertEqual(2, cycle_result["repeat_count"])
        self.assertEqual(plan_signature([action]), cycle_result["signature"])

    def test_graph_cycle_baseline_captures_signature(self) -> None:
        action = PlanAction(
            action_id="repair:file",
            logical_path="replica-4/mixed/file-a",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=2,
            repair_strategy="delete_below_quorum_file",
        )
        annotate_action(action)

        baseline = build_graph_cycle_baseline([action])

        self.assertEqual(plan_signature([action]), baseline["signature"])
        self.assertEqual([], baseline["history"])

    def test_graph_cycle_baseline_status_includes_report(self) -> None:
        action = PlanAction(
            action_id="repair:file",
            logical_path="replica-4/mixed/file-a",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=2,
            repair_strategy="delete_below_quorum_file",
        )
        annotate_action(action)

        payload = build_graph_cycle_baseline_status([action])

        self.assertIn("graph_cycle", payload)
        self.assertIn("graph_cycle_report", payload)
        self.assertIn("graph-cycle: repeat_count=0", payload["graph_cycle_report"])

    def test_graph_cycle_status_package_includes_report(self) -> None:
        action = PlanAction(
            action_id="repair:file",
            logical_path="replica-4/mixed/file-a",
            action_type="review_probable_stale_survivor",
            object_type="file",
            depth=2,
            repair_strategy="delete_below_quorum_file",
        )
        annotate_action(action)
        previous_status = {
            "graph_cycle": {
                "signature": plan_signature([action]),
                "history": [plan_signature([action])],
            }
        }

        payload = build_graph_cycle_status(previous_status, [action], max_repeats=2)

        self.assertIn("graph_cycle", payload)
        self.assertIn("graph_cycle_report", payload)
        self.assertIn("graph-cycle: repeat_count=2", payload["graph_cycle_report"])

    def test_graph_cycle_status_report_handles_missing_baseline(self) -> None:
        self.assertEqual("graph-cycle: unavailable", render_graph_cycle_status_report({}))

    def test_graph_cycle_status_report_prefers_stored_report(self) -> None:
        self.assertEqual(
            "graph-cycle: repeat_count=3, cycle_detected=true",
            render_graph_cycle_status_report(
                {
                    "graph_cycle_report": "graph-cycle: repeat_count=3, cycle_detected=true",
                    "graph_cycle": {
                        "repeat_count": 1,
                        "cycle_detected": False,
                    },
                }
            ),
        )

    def test_graph_cycle_status_summary_reports_presence(self) -> None:
        from gluster_heal_tool.planner_graph import summarize_graph_cycle_status

        summary = summarize_graph_cycle_status(
            {
                "graph_cycle_report": "graph-cycle: repeat_count=3, cycle_detected=true",
                "graph_cycle": {
                    "repeat_count": 1,
                    "cycle_detected": False,
                },
            }
        )

        self.assertTrue(summary["graph_cycle_present"])
        self.assertEqual("graph-cycle: repeat_count=3, cycle_detected=true", summary["graph_cycle_report"])

    def test_graph_cycle_status_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        status_payload = {
            "graph_cycle_report": "graph-cycle: repeat_count=3, cycle_detected=true",
            "graph_cycle": {
                "repeat_count": 1,
                "cycle_detected": False,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(["graph-cycle-status", "--status-file", str(status_path)])
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(["graph-cycle-status", "--status-file", str(status_path)])

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual("graph-cycle-status: present=yes", package_stdout.getvalue().splitlines()[0])
        self.assertEqual("graph-cycle-status: present=yes", manager_stdout.getvalue().splitlines()[0])
        self.assertEqual("graph-cycle: repeat_count=3, cycle_detected=true", package_stdout.getvalue().splitlines()[1])
        self.assertEqual("graph-cycle: repeat_count=3, cycle_detected=true", manager_stdout.getvalue().splitlines()[1])

    def test_graph_cycle_status_summary_renders_presence(self) -> None:
        self.assertEqual(
            "graph-cycle-status: present=yes",
            render_graph_cycle_status_summary(
                {
                    "graph_cycle": {
                        "repeat_count": 1,
                        "cycle_detected": False,
                    }
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
