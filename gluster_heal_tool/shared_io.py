# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Shared artifact write helpers.

These outputs are intentionally shared between sudo-driven and non-sudo-driven
parts of the toolchain. When a file is written through sudo, ownership is
returned to the invoking user so follow-up non-sudo steps can keep using it,
and the shared service group keeps group-write access.
"""
from __future__ import annotations

import json
import os
import fcntl
import tempfile
from contextlib import contextmanager
import pwd
from pathlib import Path
from typing import Any

from .install_paths import DEFAULT_SERVICE_USER


def _shared_group_gid() -> int | None:
    try:
        return pwd.getpwnam(DEFAULT_SERVICE_USER).pw_gid
    except KeyError:
        return None


def _invoking_uid_gid() -> tuple[int, int]:
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            return int(sudo_uid), int(sudo_gid)
        except ValueError:
            pass
    return os.geteuid(), os.getegid()


def _restore_owner(path: str | Path) -> None:
    target_uid, target_gid = _invoking_uid_gid()
    try:
        os.chown(path, target_uid, target_gid)
    except OSError:
        # Keep the freshly written artifact even if ownership restoration fails.
        pass
    shared_gid = _shared_group_gid()
    if shared_gid is not None and shared_gid != target_gid:
        try:
            os.chown(path, target_uid, shared_gid)
        except OSError:
            # Keep the artifact even if we cannot re-home it into the shared group.
            pass
    try:
        os.chmod(path, 0o664)
    except OSError:
        # Keep the artifact even if mode tightening fails.
        pass


def write_text_shared(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        _restore_owner(target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def shared_exclusive_lock(path: str | Path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o664)
    handle = os.fdopen(fd, "a+")
    try:
        _restore_owner(lock_path)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def write_json_shared(path: str | Path, payload: dict[str, Any]) -> None:
    write_text_shared(path, json.dumps(payload, indent=2) + "\n")
