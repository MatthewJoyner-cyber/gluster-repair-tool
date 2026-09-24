# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Batched gtest canary manager helpers."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .executor import execute_apply_results, validate_execute_results
from .models import ApplyActionResult, ApplyStep
from .install_paths import DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from .controller_paths import default_repair_run_dir
from .protocol import CanaryBatchRequest
from .remote_ops import ssh_remote_command
from .planner_payload import load_plan_actions
from .shared_io import write_json_shared
from .volume import discover_brick_paths, normalize_host_alias, parse_bricks, parse_common_brick_path, parse_volume_type
from .canary_directory import create_child_gap_canary
from .canary_directory import create_directory_backend_child_gap_canary
from .canary_directory import create_directory_child_gap_canary
from .canary_directory import create_directory_child_reference_canary
from .canary_directory import build_directory_backend_child_gap_plan_from_canary_state
from .canary_directory import build_directory_mdata_no_majority_plan_from_canary_state
from .canary_directory import create_directory_backend_child_gap_bounded_canary
from .canary_directory import create_directory_backend_child_gap_canonical_canary
from .canary_directory import create_directory_backend_child_gap_mixed_canary
from .canary_directory import create_directory_gfid_merge_canary
from .canary_directory import create_directory_gfid_mask_canary
from .canary_directory import create_directory_gfid_tie_canary
from .canary_directory import create_directory_metadata_canary
from .canary_directory import create_directory_mdata_no_majority_canary
from .canary_directory import create_directory_stale_survivor_canary
from .canary_directory import create_type_mismatch_canary
from .canary_directory import build_type_mismatch_plan_from_canary_state
from .canary_directory import observe_directory_child_state_canary
from .canary_directory import observe_directory_gfid_state_canary
from .canary_file import create_file_ctime_metadata_canary
from .canary_file import create_file_child_gap_canary
from .canary_file import create_file_child_gap_multi_canary
from .canary_file import create_file_data_split_brain_canary as _create_file_data_split_brain_canary_impl
from .canary_file import build_file_metadata_split_brain_plan_from_canary_state
from .canary_file import build_file_posix_metadata_split_brain_plan_from_canary_state
from .canary_file import build_file_child_gap_plan_from_canary_state
from .canary_file import create_file_handle_ghost_canary
from .canary_file import create_file_metadata_canary
from .canary_file import create_file_posix_metadata_canary
from .canary_file import create_file_posix_metadata_split_brain_canary
from .canary_file import create_file_posix_acl_majority_canary
from .canary_file import create_file_native_pending_metadata_canary
from .canary_file import create_directory_posix_metadata_canary
from .canary_file import create_directory_posix_default_acl_majority_canary
from .canary_file import create_file_metadata_split_brain_canary
from .canary_file import create_file_stale_survivor_canary
from .canary_file import create_file_stale_survivor_pair_canary
from .canary_file import create_arbiter_missing_gfid_canary
from .canary_file import create_arbiter_only_residue_canary
from .canary_file import create_missing_file_replica_canary
from .canary_file import create_missing_file_replica_pair_canary
from .canary_file import create_orphaned_gfid_hardlink_canary
from .canary_file import create_symlink_brick_outage_canary
from .canary_file import create_symlink_missing_stale_canary
from .canary_file import _canary_temp_mount_root
from .canary_file import _ensure_canary_mount
from .canary_file import _mountpoint_is_active
from .canary_file import _unmount_canary_mount
from .canary_shared import _brick_hosts_and_paths
from .canary_shared import prepare_canary_workspace
from .canary_shared import _require_successful_worker_response
from .canary_file_observation import observe_file_state_canary
from .volume import set_heal_settings



def _require_not_root() -> None:
    if os.getuid() == 0:
        raise RuntimeError("run this as the operator user, not root")


def _default_scenario_name() -> str:
    return f"repair-canary-{uuid.uuid4().hex[:8]}"


def _new_gfid_hex() -> str:
    return uuid.uuid4().hex


def _volume_mount_root(volume: str) -> str:
    return f"/{volume.strip().strip('/')}"


def _run_local(command: list[str]) -> None:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"command failed: {subprocess.list2cmdline(command)}")


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


def _cleanup_action_type_for_kind(kind: str) -> tuple[str, str]:
    if kind == "entry-heal":
        return ("cleanup_stale_glusterfs_index", "delete_stale_glusterfs_index_residue")
    if kind in {"directory-metadata", "directory-mdata-no-majority"}:
        return ("cleanup_dead_gfid", "delete_dead_gfid_residue")
    if kind in {"directory-gfid-merge", "directory-gfid-tie", "directory-gfid-mask", "type-mismatch"}:
        return ("cleanup_dead_gfid", "delete_dead_gfid_residue")
    if kind == "file-ctime-metadata":
        return ("cleanup_dead_file_refs", "delete_dead_file_ref_residue")
    if kind == "file-stale-survivor":
        return ("cleanup_dead_file_refs", "delete_dead_file_ref_residue")
    if kind == "directory-stale-survivor":
        return ("cleanup_dead_gfid", "delete_dead_gfid_residue")
    if kind == "file-handle-ghost":
        return ("cleanup_dead_gfid", "delete_dead_gfid_residue")
    if kind == "orphaned-gfid-hardlink":
        return ("cleanup_dead_gfid", "delete_dead_gfid_residue")
    if kind in {"file-gfid-split", "file-content-split-brain", "file-data-split-brain"}:
        return ("cleanup_dead_file_refs", "delete_dead_file_ref_residue")
    if kind == "symlink-missing-stale":
        return ("cleanup_orphaned_symlink", "delete_orphaned_symlink_residue")
    if kind == "symlink-brick-outage":
        return ("cleanup_orphaned_symlink", "delete_orphaned_symlink_residue")
    if kind in {"child-gap", "directory-child-reference", "directory-backend-child-gap-mixed"}:
        return ("cleanup_dead_file_refs", "delete_dead_file_ref_residue")
    return ("cleanup_dead_file_refs", "delete_dead_file_ref_residue")


def _cleanup_step(
    *,
    action_id: str,
    index: int,
    label: str,
    step_type: str,
    host: str,
    path: str,
    ssh_user: str,
    recursive: bool = False,
    tolerate_missing: bool = True,
) -> ApplyStep:
    preview = (
        ssh_remote_command(host, ["rm", "-rf", "--", path], ssh_user=ssh_user)
        if recursive
        else ssh_remote_command(host, ["rm", "-f", "--", path], ssh_user=ssh_user)
    )
    return ApplyStep(
        step_id=f"{action_id}:{index:02d}:{label}",
        step_type=step_type,
        host=host,
        target_path=path,
        tolerate_missing=tolerate_missing,
        command_preview=preview,
        notes=["synthetic canary teardown executed through the repair executor"],
    )


def _build_cleanup_result(
    *,
    volume: str,
    scenario: str,
    kind: str,
    mount_root: str,
    backend_root: str,
    brick_roots: dict[str, str] | None = None,
    brick_hosts: list[str],
    ssh_user: str,
    state: dict[str, Any],
) -> ApplyActionResult:
    action_type, strategy = _cleanup_action_type_for_kind(kind)
    action_id = f"canary-cleanup-{scenario}"
    logical_path = f"{mount_root}/{scenario}"
    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=action_type,
        execution_mode="plan",
        backup_root="",
        backup_mode="none",
        batch=True,
        status="proposed",
        notes=[
            f"repair strategy: {strategy}",
            "configured batch policy: delete",
            "synthetic canary teardown; execute via the repair engine",
        ],
    )

    brick_roles_by_host = {
        str(host): str(role)
        for host, role in (state.get("brick_roles_by_host") or {}).items()
        if str(host) and str(role)
    }
    if brick_roles_by_host:
        result.brick_roles_by_host = brick_roles_by_host

    scenario_root = str(state.get("backend_root") or backend_root)
    cleanup_hints = state.get("cleanup_hints") or {}
    if not isinstance(cleanup_hints, dict):
        cleanup_hints = {}
    index = 1
    for host in brick_hosts:
        host_root = scenario_root
        if brick_roots is not None:
            host_root = brick_roots.get(normalize_host_alias(host), host_root)
        result.steps.append(
            _cleanup_step(
                action_id=action_id,
                index=index,
                label="remove-scenario-root",
                step_type="remove_stale_backend",
                host=host,
                path=f"{host_root}/{scenario}",
                ssh_user=ssh_user,
                recursive=True,
            )
        )
        index += 1

    if kind in {"arbiter-missing-gfid", "arbiter-only-residue"}:
        arbiter_host = str(state.get("arbiter_host") or "")
        arbiter_handle = str(state.get("arbiter_handle") or "")
        if arbiter_host and arbiter_handle:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-arbiter-gfid-handle",
                    step_type="remove_stale_gfid",
                    host=arbiter_host,
                    path=arbiter_handle,
                    ssh_user=ssh_user,
                )
            )
            index += 1

    index_root = str(state.get("index_root") or "")
    entry_name = str(state.get("entry_name") or "")
    parent_gfid_uuid = str(state.get("parent_gfid_uuid") or state.get("gfid_uuid") or "")
    if kind == "entry-heal" and index_root and entry_name and parent_gfid_uuid:
        result.steps.append(
            _cleanup_step(
                action_id=action_id,
                index=index,
                label="remove-entry-index",
                step_type="remove_stale_gfid",
                host=str(state.get("index_host") or state.get("marker_host") or ""),
                path=f"{index_root}/{parent_gfid_uuid}/{entry_name}",
                ssh_user=ssh_user,
            )
        )
        index += 1

    if kind == "file-child-gap-multi":
        files = state.get("files") or []
        index_root = str(state.get("index_root") or "")
        index_hosts = [str(item) for item in state.get("index_hosts") or [] if str(item)]
        if not index_hosts:
            source_host = str(state.get("source_host") or "")
            if source_host:
                index_hosts = [source_host]
        if isinstance(files, list):
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    continue
                file_gfid_path = str(file_entry.get("file_gfid_path") or "")
                gfid_uuid = str(file_entry.get("gfid_uuid") or "")
                missing_host = str(state.get("missing_host") or "")
                if file_gfid_path and missing_host:
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-file-gfid",
                            step_type="remove_stale_file_gfid",
                            host=missing_host,
                            path=file_gfid_path,
                            ssh_user=ssh_user,
                        )
                    )
                    index += 1
                if index_root and gfid_uuid and index_hosts:
                    for index_host in index_hosts:
                        result.steps.append(
                            _cleanup_step(
                                action_id=action_id,
                                index=index,
                                label="remove-file-index",
                                step_type="remove_stale_gfid",
                                host=index_host,
                                path=f"{index_root}/{gfid_uuid}",
                                ssh_user=ssh_user,
                            )
                        )
                        index += 1
    if kind == "file-child-gap":
        index_root = str(state.get("index_root") or "")
        gfid_uuid = str(state.get("gfid_uuid") or "")
        file_gfid_path = str(state.get("file_gfid_path") or "")
        missing_host = str(state.get("missing_host") or "")
        if file_gfid_path and missing_host:
            missing_root = scenario_root
            if brick_roots is not None:
                missing_root = brick_roots.get(normalize_host_alias(missing_host), missing_root)
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-file-gfid",
                    step_type="remove_stale_file_gfid",
                    host=missing_host,
                    path=_file_gfid_link_path(missing_root, gfid_uuid),
                    ssh_user=ssh_user,
                )
            )
            index += 1
        if index_root and gfid_uuid:
            # Native healing can create this fixture's marker on any brick.
            for index_host in brick_hosts:
                index_root_for_host = scenario_root
                if brick_roots is not None:
                    index_root_for_host = brick_roots.get(normalize_host_alias(index_host), index_root_for_host)
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-file-index",
                        step_type="remove_stale_gfid",
                        host=index_host,
                        path=f"{index_root_for_host}/.glusterfs/indices/xattrop/{gfid_uuid}",
                        ssh_user=ssh_user,
                    )
                )
                index += 1
    if kind == "directory-backend-child-gap-mixed":
        files = state.get("file_children") or []
        if isinstance(files, list):
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    continue
                file_gfid_path = str(file_entry.get("file_gfid_path") or "")
                mixed_host = str(state.get("missing_host") or "")
                if file_gfid_path and mixed_host:
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-mixed-file-gfid",
                            step_type="remove_stale_file_gfid",
                            host=mixed_host,
                            path=file_gfid_path,
                            ssh_user=ssh_user,
                        )
                    )
                    index += 1
    if kind == "missing-file-replica":
        file_gfid_path = str(state.get("file_gfid_path") or "")
        missing_host = str(state.get("missing_host") or "")
        if file_gfid_path and missing_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-file-gfid",
                    step_type="remove_stale_file_gfid",
                    host=missing_host,
                    path=file_gfid_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1
    if kind == "file-stale-survivor":
        file_gfid_path = str(state.get("file_gfid_path") or "")
        index_root = str(state.get("index_root") or "")
        gfid_uuid = str(state.get("gfid_uuid") or "")
        cleanup_hosts = [str(item) for item in state.get("remove_hosts") or [] if str(item)]
        index_hosts = [str(item) for item in state.get("index_hosts") or [] if str(item)]
        hinted_backends_by_host = cleanup_hints.get("stale_backends_by_host") or state.get("stale_backends_by_host") or {}
        hinted_file_gfid_paths_by_host = cleanup_hints.get("stale_file_gfid_paths_by_host") or state.get("stale_file_gfid_paths_by_host") or {}
        hinted_gfid_paths_by_host = cleanup_hints.get("stale_gfid_paths_by_host") or state.get("stale_gfid_paths_by_host") or {}
        if not cleanup_hosts:
            survivor_host = str(state.get("survivor_host") or "")
            if survivor_host:
                cleanup_hosts = [survivor_host]
        if index_root and gfid_uuid and isinstance(hinted_gfid_paths_by_host, dict):
            stale_index_path = f"{index_root}/{gfid_uuid}"
            for host in index_hosts or cleanup_hosts:
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-stale-survivor-index",
                        step_type="remove_stale_gfid",
                        host=host,
                        path=stale_index_path,
                        ssh_user=ssh_user,
                    )
                )
                index += 1
                if file_gfid_path:
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-stale-survivor-file-gfid",
                            step_type="remove_stale_file_gfid",
                            host=host,
                            path=file_gfid_path,
                            ssh_user=ssh_user,
                        )
                    )
                    index += 1
        for host in cleanup_hosts:
            if file_gfid_path:
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-stale-survivor-file-gfid",
                        step_type="remove_stale_file_gfid",
                        host=host,
                        path=file_gfid_path,
                        ssh_user=ssh_user,
                    )
                )
                index += 1
        if isinstance(hinted_backends_by_host, dict):
            for host, paths in sorted(hinted_backends_by_host.items()):
                for path in paths or []:
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-stale-survivor-backend",
                            step_type="remove_stale_backend",
                            host=str(host),
                            path=str(path),
                            ssh_user=ssh_user,
                        )
                    )
                    index += 1
        if isinstance(hinted_file_gfid_paths_by_host, dict):
            for host, paths in sorted(hinted_file_gfid_paths_by_host.items()):
                for path in paths or []:
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-stale-survivor-file-gfid-hint",
                            step_type="remove_stale_file_gfid",
                            host=str(host),
                            path=str(path),
                            ssh_user=ssh_user,
                        )
                    )
                    index += 1
        if isinstance(hinted_gfid_paths_by_host, dict):
            for host, paths in sorted(hinted_gfid_paths_by_host.items()):
                for path in paths or []:
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-stale-survivor-gfid-hint",
                            step_type="remove_stale_gfid",
                            host=str(host),
                            path=str(path),
                            ssh_user=ssh_user,
                        )
            )
            index += 1
    if kind == "directory-stale-survivor":
        directory_gfid_path = str(state.get("directory_gfid_path") or "")
        survivor_host = str(state.get("survivor_host") or "")
        backend_target = str(state.get("backend_target") or "")
        if backend_target and survivor_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-directory-backend-target",
                    step_type="remove_stale_backend",
                    host=survivor_host,
                    path=backend_target,
                    ssh_user=ssh_user,
                    recursive=True,
                )
            )
            index += 1
        if directory_gfid_path and survivor_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-directory-gfid",
                    step_type="remove_stale_dir_gfid",
                    host=survivor_host,
                    path=directory_gfid_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1
    if kind == "orphaned-gfid-hardlink":
        file_gfid_path = str(state.get("file_gfid_path") or "")
        orphan_host = str(state.get("orphan_host") or "")
        backend_target = str(state.get("backend_target") or "")
        if backend_target and orphan_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-orphaned-backend-target",
                    step_type="remove_stale_backend",
                    host=orphan_host,
                    path=backend_target,
                    ssh_user=ssh_user,
                )
            )
            index += 1
        if file_gfid_path and orphan_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-orphaned-gfid",
                    step_type="remove_stale_file_gfid",
                    host=orphan_host,
                    path=file_gfid_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1
    if kind == "file-handle-ghost":
        ghost_gfid_path = str(state.get("ghost_gfid_path") or "")
        ghost_host = str(state.get("ghost_host") or "")
        index_root = str(state.get("index_root") or "")
        gfid_uuid = str(state.get("gfid_uuid") or "")
        index_host = str(state.get("index_host") or state.get("source_host") or "")
        if ghost_gfid_path and ghost_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-handle-ghost",
                    step_type="remove_stale_file_gfid",
                    host=ghost_host,
                    path=ghost_gfid_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1
        if index_root and gfid_uuid and index_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-ghost-index",
                    step_type="remove_stale_index_ghost",
                    host=index_host,
                    path=f"{index_root}/{gfid_uuid}",
                    ssh_user=ssh_user,
                )
            )
            index += 1
    if kind in {"symlink-missing-stale", "symlink-brick-outage"}:
        symlink_hosts = [str(item) for item in state.get("symlink_hosts", []) if str(item)]
        stale_hosts = [str(item) for item in state.get("stale_hosts", []) if str(item)]
        cleanup_hosts = list(dict.fromkeys(symlink_hosts + stale_hosts))
        backend_target = str(state.get("backend_target") or "")
        backend_targets_by_host = {
            normalize_host_alias(str(host)): str(path)
            for host, path in (state.get("backend_targets_by_host") or {}).items()
            if str(host) and str(path)
        }
        file_gfid_path = str(state.get("file_gfid_path") or "")
        file_gfid_paths_by_host = {
            normalize_host_alias(str(host)): str(path)
            for host, path in (state.get("file_gfid_paths_by_host") or {}).items()
            if str(host) and str(path)
        }
        if backend_target:
            for host in cleanup_hosts:
                path = backend_targets_by_host.get(normalize_host_alias(host), backend_target)
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-symlink-backend",
                        step_type="remove_stale_backend",
                        host=host,
                        path=path,
                        ssh_user=ssh_user,
                    )
                )
                index += 1
        if file_gfid_path:
            for host in cleanup_hosts:
                path = file_gfid_paths_by_host.get(normalize_host_alias(host), file_gfid_path)
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-symlink-file-gfid",
                        step_type="remove_stale_file_gfid",
                        host=host,
                        path=path,
                        ssh_user=ssh_user,
                    )
                )
                index += 1
    if kind == "file-content-split-brain":
        content_gfid_paths_by_host = {
            normalize_host_alias(str(host)): str(path)
            for host, path in (state.get("file_gfid_paths_by_host") or {}).items()
            if str(host) and str(path)
        }
        content_gfid_path = str(state.get("file_gfid_path") or "")
        content_hosts = [
            str(host)
            for host in (state.get("left_hosts") or []) + (state.get("right_hosts") or [])
            if str(host)
        ]
        for host in dict.fromkeys(content_hosts):
            path = content_gfid_paths_by_host.get(normalize_host_alias(host), content_gfid_path)
            if path:
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-content-split-file-gfid",
                        step_type="remove_stale_file_gfid",
                        host=host,
                        path=path,
                        ssh_user=ssh_user,
                    )
                )
                index += 1
        content_index_roots_by_host = {
            normalize_host_alias(str(host)): str(root)
            for host, root in (state.get("index_roots_by_host") or {}).items()
            if str(host) and str(root)
        }
        content_index_root = str(state.get("index_root") or "")
        content_gfid = str(state.get("gfid_uuid") or state.get("left_gfid_uuid") or "")
        content_index_hosts = [
            str(host)
            for host in state.get("index_hosts") or content_hosts
            if str(host)
        ]
        for host in dict.fromkeys(content_index_hosts):
            root = content_index_roots_by_host.get(normalize_host_alias(host), content_index_root)
            if root and content_gfid:
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-content-split-index",
                        step_type="remove_stale_gfid",
                        host=host,
                        path=f"{root}/{content_gfid}",
                        ssh_user=ssh_user,
                    )
                )
                index += 1

    if kind in {"file-gfid-split", "file-data-split-brain"}:
        for host_key, path_key in (("left_hosts", "left_file_gfid_path"), ("right_hosts", "right_file_gfid_path")):
            hosts = [str(item) for item in state.get(host_key, []) if str(item)]
            path = str(state.get(path_key) or "")
            if hosts and path:
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label=f"remove-{host_key.removesuffix('_hosts')}-gfid",
                        step_type="remove_stale_file_gfid",
                        host=hosts[0],
                        path=path,
                        ssh_user=ssh_user,
                    )
                )
                index += 1
        split_index_hosts = [str(item) for item in state.get("index_hosts", []) if str(item)]
        for host, gfid in (
            (split_index_hosts[0] if split_index_hosts else "", str(state.get("left_gfid_uuid") or "")),
            (split_index_hosts[0] if split_index_hosts else "", str(state.get("left_gfid_hex") or "")),
            (split_index_hosts[-1] if len(split_index_hosts) > 1 else (split_index_hosts[0] if split_index_hosts else ""), str(state.get("right_gfid_uuid") or "")),
            (split_index_hosts[-1] if len(split_index_hosts) > 1 else (split_index_hosts[0] if split_index_hosts else ""), str(state.get("right_gfid_hex") or "")),
        ):
            if host and gfid and index_root:
                result.steps.append(
                    _cleanup_step(
                        action_id=action_id,
                        index=index,
                        label="remove-split-index",
                        step_type="remove_stale_gfid",
                        host=host,
                        path=f"{index_root}/{gfid}",
                        ssh_user=ssh_user,
                    )
                )
                index += 1


    if kind in {"file-metadata-split-brain", "file-posix-metadata-split-brain"}:
        index_root = str(state.get("index_root") or "")
        index_host = str(state.get("index_host") or state.get("marker_host") or "")
        index_entry = str(state.get("index_entry") or "")
        file_gfid_path = str(state.get("file_gfid_path") or "")
        gfid_uuid = str(state.get("gfid_uuid") or "")
        index_path = index_entry or (f"{index_root}/{gfid_uuid}" if index_root and gfid_uuid else "")
        if index_root and index_host and index_path:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label=f"remove-{kind}-index",
                    step_type="remove_stale_gfid",
                    host=index_host,
                    path=index_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1
        if file_gfid_path and index_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label=f"remove-{kind}-gfid",
                    step_type="remove_stale_file_gfid",
                    host=index_host,
                    path=file_gfid_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1


    if kind in {"directory-metadata", "directory-mdata-no-majority"}:
        directory_gfid_path = str(state.get("directory_gfid_path") or "")
        marker_host = str(state.get("marker_host") or "")
        if directory_gfid_path and marker_host:
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-directory-gfid",
                    step_type="remove_stale_dir_gfid",
                    host=marker_host,
                    path=directory_gfid_path,
                    ssh_user=ssh_user,
                )
            )
            index += 1
    if kind in {"directory-gfid-merge", "directory-gfid-tie", "directory-gfid-mask", "type-mismatch"}:
        index_root = str(state.get("index_root") or "")
        index_root_by_host = state.get("index_root_by_host") or {}
        if not isinstance(index_root_by_host, dict):
            index_root_by_host = {}
        left_hosts = [str(item) for item in state.get("left_hosts", []) if str(item)]
        right_hosts = [str(item) for item in state.get("right_hosts", []) if str(item)]
        left_gfid_uuid = str(state.get("left_gfid_uuid") or "")
        right_gfid_uuid = str(state.get("right_gfid_uuid") or "")
        for host, gfid in (
            *((host, left_gfid_uuid) for host in left_hosts if host and left_gfid_uuid),
            *((host, right_gfid_uuid) for host in right_hosts if host and right_gfid_uuid),
        ):
            host_index_root = str(index_root_by_host.get(host) or index_root)
            if not host_index_root:
                continue
            result.steps.append(
                _cleanup_step(
                    action_id=action_id,
                    index=index,
                    label="remove-directory-index",
                    step_type="remove_stale_gfid",
                    host=host,
                    path=f"{host_index_root}/{gfid}",
                    ssh_user=ssh_user,
                )
            )
            index += 1

        directory_gfid_paths_by_host = state.get("directory_gfid_paths_by_host") or {}
        if isinstance(directory_gfid_paths_by_host, dict):
            for host, paths in sorted(directory_gfid_paths_by_host.items()):
                host_name = str(host)
                for path in paths or []:
                    if not host_name or not path:
                        continue
                    result.steps.append(
                        _cleanup_step(
                            action_id=action_id,
                            index=index,
                            label="remove-directory-gfid",
                            step_type="remove_stale_dir_gfid",
                            host=host_name,
                            path=str(path),
                            ssh_user=ssh_user,
                        )
                    )
                    index += 1

    if not result.steps:
        result.status = "review"
        result.notes.append("no cleanup steps were derived from canary state")
        result.steps.append(
            ApplyStep(
                step_id=f"{action_id}:{index:02d}:review-cleanup",
                step_type="review_stale_glusterfs_index_cleanup",
                notes=["no cleanup steps were derived from canary state; recheck before deleting"],
            )
        )
    return result


def create_entry_heal_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_root = _brick_hosts_and_root(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for entry-heal canary on {volume}")

    mount_root = _canary_temp_mount_root(volume)
    mount_target = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_target}/{file_name}"
    backend_target = f"{backend_root}/{scenario}/{dir_name}"
    index_root = f"{backend_root}/.glusterfs/indices/entry-changes"
    index_host = brick_hosts[0]
    peer_host = brick_hosts[2]

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_target)
    _local_mount_file(mount_file)

    index_request = _make_request(
        volume,
        scenario,
        backend_root,
        [
            {
                "op": "setxattr",
                "path": backend_target,
                "name": f"trusted.afr.{volume}-client-2",
                "value_hex": "000000010000000000000001",
            },
            {
                "op": "touch_entry_changes_index_from_parent",
                "parent_path": backend_target,
                "index_root": index_root,
                "child_name": file_name,
            },
        ],
    )
    peer_request = _make_request(
        volume,
        scenario,
        backend_root,
        [
            {
                "op": "setxattr",
                "path": backend_target,
                "name": f"trusted.afr.{volume}-client-0",
                "value_hex": "000000010000000000000001",
            }
        ],
    )

    index_response = _canary_worker_command(index_host, ssh_user=ssh_user, worker_path=worker_path, request=index_request)
    peer_response = _canary_worker_command(peer_host, ssh_user=ssh_user, worker_path=worker_path, request=peer_request)
    _print_response("entry-heal index worker", index_response)
    _print_response("entry-heal peer worker", peer_response)

    index_results = index_response.get("results", [])
    index_entry = next(
        (item for item in index_results if item.get("op") == "touch_entry_changes_index_from_parent" and item.get("ok")),
        {},
    )
    canonical_gfid = str(index_entry.get("parent_gfid_uuid") or "")
    compact_gfid = str(index_entry.get("parent_gfid_hex") or "")
    if not canonical_gfid:
        raise RuntimeError("entry-heal worker did not return a canonical parent GFID")

    _write_state(
        volume,
        scenario,
        {
            "kind": "entry-heal",
            "volume": volume,
            "scenario": scenario,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_target": mount_target,
            "mount_file": mount_file,
            "backend_root": backend_root,
            "backend_target": backend_target,
            "index_root": index_root,
            "index_host": index_host,
            "peer_host": peer_host,
            "parent_gfid_uuid": canonical_gfid,
            "parent_gfid_hex": compact_gfid,
            "entry_name": file_name,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest entry-heal canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_target}",
                f"  child file: {mount_file}",
                f"  backend target: {backend_target}",
                f"  parent GFID: {canonical_gfid}",
                f"  entry-change index: {index_root}/{canonical_gfid}/{file_name}",
                f"  index host: {index_host}",
                f"  peer host: {peer_host}",
                f"Next: sudo -n find {mount_target} -maxdepth 4 -exec stat -- {{}} +",
                f"Then: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def _create_file_split_brain_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    kind: str,
    display_label: str,
    same_gfid: bool = False,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, discovered_brick_roots = _brick_hosts_and_paths(volume)
    if len(brick_hosts) < 4:
        raise RuntimeError(f"need at least 4 bricks for {display_label} canary on {volume}")
    brick_roots = {
        normalize_host_alias(host): path
        for host, path in discovered_brick_roots.items()
    }

    backend_root = brick_roots[normalize_host_alias(brick_hosts[0])]
    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    mount_file = f"{mount_dir}/{file_name}"
    left_hosts = brick_hosts[:2]
    right_hosts = brick_hosts[2:4]
    left_gfid_hex = _new_gfid_hex()
    right_gfid_hex = left_gfid_hex if same_gfid else _new_gfid_hex()
    left_gfid = str(uuid.UUID(hex=left_gfid_hex))
    right_gfid = left_gfid if same_gfid else str(uuid.UUID(hex=right_gfid_hex))
    pending_value = "000000010000000000000001"

    _run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    _local_mount_dir(mount_dir)
    _local_mount_file(mount_file)

    for host in left_hosts + right_hosts:
        is_left = host in left_hosts
        cohort = "left" if is_left else "right"
        cohort_gfid_hex = left_gfid_hex if is_left else right_gfid_hex
        opposite_indices = range(2, 4) if is_left else range(0, 2)
        host_backend_root = brick_roots[normalize_host_alias(host)]
        host_backend_file = f"{host_backend_root}/{scenario}/{dir_name}/{file_name}"
        host_index_root = f"{host_backend_root}/.glusterfs/indices/xattrop"
        ops: list[dict[str, Any]] = [
            {
                "op": "write_text",
                "path": host_backend_file,
                "text": f"{scenario} {cohort} cohort\n",
            },
            {
                "op": "setxattr",
                "path": host_backend_file,
                "name": "trusted.gfid",
                "value_hex": cohort_gfid_hex,
            },
            {
                "op": "link_file_gfid_from_target",
                "target_path": host_backend_file,
            },
        ]
        for idx in opposite_indices:
            ops.append(
                {
                    "op": "setxattr",
                    "path": host_backend_file,
                    "name": f"trusted.afr.{volume}-client-{idx}",
                    "value_hex": pending_value,
                }
            )
        ops.append(
            {
                "op": "touch_index_from_target",
                "target_path": host_backend_file,
                "index_root": host_index_root,
            }
        )
        response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(volume, scenario, host_backend_root, ops),
        )
        _print_response(f"{display_label} worker {host}", response)
        _require_successful_worker_response(f"{display_label} worker {host}", response)

    backend_file = f"{backend_root}/{scenario}/{dir_name}/{file_name}"
    index_root = f"{backend_root}/.glusterfs/indices/xattrop"
    brick_roots_by_host = {
        host: brick_roots[normalize_host_alias(host)]
        for host in brick_hosts
    }
    file_gfid_paths_by_host = {
        host: _file_gfid_link_path(
            brick_roots_by_host[host],
            left_gfid if host in left_hosts else right_gfid,
        )
        for host in brick_hosts
    }
    index_roots_by_host = {
        host: f"{brick_roots_by_host[host]}/.glusterfs/indices/xattrop"
        for host in brick_hosts
    }

    _write_state(
        volume,
        scenario,
        {
            "kind": kind,
            "leave_heal_pending": True,
            "volume": volume,
            "scenario": scenario,
            "dir_name": dir_name,
            "file_name": file_name,
            "mount_root": mount_root,
            "mount_dir": mount_dir,
            "mount_file": mount_file,
            "backend_root": backend_root,
            "backend_target": backend_file,
            "index_root": index_root,
            "brick_roots_by_host": brick_roots_by_host,
            "index_roots_by_host": index_roots_by_host,
            "file_gfid_paths_by_host": file_gfid_paths_by_host,
            "index_hosts": left_hosts + right_hosts,
            "left_hosts": left_hosts,
            "right_hosts": right_hosts,
            "left_gfid_uuid": left_gfid,
            "left_gfid_hex": left_gfid_hex,
            "left_file_gfid_path": file_gfid_paths_by_host[left_hosts[0]],
            "right_gfid_uuid": right_gfid,
            "right_gfid_hex": right_gfid_hex,
            "right_file_gfid_path": file_gfid_paths_by_host[right_hosts[0]],
            "same_gfid": same_gfid,
            "gfid_uuid": left_gfid if same_gfid else "",
            "gfid_hex": left_gfid_hex if same_gfid else "",
            "file_gfid_path": file_gfid_paths_by_host[left_hosts[0]] if same_gfid else "",
        },
    )

    print(
        "\n".join(
            [
                f"Created gtest {display_label} canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_file}",
                f"  backend target: {backend_file}",
                f"  left cohort: {', '.join(left_hosts)} -> {left_gfid}",
                f"  right cohort: {', '.join(right_hosts)} -> {right_gfid}",
                f"  index bucket: {index_root}",
                (
                    "Expected: same GFID with divergent cohort content should stay in split-brain review; "
                    "preserve both copies before choosing a source."
                    if same_gfid
                    else "Expected: Gluster should treat this as an ambiguous entry/name-GFID conflict, not a simple auto-heal."
                ),
                f"Next: sudo -n gluster volume heal {volume} info",
                f"Then build a manifest/plan for /{scenario}/{dir_name}/{file_name} and expect review_entry_split_brain.",
            ]
        )
    )


def create_file_gfid_split_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_file_split_brain_canary(
        volume=volume,
        scenario=scenario,
        dir_name=dir_name,
        file_name=file_name,
        kind="file-gfid-split",
        display_label="file-GFID split",
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_file_content_split_brain_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_file_split_brain_canary(
        volume=volume,
        scenario=scenario,
        dir_name=dir_name,
        file_name=file_name,
        kind="file-content-split-brain",
        display_label="file content split",
        same_gfid=True,
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
    kwargs = {
        "volume": volume,
        "scenario": scenario,
        "dir_name": dir_name,
        "file_name": file_name,
        "ssh_user": ssh_user,
        "worker_path": worker_path,
    }
    if arbiter_host is not None:
        kwargs["arbiter_host"] = arbiter_host
    if not trigger_heal:
        kwargs["trigger_heal"] = False
    _create_file_data_split_brain_canary_impl(**kwargs)


def create_ctime_review_chain_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    directory_scenario = f"{scenario}-directory"
    file_scenario = f"{scenario}-file"
    create_directory_metadata_canary(
        volume=volume,
        scenario=directory_scenario,
        mismatch_host=mismatch_host,
        dir_name=dir_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )
    create_file_ctime_metadata_canary(
        volume=volume,
        scenario=file_scenario,
        mismatch_host=mismatch_host,
        dir_name=dir_name,
        file_name=file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )
    print(
        "\n".join(
            [
                "Created gtest ctime review chain:",
                f"  volume: {volume}",
                f"  base scenario: {scenario}",
                f"  directory scenario: {directory_scenario}",
                f"  file scenario: {file_scenario}",
                f"  dir: {dir_name}",
                f"  file: {file_name}",
                "Expected: exercise the directory ctime review branch first, then the file-backed ctime follow-up.",
                "Next: cleanup-ctime-review-chain --volume <vol> --name <base-scenario>",
            ]
        )
    )


def cleanup_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, brick_roots = _brick_hosts_and_paths(volume)
    backend_root = next(iter(brick_roots.values()))
    mount_root = _volume_mount_root(volume)
    state = _read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind in {
        "entry-heal",
        "file-gfid-split",
        "file-content-split-brain",
        "file-data-split-brain",
        "file-metadata-split-brain",
        "file-posix-metadata-split-brain",
        "file-posix-metadata",
        "symlink-missing-stale",
        "missing-file-replica",
        "file-child-gap-multi",
        "directory-backend-child-gap-mixed",
        "orphaned-gfid-hardlink",
        "directory-metadata",
        "directory-mdata-no-majority",
        "file-ctime-metadata",
        "file-handle-ghost",
    }:
        dir_name = str(state.get("dir_name") or dir_name)
        file_name = str(state.get("file_name") or file_name)
        if kind == "directory-backend-child-gap-mixed":
            file_children = state.get("file_children") or []
            if isinstance(file_children, list) and file_children:
                first_file = file_children[0] if isinstance(file_children[0], dict) else {}
                file_name = str(first_file.get("file_name") or file_name)
    mount_scenario_root = f"{mount_root}/{scenario}"
    backend_scenario_root = str(state.get("backend_root") or backend_root)
    temp_mount_root = _canary_temp_mount_root(volume)
    temp_mount_active = _mountpoint_is_active(temp_mount_root)
    cleanup_mount_root = str(state.get("mount_root") or mount_root)
    if temp_mount_active or cleanup_mount_root == temp_mount_root:
        cleanup_mount_root = temp_mount_root

    cleanup_result = _build_cleanup_result(
        volume=volume,
        scenario=scenario,
        kind=kind,
        mount_root=cleanup_mount_root,
        backend_root=backend_root,
        brick_roots=brick_roots,
        brick_hosts=brick_hosts,
        ssh_user=ssh_user,
        state=state,
    )
    valid, errors = validate_execute_results([cleanup_result])
    if not valid:
        raise RuntimeError("; ".join(errors))
    report = execute_apply_results([cleanup_result], keep_going=True)
    summary = report.get("summary", {})
    print(
        "\n".join(
            [
                "cleanup executed through repair engine:",
                f"  status: {cleanup_result.status}",
                f"  steps: {len(cleanup_result.steps)}",
                f"  completed: {summary.get('completed_actions', 0)}",
                f"  failed: {summary.get('failed_actions', 0)}",
                f"  skipped: {summary.get('completed_with_skips', 0)}",
            ]
        )
    )
    if cleanup_result.status == "failed":
        raise RuntimeError("canary cleanup failed; state was left in place for a retry")
    if temp_mount_active or cleanup_mount_root == temp_mount_root:
        cleanup_mount_root = temp_mount_root
        _unmount_canary_mount(temp_mount_root)
        _local_remove(temp_mount_root)
    elif kind == "missing-file-replica":
        _unmount_canary_mount(cleanup_mount_root)
        _local_remove(cleanup_mount_root)
    else:
        _local_remove(mount_scenario_root)
    if state.get("leave_heal_pending") or state.get("restore_heal_settings"):
        set_heal_settings(volume, True)
    _delete_state(volume, scenario)

    print(
        "\n".join(
            [
                "Removed gtest canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  dir name: {dir_name}",
                f"  file name: {file_name}",
                f"  mount root: {cleanup_mount_root}",
                f"  backend root: {backend_scenario_root}",
            ]
        )
    )


def cleanup_canary_wave(
    *,
    volume: str,
    scenarios: list[str],
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    parallel_actions: int = 2,
    parallel_nice: int = 5,
    run_dir: str | Path | None = None,
) -> None:
    if not scenarios:
        raise RuntimeError("need at least one canary scenario")
    if len(set(scenarios)) != len(scenarios):
        raise RuntimeError("cleanup wave scenarios must be unique")

    brick_hosts, brick_roots = _brick_hosts_and_paths(volume)
    backend_root = next(iter(brick_roots.values()))
    mount_root = _volume_mount_root(volume)
    temp_mount_root = _canary_temp_mount_root(volume)
    temp_mount_active = _mountpoint_is_active(temp_mount_root)

    cleanup_results: list[ApplyActionResult] = []
    cleanup_entries: list[dict[str, str]] = []

    for scenario in scenarios:
        state = _read_state(volume, scenario)
        if not state:
            raise RuntimeError(f"missing canary state for {volume}/{scenario}")

        kind = str(state.get("kind") or "")
        scenario_dir_name = str(state.get("dir_name") or dir_name)
        scenario_file_name = str(state.get("file_name") or file_name)
        if kind in {
            "entry-heal",
            "file-gfid-split",
            "file-content-split-brain",
            "file-data-split-brain",
            "file-metadata-split-brain",
            "file-posix-metadata-split-brain",
            "file-posix-metadata",
            "symlink-missing-stale",
            "missing-file-replica",
            "file-child-gap-multi",
            "directory-backend-child-gap-mixed",
            "orphaned-gfid-hardlink",
            "directory-metadata",
            "directory-mdata-no-majority",
            "file-ctime-metadata",
            "file-handle-ghost",
        }:
            scenario_dir_name = str(state.get("dir_name") or scenario_dir_name)
            scenario_file_name = str(state.get("file_name") or scenario_file_name)
            if kind == "directory-backend-child-gap-mixed":
                file_children = state.get("file_children") or []
                if isinstance(file_children, list) and file_children:
                    first_file = file_children[0] if isinstance(file_children[0], dict) else {}
                    scenario_file_name = str(first_file.get("file_name") or scenario_file_name)

        mount_scenario_root = f"{mount_root}/{scenario}"
        backend_scenario_root = str(state.get("backend_root") or backend_root)
        cleanup_mount_root = str(state.get("mount_root") or mount_root)
        if temp_mount_active or cleanup_mount_root == temp_mount_root:
            cleanup_mount_root = temp_mount_root

        cleanup_result = _build_cleanup_result(
            volume=volume,
            scenario=scenario,
            kind=kind,
            mount_root=cleanup_mount_root,
            backend_root=backend_root,
            brick_roots=brick_roots,
            brick_hosts=brick_hosts,
            ssh_user=ssh_user,
            state=state,
        )
        if cleanup_result.action_type not in {"cleanup_orphaned_symlink", "cleanup_stale_glusterfs_index"}:
            raise RuntimeError(
                f"cleanup wave only supports orphaned symlink or stale index cleanup actions; {scenario} is {cleanup_result.action_type}"
            )
        cleanup_result.parallel_safe = True
        cleanup_result.execution_wave = 0
        cleanup_result.execution_resources = [f"logical:{cleanup_result.logical_path}"]

        cleanup_results.append(cleanup_result)
        cleanup_entries.append(
            {
                "scenario": scenario,
                "kind": kind,
                "dir_name": scenario_dir_name,
                "file_name": scenario_file_name,
                "mount_scenario_root": mount_scenario_root,
                "backend_scenario_root": backend_scenario_root,
                "cleanup_mount_root": cleanup_mount_root,
            }
        )

    valid, errors = validate_execute_results(cleanup_results)
    if not valid:
        raise RuntimeError("; ".join(errors))

    run_path = Path(run_dir).expanduser() if run_dir else default_repair_run_dir(volume, f"canary-cleanup-wave-{uuid.uuid4().hex[:8]}")
    run_path.mkdir(parents=True, exist_ok=True)

    report = execute_apply_results(
        cleanup_results,
        keep_going=True,
        parallel_actions=max(1, parallel_actions),
        parallel_nice=parallel_nice,
        run_dir=run_path,
    )
    summary = report.get("summary", {})
    scenario_statuses = ["{}={}".format(entry["scenario"], result.status) for entry, result in zip(cleanup_entries, cleanup_results)]
    scenario_list = ", ".join(scenarios)
    status_list = ", ".join(scenario_statuses)
    print(
        "\n".join(
            [
                "cleanup wave executed through repair engine:",
                f"  volume: {volume}",
                f"  scenarios: {scenario_list}",
                f"  statuses: {status_list}",
                f"  completed: {summary.get('completed_actions', 0)}",
                f"  failed: {summary.get('failed_actions', 0)}",
                f"  skipped: {summary.get('completed_with_skips', 0)}",
                f"  waves: {summary.get('execution_waves', 0)}",
                f"  parallel requested: {summary.get('parallel_actions_requested', 0)}",
                f"  parallel nice: {summary.get('parallel_nice', 0)}",
                f"  run dir: {report.get('run_dir', '')}",
            ]
        )
    )
    if any(result.status == "failed" for result in cleanup_results):
        raise RuntimeError("one or more canary cleanup actions failed; state was left in place for a retry")

    if temp_mount_active or any(entry["cleanup_mount_root"] == temp_mount_root for entry in cleanup_entries):
        _unmount_canary_mount(temp_mount_root)
        _local_remove(temp_mount_root)

    for entry, result in zip(cleanup_entries, cleanup_results):
        if result.status == "failed":
            continue
        if entry["cleanup_mount_root"] != temp_mount_root and entry["kind"] == "missing-file-replica":
            _unmount_canary_mount(entry["cleanup_mount_root"])
            _local_remove(entry["cleanup_mount_root"])
        elif entry["cleanup_mount_root"] != temp_mount_root:
            _local_remove(entry["mount_scenario_root"])
        _delete_state(volume, entry["scenario"])

    print(
        "\n".join(
            [
                "Removed gtest canary wave:",
                f"  volume: {volume}",
                f"  scenarios: {scenario_list}",
                f"  run dir: {report.get('run_dir', '')}",
            ]
        )
    )


def cleanup_ctime_review_chain_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    cleanup_canary(
        volume=volume,
        scenario=f"{scenario}-directory",
        dir_name=dir_name,
        file_name=file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )
    cleanup_canary(
        volume=volume,
        scenario=f"{scenario}-file",
        dir_name=dir_name,
        file_name=file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gluster-gtest-canary")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="create the default entry-heal canary")
    create.add_argument("--volume", default="gtest4")
    create.add_argument("--name")
    create.add_argument("--dir", default="alpha")
    create.add_argument("--file", default="payload.txt")

    create_file = sub.add_parser("create-file-metadata", help="create a file-metadata canary")
    create_file.add_argument("--volume", default="gtest4")
    create_file.add_argument("--name")
    create_file.add_argument("--mismatch-host")
    create_file.add_argument("--dir", default="alpha")
    create_file.add_argument("--file", default="payload.txt")

    create_file_posix = sub.add_parser(
        "create-file-posix-metadata",
        help="create a file POSIX metadata canary",
    )
    create_file_posix.add_argument("--volume", default="gtest4")
    create_file_posix.add_argument("--name")
    create_file_posix.add_argument("--mismatch-host")
    create_file_posix.add_argument("--dir", default="alpha")
    create_file_posix.add_argument("--file", default="payload.txt")
    create_file_posix.add_argument(
        "--fields",
        choices=["mode", "owner", "acl", "combined", "no_majority_mode"],
        default="combined",
        help="POSIX metadata fields to drift on the mismatch brick",
    )
    create_file_posix.add_argument(
        "--keep-mismatch-online",
        action="store_true",
        help="leave the mismatch brick online; useful only for diagnostics because Gluster may normalize the drift",
    )
    create_file_posix.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="remote worker/host-ops SSH user (default: installed service account)",
    )

    create_file_posix_split_brain = sub.add_parser(
        "create-file-posix-metadata-split-brain",
        help="create an afr-synthesized file POSIX metadata split-brain canary",
    )
    create_file_posix_split_brain.add_argument("--volume", default="gtest4")
    create_file_posix_split_brain.add_argument("--name")
    create_file_posix_split_brain.add_argument(
        "--mismatch-host",
        "--source-host",
        dest="mismatch_host",
        help="preferred source tuple host for the source-choice fixture (legacy --mismatch-host alias)",
    )
    create_file_posix_split_brain.add_argument("--dir", default="alpha")
    create_file_posix_split_brain.add_argument("--file", default="payload.txt")
    create_file_posix_split_brain.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="retain generated pending/index state for independent evidence; do not launch a heal crawl",
    )
    create_file_posix_split_brain.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="remote worker/host-ops SSH user (default: installed service account)",
    )

    file_posix_split_brain_build = sub.add_parser(
        "file-posix-metadata-split-brain-build",
        help="build a plan from a live file POSIX metadata split-brain canary state",
    )
    file_posix_split_brain_build.add_argument("--volume", default="gtest4")
    file_posix_split_brain_build.add_argument("--name")
    file_posix_split_brain_build.add_argument("--plan-out", required=True)

    create_file_posix_acl_majority = sub.add_parser(
        "create-file-posix-acl-majority",
        help="create a file POSIX access ACL majority canary",
    )
    create_file_posix_acl_majority.add_argument("--volume", default="gtest4")
    create_file_posix_acl_majority.add_argument("--name")
    create_file_posix_acl_majority.add_argument("--mismatch-host")
    create_file_posix_acl_majority.add_argument("--dir", default="alpha")
    create_file_posix_acl_majority.add_argument("--file", default="payload.txt")
    create_file_posix_acl_majority.add_argument(
        "--keep-mismatch-online",
        action="store_true",
        help="leave the mismatch brick online; useful only for diagnostics because Gluster may normalize the drift",
    )
    create_file_posix_acl_majority.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="remote worker/host-ops SSH user (default: installed service account)",
    )

    create_file_native_pending = sub.add_parser(
        "create-file-native-pending-metadata",
        help="create a file native-pending metadata canary",
    )
    create_file_native_pending.add_argument("--volume", default="gtest4")
    create_file_native_pending.add_argument("--name")
    create_file_native_pending.add_argument("--mismatch-host")
    create_file_native_pending.add_argument("--dir", default="alpha")
    create_file_native_pending.add_argument("--file", default="payload.txt")
    create_file_native_pending.add_argument(
        "--ssh-user",
        default=DEFAULT_SERVICE_USER,
        help="remote worker/host-ops SSH user (default: installed service account)",
    )

    create_dir_posix = sub.add_parser(
        "create-directory-posix-metadata",
        help="create a directory POSIX mode majority canary",
    )
    create_dir_posix.add_argument("--volume", default="gtest3")
    create_dir_posix.add_argument("--name")
    create_dir_posix.add_argument("--mismatch-host")
    create_dir_posix.add_argument("--dir", default="alpha")
    create_dir_posix.add_argument("--child", default="metadata-seed.txt")
    create_dir_posix.add_argument(
        "--fields", choices=["mode", "default_acl"], default="mode",
        help="directory POSIX metadata field to drift on the mismatch brick",
    )
    create_dir_posix.add_argument("--ssh-user", default=DEFAULT_SERVICE_USER)

    create_dir_posix_default_acl_majority = sub.add_parser(
        "create-directory-posix-default-acl-majority",
        help="create a directory POSIX default ACL majority canary",
    )
    create_dir_posix_default_acl_majority.add_argument("--volume", default="gtest3")
    create_dir_posix_default_acl_majority.add_argument("--name")
    create_dir_posix_default_acl_majority.add_argument("--mismatch-host")
    create_dir_posix_default_acl_majority.add_argument("--dir", default="alpha")
    create_dir_posix_default_acl_majority.add_argument("--child", default="metadata-seed.txt")
    create_dir_posix_default_acl_majority.add_argument("--ssh-user", default=DEFAULT_SERVICE_USER)

    create_file_split = sub.add_parser(
        "create-file-metadata-split-brain",
        help="create a file metadata split-brain canary",
    )
    create_file_split.add_argument("--volume", default="gtest4")
    create_file_split.add_argument("--name")
    create_file_split.add_argument("--mismatch-host")
    create_file_split.add_argument("--dir", default="alpha")
    create_file_split.add_argument("--file", default="payload.txt")
    create_file_split.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="leave all heal controls disabled and skip the full crawl; use independent operator evidence before classifying the fixture",
    )

    create_afr_metadata_split_brain = sub.add_parser(
        "create-afr-metadata-split-brain",
        help="create the AFR-synthesized file metadata split-brain canary",
    )
    create_afr_metadata_split_brain.add_argument("--volume", default="gtest4")
    create_afr_metadata_split_brain.add_argument("--name")
    create_afr_metadata_split_brain.add_argument("--mismatch-host")
    create_afr_metadata_split_brain.add_argument("--dir", default="alpha")
    create_afr_metadata_split_brain.add_argument("--file", default="payload.txt")
    create_afr_metadata_split_brain.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="leave all heal controls disabled and skip the full crawl; use independent operator evidence before classifying the fixture",
    )

    file_metadata_split_plan = sub.add_parser(
        "file-metadata-split-brain-build",
        help="build a plan from a live file-metadata-split-brain canary state",
    )
    file_metadata_split_plan.add_argument("--volume", default="gtest4")
    file_metadata_split_plan.add_argument("--name")
    file_metadata_split_plan.add_argument("--plan-out", required=True)

    create_file_ctime = sub.add_parser(
        "create-file-ctime-metadata",
        help="create a file-backed ctime review case",
    )
    create_file_ctime.add_argument("--volume", default="gtest4")
    create_file_ctime.add_argument("--name")
    create_file_ctime.add_argument("--mismatch-host")
    create_file_ctime.add_argument("--dir", default="alpha")
    create_file_ctime.add_argument("--file", default="payload.txt")

    create_ctime_chain = sub.add_parser(
        "create-ctime-review-chain",
        help="create paired directory and file ctime review canaries",
    )
    create_ctime_chain.add_argument("--volume", default="gtest4")
    create_ctime_chain.add_argument("--name")
    create_ctime_chain.add_argument("--mismatch-host")
    create_ctime_chain.add_argument("--dir", default="alpha")
    create_ctime_chain.add_argument("--file", default="payload.txt")

    create_missing_file = sub.add_parser("create-missing-file-replica", help="create a missing file replica canary")
    create_missing_file.add_argument("--volume", default="gtest4")
    create_missing_file.add_argument("--name")
    create_missing_file.add_argument("--missing-host")
    create_missing_file.add_argument("--dir", default="alpha")
    create_missing_file.add_argument("--file", default="payload.txt")
    create_missing_file.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="leave heal settings disabled and skip the crawl; use only with fixtures that create their own pending/index evidence",
    )

    create_arbiter_missing_gfid = sub.add_parser(
        "create-arbiter-missing-gfid",
        help="create a 2+1 arbiter canary with only its trusted.gfid and handle removed",
    )
    create_arbiter_missing_gfid.add_argument("--volume", default="gtest3a")
    create_arbiter_missing_gfid.add_argument("--name")
    create_arbiter_missing_gfid.add_argument("--object-type", choices=["file", "directory"], default="file")
    create_arbiter_missing_gfid.add_argument("--arbiter-host")
    create_arbiter_missing_gfid.add_argument("--dir", default="alpha")
    create_arbiter_missing_gfid.add_argument("--file", default="payload.txt")

    create_arbiter_only_residue = sub.add_parser(
        "create-arbiter-only-residue",
        help="create a 2+1 arbiter canary with both data copies removed",
    )
    create_arbiter_only_residue.add_argument("--volume", default="gtest3a")
    create_arbiter_only_residue.add_argument("--name")
    create_arbiter_only_residue.add_argument("--object-type", choices=["file", "directory"], default="file")
    create_arbiter_only_residue.add_argument("--arbiter-host")
    create_arbiter_only_residue.add_argument("--dir", default="alpha")
    create_arbiter_only_residue.add_argument("--file", default="payload.txt")

    create_missing_file_pair = sub.add_parser(
        "create-missing-file-replica-pair",
        help="create a missing file replica canary with two missing bricks",
    )
    create_missing_file_pair.add_argument("--volume", default="gtest4")
    create_missing_file_pair.add_argument("--name")
    create_missing_file_pair.add_argument("--dir", default="alpha")
    create_missing_file_pair.add_argument("--file", default="payload.txt")

    create_file_gap = sub.add_parser(
        "create-file-child-gap",
        help="create a file-child-gap canary for backend restore development",
    )
    create_file_gap.add_argument("--volume", default="gtest4")
    create_file_gap.add_argument("--name")
    create_file_gap.add_argument("--missing-host")
    create_file_gap.add_argument("--dir", default="alpha")
    create_file_gap.add_argument("--file", default="payload.txt")
    create_file_gap.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="leave heal settings disabled and skip the crawl; cleanup restores healing after independent evidence capture",
    )

    create_file_gap_multi = sub.add_parser(
        "create-file-child-gap-multi",
        help="create a multi-file child-gap canary for backend restore development",
    )
    create_file_gap_multi.add_argument("--volume", default="gtest4")
    create_file_gap_multi.add_argument("--name")
    create_file_gap_multi.add_argument("--missing-host")
    create_file_gap_multi.add_argument("--dir", default="alpha")
    create_file_gap_multi.add_argument(
        "--file",
        dest="files",
        action="append",
        required=True,
        help="repeat to name each missing child file under the same parent directory",
    )

    file_gap_plan = sub.add_parser(
        "file-child-gap-build",
        help="build a plan from a live file-child-gap canary state",
    )
    file_gap_plan.add_argument("--volume", default="gtest4")
    file_gap_plan.add_argument("--name")
    file_gap_plan.add_argument("--plan-out", required=True)

    file_gap_multi_plan = sub.add_parser(
        "file-child-gap-multi-build",
        help="build a plan from a live multi-file child-gap canary state",
    )
    file_gap_multi_plan.add_argument("--volume", default="gtest4")
    file_gap_multi_plan.add_argument("--name")
    file_gap_multi_plan.add_argument("--plan-out", required=True)

    create_stale_survivor = sub.add_parser(
        "create-file-stale-survivor",
        help="create a below-quorum file stale-survivor canary",
    )
    create_stale_survivor.add_argument("--volume", default="gtest4")
    create_stale_survivor.add_argument("--name")
    create_stale_survivor.add_argument("--survivor-host")
    create_stale_survivor.add_argument("--dir", default="alpha")
    create_stale_survivor.add_argument("--file", default="payload.txt")

    create_stale_survivor_pair = sub.add_parser(
        "create-file-stale-survivor-pair",
        help="create a below-quorum file stale-survivor canary with two surviving bricks",
    )
    create_stale_survivor_pair.add_argument("--volume", default="gtest4")
    create_stale_survivor_pair.add_argument("--name")
    create_stale_survivor_pair.add_argument("--dir", default="alpha")
    create_stale_survivor_pair.add_argument("--file", default="payload.txt")

    create_orphaned_gfid = sub.add_parser(
        "create-orphaned-gfid-hardlink",
        help="create a bare orphaned .glusterfs hardlink canary",
    )
    create_orphaned_gfid.add_argument("--volume", default="gtest4")
    create_orphaned_gfid.add_argument("--name")
    create_orphaned_gfid.add_argument("--orphan-host")
    create_orphaned_gfid.add_argument("--dir", default="alpha")
    create_orphaned_gfid.add_argument("--file", default="payload.txt")
    create_orphaned_gfid.add_argument("--leave-heal-pending", action="store_true")

    create_handle_ghost = sub.add_parser(
        "create-file-handle-ghost",
        help="create a regular-file gfid2path handle ghost canary",
    )
    create_handle_ghost.add_argument("--volume", default="gtest4")
    create_handle_ghost.add_argument("--name")
    create_handle_ghost.add_argument("--ghost-host")
    create_handle_ghost.add_argument("--dir", default="alpha")
    create_handle_ghost.add_argument("--file", default="payload.txt")
    create_handle_ghost.add_argument("--leave-heal-pending", action="store_true")

    create_gap = sub.add_parser("create-child-gap", help="create a child-gap canary")
    create_gap.add_argument("--volume", default="gtest4")
    create_gap.add_argument("--name")
    create_gap.add_argument("--missing-host")
    create_gap.add_argument("--parent", default="alpha")
    create_gap.add_argument("--present-child", default="beta")
    create_gap.add_argument("--missing-child", default="gamma")

    create_dir_gap = sub.add_parser(
        "create-directory-child-gap",
        help="create the shallow directory child-gap canary",
    )
    create_dir_gap.add_argument("--volume", default="gtest4")
    create_dir_gap.add_argument("--name")
    create_dir_gap.add_argument("--missing-host")
    create_dir_gap.add_argument("--parent", default="alpha")
    create_dir_gap.add_argument("--present-child", default="beta")
    create_dir_gap.add_argument("--missing-child", default="gamma")
    create_dir_gap.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="disable healing before fixture setup; cleanup restores it after independent evidence capture",
    )

    create_dir_backend_gap = sub.add_parser(
        "create-directory-backend-child-gap",
        help="create the backend-child-gap canary used to develop directory child restore",
    )
    create_dir_backend_gap.add_argument("--volume", default="gtest4")
    create_dir_backend_gap.add_argument("--name")
    create_dir_backend_gap.add_argument("--missing-host")
    create_dir_backend_gap.add_argument("--parent", default="alpha")
    create_dir_backend_gap.add_argument("--present-child", default="beta")
    create_dir_backend_gap.add_argument("--missing-child", default="gamma")
    create_dir_backend_gap.add_argument(
        "--tree-depth",
        type=int,
        default=1,
        help="when greater than 1, build a deeper mixed tree with one file in each directory",
    )
    create_dir_backend_gap.add_argument(
        "--gap-level",
        type=int,
        default=1,
        help="which nested directory level to remove on the missing brick",
    )
    create_dir_backend_gap.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="disable healing before fixture setup; cleanup restores it after independent evidence capture",
    )

    create_dir_backend_gap_mixed = sub.add_parser(
        "create-directory-backend-child-gap-mixed",
        help="create the mixed file-and-directory backend child-gap canary",
    )
    create_dir_backend_gap_mixed.add_argument("--volume", default="gtest4")
    create_dir_backend_gap_mixed.add_argument("--name")
    create_dir_backend_gap_mixed.add_argument("--missing-host")
    create_dir_backend_gap_mixed.add_argument("--tree-depth", type=int, default=3)
    create_dir_backend_gap_mixed.add_argument("--gap-level", type=int, default=1)
    create_dir_backend_gap_mixed.add_argument(
        "--file",
        dest="files",
        action="append",
        help="repeat to add root-level file children that should also be repaired one at a time",
    )

    create_dir_backend_gap_bounded = sub.add_parser(
        "create-directory-backend-child-gap-bounded",
        help="create the bounded-subtree directory backend child-gap canary",
    )
    create_dir_backend_gap_bounded.add_argument("--volume", default="gtest4")
    create_dir_backend_gap_bounded.add_argument("--name")
    create_dir_backend_gap_bounded.add_argument("--missing-host")
    create_dir_backend_gap_bounded.add_argument("--parent", default="alpha")
    create_dir_backend_gap_bounded.add_argument("--present-child", default="beta")
    create_dir_backend_gap_bounded.add_argument("--missing-child", default="gamma")

    create_dir_backend_gap_canonical = sub.add_parser(
        "create-directory-backend-child-gap-canonical",
        help="create the canonical-content-recreate backend child-gap canary",
    )
    create_dir_backend_gap_canonical.add_argument("--volume", default="gtest4")
    create_dir_backend_gap_canonical.add_argument("--name")
    create_dir_backend_gap_canonical.add_argument("--missing-host")
    create_dir_backend_gap_canonical.add_argument("--parent", default="alpha")
    create_dir_backend_gap_canonical.add_argument("--present-child", default="beta")
    create_dir_backend_gap_canonical.add_argument("--missing-child", default="gamma")

    create_child_ref = sub.add_parser(
        "create-directory-child-reference",
        help="create a directory-child-reference canary",
    )
    create_child_ref.add_argument("--volume", default="gtest4")
    create_child_ref.add_argument("--name")
    create_child_ref.add_argument("--missing-host")
    create_child_ref.add_argument("--parent", default="alpha")
    create_child_ref.add_argument("--present-child", default="beta")
    create_child_ref.add_argument("--missing-child", default="payload.txt")

    observe_child = sub.add_parser(
        "observe-directory-child-state",
        help="record the current client-side metadata snapshot for a child-gap canary",
    )
    observe_child.add_argument("--volume", default="gtest4")
    observe_child.add_argument("--name")

    create_dir_merge = sub.add_parser(
        "create-directory-gfid-merge",
        help="create a same-child-set directory GFID merge canary",
    )
    create_dir_merge.add_argument("--volume", default="gtest4")
    create_dir_merge.add_argument("--name")
    create_dir_merge.add_argument("--dir", default="alpha")
    create_dir_merge.add_argument("--file", default="payload.txt")

    create_dir_tie = sub.add_parser(
        "create-directory-gfid-tie",
        help="create a bare directory GFID tie canary",
    )
    create_dir_tie.add_argument("--volume", default="gtest4")
    create_dir_tie.add_argument("--name")
    create_dir_tie.add_argument("--dir", default="alpha")

    create_dir_mask = sub.add_parser(
        "create-directory-gfid-mask",
        help="create a same-path/two-GFID masked-tree directory canary",
    )
    create_dir_mask.add_argument("--volume", default="gtest4")
    create_dir_mask.add_argument("--name")
    create_dir_mask.add_argument("--dir", default="alpha")
    create_dir_mask.add_argument("--file", default="payload.txt")
    create_dir_mask.add_argument("--shadow-file", default="shadow.txt")

    create_type_mismatch = sub.add_parser(
        "create-type-mismatch",
        help="create a file-vs-directory type mismatch canary",
    )
    create_type_mismatch.add_argument("--volume", default="gtest4")
    create_type_mismatch.add_argument("--name")
    create_type_mismatch.add_argument("--dir", default="alpha")
    create_type_mismatch.add_argument("--file", default="payload.txt")
    create_type_mismatch.add_argument("--shadow-file", default="shadow.txt")

    type_mismatch_build = sub.add_parser(
        "type-mismatch-build",
        help="build a plan from a live type-mismatch canary state",
    )
    type_mismatch_build.add_argument("--volume", default="gtest4")
    type_mismatch_build.add_argument("--name")
    type_mismatch_build.add_argument("--plan-out", required=True)

    observe_dir = sub.add_parser(
        "observe-directory-gfid-state",
        help="record the current client-side metadata snapshot for a directory GFID canary",
    )
    observe_dir.add_argument("--volume", default="gtest4")
    observe_dir.add_argument("--name")

    observe_file = sub.add_parser(
        "observe-file-state",
        help="record mount and brick-side evidence for a file-family canary",
    )
    observe_file.add_argument("--volume", default="gtest4")
    observe_file.add_argument("--name")
    observe_file.add_argument("--heal-root")

    create_dir_metadata = sub.add_parser("create-directory-metadata", help="create a directory ctime review case")
    create_dir_metadata.add_argument("--volume", default="gtest4")
    create_dir_metadata.add_argument("--name")
    create_dir_metadata.add_argument("--mismatch-host")
    create_dir_metadata.add_argument("--dir", default="alpha")

    create_dir_mdata_no_majority = sub.add_parser(
        "create-directory-mdata-no-majority",
        help="create a directory mdata disagreement with no majority source",
    )
    create_dir_mdata_no_majority.add_argument("--volume", default="gtest4")
    create_dir_mdata_no_majority.add_argument("--name")
    create_dir_mdata_no_majority.add_argument("--dir", default="alpha")

    directory_mdata_no_majority_build = sub.add_parser(
        "directory-mdata-no-majority-build",
        help="build a plan from a live directory-mdata-no-majority canary state",
    )
    directory_mdata_no_majority_build.add_argument("--volume", default="gtest4")
    directory_mdata_no_majority_build.add_argument("--name")
    directory_mdata_no_majority_build.add_argument("--plan-out", required=True)

    create_zero_afr_native_heal_smoke = sub.add_parser(
        "create-zero-afr-native-heal-smoke",
        help="create the zero-AFR native-heal-smoke metadata canary",
    )
    create_zero_afr_native_heal_smoke.add_argument("--volume", default="gtest4")
    create_zero_afr_native_heal_smoke.add_argument("--name")
    create_zero_afr_native_heal_smoke.add_argument("--dir", default="alpha")

    create_dir_stale_survivor = sub.add_parser(
        "create-directory-stale-survivor",
        help="create a directory stale-survivor canary",
    )
    create_dir_stale_survivor.add_argument("--volume", default="gtest4")
    create_dir_stale_survivor.add_argument("--name")
    create_dir_stale_survivor.add_argument("--survivor-host")
    create_dir_stale_survivor.add_argument("--dir", default="alpha")

    create_split = sub.add_parser("create-file-gfid-split", help="create a same-name/two-GFID file split canary")
    create_split.add_argument("--volume", default="gtest4")
    create_split.add_argument("--name")
    create_split.add_argument("--dir", default="alpha")
    create_split.add_argument("--file", default="payload.txt")

    create_content_split = sub.add_parser(
        "create-file-content-split-brain",
        help="create a pure-replica same-GFID/different-content split-brain canary",
    )
    create_content_split.add_argument("--volume", default="gtest4")
    create_content_split.add_argument("--name")
    create_content_split.add_argument("--dir", default="alpha")
    create_content_split.add_argument("--file", default="payload.txt")

    create_data_split = sub.add_parser(
        "create-file-data-split-brain",
        help="create an arbiter-aware file data split-brain canary",
    )
    create_data_split.add_argument("--volume", default="gtest4")
    create_data_split.add_argument("--name")
    create_data_split.add_argument("--dir", default="alpha")
    create_data_split.add_argument("--file", default="payload.txt")
    create_data_split.add_argument("--arbiter-host")
    create_data_split.add_argument(
        "--leave-heal-pending",
        action="store_true",
        help="leave all heal controls disabled and skip the full crawl; use independent operator evidence before classifying the fixture",
    )

    create_symlink = sub.add_parser("create-symlink-missing-stale", help="create a stale/broken symlink canary")
    create_symlink.add_argument("--volume", default="gtest4")
    create_symlink.add_argument("--name")
    create_symlink.add_argument("--dir", default="alpha")
    create_symlink.add_argument("--file", default="payload.txt")
    create_symlink.add_argument("--leave-heal-pending", action="store_true")

    create_symlink_outage = sub.add_parser(
        "create-symlink-brick-outage",
        help="create the brick-outage symlink canary",
    )
    create_symlink_outage.add_argument("--volume", default="gtest4")
    create_symlink_outage.add_argument("--name")
    create_symlink_outage.add_argument("--outage-host")
    create_symlink_outage.add_argument("--dir", default="alpha")
    create_symlink_outage.add_argument("--file", default="payload.txt")

    cleanup = sub.add_parser("cleanup", help="remove a canary and its state")
    cleanup.add_argument("--volume", default="gtest4")
    cleanup.add_argument("--name")
    cleanup.add_argument("--dir", default="alpha")
    cleanup.add_argument("--file", default="payload.txt")

    cleanup_wave = sub.add_parser("cleanup-wave", help="cleanup multiple canary scenarios in one repair wave")
    cleanup_wave.add_argument("--volume", default="gtest4")
    cleanup_wave.add_argument("--name", dest="scenarios", action="append", required=True)
    cleanup_wave.add_argument("--dir", default="alpha")
    cleanup_wave.add_argument("--file", default="payload.txt")
    cleanup_wave.add_argument("--parallel-actions", type=int, default=2)
    cleanup_wave.add_argument("--parallel-nice", type=int, default=5)
    cleanup_wave.add_argument("--run-dir")

    cleanup_ctime_chain = sub.add_parser(
        "cleanup-ctime-review-chain",
        help="remove the paired directory and file ctime review canaries",
    )
    cleanup_ctime_chain.add_argument("--volume", default="gtest4")
    cleanup_ctime_chain.add_argument("--name")
    cleanup_ctime_chain.add_argument("--dir", default="alpha")
    cleanup_ctime_chain.add_argument("--file", default="payload.txt")

    directory_backend_plan = sub.add_parser(
        "directory-backend-child-gap-build",
        help="build a plan from a live backend directory child-gap canary state",
    )
    directory_backend_plan.add_argument("--volume", default="gtest4")
    directory_backend_plan.add_argument("--name")
    directory_backend_plan.add_argument("--plan-out", required=True)

    directory_backend_mixed_plan = sub.add_parser(
        "directory-backend-child-gap-mixed-build",
        help="build a plan from a live mixed file-and-directory backend child-gap canary state",
    )
    directory_backend_mixed_plan.add_argument("--volume", default="gtest4")
    directory_backend_mixed_plan.add_argument("--name")
    directory_backend_mixed_plan.add_argument("--plan-out", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    _require_not_root()
    args = build_parser().parse_args(argv)
    scenario = getattr(args, "name", None) or _default_scenario_name()
    if args.cmd.startswith("create"):
        prepare_canary_workspace()
    if args.cmd == "create":
        create_entry_heal_canary(volume=args.volume, scenario=scenario, dir_name=args.dir, file_name=args.file)
        return 0
    if args.cmd == "create-file-metadata":
        create_file_metadata_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-file-posix-metadata":
        create_file_posix_metadata_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
            fields=args.fields,
            hold_mismatch_offline=not args.keep_mismatch_online,
            ssh_user=args.ssh_user,
        )
        return 0
    if args.cmd == "create-file-posix-metadata-split-brain":
        create_file_posix_metadata_split_brain_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
            ssh_user=args.ssh_user,
            leave_heal_pending=args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-file-posix-acl-majority":
        create_file_posix_acl_majority_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
            hold_mismatch_offline=not args.keep_mismatch_online,
            ssh_user=args.ssh_user,
        )
        return 0
    if args.cmd == "create-file-native-pending-metadata":
        create_file_native_pending_metadata_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
            ssh_user=args.ssh_user,
        )
        return 0
    if args.cmd == "create-directory-posix-metadata":
        create_directory_posix_metadata_canary(
            volume=args.volume,
            scenario=args.name or f"repair-canary-directory-posix-{uuid.uuid4().hex[:8]}",
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            child_name=args.child,
            fields=args.fields,
            ssh_user=args.ssh_user,
        )
        return 0
    if args.cmd == "create-directory-posix-default-acl-majority":
        create_directory_posix_default_acl_majority_canary(
            volume=args.volume,
            scenario=args.name or f"repair-canary-directory-posix-{uuid.uuid4().hex[:8]}",
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            child_name=args.child,
            ssh_user=args.ssh_user,
        )
        return 0
    if args.cmd == "create-file-metadata-split-brain":
        create_file_metadata_split_brain_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-afr-metadata-split-brain":
        create_file_metadata_split_brain_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=None,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "file-metadata-split-brain-build":
        plan = build_file_metadata_split_brain_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan

        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "file-posix-metadata-split-brain-build":
        plan = build_file_posix_metadata_split_brain_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan

        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "directory-mdata-no-majority-build":
        plan = build_directory_mdata_no_majority_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan

        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "create-file-ctime-metadata":
        create_file_ctime_metadata_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-ctime-review-chain":
        create_ctime_review_chain_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-missing-file-replica":
        create_missing_file_replica_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-arbiter-missing-gfid":
        create_arbiter_missing_gfid_canary(
            volume=args.volume,
            scenario=scenario,
            object_type=args.object_type,
            dir_name=args.dir,
            file_name=args.file,
            arbiter_host=args.arbiter_host,
        )
        return 0
    if args.cmd == "create-arbiter-only-residue":
        create_arbiter_only_residue_canary(
            volume=args.volume,
            scenario=scenario,
            object_type=args.object_type,
            dir_name=args.dir,
            file_name=args.file,
            arbiter_host=args.arbiter_host,
        )
        return 0
    if args.cmd == "create-missing-file-replica-pair":
        create_missing_file_replica_pair_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-file-child-gap":
        create_file_child_gap_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-file-child-gap-multi":
        create_file_child_gap_multi_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            dir_name=args.dir,
            file_names=list(args.files),
        )
        return 0
    if args.cmd == "file-child-gap-build":
        plan = build_file_child_gap_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan

        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "file-child-gap-multi-build":
        plan = build_file_child_gap_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan

        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "create-file-stale-survivor":
        create_file_stale_survivor_canary(
            volume=args.volume,
            scenario=scenario,
            survivor_host=args.survivor_host,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-file-stale-survivor-pair":
        create_file_stale_survivor_pair_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-orphaned-gfid-hardlink":
        create_orphaned_gfid_hardlink_canary(
            volume=args.volume,
            scenario=scenario,
            orphan_host=args.orphan_host,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-file-handle-ghost":
        create_file_handle_ghost_canary(
            volume=args.volume,
            scenario=scenario,
            ghost_host=args.ghost_host,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-child-gap":
        create_child_gap_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            parent_name=args.parent,
            present_child=args.present_child,
            missing_child=args.missing_child,
        )
        return 0
    if args.cmd == "create-directory-child-gap":
        create_directory_child_gap_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            parent_name=args.parent,
            present_child=args.present_child,
            missing_child=args.missing_child,
            leave_heal_pending=args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-directory-backend-child-gap":
        create_directory_backend_child_gap_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            parent_name=args.parent,
            present_child=args.present_child,
            missing_child=args.missing_child,
            tree_depth=args.tree_depth,
            gap_level=args.gap_level,
            leave_heal_pending=args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-directory-backend-child-gap-mixed":
        create_directory_backend_child_gap_mixed_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            tree_depth=args.tree_depth,
            gap_level=args.gap_level,
            file_names=list(args.files or []),
        )
        return 0
    if args.cmd == "create-directory-backend-child-gap-bounded":
        create_directory_backend_child_gap_bounded_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            parent_name=args.parent,
            present_child=args.present_child,
            missing_child=args.missing_child,
        )
        return 0
    if args.cmd == "create-directory-backend-child-gap-canonical":
        create_directory_backend_child_gap_canonical_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            parent_name=args.parent,
            present_child=args.present_child,
            missing_child=args.missing_child,
        )
        return 0
    if args.cmd == "directory-backend-child-gap-build":
        plan = build_directory_backend_child_gap_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan
        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "directory-backend-child-gap-mixed-build":
        plan = build_directory_backend_child_gap_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan
        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "create-directory-child-reference":
        create_directory_child_reference_canary(
            volume=args.volume,
            scenario=scenario,
            missing_host=args.missing_host,
            parent_name=args.parent,
            present_child=args.present_child,
            missing_child=args.missing_child,
        )
        return 0
    if args.cmd == "observe-directory-child-state":
        observe_directory_child_state_canary(volume=args.volume, scenario=scenario)
        return 0
    if args.cmd == "create-directory-gfid-merge":
        create_directory_gfid_merge_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-directory-gfid-tie":
        create_directory_gfid_tie_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
        )
        return 0
    if args.cmd == "create-directory-gfid-mask":
        create_directory_gfid_mask_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
            shadow_file_name=args.shadow_file,
        )
        return 0
    if args.cmd == "create-type-mismatch":
        create_type_mismatch_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
            shadow_name=args.shadow_file,
        )
        return 0
    if args.cmd == "type-mismatch-build":
        plan = build_type_mismatch_plan_from_canary_state(volume=args.volume, scenario=scenario)
        from .planner import summarize_plan

        write_json_shared(args.plan_out, plan)
        print(summarize_plan(load_plan_actions(plan)))
        return 0
    if args.cmd == "observe-directory-gfid-state":
        observe_directory_gfid_state_canary(volume=args.volume, scenario=scenario)
        return 0
    if args.cmd == "observe-file-state":
        observe_file_state_canary(volume=args.volume, scenario=scenario, heal_root=args.heal_root)
        return 0
    if args.cmd == "create-directory-metadata":
        create_directory_metadata_canary(
            volume=args.volume,
            scenario=scenario,
            mismatch_host=args.mismatch_host,
            dir_name=args.dir,
        )
        return 0
    if args.cmd == "create-directory-mdata-no-majority":
        create_directory_mdata_no_majority_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
        )
        return 0
    if args.cmd == "create-zero-afr-native-heal-smoke":
        create_directory_mdata_no_majority_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
        )
        return 0
    if args.cmd == "create-directory-stale-survivor":
        create_directory_stale_survivor_canary(
            volume=args.volume,
            scenario=scenario,
            survivor_host=args.survivor_host,
            dir_name=args.dir,
        )
        return 0
    if args.cmd == "create-file-gfid-split":
        create_file_gfid_split_canary(volume=args.volume, scenario=scenario, dir_name=args.dir, file_name=args.file)
        return 0
    if args.cmd == "create-file-content-split-brain":
        create_file_content_split_brain_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "create-file-data-split-brain":
        create_file_data_split_brain_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
            arbiter_host=args.arbiter_host,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-symlink-missing-stale":
        create_symlink_missing_stale_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
            trigger_heal=not args.leave_heal_pending,
        )
        return 0
    if args.cmd == "create-symlink-brick-outage":
        create_symlink_brick_outage_canary(
            volume=args.volume,
            scenario=scenario,
            outage_host=args.outage_host,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    if args.cmd == "cleanup":
        cleanup_canary(volume=args.volume, scenario=scenario, dir_name=args.dir, file_name=args.file)
        return 0
    if args.cmd == "cleanup-wave":
        cleanup_canary_wave(
            volume=args.volume,
            scenarios=args.scenarios,
            dir_name=args.dir,
            file_name=args.file,
            parallel_actions=args.parallel_actions,
            parallel_nice=args.parallel_nice,
            run_dir=args.run_dir,
        )
        return 0
    if args.cmd == "cleanup-ctime-review-chain":
        cleanup_ctime_review_chain_canary(
            volume=args.volume,
            scenario=scenario,
            dir_name=args.dir,
            file_name=args.file,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
