# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Real local transfer/archive/restore fidelity, with synthetic host transport."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from gluster_heal_tool import backup_maintenance as backups
from gluster_heal_tool.backup_fidelity import snapshot
from gluster_heal_tool.models import BackupArtifact
from tests.test_backup_maintenance import _result_with_artifacts


class BackupFidelityTests(unittest.TestCase):

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for read-back proof")
    def test_readback_detects_missing_children_and_changed_xattrs_without_writes(self):
        from gluster_heal_tool import backup_fidelity as fidelity
        for damage in ("missing-child", "changed-xattr"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                server = root / "server"
                tree = server / "fixture/tree"
                self.make_tree(tree)
                shell = root / "transport.py"
                shell.write_text(
                    "import os,sys\n"
                    "args=sys.argv[sys.argv.index('--server'):]\n"
                    "assert args[-1]=='/'\n"
                    f"args[-1]={str(server) + '/'!r}\n"
                    f"os.execv({shutil.which('rsync')!r},['rsync',*args])\n"
                )
                actual_run = subprocess.run
                def transport(command, **kwargs):
                    args = list(command)
                    args[args.index("-e") + 1] = shlex.join([sys.executable, str(shell)])
                    args[args.index("--rsync-path") + 1] = "rsync"
                    return actual_run(args, **kwargs)
                artifact = BackupArtifact("host-a", "/source/tree", "/fixture/tree", "backend")
                stage = root / "stage"
                with patch("gluster_heal_tool.backup_fidelity.subprocess.run", side_effect=transport):
                    fidelity.stage_remote_group([artifact], stage)
                    if damage == "missing-child":
                        (tree / "payload").unlink()
                    else:
                        os.setxattr(tree / "payload", "user.fixture", b"changed")
                    remote_before = snapshot([(tree, "tree")])
                    staged_before = snapshot([(stage / "host-a/fixture/tree", "tree")])
                    with self.assertRaisesRegex(ValueError, "verification failed"):
                        fidelity.verify_remote_group("host-a", ["/fixture/tree"], stage)
                    self.assertEqual(remote_before, snapshot([(tree, "tree")]))
                    self.assertEqual(staged_before, snapshot([(stage / "host-a/fixture/tree", "tree")]))


    def make_tree(self, root):
        root.mkdir(parents=True)
        file = root / "payload"
        file.write_bytes(b"backup payload\0binary\n")
        os.chmod(file, 0o640)
        os.setxattr(file, "user.fixture", b"xattr\0bytes")
        os.link(file, root / "hardlink")
        (root / "dangling").symlink_to("absent-target")
        os.chmod(root, 0o2750)
        for path in (file, root / "dangling", root):
            os.utime(path, ns=(1700000000123456789, 1700000000123456789), follow_symlinks=False)

    def test_local_archive_cleanup_restore_preserves_full_selected_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup" / "tree"
            self.make_tree(source)
            expected = snapshot([(source, "tree")])
            result = _result_with_artifacts(root / "backup", [BackupArtifact("localhost", "/fixture/tree", str(source), "backend")])
            archive = root / "archive.tgz"
            report = backups.manage_backup_artifacts([result], archive_path=str(archive), cleanup=True)
            self.assertTrue(report["archive"].get("fidelity_verified"), report)
            self.assertFalse(source.exists(), report)
            restored = backups.restore_backup_archive(archive, on_conflict="overwrite", cleanup_archive=True)
            self.assertTrue(restored["restore_complete"], restored)
            self.assertEqual(expected, snapshot([(source, "tree")]))

    def test_metadata_capture_error_retains_originals_and_previous_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup"
            source.write_text("original")
            archive = root / "archive.tgz"
            archive.write_bytes(b"previous archive")
            result = _result_with_artifacts(root, [BackupArtifact("localhost", "/fixture/file", str(source), "backend")])
            with patch("os.listxattr", side_effect=PermissionError("metadata inaccessible")):
                report = backups.manage_backup_artifacts([result], archive_path=str(archive), cleanup=True)
            self.assertTrue(report["errors"])
            self.assertTrue(source.exists())
            self.assertEqual(b"previous archive", archive.read_bytes())

    def test_corrupt_archive_is_not_authority_to_remove_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup"
            source.write_text("original")
            archive = root / "archive.tgz"
            result = _result_with_artifacts(root, [BackupArtifact("localhost", "/fixture/file", str(source), "backend")])
            self.assertTrue(backups.archive_backup_artifacts([result], archive)["archive_created"])
            archive.write_bytes(b"corrupt")
            report = backups.cleanup_backup_artifacts([result], archive_path=archive)
            self.assertFalse(report["cleanup_completed"])
            self.assertTrue(source.exists())

    def test_changed_backup_is_retained_after_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup"
            source.write_text("original")
            archive = root / "archive.tgz"
            result = _result_with_artifacts(root, [BackupArtifact("localhost", "/fixture/file", str(source), "backend")])
            self.assertTrue(backups.archive_backup_artifacts([result], archive)["archive_created"])
            source.write_text("changed")
            report = backups.cleanup_backup_artifacts([result], archive_path=archive)
            self.assertFalse(report["cleanup_completed"])
            self.assertEqual("changed", source.read_text())

    def test_unverified_cleanup_refuses_remote_removal(self):
        result = _result_with_artifacts(Path("/fixture/backup"), [BackupArtifact("host-a", "/fixture/file", "/fixture/backup/file", "backend")])
        with patch.object(backups, "_remove_remote_artifact") as remove:
            report = backups.cleanup_backup_artifacts([result])
        self.assertFalse(report["cleanup_completed"])
        remove.assert_not_called()

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required for transport-equivalent proof")
    def test_remote_roundtrip_preserves_cross_artifact_hardlinks_and_host_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            servers = {host:root / host for host in ("host-a", "host-b")}
            artifacts = []
            for host, server in servers.items():
                tree = server / "fixture" / "tree"
                self.make_tree(tree)
                with (tree / "payload").open("ab") as payload:
                    payload.write(host.encode())
                # A second artifact shares an inode with the first artifact.
                os.link(tree / "payload", server / "fixture" / "second")
                artifacts.extend([BackupArtifact(host, "/source/tree", "/fixture/tree", "backend"),
                                  BackupArtifact(host, "/source/second", "/fixture/second", "backend")])
            expected = {host:snapshot([(server / "fixture/tree", "tree"), (server / "fixture/second", "second")]) for host,server in servers.items()}
            actual_run = subprocess.run
            shell = root / "fixture-shell.py"
            shell.write_text("import os,sys\nfrom pathlib import Path\n"
                "host = next(a for a in sys.argv if a in {'host-a','host-b'})\n"
                "args = sys.argv[sys.argv.index('--server'):]\n"
                "assert args[-1] == '/'\n"
                "args[-1] = str(Path(__file__).parent / host) + '/'\n"
                f"os.execv({shutil.which('rsync')!r}, ['rsync', *args])\n")
            def transport(command, **kwargs):
                self.assertEqual("rsync", command[0])
                self.assertIn("-H", command)
                self.assertIn("--fake-super", command)
                args = list(command)
                args[args.index("-e") + 1] = shlex.join([sys.executable, str(shell)])
                args[args.index("--rsync-path") + 1] = "rsync"
                return actual_run(args, **kwargs)
            result = _result_with_artifacts(Path("/fixture"), artifacts)
            archive = root / "archive.tgz"
            def remove(artifact):
                path = servers[artifact.host] / artifact.backup_path.lstrip("/")
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                return ""
            with (patch("gluster_heal_tool.backup_fidelity.subprocess.run", side_effect=transport),
                  patch.object(backups, "_remove_remote_artifact", side_effect=remove)):
                archived = backups.manage_backup_artifacts([result], archive_path=str(archive), cleanup=True)
                self.assertTrue(archived["archive"].get("fidelity_verified"), archived)
                self.assertTrue(archived["cleanup"]["cleanup_completed"], archived)
                for server in servers.values():
                    self.assertFalse((server / "fixture/tree").exists())
                    self.assertFalse((server / "fixture/second").exists())
                restored = backups.restore_backup_archive(archive, on_conflict="overwrite", cleanup_archive=True)
            self.assertTrue(restored["restore_complete"], restored)
            self.assertFalse(archive.exists())
            for host,server in servers.items():
                self.assertEqual(expected[host], snapshot([(server / "fixture/tree", "tree"), (server / "fixture/second", "second")]))

    def test_remote_success_exit_with_verification_difference_retains_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup"
            source.write_text("original")
            artifact = BackupArtifact("host-a", "/source/file", "/fixture/file", "backend")
            result = _result_with_artifacts(root, [artifact])
            def stage(item, stage_root):
                path = stage_root / item.host / item.backup_path.lstrip("/")
                path.parent.mkdir(parents=True)
                shutil.copy2(source, path)
                return path, ""
            archive = root / "archive.tgz"
            with patch.object(backups, "_stage_remote_artifact", side_effect=stage):
                self.assertTrue(backups.archive_backup_artifacts([result], archive)["archive_created"])
            def transfer(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, ">f..t...... file\n" if "--dry-run" in command else "", "")
            with patch("gluster_heal_tool.backup_fidelity.subprocess.run", side_effect=transfer):
                report = backups.restore_backup_archive(archive, on_conflict="overwrite", cleanup_archive=True)
            self.assertFalse(report["restore_complete"])
            self.assertTrue(archive.exists())

    def test_restore_permission_failure_retains_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup"
            source.write_text("original")
            os.setxattr(source, "user.fixture", b"preserve")
            result = _result_with_artifacts(root, [BackupArtifact("localhost", "/fixture/file", str(source), "backend")])
            archive = root / "archive.tgz"
            self.assertTrue(backups.archive_backup_artifacts([result], archive)["archive_created"])
            source.unlink()
            with patch("os.setxattr", side_effect=PermissionError("metadata denied")):
                report = backups.restore_backup_archive(archive, on_conflict="overwrite", cleanup_archive=True)
            self.assertFalse(report["restore_complete"])
            self.assertTrue(archive.exists())

    def test_fake_super_metadata_is_retained_in_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = BackupArtifact("host-a", "/fixture/source", "/fixture/backup", "backend")
            result = _result_with_artifacts(root, [artifact])
            def stage(item, stage_root):
                target = stage_root / item.host / item.backup_path.lstrip("/")
                target.parent.mkdir(parents=True)
                target.write_bytes(b"payload")
                os.setxattr(target, "user.rsync.%stat", b"100640 0,0 1234:5678")
                os.setxattr(target, "user.rsync.%xattr.trusted.fixture", b"privileged-metadata")
                return target, ""
            archive = root / "archive.tgz"
            with patch.object(backups, "_stage_remote_artifact", side_effect=stage):
                self.assertTrue(backups.archive_backup_artifacts([result], archive)["fidelity_verified"])
            manifest = backups._read_archive_manifest(archive)
            import base64
            attrs = manifest["fidelity"]["members"]["host-a/fixture/backup"]["xattrs"]
            decoded = {base64.urlsafe_b64decode(k + '=' * (-len(k) % 4)).decode():base64.b64decode(v) for k,v in attrs.items()}
            self.assertEqual(b"100640 0,0 1234:5678", decoded["user.rsync.%stat"])
            self.assertEqual(b"privileged-metadata", decoded["user.rsync.%xattr.trusted.fixture"])


if __name__ == "__main__":
    unittest.main()
