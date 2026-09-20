# SPDX-License-Identifier: GPL-2.0-only
"""Shared helper surface for batched gtest canaries."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .install_paths import DEFAULT_SERVICE_USER
from .install_paths import DEFAULT_WORKER_PATH
from .protocol import CanaryBatchRequest
from .remote_ops import ssh_remote_command
from .shared_io import write_json_shared
from .volume import discover_brick_paths
from .volume import parse_bricks
from .volume import parse_brick_paths
from .volume import parse_brick_roles
from .volume import parse_common_brick_path
from .volume import parse_volume_type
from .volume import normalize_host_alias


def _new_gfid_hex() -> str:
    return uuid.uuid4().hex


def _volume_mount_root(volume: str) -> str:
    return f"/{volume.strip().strip('/')}"


def _run_local(command: list[str]) -> None:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"command failed: {subprocess.list2cmdline(command)}")


def _require_successful_worker_response(label: str, response: dict[str, Any]) -> None:
    failed = [
        item for item in response.get("results", [])
        if item.get("error") or not item.get("ok", False)
    ]
    if failed:
        detail = "; ".join(str(item.get("error") or "operation did not report success") for item in failed)
        raise RuntimeError(f"{label} failed: {detail}")


def _local_setfacl(path: str, acl_text: str) -> None:
    if not path or not acl_text:
        raise ValueError("missing path or ACL text")
    proc = subprocess.run(
        ["sudo", "-n", "setfacl", "--set-file=-", "--", path],
        input=acl_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "setfacl failed")


def _volume_info_text(volume: str) -> str:
    proc = subprocess.run(
        ["sudo", "-n", "gluster", "volume", "info", volume],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to read gluster volume info for {volume}")
    return proc.stdout


def _brick_hosts_and_root(volume: str) -> tuple[list[str], str]:
    info = _volume_info_text(volume)
    if parse_volume_type(info) != "Replicate":
        raise RuntimeError(
            f"unsupported volume type for this canary helper: {volume!r} is {parse_volume_type(info) or 'unknown'}; only pure Replicate volumes are supported"
        )
    bricks = parse_bricks(info)
    hosts: list[str] = []
    for host, _path in bricks:
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        raise RuntimeError(f"no brick hosts found for volume {volume}")
    return hosts, parse_common_brick_path(info)


def _brick_hosts_and_paths(volume: str) -> tuple[list[str], dict[str, str]]:
    info = _volume_info_text(volume)
    if parse_volume_type(info) != "Replicate":
        raise RuntimeError(
            f"unsupported volume type for this canary helper: {volume!r} is {parse_volume_type(info) or 'unknown'}; only pure Replicate volumes are supported"
        )
    bricks = parse_bricks(info)
    hosts: list[str] = []
    for host, _path in bricks:
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        raise RuntimeError(f"no brick hosts found for volume {volume}")
    return hosts, parse_brick_paths(info)


def _brick_roles(volume: str) -> dict[str, str]:
    info = _volume_info_text(volume)
    if parse_volume_type(info) != "Replicate":
        raise RuntimeError(
            f"unsupported volume type for this canary helper: {volume!r} is {parse_volume_type(info) or 'unknown'}; only pure Replicate volumes are supported"
        )
    roles = parse_brick_roles(info)
    if not roles:
        raise RuntimeError(f"no brick roles found for volume {volume}")
    return roles


def _normalize_brick_roles_by_host(brick_roles_by_host: Any) -> dict[str, str]:
    if not isinstance(brick_roles_by_host, dict) or not brick_roles_by_host:
        return {}
    normalized: dict[str, str] = {}
    for host, role in brick_roles_by_host.items():
        host_text = str(host).strip()
        role_text = str(role).strip()
        if host_text and role_text:
            normalized[host_text] = role_text
    return normalized


def _brick_roles_by_host(volume: str, brick_roles_by_host: Any = None) -> dict[str, str]:
    normalized = _normalize_brick_roles_by_host(brick_roles_by_host)
    if normalized:
        return normalized
    return _brick_roles(volume)


def _backend_root_for_host(volume: str, host: str) -> str:
    paths = discover_brick_paths(volume)
    host_key = normalize_host_alias(host)
    if host_key not in paths:
        raise RuntimeError(f"brick host {host} is not part of the volume")
    return paths[host_key]


def _select_role_host(
    brick_hosts: list[str],
    preferred_host: str | None,
    *,
    role: str,
    default_to_next: bool = False,
) -> str:
    if preferred_host:
        if preferred_host not in brick_hosts:
            raise RuntimeError(f"{role} host {preferred_host} is not part of the volume")
        return preferred_host
    if not brick_hosts:
        raise RuntimeError(f"need at least one brick host to choose a {role} host")
    if default_to_next and len(brick_hosts) > 1:
        return brick_hosts[1]
    return brick_hosts[0]


def _state_root() -> Path:
    override = os.environ.get("GLUSTER_GTEST_CANARY_STATE_ROOT", "").strip()
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "gluster-repair" / "gtest-canary"
    return Path.home() / ".local" / "state" / "gluster-repair" / "gtest-canary"


def _state_path(volume: str, scenario: str) -> Path:
    return _state_root() / volume / f"{scenario}.json"


def _write_state(volume: str, scenario: str, payload: dict[str, Any]) -> None:
    path = _state_path(volume, scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_shared(path, payload)


def _read_state(volume: str, scenario: str) -> dict[str, Any]:
    path = _state_path(volume, scenario)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _delete_state(volume: str, scenario: str) -> None:
    path = _state_path(volume, scenario)
    if path.exists():
        path.unlink()
    log_path = path.with_suffix(".log")
    if log_path.exists():
        log_path.unlink()
    parent = path.parent
    while parent != _state_root() and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _canary_worker_command(
    host: str,
    *,
    ssh_user: str,
    worker_path: str,
    request: CanaryBatchRequest,
    connect_timeout: int = 10,
) -> dict[str, Any]:
    proc = subprocess.run(
        ssh_remote_command(
            host,
            ["canary-batch"],
            ssh_user=ssh_user,
            use_sudo=True,
            sudo_path=worker_path,
            connect_timeout=connect_timeout,
        ),
        input=json.dumps(request.to_dict()),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"canary worker failed on {host}: exit {proc.returncode}")
    return json.loads(proc.stdout or "{}")


def _local_mount_dir(path: str) -> None:
    _run_local(["sudo", "-n", "mkdir", "-p", "--", path])


def _local_mount_file(path: str) -> None:
    _run_local(["sudo", "-n", "touch", "--", path])


def _local_write_file(path: str, text: str) -> None:
    _run_local(["sudo", "-n", "bash", "-lc", f"printf '%s\\n' {shlex.quote(text)} > {shlex.quote(path)}"])


def _is_transport_endpoint_disconnect_error(exc: Exception | str) -> bool:
    message = str(exc)
    return "Transport endpoint is not connected" in message or "ENOTCONN" in message


def _mountpoint_options(path: str) -> set[str]:
    if not path:
        return set()
    commands = [
        ["findmnt", "-T", path, "-no", "OPTIONS"],
        ["sudo", "-n", "findmnt", "-T", path, "-no", "OPTIONS"],
    ]
    for command in commands:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            continue
        options = (proc.stdout or "").strip()
        if options:
            return {item.strip().lower() for item in options.split(",") if item.strip()}
    return set()


def _mountpoint_supports_acl(path: str) -> bool:
    return "acl" in _mountpoint_options(path)


def _local_symlink(link_path: str, target_path: str) -> None:
    _run_local(
        [
            "sudo",
            "-n",
            "bash",
            "-lc",
            f"ln -sfn {shlex.quote(target_path)} {shlex.quote(link_path)}",
        ]
    )


def _local_remove(path: str) -> None:
    _run_local(["sudo", "-n", "rm", "-rf", "--", path])


def _make_request(volume: str, scenario: str, backend_root: str, ops: list[dict[str, Any]]) -> CanaryBatchRequest:
    return CanaryBatchRequest(volume=volume, scenario=scenario, backend_root=backend_root, ops=ops)


def _file_gfid_link_path(backend_root: str, gfid_uuid: str) -> str:
    compact = gfid_uuid.replace("-", "")
    return f"{backend_root}/.glusterfs/{compact[:2]}/{compact[2:4]}/{gfid_uuid}"


def _directory_gfid_link_path(backend_root: str, gfid_uuid: str) -> str:
    compact = gfid_uuid.replace("-", "")
    return f"{backend_root}/.glusterfs/{compact[:2]}/{compact[2:4]}/{gfid_uuid}"


def _print_response(prefix: str, response: dict[str, Any]) -> None:
    results = response.get("results", [])
    error_count = sum(1 for item in results if item.get("error"))
    print(f"{prefix}: host={response.get('host', '')} ops={len(results)} errors={error_count}")


def _format_afr_pending_xattrs_by_host_for_notes(afr_pending_xattrs_by_host: Any) -> list[str]:
    if not isinstance(afr_pending_xattrs_by_host, dict) or not afr_pending_xattrs_by_host:
        return []
    notes = ["afr pending xattrs by host:"]
    for host in sorted(afr_pending_xattrs_by_host):
        xattrs = afr_pending_xattrs_by_host.get(host) or []
        if not isinstance(xattrs, list):
            xattrs = []
        names = [
            str(item.get("name") or "")
            for item in xattrs
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if names:
            notes.append(f"  {host}: " + ", ".join(names))
        else:
            notes.append(f"  {host}: (empty)")
    return notes


def _format_brick_roles_by_host_for_notes(brick_roles_by_host: Any) -> list[str]:
    normalized = _normalize_brick_roles_by_host(brick_roles_by_host)
    if not normalized:
        return []
    notes = ["brick roles by host:"]
    for host in sorted(normalized):
        notes.append(f"  {host}: {normalized[host]}")
    return notes


def _format_posix_metadata_by_host_for_notes(metadata_by_host: Any) -> list[str]:
    if not isinstance(metadata_by_host, dict) or not metadata_by_host:
        return []
    notes = ["POSIX metadata by host:"]
    for host in sorted(metadata_by_host):
        record = metadata_by_host.get(host) or {}
        if not isinstance(record, dict):
            record = {}
        parts: list[str] = []
        mode_bits = record.get("mode_bits")
        if mode_bits not in {None, ""}:
            try:
                parts.append(f"mode={int(mode_bits):04o}")
            except (TypeError, ValueError):
                parts.append(f"mode={mode_bits}")
        uid = record.get("uid")
        if uid not in {None, ""}:
            parts.append(f"uid={uid}")
        gid = record.get("gid")
        if gid not in {None, ""}:
            parts.append(f"gid={gid}")
        acl_access = str(record.get("acl_access") or "").strip()
        if acl_access:
            parts.append(f"acl_access={acl_access.replace(chr(10), ' | ')}")
        acl_default = str(record.get("acl_default") or "").strip()
        if acl_default:
            parts.append(f"acl_default={acl_default.replace(chr(10), ' | ')}")
        backend = str(record.get("backend") or record.get("backend_path") or "").strip()
        if backend:
            parts.append(f"backend={backend}")
        notes.append(f"  {host}: " + ", ".join(parts) if parts else f"  {host}: (empty)")
    return notes


def _format_type_mismatch_backend_observations_for_notes(
    backend_observations_by_host: Any,
) -> list[str]:
    if not isinstance(backend_observations_by_host, dict) or not backend_observations_by_host:
        return []
    notes = ["type-mismatch backend observations by host:"]
    for host in sorted(backend_observations_by_host):
        observation = backend_observations_by_host.get(host) or {}
        if not isinstance(observation, dict):
            observation = {}
        error = str(observation.get("error") or "").strip()
        if error:
            notes.append(f"  {host}: error={error}")
            continue
        parts: list[str] = []
        kind = str(observation.get("kind") or "").strip()
        if kind:
            parts.append(f"kind={kind}")
        mtime = observation.get("mtime")
        if mtime not in {None, ""}:
            parts.append(f"mtime={mtime}")
        size = observation.get("size")
        if size not in {None, ""}:
            parts.append(f"size={size}")
        path = str(observation.get("path") or "").strip()
        if path:
            parts.append(f"path={path}")
        notes.append(f"  {host}: " + ", ".join(parts) if parts else f"  {host}: (empty)")
    return notes
