# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for per-host brick-path resolution."""
from __future__ import annotations

import os

import unittest
from unittest.mock import patch

from gluster_heal_tool import manager
from gluster_heal_tool.models import ManifestObject, ResolutionObservation


class ManagerBrickPathTests(unittest.TestCase):
    def test_normalize_gfid_child_accepts_bare_and_heal_entry_forms(self) -> None:
        canonical = "90000000-0000-4000-8000-000000000003"
        self.assertEqual(
            (canonical, "alpha/payload.txt"),
            manager._normalize_gfid_child_entry(f"{canonical}/alpha/payload.txt"),
        )
        self.assertEqual(
            (canonical, "alpha/payload.txt"),
            manager._normalize_gfid_child_entry(f"<gfid:{canonical}>/alpha/payload.txt"),
        )

    def test_load_brick_role_evidence_returns_roles(self) -> None:
        volume_info = """
Volume Name: gtest3a
Brick1: node-a:/gluster/gtest3/gtest3/brick
Brick2: node-b:/gluster/gtest3/gtest3/brick
Brick3: node-c:/gluster/gtest3/arbiter/brick (arbiter)
"""
        with patch("gluster_heal_tool.manager.get_volume_info", return_value=volume_info):
            roles, error = manager._load_brick_role_evidence("gtest3a")

        self.assertEqual(
            {"node-a": "data", "node-b": "data", "node-c": "arbiter"},
            roles,
        )
        self.assertEqual("", error)

    def test_load_brick_role_evidence_reports_volume_info_error(self) -> None:
        with patch(
            "gluster_heal_tool.manager.get_volume_info",
            side_effect=RuntimeError("volume info failed"),
        ):
            roles, error = manager._load_brick_role_evidence("gtest3a")

        self.assertEqual({}, roles)
        self.assertEqual("volume info failed", error)
    def test_resolve_via_volume_uses_host_specific_brick_paths(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3a/brick",
            "node-b": "/gluster/gtest3a/brick",
            "node-c": "/gluster/gtest3a/arbiter/brick",
        }

        with (
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager.resolve_via_workers", return_value={"ok": True}) as resolve_mock,
        ):
            result = manager.resolve_via_volume(
                heal_file="latest",
                volume="gtest3a",
                brick_path=None,
                mountpoint="/gtest3a",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )

        self.assertEqual({"ok": True}, result)
        self.assertEqual(1, resolve_mock.call_count)
        kwargs = resolve_mock.call_args.kwargs
        self.assertIsNone(kwargs["brick_path"])
        self.assertEqual(brick_paths, kwargs["brick_paths"])


    def test_worker_results_keep_queried_brick_endpoint_identity(self) -> None:
        heal_entries = [
            manager.HealEntry(
                raw="/alpha",
                source_host="node-a",
                brick="localhost:gtest4",
                index_on_host=0,
            )
        ]
        brick_paths = {
            "node-a": "/gluster/gtest4/brick",
            "192.0.2.30": "/gluster/gtest4-alias/brick",
        }

        def fake_worker(*, host, request, **_kwargs):
            return [
                ResolutionObservation(
                    host="node-a",
                    raw_entry="/alpha",
                    relpath="alpha",
                    backend=f"{request.brick_path}/alpha",
                    backend_exists=True,
                    backend_lstat_type="dir",
                )
            ]

        with (
            patch("gluster_heal_tool.manager._run_resolve_worker", side_effect=fake_worker),
            patch("gluster_heal_tool.manager.build_manifest", return_value=({}, [])) as build_mock,
            patch("gluster_heal_tool.manager.write_manifest"),
            patch("gluster_heal_tool.manager.dump_observations"),
        ):
            manager._resolve_entries_via_workers(
                heal_entries=heal_entries,
                volume="gtest4",
                hosts=list(brick_paths),
                brick_path=None,
                brick_paths=brick_paths,
                mountpoint="/gtest4",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
                ssh_user="root",
            )

        resolver = build_mock.call_args.args[1]
        observations = resolver.resolve_entry("/alpha")
        self.assertEqual(["node-a", "192.0.2.30"], [item.host for item in observations])
        self.assertEqual(
            ["/gluster/gtest4/brick/alpha", "/gluster/gtest4-alias/brick/alpha"],
            [item.backend for item in observations],
        )


class ManagerPathRepairTests(unittest.TestCase):
    def test_annotate_live_split_brain_marks_only_matching_manifest_object(self) -> None:
        matching = ManifestObject(
            logical_path="matching",
            object_type="file",
            depth=1,
            file_gfids=["11111111-2222-3333-4444-555555555555"],
        )
        unrelated = ManifestObject(
            logical_path="unrelated",
            object_type="file",
            depth=1,
            file_gfids=["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
        )

        manager._annotate_live_split_brain(
            {"matching": matching, "unrelated": unrelated},
            {"11111111-2222-3333-4444-555555555555"},
        )

        self.assertIn("heal_info_marks_split_brain", matching.notes)
        self.assertNotIn("heal_info_marks_split_brain", unrelated.notes)

    def test_live_split_brain_gfids_reads_only_marked_heal_entries(self) -> None:
        gfid = "11111111-2222-3333-4444-555555555555"
        heal_info = (
            "Brick node-a:/gluster/gtest4/brick\n"
            f"<gfid:{gfid}> - Is in split-brain\n"
            "Status: Connected\n"
            "Number of entries: 1\n"
        )

        with patch("gluster_heal_tool.manager.get_heal_info_text", return_value=heal_info):
            split_brain_gfids, error = manager._live_split_brain_gfids("gtest4")

        self.assertEqual({gfid}, split_brain_gfids)
        self.assertEqual("", error)

    def test_resolve_via_path_uses_inferred_mount_and_relative_logical_path(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3b/gtest3/brick",
            "node-b": "/gluster/gtest3b/gtest3/brick",
            "node-c": "/gluster/gtest3b/gtest3/brick",
        }
        mount_info = {
            "source": "localhost:gtest3",
            "fstype": "fuse.glusterfs",
            "target": "/mnt/gluster",
            "options": "rw,relatime",
        }

        with (
            patch("gluster_heal_tool.manager.inspect_mount_at_path", return_value=mount_info),
            patch(
                "gluster_heal_tool.manager.get_heal_info_text",
                return_value="Brick node-a:/gluster/gtest3/brick\\nNumber of entries: 0\\n",
            ),
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager._resolve_entries_via_workers", return_value={
                "raw_entries": 1,
                "unique_raw_entries": 1,
                "observations": 3,
                "object_types": {"directory": 1},
            }) as resolve_mock,
        ):
            result = manager.resolve_via_path(
                path="/mnt/gluster/repair-canary/alpha",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )

        self.assertEqual("gtest3", result["volume"])
        self.assertEqual("/mnt/gluster", result["mountpoint"])
        self.assertEqual("/mnt/gluster/repair-canary/alpha", result["path"])
        self.assertEqual("repair-canary/alpha", result["logical_path"])
        self.assertEqual("localhost:gtest3", result["source"])
        self.assertEqual(1, resolve_mock.call_count)
        kwargs = resolve_mock.call_args.kwargs
        self.assertEqual("gtest3", kwargs["volume"])
        self.assertEqual("/mnt/gluster", kwargs["mountpoint"])
        self.assertEqual(brick_paths, kwargs["brick_paths"])
        self.assertEqual("/repair-canary/alpha", kwargs["heal_entries"][0].raw)
        self.assertEqual("localhost", kwargs["heal_entries"][0].source_host)
        self.assertEqual(set(), kwargs["split_brain_gfids"])

    def test_resolve_via_path_without_mount_probe_uses_lexical_mount_context(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3b/gtest3/brick",
            "node-b": "/gluster/gtest3b/gtest3/brick",
        }
        mount_info = {
            "source": "localhost:gtest3",
            "fstype": "fuse.glusterfs",
            "target": "/mnt/gluster",
            "options": "rw,relatime",
        }

        with (
            patch("gluster_heal_tool.manager.inspect_mount_at_path") as active_mount,
            patch("gluster_heal_tool.manager.inspect_mount_at_path_lexically", return_value=mount_info) as lexical_mount,
            patch(
                "gluster_heal_tool.manager.get_heal_info_text",
                return_value="Brick node-a:/gluster/gtest3/brick\nNumber of entries: 0\n",
            ),
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager._resolve_entries_via_workers", return_value={}) as resolve_mock,
        ):
            manager.resolve_via_path(
                path="/mnt/gluster/repair-canary/alpha",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
                probe_mount=False,
            )

        lexical_mount.assert_called_once_with("/mnt/gluster/repair-canary/alpha")
        active_mount.assert_not_called()
        self.assertFalse(resolve_mock.call_args.kwargs["probe_mount"])

    def test_resolve_via_path_rejects_bind_mounts(self) -> None:
        with patch(
            "gluster_heal_tool.manager.inspect_mount_at_path",
            return_value={
                "source": "/srv/gluster-bind",
                "fstype": "none",
                "target": "/gtest3",
                "options": "rw,bind",
            },
        ):
            with self.assertRaises(RuntimeError):
                manager.resolve_via_path(
                    path="/gtest3/repair-canary/alpha",
                    resolver_path="/resolver",
                    worker_path="/worker",
                    manifest_out="/tmp/manifest.json",
                    observations_out="/tmp/observations.json",
                )

    def test_resolve_via_backend_path_maps_recorded_brick_root_without_mount_probe(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3b/gtest3/brick",
            "node-b": "/gluster/gtest3b/gtest3/brick",
            "node-c": "/gluster/gtest3b/gtest3/brick",
        }
        backend_path = "/gluster/gtest3b/gtest3/brick/repair-canary/alpha/payload.txt"
        with (
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager._resolve_entries_via_workers", return_value={
                "raw_entries": 1,
                "unique_raw_entries": 1,
                "observations": 3,
                "object_types": {"file": 1},
            }) as resolve_mock,
        ):
            result = manager.resolve_via_backend_path(
                volume="gtest3",
                backend_path=backend_path,
                mountpoint="/operator/view",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )
        self.assertEqual("gtest3", result["volume"])
        self.assertEqual("backend-path", result["evidence_route"])
        self.assertFalse(result["mount_probe"])
        self.assertEqual("repair-canary/alpha/payload.txt", result["logical_path"])
        kwargs = resolve_mock.call_args.kwargs
        self.assertEqual("localhost", kwargs["heal_entries"][0].source_host)
        self.assertEqual("/repair-canary/alpha/payload.txt", kwargs["heal_entries"][0].raw)
        self.assertEqual("/operator/view", kwargs["mountpoint"])
        self.assertFalse(kwargs["probe_mount"])


    def test_resolve_via_gfid_uses_bare_uuid_and_default_mountpoint(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3b/gtest3/brick",
            "node-b": "/gluster/gtest3b/gtest3/brick",
            "node-c": "/gluster/gtest3b/gtest3/brick",
        }
        canonical = "90000000-0000-4000-8000-000000000003"
        with (
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager._resolve_entries_via_workers", return_value={
                "raw_entries": 1,
                "unique_raw_entries": 1,
                "observations": 3,
                "object_types": {"unknown": 1},
            }) as resolve_mock,
        ):
            result = manager.resolve_via_gfid(
                volume="gtest3",
                gfid="90000000000040008000000000000003",
                mountpoint=None,
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )

        self.assertEqual("gfid", result["evidence_route"])
        self.assertEqual("gfid", result["repair_meta_input_kind"])
        kwargs = resolve_mock.call_args.kwargs
        self.assertEqual("/gtest3", kwargs["mountpoint"])
        self.assertEqual(f"<gfid:{canonical}>", kwargs["heal_entries"][0].raw)
        self.assertEqual("localhost", kwargs["heal_entries"][0].source_host)
        self.assertEqual(f"operator-gfid:{canonical}", kwargs["heal_entries"][0].brick)

    def test_resolve_via_gfid_child_uses_child_entry_shape(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3b/gtest3/brick",
            "node-b": "/gluster/gtest3b/gtest3/brick",
            "node-c": "/gluster/gtest3b/gtest3/brick",
        }
        canonical = "90000000-0000-4000-8000-000000000003"
        raw_entry = f"<gfid:{canonical}>/alpha/payload.txt"
        with (
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager._resolve_entries_via_workers", return_value={
                "raw_entries": 1,
                "unique_raw_entries": 1,
                "observations": 3,
                "object_types": {"unknown": 1},
            }) as resolve_mock,
        ):
            result = manager.resolve_via_gfid_child(
                volume="gtest3",
                gfid_child=raw_entry,
                mountpoint="/operator/view",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )

        self.assertEqual("gfid-child", result["evidence_route"])
        self.assertEqual("gfid-child", result["repair_meta_input_kind"])
        kwargs = resolve_mock.call_args.kwargs
        self.assertEqual("/operator/view", kwargs["mountpoint"])
        self.assertEqual(2, len(kwargs["heal_entries"]))
        self.assertEqual(raw_entry, kwargs["heal_entries"][0].raw)
        self.assertEqual(f"<gfid:{canonical}>", kwargs["heal_entries"][1].raw)
        self.assertEqual("localhost", kwargs["heal_entries"][0].source_host)
        self.assertEqual(f"operator-gfid-child:{canonical}/alpha/payload.txt", kwargs["heal_entries"][0].brick)
        self.assertFalse(kwargs["probe_mount"])

    def test_resolve_via_index_entry_validates_and_normalizes_path(self) -> None:
        brick_paths = {
            "node-a": "/gluster/gtest3b/gtest3/brick",
            "node-b": "/gluster/gtest3b/gtest3/brick",
            "node-c": "/gluster/gtest3b/gtest3/brick",
        }
        index_entry = ".glusterfs/indices/xattrop/90000000-0000-4000-8000-000000000003"
        with (
            patch("gluster_heal_tool.manager.discover_brick_hosts", return_value=list(brick_paths)),
            patch("gluster_heal_tool.manager.discover_brick_paths", return_value=brick_paths),
            patch("gluster_heal_tool.manager._resolve_entries_via_workers", return_value={
                "raw_entries": 1,
                "unique_raw_entries": 1,
                "observations": 3,
                "object_types": {"unknown": 1},
            }) as resolve_mock,
        ):
            result = manager.resolve_via_index_entry(
                volume="gtest3",
                index_entry=index_entry,
                mountpoint="/operator/view",
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )

        self.assertEqual("index-entry", result["evidence_route"])
        self.assertEqual("index-entry", result["repair_meta_input_kind"])
        kwargs = resolve_mock.call_args.kwargs
        self.assertEqual("/operator/view", kwargs["mountpoint"])
        self.assertEqual(f"/{index_entry}", kwargs["heal_entries"][0].raw)
        self.assertEqual("localhost", kwargs["heal_entries"][0].source_host)
        self.assertEqual(f"operator-index-entry:/{index_entry}", kwargs["heal_entries"][0].brick)

    def test_resolve_via_index_entry_rejects_non_index_path(self) -> None:
        with self.assertRaises(RuntimeError):
            manager.resolve_via_index_entry(
                volume="gtest3",
                index_entry="not-a-valid-index-entry",
                mountpoint=None,
                resolver_path="/resolver",
                worker_path="/worker",
                manifest_out="/tmp/manifest.json",
                observations_out="/tmp/observations.json",
            )
    def test_resolve_via_backend_path_rejects_unrecorded_path(self) -> None:
        with patch(
            "gluster_heal_tool.manager.discover_brick_paths",
            return_value={"node-a": "/gluster/gtest3b/gtest3/brick"},
        ):
            with self.assertRaises(RuntimeError):
                manager.resolve_via_backend_path(
                    volume="gtest3",
                    backend_path="/gluster/other/brick/alpha/payload.txt",
                    mountpoint=None,
                    resolver_path="/resolver",
                    worker_path="/worker",
                    manifest_out="/tmp/manifest.json",
                    observations_out="/tmp/observations.json",
                )


    def test_resolve_via_workers_uses_local_command_for_local_host(self) -> None:
        heal_entries = [
            manager.HealEntry(
                raw='/repair-canary/alpha',
                source_host='node-a',
                brick='localhost:gtest3',
                index_on_host=0,
            )
        ]
        completed = type('Completed', (), {'returncode': 0, 'stdout': '{"observations": []}', 'stderr': ''})()
        with (
            patch('gluster_heal_tool.manager._local_host_aliases', return_value={'node-a'}),
            patch('gluster_heal_tool.manager.subprocess.run', return_value=completed) as run_mock,
            patch('gluster_heal_tool.manager.build_manifest', return_value=({}, [])),
            patch('gluster_heal_tool.manager.write_manifest'),
            patch('gluster_heal_tool.manager.dump_observations'),
        ):
            result = manager._resolve_entries_via_workers(
                heal_entries=heal_entries,
                volume='gtest3',
                hosts=['node-a'],
                brick_path='/gluster/gtest3b/gtest3/brick',
                mountpoint='/gtest3',
                resolver_path='/resolver',
                worker_path='/worker',
                manifest_out='/tmp/manifest.json',
                observations_out='/tmp/observations.json',
                ssh_user='root',
            )

        self.assertFalse(result['object_types'])
        expected = ['/worker', 'resolve-batch'] if os.geteuid() == 0 else ['sudo', '-n', '/worker', 'resolve-batch']
        self.assertEqual(expected, run_mock.call_args.args[0])
