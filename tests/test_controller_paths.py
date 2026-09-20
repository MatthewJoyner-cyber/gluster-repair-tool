# SPDX-License-Identifier: GPL-2.0-only
"""Tests for controller-local work-root helpers."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool.controller_paths import (
    default_backup_archive_dir,
    default_brick_layout_path,
    default_canary_stage_local_path,
    default_health_report_path,
    default_heal_info_root,
    default_repair_run_dir,
    default_stage_local_path,
    default_status_file_path,
    default_temp_mount_root,
    default_work_root,
)


class ControllerPathTests(unittest.TestCase):
    def setUp(self) -> None:
        clean = {key: value for key, value in os.environ.items()
                 if key not in {"GLUSTER_REPAIR_WORK_ROOT", "GLUSTER_REPAIR_BACKUP_DIR", "XDG_STATE_HOME"}}
        self.environment = patch.dict(os.environ, clean, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_fresh_home_and_xdg_defaults(self) -> None:
        with patch.dict(os.environ, {"HOME": "/tmp/example-home", "XDG_STATE_HOME": "/tmp/example-state"}):
            self.assertEqual(Path("/tmp/example-state/gluster-repair/work"), default_work_root())
            self.assertEqual(Path("/tmp/example-state/gluster-repair/backups"), default_backup_archive_dir())
        with patch.dict(os.environ, {"HOME": "/tmp/example-home", "XDG_STATE_HOME": "relative-state"}):
            self.assertEqual(Path("/tmp/example-home/.local/state/gluster-repair/work"), default_work_root())

    def test_explicit_private_storage_overrides(self) -> None:
        with patch.dict(os.environ, {"HOME": "/tmp/example-home", "GLUSTER_REPAIR_WORK_ROOT": "~/work", "GLUSTER_REPAIR_BACKUP_DIR": "~/backups"}):
            self.assertEqual(Path("/tmp/example-home/work"), default_work_root())
            self.assertEqual(Path("/tmp/example-home/backups"), default_backup_archive_dir())

    def test_default_work_root_uses_user_state(self) -> None:
        expected = Path.home() / ".local/state/gluster-repair/work"
        self.assertEqual(expected, default_work_root())
        self.assertEqual(expected / "gluster-repair-status.json", default_status_file_path())
        self.assertEqual(expected / "heal-info", default_heal_info_root())
        self.assertEqual(expected / "gluster-repair", default_temp_mount_root())
        self.assertEqual(expected / "health-check" / "gtest-health-check.json", default_health_report_path("gtest"))
        self.assertEqual(expected / "brick-layout" / "gtest-brick-layout.json", default_brick_layout_path("gtest"))
        self.assertTrue(default_stage_local_path("/nested/path").startswith(str(expected / "gluster-repair-stage")))
        self.assertTrue(default_canary_stage_local_path("repair-canary", "payload.txt").startswith(str(expected / "repair-canary")))

    def test_default_repair_run_dir_uses_timestamped_runs_tree(self) -> None:
        started_at = datetime(2026, 6, 24, 12, 34, 56, tzinfo=timezone.utc)

        run_dir = default_repair_run_dir("gtest", "run-123", started_at)

        self.assertEqual(
            Path.home() / ".local/state/gluster-repair/work/runs/20260624T123456Z-gtest-run-123",
            run_dir,
        )

    def test_work_root_env_override_is_respected(self) -> None:
        with patch.dict(os.environ, {"GLUSTER_REPAIR_WORK_ROOT": "/tmp/custom-gluster-work"}, clear=False):
            self.assertEqual(Path("/tmp/custom-gluster-work"), default_work_root())
            self.assertEqual(
                Path("/tmp/custom-gluster-work") / "gluster-repair-status.json",
                default_status_file_path(),
            )
            self.assertEqual(
                Path("/tmp/custom-gluster-work") / "heal-info",
                default_heal_info_root(),
            )
            self.assertEqual(
                Path("/tmp/custom-gluster-work") / "brick-layout" / "gtest-brick-layout.json",
                default_brick_layout_path("gtest"),
            )
