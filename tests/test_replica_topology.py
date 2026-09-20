# SPDX-License-Identifier: GPL-2.0-only
"""Tests for replica topology."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool.apply import (
    build_apply_results,
    filter_apply_results,
    filter_execute_ready_results,
    execute_apply_results,
    render_apply_run,
    render_apply_summary,
    summarize_apply_results,
    validate_execute_results,
)
from gluster_heal_tool.models import ManifestObject, ResolutionObservation
from gluster_heal_tool.planner import build_plan, render_plan_summary, summarize_plan
from gluster_heal_tool.resolver import (
    LiveResolver,
    _BACKEND_STAT_SCRIPT,
    _INDEX_PROBE_SCRIPT,
    _parse_key_value_output,
    _remote_command,
)
from gluster_heal_tool.ssh_identity import ssh_identity_options


def _file_obs(
    host: str,
    backend: str,
    file_gfid: str,
    *,
    present: bool,
    logical_path: str,
    backend_mtime: int = 100,
    backend_size: int = 17,
    backend_mode_bits: int | None = 0o644,
    backend_uid: int | None = 1000,
    backend_gid: int | None = 1000,
    backend_acl_access_text: str = "",
    backend_acl_default_text: str = "",
    backend_acl_error: str = "",
    mounted_error: str = "",
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
        mounted_error=mounted_error,
        backend_lexists=present,
        backend_exists=present,
        backend_mtime=backend_mtime,
        backend_size=backend_size,
        backend_mode="-rw-r--r--",
        backend_mode_bits=backend_mode_bits,
        backend_uid=backend_uid,
        backend_gid=backend_gid,
        backend_acl_access_text=backend_acl_access_text,
        backend_acl_default_text=backend_acl_default_text,
        backend_acl_error=backend_acl_error,
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


def _symlink_file_obs(
    host: str,
    backend: str,
    file_gfid: str,
    *,
    terminal_path: str,
    logical_path: str,
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
        mounted_exists=True,
        mounted_lstat_type="file",
        backend_lexists=True,
        backend_exists=True,
        backend_mtime=101,
        backend_size=30,
        backend_mode="lrwxrwxrwx",
        backend_lstat_type="symlink",
        backend_is_symlink=True,
        backend_readlink=terminal_path,
        backend_readlink_chain=[terminal_path],
        backend_terminal_path=terminal_path,
        backend_terminal_lstat_type="file",
        backend_terminal_is_symlink=False,
        backend_terminal_readlink="",
        backend_terminal_trusted_gfid=file_gfid,
        backend_trusted_gfid="",
        gfid_path_lexists=True,
        gfid_path_exists=True,
        gfid_path_lstat_type="symlink",
        gfid_path_is_symlink=True,
        gfid_path_readlink=terminal_path,
        gfid_path_readlink_chain=[terminal_path],
        gfid_path_terminal_path=terminal_path,
        gfid_path_terminal_lstat_type="file",
        gfid_path_terminal_is_symlink=False,
        gfid_path_terminal_readlink="",
        gfid_path_terminal_trusted_gfid=file_gfid,
        error="",
    )


def _dir_obs(
    host: str,
    backend: str,
    gfid: str,
    *,
    present: bool,
    logical_path: str,
    backend_child_names: list[str] | None = None,
    backend_child_scan_error: str = "",
    backend_mtime: int = 100,
    backend_mode_bits: int | None = 0o755,
    backend_uid: int | None = 1000,
    backend_gid: int | None = 1000,
    backend_acl_access_text: str = "",
    backend_acl_default_text: str = "",
    backend_acl_error: str = "",
) -> ResolutionObservation:
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
        mounted_lexists=present,
        mounted_exists=present,
        mounted_lstat_type="dir" if present else "",
        backend_lexists=present,
        backend_exists=present,
        backend_mtime=backend_mtime,
        backend_size=0,
        backend_mode="drwxr-xr-x",
        backend_mode_bits=backend_mode_bits,
        backend_uid=backend_uid,
        backend_gid=backend_gid,
        backend_acl_access_text=backend_acl_access_text,
        backend_acl_default_text=backend_acl_default_text,
        backend_acl_error=backend_acl_error,
        backend_lstat_type="dir",
        backend_is_symlink=False,
        backend_readlink="",
        backend_trusted_gfid=gfid,
        backend_child_names=list(backend_child_names or []),
        backend_child_scan_error=backend_child_scan_error,
        gfid_path_lexists=present,
        gfid_path_exists=present,
        gfid_path_lstat_type="symlink" if present else "",
        gfid_path_is_symlink=present,
        gfid_path_readlink=backend,
        error="",
    )


def _file_manifest(hosts: tuple[str, ...], present_hosts: set[str], logical_path: str) -> dict[str, ManifestObject]:
    file_gfid = "11111111-2222-3333-4444-555555555555"
    backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
    return {
        logical_path: ManifestObject(
            logical_path=logical_path,
            object_type="file",
            depth=2,
            raw_entries=[logical_path],
            source_hosts=list(hosts),
            gfids=[file_gfid],
            observations={
                host: [
                    _file_obs(
                        host,
                        backend,
                        file_gfid,
                        present=host in present_hosts,
                        logical_path=logical_path,
                    )
                ]
                for host in hosts
            },
            notes=["synthetic file topology"],
        )
    }


def _directory_manifest(hosts: tuple[str, ...], present_hosts: set[str], logical_path: str) -> dict[str, ManifestObject]:
    canonical_gfid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
    return {
        logical_path: ManifestObject(
            logical_path=logical_path,
            object_type="directory",
            depth=2,
            raw_entries=[logical_path],
            source_hosts=list(hosts),
            gfids=[canonical_gfid],
            observations={
                host: [
                    _dir_obs(
                        host,
                        backend,
                        canonical_gfid,
                        present=host in present_hosts,
                        logical_path=logical_path,
                    )
                ]
                for host in hosts
            },
            notes=["synthetic directory topology"],
        )
    }


class ReplicaTopologyTests(unittest.TestCase):
    def test_shared_ssh_identity_options_disable_x11_forwarding(self) -> None:
        self.assertIn("-x", ssh_identity_options())

    def test_remote_resolver_command_quotes_bare_gfid_rows(self) -> None:
        gfid = "90000000-0000-4000-8000-000000000003"
        command = _remote_command(["/usr/bin/resolver", "-e", f"<gfid:{gfid}>"], use_sudo=True)

        self.assertIn(f"'<gfid:{gfid}>'", command)
        self.assertNotIn(f" -e <gfid:{gfid}>", command)

    def test_index_probe_script_runs_as_python_c_payload(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            proc = subprocess.run(
                ["python3", "-c", _INDEX_PROBE_SCRIPT, handle.name],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["lexists"])
        self.assertEqual("file", payload["kind"])

    def test_backend_stat_script_runs_as_python_c_payload(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            proc = subprocess.run(
                ["python3", "-c", _BACKEND_STAT_SCRIPT, handle.name],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["lexists"])
        self.assertTrue(payload["exists"])
        self.assertEqual("file", payload["kind"])

    def test_resolver_parser_accepts_service_account_index_evidence(self) -> None:
        gfid = "90000000-0000-4000-8000-000000000003"
        live_target = "/srv/gluster/brick-store/testvol/brick/SteamShare/thomas/legacycompat"
        xattrop_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{gfid}"
        output = "\n".join(
            [
                f"GFID={gfid}",
                f"GFID_PATH={xattrop_path}",
                f"BACKEND={live_target}",
                "TYPE=dir",
                "GFID_EXISTS=1",
                "BACKEND_LEXISTS=1",
                "BACKEND_EXISTS=1",
                "BACKEND_LSTAT_TYPE=dir",
                f"BACKEND_TRUSTED_GFID={gfid}",
                "GFID_PATH_LEXISTS=1",
                "GFID_PATH_EXISTS=1",
                "GFID_PATH_LSTAT_TYPE=file",
                "RELPATH=SteamShare/thomas/legacycompat",
                "DEPTH=3",
                "",
            ]
        )

        obs = _parse_key_value_output("node-a", f"<gfid:{gfid}>", output)

        self.assertEqual(xattrop_path, obs.gfid_path)
        self.assertTrue(obs.gfid_path_exists)
        self.assertEqual("file", obs.gfid_path_lstat_type)
        self.assertEqual(live_target, obs.backend)
        self.assertTrue(obs.backend_exists)
        self.assertEqual("dir", obs.backend_lstat_type)
        self.assertEqual(gfid, obs.backend_trusted_gfid)

    def test_file_restore_is_topology_agnostic(self) -> None:
        layouts = [
            ("replica-3", ("brick-a", "brick-b", "brick-c")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                manifest = _file_manifest(hosts, set(hosts[:-1]), f"{label}/restore-file")
                plan = build_plan(manifest, mountpoint="/testvol")
                self.assertEqual(1, len(plan))
                action = plan[0]
                self.assertEqual("repair_file", action.action_type)
                self.assertEqual("restore_missing_replica", action.repair_strategy)
                self.assertEqual([hosts[-1]], action.missing_hosts)
                self.assertIn("file_restore:review", action.graph_markers)
                self.assertIn("post-repair verification: use a temp mount and stat the repaired logical path", action.notes)

                results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )
                self.assertEqual(1, len(results))
                result = results[0]
                self.assertEqual("repair_file", result.action_type)
                step_types = [step.step_type for step in result.steps]
                self.assertIn("stage_winner_local", step_types)
                self.assertIn("restore_via_mount", step_types)

    def test_file_below_quorum_excludes_exact_half_topologies(self) -> None:
        layouts = [
            ("replica-3", ("brick-a", "brick-b", "brick-c")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                manifest = _file_manifest(hosts, {hosts[0]}, f"{label}/stale-file")
                plan = build_plan(manifest, mountpoint="/testvol")
                self.assertEqual(1, len(plan))
                action = plan[0]
                self.assertEqual("review_probable_stale_survivor", action.action_type)
                self.assertEqual("delete_below_quorum_file", action.repair_strategy)
                self.assertEqual([hosts[0]], action.healthy_hosts)
                self.assertIn("file_stale_survivor:review", action.graph_markers)

                results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )
                self.assertEqual(1, len(results))
                result = results[0]
                self.assertEqual("proposed", result.status)
                step_types = [step.step_type for step in result.steps]
                self.assertIn("remove_stale_gfid", step_types)
                self.assertIn("remove_restored_mount_file", step_types)

    def test_file_below_quorum_uses_heal_source_hosts_when_access_is_broad(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        manifest = _file_manifest(hosts, set(hosts), "replica-4/stale-heal-source")
        manifest["replica-4/stale-heal-source"].source_hosts = [hosts[0]]
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_file", action.repair_strategy)
        self.assertIn("heal entry source hosts", " ".join(action.notes))

    def test_file_below_quorum_can_salvage_from_chosen_brick(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/stale-salvage"
        backends = {
            "brick-a": f"/gluster/a/testvol/{logical_path}",
            "brick-b": f"/gluster/b/testvol/{logical_path}",
            "brick-c": f"/gluster/c/testvol/{logical_path}",
            "brick-d": f"/gluster/d/testvol/{logical_path}",
        }
        file_gfid = "11111111-2222-3333-4444-555555555555"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [_file_obs("brick-a", backends["brick-a"], file_gfid, present=True, logical_path=logical_path, backend_mtime=220)],
                    "brick-b": [_file_obs("brick-b", backends["brick-b"], file_gfid, present=False, logical_path=logical_path, backend_mtime=180)],
                    "brick-c": [_file_obs("brick-c", backends["brick-c"], file_gfid, present=False, logical_path=logical_path)],
                    "brick-d": [_file_obs("brick-d", backends["brick-d"], file_gfid, present=False, logical_path=logical_path)],
                },
                notes=["synthetic below-quorum salvage topology"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_file", action.repair_strategy)
        self.assertEqual("brick-a", action.winner_host)
        self.assertEqual(backends, action.file_target_backend_by_host)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"probable_stale_survivor_file": "salvage"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertFalse(getattr(result, "native_heal_first", False))
        self.assertIn("configured batch policy: salvage", "\n".join(result.notes))
        self.assertIn("review-first salvage", "\n".join(result.notes))
        step_types = [step.step_type for step in result.steps]
        self.assertIn("restore_file_backend_gap_fill", step_types)
        self.assertIn("attach_file_gfid", step_types)
        self.assertNotIn("stage_winner_local", step_types)
        self.assertNotIn("restore_via_mount", step_types)
        gap_fill_steps = [step for step in result.steps if step.step_type == "restore_file_backend_gap_fill"]
        self.assertEqual({"brick-b", "brick-c", "brick-d"}, {step.host for step in gap_fill_steps})
        self.assertEqual({"brick-a"}, {step.source_host for step in gap_fill_steps})
        self.assertEqual({backends[step.host] for step in gap_fill_steps}, {step.target_path for step in gap_fill_steps})
        self.assertTrue(all("rsync-pull" in " ".join(step.command_preview) for step in gap_fill_steps))
        self.assertTrue(
            all(f"gluster-repair@{step.host}" in " ".join(step.command_preview) for step in gap_fill_steps)
        )
        self.assertTrue(
            all("sudo -n /opt/gluster-repair/gluster-host-ops.sh rsync-pull brick-a" in step.command_preview[-1] for step in gap_fill_steps)
        )

    def test_file_below_quorum_salvage_blocks_without_target_backend_evidence(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/stale-salvage-missing-target"
        file_gfid = "11111111-2222-3333-4444-555555555556"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [_file_obs("brick-a", f"/gluster/a/testvol/{logical_path}", file_gfid, present=True, logical_path=logical_path)],
                    "brick-b": [_file_obs("brick-b", "", file_gfid, present=False, logical_path=logical_path)],
                    "brick-c": [_file_obs("brick-c", f"/gluster/c/testvol/{logical_path}", file_gfid, present=False, logical_path=logical_path)],
                    "brick-d": [_file_obs("brick-d", f"/gluster/d/testvol/{logical_path}", file_gfid, present=False, logical_path=logical_path)],
                },
                notes=["synthetic below-quorum salvage with a missing target path"],
            )
        }

        action = build_plan(manifest, mountpoint="/testvol")[0]
        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"probable_stale_survivor_file": "salvage"},
        )

        self.assertEqual("review", results[0].status)
        self.assertNotIn("restore_file_backend_gap_fill", [step.step_type for step in results[0].steps])
        self.assertIn("missing or ambiguous target backend evidence: brick-b", "\n".join(results[0].notes))

    def test_live_resolver_selects_brick_root_per_host(self) -> None:
        with patch("gluster_heal_tool.resolver.subprocess.run") as run_mock:
            run_mock.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
                subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ]
            resolver = LiveResolver(
                volume="gtest",
                hosts=["node-a", "node-b."],
                brick_path="/fallback",
                brick_paths={
                    "node-a": "/gluster/node-a/brick",
                    "node-b": "/gluster/node-b/brick",
                },
                mountpoint="/gtest",
                resolver_path="/usr/bin/gluster-repair-resolver",
                ssh_user="root",
            )
            resolver.resolve_entry("/repair-canary/item")

        self.assertIn(
            "-b /gluster/node-a/brick",
            run_mock.call_args_list[0].args[0][-1],
        )
        self.assertIn(
            "-b /gluster/node-b/brick",
            run_mock.call_args_list[1].args[0][-1],
        )

    def test_dead_gfid_xattrop_proof_becomes_cleanup(self) -> None:
        dead_gfid = "90000000-0000-4000-8000-000000000003"
        live_target = "/srv/gluster/brick-store/testvol/brick/SteamShare/thomas/legacycompat"
        canonical_gfid_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/{dead_gfid[:2]}/{dead_gfid[2:4]}/{dead_gfid}"
        xattrop_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{dead_gfid}"
        remote_output = "\n".join(
            [
                f"GFID={dead_gfid}",
                f"GFID_PATH={canonical_gfid_path}",
                f"BACKEND={live_target}",
                "TYPE=dir",
                "RELPATH=SteamShare/thomas/legacycompat",
                "DEPTH=3",
                "",
            ]
        )
        xattrop_probe = {
            "lexists": True,
            "kind": "symlink",
            "readlink": "../../9d/6d/9d6de765-e14a-41f5-a2dd-f44bc68378b9/legacycompat",
            "terminal_path": live_target,
            "terminal_exists": True,
            "terminal_kind": "dir",
            "terminal_readlink": "",
            "terminal_trusted_gfid": dead_gfid,
        }
        backend_stat = {
            "exists": True,
            "mtime": 100,
            "size": 0,
            "mode": "drwxr-xr-x",
        }

        with patch("gluster_heal_tool.resolver.subprocess.run") as run_mock:
            run_mock.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=remote_output, stderr=""),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(xattrop_probe), stderr=""),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(backend_stat), stderr=""),
            ]
            resolver = LiveResolver(
                volume="gtest",
                hosts=["node-a"],
                brick_path="/srv/gluster/brick-store/testvol/brick",
                mountpoint="/gtest",
                resolver_path="/usr/bin/gluster-repair-resolver",
                ssh_user="root",
            )
            observations = resolver.resolve_entry(f"<gfid:{dead_gfid}>")

        self.assertEqual(1, len(observations))
        obs = observations[0]
        self.assertEqual(xattrop_path, obs.gfid_path)
        self.assertEqual(live_target, obs.gfid_path_terminal_path)
        self.assertEqual(dead_gfid, obs.gfid_path_terminal_trusted_gfid)
        self.assertEqual(live_target, obs.backend)
        self.assertEqual(dead_gfid, obs.backend_terminal_trusted_gfid)

        manifest = {
            f"<gfid:{dead_gfid}>": ManifestObject(
                logical_path=f"<gfid:{dead_gfid}>",
                object_type="dead_gfid",
                depth=0,
                raw_entries=[f"<gfid:{dead_gfid}>"],
                source_hosts=["node-a"],
                gfids=[dead_gfid],
                dead_gfids=[dead_gfid],
                observations={"node-a": [obs]},
                notes=["synthetic xattrop-backed live object case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("cleanup_stale_glusterfs_index", action.action_type)
        self.assertEqual("delete_stale_glusterfs_index_residue", action.repair_strategy)
        self.assertFalse(getattr(action, "native_heal_first", False))
        self.assertFalse(getattr(action, "native_heal_first", False))
        self.assertIn("stale .glusterfs index bookkeeping", " ".join(action.notes))
        self.assertEqual({xattrop_path}, set(action.stale_backends))
        self.assertNotIn(canonical_gfid_path, action.stale_gfid_paths)

    def test_bare_gfid_live_canonical_handle_stays_review_without_index_proof(self) -> None:
        gfid = "90000000-0000-4000-8000-000000000003"
        live_target = "/srv/gluster/brick-store/testvol/brick/SteamShare/thomas/legacycompat"
        canonical_gfid_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}"
        obs = ResolutionObservation(
            host="node-a",
            raw_entry=f"<gfid:{gfid}>",
            gfid=gfid,
            gfid_path=canonical_gfid_path,
            backend=live_target,
            type="dir",
            gfid_exists=True,
            relpath="SteamShare/thomas/legacycompat",
            depth=3,
            backend_exists=True,
            backend_mode="drwxr-xr-x",
        )
        manifest = {
            "SteamShare/thomas/legacycompat": ManifestObject(
                logical_path="SteamShare/thomas/legacycompat",
                object_type="unknown",
                depth=3,
                raw_entries=[f"<gfid:{gfid}>"],
                source_hosts=["node-a"],
                gfids=[gfid],
                observations={"node-a": [obs]},
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertFalse(getattr(action, "native_heal_first", False))
        self.assertEqual([live_target], action.dead_gfid_live_references)
        self.assertEqual([], action.stale_gfid_paths)

    def test_bare_gfid_xattrop_file_with_live_backend_gfid_becomes_index_cleanup(self) -> None:
        gfid = "90000000-0000-4000-8000-000000000003"
        live_target = "/srv/gluster/brick-store/testvol/brick/SteamShare/thomas/legacycompat"
        canonical_gfid_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}"
        xattrop_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{gfid}"
        remote_output = "\n".join(
            [
                f"GFID={gfid}",
                f"GFID_PATH={canonical_gfid_path}",
                f"BACKEND={live_target}",
                "TYPE=dir",
                "RELPATH=SteamShare/thomas/legacycompat",
                "DEPTH=3",
                "",
            ]
        )
        xattrop_probe = {
            "lexists": True,
            "kind": "file",
            "readlink": "",
            "terminal_path": xattrop_path,
            "terminal_exists": True,
            "terminal_kind": "file",
            "terminal_readlink": "",
            "terminal_trusted_gfid": "",
        }
        backend_stat = {
            "lexists": True,
            "exists": True,
            "kind": "dir",
            "mtime": 100,
            "size": 156,
            "mode": "drwxr-xr-x",
            "trusted_gfid": gfid,
        }

        with patch("gluster_heal_tool.resolver.subprocess.run") as run_mock:
            run_mock.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=remote_output, stderr=""),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(xattrop_probe), stderr=""),
                subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(backend_stat), stderr=""),
            ]
            resolver = LiveResolver(
                volume="gtest",
                hosts=["node-a"],
                brick_path="/srv/gluster/brick-store/testvol/brick",
                mountpoint="/gtest",
                resolver_path="/usr/bin/gluster-repair-resolver",
                ssh_user="root",
            )
            observations = resolver.resolve_entry(f"<gfid:{gfid}>")

        obs = observations[0]
        self.assertEqual(xattrop_path, obs.gfid_path)
        self.assertEqual(live_target, obs.backend)
        self.assertEqual(gfid, obs.backend_trusted_gfid)

        manifest = {
            "SteamShare/thomas/legacycompat": ManifestObject(
                logical_path="SteamShare/thomas/legacycompat",
                object_type="unknown",
                depth=3,
                raw_entries=[f"<gfid:{gfid}>"],
                source_hosts=["node-a"],
                gfids=[gfid],
                observations={"node-a": [obs]},
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("cleanup_stale_glusterfs_index", action.action_type)
        self.assertEqual("delete_stale_glusterfs_index_residue", action.repair_strategy)
        self.assertEqual({xattrop_path}, set(action.stale_backends))
        self.assertEqual({"node-a": [xattrop_path]}, action.stale_backends_by_host)
        self.assertNotIn(canonical_gfid_path, action.stale_gfid_paths)
        self.assertIn(live_target, " ".join(action.notes))

    def test_bare_gfid_two_file_identities_stay_entry_split_brain(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/bare-gfid-entry-split"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        left_gfid = "11111111-2222-3333-4444-555555555551"
        right_gfid = "11111111-2222-3333-4444-555555555552"
        left_index = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{left_gfid}"
        right_index = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{right_gfid}"
        observations = {}
        for host, gfid, index_path in (
            ("brick-a", left_gfid, left_index),
            ("brick-b", left_gfid, left_index),
            ("brick-c", right_gfid, right_index),
            ("brick-d", right_gfid, right_index),
        ):
            obs = _file_obs(
                host,
                backend,
                gfid,
                present=True,
                logical_path=logical_path,
            )
            obs.raw_entry = f"<gfid:{gfid}>"
            obs.gfid_path = index_path
            observations[host] = [obs]

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[f"<gfid:{left_gfid}>", f"<gfid:{right_gfid}>"],
                source_hosts=list(hosts),
                gfids=[left_gfid, right_gfid],
                file_gfids=[left_gfid, right_gfid],
                observations=observations,
                notes=["multiple-file-gfids-share-logical-path"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest4")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_entry_split_brain_file", action.repair_strategy)
        self.assertEqual("file_entry_split_brain_tie", action.graph_node)
        self.assertNotEqual("cleanup_stale_glusterfs_index", action.action_type)

    def test_replica_four_same_gfid_heal_split_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/same-gfid-content-split"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid = "11111111-2222-3333-4444-555555555553"
        index_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{gfid}"
        observations = {}
        for host in hosts:
            obs = _file_obs(
                host,
                backend,
                gfid,
                present=True,
                logical_path=logical_path,
            )
            obs.raw_entry = f"<gfid:{gfid}>"
            obs.gfid_path = index_path
            observations[host] = [obs]

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[f"<gfid:{gfid}>"],
                source_hosts=list(hosts),
                gfids=[gfid],
                file_gfids=[gfid],
                observations=observations,
                notes=["heal_info_marks_split_brain"],
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest4")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_entry_split_brain_file", action.repair_strategy)
        self.assertEqual("file_entry_split_brain_tie", action.graph_node)
        self.assertNotEqual("cleanup_stale_glusterfs_index", action.action_type)
        self.assertIn("heal info marks this file as split-brain", " ".join(action.notes))

        apply_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "auto"},
        )
        self.assertEqual(1, len(apply_results))
        apply_result = apply_results[0]
        self.assertEqual("review", apply_result.status)
        self.assertEqual("quarantine_both", apply_result.recommended_choice)
        self.assertIn("recommended next step: quarantine both", " ".join(apply_result.notes))

    def test_replica_four_half_present_file_requires_preservation_decision(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/file-presence-tie"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "11111111-2222-3333-4444-555555555550"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                file_gfids=[file_gfid],
                observations={
                    host: [
                        _file_obs(
                            host,
                            backend,
                            file_gfid,
                            present=host in hosts[:2],
                            logical_path=logical_path,
                            backend_mtime=100 + index,
                        )
                    ]
                    for index, host in enumerate(hosts)
                },
                notes=["synthetic replica-4 2-of-4 presence tie"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_file_presence_tie", action.repair_strategy)
        self.assertEqual("file_presence_tie", action.graph_node)
        self.assertIn("client quorum does not provide strict-majority repair authority", " ".join(action.notes))

        review_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "auto"},
        )
        self.assertEqual(1, len(review_results))
        review = review_results[0]
        self.assertEqual("review", review.status)
        self.assertEqual("quarantine_both", review.recommended_choice)
        self.assertIn("auto source selection is disabled", " ".join(review.notes))
        self.assertNotIn("restore_via_mount", [step.step_type for step in review.steps])

        quarantine_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"file_choice": "quarantine_both"}},
        )
        self.assertEqual(1, len(quarantine_results))
        quarantine = quarantine_results[0]
        self.assertEqual("proposed", quarantine.status)
        step_types = [step.step_type for step in quarantine.steps]
        self.assertEqual(2, step_types.count("quarantine_file_backend"))
        self.assertNotIn("restore_via_mount", step_types)
        self.assertTrue(quarantine.revert_steps)

    def test_replica_two_lone_file_requires_preservation_decision(self) -> None:
        hosts = ("brick-a", "brick-b")
        logical_path = "replica-2/file-presence-tie"
        manifest = _file_manifest(hosts, {hosts[0]}, logical_path)

        plan = build_plan(manifest, mountpoint="/testvol")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_file_presence_tie", action.repair_strategy)
        self.assertEqual("file_presence_tie", action.graph_node)
        self.assertEqual("quarantine_both", action.recommended_choice)

        review = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )[0]
        self.assertEqual("review", review.status)
        self.assertNotIn("remove_stale_gfid", [step.step_type for step in review.steps])

        quarantine = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"file_choice": "quarantine_both"}},
        )[0]
        self.assertEqual("proposed", quarantine.status)
        self.assertEqual(
            1,
            [step.step_type for step in quarantine.steps].count("quarantine_file_backend"),
        )
        self.assertTrue(quarantine.revert_steps)

    def test_even_width_half_present_directory_requires_preservation_decision(self) -> None:
        layouts = [
            ("replica-2", ("brick-a", "brick-b")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                logical_path = f"{label}/directory-presence-tie"
                present_hosts = set(hosts[: len(hosts) // 2])
                manifest = _directory_manifest(hosts, present_hosts, logical_path)

                plan = build_plan(manifest, mountpoint="/testvol")

                self.assertEqual(1, len(plan))
                action = plan[0]
                self.assertEqual("review_directory_gfid_conflict", action.action_type)
                self.assertEqual("ambiguous_directory_presence_tie", action.repair_strategy)
                self.assertEqual("directory_presence_tie", action.graph_node)
                self.assertEqual("quarantine_both", action.recommended_choice)

                review = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )[0]
                self.assertEqual("review", review.status)
                self.assertEqual("quarantine_both", review.recommended_choice)
                rendered_review = render_apply_run([review])
                self.assertIn(
                    "choice: quarantine_both, keep review, or skip; then explicitly restore or confirm deletion",
                    rendered_review,
                )
                self.assertNotIn("choice: quarantine_loser", rendered_review)
                self.assertNotIn(
                    "remove_stale_directory_backend",
                    [step.step_type for step in review.steps],
                )

                quarantine = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                    decisions={logical_path: {"directory_choice": "quarantine_both"}},
                )[0]
                self.assertEqual("proposed", quarantine.status)
                self.assertEqual(
                    len(present_hosts),
                    [step.step_type for step in quarantine.steps].count(
                        "quarantine_directory_backend"
                    ),
                )
                self.assertTrue(quarantine.revert_steps)

    def test_replica_two_file_identity_tie_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b")
        logical_path = "replica-2/file-entry-tie"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        left_gfid = "11111111-2222-3333-4444-555555555551"
        right_gfid = "11111111-2222-3333-4444-555555555552"
        observations = {
            host: [
                _file_obs(
                    host,
                    backend,
                    gfid,
                    present=True,
                    logical_path=logical_path,
                )
            ]
            for host, gfid in zip(hosts, (left_gfid, right_gfid))
        }
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[left_gfid, right_gfid],
                file_gfids=[left_gfid, right_gfid],
                observations=observations,
                notes=["synthetic replica-2 1-vs-1 entry tie"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_entry_split_brain_file", action.repair_strategy)
        self.assertEqual("file_entry_split_brain_tie", action.graph_node)
        self.assertIn("top file-identity cohorts are tied", " ".join(action.notes))

    def test_stale_index_cleanup_stays_review_when_source_host_probe_failed(self) -> None:

        gfid = "90000000-0000-4000-8000-000000000003"
        live_target = "/srv/gluster/brick-store/testvol/brick/SteamShare/thomas/legacycompat"
        xattrop_path = f"/srv/gluster/brick-store/testvol/brick/.glusterfs/indices/xattrop/{gfid}"
        proven_obs = ResolutionObservation(
            host="node-a",
            raw_entry=f"<gfid:{gfid}>",
            gfid=gfid,
            gfid_path=xattrop_path,
            backend=live_target,
            type="dir",
            gfid_exists=True,
            relpath="SteamShare/thomas/legacycompat",
            depth=3,
            backend_exists=True,
            backend_lstat_type="dir",
            backend_trusted_gfid=gfid,
        )
        failed_obs = ResolutionObservation(
            host="node-c",
            raw_entry=f"<gfid:{gfid}>",
            error="root@node-c: Permission denied (publickey,password).",
        )
        manifest = {
            "SteamShare/thomas/legacycompat": ManifestObject(
                logical_path="SteamShare/thomas/legacycompat",
                object_type="directory",
                depth=3,
                raw_entries=[f"<gfid:{gfid}>"],
                source_hosts=["node-a", "node-c"],
                gfids=[gfid],
                observations={"node-a": [proven_obs], "node-c": [failed_obs]},
            )
        }

        plan = build_plan(manifest, mountpoint="/gtest")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertEqual("follow_live_reference", action.repair_strategy)
        self.assertFalse(getattr(action, "native_heal_first", False))
        self.assertEqual({"node-a": [xattrop_path]}, action.stale_backends_by_host)
        self.assertIn("source hosts without stale-index proof: node-c", " ".join(action.notes))

    def test_file_below_quorum_salvage_is_replica_four_plus_only(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        manifest = _file_manifest(hosts, {hosts[0]}, "replica-3/stale-salvage")
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("delete_below_quorum_file", action.repair_strategy)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"probable_stale_survivor_file": "salvage"},
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertEqual("review_probable_stale_survivor", result.action_type)
        self.assertIn("replica-4+ only", "\n".join(result.notes))
        self.assertNotIn("restore_file_backend_gap_fill", [step.step_type for step in result.steps])

    def test_directory_restore_is_topology_agnostic_in_dry_run(self) -> None:
        layouts = [
            ("replica-3", ("brick-a", "brick-b", "brick-c")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                manifest = _directory_manifest(hosts, set(hosts[:-1]), f"{label}/restore-dir")
                plan = build_plan(manifest, mountpoint="/testvol")
                self.assertEqual(1, len(plan))
                action = plan[0]
                self.assertEqual("recreate_missing_directory_backend", action.repair_strategy)
                self.assertEqual([hosts[-1]], action.missing_hosts)
                self.assertIn(action.directory_canonical_host, hosts[:-1])
                self.assertIn("post-repair verification: use a temp mount and stat the recreated directory", action.notes)

                results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )
                self.assertEqual(1, len(results))
                result = results[0]
                step_types = [step.step_type for step in result.steps]
                self.assertIn("mkdir_directory_backend", step_types)
                self.assertIn("attach_directory_gfid", step_types)

    def test_directory_child_gap_backend_recreate_is_topology_agnostic(self) -> None:
        layouts = [
            ("replica-3", ("brick-a", "brick-b", "brick-c")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                manifest = _directory_manifest(hosts, set(hosts[:-1]), f"{label}/restore-dir-child-gap")
                manifest[f"{label}/restore-dir-child-gap"].notes.append("directory-backend-child-gap marker")
                plan = build_plan(manifest, mountpoint="/testvol")
                self.assertEqual(1, len(plan))
                action = plan[0]
                self.assertEqual("recreate_missing_directory_backend_child_gap", action.repair_strategy)
                self.assertEqual([hosts[-1]], action.missing_hosts)
                self.assertIn("directory_backend_child_gap:present", action.graph_markers)

                results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )
                self.assertEqual(1, len(results))
                result = results[0]
                step_types = [step.step_type for step in result.steps]
                self.assertIn("mkdir_directory_backend_child_gap", step_types)
                self.assertIn("attach_directory_gfid", step_types)
                self.assertTrue(
                    any(step.step_type == "verify_directory_backend" for step in result.steps)
                )
                self.assertTrue(
                    any(step.step_type == "remove_stale_backend" for step in result.revert_steps)
                )

    def test_directory_child_gap_is_topology_agnostic(self) -> None:
        layouts = [
            ("replica-3", ("brick-a", "brick-b", "brick-c")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                backend = f"/srv/gluster/brick-store/testvol/{label}/parent"
                parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
                child_name = "gamma"
                manifest = {
                    f"{label}/parent": ManifestObject(
                        logical_path=f"{label}/parent",
                        object_type="directory",
                        depth=2,
                        raw_entries=[f"/{label}/parent"],
                        source_hosts=list(hosts),
                        gfids=[parent_gfid],
                        observations={
                            host: [
                                _dir_obs(
                                    host,
                                    backend,
                                    parent_gfid,
                                    present=True,
                                    logical_path=f"{label}/parent",
                                    backend_child_names=[child_name] if host != hosts[-1] else [],
                                )
                            ]
                            for host in hosts
                        },
                        notes=["synthetic directory child gap"],
                    )
                }
                plan = build_plan(manifest, mountpoint="/testvol")
                self.assertEqual(1, len(plan))
                action = plan[0]
                self.assertEqual("reconcile_directory_children", action.repair_strategy)
                self.assertEqual("review_directory_children", action.action_type)
                self.assertIn(hosts[-1], action.missing_directory_children_by_host)
                self.assertEqual([child_name], action.missing_directory_children_by_host[hosts[-1]])
                self.assertNotIn("directory_child_gap:executable", action.graph_markers)

                results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )
                self.assertEqual(1, len(results))
                result = results[0]
                self.assertEqual("review_directory_children", result.action_type)
                self.assertEqual("review", result.status)
                rendered = "\n".join(result.notes + [step.step_type for step in result.steps])
                self.assertIn("collect immediate --gfid-child", rendered)
                self.assertNotIn("mkdir_directory_backend_child_gap", rendered)
                self.assertNotIn("attach_directory_gfid", rendered)
                self.assertNotIn("ensure_directory_via_mount", rendered)

    def test_directory_child_gap_bounded_subtree_candidate_is_marked(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        backend = "/srv/gluster/brick-store/testvol/bounded-parent"
        parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        child_names = ["beta", "gamma"]
        manifest = {
            "bounded-parent": ManifestObject(
                logical_path="bounded-parent",
                object_type="directory",
                depth=1,
                raw_entries=["/bounded-parent"],
                source_hosts=list(hosts),
                gfids=[parent_gfid],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            parent_gfid,
                            present=True,
                            logical_path="bounded-parent",
                            backend_child_names=child_names,
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            parent_gfid,
                            present=True,
                            logical_path="bounded-parent",
                            backend_child_names=child_names,
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            parent_gfid,
                            present=True,
                            logical_path="bounded-parent",
                            backend_child_names=["beta"],
                            backend_mtime=999,
                        )
                    ],
                },
                notes=["synthetic bounded child-gap", "directory-backend-child-gap marker"],
            )
        }
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("reconcile_directory_children", action.repair_strategy)
        self.assertIn("directory_child_gap:bounded_subtree_candidate", action.graph_markers)
        self.assertIn("bounded subtree candidate", " ".join(action.notes))
        self.assertIn(action.directory_canonical_host, {"brick-a", "brick-b"})
        self.assertNotEqual("brick-c", action.directory_canonical_host)
        self.assertIn("non-gap host with the complete child set", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_children", result.action_type)
        self.assertEqual("review", result.status)
        rendered = " ".join(result.notes)
        self.assertIn("collect immediate --gfid-child", rendered)
        step_types = [step.step_type for step in result.steps]
        self.assertEqual(["review_directory_children"], step_types)
        self.assertNotIn("mkdir_directory_backend_child_gap", step_types)
        self.assertNotIn("attach_directory_gfid", step_types)

    def test_directory_child_gap_bounded_subtree_candidate_without_marker(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        backend = "/srv/gluster/brick-store/testvol/bounded-parent-live"
        parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        child_names = ["beta", "gamma"]
        manifest = {
            "bounded-parent-live": ManifestObject(
                logical_path="bounded-parent-live",
                object_type="directory",
                depth=1,
                raw_entries=["/bounded-parent-live"],
                source_hosts=list(hosts),
                gfids=[parent_gfid],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            parent_gfid,
                            present=True,
                            logical_path="bounded-parent-live",
                            backend_child_names=child_names,
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            parent_gfid,
                            present=True,
                            logical_path="bounded-parent-live",
                            backend_child_names=child_names,
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            parent_gfid,
                            present=True,
                            logical_path="bounded-parent-live",
                            backend_child_names=["beta"],
                        )
                    ],
                },
                notes=["synthetic bounded child-gap"],
            )
        }
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("reconcile_directory_children", action.repair_strategy)
        self.assertIn("directory_child_gap:bounded_subtree_candidate", action.graph_markers)
        self.assertNotIn("directory-backend-child-gap marker", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("review_directory_children", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("collect immediate --gfid-child", " ".join(result.notes))
        step_types = [step.step_type for step in result.steps]
        self.assertEqual(["review_directory_children"], step_types)
        self.assertNotIn("mkdir_directory_backend_child_gap", step_types)
        self.assertNotIn("attach_directory_gfid", step_types)

    def test_directory_presence_tie_stays_review_without_majority(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        backend = "/srv/gluster/brick-store/testvol/quorum-parent"
        parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        child_name = "gamma"
        manifest = {
            "quorum-parent": ManifestObject(
                logical_path="quorum-parent",
                object_type="directory",
                depth=1,
                raw_entries=["/quorum-parent"],
                source_hosts=list(hosts),
                gfids=[parent_gfid],
                observations={
                    "brick-a": [_dir_obs("brick-a", backend, parent_gfid, present=True, logical_path="quorum-parent", backend_child_names=[child_name])],
                    "brick-b": [_dir_obs("brick-b", backend, parent_gfid, present=True, logical_path="quorum-parent", backend_child_names=[child_name])],
                    "brick-c": [_dir_obs("brick-c", backend, parent_gfid, present=False, logical_path="quorum-parent")],
                    "brick-d": [_dir_obs("brick-d", backend, parent_gfid, present=False, logical_path="quorum-parent")],
                },
                notes=["synthetic child-gap at quorum edge"],
            )
        }
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_directory_gfid_conflict", action.action_type)
        self.assertEqual("ambiguous_directory_presence_tie", action.repair_strategy)
        self.assertEqual("directory_presence_tie", action.graph_node)
        self.assertEqual("quarantine_both", action.recommended_choice)
        self.assertIn("client quorum does not provide strict-majority repair authority", " ".join(action.notes))

    def test_type_mismatch_build_plan_emits_graph_marker(self) -> None:
        manifest = {
            "type-mismatch": ManifestObject(
                logical_path="type-mismatch",
                object_type="type_mismatch",
                depth=1,
                notes=["synthetic type mismatch case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")

        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_type_mismatch", action.action_type)
        self.assertEqual("review_type_mismatch", action.repair_strategy)
        self.assertIn("type_mismatch:review", action.graph_markers)

    def test_directory_child_gap_promotes_when_child_dependencies_are_executable(self) -> None:
        layouts = [
            ("replica-3", ("brick-a", "brick-b", "brick-c")),
            ("replica-4", ("brick-a", "brick-b", "brick-c", "brick-d")),
        ]
        for label, hosts in layouts:
            with self.subTest(layout=label):
                parent_backend = f"/srv/gluster/brick-store/testvol/{label}/parent"
                child_backend = f"/srv/gluster/brick-store/testvol/{label}/parent/gamma"
                metadata_backend = f"/srv/gluster/brick-store/testvol/{label}/parent/delta"
                parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
                child_gfid = "cccccccc-dddd-eeee-ffff-000000000000"
                metadata_gfid = "dddddddd-eeee-ffff-0000-111111111111"
                child_name = "gamma"
                metadata_name = "delta"
                manifest = {
                    f"{label}/parent/gamma": ManifestObject(
                        logical_path=f"{label}/parent/gamma",
                        object_type="file",
                        depth=3,
                        raw_entries=[f"/{label}/parent/gamma"],
                        source_hosts=list(hosts),
                        gfids=[child_gfid],
                        observations={
                            host: [
                                _file_obs(
                                    host,
                                    child_backend,
                                    child_gfid,
                                    present=host != hosts[-1],
                                    logical_path=f"{label}/parent/gamma",
                                )
                            ]
                            for host in hosts
                        },
                        notes=["synthetic child-gap file"],
                    ),
                    f"{label}/parent/delta": ManifestObject(
                        logical_path=f"{label}/parent/delta",
                        object_type="file",
                        depth=3,
                        raw_entries=[f"/{label}/parent/delta"],
                        source_hosts=list(hosts),
                        gfids=[metadata_gfid],
                        observations={
                            host: [
                                _file_obs(
                                    host,
                                    metadata_backend,
                                    metadata_gfid,
                                    present=True,
                                    logical_path=f"{label}/parent/delta",
                                )
                            ]
                            for host in hosts
                        },
                        notes=["synthetic metadata-only child"],
                    ),
                    f"{label}/parent": ManifestObject(
                        logical_path=f"{label}/parent",
                        object_type="directory",
                        depth=2,
                        raw_entries=[f"/{label}/parent"],
                        source_hosts=list(hosts),
                        gfids=[parent_gfid],
                        children=[f"{label}/parent/gamma", f"{label}/parent/delta"],
                        observations={
                            host: [
                                _dir_obs(
                                    host,
                                    parent_backend,
                                    parent_gfid,
                                    present=True,
                                    logical_path=f"{label}/parent",
                                    backend_child_names=(
                                        [child_name, metadata_name]
                                        if host != hosts[-1]
                                        else [metadata_name]
                                    ),
                                )
                            ]
                            for host in hosts
                        },
                        notes=["synthetic child-gap with executable child repair"],
                    ),
                }
                plan = build_plan(manifest, mountpoint="/testvol")
                self.assertEqual(3, len(plan))
                by_path = {action.logical_path: action for action in plan}
                child_action = by_path[f"{label}/parent/gamma"]
                metadata_action = by_path[f"{label}/parent/delta"]
                parent_action = by_path[f"{label}/parent"]
                self.assertEqual("repair_file", child_action.action_type)
                self.assertEqual("restore_missing_replica", child_action.repair_strategy)
                self.assertEqual("review_file_metadata", metadata_action.action_type)
                self.assertEqual("review_metadata_only", metadata_action.repair_strategy)
                self.assertEqual("reconcile_directory", parent_action.action_type)
                self.assertEqual("reconcile_directory_children", parent_action.repair_strategy)
                self.assertIn(child_action.action_id, parent_action.depends_on)
                self.assertNotIn(metadata_action.action_id, parent_action.depends_on)

                results = build_apply_results(
                    {"actions": [action.to_dict() for action in plan]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                )
                self.assertEqual(3, len(results))
                parent_result = next(result for result in results if result.logical_path == f"{label}/parent")
                self.assertEqual("reconcile_directory", parent_result.action_type)
                self.assertEqual("planned", parent_result.status)
                rendered = "\n".join(parent_result.notes + [step.step_type for step in parent_result.steps])
                self.assertIn("child-gap dependency closure is executable", rendered)
                self.assertIn("wait_for_child_repairs", rendered)
                self.assertIn("child actions carry the actual backend/gfid repair", rendered.lower())
                self.assertIn("do not rewrite the already-present parent", rendered.lower())
                self.assertNotIn("mkdir_directory_backend_child_gap", rendered)
                self.assertNotIn("attach_directory_gfid", rendered)
                self.assertNotIn("verify_directory_backend_child_gap", rendered)

    def test_directory_child_gap_stays_review_when_only_metadata_children_remain(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        parent_backend = "/srv/gluster/brick-store/testvol/metadata-parent"
        parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        metadata_gfid = "dddddddd-eeee-ffff-0000-111111111111"
        metadata_child = "delta"
        manifest = {
            "metadata-parent/delta": ManifestObject(
                logical_path="metadata-parent/delta",
                object_type="file",
                depth=2,
                raw_entries=["/metadata-parent/delta"],
                source_hosts=list(hosts),
                gfids=[metadata_gfid],
                observations={
                    host: [
                        _file_obs(
                            host,
                            f"/srv/gluster/brick-store/testvol/metadata-parent/delta",
                            metadata_gfid,
                            present=True,
                            logical_path="metadata-parent/delta",
                        )
                    ]
                    for host in hosts
                },
                notes=["synthetic metadata-only child"],
            ),
            "metadata-parent": ManifestObject(
                logical_path="metadata-parent",
                object_type="directory",
                depth=1,
                raw_entries=["/metadata-parent"],
                source_hosts=list(hosts),
                gfids=[parent_gfid],
                children=["metadata-parent/delta"],
                observations={
                    host: [
                        _dir_obs(
                            host,
                            parent_backend,
                            parent_gfid,
                            present=True,
                            logical_path="metadata-parent",
                            backend_child_names=[metadata_child] if host != hosts[-1] else [],
                        )
                    ]
                    for host in hosts
                },
                notes=["synthetic metadata-only child gap"],
            ),
        }
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(2, len(plan))
        parent_action = next(action for action in plan if action.logical_path == "metadata-parent")
        self.assertEqual("review_directory_children", parent_action.action_type)
        self.assertEqual("reconcile_directory_children", parent_action.repair_strategy)
        self.assertEqual([], parent_action.depends_on)
        rendered = " ".join(parent_action.notes)
        self.assertIn("soft dependencies do not supply a structural child repair", rendered)
        self.assertIn("collect immediate --gfid-child", rendered)

    def test_directory_child_gap_promotes_when_only_stale_survivor_children_remain(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        parent_backend = "/srv/gluster/brick-store/testvol/stale-parent"
        parent_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        stale_gfid = "eeeeeeee-ffff-0000-1111-222222222222"
        child_name = "ghost"
        manifest = {
            "stale-parent/ghost": ManifestObject(
                logical_path="stale-parent/ghost",
                object_type="file",
                depth=2,
                raw_entries=["/stale-parent/ghost"],
                source_hosts=list(hosts),
                gfids=[stale_gfid],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            f"/srv/gluster/brick-store/testvol/stale-parent/ghost",
                            stale_gfid,
                            present=True,
                            logical_path="stale-parent/ghost",
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            f"/srv/gluster/brick-store/testvol/stale-parent/ghost",
                            stale_gfid,
                            present=False,
                            logical_path="stale-parent/ghost",
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            f"/srv/gluster/brick-store/testvol/stale-parent/ghost",
                            stale_gfid,
                            present=False,
                            logical_path="stale-parent/ghost",
                        )
                    ],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            f"/srv/gluster/brick-store/testvol/stale-parent/ghost",
                            stale_gfid,
                            present=False,
                            logical_path="stale-parent/ghost",
                        )
                    ],
                },
                notes=["synthetic stale survivor child"],
            ),
            "stale-parent": ManifestObject(
                logical_path="stale-parent",
                object_type="directory",
                depth=1,
                raw_entries=["/stale-parent"],
                source_hosts=list(hosts),
                gfids=[parent_gfid],
                children=["stale-parent/ghost"],
                observations={
                    host: [
                        _dir_obs(
                            host,
                            parent_backend,
                            parent_gfid,
                            present=True,
                            logical_path="stale-parent",
                            backend_child_names=[child_name] if host != hosts[-1] else [],
                        )
                    ]
                    for host in hosts
                },
                notes=["synthetic child-gap with stale survivor child"],
            ),
        }
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(2, len(plan))
        child_action = next(action for action in plan if action.logical_path == "stale-parent/ghost")
        parent_action = next(action for action in plan if action.logical_path == "stale-parent")
        self.assertEqual("review_probable_stale_survivor", child_action.action_type)
        self.assertEqual("delete_below_quorum_file", child_action.repair_strategy)
        self.assertEqual("reconcile_directory", parent_action.action_type)
        self.assertEqual("reconcile_directory_children", parent_action.repair_strategy)
        self.assertIn(child_action.action_id, parent_action.depends_on)

    def test_directory_child_gap_promotes_when_split_brain_child_is_auto_resolvable(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        parent_backend = "/srv/gluster/brick-store/testvol/auto-parent"
        parent_gfid = "99999999-8888-7777-6666-555555555555"
        child_backend = f"{parent_backend}/child"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        manifest = {
            "auto-parent/child": ManifestObject(
                logical_path="auto-parent/child",
                object_type="file",
                depth=2,
                raw_entries=["/auto-parent/child"],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            child_backend,
                            gfid_a,
                            present=True,
                            logical_path="auto-parent/child",
                            backend_mtime=130,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            child_backend,
                            gfid_a,
                            present=True,
                            logical_path="auto-parent/child",
                            backend_mtime=125,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            child_backend,
                            gfid_b,
                            present=True,
                            logical_path="auto-parent/child",
                            backend_mtime=220,
                        )
                    ],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            child_backend,
                            gfid_b,
                            present=True,
                            logical_path="auto-parent/child",
                            backend_mtime=215,
                            mounted_error="split-brain-like mount access error",
                        )
                    ],
                },
                notes=["synthetic auto-resolvable split-brain child"],
            ),
            "auto-parent": ManifestObject(
                logical_path="auto-parent",
                object_type="directory",
                depth=1,
                raw_entries=["/auto-parent"],
                source_hosts=list(hosts),
                gfids=[parent_gfid],
                children=["auto-parent/child"],
                observations={
                    host: [
                        _dir_obs(
                            host,
                            parent_backend,
                            parent_gfid,
                            present=True,
                            logical_path="auto-parent",
                            backend_child_names=["child"] if host != hosts[-1] else [],
                        )
                    ]
                    for host in hosts
                },
                notes=["synthetic child-gap with split-brain child"],
            ),
        }

        auto_plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(2, len(auto_plan))
        child_action = next(action for action in auto_plan if action.logical_path == "auto-parent/child")
        parent_action = next(action for action in auto_plan if action.logical_path == "auto-parent")
        self.assertEqual("review_entry_split_brain", child_action.action_type)
        self.assertEqual("ambiguous_entry_split_brain_file", child_action.repair_strategy)
        self.assertEqual("reconcile_directory", parent_action.action_type)
        self.assertEqual("reconcile_directory_children", parent_action.repair_strategy)
        self.assertIn(child_action.action_id, parent_action.depends_on)

        review_plan = build_plan(manifest, mountpoint="/testvol", split_brain_policy="review")
        review_parent = next(action for action in review_plan if action.logical_path == "auto-parent")
        self.assertEqual("review_directory_children", review_parent.action_type)
        self.assertEqual("reconcile_directory_children", review_parent.repair_strategy)

    def test_directory_metadata_xattr_mismatch_repairs_canonical_gfid(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/metadata-xattr"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        brick_d_backend = f"/gluster/homec/testvol/{logical_path}"
        canonical_gfid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=list(hosts),
                gfids=[canonical_gfid],
                observations={
                    "brick-a": [_dir_obs("brick-a", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "brick-b": [_dir_obs("brick-b", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "brick-c": [_dir_obs("brick-c", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "brick-d": [
                        _dir_obs(
                            "brick-d",
                            brick_d_backend,
                            canonical_gfid,
                            present=True,
                            logical_path=logical_path,
                        ),
                    ],
                },
                notes=["synthetic directory metadata xattr mismatch"],
            )
        }
        manifest[logical_path].observations["brick-d"][0].backend_trusted_gfid = ""
        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("attach_directory_gfid", action.repair_strategy)
        self.assertTrue(action.directory_metadata_mismatch_hosts)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertEqual("repair_directory_metadata", result.action_type)
        attach_step = next(
            step for step in result.steps if step.step_type == "attach_directory_gfid"
        )
        self.assertEqual("brick-d", attach_step.host)
        self.assertEqual(brick_d_backend, attach_step.target_path)
        self.assertIn("metadata mismatch hosts: brick-d", " ".join(result.notes))
        self.assertIn("directory metadata repairs", render_apply_summary(summarize_apply_results(results)))

    def test_execute_ready_skips_review_only_items(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        restore_manifest = _file_manifest(hosts, {"brick-a", "brick-b", "brick-c"}, "replica-4/restore-file")
        review_manifest = _file_manifest(hosts, set(hosts), "replica-4/review-file")

        restore_plan = build_plan(restore_manifest, mountpoint="/testvol")
        review_plan = build_plan(review_manifest, mountpoint="/testvol")

        restore_results = build_apply_results(
            {"actions": [restore_plan[0].to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        review_results = build_apply_results(
            {"actions": [review_plan[0].to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )

        ready, skipped = filter_execute_ready_results(review_results + restore_results)
        self.assertEqual(1, len(ready))
        self.assertEqual("repair_file", ready[0].action_type)
        self.assertEqual(1, len(skipped))
        self.assertEqual("review_file_metadata", skipped[0].action_type)

    def test_operator_summaries_emphasize_ready_repairs(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        restore_manifest = _file_manifest(hosts, {"brick-a", "brick-b", "brick-c"}, "replica-4/restore-file")
        review_manifest = _file_manifest(hosts, set(hosts), "replica-4/review-file")
        child_gap_manifest = {
            "replica-4/review-dir": ManifestObject(
                logical_path="replica-4/review-dir",
                object_type="directory",
                depth=2,
                raw_entries=["/replica-4/review-dir"],
                source_hosts=list(hosts),
                gfids=["bbbbbbbb-cccc-dddd-eeee-ffffffffffff"],
                observations={
                    host: [
                        _dir_obs(
                            host,
                            "/srv/gluster/brick-store/testvol/replica-4/review-dir",
                            "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                            present=True,
                            logical_path="replica-4/review-dir",
                            backend_child_names=["gamma"] if host != hosts[-1] else [],
                        )
                    ]
                    for host in hosts
                },
                notes=["synthetic review directory child gap"],
            )
        }

        restore_plan = build_plan(restore_manifest, mountpoint="/testvol")
        review_plan = build_plan(review_manifest, mountpoint="/testvol")
        child_gap_plan = build_plan(child_gap_manifest, mountpoint="/testvol")
        plan_summary = summarize_plan(restore_plan + review_plan)
        apply_results = build_apply_results(
            {"actions": [action.to_dict() for action in restore_plan + review_plan + child_gap_plan]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        apply_summary = summarize_apply_results(apply_results)

        self.assertIn("PLAN\n----", render_plan_summary(plan_summary))
        self.assertIn("Plan ready:", render_plan_summary(plan_summary))
        self.assertIn("review items:", render_plan_summary(plan_summary))
        self.assertIn("APPLY\n-----", render_apply_summary(apply_summary))
        self.assertIn("Apply ready:", render_apply_summary(apply_summary))
        self.assertIn("strategies:", render_apply_summary(apply_summary))
        self.assertIn("reconcile_directory_children=1", render_apply_summary(apply_summary))
        self.assertIn("directory-child-gap=1", render_apply_summary(apply_summary))
        self.assertIn(
            "controller-cycle: next_action=rebuild-plan",
            render_apply_summary(
                summarize_apply_results(
                    apply_results,
                    controller_cycle={"controller_next_action": "rebuild-plan"},
                )
            ),
        )
        self.assertIn(
            "controller-cycle: next_action=rebuild-plan",
            render_apply_run(
                apply_results,
                controller_cycle={
                    "controller_next_action": "rebuild-plan",
                },
            ),
        )

    def test_plan_summary_includes_graph_metadata(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        survivor_manifest = _file_manifest(hosts, {"brick-a"}, "replica-4/stale-survivor")

        plan = build_plan(survivor_manifest, mountpoint="/testvol")
        summary = summarize_plan(plan)
        rendered = render_plan_summary(summary)

        self.assertEqual(plan[0].graph_node, "file_stale_survivor_mount_visible")
        self.assertTrue(plan[0].provisional)
        self.assertTrue(plan[0].rescan_after_apply)
        self.assertIn("graph: provisional=1", rendered)
        self.assertIn("rescan=1", rendered)
        self.assertIn("nodes=file_stale_survivor_mount_visible=1", rendered)

    def test_scope_filters_repair_families(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        file_manifest = _file_manifest(hosts, {"brick-a", "brick-b", "brick-c"}, "replica-4/restore-file")
        dir_manifest = _directory_manifest(hosts, set(hosts[:-1]), "replica-4/restore-dir")

        file_plan = build_plan(file_manifest, mountpoint="/testvol")
        dir_plan = build_plan(dir_manifest, mountpoint="/testvol")

        file_results = build_apply_results(
            {"actions": [file_plan[0].to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        dir_results = build_apply_results(
            {"actions": [dir_plan[0].to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )

        combined = file_results + dir_results
        files_only = filter_apply_results(combined, scope="files")
        dirs_only = filter_apply_results(combined, scope="directories")
        metadata_only = filter_apply_results(combined, scope="metadata")

        self.assertEqual(1, len(files_only))
        self.assertEqual("repair_file", files_only[0].action_type)
        self.assertEqual(1, len(dirs_only))
        self.assertEqual("repair_directory_metadata", dirs_only[0].action_type)
        self.assertEqual(0, len(metadata_only))

    def test_file_metadata_xattr_mismatch_repairs_canonical_gfid(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/file-metadata-xattr"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        canonical_gfid = "cccccccc-dddd-eeee-ffff-000000000000"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[canonical_gfid],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "brick-b": [_file_obs("brick-b", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "brick-c": [_file_obs("brick-c", backend, canonical_gfid, present=True, logical_path=logical_path)],
                    "brick-d": [_file_obs("brick-d", backend, canonical_gfid, present=True, logical_path=logical_path)],
                },
                notes=["synthetic file metadata xattr mismatch"],
            )
        }
        manifest[logical_path].observations["brick-d"][0].backend_trusted_gfid = ""

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_file_metadata", action.action_type)
        self.assertEqual("attach_file_gfid", action.repair_strategy)
        self.assertEqual(["brick-d"], action.file_metadata_mismatch_hosts)
        self.assertTrue(action.native_heal_first)
        self.assertEqual("repair_file_metadata", action.native_heal_fallback_action)
        self.assertIn("try Gluster heal/rescan first", action.native_heal_reason)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_file_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("attach_file_gfid", [step.step_type for step in result.steps])
        rendered = render_apply_run(results)
        self.assertIn("run Gluster heal/rescan first", rendered)
        self.assertIn("file metadata repairs", render_apply_summary(summarize_apply_results(results)))

    def test_file_posix_metadata_majority_repairs_owner_and_mode(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/file-posix-majority"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "eeeeeeee-ffff-0000-1111-222222222222"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o644,
                            backend_uid=1000,
                            backend_gid=1000,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o644,
                            backend_uid=1000,
                            backend_gid=1000,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o600,
                            backend_uid=1001,
                            backend_gid=1001,
                        )
                    ],
                },
                notes=["synthetic POSIX metadata majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_posix_metadata", action.action_type)
        self.assertEqual("align_posix_metadata_majority", action.repair_strategy)
        self.assertEqual(["brick-a", "brick-b"], action.metadata_majority_hosts)
        self.assertEqual(["brick-c"], action.metadata_mismatch_hosts)
        self.assertEqual("brick-a", action.metadata_source_host)
        self.assertEqual("/srv/gluster/brick-store/testvol/replica-3/file-posix-majority", action.metadata_source_backend)
        self.assertEqual(["mode", "uid", "gid"], action.metadata_fields_to_align)
        self.assertIn("metadata tuple by host", " ".join(action.notes))

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_posix_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertFalse(getattr(result, "native_heal_first", False))
        self.assertTrue(validate_execute_results(results)[0])
        self.assertEqual([result], filter_apply_results(results, scope="metadata"))
        self.assertEqual([], filter_apply_results(results, scope="files"))
        step_types = [step.step_type for step in result.steps]
        self.assertIn("apply_posix_metadata_owner", step_types)
        self.assertIn("apply_posix_metadata_mode", step_types)
        self.assertIn("align_posix_metadata_majority", "\n".join(result.notes))
        commands = [" ".join(step.command_preview) for step in result.steps]
        self.assertIn("1000:1000", commands[0])
        self.assertIn("0644", commands[1])
        self.assertNotIn("--reference", " ".join(commands))

    def test_file_posix_metadata_majority_includes_acl_alignment(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/file-posix-acl-majority"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "eeeeeeee-ffff-0000-1111-222222222224"
        acl_majority = "user::rw-\ngroup::r--\nother::---"
        acl_mismatch = "user::rw-\ngroup::---\nother::---"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_majority,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_majority,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_mismatch,
                        )
                    ],
                },
                notes=["synthetic POSIX ACL majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_posix_metadata", action.action_type)
        self.assertEqual(["brick-a", "brick-b"], action.metadata_majority_hosts)
        self.assertEqual(["brick-c"], action.metadata_mismatch_hosts)
        self.assertEqual(["acl_access"], action.metadata_fields_to_align)
        self.assertEqual("brick-a", action.metadata_source_host)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_posix_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertFalse(getattr(result, "native_heal_first", False))
        self.assertTrue(validate_execute_results(results)[0])
        step_types = [step.step_type for step in result.steps]
        self.assertIn("apply_posix_metadata_acl", step_types)
        self.assertIn("setfacl", " ".join(result.steps[-1].command_preview))
        self.assertNotIn("setfacl -k", " ".join(result.steps[-1].command_preview))

    def test_file_posix_majority_stays_entry_split_brain_when_file_gfids_conflict(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/file-posix-with-entry-conflict"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "eeeeeeee-ffff-0000-1111-222222222226"
        conflicting_gfid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid, conflicting_gfid],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o644,
                            backend_uid=1000,
                            backend_gid=1000,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o644,
                            backend_uid=1000,
                            backend_gid=1000,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o600,
                            backend_uid=1001,
                            backend_gid=1001,
                        )
                    ],
                },
                notes=["synthetic POSIX metadata with file identity conflict"],
            )
        }
        manifest[logical_path].observations["brick-c"][0].backend_trusted_gfid = conflicting_gfid

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_file", action.action_type)
        self.assertEqual("replace_conflicting_file", action.repair_strategy)
        self.assertEqual(["brick-c"], action.conflict_hosts)
        self.assertFalse(getattr(action, "metadata_tuple_by_host", {}))
        self.assertNotIn("metadata tuple by host", " ".join(action.notes))
        self.assertNotEqual("repair_posix_metadata", action.action_type)

    def test_directory_posix_drift_stays_gfid_conflict_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/dir-posix-with-gfid-conflict"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        gfid_b = "11111111-2222-3333-4444-555555555555"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1000,
                            backend_gid=1000,
                            backend_child_names=["alpha", "beta"],
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1000,
                            backend_gid=1000,
                            backend_child_names=["alpha", "beta"],
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1000,
                            backend_gid=1000,
                            backend_child_names=["alpha", "beta"],
                        )
                    ],
                    "brick-d": [
                        _dir_obs(
                            "brick-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o700,
                            backend_uid=1001,
                            backend_gid=1001,
                            backend_child_names=["alpha", "beta"],
                        )
                    ],
                },
                notes=["synthetic directory metadata with GFID conflict"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_directory_metadata", action.action_type)
        self.assertEqual("attach_directory_gfid", action.repair_strategy)
        self.assertTrue(action.directory_metadata_mismatch_hosts)
        self.assertIn("same logical directory name appears with multiple GFIDs", " ".join(action.notes))
        self.assertFalse(getattr(action, "metadata_tuple_by_host", {}))
        self.assertNotEqual("repair_posix_metadata", action.action_type)

    def test_directory_posix_drift_stays_child_gap_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/dir-posix-with-child-gap"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid = "dddddddd-ffff-0000-1111-222222222225"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1000,
                            backend_gid=1000,
                            backend_child_names=["alpha", "beta"],
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1000,
                            backend_gid=1000,
                            backend_child_names=["alpha", "beta"],
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o700,
                            backend_uid=1001,
                            backend_gid=1001,
                            backend_child_names=["alpha"],
                        )
                    ],
                },
                notes=["synthetic directory child-gap with POSIX drift"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_directory_children", action.action_type)
        self.assertEqual("reconcile_directory_children", action.repair_strategy)
        self.assertIn("directory_child_gap:present", action.graph_markers)
        self.assertNotIn("directory_child_gap:executable", action.graph_markers)
        self.assertIn("children must be repaired before parent directory", " ".join(action.notes))
        self.assertIn("collect immediate --gfid-child", " ".join(action.notes))
        self.assertFalse(getattr(action, "metadata_tuple_by_host", {}))
        self.assertNotEqual("repair_posix_metadata", action.action_type)

    def test_directory_posix_default_acl_majority_includes_acl_alignment(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/dir-posix-default-acl-majority"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        dir_gfid = "dddddddd-ffff-0000-1111-222222222225"
        default_majority = "default:user::rwx\ndefault:group::r-x\ndefault:other::---"
        default_mismatch = "default:user::rwx\ndefault:group::---\ndefault:other::---"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[dir_gfid],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_default_text=default_majority,
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_default_text=default_majority,
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_default_text=default_mismatch,
                        )
                    ],
                },
                notes=["synthetic directory POSIX default ACL majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_posix_metadata", action.action_type)
        self.assertEqual(["acl_default"], action.metadata_fields_to_align)
        self.assertEqual(["brick-a", "brick-b"], action.metadata_majority_hosts)
        self.assertEqual(["brick-c"], action.metadata_mismatch_hosts)
        self.assertEqual("brick-a", action.metadata_source_host)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("repair_posix_metadata", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertFalse(getattr(result, "native_heal_first", False))
        self.assertTrue(validate_execute_results(results)[0])
        step_types = [step.step_type for step in result.steps]
        self.assertIn("apply_posix_metadata_acl", step_types)
        command = " ".join(result.steps[-1].command_preview)
        self.assertIn("setfacl -k", command)
        self.assertIn("setfacl --set-file=-", command)

    def test_path_led_clean_file_does_not_become_stale_survivor(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/path-led-clean-file"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "eeeeeeee-ffff-0000-1111-222222222226"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[f"/{logical_path}"],
                source_hosts=["localhost"],
                gfids=[file_gfid],
                observations={
                    host: [
                        _file_obs(
                            host,
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                        )
                    ]
                    for host in hosts
                },
                notes=["synthetic operator path-led clean file"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertNotEqual("review_probable_stale_survivor", action.action_type)
        self.assertEqual("review_file_metadata", action.action_type)
        self.assertNotIn("below quorum", " ".join(action.notes))

    def test_file_posix_metadata_without_majority_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/file-posix-review"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "eeeeeeee-ffff-0000-1111-222222222223"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o644,
                            backend_uid=1000,
                            backend_gid=1000,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o600,
                            backend_uid=1001,
                            backend_gid=1001,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1002,
                            backend_gid=1002,
                        )
                    ],
                },
                notes=["synthetic POSIX metadata no-majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_posix_metadata_no_majority", action.action_type)
        self.assertEqual("choose_posix_metadata_source", action.repair_strategy)
        self.assertEqual("posix_metadata_no_majority", action.graph_node)
        self.assertEqual("posix_metadata_no_majority", action.matrix_key)
        self.assertEqual(sorted(hosts), action.metadata_mismatch_hosts)
        self.assertEqual(["mode", "uid", "gid"], action.metadata_fields_to_align)
        self.assertFalse(getattr(action, "native_heal_first", False))
        action.brick_roles_by_host = {"brick-a": "data", "brick-b": "arbiter", "brick-c": "data"}
        self.assertIn("no strict majority exists", " ".join(action.notes))

        review_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(review_results))
        review = review_results[0]
        self.assertEqual("review_posix_metadata_no_majority", review.action_type)
        self.assertEqual("review", review.status)
        rendered = render_apply_run(review_results)
        self.assertIn("choice: choose source, keep review, or skip", rendered)
        self.assertIn("support: brick roles by host:", rendered)
        self.assertIn("brick-b: arbiter", rendered)
        self.assertNotIn("run Gluster heal/rescan first", rendered)

        decided_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"metadata_source_host": "brick-b"}},
        )
        self.assertEqual(1, len(decided_results))
        decided = decided_results[0]
        self.assertEqual("repair_posix_metadata", decided.action_type)
        self.assertEqual("proposed", decided.status)
        self.assertFalse(getattr(decided, "native_heal_first", False))
        self.assertIn("align_posix_metadata_selected_source", "\n".join(decided.notes))
        self.assertIn("operator selected POSIX metadata source host: brick-b", "\n".join(decided.notes))
        self.assertTrue(validate_execute_results(decided_results)[0])
        self.assertEqual({"brick-a", "brick-c"}, {step.host for step in decided.steps})
        commands = " ".join(" ".join(step.command_preview) for step in decided.steps)
        self.assertIn("1001:1001", commands)
        self.assertIn("0600", commands)

        native_action = action.to_dict()
        native_action["gluster_visible_metadata_split_brain"] = True
        native_action["metadata_source_reason"] = "native_gluster_source"
        native_decided_results = build_apply_results(
            {"actions": [native_action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            volume="testvol",
            brick_path="/srv/gluster/brick-store/testvol",
            decisions={logical_path: {"metadata_source_host": "brick-b"}},
        )
        self.assertEqual(1, len(native_decided_results))
        native_decided = native_decided_results[0]
        self.assertEqual("repair_posix_metadata", native_decided.action_type)
        self.assertEqual("proposed", native_decided.status)
        self.assertEqual("native_gluster_source", native_decided.metadata_source_reason)
        self.assertEqual(
            ["resolve_split_brain_gluster_cli"],
            [step.step_type for step in native_decided.steps],
        )
        self.assertIn("repair strategy: resolve_posix_metadata_source_brick", "\n".join(native_decided.notes))
        self.assertIn("source-brick", " ".join(native_decided.steps[0].command_preview))
        native_rendered = render_apply_run(native_decided_results)
        self.assertIn("note: brick roles by host:", native_rendered)
        self.assertIn("note:   brick-b: arbiter", native_rendered)
        self.assertTrue(validate_execute_results(native_decided_results)[0])

        manifest[logical_path].notes.append("heal_info_marks_split_brain")
        heal_visible_plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(heal_visible_plan))
        heal_visible_action = heal_visible_plan[0]
        self.assertEqual("review_posix_metadata_no_majority", heal_visible_action.action_type)
        self.assertEqual("choose_posix_metadata_source", heal_visible_action.repair_strategy)
        self.assertTrue(heal_visible_action.gluster_visible_metadata_split_brain)
        self.assertEqual("native_gluster_source", heal_visible_action.metadata_source_reason)
        self.assertIn("verify checksums", " ".join(heal_visible_action.notes))

        heal_visible_results = build_apply_results(
            {"actions": [heal_visible_action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            volume="testvol",
            brick_path="/srv/gluster/brick-store/testvol",
            decisions={logical_path: {"metadata_source_host": "brick-b"}},
        )
        self.assertEqual("repair_posix_metadata", heal_visible_results[0].action_type)
        self.assertEqual("native_gluster_source", heal_visible_results[0].metadata_source_reason)
        self.assertEqual(
            ["resolve_split_brain_gluster_cli"],
            [step.step_type for step in heal_visible_results[0].steps],
        )

    def test_file_posix_acl_without_majority_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/file-posix-acl-review"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "eeeeeeee-ffff-0000-1111-222222222227"
        acl_a = "user::rw-\ngroup::r--\nother::---"
        acl_b = "user::rw-\ngroup::---\nother::---"
        acl_c = "user::rw-\ngroup::r-x\nother::---"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_a,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_b,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            file_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_c,
                        )
                    ],
                },
                notes=["synthetic file POSIX ACL no-majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_posix_metadata_no_majority", action.action_type)
        self.assertEqual("choose_posix_metadata_source", action.repair_strategy)
        self.assertEqual(sorted(hosts), action.metadata_mismatch_hosts)
        self.assertEqual(["acl_access"], action.metadata_fields_to_align)
        self.assertFalse(getattr(action, "native_heal_first", False))

        decided_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"metadata_source_host": "brick-b"}},
        )
        self.assertEqual(1, len(decided_results))
        decided = decided_results[0]
        self.assertEqual("repair_posix_metadata", decided.action_type)
        self.assertEqual("proposed", decided.status)
        self.assertFalse(getattr(decided, "native_heal_first", False))
        self.assertIn("align_posix_metadata_selected_source", "\n".join(decided.notes))
        self.assertTrue(validate_execute_results(decided_results)[0])
        self.assertEqual({"brick-a", "brick-c"}, {step.host for step in decided.steps})
        self.assertIn("apply_posix_metadata_acl", [step.step_type for step in decided.steps])

    def test_directory_posix_acl_without_majority_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/dir-posix-acl-review"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        dir_gfid = "dddddddd-ffff-0000-1111-222222222227"
        acl_a = "user::rwx\ngroup::r-x\nother::---"
        acl_b = "user::rwx\ngroup::---\nother::---"
        acl_c = "user::rwx\ngroup::r--\nother::---"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[dir_gfid],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_a,
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_b,
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_acl_access_text=acl_c,
                        )
                    ],
                },
                notes=["synthetic directory POSIX ACL no-majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_posix_metadata_no_majority", action.action_type)
        self.assertEqual("choose_posix_metadata_source", action.repair_strategy)
        self.assertEqual(sorted(hosts), action.metadata_mismatch_hosts)
        self.assertEqual(["acl_access"], action.metadata_fields_to_align)
        self.assertFalse(getattr(action, "native_heal_first", False))

        decided_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"metadata_source_host": "brick-b"}},
        )
        self.assertEqual(1, len(decided_results))
        decided = decided_results[0]
        self.assertEqual("repair_posix_metadata", decided.action_type)
        self.assertEqual("proposed", decided.status)
        self.assertFalse(getattr(decided, "native_heal_first", False))
        self.assertIn("align_posix_metadata_selected_source", "\n".join(decided.notes))
        self.assertTrue(validate_execute_results(decided_results)[0])
        self.assertEqual({"brick-a", "brick-c"}, {step.host for step in decided.steps})
        self.assertIn("apply_posix_metadata_acl", [step.step_type for step in decided.steps])

    def test_directory_posix_metadata_without_majority_stays_review(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c")
        logical_path = "replica-3/dir-posix-review"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        dir_gfid = "dddddddd-ffff-0000-1111-222222222226"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="directory",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[dir_gfid],
                observations={
                    "brick-a": [
                        _dir_obs(
                            "brick-a",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o755,
                            backend_uid=1000,
                            backend_gid=1000,
                        )
                    ],
                    "brick-b": [
                        _dir_obs(
                            "brick-b",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o700,
                            backend_uid=1001,
                            backend_gid=1001,
                        )
                    ],
                    "brick-c": [
                        _dir_obs(
                            "brick-c",
                            backend,
                            dir_gfid,
                            present=True,
                            logical_path=logical_path,
                            backend_mode_bits=0o644,
                            backend_uid=1002,
                            backend_gid=1002,
                        )
                    ],
                },
                notes=["synthetic directory POSIX metadata no-majority case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_posix_metadata_no_majority", action.action_type)
        self.assertEqual("choose_posix_metadata_source", action.repair_strategy)
        self.assertEqual(sorted(hosts), action.metadata_mismatch_hosts)
        self.assertEqual(["mode", "uid", "gid"], action.metadata_fields_to_align)
        self.assertFalse(getattr(action, "native_heal_first", False))
        self.assertIn("without a strict majority", " ".join(action.notes))

        review_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(review_results))
        review = review_results[0]
        self.assertEqual("review_posix_metadata_no_majority", review.action_type)
        self.assertEqual("review", review.status)
        rendered = render_apply_run(review_results)
        self.assertIn("choice: choose source, keep review, or skip", rendered)
        self.assertNotIn("run Gluster heal/rescan first", rendered)

        decided_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            decisions={logical_path: {"metadata_source_host": "brick-b"}},
        )
        self.assertEqual(1, len(decided_results))
        decided = decided_results[0]
        self.assertEqual("repair_posix_metadata", decided.action_type)
        self.assertEqual("proposed", decided.status)
        self.assertFalse(getattr(decided, "native_heal_first", False))
        self.assertIn("align_posix_metadata_selected_source", "\n".join(decided.notes))
        self.assertIn("operator selected POSIX metadata source host: brick-b", "\n".join(decided.notes))
        self.assertTrue(validate_execute_results(decided_results)[0])
        self.assertEqual({"brick-a", "brick-c"}, {step.host for step in decided.steps})

    def test_file_missing_everywhere_promotes_to_dead_file_ref_cleanup(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/dead-file-ref"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        file_gfid = "dddddddd-eeee-ffff-0000-111111111111"

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

        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={host: [missing_obs(host)] for host in hosts},
                notes=["synthetic dead file ref cleanup case"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("cleanup_dead_file_refs", action.action_type)
        self.assertEqual("delete_dead_file_ref_residue", action.repair_strategy)
        self.assertTrue(action.stale_gfid_paths_by_host)

        results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual("proposed", result.status)
        self.assertIn("remove_stale_gfid", [step.step_type for step in result.steps])
        self.assertIn("dead-file-ref cleanups", render_apply_summary(summarize_apply_results(results)))
        self.assertEqual([], result.backup_artifacts)

        with patch("gluster_heal_tool.executor._run_command") as run_command:
            run_command.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            report = execute_apply_results(results, keep_going=True)

        self.assertEqual("completed", report["actions"][0]["status"])
        self.assertEqual(1, report["summary"]["dead_file_ref_cleanup_actions"])

    def test_dead_file_ref_cleanup_without_residue_falls_back_to_review(self) -> None:
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": "repair:replica-4/dead-file-ref-empty",
                        "logical_path": "replica-4/dead-file-ref-empty",
                        "action_type": "cleanup_dead_file_refs",
                        "object_type": "file",
                        "depth": 2,
                        "repair_strategy": "delete_dead_file_ref_residue",
                        "mounted_target": "/testvol/replica-4/dead-file-ref-empty",
                        "notes": ["synthetic dead file ref cleanup with no residue"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual("review", result.status)
        self.assertIn("review_dead_file_ref_cleanup", [step.step_type for step in result.steps])
        self.assertIn("no residue paths were observed", " ".join(result.notes))

    def test_dead_file_ref_cleanup_with_explicit_residue_paths_backs_up_and_removes(self) -> None:
        results = build_apply_results(
            {
                "actions": [
                    {
                        "action_id": "repair:replica-4/dead-file-ref-backup",
                        "logical_path": "replica-4/dead-file-ref-backup",
                        "action_type": "cleanup_dead_file_refs",
                        "object_type": "file",
                        "depth": 2,
                        "repair_strategy": "delete_dead_file_ref_residue",
                        "mounted_target": "/testvol/replica-4/dead-file-ref-backup",
                        "stale_backends_by_host": {
                            "brick-a": ["/srv/gluster/brick-store/testvol/replica-4/dead-file-ref-backup"]
                        },
                        "stale_file_gfid_paths_by_host": {
                            "brick-a": ["/.glusterfs/dd/dd/dddddddd-eeee-ffff-0000-111111111111"]
                        },
                        "stale_gfid_paths_by_host": {
                            "brick-a": ["/.glusterfs/dd/dd/dddddddd-eeee-ffff-0000-111111111111"]
                        },
                        "notes": ["synthetic dead file ref cleanup with residue paths"],
                    }
                ]
            },
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual("proposed", result.status)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("backup_stale_backend", step_types)
        self.assertIn("remove_stale_backend", step_types)
        self.assertIn("backup_stale_file_gfid", step_types)
        self.assertIn("remove_stale_file_gfid", step_types)
        self.assertIn("remove_stale_gfid", step_types)
        self.assertGreaterEqual(len(result.backup_artifacts), 2)

    def test_symlink_terminal_file_promotes_to_file_repair(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/symlink-terminal-file"
        terminal_path = "/srv/gluster/brick-store/testvol/real-file"
        file_gfid = "99991111-2222-3333-4444-555566667777"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[file_gfid],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "brick-b": [_file_obs("brick-b", backend, file_gfid, present=True, logical_path=logical_path)],
                    "brick-c": [_symlink_file_obs("brick-c", backend, file_gfid, terminal_path=terminal_path, logical_path=logical_path)],
                    "brick-d": [_file_obs("brick-d", backend, file_gfid, present=False, logical_path=logical_path)],
                },
                notes=["synthetic symlink-terminal-file topology", "saw_symlink", "orphaned_symlink_present"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_file", action.action_type)
        self.assertEqual("restore_missing_replica", action.repair_strategy)
        self.assertIn("brick-c", action.healthy_hosts)
        self.assertEqual(["brick-d"], action.missing_hosts)
        self.assertIn("file_subtype:symlink", action.graph_markers)

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
        rendered = render_apply_run(results)
        self.assertNotIn("review_file_metadata", rendered)
        self.assertIn("restore_missing_replica", rendered)

    def test_restore_missing_child_replica_alias_is_supported(self) -> None:
        file_gfid = "eeee1111-ffff-0000-1111-222233334444"
        backend = "/srv/gluster/brick-store/testvol/replica-4/child-gap"
        logical_path = "replica-4/child-gap"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=["brick-a", "brick-b", "brick-c", "brick-d"],
                gfids=[file_gfid],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, file_gfid, present=True, logical_path=logical_path)],
                    "brick-b": [_file_obs("brick-b", backend, file_gfid, present=True, logical_path=logical_path)],
                    "brick-c": [_file_obs("brick-c", backend, file_gfid, present=True, logical_path=logical_path)],
                    "brick-d": [_file_obs("brick-d", backend, file_gfid, present=False, logical_path=logical_path)],
                },
                notes=["synthetic file-child-gap case", "file-child-gap marker"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("repair_file", action.action_type)
        self.assertEqual("restore_missing_child_replica", action.repair_strategy)
        self.assertIn("file_child_gap:present", action.graph_markers)

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
        self.assertTrue(any(step.step_type == "restore_via_mount_child_gap" for step in result.steps))
        rendered = render_apply_run(results)
        self.assertIn("restore_missing_child_replica", rendered)

    def test_split_brain_auto_policy_prefers_majority(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/split-brain-auto"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=120,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=119,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=118,
                        )
                    ],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=200,
                            mounted_error="split-brain-like mount access error",
                        )
                    ],
                },
                notes=["synthetic split-brain auto-policy topology"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("replace_entry_split_brain_file", action.repair_strategy)
        self.assertFalse(getattr(action, "native_heal_first", False))

        auto_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "auto"},
        )
        self.assertEqual(1, len(auto_results))
        auto_result = auto_results[0]
        self.assertEqual("proposed", auto_result.status)
        self.assertEqual("review_entry_split_brain", auto_result.action_type)
        auto_steps = [step.step_type for step in auto_result.steps]
        self.assertIn("mkdir_stage_parent", auto_steps)
        self.assertIn("stage_winner_local", auto_steps)
        self.assertIn("remove_stale_backend", auto_steps)
        self.assertIn("restore_via_mount", auto_steps)
        self.assertIn("configured batch policy: auto", "\n".join(auto_result.notes))

        majority_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "majority"},
        )
        self.assertEqual(1, len(majority_results))
        majority_result = majority_results[0]
        self.assertEqual("proposed", majority_result.status)
        self.assertEqual("review_entry_split_brain", majority_result.action_type)
        self.assertIn("configured batch policy: majority", "\n".join(majority_result.notes))
        majority_steps = [step.step_type for step in majority_result.steps]
        self.assertIn("mkdir_stage_parent", majority_steps)
        self.assertIn("stage_winner_local", majority_steps)
        self.assertIn("remove_stale_backend", majority_steps)
        self.assertIn("restore_via_mount", majority_steps)

    def test_split_brain_quarantine_policy_is_move_only(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/split-brain-quarantine"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=120)],
                    "brick-b": [_file_obs("brick-b", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=119)],
                    "brick-c": [_file_obs("brick-c", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=118)],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=200,
                            mounted_error="split-brain-like mount access error",
                        )
                    ],
                },
                notes=["synthetic split-brain quarantine topology"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        action = plan[0]

        quarantine_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "quarantine"},
        )
        self.assertEqual(1, len(quarantine_results))
        quarantine_result = quarantine_results[0]
        self.assertEqual("proposed", quarantine_result.status)
        self.assertEqual("review_entry_split_brain", quarantine_result.action_type)
        self.assertFalse(getattr(quarantine_result, "native_heal_first", False))
        quarantine_steps = [step.step_type for step in quarantine_result.steps]
        self.assertIn("quarantine_file_backend", quarantine_steps)
        self.assertNotIn("stage_winner_local", quarantine_steps)
        self.assertIn("quarantine is move-only", "\n".join(quarantine_result.notes))

    def test_split_brain_auto_policy_quarantines_large_files_instead_of_staging(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/split-brain-auto-large"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        large_size = 32 * 1024 * 1024
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=120, backend_size=large_size)],
                    "brick-b": [_file_obs("brick-b", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=119, backend_size=large_size)],
                    "brick-c": [_file_obs("brick-c", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=118, backend_size=large_size)],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=200,
                            backend_size=large_size,
                            mounted_error="split-brain-like mount access error",
                        )
                    ],
                },
                notes=["synthetic split-brain large-file topology"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        action = plan[0]

        auto_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "auto"},
        )
        self.assertEqual(1, len(auto_results))
        auto_result = auto_results[0]
        self.assertEqual("proposed", auto_result.status)
        self.assertEqual("review_entry_split_brain", auto_result.action_type)
        auto_steps = [step.step_type for step in auto_result.steps]
        self.assertIn("quarantine_file_backend", auto_steps)
        self.assertIn("restore_file_backend_gap_fill", auto_steps)
        self.assertNotIn("stage_winner_local", auto_steps)
        self.assertIn("split-brain recovery mode", "\n".join(auto_result.notes))
        self.assertIn("quarantine_loser", "\n".join(auto_result.notes))
        gap_fill_step = next(step for step in auto_result.steps if step.step_type == "restore_file_backend_gap_fill")
        self.assertEqual("ssh", gap_fill_step.command_preview[0])
        self.assertIn("rsync-pull", " ".join(gap_fill_step.command_preview))
        self.assertIn("gluster-repair@brick-d", " ".join(gap_fill_step.command_preview))
        self.assertIn("rsync-pull brick-a", gap_fill_step.command_preview[-1])
        self.assertEqual("brick-a", gap_fill_step.source_host)

    def test_arbiter_identity_source_copy_clears_marker_only_after_backfill(self) -> None:
        logical_path = "arbiter-conflict/payload.txt"
        action = {
            "action_id": f"review:/{logical_path}",
            "logical_path": logical_path,
            "action_type": "review_entry_split_brain",
            "object_type": "file",
            "repair_strategy": "arbiter_backed_data_identity_conflict",
            "mounted_target": f"/testvol/{logical_path}",
            "winner_host": "data-a",
            "winner_backend": f"/bricks/a/{logical_path}",
            "winner_file_gfid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "brick_roles_by_host": {"data-a": "data", "data-b": "data", "arbiter": "arbiter"},
            "brick_role_evidence_required": True,
            "file_copies": [
                {
                    "host": "data-a",
                    "backend": f"/bricks/a/{logical_path}",
                    "identity": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "file_gfid_path": "/bricks/a/.glusterfs/aa/aa/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
                {
                    "host": "data-b",
                    "backend": f"/bricks/b/{logical_path}",
                    "identity": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                    "file_gfid_path": "/bricks/b/.glusterfs/bb/bb/bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                },
            ],
        }

        results = build_apply_results(
            {"actions": [action]},
            execution_mode="dry-run",
            backup_mode="required",
            decisions={logical_path: {"file_choice": "quarantine_loser"}},
            volume="testvol",
            brick_path="/bricks/a",
        )

        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("proposed", result.status)
        self.assertEqual(
            [
                "quarantine_file_backend",
                "quarantine_file_gfid",
                "restore_file_backend_gap_fill",
                "attach_file_gfid",
                "resolve_split_brain_gluster_cli",
            ],
            [step.step_type for step in result.steps],
        )
        resolver_step = result.steps[-1]
        self.assertEqual("data-a", resolver_step.host)
        self.assertIn("source-brick", resolver_step.command_preview)
        self.assertIn("data-a:/bricks/a", resolver_step.command_preview)
        self.assertIn("does not choose the source", " ".join(resolver_step.notes))
        self.assertNotIn("arbiter", resolver_step.command_preview)

    def test_split_brain_policy_modes_are_covered(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/split-brain-policy-modes"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"

        manifest_majority = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, gfid_a, present=True, logical_path=logical_path)],
                    "brick-b": [_file_obs("brick-b", backend, gfid_a, present=True, logical_path=logical_path)],
                    "brick-c": [_file_obs("brick-c", backend, gfid_a, present=True, logical_path=logical_path)],
                    "brick-d": [_file_obs("brick-d", backend, gfid_b, present=True, logical_path=logical_path)],
                },
                notes=["synthetic split-brain policy coverage majority"],
            )
        }
        plan_majority = build_plan(manifest_majority, mountpoint="/testvol")
        action_majority = plan_majority[0]

        replace_results = build_apply_results(
            {"actions": [action_majority.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "replace"},
        )
        self.assertEqual("planned", replace_results[0].status)
        self.assertEqual("repair_file", replace_results[0].action_type)
        self.assertIn("repair strategy: replace_conflicting_file", "\n".join(replace_results[0].notes))

        # explicit mtime / size / ctime coverage uses the tie-shaped topology
        manifest_tie = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=110, backend_size=10)],
                    "brick-b": [_file_obs("brick-b", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=105, backend_size=10)],
                    "brick-c": [_file_obs("brick-c", backend, gfid_b, present=True, logical_path=logical_path, backend_mtime=220, backend_size=50)],
                    "brick-d": [_file_obs("brick-d", backend, gfid_b, present=True, logical_path=logical_path, backend_mtime=150, backend_size=50)],
                },
                notes=["synthetic split-brain policy coverage tie"],
            )
        }
        plan_tie = build_plan(manifest_tie, mountpoint="/testvol")
        action_tie = plan_tie[0]

        review_results = build_apply_results(
            {"actions": [action_tie.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "review"},
        )
        self.assertEqual("planned", review_results[0].status)
        self.assertEqual("repair_file", review_results[0].action_type)
        self.assertIn("repair strategy: replace_conflicting_file", "\n".join(review_results[0].notes))

        off_results = build_apply_results(
            {"actions": [action_tie.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "off"},
        )
        self.assertEqual("planned", off_results[0].status)
        self.assertEqual("repair_file", off_results[0].action_type)

        skip_results = build_apply_results(
            {"actions": [action_tie.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "skip"},
        )
        self.assertEqual("planned", skip_results[0].status)
        self.assertEqual("repair_file", skip_results[0].action_type)

        mtime_results = build_apply_results(
            {"actions": [action_tie.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "mtime"},
        )
        self.assertEqual("planned", mtime_results[0].status)
        self.assertEqual("repair_file", mtime_results[0].action_type)

        size_results = build_apply_results(
            {"actions": [action_tie.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "size"},
        )
        self.assertEqual("planned", size_results[0].status)
        self.assertEqual("repair_file", size_results[0].action_type)

        ctime_results = build_apply_results(
            {"actions": [action_tie.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "ctime"},
        )
        self.assertEqual("planned", ctime_results[0].status)
        self.assertEqual("repair_file", ctime_results[0].action_type)

    def test_split_brain_native_policy_selects_native_command_shape(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/split-brain-native-policy"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [_file_obs("brick-a", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=120)],
                    "brick-b": [_file_obs("brick-b", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=119)],
                    "brick-c": [_file_obs("brick-c", backend, gfid_a, present=True, logical_path=logical_path, backend_mtime=118)],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=200,
                            mounted_error="split-brain-like mount access error",
                        )
                    ],
                },
                notes=["synthetic split-brain native policy topology"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        action = plan[0]

        auto_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "replace"},
            split_brain_native_policy="auto",
            volume="testvol",
            brick_path="/srv/gluster/brick-store/testvol",
            worker_path="/does/not/exist",
        )
        self.assertEqual(
            [
                "sudo",
                "-n",
                "gluster",
                "volume",
                "heal",
                "testvol",
                "split-brain",
                "latest-mtime",
                f"/{logical_path}",
            ],
            auto_results[0].steps[0].command_preview,
        )
        self.assertEqual("resolve_split_brain_gluster_cli", auto_results[0].steps[1].step_type)
        self.assertIn("source-brick", auto_results[0].steps[1].command_preview)
        self.assertIn("native split-brain policy: auto", "\n".join(auto_results[0].notes))
        self.assertIn(
            "native split-brain policy auto: latest-mtime on the official Gluster resolver; if that ties, checksum the candidate copies before any source-brick follow-up",
            "\n".join(auto_results[0].notes),
        )
        self.assertIn(
            "native split-brain policy auto checksum tie: checksum worker unavailable in this environment; retaining legacy source-brick follow-up",
            "\n".join(auto_results[0].notes),
        )

        with tempfile.NamedTemporaryFile() as worker_file:
            with patch(
                "gluster_heal_tool.apply_planning_file.build_split_brain_file_checksum_report",
                return_value={
                    "outcome": "content-equal",
                    "reason": "tied copies have identical checksums",
                },
            ):
                checksum_equal_results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                    review_policies={"entry_split_brain_file": "replace"},
                    split_brain_native_policy="auto",
                    volume="testvol",
                    brick_path="/srv/gluster/brick-store/testvol",
                    worker_path=worker_file.name,
                )
        self.assertEqual("proposed", checksum_equal_results[0].status)
        self.assertIn("source-brick", checksum_equal_results[0].steps[1].command_preview)
        self.assertIn(
            "candidate copies matched; source-brick follow-up remains safe",
            "\n".join(checksum_equal_results[0].notes),
        )

        with tempfile.NamedTemporaryFile() as worker_file:
            with patch(
                "gluster_heal_tool.apply_planning_file.build_split_brain_file_checksum_report",
                return_value={
                    "outcome": "content-different",
                    "reason": "tied copies have different checksums",
                },
            ):
                checksum_diff_results = build_apply_results(
                    {"actions": [action.to_dict()]},
                    execution_mode="dry-run",
                    backup_mode="required",
                    batch=True,
                    review_policies={"entry_split_brain_file": "replace"},
                    split_brain_native_policy="auto",
                    volume="testvol",
                    brick_path="/srv/gluster/brick-store/testvol",
                    worker_path=worker_file.name,
                )
        self.assertEqual("proposed", checksum_diff_results[0].status)
        self.assertEqual("review_entry_split_brain", checksum_diff_results[0].action_type)
        self.assertIn("review_ambiguous_entry_split_brain", [step.step_type for step in checksum_diff_results[0].steps])
        self.assertNotIn("restore_via_mount", [step.step_type for step in checksum_diff_results[0].steps])
        self.assertIn("keep review-only or quarantine", "\n".join(checksum_diff_results[0].notes))

        mtime_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "replace"},
            split_brain_native_policy="latest-mtime",
            volume="testvol",
            brick_path="/srv/gluster/brick-store/testvol",
        )
        self.assertEqual(
            [
                "sudo",
                "-n",
                "gluster",
                "volume",
                "heal",
                "testvol",
                "split-brain",
                "latest-mtime",
                f"/{logical_path}",
            ],
            mtime_results[0].steps[0].command_preview,
        )

        bigger_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "replace"},
            split_brain_native_policy="bigger-file",
            volume="testvol",
            brick_path="/srv/gluster/brick-store/testvol",
        )
        self.assertEqual(
            [
                "sudo",
                "-n",
                "gluster",
                "volume",
                "heal",
                "testvol",
                "split-brain",
                "bigger-file",
                f"/{logical_path}",
            ],
            bigger_results[0].steps[0].command_preview,
        )

        explicit_action = action.to_dict()
        explicit_action["winner_host"] = "brick-a"
        explicit_action["winner_backend"] = backend

        source_only_results = build_apply_results(
            {"actions": [explicit_action]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "replace"},
            split_brain_native_policy="source-brick",
            volume="testvol",
            brick_path="/srv/gluster/brick-store/testvol",
        )
        self.assertEqual(
            [
                "sudo",
                "-n",
                "gluster",
                "volume",
                "heal",
                "testvol",
                "split-brain",
                "source-brick",
                "brick-a:/srv/gluster/brick-store/testvol",
                f"/{logical_path}",
            ],
            source_only_results[0].steps[0].command_preview,
        )

        missing_source_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "replace"},
            split_brain_native_policy="source-brick",
            volume="testvol",
            brick_path="",
        )
        self.assertNotIn("resolve_split_brain_gluster_cli", [step.step_type for step in missing_source_results[0].steps])
        self.assertIn(
            "native split-brain policy source-brick requested, but no explicit source brick is known",
            "\n".join(missing_source_results[0].notes),
        )

    def test_split_brain_auto_policy_uses_mtime_when_no_majority(self) -> None:
        hosts = ("brick-a", "brick-b", "brick-c", "brick-d")
        logical_path = "replica-4/split-brain-auto-mtime"
        backend = f"/srv/gluster/brick-store/testvol/{logical_path}"
        gfid_a = "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"
        gfid_b = "bbbb1111-cccc-dddd-eeee-ffff00001111"
        manifest = {
            logical_path: ManifestObject(
                logical_path=logical_path,
                object_type="file",
                depth=2,
                raw_entries=[logical_path],
                source_hosts=list(hosts),
                gfids=[gfid_a, gfid_b],
                observations={
                    "brick-a": [
                        _file_obs(
                            "brick-a",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=110,
                        )
                    ],
                    "brick-b": [
                        _file_obs(
                            "brick-b",
                            backend,
                            gfid_a,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=105,
                        )
                    ],
                    "brick-c": [
                        _file_obs(
                            "brick-c",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=220,
                            mounted_error="split-brain-like mount access error",
                        )
                    ],
                    "brick-d": [
                        _file_obs(
                            "brick-d",
                            backend,
                            gfid_b,
                            present=True,
                            logical_path=logical_path,
                            backend_mtime=150,
                        )
                    ],
                },
                notes=["synthetic split-brain auto-policy mtime topology"],
            )
        }

        plan = build_plan(manifest, mountpoint="/testvol")
        self.assertEqual(1, len(plan))
        action = plan[0]
        self.assertEqual("review_entry_split_brain", action.action_type)
        self.assertEqual("ambiguous_entry_split_brain_file", action.repair_strategy)

        auto_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "auto"},
        )
        self.assertEqual(1, len(auto_results))
        auto_result = auto_results[0]
        self.assertEqual("proposed", auto_result.status)
        self.assertEqual("review_entry_split_brain", auto_result.action_type)
        auto_steps = [step.step_type for step in auto_result.steps]
        self.assertIn("mkdir_stage_parent", auto_steps)
        self.assertIn("stage_winner_local", auto_steps)
        self.assertIn("remove_stale_backend", auto_steps)
        self.assertIn("restore_via_mount", auto_steps)
        self.assertIn("configured batch policy: auto", "\n".join(auto_result.notes))

        majority_results = build_apply_results(
            {"actions": [action.to_dict()]},
            execution_mode="dry-run",
            backup_mode="required",
            batch=True,
            review_policies={"entry_split_brain_file": "majority"},
        )
        self.assertEqual(1, len(majority_results))
        majority_result = majority_results[0]
        self.assertEqual("review", majority_result.status)
        self.assertEqual("review_entry_split_brain", majority_result.action_type)
        self.assertIn("review_ambiguous_entry_split_brain", [step.step_type for step in majority_result.steps])
