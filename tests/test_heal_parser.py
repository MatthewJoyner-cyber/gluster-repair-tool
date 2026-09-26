# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Heal-info formats used for live completion must be explicit and complete."""
from __future__ import annotations

import unittest

from gluster_heal_tool.heal_parser import parse_heal_info_text


class HealParserTests(unittest.TestCase):
    def test_complete_connected_brick_is_accepted(self) -> None:
        entries = parse_heal_info_text(
            "Brick peer-a:/brick-a\n/fixture/item\nStatus: Connected\nNumber of entries: 1\n",
            require_connected=True,
        )
        self.assertEqual(["/fixture/item"], [entry.raw for entry in entries])

    def test_missing_or_unqualified_state_blocks_live_completion(self) -> None:
        for ending in ("Number of entries: 0\n",
                       "Status: Disconnected\nNumber of entries: 0\n",
                       "Status: Transport endpoint is not connected\nNumber of entries: -\n",
                       "Status: Connected\nNumber of entries: -\n",
                       "Status: Connected\nNumber of entries: 1\n",
                       "Status: Connected\nUnknown diagnostic\nNumber of entries: 0\n"):
            with self.subTest(ending=ending), self.assertRaises(RuntimeError):
                parse_heal_info_text("Brick peer-a:/brick-a\n" + ending,
                                     require_connected=True)

    def test_unknown_diagnostic_is_not_a_file_entry_in_preview(self) -> None:
        entries = parse_heal_info_text(
            "Brick peer-a:/brick-a\nUnknown diagnostic\n/fixture/item\n"
        )
        self.assertEqual(["/fixture/item"], [entry.raw for entry in entries])


if __name__ == "__main__":
    unittest.main()
