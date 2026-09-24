# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for remote command construction."""
from __future__ import annotations

from unittest import TestCase

from gluster_heal_tool.remote_ops import SSH_TRANSPORT, rsync_brick_pull_command, ssh_remote_command


class RemoteOpsTests(TestCase):
    def test_ssh_remote_command_disables_x11_forwarding(self) -> None:
        command = ssh_remote_command("brick1", ["worker-op"], ssh_user="repair", use_sudo=False)

        self.assertIn("-x", command)
        self.assertLess(command.index("-x"), command.index("repair@brick1"))

    def test_brick_rsync_keeps_rsync_path_as_one_remote_argument(self) -> None:
        command = rsync_brick_pull_command(
            "brick-b",
            "brick-a",
            "/brick-a/source file",
            "/brick-b/target file",
        )

        remote_command = command[-1]
        self.assertIn("rsync-pull", remote_command)
        self.assertIn("'/brick-a/source file'", remote_command)

    def test_rsync_transport_disables_x11_forwarding(self) -> None:
        self.assertIn("-x", SSH_TRANSPORT.split())


if __name__ == "__main__":
    import unittest

    unittest.main()
