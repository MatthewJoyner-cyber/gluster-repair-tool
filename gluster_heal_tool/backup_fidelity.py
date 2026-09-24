# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Backup capture and verification contracts, independent of repair policy."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile

from .remote_ops import rsync_pull_command, rsync_push_command


XATTR_PREFIX = "GLUSTER.repair.xattr."
MTIME_NS = "GLUSTER.repair.mtime_ns"
# ACLs use -A; the explicit xattr filter admits other namespaces instead of
# silently dropping every system.* attribute under rsync's default filter.
TRANSFER_FLAGS = ["-H", "-A", "-X", "--numeric-ids", "--fake-super", "--fsync",
                  "--filter=-x system.posix_acl_*"]


def _digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _xattrs(path):
    return {base64.urlsafe_b64encode(name.encode("utf-8", "surrogateescape")).decode().rstrip("="):
            base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode()
            for name in os.listxattr(path, follow_symlinks=False)}


def snapshot(paths):
    """Capture every selected member; unreadable metadata is a hard failure."""
    result, inodes = {}, {}
    def visit(path, member):
        if member in result:
            raise ValueError(f"overlapping backup member: {member}")
        info = path.lstat()
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "unsupported"
        if kind == "unsupported":
            raise ValueError(f"unsupported backup object: {path}")
        row = dict(kind=kind, mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid,
                   mtime_ns=info.st_mtime_ns, xattrs=_xattrs(path))
        result[member] = row
        if kind == "file":
            with path.open("rb") as stream:
                row["sha256"] = _digest(stream)
            row["size"] = info.st_size
            inodes.setdefault((info.st_dev, info.st_ino), []).append(member)
        elif kind == "symlink":
            row["linkname"] = os.readlink(path)
        else:
            for child in sorted(path.iterdir()):
                visit(child, f"{member}/{child.name}")
    for path, member in paths:
        visit(Path(path), member)
    for members in inodes.values():
        for member in members:
            result[member]["hardlinks"] = sorted(members)
    return result


def verify_tar(archive, expected, manifest_name):
    """Check bytes, metadata and the complete in-scope hardlink graph."""
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != manifest_name]
        if len(members) != len(expected) or {m.name for m in members} != set(expected):
            raise ValueError("archive member inventory differs from captured backup")
        by_name = {m.name:m for m in members}
        for member in members:
            row = expected[member.name]
            kind = "directory" if member.isdir() else "symlink" if member.issym() else "file" if member.isfile() or member.islnk() else "unsupported"
            actual = dict(kind=kind, mode=member.mode, uid=member.uid, gid=member.gid,
                          mtime_ns=int(member.pax_headers.get(MTIME_NS, -1)),
                          xattrs={key[len(XATTR_PREFIX):]:value for key,value in member.pax_headers.items() if key.startswith(XATTR_PREFIX)})
            if any(actual[key] != row[key] for key in actual):
                raise ValueError(f"archive metadata differs: {member.name}")
            if kind == "symlink" and member.linkname != row["linkname"]:
                raise ValueError(f"archive symlink differs: {member.name}")
            if kind == "file":
                canonical = member.linkname if member.islnk() else member.name
                if canonical not in row["hardlinks"] or canonical not in by_name or not by_name[canonical].isfile():
                    raise ValueError(f"archive hardlink target differs: {member.name}")
                group = sorted(m.name for m in members if m.name == canonical or m.islnk() and m.linkname == canonical)
                if group != row["hardlinks"]:
                    raise ValueError(f"archive hardlink group differs: {member.name}")
                stream = tar.extractfile(member)
                if stream is None or _digest(stream) != row["sha256"] or by_name[canonical].size != row["size"]:
                    raise ValueError(f"archive content differs: {member.name}")


def safe_host(host):
    if not host or host in {".", ".."} or any(c in host for c in "/\\\r\n\0"):
        raise ValueError("invalid backup host label")
    return host


def file_list(paths, output):
    names = []
    for path in paths:
        parsed = PurePosixPath(path)
        if not parsed.is_absolute() or str(parsed) == "/" or ".." in parsed.parts or "\0" in path:
            raise ValueError(f"backup path must be absolute and bounded: {path}")
        names.append(str(parsed).lstrip("/"))
    Path(output).write_bytes(b"".join(os.fsencode(name) + b"\0" for name in sorted(set(names))))


def transfer_group_command(host, paths, stage_root, list_path, *, push=False, verify=False):
    file_list(paths, list_path)
    stage = str(Path(stage_root) / safe_host(host)) + "/"
    command = rsync_push_command(host, stage, "/") if push else rsync_pull_command(host, "/", stage)
    command[2:2] = [*TRANSFER_FLAGS, "-r", "--relative", "--no-implied-dirs", "--from0", "--files-from", str(list_path)]
    if verify:
        command[2:2] = ["--dry-run", "--checksum", "--itemize-changes", "--modify-window=-1"]
        if not push:
            # A read-back comparison must also report staged children missing
            # remotely. Dry-run makes deletion reporting non-mutating.
            command.insert(2, "--delete")
    return command


def run_transfer(command, *, verify=False):
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    if completed.returncode or (completed.stderr or "").strip() or verify and detail:
        raise ValueError(f"backup {'verification' if verify else 'transfer'} failed: {detail or completed.returncode}")


def stage_remote_group(artifacts, stage_root):
    host = safe_host(artifacts[0].host)
    if any(artifact.host != host for artifact in artifacts):
        raise ValueError("backup transfer cannot mix hosts")
    (Path(stage_root) / host).mkdir(parents=True, exist_ok=True)
    paths = [artifact.backup_path for artifact in artifacts]
    list_path = Path(stage_root) / f".{host}.files"
    run_transfer(transfer_group_command(host, paths, stage_root, list_path))
    run_transfer(transfer_group_command(host, paths, stage_root, list_path, verify=True), verify=True)
    return [(artifact, Path(stage_root) / host / artifact.backup_path.lstrip("/")) for artifact in artifacts]


def verify_remote_group(host, paths, stage_root, *, push=False):
    list_path = Path(stage_root) / f".{safe_host(host)}.verify-files"
    run_transfer(transfer_group_command(host, paths, stage_root, list_path, push=push, verify=True), verify=True)
