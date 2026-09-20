# SPDX-License-Identifier: GPL-2.0-only
"""Tests for apply reporting helpers."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gluster_heal_tool.apply_reporting import build_preflight_report
from gluster_heal_tool.apply_reporting import collect_temp_mount_verification_paths
from gluster_heal_tool.apply_reporting import needs_acl_temp_mount
from gluster_heal_tool.apply_reporting import render_apply_summary
from gluster_heal_tool.apply_reporting import summarize_apply_results
from gluster_heal_tool.models import ApplyActionResult
from gluster_heal_tool.models import ApplyStep


class ApplyReportingTests(unittest.TestCase):
    def test_preflight_uses_existing_parent_for_new_backup_root(self) -> None:
        with TemporaryDirectory() as tmpdir:
            requested = Path(tmpdir) / "new" / "backup-root"
            report = build_preflight_report(
                {
                    "backup_root": str(requested),
                    "backup_mode": "required",
                    "estimates": {},
                }
            )

        disk = report["backup_storage"]["disk"]
        self.assertEqual(str(requested), disk["path"])
        self.assertEqual(tmpdir, disk["disk_usage_path"])

    def test_needs_acl_temp_mount_detects_acl_metadata_steps(self) -> None:
        result = ApplyActionResult(
            action_id="action-1",
            logical_path="/repair-canary/gtest3/payload.txt",
            action_type="repair_posix_metadata",
            execution_mode="plan",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="planned",
            steps=[ApplyStep(step_id="step-1", step_type="apply_posix_metadata_acl")],
        )

        self.assertTrue(needs_acl_temp_mount([result]))

    def test_needs_acl_temp_mount_ignores_non_acl_metadata_steps(self) -> None:
        result = ApplyActionResult(
            action_id="action-2",
            logical_path="/repair-canary/gtest3/payload.txt",
            action_type="repair_posix_metadata",
            execution_mode="plan",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="planned",
            steps=[ApplyStep(step_id="step-1", step_type="apply_posix_metadata_mode")],
        )

        self.assertFalse(needs_acl_temp_mount([result]))

    def test_marker_clear_verifies_the_repaired_logical_path(self) -> None:
        result = ApplyActionResult(
            action_id="action-marker-clear",
            logical_path="/repair-canary/gtest3/alpha/payload.txt",
            action_type="clear_split_brain_marker",
            execution_mode="execute",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="completed",
            steps=[
                ApplyStep(
                    step_id="resolve-marker",
                    step_type="resolve_split_brain_gluster_cli",
                )
            ],
        )

        self.assertEqual(
            ["alpha/payload.txt"],
            collect_temp_mount_verification_paths([result]),
        )

    def test_safe_default_directory_recreate_counts_as_ready_directory(self) -> None:
        result = ApplyActionResult(
            action_id="action-3",
            logical_path="/repair-canary/gtest3/alpha/beta/gamma",
            action_type="repair_directory_metadata",
            execution_mode="plan",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="planned",
        )

        summary = summarize_apply_results([result])
        rendered = render_apply_summary(summary)

        self.assertEqual(1, summary["ready_repairs"])
        self.assertEqual(1, summary["directory_repairs"])
        self.assertIn("Apply ready: 1 ready repairs", rendered)
        self.assertIn("1 directories", rendered)


if __name__ == "__main__":
    unittest.main()
