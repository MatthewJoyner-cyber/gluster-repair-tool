# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only

"""Guard metadata-bearing transfer previews against silent ownership loss."""

import unittest

from gluster_heal_tool import apply  # noqa: F401 - establish the existing import order
from gluster_heal_tool.apply_planning_utils import (
    _push_tree_contents_preview,
    _restore_preview,
    _stage_tree_contents_preview,
)


class TransferMetadataPreviewTests(unittest.TestCase):
    def test_directory_stage_preserves_numeric_ownership_and_user_metadata(self) -> None:
        command = _stage_tree_contents_preview("brick-a", "/brick/canonical", "/scratch/stage")
        self.assertEqual(
            ["sudo", "-n", "rsync", "-aAX", "--numeric-ids", "--filter=-x! user.*", "--delete"],
            command[:7],
        )
        self.assertEqual("gluster-repair@brick-a:/brick/canonical/", command[-2])
        self.assertEqual("/scratch/stage/", command[-1])

    def test_directory_push_preserves_numeric_ownership_and_user_metadata(self) -> None:
        command = _push_tree_contents_preview("brick-b", "/scratch/stage", "/brick/target")
        self.assertEqual(
            ["sudo", "-n", "rsync", "-aAX", "--numeric-ids", "--filter=-x! user.*"],
            command[:6],
        )
        self.assertEqual("/scratch/stage/", command[-2])
        self.assertEqual("gluster-repair@brick-b:/brick/target/", command[-1])

    def test_local_reference_backup_uses_archive_copy(self) -> None:
        self.assertEqual(
            ["sudo", "-n", "cp", "-a", "/scratch/winner", "/backup/winner"],
            _restore_preview("/scratch/winner", "/backup/winner"),
        )


if __name__ == "__main__":
    unittest.main()
