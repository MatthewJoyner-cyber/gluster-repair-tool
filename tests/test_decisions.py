# SPDX-License-Identifier: GPL-2.0-only
"""Tests for decision reports carrying controller-cycle context."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gluster_heal_tool import decisions
from gluster_heal_tool.decisions import build_decision_report
from gluster_heal_tool.protocol import ChecksumTarget


class DecisionReportControllerCycleTests(unittest.TestCase):
    def test_checksum_worker_result_keeps_queried_brick_endpoint_identity(self) -> None:
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"checksums": [{"host": "node-a", "backend_path": "/alias/alpha", "sha256": "abc", "error": ""}]}',
                "stderr": "",
            },
        )()
        with patch("gluster_heal_tool.decisions.subprocess.run", return_value=completed):
            result = decisions._run_checksum_batch(
                "192.0.2.30",
                "gluster-repair",
                "/worker",
                [ChecksumTarget(logical_path="alpha", backend_path="/alias/alpha")],
            )

        self.assertEqual("192.0.2.30", result[0]["host"])
        self.assertEqual("/alias/alpha", result[0]["backend_path"])

    def test_decision_report_includes_controller_cycle_context(self) -> None:
        report = build_decision_report(
            {"actions": []},
            volume="gtest",
            worker_path="",
            hosts=[],
            status={
                "controller_cycle": {
                    "controller_stop": True,
                    "controller_reason": "graph cycle detected",
                    "graph_cycle_present": True,
                    "graph_cycle_repeat_count": 2,
                    "graph_cycle_detected": True,
                    "heal_repeat_count": 5,
                    "heal_repeat_hit_limit": True,
                    "heal_repeat_warning": "post-execute heal info shape has not changed for 5 consecutive checks",
                }
            },
        )

        self.assertIn("controller_cycle", report)
        self.assertIn("controller_cycle_report", report)
        self.assertEqual("rebuild-plan", report["controller_cycle"]["controller_next_action"])
        self.assertTrue(report["controller_cycle"]["controller_stop"])
        self.assertEqual(
            "controller-cycle: stop=yes, next_action=rebuild-plan, reason=graph cycle detected",
            report["controller_cycle_report"].splitlines()[0],
        )

    def test_decision_report_uses_controller_hint_for_recommendation(self) -> None:
        report = build_decision_report(
            {
                "actions": [
                    {
                        "repair_strategy": "ambiguous_entry_split_brain_file",
                        "logical_path": "/data/file",
                        "winner_file_gfid": "winner-gfid",
                        "winner_host": "host-a",
                        "file_cohorts": [
                            {
                                "identity": "cohort-a",
                                "hosts": ["host-a"],
                                "latest_mtime": 123,
                                "largest_size": 64,
                            },
                            {
                                "identity": "cohort-b",
                                "hosts": ["host-b"],
                                "latest_mtime": 122,
                                "largest_size": 64,
                            },
                        ],
                        "file_copies": [
                            {
                                "identity": "cohort-a",
                                "host": "host-a",
                                "backend": "/brick/a",
                                "mtime": 123,
                                "size": 64,
                            },
                            {
                                "identity": "cohort-b",
                                "host": "host-b",
                                "backend": "/brick/b",
                                "mtime": 122,
                                "size": 64,
                            },
                        ],
                    }
                ]
            },
            volume="gtest",
            worker_path="",
            hosts=[],
            status={
                "controller_cycle": {
                    "controller_stop": True,
                    "controller_reason": "graph cycle detected",
                    "graph_cycle_present": True,
                    "graph_cycle_repeat_count": 2,
                    "graph_cycle_detected": True,
                    "heal_repeat_count": 5,
                    "heal_repeat_hit_limit": True,
                    "heal_repeat_warning": "post-execute heal info shape has not changed for 5 consecutive checks",
                }
            },
        )

        self.assertEqual("rebuild-plan", report["controller_cycle_next_action"])
        self.assertEqual("rebuild-plan", report["items"][0]["recommendation"])
        self.assertEqual("rebuild-plan", report["items"][0]["controller_next_action"])

    def test_decision_report_includes_arbiter_backed_identity_conflict(self) -> None:
        report = build_decision_report(
            {
                "actions": [
                    {
                        "action_type": "review_entry_split_brain",
                        "repair_strategy": "arbiter_backed_data_identity_conflict",
                        "logical_path": "/data/file",
                        "recommended_choice": "quarantine_loser",
                        "winner_file_gfid": "winner-gfid",
                        "winner_host": "data-a",
                        "file_cohorts": [
                            {"identity": "winner-gfid", "hosts": ["data-a"], "latest_mtime": 123, "largest_size": 64},
                            {"identity": "loser-gfid", "hosts": ["data-b"], "latest_mtime": 122, "largest_size": 64},
                        ],
                        "file_copies": [
                            {"identity": "winner-gfid", "host": "data-a", "backend": "/brick/a", "mtime": 123, "size": 64},
                            {"identity": "loser-gfid", "host": "data-b", "backend": "/brick/b", "mtime": 122, "size": 64},
                        ],
                    }
                ]
            },
            volume="gtest3a",
            worker_path="",
            hosts=[],
        )

        self.assertEqual(1, report["ambiguous_actions"])
        self.assertEqual("/data/file", report["items"][0]["logical_path"])
        self.assertEqual("unresolved", report["items"][0]["outcome"])
        self.assertEqual("quarantine_loser", report["items"][0]["recommended_choice"])
        self.assertEqual(
            ["quarantine_loser", "quarantine_both", "keep_review", "skip"],
            report["items"][0]["operator_choices"],
        )

    def test_decision_report_includes_directory_mdata_source_choice_evidence(self) -> None:
        report = build_decision_report(
            {
                "actions": [
                    {
                        "action_type": "review_directory_metadata",
                        "repair_strategy": "review_directory_mdata_state",
                        "logical_path": "/data/dir",
                        "directory_canonical_gfid": "6a36631d-94c2-46b4-b8e4-66646840389a",
                        "directory_canonical_host": "host-b",
                        "directory_backend_by_host": {
                            "host-a": "/brick/a/data/dir",
                            "host-b": "/brick/b/data/dir",
                            "host-c": "/brick/c/data/dir",
                        },
                        "directory_mdata_by_host": {
                            "host-a": "0x01020304",
                            "host-b": "0x05060708",
                            "host-c": "0x090a0b0c",
                        },
                    }
                ]
            },
            volume="gtest",
            worker_path="",
            hosts=[],
            status={},
        )

        self.assertEqual(1, report["ambiguous_actions"])
        self.assertEqual(1, report["outcomes"]["no-majority"])
        item = report["items"][0]
        self.assertEqual("no-majority", item["outcome"])
        self.assertEqual("review_or_source_choice", item["recommendation"])
        self.assertEqual("continue", item["controller_next_action"])
        self.assertEqual(
            [
                {"host": "host-a", "backend_path": "/brick/a/data/dir", "mdata_hex": "0x01020304"},
                {"host": "host-b", "backend_path": "/brick/b/data/dir", "mdata_hex": "0x05060708"},
                {"host": "host-c", "backend_path": "/brick/c/data/dir", "mdata_hex": "0x090a0b0c"},
            ],
            item["mdata_by_host"],
        )
        self.assertIn("mdata_source_host", item["operator_choices"])
        self.assertIn("choose a source brick or source value explicitly", item["reason"])


    def test_decision_report_includes_posix_source_choice_evidence(self) -> None:
        report = build_decision_report(
            {
                "actions": [{
                    "action_type": "review_posix_metadata_no_majority",
                    "repair_strategy": "choose_posix_metadata_source",
                    "logical_path": "/data/file",
                    "metadata_backend_by_host": {"host-a": "/brick/a/file", "host-b": "/brick/b/file", "host-c": "/brick/c/file"},
                    "metadata_tuple_by_host": {
                        "host-a": {"mode_bits": 420, "uid": 0, "gid": 0, "acl_access": "", "acl_default": ""},
                        "host-b": {"mode_bits": 384, "uid": 0, "gid": 0, "acl_access": "", "acl_default": ""},
                        "host-c": {"mode_bits": 256, "uid": 0, "gid": 0, "acl_access": "", "acl_default": ""},
                    },
                }]
            },
            volume="gtest3", worker_path="", hosts=[], status={},
        )
        self.assertEqual(1, report["ambiguous_actions"])
        item = report["items"][0]
        self.assertEqual("no-majority", item["outcome"])
        self.assertEqual("review_or_source_choice", item["recommendation"])
        self.assertIn("metadata_source_host", item["operator_choices"])
        self.assertEqual(["host-a", "host-b", "host-c"], [value["host"] for value in item["posix_metadata_by_host"]])



    def test_decision_report_marks_native_posix_source_brick_mode(self) -> None:
        report = build_decision_report(
            {
                "actions": [{
                    "action_type": "review_posix_metadata_no_majority",
                    "repair_strategy": "choose_posix_metadata_source",
                    "logical_path": "/data/file",
                    "gluster_visible_metadata_split_brain": True,
                    "metadata_source_reason": "native_gluster_source",
                    "metadata_backend_by_host": {
                        "host-a": "/brick/a/file",
                        "host-b": "/brick/b/file",
                        "host-c": "/brick/c/file",
                    },
                    "metadata_tuple_by_host": {
                        "host-a": {"mode_bits": 420, "uid": 0, "gid": 0, "acl_access": "", "acl_default": ""},
                        "host-b": {"mode_bits": 384, "uid": 0, "gid": 0, "acl_access": "", "acl_default": ""},
                        "host-c": {"mode_bits": 256, "uid": 0, "gid": 0, "acl_access": "", "acl_default": ""},
                    },
                }]
            },
            volume="gtest",
            worker_path="",
            hosts=[],
            status={},
        )

        item = report["items"][0]
        self.assertTrue(item["gluster_visible_metadata_split_brain"])
        self.assertEqual("native_source_brick", item["resolution_mode"])
        self.assertIn("native source-brick resolution", item["reason"])
        self.assertIn("metadata_source_host", item["operator_choices"])



if __name__ == "__main__":
    unittest.main()
