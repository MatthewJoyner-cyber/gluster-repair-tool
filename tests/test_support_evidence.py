# SPDX-License-Identifier: GPL-2.0-only
"""Fresh split-brain identity and support-draft safety regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool import manager
from gluster_heal_tool import worker
from gluster_heal_tool.models import ManifestObject, ResolutionObservation
from gluster_heal_tool.planner import build_plan
from gluster_heal_tool.support_bundle import prepare_support_bundle


GFID = "11111111-2222-3333-4444-555555555555"
INDEX = f"/.glusterfs/indices/xattrop/{GFID}"


class LiveSplitBrainRouteTests(unittest.TestCase):
    def test_path_only_split_brain_row_blocks_identity_based_cleanup(self) -> None:
        heal_info = "Brick brick-a:/brick\n/data/file - Is in split-brain\n"
        with patch.object(manager, "get_heal_info_text", return_value=heal_info):
            gfids, error = manager._live_split_brain_gfids("testvol")
        self.assertEqual(set(), gfids)
        self.assertIn("lacks a GFID identity", error)

    def test_path_gfid_and_index_routes_forward_same_live_identity(self) -> None:
        context = {
            "volume": "testvol", "mountpoint": "/testvol", "logical_path": "data/file",
            "raw_entry": "/data/file", "source_host": "localhost",
            "source": "localhost:testvol", "fstype": "fuse.glusterfs",
            "options": "rw", "requested_path": "/testvol/data/file",
        }
        common = dict(resolver_path="/resolver", worker_path="/worker",
                      manifest_out="/tmp/manifest", observations_out="/tmp/observations")
        with (
            patch.object(manager, "_validate_path_mount_context", return_value=context),
            patch.object(manager, "_live_split_brain_gfids", return_value=({GFID}, "")),
            patch.object(manager, "discover_brick_hosts", return_value=["brick-a"]),
            patch.object(manager, "discover_brick_paths", return_value={"brick-a": "/brick"}),
            patch.object(manager, "_resolve_entries_via_workers", return_value={}) as resolve,
        ):
            manager.resolve_via_path(path="/testvol/data/file", **common)
            manager.resolve_via_gfid(volume="testvol", gfid=GFID, mountpoint="/testvol", **common)
            manager.resolve_via_index_entry(volume="testvol", index_entry=INDEX,
                                            mountpoint="/testvol", **common)
        self.assertEqual(3, resolve.call_count)
        for call in resolve.call_args_list:
            self.assertEqual({GFID}, call.kwargs["split_brain_gfids"])
            self.assertEqual("", call.kwargs["split_brain_evidence_error"])

    def test_path_gfid_and_index_object_receive_same_split_brain_note(self) -> None:
        objects = {
            "path": ManifestObject("data/file", "file", 2, file_gfids=[GFID]),
            "gfid": ManifestObject("dead-gfid:" + GFID, "file", 1, gfids=[GFID]),
            "index": ManifestObject(INDEX.lstrip("/"), "stale_glusterfs_index", 4,
                                    raw_entries=[INDEX]),
        }
        manager._annotate_live_split_brain(objects, {GFID})
        for obj in objects.values():
            self.assertIn("heal_info_marks_split_brain", obj.notes)
            self.assertIn("live_split_brain_checked", obj.notes)

    def test_unavailable_live_evidence_blocks_operator_index_cleanup(self) -> None:
        obj = ManifestObject(
            INDEX.lstrip("/"), "stale_glusterfs_index", 4,
            input_source="operator_index", source_hosts=["localhost"], raw_entries=[INDEX],
            observations={"brick-a": [ResolutionObservation(
                host="brick-a", raw_entry=INDEX,
                backend=f"/brick{INDEX}", backend_exists=True,
            )]},
        )
        manager._annotate_live_split_brain({"index": obj}, set(), "heal info unavailable")
        action = build_plan({obj.logical_path: obj}, mountpoint="/testvol")[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertIn("live_split_brain_evidence_unavailable", action.notes)

    def test_localhost_seed_is_not_a_brick_but_all_queried_bricks_need_proof(self) -> None:
        raw = f"<gfid:{GFID}>"
        proof = ResolutionObservation(
            host="brick-a", raw_entry=raw, backend="/brick/data/file",
            backend_exists=True, backend_trusted_gfid=GFID,
            gfid_path=f"/brick{INDEX}",
        )
        obj = ManifestObject(
            raw, "dead_gfid", 0, input_source="operator_gfid",
            source_hosts=["localhost"], raw_entries=[raw], gfids=[GFID],
            dead_gfids=[GFID], observations={"brick-a": [proof]},
            notes=["live_split_brain_checked"],
        )
        action = build_plan({raw: obj}, mountpoint="/testvol")[0]
        self.assertEqual("cleanup_stale_glusterfs_index", action.action_type)
        obj.observations["brick-b"] = [ResolutionObservation(host="brick-b", raw_entry=raw)]
        action = build_plan({raw: obj}, mountpoint="/testvol")[0]
        self.assertEqual("review_dead_gfid_reference", action.action_type)
        self.assertIn("brick-b", " ".join(action.notes))


class SupportBundleTests(unittest.TestCase):
    def test_afr_inspection_uses_read_only_xattr_calls(self) -> None:
        with (
            patch.object(worker.os.path, "lexists", return_value=True),
            patch.object(worker.os, "listxattr", return_value=["trusted.afr.client-0"]),
            patch.object(worker.os, "getxattr", return_value=b"\x00" * 12),
            patch.object(worker.os, "setxattr") as setxattr,
            patch.object(worker.os, "removexattr") as removexattr,
        ):
            result = worker._inspect_afr_state("/brick/canary/file")
        self.assertEqual(["trusted.afr.client-0"], result["afr_xattr_names"])
        setxattr.assert_not_called()
        removexattr.assert_not_called()

    def test_redacts_copy_and_reports_missing_without_claiming_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "heal.txt"
            private_contact = "secret" + "@" + "example.test"
            source.write_text(f"brick private-node and owner {private_contact}\n", encoding="utf-8")
            result = prepare_support_bundle(
                root / "bundle",
                {"heal_info": source, "afr_inspection": root / "missing-afr.txt"},
                private_identifiers=["private-node", private_contact],
            )
            copied = (root / "bundle" / "heal_info.txt").read_text(encoding="utf-8")
            self.assertNotIn("private-node", copied)
            self.assertNotIn(private_contact, copied)
            self.assertEqual("missing", result["inventory"]["afr_inspection"]["status"])
            self.assertEqual("copied_redacted", result["inventory"]["heal_info"]["status"])
            self.assertIn("not submitted", result["draft"])
            self.assertNotIn("afr_inspection.txt", json.dumps(result["inventory"]))

    def test_unlisted_private_identifier_refuses_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resolver.txt"
            source.write_text("operator unknown" + "@" + "example.test\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unlisted private identifier"):
                prepare_support_bundle(root / "bundle", {"resolver_record": source},
                                       private_identifiers=["known-private-token"])
            self.assertFalse((root / "bundle").exists())


if __name__ == "__main__":
    unittest.main()
