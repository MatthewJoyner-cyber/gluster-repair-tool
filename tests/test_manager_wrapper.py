# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the public gluster-manager wrapper."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from gluster_heal_tool import cli as package_cli


def _load_manager_module():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module


class ManagerWrapperRepairMetaTests(unittest.TestCase):
    def test_apply_run_delegates_to_the_canonical_cli(self) -> None:
        module = _load_manager_module()
        for flags in ([], ["--execute", "--batch"], ["--execute-ready", "--manage-heal"],
                      ["--require-snapshot", "--snapshot-ack"], ["--summary-only"], ["--help"]):
            args = ["apply-run", "--apply-in", "/tmp/apply.json", *flags]
            with self.subTest(flags=flags), patch.object(module, "cli_main", return_value=73) as cli_main:
                self.assertEqual(73, module.main(args))
                cli_main.assert_called_once_with(args)

    def test_apply_run_delegates_from_process_arguments(self) -> None:
        module = _load_manager_module()
        args = ["apply-run", "--apply-in", "/tmp/apply.json", "--execute", "--batch"]
        with (patch.object(module.sys, "argv", ["gluster-manager.py", *args]),
              patch.object(module, "cli_main", return_value=2) as cli_main):
            self.assertEqual(2, module.main())
        cli_main.assert_called_once_with(args)

    def test_apply_run_parser_exposes_canonical_execution_flags(self) -> None:
        module = _load_manager_module()

        args = module.build_parser().parse_args(
            [
                "apply-run",
                "--execute-ready",
                "--manage-heal",
                "--require-snapshot",
                "--snapshot-ack",
                "--apply-in",
                "/tmp/apply.json",
            ]
        )

        self.assertTrue(args.execute_ready)
        self.assertTrue(args.manage_heal)
        self.assertTrue(args.require_snapshot)
        self.assertTrue(args.snapshot_ack)

    def test_post_execute_heal_refresh_preserves_disabled_settings(self) -> None:
        module = package_cli
        original = {
            "cluster.self-heal-daemon": "off",
            "cluster.data-self-heal": "off",
            "cluster.metadata-self-heal": "off",
            "cluster.entry-self-heal": "off",
        }
        final_check = {"available": True, "unique_count": 0}
        final_guard = {"repeat_count": 0}

        with (
            patch.object(module, "get_heal_settings", return_value=original),
            patch.object(
                module,
                "summarize_heal_settings",
                return_value={"all_on_effective": False},
            ),
            patch.object(module, "set_heal_settings") as enable_mock,
            patch.object(module, "set_heal_settings_exact") as restore_mock,
            patch.object(
                module,
                "_refresh_post_execute_heal",
                return_value=(final_check, final_guard),
            ) as refresh_mock,
        ):
            result = module._refresh_post_execute_heal_preserving_settings("gtest", {})

        self.assertEqual((final_check, final_guard), result)
        enable_mock.assert_called_once_with("gtest", enabled=True)
        refresh_mock.assert_called_once_with("gtest", {}, repeat_limit=5, total_timeout_seconds=60.0)
        restore_mock.assert_called_once_with("gtest", original)

    def test_post_execute_heal_refresh_rejects_unverified_restore(self) -> None:
        module = package_cli
        original = {
            "cluster.self-heal-daemon": "off",
            "cluster.data-self-heal": "off",
            "cluster.metadata-self-heal": "off",
            "cluster.entry-self-heal": "off",
        }

        with (
            patch.object(module, "get_heal_settings", return_value=original),
            patch.object(
                module,
                "summarize_heal_settings",
                return_value={"all_on_effective": False},
            ),
            patch.object(module, "set_heal_settings"),
            patch.object(
                module,
                "set_heal_settings_exact",
                side_effect=RuntimeError("cluster.data-self-heal: expected 'off', got 'on'"),
            ),
            patch.object(module, "_refresh_post_execute_heal", return_value=({}, {})),
        ):
            with self.assertRaisesRegex(RuntimeError, "expected 'off', got 'on'"):
                module._refresh_post_execute_heal_preserving_settings("gtest", {})

    def test_post_execute_heal_refresh_restores_settings_after_failure(self) -> None:
        module = package_cli
        original = {
            "cluster.self-heal-daemon": "off",
            "cluster.data-self-heal": "off",
            "cluster.metadata-self-heal": "off",
            "cluster.entry-self-heal": "off",
        }

        with (
            patch.object(module, "get_heal_settings", return_value=original),
            patch.object(
                module,
                "summarize_heal_settings",
                return_value={"all_on_effective": False},
            ),
            patch.object(module, "set_heal_settings"),
            patch.object(module, "set_heal_settings_exact") as restore_mock,
            patch.object(
                module,
                "_refresh_post_execute_heal",
                side_effect=RuntimeError("heal refresh failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "heal refresh failed"):
                module._refresh_post_execute_heal_preserving_settings("gtest", {})

        restore_mock.assert_called_once_with("gtest", original)

    def test_post_execute_heal_refresh_leaves_enabled_settings_alone(self) -> None:
        module = package_cli
        enabled = {
            "cluster.self-heal-daemon": "on",
            "cluster.data-self-heal": "on",
            "cluster.metadata-self-heal": "on",
            "cluster.entry-self-heal": "on",
        }

        with (
            patch.object(module, "get_heal_settings", return_value=enabled),
            patch.object(
                module,
                "summarize_heal_settings",
                return_value={"all_on_effective": True},
            ),
            patch.object(module, "set_heal_settings") as enable_mock,
            patch.object(module, "set_heal_settings_exact") as restore_mock,
            patch.object(
                module,
                "_refresh_post_execute_heal",
                return_value=({}, {}),
            ),
        ):
            module._refresh_post_execute_heal_preserving_settings("gtest", {})

        enable_mock.assert_not_called()
        restore_mock.assert_not_called()

    def test_parser_exposes_repair_meta_modes(self) -> None:
        module = _load_manager_module()

        path_args = module.build_parser().parse_args(
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
        self.assertEqual("repair-meta", path_args.cmd)
        self.assertEqual("/mnt/gluster/repair-canary/alpha", path_args.path)
        self.assertTrue(path_args.skip_mount_probe)

        evidence_args = module.build_parser().parse_args(
            [
                "evidence-build",
                "--path",
                "/mnt/gluster/repair-canary/alpha",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )
        self.assertEqual("evidence-build", evidence_args.cmd)
        self.assertEqual("/mnt/gluster/repair-canary/alpha", evidence_args.path)
        self.assertEqual("auto", evidence_args.mount_probe)
        self.assertFalse(evidence_args.skip_mount_probe)

        backend_args = module.build_parser().parse_args(
            [
                "repair-meta",
                "--backend-path",
                "/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )
        self.assertEqual("repair-meta", backend_args.cmd)
        self.assertEqual("/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt", backend_args.backend_path)
        self.assertEqual("gtest3", backend_args.volume)

        gfid_args = module.build_parser().parse_args(
            [
                "repair-meta",
                "--gfid",
                "90000000000040008000000000000003",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )
        self.assertEqual("90000000000040008000000000000003", gfid_args.gfid)
        self.assertEqual("gtest3", gfid_args.volume)

        gfid_child_args = module.build_parser().parse_args(
            [
                "repair-meta",
                "--gfid-child",
                "<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )
        self.assertEqual("<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt", gfid_child_args.gfid_child)
        self.assertEqual("gtest3", gfid_child_args.volume)

        index_args = module.build_parser().parse_args(
            [
                "repair-meta",
                "--index-entry",
                ".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
                "--volume",
                "gtest3",
                "--manifest-out",
                "/tmp/manifest.json",
                "--observations-out",
                "/tmp/observations.json",
            ]
        )
        self.assertEqual(".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003", index_args.index_entry)
        self.assertEqual("gtest3", index_args.volume)

    def test_parser_help_mentions_targeted_evidence_routes(self) -> None:
        module = _load_manager_module()
        help_text = module.build_parser().format_help()
        self.assertIn("repair-meta", help_text)
        self.assertIn("GFID", help_text)
        self.assertIn("backend path", help_text)
        self.assertIn("index evidence", help_text)

    def test_main_dispatches_repair_meta_path_mode(self) -> None:
        module = _load_manager_module()
        summary = {
            "volume": "gtest3",
            "mountpoint": "/mnt/gluster",
            "brick_path": "/gluster/gtest3b/gtest3/brick",
            "path": "/mnt/gluster/repair-canary/alpha",
            "logical_path": "repair-canary/alpha",
            "source": "node-b:/gtest3",
            "source_host": "node-b",
            "evidence_route": "mounted-path",
            "input_source": "operator_path",
            "requested_seed": "/mnt/gluster/repair-canary/alpha",
        }
        with (
            patch.object(module, "_print_status_warnings"),
            patch.object(module, "discover_brick_hosts", return_value=["node-a", "node-b"]),
            patch.object(module, "resolve_via_path", return_value=summary) as resolve_mock,
            patch.object(module, "update_status") as update_mock,
            patch.object(module, "render_manifest_summary", return_value="summary"),
        ):
            result = module.main(
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

        self.assertEqual(0, result)
        self.assertFalse(resolve_mock.call_args.kwargs["probe_mount"])
        self.assertEqual("repair-meta-built", update_mock.call_args.kwargs["phase"])
        self.assertEqual("operator_path", update_mock.call_args.kwargs["input_source"])
        self.assertEqual("/mnt/gluster/repair-canary/alpha", update_mock.call_args.kwargs["requested_seed"])
        self.assertFalse(update_mock.call_args.kwargs["repair_meta_mount_probe"])

    def test_main_dispatches_evidence_build_path_mode(self) -> None:
        module = _load_manager_module()
        summary = {
            "volume": "gtest3",
            "mountpoint": "/mnt/gluster",
            "brick_path": "/gluster/gtest3b/gtest3/brick",
            "path": "/mnt/gluster/repair-canary/alpha",
            "logical_path": "repair-canary/alpha",
            "source": "node-b:/gtest3",
            "source_host": "node-b",
            "evidence_route": "mounted-path",
            "input_source": "operator_path",
            "requested_seed": "/mnt/gluster/repair-canary/alpha",
        }
        with (
            patch.object(module, "_print_status_warnings"),
            patch.object(module, "discover_brick_hosts", return_value=["node-a", "node-b"]),
            patch.object(module, "resolve_via_path", return_value=summary) as resolve_mock,
            patch.object(module, "update_status") as update_mock,
            patch.object(module, "render_manifest_summary", return_value="summary"),
        ):
            result = module.main(
                [
                    "evidence-build",
                    "--path",
                    "/mnt/gluster/repair-canary/alpha",
                    "--manifest-out",
                    "/tmp/manifest.json",
                    "--observations-out",
                    "/tmp/observations.json",
                ]
            )

        self.assertEqual(0, result)
        self.assertFalse(resolve_mock.call_args.kwargs["probe_mount"])
        self.assertEqual("evidence-build-built", update_mock.call_args.kwargs["phase"])
        self.assertEqual("operator_path", update_mock.call_args.kwargs["input_source"])
        self.assertEqual("/mnt/gluster/repair-canary/alpha", update_mock.call_args.kwargs["requested_seed"])
        self.assertEqual("auto", update_mock.call_args.kwargs["repair_meta_mount_probe_mode"])
        self.assertFalse(update_mock.call_args.kwargs["repair_meta_mount_probe"])

    def test_main_dispatches_evidence_build_path_probe_on_mode(self) -> None:
        module = _load_manager_module()
        summary = {
            "volume": "gtest3",
            "mountpoint": "/mnt/gluster",
            "brick_path": "/gluster/gtest3b/gtest3/brick",
            "path": "/mnt/gluster/repair-canary/alpha",
            "logical_path": "repair-canary/alpha",
            "source": "node-b:/gtest3",
            "source_host": "node-b",
            "evidence_route": "mounted-path",
            "input_source": "operator_path",
            "requested_seed": "/mnt/gluster/repair-canary/alpha",
        }
        with (
            patch.object(module, "_print_status_warnings"),
            patch.object(module, "discover_brick_hosts", return_value=["node-a", "node-b"]),
            patch.object(module, "resolve_via_path", return_value=summary) as resolve_mock,
            patch.object(module, "update_status") as update_mock,
            patch.object(module, "render_manifest_summary", return_value="summary"),
        ):
            result = module.main(
                [
                    "evidence-build",
                    "--path",
                    "/mnt/gluster/repair-canary/alpha",
                    "--manifest-out",
                    "/tmp/manifest.json",
                    "--observations-out",
                    "/tmp/observations.json",
                    "--mount-probe",
                    "on",
                ]
            )

        self.assertEqual(0, result)
        self.assertTrue(resolve_mock.call_args.kwargs["probe_mount"])
        self.assertEqual("evidence-build-built", update_mock.call_args.kwargs["phase"])
        self.assertEqual("on", update_mock.call_args.kwargs["repair_meta_mount_probe_mode"])
        self.assertTrue(update_mock.call_args.kwargs["repair_meta_mount_probe"])

    def test_main_dispatches_repair_meta_backend_mode(self) -> None:
        module = _load_manager_module()
        summary = {
            "volume": "gtest3",
            "mountpoint": "/operator/view",
            "brick_path": "/gluster/gtest3b/gtest3/brick",
            "path": "/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt",
            "backend_path": "/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt",
            "logical_path": "repair-canary/alpha/payload.txt",
            "source": "operator-backend:/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt",
            "source_host": "localhost",
            "evidence_route": "backend-path",
        }
        with (
            patch.object(module, "_print_status_warnings"),
            patch.object(module, "discover_brick_hosts", return_value=["node-a", "node-b"]),
            patch.object(module, "resolve_via_backend_path", return_value=summary) as resolve_mock,
            patch.object(module, "update_status") as update_mock,
            patch.object(module, "render_manifest_summary", return_value="summary"),
        ):
            result = module.main(
                [
                    "repair-meta",
                    "--backend-path",
                    "/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt",
                    "--volume",
                    "gtest3",
                    "--mountpoint",
                    "/operator/view",
                    "--manifest-out",
                    "/tmp/manifest.json",
                    "--observations-out",
                    "/tmp/observations.json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual("/operator/view", resolve_mock.call_args.kwargs["mountpoint"])
        self.assertEqual("backend-path", update_mock.call_args.kwargs["repair_meta_evidence_route"])
        self.assertFalse(update_mock.call_args.kwargs["repair_meta_mount_probe"])


    def test_main_dispatches_repair_meta_gfid_child_mode(self) -> None:
        module = _load_manager_module()
        summary = {
            "volume": "gtest3",
            "mountpoint": "/gtest3",
            "brick_path": "/gluster/gtest3b/gtest3/brick",
            "path": "<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt",
            "logical_path": "<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt",
            "source": "operator-gfid-child:90000000-0000-4000-8000-000000000003/alpha/payload.txt",
            "source_host": "localhost",
            "evidence_route": "gfid-child",
            "repair_meta_input": "<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt",
            "repair_meta_input_kind": "gfid-child",
        }
        with (
            patch.object(module, "_print_status_warnings"),
            patch.object(module, "discover_brick_hosts", return_value=["node-a", "node-b"]),
            patch.object(module, "resolve_via_gfid_child", return_value=summary) as resolve_mock,
            patch.object(module, "update_status") as update_mock,
            patch.object(module, "render_manifest_summary", return_value="summary"),
        ):
            result = module.main(
                [
                    "repair-meta",
                    "--gfid-child",
                    "<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt",
                    "--volume",
                    "gtest3",
                    "--manifest-out",
                    "/tmp/manifest.json",
                    "--observations-out",
                    "/tmp/observations.json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual("<gfid:90000000-0000-4000-8000-000000000003>/alpha/payload.txt", resolve_mock.call_args.kwargs["gfid_child"])
        self.assertEqual("gfid-child", update_mock.call_args.kwargs["repair_meta_input_kind"])
        self.assertFalse(update_mock.call_args.kwargs["repair_meta_mount_probe"])

    def test_main_dispatches_repair_meta_index_entry_mode(self) -> None:
        module = _load_manager_module()
        summary = {
            "volume": "gtest3",
            "mountpoint": "/operator/view",
            "brick_path": "/gluster/gtest3b/gtest3/brick",
            "path": "/.glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
            "logical_path": ".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
            "source": "operator-index-entry:/.glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
            "source_host": "localhost",
            "evidence_route": "index-entry",
            "repair_meta_input": "/.glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
            "repair_meta_input_kind": "index-entry",
        }
        with (
            patch.object(module, "_print_status_warnings"),
            patch.object(module, "discover_brick_hosts", return_value=["node-a", "node-b"]),
            patch.object(module, "resolve_via_index_entry", return_value=summary) as resolve_mock,
            patch.object(module, "update_status") as update_mock,
            patch.object(module, "render_manifest_summary", return_value="summary"),
        ):
            result = module.main(
                [
                    "repair-meta",
                    "--index-entry",
                    ".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003",
                    "--volume",
                    "gtest3",
                    "--mountpoint",
                    "/operator/view",
                    "--manifest-out",
                    "/tmp/manifest.json",
                    "--observations-out",
                    "/tmp/observations.json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual(".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003", resolve_mock.call_args.kwargs["index_entry"])
        self.assertEqual("index-entry", update_mock.call_args.kwargs["repair_meta_input_kind"])
        self.assertFalse(update_mock.call_args.kwargs["repair_meta_mount_probe"])

    def test_main_dispatches_repair_cycle_alias(self) -> None:
        module = _load_manager_module()
        status = {"controller_cycle_report": "repair-cycle report"}
        with (
            patch.object(module, "load_status", return_value=status) as load_status_mock,
            patch.object(module, "render_controller_cycle_report", return_value="repair-cycle report") as render_mock,
        ):
            result = module.main(["repair-cycle", "--status-file", "/tmp/status.json"])

        self.assertEqual(0, result)
        load_status_mock.assert_called_once_with("/tmp/status.json")
        render_mock.assert_called_once_with(status)
