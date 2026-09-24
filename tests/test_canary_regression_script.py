# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the release canary regression proof-route contract."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "gluster-canary-regression.sh"


class CanaryRegressionScriptTests(unittest.TestCase):
    def test_dry_run_declares_current_cases_as_cleanup_only(self) -> None:
        completed = subprocess.run(
            ["bash", str(RUNNER), "--dry-run", "--prefix", "proof-contract"],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn("dry_run=1", completed.stdout)
        self.assertEqual(5, completed.stdout.count("proof label: cleanup-only"))
        self.assertEqual(5, completed.stdout.count("input source: canary_state"))
        self.assertEqual(5, completed.stdout.count("dry-run: no canary commands launched"))

    def test_route_checker_accepts_matching_operator_provenance(self) -> None:
        completed = subprocess.run(
            [
                "bash",
                str(RUNNER),
                "--check-proof-route",
                "operator-seeded-live:gfid",
                "operator_gfid",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn(
            "proof route accepted: label=operator-seeded-live:gfid input_source=operator_gfid",
            completed.stdout,
        )

    def test_route_checker_rejects_repair_proof_from_canary_state(self) -> None:
        completed = subprocess.run(
            ["bash", str(RUNNER), "--check-proof-route", "heal-driven", "canary_state"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn(
            "repair proof heal-driven cannot use input_source=canary_state",
            completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
