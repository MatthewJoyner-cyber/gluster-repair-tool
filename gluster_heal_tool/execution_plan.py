# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Execution wave helpers for Gluster repair planning."""
from __future__ import annotations

from heapq import heappop, heappush
from typing import Any

from .models import PlanAction


PARALLEL_ACTION_TYPES = {
    "cleanup_orphaned_symlink",
    "cleanup_stale_glusterfs_index",
}

_PATH_BASED_RESOURCE_KINDS = {
    "backend",
    "dead-gfid-ref",
    "gfid-path",
    "logical",
    "mount",
    "stage",
}


def _normalized_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text == "/":
        return "/"
    parts: list[str] = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    normalized = "/" + "/".join(parts)
    if text.startswith("/") or normalized == "/":
        return normalized
    return normalized.lstrip("/")


def _paths_overlap(left: str, right: str) -> bool:
    left_norm = _normalized_path(left)
    right_norm = _normalized_path(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    left_prefix = left_norm.rstrip("/") + "/"
    right_prefix = right_norm.rstrip("/") + "/"
    return left_norm.startswith(right_prefix) or right_norm.startswith(left_prefix)


def _resource_key(resource: str) -> tuple[str, str, str]:
    parts = str(resource or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return str(resource or ""), "", ""


def _add_path_resource(resources: set[str], kind: str, path: str, *, host: str = "") -> None:
    cleaned = _normalized_path(path)
    if not cleaned:
        return
    if host:
        resources.add(f"{kind}:{host}:{cleaned}")
    else:
        resources.add(f"{kind}:{cleaned}")


def _add_token_resource(resources: set[str], kind: str, value: str) -> None:
    cleaned = str(value or "").strip()
    if cleaned:
        resources.add(f"{kind}:{cleaned}")


def _add_host_path_resources(resources: set[str], kind: str, values_by_host: dict[str, Any]) -> None:
    for host, value in sorted(values_by_host.items()):
        host_name = str(host or "").strip()
        if isinstance(value, str):
            _add_path_resource(resources, kind, value, host=host_name)
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, str):
                    _add_path_resource(resources, kind, item, host=host_name)


def action_execution_resources(action: PlanAction) -> list[str]:
    resources: set[str] = set()

    _add_path_resource(resources, "logical", action.logical_path)
    _add_path_resource(resources, "stage", action.stage_local_path)
    _add_path_resource(resources, "mount", action.mounted_target)

    if action.winner_backend:
        _add_path_resource(resources, "backend", action.winner_backend, host=action.winner_host)

    if action.metadata_source_backend:
        _add_path_resource(resources, "backend", action.metadata_source_backend, host=action.metadata_source_host)
    _add_host_path_resources(resources, "backend", action.metadata_backend_by_host)

    if action.directory_canonical_backend:
        _add_path_resource(resources, "backend", action.directory_canonical_backend, host=action.directory_canonical_host)
    _add_host_path_resources(resources, "backend", action.directory_backend_by_host)

    _add_host_path_resources(resources, "backend", action.stale_backends_by_host)
    for item in action.stale_backends:
        _add_path_resource(resources, "backend", item)

    _add_host_path_resources(resources, "gfid-path", action.stale_gfid_paths_by_host)
    for item in action.stale_gfid_paths:
        _add_path_resource(resources, "gfid-path", item)

    _add_host_path_resources(resources, "gfid-path", action.stale_file_gfid_paths_by_host)
    for item in action.stale_file_gfid_paths:
        _add_path_resource(resources, "gfid-path", item)

    for item in action.dead_gfids:
        _add_token_resource(resources, "gfid", item)
    for item in action.dead_gfid_live_references:
        _add_path_resource(resources, "dead-gfid-ref", item)

    return sorted(resources)


def actions_conflict(left: object, right: object) -> bool:
    left_path = str(getattr(left, "logical_path", "") or "")
    right_path = str(getattr(right, "logical_path", "") or "")
    if _paths_overlap(left_path, right_path):
        return True

    left_resources = [str(item) for item in getattr(left, "execution_resources", []) or []]
    right_resources = [str(item) for item in getattr(right, "execution_resources", []) or []]
    if set(left_resources).intersection(right_resources):
        return True

    left_keyed = [_resource_key(item) for item in left_resources]
    right_keyed = [_resource_key(item) for item in right_resources]
    for left_kind, left_host, left_value in left_keyed:
        if left_kind not in _PATH_BASED_RESOURCE_KINDS:
            continue
        for right_kind, right_host, right_value in right_keyed:
            if left_kind != right_kind or left_host != right_host:
                continue
            if _paths_overlap(left_value, right_value):
                return True
    return False


def execution_dependency_errors(actions: list[object]) -> dict[int, list[str]]:
    """Validate saved dependency identities and waves without repairing the plan."""
    by_id: dict[str, list[object]] = {}
    for action in actions:
        by_id.setdefault(action.action_id, []).append(action)
    errors: dict[int, list[str]] = {}
    for index, action in enumerate(actions, start=1):
        reasons = []
        if not action.action_id or len(by_id[action.action_id]) != 1:
            reasons.append("missing or duplicate action identity")
        for dependency in action.depends_on:
            matches = by_id.get(dependency, [])
            if len(matches) != 1:
                reasons.append(f"prerequisite {dependency!r} is missing or ambiguous")
            elif dependency == action.action_id:
                reasons.append(f"prerequisite {dependency!r} refers to the action itself")
            elif matches[0].execution_wave >= action.execution_wave:
                reasons.append(f"prerequisite {dependency!r} must be in an earlier execution wave")
        if reasons:
            errors[index] = reasons
    return errors


def _topological_order(actions: list[PlanAction]) -> tuple[list[PlanAction], dict[str, list[str]], set[str]]:
    action_by_id: dict[str, PlanAction] = {}
    original_index: dict[str, int] = {}
    indegree: dict[str, int] = {}
    outgoing: dict[str, list[str]] = {}
    unresolved_dependencies: dict[str, list[str]] = {}

    for index, action in enumerate(actions):
        action_id = str(action.action_id or "").strip()
        if not action_id:
            continue
        action_by_id[action_id] = action
        original_index[action_id] = index
        indegree[action_id] = 0
        outgoing[action_id] = []

    for action_id, action in action_by_id.items():
        missing: list[str] = []
        for dependency in action.depends_on:
            dep_id = str(dependency or "").strip()
            if not dep_id:
                continue
            if dep_id in action_by_id:
                indegree[action_id] += 1
                outgoing.setdefault(dep_id, []).append(action_id)
            else:
                missing.append(dep_id)
        if missing:
            unresolved_dependencies[action_id] = sorted(set(missing))

    ready: list[tuple[int, str]] = []
    for action_id, degree in indegree.items():
        if degree == 0:
            heappush(ready, (original_index[action_id], action_id))

    ordered_ids: list[str] = []
    while ready:
        _, action_id = heappop(ready)
        ordered_ids.append(action_id)
        for child_id in sorted(outgoing.get(action_id, []), key=lambda item: original_index[item]):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                heappush(ready, (original_index[child_id], child_id))

    remaining_ids = [
        action_id
        for action_id in sorted(action_by_id, key=lambda item: original_index[item])
        if action_id not in ordered_ids
    ]
    cycle_ids = set(remaining_ids)
    ordered = [action_by_id[action_id] for action_id in ordered_ids]
    ordered.extend(action_by_id[action_id] for action_id in remaining_ids)
    return ordered, unresolved_dependencies, cycle_ids


def annotate_execution_waves(actions: list[PlanAction]) -> None:
    if not actions:
        return

    ordered_actions, unresolved_dependencies, cycle_ids = _topological_order(actions)
    wave_members: dict[int, list[PlanAction]] = {}
    assigned_wave_by_id: dict[str, int] = {}

    for action in ordered_actions:
        action_id = str(action.action_id or "").strip()
        action.execution_resources = action_execution_resources(action)

        serialization_reasons: list[str] = []
        can_parallelize = action.action_type in PARALLEL_ACTION_TYPES
        if not can_parallelize:
            serialization_reasons.append(f"action type {action.action_type} is not in the parallel allowlist")
        if action_id in unresolved_dependencies:
            serialization_reasons.append(
                "unresolved dependencies: " + ", ".join(unresolved_dependencies[action_id])
            )
        if action_id in cycle_ids:
            serialization_reasons.append("dependency cycle detected")

        action.parallel_safe = not serialization_reasons
        action.execution_serialization_reason = "; ".join(serialization_reasons)

        candidate_wave = 0
        for dependency in action.depends_on:
            dep_wave = assigned_wave_by_id.get(str(dependency or "").strip())
            if dep_wave is not None:
                candidate_wave = max(candidate_wave, dep_wave + 1)

        wave = candidate_wave
        while True:
            members = wave_members.get(wave, [])
            if not members:
                break
            if not action.parallel_safe:
                wave += 1
                continue
            if any(not member.parallel_safe for member in members):
                wave += 1
                continue
            if any(actions_conflict(action, member) for member in members):
                wave += 1
                continue
            break

        action.execution_wave = wave
        wave_members.setdefault(wave, []).append(action)
        if action_id:
            assigned_wave_by_id[action_id] = wave
