# SPDX-License-Identifier: GPL-2.0-only
"""Temporary mount helpers."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from .controller_paths import default_temp_mount_root
from .volume import discover_brick_hosts

DEFAULT_TEMP_MOUNT_ROOT = default_temp_mount_root()


@dataclass
class TempMountResult:
    volume: str
    source_host: str
    source: str
    mount_root: str
    mountpoint: str
    aux_gfid_mount: bool
    acl_mount: bool
    cleanup: bool
    mounted: bool
    already_mounted: bool
    cleaned_up: bool
    removed_mountpoint: bool
    validated: bool
    mount_source: str
    mount_fstype: str
    mount_target: str
    mount_options: str
    expected_source_suffix: str
    command: list[str]
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def temp_mount_path(
    volume: str,
    *,
    aux_gfid_mount: bool = False,
    mount_root: str | Path = DEFAULT_TEMP_MOUNT_ROOT,
) -> Path:
    safe_volume = volume.strip().strip("/").replace("/", "-")
    suffix = "-gfid" if aux_gfid_mount else ""
    return Path(mount_root) / f"{safe_volume}{suffix}"


def _mount_source(volume: str, source_host: str | None = None) -> tuple[str, str]:
    volume_name = volume.strip().strip("/")
    if not volume_name:
        raise RuntimeError("volume name is required for a temporary mount")
    host = source_host or discover_brick_hosts(volume_name)[0]
    return host, f"{host}:{volume_name}"


def _sudo_prefix() -> list[str]:
    return ["sudo", "-n"] if os.geteuid() != 0 else []


def _inspect_mountpoint(mountpoint: Path) -> dict[str, str]:
    commands = [
        ["findmnt", "-T", str(mountpoint), "-no", "SOURCE,FSTYPE,TARGET,OPTIONS"],
        ["sudo", "-n", "findmnt", "-T", str(mountpoint), "-no", "SOURCE,FSTYPE,TARGET,OPTIONS"],
    ]
    for command in commands:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            continue
        parts = lines[-1].split(None, 3)
        if len(parts) < 3:
            continue
        source, fstype, target = parts[:3]
        options = parts[3] if len(parts) >= 4 else ""
        return {
            "source": source,
            "fstype": fstype,
            "target": target,
            "options": options,
        }
    return {}


def _mount_options(options: str) -> set[str]:
    return {item.strip().lower() for item in options.split(",") if item.strip()}


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def inspect_mount_at_path_lexically(path: str | Path) -> dict[str, str]:
    """Return the covering mount from mountinfo without resolving the target path."""
    requested = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    candidates: list[dict[str, str]] = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return {}

    for line in lines:
        left, separator, right = line.rstrip("\n").partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 2:
            continue
        target = _decode_mountinfo_path(left_fields[4])
        if target != "/" and requested != target and not requested.startswith(f"{target}/"):
            continue
        candidates.append(
            {
                "source": _decode_mountinfo_path(right_fields[1]),
                "fstype": right_fields[0],
                "target": target,
                "options": left_fields[5],
            }
        )

    if not candidates:
        return {}
    return max(candidates, key=lambda item: len(item["target"]))


def inspect_mount_at_path(path: str | Path) -> dict[str, str]:
    """Return the mount details that cover a client-visible path."""
    current = Path(path)
    while True:
        info = _inspect_mountpoint(current)
        if info:
            return info
        if current.parent == current:
            return {}
        current = current.parent


def _validate_mountpoint(
    volume: str,
    mountpoint: Path,
    *,
    aux_gfid_mount: bool,
    acl_mount: bool = False,
) -> tuple[dict[str, str], str]:
    info = _inspect_mountpoint(mountpoint)
    if not info:
        raise RuntimeError(f"unable to inspect existing mount at {mountpoint}")
    expected_suffix = f":{volume.strip().strip('/')}"
    source = info.get("source", "")
    fstype = info.get("fstype", "")
    target = info.get("target", "")
    if not source.endswith(expected_suffix):
        raise RuntimeError(
            f"temporary mount at {mountpoint} is mounted from {source or 'unknown source'}, expected a Gluster source ending in {expected_suffix}"
        )
    if "gluster" not in fstype.lower():
        raise RuntimeError(
            f"temporary mount at {mountpoint} has filesystem type {fstype or 'unknown'}, expected Gluster"
        )
    if target != str(mountpoint):
        raise RuntimeError(
            f"temporary mount at {mountpoint} reports target {target or 'unknown'}, expected {mountpoint}"
        )
    if aux_gfid_mount and not (mountpoint / ".gfid").is_dir():
        raise RuntimeError(f"temporary mount at {mountpoint} does not expose a .gfid directory")
    if acl_mount and "acl" not in _mount_options(info.get("options", "")):
        raise RuntimeError(f"temporary mount at {mountpoint} does not expose ACL support")
    return info, expected_suffix


def prepare_temp_mountpoint(mountpoint: Path) -> None:
    mountpoint.parent.mkdir(parents=True, exist_ok=True)
    if mountpoint.exists() and not mountpoint.is_dir():
        raise RuntimeError(f"temporary mountpoint {mountpoint} exists but is not a directory")
    if not mountpoint.exists():
        mountpoint.mkdir(mode=0o700)
    try:
        os.chmod(mountpoint, 0o000)
    except PermissionError:
        # A detached FUSE mount can leave a root-owned mountpoint behind. The
        # following mount already requires sudo, so repair that narrow state
        # rather than requiring the caller to manually chown the workspace.
        command = [*_sudo_prefix(), "chmod", "000", str(mountpoint)]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.strip() or f"failed to secure temporary mountpoint {mountpoint}"
            )


def mount_temp_volume(
    volume: str,
    *,
    mount_root: str | Path = DEFAULT_TEMP_MOUNT_ROOT,
    source_host: str | None = None,
    aux_gfid_mount: bool = False,
    acl_mount: bool = False,
) -> TempMountResult:
    mountpoint = temp_mount_path(volume, aux_gfid_mount=aux_gfid_mount, mount_root=mount_root)
    source_host, source = _mount_source(volume, source_host=source_host)
    command: list[str] = []
    already_mounted = os.path.ismount(mountpoint)
    if already_mounted and acl_mount:
        current_info = _inspect_mountpoint(mountpoint)
        if current_info and "acl" not in _mount_options(current_info.get("options", "")):
            command, error = _run_unmount(mountpoint)
            if error:
                raise RuntimeError(error or f"failed to unmount {mountpoint} before remounting with ACL support")
            already_mounted = False
    if already_mounted:
        info, expected_source_suffix = _validate_mountpoint(
            volume,
            mountpoint,
            aux_gfid_mount=aux_gfid_mount,
            acl_mount=acl_mount,
        )
    else:
        prepare_temp_mountpoint(mountpoint)
        command = [*_sudo_prefix(), "mount", "-t", "glusterfs"]
        options: list[str] = []
        if aux_gfid_mount:
            options.append("aux-gfid-mount")
        if acl_mount:
            options.append("acl")
        if options:
            command.extend(["-o", ",".join(options)])
        command.extend([source, str(mountpoint)])
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"failed to mount {source} on {mountpoint}")
        info, expected_source_suffix = _validate_mountpoint(
            volume,
            mountpoint,
            aux_gfid_mount=aux_gfid_mount,
            acl_mount=acl_mount,
        )
    try:
        os.chmod(mountpoint, 0o000)
    except OSError:
        pass
    return TempMountResult(
        volume=volume,
        source_host=source_host,
        source=source,
        mount_root=str(Path(mount_root)),
        mountpoint=str(mountpoint),
        aux_gfid_mount=aux_gfid_mount,
        acl_mount=acl_mount,
        cleanup=False,
        mounted=True,
        already_mounted=already_mounted,
        cleaned_up=False,
        removed_mountpoint=False,
        validated=True,
        mount_source=info.get("source", ""),
        mount_fstype=info.get("fstype", ""),
        mount_target=info.get("target", ""),
        mount_options=info.get("options", ""),
        expected_source_suffix=expected_source_suffix,
        command=command,
    )


def _run_unmount(mountpoint: Path) -> tuple[list[str], str]:
    candidates = [
        [*_sudo_prefix(), "umount", str(mountpoint)],
        [*_sudo_prefix(), "fusermount3", "-u", str(mountpoint)],
        [*_sudo_prefix(), "fusermount", "-u", str(mountpoint)],
    ]
    last_error = ""
    for command in candidates:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return command, ""
        last_error = proc.stderr.strip() or f"{command[0]} exit {proc.returncode}"
    return candidates[-1], last_error


def cleanup_temp_mount(
    volume: str,
    *,
    mount_root: str | Path = DEFAULT_TEMP_MOUNT_ROOT,
    aux_gfid_mount: bool = False,
    acl_mount: bool = False,
) -> TempMountResult:
    mountpoint = temp_mount_path(volume, aux_gfid_mount=aux_gfid_mount, mount_root=mount_root)
    source_host = ""
    source = ""
    command: list[str] = []
    error = ""
    cleaned_up = False
    removed_mountpoint = False
    validated = False
    mount_source = ""
    mount_fstype = ""
    mount_target = ""
    mount_options = ""
    expected_source_suffix = f":{volume.strip().strip('/')}"
    if os.path.ismount(mountpoint):
        info, expected_source_suffix = _validate_mountpoint(
            volume,
            mountpoint,
            aux_gfid_mount=aux_gfid_mount,
            acl_mount=acl_mount,
        )
        validated = True
        mount_source = info.get("source", "")
        mount_fstype = info.get("fstype", "")
        mount_target = info.get("target", "")
        mount_options = info.get("options", "")
        command, error = _run_unmount(mountpoint)
        cleaned_up = error == ""
    else:
        cleaned_up = True
    if mountpoint.exists() and mountpoint.is_dir():
        try:
            os.chmod(mountpoint, 0o700)
            os.rmdir(mountpoint)
            removed_mountpoint = True
        except OSError as exc:
            fallback = subprocess.run([*_sudo_prefix(), "rm", "-rf", str(mountpoint)], capture_output=True, text=True, check=False)
            if fallback.returncode == 0:
                removed_mountpoint = True
                error = ""
            elif not error:
                error = fallback.stderr.strip() or str(exc)
    return TempMountResult(
        volume=volume,
        source_host=source_host,
        source=source,
        mount_root=str(Path(mount_root)),
        mountpoint=str(mountpoint),
        aux_gfid_mount=aux_gfid_mount,
        acl_mount=acl_mount,
        cleanup=True,
        mounted=False,
        already_mounted=False,
        cleaned_up=cleaned_up,
        removed_mountpoint=removed_mountpoint,
        validated=validated,
        mount_source=mount_source,
        mount_fstype=mount_fstype,
        mount_target=mount_target,
        mount_options=mount_options,
        expected_source_suffix=expected_source_suffix,
        command=command,
        error=error,
    )


def run_temp_mount(
    volume: str,
    *,
    mount_root: str | Path = DEFAULT_TEMP_MOUNT_ROOT,
    source_host: str | None = None,
    aux_gfid_mount: bool = False,
    acl_mount: bool = False,
    cleanup: bool = False,
) -> TempMountResult:
    if cleanup:
        return cleanup_temp_mount(volume, mount_root=mount_root, aux_gfid_mount=aux_gfid_mount, acl_mount=acl_mount)
    return mount_temp_volume(
        volume,
        mount_root=mount_root,
        source_host=source_host,
        aux_gfid_mount=aux_gfid_mount,
        acl_mount=acl_mount,
    )


def _verification_target_path(
    mountpoint: Path,
    logical_path: str,
) -> Path:
    normalized = str(logical_path or "").strip()
    if normalized.startswith("<gfid:") and ">" in normalized:
        gfid = normalized[6 : normalized.find(">")].strip()
        if gfid:
            return mountpoint / ".gfid" / gfid
    return mountpoint / normalized.lstrip("/")


def _stat_verification_target(target_path: Path, *, expect_present: bool) -> dict[str, object]:
    command = [*_sudo_prefix(), "stat", "--", str(target_path)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    message = (completed.stderr or completed.stdout).strip()
    observed_present = completed.returncode == 0
    expectation_met = observed_present if expect_present else not observed_present
    return {
        "mount_path": str(target_path),
        "command": command,
        "expected_present": expect_present,
        "observed_present": observed_present,
        "status": "ok" if expectation_met else "failed",
        "returncode": completed.returncode,
        "message": message,
    }


def _emit_progress(progress_stream: TextIO | None, message: str) -> None:
    if progress_stream is None:
        return
    progress_stream.write(message.rstrip() + "\n")
    progress_stream.flush()


def verify_temp_mount_targets(
    volume: str,
    logical_paths: list[str],
    *,
    mount_root: str | Path = DEFAULT_TEMP_MOUNT_ROOT,
    source_host: str | None = None,
    aux_gfid_mount: bool = False,
    acl_mount: bool = False,
    cleanup: bool = False,
    expect_present: bool = True,
) -> dict[str, object]:
    mount_result = run_temp_mount(
        volume,
        mount_root=mount_root,
        source_host=source_host,
        aux_gfid_mount=aux_gfid_mount,
        acl_mount=acl_mount,
    )
    mountpoint = Path(mount_result.mountpoint)
    checks: list[dict[str, object]] = []
    for logical_path in logical_paths:
        target_path = _verification_target_path(mountpoint, logical_path)
        check = _stat_verification_target(target_path, expect_present=expect_present)
        check["logical_path"] = logical_path
        checks.append(check)
    cleanup_result: dict[str, object] | None = None
    if cleanup:
        cleanup_result = cleanup_temp_mount(
            volume,
            mount_root=mount_root,
            aux_gfid_mount=aux_gfid_mount,
            acl_mount=acl_mount,
        ).to_dict()
    summary = {
        "paths_checked": len(checks),
        "paths_ok": sum(1 for item in checks if item["status"] == "ok"),
        "paths_failed": sum(1 for item in checks if item["status"] == "failed"),
    }
    return {
        "available": True,
        "volume": volume,
        "mount_root": str(Path(mount_root)),
        "aux_gfid_mount": aux_gfid_mount,
        "acl_mount": acl_mount,
        "cleanup": cleanup,
        "mount": mount_result.to_dict(),
        "checks": checks,
        "summary": summary,
        "cleanup_result": cleanup_result,
    }


def _logical_child_path(root_logical: str, relative_path: Path) -> str:
    normalized_root = str(root_logical or "").strip()
    relative = relative_path.as_posix()
    if relative in ("", "."):
        return normalized_root
    if normalized_root.startswith("<gfid:") and ">" in normalized_root:
        return f"{normalized_root.rstrip('/')}/{relative}"
    base = normalized_root.strip("/").rstrip("/")
    if base:
        return f"{base}/{relative}"
    return relative


def _recursive_verification_targets(
    root_path: Path,
    root_logical: str,
    *,
    max_depth: int,
    max_entries: int,
) -> tuple[list[tuple[Path, str, int]], bool, str]:
    targets: list[tuple[Path, str, int]] = []
    truncated = False
    truncation_reason = ""
    stack: list[tuple[Path, Path, int]] = [(root_path, Path("."), 0)]
    while stack:
        current_path, relative_path, depth = stack.pop()
        targets.append((current_path, _logical_child_path(root_logical, relative_path), depth))
        if len(targets) >= max_entries:
            truncated = True
            truncation_reason = "max_entries"
            break
        if depth >= max_depth:
            continue
        try:
            children = sorted(
                (entry for entry in current_path.iterdir()),
                key=lambda entry: entry.name,
            )
        except OSError:
            continue
        for entry in reversed(children):
            stack.append((entry, relative_path / entry.name, depth + 1))
    return targets, truncated, truncation_reason


def verify_temp_mount_rescan(
    volume: str,
    logical_paths: list[str],
    *,
    mount_root: str | Path = DEFAULT_TEMP_MOUNT_ROOT,
    source_host: str | None = None,
    aux_gfid_mount: bool = False,
    acl_mount: bool = False,
    cleanup: bool = False,
    expect_present: bool = True,
    max_depth: int = 3,
    max_entries: int = 4096,
    progress_stream: TextIO | None = None,
) -> dict[str, object]:
    mount_result = run_temp_mount(
        volume,
        mount_root=mount_root,
        source_host=source_host,
        aux_gfid_mount=aux_gfid_mount,
        acl_mount=acl_mount,
    )
    mountpoint = Path(mount_result.mountpoint)
    checks: list[dict[str, object]] = []
    truncated = False
    truncation_reason = ""
    for root_index, logical_path in enumerate(logical_paths, start=1):
        _emit_progress(
            progress_stream,
            f"verify-rescan: root {root_index}/{len(logical_paths)} {logical_path}",
        )
        root_path = _verification_target_path(mountpoint, logical_path)
        targets, target_truncated, target_reason = _recursive_verification_targets(
            root_path,
            logical_path,
            max_depth=max_depth,
            max_entries=max_entries - len(checks),
        )
        if target_truncated:
            truncated = True
            truncation_reason = target_reason
        for current_path, current_logical, depth in targets:
            check = _stat_verification_target(current_path, expect_present=expect_present)
            check["logical_path"] = current_logical
            check["depth"] = depth
            checks.append(check)
            if len(checks) == 1 or len(checks) % 100 == 0:
                _emit_progress(
                    progress_stream,
                    f"verify-rescan: checked {len(checks)} nodes so far",
                )
            if len(checks) >= max_entries:
                truncated = True
                truncation_reason = truncation_reason or "max_entries"
                break
        if truncated:
            break
    cleanup_result: dict[str, object] | None = None
    if cleanup:
        cleanup_result = cleanup_temp_mount(
            volume,
            mount_root=mount_root,
            aux_gfid_mount=aux_gfid_mount,
            acl_mount=acl_mount,
        ).to_dict()
    summary = {
        "roots_checked": len(logical_paths),
        "nodes_checked": len(checks),
        "nodes_ok": sum(1 for item in checks if item["status"] == "ok"),
        "nodes_failed": sum(1 for item in checks if item["status"] == "failed"),
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "max_depth": max_depth,
        "max_entries": max_entries,
    }
    return {
        "available": True,
        "mode": "recursive",
        "volume": volume,
        "mount_root": str(Path(mount_root)),
        "aux_gfid_mount": aux_gfid_mount,
        "acl_mount": acl_mount,
        "cleanup": cleanup,
        "mount": mount_result.to_dict(),
        "checks": checks,
        "summary": summary,
        "cleanup_result": cleanup_result,
    }


def render_temp_mount_result(result: TempMountResult) -> str:
    return json.dumps(result.to_dict(), indent=2)


def render_temp_mount_verification_result(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2)
