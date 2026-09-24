# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for family-scoped graph reporting."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from gluster_heal_tool import cli as package_cli
from gluster_heal_tool.graph_family_report import render_graph_family_report


def _load_manager_main():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module.main


class GraphFamilyReportTests(unittest.TestCase):
    def test_render_graph_family_report_filters_stale_index_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": ".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555",
                    "action_type": "cleanup_stale_glusterfs_index",
                    "object_type": "stale_glusterfs_index",
                    "depth": 3,
                    "repair_strategy": "delete_stale_glusterfs_index_residue",
                    "graph_node": "stale_glusterfs_xattrop_cleanup",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "stale-index")

        self.assertIn("graph-family: stale-index", report)
        self.assertIn("nodes=stale_glusterfs_xattrop_cleanup=1", report)
        self.assertNotIn("file_stale_survivor=1", report)

    def test_render_graph_family_report_filters_directory_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "dir",
                    "action_type": "review_directory_gfid_conflict",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "rename_conflicting_directory",
                    "graph_node": "directory_split_brain",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:three",
                    "logical_path": "dir",
                    "action_type": "repair_directory_metadata",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "attach_directory_gfid",
                    "graph_node": "directory_metadata_repair",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:four",
                    "logical_path": "dir",
                    "action_type": "repair_directory_metadata",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "recreate_missing_directory_backend",
                    "graph_node": "directory_restore_backend",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:five",
                    "logical_path": "dir",
                    "action_type": "review_directory_metadata",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "review_directory_presence",
                    "graph_node": "directory_restore_review",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:sixb",
                    "logical_path": "dir",
                    "action_type": "review_directory_metadata",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "review_directory_presence",
                    "graph_node": "directory_restore_review_mount_error",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:six",
                    "logical_path": "dir",
                    "action_type": "review_directory_metadata",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "review_directory_state",
                    "graph_node": "directory_metadata_only",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:sevenb",
                    "logical_path": "dir",
                    "action_type": "review_directory_metadata",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "review_directory_state",
                    "graph_node": "directory_metadata_only_fallback",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:seven",
                    "logical_path": "unresolved-child:parent-gfid/alpha/payload.txt",
                    "action_type": "review_directory_children",
                    "object_type": "unknown",
                    "depth": 2,
                    "repair_strategy": "reconcile_directory_children",
                    "graph_node": "directory_child_reference",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:seven-immediate",
                    "logical_path": "unresolved-child:parent-gfid/gamma",
                    "action_type": "review_directory_children",
                    "object_type": "unknown",
                    "depth": 2,
                    "repair_strategy": "verify_directory_child_absence",
                    "graph_node": "directory_child_reference_immediate_missing",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:seven-tie",
                    "logical_path": "dir-presence-tie",
                    "action_type": "review_directory_gfid_conflict",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "ambiguous_directory_presence_tie",
                    "graph_node": "directory_presence_tie",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:eight",
                    "logical_path": "dir/file",
                    "action_type": "review_entry_split_brain",
                    "object_type": "directory",
                    "depth": 2,
                    "repair_strategy": "review_entry_split_brain_directory",
                    "graph_node": "directory_entry_split_brain",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "directory")

        self.assertIn("graph-family: directory", report)
        self.assertIn(
            "graph-family-summary: actions=11, nodes=directory_child_reference,directory_child_reference_immediate_missing,directory_entry_split_brain,directory_metadata_only,directory_metadata_only_fallback,directory_metadata_repair,directory_presence_tie,directory_restore_backend,directory_restore_review,directory_restore_review_mount_error,directory_split_brain",
            report,
        )
        self.assertIn(
            "nodes=directory_child_reference=1,directory_child_reference_immediate_missing=1,directory_entry_split_brain=1,directory_metadata_only=1,directory_metadata_only_fallback=1,directory_metadata_repair=1,directory_presence_tie=1,directory_restore_backend=1,directory_restore_review=1,directory_restore_review_mount_error=1,directory_split_brain=1",
            report,
        )

    def test_render_graph_family_report_filters_directory_entry_split_brain_mount_error_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_entry_split_brain",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "review_entry_split_brain_directory",
                    "graph_node": "directory_entry_split_brain_mount_error",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_entry_split_brain",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "review_entry_split_brain_directory",
                    "graph_node": "directory_entry_split_brain",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "directory")

        self.assertIn("graph-family: directory", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=directory_entry_split_brain,directory_entry_split_brain_mount_error",
            report,
        )
        self.assertIn("nodes=directory_entry_split_brain=1,directory_entry_split_brain_mount_error=1", report)

    def test_render_graph_family_report_filters_directory_gfid_conflict_mount_first_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_directory_gfid_conflict",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "rename_conflicting_directory",
                    "graph_node": "directory_gfid_conflict_mount_first",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_directory_gfid_conflict",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "rename_conflicting_directory",
                    "graph_node": "directory_gfid_conflict",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "directory")

        self.assertIn("graph-family: directory", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=directory_gfid_conflict,directory_gfid_conflict_mount_first",
            report,
        )
        self.assertIn("nodes=directory_gfid_conflict=1,directory_gfid_conflict_mount_first=1", report)

    def test_render_graph_family_report_filters_directory_child_reference_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_directory_children",
                    "object_type": "unknown",
                    "depth": 1,
                    "repair_strategy": "reconcile_directory_children",
                    "graph_node": "directory_child_reference_live",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_directory_children",
                    "object_type": "unknown",
                    "depth": 1,
                    "repair_strategy": "reconcile_directory_children",
                    "graph_node": "directory_child_reference",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "directory")

        self.assertIn("graph-family: directory", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=directory_child_reference,directory_child_reference_live",
            report,
        )
        self.assertIn("nodes=directory_child_reference=1,directory_child_reference_live=1", report)
        self.assertNotIn("file_stale_survivor=1", report)

    def test_render_graph_family_report_filters_directory_child_gap_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_directory_children",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "reconcile_directory_children",
                    "graph_node": "directory_child_gap_executable",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_directory_children",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "reconcile_directory_children",
                    "graph_node": "directory_child_gap_split_brain_child",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "directory")

        self.assertIn("graph-family: directory", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=directory_child_gap_executable,directory_child_gap_split_brain_child",
            report,
        )
        self.assertNotIn("file_stale_survivor=1", report)

    def test_render_graph_family_report_filters_file_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor_mount_visible",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor_mount_hidden",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:three",
                    "logical_path": "c",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_terminal",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_stale_survivor_mount_hidden,file_stale_survivor_mount_visible",
            report,
        )
        self.assertIn("nodes=file_stale_survivor_mount_hidden=1,file_stale_survivor_mount_visible=1", report)
        self.assertNotIn("dead_gfid_cleanup_terminal=1", report)

    def test_render_graph_family_report_filters_file_entry_split_brain_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "replace_entry_split_brain_file",
                    "graph_node": "file_entry_split_brain_replace",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "ambiguous_entry_split_brain_file",
                    "graph_node": "file_entry_split_brain_tie",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_entry_split_brain_replace,file_entry_split_brain_tie",
            report,
        )
        self.assertNotIn("dead_gfid_cleanup_terminal=1", report)

    def test_render_graph_family_report_filters_file_entry_split_brain_mount_error_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "replace_entry_split_brain_file",
                    "graph_node": "file_entry_split_brain_mount_error",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "replace_entry_split_brain_file",
                    "graph_node": "file_entry_split_brain_replace",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:three",
                    "logical_path": "c",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "ambiguous_entry_split_brain_file",
                    "graph_node": "file_entry_split_brain_tie",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=3, nodes=file_entry_split_brain_mount_error,file_entry_split_brain_replace,file_entry_split_brain_tie",
            report,
        )
        self.assertIn(
            "nodes=file_entry_split_brain_mount_error=1,file_entry_split_brain_replace=1,file_entry_split_brain_tie=1",
            report,
        )

    def test_render_graph_family_report_filters_file_split_brain_majority_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "replace_entry_split_brain_file",
                    "graph_node": "file_split_brain_resolver_first",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_entry_split_brain",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "replace_entry_split_brain_file",
                    "graph_node": "file_split_brain_majority",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_split_brain_majority,file_split_brain_resolver_first",
            report,
        )
        self.assertIn("nodes=file_split_brain_majority=1,file_split_brain_resolver_first=1", report)

    def test_render_graph_family_report_filters_orphaned_symlink_file_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_orphaned_symlink",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_orphaned_symlink_residue",
                    "graph_node": "file_orphaned_symlink_mount_visible",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_probable_orphaned_symlink",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_orphaned_symlink_residue",
                    "graph_node": "file_orphaned_symlink_mount_hidden",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:three",
                    "logical_path": "c",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_terminal",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_orphaned_symlink_mount_hidden,file_orphaned_symlink_mount_visible",
            report,
        )
        self.assertNotIn("dead_gfid_cleanup_terminal=1", report)

    def test_render_graph_family_report_filters_file_ghost_cleanup_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "unknown",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "file_handle_ghost_cleanup",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "unknown",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "post_resolution_ghost_tail",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_handle_ghost_cleanup,post_resolution_ghost_tail",
            report,
        )
        self.assertNotIn("dead_gfid_cleanup_terminal=1", report)

    def test_render_graph_family_report_filters_file_symlink_restore_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "repair_file",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "restore_missing_replica",
                    "graph_node": "file_symlink_restore",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_probable_orphaned_symlink",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_orphaned_symlink_residue",
                    "graph_node": "file_orphaned_symlink_mount_hidden",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_orphaned_symlink_mount_hidden,file_symlink_restore",
            report,
        )
        self.assertIn("nodes=file_orphaned_symlink_mount_hidden=1,file_symlink_restore=1", report)

    def test_render_graph_family_report_filters_file_restore_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "repair_file",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "restore_missing_replica",
                    "graph_node": "file_restore",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "repair_file",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "restore_missing_replica",
                    "graph_node": "file_symlink_restore",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn("graph-family-summary: actions=2, nodes=file_restore,file_symlink_restore", report)
        self.assertIn("nodes=file_restore=1,file_symlink_restore=1", report)

    def test_render_graph_family_report_filters_file_delete_below_quorum_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_delete_below_quorum",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "repair_file",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "restore_missing_replica",
                    "graph_node": "file_restore",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn("graph-family-summary: actions=2, nodes=file_delete_below_quorum,file_restore", report)
        self.assertIn("nodes=file_delete_below_quorum=1,file_restore=1", report)

    def test_render_graph_family_report_filters_file_metadata_only_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_file_metadata",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "review_metadata_only",
                    "graph_node": "file_metadata_only",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_file_metadata",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "review_metadata_only",
                    "graph_node": "file_metadata_only_fallback",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "file")

        self.assertIn("graph-family: file", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=file_metadata_only,file_metadata_only_fallback",
            report,
        )
        self.assertIn("nodes=file_metadata_only=1,file_metadata_only_fallback=1", report)

    def test_render_graph_family_report_filters_type_mismatch_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_type_mismatch",
                    "object_type": "type_mismatch",
                    "depth": 1,
                    "repair_strategy": "review_type_mismatch",
                    "graph_node": "type_mismatch_review",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "repair_file",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "restore_missing_replica",
                    "graph_node": "file_symlink_restore",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "type-mismatch")

        self.assertIn("graph-family: type-mismatch", report)
        self.assertIn("graph-family-summary: actions=1, nodes=type_mismatch_review", report)
        self.assertIn("nodes=type_mismatch_review=1", report)

    def test_render_graph_family_report_filters_directory_delete_below_quorum_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_subtree",
                    "graph_node": "directory_delete_below_quorum",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "review_type_mismatch",
                    "object_type": "type_mismatch",
                    "depth": 1,
                    "repair_strategy": "review_type_mismatch",
                    "graph_node": "type_mismatch_review",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "directory")

        self.assertIn("graph-family: directory", report)
        self.assertIn(
            "graph-family-summary: actions=1, nodes=directory_delete_below_quorum",
            report,
        )
        self.assertIn("nodes=directory_delete_below_quorum=1", report)

    def test_render_graph_family_report_filters_selected_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_terminal",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "dead-gfid")

        self.assertIn("graph-family: dead-gfid", report)
        self.assertIn("nodes=dead_gfid_cleanup_terminal=1", report)
        self.assertNotIn("file_stale_survivor=1", report)

    def test_render_graph_family_report_filters_dead_gfid_cleanup_direct_family(self) -> None:
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_direct",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_terminal",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }

        report = render_graph_family_report(plan_payload, "dead-gfid")

        self.assertIn("graph-family: dead-gfid", report)
        self.assertIn(
            "graph-family-summary: actions=2, nodes=dead_gfid_cleanup_direct,dead_gfid_cleanup_terminal",
            report,
        )
        self.assertNotIn("file_stale_survivor=1", report)

    def test_graph_family_report_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_terminal",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "dead-gfid"]
                )
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "dead-gfid"]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual("graph-family: dead-gfid", package_stdout.getvalue().splitlines()[0])
        self.assertEqual("graph-family: dead-gfid", manager_stdout.getvalue().splitlines()[0])
        self.assertIn("nodes=dead_gfid_cleanup_terminal=1", package_stdout.getvalue())
        self.assertIn("nodes=dead_gfid_cleanup_terminal=1", manager_stdout.getvalue())

    def test_graph_family_report_command_renders_file_family_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "b",
                    "action_type": "cleanup_dead_gfid",
                    "object_type": "dead_gfid",
                    "depth": 1,
                    "repair_strategy": "delete_dead_gfid_residue",
                    "graph_node": "dead_gfid_cleanup_terminal",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "file"]
                )
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "file"]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual("graph-family: file", package_stdout.getvalue().splitlines()[0])
        self.assertEqual("graph-family: file", manager_stdout.getvalue().splitlines()[0])
        self.assertIn("graph-family-summary: actions=1, nodes=file_stale_survivor", package_stdout.getvalue())
        self.assertIn("graph-family-summary: actions=1, nodes=file_stale_survivor", manager_stdout.getvalue())
        self.assertIn("nodes=file_stale_survivor=1", package_stdout.getvalue())
        self.assertIn("nodes=file_stale_survivor=1", manager_stdout.getvalue())

    def test_graph_family_report_command_renders_directory_family_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": "dir",
                    "action_type": "review_directory_gfid_conflict",
                    "object_type": "directory",
                    "depth": 1,
                    "repair_strategy": "rename_conflicting_directory",
                    "graph_node": "directory_split_brain",
                    "provisional": False,
                    "rescan_after_apply": False,
                    "followup_edges": [],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "directory"]
                )
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "directory"]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual("graph-family: directory", package_stdout.getvalue().splitlines()[0])
        self.assertEqual("graph-family: directory", manager_stdout.getvalue().splitlines()[0])
        self.assertIn("nodes=directory_split_brain=1", package_stdout.getvalue())
        self.assertIn("nodes=directory_split_brain=1", manager_stdout.getvalue())

    def test_graph_family_report_command_renders_stale_index_family_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        plan_payload = {
            "actions": [
                {
                    "action_id": "repair:one",
                    "logical_path": "a",
                    "action_type": "review_probable_stale_survivor",
                    "object_type": "file",
                    "depth": 1,
                    "repair_strategy": "delete_below_quorum_file",
                    "graph_node": "file_stale_survivor",
                    "provisional": True,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
                {
                    "action_id": "repair:two",
                    "logical_path": ".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555",
                    "action_type": "cleanup_stale_glusterfs_index",
                    "object_type": "stale_glusterfs_index",
                    "depth": 3,
                    "repair_strategy": "delete_stale_glusterfs_index_residue",
                    "graph_node": "stale_glusterfs_xattrop_cleanup",
                    "provisional": False,
                    "rescan_after_apply": True,
                    "followup_edges": [],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.json"
            plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "stale-index"]
                )
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(
                    ["graph-family-report", "--plan-in", str(plan_path), "--family", "stale-index"]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual("graph-family: stale-index", package_stdout.getvalue().splitlines()[0])
        self.assertEqual("graph-family: stale-index", manager_stdout.getvalue().splitlines()[0])
        self.assertIn("nodes=stale_glusterfs_xattrop_cleanup=1", package_stdout.getvalue())
        self.assertIn("nodes=stale_glusterfs_xattrop_cleanup=1", manager_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
