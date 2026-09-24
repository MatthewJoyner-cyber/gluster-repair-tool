# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for shared artifact write helpers."""
from __future__ import annotations

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gluster_heal_tool.shared_io import write_json_shared, write_text_shared
from gluster_heal_tool.status import update_status


class SharedIoTests(unittest.TestCase):
    def test_write_text_shared_restores_sudo_ownership(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "status.txt"
            with patch.dict(os.environ, {"SUDO_UID": "123", "SUDO_GID": "456"}, clear=False), patch(
                "gluster_heal_tool.shared_io._shared_group_gid", return_value=None
            ), patch("gluster_heal_tool.shared_io.os.chown") as mocked_chown:
                write_text_shared(target, "hello\n")

            self.assertEqual("hello\n", target.read_text(encoding="utf-8"))
            mocked_chown.assert_called_once()
            self.assertEqual((target, 123, 456), mocked_chown.call_args.args)

    def test_write_json_shared_restores_sudo_ownership(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "status.json"
            payload = {"hello": "world"}
            with patch.dict(os.environ, {"SUDO_UID": "321", "SUDO_GID": "654"}, clear=False), patch(
                "gluster_heal_tool.shared_io._shared_group_gid", return_value=None
            ), patch("gluster_heal_tool.shared_io.os.chown") as mocked_chown:
                write_json_shared(target, payload)

            self.assertEqual(payload, json.loads(target.read_text(encoding="utf-8")))
            mocked_chown.assert_called_once()
            self.assertEqual((target, 321, 654), mocked_chown.call_args.args)

    def test_write_text_shared_moves_to_service_group_when_available(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "status.txt"
            with patch.dict(os.environ, {"SUDO_UID": "123", "SUDO_GID": "456"}, clear=False), patch(
                "gluster_heal_tool.shared_io._shared_group_gid", return_value=789
            ), patch("gluster_heal_tool.shared_io.os.chown") as mocked_chown:
                write_text_shared(target, "hello\n")

            self.assertEqual("hello\n", target.read_text(encoding="utf-8"))
            self.assertEqual(
                [(target, 123, 456), (target, 123, 789)],
                [call.args for call in mocked_chown.call_args_list],
            )

    def test_write_text_shared_atomically_replaces_longer_content(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "status.json"
            write_text_shared(target, "x" * 4096)
            write_text_shared(target, "{}\n")

            self.assertEqual("{}\n", target.read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_concurrent_status_updates_remain_valid_and_preserve_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "gluster-repair-status.json"

            def write_field(index: int) -> None:
                update_status(target, **{f"field_{index}": index})

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write_field, range(32)))

            payload = json.loads(target.read_text(encoding="utf-8"))
            for index in range(32):
                self.assertEqual(index, payload[f"field_{index}"])

    def test_update_status_uses_shared_writer(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "gluster-repair-status.json"
            with patch("gluster_heal_tool.status.write_json_shared") as mocked_writer:
                update_status(target, phase="planning")

            mocked_writer.assert_called_once()
            written_path, written_payload = mocked_writer.call_args.args
            self.assertEqual(target, written_path)
            self.assertEqual("planning", written_payload["phase"])


if __name__ == "__main__":
    unittest.main()
