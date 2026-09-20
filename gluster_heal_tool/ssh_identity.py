# SPDX-License-Identifier: GPL-2.0-only
"""Local SSH identity helpers."""
from __future__ import annotations

import os
import pwd
from pathlib import Path


DEFAULT_PRIVATE_KEY_NAME = "id_ed25519"
DEFAULT_PUBLIC_KEY_NAME = "id_ed25519.pub"
SSH_KEY_ENV = "GLUSTER_REPAIR_SSH_KEY"


def _operator_user() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"


def _operator_home() -> Path:
    override_home = os.environ.get("HOME", "").strip()
    try:
        return Path(pwd.getpwnam(_operator_user()).pw_dir)
    except KeyError:
        return Path(override_home or "/root")


def default_private_key_path() -> Path:
    override = os.environ.get(SSH_KEY_ENV, "").strip()
    if override:
        return Path(override)
    return _operator_home() / ".ssh" / DEFAULT_PRIVATE_KEY_NAME


def default_public_key_path() -> Path:
    return default_private_key_path().with_name(DEFAULT_PUBLIC_KEY_NAME)


def ssh_identity_options() -> list[str]:
    key_path = default_private_key_path()
    if key_path.exists():
        return ["-x", "-i", str(key_path), "-o", "IdentitiesOnly=yes"]
    return []
