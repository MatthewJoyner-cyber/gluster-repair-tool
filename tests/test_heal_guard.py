# SPDX-License-Identifier: GPL-2.0-only
"""Tests for heal guards."""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from gluster_heal_tool.heal_guard import assess_heal_info_repeat, build_heal_info_snapshot
from gluster_heal_tool.volume import get_heal_info_text
from gluster_heal_tool.status import status_warnings, update_status


class HealGuardTests(unittest.TestCase):
    def test_build_heal_info_snapshot_includes_stable_signature(self) -> None:
        heal_text = """Brick host-a:/srv/gluster/brick-store/gtest
/example/b
/example/a
/example/a
Status: Connected
Number of entries: 3
"""
        with patch("gluster_heal_tool.heal_guard.get_heal_info_text", return_value=heal_text):
            report = build_heal_info_snapshot("gtest")

        self.assertTrue(report["available"])
        self.assertEqual(3, report["entry_count"])
        self.assertEqual(2, report["unique_count"])
        self.assertEqual(["/example/b", "/example/a"], report["sample_paths"])
        self.assertTrue(report["unique_signature"])

    def test_get_heal_info_text_falls_back_to_sudo(self) -> None:
        plain = subprocess.CompletedProcess(
            args=["gluster", "volume", "heal", "gtest", "info"],
            returncode=1,
            stdout="",
            stderr="Permission denied",
        )
        sudo = subprocess.CompletedProcess(
            args=["sudo", "-n", "gluster", "volume", "heal", "gtest", "info"],
            returncode=0,
            stdout="Brick host-a:/srv/gluster/brick-store/gtest\n/example/a\n",
            stderr="",
        )

        with patch("gluster_heal_tool.volume.subprocess.run", side_effect=[plain, sudo]) as mocked:
            heal_text = get_heal_info_text("gtest")

        self.assertEqual("Brick host-a:/srv/gluster/brick-store/gtest\n/example/a\n", heal_text)
        self.assertEqual(2, mocked.call_count)

    def test_assess_heal_info_repeat_counts_unchanged_shapes(self) -> None:
        previous_status = {
            "final_check": {"unique_signature": "shape-a"},
            "final_check_guard": {"signature": "shape-a", "repeat_count": 4},
        }
        current_snapshot = {
            "available": True,
            "unique_signature": "shape-a",
            "unique_count": 4,
        }

        guard = assess_heal_info_repeat(previous_status, current_snapshot, repeat_limit=5)

        self.assertTrue(guard["available"])
        self.assertFalse(guard["changed"])
        self.assertTrue(guard["hit_limit"])
        self.assertEqual(5, guard["repeat_count"])
        self.assertEqual(5, guard["repeat_limit"])

    def test_assess_heal_info_repeat_resets_when_shape_changes(self) -> None:
        previous_status = {
            "final_check": {"unique_signature": "shape-a"},
            "final_check_guard": {"signature": "shape-a", "repeat_count": 2},
        }
        current_snapshot = {
            "available": True,
            "unique_signature": "shape-b",
            "unique_count": 2,
        }

        guard = assess_heal_info_repeat(previous_status, current_snapshot, repeat_limit=3)

        self.assertTrue(guard["available"])
        self.assertTrue(guard["changed"])
        self.assertFalse(guard["hit_limit"])
        self.assertEqual(1, guard["repeat_count"])

    def test_assess_heal_info_repeat_counts_churn(self) -> None:
        previous_status = {
            "final_check": {"unique_signature": "shape-a"},
            "final_check_guard": {
                "signature": "shape-a",
                "repeat_count": 1,
                "churn_count": 9,
            },
        }
        current_snapshot = {
            "available": True,
            "unique_signature": "shape-b",
            "unique_count": 2,
        }

        guard = assess_heal_info_repeat(previous_status, current_snapshot, repeat_limit=3, churn_limit=10)

        self.assertTrue(guard["available"])
        self.assertTrue(guard["changed"])
        self.assertFalse(guard["hit_limit"])
        self.assertTrue(guard["churn_hit_limit"])
        self.assertEqual(1, guard["repeat_count"])
        self.assertEqual(10, guard["churn_limit"])
        self.assertEqual(10, guard["churn_count"])

    def test_status_warnings_include_repeated_heal_shape_limit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / "status.json"
            update_status(
                status_file,
                final_check_guard={
                    "signature": "shape-a",
                    "repeat_count": 5,
                    "repeat_limit": 5,
                    "hit_limit": True,
                },
            )

            warnings = status_warnings(status_file)

        self.assertTrue(any("post-execute heal info shape has not changed" in message for message in warnings))

    def test_status_warnings_include_heal_churn_limit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / "status.json"
            update_status(
                status_file,
                final_check_guard={
                    "signature": "shape-b",
                    "repeat_count": 1,
                    "repeat_limit": 2,
                    "churn_count": 10,
                    "churn_limit": 10,
                    "churn_hit_limit": True,
                },
            )

            warnings = status_warnings(status_file)

        self.assertTrue(any("post-execute heal info shape changed too many times" in message for message in warnings))
