# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for human-readable controller status reporting."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from gluster_heal_tool import cli as package_cli
from gluster_heal_tool.status_report import render_status_report


def _load_manager_main():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module.main


class StatusReportTests(unittest.TestCase):
    def test_render_status_report_shows_saved_evidence_log(self) -> None:
        report = render_status_report({"phase": "evidence-build-built", "evidence_log": "/tmp/evidence.log"})
        self.assertIn("evidence log: /tmp/evidence.log", report)

    def test_render_status_report_includes_graph_checkpoint(self) -> None:
        report = render_status_report(
            {
                "phase": "plan-built",
                "volume": "gtest",
                "mountpoint": "/gtest",
                "graph_cycle_report": "graph-cycle: repeat_count=3, cycle_detected=true",
                "graph_cycle": {
                    "repeat_count": 1,
                    "cycle_detected": False,
                },
            }
        )

        self.assertIn("status: phase=plan-built, volume=gtest, mountpoint=/gtest", report)
        self.assertIn("graph-cycle-status: present=yes", report)
        self.assertIn("graph-cycle: repeat_count=3, cycle_detected=true", report)

    def test_render_status_report_includes_controller_cycle_hint(self) -> None:
        report = render_status_report(
            {
                "phase": "apply-run-execute",
                "volume": "gtest",
                "mountpoint": "/gtest",
                "controller_cycle": {
                    "controller_stop": True,
                    "controller_reason": "graph cycle detected",
                    "controller_next_action": "rebuild-plan",
                    "graph_cycle_present": True,
                    "graph_cycle_repeat_count": 2,
                    "graph_cycle_detected": True,
                    "heal_repeat_count": 5,
                    "heal_repeat_hit_limit": True,
                    "heal_repeat_warning": "post-execute heal info shape has not changed for 5 consecutive checks",
                },
            }
        )

        self.assertIn("controller-cycle: stop=yes, next_action=rebuild-plan, reason=graph cycle detected", report)

    def test_status_report_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        status_payload = {
            "phase": "plan-built",
            "volume": "gtest",
            "mountpoint": "/gtest",
            "graph_cycle_report": "graph-cycle: repeat_count=3, cycle_detected=true",
            "graph_cycle": {
                "repeat_count": 1,
                "cycle_detected": False,
            },
            "controller_cycle": {
                "controller_stop": True,
                "controller_reason": "graph cycle detected",
                "controller_next_action": "rebuild-plan",
                "graph_cycle_present": True,
                "graph_cycle_repeat_count": 2,
                "graph_cycle_detected": True,
                "heal_repeat_count": 5,
                "heal_repeat_hit_limit": True,
                "heal_repeat_warning": "post-execute heal info shape has not changed for 5 consecutive checks",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(["status-report", "--status-file", str(status_path)])
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(["status-report", "--status-file", str(status_path)])

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual(
            "status: phase=plan-built, volume=gtest, mountpoint=/gtest",
            package_stdout.getvalue().splitlines()[0],
        )
        self.assertEqual(
            "status: phase=plan-built, volume=gtest, mountpoint=/gtest",
            manager_stdout.getvalue().splitlines()[0],
        )
        self.assertIn("graph-cycle-status: present=yes", package_stdout.getvalue())
        self.assertIn("graph-cycle-status: present=yes", manager_stdout.getvalue())
        self.assertIn("graph-cycle: repeat_count=3, cycle_detected=true", package_stdout.getvalue())
        self.assertIn("graph-cycle: repeat_count=3, cycle_detected=true", manager_stdout.getvalue())
        self.assertIn("controller-cycle: stop=yes, next_action=rebuild-plan, reason=graph cycle detected", package_stdout.getvalue())
        self.assertIn("controller-cycle: stop=yes, next_action=rebuild-plan, reason=graph cycle detected", manager_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
