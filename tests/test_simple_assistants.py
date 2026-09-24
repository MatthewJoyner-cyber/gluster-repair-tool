# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for bounded Stage 2 simple-mode evidence assistants."""
from __future__ import annotations

import unittest

from gluster_heal_tool.simple_assistants import build_simple_assistant_report


def _plan_action(**overrides: object) -> dict[str, object]:
    action: dict[str, object] = {
        "action_id": "review:/data/file",
        "logical_path": "/data/file",
        "action_type": "review_entry_split_brain",
        "repair_strategy": "ambiguous_entry_split_brain_file",
        "file_copies": [
            {"host": "brick-a", "backend": "/brick/a", "identity": "gfid-a"},
            {"host": "brick-b", "backend": "/brick/b", "identity": "gfid-b"},
        ],
        "file_cohorts": [
            {"identity": "gfid-a", "hosts": ["brick-a"]},
            {"identity": "gfid-b", "hosts": ["brick-b"]},
        ],
    }
    action.update(overrides)
    return action


class SimpleAssistantTests(unittest.TestCase):
    def test_checksum_assistant_refreshes_content_outcome_and_fingerprint(self) -> None:
        def checksum_builder(action: dict[str, object], **_kwargs: object) -> dict[str, object]:
            return {
                "outcome": "content-equal",
                "reason": "tied copies have identical checksums",
                "items": [
                    {"host": "brick-a", "sha256": "same"},
                    {"host": "brick-b", "sha256": "same"},
                ],
            }

        report = build_simple_assistant_report(
            {"actions": [_plan_action()]},
            volume="gtest3",
            worker_path="/unused",
            ssh_user="operator",
            checksum_builder=checksum_builder,
        )
        item = report["items"][0]
        self.assertEqual("content-equal", item["details"]["checksum"]["outcome"])
        self.assertTrue(
            any(note.startswith("checksum outcome: content-equal") for note in item["evidence"])
        )
        self.assertTrue(item["fingerprint"])

    def test_posix_and_live_reference_assistants_render_protection(self) -> None:
        action = _plan_action(
            action_id="review:/data/ghost",
            logical_path="/data/ghost",
            action_type="review_dead_gfid_reference",
            repair_strategy="follow_live_reference",
            file_copies=[],
            file_cohorts=[],
            metadata_tuple_by_host={
                "brick-a": {"mode_bits": 420, "uid": 1000, "gid": 1000},
                "arbiter-a": {"mode_bits": 292, "uid": 1000, "gid": 1000},
            },
            brick_roles_by_host={"brick-a": "data", "arbiter-a": "arbiter"},
            dead_gfid_live_references=["/live/reference"],
        )
        report = build_simple_assistant_report(
            {"actions": [action]},
            volume="gtest3",
            worker_path="/unused",
            ssh_user="operator",
        )
        evidence = chr(10).join(report["items"][0]["evidence"])
        self.assertIn("POSIX tuple brick-a", evidence)
        self.assertIn("arbiter restriction", evidence)
        self.assertIn("live-reference targets: /live/reference", evidence)
        self.assertEqual(1, report["summary"]["assistant_items"])

    def test_directory_assistant_reports_budget_and_recommendation(self) -> None:
        action = {
            "action_id": "review:/data/dir",
            "logical_path": "/data/dir",
            "action_type": "review_directory_gfid_conflict",
            "repair_strategy": "review_directory_gfid_conflict",
            "depth": 1,
            "healthy_hosts": ["brick-a", "brick-b"],
            "missing_hosts": [],
            "directory_child_names_by_host": {
                "brick-a": ["left.txt"],
                "brick-b": ["right.txt"],
            },
            "directory_metadata_mismatch_hosts": [],
            "brick_roles_by_host": {"brick-a": "data", "brick-b": "data"},
        }
        report = build_simple_assistant_report(
            {"actions": [action]},
            volume="gtest3",
            worker_path="/unused",
            ssh_user="operator",
        )
        item = report["items"][0]
        evidence = chr(10).join(item["evidence"])
        self.assertIn("directory-tie report:", evidence)
        self.assertIn("directory-tie budget:", evidence)
        self.assertEqual(1, report["summary"]["directory_tie_actions"])


if __name__ == "__main__":
    unittest.main()
