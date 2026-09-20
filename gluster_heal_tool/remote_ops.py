# SPDX-License-Identifier: GPL-2.0-only
"""Helpers for remote service-user execution."""
from __future__ import annotations

import shlex
import subprocess

from .install_paths import DEFAULT_HOST_OPS_PATH, DEFAULT_SERVICE_KEY_PATH, DEFAULT_SERVICE_USER
from .ssh_identity import ssh_identity_options

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]


def _ssh_transport_options(private_key_path: str | None = None) -> list[str]:
    if private_key_path:
        return ["-i", private_key_path, "-o", "IdentitiesOnly=yes"]
    return ssh_identity_options()


def _ssh_transport(private_key_path: str | None = None) -> str:
    return subprocess.list2cmdline(["ssh", *_ssh_transport_options(private_key_path), *SSH_OPTIONS])


SSH_TRANSPORT = _ssh_transport()
SERVICE_SSH_TRANSPORT = _ssh_transport(str(DEFAULT_SERVICE_KEY_PATH))


def _format_connect_timeout(connect_timeout: int | float | None) -> str | None:
    if connect_timeout is None:
        return None
    if float(connect_timeout).is_integer():
        return str(int(connect_timeout))
    return str(connect_timeout)


def _remote_prefix(*, use_sudo: bool, sudo_path: str | None = None) -> list[str]:
    if use_sudo:
        return ["sudo", "-n", str(sudo_path or DEFAULT_HOST_OPS_PATH)]
    return []


def ssh_remote_command(
    host: str,
    command: list[str],
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    use_sudo: bool = True,
    sudo_path: str | None = None,
    connect_timeout: int | float | None = None,
) -> list[str]:
    ssh_opts = [*ssh_identity_options(), *SSH_OPTIONS]
    timeout_value = _format_connect_timeout(connect_timeout)
    if timeout_value is not None:
        ssh_opts.extend(["-o", f"ConnectTimeout={timeout_value}"])
    remote_command = shlex.join([*_remote_prefix(use_sudo=use_sudo, sudo_path=sudo_path), *command])
    return [
        "ssh",
        *ssh_opts,
        f"{ssh_user}@{host}",
        remote_command,
    ]


def rsync_pull_command(
    host: str,
    source: str,
    target: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    use_sudo: bool = True,
    delete: bool = False,
    ssh_transport: str | None = None,
) -> list[str]:
    rsync_path = " ".join(["sudo", "-n", str(DEFAULT_HOST_OPS_PATH), "rsync-server"]) if use_sudo else "rsync"
    command = [
        "rsync",
        "-a",
    ]
    if delete:
        command.append("--delete")
    command.extend(
        [
            "-e",
            ssh_transport or SSH_TRANSPORT,
            "--rsync-path",
            rsync_path,
            f"{ssh_user}@{host}:{source}",
            target,
        ]
    )
    return command


def rsync_push_command(
    host: str,
    source: str,
    target: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    use_sudo: bool = True,
    delete: bool = False,
    ssh_transport: str | None = None,
) -> list[str]:
    rsync_path = " ".join(["sudo", "-n", str(DEFAULT_HOST_OPS_PATH), "rsync-server"]) if use_sudo else "rsync"
    command = [
        "rsync",
        "-a",
    ]
    if delete:
        command.append("--delete")
    command.extend(
        [
            "-e",
            ssh_transport or SSH_TRANSPORT,
            "--rsync-path",
            rsync_path,
            source,
            f"{ssh_user}@{host}:{target}",
        ]
    )
    return command


def rsync_brick_pull_command(
    target_host: str,
    source_host: str,
    source: str,
    target: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    use_sudo: bool = True,
    delete: bool = False,
) -> list[str]:
    if delete:
        raise ValueError("brick-side rsync pull does not support --delete")
    # Run the rsync client through host-ops on the target so temporary files and
    # the final backend write have brick privileges; SSH remains service-key-only.
    command = ["rsync-pull", source_host, source, target]
    return ssh_remote_command(
        target_host,
        command,
        ssh_user=ssh_user,
        use_sudo=use_sudo,
    )
