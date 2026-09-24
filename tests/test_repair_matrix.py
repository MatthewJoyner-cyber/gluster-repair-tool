# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the repair matrix."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from gluster_heal_tool.repair_matrix import repair_matrix_family_rows
from gluster_heal_tool.repair_matrix import repair_matrix_row, repair_matrix_rows, render_repair_matrix

REPO_ROOT = Path(__file__).resolve().parents[1]


class RepairMatrixTests(unittest.TestCase):
    def test_matrix_rows_cover_expected_restore_delete_and_review_paths(self) -> None:
        rows = repair_matrix_rows()
        by_key = {row["key"]: row for row in rows}

        self.assertEqual("restore_missing_replica", by_key["file_restore"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["file_restore"]["decision_class"])
        self.assertEqual("delete_below_quorum_file", by_key["file_delete_below_quorum"]["default_repair_path"])
        self.assertEqual("risky_default", by_key["file_delete_below_quorum"]["decision_class"])
        self.assertIn("quarantine_both", by_key["file_presence_tie"]["default_repair_path"])
        self.assertEqual("operator_only", by_key["file_presence_tie"]["decision_class"])
        self.assertEqual("replace_entry_split_brain_file", by_key["file_split_brain_majority"]["default_repair_path"])
        self.assertEqual("risky_default", by_key["file_split_brain_majority"]["decision_class"])
        self.assertEqual("recreate_missing_directory_backend", by_key["directory_restore"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["directory_restore"]["decision_class"])
        self.assertEqual("delete_below_quorum_subtree", by_key["directory_delete_below_quorum"]["default_repair_path"])
        self.assertEqual("risky_default", by_key["directory_delete_below_quorum"]["decision_class"])
        self.assertIn("quarantine_both", by_key["directory_presence_tie"]["default_repair_path"])
        self.assertEqual("operator_only", by_key["directory_presence_tie"]["decision_class"])
        self.assertEqual("reconcile_directory_children", by_key["directory_child_gap"]["default_repair_path"])
        self.assertEqual("chain_follow", by_key["directory_child_gap"]["decision_class"])
        self.assertIn("recover_missing_child_or_confirm_delete", by_key["directory_child_reference"]["default_repair_path"])
        self.assertEqual("chain_follow", by_key["directory_child_reference"]["decision_class"])
        self.assertIn("never delete the parent GFID handle", by_key["directory_child_reference"]["notes"])
        self.assertEqual("repair_directory_metadata", by_key["directory_gfid_merge"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["directory_gfid_merge"]["decision_class"])
        self.assertEqual("quarantine_both, then directory-tie-build", by_key["directory_split_brain"]["default_repair_path"])
        self.assertEqual("operator_only", by_key["directory_split_brain"]["decision_class"])
        self.assertEqual("repair_directory_metadata", by_key["directory_metadata_repair"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["directory_metadata_repair"]["decision_class"])
        self.assertEqual("native-heal/rescan verification", by_key["entry_heal_smoke"]["default_repair_path"])
        self.assertEqual("operator_only", by_key["entry_heal_smoke"]["decision_class"])
        self.assertEqual("refresh_directory_evidence or choose_directory_mdata_source", by_key["directory_ctime_smoke"]["default_repair_path"])
        self.assertEqual("operator_only", by_key["directory_ctime_smoke"]["decision_class"])
        self.assertEqual("repair_file_metadata", by_key["file_metadata_repair"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["file_metadata_repair"]["decision_class"])
        self.assertEqual("cleanup_dead_file_refs", by_key["file_dead_ref_cleanup"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["file_dead_ref_cleanup"]["decision_class"])
        self.assertEqual("cleanup_dead_gfid", by_key["file_handle_ghost_cleanup"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["file_handle_ghost_cleanup"]["decision_class"])
        self.assertEqual("cleanup_stale_glusterfs_index", by_key["stale_glusterfs_index_cleanup"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["stale_glusterfs_index_cleanup"]["decision_class"])
        self.assertEqual("delete_orphaned_symlink_residue", by_key["file_orphaned_symlink_cleanup"]["default_repair_path"])
        self.assertEqual("risky_default", by_key["file_orphaned_symlink_cleanup"]["decision_class"])
        self.assertEqual("cleanup_dead_gfid", by_key["dead_gfid"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["dead_gfid"]["decision_class"])
        self.assertEqual("review_dead_gfid_reference", by_key["dead_gfid_reference"]["default_repair_path"])
        self.assertEqual("chain_follow", by_key["dead_gfid_reference"]["decision_class"])
        self.assertEqual("cleanup_dead_gfid", by_key["post_resolution_ghost_tail"]["default_repair_path"])
        self.assertEqual("safe_default", by_key["post_resolution_ghost_tail"]["decision_class"])
        self.assertEqual("restore_missing_replica / delete_below_quorum_file", by_key["file_symlink"]["default_repair_path"])
        self.assertEqual("chain_follow", by_key["file_symlink"]["decision_class"])
        self.assertIn("quarantine_loser", by_key["file_type_mismatch"]["default_repair_path"])
        self.assertIn("quarantine_both", by_key["file_type_mismatch"]["default_repair_path"])
        self.assertEqual("operator_only", by_key["file_type_mismatch"]["decision_class"])
        self.assertEqual("enable file-metadata repair policy", by_key["file_metadata_only"]["default_repair_path"])
        self.assertIn("quarantine_both", by_key["file_split_brain_tie"]["default_repair_path"])
        self.assertIn("quarantine_both", by_key["directory_split_brain"]["notes"])
        self.assertIn("source-brick", by_key["file_split_brain_majority"]["notes"])

        self.assertIn("Use `restore`", render_repair_matrix())
        self.assertIn("Decision class", render_repair_matrix())
        self.assertIn("auto = majority then mtime", render_repair_matrix())
        self.assertIn("best-guess snapshot", render_repair_matrix())
        self.assertIn("full heal first", render_repair_matrix())
        self.assertIn("scan lag or stale bookkeeping", render_repair_matrix())
        self.assertIn("Avoid `sync`", render_repair_matrix())
        self.assertIn("symlink entries as file subcategories", render_repair_matrix())
        self.assertIn("Orphaned symlink residues can be cleaned", render_repair_matrix())
        self.assertIn("cleanup_dead_file_refs", render_repair_matrix())
        self.assertIn("file_handle_ghost_cleanup", render_repair_matrix())
        self.assertIn("cleanup_stale_glusterfs_index", render_repair_matrix())
        self.assertIn("post_resolution_ghost_tail", render_repair_matrix())
        self.assertIn("entry_heal_smoke", render_repair_matrix())
        self.assertIn("directory_ctime_smoke", render_repair_matrix())
        self.assertIn("directory_gfid_merge", render_repair_matrix())
        self.assertIn("review_directory_children", render_repair_matrix())
        self.assertIn("directory-tie-build first", render_repair_matrix())
        self.assertIn("rename/quarantine the losing tree", render_repair_matrix())
        self.assertIn("review-only", render_repair_matrix())
        self.assertEqual(by_key["directory_restore"], repair_matrix_row("directory_restore"))
        family_rows = repair_matrix_family_rows()
        self.assertIn("entry_heal_smoke", {row["key"] for row in family_rows["smoke"]})
        self.assertIn("directory_ctime_smoke", {row["key"] for row in family_rows["smoke"]})
        self.assertIn("post_resolution_ghost_tail", {row["key"] for row in family_rows["residue"]})
        self.assertEqual("directory_restore", family_rows["directory"][0]["key"])
        self.assertIn("directory_presence_tie", {row["key"] for row in family_rows["directory"]})
        self.assertIn("directory_gfid_merge", {row["key"] for row in family_rows["directory"]})

    def test_manager_script_renders_matrix_json(self) -> None:
        script = REPO_ROOT / "gluster-manager.py"
        completed = subprocess.run(
            [sys.executable, str(script), "repair-matrix", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertIn("rows", payload)
        self.assertGreaterEqual(len(payload["rows"]), 14)
        self.assertEqual("file_restore", payload["rows"][0]["key"])
        self.assertIn("entry_heal_smoke", {row["key"] for row in payload["rows"]})
        self.assertIn("directory_ctime_smoke", {row["key"] for row in payload["rows"]})
