# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for the read-only Gluster log helper."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


SCRIPT = Path(__file__).resolve().parents[1] / "gluster-log-ops.sh"


class LogOpsTests(TestCase):
    def _run_script(self, *args: str, log_root: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GLUSTER_REPAIR_LOG_ROOT"] = str(log_root)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_tail_reads_matching_host_logs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_root = Path(tmpdir)
            brick_root = log_root / "bricks"
            brick_root.mkdir()
            (log_root / "glusterd.log").write_text("glusterd line 1\n", encoding="utf-8")
            (log_root / "glustershd.log").write_text("shd line 1\nshd line 2\n", encoding="utf-8")
            (log_root / "glfsheal-gtest.log").write_text("heal line 1\nheal line 2\n", encoding="utf-8")
            (log_root / "gtest.log").write_text("volume line 1\nvolume line 2\n", encoding="utf-8")
            (brick_root / "gluster-homeb-gtest.log").write_text("brick line 1\nbrick line 2\n", encoding="utf-8")

            result = self._run_script("tail", "--volume", "gtest", "--lines", "1", log_root=log_root)

        self.assertEqual(0, result.returncode)
        self.assertIn(f"==> {log_root / 'glusterd.log'} <==", result.stdout)
        self.assertIn(f"==> {log_root / 'glustershd.log'} <==", result.stdout)
        self.assertIn(f"==> {log_root / 'glfsheal-gtest.log'} <==", result.stdout)
        self.assertIn(f"==> {log_root / 'gtest.log'} <==", result.stdout)
        self.assertIn(f"==> {log_root / 'bricks' / 'gluster-homeb-gtest.log'} <==", result.stdout)
        self.assertIn("shd line 2", result.stdout)
        self.assertIn("brick line 2", result.stdout)

    def test_grep_filters_to_matching_log_lines(self) -> None:
        with TemporaryDirectory() as tmpdir:
            log_root = Path(tmpdir)
            brick_root = log_root / "bricks"
            brick_root.mkdir()
            needle = "repair-canary-debugtrace"
            (log_root / "glusterd.log").write_text("glusterd quiet line\n", encoding="utf-8")
            (log_root / "glustershd.log").write_text(f"before\n{needle} after\n", encoding="utf-8")
            (log_root / "glfsheal-gtest.log").write_text(f"heal before\n{needle} heal\n", encoding="utf-8")
            (log_root / "gtest.log").write_text("volume quiet line\n", encoding="utf-8")
            (brick_root / "gluster-homeb-gtest.log").write_text(f"brick before\n{needle} brick\n", encoding="utf-8")

            result = self._run_script(
                "grep",
                "--volume",
                "gtest",
                "--pattern",
                needle,
                "--context",
                "1",
                log_root=log_root,
            )

        self.assertEqual(0, result.returncode)
        self.assertIn(f"==> {log_root / 'glustershd.log'} <==", result.stdout)
        self.assertIn(f"==> {log_root / 'glfsheal-gtest.log'} <==", result.stdout)
        self.assertIn(f"==> {log_root / 'bricks' / 'gluster-homeb-gtest.log'} <==", result.stdout)
        self.assertIn(needle, result.stdout)
        self.assertNotIn("glusterd quiet line", result.stdout)
        self.assertNotIn("volume quiet line", result.stdout)


if __name__ == "__main__":
    import unittest

    unittest.main()
