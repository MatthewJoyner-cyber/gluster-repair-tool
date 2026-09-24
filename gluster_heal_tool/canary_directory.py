# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Directory-family canary builders and observers."""
from __future__ import annotations

import uuid
import subprocess
from pathlib import Path
from typing import Any

from . import canary_shared as canary_api
from .controller_paths import default_canary_stage_local_path
from .controller_paths import default_temp_mount_root
from .evidence_provenance import canary_state_plan_provenance
from .canary_observation import _capture_mount_stat_snapshot
from .canary_observation import _capture_parent_lookup_snapshot
from .canary_observation import _observation_log_path
from .canary_observation import _write_observation_log
from .type_mismatch import type_mismatch_quarantine_recommendation
from .volume import run_heal
from .volume import set_heal_settings

DEFAULT_SERVICE_USER = canary_api.DEFAULT_SERVICE_USER
DEFAULT_WORKER_PATH = canary_api.DEFAULT_WORKER_PATH


def _brick_hosts_and_backend_roots(volume: str) -> tuple[list[str], dict[str, str]]:
    try:
        return canary_api._brick_hosts_and_paths(volume)
    except Exception:
        brick_hosts, backend_root = canary_api._brick_hosts_and_root(volume)
        return brick_hosts, {host: backend_root for host in brick_hosts}


def _canary_temp_mount_root(volume: str) -> str:
    return str(default_temp_mount_root() / "repair-canary" / volume.strip().strip("/"))


def _mountpoint_is_active(path: str) -> bool:
    proc = subprocess.run(["mountpoint", "-q", "--", path], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _ensure_canary_mount(volume: str, mount_root: str) -> None:
    canary_api._local_mount_dir(mount_root)
    if _mountpoint_is_active(mount_root):
        return
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            "mount",
            "-t",
            "glusterfs",
            f"localhost:{volume.strip().strip('/')}",
            mount_root,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to mount temporary canary workspace for {volume}")


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


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _inspect_type_mismatch_backend_observations(
    *,
    volume: str,
    scenario: str,
    backend_root: str,
    backend_target: str,
    file_hosts: list[str],
    directory_hosts: list[str],
    ssh_user: str,
    worker_path: str,
) -> dict[str, dict[str, Any]]:
    observations_by_host: dict[str, dict[str, Any]] = {}
    for host in list(dict.fromkeys(file_hosts + directory_hosts)):
        request_backend_root = backend_root or canary_api._backend_root_for_host(volume, host)
        try:
            response = canary_api._canary_worker_command(
                host,
                ssh_user=ssh_user,
                worker_path=worker_path,
                request=canary_api._make_request(
                    volume,
                    scenario,
                    request_backend_root,
                    [{"op": "inspect_path", "label": "backend_target", "path": backend_target}],
                ),
            )
        except Exception as exc:
            observations_by_host[host] = {"path": backend_target, "error": str(exc)}
            continue
        result = next((item for item in response.get("results", []) if item.get("op") == "inspect_path"), {})
        error = str(result.get("error") or "").strip()
        if error or not result.get("ok"):
            observations_by_host[host] = {
                "path": str(result.get("path") or backend_target),
                "error": error or "inspect_path failed",
            }
            continue
        observations_by_host[host] = {
            "path": str(result.get("path") or backend_target),
            "kind": str(result.get("kind") or ""),
            "mtime": result.get("mtime"),
            "size": result.get("size"),
        }
    return observations_by_host


def _type_mismatch_quarantine_recommendation(
    observations_by_host: dict[str, dict[str, Any]],
    *,
    file_hosts: list[str],
    directory_hosts: list[str],
) -> tuple[str, str, str, str]:
    file_copies = []
    directory_copies = []
    for host, observation in observations_by_host.items():
        if not isinstance(observation, dict) or str(observation.get("error") or "").strip():
            continue
        kind = str(observation.get("kind") or "").strip().lower()
        copy = {"host": host, "mtime": _coerce_int(observation.get("mtime"))}
        if host in file_hosts and kind == "file":
            file_copies.append(copy)
        elif host in directory_hosts and kind in {"dir", "directory"}:
            directory_copies.append(copy)
    return type_mismatch_quarantine_recommendation(
        file_copies,
        directory_copies,
        expected_hosts=[*file_hosts, *directory_hosts],
    )


def _capture_directory_brick_observations(
    *,
    volume: str,
    scenario: str,
    backend_root: str,
    brick_hosts: list[str],
    backend_parent: str,
    mount_parent: str,
    mount_path: str,
    missing_backend: str,
    ssh_user: str,
    worker_path: str,
) -> list[dict[str, Any]]:
    ops = [
        {"op": "inspect_path", "label": "backend_parent", "path": backend_parent},
        {"op": "inspect_path", "label": "backend_missing_child", "path": missing_backend},
        {"op": "inspect_path", "label": "mount_parent", "path": mount_parent},
        {"op": "inspect_path", "label": "mount_path", "path": mount_path},
    ]
    observations: list[dict[str, Any]] = []
    for host in brick_hosts:
        response = canary_api._canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(volume, scenario, backend_root, ops),
        )
        observations.append(
            {
                "host": response.get("host") or host,
                "results": response.get("results", []),
            }
        )
    return observations


def _deep_directory_tree_paths(mount_root: str, scenario: str, tree_depth: int) -> dict[str, str]:
    tree_root = f"{mount_root}/{scenario}/tree"
    paths: dict[str, str] = {
        "tree_root": tree_root,
        "root_file": f"{tree_root}/root.txt",
    }
    current_dir = tree_root
    for level in range(1, tree_depth + 1):
        current_dir = f"{current_dir}/level{level}"
        paths[f"level{level}_dir"] = current_dir
        paths[f"level{level}_file"] = f"{current_dir}/level{level}.txt"
    return paths


def _create_directory_child_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    kind: str,
    label: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    leave_heal_pending: bool = False,
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    missing_host = canary_api._select_role_host(brick_hosts, missing_host, role="missing", default_to_next=True)
    mount_root = _canary_temp_mount_root(volume)
    mount_path = f"{mount_root}/{scenario}/{parent_name}/{present_child}/{missing_child}"
    mount_parent = f"{mount_root}/{scenario}/{parent_name}/{present_child}"
    backend_root = backend_roots[missing_host]
    backend_scenario_root = f"{backend_root}/{scenario}"
    missing_backend = f"{backend_scenario_root}/{parent_name}/{present_child}/{missing_child}"

    if leave_heal_pending:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(mount_path)

    request = canary_api._make_request(
        volume,
        scenario,
        backend_root,
        [
            {
                "op": "rm",
                "path": missing_backend,
                "recursive": True,
                "force": True,
            }
        ],
    )
    response = canary_api._canary_worker_command(missing_host, ssh_user=ssh_user, worker_path=worker_path, request=request)
    canary_api._print_response(f"{label} worker", response)

    state_payload = {
        "kind": kind,
        "leave_heal_pending": leave_heal_pending,
        "volume": volume,
        "scenario": scenario,
        "missing_host": missing_host,
        "parent_name": parent_name,
        "present_child": present_child,
        "missing_child": missing_child,
        "mount_root": mount_root,
        "mount_parent": mount_parent,
        "mount_path": mount_path,
        "backend_root": backend_root,
        "backend_roots": backend_roots,
        "backend_scenario_root": backend_scenario_root,
        "missing_backend": missing_backend,
    }
    state_payload["baseline_parent_lookup"] = _capture_parent_lookup_snapshot(mount_parent)
    canary_api._write_state(volume, scenario, state_payload)
    snapshot_paths = [mount_parent, mount_path]
    state_payload["snapshot_paths"] = snapshot_paths
    state_payload["mount_snapshot"] = {
        "paths": [_capture_mount_stat_snapshot(path) for path in snapshot_paths],
    }
    if kind in {"directory-backend-child-gap", "directory-backend-child-gap-bounded", "directory-backend-child-gap-canonical"}:
        state_payload["backend_observations"] = _capture_directory_brick_observations(
            volume=volume,
            scenario=scenario,
            backend_root=backend_root,
            brick_hosts=brick_hosts,
            backend_parent=f"{backend_scenario_root}/{parent_name}/{present_child}",
            mount_parent=mount_parent,
            mount_path=mount_path,
            missing_backend=missing_backend,
            ssh_user=ssh_user,
            worker_path=worker_path,
        )
        state_payload["notes"] = [
            "directory-backend-child-gap marker",
            *(
                ["bounded subtree canary"]
                if kind == "directory-backend-child-gap-bounded"
                else []
            ),
            *(
                ["canonical content recreate canary"]
                if kind == "directory-backend-child-gap-canonical"
                else []
            ),
        ]
    canary_api._write_state(volume, scenario, state_payload)
    _write_observation_log(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                f"Created gtest {label} canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_root}/{scenario}/{parent_name}/{present_child}",
                f"  present child: {present_child}",
                f"  missing child: {missing_child}",
                f"  missing host: {missing_host}",
                f"  backend root: {backend_scenario_root}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                *(
                    ["  heal: disabled pending independent operator evidence; cleanup restores it"]
                    if leave_heal_pending
                    else []
                ),
                f"Next: sudo -n find {mount_root}/{scenario}/{parent_name}/{present_child} -maxdepth 4 -exec stat -- {{}} +",
                f"Then: ./gluster-gtest-canary.sh observe-directory-child-state --volume {volume} --name {scenario}",
                f"Then: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_child_gap_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_child_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        parent_name=parent_name,
        present_child=present_child,
        missing_child=missing_child,
        kind="child-gap",
        label="child-gap",
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_directory_child_gap_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    leave_heal_pending: bool = False,
) -> None:
    _create_directory_child_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        parent_name=parent_name,
        present_child=present_child,
        missing_child=missing_child,
        kind="child-gap",
        label="directory-child-gap",
        ssh_user=ssh_user,
        worker_path=worker_path,
        leave_heal_pending=leave_heal_pending,
    )


def create_directory_backend_child_gap_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    tree_depth: int = 1,
    gap_level: int = 1,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    leave_heal_pending: bool = False,
) -> None:
    if tree_depth > 1:
        create_directory_backend_child_gap_deep_canary(
            volume=volume,
            scenario=scenario,
            missing_host=missing_host,
            tree_depth=tree_depth,
            gap_level=gap_level,
            ssh_user=ssh_user,
            worker_path=worker_path,
            leave_heal_pending=leave_heal_pending,
        )
        return
    _create_directory_child_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        parent_name=parent_name,
        present_child=present_child,
        missing_child=missing_child,
        kind="directory-backend-child-gap",
        label="directory-backend-child-gap",
        ssh_user=ssh_user,
        worker_path=worker_path,
        leave_heal_pending=leave_heal_pending,
    )


def create_directory_backend_child_gap_deep_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    tree_depth: int = 3,
    gap_level: int = 1,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
    leave_heal_pending: bool = False,
) -> None:
    if tree_depth < 3:
        raise RuntimeError("deep directory child-gap canary needs tree_depth >= 3")
    if gap_level < 1 or gap_level > tree_depth:
        raise RuntimeError("gap_level must be between 1 and tree_depth")

    brick_hosts, backend_root = canary_api._brick_hosts_and_root(volume)
    missing_host = canary_api._select_role_host(brick_hosts, missing_host, role="missing", default_to_next=True)
    mount_root = _canary_temp_mount_root(volume)
    tree_paths = _deep_directory_tree_paths(mount_root, scenario, tree_depth)
    tree_root = tree_paths["tree_root"]
    backend_scenario_root = f"{backend_root}/{scenario}"
    backend_tree_root = f"{backend_scenario_root}/tree"

    # Build a 3-deep mixed tree with one file in each directory.
    if leave_heal_pending:
        set_heal_settings(volume, False)
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(tree_root)
    canary_api._local_write_file(tree_paths["root_file"], "root\n")
    for level in range(1, tree_depth + 1):
        canary_api._local_mount_dir(tree_paths[f"level{level}_dir"])
        canary_api._local_write_file(tree_paths[f"level{level}_file"], f"level {level}\n")

    gap_dir_key = f"level{gap_level}_dir"
    gap_target = tree_paths[gap_dir_key]
    gap_parent = tree_paths["tree_root"] if gap_level == 1 else tree_paths[f"level{gap_level - 1}_dir"]
    deepest_leaf = tree_paths[f"level{tree_depth}_file"]
    missing_backend = f"{backend_tree_root}/" + "/".join(f"level{level}" for level in range(1, gap_level + 1))

    request = canary_api._make_request(
        volume,
        scenario,
        backend_root,
        [
            {
                "op": "rm",
                "path": missing_backend,
                "recursive": True,
                "force": True,
            }
        ],
    )
    response = canary_api._canary_worker_command(missing_host, ssh_user=ssh_user, worker_path=worker_path, request=request)
    canary_api._print_response("directory-backend-child-gap-deep worker", response)

    state_payload = {
        "kind": "directory-backend-child-gap",
        "leave_heal_pending": leave_heal_pending,
        "volume": volume,
        "scenario": scenario,
        "missing_host": missing_host,
        "mount_root": mount_root,
        "mount_parent": gap_parent,
        "mount_path": deepest_leaf,
        "backend_root": backend_root,
        "backend_scenario_root": backend_scenario_root,
        "backend_tree_root": backend_tree_root,
        "missing_backend": missing_backend,
        "tree_depth": tree_depth,
        "gap_level": gap_level,
        "tree_paths": tree_paths,
        "snapshot_paths": [tree_root, gap_parent, gap_target, deepest_leaf],
        "notes": [
            "directory-backend-child-gap marker",
            f"deep tree depth: {tree_depth}",
            f"gap level: {gap_level}",
            f"gap target: {gap_target}",
            f"missing host: {missing_host}",
            "mixed tree canary: one file in each directory, then remove one backend subtree on a single brick",
        ],
    }
    state_payload["baseline_parent_lookup"] = _capture_parent_lookup_snapshot(gap_parent)
    state_payload["mount_snapshot"] = {
        "paths": [_capture_mount_stat_snapshot(path) for path in state_payload["snapshot_paths"]],
    }
    state_payload["backend_observations"] = _capture_directory_brick_observations(
        volume=volume,
        scenario=scenario,
        backend_root=backend_root,
        brick_hosts=brick_hosts,
        backend_parent=gap_parent.replace(mount_root, backend_scenario_root, 1),
        mount_parent=gap_parent,
        mount_path=deepest_leaf,
        missing_backend=missing_backend,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )
    canary_api._write_state(volume, scenario, state_payload)
    _write_observation_log(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                "Created gtest directory-backend-child-gap-deep canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  tree depth: {tree_depth}",
                f"  gap level: {gap_level}",
                f"  tree root: {tree_root}",
                f"  gap target: {gap_target}",
                f"  missing host: {missing_host}",
                f"  backend tree root: {backend_tree_root}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                f"Next: sudo -n find {tree_root} -maxdepth {tree_depth + 2} -exec stat -- {{}} +",
                f"Then: ./gluster-gtest-canary.sh observe-directory-child-state --volume {volume} --name {scenario}",
                f"Then: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_directory_backend_child_gap_mixed_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    tree_depth: int = 3,
    gap_level: int = 1,
    file_names: list[str] | None = None,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    if tree_depth < 3:
        raise RuntimeError("mixed directory child-gap canary needs tree_depth >= 3")
    if gap_level < 1 or gap_level > tree_depth:
        raise RuntimeError("gap_level must be between 1 and tree_depth")

    brick_hosts, backend_root = canary_api._brick_hosts_and_root(volume)
    missing_host = canary_api._select_role_host(brick_hosts, missing_host, role="missing", default_to_next=True)
    source_host = next((host for host in brick_hosts if host != missing_host), "")
    if not source_host:
        raise RuntimeError(f"need at least one source host besides {missing_host}")

    mount_root = _canary_temp_mount_root(volume)
    tree_paths = _deep_directory_tree_paths(mount_root, scenario, tree_depth)
    tree_root = tree_paths["tree_root"]
    backend_scenario_root = f"{backend_root}/{scenario}"
    backend_tree_root = f"{backend_scenario_root}/tree"
    root_file_names = list(dict.fromkeys(file_names or [Path(tree_paths["root_file"]).name]))
    if not root_file_names:
        raise RuntimeError("need at least one file name for mixed directory child-gap canary")

    # Build a 3-deep mixed tree with one file in each directory, plus root-level file children.
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(tree_root)
    canary_api._local_write_file(tree_paths["root_file"], "root\n")
    for extra_file_name in root_file_names:
        if extra_file_name == Path(tree_paths["root_file"]).name:
            continue
        canary_api._local_write_file(f"{tree_root}/{extra_file_name}", f"{extra_file_name}\n")
    for level in range(1, tree_depth + 1):
        canary_api._local_mount_dir(tree_paths[f"level{level}_dir"])
        canary_api._local_write_file(tree_paths[f"level{level}_file"], f"level {level}\n")

    file_children: list[dict[str, Any]] = []
    root_file_targets = []
    for file_name in root_file_names:
        mount_file = f"{tree_root}/{file_name}"
        backend_file = f"{backend_tree_root}/{file_name}"
        root_file_targets.append((file_name, mount_file, backend_file))

    pending_value = "000000010000000000000001"
    missing_client_index = brick_hosts.index(missing_host)
    for file_name, mount_file, backend_file in root_file_targets:
        source_response = canary_api._canary_worker_command(
            source_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                backend_root,
                [
                    {
                        "op": "setxattr",
                        "path": backend_file,
                        "name": f"trusted.afr.{volume}-client-{missing_client_index}",
                        "value_hex": pending_value,
                    },
                    {
                        "op": "touch_index_from_target",
                        "target_path": backend_file,
                        "index_root": f"{backend_root}/.glusterfs/indices/xattrop",
                    },
                ],
            ),
        )
        canary_api._print_response(f"directory-backend-child-gap-mixed file worker {file_name}", source_response)
        source_results = source_response.get("results", [])
        index_entry = next((item for item in source_results if item.get("op") == "touch_index_from_target" and item.get("ok")), {})
        canonical_gfid = str(index_entry.get("value_uuid") or "")
        if not canonical_gfid:
            raise RuntimeError(f"mixed directory child-gap worker did not return a canonical file GFID for {file_name}")
        file_gfid_path = canary_api._file_gfid_link_path(backend_root, canonical_gfid)

        missing_response = canary_api._canary_worker_command(
            missing_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                backend_root,
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
        canary_api._print_response(f"directory-backend-child-gap-mixed target worker {file_name}", missing_response)
        file_children.append(
            {
                "file_name": file_name,
                "mount_file": mount_file,
                "backend_target": backend_file,
                "gfid_uuid": canonical_gfid,
                "gfid_hex": canonical_gfid.replace("-", ""),
                "file_gfid_path": file_gfid_path,
                "source_host": source_host,
            }
        )

    gap_dir_key = f"level{gap_level}_dir"
    gap_target = tree_paths[gap_dir_key]
    gap_parent = tree_paths["tree_root"] if gap_level == 1 else tree_paths[f"level{gap_level - 1}_dir"]
    deepest_leaf = tree_paths[f"level{tree_depth}_file"]
    missing_backend = f"{backend_tree_root}/" + "/".join(f"level{level}" for level in range(1, gap_level + 1))

    request = canary_api._make_request(
        volume,
        scenario,
        backend_root,
        [
            {
                "op": "rm",
                "path": missing_backend,
                "recursive": True,
                "force": True,
            }
        ],
    )
    response = canary_api._canary_worker_command(missing_host, ssh_user=ssh_user, worker_path=worker_path, request=request)
    canary_api._print_response("directory-backend-child-gap-mixed directory worker", response)

    state_payload = {
        "kind": "directory-backend-child-gap-mixed",
        "volume": volume,
        "scenario": scenario,
        "missing_host": missing_host,
        "source_host": source_host,
        "dir_name": "tree",
        "mount_root": mount_root,
        "mount_parent": gap_parent,
        "mount_path": deepest_leaf,
        "backend_root": backend_root,
        "backend_scenario_root": backend_scenario_root,
        "backend_tree_root": backend_tree_root,
        "missing_backend": missing_backend,
        "tree_depth": tree_depth,
        "gap_level": gap_level,
        "tree_paths": tree_paths,
        "file_children": file_children,
        "snapshot_paths": [tree_root, gap_parent, gap_target, deepest_leaf],
        "notes": [
            "directory-backend-child-gap marker",
            "mixed file-and-directory child-gap canary",
            f"deep tree depth: {tree_depth}",
            f"gap level: {gap_level}",
            f"gap target: {gap_target}",
            f"missing host: {missing_host}",
            "mixed tree canary: one file in each directory, plus root-level file gaps and one backend subtree removed on a single brick",
        ],
    }
    state_payload["baseline_parent_lookup"] = _capture_parent_lookup_snapshot(gap_parent)
    state_payload["mount_snapshot"] = {
        "paths": [_capture_mount_stat_snapshot(path) for path in state_payload["snapshot_paths"]],
    }
    state_payload["backend_observations"] = _capture_directory_brick_observations(
        volume=volume,
        scenario=scenario,
        backend_root=backend_root,
        brick_hosts=brick_hosts,
        backend_parent=gap_parent.replace(mount_root, backend_scenario_root, 1),
        mount_parent=gap_parent,
        mount_path=deepest_leaf,
        missing_backend=missing_backend,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )
    canary_api._write_state(volume, scenario, state_payload)
    _write_observation_log(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                "Created gtest directory-backend-child-gap-mixed canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  tree depth: {tree_depth}",
                f"  gap level: {gap_level}",
                f"  tree root: {tree_root}",
                f"  gap target: {gap_target}",
                f"  missing host: {missing_host}",
                f"  source host: {source_host}",
                f"  backend tree root: {backend_tree_root}",
                f"  files: {', '.join(item['file_name'] for item in file_children)}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                f"Next: sudo -n find {tree_root} -maxdepth {tree_depth + 2} -exec stat -- {{}} +",
                f"Then: ./gluster-gtest-canary.sh observe-directory-child-state --volume {volume} --name {scenario}",
                f"Then: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_directory_backend_child_gap_bounded_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_child_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        parent_name=parent_name,
        present_child=present_child,
        missing_child=missing_child,
        kind="directory-backend-child-gap-bounded",
        label="directory-backend-child-gap-bounded",
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_directory_backend_child_gap_canonical_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_child_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        parent_name=parent_name,
        present_child=present_child,
        missing_child=missing_child,
        kind="directory-backend-child-gap-canonical",
        label="directory-backend-child-gap-canonical",
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_directory_stale_survivor_canary(
    *,
    volume: str,
    scenario: str,
    survivor_host: str | None = None,
    dir_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    brick_roles_by_host = canary_api._brick_roles_by_host(volume)
    if len(brick_hosts) < 3:
        raise RuntimeError(f"need at least 3 bricks for directory stale-survivor canary on {volume}")

    survivor_host = canary_api._select_role_host(brick_hosts, survivor_host, role="survivor", default_to_next=False)
    remove_hosts = [host for host in brick_hosts if host != survivor_host]
    if not remove_hosts:
        raise RuntimeError(f"need at least one replica to remove for directory stale-survivor canary on {volume}")

    mount_root = _canary_temp_mount_root(volume)
    mount_dir = f"{mount_root}/{scenario}/{dir_name}"
    backend_target_by_host = {
        host: f"{backend_roots[host]}/{scenario}/{dir_name}" for host in brick_hosts
    }
    survivor_backend_root = backend_roots[survivor_host]
    survivor_backend_target = backend_target_by_host[survivor_host]
    canonical_gfid_hex = canary_api._new_gfid_hex()
    canonical_gfid_uuid = str(uuid.UUID(hex=canonical_gfid_hex))
    index_root_by_host = {
        host: f"{backend_roots[host]}/.glusterfs/indices/xattrop" for host in brick_hosts
    }

    canary_api._run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(mount_dir)

    for host in brick_hosts:
        response = canary_api._canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                backend_roots[host],
                [
                    {
                        "op": "mkdir",
                        "path": backend_target_by_host[host],
                    },
                    {
                        "op": "setxattr",
                        "path": backend_target_by_host[host],
                        "name": "trusted.gfid",
                        "value_hex": canonical_gfid_hex,
                    },
                    {
                        "op": "link_directory_gfid_from_target",
                        "target_path": backend_target_by_host[host],
                    },
                    {
                        "op": "touch_index_from_target",
                        "target_path": backend_target_by_host[host],
                        "index_root": index_root_by_host[host],
                    },
                ],
            ),
        )
        canary_api._print_response(f"directory-stale-survivor worker {host}", response)

    for host in remove_hosts:
        prune_response = canary_api._canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                backend_roots[host],
                [
                    {
                        "op": "rm",
                        "path": backend_target_by_host[host],
                        "recursive": True,
                        "force": True,
                    },
                    {
                        "op": "rm",
                        "path": canary_api._directory_gfid_link_path(backend_roots[host], canonical_gfid_uuid),
                        "recursive": False,
                        "force": True,
                    },
                ],
            ),
        )
        canary_api._print_response(f"directory-stale-survivor prune worker {host}", prune_response)

    state_payload = {
        "kind": "directory-stale-survivor",
        "volume": volume,
        "scenario": scenario,
        "survivor_host": survivor_host,
        "remove_hosts": remove_hosts,
        "dir_name": dir_name,
        "mount_root": mount_root,
        "mount_target": mount_dir,
        "backend_root": survivor_backend_root,
        "backend_target": survivor_backend_target,
        "backend_roots": backend_roots,
        "directory_gfid_path": canary_api._directory_gfid_link_path(survivor_backend_root, canonical_gfid_uuid),
        "directory_gfid_paths_by_host": {
            host: [canary_api._directory_gfid_link_path(backend_roots[host], canonical_gfid_uuid)]
            for host in brick_hosts
        },
        "index_root": index_root_by_host[survivor_host],
        "index_hosts": [survivor_host],
        "gfid_uuid": canonical_gfid_uuid,
        "gfid_hex": canonical_gfid_hex,
        "brick_roles_by_host": brick_roles_by_host,
        "cleanup_hints": {
            "stale_directory_gfid_paths_by_host": {
                host: [canary_api._directory_gfid_link_path(backend_roots[host], canonical_gfid_uuid)]
                for host in remove_hosts
            },
            "stale_backends_by_host": {host: [backend_target_by_host[host]] for host in remove_hosts},
        },
    }
    canary_api._write_state(volume, scenario, state_payload)
    _write_observation_log(volume, scenario, state_payload)
    _trigger_canary_heal(volume, scenario)

    print(
        "\n".join(
            [
                "Created gtest directory-stale-survivor canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_dir}",
                f"  survivor host: {survivor_host}",
                f"  removed hosts: {', '.join(remove_hosts)}",
                *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
                f"  backend target: {survivor_backend_target}",
                f"  directory GFID path: {state_payload['directory_gfid_path']}",
                f"  canonical directory GFID: {canonical_gfid_uuid}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Expected: planner should treat this as stale directory residue and delete the arbiter-side residue only.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_directory_child_reference_canary(
    *,
    volume: str,
    scenario: str,
    missing_host: str | None = None,
    parent_name: str,
    present_child: str,
    missing_child: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_child_canary(
        volume=volume,
        scenario=scenario,
        missing_host=missing_host,
        parent_name=parent_name,
        present_child=present_child,
        missing_child=missing_child,
        kind="directory-child-reference",
        label="directory-child-reference",
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def _create_directory_gfid_conflict_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    kind: str,
    label: str,
    child_name: str | None = None,
    right_child_name: str | None = None,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 4:
        raise RuntimeError(f"need at least 4 bricks for {label} canary on {volume}")

    mount_root = _canary_temp_mount_root(volume)
    mount_target = f"{mount_root}/{scenario}/{dir_name}"
    mount_child = f"{mount_target}/{child_name}" if child_name else ""
    mount_right_child = f"{mount_target}/{right_child_name}" if right_child_name else ""
    backend_target_by_host = {
        host: f"{backend_roots[host]}/{scenario}/{dir_name}" for host in brick_hosts
    }
    index_root_by_host = {
        host: f"{backend_roots[host]}/.glusterfs/indices/xattrop" for host in brick_hosts
    }
    backend_root = backend_roots[brick_hosts[0]]
    backend_target = backend_target_by_host[brick_hosts[0]]
    index_root = index_root_by_host[brick_hosts[0]]
    left_hosts = brick_hosts[:2]
    right_hosts = brick_hosts[2:4]
    left_gfid_hex = canary_api._new_gfid_hex()
    right_gfid_hex = canary_api._new_gfid_hex()
    left_gfid = str(uuid.UUID(hex=left_gfid_hex))
    right_gfid = str(uuid.UUID(hex=right_gfid_hex))

    canary_api._run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(mount_target)
    if kind == "directory-gfid-merge" and mount_child:
        canary_api._local_mount_file(mount_child)

    directory_gfid_paths_by_host: dict[str, list[str]] = {}
    for host in left_hosts + right_hosts:
        is_left = host in left_hosts
        host_backend_root = backend_roots[host]
        host_backend_target = backend_target_by_host[host]
        host_index_root = index_root_by_host[host]
        cohort_gfid_hex = left_gfid_hex if is_left else right_gfid_hex
        opposite_indices = range(2, 4) if is_left else range(0, 2)
        cohort_child_name = child_name if is_left else (right_child_name or child_name)
        response = canary_api._canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                host_backend_root,
                [
                    {
                        "op": "mkdir",
                        "path": host_backend_target,
                    },
                    {
                        "op": "setxattr",
                        "path": host_backend_target,
                        "name": "trusted.gfid",
                        "value_hex": cohort_gfid_hex,
                    },
                    {
                        "op": "link_directory_gfid_from_target",
                        "target_path": host_backend_target,
                    },
                    *[
                        {
                            "op": "setxattr",
                            "path": host_backend_target,
                            "name": f"trusted.afr.{volume}-client-{idx}",
                            "value_hex": "000000010000000000000001",
                        }
                        for idx in opposite_indices
                    ],
                    {
                        "op": "touch_index_from_target",
                        "target_path": host_backend_target,
                        "index_root": host_index_root,
                    },
                    *(
                        [
                            {
                                "op": "touch",
                                "path": f"{host_backend_target}/{cohort_child_name}",
                            }
                        ]
                        if cohort_child_name
                        else []
                    ),
                ],
            ),
        )
        canary_api._print_response(f"{label} worker {host}", response)
        worker_results = response.get("results", [])
        link_entry = next(
            (
                item
                for item in worker_results
                if item.get("op") == "link_directory_gfid_from_target" and item.get("ok")
            ),
            {},
        )
        directory_gfid_path = str(link_entry.get("directory_gfid_path") or "")
        if directory_gfid_path:
            directory_gfid_paths_by_host.setdefault(host, []).append(directory_gfid_path)

    state_payload = {
        "kind": kind,
        "leave_heal_pending": True,
        "volume": volume,
        "scenario": scenario,
        "dir_name": dir_name,
        "child_name": child_name or "",
        "right_child_name": right_child_name or "",
        "mount_root": mount_root,
        "mount_target": mount_target,
        "mount_child": mount_child,
        "mount_right_child": mount_right_child,
        "backend_root": backend_root,
        "backend_target": backend_target,
        "index_root": index_root,
        "backend_roots": backend_roots,
        "backend_target_by_host": backend_target_by_host,
        "index_root_by_host": index_root_by_host,
        "directory_gfid_paths_by_host": directory_gfid_paths_by_host,
        "left_hosts": left_hosts,
        "right_hosts": right_hosts,
        "left_gfid_uuid": left_gfid,
        "left_gfid_hex": left_gfid_hex,
        "right_gfid_uuid": right_gfid,
        "right_gfid_hex": right_gfid_hex,
    }
    canary_api._write_state(volume, scenario, state_payload)
    mount_snapshot_paths = [mount_target]
    if mount_child:
        mount_snapshot_paths.append(mount_child)
    if mount_right_child and mount_right_child not in mount_snapshot_paths:
        mount_snapshot_paths.append(mount_right_child)
    mount_snapshot = {
        "paths": [_capture_mount_stat_snapshot(path) for path in mount_snapshot_paths],
    }
    state_payload["mount_snapshot"] = mount_snapshot
    canary_api._write_state(volume, scenario, state_payload)
    _write_observation_log(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                f"Created gtest {label} canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_target}",
                f"  child: {mount_child or '(none)'}",
                f"  left hosts: {', '.join(left_hosts)}",
                f"  right hosts: {', '.join(right_hosts)}",
                f"  backend target: {backend_target}",
                f"  index bucket: {index_root}",
                f"  left directory GFID: {left_gfid}",
                f"  right directory GFID: {right_gfid}",
                "Expected: merge policy should auto-promote only when the child sets match; the bare tie should remain review-only.",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Next: run the client-side stat or find probe, then use observe-directory-gfid-state to compare the snapshot.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_directory_gfid_merge_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_gfid_conflict_canary(
        volume=volume,
        scenario=scenario,
        dir_name=dir_name,
        kind="directory-gfid-merge",
        label="directory-gfid-merge",
        child_name=file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_directory_gfid_tie_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_gfid_conflict_canary(
        volume=volume,
        scenario=scenario,
        dir_name=dir_name,
        kind="directory-gfid-tie",
        label="directory-gfid-tie",
        child_name=None,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_directory_gfid_mask_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    shadow_file_name: str = "shadow.txt",
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    _create_directory_gfid_conflict_canary(
        volume=volume,
        scenario=scenario,
        dir_name=dir_name,
        kind="directory-gfid-mask",
        label="directory-gfid-mask",
        child_name=file_name,
        right_child_name=shadow_file_name,
        ssh_user=ssh_user,
        worker_path=worker_path,
    )


def create_type_mismatch_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    file_name: str,
    shadow_name: str = "shadow.txt",
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 4:
        raise RuntimeError(f"need at least 4 bricks for type-mismatch canary on {volume}")

    mount_root = _canary_temp_mount_root(volume)
    mount_target = f"{mount_root}/{scenario}/{dir_name}"
    mount_shadow = f"{mount_target}/{shadow_name}"
    backend_target_by_host = {
        host: f"{backend_roots[host]}/{scenario}/{dir_name}" for host in brick_hosts
    }
    index_root_by_host = {
        host: f"{backend_roots[host]}/.glusterfs/indices/xattrop" for host in brick_hosts
    }
    backend_root = backend_roots[brick_hosts[0]]
    backend_target = backend_target_by_host[brick_hosts[0]]
    index_root = index_root_by_host[brick_hosts[0]]
    left_hosts = brick_hosts[:2]
    right_hosts = brick_hosts[2:4]
    left_gfid_hex = canary_api._new_gfid_hex()
    right_gfid_hex = canary_api._new_gfid_hex()
    left_gfid = str(uuid.UUID(hex=left_gfid_hex))
    right_gfid = str(uuid.UUID(hex=right_gfid_hex))

    canary_api._run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)

    directory_gfid_paths_by_host: dict[str, list[str]] = {}
    for host in left_hosts + right_hosts:
        is_left = host in left_hosts
        host_backend_root = backend_roots[host]
        host_backend_target = backend_target_by_host[host]
        host_index_root = index_root_by_host[host]
        cohort_gfid_hex = left_gfid_hex if is_left else right_gfid_hex
        opposite_indices = range(2, 4) if is_left else range(0, 2)
        if is_left:
            ops = [
                {
                    "op": "write_text",
                    "path": host_backend_target,
                    "text": f"type-mismatch file on {host}\n",
                },
                {
                    "op": "setxattr",
                    "path": host_backend_target,
                    "name": "trusted.gfid",
                    "value_hex": cohort_gfid_hex,
                },
                {
                    "op": "link_file_gfid_from_target",
                    "target_path": host_backend_target,
                },
                {
                    "op": "touch_index_from_target",
                    "target_path": host_backend_target,
                    "index_root": host_index_root,
                },
            ]
        else:
            ops = [
                {
                    "op": "mkdir",
                    "path": host_backend_target,
                },
                {
                    "op": "write_text",
                    "path": f"{host_backend_target}/{shadow_name}",
                    "text": f"type-mismatch shadow on {host}\n",
                },
                {
                    "op": "setxattr",
                    "path": host_backend_target,
                    "name": "trusted.gfid",
                    "value_hex": cohort_gfid_hex,
                },
                {
                    "op": "link_directory_gfid_from_target",
                    "target_path": host_backend_target,
                },
                {
                    "op": "touch_index_from_target",
                    "target_path": host_backend_target,
                    "index_root": host_index_root,
                },
            ]
        ops.extend(
            [
                {
                    "op": "setxattr",
                    "path": host_backend_target,
                    "name": f"trusted.afr.{volume}-client-{idx}",
                    "value_hex": "000000010000000000000001",
                }
                for idx in opposite_indices
            ]
        )

        response = canary_api._canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(volume, scenario, host_backend_root, ops),
        )
        canary_api._print_response("type-mismatch worker " + host, response)
        worker_results = response.get("results", [])
        link_entry = next(
            (
                item
                for item in worker_results
                if item.get("op") in {"link_directory_gfid_from_target", "link_file_gfid_from_target"} and item.get("ok")
            ),
            {},
        )
        gfid_path = str(link_entry.get("directory_gfid_path") or link_entry.get("file_gfid_path") or "")
        if gfid_path:
            directory_gfid_paths_by_host.setdefault(host, []).append(gfid_path)

    state_payload = {
        "kind": "type-mismatch",
        "volume": volume,
        "scenario": scenario,
        "dir_name": dir_name,
        "file_name": file_name,
        "shadow_name": shadow_name,
        "mount_root": mount_root,
        "mount_target": mount_target,
        "mount_shadow": mount_shadow,
        "backend_root": backend_root,
        "backend_target": backend_target,
        "index_root": index_root,
        "backend_roots": backend_roots,
        "backend_target_by_host": backend_target_by_host,
        "index_root_by_host": index_root_by_host,
        "directory_gfid_paths_by_host": directory_gfid_paths_by_host,
        "left_hosts": left_hosts,
        "right_hosts": right_hosts,
        "left_gfid_uuid": left_gfid,
        "left_gfid_hex": left_gfid_hex,
        "right_gfid_uuid": right_gfid,
        "right_gfid_hex": right_gfid_hex,
    }
    canary_api._write_state(volume, scenario, state_payload)
    snapshot_paths = [mount_target, mount_shadow]
    state_payload["mount_snapshot"] = {
        "paths": [_capture_mount_stat_snapshot(path) for path in snapshot_paths],
    }
    heal_info_before = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    heal_info_split_brain_before = _run_local_text(
        ["sudo", "-n", "gluster", "volume", "heal", volume, "info", "split-brain"]
    )
    heal_trigger_error = ""
    try:
        _trigger_canary_heal(volume, scenario)
    except RuntimeError as exc:
        heal_trigger_error = str(exc)
        print(f"  heal crawl error: {heal_trigger_error}")
    heal_info_after = _run_local_text(["sudo", "-n", "gluster", "volume", "heal", volume, "info"])
    heal_info_split_brain_after = _run_local_text(
        ["sudo", "-n", "gluster", "volume", "heal", volume, "info", "split-brain"]
    )
    state_payload.update(
        {
            "heal_info_before": heal_info_before,
            "heal_info_split_brain_before": heal_info_split_brain_before,
            "heal_info_after": heal_info_after,
            "heal_info_split_brain_after": heal_info_split_brain_after,
        }
    )
    if heal_trigger_error:
        state_payload["heal_trigger_error"] = heal_trigger_error
    canary_api._write_state(volume, scenario, state_payload)
    _write_observation_log(volume, scenario, state_payload)

    print(
        "\n".join(
            [
                "Created gtest type-mismatch canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_target}",
                f"  shadow child: {mount_shadow}",
                f"  left hosts: {', '.join(left_hosts)}",
                f"  right hosts: {', '.join(right_hosts)}",
                f"  backend target: {backend_target}",
                f"  index bucket: {index_root}",
                f"  left directory GFID: {left_gfid}",
                f"  right directory GFID: {right_gfid}",
                "Expected: planner should keep this review-only as a type mismatch.",
                f"  heal info before heal crawl:\n{heal_info_before or '(empty)'}",
                f"  heal info split-brain before heal crawl:\n{heal_info_split_brain_before or '(empty)'}",
                f"  heal info after heal crawl:\n{heal_info_after or '(empty)'}",
                f"  heal info split-brain after heal crawl:\n{heal_info_split_brain_after or '(empty)'}",
                f"  observation log: {_observation_log_path(volume, scenario)}",
                "Next: run the client-side stat or find probe, then use manifest-build/plan-build to classify the shape.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def build_type_mismatch_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = canary_api._read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind != "type-mismatch":
        raise RuntimeError(f"scenario {scenario!r} is not a type mismatch canary")

    mount_root = str(state.get("mount_root") or canary_api._volume_mount_root(volume))
    mount_target = str(state.get("mount_target") or "").strip()
    if not mount_target:
        raise RuntimeError(f"scenario {scenario!r} does not record a mount target")
    if mount_target.startswith(mount_root):
        logical_path = mount_target[len(mount_root) :].lstrip("/")
    else:
        logical_path = mount_target.lstrip("/")
    if not logical_path:
        raise RuntimeError(f"scenario {scenario!r} does not record a usable logical path")

    mount_shadow = str(state.get("mount_shadow") or "").strip()
    left_hosts = [str(host) for host in state.get("left_hosts") or [] if str(host).strip()]
    right_hosts = [str(host) for host in state.get("right_hosts") or [] if str(host).strip()]
    heal_info_before = str(state.get("heal_info_before") or "").strip()
    heal_info_split_brain_before = str(state.get("heal_info_split_brain_before") or "").strip()
    heal_info_after = str(state.get("heal_info_after") or "").strip()
    heal_info_split_brain_after = str(state.get("heal_info_split_brain_after") or "").strip()
    heal_trigger_error = str(state.get("heal_trigger_error") or "").strip()
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
            f"scenario {scenario!r} does not record heal snapshots ({', '.join(missing_heal_snapshots)}); "
            "rerun create-type-mismatch with the current canary before building a plan"
        )
    directory_gfid_paths_by_host = state.get("directory_gfid_paths_by_host") or {}
    if not isinstance(directory_gfid_paths_by_host, dict):
        directory_gfid_paths_by_host = {}
    afr_pending_xattrs_by_host = state.get("afr_pending_xattrs_by_host") or {}
    brick_roles_by_host = canary_api._brick_roles_by_host(volume, state.get("brick_roles_by_host"))
    backend_root = str(state.get("backend_root") or "").strip()
    backend_target = str(state.get("backend_target") or "").strip()
    backend_observations_by_host = _inspect_type_mismatch_backend_observations(
        volume=volume,
        scenario=scenario,
        backend_root=backend_root,
        backend_target=backend_target,
        file_hosts=left_hosts,
        directory_hosts=right_hosts,
        ssh_user=DEFAULT_SERVICE_USER,
        worker_path=str(DEFAULT_WORKER_PATH),
    )
    recommended_choice, recommended_reason, backend_mtime_summary, loser_side = _type_mismatch_quarantine_recommendation(
        backend_observations_by_host,
        file_hosts=left_hosts,
        directory_hosts=right_hosts,
    )

    left_gfid = str(state.get("left_gfid_uuid") or "").strip()
    right_gfid = str(state.get("right_gfid_uuid") or "").strip()
    backend_roots = state.get("backend_roots") or {}
    if not isinstance(backend_roots, dict):
        backend_roots = {}
    file_copies: list[dict[str, Any]] = []
    directory_copies: list[dict[str, Any]] = []
    for host, observation in sorted(backend_observations_by_host.items()):
        if not isinstance(observation, dict) or str(observation.get("error") or "").strip():
            continue
        backend = str(observation.get("path") or backend_target).strip()
        observation_kind = str(observation.get("kind") or "").strip().lower()
        if not backend or not observation_kind:
            continue
        host_backend_root = str(backend_roots.get(host) or backend_root or canary_api._backend_root_for_host(volume, host)).strip()
        paths = directory_gfid_paths_by_host.get(host)
        gfid_path = str(paths[0]).strip() if isinstance(paths, list) and paths and str(paths[0]).strip() else ""
        mtime = _coerce_int(observation.get("mtime"))
        size = _coerce_int(observation.get("size"))
        if observation_kind == "file" and left_gfid:
            file_gfid_path = gfid_path or canary_api._file_gfid_link_path(host_backend_root, left_gfid)
            file_copies.append({
                "host": host,
                "backend": backend,
                "identity": left_gfid,
                "backend_trusted_gfid": left_gfid,
                "file_gfid": left_gfid,
                "file_gfid_path": file_gfid_path,
                "gfid_path": file_gfid_path,
                "size": size,
                "mtime": mtime,
            })
        elif observation_kind in {"dir", "directory"} and right_gfid:
            directory_gfid_path = gfid_path or canary_api._directory_gfid_link_path(host_backend_root, right_gfid)
            directory_copies.append({
                "host": host,
                "backend": backend,
                "identity": right_gfid,
                "backend_trusted_gfid": right_gfid,
                "gfid": right_gfid,
                "gfid_path": directory_gfid_path,
                "size": size,
                "mtime": mtime,
            })
    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": logical_path,
        "action_type": "review_type_mismatch",
        "object_type": "type_mismatch",
        "depth": logical_path.count("/") + 1,
        "mounted_target": mount_target,
        "repair_strategy": "review_type_mismatch",
        "file_hosts": left_hosts,
        "directory_hosts": right_hosts,
        "mount_shadow": mount_shadow,
        "mount_snapshot": state.get("mount_snapshot") or {},
        "backend_target": backend_target,
        "index_root": str(state.get("index_root") or ""),
        "recommended_choice": recommended_choice,
        "recommended_reason": recommended_reason,
        "type_mismatch_loser": loser_side,
        "file_copies": file_copies,
        "directory_copies": directory_copies,
        "directory_gfid_paths_by_host": directory_gfid_paths_by_host,
        "brick_roles_by_host": brick_roles_by_host,
        "heal_info_before": heal_info_before,
        "heal_info_split_brain_before": heal_info_split_brain_before,
        "heal_info_after": heal_info_after,
        "heal_info_split_brain_after": heal_info_split_brain_after,
        "heal_trigger_error": heal_trigger_error,
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            f"file hosts: {', '.join(left_hosts) or 'unknown'}",
            f"directory hosts: {', '.join(right_hosts) or 'unknown'}",
            f"mount shadow: {mount_shadow or 'unknown'}",
            "type-mismatch boundary: file-vs-directory disagreement with heal-visible row",
            f"heal info before heal crawl:\n{heal_info_before or '(empty)'}",
            f"heal info split-brain before heal crawl:\n{heal_info_split_brain_before or '(empty)'}",
            f"heal info after heal crawl:\n{heal_info_after or '(empty)'}",
            f"heal info split-brain after heal crawl:\n{heal_info_split_brain_after or '(empty)'}",
            *([f"heal crawl error: {heal_trigger_error}"] if heal_trigger_error else []),
            *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
            *canary_api._format_afr_pending_xattrs_by_host_for_notes(afr_pending_xattrs_by_host),
            *canary_api._format_type_mismatch_backend_observations_for_notes(backend_observations_by_host),
            f"type-mismatch mtime summary: {backend_mtime_summary}",
            f"type-mismatch recommended choice: {recommended_choice}",
            f"type-mismatch recommended reason: {recommended_reason}",
            "Expected: keep review-only; quarantine_loser when backend mtime clearly separates the file and directory sides, otherwise quarantine both conflicting branches first. On 3- or 4-replica layouts, that means every visible copy on both sides of the split. Use the final report later to choose a source only if one side is authoritative.",
        ],
    }

    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": [action],
    }


def build_directory_mdata_no_majority_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = canary_api._read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind != "directory-mdata-no-majority":
        raise RuntimeError(f"scenario {scenario!r} is not a directory mdata no-majority canary")

    mount_root = str(state.get("mount_root") or canary_api._volume_mount_root(volume))
    mount_target = str(state.get("mount_target") or "").strip()
    if not mount_target:
        raise RuntimeError(f"scenario {scenario!r} does not record a mount target")
    if mount_target.startswith(mount_root):
        logical_path = mount_target[len(mount_root) :].lstrip("/")
    else:
        logical_path = mount_target.lstrip("/")
    if not logical_path:
        raise RuntimeError(f"scenario {scenario!r} does not record a usable logical path")

    marker_host = str(state.get("marker_host") or "").strip()
    canonical_gfid = str(state.get("gfid_uuid") or "").strip()
    backend_roots = state.get("backend_roots") or {}
    if not isinstance(backend_roots, dict):
        backend_roots = {}
    backend_targets_by_host = state.get("backend_targets_by_host") or {}
    if not isinstance(backend_targets_by_host, dict):
        backend_targets_by_host = {}
    mdata_by_host = {
        str(host): str(value or "")
        for host, value in (state.get("mdata_by_host") or {}).items()
        if str(host).strip()
    }
    if not marker_host or not canonical_gfid or not mdata_by_host:
        raise RuntimeError(f"scenario {scenario!r} does not record enough directory mdata state to build a plan")

    directory_backend_by_host = {
        host: str(backend_targets_by_host.get(host) or backend_roots.get(host) or "").strip()
        for host in sorted(mdata_by_host)
        if str(backend_targets_by_host.get(host) or backend_roots.get(host) or "").strip()
    }
    brick_roles_by_host = canary_api._brick_roles_by_host(volume, state.get("brick_roles_by_host"))
    if not directory_backend_by_host:
        raise RuntimeError(f"scenario {scenario!r} does not record per-host backend paths for directory mdata review")

    directory_gfid_path = str(state.get("directory_gfid_path") or "").strip()
    mdata_counts: dict[str, int] = {}
    for value in mdata_by_host.values():
        mdata_counts[value] = mdata_counts.get(value, 0) + 1

    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": logical_path,
        "action_type": "review_directory_metadata",
        "object_type": "directory",
        "depth": logical_path.count("/") + 1,
        "mounted_target": mount_target,
        "repair_strategy": "review_directory_mdata_state",
        "directory_canonical_host": marker_host,
        "directory_canonical_backend": str(directory_backend_by_host.get(marker_host) or backend_targets_by_host.get(marker_host) or backend_roots.get(marker_host) or ""),
        "directory_canonical_gfid": canonical_gfid,
        "directory_mdata_by_host": dict(sorted(mdata_by_host.items())),
        "directory_backend_by_host": dict(sorted(directory_backend_by_host.items())),
        "directory_metadata_mismatch_hosts": sorted(mdata_by_host),
        "brick_roles_by_host": brick_roles_by_host,
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            "directory-mdata-no-majority marker: review-only source-choice proof",
            f"marker host: {marker_host}",
            f"canonical directory GFID: {canonical_gfid}",
            f"directory GFID path: {directory_gfid_path or 'unknown'}",
            "mdata by value: " + ", ".join(f"{value}:{count}" for value, count in sorted(mdata_counts.items())),
            "mdata by host: " + ", ".join(
                f"{host}={mdata_by_host.get(host) or 'missing'}"
                for host in sorted(mdata_by_host)
            ),
            "backend by host: " + ", ".join(
                f"{host}={directory_backend_by_host.get(host) or 'missing'}"
                for host in sorted(directory_backend_by_host)
            ),
            *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
            "Expected: keep review-only; choose mdata_source_host or mdata_source_value before any repair claim.",
            f"Next: sudo -n gluster volume heal {volume} info",
        ],
    }

    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": [action],
    }


def observe_directory_gfid_state_canary(
    *,
    volume: str,
    scenario: str,
) -> None:
    state = canary_api._read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind not in {"directory-gfid-merge", "directory-gfid-tie", "directory-gfid-mask", "type-mismatch"}:
        raise RuntimeError(f"scenario {scenario!r} is not a directory GFID merge/tie canary")
    mount_paths = [str(state.get("mount_target") or "")]
    mount_child = str(state.get("mount_child") or "")
    if mount_child:
        mount_paths.append(mount_child)
    mount_shadow = str(state.get("mount_shadow") or "")
    if mount_shadow:
        mount_paths.append(mount_shadow)
    current_snapshot = {
        "paths": [_capture_mount_stat_snapshot(path) for path in mount_paths if path],
    }
    baseline = state.get("mount_snapshot") or {}
    changed = current_snapshot != baseline
    observation = {
        "paths": current_snapshot["paths"],
        "changed": changed,
    }
    observations = state.get("observations") or []
    if not isinstance(observations, list):
        observations = []
    observations.append(observation)
    state["observations"] = observations
    state["last_observation"] = observation
    canary_api._write_state(volume, scenario, state)
    _write_observation_log(volume, scenario, state, observation)
    print(
        "\n".join(
            [
                f"Observed gtest directory GFID canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  kind: {kind}",
                f"  changed since baseline: {changed}",
                f"  log: {_observation_log_path(volume, scenario)}",
                "  current snapshot:",
                *[f"    - {item}" for item in current_snapshot["paths"]],
                "  baseline snapshot:",
                *[f"    - {item}" for item in (baseline.get("paths") or [])],
            ]
        )
    )


def observe_directory_child_state_canary(
    *,
    volume: str,
    scenario: str,
) -> None:
    state = canary_api._read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind not in {
        "child-gap",
        "directory-child-reference",
        "directory-backend-child-gap",
        "directory-backend-child-gap-bounded",
        "directory-backend-child-gap-canonical",
        "directory-backend-child-gap-mixed",
    }:
        raise RuntimeError(f"scenario {scenario!r} is not a directory child canary")
    snapshot_paths = [str(item) for item in state.get("snapshot_paths", []) if str(item)]
    if not snapshot_paths:
        fallback_path = str(state.get("mount_parent") or state.get("mount_path") or "")
        if fallback_path:
            snapshot_paths = [fallback_path]
    current_snapshot = {
        "paths": [_capture_mount_stat_snapshot(path) for path in snapshot_paths],
    }
    parent_lookup_path = str(state.get("mount_parent") or state.get("mount_path") or "")
    current_parent_lookup = _capture_parent_lookup_snapshot(parent_lookup_path) if parent_lookup_path else {}
    baseline = state.get("mount_snapshot") or {}
    changed = current_snapshot != baseline
    observation = {
        "paths": current_snapshot["paths"],
        "changed": changed,
        "parent_lookup": current_parent_lookup,
        "backend_observations": state.get("backend_observations") or [],
    }
    observations = state.get("observations") or []
    if not isinstance(observations, list):
        observations = []
    observations.append(observation)
    state["observations"] = observations
    state["last_observation"] = observation
    canary_api._write_state(volume, scenario, state)
    _write_observation_log(volume, scenario, state, observation)
    print(
        "\n".join(
            [
                f"Observed gtest directory child canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  kind: {kind}",
                f"  changed since baseline: {changed}",
                f"  log: {_observation_log_path(volume, scenario)}",
                f"  baseline parent lookup: {state.get('baseline_parent_lookup') or {}}",
                "  current snapshot:",
                *[f"    - {item}" for item in current_snapshot["paths"]],
                f"  current parent lookup: {current_parent_lookup or {}}",
                f"  backend observations: {len(observation['backend_observations'])}",
                "  baseline snapshot:",
                *[f"    - {item}" for item in (baseline.get("paths") or [])],
            ]
        )
    )


def build_directory_tie_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = canary_api._read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind not in {"directory-gfid-merge", "directory-gfid-tie", "directory-gfid-mask"}:
        raise RuntimeError(f"scenario {scenario!r} is not a directory GFID merge/tie canary")

    mount_root = str(state.get("mount_root") or canary_api._volume_mount_root(volume))
    mount_target = str(state.get("mount_target") or "").strip()
    if not mount_target:
        raise RuntimeError(f"scenario {scenario!r} does not record a mount target")

    if mount_target.startswith(mount_root):
        logical_path = mount_target[len(mount_root) :].lstrip("/")
    else:
        logical_path = mount_target.lstrip("/")
    if not logical_path:
        raise RuntimeError(f"scenario {scenario!r} does not record a usable logical path")

    left_hosts = [str(host) for host in state.get("left_hosts") or [] if str(host).strip()]
    right_hosts = [str(host) for host in state.get("right_hosts") or [] if str(host).strip()]
    brick_roles_by_host = canary_api._brick_roles_by_host(volume, state.get("brick_roles_by_host"))
    hosts = list(dict.fromkeys(left_hosts + right_hosts))
    if not hosts:
        raise RuntimeError(f"scenario {scenario!r} does not record any brick hosts")
    left_gfid = str(state.get("left_gfid_uuid") or "").strip()
    right_gfid = str(state.get("right_gfid_uuid") or "").strip()
    backend_target = str(state.get("backend_target") or "").strip()

    child_name = str(state.get("child_name") or "").strip()
    right_child_name = str(state.get("right_child_name") or "").strip()
    child_names_by_host = {
        host: [child_name if host in left_hosts else (right_child_name or child_name)]
        for host in hosts
        if child_name
        and (
            kind == "directory-gfid-merge"
            or (kind == "directory-gfid-mask" and (host in left_hosts or right_child_name))
        )
    }
    directory_copies: list[dict[str, Any]] = []
    for host in hosts:
        identity = left_gfid if host in left_hosts else right_gfid
        if not identity:
            continue
        directory_copies.append(
            {
                "host": host,
                "backend": backend_target,
                "identity": identity,
                "backend_trusted_gfid": identity,
                "gfid": identity,
            }
        )
    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": logical_path,
        "action_type": "review_directory_gfid_conflict",
        "object_type": "directory",
        "depth": logical_path.count("/") + 1,
        "repair_strategy": "rename_conflicting_directory",
        "mounted_target": mount_target,
        "healthy_hosts": hosts,
        "missing_hosts": [],
        "directory_canonical_gfid": left_gfid if kind == "directory-gfid-merge" else "",
        "directory_canonical_backend": backend_target if kind == "directory-gfid-merge" else "",
        "directory_copies": directory_copies,
        "directory_child_names_by_host": child_names_by_host,
        "brick_roles_by_host": brick_roles_by_host,
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            *canary_api._format_brick_roles_by_host_for_notes(brick_roles_by_host),
            *(
                [
                    "directory-backend-child-gap marker",
                    "bounded subtree canary",
                ]
                if kind == "directory-backend-child-gap"
                else []
            ),
        ],
    }
    if kind == "directory-gfid-merge":
        action["notes"].append("canary_bridge:merge tree uses the same child set on every brick")
    elif kind == "directory-gfid-mask":
        action["notes"].append("canary_bridge:mask tree seeds the child only on the left cohort")
    else:
        action["notes"].append("canary_bridge:tie tree has no seeded child set, so merge should stay review-only")

    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": [action],
    }


def build_directory_backend_child_gap_plan_from_canary_state(
    *,
    volume: str,
    scenario: str,
) -> dict[str, Any]:
    state = canary_api._read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind not in {
        "directory-backend-child-gap",
        "directory-backend-child-gap-bounded",
        "directory-backend-child-gap-canonical",
        "directory-backend-child-gap-mixed",
    }:
        raise RuntimeError(f"scenario {scenario!r} is not a backend directory child-gap canary")

    mount_root = str(state.get("mount_root") or canary_api._volume_mount_root(volume))
    mount_parent = str(state.get("mount_parent") or "").strip()
    mount_path = str(state.get("mount_path") or "").strip()
    backend_scenario_root = str(state.get("backend_scenario_root") or "").strip()
    missing_backend = str(state.get("missing_backend") or "").strip()
    missing_host = str(state.get("missing_host") or "").strip()
    parent_name = str(state.get("parent_name") or "").strip()
    present_child = str(state.get("present_child") or "").strip()
    missing_child = str(state.get("missing_child") or "").strip()
    backend_observations = state.get("backend_observations") or []
    file_children = state.get("file_children") or []
    if not missing_backend or not missing_host or not mount_path:
        raise RuntimeError(f"scenario {scenario!r} does not record enough child-gap state to build a plan")

    actions: list[dict[str, Any]] = []
    if kind == "directory-backend-child-gap-mixed":
        if not isinstance(file_children, list) or not file_children:
            raise RuntimeError(f"scenario {scenario!r} does not record enough mixed child-gap file state to build a plan")
        for index, file_state in enumerate(file_children, start=1):
            if not isinstance(file_state, dict):
                continue
            file_name = str(file_state.get("file_name") or "").strip()
            mount_file = str(file_state.get("mount_file") or "").strip()
            backend_target = str(file_state.get("backend_target") or "").strip()
            file_gfid_path = str(file_state.get("file_gfid_path") or "").strip()
            gfid_uuid = str(file_state.get("gfid_uuid") or "").strip()
            source_host = str(file_state.get("source_host") or "").strip() or str(state.get("source_host") or "").strip()
            if not file_name or not mount_file or not backend_target or not gfid_uuid or not source_host:
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
                    "mixed child-gap variant: keep the root-level file repair explicit before the directory subtree repair",
                    f"source host: {source_host}",
                    f"missing host: {missing_host}",
                    f"backend target: {backend_target}",
                    f"file GFID path: {file_gfid_path or 'unknown'}",
                    f"mount directory: {mount_parent or 'unknown'}",
                    f"backend root: {backend_scenario_root or 'unknown'}",
                    f"scenario directory: {parent_name or 'unknown'}",
                    f"file name: {file_name}",
                ],
            }
            if file_gfid_path:
                action["stale_file_gfid_paths_by_host"] = {missing_host: [file_gfid_path]}
            actions.append(action)

    healthy_hosts: list[str] = []
    canonical_host = ""
    canonical_gfid = ""
    for host_entry in backend_observations:
        host = str(host_entry.get("host") or "").strip()
        results = host_entry.get("results") or []
        if not host or not isinstance(results, list):
            continue
        for result in results:
            if str(result.get("label") or "") != "backend_missing_child":
                continue
            if result.get("ok"):
                if host not in healthy_hosts:
                    healthy_hosts.append(host)
                if not canonical_host:
                    canonical_host = host
                if not canonical_gfid:
                    canonical_gfid = str(result.get("trusted_gfid") or "").strip()
                break

    if not healthy_hosts:
        raise RuntimeError(f"scenario {scenario!r} did not record a surviving backend child copy")
    if not canonical_gfid:
        raise RuntimeError(f"scenario {scenario!r} did not record a canonical child GFID")

    action = {
        "action_id": f"canary:{scenario}",
        "logical_path": mount_path,
        "action_type": "repair_directory_metadata",
        "object_type": "directory",
        "depth": mount_path.count("/") + 1,
        "repair_strategy": (
            "canonical_content_recreate"
            if kind == "directory-backend-child-gap-canonical"
            else (
                "restore_directory_children_bounded"
                if kind == "directory-backend-child-gap-bounded"
                else "recreate_missing_directory_backend_child_gap"
            )
        ),
        "mounted_target": mount_path,
        "healthy_hosts": healthy_hosts,
        "missing_hosts": [missing_host],
        "directory_canonical_host": canonical_host,
        "directory_canonical_backend": missing_backend,
        "directory_canonical_gfid": canonical_gfid,
        "directory_metadata_mismatch_hosts": [missing_host],
        "canary_kind": kind,
        "canary_volume": volume,
        "canary_name": scenario,
        "canary_observation_log": str(_observation_log_path(volume, scenario)),
        "notes": [
            f"canary_kind:{kind}",
            f"canary_volume:{volume}",
            f"canary_name:{scenario}",
            f"canary_observation_log:{_observation_log_path(volume, scenario)}",
            *(
                ["directory child-gap variant: canonical content recreate fallback"]
                if kind == "directory-backend-child-gap-canonical"
                else (
                    ["directory child-gap variant: bounded subtree repair candidate"]
                    if kind == "directory-backend-child-gap-bounded"
                    else ["directory child-gap variant: backend recreate remains explicit for the missing child path"]
                )
            ),
            "directory-backend-child-gap marker",
            *(
                ["bounded subtree canary"]
                if kind == "directory-backend-child-gap-bounded"
                else []
            ),
            *(
                ["canonical content recreate canary"]
                if kind == "directory-backend-child-gap-canonical"
                else []
            ),
            f"child path: {missing_child or 'unknown'}",
            f"parent path: {mount_parent or 'unknown'}",
            f"backend scenario root: {backend_scenario_root or 'unknown'}",
            f"canonical child GFID: {canonical_gfid}",
            f"missing host: {missing_host}",
            *(
                ["mixed file-and-directory child-gap canary"]
                if kind == "directory-backend-child-gap-mixed"
                else []
            ),
        ],
    }
    if parent_name and present_child:
        action["notes"].append(f"parent/child anchor: {parent_name}/{present_child}")

    actions.append(action)

    return {
        "schema_version": 1,
        **canary_state_plan_provenance(kind=kind, volume=volume, scenario=scenario),
        "actions": actions,
    }


def create_directory_metadata_canary(
    *,
    volume: str,
    scenario: str,
    mismatch_host: str | None = None,
    dir_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    mismatch_host = canary_api._select_role_host(brick_hosts, mismatch_host, role="mismatch", default_to_next=True)
    canonical_hosts = [host for host in brick_hosts if host != mismatch_host]
    marker_host = canonical_hosts[0] if canonical_hosts else ""
    if not marker_host:
        raise RuntimeError(f"need at least one marker host besides {mismatch_host}")

    mount_root = _canary_temp_mount_root(volume)
    mount_target = f"{mount_root}/{scenario}/{dir_name}"
    backend_target = f"{backend_roots[marker_host]}/{scenario}/{dir_name}"
    canary_api._run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(mount_target)
    canary_api._run_local(["sudo", "-n", "touch", f"{mount_target}/metadata-seed.txt"])
    canary_api._run_local(["sudo", "-n", "stat", f"{mount_target}/metadata-seed.txt"])
    canary_api._run_local(["sudo", "-n", "stat", mount_target])
    canary_api._run_local(["sleep", "3"])

    marker_response = canary_api._canary_worker_command(
        marker_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=canary_api._make_request(
            volume,
            scenario,
            backend_roots[marker_host],
            [
                {
                    "op": "getxattr",
                    "path": backend_target,
                    "name": "trusted.gfid",
                },
                {
                    "op": "getxattr",
                    "path": backend_target,
                    "name": "trusted.glusterfs.mdata",
                },
            ],
        ),
    )
    canary_api._print_response("directory-metadata marker worker", marker_response)
    marker_results = marker_response.get("results", [])
    gfid_entry = next((item for item in marker_results if item.get("op") == "getxattr" and item.get("ok")), {})
    canonical_gfid = str(gfid_entry.get("value_uuid") or "")
    if not canonical_gfid:
        raise RuntimeError("directory-metadata canary worker did not return a canonical directory GFID")
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
    if not canonical_mdata_hex:
        raise RuntimeError("directory-metadata canary worker did not return ctime metadata; ensure ctime is enabled")
    for canonical_host in canonical_hosts:
        canonical_response = canary_api._canary_worker_command(
            canonical_host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                backend_roots[canonical_host],
                [
                    {
                        "op": "setxattr",
                        "path": backend_target,
                        "name": "trusted.glusterfs.mdata",
                        "value_hex": canonical_mdata_hex,
                    }
                ],
            ),
        )
        canary_api._print_response("directory-metadata canonical worker", canonical_response)
    directory_gfid_path = canary_api._directory_gfid_link_path(backend_roots[marker_host], canonical_gfid)

    mismatch_response = canary_api._canary_worker_command(
        mismatch_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=canary_api._make_request(
            volume,
            scenario,
            backend_roots[mismatch_host],
            [
                {
                    "op": "removexattr",
                    "path": backend_target,
                    "name": "trusted.glusterfs.mdata",
                }
            ],
        ),
    )
    canary_api._print_response("directory-metadata mismatch worker", mismatch_response)

    canary_api._write_state(
        volume,
        scenario,
        {
            "kind": "directory-metadata",
            "volume": volume,
            "scenario": scenario,
            "mismatch_host": mismatch_host,
            "marker_host": marker_host,
            "dir_name": dir_name,
            "mount_root": mount_root,
            "mount_target": mount_target,
            "backend_root": backend_roots[marker_host],
            "backend_roots": backend_roots,
            "backend_target": backend_target,
            "directory_gfid_path": directory_gfid_path,
            "gfid_uuid": canonical_gfid,
            "gfid_hex": canonical_gfid.replace("-", ""),
            "mdata_hex": canonical_mdata_hex,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest directory-ctime / mdata majority case:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_target}",
                f"  mismatch host: {mismatch_host}",
                f"  marker host: {marker_host}",
                f"  backend target: {backend_target}",
                f"  canonical directory GFID: {canonical_gfid}",
                f"  canonical ctime metadata: {canonical_mdata_hex}",
                f"  directory GFID path: {directory_gfid_path}",
                "Expected: the repair tool should classify this as majority-backed trusted.glusterfs.mdata alignment when two bricks agree, otherwise review-only.",
                "Next canary: a no-majority directory mdata case or a brick-down metadata split-brain.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )


def create_directory_mdata_no_majority_canary(
    *,
    volume: str,
    scenario: str,
    dir_name: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    brick_hosts, backend_roots = _brick_hosts_and_backend_roots(volume)
    if len(brick_hosts) < 2:
        raise RuntimeError("need at least two brick hosts for a no-majority directory mdata canary")

    mount_root = _canary_temp_mount_root(volume)
    mount_target = f"{mount_root}/{scenario}/{dir_name}"
    marker_host = brick_hosts[0]
    marker_backend_target = f"{backend_roots[marker_host]}/{scenario}/{dir_name}"
    canary_api._run_local(["sudo", "-n", "gluster", "volume", "heal", volume, "disable"])
    _ensure_canary_mount(volume, mount_root)
    canary_api._local_mount_dir(mount_target)
    canary_api._run_local(["sudo", "-n", "touch", f"{mount_target}/metadata-seed.txt"])
    canary_api._run_local(["sudo", "-n", "stat", f"{mount_target}/metadata-seed.txt"])
    canary_api._run_local(["sudo", "-n", "stat", mount_target])
    canary_api._run_local(["sleep", "3"])

    marker_response = canary_api._canary_worker_command(
        marker_host,
        ssh_user=ssh_user,
        worker_path=worker_path,
        request=canary_api._make_request(
            volume,
            scenario,
            backend_roots[marker_host],
            [
                {
                    "op": "getxattr",
                    "path": marker_backend_target,
                    "name": "trusted.gfid",
                },
                {
                    "op": "getxattr",
                    "path": marker_backend_target,
                    "name": "trusted.glusterfs.mdata",
                },
            ],
        ),
    )
    canary_api._print_response("directory-mdata-no-majority marker worker", marker_response)
    marker_results = marker_response.get("results", [])
    gfid_entry = next((item for item in marker_results if item.get("op") == "getxattr" and item.get("ok")), {})
    canonical_gfid = str(gfid_entry.get("value_uuid") or "")
    if not canonical_gfid:
        raise RuntimeError("directory-mdata no-majority canary worker did not return a canonical directory GFID")

    mdata_by_host: dict[str, str] = {}
    backend_targets_by_host: dict[str, str] = {}
    for index, host in enumerate(brick_hosts):
        # Synthetic four-byte values are enough to create distinct mdata cohorts for the planner.
        value_hex = f"0x{index + 1:08x}"
        backend_target = f"{backend_roots[host]}/{scenario}/{dir_name}"
        backend_targets_by_host[host] = backend_target
        mdata_by_host[host] = value_hex
        response = canary_api._canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=canary_api._make_request(
                volume,
                scenario,
                backend_roots[host],
                [
                    {
                        "op": "setxattr",
                        "path": backend_target,
                        "name": "trusted.glusterfs.mdata",
                        "value_hex": value_hex,
                    }
                ],
            ),
        )
        canary_api._print_response("directory-mdata-no-majority host worker", response)

    directory_gfid_path = canary_api._directory_gfid_link_path(backend_roots[marker_host], canonical_gfid)
    canary_api._write_state(
        volume,
        scenario,
        {
            "kind": "directory-mdata-no-majority",
            "volume": volume,
            "scenario": scenario,
            "marker_host": marker_host,
            "dir_name": dir_name,
            "mount_root": mount_root,
            "mount_target": mount_target,
            "backend_root": backend_roots[marker_host],
            "backend_roots": backend_roots,
            "backend_targets_by_host": backend_targets_by_host,
            "directory_gfid_path": directory_gfid_path,
            "gfid_uuid": canonical_gfid,
            "gfid_hex": canonical_gfid.replace("-", ""),
            "mdata_by_host": mdata_by_host,
        },
    )

    print(
        "\n".join(
            [
                "Created gtest directory mdata no-majority case:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  logical path: {mount_target}",
                f"  marker host: {marker_host}",
                f"  canonical directory GFID: {canonical_gfid}",
                "  mdata by host: "
                + ", ".join(f"{host}={value}" for host, value in sorted(mdata_by_host.items())),
                f"  directory GFID path: {directory_gfid_path}",
                "Expected: the repair tool should keep this review-only, print per-host mdata evidence, and ask for mdata_source_host or mdata_source_value.",
                f"Next: sudo -n gluster volume heal {volume} info",
            ]
        )
    )
