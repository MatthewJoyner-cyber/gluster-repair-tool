# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for directory recipes."""
from __future__ import annotations

import io
import json
import tempfile
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]

from gluster_heal_tool.apply import (
    build_apply_results,
    execute_apply_results,
    render_apply_run,
    render_apply_summary,
    validate_execute_results,
)
from gluster_heal_tool.apply_reporting import summarize_apply_results
from gluster_heal_tool.cli import _execution_progress_callback, build_parser
from gluster_heal_tool.controller_paths import default_stage_local_path
from gluster_heal_tool.install_paths import DEFAULT_HOST_OPS_PATH, DEFAULT_SERVICE_USER
from gluster_heal_tool.manifest import build_manifest
from gluster_heal_tool.models import ApplyActionResult, ApplyStep, HealEntry
from gluster_heal_tool.models import ManifestObject, ResolutionObservation
from gluster_heal_tool.protocol import ResolveBatchRequest
from gluster_heal_tool.planner import build_plan
from gluster_heal_tool.remote_ops import SSH_TRANSPORT
from gluster_heal_tool.volume import HEAL_OPTIONS, HealSettingsRestoreError, set_heal_settings_exact
from gluster_heal_tool.worker import _resolve_one
from gluster_heal_tool.worker import _resolve_symlink_chain


class _StaticResolver:
    def __init__(self, mapping: dict[str, list[ResolutionObservation]]) -> None:
        self.mapping = mapping

    def resolve_entry(self, raw_entry: str) -> list[ResolutionObservation]:
        return list(self.mapping.get(raw_entry, []))


def _dir_obs(
    host: str,
    backend: str,
    gfid: str,
    *,
    present: bool,
    logical_path: str = "example/path",
    child_names: list[str] | None = None,
    mounted_error: str = "",
    backend_mdata_hex: str = "",
) -> ResolutionObservation:
    mount_visible = present and not mounted_error
    return ResolutionObservation(
        host=host,
        raw_entry=f"/{logical_path}",
        gfid=gfid,
        gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
        backend=backend,
        type="directory",
        gfid_exists=True,
        file_gfid="",
        file_gfid_path="",
        relpath=logical_path,
        mounted=f"/{logical_path}",
        depth=3,
        mounted_checked=True,
        mounted_lexists=mount_visible,
        mounted_exists=mount_visible,
        mounted_lstat_type="dir" if mount_visible else "",
        mounted_error=mounted_error,
        backend_lexists=True,
        backend_exists=True,
        backend_mtime=100,
        backend_size=0,
        backend_mode="drwxr-xr-x",
        backend_lstat_type="dir",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid=gfid,
        backend_mdata_hex=backend_mdata_hex,
        backend_child_names=list(child_names or []),
        gfid_path_lexists=True,
        gfid_path_exists=True,
        gfid_path_lstat_type="symlink",
        gfid_path_is_symlink=True,
        gfid_path_readlink=backend,
        error="",
    )


def _file_obs(
    host: str,
    backend: str,
    file_gfid: str,
    *,
    present: bool,
    logical_path: str = "example/file",
) -> ResolutionObservation:
    return ResolutionObservation(
        host=host,
        raw_entry=f"/{logical_path}",
        gfid=file_gfid,
        gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
        backend=backend,
        type="file",
        gfid_exists=True,
        file_gfid=file_gfid,
        file_gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
        relpath=logical_path,
        mounted=f"/{logical_path}",
        depth=3,
        mounted_checked=True,
        mounted_lexists=present,
        mounted_exists=present,
        mounted_lstat_type="file" if present else "",
        backend_lexists=present,
        backend_exists=present,
        backend_mtime=100,
        backend_size=17,
        backend_mode="-rw-r--r--",
        backend_lstat_type="file",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid=file_gfid,
        gfid_path_lexists=present,
        gfid_path_exists=present,
        gfid_path_lstat_type="symlink" if present else "",
        gfid_path_is_symlink=present,
        gfid_path_readlink=backend,
        error="",
    )


def _symlink_obs(
    host: str,
    backend: str,
    file_gfid: str,
    *,
    logical_path: str = "example/symlink",
    target: str = "/external/target",
) -> ResolutionObservation:
    return ResolutionObservation(
        host=host,
        raw_entry=f"/{logical_path}",
        gfid=file_gfid,
        gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
        backend=backend,
        type="file",
        gfid_exists=True,
        file_gfid=file_gfid,
        file_gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
        relpath=logical_path,
        mounted=f"/{logical_path}",
        depth=3,
        mounted_checked=True,
        mounted_lexists=True,
        mounted_exists=False,
        mounted_lstat_type="symlink",
        backend_lexists=True,
        backend_exists=False,
        backend_mtime=100,
        backend_size=0,
        backend_mode="lrwxrwxrwx",
        backend_lstat_type="symlink",
        backend_is_symlink=True,
        backend_readlink=target,
        backend_trusted_gfid="",
        backend_child_names=[],
        gfid_path_lexists=True,
        gfid_path_exists=False,
        gfid_path_lstat_type="symlink",
        gfid_path_is_symlink=True,
        gfid_path_readlink=backend,
        gfid_path_terminal_path="",
        gfid_path_terminal_lstat_type="",
        gfid_path_terminal_is_symlink=False,
        gfid_path_terminal_readlink="",
        gfid_path_terminal_trusted_gfid="",
        error="",
    )


def _dead_gfid_reference_obs(
    host: str,
    backend: str,
    gfid: str,
    *,
    live_ref: str,
) -> ResolutionObservation:
    return ResolutionObservation(
        host=host,
        raw_entry=f"<gfid:{gfid}>",
        gfid=gfid,
        gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
        backend=backend,
        type="unknown",
        gfid_exists=True,
        relpath="",
        mounted="",
        depth=0,
        mounted_checked=False,
        mounted_lexists=False,
        mounted_exists=False,
        mounted_lstat_type="",
        mounted_error="",
        backend_lexists=False,
        backend_exists=False,
        backend_mtime=None,
        backend_size=None,
        backend_mode="",
        backend_lstat_type="",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid="",
        gfid_path_lexists=True,
        gfid_path_exists=True,
        gfid_path_lstat_type="symlink",
        gfid_path_is_symlink=True,
        gfid_path_readlink=live_ref,
        error="",
    )


def _dead_gfid_cleanup_obs(
    host: str,
    backend: str,
    gfid: str,
) -> ResolutionObservation:
    return ResolutionObservation(
        host=host,
        raw_entry=f"<gfid:{gfid}>",
        gfid=gfid,
        gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
        backend=backend,
        type="unknown",
        gfid_exists=True,
        relpath="",
        mounted="",
        depth=0,
        mounted_checked=False,
        mounted_lexists=False,
        mounted_exists=False,
        mounted_lstat_type="",
        mounted_error="",
        backend_lexists=False,
        backend_exists=False,
        backend_mtime=None,
        backend_size=None,
        backend_mode="",
        backend_lstat_type="",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid="",
        gfid_path_lexists=True,
        gfid_path_exists=True,
        gfid_path_lstat_type="symlink",
        gfid_path_is_symlink=True,
        gfid_path_readlink="",
        error="",
    )


def _stale_index_obs(
    host: str,
    backend: str,
    *,
    logical_path: str = ".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555",
    child_names: list[str] | None = None,
    present: bool = True,
) -> ResolutionObservation:
    return ResolutionObservation(
        host=host,
        raw_entry=f"/{logical_path}",
        gfid="",
        gfid_path="",
        backend=backend,
        type="unknown",
        gfid_exists=False,
        file_gfid="",
        file_gfid_path="",
        relpath=logical_path,
        mounted="",
        depth=5,
        mounted_checked=False,
        mounted_lexists=False,
        mounted_exists=False,
        mounted_lstat_type="",
        mounted_error="",
        backend_lexists=present,
        backend_exists=present,
        backend_mtime=100,
        backend_size=0,
        backend_mode="drwxr-xr-x",
        backend_lstat_type="dir",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid="",
        backend_child_names=list(child_names or []),
        gfid_path_lexists=False,
        gfid_path_exists=False,
        gfid_path_lstat_type="",
        gfid_path_is_symlink=False,
        gfid_path_readlink="",
        error="",
    )


class DirectoryRecipeTests(unittest.TestCase):
    def test_symlink_chain_resolver_tracks_terminal_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_dir = root / "real-dir"
            real_dir.mkdir()
            link1 = root / "link-1"
            link1.symlink_to("real-dir")
            link2 = root / "link-2"
            link2.symlink_to("link-1")

            terminal_path, terminal_kind, chain, terminal_readlink = _resolve_symlink_chain(str(link2))

            self.assertEqual(str(real_dir), terminal_path)
            self.assertEqual("dir", terminal_kind)
            self.assertEqual([str(root / "link-1"), str(real_dir)], chain)
            self.assertEqual("real-dir", terminal_readlink)

    def test_reference_chain_resolver_tracks_regular_file_gfid2path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handle = root / "handle"
            handle.write_text("handle")
            target = root / "missing" / "payload.txt"

            with patch("gluster_heal_tool.worker._gfid2path_xattr", return_value=str(target)):
                terminal_path, terminal_kind, chain, terminal_readlink = _resolve_symlink_chain(str(handle))

            self.assertEqual(str(target), terminal_path)
            self.assertEqual("", terminal_kind)
            self.assertEqual([str(target)], chain)
            self.assertEqual(str(target), terminal_readlink)

    def test_resolve_one_tracks_regular_file_gfid2path_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brick = root / "brick"
            handle = brick / ".glusterfs" / "2d" / "59" / "2d599215-a3fc-4132-8771-898896db8200"
            handle.parent.mkdir(parents=True)
            handle.write_text("handle")
            target = root / "repair-canary-handle-ghost" / "alpha" / "ghost-payload.txt"
            request = ResolveBatchRequest(
                volume="gtest",
                brick_path=str(brick),
                mountpoint=str(root),
                resolver_path="/bin/true",
                entries=[f"<gfid:dddd1111-aaaa-2222-3333-444455556677>/alpha/payload.txt"],
                probe_mount=False,
            )
            stdout = "\n".join(
                [
                    "GFID=dddd1111-aaaa-2222-3333-444455556677",
                    "GFID_PATH=" + str(handle),
                    "BACKEND=" + str(handle),
                    "RELPATH=repair-canary-handle-ghost/alpha/payload.txt",
                    "TYPE=unknown",
                    "DEPTH=3",
                ]
            )
            fake_proc = type("Proc", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

            with patch("gluster_heal_tool.worker.subprocess.run", return_value=fake_proc), patch(
                "gluster_heal_tool.worker._gfid2path_xattr", return_value=str(target)
            ):
                observation = _resolve_one(request, f"<gfid:dddd1111-aaaa-2222-3333-444455556677>/alpha/payload.txt")

            self.assertEqual(str(target), observation.backend_terminal_path)
            self.assertEqual(str(target), observation.gfid_path_terminal_path)
            self.assertEqual([str(target)], observation.backend_readlink_chain)
            self.assertEqual([str(target)], observation.gfid_path_readlink_chain)
            self.assertEqual("", observation.backend_terminal_lstat_type)
            self.assertEqual("", observation.gfid_path_terminal_lstat_type)

    def test_symlink_chain_resolver_prefers_gfid2path_xattr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_dir = root / "real-dir"
            real_dir.mkdir()
            link = root / "link"
            link.symlink_to("wrong-target")

            def fake_gfid2path(path: str) -> str:
                return str(real_dir) if path == str(link) else ""

            with patch("gluster_heal_tool.worker._gfid2path_xattr", side_effect=fake_gfid2path):
                terminal_path, terminal_kind, chain, terminal_readlink = _resolve_symlink_chain(str(link))

            self.assertEqual(str(real_dir), terminal_path)
            self.assertEqual("dir", terminal_kind)
            self.assertEqual([str(real_dir)], chain)
            self.assertEqual(str(real_dir), terminal_readlink)

    def test_manifest_records_operator_input_source(self) -> None:
        class _StaticResolver:
            def __init__(self, mapping: dict[str, list[ResolutionObservation]]) -> None:
                self.mapping = mapping

            def resolve_entry(self, raw_entry: str) -> list[ResolutionObservation]:
                return list(self.mapping.get(raw_entry, []))

        raw_entry = "/bookmarks"
        obs = ResolutionObservation(
            host="host-a",
            raw_entry=raw_entry,
            gfid="aaaa1111-bbbb-2222-cccc-333344445555",
            gfid_path="/.glusterfs/aa/aa/aaaa1111-bbbb-2222-cccc-333344445555",
            backend="/srv/gluster/brick-store/gtest/user-d/.config/gtk-3.0/bookmarks",
            type="unknown",
            gfid_exists=True,
            relpath="bookmarks",
            mounted="/bookmarks",
            depth=1,
            backend_lexists=True,
            backend_exists=True,
            backend_lstat_type="symlink",
            backend_is_symlink=True,
            backend_readlink="../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0",
            backend_readlink_chain=["../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0"],
            backend_terminal_path="/srv/example-users/user-d/.config/gtk-3.0",
            backend_terminal_lstat_type="dir",
            backend_terminal_is_symlink=False,
            backend_terminal_readlink="",
            gfid_path_lexists=True,
            gfid_path_exists=True,
            gfid_path_lstat_type="symlink",
            gfid_path_is_symlink=True,
            gfid_path_readlink="../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0",
            gfid_path_readlink_chain=["../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0"],
            gfid_path_terminal_path="/srv/example-users/user-d/.config/gtk-3.0",
            gfid_path_terminal_lstat_type="dir",
            gfid_path_terminal_is_symlink=False,
            gfid_path_terminal_readlink="",
        )
        manifest, _ = build_manifest(
            [
                HealEntry(
                    raw=raw_entry,
                    source_host="host-a",
                    brick="brick",
                    index_on_host=0,
                    input_source="operator_path",
                )
            ],
            _StaticResolver({raw_entry: [obs]}),
        )

        self.assertEqual("operator_path", manifest["bookmarks"].input_source)

    def test_manifest_classifies_symlink_terminal_directory_as_directory(self) -> None:
        class _StaticResolver:
            def __init__(self, mapping: dict[str, list[ResolutionObservation]]) -> None:
                self.mapping = mapping

            def resolve_entry(self, raw_entry: str) -> list[ResolutionObservation]:
                return list(self.mapping.get(raw_entry, []))

        raw_entry = "/bookmarks"
        obs = ResolutionObservation(
            host="host-a",
            raw_entry=raw_entry,
            gfid="aaaa1111-bbbb-2222-cccc-333344445555",
            gfid_path="/.glusterfs/aa/aa/aaaa1111-bbbb-2222-cccc-333344445555",
            backend="/srv/gluster/brick-store/gtest/user-d/.config/gtk-3.0/bookmarks",
            type="unknown",
            gfid_exists=True,
            relpath="bookmarks",
            mounted="/bookmarks",
            depth=1,
            backend_lexists=True,
            backend_exists=True,
            backend_lstat_type="symlink",
            backend_is_symlink=True,
            backend_readlink="../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0",
            backend_readlink_chain=["../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0"],
            backend_terminal_path="/srv/example-users/user-d/.config/gtk-3.0",
            backend_terminal_lstat_type="dir",
            backend_terminal_is_symlink=False,
            backend_terminal_readlink="",
            gfid_path_lexists=True,
            gfid_path_exists=True,
            gfid_path_lstat_type="symlink",
            gfid_path_is_symlink=True,
            gfid_path_readlink="../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0",
            gfid_path_readlink_chain=["../../b8/bb/b8bb25a6-106d-4749-9544-d960dc8f6934/gtk-3.0"],
            gfid_path_terminal_path="/srv/example-users/user-d/.config/gtk-3.0",
            gfid_path_terminal_lstat_type="dir",
            gfid_path_terminal_is_symlink=False,
            gfid_path_terminal_readlink="",
        )
        manifest, _ = build_manifest([HealEntry(raw=raw_entry, source_host="host-a", brick="brick", index_on_host=0)], _StaticResolver({raw_entry: [obs]}))

        self.assertEqual("directory", manifest["bookmarks"].object_type)

    def test_fake_directory_canary_builds_brick_side_recipe(self) -> None:
        canonical_gfid = "0f0f0f0f-1111-2222-3333-444444444444"
        logical_path = "repair-canary-123/subdir"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual(logical_path, action.logical_path)
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("recreate_missing_directory_backend", action.repair_strategy)
        self.assertEqual("safe_default", action.decision_class)
        self.assertEqual("directory_restore_backend", action.graph_node)
        self.assertEqual("host-c", action.directory_canonical_host)
        self.assertEqual(1, len(action.missing_hosts))
        self.assertEqual(["host-d"], action.missing_hosts)
        self.assertEqual(backend, action.directory_target_backend_by_host["host-d"])

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "review"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertIn("mkdir_directory_backend", [step.step_type for step in result.steps])
        self.assertIn("attach_directory_gfid", [step.step_type for step in result.steps])
        self.assertIn("verify_directory_backend", [step.step_type for step in result.steps])
        self.assertNotIn("review_directory_backend_recreate", [step.step_type for step in result.steps])
        self.assertEqual(backend, next(step.target_path for step in result.steps if step.step_type == "mkdir_directory_backend"))
        self.assertNotIn("ensure_directory_via_mount", [step.step_type for step in result.steps])

    def test_reconcile_directory_children_without_child_action_stays_review(self) -> None:
        canonical_gfid = "1f1f1f1f-2222-3333-4444-555555555555"
        logical_path = "repair-canary-deep-tree-1/tree/level1/level2"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"

        action = {
            "action_id": "child-gap-1",
            "action_type": "reconcile_directory",
            "logical_path": logical_path,
            "repair_strategy": "reconcile_directory_children",
            "directory_canonical_host": "host-c",
            "directory_canonical_backend": backend,
            "directory_canonical_gfid": canonical_gfid,
            "missing_hosts": ["host-d"],
            "missing_directory_children_by_host": {"host-d": ["level2.txt", "level3"]},
            "mounted_target": f"/{logical_path}",
            "graph_markers": ["directory_child_gap:bounded_subtree_candidate"],
            "notes": ["saw_dir"],
        }

        results = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "review"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review", result.status)
        self.assertEqual([], result.steps)
        rendered = "\n".join(result.notes)
        self.assertIn("no executable child action is attached", rendered)
        self.assertIn("collect immediate GFID-child evidence", rendered)
        self.assertNotIn("rewrite the already-present parent", rendered)

    def test_directory_backup_targets_use_staged_prefix_when_root_missing(self) -> None:
        canonical_gfid = "0f0f0f0f-1111-2222-3333-444444444445"
        logical_path = "repair-canary-123/subdir"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        stale_gfid_path = f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=canonical_gfid,
                            gfid_path=stale_gfid_path,
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("recreate_missing_directory_backend", action.repair_strategy)
        self.assertIn("host-d", action.stale_gfid_paths_by_host)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "review"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        backup_step = next(step for step in result.steps if step.step_type == "backup_stale_dir_gfid")
        self.assertTrue(backup_step.target_path.startswith("/var/tmp/gluster-repair/backups/"))
        self.assertIn(".backup/host-d/dir-gfid/", backup_step.target_path)

    def test_file_stale_survivor_delete_backs_up_then_removes(self) -> None:
        file_gfid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        logical_path = "repair-canary-130/stale-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        missing_obs = lambda host: ResolutionObservation(
            host=host,
            raw_entry=f"/{logical_path}",
            gfid=file_gfid,
            gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
            backend=backend,
            type="file",
            gfid_exists=True,
            file_gfid=file_gfid,
            file_gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
            relpath=logical_path,
            mounted=f"/{logical_path}",
            depth=3,
            mounted_checked=True,
            mounted_lexists=False,
            mounted_exists=False,
            mounted_lstat_type="",
            backend_lexists=False,
            backend_exists=False,
            backend_mtime=None,
            backend_size=None,
            backend_mode="",
            backend_lstat_type="",
            backend_is_symlink=False,
            backend_readlink="",
            backend_trusted_gfid="",
            gfid_path_lexists=True,
            gfid_path_exists=True,
            gfid_path_lstat_type="symlink",
            gfid_path_is_symlink=True,
            gfid_path_readlink=backend,
            error="",
        )

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=3,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_file_obs("host-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-b": [missing_obs("host-b")],
                    "host-c": [missing_obs("host-c")],
                    "host-d": [missing_obs("host-d")],
                },
                notes=["saw_file", "mount_missing_while_backend_present:host-a"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_file", action.repair_strategy)
        self.assertEqual(["host-a"], action.healthy_hosts)
        self.assertEqual(["host-b", "host-c", "host-d"], action.missing_hosts)

        review_action = action.to_dict()
        review_action.pop("decision", None)

        results = build_apply_results(
            {"actions": [review_action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "review"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertTrue(any(artifact.kind == "gfid" for artifact in result.backup_artifacts))
        self.assertIn("remove_stale_gfid", [step.step_type for step in result.steps])
        self.assertIn("remove_restored_mount_file", [step.step_type for step in result.steps])
        self.assertNotIn("restore_missing_replica", " ".join(result.notes))

    def test_file_stale_survivor_with_backend_remnant_deletes_without_recreate(self) -> None:
        file_gfid = "88888888-aaaa-bbbb-cccc-dddddddddddd"
        logical_path = "repair-canary-131/stale-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=3,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_file_obs("host-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, file_gfid, present=False, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, file_gfid, present=False, logical_path=logical_path)],
                    "host-d": [_file_obs("host-d", backend, file_gfid, present=False, logical_path=logical_path)],
                },
                notes=["saw_file", "mount_missing_while_backend_present:host-a"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_file", action.repair_strategy)
        self.assertEqual(["host-a"], action.healthy_hosts)
        self.assertEqual(["host-b", "host-c", "host-d"], action.missing_hosts)

        review_action = action.to_dict()
        review_action.pop("decision", None)

        results = build_apply_results(
            {"actions": [review_action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "review"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("remove_stale_gfid", step_types)
        self.assertIn("remove_restored_mount_file", step_types)
        self.assertNotIn("restore_missing_replica", " ".join(result.notes))

    def test_symlink_stale_survivor_promotes_to_cleanup_when_policy_deletes(self) -> None:
        file_gfid = "77777777-aaaa-bbbb-cccc-dddddddddddd"
        logical_path = "repair-canary-131/orphaned-symlink"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"

        def missing_obs(host: str) -> ResolutionObservation:
            return ResolutionObservation(
                host=host,
                raw_entry=f"/{logical_path}",
                gfid=file_gfid,
                gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
                backend=backend,
                type="file",
                gfid_exists=True,
                file_gfid=file_gfid,
                file_gfid_path=f"/.glusterfs/{file_gfid[:2]}/{file_gfid[2:4]}/{file_gfid}",
                relpath=logical_path,
                mounted=f"/{logical_path}",
                depth=3,
                mounted_checked=True,
                mounted_lexists=False,
                mounted_exists=False,
                mounted_lstat_type="",
                backend_lexists=False,
                backend_exists=False,
                backend_mtime=None,
                backend_size=None,
                backend_mode="",
                backend_lstat_type="",
                backend_is_symlink=False,
                backend_readlink="",
                backend_trusted_gfid="",
                gfid_path_lexists=True,
                gfid_path_exists=True,
                gfid_path_lstat_type="symlink",
                gfid_path_is_symlink=True,
                gfid_path_readlink=backend,
                error="",
            )

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=3,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_symlink_obs("host-a", backend, file_gfid, logical_path=logical_path)],
                    "host-b": [missing_obs("host-b")],
                    "host-c": [missing_obs("host-c")],
                    "host-d": [missing_obs("host-d")],
                },
                notes=["saw_symlink", "orphaned_symlink_present"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_orphaned_symlink", action.action_type)
        self.assertEqual("delete_orphaned_symlink_residue", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_orphaned_symlink", result.action_type)
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("remove_stale_backend", step_types)
        self.assertIn("remove_stale_gfid", step_types)
        self.assertNotIn("remove_restored_mount_file", step_types)

        review_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"probable_orphaned_symlink_file": "review"},
        )
        self.assertEqual(1, len(review_results))
        review_result = review_results[0]
        self.assertEqual("review_probable_orphaned_symlink", review_result.action_type)
        self.assertEqual("review", review_result.status)
        rendered = render_apply_run(review_results)
        self.assertIn("matrix suggestion: delete_orphaned_symlink_residue", rendered)
        self.assertIn("choice: apply suggestion, keep review, or skip", rendered)

    def test_type_mismatch_stays_review_only(self) -> None:
        manifest = {
            "repair-canary-133/type-mismatch": ManifestObject(
                logical_path="repair-canary-133/type-mismatch",
                object_type="type_mismatch",
                depth=2,
                raw_entries=["repair-canary-133/type-mismatch"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[],
                observations={},
                notes=["synthetic type mismatch case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_type_mismatch", action.action_type)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_type_mismatch", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_type_mismatch", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: quarantine_both", rendered)
        self.assertIn("type-mismatch recommended choice: quarantine_both", rendered)
        self.assertIn("choice: quarantine_loser, quarantine_both, keep review, or skip", rendered)
        self.assertIn("quarantine both branches first", rendered)

    def test_type_mismatch_quarantine_policy_is_reversible(self) -> None:
        action = {
            "action_id": "repair:type-mismatch",
            "logical_path": "type-mismatch/alpha",
            "action_type": "review_type_mismatch",
            "object_type": "type_mismatch",
            "repair_strategy": "review_type_mismatch",
            "mounted_target": "/gtest/type-mismatch/alpha",
            "file_copies": [
                {
                    "host": "node-a",
                    "backend": "/brick/type-mismatch/alpha",
                    "identity": "11111111-1111-1111-1111-111111111111",
                    "backend_trusted_gfid": "11111111-1111-1111-1111-111111111111",
                    "file_gfid_path": "/brick/.glusterfs/11/11/11111111-1111-1111-1111-111111111111",
                }
            ],
            "directory_copies": [
                {
                    "host": "node-c",
                    "backend": "/brick/type-mismatch/alpha",
                    "identity": "22222222-2222-2222-2222-222222222222",
                    "backend_trusted_gfid": "22222222-2222-2222-2222-222222222222",
                    "gfid_path": "/brick/.glusterfs/22/22/22222222-2222-2222-2222-222222222222",
                }
            ],
        }
        results = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"type_mismatch": "quarantine-both"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertIn("quarantine_conflicting_type_mismatch", " ".join(result.notes))
        step_types = [step.step_type for step in result.steps]
        self.assertIn("quarantine_file_backend", step_types)
        self.assertIn("quarantine_file_gfid", step_types)
        self.assertIn("quarantine_directory_backend", step_types)
        self.assertIn("quarantine_directory_gfid", step_types)
        self.assertTrue(any(step.step_type == "restore_quarantined_directory_gfid" for step in result.revert_steps))
        valid, errors = validate_execute_results(results)
        self.assertTrue(valid, errors)

    def test_type_mismatch_quarantine_loser_moves_only_complete_older_branch(self) -> None:
        logical_path = "type-mismatch/older-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        file_gfid = "11111111-1111-1111-1111-111111111111"
        directory_gfid = "22222222-2222-2222-2222-222222222222"
        observations = {}
        for index, host in enumerate(("host-a", "host-b")):
            observation = _file_obs(
                host,
                backend,
                file_gfid,
                present=True,
                logical_path=logical_path,
            )
            observation.backend_mtime = 100 + index
            observations[host] = [observation]
        observation = _dir_obs(
            "host-c",
            backend,
            directory_gfid,
            present=True,
            logical_path=logical_path,
        )
        observation.backend_mtime = 200
        observations["host-c"] = [observation]
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="type_mismatch",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(observations),
                gfids=[file_gfid, directory_gfid],
                file_gfids=[file_gfid],
                observations=observations,
                brick_roles_by_host={
                    "host-a": "data",
                    "host-b": "arbiter",
                    "host-c": "data",
                },
                notes=["synthetic complete type mismatch"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("quarantine_loser", action.recommended_choice)
        self.assertEqual("file", action.type_mismatch_loser)
        self.assertEqual({"host-a", "host-b"}, {copy["host"] for copy in action.file_copies})
        self.assertEqual({"host-c"}, {copy["host"] for copy in action.directory_copies})

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"type_mismatch": "quarantine-loser"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertEqual(2, step_types.count("quarantine_file_backend"))
        self.assertNotIn("quarantine_directory_backend", step_types)
        self.assertIn("complete older file branch", " ".join(result.notes))
        valid, errors = validate_execute_results(results)
        self.assertTrue(valid, errors)

    def test_type_mismatch_quarantine_loser_requires_fresh_complete_proof(self) -> None:
        action = {
            "action_id": "repair:type-mismatch-old-plan",
            "logical_path": "type-mismatch/old-plan",
            "action_type": "review_type_mismatch",
            "object_type": "type_mismatch",
            "repair_strategy": "review_type_mismatch",
            "file_copies": [
                {"host": "host-a", "backend": "/brick/a/alpha", "mtime": 100},
            ],
            "directory_copies": [
                {"host": "host-b", "backend": "/brick/b/alpha", "mtime": 200},
            ],
        }
        results = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"type_mismatch": "quarantine-loser"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review", result.status)
        self.assertEqual("quarantine_both", result.recommended_choice)
        self.assertEqual([], result.steps)
        self.assertIn("freshly planned, complete", " ".join(result.notes))

    def test_file_metadata_stays_review_only(self) -> None:
        file_gfid = "cccc1111-dddd-eeee-ffff-000011112222"
        logical_path = "repair-canary-134/file-metadata"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_file_obs("host-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-d": [_file_obs("host-d", backend, file_gfid, present=True, logical_path=logical_path)],
                },
                notes=["synthetic file metadata case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_file_metadata", action.action_type)
        self.assertEqual("review_metadata_only", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_file_metadata", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_file_metadata", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: enable_file_metadata_repair_policy", rendered)
        self.assertIn("why: The file already exists on all replicas; confirm canonical GFID evidence", rendered)
        self.assertIn(
            "rerun with --policy-file-metadata repair",
            rendered.lower(),
        )
        self.assertIn("choice: enable metadata repair policy, keep review, or skip", rendered)
        self.assertNotIn("interactive choices:", rendered)

    def test_review_directory_metadata_renders_evidence_notes(self) -> None:
        result = ApplyActionResult(
            action_id="repair:SteamShare/thomas",
            logical_path="SteamShare/thomas",
            action_type="review_directory_metadata",
            execution_mode="dry-run",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="review",
            notes=[
                "children must be repaired before parent directory",
                "directory child-set reconciliation needed across hosts: node-d, node-b, node-a, node-c",
                "directory tie preview: stable; review_directory_metadata",
                "directory depth cap reached: directory depth 6 exceeds max_depth=3",
                "no missing directory or stale metadata found; review only",
                "repair strategy: review_directory_state",
                "configured batch policy: review",
                "batch default: keep review-only; no automatic directory metadata action",
            ],
        )

        rendered = render_apply_run([result])
        self.assertIn("matrix suggestion: reconcile_directory_children", rendered)
        self.assertIn(
            "why: The directory is present in the manifest, and no missing directory or stale metadata target was found",
            rendered,
        )
        self.assertIn(
            "Next edge: follow the directory child chain first "
            "(review_directory_children / reconcile_directory_children for the same subtree)",
            rendered,
        )
        self.assertIn(
            "the next run can promote to repair_directory_metadata",
            rendered,
        )
        self.assertIn("evidence: children must be repaired before parent directory", rendered)
        self.assertIn(
            "evidence: directory child-set reconciliation needed across hosts: node-d, node-b, node-a, node-c",
            rendered,
        )
        self.assertIn("evidence: directory depth cap reached: directory depth 6 exceeds max_depth=3", rendered)
        self.assertIn("evidence: no missing directory or stale metadata found; review only", rendered)
        self.assertIn("support: for a classifier ticket, include action_id=repair:SteamShare/thomas", rendered)
        self.assertIn("support: include the manifest object, plan action, apply action, current heal info", rendered)
        self.assertIn("support: include targeted log excerpts from gluster-log-ops.sh grep", rendered)
        self.assertIn("support: include directory child-name sets, directory-tie-build output when available", rendered)
        self.assertIn("choice: follow child-chain repair, keep review, or skip", rendered)
        self.assertNotIn("evidence: configured batch policy: review", rendered)

    def test_review_directory_metadata_without_deeper_branch_lists_operator_choices(self) -> None:
        result = ApplyActionResult(
            action_id="repair:alpha",
            logical_path="alpha",
            action_type="review_directory_metadata",
            execution_mode="dry-run",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="review",
            brick_roles_by_host={"node-a": "data", "node-b": "data", "node-c": "arbiter"},
            notes=[
                "no missing directory or stale metadata found; review only",
                "repair strategy: review_directory_state",
                "configured batch policy: review",
            ],
        )

        rendered = render_apply_run([result])

        self.assertIn("matrix suggestion: refresh_directory_evidence", rendered)
        self.assertIn("Classified as directory metadata-only review", rendered)
        self.assertIn("run focused mount access and child-name probes", rendered)
        self.assertIn("promote to repair_directory_metadata only if", rendered)
        self.assertIn("choose delete_below_quorum_subtree only if", rendered)
        self.assertIn("run directory-tie-build or quarantine if names or GFIDs disagree", rendered)
        self.assertIn("arbiter evidence is metadata/quorum support only", rendered)
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("node-c: arbiter", rendered)
        self.assertIn(
            "choice: gather focused evidence, promote safe metadata repair, choose stale cleanup/quarantine, keep review, or skip",
            rendered,
        )

    def test_unknown_review_renders_generic_ticket_guidance(self) -> None:
        result = ApplyActionResult(
            action_id="repair:unknown-case",
            logical_path="unknown-case",
            action_type="review_unclassified_shape",
            execution_mode="dry-run",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="review",
            notes=[
                "graph marker: custom_future_case",
                "operator could not prove a safe branch",
                "review-only action; no filesystem steps are planned yet",
            ],
        )

        rendered = render_apply_run([result])
        self.assertIn("matrix suggestion: create_support_case", rendered)
        self.assertIn("evidence: graph marker: custom_future_case", rendered)
        self.assertIn("support: for a classifier ticket, include action_id=repair:unknown-case", rendered)
        self.assertIn("support: include raw entries, graph markers, all evidence notes printed above", rendered)
        self.assertNotIn("evidence: review-only action; no filesystem steps are planned yet", rendered)

    def test_orphaned_symlink_file_metadata_promotes_to_cleanup(self) -> None:
        file_gfid = "cccc1111-dddd-eeee-ffff-000011112223"
        logical_path = "repair-canary-134/orphaned-symlink"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_file_obs("host-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-d": [_file_obs("host-d", backend, file_gfid, present=True, logical_path=logical_path)],
                },
                notes=["saw_symlink", "orphaned_symlink_present"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_orphaned_symlink", action.action_type)
        self.assertEqual("delete_orphaned_symlink_residue", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_orphaned_symlink", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertTrue(
            any(
                step.step_type == "backup_stale_backend"
                and "cp -a" in " ".join(step.command_preview)
                for step in result.steps
            )
        )
        self.assertIn("remove_stale_backend", [step.step_type for step in result.steps])
        self.assertNotIn("remove_restored_mount_file", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("cleanup_orphaned_symlink", rendered)
        self.assertIn("delete_orphaned_symlink_residue", rendered)

    def test_file_metadata_policy_repair_promotes_to_attach_file_gfid(self) -> None:
        file_gfid = "cccc1111-dddd-eeee-ffff-000011112222"
        logical_path = "repair-canary-134/file-metadata"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_file_obs("host-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, file_gfid, present=True, logical_path=logical_path)],
                    "host-d": [_file_obs("host-d", backend, file_gfid, present=True, logical_path=logical_path)],
                },
                notes=["synthetic file metadata case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0].to_dict()
        action["file_copies"][0]["backend_trusted_gfid"] = "00000000-1111-2222-3333-444444444444"

        results = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"file_metadata_only": "repair"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_file_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertFalse(getattr(result, "native_heal_first", False))
        self.assertIn("attach_file_gfid", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertNotIn("run Gluster heal/rescan first", rendered)
        self.assertIn("configured batch policy: repair", rendered)
        self.assertIn("attach_file_gfid", rendered)

    def test_directory_presence_review_shows_child_paths(self) -> None:
        parent_gfid = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        child_gfid = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        parent_path = "repair-canary-138/alpha/beta"
        child_path = f"{parent_path}/child.txt"
        parent_backend = f"/srv/gluster/brick-store/gtest/{parent_path}"
        child_backend = f"/srv/gluster/brick-store/gtest/{child_path}"
        def _missing_dir_obs(host: str) -> ResolutionObservation:
            return ResolutionObservation(
                host=host,
                raw_entry=f"/{parent_path}",
                gfid=parent_gfid,
                gfid_path=f"/.glusterfs/{parent_gfid[:2]}/{parent_gfid[2:4]}/{parent_gfid}",
                backend=parent_backend,
                type="directory",
                gfid_exists=True,
                file_gfid="",
                file_gfid_path="",
                relpath=parent_path,
                mounted=f"/{parent_path}",
                depth=3,
                mounted_checked=True,
                mounted_lexists=False,
                mounted_exists=False,
                mounted_lstat_type="",
                mounted_error="",
                backend_lexists=False,
                backend_exists=False,
                backend_mtime=None,
                backend_size=None,
                backend_mode="",
                backend_lstat_type="",
                backend_is_symlink=False,
                backend_readlink="",
                backend_trusted_gfid="",
                gfid_path_lexists=False,
                gfid_path_exists=False,
                gfid_path_lstat_type="",
                gfid_path_is_symlink=False,
                gfid_path_readlink="",
                error="",
            )
        manifest = {
            parent_path: ManifestObject(
                logical_path=parent_path,
                object_type="directory_candidate",
                depth=3,
                raw_entries=[f"/{parent_path}"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[parent_gfid],
                children=[child_path],
                observations={
                    "host-a": [_missing_dir_obs("host-a")],
                    "host-b": [_missing_dir_obs("host-b")],
                    "host-c": [_missing_dir_obs("host-c")],
                    "host-d": [_missing_dir_obs("host-d")],
                },
                notes=["synthetic directory presence review"],
            ),
            child_path: ManifestObject(
                logical_path=child_path,
                object_type="file",
                depth=4,
                raw_entries=[child_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[child_gfid],
                observations={
                    "host-a": [_file_obs("host-a", child_backend, child_gfid, present=True, logical_path=child_path)],
                    "host-b": [_file_obs("host-b", child_backend, child_gfid, present=True, logical_path=child_path)],
                    "host-c": [_file_obs("host-c", child_backend, child_gfid, present=True, logical_path=child_path)],
                    "host-d": [_file_obs("host-d", child_backend, child_gfid, present=False, logical_path=child_path)],
                },
                notes=["synthetic child for directory presence review"],
            ),
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        parent = next(action for action in plan if action.logical_path == parent_path)
        self.assertEqual("review_directory_metadata", parent.action_type)
        self.assertEqual("review_directory_presence", parent.repair_strategy)
        self.assertEqual([f"repair:{child_path}"], parent.depends_on)

        results = build_apply_results(
            {"actions": [action.to_dict() for action in plan]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        rendered = render_apply_run(results)
        self.assertIn("review_directory_presence", rendered)
        self.assertIn(child_path, rendered)
        self.assertIn(
            "operator next: check mount access, child names, and missing-host count",
            rendered.lower(),
        )
        self.assertIn("rerun the same discovery route", rendered.lower())
        self.assertIn("repair-meta on the same path for a path-led review", rendered.lower())
        self.assertIn("fresh manifest-build and plan-build for a heal-driven scan", rendered.lower())

    def test_symlink_evidence_is_handled_as_file_family(self) -> None:
        file_gfid = "dddd1111-eeee-ffff-0000-111122223333"
        backend = "/srv/gluster/brick-store/gtest/repair-canary-135/symlink"
        manifest = {
            "repair-canary-135/symlink": ManifestObject(
                logical_path="repair-canary-135/symlink",
                object_type="file",
                depth=2,
                raw_entries=["repair-canary-135/symlink"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[file_gfid],
                observations={
                    "host-a": [_symlink_obs("host-a", backend, file_gfid, logical_path="repair-canary-135/symlink")],
                    "host-b": [_file_obs("host-b", backend, file_gfid, present=True, logical_path="repair-canary-135/symlink")],
                    "host-c": [_file_obs("host-c", backend, file_gfid, present=True, logical_path="repair-canary-135/symlink")],
                    "host-d": [_file_obs("host-d", backend, file_gfid, present=False, logical_path="repair-canary-135/symlink")],
                },
                notes=["synthetic symlink case", "saw_symlink"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_file", action.action_type)
        self.assertEqual("restore_missing_replica", action.repair_strategy)
        self.assertIn("file subtype: symlink", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_file", result.action_type)
        self.assertEqual("planned", result.status)
        steps_by_type = {step.step_type: step for step in result.steps}
        self.assertEqual(
            [
                "rsync",
                "-a",
                "-e",
                SSH_TRANSPORT,
                "--rsync-path",
                f"sudo -n {DEFAULT_HOST_OPS_PATH} rsync-server",
                f"{DEFAULT_SERVICE_USER}@host-c:{backend}",
                steps_by_type["stage_winner_local"].target_path,
            ],
            steps_by_type["stage_winner_local"].command_preview,
        )
        self.assertEqual(
            [
                "sudo",
                "-n",
                "cp",
                "-a",
                steps_by_type["restore_via_mount"].source_path,
                steps_by_type["restore_via_mount"].target_path,
            ],
            steps_by_type["restore_via_mount"].command_preview,
        )
        rendered = render_apply_run(results)
        self.assertIn("file subtype: symlink", rendered)

        with patch("gluster_heal_tool.executor._run_command") as run_command:
            run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            report = execute_apply_results(results, keep_going=True, stage_only=True)

        self.assertEqual(1, run_command.call_count)
        self.assertEqual(1, report["summary"]["completed_with_skips"])
        self.assertEqual("completed-with-nonblocking-skips", report["actions"][0]["status"])
        self.assertEqual("ok", report["actions"][0]["steps"][0]["status"])
        self.assertEqual("ok", report["actions"][0]["steps"][1]["status"])
        self.assertTrue(all(step["status"] == "skipped" for step in report["actions"][0]["steps"][2:]))

    def test_apply_build_parser_exposes_file_metadata_policy(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "apply-build",
                "--plan-in",
                "/tmp/plan.json",
                "--apply-out",
                "/tmp/apply.json",
                "--policy-file-metadata",
                "repair",
            ]
        )
        self.assertEqual("repair", args.policy_file_metadata)

    def test_apply_build_parser_exposes_type_mismatch_policy(self) -> None:
        parser = build_parser()
        default_args = parser.parse_args(["apply-build", "--plan-in", "/tmp/plan.json", "--apply-out", "/tmp/apply.json"])
        self.assertEqual("review", default_args.policy_type_mismatch)
        quarantine_args = parser.parse_args(["apply-build", "--plan-in", "/tmp/plan.json", "--apply-out", "/tmp/apply.json", "--policy-type-mismatch", "quarantine-both"])
        self.assertEqual("quarantine-both", quarantine_args.policy_type_mismatch)
        loser_args = parser.parse_args(["apply-build", "--plan-in", "/tmp/plan.json", "--apply-out", "/tmp/apply.json", "--policy-type-mismatch", "quarantine-loser"])
        self.assertEqual("quarantine-loser", loser_args.policy_type_mismatch)

    def test_apply_build_parser_exposes_directory_gfid_conflict_merge_policy(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "apply-build",
                "--plan-in",
                "/tmp/plan.json",
                "--apply-out",
                "/tmp/apply.json",
                "--policy-directory-gfid-conflict",
                "merge",
            ]
        )
        self.assertEqual("merge", args.policy_directory_gfid_conflict)

    def test_apply_build_parser_exposes_directory_gfid_conflict_quarantine_policy(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "apply-build",
                "--plan-in",
                "/tmp/plan.json",
                "--apply-out",
                "/tmp/apply.json",
                "--policy-directory-gfid-conflict",
                "quarantine",
            ]
        )
        self.assertEqual("quarantine", args.policy_directory_gfid_conflict)

    def test_apply_run_parser_exposes_manage_heal(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "apply-run",
                "--apply-in",
                "/tmp/apply.json",
                "--manage-heal",
            ]
        )
        self.assertTrue(args.manage_heal); self.assertEqual(5, args.heal_repeat_limit); self.assertEqual(60.0, args.heal_refresh_timeout)

    def test_apply_run_final_check_reports_unique_heal_entries(self) -> None:
        from gluster_heal_tool.cli import _unique_heal_entries

        heal_text = """Brick host-a:/srv/gluster/brick-store/gtest
/example/a
/example/a
/example/b
Status: Connected
Number of entries: 3
"""
        with patch("gluster_heal_tool.heal_guard.get_heal_info_text", return_value=heal_text):
            report = _unique_heal_entries("gtest")
        self.assertTrue(report["available"])
        self.assertEqual(3, report["entry_count"])
        self.assertEqual(2, report["unique_count"])
        self.assertEqual(["/example/a", "/example/b"], report["sample_paths"])
        self.assertTrue(report["unique_signature"])

    def test_apply_run_post_execute_heal_retries_after_temporarily_disabled_daemon(self) -> None:
        from gluster_heal_tool.cli import _trigger_post_execute_heal

        calls: list[str] = []

        def fake_run_heal(volume: str, *, settle_seconds: float = 0.0) -> None:
            calls.append(volume)
            if len(calls) == 1:
                raise RuntimeError("Self-heal-daemon is disabled. Heal will not be triggered on volume gtest")

        with patch("gluster_heal_tool.cli.run_heal", side_effect=fake_run_heal), patch(
            "gluster_heal_tool.cli.time.sleep"
        ) as sleep:
            result = _trigger_post_execute_heal("gtest", retries=2, pause_seconds=0.25)

        self.assertTrue(result["triggered"])
        self.assertEqual("", result["error"])
        self.assertEqual(["gtest", "gtest"], calls)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_has_calls([unittest.mock.call(0.25), unittest.mock.call(0.25)])

    def test_apply_run_post_execute_heal_refresh_retries_until_clean(self) -> None:
        from gluster_heal_tool.cli import _refresh_post_execute_heal

        dirty_snapshot = {
            'available': True,
            'unique_count': 2,
            'unique_signature': 'sig-a',
            'sample_paths': ['/example/a', '/example/b'],
            'error': '',
        }
        clean_snapshot = {
            'available': True,
            'unique_count': 0,
            'unique_signature': '',
            'sample_paths': [],
            'error': '',
        }
        with patch('gluster_heal_tool.cli._trigger_post_execute_heal', return_value={'triggered': True, 'error': ''}) as trigger_mock, patch('gluster_heal_tool.cli._unique_heal_entries', side_effect=[dirty_snapshot, clean_snapshot]):
            final_check, final_check_guard = _refresh_post_execute_heal('gtest', {}, repeat_limit=2)

        self.assertEqual(2, trigger_mock.call_count)
        self.assertEqual(0, final_check['unique_count'])
        self.assertEqual(0, final_check_guard['repeat_count'])
        self.assertFalse(final_check_guard['hit_limit'])

    def test_apply_run_post_execute_heal_refresh_hits_repeat_limit(self) -> None:
        from gluster_heal_tool.cli import _refresh_post_execute_heal

        dirty_snapshot = {
            'available': True,
            'unique_count': 1,
            'unique_signature': 'sig-a',
            'sample_paths': ['/example/a'],
            'error': '',
        }
        with patch('gluster_heal_tool.cli._trigger_post_execute_heal', return_value={'triggered': True, 'error': ''}) as trigger_mock, patch('gluster_heal_tool.cli._unique_heal_entries', side_effect=[dirty_snapshot, dirty_snapshot]):
            final_check, final_check_guard = _refresh_post_execute_heal('gtest', {}, repeat_limit=2)

        self.assertEqual(2, trigger_mock.call_count)
        self.assertEqual(1, final_check['unique_count'])
        self.assertEqual(2, final_check_guard['repeat_count'])
        self.assertTrue(final_check_guard['hit_limit'])

    def test_run_heal_invokes_gluster_heal_before_scan(self) -> None:
        from gluster_heal_tool.volume import run_heal

        with patch("gluster_heal_tool.volume.subprocess.run") as run_command, patch(
            "gluster_heal_tool.volume.time.sleep"
        ) as sleep:
            run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            run_heal("gtest", settle_seconds=0.1)
        self.assertEqual(["gluster", "volume", "heal", "gtest"], run_command.call_args.args[0])
        sleep.assert_called_once_with(0.1)

    def test_manager_apply_build_parser_exposes_split_brain_auto(self) -> None:
        import importlib.util
        from pathlib import Path

        script = REPO_ROOT / "gluster-manager.py"
        spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        args = parser.parse_args(
            [
                "apply-build",
                "--plan-in",
                "/tmp/plan.json",
                "--apply-out",
                "/tmp/apply.json",
                "--policy-entry-split-brain-file",
                "auto",
                "--policy-type-mismatch",
                "quarantine-both",
                "--policy-entry-split-brain-native",
                "auto",
            ]
        )
        self.assertEqual("auto", args.policy_entry_split_brain_file)
        self.assertEqual("quarantine-both", args.policy_type_mismatch)
        self.assertEqual("auto", args.policy_entry_split_brain_native)

    def test_manager_apply_build_parser_exposes_split_brain_quarantine(self) -> None:
        import importlib.util
        from pathlib import Path

        script = REPO_ROOT / "gluster-manager.py"
        spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        args = parser.parse_args(
            [
                "apply-build",
                "--plan-in",
                "/tmp/plan.json",
                "--apply-out",
                "/tmp/apply.json",
                "--policy-entry-split-brain-file",
                "quarantine",
                "--policy-type-mismatch",
                "quarantine-both",
                "--policy-entry-split-brain-native",
                "auto",
            ]
        )
        self.assertEqual("quarantine", args.policy_entry_split_brain_file)
        self.assertEqual("quarantine-both", args.policy_type_mismatch)
        self.assertEqual("auto", args.policy_entry_split_brain_native)

    def test_execute_apply_results_emits_progress_lines(self) -> None:
        result = ApplyActionResult(
            action_id="repair:example/file",
            logical_path="example/file",
            action_type="repair_file",
            execution_mode="execute",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="planned",
            steps=[
                ApplyStep(
                    step_id="repair:example/file:00:stage",
                    step_type="stage_winner_local",
                    host="host-a",
                    source_path="/srv/gluster/brick-store/testvol/example/file",
                    target_path="/tmp/gluster-stage/example/file",
                    command_preview=["true"],
                )
            ],
            notes=["repair strategy: restore_missing_replica"],
        )
        progress = io.StringIO()

        with patch("gluster_heal_tool.executor._run_command") as run_command:
            run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            report = execute_apply_results([result], keep_going=True, progress_stream=progress)

        self.assertEqual("completed", report["actions"][0]["status"])
        progress_text = progress.getvalue()
        self.assertIn("[1/1] start repair_file example/file", progress_text)
        self.assertIn("[1/1] done completed example/file", progress_text)

    def test_execute_apply_results_reports_structured_progress(self) -> None:
        result = ApplyActionResult(
            action_id="repair:example/progress",
            logical_path="example/progress",
            action_type="repair_file",
            execution_mode="execute",
            backup_root="",
            backup_mode="required",
            batch=True,
            status="planned",
            steps=[
                ApplyStep(
                    step_id="repair:example/progress:00:stage",
                    step_type="stage_winner_local",
                    host="host-a",
                    source_path="/srv/gluster/brick-store/testvol/example/progress",
                    target_path="/tmp/gluster-stage/example/progress",
                    command_preview=["true"],
                )
            ],
            notes=["repair strategy: restore_missing_replica"],
        )
        events: list[dict[str, object]] = []

        with patch("gluster_heal_tool.executor._run_command") as run_command:
            run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            report = execute_apply_results(
                [result],
                keep_going=True,
                progress_callback=events.append,
            )

        self.assertEqual("completed", report["actions"][0]["status"])
        self.assertTrue({"execution", "wave", "action", "step"}.issubset({str(event["phase"]) for event in events}))
        for event in events:
            self.assertIn("last_activity", event)
            self.assertIn("last_activity_at", event)
            self.assertIn("elapsed_seconds", event)
            self.assertIn("interrupt_hint", event)
            self.assertGreaterEqual(float(event["elapsed_seconds"]), 0.0)

    def test_execution_progress_callback_persists_status_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            status_path = root / "status.json"
            status_path.write_text("{}", encoding="utf-8")
            output = io.StringIO()
            callback = _execution_progress_callback(str(status_path), root / "execution")
            with patch("gluster_heal_tool.cli.sys.stderr", output):
                callback(
                    {
                        "phase": "step",
                        "message": "running stage_winner_local",
                        "last_activity": "running stage_winner_local",
                        "elapsed_seconds": 1.25,
                        "interrupt_hint": "Ctrl-C stops safely",
                    }
                )
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual("apply-run-execution", status["phase"])
            self.assertEqual("step", status["execution_progress"]["phase"])
            self.assertIn("--status", status["execution_resume"])
            self.assertIn("phase=step", output.getvalue())
            self.assertIn("elapsed=1.2s", output.getvalue())
            self.assertIn("interrupt=Ctrl-C", output.getvalue())

    def test_execute_apply_results_writes_run_artifacts(self) -> None:
        results = [
            ApplyActionResult(
                action_id="repair:example/file-a",
                logical_path="example/file-a",
                action_type="cleanup_orphaned_symlink",
                execution_mode="execute",
                backup_root="",
                backup_mode="required",
                batch=True,
                status="planned",
                steps=[
                    ApplyStep(
                        step_id="repair:example/file-a:00:stage",
                        step_type="stage_winner_local",
                        host="host-a",
                        source_path="/srv/gluster/brick-store/testvol/example/file-a",
                        target_path="/tmp/gluster-stage/example/file-a",
                        command_preview=["true"],
                    )
                ],
                notes=["repair strategy: delete_orphaned_symlink"],
            ),
            ApplyActionResult(
                action_id="repair:example/file-b",
                logical_path="example/file-b",
                action_type="cleanup_orphaned_symlink",
                execution_mode="execute",
                backup_root="",
                backup_mode="required",
                batch=True,
                status="planned",
                steps=[
                    ApplyStep(
                        step_id="repair:example/file-b:00:stage",
                        step_type="stage_winner_local",
                        host="host-a",
                        source_path="/srv/gluster/brick-store/testvol/example/file-b",
                        target_path="/tmp/gluster-stage/example/file-b",
                        command_preview=["true"],
                    )
                ],
                notes=["repair strategy: delete_orphaned_symlink"],
            ),
        ]

        progress = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "repair-run"
            with patch("gluster_heal_tool.executor._run_command") as run_command:
                run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                report = execute_apply_results(
                    results,
                    keep_going=True,
                    progress_stream=progress,
                    parallel_actions=2,
                    parallel_nice=0,
                    run_dir=run_dir,
                )

            self.assertEqual(str(run_dir), report["run_dir"])
            self.assertEqual(2, report["summary"]["actions_total"])
            self.assertEqual(2, report["summary"]["completed_actions"])
            self.assertEqual(2, report["summary"]["parallel_actions_requested"])
            self.assertTrue((run_dir / "execute.json").is_file())
            self.assertTrue((run_dir / "status.json").is_file())
            self.assertTrue((run_dir / "repair.log").is_file())
            self.assertTrue((run_dir / "actions" / "0001-repair-example-file-a.log").is_file())
            self.assertTrue((run_dir / "actions" / "0001-repair-example-file-a.json").is_file())
            self.assertTrue((run_dir / "actions" / "0002-repair-example-file-b.log").is_file())
            self.assertTrue((run_dir / "actions" / "0002-repair-example-file-b.json").is_file())
            execute_report = json.loads((run_dir / "execute.json").read_text(encoding="utf-8"))
            status_report = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            repair_log = (run_dir / "repair.log").read_text(encoding="utf-8")
            self.assertEqual(str(run_dir), execute_report["run_dir"])
            self.assertEqual("execute-complete", status_report["phase"])
            self.assertEqual(str(run_dir), status_report["run_dir"])
            self.assertIn("[1/2] start cleanup_orphaned_symlink example/file-a", repair_log)
            self.assertIn("[2/2] done completed example/file-b", repair_log)

    def test_set_heal_settings_exact_round_trips_state(self) -> None:
        state = {option: "on" for option in HEAL_OPTIONS}

        def fake_run(cmd, capture_output=True, text=True, check=False):  # type: ignore[no-untyped-def]
            if cmd[:3] == ["gluster", "volume", "info"]:
                stdout = (
                    "Volume Name: gtest\n"
                    "Type: Replicate\n"
                    "Number of Bricks: 4\n"
                    "Brick1: host-a:/srv/gluster/brick-store/gtest\n"
                    "Brick2: host-b:/srv/gluster/brick-store/gtest\n"
                    "Brick3: host-c:/srv/gluster/brick-store/gtest\n"
                    "Brick4: host-d:/srv/gluster/brick-store/gtest\n"
                )
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")
            if cmd[:3] == ["gluster", "volume", "set"]:
                state[cmd[4]] = cmd[5]
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["gluster", "volume", "get"]:
                option = cmd[4]
                stdout = "Option Value\n" + "\n".join(f"{name} {state[name]}" for name in HEAL_OPTIONS) + "\n"
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("gluster_heal_tool.volume.subprocess.run", side_effect=fake_run):
            off_settings = {option: "off" for option in HEAL_OPTIONS}
            restored = set_heal_settings_exact("gtest", off_settings)
            self.assertTrue(all(value == "off" for value in restored.values()))
            mixed_settings = {
                HEAL_OPTIONS[0]: "on",
                HEAL_OPTIONS[1]: "off",
                HEAL_OPTIONS[2]: "on",
                HEAL_OPTIONS[3]: "off",
            }
            restored = set_heal_settings_exact("gtest", mixed_settings)
            self.assertEqual(mixed_settings, restored)

    def test_set_heal_settings_exact_rejects_unverified_restore(self) -> None:
        current = {option: "on" for option in HEAL_OPTIONS}
        expected = {option: "off" for option in HEAL_OPTIONS}

        with (
            patch("gluster_heal_tool.volume.set_volume_option"),
            patch("gluster_heal_tool.volume.get_heal_settings", return_value=current),
        ):
            with self.assertRaisesRegex(
                HealSettingsRestoreError,
                "cluster.self-heal-daemon: expected 'off', got 'on'",
            ):
                set_heal_settings_exact("gtest", expected)

    def test_directory_execute_is_not_gtest_gated(self) -> None:
        canonical_gfid = "0f0f0f0f-1111-2222-3333-444444444444"
        logical_path = "repair-canary-136/alpha/beta"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=3,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["synthetic directory execute case"],
            )
        }

        gtest_plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(gtest_plan))
        gtest_results = build_apply_results(
            {"actions": [gtest_plan[0].to_dict()]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
        )
        gtest_valid, gtest_errors = validate_execute_results(gtest_results)
        self.assertTrue(gtest_valid, gtest_errors)

        gtest_plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(gtest_plan))
        gtest_results = build_apply_results(
            {"actions": [gtest_plan[0].to_dict()]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
        )
        gtest_valid, gtest_errors = validate_execute_results(gtest_results)
        self.assertTrue(gtest_valid, gtest_errors)

    def test_directory_backend_child_gap_is_executable(self) -> None:
        action = {
            "action_id": "canary:repair-canary-dir-backend-child-gap",
            "logical_path": "/gtest/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "action_type": "repair_directory_metadata",
            "object_type": "directory",
            "depth": 4,
            "repair_strategy": "recreate_missing_directory_backend_child_gap",
            "mounted_target": "/gtest/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "directory_canonical_host": "node-a",
            "directory_canonical_backend": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "directory_canonical_gfid": "b6361650-e942-4d18-8c94-ed4d3ac0e6dc",
            "missing_hosts": ["node-b"],
            "directory_target_backend_by_host": {
                "node-b": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            },
            "notes": ["synthetic directory backend child-gap execute case"],
        }

        results = build_apply_results(
            {"actions": [action]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        valid, errors = validate_execute_results(results)
        self.assertTrue(valid, errors)
        result = results[0]
        self.assertEqual("repair_directory_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("mkdir_directory_backend_child_gap", step_types)
        self.assertIn("attach_directory_gfid", step_types)
        self.assertIn("verify_directory_backend", step_types)
        self.assertEqual(1, len(result.revert_steps))

    def test_directory_backend_child_gap_execute_completes_without_skips(self) -> None:
        action = {
            "action_id": "canary:repair-canary-dir-backend-child-gap",
            "logical_path": "/gtest/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "action_type": "repair_directory_metadata",
            "object_type": "directory",
            "depth": 4,
            "repair_strategy": "recreate_missing_directory_backend_child_gap",
            "mounted_target": "/gtest/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "directory_canonical_host": "node-a",
            "directory_canonical_backend": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "directory_canonical_gfid": "b6361650-e942-4d18-8c94-ed4d3ac0e6dc",
            "missing_hosts": ["node-b"],
            "directory_target_backend_by_host": {
                "node-b": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            },
            "notes": ["synthetic directory backend child-gap execute case"],
        }

        results = build_apply_results(
            {"actions": [action]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
        )
        with patch("gluster_heal_tool.executor._run_command") as run_command:
            run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            report = execute_apply_results(results, keep_going=True)

        self.assertEqual(1, report["summary"]["completed_actions"])
        self.assertEqual(0, report["summary"]["completed_with_skips"])
        self.assertEqual(0, report["summary"]["review_only_skipped_steps"])
        rendered = render_apply_summary(report["summary"])
        self.assertNotIn("review-only tail skipped", rendered)
        self.assertIn("repair completed", rendered)

    def test_dead_gfid_becomes_cleanup_action(self) -> None:
        dead_gfid = "eeee1111-ffff-0000-1111-222233334444"
        backend = "/srv/gluster/brick-store/gtest/.glusterfs/de/ad/eeee1111-ffff-0000-1111-222233334444"
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="dead_gfid",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[dead_gfid],
                dead_gfids=[dead_gfid],
                observations={
                    "host-a": [_dead_gfid_cleanup_obs("host-a", backend, dead_gfid)],
                    "host-b": [_dead_gfid_cleanup_obs("host-b", backend, dead_gfid)],
                },
                notes=["synthetic dead gfid case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("cleanup_dead_gfid", action.action_type)
        self.assertEqual("delete_dead_gfid_residue", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_dead_gfid", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("remove_stale_gfid", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: delete_dead_gfid_residue", rendered)

    def test_dead_gfid_without_observed_residue_is_not_actionable(self) -> None:
        dead_gfid = "eeee1111-ffff-0000-1111-222233334444"
        backend = "/srv/gluster/brick-store/gtest/.glusterfs/de/ad/eeee1111-ffff-0000-1111-222233334444"
        observations = {}
        for host in ("host-a", "host-b", "host-c"):
            observation = _dead_gfid_cleanup_obs(host, backend, dead_gfid)
            observation.gfid_exists = False
            observation.gfid_path_lexists = False
            observation.gfid_path_exists = False
            observations[host] = [observation]
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="dead_gfid",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>"],
                source_hosts=["host-a", "host-b", "host-c"],
                gfids=[dead_gfid],
                dead_gfids=[dead_gfid],
                observations=observations,
            )
        }

        self.assertEqual([], build_plan(manifest, mountpoint="/gtest"))

    def test_stale_glusterfs_index_becomes_delete_by_default_cleanup(self) -> None:
        logical_path = ".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        child_names = ["child-1", "child-2"]
        raw_entry = f"/{logical_path}"
        manifest, _ = build_manifest(
            [HealEntry(raw=raw_entry, source_host="host-a", brick="brick", index_on_host=0)],
            _StaticResolver(
                {
                    raw_entry: [
                        _stale_index_obs(
                            "host-a",
                            backend,
                            logical_path=logical_path,
                            child_names=child_names,
                        )
                    ]
                }
            ),
        )

        self.assertEqual("stale_glusterfs_index", manifest[logical_path].object_type)

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("cleanup_stale_glusterfs_index", action.action_type)
        self.assertEqual("delete_stale_glusterfs_index_residue", action.repair_strategy)
        self.assertIn("internal Gluster heal bookkeeping entry", " ".join(action.notes))
        self.assertIn("recurse one level", " ".join(action.notes))
        self.assertTrue(action.stale_backends_by_host)
        self.assertEqual(
            [f"{backend}/{name}" for name in child_names],
            action.stale_backends_by_host["host-a"],
        )

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        valid, errors = validate_execute_results(results)
        self.assertTrue(valid, errors)
        result = results[0]
        self.assertEqual("cleanup_stale_glusterfs_index", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertEqual([], result.backup_artifacts)
        self.assertEqual(["remove_stale_backend", "remove_stale_backend"], [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: delete_stale_glusterfs_index_residue", rendered)
        self.assertIn("follow-up: refresh-heal-snapshot", render_apply_summary(summarize_apply_results(results)))

    def test_absent_operator_index_entry_stays_refresh_review(self) -> None:
        logical_path = ".glusterfs/indices/xattrop/aaaa1111-bbbb-2222-cccc-333344445555"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        raw_entry = f"/{logical_path}"
        manifest, _ = build_manifest(
            [HealEntry(raw=raw_entry, source_host="host-a", brick="brick", index_on_host=0)],
            _StaticResolver(
                {
                    raw_entry: [
                        _stale_index_obs(
                            "host-a",
                            backend,
                            logical_path=logical_path,
                            present=False,
                        )
                    ]
                }
            ),
        )

        plan = build_plan(manifest, mountpoint="/gtest")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("refresh_stale_index_evidence", action.repair_strategy)
        self.assertEqual("refresh_evidence", action.recommended_choice)
        self.assertEqual([], action.stale_backends)
        self.assertIn("no exact internal index entry was observed", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review", result.status)
        self.assertEqual("refresh_evidence", result.recommended_choice)
        self.assertIn("refresh read-only index evidence", " ".join(result.notes))
        self.assertIn("review_stale_glusterfs_index_cleanup", [step.step_type for step in result.steps])

    def test_dead_gfid_live_reference_promotes_to_live_reference_review(self) -> None:
        dead_gfid = "dddd1111-eeee-2222-3333-444455556666"
        live_ref = "/srv/example-users/user-b/.config/gtk-3.0/bookmarks"
        backend = "/srv/gluster/brick-store/gtest/bookmarks"
        manifest = {
            live_ref: ManifestObject(
                logical_path=live_ref,
                object_type="file",
                depth=1,
                raw_entries=[live_ref],
                source_hosts=["host-a"],
                gfids=[],
                observations={},
                notes=["synthetic live reference target"],
            ),
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="dead_gfid",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[dead_gfid],
                dead_gfids=[dead_gfid],
                observations={
                    "host-a": [
                        _dead_gfid_reference_obs(
                            "host-a",
                            backend,
                            dead_gfid,
                            live_ref=live_ref,
                        )
                    ],
                    "host-b": [
                        _dead_gfid_reference_obs(
                            "host-b",
                            backend,
                            dead_gfid,
                            live_ref=live_ref,
                        )
                    ],
                },
                notes=["synthetic dead gfid live-reference case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = next(item for item in plan if item.action_type == "review_dead_gfid_reference")
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertEqual([live_ref], action.dead_gfid_live_references)
        self.assertIn(live_ref, " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_dead_gfid_reference", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_dead_gfid_reference", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: follow_live_reference", rendered)
        self.assertIn(live_ref, rendered)

    def test_dead_gfid_reference_without_manifest_target_stays_review(self) -> None:
        dead_gfid = "cccc1111-dddd-2222-3333-444455556666"
        live_ref = "/srv/example-users/user-a/.config/dconf/user"
        backend = "/srv/gluster/brick-store/gtest/dconf-user"
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="dead_gfid",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[dead_gfid],
                dead_gfids=[dead_gfid],
                observations={
                    "host-a": [
                        _dead_gfid_reference_obs(
                            "host-a",
                            backend,
                            dead_gfid,
                            live_ref=live_ref,
                        )
                    ],
                    "host-b": [
                        _dead_gfid_reference_obs(
                            "host-b",
                            backend,
                            dead_gfid,
                            live_ref=live_ref,
                        )
                    ],
                },
                notes=["synthetic dead gfid unresolved-reference case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertIn("inspect the referenced live path", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_dead_gfid_reference", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("follow_live_reference", " ".join(result.notes))
        self.assertIn("review_dead_gfid_reference", [step.step_type for step in result.steps])

    def test_unknown_gfid_reference_without_manifest_target_stays_review(self) -> None:
        dead_gfid = "bbbb1111-aaaa-2222-3333-444455556666"
        live_ref = "/srv/example-users/user-c/.config/gtk-3.0/bookmarks"
        backend = "/srv/gluster/brick-store/gtest/bookmarks"
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="unknown",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>/alpha/payload.txt"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[dead_gfid],
                observations={
                    "host-a": [
                        _dead_gfid_reference_obs(
                            "host-a",
                            backend,
                            dead_gfid,
                            live_ref=live_ref,
                        )
                    ],
                    "host-b": [
                        _dead_gfid_reference_obs(
                            "host-b",
                            backend,
                            dead_gfid,
                            live_ref=live_ref,
                        )
                    ],
                },
                notes=["synthetic unknown gfid-reference residue"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertEqual([live_ref], action.dead_gfid_live_references)
        self.assertIn(live_ref, " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_dead_gfid_reference", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_dead_gfid_reference", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: follow_live_reference", rendered)

    def test_regular_file_gfid2path_handle_with_verified_missing_terminal_cleans_up(self) -> None:
        dead_gfid = "eeee1111-aaaa-2222-3333-444455556677"
        handle = "/srv/gluster/brick-store/gtest/brick/.glusterfs/2e/ee/2e599215-a3fc-4132-8771-898896db8200"
        target = "/repair-canary-missing-terminal/alpha/payload.txt"
        raw_entry = f"<gfid:{dead_gfid}>/alpha/payload.txt"
        observations = {}
        for host in ("host-a", "host-b"):
            observations[host] = [
                ResolutionObservation(
                    host=host,
                    raw_entry=raw_entry,
                    gfid=dead_gfid,
                    gfid_path=handle,
                    gfid_exists=True,
                    gfid_path_lexists=True,
                    gfid_path_exists=True,
                    gfid_path_lstat_type="file",
                    gfid_path_terminal_path=target,
                    gfid_path_terminal_exists=False,
                )
            ]
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="unknown",
                depth=0,
                raw_entries=[raw_entry],
                source_hosts=["host-a", "host-b"],
                gfids=[dead_gfid],
                observations=observations,
            )
        }

        action = build_plan(manifest, mountpoint="/gtest")[0]
        self.assertEqual("cleanup_dead_gfid", action.action_type)
        self.assertEqual("delete_dead_gfid_residue", action.repair_strategy)
        self.assertIn(handle, action.stale_gfid_paths)
        self.assertNotIn(target, action.dead_gfid_live_references)

    def test_regular_file_gfid2path_handle_stays_review(self) -> None:
        dead_gfid = "cccc1111-aaaa-2222-3333-444455556677"
        handle_backend = "/srv/gluster/brick-store/gtest/brick/.glusterfs/90/00/90000000-0000-4000-8000-000000000002"
        handle_target = "/repair-canary-cleanup-proof/alpha/payload.txt"
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="unknown",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>/alpha/payload.txt"],
                source_hosts=["host-a", "host-b"],
                gfids=[dead_gfid],
                observations={
                    "host-a": [
                        ResolutionObservation(
                            host="host-a",
                            raw_entry=f"<gfid:{dead_gfid}>/alpha/payload.txt",
                            gfid=dead_gfid,
                            child_rel="alpha/payload.txt",
                            gfid_path=handle_backend,
                            backend=handle_backend,
                            type="unknown",
                            gfid_exists=True,
                            relpath="",
                            depth=0,
                            mounted_checked=False,
                            mounted_lexists=False,
                            mounted_exists=False,
                            backend_lexists=True,
                            backend_exists=True,
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_terminal_path=handle_target,
                            backend_terminal_lstat_type="",
                            backend_terminal_is_symlink=False,
                            backend_terminal_readlink=handle_target,
                            backend_terminal_trusted_gfid="",
                            backend_trusted_gfid=dead_gfid,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="file",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-b": [
                        ResolutionObservation(
                            host="host-b",
                            raw_entry=f"<gfid:{dead_gfid}>/alpha/payload.txt",
                            gfid=dead_gfid,
                            child_rel="alpha/payload.txt",
                            gfid_path=handle_backend,
                            backend=handle_backend,
                            type="unknown",
                            gfid_exists=True,
                            relpath="",
                            depth=0,
                            mounted_checked=False,
                            mounted_lexists=False,
                            mounted_exists=False,
                            backend_lexists=True,
                            backend_exists=True,
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_terminal_path=handle_target,
                            backend_terminal_lstat_type="",
                            backend_terminal_is_symlink=False,
                            backend_terminal_readlink=handle_target,
                            backend_terminal_trusted_gfid="",
                            backend_trusted_gfid=dead_gfid,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="file",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["synthetic regular-file gfid2path handle residue"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertIn(handle_backend, " ".join(action.stale_gfid_paths))
        self.assertIn("inspect the referenced live path", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_dead_gfid_reference", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_dead_gfid_reference", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: follow_live_reference", rendered)

    def test_regular_file_gfidpath_handle_stays_review(self) -> None:
        dead_gfid = "dddd1111-aaaa-2222-3333-444455556677"
        handle_path = "/srv/gluster/brick-store/gtest/brick/.glusterfs/2d/59/2d599215-a3fc-4132-8771-898896db8200"
        handle_target = "/repair-canary-handle-ghost/alpha/ghost-payload.txt"
        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="unknown",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>/alpha/payload.txt"],
                source_hosts=["host-a", "host-b"],
                gfids=[dead_gfid],
                observations={
                    "host-a": [
                        ResolutionObservation(
                            host="host-a",
                            raw_entry=f"<gfid:{dead_gfid}>/alpha/payload.txt",
                            gfid=dead_gfid,
                            child_rel="alpha/payload.txt",
                            gfid_path=handle_path,
                            backend="",
                            type="unknown",
                            gfid_exists=True,
                            relpath="",
                            depth=0,
                            mounted_checked=False,
                            mounted_lexists=False,
                            mounted_exists=False,
                            backend_lexists=False,
                            backend_exists=False,
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_terminal_path="",
                            backend_terminal_lstat_type="",
                            backend_terminal_is_symlink=False,
                            backend_terminal_readlink="",
                            backend_terminal_trusted_gfid="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="file",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            gfid_path_terminal_path=handle_target,
                            gfid_path_terminal_lstat_type="",
                            gfid_path_terminal_is_symlink=False,
                            gfid_path_terminal_readlink=handle_target,
                            gfid_path_terminal_trusted_gfid="",
                            error="",
                        )
                    ],
                    "host-b": [
                        ResolutionObservation(
                            host="host-b",
                            raw_entry=f"<gfid:{dead_gfid}>/alpha/payload.txt",
                            gfid=dead_gfid,
                            child_rel="alpha/payload.txt",
                            gfid_path=handle_path,
                            backend="",
                            type="unknown",
                            gfid_exists=True,
                            relpath="",
                            depth=0,
                            mounted_checked=False,
                            mounted_lexists=False,
                            mounted_exists=False,
                            backend_lexists=False,
                            backend_exists=False,
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_terminal_path="",
                            backend_terminal_lstat_type="",
                            backend_terminal_is_symlink=False,
                            backend_terminal_readlink="",
                            backend_terminal_trusted_gfid="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="file",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            gfid_path_terminal_path=handle_target,
                            gfid_path_terminal_lstat_type="",
                            gfid_path_terminal_is_symlink=False,
                            gfid_path_terminal_readlink=handle_target,
                            gfid_path_terminal_trusted_gfid="",
                            error="",
                        )
                    ],
                },
                notes=["synthetic regular-file gfid2path handle ghost"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertIn(handle_path, " ".join(action.stale_gfid_paths))
        self.assertIn("inspect the referenced live path", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_dead_gfid_reference", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_dead_gfid_reference", [step.step_type for step in result.steps])
        self.assertIn("matrix suggestion: follow_live_reference", render_apply_run(results))

    def test_immediate_gfid_child_all_missing_never_cleans_parent_handle(self) -> None:
        parent_gfid = "90000000-0000-4000-8000-000000000004"
        child_name = "gamma"
        raw_entry = f"<gfid:{parent_gfid}>/{child_name}"
        parent_handle = f"/.glusterfs/{parent_gfid[:2]}/{parent_gfid[2:4]}/{parent_gfid}"
        logical_path = f"unresolved-child:{parent_gfid}/{child_name}"
        observations = {}
        for host in ("host-a", "host-b", "host-c"):
            obs = _dead_gfid_cleanup_obs(host, "", parent_gfid)
            obs.raw_entry = raw_entry
            obs.gfid_path = parent_handle
            obs.gfid_path_terminal_path = "repair-canary-child-ref/alpha"
            obs.gfid_path_terminal_lstat_type = "dir"
            observations[host] = [obs]
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="unknown",
                depth=2,
                raw_entries=[raw_entry],
                source_hosts=list(observations),
                gfids=[parent_gfid],
                dead_gfids=[parent_gfid],
                child_names=[child_name],
                observations=observations,
                notes=["synthetic immediate all-missing GFID child"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_directory_children", action.action_type)
        self.assertEqual("verify_directory_child_absence", action.repair_strategy)
        self.assertEqual("directory_child_reference_immediate_missing", action.graph_node)
        self.assertEqual([], action.dead_gfids)
        self.assertEqual([], action.stale_gfid_paths)
        self.assertEqual({}, action.stale_gfid_paths_by_host)
        self.assertNotIn(parent_handle, " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "auto"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review", result.status)
        self.assertEqual("recover_missing_child_or_confirm_delete", result.recommended_choice)
        rendered = render_apply_run(results)
        self.assertIn("recover_missing_child_or_confirm_delete", rendered)
        self.assertIn("do not delete the parent GFID handle", rendered)
        self.assertNotIn("remove_stale_gfid", [step.step_type for step in result.steps])

    def test_nested_gfid_child_residue_without_live_chain_stays_review(self) -> None:
        dead_gfid = "90000000-0000-4000-8000-000000000004"
        raw_entry = f"<gfid:{dead_gfid}>/alpha/payload.txt"
        backend = "/srv/gluster/brick-store/gtest/brick/.glusterfs/90/00/90000000-0000-4000-8000-000000000002"
        logical_path = f"unresolved-child:{dead_gfid}/alpha/payload.txt"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="unknown",
                depth=3,
                raw_entries=[raw_entry],
                source_hosts=["host-a", "host-b"],
                gfids=[dead_gfid],
                observations={
                    "host-a": [_dead_gfid_cleanup_obs("host-a", backend, dead_gfid)],
                    "host-b": [_dead_gfid_cleanup_obs("host-b", backend, dead_gfid)],
                },
                notes=["synthetic nested gfid-child residue"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("cleanup_dead_gfid", action.action_type)
        self.assertEqual("delete_dead_gfid_residue", action.repair_strategy)
        self.assertIn("post-resolution ghost tail", " ".join(action.notes))
        self.assertIn(dead_gfid, " ".join(action.stale_gfid_paths))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_dead_gfid", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("remove_stale_gfid", [step.step_type for step in result.steps])
        self.assertIn("post-resolution ghost tail", render_apply_run(results))

    def test_nested_gfid_child_residue_with_live_chain_stays_review(self) -> None:
        dead_gfid = "90000000-0000-4000-8000-000000000004"
        raw_entry = f"<gfid:{dead_gfid}>/alpha/payload.txt"
        backend = "/srv/gluster/brick-store/gtest/brick/.glusterfs/90/00/90000000-0000-4000-8000-000000000002"
        live_target = "repair-canary-child-ref/alpha"
        logical_path = f"unresolved-child:{dead_gfid}/alpha/payload.txt"
        manifest = {
            live_target: ManifestObject(
                logical_path=live_target,
                object_type="directory",
                depth=2,
                raw_entries=["/repair-canary-child-ref/alpha"],
                source_hosts=["host-a"],
                aliases=["/repair-canary-child-ref/alpha"],
                observations={},
                notes=["synthetic live directory target"],
            ),
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="unknown",
                depth=3,
                raw_entries=[raw_entry],
                source_hosts=["host-a", "host-b"],
                gfids=[dead_gfid],
                observations={
                    "host-a": [
                        (obs_a := _dead_gfid_cleanup_obs("host-a", backend, dead_gfid))
                    ],
                    "host-b": [
                        (obs_b := _dead_gfid_cleanup_obs("host-b", backend, dead_gfid))
                    ],
                },
                notes=["synthetic nested gfid-child residue"],
            ),
        }
        obs_a.gfid_path_terminal_path = live_target
        obs_a.gfid_path_terminal_lstat_type = "dir"
        obs_b.gfid_path_terminal_path = live_target
        obs_b.gfid_path_terminal_lstat_type = "dir"

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(2, len(plan))
        action = next(item for item in plan if item.logical_path == logical_path)
        self.assertEqual("review_directory_children", action.action_type)
        self.assertEqual("reconcile_directory_children", action.repair_strategy)
        self.assertIn("live directory chain target", " ".join(action.notes))
        self.assertIn(live_target, " ".join(action.notes))

        auto_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_children": "auto"},
        )
        self.assertEqual(1, len(auto_results))
        auto_result = auto_results[0]
        self.assertEqual("review_directory_children", auto_result.action_type)
        self.assertEqual("review", auto_result.status)
        auto_step_types = [step.step_type for step in auto_result.steps]
        self.assertEqual(["review_directory_children"], auto_step_types)
        self.assertNotIn("remove_stale_dir_gfid", auto_step_types)
        self.assertIn("collect immediate --gfid-child", " ".join(auto_result.notes))

    def test_tied_file_split_brain_uses_decision_file(self) -> None:
        logical_path = "repair-canary-132/tied-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_file_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid_b,
                            gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            backend=backend,
                            type="file",
                            gfid_exists=True,
                            file_gfid=gfid_b,
                            file_gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="split-brain-like mount access error",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=17,
                            backend_mode="-rw-r--r--",
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid_b,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_file"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_entry_split_brain_file", action.repair_strategy)
        self.assertGreaterEqual(len(action.file_cohorts), 2)

        no_decision = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(no_decision))
        review = no_decision[0]
        self.assertEqual("review_entry_split_brain", review.action_type)
        self.assertEqual("review", review.status)
        self.assertIn("review_ambiguous_entry_split_brain", [step.step_type for step in review.steps])
        rendered_review = render_apply_run(no_decision)
        self.assertIn("quarantine_both", rendered_review)
        self.assertIn("likely winner is", rendered_review)

        with_decision = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"keep_gfid": gfid_a}},
            volume="gtest",
            brick_path="/srv/gluster/brick-store/gtest",
        )
        self.assertEqual(1, len(with_decision))
        proposed = with_decision[0]
        self.assertEqual("review_entry_split_brain", proposed.action_type)
        self.assertEqual("proposed", proposed.status)
        self.assertEqual(gfid_a, proposed.decision.get("keep_gfid"))
        self.assertIn("replace_entry_split_brain_file", "\n".join(proposed.notes))
        step_types = [step.step_type for step in proposed.steps]
        self.assertEqual("resolve_split_brain_gluster_cli", step_types[0])
        self.assertIn("mkdir_stage_parent", step_types)
        self.assertIn("stage_winner_local", step_types)
        self.assertIn("remove_stale_backend", step_types)
        self.assertIn("remove_restored_mount_file", step_types)
        self.assertIn("restore_via_mount", step_types)
        self.assertEqual(
            [
                "sudo",
                "-n",
                "gluster",
                "volume",
                "heal",
                "gtest",
                "split-brain",
                "latest-mtime",
                f"/{logical_path}",
            ],
            proposed.steps[0].command_preview,
        )

        quarantine = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "quarantine"},
        )
        self.assertEqual(1, len(quarantine))
        quarantined = quarantine[0]
        self.assertEqual("review_entry_split_brain", quarantined.action_type)
        self.assertEqual("proposed", quarantined.status)
        self.assertIn("quarantine_conflicting_file", "\n".join(quarantined.notes))
        quarantine_step_types = [step.step_type for step in quarantined.steps]
        self.assertIn("quarantine_file_backend", quarantine_step_types)
        self.assertIn("quarantine_file_gfid", quarantine_step_types)
        self.assertNotIn("stage_winner_local", quarantine_step_types)
        self.assertNotIn("restore_file_backend_gap_fill", quarantine_step_types)
        self.assertTrue(validate_execute_results(quarantine)[0])
        rendered_quarantine = render_apply_run(quarantine)
        self.assertIn("original path: repair-canary-132/tied-file", rendered_quarantine)
        self.assertIn("quarantine mode: both", rendered_quarantine)
        self.assertIn("quarantine targets:", rendered_quarantine)
        self.assertIn("quarantine naming: original path + .gluster-quarantine", rendered_quarantine)
        self.assertNotIn("restore_file_backend_gap_fill", rendered_quarantine)

        loser_quarantine = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "quarantine"},
            decisions={logical_path: {"file_choice": "quarantine_loser"}},
        )
        self.assertEqual(1, len(loser_quarantine))
        loser = loser_quarantine[0]
        self.assertEqual("review_entry_split_brain", loser.action_type)
        self.assertEqual("proposed", loser.status)
        self.assertIn("decision file selected quarantine_loser", "\n".join(loser.notes))
        loser_step_types = [step.step_type for step in loser.steps]
        self.assertIn("quarantine_file_backend", loser_step_types)
        self.assertIn("quarantine_file_gfid", loser_step_types)
        self.assertIn("restore_file_backend_gap_fill", loser_step_types)
        self.assertIn("attach_file_gfid", loser_step_types)
        self.assertNotIn("stage_winner_local", loser_step_types)
        rendered_loser = render_apply_run(loser_quarantine)
        self.assertIn("original path: repair-canary-132/tied-file", rendered_loser)
        self.assertIn("quarantine mode: loser", rendered_loser)
        self.assertIn("quarantine naming: original path + .gluster-quarantine", rendered_loser)
        self.assertIn("restore_file_backend_gap_fill", rendered_loser)

    def test_split_brain_official_resolve_short_circuits_fallback(self) -> None:
        logical_path = "repair-canary-132/tied-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_file_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid_b,
                            gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            backend=backend,
                            type="file",
                            gfid_exists=True,
                            file_gfid=gfid_b,
                            file_gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="split-brain-like mount access error",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=17,
                            backend_mode="-rw-r--r--",
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid_b,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_file"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"keep_gfid": gfid_a}},
            volume="gtest",
            brick_path="/srv/gluster/brick-store/gtest",
        )

        official_command = [
            "sudo",
            "-n",
            "gluster",
            "volume",
            "heal",
            "gtest",
            "split-brain",
            "latest-mtime",
            f"/{logical_path}",
        ]

        def fake_run(cmd, capture_output=True, text=True, check=False):  # type: ignore[no-untyped-def]
            if cmd == official_command:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"GFID split-brain resolved for file /{logical_path}\n", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch(
            "gluster_heal_tool.executor._run_command", side_effect=fake_run
        ):
            report = execute_apply_results(results, keep_going=True)

        self.assertEqual("completed-with-nonblocking-skips", report["actions"][0]["status"])
        self.assertEqual("ok", results[0].steps[0].status)
        self.assertIn("native split-brain outcome: resolved by native Gluster heal", render_apply_run(results))
        self.assertTrue(all(step.status == "skipped" for step in results[0].steps[1:]))

    def test_split_brain_official_resolve_failure_stops_fallback(self) -> None:
        logical_path = "repair-canary-132/tied-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_file_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid_b,
                            gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            backend=backend,
                            type="file",
                            gfid_exists=True,
                            file_gfid=gfid_b,
                            file_gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="split-brain-like mount access error",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=17,
                            backend_mode="-rw-r--r--",
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid_b,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_file"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"keep_gfid": gfid_a}},
            volume="gtest",
            brick_path="/srv/gluster/brick-store/gtest",
        )

        official_command = [
            "sudo",
            "-n",
            "gluster",
            "volume",
            "heal",
            "gtest",
            "split-brain",
            "latest-mtime",
            f"/{logical_path}",
        ]
        source_brick_command_prefix = [
            "sudo",
            "-n",
            "gluster",
            "volume",
            "heal",
            "gtest",
            "split-brain",
            "source-brick",
        ]

        def fake_run(cmd, capture_output=True, text=True, check=False):  # type: ignore[no-untyped-def]
            if cmd == official_command:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="gluster split-brain resolve failed")
            if cmd[:8] == source_brick_command_prefix:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="gluster split-brain source-brick resolve failed")
            if cmd[:1] == ["mkdir"] or cmd[:1] == ["rsync"] or cmd[:1] == ["cp"] or cmd[:1] == ["rm"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["sudo", "-n", "cp"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["sudo", "-n", "mkdir"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["sudo", "-n", "rm"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:1] == ["ssh"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch(
            "gluster_heal_tool.executor._run_command", side_effect=fake_run
        ):
            report = execute_apply_results(results, keep_going=True)

        self.assertEqual("unknown", report["actions"][0]["status"])
        self.assertEqual("unknown", results[0].steps[0].status)
        self.assertIn("native split-brain outcome: unknown; fallback stopped", render_apply_run(results))
        self.assertTrue(all(step.status == "planned" for step in results[0].steps[1:]))

    def test_split_brain_official_resolve_tie_continues_fallback(self) -> None:
        logical_path = "repair-canary-132/tied-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_file_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid_b,
                            gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            backend=backend,
                            type="file",
                            gfid_exists=True,
                            file_gfid=gfid_b,
                            file_gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="split-brain-like mount access error",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=17,
                            backend_mode="-rw-r--r--",
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid_b,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_file"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"keep_gfid": gfid_a}},
            review_policies={"entry_split_brain_file": "replace"},
            volume="gtest",
            brick_path="/srv/gluster/brick-store/gtest",
        )

        official_command = [
            "sudo",
            "-n",
            "gluster",
            "volume",
            "heal",
            "gtest",
            "split-brain",
            "latest-mtime",
            f"/{logical_path}",
        ]
        source_brick_command_prefix = [
            "sudo",
            "-n",
            "gluster",
            "volume",
            "heal",
            "gtest",
            "split-brain",
            "source-brick",
        ]

        def fake_run(cmd, capture_output=True, text=True, check=False):  # type: ignore[no-untyped-def]
            if cmd == official_command:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="Lookup failed on /repair-canary-132/tied-file:Input/output error.\nNo difference in mtime for file /repair-canary-132/tied-file",
                    stderr="",
                )
            if cmd[:8] == source_brick_command_prefix:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f"GFID split-brain resolved for file /{logical_path}\n",
                    stderr="",
                )
            if cmd[:1] in (["mkdir"], ["rsync"], ["cp"], ["rm"]):
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["sudo", "-n", "cp"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["sudo", "-n", "mkdir"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:3] == ["sudo", "-n", "rm"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if cmd[:1] == ["ssh"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch(
            "gluster_heal_tool.executor._run_command", side_effect=fake_run
        ):
            with tempfile.TemporaryDirectory() as stage:
                for step in results[0].steps:
                    if step.step_type == "mkdir_stage_parent":
                        step.target_path = str(Path(stage) / "staging")
                report = execute_apply_results(results, keep_going=True)

        self.assertEqual("completed", report["actions"][0]["status"])
        self.assertEqual("ok", results[0].steps[0].status)
        self.assertIn("latest-mtime tie", "\n".join(results[0].steps[0].notes))
        self.assertEqual("ok", results[0].steps[1].status)
        self.assertIn(
            "official Gluster latest-mtime resolver was inconclusive; the planned follow-up remains active before manual fallback",
            "\n".join(results[0].steps[0].notes),
        )
        self.assertIn(
            "native split-brain outcome: latest-mtime tie; follow-up remains active before manual fallback",
            render_apply_run(results),
        )
        self.assertTrue(all(step.status == "ok" for step in results[0].steps))

    def test_split_brain_not_in_split_brain_counts_as_resolved(self) -> None:
        logical_path = "repair-canary-132/tied-file"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_file_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_file_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-c": [_file_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid_b,
                            gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            backend=backend,
                            type="file",
                            gfid_exists=True,
                            file_gfid=gfid_b,
                            file_gfid_path=f"/.glusterfs/{gfid_b[:2]}/{gfid_b[2:4]}/{gfid_b}",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="split-brain-like mount access error",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=17,
                            backend_mode="-rw-r--r--",
                            backend_lstat_type="file",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid_b,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_file"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="execute",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"keep_gfid": gfid_a}},
            volume="gtest",
            brick_path="/srv/gluster/brick-store/gtest",
        )

        official_command = [
            "sudo",
            "-n",
            "gluster",
            "volume",
            "heal",
            "gtest",
            "split-brain",
            "latest-mtime",
            f"/{logical_path}",
        ]

        def fake_run(cmd, capture_output=True, text=True, check=False):  # type: ignore[no-untyped-def]
            if cmd == official_command:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="Healing /repair-canary-132/tied-file failed: File not in split-brain.",
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("gluster_heal_tool.executor._run_command", side_effect=fake_run):
            report = execute_apply_results(results, keep_going=True)

        self.assertEqual("completed-with-nonblocking-skips", report["actions"][0]["status"])
        self.assertEqual("ok", results[0].steps[0].status)
        self.assertIn("not in split-brain", results[0].steps[0].message.lower())
        self.assertIn("native resolver reported not in split-brain; fallback skipped", render_apply_run(results))
        self.assertTrue(all(step.status == "skipped" for step in results[0].steps[1:]))

    def test_remove_restored_mount_file_stops_on_disconnected_mount(self) -> None:
        step = type(
            "Step",
            (),
            {
                "step_type": "remove_restored_mount_file",
                "target_path": "/gtest/example/path",
                "command_preview": ["rm", "-f", "--", "/gtest/example/path"],
                "tolerate_missing": True,
            },
        )()

        def fake_run(cmd, capture_output=True, text=True, check=False):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="rm: cannot remove '/gtest/example/path': Transport endpoint is not connected",
            )

        with patch("gluster_heal_tool.executor._run_command", side_effect=fake_run):
            from gluster_heal_tool.executor import _execute_step

            status, returncode, message = _execute_step(step)

        self.assertEqual("failed", status)
        self.assertEqual(1, returncode)
        self.assertIn("transport endpoint is not connected", message.lower())

    def test_fake_directory_canary_same_shape_still_recreates_brick_side(self) -> None:
        canonical_gfid = "11111111-2222-3333-4444-555555555555"
        logical_path = "repair-canary-127/alpha/beta"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("recreate_missing_directory_backend", action.repair_strategy)
        self.assertEqual(["host-d"], action.missing_hosts)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        step_types = [step.step_type for step in result.steps]
        self.assertIn("backup_stale_dir_gfid", step_types)
        self.assertIn("remove_stale_dir_gfid", step_types)
        self.assertIn("mkdir_directory_backend", step_types)
        self.assertIn("attach_directory_gfid", step_types)
        self.assertIn("verify_directory_backend", step_types)

    def test_fake_directory_canary_below_quorum_deletes_as_review(self) -> None:
        gfid = "0f0f0f0f-1111-2222-3333-555555555555"
        logical_path = "repair-canary-123/stale-subdir"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid, present=True, logical_path=logical_path)],
                    "host-b": [
                        ResolutionObservation(
                            host="host-b",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-c": [
                        ResolutionObservation(
                            host="host-c",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir", "mount_missing_while_backend_present:host-a"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_subtree", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("review_probable_stale_subtree", step_types)
        self.assertNotIn("mkdir_directory_backend", step_types)
        self.assertNotIn("ensure_directory_via_mount", step_types)

    def test_missing_directory_builds_brick_side_recipe(self) -> None:
        canonical_gfid = "49cfe0d0-039d-4e4b-8a89-35a549d8542b"
        canonical_backend = "/srv/gluster/brick-store/gtest/example/path"

        manifest = {
            "example/path": ManifestObject(
                logical_path="example/path",
                object_type="directory",
                depth=3,
                raw_entries=["example/path"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", canonical_backend, canonical_gfid, present=True)],
                    "host-b": [_dir_obs("host-b", canonical_backend, canonical_gfid, present=True)],
                    "host-c": [_dir_obs("host-c", canonical_backend, canonical_gfid, present=True)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/example/path",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend="/srv/gluster/brick-store/gtest/example/path",
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/path",
                            mounted="/example/path",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("recreate_missing_directory_backend", action.repair_strategy)
        self.assertEqual("safe_default", action.decision_class)
        self.assertEqual("host-c", action.directory_canonical_host)
        self.assertEqual(canonical_backend, action.directory_canonical_backend)
        self.assertEqual(canonical_gfid, action.directory_canonical_gfid)
        self.assertEqual(["host-d"], action.missing_hosts)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_directory_metadata", result.action_type)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("mkdir_directory_backend", step_types)
        self.assertIn("verify_directory_backend", step_types)
        self.assertNotIn("ensure_directory_via_mount", step_types)

    def test_below_quorum_directory_becomes_delete_review(self) -> None:
        gfid = "11111111-2222-3333-4444-555555555555"
        backend = "/srv/gluster/brick-store/gtest/example/stale"
        manifest = {
            "example/stale": ManifestObject(
                logical_path="example/stale",
                object_type="directory",
                depth=3,
                raw_entries=["example/stale"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid, present=True)],
                    "host-b": [
                        ResolutionObservation(
                            host="host-b",
                            raw_entry="/example/stale",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/stale",
                            mounted="/example/stale",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-c": [
                        ResolutionObservation(
                            host="host-c",
                            raw_entry="/example/stale",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/stale",
                            mounted="/example/stale",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/example/stale",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/stale",
                            mounted="/example/stale",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir", "mount_missing_while_backend_present:host-a"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_subtree", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("review_probable_stale_subtree", step_types)
        self.assertNotIn("mkdir_directory_backend", step_types)
        self.assertNotIn("ensure_directory_via_mount", step_types)

    def test_below_quorum_directory_auto_policy_deletes_stale_subtree(self) -> None:
        gfid = "11111111-2222-3333-4444-555555555555"
        backend = "/srv/gluster/brick-store/gtest/example/stale-auto"
        manifest = {
            "example/stale-auto": ManifestObject(
                logical_path="example/stale-auto",
                object_type="directory",
                depth=3,
                raw_entries=["example/stale-auto"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid, present=True)],
                    "host-b": [
                        ResolutionObservation(
                            host="host-b",
                            raw_entry="/example/stale-auto",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/stale-auto",
                            mounted="/example/stale-auto",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-c": [
                        ResolutionObservation(
                            host="host-c",
                            raw_entry="/example/stale-auto",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/stale-auto",
                            mounted="/example/stale-auto",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/example/stale-auto",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="example/stale-auto",
                            mounted="/example/stale-auto",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir", "mount_missing_while_backend_present:host-a"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"probable_stale_survivor_directory": "auto"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("configured batch policy: auto", "\n".join(result.notes))
        self.assertIn("review_probable_stale_subtree", [step.step_type for step in result.steps])

    def test_directory_gfid_conflict_stays_review_only(self) -> None:
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "ffffffff-1111-2222-3333-444444444444"
        backend_a = "/srv/gluster/brick-store/gtest/example/conflict"
        backend_b = "/srv/gluster/brick-store/gtest/example/conflict"
        manifest = {
            "example/conflict": ManifestObject(
                logical_path="example/conflict",
                object_type="directory",
                depth=3,
                raw_entries=["example/conflict"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend_a, gfid_a, present=True)],
                    "host-b": [_dir_obs("host-b", backend_a, gfid_a, present=True)],
                    "host-c": [_dir_obs("host-c", backend_a, gfid_b, present=True)],
                    "host-d": [
                        _dir_obs(
                            "host-d",
                            backend_b,
                            gfid_b,
                            present=True,
                            mounted_error="Input/output error",
                        )
                    ],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_directory_gfid_conflict", action.action_type)
        self.assertEqual("rename_conflicting_directory", action.repair_strategy)
        self.assertIn("mount returned EIO/ENOTCONN", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_gfid_conflict", result.action_type)
        self.assertEqual("review", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("review_directory_gfid_conflict", step_types)
        self.assertNotIn("mkdir_directory_backend", step_types)
        self.assertNotIn("ensure_directory_via_mount", step_types)

    def test_directory_metadata_cleanup_stays_review_only(self) -> None:
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": "repair:example/metadata",
                        "logical_path": "example/metadata",
                        "action_type": "reconcile_directory",
                        "object_type": "directory",
                        "depth": 3,
                        "repair_strategy": "cleanup_directory_metadata",
                        "mounted_target": "/gtest/example/metadata",
                        "stale_gfid_paths_by_host": {
                            "host-d": ["/.glusterfs/11/22/metadata-gfid"]
                        },
                        "notes": ["synthetic metadata-only cleanup case"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("reconcile_directory", result.action_type)
        self.assertEqual("dry-run", result.execution_mode)
        self.assertIn("remove stale directory GFID metadata while leaving child-set state intact", result.notes)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("cleanup_directory_metadata", step_types)
        self.assertNotIn("ensure_directory_via_mount", step_types)
        self.assertNotIn("mkdir_directory_backend", step_types)

    def test_directory_gfid_conflict_remains_review_only(self) -> None:
        logical_path = "repair-canary-128/conflict"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=1,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-d": [_dir_obs("host-d", backend, gfid_b, present=True, logical_path=logical_path)],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_directory_gfid_conflict", action.action_type)
        self.assertEqual("rename_conflicting_directory", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_gfid_conflict", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_directory_gfid_conflict", [step.step_type for step in result.steps])

    def test_directory_gfid_conflict_with_divergent_children_preserves_both_trees(self) -> None:
        logical_path = "repair-canary-128/conflict-children"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=1,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path, child_names=["payload.txt"])],
                    "host-b": [_dir_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path, child_names=["payload.txt"])],
                    "host-c": [_dir_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path, child_names=["shadow.txt"])],
                    "host-d": [_dir_obs("host-d", backend, gfid_b, present=True, logical_path=logical_path, child_names=["shadow.txt"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        action = build_plan(manifest, mountpoint="/gtest")[0]

        self.assertEqual("review_directory_gfid_conflict", action.action_type)
        self.assertEqual("rename_conflicting_directory", action.repair_strategy)
        self.assertEqual("quarantine_both", action.recommended_choice)
        self.assertNotIn("directory_child_gap:present", action.graph_markers)

        result = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )[0]
        self.assertEqual("review", result.status)
        self.assertEqual("quarantine_both", result.recommended_choice)
        self.assertIn("recommended action: quarantine_both", " ".join(result.notes))

    def test_directory_gfid_conflict_quarantine_policy_proposes_quarantine(self) -> None:
        logical_path = "repair-canary-128/conflict-quarantine"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=1,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-d": [_dir_obs("host-d", backend, gfid_b, present=True, logical_path=logical_path)],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        self.assertEqual("review_directory_gfid_conflict", action.action_type)
        self.assertEqual("rename_conflicting_directory", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_gfid_conflict": "quarantine"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_gfid_conflict", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("quarantine mode: both", result.notes)
        self.assertIn("quarantine targets:", " ".join(result.notes))
        self.assertIn("quarantine_directory_backend", [step.step_type for step in result.steps])
        self.assertTrue(any("mv --" in " ".join(step.command_preview) for step in result.steps))

    def test_directory_quarantine_loser_uses_gfid_before_shared_backend_path(self) -> None:
        logical_path = "repair-canary-128/conflict-loser"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"
        action = {
            "action_id": f"repair:{logical_path}",
            "logical_path": logical_path,
            "action_type": "review_directory_gfid_conflict",
            "object_type": "directory",
            "repair_strategy": "rename_conflicting_directory",
            "directory_canonical_host": "host-a",
            "directory_canonical_backend": backend,
            "directory_canonical_gfid": gfid_a,
            "directory_copies": [
                {
                    "host": "host-a",
                    "backend": backend,
                    "identity": gfid_a,
                    "gfid_path": f"/brick/.glusterfs/aa/aa/{gfid_a}",
                },
                {
                    "host": "host-b",
                    "backend": backend,
                    "identity": gfid_b,
                    "gfid_path": f"/brick/.glusterfs/11/11/{gfid_b}",
                },
            ],
        }

        result = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"directory_choice": "quarantine_loser"}},
        )[0]

        self.assertEqual("proposed", result.status)
        backend_steps = [
            step for step in result.steps
            if step.step_type == "quarantine_directory_backend"
        ]
        self.assertEqual(["host-b"], [step.host for step in backend_steps])
        self.assertTrue(result.revert_steps)

    def test_directory_quarantine_side_uses_explicit_gfid_cohort(self) -> None:
        logical_path = "repair-canary-128/conflict-side"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"
        action = {
            "action_id": f"repair:{logical_path}",
            "logical_path": logical_path,
            "action_type": "review_directory_gfid_conflict",
            "object_type": "directory",
            "repair_strategy": "rename_conflicting_directory",
            "directory_copies": [
                {
                    "host": "host-a",
                    "backend": backend,
                    "identity": gfid_a,
                    "gfid_path": f"/brick/.glusterfs/aa/aa/{gfid_a}",
                },
                {
                    "host": "host-b",
                    "backend": backend,
                    "identity": gfid_b,
                    "gfid_path": f"/brick/.glusterfs/11/11/{gfid_b}",
                },
            ],
        }

        result = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "quarantine_side",
                    "directory_quarantine_identity": gfid_a,
                }
            },
        )[0]

        self.assertEqual("proposed", result.status)
        backend_steps = [
            step
            for step in result.steps
            if step.step_type == "quarantine_directory_backend"
        ]
        self.assertEqual(["host-a"], [step.host for step in backend_steps])
        self.assertIn(f"quarantine selected cohort gfid: {gfid_a}", result.notes)
        self.assertIn("quarantine mode: side", result.notes)
        self.assertTrue(result.revert_steps)

    def test_directory_quarantine_side_rejects_stale_gfid_cohort(self) -> None:
        logical_path = "repair-canary-128/conflict-stale-side"
        current_gfid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        action = {
            "action_id": f"repair:{logical_path}",
            "logical_path": logical_path,
            "action_type": "review_directory_gfid_conflict",
            "object_type": "directory",
            "repair_strategy": "rename_conflicting_directory",
            "directory_copies": [
                {
                    "host": "host-a",
                    "backend": f"/srv/gluster/brick-store/gtest/{logical_path}",
                    "identity": current_gfid,
                },
            ],
        }

        result = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "quarantine_side",
                    "directory_quarantine_identity": "stale-gfid",
                }
            },
        )[0]

        self.assertEqual("review", result.status)
        self.assertFalse(
            any(
                step.step_type == "quarantine_directory_backend"
                for step in result.steps
            )
        )
        self.assertIn(
            "selected directory cohort GFID stale-gfid is absent from fresh evidence",
            result.notes,
        )

    def test_directory_gfid_conflict_majority_promotes_to_metadata_repair(self) -> None:
        logical_path = "repair-canary-128/conflict-majority"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=1,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-d": [
                        _dir_obs(
                            "host-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            mounted_error="Input/output error",
                        )
                    ],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("attach_directory_gfid", action.repair_strategy)
        self.assertEqual(["host-d"], action.directory_metadata_mismatch_hosts)
        self.assertIn("majority-backed canonical directory GFID", " ".join(action.notes))
        self.assertIn("mount returned EIO", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_directory_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("attach_directory_gfid", [step.step_type for step in result.steps])

    def test_directory_gfid_conflict_decision_marks_proposed(self) -> None:
        logical_path = "repair-canary-128/conflict"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=1,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, gfid_b, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, gfid_a, present=True, logical_path=logical_path)],
                    "host-d": [_dir_obs("host-d", backend, gfid_b, present=True, logical_path=logical_path)],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "keep_left",
                    "recommended_choice": "collapse_shared_subtree",
                }
            },
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_gfid_conflict", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertEqual("keep_left", result.decision.get("directory_choice"))
        self.assertIn("directory tie decision: keep_left", "\n".join(result.notes))

    def test_directory_gfid_conflict_merge_policy_updates_apply_notes(self) -> None:
        logical_path = "repair-canary-131/merge"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=1,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid_a, gfid_b],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid_a, present=True, logical_path=logical_path, child_names=["alpha"])],
                    "host-b": [_dir_obs("host-b", backend, gfid_a, present=True, logical_path=logical_path, child_names=["alpha"])],
                    "host-c": [_dir_obs("host-c", backend, gfid_b, present=True, logical_path=logical_path, child_names=["alpha"])],
                    "host-d": [_dir_obs("host-d", backend, gfid_b, present=True, logical_path=logical_path, child_names=["alpha"])],
                },
                notes=["saw_dir", "dir_name_gfid_conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        action = plan[0]
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("attach_directory_gfid", action.repair_strategy)

        review_action = action.to_dict()
        review_action["action_type"] = "review_directory_gfid_conflict"
        review_action["repair_strategy"] = "rename_conflicting_directory"
        results = build_apply_results(
            {"actions": [review_action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"directory_gfid_conflict": "merge"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_directory_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("attach_directory_gfid", [step.step_type for step in result.steps])
        self.assertIn("configured batch policy: merge", result.notes)
        self.assertIn(
            "directory tie merge resolved to the existing attach_directory_gfid path; this repairs identity without recreating the directory tree",
            result.notes,
        )

    def test_directory_child_gap_decision_without_dependencies_stays_review(self) -> None:
        logical_path = "repair-canary-130/child-gap"
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": f"repair:{logical_path}",
                        "logical_path": logical_path,
                        "action_type": "review_directory_children",
                        "object_type": "directory",
                        "depth": 3,
                        "repair_strategy": "reconcile_directory_children",
                        "mounted_target": f"/gtest/{logical_path}",
                        "healthy_hosts": ["host-a", "host-b", "host-c"],
                        "missing_hosts": ["host-d"],
                        "directory_child_names_by_host": {
                            "host-a": ["alpha", "beta"],
                            "host-b": ["alpha", "beta"],
                            "host-c": ["alpha", "beta"],
                            "host-d": ["alpha"],
                        },
                        "missing_directory_children_by_host": {"host-d": ["beta"]},
                        "notes": ["synthetic child-gap review case"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "reconcile_directory_children",
                    "recommended_choice": "reconcile_directory_children",
                }
            },
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_children", result.action_type)
        self.assertEqual("review", result.status)
        self.assertEqual("reconcile_directory_children", result.decision.get("directory_choice"))
        rendered = "\n".join(result.notes)
        self.assertIn("directory tie decision: reconcile_directory_children", rendered)
        self.assertIn("no executable child dependency is attached", rendered)
        self.assertNotIn("ensure_directory_via_mount", [step.step_type for step in result.steps])

    def test_directory_child_gap_auto_decision_without_dependencies_stays_review(self) -> None:
        logical_path = "repair-canary-130/child-gap-auto"
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": f"repair:{logical_path}",
                        "logical_path": logical_path,
                        "action_type": "review_directory_children",
                        "object_type": "directory",
                        "depth": 3,
                        "repair_strategy": "reconcile_directory_children",
                        "mounted_target": f"/gtest/{logical_path}",
                        "healthy_hosts": ["host-a", "host-b", "host-c"],
                        "missing_hosts": ["host-d"],
                        "directory_child_names_by_host": {
                            "host-a": ["alpha", "beta"],
                            "host-b": ["alpha", "beta"],
                            "host-c": ["alpha", "beta"],
                            "host-d": ["alpha"],
                        },
                        "missing_directory_children_by_host": {"host-d": ["beta"]},
                        "notes": ["synthetic child-gap review case"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "auto",
                    "recommended_choice": "reconcile_directory_children",
                }
            },
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_children", result.action_type)
        self.assertEqual("review", result.status)
        self.assertEqual("auto", result.decision.get("directory_choice"))
        rendered = "\n".join(result.notes)
        self.assertIn("directory tie decision: auto", rendered)
        self.assertIn("directory tie auto resolved to: reconcile_directory_children", rendered)
        self.assertIn("no executable child dependency is attached", rendered)
        self.assertNotIn("ensure_directory_via_mount", [step.step_type for step in result.steps])

    def test_directory_metadata_decision_promotes_to_attach_directory_gfid(self) -> None:
        logical_path = "repair-canary-131/metadata"
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": f"repair:{logical_path}",
                        "logical_path": logical_path,
                        "action_type": "review_directory_metadata",
                        "object_type": "directory",
                        "depth": 3,
                        "repair_strategy": "review_directory_presence",
                        "mounted_target": f"/gtest/{logical_path}",
                        "directory_canonical_gfid": "12345678-1234-1234-1234-123456789abc",
                        "directory_canonical_backend": f"/srv/gluster/brick-store/gtest/{logical_path}",
                        "directory_metadata_mismatch_hosts": ["host-d"],
                        "healthy_hosts": ["host-a", "host-b", "host-c", "host-d"],
                        "missing_hosts": [],
                        "notes": ["synthetic metadata review case"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "repair_directory_metadata",
                    "recommended_choice": "repair_directory_metadata",
                }
            },
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_directory_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertEqual("repair_directory_metadata", result.decision.get("directory_choice"))
        self.assertIn("directory tie decision: repair_directory_metadata", "\n".join(result.notes))
        self.assertIn("attach_directory_gfid", [step.step_type for step in result.steps])

    def test_directory_metadata_auto_decision_promotes_to_attach_directory_gfid(self) -> None:
        logical_path = "repair-canary-131/metadata-auto"
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": f"repair:{logical_path}",
                        "logical_path": logical_path,
                        "action_type": "review_directory_metadata",
                        "object_type": "directory",
                        "depth": 3,
                        "repair_strategy": "review_directory_presence",
                        "mounted_target": f"/gtest/{logical_path}",
                        "directory_canonical_gfid": "12345678-1234-1234-1234-123456789abc",
                        "directory_canonical_backend": f"/srv/gluster/brick-store/gtest/{logical_path}",
                        "directory_metadata_mismatch_hosts": ["host-d"],
                        "healthy_hosts": ["host-a", "host-b", "host-c", "host-d"],
                        "missing_hosts": [],
                        "notes": ["synthetic metadata review case"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "auto",
                    "recommended_choice": "repair_directory_metadata",
                }
            },
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_directory_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertEqual("auto", result.decision.get("directory_choice"))
        self.assertIn("directory tie decision: auto", "\n".join(result.notes))
        self.assertIn("directory tie auto resolved to: repair_directory_metadata", "\n".join(result.notes))
        self.assertIn("attach_directory_gfid", [step.step_type for step in result.steps])

    def test_directory_mdata_majority_promotes_to_attach_directory_mdata(self) -> None:
        logical_path = "repair-canary-132/mdata"
        gfid = "12345678-1234-1234-1234-123456789abc"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=["host-a", "host-b", "host-c"],
                observations={
                    "host-a": [
                        _dir_obs(
                            "host-a",
                            f"/brick-a/{logical_path}",
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            child_names=["seed"],
                            backend_mdata_hex="0x01020304",
                        )
                    ],
                    "host-b": [
                        _dir_obs(
                            "host-b",
                            f"/brick-b/{logical_path}",
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            child_names=["seed"],
                            backend_mdata_hex="0x01020304",
                        )
                    ],
                    "host-c": [
                        _dir_obs(
                            "host-c",
                            f"/brick-c/{logical_path}",
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            child_names=["seed"],
                            backend_mdata_hex="",
                        )
                    ],
                },
            )
        }

        actions = build_plan(manifest, "/gtest3", quorum_count=2)

        self.assertEqual(1, len(actions))
        action = actions[0]
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("attach_directory_mdata", action.repair_strategy)
        self.assertEqual("0x01020304", action.directory_canonical_mdata_hex)
        self.assertEqual(["host-c"], action.directory_mdata_mismatch_hosts)
        self.assertIn("directory_metadata_repair", action.graph_node)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertTrue(validate_execute_results(results)[0])
        step = next(step for step in result.steps if step.step_type == "attach_directory_mdata")
        self.assertEqual("host-c", step.host)
        self.assertEqual("0x01020304", step.source_path)
        self.assertIn("trusted.glusterfs.mdata", " ".join(step.command_preview))

    def test_directory_mdata_without_majority_stays_review_with_choices(self) -> None:
        logical_path = "repair-canary-132/mdata-tie"
        gfid = "12345678-1234-1234-1234-123456789abc"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=["host-a", "host-b", "host-c"],
                observations={
                    "host-a": [
                        _dir_obs(
                            "host-a",
                            f"/brick-a/{logical_path}",
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            child_names=["seed"],
                            backend_mdata_hex="0x01020304",
                        )
                    ],
                    "host-b": [
                        _dir_obs(
                            "host-b",
                            f"/brick-b/{logical_path}",
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            child_names=["seed"],
                            backend_mdata_hex="0x05060708",
                        )
                    ],
                    "host-c": [
                        _dir_obs(
                            "host-c",
                            f"/brick-c/{logical_path}",
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            child_names=["seed"],
                            backend_mdata_hex="0x090a0b0c",
                        )
                    ],
                },
            )
        }

        actions = build_plan(manifest, "/gtest3", quorum_count=2)

        self.assertEqual(1, len(actions))
        action = actions[0]
        self.assertEqual("review_directory_metadata", action.action_type)
        self.assertEqual("review_directory_mdata_state", action.repair_strategy)
        self.assertIn("no clear majority exists", "\n".join(action.notes))
        self.assertEqual(
            {
                "host-a": "0x01020304",
                "host-b": "0x05060708",
                "host-c": "0x090a0b0c",
            },
            action.directory_mdata_by_host,
        )

        rendered = render_apply_run(
            build_apply_results(
                {"actions": [action.to_dict()]},
                execution_mode="dry-run",
                backup_mode="required",
                batch=True,
            )
        )
        self.assertTrue(action.native_heal_first)
        self.assertEqual("repair_directory_metadata", action.native_heal_fallback_action)
        self.assertIn("try Gluster heal/rescan first", action.native_heal_reason)
        self.assertIn("run Gluster heal/rescan first", rendered)
        self.assertIn("If the drift remains after rescan, rerun and promote to repair_directory_metadata.", rendered)
        self.assertIn("choice: run Gluster heal/rescan, choose explicit mdata source brick/value, keep review, or skip", rendered)
        self.assertFalse(action.followup_edges)

        decided_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={
                logical_path: {
                    "directory_choice": "repair_directory_mdata",
                    "mdata_source_host": "host-b",
                }
            },
        )
        self.assertEqual(1, len(decided_results))
        decided = decided_results[0]
        self.assertEqual("repair_directory_metadata", decided.action_type)
        self.assertEqual("proposed", decided.status)
        self.assertIn("operator selected directory mdata source: host-b", "\n".join(decided.notes))
        self.assertTrue(validate_execute_results(decided_results)[0])
        mdata_steps = [step for step in decided.steps if step.step_type == "attach_directory_mdata"]
        self.assertEqual(["host-a", "host-c"], sorted(step.host for step in mdata_steps))
        self.assertEqual({"0x05060708"}, {step.source_path for step in mdata_steps})
        self.assertEqual(
            {
                ("host-a", f"/brick-a/{logical_path}"),
                ("host-c", f"/brick-c/{logical_path}"),
            },
            {(step.host, step.target_path) for step in mdata_steps},
        )

    def test_directory_stale_survivor_subtree_deletes_deepest_first(self) -> None:
        parent = "repair-canary-129/stale"
        child = "repair-canary-129/stale/child"

        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": "repair:repair-canary-129/stale",
                        "logical_path": parent,
                        "action_type": "review_probable_stale_survivor",
                        "object_type": "directory",
                        "depth": 2,
                        "repair_strategy": "delete_below_quorum_subtree",
                        "mounted_target": f"/gtest/{parent}",
                        "stale_gfid_paths_by_host": {
                            "host-d": [
                                f"/.glusterfs/11/22/{child.replace('/', '-')}",
                                f"/.glusterfs/11/22/{parent.replace('/', '-')}",
                            ]
                        },
                        "stale_backends_by_host": {
                            "host-d": [
                                f"/srv/gluster/brick-store/gtest/{child}",
                                f"/srv/gluster/brick-store/gtest/{parent}",
                            ]
                        },
                        "notes": ["synthetic stale subtree case"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("review_probable_stale_subtree", [step.step_type for step in result.steps])

        rm_targets = [
            step.target_path
            for step in result.steps
            if step.step_type == "remove_stale_backend"
        ]
        self.assertEqual(
            [f"/srv/gluster/brick-store/gtest/{child}", f"/srv/gluster/brick-store/gtest/{parent}"],
            rm_targets,
        )
        gfid_targets = [
            step.target_path
            for step in result.steps
            if step.step_type == "remove_stale_dir_gfid"
        ]
        self.assertEqual(
            [
                f"/.glusterfs/11/22/{child.replace('/', '-')}",
                f"/.glusterfs/11/22/{parent.replace('/', '-')}",
            ],
            gfid_targets,
        )

    def test_directory_entry_split_brain_stays_review_only(self) -> None:
        logical_path = "repair-canary-129/split"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid = "22222222-3333-4444-5555-666666666666"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, gfid, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, gfid, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="Input/output error",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=0,
                            backend_mode="drwxr-xr-x",
                            backend_lstat_type="dir",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("review_entry_split_brain_directory", action.repair_strategy)
        self.assertIn("split-brain or name/GFID conflict", " ".join(action.notes))
        self.assertIn("mount returned EIO/ENOTCONN", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_entry_split_brain", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_entry_split_brain_directory", [step.step_type for step in result.steps])
        self.assertNotIn("mkdir_directory_backend", [step.step_type for step in result.steps])

    def test_directory_entry_split_brain_enotconn_gets_split_brain_note(self) -> None:
        logical_path = "repair-canary-129/split-enotconn"
        backend = f"/srv/gluster/brick-store/gtest/{logical_path}"
        gfid = "33333333-4444-5555-6666-777777777777"

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[gfid],
                observations={
                    "host-a": [_dir_obs("host-a", backend, gfid, present=True, logical_path=logical_path)],
                    "host-b": [_dir_obs("host-b", backend, gfid, present=True, logical_path=logical_path)],
                    "host-c": [_dir_obs("host-c", backend, gfid, present=True, logical_path=logical_path)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry=f"/{logical_path}",
                            gfid=gfid,
                            gfid_path=f"/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                            backend=backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=logical_path,
                            mounted=f"/{logical_path}",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="Transport endpoint is not connected",
                            backend_lexists=True,
                            backend_exists=True,
                            backend_mtime=100,
                            backend_size=0,
                            backend_mode="drwxr-xr-x",
                            backend_lstat_type="dir",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid=gfid,
                            gfid_path_lexists=True,
                            gfid_path_exists=True,
                            gfid_path_lstat_type="symlink",
                            gfid_path_is_symlink=True,
                            gfid_path_readlink=backend,
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertIn("mount returned EIO/ENOTCONN", " ".join(action.notes))
        self.assertIn("below-quorum survivor evidence", " ".join(action.notes))

    def test_directory_dependency_closure_keeps_parent_chain(self) -> None:
        canonical_gfid = "49cfe0d0-039d-4e4b-8a89-35a549d8542b"
        manifest = {
            ".local": ManifestObject(
                logical_path=".local",
                object_type="directory",
                depth=1,
                raw_entries=[".local"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", "/srv/gluster/brick-store/gtest/.local", canonical_gfid, present=True)],
                    "host-b": [_dir_obs("host-b", "/srv/gluster/brick-store/gtest/.local", canonical_gfid, present=True)],
                    "host-c": [_dir_obs("host-c", "/srv/gluster/brick-store/gtest/.local", canonical_gfid, present=True)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/.local",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend="/srv/gluster/brick-store/gtest/.local",
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=".local",
                            mounted="/.local",
                            depth=1,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            ),
            ".local/share": ManifestObject(
                logical_path=".local/share",
                object_type="directory",
                depth=2,
                raw_entries=[".local/share"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", "/srv/gluster/brick-store/gtest/.local/share", canonical_gfid, present=True)],
                    "host-b": [_dir_obs("host-b", "/srv/gluster/brick-store/gtest/.local/share", canonical_gfid, present=True)],
                    "host-c": [_dir_obs("host-c", "/srv/gluster/brick-store/gtest/.local/share", canonical_gfid, present=True)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/.local/share",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend="/srv/gluster/brick-store/gtest/.local/share",
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=".local/share",
                            mounted="/.local/share",
                            depth=2,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            ),
            ".local/share/glib-2.0": ManifestObject(
                logical_path=".local/share/glib-2.0",
                object_type="directory",
                depth=3,
                raw_entries=[".local/share/glib-2.0"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", "/srv/gluster/brick-store/gtest/.local/share/glib-2.0", canonical_gfid, present=True)],
                    "host-b": [_dir_obs("host-b", "/srv/gluster/brick-store/gtest/.local/share/glib-2.0", canonical_gfid, present=True)],
                    "host-c": [_dir_obs("host-c", "/srv/gluster/brick-store/gtest/.local/share/glib-2.0", canonical_gfid, present=True)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/.local/share/glib-2.0",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend="/srv/gluster/brick-store/gtest/.local/share/glib-2.0",
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=".local/share/glib-2.0",
                            mounted="/.local/share/glib-2.0",
                            depth=3,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            ),
            ".local/share/glib-2.0/schemas": ManifestObject(
                logical_path=".local/share/glib-2.0/schemas",
                object_type="directory",
                depth=4,
                raw_entries=[".local/share/glib-2.0/schemas"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", "/srv/gluster/brick-store/gtest/.local/share/glib-2.0/schemas", canonical_gfid, present=True)],
                    "host-b": [_dir_obs("host-b", "/srv/gluster/brick-store/gtest/.local/share/glib-2.0/schemas", canonical_gfid, present=True)],
                    "host-c": [_dir_obs("host-c", "/srv/gluster/brick-store/gtest/.local/share/glib-2.0/schemas", canonical_gfid, present=True)],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/.local/share/glib-2.0/schemas",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend="/srv/gluster/brick-store/gtest/.local/share/glib-2.0/schemas",
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath=".local/share/glib-2.0/schemas",
                            mounted="/.local/share/glib-2.0/schemas",
                            depth=4,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            ),
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(4, len(plan))
        deepest = next(action for action in plan if action.logical_path == ".local/share/glib-2.0/schemas")
        self.assertEqual("recreate_missing_directory_backend", deepest.repair_strategy)
        self.assertEqual("safe_default", deepest.decision_class)
        self.assertIn("repair:.local/share/glib-2.0", deepest.depends_on)
        self.assertIn("repair:.local/share", deepest.depends_on)
        self.assertIn("repair:.local", deepest.depends_on)

        results = build_apply_results(
            {"actions": [action.to_dict() for action in plan]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(4, len(results))
        self.assertEqual(
            {"repair:.local", "repair:.local/share", "repair:.local/share/glib-2.0", "repair:.local/share/glib-2.0/schemas"},
            {result.action_id for result in results},
        )
        deepest_result = next(result for result in results if result.logical_path == ".local/share/glib-2.0/schemas")
        self.assertEqual("proposed", deepest_result.status)
        self.assertIn("mkdir_directory_backend", [step.step_type for step in deepest_result.steps])
        self.assertIn("verify_directory_backend", [step.step_type for step in deepest_result.steps])

    def test_deeper_sibling_directory_canary_stays_isolated(self) -> None:
        canonical_gfid = "c3b5f4a1-1111-2222-3333-444444444444"
        parent_backend = "/srv/gluster/brick-store/gtest/repair-canary-128/alpha/beta/gamma"
        missing_backend = f"{parent_backend}/epsilon"
        sibling_backend = f"{parent_backend}/delta"

        manifest = {
            "repair-canary-128/alpha/beta/gamma": ManifestObject(
                logical_path="repair-canary-128/alpha/beta/gamma",
                object_type="directory",
                depth=4,
                raw_entries=["repair-canary-128/alpha/beta/gamma"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", parent_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma")],
                    "host-b": [_dir_obs("host-b", parent_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma")],
                    "host-c": [_dir_obs("host-c", parent_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma")],
                    "host-d": [_dir_obs("host-d", parent_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma")],
                },
                notes=["saw_dir"],
            ),
            "repair-canary-128/alpha/beta/gamma/delta": ManifestObject(
                logical_path="repair-canary-128/alpha/beta/gamma/delta",
                object_type="directory",
                depth=5,
                raw_entries=["repair-canary-128/alpha/beta/gamma/delta"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", sibling_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/delta")],
                    "host-b": [_dir_obs("host-b", sibling_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/delta")],
                    "host-c": [_dir_obs("host-c", sibling_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/delta")],
                    "host-d": [_dir_obs("host-d", sibling_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/delta")],
                },
                notes=["saw_dir"],
            ),
            "repair-canary-128/alpha/beta/gamma/epsilon": ManifestObject(
                logical_path="repair-canary-128/alpha/beta/gamma/epsilon",
                object_type="directory",
                depth=5,
                raw_entries=["repair-canary-128/alpha/beta/gamma/epsilon"],
                source_hosts=["host-a", "host-b", "host-c", "host-d"],
                gfids=[canonical_gfid],
                observations={
                    "host-a": [_dir_obs("host-a", missing_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/epsilon")],
                    "host-b": [_dir_obs("host-b", missing_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/epsilon")],
                    "host-c": [_dir_obs("host-c", missing_backend, canonical_gfid, present=True, logical_path="repair-canary-128/alpha/beta/gamma/epsilon")],
                    "host-d": [
                        ResolutionObservation(
                            host="host-d",
                            raw_entry="/repair-canary-128/alpha/beta/gamma/epsilon",
                            gfid=canonical_gfid,
                            gfid_path=f"/.glusterfs/{canonical_gfid[:2]}/{canonical_gfid[2:4]}/{canonical_gfid}",
                            backend=missing_backend,
                            type="directory",
                            gfid_exists=True,
                            file_gfid="",
                            file_gfid_path="",
                            relpath="repair-canary-128/alpha/beta/gamma/epsilon",
                            mounted="/repair-canary-128/alpha/beta/gamma/epsilon",
                            depth=5,
                            mounted_checked=True,
                            mounted_lexists=False,
                            mounted_exists=False,
                            mounted_lstat_type="",
                            mounted_error="",
                            backend_lexists=False,
                            backend_exists=False,
                            backend_mtime=None,
                            backend_size=None,
                            backend_mode="",
                            backend_lstat_type="",
                            backend_is_symlink=False,
                            backend_readlink="",
                            backend_trusted_gfid="",
                            gfid_path_lexists=False,
                            gfid_path_exists=False,
                            gfid_path_lstat_type="",
                            gfid_path_is_symlink=False,
                            gfid_path_readlink="",
                            error="",
                        )
                    ],
                },
                notes=["saw_dir"],
            ),
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        epsilon = next(action for action in plan if action.logical_path == "repair-canary-128/alpha/beta/gamma/epsilon")
        delta = next(action for action in plan if action.logical_path == "repair-canary-128/alpha/beta/gamma/delta")
        self.assertEqual("recreate_missing_directory_backend", epsilon.repair_strategy)
        self.assertEqual("safe_default", epsilon.decision_class)
        self.assertEqual(["host-d"], epsilon.missing_hosts)
        self.assertEqual("review_directory_metadata", delta.action_type)
        self.assertEqual("review_directory_state", delta.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict() for action in plan]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(3, len(results))
        result = next(item for item in results if item.logical_path == "repair-canary-128/alpha/beta/gamma/epsilon")
        self.assertEqual("repair-canary-128/alpha/beta/gamma/epsilon", result.logical_path)
        self.assertEqual("proposed", result.status)
        self.assertIn("mkdir_directory_backend", [step.step_type for step in result.steps])
        self.assertIn("attach_directory_gfid", [step.step_type for step in result.steps])
        self.assertIn("verify_directory_backend", [step.step_type for step in result.steps])


if __name__ == "__main__":
    unittest.main()
