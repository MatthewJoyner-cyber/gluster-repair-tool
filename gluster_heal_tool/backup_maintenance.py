# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Backup archive maintenance helpers."""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import sys
import base64
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .models import ApplyActionResult, BackupArtifact
from .remote_ops import rsync_pull_command, rsync_push_command, ssh_remote_command
from . import backup_fidelity as fidelity
from .version import __version__


ARCHIVE_MANIFEST_NAME = "gluster-backup-manifest.json"
from .controller_paths import default_backup_archive_dir

DEFAULT_BACKUP_ARCHIVE_DIR = default_backup_archive_dir()
_ARCHIVE_XATTR_PREFIX = "GLUSTER.repair.xattr."


def collect_backup_artifacts(results: list[ApplyActionResult]) -> list[BackupArtifact]:
    artifacts: list[BackupArtifact] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        for artifact in result.backup_artifacts:
            key = (artifact.host, artifact.backup_path)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(artifact)
    return artifacts


def _result_backup_root(results: list[ApplyActionResult]) -> str:
    for result in results:
        backup_root = str(result.backup_root or "").strip()
        if backup_root:
            return backup_root
    return ""


def _result_backup_base(results: list[ApplyActionResult], paths: list[Path]) -> Path:
    backup_root = _result_backup_root(results)
    if backup_root:
        return Path(backup_root)
    parents = [str(path.parent) for path in paths]
    if not parents:
        return Path(".")
    return Path(os.path.commonpath(parents))


def _is_local_artifact(artifact: BackupArtifact) -> bool:
    return (
        not str(artifact.host or "").strip()
        or str(artifact.host).strip().lower() in {"localhost", "127.0.0.1", "::1"}
    )


def _remote_stage_path(stage_root: Path, artifact: BackupArtifact) -> Path:
    host = fidelity.safe_host(str(artifact.host).strip())
    return stage_root / host / str(artifact.backup_path).lstrip("/")


def _stage_remote_artifact(artifact: BackupArtifact, stage_root: Path) -> tuple[Path | None, str]:
    try:
        staged = fidelity.stage_remote_group([artifact], stage_root)
        target = staged[0][1]
        if not _path_present(target):
            return None, "remote copy completed without a staged artifact"
        return target, ""
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _remove_remote_artifact(artifact: BackupArtifact) -> str:
    command = ssh_remote_command(str(artifact.host), ["rm", "-rf", "--", artifact.backup_path])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        return (completed.stderr or completed.stdout).strip() or f"exit {completed.returncode}"
    return ""


def _collapse_paths(paths: list[Path]) -> list[Path]:
    collapsed: list[Path] = []
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        if any(_is_ancestor(existing, path) for existing in collapsed):
            continue
        collapsed = [existing for existing in collapsed if not _is_ancestor(path, existing)]
        collapsed.append(path)
    return sorted(collapsed, key=lambda item: (len(item.parts), str(item)))


def _is_ancestor(candidate: Path, path: Path) -> bool:
    if candidate == path:
        return True
    try:
        path.relative_to(candidate)
    except ValueError:
        return False
    return True


def _archive_base(backup_root: str, paths: list[Path]) -> Path:
    if backup_root:
        return Path(backup_root)
    parents = [str(path.parent) for path in paths]
    if not parents:
        return Path(".")
    return Path(os.path.commonpath(parents))


def _arcname(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
    except ValueError:
        return path.name
    rel_text = str(rel)
    return path.name if rel_text == "." else rel_text


def _archive_xattr_filter(path: Path, arcname: str):
    """Attach portable xattr records to the tar members created from one path."""
    def filter_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
        try:
            suffix = Path(member.name).relative_to(arcname)
            source = path if str(suffix) == "." else path / suffix
            for name in os.listxattr(source, follow_symlinks=False):
                value = os.getxattr(source, name, follow_symlinks=False)
                encoded_name = base64.urlsafe_b64encode(name.encode("utf-8", "surrogateescape")).decode("ascii").rstrip("=")
                member.pax_headers[f"{_ARCHIVE_XATTR_PREFIX}{encoded_name}"] = base64.b64encode(value).decode("ascii")
            member.pax_headers[fidelity.MTIME_NS] = str(source.lstat().st_mtime_ns)
        except OSError as exc:
            raise ValueError(f"cannot capture backup metadata: {member.name}: {exc}") from exc
        return member

    return filter_member


def _archive_manifest(
    backup_root: str,
    archive_base: str,
    archived_entries: int,
    artifacts: list[dict[str, str]],
    captured: dict[str, object],
) -> bytes:
    payload = {
        "schema_version": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": __version__,
        "backup_root": backup_root,
        "archive_base": archive_base,
        "archive_entries": archived_entries,
        "artifacts": artifacts,
        "fidelity": {"contract": "backup-v1", "remote_encoding": "rsync-fake-super", "members": captured},
    }
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def archive_backup_artifacts(results: list[ApplyActionResult], archive_path: str | Path) -> dict[str, object]:
    artifacts = collect_backup_artifacts(results)
    backup_root = _result_backup_root(results)
    archive_file = Path(archive_path)
    report = {
        "available": bool(artifacts), "requested_archive_path": str(archive_file),
        "archive_created": False, "archive_path": "", "archive_entries": 0,
        "archive_base": "host-scoped", "fidelity_verified": False,
        "warnings": [], "errors": [],
        "paths": [f"{a.host}:{a.backup_path}" for a in artifacts],
    }
    if not artifacts:
        report["warnings"].append("no backup artifacts found to archive")
        return report
    try:
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="gluster-backup-stage-") as tmp:
            stage_root = Path(tmp)
            staged = []
            groups = {}
            for artifact in artifacts:
                if _is_local_artifact(artifact):
                    staged.append((artifact, Path(artifact.backup_path)))
                else:
                    groups.setdefault(artifact.host, []).append(artifact)
            for host, group in groups.items():
                if len(group) == 1:
                    path, error = _stage_remote_artifact(group[0], stage_root)
                    if path is None:
                        raise ValueError(f"could not stage {host}:{group[0].backup_path}: {error}")
                    staged.append((group[0], path))
                else:
                    staged.extend(fidelity.stage_remote_group(group, stage_root))
            entries = []
            manifest_artifacts = []
            for artifact, path in staged:
                member = (_arcname(path, _archive_base(backup_root, [path]))
                          if _is_local_artifact(artifact)
                          else str(_remote_stage_path(Path("."), artifact)))
                if not _safe_archive_member(member) or member == ARCHIVE_MANIFEST_NAME:
                    raise ValueError(f"unsafe backup archive member: {member}")
                entries.append((path, member))
                manifest_artifacts.append(dict(host=str(artifact.host), member=member,
                                               destination=str(artifact.backup_path), kind=str(artifact.kind)))
            captured = fidelity.snapshot(entries)
            # Keep an existing archive intact until the new archive is verified.
            with tempfile.TemporaryDirectory(prefix=".gluster-archive-", dir=archive_file.parent) as output:
                pending = Path(output) / "backup.tgz"
                with tarfile.open(pending, "w:gz", format=tarfile.PAX_FORMAT) as tar:
                    for path, member in entries:
                        tar.add(path, arcname=member, recursive=True, filter=_archive_xattr_filter(path, member))
                    manifest_bytes = _archive_manifest(backup_root, "host-scoped", len(entries), manifest_artifacts, captured)
                    info = tarfile.TarInfo(name=ARCHIVE_MANIFEST_NAME)
                    info.size = len(manifest_bytes)
                    tar.addfile(info, io.BytesIO(manifest_bytes))
                fidelity.verify_tar(pending, captured, ARCHIVE_MANIFEST_NAME)
                if fidelity.snapshot(entries) != captured:
                    raise ValueError("backup changed while the archive was being created")
                with pending.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(pending, archive_file)
                descriptor = os.open(archive_file.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        report.update(archive_created=True, archive_path=str(archive_file),
                      archive_entries=len(entries), fidelity_verified=True)
    except (OSError, ValueError, tarfile.TarError) as exc:
        report["errors"].append(str(exc))
    return report


def _read_archive_manifest(archive_file: Path) -> dict[str, object]:
    if not archive_file.exists():
        return {}
    try:
        with tarfile.open(archive_file, "r:gz") as tar:
            try:
                member = tar.getmember(ARCHIVE_MANIFEST_NAME)
            except KeyError:
                return {}
            extracted = tar.extractfile(member)
            if extracted is None:
                return {}
            payload = extracted.read().decode("utf-8")
            data = json.loads(payload)
            return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover - best-effort metadata read
        return {}


def _status_archive_path(status: dict[str, object]) -> str:
    backup_maintenance = status.get("backup_maintenance")
    if not isinstance(backup_maintenance, dict):
        return ""
    archive_report = backup_maintenance.get("archive")
    if not isinstance(archive_report, dict):
        return ""
    return str(archive_report.get("archive_path") or "").strip()


def find_latest_backup_archive(backup_dir: str | Path | None = None) -> Path | None:
    root = Path(backup_dir) if backup_dir is not None else default_backup_archive_dir()
    if not root.exists():
        return None
    candidates: list[Path] = []
    for pattern in ("*.tgz", "*.tar.gz"):
        for path in root.glob(pattern):
            try:
                if path.is_file():
                    candidates.append(path)
            except OSError:
                continue
    if not candidates:
        return None
    best = candidates[0]
    best_key = (best.stat().st_mtime, best.name)
    for path in candidates[1:]:
        try:
            key = (path.stat().st_mtime, path.name)
        except OSError:
            continue
        if key > best_key:
            best = path
            best_key = key
    return best


def _safe_archive_member(member_name: str) -> bool:
    member_path = Path(member_name)
    if member_path.is_absolute():
        return False
    parts = member_path.parts
    return all(part not in {"..", ""} for part in parts)


def _open_restore_root(path_text: str) -> int:
    """Open (and, when needed, create) a restore root without following links."""
    path = Path(path_text)
    if path.is_absolute():
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        parts = path.parts[1:]
    else:
        fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        parts = path.parts
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise ValueError(f"unsafe restore root component: {part!r}")
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_restore_parent(root_fd: int, member_name: str) -> tuple[int, str]:
    """Return a no-follow parent descriptor and leaf for a validated member."""
    parts = Path(member_name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe archive member: {member_name}")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except Exception:
        os.close(fd)
        raise


def _descriptor_path(parent_fd: int, leaf: str) -> Path:
    """Address a verified directory through its live descriptor, not its name."""
    return Path(f"/proc/self/fd/{parent_fd}") / leaf


def _restore_root_from_manifest(manifest: dict[str, object], restore_root: str) -> str:
    restore_root = str(restore_root or "").strip()
    if restore_root:
        return restore_root
    for key in ("backup_root", "archive_base"):
        candidate = str(manifest.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def _path_is_newer(path: Path, member_mtime: float) -> bool:
    try:
        return path.lstat().st_mtime > member_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_existing_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _rename_existing_path(path: Path, suffix: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    stem = f"{path.name}{suffix}-{timestamp}"
    candidate = path.with_name(stem)
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{stem}-{counter}")
        counter += 1
    path.rename(candidate)
    return candidate


def _prompt_conflict_action(
    dest: Path,
    member: tarfile.TarInfo,
    *,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
) -> tuple[str, bool]:
    stream_out = prompt_output or sys.stderr
    stream_in = prompt_input or sys.stdin
    if prompt_input is None and not getattr(stream_in, "isatty", lambda: False)():
        return "skip", False

    relation = "newer" if _path_is_newer(dest, float(member.mtime or 0.0)) else "older-or-equal"
    member_kind = (
        "directory"
        if member.isdir()
        else "symlink"
        if member.issym()
        else "hardlink"
        if member.islnk()
        else "file"
        if member.isfile()
        else "special"
    )
    archive_mtime = datetime.fromtimestamp(
        float(member.mtime or 0.0),
        timezone.utc,
    ).isoformat()
    lines = [
        "Restore conflict",
        f"  destination: {dest}",
        f"  existing path: present; relation to archive={relation}",
        f"  archive member: {member.name}",
        f"  archive type: {member_kind}; size={int(member.size or 0)}; mtime={archive_mtime}",
        "Choices:",
        "  [o] overwrite: remove the existing path, then restore this archive member",
        "      rollback: none for the removed existing path; choose only if it is disposable",
        (
            "  [r] rename: move the existing path to "
            f"{dest.name}.kept-<timestamp>, then restore this archive member"
        ),
        "      rollback: remove the restored member and rename the kept path back",
        "  [s] skip: leave the existing path unchanged",
    ]
    try:
        print("\n".join(lines), file=stream_out)
        print("Choice [s]: ", end="", file=stream_out)
        stream_out.flush()
    except Exception:
        pass
    if not getattr(stream_in, "readline", None):
        return "skip", False
    answer = stream_in.readline().strip().lower()
    if answer in {"o", "overwrite"}:
        action = "overwrite"
    elif answer in {"r", "rename"}:
        action = "rename"
    else:
        return "skip", False

    try:
        print(
            f"Selected {action} for {dest}. Type CONFIRM exactly to continue, "
            "or anything else to skip: ",
            end="",
            file=stream_out,
        )
        stream_out.flush()
    except Exception:
        pass
    confirmation = stream_in.readline().strip() if getattr(stream_in, "readline", None) else ""
    if confirmation != "CONFIRM":
        try:
            print("Restore conflict skipped; the existing path was not changed.", file=stream_out)
        except Exception:
            pass
        return "skip", False

    bulk = False
    try:
        print(
            "Type ALL exactly to apply this confirmed choice to every remaining "
            "conflict, or press Enter for this path only: ",
            end="",
            file=stream_out,
        )
        stream_out.flush()
    except Exception:
        pass
    if getattr(stream_in, "readline", None):
        bulk = stream_in.readline().strip() == "ALL"
    return action, bulk


def _resolve_conflict_action(
    dest: Path,
    member: tarfile.TarInfo,
    *,
    on_conflict: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    conflict_state: dict[str, str],
) -> str:
    if on_conflict == "overwrite":
        return "overwrite"
    if on_conflict == "skip":
        return "skip"
    if on_conflict == "rename":
        return "rename"
    default_action = str(conflict_state.get("default_action") or "").strip()
    if default_action in {"overwrite", "rename", "skip"}:
        return default_action
    action, bulk = _prompt_conflict_action(
        dest,
        member,
        prompt_input=prompt_input,
        prompt_output=prompt_output,
    )
    if bulk:
        conflict_state["default_action"] = action
    return action


def _record_skipped_restore(report: dict[str, object], dest: Path, *, newer: bool) -> None:
    if newer:
        report["skipped_newer_paths"].append(str(dest))
    else:
        report["skipped_conflicts"].append(str(dest))
    report["warnings"].append(f"restore skipped existing path: {dest}")


def _restore_member_metadata(
    member: tarfile.TarInfo,
    dest: Path,
    report: dict[str, object],
    *,
    symlink: bool = False,
) -> None:
    """Restore archive metadata after the member has a stable destination."""
    try:
        os.chown(dest, int(member.uid), int(member.gid), follow_symlinks=not symlink)
    except OSError as exc:
        report["warnings"].append(f"ownership restore failed for {member.name}: {exc}")
    if not symlink:
        try:
            os.chmod(dest, member.mode & 0o7777)
        except OSError as exc:
            report["warnings"].append(f"mode restore failed for {member.name}: {exc}")
        try:
            stamp = int(member.pax_headers.get(fidelity.MTIME_NS, int(float(member.mtime or 0.0) * 1_000_000_000)))
            os.utime(dest, ns=(stamp, stamp))
        except OSError as exc:
            report["warnings"].append(f"timestamp restore failed for {member.name}: {exc}")
    for key, encoded_value in member.pax_headers.items():
        if not key.startswith(_ARCHIVE_XATTR_PREFIX):
            continue
        encoded_name = key[len(_ARCHIVE_XATTR_PREFIX):]
        try:
            name = base64.urlsafe_b64decode(
                f"{encoded_name}{'=' * (-len(encoded_name) % 4)}".encode("ascii")
            ).decode("utf-8", "surrogateescape")
            encoded_value = str(encoded_value)
            value = base64.b64decode(
                f"{encoded_value}{'=' * (-len(encoded_value) % 4)}".encode("ascii"),
                validate=True,
            )
            os.setxattr(dest, name, value, follow_symlinks=not symlink)
        except (OSError, ValueError, UnicodeError) as exc:
            report["warnings"].append(f"xattr restore failed for {member.name}: {exc}")
    if symlink:
        try:
            stamp = int(member.pax_headers.get(fidelity.MTIME_NS, int(float(member.mtime or 0.0) * 1_000_000_000)))
            os.utime(dest, ns=(stamp, stamp), follow_symlinks=False)
        except OSError as exc:
            report["warnings"].append(f"symlink timestamp restore failed for {member.name}: {exc}")


def _restore_regular_file(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    dest: Path,
    display_dest: Path,
    report: dict[str, object],
    *,
    on_conflict: str,
    rename_suffix: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    conflict_state: dict[str, str],
) -> None:
    member_mtime = float(member.mtime or 0.0)
    if _path_present(dest):
        newer = _path_is_newer(dest, member_mtime)
        action = _resolve_conflict_action(
            display_dest,
            member,
            on_conflict=on_conflict,
            prompt_input=prompt_input,
            prompt_output=prompt_output,
            conflict_state=conflict_state,
        )
    else:
        newer = False
        action = "overwrite"
    if action == "skip":
        _record_skipped_restore(report, display_dest, newer=newer)
        return
    if action == "rename":
        renamed = _rename_existing_path(dest, rename_suffix)
        report["renamed_paths"].append(str(display_dest.with_name(renamed.name)))
    elif _path_present(dest):
        _remove_existing_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    extracted = tar.extractfile(member)
    if extracted is None:
        report["warnings"].append(f"restore skipped missing file payload: {member.name}")
        return
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(dir=str(dest.parent), prefix=f".{dest.name}.restore-", delete=False) as handle:
            tmp_name = handle.name
            shutil.copyfileobj(extracted, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, dest)
        _restore_member_metadata(member, dest, report)
        with dest.open("rb") as handle:
            os.fsync(handle.fileno())
        report["restored_entries"] += 1
        report["restored_paths"].append(str(display_dest))
    finally:
        if tmp_name and Path(tmp_name).exists():
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


def _restore_symlink(
    member: tarfile.TarInfo,
    dest: Path,
    display_dest: Path,
    report: dict[str, object],
    *,
    on_conflict: str,
    rename_suffix: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    conflict_state: dict[str, str],
) -> None:
    action = "overwrite"
    if _path_present(dest):
        newer = _path_is_newer(dest, float(member.mtime or 0.0))
        action = _resolve_conflict_action(
            display_dest,
            member,
            on_conflict=on_conflict,
            prompt_input=prompt_input,
            prompt_output=prompt_output,
            conflict_state=conflict_state,
        )
    else:
        newer = False
    if action == "skip":
        _record_skipped_restore(report, display_dest, newer=newer)
        return
    if action == "rename":
        renamed = _rename_existing_path(dest, rename_suffix)
        report["renamed_paths"].append(str(display_dest.with_name(renamed.name)))
    elif _path_present(dest):
        _remove_existing_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(member.linkname, dest)
    _restore_member_metadata(member, dest, report, symlink=True)
    report["restored_entries"] += 1
    report["restored_paths"].append(str(display_dest))


def _restore_hardlink(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    dest: Path,
    display_dest: Path,
    target_root_fd: int,
    report: dict[str, object],
    *,
    on_conflict: str,
    rename_suffix: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    conflict_state: dict[str, str],
) -> None:
    if not _safe_archive_member(member.linkname):
        report["warnings"].append(f"unsafe hardlink target skipped: {member.name} -> {member.linkname}")
        return
    try:
        target_parent_fd, target_leaf = _open_restore_parent(target_root_fd, member.linkname)
    except (OSError, ValueError) as exc:
        report["warnings"].append(f"unsafe hardlink target skipped: {member.name} -> {member.linkname}: {exc}")
        return
    try:
        try:
            target_stat = os.stat(target_leaf, dir_fd=target_parent_fd, follow_symlinks=False)
        except OSError:
            report["warnings"].append(f"hardlink target missing or unsafe; skipped: {member.name} -> {member.linkname}")
            return
        if not stat.S_ISREG(target_stat.st_mode):
            report["warnings"].append(f"hardlink target missing or unsafe; skipped: {member.name} -> {member.linkname}")
            return
        action = "overwrite"
        if _path_present(dest):
            newer = _path_is_newer(dest, float(member.mtime or 0.0))
            action = _resolve_conflict_action(
                display_dest,
                member,
                on_conflict=on_conflict,
                prompt_input=prompt_input,
                prompt_output=prompt_output,
                conflict_state=conflict_state,
            )
        else:
            newer = False
        if action == "skip":
            _record_skipped_restore(report, display_dest, newer=newer)
            return
        if action == "rename":
            renamed = _rename_existing_path(dest, rename_suffix)
            report["renamed_paths"].append(str(display_dest.with_name(renamed.name)))
        if _path_present(dest):
            _remove_existing_path(dest)
        os.link(
            _descriptor_path(target_parent_fd, target_leaf),
            dest,
            follow_symlinks=False,
        )
    finally:
        os.close(target_parent_fd)
    _restore_member_metadata(member, dest, report)
    report["restored_entries"] += 1
    report["restored_paths"].append(str(display_dest))


def _restore_directory(
    member: tarfile.TarInfo,
    dest: Path,
    display_dest: Path,
    report: dict[str, object],
    *,
    on_conflict: str,
    rename_suffix: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    conflict_state: dict[str, str],
    defer_metadata: bool = False,
) -> None:
    member_mtime = float(member.mtime or 0.0)
    action = "overwrite"
    if _path_present(dest):
        newer = _path_is_newer(dest, member_mtime)
        action = _resolve_conflict_action(
            display_dest,
            member,
            on_conflict=on_conflict,
            prompt_input=prompt_input,
            prompt_output=prompt_output,
            conflict_state=conflict_state,
        )
    else:
        newer = False
    if action == "skip":
        _record_skipped_restore(report, display_dest, newer=newer)
        return
    if action == "rename":
        renamed = _rename_existing_path(dest, rename_suffix)
        report["renamed_paths"].append(str(display_dest.with_name(renamed.name)))
    elif _path_present(dest) and (dest.is_symlink() or not dest.is_dir()):
        _remove_existing_path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not defer_metadata:
        _restore_member_metadata(member, dest, report)
    report["restored_entries"] += 1
    report["restored_paths"].append(str(display_dest))


def _empty_restore_report() -> dict[str, object]:
    return {
        "restored_entries": 0,
        "restored_paths": [],
        "renamed_paths": [],
        "skipped_newer_paths": [],
        "skipped_conflicts": [],
        "warnings": [],
        "errors": [],
    }


def _restore_members(
    tar: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    *,
    target_root_text: str,
    display_root: Path,
    report: dict[str, object],
    on_conflict: str,
    rename_suffix: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    conflict_state: dict[str, str],
) -> int:
    """Restore selected, already-authorized archive members below one root."""
    expected_restore_entries = 0
    deferred_directories = []
    root_fd = _open_restore_root(target_root_text)
    try:
        for member in members:
            if not _safe_archive_member(member.name):
                report["warnings"].append(f"unsafe archive member skipped: {member.name}")
                continue
            if not (member.isdir() or member.issym() or member.islnk() or member.isfile()):
                report["warnings"].append(f"unsupported archive member skipped: {member.name}")
                continue
            expected_restore_entries += 1
            display_dest = display_root / member.name
            try:
                parent_fd, leaf = _open_restore_parent(root_fd, member.name)
            except (OSError, ValueError) as exc:
                report["warnings"].append(f"unsafe restore destination skipped: {member.name}: {exc}")
                continue
            try:
                dest = _descriptor_path(parent_fd, leaf)
                if member.isdir():
                    before = report["restored_entries"]
                    _restore_directory(member, dest, display_dest, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state, defer_metadata=True)
                    if report["restored_entries"] > before:
                        deferred_directories.append(member)
                elif member.issym():
                    _restore_symlink(member, dest, display_dest, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
                elif member.islnk():
                    _restore_hardlink(tar, member, dest, display_dest, root_fd, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
                else:
                    _restore_regular_file(tar, member, dest, display_dest, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
            finally:
                os.fsync(parent_fd)
                os.close(parent_fd)
        for member in reversed(deferred_directories):
            parent_fd, leaf = _open_restore_parent(root_fd, member.name)
            try:
                _restore_member_metadata(member, _descriptor_path(parent_fd, leaf), report)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    return expected_restore_entries


def _member_belongs_to_artifact(member_name: str, artifact_member: str) -> bool:
    return member_name == artifact_member or member_name.startswith(f"{artifact_member}/")


def _host_mapped_artifacts(manifest: dict[str, object]) -> tuple[list[dict[str, str]], list[str]]:
    if manifest.get("schema_version") not in {2, 3}:
        return [], []
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        return [], ["host-mapped archive manifest has no artifacts"]
    artifacts: list[dict[str, str]] = []
    errors: list[str] = []
    seen_members: set[str] = set()
    seen_destinations: set[tuple[str, str]] = set()
    for index, raw_artifact in enumerate(raw_artifacts, start=1):
        if not isinstance(raw_artifact, dict):
            errors.append(f"host-mapped artifact {index} is invalid")
            continue
        host = str(raw_artifact.get("host") or "").strip()
        member = str(raw_artifact.get("member") or "").strip()
        destination = str(raw_artifact.get("destination") or "").strip()
        kind = str(raw_artifact.get("kind") or "").strip()
        if not _safe_archive_member(member):
            errors.append(f"host-mapped artifact {index} has unsafe member: {member!r}")
            continue
        if not destination or not Path(destination).is_absolute() or not _safe_archive_member(destination.lstrip("/")):
            errors.append(f"host-mapped artifact {index} has unsafe destination: {destination!r}")
            continue
        destination_key = (host.lower(), destination)
        if member in seen_members or destination_key in seen_destinations:
            errors.append(f"host-mapped artifact {index} duplicates a member or destination")
            continue
        seen_members.add(member)
        seen_destinations.add(destination_key)
        artifacts.append({"host": host, "member": member, "destination": destination, "kind": kind})
    return artifacts, errors


def _rebased_artifact_members(
    members: list[tarfile.TarInfo],
    artifact_member: str,
    destination_name: str,
) -> list[tarfile.TarInfo]:
    rebased: list[tarfile.TarInfo] = []
    for member in members:
        suffix = member.name[len(artifact_member):].lstrip("/")
        rewritten = copy(member)
        rewritten.name = str(Path(destination_name) / suffix) if suffix else destination_name
        if member.islnk():
            if not _member_belongs_to_artifact(member.linkname, artifact_member):
                raise ValueError(
                    f"hardlink target leaves its artifact: {member.name} -> {member.linkname}"
                )
            target_suffix = member.linkname[len(artifact_member):].lstrip("/")
            rewritten.linkname = str(Path(destination_name) / target_suffix) if target_suffix else destination_name
        rebased.append(rewritten)
    return rebased


def _remote_restore_command(host: str, source: Path, destination: str) -> list[str]:
    source_text = str(source)
    destination_text = destination
    if source.is_dir() and not source.is_symlink():
        source_text = f"{source_text.rstrip('/')}/"
        destination_text = f"{destination_text.rstrip('/')}/"
    command = rsync_push_command(host, source_text, destination_text)
    command[2:2] = ["-H", "-A", "-X", "--numeric-ids"]
    return command


def _extend_restore_report(target: dict[str, object], source: dict[str, object]) -> None:
    for key in ("renamed_paths", "skipped_newer_paths", "skipped_conflicts", "warnings", "errors"):
        target[key].extend(source[key])


def _restore_host_mapped_archive(
    archive_file: Path,
    manifest: dict[str, object],
    report: dict[str, object],
    *,
    restore_root: str,
    cleanup_archive: bool,
    on_conflict: str,
    rename_suffix: str,
    prompt_input: TextIO | None,
    prompt_output: TextIO | None,
    preview: bool,
) -> dict[str, object]:
    if manifest.get("schema_version") == 3:
        from .backup_verified_restore import restore_verified_archive
        return restore_verified_archive(archive_file, manifest, report, restore_root=restore_root,
                                        cleanup_archive=cleanup_archive, on_conflict=on_conflict,
                                        rename_suffix=rename_suffix, prompt_input=prompt_input,
                                        prompt_output=prompt_output, preview=preview)
    artifacts, manifest_errors = _host_mapped_artifacts(manifest)
    report["errors"].extend(manifest_errors)
    if restore_root:
        report["errors"].append(
            "host-mapped archives restore only to their recorded destinations; --restore-root is for legacy archives"
        )
    if report["errors"]:
        report["restore_complete"] = False
        report["archive_removed"] = False
        return report

    try:
        with tarfile.open(archive_file, "r:gz") as tar:
            archive_members = [member for member in tar.getmembers() if member.name != ARCHIVE_MANIFEST_NAME]
            artifact_members: list[tuple[dict[str, str], list[tarfile.TarInfo]]] = []
            claimed_members: set[str] = set()
            for artifact in artifacts:
                selected = [
                    member for member in archive_members
                    if _member_belongs_to_artifact(member.name, artifact["member"])
                ]
                if not selected:
                    report["errors"].append(
                        f"archived artifact is absent: {artifact['host']}:{artifact['member']}"
                    )
                    continue
                for member in selected:
                    if member.name in claimed_members:
                        report["errors"].append(f"archive member is mapped more than once: {member.name}")
                    claimed_members.add(member.name)
                artifact_members.append((artifact, selected))
            unclaimed = [member.name for member in archive_members if member.name not in claimed_members]
            if unclaimed:
                report["errors"].append(f"archive contains members without a host mapping: {', '.join(unclaimed[:3])}")
            if report["errors"]:
                report["restore_complete"] = False
                report["archive_removed"] = False
                return report

            for artifact, selected in artifact_members:
                is_local = not artifact["host"] or artifact["host"].lower() in {"localhost", "127.0.0.1", "::1"}
                operation = {
                    "host": artifact["host"],
                    "member": artifact["member"],
                    "destination": artifact["destination"],
                    "operation": "local-extract" if is_local else "remote-rsync",
                }
                report["planned_restores"].append(operation)
            if preview:
                for operation in report["planned_restores"]:
                    if operation["operation"] == "remote-rsync":
                        source = Path("<archive-stage>") / str(operation["member"])
                        operation["command"] = _remote_restore_command(str(operation["host"]), source, str(operation["destination"]))
                report["restore_complete"] = False
                report["archive_removed"] = False
                if cleanup_archive:
                    report["warnings"].append("archive retained because preview does not restore entries")
                return report

            with tempfile.TemporaryDirectory(prefix="gluster-backup-restore-") as tmp:
                stage_root = Path(tmp)
                conflict_state: dict[str, str] = {}
                for artifact, selected in artifact_members:
                    host = artifact["host"]
                    destination = artifact["destination"]
                    is_local = not host or host.lower() in {"localhost", "127.0.0.1", "::1"}
                    if is_local:
                        try:
                            rebased = _rebased_artifact_members(selected, artifact["member"], Path(destination).name)
                            expected = _restore_members(
                                tar,
                                rebased,
                                target_root_text=str(Path(destination).parent),
                                display_root=Path(destination).parent,
                                report=report,
                                on_conflict=on_conflict,
                                rename_suffix=rename_suffix,
                                prompt_input=prompt_input,
                                prompt_output=prompt_output,
                                conflict_state=conflict_state,
                            )
                            report["expected_restore_entries"] += expected
                        except (OSError, ValueError) as exc:
                            report["errors"].append(f"could not restore local artifact {destination}: {exc}")
                        continue

                    if on_conflict != "overwrite":
                        report["errors"].append(
                            f"remote artifact requires --on-conflict overwrite: {host}:{destination}"
                        )
                        continue
                    stage_report = _empty_restore_report()
                    try:
                        expected = _restore_members(
                            tar,
                            selected,
                            target_root_text=str(stage_root),
                            display_root=stage_root,
                            report=stage_report,
                            on_conflict="overwrite",
                            rename_suffix=rename_suffix,
                            prompt_input=None,
                            prompt_output=None,
                            conflict_state={},
                        )
                    except (OSError, ValueError) as exc:
                        report["errors"].append(f"could not stage remote artifact {host}:{destination}: {exc}")
                        continue
                    _extend_restore_report(report, stage_report)
                    if stage_report["warnings"] or stage_report["errors"] or int(stage_report["restored_entries"]) != expected:
                        report["errors"].append(f"remote artifact staging was incomplete: {host}:{destination}")
                        continue
                    source = stage_root / artifact["member"]
                    command = _remote_restore_command(host, source, destination)
                    operation = next(
                        item for item in report["planned_restores"]
                        if item["host"] == host and item["member"] == artifact["member"]
                    )
                    operation["command"] = command
                    completed = subprocess.run(command, capture_output=True, text=True, check=False)
                    if completed.returncode:
                        detail = (completed.stderr or completed.stdout).strip() or f"exit {completed.returncode}"
                        report["errors"].append(f"remote restore failed for {host}:{destination}: {detail}")
                        continue
                    report["expected_restore_entries"] += expected
                    report["restored_entries"] += stage_report["restored_entries"]
                    report["restored_paths"].append(f"{host}:{destination}")
    except Exception as exc:  # pragma: no cover - best-effort restore
        report["errors"].append(str(exc))

    restore_complete = (
        not report["errors"]
        and not report["warnings"]
        and not report["skipped_newer_paths"]
        and not report["skipped_conflicts"]
        and int(report["restored_entries"]) == int(report["expected_restore_entries"])
    )
    report["restore_complete"] = restore_complete
    if cleanup_archive and restore_complete:
        try:
            archive_file.unlink()
            report["archive_removed"] = True
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            report["warnings"].append(f"archive cleanup failed: {exc}")
            report["archive_removed"] = False
    elif cleanup_archive:
        report["warnings"].append(
            "archive retained because restoration was incomplete; retry restore with the same archive after resolving reported entries"
        )
        report["archive_removed"] = False
    else:
        report["archive_removed"] = False
    return report


def restore_backup_archive(
    archive_path: str | Path = "",
    *,
    restore_root: str = "",
    cleanup_archive: bool = False,
    on_conflict: str = "prompt",
    rename_suffix: str = ".restore",
    prompt_input: TextIO | None = None,
    prompt_output: TextIO | None = None,
    status_archive_path: str = "",
    backup_dir: str | Path | None = None,
    preview: bool = False,
) -> dict[str, object]:
    archive_file: Path | None = None
    archive_source = ""
    archive_text = str(archive_path or "").strip()
    status_text = str(status_archive_path or "").strip()
    if archive_text:
        archive_file = Path(archive_text)
        archive_source = "explicit"
    elif status_text:
        candidate = Path(status_text)
        if candidate.exists():
            archive_file = candidate
            archive_source = "status"
    if archive_file is None:
        archive_file = find_latest_backup_archive(backup_dir)
        if archive_file is not None:
            archive_source = "latest"
    manifest = _read_archive_manifest(archive_file) if archive_file else {}
    target_root_text = _restore_root_from_manifest(manifest, restore_root)
    report = {
        "available": bool(archive_file and archive_file.exists()),
        "archive_path": str(archive_file) if archive_file else "",
        "archive_source": archive_source,
        "archive_manifest": manifest,
        "restore_root": target_root_text,
        "preview": preview,
        "planned_restores": [],
        "expected_restore_entries": 0,
        "restored_entries": 0,
        "restored_paths": [],
        "renamed_paths": [],
        "skipped_newer_paths": [],
        "skipped_conflicts": [],
        "warnings": [],
        "errors": [],
        "cleanup_archive": cleanup_archive,
        "on_conflict": on_conflict,
        "rename_suffix": rename_suffix,
    }
    conflict_state: dict[str, str] = {}
    if archive_file is None or not archive_file.exists():
        report["errors"].append("archive not found; pass --archive or keep a recent backup archive in place")
        return report
    if cleanup_archive and manifest.get("schema_version") != 3:
        report["warnings"].append("legacy archive retained because it has no verified fidelity contract")
        cleanup_archive = False
    if manifest.get("schema_version") in {2, 3}:
        return _restore_host_mapped_archive(
            archive_file,
            manifest,
            report,
            restore_root=restore_root,
            cleanup_archive=cleanup_archive,
            on_conflict=on_conflict,
            rename_suffix=rename_suffix,
            prompt_input=prompt_input,
            prompt_output=prompt_output,
            preview=preview,
        )
    if not target_root_text:
        report["errors"].append("restore root is unknown; pass --restore-root or use an archive with metadata")
        return report
    target_root = Path(target_root_text)
    if preview:
        try:
            with tarfile.open(archive_file, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name == ARCHIVE_MANIFEST_NAME:
                        continue
                    if not _safe_archive_member(member.name):
                        report["warnings"].append(f"unsafe archive member skipped: {member.name}")
                        continue
                    if not (member.isdir() or member.issym() or member.islnk() or member.isfile()):
                        report["warnings"].append(f"unsupported archive member skipped: {member.name}")
                        continue
                    report["expected_restore_entries"] += 1
                    report["planned_restores"].append(
                        {
                            "host": "",
                            "member": member.name,
                            "destination": str(target_root / member.name),
                            "operation": "local-extract",
                        }
                    )
        except Exception as exc:  # pragma: no cover - best-effort archive inspection
            report["errors"].append(str(exc))
        report["restore_complete"] = False
        report["archive_removed"] = False
        if cleanup_archive:
            report["warnings"].append("archive retained because preview does not restore entries")
        return report
    expected_restore_entries = 0
    try:
        root_fd = _open_restore_root(target_root_text)
        try:
            with tarfile.open(archive_file, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name == ARCHIVE_MANIFEST_NAME:
                        continue
                    if not _safe_archive_member(member.name):
                        report["warnings"].append(f"unsafe archive member skipped: {member.name}")
                        continue
                    if not (member.isdir() or member.issym() or member.islnk() or member.isfile()):
                        report["warnings"].append(f"unsupported archive member skipped: {member.name}")
                        continue
                    expected_restore_entries += 1
                    display_dest = target_root / member.name
                    try:
                        parent_fd, leaf = _open_restore_parent(root_fd, member.name)
                    except (OSError, ValueError) as exc:
                        report["warnings"].append(f"unsafe restore destination skipped: {member.name}: {exc}")
                        continue
                    try:
                        dest = _descriptor_path(parent_fd, leaf)
                        if member.isdir():
                            _restore_directory(member, dest, display_dest, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
                        elif member.issym():
                            _restore_symlink(member, dest, display_dest, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
                        elif member.islnk():
                            _restore_hardlink(tar, member, dest, display_dest, root_fd, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
                        else:
                            _restore_regular_file(tar, member, dest, display_dest, report, on_conflict=on_conflict, rename_suffix=rename_suffix, prompt_input=prompt_input, prompt_output=prompt_output, conflict_state=conflict_state)
                    finally:
                        os.close(parent_fd)
        finally:
            os.close(root_fd)
    except Exception as exc:  # pragma: no cover - best-effort restore
        report["errors"].append(str(exc))
        return report

    restore_complete = (
        not report["errors"]
        and not report["warnings"]
        and not report["skipped_newer_paths"]
        and not report["skipped_conflicts"]
        and int(report["restored_entries"]) == expected_restore_entries
    )
    report["expected_restore_entries"] = expected_restore_entries
    report["restore_complete"] = restore_complete
    if cleanup_archive and restore_complete:
        try:
            archive_file.unlink()
            report["archive_removed"] = True
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            report["warnings"].append(f"archive cleanup failed: {exc}")
            report["archive_removed"] = False
    elif cleanup_archive:
        report["warnings"].append(
            "archive retained because restoration was incomplete; retry restore with the same archive after resolving reported entries"
        )
        report["archive_removed"] = False
    else:
        report["archive_removed"] = False
    return report


def cleanup_backup_artifacts(results: list[ApplyActionResult], *, archive_path: str | Path = "") -> dict[str, object]:
    artifacts = collect_backup_artifacts(results)
    paths = _collapse_paths([Path(artifact.backup_path) for artifact in artifacts if _is_local_artifact(artifact)])
    backup_root = _result_backup_root(results)
    stop_base = _result_backup_base(results, paths)
    report = {
        "available": bool(artifacts),
        "cleanup_completed": False,
        "removed_paths": [],
        "missing_paths": [],
        "pruned_dirs": [],
        "warnings": [],
        "errors": [],
        "paths": [f"{artifact.host}:{artifact.backup_path}" for artifact in artifacts],
    }
    if not artifacts:
        report["warnings"].append("no backup artifacts found to clean up")
        return report

    report["missing_paths"] = [str(path) for path in paths if not _path_present(path)]
    if report["missing_paths"]:
        report["errors"].append("cleanup verification found missing local artifacts; remaining originals retained")
        return report

    if not archive_path:
        report["errors"].append("cleanup requires a verified archive; originals retained")
        return report
    try:
        from .backup_verified_restore import verify_cleanup_sources
        verify_cleanup_sources(results, archive_path)
    except (OSError, ValueError, tarfile.TarError) as exc:
        report["errors"].append(f"cleanup verification failed: {exc}")
        return report

    # Remove the selected roots deepest-first so nested backup trees disappear cleanly.
    for path in sorted(paths, key=lambda item: (-len(item.parts), str(item))):
        try:
            if not _path_present(path):
                report["missing_paths"].append(str(path))
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            report["removed_paths"].append(str(path))
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            report["errors"].append(f"{path}: {exc}")

    for artifact in artifacts:
        if _is_local_artifact(artifact):
            continue
        error = _remove_remote_artifact(artifact)
        label = f"{artifact.host}:{artifact.backup_path}"
        if error:
            report["errors"].append(f"{label}: {error}")
        else:
            report["removed_paths"].append(label)

    # Prune empty parents up to the shared backup base when we can identify one.
    candidate_parents = sorted(
        {str(path.parent) for path in paths},
        key=lambda value: (-len(Path(value).parts), value),
    )
    for parent_text in candidate_parents:
        current = Path(parent_text)
        try:
            current.relative_to(stop_base)
        except ValueError:
            continue
        while current != stop_base and current != current.parent:
            try:
                current.rmdir()
                report["pruned_dirs"].append(str(current))
            except OSError:
                break
            current = current.parent

    report["cleanup_completed"] = not report["errors"] and not report["missing_paths"]
    return report


def manage_backup_artifacts(
    results: list[ApplyActionResult],
    *,
    archive_path: str = "",
    cleanup: bool = False,
) -> dict[str, object]:
    report = {
        "requested_archive_path": archive_path,
        "cleanup_requested": cleanup,
        "archive": {},
        "cleanup": {},
        "archive_then_cleanup": bool(archive_path and cleanup),
        "warnings": [],
        "errors": [],
    }
    if archive_path:
        archive_report = archive_backup_artifacts(results, archive_path)
        report["archive"] = archive_report
        report["warnings"].extend(archive_report.get("warnings", []))
        report["errors"].extend(archive_report.get("errors", []))
        if cleanup and archive_report.get("errors"):
            report["cleanup"] = {
                "cleanup_completed": False,
                "warnings": ["cleanup skipped because archive creation failed"],
                "errors": [],
                "paths": [],
            }
            return report
    if cleanup:
        cleanup_report = cleanup_backup_artifacts(results, archive_path=archive_path)
        report["cleanup"] = cleanup_report
        report["warnings"].extend(cleanup_report.get("warnings", []))
        report["errors"].extend(cleanup_report.get("errors", []))
    return report
