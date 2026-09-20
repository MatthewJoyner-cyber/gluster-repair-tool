# SPDX-License-Identifier: GPL-2.0-only
"""Tests for manager startup preflight."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import gluster_heal_tool.cli as cli_module
from gluster_heal_tool.cli import build_parser
from gluster_heal_tool.heal_parser import parse_heal_info_text
from gluster_heal_tool.install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from gluster_heal_tool.manager import (
    build_manager_preflight_report,
    render_manager_preflight_summary,
    resolve_heal_snapshot,
    render_manifest_summary,
    refresh_manager_health_check,
    write_manager_preflight_report,
)


class ManagerPreflightTests(unittest.TestCase):
    def test_build_manager_preflight_report_counts_reachability(self) -> None:
        report_payload = {
            "schema_version": 1,
            "checked_at": "2026-05-06T00:00:00+00:00",
            "volume": "gtest",
            "ssh_user": "node-c",
            "connect_timeout": 7.0,
            "available": True,
            "error": "",
            "volume_type": "Replicate",
            "hosts": ["host-a", "host-b"],
            "bricks": [
                {"host": "host-a", "path": "/srv/gluster/brick-store/gtest", "online": True},
                {"host": "host-b", "path": "/srv/gluster/brick-store/gtest", "online": False},
            ],
            "checks": [],
            "heal_settings": {},
                "summary": {
                    "checks_checked": 2,
                    "checks_ok": 1,
                    "checks_failed": 1,
                    "hosts_checked": 2,
                    "hosts_ok": 1,
                    "hosts_failed": 1,
                    "bricks_checked": 2,
                    "bricks_online": 1,
                    "bricks_offline": 1,
                    "ready": False,
                "blockers": ["brick path has no mounted ancestor on host-b: /srv/gluster/brick-store/gtest"],
                    "warnings": [],
                },
            }

        with patch("gluster_heal_tool.manager.build_volume_health_report", return_value=report_payload):
            report = build_manager_preflight_report(
                "gtest",
                ssh_user="node-c",
                connect_timeout=7.0,
            )

        self.assertTrue(report["available"])
        self.assertEqual("gtest", report["volume"])
        self.assertEqual("node-c", report["ssh_user"])
        self.assertEqual(2, report["summary"]["hosts_checked"])
        self.assertEqual(1, report["summary"]["hosts_ok"])
        self.assertEqual(1, report["summary"]["hosts_failed"])
        self.assertFalse(report["summary"]["ready"])

        summary = render_manager_preflight_summary(report)
        self.assertIn("Health check: blocked", summary)
        self.assertIn("checks 1/2", summary)

    def test_build_manager_preflight_report_records_snapshot_gate(self) -> None:
        report_payload = {
            "schema_version": 1,
            "checked_at": "2026-05-06T00:00:00+00:00",
            "volume": "gtest",
            "ssh_user": "node-c",
            "connect_timeout": 7.0,
            "available": True,
            "error": "",
            "volume_type": "Replicate",
            "hosts": ["host-a"],
            "bricks": [{"host": "host-a", "path": "/srv/gluster/brick-store/gtest", "online": True}],
            "checks": [],
            "heal_settings": {},
            "reversibility": {
                "snapshot_required": True,
                "snapshot_acknowledged": True,
                "snapshot_inventory": {
                    "available": True,
                    "names": ["snap-one"],
                    "count": 1,
                    "raw": "",
                    "error": "",
                },
            },
            "summary": {
                "checks_checked": 0,
                "checks_ok": 0,
                "checks_failed": 0,
                "hosts_checked": 1,
                "hosts_ok": 1,
                "hosts_failed": 0,
                "bricks_checked": 1,
                "bricks_online": 1,
                "bricks_offline": 0,
                "ready": True,
                "blockers": [],
                "warnings": [],
            },
        }

        with patch("gluster_heal_tool.manager.build_volume_health_report", return_value=report_payload) as mocked:
            report = build_manager_preflight_report(
                "gtest",
                ssh_user="node-c",
                connect_timeout=7.0,
                require_snapshot=True,
                snapshot_ack=True,
            )

        self.assertTrue(mocked.called)
        self.assertTrue(report["reversibility"]["snapshot_required"])
        self.assertTrue(report["reversibility"]["snapshot_acknowledged"])
        self.assertEqual(1, report["reversibility"]["snapshot_inventory"]["count"])

    def test_build_manager_preflight_report_handles_discovery_errors(self) -> None:
        with patch("gluster_heal_tool.manager.build_volume_health_report", side_effect=RuntimeError("no volume")):
            with self.assertRaises(RuntimeError):
                build_manager_preflight_report("gtest")

    def test_manager_health_report_parser_exposes_command(self) -> None:
        args = build_parser().parse_args(
            [
                "health-check",
                "--volume",
                "gtest",
                "--health-out",
                "/tmp/manager-health.json",
                "--require-snapshot",
                "--snapshot-ack",
            ]
        )

        self.assertEqual("health-check", args.cmd)
        self.assertEqual("gtest", args.volume)
        self.assertEqual(DEFAULT_SERVICE_USER, args.ssh_user)
        self.assertTrue(args.require_snapshot)
        self.assertTrue(args.snapshot_ack)

    def test_manifest_build_parser_allows_live_heal_capture(self) -> None:
        args = build_parser().parse_args(
            [
                "manifest-build",
                "--volume",
                "gtest",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )

        self.assertEqual("manifest-build", args.cmd)
        self.assertEqual("gtest", args.volume)
        self.assertIsNone(args.heal_file)
        self.assertFalse(args.heal_latest)
        self.assertIsNone(args.heal_out)
        self.assertIsNone(args.heal_root)
        self.assertFalse(args.skip_heal_refresh)

    def test_manifest_build_parser_exposes_latest_snapshot_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "manifest-build",
                "--volume",
                "gtest",
                "--heal-latest",
                "--heal-root",
                "/tmp/heal-info-root",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )

        self.assertTrue(args.heal_latest)
        self.assertEqual("/tmp/heal-info-root", args.heal_root)

    def test_manifest_build_parser_exposes_fresh_snapshot_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "manifest-build",
                "--volume",
                "gtest",
                "--heal-fresh",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )

        self.assertTrue(args.heal_fresh)

    def test_manifest_build_parser_exposes_skip_heal_refresh_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "manifest-build",
                "--volume",
                "gtest",
                "--skip-heal-refresh",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )

        self.assertTrue(args.skip_heal_refresh)

    def test_repair_cycle_parser_exposes_command(self) -> None:
        args = build_parser().parse_args(
            [
                "repair-cycle",
                "--status-file",
                "/tmp/repair-cycle-status.json",
            ]
        )

        self.assertEqual("repair-cycle", args.cmd)
        self.assertEqual("/tmp/repair-cycle-status.json", args.status_file)

    def test_repair_cycle_dispatches_controller_report(self) -> None:
        with (
            patch.object(cli_module, "load_status", return_value={"controller_cycle_report": "repair-cycle report"}) as load_status_mock,
            patch.object(cli_module, "render_controller_cycle_report", return_value="repair-cycle report") as render_mock,
        ):
            result = cli_module.main(["repair-cycle", "--status-file", "/tmp/repair-cycle-status.json"])

        self.assertEqual(0, result)
        load_status_mock.assert_called_once_with("/tmp/repair-cycle-status.json")
        render_mock.assert_called_once()

    def test_repair_meta_parser_exposes_path_mode(self) -> None:
        args = build_parser().parse_args(
            [
                "repair-meta",
                "--path",
                "/mnt/gluster/repair-canary/alpha",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
                "--skip-mount-probe",
            ]
        )

        self.assertEqual("repair-meta", args.cmd)
        self.assertEqual("/mnt/gluster/repair-canary/alpha", args.path)
        self.assertTrue(args.skip_mount_probe)
        self.assertEqual(str(DEFAULT_WORKER_PATH), args.worker_path)
        self.assertEqual(str(DEFAULT_RESOLVER_PATH), args.resolver_path)

    def test_evidence_build_parser_exposes_path_mode(self) -> None:
        args = build_parser().parse_args(
            [
                "evidence-build",
                "--path",
                "/mnt/gluster/repair-canary/alpha",
                "--manifest-out",
                "/tmp/manifest.json",
            ]
        )

        self.assertEqual("evidence-build", args.cmd)
        self.assertEqual("/mnt/gluster/repair-canary/alpha", args.path)
        self.assertEqual("auto", args.mount_probe)
        self.assertFalse(args.skip_mount_probe)
        self.assertEqual(str(DEFAULT_WORKER_PATH), args.worker_path)
        self.assertEqual(str(DEFAULT_RESOLVER_PATH), args.resolver_path)

    def test_evidence_build_parser_exposes_explicit_mount_probe(self) -> None:
        args = build_parser().parse_args(
            [
                "evidence-build",
                "--path",
                "/mnt/gluster/repair-canary/alpha",
                "--manifest-out",
                "/tmp/manifest.json",
                "--mount-probe",
                "on",
            ]
        )

        self.assertEqual("on", args.mount_probe)
        self.assertFalse(args.skip_mount_probe)

    def test_repair_meta_parser_exposes_backend_path_mode(self) -> None:
        args = build_parser().parse_args(
            [
                "repair-meta",
                "--backend-path",
                "/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
                "--skip-mount-probe",
            ]
        )
        self.assertEqual("gtest3", args.volume)
        self.assertIn("/gluster/gtest3b", args.backend_path)
        self.assertTrue(args.skip_mount_probe)
        self.assertIsNone(args.path)

    def test_repair_meta_parser_exposes_targeted_discovery_modes(self) -> None:
        gfid_args = build_parser().parse_args(
            [
                "repair-meta",
                "--gfid",
                "90000000000040008000000000000003",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
                "--skip-mount-probe",
            ]
        )
        self.assertEqual("90000000000040008000000000000003", gfid_args.gfid)
        self.assertEqual("gtest3", gfid_args.volume)
        self.assertTrue(gfid_args.skip_mount_probe)

        child_args = build_parser().parse_args(
            [
                "repair-meta",
                "--gfid-child",
                "<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
            ]
        )
        self.assertEqual("<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt", child_args.gfid_child)
        self.assertEqual("gtest3", child_args.volume)

        index_args = build_parser().parse_args(
            [
                "repair-meta",
                "--index-entry",
                ".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
            ]
        )
        self.assertEqual(
            ".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
            index_args.index_entry,
        )
        self.assertEqual("gtest3", index_args.volume)

    def test_repair_meta_parser_help_mentions_targeted_evidence_routes(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("repair-meta", help_text)
        self.assertIn("GFID", help_text)
        self.assertIn("backend path", help_text)
        self.assertIn("index evidence", help_text)

    def test_resolve_heal_snapshot_reuses_latest_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            volume_dir = Path(tmpdir) / "gtest"
            volume_dir.mkdir(parents=True, exist_ok=True)
            latest_path = volume_dir / "gtest-heal-info-20260508T120000Z.txt"
            older_path = volume_dir / "gtest-heal-info-20260508T110000Z.txt"
            older_path.write_text("older\n")
            latest_path.write_text("latest\n")

            resolved = resolve_heal_snapshot("gtest", heal_latest=True, heal_root=tmpdir)

        self.assertEqual(str(latest_path), resolved)

    def test_resolve_heal_snapshot_forces_fresh_capture_even_when_latest_exists(self) -> None:
        with TemporaryDirectory() as tmpdir:
            volume_dir = Path(tmpdir) / 'gtest'
            volume_dir.mkdir(parents=True, exist_ok=True)
            latest_path = volume_dir / 'gtest-heal-info-20260508T120000Z.txt'
            latest_path.write_text('cached' + chr(10))

            with patch('gluster_heal_tool.manager.run_heal') as run_heal_mock, patch('gluster_heal_tool.manager.get_heal_info_text', return_value='fresh' + chr(10)):
                resolved = resolve_heal_snapshot('gtest', heal_latest=True, heal_fresh=True, heal_root=tmpdir)

            payload = Path(resolved).read_text()
            run_heal_mock.assert_called_once_with('gtest')

        self.assertNotEqual(str(latest_path), resolved)
        self.assertEqual('fresh' + chr(10), payload)

    def test_heal_parser_normalizes_split_brain_gfid_suffix(self) -> None:
        entries = parse_heal_info_text(
            "Brick node-a:/brick\n"
            "<gfid:826b4c06-4cc8-453b-bf38-a9d61848a1ea> - Is in split-brain\n"
        )
        self.assertEqual(1, len(entries))
        self.assertEqual("<gfid:826b4c06-4cc8-453b-bf38-a9d61848a1ea>", entries[0].raw)
        self.assertTrue(entries[0].split_brain)
        self.assertEqual("gfid", entries[0].kind)

    def test_resolve_heal_snapshot_preserves_info_when_refresh_fails(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "gluster_heal_tool.manager.run_heal",
                    side_effect=RuntimeError("Commit failed on node-d.matt.home. Please check log file for details."),
                ),
                patch(
                    "gluster_heal_tool.manager.get_heal_info_text",
                    return_value="Brick host-a:/srv/gluster/brick-store/gtest\n/example\n",
                ),
            ):
                resolved = resolve_heal_snapshot("gtest", heal_fresh=True, heal_root=tmpdir)

            payload = Path(resolved).read_text()
            self.assertIn("# gluster-repair heal refresh error: Commit failed on node-d.matt.home.", payload)
            self.assertIn("Brick host-a:/srv/gluster/brick-store/gtest", payload)
            self.assertIn("/example", payload)

    def test_resolve_heal_snapshot_captures_live_when_latest_missing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch('gluster_heal_tool.manager.run_heal') as run_heal_mock, patch('gluster_heal_tool.manager.get_heal_info_text', return_value='Brick host-a:/srv/gluster/brick-store/gtest' + chr(10) + '/example' + chr(10)):
                resolved = resolve_heal_snapshot('gtest', heal_latest=True, heal_root=tmpdir)

            payload = Path(resolved).read_text()
            run_heal_mock.assert_called_once_with('gtest')

        self.assertIn('Brick host-a:/srv/gluster/brick-store/gtest', payload)
        self.assertTrue(Path(resolved).name.startswith('gtest-heal-info-'))

    def test_manifest_summary_warns_when_heal_snapshot_is_not_fresh(self) -> None:
        rendered = render_manifest_summary(
            {
                "raw_entries": 1,
                "unique_raw_entries": 1,
                "observations": 4,
                "object_types": {"file": 1},
                "heal_refresh_error": "Commit failed on node-d",
            }
        )
        self.assertIn("WARNING: heal refresh failed; captured info is not fresh", rendered)
        self.assertIn("Commit failed on node-d", rendered)

    def test_resolve_heal_snapshot_skips_refresh_when_disabled(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch('gluster_heal_tool.manager.run_heal') as run_heal_mock, patch('gluster_heal_tool.manager.get_heal_info_text', return_value='Brick host-a:/srv/gluster/brick-store/gtest' + chr(10) + '/example' + chr(10)):
                resolved = resolve_heal_snapshot('gtest', heal_latest=True, heal_refresh=False, heal_root=tmpdir)

            payload = Path(resolved).read_text()
            run_heal_mock.assert_not_called()

        self.assertIn('Brick host-a:/srv/gluster/brick-store/gtest', payload)
        self.assertTrue(Path(resolved).name.startswith('gtest-heal-info-'))

    def test_refresh_manager_health_check_writes_fresh_report(self) -> None:
        report_payload = {
            "schema_version": 1,
            "checked_at": "2026-05-19T11:03:25.397080+00:00",
            "volume": "gtest",
            "ssh_user": "gluster-repair",
            "connect_timeout": 10.0,
            "available": True,
            "error": "",
            "volume_type": "Replicate",
            "hosts": ["host-a"],
            "bricks": [{"host": "host-a", "path": "/srv/gluster/brick-store/gtest", "online": True}],
            "checks": [],
            "heal_settings": {},
            "summary": {
                "checks_checked": 1,
                "checks_ok": 1,
                "checks_failed": 0,
                "hosts_checked": 1,
                "hosts_ok": 1,
                "hosts_failed": 0,
                "bricks_checked": 1,
                "bricks_online": 1,
                "bricks_offline": 0,
                "ready": True,
                "blockers": [],
                "warnings": [],
            },
        }

        with TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / "status.json"
            with patch.dict(os.environ, {"GLUSTER_REPAIR_WORK_ROOT": tmpdir}, clear=False):
                with patch("gluster_heal_tool.manager.build_manager_health_report", return_value=report_payload):
                    with patch("gluster_heal_tool.manager.write_manager_preflight_report") as mocked_write:
                        with patch("gluster_heal_tool.manager.write_brick_layout_cache") as mocked_layout_write:
                            refreshed_status, refreshed_report = refresh_manager_health_check(
                                status_file,
                                current_status={"volume": "gtest", "phase": "apply-run"},
                                volume="gtest",
                                ssh_user="gluster-repair",
                                connect_timeout=10.0,
                            )

        self.assertEqual(report_payload, refreshed_report)
        self.assertEqual("gtest", refreshed_status["volume"])
        self.assertEqual("gtest-health-check.json", Path(refreshed_status["health_out"]).name)
        self.assertTrue(mocked_write.called)
        self.assertEqual("gtest-health-check.json", Path(mocked_write.call_args.args[0]).name)
        self.assertTrue(mocked_layout_write.called)
        self.assertEqual("gtest-brick-layout.json", Path(mocked_layout_write.call_args.args[0]).name)

    def test_manifest_build_persists_discovered_brick_path(self) -> None:
        with patch("gluster_heal_tool.cli.resolve_heal_snapshot", return_value="/tmp/heal.txt"):
            with patch("gluster_heal_tool.cli.parse_heal_info_file", return_value=[]):
                with patch(
                        "gluster_heal_tool.cli.discover_brick_paths",
                        return_value={
                            "host-a": "/srv/gluster/brick-store/gtest",
                            "host-b": "/srv/gluster/brick-store/gtest",
                        },
                    ):
                    with patch("gluster_heal_tool.cli.discover_brick_hosts", return_value=["host-a", "host-b"]):
                        with patch("gluster_heal_tool.cli.build_manifest", return_value=({}, {})):
                            with patch("gluster_heal_tool.cli.write_manifest"):
                                with patch("gluster_heal_tool.cli.dump_observations"):
                                    with patch("gluster_heal_tool.cli.update_status") as mocked_update:
                                        with patch("gluster_heal_tool.cli.render_manifest_summary", return_value="manifest"):
                                            rc = cli_module.main(
                                                [
                                                    "manifest-build",
                                                    "--volume",
                                                    "gtest",
                                                    "--manifest-out",
                                                    "/tmp/manifest.json",
                                                    "--observations-out",
                                                    "/tmp/observations.json",
                                                    "--status-file",
                                                    "/tmp/status.json",
                                                ]
                                            )

        self.assertEqual(0, rc)
        self.assertTrue(mocked_update.called)
        kwargs = mocked_update.call_args.kwargs
        self.assertEqual("/srv/gluster/brick-store/gtest", kwargs["brick_path"])

    def test_manifest_build_skip_heal_refresh_passes_through(self) -> None:
        with patch('gluster_heal_tool.cli.resolve_heal_snapshot', return_value='/tmp/heal.txt') as resolve_mock:
            with patch('gluster_heal_tool.cli.parse_heal_info_file', return_value=[]):
                with patch(
                    'gluster_heal_tool.cli.discover_brick_paths',
                    return_value={
                        'host-a': '/srv/gluster/brick-store/gtest',
                        'host-b': '/srv/gluster/brick-store/gtest',
                    },
                ):
                    with patch('gluster_heal_tool.cli.discover_brick_hosts', return_value=['host-a', 'host-b']):
                        with patch('gluster_heal_tool.cli.build_manifest', return_value=({}, {})):
                            with patch('gluster_heal_tool.cli.write_manifest'):
                                with patch('gluster_heal_tool.cli.dump_observations'):
                                    with patch('gluster_heal_tool.cli.update_status'):
                                        with patch('gluster_heal_tool.cli.render_manifest_summary', return_value='manifest'):
                                            rc = cli_module.main(
                                                [
                                                    'manifest-build',
                                                    '--volume',
                                                    'gtest',
                                                    '--skip-heal-refresh',
                                                    '--manifest-out',
                                                    '/tmp/manifest.json',
                                                    '--observations-out',
                                                    '/tmp/observations.json',
                                                    '--status-file',
                                                    '/tmp/status.json',
                                                ]
                                            )

        self.assertEqual(0, rc)
        self.assertTrue(resolve_mock.called)
        self.assertFalse(resolve_mock.call_args.kwargs['heal_refresh'])

    def test_manager_preflight_parser_exposes_command(self) -> None:
        args = build_parser().parse_args(
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

        self.assertEqual("manager-preflight", args.cmd)
        self.assertEqual("gtest", args.volume)
        self.assertEqual(DEFAULT_SERVICE_USER, args.ssh_user)
        self.assertTrue(args.require_snapshot)
        self.assertTrue(args.snapshot_ack)

    def test_write_manager_preflight_report_round_trips(self) -> None:
        report = {
            "schema_version": 1,
            "volume": "gtest",
            "ssh_user": DEFAULT_SERVICE_USER,
            "connect_timeout": 10.0,
            "available": True,
            "error": "",
            "hosts": ["host-a"],
            "checks": [],
            "summary": {"hosts_checked": 1, "hosts_ok": 1, "hosts_failed": 0, "ready": True},
        }

        with TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "manager-preflight.json"
            write_manager_preflight_report(report_path, report)

            payload = json.loads(report_path.read_text())

        self.assertEqual(report, payload)


if __name__ == "__main__":
    unittest.main()
