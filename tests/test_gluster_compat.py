# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Unqualified Gluster commands must stop before repair dispatch."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool.execution_journal import ExecutionJournalError
from gluster_heal_tool.executor import execute_apply_results
from gluster_heal_tool.gluster_compat import feature_qualified, pre10_warning, require_execution_features
from gluster_heal_tool.health import render_volume_health_summary
from gluster_heal_tool.models import ApplyStep
from tests.test_execution_dependencies import action


class GlusterCompatibilityTests(unittest.TestCase):
    def test_pre10_version_warning_is_visible_without_blocking_health(self) -> None:
        self.assertIn("no compatibility claim", pre10_warning("9.6"))
        self.assertIn("no compatibility claim", pre10_warning("3.12.15"))
        for version in ("10.0", "11.1", "12.0", "unknown"):
            self.assertEqual("", pre10_warning(version))
        warning = pre10_warning("9.6")
        summary = render_volume_health_summary({
            "summary": {"ready": True, "warnings": [warning]},
            "gluster_version_warning": warning,
        })
        self.assertIn("Health check: ready", summary)
        self.assertIn(warning, summary)

    def test_only_live_qualified_release_and_feature_pair_is_available(self) -> None:
        self.assertTrue(feature_qualified("11.1", "pending_index_heal"))
        for version, feature in (("11.10", "pending_index_heal"),
                                 ("12.0", "pending_index_heal"),
                                 ("11.1", "full_namespace_heal"),
                                 ("11.1", "native_split_brain_resolution")):
            with self.subTest(version=version, feature=feature):
                self.assertFalse(feature_qualified(version, feature))

    def test_non_native_actions_do_not_need_a_version_probe(self) -> None:
        with patch("gluster_heal_tool.executor.get_gluster_version") as version:
            require_execution_features([action("parent")], version)
        version.assert_not_called()

    def test_native_resolver_refuses_before_journal_or_command(self) -> None:
        item = action("parent")
        item.steps = [ApplyStep(
            step_id="resolve", step_type="resolve_split_brain_gluster_cli",
            command_preview=["sudo", "-n", "gluster", "volume", "heal", "testvol",
                             "split-brain", "latest-mtime", "/fixture/item"],
        )]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            with patch("gluster_heal_tool.executor.get_gluster_version", return_value="11.1"), patch(
                "gluster_heal_tool.executor._run_command"
            ) as command:
                with self.assertRaisesRegex(ExecutionJournalError, "unqualified"):
                    execute_apply_results([item], run_dir=root)
            command.assert_not_called()
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
