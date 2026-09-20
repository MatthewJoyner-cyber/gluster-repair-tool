# SPDX-License-Identifier: GPL-2.0-only
"""Tests for batched canary worker operations."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call
from unittest.mock import patch

import gluster_heal_tool.canary_file as canary_file
from gluster_heal_tool.canary import _build_cleanup_result
from gluster_heal_tool.canary import build_parser
from gluster_heal_tool.canary import main
from gluster_heal_tool.canary import cleanup_ctime_review_chain_canary
from gluster_heal_tool.canary import cleanup_canary
from gluster_heal_tool.canary import cleanup_canary_wave
from gluster_heal_tool.canary import create_child_gap_canary
from gluster_heal_tool.canary import create_directory_backend_child_gap_canary
from gluster_heal_tool.canary import create_directory_backend_child_gap_bounded_canary
from gluster_heal_tool.canary import create_directory_backend_child_gap_canonical_canary
from gluster_heal_tool.canary import create_directory_backend_child_gap_mixed_canary
from gluster_heal_tool.canary import create_directory_child_gap_canary
from gluster_heal_tool.canary import create_directory_child_reference_canary
from gluster_heal_tool.canary_directory import build_directory_backend_child_gap_plan_from_canary_state
from gluster_heal_tool.canary_directory import build_directory_mdata_no_majority_plan_from_canary_state
from gluster_heal_tool.canary_directory import build_type_mismatch_plan_from_canary_state
from gluster_heal_tool.canary import create_directory_gfid_mask_canary
from gluster_heal_tool.canary import create_directory_gfid_merge_canary
from gluster_heal_tool.canary import create_type_mismatch_canary
from gluster_heal_tool.canary import create_directory_gfid_tie_canary
from gluster_heal_tool.canary import create_file_handle_ghost_canary
from gluster_heal_tool.canary import create_file_child_gap_canary
from gluster_heal_tool.canary import create_file_metadata_split_brain_canary
from gluster_heal_tool.canary import create_file_content_split_brain_canary
from gluster_heal_tool.canary import create_directory_metadata_canary
from gluster_heal_tool.canary import create_directory_mdata_no_majority_canary
from gluster_heal_tool.canary import create_directory_stale_survivor_canary
from gluster_heal_tool.canary import create_ctime_review_chain_canary
from gluster_heal_tool.canary import create_file_ctime_metadata_canary
from gluster_heal_tool.canary import create_file_child_gap_multi_canary
from gluster_heal_tool.canary import create_file_posix_metadata_canary
from gluster_heal_tool.canary import create_file_posix_acl_majority_canary
from gluster_heal_tool.canary import create_file_native_pending_metadata_canary
from gluster_heal_tool.canary import create_directory_posix_metadata_canary
from gluster_heal_tool.canary import create_directory_posix_default_acl_majority_canary
from gluster_heal_tool.canary import create_missing_file_replica_canary
from gluster_heal_tool.canary import create_file_stale_survivor_canary
from gluster_heal_tool.canary import create_file_stale_survivor_pair_canary
from gluster_heal_tool.canary import create_symlink_brick_outage_canary
from gluster_heal_tool.canary import create_symlink_missing_stale_canary
from gluster_heal_tool.canary import observe_directory_child_state_canary
from gluster_heal_tool.canary import observe_directory_gfid_state_canary
from gluster_heal_tool.canary import observe_file_state_canary
from gluster_heal_tool.controller_paths import default_canary_stage_local_path
from gluster_heal_tool.controller_paths import default_heal_info_root
from gluster_heal_tool.controller_paths import default_temp_mount_root
from gluster_heal_tool.canary_file import build_file_child_gap_plan_from_canary_state
from gluster_heal_tool.canary_file import build_file_metadata_split_brain_plan_from_canary_state
from gluster_heal_tool.canary_file import build_file_posix_metadata_split_brain_plan_from_canary_state
from gluster_heal_tool.apply import build_apply_results
from gluster_heal_tool.apply import render_apply_run
from gluster_heal_tool.protocol import CanaryBatchRequest, ResolveBatchRequest
from gluster_heal_tool.worker import _posix_acl_hex
from gluster_heal_tool.worker import _resolve_one
from gluster_heal_tool.worker import _run_canary_op
from gluster_heal_tool.worker import _trusted_gfid


def _assert_canary_state_provenance(
    test_case: TestCase,
    plan: dict[str, object],
    *,
    kind: str,
    volume: str,
    scenario: str,
) -> None:
    test_case.assertEqual("canary_state", plan["input_source"])
    test_case.assertEqual("harness-only", plan["proof_scope"])
    test_case.assertEqual(
        {"kind": kind, "volume": volume, "scenario": scenario},
        plan["canary"],
    )


class CanaryWorkerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for target in (
            "gluster_heal_tool.canary_file._ensure_canary_mount",
            "gluster_heal_tool.canary_directory._ensure_canary_mount",
            "gluster_heal_tool.canary_file.run_heal",
            "gluster_heal_tool.canary_file.set_heal_settings",
            "gluster_heal_tool.canary_file._require_posix_canary_readiness",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_posix_canary_refuses_before_any_volume_mutation_when_unready(self) -> None:
        with patch(
            "gluster_heal_tool.canary_file._require_posix_canary_readiness",
            side_effect=RuntimeError("POSIX canary readiness blocked before mutation: missing SHD"),
        ), patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots") as roots, patch(
            "gluster_heal_tool.canary_file._run_local"
        ) as run_local:
            roots.return_value = (["host-a", "host-b", "host-c"], {"host-a": "/a", "host-b": "/b", "host-c": "/c"})
            with self.assertRaisesRegex(RuntimeError, "readiness blocked"):
                create_file_posix_metadata_canary(
                    volume="testvol",
                    scenario="readiness-gate",
                    dir_name="alpha",
                    file_name="payload.txt",
                )

        run_local.assert_not_called()

    def test_canary_parser_exposes_matrix_representative_commands(self) -> None:
        parser = build_parser()
        subparsers_action = next(action for action in parser._actions if getattr(action, "choices", None))

        self.assertIn("create-missing-file-replica", subparsers_action.choices)
        self.assertIn("create-arbiter-missing-gfid", subparsers_action.choices)
        self.assertIn("create-arbiter-only-residue", subparsers_action.choices)
        arbiter_gfid_args = parser.parse_args(
            ["create-arbiter-missing-gfid", "--object-type", "directory"]
        )
        self.assertEqual("directory", arbiter_gfid_args.object_type)
        pending_args = parser.parse_args(
            ["create-missing-file-replica", "--leave-heal-pending"]
        )
        self.assertTrue(pending_args.leave_heal_pending)
        child_gap_pending_args = parser.parse_args(
            ["create-file-child-gap", "--leave-heal-pending"]
        )
        self.assertTrue(child_gap_pending_args.leave_heal_pending)
        directory_gap_pending_args = parser.parse_args(
            ["create-directory-backend-child-gap", "--leave-heal-pending"]
        )
        self.assertTrue(directory_gap_pending_args.leave_heal_pending)
        shallow_directory_gap_pending_args = parser.parse_args(
            ["create-directory-child-gap", "--leave-heal-pending"]
        )
        self.assertTrue(shallow_directory_gap_pending_args.leave_heal_pending)
        symlink_pending_args = parser.parse_args(
            ["create-symlink-missing-stale", "--leave-heal-pending"]
        )
        self.assertTrue(symlink_pending_args.leave_heal_pending)
        handle_ghost_pending_args = parser.parse_args(
            ["create-file-handle-ghost", "--leave-heal-pending"]
        )
        self.assertTrue(handle_ghost_pending_args.leave_heal_pending)
        orphaned_gfid_pending_args = parser.parse_args(
            ["create-orphaned-gfid-hardlink", "--leave-heal-pending"]
        )
        self.assertTrue(orphaned_gfid_pending_args.leave_heal_pending)
        metadata_split_pending_args = parser.parse_args(
            ["create-file-metadata-split-brain", "--leave-heal-pending"]
        )
        self.assertTrue(metadata_split_pending_args.leave_heal_pending)
        posix_split_pending_args = parser.parse_args(
            ["create-file-posix-metadata-split-brain", "--leave-heal-pending"]
        )
        self.assertTrue(posix_split_pending_args.leave_heal_pending)
        self.assertIn("create-file-child-gap", subparsers_action.choices)
        self.assertIn("create-file-child-gap-multi", subparsers_action.choices)
        self.assertIn("file-child-gap-build", subparsers_action.choices)
        self.assertIn("file-child-gap-multi-build", subparsers_action.choices)
        self.assertIn("create-file-handle-ghost", subparsers_action.choices)
        self.assertIn("create-orphaned-gfid-hardlink", subparsers_action.choices)
        self.assertIn("create-file-stale-survivor", subparsers_action.choices)
        self.assertIn("create-file-stale-survivor-pair", subparsers_action.choices)
        self.assertIn("create-file-data-split-brain", subparsers_action.choices)
        self.assertIn("create-file-content-split-brain", subparsers_action.choices)
        self.assertIn("create-file-posix-metadata", subparsers_action.choices)
        self.assertIn("create-file-posix-metadata-split-brain", subparsers_action.choices)
        self.assertIn("create-file-native-pending-metadata", subparsers_action.choices)
        self.assertIn("create-afr-metadata-split-brain", subparsers_action.choices)
        self.assertIn("create-zero-afr-native-heal-smoke", subparsers_action.choices)
        self.assertIn("create-directory-posix-metadata", subparsers_action.choices)
        self.assertIn("create-file-metadata-split-brain", subparsers_action.choices)
        self.assertIn("create-symlink-missing-stale", subparsers_action.choices)
        self.assertIn("create-directory-child-reference", subparsers_action.choices)
        self.assertIn("create-directory-child-gap", subparsers_action.choices)
        self.assertIn("create-directory-backend-child-gap", subparsers_action.choices)
        self.assertIn("create-directory-backend-child-gap-mixed", subparsers_action.choices)
        self.assertIn("create-directory-backend-child-gap-bounded", subparsers_action.choices)
        self.assertIn("create-directory-backend-child-gap-canonical", subparsers_action.choices)
        self.assertIn("directory-backend-child-gap-mixed-build", subparsers_action.choices)
        self.assertIn("create-directory-gfid-merge", subparsers_action.choices)
        self.assertIn("create-directory-gfid-tie", subparsers_action.choices)
        self.assertIn("create-directory-stale-survivor", subparsers_action.choices)
        self.assertIn("create-type-mismatch", subparsers_action.choices)
        self.assertIn("type-mismatch-build", subparsers_action.choices)
        self.assertIn("observe-directory-child-state", subparsers_action.choices)
        self.assertIn("observe-directory-gfid-state", subparsers_action.choices)
        self.assertIn("observe-file-state", subparsers_action.choices)
        self.assertIn("create-directory-metadata", subparsers_action.choices)
        self.assertIn("create-directory-mdata-no-majority", subparsers_action.choices)
        self.assertIn("directory-mdata-no-majority-build", subparsers_action.choices)
        self.assertIn("create-file-ctime-metadata", subparsers_action.choices)
        self.assertIn("create-ctime-review-chain", subparsers_action.choices)
        self.assertIn("create-file-gfid-split", subparsers_action.choices)
        self.assertIn("file-metadata-split-brain-build", subparsers_action.choices)
        self.assertIn("file-posix-metadata-split-brain-build", subparsers_action.choices)
        self.assertIn("create-symlink-brick-outage", subparsers_action.choices)
        self.assertIn("cleanup-ctime-review-chain", subparsers_action.choices)
        self.assertIn("cleanup-wave", subparsers_action.choices)

    def test_canary_parser_defaults_canary_volume_to_gtest4(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["create-symlink-missing-stale"])

        self.assertEqual("gtest4", args.volume)

    def test_symlink_pending_mode_reaches_canary_without_heal_crawl(self) -> None:
        with patch("gluster_heal_tool.canary.create_symlink_missing_stale_canary") as create_symlink:
            result = main(
                [
                    "create-symlink-missing-stale",
                    "--volume",
                    "gtest3",
                    "--name",
                    "pending-symlink",
                    "--leave-heal-pending",
                ]
            )

        self.assertEqual(0, result)
        self.assertFalse(create_symlink.call_args.kwargs["trigger_heal"])

    def test_file_handle_ghost_pending_mode_reaches_canary_without_heal_crawl(self) -> None:
        with patch("gluster_heal_tool.canary.create_file_handle_ghost_canary") as create_handle_ghost:
            result = main(
                [
                    "create-file-handle-ghost",
                    "--volume",
                    "gtest3",
                    "--name",
                    "pending-handle-ghost",
                    "--leave-heal-pending",
                ]
            )

        self.assertEqual(0, result)
        self.assertFalse(create_handle_ghost.call_args.kwargs["trigger_heal"])

    def test_file_metadata_split_pending_mode_reaches_canary_without_heal_crawl(self) -> None:
        with patch("gluster_heal_tool.canary.create_file_metadata_split_brain_canary") as create_metadata_split:
            result = main(
                [
                    "create-file-metadata-split-brain",
                    "--volume",
                    "gtest4",
                    "--name",
                    "pending-metadata-split",
                    "--leave-heal-pending",
                ]
            )

        self.assertEqual(0, result)
        self.assertFalse(create_metadata_split.call_args.kwargs["trigger_heal"])

    def test_orphaned_gfid_pending_mode_reaches_canary_without_heal_crawl(self) -> None:
        with patch("gluster_heal_tool.canary.create_orphaned_gfid_hardlink_canary") as create_orphaned_gfid:
            result = main(
                [
                    "create-orphaned-gfid-hardlink",
                    "--volume",
                    "gtest3",
                    "--name",
                    "pending-orphaned-gfid",
                    "--leave-heal-pending",
                ]
            )

        self.assertEqual(0, result)
        self.assertFalse(create_orphaned_gfid.call_args.kwargs["trigger_heal"])

    def test_file_posix_metadata_canary_is_exposed(self) -> None:
        with (
            patch(
                "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
                return_value=(
                    ["node-a", "node-b", "node-c"],
                    {
                        "node-a": "/gluster/gtest3/gtest3/brick",
                        "node-b": "/gluster/gtest3/gtest3/brick",
                        "node-c": "/gluster/gtest3/gtest3/brick",
                    },
                ),
            ),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local") as run_local,
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before", "heal info after"]),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops") as host_ops,
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch(
                "gluster_heal_tool.canary_file._canary_worker_command",
                return_value={
                    "host": "node-a",
                    "results": [{"op": "inspect_path", "ok": True, "mode_bits": 420, "uid": 1000, "gid": 1000}],
                },
            ),
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
        ):
            create_file_posix_metadata_canary(
                volume="gtest3",
                scenario="repair-canary-posix",
                mismatch_host="node-c",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual("native-transaction", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("native-heal-first", write_state.call_args.args[2]["proof_label"])
        self.assertEqual("heal info before", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("heal info after", write_state.call_args.args[2]["heal_info_after"])
        self.assertEqual(
            {"node-a": [], "node-b": []},
            write_state.call_args.args[2]["afr_pending_xattrs_by_host"],
        )
        self.assertTrue(any(call.args and call.args[0][:3] == ["sudo", "-n", "chown"] for call in run_local.call_args_list))
        self.assertTrue(any(call.args and call.args[0][:3] == ["sudo", "-n", "chmod"] for call in run_local.call_args_list))
        self.assertEqual(2, host_ops.call_count)
        self.assertEqual(
            ("node-c", ["brick-down", "--volume", "gtest3", "--brick", "/gluster/gtest3/gtest3/brick"]),
            host_ops.call_args_list[0].args[:2],
        )
        self.assertEqual(
            ("node-c", ["brick-kick", "--volume", "gtest3"]),
            host_ops.call_args_list[1].args[:2],
        )
        canary_file.run_heal.assert_called_once_with("gtest3")

    def test_file_native_pending_metadata_canary_uses_mount_transaction(self) -> None:
        calls: list[list[dict[str, object]]] = []

        def worker(host, **kwargs):  # noqa: ANN001
            calls.append(kwargs["request"].ops)
            return {"host": host, "results": [{"op": "inspect_path", "ok": True, "mode_bits": 420, "uid": 1000, "gid": 1000}]}

        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(
                ["node-a", "node-b", "node-c"],
                {"node-a": "/gluster/gtest3b/gtest3/brick", "node-b": "/gluster/gtest3b/gtest3/brick", "node-c": "/gluster/gtest3b/gtest3/brick"},
            )),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local") as run_local,
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before", "heal info after"]),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops"),
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker),
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
        ):
            create_file_native_pending_metadata_canary(
                volume="gtest3",
                scenario="repair-canary-native-pending",
                mismatch_host="node-c",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertTrue(any(call.args and call.args[0][:3] == ["sudo", "-n", "chmod"] for call in run_local.call_args_list))
        self.assertEqual("native-transaction", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("native-heal-first", write_state.call_args.args[2]["proof_label"])
        self.assertEqual("heal info before", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("heal info after", write_state.call_args.args[2]["heal_info_after"])
        self.assertEqual(
            {"node-a": [], "node-b": []},
            write_state.call_args.args[2]["afr_pending_xattrs_by_host"],
        )
        canary_file.run_heal.assert_called_once_with("gtest3")

    def test_file_native_pending_metadata_canary_continues_after_transport_endpoint_disconnect(self) -> None:
        def worker(host, **kwargs):  # noqa: ANN001
            return {"host": host, "results": [{"op": "inspect_path", "ok": True, "mode_bits": 420, "uid": 1000, "gid": 1000}]}

        def local_run(command):  # noqa: ANN001
            if command[:3] == ["sudo", "-n", "chmod"]:
                raise RuntimeError("chmod: cannot access /tmp/gluster-example/gluster-repair/repair-canary/gtest3/repair-canary-native-pending/alpha/payload.txt: Transport endpoint is not connected")

        with (
            patch(
                "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
                return_value=(
                    ["node-a", "node-b", "node-c"],
                    {"node-a": "/gluster/gtest3b/gtest3/brick", "node-b": "/gluster/gtest3b/gtest3/brick", "node-c": "/gluster/gtest3b/gtest3/brick"},
                ),
            ),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local") as run_local,
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before", "heal info after"]),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops"),
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker),
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
        ):
            run_local.side_effect = local_run
            create_file_native_pending_metadata_canary(
                volume="gtest3",
                scenario="repair-canary-native-pending",
                mismatch_host="node-c",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertTrue(any(call.args and call.args[0][:3] == ["sudo", "-n", "chmod"] for call in run_local.call_args_list))
        self.assertEqual("native-transaction", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("native-heal-first", write_state.call_args.args[2]["proof_label"])
        self.assertEqual("heal info before", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("heal info after", write_state.call_args.args[2]["heal_info_after"])
        canary_file.run_heal.assert_called_once_with("gtest3")

    def test_file_posix_metadata_split_brain_plan_bridge_exposes_native_source_brick(self) -> None:
        state = {
            "kind": "file-posix-metadata-split-brain",
            "mount_root": "/testvol",
            "mount_file": "/testvol/repair-canary-posix/alpha/payload.txt",
            "metadata_tuple_by_host": {
                "brick-a": {"mode_bits": 420, "uid": 1000, "gid": 1000, "acl_access": "user::rw-", "acl_default": ""},
                "brick-b": {"mode_bits": 384, "uid": 1000, "gid": 1000, "acl_access": "user::r--", "acl_default": ""},
                "brick-c": {"mode_bits": 256, "uid": 1000, "gid": 1000, "acl_access": "user::r--", "acl_default": ""},
                "brick-d": {"mode_bits": 420, "uid": 1000, "gid": 1000, "acl_access": "user::rw-", "acl_default": ""},
            },
            "metadata_backend_by_host": {
                "brick-a": "/brick/a/repair-canary-posix/alpha/payload.txt",
                "brick-b": "/brick/b/repair-canary-posix/alpha/payload.txt",
                "brick-c": "/brick/c/repair-canary-posix/alpha/payload.txt",
                "brick-d": "/brick/d/repair-canary-posix/alpha/payload.txt",
            },
            "metadata_fields_to_align": ["mode", "uid", "gid", "acl_access"],
            "metadata_pending_value": "000000000000000100000000",
            "afr_pending_xattrs_by_host": {
                "brick-a": [{"name": "trusted.afr.testvol-client-0"}],
                "brick-b": [{"name": "trusted.afr.testvol-client-1"}],
                "brick-c": [{"name": "trusted.afr.testvol-client-2"}],
                "brick-d": [{"name": "trusted.afr.testvol-client-3"}],
            },
            "heal_info_before": "heal info before\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_split_brain_before": "split before",
            "heal_info_after": "heal info after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_split_brain_after": "split after",
            "gluster_visible_metadata_split_brain": True,
            "brick_roles_by_host": {"brick-a": "data", "brick-b": "data", "brick-c": "arbiter", "brick-d": "data"},
            "metadata_source_reason": "native_gluster_source",
        }
        with patch("gluster_heal_tool.canary_file._read_state", return_value=state):
            plan = build_file_posix_metadata_split_brain_plan_from_canary_state(volume="testvol", scenario="repair-canary-posix")

        _assert_canary_state_provenance(
            self,
            plan,
            kind="file-posix-metadata-split-brain",
            volume="testvol",
            scenario="repair-canary-posix",
        )
        self.assertEqual(1, len(plan["actions"]))
        action = plan["actions"][0]
        self.assertEqual("review_posix_metadata_no_majority", action["action_type"])
        self.assertEqual("choose_posix_metadata_source", action["repair_strategy"])
        self.assertTrue(action["gluster_visible_metadata_split_brain"])
        self.assertEqual("native_gluster_source", action["metadata_source_reason"])
        self.assertTrue(any("native resolution mode: source-brick after source selection" in note for note in action["notes"]))
        self.assertTrue(any("POSIX metadata by host:" in note for note in action["notes"]))
        self.assertEqual({"brick-a": "data", "brick-b": "data", "brick-c": "arbiter", "brick-d": "data"}, action["brick_roles_by_host"])
        self.assertTrue(any("brick roles by host:" in note for note in action["notes"]))
        self.assertTrue(any("brick-c: arbiter" in note for note in action["notes"]))
        results = build_apply_results(plan, execution_mode="dry-run", backup_mode="required", batch=True)
        rendered = render_apply_run(results)
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("brick-c: arbiter", rendered)

    def test_file_posix_metadata_split_brain_plan_bridge_rejects_direct_x4_fixture(self) -> None:
        state = {
            "kind": "file-posix-metadata-split-brain",
            "fixture_scope": "x4-direct-bookkeeping-diagnostic",
            "gluster_visible_metadata_split_brain": True,
        }

        with patch("gluster_heal_tool.canary_file._read_state", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "direct replica-4 bookkeeping fixture"):
                build_file_posix_metadata_split_brain_plan_from_canary_state(
                    volume="testvol",
                    scenario="repair-canary-posix",
                )

    def test_posix_metadata_split_brain_topology_uses_all_to_all_x3_accusations(self) -> None:
        source, pending_targets, metadata_variants = canary_file._posix_metadata_split_brain_topology(
            ["node-a", "node-b", "node-d"],
            "node-b",
        )

        self.assertEqual("node-b", source)
        self.assertEqual(
            {
                "node-a": ["node-b", "node-d"],
                "node-b": ["node-a", "node-d"],
                "node-d": ["node-a", "node-b"],
            },
            pending_targets,
        )
        self.assertEqual(
            {"node-a": 1, "node-b": 0, "node-d": 2},
            metadata_variants,
        )

    def test_posix_metadata_split_brain_topology_keeps_x4_two_cohorts(self) -> None:
        source, pending_targets, metadata_variants = canary_file._posix_metadata_split_brain_topology(
            ["node-a", "node-b", "node-d", "node-c"],
            "node-a",
        )

        self.assertEqual("node-a", source)
        self.assertEqual(
            {
                "node-a": ["node-d", "node-c"],
                "node-b": ["node-d", "node-c"],
                "node-d": ["node-a", "node-b"],
                "node-c": ["node-a", "node-b"],
            },
            pending_targets,
        )
        self.assertEqual(
            {"node-a": 0, "node-b": 0, "node-d": 1, "node-c": 1},
            metadata_variants,
        )

    def test_file_posix_metadata_split_brain_allows_mode_uid_gid_proof_without_acl_mount(self) -> None:
        with (
            patch(
                "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
                return_value=(
                    ["node-a", "node-b", "node-d"],
                    {
                        "node-a": "/gluster/gtest3b/gtest3/brick",
                        "node-b": "/gluster/gtest3b/gtest3/brick",
                        "node-d": "/gluster/gtestlab/gtest3/brick",
                    },
                ),
            ),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount"),
            patch("gluster_heal_tool.canary_file._mountpoint_supports_acl", return_value=False),
            patch(
                "gluster_heal_tool.canary_file._run_local",
                side_effect=RuntimeError("stop after heal-disable request"),
            ) as run_local,
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after heal-disable request"):
                canary_file.create_file_posix_metadata_split_brain_canary(
                    volume="gtest3",
                    scenario="repair-canary-posix-preflight",
                    mismatch_host="node-a",
                    dir_name="alpha",
                    file_name="payload.txt",
                )

        run_local.assert_called_once_with(
            ["sudo", "-n", "gluster", "volume", "heal", "gtest3", "disable"]
        )
        self.assertEqual("file-posix-metadata-split-brain", write_state.call_args.args[2]["kind"])
        self.assertTrue(write_state.call_args.args[2]["partial"])

    def test_file_posix_metadata_split_brain_can_leave_heal_pending(self) -> None:
        brick_hosts = ["node-a", "node-b", "node-d"]
        backend_roots = {host: f"/brick/{host}" for host in brick_hosts}
        gfid = "6a36631d-94c2-46b4-b8e4-66646840389a"
        source_response = {
            "results": [
                {
                    "op": "inspect_path",
                    "ok": True,
                    "mode_bits": 0o644,
                    "uid": 1000,
                    "gid": 1000,
                    "trusted_gfid": gfid,
                }
            ]
        }
        metadata_response = {"results": [{"op": "inspect_path", "ok": True}]}
        index_response = {
            "results": [
                {
                    "op": "link_xattrop_gfid",
                    "ok": True,
                    "value_uuid": gfid,
                    "xattrop_entry": f"/brick/node-a/.glusterfs/indices/xattrop/{gfid}",
                },
                {
                    "op": "link_file_gfid_from_target",
                    "ok": True,
                    "value_uuid": gfid,
                    "file_gfid_path": f"/brick/node-a/.glusterfs/{gfid[:2]}/{gfid[2:4]}/{gfid}",
                },
            ]
        }
        afr_response = {"results": [{"op": "inspect_afr_state", "ok": True, "afr_xattrs": ["trusted.afr.gtest4-client-0"]}]}
        worker_responses = [source_response, metadata_response, metadata_response, metadata_response, index_response, afr_response, afr_response, afr_response]
        mount_file = f"{canary_file._canary_temp_mount_root('gtest4')}/posix-pending/alpha/payload.txt"
        metadata_by_host = {
            host: {
                "mode_bits": 0o644,
                "uid": 1000,
                "gid": 1000,
                "acl_access": "",
                "acl_default": "",
                "backend": f"/brick/{host}/posix-pending/alpha/payload.txt",
            }
            for host in brick_hosts
        }
        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(brick_hosts, backend_roots)),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount"),
            patch("gluster_heal_tool.canary_file._mountpoint_supports_acl", return_value=False),
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local"),
            patch(
                "gluster_heal_tool.canary_file._run_local_text",
                side_effect=[
                    "Brick node-a:/brick\n/posix-pending/alpha/payload.txt \nStatus: Connected\nNumber of entries: 1",
                    "Brick node-a:/brick\nStatus: Connected\nNumber of entries in split-brain: 0",
                ],
            ) as run_local_text,
            patch("gluster_heal_tool.canary_file._capture_file_posix_metadata_by_host", return_value=metadata_by_host),
            patch("gluster_heal_tool.canary_file._trigger_canary_heal") as trigger_heal,
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
            patch("gluster_heal_tool.canary_file._print_response"),
            patch("builtins.print"),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker_responses),
        ):
            canary_file.create_file_posix_metadata_split_brain_canary(
                volume="gtest4",
                scenario="posix-pending",
                mismatch_host="node-a",
                dir_name="alpha",
                file_name="payload.txt",
                leave_heal_pending=True,
            )

        trigger_heal.assert_not_called()
        self.assertEqual(2, run_local_text.call_count)
        state = write_state.call_args.args[2]
        self.assertTrue(state["leave_heal_pending"])
        self.assertFalse(state["heal_crawl_triggered"])
        self.assertEqual("harness-only-pending", state["proof_label"])
        self.assertTrue(state["heal_info_contains_mount_file_before"])
        self.assertFalse(state["gluster_visible_metadata_split_brain"])

    def test_file_posix_acl_majority_canary_alias_uses_acl_field(self) -> None:
        with patch("gluster_heal_tool.canary_file.create_file_posix_metadata_canary") as create_mock:
            create_file_posix_acl_majority_canary(
                volume="gtest3",
                scenario="repair-canary-file-acl",
                mismatch_host="node-c",
                dir_name="alpha",
                file_name="payload.txt",
            )

        create_mock.assert_called_once_with(
            volume="gtest3",
            scenario="repair-canary-file-acl",
            mismatch_host="node-c",
            dir_name="alpha",
            file_name="payload.txt",
            fields="acl",
            hold_mismatch_offline=True,
            ssh_user=canary_file.DEFAULT_SERVICE_USER,
            worker_path=str(canary_file.DEFAULT_WORKER_PATH),
        )

    def test_file_posix_metadata_canary_can_drift_access_acl_only(self) -> None:
        with (
            patch(
                "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
                return_value=(
                    ["node-a", "node-b", "node-c"],
                    {
                        "node-a": "/gluster/gtest3/gtest3/brick",
                        "node-b": "/gluster/gtest3/gtest3/brick",
                        "node-c": "/gluster/gtest3/gtest3/brick",
                    },
                ),
            ),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local"),
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before", "heal info after"]),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops") as host_ops,
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch("gluster_heal_tool.canary_file._local_setfacl") as setfacl,
            patch(
                "gluster_heal_tool.canary_file._canary_worker_command",
                return_value={
                    "host": "node-a",
                    "results": [{"op": "inspect_path", "ok": True, "mode_bits": 420, "uid": 1000, "gid": 1000}],
                },
            ),
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
        ):
            create_file_posix_metadata_canary(
                volume="gtest3",
                scenario="repair-canary-file-acl",
                mismatch_host="node-c",
                dir_name="alpha",
                file_name="payload.txt",
                fields="acl",
            )

        self.assertEqual("native-transaction", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("native-heal-first", write_state.call_args.args[2]["proof_label"])
        self.assertEqual("heal info before", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("heal info after", write_state.call_args.args[2]["heal_info_after"])
        setfacl.assert_called_once()
        self.assertTrue(ensure_mount.call_args.kwargs["acl_mount"])
        acl_path, acl_text = setfacl.call_args.args
        self.assertTrue(acl_path.endswith("/repair-canary/gtest3/repair-canary-file-acl/alpha/payload.txt"))
        self.assertIn("user:1:r--", acl_text)
        self.assertEqual(2, host_ops.call_count)
        canary_file.run_heal.assert_called_once_with("gtest3")
    def test_file_posix_metadata_parser_accepts_offline_and_maintenance_options(self) -> None:
        args = build_parser().parse_args(
            [
                "create-file-posix-metadata",
                "--fields",
                "mode",
                "--keep-mismatch-online",
                "--ssh-user",
                "root",
            ]
        )

        self.assertEqual("mode", args.fields)
        self.assertTrue(args.keep_mismatch_online)
        self.assertEqual("root", args.ssh_user)
        no_majority = build_parser().parse_args(["create-file-posix-metadata", "--fields", "no_majority_mode"])
        self.assertEqual("no_majority_mode", no_majority.fields)

    def test_file_posix_metadata_canary_can_create_no_majority_modes(self) -> None:
        host_ops_calls = []
        worker_calls = []
        def host_ops(host, args, **kwargs):  # noqa: ANN001
            host_ops_calls.append((host, args))
        def worker(host, **kwargs):  # noqa: ANN001
            worker_calls.append((host, kwargs["request"].ops))
            return {"host": host, "results": [{"op": "inspect_path", "ok": True, "mode_bits": 420, "uid": 0, "gid": 0}]}
        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(
                ["node-a", "node-b", "node-c"],
                {"node-a": "/brick", "node-b": "/brick", "node-c": "/brick"},
            )), patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"), patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local"), patch("gluster_heal_tool.canary_file._run_local_text", return_value="644:0:0"),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops", side_effect=host_ops),
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker),
            patch("gluster_heal_tool.canary_file._write_state"),
        ):
            create_file_posix_metadata_canary(
                volume="gtest3", scenario="repair-canary-posix-tie", mismatch_host="node-b",
                dir_name="alpha", file_name="payload.txt", fields="no_majority_mode",
            )
        self.assertEqual(["node-b", "node-c"], [host for host, _args in host_ops_calls])
        self.assertEqual("chmod", worker_calls[1][1][0]["op"])
        self.assertEqual("chmod", worker_calls[2][1][0]["op"])
        self.assertNotEqual(worker_calls[1][1][0]["mode_bits"], worker_calls[2][1][0]["mode_bits"])

    def test_directory_posix_metadata_parser_accepts_default_acl_field(self) -> None:
        args = build_parser().parse_args(["create-directory-posix-metadata", "--fields", "default_acl"])
        self.assertEqual("default_acl", args.fields)

    def test_directory_posix_metadata_canary_stops_mismatch_brick_and_chmods_directory(self) -> None:
        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(
                ["node-a", "node-b", "node-c"],
                {"node-a": "/gluster/gtest3b/gtest3/brick", "node-b": "/gluster/gtest3b/gtest3/brick", "node-c": "/gluster/gtest3b/gtest3/brick"},
            )),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local") as run_local,
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before", "heal info after"]),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops") as host_ops,
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
            patch("gluster_heal_tool.canary_file._canary_worker_command", return_value={
                "host": "node-a", "results": [{"op": "inspect_path", "ok": True, "mode_bits": 493}]
            }),
        ):
            create_directory_posix_metadata_canary(
                volume="gtest3", scenario="repair-canary-dir-posix", mismatch_host="node-b", dir_name="alpha"
            )

        self.assertEqual("native-transaction", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("native-heal-first", write_state.call_args.args[2]["proof_label"])
        self.assertEqual("heal info before", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("heal info after", write_state.call_args.args[2]["heal_info_after"])
        self.assertEqual(
            {"node-a": [], "node-c": []},
            write_state.call_args.args[2]["afr_pending_xattrs_by_host"],
        )
        self.assertTrue(any(call.args and call.args[0][:3] == ["sudo", "-n", "chmod"] for call in run_local.call_args_list))
        self.assertEqual(2, host_ops.call_count)
        self.assertEqual(
            ("node-b", ["brick-down", "--volume", "gtest3", "--brick", "/gluster/gtest3b/gtest3/brick"]),
            host_ops.call_args_list[0].args[:2],
        )
        self.assertEqual(
            ("node-b", ["brick-kick", "--volume", "gtest3"]),
            host_ops.call_args_list[1].args[:2],
        )
        canary_file.run_heal.assert_called_once_with("gtest3")

    def test_directory_posix_metadata_canary_can_drift_default_acl_only(self) -> None:
        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(
                ["node-a", "node-b", "node-c"],
                {"node-a": "/gluster/gtest3b/gtest3/brick", "node-b": "/gluster/gtest3b/gtest3/brick", "node-c": "/gluster/gtest3b/gtest3/brick"},
            )),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount,
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._run_local"),
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before", "heal info after"]),
            patch("gluster_heal_tool.canary_file._run_remote_host_ops"),
            patch("gluster_heal_tool.canary_file._brick_status_is_online", return_value=False),
            patch("gluster_heal_tool.canary_file._local_setfacl") as setfacl,
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
            patch("gluster_heal_tool.canary_file._canary_worker_command", return_value={
                "host": "node-a", "results": [{"op": "inspect_path", "ok": True, "mode_bits": 493}]
            }),
        ):
            create_directory_posix_metadata_canary(
                volume="gtest3", scenario="repair-canary-dir-acl", mismatch_host="node-b", dir_name="alpha", fields="default_acl"
            )
        self.assertEqual("native-transaction", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("native-heal-first", write_state.call_args.args[2]["proof_label"])
        self.assertEqual("heal info before", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("heal info after", write_state.call_args.args[2]["heal_info_after"])
        setfacl.assert_called_once()
        self.assertTrue(ensure_mount.call_args.kwargs["acl_mount"])
        acl_path, acl_text = setfacl.call_args.args
        self.assertTrue(acl_path.endswith("/repair-canary/gtest3/repair-canary-dir-acl/alpha"))
        self.assertIn("default:user:daemon:r-x", acl_text)
        canary_file.run_heal.assert_called_once_with("gtest3")

    def test_worker_supports_chmod_and_chown_canary_ops(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir)
            target = backend_root / "repair-canary" / "alpha" / "payload.txt"
            target.parent.mkdir(parents=True)
            target.touch()
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary",
                backend_root=str(backend_root),
                ops=[],
            )
            with patch("gluster_heal_tool.worker._chmod") as chmod_mock, patch(
                "gluster_heal_tool.worker._chown"
            ) as chown_mock, patch("gluster_heal_tool.worker._setfacl") as setfacl_mock:
                chmod_result = _run_canary_op(
                    request,
                    {
                        "op": "chmod",
                        "path": str(target),
                        "mode_bits": "0640",
                    },
                )
                chown_result = _run_canary_op(
                    request,
                    {
                        "op": "chown",
                        "path": str(target),
                        "uid": 1000,
                        "gid": 1001,
                    },
                )
                setfacl_result = _run_canary_op(
                    request,
                    {"op": "setfacl", "path": str(target), "acl_text": "user::rw-\nother::r--\n"},
                )

        self.assertTrue(chmod_result["ok"])
        self.assertEqual(0o640, chmod_result["mode_bits"])
        chmod_mock.assert_called_once_with(str(target), 0o640)
        self.assertTrue(chown_result["ok"])
        self.assertEqual(1000, chown_result["uid"])
        self.assertEqual(1001, chown_result["gid"])
        chown_mock.assert_called_once_with(str(target), 1000, 1001)
        self.assertTrue(setfacl_result["ok"])
        setfacl_mock.assert_called_once_with(str(target), "user::rw-\nother::r--\n")

    def test_file_content_split_brain_wrapper_uses_same_gfid_pure_replica_helper(self) -> None:
        with patch("gluster_heal_tool.canary._create_file_split_brain_canary") as impl:
            create_file_content_split_brain_canary(
                volume="gtest4",
                scenario="repair-canary-content-split",
                dir_name="alpha",
                file_name="payload.txt",
            )

        impl.assert_called_once_with(
            volume="gtest4",
            scenario="repair-canary-content-split",
            dir_name="alpha",
            file_name="payload.txt",
            kind="file-content-split-brain",
            display_label="file content split",
            same_gfid=True,
            ssh_user=canary_file.DEFAULT_SERVICE_USER,
            worker_path=str(canary_file.DEFAULT_WORKER_PATH),
        )

    def test_file_content_split_brain_writes_one_gfid_with_two_payload_cohorts(self) -> None:
        worker_calls: list[tuple[str, list[dict[str, object]]]] = []

        def worker(host, **kwargs):  # noqa: ANN001
            worker_calls.append((host, kwargs["request"].ops))
            return {"host": host, "results": []}

        with (
            patch(
                "gluster_heal_tool.canary._brick_hosts_and_paths",
                return_value=(
                    ["node-a", "node-b", "node-c", "node-d"],
                    {
                        "node-a": "/brick/node-a",
                        "node-b": "/brick/node-b",
                        "node-c": "/brick/node-c",
                        "node-d": "/brick/node-d",
                    },
                ),
            ),
            patch("gluster_heal_tool.canary._run_local"),
            patch("gluster_heal_tool.canary._ensure_canary_mount"),
            patch("gluster_heal_tool.canary._local_mount_dir"),
            patch("gluster_heal_tool.canary._local_mount_file"),
            patch("gluster_heal_tool.canary._canary_worker_command", side_effect=worker),
            patch("gluster_heal_tool.canary._print_response"),
            patch("gluster_heal_tool.canary._write_state") as write_state,
        ):
            create_file_content_split_brain_canary(
                volume="gtest4",
                scenario="repair-canary-content-split",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["node-a", "node-b", "node-c", "node-d"], [host for host, _ in worker_calls])
        gfid_values = {
            str(next(op["value_hex"] for op in ops if op.get("name") == "trusted.gfid"))
            for _, ops in worker_calls
        }
        payloads = {
            str(next(op["text"] for op in ops if op.get("op") == "write_text"))
            for _, ops in worker_calls
        }
        self.assertEqual(1, len(gfid_values))
        self.assertEqual(2, len(payloads))
        state = write_state.call_args.args[2]
        self.assertTrue(state["same_gfid"])
        self.assertTrue(state["leave_heal_pending"])
        self.assertEqual(next(iter(gfid_values)), state["gfid_hex"])
        self.assertEqual(state["gfid_uuid"], state["left_gfid_uuid"])
        self.assertEqual(state["gfid_uuid"], state["right_gfid_uuid"])
        self.assertEqual(state["file_gfid_path"], state["left_file_gfid_path"])

    def test_file_content_split_brain_rejects_failed_worker_before_state_is_recorded(self) -> None:
        with (
            patch(
                "gluster_heal_tool.canary._brick_hosts_and_paths",
                return_value=(
                    ["node-a", "node-b", "node-d", "192.0.2.30"],
                    {
                        "node-a": "/brick/node-a",
                        "node-b": "/brick/node-b",
                        "node-d": "/brick/node-d",
                        "192.0.2.30": "/brick/alias",
                    },
                ),
            ),
            patch("gluster_heal_tool.canary._run_local"),
            patch("gluster_heal_tool.canary._ensure_canary_mount"),
            patch("gluster_heal_tool.canary._local_mount_dir"),
            patch("gluster_heal_tool.canary._local_mount_file"),
            patch(
                "gluster_heal_tool.canary._canary_worker_command",
                return_value={"host": "node-a", "results": [{"ok": False, "error": "write failed"}]},
            ),
            patch("gluster_heal_tool.canary._print_response"),
            patch("gluster_heal_tool.canary._write_state") as write_state,
        ):
            with self.assertRaisesRegex(RuntimeError, "file content split worker node-a failed: write failed"):
                create_file_content_split_brain_canary(
                    volume="gtest4",
                    scenario="repair-canary-content-split-failure",
                    dir_name="alpha",
                    file_name="payload.txt",
                )

        write_state.assert_not_called()

    def test_file_data_split_brain_wrapper_delegates_to_arbiter_aware_helper(self) -> None:
        with patch("gluster_heal_tool.canary._create_file_data_split_brain_canary_impl") as impl:
            from gluster_heal_tool.canary import create_file_data_split_brain_canary

            create_file_data_split_brain_canary(
                volume="gtest3a",
                scenario="repair-canary-gtest3a-data-split",
                dir_name="alpha",
                file_name="payload.txt",
            )

        impl.assert_called_once_with(
            volume="gtest3a",
            scenario="repair-canary-gtest3a-data-split",
            dir_name="alpha",
            file_name="payload.txt",
            ssh_user=canary_file.DEFAULT_SERVICE_USER,
            worker_path=str(canary_file.DEFAULT_WORKER_PATH),
        )

    def test_file_data_split_brain_wrapper_passes_explicit_arbiter_host(self) -> None:
        with patch("gluster_heal_tool.canary._create_file_data_split_brain_canary_impl") as impl:
            from gluster_heal_tool.canary import create_file_data_split_brain_canary

            create_file_data_split_brain_canary(
                volume="gtest3a",
                scenario="repair-canary-gtest3a-data-split",
                dir_name="alpha",
                file_name="payload.txt",
                arbiter_host="node-c",
            )

        impl.assert_called_once_with(
            volume="gtest3a",
            scenario="repair-canary-gtest3a-data-split",
            dir_name="alpha",
            file_name="payload.txt",
            arbiter_host="node-c",
            ssh_user=canary_file.DEFAULT_SERVICE_USER,
            worker_path=str(canary_file.DEFAULT_WORKER_PATH),
        )

    def test_canary_parser_accepts_arbiter_host_for_file_data_split_brain(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["create-file-data-split-brain", "--arbiter-host", "node-c"])

        self.assertEqual("node-c", args.arbiter_host)

    def test_canary_parser_can_leave_arbiter_data_split_heal_pending(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["create-file-data-split-brain", "--leave-heal-pending"])

        self.assertTrue(args.leave_heal_pending)

    def test_file_data_split_brain_canary_prefers_volume_reported_arbiter_role(self) -> None:
        brick_hosts = ["node-a", "node-c", "node-b"]
        backend_roots = {
            "node-a": "/gluster/gtest3a/gtest3a/brick",
            "node-c": "/gluster/gtest3a/arbiter/brick",
            "node-b": "/gluster/gtest3a/gtest3a/brick",
        }
        worker_responses = [
            {"host": "node-a", "results": []},
            {"host": "node-b", "results": []},
            {"host": "node-c", "results": []},
        ]
        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(brick_hosts, backend_roots)),
            patch(
                "gluster_heal_tool.canary_file.canary_api._brick_roles",
                return_value={"node-a": "data", "node-c": "arbiter", "node-b": "data"},
            ),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount"),
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_mount_file"),
            patch("gluster_heal_tool.canary_file._run_local"),
            patch("gluster_heal_tool.canary_file._trigger_canary_heal"),
            patch("gluster_heal_tool.canary_file._record_file_baseline") as record,
            patch("gluster_heal_tool.canary_file._print_response"),
            patch("builtins.print"),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker_responses) as worker,
        ):
            canary_file.create_file_data_split_brain_canary(
                volume="gtest3a",
                scenario="repair-canary-gtest3a-data-split",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["node-a", "node-b", "node-c"], [call.args[0] for call in worker.call_args_list])
        self.assertEqual(
            {"node-a": "data", "node-b": "data", "node-c": "arbiter"},
            record.call_args.args[2]["brick_roles_by_host"],
        )

    def test_file_data_split_brain_canary_can_leave_heal_pending(self) -> None:
        brick_hosts = ["node-a", "node-b", "node-d"]
        backend_roots = {host: f"/brick/{host}" for host in brick_hosts}
        worker_responses = [{"host": host, "results": []} for host in brick_hosts]
        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(brick_hosts, backend_roots)),
            patch("gluster_heal_tool.canary_file.canary_api._brick_roles", return_value={"node-a": "data", "node-b": "data", "node-d": "arbiter"}),
            patch("gluster_heal_tool.canary_file._ensure_canary_mount"),
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_mount_file"),
            patch("gluster_heal_tool.canary_file._run_local"),
            patch("gluster_heal_tool.canary_file._trigger_canary_heal") as trigger_heal,
            patch("gluster_heal_tool.canary_file._record_file_baseline") as record,
            patch("gluster_heal_tool.canary_file._print_response"),
            patch("builtins.print"),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker_responses),
        ):
            canary_file.create_file_data_split_brain_canary(
                volume="gtest3a",
                scenario="repair-canary-gtest3a-pending",
                dir_name="alpha",
                file_name="payload.txt",
                trigger_heal=False,
            )

        trigger_heal.assert_not_called()
        self.assertTrue(record.call_args.args[2]["leave_heal_pending"])

    def test_touch_index_from_target_uses_canonical_gfid(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "xattrop"
            target.parent.mkdir(parents=True)
            target.touch()

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "touch_index_from_target",
                        "target_path": str(target),
                        "index_root": str(index_root),
                    },
                )

            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            self.assertTrue(result["ok"])
            self.assertEqual(canonical, result["value_uuid"])
            self.assertTrue((index_root / canonical).exists())

    def test_set_afr_metadata_pending_sets_and_rejects_non_canary_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            target.parent.mkdir(parents=True)
            target.touch()
            pending_value = bytes.fromhex("000000000000000100000000")
            calls: list[tuple[str, bytes]] = []

            def fake_setxattr(path, name, value, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertFalse(follow_symlinks)
                self.assertTrue(name.startswith("trusted.afr."))
                self.assertEqual(pending_value, value)
                calls.append((name, value))

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.setxattr", side_effect=fake_setxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "set_afr_metadata_pending",
                        "path": str(target),
                        "pending_names": [
                            "trusted.afr.gtest-client-0",
                            "trusted.afr.gtest-client-2",
                        ],
                        "value_hex": "000000000000000100000000",
                    },
                )
                rejected = _run_canary_op(
                    request,
                    {
                        "op": "set_afr_metadata_pending",
                        "path": str(backend_root / "other" / "alpha"),
                        "pending_names": ["trusted.afr.gtest-client-0"],
                        "value_hex": "000000000000000100000000",
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual(str(target), result["path"])
            self.assertEqual(["trusted.afr.gtest-client-0", "trusted.afr.gtest-client-2"], result["afr_xattr_names"])
            self.assertEqual(
                [
                    {"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    {"name": "trusted.afr.gtest-client-2", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                ],
                result["afr_xattrs"],
            )
            self.assertEqual("0x000000000000000100000000", result["value_hex"])
            self.assertEqual(2, len(calls))
            self.assertFalse(rejected["ok"])
            self.assertIn("not under canary scenario", str(rejected["error"]))

    def test_link_xattrop_gfid_from_target_uses_canonical_gfid(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "xattrop"
            target.parent.mkdir(parents=True)
            target.touch()

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "link_xattrop_gfid",
                        "target_path": str(target),
                    },
                )

            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            self.assertTrue(result["ok"])
            self.assertEqual(canonical, result["value_uuid"])
            self.assertEqual(str(index_root / canonical), result["xattrop_entry"])
            self.assertTrue((index_root / canonical).exists())

    def test_remove_afr_fixture_removes_only_trusted_afr_xattrs_and_xattrop_index(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "xattrop"
            target.parent.mkdir(parents=True)
            target.touch()
            index_root.mkdir(parents=True)
            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            (index_root / canonical).touch()
            afr_zero = bytes.fromhex("000000000000000100000000")
            afr_one = bytes.fromhex("000000010000000000000000")
            raw_gfid = uuid.UUID(canonical).bytes
            removals: list[str] = []

            def fake_listxattr(path, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertFalse(follow_symlinks)
                return [
                    "trusted.afr.gtest-client-0",
                    "trusted.afr.gtest-client-2",
                    "trusted.gfid",
                    "user.note",
                ]

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertFalse(follow_symlinks)
                if name == "trusted.afr.gtest-client-0":
                    return afr_zero
                if name == "trusted.afr.gtest-client-2":
                    return afr_one
                if name == "trusted.gfid":
                    return raw_gfid
                raise AssertionError(f"unexpected xattr lookup: {name}")

            def fake_removexattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertFalse(follow_symlinks)
                removals.append(name)

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with (
                patch("gluster_heal_tool.worker.os.listxattr", side_effect=fake_listxattr),
                patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr),
                patch("gluster_heal_tool.worker.os.removexattr", side_effect=fake_removexattr),
            ):
                result = _run_canary_op(
                    request,
                    {
                        "op": "remove_afr_fixture",
                        "target_path": str(target),
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual(["trusted.afr.gtest-client-0", "trusted.afr.gtest-client-2"], result["removed_xattrs"])
            self.assertEqual([str(index_root / canonical)], result["removed_index_entries"])
            self.assertEqual(["trusted.afr.gtest-client-0", "trusted.afr.gtest-client-2"], removals)
            self.assertFalse((index_root / canonical).exists())

    def test_inspect_afr_state_returns_only_trusted_afr_xattrs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            target.parent.mkdir(parents=True)
            target.touch()

            afr_zero = bytes.fromhex("000000000000000100000000")
            afr_one = bytes.fromhex("000000010000000000000000")

            def fake_listxattr(path, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertFalse(follow_symlinks)
                return [
                    "trusted.afr.gtest-client-0",
                    "trusted.afr.gtest-client-2",
                    "trusted.gfid",
                    "user.note",
                ]

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertFalse(follow_symlinks)
                if name == "trusted.afr.gtest-client-0":
                    return afr_zero
                if name == "trusted.afr.gtest-client-2":
                    return afr_one
                raise AssertionError(f"unexpected xattr lookup: {name}")

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.listxattr", side_effect=fake_listxattr), patch(
                "gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr
            ):
                result = _run_canary_op(
                    request,
                    {
                        "op": "inspect_afr_state",
                        "path": str(target),
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual(str(target), result["path"])
            self.assertEqual(
                [
                    {"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    {"name": "trusted.afr.gtest-client-2", "value_hex": "0x000000010000000000000000", "value_bytes": 12},
                ],
                result["afr_xattrs"],
            )
            self.assertEqual(["trusted.afr.gtest-client-0", "trusted.afr.gtest-client-2"], result["afr_xattr_names"])

    def test_inspect_afr_state_rejects_non_canary_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            result = _run_canary_op(
                request,
                {
                    "op": "inspect_afr_state",
                    "path": str(backend_root / "other" / "alpha"),
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("not under canary scenario", str(result["error"]))

    def test_directory_child_gap_alias_uses_shallow_child_gap_label(self) -> None:
        with patch("gluster_heal_tool.canary_directory._create_directory_child_canary") as create_child:
            create_directory_child_gap_canary(
                volume="gtest",
                scenario="repair-canary-dir-gap-shallow",
                parent_name="alpha",
                present_child="beta",
                missing_child="gamma",
                leave_heal_pending=True,
            )

        create_child.assert_called_once()
        self.assertEqual("directory-child-gap", create_child.call_args.kwargs["label"])
        self.assertTrue(create_child.call_args.kwargs["leave_heal_pending"])

    def test_directory_child_gap_uses_missing_host_brick_root_on_mixed_layout(self) -> None:
        mixed_roots = {
            "node-a": "/gluster/gtest3a/gtest3a/brick",
            "node-b": "/gluster/gtest3a/gtest3a/brick",
            "node-c": "/gluster/gtest3a/arbiter/brick",
        }
        with (
            patch("gluster_heal_tool.canary_directory._brick_hosts_and_backend_roots", return_value=(["node-a", "node-b", "node-c"], mixed_roots)),
            patch("gluster_heal_tool.canary_directory._ensure_canary_mount"),
            patch("gluster_heal_tool.canary_directory.canary_api._local_mount_dir"),
            patch("gluster_heal_tool.canary_directory.canary_api._canary_worker_command", return_value={"host": "node-a", "results": []}) as worker,
            patch("gluster_heal_tool.canary_directory.canary_api._write_state"),
            patch("gluster_heal_tool.canary_directory._write_observation_log"),
            patch("gluster_heal_tool.canary_directory._capture_parent_lookup_snapshot", return_value={}),
            patch("gluster_heal_tool.canary_directory._capture_mount_stat_snapshot", return_value={"path": "/tmp/mock"}),
        ):
            create_directory_child_gap_canary(
                volume="gtest3a",
                scenario="repair-canary-dir-gap-mixed",
                missing_host="node-a",
                parent_name="alpha",
                present_child="beta",
                missing_child="gamma",
            )

        self.assertTrue(worker.called)
        request = worker.call_args.kwargs["request"]
        self.assertEqual("/gluster/gtest3a/gtest3a/brick", request.backend_root)
        self.assertEqual("/gluster/gtest3a/gtest3a/brick/repair-canary-dir-gap-mixed/alpha/beta/gamma", request.ops[0]["path"])

    def test_directory_metadata_uses_per_host_brick_root_on_mixed_layout(self) -> None:
        mixed_roots = {
            "node-a": "/gluster/gtest3a/gtest3a/brick",
            "node-b": "/gluster/gtest3a/gtest3a/brick",
            "node-c": "/gluster/gtest3a/arbiter/brick",
        }
        worker_response = {"host": "node-a", "results": [{"op": "getxattr", "ok": True, "name": "trusted.gfid", "value_uuid": "11111111-2222-3333-4444-555555555555"}, {"op": "getxattr", "ok": True, "name": "trusted.glusterfs.mdata", "value_hex": "01020304"}]}
        with (
            patch("gluster_heal_tool.canary_directory._brick_hosts_and_backend_roots", return_value=(["node-a", "node-b", "node-c"], mixed_roots)),
            patch("gluster_heal_tool.canary_directory.canary_api._run_local"),
            patch("gluster_heal_tool.canary_directory._ensure_canary_mount"),
            patch("gluster_heal_tool.canary_directory.canary_api._local_mount_dir"),
            patch("gluster_heal_tool.canary_directory.canary_api._canary_worker_command", return_value=worker_response) as worker,
            patch("gluster_heal_tool.canary_directory.canary_api._write_state"),
        ):
            create_directory_metadata_canary(
                volume="gtest3a",
                scenario="repair-canary-dir-metadata-mixed",
                mismatch_host="node-c",
                dir_name="alpha",
            )

        self.assertGreaterEqual(worker.call_count, 3)
        first_call_request = worker.call_args_list[0].kwargs["request"]
        self.assertEqual("/gluster/gtest3a/gtest3a/brick", first_call_request.backend_root)
        self.assertEqual("/gluster/gtest3a/gtest3a/brick/repair-canary-dir-metadata-mixed/alpha", first_call_request.ops[0]["path"])

    def test_file_metadata_split_brain_plan_bridge_uses_quarantine_review_action(self) -> None:
        fake_state = {
            "kind": "file-metadata-split-brain",
            "mount_root": "/gtest",
            "mount_file": "/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "canonical_hosts": ["node-c", "node-a"],
            "divergent_hosts": ["node-d", "node-b"],
            "canonical_mdata_hex": "11111111111111111111111111111111",
            "divergent_mdata_hex": "22222222222222222222222222222222",
            "marker_host": "node-c",
            "mismatch_host": "node-d",
            "metadata_pending_value": "000000000000000100000000",
            "heal_info_before": "heal info before",
            "heal_info_split_brain_before": "split-brain before\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_after": "heal info after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_split_brain_after": "split-brain after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_contains_mount_file_before": True,
            "heal_info_contains_mount_file_after": True,
            "heal_info_split_brain_contains_mount_file_before": True,
            "heal_info_split_brain_contains_mount_file_after": True,
            "proof_label": "afr-synthesized",
            "afr_pending_xattrs_by_host": {"node-a": [{"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12}], "node-b": []},
            "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"},
        }

        with patch("gluster_heal_tool.canary_file._read_state", return_value=fake_state):
            plan = build_file_metadata_split_brain_plan_from_canary_state(
                volume="gtest",
                scenario="repair-canary-file-metadata-split-brain",
            )

        _assert_canary_state_provenance(
            self,
            plan,
            kind="file-metadata-split-brain",
            volume="gtest",
            scenario="repair-canary-file-metadata-split-brain",
        )
        action = plan["actions"][0]
        self.assertEqual("review_entry_split_brain", action["action_type"])
        self.assertEqual("ambiguous_entry_split_brain_file", action["repair_strategy"])
        self.assertEqual("node-c", action["winner_host"])
        self.assertEqual("11111111111111111111111111111111", action["winner_file_gfid"])
        self.assertEqual("heal info before", action["heal_info_before"])
        self.assertEqual("split-brain before\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt", action["heal_info_split_brain_before"])
        self.assertEqual("heal info after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt", action["heal_info_after"])
        self.assertEqual("split-brain after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt", action["heal_info_split_brain_after"])
        self.assertEqual(4, len(action["file_copies"]))
        notes = "\n".join(action["notes"])
        self.assertIn("file-metadata-split-brain marker: preserve-first quarantine proof", notes)
        self.assertIn("metadata pending value: 0x000000000000000100000000", notes)
        self.assertIn("heal info before split-brain crawl: heal info before", notes)
        self.assertIn("heal info split-brain before split-brain crawl: split-brain before", notes)
        self.assertIn("heal info after split-brain crawl: heal info after", notes)
        self.assertIn("heal info split-brain after split-brain crawl: split-brain after", notes)
        self.assertIn("afr pending xattrs by host:", notes)
        self.assertIn("brick roles by host:", notes)
        self.assertIn("node-c: arbiter", notes)
        self.assertIn("node-a: trusted.afr.gtest-client-0", notes)
        self.assertEqual({"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"}, action["brick_roles_by_host"])
        results = build_apply_results(plan, execution_mode="dry-run", backup_mode="required", batch=True)
        rendered = render_apply_run(results)
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("node-c: arbiter", rendered)

    def test_file_metadata_split_brain_plan_bridge_rejects_native_heal_smoke_state(self) -> None:
        smoke_state = {
            "kind": "file-metadata-split-brain",
            "mount_root": "/gtest",
            "mount_file": "/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "canonical_hosts": ["node-c", "node-a"],
            "divergent_hosts": ["node-d", "node-b"],
            "canonical_mdata_hex": "11111111111111111111111111111111",
            "divergent_mdata_hex": "22222222222222222222222222222222",
            "marker_host": "node-c",
            "mismatch_host": "node-d",
            "metadata_pending_value": "000000000000000100000000",
            "heal_info_before": "heal info before",
            "heal_info_split_brain_before": "split-brain before",
            "heal_info_after": "heal info after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_split_brain_after": "split-brain after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_contains_mount_file_before": False,
            "heal_info_contains_mount_file_after": True,
            "heal_info_split_brain_contains_mount_file_before": False,
            "heal_info_split_brain_contains_mount_file_after": True,
            "proof_label": "native-heal-smoke",
            "afr_pending_xattrs_by_host": {"node-a": [{"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12}], "node-b": []},
            "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"},
        }

        with patch("gluster_heal_tool.canary_file._read_state", return_value=smoke_state):
            with self.assertRaises(RuntimeError) as ctx:
                build_file_metadata_split_brain_plan_from_canary_state(
                    volume="gtest",
                    scenario="repair-canary-file-metadata-split-brain",
                )

        self.assertIn("diagnostic/native-heal-smoke", str(ctx.exception))

    def test_file_metadata_split_brain_plan_bridge_rejects_stale_state_without_split_brain_snapshots(self) -> None:
        stale_state = {
            "kind": "file-metadata-split-brain",
            "mount_root": "/gtest",
            "mount_file": "/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "canonical_hosts": ["node-c", "node-a"],
            "divergent_hosts": ["node-d", "node-b"],
            "canonical_mdata_hex": "11111111111111111111111111111111",
            "divergent_mdata_hex": "22222222222222222222222222222222",
            "marker_host": "node-c",
            "mismatch_host": "node-d",
            "metadata_pending_value": "000000000000000100000000",
            "afr_pending_xattrs_by_host": {"node-a": [{"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12}], "node-b": []},
            "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "arbiter", "node-d": "data"},
        }

        with patch("gluster_heal_tool.canary_file._read_state", return_value=stale_state):
            with self.assertRaises(RuntimeError) as ctx:
                build_file_metadata_split_brain_plan_from_canary_state(
                    volume="gtest",
                    scenario="repair-canary-file-metadata-split-brain",
                )

        self.assertIn("fresh heal info and split-brain snapshots", str(ctx.exception))

    def test_directory_backend_child_gap_alias_uses_backend_label(self) -> None:
        with patch("gluster_heal_tool.canary_directory._create_directory_child_canary") as create_child:
            create_directory_backend_child_gap_canary(
                volume="gtest",
                scenario="repair-canary-dir-gap-backend",
                parent_name="alpha",
                present_child="beta",
                missing_child="gamma",
                leave_heal_pending=True,
            )

        create_child.assert_called_once()
        self.assertEqual("directory-backend-child-gap", create_child.call_args.kwargs["label"])
        self.assertEqual("directory-backend-child-gap", create_child.call_args.kwargs["kind"])
        self.assertTrue(create_child.call_args.kwargs["leave_heal_pending"])

    def test_directory_backend_child_gap_bounded_alias_uses_bounded_label(self) -> None:
        with patch("gluster_heal_tool.canary_directory._create_directory_child_canary") as create_child:
            create_directory_backend_child_gap_bounded_canary(
                volume="gtest",
                scenario="repair-canary-dir-gap-backend-bounded",
                parent_name="alpha",
                present_child="beta",
                missing_child="gamma",
            )

        create_child.assert_called_once()
        self.assertEqual("directory-backend-child-gap-bounded", create_child.call_args.kwargs["label"])
        self.assertEqual("directory-backend-child-gap-bounded", create_child.call_args.kwargs["kind"])

    def test_directory_backend_child_gap_mixed_build_emits_file_and_directory_actions(self) -> None:
        state = {
            "kind": "directory-backend-child-gap-mixed",
            "volume": "gtest",
            "scenario": "repair-canary-dir-gap-backend-mixed",
            "missing_host": "brick-c",
            "source_host": "brick-a",
            "mount_root": "/gtest",
            "mount_parent": "/gtest/repair-canary-dir-gap-backend-mixed/tree",
            "mount_path": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level2/level3/level3.txt",
            "backend_root": "/srv/gluster/brick-store/gtest",
            "backend_scenario_root": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-mixed",
            "backend_tree_root": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-mixed/tree",
            "missing_backend": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-mixed/tree/level1",
            "tree_depth": 3,
            "gap_level": 1,
            "tree_paths": {
                "tree_root": "/gtest/repair-canary-dir-gap-backend-mixed/tree",
                "root_file": "/gtest/repair-canary-dir-gap-backend-mixed/tree/root.txt",
                "level1_dir": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1",
                "level1_file": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level1.txt",
                "level2_dir": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level2",
                "level2_file": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level2/level2.txt",
                "level3_dir": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level2/level3",
                "level3_file": "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level2/level3/level3.txt",
            },
            "file_children": [
                {
                    "file_name": "root.txt",
                    "mount_file": "/gtest/repair-canary-dir-gap-backend-mixed/tree/root.txt",
                    "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-mixed/tree/root.txt",
                    "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/aa/bb/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "gfid_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "source_host": "brick-a",
                }
            ],
            "backend_observations": [
                {
                    "host": "brick-a",
                    "results": [
                        {"label": "backend_missing_child", "ok": True, "trusted_gfid": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"}
                    ],
                }
            ],
        }

        with patch("gluster_heal_tool.canary_directory.canary_api._read_state", return_value=state):
            plan = build_directory_backend_child_gap_plan_from_canary_state(
                volume="gtest", scenario="repair-canary-dir-gap-backend-mixed"
            )

        _assert_canary_state_provenance(
            self,
            plan,
            kind="directory-backend-child-gap-mixed",
            volume="gtest",
            scenario="repair-canary-dir-gap-backend-mixed",
        )
        self.assertEqual(2, len(plan["actions"]))
        self.assertEqual("repair_file", plan["actions"][0]["action_type"])
        self.assertEqual("repair_directory_metadata", plan["actions"][1]["action_type"])
        self.assertEqual("restore_missing_child_replica", plan["actions"][0]["repair_strategy"])
        self.assertEqual("recreate_missing_directory_backend_child_gap", plan["actions"][1]["repair_strategy"])
        self.assertEqual("/gtest/repair-canary-dir-gap-backend-mixed/tree/root.txt", plan["actions"][0]["logical_path"])
        self.assertEqual(
            "/gtest/repair-canary-dir-gap-backend-mixed/tree/level1/level2/level3/level3.txt",
            plan["actions"][1]["logical_path"],
        )

    def test_directory_backend_child_gap_mixed_cleanup_removes_file_gfid_residue(self) -> None:
        state = {
            "kind": "directory-backend-child-gap-mixed",
            "volume": "gtest",
            "scenario": "repair-canary-dir-gap-backend-mixed",
            "missing_host": "brick-c",
            "backend_root": "/srv/gluster/brick-store/gtest",
            "file_children": [
                {
                    "file_name": "root.txt",
                    "mount_file": "/gtest/repair-canary-dir-gap-backend-mixed/tree/root.txt",
                    "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-mixed/tree/root.txt",
                    "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/aa/bb/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "gfid_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "source_host": "brick-a",
                }
            ],
        }

        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-dir-gap-backend-mixed",
            kind="directory-backend-child-gap-mixed",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest",
            brick_hosts=["brick-a", "brick-b", "brick-c"],
            ssh_user="gluster-repair",
            state=state,
        )

        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual(1, sum(1 for step in result.steps if step.step_type == "remove_stale_file_gfid"))

    def test_directory_backend_child_gap_canonical_alias_uses_canonical_label(self) -> None:
        with patch("gluster_heal_tool.canary_directory._create_directory_child_canary") as create_child:
            create_directory_backend_child_gap_canonical_canary(
                volume="gtest",
                scenario="repair-canary-dir-gap-backend-canonical",
                parent_name="alpha",
                present_child="beta",
                missing_child="gamma",
            )

        create_child.assert_called_once()
        self.assertEqual("directory-backend-child-gap-canonical", create_child.call_args.kwargs["label"])
        self.assertEqual("directory-backend-child-gap-canonical", create_child.call_args.kwargs["kind"])

    def test_file_child_gap_alias_uses_missing_file_replica_shape(self) -> None:
        with patch("gluster_heal_tool.canary_file.create_missing_file_replica_canary") as create_missing:
            create_file_child_gap_canary(
                volume="gtest",
                scenario="repair-canary-file-child-gap",
                dir_name="alpha",
                file_name="payload.txt",
            )

        create_missing.assert_called_once()
        self.assertEqual("file-child-gap", create_missing.call_args.kwargs["kind"])
        self.assertEqual("file-child-gap", create_missing.call_args.kwargs["label"])
        self.assertTrue(create_missing.call_args.kwargs["trigger_heal"])

    def test_file_child_gap_alias_can_leave_heal_pending(self) -> None:
        with patch("gluster_heal_tool.canary_file.create_missing_file_replica_canary") as create_missing:
            create_file_child_gap_canary(
                volume="gtest",
                scenario="repair-canary-file-child-gap",
                dir_name="alpha",
                file_name="payload.txt",
                trigger_heal=False,
            )

        create_missing.assert_called_once()
        self.assertFalse(create_missing.call_args.kwargs["trigger_heal"])

    def test_file_child_gap_cleanup_removes_index_from_every_brick(self) -> None:
        state = {
            "kind": "file-child-gap",
            "missing_host": "brick-c",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "index_root": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop",
            "gfid_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }

        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-file-child-gap",
            kind="file-child-gap",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_roots={
                "brick-a": "/srv/gluster/brick-store/gtest/brick",
                "brick-b": "/srv/gluster/brick-store/gtest/brick",
                "brick-c": "/gluster/gtestlab/gtest/brick",
            },
            brick_hosts=["brick-a", "brick-b", "brick-c"],
            ssh_user="gluster-repair",
            state=state,
        )

        index_steps = [
            step
            for step in result.steps
            if step.step_type == "remove_stale_gfid"
        ]
        self.assertEqual(
            ["brick-a", "brick-b", "brick-c"],
            [step.host for step in index_steps],
        )
        self.assertTrue(
            all(
                step.target_path.endswith(
                    "/.glusterfs/indices/xattrop/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                )
                for step in index_steps
            )
        )
        brick_c_index = next(step for step in index_steps if step.host == "brick-c")
        self.assertEqual(
            "/gluster/gtestlab/gtest/brick/.glusterfs/indices/xattrop/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            brick_c_index.target_path,
        )
        missing_gfid_step = next(
            step for step in result.steps if step.step_type == "remove_stale_file_gfid"
        )
        self.assertEqual(
            "/gluster/gtestlab/gtest/brick/.glusterfs/aa/aa/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            missing_gfid_step.target_path,
        )

    def test_missing_file_replica_canary_triggers_full_heal_crawl(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file.canary_api._brick_roles", return_value={"host-a": "data", "host-b": "data", "host-c": "data"}), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount, \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch(
                "gluster_heal_tool.canary_file._resolve_mount_file_gfid",
                return_value=(
                    "11111111-2222-3333-4444-555555555555",
                    "/srv/gluster/brick-store/gtest/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
                    "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                ),
            ), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file.run_heal") as run_heal_mock, \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file._print_response"):
            create_missing_file_replica_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                missing_host="host-b",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["host-b"], [host for host, _ops in calls])
        run_heal_mock.assert_called_once_with("gtest")

    def test_missing_file_replica_canary_can_leave_heal_pending(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, list(request.ops)))
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")),             patch("gluster_heal_tool.canary_file.canary_api._brick_roles", return_value={"host-a": "data", "host-b": "data", "host-c": "data"}),             patch("gluster_heal_tool.canary_file._run_local"),             patch("gluster_heal_tool.canary_file._ensure_canary_mount"),             patch("gluster_heal_tool.canary_file._local_mount_dir"),             patch("gluster_heal_tool.canary_file._local_mount_file"),             patch(
                "gluster_heal_tool.canary_file._resolve_mount_file_gfid",
                return_value=(
                    "11111111-2222-3333-4444-555555555555",
                    "/srv/gluster/brick-store/gtest/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
                    "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                ),
            ),             patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker),             patch("gluster_heal_tool.canary_file.run_heal") as run_heal_mock,             patch("gluster_heal_tool.canary_file.set_heal_settings") as set_heal_mock,             patch("gluster_heal_tool.canary_file._write_state"),             patch("gluster_heal_tool.canary_file._print_response"):
            create_missing_file_replica_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                missing_host="host-b",
                dir_name="alpha",
                file_name="payload.txt",
                trigger_heal=False,
            )

        self.assertEqual(["host-b"], [host for host, _ops in calls])
        run_heal_mock.assert_not_called()
        set_heal_mock.assert_called_once_with("gtest", False)

    def test_missing_file_replica_canary_rejects_an_arbiter_target(self) -> None:
        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["data-a", "data-b", "arbiter"],
                {
                    "data-a": "/srv/gluster/brick-store/gtest/brick",
                    "data-b": "/srv/gluster/brick-store/gtest/brick",
                    "arbiter": "/srv/gluster/brick-store/gtest/arbiter",
                },
            ),
        ), patch(
            "gluster_heal_tool.canary_file.canary_api._brick_roles",
            return_value={"data-a": "data", "data-b": "data", "arbiter": "arbiter"},
        ):
            with self.assertRaisesRegex(RuntimeError, "requires a data brick"):
                create_missing_file_replica_canary(
                    volume="gtest3a",
                    scenario="repair-canary-arbiter-guard",
                    missing_host="arbiter",
                    dir_name="alpha",
                    file_name="payload.txt",
                )

    def test_arbiter_missing_gfid_canary_removes_only_arbiter_metadata(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []
        states: list[dict[str, object]] = []
        canonical_gfid = "11111111-2222-3333-4444-555555555555"

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertEqual(10, connect_timeout)
            ops = list(request.ops)
            calls.append((host, ops))
            if ops[0]["op"] == "inspect_path":
                return {
                    "host": host,
                    "results": [{"op": "inspect_path", "ok": True, "trusted_gfid": canonical_gfid}],
                }
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in ops]}

        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["data-a", "data-b", "arbiter"],
                {
                    "data-a": "/gluster/data-a/gtest3a/brick",
                    "data-b": "/gluster/data-b/gtest3a/brick",
                    "arbiter": "/gluster/arbiter/gtest3a/brick",
                },
            ),
        ), patch(
            "gluster_heal_tool.canary_file.canary_api._brick_roles",
            return_value={"data-a": "data", "data-b": "data", "arbiter": "arbiter"},
        ), patch("gluster_heal_tool.canary_file._ensure_canary_mount"), patch(
            "gluster_heal_tool.canary_file._local_mount_dir"
        ), patch("gluster_heal_tool.canary_file._local_mount_file"), patch(
            "gluster_heal_tool.canary_file._run_local"
        ), patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), patch(
            "gluster_heal_tool.canary_file.set_heal_settings"
        ) as set_heal_mock, patch(
            "gluster_heal_tool.canary_file._write_state", side_effect=lambda _volume, _scenario, state: states.append(state)
        ), patch("gluster_heal_tool.canary_file._record_file_baseline") as baseline_mock:
            canary_file.create_arbiter_missing_gfid_canary(
                volume="gtest3a",
                scenario="arbiter-gfid-file",
                object_type="file",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["data-a", "data-b", "arbiter"], [host for host, _ops in calls])
        self.assertEqual(
            [
                {"op": "removexattr", "path": "/gluster/arbiter/gtest3a/brick/arbiter-gfid-file/alpha/payload.txt", "name": "trusted.gfid"},
                {"op": "rm", "path": "/gluster/arbiter/gtest3a/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555", "recursive": False, "force": True},
            ],
            calls[-1][1],
        )
        self.assertEqual("arbiter-missing-gfid", states[0]["kind"])
        self.assertTrue(states[0]["partial"])
        self.assertTrue(states[0]["restore_heal_settings"])
        self.assertEqual(canonical_gfid, states[-1]["gfid_uuid"])
        self.assertFalse(states[-1]["partial"])
        self.assertTrue(states[-1]["leave_heal_pending"])
        baseline_mock.assert_not_called()
        set_heal_mock.assert_called_once_with("gtest3a", False)

    def test_arbiter_missing_gfid_failure_keeps_recoverable_partial_state(self) -> None:
        states: list[dict[str, object]] = []
        canonical_gfid = "11111111-2222-3333-4444-555555555555"

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            ops = list(request.ops)
            if ops[0]["op"] == "inspect_path":
                return {
                    "host": host,
                    "results": [{"op": "inspect_path", "ok": True, "trusted_gfid": canonical_gfid}],
                }
            return {
                "host": host,
                "results": [
                    {"op": "removexattr", "ok": True},
                    {"op": "rm", "ok": False, "error": "injected removal failure"},
                ],
            }

        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["data-a", "data-b", "arbiter"],
                {
                    "data-a": "/gluster/data-a/gtest3a/brick",
                    "data-b": "/gluster/data-b/gtest3a/brick",
                    "arbiter": "/gluster/arbiter/gtest3a/brick",
                },
            ),
        ), patch(
            "gluster_heal_tool.canary_file.canary_api._brick_roles",
            return_value={"data-a": "data", "data-b": "data", "arbiter": "arbiter"},
        ), patch("gluster_heal_tool.canary_file._ensure_canary_mount"), patch(
            "gluster_heal_tool.canary_file._local_mount_dir"
        ), patch("gluster_heal_tool.canary_file._local_mount_file"), patch(
            "gluster_heal_tool.canary_file._run_local"
        ), patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), patch(
            "gluster_heal_tool.canary_file.set_heal_settings"
        ) as set_heal_mock, patch(
            "gluster_heal_tool.canary_file._write_state", side_effect=lambda _volume, _scenario, state: states.append(state)
        ), patch("gluster_heal_tool.canary_file._unmount_canary_mount") as unmount_mock, patch(
            "gluster_heal_tool.canary_file._local_remove"
        ) as remove_mock:
            with self.assertRaisesRegex(RuntimeError, "injected removal failure"):
                canary_file.create_arbiter_missing_gfid_canary(
                    volume="gtest3a",
                    scenario="arbiter-gfid-failure",
                    object_type="file",
                    dir_name="alpha",
                    file_name="payload.txt",
                )

        self.assertTrue(states[-1]["partial"])
        self.assertEqual(canonical_gfid, states[-1]["gfid_uuid"])
        self.assertEqual(
            "/gluster/arbiter/gtest3a/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
            states[-1]["arbiter_handle"],
        )
        set_heal_mock.assert_has_calls([call("gtest3a", False), call("gtest3a", True)])
        unmount_mock.assert_called_once()
        remove_mock.assert_called_once()

    def test_arbiter_only_residue_canary_removes_only_data_copies(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []
        states: list[dict[str, object]] = []
        canonical_gfid = "11111111-2222-3333-4444-555555555555"

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertEqual(10, connect_timeout)
            ops = list(request.ops)
            calls.append((host, ops))
            if ops[0]["op"] == "inspect_path":
                return {
                    "host": host,
                    "results": [{"op": "inspect_path", "ok": True, "trusted_gfid": canonical_gfid}],
                }
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in ops]}

        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["data-a", "data-b", "arbiter"],
                {
                    "data-a": "/gluster/data-a/gtest3a/brick",
                    "data-b": "/gluster/data-b/gtest3a/brick",
                    "arbiter": "/gluster/arbiter/gtest3a/brick",
                },
            ),
        ), patch(
            "gluster_heal_tool.canary_file.canary_api._brick_roles",
            return_value={"data-a": "data", "data-b": "data", "arbiter": "arbiter"},
        ), patch("gluster_heal_tool.canary_file._ensure_canary_mount"), patch(
            "gluster_heal_tool.canary_file._local_mount_dir"
        ), patch("gluster_heal_tool.canary_file._local_mount_file"), patch(
            "gluster_heal_tool.canary_file._run_local"
        ), patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), patch(
            "gluster_heal_tool.canary_file.set_heal_settings"
        ) as set_heal_mock, patch(
            "gluster_heal_tool.canary_file._write_state", side_effect=lambda _volume, _scenario, state: states.append(state)
        ), patch("gluster_heal_tool.canary_file._record_file_baseline") as baseline_mock:
            canary_file.create_arbiter_only_residue_canary(
                volume="gtest3a",
                scenario="arbiter-only-file",
                object_type="file",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["data-a", "data-b", "arbiter", "data-a", "data-b"], [host for host, _ops in calls])
        self.assertEqual(
            [
                {"op": "rm", "path": "/gluster/data-a/gtest3a/brick/arbiter-only-file/alpha/payload.txt", "recursive": False, "force": True},
                {"op": "rm", "path": "/gluster/data-a/gtest3a/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555", "recursive": False, "force": True},
            ],
            calls[-2][1],
        )
        self.assertEqual("arbiter-only-residue", states[0]["kind"])
        self.assertTrue(states[0]["partial"])
        self.assertTrue(states[0]["restore_heal_settings"])
        self.assertEqual("arbiter", states[-1]["arbiter_host"])
        self.assertEqual(canonical_gfid, states[-1]["gfid_uuid"])
        self.assertFalse(states[-1]["partial"])
        baseline_mock.assert_not_called()
        set_heal_mock.assert_called_once_with("gtest3a", False)

    def test_canary_heal_crawl_retries_after_daemon_disabled(self) -> None:
        with patch("gluster_heal_tool.canary_file.run_heal", side_effect=[
            RuntimeError("Self-heal-daemon is disabled; Heal will not be triggered on volume gtest"),
            None,
        ]) as run_heal_mock, patch("gluster_heal_tool.canary_file.set_heal_settings") as set_heal_mock:
            canary_file._trigger_canary_heal("gtest", "repair-canary-speed")

        self.assertEqual(2, run_heal_mock.call_count)
        set_heal_mock.assert_called_once_with("gtest", True)

    def test_file_child_gap_build_uses_explicit_child_gap_strategy(self) -> None:
        state = {
            "kind": "file-child-gap",
            "volume": "gtest",
            "scenario": "repair-canary-file-child-gap",
            "missing_host": "brick-c",
            "source_host": "brick-a",
            "dir_name": "alpha",
            "file_name": "payload.txt",
            "mount_root": "/gtest",
            "mount_dir": "/gtest/repair-canary-file-child-gap/alpha",
            "mount_file": "/gtest/repair-canary-file-child-gap/alpha/payload.txt",
            "backend_root": "/srv/gluster/brick-store/gtest",
            "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-file-child-gap/alpha/payload.txt",
            "index_root": "/srv/gluster/brick-store/gtest/.glusterfs/indices/xattrop",
            "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/aa/bb/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "gfid_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }

        with patch("gluster_heal_tool.canary_file._read_state", return_value=state):
            plan = build_file_child_gap_plan_from_canary_state(volume="gtest", scenario="repair-canary-file-child-gap")

        _assert_canary_state_provenance(
            self,
            plan,
            kind="file-child-gap",
            volume="gtest",
            scenario="repair-canary-file-child-gap",
        )
        self.assertEqual(1, len(plan["actions"]))
        action = plan["actions"][0]
        self.assertEqual("repair_file", action["action_type"])
        self.assertEqual("restore_missing_child_replica", action["repair_strategy"])
        self.assertEqual(["brick-c"], action["missing_hosts"])
        self.assertEqual(["brick-a"], action["healthy_hosts"])
        self.assertIn("file_child_gap:present", action["graph_markers"])
        self.assertEqual(
            default_canary_stage_local_path("repair-canary-file-child-gap", "payload.txt"),
            action["stage_local_path"],
        )

    def test_file_child_gap_multi_build_emits_one_repair_per_missing_child(self) -> None:
        state = {
            "kind": "file-child-gap-multi",
            "volume": "gtest",
            "scenario": "repair-canary-file-child-gap-multi",
            "missing_host": "brick-c",
            "source_host": "brick-a",
            "dir_name": "alpha",
            "mount_root": "/gtest",
            "mount_dir": "/gtest/repair-canary-file-child-gap-multi/alpha",
            "backend_root": "/srv/gluster/brick-store/gtest",
            "index_root": "/srv/gluster/brick-store/gtest/.glusterfs/indices/xattrop",
            "files": [
                {
                    "file_name": "payload-a.txt",
                    "mount_file": "/gtest/repair-canary-file-child-gap-multi/alpha/payload-a.txt",
                    "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-file-child-gap-multi/alpha/payload-a.txt",
                    "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/aa/bb/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "gfid_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
                {
                    "file_name": "payload-b.txt",
                    "mount_file": "/gtest/repair-canary-file-child-gap-multi/alpha/payload-b.txt",
                    "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-file-child-gap-multi/alpha/payload-b.txt",
                    "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/cc/dd/cccccccc-dddd-eeee-ffff-000000000000",
                    "gfid_uuid": "cccccccc-dddd-eeee-ffff-000000000000",
                },
            ],
        }

        with patch("gluster_heal_tool.canary_file._read_state", return_value=state):
            plan = build_file_child_gap_plan_from_canary_state(
                volume="gtest",
                scenario="repair-canary-file-child-gap-multi",
            )

        _assert_canary_state_provenance(
            self,
            plan,
            kind="file-child-gap-multi",
            volume="gtest",
            scenario="repair-canary-file-child-gap-multi",
        )
        self.assertEqual(2, len(plan["actions"]))
        self.assertEqual(
            ["/gtest/repair-canary-file-child-gap-multi/alpha/payload-a.txt", "/gtest/repair-canary-file-child-gap-multi/alpha/payload-b.txt"],
            [action["logical_path"] for action in plan["actions"]],
        )
        self.assertTrue(all(action["repair_strategy"] == "restore_missing_child_replica" for action in plan["actions"]))
        self.assertTrue(all(action["missing_hosts"] == ["brick-c"] for action in plan["actions"]))
        self.assertTrue(all(action["healthy_hosts"] == ["brick-a"] for action in plan["actions"]))

    def test_file_child_gap_multi_cleanup_removes_each_file_gfid(self) -> None:
        state = {
            "kind": "file-child-gap-multi",
            "volume": "gtest",
            "scenario": "repair-canary-file-child-gap-multi",
            "missing_host": "brick-c",
            "source_host": "brick-a",
            "backend_root": "/srv/gluster/brick-store/gtest",
            "index_root": "/srv/gluster/brick-store/gtest/.glusterfs/indices/xattrop",
            "index_hosts": ["brick-a"],
            "files": [
                {
                    "file_name": "payload-a.txt",
                    "mount_file": "/gtest/repair-canary-file-child-gap-multi/alpha/payload-a.txt",
                    "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-file-child-gap-multi/alpha/payload-a.txt",
                    "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/aa/bb/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "gfid_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
                {
                    "file_name": "payload-b.txt",
                    "mount_file": "/gtest/repair-canary-file-child-gap-multi/alpha/payload-b.txt",
                    "backend_target": "/srv/gluster/brick-store/gtest/repair-canary-file-child-gap-multi/alpha/payload-b.txt",
                    "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/cc/dd/cccccccc-dddd-eeee-ffff-000000000000",
                    "gfid_uuid": "cccccccc-dddd-eeee-ffff-000000000000",
                },
            ],
        }

        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-file-child-gap-multi",
            kind="file-child-gap-multi",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest",
            brick_hosts=["brick-a", "brick-b", "brick-c"],
            ssh_user="gluster-repair",
            state=state,
        )

        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual(2, sum(1 for step in result.steps if step.step_type == "remove_stale_file_gfid"))
        self.assertEqual(2, sum(1 for step in result.steps if step.step_type == "remove_stale_gfid"))
        self.assertTrue(all("/.glusterfs/indices/xattrop/" in step.target_path for step in result.steps if step.step_type == "remove_stale_gfid"))

    def test_file_child_gap_multi_canary_records_all_missing_children(self) -> None:
        source_results = [
            {"op": "touch_index_from_target", "ok": True, "value_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            {"op": "touch_index_from_target", "ok": True, "value_uuid": "cccccccc-dddd-eeee-ffff-000000000000"},
        ]
        worker_responses = [
            {"host": "brick-a", "results": [source_results[0]]},
            {"host": "brick-c", "results": [{"op": "rm", "ok": True}]},
            {"host": "brick-a", "results": [source_results[1]]},
            {"host": "brick-c", "results": [{"op": "rm", "ok": True}]},
        ]

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["brick-a", "brick-b", "brick-c"], "/srv/gluster/brick-store/gtest")), patch(
            "gluster_heal_tool.canary_file._select_role_host", return_value="brick-c"
        ), patch("gluster_heal_tool.canary_file._run_local"), patch("gluster_heal_tool.canary_file._local_mount_dir"), patch(
            "gluster_heal_tool.canary_file._local_mount_file"
        ), patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker_responses), patch(
            "gluster_heal_tool.canary_file._print_response"
        ), patch("gluster_heal_tool.canary_file._write_state") as write_state:
            create_file_child_gap_multi_canary(
                volume="gtest",
                scenario="repair-canary-file-child-gap-multi",
                missing_host="brick-c",
                dir_name="alpha",
                file_names=["payload-a.txt", "payload-b.txt"],
            )

        write_state.assert_called_once()
        payload = write_state.call_args.args[2]
        self.assertEqual("file-child-gap-multi", payload["kind"])
        self.assertTrue(payload["leave_heal_pending"])
        self.assertEqual(["brick-a"], payload["index_hosts"])
        self.assertEqual(2, len(payload["files"]))
        self.assertEqual(["payload-a.txt", "payload-b.txt"], payload["file_names"])

    def test_file_stale_survivor_pair_canary_records_two_survivors(self) -> None:
        survivor_results = [
            {"op": "touch", "ok": True},
            {"op": "setxattr", "ok": True},
            {
                "op": "link_file_gfid_from_target",
                "ok": True,
                "value_uuid": "11111111-2222-3333-4444-555555555555",
                "file_gfid_path": "/srv/gluster/brick-store/gtest/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
            },
            {"op": "touch_index_from_target", "ok": True, "value_uuid": "11111111-2222-3333-4444-555555555555"},
        ]
        worker_responses = [
            {"host": "brick-a", "results": survivor_results},
            {"host": "brick-c", "results": [{"op": "rm", "ok": True}]},
            {"host": "brick-d", "results": [{"op": "rm", "ok": True}]},
        ]

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["brick-a", "brick-b", "brick-c", "brick-d"], "/srv/gluster/brick-store/gtest")), patch("gluster_heal_tool.canary_file.canary_api._brick_roles_by_host", return_value={"brick-a": "data", "brick-b": "data", "brick-c": "data", "brick-d": "data"}), patch(
            "gluster_heal_tool.canary_file._run_local"
        ), patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount, patch("gluster_heal_tool.canary_file._local_mount_dir"), patch(
            "gluster_heal_tool.canary_file._canary_worker_command", side_effect=worker_responses
        ), patch("gluster_heal_tool.canary_file._print_response"), patch("gluster_heal_tool.canary_file._record_file_baseline") as record_baseline, patch(
            "gluster_heal_tool.canary_file.run_heal"
        ) as run_heal_mock:
            create_file_stale_survivor_pair_canary(
                volume="gtest",
                scenario="repair-canary-file-stale-survivor-pair",
                dir_name="alpha",
                file_name="payload.txt",
            )

        record_baseline.assert_called_once()
        run_heal_mock.assert_called_once_with("gtest")
        payload = record_baseline.call_args.args[2]
        self.assertEqual("file-stale-survivor", payload["kind"])
        self.assertEqual(["brick-a", "brick-b"], payload["survivor_hosts"])
        self.assertEqual(["brick-c", "brick-d"], payload["remove_hosts"])
        self.assertNotIn("stale_backends_by_host", payload)
        self.assertNotIn("stale_file_gfid_paths_by_host", payload)
        self.assertNotIn("stale_gfid_paths_by_host", payload)
        self.assertEqual(
            {
                "stale_backends_by_host": {
                    "brick-a": [payload["backend_target"]],
                    "brick-b": [payload["backend_target"]],
                },
                "stale_file_gfid_paths_by_host": {
                    "brick-a": [payload["file_gfid_path"]],
                    "brick-b": [payload["file_gfid_path"]],
                },
                "stale_gfid_paths_by_host": {
                    "brick-a": [f"{payload['index_root']}/{payload['gfid_uuid']}"],
                    "brick-b": [f"{payload['index_root']}/{payload['gfid_uuid']}"],
                },
            },
            payload["cleanup_hints"],
        )

    def test_directory_backend_child_gap_canonical_build_uses_canonical_content_recreate(self) -> None:
        state = {
            "kind": "directory-backend-child-gap-canonical",
            "volume": "gtest",
            "scenario": "repair-canary-dir-gap-backend-canonical",
            "missing_host": "brick-c",
            "parent_name": "alpha",
            "present_child": "beta",
            "missing_child": "gamma",
            "mount_root": "/gtest",
            "mount_parent": "/gtest/repair-canary-dir-gap-backend-canonical/alpha/beta",
            "mount_path": "/gtest/repair-canary-dir-gap-backend-canonical/alpha/beta/gamma",
            "backend_root": "/srv/gluster/brick-store/gtest",
            "backend_scenario_root": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-canonical",
            "missing_backend": "/srv/gluster/brick-store/gtest/repair-canary-dir-gap-backend-canonical/alpha/beta/gamma",
            "backend_observations": [
                {
                    "host": "brick-a",
                    "results": [
                        {
                            "label": "backend_missing_child",
                            "ok": True,
                            "trusted_gfid": "6a36631d-94c2-46b4-b8e4-66646840389a",
                        }
                    ],
                }
            ],
        }

        with patch("gluster_heal_tool.canary_directory.canary_api._read_state", return_value=state):
            plan = build_directory_backend_child_gap_plan_from_canary_state(
                volume="gtest", scenario="repair-canary-dir-gap-backend-canonical"
            )

        _assert_canary_state_provenance(
            self,
            plan,
            kind="directory-backend-child-gap-canonical",
            volume="gtest",
            scenario="repair-canary-dir-gap-backend-canonical",
        )
        self.assertEqual(1, len(plan["actions"]))
        action = plan["actions"][0]
        self.assertEqual("repair_directory_metadata", action["action_type"])
        self.assertEqual("canonical_content_recreate", action["repair_strategy"])
        self.assertEqual(["brick-c"], action["missing_hosts"])
        self.assertEqual(["brick-a"], action["healthy_hosts"])
        self.assertIn("canonical content recreate canary", action["notes"])

    def test_trusted_gfid_decodes_to_uuid_string(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "brick" / "repair-canary-speed" / "alpha"
            target.mkdir(parents=True)

            def fake_getxattr(path, name):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                return raw_gfid.bytes

            with patch("gluster_heal_tool.worker.os.path.exists", return_value=True), patch(
                "gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr
            ):
                self.assertEqual("6a36631d-94c2-46b4-b8e4-66646840389a", _trusted_gfid(str(target)))

    def test_trusted_gfid_decodes_textual_uuid_string(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "brick" / "repair-canary-speed" / "alpha"
            target.mkdir(parents=True)

            def fake_getxattr(path, name):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                return b"6a36631d-94c2-46b4-b8e4-66646840389a"

            with patch("gluster_heal_tool.worker.os.path.exists", return_value=True), patch(
                "gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr
            ):
                self.assertEqual("6a36631d-94c2-46b4-b8e4-66646840389a", _trusted_gfid(str(target)))

    def test_posix_acl_hex_distinguishes_missing_and_present_acl(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "payload.txt"
            target.touch()

            with patch("gluster_heal_tool.worker.os.getxattr", return_value=b"\x02\x00\x00\x00"):
                value, error = _posix_acl_hex(str(target), "system.posix_acl_access")
            self.assertEqual("0x02000000", value)
            self.assertEqual("", error)

            missing = OSError()
            missing.errno = 61
            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=missing):
                value, error = _posix_acl_hex(str(target), "system.posix_acl_access")
            self.assertEqual("", value)
            self.assertEqual("", error)

    def test_resolve_one_carries_posix_acl_evidence_into_observation(self) -> None:
        request = ResolveBatchRequest(
            volume="gtest3",
            brick_path="/brick",
            mountpoint="/gtest3",
            resolver_path="/resolver",
            entries=["/alpha/payload.txt"],
            probe_mount=False,
        )
        backend_info = {
            "mode_bits": 0o644,
            "uid": 0,
            "gid": 0,
            "acl_access": "0x02000000",
            "acl_default": "",
            "acl_error": "",
        }
        output = "BACKEND=/brick/alpha/payload.txt\nTYPE=file\nRELPATH=alpha/payload.txt\n"
        with patch("gluster_heal_tool.worker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")), patch(
            "gluster_heal_tool.worker._lstat_type", return_value=(True, True, "file", "", 0, 0, "-rw-r--r--")
        ), patch("gluster_heal_tool.worker._inspect_backend_path", return_value=backend_info), patch(
            "gluster_heal_tool.worker._trusted_gfid", return_value=""
        ), patch("gluster_heal_tool.worker._trusted_mdata_hex", return_value=""):
            observation = _resolve_one(request, "/alpha/payload.txt")

        self.assertEqual("0x02000000", observation.backend_acl_access_text)
        self.assertEqual("", observation.backend_acl_default_text)
        self.assertEqual("", observation.backend_acl_error)

    def test_inspect_path_reports_link_count_and_gfid2path(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha" / "payload.txt"
            target.parent.mkdir(parents=True)
            target.write_text("payload\n", encoding="utf-8")
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker._trusted_gfid", return_value="6a36631d-94c2-46b4-b8e4-66646840389a"), patch(
                "gluster_heal_tool.worker._gfid2path_xattr_entry",
                return_value=("trusted.gfid2path.canary", "/gtest/repair-canary-speed/alpha/payload.txt"),
            ):
                result = _run_canary_op(
                    request,
                    {
                        "op": "inspect_path",
                        "label": "file_gfid_path",
                        "path": str(target),
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual("file_gfid_path", result["label"])
            self.assertEqual("file", result["kind"])
            self.assertEqual(1, result["links"])
            self.assertEqual("6a36631d-94c2-46b4-b8e4-66646840389a", result["trusted_gfid"])
            self.assertEqual("trusted.gfid2path.canary", result["gfid2path_xattr"])
            self.assertEqual("/gtest/repair-canary-speed/alpha/payload.txt", result["gfid2path_target"])

    def test_rm_index_from_target_removes_all_name_variants(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "xattrop"
            target.parent.mkdir(parents=True)
            target.touch()
            index_root.mkdir(parents=True)
            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            compact = canonical.replace("-", "")
            (index_root / canonical).touch()
            (index_root / compact).touch()

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "rm_index_from_target",
                        "target_path": str(target),
                        "index_root": str(index_root),
                    },
                )

            self.assertTrue(result["ok"])
            self.assertFalse((index_root / canonical).exists())
            self.assertFalse((index_root / compact).exists())
            self.assertEqual(2, len(result["removed"]))

    def test_link_file_gfid_from_target_creates_hardlink(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha" / "payload.txt"
            target.parent.mkdir(parents=True)
            target.write_text("payload\n", encoding="utf-8")

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "link_file_gfid_from_target",
                        "target_path": str(target),
                    },
                )

            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            gfid_path = backend_root / ".glusterfs" / "6a" / "36" / canonical
            self.assertTrue(result["ok"])
            self.assertEqual(canonical, result["value_uuid"])
            self.assertEqual(str(gfid_path), result["file_gfid_path"])
            self.assertTrue(gfid_path.exists())
            self.assertTrue(target.samefile(gfid_path))

    def test_write_text_writes_only_under_backend_root(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha" / "payload.txt"
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            result = _run_canary_op(
                request,
                {
                    "op": "write_text",
                    "path": str(target),
                    "text": "left cohort\n",
                },
            )

            self.assertTrue(result["ok"])
            self.assertEqual("left cohort\n", target.read_text(encoding="utf-8"))
            self.assertEqual(len("left cohort\n"), result["bytes"])

    def test_write_text_rejects_paths_outside_backend_root(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            outside = Path(tmpdir) / "outside.txt"
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            result = _run_canary_op(
                request,
                {
                    "op": "write_text",
                    "path": str(outside),
                    "text": "nope\n",
                },
            )

            self.assertFalse(result["ok"])
            self.assertIn("not under backend root", result["error"])

    def test_removexattr_allows_trusted_gfid(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            target.mkdir(parents=True)
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            removed: list[tuple[str, str, bool]] = []

            def fake_removexattr(path, name, follow_symlinks=False):  # noqa: ANN001
                removed.append((path, name, follow_symlinks))

            with patch("gluster_heal_tool.worker.os.removexattr", side_effect=fake_removexattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "removexattr",
                        "path": str(target),
                        "name": "trusted.gfid",
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual([(str(target), "trusted.gfid", False)], removed)

    def test_removexattr_rejects_unsafe_xattrs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            target.mkdir(parents=True)
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            result = _run_canary_op(
                request,
                {
                    "op": "removexattr",
                    "path": str(target),
                    "name": "user.not-allowed",
                },
            )

            self.assertFalse(result["ok"])
            self.assertIn("only allows trusted.gfid", result["error"])

    def test_link_file_gfid_from_target_creates_hashed_glusterfs_link(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha" / "payload.txt"
            target.parent.mkdir(parents=True)
            target.write_text("payload\n", encoding="utf-8")

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "link_file_gfid_from_target",
                        "target_path": str(target),
                    },
                )

            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            gfid_path = backend_root / ".glusterfs" / "6a" / "36" / canonical
            self.assertTrue(result["ok"])
            self.assertEqual(str(gfid_path), result["file_gfid_path"])
            self.assertTrue(gfid_path.exists())
            self.assertTrue(gfid_path.samefile(target))

    def test_link_file_gfid_from_target_rejects_directories(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            target.mkdir(parents=True)
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            result = _run_canary_op(
                request,
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": str(target),
                },
            )

            self.assertFalse(result["ok"])
            self.assertIn("regular files", result["error"])

    def test_link_directory_gfid_from_target_creates_hashed_glusterfs_symlink(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            target = backend_root / "repair-canary-speed" / "alpha"
            target.mkdir(parents=True)

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(target), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "link_directory_gfid_from_target",
                        "target_path": str(target),
                    },
                )

            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            gfid_path = backend_root / ".glusterfs" / "6a" / "36" / canonical
            self.assertTrue(result["ok"])
            self.assertEqual(str(gfid_path), result["directory_gfid_path"])
            self.assertTrue(gfid_path.is_symlink())
            self.assertEqual(str(target), gfid_path.readlink().__fspath__())

    def test_touch_entry_changes_index_from_parent_uses_parent_gfid_and_child_name(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            parent = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "entry-changes"
            parent.mkdir(parents=True)

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(parent), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "touch_entry_changes_index_from_parent",
                        "parent_path": str(parent),
                        "index_root": str(index_root),
                        "child_name": "payload.txt",
                    },
                )

            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            self.assertTrue(result["ok"])
            self.assertEqual(canonical, result["parent_gfid_uuid"])
            self.assertTrue((index_root / canonical / "payload.txt").exists())

    def test_rm_entry_changes_index_from_parent_removes_child_and_empty_parent(self) -> None:
        raw_gfid = uuid.UUID("6a36631d-94c2-46b4-b8e4-66646840389a")
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            parent = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "entry-changes"
            canonical = "6a36631d-94c2-46b4-b8e4-66646840389a"
            parent.mkdir(parents=True)
            (index_root / canonical).mkdir(parents=True)
            (index_root / canonical / "payload.txt").touch()

            def fake_getxattr(path, name, follow_symlinks=False):  # noqa: ANN001
                self.assertEqual(str(parent), path)
                self.assertEqual("trusted.gfid", name)
                self.assertFalse(follow_symlinks)
                return raw_gfid.bytes

            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            with patch("gluster_heal_tool.worker.os.getxattr", side_effect=fake_getxattr):
                result = _run_canary_op(
                    request,
                    {
                        "op": "rm_entry_changes_index_from_parent",
                        "parent_path": str(parent),
                        "index_root": str(index_root),
                        "child_name": "payload.txt",
                    },
                )

            self.assertTrue(result["ok"])
            self.assertFalse((index_root / canonical / "payload.txt").exists())
            self.assertFalse((index_root / canonical).exists())

    def test_entry_changes_index_rejects_nested_child_names(self) -> None:
        with TemporaryDirectory() as tmpdir:
            backend_root = Path(tmpdir) / "brick"
            parent = backend_root / "repair-canary-speed" / "alpha"
            index_root = backend_root / ".glusterfs" / "indices" / "entry-changes"
            parent.mkdir(parents=True)
            request = CanaryBatchRequest(
                volume="gtest",
                scenario="repair-canary-speed",
                backend_root=str(backend_root),
                ops=[],
            )

            result = _run_canary_op(
                request,
                {
                    "op": "touch_entry_changes_index_from_parent",
                    "parent_path": str(parent),
                    "index_root": str(index_root),
                    "child_name": "nested/payload.txt",
                },
            )

            self.assertFalse(result["ok"])
            self.assertIn("single path component", result["error"])

    def test_directory_metadata_canary_stamps_all_non_mismatch_hosts(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.gfid":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                if op["op"] == "getxattr" and op["name"] == "trusted.glusterfs.mdata":
                    item["value_hex"] = "0x01020304"
                results.append(item)
            return {"host": host, "results": results}

        with patch("gluster_heal_tool.canary_shared._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_shared._run_local"), \
            patch("gluster_heal_tool.canary_shared._local_mount_dir"), \
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_shared._write_state"), \
            patch("gluster_heal_tool.canary_shared._print_response"):
            create_directory_metadata_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                mismatch_host="host-c",
                dir_name="alpha",
            )

        self.assertEqual(["host-a", "host-a", "host-b", "host-c"], [host for host, _ops in calls])
        self.assertEqual("getxattr", calls[0][1][0]["op"])
        self.assertEqual("trusted.gfid", calls[0][1][0]["name"])
        self.assertEqual("getxattr", calls[0][1][1]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[0][1][1]["name"])
        self.assertEqual("setxattr", calls[1][1][0]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[1][1][0]["name"])
        self.assertEqual("setxattr", calls[2][1][0]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[2][1][0]["name"])
        self.assertEqual("removexattr", calls[3][1][0]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[3][1][0]["name"])

    def test_directory_mdata_no_majority_canary_stamps_distinct_host_values(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []
        written_state: list[dict[str, object]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.gfid":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                results.append(item)
            return {"host": host, "results": results}

        def fake_write_state(volume, scenario, payload):  # noqa: ANN001
            written_state.append(payload)

        with patch(
            "gluster_heal_tool.canary_directory._brick_hosts_and_backend_roots",
            return_value=(
                ["host-a", "host-b", "host-c"],
                {
                    "host-a": "/gluster/a/brick",
                    "host-b": "/gluster/b/brick",
                    "host-c": "/gluster/c/brick",
                },
            ),
        ), patch("gluster_heal_tool.canary_directory.canary_api._run_local"), patch(
            "gluster_heal_tool.canary_directory._ensure_canary_mount"
        ), patch("gluster_heal_tool.canary_directory.canary_api._local_mount_dir"), patch(
            "gluster_heal_tool.canary_directory.canary_api._canary_worker_command", side_effect=fake_worker
        ), patch("gluster_heal_tool.canary_directory.canary_api._write_state", side_effect=fake_write_state), patch(
            "gluster_heal_tool.canary_directory.canary_api._print_response"
        ):
            create_directory_mdata_no_majority_canary(
                volume="gtest3",
                scenario="repair-canary-mdata-tie",
                dir_name="alpha",
            )

        self.assertEqual(["host-a", "host-a", "host-b", "host-c"], [host for host, _ops in calls])
        self.assertEqual("getxattr", calls[0][1][0]["op"])
        set_ops = [ops[0] for _host, ops in calls[1:]]
        self.assertEqual(
            ["0x00000001", "0x00000002", "0x00000003"],
            [op["value_hex"] for op in set_ops],
        )
        self.assertEqual(
            [
                "/gluster/a/brick/repair-canary-mdata-tie/alpha",
                "/gluster/b/brick/repair-canary-mdata-tie/alpha",
                "/gluster/c/brick/repair-canary-mdata-tie/alpha",
            ],
            [op["path"] for op in set_ops],
        )
        self.assertEqual("directory-mdata-no-majority", written_state[0]["kind"])
        self.assertEqual(
            {
                "host-a": "0x00000001",
                "host-b": "0x00000002",
                "host-c": "0x00000003",
            },
            written_state[0]["mdata_by_host"],
        )




    def test_directory_mdata_no_majority_plan_bridge_exposes_source_choice_evidence(self) -> None:
        state = {
            "kind": "directory-mdata-no-majority",
            "volume": "gtest3",
            "scenario": "repair-canary-mdata-tie",
            "mount_root": "/gtest3",
            "mount_target": "/gtest3/repair-canary-mdata-tie/alpha",
            "backend_roots": {
                "host-a": "/gluster/a/brick",
                "host-b": "/gluster/b/brick",
                "host-c": "/gluster/c/brick",
            },
            "backend_targets_by_host": {
                "host-a": "/gluster/a/brick/repair-canary-mdata-tie/alpha",
                "host-b": "/gluster/b/brick/repair-canary-mdata-tie/alpha",
                "host-c": "/gluster/c/brick/repair-canary-mdata-tie/alpha",
            },
            "marker_host": "host-a",
            "gfid_uuid": "6a36631d-94c2-46b4-b8e4-66646840389a",
            "directory_gfid_path": "/gluster/a/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            "mdata_by_host": {
                "host-a": "0x00000001",
                "host-b": "0x00000002",
                "host-c": "0x00000003",
            },
            "brick_roles_by_host": {"host-a": "data", "host-b": "arbiter", "host-c": "data"},
        }

        with patch("gluster_heal_tool.canary_shared._read_state", return_value=state):
            plan = build_directory_mdata_no_majority_plan_from_canary_state(
                volume="gtest3",
                scenario="repair-canary-mdata-tie",
            )

        _assert_canary_state_provenance(
            self,
            plan,
            kind="directory-mdata-no-majority",
            volume="gtest3",
            scenario="repair-canary-mdata-tie",
        )
        action = plan["actions"][0]
        self.assertEqual("review_directory_metadata", action["action_type"])
        self.assertEqual("review_directory_mdata_state", action["repair_strategy"])
        self.assertEqual("host-a", action["directory_canonical_host"])
        self.assertEqual("6a36631d-94c2-46b4-b8e4-66646840389a", action["directory_canonical_gfid"])
        self.assertEqual(
            {
                "host-a": "0x00000001",
                "host-b": "0x00000002",
                "host-c": "0x00000003",
            },
            action["directory_mdata_by_host"],
        )
        self.assertEqual(["host-a", "host-b", "host-c"], action["directory_metadata_mismatch_hosts"])
        notes = "\n".join(action["notes"])
        self.assertIn("directory-mdata-no-majority marker: review-only source-choice proof", notes)
        self.assertIn("mdata by value:", notes)
        self.assertIn("brick roles by host:", notes)
        self.assertIn("host-b: arbiter", notes)
        self.assertEqual({"host-a": "data", "host-b": "arbiter", "host-c": "data"}, action["brick_roles_by_host"])
        self.assertIn(
            "Expected: keep review-only; choose mdata_source_host or mdata_source_value before any repair claim.",
            notes,
        )

    def test_file_metadata_split_brain_canary_uses_two_cohorts(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []
        canonical_gfid = "11111111-2222-3333-4444-555555555555"
        expected_index_root = "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop"

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.glusterfs.mdata":
                    item["value_hex"] = "0x01020304"
                if op["op"] == "set_afr_metadata_pending":
                    item["afr_xattrs"] = [
                        {
                            "name": name,
                            "value_hex": f"0x{op['value_hex'].removeprefix('0x')}",
                            "value_bytes": 12,
                        }
                        for name in op["pending_names"]
                    ]
                    item["afr_xattr_names"] = [entry["name"] for entry in item["afr_xattrs"]]
                if op["op"] == "link_xattrop_gfid":
                    item["value_uuid"] = canonical_gfid
                    item["value_hex"] = canonical_gfid.replace("-", "")
                    item["index_entry"] = f"{expected_index_root}/{canonical_gfid}"
                    item["xattrop_entry"] = item["index_entry"]
                if op["op"] == "link_file_gfid_from_target":
                    item["file_gfid_path"] = canary_file._file_gfid_link_path(request.backend_root, canonical_gfid)
                if op["op"] == "inspect_afr_state":
                    if host in {"host-a", "host-b"}:
                        item["afr_xattrs"] = [
                            {"name": "trusted.afr.gtest-client-2", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                            {"name": "trusted.afr.gtest-client-3", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                        ]
                    else:
                        item["afr_xattrs"] = [
                            {"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                            {"name": "trusted.afr.gtest-client-1", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                        ]
                    item["afr_xattr_names"] = [entry["name"] for entry in item["afr_xattrs"]]
                results.append(item)
            return {"host": host, "results": results}

        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(["host-a", "host-b", "host-c", "host-d"], {"host-a": "/srv/gluster/brick-store/gtest/brick", "host-b": "/srv/gluster/brick-store/gtest/brick", "host-c": "/srv/gluster/brick-store/gtest/brick", "host-d": "/srv/gluster/brick-store/gtest/brick"})),
            patch("gluster_heal_tool.canary_file._run_local"),
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=["heal info before\n" + canary_file._canary_temp_mount_root("gtest") + "/repair-canary-speed/alpha/payload.txt", "split-brain before", "heal info after\n" + canary_file._canary_temp_mount_root("gtest") + "/repair-canary-speed/alpha/payload.txt", "split-brain after"]),
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker),
            patch("gluster_heal_tool.canary_file._record_file_baseline") as record_baseline,
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
            patch("gluster_heal_tool.canary_file.run_heal") as run_heal_mock,
            patch("gluster_heal_tool.canary_file._print_response"),
        ):
            create_file_metadata_split_brain_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                mismatch_host="host-a",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["host-a", "host-a", "host-b", "host-c", "host-d", "host-a"], [host for host, _ops in calls])
        self.assertEqual("getxattr", calls[0][1][0]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[0][1][0]["name"])
        self.assertTrue(any(op["op"] == "setxattr" and op["name"] == "trusted.glusterfs.mdata" and op["value_hex"] == "0x01020304" for op in calls[1][1]))
        self.assertTrue(any(op["op"] == "setxattr" and op["name"] == "trusted.glusterfs.mdata" for op in calls[3][1]))
        self.assertTrue(
            any(
                op["op"] == "set_afr_metadata_pending"
                and op["value_hex"] == "000000000000000100000000"
                and len(op["pending_names"]) == 2
                for op in calls[1][1]
            )
        )
        self.assertEqual("inspect_afr_state", calls[1][1][3]["op"])
        self.assertEqual("inspect_afr_state", calls[3][1][3]["op"])
        self.assertEqual("link_xattrop_gfid", calls[5][1][0]["op"])
        self.assertEqual("link_file_gfid_from_target", calls[5][1][1]["op"])
        self.assertEqual("file-metadata-split-brain", record_baseline.call_args.args[2]["kind"])
        self.assertEqual(expected_index_root, record_baseline.call_args.args[2]["index_root"])
        self.assertTrue(record_baseline.call_args.args[2]["file_gfid_path"].endswith(canonical_gfid))
        self.assertEqual(canonical_gfid, record_baseline.call_args.args[2]["gfid_uuid"])
        self.assertEqual(2, write_state.call_count)
        self.assertEqual("heal info before\n" + canary_file._canary_temp_mount_root("gtest") + "/repair-canary-speed/alpha/payload.txt", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("split-brain before", write_state.call_args.args[2]["heal_info_split_brain_before"])
        self.assertEqual("heal info after" + chr(10) + canary_file._canary_temp_mount_root("gtest") + "/repair-canary-speed/alpha/payload.txt", write_state.call_args.args[2]["heal_info_after"])
        self.assertEqual("split-brain after", write_state.call_args.args[2]["heal_info_split_brain_after"])
        self.assertTrue(write_state.call_args.args[2]["heal_info_contains_mount_file_before"])
        self.assertTrue(write_state.call_args.args[2]["heal_info_contains_mount_file_after"])
        self.assertFalse(write_state.call_args.args[2]["heal_info_split_brain_contains_mount_file_before"])
        self.assertFalse(write_state.call_args.args[2]["heal_info_split_brain_contains_mount_file_after"])
        self.assertEqual("000000000000000100000000", write_state.call_args.args[2]["metadata_pending_value"])
        self.assertEqual("afr-synthesized", write_state.call_args.args[2]["proof_label"])
        self.assertEqual(
            {
                "host-a": [
                    {"name": "trusted.afr.gtest-client-2", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    {"name": "trusted.afr.gtest-client-3", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                ],
                "host-b": [
                    {"name": "trusted.afr.gtest-client-2", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    {"name": "trusted.afr.gtest-client-3", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                ],
                "host-c": [
                    {"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    {"name": "trusted.afr.gtest-client-1", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                ],
                "host-d": [
                    {"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    {"name": "trusted.afr.gtest-client-1", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                ],
            },
            write_state.call_args.args[2]["afr_pending_xattrs_by_host"],
        )
        run_heal_mock.assert_called_once_with("gtest", settle_seconds=0)

    def test_file_metadata_split_brain_canary_falls_back_to_native_heal_smoke_when_no_row_surfaces(self) -> None:
        expected_index_root = "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop"
        canonical_gfid = "11111111-2222-3333-4444-555555555555"
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, request.ops))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.glusterfs.mdata":
                    item["value_hex"] = "0x01020304"
                if op["op"] == "inspect_afr_state":
                    item["afr_xattrs"] = [
                        {"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                        {"name": "trusted.afr.gtest-client-1", "value_hex": "0x000000000000000100000000", "value_bytes": 12},
                    ]
                    item["afr_xattr_names"] = [entry["name"] for entry in item["afr_xattrs"]]
                if op["op"] == "link_xattrop_gfid":
                    item["value_uuid"] = canonical_gfid
                    item["value_hex"] = canonical_gfid.replace("-", "")
                    item["index_entry"] = f"{expected_index_root}/{canonical_gfid}"
                    item["xattrop_entry"] = item["index_entry"]
                if op["op"] == "link_file_gfid_from_target":
                    item["file_gfid_path"] = canary_file._file_gfid_link_path(request.backend_root, canonical_gfid)
                results.append(item)
            return {"host": host, "results": results}

        local_text_calls = {"count": 0}

        def fake_run_local_text(command):  # noqa: ANN001
            if command[:4] == ["sudo", "-n", "gluster", "volume"] and command[4:] == ["heal", "gtest", "info"]:
                local_text_calls["count"] += 1
                return "heal info before" if local_text_calls["count"] == 1 else ""
            if command[:4] == ["sudo", "-n", "gluster", "volume"] and command[4:] == ["heal", "gtest", "info", "split-brain"]:
                return "split-brain before" if local_text_calls["count"] == 1 else ""
            return ""

        monotonic_values = [0.0, 0.0, 31.0, 31.0, 62.0]

        def fake_monotonic():  # noqa: ANN001
            return monotonic_values.pop(0) if monotonic_values else 62.0

        with (
            patch("gluster_heal_tool.canary_file._brick_hosts_and_backend_roots", return_value=(["host-a", "host-b", "host-c", "host-d"], {"host-a": "/srv/gluster/brick-store/gtest/brick", "host-b": "/srv/gluster/brick-store/gtest/brick", "host-c": "/srv/gluster/brick-store/gtest/brick", "host-d": "/srv/gluster/brick-store/gtest/brick"})),
            patch("gluster_heal_tool.canary_file._run_local", return_value=None),
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=fake_run_local_text),
            patch("gluster_heal_tool.canary_file.time.monotonic", side_effect=fake_monotonic),
            patch("gluster_heal_tool.canary_file.time.sleep"),
            patch("gluster_heal_tool.canary_file._local_mount_dir"),
            patch("gluster_heal_tool.canary_file._local_write_file"),
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker),
            patch("gluster_heal_tool.canary_file._record_file_baseline"),
            patch("gluster_heal_tool.canary_file._write_state") as write_state,
            patch("gluster_heal_tool.canary_file.run_heal", side_effect=[RuntimeError("Self-heal-daemon is disabled. Heal will not be triggered on volume gtest"), RuntimeError("Commit failed on node-c.matt.home. Please check log file for details.")]) as run_heal_mock,
            patch("gluster_heal_tool.canary_file.set_heal_settings") as set_heal_settings_mock,
            patch("gluster_heal_tool.canary_file._print_response"),
        ):
            create_file_metadata_split_brain_canary(
                volume="gtest",
                scenario="repair-canary-no-row",
                mismatch_host="host-a",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual("native-heal-smoke", write_state.call_args.args[2]["proof_label"])
        self.assertFalse(write_state.call_args.args[2]["heal_info_contains_mount_file_after"])
        self.assertFalse(write_state.call_args.args[2]["heal_info_split_brain_contains_mount_file_after"])
        self.assertEqual("", write_state.call_args.args[2]["heal_info_before"])
        self.assertEqual("", write_state.call_args.args[2]["heal_info_split_brain_before"])
        self.assertEqual("", write_state.call_args.args[2]["heal_info_after"])
        self.assertEqual("", write_state.call_args.args[2]["heal_info_split_brain_after"])
        self.assertEqual(2, run_heal_mock.call_count)
        set_heal_settings_mock.assert_called_once_with("gtest", True)
        self.assertEqual("Commit failed on node-c.matt.home. Please check log file for details.", write_state.call_args.args[2]["heal_trigger_error"])

    def test_cleanup_canary_removes_file_metadata_split_brain_index(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="file-metadata-split-brain",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_roots={
                "host-a": "/srv/gluster/brick-store/gtest/brick",
                "host-b": "/srv/gluster/brick-store/gtest/brick",
                "host-c": "/srv/gluster/brick-store/gtest/brick",
                "host-d": "/srv/gluster/brick-store/gtest/brick",
            },
            brick_hosts=["host-a", "host-b", "host-c", "host-d"],
            ssh_user="gluster-repair",
            state={
                "kind": "file-metadata-split-brain",
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                "index_root": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop",
                "index_host": "host-a",
                "index_entry": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop/11111111-2222-3333-4444-555555555555",
                "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
                "gfid_uuid": "11111111-2222-3333-4444-555555555555",
                "gfid_hex": "11111111222233334444555555555555",
            },
        )

        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertTrue(
            any(
                step.step_type == "remove_stale_gfid"
                and step.host == "host-a"
                and step.target_path.endswith("/11111111-2222-3333-4444-555555555555")
                for step in result.steps
            )
        )
        self.assertTrue(any(step.step_type == "remove_stale_file_gfid" for step in result.steps))

    def test_cleanup_canary_removes_file_posix_metadata_split_brain_artifacts(self) -> None:
        result = _build_cleanup_result(
            volume="gtest3",
            scenario="repair-canary-posix-split",
            kind="file-posix-metadata-split-brain",
            mount_root="/gtest3",
            backend_root="/gluster/gtest3b/gtest3/brick",
            brick_roots={
                "node-a": "/gluster/gtest3b/gtest3/brick",
                "node-b": "/gluster/gtest3b/gtest3/brick",
                "node-d": "/gluster/gtestlab/gtest3/brick",
            },
            brick_hosts=["node-a", "node-b", "node-d"],
            ssh_user="gluster-repair",
            state={
                "kind": "file-posix-metadata-split-brain",
                "backend_root": "/gluster/gtest3b/gtest3/brick",
                "backend_target": "/gluster/gtest3b/gtest3/brick/repair-canary-posix-split/alpha/payload.txt",
                "index_root": "/gluster/gtest3b/gtest3/brick/.glusterfs/indices/xattrop",
                "index_host": "node-a",
                "index_entry": "/gluster/gtest3b/gtest3/brick/.glusterfs/indices/xattrop/11111111-2222-3333-4444-555555555555",
                "file_gfid_path": "/gluster/gtest3b/gtest3/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
                "gfid_uuid": "11111111-2222-3333-4444-555555555555",
            },
        )

        self.assertTrue(any(step.step_type == "remove_stale_gfid" for step in result.steps))
        self.assertTrue(any(step.step_type == "remove_stale_file_gfid" for step in result.steps))

    def test_file_metadata_canary_sets_only_the_mismatch_host_xattr(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                results.append({"op": op["op"], "ok": True, "name": op.get("name", "")})
            return {"host": host, "results": results}

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount, \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._write_state") as write_state, \
            patch("gluster_heal_tool.canary_file._print_response"):
            canary_file.create_file_metadata_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                mismatch_host="host-b",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(1, len(calls))
        host, ops = calls[0]
        self.assertEqual("host-b", host)
        self.assertEqual(1, len(ops))
        self.assertEqual("setxattr", ops[0]["op"])
        self.assertEqual("trusted.gfid", ops[0]["name"])
        self.assertEqual("/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt", ops[0]["path"])
        self.assertEqual(1, write_state.call_count)
        self.assertEqual("backend-audit", write_state.call_args.args[2]["construction_class"])
        self.assertEqual("operator-path-mechanical", write_state.call_args.args[2]["proof_label"])

    def test_file_metadata_canary_creates_mount_directory_after_mount(self) -> None:
        events: list[tuple[str, str]] = []

        def fake_mount(volume: str, mount_root: str) -> None:
            events.append(("mount", mount_root))

        def fake_mkdir(path: str) -> None:
            events.append(("mkdir", path))

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount", side_effect=fake_mount), \
            patch("gluster_heal_tool.canary_file._local_mount_dir", side_effect=fake_mkdir), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", return_value={"host": "host-b", "results": [{"op": "setxattr", "ok": True, "name": "trusted.gfid"}]}), \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file._print_response"):
            canary_file.create_file_metadata_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                mismatch_host="host-b",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(
            [
                ("mount", f"{default_temp_mount_root()}/repair-canary/gtest"),
                ("mkdir", f"{default_temp_mount_root()}/repair-canary/gtest/repair-canary-speed/alpha"),
            ],
            events,
        )

    def test_file_handle_ghost_canary_creates_mount_directory_after_mount(self) -> None:
        events: list[tuple[str, str]] = []

        def fake_mount(volume: str, mount_root: str) -> None:
            events.append(("mount", mount_root))

        def fake_mkdir(path: str) -> None:
            events.append(("mkdir", path))

        def fake_run_local(command: list[str]) -> None:
            if command[:4] == ["sudo", "-n", "gluster", "volume"]:
                return
            if command[:3] == ["sudo", "-n", "stat"]:
                return
            raise AssertionError(f"unexpected local command: {command}")

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "touch_index_from_target":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                results.append(item)
            return {"host": host, "results": results}

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount", side_effect=fake_mount), \
            patch("gluster_heal_tool.canary_file._local_mount_dir", side_effect=fake_mkdir), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch("gluster_heal_tool.canary_file._run_local", side_effect=fake_run_local), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file.time.sleep"), \
            patch("gluster_heal_tool.canary_file._print_response"):
            canary_file.create_file_handle_ghost_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                ghost_host="host-b",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(
            [
                ("mount", f"{default_temp_mount_root()}/repair-canary/gtest"),
                ("mkdir", f"{default_temp_mount_root()}/repair-canary/gtest/repair-canary-speed/alpha"),
            ],
            events,
        )

    def test_symlink_missing_stale_canary_uses_orphaned_symlink_shape(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.gfid":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                results.append(item)
            return {"host": host, "results": results}

        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["host-a", "host-b", "host-c", "host-d"],
                {
                    "host-a": "/gluster/homea/gtest/brick",
                    "host-b": "/srv/gluster/brick-store/gtest/brick",
                    "host-c": "/gluster/homec/gtest/brick",
                    "host-d": "/gluster/homed/gtest/brick",
                },
            ),
        ), \
            patch("gluster_heal_tool.canary_file.canary_api._brick_roles_by_host", return_value={"host-a": "data", "host-b": "data", "host-c": "data", "host-d": "data"}), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount, \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_symlink"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._trigger_canary_heal") as trigger_heal, \
            patch("gluster_heal_tool.canary_file._write_state") as write_state, \
            patch("gluster_heal_tool.canary_file._print_response"), \
            patch("gluster_heal_tool.canary_file.set_heal_settings") as set_heal_settings:
            create_symlink_missing_stale_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
                trigger_heal=False,
            )

        set_heal_settings.assert_called_once_with("gtest", False)
        trigger_heal.assert_not_called()
        self.assertTrue(write_state.call_args.args[2]["leave_heal_pending"])
        self.assertEqual(["host-a", "host-c", "host-d"], [host for host, _ops in calls])
        self.assertEqual("getxattr", calls[0][1][0]["op"])
        self.assertEqual("trusted.gfid", calls[0][1][0]["name"])
        self.assertEqual("rm", calls[1][1][0]["op"])
        self.assertEqual("/gluster/homec/gtest/brick/repair-canary-speed/alpha/payload.txt", calls[1][1][0]["path"])
        self.assertEqual("rm", calls[2][1][0]["op"])
        self.assertEqual("/gluster/homed/gtest/brick/repair-canary-speed/alpha/payload.txt", calls[2][1][0]["path"])
        state = write_state.call_args.args[2]
        self.assertEqual("/gluster/homed/gtest/brick/repair-canary-speed/alpha/payload.txt", state["backend_targets_by_host"]["host-d"])
        self.assertEqual("/gluster/homed/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a", state["file_gfid_paths_by_host"]["host-d"])

    def test_symlink_brick_outage_canary_uses_host_ops_and_records_observations(self) -> None:
        calls: list[tuple[str, list[str]]] = []
        heal_texts: list[str] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            results = []
            for op in request.ops:
                self.assertEqual("inspect_path", op["op"])
                results.append(
                    {
                        "op": op["op"],
                        "ok": True,
                        "trusted_gfid": "6a36631d-94c2-46b4-b8e4-66646840389a",
                        "readlink": "/var/tmp/gluster-repair-symlink-target/repair-canary-symlink-outage/alpha/payload.txt",
                        "kind": "symlink",
                    }
                )
            calls.append((host, [op["op"] for op in request.ops]))
            return {"host": host, "results": results}

        def fake_run_local_text(command):  # noqa: ANN001
            heal_texts.append(" ".join(command))
            if command[-1] == "statistics":
                return "Type of crawl: FULL\nEnding time of crawl"
            if command[-1] == "info":
                return "Brick node-a:/srv/gluster/brick-store/gtest/brick\nNumber of entries: 0"
            return ""

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c", "host-d"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file._run_local_text", side_effect=fake_run_local_text), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._run_remote_host_ops"), \
            patch("gluster_heal_tool.canary_file._brick_status_is_online", side_effect=[False, True]), \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_symlink"), \
            patch("gluster_heal_tool.canary_file._capture_mount_symlink_snapshot", return_value={"path": "/gtest/repair-canary-symlink-outage/alpha/payload.txt", "ok": True, "kind": "symlink", "readlink": "/var/tmp/gluster-repair-symlink-target/repair-canary-symlink-outage/alpha/payload.txt"}), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file._print_response"):
            create_symlink_brick_outage_canary(
                volume="gtest",
                scenario="repair-canary-symlink-outage",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(4, len(calls))
        self.assertTrue(all(call[1] == ["inspect_path"] for call in calls))
        self.assertTrue(any("statistics" in call for call in heal_texts))

    def test_wait_for_full_heal_crawl_waits_until_the_current_full_crawl_finishes(self) -> None:
        outputs = [
            "Type of crawl: FULL\nCrawl is in progress\nNo. of entries healed: 0",
            "Type of crawl: FULL\nEnding time of crawl: Thu May 14 09:07:56 2026\nNo. of entries healed: 1",
        ]
        monotonic_values = [0.0, 1.0, 2.0]

        def fake_run_local_text(command):  # noqa: ANN001
            self.assertEqual(["sudo", "-n", "gluster", "volume", "heal", "gtest", "statistics"], command)
            return outputs.pop(0)

        with patch("gluster_heal_tool.canary_file._run_local_text", side_effect=fake_run_local_text), \
            patch("gluster_heal_tool.canary_file.time.monotonic", side_effect=monotonic_values), \
            patch("gluster_heal_tool.canary_file.time.sleep") as sleep_mock:
            result = canary_file._wait_for_full_heal_crawl("gtest")

        self.assertEqual("Type of crawl: FULL\nEnding time of crawl: Thu May 14 09:07:56 2026\nNo. of entries healed: 1", result)
        self.assertEqual([], outputs)
        self.assertEqual(1, sleep_mock.call_count)

    def test_directory_metadata_canary_auto_selects_volume_hosts(self) -> None:
        calls: list[str] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append(host)
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.gfid":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                if op["op"] == "getxattr" and op["name"] == "trusted.glusterfs.mdata":
                    item["value_hex"] = "0x01020304"
                results.append(item)
            return {"host": host, "results": results}

        with patch("gluster_heal_tool.canary_shared._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_shared._run_local"), \
            patch("gluster_heal_tool.canary_shared._local_mount_dir"), \
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_shared._write_state"), \
            patch("gluster_heal_tool.canary_shared._print_response"):
            create_directory_metadata_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
            )

        self.assertEqual(["host-a", "host-a", "host-c", "host-b"], calls)

    def test_file_ctime_metadata_canary_uses_file_mdata_shape(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True, "name": op.get("name", "")}
                if op["op"] == "getxattr" and op["name"] == "trusted.glusterfs.mdata":
                    item["value_hex"] = "0x01020304"
                results.append(item)
            return {"host": host, "results": results}

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch("gluster_heal_tool.canary_file._local_write_file"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file._print_response"):
            create_file_ctime_metadata_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                mismatch_host="host-b",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["host-a", "host-a", "host-c", "host-b"], [host for host, _ops in calls])
        self.assertEqual("getxattr", calls[0][1][0]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[0][1][0]["name"])
        self.assertEqual("touch", calls[1][1][0]["op"])
        self.assertEqual("setxattr", calls[1][1][1]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[1][1][1]["name"])
        self.assertEqual("touch", calls[2][1][0]["op"])
        self.assertEqual("setxattr", calls[2][1][1]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[2][1][1]["name"])
        self.assertEqual("removexattr", calls[3][1][0]["op"])
        self.assertEqual("trusted.glusterfs.mdata", calls[3][1][0]["name"])

    def test_ctime_review_chain_canary_runs_directory_then_file_branches(self) -> None:
        directory_calls: list[tuple[str, str]] = []
        file_calls: list[tuple[str, str]] = []

        def fake_directory(*, volume, scenario, mismatch_host, dir_name, ssh_user, worker_path):  # noqa: ANN001
            directory_calls.append((volume, scenario))
            self.assertEqual("host-c", mismatch_host)
            self.assertEqual("alpha", dir_name)
            self.assertTrue(ssh_user)
            self.assertTrue(worker_path)

        def fake_file(*, volume, scenario, mismatch_host, dir_name, file_name, ssh_user, worker_path):  # noqa: ANN001
            file_calls.append((volume, scenario))
            self.assertEqual("host-c", mismatch_host)
            self.assertEqual("alpha", dir_name)
            self.assertEqual("payload.txt", file_name)
            self.assertTrue(ssh_user)
            self.assertTrue(worker_path)

        with patch("gluster_heal_tool.canary.create_directory_metadata_canary", side_effect=fake_directory), \
            patch("gluster_heal_tool.canary.create_file_ctime_metadata_canary", side_effect=fake_file):
            create_ctime_review_chain_canary(
                volume="gtest3",
                scenario="repair-canary-ctime-chain",
                mismatch_host="host-c",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual([("gtest3", "repair-canary-ctime-chain-directory")], directory_calls)
        self.assertEqual([("gtest3", "repair-canary-ctime-chain-file")], file_calls)

    def test_ctime_review_chain_cleanup_runs_both_suffixes(self) -> None:
        cleanup_calls: list[tuple[str, str]] = []

        def fake_cleanup(*, volume, scenario, dir_name, file_name, ssh_user, worker_path):  # noqa: ANN001
            cleanup_calls.append((volume, scenario))
            self.assertEqual("alpha", dir_name)
            self.assertEqual("payload.txt", file_name)
            self.assertTrue(ssh_user)
            self.assertTrue(worker_path)

        with patch("gluster_heal_tool.canary.cleanup_canary", side_effect=fake_cleanup):
            cleanup_ctime_review_chain_canary(
                volume="gtest3",
                scenario="repair-canary-ctime-chain",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(
            [
                ("gtest3", "repair-canary-ctime-chain-directory"),
                ("gtest3", "repair-canary-ctime-chain-file"),
            ],
            cleanup_calls,
        )

    def test_file_stale_survivor_canary_uses_below_quorum_shape(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True}
                if op["op"] == "link_file_gfid_from_target":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                    item["file_gfid_path"] = "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a"
                if op["op"] == "touch_index_from_target":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                results.append(item)
            return {"host": host, "results": results}

        with patch("gluster_heal_tool.canary_file._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c", "host-d"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_file.canary_api._brick_roles_by_host", return_value={"host-a": "data", "host-b": "data", "host-c": "data", "host-d": "data"}), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount") as ensure_mount, \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._record_file_baseline"), \
            patch("gluster_heal_tool.canary_file.run_heal"), \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file._print_response"), \
            patch("builtins.print"):
            create_file_stale_survivor_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(["host-a", "host-b", "host-c", "host-d"], [host for host, _ops in calls])
        self.assertEqual("touch", calls[0][1][0]["op"])
        self.assertEqual("setxattr", calls[0][1][1]["op"])
        self.assertEqual("link_file_gfid_from_target", calls[0][1][2]["op"])
        self.assertTrue(any(op["op"] == "touch_index_from_target" for op in calls[0][1]))
        for _host, ops in calls[1:]:
            self.assertEqual("rm", ops[0]["op"])
            self.assertEqual("rm", ops[1]["op"])

    def test_file_handle_ghost_canary_uses_repair_executor_shape(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True}
                if op["op"] == "touch_index_from_target":
                    item["value_uuid"] = "6a36631d-94c2-46b4-b8e4-66646840389a"
                results.append(item)
            return {"host": host, "results": results}

        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["host-a", "host-b", "host-c"],
                {
                    "host-a": "/gluster/homea/gtest/brick",
                    "host-b": "/srv/gluster/brick-store/gtest/brick",
                    "host-c": "/gluster/homec/gtest/brick",
                },
            ),
        ), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount"), \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._record_file_baseline"), \
            patch("gluster_heal_tool.canary_file.set_heal_settings") as set_heal_settings, \
            patch("gluster_heal_tool.canary_file._write_state"), \
            patch("gluster_heal_tool.canary_file.time.sleep"), \
            patch("gluster_heal_tool.canary_file._print_response"):
            create_file_handle_ghost_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
                trigger_heal=False,
            )

        set_heal_settings.assert_called_once_with("gtest", False)

        self.assertEqual(["host-a", "host-b"], [host for host, _ops in calls])
        self.assertEqual("mkdir", calls[0][1][0]["op"])
        self.assertEqual("touch", calls[0][1][1]["op"])
        self.assertEqual("setxattr", calls[0][1][2]["op"])
        self.assertEqual("trusted.gfid", calls[0][1][2]["name"])
        self.assertEqual("touch_index_from_target", calls[0][1][4]["op"])
        self.assertEqual("write_text", calls[1][1][0]["op"])
        self.assertEqual(
            "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            calls[1][1][0]["path"],
        )
        self.assertEqual("setxattr", calls[1][1][1]["op"])
        self.assertEqual("trusted.gfid2path.canary", calls[1][1][2]["name"])

    def test_orphaned_gfid_hardlink_canary_uses_orphan_backend_root(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []
        canonical_gfid = "6a36631d-94c2-46b4-b8e4-66646840389a"
        orphan_root = "/gluster/homec/gtest/brick"

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual("gluster-repair", ssh_user)
            self.assertTrue(worker_path)
            self.assertEqual(10, connect_timeout)
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                item = {"op": op["op"], "ok": True}
                if op["op"] == "link_file_gfid_from_target":
                    item["value_uuid"] = canonical_gfid
                    item["file_gfid_path"] = f"{orphan_root}/.glusterfs/6a/36/{canonical_gfid}"
                results.append(item)
            return {"host": host, "results": results}

        with patch(
            "gluster_heal_tool.canary_file._brick_hosts_and_backend_roots",
            return_value=(
                ["host-a", "host-b", "host-c"],
                {
                    "host-a": "/gluster/homea/gtest/brick",
                    "host-b": "/srv/gluster/brick-store/gtest/brick",
                    "host-c": orphan_root,
                },
            ),
        ), \
            patch("gluster_heal_tool.canary_file._run_local"), \
            patch("gluster_heal_tool.canary_file._ensure_canary_mount"), \
            patch("gluster_heal_tool.canary_file._local_mount_dir"), \
            patch("gluster_heal_tool.canary_file._local_mount_file"), \
            patch("gluster_heal_tool.canary_file._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_file._record_file_baseline") as record_baseline, \
            patch("gluster_heal_tool.canary_file.set_heal_settings") as set_heal_settings, \
            patch("gluster_heal_tool.canary_file._print_response"), \
            patch("builtins.print"):
            canary_file.create_orphaned_gfid_hardlink_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                orphan_host="host-c",
                dir_name="alpha",
                file_name="payload.txt",
                trigger_heal=False,
            )

        set_heal_settings.assert_called_once_with("gtest", False)
        self.assertEqual(["host-c"], [host for host, _ops in calls])
        self.assertEqual(f"{orphan_root}/repair-canary-speed/alpha", calls[0][1][0]["path"])
        self.assertEqual(f"{orphan_root}/repair-canary-speed/alpha/payload.txt", calls[0][1][1]["path"])
        self.assertEqual(f"{orphan_root}/.glusterfs/6a/36/{canonical_gfid}", record_baseline.call_args.args[2]["file_gfid_path"])
        self.assertTrue(record_baseline.call_args.args[2]["leave_heal_pending"])

    def test_directory_child_reference_canary_uses_dedicated_kind(self) -> None:
        calls: list[str] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append(host)
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with patch("gluster_heal_tool.canary_shared._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_shared._local_mount_dir"), \
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_shared._write_state") as write_state, \
            patch("gluster_heal_tool.canary_shared._print_response"):
            create_directory_child_reference_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                parent_name="alpha",
                present_child="beta",
                missing_child="payload.txt",
            )

        self.assertEqual(["host-b"], calls)
        self.assertEqual("directory-child-reference", write_state.call_args.args[2]["kind"])
        self.assertEqual("host-b", write_state.call_args.args[2]["missing_host"])

    def test_directory_backend_child_gap_canary_builds_explicit_plan(self) -> None:
        state = {
            "kind": "directory-backend-child-gap",
            "volume": "gtest",
            "scenario": "repair-canary-dir-backend-child-gap",
            "missing_host": "node-b",
            "parent_name": "alpha",
            "present_child": "beta",
            "missing_child": "gamma",
            "mount_root": "/gtest",
            "mount_parent": "/gtest/repair-canary-dir-backend-child-gap/alpha/beta",
            "mount_path": "/gtest/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_scenario_root": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap",
            "missing_backend": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap/alpha/beta/gamma",
            "backend_observations": [
                {
                    "host": "node-a",
                    "results": [
                        {
                            "label": "backend_missing_child",
                            "ok": True,
                            "trusted_gfid": "11111111-1111-1111-1111-111111111111",
                        }
                    ],
                },
                {
                    "host": "node-b",
                    "results": [
                        {"label": "backend_missing_child", "ok": False},
                    ],
                },
            ],
        }

        with patch("gluster_heal_tool.canary_directory.canary_api._read_state", return_value=state):
            plan = build_directory_backend_child_gap_plan_from_canary_state(
                volume="gtest",
                scenario="repair-canary-dir-backend-child-gap",
            )

        self.assertEqual(1, plan["schema_version"])
        action = plan["actions"][0]
        self.assertEqual("repair_directory_metadata", action["action_type"])
        self.assertEqual("recreate_missing_directory_backend_child_gap", action["repair_strategy"])
        self.assertEqual("/gtest/repair-canary-dir-backend-child-gap/alpha/beta/gamma", action["logical_path"])
        self.assertEqual(["node-b"], action["missing_hosts"])
        self.assertEqual(
            "11111111-1111-1111-1111-111111111111",
            action["directory_canonical_gfid"],
        )
        self.assertIn("directory-backend-child-gap marker", action["notes"])

    def test_directory_backend_child_gap_bounded_canary_builds_bounded_plan(self) -> None:
        state = {
            "kind": "directory-backend-child-gap-bounded",
            "volume": "gtest",
            "scenario": "repair-canary-dir-backend-child-gap-bounded",
            "missing_host": "node-b",
            "parent_name": "alpha",
            "present_child": "beta",
            "missing_child": "gamma",
            "mount_root": "/gtest",
            "mount_parent": "/gtest/repair-canary-dir-backend-child-gap-bounded/alpha/beta",
            "mount_path": "/gtest/repair-canary-dir-backend-child-gap-bounded/alpha/beta/gamma",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_scenario_root": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap-bounded",
            "missing_backend": "/srv/gluster/brick-store/gtest/brick/repair-canary-dir-backend-child-gap-bounded/alpha/beta/gamma",
            "backend_observations": [
                {
                    "host": "node-a",
                    "results": [
                        {
                            "label": "backend_missing_child",
                            "ok": True,
                            "trusted_gfid": "11111111-1111-1111-1111-111111111111",
                        }
                    ],
                },
                {
                    "host": "node-b",
                    "results": [
                        {"label": "backend_missing_child", "ok": False},
                    ],
                },
            ],
        }

        with patch("gluster_heal_tool.canary_directory.canary_api._read_state", return_value=state):
            plan = build_directory_backend_child_gap_plan_from_canary_state(
                volume="gtest",
                scenario="repair-canary-dir-backend-child-gap-bounded",
            )

        self.assertEqual(1, plan["schema_version"])
        action = plan["actions"][0]
        self.assertEqual("repair_directory_metadata", action["action_type"])
        self.assertEqual("restore_directory_children_bounded", action["repair_strategy"])
        self.assertIn("bounded subtree canary", action["notes"])

    def test_child_gap_canary_records_child_snapshot_log(self) -> None:
        calls: list[str] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append(host)
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with patch("gluster_heal_tool.canary_shared._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_shared._local_mount_dir"), \
            patch("gluster_heal_tool.canary_directory._capture_mount_stat_snapshot", side_effect=[
                {"path": "/gtest/repair-canary-speed/alpha/beta", "ok": True, "inode": "1"},
                {"path": "/gtest/repair-canary-speed/alpha/beta/gamma", "ok": False, "error": "Input/output error"},
            ]), \
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_shared._write_state") as write_state, \
            patch("gluster_heal_tool.canary_directory._write_observation_log") as write_log, \
            patch("gluster_heal_tool.canary_shared._print_response"):
            create_child_gap_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                missing_host="host-b",
                parent_name="alpha",
                present_child="beta",
                missing_child="gamma",
            )

        self.assertEqual(["host-b"], calls)
        self.assertTrue(write_state.called)
        self.assertTrue(write_log.called)
        payload = write_state.call_args.args[2]
        self.assertEqual("child-gap", payload["kind"])
        self.assertEqual(
            [
                "/repair-canary-speed/alpha/beta",
                "/repair-canary-speed/alpha/beta/gamma",
            ],
            [path.removeprefix(payload["mount_root"]) for path in payload["snapshot_paths"]],
        )
        self.assertIn("mount_snapshot", payload)
        self.assertEqual(2, len(payload["mount_snapshot"]["paths"]))

    def test_directory_gfid_merge_canary_uses_merge_kind_and_directory_links(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, list(request.ops)))
            return {
                "host": host,
                "results": [
                    {
                        "op": op["op"],
                        "ok": True,
                        "directory_gfid_path": f"/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/{host}",
                    }
                    if op["op"] == "link_directory_gfid_from_target"
                    else {"op": op["op"], "ok": True}
                    for op in request.ops
                ],
            }

        with patch("gluster_heal_tool.canary_shared._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c", "host-d"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_shared._run_local"), \
            patch("gluster_heal_tool.canary_shared._local_mount_dir"), \
            patch("gluster_heal_tool.canary_shared._local_mount_file"), \
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_shared._write_state") as write_state, \
            patch("gluster_heal_tool.canary_shared._print_response"):
            create_directory_gfid_merge_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(4, len(calls))
        self.assertEqual("mkdir", calls[0][1][0]["op"])
        self.assertEqual("setxattr", calls[0][1][1]["op"])
        self.assertEqual("link_directory_gfid_from_target", calls[0][1][2]["op"])
        self.assertEqual("setxattr", calls[0][1][3]["op"])
        self.assertTrue(calls[0][1][3]["name"].startswith("trusted.afr."))
        self.assertEqual("setxattr", calls[0][1][4]["op"])
        self.assertTrue(calls[0][1][4]["name"].startswith("trusted.afr."))
        self.assertEqual("touch_index_from_target", calls[0][1][5]["op"])
        self.assertEqual("directory-gfid-merge", write_state.call_args.args[2]["kind"])
        self.assertTrue(write_state.call_args.args[2]["leave_heal_pending"])
        self.assertEqual("payload.txt", write_state.call_args.args[2]["child_name"])
        self.assertIn("host-a", write_state.call_args.args[2]["directory_gfid_paths_by_host"])
        self.assertEqual("/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop", write_state.call_args.args[2]["index_root"])

    def test_directory_gfid_tie_canary_uses_per_host_brick_roots(self) -> None:
        calls: list[tuple[str, str, list[dict[str, object]]]] = []
        roots = {
            "host-a": "/gluster/gtest4/brick",
            "host-b": "/gluster/gtest4/brick",
            "host-c": "/gluster/gtest4/brick",
            "host-d": "/gluster/gtest4-alias/brick",
        }

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, request.backend_root, list(request.ops)))
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with (
            patch(
                "gluster_heal_tool.canary_directory._brick_hosts_and_backend_roots",
                return_value=(list(roots), roots),
            ),
            patch("gluster_heal_tool.canary_shared._run_local"),
            patch("gluster_heal_tool.canary_shared._local_mount_dir"),
            patch("gluster_heal_tool.canary_shared._local_mount_file"),
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker),
            patch("gluster_heal_tool.canary_shared._write_state") as write_state,
            patch("gluster_heal_tool.canary_shared._print_response"),
        ):
            create_directory_gfid_tie_canary(
                volume="gtest4",
                scenario="stage6-directory-tie",
                dir_name="alpha",
            )

        by_host = {host: (root, ops) for host, root, ops in calls}
        self.assertEqual("/gluster/gtest4-alias/brick", by_host["host-d"][0])
        self.assertEqual(
            "/gluster/gtest4-alias/brick/stage6-directory-tie/alpha",
            by_host["host-d"][1][0]["path"],
        )
        index_op = next(op for op in by_host["host-d"][1] if op["op"] == "touch_index_from_target")
        self.assertEqual(
            "/gluster/gtest4-alias/brick/.glusterfs/indices/xattrop",
            index_op["index_root"],
        )
        payload = write_state.call_args.args[2]
        self.assertEqual(roots, payload["backend_roots"])
        self.assertEqual(
            "/gluster/gtest4-alias/brick/stage6-directory-tie/alpha",
            payload["backend_target_by_host"]["host-d"],
        )

    def test_directory_child_observation_records_baseline_and_change(self) -> None:
        with patch(
            "gluster_heal_tool.canary_shared._read_state",
            return_value={
                "kind": "child-gap",
                "mount_parent": "/gtest/repair-canary-speed/alpha/beta",
                "mount_path": "/gtest/repair-canary-speed/alpha/beta/gamma",
                "baseline_parent_lookup": {
                    "path": "/gtest/repair-canary-speed/alpha/beta",
                    "ok": True,
                    "stdout": "baseline parent lookup",
                },
                "snapshot_paths": [
                    "/gtest/repair-canary-speed/alpha/beta",
                    "/gtest/repair-canary-speed/alpha/beta/gamma",
                ],
                "mount_snapshot": {
                    "paths": [
                        {"path": "/gtest/repair-canary-speed/alpha/beta", "inode": "1", "ok": True},
                        {"path": "/gtest/repair-canary-speed/alpha/beta/gamma", "error": "Input/output error", "ok": False},
                    ]
                },
            },
        ), patch(
            "gluster_heal_tool.canary_directory._capture_mount_stat_snapshot",
            side_effect=[
                {"path": "/gtest/repair-canary-speed/alpha/beta", "inode": "1", "ok": True},
                {"path": "/gtest/repair-canary-speed/alpha/beta/gamma", "error": "Input/output error", "ok": False},
            ],
        ) as capture, patch(
            "gluster_heal_tool.canary_directory._capture_parent_lookup_snapshot",
            return_value={"path": "/gtest/repair-canary-speed/alpha/beta", "ok": True, "stdout": "parent lookup"},
        ) as parent_lookup, patch("gluster_heal_tool.canary_shared._write_state") as write_state, patch(
            "gluster_heal_tool.canary_directory._write_observation_log"
        ) as write_log, patch("builtins.print") as mocked_print:
            observe_directory_child_state_canary(volume="gtest", scenario="repair-canary-speed")

        self.assertEqual(2, capture.call_count)
        self.assertEqual(1, parent_lookup.call_count)
        self.assertTrue(write_state.called)
        self.assertTrue(write_log.called)
        payload = write_state.call_args.args[2]
        self.assertIn("baseline_parent_lookup", payload)
        self.assertIn("last_observation", payload)
        self.assertFalse(payload["last_observation"]["changed"])
        self.assertIn("parent_lookup", payload["last_observation"])
        self.assertEqual(1, len(payload["observations"]))
        printed = "\n".join(str(call.args[0]) for call in mocked_print.call_args_list if call.args)
        self.assertIn("baseline parent lookup", printed)
        self.assertIn("current parent lookup", printed)
        mocked_print.assert_called()

    def test_directory_gfid_mask_seeds_distinct_backend_child_sets(self) -> None:
        calls: dict[str, list[dict[str, object]]] = {}

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls[host] = list(request.ops)
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with (
            patch(
                "gluster_heal_tool.canary_directory._brick_hosts_and_backend_roots",
                return_value=(
                    ["host-a", "host-b", "host-c", "host-d"],
                    {
                        "host-a": "/gluster/gtest4/brick",
                        "host-b": "/gluster/gtest4/brick",
                        "host-c": "/gluster/gtest4/brick",
                        "host-d": "/gluster/gtest4-alias/brick",
                    },
                ),
            ),
            patch("gluster_heal_tool.canary_shared._run_local"),
            patch("gluster_heal_tool.canary_shared._local_mount_dir"),
            patch("gluster_heal_tool.canary_shared._local_mount_file") as local_mount_file,
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker),
            patch("gluster_heal_tool.canary_shared._write_state"),
            patch("gluster_heal_tool.canary_shared._print_response"),
        ):
            create_directory_gfid_mask_canary(
                volume="gtest4",
                scenario="stage6-directory-mask",
                dir_name="alpha",
                file_name="payload.txt",
                shadow_file_name="shadow.txt",
            )

        local_mount_file.assert_not_called()
        for host in ("host-a", "host-b"):
            touch = next(op for op in calls[host] if op["op"] == "touch")
            self.assertTrue(str(touch["path"]).endswith("/alpha/payload.txt"))
        for host in ("host-c", "host-d"):
            touch = next(op for op in calls[host] if op["op"] == "touch")
            self.assertTrue(str(touch["path"]).endswith("/alpha/shadow.txt"))

    def test_directory_gfid_tie_canary_uses_tie_kind_without_child_file(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, list(request.ops)))
            return {"host": host, "results": [{"op": op["op"], "ok": True} for op in request.ops]}

        with patch("gluster_heal_tool.canary_shared._brick_hosts_and_root", return_value=(["host-a", "host-b", "host-c", "host-d"], "/srv/gluster/brick-store/gtest/brick")), \
            patch("gluster_heal_tool.canary_shared._run_local"), \
            patch("gluster_heal_tool.canary_shared._local_mount_dir"), \
            patch("gluster_heal_tool.canary_shared._local_mount_file") as local_mount_file, \
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker), \
            patch("gluster_heal_tool.canary_shared._write_state") as write_state, \
            patch("gluster_heal_tool.canary_shared._print_response"):
            create_directory_gfid_tie_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
            )

        self.assertEqual(4, len(calls))
        self.assertEqual("mkdir", calls[0][1][0]["op"])
        self.assertEqual("setxattr", calls[0][1][1]["op"])
        self.assertEqual("link_directory_gfid_from_target", calls[0][1][2]["op"])
        self.assertEqual("setxattr", calls[0][1][3]["op"])
        self.assertTrue(calls[0][1][3]["name"].startswith("trusted.afr."))
        self.assertEqual("setxattr", calls[0][1][4]["op"])
        self.assertTrue(calls[0][1][4]["name"].startswith("trusted.afr."))
        self.assertEqual("touch_index_from_target", calls[0][1][5]["op"])
        local_mount_file.assert_not_called()
        self.assertEqual("directory-gfid-tie", write_state.call_args.args[2]["kind"])
        self.assertTrue(write_state.call_args.args[2]["leave_heal_pending"])
        self.assertEqual("", write_state.call_args.args[2]["child_name"])
        self.assertEqual("/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop", write_state.call_args.args[2]["index_root"])

    def test_type_mismatch_canary_records_heal_snapshots_and_state(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                result = {"op": op["op"], "ok": True}
                if op["op"] == "link_directory_gfid_from_target":
                    result["directory_gfid_path"] = f"/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/{host}"
                if op["op"] == "link_file_gfid_from_target":
                    result["file_gfid_path"] = f"/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/{host}"
                results.append(result)
            return {"host": host, "results": results}

        with (
            patch(
                "gluster_heal_tool.canary_shared._brick_hosts_and_root",
                return_value=(["host-a", "host-b", "host-c", "host-d"], "/srv/gluster/brick-store/gtest/brick"),
            ),
            patch("gluster_heal_tool.canary_shared._run_local"),
            patch(
                "gluster_heal_tool.canary_directory._run_local_text",
                side_effect=["heal info before", "split-brain before", "heal info after", "split-brain after"],
            ),
            patch("gluster_heal_tool.canary_directory._trigger_canary_heal") as trigger_heal,
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker),
            patch("gluster_heal_tool.canary_shared._write_state") as write_state,
            patch("gluster_heal_tool.canary_directory._write_observation_log"),
            patch("gluster_heal_tool.canary_shared._print_response"),
        ):
            create_type_mismatch_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
                shadow_name="shadow.txt",
            )

        self.assertEqual(4, len(calls))
        self.assertEqual("write_text", calls[0][1][0]["op"])
        self.assertEqual("setxattr", calls[0][1][1]["op"])
        self.assertEqual("link_file_gfid_from_target", calls[0][1][2]["op"])
        self.assertEqual("touch_index_from_target", calls[0][1][3]["op"])
        self.assertEqual("setxattr", calls[0][1][4]["op"])
        self.assertEqual("setxattr", calls[0][1][5]["op"])
        self.assertEqual(2, write_state.call_count)
        payload = write_state.call_args.args[2]
        self.assertEqual("type-mismatch", payload["kind"])
        self.assertEqual("shadow.txt", payload["shadow_name"])
        self.assertEqual("/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop", payload["index_root"])
        self.assertEqual("heal info before", payload["heal_info_before"])
        self.assertEqual("split-brain before", payload["heal_info_split_brain_before"])
        self.assertEqual("heal info after", payload["heal_info_after"])
        self.assertEqual("split-brain after", payload["heal_info_split_brain_after"])
        trigger_heal.assert_called_once_with("gtest", "repair-canary-speed")

    def test_type_mismatch_canary_records_heal_trigger_error_and_continues(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, list(request.ops)))
            results = []
            for op in request.ops:
                result = {"op": op["op"], "ok": True}
                if op["op"] == "link_directory_gfid_from_target":
                    result["directory_gfid_path"] = f"/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/{host}"
                if op["op"] == "link_file_gfid_from_target":
                    result["file_gfid_path"] = f"/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/{host}"
                results.append(result)
            return {"host": host, "results": results}

        with (
            patch(
                "gluster_heal_tool.canary_shared._brick_hosts_and_root",
                return_value=(["host-a", "host-b", "host-c", "host-d"], "/srv/gluster/brick-store/gtest/brick"),
            ),
            patch("gluster_heal_tool.canary_shared._run_local"),
            patch(
                "gluster_heal_tool.canary_directory._run_local_text",
                side_effect=["heal info before", "split-brain before", "heal info after", "split-brain after"],
            ),
            patch(
                "gluster_heal_tool.canary_directory._trigger_canary_heal",
                side_effect=RuntimeError("Glusterd Syncop Mgmt brick op 'Heal' failed"),
            ) as trigger_heal,
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker),
            patch("gluster_heal_tool.canary_shared._write_state") as write_state,
            patch("gluster_heal_tool.canary_directory._write_observation_log"),
            patch("gluster_heal_tool.canary_shared._print_response"),
        ):
            create_type_mismatch_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
                shadow_name="shadow.txt",
            )

        self.assertEqual(4, len(calls))
        self.assertEqual("write_text", calls[0][1][0]["op"])
        self.assertEqual("setxattr", calls[0][1][1]["op"])
        self.assertEqual("link_file_gfid_from_target", calls[0][1][2]["op"])
        self.assertEqual("touch_index_from_target", calls[0][1][3]["op"])
        self.assertEqual("setxattr", calls[0][1][4]["op"])
        self.assertEqual("setxattr", calls[0][1][5]["op"])
        self.assertEqual(2, write_state.call_count)
        payload = write_state.call_args.args[2]
        self.assertEqual("type-mismatch", payload["kind"])
        self.assertEqual("Glusterd Syncop Mgmt brick op 'Heal' failed", payload["heal_trigger_error"])
        self.assertEqual("heal info after", payload["heal_info_after"])
        self.assertEqual("split-brain after", payload["heal_info_split_brain_after"])
        trigger_heal.assert_called_once_with("gtest", "repair-canary-speed")

    def test_type_mismatch_plan_bridge_uses_canary_heal_snapshots(self) -> None:
        state = {
            "kind": "type-mismatch",
            "volume": "gtest",
            "scenario": "repair-canary-type-mismatch-bridge",
            "mount_root": "/gtest",
            "mount_target": "/gtest/repair-canary-type-mismatch-bridge/alpha",
            "mount_shadow": "/gtest/repair-canary-type-mismatch-bridge/alpha/shadow.txt",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-type-mismatch-bridge/alpha",
            "left_hosts": ["host-a", "host-b"],
            "right_hosts": ["host-c", "host-d"],
            "left_gfid_uuid": "11111111-1111-4111-8111-111111111111",
            "right_gfid_uuid": "22222222-2222-4222-8222-222222222222",
            "directory_gfid_paths_by_host": {"host-a": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/host-a"]},
            "afr_pending_xattrs_by_host": {"host-a": [{"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12}], "host-c": []},
            "brick_roles_by_host": {"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"},
            "heal_info_before": "heal info before",
            "heal_info_split_brain_before": "split-brain before",
            "heal_info_after": "heal info after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_split_brain_after": "split-brain after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
        }

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual(1, len(request.ops))
            op = request.ops[0]
            self.assertEqual("inspect_path", op["op"])
            mtime_by_host = {
                "host-a": 1780313288,
                "host-b": 1780313289,
                "host-c": 1780313290,
                "host-d": 1780313290,
            }
            kind = "file" if host in {"host-a", "host-b"} else "dir"
            return {
                "host": host,
                "results": [
                    {
                        "op": "inspect_path",
                        "ok": True,
                        "path": op["path"],
                        "kind": kind,
                        "mtime": mtime_by_host[host],
                        "size": 64,
                    }
                ],
            }

        with (
            patch("gluster_heal_tool.canary_shared._read_state", return_value=state),
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker),
        ):
            plan = build_type_mismatch_plan_from_canary_state(volume="gtest", scenario="repair-canary-type-mismatch-bridge")

        _assert_canary_state_provenance(
            self,
            plan,
            kind="type-mismatch",
            volume="gtest",
            scenario="repair-canary-type-mismatch-bridge",
        )
        action = plan["actions"][0]
        self.assertEqual("review_type_mismatch", action["action_type"])
        self.assertEqual("review_type_mismatch", action["repair_strategy"])
        self.assertEqual(["host-a", "host-b"], action["file_hosts"])
        self.assertEqual(["host-c", "host-d"], action["directory_hosts"])
        self.assertEqual("heal info before", action["heal_info_before"])
        self.assertEqual("split-brain after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt", action["heal_info_split_brain_after"])
        self.assertEqual("quarantine_loser", action["recommended_choice"])
        self.assertIn("quarantine the older file branch", action["recommended_reason"])
        self.assertEqual(2, len(action["file_copies"]))
        self.assertEqual(2, len(action["directory_copies"]))
        self.assertEqual("11111111-1111-4111-8111-111111111111", action["file_copies"][0]["identity"])
        self.assertEqual("22222222-2222-4222-8222-222222222222", action["directory_copies"][0]["identity"])
        notes = "\n".join(action["notes"])
        self.assertIn("type-mismatch boundary: file-vs-directory disagreement with heal-visible row", notes)
        self.assertIn("afr pending xattrs by host:", notes)
        self.assertIn("host-a: trusted.afr.gtest-client-0", notes)
        self.assertIn("type-mismatch backend observations by host:", notes)
        self.assertIn("brick roles by host:", notes)
        self.assertIn("host-c: arbiter", notes)
        self.assertEqual({"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"}, action["brick_roles_by_host"])
        self.assertIn("host-a: kind=file, mtime=1780313288", notes)
        self.assertIn("type-mismatch mtime summary: file=1780313288-1780313289; directory=1780313290-1780313290; loser=file", notes)
        self.assertIn("type-mismatch recommended choice: quarantine_loser", notes)
        results = build_apply_results(plan, execution_mode="dry-run", backup_mode="required", batch=True)
        rendered = render_apply_run(results)
        self.assertIn("matrix suggestion: quarantine_loser", rendered)
        self.assertIn("choice: quarantine_loser, quarantine_both, keep review, or skip", rendered)

    def test_type_mismatch_plan_bridge_includes_heal_trigger_error(self) -> None:
        state = {
            "kind": "type-mismatch",
            "volume": "gtest",
            "scenario": "repair-canary-type-mismatch-bridge-error",
            "mount_root": "/gtest",
            "mount_target": "/gtest/repair-canary-type-mismatch-bridge-error/alpha",
            "mount_shadow": "/gtest/repair-canary-type-mismatch-bridge-error/alpha/shadow.txt",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-type-mismatch-bridge-error/alpha",
            "left_hosts": ["host-a", "host-b"],
            "right_hosts": ["host-c", "host-d"],
            "directory_gfid_paths_by_host": {"host-a": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/host-a"]},
            "afr_pending_xattrs_by_host": {"host-a": [{"name": "trusted.afr.gtest-client-0", "value_hex": "0x000000000000000100000000", "value_bytes": 12}], "host-c": []},
            "brick_roles_by_host": {"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"},
            "heal_info_before": "heal info before",
            "heal_info_split_brain_before": "split-brain before",
            "heal_info_after": "heal info after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_info_split_brain_after": "split-brain after\n/gtest/repair-canary-file-metadata-split-brain/alpha/payload.txt",
            "heal_trigger_error": "Glusterd Syncop Mgmt brick op 'Heal' failed",
        }

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            self.assertEqual(1, len(request.ops))
            op = request.ops[0]
            self.assertEqual("inspect_path", op["op"])
            mtime_by_host = {
                "host-a": 1780313288,
                "host-b": 1780313289,
                "host-c": 1780313290,
                "host-d": 1780313290,
            }
            kind = "file" if host in {"host-a", "host-b"} else "dir"
            return {
                "host": host,
                "results": [
                    {
                        "op": "inspect_path",
                        "ok": True,
                        "path": op["path"],
                        "kind": kind,
                        "mtime": mtime_by_host[host],
                        "size": 64,
                    }
                ],
            }

        with (
            patch("gluster_heal_tool.canary_shared._read_state", return_value=state),
            patch("gluster_heal_tool.canary_shared._canary_worker_command", side_effect=fake_worker),
        ):
            plan = build_type_mismatch_plan_from_canary_state(volume="gtest", scenario="repair-canary-type-mismatch-bridge-error")

        action = plan["actions"][0]
        self.assertEqual("Glusterd Syncop Mgmt brick op 'Heal' failed", action["heal_trigger_error"])
        self.assertEqual("quarantine_loser", action["recommended_choice"])
        notes = "\n".join(action["notes"])
        self.assertIn("heal crawl error: Glusterd Syncop Mgmt brick op 'Heal' failed", notes)

    def test_type_mismatch_plan_bridge_rejects_stale_state_without_heal_snapshots(self) -> None:
        state = {
            "kind": "type-mismatch",
            "volume": "gtest",
            "scenario": "repair-canary-type-mismatch-bridge-stale",
            "mount_root": "/gtest",
            "mount_target": "/gtest/repair-canary-type-mismatch-bridge-stale/alpha",
            "mount_shadow": "/gtest/repair-canary-type-mismatch-bridge-stale/alpha/shadow.txt",
            "left_hosts": ["host-a", "host-b"],
            "right_hosts": ["host-c", "host-d"],
            "directory_gfid_paths_by_host": {"host-a": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/host-a"]},
            "brick_roles_by_host": {"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"},
        }
        with patch("gluster_heal_tool.canary_shared._read_state", return_value=state):
            with self.assertRaises(RuntimeError) as ctx:
                build_type_mismatch_plan_from_canary_state(
                    volume="gtest", scenario="repair-canary-type-mismatch-bridge-stale"
                )

        self.assertIn("does not record heal snapshots", str(ctx.exception))

    def test_directory_gfid_observation_records_baseline_and_change(self) -> None:
        with patch(
            "gluster_heal_tool.canary_shared._read_state",
            return_value={
                "kind": "directory-gfid-merge",
                "mount_target": "/gtest/repair-canary-speed/alpha",
                "mount_child": "/gtest/repair-canary-speed/alpha/payload.txt",
                "mount_snapshot": {
                    "paths": [
                        {"path": "/gtest/repair-canary-speed/alpha", "inode": "1", "ok": True},
                        {"path": "/gtest/repair-canary-speed/alpha/payload.txt", "inode": "2", "ok": True},
                    ]
                },
            },
        ), patch(
            "gluster_heal_tool.canary_directory._capture_mount_stat_snapshot",
            side_effect=[
                {"path": "/gtest/repair-canary-speed/alpha", "inode": "1", "ok": True},
                {"path": "/gtest/repair-canary-speed/alpha/payload.txt", "inode": "3", "ok": True},
            ],
        ) as capture, patch("gluster_heal_tool.canary_shared._write_state") as write_state, patch(
            "gluster_heal_tool.canary_directory._write_observation_log"
        ) as write_log, patch("builtins.print") as mocked_print:
            observe_directory_gfid_state_canary(volume="gtest", scenario="repair-canary-speed")

        self.assertEqual(2, capture.call_count)
        self.assertTrue(write_state.called)
        self.assertTrue(write_log.called)
        payload = write_state.call_args.args[2]
        self.assertIn("last_observation", payload)
        self.assertTrue(payload["last_observation"]["changed"])
        self.assertEqual(1, len(payload["observations"]))
        mocked_print.assert_called()

    def test_file_observation_records_mount_and_brick_evidence(self) -> None:
        calls: list[tuple[str, list[dict[str, object]]]] = []

        def fake_worker(host, *, ssh_user, worker_path, request, connect_timeout=10):  # noqa: ANN001
            calls.append((host, list(request.ops)))
            return {
                "host": host,
                "results": [
                    {
                        "op": op["op"],
                        "label": op["label"],
                        "path": op["path"],
                        "ok": True,
                        "lexists": True,
                        "links": 1,
                        "gfid2path_target": "/gtest/repair-canary-speed/alpha/payload.txt",
                    }
                    for op in request.ops
                ],
            }

        with patch(
            "gluster_heal_tool.canary_file_observation._read_state",
            return_value={
                "kind": "file-handle-ghost",
                "volume": "gtest",
                "scenario": "repair-canary-speed",
                "mount_dir": "/gtest/repair-canary-speed/alpha",
                "mount_file": "/gtest/repair-canary-speed/alpha/payload.txt",
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                "ghost_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                "index_root": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop",
                "gfid_uuid": "6a36631d-94c2-46b4-b8e4-66646840389a",
                "gfid_hex": "6a36631d94c246b4b8e466646840389a",
                "mount_snapshot": {
                    "paths": [
                        {"path": "/gtest/repair-canary-speed/alpha", "inode": "1", "ok": True},
                        {"path": "/gtest/repair-canary-speed/alpha/payload.txt", "inode": "2", "ok": True},
                    ]
                },
            },
        ), patch(
            "gluster_heal_tool.canary_file_observation._brick_hosts_and_root",
            return_value=(["host-a", "host-b"], "/srv/gluster/brick-store/gtest/brick"),
        ), patch(
            "gluster_heal_tool.canary_file_observation._capture_mount_stat_snapshot",
            side_effect=[
                {"path": "/gtest/repair-canary-speed/alpha", "inode": "1", "ok": True},
                {"path": "/gtest/repair-canary-speed/alpha/payload.txt", "inode": "3", "ok": True},
            ],
        ) as capture, patch(
            "gluster_heal_tool.canary_file_observation._capture_parent_lookup_snapshot",
            return_value={"path": "/gtest/repair-canary-speed/alpha", "ok": True, "stdout": "payload.txt"},
        ), patch(
            "gluster_heal_tool.canary_file_observation._latest_heal_snapshot_path",
            return_value=str(
                default_heal_info_root() / "gtest" / "gtest-heal-info-20260509T000000Z.txt"
            ),
        ), patch(
            "gluster_heal_tool.canary_file_observation._canary_worker_command",
            side_effect=fake_worker,
        ), patch("gluster_heal_tool.canary_file_observation._write_state") as write_state, patch(
            "gluster_heal_tool.canary_file_observation._write_observation_log"
        ) as write_log, patch("builtins.print") as mocked_print:
            observe_file_state_canary(volume="gtest", scenario="repair-canary-speed")

        self.assertEqual(2, capture.call_count)
        self.assertEqual(["host-a", "host-b"], [host for host, _ops in calls])
        self.assertEqual(
            ["backend_target", "file_gfid_path", "index_entry_uuid", "index_entry_hex"],
            [str(op["label"]) for op in calls[0][1]],
        )
        self.assertTrue(write_state.called)
        self.assertTrue(write_log.called)
        payload = write_state.call_args.args[2]
        observation = payload["last_observation"]
        self.assertTrue(observation["changed"])
        self.assertEqual(2, len(observation["brick_observations"]))
        self.assertEqual(
            "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            observation["file_gfid_path"],
        )
        self.assertIn("gtest-heal-info", observation["latest_heal_snapshot"])
        mocked_print.assert_called()

    def test_build_cleanup_result_uses_executor_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="missing-file-replica",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "missing_host": "host-b",
                "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            },
        )

        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual("delete_dead_file_ref_residue", next(note for note in result.notes if note.startswith("repair strategy: ")).removeprefix("repair strategy: "))
        self.assertGreaterEqual(len(result.steps), 3)
        self.assertEqual("remove_stale_backend", result.steps[0].step_type)
        self.assertEqual("host-a", result.steps[0].host)
        self.assertEqual("ssh", result.steps[0].command_preview[0])
        self.assertIn("rm -rf", " ".join(result.steps[0].command_preview))
        self.assertEqual("remove_stale_file_gfid", result.steps[-1].step_type)
        self.assertEqual("host-b", result.steps[-1].host)

    def test_arbiter_cleanup_removes_recorded_handle(self) -> None:
        arbiter_handle = "/gluster/arbiter/gtest3a/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555"
        result = _build_cleanup_result(
            volume="gtest3a",
            scenario="arbiter-only-residue",
            kind="arbiter-only-residue",
            mount_root="/gtest3a",
            backend_root="/gluster/data-a/gtest3a/brick",
            brick_hosts=["data-a", "data-b", "arbiter"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/gluster/arbiter/gtest3a/brick",
                "arbiter_host": "arbiter",
                "arbiter_handle": arbiter_handle,
            },
        )

        handle_steps = [step for step in result.steps if step.target_path == arbiter_handle]
        self.assertEqual(1, len(handle_steps))
        self.assertEqual("remove_stale_gfid", handle_steps[0].step_type)
        self.assertEqual("arbiter", handle_steps[0].host)

    def test_build_cleanup_result_uses_host_specific_brick_roots(self) -> None:
        result = _build_cleanup_result(
            volume="gtest3a",
            scenario="repair-canary-gtest3a-arbiter-path",
            kind="missing-file-replica",
            mount_root="/gtest3a",
            backend_root="/gluster/gtest3a/gtest3a/brick",
            brick_roots={
                "node-a": "/gluster/gtest3a/gtest3a/brick",
                "node-b": "/gluster/gtest3a/gtest3a/brick",
                "node-c": "/gluster/gtest3a/arbiter/brick",
            },
            brick_hosts=["node-a", "node-b", "node-c"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/gluster/gtest3a/arbiter/brick",
                "missing_host": "node-c",
                "file_gfid_path": "/gluster/gtest3a/arbiter/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            },
        )

        backend_paths = [step.target_path for step in result.steps if step.step_type == "remove_stale_backend"]
        self.assertIn("/gluster/gtest3a/gtest3a/brick/repair-canary-gtest3a-arbiter-path", backend_paths)
        self.assertIn("/gluster/gtest3a/arbiter/brick/repair-canary-gtest3a-arbiter-path", backend_paths)
        self.assertEqual("remove_stale_file_gfid", result.steps[-1].step_type)
        self.assertEqual("node-c", result.steps[-1].host)

    def test_build_cleanup_result_uses_directory_stale_survivor_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest3a",
            scenario="repair-canary-gtest3a-dir-residue",
            kind="directory-stale-survivor",
            mount_root="/gtest3a",
            backend_root="/gluster/gtest3a/gtest3a/brick",
            brick_roots={
                "node-a": "/gluster/gtest3a/gtest3a/brick",
                "node-b": "/gluster/gtest3a/gtest3a/brick",
                "node-c": "/gluster/gtest3a/arbiter/brick",
            },
            brick_hosts=["node-a", "node-b", "node-c"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/gluster/gtest3a/arbiter/brick",
                "survivor_host": "node-c",
                "backend_target": "/gluster/gtest3a/arbiter/brick/repair-canary-gtest3a-dir-residue",
                "directory_gfid_path": "/gluster/gtest3a/arbiter/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                "brick_roles_by_host": {"node-a": "data", "node-b": "data", "node-c": "arbiter"},
            },
        )

        backend_paths = [step.target_path for step in result.steps if step.step_type == "remove_stale_backend"]
        self.assertIn("/gluster/gtest3a/arbiter/brick/repair-canary-gtest3a-dir-residue", backend_paths)
        self.assertEqual("remove_stale_dir_gfid", result.steps[-1].step_type)
        self.assertEqual("node-c", result.steps[-1].host)
        self.assertEqual({"node-a": "data", "node-b": "data", "node-c": "arbiter"}, result.brick_roles_by_host)
        rendered = render_apply_run([result])
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("node-c: arbiter", rendered)

    def test_build_cleanup_result_uses_file_stale_survivor_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="file-stale-survivor",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b", "host-c", "host-d"],
            ssh_user="gluster-repair",
            state={
                "brick_roles_by_host": {"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"},
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "survivor_host": "host-a",
                "index_hosts": ["host-a"],
                "gfid_uuid": "6a36631d-94c2-46b4-b8e4-66646840389a",
                "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                "cleanup_hints": {
                    "stale_backends_by_host": {
                        "host-a": ["/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt"],
                    },
                    "stale_file_gfid_paths_by_host": {
                        "host-a": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a"],
                    },
                    "stale_gfid_paths_by_host": {
                        "host-a": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop/6a36631d-94c2-46b4-b8e4-66646840389a"],
                    },
                },
            },
        )

        self.assertEqual({"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"}, result.brick_roles_by_host)
        rendered = render_apply_run([result])
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("host-c: arbiter", rendered)
        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        self.assertEqual("delete_dead_file_ref_residue", next(note for note in result.notes if note.startswith("repair strategy: ")).removeprefix("repair strategy: "))
        step_pairs = {(step.step_type, step.host) for step in result.steps}
        self.assertIn(("remove_stale_gfid", "host-a"), step_pairs)
        self.assertIn(("remove_stale_file_gfid", "host-a"), step_pairs)

    def test_build_cleanup_result_removes_directory_gfid_paths_for_merge_canary(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="directory-gfid-merge",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b", "host-c", "host-d"],
            ssh_user="gluster-repair",
            state={
                "index_root": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop",
                "left_hosts": ["host-a", "host-b"],
                "right_hosts": ["host-c", "host-d"],
                "left_gfid_uuid": "11111111-1111-1111-1111-111111111111",
                "right_gfid_uuid": "22222222-2222-2222-2222-222222222222",
                "directory_gfid_paths_by_host": {
                    "host-a": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/a"],
                    "host-b": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/b"],
                    "host-c": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/c"],
                    "host-d": ["/srv/gluster/brick-store/gtest/brick/.glusterfs/aa/bb/d"],
                }
            },
        )

        self.assertEqual("cleanup_dead_gfid", result.action_type)
        self.assertEqual("delete_dead_gfid_residue", next(note for note in result.notes if note.startswith("repair strategy: ")).removeprefix("repair strategy: "))
        self.assertGreaterEqual(len(result.steps), 12)
        self.assertEqual("remove_stale_gfid", result.steps[4].step_type)
        self.assertEqual("host-a", result.steps[4].host)
        self.assertEqual("remove_stale_dir_gfid", result.steps[-1].step_type)
        self.assertEqual("host-d", result.steps[-1].host)

    def test_build_cleanup_result_uses_per_host_roots_for_directory_mask(self) -> None:
        roots = {
            "host-a": "/gluster/gtest4/brick",
            "host-b": "/gluster/gtest4/brick",
            "host-c": "/gluster/gtest4/brick",
            "host-d": "/gluster/gtest4-alias/brick",
        }
        index_roots = {
            host: f"{root}/.glusterfs/indices/xattrop" for host, root in roots.items()
        }
        result = _build_cleanup_result(
            volume="gtest4",
            scenario="stage6-directory-tie",
            kind="directory-gfid-mask",
            mount_root="/gtest4",
            backend_root=roots["host-a"],
            brick_roots=roots,
            brick_hosts=list(roots),
            ssh_user="gluster-repair",
            state={
                "backend_root": roots["host-a"],
                "index_root": index_roots["host-a"],
                "index_root_by_host": index_roots,
                "left_hosts": ["host-a", "host-b"],
                "right_hosts": ["host-c", "host-d"],
                "left_gfid_uuid": "11111111-1111-1111-1111-111111111111",
                "right_gfid_uuid": "22222222-2222-2222-2222-222222222222",
                "directory_gfid_paths_by_host": {
                    host: [f"{root}/.glusterfs/aa/bb/{host}"] for host, root in roots.items()
                },
            },
        )

        self.assertEqual("cleanup_dead_gfid", result.action_type)
        host_d_root = next(
            step for step in result.steps
            if step.host == "host-d" and step.step_id.endswith("remove-scenario-root")
        )
        self.assertEqual("/gluster/gtest4-alias/brick/stage6-directory-tie", host_d_root.target_path)
        host_d_index = next(
            step for step in result.steps
            if step.host == "host-d" and step.step_id.endswith("remove-directory-index")
        )
        self.assertEqual(
            "/gluster/gtest4-alias/brick/.glusterfs/indices/xattrop/22222222-2222-2222-2222-222222222222",
            host_d_index.target_path,
        )

    def test_build_cleanup_result_uses_handle_ghost_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="file-handle-ghost",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "index_root": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop",
                "index_host": "host-a",
                "ghost_host": "host-b",
                "gfid_uuid": "6a36631d-94c2-46b4-b8e4-66646840389a",
                "ghost_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            },
        )

        self.assertEqual("cleanup_dead_gfid", result.action_type)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("remove_stale_file_gfid", step_types)
        self.assertIn("remove_stale_index_ghost", step_types)
        ghost_step = next(step for step in result.steps if step.step_type == "remove_stale_file_gfid")
        self.assertEqual("host-b", ghost_step.host)

    def test_build_cleanup_result_uses_directory_mdata_no_majority_gfid_step(self) -> None:
        result = _build_cleanup_result(
            volume="gtest3",
            scenario="repair-canary-mdata-tie",
            kind="directory-mdata-no-majority",
            mount_root="/gtest3",
            backend_root="/gluster/gtest3b/brick",
            brick_hosts=["host-a", "host-b", "host-c"],
            ssh_user="gluster-repair",
            state={
                "marker_host": "host-a",
                "directory_gfid_path": "/gluster/gtest3b/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            },
        )

        self.assertEqual("cleanup_dead_gfid", result.action_type)
        gfid_step = next(step for step in result.steps if step.step_type == "remove_stale_dir_gfid")
        self.assertEqual("host-a", gfid_step.host)

    def test_build_cleanup_result_uses_file_data_split_brain_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="file-data-split-brain",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b", "host-c", "host-d"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "index_root": "/srv/gluster/brick-store/gtest/brick/.glusterfs/indices/xattrop",
                "index_hosts": ["host-a", "host-c", "host-b", "host-d"],
                "left_hosts": ["host-a", "host-b"],
                "right_hosts": ["host-c", "host-d"],
                "left_gfid_uuid": "11111111-2222-3333-4444-555555555555",
                "left_gfid_hex": "11111111222233334444555555555555",
                "right_gfid_uuid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
                "right_gfid_hex": "66666666777788889999aaaaaaaaaaaa",
                "left_file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
                "right_file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/66/66/66666666-7777-8888-9999-aaaaaaaaaaaa",
                "brick_roles_by_host": {"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"},
            },
        )

        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("remove_stale_file_gfid", step_types)
        self.assertIn("remove_stale_gfid", step_types)
        self.assertEqual({"host-a": "data", "host-b": "data", "host-c": "arbiter", "host-d": "data"}, result.brick_roles_by_host)
        rendered = render_apply_run([result])
        self.assertIn("brick roles by host:", rendered)
        self.assertIn("host-c: arbiter", rendered)

    def test_build_cleanup_result_uses_file_content_split_brain_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest4",
            scenario="repair-canary-content-split",
            kind="file-content-split-brain",
            mount_root="/gtest4",
            backend_root="/gluster/gtest4b/gtest4/brick",
            brick_hosts=["node-a", "node-b", "node-c", "node-d"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/gluster/gtest4b/gtest4/brick",
                "index_root": "/gluster/gtest4b/gtest4/brick/.glusterfs/indices/xattrop",
                "index_hosts": ["node-a", "node-b", "node-c", "node-d"],
                "left_hosts": ["node-a", "node-b"],
                "right_hosts": ["node-c", "node-d"],
                "gfid_uuid": "11111111-2222-3333-4444-555555555555",
                "file_gfid_path": "/gluster/gtest4b/gtest4/brick/.glusterfs/11/11/11111111-2222-3333-4444-555555555555",
            },
        )

        self.assertEqual("cleanup_dead_file_refs", result.action_type)
        gfid_steps = [step for step in result.steps if step.step_type == "remove_stale_file_gfid"]
        index_steps = [step for step in result.steps if step.step_type == "remove_stale_gfid"]
        self.assertEqual({"node-a", "node-b", "node-c", "node-d"}, {step.host for step in gfid_steps})
        self.assertEqual({"node-a", "node-b", "node-c", "node-d"}, {step.host for step in index_steps})
        self.assertTrue(all("11111111-2222-3333-4444-555555555555" in step.target_path for step in index_steps))

    def test_build_cleanup_result_uses_orphaned_gfid_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="orphaned-gfid-hardlink",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                "orphan_host": "host-b",
                "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
            },
        )

        self.assertEqual("cleanup_dead_gfid", result.action_type)
        step_types = [step.step_type for step in result.steps]
        self.assertIn("remove_stale_backend", step_types)
        self.assertIn("remove_stale_file_gfid", step_types)
        orphan_step = next(step for step in result.steps if step.step_type == "remove_stale_file_gfid")
        self.assertEqual("host-b", orphan_step.host)

    def test_build_cleanup_result_uses_symlink_missing_stale_steps(self) -> None:
        result = _build_cleanup_result(
            volume="gtest",
            scenario="repair-canary-speed",
            kind="symlink-missing-stale",
            mount_root="/gtest",
            backend_root="/srv/gluster/brick-store/gtest/brick",
            brick_hosts=["host-a", "host-b", "host-c", "host-d"],
            ssh_user="gluster-repair",
            state={
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "backend_target": "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                "backend_targets_by_host": {
                    "host-a": "/gluster/homea/gtest/brick/repair-canary-speed/alpha/payload.txt",
                    "host-b": "/srv/gluster/brick-store/gtest/brick/repair-canary-speed/alpha/payload.txt",
                    "host-c": "/gluster/homec/gtest/brick/repair-canary-speed/alpha/payload.txt",
                    "host-d": "/gluster/homed/gtest/brick/repair-canary-speed/alpha/payload.txt",
                },
                "symlink_hosts": ["host-a", "host-b"],
                "stale_hosts": ["host-c", "host-d"],
                "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                "file_gfid_paths_by_host": {
                    "host-a": "/gluster/homea/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                    "host-b": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                    "host-c": "/gluster/homec/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                    "host-d": "/gluster/homed/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
                },
            },
        )

        self.assertEqual("cleanup_orphaned_symlink", result.action_type)
        step_types = [step.step_type for step in result.steps]
        self.assertEqual(12, len(result.steps))
        self.assertEqual(
            ["remove_stale_backend", "remove_stale_backend", "remove_stale_backend", "remove_stale_backend"],
            step_types[:4],
        )
        self.assertIn("remove_stale_backend", step_types)
        self.assertIn("remove_stale_file_gfid", step_types)
        self.assertEqual("host-a", result.steps[0].host)
        self.assertEqual("host-a", result.steps[4].host)
        self.assertEqual("/gluster/homed/gtest/brick/repair-canary-speed/alpha/payload.txt", result.steps[7].target_path)
        self.assertEqual("/gluster/homed/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a", result.steps[11].target_path)

    def test_cleanup_canary_uses_repair_executor(self) -> None:
        state = {
            "kind": "missing-file-replica",
            "backend_root": "/srv/gluster/brick-store/gtest/brick",
            "missing_host": "host-b",
            "file_name": "payload.txt",
            "dir_name": "alpha",
            "file_gfid_path": "/srv/gluster/brick-store/gtest/brick/.glusterfs/6a/36/6a36631d-94c2-46b4-b8e4-66646840389a",
        }
        calls: list[object] = []

        def fake_execute(results, *, keep_going=False, stage_only=False, progress_stream=None):  # noqa: ANN001
            calls.append((results, keep_going, stage_only, progress_stream))
            results[0].status = "completed"
            return {
                "summary": {
                    "completed_actions": 1,
                    "failed_actions": 0,
                    "completed_with_skips": 0,
                }
            }

        with patch("gluster_heal_tool.canary._brick_hosts_and_paths", return_value=(["host-a", "host-b"], {"host-a": "/srv/gluster/brick-store/gtest/brick", "host-b": "/srv/gluster/brick-store/gtest/brick"})), \
            patch("gluster_heal_tool.canary._read_state", return_value=state), \
            patch("gluster_heal_tool.canary.validate_execute_results", return_value=(True, [])), \
            patch("gluster_heal_tool.canary.execute_apply_results", side_effect=fake_execute), \
            patch("gluster_heal_tool.canary._local_remove"), \
            patch("gluster_heal_tool.canary._delete_state"):
            cleanup_canary(
                volume="gtest",
                scenario="repair-canary-speed",
                dir_name="alpha",
                file_name="payload.txt",
            )

        self.assertEqual(1, len(calls))
        results, keep_going, stage_only, progress_stream = calls[0]
        self.assertTrue(keep_going)
        self.assertFalse(stage_only)
        self.assertIsNone(progress_stream)
        self.assertEqual("cleanup_dead_file_refs", results[0].action_type)
        self.assertEqual("completed", results[0].status)


    def test_cleanup_canary_wave_uses_repair_executor(self) -> None:
        states = {
            "cleanup-wave-c": {
                "kind": "symlink-missing-stale",
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "dir_name": "alpha",
                "file_name": "wave-c.txt",
            },
            "cleanup-wave-d": {
                "kind": "symlink-missing-stale",
                "backend_root": "/srv/gluster/brick-store/gtest/brick",
                "dir_name": "beta",
                "file_name": "wave-d.txt",
            },
        }
        build_calls: list[str] = []
        execute_calls: list[object] = []

        def fake_build_cleanup_result(**kwargs):
            scenario = str(kwargs["scenario"])
            build_calls.append(scenario)
            payload = {
                "action_id": f"canary-cleanup-{scenario}",
                "logical_path": f"/gtest/{scenario}",
                "action_type": "cleanup_orphaned_symlink",
                "execution_mode": "plan",
                "backup_root": "",
                "backup_mode": "none",
                "batch": True,
                "status": "proposed",
                "parallel_safe": False,
                "execution_wave": 0,
                "execution_resources": [],
                "steps": [],
                "revert_steps": [],
                "backup_artifacts": [],
                "revert_dirs_to_create": [],
                "estimated_stage_bytes": 0,
                "estimated_backup_bytes": 0,
                "estimated_unknown_backup_items": 0,
                "native_heal_first": False,
                "native_heal_fallback_action": "",
                "native_heal_reason": "",
                "recommended_choice": "",
                "recommended_reason": "",
                "brick_roles_by_host": {},
                "decision": {},
                "notes": [],
            }
            return SimpleNamespace(**payload)

        def fake_execute(results, *, keep_going=False, stage_only=False, progress_stream=None, parallel_actions=1, parallel_nice=5, run_dir=None):
            execute_calls.append((results, keep_going, stage_only, progress_stream, parallel_actions, parallel_nice, run_dir))
            for result in results:
                result.status = "completed"
            return {
                "summary": {
                    "completed_actions": len(results),
                    "failed_actions": 0,
                    "completed_with_skips": 0,
                    "execution_waves": 1,
                    "parallel_actions_requested": parallel_actions,
                    "parallel_nice": parallel_nice,
                },
                "run_dir": str(run_dir),
            }

        with (
            patch("gluster_heal_tool.canary._brick_hosts_and_paths", return_value=(["host-a", "host-b"], {"host-a": "/srv/gluster/brick-store/gtest/brick", "host-b": "/srv/gluster/brick-store/gtest/brick"})),
            patch("gluster_heal_tool.canary._read_state", side_effect=lambda volume, scenario: states[str(scenario)]),
            patch("gluster_heal_tool.canary._mountpoint_is_active", return_value=False),
            patch("gluster_heal_tool.canary._build_cleanup_result", side_effect=fake_build_cleanup_result),
            patch("gluster_heal_tool.canary.validate_execute_results", return_value=(True, [])),
            patch("gluster_heal_tool.canary.execute_apply_results", side_effect=fake_execute),
            patch("gluster_heal_tool.canary._unmount_canary_mount"),
            patch("gluster_heal_tool.canary._local_remove"),
            patch("gluster_heal_tool.canary._delete_state"),
        ):
            cleanup_canary_wave(
                volume="gtest",
                scenarios=["cleanup-wave-c", "cleanup-wave-d"],
                dir_name="alpha",
                file_name="payload.txt",
                parallel_actions=2,
                parallel_nice=5,
                run_dir="/tmp/cleanup-wave-test",
            )

        self.assertEqual(["cleanup-wave-c", "cleanup-wave-d"], build_calls)
        self.assertEqual(1, len(execute_calls))
        results, keep_going, stage_only, progress_stream, parallel_actions, parallel_nice, run_dir = execute_calls[0]
        self.assertTrue(keep_going)
        self.assertFalse(stage_only)
        self.assertIsNone(progress_stream)
        self.assertEqual(2, parallel_actions)
        self.assertEqual(5, parallel_nice)
        self.assertEqual("/tmp/cleanup-wave-test", str(run_dir))
        self.assertEqual(["completed", "completed"], [result.status for result in results])

if __name__ == "__main__":
    import unittest

    unittest.main()
