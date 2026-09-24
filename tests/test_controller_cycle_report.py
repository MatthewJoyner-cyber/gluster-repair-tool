# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the combined controller cycle report."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from gluster_heal_tool import cli as package_cli
from gluster_heal_tool.controller_cycle_report import build_controller_cycle_status, render_controller_cycle_report


def _load_manager_main():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module.main


class ControllerCycleReportTests(unittest.TestCase):
    def test_render_controller_cycle_report_combines_graph_and_heal_guards(self) -> None:
        report = render_controller_cycle_report(
            {
                "graph_cycle_report": "graph-cycle: repeat_count=2, cycle_detected=true",
                "graph_cycle": {
                    "repeat_count": 2,
                    "cycle_detected": True,
                },
                "final_check_guard": {
                    "repeat_count": 5,
                    "hit_limit": True,
                    "warning": "post-execute heal info shape has not changed for 5 consecutive checks",
                },
            }
        )

        self.assertIn(
            "controller-cycle: stop=yes, next_action=rebuild-plan, reason=heal info repeat limit reached; graph cycle detected",
            report,
        )
        self.assertIn("controller-cycle-graph: present=yes, repeat_count=2, cycle_detected=true", report)
        self.assertIn("graph-cycle: repeat_count=2, cycle_detected=true", report)
        self.assertIn("controller-cycle-heal: repeat_count=5, churn_count=0, hit_limit=true", report)

    def test_build_controller_cycle_status_packages_snapshot(self) -> None:
        payload = build_controller_cycle_status(
            {
                "graph_cycle_report": "graph-cycle: repeat_count=2, cycle_detected=true",
                "graph_cycle": {
                    "repeat_count": 2,
                    "cycle_detected": True,
                },
                "final_check_guard": {
                    "repeat_count": 5,
                    "hit_limit": True,
                    "warning": "post-execute heal info shape has not changed for 5 consecutive checks",
                },
            }
        )

        self.assertIn("controller_cycle", payload)
        self.assertIn("controller_cycle_report", payload)
        self.assertEqual("controller-cycle: stop=yes, next_action=rebuild-plan, reason=heal info repeat limit reached; graph cycle detected", payload["controller_cycle_report"].splitlines()[0])
        self.assertTrue(payload["controller_cycle"]["controller_stop"])
        self.assertEqual("rebuild-plan", payload["controller_cycle"]["controller_next_action"])

    def test_controller_cycle_next_action_distinguishes_graph_only_stop(self) -> None:
        payload = build_controller_cycle_status(
            {
                "graph_cycle_report": "graph-cycle: repeat_count=2, cycle_detected=true",
                "graph_cycle": {
                    "repeat_count": 2,
                    "cycle_detected": True,
                },
            }
        )

        self.assertTrue(payload["controller_cycle"]["controller_stop"])
        self.assertEqual("review-graph-cycle", payload["controller_cycle"]["controller_next_action"])
        self.assertEqual(
            "controller-cycle: stop=yes, next_action=review-graph-cycle, reason=graph cycle detected",
            payload["controller_cycle_report"].splitlines()[0],
        )

    def test_controller_cycle_next_action_distinguishes_heal_only_stop(self) -> None:
        payload = build_controller_cycle_status(
            {
                "final_check_guard": {
                    "repeat_count": 5,
                    "hit_limit": True,
                    "warning": "post-execute heal info shape has not changed for 5 consecutive checks",
                },
            }
        )

        self.assertTrue(payload["controller_cycle"]["controller_stop"])
        self.assertEqual("investigate-repair-gap", payload["controller_cycle"]["controller_next_action"])
        self.assertEqual(
            "controller-cycle: stop=yes, next_action=investigate-repair-gap, reason=heal info repeat limit reached",
            payload["controller_cycle_report"].splitlines()[0],
        )

    def test_render_controller_cycle_report_prefers_stored_report(self) -> None:
        report = render_controller_cycle_report(
            {
                "controller_cycle_report": "controller-cycle: stop=no, reason=controller can continue",
                "controller_cycle": {
                    "controller_stop": True,
                    "controller_reason": "graph cycle detected",
                },
                "graph_cycle_report": "graph-cycle: repeat_count=2, cycle_detected=true",
                "graph_cycle": {
                    "repeat_count": 2,
                    "cycle_detected": True,
                },
            }
        )

        self.assertEqual("controller-cycle: stop=no, reason=controller can continue", report)

    def test_controller_cycle_report_command_renders_on_both_surfaces(self) -> None:
        manager_main = _load_manager_main()
        status_payload = {
            "graph_cycle_report": "graph-cycle: repeat_count=2, cycle_detected=true",
            "graph_cycle": {
                "repeat_count": 2,
                "cycle_detected": True,
            },
            "final_check_guard": {
                "repeat_count": 5,
                "hit_limit": True,
                "warning": "post-execute heal info shape has not changed for 5 consecutive checks",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(["controller-cycle-report", "--status-file", str(status_path)])
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(["controller-cycle-report", "--status-file", str(status_path)])

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertEqual(
            "controller-cycle: stop=yes, next_action=rebuild-plan, reason=heal info repeat limit reached; graph cycle detected",
            package_stdout.getvalue().splitlines()[0],
        )
        self.assertEqual(
            "controller-cycle: stop=yes, next_action=rebuild-plan, reason=heal info repeat limit reached; graph cycle detected",
            manager_stdout.getvalue().splitlines()[0],
        )
        self.assertIn("controller-cycle-graph: present=yes, repeat_count=2, cycle_detected=true", package_stdout.getvalue())
        self.assertIn("controller-cycle-graph: present=yes, repeat_count=2, cycle_detected=true", manager_stdout.getvalue())
        self.assertIn("controller-cycle-heal: repeat_count=5, churn_count=0, hit_limit=true", package_stdout.getvalue())
        self.assertIn("controller-cycle-heal: repeat_count=5, churn_count=0, hit_limit=true", manager_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
