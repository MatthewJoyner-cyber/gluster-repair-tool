# SPDX-License-Identifier: GPL-2.0-only
"""Gluster heal rows must name the exact volume-relative canary object."""
import unittest

from gluster_heal_tool.canary_file import _heal_info_contains_path


GFID = "12345678-1234-1234-1234-123456789abc"
MOUNT_ROOT = "/mnt/example/repair-canary/example"
MOUNT_FILE = MOUNT_ROOT + "/fixture/alpha/payload.txt"


def heal_text(row: str, *, host: str = "host-a") -> str:
    return f"Brick {host}:/brick/{host}\n{row}\nStatus: Connected\nNumber of entries: 1\n"


class CanaryHealMatchTests(unittest.TestCase):
    def matches(self, text: str, *, mount_root: str = MOUNT_ROOT,
                mount_file: str = MOUNT_FILE, gfid: str = "") -> bool:
        return _heal_info_contains_path(
            text, mount_file, mount_root=mount_root, gfid=gfid,
        )

    def test_exact_volume_relative_path_and_split_brain_suffix(self):
        self.assertTrue(self.matches(heal_text("/fixture/alpha/payload.txt")))
        self.assertTrue(self.matches(heal_text("/fixture/alpha/payload.txt - Is in split-brain")))

    def test_same_basename_and_prefix_collisions_are_not_evidence(self):
        for row in ("/payload.txt", "/other/alpha/payload.txt", "/fixture/beta/payload.txt",
                    "/fixture/alpha/payload.txt.extra", "/fixture/alpha/payload.txt/child",
                    "/fixture/alpha/payload.tx"):
            with self.subTest(row=row):
                self.assertFalse(self.matches(heal_text(row)))

    def test_headers_and_unparsed_lines_are_not_evidence(self):
        path = "/fixture/alpha/payload.txt"
        self.assertFalse(self.matches("some notice mentioning " + path))
        self.assertFalse(self.matches("Brick host-a:/brick/" + path + "\nStatus: Connected"))
        self.assertFalse(self.matches("Brick host-a:/brick/host-a\nStatus: Connected\nNumber of entries: 0"))

    def test_mount_root_is_a_component_boundary(self):
        for root in ("/mnt/example/repair-canary/exam", "/mnt/example/repair-canary/example-extra"):
            self.assertFalse(self.matches(heal_text("/fixture/alpha/payload.txt"), mount_root=root))
        self.assertFalse(self.matches(heal_text("/fixture/alpha/payload.txt"), mount_file=MOUNT_FILE + ".extra"))
        self.assertTrue(self.matches(heal_text("/fixture/alpha/payload.txt"), mount_root=MOUNT_ROOT + "/"))
        self.assertTrue(self.matches(heal_text("/fixture/alpha/payload.txt"), mount_file=MOUNT_FILE + "/"))

    def test_mount_absolute_row_is_not_volume_relative(self):
        self.assertFalse(self.matches(heal_text(MOUNT_FILE)))

    def test_canonical_gfid_row_requires_exact_recorded_identity(self):
        row = f"<gfid:{GFID}>"
        self.assertFalse(self.matches(heal_text(row)))
        self.assertTrue(self.matches(heal_text(row), gfid=GFID))
        self.assertTrue(self.matches(heal_text(row + " - Is in split-brain"), gfid=GFID))
        for candidate in (row.replace("789abc", "789abd"), row + ".extra", f"<gfid:{GFID.upper()}>",
                          GFID, "<gfid:not-a-gfid>"):
            with self.subTest(candidate=candidate):
                self.assertFalse(self.matches(heal_text(candidate), gfid=GFID))

    def test_invalid_identity_cannot_match(self):
        for mount_file in ("", "/mnt/example/repair-canary/example-neighbour/fixture/alpha/payload.txt",
                           MOUNT_ROOT + "/fixture/../payload.txt"):
            with self.subTest(mount_file=mount_file):
                self.assertFalse(self.matches(heal_text("/fixture/alpha/payload.txt"), mount_file=mount_file))


if __name__ == "__main__":
    unittest.main()
