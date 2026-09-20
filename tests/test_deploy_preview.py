# SPDX-License-Identifier: GPL-2.0-only
"""Deploy preview and preflight must not create local or remote state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "gluster-deploy-heal-tool.sh"


class DeployPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "calls.jsonl"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GLUSTER_", "XDG_", "PYTHON", "SUDO_"))}
        self.env.update(HOME=str(self.home), USER="fixture", PATH=f"{self.bin}:{os.environ['PATH']}",
                        FIXTURE_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        self.stub("getent", "print('fixture:x:1000:1000::' + str(root / 'home') + ':/bin/sh')")
        self.stub("gluster", "print('Volume Name: example\\nType: Replicate\\nBrick1: node-a:/brick/a\\nBrick2: node-b:/brick/b')")
        self.stub("python3", "raise SystemExit('health-check must not run in preview')")
        self.stub("ssh", """if not (root / 'home/.ssh/id_ed25519').exists(): raise SystemExit(1)
if any('stat -c' in arg for arg in args): print('directory')""")
        self.stub("ssh-keygen", "raise SystemExit('unexpected key generation')")
        self.stub("ssh-copy-id", "raise SystemExit('unexpected key copy')")
        self.stub("rsync", "raise SystemExit('unexpected copy')")

    def stub(self, name: str, body: str) -> None:
        target = self.bin / name
        target.write_text(
            f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
            "root=Path(os.environ['FIXTURE_ROOT']); args=sys.argv[1:]\n"
            f"with (root/'calls.jsonl').open('a') as log: log.write(json.dumps([{name!r},args])+'\\n')\n"
            + body + "\n", encoding="utf-8",
        )
        target.chmod(0o755)

    def calls(self) -> list[tuple[str, list[str]]]:
        if not self.trace.exists():
            return []
        return [tuple(json.loads(line)) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    def run_deploy(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DEPLOY), "-v", "example", "-r", str(ROOT), *options],
            env=self.env, cwd=self.root, capture_output=True, text=True,
        )

    def keys(self) -> None:
        folder = self.home / ".ssh"
        folder.mkdir()
        (folder / "id_ed25519").write_text("fixture-private", encoding="utf-8")
        (folder / "id_ed25519.pub").write_text("fixture-public", encoding="utf-8")
        (folder / "known_hosts").write_text("node-a fixture\nnode-b fixture\n", encoding="utf-8")

    def test_dry_run_with_missing_keys_is_local_no_write_plan(self) -> None:
        result = self.run_deploy("--dry-run", "--setup-keys")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertIn("DRY-RUN", result.stdout)
        self.assertFalse({name for name, _ in self.calls()} &
                         {"python3", "ssh", "ssh-keygen", "ssh-copy-id", "rsync"})

    def test_preflight_missing_keys_refuses_without_generation(self) -> None:
        result = self.run_deploy("--preflight")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse({name for name, _ in self.calls()} &
                         {"python3", "ssh-keygen", "ssh-copy-id", "rsync"})

    def test_preflight_uses_read_only_remote_probes_and_verified_hosts(self) -> None:
        self.keys()
        before = {str(path.relative_to(self.home)): path.read_bytes()
                  for path in self.home.rglob("*") if path.is_file()}
        result = self.run_deploy("--preflight")
        self.assertEqual(0, result.returncode, result.stderr)
        after = {str(path.relative_to(self.home)): path.read_bytes()
                 for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        ssh_calls = [args for name, args in self.calls() if name == "ssh"]
        self.assertTrue(ssh_calls)
        for args in ssh_calls:
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("UpdateHostKeys=no", args)
            self.assertNotIn("accept-new", " ".join(args))
            self.assertNotIn("mkdir", " ".join(args))
            self.assertNotIn("rm -rf", " ".join(args))
        self.assertFalse({name for name, _ in self.calls()} &
                         {"python3", "ssh-keygen", "ssh-copy-id", "rsync"})

    def test_preflight_rejects_setup_keys_without_writes(self) -> None:
        result = self.run_deploy("--preflight", "--setup-keys")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse({name for name, _ in self.calls()} &
                         {"gluster", "ssh", "ssh-keygen", "ssh-copy-id", "rsync", "python3"})

    def test_custom_layouts_fail_before_discovery_or_remote_access(self) -> None:
        for options in (("-d", "/custom/tools"), ("-s", "custom-service")):
            with self.subTest(options=options):
                self.trace.unlink(missing_ok=True)
                result = self.run_deploy(*options, "--preflight")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("unsupported", result.stderr.lower())
                self.assertFalse({name for name, _ in self.calls()} &
                                 {"gluster", "ssh", "ssh-keygen", "ssh-copy-id", "rsync", "python3"})

    def test_execute_without_setup_keys_never_generates_them(self) -> None:
        result = self.run_deploy()
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse({name for name, _ in self.calls()} &
                         {"ssh-keygen", "ssh-copy-id", "rsync"})

    def test_opted_in_setup_refuses_partial_keypair(self) -> None:
        folder = self.home / ".ssh"
        folder.mkdir()
        private = folder / "id_ed25519"
        private.write_text("fixture-private", encoding="utf-8")
        result = self.run_deploy("--setup-keys")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("partial local SSH keypair", result.stderr)
        self.assertEqual("fixture-private", private.read_text(encoding="utf-8"))
        self.assertFalse({name for name, _ in self.calls()} &
                         {"ssh-keygen", "ssh-copy-id", "rsync"})

    def test_dry_run_refuses_partial_keypair_before_discovery(self) -> None:
        folder = self.home / ".ssh"
        folder.mkdir()
        (folder / "id_ed25519.pub").write_text("fixture-public", encoding="utf-8")
        result = self.run_deploy("--dry-run", "--setup-keys")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("partial local SSH keypair", result.stderr)
        self.assertFalse({name for name, _ in self.calls()} &
                         {"gluster", "ssh", "ssh-keygen", "ssh-copy-id", "rsync", "python3"})

    def test_execution_refreshes_health_only_after_copying(self) -> None:
        self.keys()
        self.stub("rsync", "pass")
        self.stub("python3", "pass")
        result = self.run_deploy()
        self.assertEqual(0, result.returncode, result.stderr)
        names = [name for name, _ in self.calls()]
        self.assertEqual(4, names.count("rsync"))
        self.assertEqual(1, names.count("python3"))
        self.assertGreater(names.index("python3"), max(index for index, name in enumerate(names) if name == "rsync"))


if __name__ == "__main__":
    unittest.main()
