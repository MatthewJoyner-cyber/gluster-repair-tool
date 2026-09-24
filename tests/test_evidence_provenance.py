# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import unittest

from gluster_heal_tool.evidence_provenance import CANARY_STATE_INPUT_SOURCE
from gluster_heal_tool.evidence_provenance import HARNESS_ONLY_PROOF_SCOPE
from gluster_heal_tool.evidence_provenance import canary_state_plan_provenance
from gluster_heal_tool.evidence_provenance import is_operator_discoverable_input_source


class EvidenceProvenanceTests(unittest.TestCase):
    def test_canary_state_provenance_is_explicit_and_harness_only(self) -> None:
        self.assertEqual(
            {
                "input_source": CANARY_STATE_INPUT_SOURCE,
                "proof_scope": HARNESS_ONLY_PROOF_SCOPE,
                "canary": {
                    "kind": "file-child-gap",
                    "volume": "testvol",
                    "scenario": "scenario-a",
                },
            },
            canary_state_plan_provenance(
                kind=" file-child-gap ",
                volume=" testvol ",
                scenario=" scenario-a ",
            ),
        )

    def test_canary_state_provenance_requires_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires kind, volume, and scenario"):
            canary_state_plan_provenance(kind="", volume="testvol", scenario="scenario-a")

    def test_operator_discoverability_excludes_canary_state(self) -> None:
        self.assertTrue(is_operator_discoverable_input_source("heal_info"))
        self.assertTrue(is_operator_discoverable_input_source("operator_gfid_child"))
        self.assertFalse(is_operator_discoverable_input_source(CANARY_STATE_INPUT_SOURCE))


if __name__ == "__main__":
    unittest.main()
