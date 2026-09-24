# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""File-family canary observation helpers."""
from __future__ import annotations

from typing import Any

from . import canary_shared as canary_api
from .canary_observation import _capture_mount_stat_snapshot
from .canary_observation import _capture_parent_lookup_snapshot
from .canary_observation import _latest_heal_snapshot_path
from .canary_observation import _observation_log_path
from .canary_observation import _write_observation_log
from .install_paths import DEFAULT_SERVICE_USER
from .install_paths import DEFAULT_WORKER_PATH

_brick_hosts_and_root = canary_api._brick_hosts_and_root
_read_state = canary_api._read_state
_write_state = canary_api._write_state
_canary_worker_command = canary_api._canary_worker_command
_make_request = canary_api._make_request

_FILE_OBSERVER_KINDS = {
    "file-handle-ghost",
    "missing-file-replica",
    "orphaned-gfid-hardlink",
    "file-stale-survivor",
    "symlink-missing-stale",
}


def _file_mount_paths(state: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("mount_dir", "mount_target", "mount_file", "ghost_ref"):
        value = str(state.get(key) or "")
        if value and value not in paths:
            paths.append(value)
    return paths


def _record_file_baseline(volume: str, scenario: str, state: dict[str, Any]) -> None:
    snapshot_paths = _file_mount_paths(state)
    state["snapshot_paths"] = snapshot_paths
    state["mount_snapshot"] = {
        "paths": [_capture_mount_stat_snapshot(path) for path in snapshot_paths],
    }
    mount_parent = str(state.get("mount_dir") or state.get("mount_target") or "")
    if mount_parent:
        state["baseline_parent_lookup"] = _capture_parent_lookup_snapshot(mount_parent)
    state["latest_heal_snapshot"] = _latest_heal_snapshot_path(volume)
    _write_state(volume, scenario, state)
    _write_observation_log(volume, scenario, state)


def _file_gfid_path_from_state(state: dict[str, Any]) -> str:
    return str(state.get("ghost_gfid_path") or state.get("file_gfid_path") or "")


def _file_inspection_paths(state: dict[str, Any]) -> list[tuple[str, str]]:
    candidates = [
        ("backend_target", str(state.get("backend_target") or "")),
        ("file_gfid_path", _file_gfid_path_from_state(state)),
    ]
    index_root = str(state.get("index_root") or "")
    gfid_uuid = str(state.get("gfid_uuid") or "")
    gfid_hex = str(state.get("gfid_hex") or "").replace("-", "")
    if index_root and gfid_uuid:
        candidates.append(("index_entry_uuid", f"{index_root}/{gfid_uuid}"))
    if index_root and gfid_hex:
        candidates.append(("index_entry_hex", f"{index_root}/{gfid_hex}"))
    paths: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, path in candidates:
        if path and path not in seen:
            seen.add(path)
            paths.append((label, path))
    return paths


def _capture_file_brick_observations(
    *,
    volume: str,
    scenario: str,
    backend_root: str,
    brick_hosts: list[str],
    state: dict[str, Any],
    ssh_user: str,
    worker_path: str,
) -> list[dict[str, Any]]:
    ops = [{"op": "inspect_path", "label": label, "path": path} for label, path in _file_inspection_paths(state)]
    observations: list[dict[str, Any]] = []
    if not ops:
        return observations
    for host in brick_hosts:
        response = _canary_worker_command(
            host,
            ssh_user=ssh_user,
            worker_path=worker_path,
            request=_make_request(volume, scenario, backend_root, ops),
        )
        observations.append(
            {
                "host": response.get("host") or host,
                "results": response.get("results", []),
            }
        )
    return observations


def observe_file_state_canary(
    *,
    volume: str,
    scenario: str,
    heal_root: str | None = None,
    ssh_user: str = DEFAULT_SERVICE_USER,
    worker_path: str = str(DEFAULT_WORKER_PATH),
) -> None:
    state = _read_state(volume, scenario)
    kind = str(state.get("kind") or "")
    if kind not in _FILE_OBSERVER_KINDS:
        raise RuntimeError(f"scenario {scenario!r} is not a supported file-family canary")
    brick_hosts, discovered_backend_root = _brick_hosts_and_root(volume)
    backend_root = str(state.get("backend_root") or discovered_backend_root)
    snapshot_paths = [path for path in _file_mount_paths(state) if path]
    current_snapshot = {
        "paths": [_capture_mount_stat_snapshot(path) for path in snapshot_paths],
    }
    baseline = state.get("mount_snapshot") or {}
    mount_parent = str(state.get("mount_dir") or state.get("mount_target") or "")
    observation = {
        "paths": current_snapshot["paths"],
        "parent_lookup": _capture_parent_lookup_snapshot(mount_parent) if mount_parent else {},
        "latest_heal_snapshot": _latest_heal_snapshot_path(volume, heal_root),
        "file_gfid_path": _file_gfid_path_from_state(state),
        "brick_observations": _capture_file_brick_observations(
            volume=volume,
            scenario=scenario,
            backend_root=backend_root,
            brick_hosts=brick_hosts,
            state=state,
            ssh_user=ssh_user,
            worker_path=worker_path,
        ),
        "changed": current_snapshot != baseline,
    }
    observations = state.get("observations") or []
    if not isinstance(observations, list):
        observations = []
    observations.append(observation)
    state["observations"] = observations
    state["last_observation"] = observation
    state["latest_heal_snapshot"] = observation["latest_heal_snapshot"]
    _write_state(volume, scenario, state)
    _write_observation_log(volume, scenario, state, observation)
    print(
        "\n".join(
            [
                "Observed gtest file canary:",
                f"  volume: {volume}",
                f"  scenario: {scenario}",
                f"  kind: {kind}",
                f"  changed since baseline: {observation['changed']}",
                f"  latest heal snapshot: {observation['latest_heal_snapshot'] or '(none recorded)'}",
                f"  file GFID path: {observation['file_gfid_path'] or '(none recorded)'}",
                f"  log: {_observation_log_path(volume, scenario)}",
                "  current snapshot:",
                *[f"    - {item}" for item in current_snapshot["paths"]],
                "  brick observations:",
                *[
                    f"    - {host_entry.get('host', '')}: {len(host_entry.get('results', []))} inspected path(s)"
                    for host_entry in observation["brick_observations"]
                ],
            ]
        )
    )
