# SPDX-License-Identifier: GPL-2.0-only
"""File-family canary builders for batched gtest scenarios."""
from __future__ import annotations

import os
import stat
import subprocess
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from . import canary_shared as canary_api
from .canary_eligibility import POSIX_STATE_VERSION, validate_posix_source_choice_state
from .controller_paths import default_canary_stage_local_path
from .controller_paths import default_temp_mount_root
from .evidence_provenance import canary_state_plan_provenance
from .canary_observation import _observation_log_path
from .canary_file_observation import _record_file_baseline
from .heal_parser import parse_heal_info_text
from .health import build_volume_health_report
from .install_paths import DEFAULT_HOST_OPS_PATH
from .install_paths import DEFAULT_SERVICE_USER
from .install_paths import DEFAULT_WORKER_PATH
from .remote_ops import ssh_remote_command
from .volume import run_heal
from .volume import set_heal_settings

_new_gfid_hex = canary_api._new_gfid_hex
_volume_mount_root = canary_api._volume_mount_root
_run_local = canary_api._run_local
_volume_info_text = canary_api._volume_info_text
_brick_hosts_and_root = canary_api._brick_hosts_and_root
_brick_hosts_and_paths = canary_api._brick_hosts_and_paths
_backend_root_for_host = canary_api._backend_root_for_host
_select_role_host = canary_api._select_role_host
_state_root = canary_api._state_root
_state_path = canary_api._state_path
_write_state = canary_api._write_state
_read_state = canary_api._read_state
_delete_state = canary_api._delete_state
_canary_worker_command = canary_api._canary_worker_command
_local_mount_dir = canary_api._local_mount_dir
_local_mount_file = canary_api._local_mount_file
_local_write_file = canary_api._local_write_file
_local_setfacl = canary_api._local_setfacl
_is_transport_endpoint_disconnect_error = canary_api._is_transport_endpoint_disconnect_error
_mountpoint_supports_acl = canary_api._mountpoint_supports_acl
_local_symlink = canary_api._local_symlink
_local_remove = canary_api._local_remove
_make_request = canary_api._make_request
_file_gfid_link_path = canary_api._file_gfid_link_path
_directory_gfid_link_path = canary_api._directory_gfid_link_path
_print_response = canary_api._print_response
_require_successful_worker_response = canary_api._require_successful_worker_response


def _brick_hosts_and_backend_roots(volume: str) -> tuple[list[str], dict[str, str]]:
    try:
        return _brick_hosts_and_paths(volume)
    except Exception:
        brick_hosts, backend_root = _brick_hosts_and_root(volume)
        return brick_hosts, {host: backend_root for host in brick_hosts}


def _require_posix_canary_readiness(volume: str, *, ssh_user: str) -> None:
    """Refuse a POSIX fixture before it changes heal state or the mounted volume."""
    report = build_volume_health_report(
        volume,
        ssh_user=ssh_user,
        require_self_heal_daemons=True,
    )
    summary = report.get("summary") or {}
    if summary.get("ready"):
        return
    details = "; ".join(str(item) for item in summary.get("blockers") or [])
    raise RuntimeError(
        "POSIX canary readiness blocked before mutation"
        + (f": {details}" if details else "")
    )


def _trigger_canary_heal(volume: str, scenario: str) -> None:
    print(f"Triggering full heal crawl for canary: volume={volume} scenario={scenario}")
    try:
        run_heal(volume)
        return
    except RuntimeError as exc:
        message = str(exc)
        if "self-heal-daemon is disabled" not in message.lower():
            raise
    print(f"  heal crawl blocked for canary: enabling heal settings on {volume} and retrying")
    set_heal_settings(volume, True)
    run_heal(volume)


def _run_local_text(command: list[str]) -> str:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"command failed: {subprocess.list2cmdline(command)}")
    return (proc.stdout or "").strip()


def _heal_info_contains_path(
    heal_text: str,
    mount_file: str,
    *,
    mount_root: str,
    gfid: str = "",
) -> bool:
    """Match a parsed Gluster row to one recorded volume-relative identity."""
    if not heal_text or not mount_file or not mount_root:
        return False
    if (not mount_file.startswith("/") or not mount_root.startswith("/")
            or any(part in (".", "..") for part in mount_file.split("/"))
            or any(part in (".", "..") for part in mount_root.split("/"))):
        return False
    try:
        relative_path = PurePosixPath(mount_file).relative_to(PurePosixPath(mount_root))
    except ValueError:
        return False
    if str(relative_path) == ".":
        return False
    identities = {"/" + str(relative_path)}
    try:
        if gfid and str(UUID(gfid)) == gfid.lower():
            identities.add(f"<gfid:{gfid.lower()}>")
    except (ValueError, TypeError, AttributeError):
        pass
    for entry in parse_heal_info_text(heal_text):
        if entry.raw in identities:
            return True
    return False


def _canary_temp_mount_root(volume: str) -> str:
    return str(default_temp_mount_root() / "repair-canary" / volume.strip().strip("/"))


def _mountpoint_is_active(path: str) -> bool:
    proc = subprocess.run(["mountpoint", "-q", "--", path], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _ensure_canary_mount(volume: str, mount_root: str, *, acl_mount: bool = False) -> None:
    _local_mount_dir(mount_root)
    if _mountpoint_is_active(mount_root):
        if acl_mount and not _mountpoint_supports_acl(mount_root):
            _unmount_canary_mount(mount_root)
        else:
            return
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            "mount",
            "-t",
            "glusterfs",
            *(["-o", "acl"] if acl_mount else []),
            f"localhost:{volume.strip().strip('/')}",
            mount_root,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to mount temporary canary workspace for {volume}")

def _unmount_canary_mount(mount_root: str) -> None:
    if not _mountpoint_is_active(mount_root):
        return
    candidates = [
        ["sudo", "-n", "fusermount3", "-u", mount_root],
        ["sudo", "-n", "fusermount3", "-uz", mount_root],
        ["sudo", "-n", "umount", "--", mount_root],
        ["sudo", "-n", "umount", "-l", "--", mount_root],
    ]
    last_error = ""
    for command in candidates:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return
        combined = f"{proc.stdout}\n{proc.stderr}".strip()
        if "not mounted" in combined.lower():
            return
        last_error = combined or last_error
    raise RuntimeError(last_error or f"failed to unmount temporary canary workspace {mount_root}")


def _resolve_mount_file_gfid(
    *,
    volume: str,
    mount_root: str,
    mount_file: str,
    backend_root: str,
) -> tuple[str, str, str]:
    if not mount_file.startswith(mount_root):
        raise RuntimeError(f"mount file {mount_file!r} is not under mount root {mount_root!r}")
    mount_rel = mount_file[len(mount_root) :]
    if not mount_rel.startswith("/"):
        mount_rel = f"/{mount_rel}"
    resolver_path = Path(__file__).resolve().parent.parent / "gluster-resolve-gfid-plus.sh"
    proc = subprocess.run(
        ["sudo", "-n", str(resolver_path), "-v", volume, "-b", backend_root, "-m", mount_root, "-e", mount_rel, "-k", "-s"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to resolve GFID for {mount_file}")
    data: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    canonical_gfid = str(
        data.get("BACKEND_TRUSTED_GFID")
        or data.get("FILE_GFID")
        or data.get("GFID")
        or ""
    ).strip()
    file_gfid_path = str(data.get("FILE_GFID_PATH") or "").strip()
    backend_path = str(data.get("BACKEND") or "").strip()
    if not canonical_gfid:
        raise RuntimeError(
            f"resolver did not return a canonical GFID for {mount_file}"
        )
    return canonical_gfid, file_gfid_path, backend_path


def _run_remote_host_ops(
    host: str,
    command: list[str],
    *,
    ssh_user: str,
    host_ops_path: str = str(DEFAULT_HOST_OPS_PATH),
    connect_timeout: int = 10,
) -> str:
    proc = subprocess.run(
        ssh_remote_command(
            host,
            command,
            ssh_user=ssh_user,
            use_sudo=True,
            sudo_path=host_ops_path,
            connect_timeout=connect_timeout,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"host-ops command failed on {host}: exit {proc.returncode}")
    return (proc.stdout or "").strip()


def _capture_mount_symlink_snapshot(path: str) -> dict[str, Any]:
    try:
        st = os.lstat(path)
    except OSError as exc:
        return {"path": path, "ok": False, "error": str(exc)}
    kind = "symlink" if stat.S_ISLNK(st.st_mode) else "not-symlink"
    readlink = os.readlink(path) if stat.S_ISLNK(st.st_mode) else ""
    return {
        "path": path,
        "ok": True,
        "kind": kind,
        "readlink": readlink,
        "inode": int(st.st_ino),
        "links": int(st.st_nlink),
        "mode": stat.filemode(st.st_mode),
        "mtime": int(st.st_mtime),
        "ctime": int(st.st_ctime),
    }

def _capture_afr_pending_xattrs_by_host(
    *,
    volume: str,
    scenario: str,
    backend_roots: dict[str, str],
    backend_by_host: dict[str, str],
    hosts: list[str],
    ssh_user: str,
    worker_path: str,
    response_label: str,
) -> dict[str, list[dict[str, object]]]:
    afr_pending_xattrs_by_host: dict[str, list[dict[str, object]]] = {}
    for host in hosts:
        afr_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_roots[host],
                [
                    {
                        "op": "inspect_afr_state",
                        "path": backend_by_host[host],
                    },
                ],
            ),
        )
        _print_response(response_label, afr_response)
        afr_entry = next(
            (
                item
                for item in afr_response.get("results", [])
                if item.get("op") == "inspect_afr_state" and item.get("ok")
            ),
            {},
        )
        afr_pending_xattrs_by_host[host] = list(afr_entry.get("afr_xattrs") or [])
    return afr_pending_xattrs_by_host


def _wait_for_full_heal_crawl(volume: str, *, timeout_seconds: int = 600, poll_seconds: int = 5) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_output = ""
    while True:
        last_output = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "statistics"])
        if "Crawl is in progress" not in last_output and "Type of crawl: FULL" in last_output:
            return last_output
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for full heal crawl to finish on {volume}")
        time.sleep(poll_seconds)


def _brick_status_is_online(
    volume: str,
    host: str,
    brick_path: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
) -> bool:
    status = _run_remote_host_ops(
        host,
        ["brick-online", "--volume", volume, "--brick", brick_path],
        ssh_user=ssh_user,
        host_ops_path=str(DEFAULT_HOST_OPS_PATH),
    )
    if status.startswith("online "):
        return True
    if status == "offline":
        return False
    raise RuntimeError(f"did not parse Gluster brick state for {host}:{brick_path}: {status!r}")


def create_file_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    backend_root = backend_roots[mismatch_host]
    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{backend_root}/{scenario}/{dir_name}/{file_name}"
    mismatch_gfid_hex = _new_gfid_hex()

    _ensure_canary_mount(volume, mount_root)
    # Recreate the directory after the temp mount is live; the pre-mount mkdir
    # is hidden by the mount overlay and would otherwise leave the file path
    # missing inside the mounted volume.
    _local_mount_dir(mount_dir)
    _local_mount_file(mount_file)

    request = _make_request(
        volume,
        scenario,
        backend_root,
        [
            {
                "op": "setxattr",
                "path": backend_file,
                "name": "trusted.gfid",
                "value_hex": mismatch_gfid_hex,
            }
        ],
    )
    response = _canary_worker_command(mismatch_host, ssh_user=ssh_user, worker_path=worker_path, request=request)
    _print_response("file-metadata worker", response)

    _write_state(
        volume,
        scenario,
        {
            "kind": "file-metadata",
            "volume": volume,
            "scenario": scenario,
            "construction_class": "backend-audit",
            "proof_label": "operator-path-mechanical",
            "mismatch_host": mismatch_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_file,
            "backend_root": backend_root,
            "backend_target": backend_file,
            "mismatch_gfid_hex": mismatch_gfid_hex,
        },
    )
    _trigger_canary_heal(volume, scenario)

    print(
        "\n".join(
            [
                "Created gtest file-metadata canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  construction class: backend-audit",
                f"  allowed proof label: operator-path-mechanical",
                f"  mismatch host: {mismatch_host}",
                f"  backend target: {backend_file}",
                f"  backend root: {backend_root}",
                f"  mismatch gfid: {mismatch_gfid_hex}",
                f"Next: sudo -n find {mount_dir} -maxdepth 4 -exec stat -- {{}} +",
                f"Then: sudo -n gluster volume heal {volume} info",
            ]
        )
    )




def _create_file_posix_no_majority_mode_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None,
    dir_name: str,
    file_name: str,
    hold_mismatch_offline: bool,
    ssh_user: str,
    worker_path: str,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for file POSIX metadata canary on {volume}")

    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    source_hosts = [host for host in brick_hosts if host != mismatch_host]
    source_host = source_hosts[0]
    second_mismatch_host = source_hosts[1]
    source_backend_root = backend_roots[source_host]
    mismatch_backend_root = backend_roots[mismatch_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    source_backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"
    mismatch_backend_file = f"{mismatch_backend_root}/{scenario}/{dir_name}/{file_name}"

    _require_posix_canary_readiness(volume, ssh_user=ssh_user)
    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_write_file(mount_file, "Initial content")
    _run_local(["sudo", "-n", "stat", mount_file])
    _run_local(["sleep", "2"])

    source_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            source_backend_root,
            [{"op": "inspect_path", "path": source_backend_file}],
        ),
    )
    _print_response("file-posix metadata source worker", source_response)
    source_results = source_response.get("results", [])
    source_entry = next((item for item in source_results if item.get("op") == "inspect_path" and item.get("ok")), {})
    source_mode_bits = int(source_entry.get("mode_bits") or 0)
    source_uid = int(source_entry.get("uid") or 0)
    source_gid = int(source_entry.get("gid") or 0)
    if not source_mode_bits or (not source_uid and not source_gid):
        mount_stat_text = _run_local_text(["sudo", "-n", "stat", "--format=%a:%u:%g", mount_file])
        mount_mode_bits_text, mount_uid_text, mount_gid_text = mount_stat_text.split(":", 2)
        if not source_mode_bits:
            source_mode_bits = int(mount_mode_bits_text or "0", 8)
        if not source_uid:
            source_uid = int(mount_uid_text or 0)
        if not source_gid:
            source_gid = int(mount_gid_text or 0)

    divergent_mode_bits = source_mode_bits ^ 0o200 or (source_mode_bits ^ 0o100)
    second_divergent_mode_bits = source_mode_bits ^ 0o040 or (source_mode_bits ^ 0o020)
    mismatch_brick_offline = False

    if hold_mismatch_offline:
        _run_remote_host_ops(
            mismatch_host,
            ["brick-down", "--volume", volume, "--brick", mismatch_backend_root],
            ssh_user=ssh_user,
            host_ops_path=str(DEFAULT_HOST_OPS_PATH),
        )
        mismatch_brick_offline = not _brick_status_is_online(volume, mismatch_host, mismatch_backend_root, ssh_user=ssh_user)
        if not mismatch_brick_offline:
            raise RuntimeError(f"brick-down did not take {mismatch_host} offline")
        _run_local(["sleep", "3"])

    try:
        mismatch_response = _canary_worker_command(
            mismatch_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                mismatch_backend_root,
                [{"op": "chmod", "path": mismatch_backend_file, "mode_bits": f"{divergent_mode_bits:04o}"}],
            ),
        )
        _print_response("file-posix metadata mismatch worker", mismatch_response)
        _require_successful_worker_response("file POSIX metadata mismatch worker", mismatch_response)
    except Exception:
        if mismatch_brick_offline:
            _run_remote_host_ops(
                mismatch_host,
                ["brick-kick", "--volume", volume],
                ssh_user=ssh_user,
                host_ops_path=str(DEFAULT_HOST_OPS_PATH),
            )
        raise

    additional_offline_hosts: list[str] = []
    second_root = backend_roots[second_mismatch_host]
    second_file = f"{second_root}/{scenario}/{dir_name}/{file_name}"
    try:
        _run_remote_host_ops(
            second_mismatch_host,
            ["brick-down", "--volume", volume, "--brick", second_root],
            ssh_user=ssh_user,
            host_ops_path=str(DEFAULT_HOST_OPS_PATH),
        )
        if _brick_status_is_online(volume, second_mismatch_host, second_root, ssh_user=ssh_user):
            raise RuntimeError(f"brick-down did not take {second_mismatch_host} offline")
        additional_offline_hosts.append(second_mismatch_host)
        second_response = _canary_worker_command(
            second_mismatch_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                second_root,
                [{"op": "chmod", "path": second_file, "mode_bits": f"{second_divergent_mode_bits:04o}"}],
            ),
        )
        _print_response("file-posix metadata second mismatch worker", second_response)
        _require_successful_worker_response("file POSIX metadata second mismatch worker", second_response)
    except Exception:
        for host in [mismatch_host, *additional_offline_hosts]:
            _run_remote_host_ops(host, ["brick-kick", "--volume", volume], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))
        raise

    backend_by_host = {host: f"{backend_roots[host]}/{scenario}/{dir_name}/{file_name}" for host in brick_hosts}
    _write_state(
        volume,
        scenario,
        {
            "kind": "file-posix-metadata",
            "volume": volume,
            "scenario": scenario,
            "construction_class": "backend-audit",
            "proof_label": "native-heal-smoke",
            "mismatch_host": mismatch_host,
            "source_host": source_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_target": source_backend_file,
            "backend_by_host": backend_by_host,
            "fields": "no_majority_mode",
            "hold_mismatch_offline": hold_mismatch_offline,
            "mismatch_brick_offline": mismatch_brick_offline,
            "additional_offline_hosts": additional_offline_hosts,
            "source_mode_bits": source_mode_bits,
            "source_uid": source_uid,
            "source_gid": source_gid,
            "divergent_mode_bits": divergent_mode_bits,
            "second_divergent_mode_bits": second_divergent_mode_bits,
        },
    )

    print("\n".join([
        "Created gtest file POSIX metadata canary:",
        f"  volume: {volume}",
        f"  scenario: {scenario}",
        f"  logical path: {mount_file}",
        f"  construction class: backend-audit",
        f"  allowed proof label: native-heal-smoke",
        f"  source host: {source_host}",
        f"  mismatch host: {mismatch_host}",
        f"  second mismatch host: {second_mismatch_host}",
        f"  fields: no_majority_mode",
        f"  mismatch brick offline: {'yes' if mismatch_brick_offline else 'no'}",
        f"  additional offline hosts: {', '.join(additional_offline_hosts) or '(none)'}",
        f"  source tuple: mode={source_mode_bits:04o} uid={source_uid} gid={source_gid}",
        f"  divergent tuple: mode={divergent_mode_bits:04o} uid={source_uid} gid={source_gid}",
        f"  second divergent mode: {second_divergent_mode_bits:04o}",
        f"  backend target: {source_backend_file}",
        "Expected: diagnostic only; before mount access backend evidence shows no majority, but Gluster may lookup-heal this zero-AFR drift from the client's local/read brick.",
        f"Evidence route: run repair-meta --backend-path {source_backend_file} --volume {volume} while both mismatch bricks remain offline.",
        "Cleanup: restore the clean snapshot after recording native-heal behavior; do not count this shape as repair proof.",
    ]))


def _create_file_posix_native_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None,
    dir_name: str,
    file_name: str,
    fields: str,
    hold_mismatch_offline: bool,
    ssh_user: str,
    worker_path: str,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for file POSIX metadata canary on {volume}")

    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    source_hosts = [host for host in brick_hosts if host != mismatch_host]
    source_host = source_hosts[0]
    source_backend_root = backend_roots[source_host]
    mismatch_backend_root = backend_roots[mismatch_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    source_backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"

    _require_posix_canary_readiness(volume, ssh_user=ssh_user)
    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root, acl_mount=fields == "acl")
    _local_mount_dir(mount_dir)
    _local_write_file(mount_file, "Initial content")
    _run_local(["sudo", "-n", "stat", mount_file])
    _run_local(["sleep", "2"])

    source_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(volume, scenario, source_backend_root, [{"op": "inspect_path", "path": source_backend_file}]),
    )
    _print_response("file-posix metadata source worker", source_response)
    source_entry = next((item for item in source_response.get("results", []) if item.get("op") == "inspect_path" and item.get("ok")), {})
    source_mode_bits = int(source_entry.get("mode_bits") or 0)
    source_uid = int(source_entry.get("uid") or 0)
    source_gid = int(source_entry.get("gid") or 0)
    source_acl_access_text = str(source_entry.get("acl_access") or "")
    source_acl_default_text = str(source_entry.get("acl_default") or "")
    if not source_mode_bits or (not source_uid and not source_gid):
        mount_stat_text = _run_local_text(["sudo", "-n", "stat", "--format=%a:%u:%g", mount_file])
        mount_mode_bits_text, mount_uid_text, mount_gid_text = mount_stat_text.split(":", 2)
        if not source_mode_bits:
            source_mode_bits = int(mount_mode_bits_text or "0", 8)
        if not source_uid:
            source_uid = int(mount_uid_text or 0)
        if not source_gid:
            source_gid = int(mount_gid_text or 0)

    divergent_mode_bits = source_mode_bits ^ 0o200 or (source_mode_bits ^ 0o100)
    divergent_uid = 0 if source_uid != 0 else 1
    divergent_gid = 0 if source_gid != 0 else 1
    divergent_acl_text = "user::rw-\nuser:1:r--\ngroup::r--\nmask::r--\nother::r--\n"

    mismatch_brick_offline = False
    if hold_mismatch_offline:
        _run_remote_host_ops(mismatch_host, ["brick-down", "--volume", volume, "--brick", mismatch_backend_root], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))
        mismatch_brick_offline = not _brick_status_is_online(volume, mismatch_host, mismatch_backend_root, ssh_user=ssh_user)
        if not mismatch_brick_offline:
            raise RuntimeError(f"brick-down did not take {mismatch_host} offline")
        _run_local(["sleep", "3"])

    try:
        if fields in {"owner", "combined"}:
            _run_local(["sudo", "-n", "chown", f"{divergent_uid}:{divergent_gid}", "--", mount_file])
        if fields in {"mode", "combined"}:
            _run_local(["sudo", "-n", "chmod", f"{divergent_mode_bits:04o}", mount_file])
        if fields == "acl":
            _local_setfacl(mount_file, divergent_acl_text)
    except Exception as exc:
        if not _is_transport_endpoint_disconnect_error(exc):
            if mismatch_brick_offline:
                _run_remote_host_ops(mismatch_host, ["brick-kick", "--volume", volume], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))
            raise
        print("  note: mount-side metadata change reported Transport endpoint is not connected; continuing because heal info already surfaced the pending shape")

    if mismatch_brick_offline:
        _run_remote_host_ops(mismatch_host, ["brick-kick", "--volume", volume], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))

    backend_by_host = {host: f"{backend_roots[host]}/{scenario}/{dir_name}/{file_name}" for host in brick_hosts}
    afr_pending_xattrs_by_host: dict[str, list[dict[str, object]]] = {}
    for host in source_hosts:
        afr_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_roots[host],
                [
                    {
                        "op": "inspect_afr_state",
                        "path": backend_by_host[host],
                    },
                ],
            ),
        )
        _print_response("file-posix metadata afr worker", afr_response)
        afr_entry = next(
            (
                item
                for item in afr_response.get("results", [])
                if item.get("op") == "inspect_afr_state" and item.get("ok")
            ),
            {},
        )
        afr_pending_xattrs_by_host[host] = list(afr_entry.get("afr_xattrs") or [])
    proof_label = "native-heal-first" if mismatch_brick_offline else "native-heal-smoke"
    heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    _trigger_canary_heal(volume, scenario)
    heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    _write_state(
        volume,
        scenario,
        {
            "kind": "file-posix-metadata",
            "volume": volume,
            "scenario": scenario,
            "construction_class": "native-transaction",
            "proof_label": proof_label,
            "mismatch_host": mismatch_host,
            "source_host": source_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_target": source_backend_file,
            "backend_by_host": backend_by_host,
            "fields": fields,
            "hold_mismatch_offline": hold_mismatch_offline,
            "mismatch_brick_offline": mismatch_brick_offline,
            "source_mode_bits": source_mode_bits,
            "source_uid": source_uid,
            "source_gid": source_gid,
            "source_acl_access_text": source_acl_access_text,
            "source_acl_default_text": source_acl_default_text,
            "divergent_mode_bits": divergent_mode_bits if fields in {"mode", "combined"} else source_mode_bits,
            "divergent_uid": divergent_uid if fields in {"owner", "combined"} else source_uid,
            "divergent_gid": divergent_gid if fields in {"owner", "combined"} else source_gid,
            "divergent_acl_text": divergent_acl_text if fields == "acl" else "",
            "afr_pending_xattrs_by_host": afr_pending_xattrs_by_host,
            "heal_info_before": heal_info_before,
            "heal_info_after": heal_info_after,
        },
    )

    print("\n".join([
        "Created gtest file POSIX metadata canary:",
        f"  volume: {volume}",
        f"  scenario: {scenario}",
        f"  logical path: {mount_file}",
        f"  construction class: native-transaction",
        f"  allowed proof label: {proof_label}",
        f"  source host: {source_host}",
        f"  mismatch host: {mismatch_host}",
        f"  fields: {fields}",
        f"  mismatch brick offline: {'yes' if mismatch_brick_offline else 'no'}",
        f"  source tuple: mode={source_mode_bits:04o} uid={source_uid} gid={source_gid}",
        f"  divergent tuple: mode={(divergent_mode_bits if fields in {'mode', 'combined'} else source_mode_bits):04o} uid={(divergent_uid if fields in {'owner', 'combined'} else source_uid)} gid={(divergent_gid if fields in {'owner', 'combined'} else source_gid)}",
        f"  source ACL access: {source_acl_access_text or '(none)'}" if fields == 'acl' else "  source ACL access: (not requested)",
        f"  source ACL default: {source_acl_default_text or '(none)'}" if fields == 'acl' else "  source ACL default: (not requested)",
        f"  backend target: {source_backend_file}",
        f"  heal info before heal crawl:\n{heal_info_before or '(empty)'}",
        f"  heal info after heal crawl:\n{heal_info_after or '(empty)'}",
        ("Expected: the repair tool should treat this as native-heal-first metadata evidence after the next heal crawl settles." if proof_label == "native-heal-first" else "Expected: this is native-heal-smoke only; Gluster may normalize it without leaving a durable pending row."),
        f"Evidence route: run sudo -n gluster volume heal {volume} info or heal statistics after the mismatch brick returns.",
        "Cleanup: execute the heal-first check, then restore the clean snapshot.",
    ]))


def create_file_posix_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    fields: str = "combined",
    hold_mismatch_offline: bool = True,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    if fields == "no_majority_mode":
        _create_file_posix_no_majority_mode_canary(
            volume=volume,
            scenario=scenario,
            mismatch_host=mismatch_host,
            dir_name=dir_name,
            file_name=file_name,
            hold_mismatch_offline=hold_mismatch_offline,
            ssh_user=ssh_user,
            worker_path=worker_path,
        )
        return
    if fields not in {"mode", "owner", "acl", "combined"}:
        raise ValueError("file POSIX metadata fields must be mode, owner, acl, combined, or no_majority_mode")
    _create_file_posix_native_metadata_canary(
        volume=volume,
        scenario=scenario,
        mismatch_host=mismatch_host,
        dir_name=dir_name,
        file_name=file_name,
        fields=fields,
        hold_mismatch_offline=hold_mismatch_offline,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )

def create_file_posix_acl_majority_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    hold_mismatch_offline: bool = True,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    create_file_posix_metadata_canary(
        volume=volume,
        scenario=scenario,
        mismatch_host=mismatch_host,
        dir_name=dir_name,
        file_name=file_name,
        fields="acl",
        hold_mismatch_offline=hold_mismatch_offline,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_file_native_pending_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for file native-pending metadata canary on {volume}")

    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    source_hosts = [host for host in brick_hosts if host != mismatch_host]
    source_host = source_hosts[0]
    source_backend_root = backend_roots[source_host]
    mismatch_backend_root = backend_roots[mismatch_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    source_backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_write_file(mount_file, "Initial content")
    _run_local(["sudo", "-n", "stat", mount_file])
    _run_local(["sudo", "-n", "stat", mount_dir])
    _run_local(["sleep", "2"])

    source_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            source_backend_root,
            [
                {
                    "op": "inspect_path",
                    "path": source_backend_file,
                }
            ],
        ),
    )
    _print_response("file-native-pending source worker", source_response)
    source_results = source_response.get("results", [])
    source_entry = next((item for item in source_results if item.get("op") == "inspect_path" and item.get("ok")), {})
    source_mode_bits = int(source_entry.get("mode_bits") or 0)
    source_uid = int(source_entry.get("uid") or 0)
    source_gid = int(source_entry.get("gid") or 0)
    if not source_mode_bits:
        raise RuntimeError("file native-pending metadata canary worker did not return source mode bits")

    divergent_mode_bits = source_mode_bits ^ 0o200 or (source_mode_bits ^ 0o100)
    _run_remote_host_ops(
        mismatch_host,
        ["brick-down", "--volume", volume, "--brick", mismatch_backend_root],
        ssh_user=ssh_user,
        host_ops_path=str(DEFAULT_HOST_OPS_PATH),
    )
    if _brick_status_is_online(volume, mismatch_host, mismatch_backend_root, ssh_user=ssh_user):
        raise RuntimeError(f"brick-down did not take {mismatch_host} offline")

    try:
        _run_local(["sudo", "-n", "chmod", f"{divergent_mode_bits:04o}", mount_file])
    except Exception as exc:
        message = str(exc)
        if "Transport endpoint is not connected" not in message and "ENOTCONN" not in message:
            _run_remote_host_ops(
                mismatch_host,
                ["brick-kick", "--volume", volume],
                ssh_user=ssh_user,
                host_ops_path=str(DEFAULT_HOST_OPS_PATH),
            )
            raise
        print(
            "  note: mount-side chmod reported Transport endpoint is not connected; "
            "continuing because heal info already surfaced the pending shape"
        )

    _run_remote_host_ops(
        mismatch_host,
        ["brick-kick", "--volume", volume],
        ssh_user=ssh_user,
        host_ops_path=str(DEFAULT_HOST_OPS_PATH),
    )
    backend_by_host = {host: f"{backend_roots[host]}/{scenario}/{dir_name}/{file_name}" for host in source_hosts}
    afr_pending_xattrs_by_host = _capture_afr_pending_xattrs_by_host(
        volume=volume,
        scenario=scenario,
        backend_roots=backend_roots,
        backend_by_host=backend_by_host,
        hosts=source_hosts,
        ssh_user=ssh_user,
        worker_path=worker_path,
        response_label="file-native-pending metadata afr worker",
    )
    heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    _trigger_canary_heal(volume, scenario)
    heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])

    _write_state(
        volume,
        scenario,
        {
            "kind": "file-native-pending-metadata",
            "volume": volume,
            "scenario": scenario,
            "construction_class": "native-transaction",
            "proof_label": "native-heal-first",
            "mismatch_host": mismatch_host,
            "source_host": source_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_target": source_backend_file,
            "backend_by_host": backend_by_host,
            "source_mode_bits": source_mode_bits,
            "source_uid": source_uid,
            "source_gid": source_gid,
            "divergent_mode_bits": divergent_mode_bits,
            "mismatch_brick_offline": True,
            "afr_pending_xattrs_by_host": afr_pending_xattrs_by_host,
            "heal_info_before": heal_info_before,
            "heal_info_after": heal_info_after,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest file native-pending metadata canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  construction class: native-transaction",
                f"  allowed proof label: native-heal-first",
                f"  source host: {source_host}",
                f"  mismatch host: {mismatch_host}",
                f"  source tuple: mode={source_mode_bits:04o} uid={source_uid} gid={source_gid}",
                f"  divergent mode: {divergent_mode_bits:04o}",
                f"  backend target: {source_backend_file}",
                "Expected: the repair tool should treat this as native-heal-first metadata evidence after the next heal crawl settles.",
                f"Evidence route: run sudo -n gluster volume heal {volume} info or heal statistics after the mismatch brick returns.",
                "Cleanup: execute the heal-first check, then restore the clean snapshot.",
            ]
        )
    )



def _create_directory_posix_native_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None,
    dir_name: str,
    child_name: str,
    fields: str,
    ssh_user: str,
    worker_path: str,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for directory POSIX metadata canary on {volume}")

    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    source_hosts = [host for host in brick_hosts if host != mismatch_host]
    source_host = source_hosts[0]
    source_root = backend_roots[source_host]
    mismatch_root = backend_roots[mismatch_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_child = f"{mount_dir}/{child_name}"
    source_dir = f"{source_root}/{scenario}/{dir_name}"

    _require_posix_canary_readiness(volume, ssh_user=ssh_user)
    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root, acl_mount=fields == "default_acl")
    _local_mount_dir(mount_dir)
    _local_write_file(mount_child, "directory metadata seed")
    _run_local(["sudo", "-n", "stat", mount_dir])
    _run_local(["sleep", "2"])

    source_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(volume, scenario, source_root, [{"op": "inspect_path", "path": source_dir}]),
    )
    _print_response("directory-posix metadata source worker", source_response)
    source_entry = next((item for item in source_response.get("results", []) if item.get("op") == "inspect_path" and item.get("ok")), {})
    source_mode_bits = int(source_entry.get("mode_bits") or 0)
    source_acl_access_text = str(source_entry.get("acl_access") or "")
    source_acl_default_text = str(source_entry.get("acl_default") or "")
    if not source_mode_bits:
        raise RuntimeError("directory POSIX metadata canary worker did not return source mode bits")

    divergent_mode_bits = source_mode_bits ^ 0o200 or (source_mode_bits ^ 0o100)
    divergent_acl_text = "\n".join([
        "user::rwx", "group::r-x", "other::r-x",
        "default:user::rwx", "default:user:daemon:r-x", "default:group::r-x",
        "default:mask::r-x", "default:other::---", "",
    ])

    _run_remote_host_ops(mismatch_host, ["brick-down", "--volume", volume, "--brick", mismatch_root], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))
    mismatch_brick_offline = not _brick_status_is_online(volume, mismatch_host, mismatch_root, ssh_user=ssh_user)
    if not mismatch_brick_offline:
        raise RuntimeError(f"brick-down did not take {mismatch_host} offline")

    try:
        if fields == "mode":
            _run_local(["sudo", "-n", "chmod", f"{divergent_mode_bits:04o}", mount_dir])
        else:
            _local_setfacl(mount_dir, divergent_acl_text)
    except Exception as exc:
        if not canary_api._is_transport_endpoint_disconnect_error(exc):
            _run_remote_host_ops(mismatch_host, ["brick-kick", "--volume", volume], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))
            raise
        print(
            "  note: mount-side metadata change reported Transport endpoint is not connected; "
            "continuing because heal info already surfaced the pending shape"
        )

    _run_remote_host_ops(mismatch_host, ["brick-kick", "--volume", volume], ssh_user=ssh_user, host_ops_path=str(DEFAULT_HOST_OPS_PATH))
    backend_by_host = {host: f"{backend_roots[host]}/{scenario}/{dir_name}" for host in source_hosts}
    afr_pending_xattrs_by_host = _capture_afr_pending_xattrs_by_host(
        volume=volume,
        scenario=scenario,
        backend_roots=backend_roots,
        backend_by_host=backend_by_host,
        hosts=source_hosts,
        ssh_user=ssh_user,
        worker_path=worker_path,
        response_label="directory-posix metadata afr worker",
    )
    heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    _trigger_canary_heal(volume, scenario)
    heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])

    _write_state(
        volume,
        scenario,
        {
            "kind": "directory-posix-metadata",
            "volume": volume,
            "scenario": scenario,
            "construction_class": "native-transaction",
            "proof_label": "native-heal-first",
            "mismatch_host": mismatch_host,
            "source_host": source_host,
            "dir_name": dir_name,
            "child_name": child_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "backend_root": source_root,
            "backend_roots": backend_roots,
            "backend_target": source_dir,
            "backend_by_host": backend_by_host,
            "fields": fields,
            "source_mode_bits": source_mode_bits,
            "source_acl_access_text": source_acl_access_text,
            "source_acl_default_text": source_acl_default_text,
            "divergent_mode_bits": divergent_mode_bits if fields == "mode" else source_mode_bits,
            "divergent_acl_text": divergent_acl_text if fields == "default_acl" else "",
            "mismatch_brick_offline": True,
            "afr_pending_xattrs_by_host": afr_pending_xattrs_by_host,
            "heal_info_before": heal_info_before,
            "heal_info_after": heal_info_after,
        },
    )

    print("\n".join([
        "Created gtest directory POSIX metadata canary:",
        f"  volume: {volume}",
        f"  scenario: {scenario}",
        f"  logical path: {mount_dir}",
        f"  seeded child: {mount_child}",
        f"  construction class: native-transaction",
        f"  allowed proof label: native-heal-first",
        f"  source host: {source_host}",
        f"  mismatch host: {mismatch_host}",
        f"  fields: {fields}",
        "  mismatch brick offline: yes",
        f"  source mode: {source_mode_bits:04o}",
        f"  divergent mode: {(divergent_mode_bits if fields == 'mode' else source_mode_bits):04o}",
        f"  source ACL access: {source_acl_access_text or '(none)'}" if fields == 'default_acl' else "  source ACL access: (not requested)",
        f"  source ACL default: {source_acl_default_text or '(none)'}" if fields == 'default_acl' else "  source ACL default: (not requested)",
        f"  backend target: {source_dir}",
        f"  heal info before heal crawl:\n{heal_info_before or '(empty)'}",
        f"  heal info after heal crawl:\n{heal_info_after or '(empty)'}",
        "Expected: structural child evidence agrees and the repair tool promotes strict-majority directory POSIX metadata alignment.",
        f"Evidence route: run sudo -n gluster volume heal {volume} info or heal statistics after the mismatch brick returns.",
        "Cleanup: execute the heal-first check, then restore the clean snapshot.",
    ]))



def create_directory_posix_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    child_name: str = "metadata-seed.txt",
    fields: str = "mode",
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    if fields not in {"mode", "default_acl"}:
        raise ValueError("directory POSIX metadata fields must be mode or default_acl")
    _create_directory_posix_native_metadata_canary(
        volume=volume,
        scenario=scenario,
        mismatch_host=mismatch_host,
        dir_name=dir_name,
        child_name=child_name,
        fields=fields,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )

def create_directory_posix_default_acl_majority_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    child_name: str = "metadata-seed.txt",
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    create_directory_posix_metadata_canary(
        volume=volume,
        scenario=scenario,
        mismatch_host=mismatch_host,
        dir_name=dir_name,
        child_name=child_name,
        fields="default_acl",
        ssh_user=ssh_user,
        worker_path=worker_path,
    )



def create_file_metadata_split_brain_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    trigger_heal: bool = True,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 4:
        raise RuntimeError(f"need at least 4 bricks for file metadata split-brain canary on {volume}")

    left_hosts = brick_hosts[:2]
    right_hosts = brick_hosts[2:4]
    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    if mismatch_host in left_hosts:
        canonical_hosts = left_hosts
        divergent_hosts = right_hosts
    else:
        canonical_hosts = right_hosts
        divergent_hosts = left_hosts
    if not canonical_hosts or not divergent_hosts:
        raise RuntimeError(f"need two brick cohorts for file metadata split-brain canary on {volume}")
    marker_host = canonical_hosts[0]
    canonical_backend_root = backend_roots[canonical_hosts[0]]
    divergent_backend_root = backend_roots[divergent_hosts[0]]
    index_host = marker_host

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{canonical_backend_root}/{scenario}/{dir_name}/{file_name}"
    canonical_mdata_hex = ""
    divergent_mdata_hex = _new_gfid_hex()
    metadata_pending_value = "000000000000000100000000"
    index_root = f"{canonical_backend_root}/.glusterfs/indices/xattrop"
    afr_pending_xattrs_by_host: dict[str, list[dict[str, object]]] = {}

    if trigger_heal:
        _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    else:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_write_file(mount_file, "Initial content")
    _run_local(["sudo", "-n", "stat", mount_file])
    time.sleep(3)

    marker_response: dict[str, Any] = {}
    for _ in range(20):
        marker_response = _canary_worker_command(
            marker_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                canonical_backend_root,
                [
                    {
                        "op": "getxattr",
                        "path": backend_file,
                        "name": "trusted.glusterfs.mdata",
                    },
                ],
            ),
        )
        marker_results = marker_response.get("results", [])
        mdata_entry = next(
            (
                item
                for item in marker_results
                if item.get("op") == "getxattr"
                and item.get("ok")
                and str(item.get("name") or "") == "trusted.glusterfs.mdata"
            ),
            {},
        )
        canonical_mdata_hex = str(mdata_entry.get("value_hex") or "")
        if canonical_mdata_hex:
            break
        time.sleep(1)
    _print_response("file-metadata-split-brain marker worker", marker_response)
    _require_successful_worker_response("file-metadata-split-brain marker worker", marker_response)
    if not canonical_mdata_hex:
        raise RuntimeError("file metadata split-brain canary worker did not return ctime metadata; ensure ctime is enabled")

    for host in canonical_hosts:
        canonical_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                canonical_backend_root,
                [
                    {
                        "op": "write_text",
                        "path": backend_file,
                        "text": f"{scenario} canonical cohort on {host}\n",
                    },
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": "trusted.glusterfs.mdata",
                        "value_hex": canonical_mdata_hex,
                    },
                    {
                        "op": "set_afr_metadata_pending",
                        "path": backend_file,
                        "pending_names": [
                            f"trusted.afr.{volume}-client-{brick_hosts.index(divergent_hosts[0])}",
                            f"trusted.afr.{volume}-client-{brick_hosts.index(divergent_hosts[1])}",
                        ],
                        "value_hex": metadata_pending_value,
                    },
                    {
                        "op": "inspect_afr_state",
                        "path": backend_file,
                    },
                ],
            ),
        )
        _print_response("file-metadata-split-brain canonical worker", canonical_response)
        _require_successful_worker_response("file-metadata-split-brain canonical worker", canonical_response)
        canonical_afr_entry = next(
            (
                item
                for item in canonical_response.get("results", [])
                if item.get("op") == "inspect_afr_state" and item.get("ok")
            ),
            {},
        )
        afr_pending_xattrs_by_host[host] = list(canonical_afr_entry.get("afr_xattrs") or [])

    for host in divergent_hosts:
        divergent_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                divergent_backend_root,
                [
                    {
                        "op": "write_text",
                        "path": backend_file,
                        "text": f"{scenario} divergent cohort on {host}\n",
                    },
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": "trusted.glusterfs.mdata",
                        "value_hex": divergent_mdata_hex,
                    },
                    {
                        "op": "set_afr_metadata_pending",
                        "path": backend_file,
                        "pending_names": [
                            f"trusted.afr.{volume}-client-{brick_hosts.index(canonical_hosts[0])}",
                            f"trusted.afr.{volume}-client-{brick_hosts.index(canonical_hosts[1])}",
                        ],
                        "value_hex": metadata_pending_value,
                    },
                    {
                        "op": "inspect_afr_state",
                        "path": backend_file,
                    },
                ],
            ),
        )
        _print_response("file-metadata-split-brain divergent worker", divergent_response)
        _require_successful_worker_response("file-metadata-split-brain divergent worker", divergent_response)
        divergent_afr_entry = next(
            (
                item
                for item in divergent_response.get("results", [])
                if item.get("op") == "inspect_afr_state" and item.get("ok")
            ),
            {},
        )
        afr_pending_xattrs_by_host[host] = list(divergent_afr_entry.get("afr_xattrs") or [])

    index_response = _canary_worker_command(
        index_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            canonical_backend_root,
            [
                {
                    "op": "link_xattrop_gfid",
                    "target_path": backend_file,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": backend_file,
                },
            ],
        ),
    )
    _print_response("file-metadata-split-brain index worker", index_response)
    _require_successful_worker_response("file-metadata-split-brain index worker", index_response)
    index_results = index_response.get("results", [])
    index_entry = next((item for item in index_results if item.get("op") == "link_xattrop_gfid" and item.get("ok")), {})
    gfid_entry = next((item for item in index_results if item.get("op") == "link_file_gfid_from_target" and item.get("ok")), {})
    canonical_gfid = str(index_entry.get("value_uuid") or gfid_entry.get("value_uuid") or "")
    index_entry_path = str(index_entry.get("index_entry") or "")
    file_gfid_path = str(gfid_entry.get("file_gfid_path") or "")
    if not canonical_gfid:
        raise RuntimeError("file metadata split-brain canary worker did not return a canonical file GFID")
    if not index_entry_path:
        index_entry_path = f"{index_root}/{canonical_gfid}"
    if not file_gfid_path:
        file_gfid_path = _file_gfid_link_path(canonical_backend_root, canonical_gfid)

    state_payload = {
        "kind": "file-metadata-split-brain",
        "leave_heal_pending": not trigger_heal,
        "volume": volume,
        "scenario": scenario,
        "construction_class": "afr-synthesized",
        "proof_label": "afr-synthesized",
        "mismatch_host": mismatch_host,
        "marker_host": marker_host,
        "dir_name": dir_name,
        "file_name": file_name,
        "mount_root": mount_root,
        "mount_target": mount_dir,
        "mount_file": mount_file,
        "backend_root": canonical_backend_root,
        "backend_roots": backend_roots,
        "backend_target": backend_file,
        "canonical_hosts": canonical_hosts,
        "divergent_hosts": divergent_hosts,
        "canonical_mdata_hex": canonical_mdata_hex,
        "divergent_mdata_hex": divergent_mdata_hex,
        "metadata_pending_value": metadata_pending_value,
        "afr_pending_xattrs_by_host": afr_pending_xattrs_by_host,
        "index_root": index_root,
        "index_host": index_host,
        "index_entry": index_entry_path,
        "file_gfid_path": file_gfid_path,
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
    }
    _record_file_baseline(volume, scenario, state_payload)
    _write_state(volume, scenario, state_payload)

    if not trigger_heal:
        state_payload.update(
            {
                "heal_info_before": "",
                "heal_info_split_brain_before": "",
                "heal_info_after": "",
                "heal_info_split_brain_after": "",
                "heal_info_contains_mount_file_before": False,
                "heal_info_contains_mount_file_after": False,
                "heal_info_split_brain_contains_mount_file_before": False,
                "heal_info_split_brain_contains_mount_file_after": False,
                "proof_label": "harness-only-pending",
            }
        )
        _write_state(volume, scenario, state_payload)
        print(
            "Leaving heal settings disabled; no heal crawl was launched. "
            "Use independent operator evidence to classify this fixture."
        )
        return

    heal_info_before = ""
    heal_info_split_brain_before = ""
    heal_visibility_deadline = time.monotonic() + 30
    while True:
        heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
        heal_info_split_brain_before = _run_local_text(
            ["sudo", "-n", "gluster", "volume", "heal", volume, "info", "split-brain"]
        )
        if _heal_info_contains_path(heal_info_before, mount_file, mount_root=mount_root, gfid=canonical_gfid) or _heal_info_contains_path(
            heal_info_split_brain_before,
            mount_file,
            mount_root=mount_root,
            gfid=canonical_gfid,
        ):
            break
        if time.monotonic() >= heal_visibility_deadline:
            break
        time.sleep(1)
    print(f"Triggering full heal crawl for canary: volume={volume} scenario={scenario}")
    heal_trigger_error = ""
    try:
        run_heal(volume, settle_seconds=0)
    except RuntimeError as exc:
        message = str(exc).strip()
        if "self-heal-daemon is disabled" in message.lower():
            print(f"  heal crawl blocked for canary: enabling heal settings on {volume} and retrying")
            try:
                set_heal_settings(volume, True)
            except RuntimeError as settings_exc:
                heal_trigger_error = str(settings_exc).strip()
                print(f"  heal crawl error: {heal_trigger_error}")
            else:
                try:
                    run_heal(volume, settle_seconds=0)
                except RuntimeError as retry_exc:
                    heal_trigger_error = str(retry_exc).strip()
                    print(f"  heal crawl error: {heal_trigger_error}")
        else:
            heal_trigger_error = message
            print(f"  heal crawl error: {heal_trigger_error}")
    heal_info_after = ""
    heal_info_split_brain_after = ""
    heal_visibility_deadline = time.monotonic() + 30
    while True:
        heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
        heal_info_split_brain_after = _run_local_text(
            ["sudo", "-n", "gluster", "volume", "heal", volume, "info", "split-brain"]
        )
        if _heal_info_contains_path(heal_info_after, mount_file, mount_root=mount_root, gfid=canonical_gfid) or _heal_info_contains_path(
            heal_info_split_brain_after,
            mount_file,
            mount_root=mount_root,
            gfid=canonical_gfid,
        ):
            break
        if time.monotonic() >= heal_visibility_deadline:
            break
        time.sleep(1)
    heal_info_contains_mount_file_before = _heal_info_contains_path(heal_info_before, mount_file, mount_root=mount_root, gfid=canonical_gfid)
    heal_info_contains_mount_file_after = _heal_info_contains_path(heal_info_after, mount_file, mount_root=mount_root, gfid=canonical_gfid)
    heal_info_split_brain_contains_mount_file_before = _heal_info_contains_path(
        heal_info_split_brain_before,
        mount_file,
        mount_root=mount_root,
        gfid=canonical_gfid,
    )
    heal_info_split_brain_contains_mount_file_after = _heal_info_contains_path(
        heal_info_split_brain_after,
        mount_file,
        mount_root=mount_root,
        gfid=canonical_gfid,
    )
    durable_split_brain = bool(
        (
            heal_info_contains_mount_file_before
            or heal_info_split_brain_contains_mount_file_before
        )
        and (
            heal_info_contains_mount_file_after
            or heal_info_split_brain_contains_mount_file_after
        )
    )

    state_payload.update(
        {
            "heal_info_before": heal_info_before,
            "heal_info_split_brain_before": heal_info_split_brain_before,
            "heal_info_after": heal_info_after,
            "heal_info_split_brain_after": heal_info_split_brain_after,
            "heal_info_contains_mount_file_before": heal_info_contains_mount_file_before,
            "heal_info_contains_mount_file_after": heal_info_contains_mount_file_after,
            "heal_info_split_brain_contains_mount_file_before": heal_info_split_brain_contains_mount_file_before,
            "heal_info_split_brain_contains_mount_file_after": heal_info_split_brain_contains_mount_file_after,
        }
    )
    if heal_trigger_error:
        state_payload["heal_trigger_error"] = heal_trigger_error
    state_payload["proof_label"] = "afr-synthesized" if durable_split_brain else "native-heal-smoke"
    _write_state(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                "Created gtest file-metadata split-brain canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  construction class: afr-synthesized",
                f"  allowed proof label: {state_payload['proof_label']}",
                f"  logical path: {mount_file}",
                f"  mismatch host: {mismatch_host}",
                f"  marker host: {marker_host}",
                f"  canonical cohort: {', '.join(canonical_hosts)} -> {canonical_mdata_hex}",
                f"  divergent cohort: {', '.join(divergent_hosts)} -> {divergent_mdata_hex}",
                f"  metadata pending value: 0x{metadata_pending_value}",
                f"  backend target: {backend_file}",
                f"  index bucket: {index_root}",
                f"  index entry: {index_entry_path}",
                f"  file GFID path: {file_gfid_path}",
                f"  canonical file GFID: {canonical_gfid}",
                f"  heal info before heal crawl:\n{heal_info_before or '(empty)'}",
                f"  heal info split-brain before heal crawl:\n{heal_info_split_brain_before or '(empty)'}",
                f"  heal info after heal crawl:\n{heal_info_after or '(empty)'}",
                f"  heal info split-brain after heal crawl:\n{heal_info_split_brain_after or '(empty)'}",
                f"  heal info contains canary path before heal crawl: {'yes' if heal_info_contains_mount_file_before else 'no'}",
                f"  heal info contains canary path after heal crawl: {'yes' if heal_info_contains_mount_file_after else 'no'}",
                f"  heal info split-brain contains canary path before heal crawl: {'yes' if heal_info_split_brain_contains_mount_file_before else 'no'}",
                f"  heal info split-brain contains canary path after heal crawl: {'yes' if heal_info_split_brain_contains_mount_file_after else 'no'}",
                (
                    "Expected: Gluster should expose this as a metadata divergence worth review after the crawl, not a blind auto-heal."
                    if durable_split_brain
                    else "Expected: this stayed diagnostic/native-heal-smoke; keep it out of repair acceptance until Gluster leaves a durable heal-info row."
                ),
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def build_file_metadata_split_brain_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = _read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind != "file-metadata-split-brain":
        raise RuntimeError(f"scenario {scenario!r} is not a file metadata split-brain canary")

    mount_root = str(state.get("mount_root") or _volume_mount_root(volume))
    mount_file = str(state.get("mount_file") or "").strip()
    backend_root = str(state.get("backend_root") or "").strip()
    backend_target = str(state.get("backend_target") or "").strip()
    canonical_hosts = [str(host) for host in state.get("canonical_hosts") or [] if str(host).strip()]
    divergent_hosts = [str(host) for host in state.get("divergent_hosts") or [] if str(host).strip()]
    canonical_mdata_hex = str(state.get("canonical_mdata_hex") or "").strip()
    divergent_mdata_hex = str(state.get("divergent_mdata_hex") or "").strip()
    marker_host = str(state.get("marker_host") or "").strip()
    mismatch_host = str(state.get("mismatch_host") or "").strip()
    metadata_pending_value = str(state.get("metadata_pending_value") or "").strip()
    metadata_pending_display = "(missing)" if not metadata_pending_value else (metadata_pending_value if metadata_pending_value.startswith("0x") else f"0x{metadata_pending_value}")
    afr_pending_xattrs_by_host = state.get("afr_pending_xattrs_by_host") or {}
    brick_roles_by_host = canary_api._brick_roles_by_host(volume, state.get("brick_roles_by_host"))
    heal_info_before = str(state.get("heal_info_before") or "").strip()
    heal_info_split_brain_before = str(state.get("heal_info_split_brain_before") or "").strip()
    heal_info_after = str(state.get("heal_info_after") or "").strip()
    heal_info_contains_mount_file_before = bool(state.get("heal_info_contains_mount_file_before"))
    heal_info_contains_mount_file_after = bool(state.get("heal_info_contains_mount_file_after"))
    heal_info_split_brain_contains_mount_file_before = bool(state.get("heal_info_split_brain_contains_mount_file_before"))
    heal_info_split_brain_contains_mount_file_after = bool(state.get("heal_info_split_brain_contains_mount_file_after"))
    heal_info_split_brain_after = str(state.get("heal_info_split_brain_after") or "").strip()
    proof_label = str(state.get("proof_label") or "").strip()
    durable_split_brain = bool(
        (
            heal_info_contains_mount_file_before
            or heal_info_split_brain_contains_mount_file_before
        )
        and (
            heal_info_contains_mount_file_after
            or heal_info_split_brain_contains_mount_file_after
        )
    )

    if not mount_file or not backend_target or not canonical_hosts or not divergent_hosts:
        raise RuntimeError(f"scenario {scenario!r} does not record enough file metadata split-brain state to build a plan")
    if not canonical_mdata_hex or not divergent_mdata_hex:
        raise RuntimeError(f"scenario {scenario!r} is missing canonical or divergent metadata evidence")
    if not all([heal_info_before, heal_info_split_brain_before, heal_info_after, heal_info_split_brain_after]):
        raise RuntimeError(f"scenario {scenario!r} does not record fresh heal info and split-brain snapshots to build a plan")
    if proof_label != "afr-synthesized" or not durable_split_brain:
        raise RuntimeError(
            f"scenario {scenario!r} stayed diagnostic/native-heal-smoke; rerun the canary or reclassify the case as diagnostic until Gluster leaves a durable heal-info row"
        )

    file_copies = []
    for host in canonical_hosts:
        file_copies.append(
            {
                "host": host,
                "backend": backend_target,
                "identity": canonical_mdata_hex,
                "backend_trusted_gfid": canonical_mdata_hex,
                "file_gfid": canonical_mdata_hex,
            }
        )
    for host in divergent_hosts:
        file_copies.append(
            {
                "host": host,
                "backend": backend_target,
                "identity": divergent_mdata_hex,
                "backend_trusted_gfid": divergent_mdata_hex,
                "file_gfid": divergent_mdata_hex,
            }
        )

    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": mount_file,
        "action_type": "review_entry_split_brain",
        "object_type": "file",
        "depth": mount_file.count("/") + 1,
        "repair_strategy": "ambiguous_entry_split_brain_file",
        "mounted_target": mount_file,
        "winner_host": marker_host or (canonical_hosts[0] if canonical_hosts else ""),
        "winner_backend": backend_target,
        "winner_file_gfid": canonical_mdata_hex,
        "healthy_hosts": canonical_hosts,
        "conflict_hosts": divergent_hosts,
        "missing_hosts": [],
        "file_copies": file_copies,
        "brick_roles_by_host": brick_roles_by_host,
        "heal_info_before": heal_info_before,
        "heal_info_split_brain_before": heal_info_split_brain_before,
        "heal_info_after": heal_info_after,
        "heal_info_split_brain_after": heal_info_split_brain_after,
        "canary_proof_label": proof_label,
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            "file-metadata-split-brain marker: preserve-first quarantine proof",
            f"mismatch host: {mismatch_host or 'unknown'}",
            f"marker host: {marker_host or 'unknown'}",
            f"backend root: {backend_root or 'unknown'}",
            f"canonical metadata: {canonical_mdata_hex}",
            f"divergent metadata: {divergent_mdata_hex}",
            f"metadata pending value: {metadata_pending_display}",
            *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
            f"heal info before split-brain crawl: {heal_info_before or '(empty)'}",
            f"heal info split-brain before split-brain crawl: {heal_info_split_brain_before or '(empty)'}",
            f"heal info after split-brain crawl: {heal_info_after or '(empty)'}",
            f"heal info contains canary path before split-brain crawl: {'yes' if heal_info_contains_mount_file_before else 'no'}",
            f"heal info contains canary path after split-brain crawl: {'yes' if heal_info_contains_mount_file_after else 'no'}",
            f"heal info split-brain contains canary path before split-brain crawl: {'yes' if heal_info_split_brain_contains_mount_file_before else 'no'}",
            f"heal info split-brain contains canary path after split-brain crawl: {'yes' if heal_info_split_brain_contains_mount_file_after else 'no'}",
            f"heal info split-brain after split-brain crawl: {heal_info_split_brain_after or '(empty)'}",
            f"canary proof label: {proof_label or '(missing)'}",
            *canary_api._format_afr_pending_xattrs_by_host_for_notes(afr_pending_xattrs_by_host),
            "Expected: review first, then quarantine or replace according to policy.",
        ],
    }

    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": [action],
    }




def _capture_file_posix_metadata_by_host(
    *,
    volume: str,
    scenario: str,
    backend_by_host: dict[str, str],
    hosts: list[str],
    ssh_user: str,
    worker_path: str,
) -> dict[str, dict[str, object]]:
    metadata_by_host: dict[str, dict[str, object]] = {}
    for host in hosts:
        response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_by_host[host],
                [
                    {
                        "op": "inspect_path",
                        "label": "posix_tuple",
                        "path": backend_by_host[host],
                    }
                ],
            ),
        )
        _print_response("file-posix split-brain worker " + host, response)
        entry = next(
            (
                item
                for item in response.get("results", [])
                if item.get("op") == "inspect_path" and item.get("ok")
            ),
            {},
        )
        if not entry:
            raise RuntimeError(f"file POSIX split-brain canary worker did not return a tuple for {host}")
        acl_error = str(entry.get("acl_error") or "").strip()
        if acl_error:
            raise RuntimeError(f"file POSIX split-brain canary ACL inspection failed on {host}: {acl_error}")
        metadata_by_host[host] = {
            "backend": str(entry.get("path") or backend_by_host[host]),
            "mode_bits": int(entry.get("mode_bits") or 0) if entry.get("mode_bits") is not None else None,
            "uid": int(entry.get("uid") or 0) if entry.get("uid") is not None else None,
            "gid": int(entry.get("gid") or 0) if entry.get("gid") is not None else None,
            "acl_access": str(entry.get("acl_access") or ""),
            "acl_default": str(entry.get("acl_default") or ""),
        }
    return metadata_by_host


def _posix_metadata_fields_to_align(metadata_by_host: dict[str, dict[str, object]], source_host: str) -> list[str]:
    if not metadata_by_host:
        return []
    source_host = source_host if source_host in metadata_by_host else sorted(metadata_by_host)[0]
    source_record = metadata_by_host.get(source_host) or {}
    field_pairs = [
        ("mode", "mode_bits"),
        ("uid", "uid"),
        ("gid", "gid"),
        ("acl_access", "acl_access"),
        ("acl_default", "acl_default"),
    ]
    fields_to_align: list[str] = []
    for public_name, record_key in field_pairs:
        if any((record or {}).get(record_key) != source_record.get(record_key) for record in metadata_by_host.values()):
            fields_to_align.append(public_name)
    return fields_to_align


def _posix_metadata_split_brain_topology(
    brick_hosts: list[str],
    requested_source_host: str | None,
) -> tuple[str, dict[str, list[str]], dict[str, int]]:
    """Return the no-source AFR accusation matrix for supported replica widths."""
    if len(brick_hosts) == 3:
        source_host = _select_role_host(
            brick_hosts,
            requested_source_host,
            role="source",
            default_to_next=True,
        )
        metadata_variant_by_host = {source_host: 0}
        metadata_variant_by_host.update(
            {
                host: index
                for index, host in enumerate(
                    (candidate for candidate in brick_hosts if candidate != source_host),
                    start=1,
                )
            }
        )
        return (
            source_host,
            {
                host: [candidate for candidate in brick_hosts if candidate != host]
                for host in brick_hosts
            },
            metadata_variant_by_host,
        )
    if len(brick_hosts) >= 4:
        selected_host = _select_role_host(
            brick_hosts,
            requested_source_host,
            role="source",
            default_to_next=True,
        )
        left_hosts = brick_hosts[:2]
        right_hosts = brick_hosts[2:4]
        if selected_host in left_hosts:
            source_hosts, other_hosts = left_hosts, right_hosts
        else:
            source_hosts, other_hosts = right_hosts, left_hosts
        return (
            source_hosts[0],
            {
                host: list(other_hosts) if host in source_hosts else list(source_hosts)
                for host in [*source_hosts, *other_hosts]
            },
            {
                host: 0 if host in source_hosts else 1
                for host in [*source_hosts, *other_hosts]
            },
        )
    raise RuntimeError("need at least three bricks for file POSIX metadata split-brain canary")


def create_file_posix_metadata_split_brain_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    leave_heal_pending: bool = False,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    source_host, pending_targets_by_host, metadata_variant_by_host = _posix_metadata_split_brain_topology(
        brick_hosts,
        mismatch_host,
    )
    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="source", default_to_next=True)
    source_backend_root = backend_roots[source_host]
    source_backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"
    backend_by_host = {host: f"{backend_roots[host]}/{scenario}/{dir_name}/{file_name}" for host in brick_hosts}
    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    index_root = f"{source_backend_root}/.glusterfs/indices/xattrop"
    metadata_pending_value = "000000000000000100000000"

    # Persist enough brick-side context to clean up even if the first FUSE write fails.
    _write_state(
        volume,
        scenario,
        {
            "kind": "file-posix-metadata-split-brain",
            "schema_version": POSIX_STATE_VERSION,
            "volume": volume,
            "scenario": scenario,
            "partial": True,
            "leave_heal_pending": leave_heal_pending,
            "restore_heal_settings": True,
            "mount_root": mount_root,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_target": source_backend_file,
            "backend_by_host": backend_by_host,
            "index_root": index_root,
            "index_host": source_host,
        },
    )
    _require_posix_canary_readiness(volume, ssh_user=ssh_user)
    brick_roles_by_host = canary_api._brick_roles_by_host(volume)
    if set(brick_roles_by_host) != set(brick_hosts) or brick_roles_by_host.get(source_host) != "data":
        raise RuntimeError("file POSIX source-choice canary needs complete brick roles and a data-brick source")
    _ensure_canary_mount(volume, mount_root, acl_mount=True)
    acl_mount_available = _mountpoint_supports_acl(mount_root)
    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _local_mount_dir(mount_dir)
    _local_write_file(mount_file, "Initial content")
    _run_local(["sudo", "-n", "stat", mount_file])

    source_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            source_backend_root,
            [
                {
                    "op": "inspect_path",
                    "label": "source_posix_tuple",
                    "path": source_backend_file,
                }
            ],
        ),
    )
    _print_response("file-posix split-brain source worker", source_response)
    _require_successful_worker_response("file POSIX split-brain source worker", source_response)
    source_entry = next(
        (
            item
            for item in source_response.get("results", [])
            if item.get("op") == "inspect_path" and item.get("ok")
        ),
        {},
    )
    source_mode_bits = source_entry.get("mode_bits")
    source_uid = source_entry.get("uid")
    source_gid = source_entry.get("gid")
    source_acl_access_text = str(source_entry.get("acl_access") or "")
    source_acl_default_text = str(source_entry.get("acl_default") or "")
    source_acl_error = str(source_entry.get("acl_error") or "").strip()
    if source_mode_bits is None or source_uid is None or source_gid is None:
        mount_stat_text = _run_local_text(["sudo", "-n", "stat", "--format=%a:%u:%g", mount_file])
        mount_mode_bits_text, mount_uid_text, mount_gid_text = mount_stat_text.split(":", 2)
        if source_mode_bits is None:
            source_mode_bits = int(mount_mode_bits_text or "0", 8)
        if source_uid is None:
            source_uid = int(mount_uid_text or 0)
        if source_gid is None:
            source_gid = int(mount_gid_text or 0)
    if not source_mode_bits:
        raise RuntimeError("file POSIX split-brain canary worker did not return source mode bits")
    if source_acl_error:
        raise RuntimeError(f"file POSIX split-brain canary source ACL inspection failed: {source_acl_error}")

    source_gfid = str(source_entry.get("trusted_gfid") or source_entry.get("file_gfid") or "").strip()
    _write_state(
        volume,
        scenario,
        {
            "kind": "file-posix-metadata-split-brain",
            "volume": volume,
            "scenario": scenario,
            "partial": True,
            "leave_heal_pending": leave_heal_pending,
            "restore_heal_settings": True,
            "mount_root": mount_root,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_target": source_backend_file,
            "backend_by_host": backend_by_host,
            "index_root": index_root,
            "index_host": source_host,
            "index_entry": f"{index_root}/{source_gfid}" if source_gfid else "",
            "file_gfid_path": _file_gfid_link_path(source_backend_root, source_gfid) if source_gfid else "",
            "gfid_uuid": source_gfid,
        },
    )

    metadata_values_by_variant: dict[int, dict[str, object]] = {
        0: {
            "mode_bits": int(source_mode_bits),
            "uid": int(source_uid),
            "gid": int(source_gid),
            "acl_text": source_acl_access_text
            + ("\n" + source_acl_default_text if source_acl_default_text else ""),
        }
    }
    for variant in sorted(set(metadata_variant_by_host.values())):
        if variant == 0:
            continue
        mode_mask = 0o200 if variant == 1 else 0o100
        metadata_values_by_variant[variant] = {
            "mode_bits": int(source_mode_bits) ^ mode_mask or (int(source_mode_bits) ^ 0o040),
            "uid": variant if int(source_uid) != variant else variant + 10,
            "gid": variant if int(source_gid) != variant else variant + 10,
            "acl_text": "\n".join(
                [
                    "user::rw-",
                    f"user:{variant}:r--",
                    "group::r--",
                    "mask::r--",
                    "other::r--",
                    "",
                ]
            ),
        }

    for host in brick_hosts:
        metadata_values = metadata_values_by_variant[metadata_variant_by_host[host]]
        ops: list[dict[str, object]] = []
        if source_uid is not None or source_gid is not None:
            ops.append(
                {
                    "op": "chown",
                    "path": backend_by_host[host],
                    "uid": int(metadata_values["uid"]),
                    "gid": int(metadata_values["gid"]),
                }
            )
        if source_mode_bits is not None:
            ops.append(
                {
                    "op": "chmod",
                    "path": backend_by_host[host],
                    "mode_bits": f"{int(metadata_values['mode_bits']):04o}",
                }
            )
        if source_acl_access_text or source_acl_default_text:
            ops.append(
                {
                    "op": "setfacl",
                    "path": backend_by_host[host],
                    "acl_text": str(metadata_values["acl_text"]),
                }
            )
        ops.extend(
            [
                {
                    "op": "set_afr_metadata_pending",
                    "path": backend_by_host[host],
                    "pending_names": [
                        f"trusted.afr.{volume}-client-{brick_hosts.index(target_host)}"
                        for target_host in pending_targets_by_host[host]
                    ],
                    "value_hex": metadata_pending_value,
                },
                {
                    "op": "inspect_afr_state",
                    "path": backend_by_host[host],
                },
                {
                    "op": "inspect_path",
                    "label": "posix_tuple",
                    "path": backend_by_host[host],
                },
            ]
        )
        response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(volume, scenario, backend_roots[host], ops),
        )
        _print_response("file-posix split-brain metadata worker", response)
        _require_successful_worker_response(f"file POSIX split-brain metadata worker {host}", response)

    index_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            source_backend_root,
            [
                {
                    "op": "link_xattrop_gfid",
                    "target_path": source_backend_file,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": source_backend_file,
                },
            ],
        ),
    )
    _print_response("file-posix split-brain index worker", index_response)
    _require_successful_worker_response("file POSIX split-brain index worker", index_response)
    index_results = index_response.get("results", [])
    index_entry = next((item for item in index_results if item.get("op") == "link_xattrop_gfid" and item.get("ok")), {})
    gfid_entry = next((item for item in index_results if item.get("op") == "link_file_gfid_from_target" and item.get("ok")), {})
    canonical_gfid = str(index_entry.get("value_uuid") or gfid_entry.get("value_uuid") or "")
    index_entry_path = str(index_entry.get("xattrop_entry") or "")
    file_gfid_path = str(gfid_entry.get("file_gfid_path") or "")
    if not canonical_gfid:
        raise RuntimeError("file POSIX split-brain canary worker did not return a canonical file GFID")
    if not index_entry_path:
        index_entry_path = f"{index_root}/{canonical_gfid}"
    if not file_gfid_path:
        file_gfid_path = _file_gfid_link_path(source_backend_root, canonical_gfid)

    metadata_by_host = _capture_file_posix_metadata_by_host(
        volume=volume,
        scenario=scenario,
        backend_by_host=backend_by_host,
        hosts=brick_hosts,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )
    metadata_tuple_by_host = {
        host: {
            "mode_bits": record.get("mode_bits"),
            "uid": record.get("uid"),
            "gid": record.get("gid"),
            "acl_access": record.get("acl_access"),
            "acl_default": record.get("acl_default"),
        }
        for host, record in sorted(metadata_by_host.items())
    }
    metadata_backend_by_host = {host: str(record.get("backend") or backend_by_host[host]) for host, record in sorted(metadata_by_host.items())}
    metadata_fields_to_align = _posix_metadata_fields_to_align(metadata_by_host, source_host)
    heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    heal_info_split_brain_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info", "split-brain"])
    if leave_heal_pending:
        heal_info_after = heal_info_before
        heal_info_split_brain_after = heal_info_split_brain_before
    else:
        _trigger_canary_heal(volume, scenario)
        heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
        heal_info_split_brain_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info", "split-brain"])
    heal_info_contains_mount_file_before = _heal_info_contains_path(heal_info_before, mount_file, mount_root=mount_root, gfid=canonical_gfid)
    heal_info_contains_mount_file_after = _heal_info_contains_path(heal_info_after, mount_file, mount_root=mount_root, gfid=canonical_gfid)
    heal_info_split_brain_contains_mount_file_before = _heal_info_contains_path(
        heal_info_split_brain_before,
        mount_file,
        mount_root=mount_root,
        gfid=canonical_gfid,
    )
    heal_info_split_brain_contains_mount_file_after = _heal_info_contains_path(
        heal_info_split_brain_after,
        mount_file,
        mount_root=mount_root,
        gfid=canonical_gfid,
    )
    stable_split_brain = bool(
        heal_info_split_brain_contains_mount_file_before and heal_info_split_brain_contains_mount_file_after
    )
    direct_x4_fixture = len(brick_hosts) == 4
    fixture_scope = (
        "x4-direct-bookkeeping-diagnostic"
        if direct_x4_fixture
        else "x3-upstream-derived-fixture"
    )
    proof_label = (
        "harness-only-pending"
        if leave_heal_pending
        else "diagnostic-only"
        if direct_x4_fixture
        else "afr-synthesized" if stable_split_brain else "native-heal-smoke"
    )
    state_payload = {
        "kind": "file-posix-metadata-split-brain",
        "schema_version": POSIX_STATE_VERSION,
        "brick_roles_by_host": brick_roles_by_host,
        "volume": volume,
        "scenario": scenario,
        "construction_class": "afr-synthesized",
        "proof_label": proof_label,
        "fixture_scope": fixture_scope,
        "source_choice_eligible": stable_split_brain and not direct_x4_fixture and not leave_heal_pending,
        "leave_heal_pending": leave_heal_pending,
        "heal_crawl_triggered": not leave_heal_pending,
        "restore_heal_settings": True,
        "acl_mount_available": acl_mount_available,
        "mismatch_host": mismatch_host,
        "source_host": source_host,
        "pending_targets_by_host": pending_targets_by_host,
        "metadata_variant_by_host": metadata_variant_by_host,
        "dir_name": dir_name,
        "file_name": file_name,
        "mount_root": mount_root,
        "mount_target": mount_dir,
        "mount_file": mount_file,
        "backend_root": source_backend_root,
        "backend_roots": backend_roots,
        "backend_target": source_backend_file,
        "backend_by_host": backend_by_host,
        "metadata_tuple_by_host": metadata_tuple_by_host,
        "metadata_backend_by_host": metadata_backend_by_host,
        "metadata_fields_to_align": metadata_fields_to_align,
        "metadata_majority_hosts": [],
        "metadata_mismatch_hosts": sorted(metadata_by_host),
        "metadata_source_reason": "native_gluster_source",
        "metadata_pending_value": metadata_pending_value,
        "afr_pending_xattrs_by_host": {},
        "index_root": index_root,
        "index_host": source_host,
        "index_entry": index_entry_path,
        "file_gfid_path": file_gfid_path,
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
        "heal_info_before": heal_info_before,
        "heal_info_split_brain_before": heal_info_split_brain_before,
        "heal_info_after": heal_info_after,
        "heal_info_split_brain_after": heal_info_split_brain_after,
        "heal_info_contains_mount_file_before": heal_info_contains_mount_file_before,
        "heal_info_contains_mount_file_after": heal_info_contains_mount_file_after,
        "heal_info_split_brain_contains_mount_file_before": heal_info_split_brain_contains_mount_file_before,
        "heal_info_split_brain_contains_mount_file_after": heal_info_split_brain_contains_mount_file_after,
        "gluster_visible_metadata_split_brain": stable_split_brain,
    }
    for host in brick_hosts:
        afr_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_roots[host],
                [
                    {
                        "op": "inspect_afr_state",
                        "path": backend_by_host[host],
                    }
                ],
            ),
        )
        afr_entry = next(
            (
                item
                for item in afr_response.get("results", [])
                if item.get("op") == "inspect_afr_state" and item.get("ok")
            ),
            {},
        )
        state_payload["afr_pending_xattrs_by_host"][host] = list(afr_entry.get("afr_xattrs") or [])
    _write_state(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                "Created gtest file POSIX metadata split-brain canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  construction class: afr-synthesized",
                f"  proof state: {proof_label}",
                f"  heal crawl triggered: {'yes' if not leave_heal_pending else 'no; fixture retained for independent evidence'}",
                f"  logical path: {mount_file}",
                f"  source host: {source_host}",
                f"  requested source host: {mismatch_host}",
                f"  source tuple: mode={int(source_mode_bits):04o} uid={int(source_uid)} gid={int(source_gid)}",
                f"  metadata variants: {metadata_variant_by_host}",
                f"  AFR pending targets: {pending_targets_by_host}",
                f"  source ACL access: {source_acl_access_text or '(none)'}",
                f"  source ACL default: {source_acl_default_text or '(none)'}",
                f"  client ACL mount support: {'yes' if acl_mount_available else 'no; mode/uid/gid proof only'}",
                f"  backend target: {source_backend_file}",
                f"  index bucket: {index_root}",
                f"  index entry: {index_entry_path}",
                f"  canonical file GFID: {canonical_gfid}",
                f"  heal info before fixture handoff:\n{heal_info_before or '(empty)'}",
                f"  heal info split-brain before fixture handoff:\n{heal_info_split_brain_before or '(empty)'}",
                f"  heal info after {'heal crawl' if not leave_heal_pending else 'fixture handoff'}:\n{heal_info_after or '(empty)'}",
                f"  heal info split-brain after {'heal crawl' if not leave_heal_pending else 'fixture handoff'}:\n{heal_info_split_brain_after or '(empty)'}",
                f"  heal info contains canary path before handoff: {'yes' if heal_info_contains_mount_file_before else 'no'}",
                f"  heal info contains canary path after handoff: {'yes' if heal_info_contains_mount_file_after else 'no'}",
                f"  split-brain info contains canary path before handoff: {'yes' if heal_info_split_brain_contains_mount_file_before else 'no'}",
                f"  split-brain info contains canary path after handoff: {'yes' if heal_info_split_brain_contains_mount_file_after else 'no'}",
                (
                    "Pending independent evidence: no repair-acceptance proof label is assigned until a separate observation records a durable Gluster split-brain row."
                    if leave_heal_pending
                    else "Expected: Gluster should expose this as an afr-synthesized POSIX source-choice proof, not as a direct-backend repair."
                    if stable_split_brain
                    else "Expected: this stayed diagnostic/native-heal-smoke; keep it out of repair acceptance until Gluster leaves a durable heal-info row."
                ),
                (
                    "Next: collect independent evidence without a heal crawl, then build the POSIX split-brain review plan only if it records the canary path as split-brain."
                    if leave_heal_pending
                    else "Next: build the POSIX split-brain review plan, then choose metadata_source_host and resolve with native source-brick."
                    if stable_split_brain
                    else "Next: rerun or reclassify this case as diagnostic/background until it can leave a durable heal-info row."
                ),
            ]
        )
    )


def build_file_posix_metadata_split_brain_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = _read_state(volume, scenario)
    logical_path = validate_posix_source_choice_state(state, volume=volume, scenario=scenario)
    kind = state["kind"]
    mount_file = state["mount_file"]

    metadata_by_host = state.get("metadata_tuple_by_host") or {}
    if not isinstance(metadata_by_host, dict) or not metadata_by_host:
        raise RuntimeError(f"scenario {scenario!r} does not record per-host POSIX metadata tuples")
    backend_by_host = state.get("metadata_backend_by_host") or {}
    if not isinstance(backend_by_host, dict) or not backend_by_host:
        backend_by_host = state.get("backend_by_host") or {}
    if not isinstance(backend_by_host, dict) or not backend_by_host:
        raise RuntimeError(f"scenario {scenario!r} does not record per-host backend paths")

    metadata_tuple_by_host: dict[str, dict[str, object]] = {}
    metadata_backend_by_host: dict[str, str] = {}
    for host in sorted(metadata_by_host):
        record = metadata_by_host.get(host) or {}
        if not isinstance(record, dict):
            record = {}
        metadata_tuple_by_host[host] = {
            "mode_bits": record.get("mode_bits"),
            "uid": record.get("uid"),
            "gid": record.get("gid"),
            "acl_access": str(record.get("acl_access") or ""),
            "acl_default": str(record.get("acl_default") or ""),
        }
        metadata_backend_by_host[host] = str(backend_by_host.get(host) or record.get("backend") or "").strip()
    source_host = str(state.get("source_host") or "").strip() or sorted(metadata_tuple_by_host)[0]
    metadata_fields_to_align = list(state.get("metadata_fields_to_align") or _posix_metadata_fields_to_align(metadata_by_host, source_host))
    heal_info_before = str(state.get("heal_info_before") or "").strip()
    heal_info_split_brain_before = str(state.get("heal_info_split_brain_before") or "").strip()
    heal_info_after = str(state.get("heal_info_after") or "").strip()
    heal_info_split_brain_after = str(state.get("heal_info_split_brain_after") or "").strip()
    missing_heal_snapshots = [
        name
        for name, value in (
            ("heal_info_before", heal_info_before),
            ("heal_info_split_brain_before", heal_info_split_brain_before),
            ("heal_info_after", heal_info_after),
            ("heal_info_split_brain_after", heal_info_split_brain_after),
        )
        if not value
    ]
    if missing_heal_snapshots:
        raise RuntimeError(
            f"scenario {scenario!r} does not record heal snapshots ({', '.join(missing_heal_snapshots)}); rerun the canary before building a plan"
        )

    metadata_pending_value = str(state.get("metadata_pending_value") or "").strip()
    afr_pending_xattrs_by_host = state.get("afr_pending_xattrs_by_host") or {}
    brick_roles_by_host = dict(state["brick_roles_by_host"])
    if not isinstance(afr_pending_xattrs_by_host, dict):
        afr_pending_xattrs_by_host = {}
    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": logical_path,
        "action_type": "review_posix_metadata_no_majority",
        "object_type": "file",
        "depth": logical_path.count("/") + 1,
        "mounted_target": mount_file,
        "repair_strategy": "choose_posix_metadata_source",
        "metadata_tuple_by_host": metadata_tuple_by_host,
        "metadata_backend_by_host": metadata_backend_by_host,
        "metadata_majority_hosts": [],
        "metadata_mismatch_hosts": sorted(metadata_tuple_by_host),
        "metadata_fields_to_align": metadata_fields_to_align,
        "metadata_source_reason": "native_gluster_source",
        "heal_info_before": heal_info_before,
        "heal_info_split_brain_before": heal_info_split_brain_before,
        "heal_info_after": heal_info_after,
        "heal_info_split_brain_after": heal_info_split_brain_after,
        "metadata_pending_value": metadata_pending_value,
        "afr_pending_xattrs_by_host": afr_pending_xattrs_by_host,
        "brick_roles_by_host": brick_roles_by_host,
        "gluster_visible_metadata_split_brain": True,
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            "file POSIX metadata split-brain: afr-synthesized upstream fixture",
            "native resolution mode: source-brick after source selection",
            f"metadata pending value: {metadata_pending_value or '(missing)'}",
            *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
            *canary_api._format_afr_pending_xattrs_by_host_for_notes(afr_pending_xattrs_by_host),
            *canary_api._format_posix_metadata_by_host_for_notes(
                {
                    host: {**record, "backend": metadata_backend_by_host.get(host, "")}
                    for host, record in metadata_tuple_by_host.items()
                }
            ),
            f"heal info before heal crawl:\n{heal_info_before or '(empty)'}",
            f"heal info split-brain before heal crawl:\n{heal_info_split_brain_before or '(empty)'}",
            f"heal info after heal crawl:\n{heal_info_after or '(empty)'}",
            f"heal info split-brain after heal crawl:\n{heal_info_split_brain_after or '(empty)'}",
            "Expected: review the live tuples, choose metadata_source_host, then use native Gluster source-brick resolution to clear the row.",
        ],
    }
    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": [action],
    }


def create_file_ctime_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    mismatch_host = _select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    canonical_hosts = [host for host in brick_hosts if host != mismatch_host]
    marker_host = canonical_hosts[0] if canonical_hosts else ""
    if not marker_host:
        raise RuntimeError(f"need at least one marker host besides {mismatch_host}")
    marker_backend_root = backend_roots[marker_host]
    canonical_backend_root = marker_backend_root
    mismatch_backend_root = backend_roots[mismatch_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{marker_backend_root}/{scenario}/{dir_name}/{file_name}"

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_write_file(mount_file, "Initial content")
    _run_local(["sudo", "-n", "stat", mount_file])
    _run_local(["sleep", "3"])

    marker_response: dict[str, Any] = {}
    canonical_mdata_hex = ""
    for _ in range(20):
        marker_response = _canary_worker_command(
            marker_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                marker_backend_root,
                [
                    {
                        "op": "getxattr",
                        "path": backend_file,
                        "name": "trusted.glusterfs.mdata",
                    },
                ],
            ),
        )
        marker_results = marker_response.get("results", [])
        mdata_entry = next(
            (
                item
                for item in marker_results
                if item.get("op") == "getxattr"
                and item.get("ok")
                and str(item.get("name") or "") == "trusted.glusterfs.mdata"
            ),
            {},
        )
        canonical_mdata_hex = str(mdata_entry.get("value_hex") or "")
        if canonical_mdata_hex:
            break
        _run_local(["sleep", "1"])
    _print_response("file-ctime marker worker", marker_response)
    if not canonical_mdata_hex:
        raise RuntimeError("file-ctime canary worker did not return ctime metadata; ensure ctime is enabled")

    for canonical_host in canonical_hosts:
        canonical_response = _canary_worker_command(
            canonical_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                canonical_backend_root,
                [
                    {
                        "op": "touch",
                        "path": backend_file,
                    },
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": "trusted.glusterfs.mdata",
                        "value_hex": canonical_mdata_hex,
                    }
                ],
            ),
        )
        _print_response("file-ctime canonical worker", canonical_response)

    mismatch_response = _canary_worker_command(
        mismatch_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            mismatch_backend_root,
            [
                {
                    "op": "removexattr",
                    "path": backend_file,
                    "name": "trusted.glusterfs.mdata",
                }
            ],
        ),
    )
    _print_response("file-ctime mismatch worker", mismatch_response)

    _write_state(
        volume,
        scenario,
        {
            "kind": "file-ctime-metadata",
            "volume": volume,
            "scenario": scenario,
            "mismatch_host": mismatch_host,
            "marker_host": marker_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_file,
            "backend_root": marker_backend_root,
            "backend_roots": backend_roots,
            "backend_target": backend_file,
            "mdata_hex": canonical_mdata_hex,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest file-ctime review case:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  mismatch host: {mismatch_host}",
                f"  marker host: {marker_host}",
                f"  backend target: {backend_file}",
                f"  canonical ctime metadata: {canonical_mdata_hex}",
                "Expected: this should expose a file-backed ctime / mdata investigation, not an immediate repair proof.",
                "Next canary: if this still stays hidden, we probably need a brick-down metadata split-brain case instead.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_missing_file_replica_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    kind: str = "missing-file-replica",
    label: str = "missing-file-replica",
    trigger_heal: bool = True,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    brick_roles = canary_api._brick_roles(volume)
    missing_host = _select_role_host(brick_hosts, missing_host, role="missing", default_to_next=True)
    if brick_roles.get(missing_host) == "arbiter":
        raise RuntimeError(f"missing-file-replica canary requires a data brick, not arbiter {missing_host}")
    source_host = next(
        (host for host in brick_hosts if host != missing_host and brick_roles.get(host) == "data"),
        "",
    )
    if not source_host:
        raise RuntimeError(f"need at least one data source host besides {missing_host}")
    source_backend_root = backend_roots[source_host]
    missing_backend_root = backend_roots[missing_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"
    index_root = f"{source_backend_root}/.glusterfs/indices/xattrop"

    if trigger_heal:
        _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    else:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    try:
        _local_mount_dir(mount_dir)
        _local_mount_file(mount_file)
        _run_local(["sudo", "-n", "stat", mount_file])

        canonical_gfid, resolver_file_gfid_path, resolver_backend_path = _resolve_mount_file_gfid(
            volume=volume,
            mount_root=mount_root,
            mount_file=mount_file,
            backend_root=source_backend_root,
        )
        file_gfid_path = resolver_file_gfid_path or _file_gfid_link_path(source_backend_root, canonical_gfid)
        if resolver_backend_path and resolver_backend_path != backend_file:
            raise RuntimeError(
                f"resolver backend path mismatch for {mount_file}: expected {backend_file}, got {resolver_backend_path}"
            )

        missing_response = _canary_worker_command(
            missing_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                missing_backend_root,
                [
                    {
                        "op": "rm",
                        "path": file_gfid_path,
                        "recursive": False,
                        "force": True,
                    },
                    {
                        "op": "rm",
                        "path": backend_file,
                        "recursive": False,
                        "force": True,
                    },
                ],
            ),
        )
        _print_response("missing-file target worker", missing_response)
    except Exception:
        _unmount_canary_mount(mount_root)
        _local_remove(mount_root)
        raise

    _write_state(
        volume,
        scenario,
        {
            "kind": kind,
            "leave_heal_pending": not trigger_heal,
            "volume": volume,
            "scenario": scenario,
            "missing_host": missing_host,
            "source_host": source_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_dir": mount_dir,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_target": backend_file,
            "index_root": index_root,
            "index_hosts": [source_host],
            "brick_roles_by_host": brick_roles,
            "gfid_uuid": canonical_gfid,
            "gfid_hex": canonical_gfid.replace("-", ""),
            "file_gfid_path": file_gfid_path,
        },
    )
    if trigger_heal:
        _trigger_canary_heal(volume, scenario)
    else:
        print("Leaving heal settings disabled; no heal crawl was launched. Use this only when the fixture already creates pending/index evidence.")

    print(
        "\n".join(
            [
                f"Created gtest {label} canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  missing host: {missing_host}",
                f"  source marker host: {source_host}",
                f"  backend target: {backend_file}",
                f"  file GFID: {canonical_gfid}",
                f"  temp mount root: {mount_root}",
                "Expected: planner should choose restore_missing_replica once the heal entry is resolved.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_arbiter_missing_gfid_canary(
    *,
    volume: str,
    scenario: str,
    object_type: str,
    dir_name: str,
    file_name: str,
    arbiter_host: str | None = None,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    """Create a 2+1 fixture with only the arbiter object's GFID metadata removed."""
    if object_type not in {"file", "directory"}:
        raise RuntimeError(f"unsupported arbiter GFID canary object type: {object_type}")
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    brick_roles = canary_api._brick_roles(volume)
    data_hosts = sorted(host for host in brick_hosts if brick_roles.get(host) == "data")
    arbiter_hosts = sorted(host for host in brick_hosts if brick_roles.get(host) == "arbiter")
    if len(data_hosts) != 2 or len(arbiter_hosts) != 1:
        raise RuntimeError(f"arbiter GFID canary requires exactly two data bricks and one arbiter on {volume}")
    if arbiter_host is None:
        arbiter_host = arbiter_hosts[0]
    if arbiter_host not in arbiter_hosts:
        raise RuntimeError(f"arbiter GFID canary requires the arbiter host, got {arbiter_host}")

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_target = mount_dir if object_type == "directory" else f"{mount_dir}/{file_name}"
    relative_path = f"{scenario}/{dir_name}" if object_type == "directory" else f"{scenario}/{dir_name}/{file_name}"
    backend_by_host = {host: f"{backend_roots[host]}/{relative_path}" for host in brick_hosts}

    _write_state(
        volume,
        scenario,
        {
            "kind": "arbiter-missing-gfid",
            "partial": True,
            "restore_heal_settings": True,
            "volume": volume,
            "scenario": scenario,
            "object_type": object_type,
            "dir_name": dir_name,
            "file_name": file_name if object_type == "file" else "",
            "mount_root": mount_root,
            "mount_dir": mount_dir,
            "mount_target": mount_target,
            "backend_root": backend_roots[data_hosts[0]],
            "backend_roots": backend_roots,
            "backend_target": backend_by_host[data_hosts[0]],
            "backend_by_host": backend_by_host,
            "arbiter_host": arbiter_host,
            "brick_roles_by_host": brick_roles,
        },
    )
    set_heal_settings(volume, False)
    try:
        _ensure_canary_mount(volume, mount_root)
        _local_mount_dir(mount_dir)
        if object_type == "file":
            _local_mount_file(mount_target)
        _run_local(["sudo", "-n", "stat", mount_target])

        data_gfids: dict[str, str] = {}
        for host in data_hosts:
            response = _canary_worker_command(
                host,
                ssh_user=ssh_user,
                worker_path=worker_path,
                request=_make_request(
                    volume,
                    scenario,
                    backend_roots[host],
                    [{"op": "inspect_path", "path": backend_by_host[host]}],
                ),
            )
            _require_successful_worker_response(f"arbiter GFID data inspection on {host}", response)
            entry = next((item for item in response.get("results", []) if item.get("op") == "inspect_path"), {})
            gfid = str(entry.get("trusted_gfid") or entry.get("file_gfid") or "").strip()
            if not gfid:
                raise RuntimeError(f"data inspection on {host} did not return trusted.gfid")
            data_gfids[host] = gfid
        if len(set(data_gfids.values())) != 1:
            raise RuntimeError(f"data bricks do not agree on the canary GFID: {data_gfids}")
        canonical_gfid = next(iter(data_gfids.values()))
        arbiter_handle = (
            _directory_gfid_link_path(backend_roots[arbiter_host], canonical_gfid)
            if object_type == "directory"
            else _file_gfid_link_path(backend_roots[arbiter_host], canonical_gfid)
        )
        _write_state(
            volume,
            scenario,
            {
                "kind": "arbiter-missing-gfid",
                "partial": True,
                "restore_heal_settings": True,
                "volume": volume,
                "scenario": scenario,
                "object_type": object_type,
                "dir_name": dir_name,
                "file_name": file_name if object_type == "file" else "",
                "mount_root": mount_root,
                "mount_dir": mount_dir,
                "mount_target": mount_target,
                "backend_root": backend_roots[data_hosts[0]],
                "backend_roots": backend_roots,
                "backend_target": backend_by_host[data_hosts[0]],
                "backend_by_host": backend_by_host,
                "arbiter_host": arbiter_host,
                "arbiter_handle": arbiter_handle,
                "brick_roles_by_host": brick_roles,
                "gfid_uuid": canonical_gfid,
                "gfid_hex": canonical_gfid.replace("-", ""),
            },
        )
        response = _canary_worker_command(
            arbiter_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_roots[arbiter_host],
                [
                    {"op": "removexattr", "path": backend_by_host[arbiter_host], "name": "trusted.gfid"},
                    {"op": "rm", "path": arbiter_handle, "recursive": object_type == "directory", "force": True},
                ],
            ),
        )
        _require_successful_worker_response("arbiter GFID removal", response)
    except Exception:
        _unmount_canary_mount(mount_root)
        _local_remove(mount_root)
        set_heal_settings(volume, True)
        raise

    state_payload = {
        "kind": "arbiter-missing-gfid",
        "partial": False,
        "leave_heal_pending": True,
        "volume": volume,
        "scenario": scenario,
        "object_type": object_type,
        "dir_name": dir_name,
        "file_name": file_name if object_type == "file" else "",
        "mount_root": mount_root,
        "mount_dir": mount_dir,
        "mount_target": mount_target,
        "backend_root": backend_roots[data_hosts[0]],
        "backend_roots": backend_roots,
        "backend_target": backend_by_host[data_hosts[0]],
        "backend_by_host": backend_by_host,
        "arbiter_host": arbiter_host,
        "arbiter_handle": arbiter_handle,
        "brick_roles_by_host": brick_roles,
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
        "construction_class": "backend-audit",
        "proof_label": "operator-path-mechanical",
    }
    # Do not probe through FUSE after removing arbiter metadata: name-heal can
    # run on lookup even while the configured heal controls are off.
    _write_state(volume, scenario, state_payload)
    print(
        "\n".join(
            [
                "Created arbiter missing-GFID canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  object type: {object_type}",
                f"  logical path: {mount_target}",
                f"  data GFID: {canonical_gfid}",
                f"  arbiter host: {arbiter_host}",
                f"  removed arbiter handle: {arbiter_handle}",
                "  healing: disabled; no crawl launched",
            ]
        )
    )


def create_arbiter_only_residue_canary(
    *,
    volume: str,
    scenario: str,
    object_type: str,
    dir_name: str,
    file_name: str,
    arbiter_host: str | None = None,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    """Create a 2+1 fixture with only the arbiter placeholder left behind."""
    if object_type not in {"file", "directory"}:
        raise RuntimeError(f"unsupported arbiter residue canary object type: {object_type}")
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    brick_roles = canary_api._brick_roles(volume)
    data_hosts = sorted(host for host in brick_hosts if brick_roles.get(host) == "data")
    arbiter_hosts = sorted(host for host in brick_hosts if brick_roles.get(host) == "arbiter")
    if len(data_hosts) != 2 or len(arbiter_hosts) != 1:
        raise RuntimeError(f"arbiter residue canary requires exactly two data bricks and one arbiter on {volume}")
    if arbiter_host is None:
        arbiter_host = arbiter_hosts[0]
    if arbiter_host not in arbiter_hosts:
        raise RuntimeError(f"arbiter residue canary requires the arbiter host, got {arbiter_host}")

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_target = mount_dir if object_type == "directory" else f"{mount_dir}/{file_name}"
    relative_path = f"{scenario}/{dir_name}" if object_type == "directory" else f"{scenario}/{dir_name}/{file_name}"
    backend_by_host = {host: f"{backend_roots[host]}/{relative_path}" for host in brick_hosts}

    _write_state(
        volume,
        scenario,
        {
            "kind": "arbiter-only-residue",
            "partial": True,
            "restore_heal_settings": True,
            "volume": volume,
            "scenario": scenario,
            "object_type": object_type,
            "dir_name": dir_name,
            "file_name": file_name if object_type == "file" else "",
            "mount_root": mount_root,
            "mount_dir": mount_dir,
            "mount_target": mount_target,
            "backend_root": backend_roots[arbiter_host],
            "backend_roots": backend_roots,
            "backend_target": backend_by_host[arbiter_host],
            "backend_by_host": backend_by_host,
            "arbiter_host": arbiter_host,
            "brick_roles_by_host": brick_roles,
        },
    )
    set_heal_settings(volume, False)
    try:
        _ensure_canary_mount(volume, mount_root)
        _local_mount_dir(mount_dir)
        if object_type == "file":
            _local_mount_file(mount_target)
        _run_local(["sudo", "-n", "stat", mount_target])

        data_gfids: dict[str, str] = {}
        for host in data_hosts:
            response = _canary_worker_command(
                host,
                ssh_user=ssh_user,
                worker_path=worker_path,
                request=_make_request(
                    volume,
                    scenario,
                    backend_roots[host],
                    [{"op": "inspect_path", "path": backend_by_host[host]}],
                ),
            )
            _require_successful_worker_response(f"arbiter residue data inspection on {host}", response)
            entry = next((item for item in response.get("results", []) if item.get("op") == "inspect_path"), {})
            gfid = str(entry.get("trusted_gfid") or entry.get("file_gfid") or "").strip()
            if not gfid:
                raise RuntimeError(f"data inspection on {host} did not return trusted.gfid")
            data_gfids[host] = gfid
        if len(set(data_gfids.values())) != 1:
            raise RuntimeError(f"data bricks do not agree on the canary GFID: {data_gfids}")
        canonical_gfid = next(iter(data_gfids.values()))
        arbiter_handle = (
            _directory_gfid_link_path(backend_roots[arbiter_host], canonical_gfid)
            if object_type == "directory"
            else _file_gfid_link_path(backend_roots[arbiter_host], canonical_gfid)
        )
        arbiter_response = _canary_worker_command(
            arbiter_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_roots[arbiter_host],
                [{"op": "inspect_path", "path": backend_by_host[arbiter_host]}],
            ),
        )
        _require_successful_worker_response("arbiter residue inspection", arbiter_response)
        arbiter_entry = next(
            (item for item in arbiter_response.get("results", []) if item.get("op") == "inspect_path"),
            {},
        )
        arbiter_gfid = str(arbiter_entry.get("trusted_gfid") or arbiter_entry.get("file_gfid") or "").strip()
        if arbiter_gfid != canonical_gfid:
            raise RuntimeError("arbiter placeholder does not match the data GFID before data copies are removed")
        _write_state(
            volume,
            scenario,
            {
                "kind": "arbiter-only-residue",
                "partial": True,
                "restore_heal_settings": True,
                "volume": volume,
                "scenario": scenario,
                "object_type": object_type,
                "dir_name": dir_name,
                "file_name": file_name if object_type == "file" else "",
                "mount_root": mount_root,
                "mount_dir": mount_dir,
                "mount_target": mount_target,
                "backend_root": backend_roots[arbiter_host],
                "backend_roots": backend_roots,
                "backend_target": backend_by_host[arbiter_host],
                "backend_by_host": backend_by_host,
                "arbiter_host": arbiter_host,
                "arbiter_handle": arbiter_handle,
                "brick_roles_by_host": brick_roles,
                "gfid_uuid": canonical_gfid,
                "gfid_hex": canonical_gfid.replace("-", ""),
            },
        )

        for host in data_hosts:
            handle = (
                _directory_gfid_link_path(backend_roots[host], canonical_gfid)
                if object_type == "directory"
                else _file_gfid_link_path(backend_roots[host], canonical_gfid)
            )
            response = _canary_worker_command(
                host,
                ssh_user=ssh_user,
                worker_path=worker_path,
                request=_make_request(
                    volume,
                    scenario,
                    backend_roots[host],
                    [
                        {"op": "rm", "path": backend_by_host[host], "recursive": object_type == "directory", "force": True},
                        {"op": "rm", "path": handle, "recursive": False, "force": True},
                    ],
                ),
            )
            _require_successful_worker_response(f"arbiter residue data removal on {host}", response)
    except Exception:
        _unmount_canary_mount(mount_root)
        _local_remove(mount_root)
        set_heal_settings(volume, True)
        raise

    state_payload = {
        "kind": "arbiter-only-residue",
        "partial": False,
        "leave_heal_pending": True,
        "volume": volume,
        "scenario": scenario,
        "object_type": object_type,
        "dir_name": dir_name,
        "file_name": file_name if object_type == "file" else "",
        "mount_root": mount_root,
        "mount_dir": mount_dir,
        "mount_target": mount_target,
        "backend_root": backend_roots[arbiter_host],
        "backend_roots": backend_roots,
        "backend_target": backend_by_host[arbiter_host],
        "backend_by_host": backend_by_host,
        "arbiter_host": arbiter_host,
        "arbiter_handle": arbiter_handle,
        "brick_roles_by_host": brick_roles,
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
        "construction_class": "backend-audit",
        "proof_label": "operator-path-mechanical",
    }
    # Do not probe through FUSE after the data copies are absent: name-heal can
    # change the intended arbiter-only residue before independent evidence.
    _write_state(volume, scenario, state_payload)
    print(
        "\n".join(
            [
                "Created arbiter-only residue canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  object type: {object_type}",
                f"  logical path: {mount_target}",
                f"  data GFID: {canonical_gfid}",
                f"  arbiter host: {arbiter_host}",
                f"  retained arbiter handle: {arbiter_handle}",
                "  healing: disabled; no crawl launched",
            ]
        )
    )


def create_missing_file_replica_pair_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 4:
        raise RuntimeError(f"need at least 4 bricks for file missing-replica pair canary on {volume}")

    source_hosts = brick_hosts[:2]
    missing_hosts = brick_hosts[2:4]
    source_backend_root = backend_roots[source_hosts[0]]
    missing_backend_root = backend_roots[missing_hosts[0]]
    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_dir = f"{source_backend_root}/{scenario}/{dir_name}"
    backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"
    index_root = f"{source_backend_root}/.glusterfs/indices/xattrop"
    canonical_gfid = ""
    file_gfid_path = ""
    canonical_gfid_hex = _new_gfid_hex()

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_mount_file(mount_file)
    _run_local(["sudo", "-n", "stat", mount_file])
    _run_local(["sleep", "3"])

    canonical_gfid, resolver_file_gfid_path, resolver_backend_path = _resolve_mount_file_gfid(
        volume=volume,
        mount_root=mount_root,
        mount_file=mount_file,
        backend_root=source_backend_root,
    )
    if resolver_backend_path and resolver_backend_path != backend_file:
        raise RuntimeError(
            f"resolver backend path mismatch for {mount_file}: expected {backend_file}, got {resolver_backend_path}"
        )
    file_gfid_path = resolver_file_gfid_path or _file_gfid_link_path(source_backend_root, canonical_gfid)

    for source_host in source_hosts:
        source_response = _canary_worker_command(
            source_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                source_backend_root,
                [
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": "trusted.gfid",
                        "value_hex": canonical_gfid.replace("-", ""),
                    },
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": f"trusted.afr.{volume}-client-{brick_hosts.index(missing_hosts[0])}",
                        "value_hex": "000000010000000000000001",
                    },
                    {
                        "op": "link_file_gfid_from_target",
                        "target_path": backend_file,
                    },
                ],
            ),
        )
        _print_response(f"missing-file-pair source worker {source_host}", source_response)

    for missing_host in missing_hosts:
        missing_response = _canary_worker_command(
            missing_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                missing_backend_root,
                [
                    {
                        "op": "rm",
                        "path": file_gfid_path,
                        "recursive": False,
                        "force": True,
                    },
                    {
                        "op": "rm",
                        "path": backend_file,
                        "recursive": False,
                        "force": True,
                    },
                ],
            ),
        )
        _print_response(f"missing-file-pair target worker {missing_host}", missing_response)

    _write_state(
        volume,
        scenario,
        {
            "kind": "missing-file-replica-pair",
            "volume": volume,
            "scenario": scenario,
            "source_hosts": source_hosts,
            "missing_hosts": missing_hosts,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_dir": mount_dir,
            "mount_file": mount_file,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_dir": backend_dir,
            "backend_target": backend_file,
            "index_root": index_root,
            "index_hosts": source_hosts,
            "gfid_uuid": canonical_gfid,
            "gfid_hex": canonical_gfid.replace("-", "") if canonical_gfid else canonical_gfid_hex,
            "file_gfid_path": file_gfid_path,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest file missing-replica pair canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  survivor hosts: {', '.join(source_hosts)}",
                f"  removed hosts: {', '.join(missing_hosts)}",
                f"  backend dir: {backend_dir}",
                f"  backend target: {backend_file}",
                f"  file GFID: {canonical_gfid}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Expected: planner should classify this as a below-quorum missing-replica pair and keep salvage review-first.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_file_child_gap_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    trigger_heal: bool = True,
) -> None:
    create_missing_file_replica_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        dir_name=dir_name,
        file_name=file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
        kind="file-child-gap",
        label="file-child-gap",
        trigger_heal=trigger_heal,
    )


def create_file_child_gap_multi_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    dir_name: str,
    file_names: list[str],
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    if not file_names:
        raise RuntimeError("need at least one file name for file-child-gap-multi canary")

    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    missing_host = _select_role_host(brick_hosts, missing_host, role="missing", default_to_next=True)
    source_host = next((host for host in brick_hosts if host != missing_host), "")
    if not source_host:
        raise RuntimeError(f"need at least one source host besides {missing_host}")
    source_backend_root = backend_roots[source_host]
    missing_backend_root = backend_roots[missing_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    backend_dir = f"{source_backend_root}/{scenario}/{dir_name}"
    index_root = f"{source_backend_root}/.glusterfs/indices/xattrop"
    pending_value = "000000010000000000000001"
    files_state: list[dict[str, Any]] = []

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)

    for file_name in file_names:
        mount_file = f"{mount_dir}/{file_name}"
        backend_file = f"{backend_dir}/{file_name}"
        _local_mount_file(mount_file)

        source_response = _canary_worker_command(
            source_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                source_backend_root,
                [
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": f"trusted.afr.{volume}-client-{brick_hosts.index(missing_host)}",
                        "value_hex": pending_value,
                    },
                    {
                        "op": "touch_index_from_target",
                        "target_path": backend_file,
                        "index_root": index_root,
                    },
                ],
            ),
        )
        _print_response(f"file-child-gap-multi source worker {file_name}", source_response)
        source_results = source_response.get("results", [])
        index_entry = next((item for item in source_results if item.get("op") == "touch_index_from_target" and item.get("ok")), {})
        canonical_gfid = str(index_entry.get("value_uuid") or "")
        if not canonical_gfid:
            raise RuntimeError(f"file-child-gap-multi worker did not return a canonical file GFID for {file_name}")
        file_gfid_path = _file_gfid_link_path(source_backend_root, canonical_gfid)

        missing_response = _canary_worker_command(
            missing_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                missing_backend_root,
                [
                    {
                        "op": "rm",
                        "path": file_gfid_path,
                        "recursive": False,
                        "force": True,
                    },
                    {
                        "op": "rm",
                        "path": backend_file,
                        "recursive": False,
                        "force": True,
                    },
                ],
            ),
        )
        _print_response(f"file-child-gap-multi target worker {file_name}", missing_response)

        files_state.append(
            {
                "file_name": file_name,
                "mount_file": mount_file,
                "backend_target": backend_file,
                "gfid_uuid": canonical_gfid,
                "gfid_hex": canonical_gfid.replace("-", ""),
                "file_gfid_path": file_gfid_path,
            }
        )

    _write_state(
        volume,
        scenario,
        {
            "kind": "file-child-gap-multi",
            "leave_heal_pending": True,
            "volume": volume,
            "scenario": scenario,
            "missing_host": missing_host,
            "source_host": source_host,
            "dir_name": dir_name,
            "file_names": file_names,
            "mount_root": mount_root,
            "mount_dir": mount_dir,
            "backend_root": source_backend_root,
            "backend_roots": backend_roots,
            "backend_dir": backend_dir,
            "index_root": index_root,
            "index_hosts": [source_host],
            "files": files_state,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest file-child-gap-multi canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical dir: {mount_dir}",
                f"  missing host: {missing_host}",
                f"  source marker host: {source_host}",
                f"  backend dir: {backend_dir}",
                f"  files: {', '.join(file_names)}",
                f"  index bucket: {index_root}",
                "Expected: each missing child should still repair one file at a time, even when the same brick is missing several files.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def build_file_child_gap_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = _read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind not in {"missing-file-replica", "file-child-gap", "file-child-gap-multi"}:
        raise RuntimeError(f"scenario {scenario!r} is not a file child-gap canary")

    mount_root = str(state.get("mount_root") or _volume_mount_root(volume))
    backend_root = str(state.get("backend_root") or "").strip()
    missing_host = str(state.get("missing_host") or "").strip()
    source_host = str(state.get("source_host") or "").strip()
    dir_name = str(state.get("dir_name") or "").strip()
    mount_dir = str(state.get("mount_dir") or "").strip()
    if kind == "file-child-gap-multi":
        files = state.get("files") or []
        if not files or not isinstance(files, list):
            raise RuntimeError(f"scenario {scenario!r} does not record enough file child-gap state to build a plan")
        actions: list[dict[str, Any]] = []
        for index, file_state in enumerate(files, start=1):
            if not isinstance(file_state, dict):
                continue
            mount_file = str(file_state.get("mount_file") or "").strip()
            backend_target = str(file_state.get("backend_target") or "").strip()
            file_gfid_path = str(file_state.get("file_gfid_path") or "").strip()
            gfid_uuid = str(file_state.get("gfid_uuid") or "").strip()
            file_name = str(file_state.get("file_name") or "").strip()
            if not mount_file or not backend_target or not gfid_uuid:
                continue
            stage_local_path = default_canary_stage_local_path(scenario, file_name)
            action = {
                "action_id": f"canary:{scenario}:{index:02d}",
                "logical_path": mount_file,
                "action_type": "repair_file",
                "object_type": "file",
                "depth": mount_file.count("/") + 1,
                "repair_strategy": "restore_missing_child_replica",
                "mounted_target": mount_file,
                "winner_host": source_host,
                "winner_backend": backend_target,
                "stage_local_path": stage_local_path,
                "missing_hosts": [missing_host],
                "healthy_hosts": [source_host],
                "file_canonical_host": source_host,
                "file_canonical_backend": backend_target,
                "file_copies": [
                    {
                        "host": source_host,
                        "backend": backend_target,
                        "gfid": gfid_uuid,
                        "file_gfid": gfid_uuid,
                        "backend_trusted_gfid": gfid_uuid,
                    }
                ],
                "graph_markers": ["file_child_gap:present", "file_restore:review"],
                "canary_kind": kind,
                "canary_volume": volume,
                "canary_name": scenario,
                "canary_observation_log": str(_observation_log_path(volume, scenario)),
                "notes": [
                    f"canary_kind:{kind}",
                    f"canary_volume:{volume}",
                    f"canary_name:{scenario}",
                    f"canary_observation_log:{_observation_log_path(volume, scenario)}",
                    "file-child-gap multi-child variant: keep the restore branch explicit so several missing children on the same brick can be exercised separately",
                    f"source host: {source_host}",
                    f"missing host: {missing_host}",
                    f"backend target: {backend_target}",
                    f"file GFID path: {file_gfid_path or 'unknown'}",
                    f"mount directory: {mount_dir or 'unknown'}",
                    f"backend root: {backend_root or 'unknown'}",
                    f"scenario directory: {dir_name or 'unknown'}",
                    f"file name: {file_name or 'unknown'}",
                ],
            }
            if file_gfid_path:
                action["stale_file_gfid_paths_by_host"] = {missing_host: [file_gfid_path]}
            actions.append(action)
        if not actions:
            raise RuntimeError(f"scenario {scenario!r} does not record enough file child-gap state to build a plan")
        return {
            "schema_version": 1,
            **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
            "actions": actions,
        }

    backend_target = str(state.get("backend_target") or "").strip()
    file_gfid_path = str(state.get("file_gfid_path") or "").strip()
    gfid_uuid = str(state.get("gfid_uuid") or "").strip()
    file_name = str(state.get("file_name") or "").strip()
    mount_file = str(state.get("mount_file") or "").strip()
    if not mount_file or not backend_target or not missing_host or not source_host:
        raise RuntimeError(f"scenario {scenario!r} does not record enough file child-gap state to build a plan")

    stage_local_path = default_canary_stage_local_path(scenario, file_name)
    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": mount_file,
        "action_type": "repair_file",
        "object_type": "file",
        "depth": mount_file.count("/") + 1,
        "repair_strategy": "restore_missing_child_replica",
        "mounted_target": mount_file,
        "winner_host": source_host,
        "winner_backend": backend_target,
        "stage_local_path": stage_local_path,
        "missing_hosts": [missing_host],
        "healthy_hosts": [source_host],
        "file_canonical_host": source_host,
        "file_canonical_backend": backend_target,
        "file_copies": [
            {
                "host": source_host,
                "backend": backend_target,
                "gfid": gfid_uuid,
                "file_gfid": gfid_uuid,
                "backend_trusted_gfid": gfid_uuid,
            }
        ],
        "graph_markers": ["file_child_gap:present", "file_restore:review"],
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            "file-child-gap variant: keep the restore branch explicit so the child-gap path can be exercised separately",
            f"source host: {source_host}",
            f"missing host: {missing_host}",
            f"backend target: {backend_target}",
            f"file GFID path: {file_gfid_path or 'unknown'}",
            f"mount directory: {mount_dir or 'unknown'}",
            f"backend root: {backend_root or 'unknown'}",
            f"scenario directory: {dir_name or 'unknown'}",
            f"file name: {file_name or 'unknown'}",
        ],
    }
    if file_gfid_path:
        action["stale_file_gfid_paths_by_host"] = {missing_host: [file_gfid_path]}

    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": [action],
    }


def create_file_stale_survivor_canary(
    *,
    volume: str,
    scenario: str,
    survivor_host: str | None = None,
    survivor_hosts: list[str] | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for file stale-survivor canary on {volume}")

    if survivor_hosts:
        chosen_survivors = [host for host in survivor_hosts if host in brick_hosts]
        if len(chosen_survivors) != len(survivor_hosts):
            raise RuntimeError(f"one or more survivor hosts are not part of the volume: {survivor_hosts}")
        if not chosen_survivors:
            raise RuntimeError("need at least one survivor host for file stale-survivor canary")
        survivor_host = chosen_survivors[0]
    else:
        survivor_host = _select_role_host(brick_hosts, survivor_host, role="survivor", default_to_next=False)
        chosen_survivors = [survivor_host]
    source_host = chosen_survivors[0]
    source_backend_root = backend_roots[source_host]
    remove_hosts = [host for host in brick_hosts if host not in chosen_survivors]
    if not remove_hosts:
        raise RuntimeError(f"need at least one replica to remove for file stale-survivor canary on {volume}")
    remove_backend_roots = {host: backend_roots[host] for host in remove_hosts}

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"
    index_root = f"{source_backend_root}/.glusterfs/indices/xattrop"
    canonical_gfid_hex = _new_gfid_hex()

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)

    survivor_response = _canary_worker_command(
        survivor_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            source_backend_root,
            [
                {
                    "op": "touch",
                    "path": backend_file,
                },
                {
                    "op": "setxattr",
                    "path": backend_file,
                    "name": "trusted.gfid",
                    "value_hex": canonical_gfid_hex,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": backend_file,
                },
                {
                    "op": "touch_index_from_target",
                    "target_path": backend_file,
                    "index_root": index_root,
                },
            ],
        ),
    )
    _print_response("file-stale-survivor source worker", survivor_response)
    survivor_results = survivor_response.get("results", [])
    link_entry = next(
        (item for item in survivor_results if item.get("op") == "link_file_gfid_from_target" and item.get("ok")),
        {},
    )
    canonical_gfid = str(link_entry.get("value_uuid") or "")
    if not canonical_gfid:
        raise RuntimeError("file stale-survivor canary worker did not return a canonical file GFID")
    file_gfid_path = str(link_entry.get("file_gfid_path") or _file_gfid_link_path(source_backend_root, canonical_gfid))
    brick_roles_by_host = canary_api._brick_roles_by_host(volume)

    for host in remove_hosts:
        remove_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                remove_backend_roots[host],
                [
                    {
                        "op": "rm",
                        "path": backend_file,
                        "recursive": False,
                        "force": True,
                    },
                    {
                        "op": "rm",
                        "path": file_gfid_path,
                        "recursive": False,
                        "force": True,
                    },
                ],
            ),
        )
        _print_response(f"file-stale-survivor prune worker {host}", remove_response)

    state_payload = {
        "kind": "file-stale-survivor",
        "volume": volume,
        "scenario": scenario,
        "survivor_host": survivor_host,
        "survivor_hosts": chosen_survivors,
        "remove_hosts": remove_hosts,
        "dir_name": dir_name,
        "file_name": file_name,
        "mount_root": mount_root,
        "mount_dir": mount_dir,
        "mount_file": mount_file,
        "backend_root": source_backend_root,
        "backend_roots": backend_roots,
        "backend_target": backend_file,
        "index_root": index_root,
        "index_hosts": [survivor_host],
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
        "file_gfid_path": file_gfid_path,
        "brick_roles_by_host": brick_roles_by_host,
        "cleanup_hints": {
            "stale_backends_by_host": {host: [backend_file] for host in chosen_survivors},
            "stale_file_gfid_paths_by_host": {host: [file_gfid_path] for host in chosen_survivors},
            "stale_gfid_paths_by_host": {host: [f"{index_root}/{canonical_gfid}"] for host in chosen_survivors},
        },
    }
    _record_file_baseline(volume, scenario, state_payload)
    _trigger_canary_heal(volume, scenario)

    print(
        "\n".join(
            [
                "Created gtest file-stale-survivor canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  survivor hosts: {', '.join(chosen_survivors)}",
                f"  removed hosts: {', '.join(remove_hosts)}",
                *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
                f"  backend target: {backend_file}",
                f"  file GFID: {canonical_gfid}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Expected: planner should treat this as probable stale survivor below quorum and default to delete.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_file_stale_survivor_pair_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, _backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 4:
        raise RuntimeError(f"need at least 4 bricks for file stale-survivor pair canary on {volume}")
    create_file_stale_survivor_canary(
        volume=volume,
        scenario=scenario,
        survivor_hosts=brick_hosts[:2],
        dir_name=dir_name,
        file_name=file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_file_data_split_brain_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    arbiter_host: str | None = None,
    trigger_heal: bool = True,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    brick_roles = canary_api._brick_roles(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for file data split-brain canary on {volume}")

    if arbiter_host:
        if arbiter_host not in brick_hosts:
            raise RuntimeError(f"arbiter host {arbiter_host} is not part of the volume")
    else:
        arbiter_host = next((host for host in brick_hosts if brick_roles.get(host) == "arbiter"), brick_hosts[-1])
    data_hosts = [host for host in brick_hosts if host != arbiter_host]
    if len(data_hosts) < 2:
        raise RuntimeError(f"need at least two data hosts besides {arbiter_host} for file data split-brain canary on {volume}")
    left_host = data_hosts[0]
    right_host = data_hosts[1]
    arbiter_backend_root = backend_roots[arbiter_host]
    left_backend_root = backend_roots[left_host]
    right_backend_root = backend_roots[right_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    left_backend_file = f"{left_backend_root}/{scenario}/{dir_name}/{file_name}"
    right_backend_file = f"{right_backend_root}/{scenario}/{dir_name}/{file_name}"
    arbiter_backend_file = f"{arbiter_backend_root}/{scenario}/{dir_name}/{file_name}"
    left_gfid_hex = _new_gfid_hex()
    right_gfid_hex = _new_gfid_hex()
    pending_value = "000000010000000000000001"

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_mount_file(mount_file)
    _run_local(["sudo", "-n", "stat", mount_file])

    left_response = _canary_worker_command(
        left_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            left_backend_root,
            [
                {
                    "op": "write_text",
                    "path": left_backend_file,
                    "text": f"{scenario} left cohort on {left_host}\n",
                },
                {
                    "op": "setxattr",
                    "path": left_backend_file,
                    "name": "trusted.gfid",
                    "value_hex": left_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": left_backend_file,
                    "name": "trusted.glusterfs.mdata",
                    "value_hex": left_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": left_backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(right_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "setxattr",
                    "path": left_backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(arbiter_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": left_backend_file,
                },
            ],
        ),
    )
    _print_response("file-data-split-brain left worker", left_response)

    right_response = _canary_worker_command(
        right_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            right_backend_root,
            [
                {
                    "op": "write_text",
                    "path": right_backend_file,
                    "text": f"{scenario} right cohort on {right_host}\n",
                },
                {
                    "op": "setxattr",
                    "path": right_backend_file,
                    "name": "trusted.gfid",
                    "value_hex": right_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": right_backend_file,
                    "name": "trusted.glusterfs.mdata",
                    "value_hex": left_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": right_backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(left_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "setxattr",
                    "path": right_backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(arbiter_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": right_backend_file,
                },
            ],
        ),
    )
    _print_response("file-data-split-brain right worker", right_response)

    arbiter_response = _canary_worker_command(
        arbiter_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            arbiter_backend_root,
            [
                {
                    "op": "touch",
                    "path": arbiter_backend_file,
                },
                {
                    "op": "setxattr",
                    "path": arbiter_backend_file,
                    "name": "trusted.gfid",
                    "value_hex": left_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": arbiter_backend_file,
                    "name": "trusted.glusterfs.mdata",
                    "value_hex": left_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": arbiter_backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(left_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "setxattr",
                    "path": arbiter_backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(right_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": arbiter_backend_file,
                },
            ],
        ),
    )
    _print_response("file-data-split-brain arbiter worker", arbiter_response)

    state_payload = {
        "kind": "file-data-split-brain",
        "leave_heal_pending": not trigger_heal,
        "volume": volume,
        "scenario": scenario,
        "dir_name": dir_name,
        "file_name": file_name,
        "mount_root": mount_root,
        "mount_dir": mount_dir,
        "mount_file": mount_file,
        "backend_root": left_backend_root,
        "backend_roots": backend_roots,
        "backend_target": left_backend_file,
        "left_host": left_host,
        "right_host": right_host,
        "arbiter_host": arbiter_host,
        "left_backend_target": left_backend_file,
        "right_backend_target": right_backend_file,
        "arbiter_backend_target": arbiter_backend_file,
        "left_gfid_hex": left_gfid_hex,
        "right_gfid_hex": right_gfid_hex,
        "left_gfid_uuid": str(uuid.UUID(hex=left_gfid_hex)),
        "right_gfid_uuid": str(uuid.UUID(hex=right_gfid_hex)),
        "left_file_gfid_path": _file_gfid_link_path(left_backend_root, str(uuid.UUID(hex=left_gfid_hex))),
        "right_file_gfid_path": _file_gfid_link_path(right_backend_root, str(uuid.UUID(hex=right_gfid_hex))),
        "arbiter_file_gfid_path": _file_gfid_link_path(arbiter_backend_root, str(uuid.UUID(hex=left_gfid_hex))),
        "index_root": f"{left_backend_root}/.glusterfs/indices/xattrop",
        "index_hosts": [left_host, right_host, arbiter_host],
        "brick_roles_by_host": brick_roles,
        "cleanup_hints": {
            "stale_backends_by_host": {
                left_host: [left_backend_file],
                right_host: [right_backend_file],
                arbiter_host: [arbiter_backend_file],
            },
            "stale_file_gfid_paths_by_host": {
                left_host: [_file_gfid_link_path(left_backend_root, str(uuid.UUID(hex=left_gfid_hex)))],
                right_host: [_file_gfid_link_path(right_backend_root, str(uuid.UUID(hex=right_gfid_hex)))],
                arbiter_host: [_file_gfid_link_path(arbiter_backend_root, str(uuid.UUID(hex=left_gfid_hex)))],
            },
        },
    }
    _record_file_baseline(volume, scenario, state_payload)
    if trigger_heal:
        _trigger_canary_heal(volume, scenario)
    else:
        print(
            "Leaving heal settings disabled; no heal crawl was launched. "
            "Use independent operator evidence to classify this fixture."
        )

    print(
        "\n".join(
            [
                "Created gtest file-data-split-brain canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  left host: {left_host}",
                f"  right host: {right_host}",
                f"  arbiter host: {arbiter_host}",
                *canary_api._format_brick_roles_by_host_for_notes(brick_roles),
                f"  left backend: {left_backend_file}",
                f"  right backend: {right_backend_file}",
                f"  arbiter backend: {arbiter_backend_file}",
                f"  left gfid: {state_payload['left_gfid_uuid']}",
                f"  right gfid: {state_payload['right_gfid_uuid']}",
                "Expected: arbiter evidence should support the data-side split, not win as payload.",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_orphaned_gfid_hardlink_canary(
    *,
    volume: str,
    scenario: str,
    orphan_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    trigger_heal: bool = True,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    orphan_host = _select_role_host(brick_hosts, orphan_host, role="orphan", default_to_next=True)
    backend_root = backend_roots[orphan_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{backend_root}/{scenario}/{dir_name}/{file_name}"
    canonical_gfid_hex = _new_gfid_hex()

    if trigger_heal:
        _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    else:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_mount_file(mount_file)
    _run_local(["sudo", "-n", "stat", mount_file])

    orphan_response = _canary_worker_command(
        orphan_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            backend_root,
            [
                {
                    "op": "mkdir",
                    "path": f"{backend_root}/{scenario}/{dir_name}",
                },
                {
                    "op": "touch",
                    "path": backend_file,
                },
                {
                    "op": "setxattr",
                    "path": backend_file,
                    "name": "trusted.gfid",
                    "value_hex": canonical_gfid_hex,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": backend_file,
                },
                {
                    "op": "rm",
                    "path": backend_file,
                    "recursive": False,
                    "force": True,
                },
            ],
        ),
    )
    _print_response("orphaned-gfid worker", orphan_response)
    _require_successful_worker_response("orphaned-gfid worker", orphan_response)
    orphan_results = orphan_response.get("results", [])
    link_entry = next((item for item in orphan_results if item.get("op") == "link_file_gfid_from_target" and item.get("ok")), {})
    canonical_gfid = str(link_entry.get("value_uuid") or "")
    if not canonical_gfid:
        raise RuntimeError("orphaned-gfid canary worker did not return a canonical file GFID")
    file_gfid_path = str(link_entry.get("file_gfid_path") or _file_gfid_link_path(backend_root, canonical_gfid))

    state_payload = {
        "kind": "orphaned-gfid-hardlink",
        "leave_heal_pending": not trigger_heal,
        "volume": volume,
        "scenario": scenario,
        "orphan_host": orphan_host,
        "dir_name": dir_name,
        "file_name": file_name,
        "mount_root": mount_root,
        "mount_dir": mount_dir,
        "mount_file": mount_file,
        "backend_root": backend_root,
        "backend_target": backend_file,
        "file_gfid_path": file_gfid_path,
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
    }
    _record_file_baseline(volume, scenario, state_payload)
    if trigger_heal:
        _trigger_canary_heal(volume, scenario)
    else:
        print("Leaving heal settings disabled; no heal crawl was launched. Use this only when the fixture already creates pending/index evidence.")

    print(
        "\n".join(
            [
                "Created gtest orphaned-gfid-hardlink canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  orphan host: {orphan_host}",
                f"  backend target: {backend_file}",
                f"  orphaned gfid path: {file_gfid_path}",
                f"  canonical file GFID: {canonical_gfid}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Expected: planner should treat the bare .glusterfs hardlink as dead GFID residue once no live reference remains.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_symlink_missing_stale_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    trigger_heal: bool = True,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for symlink missing/stale canary on {volume}")

    if len(brick_hosts) >= 4:
        keep_hosts = brick_hosts[:2]
        stale_hosts = brick_hosts[2:4]
    else:
        keep_hosts = brick_hosts[:1]
        stale_hosts = brick_hosts[1:3]
    keep_backend_root = backend_roots[keep_hosts[0]]
    stale_backend_roots = {host: backend_roots[host] for host in stale_hosts}
    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_link = f"{mount_dir}/{file_name}"
    backend_link = f"{keep_backend_root}/{scenario}/{dir_name}/{file_name}"
    backend_links_by_host = {
        host: f"{root}/{scenario}/{dir_name}/{file_name}"
        for host, root in backend_roots.items()
    }
    missing_target = f"/var/tmp/gluster-repair-symlink-target/{scenario}/{dir_name}/{file_name}"

    if trigger_heal:
        _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    else:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_symlink(mount_link, missing_target)

    probe_response = {}
    probe_host = ""
    canonical_gfid = ""
    for host in keep_hosts:
        probe_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                keep_backend_root,
                [
                    {
                        "op": "getxattr",
                        "path": backend_link,
                        "name": "trusted.gfid",
                    },
                ],
            ),
        )
        _print_response("symlink-missing-stale probe worker", probe_response)
        probe_results = probe_response.get("results", [])
        gfid_entry = next((item for item in probe_results if item.get("op") == "getxattr" and item.get("ok")), {})
        canonical_gfid = str(gfid_entry.get("value_uuid") or "")
        if canonical_gfid:
            probe_host = host
            break
    if not canonical_gfid:
        raise RuntimeError("symlink canary worker did not return a canonical file GFID")
    file_gfid_path = _file_gfid_link_path(keep_backend_root, canonical_gfid)
    file_gfid_paths_by_host = {
        host: _file_gfid_link_path(root, canonical_gfid)
        for host, root in backend_roots.items()
    }
    backend_root = keep_backend_root

    for host in stale_hosts:
        prune_response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                stale_backend_roots[host],
                [
                    {
                        "op": "rm",
                        "path": backend_links_by_host[host],
                        "recursive": False,
                        "force": True,
                    },
                ],
            ),
        )
        _print_response(f"symlink-missing-stale prune worker {host}", prune_response)
        _require_successful_worker_response(f"symlink-missing-stale prune worker {host}", prune_response)

    _write_state(
        volume,
        scenario,
        {
            "kind": "symlink-missing-stale",
            "leave_heal_pending": not trigger_heal,
            "volume": volume,
            "scenario": scenario,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_link,
            "backend_root": backend_root,
            "backend_target": backend_link,
            "backend_targets_by_host": backend_links_by_host,
            "missing_target": missing_target,
            "symlink_hosts": brick_hosts,
            "keep_hosts": keep_hosts,
            "stale_hosts": stale_hosts,
            "probe_host": probe_host,
            "gfid_uuid": canonical_gfid,
            "gfid_hex": canonical_gfid.replace("-", ""),
            "file_gfid_path": file_gfid_path,
            "file_gfid_paths_by_host": file_gfid_paths_by_host,
        },
    )
    if trigger_heal:
        _trigger_canary_heal(volume, scenario)
    else:
        print("Leaving heal settings disabled; no heal crawl was launched. Use this only when the fixture already creates pending/index evidence.")

    print(
        "\n".join(
            [
                "Created gtest symlink-missing-stale canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_link}",
                f"  missing target: {missing_target}",
                f"  keep hosts: {', '.join(keep_hosts)}",
                f"  stale hosts: {', '.join(stale_hosts)}",
                f"  probe host: {probe_host or keep_hosts[0]}",
                f"  backend target: {backend_link}",
                f"  file GFID: {canonical_gfid}",
                "Expected: planner should treat this as orphaned symlink residue, not a normal live symlink repair.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_symlink_brick_outage_canary(
    *,
    volume: str,
    scenario: str,
    outage_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for symlink brick-outage canary on {volume}")

    outage_host = _select_role_host(brick_hosts, outage_host, role="outage", default_to_next=True)
    backend_root = backend_roots[outage_host]
    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_link = f"{mount_dir}/{file_name}"
    backend_link = f"{backend_root}/{scenario}/{dir_name}/{file_name}"
    missing_target = f"/var/tmp/gluster-repair-symlink-target/{scenario}/{dir_name}/{file_name}"
    host_ops_path = str(DEFAULT_HOST_OPS_PATH)

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    outage_brick_down = False
    outage_kicked = False
    mount_snapshot: dict[str, Any] = {}
    heal_info_before = ""
    heal_statistics = ""
    heal_info_after = ""
    brick_observations: list[dict[str, Any]] = []
    canonical_gfid = ""
    file_gfid_path = ""
    brick_down_error = ""
    brick_kick_error = ""

    try:
        try:
            _run_remote_host_ops(
                outage_host,
                ["brick-down", "--volume", volume, "--brick", backend_root],
                ssh_user=ssh_user,
                host_ops_path=host_ops_path,
            )
        except Exception as exc:
            brick_down_error = str(exc)
        outage_brick_down = not _brick_status_is_online(volume, outage_host, backend_root, ssh_user=ssh_user)
        if not outage_brick_down:
            raise RuntimeError(brick_down_error or f"brick-down did not take {outage_host} offline")
        _run_local(["sleep", "3"])

        _ensure_canary_mount(volume, mount_root)
        _local_mount_dir(mount_dir)
        _local_symlink(mount_link, missing_target)
        mount_snapshot = _capture_mount_symlink_snapshot(mount_link)
        heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    finally:
        if outage_brick_down:
            try:
                _run_remote_host_ops(
                    outage_host,
                    ["brick-kick", "--volume", volume],
                    ssh_user=ssh_user,
                    host_ops_path=host_ops_path,
                )
            except Exception as exc:
                brick_kick_error = str(exc)
            outage_kicked = _brick_status_is_online(volume, outage_host, backend_root, ssh_user=ssh_user)
            if not outage_kicked:
                raise RuntimeError(brick_kick_error or f"brick-kick did not bring {outage_host} back online")

    if not outage_kicked:
        raise RuntimeError(f"failed to bring outage brick back on {outage_host}")

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "enable"])
    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "full"])
    heal_statistics = _wait_for_full_heal_crawl(volume)
    heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])

    for host in brick_hosts:
        response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(
                volume,
                scenario,
                backend_root,
                [
                    {
                        "op": "inspect_path",
                        "label": "symlink-backend",
                        "path": backend_link,
                    }
                ],
            ),
        )
        _print_response("symlink-brick-outage worker", response)
        brick_observations.append({"host": response.get("host") or host, "results": response.get("results", [])})

    first_valid = next(
        (
            result
            for item in brick_observations
            for result in item.get("results", [])
            if result.get("ok") and str(result.get("trusted_gfid") or "")
        ),
        {},
    )
    if first_valid:
        canonical_gfid = str(first_valid.get("trusted_gfid") or "")
        file_gfid_path = _file_gfid_link_path(backend_root, canonical_gfid)

    _write_state(
        volume,
        scenario,
        {
            "kind": "symlink-brick-outage",
            "volume": volume,
            "scenario": scenario,
            "outage_host": outage_host,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_dir,
            "mount_file": mount_link,
            "backend_root": backend_root,
            "backend_target": backend_link,
            "missing_target": missing_target,
            "symlink_hosts": brick_hosts,
            "brick_hosts": brick_hosts,
            "gfid_uuid": canonical_gfid,
            "gfid_hex": canonical_gfid.replace("-", ""),
            "file_gfid_path": file_gfid_path,
            "mount_snapshot": mount_snapshot,
            "heal_info_before": heal_info_before,
            "heal_info_after": heal_info_after,
            "heal_statistics": heal_statistics,
            "brick_observations": brick_observations,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest symlink-brick-outage canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_link}",
                f"  outage host: {outage_host}",
                f"  missing target: {missing_target}",
                f"  backend target: {backend_link}",
                f"  file GFID: {canonical_gfid or '(not observed)'}",
                f"  mount readlink: {mount_snapshot.get('readlink', '') or '(missing)'}",
                f"  heal info before full heal:\n{heal_info_before or '(empty)'}",
                f"  heal statistics after full heal:\n{heal_statistics or '(empty)'}",
                f"  heal info after full heal:\n{heal_info_after or '(empty)'}",
                "  brick observations:",
                *[
                    f"    - {item.get('host', '')}: {len(item.get('results', []))} inspected path(s)"
                    for item in brick_observations
                ],
                f"Next: cleanup {volume} {scenario} after confirming the brick-side target strings.",
            ]
        )
    )


def create_file_handle_ghost_canary(
    *,
    volume: str,
    scenario: str,
    ghost_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    trigger_heal: bool = True,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    ghost_host = _select_role_host(brick_hosts, ghost_host, role="ghost", default_to_next=True)
    source_host = next((host for host in brick_hosts if host != ghost_host), "")
    if not source_host:
        raise RuntimeError(f"need at least one source host besides {ghost_host}")
    source_backend_root = backend_roots[source_host]
    ghost_backend_root = backend_roots[ghost_host]

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    backend_file = f"{source_backend_root}/{scenario}/{dir_name}/{file_name}"
    index_root = f"{source_backend_root}/.glusterfs/indices/xattrop"
    pending_value = "000000010000000000000001"
    canonical_gfid_hex = _new_gfid_hex()

    if trigger_heal:
        _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    else:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_mount_file(mount_file)
    _run_local(["sudo", "-n", "stat", mount_file])
    time.sleep(3)

    source_response = _canary_worker_command(
        source_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            source_backend_root,
            [
                {
                    "op": "mkdir",
                    "path": f"{source_backend_root}/{scenario}/{dir_name}",
                },
                {
                    "op": "touch",
                    "path": backend_file,
                },
                {
                    "op": "setxattr",
                    "path": backend_file,
                    "name": "trusted.gfid",
                    "value_hex": canonical_gfid_hex,
                },
                {
                    "op": "setxattr",
                    "path": backend_file,
                    "name": f"trusted.afr.{volume}-client-{brick_hosts.index(ghost_host)}",
                    "value_hex": pending_value,
                },
                {
                    "op": "touch_index_from_target",
                    "target_path": backend_file,
                    "index_root": index_root,
                },
            ],
        ),
    )
    _print_response("file-handle-ghost source worker", source_response)
    _require_successful_worker_response("file-handle-ghost source worker", source_response)
    source_results = source_response.get("results", [])
    index_entry = next((item for item in source_results if item.get("op") == "touch_index_from_target" and item.get("ok")), {})
    canonical_gfid = str(index_entry.get("value_uuid") or "")
    if not canonical_gfid:
        raise RuntimeError("file-handle-ghost canary worker did not return a canonical file GFID")
    ghost_gfid_path = _file_gfid_link_path(ghost_backend_root, canonical_gfid)
    ghost_ref = f"{mount_root}/{scenario}/{dir_name}/ghost-{file_name}"

    ghost_response = _canary_worker_command(
        ghost_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=_make_request(
            volume,
            scenario,
            ghost_backend_root,
            [
                {
                    "op": "write_text",
                    "path": ghost_gfid_path,
                    "text": "handle-ghost\n",
                },
                {
                    "op": "setxattr",
                    "path": ghost_gfid_path,
                    "name": "trusted.gfid",
                    "value_hex": canonical_gfid.replace("-", ""),
                },
                {
                    "op": "setxattr",
                    "path": ghost_gfid_path,
                    "name": "trusted.gfid2path.canary",
                    "value_hex": ghost_ref.encode("utf-8").hex(),
                },
            ],
        ),
    )
    _print_response("file-handle-ghost ghost worker", ghost_response)
    _require_successful_worker_response("file-handle-ghost ghost worker", ghost_response)

    state_payload = {
        "kind": "file-handle-ghost",
        "leave_heal_pending": not trigger_heal,
        "volume": volume,
        "scenario": scenario,
        "ghost_host": ghost_host,
        "source_host": source_host,
        "dir_name": dir_name,
        "file_name": file_name,
        "mount_root": mount_root,
        "mount_dir": mount_dir,
        "mount_file": mount_file,
        "backend_root": source_backend_root,
        "backend_roots": backend_roots,
        "backend_target": backend_file,
        "index_root": index_root,
        "index_host": source_host,
        "gfid_uuid": canonical_gfid,
        "gfid_hex": canonical_gfid.replace("-", ""),
        "ghost_ref": ghost_ref,
        "ghost_gfid_path": ghost_gfid_path,
    }
    _record_file_baseline(volume, scenario, state_payload)
    if trigger_heal:
        _trigger_canary_heal(volume, scenario)
    else:
        print("Leaving heal settings disabled; no heal crawl was launched. Use this only when the fixture already creates pending/index evidence.")

    print(
        "\n".join(
            [
                "Created file-handle-ghost canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  ghost host: {ghost_host}",
                f"  source host: {source_host}",
                f"  backend target: {backend_file}",
                f"  ghost gfid path: {ghost_gfid_path}",
                f"  ghost backlink target: {ghost_ref}",
                f"  canonical file GFID: {canonical_gfid}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Expected: planner should choose cleanup_dead_gfid once the handle ghost is recognized.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )
