# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Tests for backup maintenance."""
from __future__ import annotations

import json
import io
import os
import importlib.util
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gluster_heal_tool.controller_paths import default_backup_archive_dir
from gluster_heal_tool.backup_maintenance import (
    _stage_remote_artifact,
    archive_backup_artifacts,
    cleanup_backup_artifacts,
    manage_backup_artifacts,
    restore_backup_archive,
)
from gluster_heal_tool.cli import build_parser
from gluster_heal_tool.models import ApplyActionResult, BackupArtifact
from gluster_heal_tool.version import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]


def _result_with_artifacts(backup_root: Path, artifacts: list[BackupArtifact]) -> ApplyActionResult:
    return ApplyActionResult(
        action_id="action-1",
        logical_path="/example/path",
        action_type="cleanup_orphaned_symlink",
        execution_mode="execute",
        backup_root=str(backup_root),
        backup_mode="required",
        batch=True,
        status="completed",
        backup_artifacts=artifacts,
    )


class BackupMaintenanceTests(unittest.TestCase):
    def test_remote_staging_accepts_dangling_symlink(self) -> None:
        artifact = BackupArtifact(
            host="brick-a",
            source_path="/src/dangling",
            backup_path="/var/tmp/gluster-repair/backups/run/dangling",
            kind="backend",
        )
        with tempfile.TemporaryDirectory() as tmp:
            stage_root = Path(tmp)
            staged = stage_root / artifact.host / artifact.backup_path.lstrip("/")

            def staged_copy(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                staged.parent.mkdir(parents=True, exist_ok=True)
                if not staged.is_symlink():
                    staged.symlink_to("missing-target")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("gluster_heal_tool.backup_maintenance.subprocess.run", side_effect=staged_copy):
                actual, error = _stage_remote_artifact(artifact, stage_root)

            self.assertEqual(staged, actual)
            self.assertEqual("", error)
            self.assertTrue(staged.is_symlink())

    def test_remote_artifacts_are_staged_and_cleaned_by_recorded_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "archives" / "remote.tgz"
            remote_artifact = BackupArtifact(
                host="brick-a",
                source_path="/src/keep.txt",
                backup_path="/var/tmp/gluster-repair/backups/run/brick-a/backend/keep.txt",
                kind="backend",
            )
            result = _result_with_artifacts(tmpdir / "backup-root", [remote_artifact])

            def stage(artifact: BackupArtifact, stage_root: Path) -> tuple[Path | None, str]:
                staged = stage_root / artifact.host / artifact.backup_path.lstrip("/")
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text("remote backup", encoding="utf-8")
                return staged, ""

            with patch("gluster_heal_tool.backup_maintenance._stage_remote_artifact", side_effect=stage) as stage_mock:
                archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])
            stage_mock.assert_called_once()
            with tarfile.open(archive_path, "r:gz") as tar:
                self.assertIn("brick-a/var/tmp/gluster-repair/backups/run/brick-a/backend/keep.txt", tar.getnames())
                manifest = json.loads(tar.extractfile(tar.getmember("gluster-backup-manifest.json")).read().decode("utf-8"))
            self.assertEqual(3, manifest["schema_version"])
            self.assertEqual(
                [{
                    "host": "brick-a",
                    "member": "brick-a/var/tmp/gluster-repair/backups/run/brick-a/backend/keep.txt",
                    "destination": "/var/tmp/gluster-repair/backups/run/brick-a/backend/keep.txt",
                    "kind": "backend",
                }],
                manifest["artifacts"],
            )

            with patch("gluster_heal_tool.backup_maintenance._remove_remote_artifact", return_value="") as remove_mock, patch(
                "gluster_heal_tool.backup_fidelity.verify_remote_group", return_value=None
            ):
                cleanup_report = cleanup_backup_artifacts([result], archive_path=archive_path)
            self.assertTrue(cleanup_report["cleanup_completed"])
            self.assertEqual(["brick-a:/var/tmp/gluster-repair/backups/run/brick-a/backend/keep.txt"], cleanup_report["removed_paths"])
            remove_mock.assert_called_once_with(remote_artifact)

    def test_host_mapped_restore_uses_recorded_host_and_destination_for_mixed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            local_path = backup_root / "local" / "keep.txt"
            local_path.parent.mkdir(parents=True)
            local_path.write_text("local backup", encoding="utf-8")
            remote_path = "/var/tmp/gluster-repair/backups/run/backend/keep.txt"
            artifacts = [
                BackupArtifact("brick-a", "/src/keep.txt", remote_path, "backend"),
                BackupArtifact("brick-b", "/src/keep.txt", remote_path, "backend"),
                BackupArtifact("localhost", "/src/keep.txt", str(local_path), "backend"),
            ]
            result = _result_with_artifacts(backup_root, artifacts)
            archive_path = tmpdir / "archives" / "mixed.tgz"

            def stage(artifact: BackupArtifact, stage_root: Path) -> tuple[Path | None, str]:
                staged = stage_root / artifact.host / artifact.backup_path.lstrip("/")
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text(f"backup from {artifact.host}", encoding="utf-8")
                return staged, ""

            with patch("gluster_heal_tool.backup_maintenance._stage_remote_artifact", side_effect=stage):
                self.assertTrue(archive_backup_artifacts([result], archive_path)["archive_created"])
            with patch("gluster_heal_tool.backup_maintenance._remove_remote_artifact", return_value=""), patch(
                "gluster_heal_tool.backup_fidelity.verify_remote_group", return_value=None
            ):
                self.assertTrue(cleanup_backup_artifacts([result], archive_path=archive_path)["cleanup_completed"])
            self.assertFalse(local_path.exists())

            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch("gluster_heal_tool.backup_maintenance.subprocess.run", return_value=completed) as run_mock:
                report = restore_backup_archive(archive_path, on_conflict="overwrite")

            self.assertTrue(report["restore_complete"])
            self.assertEqual("local backup", local_path.read_text(encoding="utf-8"))
            self.assertIn(f"brick-a:{remote_path}", report["restored_paths"])
            self.assertIn(f"brick-b:{remote_path}", report["restored_paths"])
            self.assertEqual(4, run_mock.call_count)
            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(all(all(flag in command for flag in ("-H", "-A", "-X", "--numeric-ids", "--fake-super", "--files-from")) for command in commands))
            self.assertTrue(any(command[-1].endswith("@brick-a:/") for command in commands))
            self.assertTrue(any(command[-1].endswith("@brick-b:/") for command in commands))
            self.assertFalse((backup_root / "brick-a").exists())
            self.assertFalse((backup_root / "brick-b").exists())

    def test_host_mapped_restore_preview_is_read_only_and_lists_remote_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "remote.tgz"
            artifact = BackupArtifact("brick-a", "/src/keep.txt", "/var/tmp/backup/keep.txt", "backend")
            result = _result_with_artifacts(tmpdir / "backup-root", [artifact])

            def stage(item: BackupArtifact, stage_root: Path) -> tuple[Path | None, str]:
                staged = stage_root / item.host / item.backup_path.lstrip("/")
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text("remote backup", encoding="utf-8")
                return staged, ""

            with patch("gluster_heal_tool.backup_maintenance._stage_remote_artifact", side_effect=stage):
                archive_backup_artifacts([result], archive_path)
            with patch("gluster_heal_tool.backup_maintenance.subprocess.run") as run_mock:
                report = restore_backup_archive(archive_path, preview=True, cleanup_archive=True)

            run_mock.assert_not_called()
            self.assertFalse(report["restore_complete"])
            self.assertTrue(archive_path.exists())
            self.assertEqual("remote-rsync", report["planned_restores"][0]["operation"])
            self.assertIn("brick-a", report["planned_restores"][0]["command"][-1])

    def test_host_mapped_restore_retains_archive_after_interrupted_remote_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "remote.tgz"
            artifacts = [
                BackupArtifact("brick-a", "/src/one", "/var/tmp/backup/one", "backend"),
                BackupArtifact("brick-b", "/src/two", "/var/tmp/backup/two", "backend"),
            ]
            result = _result_with_artifacts(tmpdir / "backup-root", artifacts)

            def stage(item: BackupArtifact, stage_root: Path) -> tuple[Path | None, str]:
                staged = stage_root / item.host / item.backup_path.lstrip("/")
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text(item.host, encoding="utf-8")
                return staged, ""

            with patch("gluster_heal_tool.backup_maintenance._stage_remote_artifact", side_effect=stage):
                archive_backup_artifacts([result], archive_path)
            outcomes = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 23, "", "connection lost"),
            ]
            with patch("gluster_heal_tool.backup_maintenance.subprocess.run", side_effect=outcomes):
                report = restore_backup_archive(archive_path, on_conflict="overwrite", cleanup_archive=True)

            self.assertFalse(report["restore_complete"])
            self.assertFalse(report["archive_removed"])
            self.assertTrue(archive_path.exists())
            self.assertTrue(any("brick-b:/var/tmp/backup/two" in error for error in report["errors"]))

    def test_host_mapped_restore_rejects_absent_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "absent.tgz"
            manifest = {
                "schema_version": 2,
                "artifacts": [{
                    "host": "brick-a",
                    "member": "brick-a/var/tmp/backup/keep.txt",
                    "destination": "/var/tmp/backup/keep.txt",
                    "kind": "backend",
                }],
            }
            with tarfile.open(archive_path, "w:gz") as tar:
                payload = json.dumps(manifest).encode("utf-8")
                info = tarfile.TarInfo("gluster-backup-manifest.json")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            report = restore_backup_archive(archive_path, on_conflict="overwrite")

            self.assertFalse(report["restore_complete"])
            self.assertTrue(any("archived artifact is absent" in error for error in report["errors"]))

    def test_host_mapped_local_restore_preserves_links_owner_and_xattrs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            tree = backup_root / "tree"
            tree.mkdir(parents=True)
            payload = tree / "payload"
            payload.write_text("backup", encoding="utf-8")
            hardlink = tree / "payload-link"
            os.link(payload, hardlink)
            symlink = tree / "payload-symlink"
            symlink.symlink_to("payload")
            try:
                os.setxattr(payload, "user.gluster-repair-test", b"metadata", follow_symlinks=False)
            except OSError as exc:
                self.skipTest(f"filesystem does not support test xattrs: {exc}")
            acl = subprocess.run(
                ["setfacl", "-m", "u:65534:rw-", str(payload)],
                capture_output=True,
                text=True,
                check=False,
            )
            if acl.returncode:
                self.skipTest(f"filesystem does not support test ACLs: {acl.stderr}")
            result = _result_with_artifacts(
                backup_root,
                [BackupArtifact("localhost", "/src/tree", str(tree), "backend")],
            )
            archive_path = tmpdir / "tree.tgz"
            archive_backup_artifacts([result], archive_path)
            shutil.rmtree(tree)

            report = restore_backup_archive(archive_path, on_conflict="overwrite")

            self.assertTrue(report["restore_complete"])
            self.assertEqual("backup", payload.read_text(encoding="utf-8"))
            self.assertEqual(payload.stat().st_ino, hardlink.stat().st_ino)
            self.assertTrue(symlink.is_symlink())
            self.assertEqual("payload", os.readlink(symlink))
            self.assertEqual(os.getuid(), payload.stat().st_uid)
            self.assertEqual(os.getgid(), payload.stat().st_gid)
            self.assertEqual(b"metadata", os.getxattr(payload, "user.gluster-repair-test", follow_symlinks=False))
            restored_acl = subprocess.run(
                ["getfacl", "-cpn", str(payload)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, restored_acl.returncode, restored_acl.stderr)
            self.assertIn("user:65534:rw-", restored_acl.stdout)

    def test_archive_and_cleanup_backup_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("keep", encoding="utf-8")
            dir_path = backup_root / "gluster-repair-backups" / "run2" / "node-a" / "symlink-backend" / "tree"
            (dir_path / "child.txt").parent.mkdir(parents=True, exist_ok=True)
            (dir_path / "child.txt").write_text("child", encoding="utf-8")

            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/tree",
                        backup_path=str(dir_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])
            self.assertEqual(str(archive_path), archive_report["archive_path"])
            self.assertTrue(archive_path.exists())
            with tarfile.open(archive_path, "r:gz") as tar:
                names = tar.getnames()
                manifest = json.loads(tar.extractfile(tar.getmember("gluster-backup-manifest.json")).read().decode("utf-8"))
            self.assertIn("gluster-repair-backups/run1/node-a/backend/keep.txt", names)
            self.assertIn("gluster-repair-backups/run2/node-a/symlink-backend/tree", names)
            self.assertEqual(__version__, manifest["tool_version"])

            cleanup_report = cleanup_backup_artifacts([result], archive_path=archive_path)
            self.assertTrue(cleanup_report["cleanup_completed"])
            self.assertFalse(file_path.exists())
            self.assertFalse(dir_path.exists())
            self.assertTrue(backup_root.exists())
            self.assertFalse((backup_root / "gluster-repair-backups").exists())

    def test_manage_backup_artifacts_archives_then_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("keep", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            report = manage_backup_artifacts([result], archive_path=str(archive_path), cleanup=True)
            self.assertFalse(report["errors"])
            self.assertTrue(report["archive"]["archive_created"])
            self.assertTrue(report["cleanup"]["cleanup_completed"])
            self.assertTrue(archive_path.exists())
            self.assertFalse(file_path.exists())

    def test_restore_backup_archive_uses_manifest_and_restores_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("keep", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])
            file_path.unlink()

            restore_report = restore_backup_archive(archive_path)
            self.assertEqual(str(backup_root), restore_report["restore_root"])
            self.assertFalse(restore_report["errors"])
            self.assertIn(str(file_path), restore_report["restored_paths"])
            self.assertTrue(file_path.exists())
            self.assertEqual("keep", file_path.read_text(encoding="utf-8"))

    def test_restore_backup_archive_skips_newer_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("old", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])

            file_path.write_text("newer", encoding="utf-8")
            future = time.time() + 3600
            os.utime(file_path, (future, future))

            restore_report = restore_backup_archive(archive_path)
            self.assertFalse(restore_report["errors"])
            self.assertIn(str(file_path), restore_report["skipped_newer_paths"])
            self.assertEqual("newer", file_path.read_text(encoding="utf-8"))

    def test_restore_rejected_hardlink_preserves_conflict_destinations(self) -> None:
        target_cases = {
            "traversal": "../outside",
            "missing": "missing-target",
            "symlink": "link-target",
        }
        for conflict_policy in ("overwrite", "rename"):
            for target_kind, linkname in target_cases.items():
                with self.subTest(conflict_policy=conflict_policy, target_kind=target_kind), tempfile.TemporaryDirectory() as tmp:
                    tmpdir = Path(tmp)
                    archive_path = tmpdir / "backup.tgz"
                    restore_root = tmpdir / "restore"
                    restore_root.mkdir()
                    existing = restore_root / "keep.txt"
                    existing.write_text("current", encoding="utf-8")
                    if target_kind == "symlink":
                        (restore_root / linkname).symlink_to("missing-target")
                    with tarfile.open(archive_path, "w:gz") as tar:
                        member = tarfile.TarInfo("keep.txt")
                        member.type = tarfile.LNKTYPE
                        member.linkname = linkname
                        tar.addfile(member)

                    report = restore_backup_archive(
                        archive_path,
                        restore_root=str(restore_root),
                        cleanup_archive=True,
                        on_conflict=conflict_policy,
                    )

                    self.assertEqual("current", existing.read_text(encoding="utf-8"))
                    self.assertFalse(report["renamed_paths"])
                    self.assertFalse(report["restore_complete"])
                    self.assertFalse(report["archive_removed"])
                    self.assertTrue(archive_path.exists())
                    self.assertTrue(any("hardlink target" in warning or "unsafe hardlink" in warning for warning in report["warnings"]))

    def test_dangling_symlink_backup_lifecycle_preserves_the_link_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            link_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "dangling"
            link_path.parent.mkdir(parents=True, exist_ok=True)
            link_path.symlink_to("missing-target")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/dangling",
                        backup_path=str(link_path),
                        kind="backend",
                    ),
                ],
            )
            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"

            archive_report = archive_backup_artifacts([result], archive_path)

            self.assertTrue(archive_report["archive_created"])
            with tarfile.open(archive_path, "r:gz") as tar:
                member = tar.getmember("gluster-repair-backups/run1/node-a/dangling")
                self.assertTrue(member.issym())
                self.assertEqual("missing-target", member.linkname)

            cleanup_report = cleanup_backup_artifacts([result], archive_path=archive_path)

            self.assertTrue(cleanup_report["cleanup_completed"])
            self.assertIn(str(link_path), cleanup_report["removed_paths"])
            self.assertFalse(link_path.is_symlink())

            restore_report = restore_backup_archive(archive_path, on_conflict="overwrite")

            self.assertTrue(restore_report["restore_complete"])
            self.assertTrue(link_path.is_symlink())
            self.assertEqual("missing-target", os.readlink(link_path))

            repeat_cleanup = cleanup_backup_artifacts([result], archive_path=archive_path)
            self.assertTrue(repeat_cleanup["cleanup_completed"])
            self.assertFalse(link_path.is_symlink())

            missing_report = cleanup_backup_artifacts([result], archive_path=archive_path)
            self.assertFalse(missing_report["cleanup_completed"])
            self.assertIn(str(link_path), missing_report["missing_paths"])

    def test_restore_rejects_preexisting_symlink_parent_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "backup.tgz"
            restore_root = tmpdir / "restore"
            outside = tmpdir / "outside"
            restore_root.mkdir()
            outside.mkdir()
            (restore_root / "parent").symlink_to(outside, target_is_directory=True)
            with tarfile.open(archive_path, "w:gz") as tar:
                payload = b"escaped"
                member = tarfile.TarInfo("parent/escaped.txt")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))

            report = restore_backup_archive(
                archive_path,
                restore_root=str(restore_root),
                cleanup_archive=True,
                on_conflict="overwrite",
            )

            self.assertFalse((outside / "escaped.txt").exists())
            self.assertTrue((restore_root / "parent").is_symlink())
            self.assertFalse(report["restore_complete"])
            self.assertFalse(report["archive_removed"])
            self.assertTrue(archive_path.exists())
            self.assertTrue(any("unsafe restore destination" in warning for warning in report["warnings"]))

    def test_restore_rejects_archive_symlink_parent_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "backup.tgz"
            restore_root = tmpdir / "restore"
            outside = tmpdir / "outside"
            outside.mkdir()
            with tarfile.open(archive_path, "w:gz") as tar:
                link = tarfile.TarInfo("link")
                link.type = tarfile.SYMTYPE
                link.linkname = str(outside)
                tar.addfile(link)
                payload = b"escaped"
                member = tarfile.TarInfo("link/escaped.txt")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))

            report = restore_backup_archive(archive_path, restore_root=str(restore_root))

            self.assertTrue((restore_root / "link").is_symlink())
            self.assertFalse((outside / "escaped.txt").exists())
            self.assertFalse(report["restore_complete"])
            self.assertTrue(any("unsafe restore destination" in warning for warning in report["warnings"]))

    def test_restore_keeps_archive_after_skipped_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "backup.tgz"
            restore_root = tmpdir / "restore"
            restore_root.mkdir()
            existing = restore_root / "keep.txt"
            existing.write_text("current", encoding="utf-8")
            with tarfile.open(archive_path, "w:gz") as tar:
                payload = b"archive"
                member = tarfile.TarInfo("keep.txt")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))

            report = restore_backup_archive(
                archive_path,
                restore_root=str(restore_root),
                cleanup_archive=True,
                on_conflict="skip",
            )

            self.assertEqual("current", existing.read_text(encoding="utf-8"))
            self.assertFalse(report["restore_complete"])
            self.assertFalse(report["archive_removed"])
            self.assertTrue(archive_path.exists())
            self.assertTrue(any("archive retained" in warning for warning in report["warnings"]))

    def test_restore_backup_archive_can_overwrite_existing_paths_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("archive", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])

            file_path.write_text("current", encoding="utf-8")
            restore_report = restore_backup_archive(archive_path, on_conflict="overwrite")
            self.assertFalse(restore_report["errors"])
            self.assertEqual("archive", file_path.read_text(encoding="utf-8"))
            self.assertIn(str(file_path), restore_report["restored_paths"])

    def test_restore_backup_archive_can_rename_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("archive", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])

            file_path.write_text("current", encoding="utf-8")
            restore_report = restore_backup_archive(archive_path, on_conflict="rename", rename_suffix=".kept")
            self.assertFalse(restore_report["errors"])
            self.assertEqual("archive", file_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(restore_report["renamed_paths"]))
            renamed = Path(restore_report["renamed_paths"][0])
            self.assertTrue(renamed.name.startswith("keep.txt.kept-"))

    def test_restore_backup_archive_can_apply_choice_to_all_remaining_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_one = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep-one.txt"
            file_two = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep-two.txt"
            file_one.parent.mkdir(parents=True, exist_ok=True)
            file_one.write_text("archive-one", encoding="utf-8")
            file_two.write_text("archive-two", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep-one.txt",
                        backup_path=str(file_one),
                        kind="backend",
                    ),
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep-two.txt",
                        backup_path=str(file_two),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])

            file_one.write_text("current-one", encoding="utf-8")
            file_two.write_text("current-two", encoding="utf-8")
            prompt_input = io.StringIO("r\nCONFIRM\nALL\n")
            prompt_output = io.StringIO()
            restore_report = restore_backup_archive(
                archive_path,
                on_conflict="prompt",
                prompt_input=prompt_input,
                prompt_output=prompt_output,
            )
            self.assertFalse(restore_report["errors"])
            self.assertEqual("archive-one", file_one.read_text(encoding="utf-8"))
            self.assertEqual("archive-two", file_two.read_text(encoding="utf-8"))
            self.assertEqual(2, len(restore_report["renamed_paths"]))
            rendered_prompt = prompt_output.getvalue()
            self.assertIn(f"destination: {file_one}", rendered_prompt)
            self.assertIn("archive type: file; size=11", rendered_prompt)
            self.assertIn("[o] overwrite", rendered_prompt)
            self.assertIn("[r] rename", rendered_prompt)
            self.assertIn("[s] skip", rendered_prompt)
            self.assertIn("rollback:", rendered_prompt)
            self.assertIn("Type CONFIRM exactly", rendered_prompt)
            self.assertIn("Type ALL exactly", rendered_prompt)

    def test_restore_backup_archive_unconfirmed_choice_skips_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = (
                backup_root
                / "gluster-repair-backups"
                / "run1"
                / "node-a"
                / "backend"
                / "keep.txt"
            )
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("archive", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )
            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_backup_artifacts([result], archive_path)
            file_path.write_text("current", encoding="utf-8")

            prompt_output = io.StringIO()
            restore_report = restore_backup_archive(
                archive_path,
                on_conflict="prompt",
                prompt_input=io.StringIO("o\nno\n"),
                prompt_output=prompt_output,
            )

            self.assertEqual("current", file_path.read_text(encoding="utf-8"))
            self.assertFalse(restore_report["errors"])
            self.assertNotIn(str(file_path), restore_report["restored_paths"])
            self.assertIn("was not changed", prompt_output.getvalue())

    def test_restore_backup_archive_can_override_root_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / "legacy-backup.tgz"
            restore_root = tmpdir / "restore-root"
            with tarfile.open(archive_path, "w:gz") as tar:
                payload = b"legacy"
                member = tarfile.TarInfo(name="backend/legacy.txt")
                member.size = len(payload)
                member.mtime = int(time.time())
                tar.addfile(member, io.BytesIO(payload))

            restore_report = restore_backup_archive(archive_path, restore_root=str(restore_root))
            self.assertFalse(restore_report["errors"])
            restored = restore_root / "backend" / "legacy.txt"
            self.assertTrue(restored.exists())
            self.assertEqual("legacy", restored.read_text(encoding="utf-8"))

    def test_restore_backup_archive_can_use_status_archive_implicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("keep", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            archive_path = tmpdir / "archives" / "backup-artifacts.tgz"
            archive_report = archive_backup_artifacts([result], archive_path)
            self.assertTrue(archive_report["archive_created"])
            file_path.unlink()

            restore_report = restore_backup_archive("", status_archive_path=str(archive_path))
            self.assertEqual(str(archive_path), restore_report["archive_path"])
            self.assertEqual("status", restore_report["archive_source"])
            self.assertFalse(restore_report["errors"])
            self.assertTrue(file_path.exists())

    def test_restore_backup_archive_can_use_latest_archive_implicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            backup_dir = tmpdir / "archives"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_root = tmpdir / "backup-root"
            file_path = backup_root / "gluster-repair-backups" / "run1" / "node-a" / "backend" / "keep.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("keep", encoding="utf-8")
            result = _result_with_artifacts(
                backup_root,
                [
                    BackupArtifact(
                        host="localhost",
                        source_path="/src/keep.txt",
                        backup_path=str(file_path),
                        kind="backend",
                    ),
                ],
            )

            old_archive = backup_dir / "old.tgz"
            new_archive = backup_dir / "new.tgz"
            archive_backup_artifacts([result], old_archive)
            time.sleep(0.01)
            archive_backup_artifacts([result], new_archive)
            file_path.unlink()

            restore_report = restore_backup_archive("", backup_dir=backup_dir)
            self.assertEqual(str(new_archive), restore_report["archive_path"])
            self.assertEqual("latest", restore_report["archive_source"])
            self.assertFalse(restore_report["errors"])
            self.assertTrue(file_path.exists())

    def test_parser_exposes_backup_maintenance_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "apply-run",
                "--apply-in",
                "/tmp/apply.json",
                "--archive-backups",
                "/tmp/archive.tgz",
                "--cleanup-backups",
                "--parallel-actions",
                "2",
                "--parallel-nice",
                "7",
                "--run-dir",
                "/tmp/run-dir",
            ]
        )
        self.assertEqual("/tmp/archive.tgz", args.archive_backups)
        self.assertTrue(args.cleanup_backups)
        self.assertEqual(2, args.parallel_actions)
        self.assertEqual(7, args.parallel_nice)
        self.assertEqual("/tmp/run-dir", args.run_dir)


    def test_parser_exposes_backup_restore_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "backup-restore",
                "--restore-root",
                "/tmp/restore-root",
                "--on-conflict",
                "rename",
                "--rename-suffix",
                ".kept",
                "--cleanup-archive",
                "--preview",
            ]
        )
        self.assertIsNone(args.archive)
        self.assertEqual("/tmp/restore-root", args.restore_root)
        self.assertEqual("rename", args.on_conflict)
        self.assertEqual(".kept", args.rename_suffix)
        self.assertEqual(str(default_backup_archive_dir()), args.backup_dir)
        self.assertTrue(args.cleanup_archive)
        self.assertTrue(args.preview)

    def test_parser_exposes_version(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--version"])
        self.assertEqual(0, ctx.exception.code)

    def test_manager_parser_exposes_backup_maintenance_flags(self) -> None:
        script = REPO_ROOT / "gluster-manager.py"
        spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        args = parser.parse_args(
            [
                "apply-run",
                "--apply-in",
                "/tmp/apply.json",
                "--archive-backups",
                "/tmp/archive.tgz",
                "--cleanup-backups",
                "--parallel-actions",
                "2",
                "--parallel-nice",
                "7",
                "--run-dir",
                "/tmp/run-dir",
            ]
        )
        self.assertEqual("/tmp/archive.tgz", args.archive_backups)
        self.assertTrue(args.cleanup_backups)
        self.assertEqual(2, args.parallel_actions)
        self.assertEqual(7, args.parallel_nice)
        self.assertEqual("/tmp/run-dir", args.run_dir)


    def test_manager_parser_exposes_backup_restore_command(self) -> None:
        script = REPO_ROOT / "gluster-manager.py"
        spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        args = parser.parse_args(
            [
                "backup-restore",
                "--restore-root",
                "/tmp/restore-root",
                "--on-conflict",
                "rename",
                "--rename-suffix",
                ".kept",
                "--cleanup-archive",
            ]
        )
        self.assertIsNone(args.archive)
        self.assertEqual("/tmp/restore-root", args.restore_root)
        self.assertEqual("rename", args.on_conflict)
        self.assertEqual(".kept", args.rename_suffix)
        self.assertEqual(str(default_backup_archive_dir()), args.backup_dir)
        self.assertTrue(args.cleanup_archive)

    def test_manager_parser_exposes_version(self) -> None:
        script = REPO_ROOT / "gluster-manager.py"
        spec = importlib.util.spec_from_file_location("gluster_manager_script", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--version"])
        self.assertEqual(0, ctx.exception.code)
