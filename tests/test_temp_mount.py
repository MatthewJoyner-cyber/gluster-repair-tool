# SPDX-License-Identifier: GPL-2.0-only
"""Tests for temporary mount inspection helpers."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from gluster_heal_tool.temp_mount import _mount_source
from gluster_heal_tool.temp_mount import _validate_mountpoint
from gluster_heal_tool.temp_mount import inspect_mount_at_path
from gluster_heal_tool.temp_mount import inspect_mount_at_path_lexically
from gluster_heal_tool.temp_mount import mount_temp_volume
from gluster_heal_tool.temp_mount import prepare_temp_mountpoint


class TempMountInspectionTests(unittest.TestCase):

    def test_mount_source_uses_gluster_volume_syntax(self) -> None:
        host, source = _mount_source("gtest3", source_host="node-b")

        self.assertEqual("node-b", host)
        self.assertEqual("node-b:gtest3", source)
    def test_inspect_mount_at_path_walks_up_to_parent_mount(self) -> None:
        with patch("gluster_heal_tool.temp_mount._inspect_mountpoint") as inspect_mock:
            inspect_mock.side_effect = [
                {},
                {},
                {"source": "node-b:gtest3", "fstype": "fuse.glusterfs", "target": "/mnt/gluster", "options": "rw,relatime"},
            ]

            result = inspect_mount_at_path("/mnt/gluster/deep/path/payload.txt")

        self.assertEqual("node-b:gtest3", result["source"])
        self.assertEqual(3, inspect_mock.call_count)

    def test_inspect_mount_at_path_lexically_uses_longest_matching_mount(self) -> None:
        mountinfo = (
            "28 21 0:25 / / rw,relatime - ext4 /dev/sda1 rw\n"
            "41 28 0:42 / /mnt/gluster rw,relatime - fuse.glusterfs localhost:gtest3 rw\n"
            "42 41 0:43 / /mnt/gluster/sub rw,relatime - fuse.glusterfs localhost:gtest3 rw\n"
        )
        with patch("builtins.open", mock_open(read_data=mountinfo)):
            result = inspect_mount_at_path_lexically("/mnt/gluster/sub/repair-canary/payload.txt")

        self.assertEqual("localhost:gtest3", result["source"])
        self.assertEqual("fuse.glusterfs", result["fstype"])
        self.assertEqual("/mnt/gluster/sub", result["target"])

    def test_mount_temp_volume_requests_acl_support_when_requested(self) -> None:
        mountpoint = Path("/tmp/gluster-repair/gtest3")
        with (
            patch("gluster_heal_tool.temp_mount.os.path.ismount", return_value=False),
            patch("gluster_heal_tool.temp_mount._sudo_prefix", return_value=["sudo", "-n"]),
            patch("gluster_heal_tool.temp_mount._mount_source", return_value=("node-b", "node-b:gtest3")),
            patch("gluster_heal_tool.temp_mount.prepare_temp_mountpoint"),
            patch("gluster_heal_tool.temp_mount.os.chmod"),
            patch(
                "gluster_heal_tool.temp_mount._validate_mountpoint",
                return_value=(
                    {
                        "source": "node-b:gtest3",
                        "fstype": "fuse.glusterfs",
                        "target": str(mountpoint),
                        "options": "rw,acl",
                    },
                    ":gtest3",
                ),
            ),
            patch("gluster_heal_tool.temp_mount.subprocess.run") as run_mock,
        ):
            run_mock.return_value.returncode = 0
            run_mock.return_value.stderr = ""
            run_mock.return_value.stdout = ""

            result = mount_temp_volume("gtest3", mount_root="/tmp/gluster-repair", acl_mount=True)

        self.assertTrue(result.acl_mount)
        self.assertEqual(
            ["sudo", "-n", "mount", "-t", "glusterfs", "-o", "acl", "node-b:gtest3", str(mountpoint)],
            run_mock.call_args.args[0],
        )

    def test_prepare_temp_mountpoint_repairs_root_owned_detached_mountpoint(self) -> None:
        mountpoint = Path("/tmp/gluster-repair/gtest3")
        with (
            patch.object(Path, "mkdir"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
            patch("gluster_heal_tool.temp_mount.os.chmod", side_effect=PermissionError),
            patch("gluster_heal_tool.temp_mount._sudo_prefix", return_value=["sudo", "-n"]),
            patch("gluster_heal_tool.temp_mount.subprocess.run") as run_mock,
        ):
            run_mock.return_value.returncode = 0
            run_mock.return_value.stderr = ""

            prepare_temp_mountpoint(mountpoint)

        self.assertEqual(
            ["sudo", "-n", "chmod", "000", str(mountpoint)],
            run_mock.call_args.args[0],
        )

    def test_validate_mountpoint_rejects_acl_mount_without_acl_option(self) -> None:
        mountpoint = Path("/tmp/gluster-repair/gtest3")
        with patch(
            "gluster_heal_tool.temp_mount._inspect_mountpoint",
            return_value={
                "source": "node-b:gtest3",
                "fstype": "fuse.glusterfs",
                "target": str(mountpoint),
                "options": "rw,relatime",
            },
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _validate_mountpoint("gtest3", mountpoint, aux_gfid_mount=False, acl_mount=True)

        self.assertIn("does not expose ACL support", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
