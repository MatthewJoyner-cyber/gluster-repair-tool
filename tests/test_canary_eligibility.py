# SPDX-License-Identifier: GPL-2.0-only
"""Source-choice state must retain identity, topology and actual heal rows."""
import copy
import unittest
from unittest.mock import patch

from gluster_heal_tool.canary_file import build_file_posix_metadata_split_brain_plan_from_canary_state as build_plan


def posix_state_fixture():
    logical = "fixture/alpha/payload.txt"
    hosts = ["host-a", "host-b", "host-c"]
    row = f"Brick host-a:/brick/host-a\n/{logical}\nStatus: Connected\nNumber of entries: 1"
    return {
        "schema_version": 1, "kind": "file-posix-metadata-split-brain",
        "volume": "example", "scenario": "fixture", "construction_class": "afr-synthesized",
        "fixture_scope": "x3-upstream-derived-fixture", "proof_label": "afr-synthesized",
        "source_choice_eligible": True, "leave_heal_pending": False, "heal_crawl_triggered": True,
        "gluster_visible_metadata_split_brain": True,
        "mount_root": "/mnt/example", "mount_file": f"/mnt/example/{logical}",
        "gfid_uuid": "12345678-1234-1234-1234-123456789abc", "source_host": "host-a",
        "backend_roots": {host: f"/brick/{host}" for host in hosts},
        "backend_by_host": {host: f"/brick/{host}/{logical}" for host in hosts},
        "metadata_backend_by_host": {host: f"/brick/{host}/{logical}" for host in hosts},
        "metadata_tuple_by_host": {host: {"mode_bits": mode, "uid": 1000, "gid": 1000,
                                          "acl_access": "", "acl_default": ""}
                                   for host, mode in zip(hosts, [0o644, 0o640, 0o600])},
        "brick_roles_by_host": {host: "data" for host in hosts},
        **{name: row for name in ("heal_info_before", "heal_info_after", "heal_info_split_brain_before", "heal_info_split_brain_after")},
    }


class CanaryEligibilityTests(unittest.TestCase):
    def build(self, state):
        # State conversion must not mix saved tuples with a fresh, unrelated topology.
        with patch("gluster_heal_tool.canary_file._read_state", return_value=state), \
             patch("gluster_heal_tool.canary_shared._brick_roles", side_effect=AssertionError("unexpected live discovery")):
            return build_plan(volume="example", scenario="fixture")

    def assert_refused(self, state):
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeError):
            self.build(state)
        self.assertEqual(before, state)

    def test_valid_x3_remains_a_harness_review_plan(self):
        for role in ("data", "arbiter"):
            with self.subTest(third_role=role):
                state = posix_state_fixture()
                state["brick_roles_by_host"]["host-c"] = role
                plan = self.build(state)
                self.assertEqual("harness-only", plan["proof_scope"])
                self.assertEqual("review_posix_metadata_no_majority", plan["actions"][0]["action_type"])
                self.assertEqual("fixture/alpha/payload.txt", plan["actions"][0]["logical_path"])
                self.assertEqual(state["brick_roles_by_host"], plan["actions"][0]["brick_roles_by_host"])

    def test_legacy_x4_without_scope_is_refused(self):
        state = posix_state_fixture()
        state.pop("fixture_scope")
        state.pop("schema_version")
        for key in ("backend_roots", "backend_by_host", "metadata_backend_by_host", "metadata_tuple_by_host", "brick_roles_by_host"):
            state[key]["host-d"] = copy.deepcopy(state[key]["host-c"])
        self.assert_refused(state)

    def test_x4_cannot_hide_in_a_conflicting_map(self):
        for key in ("backend_roots", "backend_by_host", "metadata_backend_by_host", "metadata_tuple_by_host", "brick_roles_by_host"):
            with self.subTest(key=key):
                state = posix_state_fixture()
                state[key]["host-d"] = copy.deepcopy(state[key]["host-c"])
                self.assert_refused(state)

    def test_version_identity_and_provenance_are_required(self):
        for key, values in {
            "schema_version": [None, 0, 2, True, "1"], "volume": [None, "different"],
            "scenario": [None, "different"], "fixture_scope": [None, "unknown", "x4-direct-bookkeeping-diagnostic"],
            "construction_class": [None, "operator-path-mechanical"],
            "proof_label": [None, "diagnostic-only", "native-heal-smoke", "harness-only-pending"],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    state = posix_state_fixture()
                    state[key] = value
                    self.assert_refused(state)

    def test_pending_partial_and_conflicting_flags_are_refused(self):
        for key, value in [("leave_heal_pending", True), ("leave_heal_pending", None),
                           ("heal_crawl_triggered", False), ("heal_crawl_triggered", "true"),
                           ("source_choice_eligible", False), ("source_choice_eligible", "true"),
                           ("source_choice_eligible", None), ("gluster_visible_metadata_split_brain", False),
                           ("partial", True)]:
            with self.subTest(key=key, value=value):
                state = posix_state_fixture()
                state[key] = value
                self.assert_refused(state)

    def test_real_rows_are_required_in_both_split_brain_snapshots(self):
        for key in ("heal_info_split_brain_before", "heal_info_split_brain_after"):
            for text in (None, "", "split before", "Brick host-a:/brick/host-a\nStatus: Connected\nNumber of entries in split-brain: 0",
                         "Brick host-a:/brick/host-a\n/payload.txt", "Brick host-a:/brick/host-a\n/fixture/alpha/payload.txt.extra",
                         "Brick host-a:/brick/host-a\n/other/alpha/payload.txt", "Brick unknown:/brick/host-a\n/fixture/alpha/payload.txt",
                         "Brick host-a:/different/brick\n/fixture/alpha/payload.txt"):
                with self.subTest(key=key, text=text):
                    state = posix_state_fixture()
                    state[key] = text
                    self.assert_refused(state)

    def test_exact_gfid_rows_are_supported(self):
        state = posix_state_fixture()
        for key in ("heal_info_split_brain_before", "heal_info_split_brain_after"):
            state[key] = f"Brick host-a:/brick/host-a\n<gfid:{state['gfid_uuid']}> - Is in split-brain"
        self.assertEqual(1, len(self.build(state)["actions"]))
        state["heal_info_split_brain_after"] = state["heal_info_split_brain_after"].replace("123456789abc", "123456789abd")
        self.assert_refused(state)

    def test_incomplete_topology_or_metadata_is_refused(self):
        for key in ("backend_roots", "backend_by_host", "metadata_backend_by_host", "metadata_tuple_by_host", "brick_roles_by_host"):
            state = posix_state_fixture()
            state[key].pop("host-c")
            with self.subTest(key=key):
                self.assert_refused(state)
        for key, value in (("mode_bits", None), ("uid", -1), ("gid", "1000"), ("acl_access", None)):
            state = posix_state_fixture()
            state["metadata_tuple_by_host"]["host-c"][key] = value
            with self.subTest(key=key):
                self.assert_refused(state)

    def test_target_source_and_role_conflicts_are_refused(self):
        for changes in ({"mount_file": "/mnt/example-neighbour/fixture/alpha/payload.txt"},
                        {"mount_file": "/mnt/example/fixture/../payload.txt"}, {"source_host": "unknown"},
                        {"gfid_uuid": "malformed"}):
            state = posix_state_fixture()
            state.update(changes)
            self.assert_refused(state)
        for role in ("unknown", "arbiter"):
            state = posix_state_fixture()
            state["brick_roles_by_host"]["host-a"] = role
            self.assert_refused(state)
        state = posix_state_fixture()
        state["metadata_backend_by_host"]["host-c"] = "/different/object"
        self.assert_refused(state)

    def test_missing_or_ambiguous_state_is_refused(self):
        for state in ({}, None, [], {"kind": "file-posix-metadata-split-brain"}):
            with self.subTest(state=state):
                with self.assertRaises(RuntimeError):
                    self.build(state)


if __name__ == "__main__":
    unittest.main()
