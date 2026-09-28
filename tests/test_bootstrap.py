# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Bootstrap transport boundaries and fresh installed package behavior."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "gluster-bootstrap-install.sh"
ENTRY_POINTS = ["gluster-manager.py", "gluster-heal-tool.py", "gluster-worker.py",
                "gluster-host-ops.sh", "gluster-log-ops.sh", "gluster-resolve-gfid-plus.sh"]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "calls.jsonl"
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(("GLUSTER_", "XDG_", "PYTHON", "SUDO_"))}
        self.env.update(HOME=str(self.home), USER="fixture-user", PATH=f"{self.bin}:{os.environ['PATH']}",
                        FIXTURE_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        self.stub("getent", "print('fixture-user:x:1000:1000::' + str(root / 'home') + ':/bin/sh')")
        self.stub("ssh-keygen", "raise SystemExit('unexpected key generation')")
        self.stub("ssh", """if 'mktemp' in ' '.join(args):
    print(root / 'stage')
elif 'bash' in args:
    (root / 'remote.sh').write_text(sys.stdin.read())
elif any('mkdir -p' in arg for arg in args):
    import shlex
    command = shlex.split(args[-1])
    for name in command[2:]: Path(name).mkdir(parents=True, exist_ok=True)
""")
        self.stub("scp", """import shutil
destination = Path(args[-1].split(':',1)[1])
sources = [Path(arg) for arg in args[:-1] if Path(arg).is_file() and arg not in {'-i'}]
# Exclude the login key passed as an option, preserving explicit source operands.
if '-i' in args: sources = [p for p in sources if str(p) != args[args.index('-i')+1]]
for source in sources: shutil.copy2(source, destination / source.name if destination.is_dir() else destination)
""")
        for name in ("operator", "operator.pub"):
            (self.home / name).write_text("fixture-key")
        self.arguments = ["bash", str(ROOT / "gluster-bootstrap-host.sh"), "-H", "host-a", "-l", "fixture-user",
                          "-i", str(self.home / "operator"), "-p", str(self.home / "operator.pub")]

    def stub(self, name, body):
        target = self.bin / name
        target.write_text(f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
                          "root=Path(os.environ['FIXTURE_ROOT']); args=sys.argv[1:]\n"
                          f"with (root/'calls.jsonl').open('a') as log: log.write(json.dumps([{name!r},args])+'\\n')\n" + body + "\n")
        target.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def run_bootstrap(self, *args):
        return subprocess.run([*self.arguments, *args], env=self.env, cwd=self.root, capture_output=True, text=True)

    def service_keys(self):
        folder = self.home / ".ssh" / "gluster-repair-service"
        folder.mkdir(parents=True)
        for name in ("gluster-repair-service", "gluster-repair-service.pub"):
            (folder / name).write_text("fixture-service-key")

    def test_preflight_does_not_generate_keys_or_stage_files(self):
        before = {str(p.relative_to(self.home)):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.run_bootstrap("--preflight")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, {str(p.relative_to(self.home)):p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse(any(name in {"scp", "ssh-keygen"} for name,args in self.calls()))
        ssh = [args for name,args in self.calls() if name == "ssh"]
        self.assertTrue(ssh)
        self.assertTrue(all("StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args for args in ssh))

    def test_preflight_ssh_or_sudo_failure_does_not_mutate_keys(self):
        self.stub("ssh", "raise SystemExit(1 if 'sudo -n true' in args else 0)")
        result = self.run_bootstrap("--preflight")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse(any(name in {"scp", "ssh-keygen"} for name,args in self.calls()))

    def test_missing_public_key_does_not_replace_private_key(self):
        (self.home / "operator.pub").unlink()
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("public key not found", result.stderr)
        self.assertEqual("fixture-key", (self.home / "operator").read_text())
        self.assertFalse(any(name in {"ssh", "scp", "ssh-keygen"} for name,args in self.calls()))

    def test_partial_service_keypair_fails_before_transport(self):
        self.service_keys()
        (self.home / ".ssh/gluster-repair-service/gluster-repair-service.pub").unlink()
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("partial service SSH keypair", result.stderr)
        self.assertFalse(any(name in {"ssh", "scp", "ssh-keygen"} for name,args in self.calls()))

    def test_unsupported_layouts_are_rejected_before_remote_access(self):
        for args in (("-d", "/custom/tools"), ("-s", "custom-service"), ("-m", "/custom/home")):
            with self.subTest(args=args):
                self.trace.unlink(missing_ok=True)
                result = self.run_bootstrap(*args)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("unsupported", result.stderr.lower())
                self.assertFalse(any(name in {"ssh", "scp", "ssh-keygen"} for name,argv in self.calls()))

    def test_transport_preserves_package_directory(self):
        self.service_keys()
        result = self.run_bootstrap()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.stage / "files/gluster_heal_tool/__init__.py").is_file())
        self.assertFalse((self.stage / "files/__init__.py").exists())
        self.assertTrue((self.stage / "files/gluster-bootstrap-install.sh").is_file())
        remote = (self.root / "remote.sh").read_text()
        self.assertIn("install_tool_tree", remote)
        self.assertIn("verify_tool_tree", remote)

    def test_fresh_install_entrypoints_and_repeat_upgrade(self):
        install = self.root / "installed tree"
        command = ["bash", "-c", 'set -e; source "$1"; install_tool_tree "$2" "$3"; verify_tool_tree "$3"',
                   "fixture", str(LIBRARY), str(ROOT), str(install)]
        for iteration in range(2):
            result = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((install / "gluster_heal_tool/__init__.py").is_file())
            for entry in ENTRY_POINTS:
                launcher = [sys.executable, "-EsB"] if entry.endswith(".py") else ["bash"]
                help_result = subprocess.run([*launcher, str(install / entry), "--help"], cwd=self.root,
                                             env=self.env, capture_output=True, text=True)
                self.assertEqual(0, help_result.returncode, f"{entry}: {help_result.stderr}")
            (install / "gluster_heal_tool/version.py").write_text("raise RuntimeError('stale version')\n")

    def test_missing_or_invalid_package_cannot_be_declared_installed(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                source = self.root / f"source-{missing}"
                source.mkdir()
                for entry in ENTRY_POINTS: shutil.copy2(ROOT / entry, source / entry)
                (source / "gluster_heal_tool").mkdir()
                if not missing: (source / "gluster_heal_tool/__init__.py").write_text("invalid python !\n")
                result = subprocess.run(["bash", "-c", 'set -e; source "$1"; install_tool_tree "$2" "$3"',
                                         "fixture", str(LIBRARY), str(source), str(self.root / "rejected")],
                                        cwd=self.root, env=self.env, capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode)
                self.assertTrue("incomplete tool tree" in result.stderr or "SyntaxError" in result.stderr, result.stderr)
                self.assertFalse((self.root / "rejected").exists())

    def volume_fixture(self):
        folder = self.home / ".ssh"
        folder.mkdir(exist_ok=True)
        (folder / "known_hosts").write_text("host-a ssh-ed25519 fixture-a\nhost-b ssh-ed25519 fixture-b\n")
        self.stub("gluster", "print('Type: Replicate\\nBrick1: host-a:/fixture/a\\nBrick2: host-b:/fixture/b')")
        self.stub("ssh-keygen", """if args[0] != '-F': raise SystemExit('unexpected key generation')
print(args[1] + ' ssh-ed25519 fixture-key')""")
        self.stub("mktemp", "raise SystemExit('preflight must not create temporary files')")

    def run_volume_preflight(self, *extra):
        return subprocess.run(["bash", str(ROOT / "gluster-bootstrap-volume.sh"), "-v", "fixture",
                               "-l", "fixture-user", "-i", str(self.home / "operator"),
                               "-p", str(self.home / "operator.pub"), "--preflight", *extra],
                              env=self.env, cwd=self.root, capture_output=True, text=True)

    def test_volume_preflight_only_reads_existing_host_keys(self):
        self.volume_fixture()
        before = {str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.run_volume_preflight()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        calls = self.calls()
        self.assertFalse(any(name in {"mktemp", "scp"} for name,args in calls))
        self.assertTrue(all(args[0] == "-F" for name,args in calls if name == "ssh-keygen"))
        targets = {arg for name,args in calls if name == "ssh" for arg in args if arg.startswith("fixture-user@")}
        self.assertEqual({"fixture-user@host-a", "fixture-user@host-b"}, targets)

    def test_volume_preflight_requires_a_key_for_every_peer(self):
        self.volume_fixture()
        self.stub("ssh-keygen", "print('host-a ssh-ed25519 fixture-key' if args[1] == 'host-a' else '# no matching key')")
        result = self.run_volume_preflight()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no verified SSH host key found for brick host host-b", result.stderr)
        self.assertFalse(any(name in {"ssh", "scp", "mktemp"} for name,args in self.calls()))

    def test_volume_preflight_uses_noninteractive_sudo_for_controller_discovery(self):
        self.volume_fixture()
        self.stub("gluster", "raise SystemExit(1)")
        self.stub("sudo", """if args != ['-n', 'gluster', 'volume', 'info', 'fixture']:
    raise SystemExit('unexpected sudo command')
print('Type: Replicate\\nBrick1: host-a:/fixture/a\\nBrick2: host-b:/fixture/b')""")
        result = self.run_volume_preflight()
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.calls()
        self.assertEqual(1, sum(name == "gluster" for name, _ in calls))
        self.assertEqual(1, sum(name == "sudo" for name, _ in calls))
        self.assertEqual({"fixture-user@host-a", "fixture-user@host-b"},
                         {arg for name, args in calls if name == "ssh" for arg in args if arg.startswith("fixture-user@")})

    def test_volume_preflight_refuses_without_controller_discovery_access(self):
        self.volume_fixture()
        self.stub("gluster", "raise SystemExit(1)")
        self.stub("sudo", "raise SystemExit(1)")
        result = self.run_volume_preflight()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("cannot read volume fixture", result.stderr)
        self.assertFalse(any(name in {"ssh", "scp", "mktemp"} for name, _ in self.calls()))

    def test_volume_custom_layout_fails_before_discovery(self):
        self.volume_fixture()
        result = self.run_volume_preflight("-m", "/custom/home")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsupported", result.stderr)
        self.assertFalse(any(name in {"gluster", "ssh", "scp", "ssh-keygen", "mktemp"} for name,args in self.calls()))

    def test_brick_only_sudoers_uses_real_helper_argument_order(self):
        sudoers = self.root / "sudoers"
        result = subprocess.run(["bash", "-c", 'set -e; source "$1"; write_bootstrap_sudoers gluster-repair /opt/gluster-repair 1 "$2"',
                                 "fixture", str(LIBRARY), str(sudoers)], env=self.env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        content = sudoers.read_text()
        self.assertIn("brick-down --volume * --brick *", content)
        self.assertNotIn("gluster-worker.py", content)
        self.stub("gluster", "print('fixture volume start')")
        for argv in (["brick-down", "--volume", "fixture", "--brick", "/fixture/brick"],
                     ["brick-kick", "--volume", "fixture"]):
            actual = subprocess.run(["bash", str(ROOT / "gluster-host-ops.sh"), *argv], env=self.env, capture_output=True, text=True)
            self.assertEqual(0, actual.returncode, actual.stderr)


if __name__ == "__main__":
    unittest.main()
