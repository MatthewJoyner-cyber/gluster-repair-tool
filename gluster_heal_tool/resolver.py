# SPDX-License-Identifier: GPL-2.0-only
"""Resolution helpers."""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path

from .install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_SERVICE_USER
from .models import ResolutionObservation
from .ssh_identity import ssh_identity_options
from .shared_io import write_json_shared
from .volume import normalize_host_alias


def _shell_join(parts: list[str]) -> str:
    return shlex.join(parts)


def _remote_command(parts: list[str], *, use_sudo: bool) -> str:
    command = ["sudo", "-n", *parts] if use_sudo else list(parts)
    return shlex.join(command)


def _is_bare_gfid_entry(raw_entry: str) -> bool:
    value = raw_entry.strip()
    return value.startswith("<gfid:") and value.endswith(">") and "/" not in value


_INDEX_PROBE_SCRIPT = r"""
import json
import os
import stat
import sys
import uuid

path = sys.argv[1]
payload = {"lexists": os.path.lexists(path)}


def _kind(mode):
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return ""


def _trusted(target):
    if not target or not os.path.exists(target):
        return ""
    try:
        raw = os.getxattr(target, "trusted.gfid")
    except OSError:
        return ""
    if not raw:
        return ""
    candidates = []
    if isinstance(raw, bytes):
        if len(raw) == 16:
            candidates.append(str(uuid.UUID(bytes=raw)))
        text = raw.decode("utf-8", errors="ignore").strip().strip("\x00").strip()
        if text:
            candidates.append(text)
    else:
        text = str(raw).strip().strip("\x00").strip()
        if text:
            candidates.append(text)
    for candidate in candidates:
        try:
            return str(uuid.UUID(candidate))
        except (ValueError, AttributeError, TypeError):
            try:
                return str(uuid.UUID(hex=candidate.replace("-", "")))
            except (ValueError, AttributeError, TypeError):
                continue
    return ""


if payload["lexists"]:
    try:
        st = os.lstat(path)
        payload["kind"] = _kind(st.st_mode)
        payload["readlink"] = os.readlink(path) if stat.S_ISLNK(st.st_mode) else ""
        terminal = os.path.realpath(path)
        payload["terminal_path"] = terminal
        payload["terminal_exists"] = os.path.lexists(terminal)
        if payload["terminal_exists"]:
            terminal_stat = os.lstat(terminal)
            payload["terminal_kind"] = _kind(terminal_stat.st_mode)
            payload["terminal_readlink"] = (
                os.readlink(terminal) if stat.S_ISLNK(terminal_stat.st_mode) else ""
            )
            payload["terminal_trusted_gfid"] = _trusted(terminal)
    except OSError as exc:
        payload["error"] = str(exc)

print(json.dumps(payload))
"""


_BACKEND_STAT_SCRIPT = r"""
import json
import os
import stat
import sys
import uuid

path = sys.argv[1]
lexists = os.path.lexists(path)
exists = os.path.exists(path)
payload = {"lexists": lexists, "exists": exists}


def _kind(mode):
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return ""


def _trusted(target):
    if not target or not os.path.exists(target):
        return ""
    try:
        raw = os.getxattr(target, "trusted.gfid")
    except OSError:
        return ""
    if not raw:
        return ""
    candidates = []
    if isinstance(raw, bytes):
        if len(raw) == 16:
            candidates.append(str(uuid.UUID(bytes=raw)))
        text = raw.decode("utf-8", errors="ignore").strip().strip("\x00").strip()
        if text:
            candidates.append(text)
    else:
        text = str(raw).strip().strip("\x00").strip()
        if text:
            candidates.append(text)
    for candidate in candidates:
        try:
            return str(uuid.UUID(candidate))
        except (ValueError, AttributeError, TypeError):
            try:
                return str(uuid.UUID(hex=candidate.replace("-", "")))
            except (ValueError, AttributeError, TypeError):
                continue
    return ""


def _mdata_hex(target):
    if not target or not os.path.exists(target):
        return ""
    try:
        raw = os.getxattr(target, "trusted.glusterfs.mdata")
    except OSError:
        return ""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return "0x" + raw.hex()
    return "0x" + bytes(str(raw), "utf-8").hex()


def _acl_hex(target, name):
    if not target or not os.path.exists(target):
        return "", ""
    try:
        raw = os.getxattr(target, name)
    except OSError as exc:
        return "", str(exc)
    if not raw:
        return "", ""
    if isinstance(raw, bytes):
        return "0x" + raw.hex(), ""
    return "0x" + bytes(str(raw), "utf-8").hex(), ""


if lexists:
    st = os.lstat(path)
    payload["kind"] = _kind(st.st_mode)
    payload["mtime"] = int(st.st_mtime)
    payload["size"] = int(st.st_size)
    payload["mode"] = stat.filemode(st.st_mode)
    payload["mode_bits"] = int(stat.S_IMODE(st.st_mode))
    payload["uid"] = int(st.st_uid)
    payload["gid"] = int(st.st_gid)
    payload["trusted_gfid"] = _trusted(path)
    payload["trusted_glusterfs_mdata"] = _mdata_hex(path)
    payload["acl_access"], payload["acl_access_error"] = _acl_hex(path, "system.posix_acl_access")
    payload["acl_default"], payload["acl_default_error"] = _acl_hex(path, "system.posix_acl_default")

print(json.dumps(payload))
"""


class LiveResolver:
    def __init__(
        self,
        volume: str,
        hosts: list[str],
        brick_path: str,
        mountpoint: str,
        brick_paths: dict[str, str] | None = None,
        resolver_path: str = str(DEFAULT_RESOLVER_PATH),
        ssh_user: str = DEFAULT_SERVICE_USER,
        verbose: bool = False,
    ) -> None:
        self.volume = volume
        self.hosts = hosts
        self.brick_path = brick_path
        self.brick_paths = {
            normalize_host_alias(host): path
            for host, path in (brick_paths or {}).items()
            if normalize_host_alias(host) and path
        }
        self.mountpoint = mountpoint
        self.resolver_path = resolver_path
        self.ssh_user = ssh_user
        self.verbose = verbose

    def resolve_entry(self, raw_entry: str) -> list[ResolutionObservation]:
        return [self._resolve_on_host(host, raw_entry) for host in self.hosts]

    def _resolve_on_host(self, host: str, raw_entry: str) -> ResolutionObservation:
        host_brick_path = self.brick_paths.get(normalize_host_alias(host), self.brick_path)
        args = [
            self.resolver_path,
            "-v",
            self.volume,
            "-b",
            host_brick_path,
            "-m",
            self.mountpoint,
            "-e",
            raw_entry,
            "-k",
        ]
        if self.verbose:
            args.append("-V")
        remote_cmd = _remote_command(args, use_sudo=self.ssh_user != "root")
        proc = subprocess.run(
            [
                "ssh",
                *ssh_identity_options(),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                f"{self.ssh_user}@{host}",
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        obs = _parse_key_value_output(host, raw_entry, proc.stdout)
        if proc.returncode != 0:
            obs.error = proc.stderr.strip() or f"resolver exit {proc.returncode}"
            return obs
        if (
            self.ssh_user == "root"
            and obs.gfid
            and (_is_bare_gfid_entry(raw_entry) or (not obs.gfid_path and not obs.backend))
        ):
            self._probe_gfid_index_reference(host, obs)
        if obs.backend:
            stat_cmd = _remote_command(
                [
                    "python3",
                    "-c",
                    _BACKEND_STAT_SCRIPT,
                    obs.backend,
                ],
                use_sudo=self.ssh_user != "root",
            )
            stat_proc = subprocess.run(
                [
                    "ssh",
                    *ssh_identity_options(),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    f"{self.ssh_user}@{host}",
                    stat_cmd,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if stat_proc.returncode == 0 and stat_proc.stdout.strip():
                payload = json.loads(stat_proc.stdout)
                obs.backend_lexists = bool(payload.get("lexists", payload.get("exists")))
                obs.backend_exists = bool(payload.get("exists"))
                obs.backend_mtime = payload.get("mtime")
                obs.backend_size = payload.get("size")
                obs.backend_mode = payload.get("mode", "")
                obs.backend_mode_bits = payload.get("mode_bits")
                obs.backend_uid = payload.get("uid")
                obs.backend_gid = payload.get("gid")
                obs.backend_acl_access_text = str(payload.get("acl_access") or obs.backend_acl_access_text)
                obs.backend_acl_default_text = str(payload.get("acl_default") or obs.backend_acl_default_text)
                obs.backend_acl_error = "; ".join(
                    part
                    for part in [str(payload.get("acl_access_error") or ""), str(payload.get("acl_default_error") or "")]
                    if part
                )
                obs.backend_lstat_type = str(payload.get("kind") or obs.backend_lstat_type)
                obs.backend_is_symlink = obs.backend_lstat_type == "symlink"
                obs.backend_trusted_gfid = str(payload.get("trusted_gfid") or obs.backend_trusted_gfid)
                obs.backend_mdata_hex = str(payload.get("trusted_glusterfs_mdata") or obs.backend_mdata_hex)
            else:
                obs.error = "; ".join(
                    part for part in [obs.error, stat_proc.stderr.strip()] if part
                )
        return obs

    def _probe_gfid_index_reference(self, host: str, obs: ResolutionObservation) -> None:
        if not obs.gfid:
            return
        brick_path = self.brick_paths.get(normalize_host_alias(host), self.brick_path)
        for bucket in ("xattrop", "dirty"):
            index_path = f"{brick_path}/.glusterfs/indices/{bucket}/{obs.gfid}"
            probe_cmd = _remote_command(
                [
                    "python3",
                    "-c",
                    _INDEX_PROBE_SCRIPT,
                    index_path,
                ],
                use_sudo=self.ssh_user != "root",
            )
            probe_proc = subprocess.run(
                [
                    "ssh",
                    *ssh_identity_options(),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    f"{self.ssh_user}@{host}",
                    probe_cmd,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe_proc.returncode != 0 or not probe_proc.stdout.strip():
                continue
            try:
                payload = json.loads(probe_proc.stdout)
            except json.JSONDecodeError:
                continue
            if not payload.get("lexists"):
                continue
            obs.gfid_path = index_path
            obs.gfid_path_lexists = True
            obs.gfid_path_exists = bool(payload.get("terminal_exists", True))
            obs.gfid_path_lstat_type = str(payload.get("kind") or "symlink")
            obs.gfid_path_is_symlink = obs.gfid_path_lstat_type == "symlink"
            obs.gfid_path_readlink = str(payload.get("readlink") or "")
            obs.gfid_path_readlink_chain = [str(payload.get("terminal_path") or "")] if payload.get("terminal_path") else []
            obs.gfid_path_terminal_path = str(payload.get("terminal_path") or "")
            obs.gfid_path_terminal_lstat_type = str(payload.get("terminal_kind") or "")
            obs.gfid_path_terminal_exists = bool(payload.get("terminal_exists", False))
            obs.gfid_path_terminal_is_symlink = obs.gfid_path_terminal_lstat_type == "symlink"
            obs.gfid_path_terminal_readlink = str(payload.get("terminal_readlink") or "")
            obs.gfid_path_terminal_trusted_gfid = str(payload.get("terminal_trusted_gfid") or "")
            terminal_path = obs.gfid_path_terminal_path
            terminal_is_internal_index = "/.glusterfs/indices/" in terminal_path
            if terminal_path and not terminal_is_internal_index:
                obs.backend = obs.gfid_path_terminal_path
                obs.backend_lexists = bool(payload.get("terminal_exists", False))
                obs.backend_exists = bool(payload.get("terminal_exists", False))
                obs.backend_lstat_type = obs.gfid_path_terminal_lstat_type
                obs.backend_is_symlink = obs.gfid_path_terminal_is_symlink
                obs.backend_readlink = obs.gfid_path_terminal_readlink
                obs.backend_readlink_chain = list(obs.gfid_path_readlink_chain)
                obs.backend_terminal_path = obs.gfid_path_terminal_path
                obs.backend_terminal_exists = obs.gfid_path_terminal_exists
                obs.backend_terminal_lstat_type = obs.gfid_path_terminal_lstat_type
                obs.backend_terminal_is_symlink = obs.gfid_path_terminal_is_symlink
                obs.backend_terminal_readlink = obs.gfid_path_terminal_readlink
                obs.backend_terminal_trusted_gfid = obs.gfid_path_terminal_trusted_gfid
                obs.backend_trusted_gfid = obs.gfid_path_terminal_trusted_gfid
                if obs.backend_exists and obs.backend_lstat_type == "dir":
                    obs.backend_child_names = list(obs.backend_child_names)
                return
            if terminal_path:
                return


class CachedResolver:
    def __init__(self, observations: list[ResolutionObservation]) -> None:
        self._by_entry: dict[str, list[ResolutionObservation]] = {}
        for obs in observations:
            self._by_entry.setdefault(obs.raw_entry, []).append(obs)

    @classmethod
    def from_file(cls, path: str | Path) -> "CachedResolver":
        payload = json.loads(Path(path).read_text())
        observations = [
            ResolutionObservation(**item)
            for item in payload.get("observations", payload)
        ]
        return cls(observations)

    def resolve_entry(self, raw_entry: str) -> list[ResolutionObservation]:
        return list(self._by_entry.get(raw_entry, []))


def dump_observations(path: str | Path, observations: list[ResolutionObservation]) -> None:
    write_json_shared(path, {"observations": [asdict(item) for item in observations]})


def _parse_key_value_output(host: str, raw_entry: str, stdout: str) -> ResolutionObservation:
    data: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return ResolutionObservation(
        host=host,
        raw_entry=raw_entry,
        gfid=data.get("GFID", ""),
        child_rel=data.get("CHILD_REL", ""),
        gfid_path=data.get("GFID_PATH", ""),
        backend=data.get("BACKEND", ""),
        type=data.get("TYPE", "unknown"),
        gfid_exists=data.get("GFID_EXISTS", "0") == "1",
        backend_lexists=data.get("BACKEND_LEXISTS", "0") == "1",
        backend_exists=data.get("BACKEND_EXISTS", "0") == "1",
        backend_lstat_type=data.get("BACKEND_LSTAT_TYPE", ""),
        backend_is_symlink=data.get("BACKEND_LSTAT_TYPE", "") == "symlink",
        backend_trusted_gfid=data.get("BACKEND_TRUSTED_GFID", ""),
        backend_gfid_path=data.get("BACKEND_GFID_PATH", ""),
        backend_gfid_path_checked=data.get("BACKEND_GFID_PATH_CHECKED", "0") == "1",
        backend_gfid_path_lexists=data.get("BACKEND_GFID_PATH_LEXISTS", "0") == "1",
        backend_gfid_path_exists=data.get("BACKEND_GFID_PATH_EXISTS", "0") == "1",
        backend_gfid_path_lstat_type=data.get("BACKEND_GFID_PATH_LSTAT_TYPE", ""),
        backend_mdata_hex=data.get("BACKEND_MDATA_HEX", ""),
        gfid_path_lexists=data.get("GFID_PATH_LEXISTS", "0") == "1",
        gfid_path_exists=data.get("GFID_PATH_EXISTS", "0") == "1",
        gfid_path_lstat_type=data.get("GFID_PATH_LSTAT_TYPE", ""),
        gfid_path_is_symlink=data.get("GFID_PATH_LSTAT_TYPE", "") == "symlink",
        file_gfid=data.get("FILE_GFID", ""),
        file_gfid_path=data.get("FILE_GFID_PATH", ""),
        relpath=data.get("RELPATH", ""),
        mounted=data.get("MOUNTED", ""),
        depth=int(data.get("DEPTH", "0") or "0"),
    )
