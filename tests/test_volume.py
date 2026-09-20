# SPDX-License-Identifier: GPL-2.0-only
"""Tests for volume helpers."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gluster_heal_tool.volume import parse_bricks
from gluster_heal_tool.volume import parse_brick_roles
from gluster_heal_tool.volume import parse_common_brick_path
from gluster_heal_tool.volume import parse_brick_paths
from gluster_heal_tool.volume import discover_brick_paths


class VolumeParseTests(unittest.TestCase):
    def test_parse_bricks_strips_arbiter_suffix_from_common_path(self) -> None:
        volume_info = """
Volume Name: gtest3a
Type: Replicate
Bricks:
Brick1: node-a:/gluster/gtest3a/gtest3a/brick
Brick2: node-b:/gluster/gtest3a/gtest3a/brick
Brick3: node-c:/gluster/gtest3a/gtest3a/brick (arbiter)
"""

        bricks = parse_bricks(volume_info)

        self.assertEqual(
            [
                ("node-a", "/gluster/gtest3a/gtest3a/brick"),
                ("node-b", "/gluster/gtest3a/gtest3a/brick"),
                ("node-c", "/gluster/gtest3a/gtest3a/brick"),
            ],
            bricks,
        )
        self.assertEqual("/gluster/gtest3a/gtest3a/brick", parse_common_brick_path(volume_info))

    def test_parse_brick_roles_marks_arbiter_brick(self) -> None:
        volume_info = """
Volume Name: gtest3a
Type: Replicate
Bricks:
Brick1: node-a:/gluster/gtest3a/gtest3a/brick
Brick2: node-b:/gluster/gtest3a/gtest3a/brick
Brick3: node-c:/gluster/gtest3a/gtest3a/brick (arbiter)
"""

        self.assertEqual(
            {
                "node-a": "data",
                "node-b": "data",
                "node-c": "arbiter",
            },
            parse_brick_roles(volume_info),
        )

    def test_parse_brick_paths_keeps_host_local_brick_roots(self) -> None:
        volume_info = """
Volume Name: gtest3a
Type: Replicate
Bricks:
Brick1: node-a:/gluster/gtest3a/brick
Brick2: node-b:/gluster/gtest3a/brick
Brick3: node-c:/gluster/gtest3a/arbiter/brick (arbiter)
"""

        self.assertEqual(
            {
                "node-a": "/gluster/gtest3a/brick",
                "node-b": "/gluster/gtest3a/brick",
                "node-c": "/gluster/gtest3a/arbiter/brick",
            },
            parse_brick_paths(volume_info),
        )

    def test_discover_brick_paths_prefers_live_topology_over_cache(self) -> None:
        live_info = """
Volume Name: gtest4
Type: Replicate
Bricks:
Brick1: node-a:/gluster/gtest4/brick
Brick2: node-b:/gluster/gtest4/brick
Brick3: node-d:/gluster/gtest4/brick
Brick4: node-a:/gluster/gtest4-secondary/brick
"""
        with (
            patch("gluster_heal_tool.volume.get_volume_info", return_value=live_info),
            patch(
                "gluster_heal_tool.volume._load_cached_brick_paths",
                return_value={"node-a": "/stale/brick"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "single brick path per host"):
                discover_brick_paths("gtest4")

    def test_discover_brick_paths_uses_cached_layout_cache_aliases(self) -> None:
        report = {
            "volume": "gtest3a",
            "bricks": [
                {
                    "host": "192.0.2.12",
                    "path": "/gluster/gtest3a/brick",
                    "aliases": ["node-b", "node-b.local", "192.0.2.12"],
                },
                {
                    "host": "node-a",
                    "path": "/gluster/gtest3a/brick",
                    "aliases": ["node-a", "node-a.local"],
                },
            ],
            "host_facts": [
                {
                    "host": "192.0.2.12",
                    "short_hostname": "node-b",
                    "fqdn": "node-b.local",
                    "ips": ["192.0.2.12"],
                    "aliases": ["192.0.2.12", "node-b", "node-b.local"],
                }
            ],
        }

        with TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "gtest3a-brick-layout.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                patch("gluster_heal_tool.volume.default_brick_layout_path", return_value=report_path),
                patch(
                    "gluster_heal_tool.volume.get_volume_info",
                    side_effect=RuntimeError("volume unavailable"),
                ),
            ):
                paths = discover_brick_paths("gtest3a")

        self.assertEqual("/gluster/gtest3a/brick", paths["node-b"])
        self.assertEqual("/gluster/gtest3a/brick", paths["node-b.local"])
        self.assertEqual("/gluster/gtest3a/brick", paths["192.0.2.12"])
