# SPDX-License-Identifier: GPL-2.0-only
"""Tests for snapshot/reversibility gating."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool.apply import build_preflight_report
from gluster_heal_tool.apply_binding import bind_manifest, bind_plan, bind_apply
from gluster_heal_tool import cli as package_cli
from gluster_heal_tool.cli import build_parser
from gluster_heal_tool.status import render_reversibility_summary, snapshot_gate_warnings


def _load_manager_module():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module


def _load_manager_main():
    return _load_manager_module().main


_BOUND_VOLUME_INFO = "Volume Name: gtest\nType: Replicate\nBrick1: node-a:/srv/brick\nBrick2: node-b:/srv/brick\n"


def _bind_test_payload(root: Path, payload: dict) -> None:
    manifest = {"schema_version": 1, "objects": {}}
    bind_manifest(manifest, volume="gtest", bricks=[
        {"host": host, "path": "/srv/brick", "role": "data"} for host in ("node-a", "node-b")
    ])
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    plan = {"schema_version": 1, "actions": []}
    bind_plan(plan, manifest, manifest_path)
    plan_path = root / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    bind_apply(payload, plan, plan_path)


class ReversibilityGateTests(unittest.TestCase):
    def test_preflight_report_records_snapshot_requirement(self) -> None:
        report = build_preflight_report(
            {
                "require_snapshot": True,
                "snapshot_ack": False,
                "backup_mode": "required",
                "backup_root": "",
                "estimates": {},
            }
        )

        self.assertIn("reversibility", report)
        self.assertTrue(report["reversibility"]["snapshot_required"])
        self.assertFalse(report["reversibility"]["snapshot_acknowledged"])
        self.assertTrue(any("snapshot confirmation is required" in message for message in report["warnings"]))

    def test_snapshot_gate_warnings_require_acknowledged_snapshot(self) -> None:
        warnings = snapshot_gate_warnings({"reversibility": {"snapshot_required": True, "snapshot_acknowledged": False}})
        self.assertTrue(any("snapshot confirmation is required" in message for message in warnings))

        self.assertFalse(
            snapshot_gate_warnings({"reversibility": {"snapshot_required": True, "snapshot_acknowledged": True}})
        )

    def test_render_reversibility_summary_describes_rollback_readiness(self) -> None:
        summary = render_reversibility_summary(
            {
                "reversibility": {
                    "snapshot_required": True,
                    "snapshot_acknowledged": True,
                    "snapshot_inventory": {"available": True, "count": 2, "names": ["snap-a", "snap-b"]},
                }
            }
        )
        self.assertIn("snapshot gate required", summary)
        self.assertIn("snapshot acknowledged", summary)
        self.assertIn("snapshot inventory 2", summary)

    def test_parser_exposes_snapshot_gate_flags(self) -> None:
        parser = build_parser()
        run_args = parser.parse_args(
            [
                "apply-run",
                "--apply-in",
                "/tmp/apply.json",
                "--require-snapshot",
                "--snapshot-ack",
            ]
        )
        self.assertTrue(run_args.require_snapshot)
        self.assertTrue(run_args.snapshot_ack)

        preflight_args = parser.parse_args(
            [
                "apply-preflight",
                "--apply-in",
                "/tmp/apply.json",
                "--preflight-out",
                "/tmp/preflight.json",
                "--require-snapshot",
                "--snapshot-ack",
            ]
        )
        self.assertTrue(preflight_args.require_snapshot)
        self.assertTrue(preflight_args.snapshot_ack)

        health_args = parser.parse_args(
            [
                "health-check",
                "--volume",
                "gtest",
                "--health-out",
                "/tmp/health.json",
                "--require-snapshot",
                "--snapshot-ack",
            ]
        )
        self.assertTrue(health_args.require_snapshot)
        self.assertTrue(health_args.snapshot_ack)

        manager_args = parser.parse_args(
            [
                "manager-preflight",
                "--volume",
                "gtest",
                "--preflight-out",
                "/tmp/manager-preflight.json",
                "--require-snapshot",
                "--snapshot-ack",
            ]
        )
        self.assertTrue(manager_args.require_snapshot)
        self.assertTrue(manager_args.snapshot_ack)

    def test_apply_run_blocks_when_controller_cycle_says_stop(self) -> None:
        manager_module = _load_manager_module()
        manager_main = manager_module.main
        apply_payload = {
            "schema_version": 1,
            "execution_mode": "dry-run",
            "backup_root": "",
            "backup_mode": "required",
            "batch": True,
            "decision_file": "",
            "estimates": {},
            "warnings": [],
            "actions": [],
        }
        status_payload = {
            "volume": "gtest",
            "controller_cycle": {
                "controller_stop": True,
                "controller_reason": "graph cycle detected",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            apply_path = Path(tmpdir) / "apply.json"
            status_path = Path(tmpdir) / "status.json"
            _bind_test_payload(Path(tmpdir), apply_payload)
            apply_path.write_text(json.dumps(apply_payload), encoding="utf-8")
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            package_stdout = io.StringIO()
            package_stderr = io.StringIO()
            manager_stdout = io.StringIO()
            manager_stderr = io.StringIO()
            with (contextlib.redirect_stdout(package_stdout), contextlib.redirect_stderr(package_stderr),
                  patch.object(package_cli, "get_volume_info", return_value=_BOUND_VOLUME_INFO),
                  patch.object(package_cli, "get_heal_settings", return_value={})):
                package_rc = package_cli.main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(status_path),
                        "--execute",
                        "--skip-health-check",
                        "--allow-heal-on",
                        "-y",
                    ]
                )
            with (contextlib.redirect_stdout(manager_stdout), contextlib.redirect_stderr(manager_stderr),
                  patch.object(package_cli, "get_volume_info", return_value=_BOUND_VOLUME_INFO),
                  patch.object(package_cli, "get_heal_settings", return_value={}),
                  patch.object(package_cli, "execute_apply_results", return_value={}) as manager_execute,
                  patch.object(package_cli, "write_execute_report")):
                manager_rc = manager_main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(status_path),
                        "--run-dir",
                        str(Path(tmpdir) / "manager-run"),
                        "--execute",
                        "--skip-health-check",
                        "--allow-heal-on",
                        "-y",
                    ]
                )

        self.assertEqual(2, package_rc)
        self.assertEqual(2, manager_rc)
        manager_execute.assert_not_called()
        self.assertIn("ERROR: execution is blocked by the saved controller-cycle decision", package_stderr.getvalue())
        self.assertIn("ERROR: execution is blocked by the saved controller-cycle decision", manager_stderr.getvalue())

    def test_apply_run_uses_live_heal_state_not_stale_status(self) -> None:
        manager_module = _load_manager_module()
        apply_payload = {
            "schema_version": 1,
            "execution_mode": "dry-run",
            "backup_root": "",
            "backup_mode": "required",
            "batch": True,
            "decision_file": "",
            "estimates": {},
            "warnings": [],
            "actions": [
                {
                    "action_id": "a1",
                    "logical_path": "/example/path",
                    "action_type": "repair_file",
                    "execution_mode": "dry-run",
                    "backup_root": "",
                    "backup_mode": "required",
                    "batch": True,
                    "status": "planned",
                    "depends_on": [],
                    "steps": [],
                    "revert_steps": [],
                    "backup_artifacts": [],
                    "revert_dirs_to_create": [],
                    "estimated_stage_bytes": 0,
                    "estimated_backup_bytes": 0,
                    "estimated_unknown_backup_items": 0,
                    "decision": {},
                    "notes": ["repair strategy: restore_missing_replica"],
                }
            ],
        }
        stale_status = {"volume": "gtest", "heal": {"all_off": False}}
        live_off_settings = {
            "cluster.self-heal-daemon": "off",
            "cluster.data-self-heal": "off",
            "cluster.metadata-self-heal": "off",
            "cluster.entry-self-heal": "off",
        }
        live_off_summary = {"all_off": True, "all_on": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            apply_path = Path(tmpdir) / "apply.json"
            package_status_path = Path(tmpdir) / "package-status.json"
            manager_status_path = Path(tmpdir) / "manager-status.json"
            _bind_test_payload(Path(tmpdir), apply_payload)
            apply_path.write_text(json.dumps(apply_payload), encoding="utf-8")
            package_status_path.write_text(json.dumps(stale_status), encoding="utf-8")
            manager_status_path.write_text(json.dumps(stale_status), encoding="utf-8")

            package_stdout = io.StringIO()
            package_stderr = io.StringIO()
            with (
                patch.object(package_cli, "get_volume_info", return_value=_BOUND_VOLUME_INFO),
                patch.object(package_cli, "get_heal_settings", return_value=live_off_settings) as package_settings,
                patch.object(package_cli, "summarize_heal_settings", return_value=live_off_summary),
                patch.object(package_cli, "_refresh_post_execute_heal_preserving_settings", return_value=({}, {})),
                contextlib.redirect_stdout(package_stdout),
                contextlib.redirect_stderr(package_stderr),
            ):
                package_rc = package_cli.main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(package_status_path),
                        "--run-dir",
                        str(Path(tmpdir) / "package-run"),
                        "--execute",
                        "--skip-health-check",
                        "-y",
                    ]
                )

            manager_stdout = io.StringIO()
            manager_stderr = io.StringIO()
            with (
                patch.object(package_cli, "get_volume_info", return_value=_BOUND_VOLUME_INFO),
                patch.object(package_cli, "get_heal_settings", return_value=live_off_settings) as manager_settings,
                patch.object(package_cli, "summarize_heal_settings", return_value=live_off_summary),
                patch.object(package_cli, "_refresh_post_execute_heal_preserving_settings", return_value=({}, {})),
                contextlib.redirect_stdout(manager_stdout),
                contextlib.redirect_stderr(manager_stderr),
            ):
                manager_rc = manager_module.main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(manager_status_path),
                        "--run-dir",
                        str(Path(tmpdir) / "manager-run"),
                        "--execute",
                        "--skip-health-check",
                        "-y",
                    ]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        package_settings.assert_called_once_with("gtest")
        manager_settings.assert_called_once_with("gtest")
        self.assertNotIn("execution requires heal off", package_stderr.getvalue())
        self.assertNotIn("execution requires heal off", manager_stderr.getvalue())

    def test_plan_build_clears_saved_controller_cycle(self) -> None:
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            plan_path = Path(tmpdir) / "plan.json"
            status_path = Path(tmpdir) / "status.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "objects": {
                            "example/file": {
                                "logical_path": "example/file",
                                "object_type": "file",
                                "depth": 1,
                                "raw_entries": [],
                                "source_hosts": [],
                                "parent_gfids": [],
                                "file_gfids": [],
                                "gfids": [],
                                "child_names": [],
                                "observations": {},
                                "dead_gfids": [],
                                "unresolved_entries": [],
                                "aliases": [],
                                "notes": [],
                                "children": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            status_path.write_text(
                json.dumps(
                    {
                        "controller_cycle": {
                            "controller_stop": True,
                            "controller_reason": "graph cycle detected",
                        }
                    }
                ),
                encoding="utf-8",
            )

            package_rc = package_cli.main(
                [
                    "plan-build",
                    "--manifest-in",
                    str(manifest_path),
                    "--plan-out",
                    str(plan_path),
                    "--status-file",
                    str(status_path),
                    "--mountpoint",
                    "/testvol",
                ]
            )

            self.assertEqual(0, package_rc)
            updated_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual({}, updated_status.get("controller_cycle"))
            self.assertEqual("", updated_status.get("controller_cycle_report"))

    def test_apply_run_dry_run_uses_apply_artifact_controller_cycle(self) -> None:
        manager_main = _load_manager_main()
        apply_payload = {
            "schema_version": 1,
            "execution_mode": "dry-run",
            "backup_root": "",
            "backup_mode": "required",
            "batch": True,
            "decision_file": "",
            "controller_cycle": {
                "controller_stop": True,
                "controller_reason": "graph cycle detected",
                "controller_next_action": "review-graph-cycle",
            },
            "estimates": {},
            "warnings": [],
            "actions": [
                {
                    "action_id": "a1",
                    "logical_path": "/example/path",
                    "action_type": "repair_file",
                    "execution_mode": "dry-run",
                    "backup_root": "",
                    "backup_mode": "required",
                    "batch": True,
                    "status": "planned",
                    "depends_on": [],
                    "steps": [],
                    "revert_steps": [],
                    "backup_artifacts": [],
                    "revert_dirs_to_create": [],
                    "estimated_stage_bytes": 0,
                    "estimated_backup_bytes": 0,
                    "estimated_unknown_backup_items": 0,
                    "decision": {},
                    "notes": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            apply_path = Path(tmpdir) / "apply.json"
            status_path = Path(tmpdir) / "status.json"
            apply_path.write_text(json.dumps(apply_payload), encoding="utf-8")
            status_path.write_text("{}", encoding="utf-8")

            package_stdout = io.StringIO()
            manager_stdout = io.StringIO()
            with contextlib.redirect_stdout(package_stdout):
                package_rc = package_cli.main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(status_path),
                        "--summary-only",
                    ]
                )
            with contextlib.redirect_stdout(manager_stdout):
                manager_rc = manager_main(
                    [
                        "apply-run",
                        "--apply-in",
                        str(apply_path),
                        "--status-file",
                        str(status_path),
                        "--summary-only",
                    ]
                )

        self.assertEqual(0, package_rc)
        self.assertEqual(0, manager_rc)
        self.assertIn("controller-cycle: next_action=review-graph-cycle", package_stdout.getvalue())
        self.assertIn("controller-cycle: next_action=review-graph-cycle", manager_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
