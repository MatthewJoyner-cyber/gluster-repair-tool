# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for explicit heal-performance controls."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gluster_heal_tool.cli import build_parser
from gluster_heal_tool.heal_performance import (
    apply_tuning,
    build_heal_performance_report,
    build_tuning_preview,
    execute_heal_performance,
)


READY_HEALTH = {
    "summary": {"ready": True},
    "checks": [
        {
            "kind": "brick_free_space",
            "host": "host-a",
            "path": "/bricks/gtest",
            "ok": True,
            "available_bytes": 100,
            "payload": ["", "", "", "100", "10%"],
        },
        {
            "kind": "brick_free_inodes",
            "host": "host-a",
            "path": "/bricks/gtest",
            "ok": True,
            "available_inodes": 100,
            "payload": ["", "", "", "100", "10%"],
        },
    ],
}


class HealPerformanceTests(unittest.TestCase):
    def test_report_detects_version_and_optional_capabilities(self) -> None:
        with patch("gluster_heal_tool.heal_performance.get_gluster_version", return_value="11.1"), patch(
            "gluster_heal_tool.heal_performance.get_volume_option",
            side_effect=lambda volume, option: "on" if option == "cluster.granular-entry-heal" else "8",
        ), patch(
            "gluster_heal_tool.heal_performance.get_heal_statistics",
            return_value="Number of entries: 4\n",
        ):
            report = build_heal_performance_report(
                "gtest",
                ssh_user="repair",
                connect_timeout=5.0,
                health_report=READY_HEALTH,
            )

        capabilities = report["capabilities"]
        self.assertEqual("11.1", capabilities["controller_gluster"]["detected_version"])
        self.assertTrue(capabilities["pending_index_heal"]["available"])
        self.assertFalse(capabilities["full_namespace_heal"]["available"])
        self.assertFalse(capabilities["per_file_split_brain_resolution"]["available"])
        self.assertEqual("on", capabilities["granular_entry_heal"]["current"])
        self.assertIn("Number of entries: 4", capabilities["pending_backlog_statistics"]["output"])
        self.assertNotIn("checks", report["health"])
        self.assertTrue(report["health"]["summary"]["ready"])

    def test_unavailable_tuning_option_is_a_warning_not_a_full_heal_blocker(self) -> None:
        def fake_get_option(volume: str, option: str) -> str:
            if option == "cluster.granular-entry-heal":
                return "on"
            if option == "cluster.shd-max-threads":
                raise RuntimeError("option unavailable")
            return "8"

        with patch("gluster_heal_tool.heal_performance.get_gluster_version", return_value="11.1"), patch(
            "gluster_heal_tool.heal_performance.get_volume_option",
            side_effect=fake_get_option,
        ), patch(
            "gluster_heal_tool.heal_performance.get_heal_statistics",
            return_value="Number of entries: 0\n",
        ):
            report = build_heal_performance_report(
                "gtest",
                ssh_user="repair",
                connect_timeout=5.0,
                health_report=READY_HEALTH,
            )

        self.assertEqual([], report["errors"])
        self.assertIn("option unavailable", report["warnings"])

    def test_tuning_failure_rolls_back_prior_changes(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def fake_set(volume: str, option: str, value: str) -> None:
            calls.append((volume, option, value))
            if option == "cluster.shd-wait-qlength" and value == "200":
                raise RuntimeError("set failed")

        preview = {
            "changes": {
                "cluster.shd-max-threads": "20",
                "cluster.shd-wait-qlength": "200",
            },
            "rollback": {
                "cluster.shd-max-threads": "8",
                "cluster.shd-wait-qlength": "128",
            },
            "errors": [],
        }
        with patch("gluster_heal_tool.heal_performance.set_volume_option", side_effect=fake_set):
            result = apply_tuning(preview, "gtest")

        self.assertEqual(
            [
                ("gtest", "cluster.shd-max-threads", "20"),
                ("gtest", "cluster.shd-wait-qlength", "200"),
                ("gtest", "cluster.shd-max-threads", "8"),
            ],
            calls,
        )
        self.assertEqual([], result["applied"])
        self.assertEqual(["cluster.shd-max-threads"], result["rolled_back"])
        self.assertIn("set failed", result["errors"][0])

    def test_default_tuning_rollback_uses_volume_reset(self) -> None:
        set_calls: list[tuple[str, str, str]] = []
        reset_calls: list[tuple[str, str]] = []

        def fake_set(volume: str, option: str, value: str) -> None:
            set_calls.append((volume, option, value))
            if option == "cluster.shd-wait-qlength":
                raise RuntimeError("set failed")

        preview = {
            "changes": {
                "cluster.shd-max-threads": "20",
                "cluster.shd-wait-qlength": "200",
            },
            "rollback": {
                "cluster.shd-max-threads": "(DEFAULT)",
                "cluster.shd-wait-qlength": "128",
            },
            "rollback_actions": {
                "cluster.shd-max-threads": {
                    "operation": "reset",
                    "command_preview": [
                        "gluster",
                        "volume",
                        "reset",
                        "gtest",
                        "cluster.shd-max-threads",
                    ],
                }
            },
            "errors": [],
        }
        with patch("gluster_heal_tool.heal_performance.set_volume_option", side_effect=fake_set), patch(
            "gluster_heal_tool.heal_performance.reset_volume_option",
            side_effect=lambda volume, option: reset_calls.append((volume, option)),
        ):
            result = apply_tuning(preview, "gtest")

        self.assertEqual(
            [
                ("gtest", "cluster.shd-max-threads", "20"),
                ("gtest", "cluster.shd-wait-qlength", "200"),
            ],
            set_calls,
        )
        self.assertEqual([("gtest", "cluster.shd-max-threads")], reset_calls)
        self.assertEqual([], result["applied"])
        self.assertEqual(
            "reset",
            result["rollback_actions"]["cluster.shd-max-threads"]["operation"],
        )

    def test_full_heal_requires_identified_capability(self) -> None:
        report = {
            "errors": [],
            "warnings": [],
            "health": READY_HEALTH,
            "options": [],
            "capabilities": {"full_namespace_heal": {"available": False}},
        }
        with patch("gluster_heal_tool.heal_performance.build_heal_performance_report", return_value=report), patch(
            "gluster_heal_tool.heal_performance.run_full_heal"
        ) as run_full:
            result = execute_heal_performance(
                "gtest",
                ssh_user="repair",
                connect_timeout=5.0,
                requested={},
                full_heal_mode="once",
                execute=True,
                batch=True,
            )

        run_full.assert_not_called()
        self.assertIn("live command qualification", result["errors"][0])

    def test_new_numeric_version_does_not_claim_unproven_commands(self) -> None:
        with patch("gluster_heal_tool.heal_performance.get_gluster_version", return_value="12.0"), patch(
            "gluster_heal_tool.heal_performance.get_volume_option", return_value="on"
        ), patch("gluster_heal_tool.heal_performance.get_heal_statistics", return_value="Number of entries: 0\n"):
            report = build_heal_performance_report(
                "gtest", ssh_user="repair", connect_timeout=5.0, health_report=READY_HEALTH,
            )
        self.assertTrue(report["capabilities"]["controller_gluster"]["available"])
        self.assertFalse(report["capabilities"]["controller_gluster"]["qualified_profile"])
        for name in ("pending_index_heal", "full_namespace_heal", "per_file_split_brain_resolution"):
            self.assertFalse(report["capabilities"][name]["available"])

    def test_full_heal_launches_once_only_with_explicit_authority(self) -> None:
        report = {
            "errors": [],
            "warnings": [],
            "health": READY_HEALTH,
            "options": [],
            "capabilities": {"full_namespace_heal": {"available": True}},
        }
        with patch("gluster_heal_tool.heal_performance.build_heal_performance_report", return_value=report), patch(
            "gluster_heal_tool.heal_performance.run_full_heal"
        ) as run_full:
            result = execute_heal_performance(
                "gtest",
                ssh_user="repair",
                connect_timeout=5.0,
                requested={},
                full_heal_mode="once",
                execute=True,
                batch=True,
            )

        run_full.assert_called_once_with("gtest")
        self.assertTrue(result["full_heal"]["launched"])

    def test_full_heal_already_run_never_launches_another(self) -> None:
        report = {
            "errors": [],
            "warnings": [],
            "health": READY_HEALTH,
            "options": [],
            "capabilities": {"full_namespace_heal": {"available": True}},
        }
        with patch("gluster_heal_tool.heal_performance.build_heal_performance_report", return_value=report), patch(
            "gluster_heal_tool.heal_performance.run_full_heal"
        ) as run_full:
            result = execute_heal_performance(
                "gtest",
                ssh_user="repair",
                connect_timeout=5.0,
                requested={},
                full_heal_mode="already-run",
                execute=True,
                batch=True,
            )

        run_full.assert_not_called()
        self.assertEqual("already-run", result["full_heal"]["mode"])
        self.assertFalse(result["full_heal"]["launched"])

    def test_full_heal_launch_failure_is_reported(self) -> None:
        report = {
            "errors": [],
            "warnings": [],
            "health": READY_HEALTH,
            "options": [],
            "capabilities": {"full_namespace_heal": {"available": True}},
        }
        with patch("gluster_heal_tool.heal_performance.build_heal_performance_report", return_value=report), patch(
            "gluster_heal_tool.heal_performance.run_full_heal",
            side_effect=RuntimeError("self-heal daemon is unavailable"),
        ):
            result = execute_heal_performance(
                "gtest",
                ssh_user="repair",
                connect_timeout=5.0,
                requested={},
                full_heal_mode="once",
                execute=True,
                batch=True,
            )

        self.assertFalse(result["full_heal"]["launched"])
        self.assertIn("full heal launch failed", result["errors"][0])

    def test_tuning_preview_rejects_out_of_range_and_unready_health(self) -> None:
        report = {
            "warnings": [],
            "options": [{"option": "cluster.shd-max-threads", "current": "8"}],
            "health": {"summary": {"ready": False}},
        }
        preview = build_tuning_preview(
            report,
            {
                "cluster.shd-max-threads": 65,
                "cluster.shd-wait-qlength": 200,
            },
        )

        self.assertIn("cluster.shd-max-threads must be between 1 and 64, got 65", preview["errors"])
        self.assertIn("health is not ready; tuning apply is blocked", preview["errors"])

    def test_parser_rejects_conflicting_full_heal_declarations(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "heal-performance",
                    "--volume",
                    "gtest",
                    "--full-heal-once",
                    "--full-heal-already-run",
                ]
            )


if __name__ == "__main__":
    unittest.main()
