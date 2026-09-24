# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Public focused-evidence log forwarding and failure behavior."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool import manager


def _manager_cli():
    script = Path(__file__).resolve().parents[1] / "gluster-manager.py"
    spec = importlib.util.spec_from_file_location("gluster_manager_evidence_logging", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader and module
    spec.loader.exec_module(module)
    return module


class EvidenceLogTests(unittest.TestCase):
    def test_log_option_reaches_every_focused_resolver_and_status(self) -> None:
        module = _manager_cli()
        routes = (
            ("--path", "/testvol/data/file", "resolve_via_path"),
            ("--backend-path", "/brick/data/file", "resolve_via_backend_path"),
            ("--gfid", "11111111-2222-3333-4444-555555555555", "resolve_via_gfid"),
            ("--gfid-child", "11111111-2222-3333-4444-555555555555/data",
             "resolve_via_gfid_child"),
            ("--index-entry", "/.glusterfs/indices/xattrop/11111111-2222-3333-4444-555555555555",
             "resolve_via_index_entry"),
        )
        summary = {"volume": "testvol", "mountpoint": "/testvol", "input_source": "operator_path"}
        for command in ("evidence-build", "repair-meta"):
            for option, value, resolver_name in routes:
                with self.subTest(command=command, route=option):
                    with (
                        patch.object(module, "_print_status_warnings"),
                        patch.object(module, "discover_brick_hosts", return_value=["brick-a"]),
                        patch.object(module, resolver_name, return_value=summary) as resolve,
                        patch.object(module, "update_status") as status,
                        patch.object(module, "render_manifest_summary", return_value="summary"),
                    ):
                        code = module.main([
                            command, option, value, "--volume", "testvol",
                            "--manifest-out", "/tmp/manifest.json",
                            "--observations-out", "/tmp/observations.json",
                            "--log-out", "/tmp/focused-evidence.log",
                        ])
                        self.assertEqual(0, code)
                        self.assertEqual("/tmp/focused-evidence.log", resolve.call_args.kwargs["log_path"])
                        self.assertEqual("/tmp/focused-evidence.log", status.call_args.kwargs["evidence_log"])

    def test_real_path_route_creates_log_and_records_it(self) -> None:
        module = _manager_cli()
        context = {
            "volume": "testvol", "mountpoint": "/testvol", "logical_path": "data/file",
            "raw_entry": "/data/file", "source_host": "localhost",
            "source": "localhost:testvol", "fstype": "fuse.glusterfs",
            "options": "rw", "requested_path": "/testvol/data/file",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "logs" / "evidence.log"
            status = root / "status.json"
            args = [
                "evidence-build", "--path", "/testvol/data/file",
                "--manifest-out", str(root / "manifest.json"),
                "--observations-out", str(root / "observations.json"),
                "--status-file", str(status), "--log-out", str(log),
            ]
            with (
                patch.object(manager, "_validate_path_mount_context", return_value=context),
                patch.object(manager, "_live_split_brain_gfids", return_value=(set(), "")),
                patch.object(manager, "discover_brick_hosts", return_value=[]),
                patch.object(manager, "discover_brick_paths", return_value={}),
                patch.object(manager, "_load_brick_role_evidence", return_value=({}, "")),
                patch.object(module, "discover_brick_hosts", return_value=[]),
            ):
                self.assertEqual(0, module.main(args))
            self.assertIn("manifest complete", log.read_text(encoding="utf-8"))
            self.assertEqual(str(log), json.loads(status.read_text(encoding="utf-8"))["evidence_log"])

    def test_unwritable_log_fails_before_manifest_or_status(self) -> None:
        module = _manager_cli()
        context = {
            "volume": "testvol", "mountpoint": "/testvol", "logical_path": "data/file",
            "raw_entry": "/data/file", "source_host": "localhost",
            "source": "localhost:testvol", "fstype": "fuse.glusterfs",
            "options": "rw", "requested_path": "/testvol/data/file",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked_log = root / "evidence.log"
            manifest = root / "manifest.json"
            status = root / "status.json"
            original_open = Path.open

            def deny_log_open(path, *args, **kwargs):
                if path == blocked_log:
                    raise PermissionError("log destination is not writable")
                return original_open(path, *args, **kwargs)

            with (
                patch.object(manager, "_validate_path_mount_context", return_value=context),
                patch.object(manager, "_live_split_brain_gfids", return_value=(set(), "")),
                patch.object(manager, "discover_brick_hosts", return_value=[]),
                patch.object(manager, "discover_brick_paths", return_value={}),
                patch.object(module, "discover_brick_hosts", return_value=[]),
                patch.object(Path, "open", deny_log_open),
            ):
                with self.assertRaises(PermissionError):
                    module.main([
                        "evidence-build", "--path", "/testvol/data/file",
                        "--manifest-out", str(manifest),
                        "--observations-out", str(root / "observations.json"),
                        "--status-file", str(status),
                        "--log-out", str(blocked_log),
                    ])
            self.assertFalse(manifest.exists())
            self.assertFalse(status.exists())


if __name__ == "__main__":
    unittest.main()
