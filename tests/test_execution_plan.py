# SPDX-License-Identifier: GPL-2.0-only
"""Tests for execution-plan wave assignment."""
from __future__ import annotations

import unittest

from gluster_heal_tool.execution_plan import actions_conflict, annotate_execution_waves
from gluster_heal_tool.models import PlanAction
from gluster_heal_tool.planner_payload import load_plan_actions


def _action(
    action_id: str,
    logical_path: str,
    action_type: str,
    *,
    depends_on: list[str] | None = None,
) -> PlanAction:
    return PlanAction(
        action_id=action_id,
        logical_path=logical_path,
        action_type=action_type,
        object_type="file",
        depth=1,
        depends_on=list(depends_on or []),
    )


class ExecutionPlanTests(unittest.TestCase):
    def test_annotate_execution_waves_groups_independent_cleanup_actions(self) -> None:
        left = _action("cleanup:left", "/gluster/data/a", "cleanup_orphaned_symlink")
        right = _action("cleanup:right", "/gluster/data/b", "cleanup_stale_glusterfs_index")

        annotate_execution_waves([left, right])

        self.assertTrue(left.parallel_safe)
        self.assertTrue(right.parallel_safe)
        self.assertEqual(0, left.execution_wave)
        self.assertEqual(0, right.execution_wave)
        self.assertIn("logical:/gluster/data/a", left.execution_resources)
        self.assertIn("logical:/gluster/data/b", right.execution_resources)

    def test_actions_conflict_detects_nested_backend_paths(self) -> None:
        left = _action("cleanup:left", "/gluster/data/a", "cleanup_orphaned_symlink")
        right = _action("cleanup:right", "/gluster/data/b", "cleanup_stale_glusterfs_index")
        left.execution_resources = ["backend:host-a:/bricks/data/tree"]
        right.execution_resources = ["backend:host-a:/bricks/data/tree/child"]

        self.assertTrue(actions_conflict(left, right))

    def test_annotate_execution_waves_serializes_dependency_chain(self) -> None:
        root = _action("cleanup:root", "/gluster/data/root", "cleanup_orphaned_symlink")
        child = _action(
            "cleanup:child",
            "/gluster/data/child",
            "cleanup_stale_glusterfs_index",
            depends_on=["cleanup:root"],
        )

        annotate_execution_waves([root, child])

        self.assertEqual(0, root.execution_wave)
        self.assertEqual(1, child.execution_wave)
        self.assertTrue(child.parallel_safe)

    def test_annotate_execution_waves_serializes_non_allowlisted_actions(self) -> None:
        repair = _action("repair:file", "/gluster/data/file", "repair_file")
        cleanup = _action("cleanup:tail", "/gluster/data/tail", "cleanup_orphaned_symlink")

        annotate_execution_waves([repair, cleanup])

        self.assertFalse(repair.parallel_safe)
        self.assertIn("parallel allowlist", repair.execution_serialization_reason)
        self.assertEqual(1, cleanup.execution_wave)

    def test_load_plan_actions_round_trips_execution_metadata(self) -> None:
        payload = {
            "actions": [
                {
                    "action_id": "cleanup:left",
                    "logical_path": "/gluster/data/a",
                    "action_type": "cleanup_orphaned_symlink",
                    "object_type": "file",
                    "depth": 1,
                    "depends_on": ["cleanup:root"],
                    "execution_wave": 3,
                    "parallel_safe": True,
                    "execution_resources": ["logical:/gluster/data/a", "backend:host-a:/bricks/data/tree"],
                    "execution_serialization_reason": "shared backend",
                }
            ]
        }

        actions = load_plan_actions(payload)

        self.assertEqual(1, len(actions))
        action = actions[0]
        self.assertEqual(3, action.execution_wave)
        self.assertTrue(action.parallel_safe)
        self.assertEqual(
            ["logical:/gluster/data/a", "backend:host-a:/bricks/data/tree"],
            action.execution_resources,
        )
        self.assertEqual("shared backend", action.execution_serialization_reason)
        self.assertEqual(["cleanup:root"], action.depends_on)
