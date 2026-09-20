# SPDX-License-Identifier: GPL-2.0-only
"""Tests for monotonic write-history status fields."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gluster_heal_tool.status import merge_write_history, update_status


class WriteHistoryTests(unittest.TestCase):
    def test_status_refresh_cannot_erase_completed_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            update_status(status_path, phase="execution", write_occurred=True)
            status = update_status(status_path, phase="evidence-refresh", write_occurred=False)

        self.assertTrue(status["write_occurred"])
        self.assertFalse(status["write_outcome_unknown"])
        self.assertEqual("completed", status["execution_write_state"])

    def test_partial_report_and_interruption_remain_unknown(self) -> None:
        history = merge_write_history(
            {"execution_summary": {"executed_actions": 1, "completed_actions": 0, "failed_actions": 1}},
            {"interrupted": True},
        )

        self.assertTrue(history["write_occurred"])
        self.assertTrue(history["write_outcome_unknown"])
        self.assertEqual("unknown", history["execution_write_state"])

    def test_all_skipped_batch_records_no_write(self) -> None:
        history = merge_write_history(
            {"execution_summary": {"executed_actions": 0, "completed_actions": 0, "failed_actions": 0}}
        )

        self.assertFalse(history["write_occurred"])
        self.assertFalse(history["write_outcome_unknown"])


if __name__ == "__main__":
    unittest.main()
