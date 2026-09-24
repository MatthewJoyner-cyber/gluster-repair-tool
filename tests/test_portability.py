# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Fresh, unprivileged controller startup outside the source checkout."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
PYTHON_ENTRY_POINTS = ("gluster-manager.py", "gluster-heal-tool.py", "gluster-worker.py")
PROBE = """
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import gluster_heal_tool
from gluster_heal_tool.controller_paths import (
    default_backup_archive_dir, default_status_file_path, default_work_root,
)
from gluster_heal_tool.status import load_status, update_status
status_file = default_status_file_path()
before = load_status(status_file)
update_status(status_file, phase='portability-probe')
print(json.dumps({
    'package': str(Path(gluster_heal_tool.__file__).resolve()),
    'work': str(default_work_root()),
    'backup': str(default_backup_archive_dir()),
    'status': str(status_file),
    'before': before,
    'after': load_status(status_file)['phase'],
}))
"""


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "installed"
        self.install.mkdir()
        shutil.copytree(
            SOURCE / "gluster_heal_tool", self.install / "gluster_heal_tool",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in PYTHON_ENTRY_POINTS:
            shutil.copy2(SOURCE / name, self.install / name)
        self.home = self.root / "fresh-home"
        self.home.mkdir()
        self.state = self.root / "fresh-state"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GLUSTER_", "XDG_", "PYTHON", "SUDO_"))
        }
        self.env.update(HOME=str(self.home), XDG_STATE_HOME=str(self.state),
                        PYTHONDONTWRITEBYTECODE="1")

    def run_probe(self, env=None):
        return subprocess.run(
            [sys.executable, "-EsB", "-c", PROBE, str(self.install)],
            cwd="/", env=env or self.env, capture_output=True, text=True,
        )

    def test_fresh_nonroot_install_uses_only_new_user_state(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("requires a non-root test runner")
        for name in PYTHON_ENTRY_POINTS:
            result = subprocess.run(
                [sys.executable, "-EsB", str(self.install / name), "--help"],
                cwd="/", env=self.env, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, f"{name}: {result.stderr}")
        result = self.run_probe()
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(str(self.install / "gluster_heal_tool/__init__.py"), payload["package"])
        self.assertEqual(str(self.state / "gluster-repair/work"), payload["work"])
        self.assertEqual(str(self.state / "gluster-repair/backups"), payload["backup"])
        self.assertEqual({}, payload["before"])
        self.assertEqual("portability-probe", payload["after"])
        self.assertFalse((self.home / ".local").exists())

    def test_explicit_work_and_backup_overrides_from_fresh_process(self) -> None:
        work = self.root / "chosen-work"
        backup = self.root / "chosen-backups"
        env = dict(self.env, GLUSTER_REPAIR_WORK_ROOT=str(work),
                   GLUSTER_REPAIR_BACKUP_DIR=str(backup))
        result = self.run_probe(env)
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(str(work), payload["work"])
        self.assertEqual(str(backup), payload["backup"])
        self.assertEqual(str(work / "gluster-repair-status.json"), payload["status"])
        self.assertFalse(self.state.exists())

    def test_unusable_work_root_fails_without_falling_back(self) -> None:
        blocker = self.root / "not-a-directory"
        blocker.write_text("blocked", encoding="utf-8")
        env = dict(self.env, GLUSTER_REPAIR_WORK_ROOT=str(blocker / "work"))
        result = self.run_probe(env)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("NotADirectoryError", result.stderr)
        self.assertEqual("blocked", blocker.read_text(encoding="utf-8"))
        self.assertFalse(self.state.exists())

    def test_permission_denied_work_root_fails_without_falling_back(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("requires a non-root test runner")
        blocked = self.root / "read-only-work"
        blocked.mkdir(mode=0o500)
        self.addCleanup(blocked.chmod, 0o700)
        env = dict(self.env, GLUSTER_REPAIR_WORK_ROOT=str(blocked))
        result = self.run_probe(env)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("PermissionError", result.stderr)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
